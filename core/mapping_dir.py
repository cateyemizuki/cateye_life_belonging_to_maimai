"""映射表目录助手（2026-10-17 用户指示：所有外置映射表集中到插件根 ``mappings/`` 子文件夹）。

职责单一：
- 提供 ``mappings/`` 目录名常量与路径拼接（唯一读取位）；
- :func:`migrate_legacy_root_file`：旧版放在插件根目录的映射表 json（``emotion_mapping.json`` /
  ``emotion_descriptions.json``，F4/F5 期布局）在新位置缺失时**复制**进 ``mappings/``——
  只复制、不删除、不改写用户文件（旧文件留在原处由用户自行处置）；
- 全程 best-effort：任何文件系统异常只告警，绝不阻断映射表装载（装载方自带内置默认兜底）。

本模块只依赖标准库（不 import maibot_sdk / 插件其他模块），emotion/ 与 companion/ 双侧
可安全引用，无导入环风险；顶层模块名避开宿主 ``src/`` 同名（§9.1 纪律）。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 插件根下集中存放用户可编辑映射表的子目录（2026-10-17 起为唯一读取位）
MAPPINGS_DIR_NAME = "mappings"


class MappingReadError(Exception):
    """映射表读取/解析失败（非 UTF-8 / IO 错误 / JSON 语法错误）；``detail`` 可直接落账。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def read_json_text(path: Path) -> Any:
    """读取并解析 JSON 文本（UTF-8，容忍一个前导 BOM——Windows 记事本另存）。

    读取/解码/解析失败统一抛 :class:`MappingReadError`（语义与 ``emotion/mapping_override``
    的 ``error_kind="parse"`` 一致；缺文件应由调用方的存在性检查先行处理）。
    """
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MappingReadError(f"非 UTF-8 文本（{path.name}）：{exc}") from exc
    except OSError as exc:  # 权限/目录/被占用等
        raise MappingReadError(f"读取失败（{path.name}）：{exc}") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        return json.loads(text)
    except ValueError as exc:
        line = getattr(exc, "lineno", "?")
        col = getattr(exc, "colno", "?")
        msg = getattr(exc, "msg", str(exc))
        raise MappingReadError(f"JSON 解析失败（{path.name} 行 {line} 列 {col}）：{msg}") from exc


def mappings_path(root_dir: Any, file_name: str) -> Path:
    """``<root>/mappings/<file_name>``（``root_dir`` 由调用方保证非空）。"""
    return Path(root_dir) / MAPPINGS_DIR_NAME / file_name


def legacy_root_path(root_dir: Any, file_name: str) -> Path:
    """旧布局位置 ``<root>/<file_name>``（F4/F5 期映射表所在）。"""
    return Path(root_dir) / Path(file_name).name


def migrate_legacy_root_file(root_dir: Any, file_name: str) -> None:
    """旧根目录映射表 → ``mappings/`` 复制（新位置已存在或旧文件缺失时不做任何事）。

    best-effort：失败仅告警，调用方按「新位置不存在」继续走各自的默认生成/内置回退。
    """
    try:
        new_path = mappings_path(root_dir, file_name)
        old_path = legacy_root_path(root_dir, file_name)
        if new_path.exists() or not old_path.is_file():
            return
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(old_path, new_path)
        logger.info("已把旧映射表复制到新位置：%s → %s（旧文件保留，可自行删除）",
                    old_path, new_path)
    except Exception as exc:  # 权限/目录/被占用等——映射表装载绝不因迁移失败中断
        logger.warning("迁移旧映射表失败（忽略，按缺失处理）：%s", exc)
