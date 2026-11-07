"""LLM 请求封装：睡眠裁判 / 人设初始化 / 回复增量评估（设计文档 §六/§十）。

三个独立请求都不携带对话历史：
- 睡眠裁判：客观数据（当前 VAD、当天情绪日志、前后日日程、睡眠时长）→
  ΔVAD_sleep = ±(0.4+0.4×[schedule_advanced].emotion_influence)，缺省调用兼容 ±0.8；
- 人设初始化：人设经 with_identity 全文注入 system_prompt → 锚点/静态偏移/乘数四组参数（带数值约束）；
- 回复增量评估：该轮回复文本（+用户侧最后一条消息上下文，§4.2 行 2）→ 事件摘要 + ΔVAD（±0.5）。
所有返回都经代码层二次校验与裁剪（LLM 数值感知失真的兜底，§十二.1）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from suite_prompts import EMOTION_SLEEP_REFEREE_DEFAULT, EMOTION_INIT_SYSTEM_DEFAULT, EMOTION_EVALUATE_DEFAULT
from suite_prompts import prompt as prompt_of, render

from .state_machine import EmotionParams
# D6（V3 接线）：情绪表摘要**不得 from-import**（导入期绑定会在覆盖生效前定型）——
# 改调运行期 getter，两处 prompt 随「默认 ⊕ 覆盖」的合并原型表实时取值。
from .vad import VADState, _clip, emotion_table_summary

SLEEP_REFEREE_PROMPT = EMOTION_SLEEP_REFEREE_DEFAULT

#: 睡眠裁判 ΔVAD 的兼容默认限幅（旧调用不传 delta_limit 时保持 ±0.8；
#: M5 服务侧一律传动态限幅 0.4+0.4×[schedule_advanced].emotion_influence）
DEFAULT_SLEEP_DELTA_LIMIT = 0.8

INIT_SYSTEM_PROMPT = EMOTION_INIT_SYSTEM_DEFAULT

EVALUATE_PROMPT = EMOTION_EVALUATE_DEFAULT


#: 评估 prompt 里「待评估正文」的默认字符预算（per_reply 旧口径；片段制由调用方放宽）
REPLY_TEXT_PROMPT_CHARS = 600
#: 保留最近正文时在头部补的省略标注（让模型知道前面还有更早的往来，不是交流的开头）
REPLY_TEXT_OMIT_MARK = "…（较早往来已省略）\n"


def _clip_prompt_body(text: str, max_chars: int, keep_recent: bool) -> str:
    """按预算裁剪待评估正文：``keep_recent=False`` 保留头部（旧语义，逐字不变）；
    ``True`` 保留**最近** ``max_chars`` 字并在头部标注省略（片段制：最新交流必须可见）。"""
    body = str(text or "")
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = REPLY_TEXT_PROMPT_CHARS
    limit = max(1, limit)
    if len(body) <= limit:
        return body
    if not keep_recent:
        return body[:limit]
    return REPLY_TEXT_OMIT_MARK + body[-limit:]


def _strip_leading_plus(text: str) -> str:
    """字符串感知地剥离 JSON 数值的前导 ``+``（部分模型输出 ``+0.04``，严格 JSON 非法）。

    只动字符串字面量**之外**、处于数值前导位置（``: , [`` 或其后空白）的 ``+``；
    字符串内部的 ``+`` 与指数记号（``1e+5``）不受影响。
    宿主 2026-10-07 夜实弹：睡眠裁判输出 ``+0.04`` 连续两轮解析失败 → 整夜冻结重试
    （每 tick 一轮 LLM 调用），第三轮模型随机去掉 ``+`` 才自愈——本兜底消除该随机性。
    """
    if "+" not in text:
        return text
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if (ch == "+" and out and out[-1] in ":,[ \t\r\n"
                and i + 1 < n and text[i + 1].isdigit()):
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _parse_json_loose(text: str) -> Optional[Dict[str, Any]]:
    """宽松 JSON 解析：整段 → ```json 围栏 → 首个 {...} 块；全失败后剥前导 ``+`` 重试一轮。"""
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence:
        candidates.insert(0, fence.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    for candidate in candidates:                     # 前导 '+' 兜底（候选优先级不变）
        normalized = _strip_leading_plus(candidate)
        if normalized == candidate:
            continue
        try:
            data = json.loads(normalized)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _delta_from(data: Any, limit: float) -> Optional[VADState]:
    """从 JSON 片段提取 ΔVAD 并做二次限幅（LLM 数值失真兜底）。

    F1（2026-11-04 模块评估 P1-10）：数值位 float 转换加 try 防护——可解析但数值位
    非法（如 ``"valence":"abc"``）时返回 None，整份回执按"无有效结构化结果"处理，
    走调用方既有的失败计次（睡眠裁判侧即 D10 重试上限/退避），不再抛 ValueError
    冒泡到循环被静默吞掉、绕过重试上限。非 dict 载荷的零向量回退语义逐字不变。
    """
    data = data or {}
    if not isinstance(data, dict):
        return VADState()
    inner = data.get("delta_vad") if isinstance(data.get("delta_vad"), dict) else data
    axes = []
    for key in ("valence", "arousal", "dominance"):
        try:
            axes.append(_clip(float(inner.get(key) or 0.0), -limit, limit))
        except (TypeError, ValueError):
            return None
    return VADState(*axes)


async def ask_sleep_referee(llm: Any, *, persona: str, vad: VADState, emotion_log_text: str,
                            yesterday_schedule: str, tomorrow_schedule: str, long_term_status: str,
                            sleep_hours: float, task_name: str,
                            delta_limit: float = DEFAULT_SLEEP_DELTA_LIMIT,
                            r_yesterday: Optional[VADState] = None,
                            e_tomorrow: Optional[VADState] = None,
                            commitment_net: float = 0.0,
                            plugin: Any = None) -> Optional[Tuple[VADState, Dict[str, str]]]:
    """睡眠裁判：返回 (ΔVAD_sleep, 理由)；请求失败/解析失败返回 None。

    M5（§4.2 睡眠大修 v2）：``delta_limit`` 为动态限幅（服务侧传
    0.4+0.4×``schedule.emotion_influence``，缺省仍为兼容值 ±0.8）；
    ``r_yesterday`` / ``e_tomorrow`` / ``commitment_net`` 为三组新增输入的数值项，
    作为裁判参考写进提示词（并已在状态机侧进入结算公式）。
    """
    try:
        limit = abs(float(delta_limit))
    except (TypeError, ValueError):
        limit = DEFAULT_SLEEP_DELTA_LIMIT
    limit = max(0.05, min(1.0, limit))
    residual = r_yesterday or VADState()
    expectation = e_tomorrow or VADState()
    try:
        net = float(commitment_net or 0.0)
    except (TypeError, ValueError):
        net = 0.0
    prompt = render(
        prompt_of(plugin, "emotion_sleep_referee"),
        fallback=SLEEP_REFEREE_PROMPT,
        persona="", v=vad.valence, a=vad.arousal, d=vad.dominance,
        emotion_log=emotion_log_text, yesterday_schedule=yesterday_schedule or "（没有记录）",
        tomorrow_schedule=tomorrow_schedule or "（没有记录）", long_term_status=long_term_status or "普通时期",
        sleep_hours=max(0.5, float(sleep_hours)), emotion_table=emotion_table_summary(),
        r_v=residual.valence, r_a=residual.arousal, r_d=residual.dominance,
        e_v=expectation.valence, e_a=expectation.arousal, e_d=expectation.dominance,
        commitment_net=net, limit=limit,
    )
    # 输出长度只使用宿主任务配置；插件只校验结构化结果。
    # 人设经 with_identity 以宿主同款开头注入 system_prompt（共享前缀缓存），
    # 用户提示词不再重复人设（persona 形参保留兼容，渲染传空防旧模板 KeyError）。
    response = await llm.generate(prompt, module="emotion", task_name=task_name,
                                  temperature=0.4, event="emotion_sleep_referee",
                                  with_identity=True)
    data = _parse_json_loose(response)
    if data is None or "delta_vad" not in data:
        return None
    reasons = {
        "valence": str(data.get("reason_valence") or "")[:300],
        "arousal": str(data.get("reason_arousal") or "")[:300],
        "dominance": str(data.get("reason_dominance") or "")[:300],
    }
    delta = _delta_from(data, limit)
    if delta is None:          # F1：数值位非法 → 与解析失败同口径（调用方计次重试）
        return None
    return delta, reasons


async def derive_params_from_persona(llm: Any, persona_text: str, task_name: str,
                                     plugin: Any = None) -> Optional[EmotionParams]:
    """人设初始化：人设文本 → 四组参数（校验+裁剪到约束区间）。

    人设统一经 ``with_identity=True`` 以宿主同款开头全文注入 system_prompt
    （共享前缀缓存，不再截断）；用户提示词只下达推导任务。``persona_text``
    形参保留兼容（仅用于 persona_digest 落盘摘要）。
    """
    response = await llm.generate(
        prompt="请根据系统提示词开头给出的人设全文，推导该角色的情绪锚点、静态偏移、"
               "增益与衰减系数，并按约定输出严格 JSON。",
        system_prompt=render(
            prompt_of(plugin, "emotion_init_system"),
            fallback=INIT_SYSTEM_PROMPT,
            emotion_table=emotion_table_summary(),
        ),
        module="emotion", task_name=task_name, temperature=0.4,
        event="emotion_init",
        with_identity=True,
    )
    data = _parse_json_loose(response)
    if data is None or "anchor" not in data:
        return None

    def _bounded(value: Any, lo: float, hi: float, default: float = 0.0) -> float:
        try:
            return max(lo, min(hi, float(value)))
        except (TypeError, ValueError):
            return default

    anchor_raw = data.get("anchor") if isinstance(data.get("anchor"), dict) else {}
    offset_raw = data.get("offset") if isinstance(data.get("offset"), dict) else {}
    params = EmotionParams(
        anchor=VADState(_bounded(anchor_raw.get("valence"), -0.6, 0.6),
                        _bounded(anchor_raw.get("arousal"), -0.6, 0.6),
                        _bounded(anchor_raw.get("dominance"), -0.6, 0.6)),
        offset=VADState(_bounded(offset_raw.get("valence"), -0.3, 0.3),
                        _bounded(offset_raw.get("arousal"), -0.3, 0.3),
                        _bounded(offset_raw.get("dominance"), -0.3, 0.3)),
        positive_gain=_bounded(data.get("positive_gain"), 0.8, 1.3, 1.0),
        negative_decay=_bounded(data.get("negative_decay"), 0.6, 1.2, 1.0),
        reasons={k: str(v)[:400] for k, v in (data.get("reasons") or {}).items()
                 if isinstance(data.get("reasons"), dict)},
        persona_digest=persona_text[:500],
        initialized=True,
    )
    return params


async def evaluate_reply_delta(llm: Any, reply_text: str, persona: str, lover_name: str,
                               task_name: str, *, user_message: str = "",
                               reply_max_chars: int = REPLY_TEXT_PROMPT_CHARS,
                               reply_keep_recent: bool = False,
                               plugin: Any = None) -> Optional[Tuple[str, VADState]]:
    """回复/外部事件增量评估：返回 (事件摘要, ΔVAD)；失败返回 None。

    ``user_message`` 是评估上下文增强（§4.2 行 2 / §七 M3 行）：该聊天流最近一条
    用户消息文本，由调用方反查后传入；拿不到时留空（prompt 里以占位说明），
    评估仍只输出有界增量（±0.5），模型始终看不到绝对 VAD（§6.3-6）。

    ``reply_max_chars`` / ``reply_keep_recent``：待评估正文的 prompt 预算与裁剪方向。
    缺省（600 字、保留头部）是 per_reply 旧语义，调用方不改则逐字不变；片段制
    （episode）传更宽的预算 + ``reply_keep_recent=True``——片段正文超预算时保留**最近**
    的往来（头部加省略标注），否则模型只看得到片段最旧的一段、最新交流被丢掉。
    """
    body = _clip_prompt_body(reply_text, reply_max_chars, reply_keep_recent)
    # 人设经 with_identity 走 system_prompt（宿主同款开头，全文不截断）；
    # persona 形参保留兼容，渲染传空防旧定制模板残留 {persona} 触发 KeyError。
    response = await llm.generate(
        prompt=render(
            prompt_of(plugin, "emotion_evaluate"),
            fallback=EVALUATE_PROMPT,
            lover_name=lover_name, persona="",
                                      user_context=(str(user_message or "").strip()[:300]
                                                    or "（未取到用户侧消息，仅按下面内容判断）"),
                                      reply_text=body),
        module="emotion", task_name=task_name, temperature=0.2,
        event="emotion_evaluate",
        with_identity=True,
    )
    data = _parse_json_loose(response)
    if data is None:
        return None
    event = str(data.get("event") or "")[:60]
    delta = _delta_from(data, 0.5)
    if delta is None:          # F1：数值位非法 → 按评估失败处理（返回 None）
        return None
    return event, delta
