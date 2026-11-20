"""Topic-balanced candidate selection and bounded recent choices; no network or model calls."""
from __future__ import annotations
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import random
import uuid


MIN_TOPIC_WEIGHT = 0.25
MAX_TOPIC_WEIGHT = 1.80


def _bounded_weight(value):
    return max(MIN_TOPIC_WEIGHT, min(MAX_TOPIC_WEIGHT, float(value)))


class TopicSampler:
    def __init__(self, path=None, *, rng=None):
        self.path=Path(path) if path else None
        self.rng=rng or random.Random()
        try:self.state=json.loads(self.path.read_text(encoding='utf-8')) if self.path else {}
        except (OSError,ValueError):self.state={}
        if not isinstance(self.state,dict):self.state={}
        self.state['recent']=[row for row in self.state.get('recent',[]) if isinstance(row,dict)][-24:]
        for name in ('picks','search_cursor'):
            try:self.state[name]=max(0,int(self.state.get(name) or 0))
            except (ValueError,TypeError,OverflowError):self.state[name]=0

    def _save(self):
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex[:8]+'.tmp')
            try:
                temporary.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8')
                os.replace(temporary,self.path)
            finally:
                if temporary.exists():temporary.unlink()

    def search_order(self, keywords):
        if not keywords:return []
        start=self.state['search_cursor']%len(keywords)
        self.state['search_cursor']=(start+1)%len(keywords)
        self._save()
        return keywords[start:]+keywords[:start]

    @staticmethod
    def annotate(candidate,keywords,weights,*,explore=False):
        hits=candidate.get('matched_keywords') or []
        retrieval=str(candidate.get('retrieval_keyword') or '')
        # Choose a single known interest topic without multiplying its chance by video count.
        topics=[key for key in keywords if key in hits or key==retrieval]
        topic=(max(topics,key=lambda key:weights.get(key,1.0)) if topics
               else str(candidate.get('tname') or '公开热门探索'))
        return {**candidate,'selection_topic':topic,'selection_weight':_bounded_weight(weights.get(topic,1.0)),
                'selection_lane':'explore' if explore else 'interest'}

    def balanced_pool(self,candidates,limit=10):
        buckets=defaultdict(list)
        for row in candidates:buckets[row['selection_topic']].append(row)
        for rows in buckets.values():rows.sort(key=lambda row:(-float(row.get('_score') or 0),-int(row.get('views') or 0)))
        recent=Counter(row.get('topic') for row in self.state['recent'][-12:])
        topics=sorted(buckets,key=lambda topic:(recent[topic],self.rng.random()))
        result=[]
        while topics and len(result)<limit:
            for topic in list(topics):
                if len(result)>=limit:break
                result.append(buckets[topic].pop(0))
                if not buckets[topic]:topics.remove(topic)
        return result

    def choose(self,pool):
        if not pool:return None
        # Three of each ten selection positions prefer exploration when real candidates exist.
        explore=self.state['picks']%10 in (2,5,8)
        lane='explore' if explore else 'interest'
        choices=[row for row in pool if row.get('selection_lane')==lane] or pool
        buckets=defaultdict(list)
        for row in choices:buckets[row['selection_topic']].append(row)
        recent=self.state['recent'][-12:]
        counts=Counter(row.get('topic') for row in recent)
        topics=list(buckets)
        last=recent[-1].get('topic') if recent else ''
        if len(topics)>1 and last in topics:topics.remove(last)
        weights=[_bounded_weight(buckets[topic][0].get('selection_weight', 1.0))/(1+counts[topic]) for topic in topics]
        topic=self.rng.choices(topics,weights=weights,k=1)[0]
        candidate=self.rng.choice(buckets[topic][:3])
        state={**self.state,'picks':self.state['picks']+1,
               'recent':(self.state['recent']+[{'topic':topic,'bvid':str(candidate.get('bvid') or '')}])[-24:]}
        previous=self.state;self.state=state
        try:self._save()
        except OSError:
            self.state=previous
            raise
        pool.remove(candidate)
        return candidate
