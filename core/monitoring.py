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

def usage_summary(data_dir,now,hours=24):
    """按窗口聚合 LLM 账本：模型 / 子模块 / token 拆分 / 缓存 / 四桶。

    与 :meth:`core.llm_usage.LLMUsageTracker.summary` 保持同口径；这里额外保留
    宿主日志的 ``truncated`` 有界读取标记。v2 行优先读 ``module`` 字段，旧行回退
    event 映射。
    """
    rows,truncated=tail_records(Path(data_dir)/'llm_usage_events.jsonl',2*1024*1024)
    models={};model_calls={};modules={};module_calls={};matrix={}
    calls=unknown=0;tokens=verified_tokens=unverified_tokens=0
    prompt_total=completion_total=cache_hit_total=cache_miss_total=0
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
            calls+=1;tokens+=count
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
            if prompt is not None:prompt_total+=prompt
            if completion is not None:completion_total+=completion
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
            'model_calls':model_calls,'module_calls':module_calls,'matrix':matrix,
            'abandoned_calls':abandoned_calls,'unknown_module_calls':unknown_module_calls,
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
                result['reloads']+=1;result['last_reload']=_log_time(row.get('timestamp'),now)
    except OSError:pass
    return result
