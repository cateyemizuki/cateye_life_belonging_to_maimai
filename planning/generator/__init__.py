"""Generator 子包 —— 日程生成组件（来源：自主规划插件 planner/generator/）。

组件清单：
- base_generator: 基础协调器（Prompt / Schema / Context / 时区）
- prompt_builder: 提示词构建（wake/sleep 锚点动态 JSON 示例 + 衔接演算）
- schema_builder: JSON Schema 定义
- context_loader: 历史上下文加载（ctx.chat / ctx.message / ctx.knowledge）
- validator: 语义验证（三餐 / 作息 / 时间连续性）+ emotional_tone 裁剪（M5）
- response_parser: LLM 响应解析与清理（含控制字符清理）
- quality_scorer: 日程质量评分
- config: 生成器配置管理
"""

from .base_generator import BaseScheduleGenerator
from .prompt_builder import LOW_MOOD_MIN_DAYS, PromptBuilder, build_emotion_context
from .schema_builder import SchemaBuilder
from .context_loader import ScheduleContextLoader
from .validator import ScheduleSemanticValidator, clip_emotional_tone
from .response_parser import LLMResponseParser
from .quality_scorer import ScheduleQualityScorer
from .config import ScheduleGeneratorConfig

__all__ = [
    "BaseScheduleGenerator",
    "PromptBuilder",
    "SchemaBuilder",
    "ScheduleContextLoader",
    "ScheduleSemanticValidator",
    "clip_emotional_tone",
    "build_emotion_context",
    "LOW_MOOD_MIN_DAYS",
    "LLMResponseParser",
    "ScheduleQualityScorer",
    "ScheduleGeneratorConfig",
]
