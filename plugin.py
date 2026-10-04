"""猫眼生活套件（cateye_test）— 整合插件组合根。

三个业务子模块（各自拥有 config / services / components）：
- planning/   自主规划复刻：日程生成（多轮+评分）/ SQLite 目标库 / planner+replyer 注入 /
  活动切换主动开口 / /plan 命令 / get_current_activity API
- companion/  虚拟恋人复刻：好感度 / 巡检主动聊天（早安晚安想念）/ 屏幕感知 /
  恋人电脑联动 / 节假日 / /mai_* 命令
- qzone/      QQ 空间：按日程发说说（双通道）/ 自动回评（Web CGI 直连，含楼中楼）/ /life 命令
- emotion/    情绪维护系统：VAD 三维状态机（日常增量+指数衰减）/ 睡眠裁判大修 / 人设初始化 / 情绪底色注入

本文件只做生命周期与装配：创建服务束、挂到 self._planning / self._companion /
self._qzone，组件注册由三个 components.py 的 Mixin 提供（多继承收集）。
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Iterable

# ── 宿主运行时导入引导（勿删）──────────────────────────────────────────
# 宿主 Runner 用 spec_from_file_location 加载本文件，只把「plugins 根目录」和宿主
# 「src」临时放进 sys.path，从不加入本插件目录（src/plugin_runtime/runner/
# plugin_loader.py:567-585）；本插件又用绝对导入引用自身模块，故必须自行把插件
# 目录放到 sys.path 首位，否则 companion/core/planning 等会被宿主 src/ 下同名模块
# 抢先命中（core/config 在宿主 src 下均存在）。配置模块命名为 suite_config 而非
# config：本插件与 cateye_harness 同进程加载，顶层 'config' 会被宿主 src/config
# 或先加载的兄弟插件占住 sys.modules。
import os as _os
import sys as _sys

_PLUGIN_DIR = _os.path.dirname(_os.path.abspath(__file__))
if _PLUGIN_DIR in _sys.path:
    _sys.path.remove(_PLUGIN_DIR)
_sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk import MaiBotPlugin

from companion.affection import AffectionManager
from companion.components import CompanionComponentsMixin
from companion.services import CompanionServices
from suite_config import SUPPORTED_CONFIG_VERSION, CateyeSuiteConfig
from core.diag import DecisionLogger, LLMCallLogger
from core.event_bus import EventBus, install_proactive_budget
from core.llm_utils import LLMService
from emotion.components import EmotionComponentsMixin
from emotion.services import EmotionServices
from planning.components import PlanningComponentsMixin
from planning.schedule_db import ScheduleDB
from planning.schedule_query import ScheduleQuery
from planning.services import PlanningServices
from qzone.cgi import QzoneCookieManager
from qzone.components import QzoneComponentsMixin
from qzone.service import QzoneService


class CateyeSuitePlugin(PlanningComponentsMixin, CompanionComponentsMixin, QzoneComponentsMixin,
                        EmotionComponentsMixin, MaiBotPlugin):
    config_model: ClassVar[type] = CateyeSuiteConfig
    # 订阅宿主 bot 配置热重载（人设/昵称变化自动同步）
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._data_dir = data_dir

        cfg = self.config
        # 公共基础设施（三个服务束共享的实例，缺省时它们会自建兜底）
        self._decision_logger = DecisionLogger(data_dir, enabled=cfg.admin.decision_log_enabled,
                                               retention_days=cfg.admin.log_retention_days)
        self._llm_logger = LLMCallLogger(data_dir, enabled=cfg.admin.llm_log_enabled,
                                         retention_days=cfg.admin.log_retention_days)
        self._db = ScheduleDB(data_dir / "schedule.db")
        # 好感度 v2：运行期动态读 [affection] 段（热重载即时生效）+ 决策日志留痕
        self._affection = AffectionManager(
            data_dir, default_level=cfg.affection.current_level,
            config_provider=lambda: self.config, decision_logger=self._decision_logger,
        )
        # config_provider：judge() 等在调用时动态读顶层配置（[judge] 段热更新即时生效）
        self._llm = LLMService(self.ctx, task_name=cfg.schedule.llm_task_name,
                               call_logger=self._llm_logger, config_provider=lambda: self.config)
        self._schedule_query = ScheduleQuery(self._db, cache_minutes=cfg.schedule.inject_cache_minutes)

        # 进程内事件总线（M0，MAINTENANCE_PLAN §三）：先于服务束构造（各服务束自读 plugin._bus），
        # 预算计数器在装配时装入（先于服务束启动；M0 实际订阅者仅它）
        self._bus = EventBus(self._decision_logger)
        # A2：预算计数落盘 data_dir（proactive_budget.json），跨 file_watcher 热重载连续
        self._proactive_budget = install_proactive_budget(
            self._bus, config_provider=lambda: self.config, decision_logger=self._decision_logger,
            data_dir=data_dir)

        # 服务束（内部动态读 self.config，热重载自动生效）
        self._planning = PlanningServices(self)
        self._companion = CompanionServices(self)
        self._qzone_cookies = QzoneCookieManager(
            self.ctx, data_dir,
            napcat_http_host=cfg.qzone.napcat_http_host,
            napcat_http_port=cfg.qzone.napcat_http_port,
            napcat_http_token=cfg.qzone.napcat_http_token,
        )
        # 第二实参为配置实例（服务读 .qzone 段）；另显式传 plugin：bus/emotion 动态属性与
        # 热重载配置（plugin.config 每次重取，SDK 热重载会整体替换配置实例）都挂在插件对象上
        self._qzone = QzoneService(self.ctx, self.config, self._schedule_query, self._llm,
                                   self._decision_logger, plugin=self,
                                   cookie_manager=self._qzone_cookies)
        self._qzone.bind_state_dir(data_dir)
        self._emotion = EmotionServices(self)

        # 宿主人设（bot 配置）
        await self._refresh_persona()

        self._stream_id = ""
        self._bg_started = False
        self._bg_tasks: list[asyncio.Task] = []
        if not cfg.plugin.enabled:
            self.ctx.logger.info("插件未启用（plugin.enabled=false），仅加载组件")
            return

        # 目标私聊 stream 解析（失败则后台重试）
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(str(cfg.target.target_qq), self._stream_id)
        await self._start_bg()
        self.ctx.logger.info("猫眼生活套件已加载 v%s（目标=%s，stream=%s）",
                             SUPPORTED_CONFIG_VERSION, cfg.target.target_qq, self._stream_id or "待解析")

    async def on_unload(self) -> None:
        await self._qzone.stop()
        try:
            await self._emotion.stop()
        except Exception:
            pass
        try:
            await self._companion.stop()
        except Exception:
            pass
        try:
            await self._planning.stop()
        except Exception:
            pass
        if getattr(self, "_stop_event", None) is not None:
            self._stop_event.set()
        for task in getattr(self, "_bg_tasks", []):
            task.cancel()
        for task in getattr(self, "_bg_tasks", []):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_started = False
        self._affection.flush()
        self._db.close()
        self.ctx.logger.info("猫眼生活套件已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        if scope == "bot":
            await self._refresh_persona()
            return
        if scope != "self":
            return
        cfg = self.config
        self._llm.update_task(cfg.schedule.llm_task_name)
        self._companion.sync_from_config()
        if not cfg.plugin.enabled:
            await self._qzone.stop()
            await self._companion.stop()
            await self._planning.stop()
            self._bg_started = False
            return
        # 目标 QQ 变化 → 重新解析 stream
        self._stream_id = await self._resolve_target_stream()
        self._apply_target(str(cfg.target.target_qq), self._stream_id)
        await self._start_bg()
        if self._stream_id:
            self._companion.patrol.start()
        if not cfg.qzone.enabled:
            await self._qzone.stop()

    # ------------------------------------------------------------------
    # 装配助手
    # ------------------------------------------------------------------
    def _apply_target(self, target_qq: str, stream_id: str) -> None:
        self._companion.set_target(target_qq, stream_id)
        self._qzone.set_target(stream_id)

    async def _start_bg(self) -> None:
        """启动后台任务（on_load 与「停用→启用」配置切换共用；幂等）。"""
        if self._bg_started:
            return
        self._stop_event = asyncio.Event()
        await self._planning.start()
        await self._companion.start()
        if self.config.qzone.enabled:
            self._qzone.start()
        await self._emotion.start()
        self._bg_tasks.append(asyncio.create_task(self._stream_retry_loop()))
        self._bg_started = True

    async def _refresh_persona(self) -> None:
        try:
            self._personality = str(await self.ctx.config.get("personality.personality", "") or "")
            nickname = str(await self.ctx.config.get("bot.nickname", "") or "")
        except Exception:
            self._personality, nickname = "", ""
        self._lover_name = nickname or "麦麦"
        self._companion.set_persona(self._lover_name, self._personality)
        self._qzone.set_persona(self._lover_name, self._personality)
        self._emotion.set_persona(self._lover_name, self._personality)

    # ------------------------------------------------------------------
    # stream 解析（companion 巡检与 qzone 提醒共用的目标会话）
    # ------------------------------------------------------------------
    async def _resolve_target_stream(self) -> str:
        """解析目标 QQ 的私聊 stream_id；失败返回空串（后台重试循环接管）。"""
        target_qq = str(self.config.target.target_qq)
        if not target_qq or target_qq == "123456789":
            return ""  # 占位号不解析
        try:
            info = await self.ctx.chat.get_stream_by_user_id(user_id=target_qq)
            payload = info.get("stream") if isinstance(info, dict) else None
            payload = payload or info
            if isinstance(payload, dict) and payload.get("stream_id"):
                return str(payload["stream_id"])
        except Exception:
            pass
        try:  # 回退：遍历私聊流
            streams = await self.ctx.chat.get_private_streams()
            if isinstance(streams, dict):
                streams = streams.get("streams") or streams.get("data") or []
            for s in streams or []:
                if isinstance(s, dict) and str(s.get("user_id") or "") == target_qq and s.get("stream_id"):
                    return str(s["stream_id"])
        except Exception:
            pass
        return ""

    async def _stream_retry_loop(self) -> None:
        """stream 未解析时每 60 秒重试；解析到则启动巡检循环。"""
        while not self._stop_event.is_set():
            if not self.config.plugin.enabled or self._companion.patrol.has_target():
                return
            stream_id = await self._resolve_target_stream()
            if stream_id:
                self._stream_id = stream_id
                self._apply_target(str(self.config.target.target_qq), stream_id)
                self._companion.patrol.start()
                self.ctx.logger.info("目标 stream 解析成功：%s", stream_id)
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                continue


def create_plugin() -> MaiBotPlugin:
    """Runner 加载入口。"""
    return CateyeSuitePlugin()
