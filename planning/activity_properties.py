"""日程属性表（实现前冻结口径 = MAINTENANCE_PLAN_10.05 §24.3 任务C；R7 依据 = 用户
2026-10-21 直派）：mappings/ 第五张表，活动**回复率调控倍率**改按「日程属性」取值。

背景：原 ``proactive_service._FREQUENCY_FACTOR_BY_GOAL_TYPE`` 频率表按 goal_type 一维
映射倍率，且 free_time/custom 缺位（A-21 ③④，随本轮收敛）——倍率与关键词词表全部
搬进 ``mappings/schedule_properties.json``，用户改 json 即可调频倍率，不再动代码。

仿 ``companion/affection_presets.py``（§19 亲和表）的加载契约：

- 文件：``<插件根>/mappings/schedule_properties.json``（读取位统一走
  ``core.mapping_dir.mappings_path``，禁止其他位置）；结构::

      {"version": 1, "properties": [...], "default_property": {...}}

  属性条目字段 ``id/label/goal_types[]/keywords[]/reply_factor/color``；
- **匹配口径（确定性、纯离线，零 LLM 调用点）**：``resolve_property(goal_type, name)``
  ① ``goal_type`` 精确命中优先（属性数组序先到先得，未收录的 goal_type 跳过）→
  ② 条目 name 含 ``property.keywords`` 子串（数组序）→ ③ ``default_property`` 兜底。
  夜间词表（睡觉/睡眠/安睡）进 sleep 属性 keywords、午休词表（午休/午睡/小憩/打盹）
  进 rest 属性 keywords——与 ``schedule_items.NIGHT_SLEEP_NAME_KEYWORDS`` /
  ``companion.patrol.NAP_NAME_KEYWORDS`` 的 B2/M9 口径**独立保留**（不迁移不替换）；
- **校验三粒度**：顶层级坏（json 不可读 / version≠1 / 顶层非对象 / 未知顶层键）→
  整文件回退内置；单属性坏（缺 id/label、``reply_factor`` 非数值或 bool/NaN/Inf 或
  超 [0, 2]、``goal_types``/``keywords`` 非字符串数组、id/label 重复）→ **仅排除该条**
  并落账；条目内未知键 → 丢弃该键其余生效；坏掉的 ``default_property`` → 回退内置
  默认条目并落账；
- 合并语义：json 同 id 覆盖内置同 id（内置顺序不变、原位替换），新 id 追加在数组尾
  ——json 可只写想改的条目，其余内置倍率照旧；
- 加载时机：**只在 on_load/热重载读一遍**（``PlanningServices`` 构造时装载；热重载=
  宿主重导入重跑 on_load，同 emotion 说表/CompanionServices 同款）；``root_dir=None``
  （测试桩）→ 跳过根层纯内置；缺失文件 best-effort 生成内置默认（失败仅告警）；
- 常驻内存 = 模块级 :data:`_ACTIVE` + :func:`apply_tables` 重绑；
  落账 ``planning.properties_applied`` / ``planning.properties_rejected``；
- 导入期安全：本模块顶层**只依赖标准库**（``core.mapping_dir`` 延迟到函数体内），
  planning 与渲染侧可安全引用，无导入环风险；渲染取色纯字符串解析，不回写。
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 文件与版本 ──────────────────────────────────────────────────────────
#: 属性表文件名（实际读取位 = ``<root>/mappings/``，经 core.mapping_dir）
PROPERTIES_FILE_NAME = "schedule_properties.json"
#: 顶层 ``version`` 必填 int 且需等于本值（bool 不算 int）
SUPPORTED_VERSION = 1
#: ``reply_factor`` 取值区间（§24.3 冻结：数值、非 bool、非 NaN/Inf、∈[0, 2]）
FACTOR_MIN = 0.0
FACTOR_MAX = 2.0
#: 条目固定字段集（未知键丢弃该键、其余生效——粒度 §24.3 原文）
PROPERTY_KEYS: Tuple[str, ...] = ("id", "label", "goal_types", "keywords", "reply_factor", "color")
#: 展示标签的长度纪律（沿用 _cap 思路的定长截断；推荐 2~3 字，硬件上限 4 字防排版挤爆）
LABEL_MAX_CHARS = 4


# ── 数据模型 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActivityProperty:
    """单条日程属性：标签 + 匹配集合 + 回复率倍率 + 展示色。

    ``color`` = ``"#RRGGBB"`` 十六进制字符串（渲染侧非法/缺失回退
    ``schedule_image.TYPE_COLORS[goal_type]`` → ``DEFAULT_TYPE_COLOR``）。
    """

    id: str
    label: str
    goal_types: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    reply_factor: float = 1.0
    color: str = ""

    def reply_tag(self) -> str:
        """``[标签]`` 短标签（长度纪律：截到 :data:`LABEL_MAX_CHARS`；空 label → 空串）。"""
        text = str(self.label or "").strip()
        if not text:
            return ""
        return f"[{text[:LABEL_MAX_CHARS]}]"


@dataclass(frozen=True)
class PropertyTable:
    """生效属性表（合并后的只读视图）：有序属性数组 + 兜底条目。"""

    properties: Tuple[ActivityProperty, ...]
    default_property: ActivityProperty

    def infer(self, goal_type: Any, name: Any) -> ActivityProperty:
        """§24.3 匹配口径：goal_type 精确命中（数组序）→ keywords 子串（数组序）→ 兜底。

        ``name`` 非字符串（垃圾数据）只按 ``str()`` 收一次子串匹配；匹配失败恒返回
        ``default_property``——调用方**永远拿到一条合法条目**（free_time/1.0 兜底）。
        """
        gt = str(goal_type or "").strip()
        text = str(name or "")
        for prop in self.properties:
            if gt and gt in prop.goal_types:
                return prop
        for prop in self.properties:
            if any(keyword and keyword in text for keyword in prop.keywords):
                return prop
        return self.default_property

    def by_id(self, property_id: Any) -> Optional[ActivityProperty]:
        """按 id 精确取条目（调试/展示用；未收录 → ``None``）。"""
        key = str(property_id or "").strip()
        for prop in self.properties:
            if prop.id == key:
                return prop
        return None


@dataclass(frozen=True)
class PropertiesLoadResult:
    """装载/校验结果（``error is None`` = 顶层级合法）。

    ``accepted`` = json 合法条目（合并前）；
    ``rejected`` = 被排除条目的 ``(展示名, 原因)`` 留账；
    ``warnings`` = 非致命提示（properties/default_property 键缺失、未知键丢弃、内联坏条目等）。
    """

    accepted: Tuple[ActivityProperty, ...] = ()
    default_property: Optional[ActivityProperty] = None
    rejected: Tuple[Tuple[str, str], ...] = ()
    warnings: Tuple[str, ...] = ()
    error: Optional[Dict[str, str]] = None
    generated_default: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


# ── 内置默认（§24.3：数值语义 = 旧 _FREQUENCY_FACTOR_BY_GOAL_TYPE 逐条迁移）──
#: 内置默认倍率（语义对齐说明写进 README；已从 proactive_service 收口至此单一来源）
BUILTIN_PROPERTIES: Tuple[ActivityProperty, ...] = (
    ActivityProperty(id="sleep", label="睡觉", goal_types=("sleep",),
                     keywords=("睡觉", "睡眠", "安睡"), reply_factor=0.05, color="#5A6982"),
    ActivityProperty(id="study", label="学习", goal_types=("study",),
                     keywords=("学习", "功课", "复习"), reply_factor=0.3, color="#4A90E2"),
    ActivityProperty(id="work", label="工作", goal_types=("work",),
                     keywords=("工作", "上班"), reply_factor=0.3, color="#6E7F95"),
    ActivityProperty(id="learn_topic", label="兴趣", goal_types=("learn_topic",),
                     keywords=("兴趣", "课题"), reply_factor=0.5, color="#00A8A8"),
    ActivityProperty(id="exercise", label="运动", goal_types=("exercise",),
                     keywords=("锻炼", "健身", "跑步"), reply_factor=0.7, color="#27AE60"),
    ActivityProperty(id="meal", label="用餐", goal_types=("meal",),
                     keywords=("早餐", "早膳", "午餐", "午饭", "晚饭", "晚餐", "宵夜", "吃饭"),
                     reply_factor=1.2, color="#F2994A"),
    ActivityProperty(id="rest", label="休息", goal_types=("rest",),
                     keywords=("午休", "午睡", "小憩", "打盹", "休息"),
                     reply_factor=1.3, color="#A3B7CE"),
    ActivityProperty(id="daily_routine", label="日常", goal_types=("daily_routine",),
                     keywords=("起床", "洗漱", "通勤"), reply_factor=1.0, color="#7F8FA6"),
    ActivityProperty(id="entertainment", label="娱乐", goal_types=("entertainment",),
                     keywords=("游戏", "电影", "动漫", "逛街"), reply_factor=1.5, color="#9B51E0"),
    ActivityProperty(id="social_maintenance", label="社交", goal_types=("social_maintenance",),
                     keywords=("聚会", "联络"), reply_factor=1.5, color="#EB568E"),
)
#: 内置兜底条目（free_time/custom 1.0——A-21 ③「频率表缺 free_time/custom」随本轮收敛）
BUILTIN_DEFAULT_PROPERTY = ActivityProperty(
    id="free_time", label="空闲", goal_types=("free_time", "custom"),
    keywords=(), reply_factor=1.0, color="#6D8FB0")


def builtin_table() -> PropertyTable:
    """内置默认属性表（§24.3：语义 = 旧频率表逐条迁移 + free_time/custom 兜底收口）。"""
    return PropertyTable(properties=BUILTIN_PROPERTIES, default_property=BUILTIN_DEFAULT_PROPERTY)


# ── 工具 ─────────────────────────────────────────────────────────────────
def _is_number(value: Any) -> bool:
    """数值判定：int/float 且剔除 bool（``True`` 是 int 的实例，不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


def _valid_color(value: Any) -> bool:
    """``#RRGGBB`` 十六进制色判定（``color`` 非 null 即须合法；``""``/null = 未设色）。"""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return True
    if not (len(text) == 7 and text.startswith("#")):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in text[1:])


# ── 校验（纯函数，仿 affection_presets 的两级早退信号）────────────────────
class _Rejected(Exception):
    """顶层级早退信号：整文件拒绝、全量回退内置。"""

    def __init__(self, error_key: str, error_kind: str, detail: str) -> None:
        super().__init__(detail)
        self.error_key = error_key
        self.error_kind = error_kind
        self.detail = detail


class _EntryRejected(Exception):
    """条目级早退信号：**仅排除该条**，其余条目继续校验。"""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name
        self.detail = detail


def _check_version(raw: Dict[str, Any]) -> None:
    if "version" not in raw:
        raise _Rejected("version", "version_missing",
                        f"顶层键 version：期望 int {SUPPORTED_VERSION}，实得 键缺失")
    value = raw["version"]
    if not isinstance(value, int) or isinstance(value, bool) or int(value) != SUPPORTED_VERSION:
        raise _Rejected("version", "version_unsupported",
                        f"顶层键 version：期望 int {SUPPORTED_VERSION}，实得 {_fmt(value)}")


def _check_top_level_keys(raw: Dict[str, Any]) -> None:
    for key in raw:
        if key not in ("version", "properties", "default_property"):
            raise _Rejected(str(key), "unknown_key",
                            f"顶层键 {key!r}：未知键（允许的键集 version、properties、default_property）")


def _check_strings_array(raw: Any, path: str, name: str, *, allow_empty: bool = True) -> Tuple[str, ...]:
    """字符串数组校验（``goal_types``/``keywords`` 共用）；非数组/含非字符串 → 条目排除）。"""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _EntryRejected(name, f"{path}：期望 字符串数组，实得 {_fmt(raw)}")
    out: List[str] = []
    seen: set = set()
    for index, item in enumerate(raw):
        if not isinstance(item, str):
            raise _EntryRejected(name, f"{path}[{index}]：期望 字符串元素，实得 {_fmt(item)}")
        text = item.strip()
        if not text:
            if not allow_empty:
                raise _EntryRejected(name, f"{path}[{index}]：期望 非空字符串，实得 {_fmt(item)}")
            continue
        if text in seen:
            continue  # 重复元素静默去重（保留首项），不算错误
        seen.add(text)
        out.append(text)
    return tuple(out)


def _check_factor(raw: Any, path: str, name: str) -> float:
    """``reply_factor``：数值（非 bool）、非 NaN/Inf、∈[0, 2]；缺失/坏值 → 条目排除。"""
    if not _is_number(raw):
        raise _EntryRejected(name, f"{path}：期望 数值（bool/字符串/缺失一律拒绝），实得 {_fmt(raw)}")
    number = float(raw)
    if not (FACTOR_MIN <= number <= FACTOR_MAX):  # 仅当满足上界判据才收（NaN 比较恒 False → 拒）
        if math.isnan(number) or math.isinf(number):
            raise _EntryRejected(name, f"{path}：期望 ∈[{FACTOR_MIN:g}, {FACTOR_MAX:g}]"
                                        f"（NaN/Infinity 一律拒绝），实得 {_fmt(raw)}")
        raise _EntryRejected(name, f"{path}：期望 ∈[{FACTOR_MIN:g}, {FACTOR_MAX:g}]，实得 {_fmt(raw)}")
    return number


_ENTRY_REJECT_NAME_DEFAULT = "default_property"


def _check_property(raw: Any, name_hint: str, *, prefix: Optional[str] = None) -> ActivityProperty:
    """单条目校验：失败抛 :class:`_EntryRejected`（由调用方捕获为排除该条）。

    ``color`` 位**不参与条目排除**（§24.3 冻结排除清单未含色位；非法色由渲染侧
    回退链兜住），由调用方在收编前规整为 "" 并落 warning。
    """
    if prefix is None:
        prefix = f"properties[{name_hint}]"
    if not isinstance(raw, dict):
        raise _EntryRejected(name_hint, f"{prefix}：期望 对象，实得 {_fmt(raw)}")
    unknown = [k for k in raw if k not in PROPERTY_KEYS]
    for key in unknown:
        logger.debug("属性条目 %s 未知键 %r 已丢弃（其余键继续生效）", prefix, key)
    # 留账名随既有合法 id（id 缺失/空白时退回数组下标 name_hint）
    pid_raw = raw.get("id")
    pid = pid_raw.strip() if isinstance(pid_raw, str) and pid_raw.strip() else ""
    display = pid or str(name_hint)
    for key in ("id", "label"):
        if key not in raw:
            raise _EntryRejected(display, f"{prefix}.{key}：键缺失（该处键集 id、label 必填）")
    if not pid:
        raise _EntryRejected(name_hint, f"{prefix}.id：期望 非空字符串，实得 {_fmt(pid_raw)}")
    label = raw["label"]
    if not isinstance(label, str) or not label.strip():
        raise _EntryRejected(display, f"{prefix}.label：期望 非空字符串，实得 {_fmt(label)}")
    factor = _check_factor(raw.get("reply_factor"), f"{prefix}.reply_factor", display)
    goal_types = _check_strings_array(raw.get("goal_types"), f"{prefix}.goal_types", display)
    keywords = _check_strings_array(raw.get("keywords"), f"{prefix}.keywords", display)
    color_raw = raw.get("color")
    color = color_raw.strip() if isinstance(color_raw, str) else ""
    if not _valid_color(color):
        color = ""  # 非法色位丢弃（条目保留），渲染走 TYPE_COLORS → 兜底回退链
    return ActivityProperty(id=pid.strip(), label=label.strip(), goal_types=goal_types,
                            keywords=keywords, reply_factor=factor, color=color)


def validate_properties_document(raw: Any) -> PropertiesLoadResult:
    """校验已解析的属性文档（**纯函数**，不碰文件系统）。

    顶层级失败 → ``error``（整文件回退）；条目级失败 → 该条进 ``rejected``、其余保留；
    ``properties`` / ``default_property`` 键缺失 → 合法但为空 + warning（与「无自定义」
    同语义）；条目未知键丢弃该键（warn 留账）；坏 ``default_property`` → 回退内置兜底
    条目并落账 rejected（不整退，其余条目照常生效）。
    """
    try:
        if not isinstance(raw, dict):
            raise _Rejected("", "type", f"顶层：期望 JSON 对象，实得 {_fmt(raw)}")
        _check_version(raw)
        _check_top_level_keys(raw)
    except _Rejected as exc:
        return PropertiesLoadResult(error={"error_key": exc.error_key,
                                           "error_kind": exc.error_kind,
                                           "detail": exc.detail})
    warnings: List[str] = []
    _raw_props = raw.get("properties")
    for _where, _raw in (("properties", _raw_props),
                         ("default_property", raw.get("default_property"))):
        if isinstance(_raw, dict):
            for key in _raw:
                if key not in PROPERTY_KEYS:
                    warnings.append(f"{_where} 未知键 {key!r} 已丢弃（其余键继续生效）")
        elif isinstance(_raw, list):
            for index, entry in enumerate(_raw):
                if isinstance(entry, dict):
                    for key in entry:
                        if key not in PROPERTY_KEYS:
                            warnings.append(f"{_where}[{index}] 未知键 {key!r} 已丢弃"
                                            f"（其余键继续生效）")
            continue
    accepted: List[ActivityProperty] = []
    rejected: List[Tuple[str, str]] = []
    raw_properties = raw.get("properties")
    if raw_properties is None:
        warnings.append("properties 键缺失：视为无自定义，全部使用内置条目")
    elif not isinstance(raw_properties, list):
        return PropertiesLoadResult(error={"error_key": "properties", "error_kind": "type",
                                           "detail": f"properties：期望 数组，实得 {_fmt(raw_properties)}"})
    else:
        seen_ids: Dict[str, int] = {}
        seen_labels: Dict[str, int] = {}
        for index, item in enumerate(raw_properties):
            try:
                prop = _check_property(item, name_hint=str(index))
            except _EntryRejected as exc:
                rejected.append((exc.name, exc.detail))
                continue
            if isinstance(item, dict):
                raw_color = item.get("color")
                if raw_color not in (None, "") and (not isinstance(raw_color, str)
                                                    or not _valid_color(str(raw_color).strip())):
                    warnings.append(f"条目 {prop.id} color 非法（期望 \"#RRGGBB\" 十六进制格式，"
                                    f"可空）：已丢弃颜色位，条目与倍率照常生效"
                                    f"（渲染回退类型色 → 兜底色）")
            if prop.id in seen_ids:
                rejected.append((prop.id, f"properties[{index}].id：期望 去空白后互不重复，"
                                          f"实得与 properties[{seen_ids[prop.id]}].id 重复"
                                          f"（内置的同名 id 仍以内置为准）"))
                continue
            if prop.label in seen_labels:
                rejected.append((prop.id, f"properties[{index}].label：期望 去空白后互不重复，"
                                          f"实得 {prop.label!r} 与 "
                                          f"properties[{seen_labels[prop.label]}].label 重复"))
                continue
            seen_ids[prop.id] = index
            seen_labels[prop.label] = index
            accepted.append(prop)
    default_prop = BUILTIN_DEFAULT_PROPERTY
    raw_default = raw.get("default_property")
    if raw_default is None:
        warnings.append("default_property 键缺失：兜底条目回退内置（free_time/1.0）")
        raw_default = None
    else:
        try:
            default_prop = _check_property(raw_default, name_hint=_ENTRY_REJECT_NAME_DEFAULT,
                                           prefix="default_property")
        except _EntryRejected as exc:
            rejected.append((exc.name, exc.detail))
            warnings.append("default_property 校验失败：兜底条目回退内置（free_time/1.0）")
    return PropertiesLoadResult(accepted=tuple(accepted), default_property=default_prop,
                                rejected=tuple(rejected), warnings=tuple(warnings))


# ── 合并 ─────────────────────────────────────────────────────────────────
def merge_tables(result: PropertiesLoadResult) -> PropertyTable:
    """内置 ⊕ json（纯函数）：json 同 id 原位覆盖内置、新 id 追加；顶层级错误 → 纯内置。

    兜底条目：json 合法 ``default_property`` 优先，否则内置 ``free_time/1.0``。
    """
    builtin = builtin_table()
    default_prop = BUILTIN_DEFAULT_PROPERTY
    if result.error is not None:
        return PropertyTable(properties=builtin.properties, default_property=BUILTIN_DEFAULT_PROPERTY)
    merged: List[ActivityProperty] = list(builtin.properties)
    positions: Dict[str, int] = {prop.id: i for i, prop in enumerate(merged)}
    for prop in result.accepted:
        if prop.id in positions:
            merged[positions[prop.id]] = prop
        else:
            positions[prop.id] = len(merged)
            merged.append(prop)
    if result.default_property is not None:
        default_prop = result.default_property
    return PropertyTable(properties=tuple(merged), default_property=default_prop)


# ── 装载入口 ──────────────────────────────────────────────────────────────
def _plugin_root() -> Path:
    """插件根目录（本模块位于 ``<plugin_root>/planning/`` 下）。"""
    return Path(__file__).resolve().parents[1]


def _read_document(path: Path) -> Any:
    """读取并解析属性文件；失败统一 ``_Rejected(error_kind="parse")``。"""
    from core.mapping_dir import MappingReadError, read_json_text  # 延迟导入：顶层零插件依赖

    try:
        return read_json_text(path)
    except MappingReadError as exc:
        raise _Rejected("", "parse", exc.detail) from exc


# ── 落账 action（§24.3 冻结字面量）───────────────────────────────────────
#: 采纳（info：accepted/rejected/warnings 清单）
ACTION_APPLIED = "planning.properties_applied"
#: 整文件回退 / 条目排除（warn：原因）
ACTION_REJECTED = "planning.properties_rejected"


def _record(result: PropertiesLoadResult, decision_logger: Any, *,
            merged: PropertyTable) -> None:
    """落账（decision_logger 缺失/异常静默——装载故障绝不影响装配链）。"""
    if decision_logger is None:
        return
    record = getattr(decision_logger, "record", None)
    if not callable(record):
        return
    try:
        if result.error is not None:
            record(action=ACTION_REJECTED, severity="warn",
                   error_key=result.error["error_key"], error_kind=result.error["error_kind"],
                   detail=result.error["detail"])
            return
        rejected_list = [{"name": name, "detail": detail} for name, detail in result.rejected]
        if rejected_list:
            record(action=ACTION_REJECTED, severity="warn",
                   rejected=rejected_list, warnings=list(result.warnings))
        record(action=ACTION_APPLIED, severity="info",
               accepted=[prop.id for prop in merged.properties],
               default=merged.default_property.id,
               rejected=rejected_list,
               warnings=list(result.warnings))
    except Exception:
        logger.debug("日程属性表落账异常（忽略）", exc_info=True)


def _generate_default(path: Path) -> None:
    """生成内置默认属性表（best-effort：失败仅告警，不阻断装载）。"""
    import json as _json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": SUPPORTED_VERSION,
            "properties": [
                {"id": p.id, "label": p.label, "goal_types": list(p.goal_types),
                 "keywords": list(p.keywords), "reply_factor": p.reply_factor, "color": p.color}
                for p in BUILTIN_PROPERTIES
            ],
            "default_property": {
                "id": BUILTIN_DEFAULT_PROPERTY.id, "label": BUILTIN_DEFAULT_PROPERTY.label,
                "goal_types": list(BUILTIN_DEFAULT_PROPERTY.goal_types),
                "keywords": list(BUILTIN_DEFAULT_PROPERTY.keywords),
                "reply_factor": BUILTIN_DEFAULT_PROPERTY.reply_factor,
                "color": BUILTIN_DEFAULT_PROPERTY.color,
            },
        }
        path.write_text(_json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("已生成默认日程属性表：%s（可自行编辑，倍率/关键词随属性生效）", path)
    except Exception as exc:
        logger.warning("生成默认日程属性表失败（忽略）：%s", exc)


def load_properties(*, root_dir: Any = None,
                    decision_logger: Any = None) -> Tuple[PropertiesLoadResult, PropertyTable]:
    """装载属性表（**只读一遍**；``root_dir=None`` 测试桩 → 跳过根层纯内置）。"""
    if root_dir is None:
        return PropertiesLoadResult(), builtin_table()
    from core.mapping_dir import ensure_user_table, mappings_path  # 延迟导入：顶层零插件依赖

    path = mappings_path(Path(root_dir), PROPERTIES_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2（2026-10-23 直派）：表层缺失 → ensure_user_table 按序补齐
        # （旧根迁移优先，仍缺才从出厂种子映射表复制；解析错误不算缺失、绝不覆盖用户文件）
        ensure_user_table(root_dir, PROPERTIES_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        _generate_default(path)
        result = PropertiesLoadResult(generated_default=True)
        return result, builtin_table()
    try:
        raw = _read_document(path)
    except _Rejected as exc:
        result = PropertiesLoadResult(error={"error_key": exc.error_key,
                                             "error_kind": exc.error_kind,
                                             "detail": exc.detail})
        table = builtin_table()
        _record(result, decision_logger, merged=table)
        if result.error is not None:
            logger.warning("日程属性表整文件回退内置（%s @ %s）：%s",
                           result.error["error_kind"], result.error["error_key"],
                           result.error["detail"])
        return result, table
    result = validate_properties_document(raw)
    merged = merge_tables(result)
    _record(result, decision_logger, merged=merged)
    if result.rejected:
        logger.warning("日程属性表有 %d 个条目被排除：%s", len(result.rejected),
                       "；".join(f"{n}（{d}）" for n, d in result.rejected))
    elif result.warnings:
        logger.info("日程属性表提示：%s", "；".join(result.warnings))
    return result, merged


# ── 生效位（仿 emotion/vad.apply_mapping_tables 的重绑模式 + §24.3 冻结命名）──
_ACTIVE: Optional[PropertyTable] = None


def apply_tables(table: PropertyTable) -> None:
    """重绑生效属性表（on_load/热重载调用一次；``_ACTIVE=None`` = 未装载用内置）。"""
    global _ACTIVE
    _ACTIVE = table


def active_table() -> PropertyTable:
    """当前生效属性表（未装载时 = 内置）。"""
    if _ACTIVE is None:
        return builtin_table()
    return _ACTIVE


def resolve_property(goal_type: Any, name: Any = "") -> ActivityProperty:
    """条目 → 属性（§24.3 匹配口径；未装载/神秘输入恒有 free_time/1.0 兜底）。"""
    return active_table().infer(goal_type, name)


def reply_factor(goal_type: Any, name: Any = "") -> float:
    """便利取值：属性回复率倍率（proactive 频率链唯一取数位）。"""
    return resolve_property(goal_type, name).reply_factor


def load_and_apply(*, root_dir: Any = None,
                   decision_logger: Any = None) -> PropertiesLoadResult:
    """装载 + 合并 + 重绑生效位（插件装配链的单次调用入口）。"""
    result, merged = load_properties(root_dir=root_dir, decision_logger=decision_logger)
    apply_tables(merged)
    return result
