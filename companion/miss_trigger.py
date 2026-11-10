"""Optional missing-turn prose; actual policy lives in initiative_profile."""
from __future__ import annotations
from typing import Any, Optional
MEMORY_LINE_MAX_CHARS = 120
EMOTION_VALENCE_THRESHOLD = .3


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
