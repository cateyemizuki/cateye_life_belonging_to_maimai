"""日程 SQLite 持久化（来源：自主规划插件 database/goal_db.py + 原 schedule_db 合并演进）。

两张表：
- ``schedule_items``：按天的日程条目（ScheduleQuery / get_current_activity API 的数据源）；
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
from typing import Any, Dict, List, Optional

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
        parameters TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_schedule_date ON schedule_items(schedule_date);
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
                self._conn.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', '2')"
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

    def replace_day_schedule(self, schedule_date: str, items: List[Dict[str, Any]]) -> int:
        """整体替换某天的日程（生成成功后落库）。返回写入条数。"""
        now = datetime.now().isoformat()
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM schedule_items WHERE schedule_date = ?", (schedule_date,))
            for item in items:
                self._conn.execute(
                    "INSERT INTO schedule_items(item_id, schedule_date, name, description, goal_type,"
                    " priority, start_minute, end_minute, parameters, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(item.get("item_id") or uuid.uuid4().hex),
                        schedule_date,
                        str(item.get("name") or "未命名活动")[:100],
                        str(item.get("description") or "")[:200],
                        str(item.get("goal_type") or "daily_routine"),
                        str(item.get("priority") or "medium"),
                        int(item.get("start_minute") or 0),
                        int(item.get("end_minute") or 60),
                        json.dumps(item.get("parameters") or {}, ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            return len(items)

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

    def delete_day_schedule(self, schedule_date: str) -> int:
        with self._lock, self._conn:
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
