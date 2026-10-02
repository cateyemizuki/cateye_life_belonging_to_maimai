"""每日定时生成循环（来源：自主规划插件 planner/auto_scheduler.py，移植版）。

后台任务：每天到点（``auto_generate_time``）且当天无日程时自动生成并落库。
完全静默运行，不发送任何消息到聊天，只记录日志。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from .config import section_or_default, ScheduleSectionConfig

logger = logging.getLogger(__name__)


def _normalize_time_str(time_str: str, default: str) -> str:
    """规范化 HH:MM；非法输入回退默认值。"""
    try:
        hour_str, minute_str = str(time_str).split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except Exception:
        pass
    return default


class ScheduleAutoScheduler:
    """日程自动调度器：在每天配置的时间自动生成新一天的日程。

    Args:
        plugin: 插件实例（读配置 / 取 generator）
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
        self._last_schedule_date: Optional[str] = None

    # ------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------

    async def start(self) -> None:
        """启动定时任务（幂等；未启用配置时只记日志）。"""
        if self.is_running:
            return
        cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
        if not cfg.auto_generate_enabled:
            logger.info("日程定时生成功能未启用")
            return
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("日程定时生成已启动 - 执行时间: %s", cfg.auto_generate_time)

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
    # 定时循环
    # ------------------------------------------------------------

    def _is_time_due(self, now: datetime, time_str: str, last_date: Optional[str]) -> bool:
        today_str = now.strftime("%Y-%m-%d")
        if last_date == today_str:
            return False
        normalized = _normalize_time_str(time_str, "00:30")
        hour_str, minute_str = normalized.split(":")
        target_time = now.replace(hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0)
        return now >= target_time

    def _seconds_until_next(self, now: datetime, time_str: str, last_date: Optional[str]) -> float:
        normalized = _normalize_time_str(time_str, "00:30")
        hour_str, minute_str = normalized.split(":")
        target_time = now.replace(hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0)
        today_str = now.strftime("%Y-%m-%d")

        if last_date != today_str and now < target_time:
            return max(1.0, (target_time - now).total_seconds())

        next_target = target_time + timedelta(days=1)
        return max(1.0, (next_target - now).total_seconds())

    async def _schedule_loop(self) -> None:
        """定时任务循环：计算下次执行时间并等待；单次失败不影响后续执行。"""
        while self.is_running:
            try:
                now = datetime.now()
                cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
                schedule_time_str = cfg.auto_generate_time
                today_str = now.strftime("%Y-%m-%d")

                if self._is_time_due(now, schedule_time_str, self._last_schedule_date):
                    self._last_schedule_date = today_str
                    if not bool(getattr(cfg, "enabled", True)):
                        logger.info("日程规划模块已停用（schedule.enabled=false），跳过 %s 的自动生成", today_str)
                        self._retry_count = 0
                        continue
                    await self._generate_today_schedule()
                    self._retry_count = 0
                    continue

                wait_seconds = max(
                    15.0,
                    self._seconds_until_next(now, schedule_time_str, self._last_schedule_date),
                )
                await asyncio.sleep(wait_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时任务出错: {e}", exc_info=True)
                # 指数退避重试（30s, 60s, 120s, 240s, 300s）
                self._retry_count += 1
                wait_time = min(30 * (2 ** (self._retry_count - 1)), self._max_retry_wait)
                logger.info(f"将在 {wait_time} 秒后重试（第 {self._retry_count} 次）")
                await asyncio.sleep(wait_time)

    async def _generate_today_schedule(self) -> None:
        """生成今日日程（当天已有日程则跳过；完全静默运行）。"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            logger.info(f"开始自动生成今日日程: {today}")

            # 当天已有日程 → 跳过（plugin._db 即 ScheduleDB）
            db = getattr(self.plugin, "_db", None)
            if db is not None and db.has_day_schedule(today):
                count = len(db.get_day_schedule(today))
                logger.info(f"今日已有 {count} 个日程，跳过自动生成")
                return

            generator = self._resolve_generator()
            if generator is None:
                logger.warning("自动生成日程失败：无可用的 ScheduleGenerator")
                return

            cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
            personality = str(getattr(self.plugin, "_personality", "") or "")
            lover_name = str(getattr(self.plugin, "_lover_name", "") or "麦麦")

            items = await generator.generate_daily_schedule(
                today,
                personality=personality,
                lover_name=lover_name,
                wake_time=cfg.wake_time,
                sleep_time=cfg.sleep_time,
                min_activities=cfg.min_activities,
                max_activities=cfg.max_activities,
                timeout_seconds=cfg.generation_timeout,
                use_multi_round=cfg.use_multi_round,
                max_rounds=cfg.max_rounds,
                quality_threshold=cfg.quality_threshold,
                enable_detailed_description=cfg.enable_detailed_description,
                min_description_length=cfg.min_description_length,
                max_description_length=cfg.max_description_length,
                max_tokens=cfg.max_tokens,
                custom_prompt=cfg.custom_prompt,
                recent_schedule_days=cfg.recent_schedule_days,
                history_message_limit=cfg.history_message_limit,
                knowledge_search_limit=cfg.knowledge_search_limit,
                cross_day_activity=cfg.cross_day_activity,
                no_sleep_mode=cfg.no_sleep_mode,
                timezone=cfg.timezone,
                llm_task_name=cfg.llm_task_name,
            )
            logger.info("自动生成日程成功: %s - 共 %d 个活动", today, len(items))

        except Exception as e:
            logger.error(f"自动生成日程出错: {e}", exc_info=True)

    def _resolve_generator(self) -> Optional[Any]:
        """取生成器：优先工厂注入；回退 plugin._schedule_gen。"""
        if self._generator_factory is not None:
            try:
                return self._generator_factory()
            except Exception as exc:
                logger.warning(f"generator_factory 调用失败: {exc}")
        return getattr(self.plugin, "_schedule_gen", None)
