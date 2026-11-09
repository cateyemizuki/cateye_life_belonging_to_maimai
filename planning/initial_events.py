# -*- coding: utf-8 -*-
"""初始小事件 JSON 库装载器（P2，2026-11-09 滚动日程计划 §6）。

职责边界（计划书 §6.1/§6.2 逐条）：

- 用户编辑的唯一运行目录 = ``mappings/initial_events/``；出厂种子在
  ``mappings/default_mappings/initial_events/``，**仅首次初始化**（目录不存在）
  复制一份；目录已存在（哪怕为空）绝不补回被删除/改坏的事件。
- 一份 JSON 定义一个事件；仅扫描该层 ``*.json``（不递归）；UTF-8 容忍一个 BOM；
  文件数 ≤128、单份 ≤32KiB。
- 某文件语法/schema/语义校验失败 → **整份跳过**，其余合法文件继续；日志列出
  文件名与原因；不覆盖、不修复、不删除原件，也不暗中恢复代码默认。
- 全部缺失/禁用/失败 → 状态事件池为空是合法状态（代码联动事件与主日程照常）。
- JSON 禁止定义工具/账号/网络请求/文件读写/动态代码/hook/sleep_debt 变化：
  未知控制字段按错误处理；模块联动只能由受保护代码注册（watch_bili/browse_qq
  为保留 ID，``llm_`` 前缀保留给生成池）。
- 重复 ID：拒绝全部冲突项并报告，结果不依赖文件遍历顺序（按文件名排序处理）。
- 加载后生成内容版本（content_version）与逐事件 definition_version 快照；
  运行回执/冷却/消耗绝不写回用户 JSON（planner 持久用量在 data 目录）。
- 本模块不做状态结算、不发布情绪事件、不调用 LLM——结算唯一链仍是
  ``LifeStateService._apply_awake_v2 → settlement_v2``（§6.5）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.mapping_dir import MappingReadError, mappings_path, read_json_text, seed_path

from .event_catalog import MAX_DELTA_ABS, STATE_FIELDS
from .event_slots import TIME_SLOTS

logger = logging.getLogger(__name__)

#: 目录名（运行目录与种子目录同名，分别在 mappings/ 与 default_mappings/ 下）
INITIAL_EVENTS_DIR = "initial_events"
#: 顶层 version 必填 int 且等于本值（bool 不算 int）
SUPPORTED_VERSION = 1
#: 文件数/单份体积上限（§6.2 建议值）
MAX_FILES = 128
MAX_FILE_BYTES = 32 * 1024
#: 时长允许范围（分钟；默认内容主要在 5–60）
MIN_DURATION_MINUTES = 3
MAX_DURATION_MINUTES = 120
#: JSON 状态事件允许的活动画像类型（禁止 sleep/idle/meal 等锚点/生理语义，
#: 禁止 work/study 把休闲伪装成工作项目）
ALLOWED_GOAL_TYPES = frozenset({"rest", "exercise", "entertainment",
                                "daily_routine", "social"})
#: 保留 ID：代码联动两项 + 生成池前缀（JSON 不得覆盖/伪装）
RESERVED_EVENT_IDS = frozenset({"watch_bili", "browse_qq"})
RESERVED_ID_PREFIX = "llm_"
#: event_id 形态（小写字母开头，小写字母/数字/下划线，2–48 字符）
_EVENT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,47}$")
#: event 对象允许的字段全集（未知控制字段 = 错误，§6.2 越权红线）
EVENT_KEYS = frozenset({
    "event_id", "name", "enabled", "goal_type", "min_minutes", "max_minutes",
    "interruptibility", "weight", "cooldown_minutes", "allowed_slots",
    "state_deltas", "templates", "tags",
})
#: 必填字段（其余有保守缺省）
REQUIRED_EVENT_KEYS = ("event_id", "name", "goal_type", "min_minutes",
                       "max_minutes", "allowed_slots", "templates")
#: 首次初始化标记（目录内非 json 文件，loader 不扫描；记录初始化状态供取证）
SEED_MARKER_NAME = ".initialized-from-seed"

ACTION_APPLIED = "planning.initial_events_applied"
ACTION_REJECTED = "planning.initial_events_rejected"
ACTION_SEEDED = "planning.initial_events_seeded"


@dataclass(frozen=True)
class InitialEventsResult:
    """一次装载结果（只读快照；events 已规范化、禁用项已剔除）。"""

    events: Tuple[Dict[str, Any], ...] = ()
    skipped: Tuple[Tuple[str, str], ...] = ()      # (文件名, 原因)
    disabled_count: int = 0
    files_seen: int = 0
    content_version: str = ""
    dir_exists: bool = False

    @property
    def ok(self) -> bool:
        return self.dir_exists


def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def _record(action: str, decision_logger: Any, **fields: Any) -> None:
    if decision_logger is None:
        return
    record = getattr(decision_logger, "record", None)
    if not callable(record):
        return
    try:
        record(action=action, **fields)
    except Exception:
        logger.debug("initial_events 落账异常（忽略）", exc_info=True)


def ensure_initial_events_dir(plugin_root: Any, decision_logger: Any = None) -> bool:
    """首次初始化：运行目录不存在 → 从种子整目录复制（含 README）并留标记。

    目录已存在（含空目录）→ 绝不补种（用户删除/改坏不自动恢复，§6.2）。
    种子缺失（旧包）→ 不创建，装载按空池处理。返回是否执行了首次复制。
    """
    if plugin_root is None:
        return False
    user_dir = mappings_path(plugin_root, INITIAL_EVENTS_DIR)
    try:
        if user_dir.exists():
            return False
        seed_dir = seed_path(plugin_root, INITIAL_EVENTS_DIR)
        if not seed_dir.is_dir():
            return False
        user_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(seed_dir, user_dir)
        try:
            (user_dir / SEED_MARKER_NAME).write_text(
                "首次初始化自出厂种子 default_mappings/initial_events/；"
                "此后升级不覆盖、不补回删除项。\n", encoding="utf-8")
        except OSError as exc:
            # 标记失败不影响复制本身（目录存在即不再补种）；留痕供取证。
            logger.debug("初始事件库初始化标记写入失败（忽略）：%s", exc)
    except Exception as exc:
        logger.warning("初始事件库首次初始化失败（按空池处理，不阻塞）：%s", exc)
        return False
    logger.info("初始事件库已从种子初始化：%s", user_dir)
    _record(ACTION_SEEDED, decision_logger, severity="info", path=str(user_dir))
    return True


def _validate_document(doc: Any, filename: str) -> Dict[str, Any]:
    """单份文件校验；任何违反 → ValueError（整份跳过）。返回规范化事件 dict。"""
    if not isinstance(doc, dict):
        raise ValueError("顶层期望 JSON 对象")
    unknown_top = set(doc) - {"version", "event"}
    if unknown_top:
        raise ValueError(f"未知顶层字段 {sorted(unknown_top)}（禁止额外控制字段）")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) \
            or version != SUPPORTED_VERSION:
        raise ValueError(f"version 期望整数 {SUPPORTED_VERSION}，实得 {version!r}")
    event = doc.get("event")
    if not isinstance(event, dict):
        raise ValueError("event 期望对象")
    unknown = set(event) - EVENT_KEYS
    if unknown:
        raise ValueError(f"未知控制字段 {sorted(unknown)}（越权字段按错误处理）")
    for key in REQUIRED_EVENT_KEYS:
        if key not in event:
            raise ValueError(f"缺少必填字段 {key}")

    eid = event["event_id"]
    if not isinstance(eid, str) or not _EVENT_ID_RE.match(eid):
        raise ValueError("event_id 需小写字母开头的小写字母/数字/下划线（2–48 字符）")
    if eid in RESERVED_EVENT_IDS:
        raise ValueError(f"event_id {eid!r} 为代码联动保留 ID（JSON 不得覆盖）")
    if eid.startswith(RESERVED_ID_PREFIX):
        raise ValueError("event_id 不得使用 llm_ 前缀（生成池保留命名空间）")

    name = event["name"]
    if not isinstance(name, str) or not name.strip() or len(name) > 60:
        raise ValueError("name 需非空字符串且 ≤60 字符")

    enabled = event.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled 需布尔值")

    goal_type = event["goal_type"]
    if not isinstance(goal_type, str) or goal_type not in ALLOWED_GOAL_TYPES:
        raise ValueError(f"goal_type {goal_type!r} 不在允许画像 {sorted(ALLOWED_GOAL_TYPES)}")

    lo, hi = event["min_minutes"], event["max_minutes"]
    if not _is_finite_number(lo) or not _is_finite_number(hi):
        raise ValueError("min/max_minutes 需有限数值（bool/字符串/NaN/Infinity 拒绝）")
    lo, hi = int(lo), int(hi)
    if lo != event["min_minutes"] or hi != event["max_minutes"]:
        raise ValueError("min/max_minutes 需整数分钟")
    if not (MIN_DURATION_MINUTES <= lo <= hi <= MAX_DURATION_MINUTES):
        raise ValueError(f"时长需 {MIN_DURATION_MINUTES}≤min≤max≤{MAX_DURATION_MINUTES} 分钟")

    interruptibility = event.get("interruptibility", 0.9)
    if not _is_finite_number(interruptibility) or not 0.0 <= float(interruptibility) <= 1.0:
        raise ValueError("interruptibility 需 [0,1] 有限数值")
    weight = event.get("weight", 0.6)
    if not _is_finite_number(weight) or not 0.1 <= float(weight) <= 1.0:
        raise ValueError("weight 需 [0.1,1.0] 有限数值")
    cooldown = event.get("cooldown_minutes", 0)
    if not _is_finite_number(cooldown) or float(cooldown) < 0:
        raise ValueError("cooldown_minutes 需 ≥0 有限数值")

    slots = event["allowed_slots"]
    if not isinstance(slots, (list, tuple)) or not slots \
            or any(s not in TIME_SLOTS for s in slots) \
            or len(set(slots)) != len(slots):
        raise ValueError(f"allowed_slots 需 {list(TIME_SLOTS)} 的非空无重复子集")

    deltas = event.get("state_deltas", {})
    if not isinstance(deltas, dict):
        raise ValueError("state_deltas 需对象")
    for key, value in deltas.items():
        if key not in STATE_FIELDS:
            raise ValueError(f"state_deltas 键 {key!r} 不在五项内（禁止 sleep_debt）")
        if not _is_finite_number(value) or abs(float(value)) > MAX_DELTA_ABS + 1e-12:
            raise ValueError(f"state_deltas.{key} 需有限数值且单次绝对值 ≤{MAX_DELTA_ABS}")

    templates = event["templates"]
    if not isinstance(templates, (list, tuple)) or not 1 <= len(templates) <= 4 \
            or any(not isinstance(t, str) or not t.strip() or len(t) > 200
                   for t in templates):
        raise ValueError("templates 需 1–4 句非空字符串（每句 ≤200 字符）")
    tags = event.get("tags", [])
    if not isinstance(tags, (list, tuple)) or len(tags) > 3 \
            or any(not isinstance(t, str) or not t.strip() or len(t) > 20 for t in tags):
        raise ValueError("tags 需 ≤3 个非空短字符串（每个 ≤20 字符）")

    definition_version = hashlib.sha256(
        json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "event_id": eid,
        "key": eid,
        "name": name.strip(),
        "enabled": enabled,
        "goal_type": goal_type,
        "min_minutes": lo,
        "max_minutes": hi,
        "interruptibility": float(interruptibility),
        "weight": float(weight),
        "cooldown_minutes": float(cooldown),
        "allowed_slots": [str(s) for s in slots],
        "state_deltas": {str(k): float(v) for k, v in deltas.items()},
        "templates": [str(t).strip() for t in templates],
        "tags": [str(t).strip() for t in tags],
        # 池类型（用途=初始列）与来源类型（JSON 文件）分开（§6.2/§8）
        "pool_kind": "builtin",
        "source": "json_file",
        "origin_kind": "json_file",
        "source_file": filename,
        "definition_version": definition_version,
    }


def load_initial_events(plugin_root: Any,
                        decision_logger: Any = None) -> InitialEventsResult:
    """启动/热重载时统一读取一次（不增后台扫目录任务，不绑定模型生成）。

    坏一份只跳过一份；重复 ID 拒绝全部冲突项（确定性，不依赖遍历顺序）；
    禁用项剔除；结果带内容版本快照。任何异常收敛为空池/部分池，绝不抛出装配链。
    """
    if plugin_root is None:
        return InitialEventsResult()
    user_dir = mappings_path(plugin_root, INITIAL_EVENTS_DIR)
    try:
        if not user_dir.is_dir():
            _record(ACTION_APPLIED, decision_logger, severity="info",
                    accepted=0, dir_exists=False,
                    note="无可用状态事件（目录不存在；代码联动事件与主日程照常）")
            return InitialEventsResult()
        files = sorted((p for p in user_dir.iterdir()
                        if p.is_file() and p.suffix.lower() == ".json"),
                       key=lambda p: p.name)
    except Exception as exc:
        logger.warning("初始事件库目录读取失败（按空池处理）：%s", exc)
        return InitialEventsResult()

    skipped: List[Tuple[str, str]] = []
    parsed: List[Tuple[str, Dict[str, Any]]] = []   # (event_id, normalized)
    files_seen = len(files)
    if len(files) > MAX_FILES:
        for extra in files[MAX_FILES:]:
            skipped.append((extra.name, f"超出文件数上限 {MAX_FILES}（按文件名排序截断）"))
        files = files[:MAX_FILES]
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError(f"单份超过 {MAX_FILE_BYTES // 1024}KiB 上限")
            doc = read_json_text(path)
            normalized = _validate_document(doc, path.name)
        except (ValueError, MappingReadError, OSError) as exc:
            skipped.append((path.name, str(exc)))
            continue
        parsed.append((normalized["event_id"], normalized))

    # 重复 ID：拒绝全部冲突项（两项都不加载），结果与遍历顺序无关。
    id_counts: Dict[str, int] = {}
    for eid, _ in parsed:
        id_counts[eid] = id_counts.get(eid, 0) + 1
    accepted: List[Dict[str, Any]] = []
    disabled_count = 0
    for eid, normalized in parsed:
        if id_counts[eid] > 1:
            skipped.append((normalized["source_file"],
                            f"event_id {eid!r} 与其他文件重复（冲突项全部拒绝）"))
            continue
        if not normalized["enabled"]:
            disabled_count += 1
            continue
        accepted.append(normalized)

    content_version = hashlib.sha256(
        json.dumps([{k: e[k] for k in ("event_id", "definition_version")}
                    for e in accepted], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12] if accepted else "empty"

    if skipped:
        logger.warning("初始事件库 %d 份被跳过：%s", len(skipped),
                       "；".join(f"{n}（{r}）" for n, r in skipped))
        _record(ACTION_REJECTED, decision_logger, severity="warn",
                skipped=[{"file": n, "reason": r} for n, r in skipped])
    _record(ACTION_APPLIED, decision_logger, severity="info",
            accepted=len(accepted), disabled=disabled_count, files_seen=files_seen,
            content_version=content_version, dir_exists=True,
            accepted_ids=[e["event_id"] for e in accepted])
    return InitialEventsResult(
        events=tuple(accepted), skipped=tuple(skipped),
        disabled_count=disabled_count, files_seen=files_seen,
        content_version=content_version, dir_exists=True)