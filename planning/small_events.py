"""Code-registered small events; no simulated conversations or contacts.

P2（2026-11-09 滚动日程计划 §6.1）：代码初始事件只保留 watch_bili/browse_qq
两项**模块联动**事件——只有装配层登记的这两项能触发对应模块能力。原三项纯状态
事件（daydream/lie_down/snack）迁到可编辑 JSON（mappings/initial_events/，
event_id 保持以延续去重与历史语义）；LEGACY_STATE_EVENTS 仅作旧 episode 无快照
时的增量回查档案，不再是活跃池成员。
"""
from __future__ import annotations
from copy import deepcopy

from .event_slots import TIME_SLOTS

BUILTIN_EVENTS = (
    {'event_id': 'watch_bili', 'name': '刷视频', 'min_minutes': 10, 'max_minutes': 25,
     'interruptibility': .95, 'goal_type': 'entertainment',
     'state_deltas': {}, 'templates': ('翻翻感兴趣的视频', '找个感兴趣的视频看看')},
    {'event_id': 'browse_qq', 'name': '刷QQ', 'min_minutes': 5, 'max_minutes': 15,
     'interruptibility': .95, 'goal_type': 'social',
     'state_deltas': {}, 'templates': ('闲下来刷一会儿QQ', '随手打开QQ看看')},
)

#: 历史纯状态内置项档案（P2 迁 JSON 前的原定义）：仅供旧 episode 无 metadata
#: 快照时回查增量（_random_event_deltas / resolve_state_deltas 的兼容链），
#: 不参与选取、不进活跃池、不改写历史。
LEGACY_STATE_EVENTS = (
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
        # 内置项同样受时段过滤（非后门）；联动两项均为安静室内活动，默认覆盖三段，
        # 可在 BUILTIN_EVENTS 显式写 allowed_slots 收窄。
        event.setdefault('allowed_slots', list(TIME_SLOTS))
    return result
