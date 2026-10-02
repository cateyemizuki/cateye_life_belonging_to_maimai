"""VAD 外部状态机（设计文档 §二/§五/§九/§十）。

职责划分（开环控制）：
- LLM 只做"事件→增量"映射，不持有情绪状态、不可视绝对值；
- 本状态机维护 VAD 绝对数值：日常指数衰减、增量结算、静态偏移与乘数、睡眠大修。

结算顺序（设计文档 §九.3）：
    应用日常衰减 → 加增量 → 应用静态偏移 → 应用乘数 → 裁剪
为避免静态偏移每轮重复累加，内部维护"原始 VAD"（vad_raw），
偏移与乘数在 effective_vad()（对外呈现/给裁判的值）中统一结算。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from .vad import EmotionEvent, VADState, _clip


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class EmotionParams:
    """人设推导的情绪参数（设计文档 §十：锚点/静态偏移/乘数）。

    数值约束：锚点 [-0.6, 0.6]、静态偏移 [-0.3, 0.3]、
    正向增益 [0.8, 1.3]、负向衰减 [0.6, 1.2]。
    """

    anchor: VADState = field(default_factory=VADState)
    offset: VADState = field(default_factory=VADState)
    positive_gain: float = 1.0
    negative_decay: float = 1.0
    reasons: Dict[str, str] = field(default_factory=dict)
    persona_digest: str = ""
    initialized: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "anchor": self.anchor.to_dict(),
            "offset": self.offset.to_dict(),
            "positive_gain": round(self.positive_gain, 3),
            "negative_decay": round(self.negative_decay, 3),
            "reasons": self.reasons,
            "persona_digest": self.persona_digest[:500],
            "initialized": self.initialized,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> "EmotionParams":
        data = data or {}
        return cls(
            anchor=VADState.from_dict(data.get("anchor")),
            offset=VADState.from_dict(data.get("offset")),
            positive_gain=float(data.get("positive_gain") or 1.0),
            negative_decay=float(data.get("negative_decay") or 1.0),
            reasons=dict(data.get("reasons") or {}),
            persona_digest=str(data.get("persona_digest") or ""),
            initialized=bool(data.get("initialized", False)),
        )

    @classmethod
    def default(cls) -> "EmotionParams":
        """恢复默认：零锚点、零偏移、中性乘数。"""
        return cls(anchor=VADState(), offset=VADState(), positive_gain=1.0, negative_decay=1.0,
                   reasons={}, persona_digest="", initialized=False)


def sleep_recovery_factor(sleep_hours: float) -> float:
    """睡眠恢复系数（倒 U 型曲线，设计文档 §三/§五.3）。

    睡得好→向锚点收敛越彻底（因子大）；睡不好→当天残留越多（因子小）。
    """
    hours = max(0.0, float(sleep_hours))
    if hours < 5:
        return 0.30
    if hours < 6:
        return 0.60
    if hours <= 9:
        return 0.85
    if hours <= 10:
        return 0.70
    return 0.50


class EmotionStateMachine:
    """VAD 状态机：日常增量结算 + 睡眠期大修。"""

    MAX_LOG = 200  # 当天事件日志上限

    def __init__(self, params: Optional[EmotionParams] = None, decay_rate: float = 0.08) -> None:
        self.params = params or EmotionParams.default()
        self.decay_rate = max(0.01, min(0.3, float(decay_rate)))
        self.vad_raw = VADState.from_dict(self.params.anchor.to_dict())  # 初始化=回到锚点
        self.emotion_log: List[EmotionEvent] = []
        self.last_update = datetime.now()

    # ── 结算 ────────────────────────────────────────────────
    def apply_daily_decay(self, now: Optional[datetime] = None) -> float:
        """日常小周期指数衰减：VAD = VAD · exp(-decay_rate·Δt)。返回衰减因子。"""
        now = now or datetime.now()
        hours = (now - self.last_update).total_seconds() / 3600.0
        if hours <= 0:
            return 1.0
        factor = math.exp(-self.decay_rate * hours)
        self.vad_raw.valence *= factor
        self.vad_raw.arousal *= factor
        self.vad_raw.dominance *= factor
        self.last_update = now
        return factor

    def record_event(self, event_description: str, delta: VADState, *, now: Optional[datetime] = None) -> VADState:
        """记录一次情绪事件：衰减 → 原始 VAD 加增量（裁剪）→ 写入当天日志。"""
        now = now or datetime.now()
        self.apply_daily_decay(now)
        self.vad_raw = VADState(
            _clip(self.vad_raw.valence + delta.valence),
            _clip(self.vad_raw.arousal + delta.arousal),
            _clip(self.vad_raw.dominance + delta.dominance),
        )
        self.emotion_log.append(EmotionEvent(
            timestamp=_now_iso(),
            event_description=str(event_description or "")[:200],
            delta_vad=delta.clipped(),
            raw_intensity=delta.intensity(),
        ))
        if len(self.emotion_log) > self.MAX_LOG:
            self.emotion_log = self.emotion_log[-self.MAX_LOG:]
        return self.vad_raw

    def effective_vad(self) -> VADState:
        """对外呈现的最终 VAD = 裁剪(原始 + 静态偏移) 后按符号应用乘数（乘数只作用于 V）。"""
        p = self.params
        v = _clip(self.vad_raw.valence + p.offset.valence)
        if v > 0:
            v = _clip(v * p.positive_gain)
        elif v < 0:
            v = _clip(v * p.negative_decay)
        a = _clip(self.vad_raw.arousal + p.offset.arousal)
        d = _clip(self.vad_raw.dominance + p.offset.dominance)
        return VADState(v, a, d)

    # ── 睡眠期大修 ──────────────────────────────────────────
    def apply_sleep_overhaul(self, sleep_delta: VADState, sleep_hours: float, *,
                             now: Optional[datetime] = None) -> Dict[str, Any]:
        """睡眠大修：向锚点收敛（恢复系数由睡眠时长决定）+ 裁判增量，随后清空日志。

        公式（设计文档 §十.7）：raw = 锚点 + (原始 - 锚点) × 恢复系数 + ΔVAD_sleep
        偏移与乘数在 effective_vad() 统一结算（裁判增量只作用原始层）。
        """
        now = now or datetime.now()
        factor = sleep_recovery_factor(sleep_hours)
        anchor = self.params.anchor
        before = self.vad_raw.to_dict()
        self.vad_raw = VADState(
            _clip(anchor.valence + (self.vad_raw.valence - anchor.valence) * factor + sleep_delta.valence),
            _clip(anchor.arousal + (self.vad_raw.arousal - anchor.arousal) * factor + sleep_delta.arousal),
            _clip(anchor.dominance + (self.vad_raw.dominance - anchor.dominance) * factor + sleep_delta.dominance),
        )
        result = {
            "recovery_factor": factor,
            "sleep_hours": sleep_hours,
            "before": before,
            "after_raw": self.vad_raw.to_dict(),
            "delta_applied": sleep_delta.to_dict(),
            "log_cleared": len(self.emotion_log),
        }
        self.emotion_log = []
        self.last_update = now
        return result

    # ── 参数生效 ────────────────────────────────────────────
    def apply_params(self, params: EmotionParams) -> None:
        """初始化/微调后生效：当前 VAD 直接设为锚点，清空情绪残留。"""
        self.params = params
        self.vad_raw = VADState.from_dict(params.anchor.to_dict())
        self.emotion_log = []
        self.last_update = datetime.now()

    def reset_params(self) -> None:
        """恢复默认：零锚点、零偏移、中性乘数。"""
        self.apply_params(EmotionParams.default())

    # ── 序列化 ──────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "vad_raw": self.vad_raw.to_dict(),
            "last_update": self.last_update.isoformat(timespec="seconds"),
            "emotion_log": [e.to_dict() for e in self.emotion_log],
            "params": self.params.to_dict(),
        }

    def load_dict(self, data: Optional[Dict[str, Any]]) -> None:
        data = data or {}
        self.vad_raw = VADState.from_dict(data.get("vad_raw"))
        try:
            self.last_update = datetime.fromisoformat(str(data.get("last_update")))
        except (ValueError, TypeError):
            self.last_update = datetime.now()
        self.emotion_log = [EmotionEvent.from_dict(e) for e in (data.get("emotion_log") or [])][: self.MAX_LOG]
        self.params = EmotionParams.from_dict(data.get("params"))

    def emotion_log_text(self, limit: int = 30) -> str:
        """当天情绪日志的文本形式（睡眠裁判输入）。"""
        if not self.emotion_log:
            return "（当天没有记录到情绪事件）"
        lines = []
        for e in self.emotion_log[-limit:]:
            d = e.delta_vad
            lines.append(
                f"- {e.timestamp[11:16] if len(e.timestamp) >= 16 else e.timestamp} "
                f"{e.event_description}（ΔV={d.valence:+.2f}，ΔA={d.arousal:+.2f}，ΔD={d.dominance:+.2f}，强度 {e.raw_intensity:.2f}）"
            )
        return "\n".join(lines)
