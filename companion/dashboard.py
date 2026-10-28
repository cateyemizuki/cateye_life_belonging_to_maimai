"""A detached, read-only companion summary without chat content or identifiers."""
from __future__ import annotations

from typing import Any


def companion_summary(plugin: Any, *, date: str) -> dict[str, Any]:
    service = getattr(plugin, '_companion', None)
    if service is None:
        return {'available': False}
    affection = service.affection
    today = affection.today_date() == date
    patrol = service.patrol.patrol_status()
    config = getattr(plugin.config, 'patrol', None)
    return {
        'available': True,
        'score': affection.score(), 'baseline': affection.baseline(),
        'tier': affection.tier(), 'tier_label': affection.tier_label()[:80],
        'today_valid': today,
        'speak_count': affection.today_speak_count() if today else 0.,
        'daily_limit': getattr(config, 'daily_max_speak', None),
        'morning_sent': affection.morning_sent() if today else False,
        'night_sent': affection.night_sent() if today else False,
        'patrol_running': bool(patrol.get('running')),
        'target_ready': bool(patrol.get('stream_id_ready')),
        'last_tick_at': patrol.get('last_tick_at', ''),
        'trigger_enabled': bool(patrol.get('trigger_enabled')),
    }
