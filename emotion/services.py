"""EmotionServices —— 情绪模块的服务束（组合根）。

plugin.py 集成方式（与其他模块同构）::

    self._emotion = EmotionServices(self)
    await self._emotion.start()
    ...
    await self._emotion.stop()
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from .service import EmotionService
from .vad import VADState

logger = logging.getLogger(__name__)


class EmotionServices:
    """情绪模块服务束：状态机 + 睡眠检测循环 + 评估任务管理。"""

    def __init__(self, plugin: Any,
                 sleep_gate_provider: Optional[Callable[[], Any]] = None,
                 sleep_anchors_provider: Optional[Callable[[], Any]] = None) -> None:
        self._plugin = plugin
        # D1（2026-11-01 模块解耦计划）：睡眠引擎门面提供者透传给 EmotionService
        # （core.contracts.SleepEngineGate）；None = 服务内回退旧 plugin 属性路径。
        # D3：真实作息锚点查询面提供者同样透传（core.contracts.SleepAnchorsSource）；
        # None = 服务内回退组合根公共口鸭子调用（不再 import companion）。
        self.service = EmotionService(plugin, sleep_gate_provider=sleep_gate_provider,
                                      sleep_anchors_provider=sleep_anchors_provider)
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3 起由 emotion.bridge 追加）
        self._event_unsubs: list = []
        #: PR3（2026-10-26，定稿 §3）：life 反馈订阅独立成组——``emotion_modulation_enabled``
        #: 热更新由关到开时只补这一组，不能因 bridge 已订阅就整体早退（2026-10-27 用户补单）。
        self._feedback_unsubs: list = []
        #: R7 = PR2 双向闭环（2026-10-26）：LifeState/Episode → VAD 反馈服务
        #: （PR8 3.0.0：``[schedule].enabled`` 唯一总开关关闭，或
        #: ``emotion_modulation_enabled`` 关闭 → 不实例化，恒 None；旧
        #: ``[life_state].enabled`` 已退役，不得再读）
        self._life_feedback: Any = None

    # 事件订阅挂点（start() 调用；stop() 注销）
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（M3：emotion.bridge 订阅 relation.*/schedule.commitment_*/qzone.*）。

        桥内处理器绝不做绝对状态设置，只经 ``record_external_event`` 入账（§6.3-1）；
        总线缺失时静默降级（§八.2）；幂等：已注册时直接返回。
        """
        if self.bus is None:
            return
        if not self._event_unsubs:
            try:
                from .bridge import register_bridge

                self._event_unsubs.extend(register_bridge(self.bus, self.service, self._plugin))
            except Exception as exc:
                logger.warning("emotion 事件订阅失败（忽略）：%s", exc)
                self._unregister_event_subscriptions()
                return
        # R7 = PR2 双向闭环（2026-10-26），计划书 §三十五/定稿 §4.1.2：
        # LifeState/Episode → VAD 反馈侧；PR3（2026-10-26，定稿 §3）拆成独立组，
        # 支撑 ``emotion_modulation_enabled`` 热更新由关到开（见 sync_from_config）。
        self._sync_life_feedback()

    def _sync_life_feedback(self) -> None:
        """按当前配置同步 life 反馈订阅（幂等；开→建，关→注销，不残留回写订阅）。"""
        if self.bus is None:
            return
        try:
            cfg = getattr(self._plugin, "config", None)
            schedule = getattr(cfg, "schedule", None)
            section = getattr(cfg, "life_state", None)
            # PR8 3.0.0：日程总开关唯一真源 = [schedule].enabled；emotion_modulation_enabled
            # 是独立功能设置（不因它缺失而放宽日程开关，反之亦然）。
            desired = bool(
                schedule is not None and bool(getattr(schedule, "enabled", False))
                and self.is_enabled())
        except Exception:
            desired = False
        feedback = getattr(self, "_life_feedback", None)
        if not hasattr(self, "_feedback_unsubs"):
            self._feedback_unsubs = []
        if desired and feedback is None:
            try:
                from .life_feedback import LifeFeedbackService

                self._life_feedback = LifeFeedbackService(self._plugin, self.service)
                self._feedback_unsubs.extend(self._life_feedback.subscribe())
            except Exception as exc:
                logger.warning("情绪-生活反馈订阅失败（忽略）：%s", exc)
                self._life_feedback = None
        elif not desired and feedback is not None:
            for unsub in self._feedback_unsubs:
                try:
                    unsub()
                except Exception:
                    pass
            self._feedback_unsubs.clear()
            self._life_feedback = None

    def sync_from_config(self) -> None:
        """配置热更新入口（plugin.on_config_update 调用）：重新评估反馈订阅开关。

        不能依赖「服务束重建」或 file_watcher 另起进程——同一实例下由关到开必须真正
        补上 episode→VAD 订阅；由开到关必须注销，关闭时不保留回写订阅。
        """
        self._sync_life_feedback()

    def _unregister_event_subscriptions(self) -> None:
        """订阅注销挂点（stop() 调用）：执行并清空全部反订阅闭包（bridge + life 反馈）。"""
        for unsub in list(self._event_unsubs) + list(getattr(self, "_feedback_unsubs", [])):
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubs.clear()
        if hasattr(self, "_feedback_unsubs"):
            self._feedback_unsubs.clear()
        # R7 = PR2 双向闭环（2026-10-26）：反馈服务的订阅闭包已在上面一并执行；
        # 此处只解除引用（预算文件逐次落盘，无残留后台任务）
        self._life_feedback = None

    # 转发入口（plugin.py / 其他模块调用）
    def set_persona(self, lover_name: str, personality: str) -> None:
        self.service.set_persona(lover_name, personality)

    def mood_text(self) -> str:
        return self.service.mood_text()

    def emotion_description_body(self) -> str:
        """F5 情绪描述正文（面板展示用；转发 self.service）。"""
        return self.service.emotion_description_body()

    def evaluate_mode(self) -> str:
        """回复后评估模式（episode/per_reply/off；面板展示用）。"""
        return self.service._evaluate_mode()

    # ── Command / Tool 面转发（components.py 组件入口；签名与内层一致）──
    def params_summary(self) -> str:
        """``/lbm_emotion_config`` 查看文本（转发 self.service）。"""
        return self.service.params_summary()

    def reset_params(self) -> str:
        """``/lbm_emotion_config reset``：恢复默认参数（转发 self.service）。"""
        return self.service.reset_params()

    def clear_log(self) -> str:
        """``/lbm_emotion_config clear``：清空当天情绪日志（转发 self.service）。"""
        return self.service.clear_log()

    async def initialize_from_persona(self) -> str:
        """人设 → LLM 推导情绪参数 → 落盘（转发 self.service；PR8 3.0.0 后为
        init_service.run_emotion 兜底链的生产路径，C-18 保留）。"""
        return await self.service.initialize_from_persona()

    def target_multiplier(self, is_target: bool) -> float:
        """§6.2 A 的 K（target 加成 / 非 target 系数；Tool 上报事件用，转发 self.service）。"""
        return self.service.target_multiplier(is_target)

    def target_bonus_step(self) -> float:
        """target 每档加成步长（转发 self.service；D7：跨模块留痕字段取值口）。

        内层已含「JSON 合法覆盖 > config > 默认」三层合并与 [0, 1] 夹取，热重载即时生效；
        调用方（companion/patrol.py 的沉默事件 target_bonus 留痕）据此与桥内入账同源。
        """
        return self.service.target_bonus_step()

    async def record_external_event(self, source: str, description: str,
                                    delta: Optional[VADState] = None, *,
                                    use_llm: bool = False,
                                    multiplier: float = 1.0,
                                    session_id: str = "",
                                    event_id: str = "",
                                    episode_id: str = "") -> Dict[str, Any]:
        """外部事件统一入账入口（Tool 上报路径；签名与内层一致，转发 self.service）。

        ``session_id``（宿主 ToolInvocation 的 stream_id / 聊天流）供 §2.4.6 A3
        跨来源去重使用；缺省空串归入同一"未知流"。
        ``event_id`` / ``episode_id``（R7 = PR2 双向闭环（2026-10-26），计划书 §三十五/
        定稿 §4.1.2）：life.episode_* 反馈侧留痕参数，只进 decision_logs 与返回值。
        """
        return await self.service.record_external_event(
            source, description, delta, use_llm=use_llm, multiplier=multiplier,
            session_id=session_id, event_id=event_id, episode_id=episode_id)

    def injection_text(self, channel: str = "replyer") -> str:
        """情绪底色注入文本（planner/replyer 注入 Hook 入口；转发 self.service）。

        F5（2026-10-11）：``channel`` 决定情绪描述的人称（planner 第三人称 / replyer 第二人称）。
        与 ``mood_text()`` 同源但多带注入要求行；Hook 侧仅消费字符串。
        """
        return self.service.injection_text(channel)

    def mood_effect(self) -> Dict[str, Any]:
        """情绪→行为结构化系数（M4；跨模块消费入口，附录 C）。

        与 ``mood_text()`` 同为转发入口：patrol/qzone 经 ``plugin._emotion.mood_effect()``
        读取；模块停用/异常时返回恒等中性值（可空降级，§八.2）。
        """
        try:
            return self.service.mood_effect()
        except Exception:
            return {"proactive_rate": 1.0, "miss_threshold": 1.0, "post_rate": 1.0, "intent_hint": ""}

    def low_mood_days(self) -> int:
        """连续低迷天数（转发 self.service；观测用）。"""
        try:
            return int(self.service.low_mood_days())
        except Exception:
            return 0

    def low_mood_state(self) -> Dict[str, Any]:
        """连续低迷达标状态（转发；M5 §6.2 F，planning 生成日程时强制插入恢复类活动）。

        模块停用/异常 → ``{}``（可空降级，§八.2）。
        """
        if not self.is_enabled():
            return {}
        try:
            return dict(self.service.low_mood_state() or {})
        except Exception:
            return {}

    def effective_valence(self) -> Optional[float]:
        """当前生效 V（供跨模块 B2 调制读取，§6.2 B）。

        模块停用（emotion.enabled=false / plugin.enabled=false）或读取异常时返回 None，
        消费方据此把调制乘数定为 1.0——单模块停用不影响其余模块（§八.2）。
        """
        if not self.is_enabled():
            return None
        try:
            return float(self.service.effective_valence())
        except Exception:
            return None

    def effective_vad_snapshot(self) -> Optional[Dict[str, Any]]:
        """当前生效 VAD 完整快照（PR2.5（2026-10-26），交接 §五.18 D5，定稿 §4.1.1）。

        返回 ``{"valence": float, "arousal": float, "dominance": float}``（内层
        ``service._machine.effective_vad().to_dict()``，含静态偏移/乘数结算后的最终
        值；VADState.to_dict 已含键名与 4 位舍入）。模块停用（emotion.enabled=false /
        plugin.enabled=false）或读取异常 → ``None``（可空降级，§八.2）——消费方
        （plugin._emotion_vad_snapshot 鸭子链公共口优先）本拍按不调制处理。
        本口即 full-VAD 的正式束级转发，替换原暂借的私有
        ``service._machine.effective_vad()`` 读数位；plugin 侧零改动即自动命中。
        """
        if not self.is_enabled():
            return None
        try:
            return dict(self.service._machine.effective_vad().to_dict())
        except Exception:
            return None

    def anchor_snapshot(self) -> Optional[Dict[str, Any]]:
        """人设 VAD 锚点公开只读端口（R2 结构审计批；转发 service.anchor_snapshot）。

        与 effective_vad_snapshot 不同，本端口**不检查 is_enabled**——锚点是人设
        静态配置，分享质量门槛在模块停用态仍需读取（旧组合根直读
        ``service._machine.params.anchor`` 的口径逐字保留）；异常 → None。
        """
        try:
            return self.service.anchor_snapshot()
        except Exception:
            return None

    def is_enabled(self) -> bool:
        return self.service._enabled()

    async def evaluate_reply(self, reply_text: str, session_id: str = "") -> None:
        """回复后 ΔVAD 评估入口（replyer.after_response Hook 的后台任务；转发 self.service）。

        与内层 ``EmotionService.evaluate_reply`` 同签名（含 session_id target 识别链路），
        节流与静默失败语义全部由内层负责。
        """
        await self.service.evaluate_reply(reply_text, session_id=session_id)

    # ── T7 会话片段制转发（组件层只问这两个入口，不自己读模型字段集）──
    def _evaluate_mode(self) -> str:
        """当前回复后评估模式（``episode`` / ``per_reply`` / ``off``；转发 self.service）。"""
        return self.service._evaluate_mode()

    def note_reply_for_episode(self, session_id: str) -> None:
        """episode 模式的 Hook 记录入口（纯内存、零 LLM；转发 self.service）。"""
        self.service.note_reply_for_episode(session_id)

    # ── M8 段 2：L1 夜间打扰记账转发（组件层只问这一个入口，门控在内层）──
    def note_night_reply(self, session_id: str) -> bool:
        """夜间打扰记账入口（replyer.after_response Hook；转发 self.service）。

        与 ``_evaluate_mode`` 无关（off 态也记账，附录 §〇 C1）；是否计入由内层按
        ``[schedule].no_sleep_mode`` / 睡眠周期 / target 私聊门控判定（§2.1/§2.2），
        返回 True 表示本次 reply 已计入打扰计数。
        """
        return bool(self.service.note_night_reply(session_id))

    def night_interrupt_snapshot(self) -> Dict[str, Any]:
        """当前打扰段快照（观测/调试用；转发 self.service，异常时返回 ``{}``）。"""
        try:
            return dict(self.service.night_interrupt_snapshot() or {})
        except Exception:
            return {}

    def night_interrupt_segments(self, cycle_key: Optional[str] = None) -> list:
        """已闭合打扰段归档（供观测与整夜合计结算消费；转发 self.service。结算会清空本周期条目）。"""
        try:
            return list(self.service.night_interrupt_segments(cycle_key))
        except Exception:
            return []

    # PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-3）：原 M9 赖床
    # 链路四转发口（in_night_sleep_cycle / night_sleep_end_minute / extend_night_sleep_until /
    # note_nap_lie_in_applied）随赖床工具整链删除；夜扰记账转发（上）与 initialize_from_persona
    # （init_service 生产兜底链）保留。

    @property
    def _bg_tasks(self) -> list:
        """评估后台任务列表（与内层同一份；Hook 侧防堆积裁剪与 stop() 清理共用）。"""
        return self.service._bg_tasks

    @_bg_tasks.setter
    def _bg_tasks(self, value: list) -> None:
        self.service._bg_tasks = value

    async def start(self) -> None:
        """启动睡眠检测循环（模块总开关关闭时不启动，热重载由循环内门控兜底）。"""
        self._register_event_subscriptions()
        if not self.is_enabled():
            self._plugin.ctx.logger.info("情绪模块已停用（emotion.enabled=false），循环未启动")
            return
        self.service.start()

    async def stop(self) -> None:
        self._unregister_event_subscriptions()
        await self.service.stop()
