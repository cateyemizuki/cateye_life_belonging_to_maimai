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
- 出厂种子在后续版本删除的事件：只有运行目录副本与旧种子逐字段一致
  （``definition_version`` 命中 :data:`RETIRED_SEED_EVENTS`）时才忽略并计数；
  用户改过的副本按「用户自定义优先」继续参与。全程只读，不删不改用户文件。
- 只读种子差异（seed_files/seed_only/runtime_only/seed_changed）只用于诊断，
  不触发复制/覆盖/删除。3.17.33 起 :data:`MEMORY_MERGE_SEED_EVENTS` 列出的
  **本批新增种子**在运行目录没有同名文件时只读合并进内存事件池（不写用户目录、
  不改文案）；其余种子仍由用户按 README 契约自行复制。用户已复制（含
  enabled=false、坏文件占位）的副本优先，不再合并该 ID。
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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
ACTION_RETIRED = "planning.initial_events_retired"

#: 出厂种子在 3.17.33（2026-11-10）删除、但旧安装运行目录可能仍留着的五份
#: 初始事件。值 = 旧种子 ``definition_version``（sha256(raw event)[:12]）。
#: 只有运行目录副本仍与旧种子逐字段一致时才忽略；用户改过（版本不匹配）
#: 视为用户自定义，继续参与。忽略只发生在内存装载，绝不删除/改写用户文件。
RETIRED_SEED_EVENTS: Dict[str, str] = {
    "local_doodle": "9f41a8714b71",
    "local_journal": "42a92a9b2317",
    "local_light_exercise": "ff2aa04fa50c",
    "local_old_notes": "3cce5408da9c",
    "local_organize": "8cc6b5057f87",
}

#: 3.17.33 新增、旧安装运行目录通常尚未复制的手动种子。这些 ID 在运行目录没有
#: 同名文件（文件名或 event_id 任一命中即视为用户已接手）时，从出厂种子只读
#: 合并进内存事件池；**不写盘、不补文件、不改文案**。只列本批新增项，避免把用户
#: 显式删除的旧种子复活；后续新增种子批次在此追加。
#: 被合并项会标 ``seed_merged=True``，并计入 ``InitialEventsResult.merged``；
#: ``seed_only`` 诊断保持「运行目录没有这份文件」的原意（不会被合并抹掉）。
MEMORY_MERGE_SEED_EVENTS: Tuple[str, ...] = (
    "local_look_outside",
    "local_rest_eyes",
    "local_wash_face",
)


@dataclass(frozen=True)
class InitialEventsResult:
    """一次装载结果（只读快照；events 已规范化、禁用项已剔除）。

    ``retired`` 记录被忽略的旧种子副本（文件名, event_id），不计入
    ``skipped``（不是坏文件，也不是用户错误）；``merged`` 记录本批新增种子
    在内存中合并的项（文件名, event_id），同样不写盘；``seed_*`` 为只读种子
    差异诊断。
    """

    events: Tuple[Dict[str, Any], ...] = ()
    skipped: Tuple[Tuple[str, str], ...] = ()      # (文件名, 原因)
    disabled_count: int = 0
    files_seen: int = 0
    content_version: str = ""
    dir_exists: bool = False
    retired: Tuple[Tuple[str, str], ...] = ()      # (文件名, event_id)
    merged: Tuple[Tuple[str, str], ...] = ()       # (种子文件名, event_id)
    seed_files: int = 0
    seed_only: Tuple[str, ...] = ()                # 种子有、运行目录未接受
    runtime_only: Tuple[str, ...] = ()             # 运行目录有、种子没有（用户自定义）
    seed_changed: Tuple[str, ...] = ()             # 同 event_id、definition_version 不同

    @property
    def ok(self) -> bool:
        return self.dir_exists

    @property
    def retired_count(self) -> int:
        return len(self.retired)

    @property
    def merged_count(self) -> int:
        return len(self.merged)


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
    """单份文件校验；任何违反 → ValueError（整份跳过）。

    返回的 name/templates/tags **逐字保留 JSON 原文**（不 strip、不改写），
    只做类型/长度/非空校验；这是「内置库文案逐字不变」的装载侧保证。
    """
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
        "name": name,
        "enabled": enabled,
        "goal_type": goal_type,
        "min_minutes": lo,
        "max_minutes": hi,
        "interruptibility": float(interruptibility),
        "weight": float(weight),
        "cooldown_minutes": float(cooldown),
        "allowed_slots": [str(s) for s in slots],
        "state_deltas": {str(k): float(v) for k, v in deltas.items()},
        "templates": [str(t) for t in templates],
        "tags": [str(t) for t in tags],
        # 池类型（用途=初始列）与来源类型（JSON 文件）分开（§6.2/§8）
        "pool_kind": "builtin",
        "source": "json_file",
        "origin_kind": "json_file",
        "source_file": filename,
        "definition_version": definition_version,
    }


def _seed_snapshot(plugin_root: Any
                   ) -> Tuple[int, Dict[str, Dict[str, Any]]]:
    """只读扫描出厂种子：返回 (JSON 文件数, {event_id: 规范化事件})。

    种子文件同样走 :func:`_validate_document`（不写盘、不落账）；坏种子只从
    差异对照/内存合并中排除，绝不影响运行目录装载。种子目录缺失（旧包）
    返回 (0, {})。重复 event_id 取文件名排序后的第一份（确定性；真实种子无重复）。
    """
    try:
        seed_dir = seed_path(plugin_root, INITIAL_EVENTS_DIR)
        if not seed_dir.is_dir():
            return 0, {}
        files = sorted((p for p in seed_dir.iterdir()
                        if p.is_file() and p.suffix.lower() == ".json"),
                       key=lambda p: p.name)
    except Exception:
        return 0, {}
    entries: Dict[str, Dict[str, Any]] = {}
    for path in files[:MAX_FILES]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            normalized = _validate_document(read_json_text(path), path.name)
        except (ValueError, MappingReadError, OSError):
            continue
        entries.setdefault(normalized["event_id"], normalized)
    return len(files), entries


def _diff_against_seed(plugin_root: Any,
                       accepted: Sequence[Mapping[str, Any]],
                       seed_snapshot: Optional[Tuple[int, Dict[str, Dict[str, Any]]]]
                       = None
                       ) -> Tuple[int, Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """运行目录 vs 出厂种子的只读差异（按 event_id + definition_version）。

    ``seed_only``：种子有、运行目录没有（用户尚未复制的新事件）；
    ``runtime_only``：运行目录有、种子没有（用户自定义或旧版残留）；
    ``seed_changed``：同 ID 但 definition_version 不同（用户改过或种子更新）。
    只用于诊断与内存合并判定，绝不据此复制/覆盖/删除用户文件。
    ``seed_snapshot`` 可由调用方复用以避免同一轮重复扫描种子目录。
    """
    seed_files, seed_map = seed_snapshot if seed_snapshot is not None \
        else _seed_snapshot(plugin_root)
    if seed_files == 0:
        return 0, (), (), ()
    runtime_map = {str(e.get("event_id") or ""): str(e.get("definition_version") or "")
                   for e in accepted if str(e.get("event_id") or "")}
    seed_versions = {eid: str(e.get("definition_version") or "")
                     for eid, e in seed_map.items()}
    seed_only = tuple(sorted(set(seed_versions) - set(runtime_map)))
    runtime_only = tuple(sorted(set(runtime_map) - set(seed_versions)))
    changed = tuple(sorted(eid for eid in set(seed_versions) & set(runtime_map)
                           if seed_versions[eid] != runtime_map[eid]))
    return seed_files, seed_only, runtime_only, changed


def load_initial_events(plugin_root: Any,
                        decision_logger: Any = None) -> InitialEventsResult:
    """启动/热重载时统一读取一次（不增后台扫目录任务，不绑定模型生成）。

    坏一份只跳过一份；重复 ID 拒绝全部冲突项（确定性，不依赖遍历顺序）；
    禁用项剔除；出厂已删除、且本地未改过的旧种子副本单独计入 ``retired``
    （不加进 ``skipped``，避免把库升级误报成用户文件错误）；结果带内容版本
    快照与只读种子差异。任何异常收敛为空池/部分池，绝不抛出装配链。
    """
    if plugin_root is None:
        return InitialEventsResult()
    user_dir = mappings_path(plugin_root, INITIAL_EVENTS_DIR)
    try:
        if not user_dir.is_dir():
            seed_files, seed_only, _runtime_only, _changed = _diff_against_seed(
                plugin_root, ())
            _record(ACTION_APPLIED, decision_logger, severity="info",
                    accepted=0, dir_exists=False, seed_files=seed_files,
                    seed_only=list(seed_only),
                    note="无可用状态事件（目录不存在；代码联动事件与主日程照常）")
            return InitialEventsResult(dir_exists=False, seed_files=seed_files,
                                       seed_only=seed_only)
        files = sorted((p for p in user_dir.iterdir()
                        if p.is_file() and p.suffix.lower() == ".json"),
                       key=lambda p: p.name)
    except Exception as exc:
        logger.warning("初始事件库目录读取失败（按空池处理）：%s", exc)
        return InitialEventsResult()

    skipped: List[Tuple[str, str]] = []
    parsed: List[Tuple[str, Dict[str, Any]]] = []   # (event_id, normalized)
    retired: List[Tuple[str, str]] = []             # (文件名, event_id)
    files_seen = len(files)
    # 运行目录里出现过的 JSON 文件名（含坏文件/禁用文件）：同名种子不再内存合并，
    # 用户对这份文件的处置（改坏/禁用/改名）优先。
    runtime_filenames = {path.name for path in files}
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

    # 出厂已删除事件：仅当运行目录副本与旧种子逐字段一致（definition_version
    # 命中 RETIRED_SEED_EVENTS）时忽略；用户改过的副本按自定义事件保留。
    active: List[Tuple[str, Dict[str, Any]]] = []
    for eid, normalized in parsed:
        retired_version = RETIRED_SEED_EVENTS.get(eid)
        if retired_version is not None \
                and normalized["definition_version"] == retired_version:
            retired.append((normalized["source_file"], eid))
            continue
        active.append((eid, normalized))

    # 重复 ID：拒绝全部冲突项（两项都不加载），结果与遍历顺序无关。
    id_counts: Dict[str, int] = {}
    for eid, _ in active:
        id_counts[eid] = id_counts.get(eid, 0) + 1
    accepted: List[Dict[str, Any]] = []
    disabled_count = 0
    for eid, normalized in active:
        if id_counts[eid] > 1:
            skipped.append((normalized["source_file"],
                            f"event_id {eid!r} 与其他文件重复（冲突项全部拒绝）"))
            continue
        if not normalized["enabled"]:
            disabled_count += 1
            continue
        accepted.append(normalized)

    # 种子差异按「运行目录实际存在的合法定义」对比（含 enabled=false 的禁用
    # 副本；退役项单独计数），避免把「已复制但禁用」误报成「未复制」。
    seed_snapshot = _seed_snapshot(plugin_root)
    seed_files, seed_only, runtime_only, seed_changed = _diff_against_seed(
        plugin_root, [normalized for _eid, normalized in active], seed_snapshot)
    seed_map = seed_snapshot[1]

    # 本批新增种子：运行目录没有接手（同名文件或同 event_id 均无）→ 只读合并。
    # 合并项使用与运行目录事件完全相同的规范化结构（文案逐字来自种子 JSON），
    # 仅多一个 seed_merged 诊断标记；不写盘、不补文件、不改变 seed_only 诊断。
    merged: List[Tuple[str, str]] = []              # (种子文件名, event_id)
    merged_events: List[Dict[str, Any]] = []
    runtime_ids = {eid for eid, _ in active}
    # 空运行目录 = 用户明确选择「空池」（README：目录已存在哪怕为空也绝不补回），
    # 不做内存合并；只有已存在至少一份 JSON 的运行目录才参与本批新增种子合并。
    if runtime_filenames:
        for eid in MEMORY_MERGE_SEED_EVENTS:
            if eid in runtime_ids or eid in RETIRED_SEED_EVENTS:
                continue
            seed_event = seed_map.get(eid)
            if seed_event is None or not seed_event.get("enabled", True):
                continue
            if str(seed_event.get("source_file") or "") in runtime_filenames:
                continue  # 同名文件已存在（坏/禁用/自定义）→ 用户处置优先
            item = dict(seed_event)
            item["seed_merged"] = True
            merged_events.append(item)
            merged.append((str(item["source_file"]), eid))
    effective = [*accepted, *merged_events]

    content_version = hashlib.sha256(
        json.dumps([{k: e[k] for k in ("event_id", "definition_version")}
                    for e in effective], ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:12] if effective else "empty"

    if retired:
        logger.info("初始事件库 %d 份旧种子副本已退役（本地未修改，不参与）：%s",
                    len(retired), "、".join(eid for _n, eid in retired))
        _record(ACTION_RETIRED, decision_logger, severity="info",
                events=[{"file": n, "event_id": eid} for n, eid in retired],
                note="出厂种子已删除且本地副本未修改；仅内存忽略，未删除/改写用户文件")
    if merged:
        logger.info("初始事件库 %d 份本批新增种子只读合并进内存池（未写入用户目录）：%s",
                    len(merged), "、".join(eid for _n, eid in merged))
    if skipped:
        logger.warning("初始事件库 %d 份被跳过：%s", len(skipped),
                       "；".join(f"{n}（{r}）" for n, r in skipped))
        _record(ACTION_REJECTED, decision_logger, severity="warn",
                skipped=[{"file": n, "reason": r} for n, r in skipped])
    _record(ACTION_APPLIED, decision_logger, severity="info",
            accepted=len(effective), disabled=disabled_count, files_seen=files_seen,
            retired=len(retired), merged=len(merged),
            content_version=content_version, dir_exists=True,
            accepted_ids=[e["event_id"] for e in effective],
            merged_ids=[eid for _n, eid in merged],
            seed_files=seed_files, seed_only=list(seed_only),
            runtime_only=list(runtime_only), seed_changed=list(seed_changed))
    return InitialEventsResult(
        events=tuple(effective), skipped=tuple(skipped),
        disabled_count=disabled_count, files_seen=files_seen,
        content_version=content_version, dir_exists=True,
        retired=tuple(retired), merged=tuple(merged),
        seed_files=seed_files, seed_only=seed_only,
        runtime_only=runtime_only, seed_changed=seed_changed)