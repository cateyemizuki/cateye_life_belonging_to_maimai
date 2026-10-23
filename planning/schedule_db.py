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
  为带 time_window 的 Goal 视图（单一数据源，避免双写漂移）。

损坏自愈：初始化遇到 malformed/not a database 时隔离旧文件重建空库。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .cycle_state import REASON_LEGACY

_CORRUPTION_KEYWORDS = ("malformed", "not a database", "encrypted", "corrupt")


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
                self._conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', '4')"
                )
                self._conn.commit()
        except sqlite3.DatabaseError as e:
            if not any(k in str(e).lower() for k in _CORRUPTION_KEYWORDS):
                raise
            self._quarantine_corrupt_files()
            with self._lock:
                self._conn = self._connect()
                self._conn.executescript(self.SCHEMA)
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
