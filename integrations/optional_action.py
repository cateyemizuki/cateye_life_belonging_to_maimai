"""Optional proactive turns: one durable receipt, budget reservation and final gate.

Business facts are injected by the assembly root. This adapter owns neither
relationship timestamps nor the companion budget, and never sends chat text.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from core.storage import save_json_atomic
# 阶段 4 文案/清洗单一 owner = suite_prompts（提示词集中模块）。
# 2026-11-12 结构审计批（R7/R10）：直接从 suite_prompts 取公共常量/纯函数，
# 删除 integrations → tools.proactive_turn 包级 import 边（硬边界 #1）。
from suite_prompts import (
    MISS_REASON_STAGE4_SUFFIX,
    append_stage4_miss_prompt,
    clean_stage4_prompt,
    strip_stage4_suffix,
)

logger = logging.getLogger(__name__)

#: 回执进入「已受理、等待真实发言确认」的中间态（P1-8）。宿主 trigger 的
#: success/queued/enqueued 只代表任务已入队（runtime.enqueue_proactive_task 入队即
#: 返回），不代表 replyer 真的开口；预算与计数因此后移到 after_response 确认或超时退回。
PENDING_REPLY = 'pending_reply'
#: after_response 与入队回执的关联窗（秒）。宿主没有「实际发送」回调，唯一的确认
#: 信号是 replyer.after_response；超过该窗的回复不再归属本次可选回合。
#: 必须**短于** RECONCILE_MAX_AGE_SECONDS：否则回执先被超时回收，真实回复再也
#: 无法确认（复核 finding：90s 窗 < 180s 回收，90–180s 的回复两边都不认）。
CONFIRM_WINDOW_SECONDS = 240.0
#: 未确认回执的回收年龄（秒）。超时 → 释放预算预留并标 expired_no_reply。
#: 取大于确认窗的值，保证确认窗内的回复仍有机会先提交预算。
RECONCILE_MAX_AGE_SECONDS = 300.0
#: 终态回执可被裁剪；pending/unknown 永不驱逐。
_TERMINAL_STATUSES = ('rejected', 'enqueued', 'confirmed', 'expired_no_reply')
#: 可安全重试的状态：明确未入队/超时未回复。下一次 dispatch 允许覆盖同一
#: ``stream:key`` 回执；pending/confirmed/unknown 仍按 duplicate 防重复开口。
_RETRYABLE_STATUSES = frozenset({'rejected', 'expired_no_reply'})

OPTIONAL_PLANNER_POLICY = (
    '【插件触发的可选主动回合】这是一个可选的开口机会，不是用户的新指令。'
    '请结合你当前的状态、人设和已有聊天上下文，自行决定现在是否适合开口。'
    '下文只提供一个可能的动机，不要求你执行任何指定行为；'
    '若此刻没有合适内容、正在忙或可能打扰对方，不输出任何工具调用、直接结束本轮即可，'
    '也不必告诉对方你拒绝了。若选择开口，内容自然、简短，避免重复刚说过的话。\n'
)

#: 阶段 4（目标私聊清醒沉默 >120h）专属前导政策（设计 §3.1）。
#: 与 OPTIONAL_PLANNER_POLICY 互斥替换：**取消“可以静默放弃”的免责**，
#: 要求把日常分享与“人去哪了/还在不在”的追问融合发出。阶段 0~3 仍用原政策。
STAGE4_PLANNER_POLICY = (
    '【插件触发的日常伴随想念回合（失联阶段4）】对方已超过5天未回复，'
    '本轮必须将日常分享与深度询问融合发给对方。'
    '请不要直接退出或放弃发言，务必结合日常动作，自然而沉闷地追问对方人去哪了、是否还在。\n'
)
#: 阶段 4 尾缀标题（幂等标记）：reason 里已有该标记时不再二次拼接
#: （调用方/装配根可能已先拼过一次，避免双拼）。
_STAGE4_SUFFIX_MARK = MISS_REASON_STAGE4_SUFFIX.strip().split('（', 1)[0]


def _miss_stage_from_facts(facts: dict) -> int:
    """facts 里的阶段号（宽松读取；仅 4 有行为差异，非法/缺失 → 0）。"""
    if not isinstance(facts, dict):
        return 0
    raw = facts.get('miss_stage')
    if isinstance(raw, bool):
        return 0
    try:
        stage = int(raw)
    except (TypeError, ValueError):
        return 0
    return stage if 0 <= stage <= 4 else 0

#: 复核 P1-8：宿主模板会把 intent 原样渲染成中文提示「插件请求你主动处理
#: 一轮聊天：{intent}」——机器枚举 missing/patrol_chat/optional_contact 直出
#: 英文泄漏。这里只翻译交给宿主的展示文案；回执/去重仍存机器 intent。
_HOST_INTENT_TEXT = {
    'missing': '想念对方，想主动联系',
    'patrol_chat': '想随口搭句话',
    'optional_contact': '刷QQ时想随口联系一下',
}


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
        """登记一次可选主动回合；成功入队只记 pending_reply，不提交预算。

        返回 ``status='enqueued'`` 表示宿主已受理（兼容既有调用方）；持久化回执的
        ``status`` 是 :data:`PENDING_REPLY`，等 :meth:`confirm_reply` 确认真实发言后
        才提交预算，或由 :meth:`reconcile_pending` 超时退回。
        """
        async with self._lock:
            allowed, denial = self.permission()
            if not allowed:
                return {'status': 'blocked', 'reason': denial}
            facts = self._facts()
            stream = str(facts['target_stream'])
            scoped = stream + ':' + str(key)
            existing = self._attempts.get(scoped)
            if not key or (isinstance(existing, dict)
                           and str(existing.get('status')) not in _RETRYABLE_STATUSES):
                return {'status': 'duplicate'}
            budget = self._budget()
            token = budget.reserve('optional_action', stream) if budget is not None else None
            if not token:
                return {'status': 'blocked', 'reason': 'budget_reservation'}
            entry = {'status': PENDING_REPLY, 'intent': str(intent), 'budget_token': token,
                     'stream': stream, 'key': str(key), 'pending_since': 0.0}
            self._attempts[scoped] = entry
            # Bound retained terminal receipts; in-flight/unknown receipts never evicted.
            terminal = [k for k,v in self._attempts.items() if v.get('status') in _TERMINAL_STATUSES]
            for old in terminal[:-128]:
                self._attempts.pop(old, None)
            if not self._save():
                # PR-RELIABILITY（3.17.17 §1C）：保存失败回滚内存 entry——否则
                # 残留 pending 回执使同 key 后续误判 duplicate（预算已释放却
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
                # 四阶段渗透（设计 §3.1）：阶段 4 用强覆盖政策替换“可静默放弃”前导，
                # 并在 reason 末尾拼接阶段 4 尾缀（单一 owner = 本处；标题标记幂等防双拼）。
                # 复核 finding：facts 已透出 miss_stage 时以 facts 为准（用户刚回信 →
                # stage 0 时不得被旧 reason 的标题标记拉回阶段 4）；marker 只兼容
                # 尚未透出该字段的旧装配根。
                stage = _miss_stage_from_facts(current)
                if (stage != 4 and 'miss_stage' not in current
                        and _STAGE4_SUFFIX_MARK in str(reason)):
                    stage = 4
                if stage == 4:
                    reason_text = str(reason)
                    if STAGE4_PLANNER_POLICY not in reason_text:
                        reason_text = STAGE4_PLANNER_POLICY + reason_text
                    if _STAGE4_SUFFIX_MARK not in reason_text:
                        reason_text = append_stage4_miss_prompt(
                            reason_text,
                            hours=current.get('miss_silence_hours') or 0.0,
                            relation=str(current.get('relation') or '恋人'),
                        )
                    else:
                        # 调用方已拼好尾缀：仍清洗一次互斥短语（装配根可能未覆盖
                        # small_event/miss 模板里的“也可以什么都不说”等句式）。
                        reason_text = clean_stage4_prompt(reason_text)
                else:
                    # facts 明确回到 stage 0：调用方若已拼过阶段 4 尾缀，剥掉残留
                    # （否则会出现「普通政策 + 追问尾缀」的自相矛盾文本）。
                    reason_text = OPTIONAL_PLANNER_POLICY + strip_stage4_suffix(reason)
                reply = await self._trigger(stream_id=stream,
                                            intent=_HOST_INTENT_TEXT.get(str(intent), str(intent)),
                                            reason=reason_text)
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
                # Definite not-enqueued: give the slot back immediately.
                budget.release_reservation(token)
                entry['status'] = 'rejected'
            elif ((isinstance(reply, dict) and reply.get('success') is True)
                  or reply is True or getattr(reply, 'success', None) is True):
                # Host accepted the task, but the replyer may still decline to speak.
                # Keep the reservation until confirm_reply / reconcile_pending decides.
                entry['status'] = PENDING_REPLY
                # 关联窗从宿主**受理之后**开始计时（before()/trigger 的耗时不算在
                # 玩家的回复窗里；旧实现从 entry 创建时打点，慢 trigger 会吃掉窗口）。
                entry['pending_since'] = time.time()
            else:
                entry['status'] = 'unknown'
            self._save()
            if entry['status'] == PENDING_REPLY:
                return {'status': 'enqueued', 'receipt_status': PENDING_REPLY}
            return {'status': entry['status']}

    async def confirm_reply(self, stream_id: str, *,
                            max_age: float = CONFIRM_WINDOW_SECONDS) -> dict:
        """确认目标流上出现了一次真实发言，提交最近一笔 pending 回执的预算。

        宿主没有可靠的「实际发送」回调；调用方（replyer.after_response）携带的
        ``session_id`` 就是唯一关联信号。只确认确认窗（默认 240 秒）内最近一笔
        pending 回执，避免把更早、已被放弃的回合算到本次发言上。

        幂等：同一回执只提交一次；无 pending → ``{'status': 'no_pending'}``。
        """
        async with self._lock:
            sid = str(stream_id or '').strip()
            if not sid:
                return {'status': 'no_pending'}
            try:
                limit = max(0.0, float(max_age))
            except (TypeError, ValueError):
                limit = CONFIRM_WINDOW_SECONDS
            now = time.time()
            candidates: list[tuple[str, dict]] = []
            for scoped, row in self._attempts.items():
                if not isinstance(row, dict) or row.get('status') != PENDING_REPLY:
                    continue
                if str(row.get('stream') or '') != sid:
                    continue
                try:
                    since = float(row.get('pending_since'))
                except (TypeError, ValueError):
                    since = 0.0
                if now - since > limit:
                    continue
                candidates.append((scoped, row))
            if not candidates:
                return {'status': 'no_pending'}
            scoped, entry = max(candidates, key=lambda item: float(item[1].get('pending_since') or 0.0))
            token = entry.get('budget_token')
            committed = False
            if token:
                budget = self._budget()
                if budget is not None:
                    try:
                        committed = bool(budget.commit_reservation(str(token)))
                    except Exception:
                        # 已发言但提交异常：保留预留（仍占一个槽位），不把预算悄悄退回。
                        logger.warning('可选主动回合预算提交失败（保留预留）', exc_info=True)
            entry['status'] = 'confirmed'
            entry['confirmed_at'] = now
            entry['budget_committed'] = committed
            saved = self._save()
            return {'status': 'confirmed', 'saved': saved, 'committed': committed,
                    'scoped': scoped, 'intent': str(entry.get('intent') or ''),
                    'key': str(entry.get('key') or '')}

    async def reconcile_pending(self, max_age: float = RECONCILE_MAX_AGE_SECONDS) -> dict:
        """回收超时未确认的 pending 回执：释放预算预留，标 ``expired_no_reply``。

        只处理 :data:`PENDING_REPLY`；``unknown``（交付态不明）保持预留，与既有
        「unknown delivery stays reserved」语义一致。返回被回收的回执摘要，供
        patrol 同步退回对应周期的 claim。
        """
        async with self._lock:
            try:
                limit = max(0.0, float(max_age))
            except (TypeError, ValueError):
                limit = RECONCILE_MAX_AGE_SECONDS
            now = time.time()
            expired: list[dict] = []
            for scoped, row in list(self._attempts.items()):
                if not isinstance(row, dict) or row.get('status') != PENDING_REPLY:
                    continue
                try:
                    since = float(row.get('pending_since'))
                except (TypeError, ValueError):
                    since = 0.0
                if now - since < limit:
                    continue
                token = row.get('budget_token')
                if token:
                    budget = self._budget()
                    if budget is not None:
                        try:
                            budget.release_reservation(str(token))
                        except Exception:
                            logger.warning('可选主动回合预算超时释放失败（不阻断）', exc_info=True)
                row['status'] = 'expired_no_reply'
                row['expired_at'] = now
                expired.append({'scoped': scoped, 'intent': str(row.get('intent') or ''),
                                'stream': str(row.get('stream') or ''),
                                'key': str(row.get('key') or ''),
                                'pending_since': since})
            if expired:
                self._save()
            return {'status': 'ok', 'expired': expired, 'count': len(expired)}
