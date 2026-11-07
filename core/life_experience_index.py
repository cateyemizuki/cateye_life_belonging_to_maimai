"""生活经历索引（PR-B1，2026-11-07）：轻量跨域事实层，纯旁路。

只存事实与引用（计划书 §4/§5.2/§6）：不承担执行、副作用或人格大模型循环；
主模块依旧拥有业务数据和执行权。任何失败都不得拖挂主业务——写入方自行
try/except，本模块的 record 对非法输入/开关关闭一律返回 False 不抛异常。

真伪分层红线（计划书 §5.2，写入方必须显式给 type，缺省拒绝）：
- observed            真实发生（视频真实看完/用户真实来信/睡眠会话事实）；
- simulated_activity  插件虚拟日程活动（不可声称现实操作）；
- inferred            低置信推测（只作动机）；
- published           平台明确成功回执（可记得自己发过，不等于别人看到）。

隐私红线：summary 只存 ≤300 字结构化摘要与来源引用，不复制原始聊天正文/图片。
连接/事务风格同 integrations/video_share_ledger.py（独立连接 + BEGIN IMMEDIATE，
WAL，RLock 串行；本模块在 core，不 import 任何业务模块——D5 结构守卫口径）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: 四层真伪分层（计划书 §5.2）；写入方必须显式给 type，缺省/非法拒绝
EXPERIENCE_TYPES = ('observed', 'simulated_activity', 'inferred', 'published')
#: 容量上限（规格 §1：超出淘汰最旧低 salience）
MAX_ROWS = 2000
#: 摘要长度上限（规格 §1：≤300 字，不复制原始正文）
SUMMARY_MAX = 300
#: TTL 默认/夹取（与 [life_state].experience_ttl_days 同口径）
TTL_DAYS_DEFAULT = 14
#: recent 只读查询的硬上限（有界只读端口）
RECENT_LIMIT_MAX = 100
#: 总线订阅 owner（停机 unsubscribe_all 用）
BUS_OWNER = 'life_experience_index'


def _clip(value: Any, limit: int) -> str:
    return str(value if value is not None else '').strip()[:limit]


def _unit(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


class LifeExperienceIndex:
    """SQLite 事实索引：record / recent / by_goal / expire / count，全部有界。"""

    def __init__(self, path: Any, *, enabled_provider: Optional[Callable[[], bool]] = None,
                 ttl_days_provider: Optional[Callable[[], int]] = None,
                 capacity: int = MAX_ROWS):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise OSError('经历索引数据路径不可用')
        self._enabled_provider = enabled_provider
        self._ttl_days_provider = ttl_days_provider
        self._capacity = max(1, min(int(capacity), MAX_ROWS))
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
              CREATE TABLE IF NOT EXISTS experiences(
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                dedupe_key TEXT NOT NULL UNIQUE,
                summary TEXT NOT NULL DEFAULT '',
                outcome TEXT NOT NULL DEFAULT '',
                salience REAL NOT NULL DEFAULT 0.5,
                confidence REAL NOT NULL DEFAULT 0.5,
                related_goal_id TEXT,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL);
              CREATE INDEX IF NOT EXISTS exp_kind_created ON experiences(kind, created_at);
              CREATE INDEX IF NOT EXISTS exp_goal ON experiences(related_goal_id);
              CREATE INDEX IF NOT EXISTS exp_expires ON experiences(expires_at);
            ''')
            db.commit()

    # ------------------------------------------------------------------
    # 连接/事务（同 ShareLedger 风格）
    # ------------------------------------------------------------------
    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def _transaction(self):
        with self._lock, self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return False  # 配置不可读保守停记，绝不拖挂主业务

    def _ttl_seconds(self) -> float:
        days = TTL_DAYS_DEFAULT
        if self._ttl_days_provider is not None:
            try:
                days = int(self._ttl_days_provider())
            except Exception:
                days = TTL_DAYS_DEFAULT
        return max(1, min(90, days)) * 86400.0

    # ------------------------------------------------------------------
    # 写入（dedupe 冲突返回 False；开关关闭零记录）
    # ------------------------------------------------------------------
    def record(self, *, type: str, kind: str, source_ref: str, dedupe_key: str,
               summary: str, outcome: str = '', salience: float = 0.5,
               confidence: float = 0.5, related_goal_id: Optional[str] = None,
               now: Optional[float] = None) -> bool:
        """记一条事实。type 为必填关键字参数（缺省 TypeError 拒绝）；
        type 非法/必填键为空/开关关闭 → False。同一 dedupe_key 不重复记账。"""
        if type not in EXPERIENCE_TYPES:
            return False
        kind = _clip(kind, 80)
        dedupe_key = _clip(dedupe_key, 300)
        if not kind or not dedupe_key:
            return False
        if not self._enabled():
            return False
        try:
            created = float(now) if now is not None else time.time()
        except (TypeError, ValueError):
            return False
        row = (
            'exp_' + uuid.uuid4().hex, type, kind, _clip(source_ref, 300), dedupe_key,
            _clip(summary, SUMMARY_MAX), _clip(outcome, 120),
            _unit(salience, 0.5), _unit(confidence, 0.5),
            _clip(related_goal_id, 160) or None, created, created + self._ttl_seconds(),
        )
        try:
            with self._transaction() as db:
                count = db.execute('SELECT count(*) FROM experiences').fetchone()[0]
                if count >= self._capacity:
                    # 超出容量：淘汰最旧低 salience（不挤掉高重要度事实）
                    db.execute(
                        'DELETE FROM experiences WHERE id IN ('
                        'SELECT id FROM experiences ORDER BY salience ASC, created_at ASC '
                        'LIMIT ?)', (count - self._capacity + 1,))
                db.execute(
                    'INSERT INTO experiences(id,type,kind,source_ref,dedupe_key,summary,'
                    'outcome,salience,confidence,related_goal_id,created_at,expires_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', row)
            return True
        except sqlite3.IntegrityError:
            return False  # dedupe_key UNIQUE 冲突：同一事件不重复记账
        except Exception as exc:
            logger.warning('经历索引写入失败（纯旁路，忽略）：%s', exc)
            return False

    def _has_dedupe(self, dedupe_key: str) -> bool:
        with self._connection() as db:
            return db.execute('SELECT 1 FROM experiences WHERE dedupe_key=?',
                              (dedupe_key,)).fetchone() is not None

    # ------------------------------------------------------------------
    # 有界只读查询（开关关闭时已有数据仍保留只读——规格 §4）
    # ------------------------------------------------------------------
    def recent(self, kinds: Optional[list] = None, limit: int = 20,
               now: Optional[float] = None) -> list[dict]:
        """新→旧返回未过期条目；limit 夹取 1..RECENT_LIMIT_MAX；kinds 空=不限。"""
        try:
            limit = max(1, min(RECENT_LIMIT_MAX, int(limit)))
        except (TypeError, ValueError):
            limit = 20
        moment = float(now) if now is not None else time.time()
        sql = 'SELECT * FROM experiences WHERE expires_at>? '
        params: list[Any] = [moment]
        names = [_clip(k, 80) for k in (kinds or []) if _clip(k, 80)]
        if names:
            sql += 'AND kind IN (%s) ' % ','.join('?' for _ in names)
            params.extend(names)
        sql += 'ORDER BY created_at DESC, id DESC LIMIT ?'
        params.append(limit)
        with self._connection() as db:
            return [dict(r) for r in db.execute(sql, params)]

    def by_goal(self, goal_id: str, limit: int = 50,
                now: Optional[float] = None) -> list[dict]:
        goal_id = _clip(goal_id, 160)
        if not goal_id:
            return []
        limit = max(1, min(RECENT_LIMIT_MAX, int(limit)))
        moment = float(now) if now is not None else time.time()
        with self._connection() as db:
            return [dict(r) for r in db.execute(
                'SELECT * FROM experiences WHERE related_goal_id=? AND expires_at>? '
                'ORDER BY created_at DESC, id DESC LIMIT ?', (goal_id, moment, limit))]

    def expire(self, now: Optional[float] = None) -> int:
        """删除已过期条目，返回删除数（TTL 维护；失败返回 0 不抛）。"""
        moment = float(now) if now is not None else time.time()
        try:
            with self._transaction() as db:
                return db.execute('DELETE FROM experiences WHERE expires_at<=?',
                                  (moment,)).rowcount
        except Exception as exc:
            logger.warning('经历索引过期清理失败（忽略）：%s', exc)
            return 0

    def count(self) -> int:
        with self._connection() as db:
            return db.execute('SELECT count(*) FROM experiences').fetchone()[0]

    # ------------------------------------------------------------------
    # 跨域事实便捷口（证据闸在装配侧/主模块；本层只做防御性复核）
    # ------------------------------------------------------------------
    def record_watched_video(self, facts: Optional[dict], episode_id: str = '', *,
                             task_id: str = '', now: Optional[float] = None) -> bool:
        """观看完成事实（observed/watched_video）。facts 必须是主模块证据闸
        （integrations.video_share_policy.watched_facts，装配侧调用）产出的可信
        事实 dict；None/缺 bvid/title/evidence 一律不记（无证据不记）。"""
        if not isinstance(facts, dict):
            return False
        bvid = _clip(facts.get('bvid'), 40)
        title = _clip(facts.get('title'), 120)
        evidence = _clip(facts.get('evidence'), 40)
        if not bvid or not title or not evidence:
            return False
        task_id = _clip(task_id, 160)
        watched_at = facts.get('watched_at')
        key = task_id or f"{bvid}:{int(watched_at or now or time.time())}"
        summary = f"看完视频 {bvid}《{title}》"
        impression = _clip(facts.get('impression'), 160)
        if impression:
            summary += f"；观后感：{impression}"
        return self.record(
            type='observed', kind='watched_video',
            source_ref=f"video_watch:{_clip(episode_id, 120) or key}",
            dedupe_key=f"video_watch:{key}", summary=summary, outcome='completed',
            salience=0.6, confidence=0.9 if evidence == 'visual' else 0.7, now=now)

    def record_watched_video_published(self, candidate: Optional[dict], tid: str, *,
                                       now: Optional[float] = None) -> bool:
        """空间发布事实（published/qzone_posted，dedupe qzone:{tid}）。
        幂等成功语义：新入账或同 tid 已在账都返回 True（供 A3 fact_sync=synced）；
        开关关闭/入参非法/写入失败返回 False（留 pending 由 A3 有界重试）。
        published 只表示平台明确成功回执，不等于别人看到/送达。"""
        tid = _clip(tid, 160)
        if not tid or not self._enabled():
            return False
        candidate = candidate if isinstance(candidate, dict) else {}
        facts = candidate.get('facts') if isinstance(candidate.get('facts'), dict) else {}
        bvid = _clip(candidate.get('bvid') or facts.get('bvid'), 40)
        title = _clip(facts.get('title'), 120)
        candidate_id = _clip(candidate.get('candidate_id'), 160)
        summary = f"向QQ空间发布了视频说说：{bvid}《{title}》（tid={tid}）"
        ok = self.record(
            type='published', kind='qzone_posted',
            source_ref=f"qzone_video:{candidate_id or bvid}:{tid}",
            dedupe_key=f"qzone:{tid}", summary=summary, outcome='posted',
            salience=0.6, confidence=1.0, now=now)
        if ok:
            return True
        try:
            return self._has_dedupe(f"qzone:{tid}")  # 已在账（总线先记）= 幂等成功
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 事件订阅（统一入口；handler 异常隔离，索引失败绝不影响主业务）
    # ------------------------------------------------------------------
    def attach_bus(self, bus: Any) -> list:
        """把索引挂到既有 EventBus（鸭子类型，core 不 import integrations）。
        返回反订阅闭包列表；bus 缺 subscribe 能力时返回空列表（保守降级）。"""
        subscribe = getattr(bus, 'subscribe', None)
        if not callable(subscribe):
            return []
        pairs = (
            ('qzone.posted', self._on_qzone_posted),
            ('schedule.activity_ended', self._on_activity_ended),
            ('schedule.sleep_started', self._on_sleep_started),
            ('schedule.woke_up', self._on_woke_up),
            ('relation.message_received', self._on_message_received),
        )
        closures = []
        for event, handler in pairs:
            try:
                closures.append(subscribe(event, handler, owner=BUS_OWNER))
            except Exception as exc:
                logger.warning('经历索引订阅 %s 失败（忽略）：%s', event, exc)
        return closures

    async def _on_qzone_posted(self, payload: dict) -> None:
        tid = _clip(payload.get('tid'), 160)
        if not tid:
            return
        summary = _clip(payload.get('summary'), SUMMARY_MAX) or f"向QQ空间发布了一条说说（tid={tid}）"
        self.record(type='published', kind='qzone_posted',
                    source_ref=f"qzone.posted:{tid}", dedupe_key=f"qzone:{tid}",
                    summary=summary, outcome='posted', salience=0.6, confidence=1.0)

    async def _on_activity_ended(self, payload: dict) -> None:
        # 现总线 payload 只有 name/goal_type/ts（无 episode id）：用 ts+name 做
        # 幂等键（同一事件重放不重复入账）；无延续价值活动也记，salience 低。
        name = _clip(payload.get('name'), 80)
        if not name:
            return
        ts = _clip(payload.get('ts'), 40)
        goal_type = _clip(payload.get('goal_type'), 40)
        self.record(type='simulated_activity', kind='activity_done',
                    source_ref=f"schedule.activity_ended:{ts}",
                    dedupe_key=f"episode:{ts}:{name}:ended",
                    summary=f"完成了日程活动「{name}」（{goal_type or '日常'}，虚拟日程）",
                    outcome='ended', salience=0.2, confidence=0.9)

    async def _on_sleep_started(self, payload: dict) -> None:
        ts = _clip(payload.get('ts'), 40)
        date = _clip(payload.get('date'), 20)
        hours = _clip(payload.get('sleep_hours'), 12)
        self.record(type='observed', kind='sleep_session',
                    source_ref=f"schedule.sleep_started:{ts}",
                    dedupe_key=f"sleep:{date}:{ts}:started",
                    summary=f"进入睡眠会话（计划 {hours or '?'} 小时）",
                    outcome='started', salience=0.4, confidence=1.0)

    async def _on_woke_up(self, payload: dict) -> None:
        ts = _clip(payload.get('ts'), 40)
        date = _clip(payload.get('date'), 20)
        hours = _clip(payload.get('sleep_hours'), 12)
        self.record(type='observed', kind='sleep_session',
                    source_ref=f"schedule.woke_up:{ts}",
                    dedupe_key=f"sleep:{date}:{ts}:woke",
                    summary=f"醒来（实际睡眠约 {hours or '?'} 小时）",
                    outcome='woke', salience=0.4, confidence=1.0)

    async def _on_message_received(self, payload: dict) -> None:
        # 只存「收到某用户消息」的事实引用与时间，不存正文（隐私红线）。
        # 现总线 payload 只有 is_target/stream_kind/ts（无 user_id/正文）。
        ts = _clip(payload.get('ts'), 40)
        stream_kind = _clip(payload.get('stream_kind'), 20) or 'unknown'
        is_target = bool(payload.get('is_target'))
        who = '目标陪伴对象' if is_target else '其他用户'
        self.record(type='observed', kind='user_message',
                    source_ref=f"relation.message_received:{ts}",
                    dedupe_key=f"message:{stream_kind}:{ts}",
                    summary=f"收到{who}的一条消息（{stream_kind}；正文不落库）",
                    outcome='received', salience=0.5 if is_target else 0.3, confidence=1.0)
