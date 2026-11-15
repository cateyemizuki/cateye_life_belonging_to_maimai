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

def _usage_count(value):
    """非负整数或 None；bool/非法值一律 None。"""
    if isinstance(value,bool) or value is None:return None
    try:return max(0,int(value))
    except (TypeError,ValueError,OverflowError):return None

def generation_logs(data_dir,now,hours=24,limit=200):
    """Read only bounded policy metadata; raw prompts/responses never leave the log."""
    hours=max(1,min(720,int(hours)));budget=2*1024*1024;records=[];truncated=False
    for offset in range(min(31,(hours+23)//24+1)):
        path=Path(data_dir)/'llm_logs'/('llm_calls_'+(now-timedelta(days=offset)).strftime('%Y-%m-%d')+'.jsonl')
        if budget<=0:truncated=True;break
        try:size=path.stat().st_size
        except OSError:continue
        rows,cut=tail_records(path,budget);budget-=min(size,budget);truncated=truncated or cut
        for row in rows:
            if row.get('kind')!='generation':continue
            try:
                at=datetime.fromisoformat(str(row.get('time','')))
                if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
                if not now-timedelta(hours=hours)<=at<=now:continue
            except (ValueError,TypeError):continue
            record={'time':at.isoformat(),'level':row.get('level') if row.get('level') in ('info','warn','error') else 'info',
                    'used_template':row.get('used_template') is True}
            for name in ('event','module','task','model','category','group_id','request_id','tier','template_name','template_title'):
                record[name]=str(row.get(name) or '')[:160]
            for name in ('message','fallback_reason'):record[name]=str(row.get(name) or '')[:400]
            for name in ('completion_tokens','warning_threshold','attempt','max_tokens'):record[name]=_usage_count(row.get(name))
            records.append(record)
    records=[row for _index,row in sorted(enumerate(records),key=lambda item:(item[1]['time'],item[0]),reverse=True)]
    counts={level:sum(row['level']==level for row in records) for level in ('info','warn','error')}
    return {'rows':records[:limit],'counts':counts,'truncated':truncated or len(records)>limit}


def decorate_usage_rows(rows,logs):
    """Join semantic outcomes to request IDs without adding calls or changing tokens."""
    from .llm_templates import usage_level
    by_id={row['request_id']:row for row in logs if row.get('request_id')}
    templates={row['group_id']:row for row in logs if row.get('used_template') and row.get('group_id')}
    last={}
    for row in logs:
        if row.get('request_id') and row.get('group_id'):
            candidate=(row.get('attempt') or 0,row['request_id'])
            if candidate[0]>=last.get(row['group_id'],(-1,''))[0]:last[row['group_id']]=candidate
    for row in rows:
        row.update(usage_level(row.get('event'),row.get('success'),row.get('completion'),row.get('abandoned')))
        match=by_id.get(row.get('request_id'))
        if match:
            for field in ('level','tier','warning_threshold'):row[field]=match[field]
            template=templates.get(match.get('group_id'))
            if template and last.get(match.get('group_id'),(-1,''))[1]==row.get('request_id'):
                row.update(used_template=True,template_name=template['template_name'],
                           template_title=template['template_title'],fallback_reason=template['fallback_reason'])
    return rows


def usage_summary(data_dir,now,hours=24):
    """按窗口聚合 LLM 账本：模型 / 子模块 / token 拆分 / 缓存 / 四桶。

    与 :meth:`core.llm_usage.LLMUsageTracker.summary` 保持同口径；这里额外保留
    宿主日志的 ``truncated`` 有界读取标记。v2 行优先读 ``module`` 字段，旧行回退
    event 映射。W-09 只增拆分覆盖率字段（``*_reported_calls`` / ``split_tokens`` /
    ``split_coverage``），不改 ``tokens`` 原语义。
    """
    logs=generation_logs(data_dir,now,hours)
    rows,truncated=tail_records(Path(data_dir)/'llm_usage_events.jsonl',2*1024*1024)
    decorate_usage_rows(rows,logs['rows'])
    level_counts={level:0 for level in ('info','warn','error')}
    models={};model_calls={};modules={};module_calls={};matrix={}
    calls=unknown=0;tokens=verified_tokens=unverified_tokens=0
    prompt_total=completion_total=cache_hit_total=cache_miss_total=0
    # W-09（2026-11-03）只增字段：与 LLMUsageTracker.summary 同口径，记录多少行
    # 明确回传了输入/输出拆分与缓存字段；缺拆分时"总量≠输入+输出"才可解释。
    prompt_reported=completion_reported=cache_reported=0
    abandoned_calls=unknown_module_calls=0
    from .llm_usage import (module_of_event,usage_bucket_of,missing_usage_source,
                            empty_buckets,reported_token_count,USAGE_TERMS,MODULE_UNKNOWN)
    # 四桶并列输出（2026-10-29 §2.2 口径）：正常回传/成功但无用量/失败/legacy（旧行）。
    buckets=empty_buckets()
    for row in rows:
        try:
            at=datetime.fromisoformat(str(row.get('ts','')))
            if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
            if not now-timedelta(hours=hours)<=at<=now:continue
            count=reported_token_count(row);model=str(row.get('model') or '未返回模型名')[:120]
            calls+=1;tokens+=count;level_counts[row['level']]+=1
            bucket=usage_bucket_of(row);buckets[bucket]['calls']+=1;buckets[bucket]['tokens']+=count
            # 「缺少来源」改名「缺用量来源调用」：口径 = usage_source != host_reported
            # 或 usage_present 不为真（旧行无该字段 → 一律计入）。旧键名兼容输出，
            # 新键名并列给出，渲染端下一工作流切换。
            if missing_usage_source(row):
                unknown+=1;unverified_tokens+=count
            else:verified_tokens+=count
            models[model]=models.get(model,0)+count
            model_calls[model]=model_calls.get(model,0)+1
            module=str(row.get('module') or '') or module_of_event(row.get('event'))
            if module==MODULE_UNKNOWN:unknown_module_calls+=1
            modules[module]=modules.get(module,0)+count
            module_calls[module]=module_calls.get(module,0)+1
            cell=matrix.setdefault(module,{}).setdefault(model,{'calls':0,'tokens':0})
            cell['calls']+=1;cell['tokens']+=count
            prompt=_usage_count(row.get('prompt'))
            completion=_usage_count(row.get('completion'))
            cache_hit=_usage_count(row.get('cache_hit'))
            cache_miss=_usage_count(row.get('cache_miss'))
            if prompt is not None:prompt_total+=prompt;prompt_reported+=1
            if completion is not None:completion_total+=completion;completion_reported+=1
            if cache_hit is not None or cache_miss is not None:cache_reported+=1
            if cache_hit is not None:cache_hit_total+=cache_hit
            if cache_miss is not None:cache_miss_total+=cache_miss
            if row.get('abandoned') is True:abandoned_calls+=1
        except (ValueError,TypeError,OverflowError):continue
    return {'calls':calls,'tokens':tokens,'models':dict(sorted(models.items(),key=lambda r:r[1],reverse=True)[:30]),
            'host_reported_tokens':verified_tokens,
            'modules':modules,'window_hours':hours,'truncated':truncated,
            'missing_usage_source_calls':unknown,'missing_usage_source_tokens':unverified_tokens,
            'buckets':buckets,
            'prompt_tokens':prompt_total,'completion_tokens':completion_total,
            'cache_hit_tokens':cache_hit_total,'cache_miss_tokens':cache_miss_total,
            # W-09 只增字段（不改 tokens 原语义）：split_coverage=拆分/总量，tokens=0 时 None。
            'prompt_reported_calls':prompt_reported,'completion_reported_calls':completion_reported,
            'cache_reported_calls':cache_reported,
            'split_tokens':prompt_total+completion_total,
            'split_coverage':(round((prompt_total+completion_total)/tokens,4) if tokens>0 else None),
            'model_calls':model_calls,'module_calls':module_calls,'matrix':matrix,
            'abandoned_calls':abandoned_calls,'unknown_module_calls':unknown_module_calls,
            'level_counts':level_counts,'generation_logs':logs['rows'],
            'generation_log_counts':logs['counts'],'generation_logs_truncated':logs['truncated'],
            'warning_thresholds':{'large':16000,'medium':10000,'small':4000},
            'terms':USAGE_TERMS,
            'note':'只统计宿主回传的Token；缺失时只计调用，不推算。调用次数包含失败。'}

def _log_time(value,now):
    """Host logs mix UTC ISO timestamps and local MM-DD times; export ISO only."""
    raw=str(value or '')
    try:
        at=datetime.fromisoformat(raw.replace('Z','+00:00'))
    except ValueError:
        try:
            at=datetime.strptime(f'{now.year}-{raw}','%Y-%m-%d %H:%M:%S').replace(tzinfo=now.tzinfo)
            if at>now+timedelta(days=1):at=at.replace(year=now.year-1)
        except ValueError:return None
    if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
    return at.isoformat()

def runtime_health(plugin,now):
    result={'scope':'bounded_recent_log_tail','plugin_errors':0,'external_errors':0,'reloads':0,'last_reload':None,'truncated':False,
            # W-17 只增字段（2026-11-03）：最近一次 error/critical 的时间与归属
            # （'plugin'/'external'）。只给时间戳与范围，不透传异常正文或日志内容；
            # 时间解析失败保持 None，不造假时间。计数值的口径不变（有界日志尾片段）。
            'last_error_at':None,'last_error_scope':None}
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
                # W-17：日志尾片段按时间顺序遍历，逐条覆盖即得「最近一次」；
                # 与 last_reload 同用 _log_time（混合 UTC ISO 与本地时间行，统一导出 ISO）。
                error_at=_log_time(row.get('timestamp'),now)
                if error_at:
                    result['last_error_at']=error_at
                    result['last_error_scope']='plugin' if key=='plugin_errors' else 'external'
            if 'Supervisor 已重启完成: file_watcher' in serialized:
                result['reloads']+=1;result['last_reload']=_log_time(row.get('timestamp'),now)
    except OSError:pass
    return result
