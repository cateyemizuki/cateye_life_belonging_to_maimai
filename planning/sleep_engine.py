"""睡眠引擎 SleepEngine（日程系统二期 PR3；定稿 §4.2 + 附录 B.2）。

R7 依据 = 用户 2026-10-26 直派【日程系统二期 PR3 SleepEngine】；D10/D11 依据 =
用户 2026-10-27 批准（裁判重试上限/退避、午休退出夜间冻结链，emotion 侧落实）。

职责（0 LLM，纯离线可测）：

- **连续睡眠曲线**（附录 B.2.4 八段）：``sleep_rates_at`` / ``integrate_sleep_gains``
  在连续段任意两点之间做**分段精确积分**——跨分段不近似，所以「每 5 分钟一拍累计」
  与「一次睡满 8h」得到同一个总量，不会 tick 重复恢复。
- **觉醒判定**（附录 B.2.3）：最早起床 ``[schedule].wake_time`` → 阈值
  ``wake_energy_threshold``（默认 0.75，按 ``sleep_wake_check_seconds`` 复检）；
  ``[schedule_advanced].lie_in_max_until``（默认 12:00）必起；硬约定优先，提前
  ``sleep_hard_commitment_lead_minutes``（默认 30 分钟）醒。硬约定/上限判定**不受**
  「未到最早起床」分支屏蔽（定稿 §1 锁定 #12 高于附录 B 的书写顺序）。
- **打断状态机**：``note_interruption`` 只记账；确认清醒 ≥
  ``sleep_interrupt_min_minutes``（默认 5）才把连续段归零（再入睡从最低档开始），
  更短只计入 ``interrupted_minutes`` / ``interruption_count``。
- **一次性结算**：醒来时只结算一次 ``sleep_sessions`` 行、只发布一次
  ``life.sleep_woke``、只调用一次 ``episode_closer`` / LifeState 结算口；事件发送
  集合随会话 ``metadata.emitted`` 持久化，重启后不重发。睡眠债公式只在**会话结束**
  算一次，tick 内绝不入账（避免与 ``LifeState.handle_sleep`` 三档重复记账）。
- **过睡只扣精力**（定稿 §1 锁定 #14）：附录 B.2.4 表的 focus 负收益只有在
  ``[life_state].sleep_oversleep_penalize_focus=true``（默认 false，本轮新增键）
  时才生效；默认过睡不动专注/压力。
- **no_sleep_mode**：禁用引擎觉醒判定（不产生 wake、不改端点），旧结算语义由
  emotion 既有链路保留；引擎返回 ``no_sleep_mode=True`` 让编排方走旧路径。
- **单一 wake provider**：``is_asleep`` / ``sleep_status`` 供 emotion 优先询问；
  引擎关闭 / 未装配 → 返回 ``None``（消费方回退旧锚点窗口，逐字旧行为）。

依赖纪律：本模块**不 import emotion / rolling_planner**；所有外部能力（SQLite 会话
存储、episode 关闭、schedule_items 端点写回、LifeState 结算、emotion 延长窗口）全部
经构造参数注入，缺失即静默降级（B.5.1「失败静默回退」）。
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .time_utils import DAY_MINUTES, TimezoneManager, normalize_day_minutes
from .schedule_items import is_night_sleep_item
from .circadian import (bounded, circadian_sleepiness, night_window, onset_band,
                       phase_shift, sleep_score)
from core.internal_tuning import tuning_section

logger = logging.getLogger(__name__)

# ── 事件名（附录 B.6.1；与 schedule.* 兼容事件不重复发布）────────────────────
EVENT_SLEEP_STARTED = "life.sleep_started"
EVENT_SLEEP_EXTENDED = "life.sleep_extended"
EVENT_SLEEP_INTERRUPTED = "life.sleep_interrupted"
EVENT_SLEEP_WOKE = "life.sleep_woke"

# ── 配置默认值（附录 B.6.3 六键 + 本轮新增过睡专注惩罚键）────────────────────
DEFAULT_WAKE_ENERGY_THRESHOLD = 0.75
DEFAULT_OVERSLEEP_START_HOURS = 9.0
DEFAULT_INTERRUPT_MIN_MINUTES = 5.0
DEFAULT_WAKE_CHECK_SECONDS = 300
DEFAULT_HARD_COMMITMENT_LEAD_MINUTES = 30
#: 显式「当前没有有效硬约定」（2026-10-27 用户批准：约定删除/改期要**立即撤销**
#: 原提前唤醒）。语义严格区分：
#: - ``None`` = 本次未提供新数据 → 沿用会话 metadata 缓存（旧行为）；
#: - :data:`NO_HARD_COMMITMENT` = 调用方明确表示"当前无有效约定" → 忽略并清除缓存；
#: - ``datetime`` = 有效约定开始时刻 → 使用并缓存。
NO_HARD_COMMITMENT: Any = object()
DEFAULT_LIE_IN_MAX_UNTIL = "12:00"
#: 过睡专注惩罚开关（定稿 §1 #14：默认只扣精力；此键默认 false，本轮新增）
DEFAULT_OVERSLEEP_PENALIZE_FOCUS = False

#: 觉醒原因白名单（附录 B.2.1；状态枚举钉死，不虚构新值）。``offline_reconnect``
#: （2026-10-29 离线睡眠补全）= 宿主离线段按 A1 分级结算后的补记醒沿：只由编排方
#: （plugin 装配链）写入 sleep_sessions 行与 ``life.sleep_woke`` 载荷，引擎自身
#: 不会产出该值（``_close_session`` 白名单放行后引擎路径仍不经过它）。
WAKE_REASONS: Tuple[str, ...] = (
    "energy_threshold", "lie_in_cap", "hard_commitment", "interrupted_awake",
    "manual", "no_sleep_mode", "offline_reconnect", "wake_window_end",
)

#: 连续睡眠曲线（附录 B.2.4 八段；``(start_h, end_h, energy_per_hour, focus_per_hour)``）
SLEEP_CURVE: Tuple[Tuple[float, float, float, float], ...] = (
    (0.0, 0.5, 0.04, 0.02),
    (0.5, 2.0, 0.10, 0.05),
    (2.0, 4.0, 0.14, 0.07),
    (4.0, 6.0, 0.12, 0.06),
    (6.0, 8.0, 0.08, 0.04),
    (8.0, 9.0, 0.02, 0.01),
    (9.0, 10.0, -0.02, -0.03),
    (10.0, math.inf, -0.05, -0.05),
)
#: 睡眠期其它状态速率（附录 B.2.2：hunger +0.0001/min、stress -0.0005/min）
SLEEP_HUNGER_PER_MIN = 0.0001
SLEEP_STRESS_PER_MIN = -0.0005
#: 睡眠债（小时）上限（与 planning/life_state.SLEEP_DEBT_MAX 同口径）
SLEEP_DEBT_MAX = 12.0


# ======================================================================
# 纯函数：曲线 / 积分 / 质量 / 债务 / 判定（无 IO、无状态）
# ======================================================================
def _clamp(value: Any, lo: float, hi: float, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN 防御
        return default
    return max(lo, min(hi, number))


def _curve_segments(oversleep_start_hours: float = DEFAULT_OVERSLEEP_START_HOURS,
                    penalize_focus: bool = False
                    ) -> Tuple[Tuple[float, float, float, float], ...]:
    """生效曲线分段：``sleep_oversleep_start_hours`` 只平移「开始递减」的边界。

    - 默认 9.0h 与附录 B.2.4 表逐行一致（8~9h +0.02，9~10h −0.02，>10h −0.05）；
    - 非默认值把 8h~边界整段按 +0.02 处理，边界后 1h 内 −0.02、再往后 −0.05
      （配置值钳 [8, 12]；非法回退 9）；
    - ``penalize_focus=False``（默认，定稿 §1 #14）→ focus 在过睡段恒 0.0，
      只有精力吃负收益；true 时才用附录表 focus 负值。
    """
    start = _clamp(oversleep_start_hours, 8.0, 12.0, DEFAULT_OVERSLEEP_START_HOURS)
    focus_oversleep_a = -0.03 if penalize_focus else 0.0
    focus_oversleep_b = -0.05 if penalize_focus else 0.0
    return (
        (0.0, 0.5, 0.04, 0.02),
        (0.5, 2.0, 0.10, 0.05),
        (2.0, 4.0, 0.14, 0.07),
        (4.0, 6.0, 0.12, 0.06),
        (6.0, 8.0, 0.08, 0.04),
        (8.0, start, 0.02, 0.01),
        (start, start + 1.0, -0.02, focus_oversleep_a),
        (start + 1.0, math.inf, -0.05, focus_oversleep_b),
    )


def sleep_rates_at(continuous_hours: Any, *,
                   oversleep_start_hours: float = DEFAULT_OVERSLEEP_START_HOURS,
                   penalize_focus: bool = DEFAULT_OVERSLEEP_PENALIZE_FOCUS
                   ) -> Tuple[float, float, float, float]:
    """连续段某一点的 ``(energy/h, focus/h, hunger/min, stress/min)`` 速率。

    前两项按附录 B.2.4 曲线；hunger/stress 固定取睡眠期速率。连续段非法
    （负数/NaN）按 0h（入睡期最低档）处理，不抛异常。
    """
    hours = max(0.0, _clamp(continuous_hours, 0.0, 240.0, 0.0))
    for seg_start, seg_end, energy_rate, focus_rate in _curve_segments(
            oversleep_start_hours, penalize_focus):
        if hours < seg_end or seg_end == math.inf:
            return energy_rate, focus_rate, SLEEP_HUNGER_PER_MIN, SLEEP_STRESS_PER_MIN
    return -0.05, (-0.05 if penalize_focus else 0.0), SLEEP_HUNGER_PER_MIN, SLEEP_STRESS_PER_MIN


def integrate_sleep_gains(start_hours: Any, end_hours: Any, *,
                          oversleep_start_hours: float = DEFAULT_OVERSLEEP_START_HOURS,
                          penalize_focus: bool = DEFAULT_OVERSLEEP_PENALIZE_FOCUS,
                          quality: Any = None) -> Dict[str, float]:
    """连续段 ``[start_hours, end_hours]`` 的分段精确积分（跨段自动切分）。

    Returns:
        ``{"energy": Δ, "focus": Δ, "hunger": Δ, "stress": Δ, "hours": 跨度}``；
        ``quality`` 非 None 时只对**正收益**乘 ``0.85 + 0.15*quality``（附录 B.2.2
        「正收益乘 quality 系数」，过睡负收益不打折）。
    """
    a = max(0.0, _clamp(start_hours, 0.0, 240.0, 0.0))
    b = max(0.0, _clamp(end_hours, 0.0, 240.0, 0.0))
    if b < a:
        a, b = b, a
    span = b - a
    energy = 0.0
    focus = 0.0
    for seg_start, seg_end, energy_rate, focus_rate in _curve_segments(
            oversleep_start_hours, penalize_focus):
        lo = max(a, seg_start)
        hi = min(b, seg_end)
        if hi <= lo:
            continue
        width = hi - lo
        energy += energy_rate * width
        focus += focus_rate * width
    factor = quality_multiplier(quality) if quality is not None else 1.0
    if factor != 1.0:
        if energy > 0:
            energy *= factor
        if focus > 0:
            focus *= factor
    return {
        "energy": energy,
        "focus": focus,
        "hunger": SLEEP_HUNGER_PER_MIN * span * 60.0,
        "stress": SLEEP_STRESS_PER_MIN * span * 60.0,
        "hours": span,
    }


def quality_multiplier(quality: Any) -> float:
    """``0.85 + 0.15*quality``（quality∈[0,1]；非法按 0.5 = 无额外起伏）。"""
    q = _clamp(quality, 0.0, 1.0, 0.5)
    return 0.85 + 0.15 * q


def sleep_quality(interrupted_minutes: Any, total_sleep_minutes: Any) -> float:
    """``1 - interrupted/total``（钳 [0,1]；total<=0 → 1.0，避免除零）。"""
    total = _clamp(total_sleep_minutes, 0.0, 60.0 * 240.0, 0.0)
    interrupted = _clamp(interrupted_minutes, 0.0, total, 0.0)
    if total <= 0:
        return 1.0
    return max(0.0, min(1.0, 1.0 - interrupted / total))


def sleep_debt_delta(actual_hours: Any, baseline_hours: Any = 8.0) -> float:
    """单次睡眠的债务增量（附录 B.2.2 原式；不 clamp，clamp 在 :func:`settle_sleep_debt`）。

    复核 P0-4：零债基准参数化（默认 8.0 保持旧行为）——用户配置窗口短于 8h
    （如 01:00–08:30=7.5h）时以窗口长为基准，睡满配置窗口不再净增债。
    """
    hours = _clamp(actual_hours, 0.0, 240.0, 0.0)
    baseline = _clamp(baseline_hours, 4.0, 12.0, 8.0)
    return max(0.0, baseline - hours) * 0.5 - min(1.0, max(0.0, hours - baseline))


def settle_sleep_debt(debt_before: Any, actual_hours: Any,
                      baseline_hours: Any = 8.0) -> float:
    """一次性睡眠债结算：``clamp(before + delta, 0, 12)``（只应在会话结束时调用）。"""
    before = _clamp(debt_before, 0.0, SLEEP_DEBT_MAX, 0.0)
    return max(0.0, min(SLEEP_DEBT_MAX,
                        before + sleep_debt_delta(actual_hours, baseline_hours)))


def debt_baseline_hours(sleep_minute: Any, wake_minute: Any) -> float:
    """复核 P0-4：零债基准 = min(8.0, 配置作息窗口环长)。

    窗口环长取 ``(wake - sleep) mod 1440``（跨午夜通用）；非法/零长回退 8.0，
    下限 4.0 防病态配置把基准压到 nap 量级。长窗口用户基准仍为 8.0（不多还）。
    """
    try:
        span = (int(wake_minute) - int(sleep_minute)) % DAY_MINUTES
    except (TypeError, ValueError):
        return 8.0
    if span <= 0:
        return 8.0
    return min(8.0, max(4.0, span / 60.0))


#: 复核 P0-4：高债恢复期 latest_wake 顺延——债 >4h 起每 1h 债顺延 15min，封顶 90min。
DEBT_WAKE_EXTENSION_START = 4.0
DEBT_WAKE_EXTENSION_PER_HOUR = 15.0
DEBT_WAKE_EXTENSION_MAX_MINUTES = 90.0


def debt_wake_extension_minutes(debt: Any) -> float:
    """复核 P0-4：恢复期觉醒上限顺延量（分钟）。

    原 latest_wake=wake+drift 截死补觉空间：短窗口+晚入睡时数学上永远不可能
    还债。债 ≤4 不顺延（日常不扰醒锚），高债线性顺延且仍受 lie_in_cap 约束。
    """
    d = _clamp(debt, 0.0, SLEEP_DEBT_MAX, 0.0)
    if d <= DEBT_WAKE_EXTENSION_START:
        return 0.0
    return min(DEBT_WAKE_EXTENSION_MAX_MINUTES,
               (d - DEBT_WAKE_EXTENSION_START) * DEBT_WAKE_EXTENSION_PER_HOUR)


def sleep_recovery_target(debt_at_sleep: Any, *, max_bonus: Any = 1.0) -> float:
    """夜间恢复目标（PR-SLEEP-RECOVERY 3.17.19，规格 §1A）：

    ``8.0 + min(1.0, debt/12) * max_bonus``（债 0 → 8h，债 12 → 8+bonus；线性有界）。
    债钳 [0, 12]、bonus 钳 [0, 4]（非法回退默认），纯函数无 IO。只作觉醒保护
    目标，不参与债结算（债仍按真实有效时长公式，不伪算还债）。
    """
    debt = _clamp(debt_at_sleep, 0.0, SLEEP_DEBT_MAX, 0.0)
    bonus = _clamp(max_bonus, 0.0, 4.0, 1.0)
    return 8.0 + bonus * min(1.0, debt / SLEEP_DEBT_MAX)


def next_occurrence(after: datetime, minute_of_day: Any, *,
                    inclusive: bool = False) -> datetime:
    """``after`` 之后（默认严格晚于）最近的钟点时刻（跨午夜/非默认作息通用）。"""
    minute = normalize_day_minutes(minute_of_day)
    base = after.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    if inclusive and base == after:
        return base
    while base <= after:
        base = base + timedelta(days=1)
    return base


@dataclass(frozen=True)
class WakeDecision:
    """一次觉醒判定结果（``reason`` 只取 :data:`WAKE_REASONS`）。"""

    awake: bool
    reason: Optional[str] = None
    deadline: Optional[datetime] = None
    disabled: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "awake": bool(self.awake),
            "reason": self.reason,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "disabled": bool(self.disabled),
        }


def hard_commitment_deadline(hard_commitment_start: Optional[datetime],
                             lead_minutes: Any = DEFAULT_HARD_COMMITMENT_LEAD_MINUTES
                             ) -> Optional[datetime]:
    """硬约定唤醒时刻 = 约定开始 − 提前量（默认 30 分钟；非法/负值按 0 钳）。"""
    if hard_commitment_start is None:
        return None
    lead = max(0.0, _clamp(lead_minutes, 0.0, 240.0,
                           DEFAULT_HARD_COMMITMENT_LEAD_MINUTES))
    return hard_commitment_start - timedelta(minutes=lead)


def wake_decision(now: datetime, *, energy: Any, earliest_wake: datetime,
                  lie_in_cap: datetime,
                  hard_commitment_start: Optional[datetime] = None,
                  hard_commitment_lead_minutes: float =
                  DEFAULT_HARD_COMMITMENT_LEAD_MINUTES,
                  wake_energy_threshold: float = DEFAULT_WAKE_ENERGY_THRESHOLD,
                  check_due: bool = True,
                  no_sleep_mode: bool = False,
                  effective_sleep_hours: Any = 0.0,
                  min_effective_hours: Optional[float] = None) -> WakeDecision:
    """纯觉醒判定（附录 B.2.3 + 定稿 §4.2.1 优先级）。

    顺序（最早截止优先，硬约定与上限都比阈值优先，**且都不被最早起床分支屏蔽**）：

    1. ``no_sleep_mode`` → 不启用引擎觉醒判定（``disabled=True``，不产生 wake）；
    2. ``lie_in_cap`` 必起；硬约定 ``start - lead`` 早于上限时按硬约定醒（硬约定
       优先；两者同为最早截止时取硬约定）；**硬约定阈值早于最早起床时照样先醒**
       （定稿 §1 #12 高于附录 B 伪代码顺序）；
    3. 未到最早起床 ``earliest_wake`` → 不因阈值醒；
    4. 阈值判定只在 ``check_due`` 为真时做（``sleep_wake_check_seconds`` 复检节流，
       到点/硬约定不受该节流影响）；
    5. ``energy >= wake_energy_threshold`` → ``energy_threshold``。

    最小有效睡眠保护（PR-SLEEP-RECOVERY 3.17.19，规格 §1C）：
    ``min_effective_hours`` 非 None 时，第 5 步阈值醒额外要求
    ``effective_sleep_hours >= min_effective_hours``（未达标继续睡）；
    **lie_in_cap / 硬约定分支不受影响**（仍在最前，优先于一切）。
    ``min_effective_hours=None``（缺省）= 旧行为逐字不变。
    """
    if no_sleep_mode:
        return WakeDecision(False, None, None, disabled=True)
    hard_due = hard_commitment_deadline(hard_commitment_start,
                                        hard_commitment_lead_minutes)
    # 1) 上限必起 / 硬约定（最早截止优先；同刻硬约定优先）
    if now >= lie_in_cap:
        if hard_due is not None and hard_due <= lie_in_cap and now >= hard_due:
            return WakeDecision(True, "hard_commitment", hard_due)
        return WakeDecision(True, "lie_in_cap", lie_in_cap)
    if hard_due is not None and now >= hard_due:
        return WakeDecision(True, "hard_commitment", hard_due)
    # 2) 最早起床前不因阈值醒（硬约定/上限已在上方先行判定）
    if now < earliest_wake:
        return WakeDecision(False, None, None)
    # 3) 阈值（按复检节流）；最小有效睡眠保护只 gate 本分支（规格 §1C）
    if check_due and _clamp(energy, 0.0, 1.0, 0.0) >= _clamp(
            wake_energy_threshold, 0.0, 1.0, DEFAULT_WAKE_ENERGY_THRESHOLD):
        if min_effective_hours is None or _clamp(
                effective_sleep_hours, 0.0, 240.0, 0.0) >= _clamp(
                min_effective_hours, 0.0, 240.0, 0.0):
            return WakeDecision(True, "energy_threshold", earliest_wake)
    return WakeDecision(False, None, None)


def resolve_hard_commitment_start(intents: Iterable[Any], frame_date: str,
                                  after: datetime,
                                  horizon: Optional[datetime] = None,
                                 include_active: bool = False, *,
                                 allow_midnight: bool = False
                                  ) -> Optional[datetime]:
    """从 spine 意图里取 ``after`` 之后最近的**硬约定**开始时刻（0 LLM、纯读）。

    ``intents`` 行形如 ``rolling_planner.extract_spine_intents`` 的输出
    （``hardness/start_minute/end_minute/day_offset/item_id``）；``start_minute`` 允许
    >1440（跨午夜环口径）。只认 ``hardness == "hard"`` 且窗口有效的行；
    ``day_offset == "1"``（次日午夜段）保守跳过，与 planner 既有口径一致。
    ``include_active=True``：额外接受「窗口已开始但 ``after`` 仍在窗内」的硬约定（返回其
    开始时刻，使 ``hard_commitment_deadline`` 落在过去 → 引擎立即正式起床）。默认 False =
    原有「只取未来」语义逐字不变。解析失败/无硬约定 → None（引擎退化为无硬约定，不虚构）。
    """
    try:
        midnight = datetime.strptime(frame_date, "%Y-%m-%d").replace(tzinfo=after.tzinfo)
    except (TypeError, ValueError):
        return None
    best: Optional[datetime] = None
    for intent in intents or []:
        if not isinstance(intent, dict):
            continue
        # Sleep anchors belong to this engine, not to the waking-appointment
        # deadline. Otherwise a hard 01:00 sleep row wakes itself at 01:05.
        if is_night_sleep_item(intent):
            continue
        if str(intent.get("hardness") or "").strip().lower() != "hard":
            continue
        # P3 核验门槛：仅用户明确确认的硬约定（匹配到 pending_commitment、带
        # commitment_hardness=hard）可触发提前醒；LLM 新生成、来源不明的 hard
        # 意图不作正式起床依据，避免临时硬意图意外唤醒。睡眠锚点已在上面排除。
        if str(intent.get("commitment_hardness") or "").strip().lower() != "hard":
            continue
        if str(intent.get("day_offset")) == "1":
            continue
        try:
            start = int(intent["start_minute"]) if allow_midnight else int(intent.get("start_minute") or 0)
            end = int(intent.get("end_minute") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if start < (0 if allow_midnight else 1) or end <= start:
            continue
        start_at = midnight + timedelta(minutes=start)
        if start_at <= after:
            if not include_active:
                continue
            end_at = midnight + timedelta(minutes=end)
            if after >= end_at:
                continue  # 窗口已结束：不是本拍有效的正式起床依据
            if horizon is not None and start_at > horizon:
                continue
            if best is None or start_at < best:
                best = start_at
            continue
        if horizon is not None and start_at > horizon:
            continue
        if best is None or start_at < best:
            best = start_at
    return best
# ======================================================================
# 打断 / 连续段状态机（纯内存，可独立测）
# ======================================================================
@dataclass
class InterruptionRecord:
    """打断状态机的持久化形态（随 life_state / sleep_sessions.metadata 落盘）。"""

    interrupted_minutes: float = 0.0
    interruption_count: int = 0
    interrupted: bool = False
    segment_started_at: Optional[datetime] = None
    interrupt_started_at: Optional[datetime] = None
    last_note_at: Optional[datetime] = None

    def total_interrupted_minutes(self, now: datetime) -> float:
        total = float(self.interrupted_minutes)
        if self.interrupted and self.last_note_at is not None and now > self.last_note_at:
            total += (now - self.last_note_at).total_seconds() / 60.0
        return max(0.0, total)


@dataclass
class AdvanceResult:
    """一个 tick 的连续段推进结果。"""

    continuous_hours_before: float = 0.0
    continuous_hours_after: float = 0.0
    reset: bool = False
    interrupted_minutes: float = 0.0
    interruption_count: int = 0


class SleepClock:
    """连续睡眠段 + 打断计数状态机（无 IO；时间全部由调用方给）。

    - ``start(now)`` 建立连续段起点；
    - ``advance(now, threshold)``：若打断仍在进行且距最后一次打扰 ≥ 阈值 →
      **确认清醒**：连续段归零（``segment_started_at = now``），本拍返回 ``reset=True``；
      若距最后一次打扰 < 阈值 → 认为又睡着了（段不重置，打扰时长入账）；
    - ``note_interruption(now, threshold)``：记账；同一次打扰内（相邻间隔 < 阈值）
      不重置、不重复计数；间隔 ≥ 阈值说明上一段清醒已确认 → 归零并计新一次打断。
    """

    def __init__(self) -> None:
        self.record = InterruptionRecord()

    # -- 生命周期 ------------------------------------------------------
    def start(self, now: datetime) -> None:
        self.record = InterruptionRecord(segment_started_at=now)

    @property
    def started_at(self) -> Optional[datetime]:
        return self.record.segment_started_at

    def continuous_minutes(self, now: datetime) -> float:
        start = self.record.segment_started_at
        if start is None or now <= start:
            return 0.0
        return (now - start).total_seconds() / 60.0

    # -- 推进 ----------------------------------------------------------
    def advance(self, now: datetime, threshold_minutes: float) -> AdvanceResult:
        record = self.record
        if record.segment_started_at is None:
            record.segment_started_at = now
        before_hours = max(0.0, (now - record.segment_started_at).total_seconds() / 3600.0)
        reset = False
        threshold = max(0.0, _clamp(threshold_minutes, 0.0, 240.0,
                                    DEFAULT_INTERRUPT_MIN_MINUTES))
        if record.interrupted:
            last = record.last_note_at or record.interrupt_started_at or now
            gap = (now - last).total_seconds() / 60.0
            if gap >= threshold:
                # 确认清醒 ≥ 阈值：连续段归零，再入睡从最低档开始
                if record.interrupt_started_at is not None:
                    record.interrupted_minutes += max(
                        0.0, (now - record.interrupt_started_at).total_seconds() / 60.0)
                record.interrupted = False
                record.interrupt_started_at = None
                record.last_note_at = None
                record.segment_started_at = now
                reset = True
            elif now > last:
                # 阈值内又睡着：本段打扰结束（段不重置），打扰时长按到本拍计
                if record.interrupt_started_at is not None:
                    record.interrupted_minutes += max(
                        0.0, (now - record.interrupt_started_at).total_seconds() / 60.0)
                record.interrupted = False
                record.interrupt_started_at = None
                record.last_note_at = None
        after_hours = max(0.0, (now - record.segment_started_at).total_seconds() / 3600.0)
        return AdvanceResult(
            continuous_hours_before=before_hours,
            continuous_hours_after=after_hours,
            reset=reset,
            interrupted_minutes=record.total_interrupted_minutes(now),
            interruption_count=record.interruption_count,
        )

    # -- 打扰记账 ------------------------------------------------------
    def note_interruption(self, now: datetime, threshold_minutes: float) -> Dict[str, Any]:
        record = self.record
        if record.segment_started_at is None:
            return {"counted": False, "reset": False,
                    "interruption_count": record.interruption_count}
        threshold = max(0.0, _clamp(threshold_minutes, 0.0, 240.0,
                                    DEFAULT_INTERRUPT_MIN_MINUTES))
        reset = False
        if record.interrupted:
            last = record.last_note_at or record.interrupt_started_at or now
            gap = (now - last).total_seconds() / 60.0
            if gap >= threshold:
                # 上一段清醒已确认：截断连续段并开新打扰段
                if record.interrupt_started_at is not None:
                    record.interrupted_minutes += max(
                        0.0, (now - record.interrupt_started_at).total_seconds() / 60.0)
                record.segment_started_at = now
                record.interrupt_started_at = now
                record.interruption_count += 1
                reset = True
            record.last_note_at = now
        else:
            record.interrupted = True
            record.interrupt_started_at = now
            record.last_note_at = now
            record.interruption_count += 1
        return {"counted": True, "reset": reset,
                "interruption_count": record.interruption_count}


# ======================================================================
# 会话（sleep_sessions 行）
# ======================================================================
@dataclass
class SleepSession:
    """一次睡眠会话（字段与附录 B.2.2 ``sleep_sessions`` DDL 一一对应）。"""

    session_id: str
    schedule_date: str
    planned_start_minute: int
    planned_end_minute: int
    created_at: str
    actual_start_minute: Optional[int] = None
    actual_end_minute: Optional[int] = None
    wake_reason: Optional[str] = None
    continuous_minutes: int = 0
    interruption_count: int = 0
    energy_start: Optional[float] = None
    energy_end: Optional[float] = None
    sleep_debt_before: Optional[float] = None
    sleep_debt_after: Optional[float] = None
    quality: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    updated_at: Optional[str] = None
    # 非 DDL 列的运行时字段（随 metadata 往返，不进 INSERT 列表）
    earliest_wake_at: Optional[datetime] = None
    lie_in_cap_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None
    # PR-SLEEP-RECOVERY（3.17.19，规格 §1A）：夜间恢复目标 + 有效睡眠累计。
    # 同随 metadata 往返（to_row/from_row 经 sync_runtime_fields 持久化，schema
    # 不变）；旧行缺省 → 目标 8.0 / 累计 0.0；热重载恢复不重置。
    recovery_target_hours: float = 8.0
    effective_sleep_hours: float = 0.0

    @property
    def closed(self) -> bool:
        return self.actual_end_minute is not None

    def to_row(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "schedule_date": self.schedule_date,
            "planned_start_minute": int(self.planned_start_minute),
            "planned_end_minute": int(self.planned_end_minute),
            "actual_start_minute": self.actual_start_minute,
            "actual_end_minute": self.actual_end_minute,
            "wake_reason": self.wake_reason,
            "continuous_minutes": int(self.continuous_minutes),
            "interruption_count": int(self.interruption_count),
            "energy_start": self.energy_start,
            "energy_end": self.energy_end,
            "sleep_debt_before": self.sleep_debt_before,
            "sleep_debt_after": self.sleep_debt_after,
            "quality": self.quality,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at or self.created_at,
        }

    @classmethod
    def from_row(cls, row: Any) -> "SleepSession":
        data = dict(row or {})
        meta = data.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (ValueError, TypeError):
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        session = cls(
            session_id=str(data.get("session_id") or ""),
            schedule_date=str(data.get("schedule_date") or ""),
            planned_start_minute=int(data.get("planned_start_minute") or 0),
            planned_end_minute=int(data.get("planned_end_minute") or 0),
            created_at=str(data.get("created_at") or ""),
            actual_start_minute=data.get("actual_start_minute"),
            actual_end_minute=data.get("actual_end_minute"),
            wake_reason=data.get("wake_reason"),
            continuous_minutes=int(data.get("continuous_minutes") or 0),
            interruption_count=int(data.get("interruption_count") or 0),
            energy_start=data.get("energy_start"),
            energy_end=data.get("energy_end"),
            sleep_debt_before=data.get("sleep_debt_before"),
            sleep_debt_after=data.get("sleep_debt_after"),
            quality=data.get("quality"),
            metadata=meta,
            updated_at=data.get("updated_at"),
        )
        session.earliest_wake_at = cls._parse_meta_dt(meta.get("earliest_wake_at"))
        session.lie_in_cap_at = cls._parse_meta_dt(meta.get("lie_in_cap_at"))
        session.closed_at = cls._parse_meta_dt(meta.get("closed_at"))
        # PR-SLEEP-RECOVERY：旧行无恢复目标字段 → 缺省 8.0（兼容，不重置既有会话）
        session.recovery_target_hours = _clamp(
            meta.get("recovery_target_hours"), 8.0, 12.0, 8.0)
        session.effective_sleep_hours = _clamp(
            meta.get("effective_sleep_hours"), 0.0, 240.0, 0.0)
        return session

    @staticmethod
    def _parse_meta_dt(raw: Any) -> Optional[datetime]:
        if isinstance(raw, str) and raw.strip():
            try:
                return datetime.fromisoformat(raw.strip())
            except ValueError:
                return None
        return None

    def sync_runtime_fields(self) -> None:
        """运行时 datetime 字段 → metadata（落盘/重启恢复的唯一通道）。"""
        if self.earliest_wake_at is not None:
            self.metadata["earliest_wake_at"] = self.earliest_wake_at.isoformat()
        if self.lie_in_cap_at is not None:
            self.metadata["lie_in_cap_at"] = self.lie_in_cap_at.isoformat()
        if self.closed_at is not None:
            self.metadata["closed_at"] = self.closed_at.isoformat()
        # PR-SLEEP-RECOVERY：恢复目标/有效睡眠累计随 metadata 落盘（热重载不重置）
        self.metadata["recovery_target_hours"] = float(self.recovery_target_hours)
        self.metadata["effective_sleep_hours"] = float(self.effective_sleep_hours)


class MemorySleepSessionStore:
    """内存会话存储（测试/无 DB 降级；接口与 ScheduleDB v6 三方法同名）。"""

    def __init__(self) -> None:
        self.rows: Dict[str, SleepSession] = {}

    def upsert_sleep_session(self, session: SleepSession) -> bool:
        self.rows[session.session_id] = session
        return True

    def finish_sleep_session(self, session_id: str, **fields: Any) -> bool:
        session = self.rows.get(session_id)
        if session is None:
            return False
        for key, value in fields.items():
            if hasattr(session, key):
                setattr(session, key, value)
        return True

    def query_sleep_sessions(self, schedule_date: Optional[str] = None,
                             limit: int = 50) -> List[SleepSession]:
        rows = [s for s in self.rows.values()
                if schedule_date is None or s.schedule_date == schedule_date]
        # 与 ScheduleDB 同口径（评估 P1 修复，2026-11-04）：缺省 = 最近 limit 条
        # （DESC，重启抑制门 _recently_woke 依赖）；给定日期 = 该日升序。
        rows.sort(key=lambda s: (s.schedule_date, s.planned_start_minute),
                  reverse=schedule_date is None)
        return rows[:max(1, int(limit))]

    def open_sleep_session(self) -> Optional[SleepSession]:
        for session in self.rows.values():
            if not session.closed:
                return session
        return None
# ======================================================================
# 引擎
# ======================================================================
class SleepEngine:
    """状态驱动觉醒引擎（单一 wake provider + 连续曲线 + 一次性结算）。

    构造参数（全部可缺省；缺失对应能力即静默降级，不阻塞主循环）：

    - ``config_provider``：返回插件配置对象（读 ``[life_state]`` / ``[schedule]`` /
      ``[schedule_advanced]``；段缺失按附录默认值，默认关纪律不变）；
    - ``life_state``：LifeStateService 形状对象。用到的口（全部可选）：
      ``snapshot()`` / ``apply_sleep_fields(fields)``（连续段字段落盘）/
      ``apply_sleep_settlement(payload)``（会话结束一次性结算，**不覆盖精力**）/
      ``sleep_debt()`` 或 snapshot 的 ``sleep_debt`` 键；
    - ``session_store``：``upsert_sleep_session/finish_sleep_session/query_sleep_sessions``
      三方法（ScheduleDB v6 同形；缺省用 :class:`MemorySleepSessionStore`）；
    - ``event_bus``：``publish_soon(event, **payload)``；
    - ``episode_closer``：``(episode_id, actual_end_minute, reason) -> bool``（planner 接）；
    - ``sleep_endpoint_updater``：``(now, actual_end_minute, reason) -> Any``
      （planning.sleep_schedule 共享端点写回）；
    - ``emotion_bridge``：emotion 服务束鸭子口（PR8 3.0.0：``extend_sleep`` 整链已删
      ——UQ-3 裁定，觉醒一律走阈值 + ``lie_in_max_until`` 上限；形参保留为兼容位，
      当前无运行期调用）。
    """

    def __init__(self, *, config_provider: Any = None, life_state: Any = None,
                 session_store: Any = None, event_bus: Any = None,
                 decision_logger: Any = None,
                 timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None,
                 episode_closer: Optional[Callable[..., Any]] = None,
                 sleep_endpoint_updater: Optional[Callable[..., Any]] = None,
                 emotion_bridge: Any = None) -> None:
        self._config_provider = config_provider
        self._life = life_state
        self._store = session_store or MemorySleepSessionStore()
        self._bus = event_bus
        self._decision_logger = decision_logger
        self._tz = timezone_manager or TimezoneManager(timezone_str)
        self._episode_closer = episode_closer
        self._sleep_endpoint_updater = sleep_endpoint_updater
        self._emotion_bridge = emotion_bridge
        self._clock = SleepClock()
        self._session: Optional[SleepSession] = None
        self._episode_id: Optional[str] = None
        self._last_prepared_at: Optional[datetime] = None
        self._last_prepared_key: Optional[str] = None
        self._last_tick_at: Optional[datetime] = None
        self._last_continuous_hours: float = 0.0
        self._last_threshold_check_at: Optional[datetime] = None
        self._prepared: Optional[Dict[str, Any]] = None
        self._warning: Optional[str] = None
        #: 本轮睡眠是否「硬约定 lead 不足」（临时新建 < 提前量）——只诊断，不虚构准时
        self._hard_commitment_lead_short = False
        self._dynamic_view: Dict[str, Any] = {}
        self._phase_key: Optional[datetime] = None
        self._phase_minutes = 0
        self._nap_decision: Dict[str, str] = {}

    # ── 配置 ──────────────────────────────────────────────────────────
    def _cfg_obj(self) -> Any:
        provider = self._config_provider
        if provider is None:
            return None
        try:
            return provider() if callable(provider) else provider
        except Exception:
            return None

    def _section(self) -> Any:
        return getattr(self._cfg_obj(), "life_state", None)

    def _cfg(self, key: str, default: Any) -> Any:
        value = getattr(self._section(), key, None)
        return default if value is None else value

    def _cfg_float(self, key: str, default: float) -> float:
        return _clamp(self._cfg(key, default), -240.0, 240.0, default)

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return int(default)

    def _cfg_bool(self, key: str, default: bool) -> bool:
        value = self._cfg(key, None)
        return bool(default) if value is None else bool(value)

    @property
    def enabled(self) -> bool:
        """``[schedule].enabled`` 派生（PR8 3.0.0 唯一日程总开关）。

        旧 ``[life_state].sleep_engine_enabled`` / ``[life_state].enabled`` 两键已退役
        （依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP1/C-4）：日程开 =
        引擎在位即随 spine 运行态生效；日程关 = 引擎随之停摆（不切回旧路线）。
        """
        return bool(getattr(getattr(self._cfg_obj(), "schedule", None), "enabled", False))

    def diagnostics(self) -> Dict[str, Any]:
        """只读诊断（不触发任何写入；供 /lbm_life rolling 与报告核对）。"""
        session = self._session
        result = {
            "enabled": self.enabled,
            "warning": self._warning,
            "session_id": session.session_id if session else None,
            "asleep": self.is_asleep(),
            "continuous_minutes": session.continuous_minutes if session else 0,
            "interruption_count": session.interruption_count if session else 0,
            "wake_reason": session.wake_reason if session else None,
            "wake_at": self._iso(session.earliest_wake_at) if session else None,
            "lie_in_cap": self._iso(session.lie_in_cap_at) if session else None,
            "hard_commitment_start": (self._iso(self._parse(session.metadata.get(
                "hard_commitment_start"))) if session else None),
            "hard_commitment_lead_short": bool(self._hard_commitment_lead_short),
        }
        if self.dynamic_enabled:
            result["dynamic"] = self.dynamic_status()
        return result

    @property
    def dynamic_enabled(self) -> bool:
        return self.enabled and not self._no_sleep_mode()

    def dynamic_status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not self.dynamic_enabled:
            return {}
        current = self._now(now)
        result = dict(self._dynamic_view)
        if self._nap_decision.get("nap_date") == current.date().isoformat():
            result.update(self._nap_decision)
        session = self._session
        if session is not None:
            if not session.closed:
                return {**result, **(session.metadata.get("dynamic_sleep") or {}), "band": "asleep"}
            if session.closed_at and 0 <= current.timestamp() - session.closed_at.timestamp() < 900:
                result["band"] = "waking"
        return result

    def record_nap(self, date: str, reason: str) -> None:
        self._nap_decision = {"nap_date": date, "nap_reason": reason}

    def _adapt_phase(self, bedtime: datetime, sleep_min: int) -> int:
        if self._phase_key == bedtime:
            return self._phase_minutes
        query = getattr(self._store, "query_sleep_sessions", None)
        rows = query(limit=24) if callable(query) else []
        offsets: Dict[str, int] = {}
        previous: Optional[int] = None
        for row in rows or []:
            session = row if isinstance(row, SleepSession) else SleepSession.from_row(row)
            start = self._aware(self._parse(session.metadata.get("sleep_started_at")), bedtime)
            dynamic = session.metadata.get("dynamic_sleep") or {}
            if (not session.closed or not start or session.wake_reason not in {
                    "energy_threshold", "lie_in_cap", "wake_window_end"}
                    or dynamic.get("onset_reason") == "manual"
                    or session.metadata.get("estimated") or session.metadata.get("restored")
                    or (session.metadata.get("actual_sleep_hours") or 0) < 2):
                continue
            if not 0 <= (bedtime.date() - start.date()).days <= 7 or start >= bedtime:
                continue
            night = str(dynamic.get("bedtime") or start.isoformat())[:10]
            offsets.setdefault(night, (start.hour * 60 + start.minute - sleep_min + 720) % 1440 - 720)
            if previous is None and type(dynamic.get("phase_minutes")) is int:
                previous = dynamic["phase_minutes"]
        self._phase_minutes = phase_shift(list(reversed(list(offsets.values()))), previous or 0)
        self._phase_key = bedtime
        return self._phase_minutes

    def dynamic_window(self, now: datetime):
        return night_window(now, *self._anchors(), self._cfg_int("sleep_drift_max_minutes", 45))

    def next_onset_boundary(self, now: Optional[datetime] = None) -> Optional[datetime]:
        if not self.dynamic_enabled or self.is_asleep(now):
            return None
        current = self._now(now)
        window = self.dynamic_window(current)
        return next((point for point in (window.start, window.latest_start) if point > current), None)

    def goodnight_reference(self, now: Optional[datetime] = None):
        """P2（2026-11-09）本夜只读晚安参考目标 + 来源，供问候侧统一消费。

        返回 ``(datetime, source)`` 或 ``None``（引擎不启用/已睡/无动态窗）。来源区分：
        high_debt_window_start（高债恢复策略采用允许窗口起点）/ engine_winding（引擎
        准备入睡的更早有效目标）/ bedtime（常规本夜采用 bedtime）。**不承诺实际入睡时间**
        （next_onset_boundary 的窗口边界不在此冒充承诺）；已睡时返回 None 交由入睡衔接。
        """
        if not self.enabled or not self.dynamic_enabled:
            return None
        current = self._now(now)
        if self.is_asleep(current):
            return None
        try:
            window = self.dynamic_window(current)
        except Exception:
            return None
        if window is None:
            return None
        debt = self._debt_now()
        debt_trigger = bounded(self._cfg_float("debt_trigger", 6.0), 0, 12, 6.0)
        if debt >= debt_trigger and window.start <= current <= window.latest_start:
            return window.start, "high_debt_window_start"
        band = (self.dynamic_status(current) or {}).get("band")
        if band == "winding_down":
            return window.start, "engine_winding"
        return window.bedtime, "bedtime"

    def actual_wake_today(self, now: datetime) -> Optional[datetime]:
        query = getattr(self._store, "query_sleep_sessions", None)
        if not callable(query):
            return None
        current = self._now(now)
        for row in query(limit=8) or []:
            session = row if isinstance(row, SleepSession) else SleepSession.from_row(row)
            # PR-FIX-TZ：closed_at 存储可能 naive（本地墙钟）→ 规范化后再与 aware 比较
            wake = self._aware(self._parse(session.metadata.get("closed_at")), current)
            if (session.closed and wake and wake.date() == current.date() and wake <= current
                    and session.wake_reason != "offline_reconnect"
                    and (session.metadata.get("actual_sleep_hours") or 0) > 0):
                return wake
        return None

    def evaluate_onset(self, now: datetime, snapshot: Dict[str, Any], *,
                       hard_start: Optional[datetime] = None,
                       interruptibility: float = 1, engagement: float = 0, blocked: bool = False,
                       manual: bool = False) -> Optional[Dict[str, Any]]:
        """Only the planner starts an episode, after the engine has accepted its onset."""
        if not self.dynamic_enabled:
            return None
        # PR-FIX-TZ：now/hard_start 统一 aware（naive 按引擎本地墙钟解释），
        # 保证 window/hard_due 与 now 的全部比较同 tz；判定语义不变。
        now = self._now(now)
        hard_start = self._aware(hard_start, now)
        window = self.dynamic_window(now)
        sleep_min, _ = self._anchors()
        cfg = tuning_section("dynamic_sleep")
        try:
            phase = self._adapt_phase(window.bedtime, sleep_min)
        except Exception:
            logger.debug("睡眠相位历史暂不可读", exc_info=True)
            phase = 0
        day_shift = cfg["weekend_phase_minutes"] if window.wake.weekday() >= 5 else cfg["weekday_phase_minutes"]
        peak = (sleep_min + 180 + bounded(day_shift + cfg["chronotype_offset_minutes"] + phase, -90, 90)) % 1440
        weights = tuple(bounded(cfg[key], 0, 1) for key in (
            "pressure_weight", "circadian_weight", "debt_weight"))
        total = sum(weights)
        weights = tuple(weight / total for weight in weights) if total else (.50, .30, .20)
        lead = self._cfg_float("sleep_hard_commitment_lead_minutes",
                               DEFAULT_HARD_COMMITMENT_LEAD_MINUTES)
        hard_due = hard_commitment_deadline(hard_start, lead)
        bonus = 12 if hard_due and now < hard_due < window.wake else 0
        energy = self._energy_now(snapshot.get("energy"))
        debt = self._debt_now()
        parts = sleep_score(energy, debt, now.hour * 60 + now.minute, peak,
                            bonus, interruptibility, engagement=engagement, weights=weights)
        previous = self._dynamic_view.get("band") if self._dynamic_view.get("bedtime") == window.bedtime.isoformat() else "awake"
        band = onset_band(parts["score"], previous) if window.start - timedelta(minutes=90) <= now <= window.latest_start else "awake"
        # PR-SLEEP-RECOVERY（规格 §1B）：高债提前睡——债 ≥ 阈值且已在允许入睡
        # 窗口内（≥window_start）→ 直接 winding_down（绕过评分阈值）；窗口外不
        # 提前，债 < 阈值评分路径逐字不变。外层 hard_due/blocked/should_start 闸
        # 不受影响（硬约定仍优先）。
        debt_trigger = bounded(cfg.get("debt_trigger", 6.0), 0, 12, 6.0)
        if debt >= debt_trigger and window.start <= now <= window.latest_start:
            band = "winding_down"
        within = window.start <= now <= window.latest_start + timedelta(minutes=5)
        due = hard_due is not None and hard_due <= now + timedelta(minutes=15)
        reason = None
        if (within and now < window.earliest_wake and not due and not blocked
                and self.should_start_sleep_episode(now)):
            if manual:
                reason = "manual"
            elif now >= window.latest_start:
                reason = "anchor_bound"
            elif band == "winding_down" and now <= window.latest_start:
                reason = "score"
        cfg_cap = next_occurrence(window.wake, self._lie_in_cap_minute())
        # 复核 P0-4：高债恢复期 latest_wake 按债补偿顺延（封顶 90min，仍受
        # lie_in_cap 约束）——原 wake+drift 截死使短窗口+晚入睡数学上永无法还债。
        # 顺延经 dynamic_sleep.latest_wake 传入会话 lie_in_cap_at，觉醒判定同口径。
        extended_latest = window.latest_wake + timedelta(
            minutes=debt_wake_extension_minutes(debt))
        latest = min(extended_latest, cfg_cap)
        self._dynamic_view = {
            **parts, "band": band, "bedtime": window.bedtime.isoformat(),
            "window_start": window.start.isoformat(), "window_end": window.latest_start.isoformat(),
            "earliest_wake": window.earliest_wake.isoformat(), "latest_wake": latest.isoformat(),
            "wake_anchor": window.wake.isoformat(), "phase_minutes": phase,
            "peak_minute": peak, "onset_reason": reason,
            "window_sets_cap": extended_latest <= cfg_cap,
            "decision": reason or ("hard_commitment" if due else "busy" if blocked else "waiting"),
        }
        return dict(self._dynamic_view) if reason else None

    # ── 时间 / 序列化助手 ─────────────────────────────────────────────
    def _now(self, now: Optional[datetime] = None) -> datetime:
        dt = now or self._tz.get_now()
        if dt.tzinfo is None:
            tz = getattr(self._tz, "_tz", None)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt

    def _aware(self, dt: Optional[datetime],
               ref: Optional[datetime] = None) -> Optional[datetime]:
        """时区一致性规范化（PR-FIX-TZ 3.17.3）：naive → 附参照时区，aware/None 原样。

        存储/恢复出来的 naive 时间戳一律按引擎本地墙钟时间解释（与 ``_restore_clock``
        的 stamp 同口径）；只消除 naive/aware 混比，不改变任何时刻的墙钟语义。
        ``ref`` 缺省取当前引擎时间；参照本身 naive（时区解析失败的降级态）则原样返回。
        """
        if dt is None or dt.tzinfo is not None:
            return dt
        tz = (ref if ref is not None else self._now()).tzinfo
        return dt.replace(tzinfo=tz) if tz is not None else dt

    @staticmethod
    def _iso(dt: Optional[datetime]) -> Optional[str]:
        return dt.isoformat() if dt is not None else None

    @staticmethod
    def _parse(dt: Any) -> Optional[datetime]:
        if isinstance(dt, datetime):
            return dt
        if isinstance(dt, str) and dt.strip():
            try:
                return datetime.fromisoformat(dt.strip())
            except ValueError:
                return None
        return None

    @staticmethod
    def _frame_minutes(now: datetime, frame_date: str) -> int:
        """``now`` 相对日程日帧 0 点的分钟（跨午夜 >1440，与 schedule_items 同口径）。"""
        try:
            midnight = datetime.strptime(frame_date, "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            return now.hour * 60 + now.minute
        return int((now - midnight).total_seconds() // 60)

    @staticmethod
    def _frame_date_at(moment: datetime) -> str:
        """会话所属日程日 = 入睡时刻的日历日（夜间条目属于入睡日）。"""
        return moment.strftime("%Y-%m-%d")

    def _anchors(self) -> Tuple[int, int]:
        """``(sleep_min, wake_min)``：``[schedule]`` 配置（缺省 23:00/07:00）。"""
        schedule = getattr(self._cfg_obj(), "schedule", None)
        from core.time_utils import parse_hhmm
        sleep_min = parse_hhmm(str(getattr(schedule, "sleep_time", "") or ""), 23 * 60)
        wake_min = parse_hhmm(str(getattr(schedule, "wake_time", "") or ""), 7 * 60)
        return sleep_min, wake_min

    def _lie_in_cap_minute(self) -> int:
        advanced = getattr(self._cfg_obj(), "schedule_advanced", None)
        from core.time_utils import parse_hhmm
        raw = str(getattr(advanced, "lie_in_max_until", "") or DEFAULT_LIE_IN_MAX_UNTIL)
        minute = parse_hhmm(raw, -1)
        if minute < 0:
            minute = parse_hhmm(DEFAULT_LIE_IN_MAX_UNTIL, 12 * 60)
        return minute

    def _interrupt_threshold(self) -> float:
        base = max(0.0, self._cfg_float("sleep_interrupt_min_minutes",
                                       DEFAULT_INTERRUPT_MIN_MINUTES))
        if self._session and self._session.metadata.get("dynamic_sleep"):
            sensitivity = bounded(tuning_section("dynamic_sleep")["awakening_sensitivity"], 0, 1, .30)
            return base * (1.3 - sensitivity)
        return base

    def _wake_check_seconds(self) -> int:
        # H 类（3.1.0 配置精简轮）：[life_state].sleep_wake_check_seconds 退役为
        # 内部节拍常量（生产现值=默认 300 已证实；到点/硬约定明确豁免此复检粒度）。
        return DEFAULT_WAKE_CHECK_SECONDS

    def _wake_threshold(self) -> float:
        return _clamp(self._cfg_float("wake_energy_threshold", DEFAULT_WAKE_ENERGY_THRESHOLD),
                      0.0, 1.0, DEFAULT_WAKE_ENERGY_THRESHOLD)

    def _oversleep_start(self) -> float:
        return _clamp(self._cfg_float("sleep_oversleep_start_hours",
                                      DEFAULT_OVERSLEEP_START_HOURS),
                      8.0, 12.0, DEFAULT_OVERSLEEP_START_HOURS)

    def _penalize_focus(self) -> bool:
        return self._cfg_bool("sleep_oversleep_penalize_focus",
                              DEFAULT_OVERSLEEP_PENALIZE_FOCUS)

    def _no_sleep_mode(self) -> bool:
        """``[schedule].no_sleep_mode``：引擎觉醒判定整链禁用（旧语义保留）。"""
        schedule = getattr(self._cfg_obj(), "schedule", None)
        return bool(getattr(schedule, "no_sleep_mode", False))

    # ── 事件 / 日志（best-effort）─────────────────────────────────────
    def _publish(self, event: str, **payload: Any) -> None:
        if self._bus is None:
            return
        try:
            data = dict(payload)
            data.setdefault("source", "sleep_engine")
            self._bus.publish_soon(event, **data)
        except Exception:
            logger.debug("睡眠引擎事件发布失败（静默）：%s", event, exc_info=True)

    def _log(self, action: str, **fields: Any) -> None:
        if self._decision_logger is None:
            return
        try:
            self._decision_logger.record(action=action, trigger_type="sleep_engine", **fields)
        except Exception:
            pass

    def _emit_once(self, session: SleepSession, key: str, event: str,
                   **payload: Any) -> bool:
        """事件一次性保证：``metadata.emitted`` 持久集合 + 内存会话双门。"""
        emitted = session.metadata.setdefault("emitted", [])
        if key in emitted:
            return False
        emitted.append(key)
        self._publish(event, session_id=session.session_id, **payload)
        return True

    # ── 外部读数（emotion wake provider / 面板）─────────────────────
    def is_asleep(self, now: Optional[datetime] = None) -> Optional[bool]:
        """``True`` 睡眠中 / ``False`` 已醒（同一夜间周期内）/ ``None`` = 引擎不表态。

        ``None`` 的三种情形：引擎未启用、本夜没有会话（还没睡着）、会话已过上限
        （让旧锚点窗口自行收尾）。消费方（emotion）据此**回退旧锚点窗口**。
        """
        if not self.enabled:
            return None
        session = self._session
        if session is None:
            return False if self.dynamic_enabled else None
        if self._no_sleep_mode():
            # no_sleep_mode：不启用引擎觉醒判定 → provider「不可用」，旧窗口语义保留
            return None
        current = self._now(now)
        if not session.closed:
            return True
        if self.dynamic_enabled:
            return False
        cap = self._aware(session.lie_in_cap_at, current)
        if isinstance(cap, datetime) and current < cap:
            return False
        return None

    def sleep_status(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """给 emotion/面板的只读状态（引擎不表态 → ``None``）。"""
        asleep = self.is_asleep(now)
        if asleep is None:
            return None
        session = self._session
        if session is None:
            return {"asleep": False}
        return {
            "session_id": session.session_id,
            "asleep": bool(asleep),
            "wake_reason": session.wake_reason,
            "continuous_minutes": int(session.continuous_minutes),
            "interruption_count": int(session.interruption_count),
            "wake_at": self._iso(session.earliest_wake_at),
            "lie_in_cap": self._iso(session.lie_in_cap_at),
            "closed_at": self._iso(session.closed_at),
        }

    def _resolve_hard_commitment(self, provided: Any, session: Any) -> Optional[datetime]:
        """硬约定来源合并（2026-10-27 用户批准）。

        ``None`` = 未提供新数据（沿用缓存）；:data:`NO_HARD_COMMITMENT` = 显式无有效
        约定（清缓存、不幽灵唤醒）；``datetime`` = 有效约定（采用并缓存）。
        """
        if provided is NO_HARD_COMMITMENT:
            if session is not None:
                session.metadata.pop("hard_commitment_start", None)
            return None
        if isinstance(provided, datetime):
            return provided
        cached = session.metadata.get("hard_commitment_start") if session is not None else None
        return self._parse(cached)

    def next_wake_boundary(self, now: Optional[datetime] = None, *,
                           hard_commitment_start: Any = None
                           ) -> Optional[datetime]:
        """下一个「必须重检」的绝对时刻（tick loop 的安全等待上限；只读不判醒）。

        取值集合的最小未来值：``lie_in_cap``（12:00 必起）/ 硬约定阈值（约定 − lead）/
        最早起床（还没到时，到点必须立刻重检）/ 阈值复检网格（``sleep_wake_check_seconds``
        对齐，避免 5 分钟固定网格漂过阈值）。

        - 引擎未启用/未在睡/``no_sleep_mode`` → ``None``（loop 保持原固定间隔）；
        - 所有候选都在过去（回拨/长间隔/刚过点）→ ``None``（调用方按"立即重检"处理）；
        - 纯内存读，不推进连续段、不写盘、不发事件。
        """
        if not self.enabled or self._no_sleep_mode():
            return None
        session = self._session
        if session is None or session.closed:
            return None
        current = self._now(now)
        earliest = self._aware(session.earliest_wake_at, current) or next_occurrence(
            current, self._anchors()[1])
        candidates: List[datetime] = []
        cap = self._aware(session.lie_in_cap_at, current) or next_occurrence(
            earliest, self._lie_in_cap_minute())
        candidates.append(cap)
        if cap <= current:
            return current  # 上限已过（回拨/长间隔）→ 调用方立即重检
        hard_due = self._aware(hard_commitment_deadline(
            self._resolve_hard_commitment(hard_commitment_start, session),
            self._cfg_float("sleep_hard_commitment_lead_minutes",
                            DEFAULT_HARD_COMMITMENT_LEAD_MINUTES)), current)
        if hard_due is not None:
            if hard_due <= current:
                return current  # 硬约定阈值已过（睡着跨过/临时新建）→ 立即正式起床
            candidates.append(hard_due)
        if current < earliest:
            candidates.append(earliest)
        else:
            last = self._last_threshold_check_at
            candidates.append(last + timedelta(seconds=self._wake_check_seconds())
                              if last is not None else current)
        future = [moment for moment in candidates if moment is not None and moment > current]
        return min(future) if future else None

    def should_start_sleep_episode(self, now: Optional[datetime] = None) -> Optional[bool]:
        """planner 开 sleep episode 前的闸（``None`` = 引擎不表态 → 旧路径）。

        - 引擎未启用 / ``no_sleep_mode`` → ``None``（旧行为，planner 自行决定）；
        - 本夜已在睡 → ``False``（勿重复开）；
        - 本夜已醒（尤其**硬约定早于 wake 锚点**醒来）且仍处原睡眠环内 → ``False``
          （直到 wake 锚点后 1h），避免每拍重开 episode/新会话、反复结算睡眠债或
          重复发布醒沿；同日重启也经 DB 最近会话行命中同一规则。
        - 否则 ``True``（可以开）。
        """
        if not self.enabled or self._no_sleep_mode():
            return None
        current = self._now(now)
        session = self._session
        if session is not None:
            if not session.closed:
                return False
            if self._suppress_until(session.earliest_wake_at,
                                    session.actual_end_minute,
                                    session.schedule_date, ref=current) > current:
                return False
        if self._recently_woke(current):
            return False
        return True

    def _suppress_until(self, earliest_wake: Optional[datetime],
                        actual_end_minute: Optional[int],
                        schedule_date: Optional[str], *,
                        ref: Optional[datetime] = None) -> datetime:
        """醒来后「本周期不再新开睡眠」的抑制截止时刻（wake 锚点 +1h）。

        PR-FIX-TZ（3.17.3）：返回值一律 aware（naive 输入按引擎本地墙钟解释），
        消除跨日已闭合会话与 aware ``current`` 混比抛 TypeError；抑制窗语义不变。
        """
        current = self._now(ref)
        if isinstance(earliest_wake, datetime):
            return self._aware(earliest_wake, current) + timedelta(hours=1)
        if actual_end_minute is not None and schedule_date:
            try:
                wake_dt = datetime.strptime(schedule_date, "%Y-%m-%d").replace(
                    tzinfo=current.tzinfo) + timedelta(minutes=int(actual_end_minute))
                return wake_dt + timedelta(hours=1)
            except (TypeError, ValueError):
                pass
        return datetime.min.replace(tzinfo=current.tzinfo)

    def _recently_woke(self, current: datetime) -> bool:
        """DB 最近会话行：本夜已醒且仍在抑制窗 → True（重启/热重载同口径）。"""
        query = getattr(self._store, "query_sleep_sessions", None)
        if not callable(query):
            return False
        try:
            rows = query(limit=5) or []
        except Exception:
            return False
        for row in rows:
            session = row if isinstance(row, SleepSession) else (
                SleepSession.from_row(row) if isinstance(row, dict) else None)
            if session is None or not session.closed:
                continue
            if not session.schedule_date:
                continue
            if session.schedule_date not in (current.strftime("%Y-%m-%d"),
                                             (current - timedelta(days=1)).strftime("%Y-%m-%d")):
                continue
            earliest = session.earliest_wake_at
            if earliest is None:
                earliest = self._parse(session.metadata.get("earliest_wake_at"))
            if self._suppress_until(earliest, session.actual_end_minute,
                                    session.schedule_date, ref=current) > current:
                return True
        return False

    # ── 会话生命周期 ─────────────────────────────────────────────────
    def _snapshot(self) -> Dict[str, Any]:
        getter = getattr(self._life, "snapshot", None)
        if callable(getter):
            try:
                snap = getter()
                return snap if isinstance(snap, dict) else {}
            except Exception:
                return {}
        return {}

    def _energy_now(self, energy: Any = None) -> Optional[float]:
        if isinstance(energy, (int, float)) and not isinstance(energy, bool):
            return _clamp(energy, 0.0, 1.0, 0.0)
        value = self._snapshot().get("energy")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _clamp(value, 0.0, 1.0, 0.0)
        return None

    def _debt_now(self) -> float:
        value = self._snapshot().get("sleep_debt")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _clamp(value, 0.0, SLEEP_DEBT_MAX, 0.0)
        getter = getattr(self._life, "sleep_debt", None)
        if callable(getter):
            try:
                return _clamp(getter(), 0.0, SLEEP_DEBT_MAX, 0.0)
            except Exception:
                return 0.0
        return 0.0

    def _start_session(self, now: datetime, episode: Optional[Dict[str, Any]],
                       energy: Any = None) -> SleepSession:
        sleep_min, wake_min = self._anchors()
        frame_date = self._frame_date_at(now)
        earliest_wake = next_occurrence(now, wake_min)
        cap = next_occurrence(earliest_wake, self._lie_in_cap_minute())
        dynamic = ((episode or {}).get("metadata") or {}).get("dynamic_sleep")
        if isinstance(dynamic, dict):
            early = self._aware(self._parse(dynamic.get("earliest_wake")), now)
            late = self._aware(self._parse(dynamic.get("latest_wake")), now)
            if early and late and now < early <= late:
                earliest_wake, cap = early, late
            else:
                dynamic = None
        planned_start = self._frame_minutes(now, frame_date)
        span_minutes = int((earliest_wake - now).total_seconds() // 60)
        try:
            planned_end = int((episode or {}).get("planned_end_minute")
                              or planned_start + span_minutes)
        except (TypeError, ValueError):
            planned_end = planned_start + span_minutes
        if planned_end <= planned_start:
            while planned_end <= planned_start:
                planned_end += DAY_MINUTES
        debt_before = self._debt_now()
        session = SleepSession(
            session_id=f"sleep:{frame_date}:{planned_start}",
            schedule_date=frame_date,
            planned_start_minute=planned_start,
            planned_end_minute=planned_end,
            created_at=now.isoformat(),
            actual_start_minute=planned_start,
            energy_start=self._energy_now(energy),
            sleep_debt_before=debt_before,
            metadata={"sleep_min": sleep_min, "wake_min": wake_min,
                      "sleep_ring": f"{sleep_min}-{wake_min}", "emitted": [],
                      "episode_id": str((episode or {}).get("episode_id") or "")},
            earliest_wake_at=earliest_wake,
            lie_in_cap_at=cap,
        )
        # PR-SLEEP-RECOVERY（规格 §1A）：会话启动时一次性计算夜间恢复目标并持久化
        session.recovery_target_hours = sleep_recovery_target(
            debt_before, max_bonus=tuning_section("dynamic_sleep").get(
                "target_max_bonus", 1.0))
        session.metadata["sleep_started_at"] = now.isoformat()
        if dynamic:
            session.metadata["dynamic_sleep"] = dict(dynamic)
        session.sync_runtime_fields()
        self._session = session
        self._episode_id = str((episode or {}).get("episode_id") or "") or None
        self._clock_metadata_writable = True
        self._clock.start(now)
        self._last_tick_at = now
        self._last_continuous_hours = 0.0
        self._last_threshold_check_at = None
        self._hard_commitment_lead_short = False
        self._save_session()
        self._emit_once(session, "started", EVENT_SLEEP_STARTED,
                        planned_start_minute=planned_start,
                        planned_end_minute=planned_end,
                        schedule_date=frame_date)
        self._log("sleep.session_started", session_id=session.session_id,
                  schedule_date=frame_date, planned_start=planned_start,
                  planned_end=planned_end)
        return session

    def _save_session(self) -> None:
        session = self._session
        if session is None:
            return
        session.updated_at = self._now().isoformat()
        if not session.closed and getattr(self, "_clock_metadata_writable", True):
            record = self._clock.record
            session.metadata["sleep_segment_started_at"] = self._iso(record.segment_started_at)
            session.metadata["sleep_clock"] = {
                "version": 1, "segment_started_at": self._iso(record.segment_started_at),
                "interrupted": bool(record.interrupted),
                "interrupt_started_at": self._iso(record.interrupt_started_at),
                "last_note_at": self._iso(record.last_note_at),
                "interrupted_minutes": float(record.interrupted_minutes),
                "interruption_count": int(record.interruption_count),
            }
        session.sync_runtime_fields()
        upsert = getattr(self._store, "upsert_sleep_session", None)
        if callable(upsert):
            try:
                upsert(session)
            except Exception as exc:
                logger.warning("sleep_sessions 写入失败（忽略，内存状态继续）：%s", exc)
        writer = getattr(self._life, "apply_sleep_fields", None)
        if callable(writer):
            try:
                writer({
                    # A closed session must never re-arm LifeState during persist/unload.
                    "sleep_started_at": None if session.closed else session.metadata.get("sleep_started_at"),
                    "continuous_sleep_minutes": 0 if session.closed else int(session.continuous_minutes),
                    "sleep_segment_started_at": None if session.closed else self._iso(self._clock.started_at),
                    "sleep_interrupted": False if session.closed else bool(self._clock.record.interrupted),
                    "last_wake_reason": session.wake_reason,
                    "last_sleep_at": session.metadata.get("sleep_started_at"),
                    "sleep_wake_target": None if session.closed else self._iso(session.earliest_wake_at),
                    "sleep_lie_in_cap": None if session.closed else self._iso(session.lie_in_cap_at),
                })
            except Exception:
                pass

    def restore(self, now: Optional[datetime] = None) -> Optional[SleepSession]:
        """重启恢复：优先 DB 未结束会话行，其次 LifeState 睡眠字段；失败静默。"""
        if not self.enabled:
            return None
        current = self._now(now)
        session: Optional[SleepSession] = None
        opener = getattr(self._store, "open_sleep_session", None)
        if callable(opener):
            try:
                session = opener()
                if isinstance(session, dict):
                    session = SleepSession.from_row(session)
                elif session is not None and not isinstance(session, SleepSession):
                    session = None
            except Exception:
                session = None
        if session is None:
            query = getattr(self._store, "query_sleep_sessions", None)
            if callable(query):
                try:
                    for row in query() or []:
                        candidate = row if isinstance(row, SleepSession) else (
                            SleepSession.from_row(row) if isinstance(row, dict) else None)
                        if candidate is not None and not candidate.closed:
                            session = candidate
                            break
                except Exception:
                    session = None
        if session is None:
            session = self._restore_recent_closed(current)
        if session is None:
            return self._restore_from_life_state(current)
        self._reconcile_recorded_wake(session, current)
        if session.earliest_wake_at is None:
            session.earliest_wake_at = next_occurrence(
                current, self._anchors()[1]) if session.actual_start_minute is None else None
        if session.lie_in_cap_at is None:
            base = session.earliest_wake_at or current
            session.lie_in_cap_at = next_occurrence(base, self._lie_in_cap_minute())
        self._session = session
        self._restore_clock(session, current)
        self._episode_id = str(session.metadata.get("episode_id") or "") or None
        self._last_tick_at = current
        # Cold-load gap is owned by the existing offline handoff. Do not integrate
        # the already elapsed segment again in the next ordinary tick.
        self._last_continuous_hours = self._clock.continuous_minutes(current) / 60.0
        self._prepared = None
        return session

    def _restore_clock(self, session: SleepSession, current: datetime) -> None:
        """Restore the segment and interruption record, rather than restart its clock."""
        self._clock = SleepClock()
        payload = session.metadata.get("sleep_clock")
        known = isinstance(payload, dict) and type(payload.get("version")) is int and payload["version"] == 1
        self._clock_metadata_writable = not isinstance(payload, dict) or known

        def stamp(raw: Any) -> Optional[datetime]:
            value = self._parse(raw)
            if value is None:
                return None
            if value.tzinfo is None and current.tzinfo is not None:
                value = value.replace(tzinfo=current.tzinfo)
            elif value.tzinfo is not None and current.tzinfo is None:
                value = value.astimezone().replace(tzinfo=None)
            return value if value <= current else None

        started = stamp(session.metadata.get("sleep_started_at"))
        segment = stamp(payload.get("segment_started_at")) if known else None
        segment = segment or stamp(session.metadata.get("sleep_segment_started_at"))
        if segment is None:
            fields = self._snapshot()
            # LifeState may contain another cycle; only its matching segment is evidence.
            if stamp(fields.get("sleep_started_at")) == started and started is not None:
                segment = stamp(fields.get("sleep_segment_started_at"))
        if segment is None:
            saved = stamp(session.updated_at) or stamp(session.created_at)
            if saved is not None:
                segment = saved - timedelta(minutes=max(0, session.continuous_minutes))
        if started is not None and segment is not None and segment < started:
            segment = started
        self._clock.record.segment_started_at = segment or current
        self._clock.record.interruption_count = max(0, session.interruption_count)
        if known:
            record = self._clock.record
            record.interrupted_minutes = _clamp(payload.get("interrupted_minutes"), 0.0, 60.0*240.0, 0.0)
            record.interruption_count = max(record.interruption_count,
                int(_clamp(payload.get("interruption_count"), 0.0, 100000.0, 0.0)))
            record.interrupt_started_at = stamp(payload.get("interrupt_started_at"))
            record.last_note_at = stamp(payload.get("last_note_at"))
            record.interrupted = bool(payload.get("interrupted")) and record.interrupt_started_at is not None

    def _restore_recent_closed(self, current: datetime) -> Optional[SleepSession]:
        """Retain the already-awake provider until this sleep cycle's cap after reload."""
        query = getattr(self._store, "query_sleep_sessions", None)
        if not callable(query):
            return None
        candidates: List[SleepSession] = []
        try:
            for offset in (0, 1):
                date = (current-timedelta(days=offset)).strftime("%Y-%m-%d")
                for row in query(schedule_date=date, limit=50) or []:
                    candidate = row if isinstance(row, SleepSession) else SleepSession.from_row(row)
                    if candidate.closed and candidate.lie_in_cap_at is not None \
                            and current < self._aware(candidate.lie_in_cap_at, current):
                        candidates.append(candidate)
        except Exception:
            return None
        return max(candidates, key=lambda candidate: (candidate.schedule_date,
                   candidate.actual_start_minute or 0)) if candidates else None

    def _reconcile_recorded_wake(self, session: SleepSession, current: datetime) -> None:
        """Repair an old resurrected row from the durable wake marker, without settlement.

        No debt/energy recalculation, wake event or endpoint write. A completed sleep
        episode may supply the original reason; otherwise record the recovery honestly.
        """
        if session.closed:
            return
        fields = self._snapshot()
        started = self._aware(self._parse(session.metadata.get("sleep_started_at")), current)
        settled = self._aware(self._parse(fields.get("last_sleep_at")), current)
        try:
            if started is None or settled is None or not started <= settled <= current:
                return
        except TypeError:
            return
        end = self._frame_minutes(settled, session.schedule_date)
        reason = str(fields.get("last_wake_reason") or "recovered_settlement")
        episodes = getattr(self._store, "episodes_today", None)
        if callable(episodes):
            try:
                for row in episodes(session.schedule_date) or []:
                    if row.get("goal_type") == "sleep" and row.get("status") == "completed" \
                            and row.get("actual_start_minute") == session.actual_start_minute \
                            and row.get("actual_end_minute") == end:
                        metadata = row.get("metadata") or {}
                        candidate = str(metadata.get("close_reason") or "").removeprefix("sleep_engine_")
                        if candidate in WAKE_REASONS:
                            reason = candidate
                        break
            except Exception:
                pass
        session.actual_end_minute = end
        session.closed_at = settled
        session.wake_reason = reason
        session.sleep_debt_after = self._debt_now()
        session.metadata["settled"] = True
        session.metadata["restored_from_settlement_marker"] = settled.isoformat()
        emitted = session.metadata.setdefault("emitted", [])
        if "woke" not in emitted:
            emitted.append("woke")
        self._session = session
        self._save_session()
        self._log("sleep.session_restore_reconciled", session_id=session.session_id,
                  recorded_wake=settled.isoformat(), wake_reason=reason,
                  no_new_settlement=True)

    def _restore_from_life_state(self, current: datetime) -> Optional[SleepSession]:
        fields = self._snapshot()
        continuous = fields.get("continuous_sleep_minutes")
        started_raw = fields.get("sleep_started_at")
        started = self._parse(started_raw)
        if started is None:
            return None
        # Old persist() could repopulate active fields after a completed wake. A
        # durable last_sleep_at at/after this start forbids snapshot resurrection.
        settled = self._parse(fields.get("last_sleep_at"))
        try:
            if settled is not None and started <= settled <= current:
                writer = getattr(self._life, "apply_sleep_fields", None)
                if callable(writer):
                    writer({"sleep_started_at": None, "continuous_sleep_minutes": 0,
                            "sleep_segment_started_at": None, "sleep_interrupted": False,
                            "sleep_wake_target": None, "sleep_lie_in_cap": None})
                return None
        except (TypeError, ValueError):
            pass
        target = self._parse(fields.get("sleep_wake_target"))
        try:
            continuous_value = int(continuous or 0)
        except (TypeError, ValueError):
            continuous_value = 0
        # 已过起床目标且连续段为 0 → 视为上一夜已结算（不虚构仍睡）
        if continuous_value <= 0 and (target is None or target <= current):
            return None
        sleep_min, wake_min = self._anchors()
        frame_date = self._frame_date_at(started)
        earliest_wake = next_occurrence(started, wake_min)
        cap = next_occurrence(earliest_wake, self._lie_in_cap_minute())
        session = SleepSession(
            session_id=f"sleep:{frame_date}:{self._frame_minutes(started, frame_date)}",
            schedule_date=frame_date,
            planned_start_minute=self._frame_minutes(started, frame_date),
            planned_end_minute=self._frame_minutes(earliest_wake, frame_date),
            created_at=started.isoformat(),
            actual_start_minute=self._frame_minutes(started, frame_date),
            energy_start=None,
            sleep_debt_before=self._debt_now(),
            metadata={"sleep_min": sleep_min, "wake_min": wake_min,
                      "sleep_ring": f"{sleep_min}-{wake_min}", "emitted": [],
                      "sleep_started_at": started.isoformat(), "restored": True},
            earliest_wake_at=earliest_wake if earliest_wake > current else None,
            lie_in_cap_at=cap,
        )
        session.metadata["sleep_segment_started_at"] = self._iso(
            self._parse(fields.get("sleep_segment_started_at")) or started)
        session.continuous_minutes = int(continuous_value)
        # PR-SLEEP-RECOVERY：LifeState 兜底恢复的会话同样带恢复目标（未结算，
        # 当前债即入睡债口径）；DB 行恢复走 from_row 保留原目标，不在此重置。
        session.recovery_target_hours = sleep_recovery_target(
            session.sleep_debt_before,
            max_bonus=tuning_section("dynamic_sleep").get("target_max_bonus", 1.0))
        session.sync_runtime_fields()
        self._session = session
        self._clock = SleepClock()
        self._clock.record.segment_started_at = (
            self._parse(fields.get("sleep_segment_started_at")) or started)
        # 评估 P4 修复（2026-11-04）：删除对 InterruptionRecord 不存在字段
        # continuous_minutes 的死赋值（无读取方；连续段由 segment_started_at 推算）。
        self._last_tick_at = current
        self._log("sleep.session_restored", session_id=session.session_id,
                  schedule_date=frame_date, continuous_minutes=continuous_value)
        return session
    # ── 每拍：prepare → life.tick(sleep_rates) → commit ────────────────
    def prepare_tick(self, now: Optional[datetime] = None, *,
                     sleep_episode: Optional[Dict[str, Any]] = None,
                     energy: Any = None,
                     hard_commitment_start: Any = None,
                     no_sleep_mode: bool = False) -> Dict[str, Any]:
        """拍前准备：推进连续段、算本拍 ``sleep_rates``、做觉醒判定（纯内存）。

        必须先 ``prepare_tick`` 再 ``LifeState.tick(sleep_rates=...)`` 再
        ``commit_tick``（定稿 §3.1 步 2/3/4）。同一 ``now`` 重复 prepare 返回同一份
        结果（``replayed=True``），不会二次推进连续段或二次积分。
        """
        current = self._now(now)
        result: Dict[str, Any] = {
            "active": False, "session_id": None, "asleep": False, "rates": None,
            "continuous_hours": 0.0, "continuous_minutes": 0,
            "quality": 1.0, "interrupted_minutes": 0.0, "interruption_count": 0,
            "reset": False, "wake": WakeDecision(False).to_dict(),
            "no_sleep_mode": bool(no_sleep_mode), "replayed": False,
            "reason": "",
        }
        if not self.enabled:
            result["reason"] = "disabled"
            return result
        if self._session is not None and self._session.closed:
            goal_type = str((sleep_episode or {}).get("goal_type") or "").strip().lower()
            if sleep_episode is not None and goal_type == "sleep":
                # 上一夜已结算：允许进入新一夜（planner 的 should_start 闸负责防同环重开）
                self._session = None
                self._prepared = None
            else:
                result.update(active=True, asleep=False,
                              session_id=self._session.session_id, reason="closed")
                return result
        if self._session is None:
            goal_type = str((sleep_episode or {}).get("goal_type") or "").strip().lower()
            if sleep_episode is not None and goal_type == "sleep":
                self._start_session(current, sleep_episode, energy)
            else:
                result["reason"] = "no_session"
                return result
        session = self._session
        assert session is not None
        if session.closed:
            result.update(active=True, asleep=False, session_id=session.session_id,
                          reason="closed")
            return result
        key = current.isoformat()
        if self._prepared is not None and self._last_prepared_key == key:
            replayed = dict(self._prepared)
            replayed["replayed"] = True
            return replayed
        # dt：距上一拍（钳 [0, 24h]；时钟回拨/重复时刻 → 0，不积分不判醒）
        dt_min = 0.0
        if self._last_tick_at is not None and current > self._last_tick_at:
            dt_min = min((current - self._last_tick_at).total_seconds() / 60.0,
                         float(DAY_MINUTES))
        advance = self._clock.advance(current, self._interrupt_threshold())
        self._last_tick_at = current
        h_after = advance.continuous_hours_after
        if advance.reset or dt_min <= 0:
            h_before = h_after if dt_min <= 0 else 0.0
        else:
            h_before = min(float(self._last_continuous_hours or 0.0), h_after)
        self._last_continuous_hours = h_after
        started_at = self._aware(self._parse(session.metadata.get("sleep_started_at")), current)
        total_session_minutes = 0.0
        if started_at is not None and current > started_at:
            total_session_minutes = (current - started_at).total_seconds() / 60.0
        quality = sleep_quality(advance.interrupted_minutes, total_session_minutes)
        # PR-SLEEP-RECOVERY（规格 §1A/C）：有效睡眠累计 = 会话总时长 − 打断扣除
        # （既有打断口径不变），供最小有效睡眠保护判定；随 _save_session 落盘。
        session.effective_sleep_hours = max(
            0.0, total_session_minutes - advance.interrupted_minutes) / 60.0
        rates: Optional[Dict[str, float]] = None
        if not no_sleep_mode:
            gains = integrate_sleep_gains(
                h_before, h_after, oversleep_start_hours=self._oversleep_start(),
                penalize_focus=self._penalize_focus(), quality=quality)
            span_minutes = max(0.0, h_after - h_before) * 60.0
            if span_minutes > 0:
                energy_rate = gains["energy"] / span_minutes
                focus_rate = gains["focus"] / span_minutes
                hunger_rate = SLEEP_HUNGER_PER_MIN
                stress_rate = SLEEP_STRESS_PER_MIN
            else:
                # 边界拍（会话首拍/确认清醒重置/时钟回拨 dt=0）：引擎连续段未推进，
                # 速率全 0（评估 P4 修复，2026-11-04）——旧实现取最低档曲线速率，
                # life.tick 按自身全 dt（含清醒部分）积分造成微量过施加。
                energy_rate = focus_rate = 0.0
                hunger_rate = stress_rate = 0.0
            rates = {
                "elapsed_sleep_minutes": max(0.0, min(dt_min, (h_after - h_before) * 60.0)),
                "energy_rate": max(-0.02, min(0.02, energy_rate)),
                "focus_rate": max(-0.02, min(0.02, focus_rate)),
                "hunger_rate": hunger_rate,
                "stress_rate": stress_rate,
                "social_battery_rate": 0.0,
            }
        # 阈值复检节流（到点/硬约定不受影响）
        earliest = self._aware(session.earliest_wake_at, current) or next_occurrence(
            current, self._anchors()[1])
        check_seconds = self._wake_check_seconds()
        check_due = True
        if current >= earliest:
            last = self._last_threshold_check_at
            if last is not None and (current - last).total_seconds() < check_seconds:
                check_due = False
            else:
                self._last_threshold_check_at = current
        effective_hard = self._aware(
            self._resolve_hard_commitment(hard_commitment_start, session), current)
        dynamic = session.metadata.get("dynamic_sleep") or {}
        threshold = self._wake_threshold()
        if dynamic:
            rhythm = circadian_sleepiness(current.hour * 60 + current.minute, dynamic["peak_minute"])
            threshold = bounded(threshold + .06 * (rhythm / 50 - 1), 0, 1)
        decision = wake_decision(
            current, energy=self._energy_now(energy), earliest_wake=earliest,
            lie_in_cap=self._aware(session.lie_in_cap_at, current) or
            next_occurrence(earliest, self._lie_in_cap_minute()),
            hard_commitment_start=effective_hard,
            hard_commitment_lead_minutes=self._cfg_float(
                "sleep_hard_commitment_lead_minutes",
                DEFAULT_HARD_COMMITMENT_LEAD_MINUTES),
            wake_energy_threshold=threshold,
            check_due=check_due, no_sleep_mode=no_sleep_mode,
            # PR-SLEEP-RECOVERY（规格 §1C）：最小有效睡眠保护——阈值醒须累计
            # 有效睡眠达本会话恢复目标；lie_in_cap/硬约定分支不受影响。
            effective_sleep_hours=session.effective_sleep_hours,
            min_effective_hours=session.recovery_target_hours)
        if dynamic and decision.reason == "lie_in_cap" and dynamic.get("window_sets_cap"):
            decision = WakeDecision(True, "wake_window_end", decision.deadline)
        # 硬约定 lead 诊断（临时新建 < 提前量 → 立即醒 + 可诊断，**不**倒填准时）
        lead_minutes = self._cfg_float("sleep_hard_commitment_lead_minutes",
                                       DEFAULT_HARD_COMMITMENT_LEAD_MINUTES)
        lead_short = bool(effective_hard is not None
                          and (effective_hard - current).total_seconds() / 60.0 < lead_minutes)
        self._hard_commitment_lead_short = lead_short
        # 只有「本次确实提供了有效约定」才写缓存；显式无约定已在 _resolve 里清除
        if isinstance(hard_commitment_start, datetime):
            session.metadata["hard_commitment_start"] = hard_commitment_start.isoformat()
        session.continuous_minutes = int(round(h_after * 60))
        session.interruption_count = int(advance.interruption_count)
        session.quality = round(quality, 4)
        result.update({
            "active": True, "session_id": session.session_id, "asleep": True,
            "rates": rates, "continuous_hours": round(h_after, 4),
            "continuous_minutes": session.continuous_minutes,
            "quality": quality, "interrupted_minutes": advance.interrupted_minutes,
            "interruption_count": advance.interruption_count,
            "reset": bool(advance.reset), "wake": decision.to_dict(),
            "no_sleep_mode": bool(no_sleep_mode),
            "reason": "prepared" if not no_sleep_mode else "no_sleep_mode",
            "dt_minutes": round(dt_min, 4),
            "hard_commitment_start": self._iso(effective_hard),
            "hard_commitment_lead_short": lead_short,
        })
        self._prepared = result
        self._last_prepared_key = key
        return result

    def commit_tick(self, now: Optional[datetime] = None, *, energy: Any = None,
                    episode_id: Optional[str] = None) -> Dict[str, Any]:
        """拍后提交：会话字段落盘；到点则**一次性**关会话/结算/发事件。"""
        current = self._now(now)
        session = self._session
        if not self.enabled or session is None:
            return {"committed": False, "woke": False, "reason": "inactive"}
        prepared = self._prepared
        if prepared is None:
            return {"committed": False, "woke": False, "reason": "not_prepared"}
        if prepared.get("no_sleep_mode"):
            self._save_session()
            return {"committed": True, "woke": False, "reason": "no_sleep_mode"}
        session.continuous_minutes = int(prepared.get("continuous_minutes") or 0)
        session.interruption_count = int(prepared.get("interruption_count") or 0)
        if prepared.get("reset"):
            self._emit_once(
                session, f"interrupted:{session.interruption_count}",
                EVENT_SLEEP_INTERRUPTED,
                count=int(session.interruption_count),
                minutes=round(float(prepared.get("interrupted_minutes") or 0.0), 1),
                reason="confirmed_awake")
            self._log("sleep.interrupted", session_id=session.session_id,
                      count=session.interruption_count,
                      minutes=round(float(prepared.get("interrupted_minutes") or 0.0), 1))
        wake = prepared.get("wake") or {}
        if wake.get("awake"):
            return self._close_session(current, str(wake.get("reason") or "manual"),
                                       energy=energy, episode_id=episode_id)
        self._save_session()
        return {"committed": True, "woke": False, "reason": "sleeping"}

    # ── 一次性结算（醒来）────────────────────────────────────────────
    def _close_session(self, now: datetime, reason: str, *, energy: Any = None,
                       episode_id: Optional[str] = None) -> Dict[str, Any]:
        session = self._session
        if session is None:
            return {"committed": False, "woke": False, "reason": "no_session"}
        if reason not in WAKE_REASONS:
            reason = "manual"
        if session.closed:
            return {"committed": True, "woke": False, "reason": "already_closed",
                    "session_id": session.session_id}
        started_at = self._aware(self._parse(session.metadata.get("sleep_started_at")), now) or now
        total_window_minutes = max(0.0, (now - started_at).total_seconds() / 60.0)
        interrupted = self._clock.record.total_interrupted_minutes(now)
        actual_sleep_minutes = max(0.0, total_window_minutes - interrupted)
        actual_hours = actual_sleep_minutes / 60.0
        quality = sleep_quality(interrupted, total_window_minutes)
        debt_before = (session.sleep_debt_before if session.sleep_debt_before is not None
                       else self._debt_now())
        # 复核 P0-4：零债基准对齐配置作息窗口（会话 metadata 携带 sleep_min/wake_min），
        # 短窗口用户睡满配置时长不再净增债；缺元数据回退 8.0 旧口径。
        baseline = debt_baseline_hours(session.metadata.get("sleep_min"),
                                       session.metadata.get("wake_min"))
        debt_after = settle_sleep_debt(debt_before, actual_hours, baseline_hours=baseline)
        session.metadata["debt_baseline_hours"] = round(baseline, 4)
        session.actual_end_minute = self._frame_minutes(now, session.schedule_date)
        session.wake_reason = reason
        session.continuous_minutes = int(round(
            self._clock.continuous_minutes(now)))
        session.interruption_count = int(self._clock.record.interruption_count)
        session.quality = round(quality, 4)
        session.energy_end = self._energy_now(energy)
        session.sleep_debt_after = round(debt_after, 4)
        session.closed_at = now
        session.metadata["settled"] = True
        session.metadata["actual_sleep_hours"] = round(actual_hours, 4)
        session.metadata["interrupted_minutes"] = round(interrupted, 1)
        session.metadata["total_window_minutes"] = round(total_window_minutes, 1)
        session.metadata["wake_minute"] = int(session.actual_end_minute)
        # PR-SLEEP-RECOVERY（规格 §1C）：边界冲突（上限/窗口末端/硬约定强制醒）
        # 睡不够恢复目标 → 如实记录未满足需求；**不扣债不假算**——债结算仍按
        # 上方真实有效时长公式（settle_sleep_debt），此处只写诊断 metadata。
        session.effective_sleep_hours = round(actual_hours, 4)
        if (reason in {"lie_in_cap", "wake_window_end", "hard_commitment"}
                and actual_hours < session.recovery_target_hours):
            session.metadata["recovery_unmet_hours"] = round(
                session.recovery_target_hours - actual_hours, 4)
        self._save_session()
        finish = getattr(self._store, "finish_sleep_session", None)
        if callable(finish):
            try:
                finish(
                    session.session_id,
                    actual_end_minute=session.actual_end_minute,
                    wake_reason=session.wake_reason,
                    continuous_minutes=session.continuous_minutes,
                    interruption_count=session.interruption_count,
                    energy_end=session.energy_end,
                    sleep_debt_after=session.sleep_debt_after,
                    quality=session.quality,
                    metadata=dict(session.metadata),
                )
            except Exception as exc:
                logger.warning("sleep_sessions 结束写入失败（忽略）：%s", exc)
        # 一次性 LifeState 结算口：睡眠债/醒来原因/时间戳（Δ精力已由 tick 积分，
        # 这里绝不再按三档覆盖精力——避免与 handle_sleep 双记账）
        settle = getattr(self._life, "apply_sleep_settlement", None)
        if callable(settle):
            try:
                settle({
                    "session_id": session.session_id,
                    "sleep_hours": round(actual_hours, 4),
                    "sleep_quality": round(quality, 4),
                    "sleep_debt": round(debt_after, 4),
                    "wake_reason": reason,
                    "energy_end": session.energy_end,
                    "continuous_minutes": session.continuous_minutes,
                    "interruption_count": session.interruption_count,
                    "sleep_started_at": session.metadata.get("sleep_started_at"),
                    "now": now,
                })
            except Exception as exc:
                logger.warning("LifeState 睡眠结算失败（忽略）：%s", exc)
        # 关闭 sleep episode（planner 注入；只调一次）
        chosen_episode = episode_id or self._episode_id
        if self._episode_closer is not None and chosen_episode:
            try:
                self._episode_closer(str(chosen_episode),
                                     int(session.actual_end_minute), reason)
            except Exception as exc:
                logger.warning("sleep episode 关闭失败（忽略）：%s", exc)
        # 更新 schedule_items sleep 端点（sleep_schedule 共享写回；只调一次）
        if self._sleep_endpoint_updater is not None:
            try:
                self._sleep_endpoint_updater(now, int(session.actual_end_minute),
                                             f"sleep_engine_{reason}",
                                             schedule_date=session.schedule_date)
            except TypeError:
                try:
                    self._sleep_endpoint_updater(now, int(session.actual_end_minute),
                                                 f"sleep_engine_{reason}")
                except Exception as exc:
                    logger.warning("睡眠端点写回失败（忽略）：%s", exc)
            except Exception as exc:
                logger.warning("睡眠端点写回失败（忽略）：%s", exc)
        # 事件恰一次
        self._emit_once(session, "woke", EVENT_SLEEP_WOKE,
                        wake_reason=reason,
                        actual_minutes=round(total_window_minutes, 1),
                        sleep_hours=round(actual_hours, 4),
                        energy=session.energy_end,
                        quality=session.quality,
                        sleep_debt=session.sleep_debt_after)
        # 评估 P3 修复（2026-11-04）：woke 发送标记随 metadata.emitted 补一次落盘。
        # 旧实现最后一次写库（上方 _save_session/finish）都在 _emit_once 之前，
        # 重启前未 persist 时 emitted 缺 woke（生产 sleep:2026-10-31:155 实例）。
        # 会话已 closed：本次写库对 LifeState 睡眠字段是同值幂等，不重复结算。
        self._save_session()
        self._log("sleep.session_woke", session_id=session.session_id,
                  wake_reason=reason, sleep_hours=round(actual_hours, 3),
                  quality=session.quality, sleep_debt=session.sleep_debt_after,
                  continuous_minutes=session.continuous_minutes)
        self._prepared = None
        dynamic = session.metadata.get("dynamic_sleep") or {}
        earliest = self._aware(self._parse(dynamic.get("wake_anchor"))
                               or session.earliest_wake_at, now)
        wake_delay = 0.0
        if isinstance(earliest, datetime) and now > earliest:
            wake_delay = (now - earliest).total_seconds() / 60.0
        return {"committed": True, "woke": True, "reason": reason,
                "session_id": session.session_id,
                "actual_sleep_hours": round(actual_hours, 4),
                "wake_delay_minutes": round(max(0.0, wake_delay), 1),
                "sleep_debt": session.sleep_debt_after,
                "quality": session.quality}

    def note_interruption(self, now: Optional[datetime] = None) -> bool:
        """夜间打扰记账（emotion ``note_night_reply`` / ``emotion.sleep_interrupt`` 接）。"""
        if not self.enabled:
            return False
        session = self._session
        if session is None or session.closed:
            return False
        current = self._now(now)
        outcome = self._clock.note_interruption(current, self._interrupt_threshold())
        if session.metadata.get("dynamic_sleep"):
            self._dynamic_view["interruption"] = ("confirmed" if outcome.get("reset") else
                                                  "brief" if outcome.get("counted") else "ignored")
        if not outcome.get("counted"):
            return False
        session.interruption_count = int(outcome.get("interruption_count") or 0)
        session.continuous_minutes = int(round(self._clock.continuous_minutes(current)))
        if outcome.get("reset"):
            self._emit_once(
                session, f"interrupted:{session.interruption_count}",
                EVENT_SLEEP_INTERRUPTED,
                count=int(session.interruption_count),
                minutes=round(self._clock.record.total_interrupted_minutes(current), 1),
                reason="confirmed_awake")
        self._save_session()
        return True

    def force_wake(self, now: Optional[datetime] = None, reason: str = "manual",
                   *, energy: Any = None) -> Dict[str, Any]:
        """手动/外部强制醒来（诊断/工具；仍走一次性结算链）。"""
        if not self.enabled:
            return {"committed": False, "woke": False, "reason": "disabled"}
        return self._close_session(self._now(now), reason, energy=energy)

    def shutdown_session(self, now: Optional[datetime] = None,
                         reason: str = "manual") -> Dict[str, Any]:
        """配置热关闭/停用时的会话收尾（**不受** ``enabled`` 开关影响）。

        用户 2026-10-27 批准：热关闭以关闭时刻结束当前睡眠会话（一次结算、actual_end=
        真实时刻），再交回旧调度。此时配置里的开关可能已先被改成关闭，故不能走
        :meth:`force_wake`（会按 disabled 直接返回、留下 open 会话卡住旧 planner）。
        无未关闭会话 → ``no_session``（不虚构结束、不改既有历史）。
        """
        session = self._session
        if session is None or session.closed:
            return {"committed": False, "woke": False, "reason": "no_session"}
        return self._close_session(self._now(now), reason)

    def persist(self) -> None:
        """停机兜底落盘（不解算会话；会话跨重启由 :meth:`restore` 接回）。"""
        try:
            self._save_session()
        except Exception:
            pass
# __APPEND_SLEEP_ENGINE__
