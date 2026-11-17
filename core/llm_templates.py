"""LLM 输出协议与保守兜底数据；不依赖业务模块。"""
from __future__ import annotations
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from uuid import uuid4
from functools import lru_cache
from .mapping_dir import ensure_user_table, mappings_path, seed_path

PLUGIN_ROOT=Path(__file__).resolve().parents[1]
THRESHOLDS={'large':16000,'medium':10000,'small':4000}


@lru_cache(maxsize=128)
def event_limits(event):
    """Read-only metadata for usage projections; never seeds or changes user files."""
    name=str(event or '')
    if not re.fullmatch(r'[a-z0-9_]+',name):name='generic'
    try:
        spec=json.loads(seed_path(PLUGIN_ROOT,'templates/'+name+'.json').read_text(encoding='utf-8-sig'))
        tier=spec.get('tier','small')
    except (OSError,ValueError):tier='small'
    if tier not in THRESHOLDS:tier='small'
    return tier,THRESHOLDS[tier]


def usage_level(event,success,completion,abandoned=False):
    tier,threshold=event_limits(event)
    level='error' if abandoned or success is not True else 'warn' if isinstance(completion,(int,float)) and not isinstance(completion,bool) and completion>threshold else 'info'
    return {'level':level,'tier':tier,'warning_threshold':threshold}


class FormatFailure(ValueError):
    pass


class SemanticFailure(ValueError):
    pass


def _load(name: str, default: dict) -> dict:
    relative='templates/'+name+'.json'
    ensure_user_table(PLUGIN_ROOT,relative)
    for path in (mappings_path(PLUGIN_ROOT,relative),seed_path(PLUGIN_ROOT,relative)):
        try:
            value=json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(value,dict):continue
            if 'response' in default and not all(key in value for key in ('event','title','format','response')):continue
            if name=='schedule_layout' and (not isinstance(value.get('rows'),list) or not value['rows']):continue
            if name=='event_pool' and not (isinstance(value.get('names'),list) and value['names']
                    and all(isinstance(item,str) and item for item in value['names'])
                    and isinstance(value.get('prefixes'),dict) and isinstance(value.get('event'),dict)
                    and isinstance(value.get('phrases'),list) and len(value['phrases'])>=2
                    and all(isinstance(item,str) and item for item in value['phrases'])):continue
            if name=='event_pool':
                try:[phrase.format(name='日常整理') for phrase in value['phrases']]
                except (KeyError,ValueError,IndexError):continue
            return value
        except (OSError,ValueError):pass
    return deepcopy(default)


def definition(event: str) -> dict:
    aliases={'judge':'miss_confirm','describe_image':'cateye_screen_describe'}
    name=aliases.get(str(event),str(event or 'generic'))
    if not re.fullmatch(r'[a-z0-9_]+',name):name='generic'
    default={'event':name,'title':'保守默认结果','tier':'small','format':'text',
             'light':True,'ok':False,'response':'暂时没有可用结果'}
    result=_load(name,default)
    if not all(key in result for key in ('event','title','format','response')):return default
    result['title']=str(result.get('title') or default['title'])[:120]
    # Limits and protocol belong to the caller, rather than editable template data.
    trusted=seed_path(PLUGIN_ROOT,'templates/'+name+'.json')
    try:
        protocol=json.loads(trusted.read_text(encoding='utf-8-sig'))
        for key in ('event','tier','format','light','required','ok'):result[key]=protocol[key]
    except (OSError,ValueError,KeyError):pass
    return result


def parse_json(text: str):
    value=str(text or '').strip()
    if value.startswith('```'):
        value=re.sub(r'^```(?:json)?\s*','',value,flags=re.I)
        value=re.sub(r'\s*```$','',value)
    try:return json.loads(value)
    except (TypeError,ValueError):
        decoder=json.JSONDecoder()
        match=re.search(r'[\[{]',re.sub(r'<think>.*?</think>','',value,flags=re.S|re.I))
        clean=re.sub(r'<think>.*?</think>','',value,flags=re.S|re.I)
        if match:
            try:return decoder.raw_decode(clean[match.start():])[0]
            except ValueError:pass
    raise FormatFailure('回复不是完整可解析的 JSON')


def _number(value,low,high) -> bool:
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and low<=value<=high


def _vad(value,limit=1.0) -> bool:
    return isinstance(value,dict) and all(_number(value.get(key),-limit,limit) for key in ('valence','arousal','dominance'))


def validate(text: str,event: str):
    spec=definition(event);fmt=spec['format']
    if not str(text or '').strip():raise FormatFailure('模型没有返回正文')
    if fmt=='text':return str(text)
    if fmt=='yesno':
        if str(text).strip().upper() not in ('Y','N','YES','NO','是','否'):raise SemanticFailure('需要明确的肯定或否定结论')
        return str(text)
    if fmt=='decision':
        if str(text).strip().strip('`\'"「」“”*')[:1] not in ('是','否'):raise SemanticFailure('需要以是或否开头的判定')
        return str(text)
    data=parse_json(text)
    if event=='schedule_spine' and isinstance(data,list):data={'intentions':data}
    if event=='schedule_spine' and isinstance(data,dict) and 'schedule_items' in data and 'intentions' not in data:data={**data,'intentions':data['schedule_items']}
    if event=='schedule_events' and isinstance(data,list):data={'events':data}
    if not isinstance(data,dict) or any(key not in data for key in spec.get('required',[])):
        raise SemanticFailure('结构完整，但缺少此任务需要的结果字段')
    valid=True
    if event=='schedule_spine':valid=isinstance(data['intentions'],list) and bool(data['intentions'])
    elif event=='schedule_events':valid=isinstance(data['events'],list) and bool(data['events'])
    elif event=='role_judge':valid=data['decision'] in ('today','future','reject')
    elif event=='affection_review':valid=_number(data['delta'],-4,5) and data.get('tag','normal') in ('normal','conflict','apology','sweet')
    elif event=='affection_daily_review':valid=_number(data['delta_score'],-4,4) and isinstance(data.get('impressions',[]),list)
    elif event=='companion_disposition':valid=_number(data['anchor_hours'],2,24) and _number(data['loneliness'],0,1)
    elif event=='emotion_init':
        valid=(_vad(data['anchor'],.6) and ('offset' not in data or _vad(data['offset'],.3))
               and _number(data.get('positive_gain',1),.8,1.3) and _number(data.get('negative_decay',1),.6,1.2)
               and isinstance(data.get('reasons',{}),dict))
    elif event in ('emotion_evaluate','emotion_daily_settlement','emotion_sleep_referee'):valid=_vad(data['delta_vad'],1.0 if event=='emotion_sleep_referee' else .5)
    elif event=='video_interest':valid=isinstance(data['keywords'],list) and bool(data['keywords']) and all(isinstance(x,str) and x.strip() for x in data['keywords'])
    elif event=='qzone_video_decision':valid=data['action'] in ('post','defer','do_not_post') and _number(data['delay_minutes'],0,1440)
    elif event=='video_share_evaluation':valid=data['action'] in ('share','defer','do_not_share') and isinstance(data['target_stream_id'],str) and _number(data['delay_seconds'],0,3600)
    elif event.startswith('activity_preference_'):
        valid=isinstance(data.get('activities'),list)
        if event.endswith('_profile'):valid=valid and isinstance(data.get('traits'),dict)
    if not valid:raise SemanticFailure('结果字段或数值不符合此任务要求')
    return data


def fallback(event: str,context: dict|None=None) -> dict:
    spec=definition(event);context=context or {};response=deepcopy(spec['response'])
    if event=='schedule_events':
        pool=_load('event_pool',{'names':['整理桌边小物'], 'prefixes':{},
            'event':{'goal_type':'daily_routine','min_minutes':5,'max_minutes':15,
                     'interruptibility':.9,'weight':.5,'state_deltas':{},'enabled':True},
            'phrases':['在身边挑一小处，{name}，按自己的节奏慢慢完成。','先把手边的东西理顺，再{name}，不用急着赶进度。']})
        slot=str(context.get('slot','08-16'));count=max(0,min(24,int(context.get('requested',1))))
        reserved=set(context.get('reserved_names') or []);rows=[];suffix=uuid4().hex[:8]
        for index in range(count):
            base=pool['names'][index%len(pool['names'])]
            prefix=str(pool['prefixes'].get(slot,'日常'));name=(prefix+base)[:10]
            variant=1
            while name in reserved:
                variant+=1;tail=str(variant);name=(prefix+base)[:10-len(tail)]+tail
            reserved.add(name)
            rows.append({**pool['event'],'source':'template','origin_kind':'template',
                         'template_used':True,'template_name':'schedule_events','template_title':spec['title'],'event_id':f'llm_tpl_{slot.replace("-","_")}_{index}_{suffix}',
                         'name':name,'templates':[phrase.format(name=base) for phrase in pool['phrases']]})
        response={'events':rows}
    if event=='schedule_spine' and context.get('previous_response'):
        response=context['previous_response'];spec={**spec,'title':'上一轮有效日程'}
    if callable(context.get('factory')):response=context['factory']()
    text=response if isinstance(response,str) else json.dumps(response,ensure_ascii=False)
    if spec.get('ok') and event not in ('schedule_spine','emotion_daily_settlement'):
        try:validate(text,event)
        except (FormatFailure,SemanticFailure):
            try:
                original=json.loads(seed_path(PLUGIN_ROOT,'templates/'+spec['event']+'.json').read_text(encoding='utf-8-sig'))
                response=original['response'];text=response if isinstance(response,str) else json.dumps(response,ensure_ascii=False)
            except (OSError,ValueError,KeyError):
                spec={**spec,'ok':False};text='暂时没有可用结果'
    return {'text':text,'name':spec['event'],'title':spec['title'],'ok':bool(spec.get('ok',True))}


def schedule_rows(date,*,wake_min,sleep_min,no_sleep_mode,max_intentions,row_builder):
    """Render the external row template against the active cycle's real anchors."""
    layout=_load('schedule_layout',{'rows':[]})
    awake=(sleep_min-wake_min)%1440 or 1440
    sleep_span=max(180,min(720,(wake_min-sleep_min)%1440))
    anchors={'wake':wake_min,'sleep':sleep_min,'mid':wake_min+awake//3,'awake_end':wake_min+awake}
    values={'sleep_span':sleep_span,'awake_end_minus_330':wake_min+awake-330}
    rows=[]
    for spec in layout.get('rows',[]):
        if not isinstance(spec,dict):continue
        try:
            if (no_sleep_mode and spec.get('condition')=='sleep') or awake<int(spec.get('min_awake',0)):continue
            anchor=anchors[spec['anchor']]
            def minute(key):
                value=spec[key]
                if value=='sleep_span':return anchor+sleep_span
                return values[value] if isinstance(value,str) else anchor+int(value)
            start,end=minute('start'),minute('end')
            if end<=start:continue
            if spec['anchor']=='mid':start,end=max(wake_min,start),min(wake_min+awake,end)
            minimum,maximum=spec['min'],spec['max']
            minimum=values[minimum] if isinstance(minimum,str) else int(minimum)
            maximum=values[maximum] if isinstance(maximum,str) else int(maximum)
            rows.append(row_builder(date,spec['name'],start,end,spec['goal'],spec['priority'],
                minimum,maximum,interruptibility=float(spec['interruptibility']),hardness=spec.get('hardness','soft')))
        except (KeyError,TypeError,ValueError):continue
    if not rows:
        rows=[row_builder(date,'自由安排',wake_min,wake_min+awake,'free_time','low',15,60,
                          interruptibility=.95,hardness='soft')]
    if len(rows)>max_intentions:
        sleep=[row for row in rows if row['goal_type']=='sleep']
        rows=sleep+[row for row in rows if row['goal_type']!='sleep'][:max(0,max_intentions-len(sleep))]
    for row in rows:
        row.setdefault('parameters',{}).update(template_used=True,template_name='schedule_spine',template_title='基础作息日程')
    return sorted(rows,key=lambda row:int(row['start_minute']))
