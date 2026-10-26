"""Generator 子包 —— 日程生成组件（spine 唯一路线）。

PR8 3.0.0 清退（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP4）：
旧 daily 生成面已整链删除——validator（ScheduleSemanticValidator/clip_emotional_tone）、
response_parser（LLMResponseParser）、quality_scorer（ScheduleQualityScorer）三个模块
整文件删除，``PromptBuilder``/``SchemaBuilder`` 不再经本包导出（base_generator 直连
子模块）。本包仅保留 spine 生成链与共享组件。

组件清单：
- base_generator: 基础协调器（spine Prompt / Schema / Context / 时区）
- prompt_builder: spine 日意图提示词构建（LOW_MOOD_MIN_DAYS 在此定义并导出）
- schema_builder: spine 日意图 JSON Schema
- context_loader: 历史上下文加载（ctx.chat / ctx.message / ctx.knowledge）
- config: 生成器配置管理
"""

from .base_generator import BaseScheduleGenerator
from .prompt_builder import LOW_MOOD_MIN_DAYS
from .context_loader import ScheduleContextLoader
from .config import ScheduleGeneratorConfig

__all__ = [
    "BaseScheduleGenerator",
    "ScheduleContextLoader",
    "LOW_MOOD_MIN_DAYS",
    "ScheduleGeneratorConfig",
]
