"""日程查询：当前活动、未来活动、全天摘要（带 N 分钟窗口缓存）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .schedule_db import ScheduleDB
from .time_utils import describe_energy, get_energy_level
from core.time_utils import minutes_of_now

# sleep 类型条目不算「正在做的事」（仅 include_sleep=True 的调用方可见，如情绪睡眠循环）
_SKIP_TYPES = {"sleep"}


class ScheduleQuery:
    def __init__(self, db: ScheduleDB, cache_minutes: int = 15) -> None:
        self._db = db
        self._cache_minutes = max(1, int(cache_minutes))
        self._cache: Dict[str, Tuple[int, Any]] = {}  # key -> (窗口号, 结果)

    def get_day_schedule(self, date: str) -> List[Dict[str, Any]]:
        return self._db.get_day_schedule(date)

    def find_current_activity(self, date: str, now: Optional[datetime] = None,
                              include_sleep: bool = False) -> Optional[Dict[str, Any]]:
        """返回当前正在进行的日程条目（默认跳过 sleep 类型）；结果按 N 分钟窗口缓存。

        include_sleep=True 时 sleep 条目也参与匹配（情绪睡眠循环用）；
        缓存键区分该开关，避免共享实例上不同调用方互相串用结果。
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
            if int(item.get("start_minute") or 0) <= cur_min < int(item.get("end_minute") or 0):
                current = item
                break
        if len(self._cache) > 64:  # 简单防膨胀
            self._cache.clear()
        self._cache[key] = (window, current or "")
        return current

    def find_next_activities(self, date: str, limit: int = 3, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        now = now or datetime.now()
        cur_min = now.hour * 60 + now.minute
        upcoming = []
        for item in self._db.get_day_schedule(date):
            if item.get("goal_type") in _SKIP_TYPES:
                continue
            if int(item.get("start_minute") or 0) > cur_min:
                upcoming.append(item)
                if len(upcoming) >= limit:
                    break
        return upcoming

    def format_activity_line(self, item: Dict[str, Any], now: Optional[datetime] = None) -> str:
        """当前活动的一行描述（含剩余时间与情绪/能量提示），注入与 Tool 共用。"""
        now = now or datetime.now()
        start = int(item.get("start_minute") or 0)
        end = int(item.get("end_minute") or 0)
        remain = end - minutes_of_now(now)
        line = f"{item.get('name', '???')}"
        if item.get("description"):
            line += f"（{item['description']}）"
        line += f" [{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}]"
        if 0 < remain <= 15:
            line += f"，约还剩 {remain} 分钟"
        energy = describe_energy(get_energy_level(now.hour))
        return f"{line}｜精神状态：{energy}"

    def format_day_summary(self, date: str) -> str:
        items = self._db.get_day_schedule(date)
        if not items:
            return f"{date}：今天还没有安排日程"
        lines = [f"{date} 的日程（共 {len(items)} 项）："]
        for idx, item in enumerate(items, 1):
            start, end = int(item.get("start_minute") or 0), int(item.get("end_minute") or 0)
            lines.append(f"{idx}. [{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}] "
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
                result.append((d, f"{start // 60:02d}:{start % 60:02d} {first.get('name', '')}"))
        return result
