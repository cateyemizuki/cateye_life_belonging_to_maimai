"""时间工具：HH:MM 解析、时间窗口判断（支持跨午夜）、分钟换算。"""

from __future__ import annotations

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


def is_in_time_window(window_start: str, window_end: str, current: str, *, invalid_result: bool = False) -> bool:
    """判断 current("HH:MM") 是否落在 [start, end) 窗口内。

    start > end 视为跨午夜区间（如 22:00-06:00）。时间配置非法时返回 invalid_result
    （静默窗口等 fail-safe 场景传 True：配置坏了宁可当静默处理）。
    """
    start, end, cur = parse_hhmm(window_start, -1), parse_hhmm(window_end, -1), parse_hhmm(current, -1)
    if start < 0 or end < 0 or cur < 0:
        return invalid_result
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜


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
