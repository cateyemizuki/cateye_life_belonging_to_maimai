"""VAD 外部状态机（设计文档 §二/§五/§九/§十）。

职责划分（开环控制）：
- LLM 只做"事件→增量"映射，不持有情绪状态、不可视绝对值；
- 本状态机维护 VAD 绝对数值：日常指数衰减、增量结算、静态偏移与乘数、睡眠大修；
- M5 睡眠冻结：进入 sleep 条目后事件只入队（``pending_events``），衰减/结算暂停；
  醒后解冻并把队列并入日志（只作观测，不二次入账）。``frozen``/``pending_events``
  是运行期状态、不落盘（热重载后由睡眠循环重新判定，避免复活陈旧冻结态）；
- M5 连续低迷日统计：每日 effective V 采样（``sample_daily_v``）→ 日均线（``daily_v_mean``）
  → 连续低迷计数（``update_low_mood_streak``，同日幂等）；三者随状态落盘，
  旧状态文件缺键一律按默认空读取（§八.2 向后兼容）。

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


#: 睡眠恢复系数下界（§4.2 睡眠大修 v2 / §6.3-3：恒 ≥0.3，保证向锚点收敛）
MIN_SLEEP_RECOVERY_FACTOR = 0.3
#: 睡眠大修 v2 数值项权重（SYNERGY §3.1：+ R_yesterday×w1 + E_tomorrow×w2；§4.2 输入扩展）
SLEEP_RESIDUAL_WEIGHT = 0.5
SLEEP_EXPECTATION_WEIGHT = 0.5
#: 当日约定净兑现（kept − broken 次数）的情绪项与夹取（§4.2 第三输入）
SLEEP_COMMITMENT_WEIGHT = 0.1
SLEEP_COMMITMENT_CLAMP = 0.3
#: 日程情感残差/预期的逐维上限（与条目 emotional_tone 的 ±0.5 同界，防多项求和发散）
SCHEDULE_TONE_CLAMP = 0.5
#: 连续低迷软约束（MAINTENANCE_PLAN §4.2 / §6.2 F）：日均线 V 阈值 / 连续天数门槛 / 日采样桶保留天数
LOW_MOOD_THRESHOLD = -0.5
LOW_MOOD_MIN_DAYS = 3
DAILY_V_KEEP_DAYS = 7


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
    下界恒 ≥ :data:`MIN_SLEEP_RECOVERY_FACTOR`（§4.2「恢复系数恒 ≥0.3 保证收敛」）。
    """
    hours = max(0.0, float(sleep_hours))
    if hours < 5:
        return MIN_SLEEP_RECOVERY_FACTOR
    if hours < 6:
        return 0.60
    if hours <= 9:
        return 0.85
    if hours <= 10:
        return 0.70
    return 0.50


def _clamp_state(state: Optional[VADState], limit: float) -> VADState:
    """把 VAD 逐维夹到 ±limit（None 视为零向量；limit ≤0 时返回零向量）。"""
    try:
        bound = abs(float(limit))
    except (TypeError, ValueError):
        bound = 0.0
    state = state or VADState()
    return VADState(
        _clip(state.valence, -bound, bound),
        _clip(state.arousal, -bound, bound),
        _clip(state.dominance, -bound, bound),
    )


class EmotionStateMachine:
    """VAD 状态机：日常增量结算 + 睡眠期冻结/大修。"""

    MAX_LOG = 200  # 当天事件日志上限
    MAX_PENDING = 200  # 睡眠冻结期入队事件上限（超限丢最旧）

    def __init__(self, params: Optional[EmotionParams] = None, decay_rate: float = 0.08) -> None:
        self.params = params or EmotionParams.default()
        self.decay_rate = max(0.01, min(0.3, float(decay_rate)))
        self.vad_raw = VADState.from_dict(self.params.anchor.to_dict())  # 初始化=回到锚点
        self.emotion_log: List[EmotionEvent] = []
        self.last_update = datetime.now()
        # M5 睡眠冻结（运行期状态，不落盘：热重载后由睡眠循环重新判定，避免复活陈旧冻结态）
        self.frozen = False
        self.pending_events: List[EmotionEvent] = []
        self._frozen_at: Optional[datetime] = None
        # M5 连续低迷日统计（落盘；旧状态文件缺这些键 → 默认空，向后兼容读取）
        #   daily_v: date -> {"sum": 当日 effective V 采样和, "count": 采样次数}（滚动 7 天）
        self.daily_v: Dict[str, Dict[str, float]] = {}
        self.low_mood_streak: int = 0            # 连续"日均线 V<阈值"的天数
        self.low_mood_last_eval_date: str = ""   # 已评估到的"日"（补评用，日期递增）
        self.low_mood_checked_date: str = ""     # 已跑过日切评估的"当天"（每日最多评估一次）

    # ── 睡眠冻结（M5；§4.2 睡眠结算改造）──────────────────────
    def freeze(self, now: Optional[datetime] = None) -> None:
        """进入睡眠条目 → 冻结：事件只入队，衰减与评估结算暂停（幂等）。"""
        if self.frozen:
            return
        self.frozen = True
        self._frozen_at = now or datetime.now()

    def unfreeze(self, now: Optional[datetime] = None, *, drain_to_log: bool = True) -> List[EmotionEvent]:
        """醒后解冻：推进 ``last_update``（冻结时段不参与衰减），返回并清空冻结期队列。

        ``drain_to_log=True`` 时把队列并入当天日志（只作观测保留，**不再二次入账**）——
        夜间事件的结算由缓存的大修一次性完成（§4.2「应用缓存并解冻」）。
        """
        if not self.frozen:
            return []
        self.frozen = False
        self._frozen_at = None
        self.last_update = now or datetime.now()
        drained = list(self.pending_events)
        self.pending_events = []
        if drain_to_log and drained:
            self.emotion_log.extend(drained)
            if len(self.emotion_log) > self.MAX_LOG:
                self.emotion_log = self.emotion_log[-self.MAX_LOG:]
        return drained

    # ── 结算 ────────────────────────────────────────────────
    def apply_daily_decay(self, now: Optional[datetime] = None) -> float:
        """日常小周期指数衰减：VAD = VAD · exp(-decay_rate·Δt)。返回衰减因子。

        冻结期（睡眠中）暂停衰减并恒返回 1.0（``last_update`` 不推进，解冻时统一推进，
        避免醒后补算整夜衰减）。
        """
        if self.frozen:
            return 1.0
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
        """记录一次情绪事件：衰减 → 原始 VAD 加增量（裁剪）→ 写入当天日志。

        冻结期（睡眠中）：事件**只入队** ``pending_events``，不衰减、不结算（§4.2）。
        """
        now = now or datetime.now()
        event = EmotionEvent(
            timestamp=_now_iso(),
            event_description=str(event_description or "")[:200],
            delta_vad=delta.clipped(),
            raw_intensity=delta.intensity(),
        )
        if self.frozen:
            self.pending_events.append(event)
            if len(self.pending_events) > self.MAX_PENDING:
                self.pending_events = self.pending_events[-self.MAX_PENDING:]
            return self.vad_raw
        self.apply_daily_decay(now)
        self.vad_raw = VADState(
            _clip(self.vad_raw.valence + delta.valence),
            _clip(self.vad_raw.arousal + delta.arousal),
            _clip(self.vad_raw.dominance + delta.dominance),
        )
        self.emotion_log.append(event)
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
                             now: Optional[datetime] = None,
                             r_yesterday: Optional[VADState] = None,
                             e_tomorrow: Optional[VADState] = None,
                             commitment_net: float = 0.0,
                             delta_limit: Optional[float] = None) -> Dict[str, Any]:
        """睡眠大修 v2：向锚点收敛 + 裁判增量 + 日程残差/预期/约定三输入，随后清空日志。

        公式（设计文档 §十.7 + SYNERGY §3.1 / MAINTENANCE_PLAN §4.2）::

            raw = 锚点 + (原始 − 锚点) × 恢复系数(睡眠时长倒U，恒 ≥0.3)
                  + ΔVAD裁判(±delta_limit，动态限幅)
                  + R_yesterday × w1 + E_tomorrow × w2 + clamp(约定净兑现 × w3)

        - ``r_yesterday`` / ``e_tomorrow``：前日情感残差与明日情感预期（日程条目
          ``emotional_tone`` 求和，逐维再夹 ±0.5），缺省零向量（旧调用行为不变）；
        - ``commitment_net``：当日约定净兑现次数（kept − broken）；
        - ``delta_limit``：裁判增量动态限幅（服务侧传 0.4+0.4×``schedule.emotion_influence``）；
          None 表示不做额外夹取（兼容旧调用）。
        偏移与乘数仍在 effective_vad() 统一结算（三者只作用原始层）。
        """
        now = now or datetime.now()
        factor = max(MIN_SLEEP_RECOVERY_FACTOR, sleep_recovery_factor(sleep_hours))
        residual = _clamp_state(r_yesterday, SCHEDULE_TONE_CLAMP)
        expectation = _clamp_state(e_tomorrow, SCHEDULE_TONE_CLAMP)
        try:
            net = float(commitment_net or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        commitment = _clip(net * SLEEP_COMMITMENT_WEIGHT, -SLEEP_COMMITMENT_CLAMP, SLEEP_COMMITMENT_CLAMP)
        delta = _clamp_state(sleep_delta, delta_limit) if delta_limit is not None \
            else VADState(sleep_delta.valence, sleep_delta.arousal, sleep_delta.dominance)
        anchor = self.params.anchor
        before = self.vad_raw.to_dict()
        self.vad_raw = VADState(
            _clip(anchor.valence + (self.vad_raw.valence - anchor.valence) * factor
                  + delta.valence + residual.valence * SLEEP_RESIDUAL_WEIGHT
                  + expectation.valence * SLEEP_EXPECTATION_WEIGHT + commitment),
            _clip(anchor.arousal + (self.vad_raw.arousal - anchor.arousal) * factor
                  + delta.arousal + residual.arousal * SLEEP_RESIDUAL_WEIGHT
                  + expectation.arousal * SLEEP_EXPECTATION_WEIGHT),
            _clip(anchor.dominance + (self.vad_raw.dominance - anchor.dominance) * factor
                  + delta.dominance + residual.dominance * SLEEP_RESIDUAL_WEIGHT
                  + expectation.dominance * SLEEP_EXPECTATION_WEIGHT),
        )
        result = {
            "recovery_factor": factor,
            "sleep_hours": sleep_hours,
            "before": before,
            "after_raw": self.vad_raw.to_dict(),
            "delta_requested": sleep_delta.to_dict(),
            "delta_applied": delta.to_dict(),
            "delta_limit": delta_limit,
            "r_yesterday": residual.to_dict(),
            "e_tomorrow": expectation.to_dict(),
            "commitment_net": net,
            "commitment_delta": round(commitment, 4),
            "log_cleared": len(self.emotion_log),
        }
        self.emotion_log = []
        self.last_update = now
        return result

    # ── 连续低迷日统计（M5；§4.2 / §6.2 F）────────────────────
    def sample_daily_v(self, date: str, value: float, *,
                       keep_days: int = DAILY_V_KEEP_DAYS) -> None:
        """把当前 effective V 采样进 ``date`` 的日均桶（滚动保留最近 keep_days 天）。"""
        key = str(date or "")
        if not key:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        bucket = self.daily_v.setdefault(key, {"sum": 0.0, "count": 0})
        bucket["sum"] = float(bucket.get("sum") or 0.0) + number
        bucket["count"] = int(bucket.get("count") or 0) + 1
        limit = max(1, int(keep_days))
        if len(self.daily_v) > limit:
            for old in sorted(self.daily_v)[:-limit]:
                self.daily_v.pop(old, None)

    def daily_v_mean(self, date: str) -> Optional[float]:
        """某日 effective V 采样均值；无采样返回 None。"""
        bucket = self.daily_v.get(str(date or "")) or {}
        count = int(bucket.get("count") or 0)
        if count <= 0:
            return None
        return float(bucket.get("sum") or 0.0) / count

    def update_low_mood_streak(self, date: str, *,
                               threshold: float = LOW_MOOD_THRESHOLD) -> int:
        """按 ``date`` 的日均线 V 更新连续低迷天数并返回。

        - 日均 < ``threshold`` → 连续 +1；否则（含无采样日）→ 归零；
        - 幂等：同一 ``date`` 重复调用、或 ``date`` 早于已评估日（乱序补评）→ 直接返回现值，
          绝不把已计入的旧日子重复计数。
        """
        key = str(date or "")
        last = str(self.low_mood_last_eval_date or "")
        if not key or (last and key <= last):
            return self.low_mood_streak
        mean = self.daily_v_mean(key)
        is_low = mean is not None and float(mean) < float(threshold)
        self.low_mood_streak = int(self.low_mood_streak) + 1 if is_low else 0
        self.low_mood_last_eval_date = key
        return self.low_mood_streak

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
            # M5 连续低迷日统计（旧版本读到时忽略；旧状态文件缺这些键 → 本文件默认值）
            "daily_v": {d: {"sum": round(float(v.get("sum") or 0.0), 4),
                            "count": int(v.get("count") or 0)}
                        for d, v in self.daily_v.items() if isinstance(v, dict)},
            "low_mood_streak": int(self.low_mood_streak),
            "low_mood_last_eval_date": str(self.low_mood_last_eval_date),
            "low_mood_checked_date": str(self.low_mood_checked_date),
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
        # 连续低迷日统计（缺键/类型异常 → 空默认，绝不因旧状态文件启动失败）
        raw_daily = data.get("daily_v")
        self.daily_v = {}
        if isinstance(raw_daily, dict):
            for day, bucket in raw_daily.items():
                if not isinstance(bucket, dict):
                    continue
                try:
                    self.daily_v[str(day)] = {"sum": float(bucket.get("sum") or 0.0),
                                              "count": int(bucket.get("count") or 0)}
                except (TypeError, ValueError):
                    continue
        try:
            self.low_mood_streak = max(0, int(data.get("low_mood_streak") or 0))
        except (TypeError, ValueError):
            self.low_mood_streak = 0
        self.low_mood_last_eval_date = str(data.get("low_mood_last_eval_date") or "")
        self.low_mood_checked_date = str(data.get("low_mood_checked_date") or "")

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
