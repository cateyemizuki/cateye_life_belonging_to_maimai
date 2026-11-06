"""日程规划系统配置 section（来源：自主规划插件 config_models.py [schedule]/[inject]/[proactive]/[autonomous_planning]）。

2026-10-10 配置整理轮后的四大段：
- ``[schedule]``           日程系统（基础）：总开关 / 作息锚点 / 自动生成 / 活动数量 /
                           注入开关与范围 / 真实节假日两项开关——**用户日常只会动这里**；
- ``[schedule_advanced]``  日程系统·高级设置：多轮生成 / 角色裁判 / list 绘图 / 时区 /
                           缓存 / 注入调优 / 赖床 / 情绪影响 / 后台清理（原 [inject] 与
                           [planning_cleanup] 两段并入本段）；
- ``[planning_proactive]`` 日程主动行为：早晚安两类 + 名单系统 + 赖床提醒（原问候专用四名单
                           与活动切换两名单**合并为**群聊名单 / 私聊名单两份 + 名单模式；
                           2026-10-20 用户指示：活动切换主动开口整条移除）；
- ``[life_state]``        生活状态（2026-10-25 滚动化改造新增，默认关：精力/饥饿/专注/压力
                           与滚动执行的全部键位；enabled=false 时零行为）。
- （``[llm]`` / ``[prompts]`` 在 suite_config.py / suite_prompts.py 定义。）

本模块只定义 section 类（PluginConfigBase 子类），不定义聚合模型；
顶层聚合由 suite_config.py 挂载，跨段旧键迁移也由那里的聚合校验器统一执行。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal

import pydantic
from maibot_sdk import Field, PluginConfigBase

from .event_catalog_config import EventCatalogFields

from core.i18n import ui_i18n as _ui_i18n
from core.internal_tuning import tuning_value as _tuning_value

#: 名单模式的取值（新名单系统）：blacklist = 名单内不生效；whitelist = 只对名单内生效
LIST_MODE_BLACKLIST = "blacklist"
LIST_MODE_WHITELIST = "whitelist"

#: H 类内部节拍常数（2026-11-04 配置精简轮）：状态机 tick 轮询粒度。
#: 生产现值=默认 300 已获 evidence 证实；「间隔粒度非时刻锚点」口径（方案 §1 规则2，
#: 若复核采广义时钟口径应回退 keep）。旧 TOML 键经 LEGACY_ROUTE_KEYS 入口 pop。
LIFE_TICK_SECONDS = 300


# ---------------------------------------------------------------------------
# [schedule] 日程系统（基础）
# ---------------------------------------------------------------------------


class ScheduleSectionConfig(EventCatalogFields):
    """设置每日作息、活动偏好和日程生成方式。"""

    __ui_label__ = "日程系统"
    __ui_icon__ = "calendar"
    __ui_order__ = 4
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
        description="入睡时间锚点（HH:MM）：这一刻之后进入睡眠时段；晚安漂移锚在睡前界 "
                    "min(本时刻, 生效静默起点)（最后一段日程的结束只用作观测窗下界）",
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

    # ── 生成结果口径 ─────────────────────────────────────────
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
        description="在 replyer 生成回复前把当前日程作为额外提示注入；冷却与 planner 侧按通道分开记账"
                    "（各自只有「同活动+同意图」才跳过，闲聊意图另有概率掷骰）——"
                    "planner 本轮注入过不拦截 replyer，同一轮不保证绝不双份",
        json_schema_extra={
            "label": "注入 replyer",
            "hint": "关 = 只有 planner 侧注入；两处都关等于完全不注入日程",
            **_ui_i18n("Inject into replyer"),
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

    # ── 滚动化改造（2026-10-25 滚动化改造新增）─────────────────────────────
    # R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
    # D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）。
    # PR8 3.0.0：``spine_mode`` 开关键已退役（依据 = 用户 2026-10-27 授权直接清理 +
    # 逐符号审定清单 WP3）——日意图 为唯一生成路线，本组 spine_* 键无条件参与判定。
    min_intentions: int = Field(
        default=3,
        ge=1,
        description="每天生成的日意图最少意图数",
        json_schema_extra={
            "label": "最少日意图数",
            **_ui_i18n("Daily intentions: min intentions"),
        },
    )
    max_intentions: int = Field(
        default=6,
        ge=1,
        description="每天生成的日意图最多意图数",
        json_schema_extra={
            "label": "最多日意图数",
            **_ui_i18n("Daily intentions: max intentions"),
        },
    )
    max_described_intents: int = Field(
        default=1,
        ge=0,
        description="最多附说明的日意图条数，0表示不附说明",
        json_schema_extra={
            "label": "带说明的日意图数",
            **_ui_i18n("Daily intentions: described intents"),
        },
    )
    # 2026-11-04 配置精简轮（J 类）：spine_recent_days / spine_history_messages /
    # spine_knowledge_limit 移入 mappings/internal_tuning.json[schedule]（内部上下文
    # 容量）。旧 TOML 键经 migrate_internal_tuning 值搬运；同名只读 property 见类尾。

    intention_multi_round: bool = Field(
        default=False,
        description="日意图生成失败时最多再尝试一轮",
        json_schema_extra={
            "label": "多轮生成日意图",
            **_ui_i18n("Daily intentions: multi-round"),
        },
    )
    generate_at: str = Field(
        default="00:30",
        description="每天生成日意图的固定时刻（HH:MM），无条件生效；"
                    "留空或非法值固定回落 00:30",
        json_schema_extra={
            "label": "日意图生成时刻",
            "hint": "HH:MM；留空/非法 = 回落 00:30（设计 §9.1：勿用稀疏意图的最后 end 触发，会过早生成次日）",
            "pattern": r"^\d{2}:\d{2}$",
            **_ui_i18n("Daily intentions: generate at",
                       "Fixed daily HH:MM for 日意图 generation; empty/invalid falls back to 00:30."),
        },
    )
    # PR8 3.0.0：``planning_daily_token_budget`` 预留键位已退役（自述「熔断策略本阶段
    # 未启用」的自造预留键；依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP3）。
    # 2026-10-29：事件库输出长度与Token额度也交回宿主，旧键只忽略。

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[schedule]）─────
    @property
    def spine_recent_days(self) -> int:
        """日意图 生成回看天数；0 = 不回看。"""
        return int(_tuning_value("schedule", "spine_recent_days"))

    @property
    def spine_history_messages(self) -> int:
        """日意图 生成回读消息条数；0 = 禁用。"""
        return int(_tuning_value("schedule", "spine_history_messages"))

    @property
    def spine_knowledge_limit(self) -> int:
        """日意图 生成知识库检索条数；0 = 禁用。"""
        return int(_tuning_value("schedule", "spine_knowledge_limit"))


# ---------------------------------------------------------------------------
# [schedule_advanced] 日程系统·高级设置
# ---------------------------------------------------------------------------


class ScheduleAdvancedSectionConfig(PluginConfigBase):
    """调整日程生成、上下文注入、角色判断和历史保留。"""

    __ui_label__ = "日程系统·高级设置"
    __ui_icon__ = "sliders"
    __ui_order__ = 5
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule · Advanced",
               "description": "Deep tuning for generation, role judge, rendering, caches and cleanup."}
    }

    # ── 多轮生成 ─────────────────────────────────────────────
    # 2026-11-04 配置精简轮（J 类）：max_rounds / quality_threshold / generation_timeout /
    # recent_schedule_days / history_message_limit / knowledge_search_limit 移入
    # mappings/internal_tuning.json[schedule_advanced]（内部调参；本段生产现值未证实，
    # 走值搬运不硬编码）。同名只读 property 见类尾。
    # J 类：min_description_length / max_description_length → internal_tuning.json（类尾 property）。
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
        description="修改日程前按人设判断是否接受请求；可修改今天、记录未来预约或拒绝，判定失败时保持日程不变。",
        json_schema_extra={
            "label": "角色裁判模式",
            "hint": "开 = 请求先经角色人格判断（可能被拒绝，更像真人）；"
                    "关 = 记录为今天的候选约定，仅当天重新生成日程时才会纳入",
            **_ui_i18n("Role judge",
                       "On: the bot decides in character (today / future / reject). "
                       "Off: the request is recorded as a candidate commitment dated today; "
                       "it is only picked up if today's schedule is regenerated."),
        },
    )
    # J 类：role_judge_temperature → internal_tuning.json（采样温度，类尾 property）。

    # ── 命令展示 ────────────────────────────────────────────
    list_draw_image: bool = Field(
        default=True,
        description="/lbm_plan list 是否绘制日程图片（内置字体渲染后合并转发）；关闭后只返回文字长文本",
        json_schema_extra={
            "label": "list 绘制图片",
            "hint": "关 = 纯文字，省渲染开销；渲染失败时会自动降级为纯文字",
            **_ui_i18n("Draw list image"),
        },
    )
    # J 类：image_timeout_seconds → internal_tuning.json（渲染超时护栏，类尾 property）。

    # ── 智能注入调优（原 [inject] 段）────────────────────────
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
    # J 类：context_max_turns / context_ttl → internal_tuning.json（类尾 property）。

    # ── 性能与缓存 ───────────────────────────────────────────
    # J 类：inject_cache_minutes / cache_ttl / cache_max_size → internal_tuning.json（类尾 property）。
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

    # ── 问候补发冷静期（§31.1 通则②；R7 = 用户 2026-10-23 第二轮直派，计划书 §31.4）──
    greeting_min_silence_minutes: int = Field(
        default=3,
        ge=0,
        description="补发冷静期：过了掷点才满足条件的补发，须 bot 本会话最近一条消息距今至少 N 分钟才开口；"
                    "窗内一直不满足则到期仍弃。0 = 不要求冷静期（过点即时补发）",
        json_schema_extra={
            "label": "补发冷静期(分钟)",
            "hint": "过点才达标的早/晚安补发前，bot 本会话须 ≥N 分钟未发言；0 = 关闭该冷却",
            "step": 1,
            **_ui_i18n("Backsup silence cooldown (min)",
                       "Post-roll-point make-up greetings require this many minutes since the "
                       "bot's latest own message in that chat; never satisfied → dropped at "
                       "deadline."),
        },
    )

    # ── 赖床 ────────────────────────────────────────────────
    lie_in_max_until: str = Field(
        default="12:00",
        description="睡眠引擎赖床硬上限时刻（HH:MM）：觉醒时间最晚不超过本值（过点必起）；"
                    "格式非法按 12:00 处理",
        json_schema_extra={
            "label": "赖床上限时刻",
            "hint": "HH:MM；最大只能睡到这个点（默认 12:00）",
            "pattern": r"^\d{1,2}:\d{2}$",
            **_ui_i18n("Lie-in hard limit", "Hard cap for engine lie-in (HH:MM)."),
        },
    )

    # ── 后台清理（原 [planning_cleanup] 段）──────────────────
    # J 类：cleanup_interval → internal_tuning.json（后台清理节拍，类尾 property）。
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

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[schedule_advanced]）──
    # 2026-11-04 配置精简轮：16 个内部调参键撤出 TOML/WebUI；旧键值经
    # migrate_internal_tuning 在 SDK merge 前搬入用户表（只补缺失键，不覆盖现场微调）。
    @property
    def max_rounds(self) -> int:
        """多轮生成最大轮数。"""
        return int(_tuning_value("schedule_advanced", "max_rounds"))

    @property
    def quality_threshold(self) -> float:
        """质量阈值：达到该分立即停止重试。"""
        return float(_tuning_value("schedule_advanced", "quality_threshold"))

    @property
    def generation_timeout(self) -> float:
        """日程生成 LLM 调用超时（秒）。"""
        return float(_tuning_value("schedule_advanced", "generation_timeout"))

    @property
    def recent_schedule_days(self) -> int:
        """生成去重回看天数。"""
        return int(_tuning_value("schedule_advanced", "recent_schedule_days"))

    @property
    def history_message_limit(self) -> int:
        """生成灵感回读消息条数；0 = 禁用。"""
        return int(_tuning_value("schedule_advanced", "history_message_limit"))

    @property
    def knowledge_search_limit(self) -> int:
        """生成知识库检索条数；0 = 禁用。"""
        return int(_tuning_value("schedule_advanced", "knowledge_search_limit"))

    @property
    def min_description_length(self) -> int:
        """活动描述最小字符数。"""
        return int(_tuning_value("schedule_advanced", "min_description_length"))

    @property
    def max_description_length(self) -> int:
        """活动描述最大字符数。"""
        return int(_tuning_value("schedule_advanced", "max_description_length"))

    @property
    def role_judge_temperature(self) -> float:
        """角色裁判采样温度。"""
        return float(_tuning_value("schedule_advanced", "role_judge_temperature"))

    @property
    def image_timeout_seconds(self) -> float:
        """日程图片绘制超时（秒）；超时静默降级纯文字。"""
        return float(_tuning_value("schedule_advanced", "image_timeout_seconds"))

    @property
    def context_max_turns(self) -> int:
        """意图判定对话上下文保留轮数。"""
        return int(_tuning_value("schedule_advanced", "context_max_turns"))

    @property
    def context_ttl(self) -> int:
        """对话上下文过期时间（秒）。"""
        return int(_tuning_value("schedule_advanced", "context_ttl"))

    @property
    def inject_cache_minutes(self) -> int:
        """当前活动查询时间窗口缓存（分钟）。"""
        return int(_tuning_value("schedule_advanced", "inject_cache_minutes"))

    @property
    def cache_ttl(self) -> int:
        """日程注入缓存有效期（秒）。"""
        return int(_tuning_value("schedule_advanced", "cache_ttl"))

    @property
    def cache_max_size(self) -> int:
        """注入缓存最大条目数（LRU）。"""
        return int(_tuning_value("schedule_advanced", "cache_max_size"))

    @property
    def cleanup_interval(self) -> int:
        """后台清理循环间隔（秒）。"""
        return int(_tuning_value("schedule_advanced", "cleanup_interval"))


# ---------------------------------------------------------------------------
# [planning_proactive] 日程主动行为
# ---------------------------------------------------------------------------


class PlanningProactiveSectionConfig(PluginConfigBase):
    """设置早/午/晚安问候、发送时间和群聊或私聊名单；想念在陪伴模块设置。

    2026-11-04 主动行为重写新增两个用户可感知字段：``greeting_morning_latest``
    （早安有界上限时刻）与 ``greeting_noon_enabled``（中午问好开关，仅目标陪伴私聊）。
    """

    __ui_label__ = "日程主动行为"
    __ui_icon__ = "notifications_active"
    __ui_order__ = 6
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Schedule Proactive",
               "description": "Morning/noon/evening greetings, send timing and group/private chat lists."}
    }

    # ── 活动频率调控 ─────────────────────────────────────────
    # 2026-10-20 用户指示：日程主动行为整条移除——主动入口只剩早/晚安与陪伴 patrol。
    # 以下两个键已删除：enable_proactive_trigger（活动切换主动开口）、
    # proactive_fresh_window_minutes（触发窗口）；旧配置由 suite_config 迁移器显式丢弃。
    # 2026-10-22 §27.1（R7）：早/晚安分支闸门分离（目标激活强制 / 群聊必要性配置 /
    # 其他私聊阈值恒开口），见上方问候段注释。
    enable_frequency_modulation: bool = Field(
        default=True,
        description="按当前活动类型调节聊天频率：学习/工作时降低发言频率，休息/娱乐时提高",
        json_schema_extra={
            "label": "活动频率调控",
            "hint": "关 = 不让日程影响聊天频率",
            **_ui_i18n("Frequency modulation"),
        },
    )
    # 定稿 §4.5（二期）：目标陪伴私聊的回复因子恒 1.0——只对**实际解析成功**的目标
    # 陪伴私聊生效（``plugin._stream_id`` 非空且 [patrol].target_qq 已配置），本插件
    # 固定 1.0；其他流仍按 activity_properties 的 ``reply_factor``。开关关闭 = 逐字恢复
    # 旧行为（目标流不再补入频率集合，全部按属性因子），不是"把所有流固定 1"。
    target_frequency_always_one: bool = Field(
        default=True,
        description="陪伴目标私聊的回复倍率固定为1.0；关闭后与其他会话一样按活动调整。",
        json_schema_extra={
            "label": "目标私聊频率恒 1",
            "hint": "默认开：陪伴对象私聊不被学习/工作活动压低频率；关 = 全部按旧的活动属性因子",
            **_ui_i18n("Target chat frequency = 1.0",
                       "Keep the target companion private chat at 1.0 regardless of the "
                       "current activity; off = legacy per-activity factors everywhere."),
        },
    )

    # ── 名单系统（群聊 / 私聊）───────────────────────────────
    proactive_group_ids: List[str] = Field(
        default_factory=list,
        description='群聊名单（填**裸群号**如 ["123456"]，也接受 group:/qq:group:（群）'
                    "前缀写法及 qq:group_/group_ 变体，自动取号）："
                    "名单模式=黑名单时这些群不生效、留空则所有群生效；"
                    "名单模式=白名单时只有这些群生效、留空则所有群都不生效。不支持通配 *",
        json_schema_extra={
            "label": "群聊名单",
            "hint": '填群号（裸号或 group:/qq:group: 前缀），例 ["123456"]；'
                    "默认黑名单 + 留空 = 所有群聊都生效",
            **_ui_i18n("Group list",
                       'Group IDs, bare or prefixed (group:/qq:group:); '
                       'default mode = blacklist with empty list = all groups.'),
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
        description='私聊名单（填**裸 QQ 号**如 ["789"]，也接受 private:/qq:private:/qq:（私聊）'
                    "前缀写法及 user: 变体，自动取号）："
                    "名单模式=白名单时只有这些私聊生效、留空则所有私聊都不生效；"
                    "名单模式=黑名单时这些私聊不生效。",
        json_schema_extra={
            "label": "私聊名单",
            "hint": '填 QQ 号（裸号或 private:/qq:private:/qq: 前缀）；'
                    "默认白名单 + 留空 = 其他私聊一概不主动，只有陪伴对象私聊照常",
            **_ui_i18n("Private list",
                       'QQ IDs, bare or prefixed (private:/qq:private:/qq:); '
                       'default mode = whitelist with empty list = no other privates.'),
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
        description="在起床时间加随机漂移后，向符合条件的会话发送早安；日程缺失时使用配置作息。",
        json_schema_extra={
            "label": "启用早安",
            "hint": "关 = 完全不发早安（含目标私聊与群聊）",
            **_ui_i18n("Morning greeting", "Off: no morning greetings anywhere."),
        },
    )
    greeting_morning_silence_hours: float = Field(
        default=2.0,
        ge=0,
        description="早安沉默闸（小时）：今日第一日程（= 周期起点）前 N 小时内该会话 bot 自己发过消息"
                    "→ 该会话本周期跳过早安（不掷点、不建条目）。0 = 禁用该闸门；查询异常按不过处理（宁不发）",
        json_schema_extra={
            "label": "早安沉默闸(小时)",
            "hint": "第一日程前 N 小时内 bot 发过消息就不发早安；0 = 关闭；默认 2",
            **_ui_i18n("Morning silence gate (h)",
                       "Skip the morning greeting if the bot spoke within N hours before "
                       "the first schedule item; 0 disables."),
        },
    )
    greeting_morning_latest: str = Field(
        default="10:00",
        description="早安最晚时刻（HH:MM，本地时区）：超过该时刻不再发送早安（三流同口径）。"
                    "生效上限 = 本时刻与「起床后 2 小时」取早；晚醒导致起床已过上限时当日不发早安。"
                    "空值或非法格式回退默认 10:00（不放宽）",
        json_schema_extra={
            "label": "早安最晚时刻",
            "hint": "HH:MM；过了这个点就不道早安了（与醒后2小时取早）；默认 10:00",
            "pattern": r"^\d{1,2}:\d{2}$",
            **_ui_i18n("Morning greeting latest",
                       "HH:MM cap for morning greetings (earlier of this and wake+2h); "
                       "empty/invalid falls back to 10:00."),
        },
    )
    greeting_night_enabled: bool = Field(
        default=True,
        description="在入睡时间前的随机漂移窗口，向符合条件的会话发送晚安；日程缺失时使用配置作息。",
        json_schema_extra={
            "label": "启用晚安",
            "hint": "关 = 完全不发晚安（含目标私聊与群聊）",
            **_ui_i18n("Night greeting", "Off: no night greetings anywhere."),
        },
    )
    greeting_noon_enabled: bool = Field(
        default=True,
        description="每天本地12:00–13:00向陪伴目标私聊问候一次；睡眠期间暂缓，窗口结束后跳过。",
        json_schema_extra={
            "label": "启用中午问好",
            "hint": "开 = 每天中午在陪伴对象私聊问一声午安（仅目标私聊，每日一次）；关 = 不发",
            **_ui_i18n("Noon greeting",
                       "On: one daily noon greeting in the companion target private chat "
                       "(12:00-13:00 window, budget-exempt, deduplicated)."),
        },
    )
    morning_drift: str = Field(
        default="10-30",
        description="早安漂移区间（分钟，相对起床锚点）：填 \"a-b\" 或单数字 N（= 0~N 随机）；"
                    "触发时刻 = 起床锚点 + 随机漂移。非法值回退 10-30",
        json_schema_extra={
            "label": "早安漂移(分钟)",
            "hint": '例 "10-30" 或 "20"（= 0~20 随机）',
            "pattern": r"^\d+(-\d+)?$",
            **_ui_i18n("Morning drift (min)", 'e.g. "10-30" or "20"'),
        },
    )
    night_drift: str = Field(
        default="10-30",
        description="晚安提前时间（分钟）：a-b表示随机区间，N表示0至N；非法值使用10-30。",
        json_schema_extra={
            "label": "晚安漂移(分钟)",
            "hint": '例 "10-30" 或 "20"；在睡前界前 10~30 分钟内随机开口'
                    "（睡前界 = 真实入睡锚点，回退 [schedule] sleep_time）",
            "pattern": r"^\d+(-\d+)?$",
            **_ui_i18n("Night drift (min)", 'e.g. "10-30" or "20"'),
        },
    )
    # 2026-10-22 用户裁定回归（R7 依据 = 用户 2026-10-22 第二批四直派，计划书 §二十七）：
    # 恢复 greeting_require_activation 为真配置键，但**只作用于群聊分支**——§31.1（R7 = 用户
    # 2026-10-23 第二轮直派）起语义收窄为「群聊早安必要性检查」：底层硬闸（判定窗
    # [上份日程开始, 本份日程开始) 窗内非 bot 消息 ≥1）无条件生效；本开关开 → 在硬闸之上
    # 额外判定「今天日程第一个条目时段内非 bot 消息 ≥1」（AND）；关 → 不判。目标私聊
    # 新口径**无条件**、其他私聊走阈值判定，均不受本开关控制。
    greeting_require_activation: bool = Field(
        default=True,
        description="群聊早安必要性检查：硬闸（判定窗内非 bot 消息 ≥1）之上的额外判定——"
                    "「今天日程第一个条目时段内有其他用户主动开口」（非 bot 消息 ≥1），"
                    "关 = 不判该条（只影响群聊早安；目标私聊与其他私聊不受此开关控制）",
        json_schema_extra={
            "label": "群聊早安必要性检查",
            "hint": "默认开：今天日程第一个条目时段内群里没人开口就不发早安；关 = 只看硬闸",
            **_ui_i18n("Group greeting necessity check",
                       "On: a group's morning greeting additionally requires someone speaking "
                       "inside the first schedule item's time slot today."),
        },
    )
    greeting_other_private_min_messages: int = Field(
        default=5,
        ge=1,
        description="其他私聊**早安**门槛（条）：早安判定窗 = [上份日程开始, 本份日程开始)——"
                    "窗内**由对方发送**的消息数达到该值才可能收到早安（bot 自身消息不计入；"
                    "至少 1 条，0/负数被拒绝）；晚安数量阈值另见 greeting_private_night_min_messages；"
                    "取不到消息计数按 0 处理（不发送）",
        json_schema_extra={
            "label": "私聊早安门槛(条)",
            "hint": "防打扰护栏：判定窗内对方没发够 5 条就不主动问早安（可设 1 条）",
            **_ui_i18n("Other-private morning threshold (msgs)",
                       "Counts only messages sent by the other party within "
                       "[previous cycle start, current cycle start)."),
        },
    )
    greeting_private_night_min_messages: int = Field(
        default=5,
        ge=1,
        description="其他私聊**晚安**数量阈值（条）：晚安判定窗 = [本轮日程开始, 晚安结束)——"
                    "窗内**由对方发送**的消息数达到该值即视为通过（与早安连锁二选一，OR；"
                    "至少 1 条，0/负数被拒绝）；取不到消息计数按 0 处理（不发送）",
        json_schema_extra={
            "label": "私聊晚安阈值(条)",
            "hint": "晚安二选一之一：本轮日程开始后对方发够 N 条即通过；另一路 = 本轮已对"
                    "该流道过早安（连锁）",
            **_ui_i18n("Other-private night threshold (msgs)",
                       "One branch of the night OR gate: counterparty messages >= N within "
                       "[current cycle start, night deadline)."),
        },
    )

    # PR8 3.0.0：``nap_reminder_enabled`` / ``nap_reminder_max_per_day`` 两键已退役
    # （赖床提醒链随 UQ-3/裁定 3 清退：引擎觉醒一律走阈值 + lie_in 上限，不再发赖床提醒；
    # 依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP3。旧消费者
    # emotion/service._nap_cfg 经 getattr 带默认读取，缺键安全降级）。

    # ── 兼容位（旧问候开关/四名单）已彻底移除（2026-10-10 第二轮，用户要求）──
    # 移除理由：隐藏兼容位运行期早已不再参与判定（问候走两个总开关 + 统一名单），但用户
    # 在 WebUI/配置文件里看到/填了它们会"看起来配了黑名单却没生效"（B2 的成因之一）。
    # 旧值仍在**首轮迁移**里被读取并并入统一名单（见 _migrate_legacy_lists：它直接读原始
    # 输入字典，不依赖本段有没有声明这些字段），迁移后这些键在落盘结果中自然消失。
    # ── 旧键迁移（段级；在聚合校验器里与其它段一起被调用）──────
    @pydantic.model_validator(mode="before")
    @classmethod
    def _migrate_legacy_lists(cls, data: object) -> object:
        """旧名单/开关 → 新统一名单系统（2026-10-10 合并；**只在首轮迁移生效**）。

        **只读旧键、不再声明旧字段**（2026-10-10 第二轮）：下面出现的旧键都已从模型删除。
        **真正被读取**（旧值搬进统一名单/门槛，随后被 pop 或随落盘消失）的是：
        ``greeting_group_blacklist`` / ``greeting_group_whitelist`` /
        ``greeting_other_private_whitelist`` / ``greeting_other_private_blacklist`` /
        ``greeting_other_private_enabled`` / ``morning_other_private_enabled`` /
        ``morning_other_private_min_messages`` / ``proactive_other_streams``。
        **``greeting_target_enabled`` / ``greeting_group_enabled`` 不读也不搬**（T3 问题表 #2，
        用户裁决 A）：旧的显式 ``false`` **不保留**，升级后按新默认（开）生效——要停掉某类问候
        请改统一名单与两个总开关。

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
# [life_state] 生活状态（滚动化改造新增段，默认关）
# ---------------------------------------------------------------------------


class LifeStateSectionConfig(PluginConfigBase):
    """设置精力、饥饿、专注、压力、社交状态及睡眠恢复。"""

    __ui_label__ = "生活状态"
    __ui_icon__ = "activity"
    __ui_order__ = 8
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Life State (rolling)",
               "description": "Energy/hunger/focus/stress drives and the rolling planner."}
    }

    # 2026-11-04 配置精简轮（H 类）：tick_seconds 固化为模块常量 LIFE_TICK_SECONDS=300
    # （生产现值=默认已证实；内部轮询粒度非时刻锚点）。旧 TOML 键经 LEGACY_ROUTE_KEYS
    # 入口 pop（只 pop 不搬运，无值可丢）。
    # J 类：horizon_minutes → internal_tuning.json[life_state]（类尾 property）。
    replan_cooldown_minutes: int = Field(
        default=20,
        ge=5,
        description="两次重规划的最小间隔（分钟）",
        json_schema_extra={
            "label": "重规划冷却(分钟)",
            **_ui_i18n("Replan cooldown (min)"),
        },
    )
    max_replans_per_hour: int = Field(
        default=2,
        ge=1,
        description="每小时最多重规划次数",
        json_schema_extra={
            "label": "每小时重规划上限",
            **_ui_i18n("Replans per hour"),
        },
    )
    random_event_rate: float = Field(
        default=0.08,
        ge=0.0,
        le=0.2,
        description="随机小事件概率（只在转场/空闲时掷骰；0.05-0.08 建议）",
        json_schema_extra={
            "label": "随机事件概率",
            "step": 0.01,
            **_ui_i18n("Random event rate"),
        },
    )
    max_random_events_per_day: int = Field(
        default=3,
        ge=0,
        description="每天随机小事件上限；0 = 禁用随机事件",
        json_schema_extra={
            "label": "每日随机事件上限",
            **_ui_i18n("Random events per day"),
        },
    )
    # J 类：resume_pool_size / resume_expire_minutes / low_energy_threshold /
    # high_hunger_threshold / high_stress_threshold / brief_attention_progress /
    # late_attention_progress → internal_tuning.json[life_state]（类尾 property）。
    # PR8 3.0.0：``profile_path`` 死键已退役（画像装载固定走
    # activity_profiles.load_and_apply(root_dir) → mappings 链，表位置固定；
    # 依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP3）。
    # H 类（2026-11-04）：state_file 固化为 planning.life_state.STATE_FILE_NAME
    # （逐字 'life_state.json'——改名即丢生产状态；生产现值=默认已证实）。
    # 旧 TOML 键经 LEGACY_ROUTE_KEYS 入口 pop。
    keep_episode_days: int = Field(
        default=14,
        ge=1,
        description="活动 episode 日志保留天数（超过按日清理）",
        json_schema_extra={
            "label": "episode 保留天数",
            **_ui_i18n("Keep episode days"),
        },
    )
    # PR2（2026-10-26），计划书 §三十四/定稿 §4.1.1（R7 依据 = 用户 2026-10-26 直派
    # 【日程系统二期 PR2 双向闭环·调制侧】）：情绪 → 生活状态调制开关。false（默认）
    # 时不读 emotion VAD、不构造调制服务、modulation=None（逐字旧行为）；true 时生效
    # VAD 经 EMA/中性死区折算五项 rise/fall 速率增益（系数表
    # mappings/emotion_life_modulation.json，经种子链装载、缺失/坏表按内置默认=定稿表，
    # 不设路径键——学习 profile_path 死键教训，表位置固定走 mappings 链）。
    # 生活状态结算模型开关（2026-11-03 优化方案 §3/§5 + 实施硬要求 1；R7 依据 = 用户
    # 2026-11-03 直派【生活状态结算模型实现】）。"v1"（默认）= 既有线性速率结算逐字不变；
    # "v2" = 目标趋近模型（纯函数层 planning.settlement_v2，参数版本化）。新旧模型
    # 互斥：同一状态同一时刻只走其一；v2 下情绪调制不消费（modulation 强制 None）。
    # 档位热重载即生效（下一拍结算切换），不重置/不改写历史状态数值；启用前应先跑
    # union/work/20261103_state_balance/verify_v2.py 离线回放（方案 §5.2/§5.3）。
    # v2 参数覆盖（实施硬要求 2/7：候选参数集中且版本化；开发文档 §5.6 dict 字段
    # 推荐「字符串 + json.loads」）。空 = 内置默认 planning.settlement_v2.V2_PARAMS
    # （版本 20261103.1，含方案 §3 候选：休息精力目标 0.82、睡眠目标 0.92、
    # 进食后饥饿 0.15 等）；JSON 对象仅收已知键（未知键丢弃并告警），activity
    # 子表按 画像 → 轴 → [目标(0~1), 时间常数(分钟)] 逐项合并。
    settlement_params_json: str = Field(
        default="",
        description="覆盖生活状态结算参数的JSON对象；留空使用内置值，未知键忽略。",
        json_schema_extra={
            "label": "结算参数(JSON)",
            "hint": '例：{"sleep_energy_target": 0.90, "activity": {"rest": {"energy": [0.85, 60]}}}',
            **_ui_i18n("Settlement params (JSON)",
                       "JSON object overriding planning.settlement_v2.V2_PARAMS; empty = built-in defaults."),
        },
    )
    # PR3 SleepEngine（2026-10-26，定稿 §4.2 + 附录 B.6.3；R7 依据 = 用户直派
    # 【日程系统二期 PR3】）：睡眠键组全部出厂值。PR8 3.0.0：``sleep_engine_enabled``
    # 开关键已退役（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP1/WP3）
    # ——睡眠引擎随 ``[schedule].enabled`` 唯一总开关生效或停摆。
    sleep_drift_max_minutes: int = Field(
        default=45, ge=0, le=90,
        description="动态睡眠下自然入睡和起床可偏离作息时间的最大分钟数。",
        json_schema_extra={"label": "作息浮动上限（分钟）", **_ui_i18n("Sleep drift limit (minutes)")},
    )
    wake_energy_threshold: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description="觉醒精力阈值：到最早起床时间后精力达到该值即醒（默认 0.75）",
        json_schema_extra={
            "label": "觉醒精力阈值",
            "step": 0.05,
            **_ui_i18n("Wake energy threshold"),
        },
    )
    sleep_oversleep_start_hours: float = Field(
        default=9.0,
        ge=8.0,
        le=12.0,
        description="过睡起点（小时）：超过该连续睡眠时长后精力转为负收益（默认 9）",
        json_schema_extra={
            "label": "过睡起点(小时)",
            "step": 0.5,
            **_ui_i18n("Oversleep starts (h)"),
        },
    )
    # J 类：sleep_interrupt_min_minutes → internal_tuning.json[life_state]（打断确认
    # 去抖阈值，内部机制非锚点；类尾 property）。
    # H 类（2026-11-04）：sleep_wake_check_seconds 固化为 planning.sleep_engine.
    # DEFAULT_WAKE_CHECK_SECONDS=300（生产现值=默认已证实；到点/硬约定明确豁免此
    # 复检粒度，无语义风险）。旧 TOML 键经 LEGACY_ROUTE_KEYS 入口 pop。
    sleep_hard_commitment_lead_minutes: int = Field(
        default=30,
        ge=0,
        le=240,
        description="硬约定提前唤醒（分钟，默认 30）：约定开始前该时长必须醒",
        json_schema_extra={
            "label": "硬约定提前(分钟)",
            **_ui_i18n("Hard commitment lead (min)"),
        },
    )
    sleep_oversleep_penalize_focus: bool = Field(
        default=False,
        description="过睡是否扣专注（默认关；定稿锁定「睡太久默认只影响精力」，开启后才用曲线的专注负收益）",
        json_schema_extra={
            "label": "过睡扣专注",
            "hint": "关 = 睡太久只扣精力，专注不变；开 = 同时按曲线扣专注",
            **_ui_i18n("Oversleep penalizes focus",
                       "Off = oversleep only drains energy."),
        },
    )

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[life_state]）────
    @property
    def horizon_minutes(self) -> int:
        """滚动规划窗长（分钟）。"""
        return int(_tuning_value("life_state", "horizon_minutes"))

    @property
    def resume_pool_size(self) -> int:
        """恢复池容量；0 = 不恢复。"""
        return int(_tuning_value("life_state", "resume_pool_size"))

    @property
    def resume_expire_minutes(self) -> int:
        """恢复池条目过期时间（分钟）。"""
        return int(_tuning_value("life_state", "resume_expire_minutes"))

    @property
    def low_energy_threshold(self) -> float:
        """低精力阈值。"""
        return float(_tuning_value("life_state", "low_energy_threshold"))

    @property
    def high_hunger_threshold(self) -> float:
        """高饥饿阈值。"""
        return float(_tuning_value("life_state", "high_hunger_threshold"))

    @property
    def high_stress_threshold(self) -> float:
        """高压力阈值。"""
        return float(_tuning_value("life_state", "high_stress_threshold"))

    @property
    def brief_attention_progress(self) -> float:
        """简短回复进度阈值。"""
        return float(_tuning_value("life_state", "brief_attention_progress"))

    @property
    def late_attention_progress(self) -> float:
        """稍后细说进度阈值。"""
        return float(_tuning_value("life_state", "late_attention_progress"))

    @property
    def sleep_interrupt_min_minutes(self) -> int:
        """打断确认去抖阈值（分钟）。"""
        return int(_tuning_value("life_state", "sleep_interrupt_min_minutes"))


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
