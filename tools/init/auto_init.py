# -*- coding: utf-8 -*-
"""加载自动初始化驱动（``init_registry.json`` + ``manifest.py`` + ``InitService``）。

流程（方案 §四 / §九）
======================
1. 读登记文件（不存在 → 全 false；格式不符 → 直接重写，不回填旧 ``life_init.json``）。
2. 算需 init 字段：``initialized==false`` ∪ 版本重 init 区间并集
   （``(旧版本, 当前版本]`` 内各版本 ``VERSION_REINIT`` 字段）。
3. 按 :data:`tools.init.manifest.INIT_FIELDS` 顺序逐字段
   ``InitService.run_stage`` → 独立校验器（``ValidationResult``）→ 成功且校验通过
   才 ``mark_true``，否则 ``mark_false``（attempts+1 + last_failed_at + last_error）。
4. 写回 ``plugin_version``；失败字段下次加载自动重 init（崩溃恢复）。

退避
====
``attempts<3`` 每次加载正常重试；``>=3`` 仅当距 ``last_failed_at`` 超过
1h→6h→24h（封顶）窗口才重试。**手动命令不受退避限制**（强制重试）。

单飞
====
模块级 ``(data_dir, event_loop)`` 锁：同一数据目录的自动/手动初始化串行；
业务层 ``InitService._lock`` 再保证与旧 ``run_full/run_emotion/run_events`` 互斥。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .manifest import (INIT_FIELDS, VALIDATORS, ValidationContext, ValidationResult,
                       backoff_seconds, reinit_fields_for_upgrade)
from .registry import InitRegistry, parse_timestamp

logger = logging.getLogger(__name__)

#: 单飞锁（key = (data_dir, id(loop))；测试/热重载各自 loop 不串）
_RUN_LOCKS: Dict[Tuple[str, int], asyncio.Lock] = {}

#: 字段中文名（命令/日志展示）
FIELD_LABELS: Dict[str, str] = {
    "emotion": "情绪参数",
    "events": "事件库",
    "state_sleep": "生活状态/睡眠",
    "frequency": "目标频率",
    "video_interest": "视频兴趣",
    "companion_disposition": "联系倾向",
    "activity_preferences": "日程偏好",
}


def field_label(name: str) -> str:
    return FIELD_LABELS.get(str(name), str(name))


def _lock_for(data_dir: Path) -> asyncio.Lock:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    key = (str(data_dir), id(loop))
    lock = _RUN_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _RUN_LOCKS[key] = lock
    return lock


def _plugin_data_dir(plugin: Any) -> Optional[Path]:
    raw = getattr(plugin, "_data_dir", None)
    if raw is None:
        ctx = getattr(plugin, "ctx", None)
        paths = getattr(ctx, "paths", None)
        raw = getattr(paths, "data_dir", None)
    return Path(raw) if raw else None


def _short_ts(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]


def retry_allowed(entry: Mapping[str, Any], now: datetime) -> bool:
    """退避判定：attempts<3 恒可重试；否则距 last_failed_at 超过窗口。"""
    attempts = int(entry.get("attempts") or 0)
    window = backoff_seconds(attempts)
    if window <= 0:
        return True
    last = parse_timestamp(entry.get("last_failed_at"))
    if last is None:
        return True
    if last.tzinfo is None and now.tzinfo is not None:
        last = last.replace(tzinfo=now.tzinfo)
    try:
        return (now - last).total_seconds() >= window
    except TypeError:
        return True


def next_retry_at(entry: Mapping[str, Any]) -> Optional[datetime]:
    attempts = int(entry.get("attempts") or 0)
    window = backoff_seconds(attempts)
    last = parse_timestamp(entry.get("last_failed_at"))
    if window <= 0 or last is None:
        return None
    return last + timedelta(seconds=window)


def compute_need(registry_document: Mapping[str, Any], current_version: str) -> List[str]:
    """需 init 字段 = 未初始化 ∪ 版本重 init 区间并集（保持 STAGE_ORDER）。"""
    fields = registry_document.get("fields") or {}
    need = {name for name in INIT_FIELDS
            if not bool((fields.get(name) or {}).get("initialized"))}
    need.update(reinit_fields_for_upgrade(
        registry_document.get("plugin_version"), current_version))
    return [name for name in INIT_FIELDS if name in need]


@dataclass(frozen=True)
class FieldOutcome:
    field: str
    status: str                       # success / failed / backoff / blocked
    stage_status: str = ""
    message: str = ""
    errors: Tuple[str, ...] = ()
    validated: bool = False
    attempts: int = 0
    next_retry_at: Optional[str] = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success"


@dataclass(frozen=True)
class AutoInitReport:
    manual: bool
    plugin_version: str
    registry_path: str
    preflight: Mapping[str, Any]
    need: Tuple[str, ...]
    outcomes: Tuple[FieldOutcome, ...]
    disabled: bool = False

    @property
    def ok(self) -> bool:
        if self.disabled:
            return False
        if (self.preflight or {}).get("ok") is False:
            return False
        return all(outcome.ok or outcome.status == "backoff" for outcome in self.outcomes)

    @property
    def succeeded(self) -> Tuple[FieldOutcome, ...]:
        return tuple(item for item in self.outcomes if item.ok)

    @property
    def failed(self) -> Tuple[FieldOutcome, ...]:
        return tuple(item for item in self.outcomes if item.status == "failed")

    @property
    def lines(self) -> List[str]:
        prefix = "手动初始化" if self.manual else "自动初始化"
        if self.disabled:
            return [f"{prefix}未执行：插件未启用（plugin.enabled=false）。"]
        preflight_ok = (self.preflight or {}).get("ok")
        if preflight_ok is False:
            reason = str((self.preflight or {}).get("reason") or "未知原因")
            return [f"{prefix}未执行：preflight 失败（{reason}）。"]
        if not self.outcomes:
            if not self.need:
                return [f"{prefix}：所有字段已初始化且无需版本重 init（幂等跳过）。"]
            return [f"{prefix}：无可执行字段。"]
        lines = [f"{prefix}：成功 {len(self.succeeded)}/{len(self.outcomes)} 个字段"
                 + (f"（版本 {self.plugin_version}）" if self.plugin_version else "")]
        for outcome in self.outcomes:
            label = field_label(outcome.field)
            if outcome.status == "success":
                lines.append(f"[成功] {label}：{outcome.message or '已初始化并通过校验'}")
            elif outcome.status == "backoff":
                until = f"；最早 {_short_ts(outcome.next_retry_at)} 再试" if outcome.next_retry_at else ""
                lines.append(f"[退避] {label}：连续失败 {outcome.attempts} 次{until}（手动可强制）")
            elif outcome.status == "blocked":
                lines.append(f"[跳过] {label}：{outcome.message or '无需初始化'}")
            else:
                lines.append(f"[失败] {label}：{outcome.message or '初始化失败'}")
                for error in outcome.errors:
                    lines.append(f"  - {error}")
        lines.append(f"登记文件：{self.registry_path}")
        return lines

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "manual": bool(self.manual),
            "plugin_version": self.plugin_version,
            "registry_path": self.registry_path,
            "preflight": dict(self.preflight or {}),
            "disabled": bool(self.disabled),
            "need": list(self.need),
            "outcomes": [
                {"field": item.field, "status": item.status,
                 "stage_status": item.stage_status, "message": item.message,
                 "errors": list(item.errors), "validated": bool(item.validated),
                 "attempts": int(item.attempts), "next_retry_at": item.next_retry_at,
                 "details": dict(item.details)}
                for item in self.outcomes
            ],
        }


class AutoInitDriver:
    """读登记 → 逐字段 init+校验+标记（自动与手动共用的唯一驱动）。"""

    def __init__(self, plugin: Any, *, service: Any = None,
                 registry: Optional[InitRegistry] = None,
                 now_provider: Any = None) -> None:
        data_dir = _plugin_data_dir(plugin)
        if data_dir is None:
            raise RuntimeError("自动初始化需要 data_dir（plugin._data_dir / ctx.paths.data_dir）")
        self.plugin = plugin
        self.data_dir = data_dir
        self.registry = registry if registry is not None else InitRegistry(data_dir)
        if service is None:
            service = getattr(getattr(plugin, "_planning", None), "init", None)
        if service is None:
            raise RuntimeError("初始化服务未装配（plugin._planning.init 不存在）")
        self.service = service
        self._now_provider = now_provider
        self._log = getattr(getattr(plugin, "ctx", None), "logger", logger)

    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        if self._now_provider is not None:
            value = self._now_provider()
            if isinstance(value, datetime):
                return value
        return datetime.now().astimezone()

    def _current_version(self) -> str:
        try:
            versions = self.service.versions()
            return str((versions or {}).get("plugin_version") or "")
        except Exception:
            return ""

    def _validation_context(self, stage: Any, version: str) -> ValidationContext:
        config = None
        try:
            config = self.service.config()
        except Exception:
            config = getattr(self.plugin, "config", None)
        root = getattr(self.plugin, "_plugin_root", None)
        return ValidationContext(
            data_dir=self.data_dir,
            stage=stage,
            config=config,
            plugin_root=Path(root) if root else None,
            personality=str(getattr(self.plugin, "_personality", "") or ""),
            lover_name=str(getattr(self.plugin, "_lover_name", "") or "麦麦"),
            plugin_version=version,
            now=self._now(),
        )

    # ------------------------------------------------------------------
    async def run(self, *, manual: bool = False,
                  fields: Optional[Sequence[str]] = None,
                  force: Optional[bool] = None) -> AutoInitReport:
        """执行一次初始化；单飞锁覆盖整个读-算-跑-标记链路。"""
        if fields:
            unknown = [name for name in fields if name not in INIT_FIELDS]
            if unknown:
                raise ValueError(f"未知初始化字段：{', '.join(unknown)}")
        async with _lock_for(self.data_dir):
            return await self._run_locked(manual=manual, fields=fields, force=force)

    async def _run_locked(self, *, manual: bool, fields: Optional[Sequence[str]],
                          force: Optional[bool]) -> AutoInitReport:
        version = self._current_version()
        preflight = self.service.preflight()
        preflight_details = getattr(preflight, "details", None)
        preflight_details = preflight_details if isinstance(preflight_details, Mapping) else {}
        disabled = bool(preflight_details.get("disabled"))
        if disabled:
            self.registry.set_preflight(None, reason="plugin_disabled", disabled=True)
            return AutoInitReport(
                manual=manual, plugin_version=version,
                registry_path=str(self.registry.path),
                preflight=dict(self.registry.snapshot().get("preflight") or {}),
                need=(), outcomes=(), disabled=True)
        if not preflight.ok:
            reason = "；".join(str(line) for line in getattr(preflight, "error_lines", ()) or ())
            reason = reason or str(getattr(preflight, "message", "") or "preflight 失败")
            self.registry.set_preflight(False, reason=reason[:500])
            return AutoInitReport(
                manual=manual, plugin_version=version,
                registry_path=str(self.registry.path),
                preflight=dict(self.registry.snapshot().get("preflight") or {}),
                need=(), outcomes=())
        self.registry.set_preflight(True)

        document = self.registry.load()
        if fields:
            requested = [name for name in INIT_FIELDS if name in set(fields)]
            version_fields: set = set()
        else:
            requested = compute_need(document, version)
            version_fields = reinit_fields_for_upgrade(
                document.get("plugin_version"), version)
        outcomes: List[FieldOutcome] = []
        for name in requested:
            outcomes.append(await self._run_field(
                name, manual=manual, force=force,
                version=version, version_fields=version_fields))
        # 处理完字段后写回版本（preflight 失败不写，避免丢掉版本重 init 信号）。
        try:
            self.registry.set_plugin_version(version)
        except Exception as exc:
            self._log.warning("init_registry 写回版本失败（忽略）：%s", exc)
        return AutoInitReport(
            manual=manual, plugin_version=version,
            registry_path=str(self.registry.path),
            preflight=dict(self.registry.snapshot().get("preflight") or {}),
            need=tuple(requested), outcomes=tuple(outcomes))

    async def _run_field(self, name: str, *, manual: bool, force: Optional[bool],
                         version: str, version_fields: set) -> FieldOutcome:
        entry = self.registry.field_state(name)
        forced_by_version = name in version_fields and bool(entry.get("initialized"))
        if forced_by_version:
            # 版本升级重 init：先落 false（崩溃后下次加载继续重试），attempts 归零。
            self.registry.invalidate(name)
            entry = self.registry.field_state(name)
        if not manual and not retry_allowed(entry, self._now()):
            retry_at = next_retry_at(entry)
            return FieldOutcome(
                name, "backoff", attempts=int(entry.get("attempts") or 0),
                next_retry_at=retry_at.isoformat(timespec="seconds") if retry_at else None,
                details={"last_failed_at": entry.get("last_failed_at")})
        stage_force = bool(force) if force is not None else False
        # registry 为真源（2026-11-13）：登记未初始化 → 强制重 init，不信任业务层旧标记
        # （旧 emotion_config.initialized=true 但 persona 已变/旧格式时，阶段会误跳过致死循环）。
        registry_uninitialized = not bool(entry.get("initialized"))
        try:
            result = await self.service.run_stage(
                name, manual=manual,
                force=bool(forced_by_version or stage_force or registry_uninitialized))
        except asyncio.CancelledError:
            # 取消不记失败：登记保持调用前状态（false 字段下次自然重试）。
            raise
        except Exception as exc:
            self.registry.mark_false(name, error=str(exc))
            state = self.registry.field_state(name)
            return FieldOutcome(name, "failed", message=f"阶段执行异常：{exc}",
                                errors=(str(exc),), attempts=int(state.get("attempts") or 0))
        stage = result.stage(name)
        stage_status = str(getattr(stage, "status", "") or "")
        if stage is None:
            self.registry.mark_false(name, error="阶段未返回结果")
            state = self.registry.field_state(name)
            return FieldOutcome(name, "failed", message="阶段未返回结果",
                                attempts=int(state.get("attempts") or 0))
        if not stage.ok:
            errors = tuple(str(line) for line in getattr(stage, "error_lines", ()) or ())
            message = str(getattr(stage, "message", "") or "") or (
                "字段初始化失败" if not stage.skipped else "字段无需初始化但未通过校验")
            self.registry.mark_false(name, error=message)
            state = self.registry.field_state(name)
            return FieldOutcome(name, "failed", stage_status=stage_status,
                                message=message, errors=errors,
                                attempts=int(state.get("attempts") or 0),
                                details=dict(getattr(stage, "details", {}) or {}))
        validator = VALIDATORS.get(name)
        validated = False
        errors: Tuple[str, ...] = ()
        validator_note = ""
        if validator is not None:
            try:
                result = validator(self._validation_context(stage, version))
                if isinstance(result, ValidationResult):
                    validated = bool(result.ok)
                    errors = tuple(str(line) for line in result.errors if str(line).strip())
                else:
                    # 兼容旧式 bool 校验器（测试替身/外部注入）：无详细原因可用。
                    validated = bool(result)
            except Exception as exc:
                validated = False
                validator_note = f"校验器异常：{type(exc).__name__}: {exc}"
                errors = (validator_note,)
                self._log.warning("初始化字段 %s 校验器异常（留 false）：%s", name, exc)
        if not validated:
            detail = "；".join(errors)
            message = "初始化返回成功但独立校验未通过（不记完成）"
            if detail:
                message = f"{message}：{detail}"
            elif not validator_note:
                # 无校验器 / 旧式 bool 校验器只给 False 时，至少说明未通过。
                detail = message
            self.registry.mark_false(name, error=detail or message)
            state = self.registry.field_state(name)
            return FieldOutcome(name, "failed", stage_status=stage_status,
                                message=message, errors=errors, validated=False,
                                attempts=int(state.get("attempts") or 0),
                                details=dict(getattr(stage, "details", {}) or {}))
        self.registry.mark_true(name, version)
        state = self.registry.field_state(name)
        return FieldOutcome(
            name, "success", stage_status=stage_status,
            message=str(getattr(stage, "message", "") or "已初始化并通过校验"),
            validated=True, attempts=int(state.get("attempts") or 0),
            details=dict(getattr(stage, "details", {}) or {}))


# ----------------------------------------------------------------------
# 模块入口（plugin.on_load / 命令共同使用）
# ----------------------------------------------------------------------
def build_driver(plugin: Any, **kwargs: Any) -> AutoInitDriver:
    return AutoInitDriver(plugin, **kwargs)


async def run(plugin: Any) -> Optional[AutoInitReport]:
    """自动初始化入口（非阻塞调用方用 :func:`schedule`）；异常静默降级。"""
    try:
        return await build_driver(plugin).run(manual=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            logger.warning("加载自动初始化失败（静默降级，不影响插件加载）：%s", exc)
        except Exception:
            pass
        return None


async def run_manual(plugin: Any, fields: Optional[Sequence[str]] = None,
                     *, force: Optional[bool] = None) -> AutoInitReport:
    """手动命令入口：不受退避限制；显式字段默认强制重 init，全量保持幂等。"""
    if force is None:
        force = bool(fields)
    return await build_driver(plugin).run(manual=True, fields=fields, force=force)


def schedule(plugin: Any) -> Optional[asyncio.Task]:
    """在运行中的事件循环上非阻塞启动自动初始化；异常/无循环返回 None。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    try:
        task = loop.create_task(run(plugin))
    except Exception as exc:
        try:
            logger.warning("自动初始化任务创建失败（忽略）：%s", exc)
        except Exception:
            pass
        return None

    def _consume(done: asyncio.Task) -> None:
        try:
            done.exception()
        except (asyncio.CancelledError, Exception):
            pass

    task.add_done_callback(_consume)
    return task


def registry_snapshot(plugin: Any) -> Dict[str, Any]:
    data_dir = _plugin_data_dir(plugin)
    if data_dir is None:
        return {"path": "", "fields": {}, "preflight": {}, "plugin_version": None}
    return InitRegistry(data_dir).snapshot()


def status_text(plugin: Any) -> str:
    """``/lbm ops init status`` 文本：直读 init_registry.json 逐字段展示。"""
    snapshot = registry_snapshot(plugin)
    lines = [f"初始化登记：{snapshot.get('path') or '未装配'}"
             f"（schema v1，登记版本={snapshot.get('plugin_version') or '未写入'}）"]
    preflight = snapshot.get("preflight") or {}
    if preflight.get("ok") is True:
        lines.append(f"预检：通过（{_short_ts(preflight.get('checked_at')) or '时间未知'}）")
    elif preflight.get("ok") is False:
        lines.append(f"预检：失败（{preflight.get('reason') or '未知原因'}；整体未初始化）")
    else:
        lines.append("预检：尚无记录（插件停用或尚未执行加载自动初始化）")
    fields = snapshot.get("fields") or {}
    for name in INIT_FIELDS:
        entry = fields.get(name) or {}
        initialized = bool(entry.get("initialized"))
        line = (f"- {field_label(name)}：{'已初始化' if initialized else '未初始化'}"
                f"，version={entry.get('version') or '-'}"
                f"，attempts={int(entry.get('attempts') or 0)}"
                f"，validated_at={_short_ts(entry.get('validated_at')) or '-'}"
                f"，last_failed_at={_short_ts(entry.get('last_failed_at')) or '-'}")
        last_error = str(entry.get("last_error") or "").strip()
        if last_error and not initialized:
            line = f"{line}\n  上次失败原因：{last_error}"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "AutoInitDriver", "AutoInitReport", "FieldOutcome", "FIELD_LABELS",
    "build_driver", "run", "run_manual", "schedule", "status_text",
    "registry_snapshot", "compute_need", "retry_allowed", "next_retry_at",
    "field_label",
]
