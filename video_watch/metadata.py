"""Public video metadata snapshots and bounded background fill; never performs a watch or LLM call."""
from __future__ import annotations
from datetime import datetime,timezone
import asyncio
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit,urlunsplit

BV=re.compile(r'^BV[A-Za-z0-9]{10}$')
METRICS=('likes','coins','favorites')


def count(value):
    if isinstance(value,bool) or value is None:return None
    try:
        number=float(value)
        return int(number) if math.isfinite(number) and number>=0 and number.is_integer() else None
    except (TypeError,ValueError,OverflowError):return None


def safe_cover(value):
    if not isinstance(value,str):return None
    if value.startswith('//'):value='https:'+value
    try:
        url=urlsplit(value)
        host=(url.hostname or '').lower()
        if (url.scheme not in ('http','https') or url.username or url.password
                or url.port not in (None,80,443) or not any(host==domain or host.endswith('.'+domain) for domain in ('hdslb.com','biliimg.com'))):return None
        return urlunsplit((url.scheme,url.netloc,url.path,url.query,''))
    except (ValueError,TypeError):return None


def metadata_snapshot(data,*,provenance='watch_snapshot',captured_at=None):
    stat=data.get('stat') if isinstance(data.get('stat'),dict) else {}
    supplied=data.get('metrics') if isinstance(data.get('metrics'),dict) else {}
    return {'title':str(data.get('title') or '').strip()[:400],
            'cover':safe_cover(data.get('cover') or data.get('pic')),
            'metrics':{name:count(supplied.get(name,stat.get(key))) for name,key in (('likes','like'),('coins','coin'),('favorites','favorite'))},
            'metrics_captured_at':captured_at or datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'metrics_provenance':provenance}


def presentation(row):
    video=row.get('video') if isinstance(row.get('video'),dict) else {}
    visual=row.get('visual') if isinstance(row.get('visual'),dict) else {}
    metrics=row.get('metrics') if isinstance(row.get('metrics'),dict) else video.get('metrics') if isinstance(video.get('metrics'),dict) else {}
    return {'title':str(video.get('title') or row.get('title') or '').strip()[:400],
            'cover':safe_cover(row.get('cover') or video.get('cover') or video.get('pic')),
            'impression':None if row.get('impression_source')=='template_unavailable' else str(row.get('impression') or '').strip()[:2000] or None,
            'impression_provenance':'template_unavailable' if row.get('impression_source')=='template_unavailable' else
                'understood_content' if row.get('impression') and (visual.get('understanding_status')=='completed' or len(str(row.get('subtitle_text') or ''))>=40) else
                'metadata_only' if row.get('impression') else 'unavailable',
            'metrics':{name:count(metrics.get(name)) for name in METRICS},
            'metrics_captured_at':row.get('metrics_captured_at') or video.get('metrics_captured_at'),
            'metrics_provenance':str(row.get('metrics_provenance') or video.get('metrics_provenance') or 'unrecorded')}


def merge_missing(row,snapshot):
    """Preserve existing actual values, including zero; mixed metric fields keep explicit provenance."""
    original=presentation(row);changed=False;out=dict(row)
    video=dict(row.get('video') or {}) if isinstance(row.get('video'),dict) else {}
    if not original['title'] and snapshot.get('title'):
        video['title']=snapshot['title'];out['video']=video;changed=True
    if original['cover'] is None and safe_cover(snapshot.get('cover')):
        out['cover']=snapshot['cover'];changed=True
    values=dict(original['metrics']);new_values=snapshot.get('metrics') or {};added=[]
    for name in METRICS:
        value=count(new_values.get(name))
        if values[name] is None and value is not None:values[name]=value;added.append(name)
    if added:
        out['metrics']=values
        provenance=str(snapshot.get('metrics_provenance') or 'unrecorded')
        out['metrics_field_provenance']={**{name:original['metrics_provenance'] for name in METRICS if original['metrics'][name] is not None},
                                          **{name:provenance for name in added}}
        out['metrics_captured_at']=snapshot.get('metrics_captured_at')
        out['metrics_provenance']=provenance
        changed=True
    return out,changed


def needs_metadata(row):
    doc=presentation(row)
    return not doc['title'] or not doc['cover'] or any(doc['metrics'][name] is None for name in METRICS)


class MetadataBackfill:
    def __init__(self,path,*,fetch=None,clock=None):
        self.path=Path(path);self.fetch=fetch;self.clock=clock or time.time
        try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={}
        if not isinstance(self.state,dict):self.state={}
        self.lock=asyncio.Lock()

    async def _fetch(self,bvid):
        if self.fetch is not None:return await self.fetch(bvid)
        import httpx
        async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
            result=await client.get('https://api.bilibili.com/x/web-interface/view',params={'bvid':bvid},headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.bilibili.com/'})
            result.raise_for_status();return result.json()

    def _save(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8');os.replace(temporary,self.path)

    async def run_batch(self,store,limit=3):
        async with self.lock:
            now=self.clock();checked=updated=0
            if now<float(self.state.get('global_retry_at') or 0):return {'checked':0,'updated':0,'backoff':True}
            attempts=self.state.setdefault('videos',{})
            if not isinstance(attempts,dict):attempts={};self.state['videos']=attempts
            seen=set()
            for row in reversed(store.load_history()):
                bvid=str(row.get('bvid') or '')
                if checked>=max(1,min(10,limit)):break
                if not BV.fullmatch(bvid) or bvid in seen or row.get('status')!='completed' or not needs_metadata(row):continue
                seen.add(bvid);prior=attempts.get(bvid) or {}
                if now<float(prior.get('retry_at') or 0):continue
                checked+=1
                count_attempts=min(10,int(prior.get('attempts') or 0)+1)
                try:
                    payload=await self._fetch(bvid)
                    if not isinstance(payload,dict) or payload.get('code')!=0 or not isinstance(payload.get('data'),dict):
                        raise ValueError('metadata_unavailable')
                    data=payload['data']
                    if data.get('bvid') not in (None,bvid):raise ValueError('metadata_identity_mismatch')
                    snapshot=metadata_snapshot(data,provenance='backfilled_current')
                    updated+=int(store.update_video_metadata(bvid,snapshot))
                    # Partial APIs are retryable; actual zero is complete and remains untouched.
                    attempts[bvid]={'attempts':count_attempts,'retry_at':now+min(86400,900*2**(count_attempts-1)),'state':'checked'}
                except asyncio.CancelledError:raise
                except Exception as exc:
                    status=getattr(getattr(exc,'response',None),'status_code',None)
                    attempts[bvid]={'attempts':count_attempts,'retry_at':now+min(86400,900*2**(count_attempts-1)),
                                    'state':'failed','error':f'http_{status}' if status else type(exc).__name__}
                    if status in (412,429):self.state['global_retry_at']=now+3600
                self.state['videos']=dict(list(attempts.items())[-1000:]);self._save()
                if self.state.get('global_retry_at',0)>now:break
            return {'checked':checked,'updated':updated,'backoff':False}
