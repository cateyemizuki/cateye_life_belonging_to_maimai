"""companion 子模块配置段（来源：虚拟恋人插件 mai-love config.py）。

只定义 section 类，不定义聚合模型——聚合由顶层 config.py 的 CateyeSuiteConfig 负责：
- PatrolSectionConfig          挂为顶层 [patrol] 段（演进版，在骨架字段上补齐源插件能力）；
- AffectionSectionConfig       挂为顶层 [affection] 段（照搬）；
- CateyeScreenSectionConfig    挂为顶层 [cateye] 段（新增：恋人电脑屏幕感知联动）；
- JudgeSectionConfig           挂为顶层 [judge] 段（mai-love v2.6.3：判定类 LLM 调用参数）。

不建 [whitelist] 段——鉴权统一读顶层 [target] 段的 target_qq；
不建 [llm_log]/[proactive_log] 段——复用顶层 [admin] 段的
llm_log_enabled / decision_log_enabled / log_retention_days。

WebUI 元数据约定与顶层 config.py 一致：
- 每个字段带中文短标题 label + 英文 i18n + hint；
- description 写完整中文说明，作为界面未命中翻译时的回退文案。
"""

from __future__ import annotations

from typing import ClassVar, Dict, Literal

from maibot_sdk import Field, PluginConfigBase


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；与顶层 config.py 同一写法）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


# ---------------------------------------------------------------------------
# 默认提示词 / 文案模板（源插件 constants.py；config 字段默认值与运行时回退共用）
# ---------------------------------------------------------------------------

#: 想念触发时传给 planner 的 reason（占位符：{lover_name} / {relation} / {hours}）
MISS_REASON_PROMPT_DEFAULT = (
    "你已经有 {hours} 个小时没收到{relation}的消息了，你有点想TA了。"
    "可以主动开口问问近况、表达一下想念；但不必强行找话题，"
    "如果觉得此刻开口不自然，平淡地打个招呼也可以。"
)

#: 想念触发前 LLM 把关提示词（占位符：{lover_name} / {relation} / {personality} /
#: {current_time} / {hours} / {activity_context}；回复 Y 才放行）
MISS_CONFIRM_PROMPT_DEFAULT = (
    "你是“{lover_name}”，而对方是你的{relation}（人设：{personality}）。"
    "现在是 {current_time}，你上次收到TA的消息已经是 {hours} 小时前。{activity_context}\n"
    "请站在“{lover_name}”的角度判断：此刻主动发一条关心TA近况的消息"
    "是否自然、是否体贴？\n"
    "- 深夜TA可能睡了、TA可能在忙、或你觉得突兀 → 回复 N\n"
    "- 你确实想TA了、此刻开口很自然 → 回复 Y\n"
    "只回复一个大写字母：Y 或 N。"
)

#: 恋人电脑屏幕截图的视觉理解提示词（随截图发给 vlm 任务）
SCREEN_DESCRIBE_PROMPT_DEFAULT = (
    "这是你的恋人电脑屏幕的截图。请用一句话（不超过 40 字、口语化）描述"
    "TA现在可能在干什么（比如在打游戏 / 写代码 / 看视频 / 挂机离开）。\n"
    "如果截图上看不出具体内容，就回答：屏幕上看不出具体内容。"
)

#: 屏幕旁白文案模板（占位符：{date} 年月日 / {time} 时分 / {user_name} / {description}）
SCREEN_NARRATION_TEMPLATE_DEFAULT = (
    "现在是{date} {time}，你看了眼{user_name}的电脑屏幕，TA正在：{description}"
)
SCREEN_OFFLINE_NARRATION_TEMPLATE_DEFAULT = (
    "现在是{date} {time}，你想看一眼{user_name}的电脑屏幕，但TA的电脑没开"
)
SCREEN_FAILED_NARRATION_TEMPLATE_DEFAULT = (
    "现在是{date} {time}，你看了眼{user_name}的电脑屏幕，但没看清TA在干什么"
)


# ---------------------------------------------------------------------------
# [patrol] 主动巡检（源插件 [schedule] + [probability] + [time_windows] 三段合并）
# ---------------------------------------------------------------------------


class PatrolSectionConfig(PluginConfigBase):
    """主动巡检（来源：虚拟恋人插件）。"""

    __ui_label__ = "主动巡检"
    __ui_icon__ = "radar"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Patrol", "description": "Background loop that decides when to proactively start a chat."}
    }

    # ── 模块独立总开关 ───────────────────────────────────────
    enabled: bool = Field(
        default=True,
        description="恋人模块总开关：关闭后停止巡检主动聊天与情绪上下文注入（命令与工具返回未启用提示）",
        json_schema_extra={
            "label": "启用恋人模块",
            "hint": "每个业务模块都有独立总开关；关闭不影响其他模块",
            **_ui_i18n("Enable companion module", "Master switch of the companion module."),
        },
    )
    # ── 关系类型（你是 bot 的谁）────────────────────────────
    relation_type: Literal["恋人", "挚友", "维护者", "家人", "自定义"] = Field(
        default="恋人",
        description="目标用户是 bot 的谁：影响想念提示词、消息口吻、好感度档位名称与消息后缀",
        json_schema_extra={
            "label": "关系类型",
            "hint": "恋人=甜蜜亲昵；挚友=轻松直来直去；维护者=信赖依赖；家人=随性亲近；自定义=用下方称谓",
            **_ui_i18n("Relation type", "Who the target user is to the bot."),
        },
    )
    relation_custom: str = Field(
        default="",
        description="自定义关系的称谓（关系类型选「自定义」时生效，如：姐姐、师兄、搭子）",
        json_schema_extra={
            "label": "自定义称谓",
            "hint": "留空时回退「重要的人」",
            "placeholder": "姐姐",
            **_ui_i18n("Custom relation label", "Used when relation type is 自定义."),
        },
    )

    check_interval_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="巡检循环间隔（分钟）：每隔多久判断一次该不该主动说话",
        json_schema_extra={"label": "巡检间隔(分钟)", **_ui_i18n("Check interval (min)")},
    )
    proactive_trigger_enabled: bool = Field(
        default=True,
        description="触发方式开关：开启=经 maisaka.proactive.trigger 唤醒 planner（人格化表达）；关闭=完全不主动",
        json_schema_extra={"label": "主动触发开关", **_ui_i18n("Proactive trigger")},
    )
    daily_max_speak: int = Field(
        default=5,
        ge=0,
        le=50,
        description="每日主动发言预算（次）；未发晚安时为晚安预留 1 次。0=完全静音",
        json_schema_extra={"label": "每日预算", **_ui_i18n("Daily budget")},
    )
    global_proactive_cap: int = Field(
        default=0,
        ge=0,
        le=200,
        description="全渠道每日主动打扰上限（次）：私聊主动 + 空间互动提醒共享；0=自动取「每日预算+2」",
        json_schema_extra={
            "label": "全渠道打扰上限",
            "hint": "0=自动取「每日预算 + 2」；大于 0 时直接采用该值（patrol/planning/空间提醒共享同一计数）",
            **_ui_i18n("Global proactive cap",
                       "0 = daily budget + 2. Shared daily cap for all proactive interruptions "
                       "(patrol / planning / qzone reminders)."),
        },
    )
    min_trigger_interval_minutes: int = Field(
        default=240,
        ge=0,
        le=1440,
        description="任意两次主动发言之间的硬性最小间隔（分钟），0=不限制；对全部触发生效（早晚安/想念可用下面开关豁免）",
        json_schema_extra={"label": "最小间隔(分钟)", **_ui_i18n("Min interval (min)")},
    )
    min_interval_exempt_greetings: bool = Field(
        default=False,
        description="早安/晚安是否豁免上面的「主动发言最小间隔」；打开后无论间隔多短都保证每天早晚各一条",
        json_schema_extra={
            "label": "早晚安豁免最小间隔",
            "hint": "打开后早安/晚安不受最小间隔限制（静默时段与每日预算仍然生效）",
            **_ui_i18n("Exempt greetings from min interval",
                       "Morning/night greetings ignore the minimum interval (silence window and daily cap still apply)."),
        },
    )
    min_interval_exempt_miss: bool = Field(
        default=False,
        description="想念是否豁免「主动发言最小间隔」；想念自身有沉默时长门槛，豁免可避免「当天已主动发言就永远说不了想你」",
        json_schema_extra={
            "label": "想念豁免最小间隔",
            "hint": "打开后想念不受最小间隔限制（沉默门槛、每日预算、用户冷却、LLM 把关仍然生效）",
            **_ui_i18n("Exempt missing from min interval",
                       "The miss trigger ignores the minimum interval; its own gates still apply."),
        },
    )
    user_cooldown_minutes: int = Field(
        default=30,
        ge=0,
        le=180,
        description="用户冷却（分钟）：对方最后一次发言后多久内不主动打扰（想念/节点/日常受控，早晚安不受控）",
        json_schema_extra={"label": "用户冷却(分钟)", **_ui_i18n("User cooldown (min)")},
    )
    morning_start: str = Field(
        default="07:00",
        description="早安触发窗口开始（HH:MM）",
        json_schema_extra={"label": "早安开始", **_ui_i18n("Morning window start")},
    )
    morning_end: str = Field(
        default="10:00",
        description="早安触发窗口结束（HH:MM）",
        json_schema_extra={"label": "早安结束", **_ui_i18n("Morning window end")},
    )
    night_start: str = Field(
        default="22:00",
        description="晚安触发窗口开始（HH:MM）",
        json_schema_extra={"label": "晚安开始", **_ui_i18n("Night window start")},
    )
    night_end: str = Field(
        default="23:59",
        description="晚安触发窗口结束（HH:MM）",
        json_schema_extra={"label": "晚安结束", **_ui_i18n("Night window end")},
    )
    silence_start: str = Field(
        default="00:00",
        description="静默时段开始（HH:MM），静默期内巡检不触发（支持跨午夜；配置非法按静默处理）",
        json_schema_extra={"label": "静默开始", **_ui_i18n("Silence start")},
    )
    silence_end: str = Field(
        default="07:30",
        description="静默时段结束（HH:MM）",
        json_schema_extra={"label": "静默结束", **_ui_i18n("Silence end")},
    )
    default_speak_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="日常搭话触发概率（巡检命中且无当前活动时）",
        json_schema_extra={"label": "日常概率", "step": 0.05, **_ui_i18n("Daily rate")},
    )
    activity_trigger_rate: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="日程节点分享概率（巡检命中当前日程活动时）",
        json_schema_extra={"label": "节点概率", "step": 0.05, **_ui_i18n("Activity rate")},
    )
    miss_enabled: bool = Field(
        default=True,
        description="想念机制：目标用户沉默超过阈值后按概率触发想念发言（每天最多一次）",
        json_schema_extra={"label": "想念机制", **_ui_i18n("Missing mechanism")},
    )
    miss_hours_min: float = Field(
        default=4.0,
        ge=0.5,
        le=72.0,
        description="想念触发区间下限（小时）：用户沉默不足该时长绝不触发；写反时自动交换",
        json_schema_extra={"label": "想念阈值下限(小时)", "step": 0.5, **_ui_i18n("Miss threshold min (h)")},
    )
    miss_hours_max: float = Field(
        default=8.0,
        ge=0.5,
        le=72.0,
        description="想念触发区间上限（小时）：区间内每轮巡检随机取阈值，沉默越久越容易触发",
        json_schema_extra={"label": "想念阈值上限(小时)", "step": 0.5, **_ui_i18n("Miss threshold max (h)")},
    )
    miss_speak_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="想念触发概率",
        json_schema_extra={"label": "想念概率", "step": 0.05, **_ui_i18n("Miss rate")},
    )
    miss_avoid_future_schedule: bool = Field(
        default=False,
        description="想念是否避开未来日程：开启后未来窗口内有日程节点则本轮不打扰（默认关闭=不看日程）",
        json_schema_extra={
            "label": "想念避开未来日程",
            "hint": "日程密集时打开会让想念几乎永不触发",
            **_ui_i18n("Miss avoids future schedule",
                       "Skip the miss trigger when a schedule activity starts within the window below. Off by default."),
        },
    )
    miss_future_schedule_hours: float = Field(
        default=2.0,
        ge=0.0,
        le=72.0,
        description="「想念避开未来日程」的窗口长度（小时）：未来这么多小时内有日程节点就不打扰",
        json_schema_extra={"label": "想念避开日程窗口(小时)", "step": 0.5, **_ui_i18n("Miss future schedule window (h)")},
    )
    miss_llm_check_enabled: bool = Field(
        default=True,
        description="想念触发前用 LLM 以角色身份把关（回答 Y 才触发），驳回后 30 分钟内不再重复判断",
        json_schema_extra={"label": "想念 LLM 把关", **_ui_i18n("Miss LLM gate")},
    )
    miss_reason_prompt: str = Field(
        default=MISS_REASON_PROMPT_DEFAULT,
        description="想念触发时传给 planner 的提示文本（reason）。可用占位符：{lover_name}、{hours}",
        json_schema_extra={
            "label": "想念触发提示词",
            "rows": 3,
            **_ui_i18n("Miss reason prompt",
                       "Sent to the planner when a miss trigger fires. Placeholders: {lover_name}, {hours}."),
        },
    )
    miss_confirm_prompt: str = Field(
        default=MISS_CONFIRM_PROMPT_DEFAULT,
        description="想念触发前 LLM 把关的提示词模板（回复 Y 才触发）。占位符："
                    "{lover_name}、{personality}、{current_time}、{hours}、{activity_context}",
        json_schema_extra={
            "label": "想念把关提示词",
            "rows": 5,
            **_ui_i18n("Miss confirm prompt",
                       "LLM gate template; must answer Y to proceed. Placeholders: {lover_name}, {personality}, etc."),
        },
    )


# ---------------------------------------------------------------------------
# [affection] 好感度（照搬源插件，仅一个字段）
# ---------------------------------------------------------------------------


class AffectionSectionConfig(PluginConfigBase):
    """好感度系统（来源：虚拟恋人插件；M1 扩展为 v2 状态层配置，全部带默认值可关闭）。

    `enabled=false` 时整体回退 v1 三档行为（§八.2：新增配置关闭时回退旧行为）；
    `review_*` 四项为 M2 好感 LLM 裁判（会话采样 + 关系日结）的开关与频控/任务名。
    """

    __ui_label__ = "好感度"
    __ui_icon__ = "favorite"
    __ui_order__ = 4
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Affection", "description": "Companion affection tier system (v2 score/baseline)."}
    }

    current_level: Literal[0, 1, 2] = Field(
        default=0,
        description="好感度档位：0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱）",
        json_schema_extra={"label": "好感档位", **_ui_i18n("Affection level")},
    )
    enabled: bool = Field(
        default=True,
        description="启用好感度 v2：开启后按 score/baseline 连续分运行（规则事件记账 + 日衰减 + 档位迟滞）；"
                    "关闭则回退 v1 三档行为（档位仅由命令/工具设置，不自动变化）",
        json_schema_extra={
            "label": "启用好感度 v2",
            "hint": "关闭=保留旧三档手感（无日衰减、无事件记账）；文件仍以 v2 格式保存，可随时再开启",
            **_ui_i18n("Enable affection v2",
                       "Off = legacy 3-tier behaviour: tier only changes via commands, no decay or rule events."),
        },
    )
    decay_pull: float = Field(
        default=0.06,
        ge=0.0,
        le=1.0,
        description="日衰减拉回基线的比例：每天日切执行 score += (baseline − score) × 该值（0=不衰减）",
        json_schema_extra={
            "label": "日衰减拉回",
            "step": 0.01,
            "hint": "越大=一次甜蜜日忘得越快；0.06 约 5~6 天回落大半",
            **_ui_i18n("Daily decay pull", "0 = no decay. 0.06 pulls most of a +2 day back within about a week."),
        },
    )
    baseline_ema: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description="关系基线漂移速度：日切执行 baseline += (score − baseline) × 该值（0=基线永不移动）",
        json_schema_extra={
            "label": "基线漂移速度",
            "step": 0.01,
            "hint": "基线是「感情底色」：涨得慢、忘得慢；0.03 表示长期相处才会抬升",
            **_ui_i18n("Baseline EMA", "How fast the long-term baseline follows the recent score. 0 = frozen."),
        },
    )
    daily_positive_cap: float = Field(
        default=4.0,
        ge=0.0,
        le=100.0,
        description="单日正向好感增量上限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日正向上限",
            "step": 0.5,
            "hint": "防止一天之内好感暴涨；设为 0 = 当日不再增加好感",
            **_ui_i18n("Daily positive cap", "Hard daily ceiling for positive affection gains."),
        },
    )
    daily_negative_cap: float = Field(
        default=3.0,
        ge=0.0,
        le=100.0,
        description="单日负向好感扣减下限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日负向下限",
            "step": 0.5,
            "hint": "防止一次矛盾把好感打崩；设为 0 = 当日不再扣好感",
            **_ui_i18n("Daily negative cap", "Hard daily floor for negative affection losses."),
        },
    )
    tier_hysteresis: float = Field(
        default=3.0,
        ge=0.0,
        le=30.0,
        description="档位迟滞带（分）：升档需 score ≥ 当前档上界 + 该值，降档需 ≤ 下界 − 该值（0=无迟滞）",
        json_schema_extra={
            "label": "档位迟滞带",
            "step": 0.5,
            "hint": "防止在档位边界反复横跳",
            **_ui_i18n("Tier hysteresis", "Extra margin required before a tier change; prevents oscillation."),
        },
    )
    tier_min_dwell_hours: float = Field(
        default=24.0,
        ge=0.0,
        le=720.0,
        description="档位最短停留时长（小时）：距进入当前档位不足该时长则不升/降档（0=不限）",
        json_schema_extra={
            "label": "档位最短停留(小时)",
            "step": 1.0,
            "hint": "档位变化最多一天一次，避免情绪化跳档",
            **_ui_i18n("Min tier dwell (h)", "Minimum hours in a tier before it may change again."),
        },
    )
    review_enabled: bool = Field(
        default=True,
        description="好感度 LLM 裁判总开关（M2：会话采样评估 + 睡眠时段关系日结）；"
                    "关闭则完全不做 LLM 评估（规则事件仍照常记账）",
        json_schema_extra={
            "label": "好感裁判开关",
            "hint": "关闭=零 LLM 成本：不做会话采样评估，也不做关系日结；规则事件与日衰减照常",
            **_ui_i18n("Affection review", "M2: sample-based conversation review and nightly relationship review."),
        },
    )
    review_sample_every_rounds: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="会话裁判采样频率（轮）：每多少轮对方私聊消息抽样一次（与每日上限取先到者）",
        json_schema_extra={
            "label": "会话采样轮数",
            "hint": "越小抽样越勤、LLM 成本越高；默认 20 轮一次",
            **_ui_i18n("Review sample rounds", "M2: one sample review every N conversation rounds."),
        },
    )
    review_max_per_day: int = Field(
        default=2,
        ge=0,
        le=50,
        description="会话裁判每日上限（次）：0=当天不做采样评估（不影响睡眠时段的关系日结）",
        json_schema_extra={
            "label": "会话裁判每日上限",
            "hint": "控制 LLM 成本；日结每天最多 1 次，与采样评估分开计",
            **_ui_i18n("Review daily cap", "M2: max sample reviews per day. The nightly review is separate."),
        },
    )
    review_task_name: str = Field(
        default="utils",
        description="好感裁判使用的 LLM 任务名（宿主 model_config 的任务键，默认 utils）",
        json_schema_extra={
            "label": "裁判任务名",
            "hint": "建议指到便宜、非思考型的任务（会话采样与关系日结共用）",
            **_ui_i18n("Review task name", "M2: model task used by the affection referees."),
        },
    )
    emotion_modulation: bool = Field(
        default=True,
        description="情绪调制好感增量开关（B2，M3 已接线）：delta × clamp(1 + 0.25 × V × sign(delta), 0.6, 1.4)；"
                    "情绪模块停用或读不到当前 V 时乘数恒 1",
        json_schema_extra={
            "label": "情绪调制好感",
            "hint": "开心时好事加分更多、低落时坏事扣分更狠；关闭则所有增量按原值入账",
            **_ui_i18n("Emotion modulation",
                       "Modulate affection deltas by the current mood (multiplier 0.6-1.4)."),
        },
    )


# ---------------------------------------------------------------------------
# [judge] 判定类 LLM 调用（mai-love v2.6.3：想念把关 miss_confirm 等专用参数）
# ---------------------------------------------------------------------------


class JudgeSectionConfig(PluginConfigBase):
    """判定类 LLM 调用（想念把关 miss_confirm 等）的专用模型与参数。

    为什么单独一个板块：判定调用只要求"给出明确结论"，与生成日程/回复的需求
    完全不同——此前把关与日程共用同一个 LLM 任务且 max_tokens 写死 16，宿主把
    任务切到思考型模型后 reasoning 吃满预算、正文恒为空，想念把关整批失败。
    现在模型、最大 token 数、温度都可按判定场景单独调（热更新即时生效）。
    """

    __ui_label__ = "LLM 判断"
    __ui_icon__ = "gavel"
    __ui_order__ = 10
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "LLM Judgment", "description": "Dedicated model and parameters for verdict-style LLM calls (e.g. the miss gate)."}
    }

    model: str = Field(
        default="",
        description="判断用模型：填宿主模型任务名（utils/replyer/planner）或 WebUI 模型列表里的具体模型名；"
                    "留空 = 跟随「LLM 模型任务名」（[schedule] llm_task_name）",
        json_schema_extra={
            "label": "判断用模型",
            "hint": "判断只需要明确的 Y/N 结论，建议用便宜、非思考型的任务/模型；"
                    "坚持用思考型模型的话，请把下面的「判断最大 token 数」保持 512 以上",
            **_ui_i18n("Judge model",
                       "Model task or model name for judgment calls; empty = follow the main LLM task. "
                       "A cheap non-thinking model is recommended; keep max tokens >= 512 for thinking models."),
        },
    )
    max_tokens: int = Field(
        default=512,
        ge=16,
        le=8192,
        description="判断调用的最大生成 token 数。思考型模型会先消耗 token 输出推理内容，"
                    "预算太小会导致正文为空、判定被当作失败（fail-closed：按驳回处理）",
        json_schema_extra={
            "label": "判断最大 token 数",
            "hint": "默认 512 对 Y/N 判定远超所需；用思考型模型时不要低于 512",
            **_ui_i18n("Judge max tokens",
                       "Max tokens for judgment calls. Thinking models spend tokens on reasoning first; keep >= 512."),
        },
    )
    temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="判断调用的采样温度。判定要求结论稳定，默认 0.2 低温；"
                    "调高会更容易出现模糊表述，导致结论解析失败（同样按驳回处理）",
        json_schema_extra={
            "label": "判断温度",
            "hint": "默认 0.2。判定要的是稳定结论，不是创意，一般不用动",
            **_ui_i18n("Judge temperature",
                       "Sampling temperature for judgment calls. Default 0.2 keeps verdicts stable."),
        },
    )


# ---------------------------------------------------------------------------
# [cateye] 恋人电脑屏幕感知（源插件 [cateye] 段）
# ---------------------------------------------------------------------------


class CateyeScreenSectionConfig(PluginConfigBase):
    """恋人电脑屏幕感知（联动「cateye 统一连接插件」cateye.connect-hub）。

    主动触发前看一眼恋人电脑：已连接则截图 + 视觉模型转述，作为「旁白」以
    尾随锚点方式写进 planner/replyer 上下文；未连接/看不成有各自的占位文案。
    """

    __ui_label__ = "恋人电脑感知"
    __ui_icon__ = "monitor"
    __ui_order__ = 7
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Lover PC Awareness",
               "description": "Peek at the lover's PC via the cateye connect-hub before proactive triggers."}
    }

    enabled: bool = Field(
        default=False,
        description="启用屏幕感知：每次主动触发前经 cateye.connect-hub 截图并转述；需要 cateye 插件与客户端在线",
        json_schema_extra={
            "label": "启用屏幕感知",
            "hint": "默认关闭；需同时安装「cateye 统一连接插件」并在用户电脑跑起客户端",
            **_ui_i18n("Enable screen awareness",
                       "Requires the cateye connect-hub plugin and an online client on the lover's PC."),
        },
    )
    screenshot_blur: int = Field(
        default=0,
        ge=0,
        le=64,
        description="截图模糊半径（0-64）：0=清晰；介意隐私可设 30 左右（视觉模型仍能看出大概在干什么）",
        json_schema_extra={"label": "截图模糊半径", **_ui_i18n("Screenshot blur")},
    )
    vlm_task: str = Field(
        default="vlm",
        description="截图理解使用的视觉模型任务名（宿主 model_config 的任务键，默认 vlm）",
        json_schema_extra={"label": "视觉任务名", **_ui_i18n("Vision task name")},
    )
    timeout_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=120.0,
        description="查看电脑（截图 + 视觉理解）整体超时（秒）；超时不阻塞巡检，本轮按不可用处理",
        json_schema_extra={"label": "查看超时(秒)", **_ui_i18n("Peek timeout (s)")},
    )
    describe_prompt: str = Field(
        default=SCREEN_DESCRIBE_PROMPT_DEFAULT,
        description="屏幕截图的视觉理解提示词（随截图发给视觉模型，要求一句话描述）",
        json_schema_extra={
            "label": "屏幕理解提示词",
            "rows": 4,
            **_ui_i18n("Screen describe prompt",
                       "Prompt sent to the vision model along with the screenshot."),
        },
    )
    narration_template: str = Field(
        default=SCREEN_NARRATION_TEMPLATE_DEFAULT,
        description="看清楚时注入的旁白模板。占位符：{date} 年月日、{time} 时分、{user_name}、{description}",
        json_schema_extra={
            "label": "屏幕旁白模板",
            "rows": 3,
            **_ui_i18n("Narration template",
                       "Template used when the screen was read. Placeholders: {date}, {time}, {user_name}, {description}."),
        },
    )
    context_ttl_minutes: int = Field(
        default=90,
        ge=0,
        le=1440,
        description="屏幕旁白存活上限（分钟）：超过即丢弃不再注入；0=不限时（只随锚点滑出上下文失效）",
        json_schema_extra={"label": "旁白存活上限(分钟)", **_ui_i18n("Narration TTL (min)")},
    )
    offline_narration_template: str = Field(
        default=SCREEN_OFFLINE_NARRATION_TEMPLATE_DEFAULT,
        description="电脑没开（客户端未连接 / hub 不可用）时注入的旁白。占位符：{date} {time} {user_name}。留空=该情况不注入",
        json_schema_extra={
            "label": "电脑没开旁白",
            "rows": 2,
            **_ui_i18n("Offline narration",
                       "Narration when the PC is offline. Placeholders: {date}, {time}, {user_name}. Blank = skip."),
        },
    )
    failed_narration_template: str = Field(
        default=SCREEN_FAILED_NARRATION_TEMPLATE_DEFAULT,
        description="截图/视觉理解失败时注入的旁白。占位符：{date} {time} {user_name}。留空=该情况不注入；整体超时按不注入处理",
        json_schema_extra={
            "label": "没看清旁白",
            "rows": 2,
            **_ui_i18n("Failed narration",
                       "Narration when screenshot/vision fails. Blank = skip; timeout skips entirely."),
        },
    )
