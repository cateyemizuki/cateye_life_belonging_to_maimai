"""任务 / 观看历史的持久化（任务终态持久化 + 成功缓存 + 观看记忆）。

存储布局（全部位于插件自己的 data_dir 内，宿主隔离）::

    tasks.json              任务台账（含进行中任务；重启恢复的依据）
    history.json            观看记忆（仅真实完成的任务；分享扩展字段预留）
    cache/{bvid}.json       仅BV和文字结果；默认保存后15天过期，读取不续期
    jobs/{task_id}/         单任务的 spec/progress/result 与临时媒体

写入一律「临时文件 + os.replace」原子落盘；读取失败按损坏处理（返回默认值，
不抛出拖挂装配）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class WatchStore:
    """tasks/history/cache 三个 JSON 面的读写门面（进程内互斥）。"""

    def __init__(self, data_dir: Path, cache_retention_days: int = 15) -> None:
        self._dir = Path(data_dir)
        self._tasks_path = self._dir / "tasks.json"
        self._history_path = self._dir / "history.json"
        self._cache_dir = self._dir / "cache"
        self._lock = threading.RLock()
        self._cache_retention_seconds = max(1, int(cache_retention_days)) * 86400
        try:
            self.prune_cache()
        except OSError:
            # 不可读的缓存不能阻止任务服务加载；命中按不可得处理。
            pass

    # ---- 任务台账 ----
    def load_tasks(self) -> List[Dict[str, Any]]:
        with self._lock:
            payload = read_json(self._tasks_path, {"tasks": []})
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        return [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []

    def save_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        with self._lock:
            atomic_write_json(self._tasks_path, {"tasks": tasks})

    def upsert_task(self, task: Dict[str, Any]) -> None:
        tasks = self.load_tasks()
        for index, existing in enumerate(tasks):
            if existing.get("task_id") == task.get("task_id"):
                tasks[index] = task
                break
        else:
            tasks.append(task)
        self.save_tasks(tasks)

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        for task in self.load_tasks():
            if task.get("task_id") == task_id:
                return task
        return None

    @staticmethod
    def is_terminal(status: str) -> bool:
        return status in _TERMINAL_STATES

    # ---- 成功结果缓存 ----
    #: 保留旧调用签名；各模式的文字记忆均按BV复用，显式重看才绕过。
    DEFAULT_CACHE_MODE = "text_sources"

    def cache_path(self, bvid: str, mode: str = DEFAULT_CACHE_MODE) -> Path:
        if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid):
            raise ValueError("无效的缓存BV号")
        # mode仅保留调用签名；观看记忆按BV命中，实际旧采样范围在文字中说明。
        return self._cache_dir / f"{bvid}.json"

    def load_cache(self, bvid: str, mode: str = DEFAULT_CACHE_MODE) -> Optional[Dict[str, Any]]:
        with self._lock:
            path = self.cache_path(bvid, mode)
            if self._cache_dir.is_symlink() or path.is_symlink():
                return None
            try:
                age = time.time() - path.stat().st_mtime
                if age < -300 or age >= self._cache_retention_seconds:
                    path.unlink(missing_ok=True)
                    return None
            except OSError:
                return None
            payload = read_json(path, None)
            return payload if (isinstance(payload, dict) and set(payload) == {"bvid", "text"}
                               and payload["bvid"] == bvid and isinstance(payload["text"], str)
                               and payload["text"].strip()) else None

    def save_cache(self, bvid: str, result: Any,
                   mode: str = DEFAULT_CACHE_MODE) -> None:
        from .result_text import result_text
        if isinstance(result, dict) and result.get("status", "completed") != "completed":
            return
        text = result_text(result) if isinstance(result, dict) else str(result or "")
        if not text.strip():
            return
        with self._lock:
            if self._cache_dir.is_symlink():
                return
            self._write_text_cache(bvid, text, time.time())

    def _write_text_cache(self, bvid: str, text: str, saved_at: float) -> None:
        path = self.cache_path(bvid)
        if path.is_symlink():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        if temp.is_symlink():
            raise OSError("拒绝写入符号链接缓存临时文件")
        temp.write_text(json.dumps({"bvid": bvid, "text": text}, ensure_ascii=False), encoding="utf-8")
        # 先设置临时文件时间再原子替换，迁移不刷新保留期，读取也不续期。
        os.utime(temp, (saved_at, saved_at))
        os.replace(temp, path)

    def prune_cache(self) -> int:
        """清理过期项，并把旧结构缓存迁成仅BV/文字；不接触jobs/history。"""
        from .result_text import result_text
        removed = 0
        with self._lock:
            if self._cache_dir.is_symlink() or not self._cache_dir.exists():
                return 0
            groups: Dict[str, list] = {}
            now = time.time()
            for path in self._cache_dir.glob("*.json"):
                match = re.fullmatch(r"(BV[A-Za-z0-9]{10})(?:__[A-Za-z0-9_-]+)?\.json", path.name)
                if not match or path.is_symlink():
                    continue
                try:
                    stamp = path.stat().st_mtime
                    if now - stamp >= self._cache_retention_seconds or stamp > now + 300:
                        path.unlink(missing_ok=True);removed += 1;continue
                    value = read_json(path, None)
                    if not isinstance(value, dict):
                        continue
                    if set(value) == {"bvid", "text"} and value.get("bvid") == match[1] and isinstance(value.get("text"), str):
                        text = value["text"]
                    elif value.get("status") == "completed":
                        text = result_text(value)
                    else:
                        continue
                    if text.strip():groups.setdefault(match[1], []).append((stamp, path, text, value))
                except OSError:
                    continue
            for bvid, rows in groups.items():
                stamp, source, text, value = max(rows, key=lambda r:r[0])
                target = self.cache_path(bvid)
                if target.is_symlink():
                    continue
                try:
                    if source != target or set(value) != {"bvid", "text"}:
                        self._write_text_cache(bvid, text, stamp)
                    for _, path, _, _ in rows:
                        if path != target:
                            path.unlink(missing_ok=True);removed += 1
                except OSError:
                    continue
        return removed

    # ---- 观看记忆 ----
    def load_history(self) -> List[Dict[str, Any]]:
        payload = read_json(self._history_path, {"watched": []})
        watched = payload.get("watched") if isinstance(payload, dict) else None
        return [w for w in watched if isinstance(w, dict)] if isinstance(watched, list) else []

    def append_history(self, entry: Dict[str, Any]) -> None:
        watched = self.load_history()
        watched.append(entry)
        # 有界：最多保留 500 条真实观看记录，淘汰最旧（去重与 V2 分享用）
        if len(watched) > 500:
            watched = watched[-500:]
        with self._lock:
            atomic_write_json(self._history_path, {"watched": watched})

    def watched_bvids(self) -> List[str]:
        return [str(entry.get("bvid") or "") for entry in self.load_history()]
