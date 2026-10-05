"""EmotionServices —— 情绪模块的服务束（组合根）。

plugin.py 集成方式（与其他模块同构）::

    self._emotion = EmotionServices(self)
    await self._emotion.start()
    ...
    await self._emotion.stop()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .service import EmotionService
from .vad import VADState

logger = logging.getLogger(__name__)


class EmotionServices:
    """情绪模块服务束：状态机 + 睡眠检测循环 + 评估任务管理。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self.service = EmotionService(plugin)
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3 起由 emotion.bridge 追加）
        self._event_unsubs: list = []

    # 事件订阅挂点（start() 调用；stop() 注销）
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（M3：emotion.bridge 订阅 relation.*/schedule.commitment_*/qzone.*）。

        桥内处理器绝不做绝对状态设置，只经 ``record_external_event`` 入账（§6.3-1）；
        总线缺失时静默降级（§八.2）；幂等：已注册时直接返回。
        """
        if self.bus is None or self._event_unsubs:
            return
        try:
            from .bridge import register_bridge

            self._event_unsubs.extend(register_bridge(self.bus, self.service, self._plugin))
        except Exception as exc:
            logger.warning("emotion 事件订阅失败（忽略）：%s", exc)
            self._unregister_event_subscriptions()

    def _unregister_event_subscriptions(self) -> None:
        """订阅注销挂点（stop() 调用）：执行并清空全部反订阅闭包。"""
        for unsub in self._event_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubs.clear()

    # 转发入口（plugin.py / 其他模块调用）
    def set_persona(self, lover_name: str, personality: str) -> None:
        self.service.set_persona(lover_name, personality)

    def mood_text(self) -> str:
        return self.service.mood_text()

    # ── Command / Tool 面转发（components.py 组件入口；签名与内层一致）──
    def params_summary(self) -> str:
        """``/cateye_test_emotion_config`` 查看文本（转发 self.service）。"""
        return self.service.params_summary()

    def reset_params(self) -> str:
        """``/cateye_test_emotion_config reset``：恢复默认参数（转发 self.service）。"""
        return self.service.reset_params()

    def clear_log(self) -> str:
        """``/cateye_test_emotion_config clear``：清空当天情绪日志（转发 self.service）。"""
        return self.service.clear_log()

    async def initialize_from_persona(self) -> str:
        """``/cateye_test_emotion_init`` 主体：人设 → LLM 推导 → 落盘（转发 self.service）。"""
        return await self.service.initialize_from_persona()

    def target_multiplier(self, is_target: bool) -> float:
        """§6.2 A 的 K（target 加成 / 非 target 系数；Tool 上报事件用，转发 self.service）。"""
        return self.service.target_multiplier(is_target)

    async def record_external_event(self, source: str, description: str,
                                    delta: Optional[VADState] = None, *,
                                    use_llm: bool = False,
                                    multiplier: float = 1.0,
                                    session_id: str = "") -> Dict[str, Any]:
        """外部事件统一入账入口（Tool 上报路径；签名与内层一致，转发 self.service）。

        ``session_id``（宿主 ToolInvocation 的 stream_id / 聊天流）供 §2.4.6 A3
        跨来源去重使用；缺省空串归入同一"未知流"。
        """
        return await self.service.record_external_event(
            source, description, delta, use_llm=use_llm, multiplier=multiplier,
            session_id=session_id)

    def injection_text(self) -> str:
        """情绪底色注入文本（planner/replyer 注入 Hook 入口；转发 self.service）。

        与 ``mood_text()`` 同源但多带注入要求行；Hook 侧仅消费字符串。
        """
        return self.service.injection_text()

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

    def is_enabled(self) -> bool:
        return self.service._enabled()

    async def evaluate_reply(self, reply_text: str, session_id: str = "") -> None:
        """回复后 ΔVAD 评估入口（replyer.after_response Hook 的后台任务；转发 self.service）。

        与内层 ``EmotionService.evaluate_reply`` 同签名（含 session_id target 识别链路），
        节流与静默失败语义全部由内层负责。
        """
        await self.service.evaluate_reply(reply_text, session_id=session_id)

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
