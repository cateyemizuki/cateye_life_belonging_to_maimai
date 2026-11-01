"""配置分组与内部字段退役的纯内存迁移，不读写配置文件。"""
from __future__ import annotations
from typing import Any

HISTORY_DISPLAY_KEYS = ("history_changes_limit", "history_state_epsilon", "history_debt_epsilon")

def migrate_display_config(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    out = dict(value)
    old = out.get("maimai_interaction")
    if isinstance(old, dict):
        old = dict(old)
        display = dict(out.get("dashboard_display") or {})
        for key in HISTORY_DISPLAY_KEYS:
            if key in old:
                display.setdefault(key, old.pop(key))
        out["maimai_interaction"] = old
        if display:
            out["dashboard_display"] = display
    return out
