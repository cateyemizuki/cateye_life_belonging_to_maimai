"""观看任务管理器：有界队列 + 串行 worker 子进程 + 取消/去重/缓存/重启恢复。

设计要点（B 计划 §4/§6）：
- **任务先回执**：submit 立即返回排队/受理状态，绝不阻塞工具 RPC 或日程 tick；
- **串行有界**：同一时刻至多一个 worker 子进程，队列容量有界，满了如实拒绝；
- **去重**：同 BV 的进行中任务只返回既有 task_id（任务关联唯一，不每拍重复提交）；
- **缓存**：已完成任务的完整范围结果按 BV 复用（缓存命中直接返回结果）；
- **预算**：总时限由父侧强制（deadline 到点 kill 子进程 → failed/timeout）；
- **隔离诚实**：V1 采用「子进程 + 硬截止 + 进程树终止」的降级隔离，不做内存
  硬上限假称；Windows Job 对象等硬隔离留待生产加固（README 明确记录）；
- **重启恢复**：进行中任务一律如实置为 failed/interrupted_by_restart，
  只有已确认的终态与缓存可复用，不自动风暴重试。
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .store import WatchStore, atomic_write_json, read_json

_QUEUED = "queued"
_RUNNING = "running"
_COMPLETED = "completed"
_FAILED = "failed"
_CANCELLED = "cancelled"

#: 任务台账的兼容来源（2026-11-10 刷视频批）：`source` 仍只保留 schedule/tool
#: （既有端口/分享/测试口径不变）；原始传入值与语义枚举另存 requested_source/
#: source_kind，历史条目写 source（tool/schedule/event）与 source_kind。
_SOURCE_KIND_BY_SOURCE = {
    "schedule": "autonomous_schedule",
    "autonomous_schedule": "autonomous_schedule",
    "event": "autonomous_event",
    "autonomous_event": "autonomous_event",
    "tool": "tool",
    "debug": "tool",
}


def _source_kind(source: str) -> str:
    return _SOURCE_KIND_BY_SOURCE.get(str(source or "").strip().lower(), "tool")


def _history_source(task: Dict[str, Any]) -> str:
    """历史/投影 source：tool / autonomous_schedule / autonomous_event。

    与 dashboard（S2）和前端视频来源标签的跨流域契约一致；debug 归 tool。
    优先使用入队时已细分的 source_kind（gap-* 小事件不会被误标成日程）。
    """
    kind = str(task.get("source_kind") or "").strip().lower()
    if kind in ("tool", "autonomous_schedule", "autonomous_event"):
        return kind
    requested = str(task.get("requested_source") or "").strip().lower()
    if requested in ("tool", "schedule", "event"):
        return _source_kind(requested)
    return _source_kind(str(task.get("source") or "tool"))


class WatchTaskManager:
    """任务台账 + worker 子进程编排。SDK 无关（worker 命令可注入以便测试）。"""

    def __init__(self, data_dir: Path, logger: Any = None,
                 queue_capacity: int = 3, task_deadline_seconds: int = 900,
                 max_video_duration_seconds: int = 1800,
                 worker_cmd: Optional[List[str]] = None,
                 visual_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 cache_retention_days: int = 15,
                 qzone_assets_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None) -> None:
        self._store = WatchStore(Path(data_dir), cache_retention_days)
        self._dir = Path(data_dir)
        self._log = logger
        self._queue_capacity = max(1, int(queue_capacity))
        # 下限仅作编程兜底；生产下限由配置模型 ge=60 把关（测试需要更短时限）
        self._deadline_seconds = max(1, int(task_deadline_seconds))
        self._max_duration = max(0, int(max_video_duration_seconds))
        # worker 命令可注入（隔离测试用假 worker）；默认 = 本包 worker.py 子进程
        self._worker_cmd = worker_cmd  # type: ignore[assignment]
        # 采样能力标记仍用于运行任务去重与历史口径；文字记忆按BV复用。
        self._visual_provider = visual_provider
        # PR-A1：空间高清截图提取参数（None = 不提取，shadow/off 零开销）
        self._qzone_assets_provider = qzone_assets_provider
        self._proc: Optional[Any] = None
        self._current_task_id: Optional[str] = None
        self._cancel_requested: set = set()
        self._pump_task: Optional[asyncio.Task] = None
        self._impression_hook: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._postprocess_task: Optional[asyncio.Task] = None
        self._task_guard: Optional[Callable[[Dict[str, Any]], bool]] = None
        self._closed = False
        self._state_listener: Optional[Callable[[], None]] = None
        # 2026-11-04 刷视频功能批：自动候选提供器（装配根注入 integrations 兴趣
        # 候选源的同步 pick 面；None = 未接入，next_candidate 如实 no_candidate_source）
        self._candidate_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._recover()

    def set_task_guard(self, guard: Callable[[Dict[str, Any]], bool]) -> None:
        self._task_guard = guard

    def set_state_listener(self, listener: Callable[[], None]) -> None:
        self._state_listener = listener

    def _notify_state(self) -> None:
        if self._state_listener is not None:
            self._state_listener()

    def set_impression_hook(self, hook: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
        """注入观后感生成回调（插件侧经宿主 LLM 文本任务实现；失败须如实降级）。"""
        self._impression_hook = hook

    def set_candidate_provider(self, provider: Optional[Callable[[], Dict[str, Any]]]) -> None:
        """注入自动候选提供器（同步面；异步准备由 integrations 协调器先行完成）。

        提供器返回 ``{"candidate": {...}|None, "reason": str}``；候选须含真实
        bvid（来自公开接口），无合适结果时如实返回 None，绝不编造 BV 号。
        """
        self._candidate_provider = provider

    def excluded_bvids(self) -> set:
        """候选去重集合（只读）：已看历史 ∪ 文字缓存 ∪ 活跃（非终态）任务。"""
        excluded = {b for b in self._store.watched_bvids() if b}
        excluded.update(self._store.cached_bvids())
        for task in self._store.load_tasks():
            if not self._store.is_terminal(str(task.get("status"))):
                bvid = str(task.get("bvid") or "")
                if bvid:
                    excluded.add(bvid)
        return excluded

    # ------------------------------------------------------------------
    # 画面采样：spec注入与运行任务能力标记
    # ------------------------------------------------------------------
    def _visual_spec(self) -> Optional[Dict[str, Any]]:
        """本次装配的画面采样参数（None = 纯文本来源流水线，行为与独立原型一致）。"""
        provider = self._visual_provider
        if provider is None:
            return None
        try:
            spec = provider()
        except Exception:
            return None
        return spec if isinstance(spec, dict) and spec else None

    def _qzone_assets_spec(self) -> Optional[Dict[str, Any]]:
        """PR-A1：本次装配的空间截图提取参数（None = 不提取；worker 不读插件配置，
        资产目录绝对路径由装配根经此注入 spec）。提供器异常保守视为不提取。"""
        provider = self._qzone_assets_provider
        if provider is None:
            return None
        try:
            spec = provider()
        except Exception:
            return None
        return spec if isinstance(spec, dict) and spec else None

    def _requested_mode_token(self) -> str:
        """运行任务能力标记：请求画面采样 → frames-<能力版本>，否则文本来源。"""
        spec = self._visual_spec()
        if not spec:
            return WatchStore.DEFAULT_CACHE_MODE
        capability = str(spec.get("capability") or "v1").strip() or "v1"
        return f"frames-{capability}"

    @staticmethod
    def _result_mode_token(result: Dict[str, Any]) -> str:
        """历史结果能力标记；不影响按BV查找文字观看记忆。"""
        watched = result.get("watched_range") if isinstance(result.get("watched_range"), dict) else {}
        if str((watched or {}).get("mode") or "") != "sampled_frames":
            return WatchStore.DEFAULT_CACHE_MODE
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
        if (not str(visual.get("summary") or "").strip()
                or visual.get("understanding_status") not in (None, "completed")):
            return WatchStore.DEFAULT_CACHE_MODE
        capability = str((visual or {}).get("capability") or "v1").strip() or "v1"
        return f"frames-{capability}"

    # ------------------------------------------------------------------
    # 对外操作（submit/status/result/cancel/候选）
    # ------------------------------------------------------------------
    def submit(self, video: str, purpose: str = "", source: str = "tool",
               episode_id: str = "", owner: str = "", rewatch: bool = False) -> Dict[str, Any]:
        from .bv import normalize_video_ref

        if self._closed:
            return {"success": False, "error_class": "unavailable", "error": "观看服务已停止"}
        bvid = normalize_video_ref(video or "")
        if not bvid:
            return {"success": False, "error_class": "invalid_input",
                    "error": "无法识别的视频标识：仅支持 BV 号或 bilibili.com 视频页链接（含 b23.tv 短链）"}
        # 文字记忆按BV命中；只有显式重看才绕过，仍遵守运行任务去重。
        requested_mode = self._requested_mode_token()
        cached = None if rewatch else self._store.load_cache(bvid)
        if isinstance(cached, dict):
            return {"success": True, "cached": True, "status": _COMPLETED,
                    "result": {"status": _COMPLETED, "cached_text": cached["text"],
                               "video": {"bvid": bvid}}}
        owner = str(owner or "").strip()
        # 进行中去重：同 BV 只关联一个任务（2026-11-04 模块评估问题6：去重键
        # 仅按 bvid，不含 cache_mode——配置热更改变采样能力时不产生同 BV 并行
        # 任务；能力差异只记录在任务台账，不影响去重）；S0 所有权——本次调用者
        # 登记为该任务的引用持有者之一（cancel 非 force 只撤销自己的引用，
        # 不误杀其他调用者）
        for task in self._store.load_tasks():
            if (task.get("bvid") == bvid
                    and not self._store.is_terminal(str(task.get("status")))):
                if owner:
                    owners = [str(o) for o in (task.get("owners") or [])]
                    if owner not in owners:
                        owners.append(owner)
                        task["owners"] = owners
                        self._store.upsert_task(task)
                return {"success": True, "cached": False, "deduped": True,
                        "task_id": task["task_id"], "status": task.get("status")}
        tasks = self._store.load_tasks()
        # 2026-11-10 队列语义：运行中的那个视频不占排队名额（一次仍只跑一个）；
        # 已有任务时工具调用按容量进入排队，满员才如实 queue_full。
        queued = [t for t in tasks if str(t.get("status")) == _QUEUED]
        if len(queued) >= self._queue_capacity:
            return {"success": False, "error_class": "queue_full",
                    "error": f"观看队列已满（{self._queue_capacity}），请稍后再提交"}
        requested_source = str(source or "tool").strip().lower() or "tool"
        source_kind = _source_kind(requested_source)
        # 空隙冲浪（gap-*）没有日程行，来源语义是「小事件」而非「日程」；
        # 兼容端口仍传 source="schedule"，在此按 episode 前缀细分（不改端口契约）。
        if source_kind == "autonomous_schedule" and str(episode_id or "").startswith("gap-"):
            source_kind = "autonomous_event"
        now = time.time()
        task = {
            "task_id": f"bw-{uuid.uuid4().hex[:12]}",
            "bvid": bvid,
            "purpose": str(purpose or "")[:200],
            # 兼容字段：既有端口契约只有 schedule/tool；语义枚举见 source_kind。
            "source": ("schedule" if requested_source in ("schedule", "autonomous_schedule")
                       else "tool"),
            "requested_source": requested_source,
            "source_kind": source_kind,
            "episode_id": str(episode_id or ""),
            "owners": [owner] if owner else [],
            "cache_mode": requested_mode,
            "visual_spec": self._visual_spec(),
            "qzone_assets_spec": self._qzone_assets_spec(),
            "status": _QUEUED,
            # 发起/受理时刻：观看入库的 watched_at 取此值（不是处理完成时间）。
            "created_at": now,
            "requested_at": now,
            "deadline_seconds": self._deadline_seconds,
            "deadline_epoch": now + self._deadline_seconds,
            "error_class": None,
            "error": None,
        }
        tasks.append(task)
        self._store.save_tasks(tasks)
        self._kick()
        return {"success": True, "cached": False, "task_id": task["task_id"],
                "status": _QUEUED, "stage": "queued"}

    def status(self, task_id: str) -> Dict[str, Any]:
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        receipt = {"success": True, "task_id": task_id, "status": task.get("status"),
                   "bvid": task.get("bvid"),
                   # 结算口径：任务真正开始处理/发起/结束的时刻（取消/中断时
                   # 上层按 started_at 结算实际观看，排队时间不算观看）。
                   "created_at": task.get("created_at"),
                   "started_at": task.get("started_at"),
                   "finished_at": task.get("finished_at")}
        if str(task.get("status")) == _RUNNING:
            progress = read_json(self._job_dir(task_id) / "progress.json", None)
            if isinstance(progress, dict):
                receipt["stage"] = progress.get("stage")
                receipt["detail"] = progress.get("detail")
        if self._store.is_terminal(str(task.get("status"))):
            receipt["error_class"] = task.get("error_class")
        return receipt

    def result(self, task_id: str) -> Dict[str, Any]:
        # A task result is authoritative; a same-video cache is not a task identity.
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        status = str(task.get("status"))
        if not self._store.is_terminal(status):
            return {"success": False, "error_class": "not_ready", "status": status,
                    "created_at": task.get("created_at"),
                    "started_at": task.get("started_at"),
                    "error": "任务尚未结束"}
        data = read_json(self._job_dir(task_id) / "result.json", None)
        if (isinstance(data, dict) and data.get("task_id") in (None, task_id)
                and data.get("status") == status):
            visual = data.get("visual")
            if isinstance(visual, dict):
                visual.pop("frames", None)
            # 3.17.33 复核修复：结果回执携带发起/受理时刻，下游经历索引与
            # watch history 共用「任务发起时间」口径（不再用完成时刻）。
            data.setdefault("created_at", task.get("created_at"))
            data.setdefault("requested_at", task.get("requested_at") or task.get("created_at"))
            data.setdefault("started_at", task.get("started_at"))
            return {"success": True, "status": status, "result": data}
        if status == _COMPLETED:
            return {"success": False, "status": status, "error_class": "result_missing",
                    "error": "本任务结果不可得，未使用同BV其他任务的结果代替"}
        return {"success": True, "status": status, "result": {
            "task_id": task_id, "status": status, "error_class": task.get("error_class"),
            "error": task.get("error"), "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "requested_at": task.get("requested_at") or task.get("created_at")}}

    def cancel(self, task_id: str, owner: str = "", force: bool = False) -> Dict[str, Any]:
        """取消/释放观看任务（S0 所有权 + 2026-11-10 不打断语义）。

        - ``force=True``（用户经工具显式取消）：无条件终止；
        - ``force=False + owner=schedule:/event:``（日程/租约/消息钩子的自动
          释放）：**只撤销该 owner 引用，绝不取消任务**——日程结束、owner 释放、
          busy 变化都不再打断已受理的观看；任务照常跑完并入库；
        - ``force=False + tool/debug 显式所有者``：按显式取消处理（工具语义）；
        - ``force=False + 无 owner``：历史 API 兜底，直接取消。
        """
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        status = str(task.get("status"))
        if self._store.is_terminal(status):
            # 已结束任务返回原终态，不重跑
            return {"success": True, "task_id": task_id, "status": status,
                    "already_terminal": True}
        owner = str(owner or "").strip()
        if not force and owner:
            owners = [str(o) for o in (task.get("owners") or [])]
            matched = owner in owners
            if owners and not matched:
                return {"success": False, "task_id": task_id, "status": status,
                        "error_class": "owner_mismatch", "error": "调用者未持有本任务引用"}
            if matched:
                owners.remove(owner)
                task["owners"] = owners
                self._store.upsert_task(task)
            # 日程/事件自动引用：只释放、绝不取消（日程结束/租约失效/忙闲）。
            if owner.startswith(("schedule:", "event:", "autonomous_schedule:",
                                 "autonomous_event:")):
                return {"success": True, "task_id": task_id, "status": status,
                        "released": True, "owner_matched": matched,
                        "content": (f"任务 {task_id} 已释放引用 {owner}；观看任务不因"
                                    "日程/忙闲/工具调用中断，继续处理至完成")}
            # tool/debug 等显式调用者：无 force 也按显式取消处理（历史 API 语义）。
            force = True
        if not force:
            # 显式 cancel（无 owner）：保留历史 API 的直接取消语义。
            pass
        else:
            task["owners"] = []
            self._store.upsert_task(task)
        self._cancel_requested.add(task_id)
        if (self._current_task_id == task_id and self._postprocess_task is not None
                and not self._postprocess_task.done()):
            self._postprocess_task.cancel()
        if status == _QUEUED:
            self._set_terminal(task, _CANCELLED, "cancelled", "提交后、开始处理前被取消")
            return {"success": True, "task_id": task_id, "status": _CANCELLED}
        # running：由监视循环在下一个轮询点终止子进程并落终态
        return {"success": True, "task_id": task_id, "status": status,
                "cancelling": True}

    def release_owner(self, task_id: str, owner: str) -> Dict[str, Any]:
        """只移除引用、绝不取消任务（消息钩子/协调器释放专用）。

        与 ``cancel(force=False, owner=...)`` 同语义的显式命名入口；owner 不
        存在时幂等成功，便于日程/租约重复释放。
        """
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        status = str(task.get("status"))
        if self._store.is_terminal(status):
            return {"success": True, "task_id": task_id, "status": status,
                    "already_terminal": True, "released": False}
        owner = str(owner or "").strip()
        if not owner:
            return {"success": False, "error_class": "invalid_input",
                    "error": "释放引用必须指定 owner"}
        owners = [str(o) for o in (task.get("owners") or [])]
        matched = owner in owners
        if matched:
            owners.remove(owner)
            task["owners"] = owners
            self._store.upsert_task(task)
        return {"success": True, "task_id": task_id, "status": status,
                "released": matched, "owner_matched": matched}

    async def shutdown(self) -> None:
        """停机（S0 生命周期修复）：取消任务泵、终止在跑 worker。

        队列/进行中任务保留在台账（不伪造终态），下次装配 ``_recover()`` 如实
        标记 ``interrupted_by_restart``；停机后不再有任何本任务的泵轮询或父侧
        模型调用（观后感/画面摘要钩子随泵取消）。
        """
        # 顺序敏感：先取 worker 引用再取消泵——泵被取消时其清理路径会清空
        # self._proc，若后取会漏掉 terminate（孤儿进程，S0 测试实证）
        self._closed = True
        proc = self._proc
        pump = self._pump_task
        self._pump_task = None
        if pump is not None and not pump.done():
            pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            except Exception:
                pass  # 泵自身异常即已终止，停机语义不变
        self._proc = None
        self._current_task_id = None
        self._cancel_requested.clear()
        self._postprocess_task = None
        if proc is not None and proc.returncode is None:
            try:
                await self._terminate(proc)
            except Exception:
                pass

    def next_candidate(self) -> Dict[str, Any]:
        """自动候选（兴趣驱动；2026-11-04 刷视频功能批）。

        提供器未注入 → 如实 ``no_candidate_source``（旧口径）；注入后由其
        同步 pick 面返回真实候选（异步兴趣检查/抓取由协调器先 prepare）。
        提供器异常/非法候选一律降级 no_candidate，不编造 BV 号。
        """
        provider = self._candidate_provider
        if provider is None:
            return {"success": True, "candidate": None, "reason": "no_candidate_source"}
        try:
            receipt = provider()
        except Exception as exc:
            if self._log:
                self._log.warning("自动候选提供器异常（降级 no_candidate）：%s", exc)
            return {"success": True, "candidate": None, "reason": "candidate_error"}
        if not isinstance(receipt, dict):
            return {"success": True, "candidate": None, "reason": "candidate_error"}
        candidate = receipt.get("candidate")
        if isinstance(candidate, dict):
            from .bv import normalize_video_ref
            bvid = normalize_video_ref(str(candidate.get("bvid") or ""))
            if not bvid:
                if self._log:
                    self._log.warning("自动候选缺少合法BV（如实丢弃，不编造）")
                return {"success": True, "candidate": None, "reason": "candidate_invalid"}
            candidate = dict(candidate)
            candidate["bvid"] = bvid
            return {"success": True, "candidate": candidate,
                    "reason": str(receipt.get("reason") or "")}
        return {"success": True, "candidate": None,
                "reason": str(receipt.get("reason") or "no_candidate")}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def recover(self) -> List[str]:
        """重启恢复：非终态任务如实置为 failed/interrupted_by_restart。"""
        touched: List[str] = []
        tasks = self._store.load_tasks()
        for task in tasks:
            if not self._store.is_terminal(str(task.get("status"))):
                task["status"] = _FAILED
                task["error_class"] = "interrupted_by_restart"
                task["error"] = "宿主重启导致任务中断；结果不可复用，需重新提交"
                atomic_write_json(self._job_dir(str(task["task_id"])) / "result.json", {
                    "task_id": task["task_id"], "status": _FAILED,
                    "error_class": "interrupted_by_restart", "error": task["error"]})
                touched.append(str(task.get("task_id")))
        if touched:
            self._store.save_tasks(tasks)
        return touched

    def _recover(self) -> None:
        try:
            self.recover()
        except Exception as exc:  # pragma: no cover - 装配防御
            if self._log:
                self._log.warning(f"观看任务恢复检查失败（忽略）: {exc}")

    # ------------------------------------------------------------------
    # 泵：串行消费队列
    # ------------------------------------------------------------------
    def _kick(self) -> None:
        if self._closed:
            return
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.get_running_loop().create_task(self._pump())

    async def _pump(self) -> None:
        while True:
            task = self._next_queued()
            if task is None:
                return
            if str(task.get("task_id")) in self._cancel_requested:
                self._set_terminal(task, _CANCELLED, "cancelled", "开始处理前被取消")
                continue
            await self._run_task(task)

    def _next_queued(self) -> Optional[Dict[str, Any]]:
        tasks = self._store.load_tasks()
        for task in tasks:
            if task.get("status") == _QUEUED:
                return task
        return None

    def _job_dir(self, task_id: str) -> Path:
        return self._dir / "jobs" / str(task_id)

    def _watch_cmd(self) -> List[str]:
        if self._worker_cmd is not None:
            return list(self._worker_cmd)
        worker_entry = Path(__file__).with_name("worker.py")
        return [sys.executable, "-B", str(worker_entry)]

    async def _run_task(self, task: Dict[str, Any]) -> None:
        task_id = str(task["task_id"])
        # 2026-11-10 不打断：busy/日程状态只在 submit 发起时把关；任务一经受理，
        # 运行期不再用 task_guard 取消（否则排队期间忙闲变化会杀掉已受理任务）。
        if self._closed:
            self._set_terminal(task, _CANCELLED, "cancelled", "观看服务停止")
            return
        job_dir = self._job_dir(task_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        started_at = time.time()
        spec = {"task_id": task_id, "bvid": task.get("bvid"),
                "purpose": task.get("purpose", ""),
                "max_video_duration_seconds": self._max_duration,
                "created_at": float(task.get("created_at") or started_at),
                "requested_at": float(task.get("requested_at") or task.get("created_at") or started_at),
                "started_at": started_at,
                "deadline_epoch": float(task.get("deadline_epoch") or
                                        time.time() + float(task.get("deadline_seconds") or self._deadline_seconds))}
        if time.time() >= spec["deadline_epoch"]:
            self._set_terminal(task, _FAILED, "timeout", "任务在排队期间已到总截止")
            return
        visual_spec = task.get("visual_spec", self._visual_spec())
        if visual_spec:
            # B2：worker 侧有界抽帧参数（不可得/失败由 worker 如实降级，不影响文本来源）
            spec["visual"] = visual_spec
        qzone_assets_spec = task.get("qzone_assets_spec", self._qzone_assets_spec())
        if qzone_assets_spec:
            # PR-A1：仅 auto+空间启用时装配根才给参数；worker 据此提取高清截图资产
            spec["qzone_assets"] = qzone_assets_spec
        atomic_write_json(job_dir / "spec.json", spec)
        task["status"] = _RUNNING
        task["started_at"] = started_at
        self._store.upsert_task(task)
        self._notify_state()
        cmd = [*self._watch_cmd(), str(job_dir / "spec.json"), str(job_dir / "result.json")]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(self._dir), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                               if os.name == "nt" else 0),
                start_new_session=os.name != "nt")
        except Exception as exc:
            self._set_terminal(task, _FAILED, "worker_error", f"worker 启动失败: {exc}")
            return
        self._proc = proc
        self._current_task_id = task_id
        deadline = float(spec["deadline_epoch"])
        try:
            while True:
                if task_id in self._cancel_requested:
                    await self._terminate(proc)
                    self._set_terminal(task, _CANCELLED, "cancelled", "处理中被取消")
                    return
                if time.time() >= deadline:
                    await self._terminate(proc)
                    self._set_terminal(task, _FAILED, "timeout", "任务超过总时限被终止；已观看范围不冒充完整")
                    return
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.5)
            result = read_json(job_dir / "result.json", None)
            if isinstance(result, dict) and result.get("status") in (_COMPLETED, _FAILED):
                # worker 自报终态；cancelled 请求仍以父侧口径为准
                if task_id in self._cancel_requested:
                    self._set_terminal(task, _CANCELLED, "cancelled", "处理中被取消")
                    return
                await self._finish(task, result)
            else:
                tail = b""
                try:
                    if proc.stdout is not None:
                        tail = (await proc.stdout.read())[-800:]
                except Exception:
                    pass
                self._set_terminal(task, _FAILED, "worker_error",
                                   f"worker 未写出有效结果（exit={proc.returncode}）{tail.decode('utf-8', 'replace')[:400]}")
        finally:
            self._proc = None
            self._current_task_id = None
            self._cancel_requested.discard(task_id)

    @staticmethod
    async def _terminate(proc: Any) -> None:
        # The PID is exclusively the worker created above, never a host/supervisor PID.
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0 and proc.returncode is None:
            try:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec(
                        "taskkill", "/PID", str(pid), "/T", "/F",
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW)
                    await asyncio.wait_for(killer.wait(), 5)
                else:
                    os.killpg(pid, signal.SIGKILL)
            except (OSError, asyncio.TimeoutError):
                pass
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (Exception, asyncio.TimeoutError):
            pass

    async def _finish(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        task_id = str(task["task_id"])
        result["task_id"] = task_id
        self._current_task_id = task_id
        status = str(result.get("status"))
        if status != _COMPLETED:
            self._set_terminal(task, _FAILED,
                               str(result.get("error_class") or "worker_error"),
                               str(result.get("error") or "worker 上报失败"))
            return
        # 2026-11-10 刷视频批：worker 处理完立即视为看完并入库——先写缓存与历史，
        # 再跑观后感/画面摘要等父侧模型加工；后处理只回填字段，不改 watched_at。
        finished_at = float(result.get("finished_at") or time.time())
        self._enforce_result_boundary(result)
        task["result_cache_mode"] = self._result_mode_token(result)
        self._persist_completed(task, result,
                                self._completion_entry(task, result, finished_at),
                                append=True, final=False)
        # 观后感/画面摘要（可选）：经宿主 LLM 生成；S0 起纳入任务总截止——
        # 预算=剩余截止（上限 180s），耗尽/超时都如实标注并保留 worker 原始结果
        result = await self._run_completion_hooks(task, result)
        if task_id in self._cancel_requested:
            # 显式取消发生在即时入库之后：回滚刚写入的观看记忆并落 cancelled。
            self._store.remove_history(task_id)
            self._set_terminal(task, _CANCELLED, "cancelled",
                               "后处理阶段被显式取消")
            return
        # Enforce the result boundary even when a hook failed or exhausted its budget.
        self._enforce_result_boundary(result)
        # 2026-11-04 刷视频功能批（前批遗留修复）：impression_error 只作内部
        # 诊断——result_text 会把 impression_error 兜底成对外「观后感」，导致
        # processing 错误文案外泄；改名 impression_diag（仍留 result.json/
        # history 供追溯），对外文本诚实回落「暂无观后感」，不带错误细节。
        impression_diag = result.pop("impression_error", None)
        if impression_diag:
            result["impression_diag"] = str(impression_diag)[:400]
        # 回填后处理字段（按 task_id 更新同一条历史；watched_at/first_watched_at 不动）。
        self._persist_completed(task, result,
                                self._completion_entry(task, result, finished_at),
                                append=False)
        self._set_terminal(task, _COMPLETED, None, None, result=result,
                           finished_at=finished_at)

    async def _run_completion_hooks(self, task: Dict[str, Any],
                                    result: Dict[str, Any]) -> Dict[str, Any]:
        if self._impression_hook is None:
            return result
        task_id = str(task.get("task_id") or "")
        remaining = float(task.get("deadline_epoch") or 0) - time.time()
        budget = min(180.0, remaining) if remaining > 0 else 0.0
        if budget <= 0:
            result["impression"] = None
            result["impression_error"] = ("父侧加工窗口已被任务总截止耗尽，"
                                          "保留 worker 原始结果（如实标注）")
            return result
        try:
            self._postprocess_task = asyncio.create_task(
                self._impression_hook(dict(result)))
            patched = await asyncio.wait_for(self._postprocess_task, timeout=budget)
            if isinstance(patched, dict):
                return patched
            return result
        except asyncio.TimeoutError:
            result["impression"] = None
            result["impression_error"] = ("父侧加工（观后感/画面摘要）超过任务总截止，"
                                          "如实截断（不做晚到补跑）")
            return result
        except asyncio.CancelledError:
            if task_id not in self._cancel_requested:
                raise
            # 显式取消：跳过剩余后处理，由 _finish 回滚即时写入的历史条目。
            return result
        except Exception as exc:
            result["impression"] = None
            result["impression_error"] = f"观后感生成失败（如实降级）：{exc}"
            return result
        finally:
            self._postprocess_task = None

    @staticmethod
    def _enforce_result_boundary(result: Dict[str, Any]) -> None:
        visual = result.get("visual")
        if isinstance(visual, dict):
            visual.pop("frames", None)
            if not str(visual.get("summary") or "").strip():
                visual["understanding_status"] = "not_completed"
                result["completeness"] = "partial"
            else:
                result["completeness"] = "visual"

    def _completion_entry(self, task: Dict[str, Any], result: Dict[str, Any],
                          finished_at: float) -> Dict[str, Any]:
        entry = dict(result)
        # 观看时间口径：任务入队/受理时刻（created_at），不是父侧处理完成时间。
        watched_at = float(task.get("created_at") or task.get("requested_at")
                           or task.get("started_at") or finished_at)
        entry["bvid"] = task.get("bvid")
        entry["watched_at"] = watched_at
        entry["requested_at"] = watched_at
        entry["finished_at"] = finished_at
        entry["task_id"] = task.get("task_id")
        entry["episode_id"] = task.get("episode_id", "")
        entry["purpose"] = task.get("purpose", "")
        entry["source"] = _history_source(task)
        entry["source_kind"] = str(task.get("source_kind")
                                   or _source_kind(entry["source"]))
        entry["requested_source"] = str(task.get("requested_source")
                                        or entry["source"])
        # V2 主动分享扩展位（本期不启用、不默认填写）
        entry.setdefault("recommended_reason", "")
        entry.setdefault("shared_to", "")
        entry.setdefault("shared_at", "")
        entry.setdefault("share_receipt", "")
        return entry

    def _persist_completed(self, task: Dict[str, Any], result: Dict[str, Any],
                           entry: Dict[str, Any], *, append: bool,
                           final: bool = True) -> None:
        """落文字缓存 + 历史条目（首次 append；后处理按 task_id 回填）。

        ``final=False`` 用于 worker 完成后的即时入库：历史/缓存先写，任务状态
        仍保持 running 直到父侧后处理结束（显式取消可在此窗口回滚历史）。
        """
        if final:
            task["status"] = _COMPLETED
            task["finished_at"] = float(entry.get("finished_at") or time.time())
        task["result_cache_mode"] = self._result_mode_token(result)
        self._store.upsert_task(task)
        try:
            self._store.save_cache(str(task.get("bvid")), result)
        except OSError as exc:
            # 缓存失败不能抹掉真实任务终态或触发重复观看。
            if self._log:
                self._log.warning("视频文字缓存写入失败：%s", exc)
        if append:
            self._store.append_history(entry)
        else:
            self._store.update_history(str(task.get("task_id") or ""), entry)

    def _set_terminal(self, task: Dict[str, Any], status: str, error_class: Optional[str],
                      error: Optional[str], result: Optional[Dict[str, Any]] = None,
                      finished_at: Optional[float] = None) -> None:
        task["status"] = status
        task["finished_at"] = float(finished_at) if finished_at is not None else time.time()
        task["error_class"] = error_class
        task["error"] = error
        self._store.upsert_task(task)
        job_dir = self._job_dir(str(task.get("task_id")))
        payload = result if result is not None else {
            "task_id": task["task_id"], "status": status,
            "error_class": error_class, "error": error}
        atomic_write_json(job_dir / "result.json", payload)
        self._cleanup_media(job_dir)
        self._notify_state()
        # 媒体/临时目录清理（V1 仅文本流水线，无媒体；目录保留 result/spec 供追溯）
        if self._log:
            self._log.info(f"观看任务终态 {task.get('task_id')} -> {status}"
                           + (f" ({error_class}: {error})" if error_class else ""))

    def _cleanup_media(self, job_dir: Path) -> None:
        """Remove only this task's media, never its receipts or arbitrary directories."""
        if job_dir.is_symlink() or job_dir.resolve().parent != (self._dir / "jobs").resolve():
            return
        paths = [job_dir / "media.mp4"]
        frames = job_dir / "frames"
        if not frames.is_symlink() and frames.resolve().parent == job_dir.resolve():
            paths.extend(frames.glob("*.jpg"))
        for path in paths:
            if path.is_symlink():continue
            if path.resolve().parent not in (job_dir.resolve(), frames.resolve()):continue
            try:path.unlink(missing_ok=True)
            except OSError:
                if self._log:self._log.debug("任务临时媒体暂未清除，保留结果回执")
