"""PlanningServices —— planning 模块的服务束（组合根）。

plugin.py 集成方式（整合者执行，见 INTEGRATION.md）::

    from planning.services import PlanningServices
    # on_load 末尾（plugin.enabled 且 self._data_dir 就绪后）：
    self._planning = PlanningServices(self)
    await self._planning.start()
    # on_unload：
    await self._planning.stop()

服务实例动态读 plugin.config.xxx，配置热重载自动生效；
数据目录用 plugin._data_dir（= ctx.paths.data_dir）。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, List, Optional

from .cleanup_service import CleanupService
from .command_service import CommandService
from .goal_manager import close_goal_manager, get_goal_manager
from .inject_service import InjectService
from .proactive_service import ProactiveService
from .schedule_generator import ScheduleGenerator
from .tools_service import ToolsService

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


class PlanningServices:
    """日程规划服务束：初始化 / 启动 / 停止全部 planning 后台服务与组件依赖。"""

    def __init__(self, plugin: Any) -> None:
        """
        Args:
            plugin: 插件实例（须已完成 on_load：self._data_dir / self._db /
                self._llm / self._schedule_query 可用）
        """
        self._plugin = plugin
        self._started = False
        self._bg_tasks: List[asyncio.Task] = []

        data_dir = getattr(plugin, "_data_dir", None)
        if data_dir is None:
            raise RuntimeError("PlanningServices 需要 plugin._data_dir 已就绪（在 on_load 中创建）")

        # GoalManager 单例（复用 plugin._db 的 SQLite 连接，避免双连接写竞争）
        self.goal_manager = get_goal_manager(str(data_dir), db=plugin._db)

        # 完整版日程生成器（多轮生成 + 约定匹配 + 上下文增强）。
        # 重绑 plugin._schedule_gen：plugin.py 既有调用点（_ensure_today_schedule /
        # /life plan regenerate）自动升级为完整管线。
        self.generator = ScheduleGenerator(
            plugin._db, plugin._llm,
            plugin=plugin, goal_manager=self.goal_manager,
        )
        try:
            plugin._schedule_gen = self.generator
        except Exception as exc:
            logger.warning("重绑 plugin._schedule_gen 失败（骨架生成器继续可用）: %s", exc)

        # 组件依赖的服务
        self.tools = ToolsService(plugin, generator=self.generator)
        self.commands = CommandService(plugin, tools_service=self.tools)
        self.inject = InjectService(plugin)
        self.proactive = ProactiveService(plugin)
        self.cleanup = CleanupService(plugin, generator_factory=lambda: self.generator)
        logger.info("PlanningServices 初始化完成（完整管线已接管日程生成）")

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------

    async def start(self) -> None:
        """启动后台循环（幂等）：清理 + 定时生成 + 注入缓存预热 + 主动行为。"""
        if self._started:
            return
        self._started = True
        if not bool(getattr(self._plugin.config.schedule, "enabled", True)):
            logger.info("日程规划模块已停用（schedule.enabled=false），后台循环未启动")
            return
        self._bg_tasks = [
            asyncio.create_task(self.cleanup.run_cleanup_loop()),
            asyncio.create_task(self.cleanup.run_scheduler_loop()),
            asyncio.create_task(self.inject.preheat_cache()),
            asyncio.create_task(self.proactive.run_loop()),
        ]
        logger.info("PlanningServices 后台任务已启动（%d 个循环）", len(self._bg_tasks))

    async def stop(self) -> None:
        """停止并取消全部后台任务，释放 GoalManager 单例。"""
        try:
            await self.proactive.stop()
        except Exception:
            pass
        try:
            await self.cleanup.stop()
        except Exception:
            pass
        for task in self._bg_tasks:
            if not task.done():
                task.cancel()
        for task in self._bg_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_tasks.clear()
        self._started = False
        close_goal_manager()
        logger.info("PlanningServices 已停止")

    # ------------------------------------------------------------
    # 组件转发入口（components.py 的 Mixin 方法委托到这里）
    # ------------------------------------------------------------

    async def get_current_activity_snapshot(self, chat_id: str = "global") -> dict:
        """当前活动快照（@API get_current_activity 的行为主体）。"""
        return await self.inject.get_current_activity_snapshot(chat_id or "global")

    async def regenerate_today_schedule_now(self, *, extra_prompt: str = "") -> list:
        """立即重生成今日日程（/plan regenerate 的行为主体）。"""
        return await self.tools.regenerate_today_schedule_now(extra_prompt=extra_prompt)

    def status_text(self) -> str:
        """规划子系统一行状态（供 /life status 等聚合展示；可按需扩展）。"""
        gm = self.goal_manager
        try:
            today_count = len(gm.get_schedule_goals(chat_id="global"))
        except Exception:
            today_count = 0
        return f"日程规划：今日 {today_count} 项活动｜注入={'开' if self._plugin.config.schedule.inject_into_planner else '关'}"
