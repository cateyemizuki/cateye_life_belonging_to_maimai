# -*- coding: utf-8 -*-
"""告状小工具纯逻辑助手（需求3，2026-10-24 用户直派）。

bot 受骚扰时经 ``lbm_report_harassment`` Tool 把情况转达给维护者。本模块只放
**纯逻辑**：维护者目标解析优先级、冷却判定、告状文案与决策日志字段构造——
**不 import 宿主 ctx、不做任何 I/O**（stream 解析 / 发送 / 日志落盘留在
components 层），可独立 import 便于离线测试。
"""

from __future__ import annotations

import math
import time
from typing import Any

#: 工具注册名（components 层 @Tool 名与 plugin.get_components 关闭过滤共用此常量）
HARASS_TOOL_NAME = "lbm_report_harassment"
#: 占位 QQ（与 plugin._resolve_target_stream 的占位口径一致：占位号视为未配置）
PLACEHOLDER_QQ = "123456789"
#: 决策日志 trigger_type（成功/失败/冷却跳过三形态共用）
TRIGGER_TYPE = "admin.harass_report"


def normalize_qq(value: Any) -> str:
    """QQ 归一：剥 ``qq:`` 前缀（admin_users 文档允许该形式）、去空白；非纯数字 → 空串。"""
    s = str(value or "").strip()
    if s.lower().startswith("qq:"):
        s = s[3:].strip()
    return s if s.isdigit() else ""


def resolve_maintainer_qq(target_qq: Any, admin_users: Any = None,
                          companion_target_qq: Any = None) -> str:
    """显式告状对象 → 陪伴目标 → 宿主全局管理员；排除占位与非法账号。

    第二参数仅接收宿主管理员，保留位置参数以兼容旧纯函数调用。
    """
    for value in (target_qq, companion_target_qq):
        qq = normalize_qq(value)
        if qq and qq != PLACEHOLDER_QQ:
            return qq
    if isinstance(admin_users, (str, int)):
        admin_users = [admin_users]
    if not isinstance(admin_users, (list, tuple)):
        return ""
    for entry in admin_users:
        qq = normalize_qq(entry)
        if qq and qq != PLACEHOLDER_QQ:
            return qq
    return ""


def cooldown_check(now_ts: float, last_ts: float, cooldown_minutes: Any) -> tuple[bool, int]:
    """冷却判定（纯函数）：返回 ``(是否放行, 剩余分钟数[向上取整])``。

    ``cooldown_minutes`` 非法/≤0 → 恒放行；``last_ts``≤0（从未告状）→ 放行。
    """
    try:
        cd = max(0, int(cooldown_minutes or 0))
    except (TypeError, ValueError):
        cd = 0
    if cd <= 0 or last_ts <= 0:
        return True, 0
    remaining_seconds = cd * 60 - (now_ts - last_ts)
    if remaining_seconds <= 0:
        return True, 0
    return False, max(1, int(math.ceil(remaining_seconds / 60.0)))


def build_report_text(description: Any, location: str = "") -> str:
    """告状文案：『【告状】{（发生地：…）}{description 原文}』。

    **运维消息不是角色消息**：不追加情绪后缀、不改写 bot 的 description 原文。
    """
    head = "【告状】"
    if str(location or "").strip():
        head += f"（发生地：{str(location).strip()}）"
    return head + str(description or "").strip()


def cooldown_hint(remaining_minutes: int) -> str:
    """冷却内调用时给模型看的剩余提示。"""
    return f"告状冷却中：刚才已经告过状了，{max(1, int(remaining_minutes))} 分钟内不必重复（维护者已知悉）。"


def decision_fields(*, result: str, maintainer_qq: str = "", location: str = "",
                    detail: str = "") -> dict[str, Any]:
    """决策日志 ``admin.harass_report`` 留痕字段（三形态共用 trigger_type）。

    ``result`` ∈ ``success`` / ``failed`` / ``cooldown_skip``（组件层传入）。
    """
    return {
        "result": result,
        "maintainer": maintainer_qq,
        "location": location,
        "detail": detail,
    }


def monotonic_now() -> float:
    """冷却时间戳取值（独立成函数便于测试注入/打桩）。"""
    return time.monotonic()
