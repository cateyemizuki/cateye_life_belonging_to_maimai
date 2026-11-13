# -*- coding: utf-8 -*-
"""初始化清单（随仓库上传的唯一真源）。

职责
====
- :data:`INIT_FIELDS`：可初始化字段（顺序即依赖顺序，逐字段 init/校验/登记）。
- :data:`VERSION_REINIT`：版本 → 该版本升级时需重新初始化的字段；跨多版本升级
  取 ``(旧版本, 当前版本]`` 区间并集（方案 §五.2 / §八.4）。
- :data:`VALIDATORS`：每字段独立校验器（init 成功 ≠ 完成；校验通过才写 true，
  防「假完成」，方案 §五.1）。校验器返回 :class:`ValidationResult`，失败时逐条
  给出中文原因（驱动层落入 ``init_registry.json`` 的 ``last_error`` 并透出 status）。

边界
====
本模块只依赖 stdlib；校验器只读 ``data_dir`` / 阶段结果 / 配置开关，不 import
业务服务、不发消息、不写盘。``life_state`` 的规范键集合经惰性 import 业务
默认文档取得（tools 层编排允许），失败时保守返回空键集而不是猜键集。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

#: 可初始化字段（顺序 = 依赖顺序 = STAGE_ORDER）
INIT_FIELDS: Tuple[str, ...] = (
    "emotion",
    "events",
    "state_sleep",
    "frequency",
    "video_interest",
    "companion_disposition",
    "activity_preferences",
)

#: 版本 → 该版本升级时需重新初始化的字段（累积；跨版本取区间并集）。
#: 当前版本 3.17.35 为自动初始化系统上线批：无字段语义变更，显式登记空表，
#: 便于后续版本按同一格式追加。
VERSION_REINIT: Dict[str, Tuple[str, ...]] = {
    "3.17.34": ("activity_preferences",),
    "3.17.35": (),
}

#: 登记文件（运行时生成于 ``<data_dir>/init_registry.json``）
REGISTRY_FILE_NAME = "init_registry.json"
REGISTRY_SCHEMA_VERSION = 1

#: 连续失败退避窗口（秒）：attempts<3 不限制；>=3 依次 1h → 6h → 24h 封顶。
BACKOFF_WINDOWS_SECONDS: Tuple[int, ...] = (3600, 6 * 3600, 24 * 3600)
BACKOFF_ATTEMPT_THRESHOLD = 3

# ----------------------------------------------------------------------
# 版本比较 / 重 init 区间并集
# ----------------------------------------------------------------------
def version_key(text: Any) -> Tuple[int, ...]:
    """把 ``3.17.35`` 之类的版本串转成可比较元组；无法解析返回空元组。"""
    parts = []
    for chunk in str(text or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits:
            parts.append(int(digits))
        elif parts:
            break
    return tuple(parts)


def reinit_fields_for_upgrade(from_version: Any, to_version: Any) -> Set[str]:
    """返回 ``(from_version, to_version]`` 区间内各版本 REINIT 字段的并集。

    - 旧版本缺失/不可解析/不低于当前版本 → 空集（首次安装由「全 false」驱动）。
    - 只返回 ``INIT_FIELDS`` 内登记的字段，未知字段忽略。
    """
    old = version_key(from_version)
    new = version_key(to_version)
    if not old or not new or old >= new:
        return set()
    fields: Set[str] = set()
    for version, names in VERSION_REINIT.items():
        key = version_key(version)
        if not key:
            continue
        if old < key <= new:
            fields.update(str(name) for name in names)
    return fields & set(INIT_FIELDS)


def backoff_seconds(attempts: int) -> int:
    """attempts>=3 时的退避窗口（秒）；未达阈值返回 0（每次加载正常重试）。"""
    try:
        count = int(attempts)
    except (TypeError, ValueError):
        return 0
    if count < BACKOFF_ATTEMPT_THRESHOLD:
        return 0
    index = min(count - BACKOFF_ATTEMPT_THRESHOLD, len(BACKOFF_WINDOWS_SECONDS) - 1)
    return int(BACKOFF_WINDOWS_SECONDS[index])


# ----------------------------------------------------------------------
# 校验上下文
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class ValidationContext:
    """校验器只读上下文（不持有写口/不触发业务副作用）。"""

    data_dir: Path
    stage: Any = None                       # StageResult（duck-typed：status/ok/details）
    config: Any = None
    plugin_root: Optional[Path] = None
    personality: str = ""
    plugin_version: str = ""
    now: Optional[datetime] = None
    extra: Mapping[str, Any] = field(default_factory=dict)


#: 单次校验最多收集的失败原因条数（坏目录/超大事件库不撑爆 registry 与状态文本）。
MAX_VALIDATION_ERRORS = 8


@dataclass(frozen=True)
class ValidationResult:
    """独立校验结论：``ok`` + 逐条中文失败原因。

    - ``ok=False`` 时 :attr:`errors` 至少一条，逐条对应具体失败点（供
      ``InitRegistry.mark_false`` 落盘、``FieldOutcome.errors`` 与 status 展示）；
    - ``ok=True`` 时 :attr:`errors` 为空（无需初始化/校验通过都不是错误）。

    ``bool(result)`` 等价于 ``result.ok``：兼容旧式「校验器当 bool 用」的注入口，
    新代码一律读 ``.ok`` / ``.errors``。
    """

    ok: bool
    errors: Tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ok)


def _validation_result(errors: Sequence[str]) -> ValidationResult:
    """收敛失败原因：去空、保序、超上限截断（附剩余条数说明）。"""
    items = tuple(str(item).strip() for item in errors if str(item).strip())
    if len(items) > MAX_VALIDATION_ERRORS:
        hidden = len(items) - MAX_VALIDATION_ERRORS
        items = items[:MAX_VALIDATION_ERRORS] + (f"另有 {hidden} 条失败原因未列出",)
    return ValidationResult(ok=not items, errors=items)


# ----------------------------------------------------------------------
# 通用读取
# ----------------------------------------------------------------------
def _load_json(path: Optional[Path]) -> Any:
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _mapping(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _sequence(value: Any) -> Optional[list]:
    return value if isinstance(value, list) else None


def _number(value: Any) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value)))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _stage_ok(ctx: ValidationContext) -> bool:
    return bool(getattr(ctx.stage, "ok", False))


def _details(ctx: ValidationContext) -> Mapping[str, Any]:
    details = getattr(ctx.stage, "details", None)
    return details if isinstance(details, Mapping) else {}


def _config_value(config: Any, section: str, key: str, default: Any = None) -> Any:
    """兼容 dataclass/section 对象与 dict 的只读配置取值。"""
    if config is None:
        return default
    if isinstance(config, Mapping):
        block = config.get(section)
    else:
        block = getattr(config, section, None)
    if block is None:
        return default
    if isinstance(block, Mapping):
        return block.get(key, default)
    return getattr(block, key, default)


def _plugin_enabled(ctx: ValidationContext) -> bool:
    return bool(_config_value(ctx.config, "plugin", "enabled", True))


def _video_enabled(ctx: ValidationContext) -> bool:
    return bool(_plugin_enabled(ctx)
                and _config_value(ctx.config, "video_watch", "enabled", False))


def _schedule_enabled(ctx: ValidationContext) -> bool:
    return bool(_plugin_enabled(ctx)
                and _config_value(ctx.config, "schedule", "enabled", True))


def _persona_matches(stored: Any, personality: str) -> bool:
    """persona 指纹匹配：存储摘要 == 当前人设前 500 字（与 referee 落盘同口径）。

    - 当前人设不可得时只要求摘要非空（不伪造匹配）。
    - 人设可得时摘要必须逐字等于前缀；空摘要视为不匹配（防旧默认值冒充）。
    """
    digest = _text(stored)
    current = str(personality or "")
    if current:
        return bool(digest) and digest == current[:500]
    return bool(digest)


# ----------------------------------------------------------------------
# 各字段校验器（返回 ValidationResult；异常一律由驱动层降级 false，不抛出）
# ----------------------------------------------------------------------
#: 情绪三轴（emotion params.to_dict 口径；2026-11-13 修正旧 v/a/d 误用）
_EMOTION_AXES: Tuple[str, ...] = ("valence", "arousal", "dominance")


def _vector_errors(label: str, value: Any) -> List[str]:
    """情绪向量（anchor/offset）逐轴检查；返回具体缺失/非数原因。"""
    vector = _mapping(value)
    if vector is None:
        return [f"{label} 缺失或不是对象"]
    return [f"{label}.{axis} 缺失/非数"
            for axis in _EMOTION_AXES if not _number(vector.get(axis))]


def _stage_status(ctx: ValidationContext) -> str:
    return str(getattr(ctx.stage, "status", "") or "未返回")


def validate_emotion(ctx: ValidationContext) -> ValidationResult:
    """情绪：persona 指纹匹配 + 参数非空且 initialized=true。"""
    doc = _load_json(ctx.data_dir / "emotion_config.json")
    root = _mapping(doc)
    if root is None:
        return _validation_result(["emotion_config.json 缺失或不是合法 JSON"])
    errors: List[str] = []
    params = _mapping(root.get("params"))
    if params is None:
        errors.append("params 缺失或不是对象")
    else:
        if params.get("initialized") is not True:
            errors.append("params.initialized 非 true")
        errors.extend(_vector_errors("params.anchor", params.get("anchor")))
        errors.extend(_vector_errors("params.offset", params.get("offset")))
        for key in ("positive_gain", "negative_decay"):
            if not _number(params.get(key)):
                errors.append(f"params.{key} 缺失/非数")
        if not _persona_matches(params.get("persona_digest"), ctx.personality):
            if not _text(params.get("persona_digest")):
                errors.append("persona_digest 缺失（需重 init）")
            elif str(ctx.personality or ""):
                errors.append("persona_digest 与当前人设不匹配（人设已变更，需重 init）")
            else:
                errors.append("persona_digest 为空且当前人设不可得（需重 init）")
    return _validation_result(errors)


def validate_events(ctx: ValidationContext) -> ValidationResult:
    """事件库：目录非空 + 事件条目 schema 合法（mode=off 视为无需初始化）。"""
    mode = _text(_config_value(ctx.config, "event_catalog", "mode", "off")).lower()
    if mode == "off":
        return _validation_result([])
    details = _details(ctx)
    raw_path = details.get("catalog_path")
    if raw_path:
        path = Path(str(raw_path))
    elif ctx.plugin_root is not None:
        path = ctx.plugin_root / "mappings" / "generated_events.json"
    else:
        path = ctx.data_dir / "generated_events.json"
    doc = _mapping(_load_json(path))
    if doc is None:
        return _validation_result([f"事件目录缺失或不是合法 JSON：{path}"])
    events = _sequence(doc.get("events"))
    if not events:
        return _validation_result([f"事件目录为空（events 缺失或为空）：{path}"])
    errors: List[str] = []
    seen_ids: Set[str] = set()
    for index, event in enumerate(events, start=1):
        item = _mapping(event)
        if item is None:
            errors.append(f"第 {index} 个事件不是对象")
            continue
        event_id = _text(item.get("event_id"))
        if event_id:
            if event_id in seen_ids:
                errors.append(f"event_id 重复：{event_id}（第 {index} 个事件）")
            else:
                seen_ids.add(event_id)
        for key in ("event_id", "name", "goal_type"):
            if not _text(item.get(key)):
                errors.append(f"第 {index} 个事件 {key} 缺失")
        if (not isinstance(item.get("min_minutes"), int)
                or not isinstance(item.get("max_minutes"), int)):
            errors.append(f"第 {index} 个事件 min_minutes/max_minutes 非整数")
    return _validation_result(errors)


def _life_state_default_keys() -> Tuple[str, ...]:
    """规范状态文档键集合（唯一真源 planning.life_state）；不可得返回空。"""
    try:
        from planning.life_state import default_state_document
        document = default_state_document()
        return tuple(str(key) for key in document.keys())
    except Exception:
        return ()


def _sqlite_has_table(path: Path, table: str) -> bool:
    conn = None
    try:
        if not path.exists():
            return False
        conn = sqlite3.connect(str(path))
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        return row is not None
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def validate_state_sleep(ctx: ValidationContext) -> ValidationResult:
    """状态/睡眠：life_state.json 存在 + 规范键齐全 + sleep_sessions 表存在。"""
    details = _details(ctx)
    raw_path = details.get("state_file")
    path = Path(str(raw_path)) if raw_path else ctx.data_dir / "life_state.json"
    errors: List[str] = []
    doc = _mapping(_load_json(path))
    if doc is None:
        errors.append(f"状态文档缺失或不是合法 JSON：{path}")
    else:
        required = _life_state_default_keys()
        if not required:
            errors.append("无法读取规范状态键集合（planning.life_state.default_state_document 不可用）")
        else:
            missing = [key for key in required if key not in doc]
            if missing:
                errors.append("life_state.json 缺少规范键：" + "、".join(missing))
        # 版本字段存在时必须是当前版本（未知未来版本由准备阶段拒绝重建）。
        if "version" in doc and not isinstance(doc.get("version"), int):
            errors.append("life_state.version 非整数（不是合法状态文档）")
    raw_db = details.get("db_path")
    db_path = Path(str(raw_db)) if raw_db else ctx.data_dir / "schedule.db"
    if not _sqlite_has_table(db_path, "sleep_sessions"):
        errors.append(f"schedule.db 缺少 sleep_sessions 表：{db_path}")
    return _validation_result(errors)


def validate_frequency(ctx: ValidationContext) -> ValidationResult:
    """目标频率：开关关/日程停用=无需初始化；否则校准成功且真实下发 applied。"""
    if not _schedule_enabled(ctx):
        return _validation_result([])
    always_one = _config_value(
        ctx.config, "planning_proactive", "target_frequency_always_one", True)
    if not bool(always_one):
        return _validation_result([])
    if not _stage_ok(ctx):
        return _validation_result([f"频率校准阶段未成功（status={_stage_status(ctx)}）"])
    details = _details(ctx)
    if details.get("applied") is True:
        return _validation_result([])
    # 兼容注入式校准器：仅返回 True/字符串时无 applied 键，按阶段成功认定。
    if "applied" not in details and "target_resolved" not in details:
        return _validation_result([])
    return _validation_result(["目标频率未真实下发（details.applied 非 true）"])


def validate_video_interest(ctx: ValidationContext) -> ValidationResult:
    """视频兴趣：模块关闭=无需初始化；开启=兴趣文档有关键词且带有效时间戳。"""
    if not _video_enabled(ctx):
        return _validation_result([])
    if not _stage_ok(ctx):
        return _validation_result([f"视频兴趣阶段未成功（status={_stage_status(ctx)}）"])
    doc = _mapping(_load_json(ctx.data_dir / "video_watch" / "video_interest.json"))
    if doc is None:
        return _validation_result(["video_interest.json 缺失或不是合法 JSON"])
    errors: List[str] = []
    keywords = _sequence(doc.get("keywords"))
    if not keywords or not all(_text(word) for word in keywords):
        errors.append("keywords 缺失/为空或含空词")
    if not (_number(doc.get("obtained_at")) and float(doc.get("obtained_at") or 0) > 0):
        errors.append("obtained_at 缺失或非正数")
    return _validation_result(errors)


def validate_companion_disposition(ctx: ValidationContext) -> ValidationResult:
    """联系倾向：initiative_state.json 的 profile 合法且与人设指纹匹配。"""
    doc = _mapping(_load_json(ctx.data_dir / "initiative_state.json"))
    if doc is None:
        return _validation_result(["initiative_state.json 缺失或不是合法 JSON"])
    errors: List[str] = []
    if doc.get("version") != 1:
        errors.append(f"version 非 1（实际 {doc.get('version')!r}）")
    profile = _mapping(doc.get("profile"))
    if profile is None:
        errors.append("profile 缺失或不是对象")
    else:
        anchor = profile.get("anchor_hours")
        if not _number(anchor) or not (2 <= float(anchor) <= 24):
            errors.append("profile.anchor_hours 缺失/非数或不在 2-24")
        loneliness = profile.get("loneliness")
        if not _number(loneliness) or not (0 <= float(loneliness) <= 1):
            errors.append("profile.loneliness 缺失/非数或不在 0-1")
        digest = _text(profile.get("persona_hash"))
        current = str(ctx.personality or "")
        if not digest:
            errors.append("profile.persona_hash 缺失（需重 init）")
        elif current and digest != hashlib.sha256(current.encode("utf-8")).hexdigest():
            errors.append("profile.persona_hash 与当前人设不匹配（人设已变更，需重 init）")
    return _validation_result(errors)


def validate_activity_preferences(ctx: ValidationContext) -> ValidationResult:
    """日程偏好：开关关=无需初始化；开启=缓存 schema 合法且有人设指纹。"""
    if not _schedule_enabled(ctx):
        return _validation_result([])
    if not _stage_ok(ctx):
        return _validation_result([f"日程偏好阶段未成功（status={_stage_status(ctx)}）"])
    doc = _mapping(_load_json(ctx.data_dir / "activity_preferences.json"))
    if doc is None:
        return _validation_result(["activity_preferences.json 缺失或不是合法 JSON"])
    errors: List[str] = []
    if doc.get("schema_version") != 1:
        errors.append(f"schema_version 非 1（实际 {doc.get('schema_version')!r}）")
    digest = _text(doc.get("persona_hash"))
    current = str(ctx.personality or "")
    if not digest:
        errors.append("persona_hash 缺失（需重 init）")
    elif current and digest != hashlib.sha256(current.encode("utf-8")).hexdigest():
        errors.append("persona_hash 与当前人设不匹配（人设已变更，需重 init）")
    activities = _mapping(doc.get("activities")) or {}
    traits = _mapping(doc.get("traits")) or {}
    pending = _mapping(doc.get("pending")) or {}
    has_traits = bool(traits) and all(_number(traits.get(axis)) for axis in (
        "leisure_recovery", "physical_activity", "focused_activity",
        "social", "novelty", "regularity"))
    has_pending = bool(pending.get("count")) or bool(pending.get("traits"))
    if traits and not has_traits:
        errors.append("traits 轴值缺失/非数（需要 leisure_recovery/physical_activity/"
                      "focused_activity/social/novelty/regularity）")
    if not (bool(activities) or has_traits or has_pending):
        errors.append("activities/traits/pending 均为空（无可用日程偏好）")
    return _validation_result(errors)


#: 字段 → 独立校验器（init 后调用；``ValidationResult.ok`` 通过才标记 initialized=true）
VALIDATORS: Dict[str, Callable[[ValidationContext], ValidationResult]] = {
    "emotion": validate_emotion,
    "events": validate_events,
    "state_sleep": validate_state_sleep,
    "frequency": validate_frequency,
    "video_interest": validate_video_interest,
    "companion_disposition": validate_companion_disposition,
    "activity_preferences": validate_activity_preferences,
}


__all__ = [
    "INIT_FIELDS", "VERSION_REINIT", "REGISTRY_FILE_NAME",
    "REGISTRY_SCHEMA_VERSION", "BACKOFF_WINDOWS_SECONDS",
    "BACKOFF_ATTEMPT_THRESHOLD", "ValidationContext", "ValidationResult",
    "MAX_VALIDATION_ERRORS", "VALIDATORS",
    "version_key", "reinit_fields_for_upgrade", "backoff_seconds",
]
