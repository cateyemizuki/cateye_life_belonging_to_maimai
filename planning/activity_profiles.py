"""活动画像表（第六张映射表；R7 依据 = 用户 2026-10-25 直派【日程系统滚动化改造，
设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，
附录 B.3 / B.6.2】）：mappings/ 第六张表 ``activity_profiles.json`` 的装载与结构表真源。

背景（设计附录 B.3）：滚动执行层需要按「活动类型」取一组确定性参数——时长采样分布
（normal / lognormal + 截断 + 最小块）、可中断性/切走成本、五项状态速率（每分钟）、
恢复倾向与自发权重。这些参数全部外置到本表，用户改 json 即可调，不动代码；
缺失类型回退 ``daily_routine`` 并记日志（B.3 说明原文）。

仿 ``planning/activity_properties.py``（第五张表 §24.3）的加载契约：

- 文件：``<插件根>/mappings/activity_profiles.json``（读取位统一走
  ``core.mapping_dir.mappings_path``，禁止其他位置）；结构::

      {"version": 1, "profiles": {"<类型>": {...}, ...}}

- **校验三级粒度（仿 §19/§24）**：顶层级坏（json 不可读 / version 缺失或≠1 /
  顶层非对象 / 未知顶层键）→ 整文件回退内置；单条目坏（``duration`` 结构或参数非法 /
  ``min_block`` 非正数 / 速率与比例位非数值等）→ **仅排除该条**并落账，其余条目照常
  生效；条目内未知键 → 丢弃该键其余生效（落 warning）；
- 合并语义：json 同类型覆盖内置同类型（内置顺序不变、原位替换），新类型追加在尾部
  ——json 可只写想改的条目，其余内置画像照旧；
- 表层映射表纪律（§29.2 同款）：表层读取位 = ``core/mapping_dir.mappings_path``；
  经 ``ensure_user_table(root_dir, "activity_profiles.json")`` 补齐（旧根迁移优先 →
  出厂种子 ``mappings/default_mappings/`` 复制）；**解析错误不算缺失**（绝不复制/
  覆盖用户文件）；``root_dir=None``（测试桩）→ 跳过根层纯内置；
- 加载时机：只在 on_load/热重载读一遍（``root_dir`` 缺失走纯内置；缺失文件
  best-effort 生成内置默认，失败仅告警）；常驻内存 = 模块级 :data:`_ACTIVE` +
  :func:`apply_profiles` 重绑（仿 activity_properties 模式）；落账
  ``planning.profiles_applied`` / ``planning.profiles_rejected``；
- 导入期安全：本模块顶层**只依赖标准库**（``core.mapping_dir`` 延迟到函数体内），
  planning 与滚动规划侧可安全引用，无导入环风险；采样纯离线零 LLM 调用点。
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 文件与版本 ──────────────────────────────────────────────────────────
#: 画像表文件名（实际读取位 = ``<root>/mappings/``，经 core.mapping_dir）
PROFILES_FILE_NAME = "activity_profiles.json"
#: 顶层 ``version`` 必填 int 且需等于本值（bool 不算 int）
SUPPORTED_VERSION = 1
#: 条目固定字段集（未知键丢弃该键、其余生效——粒度对齐 §24.3）
PROFILE_KEYS: Tuple[str, ...] = (
    "label", "duration", "min_block",
    "interruptibility", "switch_cost",
    "energy_rate", "hunger_rate", "focus_rate", "stress_rate", "social_rate",
    "resume_bias", "spontaneous_weight",
)
#: ``duration.dist`` 只支持这两种（B.3：后续可加 choice / uniform，本版不收）
SUPPORTED_DISTS: Tuple[str, ...] = ("normal", "lognormal")
#: 缺失类型的回退画像（B.3 说明原文：缺失 profile 时用 daily_routine 默认并记日志）
DEFAULT_PROFILE_ID = "daily_routine"
#: 时长采样的低精力偏移口径（B.6.2：energy < 0.3 且 energy_rate < 0 → 时长 ×0.7）
LOW_ENERGY_THRESHOLD = 0.3
LOW_ENERGY_SCALE = 0.7
#: 比例位（interruptibility/switch_cost/resume_bias/spontaneous_weight）取值区间，
#: 越界钳入区间并落 warning（数值位本身非法才排除条目）
RATIO_MIN = 0.0
RATIO_MAX = 1.0


# ── 数据模型 ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DurationSpec:
    """时长采样分布参数（B.3 ``duration`` 对象：dist + 中心参数 + sigma + 截断）。

    ``normal`` 用 ``mean``；``lognormal`` 用 ``median``（取自然对数后做 mu）；
    ``min/max`` 为采样后截断区间（分钟）。
    """

    dist: str
    mean: Optional[float] = None
    median: Optional[float] = None
    sigma: float = 1.0
    min_minutes: float = 0.0
    max_minutes: float = 0.0

    def sample(self, rng: Any) -> float:
        """按分布采样一个未截断分钟数（截断由 :func:`sample_duration` 统一做）。"""
        if self.dist == "normal":
            return float(rng.gauss(self.mean, self.sigma))
        return float(rng.lognormvariate(math.log(self.median), self.sigma))


@dataclass(frozen=True)
class ActivityProfile:
    """单条活动画像：时长分布 + 最小块 + 可中断性/切走成本 + 五项状态速率（每分钟）。

    速率语义（B.3）：正数增加、负数减少；``resume_bias`` 影响恢复池优先级；
    ``spontaneous_weight`` 影响空闲时被选中的概率（消费位在滚动规划层）。
    """

    key: str
    label: str
    duration: DurationSpec
    min_block: float
    interruptibility: float = 0.5
    switch_cost: float = 0.5
    energy_rate: float = 0.0
    hunger_rate: float = 0.0
    focus_rate: float = 0.0
    stress_rate: float = 0.0
    social_rate: float = 0.0
    resume_bias: float = 0.0
    spontaneous_weight: float = 0.0


@dataclass(frozen=True)
class ProfileTable:
    """生效画像表（合并后的只读视图）：有序 ``(类型, 画像)`` 数组。"""

    entries: Tuple[Tuple[str, ActivityProfile], ...]

    def get(self, key: Any) -> Optional[ActivityProfile]:
        """按类型精确取画像（未收录 → ``None``，由 :func:`get_profile` 兜底）。"""
        k = str(key or "").strip()
        for name, profile in self.entries:
            if name == k:
                return profile
        return None


@dataclass(frozen=True)
class ProfilesLoadResult:
    """装载/校验结果（``error is None`` = 顶层级合法）。

    ``accepted`` = json 合法条目 ``(类型, 画像)``（合并前）；
    ``rejected`` = 被排除条目的 ``(类型, 原因)`` 留账；
    ``warnings`` = 非致命提示（profiles 键缺失、未知键丢弃、越界钳制等）。
    """

    accepted: Tuple[Tuple[str, ActivityProfile], ...] = ()
    rejected: Tuple[Tuple[str, str], ...] = ()
    warnings: Tuple[str, ...] = ()
    error: Optional[Dict[str, str]] = None
    generated_default: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


# ── 内置默认（设计附录 B.3 的 json 逐条迁移；数值语义 = 附录原文）────────────
#: 内置默认画像（B.3 全部条目；缺类型回退 daily_routine）
BUILTIN_PROFILES: Tuple[Tuple[str, ActivityProfile], ...] = (
    ("work", ActivityProfile(
        key="work", label="工作",
        duration=DurationSpec("lognormal", median=50.0, sigma=0.35,
                              min_minutes=25.0, max_minutes=100.0),
        min_block=25.0, interruptibility=0.25, switch_cost=0.8,
        energy_rate=-0.0015, hunger_rate=0.0004, focus_rate=-0.0010,
        stress_rate=0.0010, social_rate=0.0000,
        resume_bias=0.6, spontaneous_weight=0.2)),
    ("study", ActivityProfile(
        key="study", label="学习",
        duration=DurationSpec("lognormal", median=50.0, sigma=0.30,
                              min_minutes=25.0, max_minutes=90.0),
        min_block=25.0, interruptibility=0.55, switch_cost=0.5,
        energy_rate=-0.0013, hunger_rate=0.0004, focus_rate=-0.0009,
        stress_rate=0.0008, social_rate=0.0000,
        resume_bias=0.5, spontaneous_weight=0.2)),
    ("meal", ActivityProfile(
        key="meal", label="用餐",
        duration=DurationSpec("normal", mean=35.0, sigma=10.0,
                              min_minutes=20.0, max_minutes=50.0),
        min_block=20.0, interruptibility=0.90, switch_cost=0.2,
        energy_rate=0.0003, hunger_rate=-0.0180, focus_rate=0.0002,
        stress_rate=-0.0003, social_rate=0.0000,
        resume_bias=0.1, spontaneous_weight=0.1)),
    ("rest", ActivityProfile(
        key="rest", label="休息",
        duration=DurationSpec("normal", mean=25.0, sigma=10.0,
                              min_minutes=15.0, max_minutes=45.0),
        min_block=10.0, interruptibility=0.95, switch_cost=0.1,
        energy_rate=0.0060, hunger_rate=0.0002, focus_rate=0.0030,
        stress_rate=-0.0040, social_rate=0.0000,
        resume_bias=0.0, spontaneous_weight=0.8)),
    ("exercise", ActivityProfile(
        key="exercise", label="运动",
        duration=DurationSpec("normal", mean=45.0, sigma=15.0,
                              min_minutes=25.0, max_minutes=75.0),
        min_block=20.0, interruptibility=0.80, switch_cost=0.3,
        energy_rate=-0.0020, hunger_rate=0.0010, focus_rate=0.0010,
        stress_rate=-0.0050, social_rate=0.0000,
        resume_bias=0.2, spontaneous_weight=0.3)),
    ("entertainment", ActivityProfile(
        key="entertainment", label="娱乐",
        duration=DurationSpec("lognormal", median=60.0, sigma=0.40,
                              min_minutes=20.0, max_minutes=120.0),
        min_block=15.0, interruptibility=0.95, switch_cost=0.1,
        energy_rate=0.0005, hunger_rate=0.0002, focus_rate=-0.0020,
        stress_rate=-0.0030, social_rate=0.0000,
        resume_bias=0.1, spontaneous_weight=0.6)),
    ("daily_routine", ActivityProfile(
        key="daily_routine", label="日常",
        duration=DurationSpec("normal", mean=20.0, sigma=8.0,
                              min_minutes=10.0, max_minutes=40.0),
        min_block=10.0, interruptibility=0.90, switch_cost=0.2,
        energy_rate=-0.0005, hunger_rate=0.0003, focus_rate=-0.0005,
        stress_rate=0.0000, social_rate=0.0000,
        resume_bias=0.2, spontaneous_weight=0.4)),
    ("social", ActivityProfile(
        key="social", label="社交",
        duration=DurationSpec("normal", mean=40.0, sigma=15.0,
                              min_minutes=15.0, max_minutes=90.0),
        min_block=15.0, interruptibility=0.90, switch_cost=0.2,
        energy_rate=-0.0008, hunger_rate=0.0003, focus_rate=-0.0005,
        stress_rate=-0.0020, social_rate=-0.0200,
        resume_bias=0.1, spontaneous_weight=0.3)),
    ("idle", ActivityProfile(
        key="idle", label="待机",
        duration=DurationSpec("normal", mean=20.0, sigma=10.0,
                              min_minutes=10.0, max_minutes=40.0),
        min_block=10.0, interruptibility=1.0, switch_cost=0.05,
        energy_rate=0.0004, hunger_rate=0.0003, focus_rate=-0.0010,
        stress_rate=-0.0002, social_rate=0.0000,
        resume_bias=0.0, spontaneous_weight=1.0)),
    ("transition", ActivityProfile(
        key="transition", label="转场",
        duration=DurationSpec("normal", mean=10.0, sigma=3.0,
                              min_minutes=5.0, max_minutes=15.0),
        min_block=5.0, interruptibility=1.0, switch_cost=0.0,
        energy_rate=0.0000, hunger_rate=0.0002, focus_rate=0.0000,
        stress_rate=0.0000, social_rate=0.0000,
        resume_bias=0.0, spontaneous_weight=0.0)),
)


def builtin_table() -> ProfileTable:
    """内置默认画像表（B.3 全部条目）。"""
    return ProfileTable(entries=BUILTIN_PROFILES)


# ── 工具 ─────────────────────────────────────────────────────────────────
def _is_number(value: Any) -> bool:
    """数值判定：int/float 且剔除 bool（``True`` 是 int 的实例，不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    """有限数值判定（NaN/Infinity 一律不算）。"""
    return _is_number(value) and math.isfinite(float(value))


def _fmt(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 160 else text[:157] + "..."


def _energy_of(state: Any) -> float:
    """从 state（dict 或带 ``energy`` 属性的对象）取精力；取不到按 1.0（不做低精力偏移）。"""
    try:
        if isinstance(state, dict):
            value = state.get("energy")
        else:
            value = getattr(state, "energy", None)
        if _is_number(value):
            return float(value)
    except Exception:
        pass
    return 1.0


# ── 校验（纯函数，仿 activity_properties 的两级早退信号）──────────────────
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
        if key not in ("version", "profiles"):
            raise _Rejected(str(key), "unknown_key",
                            f"顶层键 {key!r}：未知键（允许的键集 version、profiles）")


def _check_duration(raw: Any, name: str) -> DurationSpec:
    """``duration`` 校验：dist 合法 + 中心参数/sigma/截断全为有限数值且关系成立。

    任一违反 → 条目排除（``_EntryRejected``）。
    """
    if not isinstance(raw, dict):
        raise _EntryRejected(name, f"duration：期望 对象，实得 {_fmt(raw)}")
    dist = raw.get("dist")
    if not isinstance(dist, str) or dist.strip() not in SUPPORTED_DISTS:
        raise _EntryRejected(name, f"duration.dist：期望 {' / '.join(SUPPORTED_DISTS)}，"
                                   f"实得 {_fmt(dist)}")
    dist = dist.strip()
    center_key = "mean" if dist == "normal" else "median"
    center = raw.get(center_key)
    if not _is_finite_number(center):
        raise _EntryRejected(name, f"duration.{center_key}：期望 有限数值（bool/字符串/"
                                   f"NaN/Infinity/缺失一律拒绝），实得 {_fmt(center)}")
    sigma = raw.get("sigma")
    if not _is_finite_number(sigma) or float(sigma) <= 0:
        raise _EntryRejected(name, f"duration.sigma：期望 正的有限数值，实得 {_fmt(sigma)}")
    lo = raw.get("min")
    hi = raw.get("max")
    if not _is_finite_number(lo) or not _is_finite_number(hi):
        raise _EntryRejected(name, f"duration.min/max：期望 有限数值（缺失一律拒绝），"
                                   f"实得 min={_fmt(lo)} max={_fmt(hi)}")
    if float(lo) < 0:
        raise _EntryRejected(name, f"duration.min：期望 >= 0，实得 {_fmt(lo)}")
    if float(hi) < float(lo):
        raise _EntryRejected(name, f"duration.min/max：期望 min <= max，"
                                   f"实得 min={_fmt(lo)} max={_fmt(hi)}")
    if dist == "normal":
        return DurationSpec(dist=dist, mean=float(center), sigma=float(sigma),
                            min_minutes=float(lo), max_minutes=float(hi))
    return DurationSpec(dist=dist, median=float(center), sigma=float(sigma),
                        min_minutes=float(lo), max_minutes=float(hi))


def _check_ratio(raw: Any, path: str, name: str, default: float,
                 warnings: List[str]) -> float:
    """比例位（[0,1]）：非数值 → 条目排除；越界 → 钳入区间并落 warning。"""
    if raw is None:
        return default
    if not _is_finite_number(raw):
        raise _EntryRejected(name, f"{path}：期望 有限数值（bool/字符串/NaN/Infinity 一律拒绝），"
                                   f"实得 {_fmt(raw)}")
    value = float(raw)
    if value < RATIO_MIN:
        warnings.append(f"{path} 低于 {RATIO_MIN:g}，已钳入区间")
        return RATIO_MIN
    if value > RATIO_MAX:
        warnings.append(f"{path} 高于 {RATIO_MAX:g}，已钳入区间")
        return RATIO_MAX
    return value


def _check_profile(key: str, raw: Any, warnings: List[str]) -> ActivityProfile:
    """单条目校验：失败抛 :class:`_EntryRejected`（由调用方捕获为排除该条）。

    - ``label`` 可选（缺失/空白回退类型名，只提示不排除）；
    - ``duration`` / ``min_block`` 必须合法（规划关键参数，坏值宁缺毋滥——该类型
      回退 daily_routine）；
    - 五项速率可选（缺省 0.0），非有限数值 → 条目排除；
    - 四个比例位可选，越界钳入 [0, 1] 并落 warning。
    """
    prefix = f"profiles[{key}]"
    if not isinstance(raw, dict):
        raise _EntryRejected(key, f"{prefix}：期望 对象，实得 {_fmt(raw)}")
    unknown = [k for k in raw if k not in PROFILE_KEYS]
    for k in unknown:
        warnings.append(f"{prefix} 未知键 {k!r} 已丢弃（其余键继续生效）")
    label_raw = raw.get("label")
    if isinstance(label_raw, str) and label_raw.strip():
        label = label_raw.strip()
    else:
        if label_raw is not None:
            warnings.append(f"{prefix}.label 非法（期望非空字符串），回退类型名 {key!r}")
        label = key
    duration = _check_duration(raw.get("duration"), key)
    min_block = raw.get("min_block")
    if not _is_finite_number(min_block) or float(min_block) <= 0:
        raise _EntryRejected(key, f"{prefix}.min_block：期望 正的有限数值（非正数/缺失"
                                  f"一律拒绝），实得 {_fmt(min_block)}")
    rates: Dict[str, float] = {}
    for rate_key in ("energy_rate", "hunger_rate", "focus_rate",
                     "stress_rate", "social_rate"):
        value = raw.get(rate_key)
        if value is None:
            rates[rate_key] = 0.0
        elif not _is_finite_number(value):
            raise _EntryRejected(key, f"{prefix}.{rate_key}：期望 有限数值"
                                      f"（bool/字符串/NaN/Infinity 一律拒绝），实得 {_fmt(value)}")
        else:
            rates[rate_key] = float(value)
    interruptibility = _check_ratio(raw.get("interruptibility"),
                                    f"{prefix}.interruptibility", key, 0.5, warnings)
    switch_cost = _check_ratio(raw.get("switch_cost"),
                               f"{prefix}.switch_cost", key, 0.5, warnings)
    resume_bias = _check_ratio(raw.get("resume_bias"),
                               f"{prefix}.resume_bias", key, 0.0, warnings)
    spontaneous_weight = _check_ratio(raw.get("spontaneous_weight"),
                                      f"{prefix}.spontaneous_weight", key, 0.0, warnings)
    return ActivityProfile(
        key=key, label=label, duration=duration, min_block=float(min_block),
        interruptibility=interruptibility, switch_cost=switch_cost,
        energy_rate=rates["energy_rate"], hunger_rate=rates["hunger_rate"],
        focus_rate=rates["focus_rate"], stress_rate=rates["stress_rate"],
        social_rate=rates["social_rate"], resume_bias=resume_bias,
        spontaneous_weight=spontaneous_weight)


def validate_profiles_document(raw: Any) -> ProfilesLoadResult:
    """校验已解析的画像文档（**纯函数**，不碰文件系统）。

    顶层级失败 → ``error``（整文件回退）；条目级失败 → 该条进 ``rejected``、其余保留；
    ``profiles`` 键缺失 → 合法但为空 + warning（与「无自定义」同语义）；
    条目未知键丢弃该键（warning 留账）。
    """
    try:
        if not isinstance(raw, dict):
            raise _Rejected("", "type", f"顶层：期望 JSON 对象，实得 {_fmt(raw)}")
        _check_version(raw)
        _check_top_level_keys(raw)
    except _Rejected as exc:
        return ProfilesLoadResult(error={"error_key": exc.error_key,
                                         "error_kind": exc.error_kind,
                                         "detail": exc.detail})
    warnings: List[str] = []
    accepted: List[Tuple[str, ActivityProfile]] = []
    rejected: List[Tuple[str, str]] = []
    raw_profiles = raw.get("profiles")
    if raw_profiles is None:
        warnings.append("profiles 键缺失：视为无自定义，全部使用内置画像")
    elif not isinstance(raw_profiles, dict):
        return ProfilesLoadResult(error={"error_key": "profiles", "error_kind": "type",
                                         "detail": f"profiles：期望 对象，实得 {_fmt(raw_profiles)}"})
    else:
        for key, item in raw_profiles.items():
            name = str(key).strip()
            if not name:
                rejected.append((str(key), "profiles 键：期望 非空字符串类型名，实得 空白"))
                continue
            try:
                profile = _check_profile(name, item, warnings)
            except _EntryRejected as exc:
                rejected.append((exc.name, exc.detail))
                continue
            accepted.append((name, profile))
    return ProfilesLoadResult(accepted=tuple(accepted), rejected=tuple(rejected),
                              warnings=tuple(warnings))


# ── 合并 ─────────────────────────────────────────────────────────────────
def merge_tables(result: ProfilesLoadResult) -> ProfileTable:
    """内置 ⊕ json（纯函数）：json 同类型原位覆盖内置、新类型追加；顶层级错误 → 纯内置。"""
    builtin = builtin_table()
    if result.error is not None:
        return builtin
    merged: List[Tuple[str, ActivityProfile]] = list(builtin.entries)
    positions: Dict[str, int] = {name: i for i, (name, _) in enumerate(merged)}
    for name, profile in result.accepted:
        if name in positions:
            merged[positions[name]] = (name, profile)
        else:
            positions[name] = len(merged)
            merged.append((name, profile))
    return ProfileTable(entries=tuple(merged))


# ── 装载入口 ──────────────────────────────────────────────────────────────
def _plugin_root() -> Path:
    """插件根目录（本模块位于 ``<plugin_root>/planning/`` 下）。"""
    return Path(__file__).resolve().parents[1]


def _read_document(path: Path) -> Any:
    """读取并解析画像文件；失败统一 ``_Rejected(error_kind="parse")``。"""
    from core.mapping_dir import MappingReadError, read_json_text  # 延迟导入：顶层零插件依赖

    try:
        return read_json_text(path)
    except MappingReadError as exc:
        raise _Rejected("", "parse", exc.detail) from exc


# ── 落账 action（与 activity_properties 的 planning.properties_* 对齐）────
#: 采纳（info：accepted/rejected/warnings 清单）
ACTION_APPLIED = "planning.profiles_applied"
#: 整文件回退 / 条目排除（warn：原因）
ACTION_REJECTED = "planning.profiles_rejected"


def _record(result: ProfilesLoadResult, decision_logger: Any, *,
            merged: ProfileTable) -> None:
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
               accepted=[name for name, _ in merged.entries],
               rejected=rejected_list,
               warnings=list(result.warnings))
    except Exception:
        logger.debug("活动画像表落账异常（忽略）", exc_info=True)


def _generate_default(path: Path) -> None:
    """生成内置默认画像表（best-effort：失败仅告警，不阻断装载）。"""
    import json as _json

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = {"version": SUPPORTED_VERSION, "profiles": {}}
        profiles: Dict[str, Dict[str, Any]] = {}
        for name, p in BUILTIN_PROFILES:
            duration: Dict[str, Any] = {"dist": p.duration.dist,
                                        "sigma": p.duration.sigma,
                                        "min": p.duration.min_minutes,
                                        "max": p.duration.max_minutes}
            if p.duration.dist == "normal":
                duration["mean"] = p.duration.mean
            else:
                duration["median"] = p.duration.median
            profiles[name] = {
                "label": p.label, "duration": duration, "min_block": p.min_block,
                "interruptibility": p.interruptibility, "switch_cost": p.switch_cost,
                "energy_rate": p.energy_rate, "hunger_rate": p.hunger_rate,
                "focus_rate": p.focus_rate, "stress_rate": p.stress_rate,
                "social_rate": p.social_rate, "resume_bias": p.resume_bias,
                "spontaneous_weight": p.spontaneous_weight,
            }
        doc["profiles"] = profiles
        path.write_text(_json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        logger.info("已生成默认活动画像表：%s（可自行编辑，时长分布/速率随画像生效）", path)
    except Exception as exc:
        logger.warning("生成默认活动画像表失败（忽略）：%s", exc)


def load_profiles(*, root_dir: Any = None,
                  decision_logger: Any = None) -> Tuple[ProfilesLoadResult, ProfileTable]:
    """装载画像表（**只读一遍**；``root_dir=None`` 测试桩 → 跳过根层纯内置）。"""
    if root_dir is None:
        return ProfilesLoadResult(), builtin_table()
    from core.mapping_dir import ensure_user_table, mappings_path  # 延迟导入：顶层零插件依赖

    path = mappings_path(Path(root_dir), PROFILES_FILE_NAME)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        # §29.2 同款补链：表层缺失 → ensure_user_table 按序补齐（旧根迁移优先，
        # 仍缺才从出厂种子映射表复制；解析错误不算缺失、绝不覆盖用户文件）
        ensure_user_table(root_dir, PROFILES_FILE_NAME)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        _generate_default(path)
        result = ProfilesLoadResult(generated_default=True)
        return result, builtin_table()
    try:
        raw = _read_document(path)
    except _Rejected as exc:
        result = ProfilesLoadResult(error={"error_key": exc.error_key,
                                           "error_kind": exc.error_kind,
                                           "detail": exc.detail})
        table = builtin_table()
        _record(result, decision_logger, merged=table)
        logger.warning("活动画像表整文件回退内置（%s @ %s）：%s",
                       result.error["error_kind"], result.error["error_key"],
                       result.error["detail"])
        return result, table
    result = validate_profiles_document(raw)
    merged = merge_tables(result)
    _record(result, decision_logger, merged=merged)
    if result.rejected:
        logger.warning("活动画像表有 %d 个条目被排除：%s", len(result.rejected),
                       "；".join(f"{n}（{d}）" for n, d in result.rejected))
    elif result.warnings:
        logger.info("活动画像表提示：%s", "；".join(result.warnings))
    return result, merged


# ── 生效位（仿 activity_properties.apply_tables 的重绑模式）───────────────
_ACTIVE: Optional[ProfileTable] = None
#: 缺失类型告警去重（每次 apply_profiles 重置；同一类型每轮只 warn 一次防刷屏）
_MISSING_WARNED: set = set()


def apply_profiles(table: ProfileTable) -> None:
    """重绑生效画像表（on_load/热重载调用一次；``_ACTIVE=None`` = 未装载用内置）。"""
    global _ACTIVE
    _ACTIVE = table
    _MISSING_WARNED.clear()


def active_profiles() -> ProfileTable:
    """当前生效画像表（未装载时 = 内置）。"""
    if _ACTIVE is None:
        return builtin_table()
    return _ACTIVE


def get_profile(profile_type: Any) -> ActivityProfile:
    """类型 → 画像（缺失类型回退 ``daily_routine`` 并记日志——B.3 说明原文）。

    同一缺失类型每轮（apply_profiles 之间）只 warn 一次，其余走 debug，防 tick 刷屏。
    """
    table = active_profiles()
    profile = table.get(profile_type)
    if profile is not None:
        return profile
    key = str(profile_type or "").strip()
    if key not in _MISSING_WARNED:
        _MISSING_WARNED.add(key)
        logger.warning("活动画像表缺类型 %r，回退 %s（可在 mappings/%s 补该条目）",
                       key, DEFAULT_PROFILE_ID, PROFILES_FILE_NAME)
    else:
        logger.debug("活动画像表缺类型 %r，回退 %s", key, DEFAULT_PROFILE_ID)
    fallback = table.get(DEFAULT_PROFILE_ID)
    if fallback is not None:
        return fallback
    for _, profile in builtin_table().entries:
        if profile.key == DEFAULT_PROFILE_ID:
            return profile
    return builtin_table().entries[0][1]  # 理论不可达（内置恒含 daily_routine）


def sample_duration(profile: ActivityProfile, state: Any,
                    remaining_window: Any = None,
                    rng: Any = None) -> int:
    """时长采样（B.6.2 原文口径；纯离线零 LLM）。

    步骤：

    1. 按画像分布采样（``normal`` → ``rng.gauss(mean, sigma)``；
       ``lognormal`` → ``rng.lognormvariate(log(median), sigma)``）；
    2. 截断到 ``[duration.min, duration.max]``；
    3. ``remaining_window`` 非 None 时再裁到剩余窗口；
    4. 精力 < 0.3 且 ``energy_rate < 0`` → 时长 ×0.7（低精力向短端偏移）；
    5. 返回 ``max(min_block, round(minutes))``（``min_block`` 由调用方保证；
       「剩余时间不足最小块时不硬塞」是规划层的取舍，采样器按 B.6.2 原样返回）。

    ``state`` 取 ``energy``：dict 走 ``state["energy"]``、对象走 ``.energy``；
    取不到按 1.0（不做低精力偏移）。``rng`` 缺省用模块级 ``random``（调用方可传
    按日期派生的 ``random.Random`` 实例保证可复现）。
    """
    if rng is None:
        rng = random
    spec = profile.duration
    minutes = spec.sample(rng)
    minutes = max(spec.min_minutes, min(spec.max_minutes, minutes))
    if remaining_window is not None:
        try:
            window = float(remaining_window)
            if window >= 0:
                minutes = min(minutes, window)
        except (TypeError, ValueError):
            pass  # 垃圾窗口按未提供处理（不裁剪）
    if _energy_of(state) < LOW_ENERGY_THRESHOLD and profile.energy_rate < 0:
        minutes *= LOW_ENERGY_SCALE
    return max(profile.min_block, round(minutes))


def load_and_apply(*, root_dir: Any = None,
                   decision_logger: Any = None) -> ProfilesLoadResult:
    """装载 + 合并 + 重绑生效位（插件装配链的单次调用入口）。"""
    result, merged = load_profiles(root_dir=root_dir, decision_logger=decision_logger)
    apply_profiles(merged)
    return result
