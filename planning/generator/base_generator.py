"""Base Generator Module（来源：自主规划插件，改造版）。

职责：
    - 模型任务名解析（供 ScheduleGenerator 经 core.llm_utils.LLMService 调用时使用）
    - 组件协调（PromptBuilder / SchemaBuilder / ScheduleContextLoader / TimezoneManager）

改造点：goal_manager 可为 None（无库场景下 PromptBuilder 仍可工作）。
"""

from typing import Any, Dict, List, Optional, Tuple
import logging

from ..time_utils import TimezoneManager
from ..goal_manager import GoalManager
from .context_loader import ScheduleContextLoader
from .prompt_builder import PromptBuilder
from .schema_builder import SchemaBuilder

logger = logging.getLogger(__name__)

# 默认模型任务名（需在主程序 model_config.toml 中预先配置）
DEFAULT_LLM_TASK_NAME = "replyer"
DEFAULT_TEMPERATURE = 0.85  # 提到 0.85 让日程描述更有变化（schema 校验兜底防跑偏）


class BaseScheduleGenerator:
    """基础日程生成器：模型任务名 + 组件协调。

    各子组件通过依赖注入装配，``ScheduleGenerator`` 继承此类获得 Prompt / Schema /
    Context 构建能力。
    """

    def __init__(self, goal_manager: Optional[GoalManager], config: Optional[Dict[str, Any]] = None) -> None:
        self.goal_manager = goal_manager
        self.yesterday_schedule_summary: Optional[str] = None  # 近几天日程摘要（用于上下文）
        # 动态上下文（由 ScheduleGenerator 在生成前异步注入）
        self.pending_commitments: Optional[List[Dict[str, Any]]] = None
        self.history_context: str = ""
        self.knowledge_context: str = ""
        #: M5（§6.2 F）睡醒情绪描述 + 引导约束（ScheduleGenerator 按 schedule.emotion_influence 装载；空串=不注入）
        self.emotion_context: str = ""
        #: M5（§6.2 F）连续低迷天数（ScheduleGenerator 从情绪侧 low_mood_state() 装载；≥3 触发软约束）
        self.low_mood_days: int = 0
        self.config: Dict[str, Any] = config or {}  # 保存配置

        # 初始化时区管理器
        timezone_str = self.config.get("timezone", "Asia/Shanghai")
        self.tz_manager = TimezoneManager(timezone_str)

        # 初始化子组件（依赖注入）
        self.prompt_builder = PromptBuilder(self.config, self.tz_manager)
        self.schema_builder = SchemaBuilder(self.config)
        self.context_loader = ScheduleContextLoader(goal_manager, self.tz_manager)

    # ========================================================================
    # 模型配置
    # ========================================================================

    def get_model_config(self) -> Tuple[str, int, float]:
        """获取 LLM 调用参数 ``(task_name, max_tokens, temperature)``。"""
        task_name = str(self.config.get("llm_task_name", DEFAULT_LLM_TASK_NAME)).strip() or DEFAULT_LLM_TASK_NAME
        max_tokens = int(self.config.get("max_tokens", 8192))
        temperature = float(self.config.get("temperature", DEFAULT_TEMPERATURE))
        logger.debug("LLM 任务名: %s (max_tokens=%s, temperature=%s)", task_name, max_tokens, temperature)
        return task_name, max_tokens, temperature

    # ========================================================================
    # 委托方法（薄包装，子组件承担实际逻辑）
    # ========================================================================

    def build_json_schema(self) -> dict:
        """构建 JSON Schema（委托给 SchemaBuilder）。"""
        return self.schema_builder.build_json_schema()

    def build_schedule_prompt(
        self,
        schedule_type: Any,
        preferences: Dict[str, Any],
        schema: Optional[Dict] = None,
    ) -> str:
        """构建日程生成提示词（委托给 PromptBuilder）。"""
        return self.prompt_builder.build_schedule_prompt(
            schedule_type,
            preferences,
            schema,
            self.yesterday_schedule_summary,
            pending_commitments=self.pending_commitments,
            history_context=self.history_context,
            knowledge_context=self.knowledge_context,
            emotion_context=self.emotion_context,
            low_mood_days=self.low_mood_days,
        )

    def build_retry_prompt(
        self,
        schedule_type: Any,
        preferences: Dict[str, Any],
        schema: Dict,
        previous_issues: List[str],
    ) -> str:
        """构建第二轮 prompt（委托给 PromptBuilder）。"""
        return self.prompt_builder.build_retry_prompt(
            schedule_type,
            preferences,
            schema,
            previous_issues,
            self.yesterday_schedule_summary,
            pending_commitments=self.pending_commitments,
            history_context=self.history_context,
            knowledge_context=self.knowledge_context,
            emotion_context=self.emotion_context,
            low_mood_days=self.low_mood_days,
        )
