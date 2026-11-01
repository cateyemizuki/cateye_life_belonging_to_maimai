"""情绪状态持久化内核（P2-07 第一阶段自 emotion/service.py 下沉）。

只承载「状态文档构建 + 原子写盘」纯函数，不持有 EmotionService 状态、不导入
planning/companion；服务端仍负责收集 self 属性、观测采样与错误降级。这样既缩小
service.py 的体积，又保留测试直接构造 ``EmotionService.__new__`` 并调用
``_save_state()`` 的既有路径。

关键语义（逐字保留）：
- episode 水位/日计数与状态机写同一份 emotion_state.json；
- 盘上已有 ``applied_sleep_date`` 覆盖式并入（状态机不认识该键）；
- 未知版本的睡眠回执按 JSON 值逐字保留；
- 已知回执重新编码；``_saving_sleep_cycle`` 生效时清掉旧回执。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from core.storage import save_json_atomic

from . import sleep_cache

logger = logging.getLogger(__name__)


def parse_episode_state(raw: Any) -> Dict[str, Any]:
    """解析状态文件里的 ``episode`` 键（水位 + 日计数）；缺键/坏类型 → 空默认，不抛。"""
    out: Dict[str, Any] = {"last_eval_ts": {}, "eval_date": "", "eval_count": 0}
    if not isinstance(raw, dict):
        return out
    raw_ts = raw.get("last_eval_ts")
    if isinstance(raw_ts, dict):
        for stream, value in raw_ts.items():
            try:
                out["last_eval_ts"][str(stream)] = float(value)
            except (TypeError, ValueError):
                continue
    out["eval_date"] = str(raw.get("eval_date") or "")
    try:
        out["eval_count"] = max(0, int(raw.get("eval_count") or 0))
    except (TypeError, ValueError):
        out["eval_count"] = 0
    return out


def build_document(machine_state: Dict[str, Any], episode: Dict[str, Any], *,
                   previous: Any, pending_overhaul: Any, sleep_period: Any,
                   applying_cycle: str) -> Dict[str, Any]:
    """构建待写状态文档（纯函数；不读文件、不写文件）。"""
    state = machine_state
    state["episode"] = {
        "last_eval_ts": {str(k): round(float(v), 3)
                         for k, v in episode["last_eval_ts"].items()},
        "eval_date": str(episode["eval_date"]),
        "eval_count": int(episode["eval_count"]),
    }
    # P4 落盘护栏：醒沿把 applied_sleep_date 直接写入文件，而本方法整体重写该文件；
    # 覆盖式并入盘上原值（状态机不认识该键，load_dict 忽略；不改其值、不新增字段）。
    if isinstance(previous, dict) and "applied_sleep_date" in previous:
        state["applied_sleep_date"] = previous["applied_sleep_date"]
    # Keep unknown receipt versions byte-for-byte at the JSON value level.
    prior_receipt = previous.get("pending_sleep_overhaul") if isinstance(previous, dict) else None
    if isinstance(prior_receipt, dict) and not sleep_cache.known(prior_receipt):
        state["pending_sleep_overhaul"] = prior_receipt
    elif pending_overhaul is not None and sleep_period:
        try:
            state["pending_sleep_overhaul"] = sleep_cache.encode(pending_overhaul, sleep_period)
        except (ValueError, TypeError, KeyError):
            logger.warning("睡眠裁判回执未通过持久化校验")
    if applying_cycle:
        state["applied_sleep_date"] = applying_cycle
        if sleep_cache.known(state.get("pending_sleep_overhaul")):
            state.pop("pending_sleep_overhaul", None)
    return state


def write_document(path: Path, document: Dict[str, Any], *, writer=save_json_atomic) -> bool:
    """原子写状态文档；``writer`` 可注入（测试按 emotion.service 名字打桩）。"""
    return bool(writer(path, document))


def write_params(path: Path, params: Dict[str, Any], saved_at: str, *,
                 writer=save_json_atomic) -> bool:
    """原子写参数文件；``writer`` 可注入（测试按 emotion.service 名字打桩）。"""
    return bool(writer(path, {"params": params, "saved_at": saved_at}))
