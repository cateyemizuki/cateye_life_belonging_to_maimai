"""LLM 请求封装：睡眠裁判 / 人设初始化 / 回复增量评估（设计文档 §六/§十）。

三个独立请求都不携带对话历史：
- 睡眠裁判：客观数据（当前 VAD、当天情绪日志、前后日日程、睡眠时长）→ ΔVAD_sleep（±0.8）；
- 人设初始化：纯人设文本 → 锚点/静态偏移/乘数四组参数（带数值约束）；
- 回复增量评估：该轮回复文本（+用户侧最后一条消息上下文，§4.2 行 2）→ 事件摘要 + ΔVAD（±0.5）。
所有返回都经代码层二次校验与裁剪（LLM 数值感知失真的兜底，§十二.1）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from .state_machine import EmotionParams
from .vad import EMOTION_TABLE_SUMMARY, VADState, _clip

SLEEP_REFEREE_PROMPT = """你是一个客观的情绪裁判系统，负责对 AI 角色的睡眠期情绪进行大修。
你不参与对话，只依据下面的客观数据输出 JSON。

【Bot 人设】
{persona}

【当前 VAD 绝对数值】
V: {v:+.2f}, A: {a:+.2f}, D: {d:+.2f}

【当天情绪日志（原始事件流）】
{emotion_log}

【日程信息】
前一天日程：
{yesterday_schedule}
第二天日程：
{tomorrow_schedule}
长期状态：{long_term_status}
预计睡眠时长：{sleep_hours:.1f} 小时

【情绪原型参考表】（V 效价 / A 唤醒 / D 支配）
{emotion_table}

【前一日日程情感残差 R_yesterday】（已按释怀/反刍加权求和，代码层数值项；正=释怀、负=反刍残留）
V: {r_v:+.2f}, A: {r_a:+.2f}, D: {r_d:+.2f}

【次日日程情感预期 E_tomorrow】（明日条目情感基调求和；考试/挑战 → V↓A↑D↓）
V: {e_v:+.2f}, A: {e_a:+.2f}, D: {e_d:+.2f}

【当日约定兑现情况】净兑现 {commitment_net:+.0f} 次（正=兑现多于违约，负=违约多于兑现）

【评分规则】
1. 越靠近睡前的情绪事件，权重略微增加（近因效应）；强度大的事件影响更大。
2. 睡眠时长影响恢复效果：<5 小时恢复极差（V 负向、A 升高）；6-9 小时恢复最佳（V 回升）；>10 小时睡懵（A 大幅降低）。
3. 日程预期：评估前一日日程是"释怀"还是"反刍"，第二日日程是"期待"还是"焦虑"（第二天有考试/挑战 → A 升高、D 降低）。
4. 必须结合人设性格做情绪过滤（如高神经质 → 焦虑更容易留存）。
5. R_yesterday / E_tomorrow / 约定情况是代码层已计算的参考数值，最终增量仍需你综合人设与日志判断。
6. 给出最终的 ΔVAD（增量，非绝对值），每一维范围必须在 [-{limit:.2f}, {limit:.2f}] 之间。
7. reason 中必须直接引用上面日程/情绪日志的原文片段作为依据，禁止脑补不存在的事件。

只输出 JSON：{{"delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}, "reason_valence": "...", "reason_arousal": "...", "reason_dominance": "..."}}"""

#: 睡眠裁判 ΔVAD 的兼容默认限幅（旧调用不传 delta_limit 时保持 ±0.8；
#: M5 服务侧一律传动态限幅 0.4+0.4×schedule.emotion_influence）
DEFAULT_SLEEP_DELTA_LIMIT = 0.8

INIT_SYSTEM_PROMPT = """你是一位情绪心理学专家，负责为 AI 角色建立 VAD（效价、唤醒度、支配感）三维情绪模型。
V 代表愉悦度，范围 -1.0 到 1.0，正值愉悦、负值痛苦；A 代表唤醒度，-1.0 到 1.0，正值兴奋紧张、负值平静困倦；D 代表支配感，-1.0 到 1.0，正值掌控自信、负值无力顺从。
你需要根据角色的 AI 人设，推导出该角色的情绪锚点、静态偏移值以及情绪增益与衰减系数。

【情绪原型参考表】（供锚点定位参考）
{emotion_table}

【数值约束（超范围视为无效）】
- 锚点 anchor：每一维 [-0.6, 0.6]（避免长期处于极端情绪）
- 静态偏移 offset：每一维 [-0.3, 0.3]
- 正向增益 positive_gain：[0.8, 1.3]（V>0 时乘，开心更开心/更收敛）
- 负向衰减 negative_decay：[0.6, 1.2]（V<0 时乘，难过更持久/更快消退）
- 正向静态偏移一般不超过 +0.3，保持负面情绪的比例才真实。

必须输出严格 JSON（每个参数都要给出引用人设原文的推导理由）：
{{"anchor": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}},
  "offset": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}},
  "positive_gain": 1.0, "negative_decay": 1.0,
  "reasons": {{"anchor": "...", "offset": "...", "multiplier": "..."}}}}"""

EVALUATE_PROMPT = """你是 AI 角色「{lover_name}」的情绪增量评估器。你看不到角色当前的绝对情绪数值，只根据下面这段交流内容，评估"这轮交流/这件事"给角色情绪带来的变化量 ΔVAD。

【角色人设（情绪过滤依据）】
{persona}

【用户侧最后一条消息（对话上下文；可能为空）】
{user_context}

【AI 的回复内容 / 待评估的交流内容】
{reply_text}

【要求】
1. event 用不超过 30 字概括「这轮交流发生了什么」（谁说了什么、发生了什么、结果如何），不要笼统写"日常对话"；
2. ΔV/ΔA/ΔD 每一维范围 [-0.5, 0.5]；没有明显情绪波动时全部接近 0；
3. 只输出 JSON：{{"event": "...", "delta_vad": {{"valence": 0.0, "arousal": 0.0, "dominance": 0.0}}}}"""


def _parse_json_loose(text: str) -> Optional[Dict[str, Any]]:
    """宽松 JSON 解析：整段 → ```json 围栏 → 首个 {...} 块。"""
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
    return None


def _delta_from(data: Any, limit: float) -> VADState:
    """从 JSON 片段提取 ΔVAD 并做二次限幅（LLM 数值失真兜底）。"""
    data = data or {}
    if not isinstance(data, dict):
        return VADState()
    inner = data.get("delta_vad") if isinstance(data.get("delta_vad"), dict) else data
    return VADState(
        _clip(float(inner.get("valence") or 0.0), -limit, limit),
        _clip(float(inner.get("arousal") or 0.0), -limit, limit),
        _clip(float(inner.get("dominance") or 0.0), -limit, limit),
    )


async def ask_sleep_referee(llm: Any, *, persona: str, vad: VADState, emotion_log_text: str,
                            yesterday_schedule: str, tomorrow_schedule: str, long_term_status: str,
                            sleep_hours: float, task_name: str,
                            delta_limit: float = DEFAULT_SLEEP_DELTA_LIMIT,
                            r_yesterday: Optional[VADState] = None,
                            e_tomorrow: Optional[VADState] = None,
                            commitment_net: float = 0.0) -> Optional[Tuple[VADState, Dict[str, str]]]:
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
    prompt = SLEEP_REFEREE_PROMPT.format(
        persona=persona or "（暂无人设）", v=vad.valence, a=vad.arousal, d=vad.dominance,
        emotion_log=emotion_log_text, yesterday_schedule=yesterday_schedule or "（没有记录）",
        tomorrow_schedule=tomorrow_schedule or "（没有记录）", long_term_status=long_term_status or "普通时期",
        sleep_hours=max(0.5, float(sleep_hours)), emotion_table=EMOTION_TABLE_SUMMARY,
        r_v=residual.valence, r_a=residual.arousal, r_d=residual.dominance,
        e_v=expectation.valence, e_a=expectation.arousal, e_d=expectation.dominance,
        commitment_net=net, limit=limit,
    )
    # 三组 reason 为长文本，600 tokens 会截断 JSON（宿主验证日 8 次尝试零落地实测）
    response = await llm.generate(prompt, task_name=task_name, temperature=0.4, max_tokens=2000,
                                  event="emotion_sleep_referee")
    data = _parse_json_loose(response)
    if data is None or "delta_vad" not in data:
        return None
    reasons = {
        "valence": str(data.get("reason_valence") or "")[:300],
        "arousal": str(data.get("reason_arousal") or "")[:300],
        "dominance": str(data.get("reason_dominance") or "")[:300],
    }
    return _delta_from(data, limit), reasons


async def derive_params_from_persona(llm: Any, persona_text: str, task_name: str) -> Optional[EmotionParams]:
    """人设初始化：人设文本 → 四组参数（校验+裁剪到约束区间）。"""
    response = await llm.generate(
        prompt=str(persona_text or "")[:2000],
        system_prompt=INIT_SYSTEM_PROMPT.format(emotion_table=EMOTION_TABLE_SUMMARY),
        task_name=task_name, temperature=0.4, max_tokens=900, event="emotion_init",
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
                               task_name: str, *, user_message: str = "") -> Optional[Tuple[str, VADState]]:
    """回复/外部事件增量评估：返回 (事件摘要, ΔVAD)；失败返回 None。

    ``user_message`` 是评估上下文增强（§4.2 行 2 / §七 M3 行）：该聊天流最近一条
    用户消息文本，由调用方反查后传入；拿不到时留空（prompt 里以占位说明），
    评估仍只输出有界增量（±0.5），模型始终看不到绝对 VAD（§6.3-6）。
    """
    response = await llm.generate(
        prompt=EVALUATE_PROMPT.format(lover_name=lover_name, persona=(persona or "（暂无人设）")[:800],
                                      user_context=(str(user_message or "").strip()[:300]
                                                    or "（未取到用户侧消息，仅按下面内容判断）"),
                                      reply_text=str(reply_text or "")[:600]),
        task_name=task_name, temperature=0.2, max_tokens=150, event="emotion_evaluate",
    )
    data = _parse_json_loose(response)
    if data is None:
        return None
    event = str(data.get("event") or "")[:60]
    return event, _delta_from(data, 0.5)
