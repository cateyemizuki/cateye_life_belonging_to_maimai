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


# ==================== 日帧分钟 / 睡眠环（跨午夜统一口径；N2/N4）====================
# 约定（日程生成链与展示/读取侧共用，唯一真源）：
#     条目 start_minute 取当天帧 [0, 1440) 的钟点分钟；
#     end_minute 允许 > 1440 表达"跨午夜"（如 23:00 入睡 + 8h ⇒ 1380→1860）；
#     展示侧一律经 :func:`format_day_minute` 渲染（>1440 ⇒ "次日 HH:MM"）；
#     读取侧的"是否在进行中 / 是否接下来"一律经 :func:`is_in_day_frame` /
#     :func:`is_upcoming_day_minute`（同一个 24h 环），不得各自手写 ``//60`` 或一次性 −1440。
DAY_MINUTES = 24 * 60
#: 模块内短名（历史沿用；对外请用 :data:`DAY_MINUTES`）
_DAY_MINUTES = DAY_MINUTES


def normalize_day_minutes(minutes: Any) -> int:
    """日帧分钟归一：任意分钟数 → ``[0, 1440)``（跨午夜条目 ``end>1440`` 归一后即次日钟点）。

    N2/N4：日程归一化按「跨午夜 = 同一个 24h 环」处理，条目的 ``end_minute`` 允许大于 1440
    （如 23:00 入睡 + 8h ⇒ 1380→1860）；展示/读取侧需先经本函数归一，再按 ``HH:MM`` 渲染，
    避免手写 ``//60/%60`` 或一次性 ``−1440`` 各自漂移。非法输入 → 0（不抛异常，可空降级）。
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return 0
    return value % _DAY_MINUTES


def _day_offset_minutes(minutes: Any) -> Tuple[int, int]:
    """日帧分钟 → ``(日偏移, 帧内钟点分钟)``（跨午夜分解的唯一实现）。

    - ``[0, 1440)`` → ``(0, 原值)``；
    - ``>= 1440``（跨午夜写法，如 1860 = 次日 07:00）→ ``(1, 原值 − 1440)``；
    - 负值（防御性；正常链路不产生）→ ``(-1, 原值 + 1440)``。

    非法输入按 ``0`` 处理（不抛异常，可空降级）。
    """
    try:
        value = int(minutes)
    except (TypeError, ValueError):
        return 0, 0
    offset = value // _DAY_MINUTES          # 向下取整：负值也得到负偏移
    return offset, value - offset * _DAY_MINUTES


def format_day_minute(minutes: Any) -> str:
    """日帧分钟 → 展示文本（**唯一实现**：禁止展示面各自手写 ``//60/%60``）。

    口径（与 :func:`normalize_day_minutes` 同一个 24h 环）：

    - ``[0, 1440)`` → ``HH:MM``（570 → ``09:30``）；
    - ``>= 1440`` → ``次日 HH:MM``（1860 → ``次日 07:00``；旧写法裸做 ``//60`` 会渲染成
      ``31:00``，即读取侧跨午夜渲染缺陷）；
    - 负值 → ``前日 HH:MM``（防御性）。

    偏移超过 1 天不在现有生成链内（条目时长 ≤ 12h + 跨午夜 ⇒ 偏移恒为 ±1）；此处同向偏移
    统一标「次日 / 前日」，不做多日标号。
    """
    offset, clock = _day_offset_minutes(minutes)
    text = f"{clock // 60:02d}:{clock % 60:02d}"
    if offset > 0:
        return f"次日 {text}"
    if offset < 0:
        return f"前日 {text}"
    return text


def is_in_day_frame(start_minute: Any, end_minute: Any, cur_minute: Any) -> bool:
    """条目 ``[start, end)`` 是否覆盖 ``cur``（跨午夜按**同一个 24h 环**判定，唯一实现）。

    ``end`` 可 >1440（跨午夜写法，如 23:00 入睡 + 8h ⇒ ``1380 → 1860``）：把 ``cur`` 与
    ``cur + 1440`` 两个等价环位一起试——``cur=30``（凌晨 00:30）⇒ ``1470`` 落在
    ``[1380, 1860)`` → 命中（"现在正在进行"）；``cur=600``（10:00）⇒ 两个环位都不落在窗口内
    → 未命中。``end <= start``（倒挂/无时长）视为非法条目 → ``False``。
    """
    try:
        start = int(start_minute)
        end = int(end_minute)
        cur = int(cur_minute)
    except (TypeError, ValueError):
        return False
    if end <= start:
        return False
    cur = normalize_day_minutes(cur)
    return start <= cur < end or start <= cur + _DAY_MINUTES < end


def is_upcoming_day_minute(start_minute: Any, cur_minute: Any, end_minute: Any = None) -> bool:
    """``start`` 是否在 ``cur`` 之后（**同一个 24h 环**，日帧 ``[0, 1440)``；唯一实现）。

    - ``start > cur``：同帧内稍后（14:00 > 12:30）→ ``True``；
    - ``start == cur``：正好开始（"正在进行"由 ``find_current_activity`` 负责）→ ``False``；
    - ``start == 0 < cur``：帧首分钟在环上同时是**帧尾**（= 1440，即"今晚 24:00"）→ ``True``
      ——这就是「23:35 的下一步是 00:00 的尾段条目」；白天查询时这类帧尾条目同样会列出
      （它表示"今晚 24:00"的下一段，同一环口径的自然结果），排序恒排最后；但给出
      ``end_minute`` 且 ``cur`` 仍落在该窗口内时（如 00:30 的 ``(0, 60)`` 尾段）它是
      "正在进行"而非"接下来" → ``False``；
    - 其余 ``start < cur``：帧内已过 → ``False``（**不**回绕成"明天同一时刻"，否则列表会
      把整天条目都当作"接下来"）。
    """
    start = normalize_day_minutes(start_minute)
    cur = normalize_day_minutes(cur_minute)
    if start > cur:
        return True
    if start != 0 or cur == 0:
        return False
    if end_minute is not None and is_in_day_frame(start, end_minute, cur):
        return False
    return True


def ring_minutes_ahead(start_minute: Any, cur_minute: Any) -> int:
    """``start`` 相对 ``cur`` 在 24h 环上"还在前面"的分钟数（``0`` = 同一分钟）。

    排序用（值越小越近）：帧尾（``start == 0 < cur``）得 ``1440 − cur``，自然排在最后；
    与 :func:`is_upcoming_day_minute` 同一环口径。
    """
    return (normalize_day_minutes(start_minute) - normalize_day_minutes(cur_minute)) % _DAY_MINUTES


def is_in_sleep_ring(start_minute: Any, wake_min: Any, sleep_min: Any) -> bool:
    """判断某钟点分钟是否落在**睡眠环**内（跨午夜按同一个 24h 环处理）。

    - ``sleep_min <= wake_min``（跨午夜作息，如 01:00 入睡 / 07:00 起床）：
      睡眠环 = ``[sleep_min, wake_min)``，清醒时段绕午夜延续（``[wake, 1440) ∪ [0, sleep)``）；
    - 否则（常规作息，如 23:00 入睡 / 07:00 起床）：
      睡眠环 = ``[sleep_min, 1440) ∪ [0, wake_min)``。

    睡眠环内的条目（睡觉 / 跨午夜条目）保留 ``start + 时长`` 的跨午夜写法（``end`` 可 >1440）；
    环外（清醒）条目的 ``end`` 才夹到入睡锚点（跨午夜作息下即 ``sleep_min + 1440``）。
    入参均为 HH:MM 解析出的钟点分钟；非法输入按 0 归一（可空降级）。
    """
    start = normalize_day_minutes(start_minute)
    wake = normalize_day_minutes(wake_min)
    sleep = normalize_day_minutes(sleep_min)
    if sleep <= wake:
        return sleep <= start < wake
    return start >= sleep or start < wake


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