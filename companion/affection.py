"""好感度管理（来源：虚拟恋人插件 affection_manager）。

JSON 持久化（data_dir/affection_memory.json），_dirty 标记 + flush() 强写。
记录：好感档位、每日发言数、早晚安/想念标记、用户最后发言时间、自己最后发言时间，
以及用户习惯字典 habits（memory_manager 的底层存储，承载 set_habit/get_habit 等读写）。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from core.storage import load_json, save_json_atomic

# 各档位备注（注入用短描述）
LEVEL_LABELS = {0: "熟悉", 1: "亲密", 2: "热恋"}

# 各档位完整描述（planner 注入用；来源：源插件 constants.AFFECTION_DESCRIPTIONS）
LEVEL_DESCRIPTIONS = {
    0: "初识阶段，语气温柔但有分寸感，保持适当距离",
    1: "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
    2: "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
}


class AffectionManager:
    def __init__(self, data_dir: Path, default_level: int = 0) -> None:
        self._path = Path(data_dir) / "affection_memory.json"
        self._dirty = False
        self._last_flush = 0.0
        state = load_json(self._path, {}) or {}
        self._state: dict[str, Any] = {
            "affection_level": int(state.get("affection_level", default_level) or 0),
            "habits": dict(state.get("habits", {}) or {}),
            "today_speak_count": float(state.get("today_speak_count", 0.0) or 0.0),
            "today_date": str(state.get("today_date", "") or ""),
            "morning_sent_today": bool(state.get("morning_sent_today", False)),
            "night_sent_today": bool(state.get("night_sent_today", False)),
            "miss_sent_today": bool(state.get("miss_sent_today", False)),
            "last_user_msg_time": state.get("last_user_msg_time"),
            "last_speak_time": state.get("last_speak_time"),
        }

    # ---- 读 ----
    def level(self) -> int:
        return int(self._state["affection_level"])

    def level_label(self) -> str:
        return LEVEL_LABELS.get(self.level(), "熟悉")

    def level_description(self) -> str:
        """当前档位的完整描述（planner 注入用）。"""
        return LEVEL_DESCRIPTIONS.get(self.level(), LEVEL_DESCRIPTIONS[0])

    # ---- 用户习惯（memory_manager 的底层存储）----
    def habits(self) -> dict[str, Any]:
        """获取用户习惯字典。"""
        return dict(self._state.get("habits", {}) or {})

    def set_habit(self, key: str, value: Any) -> None:
        """设置一条用户习惯。"""
        habits = dict(self._state.get("habits", {}) or {})
        habits[str(key)] = value
        self._state["habits"] = habits
        self._mark_dirty()

    def get_habit(self, key: str, default: Any = None) -> Any:
        """获取指定习惯；不存在返回 default。"""
        return self.habits().get(str(key), default)

    def all_habits(self) -> dict[str, Any]:
        """获取所有习惯（同 habits()，别名保持与源插件 MemoryManager 对齐）。"""
        return self.habits()

    def build_personal_context(self) -> str:
        """构造个性化上下文文案（如「用户习惯：wednesday overtime → 周三加班」）。"""
        habits = self.habits()
        if not habits:
            return ""
        parts = [f"用户习惯：{str(k).replace('_', ' ')} → {v}" for k, v in habits.items()]
        return "。".join(parts)

    def today_speak_count(self) -> float:
        return float(self._state["today_speak_count"])

    def today_date(self) -> str:
        return str(self._state["today_date"])

    def morning_sent(self) -> bool:
        return bool(self._state["morning_sent_today"])

    def night_sent(self) -> bool:
        return bool(self._state["night_sent_today"])

    def miss_sent_today(self) -> bool:
        return bool(self._state["miss_sent_today"])

    def _parse_dt(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def last_user_msg_time(self) -> Optional[datetime]:
        return self._parse_dt(self._state["last_user_msg_time"])

    def last_speak_time(self) -> Optional[datetime]:
        return self._parse_dt(self._state["last_speak_time"])

    # ---- 写 ----
    def update_level(self, level: int) -> None:
        self._state["affection_level"] = max(0, min(2, int(level)))
        self._mark_dirty()

    def increment_speak(self, amount: float = 1.0) -> None:
        self._state["today_speak_count"] = float(self._state["today_speak_count"]) + amount
        self._state["last_speak_time"] = datetime.now().isoformat()
        self._mark_dirty()

    def set_morning_sent(self) -> None:
        self._state["morning_sent_today"] = True
        self._mark_dirty()

    def set_night_sent(self) -> None:
        self._state["night_sent_today"] = True
        self._mark_dirty()

    def set_miss_sent(self) -> None:
        self._state["miss_sent_today"] = True
        self._mark_dirty()

    def update_last_user_msg_time(self, dt: Optional[datetime] = None) -> None:
        self._state["last_user_msg_time"] = (dt or datetime.now()).isoformat()
        self._mark_dirty()

    def reset_daily(self, date: str) -> None:
        """日切：清每日标记与计数；保留 last_speak_time（跨午夜间隔约束仍生效）。"""
        self._state["today_date"] = date
        self._state["today_speak_count"] = 0.0
        self._state["morning_sent_today"] = False
        self._state["night_sent_today"] = False
        self._state["miss_sent_today"] = False
        self._mark_dirty()

    def _mark_dirty(self) -> None:
        self._dirty = True
        now = time.monotonic()
        if now - self._last_flush > 1.0:  # 1 秒节流写盘
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        if save_json_atomic(self._path, self._state):
            self._dirty = False
        self._last_flush = time.monotonic()

    def set_level_labels(self, labels: dict) -> None:
        """注入关系相关的档位名称（companion.relation.RelationProfile）。"""
        self._level_labels = dict(labels or {})

    def status_text(self) -> str:
        labels = getattr(self, "_level_labels", None) or LEVEL_LABELS
        label = labels.get(self.level(), labels.get(0, "熟悉"))
        return (
            f"好感度：{label}（{self.level()}）｜今日主动发言：{self.today_speak_count():g} 次"
            f"｜早安{'已' if self.morning_sent() else '未'}发｜晚安{'已' if self.night_sent() else '未'}发"
        )
