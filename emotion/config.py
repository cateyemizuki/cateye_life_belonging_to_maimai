"""情绪系统 — 配置段（每个业务模块一个独立总开关的样板）。"""

from __future__ import annotations

from typing import ClassVar, Dict

from maibot_sdk import Field, PluginConfigBase


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class EmotionSectionConfig(PluginConfigBase):
    """情绪维护系统（VAD 三维模型：日常增量微调 + 睡眠期大修）。"""

    __ui_label__ = "情绪系统"
    __ui_icon__ = "smile"
    __ui_order__ = 7
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
        description="日常指数衰减率（每小时）：情绪向基线回落的速度；对话频繁可调高防无限累积",
        json_schema_extra={"label": "衰减率", "step": 0.01, **_ui_i18n("Decay rate")},
    )
    evaluate_on_reply: bool = Field(
        default=True,
        description="每次 bot 回复后用小模型评估该轮对话的情绪增量 ΔVAD（事件→增量映射）",
        json_schema_extra={"label": "回复后评估增量", **_ui_i18n("Evaluate on reply")},
    )
    evaluate_min_interval_seconds: int = Field(
        default=60,
        ge=0,
        le=3600,
        description="两次增量评估之间的最小间隔（秒），控制评估成本",
        json_schema_extra={"label": "评估最小间隔(秒)", **_ui_i18n("Min evaluate interval (s)")},
    )
    evaluator_task_name: str = Field(
        default="utils",
        description="增量评估/睡眠裁判使用的模型任务名（裁判默认见 referee_task_name）",
        json_schema_extra={"label": "评估任务名", **_ui_i18n("Evaluator LLM task")},
    )
    referee_task_name: str = Field(
        default="replyer",
        description="睡眠裁判 LLM 的模型任务名（独立请求，不携带对话历史）",
        json_schema_extra={"label": "睡眠裁判任务名", **_ui_i18n("Sleep referee LLM task")},
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
    target_bonus_step: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="target 每档情绪加成步长（加算）：加成 = step × (档位+1)，档 0~4 对应 +0.1~+0.5",
        json_schema_extra={"label": "目标加成步长", "step": 0.01,
                           **_ui_i18n("Target bonus step")},
    )
    injection_enabled: bool = Field(
        default=True,
        description="把当前情绪底色（模糊化描述）注入 replyer 提示词，引导语气与情绪一致",
        json_schema_extra={"label": "情绪底色注入", **_ui_i18n("Mood injection")},
    )
    # ── A3 跨来源去重（§2.4.6：自动评估 ⇄ 工具上报，同一流+窗口只计一次）──────
    dedup_enabled: bool = Field(
        default=True,
        description="跨来源去重：自动评估（replyer）与 emotion_report 工具对同一事件的重复入账只计一次（§2.4.6 A3）",
        json_schema_extra={"label": "跨来源去重", **_ui_i18n("Cross-source dedup")},
    )
    dedup_window_seconds: int = Field(
        default=180,
        ge=0,
        le=3600,
        description="跨来源去重时间窗（秒）：同一聊天流内两条来源先后入账超过该间隔则各自计入；0=关闭窗口判定",
        json_schema_extra={"label": "去重时间窗(秒)", **_ui_i18n("Dedup window (s)")},
    )
    check_interval_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="睡眠检测循环间隔（分钟）：进入日程睡眠时段后触发睡眠裁判大修",
        json_schema_extra={"label": "睡眠检测间隔(分钟)", **_ui_i18n("Sleep check interval (min)")},
    )
