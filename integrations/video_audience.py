"""Explicitly allowed routes and bounded incremental topic context.

Raw chat text lives only in the current evaluation input, never in profiles.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib,json
from typing import Any
from integrations.video_share_policy import QQ,clean,number

_CST = timezone(timedelta(hours=8))


def _day_window(now: float) -> tuple[float, str]:
    """业务时区（Asia/Shanghai）当日零点epoch与日期键，用于群活跃门槛按日计数。"""
    dt = datetime.fromtimestamp(now, _CST)
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), start.strftime('%Y-%m-%d')


def rows(value:Any,key:str)->list:
    if isinstance(value,dict):
        if value.get('success') is False:raise ValueError('宿主拒绝读取')
        value=value.get(key,value.get('data',[]))
    if not isinstance(value,list):raise ValueError('未知宿主数据形态')
    return [x for x in value if isinstance(x,dict)]


def message_epoch(value:Any)->float:
    if isinstance(value,(int,float)):return number(value)
    try:return float(value)
    except (TypeError,ValueError):
        try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
        except (TypeError,ValueError):return 0.0


def message_sender_id(row: dict) -> str:
    info = row.get("message_info")
    user = info.get("user_info") if isinstance(info, dict) else None
    if not isinstance(user, dict):
        user = row.get("user_info")
    return str(row.get("user_id") or (user.get("user_id") if isinstance(user, dict) else "") or "")


def _is_bot(row: dict, route: dict) -> bool:
    sender = message_sender_id(row)
    return (row.get("is_mai") is True or row.get("is_self") is True
            or bool(sender and sender == route.get("account_id")))


def route_of(raw:dict)->dict|None:
    stream=str(raw.get('stream_id') or raw.get('session_id') or '').strip()
    platform=str(raw.get('platform') or 'qq')
    group=str(raw.get('group_id') or '').strip()
    user=str(raw.get('user_id') or '').strip()
    if not stream or platform!='qq':return None
    if group:
        if not QQ.fullmatch(group):return None
        return {'stream_id':stream,'kind':'group','group_id':group,'name':clean(raw.get('group_name'),80) or '群聊',
                'account_id':str(raw.get('account_id') or '')}
    if not QQ.fullmatch(user) or raw.get('is_group_session') is True:return None
    return {'stream_id':stream,'kind':'private','user_id':user,'name':clean(raw.get('user_cardname') or raw.get('user_nickname') or raw.get('user_name') or raw.get('nickname'),80) or '私聊',
            'account_id':str(raw.get('account_id') or '')}


class AudienceProfiles:
    def __init__(self,ledger,*,stream_reader,message_reader,config_provider,target_provider):
        self.ledger=ledger;self._streams=stream_reader;self._messages=message_reader
        self._config=config_provider;self._target=target_provider

    async def routes(self,*,manual_stream='')->list[dict]:
        registered=rows(await self._streams(),'streams')
        routes=[r for raw in registered if (r:=route_of(raw)) is not None]
        if manual_stream:return [r for r in routes if r['stream_id']==manual_stream]
        cfg=self._config();extra=set()
        for raw in getattr(cfg,'allowed_chats',[]) or []:
            if isinstance(raw,str) and ':' in raw:
                kind,identifier=raw.strip().split(':',1)
                if kind in ('group','private') and QQ.fullmatch(identifier):extra.add((kind,identifier))
        target=self._target();allow_all=bool(getattr(cfg,'allow_all_groups',False))
        allowed=[]
        for route in routes:
            identity=(route['kind'],route.get('group_id',route.get('user_id')))
            companion=(getattr(cfg,'include_companion',True) and route['kind']=='private'
                       and route['stream_id']==target.get('stream_id') and route.get('user_id')==target.get('user_id'))
            # 自动候选只含：陪伴目标私聊 + 群（allow_all_groups 或 allowed_chats 的 group:）。
            # 非陪伴目标私聊始终不主动发视频——即使 allowed_chats 写了 private:非陪伴QQ 也不纳入。
            # bot 用手动工具指定发送走 routes(manual_stream=...) 提前返回，不受此限。
            if companion:allowed.append(route)
            elif route['kind']=='group' and (allow_all or identity in extra):allowed.append(route)
        await asyncio.to_thread(self.ledger.restrict_profiles, [r['stream_id'] for r in allowed])
        return allowed

    def companion_target(self) -> dict:
        """陪伴目标路由身份（stream_id/user_id）；手动转发到陪伴私聊时核验用。"""
        target=self._target() or {}
        return {'stream_id':str(target.get('stream_id') or ''),'user_id':str(target.get('user_id') or '')}

    async def _day_activity(self, route, profile, day_start, day_key, now, minimum):
        """Count non-bot messages from midnight, independently of the recent topic window."""
        same_day = profile.get('activity_day') == day_key
        count = int(profile.get('day_non_bot_count', 0)) if same_day else 0
        if count >= minimum:
            return count
        cursor = number(profile.get('activity_cursor'), day_start) if same_day else day_start
        boundary_ids = set(profile.get('activity_boundary_ids') or []) if same_day else set()
        # Inclusive cursor, so equal-time messages are deduped by stable message ID.
        limit = min(minimum - count, 256) + len(boundary_ids)
        page = rows(await self._messages(route['stream_id'], str(cursor), str(now),
                                         limit=max(1, limit), limit_mode='earliest',
                                         filter_mai=True), 'messages')
        seen = set()
        newest = cursor
        last_ids = set(boundary_ids)
        for row in page:
            mid = str(row.get('message_id') or row.get('id') or '')
            stamp = message_epoch(row.get('time', row.get('timestamp')))
            if not mid or not cursor <= stamp <= now or mid in seen:
                continue
            seen.add(mid)
            if stamp > newest:
                newest, last_ids = stamp, set()
            if stamp == newest:
                last_ids.add(mid)
            info = row.get('message_info') or {}
            user = info.get('user_info') or row.get('user_info') or {}
            sender = str(row.get('user_id') or user.get('user_id') or '')
            if (mid not in boundary_ids and not row.get('is_self') and not row.get('is_mai')
                    and not (sender and sender == route.get('account_id'))):
                count += 1
        profile.update(activity_day=day_key, activity_cursor=newest, activity_boundary_ids=sorted(last_ids),
                       day_count_is_lower_bound=len(page) >= limit)
        return count

    async def latest_context(self, route: dict, now: float) -> dict | None:
        """Read the latest exchange on demand, including when the topic profile is cached."""
        try:
            items = rows(await self._messages(
                route["stream_id"], str(now - 7 * 86400), str(now),
                limit=12, limit_mode="latest", filter_mai=False), "messages")
        except Exception:
            return None
        latest = []
        for row in items:
            stamp = message_epoch(row.get("time", row.get("timestamp")))
            if not now - 7 * 86400 <= stamp <= now:
                continue
            mid = str(row.get("message_id") or row.get("id") or "")
            text = clean(row.get("processed_plain_text") or row.get("plain_text") or row.get("text"), 240)
            latest.append((stamp, mid, text, _is_bot(row, route)))
        latest.sort(key=lambda item: (item[0], item[1]))
        fingerprint = hashlib.sha256(json.dumps(latest, ensure_ascii=False).encode()).hexdigest()
        return {
            "fingerprint": fingerprint,
            "recent": [{"text": text, "is_bot": own, "at": stamp}
                       for stamp, _, text, own in latest if text][-8:],
            "last_bot_at": max((stamp for stamp, _, _, own in latest if own), default=0.0),
            "recent_rate": sum(1 for stamp, _, _, _ in latest if now - stamp < 120),
        }

    async def collect(self,now:float)->list[dict]:
        routes = await self.routes()
        routes.sort(key=lambda route: route["kind"] != "private")
        out=[]
        cfg=self._config();interval=max(300,float(getattr(cfg,'profile_refresh_minutes',30))*60)
        threshold=max(0,int(getattr(cfg,'min_group_daily_messages',0)))
        day_start,day_key=_day_window(now)
        # No implicit fallback to a companion route if another allowed stream fails.
        for route in routes:
            sid=route['stream_id'];old=await asyncio.to_thread(self.ledger.profile, sid);profile=dict(old)
            refresh_after=min(interval,300) if old.get('coverage')!='ok' else interval
            needs_day_count = route['kind'] == 'group' and threshold > 0
            if not old or now-number(old.get('checked_at'))>=refresh_after or (needs_day_count and old.get('activity_day') != day_key):
                try:
                    start=max(now-7*86400,number(old.get('cursor'))-2) if old else now-7*86400
                    data=rows(await self._messages(sid,str(start),str(now),limit=80,
                        limit_mode='earliest' if old else 'latest',filter_mai=False),'messages')
                    normalized=[];seen=set(old.get('seen_ids') or [])
                    for row in data:
                        stamp=message_epoch(row.get('time',row.get('timestamp')))
                        mid=str(row.get('message_id') or row.get('id') or '')
                        if not mid or not start<=stamp<=now+5:continue
                        text=clean(row.get('processed_plain_text') or row.get('plain_text') or row.get('text'),240)
                        if not text:continue
                        normalized.append((stamp,mid,text,_is_bot(row, route)))
                    normalized.sort(key=lambda x:(x[0],x[1]))
                    new=[x for x in normalized if x[1] not in seen]
                    cursor=max([number(old.get('cursor')),*(x[0] for x in normalized)])
                    # 按日累计非bot消息（跨刷新累加、日切重置），供群活跃门槛使用。
                    prior_count=int(old.get('day_non_bot_count',0)) if old.get('day_key')==day_key else 0
                    if needs_day_count:
                        day_count = await self._day_activity(route, profile, day_start, day_key, now, threshold)
                    else:
                        day_count=prior_count+sum(1 for x in new if not x[3] and x[0]>=day_start)
                    profile.update(checked_at=now,cursor=cursor,coverage='partial' if old and len(data)>=80 else 'ok',
                        history_scope='incremental' if old else 'latest_80',
                        coverage_start=min((x[0] for x in normalized),default=start),
                        seen_ids=list(dict.fromkeys([*(old.get('seen_ids') or []),*(x[1] for x in normalized)]))[-160:],
                        message_count=int(old.get('message_count',0))+len(new),
                        last_message_at=max([number(old.get('last_message_at')),*(x[0] for x in normalized)]),
                        last_bot_at=max([number(old.get('last_bot_at')),*(x[0] for x in normalized if x[3])]),
                        recent_rate=sum(1 for x in normalized if now-x[0]<120),
                        has_user_history=bool(old.get('has_user_history') or any(not x[3] for x in normalized)),
                        day_key=day_key,day_non_bot_count=day_count)
                    # 不达标群不建立/更新话题画像（仅留轻量活动计数），省token并防误发小群。
                    if route['kind']=='group' and threshold>0 and day_count<threshold:
                        profile.pop('topics',None)
                    await asyncio.to_thread(self.ledger.save_profile, sid, profile)
                except Exception:
                    profile.update(checked_at=now,coverage='unavailable')
                    await asyncio.to_thread(self.ledger.save_profile, sid, profile)
            # 群活跃门槛：当日非bot消息不足则不纳入候选（私聊豁免）；跨日未刷新计数视为0。
            stored_count=int(profile.get('day_non_bot_count',0)) if profile.get('day_key')==day_key else 0
            if route['kind']=='group' and threshold>0 and stored_count<threshold:continue
            if (profile.get('coverage')!='ok' or not profile.get('has_user_history')
                    or now-number(profile.get('last_message_at'))>7*86400):continue
            out.append({**route,'topics':profile.get('topics') or [],
                'as_of':profile.get('checked_at'),'last_message_at':profile.get('last_message_at'),
                'message_count':profile.get('message_count',0),'day_non_bot_count':stored_count,
                'day_count_is_lower_bound':bool(profile.get('day_count_is_lower_bound'))})
            if len(out)>=8:break
        latest = await asyncio.gather(*(self.latest_context(route, now) for route in out))
        return [{**route, **context} for route, context in zip(out, latest) if context is not None]

    def update_topics(self,updates:dict,allowed:list[dict]):
        valid={r['stream_id'] for r in allowed}
        for sid,topics in updates.items():
            if sid not in valid or not isinstance(topics,list):continue
            profile=self.ledger.profile(sid)
            profile['topics']=list(dict.fromkeys(clean(x,50) for x in topics if isinstance(x,str) and clean(x,50)))[:12]
            self.ledger.save_profile(sid,profile)
