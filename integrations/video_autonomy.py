"""Small durable controls for autonomous viewing; independent of scheduler state ownership."""
from __future__ import annotations
from datetime import datetime
import json
import os
from pathlib import Path
import uuid


def config_value(config,name,default):
    return config.get(name,default) if isinstance(config,dict) else getattr(config,name,default)


def quota_limits(config=None,*,legacy=False):
    defaults={'daily_session_limit':3 if legacy else 8,'daily_minutes_limit':120 if legacy else 240,
              'daily_dedicated_limit':2 if legacy else 4,'daily_gap_limit':2,
              'schedule_casual_daily_limit':3,'gap_min_interval_minutes':60}
    result={}
    for key,default in defaults.items():
        try:value=int(config_value(config,key,default))
        except (TypeError,ValueError,OverflowError):value=default
        result[key]=max(0,min(1440 if 'minutes' in key else 100,value))
    return result


def episode_watch_kind(episode):
    from core.activity_attributes import activity_attributes_for_episode,activity_module_guard
    if 'watch_video' not in (activity_attributes_for_episode(episode) or []):return ''
    if activity_module_guard(episode,module='watch_video'):return ''
    return 'dedicated'


def casual_eligible(episode):
    from core.activity_attributes import activity_module_guard
    return not episode_watch_kind(episode) and not activity_module_guard(episode,require_clear_idle=True)


def remaining_episode_minutes(episode,now):
    try:
        day=str(episode.get('schedule_date') or '')
        end=float(episode.get('planned_end_minute'))
        if not day or end<0:return None
        # Reuse the schedule clock policy: normalize missing wall times and choose the first fold.
        from core.time_utils import wall_time,elapsed_seconds,shift_elapsed
        minute=int(end)
        boundary=wall_time(datetime.fromisoformat(day).date(),minute,now.tzinfo)
        boundary=shift_elapsed(boundary,(end-minute)*60)
        return max(0.0,elapsed_seconds(boundary,now)/60)
    except (ValueError,TypeError,OverflowError):return None


class EpisodeOpportunities:
    """Legacy episode-keyed chance file, retained for compatibility and intent-ledger migration."""
    def __init__(self,path):self.path=Path(path)

    def roll_once(self,episode_id,probability,*,rng,now):
        try:
            with self.path.open(encoding='utf-8') as stream:document=json.load(stream)
        except FileNotFoundError:document={}
        except (OSError,ValueError):return None  # Corrupt/unknown ownership is not a fresh chance.
        if not isinstance(document,dict):return None
        if episode_id in document:return None
        cutoff=now-30*86400
        document={key:row for key,row in document.items() if isinstance(row,dict) and float(row.get('at') or 0)>=cutoff}
        if len(document)>=2048:
            document=dict(sorted(document.items(),key=lambda item:float(item[1].get('at') or 0))[-2047:])
        value=float(rng.random());hit=value<probability
        document[episode_id]={'at':now,'hit':hit,'probability':probability}
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex[:8]+'.tmp')
        try:
            temporary.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8');os.replace(temporary,self.path)
        except OSError:return None
        finally:
            if temporary.exists():temporary.unlink()
        return hit
