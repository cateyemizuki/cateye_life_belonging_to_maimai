# -*- coding: utf-8 -*-
"""初始化清单（随仓库上传的唯一真源）。

职责
====
- :data:`INIT_FIELDS`：可初始化字段（顺序即依赖顺序，逐字段 init/校验/登记）。
- :data:`VERSION_REINIT`：版本 → 该版本升级时需重新初始化的字段；跨多版本升级
  取 ``(旧版本, 当前版本]`` 区间并集（方案 §五.2 / §八.4）。
- :data:`VALIDATORS`：每字段独立校验器（init 成功 ≠ 完成；校验通过才写 true，
  防「假完成」，方案 §五.1）。

边界
====
本模块只依赖 stdlib；校验器只读 ``data_dir`` / 阶段结果 / 配置开关，不 import
业务服务、不发消息、不写盘。``life_state`` 的规范键集合经惰性 import 业务
默认文档取得（tools 层编排允许），失败时保守返回 False 而不是猜键集。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Set, Tuple

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
# 各字段校验器（返回 bool；异常一律 False，不抛出到驱动层）
# ----------------------------------------------------------------------
def validate_emotion(ctx: ValidationContext) -> bool:
    """情绪：persona 指纹匹配 + 参数非空且 initialized=true。"""
    doc = _load_json(ctx.data_dir / "emotion_config.json")
    root = _mapping(doc)
    if root is None:
        return False
    params = _mapping(root.get("params"))
    if params is None or params.get("initialized") is not True:
        return False
    for vector_name in ("anchor", "offset"):
        vector = _mapping(params.get(vector_name))
        # 轴名为 valence/arousal/dominance（emotion params.to_dict 口径，2026-11-13 修正旧 v/a/d 误用）
        if vector is None or not all(_number(vector.get(axis)) for axis in ("valence", "arousal", "dominance")):
            return False
    if not _number(params.get("positive_gain")) or not _number(params.get("negative_decay")):
        return False
    return _persona_matches(params.get("persona_digest"), ctx.personality)


def validate_events(ctx: ValidationContext) -> bool:
    """事件库：目录非空 + 事件条目 schema 合法（mode=off 视为无需初始化）。"""
    mode = _text(_config_value(ctx.config, "event_catalog", "mode", "off")).lower()
    if mode == "off":
        return True
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
        return False
    events = _sequence(doc.get("events"))
    if not events:
        return False
    for event in events:
        item = _mapping(event)
        if item is None:
            return False
        if not _text(item.get("event_id")) or not _text(item.get("name")):
            return False
        if not _text(item.get("goal_type")):
            return False
        if not isinstance(item.get("min_minutes"), int) or not isinstance(item.get("max_minutes"), int):
            return False
    return True


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


def validate_state_sleep(ctx: ValidationContext) -> bool:
    """状态/睡眠：life_state.json 存在 + 规范键齐全 + sleep_sessions 表存在。"""
    details = _details(ctx)
    raw_path = details.get("state_file")
    path = Path(str(raw_path)) if raw_path else ctx.data_dir / "life_state.json"
    doc = _mapping(_load_json(path))
    if doc is None:
        return False
    required = _life_state_default_keys()
    if not required:
        return False
    if any(key not in doc for key in required):
        return False
    # 版本字段存在时必须是当前版本（未知未来版本由准备阶段拒绝重建）。
    if "version" in doc and not isinstance(doc.get("version"), int):
        return False
    raw_db = details.get("db_path")
    db_path = Path(str(raw_db)) if raw_db else ctx.data_dir / "schedule.db"
    return _sqlite_has_table(db_path, "sleep_sessions")


def validate_frequency(ctx: ValidationContext) -> bool:
    """目标频率：开关关/日程停用=无需初始化；否则校准成功且真实下发 applied。"""
    if not _schedule_enabled(ctx):
        return True
    always_one = _config_value(
        ctx.config, "planning_proactive", "target_frequency_always_one", True)
    if not bool(always_one):
        return True
    if not _stage_ok(ctx):
        return False
    details = _details(ctx)
    if details.get("applied") is True:
        return True
    # 兼容注入式校准器：仅返回 True/字符串时无 applied 键，按阶段成功认定。
    return "applied" not in details and "target_resolved" not in details


def validate_video_interest(ctx: ValidationContext) -> bool:
    """视频兴趣：模块关闭=无需初始化；开启=兴趣文档有关键词且带有效时间戳。"""
    if not _video_enabled(ctx):
        return True
    if not _stage_ok(ctx):
        return False
    doc = _mapping(_load_json(ctx.data_dir / "video_watch" / "video_interest.json"))
    if doc is None:
        return False
    keywords = _sequence(doc.get("keywords"))
    if not keywords or not all(_text(word) for word in keywords):
        return False
    return _number(doc.get("obtained_at")) and float(doc.get("obtained_at") or 0) > 0


def validate_companion_disposition(ctx: ValidationContext) -> bool:
    """联系倾向：initiative_state.json 的 profile 合法且与人设指纹匹配。"""
    doc = _mapping(_load_json(ctx.data_dir / "initiative_state.json"))
    if doc is None or doc.get("version") != 1:
        return False
    profile = _mapping(doc.get("profile"))
    if profile is None:
        return False
    anchor = profile.get("anchor_hours")
    loneliness = profile.get("loneliness")
    if not _number(anchor) or not (2 <= float(anchor) <= 24):
        return False
    if not _number(loneliness) or not (0 <= float(loneliness) <= 1):
        return False
    digest = _text(profile.get("persona_hash"))
    if not digest:
        return False
    current = str(ctx.personality or "")
    if current:
        return digest == hashlib.sha256(current.encode("utf-8")).hexdigest()
    return True


def validate_activity_preferences(ctx: ValidationContext) -> bool:
    """日程偏好：开关关=无需初始化；开启=缓存 schema 合法且有人设指纹。"""
    if not _schedule_enabled(ctx):
        return True
    if not _stage_ok(ctx):
        return False
    doc = _mapping(_load_json(ctx.data_dir / "activity_preferences.json"))
    if doc is None or doc.get("schema_version") != 1:
        return False
    digest = _text(doc.get("persona_hash"))
    if not digest:
        return False
    current = str(ctx.personality or "")
    if current and digest != hashlib.sha256(current.encode("utf-8")).hexdigest():
        return False
    activities = _mapping(doc.get("activities")) or {}
    traits = _mapping(doc.get("traits")) or {}
    pending = _mapping(doc.get("pending")) or {}
    has_traits = bool(traits) and all(_number(traits.get(axis)) for axis in (
        "leisure_recovery", "physical_activity", "focused_activity",
        "social", "novelty", "regularity"))
    has_pending = bool(pending.get("count")) or bool(pending.get("traits"))
    return bool(activities) or has_traits or has_pending


#: 字段 → 独立校验器（init 后调用；返回 bool，通过才标记 initialized=true）
VALIDATORS: Dict[str, Callable[[ValidationContext], bool]] = {
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
    "BACKOFF_ATTEMPT_THRESHOLD", "ValidationContext", "VALIDATORS",
    "version_key", "reinit_fields_for_upgrade", "backoff_seconds",
]
