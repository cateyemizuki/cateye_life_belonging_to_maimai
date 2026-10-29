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
    models={};modules={};calls=unknown=0;tokens=verified_tokens=unverified_tokens=0
    from .llm_usage import module_of_event,usage_bucket_of,missing_usage_source,empty_buckets
    # 四桶并列输出（2026-10-29 §2.2 口径）：正常回传/成功但无用量/失败/legacy（旧行）。
    buckets=empty_buckets()
    for row in rows:
        try:
            at=datetime.fromisoformat(str(row.get('ts','')))
            if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
            if not now-timedelta(hours=hours)<=at<=now:continue
            count=max(0,int(row.get('tokens',0)));model=str(row.get('model') or '未返回模型名')[:120]
            calls+=1;tokens+=count
            bucket=usage_bucket_of(row);buckets[bucket]['calls']+=1;buckets[bucket]['tokens']+=count
            # 「待核验」改名「缺用量来源调用」：口径 = usage_source != host_reported
            # 或 usage_present 不为真（旧行无该字段 → 一律计入）。旧键名兼容输出，
            # 新键名并列给出，渲染端下一工作流切换。
            if missing_usage_source(row):
                unknown+=1;unverified_tokens+=count
            else:verified_tokens+=count
            models[model]=models.get(model,0)+count
            module=module_of_event(row.get('event'));modules[module]=modules.get(module,0)+count
        except (ValueError,TypeError,OverflowError):continue
    return {'calls':calls,'tokens':tokens,'models':dict(sorted(models.items(),key=lambda r:r[1],reverse=True)[:30]),
            'host_reported_tokens':verified_tokens,'unverified_tokens':unverified_tokens,
            'modules':modules,'unverified_usage_calls':unknown,'window_hours':hours,'truncated':truncated,
            'missing_usage_source_calls':unknown,'missing_usage_source_tokens':unverified_tokens,
            'buckets':buckets,
            'statement_status':'not_provided','cost_verified':False,
            'note':'新记录标明宿主用量来源；缺失与旧记录单独标记。未提供供应商账单，不能据此确认实际扣费。'}

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
                # Host usage/database logs mention the requester in SQL parameters.
                # That marker does not transfer ownership of the host failure.
                origins=' '.join(str(row.get(k) or '') for k in ('module','logger','logger_name'))
                host_owned=any(part.startswith(('src.llm_models.', 'src.common.database.'))
                               for part in origins.split())
                key='plugin_errors' if not host_owned and 'life_belonging_to_maimai' in serialized else 'external_errors'
                result[key]+=1
            if 'Supervisor 已重启完成: file_watcher' in serialized:
                result['reloads']+=1;result['last_reload']=row.get('timestamp')
    except OSError:pass
    return result
