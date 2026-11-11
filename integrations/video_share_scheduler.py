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
import math
import random
import uuid

from integrations.video_share_policy import BV, clean, number, watched_facts

#: 陪伴目标分享机会倍率上限（配置 >1 时生效；1.0 = 现状）
TARGET_SHARE_BOOST_MAX = 3.0


def target_share_multiplier(probability, *, is_target: bool) -> float:
    """把配置值解释为「目标私聊分享机会倍率」：≤1 = 不过滤/过滤，>1 = 放宽频控。"""
    if not is_target:
        return 1.0
    try:
        value = float(probability)
    except (TypeError, ValueError):
        return 1.0
    if value <= 1.0:
        return 1.0
    return max(1.0, min(TARGET_SHARE_BOOST_MAX, value))


def boosted_limits(limits: dict, multiplier: float) -> dict:
    """按倍率放宽目标私聊的日限/流限并缩短最小间隔；其它会话拿到原值。"""
    if multiplier <= 1.0:
        return dict(limits)
    out = dict(limits)
    for key in ('daily_limit', 'stream_daily_limit'):
        if key in out:
            try:
                out[key] = max(1, int(math.ceil(float(out[key]) * multiplier)))
            except (TypeError, ValueError):
                pass
    if 'min_interval_hours' in out:
        try:
            out['min_interval_hours'] = max(0.25, float(out['min_interval_hours']) / multiplier)
        except (TypeError, ValueError):
            pass
    return out


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
                 prepare_card, send_card, preflight, reserve, commit, release, append_context,
                 probability_provider=None, roll=None, stage_provider=None, after_deliver=None):
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
        # F2（3.17.33 复核修复）：陪伴目标私聊的额外分享概率。
        # None / 不可调用 → 不额外过滤（旧行为）；1.0 也等价于不过滤。
        self._probability = probability_provider
        self._roll = roll or random.random
        # 2026-11-11 阶段 4 视频通道（设计方案 §3.3）：业务端口由装配根注入，
        # integrations 不反向依赖 plugin——
        # ``stage_provider`` 只读返回目标当前想念阶段（0..4）；
        # ``after_deliver`` 在卡片 accepted 且事实同步成功后、仅阶段 4 调用一次。
        self._stage = stage_provider
        self._after_deliver = after_deliver
        self._after_delivered = set()
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
        """把已受理卡片写入聊天上下文；返回最终 history 状态（未领取 → None）。

        阶段 4 说话轮只在 ``'synced'`` 后触发（事实先于说话进入上下文）。
        """
        item = await asyncio.to_thread(self.ledger.claim_history, attempt)
        if item is None:
            return None
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
        return state

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

    def _is_companion_route(self, route: dict) -> bool:
        """该路由是否为陪伴目标私聊（只对目标私聊应用额外概率闸）。"""
        try:
            target = self.profiles.companion_target()
        except Exception:
            return False
        if not isinstance(target, dict):
            return False
        return (str(route.get('kind') or '') == 'private'
                and str(route.get('stream_id') or '') == str(target.get('stream_id') or '')
                and str(route.get('user_id') or '') == str(target.get('user_id') or ''))

    def _target_multiplier(self, route: dict) -> float:
        """目标私聊的分享机会倍率；非目标/读取失败/≤1 → 1.0（现状）。"""
        if self._probability is None or not self._is_companion_route(route):
            return 1.0
        try:
            return target_share_multiplier(self._probability(), is_target=True)
        except Exception:
            return 1.0

    def _probability_rejects(self, route: dict) -> bool:
        """额外概率闸：仅在陪伴目标私聊、且配置概率 < 1.0 时掷一次。

        >1 不在这条闸里过滤，而是由 :meth:`_target_multiplier` 放宽频控。
        """
        if self._probability is None or not self._is_companion_route(route):
            return False
        try:
            probability = float(self._probability())
        except Exception:
            return False  # 读取失败按旧行为（不额外过滤）
        if probability >= 1.0:
            return False
        probability = max(0.0, probability)
        try:
            return float(self._roll()) >= probability
        except Exception:
            return False

    async def _deliver(self, candidate: dict, audiences: list) -> dict:
        bvid = candidate['bvid']
        decision = candidate['decision']
        now = self._now()
        selected = next((r for r in audiences if r['stream_id'] == decision.get('target_stream_id')), None)
        if (not selected or decision.get('audience_fingerprint') != selected.get('fingerprint')
                or number(decision.get('valid_until')) <= now):
            await asyncio.to_thread(self.ledger.postpone, bvid, 'topic_changed_or_decision_stale', now, 300, reevaluate=True)
            return {'status': 'deferred'}
        multiplier = self._target_multiplier(selected)
        rate_cap = max(1, int(math.ceil(8 * multiplier)))
        busy_seconds = max(120.0, 300.0 / multiplier)
        if selected.get('recent_rate', 0) >= rate_cap or now - number(selected.get('last_bot_at')) < busy_seconds:
            await asyncio.to_thread(self.ledger.postpone, bvid, 'target_conversation_busy', now)
            return {'status': 'deferred'}
        allowed, reason = self._gate(selected)
        if not allowed:
            await asyncio.to_thread(self.ledger.postpone, bvid, reason, now)
            return {'status': 'deferred', 'reason': reason}
        reason = await asyncio.to_thread(
            self.ledger.capacity_reason, now, self._day(),
            boosted_limits(self._limits(), multiplier), bvid=bvid, stream=selected['stream_id'])
        if reason:
            await asyncio.to_thread(self.ledger.postpone, bvid, reason, now, 1800)
            return {'status': 'deferred', 'reason': reason}
        # F2（3.17.33 复核修复）：既有授权/夜间/冷却/预算闸之后、卡片生成之前，再对
        # 陪伴目标私聊掷一次额外概率；未命中按终态跳过，后续候选仍可正常评估。
        if self._probability_rejects(selected):
            await asyncio.to_thread(
                self.ledger.decide, bvid,
                {'action': 'do_not_share', 'reason': 'target_probability_gate'}, self._now())
            return {'status': 'rejected', 'reason': 'target_probability_gate'}
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
            history = await self.synchronize_fact(attempt)
            # 阶段 4 视频通道：事实写入成功后，陪伴目标私聊额外触发一次说话轮。
            # 回调异常/失败绝不改变已 accepted 的卡片回执；非目标路由/非阶段 4 不调用。
            await self._after_deliver_once(attempt, route, facts, history)
            return {'status': 'accepted', 'attempt_id': attempt,
                    'route': dict(route), 'facts': dict(facts)}
        if isinstance(receipt, dict) and receipt.get('not_sent') is True:
            await asyncio.to_thread(self.ledger.finish, attempt, 'rejected', receipt)
            self._release(token)
            if receipt.get('error_class') == 'gate_changed':
                await asyncio.to_thread(self.ledger.decide, bvid, decision, self._now())
                await asyncio.to_thread(self.ledger.postpone, bvid, 'late_gate_changed_no_send', self._now())
            return {'status': 'rejected'}
        await asyncio.to_thread(self.ledger.finish, attempt, 'uncertain', receipt if isinstance(receipt, dict) else {})
        return {'status': 'uncertain'}

    async def _after_deliver_once(self, attempt: str, route: dict, facts: dict,
                                  history: str | None) -> None:
        """阶段 4 说话轮的唯一投递后钩子（at-most-once，进程内幂等）。

        只在「卡片 accepted + 陪伴目标私聊 + 事实已写入上下文」后调用；
        失败/异常只留痕，不改变卡片已受理的语义。装配根注入的回调必须快速返回
        （插件侧用 ``create_task`` 承接长耗时 planner 触发，避免占用分享调度锁）。
        """
        if self._after_deliver is None or not callable(self._after_deliver):
            return
        if history != 'synced' or not self._is_companion_route(route):
            return
        if attempt in self._after_delivered:
            return
        stage = 0
        if self._stage is not None:
            try:
                stage = int(self._stage() or 0)
            except Exception:
                stage = 0
        if stage != 4:
            return
        self._after_delivered.add(attempt)
        try:
            await self._after_deliver(attempt_id=attempt, route=dict(route),
                                      facts=dict(facts or {}), stage=4)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 说话轮失败不影响卡片事实与预算回执（调用方仍需拿到 accepted）。
            pass

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

    async def share(self, stream_id: str, request_id: str = '', *, video: str = '',
                    to_companion: bool = False) -> dict:
        """两参数手动分享：①video=BV号或视频链接（解析真实BV）；②to_companion。

        to_companion=true → 转发陪伴目标私聊（配置取流并核验身份）；false/不传 →
        发送到当前聊天（工具调用所在流）。目标不由 bot 指定任意 stream_id。防重/
        回执/账号核验/未知不补发等安全边界与自动分享共用同一台账，保持不变。
        """
        async with self._lock:
            ledger = self.social.ledger
            # 参数①：工具层已把 BV/链接/短链解析为真实BV（复用 video_watch.bv）；
            # 此处只接受形态合法的BV，非法/未解析诚实不发送（integrations 不反向依赖 video_watch）。
            bvid = str(video or '').strip()
            if not BV.fullmatch(bvid):
                return {'success': False, 'error_class': 'invalid_video',
                        'content': '未解析到真实BV号（仅支持B站视频BV/页面链接/b23.tv短链/分享文字），不搜索、不编造、不发送。'}
            # 参数②：陪伴目标私聊或当前聊天；去掉任意指定聊天流参数。
            target = self.social.profiles.companion_target() if to_companion else {}
            send_stream = (target.get('stream_id') or '') if to_companion else str(stream_id or '')
            if to_companion and not send_stream:
                return {'success': False, 'error_class': 'companion_unavailable',
                        'content': '陪伴目标私聊不可核验，不发送。'}
            routes = await self.social.profiles.routes(manual_stream=send_stream)
            if len(routes) != 1:
                return {'success': False, 'error_class': 'unknown_current_stream',
                        'content': '目标聊天路由不可核验，不发送。'}
            if to_companion and (routes[0].get('kind') != 'private'
                                 or routes[0].get('user_id') != target.get('user_id')):
                return {'success': False, 'error_class': 'companion_route_mismatch',
                        'content': '陪伴目标私聊路由已变化，不发送。'}
            key = hashlib.sha256((send_stream + '|' + request_id).encode()).hexdigest() if request_id else ''
            now = self._now()
            prior = await asyncio.to_thread(ledger.request_receipt, key)
            if prior is None:
                prior = await asyncio.to_thread(ledger.recent_stream_receipt, send_stream, now)
            if prior:
                return attempt_result(prior, duplicate=True)
            recent = {}
            for raw in self._recent() or []:
                if not isinstance(raw, dict):
                    continue
                row_bvid = str(raw.get('bvid') or '')
                title = clean(raw.get('title'), 200)
                if BV.fullmatch(row_bvid) and title and row_bvid not in recent:
                    recent[row_bvid] = {'bvid': row_bvid, 'title': title}
            selected = recent.get(bvid)
            if selected is None:
                return {'success': False, 'error_class': 'no_recent_video',
                        'content': '近期观看记忆中没有这个可分享视频，不搜索、不编造。'}
            prior = await asyncio.to_thread(ledger.pair_receipt, bvid, send_stream, now, manual=True)
            if prior:
                return attempt_result(prior, duplicate=True)
            await asyncio.to_thread(ledger.ensure_manual, selected, now)
            claim = await asyncio.to_thread(
                ledger.claim, selected['bvid'], send_stream, 'manual', key, now, self._day(), {}, preparing=True)
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
                refreshed = await self.social.profiles.routes(manual_stream=send_stream)
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