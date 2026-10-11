"""日程查询：当前活动、未来活动、全天摘要（带 N 分钟窗口缓存）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .schedule_db import ScheduleDB
from .time_utils import (describe_energy, format_day_minute, get_energy_level,
                         is_in_day_frame, is_in_day_frame_marked,
                         is_upcoming_day_minute, is_upcoming_day_minute_marked,
                         ring_minutes_ahead)
from core.time_utils import minutes_of_now

# sleep 类型条目不算「正在做的事」（仅 include_sleep=True 的调用方可见，如情绪睡眠循环）
_SKIP_TYPES = {"sleep"}


class ScheduleQuery:
    def __init__(self, db: ScheduleDB, cache_minutes: int = 15) -> None:
        self._db = db
        self._cache_minutes = max(1, int(cache_minutes))
        self._cache: Dict[str, Tuple[int, Any]] = {}  # key -> (窗口号, 结果)

    @property
    def db(self) -> ScheduleDB:
        """底层日程库（只读访问器）。

        P6 顺延写回用：跨午夜赖床要顺延的是**醒来日**日程（与本查询实例同一份库），
        不能借入睡日那一次写回携带，故按层访问同一 ``ScheduleDB``。
        """
        return self._db

    def get_day_schedule(self, date: str) -> List[Dict[str, Any]]:
        return self._db.get_day_schedule(date)

    def find_current_activity(self, date: str, now: Optional[datetime] = None,
                              include_sleep: bool = False) -> Optional[Dict[str, Any]]:
        """返回当前正在进行的日程条目（默认跳过 sleep 类型）；结果按 N 分钟窗口缓存。

        include_sleep=True 时 sleep 条目也参与匹配（情绪睡眠循环用）；
        缓存键区分该开关，避免共享实例上不同调用方互相串用结果。
        覆盖判定走 :func:`time_utils.is_in_day_frame_marked`（同一个 24h 环 + A-16
        日期帧标记）——凌晨 00:30 也能命中「23:00 → 次日 07:00」的跨午夜睡觉条目；
        并合并**前一日期帧**的行：``day_offset=1``（今晚午夜段）与 ``end>1440``
        （跨午夜延续）的昨日条目在今天凌晨仍可命中。
        """
        now = now or datetime.now()
        window = minutes_of_now(now) // self._cache_minutes
        key = f"cur_{date}_{int(bool(include_sleep))}"
        cached = self._cache.get(key)
        if cached and cached[0] == window:
            return cached[1] or None
        current = None
        cur_min = minutes_of_now(now)
        for item in self._db.get_day_schedule(date):
            if not include_sleep and item.get("goal_type") in _SKIP_TYPES:
                continue
            if is_in_day_frame_marked(item.get("start_minute") or 0, item.get("end_minute") or 0,
                                      cur_min, item.get("day_offset")):
                current = item
                break
        if current is None:
            prev_date = self._previous_date(date)
            if prev_date:
                for item in self._db.get_day_schedule(prev_date):
                    if not include_sleep and item.get("goal_type") in _SKIP_TYPES:
                        continue
                    end = int(item.get("end_minute") or 0)
                    offset = item.get("day_offset")
                    if offset == 1:
                        # 昨日标记的"今晚午夜段"：今天就发生在 start..end（钟点）区间
                        start = int(item.get("start_minute") or 0)
                        end_clock = end if end < 1440 else end - 1440
                        if start <= cur_min < end_clock:
                            current = item
                            break
                    elif end > 1440 and is_in_day_frame(
                            item.get("start_minute") or 0, end, cur_min):
                        current = item
                        break
        if len(self._cache) > 64:  # 简单防膨胀
            self._cache.clear()
        self._cache[key] = (window, current or "")
        return current

    def find_next_activities(self, date: str, limit: int = 3, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """今天**还没开始**的活动（前 ``limit`` 条），跨午夜按同一个 24h 环判定。

        ``N1 读取侧``：起点比较统一走 ``time_utils.is_upcoming_day_minute``（唯一口径）——
        23:35 时帧首（``0`` 分钟）条目按环上的**帧尾**（今晚 24:00）算作"接下来"，
        不再被 ``start > cur`` 的单调比较漏掉；列表按环上距离排序（同帧内按 start 升序，
        帧尾条目自然排在最后）。正在进行中的尾段（如 00:30 的 ``(0, 60)``）不算"接下来"。
        """
        now = now or datetime.now()
        cur_min = minutes_of_now(now)
        upcoming: List[Tuple[int, Dict[str, Any]]] = []
        for item in self._db.get_day_schedule(date):
            if item.get("goal_type") in _SKIP_TYPES:
                continue
            raw_start = item.get("start_minute")
            if not is_upcoming_day_minute_marked(raw_start, cur_min, item.get("end_minute"),
                                                 item.get("day_offset")):
                continue
            upcoming.append((ring_minutes_ahead(raw_start, cur_min), item))
        # 前一日期帧的 day_offset=1 行（昨晚标记的"今晚午夜段"）在今天尚未开始的也算"接下来"
        prev_date = self._previous_date(date)
        if prev_date:
            for item in self._db.get_day_schedule(prev_date):
                if item.get("goal_type") in _SKIP_TYPES or item.get("day_offset") != 1:
                    continue
                start = int(item.get("start_minute") or 0)
                if start > cur_min:
                    upcoming.append((start - cur_min, item))
        upcoming.sort(key=lambda pair: pair[0])
        return [item for _ahead, item in upcoming[:max(0, int(limit))]]

    @staticmethod
    def _previous_date(date: str) -> Optional[str]:
        """``YYYY-MM-DD`` → 前一天（解析失败返回 None，调用方跳过跨日合并）。"""
        try:
            return (datetime.strptime(date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return None

    def format_activity_line(self, item: Dict[str, Any], now: Optional[datetime] = None) -> str:
        """当前活动的一行描述（含剩余时间与情绪/能量提示），注入与 Tool 共用。

        时间窗一律经 ``time_utils.format_day_minute`` 渲染：跨午夜条目 ``end>1440``
        显示为「23:00-次日 07:00」（不再是裸 ``//60`` 的 31:00）。
        """
        now = now or datetime.now()
        start = int(item.get("start_minute") or 0)
        end = int(item.get("end_minute") or 0)
        # 剩余时间按同一个 24h 环归一：跨午夜条目（end>1440）在次日凌晨（如 06:50）也能
        # 得到真实的「约还剩 N 分钟」，不再是 1830 这类帧外大数（裸 end - cur 的旧缺陷）
        remain = ring_minutes_ahead(end, minutes_of_now(now))
        line = f"{item.get('name', '???')}"
        if item.get("description"):
            line += f"（{item['description']}）"
        line += f" [{format_day_minute(start)}-{format_day_minute(end)}]"
        if 0 < remain <= 15:
            line += f"，约还剩 {remain} 分钟"
        energy = describe_energy(get_energy_level(now.hour))
        return f"{line}｜精神状态：{energy}"

    def format_day_summary(self, date: str) -> str:
        """全天日程逐条摘要（时间窗同样经 ``format_day_minute`` 渲染，跨午夜标「次日」）。"""
        items = self._db.get_day_schedule(date)
        if not items:
            return f"{date}：今天还没有安排日程"
        lines = [f"{date} 的日程（共 {len(items)} 项）："]
        for idx, item in enumerate(items, 1):
            start, end = int(item.get("start_minute") or 0), int(item.get("end_minute") or 0)
            lines.append(f"{idx}. [{format_day_minute(start)}-{format_day_minute(end)}] "
                         f"{item.get('name', '???')}｜{item.get('goal_type', '')}")
            if item.get("description"):
                lines.append(f"   └ {item['description']}")
        return "\n".join(lines)

    def next_day_preview(self, date: str, days: int = 1) -> List[Tuple[str, str]]:
        """明天/未来几天的首个活动预览（注入用，骨架版只看明天第一条）。"""
        result = []
        base = datetime.strptime(date, "%Y-%m-%d")
        for offset in range(1, days + 1):
            d = (base + timedelta(days=offset)).strftime("%Y-%m-%d")
            items = self._db.get_day_schedule(d)
            if items:
                first = items[0]
                start = int(first.get("start_minute") or 0)
                result.append((d, f"{format_day_minute(start)} {first.get('name', '')}"))
        return result
