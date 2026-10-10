"""Schedule Semantic Validator Module.

This module provides semantic validation for schedule items.
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from ..time_utils import normalize_day_minutes


logger = logging.getLogger(__name__)

#: 日程条目 emotional_tone 的逐维裁剪界（MAINTENANCE_PLAN §6.2 E：裁剪 ±0.5）
EMOTIONAL_TONE_CLAMP = 0.5
#: emotional_tone 的三个维度（V 效价 / A 唤醒 / D 支配）
EMOTIONAL_TONE_AXES = ("valence", "arousal", "dominance")

#: 空档类警告的识别标记（N2/N1：quality_scorer 据此单独重罚，空档必须能压到重试线下）
GAP_WARNING_MARKER = "时间空档"


def is_gap_warning(warning: Any) -> bool:
    """警告是否属于「时间空档」类（N2/N1：空档类警告单独重罚，能触发重试）。"""
    return GAP_WARNING_MARKER in str(warning or "")


def item_time_block(item: Any) -> Optional[Tuple[str, int, int]]:
    """取日程条目 ``(名称, 起始分钟, 结束分钟)``；取不到 → ``None``。

    两形态通吃（N2：空档检查 / 覆盖率评分在归一化前、后都能跑，取数口径唯一）：

    - **归一化条目**（DB 形态）：``start_minute`` / ``end_minute``（跨午夜 ``end`` 可 >1440）；
    - **原始条目**（LLM 形态）：``time_slot`` + ``duration_hours``；缺 ``duration_hours``
      时取不到结束时间 → ``None``（与旧实现口径一致，不臆造时长）。
    """
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or "未命名")
    start, end = item.get("start_minute"), item.get("end_minute")
    if start is not None and end is not None:
        try:
            return name, int(start), int(end)
        except (TypeError, ValueError):
            return None
    time_slot = str(item.get("time_slot") or "")
    duration_hours = item.get("duration_hours")
    if not time_slot or duration_hours is None:
        return None
    try:
        start_minutes = ScheduleSemanticValidator._parse_time_to_minutes(time_slot)
        return name, start_minutes, start_minutes + int(float(duration_hours) * 60)
    except (TypeError, ValueError):
        return None


def clip_emotional_tone(raw: Any, limit: float = EMOTIONAL_TONE_CLAMP) -> Dict[str, float]:
    """校验并裁剪日程条目 ``emotional_tone``（M5；§6.2 E：入库 parameters，裁剪 ±0.5）。

    - 非 dict / 全维无效 → 返回空 dict（视为"无情绪基调"，睡眠侧按 0 计）；
    - 单维非数值（字符串/布尔等）一律跳过（不写非法值、不抛异常）；
    - 数值逐维夹到 ±limit（默认 ±0.5），保证任意 LLM 输出都无法越界。
    """
    if not isinstance(raw, dict):
        return {}
    try:
        bound = abs(float(limit))
    except (TypeError, ValueError):
        bound = EMOTIONAL_TONE_CLAMP
    clipped: Dict[str, float] = {}
    for axis in EMOTIONAL_TONE_AXES:
        value = raw.get(axis)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        clipped[axis] = max(-bound, min(bound, number))
    return clipped


class ScheduleSemanticValidator:
    """
    日程语义验证器

    检查日程的语义合理性，包括：
    - 时间合理性（用餐时间、作息时间等）
    - 活动持续时间
    - 优先级匹配
    """

    #: 空档判定阈值（分钟）：相邻条目间隙 ≥ 此值计为「时间空档」（N2 原口径不变）
    GAP_MINUTES = 30

    # 合理时间范围（小时）- 基于常识的时间安排
    REASONABLE_TIME_RANGES = {
        "meal": {
            # 用餐时间遵循常规生活习惯
            "早餐": (6, 9),    # 早餐 6:00-9:00
            "午餐": (11, 14),  # 午餐 11:00-14:00
            "晚餐": (17, 20),  # 晚餐 17:00-20:00
            "早饭": (6, 9),
            "午饭": (11, 14),
            "晚饭": (17, 20),
        },
        "daily_routine": {
            "睡觉": [(22, 24), (0, 6)],  # 22点-次日6点（跨午夜）
            "睡前": [(21, 24), (0, 2)],  # 睡前活动：21点-次日2点
            "起床": (6, 10),
            "洗漱": (6, 23),
        },
        "social_maintenance": {
            "夜聊": (20, 24),  # 夜聊：20点-24点
            "晚安": (21, 24),  # 晚安：21点-24点
        },
        "study": {
            "上课": (8, 18),
            "自习": (8, 23),
            "学习": (8, 23),
        },
        "exercise": {
            "运动": [(6, 9), (17, 22)],  # 早上或晚上
            "健身": [(6, 9), (17, 22)],
        }
    }

    def validate(self, items: List[Dict]) -> Tuple[List[Dict], List[str]]:
        """
        语义验证

        Args:
            items: 日程项列表

        Returns:
            (有效项列表, 警告列表)
        """
        valid_items = []
        warnings = []

        for idx, item in enumerate(items):
            item_warnings = []

            # 1. 检查时间合理性
            time_warning = self._check_time_reasonableness(item)
            if time_warning:
                item_warnings.append(time_warning)

            # 2. 检查活动持续时间
            duration_warning = self._check_duration(item, items)
            if duration_warning:
                item_warnings.append(duration_warning)

            # 3. 检查优先级合理性
            priority_warning = self._check_priority_match(item)
            if priority_warning:
                item_warnings.append(priority_warning)

            if item_warnings:
                warnings.append(f"第{idx+1}项 ({item.get('name', '未命名')}): " + "; ".join(item_warnings))

            # 即使有警告也保留该项（只是记录）
            valid_items.append(item)

        # 4. 检查时间连续性（整体验证）
        continuity_warnings = self._check_time_continuity(items)
        warnings.extend(continuity_warnings)

        return valid_items, warnings

    def _check_time_reasonableness(self, item: Dict) -> Optional[str]:
        """检查时间是否合理"""
        time_slot = item.get("time_slot", "")
        goal_type = item.get("goal_type")
        name = item.get("name", "")

        if not time_slot:
            return None

        try:
            hour = int(time_slot.split(":")[0])
        except (ValueError, IndexError, AttributeError) as e:
            logger.warning(f"时间格式错误: {time_slot} - {e}")
            return "时间格式错误"

        # 检查用餐时间
        if goal_type == "meal":
            for meal_name, (start_h, end_h) in self.REASONABLE_TIME_RANGES["meal"].items():
                if meal_name in name:
                    if not (start_h <= hour <= end_h):
                        return f"{meal_name}时间不合理（{time_slot}），建议{start_h:02d}:00-{end_h:02d}:00"

        # 检查作息时间
        if goal_type == "daily_routine":
            for routine_name, time_range in self.REASONABLE_TIME_RANGES["daily_routine"].items():
                if routine_name in name:
                    if isinstance(time_range, list):
                        # 跨午夜的时间段
                        in_range = any(start <= hour <= end for start, end in time_range)
                        if not in_range:
                            return f"{routine_name}时间不合理（{time_slot}）"
                    else:
                        start_h, end_h = time_range
                        if not (start_h <= hour <= end_h):
                            return f"{routine_name}时间不合理（{time_slot}），建议{start_h:02d}:00-{end_h:02d}:00"

        # 检查学习时间
        if goal_type == "study":
            for study_name, (start_h, end_h) in self.REASONABLE_TIME_RANGES["study"].items():
                if study_name in name:
                    if not (start_h <= hour <= end_h):
                        return f"{study_name}时间不合理（{time_slot}），建议{start_h:02d}:00-{end_h:02d}:00"

        # 检查运动时间
        if goal_type == "exercise":
            for exercise_name, time_ranges in self.REASONABLE_TIME_RANGES["exercise"].items():
                if exercise_name in name:
                    in_range = any(start <= hour <= end for start, end in time_ranges)
                    if not in_range:
                        return f"{exercise_name}时间不合理（{time_slot}），建议早上6-9点或晚上17-22点"

        # 检查社交活动时间（如夜聊等）
        if goal_type == "social_maintenance":
            for social_name, time_range in self.REASONABLE_TIME_RANGES.get("social_maintenance", {}).items():
                if social_name in name:
                    if isinstance(time_range, list):
                        # 跨午夜的时间段
                        in_range = any(start <= hour <= end for start, end in time_range)
                        if not in_range:
                            return f"{social_name}时间不合理（{time_slot}）"
                    else:
                        start_h, end_h = time_range
                        if not (start_h <= hour <= end_h):
                            return f"{social_name}时间不合理（{time_slot}），建议{start_h:02d}:00-{end_h:02d}:00"

        return None

    def _check_duration(self, item: Dict, all_items: List[Dict]) -> Optional[str]:
        """检查活动持续时间是否合理"""
        time_slot = item.get("time_slot", "")
        name = item.get("name", "")

        if not time_slot:
            return None

        # 找到下一个活动的时间
        current_minutes = self._parse_time_to_minutes(time_slot)

        next_minutes = None
        for other in all_items:
            if other != item:
                other_minutes = self._parse_time_to_minutes(other.get("time_slot", ""))
                if other_minutes > current_minutes:
                    if next_minutes is None or other_minutes < next_minutes:
                        next_minutes = other_minutes

        if next_minutes:
            duration = next_minutes - current_minutes

            # 检查持续时间是否合理
            if duration < 15:
                return f"持续时间过短（{duration}分钟），建议至少15分钟"

            # 睡觉、休息、自由时间可以超过3小时
            if duration > 180 and "自由" not in name and "休息" not in name and "睡" not in name and "安睡" not in name:
                return f"持续时间过长（{duration}分钟），建议不超过3小时"

        return None

    def _check_priority_match(self, item: Dict) -> Optional[str]:
        """检查优先级是否与活动类型匹配"""
        goal_type = item.get("goal_type")
        priority = item.get("priority")
        name = item.get("name", "")

        # 吃饭、睡觉应该是high或medium优先级
        if goal_type in ["meal", "daily_routine"]:
            if "睡觉" in name or "吃" in name or "早饭" in name or "午饭" in name or "晚饭" in name:
                if priority == "low":
                    return "基本生理需求应该设为medium或high优先级"

        return None

    @staticmethod
    def _parse_time_to_minutes(time_str: str) -> int:
        """将HH:MM转换为分钟数"""
        try:
            parts = time_str.split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except (ValueError, IndexError, AttributeError):
            return 0

    @staticmethod
    def _item_time_block(item: Any) -> Optional[Tuple[str, int, int]]:
        """取条目 ``(名称, 起始分钟, 结束分钟)``；取不到 → ``None``（委托模块级 :func:`item_time_block`）。"""
        return item_time_block(item)

    @classmethod
    def _gap_warnings(cls, blocks: List[Tuple[str, int, int]], *,
                      min_gap_minutes: int = GAP_MINUTES,
                      allowed_gap: Optional[Tuple[int, int]] = None) -> List[str]:
        """相邻条目空档检查（``blocks`` = ``(名称, 起始, 结束)``，按起始排序后逐对比较）。

        ``end`` 可 >1440（跨午夜写法）；展示用钟点统一经 :func:`normalize_day_minutes` 归一
        （1500 → 01:00），避免出现 "25:00" 这类帧外钟点。``allowed_gap`` 内的间隙不算空档
        （跨午夜作息下缺睡觉条目时，睡眠环本身就是留空段）。
        """
        if not blocks:
            return []
        ordered = sorted(blocks, key=lambda block: block[1])
        warnings: List[str] = []
        for current, nxt in zip(ordered, ordered[1:]):
            current_end, next_start = current[2], nxt[1]
            if next_start <= current_end:
                continue
            gap_minutes = next_start - current_end
            if gap_minutes < min_gap_minutes:
                continue
            if allowed_gap and allowed_gap[0] <= current_end and next_start <= allowed_gap[1]:
                continue
            current_end_time = f"{normalize_day_minutes(current_end) // 60:02d}:{normalize_day_minutes(current_end) % 60:02d}"
            next_start_time = f"{normalize_day_minutes(next_start) // 60:02d}:{normalize_day_minutes(next_start) % 60:02d}"
            warnings.append(
                f"注意：时间空档：{current_end_time}-{next_start_time} "
                f"({gap_minutes / 60.0:.1f}小时无安排)"
            )
        return warnings

    def _check_time_continuity(self, items: List[Dict]) -> List[str]:
        """检查时间连续性，检测活动之间的空档

        Args:
            items: 日程项列表（原始 LLM 形态；归一化条目同样可判）

        Returns:
            警告列表
        """
        blocks = [block for block in (self._item_time_block(item) for item in (items or [])) if block]
        return self._gap_warnings(blocks)

    def check_normalized_gaps(
        self,
        items: List[Dict],
        *,
        min_gap_minutes: int = GAP_MINUTES,
        wake_min: Optional[int] = None,
        sleep_min: Optional[int] = None,
    ) -> List[str]:
        """对**归一化之后**的条目复查空档（N2/N1：归一化制造的空档只有此时才看得见）。

        入参取 ``start_minute`` / ``end_minute``（DB 形态）；跨午夜条目 ``end`` 可 >1440，
        展示钟点按日帧归一。跨午夜作息（``sleep_min <= wake_min``，如 01:00 入睡 / 07:00 起床）
        且睡觉条目缺失时，睡眠环 ``[sleep_min, wake_min)`` 本身会表现为一段空档——该段不算空档
        （睡眠时段本就留空），其余间隙按 :attr:`GAP_MINUTES` 判定。
        """
        blocks = [block for block in (self._item_time_block(item) for item in (items or [])) if block]
        allowed_gap: Optional[Tuple[int, int]] = None
        if wake_min is not None and sleep_min is not None and int(sleep_min) <= int(wake_min):
            allowed_gap = (int(sleep_min), int(wake_min))
        return self._gap_warnings(blocks, min_gap_minutes=min_gap_minutes, allowed_gap=allowed_gap)
