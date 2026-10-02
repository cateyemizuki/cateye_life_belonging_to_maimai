"""时间工具（R0-C2 合并：窗口迁移/strip_tz + 时区管理 + 窗口判定 + 能量模型）。

来源：自主规划插件 utils/time_utils.py、utils/timezone_manager.py、
utils/energy_model.py（照搬合并；C1 裁决以本侧能量曲线为唯一实现）。

处理两种 time_window 格式：
    - 旧格式: [hour, hour]，hour 取 0-23
    - 新格式: [minutes, minutes]，minutes 取 0-1440（跨夜时 end 可 > 1440）

分层约定：core/time_utils.py 只保留纯时间函数（parse_hhmm / minutes_of_now /
next_run_at / hours_between）；planning 侧的时间/窗口/能量函数集中在本模块。
"""

from __future__ import annotations

import logging
from datetime import datetime, tzinfo
from typing import Any, List, Optional, Tuple, Union

from core.time_utils import parse_hhmm

logger = logging.getLogger(__name__)


# ==================== 时区管理（来源：自主规划插件 utils/timezone_manager.py，照搬）====================
# 解析优先级：
#     1. Python 3.9+ 标准库 ``zoneinfo``（无第三方依赖，IANA tzdata）
#     2. 第三方 ``pytz``（向后兼容）
#     3. 系统本地时区（兜底，可能不准确）


class TimezoneManager:
    """时区管理器 - 集中管理时区处理，避免重复代码。"""

    def __init__(self, timezone_str: str = "Asia/Shanghai") -> None:
        self.timezone_str: str = timezone_str
        self._tz: Optional[tzinfo] = self._init_timezone()

    def _init_timezone(self) -> Optional[tzinfo]:
        """按优先级解析时区对象；全部失败返回 None 由调用方降级到系统时间。"""
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(self.timezone_str)
        except ImportError:
            pass
        except Exception as exc:
            logger.warning(f"zoneinfo 加载 {self.timezone_str} 失败: {exc}，尝试 pytz 降级")

        try:
            import pytz
            return pytz.timezone(self.timezone_str)
        except ImportError:
            logger.warning("zoneinfo 与 pytz 均不可用，将使用系统时区")
        except Exception as exc:
            logger.warning(f"时区初始化失败: {exc}，将使用系统时区")
        return None

    def get_now(self) -> datetime:
        """获取配置时区下的当前时间；解析失败时回退系统时间。"""
        if self._tz is not None:
            return datetime.now(self._tz)
        return datetime.now()


# ==================== 时间窗口工具 ====================


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


def is_in_time_window(window_start: str, window_end: str, current: str, *, invalid_result: bool = False) -> bool:
    """判断 current("HH:MM") 是否落在 [start, end) 窗口内。

    start > end 视为跨午夜区间（如 22:00-06:00）。时间配置非法时返回 invalid_result
    （静默窗口等 fail-safe 场景传 True：配置坏了宁可当静默处理）。

    R0-C2：自 core/time_utils.py 迁入（窗口类判定函数，与窗口迁移/解析集中一处）。
    """
    start, end, cur = parse_hhmm(window_start, -1), parse_hhmm(window_end, -1), parse_hhmm(current, -1)
    if start < 0 or end < 0 or cur < 0:
        return invalid_result
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜


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


# ==================== 时段能量基线模型（来源：自主规划插件 utils/energy_model.py，照搬）====================
# 按 24 小时时段映射能量值（0-100），用于注入时提示 LLM 当前 bot 的精神状态。
# 参考真实人类作息：凌晨低 / 上午高峰 / 午后微疲 / 下午次高峰 / 晚间放松 / 深夜撑不住。

# 24 小时能量曲线（参考真实人类作息）
_ENERGY_TABLE: dict[int, int] = {
    0: 15, 1: 12, 2: 10, 3: 10, 4: 12, 5: 20,
    6: 50, 7: 60, 8: 70, 9: 80, 10: 85, 11: 88,
    12: 70, 13: 60, 14: 65, 15: 75, 16: 75, 17: 70,
    18: 65, 19: 70, 20: 65, 21: 55, 22: 45, 23: 30,
}


def get_energy_level(hour: int) -> int:
    """根据 24 小时制小时数返回能量值（0-100，数字越大越精神）。"""
    clamped = max(0, min(23, int(hour)))
    return _ENERGY_TABLE[clamped]


def describe_energy(energy: int) -> str:
    """把数值能量值映射为简短自然语言描述（5 档）。"""
    if energy >= 80:
        return "精神满满"
    if energy >= 60:
        return "状态不错"
    if energy >= 40:
        return "正常"
    if energy >= 25:
        return "有点累"
    if energy >= 10:
        return "困了"
    return "快撑不住"