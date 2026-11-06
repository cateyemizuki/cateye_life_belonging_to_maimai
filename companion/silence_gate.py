"""陪伴静默/午休判定对外公共门面（D3，2026-11-01 模块解耦计划 §3.1）。

qzone 等兄弟模块经组合根公共口（``plugin.companion_silence_gate``）取本门面，
不直接导入 companion.patrol——判定语义仍由 patrol 单一真源持有，本模块只做
绑定转发，不复制任何口径（§24.2 任务B「所有消费点共享，禁止自建副本」）。
"""

from __future__ import annotations

from typing import Any


class CompanionSilenceGate:
    """patrol 判定器的绑定转发面：实例持有 plugin，方法逐一委托 patrol 真源。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def resolve_silence_window(self) -> Any:
        """静默窗生效值（含真实作息锚点解析；返回对象带 start/end 原文）。"""
        from .patrol import resolve_silence_window

        return resolve_silence_window(self._plugin)

    def nap_item_matches(self, activity: Any) -> bool:
        """当前活动条目是否午休/小睡（纯判定）。"""
        from .patrol import nap_item_matches

        return nap_item_matches(activity)

    def nap_intent_matches_now(self, day_schedule: Any, now: Any = None) -> bool:
        """日程 spine 意图行按时间窗是否命中午休（纯判定）。"""
        from .patrol import nap_intent_matches_now

        engine = getattr(self._plugin, "_sleep_engine", None)
        return nap_intent_matches_now(day_schedule, now,
                                      dynamic=bool(getattr(engine, "dynamic_enabled", False)))

    def actual_nap_item_matches(self, activity: Any) -> bool:
        from .patrol import actual_nap_item_matches
        return actual_nap_item_matches(activity)

    def sleep_engine_asleep(self, now: Any = None) -> bool:
        """睡眠引擎是否判定仍在睡（异常/未装配 False，与 patrol 同语义）。"""
        from .patrol import sleep_engine_asleep

        return sleep_engine_asleep(self._plugin, now)
