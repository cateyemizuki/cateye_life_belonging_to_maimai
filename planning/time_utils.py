"""时间窗口工具（来源：自主规划插件 utils/time_utils.py，照搬）。

处理两种 time_window 格式：
    - 旧格式: [hour, hour]，hour 取 0-23
    - 新格式: [minutes, minutes]，minutes 取 0-1440（跨夜时 end 可 > 1440）
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional, Tuple, Union


def strip_tz(dt: Optional[datetime]) -> Optional[datetime]:
    """剥离 datetime 的 tzinfo，便于跨 tz-aware / tz-naive 安全比较。

    历史背景：数据库里旧数据是 tz-naive 字符串，新数据是 tz-aware 字符串，
    两者直接比较会抛 ``TypeError: can't compare offset-naive and offset-aware
    datetimes``。在所有 ``goal.created_at`` / ``goal.deadline`` 与"现在"的比较点
    统一剥离 tz，保证不会爆错。
    """
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def migrate_time_window(time_window: Optional[List[Union[int, float]]]) -> Optional[List[int]]:
    """迁移旧格式时间窗口到新格式（支持跨夜，如 [23, 1] → [1380, 1500]）。"""
    if not time_window or len(time_window) < 2:
        return None

    start, end = time_window[0], time_window[1]

    # 起止时间相同视为无效
    if start == end:
        return None

    # 两个值都 <= 24 判定为旧格式（小时）
    if start < 24 and end <= 24:
        start_minutes = start * 60
        end_minutes = end * 60
        if end_minutes <= start_minutes:
            end_minutes += 1440
        return [int(start_minutes), int(end_minutes)]

    # 已经是新格式
    return [int(start), int(end)]


def parse_time_window(time_window: Optional[List[Union[int, float]]]) -> Tuple[Optional[int], Optional[int]]:
    """解析时间窗口，统一返回分钟格式 (start_minutes, end_minutes) 或 (None, None)。"""
    migrated = migrate_time_window(time_window)
    if not migrated:
        return None, None
    return migrated[0], migrated[1]


def parse_time_slot(time_slot: str) -> Tuple[Optional[int], Optional[int]]:
    """解析 HH:MM 格式时间字符串为 (hour, minute)。"""
    if not time_slot or not isinstance(time_slot, str):
        return None, None
    try:
        parts = time_slot.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return hour, minute
    except (ValueError, IndexError):
        return None, None


def time_slot_to_minutes(time_slot: str) -> Optional[int]:
    """将 HH:MM 格式时间转换为从 00:00 开始的分钟数（如 570 表示 09:30）。"""
    hour, minute = parse_time_slot(time_slot)
    if hour is None:
        return None
    return hour * 60 + minute


def format_minutes_to_time(minutes: int) -> str:
    """将分钟数格式化为 HH:MM 字符串。"""
    hour = minutes // 60
    minute = minutes % 60
    return f"{hour:02d}:{minute:02d}"


def get_time_window_from_goal(goal: Any) -> Tuple[int, int]:
    """从目标对象中提取时间窗口（优先 parameters，其次 conditions；默认 (0, 60)）。"""
    time_window = None
    if hasattr(goal, "parameters") and goal.parameters and "time_window" in goal.parameters:
        time_window = goal.parameters.get("time_window")
    elif hasattr(goal, "conditions") and goal.conditions and "time_window" in goal.conditions:
        time_window = goal.conditions.get("time_window")

    if not time_window:
        return (0, 60)

    start_minutes, end_minutes = parse_time_window(time_window)
    if start_minutes is None:
        return (0, 60)
    return (start_minutes, end_minutes)
