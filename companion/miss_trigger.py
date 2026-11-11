"""Optional missing-turn prose; actual policy lives in initiative_profile."""
from __future__ import annotations
import unicodedata
from typing import Any, Optional

# 阶段 4 文案/清洗单一 owner = core.stage4_prompt（纯文本模块，无业务依赖）。
# companion 与 tools.proactive_turn 都从这里转发，避免两份表漂移（复核 finding）。
# 这些名字保持在本模块命名空间内（plugin/tests 既有 `miss_trigger.xxx` 引用不变）。
from core.stage4_prompt import (  # noqa: E402
    MISS_REASON_STAGE4_SUFFIX,
    MISS_REASON_STAGE4_VIDEO_SUFFIX,
    STAGE4_SUPPRESSED_PHRASES,
    append_stage4_miss_prompt,
    clean_stage4_prompt,
    _clean_stage4_base,
    _format_hours,
    _SUPPRESSED_PHRASES,
)
MEMORY_LINE_MAX_CHARS = 120
EMOTION_VALENCE_THRESHOLD = .3

#: 短回复判定：去掉空白/标点/表情后**有意义字符**少于 3 个即算短回复。
SHORT_REPLY_MAX_CHARS = 3


def _to_float(value: Any) -> Optional[float]:
    """防御性 float（bool/非法 → None）；None 语义 = 该轴不可得。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result:  # NaN
        return None
    return result

def memory_line(habits: Any) -> str:
    """关系记忆语境行（可选）：用户习惯 → 一句「你记得TA的习惯：…」。

    无习惯/非法入参 → 空串（调用方不追加）。截断 :data:`MEMORY_LINE_MAX_CHARS`。
    """
    if not isinstance(habits, dict) or not habits:
        return ""
    parts = [f"{str(k).replace('_', ' ')} → {v}" for k, v in habits.items()]
    text = "；".join(parts)
    if len(text) > MEMORY_LINE_MAX_CHARS:
        text = text[:MEMORY_LINE_MAX_CHARS] + "…"
    return f"你记得TA的一些习惯：{text}。可以自然地带一句，不要逐条汇报。"

MISS_REASON_DEFAULT = (
    "【可选动机·想念】距离陪伴目标上次来信约{hours}小时，你有一点想联系TA。"
    "这只是插件给出的一个念头，不代表你必须立刻开口；是否说话、说什么，"
    "由你按当前状态和上下文决定。不合适就不输出任何工具调用，"
    "不必解释，也不要重复刚刚的问候。{memory_line}"
)

# P0-ACT-1 阶梯想念（2026-11-11 gemini3.8flash 四阶段定稿）：语气随沉默递增，
# 由「轻度试探」→「在意升温」→「直接追问去向」。占位符只允许
# {relation} / {hours} / {memory_line}（{lover_name} 兼容但不再使用）——
# suite_prompts.render 遇到未知占位符会静默回退 MISS_REASON_DEFAULT。
MISS_REASON_STAGE1 = (
    "【触发动机·试探联系·阶段1】距离陪伴目标（与你的关系：{relation}）上次回复已经过去约{hours}小时。"
    "不要直白说出“有点想你了”或肉麻的表白词；"
    "请完全结合你和TA的{relation}关系及你的人设性格，自然地问一嘴对方现在在干嘛，"
    "或者带点随性的小好奇、轻轻问一句怎么半天没理你。"
    "保持简短口语化，像平时随手敲过来的一句话，不要长篇大论。{memory_line}"
)

MISS_REASON_STAGE2 = (
    "【触发动机·挂念追问·阶段2】距离陪伴目标（关系：{relation}）上次回复已经过去约{hours}小时（超过一天）。"
    "你心里已经明显在意对方为什么一直没有回音，语气中可以带出一点等待感和轻微的失落或小抱怨。"
    "可以顺手提一句你刚做完的小事或眼前看到的日常作为由头，重点询问TA是不是最近太忙、"
    "忘记看手机了，还是发生了什么事。不要客套寒暄，像熟人之间真实的追问那样自然开口。{memory_line}"
)

MISS_REASON_STAGE3 = (
    "【触发动机·追问去向·阶段3】距离陪伴目标（关系：{relation}）上次来信已经过去约{hours}小时（两三天以上未回）。"
    "你积累了很强的失落、委屈或焦急感，不要再掩饰，也不要用客套话打圆场。"
    "本次开口的核心是直接问对方“人到底去哪了”、怎么会失联这么久，或者是不是在故意躲着你。"
    "语气完全贴合你的性格（可以是闷闷的追问、带点小脾气的质问，或压着担心的认真确认），"
    "务必让对方感受到你确实在等一个回音。{memory_line}"
)

#: 阶段号 → 文案（未知/0/4 回退默认模板；阶段 4 走每日任务尾缀，不占独立想念）。
STAGE_REASONS = {1: MISS_REASON_STAGE1, 2: MISS_REASON_STAGE2, 3: MISS_REASON_STAGE3}


def miss_reason_for_stage(stage: Any) -> str:
    """阶段号 → 该阶段的想念 reason 模板（非法 → 默认模板，旧行为逐字不变）。

    阶段 4 不产生独立想念（由每日主动任务承载），因此同样回退默认模板；调用方在
    阶梯开启时不会走到这里（``evaluate_miss`` 已返回不可受理）。
    """
    try:
        index = int(stage)
    except (TypeError, ValueError):
        return MISS_REASON_DEFAULT
    return STAGE_REASONS.get(index, MISS_REASON_DEFAULT)


def _meaningful_chars(text: str) -> str:
    """保留字母/数字（含中文），丢弃空白、标点、符号（表情）与组合符。"""
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def is_short_reply(text: Any) -> bool:
    """短回复判定（P0-ACT-1 第 3 条）：``<3 字`` 或纯表情/纯标点 → True。

    只做字符类别判断，不做语义分析：去掉空白/标点/表情后有意义字符少于
    :data:`SHORT_REPLY_MAX_CHARS` 即短。``None``/空串 = 文本不可得 → False
    （调用方按「未知 = 真实回复」处理，绝不因读不到文本而误顺延周期）。
    """
    if text is None:
        return False
    raw = str(text).strip()
    if not raw:
        return False
    return len(_meaningful_chars(raw)) < SHORT_REPLY_MAX_CHARS


def state_words(energy: Any, social_battery: Any, valence: Any = None) -> str:
    """把关 prompt 的状态语境行（低分辨率；各轴不可得则跳过该轴）。"""
    e = _to_float(energy)
    s = _to_float(social_battery)
    v = _to_float(valence)
    parts = []
    if s is not None:
        if s < 0.25:
            parts.append("暂时不太有精力聊天")
        elif s > 0.85:
            parts.append("社交电量挺满")
    if e is not None:
        if e < 0.3:
            parts.append("有些疲惫")
        elif e > 0.8:
            parts.append("精神不错")
    if v is not None:
        if v < -EMOTION_VALENCE_THRESHOLD:
            parts.append("心情有点低落")
        elif v > EMOTION_VALENCE_THRESHOLD:
            parts.append("心情不错")
    if not parts:
        return ""
    return "你现在的状态：" + "、".join(parts) + "。"
