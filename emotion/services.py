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

    def mood_effect(self) -> Dict[str, Any]:
        """情绪→行为结构化系数（M4；跨模块消费入口，附录 C）。

        与 ``mood_text()`` 同为转发入口：patrol/qzone 经 ``plugin._emotion.mood_effect()``
        读取；模块停用/异常时返回恒等中性值（可空降级，§八.2）。
        """
        try:
            return self.service.mood_effect()
        except Exception:
            return {"proactive_rate": 1.0, "miss_threshold": 1.0, "post_rate": 1.0, "intent_hint": ""}

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
