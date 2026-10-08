"""VAD 三维情绪模型核心（设计文档 §一/§四/§七）。

- 数值范围统一 [-1.0, 1.0]：V 效价（愉悦度）、A 唤醒度、D 支配感；
- 25 种情绪原型（10 正面 / 3 中性 / 12 负面），映射用加权欧氏距离
  （V 1.0 / A 0.8 / D 1.2，区分"恐惧/愤怒"时 D 权重更高）；
- 距离阈值内取最近标签，多条命中输出复合描述（激活度=1/距离 归一化）；
- 全部超阈值 → 低分辨率三档描述兜底（给对话 LLM 模糊反馈，防长期漂移）。

M12（V3 接线）：原型表与五档阈值可被可选 ``<data_dir>/emotion_mapping.json`` 覆盖——模块内
``EMOTION_PROTOTYPES`` / ``EMOTION_TABLE_SUMMARY`` 保持为**代码默认基线**，运行期生效值在
``_PROTO_CENTERS`` / ``_LEVEL_THRESHOLDS`` / ``_EMOTION_TABLE_SUMMARY`` 三个生效位（装载时由
:func:`apply_mapping_tables` 重绑：默认 ⊕ 覆盖）。映射算法（权重/阈值/复合条数/激活占比）
与 ``_LEVELS`` 档词/比较链**不开放**覆盖。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

# M12（V3 接线）：生效阈值默认基线取自覆盖层的声明位（`DEFAULT_LEVEL_THRESHOLDS`），
# 使 `_level` 的切点成为单一真源。本 import 无环：`mapping_override` 顶层只依赖标准库，
# 它对 vad/bridge 的引用全部延迟到函数体内（见其 `default_tables`）。
from . import mapping_override as _mapping_override

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

# 原型中心点（区间中点），映射时计算加权欧氏距离（**代码默认基线**，不随覆盖变化：
# `mapping_override.default_tables()` 读本处作默认；运行期生效值见 `_PROTO_CENTERS`）
_DEFAULT_PROTO_CENTERS = [
    (name, ((v[0] + v[1]) / 2, (a[0] + a[1]) / 2, (d[0] + d[1]) / 2))
    for name, v, a, d in EMOTION_PROTOTYPES
]


def _format_table_summary(centers: List[Tuple[str, Tuple[float, float, float]]]) -> str:
    """情绪表摘要文本（标签 + VAD 中心，砍掉区间细节控制 token）。"""
    return "\n".join(
        f"- {name}：V={v:+.1f}，A={a:+.1f}，D={d:+.1f}" for name, (v, a, d) in centers
    )


# ── 生效位（V3 接线：默认 ⊕ 覆盖；由装载方经 apply_mapping_tables 推入）──────────
# **生效**原型中心：``map_emotions`` 调用期读本全局（装载时重绑）；无覆盖 = 代码默认。
_PROTO_CENTERS: List[Tuple[str, Tuple[float, float, float]]] = list(_DEFAULT_PROTO_CENTERS)
# **生效**五档阈值（默认基线 = mapping_override.DEFAULT_LEVEL_THRESHOLDS；见 apply 函数）
_LEVEL_THRESHOLDS: Tuple[float, ...] = tuple(float(x) for x in
                                             _mapping_override.DEFAULT_LEVEL_THRESHOLDS)
# **生效**摘要文本（覆盖后按合并原型重算；运行期读取入口 = emotion_table_summary()）
_EMOTION_TABLE_SUMMARY: str = _format_table_summary(_PROTO_CENTERS)

#: 情绪表摘要的**默认基线**文本（供 LLM 提示词使用；标签 + VAD 中心，砍掉区间细节控制 token）。
#: V3 接线（D6）：覆盖生效时本常量不随动——referee 两处 prompt 已改为运行期调用
#: :func:`emotion_table_summary`，不再 from-import 本常量；本常量保留 = 默认形态快照/对照用。
EMOTION_TABLE_SUMMARY = _format_table_summary(_DEFAULT_PROTO_CENTERS)


def emotion_table_summary() -> str:
    """当前**生效**的情绪表摘要文本（默认 ⊕ 覆盖；D6：提示词的运行期读取入口）。"""
    return _EMOTION_TABLE_SUMMARY


def apply_mapping_tables(*, prototypes: Optional[List[Dict[str, Any]]] = None,
                         level_thresholds: Optional[Any] = None) -> None:
    """把「默认 ⊕ 覆盖」的生效原型表 / 五档阈值推入本模块（V3 接线；装载方调用）。

    - ``prototypes=None`` → 回代码默认（``prototypes_as_dict()``）；否则为
      ``mapping_override`` 校验输出的区间形状表（中心点按 ``(lo+hi)/2`` 重派生）；
    - ``level_thresholds`` 长度非 4 / 不可转数值 → 回默认四点（比较链与档词冻结，语义不变）；
    - 覆盖为空表时不改现状（校验层本不放行空表，此处只防映射被打成无标签）。
    """
    global _PROTO_CENTERS, _EMOTION_TABLE_SUMMARY, _LEVEL_THRESHOLDS
    table = prototypes_as_dict() if prototypes is None else prototypes
    centers = _mapping_override.prototype_centers(table) or list(_PROTO_CENTERS)
    _PROTO_CENTERS = centers
    _EMOTION_TABLE_SUMMARY = _format_table_summary(centers)
    thresholds: Tuple[float, ...] = ()
    if level_thresholds is not None:
        try:
            thresholds = tuple(float(x) for x in level_thresholds)
        except (TypeError, ValueError):
            thresholds = ()
    if len(thresholds) != _mapping_override.LEVEL_THRESHOLD_COUNT:
        thresholds = tuple(float(x) for x in _mapping_override.DEFAULT_LEVEL_THRESHOLDS)
    _LEVEL_THRESHOLDS = thresholds


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
    """单维五档描述（切点 = 生效阈值 ``_LEVEL_THRESHOLDS``：V3 接线后可被 JSON 覆盖）。

    比较链与档词**冻结**：``<=c1`` / ``<=c2`` / ``<c3`` / ``<c4`` / 其余 —— 左闭右开的
    不对称语义原样保留（默认四点 = (-0.5, -0.15, 0.15, 0.5) 时逐值等于接线前行为）。
    """
    c1, c2, c3, c4 = _LEVEL_THRESHOLDS
    if value <= c1:
        return _LEVELS[0]
    if value <= c2:
        return _LEVELS[1]
    if value < c3:
        return _LEVELS[2]
    if value < c4:
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
