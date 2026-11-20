"""Authenticated WebUI editor for the bot and the verified companion's video interests."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

from core.plugin_context import PluginContext
from integrations.video_interest import PERSONA_BASE_WEIGHT, VideoInterestService
from integrations.video_preferences import MAX_PREFERENCES, term_key, topic_weights

PROFILES = ("bot", "companion")
_UNAVAILABLE = {"available": False}


def _profile(plugin, name):
    if name == "companion":
        if not plugin._active_video_preference_target()[0]:
            return None
        return plugin._companion_video_preference_service().editable_profile()
    if name == "bot":
        root = PluginContext(plugin).data_dir
        if root is None:
            return None
        service = VideoInterestService(Path(root) / "video_watch")
        doc = service.load() or {}
        baseline = {term_key(term): doc.get("weights", {}).get(term_key(term), PERSONA_BASE_WEIGHT)
                    for term in doc.get("keywords", [])}
        return ("bot",), baseline, service.manual_interests
    return None


def _view(profile) -> dict:
    if profile is None:
        return dict(_UNAVAILABLE)
    identity, automatic, store = profile
    manual = store.load()
    weights = automatic if manual is None else manual
    mode = "automatic" if manual is None else "manual"
    digest = sha256(json.dumps([identity, mode, list(weights.items())], ensure_ascii=False).encode()).hexdigest()
    return {
        "available": True,
        "mode": mode,
        "revision": digest,
        "minimum": store.lower,
        "maximum": store.upper,
        "minimum_count": store.minimum_count,
        "maximum_count": MAX_PREFERENCES,
        "entries": [{"topic": term, "weight": weight} for term, weight in weights.items()],
    }


def preference_editor_snapshot(plugin) -> dict:
    return {name: _view(_profile(plugin, name)) for name in PROFILES}


def save_preference_edit(plugin, *, profile: str, revision: str, mode: str, entries_json: str) -> dict:
    if profile not in PROFILES or mode not in ("manual", "automatic"):
        return {"ok": False, "message": "编辑目标或保存方式无效。"}
    current = _profile(plugin, profile)
    if current is None:
        return {"ok": False, "message": "当前兴趣档案不可用。"}
    view = _view(current)
    if revision != view["revision"]:
        return {"ok": False, "conflict": True, "message": "档案已更新或陪伴目标已切换，请重新读取。"}
    _, _, store = current
    weights = None
    if mode == "manual":
        weights = _validated_entries(entries_json, store)
        if weights is None:
            return {"ok": False, "message": "请核对题材、条目数和权重范围。题材需唯一，最长 40 字。"}
    if not store.save(weights):
        return {"ok": False, "message": "保存失败，请稍后再试。"}
    return {"ok": True, "profile": _view(_profile(plugin, profile))}


def _validated_entries(text, store) -> dict[str, float] | None:
    if not isinstance(text, str) or len(text) > 4096:
        return None
    try:
        entries = json.loads(text)
    except (RecursionError, ValueError):
        return None
    if not isinstance(entries, list) or not store.minimum_count <= len(entries) <= MAX_PREFERENCES:
        return None
    raw = {}
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"topic", "weight"}:
            return None
        term, weight = entry["topic"], entry["weight"]
        if not isinstance(term, str) or type(weight) not in (int, float) or not store.lower <= weight <= store.upper:
            return None
        key = term_key(term)
        if key in raw:
            return None
        raw[key] = weight
    weights = topic_weights(raw, store.lower, store.upper)
    return weights if len(weights) == len(entries) else None
