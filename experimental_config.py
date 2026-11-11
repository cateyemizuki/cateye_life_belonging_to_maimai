# -*- coding: utf-8 -*-
"""``[experimental]`` 实验性功能配置段（2026-11-11 用户裁定，schema 顶部）。

用户裁定（权威）：所有实验性特性（F1/F3/F4/联动批）的开关与参数，实验期全部集中
在本段，**不散落**到 ``[patrol]`` / ``[life_state]`` 等原板块；每个键的「转正后目标
板块与键名」见 ``union/20261111_实验配置迁移映射.md``。实验期默认全部关闭。

本模块只定义 section 类，聚合由顶层 ``suite_config.CateyeSuiteConfig`` 负责：

- ``[experimental].group_attention_*``：F1 群注意力衰减（B1 批）；
- ``[experimental].transition_*``：F3 转场拖延（B2 批）；
- 后续 F4/联动批的新键继续追加在本段，按批在迁移映射文档登记目标板块。

参数默认值与 ``core.internal_tuning.INTERNAL_TUNING_DEFAULTS`` 对应 J 段同源
（实验期用户可见；转正后参数迁回 J 类 ``[group_attention]`` / ``[transition]`` 段）。
运行期只读本 section 的 pydantic 字段，不读 ``mappings/internal_tuning.json``——
实验期配置只有一个用户可见入口。
"""

from __future__ import annotations

from typing import ClassVar, Dict, List

from maibot_sdk import Field, PluginConfigBase

from core.i18n import ui_i18n as _ui_i18n
from core.internal_tuning import INTERNAL_TUNING_DEFAULTS

#: F1 参数默认值单一真源（与 internal_tuning 种子/迁移目标同名）
_GA: Dict[str, object] = dict(INTERNAL_TUNING_DEFAULTS["group_attention"])

#: F3 参数默认值单一真源（转正后目标 = internal_tuning.json [transition]）
_TR: Dict[str, object] = dict(INTERNAL_TUNING_DEFAULTS["transition"])


class ExperimentalSectionConfig(PluginConfigBase):
    """实验性功能（默认全关；观察 1–2 周后逐特性裁决转正/调参/回退）。

    迁移提示：转正后按 ``union/20261111_实验配置迁移映射.md`` 迁回目标板块；
    关闭本段任一开关即回到 3.17.33 对应行为（专项含默认关逐字不变断言）。
    """

    __ui_label__ = "实验性功能"
    __ui_icon__ = "flask"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Experimental",
               "description": "Experimental features. All off by default; promotion plan is "
                              "documented per key in the migration map."}
    }

    # ── F1 群注意力衰减（B1；v2 §3）────────────────────────────────
    group_attention_decay_enabled: bool = Field(
        default=False,
        description="群注意力衰减：群话题与当前活动不搭、且 bot 很久没被点名/接话时，"
                    "逐步降低该群的下发频率；被点名、目标用户发言或话题回到活动时逐步恢复。"
                    "只调频率、不拦消息，回不回由 bot/planner 决定。",
        json_schema_extra={
            "label": "群注意力衰减",
            "hint": "默认关。开 = 只对 [planning_proactive] 群生效集生效；下限=宿主因子×0.5，"
                    "睡眠完全豁免，目标私聊恒 1.0；安静群不衰减",
            **_ui_i18n("Group attention decay",
                       "Experimental: gradually lower reply frequency in groups whose topic "
                       "doesn't match the current activity and where the bot hasn't been "
                       "addressed for a while. Frequency only; never blocks messages."),
        },
    )
    group_attention_sample_size: int = Field(
        default=_GA["sample_size"], ge=3, le=200,
        description="每群环形缓冲的消息条数（用于话题失配比例；少于 3 条不判定失配）",
        json_schema_extra={
            "label": "采样条数",
            "hint": "默认 20；越大越稳、越迟钝",
            **_ui_i18n("Sample size", "Messages kept per group for topic mismatch ratio."),
        },
    )
    group_attention_decay_window_minutes: int = Field(
        default=_GA["decay_window_minutes"], ge=1, le=1440,
        description="「多久没被点名/没参与」的饱和窗（分钟）：达到该时长沉默分记为 1",
        json_schema_extra={
            "label": "衰减饱和窗(分钟)",
            "hint": "默认 45；越短越容易衰减",
            **_ui_i18n("Decay window (min)", "Silence window that saturates the attention score."),
        },
    )
    group_attention_silence_weight: float = Field(
        default=_GA["silence_weight"], ge=0.0, le=1.0,
        description="失配分数中「沉默」占比（与话题权重共同决定目标乘数）",
        json_schema_extra={
            "label": "沉默权重",
            "hint": "默认 0.45；与话题权重相加为 1 时目标最低到 0.5",
            **_ui_i18n("Silence weight", "Weight of silence in the decay score."),
        },
    )
    group_attention_mismatch_weight: float = Field(
        default=_GA["mismatch_weight"], ge=0.0, le=1.0,
        description="失配分数中「话题不搭」占比（与沉默权重共同决定目标乘数）",
        json_schema_extra={
            "label": "话题失配权重",
            "hint": "默认 0.55；首期纯关键词判定、零 LLM",
            **_ui_i18n("Mismatch weight", "Weight of topic mismatch in the decay score."),
        },
    )
    group_attention_step_per_tick: float = Field(
        default=_GA["step_per_tick"], ge=0.0, le=0.5,
        description="每 60s 向下的最大步进（乘数从 1.0 到 0.5 约需 25 分钟，避免一次撞下限）",
        json_schema_extra={
            "label": "向下步进/拍",
            "hint": "默认 0.02；调大衰减更快、更容易显得冷淡",
            **_ui_i18n("Decay step per tick", "Maximum downward multiplier step every 60s."),
        },
    )
    group_attention_recover_step_per_tick: float = Field(
        default=_GA["recover_step_per_tick"], ge=0.0, le=0.5,
        description="每 60s 向上的最大恢复步进（被点名/目标发言后不会瞬间满频）",
        json_schema_extra={
            "label": "恢复步进/拍",
            "hint": "默认 0.05；调小恢复更慢",
            **_ui_i18n("Recovery step per tick", "Maximum upward multiplier step every 60s."),
        },
    )
    group_attention_planner_hint_min_multiplier: float = Field(
        default=_GA["planner_hint_min_multiplier"], ge=0.0, le=1.0,
        description="乘数低于该值才可能向 planner 注入一行【群聊注意力】状态",
        json_schema_extra={
            "label": "状态行阈值",
            "hint": "默认 0.85；调到 1.0 = 只要衰减就提示",
            **_ui_i18n("Planner hint threshold",
                       "Only emit the planner attention line below this multiplier."),
        },
    )
    group_attention_planner_hint_interval_minutes: int = Field(
        default=_GA["planner_hint_interval_minutes"], ge=1, le=1440,
        description="每个群两次【群聊注意力】状态行之间的最小间隔（分钟）",
        json_schema_extra={
            "label": "状态行间隔(分钟)",
            "hint": "默认 30；只限流、不抢日程块、不受日程注入冷却吞并",
            **_ui_i18n("Planner hint interval (min)",
                       "Minimum interval between two attention lines in the same group."),
        },
    )
    group_attention_quiet_group_grace_minutes: int = Field(
        default=_GA["quiet_group_grace_minutes"], ge=1, le=1440,
        description="该窗内无任何群消息/回复 → 视为安静群：目标乘数回 1.0、不继续衰减",
        json_schema_extra={
            "label": "安静群宽限(分钟)",
            "hint": "默认 30；防止冷清群被越衰减越不敢说话",
            **_ui_i18n("Quiet group grace (min)",
                       "No messages within this window => quiet group, recover to 1.0."),
        },
    )

    # ── F3 转场拖延（B2；v2 §4）────────────────────────────────────
    transition_enabled: bool = Field(
        default=_TR["enabled"],
        description="转场拖延：从吃饭/娱乐/休息/待机切到工作/学习这类高反差切换时，"
                    "先插 3–8 分钟惯性过渡窗（专注缓慢爬坡），并以 20% 概率把启动推迟"
                    " 15 分钟；拖延后可低概率在聊天里随口提一句。只作用于软意图，"
                    "硬约定/睡眠/吃饭锚点完全不碰。",
        json_schema_extra={
            "label": "转场拖延",
            "hint": "默认关。开 = 每次高反差软切换最多一次缓冲（日限 4）+ 一次拖延（日限 2）；"
                    "idle 调度与对外口径不变",
            **_ui_i18n("Transition drag",
                       "Experimental: after a low-focus activity, insert a 3-8 minute "
                       "transition window before work/study and occasionally delay the "
                       "start. Soft intents only; commitments and sleep are untouched."),
        },
    )
    transition_window_min: int = Field(
        default=_TR["window_min"], ge=1, le=30,
        description="惯性过渡窗最短分钟数（显式 duration，不受 transition 画像 min_block=5 限制）",
        json_schema_extra={
            "label": "过渡窗最短(分钟)",
            "hint": "默认 3；剩余窗口不足该值时不插缓冲",
            **_ui_i18n("Transition window min", "Minimum transition window length."),
        },
    )
    transition_window_max: int = Field(
        default=_TR["window_max"], ge=1, le=60,
        description="惯性过渡窗最长分钟数",
        json_schema_extra={
            "label": "过渡窗最长(分钟)",
            "hint": "默认 8；窗末 focus 约到 0.65，工作可用即可、不追求满值",
            **_ui_i18n("Transition window max", "Maximum transition window length."),
        },
    )
    transition_procrastination_prob: float = Field(
        default=_TR["procrastination_prob"], ge=0.0, le=1.0,
        description="每次高反差软意图启动前的拖延掷骰概率（独立种子，不耗 planner 主 RNG）",
        json_schema_extra={
            "label": "拖延概率",
            "hint": "默认 0.20；缓冲分支优先，缓冲没插时才掷拖延",
            **_ui_i18n("Procrastination probability",
                       "Chance to delay a high-contrast soft intent start."),
        },
    )
    transition_delay_minutes: int = Field(
        default=_TR["delay_minutes"], ge=1, le=180,
        description="拖延基准分钟数（实际值再叠加 ± 抖动，并按硬边界/窗口右界夹取）",
        json_schema_extra={
            "label": "推迟时长(分钟)",
            "hint": "默认 15；放不进窗口就放弃拖延、按原路径启动",
            **_ui_i18n("Delay minutes", "Base delay before starting the intent."),
        },
    )
    transition_delay_jitter_minutes: int = Field(
        default=_TR["delay_jitter_minutes"], ge=0, le=60,
        description="推迟时长的随机抖动幅度（± 分钟）",
        json_schema_extra={
            "label": "推迟抖动(分钟)",
            "hint": "默认 5；实际推迟 = 基准 ± 抖动，夹取后仍放不下则放弃",
            **_ui_i18n("Delay jitter (min)", "Random +/- jitter on the delay."),
        },
    )
    transition_daily_buffer_limit: int = Field(
        default=_TR["daily_buffer_limit"], ge=0, le=20,
        description="每日惯性过渡窗上限（跨 reload 由 transition_usage.json 保持，不刷额度）",
        json_schema_extra={
            "label": "过渡窗日限",
            "hint": "默认 4；0 = 不插过渡窗（但拖延仍可单独生效）",
            **_ui_i18n("Daily transition limit", "Maximum transition windows per day."),
        },
    )
    transition_daily_procrastination_limit: int = Field(
        default=_TR["daily_procrastination_limit"], ge=0, le=20,
        description="每日拖延事件上限（跨 reload 持久）",
        json_schema_extra={
            "label": "拖延日限",
            "hint": "默认 2；0 = 不拖延",
            **_ui_i18n("Daily procrastination limit",
                       "Maximum procrastination events per day."),
        },
    )
    transition_low_focus_types: List[str] = Field(
        default=_TR["low_focus_types"],
        description="反差源活动类型（上一活动属于这些类型 → 视为低专注状态）",
        json_schema_extra={
            "label": "低专注类型",
            "hint": "默认 meal/entertainment/rest/idle；只影响反差判定，不改任何画像",
            **_ui_i18n("Low focus types",
                       "Activity types considered low-focus for contrast detection."),
        },
    )
    transition_high_focus_types: List[str] = Field(
        default=_TR["high_focus_types"],
        description="反差目标活动类型（软意图目标属于这些类型 → 可能插过渡窗/拖延）",
        json_schema_extra={
            "label": "高专注类型",
            "hint": "默认 work/study；硬约定永不延迟、永不替换",
            **_ui_i18n("High focus types",
                       "Activity types considered high-focus targets."),
        },
    )
    transition_boundary_margin_minutes: int = Field(
        default=_TR["boundary_margin_minutes"], ge=0, le=60,
        description="插过渡窗/拖延时距下一硬边界（或意图窗口右界）保留的余量分钟",
        json_schema_extra={
            "label": "边界余量(分钟)",
            "hint": "默认 2；余量不足 window_min 时不插缓冲，放不下推迟时长时放弃拖延",
            **_ui_i18n("Boundary margin (min)",
                       "Safety margin kept before hard boundaries / intent window end."),
        },
    )
    transition_focus_target: float = Field(
        default=_TR["focus_target"], ge=0.0, le=1.0,
        description="过渡窗内 focus 趋近目标（注入结算 v2 activity.transition.focus[0]）",
        json_schema_extra={
            "label": "过渡专注靶",
            "hint": "默认 0.68；8 分钟窗末约到 0.65（起点 0.45~0.55 时），工作可用即可",
            **_ui_i18n("Transition focus target",
                       "Focus target approached during the transition window."),
        },
    )
    transition_focus_tau_min: float = Field(
        default=_TR["focus_tau_min"], ge=0.5, le=120.0,
        description="过渡窗 focus 趋近时间常数（分钟；越小爬坡越快）",
        json_schema_extra={
            "label": "过渡专注时间常数(分钟)",
            "hint": "默认 3.0；配合靶 0.68，窗末 focus 不低于 0.60",
            **_ui_i18n("Transition focus tau (min)",
                       "Time constant of the focus approach during the window."),
        },
    )
    transition_confess_probability: float = Field(
        default=_TR["confess_probability"], ge=0.0, le=1.0,
        description="聊天时主动自我揭露「刚才拖延/发呆」的概率（按聊天流×日程段去重）",
        json_schema_extra={
            "label": "自我揭露概率",
            "hint": "默认 0.10；同一流同一次过渡/拖延最多一次，同流两次至少隔 20 分钟",
            **_ui_i18n("Self-disclosure probability",
                       "Chance to mention the drag/loafing in chat, deduped per stream "
                       "and episode."),
        },
    )
    transition_confess_min_interval_minutes: int = Field(
        default=_TR["confess_min_interval_minutes"], ge=0, le=1440,
        description="同一聊天流两次自我揭露的最小间隔（分钟；在 episode 去重之上）",
        json_schema_extra={
            "label": "揭露最小间隔(分钟)",
            "hint": "默认 20；只限流，不阻塞正常回复",
            **_ui_i18n("Self-disclosure interval (min)",
                       "Minimum interval between two disclosures in the same stream."),
        },
    )
    transition_procrastination_event_branch: bool = Field(
        default=_TR["procrastination_event_branch"],
        description="预留：拖延时改插摸鱼 episode（本批不实现，仅保留开关；开与不开行为一致）",
        json_schema_extra={
            "label": "拖延摸鱼分支(未实现)",
            "hint": "默认关；首期只做延迟分支，开本键不产生任何新行为",
            **_ui_i18n("Procrastination loafing branch (unused)",
                       "Reserved switch; not implemented in this batch."),
        },
    )
