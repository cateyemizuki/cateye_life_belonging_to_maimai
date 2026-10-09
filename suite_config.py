"""猫眼生活套件（cateye_test）插件 — 顶层配置聚合。

- 文件名刻意不叫 ``config.py``：宿主 Runner 加载插件时 sys.path 含宿主 ``src``，
  且 cateye_harness 与本插件同进程加载，顶层 ``config`` 会被宿主 ``src/config`` 或
  兄弟插件抢占（L1 实证），故改名并由 plugin.py 顶部引导头接管自身模块解析。
- 各业务子模块各自拥有自己的配置 section 类（模块化维护）：
  - planning/config.py  日程系统：schedule / schedule_advanced / planning_proactive
  - companion/config.py 陪伴模块：patrol（含目标用户与屏幕感知）/ affection
  - suite_prompts.py    提示词集中模块：prompts
  - qzone/config.py     QQ 空间：qzone
  - emotion/config.py   情绪系统：emotion
- 顶层只保留跨模块共享的基础段（plugin / admin）与新增的 LLM 调用段（llm），
  并把各子模块 section 扁平挂载为聚合模型的顶层字段（嵌套段 WebUI 不渲染）。

## 2026-10-10 配置整理轮（本次改动的总说明）

**段布局变化**：原 [target] 并入 [patrol]（陪伴模块）、原 [cateye] 并入 [patrol]、
原 [judge] 并入新增 [llm]、原 [inject] 与 [planning_cleanup] 并入新增 [schedule_advanced]、
提示词集中进新增 [prompts]。段名保持不变的段（plugin/patrol/affection/schedule/
planning_proactive/admin/qzone/emotion）里的同名键，值会照旧保留。

**为什么不递增 config_version**（重要，别乱改）：宿主 Runner 在「配置版本号变化」时会
**先**用最新默认结构重建设置文件（只保留新旧同名的键），之后才把结果交给插件模型校验——
一旦递增，跨段搬迁（如 target_qq → patrol.target_qq）与所有改名键的用户值都会在插件
看到它们之前被丢弃。因此本轮的旧键迁移放在 :class:`CateyeSuiteConfig` 的
``mode="before"`` 校验器里（版本号不变 → 宿主把原始配置交给插件 → 插件搬迁 → 落盘时
旧键自然消失），:data:`SUPPORTED_CONFIG_VERSION` 保持不变是有意为之。

**迁移映射**（旧 → 新，全部在 :meth:`CateyeSuiteConfig._migrate_legacy_sections`）：
- ``[target].target_qq`` → ``[patrol].target_qq``；
- ``[cateye].*`` → ``[patrol].screen_*`` / ``[llm].vlm_task`` / ``[prompts].screen_*``；
- ``[judge].*`` → ``[llm].judge_*``；
- ``[schedule].llm_task_name`` → ``[llm].task_name``；
- ``[affection].review_task_name`` → ``[llm].review_task_name``；
- ``[emotion].evaluator_task_name / referee_task_name`` → ``[llm].*``；
- ``[qzone].llm_task_name`` → ``[llm].qzone_task_name``；
- ``[inject].*`` / ``[planning_cleanup].*`` → ``[schedule_advanced].*``；
- ``[schedule]`` 的 23 个深水字段（timezone / max_tokens / generation_timeout / use_multi_round /
  max_rounds / quality_threshold / recent_schedule_days / history_message_limit / knowledge_search_limit /
  cross_day_activity / min_description_length / max_description_length / emotion_influence / lie_in_max_until /
  list_draw_image / image_timeout_seconds / role_judge_enabled / role_judge_temperature / inject_cache_minutes /
  cache_ttl / cache_max_size / max_future_activities / auto_generate_time）→ ``[schedule_advanced]``（键名不变）。
"""

from __future__ import annotations

from typing import ClassVar, Dict, List

import pydantic
from maibot_sdk import Field, PluginConfigBase

from companion.config import (
    AffectionSectionConfig,
    PatrolSectionConfig,
    migrate_legacy_cateye,
    migrate_legacy_patrol,
)
from emotion.config import EmotionSectionConfig
from planning.config import (
    PlanningProactiveSectionConfig,
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
)
from qzone.config import QzoneSectionConfig
from suite_prompts import PromptsSectionConfig

# 与 _manifest.json 的 version 保持同步。
# 注意：**不要**因为配置结构调整而递增本值——宿主会在版本变化时先重建再交给插件，
# 迁移逻辑（见模块 docstring）将失效。递增时机 = 本次结构调整被用户确认并落盘之后。
SUPPORTED_CONFIG_VERSION = "0.2.0"

#: 本轮（2026-10-10 配置整理轮）的段布局标记；写入 [plugin] layout_version 用于迁移幂等
CURRENT_LAYOUT_VERSION = "2026-10-10"

#: 2026-10-10 段拆分：从 [schedule] 迁往 [schedule_advanced] 的字段（键名不变，仅换段）
_SCHEDULE_TO_ADVANCED_KEYS = (
    "timezone", "max_tokens", "generation_timeout", "use_multi_round", "max_rounds",
    "quality_threshold", "recent_schedule_days", "history_message_limit", "knowledge_search_limit",
    "cross_day_activity", "min_description_length", "max_description_length", "emotion_influence",
    "lie_in_max_until", "list_draw_image", "image_timeout_seconds", "role_judge_enabled",
    "role_judge_temperature", "inject_cache_minutes", "cache_ttl", "cache_max_size",
    "max_future_activities", "auto_generate_time",
)


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关 = 全部模块停摆（含命令与工具）",
            **_ui_i18n("Enable plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "label": "配置版本",
            "hidden": True,
            "disabled": True,
            **_ui_i18n("Config version"),
        },
    )
    layout_version: str = Field(
        default="",
        description="段布局标记（2026-10-10 配置整理轮的迁移幂等用；用户不要改）",
        json_schema_extra={
            "label": "布局版本",
            "hidden": True,
            **_ui_i18n("Layout version"),
        },
    )


class LlmSectionConfig(PluginConfigBase):
    """LLM 调用集中段（2026-10-10 新增）。

    插件里**所有**模型调用都经 ``core/llm_utils.LLMService`` 这一个模块发出；
    本段集中管理它们的模型路由与判定参数（原先散在 schedule/judge/affection/emotion/qzone/cateye 六处）。
    任务名 = 宿主 ``model_config.toml`` 里的任务键（如 utils / replyer / planner）或具体模型名。
    """

    __ui_label__ = "LLM 调用"
    __ui_icon__ = "cpu"
    __ui_order__ = 6
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "LLM Calls",
               "description": "Model routing for every LLM call the plugin makes (task names and judge parameters)."}
    }

    task_name: str = Field(
        default="replyer",
        description="主任务名：日程生成、工具发言等「生成类」调用默认用它（宿主模型任务键，如 replyer/utils/planner）",
        json_schema_extra={
            "label": "主任务名",
            "hint": "决定 bot「说人话」的能力；换模型后行为变化最明显的就是这一项",
            **_ui_i18n("Main task", "Task used by generation-style calls."),
        },
    )
    judge_model: str = Field(
        default="",
        description="判断用模型：填宿主任务名或具体模型名；留空 = 跟随「主任务名」。"
                    "判定类调用只需要明确结论（如 Y/N）",
        json_schema_extra={
            "label": "判断用模型",
            "hint": "建议指到便宜、非思考型模型；用思考型模型时请把「判断最大 token」保持 512 以上",
            **_ui_i18n("Judge model", "Empty = follow the main task."),
        },
    )
    judge_max_tokens: int = Field(
        default=512,
        ge=16,
        le=8192,
        description="判断调用的最大生成 token 数。思考型模型会先消耗 token 输出推理内容，"
                    "预算太小时正文为空、判定按失败（驳回）处理",
        json_schema_extra={
            "label": "判断最大 token",
            "hint": "默认 512；判定被大量驳回时可以调大",
            **_ui_i18n("Judge max tokens"),
        },
    )
    judge_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="判断调用的采样温度：越低结论越稳定（判定不需要创意）",
        json_schema_extra={
            "label": "判断温度",
            "step": 0.1,
            "hint": "默认 0.2，一般不用动",
            **_ui_i18n("Judge temperature"),
        },
    )
    review_task_name: str = Field(
        default="utils",
        description="好感裁判任务名：会话采样评估与关系日结使用（建议便宜、非思考型）",
        json_schema_extra={
            "label": "好感裁判任务名",
            **_ui_i18n("Affection review task"),
        },
    )
    emotion_referee_task_name: str = Field(
        default="replyer",
        description="情绪睡眠裁判任务名：睡眠期情绪大修使用（需要理解人设与日程，建议与主任务同档）",
        json_schema_extra={
            "label": "情绪裁判任务名",
            **_ui_i18n("Emotion referee task"),
        },
    )
    emotion_evaluator_task_name: str = Field(
        default="utils",
        description="情绪评估任务名：回复/事件的 ΔVAD 增量评估使用（调用频繁，建议便宜、非思考型）",
        json_schema_extra={
            "label": "情绪评估任务名",
            **_ui_i18n("Emotion evaluator task"),
        },
    )
    qzone_task_name: str = Field(
        default="utils",
        description="QQ 空间任务名：说说文案与评论回复使用",
        json_schema_extra={
            "label": "空间任务名",
            **_ui_i18n("Qzone task"),
        },
    )
    vlm_task: str = Field(
        default="vlm",
        description="视觉任务名：屏幕截图理解使用（必须是支持图片输入的视觉模型任务）",
        json_schema_extra={
            "label": "视觉任务名",
            "hint": "指错成纯文本模型时屏幕感知只会返回「看不清」",
            **_ui_i18n("Vision task"),
        },
    )


class AdminSectionConfig(PluginConfigBase):
    """管理与日志（跨模块共享）。"""

    __ui_label__ = "管理与日志"
    __ui_icon__ = "shield"
    __ui_order__ = 9
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Admin & Logs", "description": "Command permissions and diagnostic logs."}
    }

    admin_users: List[str] = Field(
        default_factory=list,
        description="命令管理员（QQ 号或 qq:123456 形式）；留空 = 仅本机控制台可用管理子命令",
        json_schema_extra={"label": "管理员", **_ui_i18n("Admin users")},
    )
    allowed_streams: List[str] = Field(
        default_factory=list,
        description="命令/注入允许的会话（session_id）；留空 = 不限制",
        json_schema_extra={"label": "允许会话", **_ui_i18n("Allowed streams")},
    )
    llm_log_enabled: bool = Field(
        default=True,
        description="记录 LLM 调用日志（JSONL，按天分文件）",
        json_schema_extra={
            "label": "LLM 日志",
            "hint": "排查「模型答非所问」时必看；按天滚动、默认保留 3 天",
            **_ui_i18n("LLM log"),
        },
    )
    decision_log_enabled: bool = Field(
        default=True,
        description="记录巡检/空间/规划决策日志（JSONL，按天分文件）",
        json_schema_extra={
            "label": "决策日志",
            "hint": "排查「为什么没主动说话」时必看",
            **_ui_i18n("Decision log"),
        },
    )
    log_retention_days: int = Field(
        default=3,
        ge=1,
        le=30,
        description="日志保留天数",
        json_schema_extra={
            "label": "日志保留天数",
            "hint": "超过天数的日志自动清理（1-30 天）",
            **_ui_i18n("Log retention (days)"),
        },
    )


class CateyeSuiteConfig(PluginConfigBase):
    """插件完整配置。

    注意：子分组必须扁平挂为顶层字段（嵌套段 WebUI 不渲染）。
    各 section 类定义在各子模块的 config.py / suite_prompts.py 中（模块化维护）。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    # 陪伴模块（companion/config.py；含目标用户与屏幕感知）
    patrol: PatrolSectionConfig = Field(default_factory=PatrolSectionConfig)
    affection: AffectionSectionConfig = Field(default_factory=AffectionSectionConfig)
    # 日程（planning/config.py）
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    schedule_advanced: ScheduleAdvancedSectionConfig = Field(default_factory=ScheduleAdvancedSectionConfig)
    planning_proactive: PlanningProactiveSectionConfig = Field(default_factory=PlanningProactiveSectionConfig)
    # LLM 调用集中段（本文件）与提示词集中段（suite_prompts.py）
    llm: LlmSectionConfig = Field(default_factory=LlmSectionConfig)
    prompts: PromptsSectionConfig = Field(default_factory=PromptsSectionConfig)
    # 管理与日志（本文件）
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    # QQ 空间（qzone/config.py）
    qzone: QzoneSectionConfig = Field(default_factory=QzoneSectionConfig)
    # 情绪系统（emotion/config.py）
    emotion: EmotionSectionConfig = Field(default_factory=EmotionSectionConfig)

    @pydantic.model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sections(cls, data: object) -> object:
        """跨段旧键迁移（2026-10-10 配置整理轮；**一次性、幂等**）。

        宿主在版本号不变时把原始配置（含已删段）交到这里，本校验器负责：
        1. [target] / [cateye] / [judge] / [inject] / [planning_cleanup] 五个已删段的
           字段搬进新家（搬完即从字典里 pop，落盘/校验都不再看到它们）；
        2. 各段内部改名键的搬运（llm_task_name→[llm].task_name 等）；
        3. [patrol] 段内部已删键的清理与阈值合并（companion.config.migrate_legacy_patrol）。

        **幂等**：迁移完成时在 ``[plugin] layout_version`` 写下本轮布局标记；下一次校验
        （迁移结果被宿主落盘后再读回）看到该标记即跳过全部值搬运——新旧同名的键
        （如 ``proactive_group_ids``）不会被二次解释。本校验器还会给
        ``[planning_proactive]`` 放一枚 ``_layout_migrated`` 哨兵，通知段级校验器是否跳过
        它自己的旧名单映射（见 PlanningProactiveSectionConfig._migrate_legacy_lists）。

        缺陷史（2026-10-10 离线自检抓到）：无标记版迁移在"落盘 → 再读回"时会把新白名单
        当成旧白名单再映射一遍（群聊名单在黑名单模式下被误转白名单）→ 非幂等，故加标记。
        非 dict 输入（模型实例 / None）原样放行。
        """
        if not isinstance(data, dict):
            return data
        migrated = dict(data)
        plugin_section = dict(migrated.get("plugin") or {})
        already_migrated = str(plugin_section.get("layout_version") or "") == CURRENT_LAYOUT_VERSION

        # [planning_proactive] 哨兵：True = 已迁移形态（段级校验器跳过旧名单映射）
        proactive_data = dict(migrated.get("planning_proactive") or {})
        proactive_data["_layout_migrated"] = already_migrated
        migrated["planning_proactive"] = proactive_data

        if already_migrated:
            # 已迁移：只清掉理论上不该再出现的旧段（用户手工加回时忽略不炸），不做任何值搬运
            for stale in ("target", "cateye", "judge", "inject", "planning_cleanup"):
                migrated.pop(stale, None)
            migrated["plugin"] = plugin_section
            return migrated

        # 1) [target] → [patrol].target_qq（其余旧键丢弃）
        legacy_target = migrated.pop("target", None)
        patrol_data = migrate_legacy_patrol(migrated.get("patrol") or {}, legacy_target)
        migrated["patrol"] = patrol_data

        # 2) [cateye] → [patrol].screen_* / [llm].vlm_task / [prompts].screen_*
        cateye_migrated = migrate_legacy_cateye(migrated.pop("cateye", None))
        if cateye_migrated["patrol"]:
            for key, value in cateye_migrated["patrol"].items():
                migrated["patrol"].setdefault(key, value)
        llm_data = dict(migrated.get("llm") or {})
        if cateye_migrated["llm"].get("vlm_task"):
            llm_data.setdefault("vlm_task", cateye_migrated["llm"]["vlm_task"])
        prompts_data = dict(migrated.get("prompts") or {})
        for key, value in cateye_migrated["prompts"].items():
            prompts_data.setdefault(key, value)

        # 3) [judge] → [llm].judge_*
        judge_data = migrated.pop("judge", None)
        if isinstance(judge_data, dict):
            for old, new in (("model", "judge_model"), ("max_tokens", "judge_max_tokens"),
                             ("temperature", "judge_temperature")):
                if old in judge_data:
                    llm_data.setdefault(new, judge_data.get(old))

        # 4) 各段任务名 → [llm]
        schedule_data = dict(migrated.get("schedule") or {})
        if schedule_data.get("llm_task_name"):
            llm_data.setdefault("task_name", schedule_data.pop("llm_task_name"))
        else:
            schedule_data.pop("llm_task_name", None)
        affection_data = dict(migrated.get("affection") or {})
        if affection_data.get("review_task_name"):
            llm_data.setdefault("review_task_name", affection_data.pop("review_task_name"))
        else:
            affection_data.pop("review_task_name", None)
        emotion_data = dict(migrated.get("emotion") or {})
        if emotion_data.get("referee_task_name"):
            llm_data.setdefault("emotion_referee_task_name", emotion_data.pop("referee_task_name"))
        else:
            emotion_data.pop("referee_task_name", None)
        if emotion_data.get("evaluator_task_name"):
            llm_data.setdefault("emotion_evaluator_task_name", emotion_data.pop("evaluator_task_name"))
        else:
            emotion_data.pop("evaluator_task_name", None)
        qzone_data = dict(migrated.get("qzone") or {})
        if qzone_data.get("llm_task_name"):
            llm_data.setdefault("qzone_task_name", qzone_data.pop("llm_task_name"))
        else:
            qzone_data.pop("llm_task_name", None)

        # 5) [schedule] 深水字段 → [schedule_advanced]（键名不变；2026-10-10 段拆分）
        advanced_data = dict(migrated.get("schedule_advanced") or {})
        for key in _SCHEDULE_TO_ADVANCED_KEYS:
            if key in schedule_data:
                advanced_data.setdefault(key, schedule_data.pop(key))

        # 6) [inject] / [planning_cleanup] → [schedule_advanced]（键名不变）
        for legacy_section in ("inject", "planning_cleanup"):
            legacy = migrated.pop(legacy_section, None)
            if isinstance(legacy, dict):
                for key, value in legacy.items():
                    advanced_data.setdefault(key, value)

        # 迁移完成：写下本轮布局标记（下次校验据此跳过值搬运，保证幂等）
        plugin_section["layout_version"] = CURRENT_LAYOUT_VERSION
        migrated["plugin"] = plugin_section

        migrated["llm"] = llm_data
        migrated["prompts"] = prompts_data
        migrated["schedule"] = schedule_data
        migrated["affection"] = affection_data
        migrated["emotion"] = emotion_data
        migrated["qzone"] = qzone_data
        migrated["schedule_advanced"] = advanced_data
        return migrated
