"""VAD 三维情绪模型核心（设计文档 §一/§四/§七）。

- 数值范围统一 [-1.0, 1.0]：V 效价（愉悦度）、A 唤醒度、D 支配感；
- 25 种情绪原型（10 正面 / 3 中性 / 12 负面），映射用加权欧氏距离
  （V 1.0 / A 0.8 / D 1.2，区分"恐惧/愤怒"时 D 权重更高）；
- 距离阈值内取最近标签，多条命中输出复合描述（激活度=1/距离 归一化）；
- 全部超阈值 → 低分辨率三档描述兜底（给对话 LLM 模糊反馈，防长期漂移）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

VAD_WEIGHTS = (1.0, 0.8, 1.2)
#: 距离阈值：低于它才认为命中某个情绪原型（设计文档 §七.2）
LABEL_DISTANCE_THRESHOLD = 0.55
#: 复合描述最多同时输出几个标签
MAX_COMBINED_LABELS = 3


def _clip(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


@dataclass
class VADState:
    """VAD 三维情绪状态，各维取值 [-1, 1]。"""

    valence: float = 0.0
    arousal: float = 0.0
    dominance: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {"valence": round(self.valence, 4), "arousal": round(self.arousal, 4),
                "dominance": round(self.dominance, 4)}

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "VADState":
        data = data or {}
        return cls(
            valence=_clip(data.get("valence", 0.0)),
            arousal=_clip(data.get("arousal", 0.0)),
            dominance=_clip(data.get("dominance", 0.0)),
        )

    def clipped(self) -> "VADState":
        return VADState(_clip(self.valence), _clip(self.arousal), _clip(self.dominance))

    def intensity(self) -> float:
        """情绪强度 = √(v² + a² + d²)（事件日志用）。"""
        return math.sqrt(self.valence**2 + self.arousal**2 + self.dominance**2)


@dataclass
class EmotionEvent:
    """当天情绪事件（原始增量，供睡眠裁判使用；设计文档 §五.2）。"""

    timestamp: str
    event_description: str
    delta_vad: VADState
    raw_intensity: float

    def to_dict(self) -> Dict[str, Any]:
        return {"timestamp": self.timestamp, "event_description": self.event_description,
                "delta_vad": self.delta_vad.to_dict(), "raw_intensity": round(self.raw_intensity, 4)}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmotionEvent":
        return cls(
            timestamp=str(data.get("timestamp") or ""),
            event_description=str(data.get("event_description") or ""),
            delta_vad=VADState.from_dict(data.get("delta_vad")),
            raw_intensity=float(data.get("raw_intensity") or 0.0),
        )


# ── 25 种情绪原型（V/A/D 三维区间；来源：设计文档 §四）──────────────────
# (标签, (v_lo, v_hi), (a_lo, a_hi), (d_lo, d_hi))
EMOTION_PROTOTYPES: List[Tuple[str, Tuple[float, float], Tuple[float, float], Tuple[float, float]]] = [
    # 基础 10 种
    ("狂喜", (0.7, 1.0), (0.6, 1.0), (0.5, 1.0)),
    ("平静", (0.3, 0.8), (-0.8, -0.2), (0.2, 0.7)),
    ("期待", (0.2, 0.8), (0.3, 0.7), (-0.2, 0.4)),
    ("愤怒", (-0.9, -0.3), (0.6, 1.0), (0.4, 0.9)),
    ("恐惧", (-0.9, -0.4), (0.7, 1.0), (-0.9, -0.4)),
    ("悲伤", (-1.0, -0.5), (-0.5, -0.1), (-0.8, -0.3)),
    ("焦虑", (-0.7, -0.2), (0.5, 0.9), (-0.7, -0.2)),
    ("疲惫", (-0.4, 0.1), (-0.9, -0.4), (-0.5, 0.0)),
    ("厌恶", (-0.9, -0.4), (0.2, 0.6), (0.3, 0.8)),
    ("无奈", (-0.5, -0.1), (-0.4, 0.2), (-0.9, -0.5)),
    # 拓展 15 种
    ("骄傲", (0.6, 0.9), (0.4, 0.8), (0.6, 1.0)),
    ("嫉妒", (-0.7, -0.3), (0.4, 0.7), (-0.6, -0.2)),
    ("羞耻", (-0.8, -0.4), (0.3, 0.6), (-0.9, -0.6)),
    ("无聊", (-0.3, 0.1), (-0.8, -0.5), (0.0, 0.4)),
    ("好奇", (0.3, 0.7), (0.4, 0.7), (-0.1, 0.4)),
    ("感激", (0.7, 1.0), (0.2, 0.6), (-0.2, 0.3)),
    ("挫败", (-0.6, -0.2), (0.5, 0.8), (-0.5, -0.1)),
    ("释然", (0.4, 0.8), (-0.6, -0.2), (0.3, 0.7)),
    ("轻蔑", (-0.6, -0.2), (0.2, 0.5), (0.6, 0.9)),
    ("忧郁", (-0.6, -0.2), (-0.7, -0.3), (-0.6, -0.2)),
    ("敬畏", (0.5, 0.9), (0.6, 0.9), (-0.7, -0.3)),
    ("惊喜", (0.4, 0.8), (0.7, 1.0), (-0.4, 0.2)),
    ("内疚", (-0.8, -0.4), (0.3, 0.7), (-0.7, -0.3)),
    ("冷漠", (-0.4, 0.0), (-0.9, -0.6), (0.3, 0.7)),
    ("怀旧", (0.2, 0.6), (-0.3, 0.2), (-0.2, 0.3)),
]

# 原型中心点（区间中点），映射时计算加权欧氏距离
_PROTO_CENTERS = [
    (name, ((v[0] + v[1]) / 2, (a[0] + a[1]) / 2, (d[0] + d[1]) / 2))
    for name, v, a, d in EMOTION_PROTOTYPES
]

#: 供 LLM 提示词使用的情绪表摘要（标签 + VAD 中心，砍掉区间细节控制 token）
EMOTION_TABLE_SUMMARY = "\n".join(
    f"- {name}：V={v:+.1f}，A={a:+.1f}，D={d:+.1f}" for name, (v, a, d) in _PROTO_CENTERS
)


def weighted_distance(vad: VADState, center: Tuple[float, float, float]) -> float:
    """加权欧氏距离（V 1.0 / A 0.8 / D 1.2）。"""
    wv, wa, wd = VAD_WEIGHTS
    return math.sqrt(
        wv * (vad.valence - center[0]) ** 2
        + wa * (vad.arousal - center[1]) ** 2
        + wd * (vad.dominance - center[2]) ** 2
    )


def map_emotions(vad: VADState, *, threshold: float = LABEL_DISTANCE_THRESHOLD,
                 max_labels: int = MAX_COMBINED_LABELS) -> Tuple[List[Tuple[str, float]], Optional[str]]:
    """VAD → 情绪标签（含混合比例）。

    Returns:
        ([(标签, 激活占比)], 最近标签或 None)。
        激活占比 = (1/距离) 归一化；全部超阈值时返回空列表。
    """
    scored = sorted(
        ((name, weighted_distance(vad, center) + 1e-6) for name, center in _PROTO_CENTERS),
        key=lambda kv: kv[1],
    )
    nearest = scored[0][0] if scored else None
    hits = [(name, dist) for name, dist in scored[:max_labels] if dist <= threshold]
    if not hits:
        return [], nearest
    inv = [1.0 / dist for _, dist in hits]
    total = sum(inv)
    return [(name, share / total) for (name, _), share in zip(hits, inv)], nearest


_LEVELS = ("很低", "偏低", "中等", "偏高", "很高")


def _level(value: float) -> str:
    """单维五档描述。"""
    if value <= -0.5:
        return _LEVELS[0]
    if value <= -0.15:
        return _LEVELS[1]
    if value < 0.15:
        return _LEVELS[2]
    if value < 0.5:
        return _LEVELS[3]
    return _LEVELS[4]


def low_resolution_description(vad: VADState) -> str:
    """低分辨率情绪描述（模糊化反馈，防 LLM 长期漂移与表达脱节）。"""
    return f"愉悦度：{_level(vad.valence)}；唤醒度：{_level(vad.arousal)}；支配感：{_level(vad.dominance)}"


def mood_summary(vad: VADState) -> str:
    """情绪底色一句话：标签（含混合比例）+ 低分辨率描述。"""
    hits, _ = map_emotions(vad)
    if hits:
        label_text = "、".join(f"{name}({share:.0%})" for name, share in hits[:2])
    else:
        label_text = "复杂难名"
    return f"{label_text}。{low_resolution_description(vad)}"


def prototypes_as_dict() -> List[Dict[str, Any]]:
    return [
        {"label": name, "valence": [v[0], v[1]], "arousal": [a[0], a[1]], "dominance": [d[0], d[1]]}
        for name, v, a, d in EMOTION_PROTOTYPES
    ]
