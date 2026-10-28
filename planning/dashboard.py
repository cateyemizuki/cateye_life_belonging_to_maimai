"""Small, bounded, read-only dashboard projection. No context prompts or raw logs."""
from __future__ import annotations

import math
import json
from datetime import datetime, timedelta
from typing import Any

from .time_utils import TimezoneManager, format_day_minute
from .life_history import FIELDS

def _companion_summary(plugin: Any, *, date: str) -> dict[str, Any]:
    if getattr(plugin, '_companion', None) is None:
        return {'available': False}
    from companion.dashboard import companion_summary
    return companion_summary(plugin, date=date)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return round(float(value), 6)
    return None


def _row(row: Any) -> dict[str, Any]:
    """Explicit fields keep prompt metadata, user IDs and chat text out of the UI."""
    if not isinstance(row, dict):
        return {}
    result = {key: str(row[key])[:160] for key in
              ('name', 'goal_type', 'status', 'source', 'attention_mode', 'schedule_date', 'hardness')
              if row.get(key) is not None}
    for key in ('start_minute', 'end_minute', 'planned_start_minute', 'planned_end_minute',
                'actual_start_minute', 'actual_end_minute', 'day_offset', 'progress'):
        if key in row:
            result[key] = _number(row[key])
    start = result.get('planned_start_minute', result.get('start_minute'))
    end = result.get('planned_end_minute', result.get('end_minute'))
    result['time_label'] = ' — '.join(format_day_minute(int(v)) if v is not None else '待定'
                                      for v in (start, end))
    for key in ('state_before', 'state_after'):
        values = row.get(key)
        if isinstance(values, dict):
            result[key] = {field: _number(values.get(field)) for field in (*FIELDS, 'sleep_debt')}
    return result


def build_life_dashboard(plugin: Any, *, hours: int = 24,
                         now: datetime | None = None) -> dict[str, Any]:
    cfg = getattr(plugin, 'config', None)
    tz_name = str(getattr(getattr(cfg, 'schedule_advanced', None), 'timezone', '') or 'Asia/Shanghai')
    clock = TimezoneManager(tz_name)
    now = now or clock.get_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=clock.get_now().tzinfo)
    date = now.strftime('%Y-%m-%d')
    hours = hours if type(hours) is int and hours in (6, 24, 72, 168) else 24
    enabled = bool(getattr(getattr(cfg, 'schedule', None), 'enabled', False))
    life = getattr(plugin, '_life_state', None)
    model = getattr(plugin, '_life_model', None)
    db = getattr(plugin, '_db', None)
    warnings: list[str] = []

    def read(label: str, callback: Any, fallback: Any) -> Any:
        try:
            return callback()
        except Exception:
            warnings.append(label)
            return fallback

    state = read('生活状态暂不可读', life.snapshot, {}) if life is not None and enabled else {}
    safe_state = {key: _number(state.get(key)) for key in (*FIELDS, 'sleep_debt', 'mental_state')}
    for key in ('updated_at', 'last_tick_at', 'last_meal_at', 'last_break_at', 'last_sleep_at',
                'sleep_started_at', 'sleep_wake_target', 'sleep_lie_in_cap', 'last_wake_reason'):
        safe_state[key] = str(state[key])[:80] if state.get(key) is not None else None
    safe_state['sleep_interrupted'] = bool(state.get('sleep_interrupted'))
    safe_state['continuous_sleep_minutes'] = _number(state.get('continuous_sleep_minutes'))
    age = None
    future_state = False
    if safe_state['updated_at']:
        try:
            updated = datetime.fromisoformat(safe_state['updated_at'])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=now.tzinfo)
            elapsed = (now-updated).total_seconds()
            future_state = elapsed < -300
            age = max(0., elapsed)
        except (ValueError, TypeError):
            pass
    ready = enabled and model is not None
    today = read('当日活动暂不可读', lambda: model.today(now), {}) if ready else {}
    current = read('运行中活动暂不可读', lambda: db.active_episode(), None) if ready and db else None
    if not isinstance(current, dict) or current.get('status') != 'active':
        current = None
    history = read('生活历史暂不可读', lambda: life.history_snapshot(now=now, hours=hours), {'points': []}) if enabled and life else {'points': []}
    sessions: list[dict[str, Any]] = []
    if ready and db:
        for offset in range(7):
            day = (now-timedelta(days=offset)).strftime('%Y-%m-%d')
            rows = read('睡眠记录暂不可读', lambda d=day: db.query_sleep_sessions(schedule_date=d, limit=16), [])
            for row in rows:
                selected = {key: _number(row.get(key)) for key in
                            ('actual_start_minute', 'actual_end_minute', 'continuous_minutes',
                             'interruption_count', 'energy_start', 'energy_end', 'sleep_debt_before',
                             'sleep_debt_after', 'quality')}
                selected.update({key: str(row[key])[:80] if row.get(key) is not None else None for key in
                                 ('schedule_date', 'wake_reason', 'created_at', 'updated_at')})
                sessions.append(selected)
            if len(sessions) >= 8:
                break
        sessions.sort(key=lambda row: (row.get('schedule_date') or '', row.get('actual_start_minute') or 0), reverse=True)
    engine = getattr(plugin, '_sleep_engine', None)
    sleep_live = read('睡眠引擎暂不可读', lambda: engine.sleep_status(now), None) if enabled and engine else None
    safe_sleep = None
    if isinstance(sleep_live, dict):
        safe_sleep = {key: sleep_live.get(key) for key in
                      ('asleep', 'wake_reason', 'continuous_minutes', 'interruption_count',
                       'wake_at', 'lie_in_cap', 'closed_at')}
    companion = read('陪伴状态暂不可读', lambda: _companion_summary(plugin, date=date), {'available': False})
    observations = []
    if safe_state['energy'] is not None and safe_state['energy'] < .3:
        observations.append('精力偏低，留意后续休息与恢复活动。')
    if safe_state['hunger'] is not None and safe_state['hunger'] > .7:
        observations.append('饥饿较高，留意当前计划中的用餐安排。')
    if safe_state['stress'] is not None and safe_state['stress'] > .8:
        observations.append('压力较高，适合放慢节奏。')
    if safe_state['sleep_debt'] is not None and safe_state['sleep_debt'] > 2:
        observations.append('睡眠债仍在，后续完整睡眠会逐步偿还。')
    if enabled and not ready:
        warnings.append('日程运行态尚未就绪')
    result = {
        'schema_version': 1, 'generated_at': now.isoformat(), 'timezone': tz_name,
        'window_hours': hours, 'state': safe_state, 'state_available': bool(state),
        'state_age_seconds': age, 'stale': age is None or age > 900 or future_state,
        'history': history,
        'schedule': {'enabled': enabled, 'runtime_ready': ready, 'date': date,
                     'current': _row(current) if current else None,
                     'intents': [_row(row) for row in today.get('intents', [])[:80]],
                     'episodes': [_row(row) for row in today.get('episodes', [])[-120:]],
                     'summary': {key: _number(today.get('summary', {}).get(key)) for key in
                                 ('intent_count', 'episode_count', 'completed_count', 'interrupted_count', 'abandoned_count')}},
        'sleep': {'current': safe_sleep, 'sessions': sessions[:8],
                  'configured_sleep': str(getattr(getattr(cfg, 'schedule', None), 'sleep_time', '') or '')[:8],
                  'configured_wake': str(getattr(getattr(cfg, 'schedule', None), 'wake_time', '') or '')[:8]},
        'companion': companion, 'observations': observations, 'warnings': list(dict.fromkeys(warnings))[:8],
    }
    if future_state:
        result['warnings'].append('状态时间晚于当前时钟，请核对服务器时间')
    if history.get('clock_regressed'):
        result['warnings'].append('时钟回拨，历史暂缓采样以保留时间顺序')
    # Host result limit is 512 KiB. Reserve room for its envelope and JSON spacing.
    result['truncated'] = False
    while len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 420*1024:
        result['truncated'] = True
        rows = result['history'].get('points', [])
        if len(rows) > 60:
            result['history']['points'] = rows[::2] + ([rows[-1]] if rows[-1] not in rows[::2] else [])
            result['history']['downsampled'] = True
        elif len(result['schedule']['episodes']) > 10:
            result['schedule']['episodes'] = result['schedule']['episodes'][len(result['schedule']['episodes'])//2:]
        else:
            result['schedule']['intents'] = result['schedule']['intents'][:len(result['schedule']['intents'])//2]
    return result
