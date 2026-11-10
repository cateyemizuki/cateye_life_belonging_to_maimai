"""情绪系统 — 配置段（每个业务模块一个独立总开关的样板）。"""

from __future__ import annotations

from typing import ClassVar, Dict, Literal

from maibot_sdk import Field, PluginConfigBase

from core.i18n import ui_i18n as _ui_i18n
from core.internal_tuning import tuning_value as _tuning_value


class EmotionSectionConfig(PluginConfigBase):
    """设置情绪评估、自然变化、对话表达和睡眠恢复。"""

    __ui_label__ = "情绪系统"
    __ui_icon__ = "smile"
    __ui_order__ = 7
    # 2026-10-10：模型任务名（evaluator/referee）已迁入 [llm] 段统一管理。
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Emotion", "description": "VAD emotion state machine with sleep overhaul."}
    }

    # ── 模块独立总开关（新模块样板：每个业务模块的第一个字段）──────
    enabled: bool = Field(
        default=True,
        description="情绪模块总开关：关闭后停止情绪评估/注入/睡眠大修（命令与工具返回未启用提示）",
        json_schema_extra={
            "label": "启用情绪模块",
            "hint": "每个业务模块都有独立总开关；关闭不影响其他模块",
            **_ui_i18n("Enable emotion module", "Master switch of the emotion module."),
        },
    )
    decay_rate: float = Field(
        default=0.08,
        ge=0.01,
        le=0.3,
        description="日常指数衰减率（每小时）：情绪向 0（中性）回落的速度；向人设锚点收敛只在睡眠大修发生；"
                    "对话频繁可调高防无限累积",
        json_schema_extra={"label": "衰减率", "step": 0.01, **_ui_i18n("Decay rate")},
    )
    evaluate_on_reply: bool = Field(
        default=True,
        description="回复后情绪评估的兼容开关；已设置评估模式时使用评估模式。",
        json_schema_extra={"label": "回复后评估增量", **_ui_i18n("Evaluate on reply")},
    )
    evaluate_mode: Literal["episode", "per_reply", "off"] = Field(
        default="episode",
        description="情绪评估方式：episode在会话闲置后整段评估，per_reply逐回复评估（至少间隔60秒），off关闭。",
        json_schema_extra={
            "label": "回复后评估模式",
            "hint": "episode 按会话片段整段评估（成本封顶）；per_reply 为旧每回复模式",
            **_ui_i18n("Evaluate mode", "episode: evaluate one idle session segment at a time."),
        },
    )
    # 2026-11-04 配置精简轮（J 类）：episode_idle_minutes 移入
    # mappings/internal_tuning.json[emotion]（内部批处理阈值）；同名只读 property 见类尾。
    evaluate_max_per_day: int = Field(
        default=10,
        ge=0,
        le=200,
        description="情绪评估每日上限（次，两种模式共用，硬封顶；0=当日不评估）",
        json_schema_extra={"label": "评估每日上限(次)", **_ui_i18n("Max evaluations per day")},
    )
    evaluate_min_interval_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="两次增量评估之间的最小间隔（秒），控制评估成本",
        json_schema_extra={"label": "评估最小间隔(秒)", **_ui_i18n("Min evaluate interval (s)")},
    )
    # ── M3 事件倍率（§6.2 A，附录 C；一律标量，不用 dict/list——陷阱 #4）──────
    global_event_multiplier: float = Field(
        default=1.0,
        ge=0.1,
        le=3.0,
        description="全局情绪事件幅度旋钮：作用于一切外部事件增量（不含日常衰减），运行期调控情绪整体波动大小",
        json_schema_extra={"label": "全局事件倍率", "step": 0.1,
                           **_ui_i18n("Global event multiplier")},
    )
    non_target_multiplier: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="非 target 事件系数（群聊等非重要他人引发的事件）：默认 1.0 保持自然幅度，调低可淡化群聊影响",
        json_schema_extra={"label": "非目标事件倍率", "step": 0.05,
                           **_ui_i18n("Non-target event multiplier")},
    )
    # J 类：target_bonus_step → internal_tuning.json[emotion]（内部权重；
    # emotion_mapping.json 的 multipliers 覆盖优先级不变，见 _merged_multiplier）。
    injection_enabled: bool = Field(
        default=True,
        description="把当前情绪底色（模糊化描述）作为轻微心境参考注入提示词，不强制语气与情绪一致",
        json_schema_extra={"label": "情绪底色注入", **_ui_i18n("Mood injection")},
    )
    # ── PR-C1 H4（2026-11-07）：短期情绪原因时效（只作解释，不二次加减 VAD）──────
    emotion_reason_ttl_hours: int = Field(
        default=6,
        ge=1,
        le=48,
        description="短期情绪原因的有效期（小时）：事件登记的原因摘要到期后不再注入；"
                    "原因只解释心情来源，情绪数值仍由既有衰减逐渐平复",
        json_schema_extra={"label": "情绪原因有效期(小时)",
                           **_ui_i18n("Emotion reason TTL (hours)",
                                      "Short-term reason entries expire and stop being injected.")},
    )
    emotion_reason_inject_enabled: bool = Field(
        default=False,
        description="在情绪底色注入后附最近一条未过期的高重要度原因摘要（因为…）；"
                    "3.17.29 起默认关闭（复核 VAD-4.6：普通闲聊不再每轮携带具体原因，"
                    "避免把背景心情重新变成话题；需要时可在面板开启）",
        json_schema_extra={"label": "注入情绪原因",
                           **_ui_i18n("Inject emotion reason",
                                      "Append at most one fresh reason summary to the mood injection.")},
    )
    # ── A3 跨来源去重（§2.4.6：自动评估 ⇄ 工具上报，同一流+窗口只计一次）──────
    dedup_enabled: bool = Field(
        default=True,
        description="跨来源去重：自动评估（replyer）与 emotion_report 工具对同一事件的重复入账只计一次（§2.4.6 A3）",
        json_schema_extra={"label": "跨来源去重", **_ui_i18n("Cross-source dedup")},
    )
    # J 类：dedup_window_seconds → internal_tuning.json[emotion]（内部阈值；类尾 property）。
    check_interval_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="睡眠检测循环间隔（分钟）：进入日程睡眠时段后触发睡眠裁判大修",
        json_schema_extra={"label": "睡眠检测间隔(分钟)", **_ui_i18n("Sleep check interval (min)")},
    )
    # ── M8 段 2：L1 夜间打扰判定（附录 §2.1；生效条件 = [schedule].no_sleep_mode 为 false）────
    interrupt_reply_threshold: int = Field(
        default=3,
        ge=1,
        le=100,
        description="夜间打扰判定：同一时间窗内累计 reply 达到该次数才算「被吵醒」"
                    "（只触发 planner、没有 reply 不算；生效还要求处于睡眠周期且 reply 来自 target 私聊）",
        json_schema_extra={
            "label": "打扰判定回复数(次)",
            "hint": "窗口内累计 target 私聊 reply ≥ 该值 → 判定被吵醒；只看计数，不逐次入账",
            **_ui_i18n("Interrupt reply threshold", "Replies within the window needed to count as woken up."),
        },
    )
    interrupt_window_minutes: int = Field(
        default=10,
        ge=1,
        le=1440,
        description="夜间打扰判定的计数窗口（分钟）：窗口 = [最后一次 reply − 本值, 最后一次 reply]，随 reply 滚动",
        json_schema_extra={"label": "打扰计数窗(分钟)", **_ui_i18n("Interrupt window (min)")},
    )
    resleep_minutes: int = Field(
        default=5,
        ge=1,
        le=1440,
        description="夜间打扰判定：自最后一次 reply 起该时长内无新 reply 即视为再次睡着（打扰段闭合，回到睡眠态）",
        json_schema_extra={"label": "重新入睡判定(分钟)", **_ui_i18n("Resleep minutes")},
    )

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[emotion]）───────
    @property
    def episode_idle_minutes(self) -> int:
        """episode 模式会话闲置冲刷阈值（分钟）。"""
        return int(_tuning_value("emotion", "episode_idle_minutes"))

    @property
    def target_bonus_step(self) -> float:
        """target 每档情绪加成步长（emotion_mapping.json 覆盖优先）。"""
        return float(_tuning_value("emotion", "target_bonus_step"))

    @property
    def dedup_window_seconds(self) -> int:
        """跨来源去重时间窗（秒）；0 = 关闭窗口判定。"""
        return int(_tuning_value("emotion", "dedup_window_seconds"))

    # 注：M6 的 emoji 情绪联动键已于 2026-10-11 按用户裁决随实现一并移除
    # （自 T6 起无消费组件；DEV_GUIDE 附录 A-7）。**维持移除不变**——2026-10-14 §18.2
    # 恢复的是 manifest 能力声明与空间侧表情包配图（消费点在 qzone），与本键无关。
    # 旧配置里的该键由宿主 extra=ignore 静默丢弃。


