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

import asyncio
import logging
import tempfile
import uuid
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.time_utils import parse_hhmm
from .schedule_db import ScheduleDB
from .time_utils import TimezoneManager, wall_time, elapsed_seconds, shift_elapsed

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
            # 未接线场景（独立测试/单例先于服务束创建）绝不写插件代码目录：
            # 宿主隔离数据目录由 PlanningServices 必传（services.py 已做 None 前置校验）。
            data_dir = Path(tempfile.gettempdir())
            logger.warning("GoalManager 未收到 data_dir，回退系统临时目录：%s", data_dir)
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = db if db is not None else ScheduleDB(self.data_dir / db_name)
        self.tz_manager = TimezoneManager()
        self._clock_provider: Optional[Any] = None
        #: 进程内事件总线（M0；由 PlanningServices 装配时绑定，未绑定=静默跳过事件）
        self._bus: Optional[Any] = None
        self._commitment_required_owners: tuple[str, ...] = ()
        self._commitment_dispatch_task = None
        self._commitment_dispatch_lock = asyncio.Lock()
        logger.debug("GoalManager 初始化完成，数据库: %s", self.data_dir / db_name)

    def bind_event_bus(self, bus: Optional[Any], *, required_owners: tuple[str, ...] = ()) -> None:
        """装配必要消费者；缺任一消费者时保留 outbox，默认兼容独立调用。"""
        self._bus = bus
        self._commitment_required_owners = tuple(required_owners)

    def bind_clock_provider(self, provider: Optional[Any]) -> None:
        """动态获取配置区时钟；独立构造仍回退既有 tz_manager。"""
        self._clock_provider = provider

    def _clock(self) -> TimezoneManager:
        provider = getattr(self, "_clock_provider", None)
        return provider() if callable(provider) else self.tz_manager

    # ---- 变更通知（睡眠 loop 内部重检口；PR3 复核 2026-10-27）----
    def set_change_listener(self, listener: Optional[Any]) -> None:
        """挂/卸约定/日程写入监听（``listener(reason)``；None = 断开）。

        只用于唤醒生活状态 loop 重算硬约定（睡眠中新增临近约定要即时醒，不固定等
        下一个 tick）；不推进状态、不发消息、不调模型。
        """
        self._change_listener = listener

    def _notify_change(self, reason: str) -> None:
        listener = getattr(self, "_change_listener", None)
        if listener is None:
            return
        try:
            listener(reason)
        except Exception:
            logger.debug("GoalManager 变更通知失败（忽略）：%s", reason, exc_info=True)

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
        hardness: Optional[str] = None,
        hard_source: str = "",
    ) -> Goal:
        """添加一条未来约定（日期归一化，格式不统一也能正确匹配）。"""
        normalized_date = _normalize_commitment_date(commitment_date, self._clock().get_now())
        parameters = {
            "commitment_date": normalized_date,
            "time": time,
            "notes": notes,
            "reason": reason,
            "source": "user_request",
            "commitment_lifecycle": 1,
        }
        if hardness in ("soft", "hard"):
            parameters.update(hardness=hardness, hard=hardness == "hard", hard_source=hard_source)
        goal = self.create_goal(
            name=title,
            description=notes or title,
            goal_type="pending_commitment",
            creator_id=creator_id,
            chat_id="global",
            priority="medium",
            parameters=parameters,
        )
        # 睡眠中新增临近约定要即时重算（只唤醒睡眠 loop，不推进状态/不调模型）
        self._notify_change("commitment_added")
        return goal

    def accept_timed_commitment(
        self, date: str, title: str, when: str, *,
        notes: str = "", reason: str = "", source: str = "user_request",
        duration: int = 60, activity_type: str = "daily_routine", priority: str = "high",
        publish_today: bool = False, existing_goal_id: Optional[str] = None,
        publication: Optional[Callable[[str], None]] = None,
    ) -> Optional[Goal]:
        """Accept or repair a stable hard commitment and confirm its resulting publication.

        The database owns identity matching and the atomic goal/day-row write. A repair
        returns None if cancellation won the transaction. Publication errors propagate,
        retaining the accepted goal for the existing retry/reconciliation path.
        """
        goal_id = self.db.accept_timed_commitment(
            date, title, when, notes=notes, reason=reason, source=source,
            duration=duration, activity_type=activity_type, priority=priority,
            publish_today=publish_today, existing_goal_id=existing_goal_id)
        if not goal_id:
            return None
        stored = self.db.get_goal(goal_id)
        if stored is None:
            raise RuntimeError("accepted commitment has no persistent goal")
        if publication is not None:
            publication(date)
        return Goal.from_dict(stored)

    def get_pending_commitments(self, commitment_date: Optional[str] = None) -> List[Goal]:
        """查询未来约定（仅 status=active；commitment_date 留空 = 今天）。"""
        if commitment_date is None:
            commitment_date = self._clock().get_now().strftime("%Y-%m-%d")
        else:
            commitment_date = _normalize_commitment_date(commitment_date, self._clock().get_now())
        candidates = self.get_all_goals(chat_id="global", status=GoalStatus.ACTIVE)
        return [
            g for g in candidates
            if g.goal_type == "pending_commitment"
            and (g.parameters or {}).get("commitment_date") == commitment_date
        ]

    def consume_pending_commitments(self, commitment_date: str,
                                   *, matched_titles: Optional[Any] = None) -> List[Goal]:
        """兼容旧入口：排入计划不能消费约定，实际结算由 reconcile_commitments 驱动。"""
        return []

    def reconcile_commitments(self, now: Optional[datetime] = None, *, grace_minutes: int = 10) -> int:
        """按持久执行证据/到期补扫结算；重启、重排及上次写库失败均可安全重试。

        仅处理新生命周期或已排入新日表的 active 约定，既有 completed 历史不倒写。
        completed episode 才是兑现证据；仍在执行的约定即使超过窗口也等待完成。
        """
        clock = self._clock()
        moment = clock.to_local(now)
        settled = 0
        goals = self.db.get_all_goals(chat_id="global", status="active")
        episodes_by_date: Dict[str, List[Dict[str, Any]]] = {}
        for row in goals:
            if row.get("goal_type") != "pending_commitment":
                continue
            goal = Goal.from_dict(row)
            params = goal.parameters or {}
            plan = params.get("commitment_plan") or {}
            if not plan and not params.get("commitment_lifecycle"):
                continue
            date = str(plan.get("schedule_date") or params.get("commitment_date") or "")
            try:
                day = datetime.strptime(date, "%Y-%m-%d").date()
                base = wall_time(day, 0, clock._tz)
                end = int(plan.get("end_minute") or 0)
                if not end:
                    pieces = str(params.get("time") or "").split(":")
                    end = int(pieces[0]) * 60 + int(pieces[1]) + 60 if len(pieces) == 2 else 1440
                end_at = wall_time(day, end, clock._tz)
                due = shift_elapsed(end_at, grace_minutes * 60)
            except (TypeError, ValueError):
                logger.warning("约定到期信息无效，保留待处理: id=%s", goal.goal_id)
                continue
            # Recover unexpired, explicitly timed appointments from older releases which
            # stored the goal but had already generated its day. Never reconstruct past activity.
            minute = parse_hhmm(str(params.get("time") or ""), -1)
            if (not plan and params.get("commitment_lifecycle") and params.get("hardness") != "soft"
                    and minute >= 0 and day >= moment.date() and elapsed_seconds(end_at, moment) > 0
                    and self.db.has_day_schedule(date)):
                self.accept_timed_commitment(date, goal.name, str(params["time"]),
                    notes=str(params.get("notes") or ""), reason=str(params.get("reason") or ""),
                    source=str(params.get("hard_source") or "user_request"),
                    duration=max(1, int(params.get("duration_minutes") or 60)),
                    activity_type=str(params.get("activity_type") or "daily_routine"),
                    priority=str(params.get("activity_priority") or "high"), publish_today=False,
                    existing_goal_id=goal.goal_id)
            if elapsed_seconds(base, moment) > 0:
                continue
            if date not in episodes_by_date:
                episodes_by_date[date] = self.db.episodes_today(date)
            evidence = []
            for episode in episodes_by_date[date]:
                meta = episode.get("metadata") or {}
                if (str(meta.get("commitment_id") or "") == goal.goal_id
                        or (plan.get("item_id") and plan["item_id"] in
                            (meta.get("commitment_item_id"), meta.get("intent_item_id")))):
                    evidence.append(episode)
            completed = any(e.get("status") == "completed" and e.get("actual_start_minute") is not None
                            and e.get("actual_end_minute") is not None
                            and elapsed_seconds(wall_time(day, int(e["actual_end_minute"]), clock._tz), moment) <= 0
                            for e in evidence)
            active = any(e.get("status") == "active" for e in evidence)
            if not completed and (elapsed_seconds(moment, due) < 0 or active):
                continue
            kept = completed
            title = str(goal.name or "").strip()
            when = str(params.get("time") or "").strip()
            notes = str(params.get("notes") or "").strip()
            if self.db.settle_commitment(goal.goal_id, "kept" if kept else "broken", {
                "summary": f"{title}（{when}）" if when else title,
                "commitment": title, "time": when, "notes": notes,
                "date": str(params.get("commitment_date") or ""), "matched": kept,
            }):
                settled += 1
        self._schedule_commitment_dispatch()
        return settled

    def _schedule_commitment_dispatch(self) -> None:
        if self._bus is None or (self._commitment_dispatch_task is not None
                                 and not self._commitment_dispatch_task.done()):
            return
        try:
            self._commitment_dispatch_task = asyncio.get_running_loop().create_task(self.flush_commitment_events())
        except RuntimeError:
            pass  # 同步独立调用保留 outbox，下一次运行中的 tick 继续发送。

    async def flush_commitment_events(self) -> int:
        """确认发布后再确认 outbox；发布/确认失败保留，稳定 event_id 供订阅方幂等。"""
        if self._bus is None:
            return 0
        delivered = 0
        async with self._commitment_dispatch_lock:
            try:
                for event in self.db.pending_commitment_events():
                    confirmed = getattr(self._bus, "publish_confirmed", None)
                    name = "schedule.commitment_" + event["outcome"]
                    required = self._commitment_required_owners
                    if callable(confirmed):
                        options = {"required_owners": required} if required else {}
                        if not await confirmed(name, source="planning", **options, **event["payload"]):
                            logger.warning("约定结算订阅者尚未全部确认: event_id=%s", event["event_id"])
                            continue
                    else:
                        if required:
                            logger.warning("约定总线不支持必要消费者确认，保留 outbox: event_id=%s", event["event_id"])
                            continue
                        await self._bus.publish(name, source="planning", **event["payload"])
                    self.db.acknowledge_commitment_event(event["event_id"])
                    delivered += 1
            except Exception:
                logger.warning("约定结算事件发布未完成，保留 outbox 等待重试", exc_info=True)
        return delivered

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
        if item.get("day_offset") is not None:
            # A-16 日期帧标记透传（裁决①）：注入/视图侧据此消解帧首双关
            parameters["day_offset"] = int(item.get("day_offset") or 0)
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
            now = self._clock().get_now()
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
            date_str = self._clock().get_now().strftime("%Y-%m-%d")

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
        """删除目标：goals 表未命中时回退删除日程条目（合成 Goal 场景）。

        2026-10-27 用户批准：删除约定要**立即撤销**原提前唤醒 → 删除成功后通知
        内部重检口（``request_life_recheck`` 链），由睡眠引擎按当前有效约定重算。
        """
        cancellation = self.db.cancel_commitment(goal_id, when=self._clock().get_now())
        if cancellation is not None:
            if cancellation:
                self._notify_change("commitment_removed")
            return cancellation
        if self.db.get_goal(goal_id) is not None:
            deleted = self.db.delete_goal(goal_id)
        else:
            deleted = self.db.delete_schedule_item(goal_id)
        if deleted:
            logger.debug("已删除目标: %s", goal_id)
            self._notify_change("commitment_removed")
        return deleted

    def _schedule_item_ids(self) -> set:
        """今日日程条目的 item_id 集合（合成 Goal 的写回路由）。"""
        now = self._clock().get_now()
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
            changed = self.db.delete_schedule_item(item_id)
            if changed:
                self._notify_change("schedule_item_removed")
            return changed
        # 其余字段（priority / time_window 等）日程条目暂不支持原位更新
        logger.debug("日程条目 %s 不支持字段更新: %s", item_id, list(kwargs))
        return False

    # ============================================================
    # 清理
    # ============================================================

    def cleanup_old_goals(self, days: int = 30) -> int:
        """清理 days 天前的已完成 / 已取消目标。"""
        cutoff_date = self._clock().get_now() - timedelta(days=days)
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
