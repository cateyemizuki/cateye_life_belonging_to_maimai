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
                             target_stream_provider: Any = None) -> ProactiveBudget:
    # Repeated assembly on the same facade must not leave a stale second writer.
    bus.stop_budget()
    budget = ProactiveBudget(config_provider=config_provider, decision_logger=decision_logger,
                             data_dir=data_dir, target_stream_provider=target_stream_provider)
    bus._budget_unsubscribe = bus.subscribe('proactive.used', budget.on_proactive_used,
                                        owner='proactive_budget')
    bus._budget = budget
    return budget
