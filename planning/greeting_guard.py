"""Host message identity and durable receipts for scheduled greetings."""

from __future__ import annotations

from datetime import datetime
import math
from pathlib import Path
from typing import Any

from core.storage import load_json, save_json_atomic
from core.targeting import qq_id


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def sender_id(row: Any) -> str:
    info = _field(row, "message_info")
    user = _field(info, "user_info") or _field(row, "user_info")
    return qq_id(_field(row, "user_id") or _field(user, "user_id"))


def message_account(row: Any) -> str:
    return qq_id(_field(row, "account_id"))


def own_message(row: Any) -> bool | None:
    """The current SDK preserves sender and route account IDs but omits is_mai."""
    for name in ("is_mai", "is_self"):
        if _field(row, name) is True:
            return True
    sender, account = sender_id(row), message_account(row)
    if sender and account:
        return sender == account
    if _field(row, "is_mai") is False or _field(row, "received_at") is not None:
        return False
    return None


def message_epoch(row: Any, *, inbound: bool = False) -> float | None:
    raw = _field(row, "received_at") if inbound else None
    raw = raw or _field(row, "timestamp") or _field(row, "time")
    try:
        stamp = float(raw)
    except (TypeError, ValueError, OverflowError):
        if not isinstance(raw, (datetime, str)):
            return None
        try:
            dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(raw)
            stamp = dt.timestamp()
        except (ValueError, OSError, OverflowError):
            return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def is_target_reply(row: Any, owner: str) -> bool:
    """Count actual incoming target content, including pictures and voice."""
    if sender_id(row) != owner or own_message(row) is not False:
        return False
    if _field(row, "is_notify") or _field(row, "is_command"):
        return False
    text = str(_field(row, "processed_plain_text") or "").lstrip()
    return not text.startswith("/")


class TargetGreetingReceipts:
    """Last confirmed greeting per verified target, independent of planner admission."""

    def __init__(self, data_dir: Any):
        self._path = Path(data_dir) / "target_greeting_receipts.json" if data_dir else None
        self._recent_memory: dict[str, float] = {}

    def _load(self) -> dict | None:
        if self._path is None:
            return {}
        doc = load_json(self._path, None)
        if doc is None:
            return None if self._path.exists() else {}
        if isinstance(doc, dict) and doc.get("version") == 1 and isinstance(doc.get("by_target"), dict):
            return doc["by_target"]
        return None

    def last_sent(self, owner: str) -> tuple[bool, float]:
        doc = self._load()
        if doc is None:
            return False, 0.0
        saved = doc.get(owner, 0.0)
        if type(saved) not in (int, float) or not math.isfinite(saved) or saved < 0:
            return False, 0.0
        return True, max(float(saved), self._recent_memory.get(owner, 0.0))

    def record(self, owner: str, at: float) -> bool:
        if not owner or not math.isfinite(at) or at <= 0:
            return False
        doc = self._load()
        if doc is None:
            return False  # Preserve an unreadable journal for recovery.
        saved = doc.get(owner, 0.0)
        if type(saved) not in (int, float) or not math.isfinite(saved) or saved < 0:
            return False
        latest = max(at, self._recent_memory.get(owner, 0.0))
        if latest <= saved:
            return True
        self._recent_memory[owner] = latest
        if self._path is None:
            return True
        return save_json_atomic(self._path, {"version": 1, "by_target": {**doc, owner: latest}})
