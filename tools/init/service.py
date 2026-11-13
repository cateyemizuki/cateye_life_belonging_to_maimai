# -*- coding: utf-8 -*-
"""统一初始化服务（自 ``planning/init_service.py`` 整体迁入 ``tools/init/``）。

设计约束（定稿 §4.4 / 附录 B.5；2026-11-13 自动初始化裁决批）：

- 只编排，不直接发 QQ：:class:`InitService` 永远不持有 ``ctx`` / 不调用
  ``send.text``；命令入口把 ``result.summary_lines`` / ``result.error_lines``
  逐条发送。自动调用（``manual=False``）因此天然不产生主动消息。
- 阶段独立：preflight 失败才整体停止；业务字段（``STAGE_ORDER`` 七项）互不
  阻断，失败只影响本字段（自动驱动逐字段捕获异常继续）。
- 幂等：全量初始化重复执行时，已初始化的情绪参数与未到期的事件库直接
  ``skipped``，不重复消费模型；已有 ``life_state.json`` 只补缺失键，绝不
  覆盖已有值，也不改配置（不会自动启用生活状态/睡眠引擎）。
- 事件：全量走 ``EventCatalogService.refresh(manual=True)`` 的到期检查；
  ``/lbm ops init events`` 强制刷新。成功才采用返回的 ``next_update_at``；
  失败保留旧库（由 EventCatalogService 保证），本服务只如实记录旧值。
- 完成标记：**不再写 ``life_init.json`` 汇总**（2026-11-13 用户裁决弃用）。
  逐字段完成状态由 :mod:`tools.init.registry` 的 ``init_registry.json`` 承担；
  ``status()`` 直读登记文件，不再读旧汇总。
- status 纯读：只读登记文件、事件服务 ``status()``、情绪 ``status_snapshot()``、
  生命周期文件存在性与 DB schema，不写任何文件。

后续共享接线：

1. ``PlanningServices`` 构造 ``InitService(self._plugin, data_dir=...)`` 并
   暴露为 ``self.init``；``plugin.py`` 可用 ``_planning.init`` 取用。
2. 双语根命令 ``/lbm ops init [字段|all|status]`` / ``/麦麦生活 运维 初始化``
   经注册表 handler 进入 ``tools.init.auto_init`` 的统一驱动；手动不受退避限制。
3. 事件服务实例必须提供 ``status() -> dict`` 与
   ``async refresh(*, manual: bool) -> RefreshResult``（PR4 已完成）。
4. 频率阶段需要「目标频率公共校准口」：默认解析
   ``plugin._planning.proactive.calibrate_target_frequency()``，返回
   ``None/True`` 或 ``{"ok": bool, "target_resolved": bool, ...}``；
   也可由构造参数 ``frequency_calibrator`` 注入。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import sqlite3
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.storage import save_json_atomic
from planning.life_state import (STATE_FILE_NAME, STATE_VERSION, default_state_document,
                                resolve_state_file_path)

from .manifest import INIT_FIELDS
from .registry import InitRegistry

logger = logging.getLogger(__name__)

#: 业务字段顺序（preflight 为框架阶段，不计入字段 DoD；顺序 = 依赖顺序）
STAGE_ORDER: Tuple[str, ...] = tuple(INIT_FIELDS)

#: 当前 ``life_state.json`` 的完整字段及缺失时的最小默认值。
#: **唯一真源 = planning.life_state.default_state_document()**（与服务 save() 共用
#: ``_document()``）——2026-10-27 共享接线改：不再维护静态 19 字段清单，避免 PR3 新增
#: 睡眠字段后「19/20 字段」口径漂移。
def _state_defaults() -> Dict[str, Any]:
    """规范默认状态文档（缺字段补齐 / 损坏重建共用；含 version）。"""
    return default_state_document()


# ======================================================================
# 结果模型（与既有服务同风格；可直接逐条发送）
# ======================================================================
@dataclass(frozen=True)
class StageResult:
    """单阶段结构化结果。"""

    stage: str
    status: str                     # success / skipped / failed / cancelled
    message: str = ""
    reason: str = ""
    error_lines: Tuple[str, ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("success", "skipped")

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def failed(self) -> bool:
        return self.status in ("failed", "cancelled")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "message": self.message,
            "reason": self.reason,
            "errors": list(self.error_lines),
            "details": _jsonable(self.details),
        }


@dataclass(frozen=True)
class InitResult:
    """单个字段/一次命令初始化的结构化结果。"""

    command: str
    manual: bool
    status: str                    # success / partial / failed / skipped / disabled / cancelled
    stages: Tuple[StageResult, ...]
    started_at: str = ""
    finished_at: str = ""
    persona_name: str = ""
    persona_fingerprint: str = ""
    plugin_version: str = ""
    config_version: str = ""
    catalog_version: str = ""
    next_update_at: Optional[str] = None
    disabled: bool = False

    @property
    def ok(self) -> bool:
        return self.status in ("success", "skipped")

    @property
    def business_stages(self) -> Tuple[StageResult, ...]:
        return tuple(stage for stage in self.stages if stage.stage in STAGE_ORDER)

    def stage(self, name: str) -> Optional[StageResult]:
        for stage in self.stages:
            if stage.stage == name:
                return stage
        return None

    @property
    def failed_stages(self) -> Tuple[StageResult, ...]:
        return tuple(stage for stage in self.stages if stage.failed)

    @property
    def error_lines(self) -> Tuple[str, ...]:
        lines: List[str] = []
        for stage in self.stages:
            for line in stage.error_lines:
                text = str(line or "").strip()
                if text:
                    lines.append(f"{_stage_label(stage.stage)}：{text}")
        return tuple(lines)

    @property
    def summary_lines(self) -> List[str]:
        """命令入口可直接逐条发送的文本（不含 @ / 卡片结构）。"""
        lines: List[str] = []
        if self.status == "disabled":
            return ["初始化未执行：插件未启用（plugin.enabled=false）。"]
        lines.append(_headline(self))
        for stage in self.stages:
            if stage.stage == "preflight" and stage.ok:
                continue
            text = stage.message or _default_stage_message(stage)
            if stage.error_lines:
                text = text or _stage_label(stage.stage)
                lines.append(f"[{_status_word(stage.status)}] {_stage_label(stage.stage)}：{text}")
                for error in stage.error_lines:
                    lines.append(f"  - {error}")
            else:
                lines.append(f"[{_status_word(stage.status)}] {_stage_label(stage.stage)}：{text}")
        if self.next_update_at:
            lines.append(f"下次事件更新：{_short_ts(self.next_update_at)}")
        if self.catalog_version:
            lines.append(f"事件库版本：{self.catalog_version}")
        return lines

    @property
    def status_text(self) -> str:
        return "\n".join(self.summary_lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "command": self.command,
            "manual": bool(self.manual),
            "status": self.status,
            "ok": self.ok,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "persona": {"name": self.persona_name,
                        "fingerprint": self.persona_fingerprint},
            "plugin_version": self.plugin_version,
            "config_version": self.config_version,
            "catalog_version": self.catalog_version,
            "next_update_at": self.next_update_at,
            "disabled": bool(self.disabled),
            "stages": [stage.to_dict() for stage in self.stages],
            "errors": list(self.error_lines),
        }


@dataclass(frozen=True)
class InitStatus:
    """``/lbm ops init status`` 的只读快照（直读 init_registry.json）。"""

    plugin_enabled: bool
    registry_path: str
    registry_plugin_version: str
    fields: Mapping[str, Mapping[str, Any]]
    preflight: Mapping[str, Any]
    next_update_at: Optional[str]
    catalog_version: str
    event_mode: str
    event_degraded: bool
    event_reason: str
    switches: Mapping[str, Any]
    emotion_initialized: bool
    life_state_file_exists: bool
    life_state_file: str
    db_version: str
    sleep_sessions: bool

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))

    @property
    def status_text(self) -> str:
        lines: List[str] = []
        if not self.plugin_enabled:
            lines.append("插件未启用（plugin.enabled=false）。")
        lines.append(f"初始化登记：{self.registry_path}"
                     f"（schema v1，登记版本={self.registry_plugin_version or '未写入'}）")
        preflight = self.preflight or {}
        preflight_ok = preflight.get("ok")
        if preflight_ok is True:
            lines.append(f"预检：通过（{_short_ts(preflight.get('checked_at')) or '时间未知'}）")
        elif preflight_ok is False:
            reason = str(preflight.get("reason") or "未知原因")
            lines.append(f"预检：失败（{reason}；整体未初始化）")
        else:
            lines.append("预检：尚无记录（插件停用或尚未执行加载自动初始化）")
        for name in STAGE_ORDER:
            entry = self.fields.get(name) or {}
            initialized = bool(entry.get("initialized"))
            line = (
                f"- {_stage_label(name)}：{'已初始化' if initialized else '未初始化'}"
                f"，version={entry.get('version') or '-'}"
                f"，attempts={int(entry.get('attempts') or 0)}"
                f"，validated_at={_short_ts(entry.get('validated_at')) or '-'}"
                f"，last_failed_at={_short_ts(entry.get('last_failed_at')) or '-'}"
            )
            last_error = str(entry.get("last_error") or "").strip()
            if last_error and not initialized:
                line = f"{line}\n  上次失败原因：{last_error}"
            lines.append(line)
        lines.append(f"事件库：模式 {self.event_mode}，版本 {self.catalog_version or '无'}"
                     + ("（降级：" + self.event_reason + "）" if self.event_degraded else ""))
        if self.next_update_at:
            lines.append(f"下次事件更新：{_short_ts(self.next_update_at)}")
        else:
            lines.append("下次事件更新：未安排")
        # PR8 3.0.0：``[schedule].enabled`` 是唯一日程总开关，生活状态/睡眠引擎随之
        # 生效或停摆（旧 [life_state] 两开关退役；依据 = 用户 2026-10-27 授权直接清理
        # + 逐符号审定清单 WP1）。
        schedule_on = bool(self.switches.get("life_state_enabled"))
        lines.append(f"开关：日程={'开' if schedule_on else '关'}"
                     f"（生活状态与睡眠引擎随日程开关）"
                     f"，生活状态文件={'有' if self.life_state_file_exists else '无'}"
                     f"，情绪参数={'已初始化' if self.emotion_initialized else '未初始化'}")
        lines.append(f"数据库：v{self.db_version or '?'}"
                     f"，sleep_sessions={'有' if self.sleep_sessions else '无'}")
        return "\n".join(lines)


# ======================================================================
# 小工具
# ======================================================================
def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """把阶段详情收敛为可 JSON 序列化的浅结构（不打印长文本）。"""
    if depth > 4:
        return str(value)[:200]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth=depth + 1) for v in value]
    if is_dataclass(value):
        try:
            return _jsonable(asdict(value), depth=depth + 1)
        except Exception:
            return str(value)[:200]
    return str(value)[:200]


def _first_line(text: str, limit: int = 240) -> str:
    raw = str(text or "").strip()
    line = raw.splitlines()[0] if raw else ""
    return line[:limit]


def _stage_label(name: str) -> str:
    return {
        "preflight": "预检",
        "emotion": "情绪参数",
        "events": "事件库",
        "state_sleep": "状态/睡眠",
        "frequency": "目标频率",
        "video_interest": "刷视频偏好",
        "companion_disposition": "联系倾向",
        "activity_preferences": "日程偏好",
        "summary": "汇总",
    }.get(name, name or "阶段")


def _status_word(status: str) -> str:
    return {
        "success": "成功",
        "skipped": "跳过",
        "failed": "失败",
        "cancelled": "已取消",
        "pending": "未执行",
    }.get(status, status or "未知")


def _overall_word(status: str) -> str:
    return {
        "success": "成功",
        "partial": "部分完成",
        "failed": "失败",
        "skipped": "全部跳过",
        "disabled": "未启用",
        "cancelled": "已取消",
    }.get(status, status or "未知")


def _headline(result: InitResult) -> str:
    business = result.business_stages
    success = sum(1 for s in business if s.status == "success")
    skipped = sum(1 for s in business if s.status == "skipped")
    failed = sum(1 for s in business if s.failed)
    prefix = "全量初始化" if result.command == "full" else (
        "情绪初始化" if result.command == "emotion" else "事件库初始化")
    if result.status == "success":
        tail = f"{success} 阶段完成"
        if skipped:
            tail += f"，{skipped} 阶段跳过"
        return f"{prefix}完成：{tail}。"
    if result.status == "skipped":
        return f"{prefix}未执行：{skipped} 阶段均按开关跳过。"
    if result.status == "cancelled":
        return f"{prefix}已取消：已完成 {success} 阶段，未完成阶段可重新执行。"
    return f"{prefix}{_overall_word(result.status)}：{success} 完成 / {skipped} 跳过 / {failed} 失败。"


def _default_stage_message(stage: StageResult) -> str:
    if stage.status == "success":
        return "完成"
    if stage.status == "skipped":
        return stage.reason or "跳过"
    return stage.reason or "未完成"


def _short_ts(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:32]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "开"):
        return True
    if text in ("0", "false", "no", "off", "关", ""):
        return False
    return default


def _field(source: Any, section: str, key: str, default: Any = None) -> Any:
    """从对象或 dict 配置里读 section.key（缺段/坏值回默认）。"""
    sec = None
    if isinstance(source, Mapping):
        sec = source.get(section)
    elif source is not None:
        sec = getattr(source, section, None)
    if isinstance(sec, Mapping):
        return sec.get(key, default)
    if sec is None:
        return default
    return getattr(sec, key, default)


def _fingerprint(text: str) -> str:
    text = str(text or "")
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _is_async_callable(obj: Any) -> bool:
    return inspect.iscoroutinefunction(obj) or inspect.iscoroutinefunction(
        getattr(obj, "__call__", None))


def _coerce_initialized(raw: Any) -> Optional[bool]:
    if isinstance(raw, Mapping):
        value = raw.get("initialized")
        return bool(value) if value is not None else None
    if isinstance(raw, bool):
        return raw
    return None


def _emotion_outcome(raw: Any) -> Dict[str, Any]:
    """情绪初始化返回值适配：兼容现有 str，也接受结构化结果。"""
    if isinstance(raw, StageResult):
        return {
            "ok": raw.ok and raw.status != "skipped",
            "message": raw.message or (raw.error_lines[0] if raw.error_lines else ""),
            "errors": list(raw.error_lines),
            "details": dict(raw.details),
        }
    if isinstance(raw, Mapping):
        ok = raw.get("ok")
        if ok is None:
            ok = str(raw.get("status") or "").lower() in ("success", "ok", "done")
        errors = raw.get("error_lines") or raw.get("errors") or ()
        return {
            "ok": bool(ok),
            "message": str(raw.get("message") or raw.get("text") or ""),
            "errors": [str(x) for x in errors] if not isinstance(errors, str) else [errors],
            "details": {k: v for k, v in raw.items()
                        if k not in ("ok", "message", "text", "errors", "error_lines")},
        }
    text = str(raw or "").strip()
    if not text:
        return {"ok": False, "message": "", "errors": ["情绪初始化失败：模型返回空内容。"],
                "details": {}}
    first = text.splitlines()[0][:120]
    failed = (first.startswith("情绪初始化失败") or first.startswith("初始化失败")
              or "失败：" in first[:40])
    details = {"output_preview": text[:300]}
    if failed:
        return {"ok": False, "message": first, "errors": [first], "details": details}
    return {"ok": True, "message": first, "errors": [], "details": details}


def _event_outcome(raw: Any) -> Dict[str, Any]:
    """EventCatalogService.RefreshResult / dict / None 适配。"""
    if raw is None:
        return {"ok": False, "status": "failed", "message": "",
                "errors": ["事件库刷新未返回结果。"], "details": {}}
    if isinstance(raw, Mapping):
        get = raw.get
    else:
        get = lambda key, default=None: getattr(raw, key, default)
    errors = get("errors") or get("error_lines") or ()
    if isinstance(errors, str):
        errors = [errors]
    status = str(get("status") or "").lower()
    ok = bool(get("ok"))
    reason = str(get("reason") or "")
    if status == "skipped" and ok:
        message = "事件库刷新跳过（" + (reason or "skipped") + "）。"
    elif ok:
        message = f"事件库已刷新（{int(get('events_count') or 0)} 条生成事件）。"
    else:
        message = ""
    details = {
        "result_status": status,
        "reason": reason,
        "events_count": int(get("events_count") or 0),
        "tokens_used": int(get("tokens_used") or 0),
        "called_llm": bool(get("called_llm")),
        "catalog_version": str(get("catalog_version") or ""),
        "next_update_at": get("next_update_at"),
    }
    return {"ok": ok, "status": status, "message": message, "reason": reason,
            "errors": [str(x) for x in errors], "details": details}


# ======================================================================
# InitService
# ======================================================================
class InitService:
    """统一初始化编排服务。

    Args:
        data_dir: 插件数据目录（``plugin._data_dir`` / ``ctx.paths.data_dir``）。
        plugin: 插件实例（可选；用于默认解析情绪/事件/生活状态/配置/版本）。
        config / config_provider: 配置对象或返回配置的 getter（热重载友好）。
        plugin_enabled: bool 或 getter；缺省读 ``config.plugin.enabled``。
        db / db_path: 已装配的 ScheduleDB 或数据库路径（缺省 data_dir/schedule.db）。
        emotion_initializer: ``async () -> str | StageResult | dict``；缺省
            ``plugin._emotion.initialize_from_persona``。
        emotion_status_provider: 只读 getter，返回 bool / ``{"initialized": bool}``。
        event_catalog / event_catalog_provider: PR4 的 EventCatalogService 或 getter。
        frequency_calibrator: 目标频率公共校准口（无参可调用/可 await）；缺省解析
            ``plugin._planning.proactive.calibrate_target_frequency``。
        life_state: 可选 LifeStateService（本服务只读文件，不用其写口）。
        state_preparer / sleep_sessions_ensurer: 测试/后续接线的替换口。
        persona_provider / version_provider: 汇总元数据 getter。
        now_provider / tz: 时间注入（离线测试固定时间）。
        summary_path: 旧 ``life_init.json`` 路径（只读兼容属性；2026-11-13 起不再写入/读取）。
    """

    def __init__(
        self,
        data_dir: Any = None,
        *,
        plugin: Any = None,
        config: Any = None,
        config_provider: Optional[Callable[[], Any]] = None,
        plugin_enabled: Any = None,
        db: Any = None,
        db_path: Any = None,
        emotion_initializer: Any = None,
        emotion_status_provider: Optional[Callable[[], Any]] = None,
        event_catalog: Any = None,
        event_catalog_provider: Optional[Callable[[], Any]] = None,
        frequency_calibrator: Any = None,
        life_state: Any = None,
        state_preparer: Optional[Callable[..., Any]] = None,
        sleep_sessions_ensurer: Optional[Callable[..., Any]] = None,
        persona_provider: Optional[Callable[[], Any]] = None,
        version_provider: Optional[Callable[[], Any]] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        tz: Any = None,
        summary_path: Any = None,
    ) -> None:
        # 兼容两种装配写法：``InitService(plugin)``（与其他服务束同风格）
        # 与 ``InitService(data_dir, plugin=plugin, ...)``。
        if (plugin is None and data_dir is not None
                and not isinstance(data_dir, (str, bytes, Path))
                and getattr(data_dir, "_data_dir", None) is not None):
            plugin = data_dir
            data_dir = getattr(plugin, "_data_dir")
        if data_dir is None and plugin is not None:
            data_dir = getattr(plugin, "_data_dir", None)
        if data_dir is None:
            raise ValueError("InitService 需要 data_dir（或含 _data_dir 的 plugin）")
        self.data_dir = Path(data_dir)
        self._plugin = plugin
        self._config = config
        self._config_provider = config_provider
        self._plugin_enabled_provider = plugin_enabled
        self._db = db
        if self._db is None and plugin is not None:
            self._db = getattr(plugin, "_db", None)
        self._db_path = Path(db_path) if db_path is not None else None
        self._emotion_initializer = emotion_initializer
        self._emotion_status_provider = emotion_status_provider
        self._event_catalog = event_catalog
        self._event_catalog_provider = event_catalog_provider
        self._frequency_calibrator = frequency_calibrator
        self._life_state = life_state
        self._state_preparer = state_preparer
        self._sleep_sessions_ensurer = sleep_sessions_ensurer
        self._persona_provider = persona_provider
        self._version_provider = version_provider
        self._now_provider = now_provider
        self._tz = tz
        self.summary_path = (Path(summary_path) if summary_path is not None
                             else self.data_dir / "life_init.json")
        #: 逐字段登记文件（完成状态唯一真源；旧 life_init.json 已弃用）
        self.registry = InitRegistry(self.data_dir)
        #: 同一实例的初始化串行化：并发两次 full 不会重复生成；第二个执行时
        #: 情绪已 initialized / 事件 next_update_at 未到期 → skipped。
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 基础读取（全部无副作用）
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        if self._now_provider is not None:
            value = self._now_provider()
            if isinstance(value, datetime):
                return value
        tz = self._tz
        getter = getattr(tz, "get_now", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                pass
        return datetime.now().astimezone()

    def _current_config(self) -> Any:
        source = self._config
        if self._config_provider is not None:
            try:
                source = self._config_provider()
            except Exception:
                source = self._config
        if source is None and self._plugin is not None:
            source = getattr(self._plugin, "config", None)
        return source

    def _plugin_enabled(self) -> bool:
        provider = self._plugin_enabled_provider
        if provider is not None:
            try:
                return bool(provider() if callable(provider) else provider)
            except Exception:
                return False
        cfg = self._current_config()
        return _as_bool(_field(cfg, "plugin", "enabled", True), default=True)

    def _switches(self, cfg: Any = None) -> Dict[str, Any]:
        cfg = self._current_config() if cfg is None else cfg
        freq_value = _field(cfg, "planning_proactive", "target_frequency_always_one", None)
        return {
            # PR8 3.0.0：``[schedule].enabled`` 是唯一日程总开关（旧
            # ``[life_state].enabled`` / ``[life_state].sleep_engine_enabled`` 退役；
            # 依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP1）。两个旧
            # switches 键名保留（诊断 details/摘要消费），取值同源自 schedule.enabled。
            "life_state_enabled": _as_bool(_field(cfg, "schedule", "enabled", True)),
            "sleep_engine_enabled": _as_bool(_field(cfg, "schedule", "enabled", True)),
            "event_catalog_mode": str(_field(cfg, "event_catalog", "mode", "off") or "off"),
            "frequency_always_one": _as_bool(freq_value, default=True),
            "frequency_switch_explicit": freq_value is not None,
        }

    def _state_file_path(self, cfg: Any = None) -> Path:
        """状态文件路径：相对路径一律落在数据目录内（越界 → 回退规范默认并告警）。

        工单要求：「state_file 等相对路径不得绕出数据目录」。绝对路径同样做包含校验
        （只允许数据目录内部），越界不写、不创建，改回 ``<data_dir>/life_state.json``。
        """
        # H 类（3.1.0 配置精简轮）：[life_state].state_file 退役为常量 STATE_FILE_NAME
        # （逐字 'life_state.json'，生产现值=默认已证实；改名即丢生产状态）。
        # cfg 参数保留签名兼容（调用方仍传配置），不再读取已删键。
        return resolve_state_file_path(self.data_dir, STATE_FILE_NAME)

    def _timezone_str(self, cfg: Any = None) -> str:
        cfg = self._current_config() if cfg is None else cfg
        return str(_field(cfg, "schedule_advanced", "timezone", "Asia/Shanghai")
                   or "Asia/Shanghai")

    def _persona(self) -> Dict[str, Any]:
        payload: Any = None
        if self._persona_provider is not None:
            try:
                payload = self._persona_provider()
            except Exception:
                payload = None
        name = ""
        personality = ""
        if isinstance(payload, Mapping):
            name = str(payload.get("name") or payload.get("lover_name") or "")
            personality = str(payload.get("personality") or payload.get("persona") or "")
        elif payload is not None:
            name = str(payload)
        if not name and self._plugin is not None:
            name = str(getattr(self._plugin, "_lover_name", "") or "")
        if not personality and self._plugin is not None:
            personality = str(getattr(self._plugin, "_personality", "") or "")
        return {"name": name, "fingerprint": _fingerprint(personality),
                "personality_length": len(personality)}

    def _versions(self) -> Dict[str, str]:
        if self._version_provider is not None:
            try:
                payload = self._version_provider()
                if isinstance(payload, Mapping):
                    return {
                        "plugin_version": str(payload.get("plugin_version") or ""),
                        "config_version": str(payload.get("config_version") or ""),
                    }
                return {"plugin_version": str(payload or ""), "config_version": ""}
            except Exception:
                pass
        plugin_root = getattr(self._plugin, "_plugin_root", None)
        root = Path(plugin_root) if plugin_root is not None else Path(__file__).resolve().parents[2]
        plugin_version = ""
        try:
            manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
            if isinstance(manifest, Mapping):
                plugin_version = str(manifest.get("version") or "")
        except (OSError, ValueError):
            plugin_version = ""
        cfg = self._current_config()
        config_version = str(_field(cfg, "plugin", "config_version", "") or "")
        return {"plugin_version": plugin_version, "config_version": config_version}

    def _emotion_object(self) -> Any:
        if self._emotion_initializer is not None:
            candidate = self._emotion_initializer
            if not _is_async_callable(candidate) and callable(
                    getattr(candidate, "initialize_from_persona", None)):
                return candidate
        obj = getattr(self._plugin, "_emotion", None) if self._plugin is not None else None
        if obj is None:
            obj = getattr(self._plugin, "emotion", None) if self._plugin is not None else None
        return obj

    def _event_service(self) -> Any:
        if self._event_catalog_provider is not None:
            try:
                return self._event_catalog_provider()
            except Exception:
                return None
        if self._event_catalog is not None:
            return self._event_catalog
        if self._plugin is not None:
            for attr in ("_event_catalog", "event_catalog"):
                obj = getattr(self._plugin, attr, None)
                if obj is not None:
                    return obj
            planning = getattr(self._plugin, "_planning", None)
            for attr in ("event_catalog", "events"):
                obj = getattr(planning, attr, None) if planning is not None else None
                if obj is not None:
                    return obj
        return None

    def _frequency_object(self) -> Any:
        if self._frequency_calibrator is not None:
            return self._frequency_calibrator
        if self._plugin is None:
            return None
        planning = getattr(self._plugin, "_planning", None)
        candidates = [planning,
                      getattr(planning, "proactive", None) if planning is not None else None,
                      getattr(self._plugin, "_proactive", None)]
        for candidate in candidates:
            if candidate is None:
                continue
            for name in ("calibrate_target_frequency", "ensure_target_frequency",
                         "apply_target_frequency"):
                if callable(getattr(candidate, name, None)):
                    return candidate
        return None

    # ------------------------------------------------------------------
    # 只读 DB 检查
    # ------------------------------------------------------------------
    def _database_path(self) -> Path:
        if self._db_path is not None:
            return self._db_path
        if self._db is not None:
            for attr in ("db_path", "_db_path", "path"):
                value = getattr(self._db, attr, None)
                if value:
                    try:
                        return Path(value)
                    except (TypeError, ValueError):
                        pass
        return self.data_dir / "schedule.db"

    def _schema_status(self) -> Dict[str, Any]:
        """只读检查 schedule.db 的 schema_version / sleep_sessions。"""
        if self._db is not None:
            for name in ("schema_status", "schema_info"):
                fn = getattr(self._db, name, None)
                if callable(fn):
                    try:
                        raw = fn()
                        if isinstance(raw, Mapping):
                            version = str(raw.get("version") or raw.get("schema_version") or "")
                            table = bool(raw.get("sleep_sessions"))
                            return {"exists": True, "version": version,
                                    "sleep_sessions": table, "error": ""}
                    except Exception as exc:
                        return {"exists": False, "version": "", "sleep_sessions": False,
                                "error": str(exc)}
        path = self._database_path()
        if not path.exists():
            return {"exists": False, "version": "", "sleep_sessions": False,
                    "error": f"数据库不存在：{path}"}
        conn = None
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=10.0)
            version = ""
            try:
                row = conn.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
                if row is not None and row[0] is not None:
                    version = str(row[0])
            except sqlite3.DatabaseError:
                version = ""
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='sleep_sessions'"
            ).fetchone()
            return {"exists": True, "version": version, "sleep_sessions": row is not None,
                    "error": ""}
        except (sqlite3.Error, OSError) as exc:
            return {"exists": False, "version": "", "sleep_sessions": False,
                    "error": str(exc)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass

    # ------------------------------------------------------------------
    # preflight
    # ------------------------------------------------------------------
    def _preflight(self, cfg: Any) -> StageResult:
        details: Dict[str, Any] = {"data_dir": str(self.data_dir)}
        if not self._plugin_enabled():
            return StageResult("preflight", "skipped", reason="plugin_disabled",
                               message="插件未启用（plugin.enabled=false），不执行初始化。",
                               details={**details, "disabled": True})
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return StageResult("preflight", "failed",
                               error_lines=(f"数据目录不可创建/不可写：{exc}",),
                               details=details)
        if not self.data_dir.is_dir():
            return StageResult("preflight", "failed",
                               error_lines=(f"数据目录不是目录：{self.data_dir}",),
                               details=details)
        if cfg is None:
            return StageResult("preflight", "failed",
                               error_lines=("配置不可用：无法读取开关与路径。",),
                               details=details)
        switches = self._switches(cfg)
        details["switches"] = switches
        db = self._schema_status()
        details["database"] = db
        errors: List[str] = []
        if not db.get("exists"):
            errors.append(f"数据库不可用：{db.get('error') or self._database_path()}")
        else:
            version = str(db.get("version") or "")
            try:
                version_num = int(version)
            except (TypeError, ValueError):
                version_num = 0
            if version_num < 6:
                errors.append(f"数据库 schema 版本不足（当前 {version or '未知'}，需要 v6）："
                              "请先让插件完成 v6 迁移后重试。")
        if errors:
            return StageResult("preflight", "failed", error_lines=tuple(errors),
                               details=details)
        return StageResult("preflight", "success", message="数据目录/DB v6/配置检查通过。",
                           details=details)

    # ------------------------------------------------------------------
    # 阶段 1：情绪
    # ------------------------------------------------------------------
    def _emotion_legacy_format(self) -> bool:
        """旧格式判定（用户裁定 2026-11-13）：params 标记 initialized=true 但 anchor/offset
        三轴（valence/arousal/dominance）无效（null/缺轴/非数）→ True，应判未初始化强制重 init。
        文件缺失/标记 false/格式有效 → False（交既有标记逻辑）。"""
        try:
            raw = json.loads((self.data_dir / "emotion_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        params = raw.get("params") if isinstance(raw, Mapping) else None
        if not isinstance(params, Mapping) or not bool(params.get("initialized", False)):
            return False
        for name in ("anchor", "offset"):
            vec = params.get(name)
            if not isinstance(vec, Mapping):
                return True
            for axis in ("valence", "arousal", "dominance"):
                v = vec.get(axis)
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    return True
        return False

    def _emotion_initialized(self) -> Optional[bool]:
        # 旧格式直接判未初始化（不信任旧 initialized 标记，强制重 init）
        if self._emotion_legacy_format():
            return False
        if self._emotion_status_provider is not None:
            try:
                return _coerce_initialized(self._emotion_status_provider())
            except Exception:
                return None
        obj = self._emotion_object()
        if obj is not None:
            for candidate in (obj, getattr(obj, "service", None)):
                if candidate is None:
                    continue
                fn = getattr(candidate, "status_snapshot", None)
                if callable(fn):
                    try:
                        return _coerce_initialized(fn())
                    except Exception:
                        continue
        path = self.data_dir / "emotion_config.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            params = raw.get("params") if isinstance(raw, Mapping) else None
            if isinstance(params, Mapping):
                return bool(params.get("initialized", False))
        except (OSError, ValueError):
            pass
        return False

    async def _stage_emotion(self, *, force: bool) -> StageResult:
        if not force:
            initialized = self._emotion_initialized()
            if initialized:
                return StageResult("emotion", "skipped", reason="already_initialized",
                                   message="情绪参数已初始化，跳过（幂等，不消耗模型）。",
                                   details={"already_initialized": True})
        initializer = self._emotion_initializer
        if initializer is not None and not callable(initializer):
            initializer = getattr(initializer, "initialize_from_persona", None)
        if initializer is None:
            obj = self._emotion_object()
            if obj is not None:
                initializer = getattr(obj, "initialize_from_persona", None)
        if not callable(initializer):
            return StageResult("emotion", "failed",
                               error_lines=("情绪服务未接线：缺少 initialize_from_persona()。",),
                               details={"forced": bool(force)})
        try:
            output = initializer()
            if inspect.isawaitable(output):
                output = await output
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StageResult("emotion", "failed",
                               error_lines=(f"情绪初始化异常：{exc}",),
                               details={"forced": bool(force)})
        outcome = _emotion_outcome(output)
        details = {"forced": bool(force), **outcome.get("details", {})}
        if not outcome["ok"]:
            return StageResult("emotion", "failed",
                               error_lines=tuple(outcome.get("errors") or ("情绪初始化失败。",)),
                               details=details)
        details["initialized_after"] = self._emotion_initialized()
        return StageResult("emotion", "success",
                           message=_first_line(outcome.get("message", "")) or "情绪参数已初始化。",
                           details=details)

    # ------------------------------------------------------------------
    # 阶段 2：事件库
    # ------------------------------------------------------------------
    async def _event_status(self, service: Any) -> Dict[str, Any]:
        if service is None:
            raise RuntimeError("事件库服务未接线（缺少 EventCatalogService）")
        fn = getattr(service, "status", None)
        if not callable(fn):
            raise RuntimeError("事件库服务缺少 status() 只读接口")
        raw = fn()
        if inspect.isawaitable(raw):
            raw = await raw
        return dict(raw) if isinstance(raw, Mapping) else {}

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _event_due(self, status: Mapping[str, Any], now: datetime) -> bool:
        try:
            generated = int(status.get("generated_events") or 0)
        except (TypeError, ValueError):
            generated = 0
        if generated <= 0:
            return True
        next_at = self._parse_ts(status.get("next_update_at"))
        if next_at is None:
            return False
        try:
            if next_at.tzinfo is None and now.tzinfo is not None:
                next_at = next_at.replace(tzinfo=now.tzinfo)
            return next_at <= now
        except TypeError:
            return False

    async def _stage_events(self, *, force: bool, now: datetime) -> StageResult:
        service = self._event_service()
        details: Dict[str, Any] = {"forced": bool(force)}
        try:
            status = await self._event_status(service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StageResult("events", "failed",
                               error_lines=(f"事件库状态读取失败：{exc}",), details=details)
        mode = str(status.get("mode") or _field(self._current_config(),
                                                "event_catalog", "mode", "off") or "off")
        details.update({
            "mode": mode,
            "catalog_path": status.get("path"),
            "generated_events": status.get("generated_events"),
            "old_catalog_version": status.get("catalog_version") or "",
            "old_next_update_at": status.get("next_update_at"),
        })
        if mode == "off":
            return StageResult("events", "skipped", reason="mode_off",
                               message="事件库模式为 off，跳过生成；旧库/内置池保持可用。",
                               details=details)
        if not force and not self._event_due(status, now):
            return StageResult("events", "skipped", reason="not_due",
                               message="事件库尚未到期，跳过（幂等，不消耗模型）。",
                               details=details)
        refresh = getattr(service, "refresh", None)
        if not callable(refresh):
            return StageResult("events", "failed",
                               error_lines=("事件库服务缺少 refresh(manual=...)。",),
                               details=details)
        try:
            raw = refresh(manual=True)
            if inspect.isawaitable(raw):
                raw = await raw
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StageResult("events", "failed",
                               error_lines=(f"事件库刷新异常（旧库保留）：{exc}",),
                               details={**details, "old_library_preserved": True})
        outcome = _event_outcome(raw)
        details.update(outcome.get("details", {}))
        details["old_library_preserved"] = not outcome["ok"]
        if outcome.get("status") == "skipped" and outcome.get("ok"):
            return StageResult("events", "skipped",
                               reason=str(outcome.get("reason") or "skipped"),
                               message=_first_line(outcome.get("message", "")) or "事件库刷新跳过。",
                               details=details)
        if not outcome["ok"]:
            return StageResult("events", "failed",
                               error_lines=tuple(outcome.get("errors") or ("事件库刷新失败。",)),
                               details=details)
        # 校验器需要刷新后的真实目录快照（条目数/版本/路径），不只旧值。
        try:
            refreshed = await self._event_status(service)
        except Exception:
            refreshed = {}
        if refreshed:
            details.update({
                "catalog_path": refreshed.get("path") or details.get("catalog_path"),
                "generated_events": refreshed.get("generated_events"),
                "catalog_version": refreshed.get("catalog_version") or "",
                "next_update_at": refreshed.get("next_update_at"),
                "degraded": bool(refreshed.get("degraded", False)),
            })
        return StageResult("events", "success",
                           message=_first_line(outcome.get("message", "")) or "事件库已刷新。",
                           details=details)

    # ------------------------------------------------------------------
    # 阶段 3：状态 / 睡眠
    # ------------------------------------------------------------------
    async def _ensure_sleep_sessions(self) -> Dict[str, Any]:
        if self._sleep_sessions_ensurer is not None:
            try:
                raw = self._sleep_sessions_ensurer()
                if inspect.isawaitable(raw):
                    raw = await raw
                if isinstance(raw, Mapping):
                    return dict(raw)
                return {"ok": bool(raw)}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        db = self._db
        for name in ("ensure_sleep_sessions", "ensure_schema"):
            fn = getattr(db, name, None) if db is not None else None
            if callable(fn):
                try:
                    raw = fn()
                    if inspect.isawaitable(raw):
                        raw = await raw
                    if raw is False:
                        return {"ok": False, "error": f"{name}() 返回 False"}
                    status = self._schema_status()
                    return {"ok": bool(status.get("sleep_sessions")),
                            "action": "ensured", "schema": status}
                except Exception as exc:
                    return {"ok": False, "error": str(exc)}
        status = self._schema_status()
        if status.get("sleep_sessions"):
            return {"ok": True, "action": "exists", "schema": status}
        if not status.get("exists"):
            return {"ok": False, "error": status.get("error") or "数据库不可用"}
        # v6 已由 preflight 保证；这里用 ScheduleDB 的公开构造重跑幂等迁移
        # （CREATE TABLE IF NOT EXISTS），避免手抄 sleep_sessions DDL。
        try:
            from planning.schedule_db import ScheduleDB
            handle = ScheduleDB(self._database_path())
            try:
                handle.close()
            except Exception:
                pass
            status = self._schema_status()
            return {"ok": bool(status.get("sleep_sessions")),
                    "action": "created", "schema": status}
        except Exception as exc:
            return {"ok": False, "error": f"sleep_sessions 创建失败：{exc}"}

    def _prepare_state_file(self, path: Path, cfg: Any) -> Dict[str, Any]:
        """确保 life_state.json 存在且只补缺失键；损坏则**先备份再按规范默认重建**。

        - 已有有效状态 → 只补缺失键，已有值一律不覆盖、不改任何开关；
        - 文件损坏（不可读/非对象/字段级全坏）→ 备份 ``<name>.corrupt-<ts>.bak`` 后
          以 :func:`planning.life_state.default_state_document` 重建（定稿口径）；
        - **未知未来版本** → 不猜着覆盖，保留原文件并失败上报（真实歧义交人工）。
        """
        result: Dict[str, Any] = {"path": str(path), "created": False,
                                  "fields_added": [], "existing_values_preserved": True}
        if not path.exists():
            try:
                from planning.life_state import LifeStateService
                service = LifeStateService(path, timezone_str=self._timezone_str(cfg))
                if not service.save():
                    result.update({"ok": False, "error": "状态文件写入失败（原子写返回 False）"})
                    return result
            except Exception as exc:
                result.update({"ok": False, "error": f"状态文件创建失败：{exc}"})
                return result
            result["created"] = True
            result["ok"] = True
            result["fields_added"] = list(_state_defaults().keys())
            return result
        raw: Any = None
        corrupt_reason = ""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            corrupt_reason = f"不可读/损坏：{exc}"
        if raw is not None and not isinstance(raw, dict):
            corrupt_reason = "顶层不是对象"
        if corrupt_reason:
            backup = self._backup_corrupt_state_file(path)
            if not save_json_atomic(path, _state_defaults()):
                result.update({"ok": False,
                               "error": f"状态文件损坏（{corrupt_reason}）且默认文档写入失败；"
                                        f"原文件备份={backup or '失败'}"})
                return result
            result.update({"ok": True, "recovered": True, "backup": str(backup or ""),
                           "fields_added": list(_state_defaults().keys()),
                           "existing_values_preserved": False,
                           "warning": f"状态文件损坏（{corrupt_reason}）已备份并按规范默认重建"})
            return result
        version = raw.get("version")
        if version is not None and version != STATE_VERSION:
            # 未知（未来）版本：不猜着覆盖 —— 保留原字节，明确失败上报。
            result.update({"ok": False,
                           "error": f"状态文件 version 不认识（期望 {STATE_VERSION}，实得 "
                                    f"{version!r}）；为免覆盖新版本数据，本次不重建，请人工确认后删除或迁移。"})
            return result
        defaults = _state_defaults()
        added: List[str] = []
        for key, value in defaults.items():
            if key not in raw:
                raw[key] = value
                added.append(key)
        if added:
            if not save_json_atomic(path, raw):
                result.update({"ok": False, "error": "状态文件补字段写入失败（原文件保留）"})
                return result
        result.update({"ok": True, "fields_added": added,
                       "existing_values_preserved": True})
        return result

    @staticmethod
    def _backup_corrupt_state_file(path: Path) -> Optional[Path]:
        """损坏状态文件副本（``<name>.corrupt-<YYYYmmdd-HHMMSS>.bak``；失败 → None）。"""
        try:
            import shutil
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = path.with_name(f"{path.name}.corrupt-{stamp}.bak")
            shutil.copy2(path, target)
            logger.warning("状态文件损坏，已保留备份：%s", target)
            return target
        except Exception as exc:  # pragma: no cover - 备份失败仍继续重建
            logger.warning("状态文件备份失败（仍按默认重建）：%s", exc)
            return None

    async def _stage_state_sleep(self, cfg: Any) -> StageResult:
        switches = self._switches(cfg)
        details: Dict[str, Any] = {
            "life_state_enabled": switches["life_state_enabled"],
            "sleep_engine_enabled": switches["sleep_engine_enabled"],
            "auto_enabled_new_module": False,
        }
        # PR8 3.0.0：旧「单开 sleep_engine_enabled 提示需同开生活状态」分支已删
        # （两退役开关并入 schedule.enabled 唯一真源；依据 = 用户 2026-10-27 授权
        # 直接清理 + 逐符号审定清单 WP1）。
        path = self._state_file_path(cfg)
        details["state_file"] = str(path)
        details["db_path"] = str(self._database_path())
        try:
            if self._state_preparer is not None:
                raw = self._state_preparer(path, cfg)
                if inspect.isawaitable(raw):
                    raw = await raw
                state_info = dict(raw) if isinstance(raw, Mapping) else {"ok": bool(raw)}
            else:
                state_info = self._prepare_state_file(path, cfg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StageResult("state_sleep", "failed",
                               error_lines=(f"状态文件准备异常：{exc}",), details=details)
        details["state"] = state_info
        if not state_info.get("ok", False):
            return StageResult("state_sleep", "failed",
                               error_lines=(str(state_info.get("error") or "状态文件准备失败。"),),
                               details=details)
        try:
            sleep_info = await self._ensure_sleep_sessions()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            sleep_info = {"ok": False, "error": str(exc)}
        details["sleep_sessions"] = sleep_info
        if not sleep_info.get("ok"):
            return StageResult("state_sleep", "failed",
                               error_lines=(f"sleep_sessions 表不可用："
                                            f"{sleep_info.get('error') or '未知原因'}",),
                               details=details)
        if state_info.get("created"):
            message = "已创建默认 life_state.json 与 sleep_sessions（未改动任何开关）。"
        elif state_info.get("fields_added"):
            message = ("已补齐 life_state.json 缺失字段（已有值未覆盖）与 sleep_sessions"
                       "（未改动任何开关）。")
        else:
            message = "life_state.json 与 sleep_sessions 已就绪（已有状态未覆盖）。"
        # PR8 3.0.0：sleep_engine_not_effective 提示随单开分支一并退役（睡眠引擎随
        # [schedule].enabled 生效；依据 = 用户 2026-10-27 授权直接清理）。
        return StageResult("state_sleep", "success", message=message, details=details)

    # ------------------------------------------------------------------
    # 阶段 4：目标频率
    # ------------------------------------------------------------------
    async def _stage_frequency(self, cfg: Any) -> StageResult:
        switches = self._switches(cfg)
        details: Dict[str, Any] = {
            "switch": switches["frequency_always_one"],
            "switch_explicit": switches["frequency_switch_explicit"],
        }
        if not switches["frequency_always_one"]:
            return StageResult("frequency", "skipped", reason="switch_off",
                               message="target_frequency_always_one=false，保留旧频率行为。",
                               details=details)
        target = self._frequency_object()
        if target is None:
            return StageResult("frequency", "failed",
                               error_lines=("目标频率校准口未接线：需要 "
                                            "proactive.calibrate_target_frequency() 或注入 "
                                            "frequency_calibrator。",),
                               details=details)
        details["calibrator"] = type(target).__name__
        candidate = target
        if not callable(target):
            candidate = None
            for name in ("calibrate_target_frequency", "ensure_target_frequency",
                         "apply_target_frequency"):
                fn = getattr(target, name, None)
                if callable(fn):
                    candidate = fn
                    break
        if not callable(candidate):
            return StageResult("frequency", "failed",
                               error_lines=("目标频率校准口不可调用。",), details=details)
        try:
            raw = candidate()
            if inspect.isawaitable(raw):
                raw = await raw
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StageResult("frequency", "failed",
                               error_lines=(f"目标频率校准异常：{exc}",), details=details)
        outcome = _frequency_outcome(raw)
        details.update(outcome.get("details", {}))
        if outcome.get("status") == "skipped":
            return StageResult("frequency", "skipped",
                               reason=str(outcome.get("reason") or "skipped"),
                               message=_first_line(outcome.get("message", "")) or "目标频率校准跳过。",
                               details=details)
        if not outcome["ok"]:
            return StageResult("frequency", "failed",
                               error_lines=tuple(outcome.get("errors") or ("目标频率校准失败。",)),
                               details=details)
        return StageResult("frequency", "success",
                           message=_first_line(outcome.get("message", ""))
                           or "目标频率已校准为 1.0。",
                           details=details)

    # ------------------------------------------------------------------
    # 编排 / 汇总
    # ------------------------------------------------------------------
    def _catalog_view(self) -> Dict[str, Any]:
        service = self._event_service()
        if service is None:
            return {}
        try:
            raw = service.status()
            if inspect.isawaitable(raw):
                return {}
            return dict(raw) if isinstance(raw, Mapping) else {}
        except Exception:
            return {}

    def _overall(self, stages: Sequence[StageResult]) -> str:
        business = [s for s in stages if s.stage in STAGE_ORDER]
        if not business:
            return "failed"
        if any(s.status == "cancelled" for s in business):
            return "cancelled"
        failed = [s for s in business if s.status == "failed"]
        if failed:
            return "partial" if any(s.ok for s in business) else "failed"
        if all(s.status == "skipped" for s in business):
            return "skipped"
        return "success"

    def _result(self, *, command: str, manual: bool, status: str,
                stages: Sequence[StageResult], started: str, finished: str,
                catalog: Mapping[str, Any], persona: Mapping[str, Any],
                versions: Mapping[str, str], disabled: bool = False) -> InitResult:
        events = next((s for s in stages if s.stage == "events"), None)
        details = events.details if events is not None else {}
        next_update = (catalog.get("next_update_at")
                       or details.get("next_update_at")
                       or details.get("old_next_update_at"))
        catalog_version = (catalog.get("catalog_version")
                           or details.get("catalog_version")
                           or details.get("old_catalog_version") or "")
        return InitResult(
            command=command, manual=bool(manual), status=status, stages=tuple(stages),
            started_at=started, finished_at=finished,
            persona_name=str(persona.get("name") or ""),
            persona_fingerprint=str(persona.get("fingerprint") or ""),
            plugin_version=str(versions.get("plugin_version") or ""),
            config_version=str(versions.get("config_version") or ""),
            catalog_version=str(catalog_version or ""),
            next_update_at=next_update, disabled=bool(disabled))

    def _build_plan(self, command: str, *, field: Optional[str], manual: bool,
                    cfg: Any, emotion_force: bool, events_force: bool,
                    force: bool) -> List[Tuple[str, Callable[[], Any]]]:
        """按命令或单字段构造执行计划（顺序 = ``STAGE_ORDER``）。"""
        plan: List[Tuple[str, Callable[[], Any]]] = []

        def add_emotion() -> None:
            plan.append(("emotion", lambda: self._stage_emotion(force=emotion_force)))

        def add_events() -> None:
            plan.append(("events", lambda: self._stage_events(
                force=events_force, now=self._now())))

        def add_state_sleep() -> None:
            plan.append(("state_sleep", lambda: self._stage_state_sleep(cfg)))

        def add_frequency() -> None:
            plan.append(("frequency", lambda: self._stage_frequency(cfg)))

        def add_video_interest() -> None:
            video_initializer = getattr(self._plugin, "_initialize_video_interest", None)
            if not callable(video_initializer):
                return

            async def video_stage() -> StageResult:
                result = await video_initializer()
                state = result.get("status") if isinstance(result, dict) else "unavailable"
                keywords = result.get("keywords") if isinstance(result, dict) else ()
                return StageResult(
                    "video_interest",
                    "success" if state == "fresh"
                    else "skipped" if state in ("disabled", "cached")
                    else "failed",
                    message=(f"已按人设重置视频偏好（{len(keywords or [])} 个关键词）"
                             if state == "fresh" else "视频模块关闭，跳过偏好初始化"
                             if state == "disabled" else "沿用未到期视频偏好"
                             if state == "cached" else "偏好生成失败，旧偏好已失效"),
                    reason=str(state), details={"source": "persona_init"})

            plan.append(("video_interest", video_stage))

        def add_disposition() -> None:
            initializer = getattr(self._plugin, "_initialize_companion_disposition", None)
            if not callable(initializer):
                return

            async def disposition_stage() -> StageResult:
                profile = await initializer(force=bool(force if field else manual))
                generated = profile.get("source") == "persona_llm"
                return StageResult(
                    "companion_disposition", "success" if generated else "skipped",
                    message="已生成人设联系倾向" if generated else "采用中性联系倾向",
                    details={"source": profile.get("source"),
                             "anchor_hours": profile.get("anchor_hours"),
                             "loneliness": profile.get("loneliness")})

            plan.append(("companion_disposition", disposition_stage))

        def add_preferences() -> None:
            # P3（2026-11-09 §14.8）：日程偏好阶段——可独立标记成功/降级/失败；
            # 默认命中缓存，明确重建（manual/字段显式）才换本人人设画像。
            pref_initializer = getattr(self._plugin, "_initialize_activity_preferences", None)
            if not callable(pref_initializer):
                return

            async def preferences_stage() -> StageResult:
                result = await pref_initializer(force=bool(force if field else manual))
                state = str(result.get("status") or "unavailable")
                ok_states = ("ok", "partial", "cached")
                stage_state = ("success" if state in ok_states
                               else "skipped" if state in ("disabled", "no_llm", "deferred_busy")
                               else "failed")
                return StageResult(
                    "activity_preferences", stage_state,
                    message=(f"日程偏好就绪（评估 {result.get('evaluated', 0)} 项）"
                             if stage_state == "success"
                             else "日程偏好未启用或暂缓（中性降级，不阻塞日程）"
                             if stage_state == "skipped"
                             else "日程偏好评估失败（中性降级，旧有效画像保留）"),
                    reason=state,
                    details={"evaluated": result.get("evaluated", 0),
                             "traits": result.get("traits", False),
                             "tokens": result.get("tokens", 0)})

            plan.append(("activity_preferences", preferences_stage))

        builders = {
            "emotion": add_emotion,
            "events": add_events,
            "state_sleep": add_state_sleep,
            "frequency": add_frequency,
            "video_interest": add_video_interest,
            "companion_disposition": add_disposition,
            "activity_preferences": add_preferences,
        }
        if field is not None:
            builders[field]()
            return plan
        if command in ("full", "emotion"):
            add_emotion()
        if command in ("full", "events"):
            add_events()
        if command == "full":
            add_state_sleep()
            add_frequency()
            # 全量命令保留既有语义：仅人工全量重置视频偏好；自动驱动走 run_stage
            # 逐字段执行（``field`` 分支），不受此限制。
            if manual:
                add_video_interest()
            add_disposition()
            add_preferences()
        return plan

    async def _execute(self, command: str, *, manual: bool,
                       emotion_force: bool, events_force: bool,
                       field: Optional[str] = None,
                       force: bool = False) -> InitResult:
        if field is not None and field not in STAGE_ORDER:
            raise ValueError(f"未知初始化字段：{field}")
        async with self._lock:
            started_dt = self._now()
            started = started_dt.isoformat(timespec="seconds")
            cfg = self._current_config()
            persona = self._persona()
            versions = self._versions()
            stages: List[StageResult] = []
            preflight = self._preflight(cfg)
            stages.append(preflight)
            if preflight.details.get("disabled"):
                finished = self._now().isoformat(timespec="seconds")
                return self._result(command=command, manual=manual, status="disabled",
                                    stages=stages, started=started, finished=finished,
                                    catalog={}, persona=persona, versions=versions,
                                    disabled=True)
            if not preflight.ok:
                finished = self._now().isoformat(timespec="seconds")
                return self._result(command=command, manual=manual, status="failed",
                                    stages=stages, started=started, finished=finished,
                                    catalog={}, persona=persona, versions=versions)

            plan = self._build_plan(command, field=field, manual=manual, cfg=cfg,
                                    emotion_force=emotion_force,
                                    events_force=events_force, force=force)
            for name, factory in plan:
                try:
                    stage = await factory()
                except asyncio.CancelledError:
                    stages.append(StageResult(
                        name, "cancelled", reason="cancelled",
                        message="执行被取消，本阶段未记作完成；可重新执行。",
                        details={"cancelled": True}))
                    raise
                except Exception as exc:
                    logger.warning("init_service: 阶段 %s 未捕获异常（继续后续阶段）：%s",
                                   name, exc)
                    stage = StageResult(name, "failed",
                                        error_lines=(f"阶段执行异常：{exc}",),
                                        details={"exception": type(exc).__name__})
                stages.append(stage)

            catalog = self._catalog_view()
            status = self._overall(stages)
            finished = self._now().isoformat(timespec="seconds")
            return self._result(command=command, manual=manual, status=status,
                                stages=stages, started=started, finished=finished,
                                catalog=catalog, persona=persona, versions=versions)

    # ------------------------------------------------------------------
    # 公开入口
    # ------------------------------------------------------------------
    async def run_full(self, *, manual: bool = True) -> InitResult:
        """全量：七字段（既有幂等语义）+ 人工全量的视频偏好重置。"""
        return await self._execute("full", manual=manual, emotion_force=False,
                                   events_force=False)

    async def run_emotion(self, *, manual: bool = True, force: bool = True) -> InitResult:
        """只跑情绪参数。默认 ``force=True`` = 旧 ``/lbm_emotion_init`` 语义。"""
        return await self._execute("emotion", manual=manual, emotion_force=force,
                                   events_force=False)

    async def run_events(self, *, manual: bool = True, force: bool = True) -> InitResult:
        """只跑事件库；默认强制刷新（定稿 ``/lbm ops init events``）。"""
        return await self._execute("events", manual=manual, emotion_force=False,
                                   events_force=force)

    async def run_stage(self, stage: str, *, manual: bool = False,
                        force: bool = False) -> InitResult:
        """只跑一个字段（自动驱动/字段命令共用；与 full 共用同一实例锁）。"""
        if stage not in STAGE_ORDER:
            raise ValueError(f"未知初始化字段：{stage}")
        return await self._execute(f"stage:{stage}", manual=manual,
                                   emotion_force=force, events_force=force,
                                   field=stage, force=force)

    # ------------------------------------------------------------------
    # 公开只读口（自动驱动 / 命令共用）
    # ------------------------------------------------------------------
    def preflight(self) -> StageResult:
        """preflight（plugin.enabled / data_dir / DB v6 / 配置）只读检查。"""
        return self._preflight(self._current_config())

    def versions(self) -> Dict[str, str]:
        """当前插件/配置版本（登记文件写回用）。"""
        return self._versions()

    def persona(self) -> Dict[str, Any]:
        """当前人设摘要（name/fingerprint/personality_length）。"""
        return self._persona()

    def config(self) -> Any:
        """当前配置对象（热重载友好；只读消费）。"""
        return self._current_config()

    # ------------------------------------------------------------------
    # 只读 status（无副作用：不建目录、不写文件、不 reload、不调用模型）
    # ------------------------------------------------------------------
    def status(self) -> InitStatus:
        cfg = self._current_config()
        enabled = self._plugin_enabled()
        registry = self.registry.snapshot()
        catalog = self._catalog_view()
        switches = self._switches(cfg)
        state_path = self._state_file_path(cfg)
        emotion_initialized = bool(self._emotion_initialized())
        db = self._schema_status()
        return InitStatus(
            plugin_enabled=enabled,
            registry_path=str(registry.get("path") or self.registry.path),
            registry_plugin_version=str(registry.get("plugin_version") or ""),
            fields=dict(registry.get("fields") or {}),
            preflight=dict(registry.get("preflight") or {}),
            next_update_at=catalog.get("next_update_at"),
            catalog_version=str(catalog.get("catalog_version") or ""),
            event_mode=str(catalog.get("mode") or switches["event_catalog_mode"]),
            event_degraded=bool(catalog.get("degraded", False)),
            event_reason=str(catalog.get("reason") or ""),
            switches=switches,
            emotion_initialized=emotion_initialized,
            life_state_file_exists=state_path.exists(),
            life_state_file=str(state_path),
            db_version=str(db.get("version") or ""),
            sleep_sessions=bool(db.get("sleep_sessions")),
        )

    def status_text(self) -> str:
        return self.status().status_text


# ----------------------------------------------------------------------
# 返回值适配（频率）
# ----------------------------------------------------------------------
def _frequency_outcome(raw: Any) -> Dict[str, Any]:
    if raw is None or raw is True:
        return {"ok": True, "message": "目标频率已校准为 1.0。", "details": {}}
    if raw is False:
        return {"ok": False, "message": "", "details": {},
                "errors": ["目标频率校准返回 False。"]}
    if isinstance(raw, Mapping):
        status = str(raw.get("status") or "").lower()
        target_ok = raw.get("target_resolved", True)
        ok = raw.get("ok")
        if ok is None:
            ok = target_ok and status not in ("failed", "error")
        errors = raw.get("error_lines") or raw.get("errors") or ()
        if isinstance(errors, str):
            errors = [errors]
        if not errors and raw.get("error"):
            errors = [str(raw.get("error"))]
        if target_ok is False:
            readable = ("目标私聊会话尚未解析，无法校准目标频率"
                        "（其他初始化阶段不受影响）。")
            errors = [readable] + [str(x) for x in errors if str(x) != readable]
        details = {k: v for k, v in raw.items()
                   if k not in ("ok", "message", "text", "errors", "error_lines", "error")}
        return {
            "ok": bool(ok and target_ok is not False),
            "status": status,
            "reason": str(raw.get("reason") or ""),
            "message": str(raw.get("message") or raw.get("text") or ""),
            "errors": [str(x) for x in errors],
            "details": details,
        }
    if isinstance(raw, str):
        return {"ok": True, "message": raw, "details": {"output": raw[:200]}}
    ok = bool(getattr(raw, "ok", True))
    errors = getattr(raw, "error_lines", None) or getattr(raw, "errors", None) or ()
    if isinstance(errors, str):
        errors = [errors]
    details = {}
    for key in ("target_stream", "target_resolved", "factor", "applied", "switched",
                "reason", "message"):
        value = getattr(raw, key, None)
        if value is not None:
            details[key] = value
    target_ok = getattr(raw, "target_resolved", True)
    if target_ok is False:
        readable = ("目标私聊会话尚未解析，无法校准目标频率"
                    "（其他初始化阶段不受影响）。")
        errors = [readable] + [str(x) for x in errors if str(x) != readable]
    return {"ok": ok and target_ok is not False,
            "status": str(getattr(raw, "status", "") or "").lower(),
            "reason": str(getattr(raw, "reason", "") or ""),
            "message": str(getattr(raw, "message", "") or ""),
            "errors": [str(x) for x in errors],
            "details": details}


__all__ = [
    "STAGE_ORDER", "StageResult", "InitResult", "InitStatus", "InitService",
]





