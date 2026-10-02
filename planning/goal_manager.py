"""Goal Manager（来源：自主规划插件 planner/goal_manager.py，接口照搬）。

存储层为 ``ScheduleDB``（同库两张表）：
- 长期目标 / 未来约定 → ``goals`` 表（status 流转，pending_commitment 用
  goal_type 区分，parameters JSON 携带 commitment_date / time / notes / reason）；
- 当日日程条目 → ``schedule_items`` 表。``get_schedule_goals`` /
  ``get_active_goals`` 会把日程条目**合成**为带 ``time_window`` 的 Goal 视图
  （单一数据源，避免日程双写漂移）——合成 Goal 的 goal_id 即 item_id，
  对其 delete/update 会落到对应日程条目上。

单例接口照搬源插件：``get_goal_manager(data_dir)`` / ``close_goal_manager()``。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.time_utils import parse_hhmm
from .schedule_db import ScheduleDB
from .time_utils import TimezoneManager

logger = logging.getLogger(__name__)

# 支持的日期格式（按优先级尝试）
_COMMITMENT_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def _normalize_commitment_date(raw: str, today: datetime) -> str:
    """把任意输入归一化为 ``YYYY-MM-DD``；解析失败回退 today（保证比较口径一致）。"""
    text = str(raw or "").strip()
    if not text:
        return today.strftime("%Y-%m-%d")
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d")
    except ValueError:
        pass
    for fmt in _COMMITMENT_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.warning(f"commitment_date 无法解析: {raw!r}，回退为今天 {today.strftime('%Y-%m-%d')}")
    return today.strftime("%Y-%m-%d")


class GoalStatus(Enum):
    """目标状态枚举。"""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class GoalPriority(Enum):
    """目标优先级枚举。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Goal:
    """单个目标（长期目标 / 未来约定 / 由日程条目合成的日程 Goal 视图）。"""

    def __init__(
        self,
        goal_id: str,
        name: str,
        description: str,
        goal_type: str,
        priority: Any,
        creator_id: str,
        chat_id: str,
        status: Any = GoalStatus.ACTIVE,
        created_at: Optional[datetime] = None,
        deadline: Optional[datetime] = None,
        conditions: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        progress: int = 0,
        last_executed_at: Optional[datetime] = None,
        execution_count: int = 0,
        **kwargs,  # 忽略旧字段如 interval_seconds
    ):
        self.goal_id = goal_id
        self.name = name
        self.description = description
        self.goal_type = goal_type
        self.priority = priority if isinstance(priority, GoalPriority) else GoalPriority(priority)
        self.creator_id = creator_id
        self.chat_id = chat_id
        self.status = status if isinstance(status, GoalStatus) else GoalStatus(status)
        if created_at is None:
            created_at = TimezoneManager().get_now()
        self.created_at = created_at
        self.deadline = deadline
        self.conditions = conditions or {}
        self.parameters = parameters or {}
        self.progress = progress
        self.last_executed_at = last_executed_at
        self.execution_count = execution_count

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal_id": self.goal_id,
            "name": self.name,
            "description": self.description,
            "goal_type": self.goal_type,
            "priority": self.priority.value,
            "creator_id": self.creator_id,
            "chat_id": self.chat_id,
            "status": self.status.value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "conditions": self.conditions,
            "parameters": self.parameters,
            "progress": self.progress,
            "last_executed_at": self.last_executed_at.isoformat() if self.last_executed_at else None,
            "execution_count": self.execution_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Goal":
        created_at = cls._parse_datetime(data.get("created_at"))
        deadline = cls._parse_datetime(data.get("deadline"))
        last_executed_at = cls._parse_datetime(data.get("last_executed_at"))
        return cls(
            goal_id=data["goal_id"],
            name=data["name"],
            description=data["description"],
            goal_type=data["goal_type"],
            priority=data["priority"],
            creator_id=data["creator_id"],
            chat_id=data["chat_id"],
            status=data.get("status", "active"),
            created_at=created_at,
            deadline=deadline,
            conditions=data.get("conditions", {}),
            parameters=data.get("parameters", {}),
            progress=data.get("progress", 0),
            last_executed_at=last_executed_at,
            execution_count=data.get("execution_count", 0),
        )

    @staticmethod
    def _parse_datetime(dt_str):
        if not dt_str:
            return None
        try:
            return datetime.fromisoformat(dt_str)
        except (ValueError, TypeError):
            return None

    def get_summary(self) -> str:
        """格式化目标摘要（含状态 / 优先级 / 进度 / 剩余时间 / 时间窗口）。"""
        status_emoji = {
            GoalStatus.ACTIVE: "🟢",
            GoalStatus.PAUSED: "⏸️",
            GoalStatus.COMPLETED: "✅",
            GoalStatus.CANCELLED: "❌",
        }
        priority_emoji = {
            GoalPriority.HIGH: "🔴",
            GoalPriority.MEDIUM: "🟡",
            GoalPriority.LOW: "🟢",
        }
        lines = [
            f"{status_emoji[self.status]} 目标: {self.name}",
            f"   ID: {self.goal_id[:8]}...",
            f"   聊天流: {self.chat_id}",
            f"   优先级: {priority_emoji[self.priority]} {self.priority.value}",
            f"   进度: {self.progress}%",
            f"   执行次数: {self.execution_count}",
        ]
        if self.deadline:
            now = TimezoneManager().get_now()
            time_left = strip_tz_dt(self.deadline) - strip_tz_dt(now)
            if time_left.total_seconds() > 0:
                days = time_left.days
                hours = time_left.seconds // 3600
                lines.append(f"   剩余时间: {days}天{hours}小时")
            else:
                lines.append("   ⚠️ 已超期")

        time_window = self.parameters.get("time_window") if self.parameters else None
        if time_window and isinstance(time_window, list) and len(time_window) == 2:
            start_h, start_m = divmod(time_window[0], 60)
            end_h, end_m = divmod(time_window[1], 60)
            lines.append(f"   时间: {start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}")
        return "\n".join(lines)


def strip_tz_dt(dt: Optional[datetime]) -> Optional[datetime]:
    """剥离 tzinfo（与 planning.time_utils.strip_tz 等价的本地别名，避免循环导入）。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class GoalManager:
    """目标管理器（SQLite 持久化 + 日程条目合成视图）。

    Args:
        data_dir: 数据目录（数据库文件 ``schedule.db`` 所在目录）
        db_name: 数据库文件名（默认与插件主库一致 ``schedule.db``）
        db: 已有的 ScheduleDB 实例（PlanningServices 会传入 plugin._db 复用连接；
            缺省时自建，供独立测试场景）
    """

    def __init__(
        self,
        data_dir: str = None,
        db_name: str = "schedule.db",
        db: Optional[ScheduleDB] = None,
    ):
        if data_dir is None:
            data_dir = Path(__file__).parent
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db if db is not None else ScheduleDB(self.data_dir / db_name)
        self.tz_manager = TimezoneManager()
        #: 进程内事件总线（M0；由 PlanningServices 装配时绑定，未绑定=静默跳过事件）
        self._bus: Optional[Any] = None
        logger.debug("GoalManager 初始化完成，数据库: %s", self.data_dir / db_name)

    def bind_event_bus(self, bus: Optional[Any]) -> None:
        """绑定事件总线（M0 装配点；允许 None=不发事件，兼容独立测试）。"""
        self._bus = bus

    def _publish_soon(self, event: str, **payload: Any) -> None:
        """同步上下文发布事件（本方法非 async，publish_soon 内部 create_task）。"""
        bus = self._bus
        if bus is None:
            return
        try:
            bus.publish_soon(event, source="planning", **payload)
        except Exception:
            logger.debug("事件发布失败：%s", event, exc_info=True)

    # ============================================================
    # 目标 CRUD（goals 表）
    # ============================================================

    def create_goal(
        self,
        name: str,
        description: str,
        goal_type: str,
        creator_id: str,
        chat_id: str,
        priority: str = "medium",
        deadline: Optional[datetime] = None,
        conditions: Optional[Dict[str, Any]] = None,
        parameters: Optional[Dict[str, Any]] = None,
        auto_save: bool = True,  # 兼容参数（始终立即落库）
        **kwargs,
    ) -> Goal:
        goal_id = str(uuid.uuid4())
        self.db.create_goal(
            goal_id=goal_id,
            name=name,
            description=description,
            goal_type=goal_type,
            priority=priority,
            creator_id=creator_id,
            chat_id=chat_id,
            deadline=deadline,
            conditions=conditions,
            parameters=parameters,
        )
        logger.debug("创建目标: %s (ID: %s)", name, goal_id)
        return Goal(
            goal_id=goal_id,
            name=name,
            description=description,
            goal_type=goal_type,
            priority=GoalPriority(priority),
            creator_id=creator_id,
            chat_id=chat_id,
            deadline=deadline,
            conditions=conditions,
            parameters=parameters,
        )

    def create_goals_batch(self, goals_data: List[Dict[str, Any]]) -> List[Goal]:
        """批量创建目标；任一失败抛异常（调用方自行回滚日志）。"""
        created_goals = []
        try:
            for data in goals_data:
                data = dict(data)
                data.pop("auto_save", None)
                created_goals.append(self.create_goal(**data))
            logger.info("批量创建了 %d 个目标", len(created_goals))
            return created_goals
        except Exception as exc:
            logger.error("批量创建目标失败: %s", exc, exc_info=True)
            raise

    def get_goal(self, goal_id: str) -> Optional[Goal]:
        """按 ID 查目标：先查 goals 表，再查日程条目（合成视图）。"""
        data = self.db.get_goal(goal_id)
        if data:
            return Goal.from_dict(data)
        item = self.db.get_schedule_item(goal_id)
        if item:
            return self._goal_from_schedule_item(item)
        return None

    def get_all_goals(
        self,
        chat_id: Optional[str] = None,
        status: Optional[GoalStatus] = None,
    ) -> List[Goal]:
        """全部目标（goals 表 + 合成的日程 Goal；合成条目恒为 ACTIVE）。"""
        status_str = status.value if status else None
        goals = [Goal.from_dict(d) for d in self.db.get_all_goals(chat_id=chat_id, status=status_str)]
        if status_str in (None, GoalStatus.ACTIVE.value):
            goals.extend(self._synthesized_schedule_goals())
        return goals

    def get_active_goals(self, chat_id: Optional[str] = None) -> List[Goal]:
        """活跃目标 = 今日/昨日日程合成 Goal + goals 表中 status=active 的目标。"""
        return self.get_all_goals(chat_id=chat_id, status=GoalStatus.ACTIVE)

    # ============================================================
    # pending_commitments：未来约定（goal_type="pending_commitment"）
    # ============================================================
    # 复用 goals 表存储，用 goal_type 区分；parameters JSON 中含：
    #   commitment_date / time / notes / reason / source
    # 消费时 status 改为 COMPLETED，不删除（保留审计）。

    def add_pending_commitment(
        self,
        commitment_date: str,
        title: str,
        *,
        time: str = "",
        notes: str = "",
        reason: str = "",
        creator_id: str = "system",
    ) -> Goal:
        """添加一条未来约定（日期归一化，格式不统一也能正确匹配）。"""
        normalized_date = _normalize_commitment_date(commitment_date, self.tz_manager.get_now())
        parameters = {
            "commitment_date": normalized_date,
            "time": time,
            "notes": notes,
            "reason": reason,
            "source": "user_request",
        }
        return self.create_goal(
            name=title,
            description=notes or title,
            goal_type="pending_commitment",
            creator_id=creator_id,
            chat_id="global",
            priority="medium",
            parameters=parameters,
        )

    def get_pending_commitments(self, commitment_date: Optional[str] = None) -> List[Goal]:
        """查询未来约定（仅 status=active；commitment_date 留空 = 今天）。"""
        if commitment_date is None:
            commitment_date = self.tz_manager.get_now().strftime("%Y-%m-%d")
        else:
            commitment_date = _normalize_commitment_date(commitment_date, self.tz_manager.get_now())
        candidates = self.get_all_goals(chat_id="global", status=GoalStatus.ACTIVE)
        return [
            g for g in candidates
            if g.goal_type == "pending_commitment"
            and (g.parameters or {}).get("commitment_date") == commitment_date
        ]

    def consume_pending_commitments(self, commitment_date: str,
                                   *, matched_titles: Optional[Any] = None) -> List[Goal]:
        """消费指定日期的全部未消费约定（标记 COMPLETED，保留审计）。

        Args:
            commitment_date: 约定日期（YYYY-MM-DD）。
            matched_titles: 本次日程生成中已匹配到日程项的约定标题集合（M0 新增，默认 None）。
                None=未知匹配结果（按兑现上报）；非 None 时未匹配的约定上报为违约。
                消费事件：``schedule.commitment_kept`` / ``schedule.commitment_broken``（附录 A）。
        """
        consumed = self.get_pending_commitments(commitment_date)
        for goal in consumed:
            self.db.update_goal(goal.goal_id, status=GoalStatus.COMPLETED.value)
        if consumed:
            logger.info("已消费 %d 条 %s 的未来约定", len(consumed), commitment_date)
            self._publish_commitments(consumed, matched_titles)
        return consumed

    def _publish_commitments(self, consumed: List[Goal], matched_titles: Optional[Any]) -> None:
        """按匹配结果逐条上报约定兑现/违约（M0；无总线时静默）。"""
        known = None if matched_titles is None else {str(t) for t in matched_titles}
        for goal in consumed:
            params = goal.parameters or {}
            title = str(goal.name or "").strip()
            when = str(params.get("time") or "").strip()
            notes = str(params.get("notes") or "").strip()
            kept = True if known is None else title in known
            self._publish_soon(
                "schedule.commitment_kept" if kept else "schedule.commitment_broken",
                summary=f"{title}（{when}）" if when else title,
                commitment=title, time=when, notes=notes,
                date=str(params.get("commitment_date") or ""),
                matched=kept,
            )

    # ============================================================
    # 日程 Goal 合成视图（schedule_items → 带 time_window 的 Goal）
    # ============================================================

    def _goal_from_schedule_item(self, item: Dict[str, Any]) -> Goal:
        """把 schedule_items 行合成为 Goal 视图（time_window 放入 parameters）。"""
        schedule_date = str(item.get("schedule_date") or "")
        try:
            created_at = datetime.strptime(schedule_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            created_at = None
        start = int(item.get("start_minute") or 0)
        end = int(item.get("end_minute") or 60)
        parameters = dict(item.get("parameters") or {})
        parameters.setdefault("time_window", [start, end])
        return Goal(
            goal_id=str(item.get("item_id") or ""),
            name=str(item.get("name") or ""),
            description=str(item.get("description") or ""),
            goal_type=str(item.get("goal_type") or "daily_routine"),
            priority=str(item.get("priority") or "medium"),
            creator_id="system",
            chat_id="global",
            status=GoalStatus.ACTIVE,
            created_at=created_at,
            parameters=parameters,
        )

    def _synthesized_schedule_goals(self, dates: Optional[List[str]] = None) -> List[Goal]:
        """把今天（及可选昨天）的日程条目合成为 Goal 列表。"""
        if dates is None:
            now = self.tz_manager.get_now()
            today = now.strftime("%Y-%m-%d")
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            dates = [today, yesterday]
        goals: List[Goal] = []
        for date_str in dates:
            for item in self.db.get_day_schedule(date_str):
                goals.append(self._goal_from_schedule_item(item))
        return goals

    def get_schedule_goals(
        self,
        chat_id: str = "global",
        date_str: Optional[str] = None,
        include_yesterday: bool = False,
    ) -> List[Goal]:
        """获取指定日期的日程 Goal（含跨夜参数；按 (name, time_window) 去重）。"""
        if date_str is None:
            date_str = self.tz_manager.get_now().strftime("%Y-%m-%d")

        target_dates = {date_str}
        if include_yesterday:
            try:
                base = datetime.strptime(date_str, "%Y-%m-%d")
                target_dates.add((base - timedelta(days=1)).strftime("%Y-%m-%d"))
            except (TypeError, ValueError):
                pass

        schedule_goals: List[Goal] = []
        for date in sorted(target_dates):
            for item in self.db.get_day_schedule(date):
                goal = self._goal_from_schedule_item(item)
                if goal.goal_type == "pending_commitment":
                    continue
                schedule_goals.append(goal)

        # 去重：按 (name, time_window)，保留最先出现的
        seen: set = set()
        unique_goals: List[Goal] = []
        for goal in schedule_goals:
            tw = (goal.parameters or {}).get("time_window")
            key = (goal.name, tuple(tw) if isinstance(tw, list) else None)
            if key not in seen:
                seen.add(key)
                unique_goals.append(goal)
        if len(unique_goals) < len(schedule_goals):
            logger.debug("去重：%d -> %d 个日程", len(schedule_goals), len(unique_goals))
        return unique_goals

    # ============================================================
    # 状态流转
    # ============================================================

    def update_goal(self, goal_id: str, **kwargs: Any) -> bool:
        """更新目标字段（日程条目合成 Goal 的时间窗更新落到 schedule_items）。"""
        if goal_id in self._schedule_item_ids():
            return self._update_schedule_item(goal_id, **kwargs)
        return self.db.update_goal(goal_id, **kwargs)

    def update_goal_status(self, goal_id: str, status: GoalStatus) -> bool:
        return self.update_goal(goal_id, status=status.value)

    def update_goal_progress(self, goal_id: str, progress: int) -> bool:
        progress = max(0, min(100, progress))
        return self.update_goal(goal_id, progress=progress)

    def complete_goal(self, goal_id: str) -> bool:
        return self.update_goal(goal_id, status=GoalStatus.COMPLETED.value, progress=100)

    def pause_goal(self, goal_id: str) -> bool:
        return self.update_goal_status(goal_id, GoalStatus.PAUSED)

    def resume_goal(self, goal_id: str) -> bool:
        return self.update_goal_status(goal_id, GoalStatus.ACTIVE)

    def cancel_goal(self, goal_id: str) -> bool:
        return self.update_goal_status(goal_id, GoalStatus.CANCELLED)

    def delete_goal(self, goal_id: str) -> bool:
        """删除目标：goals 表未命中时回退删除日程条目（合成 Goal 场景）。"""
        if self.db.get_goal(goal_id) is not None:
            deleted = self.db.delete_goal(goal_id)
        else:
            deleted = self.db.delete_schedule_item(goal_id)
        if deleted:
            logger.debug("已删除目标: %s", goal_id)
        return deleted

    def _schedule_item_ids(self) -> set:
        """今日日程条目的 item_id 集合（合成 Goal 的写回路由）。"""
        now = self.tz_manager.get_now()
        today = now.strftime("%Y-%m-%d")
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        ids = set()
        for date in (today, yesterday):
            for item in self.db.get_day_schedule(date):
                ids.add(str(item.get("item_id") or ""))
        return ids

    def _update_schedule_item(self, item_id: str, **kwargs: Any) -> bool:
        """对合成 Goal 的字段更新落到 schedule_items（目前支持 status=completed/cancelled → 删除）。"""
        status = kwargs.get("status")
        if status in (GoalStatus.COMPLETED.value, GoalStatus.CANCELLED.value):
            return self.db.delete_schedule_item(item_id)
        # 其余字段（priority / time_window 等）日程条目暂不支持原位更新
        logger.debug("日程条目 %s 不支持字段更新: %s", item_id, list(kwargs))
        return False

    # ============================================================
    # 清理
    # ============================================================

    def cleanup_old_goals(self, days: int = 30) -> int:
        """清理 days 天前的已完成 / 已取消目标。"""
        cutoff_date = self.tz_manager.get_now() - timedelta(days=days)
        completed_count = self.db.delete_goals_by_status(
            status=GoalStatus.COMPLETED.value, older_than=cutoff_date
        )
        cancelled_count = self.db.delete_goals_by_status(
            status=GoalStatus.CANCELLED.value, older_than=cutoff_date
        )
        total = completed_count + cancelled_count
        if total > 0:
            logger.info("清理了 %d 个旧目标（%d 天前）", total, days)
        return total

    def cleanup_expired_schedules(self) -> int:
        """清理过期日程（昨天及更早的日程条目；保留近 3 天供去重参考）。"""
        cleaned = self.db.cleanup_old_days(keep_days=3)
        if cleaned > 0:
            logger.info("🧹 清理了 %d 个过期日程条目（3 天前）", cleaned)
        return cleaned

    def get_goals_summary(self, chat_id: Optional[str] = None) -> str:
        """目标总览文本（按状态分组 + 优先级排序）。"""
        goals = self.get_all_goals(chat_id=chat_id)
        if not goals:
            return "📋 当前没有任何目标"

        active = [g for g in goals if g.status == GoalStatus.ACTIVE]
        paused = [g for g in goals if g.status == GoalStatus.PAUSED]
        completed = [g for g in goals if g.status == GoalStatus.COMPLETED]

        lines = [f"📋 目标总览 (共 {len(goals)} 个)\n"]
        if active:
            lines.append(f"🟢 活跃目标 ({len(active)}个):")
            priority_order = {"high": 0, "medium": 1, "low": 2}
            for goal in sorted(active, key=lambda g: priority_order.get(g.priority.value, 1)):
                lines.append(goal.get_summary())
                lines.append("")
        if paused:
            lines.append(f"\n⏸️ 暂停目标 ({len(paused)}个):")
            for goal in paused[:3]:
                lines.append(f"   - {goal.name}")
        if completed:
            lines.append(f"\n✅ 已完成 ({len(completed)}个)")
        return "\n".join(lines)

    def close(self) -> None:
        """关闭数据库连接（共享实例时仅释放引用，不关底层连接）。"""
        # ScheduleDB 的生命周期由持有方（plugin._db / PlanningServices）管理；
        # 这里保留 close 语义兼容源接口，但避免重复关闭共享连接。
        logger.debug("GoalManager.close() 调用（共享连接，由持有方统一关闭）")


# Global singleton
_goal_manager: Optional[GoalManager] = None


def get_goal_manager(data_dir: Optional[str] = None, db: Optional[ScheduleDB] = None) -> GoalManager:
    """获取全局 GoalManager 单例。

    Args:
        data_dir: 数据目录；仅首次创建单例时生效。整合入口（PlanningServices）
            会传入宿主隔离数据目录与共享的 ScheduleDB 实例。
        db: 共享的 ScheduleDB 实例（推荐传入 plugin._db 复用连接）。

    Returns:
        GoalManager 单例实例。
    """
    global _goal_manager
    if _goal_manager is None:
        _goal_manager = GoalManager(data_dir=data_dir, db=db)
    return _goal_manager


def close_goal_manager() -> None:
    """关闭并释放全局 GoalManager 单例（再次 get_goal_manager 会重建实例）。"""
    global _goal_manager
    if _goal_manager is not None:
        _goal_manager = None
