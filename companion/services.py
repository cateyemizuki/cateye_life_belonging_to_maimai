"""companion 服务束：一次性初始化虚拟恋人的全部服务，供 plugin.py 持有。

用法（整合者后续接入 plugin.py）：
    self._companion = CompanionServices(self)
    await self._companion.start()
    ...
    await self._companion.stop()

设计约定：
- 服务构造持 plugin 引用，运行时动态读 ``plugin.config.xxx``（配置热重载无需重建）；
- 数据目录 ``plugin._data_dir``；
- 顶层已有的实例（好感度/日志/LLM/巡检查询）优先复用，缺失时自建兜底；
- 「今日日程」一律经 ``plugin._planning`` 的 ScheduleQuery 取
  （缺省回退 ``plugin._schedule_query``，再缺省为 None → 各处降级文案）；
- ``set_persona(lover_name, personality)`` 是人设刷新入口（顶层
  config_reload_subscriptions 的 bot 变更回调里调用）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.diag import DecisionLogger, LLMCallLogger
from core.llm_utils import LLMService
from planning.schedule_query import ScheduleQuery

from .affection import AffectionManager
from .affection_events import record_rule_event
from .affection_referee import AffectionReviewer
from .cateye_client import CateyeClient
from .holiday_service import HolidayService
from .memory_manager import MemoryManager
from .message_service import MessageService
from .patrol import PatrolService
from .screen_context import ScreenContextStore
from .sender_identity import SenderCache, SessionKindCache

logger = logging.getLogger(__name__)


def _emotion_valence(plugin: Any) -> Optional[float]:
    """读取情绪模块当前生效 V（M3 B2，§6.2 B）；缺失/停用/异常一律返回 None。

    跨模块调用可空降级（§八.2）：``getattr(plugin, "_emotion", None)`` 判空 +
    只用 ``is_enabled`` / ``effective_valence`` 两个公开入口，单模块停用不影响其余。
    返回 None 时好感调制乘数恒 1.0（M1 占位语义的降级路径）。
    """
    emotion = getattr(plugin, "_emotion", None)
    if emotion is None:
        return None
    try:
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled) and not enabled():
            return None
        getter = getattr(emotion, "effective_valence", None)
        if not callable(getter):
            return None
        value = getter()
    except Exception:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class CompanionServices:
    """虚拟恋人服务束（好感度 / 巡检 / 消息 / 记忆 / 屏幕感知 / 节假日）。"""

    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin
        self.ctx = plugin.ctx
        self._data_dir = Path(getattr(plugin, "_data_dir", Path(".")))
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3+ 在 _register_event_subscriptions 里追加）
        self._event_unsubs: list = []

        cfg = plugin.config
        # ---- 复用顶层已有实例（只增不改名；缺失时自建兜底）----
        self.affection: AffectionManager = (
            getattr(plugin, "_affection", None)
            or AffectionManager(
                self._data_dir,
                default_level=int(getattr(cfg.affection, "current_level", 0)),
                config_provider=lambda: plugin.config,
                decision_logger=getattr(plugin, "_decision_logger", None),
            )
        )
        # 好感 v2 档位变化事件需要总线（plugin._bus 在服务束构造前已就绪，可空降级）
        if self.bus is not None:
            try:
                self.affection.set_event_bus(self.bus)
            except Exception:
                pass
        # M3（B2）：注入情绪效价提供者，好感入账按 §6.2 B 做情绪调制。
        # 提供者是惰性 lambda：plugin._emotion 在本服务束构造之后才装配（plugin.py 顺序），
        # 调用时才解析；情绪模块停用/缺失 → None → 乘数恒 1.0（可空降级，§八.2）。
        try:
            self.affection.set_emotion_vad_provider(lambda: _emotion_valence(plugin))
        except Exception:
            pass
        self.decision_logger: Optional[DecisionLogger] = getattr(plugin, "_decision_logger", None)
        self.llm_logger: Optional[LLMCallLogger] = getattr(plugin, "_llm_logger", None)
        self.llm: LLMService = (
            getattr(plugin, "_llm", None)
            or LLMService(self.ctx, task_name=cfg.schedule.llm_task_name, call_logger=self.llm_logger,
                          config_provider=lambda: plugin.config)
        )
        # 日程查询：消费 planning 子模块（顶层 plugin._schedule_query 即 planning.ScheduleQuery）
        self.schedule_query: Optional[ScheduleQuery] = getattr(plugin, "_schedule_query", None)

        # ---- 纯 companion 服务 ----
        #: 发送者身份缓存（v2.5.0：上下文条目里只有 msg_id 与显示名，靠反查判定恋人）
        self.senders = SenderCache()
        #: 会话 ID → 是否群聊
        self.session_kinds = SessionKindCache()
        #: 屏幕旁白「尾随锚点」注入缓存（触发时发布，请求 Hook 消费）
        cateye_cfg = getattr(cfg, "cateye", None)
        self.screen_store = ScreenContextStore(
            ttl_minutes=float(getattr(cateye_cfg, "context_ttl_minutes", 90) or 90)
        )
        #: 恋人电脑联动客户端（connect-hub 跨插件 API）
        self.cateye = CateyeClient(plugin, llm=self.llm, call_logger=self.llm_logger)
        #: 节假日服务（注入日期行用）
        self.holiday = HolidayService(self._data_dir)
        #: 本地记忆库（用户习惯）
        self.memory = MemoryManager(self.affection)
        #: 关系档案（对方是 bot 的谁：恋人/挚友/维护者/家人/自定义，随配置热更新）
        self.relation = self._resolve_relation()
        #: 消息发送服务（情绪后缀 + 直发）
        self.message = MessageService(plugin, self.affection, lover_name=self.lover_name)
        self.message.set_relation(self.relation)
        self.affection.set_level_labels(self.relation.level_labels)
        #: 巡检调度器（完整判定链）
        self.patrol = PatrolService(
            self.ctx, plugin, self.affection, self.schedule_query, self.llm,
            self.decision_logger, cateye=self.cateye, screen_store=self.screen_store,
        )
        self.patrol.set_relation(self.relation)
        #: M2 好感 LLM 裁判编排器（会话采样 + 关系日结；总线可空降级）
        self.reviews = AffectionReviewer(
            plugin, self.affection, self.llm, bus=self.bus,
            decision_logger=self.decision_logger,
            relation_getter=lambda: self.relation,
        )

    def _resolve_relation(self) -> Any:
        """从 patrol 配置段解析关系档案（relation_type / relation_custom）。"""
        from .relation import resolve_relation

        patrol_cfg = getattr(self.plugin.config, "patrol", None)
        return resolve_relation(
            getattr(patrol_cfg, "relation_type", "恋人"),
            getattr(patrol_cfg, "relation_custom", ""),
        )

    # ------------------------------------------------------------------
    # 人设与目标
    # ------------------------------------------------------------------
    @property
    def lover_name(self) -> str:
        return str(getattr(self.plugin, "_lover_name", "") or "麦麦")

    @property
    def personality(self) -> str:
        return str(getattr(self.plugin, "_personality", "") or "")

    def set_persona(self, lover_name: str, personality: str = "") -> None:
        """人设刷新入口（顶层的 bot 配置热重载回调里调用）。"""
        self.patrol.set_persona(lover_name, personality or self.personality)
        self.message.set_lover_name(lover_name)

    def set_target(self, target_qq: str, stream_id: str) -> None:
        """目标会话解析结果透传给巡检器（stream 解析成功/变化时调用）。"""
        self.patrol.set_target(target_qq, stream_id)

    def sync_from_config(self) -> None:
        """配置热重载后同步动态项（关系档案 / llm 任务名 / 好感度档位 / 旁白 TTL / 日志开关）。"""
        cfg = self.plugin.config
        try:
            self.relation = self._resolve_relation()
            self.message.set_relation(self.relation)
            self.patrol.set_relation(self.relation)
            self.affection.set_level_labels(self.relation.level_labels)
        except Exception:
            pass
        try:
            self.llm.update_task(cfg.schedule.llm_task_name)
        except Exception:
            pass
        try:
            # v2：档位由 score/迟滞驱动，current_level 只作「无档案时的初始档位」（构造时传入）；
            # 热重载时不再回写，否则每次改配置都会把累积的 score 冲回档位代表分数。
            # enabled=false（v1 回退模式）时维持旧行为：配置档位直接下发。
            if not self.affection.is_enabled():
                self.affection.update_level(
                    int(getattr(cfg.affection, "current_level", self.affection.level()))
                )
        except Exception:
            pass
        cateye_cfg = getattr(cfg, "cateye", None)
        try:
            self.screen_store.ttl_seconds = (
                max(0.0, float(getattr(cateye_cfg, "context_ttl_minutes", 90) or 0)) * 60.0
            )
        except Exception:
            pass
        admin = getattr(cfg, "admin", None)
        if admin is not None:
            if self.llm_logger is not None:
                self.llm_logger.update_settings(admin.llm_log_enabled, admin.log_retention_days)
            if self.decision_logger is not None:
                self.decision_logger.update_settings(admin.decision_log_enabled, admin.log_retention_days)

    # ------------------------------------------------------------------
    # 事件订阅挂点（M0：登记于此，M3+ 消费者按需 subscribe 并存入 _event_unsubs）
    # ------------------------------------------------------------------
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（start() 调用）。

        M1：订阅 planning 的约定兑现/违约事件 → affection_events 各记 1 次
        （``commitment_kept`` +1.0 / ``commitment_broken`` −0.5，每约定 1 次；
        频控/限幅/档位重估都在规则事件层内完成）。
        M2：追加订阅 ``schedule.sleep_started`` → 关系日结（大周期，§5.4-2）；
        日结晚于情绪睡眠大修的保证机制见 ``affection_referee.AffectionReviewer.on_sleep_started``。
        幂等：已注册时直接返回，其余消费者（M3）在此追加 ``self._event_unsubs.append(...)``。
        """
        if self.bus is None or self._event_unsubs:
            return
        try:
            self._event_unsubs.append(
                self.bus.subscribe("schedule.commitment_kept", self._on_commitment_kept,
                                   owner="companion")
            )
            self._event_unsubs.append(
                self.bus.subscribe("schedule.commitment_broken", self._on_commitment_broken,
                                   owner="companion")
            )
            reviewer = getattr(self, "reviews", None)
            if reviewer is not None:
                self._event_unsubs.append(
                    self.bus.subscribe("schedule.sleep_started", reviewer.on_sleep_started,
                                       owner="companion")
                )
        except Exception as exc:
            logger.warning("companion 事件订阅失败（忽略）：%s", exc)
            self._unregister_event_subscriptions()

    async def _on_commitment_kept(self, payload: Any) -> None:
        """``schedule.commitment_kept`` 订阅者（planning 约定兑现）。"""
        self._record_commitment_event("commitment_kept", payload)

    async def _on_commitment_broken(self, payload: Any) -> None:
        """``schedule.commitment_broken`` 订阅者（planning 约定违约）。"""
        self._record_commitment_event("commitment_broken", payload)

    def _record_commitment_event(self, kind: str, payload: Any) -> None:
        """把一条约定事件记为好感规则事件（payload 不可用时静默降级）。

        去重键取 ``commitment``（回退 ``summary``），配合规则表的 ``per_key_once``
        实现「每约定 1 次」；好感关闭（enabled=false）时规则事件层自行跳过。
        """
        data = payload if isinstance(payload, dict) else {}
        key = str(data.get("commitment") or data.get("summary") or "").strip()
        desc = str(data.get("summary") or data.get("commitment") or "").strip()
        try:
            record_rule_event(self.affection, kind, None, desc, key=key)
        except Exception as exc:
            logger.debug("好感约定事件 %s 记录失败（忽略）：%s", kind, exc)

    def _unregister_event_subscriptions(self) -> None:
        """订阅注销挂点（stop() 调用）：执行并清空全部反订阅闭包。"""
        for unsub in self._event_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubs.clear()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动服务束（幂等）：加载时清理过期日志、预热节假日缓存；
        目标 stream 已解析则启动巡检循环（未解析时由顶层重试循环解析后再调）。"""
        self._register_event_subscriptions()
        try:
            if self.llm_logger is not None:
                self.llm_logger.cleanup()
            if self.decision_logger is not None:
                self.decision_logger.cleanup()
        except Exception:
            pass
        patrol_enabled = bool(getattr(getattr(self.plugin.config, 'patrol', None), 'enabled', True))
        if patrol_enabled and self.patrol.has_target():
            self.patrol.start()
        elif not patrol_enabled:
            self.plugin.ctx.logger.info('恋人模块已停用（patrol.enabled=false），巡检循环未启动')
        # 预热当天节假日缓存，避免首次 planner 注入阻塞在 API 调用
        try:
            asyncio.ensure_future(self.holiday.get_holiday_info(datetime.now().strftime("%Y-%m-%d")))
        except Exception:
            pass

    async def stop(self) -> None:
        """停止服务束：停巡检、清身份缓存、丢弃待注入旁白、强写好感度。"""
        self._unregister_event_subscriptions()
        reviewer = getattr(self, "reviews", None)
        if reviewer is not None:
            try:
                reviewer.close()  # 取消未执行的关系日结/采样后台任务（M2）
            except Exception:
                pass
        await self.patrol.stop()
        self.senders.clear()
        self.session_kinds.clear()
        self.screen_store.discard()
        try:
            self.affection.flush()
        except Exception:
            pass
