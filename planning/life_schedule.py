"""生活日程读模型 LifeScheduleReadModel（R7 依据 = 用户 2026-10-25 直派【日程系统
滚动化改造，设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统
滚动化改造设计，附录 B.5.3 / 主设计 §7.2】）。

对上层提供 ``current / next / today / state_line`` 四个只读口（0 LLM、只读不落库），
把「episode 日志（activity_episodes）+ spine 意图（schedule_items.parameters.spine=true）
+ 恢复池 + RollingPlanner 预演」合并成统一视图：

- :meth:`current` 优先级（主设计 §7.2）：进行中 episode > 已计划未开始 episode >
  恢复池可恢复 paused > None（空闲）；
- :meth:`next` = merge(planned_future, paused_recent, planner.preview, spine 意图)[:limit]；
- :meth:`today` = 当日 spine 意图 + 已发生 episode 汇总；
- :meth:`state_line` = ``LifeStateService.describe``（LifeState 缺失 → 空串，调用方
  回退静态 ``describe_energy``——§6.6 保留旧函数口径）。

include_sleep 语义（**不可弄断的情绪链**）：

- ``include_sleep=False``：跳过 sleep episode（与 ``planning/schedule_query.py`` 的
  ``_SKIP_TYPES = {sleep}`` 完全一致）；
- ``include_sleep=True``：允许返回 sleep episode——``emotion/service.py``（约 :1430
  睡眠条目判定 / :2532 睡眠大修入口）与 ``qzone/service.py``（约 :450 睡眠发帖）的
  ``find_current_activity(..., include_sleep=True)`` 链依赖此语义，runtime provider
  **不能把睡眠 episode 过滤掉**。

返回 dict 兼容 schedule_items 行形态（name/goal_type/start_minute/end_minute/
day_offset/description），既有 ``format_activity_line`` / ``is_night_sleep_item`` 等
消费者零改动可读；runtime 附加键（episode_id/status/progress/attention_mode 等）另附。

本模块只读、零 LLM、不新增任何主动开口入口（纪律 5）；异常一律静默降级返回
None/空，让 ``ScheduleQuery`` 回退旧时间窗路径（P2 读取侧灰度口径）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.time_utils import minutes_of_now

from .rolling_planner import extract_spine_intents
from .time_utils import (TimezoneManager, is_in_day_frame,
                         is_upcoming_day_minute, ring_minutes_ahead)

logger = logging.getLogger(__name__)

#: sleep 类型条目跳过集（与 schedule_query._SKIP_TYPES = {sleep} 对齐——口径注释，勿改语义）
_SKIP_SLEEP_TYPES = {"sleep"}
#: resume 恢复门槛（与 RollingPlanner §5.5/B.6.3 同口径）
_RESUME_MIN_PROGRESS = 0.2
_RESUME_BONUS_FLOOR = 0.25


class LifeScheduleReadModel:
    """生活日程只读门面（B.5.3）：episode/spine/恢复池/LifeState 的统一 current/next/today/state。

    Args:
        db: :class:`~planning.schedule_db.ScheduleDB`。
        life_state: :class:`~planning.life_state.LifeStateService`（可选；缺失 →
            :meth:`state_line` 返回空串）。
        planner: :class:`~planning.rolling_planner.RollingPlanner`（可选；缺失 →
            :meth:`next` 不含 planner 预演、恢复池按 B.6.3 本地判定）。
        config_provider: 返回插件配置的可调用（``[life_state].enabled`` 读此处；
            段缺失/异常 → ``enabled=False``——默认关纪律）。
    """

    def __init__(self, db: Any, *, life_state: Any = None, planner: Any = None,
                 config_provider: Any = None, timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None) -> None:
        self._db = db
        self._life = life_state
        self._planner = planner
        self._config_provider = config_provider
        self._tz = timezone_manager or TimezoneManager(timezone_str)

    # ── 配置（默认关：段缺失/异常一律 False，旧行为不变）──────────────────
    def _cfg_section(self) -> Any:
        provider = self._config_provider
        if provider is None:
            return None
        try:
            cfg = provider() if callable(provider) else provider
        except Exception:
            return None
        return getattr(cfg, "life_state", None)

    @property
    def enabled(self) -> bool:
        """``[life_state].enabled``（ScheduleQuery 据此决定是否走 runtime 路径）。"""
        return bool(getattr(self._cfg_section(), "enabled", False))

    # ── 时间助手 ─────────────────────────────────────────────────────────
    def _now(self, now: Optional[datetime]) -> datetime:
        dt = now or self._tz.get_now()
        if dt.tzinfo is None:
            tz = getattr(self._tz, "_tz", None)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt

    def _date_of(self, now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    # ── episode 行 → schedule_item 兼容形态（既有消费者零改动）────────────
    @staticmethod
    def _episode_as_item(episode: Dict[str, Any], *, paused: bool = False) -> Dict[str, Any]:
        meta = episode.get("metadata") if isinstance(episode.get("metadata"), dict) else {}
        actual_start = episode.get("actual_start_minute")
        start = int(actual_start) if actual_start is not None else int(episode.get("planned_start_minute") or 0)
        actual_end = episode.get("actual_end_minute")
        end = int(actual_end) if actual_end is not None else int(episode.get("planned_end_minute") or 0)
        return {
            # schedule_items 兼容键
            "item_id": episode.get("episode_id"),
            "name": episode.get("name"),
            "goal_type": episode.get("goal_type"),
            "description": str(meta.get("note") or meta.get("description") or ""),
            "start_minute": start,
            "end_minute": end,
            "day_offset": episode.get("day_offset"),
            "parameters": {},
            # runtime 附加键
            "episode_id": episode.get("episode_id"),
            "status": episode.get("status"),
            "progress": episode.get("progress"),
            "attention_mode": episode.get("attention_mode"),
            "interruptibility": episode.get("interruptibility"),
            "switch_cost": episode.get("switch_cost"),
            "source": episode.get("source"),
            "resume_of": episode.get("resume_of"),
            "is_runtime_episode": True,
            "paused": bool(paused),
        }

    @staticmethod
    def _is_sleep(episode_or_item: Dict[str, Any]) -> bool:
        return str(episode_or_item.get("goal_type") or "") in _SKIP_SLEEP_TYPES

    # ================================================================
    # current（§7.2 优先级：active > planned 到点 > 恢复池 > None）
    # ================================================================
    def current(self, now: Optional[datetime] = None,
                include_sleep: bool = False) -> Optional[Dict[str, Any]]:
        """当前活动（runtime 口径）；取不到/异常 → ``None``（调用方回退旧时间窗路径）。

        ``include_sleep=False`` 跳过 sleep episode（对齐 ``schedule_query._SKIP_TYPES``）；
        ``True`` 允许返回 sleep episode（emotion 睡眠大修 / qzone 睡眠发帖链依赖）。
        """
        try:
            return self._current_inner(now, include_sleep)
        except Exception as exc:
            logger.warning("life_schedule: current 读取失败（回退旧路径）：%s", exc)
            return None

    def _current_inner(self, now: Optional[datetime], include_sleep: bool) -> Optional[Dict[str, Any]]:
        now = self._now(now)
        date = self._date_of(now)
        cur = minutes_of_now(now)
        # 1) 进行中 episode（任意时刻最多一个 active）
        active = self._db.active_episode()
        if active is not None and (include_sleep or not self._is_sleep(active)):
            return self._episode_as_item(active)
        # 2) 已计划未开始、但时间窗已覆盖当前钟点的 planned episode
        for row in self._db.planned_future(date, limit=50):
            if not include_sleep and self._is_sleep(row):
                continue
            if is_in_day_frame(row.get("planned_start_minute") or 0,
                               row.get("planned_end_minute") or 0, cur):
                return self._episode_as_item(row)
        # 3) 恢复池可恢复 paused（B.6.3 门槛：progress ≥ 0.2、未过期、加成 > 0.25）
        for row in self._resume_candidates(now):
            if not include_sleep and self._is_sleep(row):
                continue
            return self._episode_as_item(row, paused=True)
        return None

    def _resume_candidates(self, now: datetime) -> List[Dict[str, Any]]:
        """恢复池可恢复行（planner 可用时复用其排序与衰减，否则本地按 B.6.3 判定）。"""
        if self._planner is not None:
            try:
                return list(self._planner.resume_pool(now))
            except Exception as exc:
                logger.warning("life_schedule: planner.resume_pool 失败（本地判定）：%s", exc)
        expire = 240  # 附录 B.4.1 默认（本层无配置时的保守回退）
        rows = self._db.paused_recent(limit=5)
        out: List[Dict[str, Any]] = []
        for row in rows:
            progress = row.get("progress")
            try:
                progress_f = float(progress)
            except (TypeError, ValueError):
                continue
            if progress_f < _RESUME_MIN_PROGRESS:
                continue
            updated_raw = row.get("updated_at")
            if isinstance(updated_raw, str) and updated_raw.strip():
                try:
                    updated = datetime.fromisoformat(updated_raw.strip())
                except ValueError:
                    updated = None
                if updated is not None:
                    try:
                        age_minutes = (self._now(now) - updated).total_seconds() / 60.0
                    except TypeError:
                        age_minutes = expire + 1
                    if age_minutes > expire:
                        continue
            out.append(row)
        return out

    # ================================================================
    # next（merge planned_future / paused_recent / planner.preview / spine 意图）
    # ================================================================
    def next(self, now: Optional[datetime] = None, limit: int = 2) -> List[Dict[str, Any]]:
        """接下来 ``limit`` 条（runtime 口径；跨午夜按同一个 24h 环排序）。

        合并源（§7.2）：planned_future > paused_recent（恢复池）> planner.preview 预演 >
        spine 意图（schedule_items ``parameters.spine=true`` 的未来行）；同 start 去重
        （episode 优先于预演/意图）；sleep 类型跳过（与 ``find_next_activities`` 口径一致）。
        """
        try:
            return self._next_inner(now, limit)
        except Exception as exc:
            logger.warning("life_schedule: next 读取失败（回退旧路径）：%s", exc)
            return []

    def _next_inner(self, now: Optional[datetime], limit: int) -> List[Dict[str, Any]]:
        now = self._now(now)
        date = self._date_of(now)
        cur = minutes_of_now(now)
        merged: List[Tuple[int, int, Dict[str, Any]]] = []  # (环距, 优先级, item)

        def add(item: Dict[str, Any], tier: int) -> None:
            if self._is_sleep(item):
                return  # 与 find_next_activities 的 _SKIP_TYPES 口径一致
            start = int(item.get("start_minute") or 0)
            merged.append((ring_minutes_ahead(start, cur), tier, item))

        # 源 1：已计划未开始 episode（真实已排）
        for row in self._db.planned_future(date, cur_minute=cur, limit=max(1, int(limit)) * 3):
            if not is_upcoming_day_minute(row.get("planned_start_minute") or 0, cur,
                                          row.get("planned_end_minute") or 0):
                continue
            add(self._episode_as_item(row), tier=0)
        # 源 2：恢复池（可恢复的 paused）
        for row in self._resume_candidates(now)[:2]:
            add(self._episode_as_item(row, paused=True), tier=1)
        # 源 3：planner 预演（只读；补足空位）
        planner = self._planner
        if planner is not None:
            try:
                for entry in planner.preview(now, limit=max(1, int(limit))):
                    horizon = max(0, int(entry.get("max_minutes") or 0)) or 30
                    item = {
                        "name": entry.get("name"), "goal_type": entry.get("goal_type"),
                        "description": "", "start_minute": cur,
                        "end_minute": cur + horizon,
                        "day_offset": 0, "parameters": {},
                        "is_preview": True, "kind": entry.get("kind"),
                        "score": entry.get("score"),
                    }
                    add(item, tier=2)
            except Exception as exc:
                logger.warning("life_schedule: planner.preview 失败（跳过预演源）：%s", exc)
        # 源 4：spine 意图未来行（schedule_items 的 spine=true 未到窗行）
        try:
            intents = extract_spine_intents(self._db.get_day_schedule(date))
        except Exception:
            intents = []
        for intent in intents:
            start = int(intent["start_minute"] or 0)
            if str(intent.get("day_offset")) == "1":
                continue  # 保守：午夜段不并入当日 next（§7.8 帧首双关）
            if not is_upcoming_day_minute(start, cur, int(intent["end_minute"] or 0)):
                continue
            add({
                "name": intent["name"], "goal_type": intent["goal_type"],
                "description": intent["note"] or intent["description"],
                "start_minute": start, "end_minute": int(intent["end_minute"] or 0),
                "day_offset": intent.get("day_offset"), "parameters": {},
                "is_spine_intent": True, "hardness": intent["hardness"],
                "priority": intent["priority"],
            }, tier=3)
        # 同钟点去重：episode(0) > 恢复池(1) > 预演(2) > 意图(3)——先到先得
        # （merged 已按 (环距, tier) 排序，真实行恒先于占位行）
        seen: Dict[int, Dict[str, Any]] = {}
        for _ahead, _tier, item in sorted(merged, key=lambda t: (t[0], t[1])):
            key = _ahead
            if key in seen:
                continue
            seen[key] = item
        return list(seen.values())[:max(0, int(limit))]

    # ================================================================
    # today（意图 + 已发生汇总）
    # ================================================================
    def today(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """当日汇总：spine 意图 + 全部 episode（实际发生）+ 完成统计（只读）。"""
        try:
            now = self._now(now)
            date = self._date_of(now)
            try:
                intents = extract_spine_intents(self._db.get_day_schedule(date))
            except Exception:
                intents = []
            try:
                episodes = self._db.episodes_today(date)
            except Exception:
                episodes = []
            completed = [e for e in episodes if str(e.get("status")) == "completed"]
            return {
                "date": date,
                "intents": intents,
                "episodes": episodes,
                "completed": completed,
                "summary": {
                    "intent_count": len(intents),
                    "episode_count": len(episodes),
                    "completed_count": len(completed),
                    "interrupted_count": sum(1 for e in episodes
                                             if str(e.get("status")) == "interrupted"),
                    "abandoned_count": sum(1 for e in episodes
                                           if str(e.get("status")) == "abandoned"),
                },
            }
        except Exception as exc:
            logger.warning("life_schedule: today 读取失败（返回空汇总）：%s", exc)
            return {"date": "", "intents": [], "episodes": [], "completed": [],
                    "summary": {"intent_count": 0, "episode_count": 0,
                                "completed_count": 0, "interrupted_count": 0,
                                "abandoned_count": 0}}

    # ================================================================
    # state_line（LifeStateService.describe；缺失 → 空串回退静态能量）
    # ================================================================
    def state_line(self, now: Optional[datetime] = None, max_items: int = 2) -> str:
        """一行生活状态（如「状态：有点累（精力 0.38），有点饿（饥饿 0.72）」）。

        LifeState 缺失/异常 → 空串（调用方回退 ``time_utils.describe_energy`` 静态曲线
        ——§6.6「保留旧函数作为回退」口径）。零 emoji。
        """
        if self._life is None:
            return ""
        try:
            return str(self._life.describe(self._now(now), max_items=max_items) or "")
        except Exception as exc:
            logger.warning("life_schedule: state_line 生成失败（回退静态能量）：%s", exc)
            return ""
