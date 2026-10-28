"""Bounded, redacted read projections for the dashboard and overnight evidence."""
import json
from datetime import datetime,timedelta
from pathlib import Path

def tail_records(path,budget=512*1024):
    rows=[];truncated=False
    try:
        with Path(path).open('rb') as stream:
            size=stream.seek(0,2);truncated=size>budget;stream.seek(max(0,size-budget))
            if truncated:stream.readline()
            for line in stream.read(budget).decode('utf-8',errors='replace').splitlines():
                try:
                    row=json.loads(line)
                    if isinstance(row,dict):rows.append(row)
                except ValueError:pass
    except OSError:pass
    return rows,truncated

def usage_summary(data_dir,now,hours=24):
    rows,truncated=tail_records(Path(data_dir)/'llm_usage_events.jsonl',2*1024*1024)
    models={};modules={};calls=unknown=0;tokens=0
    from .llm_usage import module_of_event
    for row in rows:
        try:
            at=datetime.fromisoformat(str(row.get('ts','')))
            if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
            if not now-timedelta(hours=hours)<=at<=now:continue
            count=max(0,int(row.get('tokens',0)));model=str(row.get('model') or '未返回模型名')[:120]
            calls+=1;tokens+=count
            if row.get('usage_source')!='host_reported':unknown+=1
            models[model]=models.get(model,0)+count
            module=module_of_event(row.get('event'));modules[module]=modules.get(module,0)+count
        except (ValueError,TypeError,OverflowError):continue
    return {'calls':calls,'tokens':tokens,'models':dict(sorted(models.items(),key=lambda r:r[1],reverse=True)[:30]),
            'modules':modules,'unverified_usage_calls':unknown,'window_hours':hours,'truncated':truncated,
            'statement_status':'not_provided','cost_verified':False,
            'note':'调用用量来自宿主回执；缺失与旧记录单独标记。未提供供应商账单，不能据此确认实际扣费。'}

def runtime_health(plugin,now):
    result={'scope':'bounded_recent_log_tail','plugin_errors':0,'external_errors':0,'reloads':0,'last_reload':None,'truncated':False}
    root=getattr(plugin,'_plugin_root',None)
    if not root:return result
    try:
        folder=Path(root).parent.parent/'logs'
        files=list(folder.glob('app_*.log.jsonl'))
        if not files:return result
        rows,truncated=tail_records(max(files,key=lambda f:f.stat().st_mtime))
        result['truncated']=truncated
        for row in rows:
            serialized=' '.join(str(row.get(k) or '') for k in ('event','module','logger','logger_name'))
            if str(row.get('level','')).lower() in ('error','critical'):
                key='plugin_errors' if 'life_belonging_to_maimai' in serialized else 'external_errors'
                result[key]+=1
            if 'Supervisor 已重启完成: file_watcher' in serialized:
                result['reloads']+=1;result['last_reload']=row.get('timestamp')
    except OSError:pass
    return result
