"""诊断日志（来源：虚拟恋人插件 decision_logger / llm_logger，简化合并版）。

两个 JSONL 按天分文件：
- decisions_YYYY-MM-DD.jsonl  巡检/空间决策（trigger/skip/spoken/info）
- llm_calls_YYYY-MM-DD.jsonl  LLM 调用记录
保留 log_retention_days 天，每小时节流清理一次。

对外能力（对齐源插件，签名兼容约束：record(...) 参数不变）：
- record(...)                      写入（DecisionLogger 支持 record_skips 关掉跳过轮）
- read_recent(days, limit)         读取（新→旧，/life diag 用，保留）
- read_entries(days=None)          读取（旧→新升序，/mai_llm_log、/mai_diag 合并转发用）
- cleanup()                        按文件名日期清理过期文件（解析失败回退 mtime）
- update_settings(...)             热更新开关与保留天数
- enabled / retention_days         属性
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# 清理节流间隔（秒）：写入路径上最多每小时做一次过期清理
_CLEANUP_INTERVAL_SECONDS = 3600.0


def _cleanup_dir(directory: Path, retention_days: int) -> None:
    cutoff = time.time() - retention_days * 86400
    try:
        for f in directory.glob("*.jsonl"):
            if f.stat().st_mtime < cutoff:
                f.unlink()
    except OSError:
        pass


def _parse_file_date(name: str, prefix: str) -> Optional[Any]:
    """从 ``{prefix}_YYYY-MM-DD.jsonl`` 文件名解析日期；失败返回 None。"""
    stem = name[len(prefix):-len(".jsonl")]
    try:
        return datetime.strptime(stem, "%Y-%m-%d").date()
    except ValueError:
        return None


def _cleanup_by_date(directory: Path, pattern: str, prefix: str, retention_days: int) -> int:
    """按文件名中的日期删除过期日志（解析失败回退文件修改时间），返回删除数量。"""
    removed = 0
    if not directory.exists():
        return 0
    cutoff_date = (datetime.now() - timedelta(days=max(1, int(retention_days)))).date()
    try:
        candidates = list(directory.glob(pattern))
    except OSError:
        return 0
    for path in candidates:
        file_date = _parse_file_date(path.name, prefix)
        if file_date is None:
            try:
                file_date = datetime.fromtimestamp(path.stat().st_mtime).date()
            except OSError:
                continue
        if file_date < cutoff_date:
            try:
                path.unlink()
                removed += 1
            except OSError:
                continue
    return removed


def _read_entries_ascending(directory: Path, pattern_prefix: str,
                            retention_days: int, days: Optional[int]) -> list[dict[str, Any]]:
    """读取最近 N 天的 JSONL 记录（按时间升序），损坏行跳过。"""
    span = max(1, int(retention_days)) if days is None else max(1, int(days))
    entries: list[dict[str, Any]] = []
    seen_files: set[Path] = set()
    base = datetime.now()
    for offset in range(span):
        day = base - timedelta(days=offset)
        path = directory / f"{pattern_prefix}_{day.strftime('%Y-%m-%d')}.jsonl"
        if path in seen_files or not path.exists():
            continue
        seen_files.add(path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        entries.append(item)
        except OSError:
            continue
    entries.sort(key=lambda item: str(item.get("time", "")))
    return entries


class DecisionLogger:
    ACTION_TRIGGER = "trigger"
    ACTION_SKIP = "skip"
    #: planner 确认真的生成并发送了回复（对最近一次触发的回执）
    ACTION_SPOKEN = "spoken"
    ACTION_INFO = "info"

    def __init__(self, data_dir: Path, enabled: bool = True, retention_days: int = 3,
                 record_skips: bool = True) -> None:
        self._dir = Path(data_dir) / "decision_logs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._enabled = enabled
        self._retention_days = max(1, int(retention_days))
        self._record_skips = bool(record_skips)  # False = 只记真正触发的轮次
        self._last_cleanup = 0.0

    # ---- 属性与热更新 ----
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def retention_days(self) -> int:
        return self._retention_days

    @property
    def record_skips(self) -> bool:
        return self._record_skips

    def update_settings(self, enabled: bool, retention_days: int,
                        record_skips: Optional[bool] = None) -> None:
        """热更新开关与保留天数（配置变更时调用）。"""
        self._enabled = bool(enabled)
        self._retention_days = max(1, int(retention_days))
        if record_skips is not None:
            self._record_skips = bool(record_skips)

    def record(self, *, action: str, trigger_type: str = "", reason: str = "", **fields: Any) -> None:
        if not self._enabled:
            return
        normalized = str(action or "").strip() or self.ACTION_SKIP
        if normalized == self.ACTION_SKIP and not self._record_skips:
            return
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "action": normalized,
            "trigger_type": trigger_type,
            "reason": (reason or "")[:400],
            **fields,
        }
        try:
            path = self._dir / f"decisions_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            return
        if time.monotonic() - self._last_cleanup > _CLEANUP_INTERVAL_SECONDS:
            self._last_cleanup = time.monotonic()
            self.cleanup()

    def read_recent(self, days: int = 1, limit: int = 50) -> list[dict[str, Any]]:
        """读取最近 N 天的决策日志（新→旧），供 /life diag 使用。"""
        entries: list[dict[str, Any]] = []
        now = datetime.now()
        for offset in range(days):
            d = datetime.fromordinal(now.toordinal() - offset).strftime("%Y-%m-%d")
            path = self._dir / f"decisions_{d}.jsonl"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue
        return entries[-limit:][::-1]

    def read_entries(self, days: Optional[int] = None) -> list[dict[str, Any]]:
        """读取最近 N 天的决策记录（按时间升序）；None = 整个保留期。"""
        return _read_entries_ascending(self._dir, "decisions", self._retention_days, days)

    def cleanup(self) -> int:
        """删除超过保留天数的日志文件，返回删除数量。"""
        removed = _cleanup_by_date(self._dir, "decisions_*.jsonl", "decisions_", self._retention_days)
        if removed < 1:  # 兜底：文件名不规则时按 mtime 再扫一遍
            _cleanup_dir(self._dir, self._retention_days)
        return removed


class LLMCallLogger:
    def __init__(self, data_dir: Path, enabled: bool = True, retention_days: int = 3) -> None:
        self._dir = Path(data_dir) / "llm_logs"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._enabled = enabled
        self._retention_days = max(1, int(retention_days))
        self._last_cleanup = 0.0

    # ---- 属性与热更新 ----
    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def retention_days(self) -> int:
        return self._retention_days

    def update_settings(self, enabled: bool, retention_days: int) -> None:
        """热更新开关与保留天数（配置变更时调用）。"""
        self._enabled = bool(enabled)
        self._retention_days = max(1, int(retention_days))

    def record(self, *, event: str, task: str, success: bool, response: str = "", error: str = "") -> None:
        if not self._enabled:
            return
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "task": task,
            "success": success,
            "response": (response or "")[:4000],
            "error": (error or "")[:400],
        }
        try:
            path = self._dir / f"llm_calls_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return
        if time.monotonic() - self._last_cleanup > _CLEANUP_INTERVAL_SECONDS:
            self._last_cleanup = time.monotonic()
            self.cleanup()

    def read_entries(self, days: Optional[int] = None) -> list[dict[str, Any]]:
        """读取最近 N 天的调用记录（按时间升序）；None = 整个保留期。"""
        return _read_entries_ascending(self._dir, "llm_calls", self._retention_days, days)

    def cleanup(self) -> int:
        """删除超过保留天数的日志文件，返回删除数量。"""
        removed = _cleanup_by_date(self._dir, "llm_calls_*.jsonl", "llm_calls_", self._retention_days)
        if removed < 1:  # 兜底：文件名不规则时按 mtime 再扫一遍
            _cleanup_dir(self._dir, self._retention_days)
        return removed
