"""Stable personality tendencies and durable silence-cycle facts, never chat text."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from core.json_payload import extract_json_payload
from core.storage import save_json_atomic

logger = logging.getLogger(__name__)
DEFAULT_ANCHOR_HOURS = 6.0
DEFAULT_LONELINESS = 0.5
RECOVERY_GRACE_SECONDS = 900

# ---------------------------------------------------------------------------
# P0-ACT-1 阶梯想念（2026-11-11 Gemini 批）
#
# 旧实现：单个沉默周期只受理一次 ``miss`` claim，一开口就永久消耗整个周期
# （6h 触发后 48h 仍未回也再不开口）。阶梯把「受理」拆成四个阶段；阶段 1~3 各占
# 一次独立 claim，阶段窗口按**清醒沉默时长**（已扣睡眠）计量：
#
#   阶段 1：6h   – 20h   轻度试探（禁直白表白）
#   阶段 2：20h  – 48h   在意升温
#   阶段 3：48h  – 120h  直接追问去向（120h 含在本阶段内）
#   阶段 4：> 120h       渗透模式：**不再独立触发**，改由每日主动任务承载
#
# 阶段 4（2026-11-11 gemini3.8flash 四阶段方案）：沉默超过 120h 后，本巡检不再
# 发起独立 ``miss`` 回合——``evaluate_miss`` 直接返回
# ``stage4_delegated_to_daily_tasks``，不产出 probability/eligible，也不写 claim。
# 该阶段只提供**只读**状态（``PatrolService.current_target_miss_stage``），由早午晚安、
# 视频分享等每日主动任务在派发时拼接待办文案；沉默事实 ``relation.silence_detected``
# 与每日预算内的其他巡检行为不受影响，仍是独立链路。
#
# 阶段窗口是「可受理区间」，不是「必然开口时刻」：状态门槛（沟通容量/动机阈值）
# 仍然先行，阶段只决定本次受理属于哪一档、用哪一档文案。
# ---------------------------------------------------------------------------

#: 每阶段的起止（小时；下界含、上界不含，阶段 3 含 120h）。
MISS_STAGE_WINDOWS: tuple[tuple[float, float], ...] = (
    (6.0, 20.0),
    (20.0, 48.0),
    (48.0, 120.0),
)
#: 阶段 3 的上界 = 阶段 4 的入口：``> 120h`` 进入阶段 4（渗透模式，独立想念停发，
#: 改由每日主动任务承载），本周期不再产生独立的 ``miss`` 回合。
MISS_STAGE_END_HOURS = MISS_STAGE_WINDOWS[-1][1]
#: 单次巡检（默认 10 分钟）的想念概率带：与巡检间隔匹配，杜绝「6h 后 90% 立刻开口」。
MISS_STAGE_PROB_MIN = 0.05
MISS_STAGE_PROB_MAX = 0.12
#: 阶段内先按 0.05 静候多久再开始爬升（小时）。首个达标候选通常落在阶段起点后
#: 约 3h（动机阈值），因此静候 4h ≈ 达标后 1h 仍是很低的单拍概率。
MISS_STAGE_PROB_HOLD_HOURS = 4.0
#: 静候之后从 0.05 爬到 0.12 所需的时长（小时）。实测（10 分钟一拍）首个达标候选后
#: 1h 约 30%、2h 约 53%、3h 约 74%、4h 约 88% 触发——主质量落在「达标后 2–4 小时」，
#: 且不会再出现 6h 后 90% 立刻开口。
MISS_STAGE_PROB_RAMP_HOURS = 2.0
#: 短回复（<3 字/纯表情）的顺延量：每来一条只把周期起点前移 1.5h（不归零）。
SHORT_REPLY_DEFER_HOURS = 1.5
#: 短回复顺延的「半衰期」上限：多条短回复最多抵消当前沉默的一半，绝不能把周期清零。
SHORT_REPLY_HALF_LIFE = 0.5
#: 阶段 claim 键前缀（``miss:s1`` / ``miss:s2`` / ``miss:s3``）；旧单次 claim = ``miss``。
STAGE_CLAIM_PREFIX = 'miss:s'


def epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = value.timestamp() if isinstance(value, datetime) else float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class MissDecision:
    eligible: bool
    probability: float
    reason: str
    silence_hours: float = 0.0
    motivation: float = 0.0
    capacity: float = 0.0
    stage: int = 0


def _clamp(value: Any, low: float, high: float) -> float:
    """防御性钳位（非法/NaN → ``low``）；仅服务阶梯窗口与概率带。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if not math.isfinite(number):
        return low
    return max(low, min(high, number))


def _stage_starts(anchor: float) -> tuple[float, float, float]:
    """三个阶段的下界（人设锚点只抬不降；锚点 > 阶段下界时该阶段窗口自然收缩）。"""
    first = max(MISS_STAGE_WINDOWS[0][0], anchor)
    return (first, max(MISS_STAGE_WINDOWS[1][0], first),
            max(MISS_STAGE_WINDOWS[2][0], max(MISS_STAGE_WINDOWS[1][0], first)))


def miss_stage(hours: Any, anchor: float = DEFAULT_ANCHOR_HOURS) -> int:
    """清醒沉默时长 → 阶段号（1/2/3/4）；不在任何阶段窗口内 → 0。

    窗口：6–20h / 20–48h / 48–120h（人设锚点更高时下界上抬）。**120h 含在阶段 3**；
    **超过 120h 返回 4**（渗透模式）：本周期不再产生独立想念主动消息，改由每日主动
    任务承载；等用户下一次真实长回复重建周期。阶段 4 没有 claim 键
    （:func:`stage_claim_kind` 仍返回空串），也不参与 :func:`_stage_probability`。
    """
    try:
        value = float(hours)
        anchor_value = max(2.0, min(24.0, float(anchor)))
    except (TypeError, ValueError):
        return 0
    if not (math.isfinite(value) and math.isfinite(anchor_value)) or value < 0:
        return 0
    starts = _stage_starts(anchor_value)
    if value >= starts[2]:
        return 3 if value <= MISS_STAGE_END_HOURS else 4
    if value >= starts[1]:
        return 2
    if value >= starts[0]:
        return 1
    return 0


def miss_stage_start(stage: Any, anchor: float = DEFAULT_ANCHOR_HOURS) -> float:
    """阶段 ``stage`` 的起始小时数（非法阶段 → 0）。"""
    try:
        index = int(stage)
        anchor_value = max(2.0, min(24.0, float(anchor)))
    except (TypeError, ValueError):
        return 0.0
    if index not in (1, 2, 3):
        return 0.0
    return _stage_starts(anchor_value)[index - 1]


def stage_claim_kind(stage: Any) -> str:
    """阶段号 → 持久化 claim 键（``miss:s1``…）；非法阶段 → 空串。"""
    try:
        index = int(stage)
    except (TypeError, ValueError):
        return ''
    return f'{STAGE_CLAIM_PREFIX}{index}' if index in (1, 2, 3) else ''


def is_miss_claim_kind(kind: Any) -> bool:
    """``miss``（旧单次）或 ``miss:s1``…``miss:s3``（阶梯）；阶段 4 无 claim 键。"""
    text = str(kind or '')
    if text == 'miss':
        return True
    if not text.startswith(STAGE_CLAIM_PREFIX):
        return False
    try:
        index = int(text[len(STAGE_CLAIM_PREFIX):])
    except (TypeError, ValueError):
        return False
    return index in (1, 2, 3)


def open_miss_claim_kinds(claims: Any) -> list[str]:
    """周期内仍**未落定**的想念 claim（``reserved`` / ``pending_reply``）。

    按阶段从高到低返回，旧单次 ``miss`` 排最后；终态（spoken/published/unknown）
    不返回——unknown 保留占用是既有契约，但不再阻止新的阶梯阶段。
    """
    if not isinstance(claims, dict):
        return []

    def order(kind: str) -> tuple[int, int]:
        if kind == 'miss':
            return (1, 0)  # 旧单次 claim 排最后（阶梯阶段优先确认）
        try:
            return (0, -int(kind[len(STAGE_CLAIM_PREFIX):]))
        except (TypeError, ValueError):
            return (0, 0)

    pending = [kind for kind, status in claims.items()
               if is_miss_claim_kind(kind) and status in ('reserved', 'pending_reply')]
    pending.sort(key=order)
    return pending


def effective_anchor_epoch(row: Any) -> float | None:
    """周期行 → **生效**起点 epoch（短路顺延之后；``defer`` 只后移不越过真实来信）。"""
    if not isinstance(row, dict):
        return None
    anchor = epoch(row.get('anchor'))
    if anchor is None:
        return None
    defer = epoch(row.get('defer'))
    if defer is None or defer < 0:
        defer = 0.0
    raw = epoch(row.get('raw_anchor'))
    limit = raw if raw is not None else anchor + defer
    return max(anchor, min(anchor + defer, limit))


def cycle_defer_hours(row: Any) -> float:
    """周期行因短回复被顺延的小时数（原周期起点 → 生效起点；缺失/异常 → 0.0）。"""
    if not isinstance(row, dict):
        return 0.0
    anchor = epoch(row.get('anchor'))
    effective = effective_anchor_epoch(row)
    if anchor is None or effective is None:
        return 0.0
    return max(0.0, (effective - anchor) / 3600.0)


def _stage_probability(hours: float, anchor: float, stage: int,
                       loneliness: float, emotion: float) -> float:
    """阶段内单拍概率：``MISS_STAGE_PROB_MIN`` → ``MISS_STAGE_PROB_MAX`` 平滑爬升。

    先按 ``MISS_STAGE_PROB_HOLD_HOURS`` 静候在 ``MIN``，再用
    ``MISS_STAGE_PROB_RAMP_HOURS`` 平滑爬到 ``MAX``；人设孤独度与情绪强度只做
    带内微调（×0.85–1.10 后再钳回带内）。结果恒在 ``[MIN, MAX]``——单拍概率不再
    出现旧公式的 0.35 顶格。
    """
    start = miss_stage_start(stage, anchor)
    ramp = _clamp((hours - start - MISS_STAGE_PROB_HOLD_HOURS)
                  / MISS_STAGE_PROB_RAMP_HOURS, 0.0, 1.0)
    persona = _clamp(loneliness, 0.0, 1.0)
    feeling = _clamp(emotion / 0.8 if emotion else 0.0, 0.0, 1.0)
    micro = 0.85 + 0.15 * persona + 0.10 * feeling
    base = MISS_STAGE_PROB_MIN + (MISS_STAGE_PROB_MAX - MISS_STAGE_PROB_MIN) * ramp
    return _clamp(base * micro, MISS_STAGE_PROB_MIN, MISS_STAGE_PROB_MAX)


def evaluate_miss(hours: float, anchor: float, loneliness: float,
                  energy: Any, social: Any, valence: Any, *,
                  staged: bool = False) -> MissDecision:
    """Silence creates need; low social energy reduces ability to speak.

    ``staged=False``（默认）保持 3.17.33 及以前的逐字契约：概率按单拍上限 .35 计算，
    由持久化周期回执限制每个沉默周期只受理一次。

    ``staged=True``（P0-ACT-1 阶梯想念）：状态门槛（沟通容量 / 动机阈值）完全不变，
    只把概率换成 ``[.05, .12]`` 的阶段内平滑带，并在 ``stage`` 里带出 1/2/3/4。
    受理阶段由调用方按 stage 各占一次 claim；**阶段 4 不参与受理**——直接返回
    ``stage4_delegated_to_daily_tasks``（不产出 probability / eligible），独立想念停发，
    改由每日主动任务承载。
    """
    if not all(math.isfinite(x) for x in (hours, anchor, loneliness)) or hours < 0:
        return MissDecision(False, 0, 'invalid_time')
    anchor = max(2.0, min(24.0, anchor))
    loneliness = max(0.0, min(1.0, loneliness))
    stage = miss_stage(hours, anchor) if staged else 0
    if hours < anchor:
        return MissDecision(False, 0, 'before_personality_anchor', hours, stage=stage)
    if staged and stage <= 0:
        return MissDecision(False, 0, 'outside_stage_window', hours)
    if staged and stage == 4:
        # 阶段 4 的归因必须稳定：放在容量/动机门槛之前，低电量或负情绪也不会把
        # reason 盖成 low_communication_capacity / motivation_below_threshold。
        return MissDecision(False, 0.0, 'stage4_delegated_to_daily_tasks', hours,
                            stage=4)
    def unit(value: Any, fallback: float = .5) -> float:
        result = epoch(value)
        return max(0., min(1., result)) if result is not None else fallback
    e, s = unit(energy), unit(social)
    capacity = e * s
    if e < .2 or s < .2:
        return MissDecision(False, 0, 'low_communication_capacity', hours, capacity=capacity,
                            stage=stage)
    v = epoch(valence) or 0.0
    # Both seeking company and having something cheerful to share are motives.
    emotion = min(1., abs(v)) * (.8 if v < 0 else .6)
    accumulated = min(1., (hours - anchor) / max(anchor, 2.))
    motivation = .65 * accumulated + .35 * emotion
    threshold = .55 - .25 * loneliness
    if motivation < threshold:
        return MissDecision(False, 0, 'motivation_below_threshold', hours, motivation, capacity,
                            stage)
    if staged:
        p = _stage_probability(hours, anchor, stage, loneliness, emotion)
    else:
        p = min(.35, (.08 + .22 * (motivation - threshold) / max(.1, 1. - threshold))
                * math.sqrt(capacity))
    return MissDecision(True, p, 'candidate', hours, motivation, capacity, stage)


class InitiativeMemory:
    """One owner for H/L and per-target real-message cycle receipts.

    Claims persist before side effects; an unknown outcome keeps its claim.
    Corrupt storage blocks the current cycle until a new real message arrives.
    """
    def __init__(self, data_dir: Any = None) -> None:
        self.path = Path(data_dir) / 'initiative_state.json' if data_dir else None
        self._lock = asyncio.Lock()
        self.corrupt = False
        try:
            self.doc = json.loads(self.path.read_text(encoding='utf-8')) if self.path and self.path.exists() else {}
            if not isinstance(self.doc, dict) or self.doc.get('version', 1) != 1:
                raise ValueError('unsupported initiative state')
        except (OSError, ValueError, TypeError):
            self.doc = {}
            self.corrupt = True
        self.doc.setdefault('version', 1)
        for key in ('profile', 'cycles'):
            if key in self.doc and not isinstance(self.doc[key], dict):
                self.doc[key] = {}
                self.corrupt = True
            else:
                self.doc.setdefault(key, {})

    def _save(self) -> bool:
        if self.path is None:
            return True  # isolated/in-memory consumer, no production data directory
        try:
            result = save_json_atomic(self.path, self.doc)
            return result is not False
        except Exception:
            logger.warning('主动动机状态保存失败，本轮不执行', exc_info=True)
            return False

    def profile(self) -> dict:
        raw = dict(self.doc.get('profile') or {})
        h, l = epoch(raw.get('anchor_hours')), epoch(raw.get('loneliness'))
        if raw and (h is None or l is None or not 2 <= h <= 24 or not 0 <= l <= 1):
            raw.update(anchor_hours=DEFAULT_ANCHOR_HOURS, loneliness=DEFAULT_LONELINESS,
                       source='default', fallback_reason='invalid_stored_disposition')
        return raw

    async def initialize(self, personality: str, llm: Any, *, force: bool = False) -> dict:
        fingerprint = hashlib.sha256(str(personality).encode('utf-8')).hexdigest()
        async with self._lock:
            prior = self.profile()
            if not force and prior.get('persona_hash') == fingerprint:
                return prior
            profile = {'persona_hash': fingerprint, 'anchor_hours': DEFAULT_ANCHOR_HOURS,
                       'loneliness': DEFAULT_LONELINESS, 'source': 'default', 'version': 1}
            if personality and llm is not None:
                try:
                    text = await llm.generate(
                        '仅根据下列bot人设确定稳定的联系倾向，不能根据当前用户行为推测。'
                        '输出JSON：{"anchor_hours":2到24的数字,"loneliness":0到1的数字}。'
                        'anchor_hours表示收到对方消息后至少沉默多久才开始想念候选；'
                        'loneliness表示对缺少联系的敏感度，不是当前心情。'
                        '内向不等于孤独、没有精力不等于想联系。只输出JSON，不输出思考。\n人设：'
                        + str(personality)[:5000], task_name='utils', module='companion', event='companion_disposition')
                    raw = extract_json_payload(text)
                    h, l = raw['anchor_hours'], raw['loneliness']
                    if isinstance(h, bool) or isinstance(l, bool):
                        raise ValueError('boolean disposition')
                    h, l = float(h), float(l)
                    if not (math.isfinite(h) and math.isfinite(l) and 2 <= h <= 24 and 0 <= l <= 1):
                        raise ValueError('out of bounds')
                    profile.update(anchor_hours=h, loneliness=l, source='persona_llm')
                except asyncio.CancelledError:
                    raise
                except Exception:
                    profile['fallback_reason'] = 'generation_unavailable'
                    logger.warning('联系人设倾向生成未成功，采用中性默认值', exc_info=True)
            self.doc['profile'] = profile
            self._save()
            return dict(profile)

    def cycle(self, target: str, last_message: Any, now: Any, *,
              short_reply: bool | None = None) -> dict | None:
        """取/推进当前沉默周期行。

        ``short_reply=True``（<3 字/纯表情）**不新建周期**：保留已用阶段 claim，
        只把生效起点往后顺延 ``SHORT_REPLY_DEFER_HOURS``（上限 = 当前沉默的一半，
        即半衰期；绝不越过真实来信时刻）。``False``/``None``（真实长回复或文本
        不可得）沿用旧语义：新建周期、清空 claim。

        ``raw_anchor`` 记录**真实**最后来信时刻，``defer`` 记录短回复顺延秒数；
        周期键 ``anchor`` 保持该周期首条来信时刻不变——派发 key 与超时回收因此
        在短回复前后仍指向同一周期。
        """
        raw, at = epoch(last_message), epoch(now)
        if not target or raw is None or at is None or raw > at:
            return None
        cycles = self.doc['cycles']
        row = cycles.get(target)
        if row is not None and (not isinstance(row, dict) or epoch(row.get('anchor')) is None
                               or epoch(row.get('observed_at')) is None
                               or not isinstance(row.get('claims'), dict)):
            self.corrupt = True
            row = None
        row_anchor = epoch(row.get('anchor')) if isinstance(row, dict) else None
        row_raw = epoch(row.get('raw_anchor')) if isinstance(row, dict) else None
        if row_anchor is None:
            row = None
        elif row_raw is None:
            row_raw = row_anchor
        if not isinstance(row, dict) or raw > row_raw:
            if isinstance(row, dict) and short_reply:
                # 短回复：同一周期内只顺延起点，已发出的阶段不回收、不重复。
                defer = epoch(row.get('defer')) or 0.0
                elapsed = max(0.0, raw - float(row_anchor or raw))
                ceiling = elapsed * SHORT_REPLY_HALF_LIFE
                row['defer'] = max(0.0, min(defer + SHORT_REPLY_DEFER_HOURS * 3600.0,
                                            ceiling))
                row['raw_anchor'] = raw
                row['observed_at'] = at
                # 顺延落盘失败不影响本轮判定：进程内状态已一致，下一轮会再次尝试。
                self._save()
                return row
            row = {'anchor': raw, 'raw_anchor': raw, 'defer': 0.0,
                   'observed_at': at, 'claims': {}}
            if self.corrupt:
                # 损坏恢复：本周期所有受理口（旧单次 + 三个阶梯阶段 + 沉默事件）
                # 一并占住，直到新的真实来信建立新周期——阶梯不能让恢复期漏口。
                row['claims'] = {kind: 'uncertain_recovery' for kind in
                                 ('miss', stage_claim_kind(1), stage_claim_kind(2),
                                  stage_claim_kind(3), 'silence')}
                self.corrupt = False
            cycles[target] = row
            # Store only a few target scopes; real relationship history lives elsewhere.
            for key in list(cycles)[:-4]:
                cycles.pop(key, None)
            if not self._save():
                return None
        elif raw < float(row_anchor):
            return None  # stale user timestamp must not resurrect an older cycle
        return row

    def claim(self, target: str, last_message: Any, now: Any, kind: str, *,
              status: str = 'reserved') -> bool:
        """登记一次周期占用；``status`` 供调用方标注交付阶段（P1-8: pending_reply）。"""
        row = self.cycle(target, last_message, now)
        if row is None or kind in row['claims']:
            return False
        if float(epoch(now) or 0) - float(row['observed_at']) < RECOVERY_GRACE_SECONDS:
            return False
        row['claims'][kind] = str(status)
        return self._save()

    def release(self, target: str, kind: str) -> bool:
        """退回一次**仍未落定**的周期占用（P1-8：definite not-enqueued / 超时未回复）。

        只释放 pending 类状态（``reserved`` / ``pending_reply``）；``spoken``、
        ``published`` 与 unknown 终态绝不回滚——unknown 交付态保留占用的既有语义不变。
        """
        row = self.doc.get('cycles', {}).get(target)
        if not isinstance(row, dict):
            return False
        claims = row.get('claims')
        if not isinstance(claims, dict):
            return False
        previous = claims.get(kind)
        if previous not in ('reserved', 'pending_reply'):
            return False
        del claims[kind]
        if self._save():
            return True
        claims[kind] = previous  # 落盘失败恢复内存态，避免占用被静默丢掉
        return False

    def receipt(self, target: str, kind: str, status: str) -> bool:
        row = self.doc.get('cycles', {}).get(target)
        if isinstance(row, dict) and kind in row.get('claims', {}):
            row['claims'][kind] = str(status)
            return self._save()
        return False
