"""情绪展示与只读状态摘要（P2-07 第二阶段自 emotion/service.py 下沉）。

只依赖状态机对象、映射表文字与 VAD 纯函数，不持有服务状态、不落盘、不调 LLM；
EmotionService 保留同名方法作为薄包装。外部调用面（`/lbm_emotion_config`、
`/lbm_dashboard`、planner/replyer 注入）语义不变。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from . import descriptions
from .vad import _level, map_emotions

#: 三维粗粒度短词（状态总览用；五档 → 中/偏高…）
_COARSE_WORDS = {
    "很低": "很低",
    "偏低": "偏低",
    "中等": "中",
    "偏高": "偏高",
    "很高": "很高",
}


def primary_emotion_description(vad: Any, descriptions_map: Optional[Dict[str, Any]] = None) -> str:
    """当前主导情绪的详细文字描述；映射表外/无命中 → 通用固定描述。"""
    descriptions_map = descriptions_map or {}
    try:
        labels, _nearest = map_emotions(vad)
    except Exception:
        labels = []
    if not labels:
        return descriptions_map.get(descriptions.GENERIC_LABEL) or descriptions.generic_description(
            descriptions.GENERIC_LABEL)
    primary, _share = labels[0]
    desc = descriptions_map.get(str(primary))
    if not desc:
        desc = descriptions.generic_description(str(primary))
    try:
        if len(labels) > 1 and float(labels[1][1]) >= 0.25:
            desc = f"{desc}同时夹杂着一点「{labels[1][0]}」的成分。"
    except (TypeError, ValueError):
        pass
    return desc


def emotion_description_text(vad: Any, descriptions_map: Optional[Dict[str, Any]],
                             lover_name: Any, channel: str = "planner") -> str:
    """F5 双人称渲染：planner 第三人称（bot 名）、replyer 第二人称。"""
    body = primary_emotion_description(vad, descriptions_map)
    if channel == "replyer":
        return f"你此刻的情绪：{body}"
    name = str(lover_name or "麦麦")
    return f"{name}此刻的情绪：{body}"


def params_summary(machine: Any) -> str:
    """/lbm_emotion_config 查看文本。"""
    p = machine.params
    eff = machine.effective_vad()
    labels, _ = map_emotions(eff)
    label_text = "、".join(f"{n}({s:.0%})" for n, s in labels[:3]) or "复杂难名"
    lines = [
        f"初始化：{'已完成' if p.initialized else '未初始化（零锚点零偏移）'}",
        f"锚点：V={p.anchor.valence:+.2f}，A={p.anchor.arousal:+.2f}，D={p.anchor.dominance:+.2f}",
        f"静态偏移：V={p.offset.valence:+.2f}，A={p.offset.arousal:+.2f}，D={p.offset.dominance:+.2f}",
        f"乘数：正向增益 ×{p.positive_gain:.2f}，负向衰减 ×{p.negative_decay:.2f}",
        f"当前 VAD（含偏移）：V={eff.valence:+.2f}，A={eff.arousal:+.2f}，D={eff.dominance:+.2f}｜情绪底色：{label_text}",
        f"当天情绪事件：{len(machine.emotion_log)} 条",
    ]
    if p.reasons:
        for key in ("anchor", "offset", "multiplier"):
            if p.reasons.get(key):
                lines.append(f"推导理由·{key}：{p.reasons[key]}")
    return "\n".join(lines)


def status_snapshot(machine: Any, mood_text: str) -> Dict[str, Any]:
    """聚合总览用的只读状态快照（不落盘、不改状态机任何字段）。"""
    eff = machine.effective_vad()
    labels, _nearest = map_emotions(eff)
    params = machine.params
    primary_label = str(labels[0][0]) if labels else ""
    primary_share = round(float(labels[0][1]), 4) if labels else 0.0
    recent_events = []
    for event in list(getattr(machine, "emotion_log", None) or [])[-3:]:
        delta = getattr(event, "delta_vad", None)
        if isinstance(delta, dict):
            v, a, d = (delta.get("valence"), delta.get("arousal"), delta.get("dominance"))
        else:
            v, a, d = (getattr(delta, "valence", 0.0), getattr(delta, "arousal", 0.0),
                       getattr(delta, "dominance", 0.0))
        timestamp = str(getattr(event, "timestamp", "") or "")
        recent_events.append({
            "time": timestamp[11:16] if len(timestamp) >= 16 else timestamp,
            "desc": str(getattr(event, "event_description", "") or "")[:40],
            "v": round(float(v if v is not None else 0.0), 4),
            "a": round(float(a if a is not None else 0.0), 4),
            "d": round(float(d if d is not None else 0.0), 4),
        })
    return {
        "initialized": bool(params.initialized),
        "mood_text": mood_text,
        "labels": [str(name) for name, _share in labels[:3]],
        "label": primary_label,
        "share": primary_share,
        "vad_words": {
            "v": _COARSE_WORDS.get(_level(eff.valence), _level(eff.valence)),
            "a": _COARSE_WORDS.get(_level(eff.arousal), _level(eff.arousal)),
            "d": _COARSE_WORDS.get(_level(eff.dominance), _level(eff.dominance)),
        },
        "effective": eff.to_dict(),
        "event_count": len(machine.emotion_log),
        "recent_events": recent_events,
        "anchor": params.anchor.to_dict(),
        "offset": params.offset.to_dict(),
        "frozen": bool(getattr(machine, "frozen", False)),
    }
