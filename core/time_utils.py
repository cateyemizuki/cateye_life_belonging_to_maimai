"""时间工具：HH:MM 解析与分钟换算等纯时间函数。

R0-C2：窗口类判定（is_in_time_window）已迁至 planning/time_utils.py，
本模块只保留纯时间函数；core 不依赖 planning。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta


def parse_hhmm(value: str, default_minutes: int = 0) -> int:
    """把 "HH:MM" 解析成当天分钟数；非法输入回退默认值。"""
    try:
        parts = str(value).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (ValueError, IndexError, TypeError):
        pass
    return default_minutes


def minutes_of_now(now: datetime | None = None) -> int:
    return (now or datetime.now()).hour * 60 + (now or datetime.now()).minute


def next_run_at(now: datetime, hhmm: str) -> datetime:
    """返回 now 之后（含当天）最近的一次 hhmm 时刻，用于每日定时循环。"""
    target = now.replace(hour=parse_hhmm(hhmm) // 60, minute=parse_hhmm(hhmm) % 60, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def hours_between(later: datetime, earlier: datetime) -> float:
    """两个时刻的小时差（later - earlier，负值返回 0）。"""
    delta = (later - earlier).total_seconds() / 3600.0
    return max(0.0, delta)


#: 阈值区间写法的严格形态：单个数字（``8`` / ``8.5``）或 ``a-b``（``4-8``）
_HOURS_RANGE_RE = re.compile(r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$")


def parse_hours_range(value: object, default: tuple[float, float] = (4.0, 8.0)) -> tuple[float, float]:
    """解析「小时区间」配置（2026-10-10 阈值合并轮）：``"4-8"`` / ``8`` → ``(low, high)``。

    - 单个数字 ``N`` → ``(0, N)``（用户只写一个数 = 从 0 到该值）；
    - ``"a-b"`` → ``(min, max)``（``a > b`` 自动交换）；
    - 空值 / 负数 / 其它非法写法 → 回退 ``default``（默认 4~8 小时）。

    只做解析、不做 IO；伴护巡检（companion/patrol.py）与配置层共用同一口径。
    """
    raw = "" if value is None else str(value).strip()
    if _HOURS_RANGE_RE.match(raw):
        try:
            if "-" in raw:
                left, right = raw.split("-", 1)
                low, high = float(left), float(right)
            else:
                low, high = 0.0, float(raw)
        except (TypeError, ValueError):
            return default
        low, high = (low, high) if low <= high else (high, low)
        return max(0.0, low), max(0.0, high)
    return default
