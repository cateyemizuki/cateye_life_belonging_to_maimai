"""小事件池配置：六项配额决定生成数量，按一个周期区间自动更新。"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, ClassVar, Dict
import re
from pydantic import field_validator
from maibot_sdk import Field, PluginConfigBase
from core.coerce import as_int as _as_int

DEFAULT_REFRESH_CYCLE = "5-8"
DEFAULT_MIN_INTERVAL_DAYS = 5
DEFAULT_MAX_INTERVAL_DAYS = 8
DEFAULT_REUSABLE_00_08 = 1
DEFAULT_REUSABLE_08_16 = 5
DEFAULT_REUSABLE_16_24 = 4
DEFAULT_ONE_SHOT_00_08 = 3
DEFAULT_ONE_SHOT_08_16 = 6
DEFAULT_ONE_SHOT_16_24 = 5
EVENT_QUOTA_MAX = 12
DEFAULT_NO_PUZZLE = True
DEFAULT_TASK_NAME = "utils"
DEFAULT_TEMPERATURE = 0.9
DEFAULT_AUTO_CHECK_SECONDS = 300


def parse_refresh_cycle(value: Any) -> tuple[int, int]:
    """接受 5-8 或单个天数；合法范围为 1..365 天，反向区间自动整理。"""
    match = re.fullmatch(r"\s*(\d+)(?:\s*[-~～]\s*(\d+))?\s*", str(value or ""))
    if not match:
        return DEFAULT_MIN_INTERVAL_DAYS, DEFAULT_MAX_INTERVAL_DAYS
    low, high = int(match[1]), int(match[2] or match[1])
    if not (1 <= low <= 365 and 1 <= high <= 365):
        return DEFAULT_MIN_INTERVAL_DAYS, DEFAULT_MAX_INTERVAL_DAYS
    return min(low, high), max(low, high)


def derive_pool_cap(builtin_count: int, reusable_limits: Any, one_shot_limits: Any) -> int:
    """池上限派生（3.17.6 PR-REMOVE-POOLCAP）：内置数 + 三时段六键配额和。

    独立总配额 ``max_pool_events`` 已退役（用户裁定：三时段配额是生成数量的
    唯一权威，独立总上限冗余且与配额冲突 = E_CAPACITY 根因）。派生值永远与
    配额自洽：生成项数 == 配额和时不触发截断/拒绝；配额和变化上限随之变化。
    有界保护仍在：每段配额 le=EVENT_QUOTA_MAX(12)，池总量天然 bounded。
    """
    return (max(0, int(builtin_count))
            + sum(max(0, int(x)) for x in reusable_limits)
            + sum(max(0, int(x)) for x in one_shot_limits))


def _attr(source: Any, name: str, default: Any) -> Any:
    return source.get(name, default) if isinstance(source, dict) else getattr(source, name, default)


class EventCatalogSectionConfig(PluginConfigBase):
    """自动补充角色生活中的小事件，空白时立即尝试生成。"""
    __ui_label__ = "小事件池"
    __ui_order__ = 14

    refresh_cycle: str = Field(default=DEFAULT_REFRESH_CYCLE,
        description="小事件池每次更新后，在此范围内随机选择下次更新间隔，单位为天。例如 5-8 表示隔 5 至 8 天更新；空白事件池会立即尝试补充。",
        json_schema_extra={"label": "小事件池子变化周期", "hint": "填写 1 至 365 天内的区间，如 5-8，或填写单个天数。"})
    reusable_00_08: int = Field(default=1, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 00:00–08:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "00-08可复用生成配额"})
    reusable_08_16: int = Field(default=5, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 08:00–16:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "08-16可复用生成配额"})
    reusable_16_24: int = Field(default=4, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 16:00–24:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "16-24可复用生成配额"})
    one_shot_00_08: int = Field(default=3, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 00:00–08:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "00-08一次性生成配额"})
    one_shot_08_16: int = Field(default=6, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 08:00–16:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "08-16一次性生成配额"})
    one_shot_16_24: int = Field(default=5, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 16:00–24:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "16-24一次性生成配额"})
    no_puzzle: bool = Field(default=DEFAULT_NO_PUZZLE,
        description="生成活动时避免反复使用拼图、十字绣等套路，增加符合角色人设的生活变化。",
        json_schema_extra={"label": "避免套路化活动"})
    task_name: str = Field(default=DEFAULT_TASK_NAME,
        description="生成小事件使用的宿主模型任务名称，默认 utils；任务不存在时使用 utils。",
        json_schema_extra={"label": "小事件模型任务"})

    @field_validator("refresh_cycle", mode="before")
    @classmethod
    def _normalize_cycle(cls, value: Any) -> str:
        low, high = parse_refresh_cycle(value)
        return f"{low}-{high}"

    def normalized(self) -> "EventCatalogConfig":
        return resolve_config(self)


@dataclass(frozen=True)
class EventCatalogConfig:
    refresh_cycle: str = DEFAULT_REFRESH_CYCLE
    reusable_limits: tuple = (1, 5, 4)
    one_shot_limits: tuple = (3, 6, 5)
    no_puzzle: bool = DEFAULT_NO_PUZZLE
    task_name: str = DEFAULT_TASK_NAME
    temperature: float = DEFAULT_TEMPERATURE
    auto_check_seconds: int = DEFAULT_AUTO_CHECK_SECONDS

    @property
    def min_interval_days(self) -> int:
        return parse_refresh_cycle(self.refresh_cycle)[0]

    @property
    def max_interval_days(self) -> int:
        return parse_refresh_cycle(self.refresh_cycle)[1]

    @property
    def period_limits(self) -> tuple:
        return tuple(r + o for r, o in zip(self.reusable_limits, self.one_shot_limits))

    @property
    def period_limit_total(self) -> int:
        return sum(self.period_limits)

    @property
    def max_pool_events(self) -> int:
        from .small_events import builtin_events
        return derive_pool_cap(len(builtin_events()), self.reusable_limits, self.one_shot_limits)


DEFAULT_CONFIG = EventCatalogConfig()


def resolve_config(source: Any) -> EventCatalogConfig:
    if source is None:
        return DEFAULT_CONFIG
    nested = _attr(source, "event_catalog", None)
    if nested is not None:
        source = nested
    def quota(name, default):
        return max(0, min(EVENT_QUOTA_MAX, _as_int(_attr(source, name, default), default)))
    low, high = parse_refresh_cycle(_attr(source, "refresh_cycle", DEFAULT_REFRESH_CYCLE))
    return EventCatalogConfig(
        refresh_cycle=f"{low}-{high}",
        reusable_limits=tuple(quota("reusable_" + slot, default) for slot, default in zip(("00_08", "08_16", "16_24"), (1, 5, 4))),
        one_shot_limits=tuple(quota("one_shot_" + slot, default) for slot, default in zip(("00_08", "08_16", "16_24"), (3, 6, 5))),
        no_puzzle=bool(_attr(source, "no_puzzle", DEFAULT_NO_PUZZLE)),
        task_name=str(_attr(source, "task_name", DEFAULT_TASK_NAME) or DEFAULT_TASK_NAME),
    )


def section_or_default(plugin: Any, attr: str = "event_catalog") -> EventCatalogConfig:
    return resolve_config(getattr(getattr(plugin, "config", None), attr, None))


class EventCatalogFields(PluginConfigBase):
    """日程使用的小事件池配额与自动更新周期。"""
    event_catalog_refresh_cycle: str = Field(default=DEFAULT_REFRESH_CYCLE,
        description="小事件池每次更新后，在此范围内随机选择下次更新间隔，单位为天。例如 5-8 表示隔 5 至 8 天更新；空白事件池会立即尝试补充。",
        json_schema_extra={"label": "小事件池子变化周期", "hint": "填写 1 至 365 天内的区间，如 5-8，或填写单个天数。"})
    event_catalog_reusable_00_08: int = Field(default=1, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 00:00–08:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "00-08可复用生成配额"})
    event_catalog_reusable_08_16: int = Field(default=5, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 08:00–16:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "08-16可复用生成配额"})
    event_catalog_reusable_16_24: int = Field(default=4, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 16:00–24:00 时段生成的可复用小事件数量。生成后可在多个日程中反复安排；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "16-24可复用生成配额"})
    event_catalog_one_shot_00_08: int = Field(default=3, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 00:00–08:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "00-08一次性生成配额"})
    event_catalog_one_shot_08_16: int = Field(default=6, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 08:00–16:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "08-16一次性生成配额"})
    event_catalog_one_shot_16_24: int = Field(default=5, ge=0, le=EVENT_QUOTA_MAX, description='每次更新时，为 16:00–24:00 时段生成的一次性小事件数量。安排并完成一次后，本次更新周期内不再重复使用；0 表示此时段不生成这类事件，最多 12 条，只影响生成池，不改变内置事件。', json_schema_extra={"label": "16-24一次性生成配额"})
    event_catalog_no_puzzle: bool = Field(default=DEFAULT_NO_PUZZLE,
        description="生成活动时避免反复使用拼图、十字绣等套路，增加符合角色人设的生活变化。",
        json_schema_extra={"label": "避免套路化活动"})
    event_catalog_task_name: str = Field(default=DEFAULT_TASK_NAME,
        description="生成小事件使用的宿主模型任务名称，默认 utils；任务不存在时使用 utils。",
        json_schema_extra={"label": "小事件模型任务"})

    @field_validator("event_catalog_refresh_cycle", mode="before")
    @classmethod
    def _normalize_event_cycle(cls, value: Any) -> str:
        low, high = parse_refresh_cycle(value)
        return f"{low}-{high}"
