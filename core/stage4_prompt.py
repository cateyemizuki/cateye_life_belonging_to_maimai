# -*- coding: utf-8 -*-
"""阶段 4（目标私聊清醒沉默 >120h）提示词尾缀与互斥短语清洗。

**单一 owner**：``companion.miss_trigger`` 与 ``tools.proactive_turn`` 都从这里
转发，避免同一阶段 4 文案在不同入口（plugin 点播通知 / optional_action 派发 /
问候 / 刷QQ）得到不同清洗结果（复核 finding）。本模块只做纯文本处理：不读配置、
不碰业务状态、不发消息，导入无副作用。

占位符只允许 ``{hours}`` / ``{relation}`` / ``{memory_line}``。
"""
from __future__ import annotations

from typing import Any, Optional

#: 通用每日主动任务尾缀（早/午/晚安、刷QQ随口搭话、巡检日常等）。
MISS_REASON_STAGE4_SUFFIX = (
    "\n【深层心理覆盖·失联阶段4（已沉默约{hours}小时）】"
    "注意：陪伴目标（关系：{relation}）已经超过五天完全没有回复过你任何消息。"
    "虽然本轮由日常行为触发了这次开口机会，但你在表达上述日常事件或问候的同时，"
    "整个人已处于明显沉默低落、压抑着失落却又克制不住牵挂的状态。"
    "请在完成上述日常分享或问候的同时（或紧接在后半句），"
    "务必带上沉闷、略带犹豫但反复确认的询问：问对方人到底去哪了、是不是还在、"
    "或者到底能不能看到你发的消息。不要再假装一切如常地轻松寒暄，"
    "先说出要做的日常事，话锋自然落到反复确认对方是否还在、为什么始终没有声音上。"
    "{memory_line}"
)

#: 视频分享/分析类主动任务专用尾缀（对接“分析视频给对方，同时问人去哪了”）。
MISS_REASON_STAGE4_VIDEO_SUFFIX = (
    "\n【深层心理覆盖·失联阶段4（已沉默约{hours}小时）】"
    "注意：陪伴目标（关系：{relation}）已经连续超过五天没有任何回信。"
    "你刚刚看了一个视频并且习惯性地想发给TA/跟TA聊聊视频内容，请先顺着视频内容简单说两句你的感想或分析；"
    "但话说到后面，由于这么久一直没人回应，你的情绪不可避免地沉了下来。"
    "请在聊完视频的最后或紧接着补上一句沉默又执拗的追问：问TA人到底去哪了、"
    "这么多天过去了是不是还在、这条消息还会不会有人看。"
    "展现出“就算没人回也还在下意识跟你分享日常，但心里已经憋不住想问你在不在”的真实挣扎感。"
    "{memory_line}"
)

#: 阶段 4 拼接前必须剥离的互斥指令（日常任务模板自带的「不要追问 / 可静默放弃」等）。
#: 按「长短语优先」排序，先剥组合句再剥单词；两份旧表（companion / tools）的并集
#: 加上刷QQ原文与点播通知里的实际句式。
STAGE4_SUPPRESSED_PHRASES = (
    "不要追问对方、不要开新话题",
    "不要追问对方，不要开新话题",
    "不要追问对方,不要开新话题",
    "不要追问对方;不要开新话题",
    "不要追问对方",
    "不要追问不休",
    "不要追问",
    "不要开新话题",
    "轻松关心",
    "不必刻意强调想念",
    "无需追问对方未回应的细节",
    "无需追问对方未回应",
    "不要求回应的话；也可以什么都不说",
    "也不要求问候、询问、汇报日程或表达想念",
    "没什么想说就不调用任何工具，直接结束",
    "没什么想说就不调用任何工具；直接结束",
    "没什么想说就不调用任何工具、直接结束",
    "没什么想说就不调用任何工具",
    "没什么想说，就不调用任何工具，直接结束",
    "也可以什么都不说",
    "可以自然地带一句，也可以选择不提",
    "直接结束本轮即可",
)

#: 兼容旧名（内部/外部既有引用）。
_SUPPRESSED_PHRASES = STAGE4_SUPPRESSED_PHRASES

#: 阶段 4 尾缀标题（幂等/剥离的确定性标记）。
STAGE4_SUFFIX_MARK = "【深层心理覆盖·失联阶段4"


def strip_stage4_suffix(text: Any) -> str:
    """移除已拼接的阶段 4 尾缀（facts 已回到 stage 0 时避免残留矛盾指令）。"""
    value = str(text or "")
    index = value.find(STAGE4_SUFFIX_MARK)
    if index < 0:
        return value
    return value[:index].rstrip(" \t\r\n")


def _clean_stage4_base(base_prompt: Any) -> str:
    """剥离与阶段 4 冲突的「不要追问/可静默放弃」短语并清理悬空标点。"""
    cleaned = str(base_prompt or "").strip()
    for phrase in STAGE4_SUPPRESSED_PHRASES:
        cleaned = cleaned.replace(phrase, "")
    cleaned = cleaned.replace("、、", "、")
    for pair, replacement in (("，。", "。"), ("。，", "。"), ("，、", "，"),
                              ("、，", "，"), ("、。", "。"), ("。、", "。"),
                              ("；。", "。"), ("。；", "。"), ("；，", "，"),
                              ("，；", "；"), ("；、", "；"), ("、；", "；"),
                              ("，，", "，"), ("；；", "；"), ("。。", "。")):
        while pair in cleaned:
            cleaned = cleaned.replace(pair, replacement)
    cleaned = cleaned.strip("、，； \t\r\n")
    # 纯标点/表情（没有字母数字/汉字）等同空 base，避免输出「，。＋尾缀」。
    if not any(ch.isalnum() for ch in cleaned):
        return ""
    return cleaned


def clean_stage4_prompt(text: Any) -> str:
    """公开的互斥短语清洗（供已在别处拼好尾缀的调用方复用，避免漏清洗）。"""
    return _clean_stage4_base(text)


def _format_hours(hours: Any, *, nonpositive_text: Optional[str] = None) -> str:
    """阶段 4 尾缀的小时数（非法入参 → 0.0，绝不因文案拼接抛异常）。

    ``nonpositive_text`` 给定时，小时数 <= 0（含非法/NaN）返回该文案；
    ``tools.proactive_turn`` 用 ``"120+"`` 表达「调用方拿不到精确小时数」，
    默认 ``None`` 保持历史 ``0.0`` 口径。
    """
    try:
        value = float(hours)
    except (TypeError, ValueError):
        return nonpositive_text or "0.0"
    if value != value:  # NaN
        return nonpositive_text or "0.0"
    if nonpositive_text is not None and value <= 0:
        return nonpositive_text
    return f"{value:.1f}"


def append_stage4_miss_prompt(
    base_prompt: Any,
    *,
    hours: Any,
    relation: str = "恋人",
    memory_line: str = "",
    is_video: bool = False,
    nonpositive_hours_text: Optional[str] = None,
) -> str:
    """在每日主动任务提示词后拼接阶段 4 深度想念文案，并清洗互斥短语。

    - ``base_prompt`` 为空（或清洗后只剩标点）→ 直接返回尾缀（去掉前导换行）；
    - 非空 → 保留清理后的日常任务骨架，换行拼接尾缀；
    - ``is_video=True`` 使用视频分享专用尾缀（先聊视频再追问），否则用通用尾缀；
    - ``nonpositive_hours_text`` 给定时，小时数 <= 0/非法 → 该文案（默认 ``0.0``）；
    - 占位符只有 ``{hours}`` / ``{relation}`` / ``{memory_line}``，与
      :func:`companion.miss_trigger.memory_line` 的输出直接兼容。
    """
    cleaned = _clean_stage4_base(base_prompt)
    tpl = MISS_REASON_STAGE4_VIDEO_SUFFIX if is_video else MISS_REASON_STAGE4_SUFFIX
    suffix = tpl.format(
        hours=_format_hours(hours, nonpositive_text=nonpositive_hours_text),
        relation=str(relation or "恋人"),
        memory_line=str(memory_line or ""),
    )
    return f"{cleaned}{suffix}" if cleaned else suffix.lstrip("\n")
