"""日程条目口径共享：**夜晚睡眠条目**判定与条目**结束分钟**读取（口径唯一真源）。

准入背景（M10 第 6 段 / 附录 §五，裁决 B2/B4）：日程生成锚点改为"最后日程结束后多久生成
新一天日程"，基准 = **当天最后一个非睡眠日程条目的结束时间**；该读数与 M9 赖床工具
（附录 §4.4 "今日 sleep 条目"）的判定必须**同源**，否则两处口径会各自漂移。

本模块是上述口径的**唯一真源**（纯模块级函数：无 self、无 IO、无插件依赖）：

- ``tools_service.py``（M9 赖床工具 / §4.4）：取"今日夜晚 sleep 条目"；
- ``auto_scheduler.py``（B2 生成锚点 / §五）：取"最后非睡眠条目结束"。

判定口径（与 M9 逐字一致，附录 §4.4）：

- 主判 ``goal_type == "sleep"``；
- 名称兜底**只认夜间词**（``睡觉`` / ``睡眠`` / ``安睡``）：日程生成 prompt 让"睡觉"条目用
  ``goal_type=daily_routine``（M5 实测），只看 goal_type 会漏判；午休 / 午睡 / 小憩 / 打盹
  **不算**（§八.7 / C4-补充：午休本轮不动）。

⚠️ 与 ``planning/proactive_service.py`` 的 ``_is_sleep_schedule_item``（M10 第 5 段 §三 L2 早晚安
基准）**不合并**：那一处是**更宽**的词表（含午休/午睡/小憩/打盹，与 ``emotion/service.py`` 的
``_is_sleep_item`` 对齐，服务问候基准过滤）；本模块只服务"M9 赖床 + B2 生成锚点"这一口径，
两处各自服务不同基准，合并会回退第 5 段语义。
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Sequence, Tuple

#: 夜晚睡眠条目的名称兜底关键词（**只含夜间词**；午休/午睡/小憩/打盹不算）
NIGHT_SLEEP_NAME_KEYWORDS: Tuple[str, ...] = ("睡觉", "睡眠", "安睡")


def _field(item: Any, name: str) -> Any:
    """dict 行（schedule_items 表）与对象（Goal）两形态取字段。"""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def is_night_sleep_item(item: Any) -> bool:
    """条目是否为**夜晚睡眠**条目（主判 ``goal_type=="sleep"``，名称兜底仅夜间词）。"""
    if item is None:
        return False
    if str(_field(item, "goal_type") or "") == "sleep":
        return True
    name = str(_field(item, "name") or "")
    return any(keyword in name for keyword in NIGHT_SLEEP_NAME_KEYWORDS)


def item_end_minute(item: Any) -> Optional[int]:
    """条目结束分钟数（当日帧内；跨午夜条目可能 > 1440）；取不到 → ``None``。

    取数优先级（与既有取数口径一致）：``end_minute``（schedule_items 表列）→
    ``parameters.time_window``（Goal 形态）→ ``conditions.time_window``。
    """
    end = _field(item, "end_minute")
    if end is not None:
        try:
            return int(end)
        except (TypeError, ValueError):
            return None
    for holder in (_field(item, "parameters"), _field(item, "conditions")):
        if not isinstance(holder, dict):
            continue
        window = holder.get("time_window")
        if not isinstance(window, (list, tuple)) or len(window) < 2:
            continue
        try:
            return int(window[1])
        except (TypeError, ValueError):
            continue
    return None


def find_night_sleep_item(items: Optional[Iterable[Any]]) -> Optional[Any]:
    """今日日程里的**夜晚睡眠**条目（两轮判定：先 ``goal_type=="sleep"``，再名称兜底）。

    返回命中的原条目（dict 行或 Goal 对象原样），无命中 → ``None``。
    """
    rows: List[Any] = [item for item in (items or []) if item is not None]
    for item in rows:
        if str(_field(item, "goal_type") or "") == "sleep":
            return item
    for item in rows:
        name = str(_field(item, "name") or "")
        if any(keyword in name for keyword in NIGHT_SLEEP_NAME_KEYWORDS):
            return item
    return None


def last_non_sleep_end_minute(items: Optional[Sequence[Any]]) -> Optional[int]:
    """当天最后一个**非睡眠**条目（夜晚睡眠条目除外的任何条目）的结束分钟数。

    跨午夜条目可能 > 1440；无可用条目（空表/全是睡眠条目/取不到结束时间）→ ``None``
    —— 调用方据此回退作息锚点（B4："有日程用日程、无日程用锚点"）。
    """
    latest: Optional[int] = None
    for item in (items or []):
        if is_night_sleep_item(item):
            continue
        end = item_end_minute(item)
        if end is None:
            continue
        if latest is None or end > latest:
            latest = end
    return latest
