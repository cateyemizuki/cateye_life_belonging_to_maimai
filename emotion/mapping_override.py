"""情绪映射表 JSON 覆盖层 — 加载与校验（M12 / 单元 V2）。

**范围声明**：本单元只落"读取 + 校验 + 落账"，**不接线**——模块保持惰性，不被任何
现有代码路径调用（V3 才把 ``vad``/``bridge``/``service``/``state_machine`` 的常量改读
"默认 ⊕ 覆盖"的合并结果）。因此本模块不改变既有行为。

数据文件
--------
``<data_dir>/emotion_mapping.json``（可选；宿主插件数据目录，与 ``proactive_budget.json``
同级惯例）。**不存在 = 纯代码默认**（静默，不落任何 action）。文件只读：本模块不写任何文件。

顶层结构（``version`` 必填，四张表键均可选）::

    {
      "version": 1,
      "prototypes": [{"label": "平静", "valence": [0.3, 0.8],
                      "arousal": [-0.8, -0.2], "dominance": [0.2, 0.7]}, ...],
      "template_events": {"relation.silence_detected": {"valence": -0.12, "arousal": 0.04,
                                                        "dominance": 0.0},
                          "schedule.commitment_kept": {...}, "schedule.commitment_broken": {...},
                          "qzone.comment_received": {"valence_min": 0.05, "valence_max": 0.1}},
      "multipliers": {"global_event_multiplier": 1.0, "non_target_multiplier": 1.0,
                      "target_bonus_step": 0.1},
      "level_thresholds": [-0.5, -0.15, 0.15, 0.5]
    }

语义（V1 盘点裁定，权威）
------------------------
- ``version`` 必填 int 且 ``== SUPPORTED_VERSION``（``bool`` 不算 int）；键缺失 →
  ``error_kind=version_missing``，在场但非 int/≠1 → ``version_unsupported``；
- 四表键均可选，**存在即整表替换**——表内漏键 = 校验失败，**不**静默保留默认；
- 任一校验失败 → **整文件拒绝、全量回退默认**，不做部分应用；
- 优先级：**JSON 合法文件 > config（含用户显式值）> 代码默认**。四表中只有 ``multipliers``
  在 config 有对应项（``[emotion]`` 的三个倍率字段）；``prototypes``/``template_events``/
  ``level_thresholds`` 没有 config 对应项；
- ``|单事件增量| ≤ EVENT_DELTA_LIMIT``（= ``emotion/service.py`` 的 ``EVENT_DELTA_CLAMP``）；
  ``json.loads`` 接受的 ``NaN``/``Infinity`` 字面量一律按 range 拒绝（判定写法为"仅当满足
  上界判据才收"，不写成"大于才拒"——后者会放过 NaN）；
- ``level_thresholds`` **单调不减**（``c1<=c2<=c3<=c4``）：出现**下降**才拒绝；
  相邻**相等**只告警不拒绝（该档位为空）；
- 与 config 的一处**已知差异**：``non_target_multiplier`` / ``target_bonus_step`` 在 config
  侧 ``ge=0.0``（允许 0），JSON 侧下限为**开区间**（0 不合法）——0 倍率等于把该通道静音，
  属配置自伤，JSON 覆盖层不收。

``error_kind`` 取值与判据
------------------------
- ``parse``              读取失败 / 非 UTF-8 / JSON 语法错误（``error_key`` 为空串，无键可指）；
- ``version_missing``    ``version`` 键缺失；
- ``version_unsupported````version`` 在场但非 int（含 bool）或 ≠ 1；
- ``unknown_key``        出现 schema 之外的键/事件名（顶层、原型条目、事件条目、倍率）；
- ``incomplete_table``   应有键缺失（表内漏键、``template_events`` 四事件不全）；
- ``type``               类型不符（表非对象/数组、数值位是 bool 或字符串等）；
- ``range``              数量越界（原型条数 / 阈值个数）与取值约束（越界、``NaN``/``Infinity``、
                          阈值出现下降、标签空或重复、``valence_min > valence_max``）。

校验顺序固定（报告**首个**出错键）：version → 顶层键集 → prototypes → template_events →
multipliers → level_thresholds；表内按 JSON 文件序（数组按索引序）。
``error_key`` 是点分路径（如 ``prototypes[3].valence``、``template_events.qzone.comment_received``）。

落账（``DecisionLogger.record``；``decision_logger=None`` 时跳过落账，返回值不变）
--------------------------------------------------------------------------------
- 采纳 → ``ACTION_APPLIED``（``emotion.mapping_override_applied``）：
  ``severity="info"``、``version``、``keys``（实际覆盖的表名）、``counts``（各表条目数）、
  有告警时附 ``warnings``；
- 回退 → ``ACTION_REJECTED``（``emotion.mapping_override_rejected``）：
  ``severity="warn"``、``error_key``、``error_kind``、``detail``（含期望 vs 实得）。

``core/diag.py`` 没有严重度系统：``severity`` 只是条目的**自由字段**（不新造机制），
上下文日志由调用方（V3）打。

本模块**不 import maibot_sdk**，模块顶层只依赖标准库（``vad``/``bridge``/``config`` 的
常量访问全部延迟到函数体内，见 :func:`default_tables`），既能在无 SDK 的离线环境独立导入，
也避免 V3 接线后 ``vad`` ↔ 本模块的 import 环。校验器**只**把 schema 判定失败降级为
rejected 结果对象；模块自身缺陷不吞（调用方按本仓既有 chaperone 惯例 catch 兜底）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from core.mapping_dir import ensure_user_table, mappings_path

logger = logging.getLogger(__name__)

# ── 文件与版本 ────────────────────────────────────────────────────────────
#: 覆盖层文件名（宿主 data_dir 下）
MAPPING_FILE_NAME = "emotion_mapping.json"
#: 当前 schema 版本；顶层 ``version`` 必填 int 且需等于本值
SUPPORTED_VERSION = 1

# ── 顶层键与四张表 ────────────────────────────────────────────────────────
#: 四张数据表（顺序 = 落账 keys/counts 与合并输出的顺序）
TABLE_KEYS: Tuple[str, ...] = ("prototypes", "template_events", "multipliers", "level_thresholds")
#: 顶层键集（``version`` 必填，四表键可选）
TOP_LEVEL_KEYS: Tuple[str, ...] = ("version",) + TABLE_KEYS

# ── ① prototypes ─────────────────────────────────────────────────────────
#: 原型条目键集（恰为这四个；形状同 ``vad.prototypes_as_dict()``）
PROTOTYPE_FIELDS: Tuple[str, ...] = ("label", "valence", "arousal", "dominance")
#: 三条轴（各为 ``[lo, hi]``）
AXIS_FIELDS: Tuple[str, ...] = ("valence", "arousal", "dominance")
PROTOTYPE_MIN_COUNT = 10
PROTOTYPE_MAX_COUNT = 40
#: 三轴取值域（含端点；中心点 = ``(lo+hi)/2`` 构造性落在同域内）
AXIS_MIN = -1.0
AXIS_MAX = 1.0

# ── ② template_events ────────────────────────────────────────────────────
#: 事件白名单（**整表替换**：四个事件全需在场；其余事件名 = 未知键）
TEMPLATE_EVENT_NAMES: Tuple[str, ...] = (
    "relation.silence_detected",
    "schedule.commitment_kept",
    "schedule.commitment_broken",
    "qzone.comment_received",
)
#: 三维标量形状的事件（前三个）
EVENT_TRIPLE_NAMES: Tuple[str, ...] = TEMPLATE_EVENT_NAMES[:3]
#: 区间形状的事件（空间评论）
EVENT_RANGE_NAME: str = TEMPLATE_EVENT_NAMES[3]
#: 三维标量事件的键集
EVENT_TRIPLE_FIELDS: Tuple[str, ...] = ("valence", "arousal", "dominance")
#: 空间评论事件的键集
EVENT_RANGE_FIELDS: Tuple[str, ...] = ("valence_min", "valence_max")
#: 单事件增量硬限幅（§6.2 A：``|Δ| ≤ 0.5``）。与 ``emotion/service.py`` 的
#: ``EVENT_DELTA_CLAMP`` 同值：此处不 import service —— 本模块必须能在无 maibot_sdk 的
#: 离线环境独立导入，且 V3 接线后 service 会反向引用本模块（避免 import 环）。
#: V4 用例可断言 ``EVENT_DELTA_LIMIT == emotion.service.EVENT_DELTA_CLAMP``。
EVENT_DELTA_LIMIT = 0.5

# ── ③ multipliers ────────────────────────────────────────────────────────
#: 三层倍率的键名（**逐字同 ``emotion/config.py`` 的字段名**，不含拼写别名）
MULTIPLIER_NAMES: Tuple[str, ...] = (
    "global_event_multiplier",
    "non_target_multiplier",
    "target_bonus_step",
)
#: 各倍率的 JSON 侧闭区间 ``[lo, hi]``（与 ``[emotion]`` config 的 ge/le 对齐；
#: 下限为开区间者见 :data:`MULTIPLIER_OPEN_LOWER`）
MULTIPLIER_BOUNDS: Dict[str, Tuple[float, float]] = {
    "global_event_multiplier": (0.1, 3.0),
    "non_target_multiplier": (0.0, 1.0),
    "target_bonus_step": (0.0, 1.0),
}
#: 下限**开区间**的倍率（JSON 侧不收 0；config 侧 ge=0.0 允许 0——差异已记文档）
MULTIPLIER_OPEN_LOWER: Tuple[str, ...] = ("non_target_multiplier", "target_bonus_step")

# ── ④ level_thresholds ───────────────────────────────────────────────────
#: 四档切分点（五档描述 = 4 个阈值）
LEVEL_THRESHOLD_COUNT = 4
#: 阈值默认基线 = ``emotion/vad.py`` 的 ``_level()`` 判据（``<=-0.5`` / ``<=-0.15`` /
#: ``<0.15`` / ``<0.5``，其边界即下列四点）。**该处是函数内字面量、无模块常量可引用**，
#: 故在此声明一次；V3 接线后由 ``vad._level`` 反向引用本表 → 届时成为单一真源。
#: （自测用 ``vad._level`` 在四个边界上的实测档位回验本表与代码一致。）
DEFAULT_LEVEL_THRESHOLDS: Tuple[float, ...] = (-0.5, -0.15, 0.15, 0.5)

# ── 落账 action ──────────────────────────────────────────────────────────
#: 采纳（info）
ACTION_APPLIED = "emotion.mapping_override_applied"
#: 回退（warn）
ACTION_REJECTED = "emotion.mapping_override_rejected"

#: error_kind 全集（``version`` 族含两个具体值）
ERROR_KINDS: Tuple[str, ...] = ("parse", "version_missing", "version_unsupported",
                                "unknown_key", "incomplete_table", "type", "range")


def _is_number(value: Any) -> bool:
    """数值判定：``int``/``float`` 且**剔除 bool**（``True`` 是 int 的实例，不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    """``int`` 判定（同样剔除 bool）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _fmt(value: Any) -> str:
    """detail 里的"实得"渲染（截断超长结构，避免日志被大数组撑爆）。"""
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


def _allowed(keys: Any) -> str:
    return "、".join(str(k) for k in keys)


class _Rejected(Exception):
    """内部早退信号：**首个**校验失败即刻整文件拒绝（不收集全部错误）。"""

    def __init__(self, error_key: str, error_kind: str, detail: str) -> None:
        super().__init__(detail)
        self.error_key = error_key
        self.error_kind = error_kind
        self.detail = detail


@dataclass(frozen=True)
class MappingOverrideResult:
    """装载/校验结果（``error is None`` = 合法；各表 ``None`` = 用代码默认）。"""

    version: Optional[int] = None
    prototypes: Optional[List[Dict[str, Any]]] = None
    template_events: Optional[Dict[str, Dict[str, float]]] = None
    multipliers: Optional[Dict[str, float]] = None
    level_thresholds: Optional[List[float]] = None
    #: 实际覆盖的表名（顺序 = :data:`TABLE_KEYS`）
    applied_keys: List[str] = field(default_factory=list)
    #: 非致命告警（如 level_thresholds 相邻相等）
    warnings: List[str] = field(default_factory=list)
    #: None 或 ``{"error_key", "error_kind", "detail"}``
    error: Optional[Dict[str, str]] = None

    @property
    def ok(self) -> bool:
        """是否合法（合法 = 采纳；不合法 = 整文件回退默认）。"""
        return self.error is None

    def counts(self) -> Dict[str, int]:
        """已覆盖表的条目数（未覆盖的表不出现）。"""
        counts: Dict[str, int] = {}
        for key in TABLE_KEYS:
            value = getattr(self, key)
            if value is not None:
                counts[key] = len(value)
        return counts


def _rejected(error_key: str, error_kind: str, detail: str) -> MappingOverrideResult:
    return MappingOverrideResult(error={"error_key": error_key,
                                        "error_kind": error_kind,
                                        "detail": detail})


# ────────────────────────────────────────────────────────────────────────
# 校验（纯函数）
# ────────────────────────────────────────────────────────────────────────
def _check_version(raw: Dict[str, Any]) -> int:
    """① version 必填 int 且 == SUPPORTED_VERSION（bool 剔除）。"""
    if "version" not in raw:
        raise _Rejected("version", "version_missing",
                        f"顶层键 version：期望 int {SUPPORTED_VERSION}，实得 键缺失")
    value = raw["version"]
    if not _is_int(value) or int(value) != SUPPORTED_VERSION:
        raise _Rejected("version", "version_unsupported",
                        f"顶层键 version：期望 int {SUPPORTED_VERSION}，实得 {_fmt(value)}")
    return int(value)


def _check_top_level_keys(raw: Dict[str, Any]) -> None:
    """顶层键集（文件序取首个未知键）。"""
    for key in raw:
        if key not in TOP_LEVEL_KEYS:
            raise _Rejected(str(key), "unknown_key",
                            f"顶层键 {key!r}：未知键（允许的键集 {_allowed(TOP_LEVEL_KEYS)}）")


def _check_exact_keys(entry: Dict[str, Any], path: str, required: Tuple[str, ...]) -> None:
    """键集恰为 ``required``：先报未知键（文件序），再报漏键（``required`` 顺序）。"""
    for key in entry:
        if key not in required:
            raise _Rejected(f"{path}.{key}", "unknown_key",
                            f"{path}.{key}：未知键（该处键集恰为 {_allowed(required)}）")
    for key in required:
        if key not in entry:
            raise _Rejected(f"{path}.{key}", "incomplete_table",
                            f"{path}.{key}：键缺失（该处键集恰为 {_allowed(required)}，"
                            f"实得 {_allowed(entry.keys())}）")


def _check_span(value: Any, path: str) -> List[float]:
    """三轴之一：长度 2 的数值数组、``lo<=hi``（允许相等）、两界 ∈[-1,1]。"""
    if not isinstance(value, list) or len(value) != 2 or not all(_is_number(x) for x in value):
        raise _Rejected(path, "type",
                        f"{path}：期望 长度 2 的数值数组 [lo, hi]，实得 {_fmt(value)}")
    lo, hi = float(value[0]), float(value[1])
    if lo > hi:
        raise _Rejected(path, "range", f"{path}：期望 lo<=hi（允许相等），实得 {_fmt(value)}")
    if not (AXIS_MIN <= lo <= AXIS_MAX and AXIS_MIN <= hi <= AXIS_MAX):
        raise _Rejected(path, "range",
                        f"{path}：期望 两界 ∈[{AXIS_MIN}, {AXIS_MAX}]，实得 {_fmt(value)}")
    return [lo, hi]


def _check_prototypes(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """① 原型表：10~40 条，键集恰为 label + 三轴（轴为长度 2 数组，lo<=hi，∈[-1,1]）。

    输出的 ``label`` 已 strip（判重与输出同口径的去空白规范化）。
    """
    value = raw["prototypes"]
    if not isinstance(value, list):
        raise _Rejected("prototypes", "type",
                        f"prototypes：期望 数组，实得 {_fmt(value)}")
    count = len(value)
    if not PROTOTYPE_MIN_COUNT <= count <= PROTOTYPE_MAX_COUNT:
        raise _Rejected("prototypes", "range",
                        f"prototypes：期望 条目数 {PROTOTYPE_MIN_COUNT}~{PROTOTYPE_MAX_COUNT}，"
                        f"实得 {count}")
    out: List[Dict[str, Any]] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(value):
        path = f"prototypes[{index}]"
        if not isinstance(item, dict):
            raise _Rejected(path, "type", f"{path}：期望 对象，实得 {_fmt(item)}")
        _check_exact_keys(item, path, PROTOTYPE_FIELDS)
        label = item["label"]
        if not isinstance(label, str):
            raise _Rejected(f"{path}.label", "type",
                            f"{path}.label：期望 非空字符串，实得 {_fmt(label)}")
        normalized = label.strip()
        if not normalized:
            raise _Rejected(f"{path}.label", "range",
                            f"{path}.label：期望 strip 去空白后非空，实得 {_fmt(label)}")
        if normalized in seen:
            raise _Rejected(f"{path}.label", "range",
                            f"{path}.label：期望 去空白后标签互不重复，实得 {_fmt(label)} "
                            f"与 prototypes[{seen[normalized]}].label 重复")
        seen[normalized] = index
        entry: Dict[str, Any] = {"label": normalized}
        for axis in AXIS_FIELDS:
            entry[axis] = _check_span(item[axis], f"{path}.{axis}")
        out.append(entry)
    return out


def _check_event_delta(value: Any, path: str) -> float:
    """事件增量：数值标量（bool 剔除）、``|值| ≤ EVENT_DELTA_LIMIT``。

    ``if not abs(number) <= EVENT_DELTA_LIMIT`` 的写法**故意不同于** ``abs(x) > limit``：
    ``json.loads`` 默认接受 ``NaN``/``Infinity`` 字面量，NaN 的任何比较均为 False，
    "大于才拒"会让 NaN 穿过判据被当作合法标量采纳（与本模块"非法整表回退"的目的相悖）；
    取"仅当满足上界判据才收"的口径后 NaN 与 ±Infinity 一律 range 拒绝（与 ①/③/④ 一致）。
    """
    if not _is_number(value):
        raise _Rejected(path, "type", f"{path}：期望 数值标量，实得 {_fmt(value)}")
    number = float(value)
    if not abs(number) <= EVENT_DELTA_LIMIT:
        raise _Rejected(path, "range",
                        f"{path}：期望 |值|≤{EVENT_DELTA_LIMIT}（EVENT_DELTA_CLAMP），"
                        f"实得 {_fmt(value)}")
    return number


def _check_template_events(raw: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """② 模板事件表：白名单恰 4 事件且**全需在场**（整表替换）。"""
    value = raw["template_events"]
    if not isinstance(value, dict):
        raise _Rejected("template_events", "type",
                        f"template_events：期望 对象，实得 {_fmt(value)}")
    for name in value:  # 未知事件名（文件序）
        if name not in TEMPLATE_EVENT_NAMES:
            raise _Rejected(f"template_events.{name}", "unknown_key",
                            f"template_events.{name}：未知事件名"
                            f"（白名单 {_allowed(TEMPLATE_EVENT_NAMES)}）")
    for name in TEMPLATE_EVENT_NAMES:  # 4 事件全需在场
        if name not in value:
            raise _Rejected(f"template_events.{name}", "incomplete_table",
                            f"template_events.{name}：键缺失（整表替换要求 4 个白名单事件"
                            f"全在场，实得 {_allowed(value.keys())}）")
    out: Dict[str, Dict[str, float]] = {}
    for name in EVENT_TRIPLE_NAMES:
        path = f"template_events.{name}"
        entry = value[name]
        if not isinstance(entry, dict):
            raise _Rejected(path, "type", f"{path}：期望 对象，实得 {_fmt(entry)}")
        _check_exact_keys(entry, path, EVENT_TRIPLE_FIELDS)
        out[name] = {axis: _check_event_delta(entry[axis], f"{path}.{axis}")
                     for axis in EVENT_TRIPLE_FIELDS}
    path = f"template_events.{EVENT_RANGE_NAME}"
    entry = value[EVENT_RANGE_NAME]
    if not isinstance(entry, dict):
        raise _Rejected(path, "type", f"{path}：期望 对象，实得 {_fmt(entry)}")
    _check_exact_keys(entry, path, EVENT_RANGE_FIELDS)
    vmin = _check_event_delta(entry["valence_min"], f"{path}.valence_min")
    vmax = _check_event_delta(entry["valence_max"], f"{path}.valence_max")
    if vmin > vmax:
        raise _Rejected(f"{path}.valence_min", "range",
                        f"{path}：期望 valence_min<=valence_max，实得 {_fmt(entry)}")
    out[EVENT_RANGE_NAME] = {"valence_min": vmin, "valence_max": vmax}
    return out


def _check_multipliers(raw: Dict[str, Any]) -> Dict[str, float]:
    """③ 三层倍率：3 键全需在场、键名逐字同 config 字段名、范围见 §③。"""
    value = raw["multipliers"]
    if not isinstance(value, dict):
        raise _Rejected("multipliers", "type",
                        f"multipliers：期望 对象，实得 {_fmt(value)}")
    _check_exact_keys(value, "multipliers", MULTIPLIER_NAMES)
    out: Dict[str, float] = {}
    for name in MULTIPLIER_NAMES:
        path = f"multipliers.{name}"
        item = value[name]
        if not _is_number(item):
            raise _Rejected(path, "type", f"{path}：期望 数值，实得 {_fmt(item)}")
        lo, hi = MULTIPLIER_BOUNDS[name]
        number = float(item)
        if name in MULTIPLIER_OPEN_LOWER:
            if not lo < number <= hi:
                raise _Rejected(path, "range",
                                f"{path}：期望 ∈({lo}, {hi}]（下限开区间，不收 0），"
                                f"实得 {_fmt(item)}")
        elif not lo <= number <= hi:
            raise _Rejected(path, "range",
                            f"{path}：期望 ∈[{lo}, {hi}]，实得 {_fmt(item)}")
        out[name] = number
    return out


def _check_level_thresholds(raw: Dict[str, Any]) -> Tuple[List[float], List[str]]:
    """④ 五档阈值：长度恰 4、各项 ∈[-1,1]、单调不减；相邻相等**只告警**。"""
    value = raw["level_thresholds"]
    if not isinstance(value, list):
        raise _Rejected("level_thresholds", "type",
                        f"level_thresholds：期望 数组，实得 {_fmt(value)}")
    if len(value) != LEVEL_THRESHOLD_COUNT:
        raise _Rejected("level_thresholds", "range",
                        f"level_thresholds：期望 长度恰 {LEVEL_THRESHOLD_COUNT}，"
                        f"实得 {len(value)}")
    numbers: List[float] = []
    for index, item in enumerate(value):
        path = f"level_thresholds[{index}]"
        if not _is_number(item):
            raise _Rejected(path, "type", f"{path}：期望 数值，实得 {_fmt(item)}")
        number = float(item)
        if not AXIS_MIN <= number <= AXIS_MAX:
            raise _Rejected(path, "range",
                            f"{path}：期望 ∈[{AXIS_MIN}, {AXIS_MAX}]，实得 {_fmt(item)}")
        numbers.append(number)
    warnings: List[str] = []
    for index in range(len(numbers) - 1):
        current, following = numbers[index], numbers[index + 1]
        if current > following:
            raise _Rejected(f"level_thresholds[{index + 1}]", "range",
                            f"level_thresholds：期望 单调不减（c{index}<=c{index + 1}），"
                            f"实得 {_fmt(value)}")
        if current == following:
            warnings.append(f"level_thresholds：相邻阈值相等"
                            f"（c{index}==c{index + 1}=={current!r}）——该档位为空，"
                            f"仅告警不拒绝")
    return numbers, warnings


def validate_document(raw: Any) -> MappingOverrideResult:
    """校验已解析的 JSON 文档（**纯函数**，不碰文件系统），返回结果对象。

    合法 → 各表为校验后的表（未在场的表 ``None`` = 用代码默认）、``applied_keys`` 为实际
    覆盖的表名；不合法 → 四表全 ``None``、``applied_keys=[]``、``error`` 带首个出错键。
    本函数只把 schema 判定失败降级为 rejected；模块自身缺陷不吞（直接冒泡）。
    """
    try:
        if not isinstance(raw, dict):
            raise _Rejected("", "type", f"顶层：期望 JSON 对象，实得 {_fmt(raw)}")
        version = _check_version(raw)
        _check_top_level_keys(raw)
        prototypes = _check_prototypes(raw) if "prototypes" in raw else None
        template_events = _check_template_events(raw) if "template_events" in raw else None
        multipliers = _check_multipliers(raw) if "multipliers" in raw else None
        level_thresholds: Optional[List[float]] = None
        warnings: List[str] = []
        if "level_thresholds" in raw:
            level_thresholds, warnings = _check_level_thresholds(raw)
    except _Rejected as exc:
        return _rejected(exc.error_key, exc.error_kind, exc.detail)
    return MappingOverrideResult(
        version=version,
        prototypes=prototypes,
        template_events=template_events,
        multipliers=multipliers,
        level_thresholds=level_thresholds,
        applied_keys=[key for key in TABLE_KEYS if key in raw],
        warnings=warnings,
    )


# ────────────────────────────────────────────────────────────────────────
# 默认基线（只引用现有常量，不复制数值）
# ────────────────────────────────────────────────────────────────────────
def _config_multiplier_defaults() -> Optional[Dict[str, float]]:
    """``[emotion]`` 三层倍率的**字段默认值**（``emotion/config.py`` 的 Field default）。

    延迟导入 ``emotion/config.py``（该模块 import maibot_sdk）：取不到时返回 ``None``
    （只影响 :func:`default_tables` 的 ``multipliers``，不影响校验与合并语义）。
    """
    try:
        from .config import EmotionSectionConfig
    except Exception as exc:  # sdk 缺失 / 循环导入期等
        logger.debug("emotion mapping override：config 默认值不可得（%s）", exc)
        return None
    fields = getattr(EmotionSectionConfig, "model_fields", None)  # pydantic v2
    if not isinstance(fields, dict):
        fields = getattr(EmotionSectionConfig, "__fields__", None)  # pydantic v1
    if not isinstance(fields, dict):
        return None
    out: Dict[str, float] = {}
    for name in MULTIPLIER_NAMES:
        info = fields.get(name)
        default = getattr(info, "default", None)
        if not _is_number(default):
            return None
        out[name] = float(default)
    return out


def default_tables() -> Dict[str, Any]:
    """代码默认基线（四张表），**只引用现有常量**：

    - ``prototypes``       ← ``emotion/vad.py`` 的 ``EMOTION_PROTOTYPES``（``prototypes_as_dict()``）；
    - ``template_events``  ← ``emotion/bridge.py`` 的模板增量常量（沉默/兑现/违约/空间评论区间）；
    - ``multipliers``      ← ``emotion/config.py`` 的字段默认值（取不到时 ``None``，见上）；
    - ``level_thresholds`` ← :data:`DEFAULT_LEVEL_THRESHOLDS`（``vad._level`` 判据的声明位）。

    ``vad``/``bridge`` 也在函数体内延迟导入：V3 接线后这两个模块会反向引用本模块，
    顶层导入会成环。每次调用重建（廉价），不做缓存——热重载后不会读到陈旧基线。
    """
    from . import vad
    from .bridge import (COMMITMENT_BROKEN_DELTA, COMMITMENT_KEPT_DELTA, QZONE_COMMENT_V_MAX,
                         QZONE_COMMENT_V_MIN, SILENCE_DELTA)

    def _triple(delta: Any) -> Dict[str, float]:
        return {"valence": float(delta.valence), "arousal": float(delta.arousal),
                "dominance": float(delta.dominance)}

    return {
        "prototypes": vad.prototypes_as_dict(),
        "template_events": {
            "relation.silence_detected": _triple(SILENCE_DELTA),
            "schedule.commitment_kept": _triple(COMMITMENT_KEPT_DELTA),
            "schedule.commitment_broken": _triple(COMMITMENT_BROKEN_DELTA),
            "qzone.comment_received": {"valence_min": float(QZONE_COMMENT_V_MIN),
                                       "valence_max": float(QZONE_COMMENT_V_MAX)},
        },
        "multipliers": _config_multiplier_defaults(),
        "level_thresholds": [float(x) for x in DEFAULT_LEVEL_THRESHOLDS],
    }


def __getattr__(name: str) -> Any:
    """PEP 562：``DEFAULTS`` 惰性构建（避免导入期就把 vad/bridge/config 拉进来）。

    消费方要跨热重载取最新基线时，直接调用 :func:`default_tables`。
    """
    if name == "DEFAULTS":
        return default_tables()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ────────────────────────────────────────────────────────────────────────
# 合并（纯函数）
# ────────────────────────────────────────────────────────────────────────
def merge_table(default: Any, override: Any) -> Any:
    """单表合并：``override is None`` → 默认；否则**整表替换**（不逐键合并）。"""
    return default if override is None else override


def merge_tables(override: Optional[MappingOverrideResult],
                 defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """默认 ⊕ 覆盖（纯函数）：返回四张表的最终值，供 V3 接线消费。

    ``override`` 为 ``None``/被拒绝（``error`` 非空）→ 全量默认；单表 ``None`` → 该表默认。
    """
    base = default_tables() if defaults is None else dict(defaults)
    out: Dict[str, Any] = {}
    for key in TABLE_KEYS:
        if override is None or override.error is not None:
            out[key] = base.get(key)
            continue
        out[key] = merge_table(base.get(key), getattr(override, key))
    return out


def prototype_centers(prototypes: Optional[List[Dict[str, Any]]],
                      ) -> List[Tuple[str, Tuple[float, float, float]]]:
    """原型中心点列表 ``[(label, (v, a, d)), ...]``——中心 = 各轴区间中点 ``(lo+hi)/2``。

    与 ``emotion/vad.py`` 的 ``_PROTO_CENTERS`` 同口径；因 ``lo/hi ∈[-1,1]`` 且 ``lo<=hi``，
    中心构造性落在 ``[-1,1]``（无需再裁）。输入按 :func:`validate_document` 的输出形状。
    """
    out: List[Tuple[str, Tuple[float, float, float]]] = []
    for item in prototypes or []:
        label = str(item.get("label", "")).strip()
        valence = item["valence"]
        arousal = item["arousal"]
        dominance = item["dominance"]
        center = ((float(valence[0]) + float(valence[1])) / 2,
                  (float(arousal[0]) + float(arousal[1])) / 2,
                  (float(dominance[0]) + float(dominance[1])) / 2)
        out.append((label, center))
    return out


# ────────────────────────────────────────────────────────────────────────
# 装载入口
# ────────────────────────────────────────────────────────────────────────
def _read_document(path: Path) -> Any:
    """读取并解析覆盖文件；读取/解析失败统一 ``_Rejected(error_kind="parse")``。

    显式 ``json.loads``（不用 ``core/storage.load_json``：后者吞 ``JSONDecodeError``，
    无法区分"损坏"与"缺文件"）。UTF-8 文本；容忍一个前导 BOM（Windows 记事本另存）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _Rejected("", "parse", f"非 UTF-8 文本（{path.name}）：{exc}") from exc
    except OSError as exc:  # 权限/目录/被占用等
        raise _Rejected("", "parse", f"读取失败（{path.name}）：{exc}") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise _Rejected("", "parse",
                        f"JSON 解析失败（{path.name} 行 {exc.lineno} 列 {exc.colno}）："
                        f"{exc.msg}") from exc


def _record_outcome(result: MappingOverrideResult, decision_logger: Any) -> None:
    """两 action 落账（``DecisionLogger.record``）；logger 为空/异常时静默跳过。"""
    if decision_logger is None:
        return
    record = getattr(decision_logger, "record", None)
    if not callable(record):
        return
    if result.error is not None:
        action = ACTION_REJECTED
        fields: Dict[str, Any] = {
            "severity": "warn",
            "error_key": result.error["error_key"],
            "error_kind": result.error["error_kind"],
            "detail": result.error["detail"],
        }
    else:
        action = ACTION_APPLIED
        fields = {
            "severity": "info",
            "version": result.version,
            "keys": list(result.applied_keys),
            "counts": result.counts(),
        }
        if result.warnings:
            fields["warnings"] = list(result.warnings)
    try:
        record(action=action, **fields)
    except Exception:  # 落账故障绝不影响装载结果（DecisionLogger 自身也吞写入异常）
        logger.debug("emotion mapping override：落账 %s 异常（忽略）", action, exc_info=True)


ROOT_MAPPING_FILE_NAME = "emotion_mapping.json"
#: 插件根目录默认映射表文件（F4 2026-10-11）：缺失时由内置四表生成，用户可编辑
#: （2026-10-17 起实际读取位 = ``<root>/mappings/``，见 :func:`load_effective`；
#: 路径拼接唯一真源 = ``core/mapping_dir.mappings_path``——§19.4 / 复核 findings#9
#: 仅补注释，常量名不变）


def load_effective(data_dir: Union[str, Path], decision_logger: Any = None,
                   *, root_dir: Any = None) -> MappingOverrideResult:
    """F4（裁决 2026-10-11）：插件根映射表优先 → data_dir 覆盖层（M12）→ 内置默认。

    2026-10-17 起根表读取位 = ``<root>/mappings/emotion_mapping.json``（映射表集中子文件夹）：
    - 新位置缺失 → :func:`core.mapping_dir.ensure_user_table` 补齐（§29.2：旧根文件
      ``<root>/emotion_mapping.json`` 优先迁移；仍缺才从出厂种子 ``mappings/default_mappings/``
      复制；解析错误不算缺失、绝不覆盖用户文件），随后按新位置读取；
    - 根文件存在 → 校验：合法即生效（**不再叠加** data_dir 覆盖层——根文件是唯一
      用户编辑位）；非法 → 告警并回退**内置默认**（用户口径「格式有误时回退为
      使用默认的映射表」，不落入 data_dir 以免双重覆盖）；
    - 根文件缺失 → best-effort 生成内置默认文件（供用户编辑），并继续走
      data_dir 覆盖层（M12 兼容）→ 内置默认；
    - ``root_dir=None``（测试桩/无插件根信息的调用方）→ **跳过根层**，纯走
      data_dir 覆盖层（M12 行为逐字不变；根层仅由真实插件实例启用）；
    - 结果落账与 :func:`load` 同口径。
    """
    if root_dir is None:
        return load(data_dir, decision_logger)
    root = Path(root_dir)
    root_path = mappings_path(root, ROOT_MAPPING_FILE_NAME)
    try:
        present = root_path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2（2026-10-23 直派）：migrate_legacy_root_file 调用点升级为 ensure_user_table
        # （旧根迁移仍优先，表层仍缺才从出厂种子映射表复制；解析错误不算缺失）
        ensure_user_table(root, ROOT_MAPPING_FILE_NAME)
        try:
            present = root_path.exists()
        except OSError:
            present = False
    if not present:
        _generate_default_root(root_path)
        return load(data_dir, decision_logger)
    try:
        raw = _read_document(root_path)
    except _Rejected as exc:
        logger.warning("插件根目录映射表非法（%s：%s）→ 回退内置默认（不叠加 data_dir 覆盖层）",
                       exc.error_kind, exc.detail)
        result = _rejected(exc.error_key, exc.error_kind, exc.detail)
        _record_outcome(result, decision_logger)
        return result
    result = validate_document(raw)
    _record_outcome(result, decision_logger)
    return result


def _generate_default_root(path: Path) -> None:
    """生成插件根目录默认映射表（内置四表；best-effort，失败仅告警）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"version": 1, **default_tables()}
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("已生成默认情绪映射表：%s（可自行编辑，格式错误将回退内置默认）", path)
    except Exception as exc:
        logger.warning("生成默认情绪映射表失败（忽略）：%s", exc)


def load(data_dir: Union[str, Path], decision_logger: Any = None,
         *, file_name: str = MAPPING_FILE_NAME) -> MappingOverrideResult:
    """装载 ``<data_dir>/<file_name>`` 并校验（**只读**，不写任何文件）。

    - 文件不存在 → 静默纯默认（各表 ``None``、``applied_keys=[]``、``error=None``、**零 action**）；
    - 存在 → 读取 + 校验：合法落 ``ACTION_APPLIED``，非法落 ``ACTION_REJECTED`` 并全量回退默认；
    - ``decision_logger=None`` 时跳过落账，返回值不变。

    返回值见 :class:`MappingOverrideResult`。
    """
    path = Path(data_dir) / file_name
    try:
        present = path.exists()
    except OSError:  # 路径不可判定 → 按不存在处理（不因数据目录异常拖挂装载）
        present = False
    if not present:
        logger.debug("emotion mapping override：%s 不存在 → 纯代码默认（不落 action）", path)
        return MappingOverrideResult()
    try:
        raw = _read_document(path)
    except _Rejected as exc:
        result = _rejected(exc.error_key, exc.error_kind, exc.detail)
    else:
        result = validate_document(raw)
    _record_outcome(result, decision_logger)
    return result
