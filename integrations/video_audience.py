"""Explicitly allowed routes and bounded incremental topic context.

Raw chat text lives only in the current evaluation input, never in profiles.
"""
from __future__ import annotations
import asyncio
from datetime import datetime
import hashlib,json
from typing import Any
from integrations.video_share_policy import QQ,clean,number


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
        target=self._target()
        allowed=[]
        for route in routes:
            identity=(route['kind'],route.get('group_id',route.get('user_id')))
            companion=(getattr(cfg,'include_companion',True) and route['kind']=='private'
                       and route['stream_id']==target.get('stream_id') and route.get('user_id')==target.get('user_id'))
            if companion or identity in extra:allowed.append(route)
        await asyncio.to_thread(self.ledger.restrict_profiles, [r['stream_id'] for r in allowed])
        return allowed[:12]

    async def collect(self,now:float)->list[dict]:
        routes=await self.routes();out=[]
        cfg=self._config();interval=max(300,float(getattr(cfg,'profile_refresh_minutes',30))*60)
        # No implicit fallback to a companion route if another allowed stream fails.
        for route in routes:
            sid=route['stream_id'];old=await asyncio.to_thread(self.ledger.profile, sid);profile=dict(old)
            recent=[]
            refresh_after=min(interval,300) if old.get('coverage')!='ok' else interval
            if not old or now-number(old.get('checked_at'))>=refresh_after:
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
                        info=row.get('message_info') if isinstance(row.get('message_info'),dict) else {}
                        user=(info.get('user_info') if isinstance(info.get('user_info'),dict)
                              else row.get('user_info') if isinstance(row.get('user_info'),dict) else {})
                        sender=str(row.get('user_id') or user.get('user_id') or '')
                        own=(row.get('is_mai') is True or row.get('is_self') is True
                             or bool(sender and route.get('account_id') and sender==route['account_id']))
                        normalized.append((stamp,mid,text,own))
                    normalized.sort(key=lambda x:(x[0],x[1]))
                    new=[x for x in normalized if x[1] not in seen]
                    cursor=max([number(old.get('cursor')),*(x[0] for x in normalized)])
                    profile.update(checked_at=now,cursor=cursor,coverage='partial' if old and len(data)>=80 else 'ok',
                        history_scope='incremental' if old else 'latest_80',
                        coverage_start=min((x[0] for x in normalized),default=start),
                        seen_ids=list(dict.fromkeys([*(old.get('seen_ids') or []),*(x[1] for x in normalized)]))[-160:],
                        message_count=int(old.get('message_count',0))+len(new),
                        last_message_at=max([number(old.get('last_message_at')),*(x[0] for x in normalized)]),
                        last_bot_at=max([number(old.get('last_bot_at')),*(x[0] for x in normalized if x[3])]),
                        recent_rate=sum(1 for x in normalized if now-x[0]<120),
                        has_user_history=bool(old.get('has_user_history') or any(not x[3] for x in normalized)))
                    recent=[{'text':x[2],'is_bot':x[3]} for x in new[-8:]]
                    await asyncio.to_thread(self.ledger.save_profile, sid, profile)
                except Exception:
                    profile.update(checked_at=now,coverage='unavailable')
                    await asyncio.to_thread(self.ledger.save_profile, sid, profile)
            if (profile.get('coverage')!='ok' or not profile.get('has_user_history')
                    or now-number(profile.get('last_message_at'))>7*86400):continue
            digest=hashlib.sha256(json.dumps({k:profile.get(k) for k in ('cursor','last_message_at')},sort_keys=True).encode()).hexdigest()
            out.append({**route,'topics':profile.get('topics') or [],'recent':recent,
                'as_of':profile.get('checked_at'),'last_message_at':profile.get('last_message_at'),
                'message_count':profile.get('message_count',0),'recent_rate':profile.get('recent_rate',0),
                'last_bot_at':profile.get('last_bot_at',0),'fingerprint':digest})
        return out

    def update_topics(self,updates:dict,allowed:list[dict]):
        valid={r['stream_id'] for r in allowed}
        for sid,topics in updates.items():
            if sid not in valid or not isinstance(topics,list):continue
            profile=self.ledger.profile(sid)
            profile['topics']=list(dict.fromkeys(clean(x,50) for x in topics if isinstance(x,str) and clean(x,50)))[:12]
            self.ledger.save_profile(sid,profile)
