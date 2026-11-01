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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .generator import LOW_MOOD_MIN_DAYS

#: 连续低迷事件缓存的新鲜度上限（小时）：超龄一律按未激活处理
#: （防情绪模块停用/停机后拿陈旧事件强制插入恢复类活动）
LOW_MOOD_CACHE_MAX_AGE_HOURS = 72.0

from .cleanup_service import CleanupService
from .command_service import CommandService
from .event_catalog import EventCatalogService
from .goal_manager import close_goal_manager, get_goal_manager
from .holiday_service import HolidayService
from .inject_service import InjectService
from .init_service import InitService
from .proactive_service import ProactiveService
from .schedule_generator import ScheduleGenerator
from .schedule_items import is_night_sleep_item
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

        # 日程属性表装载（2026-10-21 §24.3 任务C：mappings/ 只在加载/重载读一遍）。
        # EmotionService/CompanionServices 同款模式：热重载 = 宿主重导入重跑 on_load → 自动重读；
        # root_dir 缺失（测试桩）= 纯内置；失败只告警不拖挂（内置默认恒可用）。
        try:
            from . import activity_properties
            _props_result = activity_properties.load_and_apply(
                root_dir=getattr(plugin, "_plugin_root", None),
                decision_logger=getattr(plugin, "_decision_logger", None))
            if _props_result.rejected:
                logger.warning("日程属性表有 %d 个条目被排除（其余生效）", len(_props_result.rejected))
        except Exception as exc:
            logger.warning("日程属性表装载异常（按内置默认处理）：%s", exc)

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
        #: 节假日服务（2026-10-10 由 companion 迁入 planning：日期/节假日只随日程块注入）
        self.holiday = HolidayService(data_dir)
        self.inject = InjectService(plugin, holiday_service=self.holiday)
        # S2（冲浪计划书 §5）：注意力租约注入——SURF 有效时活动层倍率按 0.45/0.70
        # 替换解析；None（未装配）时频率链行为与既有逐字一致
        self.proactive = ProactiveService(plugin, attention=getattr(plugin, "_attention", None))
        self.cleanup = CleanupService(plugin, generator_factory=lambda: self.generator)
        # ── PR4 事件库服务（2026-10-27 共享接线）：**全插件唯一实例** ──────────────
        # 单实例是有意的：事件库的 token 预算/「每日一次自动尝试」账本是进程内锁 +
        # 原子文件，多实例并发刷新会互相覆盖；热重载/开关切换只能复用同一实例并
        # start/stop 后台（见 sync_event_catalog），绝不重建。
        self.event_catalog: Any = self._build_event_catalog()
        # 供 InitService/工具按公共属性取同一实例（plugin 侧也挂一份只读引用）
        try:
            plugin._event_catalog = self.event_catalog
        except Exception:
            pass
        # ── PR5 统一初始化服务（/lbm_init 的业务层）：单实例、无后台 task ──────────
        # 事件统一走上面的 EventCatalogService.refresh(manual=True)（不重复生成/预算）；
        # 频率校准走 PR6 公共口 self.calibrate_target_frequency。
        self.init = InitService(
            data_dir, plugin=plugin,
            config_provider=lambda: getattr(plugin, "config", None),
            event_catalog=self.event_catalog,
            frequency_calibrator=self.calibrate_target_frequency,
        )
        # R7 滚动化服务占位（R7 依据 = 用户 2026-10-25 直派【日程系统滚动化改造】）：
        # 接线位（后续阶段）在此实例化 LifeStateService / RollingPlanner /
        # LifeScheduleReadModel 并 sq.set_runtime_provider(life_schedule) + 5 分钟 tick；
        # 当前先占位 None = 未接线（组件面 getattr 判空降级、门面审计 hasattr 通过）。
        # PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理）：``[schedule].enabled`` 是
        # 唯一日程总开关（旧 [life_state] 两开关退役），服务束装配/停摆由 plugin 按
        # schedule.enabled 门控；本占位字段保留作 getattr 判空降级面。
        self.life_state: Any = None
        self.rolling_planner: Any = None
        self.life_schedule: Any = None
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

    # ------------------------------------------------------------
    # 事件库服务装配 + 自动生成闸（PR4 共享接线）
    # ------------------------------------------------------------

    def _event_catalog_section(self) -> Any:
        """``[event_catalog]`` section（热重载动态读；缺失 → None → 服务用默认 off）。"""
        return getattr(getattr(self._plugin, "config", None), "event_catalog", None)

    def _build_event_catalog(self) -> Any:
        """构造唯一 ``EventCatalogService``（失败 → None，planner 回退内置池旧路径）。"""
        plugin = self._plugin
        try:
            timezone_str = str(getattr(
                getattr(getattr(plugin, "config", None), "schedule_advanced", None),
                "timezone", "") or "") or "Asia/Shanghai"
            service = EventCatalogService(
                plugin_root=getattr(plugin, "_plugin_root", None),
                data_dir=getattr(plugin, "_data_dir", None),
                llm=getattr(plugin, "_llm", None),
                config_provider=self._event_catalog_section,
                tz=timezone_str,
                # 清醒/非睡眠/无硬约定闸：**必须**注入（服务本身 fail-closed）
                auto_gate=self.event_catalog_auto_gate,
                persona_provider=lambda: str(getattr(plugin, "_personality", "") or ""),
                recent_schedule_names_provider=self._recent_schedule_names,
                recent_event_names_provider=self._recent_event_names,
            )
            logger.info("事件库服务已装配（路径=%s）", getattr(service.store, "path", "?"))
            return service
        except Exception as exc:
            logger.warning("事件库服务装配失败（按内置池降级）：%s", exc)
            return None

    def _recent_schedule_names(self) -> List[str]:
        """近 30 天已出现过的活动名（用于生成去重；取不到 → 空列表，不影响运行）。"""
        plugin = self._plugin
        names: List[str] = []
        db = getattr(plugin, "_db", None)
        for attr in ("get_recent_activity_names", "get_recent_schedule_names"):
            fn = getattr(db, attr, None)
            if callable(fn):
                try:
                    return [str(x) for x in (fn(days=30) or []) if str(x or "").strip()]
                except Exception:
                    continue
        getter = getattr(db, "get_day_schedule", None)
        if callable(getter):
            try:
                now = self._now()
                for offset in range(0, 30):
                    day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
                    for row in (getter(day) or []):
                        if isinstance(row, dict):
                            name = str(row.get("name") or "").strip()
                            if name:
                                names.append(name)
            except Exception:
                return []
        return names

    def _recent_event_names(self) -> List[str]:
        """近 30 天生成过的事件名（episode metadata 来源；取不到 → 空列表）。"""
        db = getattr(self._plugin, "_db", None)
        for attr in ("get_recent_event_names", "get_recent_episode_event_names"):
            fn = getattr(db, attr, None)
            if callable(fn):
                try:
                    return [str(x) for x in (fn(days=30) or []) if str(x or "").strip()]
                except Exception:
                    return []
        return []

    def _now(self) -> datetime:
        """当前时间（时区真源与日程一致；异常回退系统本地时区）。"""
        try:
            from .time_utils import TimezoneManager
            tz_str = str(getattr(
                getattr(getattr(self._plugin, "config", None), "schedule_advanced", None),
                "timezone", "") or "") or "Asia/Shanghai"
            return TimezoneManager(tz_str).get_now()
        except Exception:
            return datetime.now().astimezone()

    def _runtime_asleep(self, now: Optional[datetime] = None) -> Optional[bool]:
        """引擎不表态时的**非时间窗**回退（None = 无法判断 → 调用方 fail-closed）。

        PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 C 项）：
        旧实现按 ``[schedule]`` 作息锚点时间窗推断睡着/清醒——与统一 runtime 真源
        冲突，且真实 anchors 是 dataclass 时下标取值抛错、早已退化为死分支。现只读
        真实会话/运行态：``LifeState.sleep_status``（如提供）→ ``ScheduleQuery.
        runtime_current`` 的夜间睡眠 episode；都没有 → ``None``（未知，绝不按钟点
        伪造确定清醒/入睡）。
        """
        plugin = self._plugin
        life = getattr(plugin, "_life_state", None)
        if life is not None:
            getter = getattr(life, "sleep_status", None)
            if callable(getter):
                try:
                    status = getter()
                    if isinstance(status, dict) and isinstance(status.get("asleep"), bool):
                        return status["asleep"]
                except Exception:
                    pass
        query = getattr(plugin, "_schedule_query", None)
        runtime_current = getattr(query, "runtime_current", None)
        if callable(runtime_current):
            try:
                item = runtime_current(now, include_sleep=True)
            except Exception:
                item = None
            if isinstance(item, dict) and is_night_sleep_item(item):
                return True
            if (isinstance(item, dict) and item.get("is_runtime_episode") is True
                    and item.get("status") == "active" and item.get("goal_type")):
                return False
        return None

    def _hard_commitment_now(self, now: Optional[datetime] = None) -> Optional[bool]:
        """仅当前时间窗的硬约定阻止生成；读取失败或数据未知时保守拒绝。"""
        cur = self._now() if now is None else now
        if not isinstance(cur, datetime):
            return None
        if cur.tzinfo is None:
            cur = cur.replace(tzinfo=self._now().tzinfo)
        query = getattr(self._plugin, "_schedule_query", None)
        current_getter = getattr(query, "runtime_current", None)
        if callable(current_getter):
            try:
                item = current_getter(cur, include_sleep=True)
            except Exception:
                return None
            if item is not None and not isinstance(item, dict):
                return None
            if isinstance(item, dict):
                hardness = item.get("hardness")
                for key in ("parameters", "metadata"):
                    nested = item.get(key)
                    if hardness is None and isinstance(nested, dict):
                        hardness = nested.get("hardness")
                if str(hardness or "").lower() == "hard":
                    return True
        # 未执行的未来约定不等于当前占用；时间窗以真实日程为准。
        # 同时查昨日，以覆盖 23:00–次日01:00 等跨午夜硬约定。
        getter = getattr(query, "get_day_schedule", None)
        if not callable(getter):
            return None
        midnight = cur.replace(hour=0, minute=0, second=0, microsecond=0)
        for base in (midnight - timedelta(days=1), midnight):
            try:
                rows = getter(base.strftime("%Y-%m-%d"))
            except Exception:
                return None
            if not isinstance(rows, (list, tuple)):
                return None
            for row in rows:
                if not isinstance(row, dict):
                    return None
                params = row.get("parameters") or {}
                if not isinstance(params, dict):
                    return None
                if str(params.get("hardness") or row.get("hardness") or "").lower() != "hard":
                    continue
                start, end = row.get("start_minute"), row.get("end_minute")
                if any(isinstance(v, bool) or not isinstance(v, int) for v in (start, end)):
                    return None
                if not 0 <= start <= 2880 or not 0 <= end <= 4320 or start == end:
                    return None
                if end < start:
                    end += 1440
                offset = 1440 if row.get("day_offset") == 1 and start < 1440 else 0
                if base + timedelta(minutes=start + offset) <= cur < base + timedelta(minutes=end + offset):
                    return True
        return False

    def event_catalog_auto_gate(self, now: Any = None) -> Dict[str, Any]:
        """自动事件生成的闸：清醒 / 非睡眠 / 无硬约定；**未知一律 fail-closed**。

        读取顺序（PR3 交接口径）：``plugin.sleep_asleep(now)`` 是唯一权威——
        ``True`` 在睡、``False`` 清醒、``None`` 才回退旧周期/日程判断；
        ``None`` 再判断不出 → 直接拒绝（``sleep_unknown``）。硬约定源都不可用
        → 拒绝（``hard_commitment_unknown``）。任何异常 → 拒绝（``gate_error``）。
        """
        try:
            plugin = self._plugin
            asleep: Any = None
            getter = getattr(plugin, "sleep_asleep", None)
            if callable(getter):
                asleep = getter(now)
            if asleep is None:
                asleep = self._runtime_asleep(now)
            if asleep is None:
                return {"can_generate": False, "reason": "sleep_unknown"}
            if asleep:
                return {"awake": False, "in_sleep": True, "hard_commitment": False}
            hard = self._hard_commitment_now(now)
            if hard is None:
                return {"can_generate": False, "reason": "hard_commitment_unknown"}
            return {"awake": True, "in_sleep": False, "hard_commitment": bool(hard)}
        except Exception as exc:
            logger.warning("事件库自动闸异常（本次跳过）：%s", exc)
            return {"can_generate": False, "reason": "gate_error"}

    async def sync_event_catalog(self) -> None:
        """配置热更新后的模式切换（off/manual/auto 三态；复用同一实例）。"""
        service = getattr(self, "event_catalog", None)
        if service is None:
            return
        sync = getattr(service, "sync_config", None)
        try:
            if callable(sync):
                await sync()
            else:
                await service.stop()
                await service.start()
        except Exception as exc:
            logger.warning("事件库模式切换失败（保持只读）：%s", exc)

    async def stop_event_catalog(self) -> None:
        """停事件库后台（插件停用/卸载；inflight 生成按保守结果保留旧库）。"""
        service = getattr(self, "event_catalog", None)
        if service is None:
            return
        try:
            await service.stop()
        except Exception:
            logger.warning("事件库停止失败（忽略）", exc_info=True)

    # ------------------------------------------------------------
    # 目标频率校准（PR6，定稿 §4.5）
    # ------------------------------------------------------------

    async def calibrate_target_frequency(self, *, force: bool = True) -> Dict[str, Any]:
        """公共转发口：``self.proactive.calibrate_target_frequency(force=...)``。

        供 ``InitService`` / 插件 startup / target 解析 / 配置更新统一调用，避免各处再去
        摸 ``proactive`` 私有属性。缺 proactive（未接线/测试桩）→ 返回 ok=False 的可读结果。
        """
        proactive = getattr(self, "proactive", None)
        if proactive is None or not hasattr(proactive, "calibrate_target_frequency"):
            return {"ok": False, "enabled": False, "target_resolved": False,
                    "target_stream": "", "factor": 1.0, "applied": False,
                    "skipped": "", "reason": "proactive_unavailable",
                    "errors": ["ProactiveService 未接线"]}
        return await proactive.calibrate_target_frequency(force=force)

    async def start(self) -> None:
        """启动后台循环（幂等）：清理 + 定时生成 + 注入缓存预热 + 主动行为。"""
        if self._started:
            return
        self._started = True
        self._register_event_subscriptions()
        # 事件库（PR4）：独立于 schedule.enabled —— 事件库可单独 manual 准备；mode=off
        # 时服务自身不读生成库、不起后台。失败只记日志，不影响其它后台循环。
        try:
            service = getattr(self, "event_catalog", None)
            if service is not None:
                await service.start()
        except Exception as exc:
            logger.warning("事件库启动失败（保持只读）：%s", exc)
        if not bool(getattr(self._plugin.config.schedule, "enabled", True)):
            logger.info("日程规划模块已停用（schedule.enabled=false），后台循环未启动")
            return
        # PR6（定稿 §4.5）：启动即校准目标频率，**不等**第一次活动切换；目标未解析/
        # 开关关闭/SDK 失败都只记日志，绝不拖挂启动（校准口内部已有可读返回）。
        try:
            calibration = await self.calibrate_target_frequency(force=True)
            if calibration.get("errors"):
                logger.info("目标频率校准未生效（%s）：%s", calibration.get("reason"),
                            "；".join(calibration["errors"]))
        except Exception as exc:
            logger.warning("目标频率校准异常（忽略，不阻塞启动）：%s", exc)
        self._bg_tasks = [
            asyncio.create_task(self.cleanup.run_cleanup_loop()),
            asyncio.create_task(self.cleanup.run_scheduler_loop()),
            asyncio.create_task(self.inject.preheat_cache()),
            asyncio.create_task(self.proactive.run_loop()),
            asyncio.create_task(self._preheat_holiday()),
        ]
        logger.info("PlanningServices 后台任务已启动（%d 个循环）", len(self._bg_tasks))

    async def _preheat_holiday(self) -> None:
        """预热当天节假日缓存（避免首次注入阻塞在联网查询；失败静默）。"""
        try:
            await self.holiday.get_holiday_info(datetime.now().strftime("%Y-%m-%d"))
        except Exception as exc:
            logger.debug("节假日缓存预热失败（忽略）：%s", exc)

    async def stop(self) -> None:
        """停止并取消全部后台任务，释放 GoalManager 单例。"""
        self._unregister_event_subscriptions()
        try:
            await self.stop_event_catalog()
        except Exception:
            pass
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

    @property
    def sleep_engine(self) -> Any:
        """睡眠引擎只读句柄（实际装配在 plugin._sleep_engine；PR6 诊断兼容口）。"""
        return getattr(self._plugin, "_sleep_engine", None)
