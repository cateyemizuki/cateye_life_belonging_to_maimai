"""Small-event time-slot rules (v3.9.0).

Three non-overlapping local-clock slots gate both generation batches and runtime
picking. These are pure functions reused by every pick/start path so the prompt
layer is never the only defence. Slot ranges are suitability filters, not a
replacement for the sleep engine or the schedule controller.

Boundaries are left-closed/right-open in the plugin's effective timezone:
00:00<=t<08:00 -> 00-08; 08:00<=t<16:00 -> 08-16; 16:00<=t<24:00 -> 16-24.
"""
from __future__ import annotations

from datetime import datetime, tzinfo
from typing import Any, Mapping, Optional

SLOT_00_08 = "00-08"
SLOT_08_16 = "08-16"
SLOT_16_24 = "16-24"
TIME_SLOTS = (SLOT_00_08, SLOT_08_16, SLOT_16_24)

#: minute-of-day boundaries [start, end) for each slot, aligned with TIME_SLOTS
_SLOT_END = (480, 960, 1440)

SLOT_WINDOW_TEXT = {
    SLOT_00_08: "00:00–08:00（深夜与清晨）",
    SLOT_08_16: "08:00–16:00（日间）",
    SLOT_16_24: "16:00–24:00（傍晚至夜晚）",
}

#: per-slot generation focus, injected into the batch prompt
SLOT_GUIDANCE = {
    SLOT_00_08: "只生成低噪音、不扰民、不依赖白天日光的全天适宜小事；"
                "禁止浇花、打扫、外出采购、晨间采光依赖的活动。真实是否还在睡由睡眠引擎判断。",
    SLOT_08_16: "适合日间的家务、植物照料、整理桌面、学习、轻爱好等需要日光或 daytime 条件的活动。",
    SLOT_16_24: "傍晚至夜晚的轻娱乐、阅读、安静整理等温和个人活动；"
                "不要生成深夜仍不宜的打扫/高噪音活动，需要特定日光/噪声条件的活动应归入日间。",
}


def _wall_minute(now: datetime, tz: Optional[tzinfo]) -> int:
    """Local-clock minute-of-day in the effective timezone (never server UTC)."""
    if tz is not None:
        dt = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    else:
        dt = now
    return dt.hour * 60 + dt.minute


def time_slot_of(now: datetime, tz: Optional[tzinfo] = None) -> str:
    minute = _wall_minute(now, tz)
    if minute < _SLOT_END[0]:
        return SLOT_00_08
    if minute < _SLOT_END[1]:
        return SLOT_08_16
    return SLOT_16_24


def slot_remaining_minutes(now: datetime, tz: Optional[tzinfo] = None) -> int:
    """Whole minutes until the current slot's right boundary (>=1 within a slot)."""
    minute = _wall_minute(now, tz)
    for end in _SLOT_END:
        if minute < end:
            return end - minute
    return 0


def slot_end_minute(slot: str) -> int:
    try:
        return _SLOT_END[TIME_SLOTS.index(slot)]
    except ValueError:
        return 1440


def event_allowed_in_slot(event: Mapping[str, Any], slot: str) -> bool:
    """Whether ``event`` may occur in ``slot``.

    Generated events carry a single authoritative ``time_slot``; builtin events
    carry an ``allowed_slots`` list. Events with no recognised slot tag (legacy
    v1/v2 data) are NOT treated as all-day — they are excluded until regenerated.
    """
    if not isinstance(event, Mapping) or slot not in TIME_SLOTS:
        return False
    if event.get("pool_kind") == "builtin" or event.get("source") == "builtin":
        allowed = event.get("allowed_slots")
        return isinstance(allowed, (list, tuple)) and slot in allowed
    return event.get("time_slot") == slot