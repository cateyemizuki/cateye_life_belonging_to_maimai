"""睡眠端点共享写回 sleep_schedule（日程系统二期 PR3，定稿 §4.2.3）。

R7 依据 = 用户 2026-10-26 直派【日程系统二期 PR3】+ 计划盘点 D-k：`nap_lie_in` 原先
独享「改 sleep 条目端点 + 顺延重叠清醒条目 + 跨午夜第二次写回」整段逻辑，定稿要求
抽到本模块供**旧赖床工具与睡眠引擎共用**，避免两份漂移。

本模块只做纯函数 + 一个注入式写回助手：

- 纯函数：``parse_nap_target`` / ``normalize_hhmm`` / ``lie_in_max_minutes`` /
  ``find_sleep_item`` / ``nap_end_datetime`` / ``nap_schedule_date`` /
  ``postpone_items_for_lie_in`` —— 与原 ``planning/tools_service.py`` 同口径
  （M9 as-built，逐字语义；``planning_tools`` 已改为委托本模块）。
- :func:`update_sleep_endpoint`：一条 sleep 条目端点更新的**唯一**实现
  （钳上限 → 改 sleep end → 同日顺延醒来窗口内的清醒条目 → 写回 → 跨午夜补写
  醒来日）。调用方（引擎）只给数据与写回回调，行为由本函数统一。
- :class:`SleepEndpointHook`：给 ``SleepEngine`` 的 ``sleep_endpoint_updater`` 用的
  插件适配器（duck-typing 取 ``plugin._schedule_query`` / ``plugin._schedule_gen``；
  失败静默，绝不拖挂 tick）。

PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-3）：旧赖床工具
已整链删除，``nap_tool_managed_by_engine`` / ``nap_tool_engine_managed_reply`` /
``NAP_MANAGED_BY_ENGINE_TEXT`` 随之移除；本模块保留的端点写回面只服务
``SleepEndpointHook`` 引擎链。

依赖纪律：不 import tools_service（避免循环）；不 import emotion；不调用任何 LLM。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .schedule_items import find_night_sleep_item, is_night_sleep_item
from .time_utils import DAY_MINUTES

logger = logging.getLogger(__name__)

#: 钳制上限缺省（[schedule_advanced] lie_in_max_until；HH:MM）
DEFAULT_LIE_IN_MAX_UNTIL = "12:00"


# ======================================================================
# 纯函数（与原 tools_service 同口径；迁移后 tools_service 委托本模块）
# ======================================================================
def parse_nap_target(value: Any) -> Optional[int]:
    """赖床时间解析：容忍 ``HH:MM`` / ``H:MM`` / 纯小时数；非法/缺参 → None。

    纯小时数按整点（``"9"`` → 09:00）；全角冒号容错；不抛异常。
    """
    raw = str(value if value is not None else "").strip().replace("：", ":")
    if not raw:
        return None
    head, sep, tail = raw.partition(":")
    try:
        hour = int(head)
        if not sep:
            minute = 0                       # 纯小时数 → 整点
        else:
            if not tail.strip():
                return None                  # "9:" 缺分钟
            minute = int(tail)
    except (TypeError, ValueError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def normalize_hhmm(value: Any, default: str = DEFAULT_LIE_IN_MAX_UNTIL) -> str:
    """规范成 ``"HH:MM"`` 文本（非法回退 ``default``，再坏回 12:00）。"""
    minute = parse_nap_target(value)
    if minute is None:
        minute = parse_nap_target(default)
    if minute is None:
        minute = parse_nap_target(DEFAULT_LIE_IN_MAX_UNTIL) or 12 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def lie_in_max_minutes(value: Any) -> int:
    """``lie_in_max_until`` 配置值 → 当天分钟数（非法回退 12:00；钳制与提示同源）。"""
    return parse_nap_target(normalize_hhmm(value)) or 12 * 60


def find_sleep_item(items: Any) -> Optional[Dict[str, Any]]:
    """今日日程里的**夜晚睡眠条目**（共享口径 ``planning/schedule_items.py``）。"""
    rows = [item for item in (items or []) if isinstance(item, dict)]
    return find_night_sleep_item(rows)


def nap_end_datetime(now: datetime, minute: Any) -> datetime:
    """把「当天分钟数」落成 ``now`` 之后最近的时刻（跨午夜自动 +1 天）。"""
    value = int(minute)
    end = now.replace(hour=(value // 60) % 24, minute=value % 60, second=0, microsecond=0)
    return end if end > now else end + timedelta(days=1)


def nap_schedule_date(now: datetime, sleep_time: Any, wake_time: Any) -> str:
    """「今日日程」日期（新一天起点 = ``[schedule] wake_time`` 锚点）。

    夜间睡眠条目属于**入睡日**；凌晨（早于 wake 锚点的跨午夜段）读昨天，否则读今天。
    夜猫子作息（sleep <= wake，同日）一律读今天。锚点解析失败按缺省 23:00/07:00。
    """
    from core.time_utils import parse_hhmm
    sleep_min = parse_hhmm(str(sleep_time or ""), 23 * 60)
    wake_min = parse_hhmm(str(wake_time or ""), 7 * 60)
    cur = now.hour * 60 + now.minute
    if sleep_min > wake_min and cur < wake_min:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def postpone_items_for_lie_in(items: Any, lo: Any, hi: Any) -> int:
    """把延长窗口 ``[lo, hi)`` 内重叠的**非睡眠**条目顺延到 ``hi`` 之后（就地改）。

    红线（与 M9/P6 as-built 逐字一致）：只动与窗口重叠的条目（``start < hi`` 且
    ``end > lo``）；保留时长、``end`` 封顶 24:00（不产生倒挂）；夜晚睡眠条目不动。
    返回被顺延的条目数。
    """
    try:
        low = int(lo)
        high = int(hi)
    except (TypeError, ValueError):
        return 0
    moved = 0
    for item in items or []:
        if not isinstance(item, dict) or is_night_sleep_item(item):
            continue
        try:
            start = int(item.get("start_minute") or 0)
            end = int(item.get("end_minute") or 0)
        except (TypeError, ValueError):
            continue
        if end <= start or start >= high or end <= low:
            continue
        item["start_minute"] = int(high)
        item["end_minute"] = min(DAY_MINUTES, int(high) + (end - start))
        moved += 1
    return moved


# ======================================================================
# 一条 sleep 端点更新的唯一实现（旧工具 + 引擎共用）
# ======================================================================
@dataclass
class EndpointUpdate:
    """一次 sleep 端点写回的结果（调用方据此决定话术/是否计次）。"""

    ok: bool = False
    changed: bool = False
    target_minute: int = 0
    clamped: bool = False
    moved_awake: int = 0
    wake_day_written: int = 0
    wake_day: Optional[str] = None
    schedule_date: Optional[str] = None
    sleep_item: Optional[Dict[str, Any]] = None
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


def update_sleep_endpoint(
    *,
    now: datetime,
    target_minute: Any,
    items: List[Dict[str, Any]],
    sleep_item: Optional[Dict[str, Any]] = None,
    limit_minute: Any = None,
    schedule_date: Optional[str] = None,
    reason: str = "",
    postpone_awake: bool = True,
    apply_items: Optional[Callable[..., Any]] = None,
    postpone_wake_day: Optional[Callable[..., Any]] = None,
    clamp_to_limit: bool = True,
) -> EndpointUpdate:
    """把一条 sleep 条目的 ``end_minute`` 推到目标时刻（共享写回；失败返回 ok=False）。

    Args:
        now: 当前时刻（写回窗口起点/跨午夜判定用；测试可注入）。
        target_minute: 目标端点（当天分钟数；跨午夜可 >1440）。
        items: 入睡日日程条目（就地修改；调用方保证是 DB 行的副本）。
        sleep_item: 已找到的 sleep 条目（缺省按共享口径现找）。
        limit_minute: ``lie_in_max_until`` 分钟数（``clamp_to_limit`` 时生效）。
        schedule_date: 入睡日（缺省 ``now`` 的日历日）。
        reason: 写回原因（工具 = ``tool_edit``；引擎 = ``sleep_engine_*``）。
        postpone_awake: 是否顺延与延长窗口重叠的清醒条目（旧工具 true；引擎醒来
            缩短端点 false）。
        apply_items: ``(items, date, reason) -> int`` 写回回调（generator.apply_schedule_items
            的鸭子口）；缺省 → 只改内存不落库并返回 ok=False。
        postpone_wake_day: ``(day, lo, hi) -> int`` 跨午夜醒来日补写回调。
        clamp_to_limit: 是否按上限钳（引擎已自行钳过 → false）。
    """
    result = EndpointUpdate(schedule_date=schedule_date)
    try:
        target = int(target_minute)
    except (TypeError, ValueError):
        result.error = "bad_target"
        return result
    if clamp_to_limit and limit_minute is not None:
        try:
            limit = int(limit_minute)
        except (TypeError, ValueError):
            limit = None
        if limit is not None and target > limit:
            target = limit
            result.clamped = True
    result.target_minute = target
    item = sleep_item or find_sleep_item(items)
    result.sleep_item = item
    if item is None:
        result.error = "no_sleep_item"
        return result
    try:
        current_end = int(item.get("end_minute") or 0)
    except (TypeError, ValueError):
        current_end = 0
    item["end_minute"] = target
    result.changed = current_end != target
    # 醒来日帧：跨午夜时窗口起点 = 该日 0 点（与旧工具 _nap_end_datetime 同口径）
    end_dt = nap_end_datetime(now, target)
    wake_day = end_dt.strftime("%Y-%m-%d")
    today = schedule_date or now.strftime("%Y-%m-%d")
    result.wake_day = wake_day
    result.schedule_date = today
    window_lo = now.hour * 60 + now.minute if wake_day == today else 0
    if postpone_awake and wake_day == today:
        result.moved_awake = postpone_items_for_lie_in(items, window_lo, target)
    if apply_items is None:
        result.error = "no_writer"
        return result
    try:
        written = apply_items(items, today, reason)
    except TypeError:
        # 兼容 (items, date) 两参回调
        written = apply_items(items, today)
    except Exception as exc:
        result.error = f"write_failed: {exc}"
        return result
    if not written:
        result.error = "write_failed"
        return result
    if postpone_awake and wake_day != today and postpone_wake_day is not None:
        try:
            result.wake_day_written = int(postpone_wake_day(wake_day, window_lo, target) or 0)
        except Exception:
            result.wake_day_written = 0
    result.ok = True
    return result
# ======================================================================
# 引擎侧适配器（plugin 装配用；不 import 插件，全部 duck-typing）
# ======================================================================
class SleepEndpointHook:
    """``SleepEngine(sleep_endpoint_updater=...)`` 的插件适配器。

    醒来/延长时把 sleep 条目的 ``end`` 写到实际端点（共享写回，失败静默）。
    读取链与旧赖床工具完全一致：``plugin._schedule_query`` + ``plugin._schedule_gen``
    （``apply_schedule_items`` 共享漏斗，不消费未来约定）；跨午夜补写醒来日同样走
    ``query.db.replace_day_schedule`` + ``cycle_state.enter_schedule_cycle``。
    """

    def __init__(self, plugin: Any, *, reason: str = "sleep_engine_wake") -> None:
        self._plugin = plugin
        self._reason = reason

    def __call__(self, now: datetime, end_minute: Any, reason: str = "",
                 schedule_date: Optional[str] = None) -> EndpointUpdate:
        try:
            query = getattr(self._plugin, "_schedule_query", None)
            if query is None or not callable(getattr(query, "get_day_schedule", None)):
                return EndpointUpdate(ok=False, error="no_query")
            config = getattr(self._plugin, "config", None)
            schedule = getattr(config, "schedule", None)
            # 日程日帧真源：引擎会话的 schedule_date（昨夜条目属于入睡日）。
            # 早醒/晚醒都在 wake 锚点之后，单靠 now 推日期会把写回打到**今夜**条目上
            # （旧行为），导致刚睡醒这一夜的端点不动 → patrol/qzone 仍读旧锚点。
            today = str(schedule_date or "").strip() or nap_schedule_date(
                now, getattr(schedule, "sleep_time", ""),
                getattr(schedule, "wake_time", ""))
            items = [dict(row) for row in (query.get_day_schedule(today) or [])
                     if isinstance(row, dict)]
            generator = getattr(self._plugin, "_schedule_gen", None)
            apply_items = getattr(generator, "apply_schedule_items", None)
            if not callable(apply_items):
                return EndpointUpdate(ok=False, error="no_generator")

            def _apply(rows: List[Dict[str, Any]], date: str, why: str) -> int:
                return int(apply_items(rows, date, consume_commitments=False,
                                       reason=why) or 0)

            def _postpone_wake_day(day: str, lo: int, hi: int) -> int:
                return self._postpone_wake_day(query, day, lo, hi)

            return update_sleep_endpoint(
                now=now, target_minute=end_minute, items=items,
                limit_minute=None, schedule_date=today,
                reason=reason or self._reason, postpone_awake=False,
                apply_items=_apply, postpone_wake_day=_postpone_wake_day,
                clamp_to_limit=False)
        except Exception as exc:  # 端点写回失败绝不阻塞 tick
            logger.warning("睡眠端点写回适配器失败（忽略）：%s", exc)
            return EndpointUpdate(ok=False, error=str(exc))

    def _postpone_wake_day(self, query: Any, day: str, lo: int, hi: int) -> int:
        """跨午夜醒来日日程的顺延写回（与旧赖床工具同一实现口径）。"""
        try:
            items = [dict(row) for row in (query.get_day_schedule(day) or [])
                     if isinstance(row, dict)]
            if not items:
                return 0
            moved = postpone_items_for_lie_in(items, lo, hi)
            db = getattr(query, "db", None)
            if not moved or db is None:
                return 0
            reason = self._reason
            stamps = (None, None)
            try:
                from .cycle_state import (REASON_TOOL_EDIT, enter_schedule_cycle,
                                          entry_context_from_plugin)
                reason = REASON_TOOL_EDIT
                stamps = enter_schedule_cycle(
                    getattr(self._plugin, "_data_dir", None), day, items,
                    reason=reason,
                    decision_logger=getattr(self._plugin, "_decision_logger", None),
                    **entry_context_from_plugin(self._plugin))
            except Exception:
                pass
            db.replace_day_schedule(
                day, items, entry_reason=reason,
                cycle_start_ts=(stamps[0] if stamps else None),
                cycle_end_ts=(stamps[1] if stamps else None))
            return moved
        except Exception as exc:
            logger.warning("跨午夜醒来日顺延失败（忽略）：%s", exc)
            return 0


__all__ = [
    "DEFAULT_LIE_IN_MAX_UNTIL", "EndpointUpdate",
    "SleepEndpointHook", "find_sleep_item", "lie_in_max_minutes",
    "nap_end_datetime", "nap_schedule_date",
    "normalize_hhmm", "parse_nap_target",
    "postpone_items_for_lie_in", "update_sleep_endpoint",
]

