"""配置分组与内部字段退役的纯内存迁移，不读写配置文件。"""
from __future__ import annotations
from typing import Any, Dict, Tuple

HISTORY_DISPLAY_KEYS = ("history_changes_limit", "history_state_epsilon", "history_debt_epsilon")

# Public configuration for the single current schedule / settlement path.
RELEASE_RENAMES = {
    "schedule": {
        "spine_min_intentions": "min_intentions",
        "spine_max_intentions": "max_intentions",
        "spine_max_described_intents": "max_described_intents",
        "spine_use_multi_round": "intention_multi_round",
        "spine_generate_at": "generate_at",
    },
    "life_state": {"settlement_v2_params_json": "settlement_params_json"},
}
RELEASE_RETIRED = {
    "patrol": ("miss_silence_hours", "miss_llm_check_enabled", "min_trigger_interval_minutes",
               "user_cooldown_minutes", "nap_quiet_enabled", "miss_hours_min", "miss_hours_max"),
    "schedule": ("auto_generate_enabled", "auto_generate_after_last_end_minutes", "min_activities",
                 "max_activities", "custom_prompt", "inject_scope"),
    "schedule_advanced": ("cross_day_activity", "enable_intent_classification",
                          "enable_inject_optimization", "intent_tuning_enabled", "use_multi_round",
                          "auto_generate_time"),
    "life_state": ("settlement_model", "emotion_modulation_enabled", "dynamic_sleep_enabled"),
}
EVENT_CATALOG_FIELDS = ("mode", "min_interval_days", "max_interval_days", "max_generated_events", "task_name",
                        "period_limit_00_08", "period_limit_08_16", "period_limit_16_24")


def migrate_release_config(value: Any) -> Any:
    """Pure, idempotent migration; explicit canonical values win, including false/0."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    for name in RELEASE_RETIRED.keys() | RELEASE_RENAMES.keys():
        raw = out.get(name)
        if not isinstance(raw, dict):
            continue
        sec = dict(raw)
        for old, new in RELEASE_RENAMES.get(name, {}).items():
            if old in sec:
                sec.setdefault(new, sec.pop(old))
        for key in RELEASE_RETIRED.get(name, ()):
            sec.pop(key, None)
        out[name] = sec
    old_events = out.pop("event_catalog", None)
    if isinstance(old_events, dict):
        old_events = dict(old_events)
        if "max_generated_events" not in old_events and "max_events" in old_events:
            old_events["max_generated_events"] = old_events["max_events"]
        schedule = dict(out.get("schedule") or {})
        for key in EVENT_CATALOG_FIELDS:
            if key in old_events:
                schedule.setdefault("event_catalog_" + key, old_events[key])
        out["schedule"] = schedule
    mascot = out.get("maimai_interaction")
    if isinstance(mascot, dict) and "preset" in mascot:
        mascot = dict(mascot)
        preset = str(mascot["preset"]).strip().lower()
        mascot["preset"] = {"old": "fish", "new": "human"}.get(preset, preset)
        if mascot["preset"] not in ("fish", "human"):
            mascot["preset"] = "human"
        out["maimai_interaction"] = mascot
    return out


def rescue_release_config(value: Any, disk: Any) -> Any:
    """Rescue renamed keys before SDK persistence, even with an existing layout marker.

    On a version rebuild the host has already inserted canonical defaults. A key
    absent from the old disk cannot be treated as an explicit canonical setting.
    Raw input containing both names, however, has unambiguous canonical priority.
    """
    if not isinstance(value, dict):
        return value
    out = migrate_release_config(value)
    if not isinstance(disk, dict):
        return out
    migrated_disk = migrate_release_config(disk)
    pairs = [(sec, old, sec, new) for sec, mapping in RELEASE_RENAMES.items() for old, new in mapping.items()]
    pairs += [("event_catalog", key, "schedule", "event_catalog_" + key) for key in EVENT_CATALOG_FIELDS]
    # The oldest event cap alias needs the same pre-merge rescue.
    pairs += [("event_catalog", "max_events", "schedule", "event_catalog_max_generated_events")]
    for old_sec, old_key, new_sec, new_key in pairs:
        src_disk = disk.get(old_sec)
        canonical_disk = disk.get(new_sec)
        src_input = value.get(old_sec)
        canonical_input = value.get(new_sec)
        raw_explicit = (isinstance(src_input, dict) and old_key in src_input
                        and isinstance(canonical_input, dict) and new_key in canonical_input)
        if (isinstance(src_disk, dict) and old_key in src_disk
                and not (isinstance(canonical_disk, dict) and new_key in canonical_disk)
                and not raw_explicit):
            target = dict(out.get(new_sec) or {})
            if new_key in (migrated_disk.get(new_sec) or {}):
                target[new_key] = migrated_disk[new_sec][new_key]
                out[new_sec] = target
    # old/new is a value rename: the old host may have discarded the enum value.
    old_mascot = disk.get("maimai_interaction")
    if isinstance(old_mascot, dict) and old_mascot.get("preset") in ("old", "new"):
        target = dict(out.get("maimai_interaction") or {})
        target["preset"] = migrated_disk["maimai_interaction"]["preset"]
        out["maimai_interaction"] = target
    return out

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


def migrate_internal_tuning(value: Any) -> Tuple[Any, Dict[str, Dict[str, Any]]]:
    """J 类内部调参键（2026-11-04 配置精简轮，40 项）：旧 TOML 键摘出 + 显式 pop。

    纯内存、不读写文件（本模块契约）：从各段 dict 摘出
    :data:`core.internal_tuning.INTERNAL_TUNING_DEFAULTS` 登记的键，返回
    ``(清理后 data, 捕获值 dict[段][键])``。捕获值的落盘由
    ``plugin.normalize_plugin_config`` 在 SDK merge 出厂默认之前经
    ``core.internal_tuning.merge_captured`` 完成（只补用户表缺失键，幂等）；
    聚合校验器入口也调用本函数 pop（两分支共经），保证离线 ``model_validate``
    路径不会把已删键写回 TOML。

    应在 :func:`migrate_display_config` 之后调用（旧 [maimai_interaction] 里的
    history_*_epsilon 先搬进 [dashboard_display]，再被本函数捕获）。
    非 dict 输入原样放行；不修改入参。
    """
    from core.internal_tuning import INTERNAL_TUNING_DEFAULTS

    if not isinstance(value, dict):
        return value, {}
    out = dict(value)
    captured: Dict[str, Dict[str, Any]] = {}
    for section, keys in INTERNAL_TUNING_DEFAULTS.items():
        sec = out.get(section)
        if not isinstance(sec, dict):
            continue
        taken: Dict[str, Any] = {}
        cleaned = dict(sec)
        for key in keys:
            if key in cleaned:
                taken[key] = cleaned.pop(key)
        if taken:
            captured[section] = taken
            out[section] = cleaned
    return out, captured
