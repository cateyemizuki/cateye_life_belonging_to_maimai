"""任务 / 观看历史的持久化（任务终态持久化 + 成功缓存 + 观看记忆）。

存储布局（全部位于插件自己的 data_dir 内，宿主隔离）::

    tasks.json              任务台账（含进行中任务；重启恢复的依据）
    history.json            观看记忆（仅真实完成的任务；分享扩展字段预留）
    cache/{bvid}.json       成功结果缓存（相同视频+相同范围口径去重复用）
    jobs/{task_id}/         单任务的 spec/progress/result 与临时媒体

写入一律「临时文件 + os.replace」原子落盘；读取失败按损坏处理（返回默认值，
不抛出拖挂装配）。
"""

from __future__ import annotations

import json
import os
import threading
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

    def __init__(self, data_dir: Path) -> None:
        self._dir = Path(data_dir)
        self._tasks_path = self._dir / "tasks.json"
        self._history_path = self._dir / "history.json"
        self._cache_dir = self._dir / "cache"
        self._lock = threading.Lock()

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
    #: 文本来源模式沿用旧文件名 {bvid}.json（独立原型缓存可继续命中）；
    #: 其他模式（抽帧采样等）带模式 token（B 计划 §5.1：模式/能力版本入键，互不命中）。
    DEFAULT_CACHE_MODE = "text_sources"

    def cache_path(self, bvid: str, mode: str = DEFAULT_CACHE_MODE) -> Path:
        token = str(mode or "").strip() or "text_sources"
        if token == "text_sources":
            return self._cache_dir / f"{bvid}.json"
        safe = "".join(ch for ch in token if ch.isalnum() or ch in "-_")[:40] or "mode"
        return self._cache_dir / f"{bvid}__{safe}.json"

    def load_cache(self, bvid: str, mode: str = DEFAULT_CACHE_MODE) -> Optional[Dict[str, Any]]:
        return read_json(self.cache_path(bvid, mode), None)

    def save_cache(self, bvid: str, result: Dict[str, Any],
                   mode: str = DEFAULT_CACHE_MODE) -> None:
        atomic_write_json(self.cache_path(bvid, mode), result)

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
