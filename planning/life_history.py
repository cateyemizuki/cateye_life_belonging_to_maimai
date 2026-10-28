"""Bounded observations of real life state; no replay, model calls or bot actions."""
from __future__ import annotations

import json
import logging
import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.storage import save_json_atomic

logger = logging.getLogger(__name__)
FIELDS = ('energy', 'hunger', 'focus', 'stress', 'social_battery')
MAX_POINTS = 2304
RETENTION_HOURS = 168
SAMPLE_SECONDS = 300
MAX_FILE_BYTES = 2 * 1024 * 1024


def _time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp if stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


class LifeStateHistory:
    """Persist a 7-day / 2304-point ring; a missing observation stays missing."""

    def __init__(self, path: Path, decision_logger: Any = None) -> None:
        self.path = Path(path)
        self._logger = decision_logger
        self._points: list[dict[str, Any]] = []
        self._writable = True
        self._persisted = True
        self._clock_regressed = False
        self._load()

    @staticmethod
    def _normalize(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        stamp = _time(raw.get('at') or raw.get('updated_at'))
        if stamp is None:
            return None
        result: dict[str, Any] = {'at': stamp.isoformat()}
        for key in (*FIELDS, 'sleep_debt'):
            value = raw.get(key)
            ceiling = 12. if key == 'sleep_debt' else 1.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return None
            result[key] = round(max(0., min(ceiling, float(value))), 6)
        result['mental_state'] = round(result['energy'] * result['focus'] * (1-result['stress']), 6)
        result['sleeping'] = bool(raw.get('sleeping', raw.get('sleep_started_at')))
        result['interrupted'] = bool(raw.get('interrupted', raw.get('sleep_interrupted', False)))
        result['reason'] = str(raw.get('reason') or 'sample')[:40]
        return result

    def _load(self) -> None:
        try:
            with self.path.open('rb') as stream:
                source = stream.read(MAX_FILE_BYTES+1)
            if len(source) > MAX_FILE_BYTES:
                raise ValueError('history exceeds read bound')
            raw = json.loads(source)
            if not isinstance(raw, dict):
                raise ValueError('history must be an object')
            if type(raw.get('version')) is not int or raw['version'] != 1:
                self._writable = False
                self._persisted = False
                logger.warning('未知生活历史版本，保留原文件并停用历史写入：%s', self.path)
                return
            if not isinstance(raw.get('points'), list):
                raise ValueError('history points must be a list')
            rows = [self._normalize(item) for item in raw['points'][-MAX_POINTS:]]
            valid = [row for row in rows if row is not None]
            valid.sort(key=lambda row: _time(row['at']).timestamp())
            # Last-writer observation for an identical timestamp; no invented samples.
            unique = {row['at']: row for row in valid}
            self._points = list(unique.values())[-MAX_POINTS:]
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            try:
                if self.path.exists():
                    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
                    shutil.copy2(self.path, self.path.with_name(f'{self.path.name}.corrupt-{stamp}.bak'))
            except OSError:
                self._writable = False
            self._persisted = False
            logger.warning('生活历史读取失败（保留备份后重新采样，不阻塞）：%s', exc)

    def sample(self, snapshot: Any, *, reason: str = 'sample', force: bool = False) -> bool:
        """Accept actual observations; failure to persist only affects durability."""
        if not self._writable or not isinstance(snapshot, dict):
            return False
        row = self._normalize({**snapshot, 'reason': reason})
        if row is None:
            return False
        at = _time(row['at'])
        if self._points:
            elapsed = (at - _time(self._points[-1]['at'])).total_seconds()
            if elapsed < 0:
                self._clock_regressed = True
                return False
            self._clock_regressed = False
            if not force and elapsed < SAMPLE_SECONDS:
                return False
            if elapsed == 0:
                self._points.pop()
        cutoff = at - timedelta(hours=RETENTION_HOURS)
        self._points = [item for item in self._points if _time(item['at']) >= cutoff]
        self._points.append(row)
        self._points = self._points[-MAX_POINTS:]
        try:
            self._persisted = bool(save_json_atomic(self.path, {'version': 1, 'points': self._points}))
        except Exception:
            self._persisted = False
        if self._logger is not None:
            try:
                self._logger.record(action='life.state_changed', trigger_type='life_state_history',
                                    state=dict(row), history_persisted=self._persisted)
            except Exception:
                pass
        return True

    def snapshot(self, *, now: datetime, hours: int = 24, limit: int = 600) -> dict[str, Any]:
        cutoff = now-timedelta(hours=max(1, min(RETENTION_HOURS, int(hours))))
        rows = [dict(item) for item in self._points if cutoff <= _time(item['at']) <= now]
        for index, row in enumerate(rows):
            if index and (_time(row['at'])-_time(rows[index-1]['at'])).total_seconds() > SAMPLE_SECONDS*3:
                row['gap_before'] = True
        total = len(rows)
        limit = max(2, min(MAX_POINTS, int(limit)))
        if total > limit:
            indices = [round(i*(total-1)/(limit-1)) for i in range(limit)]
            reduced = []
            previous = -1
            for index in indices:
                row = rows[index]
                if any(item.get('gap_before') for item in rows[previous+1:index+1]):
                    row['gap_before'] = True
                reduced.append(row)
                previous = index
            rows = reduced
        return {'points': rows, 'total_points': total, 'downsampled': total > limit,
                'interval_seconds': SAMPLE_SECONDS, 'capacity': MAX_POINTS,
                'retention_hours': RETENTION_HOURS, 'persisted': self._persisted,
                'writable': self._writable, 'clock_regressed': self._clock_regressed,
                'started_at': self._points[0]['at'] if self._points else None}
