"""Independent video social decisions and delivery, with injected business ports.

The ledger is a synchronous, thread-safe SQLite layer. Inside async methods its
calls are offloaded with ``asyncio.to_thread`` so a contended write lock never
blocks the plugin event loop. ``record_from_result`` and ``snapshot`` stay sync:
the former is a quick watch-completion callback, the latter a rare diagnostic read.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid

from integrations.video_share_policy import BV, clean, number, watched_facts


def attempt_result(attempt: dict, *, duplicate: bool = False) -> dict:
    facts = attempt.get('facts') or {}
    bvid = attempt.get('bvid') or facts.get('bvid')
    title = facts.get('title') or attempt.get('title') or bvid
    state = attempt.get('status') or attempt.get('state') or 'uncertain'
    accepted = state == 'accepted'
    receipt = attempt.get('receipt') or {}
    if isinstance(receipt, str):
        try:
            receipt = json.loads(receipt)
        except ValueError:
            receipt = {}
    message_id = receipt.get('message_id') if isinstance(receipt, dict) else None
    if accepted:
        content = (f'已向当前聊天提交B站视频卡片：《{title}》 {bvid}。适配器已受理，不等于对方已收到。'
                   '需要谈内容时可用这个BV读取观看缓存，不必重新观看。')
    else:
        content = '尚无确定的发送成功回执，不声称已送达，不要反复调用工具补发。'
    return {
        'success': accepted, 'bvid': bvid, 'title': title, 'delivery_state': state,
        'duplicate': duplicate, 'message_id': message_id, 'stop_after_execution': False,
        'content': ('本次已处理，不重复发送。' if duplicate else '') + content,
    }


class VideoSocialService:
    def __init__(self, ledger, profiles, evaluator, *, config_provider, clock, day,
                 prepare_card, send_card, preflight, reserve, commit, release, append_context):
        self.ledger = ledger
        self.profiles = profiles
        self.evaluator = evaluator
        self._config = config_provider
        self._now = clock
        self._day = day
        self._prepare = prepare_card
        self._send = send_card
        self._gate = preflight
        self._reserve = reserve
        self._commit = commit
        self._release = release
        self._append = append_context
        self._lock = asyncio.Lock()
        self._mode = ''
        self._settled = set()

    def record_from_result(self, result: dict, episode_id: str):
        now = self._now()
        facts = watched_facts(result, episode_id, now)
        if not facts:
            return False
        return self.ledger.observe(facts, now, self._config().candidate_ttl_hours * 3600)

    def _limits(self) -> dict:
        c = self._config()
        return {k: getattr(c, k) for k in
                ('daily_limit', 'stream_daily_limit', 'min_interval_hours', 'max_targets_per_video')}

    async def synchronize_fact(self, attempt: str):
        item = await asyncio.to_thread(self.ledger.claim_history, attempt)
        if item is None:
            return
        facts = item['facts']
        text = (f"[视频分享事实:{attempt}] 本聊天已提交B站视频卡片：{facts['bvid']}《{facts['title']}》。"
                "适配器已受理，未确认实际送达或阅读。这是已发生行为的记录，不是新任务，勿再次分享同一卡片。")
        try:
            reply = await self._append(item['stream'], text, attempt)
            state = 'synced' if isinstance(reply, dict) and reply.get('success') is True else 'rejected'
        except asyncio.CancelledError:
            await asyncio.to_thread(self.ledger.finish_history, attempt, 'uncertain')
            raise
        except Exception:
            state = 'uncertain'
        await asyncio.to_thread(self.ledger.finish_history, attempt, state)

    async def _settle_accepted(self):
        # Recheck on each process start, even if a previous process thought it
        # committed. The budget's reservation API is idempotent and owns counts.
        rows = await asyncio.to_thread(self.ledger.accepted_with_tokens)
        for row in rows:
            if row['id'] in self._settled:
                continue
            self._commit(row['token'])
            self._settled.add(row['id'])

    async def tick(self) -> dict:
        async with self._lock:
            await self._settle_accepted()
            now = self._now()
            await asyncio.to_thread(self.ledger.prune, now)
            cfg = self._config()
            mode = cfg.mode
            if mode == '关闭':
                return {'status': 'disabled'}
            if mode == '自动分享' and self._mode != mode:
                await asyncio.to_thread(self.ledger.activate_shadow, now)
            self._mode = mode
            candidate = await asyncio.to_thread(self.ledger.due, now)
            if candidate is None:
                return {'status': 'no_candidate'}
            if mode == '自动分享':
                reason = await asyncio.to_thread(
                    self.ledger.capacity_reason, now, self._day(), self._limits(), bvid=candidate['bvid'])
                if reason:
                    await asyncio.to_thread(self.ledger.postpone, candidate['bvid'], reason, now, 1800)
                    return {'status': 'deferred', 'reason': reason}
            allowed, reason = self._gate(None)
            if not allowed:
                await asyncio.to_thread(self.ledger.postpone, candidate['bvid'], reason, now)
                return {'status': 'deferred', 'reason': reason}
            try:
                audiences = await self.profiles.collect(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                audiences = []
            fresh = []
            for r in audiences:
                prior = await asyncio.to_thread(
                    self.ledger.pair_receipt, candidate['bvid'], r['stream_id'], now)
                if prior is None:
                    fresh.append(r)
            audiences = fresh
            if not audiences:
                await asyncio.to_thread(
                    self.ledger.postpone, candidate['bvid'], 'no_allowed_fresh_audience', now, 1800)
                return {'status': 'deferred', 'reason': 'no_allowed_fresh_audience'}
            if candidate['status'] != 'queued':
                if not await asyncio.to_thread(self.ledger.begin_evaluation, candidate['bvid'], now):
                    return {'status': 'evaluation_busy_or_limited'}
                try:
                    decision = await self.evaluator.evaluate(candidate['facts'], audiences)
                except asyncio.CancelledError:
                    await asyncio.to_thread(
                        self.ledger.postpone, candidate['bvid'], 'evaluation_cancelled',
                        self._now(), 1800, reevaluate=True)
                    raise
                except Exception:
                    decision = {'action': 'defer', 'target_stream_id': '',
                                'delay_seconds': 1800, 'reason': 'evaluation_failed'}
                await asyncio.to_thread(
                    self.profiles.update_topics, decision.get('profile_topics') or {}, audiences)
                selected = next((r for r in audiences
                                 if r['stream_id'] == decision.get('target_stream_id')), None)
                if selected:
                    decision['audience_fingerprint'] = selected['fingerprint']
                    decision['valid_until'] = candidate['expires']
                shadow = mode != '自动分享' or self._config().mode != '自动分享'
                await asyncio.to_thread(self.ledger.decide, candidate['bvid'], decision, self._now(), shadow=shadow)
                if shadow:
                    return {'status': 'shadow', 'decision': decision}
                if decision.get('action') != 'share':
                    return {'status': decision.get('action')}
                candidate = {**candidate, 'status': 'queued', 'decision': decision}
            return await self._deliver(candidate, audiences)

    async def _deliver(self, candidate: dict, audiences: list) -> dict:
        bvid = candidate['bvid']
        decision = candidate['decision']
        now = self._now()
        selected = next((r for r in audiences if r['stream_id'] == decision.get('target_stream_id')), None)
        if (not selected or decision.get('audience_fingerprint') != selected.get('fingerprint')
                or number(decision.get('valid_until')) <= now):
            await asyncio.to_thread(self.ledger.postpone, bvid, 'topic_changed_or_decision_stale', now, 300, reevaluate=True)
            return {'status': 'deferred'}
        if selected.get('recent_rate', 0) >= 8 or now - number(selected.get('last_bot_at')) < 300:
            await asyncio.to_thread(self.ledger.postpone, bvid, 'target_conversation_busy', now)
            return {'status': 'deferred'}
        allowed, reason = self._gate(selected)
        if not allowed:
            await asyncio.to_thread(self.ledger.postpone, bvid, reason, now)
            return {'status': 'deferred', 'reason': reason}
        reason = await asyncio.to_thread(
            self.ledger.capacity_reason, now, self._day(), self._limits(), bvid=bvid, stream=selected['stream_id'])
        if reason:
            await asyncio.to_thread(self.ledger.postpone, bvid, reason, now, 1800)
            return {'status': 'deferred', 'reason': reason}
        facts = candidate['facts']
        try:
            generated = await self._prepare(bvid=bvid, title=facts['title'],
                                            desc='哔哩哔哩视频分享', pic_url=facts.get('cover_url', ''))
        except asyncio.CancelledError:
            raise
        except Exception:
            generated = {}
        if not isinstance(generated, dict) or not generated.get('success'):
            await asyncio.to_thread(
                self.ledger.decide, bvid, {'action': 'do_not_share', 'reason': 'card_generation_failed'}, self._now())
            return {'status': 'rejected', 'reason': 'card_generation_failed'}
        # All awaited work is behind us; re-read consent/routes and business
        # gates before reserving a budget or persisting the send intent.
        try:
            current = await self.profiles.routes()
        except Exception:
            current = []
        # 只比身份键：群改名/昵称变化不应误判为撤授权；账号或流变化仍会挡住。
        ident = {k: selected.get(k) for k in ('stream_id', 'account_id', 'kind')}
        route = next((r for r in current
                      if {k: r.get(k) for k in ('stream_id', 'account_id', 'kind')} == ident), None)
        if self._config().mode != '自动分享' or route is None:
            await asyncio.to_thread(self.ledger.postpone, bvid, 'consent_or_route_changed', self._now(), 300, reevaluate=True)
            return {'status': 'deferred'}
        allowed, reason = self._gate(route)
        if not allowed:
            await asyncio.to_thread(self.ledger.postpone, bvid, reason, self._now())
            return {'status': 'deferred'}
        # The transaction elects one sender BEFORE touching the shared budget;
        # losing processes must not reserve/release over another owner's slot.
        claim = await asyncio.to_thread(
            self.ledger.claim, bvid, route['stream_id'], 'auto', 'auto:' + uuid.uuid4().hex,
            self._now(), self._day(), self._limits())
        if claim['status'] != 'claimed':
            if claim['status'] == 'duplicate':
                await asyncio.to_thread(
                    self.ledger.decide, bvid, {'action': 'do_not_share', 'reason': 'already_shared_or_uncertain'}, self._now())
            else:
                await asyncio.to_thread(self.ledger.postpone, bvid, claim.get('reason', 'blocked'), self._now(), 1800)
            return claim
        attempt = claim['attempt_id']
        try:
            token = self._reserve(route)
        except Exception:
            # The adapter has not been called. A budget-storage failure is not
            # an uncertain network delivery and must not strand the candidate.
            await asyncio.to_thread(self.ledger.finish, attempt, 'rejected', reason='budget_reservation_failed_no_send')
            await asyncio.to_thread(self.ledger.decide, bvid, decision, self._now())
            await asyncio.to_thread(self.ledger.postpone, bvid, 'budget_storage_unavailable', self._now(), 1800)
            return {'status': 'deferred', 'reason': 'budget_storage_unavailable'}
        if token is None:
            await asyncio.to_thread(self.ledger.finish, attempt, 'rejected', reason='budget_unavailable_no_send')
            await asyncio.to_thread(self.ledger.decide, bvid, decision, self._now())
            await asyncio.to_thread(self.ledger.postpone, bvid, 'budget', self._now(), 1800)
            return {'status': 'deferred'}
        try:
            await asyncio.to_thread(self.ledger.token, attempt, token)
        except BaseException:
            self._release(token)
            raise  # No network send occurred; the persisted intent stays conservative.
        try:
            receipt = await self._send(route, generated['ark_json'])
        except asyncio.CancelledError:
            await asyncio.to_thread(self.ledger.finish, attempt, 'uncertain', reason='send_cancelled')
            raise
        except Exception:
            receipt = {'uncertain': True, 'error_class': 'unknown'}
        if isinstance(receipt, dict) and receipt.get('success') is True and not receipt.get('uncertain'):
            await asyncio.to_thread(self.ledger.finish, attempt, 'accepted', receipt)
            self._commit(token)
            self._settled.add(attempt)
            cfg = self._config()
            await asyncio.to_thread(
                self.ledger.reopen_for_other_targets, bvid, self._now(),
                cfg.max_targets_per_video, cfg.min_interval_hours * 3600)
            await self.synchronize_fact(attempt)
            return {'status': 'accepted', 'attempt_id': attempt}
        if isinstance(receipt, dict) and receipt.get('not_sent') is True:
            await asyncio.to_thread(self.ledger.finish, attempt, 'rejected', receipt)
            self._release(token)
            if receipt.get('error_class') == 'gate_changed':
                await asyncio.to_thread(self.ledger.decide, bvid, decision, self._now())
                await asyncio.to_thread(self.ledger.postpone, bvid, 'late_gate_changed_no_send', self._now())
            return {'status': 'rejected'}
        await asyncio.to_thread(self.ledger.finish, attempt, 'uncertain', receipt if isinstance(receipt, dict) else {})
        return {'status': 'uncertain'}

    def snapshot(self) -> dict:
        return {'mode': self._config().mode, **self.ledger.snapshot()}


class ManualVideoShares:
    """A user/natural tool's current-stream action, sharing the same receipts."""

    def __init__(self, social: VideoSocialService, *, recent_provider, clock, day, chooser=random.choice):
        self.social = social
        self._recent = recent_provider
        self._now = clock
        self._day = day
        self._choose = chooser
        self._lock = asyncio.Lock()

    async def share(self, stream_id: str, request_id: str = '', *, preferred_bvid: str = '') -> dict:
        async with self._lock:
            ledger = self.social.ledger
            routes = await self.social.profiles.routes(manual_stream=stream_id)
            if len(routes) != 1:
                return {'success': False, 'error_class': 'unknown_current_stream',
                        'content': '当前聊天路由不可核验，不发送。'}
            key = hashlib.sha256((stream_id + '|' + request_id).encode()).hexdigest() if request_id else ''
            now = self._now()
            prior = await asyncio.to_thread(ledger.request_receipt, key)
            if prior is None:
                prior = await asyncio.to_thread(ledger.recent_stream_receipt, stream_id, now)
            if prior:
                return attempt_result(prior, duplicate=True)
            seen = set()
            pool = []
            for raw in self._recent() or []:
                if not isinstance(raw, dict):
                    continue
                bvid = str(raw.get('bvid') or '')
                title = clean(raw.get('title'), 200)
                if BV.fullmatch(bvid) and title and bvid not in seen:
                    seen.add(bvid)
                    pool.append({'bvid': bvid, 'title': title})
            if preferred_bvid:
                prior = await asyncio.to_thread(ledger.pair_receipt, preferred_bvid, stream_id, now, manual=True)
                if prior:
                    return attempt_result(prior, duplicate=True)
                pool = [r for r in pool if r['bvid'] == preferred_bvid]
            else:
                filtered = []
                for r in pool:
                    if await asyncio.to_thread(ledger.pair_receipt, r['bvid'], stream_id, now, manual=True) is None:
                        filtered.append(r)
                pool = filtered
            if not pool:
                return {'success': False, 'error_class': 'no_recent_video',
                        'content': '近期观看记忆中没有这个可分享视频，不搜索、不编造。'}
            selected = self._choose(pool)
            await asyncio.to_thread(ledger.ensure_manual, selected, now)
            claim = await asyncio.to_thread(
                ledger.claim, selected['bvid'], stream_id, 'manual', key, now, self._day(), {}, preparing=True)
            if claim['status'] == 'duplicate':
                return attempt_result(claim['attempt'], duplicate=True)
            if claim['status'] != 'claimed':
                return {'success': False, 'error_class': claim.get('reason'),
                        'content': '分享防重或状态检查未放行，本次不发送。'}
            attempt = claim['attempt_id']
            try:
                generated = await self.social._prepare(**selected)
            except asyncio.CancelledError:
                await asyncio.to_thread(ledger.finish, attempt, 'rejected', reason='preparation_cancelled_no_send')
                raise
            except Exception:
                generated = {}
            if not isinstance(generated, dict) or not generated.get('success'):
                await asyncio.to_thread(ledger.finish, attempt, 'rejected', reason='card_generation_failed')
                return {'success': False, 'error_class': 'card_generation_failed', 'content': '卡片生成失败，未发送。'}
            try:
                refreshed = await self.social.profiles.routes(manual_stream=stream_id)
            except BaseException:
                await asyncio.to_thread(ledger.finish, attempt, 'rejected', reason='route_unavailable_no_send')
                raise
            if refreshed != routes or not await asyncio.to_thread(ledger.mark_sending, attempt):
                await asyncio.to_thread(ledger.finish, attempt, 'rejected', reason='route_changed_no_send')
                return {'success': False, 'content': '路由已变化，未发送。'}
            try:
                receipt = await self.social._send(routes[0], generated['ark_json'], manual=True)
            except asyncio.CancelledError:
                await asyncio.to_thread(ledger.finish, attempt, 'uncertain', reason='manual_send_cancelled')
                raise
            except Exception:
                receipt = {'uncertain': True}
            if isinstance(receipt, dict) and receipt.get('success') is True and not receipt.get('uncertain'):
                state = 'accepted'
            elif isinstance(receipt, dict) and receipt.get('not_sent') is True:
                state = 'rejected'
            else:
                state = 'uncertain'
            await asyncio.to_thread(ledger.finish, attempt, state, receipt if isinstance(receipt, dict) else {})
            if state == 'accepted':
                await self.social.synchronize_fact(attempt)
            return attempt_result({'status': state, 'facts': selected})