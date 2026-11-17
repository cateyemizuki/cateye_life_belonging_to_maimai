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

def generation_logs(data_dir,now,hours=24,limit=200,*,include_all=False):
    """Read bounded evidence; the display cap never limits request association."""
    hours=max(1,min(720,int(hours)));records=[];truncated=False
    if data_dir:
        # Separate budgets prevent optional raw diagnostics from crowding out compact evidence.
        for folder,prefix in (('llm_generation_outcomes','generation_'),('llm_logs','llm_calls_')):
            budget=2*1024*1024
            for offset in range(min(31,(hours+23)//24+1)):
                path=Path(data_dir)/folder/(prefix+(now-timedelta(days=offset)).strftime('%Y-%m-%d')+'.jsonl')
                try:size=path.stat().st_size
                except OSError:continue
                if budget<=0:truncated=True;break
                rows,cut=tail_records(path,budget);budget-=min(size,budget);truncated=truncated or cut
                for row in rows:
                    if row.get('kind')!='generation':continue
                    try:
                        at=datetime.fromisoformat(str(row.get('time','')))
                        if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
                        elif now.tzinfo is None:at=at.replace(tzinfo=None)
                        if not now-timedelta(hours=hours)<=at<=now:continue
                    except (ValueError,TypeError):continue
                    record={'time':at.isoformat(),'level':row.get('level') if row.get('level') in ('info','warn','error') else 'unknown',
                            'used_template':row.get('used_template') is True}
                    for name in ('event','module','task','model','category','group_id','request_id','tier','template_name','template_title','evidence_id'):
                        record[name]=str(row.get(name) or '')[:160]
                    for name in ('message','fallback_reason'):record[name]=str(row.get(name) or '')[:400]
                    for name in ('completion_tokens','warning_threshold','attempt','max_tokens'):record[name]=_usage_count(row.get(name))
                    records.append(record)
    # New rows share an evidence ID across the durable journal and optional diagnostics.
    unique={}
    for record in records:
        key=record['evidence_id'] or json.dumps(record,sort_keys=True,ensure_ascii=False)
        unique.setdefault(key,record)
    records=sorted(unique.values(),key=lambda row:(row['time'],row.get('attempt') or 0),reverse=True)
    counts={level:sum(row['level']==level for row in records) for level in ('info','warn','error','unknown')}
    result={'rows':records[:limit],'counts':counts,'truncated':truncated or len(records)>limit,
            'read_truncated':truncated,'display_truncated':len(records)>limit,'total':len(records)}
    if include_all:result['_all_rows']=records
    return result


def decorate_usage_rows(rows,logs):
    """One semantic projection for aggregation and every page; never changes billing."""
    from .llm_templates import usage_level
    by_id={};templates={};last={}
    rank={'unknown':-1,'info':0,'warn':1,'error':2}
    for evidence in logs:
        request=evidence.get('request_id');group=evidence.get('group_id')
        if evidence.get('used_template') and group:templates.setdefault(group,evidence)
        if request and not evidence.get('used_template'):
            previous=by_id.get(request)
            if previous is None or rank.get(evidence.get('level'),-1)>rank.get(previous.get('level'),-1):
                by_id[request]=evidence
            if group:
                candidate=(evidence.get('attempt') or 0,evidence.get('time') or '',request)
                if candidate>last.get(group,(-1,'','')):last[group]=candidate
    for row in rows:
        prior={key:row[key] for key in ('level','tier','warning_threshold','semantic_status','semantic_category')
               if key in row} if row.get('semantic_status')=='recorded' else {}
        base=usage_level(row.get('event'),row.get('success'),row.get('completion') if row.get('usage_source')=='host_reported' or row.get('usage_present') is True else None,row.get('abandoned'))
        # A successful host response says nothing about validation of its content.
        if base['level']=='info' or (row.get('success') is None and not row.get('abandoned')):base['level']='unknown'
        row.update(base,semantic_status='unrecorded')
        match=by_id.get(row.get('request_id'))
        if match:
            row['semantic_status']='recorded'
            if row.get('success') is not False and not row.get('abandoned'):row['level']=match.get('level','unknown')
            for field in ('tier','warning_threshold'):
                if match.get(field) is not None:row[field]=match[field]
            row['semantic_category']=match.get('category','')
            group=match.get('group_id')
        else:
            # Compatibility callers may decorate an already-projected page with a display fragment.
            # Missing evidence in that fragment must not erase a previously recorded judgment.
            row.update(prior)
            group=row.get('generation_group_id')
        template=templates.get(group)
        if template and (template.get('request_id')==row.get('request_id') or
                         (not template.get('request_id') and last.get(group,(-1,'',''))[2]==row.get('request_id'))):
            row.update(used_template=True,template_name=template['template_name'],
                       template_title=template['template_title'],fallback_reason=template['fallback_reason'])
    return rows


def usage_summary(data_dir,now,hours=24,*,include_rows=False):
    """按窗口聚合 LLM 账本：模型 / 子模块 / token 拆分 / 缓存 / 四桶。

    与 :meth:`core.llm_usage.LLMUsageTracker.summary` 保持同口径；这里额外保留
    宿主日志的 ``truncated`` 有界读取标记。v2 行优先读 ``module`` 字段，旧行回退
    event 映射。W-09 只增拆分覆盖率字段（``*_reported_calls`` / ``split_tokens`` /
    ``split_coverage``），不改 ``tokens`` 原语义。
    """
    hours=max(1,min(720,int(hours)))
    logs=generation_logs(data_dir,now,hours,include_all=True)
    rows,truncated=tail_records(Path(data_dir)/'llm_usage_events.jsonl',2*1024*1024) if data_dir else ([],False)
    decorate_usage_rows(rows,logs['_all_rows'])
    level_counts={level:0 for level in ('info','warn','error','unknown')}
    projected=[];schema1_calls=0
    models={};model_calls={};modules={};module_calls={};matrix={}
    model_reported={};module_reported={};total_reported=0
    calls=unknown=0;tokens=verified_tokens=unverified_tokens=0
    prompt_total=completion_total=cache_hit_total=cache_miss_total=0
    # W-09（2026-11-03）只增字段：与 LLMUsageTracker.summary 同口径，记录多少行
    # 明确回传了输入/输出拆分与缓存字段；缺拆分时"总量≠输入+输出"才可解释。
    prompt_reported=completion_reported=cache_reported=0
    abandoned_calls=unknown_module_calls=0
    from .llm_usage import (module_of_event,usage_bucket_of,missing_usage_source,
                            empty_buckets,reported_token_count,USAGE_TERMS,MODULE_UNKNOWN,entry_module)
    # 四桶并列输出（2026-10-29 §2.2 口径）：正常回传/成功但无用量/失败/legacy（旧行）。
    buckets=empty_buckets()
    for row in rows:
        try:
            at=datetime.fromisoformat(str(row.get('ts','')))
            if at.tzinfo is None:at=at.replace(tzinfo=now.tzinfo)
            elif now.tzinfo is None:at=at.replace(tzinfo=None)
            if not now-timedelta(hours=hours)<=at<=now:continue
            count=reported_token_count(row);model=str(row.get('model') or '未返回模型名')[:120]
            if (_usage_count(row.get('schema')) or 1)<2:schema1_calls+=1
            calls+=1;tokens+=count;level_counts[row['level']]+=1
            bucket=usage_bucket_of(row);buckets[bucket]['calls']+=1;buckets[bucket]['tokens']+=count
            available=row.get('usage_source')=='host_reported'
            if available:total_reported+=1
            buckets[bucket]['reported_calls']=buckets[bucket].get('reported_calls',0)+int(available)
            # 「缺少来源」改名「缺用量来源调用」：口径 = usage_source != host_reported
            # 或 usage_present 不为真（旧行无该字段 → 一律计入）。旧键名兼容输出，
            # 新键名并列给出，渲染端下一工作流切换。
            if missing_usage_source(row):
                unknown+=1;unverified_tokens+=count
            else:verified_tokens+=count
            models[model]=models.get(model,0)+count
            model_calls[model]=model_calls.get(model,0)+1
            module=entry_module(row)
            if module==MODULE_UNKNOWN:unknown_module_calls+=1
            modules[module]=modules.get(module,0)+count
            module_calls[module]=module_calls.get(module,0)+1
            cell=matrix.setdefault(module,{}).setdefault(model,{'calls':0,'tokens':0,'reported_calls':0})
            cell['calls']+=1;cell['tokens']+=count;cell['reported_calls']+=int(available)
            model_reported[model]=model_reported.get(model,0)+int(available)
            module_reported[module]=module_reported.get(module,0)+int(available)
            reported=row.get('usage_source')=='host_reported' or row.get('usage_present') is True
            prompt=_usage_count(row.get('prompt')) if reported else None
            completion=_usage_count(row.get('completion')) if reported else None
            cache_hit=_usage_count(row.get('cache_hit')) if reported else None
            cache_miss=_usage_count(row.get('cache_miss')) if reported else None
            if include_rows:
                item={key:row.get(key) for key in ('event','task_name','request_id','success','abandoned',
                      'level','tier','warning_threshold','semantic_status','semantic_category','used_template',
                      'template_name','template_title','fallback_reason')}
                item['used_template']=row.get('used_template') is True
                item.update(ts=at.isoformat(),module=module,model=str(row.get('model') or '')[:120],
                            prompt=prompt,completion=completion,tokens=count,cache_hit=cache_hit,
                            cache_miss=cache_miss,duration_ms=_usage_count(row.get('duration_ms')),
                            attempts=_usage_count(row.get('attempts')),usage_present=row.get('usage_present') is True,
                            usage_source=str(row.get('usage_source') or ''))
                projected.append(item)
            if prompt is not None:prompt_total+=prompt;prompt_reported+=1
            if completion is not None:completion_total+=completion;completion_reported+=1
            if cache_hit is not None or cache_miss is not None:cache_reported+=1
            if cache_hit is not None:cache_hit_total+=cache_hit
            if cache_miss is not None:cache_miss_total+=cache_miss
            if row.get('abandoned') is True:abandoned_calls+=1
        except (ValueError,TypeError,OverflowError):continue
    for bucket in buckets.values():bucket.setdefault('reported_calls',0)
    return {'calls':calls,'tokens':tokens,'total_reported_calls':total_reported,
            'model_reported_calls':model_reported,'module_reported_calls':module_reported,'models':dict(sorted(models.items(),key=lambda r:r[1],reverse=True)[:30]),
            'host_reported_tokens':verified_tokens,
            'modules':modules,'window_hours':hours,'truncated':truncated,
            'semantic_evidence_truncated':logs['read_truncated'],
            'has_usage':tokens>0,'window_start':(now-timedelta(hours=hours)).isoformat(timespec='seconds'),
            'schema1_calls':schema1_calls,'rows':sorted(projected,key=lambda row:row['ts'],reverse=True),
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
            'generation_logs_read_truncated':logs['read_truncated'],
            'generation_logs_display_truncated':logs['display_truncated'],'generation_logs_total':logs['total'],
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
