"""参数验证器（来源：自主规划插件 core/parameter_validator.py，照搬）。

统一的参数验证逻辑，消除多模块间的代码重复。
"""

from typing import Any

from .exceptions import InvalidTimeWindowError

# 时间常量
MINUTES_PER_DAY = 1440  # 24小时 = 1440分钟
MIN_TIME_MINUTES = 0
MAX_TIME_MINUTES = MINUTES_PER_DAY


class ParameterValidator:
    """参数验证器 - 统一的参数验证逻辑（时间窗口验证：格式、范围、逻辑）。

    Example:
        >>> ParameterValidator.validate_time_window([480, 540])  # 08:00-09:00
    """

    @staticmethod
    def validate_time_window(time_window: Any, field_name: str = "time_window") -> None:
        """验证时间窗口格式和值范围。

        Args:
            time_window: 时间窗口（应为 [start_minutes, end_minutes] 格式）
            field_name: 字段名称（用于错误消息）

        Raises:
            InvalidTimeWindowError: 时间窗口无效

        验证规则：
        - 必须是列表
        - 必须有2个元素
        - 元素必须是整数
        - 值必须在0-1440范围内（24小时 = 1440分钟）
        - 起始时间必须小于结束时间

        ⚠️ 注意：该验证不接受跨午夜窗口（end > 1440）；跨夜日程只能由
        日程生成链路（apply_schedule）产生，manage_goal 工具路径不支持。
        """
        if not isinstance(time_window, list):
            raise InvalidTimeWindowError(
                f"{field_name}必须是列表，当前类型: {type(time_window).__name__}",
                time_window=time_window,
            )

        if len(time_window) != 2:
            raise InvalidTimeWindowError(
                f"{field_name}必须包含2个元素，当前: {len(time_window)}个",
                time_window=time_window,
            )

        if not all(isinstance(x, int) for x in time_window):
            raise InvalidTimeWindowError(
                f"{field_name}的元素必须是整数，当前: {[type(x).__name__ for x in time_window]}",
                time_window=time_window,
            )

        # 验证取值范围（使用常量）
        start, end = time_window
        if not (MIN_TIME_MINUTES <= start < MAX_TIME_MINUTES and MIN_TIME_MINUTES < end <= MAX_TIME_MINUTES):
            raise InvalidTimeWindowError(
                f"{field_name}的值必须在{MIN_TIME_MINUTES}-{MAX_TIME_MINUTES}范围内，当前: {time_window}",
                time_window=time_window,
            )

        if start >= end:
            raise InvalidTimeWindowError(
                f"{field_name}的起始时间必须小于结束时间，当前: {time_window}",
                time_window=time_window,
            )
