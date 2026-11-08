"""JSON 持久化小工具：原子写 + 容错读。"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

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
    """tmp + os.replace 原子写，避免写一半损坏。

    PR-RELIABILITY（3.17.17 §1D）：tmp 用唯一名（uuid 后缀，同目录保证
    os.replace 同卷）——并发写同路径不再争用同一临时文件。Windows 下并发
    replace 同一目标可能瞬时 WinError 5（目标被另一 replace 短暂占用），
    有界重试吸收（5 次、递增短睡眠）；单写入者语义（最后写入者胜、不交错）
    仍由调用方锁保证；失败路径 best-effort 清理 tmp。
    """
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + "." + uuid4().hex[:12] + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
        return True
    except OSError:
        _cleanup_tmp(tmp)
        return False
    except Exception:
        _cleanup_tmp(tmp)
        raise


def _cleanup_tmp(tmp: Path | None) -> None:
    if tmp is None:
        return
    try:
        os.unlink(tmp)
    except OSError:
        # best-effort 清理留痕（守卫 #9：高风险路径 except 不得只有 pass）；
        # 残留 tmp 带唯一名，不会与后续写入争用，可由磁盘清理兜底。
        logger.debug("原子写临时文件清理失败（忽略）: %s", tmp, exc_info=True)
