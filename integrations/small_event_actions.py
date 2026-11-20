"""An actual browsing-QQ episode may offer one optional social opportunity."""
from __future__ import annotations
from typing import Any
import random
from core.activity_attributes import activity_attributes_for_episode, activity_module_guard

BROWSE_CONTACT_PROBABILITY = .20


class SmallEventActions:
    def __init__(self, mark: Any, dispatch: Any, *, draw: Any = None) -> None:
        self._mark, self._dispatch = mark, dispatch
        self._draw = draw or random.random

    async def tick(self, episode: Any) -> dict:
        if not isinstance(episode, dict) or episode.get('status') not in (None, 'active', 'running'):
            return {'status': 'not_active'}
        meta = episode.get('metadata') or {}
        if 'browse_qq' not in (activity_attributes_for_episode(episode) or ()):
            return {'status': 'not_browsing_qq'}
        blocked = activity_module_guard(episode)
        if blocked:
            return {'status': 'blocked', 'reason': blocked}
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
            "你正顺手翻着手机，想起可以给对方随口发句话。"
            "没有必须说的内容，也没必要报备日程；真有想聊的就按平时口吻说一句，"
            "没什么想说就直接安静结束。")
        self._mark(eid, {'browse_contact_attempt': str(result.get('status') or 'unknown')})
        return result
