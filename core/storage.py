"""JSON 持久化小工具：原子写 + 容错读。"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        # F6（2026-11-04）：损坏文件留痕——否则下次原子写会用默认值静默覆盖，
        # 数据丢失不可观测。回退 default 的既有行为不变；只记路径不记内容。
        logger.warning("JSON 文件损坏，回退默认值: %s", path)
        return default
    except OSError:
        return default


def save_json_atomic(path: Path, data: Any) -> bool:
    """tmp + os.replace 原子写，避免写一半损坏。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
