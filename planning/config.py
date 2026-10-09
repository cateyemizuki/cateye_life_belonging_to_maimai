"""日程规划系统配置 section（来源：自主规划插件 config_models.py [schedule]/[inject]/[proactive]/[autonomous_planning]）。

2026-10-10 配置整理轮后的四大段：
- ``[schedule]``           日程系统（基础）：总开关 / 作息锚点 / 自动生成 / 活动数量 /
                           注入开关与范围 / 真实节假日两项开关——**用户日常只会动这里**；
- ``[schedule_advanced]``  日程系统·高级设置：多轮生成 / 角色裁判 / list 绘图 / 时区 /
                           缓存 / 注入调优 / 赖床 / 情绪影响 / 后台清理（原 [inject] 与
                           [planning_cleanup] 两段并入本段）；
- ``[planning_proactive]`` 日程主动行为：活动切换主动开口 + 早晚安两类 + 名单系统 +
                           赖床提醒（原问候专用四名单与活动切换两名单**合并为**
                           群聊名单 / 私聊名单两份 + 名单模式）；
- （``[llm]`` / ``[prompts]`` 在 suite_config.py / suite_prompts.py 定义。）

本模块只定义 section 类（PluginConfigBase 子类），不定义聚合模型；
顶层聚合由 suite_config.py 挂载，跨段旧键迁移也由那里的聚合校验器统一执行。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal

import pydantic
from maibot_sdk import Field, PluginConfigBase

#: 名单模式的取值（新名单系统）：blacklist = 名单内不生效；whitelist = 只对名单内生效
LIST_MODE_BLACKLIST = "blacklist"
LIST_MODE_WHITELIST = "whitelist"


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


# ---------------------------------------------------------------------------
# [schedule] 日程系统（基础）
# ---------------------------------------------------------------------------


class ScheduleSectionConfig(PluginConfigBase):
    """日程系统基础设置（日常只动这一段）。"""

    __ui_label__ = "日程系统"
    __ui_icon__ = "calendar"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule", "description": "Daily routine generation, injection and holiday awareness."}
    }

    # ── 模块独立总开关 ───────────────────────────────────────
    enabled: bool = Field(
        default=True,
        description="日程模块总开关：关闭后停止日程生成/注入/主动行为（工具与命令返回未启用提示）",
        json_schema_extra={
            "label": "启用日程模块",
            "hint": "关 = 不生成日程、不注入日程、不主动开口；早晚安一并停止（它们由本模块驱动）",
            **_ui_i18n("Enable schedule module", "Master switch of the schedule module."),
        },
    )

    # ── 作息锚点 ─────────────────────────────────────────────
    wake_time: str = Field(
        default="07:00",
        description="起床时间锚点（HH:MM）：清醒时段从这一刻开始；早安问候也以这一刻为基准做漂移",
        json_schema_extra={
            "label": "起床时间",
            "hint": "HH:MM；同时被早安问候与日程生成锚定使用（默认 07:00）",
            "pattern": r"^\d{2}:\d{2}$",
            **_ui_i18n("Wake time", "Wake-up anchor (HH:MM); also the morning-greeting baseline."),
        },
    )
    sleep_time: str = Field(
        default="23:00",
        description="入睡时间锚点（HH:MM）：这一刻之后进入睡眠时段；晚安问候以当天最后一个日程为准、"
                    "无日程时回退本时刻",
        json_schema_extra={
            "label": "入睡时间",
            "hint": "HH:MM；睡眠时段整体不生成日程（无睡眠模式）且不主动开口",
            "pattern": r"^\d{2}:\d{2}$",
            **_ui_i18n("Sleep time", "Bedtime anchor (HH:MM)."),
        },
    )
    no_sleep_mode: bool = Field(
        default=False,
        description="无睡眠模式：入睡到起床的时段不生成任何日程（生成后自动销毁该时段残留条目），"
                    "且睡眠时段内不触发主动行为。适合不需要睡眠的角色设定",
        json_schema_extra={
            "label": "无睡眠模式",
            "hint": "开 = 睡眠时段空着；关 = 正常生成「睡觉」条目",
            **_ui_i18n("No-sleep mode", "No schedule entries inside the sleep window when enabled."),
        },
    )

    # ── 定时生成 ─────────────────────────────────────────────
    auto_generate_enabled: bool = Field(
        default=True,
        description="每天自动用 LLM 生成当日日程（当天已有日程则跳过）",
        json_schema_extra={
            "label": "自动生成日程",
            "hint": "关 = 只能手动 /plan regenerate；已有日程时不会重复生成",
            **_ui_i18n("Auto generate"),
        },
    )
    auto_generate_after_last_end_minutes: int = Field(
        default=90,
        ge=0,
        description="生成时刻（相对锚点）：当天最后一个非睡眠日程条目结束后 N 分钟生成新一天日程；"
                    "当天没有日程时以「入睡时间」为基准。硬约束：不晚于「起床时间」前 30 分钟",
        json_schema_extra={
            "label": "生成延迟（最后日程结束后）",
            "hint": "分钟；默认 90 = 入睡锚点 23:00 时 00:30 生成",
            **_ui_i18n("Delay after last activity", "Minutes after the last non-sleep activity ends."),
        },
    )

    # ── 生成结果口径 ─────────────────────────────────────────
    min_activities: int = Field(
        default=6,
        ge=1,
        le=20,
        description="每天生成的最少活动数",
        json_schema_extra={
            "label": "最少活动数",
            "hint": "太少会显得一天很空；建议 5-8",
            **_ui_i18n("Min activities"),
        },
    )
    max_activities: int = Field(
        default=12,
        ge=1,
        le=25,
        description="每天生成的最多活动数",
        json_schema_extra={
            "label": "最多活动数",
            "hint": "太多会碎片化；建议 10-15",
            **_ui_i18n("Max activities"),
        },
    )
    custom_prompt: str = Field(
        default="",
        description="当前生活阶段 / 长期状态：角色的持续设定（如「正在环游世界」「备考研究生」），"
                    "日程生成会延续这一方向",
        json_schema_extra={
            "label": "当前生活阶段 / 长期状态",
            "hint": "留空使用默认风格；这是让日程贴合角色主线最有效的一项",
            "rows": 3,
            "placeholder": "（留空使用默认风格）",
            **_ui_i18n("Life stage / long-term state", "Persistent life-stage hint merged into the generation prompt."),
        },
    )
    enable_detailed_description: bool = Field(
        default=True,
        description="活动描述开关：关闭后生成、注入、命令展示都不再显示长描述（省 token）",
        json_schema_extra={
            "label": "活动描述",
            "hint": "关 = 日程只显示活动名，不显示描述",
            **_ui_i18n("Detailed description"),
        },
    )

    # ── 注入 ────────────────────────────────────────────────
    inject_into_planner: bool = Field(
        default=True,
        description="在 planner 决策时把当前日程注入上下文（决定 bot 说话时知不知道自己在干什么）",
        json_schema_extra={
            "label": "注入 planner",
            "hint": "关 = planner 侧看不到日程（回复会失去生活感）",
            **_ui_i18n("Inject into planner"),
        },
    )
    inject_into_replyer: bool = Field(
        default=True,
        description="在 replyer 生成回复前把当前日程作为额外提示注入；与 planner 注入共享冷却，不会两阶段连刷",
        json_schema_extra={
            "label": "注入 replyer",
            "hint": "关 = 只有 planner 侧注入；两处都关等于完全不注入日程",
            **_ui_i18n("Inject into replyer"),
        },
    )
    inject_scope: Literal["all", "target_private"] = Field(
        default="all",
        description="注入范围：all = 所有会话（群聊、私聊都注入日程）；"
                    "target_private = 只给目标陪伴私聊注入",
        json_schema_extra={
            "label": "注入范围",
            "hint": "默认 all（所有会话都有日程感）；选 target_private 则只有陪伴对象私聊能看到日程",
            **_ui_i18n("Injection scope",
                       "all = every chat; target_private = only the companion private chat."),
        },
    )

    # ── 真实节假日 ───────────────────────────────────────────
    holiday_inject_enabled: bool = Field(
        default=True,
        description="注入真实节假日：开启后日程块头部会注明今天日期、星期几与节假日状态"
                    "（如「国庆节假期」/「调休补班」）",
        json_schema_extra={
            "label": "注入真实节假日",
            "hint": "关 = 日程块不显示日期与节假日；需要联网查询（失败时按普通工作日处理）",
            **_ui_i18n("Inject real-world holidays",
                       "Shows date/weekday/holiday in the schedule block header."),
        },
    )
    holiday_affects_schedule: bool = Field(
        default=False,
        description="节假日影响日程生成：开启后生成日程时会考虑今天是不是节假日（假期更放松、补班更像工作日）",
        json_schema_extra={
            "label": "节假日影响日程生成",
            "hint": "默认关；开 = 节假日当天的日程会明显更「放假」",
            **_ui_i18n("Holidays affect generation", "Let holidays influence the generated schedule."),
        },
    )


# ---------------------------------------------------------------------------
# [schedule_advanced] 日程系统·高级设置
# ---------------------------------------------------------------------------


class ScheduleAdvancedSectionConfig(PluginConfigBase):
    """日程系统·高级设置（原 [schedule] 深水项 + [inject] 段 + [planning_cleanup] 段）。

    这里的每一项都直接影响生成质量、模型成本或渲染行为；不理解建议保持默认。
    """

    __ui_label__ = "日程系统·高级设置"
    __ui_icon__ = "sliders"
    __ui_order__ = 4
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule · Advanced",
               "description": "Deep tuning for generation, role judge, rendering, caches and cleanup."}
    }

    # ── 多轮生成 ─────────────────────────────────────────────
    use_multi_round: bool = Field(
        default=True,
        description="多轮生成：首轮质量不达标时按反馈重试（提升日程质量，增加 token 成本）",
        json_schema_extra={
            "label": "多轮生成",
            "hint": "关 = 只生成一轮（省成本、质量波动大）",
            **_ui_i18n("Multi-round generation"),
        },
    )
    max_rounds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="多轮生成最大轮数（1-5，推荐 2；越多越费 token）",
        json_schema_extra={
            "label": "最大轮数",
            **_ui_i18n("Max rounds"),
        },
    )
    quality_threshold: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description="质量阈值：达到该分立即停止重试（0.80-0.90 推荐 0.85）",
        json_schema_extra={
            "label": "质量阈值",
            "step": 0.05,
            "hint": "调高 = 更容易触发重试、成本更高",
            **_ui_i18n("Quality threshold"),
        },
    )
    max_tokens: int = Field(
        default=8192,
        ge=1000,
        description="日程生成 LLM 的最大 token 数（过小可能截断日程）",
        json_schema_extra={
            "label": "生成最大 token",
            **_ui_i18n("Max tokens"),
        },
    )
    generation_timeout: float = Field(
        default=180.0,
        ge=10.0,
        description="日程生成 LLM 调用超时（秒），推荐 120-300；会换算为宿主超时，避免被默认 30 秒截断",
        json_schema_extra={
            "label": "生成超时(秒)",
            **_ui_i18n("Generation timeout (s)"),
        },
    )
    recent_schedule_days: int = Field(
        default=3,
        ge=1,
        le=14,
        description="生成新日程时回看最近 N 天的日程作为去重参考（3-5 防交替式重复）",
        json_schema_extra={
            "label": "回看天数",
            **_ui_i18n("Recent days lookback"),
        },
    )
    history_message_limit: int = Field(
        default=30,
        ge=0,
        description="生成日程时回读各会话当天最近 N 条聊天消息作为灵感。0 = 禁用",
        json_schema_extra={
            "label": "历史消息条数",
            **_ui_i18n("History message limit"),
        },
    )
    knowledge_search_limit: int = Field(
        default=5,
        ge=0,
        description="生成日程时检索知识库的最大条数。0 = 禁用",
        json_schema_extra={
            "label": "知识库条数",
            **_ui_i18n("Knowledge search limit"),
        },
    )
    cross_day_activity: bool = Field(
        default=True,
        description="跨天活动：允许 time_window 跨夜写法，并在注入时回读昨日跨夜活动",
        json_schema_extra={
            "label": "跨天活动",
            **_ui_i18n("Cross-day activity"),
        },
    )
    min_description_length: int = Field(
        default=20,
        ge=5,
        description="活动描述最小字符数（20-30 较合理，过短易模糊）",
        json_schema_extra={
            "label": "描述最少字数",
            **_ui_i18n("Min description length"),
        },
    )
    max_description_length: int = Field(
        default=50,
        ge=5,
        description="活动描述最大字符数（50-100 较合理，过长占 token）",
        json_schema_extra={
            "label": "描述最多字数",
            **_ui_i18n("Max description length"),
        },
    )
    timezone: str = Field(
        default="Asia/Shanghai",
        description="时区设置（IANA 命名，如 Asia/Shanghai、UTC、America/New_York）：日程日期、"
                    "活动时段、问候漂移都按此时区计算",
        json_schema_extra={
            "label": "时区",
            "placeholder": "Asia/Shanghai",
            "hint": "改错会让「今天」错位（写入时间正常但日程日期不对）",
            **_ui_i18n("Timezone"),
        },
    )

    # ── 角色裁判 ─────────────────────────────────────────────
    role_judge_enabled: bool = Field(
        default=True,
        description="角色裁判模式：你（用户）用自然语言要求改日程时，让 bot 以角色身份先做判断——"
                    "接受并改今天（today）/ 接受为未来预约（future）/ 拒绝不改（reject）。"
                    "判定失败时安全回退为「日程不变」",
        json_schema_extra={
            "label": "角色裁判模式",
            "hint": "开 = 请求先经角色人格判断（可能被拒绝，更像真人）；关 = 一律记为未来预约",
            **_ui_i18n("Role judge",
                       "On: the bot decides in character (today / future / reject). "
                       "Off: every request is recorded as a future appointment."),
        },
    )
    role_judge_temperature: float = Field(
        default=0.3,
        ge=0.0,
        le=2.0,
        description="角色裁判温度：越低越保守（更容易拒绝离谱请求），越高越随和",
        json_schema_extra={
            "label": "裁判温度",
            "step": 0.1,
            "hint": "默认 0.3；判定要稳定结论，一般不用动",
            **_ui_i18n("Judge temperature"),
        },
    )

    # ── 命令展示 ────────────────────────────────────────────
    list_draw_image: bool = Field(
        default=True,
        description="/plan list 是否绘制日程图片（内置字体渲染后合并转发）；关闭后只返回文字长文本",
        json_schema_extra={
            "label": "list 绘制图片",
            "hint": "关 = 纯文字，省渲染开销；渲染失败时会自动降级为纯文字",
            **_ui_i18n("Draw list image"),
        },
    )
    image_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="日程图片绘制超时（秒）；超时静默降级为纯文字（不提示失败）",
        json_schema_extra={
            "label": "绘制超时(秒)",
            **_ui_i18n("Image timeout (s)"),
        },
    )

    # ── 智能注入调优（原 [inject] 段）────────────────────────
    enable_intent_classification: bool = Field(
        default=True,
        description="意图分类：识别用户消息属于问当前 / 问未来 / 闲聊 / 技术问答，决定注入的详细程度与是否注入",
        json_schema_extra={
            "label": "启用意图分类",
            "hint": "关 = 所有消息按同一口径注入，可能在不相关的问答里也带日程",
            **_ui_i18n("Intent classification"),
        },
    )
    enable_inject_optimization: bool = Field(
        default=True,
        description="注入优化器：防止重复注入和无效打扰（planner / replyer 共用冷却）",
        json_schema_extra={
            "label": "启用注入优化",
            "hint": "关 = 每轮都可能重复注入日程，消息更啰嗦",
            **_ui_i18n("Injection optimizer"),
        },
    )
    casual_chat_inject_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="闲聊场景（非直接询问 / 非技术问答）的注入概率",
        json_schema_extra={
            "label": "闲聊注入概率",
            "step": 0.05,
            "hint": "0 = 闲聊时不提日程；1 = 每次都提",
            **_ui_i18n("Casual inject probability"),
        },
    )
    context_max_turns: int = Field(
        default=3,
        ge=1,
        description="对话上下文保留轮数（用于判断是否在连续讨论日程话题）",
        json_schema_extra={
            "label": "上下文轮数",
            **_ui_i18n("Context turns"),
        },
    )
    context_ttl: int = Field(
        default=600,
        ge=60,
        description="对话上下文过期时间（秒），超时后认为是新会话",
        json_schema_extra={
            "label": "上下文过期(秒)",
            **_ui_i18n("Context TTL (s)"),
        },
    )

    # ── 性能与缓存 ───────────────────────────────────────────
    inject_cache_minutes: int = Field(
        default=15,
        ge=1,
        le=60,
        description="当前活动查询的时间窗口缓存（分钟）",
        json_schema_extra={
            "label": "活动缓存(分钟)",
            "hint": "越小越实时、查库越频繁；一般不用动",
            **_ui_i18n("Activity cache (min)"),
        },
    )
    cache_ttl: int = Field(
        default=300,
        ge=10,
        description="日程注入缓存有效期（秒，默认 5 分钟）；过期后重查数据库",
        json_schema_extra={
            "label": "缓存有效期(秒)",
            **_ui_i18n("Cache TTL (s)"),
        },
    )
    cache_max_size: int = Field(
        default=100,
        ge=10,
        description="注入缓存最大条目数（LRU 策略，超出按最近最少使用淘汰）",
        json_schema_extra={
            "label": "缓存最大条数",
            **_ui_i18n("Cache max entries"),
        },
    )
    max_future_activities: int = Field(
        default=3,
        ge=0,
        description="注入时最多显示的未来活动条数。0 = 不显示",
        json_schema_extra={
            "label": "未来活动条数",
            **_ui_i18n("Future activities shown"),
        },
    )

    # ── 情绪联动 ────────────────────────────────────────────
    emotion_influence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="情绪对日程的影响强度（0 = 关闭）：睡醒情绪会写进日程生成提示词，"
                    "同时决定睡眠结算的增量限幅",
        json_schema_extra={
            "label": "情绪影响强度",
            "step": 0.05,
            "hint": "0 = 日程完全不受情绪影响；越高情绪对当天安排的影响越明显",
            **_ui_i18n("Emotion influence"),
        },
    )

    # ── 赖床 ────────────────────────────────────────────────
    lie_in_max_until: str = Field(
        default="12:00",
        description="赖床（推迟起床）的硬上限时刻（HH:MM）：夜间被吵醒后调用赖床工具时，"
                    "传入的起床时间超过本值会被钳到本值；格式非法按 12:00 处理",
        json_schema_extra={
            "label": "赖床上限时刻",
            "hint": "HH:MM；最大只能赖到这个点（默认 12:00）",
            "pattern": r"^\d{1,2}:\d{2}$",
            **_ui_i18n("Lie-in hard limit", "Hard cap for the lie-in tool (HH:MM)."),
        },
    )

    # ── 后台清理（原 [planning_cleanup] 段）──────────────────
    cleanup_interval: int = Field(
        default=3600,
        ge=60,
        description="后台清理循环间隔（秒，默认 1 小时）",
        json_schema_extra={
            "label": "清理间隔(秒)",
            "hint": "后台删除过期日程/目标/约定的频率",
            **_ui_i18n("Cleanup interval (s)"),
        },
    )
    cleanup_old_goals_days: int = Field(
        default=30,
        ge=1,
        description="保留多少天前的已完成 / 取消目标（超过自动删除）",
        json_schema_extra={
            "label": "目标保留天数",
            **_ui_i18n("Goal retention (days)"),
        },
    )

    # ── 兼容字段（隐藏，不占界面）────────────────────────────
    auto_generate_time: str = Field(
        default="00:30",
        description="【已废弃】旧的「每日自动生成日程的绝对时刻」（HH:MM）。仅当日程生成延迟保持默认且"
                    "本值被改过时才生效；新配置请用「生成延迟（最后日程结束后）」",
        json_schema_extra={
            "label": "生成时刻（已废弃）",
            "placeholder": "00:30",
            "hidden": True,
            **_ui_i18n("Generate at (deprecated)"),
        },
    )


# ---------------------------------------------------------------------------
# [planning_proactive] 日程主动行为
# ---------------------------------------------------------------------------


class PlanningProactiveSectionConfig(PluginConfigBase):
    """日程主动行为：活动切换主动开口 + 早晚安问候 + 名单系统 + 赖床提醒。

    与 [patrol]（陪伴巡检）分工：
    - 本段负责**群聊与其他私聊**的活动切换开口与早/晚安问候，以及名单过滤；
    - 目标陪伴私聊的日常巡检/想念归 [patrol]；早/晚安由本段的漂移机制统一驱动
      （发过之后写标记，陪伴侧巡检见标记即不再重复开口——两模块因此不会各发一条）。

    名单系统（2026-10-10 合并）：原「活动切换白名单（支持前缀）+ 问候专用四名单」统一为
    群聊名单 / 私聊名单两份（**只填裸号**：群聊填群号、私聊填 QQ 号），各配一个模式开关：
    - 模式 = 黑名单（默认·群聊）：名单里的人**不**生效，留空 = 全部生效；
    - 模式 = 白名单（默认·私聊）：只有名单里的人生效，留空 = 全部不生效。
    目标陪伴对象**始终视为在私聊黑名单中**（防止日程主动开口与陪伴的早/晚安撞车），
    但早/晚安问候对目标私聊仍然照发（问候走单独通道，不看名单过滤）。
    """

    __ui_label__ = "日程主动行为"
    __ui_icon__ = "notifications_active"
    __ui_order__ = 5
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule Proactive",
               "description": "Activity-switch proactive chat, morning/night greetings and chat lists."}
    }

    # ── 活动切换主动发起 ─────────────────────────────────────
    enable_proactive_trigger: bool = Field(
        default=True,
        description="活动切换主动开口：活动开始后在触发窗口内让 bot 主动说一两句（每个会话每个活动每天 1 次）。"
                    "跳过当天睡醒后的第一个活动（由早安负责），睡眠时段不触发",
        json_schema_extra={
            "label": "启用活动切换主动开口",
            "hint": "关 = 群聊里不再按日程切换主动发言；早/晚安不受影响",
            **_ui_i18n("Activity-switch trigger"),
        },
    )
    proactive_fresh_window_minutes: int = Field(
        default=10,
        ge=1,
        le=60,
        description="触发窗口（分钟）：活动开始后每个会话在窗口内获得独立随机延迟，延迟结束即触发；"
                    "错过窗口不补发",
        json_schema_extra={
            "label": "触发窗口(分钟)",
            "hint": "越大越不容易「活动刚开始就说话」（更像真人）",
            **_ui_i18n("Fresh window (min)"),
        },
    )
    enable_frequency_modulation: bool = Field(
        default=True,
        description="按当前活动类型调节聊天频率：学习/工作时降低发言频率，休息/娱乐时提高",
        json_schema_extra={
            "label": "活动频率调控",
            "hint": "关 = 不让日程影响聊天频率",
            **_ui_i18n("Frequency modulation"),
        },
    )

    # ── 名单系统（群聊 / 私聊）───────────────────────────────
    proactive_group_ids: List[str] = Field(
        default_factory=list,
        description='群聊名单（只填**裸群号**，如 ["123456"]，不需要任何前缀）：'
                    "名单模式=黑名单时这些群不生效、留空则所有群生效；"
                    "名单模式=白名单时只有这些群生效、留空则所有群都不生效。不支持通配 *",
        json_schema_extra={
            "label": "群聊名单",
            "hint": '只填群号，例 ["123456"]；默认黑名单 + 留空 = 所有群聊都生效',
            **_ui_i18n("Group list", 'Bare group IDs; default mode = blacklist with empty list = all groups.'),
        },
    )
    proactive_group_list_mode: Literal["blacklist", "whitelist"] = Field(
        default="blacklist",
        description="群聊名单模式：黑名单 = 名单内的群不生效（留空 = 全部生效）；"
                    "白名单 = 只有名单内的群生效（留空 = 全部不生效）",
        json_schema_extra={
            "label": "群聊名单模式",
            "hint": "默认黑名单（留空即全群生效，符合「群里本来就该活跃」的直觉）",
            **_ui_i18n("Group list mode", "blacklist (default) or whitelist."),
        },
    )
    proactive_private_ids: List[str] = Field(
        default_factory=list,
        description='私聊名单（只填**裸 QQ 号**，如 ["789"]，不需要任何前缀）：'
                    "名单模式=白名单时只有这些私聊生效、留空则所有私聊都不生效；"
                    "名单模式=黑名单时这些私聊不生效。目标陪伴对象始终按黑名单处理",
        json_schema_extra={
            "label": "私聊名单",
            "hint": '只填 QQ 号；默认白名单 + 留空 = 其他私聊一概不主动，只有陪伴对象私聊照常',
            **_ui_i18n("Private list", 'Bare QQ IDs; default mode = whitelist with empty list = no other privates.'),
        },
    )
    proactive_private_list_mode: Literal["blacklist", "whitelist"] = Field(
        default="whitelist",
        description="私聊名单模式：白名单 = 只有名单内的私聊生效（留空 = 都不生效）；"
                    "黑名单 = 名单内的私聊不生效（留空 = 全部生效）",
        json_schema_extra={
            "label": "私聊名单模式",
            "hint": "默认白名单（留空即不打扰任何其他私聊，避免误伤）",
            **_ui_i18n("Private list mode", "whitelist (default) or blacklist."),
        },
    )

    # ── 早晚安 ──────────────────────────────────────────────
    greeting_morning_enabled: bool = Field(
        default=True,
        description="启用早安：睡醒时刻（[schedule] 起床时间）+ 早安稳态漂移后，向候选举行的会话道早安",
        json_schema_extra={
            "label": "启用早安",
            "hint": "关 = 完全不发早安（含目标私聊与群聊）",
            **_ui_i18n("Morning greeting", "Off: no morning greetings anywhere."),
        },
    )
    greeting_night_enabled: bool = Field(
        default=True,
        description="启用晚安：当天最后一个非睡眠日程结束时刻 − 漂移后，向候选举行的会话道晚安",
        json_schema_extra={
            "label": "启用晚安",
            "hint": "关 = 完全不发晚安（含目标私聊与群聊）",
            **_ui_i18n("Night greeting", "Off: no night greetings anywhere."),
        },
    )
    morning_drift: str = Field(
        default="10-30",
        description="早安漂移区间（分钟，相对起床时刻）：填 \"a-b\" 或单数字 N（= 0~N 随机）；"
                    "触发时刻 = 起床时刻 + 随机漂移。非法值回退 10-30",
        json_schema_extra={
            "label": "早安漂移(分钟)",
            "hint": '例 "10-30" 或 "20"（= 0~20 随机）',
            "pattern": r"^\d+(-\d+)?$",
            **_ui_i18n("Morning drift (min)", 'e.g. "10-30" or "20"'),
        },
    )
    night_drift: str = Field(
        default="10-30",
        description="晚安漂移区间（分钟，相对最后一条日程结束）：填 \"a-b\" 或单数字 N（= 0~N 随机）；"
                    "触发时刻 = 最后日程结束 − 随机漂移。非法值回退 10-30",
        json_schema_extra={
            "label": "晚安漂移(分钟)",
            "hint": '例 "10-30" 或 "20"；向前漂移（比日程结束更早开口）',
            "pattern": r"^\d+(-\d+)?$",
            **_ui_i18n("Night drift (min)", 'e.g. "10-30" or "20"'),
        },
    )
    greeting_require_activation: bool = Field(
        default=True,
        description="问候需激活（群聊与其他私聊）：到点后需先观察到该会话自基准点起有人发过消息才问候，"
                    "无人则推迟、到期限放弃（当天不补发）。目标私聊不受此项影响",
        json_schema_extra={
            "label": "问候需激活",
            "hint": "开 = 群里没人说话就不进去打招呼（推荐）；关 = 到点就发",
            **_ui_i18n("Greeting requires activation"),
        },
    )
    greeting_other_private_min_messages: int = Field(
        default=5,
        ge=5,
        description="其他私聊问候门槛（条）：前一日该私聊消息数达到该值才可能收到早/晚安"
                    "（**不允许低于 5**）；取不到消息计数按 0 处理（不发送）",
        json_schema_extra={
            "label": "私聊问候门槛(条)",
            "hint": "防打扰护栏：昨天没聊够 5 条就不主动问早/晚安",
            **_ui_i18n("Other-private greeting threshold (msgs)"),
        },
    )

    # ── 赖床提醒 ─────────────────────────────────────────────
    nap_reminder_enabled: bool = Field(
        default=True,
        description="赖床提醒：夜间被吵醒且重新睡着后，向 bot 发一条临时提醒（点名赖床工具，只在当轮有效）",
        json_schema_extra={
            "label": "赖床提醒",
            "hint": "关 = 不提醒，但 bot 仍可自行推迟起床",
            **_ui_i18n("Nap reminder"),
        },
    )
    nap_reminder_max_per_day: int = Field(
        default=2,
        ge=1,
        description="每个睡眠周期（不是自然日）的有效提醒次数上限：提醒发送 + 工具成功改日程各计 1 次；"
                    "用尽后不再提醒",
        json_schema_extra={
            "label": "赖床提醒上限",
            **_ui_i18n("Nap reminder cap"),
        },
    )

    # ── 兼容字段（隐藏，不占界面；保留旧值语义）───────────────
    greeting_target_enabled: bool = Field(
        default=True,
        description="（兼容旧配置）目标陪伴私聊的问候开关；新界面不再提供，默认开",
        json_schema_extra={"label": "目标私聊问候（旧）", "hidden": True,
                           **_ui_i18n("Target-chat greetings (legacy)")},
    )
    greeting_group_enabled: bool = Field(
        default=True,
        description="（兼容旧配置）群聊问候开关；新界面不再提供，默认开",
        json_schema_extra={"label": "群聊问候（旧）", "hidden": True,
                           **_ui_i18n("Group greetings (legacy)")},
    )
    greeting_other_private_enabled: bool = Field(
        default=True,
        description="非目标私聊问候总开关（2026-10-10 起默认开）：真正的「发不发」由私聊名单表达——"
                    "私聊名单留空（默认白名单）时本就无人候选，故默认开不会打扰任何人；"
                    "只有旧配置显式 false 时才整体关闭其他私聊问候",
        json_schema_extra={"label": "其他私聊问候（旧）", "hidden": True,
                           **_ui_i18n("Other-private greetings (legacy)")},
    )
    greeting_group_blacklist: List[str] = Field(
        default_factory=list,
        description="（兼容旧配置）群聊问候黑名单；已并入「群聊名单」",
        json_schema_extra={"label": "群聊问候黑名单（旧）", "hidden": True,
                           **_ui_i18n("Group greeting blacklist (legacy)")},
    )
    greeting_group_whitelist: List[str] = Field(
        default_factory=list,
        description="（兼容旧配置）群聊问候白名单；已并入「群聊名单」",
        json_schema_extra={"label": "群聊问候白名单（旧）", "hidden": True,
                           **_ui_i18n("Group greeting whitelist (legacy)")},
    )
    greeting_other_private_whitelist: List[str] = Field(
        default_factory=list,
        description="（兼容旧配置）非目标私聊问候白名单；已并入「私聊名单」",
        json_schema_extra={"label": "其他私聊问候白名单（旧）", "hidden": True,
                           **_ui_i18n("Other-private greeting whitelist (legacy)")},
    )
    greeting_other_private_blacklist: List[str] = Field(
        default_factory=list,
        description="（兼容旧配置）非目标私聊问候黑名单；已并入「私聊名单」",
        json_schema_extra={"label": "其他私聊问候黑名单（旧）", "hidden": True,
                           **_ui_i18n("Other-private greeting blacklist (legacy)")},
    )
    morning_other_private_enabled: bool = Field(
        default=False,
        description="（兼容旧配置）旧「其他私聊早安」开关；并入 greeting_other_private_enabled",
        json_schema_extra={"label": "其他私聊早安（旧）", "hidden": True,
                           **_ui_i18n("Morning to other privates (legacy)")},
    )
    morning_other_private_min_messages: int = Field(
        default=5,
        ge=5,
        description="（兼容旧配置）旧「其他私聊早安门槛」；并入 greeting_other_private_min_messages",
        json_schema_extra={"label": "其他私聊门槛（旧）", "hidden": True,
                           **_ui_i18n("Other-private threshold (legacy)")},
    )

    # ── 旧键迁移（段级；在聚合校验器里与其它段一起被调用）──────
    @pydantic.model_validator(mode="before")
    @classmethod
    def _migrate_legacy_lists(cls, data: object) -> object:
        """旧名单/开关 → 新统一名单系统（2026-10-10 合并；**只在首轮迁移生效**）。

        触发条件：聚合校验器判定"尚未迁移"（``[plugin] layout_version`` 不是本轮标记）时，
        会在本段字典里放一枚哨兵 ``_layout_migrated=False``；已迁移形态（哨兵为 True）直接放行——
        否则新旧同名的 ``proactive_group_ids`` 会在第二次校验时被当成旧白名单再映射一遍
        （非幂等，见 scratch/_config_round_checks.py 的迁移幂等用例）。

        迁移语义（旧键取值偏离旧默认时才发生）：

        - ``proactive_group_ids`` 非空（旧活动切换白名单）→ ``proactive_group_list_mode=whitelist``
          且名单沿用（保住「只在指定群开口」的旧语义）；
        - 否则 ``greeting_group_blacklist`` 非空 → 黑名单模式沿用；``greeting_group_whitelist``
          非空（且非通配）→ 白名单模式沿用；通配 ``["*"]`` → 黑名单 + 空名单（= 全放行）；
        - ``greeting_other_private_whitelist`` 非空 → 私聊白名单沿用；``["*"]`` → 黑名单 + 空名单；
          ``greeting_other_private_blacklist`` 非空 → 黑名单沿用；
        - 旧 ``greeting_other_private_enabled=true`` / ``morning_other_private_enabled=true`` 且
          没有任何私聊名单 → 视为「旧版全放行过」，按黑名单 + 空名单（对已知私聊生效）迁移；
        - 旧**问候白名单**只在「旧开关为真」时搬进新名单——旧开关为假时该名单在旧版是惰性的，
          搬过去会凭空开启问候（迁移必须复现旧 effective 行为）；
        - 旧 ``morning_other_private_min_messages`` 偏离 5 → 补进新门槛（新键为默认时）。
        """
        if not isinstance(data, dict):
            return data
        if data.get("_layout_migrated") is True:
            # 已迁移形态（聚合校验器判定过）：同名键不再做旧语义映射，直接放行
            return data
        migrated = dict(data)
        migrated.pop("_layout_migrated", None)

        def _clean(raw: object) -> List[str]:
            out: List[str] = []
            for item in list(raw or []) if isinstance(raw, (list, tuple)) else []:
                text = str(item).strip()
                if text and text != "*":
                    out.append(text)
            return out

        def _wildcard(raw: object) -> bool:
            return any(str(item).strip() == "*" for item in (raw or []) if str(item).strip())

        group_whitelist = _clean(migrated.get("greeting_group_whitelist"))
        group_blacklist = _clean(migrated.get("greeting_group_blacklist"))
        old_group_ids = _clean(migrated.get("proactive_group_ids"))

        new_group_ids: List[str] = []
        new_group_mode = LIST_MODE_BLACKLIST
        if old_group_ids:
            new_group_ids, new_group_mode = old_group_ids, LIST_MODE_WHITELIST
        elif group_blacklist:
            new_group_ids, new_group_mode = group_blacklist, LIST_MODE_BLACKLIST
        elif group_whitelist:
            new_group_ids, new_group_mode = group_whitelist, LIST_MODE_WHITELIST
        elif _wildcard(migrated.get("greeting_group_whitelist")):
            new_group_ids, new_group_mode = [], LIST_MODE_BLACKLIST  # 通配 = 全放行
        migrated["proactive_group_ids"] = new_group_ids
        migrated["proactive_group_list_mode"] = new_group_mode

        other_whitelist = _clean(migrated.get("greeting_other_private_whitelist"))
        other_blacklist = _clean(migrated.get("greeting_other_private_blacklist"))
        old_other_streams = _clean(migrated.pop("proactive_other_streams", None))
        # 旧前缀写法（qq:private:789 / session:xxx）→ 只保留可解析的裸 QQ 号
        old_other_qq: List[str] = []
        for entry in old_other_streams:
            text = entry.strip()
            if ":private:" in text:
                text = text.rsplit(":private:", 1)[1].strip()
            if text.isdigit():
                old_other_qq.append(text)

        old_greeting_switch_on = bool(migrated.get("greeting_other_private_enabled", False)) or bool(
            migrated.get("morning_other_private_enabled"))
        new_private_ids: List[str] = []
        new_private_mode = LIST_MODE_WHITELIST
        if other_whitelist and old_greeting_switch_on:
            # 旧问候白名单 + 旧开关为真 = 旧版真的会问候这些私聊 → 沿用白名单
            new_private_ids, new_private_mode = other_whitelist, LIST_MODE_WHITELIST
        elif other_blacklist:
            new_private_ids, new_private_mode = other_blacklist, LIST_MODE_BLACKLIST
        elif old_other_qq:
            new_private_ids, new_private_mode = old_other_qq, LIST_MODE_WHITELIST
        elif _wildcard(migrated.get("greeting_other_private_whitelist")):
            new_private_ids, new_private_mode = [], LIST_MODE_BLACKLIST
        elif old_greeting_switch_on:
            # 旧版「开着但名单空/通配」= 对所有已知私聊生效 → 新语义下等价于黑名单 + 空名单
            new_private_ids, new_private_mode = [], LIST_MODE_BLACKLIST
        migrated["proactive_private_ids"] = new_private_ids
        migrated["proactive_private_list_mode"] = new_private_mode

        # 旧门槛（偏离默认 5 才搬运；新键保持默认时才接管）
        legacy_threshold = migrated.get("morning_other_private_min_messages")
        try:
            legacy_value = int(legacy_threshold) if legacy_threshold is not None else 5
        except (TypeError, ValueError):
            legacy_value = 5
        if legacy_value != 5 and int(migrated.get("greeting_other_private_min_messages", 5) or 5) == 5:
            migrated["greeting_other_private_min_messages"] = legacy_value
        return migrated


# ---------------------------------------------------------------------------
# 兼容读取
# ---------------------------------------------------------------------------


def section_or_default(plugin: Any, attr: str, default_factory: Any) -> Any:
    """从聚合配置读取 section；尚未集成挂载时返回默认实例（保证服务可独立运行）。

    整合者在顶层 suite_config.py 挂载 section 前后，服务都无需改动——
    未挂载时拿到的是本模块定义的默认值，挂载后动态读到用户配置（热重载生效）。
    """
    section = getattr(plugin.config, attr, None)
    return section if section is not None else default_factory()
