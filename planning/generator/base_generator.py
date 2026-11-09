"""Base Generator Module（来源：自主规划插件，改造版）。

职责：
    - 组件协调（PromptBuilder / SchemaBuilder / ScheduleContextLoader / TimezoneManager）

改造点：goal_manager 可为 None（无库场景下 PromptBuilder 仍可工作）。

PR8 3.0.0 清退（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP4）：
删旧 daily 面三个透传（``build_json_schema``/``build_schedule_prompt``/
``build_retry_prompt``）与零调用的 ``get_model_config``（C-19）；spine 两透传与
context 装载保留。
"""

from typing import Any, Dict, List, Optional
import logging

from ..time_utils import TimezoneManager
from ..goal_manager import GoalManager
from .context_loader import ScheduleContextLoader
from .prompt_builder import PromptBuilder
from .schema_builder import SchemaBuilder

logger = logging.getLogger(__name__)


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
        #: 节假日上下文（2026-10-10：只有 [schedule] holiday_affects_schedule 开启时由
        #: ScheduleGenerator 装载；空串=不注入，行为与旧版逐字一致）
        self.holiday_context: str = ""
        #: PR-B1（2026-11-07）：近期真实经历摘要行（经历索引有界只读端口装载；
        #: 空串=不注入，行为与旧版逐字一致——失败/为空/开关关闭一律空降级）
        self.experience_context: str = ""
        #: PR-B2（2026-11-07）：≤4 条持续动机摘要行（MotiveStore 有界只读端口装载；
        #: 空串=不注入，行为与旧版逐字一致——失败/为空/开关关闭一律空降级）
        self.motive_context: str = ""
        #: P3（2026-11-09 §14.8）：稳定口味摘要（偏好服务只读端口装载；空串=不注入）
        self.preference_context: str = ""
        self.config: Dict[str, Any] = config or {}  # 保存配置

        # 初始化时区管理器
        timezone_str = self.config.get("timezone", "Asia/Shanghai")
        self.tz_manager = TimezoneManager(timezone_str)

        # 初始化子组件（依赖注入）
        self.prompt_builder = PromptBuilder(self.config, self.tz_manager)
        self.schema_builder = SchemaBuilder(self.config)
        self.context_loader = ScheduleContextLoader(goal_manager, self.tz_manager)

    # ========================================================================
    # 委托方法（薄包装，子组件承担实际逻辑；PR8 3.0.0 起仅存 spine 两透传）
    # ========================================================================

    def build_spine_schema(self, min_intentions: int = 3, max_intentions: int = 6) -> dict:
        """构建 spine 日意图精简 Schema（委托给 SchemaBuilder；2026-10-25 滚动化改造新增）。"""
        return self.schema_builder.build_spine_schema(min_intentions, max_intentions)

    def build_spine_prompt(
        self,
        schema: Optional[Dict] = None,
        *,
        min_intentions: int = 3,
        max_intentions: int = 6,
        max_described: int = 1,
        no_sleep_mode: bool = False,
    ) -> str:
        """构建 spine 日意图提示词（委托给 PromptBuilder，转发既有上下文槽位；
        2026-10-25 滚动化改造新增，R7 依据 = 用户 2026-10-25 直派）。"""
        return self.prompt_builder.build_spine_prompt(
            schema,
            min_intentions=min_intentions,
            max_intentions=max_intentions,
            max_described=max_described,
            no_sleep_mode=no_sleep_mode,
            yesterday_context=self.yesterday_schedule_summary,
            pending_commitments=self.pending_commitments,
            history_context=self.history_context,
            knowledge_context=self.knowledge_context,
            low_mood_days=self.low_mood_days,
            holiday_context=self.holiday_context,
            experience_context=self.experience_context,
            motive_context=self.motive_context,
            preference_context=self.preference_context,
        )
