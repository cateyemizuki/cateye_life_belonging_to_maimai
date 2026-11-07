# -*- coding: utf-8 -*-
"""Small-event configuration and the flattened schedule fields.

v3.9.2：三时段配额按「可复用 / 一次性」分开配置（reusable_* / one_shot_*），
每时段请求条数 = 两者之和；默认合计生成 24 条（可复用 10 + 一次性 14），
总池 = 内置 5 + 生成 24 = 29，上限 32。更新周期默认 7–10 天（用户自定义
范围仍按 1–365 夹取，不锁死默认值）。Legacy event_catalog input is migrated
by the root；旧 period_limit_* 三键退役（纯 pop，不搬运）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Dict, Literal, Mapping, Optional

from maibot_sdk import Field, PluginConfigBase

from core.coerce import as_int as _as_int
from core.internal_tuning import tuning_value as _tuning_value

#: 代码默认（定稿 §1 #6：部署灰度先 manual，代码默认 off）
DEFAULT_MODE = "off"
#: v3.9.2 用户裁定：更新周期默认 7–10 天；自定义范围仍按 1–365 夹取
DEFAULT_MIN_INTERVAL_DAYS = 7
DEFAULT_MAX_INTERVAL_DAYS = 10
#: 定稿 §4.3.1 / 附录 B.6.3：生成条数上限（旧单池口径，v3.9.2 起双池条数由时段配额决定）
DEFAULT_MAX_GENERATED_EVENTS = 8
DEFAULT_MIN_GENERATED_EVENTS = 1
#: v3.9.2 三时段「可复用 / 一次性」独立配额（00-08 / 08-16 / 16-24）：
#: 可复用默认 1/5/4（合计 10），一次性默认 3/6/5（合计 14），总计生成 24 条。
DEFAULT_REUSABLE_00_08 = 1
DEFAULT_REUSABLE_08_16 = 5
DEFAULT_REUSABLE_16_24 = 4
DEFAULT_ONE_SHOT_00_08 = 3
DEFAULT_ONE_SHOT_08_16 = 6
DEFAULT_ONE_SHOT_16_24 = 5
#: 单项配额上限（每时段每池 0–12）
EVENT_QUOTA_MAX = 12
#: v3.9.2 总池扩容：内置 5 + 生成 24 = 29，上限 32（internal_tuning.json 同钉）
DEFAULT_MAX_POOL_EVENTS = 32
#: NO Puzzle 开关默认开：生成提示词追加反套路引导（避免拼图/十字绣等滥用活动）
DEFAULT_NO_PUZZLE = True
DEFAULT_TASK_NAME = "utils"
DEFAULT_TEMPERATURE = 0.9
#: 自动检查节拍（秒）；每天最多真正生成一次，检查本身不消耗 LLM
DEFAULT_AUTO_CHECK_SECONDS = 300

VALID_MODES = ("off", "manual", "auto")


def _i18n(en_label: str, en_hint: str = "") -> dict:
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class EventCatalogSectionConfig(PluginConfigBase):
    """生成符合角色人设的日常小事件，供空闲或转场时安排。"""

    __ui_label__ = "事件库（生成小事件）"
    __ui_icon__ = "sparkles"
    __ui_order__ = 12
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Event Catalog (generated events)",
               "description": "LLM-generated small events used as idle/transition fillers. Off by default."}
    }

    mode: Literal["off", "manual", "auto"] = Field(
        default=DEFAULT_MODE,
        description="事件库模式：off仅使用内置事件，manual手动生成，auto到期自动生成。",
        json_schema_extra={
            "label": "事件库模式",
            "hint": "自动模式按生成周期更新事件，手动模式使用初始化事件库命令。",
            **_i18n("Event catalog mode", "off = builtin pool only; manual = command only; auto = due background update."),
        },
    )
    min_interval_days: int = Field(
        default=DEFAULT_MIN_INTERVAL_DAYS,
        ge=1,
        le=365,
        description="生成周期下限（天，建议 7）",
        json_schema_extra={"label": "生成周期下限(天)", **_i18n("Min interval (days)")},
    )
    max_interval_days: int = Field(
        default=DEFAULT_MAX_INTERVAL_DAYS,
        ge=1,
        le=365,
        description="生成周期上限（天，建议 10）",
        json_schema_extra={"label": "生成周期上限(天)", **_i18n("Max interval (days)")},
    )
    max_generated_events: int = Field(
        default=DEFAULT_MAX_GENERATED_EVENTS,
        ge=DEFAULT_MIN_GENERATED_EVENTS,
        le=DEFAULT_MAX_GENERATED_EVENTS,
        description="每个生成池的条数上限（1至8，旧单池口径）；v3.9.2 起双池条数由三时段可复用/一次性配额决定。",
        json_schema_extra={"label": "每池生成上限", "hint":"默认总容量32：内置5+生成24；一次性抽中后本周期不再出现。", **_i18n("Generated events per pool")},
    )
    reusable_00_08: int = Field(
        default=DEFAULT_REUSABLE_00_08, ge=0, le=EVENT_QUOTA_MAX,
        description="00:00–08:00 时段可复用小事件生成配额（0=该时段该池不生成）。深夜清晨低干扰活动。",
        json_schema_extra={"label": "00-08可复用生成配额", **_i18n("00-08 reusable quota")},
    )
    reusable_08_16: int = Field(
        default=DEFAULT_REUSABLE_08_16, ge=0, le=EVENT_QUOTA_MAX,
        description="08:00–16:00 时段可复用小事件生成配额（0=该时段该池不生成）。日间家务/照料/学习。",
        json_schema_extra={"label": "08-16可复用生成配额", **_i18n("08-16 reusable quota")},
    )
    reusable_16_24: int = Field(
        default=DEFAULT_REUSABLE_16_24, ge=0, le=EVENT_QUOTA_MAX,
        description="16:00–24:00 时段可复用小事件生成配额（0=该时段该池不生成）。傍晚夜晚温和休闲。",
        json_schema_extra={"label": "16-24可复用生成配额", **_i18n("16-24 reusable quota")},
    )
    one_shot_00_08: int = Field(
        default=DEFAULT_ONE_SHOT_00_08, ge=0, le=EVENT_QUOTA_MAX,
        description="00:00–08:00 时段一次性小事件生成配额（抽中后本周期不再出现）。深夜清晨低干扰活动。",
        json_schema_extra={"label": "00-08一次性生成配额", **_i18n("00-08 one-shot quota")},
    )
    one_shot_08_16: int = Field(
        default=DEFAULT_ONE_SHOT_08_16, ge=0, le=EVENT_QUOTA_MAX,
        description="08:00–16:00 时段一次性小事件生成配额（抽中后本周期不再出现）。日间家务/照料/学习。",
        json_schema_extra={"label": "08-16一次性生成配额", **_i18n("08-16 one-shot quota")},
    )
    one_shot_16_24: int = Field(
        default=DEFAULT_ONE_SHOT_16_24, ge=0, le=EVENT_QUOTA_MAX,
        description="16:00–24:00 时段一次性小事件生成配额（抽中后本周期不再出现）。傍晚夜晚温和休闲。",
        json_schema_extra={"label": "16-24一次性生成配额", **_i18n("16-24 one-shot quota")},
    )
    no_puzzle: bool = Field(
        default=DEFAULT_NO_PUZZLE,
        json_schema_extra={"label": "NO Puzzle"},
    )
    # 2026-11-04 配置精简轮（J 类）：max_pool_events 移入
    # mappings/internal_tuning.json[event_catalog]（内部容量）；同名只读 property 见下。
    task_name: str = Field(
        default=DEFAULT_TASK_NAME,
        description="生成小事件使用的宿主任务名，默认 utils；自定义任务不存在时使用 utils",
        json_schema_extra={"label": "LLM 任务名", **_i18n("LLM task name")},
    )

    # H 类（2026-11-04 配置精简轮）：
    # - ``max_events`` 隐藏兼容 Field 已删：旧键仍由 migrate_legacy_event_catalog 在
    #   默认填充前搬进规范键 max_generated_events 后 pop（生产两键同值 8，无损）；
    #   resolve_config 对原始 dict 的旧读法兜底保留。
    # - ``temperature`` 隐藏 Field 已删：2026-10-27 用户锁定 0.9，resolve_config 恒返回
    #   DEFAULT_TEMPERATURE（配置值本就不生效）；旧 TOML 键经 LEGACY_ROUTE_KEYS 入口 pop。

    @property
    def max_pool_events(self) -> int:
        """内置与生成事件合并后的总条数上限（J 类 → internal_tuning.json）。"""
        return int(_tuning_value("event_catalog", "max_pool_events"))

    def normalized(self) -> "EventCatalogConfig":
        """Pydantic 段 → 服务只读视图（不做 IO）。"""
        return resolve_config(self)


@dataclass(frozen=True)
class EventCatalogConfig:
    """服务使用的只读配置快照（与 section / dict / SimpleNamespace 解耦）。"""

    mode: str = DEFAULT_MODE
    min_interval_days: int = DEFAULT_MIN_INTERVAL_DAYS
    max_interval_days: int = DEFAULT_MAX_INTERVAL_DAYS
    max_generated_events: int = DEFAULT_MAX_GENERATED_EVENTS
    #: 三时段配额（顺序对齐 event_slots.TIME_SLOTS）：可复用 / 一次性分开
    reusable_limits: tuple = (DEFAULT_REUSABLE_00_08, DEFAULT_REUSABLE_08_16, DEFAULT_REUSABLE_16_24)
    one_shot_limits: tuple = (DEFAULT_ONE_SHOT_00_08, DEFAULT_ONE_SHOT_08_16, DEFAULT_ONE_SHOT_16_24)
    no_puzzle: bool = DEFAULT_NO_PUZZLE
    max_pool_events: int = DEFAULT_MAX_POOL_EVENTS
    enforce_pool_cap: bool = True
    task_name: str = DEFAULT_TASK_NAME

    temperature: float = DEFAULT_TEMPERATURE

    auto_check_seconds: int = DEFAULT_AUTO_CHECK_SECONDS

    @property
    def period_limits(self) -> tuple:
        """三时段合计请求条数（可复用+一次性），顺序对齐 event_slots.TIME_SLOTS。"""
        return tuple(int(r) + int(o) for r, o in zip(self.reusable_limits, self.one_shot_limits))

    @property
    def period_limit_total(self) -> int:
        return sum(self.period_limits)

    @property
    def generation_allowed_by_mode(self) -> bool:
        return self.mode in ("manual", "auto")

    @property
    def auto_enabled(self) -> bool:
        return self.mode == "auto"


DEFAULT_CONFIG = EventCatalogConfig()


def _attr(source: Any, name: str, default: Any) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _explicitly_set(source: Any, name: str) -> Optional[bool]:
    """pydantic 模型判断字段是否由用户显式给出；非 pydantic 返回 None。

    ``max_events`` 是旧键：只有用户显式写旧键、且没写规范字段时才接管，
    避免 pydantic 默认值把兼容读法永远压掉。
    """
    fields_set = getattr(source, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(source, "__fields_set__", None)
    if isinstance(fields_set, (set, frozenset)):
        return name in fields_set
    return None


def resolve_config(source: Any) -> EventCatalogConfig:
    """把 section / dict / SimpleNamespace / None 归一化成只读配置。

    解析优先级（已按用户 2026-10-26 答复落地）：

    1. ``max_generated_events`` 显式给出 → 用它；
    2. 仅旧键 ``max_events`` 显式给出 → 按生成条数兼容接管；
    3. 都没有 → 规范字段默认 8。

    总池上限读 ``max_pool_events``（规范字段，v3.9.2 默认 32）、生成上限读
    ``max_generated_events``（规范字段，默认 8，旧单池口径）；``max_events`` 仅作旧读法兜底。
    用户已裁定合并总池 <=32，故 ``enforce_pool_cap`` 默认 True；保留该参数仅为
    兼容旧测试/显式放宽。
    """
    if source is None:
        return DEFAULT_CONFIG
    # 允许直接传插件聚合配置：未挂 [event_catalog] 时按默认 off
    if not isinstance(source, dict) and not hasattr(source, "mode"):
        nested = getattr(source, "event_catalog", None)
        if nested is not None:
            source = nested
    mode = str(_attr(source, "mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    if mode not in VALID_MODES:
        mode = DEFAULT_MODE
    generated = _attr(source, "max_generated_events", None)
    canonical_explicit = _explicitly_set(source, "max_generated_events")
    legacy_explicit = _explicitly_set(source, "max_events")
    if canonical_explicit is True:
        pass  # 规范字段优先
    elif canonical_explicit is False and legacy_explicit is True:
        generated = _attr(source, "max_events", DEFAULT_MAX_GENERATED_EVENTS)
    elif generated is None:
        generated = _attr(source, "max_events", DEFAULT_MAX_GENERATED_EVENTS)
    generated = _as_int(generated, DEFAULT_MAX_GENERATED_EVENTS)
    generated = max(DEFAULT_MIN_GENERATED_EVENTS, min(DEFAULT_MAX_GENERATED_EVENTS, generated))
    min_days = max(1, _as_int(_attr(source, "min_interval_days", DEFAULT_MIN_INTERVAL_DAYS),
                              DEFAULT_MIN_INTERVAL_DAYS))
    max_days = max(1, _as_int(_attr(source, "max_interval_days", DEFAULT_MAX_INTERVAL_DAYS),
                              DEFAULT_MAX_INTERVAL_DAYS))
    if min_days > max_days:
        min_days, max_days = max_days, min_days
    # v3.9.2：周期默认 7–10 天，但用户自定义范围不锁死默认值——只按 Field 边界 1–365 夹取
    min_days = max(1, min(365, min_days))
    max_days = max(1, min(365, max_days))
    if min_days > max_days:
        min_days, max_days = max_days, min_days
    # 用户锁定 temperature=0.9（固定协议）：不读配置，避免出现「可编辑但永不生效」的死键。
    # 字段仍在 section 上（隐藏、默认 0.9）以便配置文件自洽；想改只能改本模块常量。
    temperature = DEFAULT_TEMPERATURE
    def _quota(name: str, default: int) -> int:
        return max(0, min(EVENT_QUOTA_MAX, _as_int(_attr(source, name, default), default)))
    return EventCatalogConfig(
        mode=mode,
        min_interval_days=min_days,
        max_interval_days=max_days,
        max_generated_events=generated,
        reusable_limits=(_quota("reusable_00_08", DEFAULT_REUSABLE_00_08),
                         _quota("reusable_08_16", DEFAULT_REUSABLE_08_16),
                         _quota("reusable_16_24", DEFAULT_REUSABLE_16_24)),
        one_shot_limits=(_quota("one_shot_00_08", DEFAULT_ONE_SHOT_00_08),
                         _quota("one_shot_08_16", DEFAULT_ONE_SHOT_08_16),
                         _quota("one_shot_16_24", DEFAULT_ONE_SHOT_16_24)),
        no_puzzle=bool(_attr(source, "no_puzzle", DEFAULT_NO_PUZZLE)),
        max_pool_events=max(1, _as_int(_attr(source, "max_pool_events", DEFAULT_MAX_POOL_EVENTS),
                                       DEFAULT_MAX_POOL_EVENTS)),
        enforce_pool_cap=bool(_attr(source, "enforce_pool_cap", True)),
        task_name=str(_attr(source, "task_name", DEFAULT_TASK_NAME) or DEFAULT_TASK_NAME),

        temperature=temperature,
        auto_check_seconds=max(30, _as_int(
            _attr(source, "auto_check_seconds", DEFAULT_AUTO_CHECK_SECONDS),
            DEFAULT_AUTO_CHECK_SECONDS)),
    )


def section_or_default(plugin: Any, attr: str = "event_catalog") -> EventCatalogConfig:
    """从插件聚合配置读段；未挂载时返回默认（服务可独立运行）。"""
    section = getattr(getattr(plugin, "config", None), attr, None)
    return resolve_config(section)


def migrate_legacy_event_catalog(section: Any,
                                 factory_defaults: Optional[Mapping[str, Any]] = None
                                 ) -> Any:
    """把**原始** ``[event_catalog]`` 字典里的旧键 ``max_events`` 迁到规范字段。

    为什么必须在「配置归一化默认填充之前」做（2026-10-27 用户裁定）：
    宿主/SDK 归一化会先把出厂默认 merge 进输入，之后 pydantic 的
    ``model_fields_set`` 里**每个**字段都算「显式给出」，段级 ``resolve_config``
    的「规范字段优先」判据因此失效——用户只写了 ``max_events=6`` 也会被
    默认 ``max_generated_events=8`` 吞掉。故在 ``CateyeSuiteConfig`` 的
    ``mode="before"`` 校验器里、默认值尚未落库时先做一次纯文本搬迁：

    1. 只有旧键 → ``max_generated_events = max_events``；
    2. 两个都有 → 规范字段优先（**包括规范字段值恰好等于出厂默认 8**：显式键即优先，
       不以「值等于默认」猜测显式性）；
    3. 迁完显式 ``pop`` 旧键（§9.5 口径：显式丢弃、不做键缺失猜测）。

    真实宿主路径的「默认 merge」问题由 ``plugin.normalize_plugin_config`` 在调用 SDK
    merge **之前**对原始输入先跑本函数解决（默认键一旦 merge 进来就无法再靠值猜测）。
    幂等：迁移结果只含规范字段，第二次进入不再触发任何搬运。
    ``factory_defaults`` 仅为旧调用签名兼容保留，不参与判定。
    """
    if not isinstance(section, dict):
        return section
    if "max_events" not in section:
        return section
    out = dict(section)
    legacy = _as_int(out.get("max_events"), DEFAULT_MAX_GENERATED_EVENTS)
    legacy = max(DEFAULT_MIN_GENERATED_EVENTS, min(DEFAULT_MAX_GENERATED_EVENTS, legacy))
    if "max_generated_events" in out:
        # 规范字段存在即为显式来源（8 也是显式值）→ 无条件优先，旧键显式丢弃。
        out.pop("max_events", None)
        return out
    out["max_generated_events"] = legacy
    out.pop("max_events", None)
    return out



class EventCatalogFields(PluginConfigBase):
    """小事件生成参数，在日程板块展示。"""

    event_catalog_mode: Literal["off", "manual", "auto"] = Field(
        default=DEFAULT_MODE,
        description="事件库模式：off仅使用内置事件，manual手动生成，auto到期自动生成。",
        json_schema_extra={
            "label": "事件库模式",
            "hint": "自动模式按生成周期更新事件，手动模式使用初始化事件库命令。",
            **_i18n("Event catalog mode", "off = builtin pool only; manual = command only; auto = due background update."),
        },
    )
    event_catalog_min_interval_days: int = Field(
        default=DEFAULT_MIN_INTERVAL_DAYS,
        ge=1,
        le=365,
        description="生成周期下限（天，建议 7）",
        json_schema_extra={"label": "生成周期下限(天)", **_i18n("Min interval (days)")},
    )
    event_catalog_max_interval_days: int = Field(
        default=DEFAULT_MAX_INTERVAL_DAYS,
        ge=1,
        le=365,
        description="生成周期上限（天，建议 10）",
        json_schema_extra={"label": "生成周期上限(天)", **_i18n("Max interval (days)")},
    )
    event_catalog_max_generated_events: int = Field(
        default=DEFAULT_MAX_GENERATED_EVENTS,
        ge=DEFAULT_MIN_GENERATED_EVENTS,
        le=DEFAULT_MAX_GENERATED_EVENTS,
        description="每个生成池的条数上限（1至8，旧单池口径）；v3.9.2 起双池条数由三时段可复用/一次性配额决定。",
        json_schema_extra={"label": "每池生成上限", "hint":"默认总容量32：内置5+生成24；一次性抽中后本周期不再出现。", **_i18n("Generated events per pool")},
    )
    event_catalog_reusable_00_08: int = Field(
        default=DEFAULT_REUSABLE_00_08, ge=0, le=EVENT_QUOTA_MAX,
        description="00:00–08:00 时段可复用小事件生成配额（0=该时段该池不生成）。",
        json_schema_extra={"label": "00-08可复用生成配额", **_i18n("00-08 reusable quota")},
    )
    event_catalog_reusable_08_16: int = Field(
        default=DEFAULT_REUSABLE_08_16, ge=0, le=EVENT_QUOTA_MAX,
        description="08:00–16:00 时段可复用小事件生成配额（0=该时段该池不生成）。",
        json_schema_extra={"label": "08-16可复用生成配额", **_i18n("08-16 reusable quota")},
    )
    event_catalog_reusable_16_24: int = Field(
        default=DEFAULT_REUSABLE_16_24, ge=0, le=EVENT_QUOTA_MAX,
        description="16:00–24:00 时段可复用小事件生成配额（0=该时段该池不生成）。",
        json_schema_extra={"label": "16-24可复用生成配额", **_i18n("16-24 reusable quota")},
    )
    event_catalog_one_shot_00_08: int = Field(
        default=DEFAULT_ONE_SHOT_00_08, ge=0, le=EVENT_QUOTA_MAX,
        description="00:00–08:00 时段一次性小事件生成配额（抽中后本周期不再出现）。",
        json_schema_extra={"label": "00-08一次性生成配额", **_i18n("00-08 one-shot quota")},
    )
    event_catalog_one_shot_08_16: int = Field(
        default=DEFAULT_ONE_SHOT_08_16, ge=0, le=EVENT_QUOTA_MAX,
        description="08:00–16:00 时段一次性小事件生成配额（抽中后本周期不再出现）。",
        json_schema_extra={"label": "08-16一次性生成配额", **_i18n("08-16 one-shot quota")},
    )
    event_catalog_one_shot_16_24: int = Field(
        default=DEFAULT_ONE_SHOT_16_24, ge=0, le=EVENT_QUOTA_MAX,
        description="16:00–24:00 时段一次性小事件生成配额（抽中后本周期不再出现）。",
        json_schema_extra={"label": "16-24一次性生成配额", **_i18n("16-24 one-shot quota")},
    )
    event_catalog_no_puzzle: bool = Field(
        default=DEFAULT_NO_PUZZLE,
        json_schema_extra={"label": "NO Puzzle"},
    )
    event_catalog_task_name: str = Field(
        default=DEFAULT_TASK_NAME,
        description="生成小事件使用的宿主任务名，默认 utils；自定义任务不存在时使用 utils",
        json_schema_extra={"label": "LLM 任务名", **_i18n("LLM task name")},
    )
