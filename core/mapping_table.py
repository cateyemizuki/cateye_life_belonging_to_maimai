"""映射表装载公共协议（2026-11-02 质量清单 P2-04）。

此前 6 个映射表装载器各自复制「表层路径解析 → 缺表补种 → 读文件 → 版本/顶层键
校验」样板，BOM 容忍、种子链、错误文案存在多副本漂移风险。本模块只收敛这部分
**装载协议**，各表的条目校验/合并/落账仍留在业务模块：

- :class:`MappingTableSpec`：文件名 + 日志用中文名 + 缺表生成器；
- :func:`resolve_table_path`：表层路径 → 旧根迁移/种子复制 → 仍缺则生成默认；
- :func:`read_document` / :func:`check_version` / :func:`check_top_level_keys`：
  统一读取与文件级校验（错误消息逐字保持原各模块口径）。

本模块只依赖标准库与 :mod:`core.mapping_dir`，不导入业务模块或 plugin。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .mapping_dir import MappingReadError, ensure_user_table, mappings_path, read_json_text

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MappingTableSpec:
    """单张映射表的文件级描述（条目校验/合并策略由业务模块负责）。"""

    file_name: str
    label: str
    generate_default: Callable[[Path], None]


def plugin_root(module_file: Any) -> Path:
    """业务模块文件（位于插件根下一层）→ 插件根目录。"""
    return Path(module_file).resolve().parents[1]


def resolve_table_path(root_dir: Any, spec: MappingTableSpec) -> tuple[Path, bool]:
    """解析表层映射表路径；返回 ``(path, generated_default)``。

    缺失时按 §29.2 既有顺序补齐：旧根资产迁移优先 → 出厂种子复制 → 仍缺才调用
    ``spec.generate_default`` 生成内置默认。存在性异常按缺失处理（不拖挂装载）。
    生成失败由生成器自身 best-effort 吞掉；本函数仍返回 generated=True，与各
    装载器既有「生成后按内置返回」语义一致。
    """
    path = mappings_path(Path(root_dir), spec.file_name)
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        ensure_user_table(root_dir, spec.file_name)
        try:
            present = path.exists()
        except OSError:
            present = False
    if not present:
        spec.generate_default(path)
        return path, True
    return path, False


def read_document(path: Path, rejected: Callable[[str, str, str], BaseException]) -> Any:
    """读取 JSON；失败统一抛调用方的 ``rejected("", "parse", detail)``。"""
    try:
        return read_json_text(path)
    except MappingReadError as exc:
        raise rejected("", "parse", exc.detail) from exc


def check_version(raw: dict, supported_version: int,
                  rejected: Callable[[str, str, str], BaseException],
                  *, fmt: Callable[[Any], str] = repr) -> int:
    """顶层 ``version`` 必填 int（剔除 bool）且等于 ``supported_version``。"""
    if "version" not in raw:
        raise rejected("version", "version_missing",
                       f"顶层键 version：期望 int {supported_version}，实得 键缺失")
    value = raw["version"]
    if isinstance(value, bool) or not isinstance(value, int) or int(value) != supported_version:
        raise rejected("version", "version_unsupported",
                       f"顶层键 version：期望 int {supported_version}，实得 {fmt(value)}")
    return int(value)


def check_top_level_keys(raw: dict, allowed: Sequence[str],
                         rejected: Callable[[str, str, str], BaseException],
                         *, allowed_text: Optional[str] = None) -> None:
    """顶层键集校验：文件序遇到首个未知键即抛 ``rejected``。"""
    text = allowed_text if allowed_text is not None else "、".join(str(k) for k in allowed)
    for key in raw:
        if key not in allowed:
            raise rejected(str(key), "unknown_key",
                           f"顶层键 {key!r}：未知键（允许的键集 {text}）")
