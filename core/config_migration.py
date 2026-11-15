"""配置分组与内部字段退役的纯内存迁移，不读写配置文件。"""
from __future__ import annotations
from typing import Any, Dict, Tuple

HISTORY_DISPLAY_KEYS = ("history_changes_limit", "history_state_epsilon", "history_debt_epsilon")

# Public configuration for the single current schedule / settlement path.
# 3.18.0（2026-11-15 用户裁决 schema 改造批）：spine_use_multi_round / spine_generate_at
# 的目标键（intention_multi_round / generate_at）本批一并退役——始终多轮、生成时刻
# 不再是用户配置项，两条旧改名对降级为纯 pop（登记进 RELEASE_RETIRED["schedule"]）。
RELEASE_RENAMES = {
    "schedule": {
        "spine_min_intentions": "min_intentions",
        "spine_max_intentions": "max_intentions",
        "spine_max_described_intents": "max_described_intents",
    },
    "life_state": {"settlement_v2_params_json": "settlement_params_json"},
}
# 3.18.0 退役登记（用户裁决：移除项一律纯 pop，行为成为唯一路径；各 section 以
# 常量只读 property 桥接既有消费点，见各 config.py 类尾注释）：
# - patrol：想念扣除睡眠/阶梯想念/预算弹性三开关 + 主动回复目标处理（两档行为早已相同）；
# - schedule：多轮生成开关（始终多轮）、日意图生成时刻（内部固定）、事件库模式
#   （始终自动更新）、每池生成上限（数量权威 = 六配额键）、生成周期上下限
#   （合并为 event_catalog_refresh_cycle，值搬运见 migrate_event_catalog_cycle，
#   本处 pop 只是兜底清扫——搬运先于 pop 发生）；
# - life_state：睡眠债弹性还债 / 好感折减 / 目标私聊回充三开关；
# - emotion：回复后评估兼容开关（evaluate_mode 承担）/ 清醒回归人设锚点 / 评估配额分区；
# - affection：早晚安好感事件；prompts：两条死键 + 状态潜意识化开关；
# - video_watch：真实观看兴趣反馈；admin：日志三键（记录恒开、清理定死 30 天）。
RELEASE_RETIRED = {
    "patrol": ("miss_silence_hours", "miss_llm_check_enabled", "min_trigger_interval_minutes",
               "user_cooldown_minutes", "nap_quiet_enabled", "miss_hours_min", "miss_hours_max",
               "proactive_reply_target_mode", "miss_sleep_excluded",
               "miss_stage_escalation_enabled", "budget_elasticity_enabled"),
    "schedule": ("auto_generate_enabled", "auto_generate_after_last_end_minutes", "min_activities",
                 "max_activities", "custom_prompt", "inject_scope",
                 # v3.9.2：旧三时段合并上限退役（纯 pop，不搬运；新配额为
                 # event_catalog_reusable_* / event_catalog_one_shot_* 六键）
                 "event_catalog_period_limit_00_08", "event_catalog_period_limit_08_16",
                 "event_catalog_period_limit_16_24",
                 # 3.18.0：目标键已退役的旧改名对 + 本批移除键
                 "spine_use_multi_round", "spine_generate_at",
                 "intention_multi_round", "generate_at",
                 "event_catalog_mode", "event_catalog_max_generated_events",
                 "event_catalog_min_interval_days", "event_catalog_max_interval_days"),
    "schedule_advanced": ("cross_day_activity", "enable_intent_classification",
                          "enable_inject_optimization", "intent_tuning_enabled", "use_multi_round",
                          "auto_generate_time"),
    "life_state": ("settlement_model", "emotion_modulation_enabled", "dynamic_sleep_enabled",
                   "sleep_debt_repay_v2", "social_battery_affinity_discount",
                   "target_chat_social_recharge"),
    "emotion": ("evaluate_on_reply", "decay_to_persona_anchor", "emotion_quota_partition_enabled"),
    "affection": ("exchange_events_enabled",),
    "prompts": ("companion_activity_reason", "companion_daily_reason", "state_subconscious_style"),
    "video_watch": ("interest_watch_feedback_enabled",),
    "admin": ("llm_log_enabled", "decision_log_enabled", "log_retention_days"),
    "llm": ("usage_retention_hours",),
}
# 3.18.0：事件库键清单（用户可见键 = [schedule].event_catalog_*；服务视图由
# suite_config.CateyeSuiteConfig.event_catalog property 反向组装）。
# mode / min_interval_days / max_interval_days / max_generated_events 退役；
# 新增 refresh_cycle（「小事件池子变化周期」range 字符串，默认 "5-8"）。
EVENT_CATALOG_FIELDS = ("refresh_cycle", "task_name",
                        "reusable_00_08", "reusable_08_16", "reusable_16_24",
                        "one_shot_00_08", "one_shot_08_16", "one_shot_16_24", "no_puzzle")

#: 3.18.0 experimental 转正：五个用户可见开关退役（行为恒开，无值可搬）。
#: group_chat_social_drain_per_reply 是参数不是开关，降级 J 表 life_state 同名键。
EXPERIMENTAL_RETIRED_SWITCHES = frozenset({
    "group_attention_decay_enabled",
    "transition_enabled",
    "social_boundary_enabled",
    "bpp_bridge_enabled",
    "suppress_hunger_mention_enabled",
})

#: 3.18.0 experimental 转正：参数键前缀 → internal_tuning.json 目标段
#: （剥前缀后与 INTERNAL_TUNING_DEFAULTS 同名键对齐；未登记的键丢弃）。
EXPERIMENTAL_PARAM_PREFIXES = {
    "group_attention_": "group_attention",
    "transition_": "transition",
    "social_boundary_": "boundary",
}

#: 「小事件池子变化周期」出厂默认（与 planning.event_catalog_config.DEFAULT_REFRESH_CYCLE
#: 同步；本模块保持纯内存、不 import 业务模块，故用字面量 + 注释锚定）。
EVENT_CATALOG_REFRESH_CYCLE_DEFAULT = "5-8"
#: 旧生成周期出厂默认 7/10：仅在旧键单边缺失时补全 missing 侧，保住旧有效区间。
_CYCLE_FILL_MIN = 7
_CYCLE_FILL_MAX = 10

#: 3.17.36（旧版兼容段）：[qzone].napcat_http_* → [legacy_compat].snowluma_http_*。
#: 概念改名 NapCat HTTP → Snowluma HTTP 且整段移出 [qzone]（跨段 + 改名，值搬运）。
#: 三键出厂默认均为 ""：目标键缺位**或仍等于出厂默认 ""** 视同可搬入
#: （_strip_factory_defaults 同口径——宿主/SDK 先 merge 默认，setdefault 裸用会吞旧值）；
#: 用户在新段的显式非空值优先。旧键搬完即 pop（退役，不再出现在 schema）。
LEGACY_COMPAT_RENAMES = {
    "napcat_http_host": "snowluma_http_host",
    "napcat_http_port": "snowluma_http_port",
    "napcat_http_token": "snowluma_http_token",
}


def migrate_legacy_compat_config(value: Any) -> Any:
    """[qzone].napcat_http_* → [legacy_compat].snowluma_http_*（纯内存、幂等、不改入参）。

    与 :func:`migrate_display_config` 同精神（跨段 setdefault+pop），但带键名改名，
    且目标键等于出厂默认 "" 时视同缺位（见 :data:`LEGACY_COMPAT_RENAMES` 注释）。
    调用点：plugin.normalize_plugin_config 的原始输入与磁盘抢救结果（SDK merge 之前）、
    聚合校验器入口（两分支共经）、:func:`rescue_release_config` 的磁盘抢救对。
    """
    if not isinstance(value, dict):
        return value
    out = dict(value)
    old = out.get("qzone")
    if not isinstance(old, dict):
        return out
    old = dict(old)
    compat = dict(out.get("legacy_compat") or {})
    changed = False
    for old_key, new_key in LEGACY_COMPAT_RENAMES.items():
        if old_key not in old:
            continue
        legacy_value = old.pop(old_key)
        changed = True
        if not compat.get(new_key):  # 缺位或仍为出厂默认 "" → 搬旧值；显式非空值优先
            compat[new_key] = legacy_value
    if changed:
        out["qzone"] = old
    if compat:
        out["legacy_compat"] = compat
    return out


def _cycle_int(value: Any, fallback: int) -> int:
    """旧周期键取值 → 1..365 整数；坏值回退 fallback（与旧 resolve 夹取同口径）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback
    return max(1, min(365, n))


def _format_cycle(low: Any, high: Any) -> str:
    """旧上下限 → "a-b" 字符串。单边缺失按旧出厂默认 7/10 补全（保住旧有效区间）；
    a>b 自动交换；两侧都夹取 1..365（与旧 resolve_config 口径一致）。"""
    lo = _cycle_int(low, _CYCLE_FILL_MIN) if low is not None else _CYCLE_FILL_MIN
    hi = _cycle_int(high, _CYCLE_FILL_MAX) if high is not None else _CYCLE_FILL_MAX
    if lo > hi:
        lo, hi = hi, lo
    return f"{lo}-{hi}"


def _cycle_writable(schedule: Dict[str, Any]) -> bool:
    """目标键缺位/空/仍等于出厂默认 "5-8" 视同可搬入（宿主先 merge 默认，
    与 LEGACY_COMPAT_RENAMES 同精神）；显式非默认值优先。"""
    current = schedule.get("event_catalog_refresh_cycle")
    return current is None or str(current).strip() == ""


def migrate_event_catalog_cycle(value: Any) -> Any:
    """生成周期旧两键 → ``[schedule].event_catalog_refresh_cycle``（值搬运、幂等）。

    3.18.0 用户裁决：min_interval_days + max_interval_days 合并为单个 range 字符串键
    「小事件池子变化周期」（默认 "5-8"）。同时处理两个旧位置：
    ``[schedule].event_catalog_min/max_interval_days`` 与旧 ``[event_catalog]`` 段的
    ``min/max_interval_days``（新位置优先）。旧键搬完即 pop。
    必须在 :func:`migrate_release_config` 的退役 pop 之前调用（本模块内部已保证）。
    """
    if not isinstance(value, dict):
        return value
    out = dict(value)
    had_schedule = isinstance(out.get("schedule"), dict)
    schedule = dict(out["schedule"]) if had_schedule else {}
    changed = False
    lo = schedule.pop("event_catalog_min_interval_days", None)
    hi = schedule.pop("event_catalog_max_interval_days", None)
    if lo is not None or hi is not None:
        changed = True
        if _cycle_writable(schedule):
            schedule["event_catalog_refresh_cycle"] = _format_cycle(lo, hi)
    old = out.get("event_catalog")
    if isinstance(old, dict):
        old = dict(old)
        olo = old.pop("min_interval_days", None)
        ohi = old.pop("max_interval_days", None)
        if olo is not None or ohi is not None:
            changed = True
            if _cycle_writable(schedule):
                schedule["event_catalog_refresh_cycle"] = _format_cycle(olo, ohi)
            out["event_catalog"] = old
    if changed or had_schedule:
        out["schedule"] = schedule
    return out


def migrate_release_config(value: Any) -> Any:
    """Pure, idempotent migration; explicit canonical values win, including false/0."""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    # 3.18.0：周期两键先组装成 refresh_cycle，再走下面的退役 pop（顺序不可换）。
    out = migrate_event_catalog_cycle(out)
    emotion = out.get("emotion")
    if isinstance(emotion, dict) and "evaluate_on_reply" in emotion:
        emotion = dict(emotion)
        if "evaluate_mode" not in emotion:
            emotion["evaluate_mode"] = "episode" if emotion["evaluate_on_reply"] else "off"
        out["emotion"] = emotion
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
        schedule = dict(out.get("schedule") or {})
        for key in EVENT_CATALOG_FIELDS:
            if key in old_events:
                schedule.setdefault("event_catalog_" + key, old_events[key])
        out["schedule"] = schedule
    # 3.18.0 experimental 转正：整段退役。参数键的 J 表捕获由
    # migrate_internal_tuning 完成（调用顺序：先捕获后 pop；plugin.normalize 与
    # 聚合校验器入口都满足）。此处 pop 保证离线 model_validate 路径也不落盘旧段。
    out.pop("experimental", None)
    mascot = out.get("maimai_interaction")
    if isinstance(mascot, dict) and "preset" in mascot:
        mascot = dict(mascot)
        preset = str(mascot["preset"]).strip().lower()
        mascot["preset"] = {"old": "fish", "new": "human"}.get(preset, preset)
        if mascot["preset"] not in ("fish", "human"):
            mascot["preset"] = "human"
        out["maimai_interaction"] = mascot
    return out


def _disk_cycle_source(disk: Dict[str, Any]) -> Any:
    """磁盘旧周期键取值（[schedule] 优先，旧 [event_catalog] 段兜底）；无则 None。"""
    sec = disk.get("schedule")
    if isinstance(sec, dict):
        lo = sec.get("event_catalog_min_interval_days")
        hi = sec.get("event_catalog_max_interval_days")
        if lo is not None or hi is not None:
            return (lo, hi)
    old = disk.get("event_catalog")
    if isinstance(old, dict):
        lo = old.get("min_interval_days")
        hi = old.get("max_interval_days")
        if lo is not None or hi is not None:
            return (lo, hi)
    return None


def rescue_release_config(value: Any, disk: Any) -> Any:
    """Rescue renamed keys before SDK persistence, even with an existing layout marker.

    On a version rebuild the host has already inserted canonical defaults. A key
    absent from the old disk cannot be treated as an explicit canonical setting.
    Raw input containing both names, however, has unambiguous canonical priority.
    """
    if not isinstance(value, dict):
        return value
    out = migrate_legacy_compat_config(migrate_release_config(value))
    if not isinstance(disk, dict):
        return out
    migrated_disk = migrate_legacy_compat_config(migrate_release_config(disk))
    pairs = [(sec, old, sec, new) for sec, mapping in RELEASE_RENAMES.items() for old, new in mapping.items()]
    pairs += [("event_catalog", key, "schedule", "event_catalog_" + key) for key in EVENT_CATALOG_FIELDS]
    # 3.17.36：[qzone].napcat_http_* → [legacy_compat].snowluma_http_* 跨段改名对
    # （磁盘旧键在宿主重建后只存在于磁盘时的抢救通道，与 event_catalog 同口径）。
    pairs += [("qzone", old, "legacy_compat", new) for old, new in LEGACY_COMPAT_RENAMES.items()]
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
    # 3.18.0：生成周期旧两键的磁盘抢救（组装型，非单键复制）：磁盘仍带旧键、
    # 且磁盘与原始输入都没有显式非默认 refresh_cycle 时，用磁盘值组装搬入。
    # migrated_disk 已经过 migrate_event_catalog_cycle，组装结果直接复用。
    disk_cycle = _disk_cycle_source(disk)
    if disk_cycle is not None:
        def _explicit_cycle(sec: Any) -> bool:
            if not isinstance(sec, dict):
                return False
            current = sec.get("event_catalog_refresh_cycle")
            return current is not None and str(current).strip() != ""
        incoming_cycle = (value.get("schedule") or {}).get("event_catalog_refresh_cycle")
        incoming_nondefault = incoming_cycle is not None and str(incoming_cycle).strip() not in ("", EVENT_CATALOG_REFRESH_CYCLE_DEFAULT)
        if not _explicit_cycle(disk.get("schedule")) and not incoming_nondefault:
            disk_sched = migrated_disk.get("schedule")
            if isinstance(disk_sched, dict) and "event_catalog_refresh_cycle" in disk_sched:
                target = dict(out.get("schedule") or {})
                target["event_catalog_refresh_cycle"] = disk_sched["event_catalog_refresh_cycle"]
                out["schedule"] = target
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
    """J 类内部调参键（2026-11-04 配置精简轮；3.18.0 起含 experimental 转正捕获）：
    旧 TOML 键摘出 + 显式 pop。

    纯内存、不读写文件（本模块契约）：从各段 dict 摘出
    :data:`core.internal_tuning.INTERNAL_TUNING_DEFAULTS` 登记的键，返回
    ``(清理后 data, 捕获值 dict[段][键])``。捕获值的落盘由
    ``plugin.normalize_plugin_config`` 在 SDK merge 出厂默认之前经
    ``core.internal_tuning.merge_captured`` 完成（只补用户表缺失键，幂等）；
    聚合校验器入口也调用本函数 pop（两分支共经），保证离线 ``model_validate``
    路径不会把已删键写回 TOML。

    3.18.0 experimental 转正：``[experimental]`` 参数键按前缀剥壳捕获进
    group_attention / transition / boundary 三段（与 J 表同名键对齐），
    group_chat_social_drain_per_reply 捕获进 life_state 同名键；五个开关退役
    （纯 pop，行为恒开）；整段摘除。捕获值与 J 类共用同一落盘通道。

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
    # 在任何默认种子占位之前，一次捕获旧分组里的内部参数。
    # 若先按别的段创建完整表，晚到的旧参数会被「只补缺失键」挡住。
    for old_section, section in {"planning_cleanup": "schedule_advanced",
                                  "inject": "schedule_advanced", "judge": "llm",
                                  "cateye": "patrol", "target": "patrol"}.items():
        old = out.get(old_section)
        if not isinstance(old, dict):
            continue
        cleaned = dict(old)
        for key in INTERNAL_TUNING_DEFAULTS.get(section, {}):
            if key in cleaned:
                captured.setdefault(section, {}).setdefault(key, cleaned.pop(key))
        out[old_section] = cleaned
    # 3.18.0 experimental 转正捕获（开关不搬、参数剥前缀搬、整段 pop）。
    exp = out.get("experimental")
    if isinstance(exp, dict):
        exp_taken: Dict[str, Dict[str, Any]] = {}
        for key, val in exp.items():
            if key in EXPERIMENTAL_RETIRED_SWITCHES:
                continue
            if key == "group_chat_social_drain_per_reply":
                exp_taken.setdefault("life_state", {})[key] = val
                continue
            for prefix, section in EXPERIMENTAL_PARAM_PREFIXES.items():
                if key.startswith(prefix):
                    sub = key[len(prefix):]
                    if sub in INTERNAL_TUNING_DEFAULTS.get(section, {}):
                        exp_taken.setdefault(section, {})[sub] = val
                    break
        for section, keys in exp_taken.items():
            captured.setdefault(section, {}).update(keys)
        out.pop("experimental", None)
    return out, captured
