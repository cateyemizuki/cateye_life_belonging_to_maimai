"""Schema Builder Module.

This module provides JSON Schema building functionality for schedule generation.
Separated from BaseScheduleGenerator to follow Single Responsibility Principle.

PR8 3.0.0 清退（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP4）：
旧 daily 全天 schema（``build_json_schema`` 及其缓存）已删除；仅保留 spine
日意图精简 Schema。注意：``planning/event_prompt.py`` 的同名 ``build_json_schema``
是事件库另一实现（event_catalog 在用），与本模块无关、未触碰。
"""

from typing import Any, Dict
import logging


logger = logging.getLogger(__name__)


class SchemaBuilder:
    """Schema构建器 - 单一职责：构建JSON Schema（spine 唯一路线）

    该类负责：
    1. 构建 spine 日意图的精简 JSON Schema
    2. 提供Schema验证标准

    与BaseScheduleGenerator的区别：
    - 只负责Schema构建，不涉及其他业务逻辑
    - 配置驱动，易于扩展
    """

    def __init__(self, config: Dict[str, Any]):
        """初始化Schema构建器

        Args:
            config: 配置字典
        """
        self.config = config

    def build_spine_schema(self, min_intentions: int = 3, max_intentions: int = 6) -> dict:
        """构建 spine 日意图的精简 JSON Schema（2026-10-25 滚动化改造新增）。

        R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
        D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，
        §4.2「精简 schema（8 个字段）」）。

        意图卡 8 个必填字段：``name / goal_type / window / min_minutes / max_minutes /
        priority / hardness / interruptibility``；``note`` 可选（只给 1-3 条重要意图写一句）。
        旧全天 schema（``build_json_schema``）已随 PR8 3.0.0 daily 链清退。
        """
        try:
            lo = max(1, int(min_intentions))
        except (TypeError, ValueError):
            lo = 3
        try:
            hi = max(lo, int(max_intentions))
        except (TypeError, ValueError):
            hi = max(lo, 6)
        return {
            "type": "object",
            "required": ["intentions"],
            "properties": {
                "intentions": {
                    "type": "array",
                    "minItems": lo,
                    "maxItems": hi,
                    "items": {
                        "type": "object",
                        "required": [
                            "name", "goal_type", "window", "min_minutes", "max_minutes",
                            "priority", "hardness", "interruptibility",
                        ],
                        "properties": {
                            "name": {
                                "type": "string",
                                "minLength": 2,
                                "maxLength": 20,
                                "description": "意图名称（简短中性）",
                            },
                            "goal_type": {
                                "type": "string",
                                "enum": [
                                    "daily_routine",
                                    "meal",
                                    "study",
                                    "work",
                                    "sleep",
                                    "entertainment",
                                    "social_maintenance",
                                    "exercise",
                                    "learn_topic",
                                    "rest",
                                    "free_time",
                                    "custom",
                                ],
                                "description": "活动类型",
                            },
                            "window": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": 2,
                                "items": {
                                    "type": "string",
                                    "pattern": "^([01]?[0-9]|2[0-3]):[0-5][0-9]$",
                                },
                                "description": "允许发生的窗口 [开始 HH:MM, 结束 HH:MM]",
                            },
                            "min_minutes": {
                                "type": "integer",
                                "minimum": 5,
                                "maximum": 720,
                                "description": "最短做多少分钟",
                            },
                            "max_minutes": {
                                "type": "integer",
                                "minimum": 5,
                                "maximum": 720,
                                "description": "最长做多少分钟（>= min_minutes）",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                                "description": "优先级",
                            },
                            "hardness": {
                                "type": "string",
                                "enum": ["hard", "soft"],
                                "description": "hard = 硬约定（时间固定不可移动）；soft = 软意图",
                            },
                            "interruptibility": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "被打断的代价（0~1，越高越容易被打断）",
                            },
                            "note": {
                                "type": "string",
                                "maxLength": 40,
                                "description": "可选：一句 15-25 字的补充说明（只给重要意图）",
                            },
                        },
                    },
                }
            },
        }
