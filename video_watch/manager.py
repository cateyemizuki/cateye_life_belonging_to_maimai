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

#: 已知失败分类（结果契约 §4.2：排队、失败、超时、取消不伪装成功）
ERROR_CLASSES = {
    "invalid_input", "unsupported_video", "network", "timeout",
    "cancelled", "queue_full", "worker_error", "interrupted_by_restart",
    "not_found",
}


class WatchTaskManager:
    """任务台账 + worker 子进程编排。SDK 无关（worker 命令可注入以便测试）。"""

    def __init__(self, data_dir: Path, logger: Any = None,
                 queue_capacity: int = 3, task_deadline_seconds: int = 900,
                 max_video_duration_seconds: int = 1800,
                 worker_cmd: Optional[List[str]] = None,
                 visual_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None) -> None:
        self._store = WatchStore(Path(data_dir))
        self._dir = Path(data_dir)
        self._log = logger
        self._queue_capacity = max(1, int(queue_capacity))
        # 下限仅作编程兜底；生产下限由配置模型 ge=60 把关（测试需要更短时限）
        self._deadline_seconds = max(1, int(task_deadline_seconds))
        self._max_duration = max(30, int(max_video_duration_seconds))
        # worker 命令可注入（隔离测试用假 worker）；默认 = 本包 worker.py 子进程
        self._worker_cmd = worker_cmd  # type: ignore[assignment]
        # 提交期缓存模式 token（B 计划 §5.1：文本缓存与画面采样缓存互不命中；
        # None/异常 = 纯文本来源口径，与独立原型行为一致）
        self._visual_provider = visual_provider
        self._proc: Optional[Any] = None
        self._current_task_id: Optional[str] = None
        self._cancel_requested: set = set()
        self._pump_task: Optional[asyncio.Task] = None
        self._impression_hook: Optional[Callable[[Dict[str, Any]], Any]] = None
        self._recover()

    def set_impression_hook(self, hook: Optional[Callable[[Dict[str, Any]], Any]]) -> None:
        """注入观后感生成回调（插件侧经宿主 LLM 文本任务实现；失败须如实降级）。"""
        self._impression_hook = hook

    # ------------------------------------------------------------------
    # 画面采样（B2）：spec 注入与缓存键分型
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
        """提交期缓存模式 token：请求画面采样 → frames-<能力版本>，否则文本来源。"""
        spec = self._visual_spec()
        if not spec:
            return WatchStore.DEFAULT_CACHE_MODE
        capability = str(spec.get("capability") or "v1").strip() or "v1"
        return f"frames-{capability}"

    @staticmethod
    def _result_mode_token(result: Dict[str, Any]) -> str:
        """结果侧缓存模式 token：watched_range.mode=sampled_frames → frames-<capability>。"""
        watched = result.get("watched_range") if isinstance(result.get("watched_range"), dict) else {}
        if str((watched or {}).get("mode") or "") != "sampled_frames":
            return WatchStore.DEFAULT_CACHE_MODE
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
        capability = str((visual or {}).get("capability") or "v1").strip() or "v1"
        return f"frames-{capability}"

    # ------------------------------------------------------------------
    # 对外操作（submit/status/result/cancel/候选）
    # ------------------------------------------------------------------
    def submit(self, video: str, purpose: str = "", source: str = "tool",
               episode_id: str = "") -> Dict[str, Any]:
        from .bv import normalize_video_ref

        bvid = normalize_video_ref(video or "")
        if not bvid:
            return {"success": False, "error_class": "invalid_input",
                    "error": "无法识别的视频标识：仅支持 BV 号或 bilibili.com 视频页链接（含 b23.tv 短链）"}
        # 缓存命中：按「本次请求的模式」查缓存（不新增观看记录——去重口径）；
        # 旧文本缓存不会命中画面采样请求，反之亦然
        cached = self._store.load_cache(bvid, self._requested_mode_token())
        if isinstance(cached, dict) and cached.get("status") == _COMPLETED:
            return {"success": True, "cached": True, "task_id": cached.get("task_id", ""),
                    "status": _COMPLETED, "result": cached}
        # 进行中去重：同 BV 只关联一个任务
        for task in self._store.load_tasks():
            if task.get("bvid") == bvid and not self._store.is_terminal(str(task.get("status"))):
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
            "status": _QUEUED,
            "created_at": time.time(),
            "deadline_seconds": self._deadline_seconds,
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
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        status = str(task.get("status"))
        if status == _COMPLETED:
            cached = self._store.load_cache(str(task.get("bvid")))
            if isinstance(cached, dict):
                return {"success": True, "status": status, "result": cached}
        if self._store.is_terminal(status):
            result = read_json(self._job_dir(task_id) / "result.json", None)
            if isinstance(result, dict):
                return {"success": True, "status": status, "result": result}
            return {"success": True, "status": status,
                    "result": {"task_id": task_id, "status": status,
                               "error_class": task.get("error_class"),
                               "error": task.get("error")}}
        return {"success": False, "error_class": "not_ready",
                "error": f"任务尚未结束（当前 {status}）",
                "task_id": task_id, "status": status}

    def cancel(self, task_id: str) -> Dict[str, Any]:
        task = self._store.get_task(task_id or "")
        if not task:
            return {"success": False, "error_class": "not_found", "error": "任务不存在"}
        status = str(task.get("status"))
        if self._store.is_terminal(status):
            # 已结束任务返回原终态，不重跑
            return {"success": True, "task_id": task_id, "status": status,
                    "already_terminal": True}
        self._cancel_requested.add(task_id)
        if status == _QUEUED:
            self._set_terminal(task, _CANCELLED, "cancelled", "提交后、开始处理前被取消")
            return {"success": True, "task_id": task_id, "status": _CANCELLED}
        # running：由监视循环在下一个轮询点终止子进程并落终态
        return {"success": True, "task_id": task_id, "status": status,
                "cancelling": True}

    def next_candidate(self) -> Dict[str, Any]:
        """从配置 watchlist 里挑一个还没真实看过的候选（日程选片用）。"""
        watchlist = self._watchlist_provider() if hasattr(self, "_watchlist_provider") else []
        watched = set(self._store.watched_bvids())
        from .bv import normalize_video_ref
        seen: set = set()
        for raw in watchlist:
            bvid = normalize_video_ref(str(raw or ""))
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            if bvid in watched:
                continue
            return {"success": True, "candidate": {"bvid": bvid, "raw": str(raw)},
                    "reason": "watchlist"}
        reason = "watchlist_empty" if not watchlist else "all_watched"
        return {"success": True, "candidate": None, "reason": reason}

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

    def attach_watchlist(self, provider: Callable[[], List[str]]) -> None:
        """注入 watchlist 读取回调（插件装配时来自配置热读）。"""
        self._watchlist_provider = provider  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # 泵：串行消费队列
    # ------------------------------------------------------------------
    def _kick(self) -> None:
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
        job_dir = self._job_dir(task_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        spec = {"task_id": task_id, "bvid": task.get("bvid"),
                "purpose": task.get("purpose", ""),
                "max_video_duration_seconds": self._max_duration,
                "deadline_epoch": time.time() + float(task.get("deadline_seconds") or self._deadline_seconds)}
        visual_spec = self._visual_spec()
        if visual_spec:
            # B2：worker 侧有界抽帧参数（不可得/失败由 worker 如实降级，不影响文本来源）
            spec["visual"] = visual_spec
        atomic_write_json(job_dir / "spec.json", spec)
        task["status"] = _RUNNING
        task["started_at"] = time.time()
        self._store.upsert_task(task)
        cmd = [*self._watch_cmd(), str(job_dir / "spec.json"), str(job_dir / "result.json")]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=str(self._dir), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
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
        try:
            proc.kill()
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass

    async def _finish(self, task: Dict[str, Any], result: Dict[str, Any]) -> None:
        status = str(result.get("status"))
        if status == _COMPLETED:
            # 观后感（可选）：经宿主 LLM 文本任务生成；失败如实置 None，不阻塞任务
            if self._impression_hook is not None:
                try:
                    patched = await self._impression_hook(dict(result))
                    if isinstance(patched, dict):
                        result = patched
                except Exception as exc:
                    result["impression"] = None
                    result["impression_error"] = f"观后感生成失败（如实降级）：{exc}"
            task["status"] = _COMPLETED
            self._store.save_cache(str(task.get("bvid")), result,
                                   self._result_mode_token(result))
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
        if result is not None:
            atomic_write_json(job_dir / "result.json", result)
        # 媒体/临时目录清理（V1 仅文本流水线，无媒体；目录保留 result/spec 供追溯）
        if self._log:
            self._log.info(f"观看任务终态 {task.get('task_id')} -> {status}"
                           + (f" ({error_class}: {error})" if error_class else ""))
