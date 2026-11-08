"""Optional proactive turns: one durable receipt, budget reservation and final gate.

Business facts are injected by the assembly root. This adapter owns neither
relationship timestamps nor the companion budget, and never sends chat text.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from core.storage import save_json_atomic

logger = logging.getLogger(__name__)
OPTIONAL_PLANNER_POLICY = (
    '【插件触发的可选主动回合】这不是用户的新指令。'
    '请根据你当前的状态、人设和已有聊天上下文，自行决定现在是否适合行动。'
    '下文只提供一个可能的动机，不要求你执行任何指定行为。'
    '若不想执行、没有合适内容、正在忙或可能打扰对方，请不输出任何工具调用，直接结束本回合；'
    '也不必告诉对方你拒绝了任务。若选择交流，内容自然、简短，避免重复刚说过的话。\n'
)


def action_permission(facts: dict) -> tuple[bool, str]:
    """Read-only shared policy; three-minute busy facts are provided upstream."""
    if not facts.get('enabled') or not facts.get('target_stream'):
        return False, 'disabled_or_no_target'
    if facts.get('asleep'):
        return False, 'asleep'
    if facts.get('silent'):
        return False, 'quiet_window'
    if facts.get('coverage') != 'ok' or facts.get('busy'):
        return False, 'conversation_busy_or_unknown'
    if facts.get('recent_outbound'):
        return False, 'recent_outbound_collision'
    if facts.get('budget_remaining') is None or facts['budget_remaining'] <= 0:
        return False, 'budget'
    return True, 'allowed'


class OptionalActionCoordinator:
    def __init__(self, data_dir: Any, facts: Callable[[], dict], budget: Callable[[], Any],
                 trigger: Callable[..., Any]) -> None:
        self._path = Path(data_dir) / 'optional_action_receipts.json'
        self._facts, self._budget, self._trigger = facts, budget, trigger
        self._lock = asyncio.Lock()
        self._corrupt = False
        try:
            doc = json.loads(self._path.read_text(encoding='utf-8')) if self._path.exists() else {}
            if (not isinstance(doc, dict) or type(doc.get('version', 1)) is not int
                    or doc.get('version', 1) != 1 or not isinstance(doc.get('attempts', {}), dict)):
                raise ValueError('bad optional action receipts')
            self._attempts = doc.get('attempts', {})
            if any(not isinstance(row, dict) for row in self._attempts.values()):
                raise ValueError('bad optional action receipt row')
        except (OSError, ValueError):
            self._attempts = {}
            self._corrupt = True

    def permission(self) -> tuple[bool, str]:
        if self._corrupt:
            return False, 'receipt_storage_corrupt'
        try:
            return action_permission(self._facts())
        except Exception:
            return False, 'facts_unavailable'

    def _save(self) -> bool:
        try:
            return save_json_atomic(self._path, {'version': 1, 'attempts': self._attempts}) is not False
        except Exception:
            logger.warning('可选主动回合回执保存失败，取消执行', exc_info=True)
            return False

    async def dispatch(self, key: str, intent: str, reason: str, *, before: Any = None) -> dict:
        async with self._lock:
            allowed, denial = self.permission()
            if not allowed:
                return {'status': 'blocked', 'reason': denial}
            facts = self._facts()
            stream = str(facts['target_stream'])
            scoped = stream + ':' + str(key)
            if not key or scoped in self._attempts:
                return {'status': 'duplicate'}
            budget = self._budget()
            token = budget.reserve('optional_action', stream) if budget is not None else None
            if not token:
                return {'status': 'blocked', 'reason': 'budget_reservation'}
            entry = {'status': 'reserved', 'intent': str(intent), 'budget_token': token}
            self._attempts[scoped] = entry
            # Bound retained terminal receipts; in-flight/unknown receipts never evicted.
            terminal = [k for k,v in self._attempts.items() if v.get('status') in ('rejected', 'enqueued')]
            for old in terminal[:-128]:
                self._attempts.pop(old, None)
            if not self._save():
                # PR-RELIABILITY（3.17.17 §1C）：保存失败回滚内存 entry——否则
                # 残留 reserved 回执使同 key 后续误判 duplicate（预算已释放却
                # 再也发不出）。预算释放失败留 warning 不阻断（三态收敛）。
                self._attempts.pop(scoped, None)
                try:
                    budget.release_reservation(token)
                except Exception:
                    logger.warning('可选主动回合预算释放失败（不阻断）', exc_info=True)
                return {'status': 'blocked', 'reason': 'receipt_persist_failed'}
            try:
                if before is not None:
                    await before()
                # Awaited screen/context work can race a new user message or a target change.
                current = self._facts()
                # The reservation already consumed one available slot; all other gates recheck.
                current = dict(current, budget_remaining=1)
                if current.get('target_stream') != stream or not action_permission(current)[0]:
                    budget.release_reservation(token)
                    entry['status'] = 'rejected'
                    self._save()
                    return {'status': 'rejected', 'reason': 'facts_changed'}
                reply = await self._trigger(stream_id=stream, intent=intent,
                                            reason=OPTIONAL_PLANNER_POLICY + str(reason))
            except asyncio.CancelledError:
                entry['status'] = 'unknown'
                self._save()
                raise
            except Exception:
                entry['status'] = 'unknown'
                self._save()
                return {'status': 'unknown'}
            if isinstance(reply, dict) and (reply.get('success') is False
                    or reply.get('enqueued') is False or reply.get('queued') is False):
                budget.release_reservation(token)
                entry['status'] = 'rejected'
            elif ((isinstance(reply, dict) and reply.get('success') is True)
                  or reply is True or getattr(reply, 'success', None) is True):
                budget.commit_reservation(token)
                entry['status'] = 'enqueued'
            else:
                entry['status'] = 'unknown'
            self._save()
            return {'status': entry['status']}
