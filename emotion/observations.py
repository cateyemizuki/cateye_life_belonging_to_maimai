"""Bounded VAD observations; no chat, prompts, identities or retrospective reconstruction."""
import json,math,shutil
from datetime import datetime,timedelta
from pathlib import Path
from core.storage import save_json_atomic

class EmotionObservations:
    def __init__(self,path):
        self.path=Path(path);self.points=[];self.writable=True;self.persisted=True
        try:
            if self.path.exists():
                if self.path.stat().st_size>1024*1024:raise ValueError('oversized observations')
                raw=json.loads(self.path.read_text(encoding='utf-8'))
                if raw.get('version')!=1:self.writable=False;return
                for row in raw.get('points',[])[-2304:]:
                    if not isinstance(row,dict):continue
                    stamp=datetime.fromisoformat(str(row.get('at','')))
                    if stamp.tzinfo is None:continue
                    if not all(type(row.get(k)) in (int,float) and math.isfinite(row[k]) and -1<=row[k]<=1 for k in ('valence','arousal','dominance')):continue
                    self.points.append({k:row[k] for k in ('at','valence','arousal','dominance','frozen') if k in row})
                self.points.sort(key=lambda r:datetime.fromisoformat(r['at']).timestamp())
        except Exception:
            self.persisted=False
            try:
                if self.path.exists():shutil.copy2(self.path,self.path.with_name(self.path.name+'.corrupt-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.bak'))
            except OSError:self.writable=False
    def sample(self,now,vad,frozen=False):
        if not self.writable or now.tzinfo is None:return
        if self.points and (now-datetime.fromisoformat(self.points[-1]['at'])).total_seconds()<300:return
        if not all(type(vad.get(k)) in (int,float) and math.isfinite(vad[k]) for k in ('valence','arousal','dominance')):return
        row={'at':now.isoformat(),'frozen':bool(frozen),**{k:round(max(-1,min(1,vad[k])),5) for k in ('valence','arousal','dominance')}}
        cutoff=now-timedelta(days=7)
        self.points=[r for r in self.points if datetime.fromisoformat(r['at'])>=cutoff][-2303:]+[row]
        try:self.persisted=bool(save_json_atomic(self.path,{'version':1,'points':self.points}))
        except Exception:self.persisted=False
    def snapshot(self,now,hours=24):
        rows=[dict(r) for r in self.points if now-timedelta(hours=hours)<=datetime.fromisoformat(r['at'])<=now]
        for i,row in enumerate(rows):
            if i and (datetime.fromisoformat(row['at'])-datetime.fromisoformat(rows[i-1]['at'])).total_seconds()>900:row['gap_before']=True
        downsampled=len(rows)>600
        if downsampled:
            # Preserve gap knowledge across uniform display sampling.
            indices=[round(i*(len(rows)-1)/599) for i in range(600)];kept=[];previous=-1
            for index in indices:
                row=rows[index]
                if any(r.get('gap_before') for r in rows[previous+1:index+1]):row['gap_before']=True
                kept.append(row);previous=index
            rows=kept
        return {'points':rows,'downsampled':downsampled,'writable':self.writable,'persisted':self.persisted}
