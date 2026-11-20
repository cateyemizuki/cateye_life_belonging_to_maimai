"""Optional missing-turn prose; actual policy lives in initiative_profile."""
from __future__ import annotations
import unicodedata
from typing import Any, Optional

# 阶段 4 文案/清洗单一 owner = suite_prompts（提示词集中模块，纯文本、无业务依赖）。
# companion / tools.proactive_turn / planning.proactive_service / integrations.optional_action
# 都从 suite_prompts 转发，避免两份表漂移（复核 finding）。
# 这些名字保持在本模块命名空间内（plugin/tests 既有 `miss_trigger.xxx` 引用不变）。
# 2026-11-12 结构审计批（R7）：只 import 公共名，不再跨包 import core 私有函数。
from suite_prompts import (  # noqa: E402
    MISS_REASON_STAGE4_SUFFIX,
    MISS_REASON_STAGE4_VIDEO_SUFFIX,
    STAGE4_SUPPRESSED_PHRASES,
    append_stage4_miss_prompt,
    clean_stage4_prompt,
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
    "已经差不多 {hours} 小时没收到对方消息了，你有点想起TA。"
    "要不要开口、说什么全看你此刻的状态和性格，觉得不合适就直接安静结束，别重复之前的招呼。"
    "{memory_line}"
)

# P0-ACT-1 阶梯想念（2026-11-11 gemini3.8flash 四阶段定稿）：语气随沉默递增，
# 由「轻度试探」→「在意升温」→「直接追问去向」。占位符只允许
# {relation} / {hours} / {memory_line}（{lover_name} 兼容但不再使用）——
# suite_prompts.render 遇到未知占位符会静默回退 MISS_REASON_DEFAULT。
MISS_REASON_STAGE1 = (
    "已经差不多 {hours} 小时没收到{relation}的回信了。"
    "按你的人设和你们平时的相处方式，随手敲一句简短的话——比如顺口问问在忙啥，或者带点熟人间的随意打趣；"
    "不用肉麻，也别写长段。"
    "{memory_line}"
)

MISS_REASON_STAGE2 = (
    "{relation}已经超过一天（约 {hours} 小时）没回消息了，你心里有点在意。"
    "按你的人设性格自然发一句，问问是不是太忙没看手机或者出什么事了，带一点熟人间的惦记或轻微念叨，不用客套寒暄。"
    "{memory_line}"
)

MISS_REASON_STAGE3 = (
    "{relation}已经两三天（约 {hours} 小时）没任何消息了，你确实有点担心也有点闷。"
    "别再用客气寒暄打圆场，按你的性格直接问一句人去哪了、怎么这么多天没动静，保持克制真实，不写小作文。"
    "{memory_line}"
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
