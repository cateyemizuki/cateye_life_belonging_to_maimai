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


def private_qq_stream(row: Any, target_qq: str, *, queried_by_user: bool = False) -> str:
    """Accept the host's scoped lookup; reject conflicting platform, group or user metadata."""
    if not isinstance(row, dict) or row.get("success") is False:
        return ""
    if str(row.get("platform") or "qq").strip().lower() != "qq":
        return ""
    if (row.get("group_id") or row.get("is_group_session") is True
            or str(row.get("chat_type") or "").lower() == "group"):
        return ""
    owner = str(row.get("user_id") or "").strip()
    if owner != target_qq and (owner or not queried_by_user):
        return ""
    return str(row.get("stream_id") or "").strip()


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


def command_admin(config: Any, *, platform: str, user_id: str, is_local_operator: bool) -> bool:
    """Command identities come from the host envelope, independently of chat text."""
    if is_local_operator is True:
        return True
    caller = qq_id(user_id)
    if str(platform or "").strip().lower() != "qq" or not caller:
        return False
    return any(qq_id(candidate) == caller for candidate in admin_users_of(config))


def command_stream_allowed(config: Any, *, stream_id: str, platform: str, user_id: str,
                           group_id: str, is_local_operator: bool) -> bool:
    if is_local_operator is True:
        return True
    entries = getattr(getattr(config, "admin", None), "allowed_streams", ()) or ()
    allowed = {str(entry).strip() for entry in entries if str(entry).strip()}
    if not allowed or any(entry.lower() == "all" for entry in allowed):
        return True
    sid = str(stream_id or "").strip()
    if not sid:
        return False
    candidates = {sid, "session:" + sid}
    platform = str(platform or "").strip().lower()
    group = str(group_id or "").strip()
    user = str(user_id or "").strip()
    if platform and group:
        candidates.add(f"{platform}:group:{group}")
    elif platform and user:
        candidates.add(f"{platform}:private:{user}")
    return not allowed.isdisjoint(candidates)


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
