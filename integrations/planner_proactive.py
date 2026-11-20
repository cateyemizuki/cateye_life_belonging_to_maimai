"""Dispatch one typed proactive planner task through the host."""
from __future__ import annotations

from typing import Any

_CATEGORIES = {
    "morning_greeting": "greeting",
    "noon_greeting": "greeting",
    "night_greeting": "greeting",
    "greeting": "greeting",
    "video_share": "video_share",
    "video_share_debug": "video_share",
    "video_share_comment": "video_share",
    "video_completion": "video_completion",
    "miss": "missing",
    "missing": "missing",
    "surf_chitchat": "browse_qq",
    "browse_qq": "browse_qq",
    "patrol_chat": "patrol_chat",
    "qzone_comment": "qzone_comment",
}
OPTIONAL_KINDS = {"missing": "miss", "patrol_chat": "patrol_chat",
                  "optional_contact": "surf_chitchat"}


def proactive_category(kind: Any) -> str:
    """Map a display/legacy kind to its independent business category."""
    return _CATEGORIES.get(str(kind or ""), "other")


async def dispatch_proactive(
    plugin: Any, *, stream_id: str, kind: str, intent: str, reason: str = "",
    priority: str = "", key: str = "", metadata: dict | None = None,
    miss_stage: int = 0, trigger: Any = None,
    optional_key: str = "", optional_intent: str = "",
    video_failed: bool | None = None,
) -> dict:
    """Register once before enqueue, with an exact marker the host carries in context.

    Transport errors keep the reservation until it expires. A new attempt could
    otherwise send a second task after an accepted request lost its receipt.
    """
    sid = str(stream_id or "").strip()
    if not sid or not str(intent or "").strip():
        return {"success": False, "error": "invalid_proactive_task"}
    if trigger is None:
        host = getattr(getattr(plugin, "ctx", None), "maisaka", None)
        trigger = getattr(getattr(host, "proactive", None), "trigger", None)
        if not callable(trigger):
            trigger = getattr(host, "trigger_proactive", None)
    if not callable(trigger):
        return {"success": False, "error": "proactive_trigger_unavailable"}

    getter = getattr(plugin, "_mai_proactive_turns", None)
    registry = getter() if callable(getter) else None
    turn_id = ""
    outcome = {"video_failed": video_failed} if kind == "video_completion" and isinstance(video_failed, bool) else {}
    if registry is not None:
        turn_id = registry.register(sid, kind, reason, miss_stage=miss_stage, key=key,
                                    optional_key=optional_key, optional_intent=optional_intent, **outcome)
        if not turn_id:
            return {"success": False, "duplicate": True, "error": "proactive_category_duplicate"}

    payload = {k: v for k, v in (metadata or {}).items()
               if k not in ("lbm_turn_id", "lbm_proactive_kind", "lbm_optional_key", "video_failed")}
    payload.update(outcome)
    payload["lbm_proactive_kind"] = proactive_category(kind)
    if turn_id:
        payload["lbm_turn_id"] = turn_id
    if optional_key:
        payload["lbm_optional_key"] = optional_key
    params: dict[str, Any] = {"stream_id": sid, "intent": intent, "reason": reason, "metadata": payload}
    if priority and priority != "normal":
        params["priority"] = priority
    reply = await trigger(**params)
    if reply is True or reply is False:
        result = {"success": reply}
    elif isinstance(reply, dict):
        result = dict(reply)
    elif getattr(reply, "success", None) is True:
        result = {"success": True}
    else:
        result = {"status": "unknown"}

    rejected = any(result.get(field) is False for field in ("success", "queued", "enqueued"))
    if rejected:
        result["success"] = False
    if registry is not None:
        if rejected:
            registry.discard(turn_id)
        elif result.get("success") is True:
            registry.bind_host_task(turn_id, result.get("task_id"))
        result["lbm_turn_id"] = turn_id
    return result
