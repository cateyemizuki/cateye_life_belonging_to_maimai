"""跨模块数值转换（2026-11-02 质量清单 P2-05）。

此前 ``_to_float`` / ``_to_int`` / ``_is_number`` / ``_as_float`` 在 19 处以
近似但不完全相同的语义各写一份（bool 是否算数值、字符串是否接受、NaN/Inf
是否拒绝、默认值类型是否强制转换）。本模块把它们收成两族显式口径：

- **宽松族** :func:`to_float` / :func:`to_int`：接受字符串数字与 bool
  （``float("1.2")`` / ``float(True)``），失败回退默认值；
- **严格族** :func:`is_number` / :func:`is_int` / :func:`is_finite_number` /
  :func:`as_float`：只接受数值类型，bool 一律不算数值；``as_float`` 拒绝 NaN
  （``is_finite_number`` 额外拒绝 Inf）。

本模块只依赖标准库，不导入业务模块或 plugin（与 core/time_utils.py 同层纪律）。
"""

from __future__ import annotations

import math
from typing import Any, Optional


def to_float(value: Any, default: float = 0.0) -> float:
    """宽松转 float：字符串数字与 bool 可用；失败回退 ``float(default)``。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def to_int(value: Any, default: int = 0) -> int:
    """宽松转 int：字符串数字与 bool 可用；失败回退 ``int(default)``。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def is_number(value: Any) -> bool:
    """数值判定：``int``/``float`` 且剔除 bool（``True`` 是 int 的实例，不算数值）。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_int(value: Any) -> bool:
    """``int`` 判定（同样剔除 bool）。"""
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value: Any) -> bool:
    """有限数值判定：``int``/``float``、非 bool、非 NaN/Infinity。"""
    return is_number(value) and math.isfinite(float(value))


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """严格转 float：bool/非数值/NaN 不算数值；失败回退 ``default``（Infinity 保留）。"""
    if not is_number(value):
        return default
    number = float(value)
    if number != number:  # NaN 防御
        return default
    return number


def as_int(value: Any, default: int = 0) -> int:
    """严格转 int：bool/非数值不算数值（字符串不自动解析）；失败回退 ``default``。"""
    if not is_number(value):
        return default
    return int(value)
