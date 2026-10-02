"""目标清理 / 自动调度 后台任务的业务实现
（来源：自主规划插件 services/cleanup_service.py，移植版）。

通过 ``PlanningServices.start`` 中 ``asyncio.create_task`` 启动循环，
``stop`` 中 cancel。清理范围：过期日程条目 / 旧目标。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from .auto_scheduler import ScheduleAutoScheduler
from .config import PlanningCleanupSectionConfig, section_or_default
from .goal_manager import get_goal_manager

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


class CleanupService:
    """目标清理与自动调度服务。"""

    def __init__(self, plugin: Any, generator_factory: Optional[Any] = None) -> None:
        """
        Args:
            plugin: 当前插件实例
            generator_factory: ScheduleGenerator 工厂（传给自动调度器）
        """
        self._plugin = plugin
        self._stop_event: asyncio.Event = asyncio.Event()
        self._generator_factory = generator_factory
        # 自动调度器实例（延迟到 run_scheduler_loop 创建，避免 __init__ 阶段事件循环未就绪）
        self._auto_scheduler: Optional[ScheduleAutoScheduler] = None

    # ------------------------------------------------------------
    # 后台任务 1：清理循环
    # ------------------------------------------------------------

    async def run_cleanup_loop(self) -> None:
        """定期清理过期日程与旧目标（间隔由 planning_cleanup.cleanup_interval 控制）。"""
        logger.info("日程清理循环已启动")
        try:
            while not self._stop_event.is_set():
                try:
                    await self._cleanup_old_goals()
                except Exception as exc:
                    logger.error(f"清理目标异常: {exc}", exc_info=True)

                cleanup_cfg = section_or_default(self._plugin, "planning_cleanup", PlanningCleanupSectionConfig)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=float(cleanup_cfg.cleanup_interval))
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.debug("清理循环被取消")
            raise
        finally:
            logger.info("日程清理循环已退出")

    async def _cleanup_old_goals(self) -> None:
        """清理旧目标与过期日程（一次性操作）。"""
        goal_manager = get_goal_manager()

        # 1. 清理过期的日程（昨天及更早的日程条目；保留近 3 天供去重参考）
        expired_schedules = goal_manager.cleanup_expired_schedules()
        if expired_schedules > 0:
            logger.info(f"清理了 {expired_schedules} 个过期日程（3 天前）")

        # 2. 清理已完成 / 已取消的旧目标（保留 cleanup_old_goals_days 天）
        cleanup_cfg = section_or_default(self._plugin, "planning_cleanup", PlanningCleanupSectionConfig)
        cleaned_count = goal_manager.cleanup_old_goals(days=cleanup_cfg.cleanup_old_goals_days)
        if cleaned_count > 0:
            logger.info(f"清理了 {cleaned_count} 个旧目标（{cleanup_cfg.cleanup_old_goals_days} 天前）")

    # ------------------------------------------------------------
    # 后台任务 2：自动调度循环（每日定时生成日程）
    # ------------------------------------------------------------

    async def run_scheduler_loop(self) -> None:
        """启动并守护 ``ScheduleAutoScheduler``（每日定时自动生成日程）。"""
        logger.info("日程自动调度循环已启动")
        try:
            # 延迟 5 秒启动，确保插件其他组件就绪
            await asyncio.sleep(5)
            self._auto_scheduler = ScheduleAutoScheduler(plugin=self._plugin, generator_factory=self._generator_factory)
            await self._auto_scheduler.start()

            # 等待停止信号
            await self._stop_event.wait()
        except asyncio.CancelledError:
            logger.debug("自动调度循环被取消")
            raise
        finally:
            # 优雅停止调度器
            if self._auto_scheduler is not None:
                try:
                    await self._auto_scheduler.stop()
                except Exception as exc:
                    logger.warning(f"停止自动调度器异常: {exc}")
                self._auto_scheduler = None
            logger.info("日程自动调度循环已退出")

    # ------------------------------------------------------------
    # 停止控制
    # ------------------------------------------------------------

    async def stop(self) -> None:
        """通知所有循环停止。"""
        self._stop_event.set()
