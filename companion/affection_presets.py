"""好感度预设表（2026-10-17 用户指示，计划书 §19.2）：外置 JSON 定义「关系预设 → 档位区间」。

仿 ``emotion/mapping_override.py`` 的加载契约，错误粒度为用户口径的**第三种**（预设级排除）：

- 文件：``<插件根>/mappings/affection_presets.json``（映射表集中子文件夹，``core/mapping_dir``；
  表层缺失时 ``ensure_user_table`` 按序补齐——旧根文件优先迁移，仍缺才从出厂种子
  ``mappings/default_mappings/`` 复制，§29.2）；
- 结构::

    {
      "version": 1,
      "presets": {
        "恋人": {"tiers": [{"label": "陌生", "min": 0, "max": 50},
                           {"label": "认识", "min": 50, "max": 70}, ...]},
        ...
      }
    }

- **预设级校验**（每预设独立判定）：``tiers`` 为数组、长度 1~5；每档键集恰为
  ``label/min/max``；``label`` strip 后非空且预设内不重复；``min/max`` 为数值
  （bool/NaN/Infinity 拒绝——「仅当满足上界判据才收」写法）；``0 ≤ min < max ≤ 200``；
  区间**连续覆盖 [0, 200]**（首档 ``min==0``、相邻相接 ``min[i+1]==max[i]``、末档 ``max==200``）；
- **错误粒度**：某预设校验失败 → **仅排除该预设**（``rejected`` 清单落账），合法预设保留；
  顶层级错误（version 缺失/≠1、presets 非对象、未知顶层键、解析失败）→ 整文件回退内置默认；
  全部预设被排除 / ``presets`` 键缺失 → 内置默认（缺键视为「无自定义」，与文件缺失同语义）；
- 优先级：**json 预设 > 同名内置预设（覆盖）> 其余内置预设**；内置五预设恒可用
  （``[patrol].relation_type`` 下拉与回退的兜底，默认值「恋人」永不非法）；
- 加载时机：**只在 on_load/热重载读一遍**（``load_and_apply``）；``root_dir=None``（测试桩）
  → 跳过根层纯内置；默认文件生成 best-effort（失败仅告警）；
- 导入期安全：本模块顶层**只依赖标准库**（``core.mapping_dir`` 延迟到函数体内），
  可被 ``companion/config.py`` 在配置模型构造期引用（``available_preset_names`` 动态下拉，
  只读不写、失败静默兜底内置名）；生效位重绑仿 ``emotion/vad.apply_mapping_tables``。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 文件与版本 ────────────────────────────────────────────────────────────
#: 预设表文件名（实际读取位 = ``<root>/mappings/``）
PRESETS_FILE_NAME = "affection_presets.json"
#: 顶层 ``version`` 必填 int 且需等于本值（bool 不算 int）
SUPPORTED_VERSION = 1

# ── 分数域（0~200，2026-10-17 起的唯一真源）───────────────────────────────
SCORE_MIN = 0.0
SCORE_MAX = 200.0
#: 预设档位数上下限（用户口径「1~5 档自由构造」）
TIER_COUNT_MIN = 1
TIER_COUNT_MAX = 5

# ── 内置默认 ──────────────────────────────────────────────────────────────
#: 内置预设名（与 ``[patrol].relation_type`` 枚举对齐；「自定义」的特殊标签构造在 relation 侧）
BUILTIN_PRESET_NAMES: Tuple[str, ...] = ("恋人", "挚友", "维护者", "家人", "自定义")
#: 内置档位区间 ``[lo, hi)``（末档 hi 闭合）——旧 0~100 恋人区间等比 ×2（体验等价迁移）；
#: 全部内置预设共用这一份区间（旧行为即全局一份 TIER_BOUNDS + 按关系换标签）
BUILTIN_TIER_BOUNDS: Tuple[Tuple[float, float], ...] = (
    (0.0, 40.0), (40.0, 90.0), (90.0, 140.0), (140.0, 176.0), (176.0, 200.0),
)
#: 内置档位标签（单一真源自 relation.py 迁入；「自定义」由 relation.resolve_relation 按称谓动态构造）
#: 2026-10-17 §19.4 / 复核 findings#2：本表是**静态视图**，relation_type=自定义 时 relation 侧
#: 以实际称谓（relation_custom）覆盖显示（companion/components.py:647 优先
#: ``relation.tier_label(tier)``，其档位名按三桶动态构造）——故「自定义」末档字面量取
#: 与 relation_custom 留空时一致的兜底词「重要的人」，不再写占位性质的「称谓」。
BUILTIN_TIER_LABELS: Dict[str, Tuple[str, ...]] = {
    "恋人": ("生疏", "熟悉", "亲密", "热恋", "挚爱"),
    "挚友": ("点头之交", "熟识", "投缘", "铁哥们", "过命的交情"),
    "维护者": ("路人", "认识", "信任", "依靠", "生命线"),
    "家人": ("相识", "亲近", "家人", "至亲", "血浓于水"),
    "自定义": ("熟识", "熟识", "亲近", "亲近", "重要的人"),
}

# ── 落账 action ──────────────────────────────────────────────────────────
#: 采纳（info：accepted/rejected/warnings 清单）
ACTION_APPLIED = "affection.presets_applied"
#: 整文件回退（warn：顶层级原因）
ACTION_REJECTED = "affection.presets_rejected"

#: error_kind 全集（词表与 emotion/mapping_override 对齐）
ERROR_KINDS: Tuple[str, ...] = ("parse", "version_missing", "version_unsupported",
                                "unknown_key", "incomplete_table", "type", "range")


# ── 数据模型 ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TierSpec:
    """单档：标签 + ``[low, high)`` 区间（末档 ``high`` 闭合）。"""

    label: str
    low: float
    high: float


@dataclass(frozen=True)
class Preset:
    """一个关系预设：有序档位表 + 来源标记。"""

    name: str
    tiers: Tuple[TierSpec, ...]
    source: str = "builtin"  # "builtin" | "json"

    @property
    def size(self) -> int:
        return len(self.tiers)

    def bounds(self) -> Tuple[Tuple[float, float], ...]:
        return tuple((t.low, t.high) for t in self.tiers)

    def labels(self) -> Tuple[str, ...]:
        return tuple(t.label for t in self.tiers)

    def tier_for_score(self, score: float) -> int:
        """分数 → 档位索引（不含迟滞；自末档向下首个 ``score >= low`` 命中）。"""
        value = max(SCORE_MIN, min(SCORE_MAX, float(score)))
        for index in range(self.size - 1, -1, -1):
            if value >= self.tiers[index].low:
                return index
        return 0

    def representative_score(self, tier: int) -> float:
        """档位代表分 = 该档区间中值（区间来自预设，不再使用全局静态均值表）。"""
        index = max(0, min(self.size - 1, int(tier)))
        spec = self.tiers[index]
        return (spec.low + spec.high) / 2.0


@dataclass(frozen=True)
class PresetsLoadResult:
    """装载/校验结果（``error is None`` = 顶层级合法；``accepted`` = json 合法预设）。"""

    accepted: Dict[str, Preset]
    #: （预设名, 原因）——被排除的预设
    rejected: Tuple[Tuple[str, str], ...] = ()
    #: 顶层级错误（整文件回退内置）；None = 顶层级合法
    error: Optional[Dict[str, str]] = None
    #: 非致命告警（如 presets 键缺失）
    warnings: Tuple[str, ...] = ()
    #: 本次是否生成了默认文件
    generated_default: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


# ── 工具 ─────────────────────────────────────────────────────────────────
def _is_number(value: Any) -> bool:
    """数值判定：int/float 且剔除 bool（``True`` 是 int 的实例，不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _in_score_domain(value: float) -> bool:
    """∈[0, 200] 且非 NaN/Infinity：仅当满足上界判据才收（NaN 的比较恒 False → 拒）。"""
    return SCORE_MIN <= value <= SCORE_MAX


def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


# ── 内置合成 ──────────────────────────────────────────────────────────────
def builtin_presets() -> Dict[str, Preset]:
    """内置默认预设（每次调用重建，廉价；热重载后不会读到陈旧基线）。"""
    out: Dict[str, Preset] = {}
    for name in BUILTIN_PRESET_NAMES:
        labels = BUILTIN_TIER_LABELS[name]
        tiers = tuple(TierSpec(label=labels[i], low=lo, high=hi)
                      for i, (lo, hi) in enumerate(BUILTIN_TIER_BOUNDS))
        out[name] = Preset(name=name, tiers=tiers, source="builtin")
    return out


# ── 校验（纯函数）────────────────────────────────────────────────────────
class _Rejected(Exception):
    """顶层级早退信号：整文件拒绝、全量回退内置。"""

    def __init__(self, error_key: str, error_kind: str, detail: str) -> None:
        super().__init__(detail)
        self.error_key = error_key
        self.error_kind = error_kind
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
        if key not in ("version", "presets"):
            raise _Rejected(str(key), "unknown_key",
                            f"顶层键 {key!r}：未知键（允许的键集 version、presets）")


class _PresetRejected(Exception):
    """预设级早退信号：**仅排除该预设**，其余预设继续校验。"""

    def __init__(self, name: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name
        self.detail = detail


def _check_tiers(name: str, raw_preset: Any) -> Preset:
    """单预设校验：失败抛 :class:`_PresetRejected`（由调用方捕获为排除该预设）。"""
    prefix = f"presets.{name}"
    if not isinstance(raw_preset, dict):
        raise _PresetRejected(name, f"{prefix}：期望 对象，实得 {_fmt(raw_preset)}")
    for key in raw_preset:
        if key != "tiers":
            raise _PresetRejected(name, f"{prefix}.{key}：未知键（该处键集恰为 tiers）")
    if "tiers" not in raw_preset:
        raise _PresetRejected(name, f"{prefix}.tiers：键缺失（该处键集恰为 tiers）")
    tiers_raw = raw_preset["tiers"]
    if not isinstance(tiers_raw, list):
        raise _PresetRejected(name, f"{prefix}.tiers：期望 数组，实得 {_fmt(tiers_raw)}")
    count = len(tiers_raw)
    if not TIER_COUNT_MIN <= count <= TIER_COUNT_MAX:
        raise _PresetRejected(name, f"{prefix}.tiers：期望 档位数 {TIER_COUNT_MIN}~{TIER_COUNT_MAX}，"
                                    f"实得 {count}")
    tiers: List[TierSpec] = []
    seen: Dict[str, int] = {}
    for index, item in enumerate(tiers_raw):
        path = f"{prefix}.tiers[{index}]"
        if not isinstance(item, dict):
            raise _PresetRejected(name, f"{path}：期望 对象，实得 {_fmt(item)}")
        for key in item:
            if key not in ("label", "min", "max"):
                raise _PresetRejected(name, f"{path}.{key}：未知键（该处键集恰为 label、min、max）")
        for key in ("label", "min", "max"):
            if key not in item:
                raise _PresetRejected(name, f"{path}.{key}：键缺失（该处键集恰为 label、min、max）")
        label = item["label"]
        if not isinstance(label, str):
            raise _PresetRejected(name, f"{path}.label：期望 非空字符串，实得 {_fmt(label)}")
        normalized = label.strip()
        if not normalized:
            raise _PresetRejected(name, f"{path}.label：期望 strip 去空白后非空，实得 {_fmt(label)}")
        if normalized in seen:
            raise _PresetRejected(name, f"{path}.label：期望 去空白后标签互不重复，"
                                        f"实得 {_fmt(label)} 与 tiers[{seen[normalized]}].label 重复")
        seen[normalized] = index
        low = _check_bound(item["min"], f"{path}.min", name)
        high = _check_bound(item["max"], f"{path}.max", name)
        if not low < high:
            raise _PresetRejected(name, f"{path}：期望 min < max，实得 [{_fmt(item['min'])}, "
                                        f"{_fmt(item['max'])})")
        tiers.append(TierSpec(label=normalized, low=low, high=high))
    # 区间连续覆盖 [0, 200]
    if tiers[0].low != SCORE_MIN:
        raise _PresetRejected(name, f"{prefix}.tiers[0].min：期望 {SCORE_MIN:g}（覆盖区间起点），"
                                    f"实得 {_fmt(tiers[0].low)}")
    for index in range(len(tiers) - 1):
        if tiers[index].high != tiers[index + 1].low:
            raise _PresetRejected(name, f"{prefix}.tiers[{index + 1}].min：期望与上一档 max 相接"
                                        f"（{_fmt(tiers[index].high)}），"
                                        f"实得 {_fmt(tiers[index + 1].low)}")
    if tiers[-1].high != SCORE_MAX:
        raise _PresetRejected(name, f"{prefix}.tiers[{len(tiers) - 1}].max：期望 {SCORE_MAX:g}"
                                    f"（覆盖区间终点），实得 {_fmt(tiers[-1].high)}")
    return Preset(name=name, tiers=tuple(tiers), source="json")


def _check_bound(value: Any, path: str, name: str) -> float:
    if not _is_number(value):
        raise _PresetRejected(name, f"{path}：期望 数值，实得 {_fmt(value)}")
    number = float(value)
    if not _in_score_domain(number):
        raise _PresetRejected(name, f"{path}：期望 ∈[{SCORE_MIN:g}, {SCORE_MAX:g}]"
                                    f"（总区间严格校验，NaN/Infinity 一律拒绝），实得 {_fmt(value)}")
    return number


def validate_presets_document(raw: Any) -> PresetsLoadResult:
    """校验已解析的预设文档（**纯函数**，不碰文件系统）。

    顶层级失败 → ``error``（整文件回退）；预设级失败 → 该预设进 ``rejected``、其余保留；
    ``presets`` 键缺失 → 合法但 ``accepted`` 为空 + warning（与「无自定义」同语义）。
    """
    try:
        if not isinstance(raw, dict):
            raise _Rejected("", "type", f"顶层：期望 JSON 对象，实得 {_fmt(raw)}")
        _check_version(raw)
        _check_top_level_keys(raw)
    except _Rejected as exc:
        return PresetsLoadResult(accepted={}, error={"error_key": exc.error_key,
                                                     "error_kind": exc.error_kind,
                                                     "detail": exc.detail})
    raw_presets = raw.get("presets")
    if raw_presets is None:
        return PresetsLoadResult(accepted={},
                                 warnings=("presets 键缺失：视为无自定义，全部使用内置预设",))
    if not isinstance(raw_presets, dict):
        return PresetsLoadResult(accepted={}, error={"error_key": "presets", "error_kind": "type",
                                                     "detail": f"presets：期望 对象，实得 {_fmt(raw_presets)}"})
    accepted: Dict[str, Preset] = {}
    rejected: List[Tuple[str, str]] = []
    for name in raw_presets:
        key = str(name).strip()
        if not key:
            rejected.append((str(name), "presets.<空名>：预设名 strip 去空白后为空"))
            continue
        try:
            accepted[key] = _check_tiers(key, raw_presets[name])
        except _PresetRejected as exc:
            rejected.append((key, exc.detail))
    return PresetsLoadResult(accepted=accepted, rejected=tuple(rejected))


# ── 装载入口 ──────────────────────────────────────────────────────────────
def _plugin_root() -> Path:
    """插件根目录（本模块位于 ``<plugin_root>/companion/`` 下）。"""
    return Path(__file__).resolve().parents[1]


def _read_document(path: Path) -> Any:
    """读取并解析预设文件；失败统一 ``_Rejected(error_kind="parse")``。"""
    from core.mapping_dir import MappingReadError, read_json_text  # 延迟导入：顶层零插件依赖

    try:
        return read_json_text(path)
    except MappingReadError as exc:
        raise _Rejected("", "parse", exc.detail) from exc


def _record(result: PresetsLoadResult, decision_logger: Any, *, merged_names: List[str]) -> None:
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
        else:
            record(action=ACTION_APPLIED, severity="info",
                   accepted=sorted(merged_names),
                   rejected=[{"name": name, "detail": detail} for name, detail in result.rejected],
                   warnings=list(result.warnings))
    except Exception:
        logger.debug("好感度预设表落账异常（忽略）", exc_info=True)


def merge_presets(result: PresetsLoadResult) -> Dict[str, Preset]:
    """内置 ⊕ json（纯函数）：json 同名覆盖内置；顶层级错误/全排除 → 纯内置。"""
    merged = builtin_presets()
    if result.error is not None:
        return merged
    for name, preset in result.accepted.items():
        merged[name] = preset
    return merged


def load_presets(*, root_dir: Any = None,
                 decision_logger: Any = None) -> Tuple[PresetsLoadResult, Dict[str, Preset]]:
    """装载预设表（**只读一遍**；``root_dir=None`` 测试桩 → 跳过根层纯内置）。"""
    if root_dir is None:
        return PresetsLoadResult(accepted={}), builtin_presets()
    from core.mapping_dir import ensure_user_table, mappings_path  # 延迟导入：顶层零插件依赖

    path = mappings_path(Path(root_dir), PRESETS_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2（2026-10-23 直派）：表层缺失 → ensure_user_table 按序补齐
        # （旧根迁移优先，仍缺才从出厂种子映射表复制；解析错误不算缺失、绝不覆盖用户文件）
        ensure_user_table(root_dir, PRESETS_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        _generate_default(path)
        result = PresetsLoadResult(accepted={}, generated_default=True)
        return result, builtin_presets()
    try:
        raw = _read_document(path)
    except _Rejected as exc:
        result = PresetsLoadResult(accepted={}, error={"error_key": exc.error_key,
                                                       "error_kind": exc.error_kind,
                                                       "detail": exc.detail})
        _record(result, decision_logger, merged_names=sorted(builtin_presets()))
        return result, builtin_presets()
    result = validate_presets_document(raw)
    merged = merge_presets(result)
    _record(result, decision_logger, merged_names=sorted(merged))
    return result, merged


def _generate_default(path: Path) -> None:
    """生成内置默认预设表（best-effort：失败仅告警，不阻断装载）。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "version": SUPPORTED_VERSION,
            "presets": {
                name: {"tiers": [{"label": spec.label, "min": spec.low, "max": spec.high}
                                 for spec in preset.tiers]}
                for name, preset in builtin_presets().items() if name != "自定义"
            },
        }
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("已生成默认好感度预设表：%s（可自行编辑，坏预设将被排除并回退内置默认）", path)
    except Exception as exc:
        logger.warning("生成默认好感度预设表失败（忽略）：%s", exc)


# ── 生效位（仿 emotion/vad.apply_mapping_tables 的重绑模式）──────────────
_ACTIVE_PRESETS: Optional[Dict[str, Preset]] = None


def apply_presets(presets: Dict[str, Preset]) -> None:
    """重绑生效预设表（on_load/热重载调用一次；``_ACTIVE_PRESETS=None`` = 未装载用内置）。"""
    global _ACTIVE_PRESETS
    _ACTIVE_PRESETS = dict(presets)


def active_presets() -> Dict[str, Preset]:
    """当前生效预设表（未装载时 = 内置）。"""
    if _ACTIVE_PRESETS is None:
        return builtin_presets()
    return dict(_ACTIVE_PRESETS)


def preset_for(name: Any) -> Preset:
    """按预设名取生效预设；未知名回退内置「恋人」（与 relation.resolve_relation 的回退一致）。"""
    presets = active_presets()
    key = str(name or "").strip()
    if key in presets:
        return presets[key]
    fallback = presets.get(BUILTIN_PRESET_NAMES[0])
    return fallback if fallback is not None else builtin_presets()[BUILTIN_PRESET_NAMES[0]]


def load_and_apply(*, root_dir: Any = None,
                   decision_logger: Any = None) -> PresetsLoadResult:
    """装载 + 合并 + 重绑生效位（插件装配链的单次调用入口）。"""
    result, merged = load_presets(root_dir=root_dir, decision_logger=decision_logger)
    apply_presets(merged)
    return result


def available_preset_names() -> Tuple[str, ...]:
    """配置模型导入期的下拉名单：内置名 ∪ json 合法预设名（**只读不写、失败静默兜底内置**）。

    供 ``companion/config.py`` 动态构造 ``relation_type`` 的 ``Literal``——宿主每次请求
    现场重算 schema、热重载重导入模块，故 json 变更后每次启动（含热重载）下拉即变更
    （2026-10-17 宿主侦察结论）。本函数不生成默认文件、不落账、不触碰生效位。
    """
    names: List[str] = list(BUILTIN_PRESET_NAMES)
    try:
        from core.mapping_dir import mappings_path  # 延迟导入：顶层零插件依赖

        path = mappings_path(_plugin_root(), PRESETS_FILE_NAME)
        raw = json.loads(path.read_text(encoding="utf-8"))
        result = validate_presets_document(raw)
        for name in result.accepted:
            if name not in names:
                names.append(name)
    except Exception as exc:  # 文件缺失/非法/IO——导入期绝不抛
        logger.debug("导入期读取预设表失败（下拉回退内置名单）：%s", exc)
    return tuple(names)
