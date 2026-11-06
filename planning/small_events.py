"""Single builtin small-event catalog; no simulated conversations or contacts."""
from __future__ import annotations
from copy import deepcopy

BUILTIN_EVENTS = (
    {'event_id': 'watch_bili', 'name': '刷视频', 'min_minutes': 10, 'max_minutes': 25,
     'interruptibility': .95, 'goal_type': 'entertainment',
     'state_deltas': {}, 'templates': ('翻翻感兴趣的视频', '找个感兴趣的视频看看')},
    {'event_id': 'browse_qq', 'name': '刷QQ', 'min_minutes': 5, 'max_minutes': 15,
     'interruptibility': .95, 'goal_type': 'social',
     'state_deltas': {}, 'templates': ('闲下来刷一会儿QQ', '随手打开QQ看看')},
    {'event_id': 'daydream', 'name': '发呆', 'min_minutes': 5, 'max_minutes': 10,
     'interruptibility': .98, 'goal_type': 'rest',
     'state_deltas': {}, 'templates': ('放空一会儿', '坐着发会儿呆')},
    {'event_id': 'lie_down', 'name': '躺一会', 'min_minutes': 10, 'max_minutes': 20,
     'interruptibility': .95, 'goal_type': 'rest',
     'state_deltas': {}, 'templates': ('找个舒服的地方躺一会儿', '躺下来歇一小会儿')},
    {'event_id': 'snack', 'name': '吃点小零食', 'min_minutes': 5, 'max_minutes': 10,
     'interruptibility': .95, 'goal_type': 'entertainment',
     'state_deltas': {'hunger': -.05}, 'templates': ('拿点小零食慢慢吃', '吃两口零食解解馋')},
)


def builtin_events() -> list[dict]:
    result = deepcopy(list(BUILTIN_EVENTS))
    for event in result:
        event.update(key=event['event_id'], weight=1., enabled=True, source='builtin', pool_kind='builtin')
        event['templates'] = list(event['templates'])
    return result
