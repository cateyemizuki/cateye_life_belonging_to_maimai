"""Schedule Quality Scorer Module.

This module provides quality scoring for generated schedules,
evaluating factors like activity count, description length, and time coverage.

Classes:
    ScheduleQualityScorer: Calculates quality scores for schedules

Example:
    >>> scorer = ScheduleQualityScorer(config)
    >>> score = scorer.calculate_score(items, warnings)
    >>> print(f"Quality: {score:.2f}")
"""

from typing import Any, Dict, List
import logging

from core.time_utils import parse_hhmm

from .validator import is_gap_warning, item_time_block

logger = logging.getLogger(__name__)

#: 普通警告单项罚分（旧值；保持向后兼容）
WARNING_PENALTY_PER_ITEM = 0.05
#: 空档类警告单项罚分（N2/N1：≥0.25 —— 单项即可把满分压到 0.75 < 0.85 重试线）
GAP_WARNING_PENALTY = 0.25
#: 评分兜底作息锚点（配置缺 ``wake_time`` / ``sleep_time`` 时用）
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60
_DAY_MINUTES = 24 * 60


class ScheduleQualityScorer:
    """日程质量评分器

    职责：
    - 评估生成日程的质量（0.0-1.0）
    - 考虑多个维度：活动数量、描述长度、时间覆盖、警告数量
    - 支持可配置的评分权重

    评分维度：
    1. 基础分（base）
    2. 活动数量合理性（activity_count）
    3. 描述长度充分性（description_length）
    4. 时间覆盖全天性（time_coverage）
    5. 警告惩罚（warnings）

    Args:
        config: 配置字典，包含评分相关参数
            - min_activities: 最少活动数
            - max_activities: 最多活动数
            - min_description_length: 最短描述
            - max_description_length: 最长描述

    Examples:
        >>> config = {
        ...     'min_activities': 8,
        ...     'max_activities': 15,
        ...     'min_description_length': 20,
        ...     'max_description_length': 50
        ... }
        >>> scorer = ScheduleQualityScorer(config)
        >>> items = [{"name": "早餐", "description": "吃早餐吃早餐吃早餐", "time_slot": "08:00"}]
        >>> score = scorer.calculate_score(items, [])
        >>> score > 0.0
        True
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化评分器

        Args:
            config: 配置字典
        """
        self.config = config

        # 评分权重（可通过配置覆盖）
        self.weights = {
            'base': 0.5,
            'activity_count': 0.2,
            'description_length': 0.15,
            'time_coverage': 0.15,
            'warnings': 0.3  # 警告惩罚上限
        }

    def calculate_score(
        self,
        items: List[Dict[str, Any]],
        warnings: List[str]
    ) -> float:
        """计算日程质量分数

        评分公式：
        ```
        score = base
              + (活动数量合理 ? activity_count_weight : 0)
              + (描述长度充分 ? description_length_weight : 0)
              + (时间覆盖率 * time_coverage_weight)
              - min(普通警告数 * 0.05, warnings_weight)
              - min(空档警告数 * GAP_WARNING_PENALTY, 1.0)
        ```

        空档类警告（``validator.GAP_WARNING_MARKER``）单独重罚 ≥0.25/条（N2/N1：旧口径 0.05/条
        时 0.95 分照样放行；现单项空档即可把满分压到 0.75 < 质量阈值，触发下一轮重试）。
        条目可为**归一化形态**（``start_minute``/``end_minute``）或**原始形态**
        （``time_slot``/``duration_hours``），取数口径见 ``validator.item_time_block``。

        Args:
            items: 日程项列表
            warnings: 警告列表（validator 产出）

        Returns:
            质量分数（0.0-1.0之间）

        Examples:
            >>> scorer = ScheduleQualityScorer({
            ...     'min_activities': 5,
            ...     'max_activities': 10,
            ...     'min_description_length': 10,
            ...     'max_description_length': 50
            ... })
            >>> items = [
            ...     {"description": "这是一个足够长的描述文本", "time_slot": "08:00"},
            ...     {"description": "另一个活动的描述", "time_slot": "12:00"}
            ... ]
            >>> score = scorer.calculate_score(items, [])
            >>> 0.0 <= score <= 1.0
            True
        """
        if not items:
            logger.warning("日程项为空，质量分数为0")
            return 0.0

        # 读取配置参数
        min_activities = self.config.get('min_activities', 8)
        max_activities = self.config.get('max_activities', 15)
        min_desc_len = self.config.get('min_description_length', 20)
        max_desc_len = self.config.get('max_description_length', 50)
        target_desc_len = (min_desc_len + max_desc_len) // 2

        # 初始化分数
        score = self.weights['base']

        # 1. 活动数量评分
        activity_count_score = self._score_activity_count(
            len(items),
            min_activities,
            max_activities
        )
        score += activity_count_score * self.weights['activity_count']

        # 2. 描述长度评分
        description_score = self._score_description_length(
            items,
            min_desc_len,
            target_desc_len
        )
        score += description_score * self.weights['description_length']

        # 3. 时间覆盖率评分
        coverage_score = self._score_time_coverage(items)
        score += coverage_score * self.weights['time_coverage']

        # 4. 警告惩罚：普通警告按旧口径，空档类单独重罚（N2/N1：空档必须能触发重试）
        gap_count = sum(1 for warning in (warnings or []) if is_gap_warning(warning))
        other_count = max(0, len(warnings or []) - gap_count)
        ordinary_penalty = min(other_count * WARNING_PENALTY_PER_ITEM, self.weights['warnings'])
        gap_penalty = min(gap_count * GAP_WARNING_PENALTY, 1.0)
        warning_penalty = ordinary_penalty + gap_penalty
        score -= warning_penalty

        # 确保分数在[0, 1]范围内
        final_score = max(0.0, min(1.0, score))

        logger.debug(
            f"质量评分: {final_score:.2f} "
            f"(活动数:{activity_count_score:.2f}, "
            f"描述:{description_score:.2f}, "
            f"覆盖:{coverage_score:.2f}, "
            f"警告:-{ordinary_penalty:.2f}, 空档:-{gap_penalty:.2f})"
        )

        return final_score

    def _score_activity_count(
        self,
        count: int,
        min_count: int,
        max_count: int
    ) -> float:
        """评估活动数量

        Args:
            count: 实际活动数
            min_count: 最少要求
            max_count: 最多要求

        Returns:
            分数（0.0-1.0）
        """
        if min_count <= count <= max_count:
            return 1.0  # 完美
        elif count >= min_count - 2:
            return 0.5  # 接近要求
        else:
            return 0.0  # 不达标

    def _score_description_length(
        self,
        items: List[Dict[str, Any]],
        min_length: int,
        target_length: int
    ) -> float:
        """评估描述长度

        Args:
            items: 日程项列表
            min_length: 最短要求
            target_length: 目标长度

        Returns:
            分数（0.0-1.0）
        """
        if not items:
            return 0.0

        # 计算平均描述长度
        total_length = sum(len(item.get('description', '')) for item in items)
        avg_length = total_length / len(items)

        if avg_length >= target_length:
            return 1.0  # 达到目标
        elif avg_length >= min_length:
            return 0.5  # 达到最低要求
        else:
            return 0.0  # 不达标

    def _score_time_coverage(self, items: List[Dict[str, Any]]) -> float:
        """评估**清醒时段**的时间覆盖率（分钟级）：Σ(条目在清醒环内占用的分钟) / 清醒时长。

        清醒环 = ``[wake_min, sleep_min + 1440)``（跨午夜作息 ``sleep_min <= wake_min`` 时绕午夜；
        否则 ``[wake_min, sleep_min)``）；锚点取配置 ``wake_time`` / ``sleep_time``（缺省 07:00/23:00）。
        条目可为归一化形态或原始形态（``validator.item_time_block``）；跨午夜条目 ``end`` 可 >1440，
        按 ``k∈{-1,0,1}`` 平移与清醒环求交，不重复计数。

        N2：旧口径只数"不同起始小时数 / 16"——条目被压成 15 分钟时覆盖率几乎不降
        （真机 0.69 照旧放行）；分钟级口径下塌缩/空档会直接压低覆盖率。

        Args:
            items: 日程项列表

        Returns:
            覆盖率（0.0-1.0）

        Examples:
            >>> scorer = ScheduleQualityScorer({})
            >>> items = [
            ...     {"time_slot": "08:00"},
            ...     {"time_slot": "12:00"},
            ...     {"time_slot": "18:00"}
            ... ]
            >>> coverage = scorer._score_time_coverage(items)
            >>> 0.0 <= coverage <= 1.0
            True
        """
        if not items:
            return 0.0

        wake_min = parse_hhmm(str(self.config.get('wake_time') or ''), _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(str(self.config.get('sleep_time') or ''), _DEFAULT_SLEEP_MINUTES)
        awake_end = sleep_min + (_DAY_MINUTES if sleep_min <= wake_min else 0)
        awake_len = awake_end - wake_min
        if awake_len <= 0:
            return 0.0

        covered = 0
        for item in items:
            block = item_time_block(item)
            if block is None:
                continue
            start, end = block[1], block[2]
            if end <= start:
                continue
            for shift in (-_DAY_MINUTES, 0, _DAY_MINUTES):
                lo = max(start + shift, wake_min)
                hi = min(end + shift, awake_end)
                if hi > lo:
                    covered += hi - lo

        return min(1.0, covered / float(awake_len))
