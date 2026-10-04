"""日程规划系统配置 section（来源：自主规划插件 config_models.py [schedule]/[inject]/[proactive]/[autonomous_planning]）。

本模块只定义 section 类（PluginConfigBase 子类），不定义聚合模型；
顶层聚合由整合者在 config.py 中挂载（见 INTEGRATION.md 的推荐挂载名）。

WebUI 元数据约定（与顶层 config.py 一致）：
- 每个字段带中文短标题 label（缺省回退显示英文字段名）；
- 每个字段带英文 i18n 翻译（zh-CN 文案即中文字段本身，无需重复）；
- description 写完整中文说明，作为界面未命中翻译时的回退文案。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal

from maibot_sdk import Field, PluginConfigBase


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class ScheduleSectionConfig(PluginConfigBase):
    """日程系统（来源：自主规划插件，v4.9.1 全量字段）。"""

    __ui_label__ = "日程系统"
    __ui_icon__ = "calendar"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule", "description": "LLM-generated daily routine, injection, proactive acts and queries."}
    }

    # ── 模块独立总开关 ───────────────────────────────────────
    enabled: bool = Field(
        default=True,
        description="日程规划模块总开关：关闭后停止日程生成/注入/主动行为（工具与命令返回未启用提示）",
        json_schema_extra={
            "label": "启用日程模块",
            "hint": "每个业务模块都有独立总开关；关闭不影响其他模块",
            **_ui_i18n("Enable schedule module", "Master switch of the schedule module."),
        },
    )

    # ── 作息锚点 ─────────────────────────────────────────────
    wake_time: str = Field(
        default="07:00",
        description="起床时间锚点（HH:MM），清醒时段的活动从该时间开始安排；入睡早于起床表示跨午夜夜猫子作息",
        json_schema_extra={
            "label": "起床时间",
            "hint": "HH:MM；清醒活动从这一刻开始排（默认 07:00）",
            "pattern": r"^\d{2}:\d{2}$",
            **_ui_i18n("Wake time", "Wake-up anchor (HH:MM); awake activities start here."),
        },
    )
    sleep_time: str = Field(
        default="23:00",
        description="入睡时间锚点（HH:MM），这一刻之后到次日起床是睡眠时段；无睡眠模式下该时段不生成日程",
        json_schema_extra={
            "label": "入睡时间",
            "hint": "HH:MM；这一刻之后进入睡眠时段（默认 23:00）",
            "pattern": r"^\d{2}:\d{2}$",
            **_ui_i18n("Sleep time", "Bedtime anchor (HH:MM)."),
        },
    )
    no_sleep_mode: bool = Field(
        default=False,
        description="无睡眠模式。开启后入睡到起床的时段不生成任何日程：提示词要求 LLM 不在该时段安排活动，"
                    "生成后代码再销毁该时段内残留的日程（与时段边界重叠的活动截断到边界），"
                    "且睡眠时段内主动行为（活动切换发起 / 早间问好）不会触发。适合不需要睡眠的角色设定。",
        json_schema_extra={
            "label": "无睡眠模式",
            "hint": "开启后睡眠时段不生成任何日程（生成后自动销毁该时段日程）",
            **_ui_i18n("No-sleep mode", "No schedule entries inside the sleep window when enabled."),
        },
    )

    # ── 定时生成 ─────────────────────────────────────────────
    auto_generate_enabled: bool = Field(
        default=True,
        description="每天到点自动用 LLM 生成当日日程（当天已有日程则跳过）",
        json_schema_extra={"label": "自动生成", **_ui_i18n("Auto generate")},
    )
    auto_generate_time: str = Field(
        default="00:30",
        description="每日自动生成日程的时刻（HH:MM），推荐凌晨低峰期",
        json_schema_extra={"label": "生成时刻", "placeholder": "00:30", **_ui_i18n("Generate at")},
    )

    # ── 生成参数 ─────────────────────────────────────────────
    min_activities: int = Field(
        default=6,
        ge=1,
        le=20,
        description="生成的日程最少活动数",
        json_schema_extra={"label": "最少活动数", **_ui_i18n("Min activities")},
    )
    max_activities: int = Field(
        default=12,
        ge=1,
        le=25,
        description="生成的日程最多活动数",
        json_schema_extra={"label": "最多活动数", **_ui_i18n("Max activities")},
    )
    custom_prompt: str = Field(
        default="",
        description="自定义日程生成提示词。语义：角色的长期生活状态/持续阶段（如「正在环游世界」「备考研究生」），"
                    "日程生成会延续这一方向（留空使用默认风格）",
        json_schema_extra={
            "label": "当前生活阶段 / 长期状态",
            "hint": "留空使用默认；如「正在环游世界」「备考研究生」",
            "rows": 3,
            "placeholder": "（留空使用默认风格）",
            **_ui_i18n("Life stage / long-term state", "Persistent life-stage hint merged into the generation prompt."),
        },
    )
    use_multi_round: bool = Field(
        default=True,
        description="启用多轮生成（首轮质量不达标时按反馈重试，提升日程质量）",
        json_schema_extra={"label": "多轮生成", **_ui_i18n("Multi-round generation")},
    )
    max_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="多轮生成最大轮数（1-5，推荐 2；越多越费 token）",
        json_schema_extra={"label": "最大轮数", **_ui_i18n("Max rounds")},
    )
    quality_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="多轮生成质量阈值（达到此分即提前停止重试），0.80-0.90 推荐 0.85",
        json_schema_extra={"label": "质量阈值", "step": 0.05, **_ui_i18n("Quality threshold")},
    )
    enable_detailed_description: bool = Field(
        default=True,
        description="启用详细活动描述（关闭后生成、注入、命令展示都不显示长描述）",
        json_schema_extra={"label": "详细描述", **_ui_i18n("Detailed description")},
    )
    min_description_length: int = Field(
        default=20,
        ge=5,
        description="活动描述最小字符数（20-30 较合理，过短易模糊）",
        json_schema_extra={"label": "描述最少字数", **_ui_i18n("Min description length")},
    )
    max_description_length: int = Field(
        default=50,
        ge=5,
        description="活动描述最大字符数（50-100 较合理，过长占 token）",
        json_schema_extra={"label": "描述最多字数", **_ui_i18n("Max description length")},
    )
    max_tokens: int = Field(
        default=8192,
        ge=1000,
        description="LLM 生成日程的最大 token 数（过小可能截断日程）",
        json_schema_extra={"label": "最大 token", **_ui_i18n("Max tokens")},
    )
    recent_schedule_days: int = Field(
        default=3,
        ge=1,
        le=14,
        description="生成新日程时回看最近 N 天的日程作为去重参考（N=1 只看昨天；3-5 防交替式重复）",
        json_schema_extra={"label": "回看天数", **_ui_i18n("Recent days lookback")},
    )

    # ── 跨会话上下文 ─────────────────────────────────────────
    history_message_limit: int = Field(
        default=30,
        ge=0,
        description="生成日程时回读各会话当天最近 N 条聊天消息作为上下文。0 = 禁用",
        json_schema_extra={"label": "历史消息条数", **_ui_i18n("History message limit")},
    )
    knowledge_search_limit: int = Field(
        default=5,
        ge=0,
        description="生成日程时检索知识库的最大条数。0 = 禁用",
        json_schema_extra={"label": "知识库条数", **_ui_i18n("Knowledge search limit")},
    )
    cross_day_activity: bool = Field(
        default=True,
        description="启用跨天活动（time_window 跨夜的写法 + 注入时回读昨日跨夜活动）",
        json_schema_extra={"label": "跨天活动", **_ui_i18n("Cross-day activity")},
    )

    # ── LLM 任务 ────────────────────────────────────────────
    llm_task_name: str = Field(
        default="replyer",
        description="日程生成使用的模型任务名（utils/replyer/planner 等，走宿主任务路由）",
        json_schema_extra={"label": "生成任务名", **_ui_i18n("LLM task")},
    )
    generation_timeout: float = Field(
        default=180.0,
        ge=10.0,
        description="日程生成 LLM 调用超时（秒），推荐 120-300；会换算为 timeout_ms 传给宿主避免被默认 30s 截断",
        json_schema_extra={"label": "生成超时(秒)", **_ui_i18n("Generation timeout (s)")},
    )

    # ── 角色裁判 ────────────────────────────────────────────
    role_judge_enabled: bool = Field(
        default=True,
        description="角色裁判模式：用户通过 cateye_test_update_schedule_v4 提出自然语言日程请求时，LLM 扮演 bot 判断接不接——"
                    "today=接受并调整今天日程；future=接受未来预约（写入候选清单，到点主动兑现）；"
                    "reject=拒绝（日程不变）。判定失败安全降级为日程不变。关闭后所有请求一律记为未来约定",
        json_schema_extra={
            "label": "角色裁判模式",
            "hint": "today 改今天 / future 记预约 / reject 拒绝；关闭后一律记为预约",
            **_ui_i18n("Role judge", "LLM arbitrates user schedule requests (today/future/reject)."),
        },
    )
    role_judge_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="角色裁判 LLM 温度。越低越保守（更倾向拒绝离谱请求）",
        json_schema_extra={"label": "裁判温度", "step": 0.1, **_ui_i18n("Judge temperature")},
    )

    # ── 注入 ────────────────────────────────────────────────
    inject_into_planner: bool = Field(
        default=True,
        description="在 planner 决策时把当前活动注入 items 上下文（SystemMessageItem 快照协议）",
        json_schema_extra={"label": "注入 planner", **_ui_i18n("Inject into planner")},
    )
    inject_into_replyer: bool = Field(
        default=True,
        description="在 replyer 调 LLM 前把当前活动作为 extra_prompt 注入；与 planner 注入共享冷却，不会两阶段连刷",
        json_schema_extra={"label": "注入 replyer", **_ui_i18n("Inject into replyer")},
    )
    inject_scope: Literal["target_private", "all"] = Field(
        default="target_private",
        description="注入范围：target_private=只注入目标私聊；all=所有会话",
        json_schema_extra={"label": "注入范围", **_ui_i18n("Injection scope")},
    )
    max_future_activities: int = Field(
        default=3,
        ge=0,
        description="智能注入时最多显示的未来活动数量。0 = 不显示",
        json_schema_extra={"label": "未来活动条数", **_ui_i18n("Future activities shown")},
    )

    # ── 缓存 ────────────────────────────────────────────────
    inject_cache_minutes: int = Field(
        default=15,
        ge=1,
        le=60,
        description="当前活动查询的时间窗口缓存（分钟）",
        json_schema_extra={"label": "活动缓存(分钟)", **_ui_i18n("Activity cache (min)")},
    )
    cache_ttl: int = Field(
        default=300,
        ge=10,
        description="日程注入缓存有效期（秒，默认 5 分钟）；过期后重查数据库",
        json_schema_extra={"label": "缓存有效期", **_ui_i18n("Cache TTL (s)")},
    )
    cache_max_size: int = Field(
        default=100,
        ge=10,
        description="注入缓存最大条目数（LRU 策略，超出按最近最少使用淘汰）",
        json_schema_extra={"label": "缓存最大条数", **_ui_i18n("Cache max entries")},
    )

    # ── 命令展示 ────────────────────────────────────────────
    list_draw_image: bool = Field(
        default=True,
        description="/cateye_test_plan list 是否绘制日程图片。关闭后只返回文字长文本",
        json_schema_extra={"label": "list 绘制图片", **_ui_i18n("Draw list image")},
    )
    image_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="日程图片绘制超时（秒）。超时静默放弃图片，/cateye_test_plan list 自动降级为纯文字（不会提示绘制失败）",
        json_schema_extra={"label": "绘制超时", **_ui_i18n("Image timeout (s)")},
    )

    # ── 时区 ────────────────────────────────────────────────
    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区设置（IANA 命名，如 Asia/Shanghai、UTC、America/New_York）",
        json_schema_extra={"label": "时区", "placeholder": "Asia/Shanghai", **_ui_i18n("Timezone")},
    )

    # ── M5 情绪联动（§4.2 / §6.2 F）─────────────────────────
    emotion_influence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="情绪对日程的影响强度（0~1，0=关闭）：睡醒情绪描述进日程生成 prompt，"
                    "同时决定睡眠大修裁判增量的动态限幅 ±(0.4+0.4×本值)",
        json_schema_extra={"label": "情绪影响强度", "step": 0.05, **_ui_i18n("Emotion influence")},
    )


class InjectSectionConfig(PluginConfigBase):
    """智能注入策略（来源：自主规划插件 [inject] 段；推荐挂载名 ``inject``）。"""

    __ui_label__ = "智能注入策略"
    __ui_icon__ = "zap"
    __ui_order__ = 7
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Smart Injection", "description": "Intent-aware schedule injection tuning."}
    }

    enable_intent_classification: bool = Field(
        default=True,
        description="启用意图分类（识别用户消息属于问当前 / 问未来 / 闲聊 / 技术问答等，决定注入的详细程度与是否注入）",
        json_schema_extra={"label": "启用意图分类", **_ui_i18n("Intent classification")},
    )
    enable_inject_optimization: bool = Field(
        default=True,
        description="启用注入优化器（防止重复注入和无效打扰，planner / replyer 共用冷却）",
        json_schema_extra={"label": "启用注入优化", **_ui_i18n("Injection optimizer")},
    )
    casual_chat_inject_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="闲聊场景（非直接询问 / 非技术问答）的注入概率（0-1）",
        json_schema_extra={"label": "闲聊注入概率", "step": 0.05, **_ui_i18n("Casual inject probability")},
    )
    context_max_turns: int = Field(
        default=3,
        ge=1,
        description="对话上下文保留轮数（用于判断是否在连续讨论日程话题）",
        json_schema_extra={"label": "上下文轮数", **_ui_i18n("Context turns")},
    )
    context_ttl: int = Field(
        default=600,
        ge=60,
        description="对话上下文过期时间（秒），超时后认为是新会话",
        json_schema_extra={"label": "上下文过期", **_ui_i18n("Context TTL (s)")},
    )


class PlanningProactiveSectionConfig(PluginConfigBase):
    """日程驱动的主动行为（来源：自主规划插件 [proactive] 段；推荐挂载名 ``planning_proactive``）。

    与 [patrol]（虚拟恋人巡检）互相独立：本段负责「活动切换主动开口 / 早间问好 /
    按活动调节聊天频率」，白名单取群聊与其他会话两份名单的并集。
    """

    __ui_label__ = "日程主动行为"
    __ui_icon__ = "notifications_active"
    __ui_order__ = 8
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule Proactive", "description": "Activity-switch proactive chats and frequency modulation."}
    }

    proactive_group_ids: List[str] = Field(
        default_factory=list,
        description="群聊主动行为白名单：直接填群号（如 [\"123456\"]），无需前缀。留空 = 所有群聊生效",
        json_schema_extra={
            "label": "群聊白名单",
            "hint": '直接填群号，例 ["123456"]；留空 = 所有群聊生效',
            **_ui_i18n("Group whitelist", "Group IDs; empty = all groups."),
        },
    )
    proactive_other_streams: List[str] = Field(
        default_factory=list,
        description="其他会话主动行为白名单：私聊与指定会话，按 qq:private:789 / session:xxx 格式填写，留空 = 不含其他会话",
        json_schema_extra={
            "label": "其他会话白名单",
            "hint": 'qq:private:789 / session:xxx；留空 = 不含其他会话',
            **_ui_i18n("Other streams", "qq:private:789 / session:xxx entries."),
        },
    )
    proactive_fresh_window_minutes: int = Field(
        default=10,
        ge=1,
        le=60,
        description="主动行为触发窗口（分钟）。活动开始后每个会话在窗口内获得独立随机延迟，延迟结束即触发；错过窗口不补发",
        json_schema_extra={"label": "触发窗口(分钟)", **_ui_i18n("Fresh window (min)")},
    )
    enable_proactive_trigger: bool = Field(
        default=True,
        description="活动切换主动发起：活动开始后在触发窗口内让 bot 主动开口（每会话每活动每天 1 次）。"
                    "不覆盖当天睡醒后的第一个活动（由早间问好负责），睡眠时段内不触发",
        json_schema_extra={"label": "活动切换主动发起", **_ui_i18n("Activity-switch trigger")},
    )
    enable_morning_greeting: bool = Field(
        default=False,
        description="早间问好：bot 睡醒后第一个活动开始时，主动向白名单内的会话道早安",
        json_schema_extra={"label": "早间问好", **_ui_i18n("Morning greeting")},
    )
    morning_greeting_require_activation: bool = Field(
        default=False,
        description="早间问好是否需要激活。开启后：随机延迟结束并不立即问好——从 bot 睡醒起算观察会话消息，"
                    "期间有人说话则照常问好；截止第一个活动结束前 10 分钟仍无人说话，放弃该会话的早间问好",
        json_schema_extra={"label": "早间问好需激活", **_ui_i18n("Require activation")},
    )
    enable_frequency_modulation: bool = Field(
        default=True,
        description="按当前活动类型动态调节聊天频率（ctx.frequency.set_adjust）：学习/工作时 30%，休息/娱乐时 130%~150%",
        json_schema_extra={"label": "活动频率调控", **_ui_i18n("Frequency modulation")},
    )


class PlanningCleanupSectionConfig(PluginConfigBase):
    """日程后台清理（来源：自主规划插件 [autonomous_planning] 段；推荐挂载名 ``planning_cleanup``）。"""

    __ui_label__ = "日程后台清理"
    __ui_icon__ = "trash-2"
    __ui_order__ = 9
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule Cleanup", "description": "Background cleanup of stale schedules and goals."}
    }

    cleanup_interval: int = Field(
        default=3600,
        ge=60,
        description="后台清理循环间隔（秒，默认 1 小时）",
        json_schema_extra={"label": "清理间隔(秒)", **_ui_i18n("Cleanup interval (s)")},
    )
    cleanup_old_goals_days: int = Field(
        default=30,
        ge=1,
        description="保留多少天前的已完成 / 取消目标（超过自动删除）",
        json_schema_extra={"label": "目标保留天数", **_ui_i18n("Goal retention (days)")},
    )


def section_or_default(plugin: Any, attr: str, default_factory: Any) -> Any:
    """从聚合配置读取 section；尚未集成挂载时返回默认实例（保证服务可独立运行）。

    整合者在顶层 config.py 挂载 section 前后，服务都无需改动——
    未挂载时拿到的是本模块定义的默认值，挂载后动态读到用户配置（热重载生效）。
    """
    section = getattr(plugin.config, attr, None)
    return section if section is not None else default_factory()
