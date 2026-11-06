"""Transactional video social facts and receipts; never performs network I/O."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
import hashlib,json,re,sqlite3,threading,uuid
from pathlib import Path
from typing import Any

from integrations.video_share_policy import ACTIVE_ATTEMPTS,BV,KEEP_SECONDS,clean,number


class ShareLedger:
    def __init__(self, path: Any):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        if self.path.is_symlink():raise OSError('分享数据路径不可用')
        self._lock=threading.RLock()
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS videos(
                bvid TEXT PRIMARY KEY,facts TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,
                status TEXT NOT NULL,next_at REAL NOT NULL,eval_count INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL DEFAULT '{}',reason TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS attempts(
                id TEXT PRIMARY KEY,bvid TEXT NOT NULL,stream TEXT NOT NULL,kind TEXT NOT NULL,
                request_key TEXT NOT NULL,day TEXT NOT NULL,created REAL NOT NULL,status TEXT NOT NULL,
                token TEXT NOT NULL DEFAULT '',receipt TEXT NOT NULL DEFAULT '{}',
                history TEXT NOT NULL DEFAULT 'none',reason TEXT NOT NULL DEFAULT '');
              CREATE UNIQUE INDEX IF NOT EXISTS attempt_request ON attempts(request_key) WHERE request_key<>'';
              CREATE INDEX IF NOT EXISTS attempt_pair ON attempts(bvid,stream,created);
              CREATE TABLE IF NOT EXISTS profiles(stream TEXT PRIMARY KEY,doc TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS legacy_manual(scope TEXT,bvid TEXT,status TEXT,created REAL);
            ''')
            version=self._meta(db,'version','1')
            if version!='1':raise ValueError('未知分享数据版本')
            self._set(db,'version','1')
            db.commit()
        # A lost receipt is never a license to send again. This also safely
        # blocks duplicate work if two plugin processes overlap during reload.
        with self._transaction() as db:
            db.execute("UPDATE attempts SET status='rejected',reason='preparation_interrupted_no_send' WHERE status='preparing'")
            db.execute("UPDATE attempts SET status='uncertain',reason='recovered_inflight' WHERE status='sending'")
            db.execute("UPDATE attempts SET history='uncertain' WHERE history='appending'")
            db.execute("UPDATE videos SET status='deferred',next_at=next_at+1800,reason='evaluation_interrupted' WHERE status='evaluating'")

    @contextmanager
    def _connection(self):
        db=sqlite3.connect(self.path,timeout=3);db.row_factory=sqlite3.Row
        try:yield db
        finally:db.close()

    @contextmanager
    def _transaction(self):
        with self._lock,self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback();raise

    @staticmethod
    def _meta(db,key,default=''):
        row=db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return row['value'] if row else default

    @staticmethod
    def _set(db,key,value):
        db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value)))

    def _clock(self,db,now):
        if number(now,-1)!=now or now<=0:return False
        previous=number(self._meta(db,'last_epoch','0'))
        if now<previous-5:return False
        self._set(db,'last_epoch',max(now,previous));return True

    @staticmethod
    def _video(row):
        if row is None:return None
        doc=dict(row);doc['facts']=json.loads(doc['facts']);doc['decision']=json.loads(doc['decision'])
        return doc

    @staticmethod
    def _attempt(db,row):
        out=dict(row)
        facts=db.execute('SELECT facts FROM videos WHERE bvid=?',(out['bvid'],)).fetchone()
        out['facts']=json.loads(facts['facts']) if facts else {'bvid':out['bvid'],'title':out['bvid']}
        return out

    def request_receipt(self,key:str)->dict|None:
        if not key:return None
        with self._connection() as db:
            row=db.execute('SELECT * FROM attempts WHERE request_key=?',(key,)).fetchone()
            return self._attempt(db,row) if row else None

    def recent_stream_receipt(self,stream:str,now:float)->dict|None:
        with self._connection() as db:
            row=db.execute("SELECT * FROM attempts WHERE stream=? AND created>? AND status IN ('preparing','sending','accepted','uncertain') ORDER BY created DESC LIMIT 1",(stream,now-120)).fetchone()
            return self._attempt(db,row) if row else None

    def pair_receipt(self,bvid:str,stream:str,now:float,*,manual=False)->dict|None:
        with self._connection() as db:
            row=db.execute("SELECT * FROM attempts WHERE bvid=? AND stream IN (?,'*') AND status IN ('preparing','sending','accepted','uncertain') ORDER BY created DESC LIMIT 1",(bvid,stream)).fetchone()
            if row and (row['status']!='accepted' or now-row['created']<(21600 if manual else KEEP_SECONDS)):
                return self._attempt(db,row)
            return None

    def observe(self,facts:dict,now:float,ttl:float)->bool:
        if not BV.fullmatch(str(facts.get('bvid') or '')):return False
        with self._transaction() as db:
            if not self._clock(db,now):return False
            old=db.execute('SELECT status FROM videos WHERE bvid=?',(facts['bvid'],)).fetchone()
            if old and old['status']!='manual_only':return False
            if not old and db.execute('SELECT count(*) FROM videos').fetchone()[0]>=512:return False
            db.execute('''INSERT INTO videos(bvid,facts,created,expires,status,next_at) VALUES(?,?,?,?,'observed',?)
                ON CONFLICT(bvid) DO UPDATE SET facts=excluded.facts,created=excluded.created,
                expires=excluded.expires,status='observed',next_at=excluded.next_at''',
                (facts['bvid'],json.dumps(facts,ensure_ascii=False),now,now+ttl,now))
            return True

    def ensure_manual(self,facts:dict,now:float):
        if not BV.fullmatch(str(facts.get('bvid') or '')):raise ValueError('无效观看记忆')
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO videos(bvid,facts,created,expires,status,next_at) VALUES(?,?,?,?,'manual_only',?)",
                (facts['bvid'],json.dumps(facts,ensure_ascii=False),now,now+KEEP_SECONDS,now))

    def due(self,now:float)->dict|None:
        with self._transaction() as db:
            if not self._clock(db,now):return None
            db.execute("UPDATE videos SET status='expired',reason='ttl' WHERE expires<=? AND status IN ('observed','deferred','queued','evaluated')",(now,))
            row=db.execute("SELECT * FROM videos WHERE status IN ('observed','deferred','queued') AND next_at<=? AND expires>? ORDER BY CASE status WHEN 'queued' THEN 0 ELSE 1 END,created LIMIT 1",(now,now)).fetchone()
            return self._video(row)

    def begin_evaluation(self,bvid:str,now:float)->bool:
        with self._transaction() as db:
            return db.execute("UPDATE videos SET status='evaluating',eval_count=eval_count+1 WHERE bvid=? AND status IN ('observed','deferred') AND eval_count<3 AND expires>? AND next_at<=?",(bvid,now,now)).rowcount==1

    def decide(self,bvid:str,decision:dict,now:float,*,shadow=False):
        action=decision.get('action');status='queued' if action=='share' else 'deferred' if action=='defer' else 'skipped'
        if shadow:status='evaluated'
        next_at=now+max(300,number(decision.get('delay_seconds'),1800)) if action=='defer' else now
        with self._transaction() as db:
            db.execute('UPDATE videos SET status=?,decision=?,next_at=?,reason=? WHERE bvid=?',
                (status,json.dumps(decision,ensure_ascii=False),next_at,clean(decision.get('reason'),300),bvid))
            db.execute("UPDATE videos SET status='skipped',reason='evaluation_limit' WHERE bvid=? AND status='deferred' AND eval_count>=3",(bvid,))

    def postpone(self,bvid:str,reason:str,now:float,seconds=300,*,reevaluate=False):
        with self._transaction() as db:
            row=db.execute('SELECT status,eval_count FROM videos WHERE bvid=?',(bvid,)).fetchone()
            if not row:return
            status='deferred' if reevaluate or row['status']=='evaluating' else row['status']
            if status=='deferred' and row['eval_count']>=3:status='skipped'
            db.execute('UPDATE videos SET status=?,reason=?,next_at=? WHERE bvid=?',
                (status,clean(reason,300),now+seconds,bvid))

    def _auto_limit_reason(self, db, bvid, stream, now, day, limits) -> str:
        # active 是固定 SQL 片段（非用户输入），仅用于减少重复、便于阅读。
        active = "status IN ('preparing','sending','accepted','uncertain')"
        daily = json.loads(self._meta(db, 'legacy_daily', '{}'))
        inherited = (number(daily.get('sent_count')) + number(daily.get('held_count'))
                     if daily.get('date') == day else 0)
        legacy_count = db.execute(
            f"SELECT count(*) FROM attempts WHERE kind='legacy_auto' AND day=? AND {active}",
            (day,)).fetchone()[0]
        inherited = max(inherited, legacy_count)
        count = db.execute(
            f"SELECT count(*) FROM attempts WHERE kind='auto' AND day=? AND {active}",
            (day,)).fetchone()[0]
        if count + inherited >= limits.get('daily_limit', 1):
            return 'daily_limit'
        count = db.execute(
            f"SELECT count(*) FROM attempts WHERE kind IN ('auto','legacy_auto') "
            f"AND stream=? AND day=? AND {active}", (stream, day)).fetchone()[0]
        if count >= limits.get('stream_daily_limit', 1):
            return 'stream_daily_limit'
        last = db.execute(
            f"SELECT max(created) FROM attempts WHERE kind IN ('auto','legacy_auto') AND {active}").fetchone()[0]
        last = max(number(last), number(daily.get('last_sent_epoch')), number(daily.get('last_attempt_epoch')))
        if last and now - last < limits.get('min_interval_hours', 6) * 3600:
            return 'interval'
        targets = db.execute(
            f"SELECT count(DISTINCT stream) FROM attempts WHERE bvid=? "
            f"AND kind IN ('auto','legacy_auto') AND {active}", (bvid,)).fetchone()[0]
        if targets >= limits.get('max_targets_per_video', 1):
            return 'video_target_limit'
        return ''

    def capacity_reason(self,now:float,day:str,limits:dict,*,bvid='',stream='')->str:
        """Read-only early filter; claim repeats it under BEGIN IMMEDIATE."""
        with self._connection() as db:
            if now<number(self._meta(db,'last_epoch','0'))-5:return 'clock_regressed'
            return self._auto_limit_reason(db,bvid,stream,now,day,limits)


    def claim(self, bvid: str, stream: str, kind: str, request_key: str, now: float,
              day: str, limits: dict, *, budget_token: str = '', preparing: bool = False) -> dict:
        """Atomic attempt + quota claim, shared by manual and automatic paths."""
        if kind not in ('auto', 'manual') or not stream or not BV.fullmatch(bvid):
            return {'status': 'blocked', 'reason': 'invalid_identity'}
        with self._transaction() as db:
            if not self._clock(db, now):
                return {'status': 'blocked', 'reason': 'clock_regressed'}
            if request_key:
                old = db.execute("SELECT * FROM attempts WHERE request_key=?", (request_key,)).fetchone()
                if old:
                    return {'status': 'duplicate', 'attempt': self._attempt(db, old)}
            video = db.execute("SELECT * FROM videos WHERE bvid=?", (bvid,)).fetchone()
            if video is None:
                return {'status': 'blocked', 'reason': 'no_watched_fact'}
            if kind == 'auto':
                decision = json.loads(video['decision'])
                if (video['status'] != 'queued' or video['expires'] <= now
                        or decision.get('target_stream_id') != stream):
                    return {'status': 'blocked', 'reason': 'not_queued_for_target'}
            rows = db.execute(
                "SELECT * FROM attempts WHERE (bvid=? AND stream IN (?,'*')) OR (stream=? AND created>?) "
                "ORDER BY created DESC", (bvid, stream, stream, now - 120)).fetchall()
            window = KEEP_SECONDS if kind == 'auto' else 21600
            for row in rows:
                is_active = row['status'] in ACTIVE_ATTEMPTS
                pair = row['bvid'] == bvid and row['stream'] in (stream, '*')
                if is_active and pair and (row['status'] != 'accepted' or now - row['created'] < window):
                    return {'status': 'duplicate', 'attempt': self._attempt(db, row)}
                if is_active and row['stream'] == stream and now - row['created'] < 120:
                    return {'status': 'blocked', 'reason': 'stream_cooldown'}
            scope = hashlib.sha256(stream.encode()).hexdigest()
            for old in db.execute("SELECT * FROM legacy_manual WHERE scope=? AND bvid=?", (scope, bvid)):
                if old['status'] == 'uncertain' or now - old['created'] < window:
                    return {'status': 'blocked', 'reason': 'legacy_pair_receipt'}
            if kind == 'auto':
                reason = self._auto_limit_reason(db, bvid, stream, now, day, limits)
                if reason:
                    return {'status': 'blocked', 'reason': reason}
            if db.execute("SELECT count(*) FROM attempts").fetchone()[0] >= 2048:
                return {'status': 'blocked', 'reason': 'receipt_capacity'}
            attempt = uuid.uuid4().hex
            status = 'preparing' if preparing else 'sending'
            db.execute(
                "INSERT INTO attempts(id,bvid,stream,kind,request_key,day,created,status,token) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (attempt, bvid, stream, kind, request_key, day, now, status, budget_token))
            return {'status': 'claimed', 'attempt_id': attempt, 'facts': json.loads(video['facts'])}

    def mark_sending(self,attempt:str)->bool:
        with self._transaction() as db:
            return db.execute("UPDATE attempts SET status='sending' WHERE id=? AND status='preparing'",(attempt,)).rowcount==1

    def activate_shadow(self,now:float):
        with self._transaction() as db:
            db.execute("UPDATE videos SET status='deferred',next_at=?,reason='shadow_to_auto' WHERE status='evaluated' AND expires>? AND eval_count<3",(now,now))

    def reopen_for_other_targets(self,bvid:str,now:float,maximum:int,interval:float):
        if maximum<=1:return
        with self._transaction() as db:
            count=db.execute("SELECT count(DISTINCT stream) FROM attempts WHERE bvid=? AND kind IN ('auto','legacy_auto') AND status IN ('sending','accepted','uncertain')",(bvid,)).fetchone()[0]
            if count<maximum:
                db.execute("UPDATE videos SET status='deferred',decision='{}',next_at=?,reason='other_target_requires_new_decision' WHERE bvid=? AND status='accepted' AND expires>? AND eval_count<3",
                    (now+interval,bvid,now+interval))

    def token(self,attempt:str,token:str):
        with self._transaction() as db:
            if db.execute("UPDATE attempts SET token=? WHERE id=? AND status='sending'",(token,attempt)).rowcount!=1:
                raise ValueError('分享预留状态已变化')

    def finish(self, attempt: str, status: str, receipt: dict | None = None, reason: str = ''):
        if status not in ('accepted', 'rejected', 'uncertain'):
            raise ValueError('无效发送状态')
        safe = {
            'message_id': str((receipt or {}).get('message_id') or '')[:160],
            'error_class': clean((receipt or {}).get('error_class'), 100),
        }
        with self._transaction() as db:
            old = db.execute("SELECT bvid,kind,status FROM attempts WHERE id=?", (attempt,)).fetchone()
            if not old:
                raise ValueError('不存在的发送尝试')
            if old['status'] not in ('preparing', 'sending', 'uncertain'):
                return
            history = 'pending' if status == 'accepted' else 'none'
            db.execute(
                "UPDATE attempts SET status=?,receipt=?,reason=?,history=? WHERE id=?",
                (status, json.dumps(safe, ensure_ascii=False), clean(reason, 300), history, attempt))
            if old['kind'] == 'auto':
                db.execute("UPDATE videos SET status=?,reason=? WHERE bvid=?",
                           (status, clean(reason, 300), old['bvid']))

    def accepted_with_tokens(self):
        with self._connection() as db:
            return [dict(r) for r in db.execute("SELECT id,token FROM attempts WHERE status='accepted' AND token<>''")]

    def uncertain_receipts(self, limit: int = 50) -> list[dict]:
        """Read-only uncertain receipts for admin diagnosis; never mutates."""
        with self._connection() as db:
            rows = db.execute(
                "SELECT id,bvid,stream,kind,created,reason,history FROM attempts "
                "WHERE status='uncertain' ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def resolve_uncertain(self, attempt_id: str) -> dict:
        """Admin-verified clearance of a stuck uncertain receipt.

        Marks the attempt known-unsent (rejected) and keeps the row for audit;
        never deletes evidence. The video is set terminal-skipped so automatic
        sharing does not resume by itself — only a deliberate manual re-share
        becomes possible again. Use only after confirming nothing was delivered.
        """
        with self._transaction() as db:
            row = db.execute("SELECT bvid,kind,status FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                return {'success': False, 'reason': 'not_found'}
            if row['status'] != 'uncertain':
                return {'success': False, 'reason': 'not_uncertain', 'status': row['status']}
            db.execute("UPDATE attempts SET status='rejected',reason='manual_verified_unsent' "
                       "WHERE id=? AND status='uncertain'", (attempt_id,))
            db.execute("UPDATE videos SET status='skipped',reason='manual_cleared' "
                       "WHERE bvid=? AND status='uncertain'", (row['bvid'],))
            return {'success': True, 'bvid': row['bvid'], 'kind': row['kind']}

    def claim_history(self,attempt:str)->dict|None:
        with self._transaction() as db:
            row=db.execute("SELECT a.*,v.facts FROM attempts a JOIN videos v USING(bvid) WHERE a.id=? AND a.status='accepted' AND a.history='pending'",(attempt,)).fetchone()
            if not row:return None
            db.execute("UPDATE attempts SET history='appending' WHERE id=?",(attempt,))
            out=dict(row);out['facts']=json.loads(out['facts']);return out

    def finish_history(self,attempt:str,state:str):
        if state not in ('synced','rejected','uncertain'):raise ValueError('无效同步状态')
        with self._transaction() as db:db.execute('UPDATE attempts SET history=? WHERE id=? AND history=\'appending\'',(state,attempt))

    def context_facts(self,stream:str,now:float)->list[dict]:
        with self._connection() as db:
            rows=db.execute("SELECT a.id,a.created,a.receipt,v.facts FROM attempts a JOIN videos v USING(bvid) WHERE a.stream=? AND a.status='accepted' AND a.created>? ORDER BY a.created DESC LIMIT 3",(stream,now-KEEP_SECONDS)).fetchall()
            return [{'id':r['id'],'at':r['created'],**{k:v for k,v in json.loads(r['facts']).items() if k in ('bvid','title')}} for r in rows]

    def profile(self,stream:str)->dict:
        with self._connection() as db:
            row=db.execute('SELECT doc FROM profiles WHERE stream=?',(stream,)).fetchone()
            return json.loads(row['doc']) if row else {}

    def save_profile(self,stream:str,doc:dict):
        with self._transaction() as db:
            db.execute('INSERT INTO profiles VALUES(?,?) ON CONFLICT(stream) DO UPDATE SET doc=excluded.doc',
                       (stream,json.dumps(doc,ensure_ascii=False)))

    def restrict_profiles(self,streams:list[str]):
        with self._transaction() as db:
            allowed=set(streams)
            for row in db.execute('SELECT stream FROM profiles').fetchall():
                if row['stream'] not in allowed:db.execute('DELETE FROM profiles WHERE stream=?',(row['stream'],))

    def import_legacy(self,base:Path,now:float):
        """Read once, never rewrite original receipts. Bad legacy data fails closed."""
        with self._transaction() as db:
            if self._meta(db,'legacy_imported'):return
            path=base/'video_watch/share_candidates.json';old=json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
            recent=base/'recent_video_share_receipts.json';manual=json.loads(recent.read_text(encoding='utf-8-sig')) if recent.exists() else {}
            if not isinstance(old,dict) or not isinstance(manual,dict):raise ValueError('旧分享回执损坏')
            for document in (old,manual):
                if 'version' in document and (type(document['version']) is not int or document['version']!=1):
                    raise ValueError('未知旧分享数据版本')
            rows=old.get('candidates',[]);receipts=manual.get('receipts',{})
            if not isinstance(rows,list) or not isinstance(receipts,dict):raise ValueError('旧分享回执形态错误')
            daily=old.get('daily',{})
            if not isinstance(daily,dict):raise ValueError('旧分享额度损坏')
            if daily.get('date') and not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(daily['date'])):
                raise ValueError('旧分享额度日期损坏')
            for key in ('sent_count','held_count'):
                if key in daily and (type(daily[key]) is not int or daily[key]<0):
                    raise ValueError('旧分享额度计数损坏')
            for key in ('last_sent_epoch','last_attempt_epoch'):
                value=daily.get(key)
                if value is not None and (number(value,-1)!=value or value<0):
                    raise ValueError('旧分享时间损坏')
            self._set(db,'legacy_daily',json.dumps(daily))
            for row in rows:
                if not isinstance(row,dict) or not BV.fullmatch(str(row.get('bvid') or '')):
                    raise ValueError('旧视频身份损坏')
                if row.get('status') not in ('pending','replaced','expired','sent','offered','sending','inflight','dropped','uncertain'):
                    raise ValueError('未知旧分享状态')
                facts={k:row.get(k,'') for k in ('bvid','title','summary','impression','cover_url')}
                facts['evidence']='legacy_visual';created=number(row.get('created_at'),now);expires=number(row.get('expire_at'),created+7200)
                state=row.get('status');status='observed' if state=='pending' and expires>now else 'skipped'
                db.execute('INSERT OR IGNORE INTO videos(bvid,facts,created,expires,status,next_at) VALUES(?,?,?,?,?,?)',
                    (facts['bvid'],json.dumps(facts,ensure_ascii=False),created,expires,status,now))
                if state in ('sent','offered','sending','inflight','uncertain') or row.get('uncertain'):
                    status='accepted' if state=='sent' else 'uncertain'
                    aid='legacy-'+str(row.get('share_id') or uuid.uuid4().hex)
                    db.execute("INSERT OR IGNORE INTO attempts(id,bvid,stream,kind,request_key,day,created,status,history,reason) VALUES(?,?,?,'legacy_auto',?,?,?,?,'none','legacy_receipt')",
                        (aid,facts['bvid'],str(row.get('target_stream') or '*'),aid,str((old.get('daily') or {}).get('date') or ''),created,status))
            for row in receipts.values():
                if not isinstance(row,dict):raise ValueError('旧手动回执损坏')
                if (not re.fullmatch(r'[0-9a-f]{64}',str(row.get('scope') or ''))
                        or not BV.fullmatch(str(row.get('bvid') or ''))):
                    raise ValueError('旧手动回执身份损坏')
                state='accepted' if row.get('state')=='accepted' else 'uncertain'
                db.execute('INSERT INTO legacy_manual VALUES(?,?,?,?)',(row['scope'],row['bvid'],state,number(row.get('at'),now)))
            self._set(db,'legacy_imported',now)

    def prune(self,now:float):
        with self._transaction() as db:
            db.execute("DELETE FROM attempts WHERE status IN ('accepted','rejected') AND created<?",(now-KEEP_SECONDS,))
            db.execute('DELETE FROM videos WHERE created<? AND bvid NOT IN (SELECT bvid FROM attempts)',(now-KEEP_SECONDS,))
            db.execute("DELETE FROM legacy_manual WHERE status='accepted' AND created<?",(now-KEEP_SECONDS,))

    def snapshot(self)->dict:
        with self._connection() as db:
            counts={r['status']:r['n'] for r in db.execute('SELECT status,count(*) n FROM videos GROUP BY status')}
            attempts=[dict(r) for r in db.execute('SELECT id,bvid,stream,kind,created,status,history,reason FROM attempts ORDER BY created DESC LIMIT 10')]
            candidates=[self._video(r) for r in db.execute('SELECT * FROM videos ORDER BY created DESC LIMIT 8')]
            return {'candidate_states':counts,'attempts':attempts,'candidates':candidates,
                    'profile_count':db.execute('SELECT count(*) FROM profiles').fetchone()[0],
                    'legacy_imported':bool(self._meta(db,'legacy_imported'))}
