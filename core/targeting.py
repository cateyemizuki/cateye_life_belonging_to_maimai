"""陪伴与管理员身份读取：显式配置优先，唯一 QQ 管理员兜底。"""
from __future__ import annotations

import asyncio
from typing import Any


def qq_id(value: Any) -> str:
    if isinstance(value, bool):
        return ""
    text = str(value or "").strip()
    if text.lower().startswith("qq:"):
        text = text[3:]
    return text if 0 < len(text) <= 20 and text.isascii() and text.isdigit() and int(text) > 0 and text != "123456789" else ""


def unique_qq_admin(values: Any) -> str:
    """不从多个管理员或混合平台列表中猜选一个对象。"""
    if not isinstance(values, (list, tuple)) or not values:
        return ""
    normalized = [qq_id(value) for value in values]
    if not all(normalized):
        return ""
    identities = set(normalized)
    return next(iter(identities)) if len(identities) == 1 else ""


def target_qq_of(config: Any) -> str:
    config = getattr(config, "config", config)
    section = getattr(config, "patrol", None)
    value = getattr(section, "target_qq", "")
    if str(value or "").strip():
        return qq_id(value)
    return qq_id(getattr(section, "_host_qq_admin", ""))


def admin_users_of(config: Any) -> list[str]:
    config = getattr(config, "config", config)
    values = getattr(getattr(config, "admin", None), "admin_users", None)
    if values:
        return [str(value) for value in values]
    target = target_qq_of(config)
    return [target] if target else []


async def refresh_host_target(plugin: Any) -> None:
    """活读宿主命令管理员，不将解析结果写回 TOML。失败时清除旧缓存。"""
    section = getattr(getattr(plugin, "config", None), "patrol", None)
    if section is None:
        return
    target = ""
    try:
        values = await asyncio.wait_for(plugin.ctx.config.get("plugin.permission"), timeout=3.0)
        target = unique_qq_admin(values)
    except Exception:
        pass
    section._host_qq_admin = target
