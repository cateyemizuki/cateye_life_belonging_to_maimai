"""Durable per-intent chance and accepted-task facts; no schedule state ownership."""
from __future__ import annotations
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid


def watch_budget_identity(episode,lookup=None):
    """Follow an unchanged intent through resumes; its original date is the stable anchor."""
    if not isinstance(episode,dict):return None
    current=episode;seen=set();aliases=[];intent='';day='';root=''
    for _ in range(32):
        eid=str(current.get('episode_id') or '').strip()
        if not eid or eid in seen:return None
        meta=current.get('metadata') if isinstance(current.get('metadata'),dict) else {}
        item=str(meta.get('intent_item_id') or '').strip()
        date=str(current.get('schedule_date') or '').strip()
        if intent and item and item!=intent:break
        seen.add(eid);aliases.append(eid);root=eid
        if item:
            intent=item
            if date:day=date
        parent=str(current.get('resume_of') or '').strip()
        if not parent:break
        if parent in seen:return None
        previous=lookup(parent) if callable(lookup) else None
        if not isinstance(previous,dict):
            aliases.append(parent)
            if not intent:root=parent
            break
        current=previous
    else:return None
    key=json.dumps(['intent',day,intent] if intent and day else ['episode',root],ensure_ascii=False,separators=(',',':'))
    return {'key':key,'aliases':aliases,'intent_item_id':intent or None}


class IntentWatchLedger:
    """Atomically reserve a slot, then count only a confirmed unique task ID."""
    def __init__(self,path):
        self.path=Path(path)
        with self._connection(write=True) as db:
            db.execute('CREATE TABLE IF NOT EXISTS scopes (key TEXT PRIMARY KEY, updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS chances (scope TEXT PRIMARY KEY REFERENCES scopes(key) ON DELETE CASCADE, at REAL, probability REAL, hit INTEGER, source TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS accepted (scope TEXT REFERENCES scopes(key) ON DELETE CASCADE, task_id TEXT, episode_id TEXT, at REAL, PRIMARY KEY(scope,task_id))')
            db.execute('CREATE TABLE IF NOT EXISTS pending (scope TEXT REFERENCES scopes(key) ON DELETE CASCADE, token TEXT PRIMARY KEY, episode_id TEXT, at REAL, state TEXT)')
            db.execute('CREATE INDEX IF NOT EXISTS pending_scope ON pending(scope)')
            db.execute('CREATE TABLE IF NOT EXISTS legacy (scope TEXT REFERENCES scopes(key) ON DELETE CASCADE, episode_id TEXT, unidentified INTEGER, PRIMARY KEY(scope,episode_id))')

    @contextmanager
    def _connection(self,*,write=False):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=.5,isolation_level=None)
        try:
            db.execute('PRAGMA foreign_keys=ON')
            if write:db.execute('BEGIN IMMEDIATE')
            yield db
            if write:db.commit()
        except BaseException:
            if db.in_transaction:db.rollback()
            raise
        finally:db.close()

    @staticmethod
    def _counts(db,key):
        known=db.execute('SELECT COUNT(*) FROM accepted WHERE scope=?',(key,)).fetchone()[0]
        old=db.execute('SELECT COALESCE(SUM(unidentified),0) FROM legacy WHERE scope=?',(key,)).fetchone()[0]
        pending=db.execute('SELECT COUNT(*) FROM pending WHERE scope=?',(key,)).fetchone()[0]
        return {'accepted_count':int(known+old),'pending_count':int(pending)}

    def _ensure(self,db,key,now):
        # Time alone cannot prove that an intent is no longer resumable. Keep its tiny fact tombstone.
        db.execute('INSERT INTO scopes(key,updated) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET updated=excluded.updated',(key,now))

    def snapshot(self,key):
        with self._connection() as db:
            result=self._counts(db,key)
            row=db.execute('SELECT at,probability,hit,source FROM chances WHERE scope=?',(key,)).fetchone()
            result['chance']=None if row is None else {'at':row[0],'probability':row[1],'hit':None if row[2] is None else bool(row[2]),'source':row[3]}
            return result

    def seed_legacy(self,key,episode_id,*,task_ids=(),reported_count=0,chance=None,now):
        """Import existing acceptance/chance evidence once; unknown IDs are explicit legacy counts."""
        with self._connection(write=True) as db:
            self._ensure(db,key,now)
            if db.execute('SELECT 1 FROM legacy WHERE scope=? AND episode_id=?',(key,episode_id)).fetchone():return
            ids={str(value) for value in task_ids if value}
            for task in ids:db.execute('INSERT OR IGNORE INTO accepted VALUES (?,?,?,?)',(key,task,episode_id,now))
            db.execute('INSERT INTO legacy VALUES (?,?,?)',(key,episode_id,max(0,int(reported_count or 0)-len(ids))))
            if isinstance(chance,dict):
                hit=chance.get('hit');probability=chance.get('probability')
                probability=float(probability) if isinstance(probability,(float,int)) and not isinstance(probability,bool) else None
                db.execute('INSERT OR IGNORE INTO chances VALUES (?,?,?,?,?)',(key,chance.get('at'),probability,int(hit) if isinstance(hit,bool) else None,'legacy'))

    def roll_once(self,key,probability,*,rng,now):
        with self._connection(write=True) as db:
            self._ensure(db,key,now)
            counts=self._counts(db,key)
            if counts['accepted_count'] or counts['pending_count'] or db.execute('SELECT 1 FROM chances WHERE scope=?',(key,)).fetchone():return None
            hit=float(rng.random())<probability
            db.execute('INSERT INTO chances VALUES (?,?,?,?,?)',(key,now,probability,int(hit),'roll'))
            return hit

    def reserve(self,key,episode_id,limit,*,now):
        with self._connection(write=True) as db:
            self._ensure(db,key,now);counts=self._counts(db,key)
            if counts['pending_count']:return {'token':None,'reason':'submission_unresolved',**counts}
            if counts['accepted_count']>=max(0,int(limit)):return {'token':None,'reason':'intent_watch_limit',**counts}
            token=uuid.uuid4().hex
            db.execute('INSERT INTO pending VALUES (?,?,?,?,?)',(key,token,episode_id,now,'reserved'))
            return {'token':token,'reason':'',**counts}

    def confirm(self,key,token,task_id,*,now):
        if not str(task_id or ''):raise ValueError('missing accepted task identity')
        with self._connection(write=True) as db:
            self._ensure(db,key,now)
            pending=db.execute('SELECT episode_id FROM pending WHERE scope=? AND token=?',(key,token)).fetchone()
            exists=db.execute('SELECT 1 FROM accepted WHERE scope=? AND task_id=?',(key,str(task_id))).fetchone()
            if pending is None and not exists:raise ValueError('missing intent reservation')
            if not exists:db.execute('INSERT INTO accepted VALUES (?,?,?,?)',(key,str(task_id),pending[0],now))
            db.execute('DELETE FROM pending WHERE scope=? AND token=?',(key,token))
            return {'new':not bool(exists),**self._counts(db,key)}

    def reject(self,key,token):
        with self._connection(write=True) as db:
            return db.execute("DELETE FROM pending WHERE scope=? AND token=? AND state='reserved'",(key,token)).rowcount>0

    def mark_uncertain(self,key,token):
        with self._connection(write=True) as db:
            db.execute("UPDATE pending SET state='uncertain' WHERE scope=? AND token=?",(key,token))
