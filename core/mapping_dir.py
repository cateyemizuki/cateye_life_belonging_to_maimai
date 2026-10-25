"""映射表目录助手（2026-10-17 用户指示：所有外置映射表集中到插件根 ``mappings/`` 子文件夹）。

职责单一：
- 提供 ``mappings/`` 目录名常量与路径拼接（唯一读取位）；
- :func:`migrate_legacy_root_file`：旧版放在插件根目录的映射表 json（``emotion_mapping.json`` /
  ``emotion_descriptions.json``，F4/F5 期布局）在新位置缺失时**复制**进 ``mappings/``——
  只复制、不删除、不改写用户文件（旧文件留在原处由用户自行处置）；
- :func:`seed_path` / :func:`ensure_user_table`：出厂种子目录 ``mappings/default_mappings/``
  （2026-10-23 §二十九.2）：随包种子源；表层缺表时按序补齐——旧根资产优先（migrate），
  仍缺才从种子复制；
- 全程 best-effort：任何文件系统异常只告警，绝不阻断映射表装载（装载方自带内置默认兜底）；

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

#: 出厂种子子目录（2026-10-23 §29.2 直派）：随包分发的出厂默认（README + 六张 json），
#: 只作种子源——插件运行期**只读表层** ``mappings/`` 各张表，深层不参与装载。
#: 表层 = 用户数据区（文件不随包分发、更新插件不覆盖）；深层 = 随包种子源。
SEED_DIR_NAME = "default_mappings"

#: 第七张表：情绪-生活调制表（R7 = PR2 双向闭环，2026-10-26，计划书 §三十五/定稿 §4.1）。
#: 装载方 = emotion/life_feedback.py（emotion 侧唯一消费点）；gains 节为定稿 §4.1.1 的
#: VAD→LifeState 五项 rise/fall 调制系数（消费方 planning/life_modulation 属后续 PR，
#: 本 PR 只播种），feedback 节为 life.episode_* → VAD 事件类增量与日上限（本 PR 消费）。
#: 与其余六表「常量在装载方模块」的惯例略有不同：按派单要求登记在本模块作为单一真源，
#: 装载方从本处 import，避免两处各写一份字面量漂移。
EMOTION_LIFE_MODULATION_FILE_NAME = "emotion_life_modulation.json"


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


def seed_path(root_dir: Any, file_name: str) -> Path:
    """出厂种子路径 ``<root>/mappings/default_mappings/<file_name>``（2026-10-23 §29.2）。"""
    return Path(root_dir) / MAPPINGS_DIR_NAME / SEED_DIR_NAME / file_name


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


def ensure_user_table(root_dir: Any, file_name: str) -> None:
    """表层映射表缺失时的补齐入口（2026-10-23 §29.2 直派，五装载方共用）。

    仅当表层 ``mappings/<file_name>`` **不存在**时按序动作：

    1. 旧根迁移（:func:`migrate_legacy_root_file`）——用户旧资产优先，种子让位；
    2. 仍缺失 → 从出厂种子 ``mappings/default_mappings/<file_name>`` 复制进表层
       （``shutil.copyfile``，复制来源 = 原 git 内容，逐字节一致）。

    **红线（用户 2026-10-23 直派，计划书 §29.2）：解析错误（``MappingReadError``）
    不算缺失——本函数只做存在性判断（``exists()``），绝不读取/校验表层文件内容，
    因此绝不在解析错误时复制或覆盖用户文件**；解析错误由各装载方的既有校验
    回退链处理（回退内置默认），表层文件原地不动。

    best-effort：任何文件系统异常只告警，调用方一律按「表层仍缺失」继续走
    各自的内置默认生成（连种子也没有或缺种子照旧缺表处理，语义不变）。
    """
    try:
        user_path = mappings_path(root_dir, file_name)
        if user_path.exists():
            return
        # 旧根迁移优先（migrate 自带「表层已存在/旧根缺失 → 空操作」判断，用户旧资产优先）
        migrate_legacy_root_file(root_dir, file_name)
        if user_path.exists():
            return
        seed = seed_path(root_dir, file_name)
        if not seed.is_file():
            return
        user_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, user_path)
        logger.info("表层映射表缺失，已从出厂种子复制补齐：%s → %s（表层为用户数据区，可自行编辑）",
                    seed, user_path)
    except Exception as exc:  # 权限/目录/被占用等——补表失败绝不阻断映射表装载
        logger.warning("补齐表层映射表失败（忽略，按缺失处理）：%s", exc)
