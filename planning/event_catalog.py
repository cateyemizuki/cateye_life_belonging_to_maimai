# -*- coding: utf-8 -*-
"""PR4 EventCatalog：LLM 小事件库（独立新模块，运行时 0 LLM）。

职责分层（定稿 §4.3 / 附录 B.1）：

- :class:`EventCatalogStore`：``mappings/generated_events.json`` 读/写；
  原子写 + ``.bak``（保留最近 3 份）；缺失/损坏/未知版本 → 内置池兜底；
  装载永不改写用户文件。
- :class:`EventCatalogValidator`：schema/语义/安全全量校验；错误码 E_*。
- :class:`EventCatalogService`：生成（经 ``LLMService`` 唯一出口）+ 校验 +
  全量替换 + 周期/每日一次/预算熔断/清醒闸/并发互斥 + 运行时 pick 与
  ``state_deltas`` 查询。任何异常都不向上抛，不拖挂插件。
- :func:`state_affinity` / ``Store.pick_event``：纯本地、0 LLM；
  保留 4h 去重、idle/转场语义与状态加权。

与旧路径兼容：

- 内置池与 ``rolling_planner.RANDOM_EVENT_POOL`` 逐条一致（测试钉住），
  生成的 ``event_id`` 统一 ``llm_`` 前缀，不覆盖内置 key；
- 生成的 episode 建议在创建时把 :meth:`EventCatalogService.metadata_for_event`
  的快照（``event_id/event_key/catalog_version/state_deltas``）写进 metadata；
  关闭时优先读快照，旧 episode 无快照再回查旧内置池；
- 本模块不 import ``rolling_planner``，避免后续 planner 反向 import 时成环。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import threading
from uuid import uuid4
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from core.coerce import is_finite_number as _is_number, is_int as _is_int
from core.llm_utils import LLMResult
from core.mapping_dir import MappingReadError, mappings_path, read_json_text
from core.storage import save_json_atomic

from .event_catalog_config import EventCatalogConfig, derive_pool_cap, resolve_config
from .event_pools import POOL_VERSION, normalize_cycle, allocate_pools, catalog_write_guard
from .event_prompt import (GENERATION_TEMPERATURE,
                           EventPromptError, build_event_prompt,
                           build_json_schema, parse_events_response)

logger = logging.getLogger(__name__)

# ── 常量（附录 B.1）──────────────────────────────────────────────────────────
CATALOG_VERSION = POOL_VERSION
CATALOG_FILE_NAME = "generated_events.json"
CATALOG_STATE_FILE_NAME = "event_catalog_state.json"
EVENT_ID_PREFIX = "llm_"
EVENT_ID_RE = re.compile(r"^llm_[a-z0-9_]{2,48}$")
#: 同事件 4 小时内不重复（A.7；与 rolling_planner 旧逻辑一致）
RANDOM_EVENT_DEDUP_MINUTES = 240
MIN_GENERATED_EVENTS = 3
MAX_GENERATED_EVENTS = 8
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 30
MIN_TEMPLATE_CHARS = 10
MAX_TEMPLATE_CHARS = 40
MAX_NAME_CHARS = 10
MIN_NAME_CHARS = 2
MAX_DELTA_ABS = 0.08
MIN_WEIGHT = 0.1
MAX_WEIGHT = 1.0
#: 五维状态（禁止 sleep_debt）
STATE_FIELDS: Tuple[str, ...] = ("energy", "hunger", "focus", "stress", "social_battery")
#: 允许的 goal_type：禁止 sleep/meal/pending_commitment（附录 B.1.4）
FORBIDDEN_GOAL_TYPES = frozenset({"sleep", "meal", "pending_commitment"})
ALLOWED_GOAL_TYPES = frozenset({"daily_routine", "rest", "social", "entertainment",
                                "exercise", "idle", "transition"})
#: 事件生成 LLM 调用点标签（接线工人需把 schedule_events 登记到
#: core/llm_usage.py 的 EVENT_MODULE_MAP → planning）
LLM_EVENT_NAME = "schedule_events"
LLM_FALLBACK_TASK = "utils"
EVENT_GENERATION_TEMPERATURE = GENERATION_TEMPERATURE
#: 3.17.6（PR-REMOVE-POOLCAP §2.5 E_LLM 截断修复）：事件生成显式 override
#: max_tokens——按本时段 requested 条数动态：下限 2000（全量验证 dwfrun-bebdbd00：
#: 16-24 段 9 事件需 ~999 completion tokens，max_tokens=2000 5/5 全绿、800 截断），
#: 每条 220 tokens，封顶 6000（有界不无界放大；单段请求上限 12+12=24 → 5280）。
#: 宿主链路支持显式 override 且优先于任务/模型配置（SDK ctx.llm.generate →
#: 宿主 llm.generate capability → _resolve_effective_max_tokens）。
EVENT_LLM_MAX_TOKENS_FLOOR = 2000
EVENT_LLM_TOKENS_PER_EVENT = 220
EVENT_LLM_MAX_TOKENS_CAP = 6000


def event_generation_max_tokens(requested: int) -> int:
    """事件生成输出上限（有界）：min(6000, max(2000, requested*220))。"""
    return min(EVENT_LLM_MAX_TOKENS_CAP,
               max(EVENT_LLM_MAX_TOKENS_FLOOR,
                   max(0, int(requested)) * EVENT_LLM_TOKENS_PER_EVENT))


def _response_truncated(result: Any, max_tokens_sent: Optional[int]) -> bool:
    """截断判定：finish_reason=length 明示优先；宿主不回传 finish_reason 时
    以 completion_tokens 触顶（>= 本次发送的 max_tokens）启发式兜底。"""
    if str(getattr(result, "finish_reason", "") or "").strip().lower() == "length":
        return True
    completion = getattr(result, "completion_tokens", None)
    return bool(max_tokens_sent and completion is not None
                and int(completion) >= int(max_tokens_sent))


def _callable_accepts(fn: Any, name: str) -> bool:
    """旧测试桩兼容：签名含 name 或 **kwargs 才传新参数（无法内省按支持处理）。"""
    import inspect
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(p.kind == inspect.Parameter.VAR_KEYWORD
                                 for p in params.values())
#: 备份保留数（.bak/.bak.2/.bak.3）
BACKUP_KEEP = 3
#: 旧展示用粗略换算（仅 estimate_tokens；预算已改用字节上界 + 真实 usage）
_CHARS_PER_TOKEN = 2
#: 内置池权重固定 1.0（附录 B.1.3）
BUILTIN_WEIGHT = 1.0
#: 刷视频小事件 key（2026-11-04 刷视频功能批）：事件本体已在内置池（B 计划
#: 2026-11-01），本批补「启用门控」——仅当 [video_watch].enabled=true 时可被
#: roll 到（装配根经 set_event_gate 注入动态配置闸；热重载即时生效）。
VIDEO_WATCH_EVENT_KEY = "watch_bili"

from .small_events import BUILTIN_EVENTS, builtin_events

#: 安全关键词（模板 / 名称；宁可保守，命中即 E_SAFETY）
_SAFETY_TERMS: Tuple[str, ...] = (
    "打架", "殴打", "杀死", "自杀", "自残", "割腕", "跳楼", "轻生", "枪", "尸体",
    "绑架", "虐待", "下毒", "爆炸", "恐怖袭击",
    "色情", "性爱", "裸体", "约炮", "强奸", "做爱", "嫖娼",
    "毒品", "吸毒", "大麻", "冰毒", "赌博", "赌场", "抢劫", "偷窃", "诈骗",
    "贿赂", "走私", "洗钱", "伪造",
    "医院", "看病", "吃药", "药物", "手术", "处方", "诊断", "治疗",
    "股票", "基金", "投资", "理财", "贷款", "借钱", "信用卡", "比特币", "炒股",
    "律师", "起诉", "官司", "法院", "合同纠纷",
    "真实地址", "门牌号",
)
_URL_RE = re.compile(r"(https?://|www\.|\b[\w.-]+\.(?:com|cn|net|org|io)\b)", re.IGNORECASE)
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_CONTACT_RE = re.compile(r"(qq|微信|电话|手机|联系方式)\s*[:：]?\s*\d{5,}", re.IGNORECASE)
_ADDRESS_RE = re.compile(r"[\u4e00-\u9fa5]{1,8}(路|街|道|小区)\s*\d+\s*号")


def compute_catalog_version(document: Any) -> str:
    """稳定目录版本：内置池 / 生成库内容 + generated_at 的短哈希。"""
    if not isinstance(document, dict):
        return "builtin_v1"
    events = document.get("events")
    if not isinstance(events, list) or not events:
        return "builtin_v1"
    payload = json.dumps({"version": document.get("version"),
                          "generated_at": document.get("generated_at"),
                          "events": events}, ensure_ascii=False, sort_keys=True)
    return "cat_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


# ── 校验 ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ValidationError:
    code: str
    message: str
    field: str = ""
    event_id: str = ""

    def __str__(self) -> str:
        prefix = f"{self.code}"
        if self.event_id:
            prefix += f"[{self.event_id}]"
        if self.field:
            prefix += f".{self.field}"
        return f"{prefix}: {self.message}"


@dataclass(frozen=True)
class ValidationResult:
    errors: Tuple[ValidationError, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> set:
        return {e.code for e in self.errors}

    @property
    def lines(self) -> List[str]:
        return [str(e) for e in self.errors]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _danger_hit(text: str) -> bool:
    """危险/违法/性/自伤/医疗/金融/法律等内容（E_SAFETY）。"""
    return any(term in text for term in _SAFETY_TERMS)


def _contact_hit(text: str) -> bool:
    """真实 URL / 电话 / 邮箱 / QQ 微信 / 地址（E_NAME / E_TEMPLATE）。"""
    lowered = text.lower()
    return bool(_URL_RE.search(lowered) or _PHONE_RE.search(text)
                or _EMAIL_RE.search(lowered) or _CONTACT_RE.search(text)
                or _ADDRESS_RE.search(text))


def _safety_hit(text: str) -> bool:
    """兼容旧调用：任一危险或联系方式命中。"""
    return _danger_hit(text) or _contact_hit(text)


class EventCatalogValidator:
    """附录 B.1.4 全量校验。命中任一错误 → 文档不得替换旧库。"""

    def __init__(self, *, builtin_count: Optional[int] = None,
                 max_generated_events: int = MAX_GENERATED_EVENTS,
                 max_pool_events: Optional[int] = None,
                 enforce_pool_cap: bool = True,
                 period_limits: Optional[Sequence[int]] = None,
                 reusable_limits: Optional[Sequence[int]] = None,
                 one_shot_limits: Optional[Sequence[int]] = None) -> None:
        """``max_pool_events`` 已退役（3.17.6 PR-REMOVE-POOLCAP）：缺省 None =
        派生上限（内置数 + 六键配额和）；旧调用方显式传值时按 deprecated 覆盖
        尊重（仅兼容旧测试/旧装配，生产装配不再传）。"""
        self.builtin_count = len(BUILTIN_EVENTS) if builtin_count is None else int(builtin_count)
        self.max_generated_events = max(1,
                                        min(MAX_GENERATED_EVENTS, int(max_generated_events)))
        self.enforce_pool_cap = bool(enforce_pool_cap)
        from .event_slots import TIME_SLOTS
        from .event_catalog_config import (DEFAULT_REUSABLE_00_08, DEFAULT_REUSABLE_08_16,
                                           DEFAULT_REUSABLE_16_24, DEFAULT_ONE_SHOT_00_08,
                                           DEFAULT_ONE_SHOT_08_16, DEFAULT_ONE_SHOT_16_24)
        reusable_defaults = (DEFAULT_REUSABLE_00_08, DEFAULT_REUSABLE_08_16, DEFAULT_REUSABLE_16_24)
        one_shot_defaults = (DEFAULT_ONE_SHOT_00_08, DEFAULT_ONE_SHOT_08_16, DEFAULT_ONE_SHOT_16_24)

        def _limits(raw: Optional[Sequence[int]], defaults: tuple) -> tuple:
            values = tuple(raw) if raw is not None else defaults
            return tuple(max(0, int(values[i] if i < len(values) else defaults[i]))
                         for i in range(len(TIME_SLOTS)))

        self.reusable_limits = _limits(reusable_limits, reusable_defaults)
        self.one_shot_limits = _limits(one_shot_limits, one_shot_defaults)
        # 每时段上限 = 可复用 + 一次性合计；兼容旧调用直接传合计 period_limits
        if period_limits is not None:
            totals = _limits(period_limits, tuple(r + o for r, o in
                                                  zip(reusable_defaults, one_shot_defaults)))
        else:
            totals = tuple(r + o for r, o in zip(self.reusable_limits, self.one_shot_limits))
        self.period_limits = {slot: totals[i] for i, slot in enumerate(TIME_SLOTS)}
        # 3.17.6（PR-REMOVE-POOLCAP）：池上限默认派生 = 内置 + 六键配额和，
        # 永远与配额自洽（生成项数==配额和时不触发总量拒绝）。显式传值 =
        # deprecated 覆盖（旧测试/旧装配兼容），生产路径不再传。
        if max_pool_events is None:
            self.max_pool_events = derive_pool_cap(
                self.builtin_count, self.reusable_limits, self.one_shot_limits)
        else:
            self.max_pool_events = max(1, int(max_pool_events))

    # ---- 单事件 ----
    def validate_event(self, event: Any, *, index: Optional[int] = None,
                       reserved_ids: Iterable[str] = (),
                       reserved_names: Iterable[str] = (),
                       recent_names: Iterable[str] = ()) -> List[ValidationError]:
        errors: List[ValidationError] = []
        eid = ""
        if not isinstance(event, dict):
            return [ValidationError("E_SCHEMA", "事件必须是对象", f"events[{index}]")]
        eid = _text(event.get("event_id"))

        def add(code: str, message: str, field_name: str = "") -> None:
            errors.append(ValidationError(code, message, field_name, eid))

        # event_id：llm_ 前缀 + slug + 唯一
        if not EVENT_ID_RE.match(eid):
            add("E_ID", "event_id 必须以 llm_ 开头，且只含小写字母/数字/下划线（2-48 字符）",
                "event_id")
        if eid and (eid in set(reserved_ids)):
            add("E_ID", "event_id 与内置池/保留 id 冲突", "event_id")

        # name：2-10 字，无换行 / URL / 联系方式 / 危险词
        name = _text(event.get("name"))
        if len(name) < MIN_NAME_CHARS or len(name) > MAX_NAME_CHARS:
            add("E_NAME", f"name 长度需 {MIN_NAME_CHARS}-{MAX_NAME_CHARS} 字", "name")
        if "\n" in str(event.get("name") or "") or "\r" in str(event.get("name") or ""):
            add("E_NAME", "name 不得含换行", "name")
        if name and _contact_hit(name):
            add("E_NAME", "name 含 URL/联系方式/真实地址", "name")
        if name and _danger_hit(name):
            add("E_SAFETY", "name 含危险/违法/性/自伤/医疗/金融/法律内容", "name")

        # goal_type
        goal_type = _text(event.get("goal_type"))
        if goal_type in FORBIDDEN_GOAL_TYPES:
            add("E_GOAL_TYPE", f"goal_type 禁止 {goal_type}", "goal_type")
        elif goal_type not in ALLOWED_GOAL_TYPES:
            add("E_GOAL_TYPE", f"goal_type 不在允许集合：{goal_type}", "goal_type")

        # 时长
        min_minutes = event.get("min_minutes")
        max_minutes = event.get("max_minutes")
        if not _is_int(min_minutes) or not _is_int(max_minutes):
            add("E_DURATION", "min_minutes/max_minutes 必须是整数", "min_minutes")
        else:
            if not (MIN_DURATION_MINUTES <= min_minutes <= MAX_DURATION_MINUTES):
                add("E_DURATION", "min_minutes 需在 5-30", "min_minutes")
            if not (MIN_DURATION_MINUTES <= max_minutes <= MAX_DURATION_MINUTES):
                add("E_DURATION", "max_minutes 需在 5-30", "max_minutes")
            if max_minutes < min_minutes:
                add("E_DURATION", "max_minutes 必须 >= min_minutes", "max_minutes")

        # 可中断性
        interruptibility = event.get("interruptibility")
        if not _is_number(interruptibility) or not (0.0 <= float(interruptibility) <= 1.0):
            add("E_INTERRUPT", "interruptibility 必须是 0-1 数值", "interruptibility")

        # 权重
        weight = event.get("weight")
        if not _is_number(weight) or not (MIN_WEIGHT <= float(weight) <= MAX_WEIGHT):
            add("E_WEIGHT", "weight 必须是 0.1-1.0 数值", "weight")

        # state_deltas
        deltas = event.get("state_deltas")
        if not isinstance(deltas, dict):
            add("E_DELTA", "state_deltas 必须是对象", "state_deltas")
        else:
            for key, value in deltas.items():
                if key not in STATE_FIELDS:
                    add("E_DELTA", f"state_deltas 键 {key!r} 不在五项内（禁止 sleep_debt）",
                        "state_deltas")
                    continue
                if not _is_number(value) or abs(float(value)) > MAX_DELTA_ABS + 1e-12:
                    add("E_DELTA", f"state_deltas.{key} 单次绝对值需 <=0.08", "state_deltas")

        # templates
        templates = event.get("templates")
        if not isinstance(templates, list) or not (2 <= len(templates) <= 3):
            add("E_TEMPLATE", "templates 必须是 2-3 条数组", "templates")
        else:
            seen: set = set()
            for pos, item in enumerate(templates):
                text = _text(item)
                if not isinstance(item, str) or not (MIN_TEMPLATE_CHARS <= len(text) <= MAX_TEMPLATE_CHARS):
                    add("E_TEMPLATE", f"templates[{pos}] 需为 {MIN_TEMPLATE_CHARS}-{MAX_TEMPLATE_CHARS} 字",
                        "templates")
                if "\n" in str(item or "") or "\r" in str(item or ""):
                    add("E_TEMPLATE", f"templates[{pos}] 不得含换行", "templates")
                if text and _contact_hit(text):
                    add("E_TEMPLATE", f"templates[{pos}] 含 URL/联系方式/真实地址", "templates")
                if text and _danger_hit(text):
                    add("E_SAFETY", f"templates[{pos}] 含危险/违法/性/自伤/医疗/金融/法律内容",
                        "templates")
                if text in seen:
                    add("E_TEMPLATE", f"templates[{pos}] 重复", "templates")
                seen.add(text)

        # enabled（可选）
        if "enabled" in event and not isinstance(event.get("enabled"), bool):
            add("E_SCHEMA", "enabled 必须是布尔", "enabled")

        # 重复（与内置池 / 现有生成池 / 近 30 天）
        if name:
            reserved = {str(n).strip() for n in reserved_names if str(n or "").strip()}
            recent = {str(n).strip() for n in recent_names if str(n or "").strip()}
            if name in reserved:
                add("E_DUPLICATE", "name 与内置池/现有事件重名", "name")
            elif name in recent:
                add("E_DUPLICATE", "name 与近 30 天事件重名", "name")
        return errors

    # ---- 整份文档 ----
    def validate_document(self, document: Any, *, reserved_ids: Iterable[str] = (),
                          reserved_names: Iterable[str] = (),
                          recent_names: Iterable[str] = (),
                          now: Optional[datetime] = None) -> ValidationResult:
        errors: List[ValidationError] = []
        if not isinstance(document, dict):
            return ValidationResult((ValidationError("E_SCHEMA", "文档根必须是对象"),))
        version = document.get("version")
        if version not in (1, CATALOG_VERSION):
            errors.append(ValidationError("E_SCHEMA", f"version 必须为 {CATALOG_VERSION}",
                                          "version"))
        for key in ("source", "model", "generated_at", "next_update_at", "prompt_hash"):
            if key in document and not isinstance(document.get(key), str):
                errors.append(ValidationError("E_SCHEMA", f"{key} 必须是字符串", key))
        for key in ("generated_at", "next_update_at"):
            raw = document.get(key)
            if isinstance(raw, str) and raw.strip():
                try:
                    datetime.fromisoformat(raw.strip())
                except ValueError:
                    errors.append(ValidationError("E_SCHEMA", f"{key} 必须是 ISO 时间", key))
        events = document.get("events")
        if not isinstance(events, list):
            errors.append(ValidationError("E_SCHEMA", "events 必须是数组", "events"))
            return ValidationResult(tuple(errors))
        dual = version == CATALOG_VERSION
        if dual:
            try:
                normalize_cycle(document)
            except ValueError as exc:
                errors.append(ValidationError('E_SCHEMA', str(exc), 'consumed'))
            for kind, pool_cap in (('reusable', sum(self.reusable_limits)),
                                   ('one_shot', sum(self.one_shot_limits))):
                count = sum(isinstance(e, dict) and e.get('pool_kind') == kind for e in events)
                if count > pool_cap:
                    errors.append(ValidationError('E_COUNT', f'{kind} 条数超过 {pool_cap}', 'events'))
            from .event_slots import TIME_SLOTS
            slot_counts = {slot: 0 for slot in TIME_SLOTS}
            for e in events:
                if not isinstance(e, dict):
                    continue
                ts = e.get('time_slot')
                if ts not in TIME_SLOTS:
                    errors.append(ValidationError('E_SLOT', '生成事件必须标记有效 time_slot（00-08/08-16/16-24）',
                                                  'time_slot', str(e.get('event_id') or '')))
                else:
                    slot_counts[ts] += 1
            for slot in TIME_SLOTS:
                if slot_counts[slot] > self.period_limits[slot]:
                    errors.append(ValidationError('E_COUNT',
                                                  f'{slot} 时段 {slot_counts[slot]} 条超过上限 {self.period_limits[slot]}',
                                                  'events'))
        elif not (MIN_GENERATED_EVENTS <= len(events) <= self.max_generated_events):
            errors.append(ValidationError(
                "E_COUNT", f"生成条数需 {MIN_GENERATED_EVENTS}-{self.max_generated_events}，"
                f"实际 {len(events)}", "events"))
        if (dual or self.enforce_pool_cap) and self.builtin_count + len(events) > self.max_pool_events:
            errors.append(ValidationError(
                "E_COUNT", f"内置池 {self.builtin_count} + 生成 {len(events)} 超过总池上限 "
                f"{self.max_pool_events}", "events"))
        seen_ids: set = set()
        seen_names: set = set()
        reserved_id_set = {str(x) for x in reserved_ids}
        for index, event in enumerate(events):
            item_errors = self.validate_event(event, index=index,
                                              reserved_ids=reserved_id_set,
                                              reserved_names=reserved_names,
                                              recent_names=recent_names)
            errors.extend(item_errors)
            if (isinstance(event, dict) and event.get('pool_kind') == 'one_shot'
                    and event.get('goal_type') in ('rest', 'idle', 'transition')):
                errors.append(ValidationError('E_GOAL_TYPE', '一次性事件应有具体小目标，不是反复休息或转场',
                                              'goal_type', str(event.get('event_id') or '')))
            if isinstance(event, dict):
                eid = _text(event.get("event_id"))
                name = _text(event.get("name"))
                if eid:
                    if eid in seen_ids:
                        errors.append(ValidationError("E_ID", "event_id 文档内重复",
                                                      "event_id", eid))
                    seen_ids.add(eid)
                if name:
                    if name in seen_names:
                        errors.append(ValidationError("E_DUPLICATE", "name 文档内重复",
                                                      "name", eid))
                    seen_names.add(name)
        # 排序去重，保证手动逐条输出稳定
        unique: List[ValidationError] = []
        seen_keys: set = set()
        for error in errors:
            key = (error.code, error.field, error.event_id, error.message)
            if key not in seen_keys:
                seen_keys.add(key)
                unique.append(error)
        return ValidationResult(tuple(unique))


@dataclass(frozen=True)
class CatalogSnapshot:
    """一次装载结果（只读；服务与 planner 都从这里读）。"""

    path: Path
    document: Dict[str, Any] = field(default_factory=dict)
    generated_events: Tuple[Dict[str, Any], ...] = ()
    builtin_events: Tuple[Dict[str, Any], ...] = ()
    active_events: Tuple[Dict[str, Any], ...] = ()
    version: int = CATALOG_VERSION
    generated_at: Optional[str] = None
    next_update_at: Optional[str] = None
    catalog_version: str = "builtin_v1"
    degraded: bool = False
    reason: str = ""
    invalid_count: int = 0
    pool_truncated: int = 0

    @property
    def generation_id(self) -> str:
        return str(self.document.get('generation_id') or '')

    @property
    def reusable_events(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(dict(e) for e in self.generated_events if e.get('pool_kind') == 'reusable')

    @property
    def one_shot_events(self) -> Tuple[Dict[str, Any], ...]:
        return tuple(dict(e) for e in self.active_events if e.get('pool_kind') == 'one_shot')

    @property
    def consumed_events(self) -> Tuple[Dict[str, Any], ...]:
        receipts = self.document.get('consumed') or {}
        return tuple(dict(e, consumption=dict(receipts[e['event_id']]))
                     for e in self.generated_events if e.get('event_id') in receipts)

    def get_event(self, event_id: Any) -> Optional[Dict[str, Any]]:
        wanted = str(event_id or "")
        for event in (*self.builtin_events, *self.generated_events):
            if event.get("event_id") == wanted or event.get("key") == wanted:
                return dict(event)
        return None

    def state_deltas(self, event_id: Any) -> Optional[Dict[str, float]]:
        event = self.get_event(event_id)
        if event is None:
            return None
        deltas = event.get("state_deltas")
        return dict(deltas) if isinstance(deltas, dict) else None


def _now_of(value: Any, tz: Optional[timezone]) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=tz or timezone.utc)
    return datetime.now(tz or timezone.utc)


def _parse_iso(value: Any, tz: Optional[timezone]) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=tz or timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=tz or timezone.utc)


def resolve_state_deltas(metadata: Any,
                         *, builtin: Optional[Sequence[Mapping[str, Any]]] = None
                         ) -> Optional[Dict[str, float]]:
    """episode metadata → 一次性 state_deltas（快照优先，旧行回退内置池）。

    共享接线时 ``RollingPlanner._random_event_deltas`` 应改为先调用本函数：

    - ``metadata["state_deltas"]`` 存在（新 episode 创建时快照）→ 直接用，
      生成库后续轮换不影响进行中活动；
    - 否则按旧 ``metadata["event_key"]`` 回查内置池（旧 episode 兼容）；
    - ``state_deltas_applied`` 为真 → 返回 None（防双应用）。

    返回 ``{}`` 表示快照存在但无增量（仍应写 applied 标记）；None 表示非随机
    事件 / 已应用 / 旧 key 查不到。
    """
    if not isinstance(metadata, Mapping) or metadata.get("state_deltas_applied"):
        return None
    snapshot = metadata.get("state_deltas")
    if isinstance(snapshot, dict):
        return {key: float(value) for key, value in snapshot.items()
                if key in STATE_FIELDS and _is_number(value)}
    event_key = str(metadata.get("event_key") or metadata.get("event_id") or "")
    if not event_key:
        return None
    for event in (builtin if builtin is not None else BUILTIN_EVENTS):
        eid = str(event.get("event_id") or event.get("key") or "")
        if eid == event_key:
            deltas = event.get("state_deltas")
            return {key: float(value) for key, value in deltas.items()
                    if key in STATE_FIELDS and _is_number(value)} \
                if isinstance(deltas, dict) else None
    return None


def state_affinity(event: Mapping[str, Any], state: Any) -> float:
    """状态 → 事件权重乘子（纯函数；无状态/无法解析 = 1.0）。

    只做温和偏好，不淘汰事件：低精力偏休息/日常、避开高消耗；高压力偏休息；
    低专注偏日常/转场。
    """
    if not isinstance(state, Mapping):
        return 1.0
    factor = 1.0
    goal_type = str(event.get("goal_type") or "")
    low_energy_types = {"rest", "idle", "daily_routine"}
    high_energy_types = {"exercise", "social", "work", "study"}
    energy = state.get("energy")
    if _is_number(energy):
        if float(energy) < 0.30:
            factor *= 1.5 if goal_type in low_energy_types else (
                0.5 if goal_type in high_energy_types else 1.0)
        elif float(energy) >= 0.85 and goal_type == "exercise":
            factor *= 1.2
    stress = state.get("stress")
    if _is_number(stress) and float(stress) > 0.80:
        if goal_type in ("rest", "idle"):
            factor *= 1.3
        elif goal_type == "social":
            factor *= 0.6
    focus = state.get("focus")
    if _is_number(focus) and float(focus) < 0.30:
        if goal_type in ("daily_routine", "idle"):
            factor *= 1.2
        elif goal_type in ("work", "study"):
            factor *= 0.6
    return max(0.05, factor)


class EventCatalogStore:
    """``generated_events.json`` 读写 + 装载回退 + 纯本地 pick。

    构造即装载一次（只读）；``save`` 才写盘。任何异常都收敛成 ``degraded``
    快照（内置池），绝不抛出到插件装配/ tick。
    """

    def __init__(self, path: Any, *, builtin: Optional[Sequence[Mapping[str, Any]]] = None,
                 validator: Optional[EventCatalogValidator] = None,
                 max_generated_events: int = MAX_GENERATED_EVENTS,
                 max_pool_events: Optional[int] = None,
                 enforce_pool_cap: bool = True,
                 tz: Optional[timezone] = None,
                 load_on_init: bool = True) -> None:
        """``max_pool_events`` 已退役（3.17.6）：None = 装载时按 validator 配额
        派生上限；旧调用方显式传值按 deprecated 覆盖尊重（兼容旧测试）。"""
        self.path = Path(path)
        self._write_lock = threading.RLock()
        self._tz = tz
        self._pool_cap_override = None if max_pool_events is None else max(1, int(max_pool_events))
        self._enforce_pool_cap = bool(enforce_pool_cap)
        self._builtin = tuple(self._normalize_builtin(builtin) if builtin is not None
                              else builtin_events())
        self._validator = validator or EventCatalogValidator(
            builtin_count=len(self._builtin),
            max_generated_events=max_generated_events,
            max_pool_events=max_pool_events,
            enforce_pool_cap=enforce_pool_cap)
        self._snapshot = self._fallback("missing")
        # 运行时事件门控（2026-11-04 刷视频功能批）：event_key -> bool；
        # None = 不设闸（旧行为）。闸是动态配置读数，不进快照/不改用户文件。
        self._event_gate: Optional[Callable[[str], bool]] = None
        # mode=off 时**不读**生成库（2026-10-27 共享接线契约：off 不读写生成库）：
        # 只保留内置池快照，用户文件既不删除也不改写；切到 manual/auto 时由 reload() 补读。
        if load_on_init:
            self.reload()

    def _pool_cap(self) -> int:
        """运行期池上限：deprecated 显式覆盖优先，否则派生（内置 + 六键配额和）。"""
        if self._pool_cap_override is not None:
            return self._pool_cap_override
        return derive_pool_cap(len(self._builtin),
                               self._validator.reusable_limits,
                               self._validator.one_shot_limits)

    def set_event_gate(self, gate: Optional[Callable[[str], bool]]) -> None:
        """注入运行时事件门控（装配根用；pick 时逐事件询问，False = 不可 roll）。

        闸异常按「该事件不可用」处理（fail-closed）；当前唯一消费场景 =
        watch_bili 随 [video_watch].enabled 启停（用户规格：启用看视频功能时
        自动把「刷B站」加入可 roll 的小事件池，关闭即移出）。
        """
        self._event_gate = gate

    def _gate_allows(self, event_id: str) -> bool:
        gate = self._event_gate
        if gate is None:
            return True
        try:
            return bool(gate(event_id))
        except Exception:
            logger.debug("event_catalog: 事件门控异常（按不可用处理）：%s", event_id)
            return False

    def use_builtin_only(self, reason: str = "mode_off") -> CatalogSnapshot:
        """不读盘、只用内置池（``mode=off`` 语义；不删不改用户文件）。"""
        self._snapshot = self._fallback(reason)
        return self._snapshot


    @staticmethod
    def _normalize_builtin(items: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
        from .event_slots import TIME_SLOTS
        output: List[Dict[str, Any]] = []
        for raw in items:
            eid = str(raw.get("event_id") or raw.get("key") or "")
            output.append({
                "event_id": eid,
                "key": eid,
                "name": str(raw.get("name") or ""),
                "goal_type": str(raw.get("goal_type") or "daily_routine"),
                "min_minutes": int(raw.get("min_minutes") or 5),
                "max_minutes": int(raw.get("max_minutes") or 5),
                "interruptibility": float(raw.get("interruptibility") or 0.0),
                "weight": float(raw.get("weight") or BUILTIN_WEIGHT),
                "state_deltas": dict(raw.get("state_deltas") or {}),
                "templates": [str(x) for x in (raw.get("templates") or ())],
                "enabled": True,
                "source": "builtin",
                "pool_kind": "builtin",
                "allowed_slots": list(raw.get("allowed_slots") or TIME_SLOTS),
            })
        return output

    # ---- 装载 ----
    def _fallback(self, reason: str, *, document: Optional[Dict[str, Any]] = None,
                  invalid_count: int = 0, next_update_at: Optional[str] = None) -> CatalogSnapshot:
        active = tuple(dict(e) for e in self._builtin)
        return CatalogSnapshot(
            path=self.path,
            document=dict(document or {}),
            generated_events=(),
            builtin_events=tuple(dict(e) for e in self._builtin),
            active_events=active,
            version=CATALOG_VERSION,
            generated_at=(document or {}).get("generated_at"),
            next_update_at=next_update_at,
            catalog_version="builtin_v1",
            degraded=True,
            reason=reason,
            invalid_count=invalid_count,
        )

    def reload(self) -> CatalogSnapshot:
        """重新读盘；坏文件/坏项只降级，不删除不重写用户文件。"""
        try:
            if not self.path.exists():
                self._snapshot = self._fallback("missing")
                return self._snapshot
            try:
                document = read_json_text(self.path)
            except MappingReadError as exc:
                logger.warning("event_catalog: 事件库读取/解析失败，回退内置池：%s", exc.detail)
                self._snapshot = self._fallback("corrupt")
                return self._snapshot
            if not isinstance(document, dict):
                self._snapshot = self._fallback("corrupt")
                return self._snapshot
            version = document.get("version")
            if version not in (1, CATALOG_VERSION):
                logger.warning("event_catalog: 不认识的版本 %r，回退内置池", version)
                self._snapshot = self._fallback("unsupported_version", document=document)
                return self._snapshot
            try:
                document = normalize_cycle(document)
            except ValueError as exc:
                logger.warning('event_catalog: 消耗状态不可核验，仅使用内置池：%s', exc)
                self._snapshot = self._fallback('invalid_consumption_state')
                return self._snapshot
            raw_events = document.get("events")
            if not isinstance(raw_events, list):
                self._snapshot = self._fallback("corrupt", document=document)
                return self._snapshot

            reserved_ids = {e["event_id"] for e in self._builtin}
            reserved_names = {e["name"] for e in self._builtin}
            valid: List[Dict[str, Any]] = []
            invalid_count = 0
            for raw in raw_events:
                item_errors = self._validator.validate_event(
                    raw, reserved_ids=reserved_ids, reserved_names=reserved_names)
                if item_errors:
                    invalid_count += 1
                    logger.warning("event_catalog: 忽略非法生成项 %s", item_errors[0])
                    continue
                if not isinstance(raw, dict) or raw.get("enabled", True) is False:
                    continue
                eid = str(raw.get("event_id"))
                normalized = {
                    "event_id": eid,
                    "key": eid,
                    "name": str(raw.get("name")).strip(),
                    "goal_type": str(raw.get("goal_type")).strip(),
                    "min_minutes": int(raw.get("min_minutes")),
                    "max_minutes": int(raw.get("max_minutes")),
                    "interruptibility": float(raw.get("interruptibility")),
                    "weight": float(raw.get("weight")),
                    "state_deltas": {k: float(v) for k, v in dict(raw.get("state_deltas") or {}).items()},
                    "templates": [str(x) for x in raw.get("templates") or ()],
                    "enabled": True,
                    "source": "llm",
                    "pool_kind": raw['pool_kind'],
                    "generation_id": document['generation_id'],
                    # PR-FIX-SLOT（3.17.4）：装载必须携带时段标签，否则
                    # event_allowed_in_slot 把生成项当无标签旧项全时段排除。
                    # v3 validator 已校验必填+合法；raw.get 与校验口径一致，
                    # v1 旧无标签项取 None 仍按既有语义排除（不改过滤语义）。
                    "time_slot": raw.get("time_slot"),
                }
                reserved_ids.add(eid)
                reserved_names.add(normalized["name"])
                valid.append(normalized)

            pool_truncated = 0
            if self._enforce_pool_cap:
                # 3.17.6（PR-REMOVE-POOLCAP）：上限派生 = 内置 + validator 六键
                # 配额和（装载时求值，随配额自洽）；合法生成项（==配额和）不再
                # 被独立总上限截断。显式 deprecated 覆盖仅旧测试/旧装配使用。
                pool_cap = self._pool_cap()
                slots = max(0, pool_cap - len(self._builtin))
                if len(valid) > slots:
                    pool_truncated = len(valid) - slots
                    logger.warning("event_catalog: 生成项 %d 条 + 内置 %d 条超过总池上限 %d，"
                                   "运行期保留前 %d 条生成项（文件不重写）",
                                   len(valid), len(self._builtin), pool_cap, slots)
                    valid = valid[:slots]

            next_update_at = document.get("next_update_at")
            if not isinstance(next_update_at, str) or not next_update_at.strip():
                next_update_at = None
            degraded = bool(invalid_count) or not valid
            consumed = document['consumed']
            active = tuple(dict(e, pool_kind='builtin') for e in self._builtin) + tuple(
                dict(e) for e in valid if e['event_id'] not in consumed)
            self._snapshot = CatalogSnapshot(
                path=self.path,
                document=document,
                generated_events=tuple(dict(e) for e in valid),
                builtin_events=tuple(dict(e) for e in self._builtin),
                active_events=active,
                version=CATALOG_VERSION,
                generated_at=document.get("generated_at"),
                next_update_at=next_update_at if valid else None,
                catalog_version=compute_catalog_version(document) if valid else "builtin_v1",
                degraded=degraded,
                reason=("invalid_items" if valid else "no_valid_events") if degraded else "",
                invalid_count=invalid_count,
                pool_truncated=pool_truncated,
            )
            return self._snapshot
        except Exception as exc:  # 装载绝不影响 tick / 插件启动
            logger.warning("event_catalog: 装载异常，回退内置池：%s", exc)
            self._snapshot = self._fallback("error")
            return self._snapshot

    # ---- 只读视图 ----
    @property
    def snapshot(self) -> CatalogSnapshot:
        return self._snapshot

    @property
    def active_events(self) -> List[Dict[str, Any]]:
        return [dict(e) for e in self._snapshot.active_events]

    @property
    def generated_events(self) -> List[Dict[str, Any]]:
        return [dict(e) for e in self._snapshot.generated_events]

    @property
    def catalog_version(self) -> str:
        return self._snapshot.catalog_version

    @property
    def next_update_at(self) -> Optional[str]:
        return self._snapshot.next_update_at

    def get_event(self, event_id: Any) -> Optional[Dict[str, Any]]:
        return self._snapshot.get_event(event_id)

    def get_state_deltas(self, event_id: Any) -> Optional[Dict[str, float]]:
        return self._snapshot.state_deltas(event_id)

    def file_fingerprint(self) -> str:
        """当前文件内容指纹（生成期间外部改动检测）。"""
        try:
            if not self.path.exists():
                return ""
            return hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError:
            return ""

    # ---- 原子写 + .bak ----
    def _rotate_backup(self) -> bool:
        if not self.path.exists():
            return True
        bak = Path(str(self.path) + ".bak")
        try:
            for index in range(BACKUP_KEEP - 1, 0, -1):
                src = Path(f"{bak}.{index}")
                dst = Path(f"{bak}.{index + 1}")
                if src.exists():
                    os.replace(src, dst)
            if bak.exists():
                os.replace(bak, Path(f"{bak}.2"))
            shutil.copy2(self.path, bak)
            return True
        except OSError as exc:
            logger.warning("event_catalog: 备份旧事件库失败，放弃本次替换：%s", exc)
            return False

    def save(self, document: Mapping[str, Any], *, expected_fingerprint: Optional[str] = None) -> bool:
        """原子替换；先备份旧文件。失败 → 旧文件保持原样。"""
        try:
            normalized = normalize_cycle(dict(document))
            with self._write_lock, catalog_write_guard(self.path):
                if expected_fingerprint is not None and self.file_fingerprint() != expected_fingerprint:
                    return False
                if self.path.exists() and not self._rotate_backup():
                    return False
                if not save_json_atomic(self.path, normalized):
                    logger.warning("event_catalog: 原子写事件库失败（旧文件保留）")
                    return False
                self.reload()
                return True
        except Exception as exc:
            logger.warning("event_catalog: 保存事件库异常（旧文件保留）：%s", exc)
            return False

    def claim_event(self, event: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
        """Final draw consumes one-shot events before episode creation; never on preview."""
        if event.get('pool_kind') != 'one_shot':
            return True
        try:
            with self._write_lock, catalog_write_guard(self.path):
                document = normalize_cycle(read_json_text(self.path))
                eid = str(event.get('event_id') or event.get('key') or '')
                if event.get('generation_id') != document['generation_id'] or eid in document['consumed']:
                    return False
                actual = next((e for e in document['events'] if isinstance(e, dict)
                               and e.get('event_id') == eid and e.get('pool_kind') == 'one_shot'), None)
                if actual is None or not self._gate_allows(eid):
                    return False
                document['consumed'][eid] = {'generation_id': document['generation_id'],
                    'status': 'selected', 'picked_at': _now_of(now, self._tz).isoformat(timespec='seconds')}
                if not save_json_atomic(self.path, document):
                    return False
                self.reload()
                return True
        except Exception:
            logger.warning('event_catalog: 一次性事件消耗无法持久化，本次不执行', exc_info=True)
            return False

    def finish_claim(self, event: Mapping[str, Any], status: str, episode_id: str = '',
                     *, reason: str = '') -> None:
        if event.get('pool_kind') != 'one_shot' or status not in ('started', 'start_failed', 'interrupted', 'completed'):
            return
        try:
            with self._write_lock, catalog_write_guard(self.path):
                document = normalize_cycle(read_json_text(self.path))
                if document['generation_id'] != event.get('generation_id'):
                    return  # next cycle must not receive an old activity's receipt
                receipt = document['consumed'].get(str(event.get('event_id') or event.get('key') or ''))
                if not isinstance(receipt, dict):
                    return
                if receipt.get('status') in ('completed', 'interrupted', 'start_failed'):
                    return  # a late callback cannot turn a terminal receipt back into started
                receipt.update(status=status, episode_id=str(episode_id)[:120])
                if reason:
                    receipt['reason'] = str(reason)[:120]
                if save_json_atomic(self.path, document):
                    self.reload()
        except Exception:
            logger.warning('event_catalog: 一次性事件结果未补记，原消耗记录仍保留', exc_info=True)

    # ---- 运行时 pick（0 LLM）----
    def pick_event(self, rng: random.Random, state: Any = None, recent: Any = None,
                   *, now: Optional[datetime] = None, context: Optional[str] = None,
                   events: Optional[Sequence[Mapping[str, Any]]] = None,
                   dedup_minutes: int = RANDOM_EVENT_DEDUP_MINUTES) -> Optional[Dict[str, Any]]:
        """按权重抽样一个事件（含 ``duration_minutes``）。

        ``context`` 只允许 ``None`` / ``idle`` / ``transition``（转场/空闲语义）；
        其它上下文直接返回 ``None``。``recent`` 支持 ``{event_id: datetime|ISO}``、
        ``[(event_id, ts)]`` 或带 ``event_id/timestamp`` 的 dict 列表。
        """
        if context not in (None, "idle", "transition"):
            return None
        now_dt = _now_of(now, self._tz)
        from .event_slots import time_slot_of, slot_remaining_minutes, event_allowed_in_slot
        slot = time_slot_of(now_dt, self._tz)
        remaining = slot_remaining_minutes(now_dt, self._tz)
        last_map = self._recent_map(recent, now_dt)
        pool = [dict(e) for e in (events if events is not None else self._snapshot.active_events)]
        cooled: List[Dict[str, Any]] = []
        for event in pool:
            eid = str(event.get("event_id") or event.get("key") or "")
            if not eid or event.get("enabled", True) is False:
                continue
            if event.get('pool_kind') == 'one_shot':
                if (event.get('generation_id') != self._snapshot.generation_id
                        or eid in (self._snapshot.document.get('consumed') or {})):
                    continue
            if not self._gate_allows(eid):
                continue  # 运行时门控（如 video_watch.enabled 关闭 → 刷B站不可 roll）
            if not event_allowed_in_slot(event, slot):
                continue  # 时段过滤（含旧无标签项排除）；即使外部传入 events 也过滤
            try:
                if int(event.get('min_minutes') or MIN_DURATION_MINUTES) > remaining:
                    continue  # 最小时长容纳不下当前时段剩余（如 07:59 抽 10 分钟活动）
            except (TypeError, ValueError):
                continue
            last = last_map.get(eid)
            if last is not None:
                elapsed = (now_dt - last).total_seconds()
                if elapsed < dedup_minutes * 60:
                    continue
            cooled.append(event)
        if not cooled:
            return None
        weights: List[float] = []
        for event in cooled:
            try:
                weight = float(event.get("weight") or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            weights.append(max(0.0, weight * state_affinity(event, state)))
        if sum(weights) <= 0:
            weights = [1.0 for _ in cooled]
        try:
            chosen = rng.choices(cooled, weights=weights, k=1)[0]
        except AttributeError:  # 极简 rng 桩（只有 choice）兜底
            chosen = rng.choice(cooled)
        output = dict(chosen)
        output["state_deltas"] = dict(output.get("state_deltas") or {})
        output["templates"] = list(output.get("templates") or ())
        min_minutes = int(output.get("min_minutes") or MIN_DURATION_MINUTES)
        max_minutes = int(output.get("max_minutes") or min_minutes)
        max_minutes = min(max_minutes, remaining)  # 时长上界不越过当前时段右边界
        if max_minutes < min_minutes:
            return None
        output["duration_minutes"] = rng.randint(min_minutes, max(max_minutes, min_minutes))
        output["time_slot"] = slot
        output["catalog_version"] = self.catalog_version
        if context:
            output["context"] = context
        return output

    def _recent_map(self, recent: Any, now: datetime) -> Dict[str, datetime]:
        output: Dict[str, datetime] = {}
        if not recent:
            return output
        items: Iterable[Any]
        if isinstance(recent, Mapping):
            items = recent.items()
        elif isinstance(recent, (list, tuple)):
            items = recent
        else:
            items = ()
        for item in items:
            eid: Any = None
            stamp: Any = None
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                eid, stamp = item[0], item[1]
            elif isinstance(item, Mapping):
                eid = item.get("event_id") or item.get("key")
                stamp = item.get("timestamp") or item.get("actual_end_at") \
                    or item.get("last_seen_at") or item.get("ts")
            elif isinstance(item, str):
                eid, stamp = item, now
            if not eid:
                continue
            parsed = _parse_iso(stamp, self._tz)
            if parsed is None and _is_number(stamp):
                try:
                    parsed = datetime.fromtimestamp(float(stamp), self._tz)
                except (OverflowError, OSError, ValueError):
                    parsed = None
            if parsed is None:
                parsed = now
            output[str(eid)] = parsed
        return output



@dataclass(frozen=True)
class RefreshResult:
    """刷新结果（manual 调用方可直接逐条 QQ；auto 只落日志）。"""

    ok: bool
    status: str
    manual: bool = False
    errors: Tuple[str, ...] = ()
    reason: str = ""
    catalog_version: str = ""
    next_update_at: Optional[str] = None
    events_count: int = 0
    tokens_used: int = 0
    called_llm: bool = False

    @property
    def error_lines(self) -> List[str]:
        return list(self.errors)


class EventCatalogService:
    """事件库服务（供 plugin 装配、planner 取池、/lbm_init events 调用）。

    构造参数（全部可选，便于离线测试与后续接线）：

    - ``catalog_path``：事件库路径；缺省用 ``plugin_root/mappings/generated_events.json``；
    - ``state_path``：自动检查/预算账本；缺省 ``data_dir/event_catalog_state.json``；
      没有 ``data_dir`` 时只在进程内保存（重启会丢「当日已尝试」）；
    - ``llm``：``core.llm_utils.LLMService``（唯一网络出口；本模块不另开请求）；
    - ``config`` / ``config_provider``：``[event_catalog]`` section / dict / SimpleNamespace；
    - ``auto_gate``：返回 bool 或 ``{"awake","in_sleep","hard_commitment"}`` 的闸；
    - ``persona_provider`` / ``recent_schedule_names_provider`` /
      ``recent_event_names_provider``：只提供人设文本与名称，**不得提供聊天正文**；
    - ``now_provider`` / ``rng``：离线测试可注入。
    """

    def __init__(self, *, catalog_path: Any = None, plugin_root: Any = None,
                 data_dir: Any = None, state_path: Any = None,
                 llm: Any = None, config: Any = None,
                 config_provider: Optional[Callable[[], Any]] = None,
                 validator: Optional[EventCatalogValidator] = None,
                 store: Optional[EventCatalogStore] = None,
                 tz: Any = None, now_provider: Optional[Callable[[], datetime]] = None,
                 rng: Optional[random.Random] = None,
                 persona_provider: Any = None,
                 recent_schedule_names_provider: Any = None,
                 recent_event_names_provider: Any = None,
                 auto_gate: Optional[Callable[[datetime], Any]] = None,
                 allow_ungated_auto: bool = False) -> None:
        self._tz = _tzinfo(tz) or datetime.now().astimezone().tzinfo
        self._config = config
        self._config_provider = config_provider
        self._llm = llm
        self._now_provider = now_provider
        self._rng = rng or random.Random()
        self._persona_provider = persona_provider
        self._recent_schedule_provider = recent_schedule_names_provider
        self._recent_event_provider = recent_event_names_provider
        self._auto_gate = auto_gate
        self._allow_ungated_auto = bool(allow_ungated_auto)
        cfg = self._current_config()
        if catalog_path is None:
            if plugin_root is not None:
                catalog_path = mappings_path(plugin_root, CATALOG_FILE_NAME)
            else:
                catalog_path = Path("mappings") / CATALOG_FILE_NAME
        if state_path is None and data_dir is not None:
            state_path = Path(data_dir) / CATALOG_STATE_FILE_NAME
        self._state_path = Path(state_path) if state_path is not None else None
        self._state = self._load_state()
        # 3.17.6（PR-REMOVE-POOLCAP）：不再传独立总上限——validator/store 缺省
        # 派生（内置 + 六键配额和），与三时段配额永远自洽。
        self._validator = validator or EventCatalogValidator(
            builtin_count=len(builtin_events()),
            max_generated_events=cfg.max_generated_events,
            reusable_limits=cfg.reusable_limits,
            one_shot_limits=cfg.one_shot_limits)
        self._custom_validator = validator is not None
        self.store = store or EventCatalogStore(
            catalog_path, validator=self._validator,
            max_generated_events=cfg.max_generated_events, tz=self._tz,
            # mode=off：构造期就不读生成库（off 只用内置池；不删不改用户文件）
            load_on_init=cfg.mode != "off")
        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._last_refresh: Optional[RefreshResult] = None

    # ---- 配置 / 时间 ----
    def _current_config(self) -> EventCatalogConfig:
        source = self._config
        if self._config_provider is not None:
            try:
                source = self._config_provider()
            except Exception:
                source = self._config
        return resolve_config(source)

    def _now(self, now: Optional[datetime] = None) -> datetime:
        if now is not None:
            return now if now.tzinfo is not None else now.replace(tzinfo=self._tz or timezone.utc)
        try:
            value = self._now_provider() if self._now_provider is not None else None
        except Exception:
            value = None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=self._tz or timezone.utc)
        return datetime.now(self._tz or timezone.utc)

    @staticmethod
    def _provider_value(provider: Any, default: Any) -> Any:
        if provider is None:
            return default
        if callable(provider):
            try:
                return provider()
            except Exception:
                logger.debug("event_catalog: provider 取值失败（用默认）", exc_info=True)
                return default
        return provider

    def _names_of(self, provider: Any) -> List[str]:
        value = self._provider_value(provider, ())
        if isinstance(value, str):
            return [value] if value.strip() else []
        try:
            return [str(item) for item in value if str(item or "").strip()]
        except TypeError:
            return []

    # ---- 状态账本（自动每日一次 / token 预算）----
    def _load_state(self) -> Dict[str, Any]:
        if self._state_path is None:
            return {"version": 1}
        try:
            if not self._state_path.exists():
                return {"version": 1}
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return dict(data) if isinstance(data, dict) else {"version": 1}
        except Exception:
            return {"version": 1}

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state["version"] = 1
            save_json_atomic(self._state_path, self._state)
        except Exception:
            logger.debug("event_catalog: 状态账本落盘失败（忽略）", exc_info=True)

    def _day_of(self, now: datetime) -> str:
        return now.date().isoformat()

    def _auto_attempted_today(self, now: datetime) -> bool:
        return str(self._state.get("auto_last_attempt_date") or "") == self._day_of(now)

    def _mark_auto_attempt(self, now: datetime) -> None:
        self._state["auto_last_attempt_date"] = self._day_of(now)
        self._save_state()

    def _tokens_today(self, now: datetime) -> int:
        if (str(self._state.get("token_date") or "") != self._day_of(now)
                or self._state.get("token_basis") != "host_reported"):
            return 0
        try:
            return max(0, int(self._state.get("token_used") or 0))
        except (TypeError, ValueError):
            return 0

    def _write_tokens_today(self, used: int, now: datetime) -> None:
        self._state["token_date"] = self._day_of(now)
        self._state["token_used"] = max(0, int(used))
        self._state["token_basis"] = "host_reported"
        self._save_state()







    def _record_tokens(self, tokens: int, now: datetime) -> None:
        """兼容旧内部调用：直接累加（新预算流程只用 reserve/settle）。"""
        self._write_tokens_today(self._tokens_today(now) + max(0, int(tokens)), now)

    # ---- 只读状态 / 查询 ----
    def get_event(self, event_id: Any) -> Optional[Dict[str, Any]]:
        return self.store.get_event(event_id)

    def get_state_deltas(self, event_id: Any) -> Optional[Dict[str, float]]:
        return self.store.get_state_deltas(event_id)

    def set_event_gate(self, gate: Optional[Callable[[str], bool]]) -> None:
        """运行时事件门控（透传 store；装配根注入 video_watch.enabled 闸）。"""
        self.store.set_event_gate(gate)

    @property
    def catalog_version(self) -> str:
        return self.store.catalog_version

    @property
    def state_path(self) -> Optional[Path]:
        return self._state_path

    def pick_event(self, rng: random.Random, state: Any = None, recent: Any = None,
                   *, now: Optional[datetime] = None, context: Optional[str] = None,
                   events: Optional[Sequence[Mapping[str, Any]]] = None) -> Optional[Dict[str, Any]]:
        try:
            return self.store.pick_event(rng, state, recent, now=self._now(now),
                                         context=context, events=events)
        except Exception as exc:  # 运行时选择失败只回退内置池/不插事件
            logger.warning("event_catalog: pick_event 失败（本次不插随机事件）：%s", exc)
            try:
                return self.store.pick_event(rng, state, recent, now=self._now(now),
                                             context=context,
                                             events=self.store.snapshot.builtin_events)
            except Exception:
                return None

    def metadata_for_event(self, event: Any, *, trigger: str = "random_event",
                           description: Optional[str] = None) -> Dict[str, Any]:
        """episode 创建时快照（库更新不改变进行中活动结算）。

        返回的 ``event_id/event_key/catalog_version/event_catalog_version/
        state_deltas`` 应原样写进 ``activity_episodes.metadata``；关闭时优先读
        快照，不再回查事件池。旧 episode 无快照才回退旧内置池。
        """
        if isinstance(event, str):
            resolved = self.store.get_event(event)
        elif isinstance(event, Mapping):
            resolved = dict(event)
        else:
            resolved = None
        if not resolved:
            return {}
        eid = str(resolved.get("event_id") or resolved.get("key") or "")
        metadata: Dict[str, Any] = {
            "trigger": trigger,
            "event_id": eid,
            "event_key": eid,
            "catalog_version": self.store.catalog_version,
            "event_catalog_version": self.store.catalog_version,
            "event_source": str(resolved.get("source") or ""),
            "event_name": str(resolved.get("name") or ""),
            "event_pool_kind": str(resolved.get('pool_kind') or 'reusable'),
            "event_time_slot": str(resolved.get('time_slot') or ''),
            "event_generation_id": str(resolved.get('generation_id') or ''),
            "goal_type": str(resolved.get("goal_type") or ""),
            "state_deltas": dict(resolved.get("state_deltas") or {}),
        }
        if description:
            metadata["description"] = description
        if resolved.get("duration_minutes") is not None:
            metadata["duration_minutes"] = resolved.get("duration_minutes")
        return metadata

    def claim_event(self, event: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
        return self.store.claim_event(event, now=self._now(now))

    def finish_claim(self, event: Mapping[str, Any], status: str, episode_id: str = '',
                     *, reason: str = '') -> None:
        self.store.finish_claim(event, status, episode_id, reason=reason)

    def status(self) -> Dict[str, Any]:
        cfg = self._current_config()
        now = self._now()
        snap = self.store.snapshot
        last = self._last_refresh
        return {
            "mode": cfg.mode,
            "path": str(self.store.path),
            "file_exists": self.store.path.exists(),
            "loaded": bool(snap.document) or bool(snap.active_events),
            "degraded": bool(snap.degraded),
            "reason": snap.reason,
            "catalog_version": snap.catalog_version,
            "generated_events": len(snap.generated_events),
            "reusable_events": len(snap.reusable_events),
            "one_shot_remaining": len(snap.one_shot_events),
            "one_shot_consumed": len(snap.consumed_events),
            "generation_id": snap.generation_id,
            "builtin_events": len(snap.builtin_events),
            "active_events": len(snap.active_events),
            "invalid_count": snap.invalid_count,
            "pool_truncated": snap.pool_truncated,
            "generated_at": snap.generated_at,
            "next_update_at": snap.next_update_at,
            "auto_last_attempt_date": str(self._state.get("auto_last_attempt_date") or ""),
            "tokens_today": self._tokens_today(now),
            "task_name": cfg.task_name,
            "min_interval_days": cfg.min_interval_days,
            "max_interval_days": cfg.max_interval_days,
            "max_generated_events": cfg.max_generated_events,
            "max_pool_events": cfg.max_pool_events,
            "auto_running": bool(self._task is not None and not self._task.done()),
            "last_refresh": (None if last is None else {
                "ok": last.ok, "status": last.status, "reason": last.reason,
                "errors": list(last.errors), "events_count": last.events_count,
                "next_update_at": last.next_update_at,
            }),
        }

    def status_text(self) -> str:
        snap = self.store.snapshot
        cfg = self._current_config()
        parts = [f"事件库模式：{cfg.mode}",
                 f"目录版本：{snap.catalog_version}",
                 f"生成事件：{len(snap.generated_events)} 条 / 内置：{len(snap.builtin_events)} 条"]
        if snap.next_update_at:
            parts.append(f"下次更新：{snap.next_update_at}")
        if snap.degraded:
            parts.append(f"状态：降级（{snap.reason or 'invalid'}）")
        return "；".join(parts)

    def reload(self) -> CatalogSnapshot:
        """重新装载事件库；``mode=off`` 时**不读**生成库（只保留内置池）。"""
        try:
            if self._current_config().mode == "off":
                return self.store.use_builtin_only("mode_off")
            return self.store.reload()
        except Exception as exc:
            logger.warning("event_catalog: reload 失败（保留旧快照）：%s", exc)
            return self.store.snapshot

    # ---- 生成刷新 ----
    def _due(self, now: datetime) -> bool:
        raw = self.store.next_update_at
        if not raw:
            return True
        parsed = _parse_iso(raw, self._tz)
        return parsed is None or now >= parsed

    def _gate(self, now: datetime) -> Tuple[bool, str]:
        if self._auto_gate is None:
            if self._allow_ungated_auto:
                return True, ""
            return False, "gate_unavailable"
        try:
            value = self._auto_gate(now)
        except Exception as exc:
            logger.warning("event_catalog: 自动生成闸异常（本次跳过）：%s", exc)
            return False, "gate_error"
        if isinstance(value, bool):
            return (True, "") if value else (False, "gate_denied")
        if isinstance(value, Mapping):
            if value.get("can_generate") is False:
                return False, "gate_denied"
            if not value.get("awake", True):
                return False, "gate_not_awake"
            if value.get("in_sleep") or value.get("sleeping"):
                return False, "gate_in_sleep"
            if value.get("hard_commitment"):
                return False, "gate_hard_commitment"
            return True, ""
        return False, "gate_invalid"

    def _skipped(self, reason: str, *, manual: bool) -> RefreshResult:
        return RefreshResult(ok=False, status="skipped", manual=manual,
                             errors=(reason,), reason=reason,
                             catalog_version=self.store.catalog_version,
                             next_update_at=self.store.next_update_at,
                             events_count=len(self.store.generated_events))

    def _failed(self, code: str, message: str, *, manual: bool, tokens: int = 0,
                called_llm: bool = False) -> RefreshResult:
        return RefreshResult(ok=False, status="failed", manual=manual,
                             errors=(f"{code}: {message}",), reason=code,
                             catalog_version=self.store.catalog_version,
                             next_update_at=self.store.next_update_at,
                             tokens_used=tokens, called_llm=called_llm)

    async def refresh(self, *, manual: bool = False,
                      now: Optional[datetime] = None) -> RefreshResult:
        """生成一次并全量替换（manual=True 时错误逐条返回给调用方）。"""
        cfg = self._current_config()
        if cfg.mode == "off":
            result = self._skipped("mode_off", manual=manual)
        elif not manual and cfg.mode != "auto":
            result = self._skipped("mode_not_auto", manual=manual)
        else:
            now_dt = self._now(now)
            try:
                async with self._lock:
                    result = await self._refresh_locked(cfg, manual, now_dt)
            except Exception as exc:  # 绝不拖挂插件
                logger.warning("event_catalog: refresh 异常（旧库保留）：%s", exc)
                result = self._failed("E_INTERNAL", str(exc), manual=manual)
        self._last_refresh = result
        return result

    async def _call_llm_once(self, prompt: Any, *, task_name: str,
                             temperature: float,
                             max_tokens: Optional[int] = None) -> LLMResult:
        """发出恰好一次生成请求；详细结果可用时走同一响应的 LLMResult。

        3.17.6（E_LLM 截断修复）：max_tokens 显式 override（有界，调用方按
        requested 计算）；旧测试桩签名不接受时不传（兼容，不改单次请求语义）。
        """
        detailed = getattr(self._llm, "generate_detailed", None)
        if callable(detailed):
            extra: Dict[str, Any] = {}
            if max_tokens is not None and _callable_accepts(detailed, "max_tokens"):
                extra["max_tokens"] = int(max_tokens)
            return await detailed(
                prompt.prompt,
                system_prompt=prompt.system_prompt,
                task_name=task_name,
                temperature=temperature,

                module="planning",
                event=LLM_EVENT_NAME,
                strict_params=True,
                with_identity=True,
                **extra,
            )
        # 兼容旧测试桩：只有字符串返回，无 usage/错误分类；失败一律保守记账。
        text = await self._llm.generate(
            prompt.prompt,
            system_prompt=prompt.system_prompt,
            task_name=task_name,
            temperature=temperature,

            module="planning",
            event=LLM_EVENT_NAME,
            with_identity=True,
        )
        text = str(text or "").strip()
        return LLMResult(ok=bool(text), text=text,
                         category="ok" if text else "empty",
                         task_name=task_name, request_sent=True, consumed=True,
                         attempts=1)

    async def _attempt_llm(self, prompt: Any, *, task_name: str,
                           temperature: float, now: datetime,
                           max_tokens: Optional[int] = None) -> Tuple[LLMResult, int]:
        """No plugin limit or precharge; accumulate only host-reported usage."""
        result = await self._call_llm_once(prompt, task_name=task_name, temperature=temperature,
                                           max_tokens=max_tokens)
        spent = max(0, int(result.total_tokens)) if result.usage_available and result.total_tokens is not None else 0
        if result.usage_available:
            self._record_tokens(spent, now)
        return result, spent

    async def _refresh_locked(self, cfg: EventCatalogConfig, manual: bool,
                              now: datetime) -> RefreshResult:
        self.store.reload()
        start_fp = self.store.file_fingerprint()
        persona = str(self._provider_value(self._persona_provider, '') or '')
        recent_schedule = self._names_of(self._recent_schedule_provider)
        recent_events = self._names_of(self._recent_event_provider)
        builtin = self.store.snapshot.builtin_events
        from .event_slots import TIME_SLOTS
        reusable_limits = cfg.reusable_limits
        one_shot_limits = cfg.one_shot_limits
        # 3.17.6（PR-REMOVE-POOLCAP）：移除生成前「配额和 vs 独立总容量」的
        # E_CAPACITY 校验——三时段配额各自合法（validator 逐段校验）即允许生成；
        # 池上限派生自配额和，天然自洽，不再有独立总容量拒绝路径。
        if self._llm is None or not (callable(getattr(self._llm, 'generate_detailed', None))
                                    or callable(getattr(self._llm, 'generate', None))):
            return self._failed('E_LLM', 'LLMService不可用', manual=manual)
        reserved_names = [e['name'] for e in (*builtin, *self.store.snapshot.generated_events)] + recent_events
        reserved_ids = {e['event_id'] for e in builtin}
        events, hashes, models, failures = [], [], [], []
        charged = 0
        model_calls = 0
        from core.llm_utils import over_limit_trim_enabled
        trim = over_limit_trim_enabled(self._config)
        for slot_index, slot in enumerate(TIME_SLOTS):
            reusable_n = int(reusable_limits[slot_index])
            one_shot_n = int(one_shot_limits[slot_index])
            requested = reusable_n + one_shot_n
            if requested <= 0:
                continue
            prompt = build_event_prompt(persona=persona, recent_schedule_names=recent_schedule,
                existing_event_names=reserved_names + [e.get('name', '') for e in events],
                count=requested, min_count=1, time_slot=slot, no_puzzle=cfg.no_puzzle)
            # 3.17.6（E_LLM 截断修复）：按本时段 requested 动态、有界地显式
            # override max_tokens（宿主任务配置不足时不再截断 JSON）。
            slot_max_tokens = event_generation_max_tokens(requested)
            try:
                chosen, spent = await self._attempt_llm(prompt, task_name=cfg.task_name,
                    temperature=EVENT_GENERATION_TEMPERATURE, now=now,
                    max_tokens=slot_max_tokens)
                charged += spent
                model_calls += max(0, int(chosen.attempts or 0))
                # A declared-but-missing task sent no model request; resolve its normal fallback.
                if chosen.category == 'task_missing' and cfg.task_name != LLM_FALLBACK_TASK:
                    chosen, spent = await self._attempt_llm(prompt, task_name=LLM_FALLBACK_TASK,
                        temperature=EVENT_GENERATION_TEMPERATURE, now=now,
                        max_tokens=slot_max_tokens)
                    charged += spent
                    model_calls += max(0, int(chosen.attempts or 0))
                if not chosen.ok or not str(chosen.text or '').strip():
                    if _response_truncated(chosen, slot_max_tokens):
                        failures.append(f'{slot}: 输出被 max_tokens 截断（finish_reason=length '
                                        f'或 completion_tokens 触顶 {slot_max_tokens}），需增大上限')
                    else:
                        failures.append(f'{slot}: 模型未返回可用事件（{chosen.category}）')
                    continue
                try:
                    rows = parse_events_response(chosen.text)
                except EventPromptError as exc:
                    # 3.17.6：截断诊断明示——finish_reason=length（宿主回传时）
                    # 或 completion_tokens 触顶本次发送的 max_tokens → JSON 被截断。
                    if _response_truncated(chosen, slot_max_tokens):
                        failures.append(f'{slot}: 输出被 max_tokens 截断致 JSON 不可解析'
                                        f'（finish_reason=length 或 completion_tokens 触顶 '
                                        f'{slot_max_tokens}），需增大上限：{exc}')
                    else:
                        failures.append(f'{slot}: 生成响应不可用（{exc}）')
                    continue
                if len(rows) < 1:
                    failures.append(f'{slot}: 模型未返回事件')
                    continue
                if len(rows) > requested:
                    if trim:
                        rows = rows[:requested]  # 超量裁剪兜底：取前N条，维持按索引分池语义
                    else:
                        failures.append(f'{slot}: 返回条数不在1-{requested}内')
                        continue
                for i, row in enumerate(rows):
                    item = _normalize_event(row)
                    item['pool_kind'] = 'reusable' if i < reusable_n else 'one_shot'
                    item['time_slot'] = slot
                    events.append(item)
                hashes.append(prompt.prompt_hash)
                models.append(str(getattr(self._llm, 'model_name', '') or ''))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning('event_catalog: %s时段生成失败，保留旧库：%s', slot, exc)
                failures.append(f'{slot}: 生成响应不可用')
        if failures:
            return self._failed('E_LLM', '；'.join(failures), manual=manual,
                                tokens=charged, called_llm=model_calls > 0)
        document = {
            'version': CATALOG_VERSION, 'source': 'llm', 'model': ','.join(models),
            'generation_id': 'generation_' + uuid4().hex,
            'generated_at': now.isoformat(timespec='seconds'),
            'next_update_at': (now + timedelta(days=self._next_interval_days(cfg))).isoformat(timespec='seconds'),
            'prompt_hash': hashlib.sha256('|'.join(hashes).encode()).hexdigest(),
            'events': events, 'consumed': {},
        }
        validator = self._validator if self._custom_validator else EventCatalogValidator(
            builtin_count=len(builtin), max_generated_events=cfg.max_generated_events,
            reusable_limits=cfg.reusable_limits, one_shot_limits=cfg.one_shot_limits)
        validation = validator.validate_document(document, reserved_ids=reserved_ids,
            reserved_names=reserved_names, recent_names=recent_events)
        if not validation.ok:
            return RefreshResult(ok=False, status='failed', manual=manual,
                errors=tuple(validation.lines), reason='E_VALIDATION',
                catalog_version=self.store.catalog_version, next_update_at=self.store.next_update_at,
                events_count=0, tokens_used=charged, called_llm=model_calls > 0)
        current_mode = self._current_config().mode
        if self._stopping or current_mode == 'off' or current_mode != cfg.mode:
            return self._failed('E_MODE_CHANGED', '生成期间模式已变化，保留旧事件库', manual=manual,
                                tokens=charged, called_llm=model_calls > 0)
        if self.store.file_fingerprint() != start_fp:
            return self._failed('E_CONFLICT', '生成期间事件库或消耗记录变化，保留旧库', manual=manual,
                                tokens=charged, called_llm=model_calls > 0)
        if not self.store.save(document, expected_fingerprint=start_fp):
            return self._failed('E_WRITE', '双池原子发布失败，保留旧库', manual=manual,
                                tokens=charged, called_llm=model_calls > 0)
        return RefreshResult(ok=True, status='success', manual=manual,
            catalog_version=self.store.catalog_version, next_update_at=self.store.next_update_at,
            events_count=len(self.store.generated_events), tokens_used=charged, called_llm=model_calls > 0)


    def _next_interval_days(self, cfg: EventCatalogConfig) -> int:
        try:
            return int(self._rng.randint(cfg.min_interval_days, cfg.max_interval_days))
        except Exception:
            return cfg.min_interval_days

    # ---- 自动调度 ----
    async def auto_check(self, now: Optional[datetime] = None) -> Optional[RefreshResult]:
        """每日最多一次、到期、清醒/非睡眠/无硬约定才生成；失败当日不重试。"""
        cfg = self._current_config()
        if cfg.mode != "auto":
            return None
        now_dt = self._now(now)
        if self._auto_attempted_today(now_dt):
            return None
        if not self._due(now_dt):
            return None
        allowed, reason = self._gate(now_dt)
        if not allowed:
            return self._skipped(reason, manual=False)
        if self._lock.locked():
            return None
        self._mark_auto_attempt(now_dt)  # 先记当日尝试：失败也不在当天重试
        result = await self.refresh(manual=False, now=now_dt)
        if result is not None and result.status == "failed":
            logger.warning("event_catalog.auto_refresh 失败（旧库保留，当日不重试）：%s",
                           "；".join(result.error_lines))
        return result

    async def start(self) -> None:
        """装配入口：``mode!=off`` 时装载生成库；``mode=auto`` 时挂后台检查任务。

        - ``mode=off``：**不读**生成库、不启动任何后台任务（off 只用内置池；用户文件
          不删不改）；若此前 auto 后台在跑则在此停掉；
        - 可重复调用（on_load / 热重载 / 停用后重启共用；不会重复建任务、不会重建实例）；
        - 任何异常只记日志，绝不拖挂插件。
        """
        try:
            cfg = self._current_config()
            if cfg.mode == "off":
                self.store.use_builtin_only("mode_off")
                await self.stop()
                logger.info("event_catalog: mode=off（不读生成库、不启动后台）")
                return
            self.reload()
            if cfg.auto_enabled and self._auto_gate is None and not self._allow_ungated_auto:
                logger.warning("event_catalog: mode=auto 但未提供 auto_gate，自动生成将保持跳过"
                               "（fail-closed；接线时需注入清醒/睡眠/硬约定闸）")
            if cfg.auto_enabled and self._task is None:
                self._stopping = False
                self._task = asyncio.create_task(self._auto_loop())
                logger.info("event_catalog: 自动事件库检查已启动（每 %ss 检查一次）",
                            cfg.auto_check_seconds)
        except Exception as exc:
            logger.warning("event_catalog: start 失败（服务保持只读）：%s", exc)
            self._task = None

    async def sync_config(self) -> None:
        """配置热更新后的模式切换口（幂等；不新建实例、不重置账本）。

        ``auto→其它``/``off``：停后台；``→manual/auto``：按需读库；``auto`` 且任务
        已在跑 → 什么都不做（避免重复后台/绕预算）。生成记账（``event_catalog_state.json``）
        与实例始终复用同一份，故关闭再打开不会重置当日预算/「每日一次」记录。
        """
        await self.start()

    async def stop(self) -> None:
        """停止后台检查；可重复调用。"""
        self._stopping = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("event_catalog: 自动任务退出异常（忽略）", exc_info=True)

    async def _auto_loop(self) -> None:
        while not self._stopping:
            try:
                await self.auto_check()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("event_catalog: 自动检查异常（继续下一轮）", exc_info=True)
            seconds = max(30, self._current_config().auto_check_seconds)
            try:
                await asyncio.sleep(seconds)
            except asyncio.CancelledError:
                raise

    def __del__(self) -> None:  # pragma: no cover - 只做兜底，不阻塞
        try:
            task = getattr(self, "_task", None)
            if task is not None and not task.done():
                task.cancel()
        except Exception:
            pass


def _tzinfo(value: Any) -> Optional[timezone]:
    if value is None or isinstance(value, timezone):
        return value
    name = str(value or "").strip()
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)  # type: ignore[return-value]
    except Exception:
        return None


def _normalize_event(raw: Any) -> Any:
    """只做字符串去空白（不把非法类型转成合法）；校验仍按原类型判定。"""
    if not isinstance(raw, dict):
        return raw
    event = dict(raw)
    for key in ("event_id", "name", "goal_type"):
        if key in event and isinstance(event[key], str):
            event[key] = event[key].strip()
    templates = event.get("templates")
    if isinstance(templates, list):
        event["templates"] = [item.strip() if isinstance(item, str) else item
                              for item in templates]
    event.setdefault("enabled", True)
    return event


__all__ = [
    "BUILTIN_EVENTS", "CatalogSnapshot", "EventCatalogService", "EventCatalogStore",
    "EventCatalogValidator", "RefreshResult", "ValidationError", "ValidationResult",
    "build_json_schema", "builtin_events", "compute_catalog_version",
    "resolve_state_deltas", "state_affinity",
]

