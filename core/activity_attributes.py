"""活动模块属性的共享叶协议；仅依赖标准库，不执行模块、不修改状态。"""
from __future__ import annotations

import math
from typing import Any


def _sleep_guard(item: dict, details: dict) -> bool:
    # 兼容旧睡眠标签仅作保守阻止，不把标签当成实际执行证据。
    name = str(item.get("name") or "")
    return (str(item.get("goal_type") or "") == "sleep" or details.get("kind") == "nap"
            or details.get("sleep_engine") is True or details.get("trigger") == "sleep_anchor"
            or any(word in name for word in ("睡觉", "睡眠", "安睡", "午休", "午睡", "小憩", "打盹")))


ATTRIBUTE_KEY = "activity_attributes"
ACTIVITY_ATTRIBUTES = ("watch_video", "browse_qq")
LEGACY_EVENT_ATTRIBUTES = {"watch_bili": "watch_video", "browse_qq": "browse_qq"}


def normalize_activity_attributes(value: Any) -> list[str] | None:
    """未知、混合垃圾、空列表一律 null；只接受规范标识并固定顺序去重。"""
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(item, str) or item not in ACTIVITY_ATTRIBUTES for item in value):
        return None
    return [key for key in ACTIVITY_ATTRIBUTES if key in value]


def activity_attributes_for_episode(episode: Any) -> list[str] | None:
    """显式 null 不回退；仅无字段旧行按内置事件身份兼容，不按名字/goal_type猜测。"""
    if not isinstance(episode, dict):
        return None
    metadata = episode.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    if ATTRIBUTE_KEY in metadata:
        return normalize_activity_attributes(metadata[ATTRIBUTE_KEY])
    event_id = str(metadata.get("event_id") or metadata.get("event_key") or "")
    attribute = LEGACY_EVENT_ATTRIBUTES.get(event_id)
    return [attribute] if attribute else None


def activity_module_guard(item: Any, *, require_clear_idle: bool = False, module: str = "") -> str:
    """空串=未见阻止证据；闲暇顺便行为须明确可中断且当前可响应，不把缺省 .5 当空闲。"""
    if not isinstance(item, dict):
        return "unavailable"
    params = item.get("parameters")
    meta = item.get("metadata")
    details = {**(params if isinstance(params, dict) else {}),
               **(meta if isinstance(meta, dict) else {})}
    status = str(item.get("status") or "").strip()
    if status and status not in ("active", "running"):
        return "inactive"
    if _sleep_guard(item, details):
        return "sleep"
    hardness = str(details.get("hardness") or item.get("hardness") or "").strip().lower()
    commitment_hardness = str(details.get("commitment_hardness") or item.get("commitment_hardness") or "").strip().lower()
    if (str(item.get("source") or "") == "commitment" or hardness == "hard"
            or commitment_hardness == "hard" or details.get("trigger") == "hard_commitment"):
        return "hard_commitment"
    attention = str(item.get("attention_mode") or details.get("attention_mode") or "").strip()
    # A dedicated watch may become less responsive to chat without becoming busy with another task.
    watch_context=(module=="watch_video" and not require_clear_idle
                   and "watch_video" in (activity_attributes_for_episode(item) or []))
    if attention=="later" or (attention=="brief" and not watch_context):
        return "busy"
    if require_clear_idle and attention != "now":
        return "unknown_idle"
    if details.get("busy") is True or item.get("busy") is True:
        return "busy"
    reply_mode = str(details.get("reply_mode") or item.get("reply_mode") or "").strip().lower()
    if reply_mode in ("busy", "later", "silent", "off"):
        return "busy"
    raw = item.get("interruptibility", details.get("interruptibility"))
    try:
        interruptibility = float(raw)
        if isinstance(raw, bool) or not math.isfinite(interruptibility) or not 0 <= interruptibility <= 1:
            return "unknown_interruptibility"
    except (TypeError, ValueError):
        return "unknown_idle" if require_clear_idle else ""
    if interruptibility < .45:
        return "busy"
    if require_clear_idle and interruptibility < .7:
        return "unknown_idle"
    return ""


