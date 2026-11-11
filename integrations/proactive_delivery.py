"""Proactive delivery wiring. The companion budget is the only counter owner.

The delivery facade preserves existing publishers' bus/gate interface while the
underlying EventBus contains no policy. No process-global budget is installed.
"""
from __future__ import annotations

from typing import Any
from core.event_bus import EventBus
from companion.proactive_budget import ProactiveBudget


class ProactiveDelivery(EventBus):
    def __init__(self, decision_logger: Any = None) -> None:
        super().__init__(decision_logger)
        self._budget: ProactiveBudget | None = None
        self._budget_unsubscribe = None

    @property
    def budget(self) -> ProactiveBudget | None:
        return self._budget

    def allow_proactive(self, channel: str = '', intent: str = '', stream_id: str = '') -> bool:
        # fail-closed（2026-11-04 模块评估 P2-01）：预算未安装时拒绝，与链上其余
        # 门控同口径（video_companion 预算 None 保守拒绝）；装配经
        # install_proactive_budget 注入，测试注入路径不变。
        if self._budget is None:
            return False
        return self._budget.allow_proactive(channel, intent, stream_id)

    def stop_budget(self) -> None:
        if self._budget_unsubscribe is not None:
            self._budget_unsubscribe()
            self._budget_unsubscribe = None
        self._budget = None


def install_proactive_budget(bus: ProactiveDelivery, config_provider: Any = None,
                             decision_logger: Any = None, data_dir: Any = None,
                             target_stream_provider: Any = None,
                             elasticity_provider: Any = None) -> ProactiveBudget:
    """装配主动预算，并在装配后注入可选弹性信号源（3.4）。

    ``elasticity_provider`` 缺省 None 时 budget 的 ``cap()`` 与旧实现逐字一致；
    真实组合根经本参数注入 affection/emotion/holiday 信号，避免「配置开了但没人
    接线」。接线走 ``ProactiveBudget.set_elasticity_provider(...)``——构造参数
    只保留既有字段，信号源是装配期可替换的端口（重复装配/热重载可重设）。
    """
    # Repeated assembly on the same facade must not leave a stale second writer.
    bus.stop_budget()
    budget = ProactiveBudget(config_provider=config_provider, decision_logger=decision_logger,
                             data_dir=data_dir, target_stream_provider=target_stream_provider)
    # 3.4（2026-11-11 复核修复）：install 后显式核验/注入信号端口；budget 侧若
    # 新增/移除 setter，装配不因此拖挂启动（保留 None = 基础预算的旧行为）。
    setter = getattr(budget, 'set_elasticity_provider', None)
    if callable(setter):
        setter(elasticity_provider)
    elif elasticity_provider is not None:
        budget._elasticity_provider = elasticity_provider
    bus._budget_unsubscribe = bus.subscribe('proactive.used', budget.on_proactive_used,
                                        owner='proactive_budget')
    bus._budget = budget
    return budget
