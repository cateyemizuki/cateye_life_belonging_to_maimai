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


class WatchTaskManager:
    """任务台账 + worker 子进程编排。SDK 无关（worker 命令可注入以便测试）。"""

    def __init__(self, data_dir: Path, logger: Any = None,
                 queue_capacity: int = 3, task_deadline_seconds: int = 900,
                 max_video_duration_seconds: int = 1800,
                 worker_cmd: Optional[List[str]] = None,
                 visual_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
                 cache_retention_days: int = 15) -> None:
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
        active = [t for t in tasks if not self._store.is_terminal(str(t.get("status")))]
        if len(active) >= self._queue_capacity:
            return {"success": False, "error_class": "queue_full",
                    "error": f"观看队列已满（{self._queue_capacity}），请稍后再提交"}
        task = {
            "task_id": f"bw-{uuid.uuid4().hex[:12]}",
            "bvid": bvid,
            "purpose": str(purpose or "")[:200],
            "source": "schedule" if source == "schedule" else "tool",
            "episode_id": str(episode_id or ""),
            "owners": [owner] if owner else [],
            "cache_mode": requested_mode,
            "visual_spec": self._visual_spec(),
            "status": _QUEUED,
            "created_at": time.time(),
            "deadline_seconds": self._deadline_seconds,
            "deadline_epoch": time.time() + self._deadline_seconds,
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
                   "bvid": task.get("bvid")}
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
                    "error": "任务尚未结束"}
        data = read_json(self._job_dir(task_id) / "result.json", None)
        if (isinstance(data, dict) and data.get("task_id") in (None, task_id)
                and data.get("status") == status):
            visual = data.get("visual")
            if isinstance(visual, dict):
                visual.pop("frames", None)
            return {"success": True, "status": status, "result": data}
        if status == _COMPLETED:
            return {"success": False, "status": status, "error_class": "result_missing",
                    "error": "本任务结果不可得，未使用同BV其他任务的结果代替"}
        return {"success": True, "status": status, "result": {
            "task_id": task_id, "status": status, "error_class": task.get("error_class"),
            "error": task.get("error")}}

    def cancel(self, task_id: str, owner: str = "", force: bool = False) -> Dict[str, Any]:
        """取消/释放观看任务（S0 所有权语义，B 计划/冲浪计划 §4.2）。

        - ``force=True``（用户经工具显式取消）：无条件取消，清空全部引用；
        - ``force=False``（自动事件释放）：撤销自己的引用；仍有其他调用者持有
          时**只释放不取消**（回执 ``released=True``），避免日程收尾误杀用户的
          显式点播或另一个事件的任务；无引用信息时保持旧行为（直接取消）。
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
            if owners and owner not in owners:
                return {"success": False, "task_id": task_id, "status": status,
                        "error_class": "owner_mismatch", "error": "调用者未持有本任务引用"}
            if owner in owners and len(owners) > 1:
                owners.remove(owner)
                task["owners"] = owners
                self._store.upsert_task(task)
                return {"success": True, "task_id": task_id, "status": status,
                        "released": True,
                        "content": f"任务 {task_id} 仍有其他调用者（{len(owners)}），本次只释放引用不取消"}
        if not force:
            # 释放最后一个引用后清空（下次 dedupe 重新登记）
            if owner:
                owners = [str(o) for o in (task.get("owners") or []) if o != owner]
                task["owners"] = owners
                self._store.upsert_task(task)
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
        if self._closed or (self._task_guard is not None and not self._task_guard(task)):
            self._set_terminal(task, _CANCELLED, "attention_revoked", "自动观看条件已失效")
            return
        job_dir = self._job_dir(task_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        spec = {"task_id": task_id, "bvid": task.get("bvid"),
                "purpose": task.get("purpose", ""),
                "max_video_duration_seconds": self._max_duration,
                "deadline_epoch": float(task.get("deadline_epoch") or
                                        time.time() + float(task.get("deadline_seconds") or self._deadline_seconds))}
        if time.time() >= spec["deadline_epoch"]:
            self._set_terminal(task, _FAILED, "timeout", "任务在排队期间已到总截止")
            return
        visual_spec = task.get("visual_spec", self._visual_spec())
        if visual_spec:
            # B2：worker 侧有界抽帧参数（不可得/失败由 worker 如实降级，不影响文本来源）
            spec["visual"] = visual_spec
        atomic_write_json(job_dir / "spec.json", spec)
        task["status"] = _RUNNING
        task["started_at"] = time.time()
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
        result["task_id"] = str(task["task_id"])
        self._current_task_id = str(task["task_id"])
        status = str(result.get("status"))
        if status == _COMPLETED:
            # 观后感/画面摘要（可选）：经宿主 LLM 生成；S0 起纳入任务总截止——
            # 预算=剩余截止（上限 180s），耗尽/超时都如实标注并保留 worker 原始结果
            if self._impression_hook is not None:
                remaining = float(task.get("deadline_epoch") or 0) - time.time()
                budget = min(180.0, remaining) if remaining > 0 else 0.0
                if budget <= 0:
                    result["impression"] = None
                    result["impression_error"] = ("父侧加工窗口已被任务总截止耗尽，"
                                                  "保留 worker 原始结果（如实标注）")
                else:
                    try:
                        self._postprocess_task = asyncio.create_task(
                            self._impression_hook(dict(result)))
                        patched = await asyncio.wait_for(
                            self._postprocess_task, timeout=budget)
                        if isinstance(patched, dict):
                            result = patched
                    except asyncio.TimeoutError:
                        result["impression"] = None
                        result["impression_error"] = ("父侧加工（观后感/画面摘要）超过任务总截止，"
                                                      "如实截断（不做晚到补跑）")
                    except asyncio.CancelledError:
                        if str(task["task_id"]) not in self._cancel_requested:
                            raise
                        self._set_terminal(task, _CANCELLED, "cancelled", "父侧加工时取消")
                        return
                    except Exception as exc:
                        result["impression"] = None
                        result["impression_error"] = f"观后感生成失败（如实降级）：{exc}"
                    finally:
                        self._postprocess_task = None
            if str(task["task_id"]) in self._cancel_requested:
                self._set_terminal(task, _CANCELLED, "cancelled", "结果入库前取消")
                return
            # Enforce the result boundary even when a hook failed or exhausted its budget.
            visual = result.get("visual")
            if isinstance(visual, dict):
                visual.pop("frames", None)
                if not str(visual.get("summary") or "").strip():
                    visual["understanding_status"] = "not_completed"
                    result["completeness"] = "partial"
                else:
                    result["completeness"] = "visual"
            # 2026-11-04 刷视频功能批（前批遗留修复）：impression_error 只作内部
            # 诊断——result_text 会把 impression_error 兜底成对外「观后感」，导致
            # processing 错误文案外泄；改名 impression_diag（仍留 result.json/
            # history 供追溯），对外文本诚实回落「暂无观后感」，不带错误细节。
            impression_diag = result.pop("impression_error", None)
            if impression_diag:
                result["impression_diag"] = str(impression_diag)[:400]
            task["status"] = _COMPLETED
            task["result_cache_mode"] = self._result_mode_token(result)
            try:
                self._store.save_cache(str(task.get("bvid")), result)
            except OSError as exc:
                # 缓存失败不能抹掉真实任务终态或触发重复观看。
                if self._log:
                    self._log.warning("视频文字缓存写入失败：%s", exc)
            entry = dict(result)
            entry["bvid"] = task.get("bvid")
            entry["watched_at"] = time.time()
            entry["task_id"] = task.get("task_id")
            entry["episode_id"] = task.get("episode_id", "")
            entry["purpose"] = task.get("purpose", "")
            # V2 主动分享扩展位（本期不启用、不默认填写）
            entry.setdefault("recommended_reason", "")
            entry.setdefault("shared_to", "")
            entry.setdefault("shared_at", "")
            entry.setdefault("share_receipt", "")
            self._store.append_history(entry)
            self._set_terminal(task, _COMPLETED, None, None, result=result)
        else:
            self._set_terminal(task, _FAILED,
                               str(result.get("error_class") or "worker_error"),
                               str(result.get("error") or "worker 上报失败"))

    def _set_terminal(self, task: Dict[str, Any], status: str, error_class: Optional[str],
                      error: Optional[str], result: Optional[Dict[str, Any]] = None) -> None:
        task["status"] = status
        task["finished_at"] = time.time()
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
