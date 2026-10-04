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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .generator import LOW_MOOD_MIN_DAYS

#: 连续低迷事件缓存的新鲜度上限（小时）：超龄一律按未激活处理
#: （防情绪模块停用/停机后拿陈旧事件强制插入恢复类活动）
LOW_MOOD_CACHE_MAX_AGE_HOURS = 72.0

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
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3+ 在 _register_event_subscriptions 里追加）
        self._event_unsubs: List[Any] = []
        #: M5（§6.2 F）：最近一次睡醒情绪（emotion.overhauled payload 缓存），供日程生成
        #: 的 emotion_context 槽读取；运行期状态、不落盘（无事件时生成侧退化为实时 mood_text）
        self._wake_emotion: Dict[str, Any] = {}
        #: M5（§6.2 F）：连续低迷事件缓存（emotion.low_mood_day），供日程生成强制插入恢复类活动
        self._low_mood_day: Dict[str, Any] = {}

        data_dir = getattr(plugin, "_data_dir", None)
        if data_dir is None:
            raise RuntimeError("PlanningServices 需要 plugin._data_dir 已就绪（在 on_load 中创建）")

        # GoalManager 单例（复用 plugin._db 的 SQLite 连接，避免双连接写竞争）
        self.goal_manager = get_goal_manager(str(data_dir), db=plugin._db)
        # 约定消费事件（commitment_kept/broken）经总线上报（M0；无总线时静默跳过）
        self.goal_manager.bind_event_bus(self.bus)

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
    # 事件订阅挂点（M0：登记于此，M3+ 消费者按需 subscribe 并存入 _event_unsubs）
    # ------------------------------------------------------------
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（start() 调用）。

        M0 唯一的实际订阅（proactive.used → 预算计数器）由 core/event_bus 在装配时安装；
        M5 本束新增 ``emotion.overhauled`` / ``emotion.low_mood_day`` 订阅：缓存睡醒情绪
        （低分辨率 VAD + levels）与连续低迷状态，供日程生成的 ``emotion_context`` 槽与
        连续低迷软约束（§6.2 F）。"""
        if self.bus is None:
            return
        for event, handler in (("emotion.overhauled", self._on_emotion_overhauled),
                               ("emotion.low_mood_day", self._on_low_mood_day)):
            try:
                self._event_unsubs.append(self.bus.subscribe(event, handler, owner="planning"))
            except Exception as exc:
                logger.warning("planning 订阅 %s 失败（忽略）：%s", event, exc)

    async def _on_emotion_overhauled(self, payload: Any) -> None:
        """``emotion.overhauled`` 处理器：只缓存低分辨率情绪，不做任何状态设置（§6.3-1）。"""
        data = payload if isinstance(payload, dict) else {}
        levels = data.get("levels") if isinstance(data.get("levels"), dict) else {}
        self._wake_emotion = {
            "mood": str(data.get("mood") or "")[:300],
            "levels": {str(k): str(v) for k, v in levels.items()},
            "recovery_factor": data.get("recovery_factor", 0.0),
            "sleep_hours": data.get("sleep_hours", 0.0),
            "ts": str(data.get("ts") or ""),
        }

    async def _on_low_mood_day(self, payload: Any) -> None:
        """``emotion.low_mood_day`` 处理器：只缓存连续低迷状态，不做任何状态设置（§6.3-1）。"""
        data = payload if isinstance(payload, dict) else {}
        try:
            days = int(data.get("days") or 0)
        except (TypeError, ValueError):
            days = 0
        self._low_mood_day = {"days": days, "date": str(data.get("date") or ""),
                              "mean_valence": data.get("mean_valence"),
                              "ts": str(data.get("ts") or "")}

    def wake_emotion(self) -> Dict[str, Any]:
        """最近一次睡醒情绪缓存（无 → 空 dict；生成侧据此拼 emotion_context）。"""
        return dict(self._wake_emotion or {})

    def low_mood_state(self) -> Dict[str, Any]:
        """连续低迷事件缓存（未满门槛 / 事件超龄 → 空 dict；生成侧据此强制插入恢复类活动）。

        新鲜度：事件 ``ts`` 超过 :data:`LOW_MOOD_CACHE_MAX_AGE_HOURS` 视为陈旧
        （情绪模块停用/停机后不得拿旧事件强制插入），生成侧另有实时 ``low_mood_state()`` 优先。
        """
        state = dict(self._low_mood_day or {})
        if int(state.get("days") or 0) < LOW_MOOD_MIN_DAYS:
            return {}
        ts = str(state.get("ts") or "")
        if ts:
            try:
                age_hours = (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 3600.0
            except (TypeError, ValueError):
                age_hours = 0.0
            if age_hours > LOW_MOOD_CACHE_MAX_AGE_HOURS:
                return {}
        return state

    def _unregister_event_subscriptions(self) -> None:
        """订阅注销挂点（stop() 调用）：执行并清空全部反订阅闭包。"""
        for unsub in self._event_unsubs:
            try:
                unsub()
            except Exception:
                logger.warning("planning 反订阅失败", exc_info=True)
        self._event_unsubs.clear()

    # ------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------

    async def start(self) -> None:
        """启动后台循环（幂等）：清理 + 定时生成 + 注入缓存预热 + 主动行为。"""
        if self._started:
            return
        self._started = True
        self._register_event_subscriptions()
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
        self._unregister_event_subscriptions()
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
