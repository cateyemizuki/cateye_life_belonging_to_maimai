"""跨模块最小契约（D1，2026-11-01 模块解耦与统一联动架构计划 §3.1/§四）。

只声明消费方实际用到的最小 Protocol 与提供者形态，无业务实现：
本模块不得导入任何业务模块（companion/emotion/planning/qzone）或 plugin，
只可用标准库类型（层间纪律与 core/time_utils.py:3-4 同源）。

装配口径（组合根 plugin.py 负责）：提供者一律**惰性**——被注入的实例
（睡眠引擎/情绪服务束）晚于消费方服务束装配，调用时才解析；返回 None =
能力未装配，消费方按既有可空降级语义处理（§八.2），不改为启动失败。
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Protocol


class SleepEngineGate(Protocol):
    """睡眠引擎对情绪侧的最小门面（D1：emotion 不再直读 ``plugin._sleep_engine``）。

    实现方 = ``planning.sleep_engine.SleepEngine``（鸭子类型兼容既有测试桩）；
    ``is_asleep`` 返回 True/False = 引擎权威，None = 引擎不表态（调用方回退
    旧窗口逻辑，PR3 单一 wake provider 口径不变）。
    """

    def is_asleep(self, now: Any) -> Optional[bool]:
        """引擎是否睡着；None = 不表态（未启用/未装配/无本夜会话）。"""
        ...

    def note_interruption(self, current: Any) -> Any:
        """夜间打断喂给引擎打断状态机（只记账，L1 既有语义）。"""
        ...


class EmotionValenceSource(Protocol):
    """情绪模块对陪伴侧的最小只读面（M3 B2 既有公开入口收拢为显式契约）。

    实现方 = ``emotion.services.EmotionServices``；缺失/停用由消费方降级
    （好感调制乘数恒 1.0，§6.2 B）。
    """

    def is_enabled(self) -> bool:
        """情绪模块是否启用。"""
        ...

    def effective_valence(self) -> Optional[float]:
        """当前生效效价 V；不可用返回 None。"""
        ...


#: 惰性提供者（装配期注入；调用返回 None = 能力未装配）
SleepEngineGateProvider = Callable[[], Optional[SleepEngineGate]]
EmotionValenceSourceProvider = Callable[[], Optional[EmotionValenceSource]]
