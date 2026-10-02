"""EmotionServices —— 情绪模块的服务束（组合根）。

plugin.py 集成方式（与其他模块同构）::

    self._emotion = EmotionServices(self)
    await self._emotion.start()
    ...
    await self._emotion.stop()
"""

from __future__ import annotations

from typing import Any

from .service import EmotionService


class EmotionServices:
    """情绪模块服务束：状态机 + 睡眠检测循环 + 评估任务管理。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self.service = EmotionService(plugin)
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3+ 在 _register_event_subscriptions 里追加）
        self._event_unsubs: list = []

    # 事件订阅挂点（M0：登记于此，M3+ 消费者按需 subscribe 并存入 _event_unsubs）
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（start() 调用）。M0 本束无订阅者：
        唯一的实际订阅（proactive.used → 预算计数器）由 core/event_bus 在装配时安装。
        M3+ 消费者在此 ``self._event_unsubs.append(self.bus.subscribe(...))``。"""
        if self.bus is None:
            return

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
