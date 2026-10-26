"""日程 SQLite 持久化（来源：自主规划插件 database/goal_db.py + 原 schedule_db 合并演进）。

三张表（v4 起）：
- ``schedule_items``：按天的日程条目（ScheduleQuery / get_current_activity API 的数据源）；
  v4 增列 ``cycle_start_ts``/``cycle_end_ts``（INTEGER，**代码盖戳**，LLM 不参与；日帧
  分钟，跨 0 点照取 >1440 的环节点、不折回——§31.2 血缘）与 ``entry_reason``（TEXT：
  ``tool_edit``/``regenerate``/``scheduled_generate``/``fallback_template`` 四种进账原因，
  迁移补戳行为 ``legacy_v3``）；
- ``schedule_archive``（v4 伴随表，纯新增）：进账前旧日行归档——行快照 + 其 cycle 戳 +
  reason + 归档时刻（``archived_at``）与归档原因（``archive_reason`` = 引发归档的本次
  进账 reason）；**失效时机（cleanup_old_days 保留窗）不变，归档不改任何读取行为**；
- ``goals``：长期目标 + 未来约定（pending_commitment，goal_type 区分），
  含 conditions / parameters / deadline / progress / execution_count 全字段。
  ``GoalManager``（goal_manager.py）基于该表工作，并会把当日日程条目合成
  为带 time_window 的 Goal 视图（单一数据源，避免双写漂移）；
- ``activity_episodes``（v5 纯新增表，附录 B.1.1 全字段 DDL 原样）：活动档案/Episode 日志
  ——记录实际发生（planned/actual、status/progress、打断/恢复/放弃与决策依据 metadata）。
  **只增不改**：``schedule_items`` / ``goals`` / ``schedule_archive`` 等旧表一字不动；
  v4→v5 迁移（``_migrate_schema_v5``）失败只告警不抛、不影响旧表读取（schema_version
  保持旧值，下次初始化重试）。planned_*/actual_* 允许 >1440、``day_offset`` 与
  ``schedule_items`` 同口径（§7.8 跨午夜条目口径）。

损坏自愈：初始化遇到 malformed/not a database 时隔离旧文件重建空库。

R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cycle_state import REASON_LEGACY
from .time_utils import DAY_MINUTES

logger = logging.getLogger(__name__)

_CORRUPTION_KEYWORDS = ("malformed", "not a database", "encrypted", "corrupt")

# v5 新增表（附录 B.1.1 全字段 DDL 原样，只增不改；不放 SCHEMA 主脚本是为了故障隔离：
# 本块任何失败都不影响旧表的建表/读取——设计附录 B.1.3「迁移失败不影响旧库读取」）。
_EPISODES_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_episodes (
    episode_id           TEXT PRIMARY KEY,
    schedule_date        TEXT NOT NULL,
    day_offset           INTEGER,                -- 与 schedule_items 同口径；跨午夜用
    source               TEXT NOT NULL,          -- spine | rolling | commitment | random | resume | manual
    name                 TEXT NOT NULL,
    goal_type            TEXT NOT NULL,
    property_id          TEXT,
    planned_start_minute INTEGER NOT NULL,
    planned_end_minute   INTEGER NOT NULL,
    actual_start_minute  INTEGER,
    actual_end_minute    INTEGER,
    status               TEXT NOT NULL,          -- planned | active | completed | interrupted | paused | abandoned | skipped
    progress             REAL NOT NULL DEFAULT 0, -- 0..1
    interruptibility      REAL,
    switch_cost          REAL,
    attention_mode       TEXT,                   -- now | brief | later
    interrupted_by       TEXT,
    resume_of            TEXT,
    state_before         TEXT,                   -- JSON
    state_after          TEXT,                   -- JSON
    metadata             TEXT,                   -- JSON: reason/top3/seed/...
    created_at           TEXT NOT NULL,
    updated_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_episodes_date_start
    ON activity_episodes(schedule_date, planned_start_minute);
CREATE INDEX IF NOT EXISTS idx_episodes_status
    ON activity_episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_resume
    ON activity_episodes(resume_of);
"""

# 附加约束（超出附录三索引，保守裁量）：表达式部分唯一索引强制「任意时刻最多一个
# active episode」——非 active 行取 NULL（UNIQUE 索引中 NULL 互不冲突），active 行取 1
# （重复即冲突）。写路径先关后开（_close_active_episodes_locked），本索引做底层兜底。
_EPISODE_ACTIVE_SINGLETON_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_active_singleton"
    " ON activity_episodes(CASE WHEN status = 'active' THEN 1 ELSE NULL END)"
)

#: 离散活动 goal_type 集合（附录 §5.5：吃饭/洗漱等离散活动完成即 1.0）。
#: 内置保守只含 meal；其余离散活动由调用方 finish(..., discrete=True) 显式标记。
EPISODE_DISCRETE_GOAL_TYPES = frozenset({"meal"})

#: 默认进度上限（附录 §5.5：已用时长/计划时长，上限 0.95）。
_EPISODE_PROGRESS_CAP = 0.95


class ScheduleDB:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS schedule_items (
        item_id TEXT PRIMARY KEY,
        schedule_date TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        goal_type TEXT NOT NULL DEFAULT 'daily_routine',
        priority TEXT NOT NULL DEFAULT 'medium',
        start_minute INTEGER NOT NULL DEFAULT 0,
        end_minute INTEGER NOT NULL DEFAULT 60,
        day_offset INTEGER,
        parameters TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT,
        cycle_start_ts INTEGER,
        cycle_end_ts INTEGER,
        entry_reason TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule_items(schedule_date);
    CREATE TABLE IF NOT EXISTS schedule_archive (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT,
        schedule_date TEXT NOT NULL,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        goal_type TEXT NOT NULL DEFAULT 'daily_routine',
        priority TEXT NOT NULL DEFAULT 'medium',
        start_minute INTEGER NOT NULL DEFAULT 0,
        end_minute INTEGER NOT NULL DEFAULT 60,
        day_offset INTEGER,
        parameters TEXT,
        created_at TEXT,
        updated_at TEXT,
        cycle_start_ts INTEGER,
        cycle_end_ts INTEGER,
        entry_reason TEXT,
        archived_at TEXT NOT NULL,
        archive_reason TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_archive_date ON schedule_archive(schedule_date);
    CREATE INDEX IF NOT EXISTS idx_schedule_archive_archived ON schedule_archive(archived_at);
    CREATE TABLE IF NOT EXISTS goals (
        goal_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT NOT NULL,
        goal_type TEXT NOT NULL,
        priority TEXT NOT NULL,
        creator_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        deadline TEXT,
        interval_seconds INTEGER,
        conditions TEXT,
        parameters TEXT,
        progress INTEGER DEFAULT 0,
        last_executed_at TEXT,
        execution_count INTEGER DEFAULT 0,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_goals_chat_id ON goals(chat_id);
    CREATE INDEX IF NOT EXISTS idx_goals_status ON goals(status);
    CREATE INDEX IF NOT EXISTS idx_goals_created_at ON goals(created_at);
    CREATE INDEX IF NOT EXISTS idx_goals_chat_status ON goals(chat_id, status);
    CREATE INDEX IF NOT EXISTS idx_goals_chat_status_time ON goals(chat_id, status, created_at);
    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_schema()

    # ---- 连接与自愈 ----
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            return conn
        except sqlite3.DatabaseError:
            # 失败也要关掉连接：Windows 上不释放句柄，隔离重命名就无法进行
            try:
                conn.close()
            except sqlite3.Error:
                pass
            raise

    def _init_schema(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._lock:
                self._conn = self._connect()
                self._conn.executescript(self.SCHEMA)
                self._migrate_schema_v3()
                self._migrate_schema_v4()
                # v4→v5（R7 依据 = 用户 2026-10-25 直派，设计附录 B.1.3）：迁移失败不影响
                # 旧表读取——失败只告警，schema_version 保持旧值，下次初始化重试。
                if self._migrate_schema_v5():
                    version = "5"
                else:
                    row = self._conn.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()
                    version = str(row["value"]) if row and row["value"] else "4"
                self._conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                    (version,),
                )
                self._conn.commit()
        except sqlite3.DatabaseError as e:
            if not any(k in str(e).lower() for k in _CORRUPTION_KEYWORDS):
                raise
            self._quarantine_corrupt_files()
            with self._lock:
                self._conn = self._connect()
                self._conn.executescript(self.SCHEMA)
                self._migrate_schema_v5()
                self._conn.commit()

    def _migrate_schema_v3(self) -> None:
        """v2→v3 增列迁移（A-16 裁决① 2026-10-11）：schedule_items 加 ``day_offset``。

        只对旧库补列（CREATE TABLE IF NOT EXISTS 对已存在的表不会加列）；缺省 NULL =
        旧行为（读取侧按未标记的环口径判定），不回填不迁移行数据——向后兼容。
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(schedule_items)")}
        if "day_offset" not in columns:
            self._conn.execute("ALTER TABLE schedule_items ADD COLUMN day_offset INTEGER")

    def _migrate_schema_v4(self) -> None:
        """v3→v4 增列迁移（§31.2 血缘，R7 = 用户 2026-10-23 第二轮直派）：

        - ``schedule_items`` 加 ``cycle_start_ts`` / ``cycle_end_ts``（INTEGER）与
          ``entry_reason``（TEXT）；``schedule_archive`` 伴随表由 SCHEMA 建齐；
        - 旧行补戳 = ``min(start)`` / ``max(end)`` 环（按 ``schedule_date`` 分组，逐行盖同
          一份包络——跨 0 点照取 >1440 的环节点、不折回）且 ``entry_reason='legacy_v3'``
          （§31.2 legacy 库迁移口径；与本周轮新进账——由进账漏斗给的签名参数盖戳——区分开）。
        """
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(schedule_items)")}
        if "cycle_start_ts" not in columns:
            self._conn.execute("ALTER TABLE schedule_items ADD COLUMN cycle_start_ts INTEGER")
        if "cycle_end_ts" not in columns:
            self._conn.execute("ALTER TABLE schedule_items ADD COLUMN cycle_end_ts INTEGER")
        if "entry_reason" not in columns:
            self._conn.execute("ALTER TABLE schedule_items ADD COLUMN entry_reason TEXT")
        need_backfill = self._conn.execute(
            "SELECT COUNT(*) FROM schedule_items WHERE cycle_start_ts IS NULL"
            " OR cycle_end_ts IS NULL OR entry_reason IS NULL"
        ).fetchone()[0]
        if not need_backfill:
            return
        groups: List[Tuple[Any, Any, Any]] = self._conn.execute(
            "SELECT schedule_date, MIN(start_minute), MAX(end_minute) FROM schedule_items"
            " GROUP BY schedule_date"
        ).fetchall()
        for schedule_date, min_start, max_end in groups:
            self._conn.execute(
                "UPDATE schedule_items SET cycle_start_ts = ?, cycle_end_ts = ?,"
                " entry_reason = ? WHERE schedule_date = ? AND (cycle_start_ts IS NULL"
                " OR cycle_end_ts IS NULL OR entry_reason IS NULL)",
                (min_start, max_end, REASON_LEGACY, schedule_date),
            )

    def _migrate_schema_v5(self) -> bool:
        """v4→v5 迁移（R7 依据 = 用户 2026-10-25 直派【日程系统滚动化改造，设计附录 B.1.1/B.1.3】）：

        纯新增 ``activity_episodes`` 表 + 三索引（附录 B.1.1 全字段 DDL 原样）+ 附加的
        active 单例部分唯一索引。**只增不改**：旧表（schedule_items / schedule_archive /
        goals / metadata）零改动；旧库升级即建新表，旧行零迁移（episode 是全新数据）。
        全程 try/except 隔离——任何失败告警不抛、返回 False（调用方保持 schema_version
        旧值，下次初始化重试），旧表读取不受影响。
        """
        try:
            self._conn.executescript(_EPISODES_SCHEMA)
        except sqlite3.Error as e:
            logger.warning("schedule_db: v5 迁移建 activity_episodes 失败（不影响旧表）：%s", e)
            return False
        try:
            self._conn.execute(_EPISODE_ACTIVE_SINGLETON_INDEX_SQL)
        except sqlite3.Error as e:
            # 表与三索引已建齐；仅附加单例索引未成——不阻断 v5，告警留痕
            logger.warning("schedule_db: v5 附加单例索引建立失败（不影响旧表）：%s", e)
        return True

    def _quarantine_corrupt_files(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            src = Path(self._db_path + suffix)
            if src.exists():
                try:
                    src.rename(Path(self._db_path + suffix + f".corrupt-{stamp}"))
                except OSError:
                    try:
                        src.unlink()
                    except OSError:
                        pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ================================================================
    # 日程条目（schedule_items）CRUD —— 兼容契约，签名不变
    # ================================================================
    @staticmethod
    def _item_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        try:
            data["parameters"] = json.loads(data.get("parameters") or "{}")
        except (json.JSONDecodeError, TypeError):
            data["parameters"] = {}
        return data

    @staticmethod
    def _cycle_stamps_of(items: List[Dict[str, Any]]) -> Tuple[Optional[int], Optional[int]]:
        """条目集合 → ``min(start)``/``max(end)`` 日帧分钟包络（跨 0 点照取 >1440、不折回）。

        调用方未显式传包络（``cycle_start_ts``/``cycle_end_ts``）时的兜底口径（§31.2
        「旧行补戳 = min(start)/max(end) 环」同款；进账漏斗给的包络是入睡并入形态，
        见 planning/cycle_state.py envelope_minutes）。全部缺分钟 → ``(None, None)``。
        """
        starts: List[int] = []
        ends: List[int] = []
        for item in items or []:
            for key, bucket in (("start_minute", starts), ("end_minute", ends)):
                value = item.get(key)
                if value is None:
                    continue
                try:
                    bucket.append(int(value))
                except (TypeError, ValueError):
                    continue
        if not starts or not ends:
            return None, None
        return min(starts), max(ends)

    def _archive_day_rows_locked(self, schedule_date: str, archive_reason: str,
                                 now: str) -> int:
        """进账前旧日行归档（只在持有连接锁/事务内调用；失败不抛——归档是纯新增）。

        快照 = 行全字段 + 其 cycle 戳与原 ``entry_reason`` + ``archived_at``（归档时刻）
        + ``archive_reason``（引发归档的本次进账 reason）。返回归档行数。
        """
        rows = self._conn.execute(
            "SELECT * FROM schedule_items WHERE schedule_date = ?", (schedule_date,)
        ).fetchall()
        archived = 0
        for row in rows:
            try:
                self._conn.execute(
                    "INSERT INTO schedule_archive(item_id, schedule_date, name, description,"
                    " goal_type, priority, start_minute, end_minute, day_offset, parameters,"
                    " created_at, updated_at, cycle_start_ts, cycle_end_ts, entry_reason,"
                    " archived_at, archive_reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["item_id"], row["schedule_date"], row["name"], row["description"],
                     row["goal_type"], row["priority"], row["start_minute"], row["end_minute"],
                     row["day_offset"], row["parameters"], row["created_at"], row["updated_at"],
                     row["cycle_start_ts"], row["cycle_end_ts"], row["entry_reason"],
                     now, archive_reason),
                )
                archived += 1
            except (sqlite3.SqliteError, IndexError, TypeError, KeyError):
                continue
        return archived

    def replace_day_schedule(
        self,
        schedule_date: str,
        items: List[Dict[str, Any]],
        entry_reason: str = REASON_LEGACY,
        cycle_start_ts: Optional[int] = None,
        cycle_end_ts: Optional[int] = None,
    ) -> int:
        """整体替换某天的日程（生成成功后落库）。返回写入条数。

        v4 签名（§31.2 血缘）：``entry_reason`` = 四种进账原因之一
        （``tool_edit``/``regenerate``/``scheduled_generate``/``fallback_template``），
        未按新管线传原因的旧调用方/直调以 ``legacy_v3`` 如实登记；``cycle_start_ts`` /
        ``cycle_end_ts`` = 进账漏斗包络（入睡并入口径），未传时按 ``min(start)`` /
        ``max(end)`` 本地兜底。进账前先把同日旧行整体归档进 ``schedule_archive``
        （行快照 + cycle 戳 + reason + 归档时刻；**失效时机不变，归档是纯新增**）。
        """
        now = datetime.now().isoformat()
        stamps = self._cycle_stamps_of(items)
        start_stamp = int(cycle_start_ts) if cycle_start_ts is not None else stamps[0]
        end_stamp = int(cycle_end_ts) if cycle_end_ts is not None else stamps[1]
        reason = str(entry_reason or REASON_LEGACY)
        with self._lock, self._conn:
            self._archive_day_rows_locked(schedule_date, reason, now)
            self._conn.execute("DELETE FROM schedule_items WHERE schedule_date = ?", (schedule_date,))
            for item in items:
                self._conn.execute(
                    "INSERT INTO schedule_items(item_id, schedule_date, name, description, goal_type,"
                    " priority, start_minute, end_minute, day_offset, parameters, created_at, updated_at,"
                    " cycle_start_ts, cycle_end_ts, entry_reason)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(item.get("item_id") or uuid.uuid4().hex),
                        schedule_date,
                        str(item.get("name") or "未命名活动")[:100],
                        str(item.get("description") or "")[:200],
                        str(item.get("goal_type") or "daily_routine"),
                        str(item.get("priority") or "medium"),
                        int(item.get("start_minute") or 0),
                        int(item.get("end_minute") or 60),
                        (None if item.get("day_offset") is None else int(item.get("day_offset") or 0)),
                        json.dumps(item.get("parameters") or {}, ensure_ascii=False),
                        now,
                        now,
                        start_stamp,
                        end_stamp,
                        reason,
                    ),
                )
            return len(items)

    def get_archived_day_schedule(self, schedule_date: str,
                                  limit: int = 100) -> List[Dict[str, Any]]:
        """读归档（archived_at 升序，按行粒度；测试面与 diag 只读用，不影响任何读取行为）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedule_archive WHERE schedule_date = ?"
                " ORDER BY archive_id ASC LIMIT ?",
                (schedule_date, int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_day_schedule(self, schedule_date: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM schedule_items WHERE schedule_date = ? ORDER BY start_minute ASC",
                (schedule_date,),
            ).fetchall()
        return [self._item_row(r) for r in rows]

    def has_day_schedule(self, schedule_date: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM schedule_items WHERE schedule_date = ?", (schedule_date,)
            ).fetchone()
        return bool(row and row["c"])

    def get_schedule_item(self, item_id: str) -> Optional[Dict[str, Any]]:
        """按 item_id 查单条日程条目（供合成 Goal 的读取路径使用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM schedule_items WHERE item_id = ?", (item_id,)
            ).fetchone()
        return self._item_row(row) if row else None

    def delete_schedule_item(self, item_id: str) -> bool:
        """删除单条日程条目（/plan delete 按序号删除合成 Goal 时使用）。"""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM schedule_items WHERE item_id = ?", (item_id,))
            return cur.rowcount > 0

    def delete_day_schedule(self, schedule_date: str, *, archive_reason: str = "") -> int:
        """删除某天全部日程条目；``archive_reason`` 非空 → 先把旧行归档（v4 §31.2）。

        regenerate 指令的进账流先经本方法清日再写（schedule_generator.regenerate_today_schedule
        透传 reason="regenerate"）——先删后写场景下旧日行**就是本次进账所替换的旧行**，
        归档在删除前完成（快照 + cycle 戳 + reason + 归档时刻），保住血缘；默认不归档 =
        旧行为（失效时机不变）。
        """
        with self._lock, self._conn:
            if archive_reason:
                self._archive_day_rows_locked(schedule_date, archive_reason,
                                              datetime.now().isoformat())
            cur = self._conn.execute("DELETE FROM schedule_items WHERE schedule_date = ?", (schedule_date,))
            return cur.rowcount

    def cleanup_old_days(self, keep_days: int = 3) -> int:
        """删除 keep_days 天之前的旧日程条目。"""
        cutoff = (datetime.now().date()).toordinal() - keep_days
        cutoff_date = datetime.fromordinal(cutoff).strftime("%Y-%m-%d")
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM schedule_items WHERE schedule_date < ?", (cutoff_date,))
            return cur.rowcount

    # ================================================================
    # 目标 / 未来约定（goals 表）CRUD —— 来源：database/goal_db.py
    # ================================================================
    @staticmethod
    def _goal_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for field in ("conditions", "parameters"):
            raw = data.get(field)
            if raw:
                try:
                    data[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[field] = {}
            else:
                data[field] = {}
        return data

    def create_goal(
        self,
        goal_id: str,
        name: str,
        description: str,
        goal_type: str,
        priority: str,
        creator_id: str,
        chat_id: str,
        status: str = "active",
        created_at: Optional[datetime] = None,
        deadline: Optional[datetime] = None,
        conditions: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        progress: int = 0,
        last_executed_at: Optional[datetime] = None,
        execution_count: int = 0,
        **kwargs,  # 忽略旧字段如 interval_seconds
    ) -> str:
        """创建目标（goal_id 已存在时抛 sqlite3.IntegrityError）。"""
        if created_at is None:
            created_at = datetime.now()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO goals ("
                " goal_id, name, description, goal_type, priority,"
                " creator_id, chat_id, status, created_at, deadline,"
                " interval_seconds, conditions, parameters, progress,"
                " last_executed_at, execution_count, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    goal_id,
                    name,
                    description,
                    goal_type,
                    priority,
                    creator_id,
                    chat_id,
                    status,
                    created_at.isoformat(),
                    deadline.isoformat() if deadline else None,
                    None,  # interval_seconds 已弃用，始终为 NULL
                    json.dumps(conditions, ensure_ascii=False) if conditions else None,
                    json.dumps(parameters, ensure_ascii=False) if parameters else None,
                    progress,
                    last_executed_at.isoformat() if last_executed_at else None,
                    execution_count,
                    datetime.now().isoformat(),
                ),
            )
        return goal_id

    def get_goal(self, goal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
        return self._goal_row(row) if row else None

    def get_all_goals(
        self,
        chat_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """按 chat_id / status 过滤查询目标（created_at 倒序）。"""
        query = "SELECT * FROM goals WHERE 1=1"
        params: List[Any] = []
        if chat_id:
            query += " AND chat_id = ?"
            params.append(chat_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        if offset > 0:
            query += " OFFSET ?"
            params.append(offset)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._goal_row(r) for r in rows]

    def update_goal(self, goal_id: str, **kwargs: Any) -> bool:
        """更新目标字段（conditions/parameters 自动 JSON 序列化，datetime 转 ISO）。"""
        if not kwargs:
            return False
        set_clauses = []
        params: List[Any] = []
        for key, value in kwargs.items():
            if key in ("conditions", "parameters") and value is not None:
                set_clauses.append(f"{key} = ?")
                params.append(json.dumps(value, ensure_ascii=False))
            elif key in ("created_at", "deadline", "last_executed_at") and value is not None:
                set_clauses.append(f"{key} = ?")
                params.append(value.isoformat() if isinstance(value, datetime) else value)
            else:
                set_clauses.append(f"{key} = ?")
                params.append(value)
        set_clauses.append("updated_at = ?")
        params.append(datetime.now().isoformat())
        params.append(goal_id)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"UPDATE goals SET {', '.join(set_clauses)} WHERE goal_id = ?", params
            )
            return cur.rowcount > 0

    def delete_goal(self, goal_id: str) -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
            return cur.rowcount > 0

    def delete_goals_by_status(self, status: str, older_than: Optional[datetime] = None) -> int:
        """按状态删除目标（可选只删 older_than 之前创建的）。"""
        query = "DELETE FROM goals WHERE status = ?"
        params: List[Any] = [status]
        if older_than:
            query += " AND created_at < ?"
            params.append(older_than.isoformat())
        with self._lock, self._conn:
            cur = self._conn.execute(query, params)
            return cur.rowcount

    # ================================================================
    # 活动档案（activity_episodes，v5 纯新增表）CRUD
    # R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
    # D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）
    # ——附录 B.1.1 全字段 DDL 原样；只增不改：schedule_items / goals /
    # schedule_archive 等旧表一字不动。并发口径沿用本类既有模式：WAL + RLock +
    # 短事务（with self._lock, self._conn），tick 与命令并发无死锁；所有写方法
    # 失败告警（logger.warning）不抛，按返回值 None/False/0 表达。
    # ================================================================
    @staticmethod
    def _episode_row(row: sqlite3.Row) -> Dict[str, Any]:
        data = dict(row)
        for field in ("state_before", "state_after", "metadata"):
            raw = data.get(field)
            if raw:
                try:
                    data[field] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    data[field] = {}
            else:
                data[field] = {}
        return data

    @staticmethod
    def _dump_json_field(value: Optional[Dict[str, Any]]) -> Optional[str]:
        if value is None:
            return None
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _merge_json_text(raw: Any, updates: Optional[Dict[str, Any]]) -> Optional[str]:
        """行内 JSON 字段（metadata 等）读改写：旧值解析失败按空 dict 降级、追加 updates。"""
        base: Dict[str, Any] = {}
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    base = loaded
            except (json.JSONDecodeError, TypeError):
                base = {}
        if updates:
            base.update(updates)
        return json.dumps(base, ensure_ascii=False) if base else None

    @staticmethod
    def _clamp_progress(value: Any) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, min(1.0, number))

    @classmethod
    def _default_episode_progress(cls, planned_start: Any, planned_end: Any,
                                  actual_start: Any, actual_end: Any, *,
                                  discrete: bool = False,
                                  allow_full: bool = False) -> Optional[float]:
        """默认进度 = 已用时长 / 计划时长（上限 0.95）；离散活动完成（allow_full）即 1.0。

        - 端点缺失/计划时长非正/倒挂不可解 → None（调用方保留原 progress 不覆盖）；
        - 实际端点倒挂（actual_end < actual_start）按跨午夜同一 24h 环 +``DAY_MINUTES``
          修正（与 cycle_state.py 入睡钟点环抬同款口径，禁手写 −1440）；
        - 显式传入的 progress 永远优先（本函数只服务默认口径）。
        """
        try:
            planned_span = int(planned_end) - int(planned_start)
            used = int(actual_end) - int(actual_start)
        except (TypeError, ValueError):
            return None
        if planned_span <= 0:
            return None
        if used < 0:
            used += DAY_MINUTES
        if used < 0:
            return None
        if allow_full and discrete:
            return 1.0
        return min(cls._clamp_progress(used / planned_span), _EPISODE_PROGRESS_CAP)

    def _close_active_episodes_locked(self, closed_by: str,
                                      actual_end_minute: Optional[int],
                                      now: str, close_reason: str) -> int:
        """关闭全部既有 active episode（「任意时刻最多一个 active」的强制点；持锁事务内调用）。

        既有 active → ``status='interrupted'``（``interrupted_by`` = 关闭发起方，即新
        episode_id），实际结束 = ``actual_end_minute``（未传 → 该行 planned_end），
        进度按默认口径（上限 0.95），metadata 追加 close_reason / superseded_by。
        单行失败告警不抛（返回已关闭行数）；idx_episodes_active_singleton 兜底强制。
        """
        closed = 0
        rows = self._conn.execute(
            "SELECT episode_id, planned_start_minute, planned_end_minute, actual_start_minute,"
            " progress, metadata FROM activity_episodes WHERE status = 'active'"
        ).fetchall()
        for row in rows:
            end = actual_end_minute if actual_end_minute is not None else row["planned_end_minute"]
            progress = self._default_episode_progress(
                row["planned_start_minute"], row["planned_end_minute"],
                row["actual_start_minute"], end,
            )
            if progress is None:
                progress = self._clamp_progress(row["progress"])
            try:
                self._conn.execute(
                    "UPDATE activity_episodes SET status = 'interrupted',"
                    " interrupted_by = COALESCE(interrupted_by, ?), actual_end_minute = ?,"
                    " progress = ?, metadata = ?, updated_at = ?"
                    " WHERE episode_id = ? AND status = 'active'",
                    (closed_by, end, progress,
                     self._merge_json_text(
                         row["metadata"],
                         {"close_reason": close_reason, "superseded_by": closed_by},
                     ),
                     now, row["episode_id"]),
                )
                closed += 1
            except sqlite3.Error as e:
                logger.warning("schedule_db: 关闭 active episode 失败（不抛）：%s", e)
        return closed

    def _insert_episode_locked(self, *, episode_id: str, schedule_date: str, day_offset: Any,
                               source: str, name: str, goal_type: str, property_id: Any,
                               planned_start: int, planned_end: int, actual_start: Any,
                               status: str, progress: float = 0.0,
                               interruptibility: Any, switch_cost: Any,
                               attention_mode: Any, interrupted_by: Any, resume_of: Any,
                               state_before: Optional[Dict[str, Any]],
                               metadata: Optional[Dict[str, Any]], now: str) -> None:
        """单行插入（持锁事务内调用；字段越界即抛由调用方统一告警吞掉）。"""
        self._conn.execute(
            "INSERT INTO activity_episodes(episode_id, schedule_date, day_offset, source,"
            " name, goal_type, property_id, planned_start_minute, planned_end_minute,"
            " actual_start_minute, status, progress, interruptibility, switch_cost,"
            " attention_mode, interrupted_by, resume_of, state_before, metadata,"
            " created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (episode_id, str(schedule_date), day_offset, str(source or "manual"),
             str(name or "未命名活动")[:100], str(goal_type or "daily_routine"), property_id,
             int(planned_start), int(planned_end), actual_start, status, self._clamp_progress(progress),
             interruptibility, switch_cost, attention_mode, interrupted_by, resume_of,
             self._dump_json_field(state_before), self._dump_json_field(metadata), now, now),
        )

    def start_episode(
        self,
        schedule_date: str,
        name: str,
        goal_type: str,
        source: str,
        planned_start_minute: int,
        planned_end_minute: int,
        *,
        episode_id: Optional[str] = None,
        day_offset: Optional[int] = None,
        property_id: Optional[str] = None,
        interruptibility: Optional[float] = None,
        switch_cost: Optional[float] = None,
        attention_mode: Optional[str] = None,
        interrupted_by: Optional[str] = None,
        resume_of: Optional[str] = None,
        actual_start_minute: Optional[int] = None,
        state_before: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """开始一个 episode（status='active'）。返回 episode_id；写失败告警不抛 → None。

        start 前先关闭既有 active（任意时刻最多一个 active：既有行 → interrupted，
        ``interrupted_by`` = 本行 id；单例唯一索引兜底）。``actual_start_minute`` 未传
        → 取 ``planned_start_minute``（确定性兜底，DB 层不取墙钟）。metadata 存决策
        依据（top3 候选/分数/随机种子/放弃原因等，由调用方给）。
        """
        eid = str(episode_id or uuid.uuid4().hex)
        now = datetime.now().isoformat()
        actual_start = planned_start_minute if actual_start_minute is None else int(actual_start_minute)
        try:
            with self._lock, self._conn:
                self._close_active_episodes_locked(eid, actual_start, now,
                                                   "superseded_by_start")
                self._insert_episode_locked(
                    episode_id=eid, schedule_date=schedule_date, day_offset=day_offset,
                    source=source, name=name, goal_type=goal_type, property_id=property_id,
                    planned_start=int(planned_start_minute),
                    planned_end=int(planned_end_minute), actual_start=actual_start,
                    status="active", interruptibility=interruptibility,
                    switch_cost=switch_cost, attention_mode=attention_mode,
                    interrupted_by=interrupted_by, resume_of=resume_of,
                    state_before=state_before, metadata=metadata, now=now,
                )
            return eid
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: start_episode 写入失败（不抛）：%s", e)
            return None

    def plan_episode(
        self,
        schedule_date: str,
        name: str,
        goal_type: str,
        source: str,
        planned_start_minute: int,
        planned_end_minute: int,
        *,
        episode_id: Optional[str] = None,
        day_offset: Optional[int] = None,
        property_id: Optional[str] = None,
        interruptibility: Optional[float] = None,
        switch_cost: Optional[float] = None,
        attention_mode: Optional[str] = None,
        interrupted_by: Optional[str] = None,
        resume_of: Optional[str] = None,
        state_before: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """预写入一条 planned episode（不开始、不关闭 active；spine/硬约定提前落库用）。

        保守裁量新增：ask 方法清单缺 planned 行的创建入口，而 planned_future/skip 以
        planned 行为对象——本方法补齐 Create 面，字段与 start_episode 全同、status='planned'。
        """
        eid = str(episode_id or uuid.uuid4().hex)
        now = datetime.now().isoformat()
        try:
            with self._lock, self._conn:
                self._insert_episode_locked(
                    episode_id=eid, schedule_date=schedule_date, day_offset=day_offset,
                    source=source, name=name, goal_type=goal_type, property_id=property_id,
                    planned_start=int(planned_start_minute),
                    planned_end=int(planned_end_minute), actual_start=None,
                    status="planned", interruptibility=interruptibility,
                    switch_cost=switch_cost, attention_mode=attention_mode,
                    interrupted_by=interrupted_by, resume_of=resume_of,
                    state_before=state_before, metadata=metadata, now=now,
                )
            return eid
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: plan_episode 写入失败（不抛）：%s", e)
            return None

    def _transition_episode(
        self,
        episode_id: str,
        to_status: str,
        allowed_from: Tuple[str, ...],
        *,
        actual_end_minute: Optional[int] = None,
        progress: Optional[float] = None,
        discrete: Optional[bool] = None,
        allow_full_on_discrete: bool = False,
        interrupted_by: Optional[str] = None,
        state_after: Optional[Dict[str, Any]] = None,
        extra_metadata: Optional[Dict[str, Any]] = None,
        end_fallback_planned: bool = False,
    ) -> bool:
        """episode 状态迁移共用体（持锁短事务；行不存在/状态不合法 → False，不抛）。

        ``actual_end_minute``：显式传入优先；未传时 finish 按 planned_end 兜底（视为
        自然完成），其余迁移（pause/interrupt/abandon/skip）保持原 actual_end 不虚构。
        progress 显式传入优先（clamp 到 [0,1]），否则按默认口径（已用/计划、上限 0.95；
        离散活动且 allow_full_on_discrete 才 1.0），不可计算 → 保留原 progress。
        state_after/metadata 未传 → 保留原值；metadata 传则与旧行 JSON 合并。
        """
        now = datetime.now().isoformat()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM activity_episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                if row is None or row["status"] not in allowed_from:
                    return False
                if actual_end_minute is not None:
                    end = int(actual_end_minute)
                elif end_fallback_planned:
                    end = row["planned_end_minute"]
                else:
                    end = row["actual_end_minute"]
                if progress is not None:
                    new_progress: float = self._clamp_progress(progress)
                else:
                    is_discrete = (row["goal_type"] in EPISODE_DISCRETE_GOAL_TYPES
                                   if discrete is None else bool(discrete))
                    new_progress = self._default_episode_progress(
                        row["planned_start_minute"], row["planned_end_minute"],
                        row["actual_start_minute"], end,
                        discrete=is_discrete, allow_full=allow_full_on_discrete,
                    )
                    if new_progress is None:
                        new_progress = self._clamp_progress(row["progress"])
                self._conn.execute(
                    "UPDATE activity_episodes SET status = ?, actual_end_minute = ?,"
                    " progress = ?, interrupted_by = COALESCE(?, interrupted_by),"
                    " state_after = COALESCE(?, state_after), metadata = ?, updated_at = ?"
                    " WHERE episode_id = ? AND status = ?",
                    (to_status, end, new_progress, interrupted_by,
                     self._dump_json_field(state_after),
                     self._merge_json_text(row["metadata"], extra_metadata),
                     now, episode_id, row["status"]),
                )
            return True
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: episode %s -> %s 写入失败（不抛）：%s",
                           episode_id, to_status, e)
            return False

    def finish(self, episode_id: str, *, actual_end_minute: Optional[int] = None,
               progress: Optional[float] = None, discrete: Optional[bool] = None,
               state_after: Optional[Dict[str, Any]] = None,
               metadata: Optional[Dict[str, Any]] = None) -> bool:
        """完成 episode（active/paused → completed）。

        progress 默认口径：离散活动（goal_type ∈ EPISODE_DISCRETE_GOAL_TYPES，或显式
        ``discrete=True``）→ 1.0；其余 = 已用时长/计划时长（上限 0.95）。显式
        ``progress`` 传入优先（clamp [0,1]；用户明确说"做完了"可由调用方传 1.0）。
        """
        return self._transition_episode(
            episode_id, "completed", ("active", "paused"),
            actual_end_minute=actual_end_minute, progress=progress, discrete=discrete,
            allow_full_on_discrete=True, state_after=state_after, extra_metadata=metadata,
            end_fallback_planned=True,
        )

    def interrupt(self, episode_id: str, *, interrupted_by: Optional[str] = None,
                  actual_end_minute: Optional[int] = None,
                  progress: Optional[float] = None,
                  state_after: Optional[Dict[str, Any]] = None,
                  metadata: Optional[Dict[str, Any]] = None) -> bool:
        """打断 episode（active → interrupted；记录 interrupted_by）。

        进度：传 ``actual_end_minute``/``progress`` 时按默认口径（已用/计划、上限 0.95）
        或显式值；都不传 → 保持原 progress、原 actual_end（不虚构进度）。
        """
        return self._transition_episode(
            episode_id, "interrupted", ("active",),
            actual_end_minute=actual_end_minute, progress=progress,
            interrupted_by=interrupted_by, state_after=state_after, extra_metadata=metadata,
        )

    def pause(self, episode_id: str, *, actual_end_minute: Optional[int] = None,
              progress: Optional[float] = None,
              state_after: Optional[Dict[str, Any]] = None,
              metadata: Optional[Dict[str, Any]] = None) -> bool:
        """暂停 episode（active/interrupted → paused；进度保留供恢复池使用）。

        传 ``actual_end_minute``/``progress`` 时按默认口径或显式值刷新；都不传 →
        保持原 progress / 原 actual_end（不虚构）。
        """
        return self._transition_episode(
            episode_id, "paused", ("active", "interrupted"),
            actual_end_minute=actual_end_minute, progress=progress,
            state_after=state_after, extra_metadata=metadata,
        )

    def update_active_progress(self, episode_id: str, *, cur_minute: Optional[int] = None,
                               attention_mode: Optional[str] = None) -> Optional[float]:
        """运行中 episode 的进度/注意力档刷新（active → active，不改状态；PR1 P0 修复
        （2026-10-26），计划书 §三十四/定稿 §2 0.1/0.2——progress 运行期随已用时长增长）。

        - ``cur_minute`` 传入 → progress 按默认口径（已用/计划、上限 0.95，复用
          :meth:`_default_episode_progress`）重算；不可计算 → 保留原 progress（不虚构）；
        - ``attention_mode`` 传入 → 一并写回（运行期按新进度重算的注意力档）；
        - 行不存在/非 active/两个参数都未传/写失败 → None（告警不抛）；
          仅刷 attention_mode（``cur_minute`` 未传）时返回 None。
        """
        if cur_minute is None and attention_mode is None:
            return None
        now = datetime.now().isoformat()
        new_progress: Optional[float] = None
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT planned_start_minute, planned_end_minute, actual_start_minute,"
                    " progress FROM activity_episodes"
                    " WHERE episode_id = ? AND status = 'active'", (episode_id,),
                ).fetchone()
                if row is None:
                    return None
                if cur_minute is not None:
                    computed = self._default_episode_progress(
                        row["planned_start_minute"], row["planned_end_minute"],
                        row["actual_start_minute"], int(cur_minute),
                    )
                    new_progress = (self._clamp_progress(row["progress"])
                                    if computed is None else computed)
                sets = ["updated_at = ?"]
                params: List[Any] = [now]
                if new_progress is not None:
                    sets.append("progress = ?")
                    params.append(new_progress)
                if attention_mode is not None:
                    sets.append("attention_mode = ?")
                    params.append(str(attention_mode))
                params.append(episode_id)
                self._conn.execute(
                    f"UPDATE activity_episodes SET {', '.join(sets)}"
                    " WHERE episode_id = ? AND status = 'active'", params,
                )
            return new_progress
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: update_active_progress(%s) 失败（不抛）：%s",
                           episode_id, e)
            return None

    def resume(
        self,
        episode_id: str,
        *,
        planned_start_minute: Optional[int] = None,
        planned_end_minute: Optional[int] = None,
        actual_start_minute: Optional[int] = None,
        attention_mode: Optional[str] = None,
        state_before: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """恢复 paused/interrupted episode → 新建 active 行（source='resume'、resume_of=原 id）。

        进度原样保留（附录「恢复时保留」）；计划窗缺省沿用原行（调用方可传
        ``planned_*`` 改写为剩余时长形态）；原行保持 paused 不改写状态（不覆盖历史），
        仅 metadata 记 ``resumed_by``。已有恢复行（resume_of 命中）→ 拒绝（None），
        不二次复活。start 前同样先关闭既有 active（任意时刻最多一个 active）。
        返回新 episode_id；写失败/不合法 → None（告警不抛）。
        """
        new_id = uuid.uuid4().hex
        now = datetime.now().isoformat()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT * FROM activity_episodes WHERE episode_id = ?", (episode_id,)
                ).fetchone()
                if row is None or row["status"] not in ("paused", "interrupted"):
                    return None
                resumed_before = self._conn.execute(
                    "SELECT COUNT(*) FROM activity_episodes WHERE resume_of = ?",
                    (episode_id,),
                ).fetchone()[0]
                if resumed_before:
                    return None
                planned_start = row["planned_start_minute"] if planned_start_minute is None \
                    else int(planned_start_minute)
                planned_end = row["planned_end_minute"] if planned_end_minute is None \
                    else int(planned_end_minute)
                actual_start = planned_start if actual_start_minute is None \
                    else int(actual_start_minute)
                self._close_active_episodes_locked(new_id, actual_start, now,
                                                   "superseded_by_resume")
                self._insert_episode_locked(
                    episode_id=new_id, schedule_date=row["schedule_date"],
                    day_offset=row["day_offset"], source="resume", name=row["name"],
                    goal_type=row["goal_type"], property_id=row["property_id"],
                    planned_start=planned_start, planned_end=planned_end,
                    actual_start=actual_start, status="active",
                    progress=self._clamp_progress(row["progress"]),
                    interruptibility=row["interruptibility"],
                    switch_cost=row["switch_cost"],
                    attention_mode=(attention_mode if attention_mode is not None
                                    else row["attention_mode"]),
                    interrupted_by=None, resume_of=episode_id,
                    state_before=state_before, metadata=metadata, now=now,
                )
                self._conn.execute(
                    "UPDATE activity_episodes SET metadata = ?, updated_at = ?"
                    " WHERE episode_id = ? AND status = ?",
                    (self._merge_json_text(row["metadata"], {"resumed_by": new_id}),
                     now, episode_id, row["status"]),
                )
            return new_id
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: resume(%s) 写入失败（不抛）：%s", episode_id, e)
            return None

    def abandon(self, episode_id: str, *, reason: Optional[str] = None,
                metadata: Optional[Dict[str, Any]] = None) -> bool:
        """放弃 episode（planned/active/paused/interrupted → abandoned）。

        ``reason``（放弃原因）记入 metadata ``abandon_reason``——附录「放弃必须在日志
        里可解释」。注意：close-active 关闭路径不经本方法（那边记 close_reason）。
        """
        extra = dict(metadata or {})
        if reason is not None:
            extra.setdefault("abandon_reason", str(reason))
        return self._transition_episode(
            episode_id, "abandoned", ("planned", "active", "paused", "interrupted"),
            extra_metadata=extra or None,
        )

    def skip(self, episode_id: str, *, reason: Optional[str] = None,
             metadata: Optional[Dict[str, Any]] = None) -> bool:
        """跳过 episode（planned → skipped，「未开始即过期」；原因记 metadata skip_reason）。"""
        extra = dict(metadata or {})
        if reason is not None:
            extra.setdefault("skip_reason", str(reason))
        return self._transition_episode(
            episode_id, "skipped", ("planned",),
            extra_metadata=extra or None,
        )

    def update_episode_metadata(self, episode_id: str,
                                updates: Dict[str, Any]) -> bool:
        """合并追加 episode 行 metadata（不迁移状态、不碰其他字段；读改写原子）。

        PR2.5 修复（2026-10-26），交接 §五.18 D1：close-active 被挤行由 planner 侧
        补应用随机事件 deltas 后，经本方法补写 ``metadata.state_deltas_applied``
        持久标记（防双应用沿用 PR1 0.4 同一标记口径）。
        行不存在/``updates`` 为空/写失败 → False（告警不抛，与本类写路径同款纪律）。
        """
        if not updates:
            return False
        now = datetime.now().isoformat()
        try:
            with self._lock, self._conn:
                row = self._conn.execute(
                    "SELECT metadata FROM activity_episodes WHERE episode_id = ?",
                    (episode_id,),
                ).fetchone()
                if row is None:
                    return False
                self._conn.execute(
                    "UPDATE activity_episodes SET metadata = ?, updated_at = ?"
                    " WHERE episode_id = ?",
                    (self._merge_json_text(row["metadata"], updates), now, episode_id),
                )
            return True
        except (sqlite3.Error, ValueError, TypeError) as e:
            logger.warning("schedule_db: update_episode_metadata(%s) 失败（不抛）：%s",
                           episode_id, e)
            return False

    def get_episode(self, episode_id: str) -> Optional[Dict[str, Any]]:
        """按 episode_id 查单条（供 RollingPlanner/ReadModel 复盘单集用；只读）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM activity_episodes WHERE episode_id = ?", (episode_id,)
            ).fetchone()
        return self._episode_row(row) if row else None

    def active_episode(self, schedule_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """当前 active episode（任意时刻最多一个；``schedule_date`` 给定则限定该日）。"""
        query = "SELECT * FROM activity_episodes WHERE status = 'active'"
        params: List[Any] = []
        if schedule_date:
            query += " AND schedule_date = ?"
            params.append(schedule_date)
        query += " ORDER BY created_at DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return self._episode_row(row) if row else None

    def planned_future(self, schedule_date: str, cur_minute: Optional[int] = None,
                       limit: int = 10) -> List[Dict[str, Any]]:
        """planned 且未开始的 episode（按 planned_start 升序；恢复池/spine 合并由上层做）。

        ``cur_minute`` 未传 → 该日全部 planned。跨午夜 planned_* 允许 >1440、
        ``day_offset`` 与 schedule_items 同口径；只查给定 schedule_date（跨日由调用方
        按 day_offset/次日合并，与 §7.8 读取口径一致）。
        """
        query = ("SELECT * FROM activity_episodes WHERE status = 'planned'"
                 " AND schedule_date = ?")
        params: List[Any] = [schedule_date]
        if cur_minute is not None:
            query += " AND planned_start_minute >= ?"
            params.append(int(cur_minute))
        query += " ORDER BY planned_start_minute ASC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._episode_row(r) for r in rows]

    def paused_recent(self, schedule_date: Optional[str] = None,
                      limit: int = 5) -> List[Dict[str, Any]]:
        """恢复池候选：status='paused' 且尚无恢复行（resume_of 未命中，不重复复活），
        updated_at 倒序。过期/放弃判定归 RollingPlanner，本层只按序给出。"""
        query = ("SELECT * FROM activity_episodes WHERE status = 'paused'"
                 " AND episode_id NOT IN (SELECT resume_of FROM activity_episodes"
                 " WHERE resume_of IS NOT NULL)")
        params: List[Any] = []
        if schedule_date:
            query += " AND schedule_date = ?"
            params.append(schedule_date)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._episode_row(r) for r in rows]

    def completed_today(self, schedule_date: str) -> List[Dict[str, Any]]:
        """该日已完成 episode（实际发生口径；actual_start 缺省回退 planned_start 排序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity_episodes WHERE status = 'completed'"
                " AND schedule_date = ?"
                " ORDER BY COALESCE(actual_start_minute, planned_start_minute) ASC",
                (schedule_date,),
            ).fetchall()
        return [self._episode_row(r) for r in rows]

    def episodes_today(self, schedule_date: str) -> List[Dict[str, Any]]:
        """该日全部 episode（全状态；planned_start 升序）。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM activity_episodes WHERE schedule_date = ?"
                " ORDER BY planned_start_minute ASC, created_at ASC",
                (schedule_date,),
            ).fetchall()
        return [self._episode_row(r) for r in rows]

    def cleanup_old_episodes(self, keep_days: int = 14) -> int:
        """删除 keep_days 天前的 episode（附录 B.7：保留 14 天，按日清理；失败告警返 0）。"""
        cutoff = (datetime.now().date()).toordinal() - keep_days
        cutoff_date = datetime.fromordinal(cutoff).strftime("%Y-%m-%d")
        try:
            with self._lock, self._conn:
                cur = self._conn.execute(
                    "DELETE FROM activity_episodes WHERE schedule_date < ?", (cutoff_date,)
                )
                return cur.rowcount
        except sqlite3.Error as e:
            logger.warning("schedule_db: cleanup_old_episodes 失败（不抛）：%s", e)
            return 0
