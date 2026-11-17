"""每日定时生成循环。

日程总开关启用后，按本夜入睡点提前生成下一日意图。每天至多成功一次，
失败退避后重试；不直接发送聊天消息。生成时刻按起床锚点形成的日程日换算，
并限制在下一日起点之前至少30分钟。已退役的相对延迟及旧绝对时刻不参与判定。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from core.llm_utils import llm_task_name_of
from core.time_utils import parse_hhmm

from .config import (
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
    section_or_default,
)
from .time_utils import (DAY_MINUTES, TimezoneManager, wall_time as _wall_time,
                         elapsed_seconds as _elapsed_seconds, shift_elapsed as _shift_elapsed)

logger = logging.getLogger(__name__)

#: 硬约束（B2）：生成时刻距**新一天起点**的最小提前量（分钟）——"至少新一天开始前 30 分钟生成"
GENERATE_LEAD_MINUTES = 30
#: 缺省作息锚点（配置缺失/非法时兜底）：07:00 起床 / 23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60
def in_frame_minutes(clock_minutes: int, wake_minutes: int) -> int:
    """钟点分钟数 → **当日帧相对分钟**：``(clock − wake) mod 1440``（帧起点 = 当日 wake 锚点）。

    早于帧起点的钟点回绕到**帧尾**——夜猫子作息（sleep ≤ wake）的入睡锚点、以及跨午夜
    条目落在凌晨的那一段（如 23:00 入睡 + 8h ⇒ 1380→1860，次日 00:00–07:00 段）都落在
    帧尾（2026-10-10 N2/N4 起条目跨午夜保留原时长，不再被截断到入睡锚点）。
    """
    return (int(clock_minutes) - int(wake_minutes)) % DAY_MINUTES




def clamp_due_minutes(due_minutes: int) -> int:
    """硬约束（B2）：帧内到期分钟数 ≤ ``1440 − 30``（= 新一天起点前 30 分钟）；超出强制拉回。"""
    return min(int(due_minutes), DAY_MINUTES - GENERATE_LEAD_MINUTES)










def schedule_day_start(now: datetime, wake_minutes: int) -> datetime:
    """当前所处的「日程日」起点 = 不晚于 ``now`` 的最近一次 **wake 锚点**（F16 新一天起点）。

    例：wake 07:00 时，00:30 仍属**前一天**的帧（帧跨越午夜），07:00 之后属当天。
    """
    start = _wall_time(now.date(), wake_minutes, now.tzinfo)
    if _elapsed_seconds(start, now) > 0:
        start = _wall_time(now.date() - timedelta(days=1), wake_minutes, now.tzinfo)
    return start


class ScheduleAutoScheduler:
    """日程自动调度器：在每天（相对锚点算出的）到期时刻自动生成新一天的日程。

    Args:
        plugin: 插件实例（读配置 / 取 generator / 取 ScheduleDB）
        generator_factory: 返回 ScheduleGenerator 的可调用（PlanningServices 注入；
            缺省回退用 plugin._schedule_gen）
    """

    def __init__(self, plugin: Any, generator_factory: Optional[Any] = None) -> None:
        self.plugin = plugin
        self._generator_factory = generator_factory
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        # 指数退避参数（单次失败不阻塞后续执行）
        self._retry_count = 0
        self._max_retry_wait = 300  # 最大等待 5 分钟
        # "每日一次"防重：记录**已成功生成/已处理的目标日**（= 新一天，非当前日历日）。
        # 2026-11-04 修复（评估问题5）：生成成功后才标记；失败不标记 → 退避后可重试。
        self._last_schedule_date: Optional[str] = None

    # ------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------

    async def start(self) -> None:
        """启动定时任务（幂等；未启用配置时只记日志）。"""
        if self.is_running:
            return
        cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("日程定时生成已启动 - %s", self._describe_timing(self._timing_view()))

    async def stop(self) -> None:
        """停止定时任务并等待退出。"""
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        logger.info("日程定时任务已停止")

    # ------------------------------------------------------------
    # 到期时刻（B2：按日期 + 相对锚点；全部可注入时钟直测）
    # ------------------------------------------------------------

    def _timing_view(self) -> Any:
        """唯一生成时刻来自日程基础配置。"""
        return section_or_default(self.plugin, 'schedule', ScheduleSectionConfig)

    def _clock(self) -> TimezoneManager:
        advanced = (section_or_default(self.plugin, "schedule_advanced", ScheduleAdvancedSectionConfig)
                    if getattr(self.plugin, "config", None) is not None else ScheduleAdvancedSectionConfig())
        return TimezoneManager(advanced.timezone)

    def _local_time(self, value: Optional[datetime] = None) -> datetime:
        """唯一调度时钟；注入的 naive 时间按配置时区墙钟解释。"""
        return self._clock().to_local(value)

    def _describe_timing(self, cfg: Any) -> str:
        return f"日意图在本夜入睡参考前 {GENERATE_LEAD_MINUTES} 分钟生成，当前日程结束后接替"

    def _anchors(self, cfg: Any) -> Tuple[int, int]:
        """``(wake_minutes, sleep_minutes)`` 作息锚点（F16；缺省/非法 07:00 / 23:00）。"""
        wake = parse_hhmm(str(getattr(cfg, "wake_time", "") or ""), _DEFAULT_WAKE_MINUTES)
        sleep = parse_hhmm(str(getattr(cfg, "sleep_time", "") or ""), _DEFAULT_SLEEP_MINUTES)
        return wake, sleep

    def _due_minutes_in_frame(self, frame_start: datetime, cfg: Any) -> int:
        """兼容分钟查询；DST 日帧按实际经过分钟返回。"""
        local_start = self._local_time(frame_start)
        return int(_elapsed_seconds(self._due_at_for_frame(local_start, cfg), local_start) / 60)

    def _due_at_for_frame(self, frame_start: datetime, cfg: Any) -> datetime:
        """每次重读最早允许入睡点，锚点按配置区日历、提前量按真实秒计算。"""
        wake_min, sleep_min = self._anchors(cfg)
        sleep_at = _wall_time(frame_start.date(), wake_min + in_frame_minutes(sleep_min, wake_min),
                              frame_start.tzinfo)
        next_start = _wall_time(frame_start.date() + timedelta(days=1), wake_min, frame_start.tzinfo)
        engine = getattr(self.plugin, "_sleep_engine", None)
        try:
            reference = _shift_elapsed(sleep_at, -3600)
            window = engine.dynamic_window(reference) if engine is not None else None
            earliest = getattr(window, "start", None)
            if isinstance(earliest, datetime):
                earliest = self._local_time(earliest)
                if (_elapsed_seconds(earliest, frame_start) >= 0
                        and _elapsed_seconds(earliest, next_start) < 0
                        and _elapsed_seconds(earliest, sleep_at) < 0):
                    sleep_at = earliest
        except Exception:
            logger.debug("动态睡眠窗不可得，使用配置入睡锚点", exc_info=True)
        due = _shift_elapsed(sleep_at, -GENERATE_LEAD_MINUTES * 60)
        cap = _shift_elapsed(next_start, -GENERATE_LEAD_MINUTES * 60)
        return max((frame_start, min((due, cap), key=lambda dt: dt.timestamp())),
                   key=lambda dt: dt.timestamp())

    def _due_for_frame(self, frame_start: datetime, cfg: Any) -> Tuple[str, datetime]:
        """某「日程日」帧对应的 ``(目标日, 到期时刻)``（**目标日** = 帧起点 +1 天 = 新一天）。"""
        frame_start = self._local_time(frame_start)
        target_date = (frame_start.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        due_at = self._due_at_for_frame(frame_start, cfg)
        return target_date, due_at

    def _pending_generation(self, now: datetime, cfg: Any) -> Tuple[str, datetime]:
        """下一个**尚未生成**的目标日 ``T`` 与其到期时刻（已生成 → 顺延到下一帧）。

        顺延只可能发生在"本帧目标日刚生成过"的场景（每日至多一次），故两帧足够；
        到期时刻用**目标日的上一帧**（该帧的日程已存在）计算，与生产时序一致。
        """
        now = self._local_time(now)
        frame_start = schedule_day_start(now, self._anchors(cfg)[0])
        target_date, due_at = self._due_for_frame(frame_start, cfg)
        if self._last_schedule_date != target_date:
            return target_date, due_at
        next_start = _wall_time(frame_start.date() + timedelta(days=1), self._anchors(cfg)[0], now.tzinfo)
        return self._due_for_frame(next_start, cfg)

    def _is_time_due(self, now: datetime, target_date: str, due_at: datetime) -> bool:
        """到期判定（B2）：目标日**尚未生成**，且当前时刻已到/已过它的到期时刻。

        沿用既有"每日一次"语义：已过到期时刻且目标日尚未生成 → 到期（立即生成）；
        ``_last_schedule_date`` 已记该目标日 → 不再触发（跨日由 :meth:`_pending_generation`
        推进到下一帧，重新判定）。
        """
        return (self._last_schedule_date != target_date
                and _elapsed_seconds(self._local_time(now), self._local_time(due_at)) >= 0)

    def _seconds_until_next(self, now: datetime, cfg: Any) -> float:
        """距下一个待生成目标日到期还有多少秒（已到期未生成 → 1s，交给到期判定立刻触发）。"""
        _target_date, due_at = self._pending_generation(now, cfg)
        return max(1.0, _elapsed_seconds(due_at, self._local_time(now)))

    # ------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------

    async def _schedule_loop(self) -> None:
        """定时任务循环：计算下次执行时间并等待；单次失败不影响后续执行（结构不变）。"""
        while self.is_running:
            try:
                now = self._local_time()
                cfg = self._timing_view()
                target_date, due_at = self._pending_generation(now, cfg)

                if self._is_time_due(now, target_date, due_at):
                    if not bool(getattr(cfg, "enabled", True)):
                        logger.info("日程规划模块已停用（schedule.enabled=false），跳过 %s 的自动生成", target_date)
                        # 停用 = 有意跳过，仍记为已处理（防每拍重复判定/日志热循环）
                        self._last_schedule_date = target_date
                        self._retry_count = 0
                        continue
                    # 2026-11-04 修复（评估问题5）：先标记后生成 → 生成失败当日不再
                    # 重试。现改为**生成成功（或确认已有 spine 日表）后才标记**；
                    # 失败保留未标记状态并走指数退避重试（generator 正常返回即已落库，
                    # 见 schedule_generator._generate_spine_schedule 的 apply 分支）。
                    generated = await self._generate_today_schedule(target_date)
                    if generated:
                        self._last_schedule_date = target_date
                        self._retry_count = 0
                        continue
                    self._retry_count += 1
                    wait_time = min(30 * (2 ** (self._retry_count - 1)), self._max_retry_wait)
                    logger.warning("日程生成失败，%s 秒后重试（第 %s 次，目标日 %s）",
                                   wait_time, self._retry_count, target_date)
                    await asyncio.sleep(wait_time)
                    continue

                wait_seconds = max(15.0, self._seconds_until_next(now, cfg))
                await asyncio.sleep(min(60.0, wait_seconds))

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时任务出错: {e}", exc_info=True)
                # 指数退避重试（30s, 60s, 120s, 240s, 300s）
                self._retry_count += 1
                wait_time = min(30 * (2 ** (self._retry_count - 1)), self._max_retry_wait)
                logger.info(f"将在 {wait_time} 秒后重试（第 {self._retry_count} 次）")
                await asyncio.sleep(wait_time)

    def _target_date_for(self, now: Optional[datetime] = None) -> str:
        """缺省目标日：按 wake 锚点口径推出的"新一天"（``_generate_today_schedule`` 直调用）。"""
        now = self._local_time(now)
        cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
        frame_start = schedule_day_start(now, self._anchors(cfg)[0])
        return (frame_start.date() + timedelta(days=1)).strftime("%Y-%m-%d")

    async def _generate_today_schedule(self, date_str: Optional[str] = None) -> bool:
        """生成指定日期（缺省 = 当前帧的**新一天**）的日程；该日已有日程则跳过；完全静默。

        Returns:
            True = 已生成落库 / 该日已有 spine 日表（调用方可标记该目标日已处理）；
            False = 生成失败（generator 不可得 / 异常；调用方应保留重试，不标记）。
            2026-11-04 修复（评估问题5）：原恒返回 None 且吞异常，调用方无从区分成败。
        """
        try:
            target = date_str or self._target_date_for()
            logger.info(f"开始自动生成日程: {target}")

            # 目标日已有日程 → 仅当已是 spine 日表才跳过（PR8 3.0.0 / 裁定 6：
            # 升级前旧 daily 当天/未来日表不阻断新主线；旧行保留为历史，仅以 spine
            # 重建当前/未来日表，不批量改写已执行历史）。
            db = getattr(self.plugin, "_db", None)
            if db is not None and db.has_day_schedule(target):
                rows = list(db.get_day_schedule(target) or [])
                from .rolling_planner import extract_spine_intents
                if extract_spine_intents(rows):
                    from .cycle_state import (enter_schedule_cycle, entry_context_from_plugin,
                        REASON_SCHEDULED, REASON_REGENERATE, REASON_TOOL_EDIT, REASON_FALLBACK)
                    reasons = {REASON_SCHEDULED, REASON_REGENERATE, REASON_TOOL_EDIT, REASON_FALLBACK}
                    # Resume the persisted publication, including same-day edits,
                    # without turning it into a new scheduled rotation.
                    entry_reason = next((str(row.get("entry_reason")) for row in rows
                                         if row.get("entry_reason") in reasons), REASON_SCHEDULED)
                    enter_schedule_cycle(getattr(self.plugin, "_data_dir", None), target, rows,
                        reason=entry_reason,
                        decision_logger=getattr(self.plugin, "_decision_logger", None),
                        **entry_context_from_plugin(self.plugin))
                    logger.info(f"{target} 已有 {len(rows)} 个 spine 日程，跳过自动生成")
                    return True
                logger.info(f"{target} 为旧非 spine 日表（{len(rows)} 行），按 3.0.0 重建 spine 日程")

            generator = self._resolve_generator()
            if generator is None:
                logger.warning("自动生成日程失败：无可用的 ScheduleGenerator")
                return False

            cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
            adv = section_or_default(self.plugin, "schedule_advanced", ScheduleAdvancedSectionConfig)
            personality = str(getattr(self.plugin, "_personality", "") or "")
            lover_name = str(getattr(self.plugin, "_lover_name", "") or "麦麦")

            # PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理）：spine 为唯一生成路线，
            # 恒走 spine 分支（生成器内部读 [schedule].spine_* 键；spine 专属参数在
            # 生成器侧覆盖，下列共享入参照常透传；旧 spine_mode 判定已删）
            items = await generator.generate_daily_schedule(
                target,
                personality=personality,
                lover_name=lover_name,
                wake_time=cfg.wake_time,
                sleep_time=cfg.sleep_time,
                min_activities=cfg.min_intentions,
                max_activities=cfg.max_intentions,
                timeout_seconds=adv.generation_timeout,
                use_multi_round=True,
                max_rounds=adv.max_rounds,
                quality_threshold=adv.quality_threshold,
                enable_detailed_description=cfg.enable_detailed_description,
                min_description_length=adv.min_description_length,
                max_description_length=adv.max_description_length,

                custom_prompt="",
                recent_schedule_days=adv.recent_schedule_days,
                history_message_limit=adv.history_message_limit,
                knowledge_search_limit=adv.knowledge_search_limit,
                cross_day_activity=True,
                no_sleep_mode=cfg.no_sleep_mode,
                timezone=adv.timezone,
                llm_task_name=llm_task_name_of(self.plugin),
                mode="spine",
            )
            logger.info("自动生成日程成功: %s - 共 %d 个活动（spine 日意图）",
                        target, len(items))
            # generator 正常返回 = 已走 apply_schedule_items/_replace_with_entry 落库
            return True

        except Exception as e:
            logger.error(f"自动生成日程出错: {e}", exc_info=True)
            return False

    def _resolve_generator(self) -> Optional[Any]:
        """取生成器：优先工厂注入；回退 plugin._schedule_gen。"""
        if self._generator_factory is not None:
            try:
                return self._generator_factory()
            except Exception as exc:
                logger.warning(f"generator_factory 调用失败: {exc}")
        return getattr(self.plugin, "_schedule_gen", None)
