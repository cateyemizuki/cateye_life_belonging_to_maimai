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


def evaluate_miss(hours: float, anchor: float, loneliness: float,
                  energy: Any, social: Any, valence: Any) -> MissDecision:
    """Silence creates need; low social energy reduces ability to speak.

    Probability is per existing patrol tick, capped at .35. A durable cycle
    receipt, not this random draw, limits each silence cycle to one request.
    """
    if not all(math.isfinite(x) for x in (hours, anchor, loneliness)) or hours < 0:
        return MissDecision(False, 0, 'invalid_time')
    anchor = max(2.0, min(24.0, anchor))
    loneliness = max(0.0, min(1.0, loneliness))
    if hours < anchor:
        return MissDecision(False, 0, 'before_personality_anchor', hours)
    def unit(value: Any, fallback: float = .5) -> float:
        result = epoch(value)
        return max(0., min(1., result)) if result is not None else fallback
    e, s = unit(energy), unit(social)
    capacity = e * s
    if e < .2 or s < .2:
        return MissDecision(False, 0, 'low_communication_capacity', hours, capacity=capacity)
    v = epoch(valence) or 0.0
    # Both seeking company and having something cheerful to share are motives.
    emotion = min(1., abs(v)) * (.8 if v < 0 else .6)
    accumulated = min(1., (hours - anchor) / max(anchor, 2.))
    motivation = .65 * accumulated + .35 * emotion
    threshold = .55 - .25 * loneliness
    if motivation < threshold:
        return MissDecision(False, 0, 'motivation_below_threshold', hours, motivation, capacity)
    p = min(.35, (.08 + .22 * (motivation - threshold) / max(.1, 1. - threshold)) * math.sqrt(capacity))
    return MissDecision(True, p, 'candidate', hours, motivation, capacity)


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

    def cycle(self, target: str, last_message: Any, now: Any) -> dict | None:
        anchor, at = epoch(last_message), epoch(now)
        if not target or anchor is None or at is None or anchor > at:
            return None
        cycles = self.doc['cycles']
        row = cycles.get(target)
        if row is not None and (not isinstance(row, dict) or epoch(row.get('anchor')) is None
                               or epoch(row.get('observed_at')) is None
                               or not isinstance(row.get('claims'), dict)):
            self.corrupt = True
            row = None
        if not isinstance(row, dict) or anchor > float(row.get('anchor', 0)):
            row = {'anchor': anchor, 'observed_at': at, 'claims': {}}
            if self.corrupt:
                row['claims'] = {'miss': 'uncertain_recovery', 'silence': 'uncertain_recovery'}
                self.corrupt = False
            cycles[target] = row
            # Store only a few target scopes; real relationship history lives elsewhere.
            for key in list(cycles)[:-4]:
                cycles.pop(key, None)
            if not self._save():
                return None
        if anchor < float(row.get('anchor', 0)):
            return None  # stale user timestamp must not resurrect an older cycle
        return row

    def claim(self, target: str, last_message: Any, now: Any, kind: str) -> bool:
        row = self.cycle(target, last_message, now)
        if row is None or kind in row['claims']:
            return False
        if float(epoch(now) or 0) - float(row['observed_at']) < RECOVERY_GRACE_SECONDS:
            return False
        row['claims'][kind] = 'reserved'
        return self._save()

    def receipt(self, target: str, kind: str, status: str) -> None:
        row = self.doc.get('cycles', {}).get(target)
        if isinstance(row, dict) and kind in row.get('claims', {}):
            row['claims'][kind] = str(status)
            self._save()
