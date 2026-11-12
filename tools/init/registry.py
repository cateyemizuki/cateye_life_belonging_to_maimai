# -*- coding: utf-8 -*-
"""逐字段初始化登记文件读写（``<data_dir>/init_registry.json``）。

崩溃安全契约
============
- ``initialized=true`` 是唯一「完成」信号，**只在该字段 init 成功且校验器通过后**
  由 :meth:`InitRegistry.mark_true` 写入；
- 每次标记都重新读盘 → 只改一个字段 → ``core.storage.save_json_atomic`` 整文件
  原子写（tmp + os.replace）：写盘中途崩溃只会留下旧态或新态，不会出现半截 JSON；
- 文件不存在 → 全 false（首次安装，不落盘）；文件存在但格式不符新版 schema
  （schema_version!=1 / 字段条目缺键/类型错）→ **直接重写为全 false**，不回填旧
  ``life_init.json``（方案 §八.5 裁决）。

字段条目：
``{"initialized": bool, "version": str|None, "validated_at": str|None,
   "attempts": int, "last_failed_at": str|None}``
顶层另有 ``plugin_version`` 与 ``preflight``（预检状态，区别于业务字段失败）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from core.storage import save_json_atomic

from .manifest import (INIT_FIELDS, REGISTRY_FILE_NAME, REGISTRY_SCHEMA_VERSION)

logger = logging.getLogger(__name__)

#: 预检状态缺省值（与业务字段分开登记，方案 §五.5）
_DEFAULT_PREFLIGHT: Dict[str, Any] = {"ok": None, "checked_at": None, "reason": ""}


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_timestamp(value: Any) -> Optional[datetime]:
    """解析 ISO 时间戳；不可解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def default_field_state() -> Dict[str, Any]:
    return {
        "initialized": False,
        "version": None,
        "validated_at": None,
        "attempts": 0,
        "last_failed_at": None,
    }


def default_document() -> Dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "plugin_version": None,
        "preflight": dict(_DEFAULT_PREFLIGHT),
        "fields": {name: default_field_state() for name in INIT_FIELDS},
    }


def _is_optional_text(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _valid_field_entry(entry: Any) -> bool:
    if not isinstance(entry, Mapping):
        return False
    if not isinstance(entry.get("initialized"), bool):
        return False
    if not _is_optional_text(entry.get("version")):
        return False
    if not _is_optional_text(entry.get("validated_at")):
        return False
    attempts = entry.get("attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        return False
    return _is_optional_text(entry.get("last_failed_at"))


def _valid_preflight(value: Any) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping):
        return False
    if value.get("ok") not in (None, True, False):
        return False
    if not _is_optional_text(value.get("checked_at")):
        return False
    return isinstance(value.get("reason", ""), str)


def document_matches_schema(document: Any) -> bool:
    """文件是否完全符合现行 schema（任何缺键/类型错 → False，触发重写）。"""
    if not isinstance(document, Mapping):
        return False
    if document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
        return False
    if not _is_optional_text(document.get("plugin_version")):
        return False
    if not _valid_preflight(document.get("preflight")):
        return False
    fields = document.get("fields")
    if not isinstance(fields, Mapping):
        return False
    for name in INIT_FIELDS:
        if not _valid_field_entry(fields.get(name)):
            return False
    return True


class InitRegistry:
    """登记文件单一读写口（进程内线程锁 + 原子整文件写）。"""

    def __init__(self, data_dir: Any, *, path: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.path = Path(path) if path is not None else self.data_dir / REGISTRY_FILE_NAME
        self._io_lock = threading.RLock()

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------
    def _read_raw(self) -> Any:
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return "__invalid__"

    def _fresh_and_rewrite(self, reason: str) -> Dict[str, Any]:
        document = default_document()
        try:
            written = save_json_atomic(self.path, document)
        except Exception as exc:
            written = False
            logger.warning("init_registry 重写失败（%s）：%s", reason, exc)
        else:
            if not written:
                logger.warning("init_registry 重写失败（%s）：原子写返回 False", reason)
        return document

    def load(self, *, rewrite: bool = True) -> Dict[str, Any]:
        """读登记文件；不存在→全 false；格式不符→重写为全 false（``rewrite=False`` 只读不写）。"""
        with self._io_lock:
            raw = self._read_raw()
            if raw is None:
                return default_document()
            if not document_matches_schema(raw):
                if not rewrite:
                    return default_document()
                logger.warning("init_registry 格式不符 schema v%s，直接重写（不回填）：%s",
                               REGISTRY_SCHEMA_VERSION, self.path)
                return self._fresh_and_rewrite("格式不符")
            document = dict(raw)
            document["fields"] = {
                name: dict((raw.get("fields") or {}).get(name) or default_field_state())
                for name in INIT_FIELDS
            }
            preflight = document.get("preflight")
            document["preflight"] = dict(preflight) if isinstance(preflight, Mapping) else dict(_DEFAULT_PREFLIGHT)
            return document

    def field_state(self, field: str) -> Dict[str, Any]:
        document = self.load()
        return dict(document["fields"].get(field) or default_field_state())

    def snapshot(self) -> Dict[str, Any]:
        """只读快照（命令 status / 诊断用；不创建/重写文件）。"""
        document = self.load(rewrite=False)
        return {
            "schema_version": document.get("schema_version"),
            "plugin_version": document.get("plugin_version"),
            "preflight": dict(document.get("preflight") or _DEFAULT_PREFLIGHT),
            "path": str(self.path),
            "fields": {name: dict(document["fields"].get(name) or default_field_state())
                       for name in INIT_FIELDS},
        }

    # ------------------------------------------------------------------
    # 原子逐字段写
    # ------------------------------------------------------------------
    def _save(self, document: Mapping[str, Any]) -> bool:
        try:
            return bool(save_json_atomic(self.path, document))
        except Exception as exc:
            logger.warning("init_registry 写入失败：%s", exc)
            return False

    def mark_true(self, field: str, version: str, *, validated_at: Any = None) -> bool:
        """init 成功且校验通过：写 true（attempts 归零、清空失败时间）。"""
        if field not in INIT_FIELDS:
            raise ValueError(f"未知初始化字段：{field}")
        with self._io_lock:
            document = self.load()
            entry = document["fields"][field]
            entry.update(
                initialized=True,
                version=str(version or "") or None,
                validated_at=str(validated_at or _now_iso()),
                attempts=0,
                last_failed_at=None,
            )
            return self._save(document)

    def mark_false(self, field: str, *, error: str = "") -> bool:
        """init 失败/校验不过：保持 false，attempts+1 并记最后失败时间。"""
        if field not in INIT_FIELDS:
            raise ValueError(f"未知初始化字段：{field}")
        with self._io_lock:
            document = self.load()
            entry = document["fields"][field]
            entry.update(
                initialized=False,
                version=None,
                validated_at=None,
                attempts=int(entry.get("attempts") or 0) + 1,
                last_failed_at=_now_iso(),
            )
            if error:
                logger.info("初始化字段 %s 留 false（attempts=%s）：%s",
                            field, entry["attempts"], error)
            return self._save(document)

    def invalidate(self, field: str) -> bool:
        """版本升级重 init：强制 false 且 attempts 归零（崩溃后下次加载继续重试）。"""
        if field not in INIT_FIELDS:
            raise ValueError(f"未知初始化字段：{field}")
        with self._io_lock:
            document = self.load()
            document["fields"][field] = default_field_state()
            return self._save(document)

    def set_plugin_version(self, version: str) -> bool:
        """写回登记文件版本（无变化则不重复落盘）。"""
        with self._io_lock:
            document = self.load()
            normalized = str(version or "") or None
            if document.get("plugin_version") == normalized:
                return False
            document["plugin_version"] = normalized
            return self._save(document)

    def set_preflight(self, ok: Optional[bool], *, reason: str = "",
                      checked_at: Any = None, disabled: bool = False) -> bool:
        """登记 preflight 状态（失败/停用不产生业务字段 attempts）。"""
        with self._io_lock:
            document = self.load()
            document["preflight"] = {
                "ok": None if disabled else bool(ok),
                "checked_at": str(checked_at or _now_iso()),
                "reason": str(reason or ""),
            }
            return self._save(document)


__all__ = [
    "InitRegistry", "default_document", "default_field_state",
    "document_matches_schema", "parse_timestamp",
]
