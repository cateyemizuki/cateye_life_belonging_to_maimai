"""Bounded sleep-referee receipts, stored with the VAD/settlement transaction.

Only structured results are retained; prompts and conversation context never enter
this payload. Unknown versions are preserved by the service's save boundary.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from .vad import VADState

VERSION = 1
MAX_BYTES = 32768
TTL = timedelta(hours=36)
AXES = ("valence", "arousal", "dominance")
NUMBERS = {"sleep_hours": (0, 24), "sleep_window_minutes": (0, 1440),
           "interrupted_minutes": (0, 1440), "actual_sleep_hours": (0, 24),
           "interrupt_count": (0, 100000), "commitment_net": (-100, 100),
           "delta_limit": (0, 1)}


def known(raw: Any) -> bool:
    return isinstance(raw, dict) and type(raw.get("version")) is int and raw["version"] == VERSION


def _number(raw: Any, lo: float, hi: float) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (float, int)):
        raise ValueError("number required")
    value = float(raw)
    if not math.isfinite(value) or not lo <= value <= hi:
        raise ValueError("number out of bounds")
    return value


def encode(cached: dict, period: tuple) -> dict:
    start, end = period
    out = {"version": VERSION, "cycle_key": str(cached["cycle_key"]),
           "period": [start.isoformat(), end.isoformat()],
           "computed_at": cached["computed_at"].isoformat()}
    for name in ("delta", "r_yesterday", "e_tomorrow"):
        out[name] = cached.get(name, VADState()).to_dict()
    out["reasons"] = {axis: str(cached.get("reasons", {}).get(axis, ""))[:300] for axis in AXES}
    for name in NUMBERS:
        if cached.get(name) is not None:
            out[name] = cached[name]
    # Validate our own output too; a malformed provider result is never durable.
    if decode(out, cached["computed_at"]) is None:
        raise ValueError("invalid sleep receipt")
    return out


def decode(raw: Any, now: datetime):
    try:
        if not known(raw) or len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_BYTES:
            return None
        start, end = [datetime.fromisoformat(x) for x in raw["period"]]
        computed = datetime.fromisoformat(raw["computed_at"])
        # Service dates are local wall times. Normalize aware receipts to the same
        # frame before comparing, without silently mixing naive/aware arithmetic.
        def local(value):
            if now.tzinfo is None:
                return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
            return value.astimezone(now.tzinfo) if value.tzinfo else value.replace(tzinfo=now.tzinfo)
        start, end, computed = map(local, (start, end, computed))
        key = f"{start:%Y-%m-%d}@{start.hour * 60 + start.minute}"
        if (raw["cycle_key"] != key or not start < end <= start + timedelta(hours=24)
                or not start <= computed <= now or now - computed > TTL):
            return None
        cached = {"cycle_key": key, "computed_at": computed}
        for name in ("delta", "r_yesterday", "e_tomorrow"):
            vector = raw[name]
            cached[name] = VADState(**{axis: _number(vector[axis], -1, 1) for axis in AXES})
        reasons = raw.get("reasons", {})
        if not isinstance(reasons, dict) or any(not isinstance(reasons.get(k, ""), str)
                or len(reasons.get(k, "")) > 300 for k in AXES):
            return None
        cached["reasons"] = {k: reasons.get(k, "") for k in AXES}
        for name, bounds in NUMBERS.items():
            if name in raw:
                cached[name] = _number(raw[name], *bounds)
        if "sleep_hours" not in cached:
            return None
        return cached, (start, end)
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
