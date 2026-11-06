"""An actual browsing-QQ episode may offer one optional social opportunity."""
from __future__ import annotations
from typing import Any
import random

BROWSE_CONTACT_PROBABILITY = .20


class SmallEventActions:
    def __init__(self, mark: Any, dispatch: Any, *, draw: Any = None) -> None:
        self._mark, self._dispatch = mark, dispatch
        self._draw = draw or random.random

    async def tick(self, episode: Any) -> dict:
        if not isinstance(episode, dict) or episode.get('status') not in (None, 'active', 'running'):
            return {'status': 'not_active'}
        meta = episode.get('metadata') or {}
        if meta.get('event_key') != 'browse_qq':
            return {'status': 'not_browsing_qq'}
        eid = str(episode.get('episode_id') or '')
        if not eid or meta.get('browse_contact_attempt'):
            return {'status': 'duplicate'}
        chosen = self._draw() < BROWSE_CONTACT_PROBABILITY
        # Persist even a failed draw; never reroll each life tick or after a reload.
        status = 'reserved' if chosen else 'not_selected'
        if self._mark(eid, {'browse_contact_attempt': status}) is False:
            return {'status': 'persist_failed'}
        if not chosen:
            return {'status': status}
        result = await self._dispatch('browse_qq:' + eid, 'optional_contact',
            '【可选动机·刷QQ】你正在闲下来刷QQ，可能想随口联系一下陪伴目标。'
            '没有新消息、没有指定话题，也不要求问候、询问、汇报日程或表达想念。'
            '结合已有上下文自行决定；没什么想说就不调用任何工具，直接结束。')
        self._mark(eid, {'browse_contact_attempt': str(result.get('status') or 'unknown')})
        return result
