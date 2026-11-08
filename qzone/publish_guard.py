"""Bounded durable publish receipts. An uncertain send is never blindly replayed."""
from __future__ import annotations
import hashlib,json,time
from pathlib import Path
from typing import Any
from core.storage import save_json_atomic

UNRESOLVED={'sending','uncertain'}
MAX_RECEIPTS=128

def preview_post(text: str,images: Any=None,visibility: int=1) -> dict[str,Any]:
    clean=str(text or '').strip()
    digests=[hashlib.sha256(bytes(img)).hexdigest() for img in (images or [])[:4] if isinstance(img,(bytes,bytearray)) and img]
    key=hashlib.sha256(json.dumps([clean,digests,int(visibility)],ensure_ascii=False).encode()).hexdigest()
    return {'key':key,'content':clean[:5000],'characters':len(clean),'image_count':len(digests),'visibility':int(visibility)}

def definitely_unavailable(message: Any) -> bool:
    text=''.join(str(message or '').lower().split())
    return any(mark in text for mark in ('e_not_found','notregistered','unknownapi','接口不存在','未注册','api不存在','找不到api')) or ('api' in text and 'notfound' in text)

class PublishLedger:
    def __init__(self,path: Any=None):
        self.path=Path(path) if path else None
        self.rows={};self.writable=True
        if self.path and self.path.exists():
            try:
                if self.path.stat().st_size>1024*1024:raise ValueError('oversized receipts')
                raw=json.loads(self.path.read_text(encoding='utf-8'))
                if raw.get('version')!=1 or not isinstance(raw.get('rows'),dict):raise ValueError('unknown receipt format')
                self.rows={k:v for k,v in raw['rows'].items() if isinstance(k,str) and isinstance(v,dict)}
                # A crash between dispatch and acknowledgement is ambiguous.
                for row in self.rows.values():
                    if row.get('status')=='sending':row['status']='uncertain'
            except Exception:
                self.writable=False
    def save(self):
        if self.path is None:return True
        try:return bool(save_json_atomic(self.path,{'version':1,'rows':self.rows}))
        except Exception:return False
    def begin(self,preview: dict[str,Any],now: float|None=None):
        now=time.time() if now is None else now
        key=preview['key'];old=self.rows.get(key)
        if not self.writable:return False,{'status':'storage_unavailable'}
        if old:
            if old.get('status') in UNRESOLVED:return False,old
            if old.get('status')=='sent' and now-float(old.get('updated_at',now))<86400:return False,old
            if old.get('status')!='sent' and (int(old.get('attempts',0))>=2 or now<float(old.get('retry_at',0))):return False,old
        if key not in self.rows and len(self.rows)>=MAX_RECEIPTS:
            disposable=[(float(v.get('updated_at',0)),k) for k,v in self.rows.items() if v.get('status') not in UNRESOLVED]
            if not disposable:return False,{'status':'capacity_blocked'}
            del self.rows[min(disposable)[1]]
        attempts=1 if not old or old.get('status')=='sent' else int(old.get('attempts',0))+1
        row={**preview,'status':'sending','attempts':attempts,'created_at':now,'updated_at':now}
        self.rows[key]=row
        if not self.save():
            row['status']='storage_unavailable';return False,row
        return True,row
    def finish(self,key: str,status: str,*,result: Any=None,error: str=''):
        row=self.rows[key]
        if status=='retry_wait' and int(row.get('attempts',0))>=2:status='failed'
        row.update(status=status,updated_at=time.time(),error_class=error[:48])
        if result:row['result']={k:result[k] for k in ('tid','time') if k in result}
        if status=='retry_wait':row['retry_at']=time.time()+60
        self.save();return row
    def recent_sent(self,limit: int=5) -> list[dict[str,Any]]:
        """PR-FIXED-CHROME（3.17.15）只读：最近已确认发送的说说（status=sent），
        按 created_at 降序取≤limit（夹取 1..20）。返回 content 截断 120 字、
        created_at（epoch 秒）、tid（回执 result.tid，缺失空串）、image_count。
        不改 rows、不落盘、不发起任何空间动作；供娱乐面板「最近动态」展示，
        不含路径/私聊正文。损坏行（created_at 非数值）按 0 排序兜底，不抛。"""
        try:limit=max(1,min(20,int(limit)))
        except Exception:limit=5
        def ts(row: dict[str,Any]) -> float:
            value=row.get('created_at')
            return float(value) if isinstance(value,(int,float)) else 0.0
        sent=[r for r in self.rows.values() if isinstance(r,dict) and r.get('status')=='sent']
        sent.sort(key=ts,reverse=True)
        out=[]
        for row in sent[:limit]:
            result=row.get('result') if isinstance(row.get('result'),dict) else {}
            images=row.get('image_count')
            out.append({'content':str(row.get('content',''))[:120],
                        'created_at':row.get('created_at') if isinstance(row.get('created_at'),(int,float)) else None,
                        'tid':str(result.get('tid','') or ''),
                        'image_count':int(images) if isinstance(images,(int,float)) else 0})
        return out
    def summary(self):
        counts={}
        for row in self.rows.values():counts[row.get('status','unknown')]=counts.get(row.get('status','unknown'),0)+1
        return {'writable':self.writable,'counts':counts,'recent':[{'status':r.get('status'),'attempts':r.get('attempts'),
                    'updated_at':r.get('updated_at'),'image_count':r.get('image_count'),'error_class':r.get('error_class','')}
                    for r in sorted(self.rows.values(),key=lambda r:r.get('updated_at',0),reverse=True)[:8]]}
