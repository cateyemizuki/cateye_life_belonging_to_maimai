"""陪伴目标私聊的按需屏幕工具及请求期作用域过滤。"""
from __future__ import annotations

from datetime import datetime
import json
import logging
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from companion.config import target_qq_of
from companion.screen_config import screen_config_of
from core.storage import save_json_atomic

SCREEN_TOOL_NAME = "lbm_peek_companion_screen"
logger = logging.getLogger(__name__)
SCREEN_TOOL_DESCRIPTION = (
    "查看陪伴对象电脑当前的屏幕状况。仅在陪伴对象私聊中可用；"
    "返回本次截图的文字描述，不控制电脑、不发送图片。"
    "结果只代表标注时间的画面；离线或没看清时如实说明，不猜测。"
    "一次调用后根据结果正常交流，不要连续反复截图。"
)


def _is_screen_tool(definition: Any) -> bool:
    if not isinstance(definition, dict):
        return False
    function = definition.get("function")
    name = function.get("name") if isinstance(function, dict) else definition.get("name")
    return name == SCREEN_TOOL_NAME


def screen_registration_stream(plugin: Any) -> str:
    """Recover a verified single stream with the same source of authority."""
    from core.targeting import qq_id
    try:
        path = Path(plugin.ctx.paths.data_dir) / "screen_target_scope.json"
        if path.stat().st_size > 4096:
            return ""
        saved = json.loads(path.read_text(encoding="utf-8"))
        owner = qq_id(saved.get("target_qq"))
        if not owner:
            return ""
        explicit = str(getattr(plugin.config.patrol, "target_qq", "") or "").strip()
        if explicit:
            if qq_id(explicit) != owner:
                return ""
        elif saved.get("basis") != "host_admin":
            return ""
        return str(saved.get("stream_id") or "").strip()
    except (OSError, ValueError, AttributeError, TypeError):
        return ""


def remember_screen_target(plugin: Any, target_qq: str, stream_id: str) -> bool:
    from core.targeting import qq_id
    try:
        path = Path(plugin.ctx.paths.data_dir) / "screen_target_scope.json"
        explicit = str(getattr(plugin.config.patrol, "target_qq", "") or "").strip()
        owner = qq_id(target_qq)
        stream = str(stream_id or "").strip()
        if owner and stream and target_qq_of(plugin.config) == owner:
            binding = {"target_qq": owner, "stream_id": stream,
                       "basis": "explicit" if explicit else "host_admin"}
        else:
            binding = {"target_qq": "", "stream_id": "", "basis": ""}
        if path.is_file() and path.stat().st_size <= 4096:
            try:
                if json.loads(path.read_text(encoding="utf-8")) == binding:
                    return True
            except ValueError:
                pass
        return bool(save_json_atomic(path, binding))
    except (OSError, ValueError, TypeError, AttributeError):
        logger.warning("屏幕工具会话绑定未保存，等待下次核验", exc_info=True)
        return False


class ScreenToolsMixin:
    def _screen_tool_allowed(self, context: dict[str, Any]) -> bool:
        """只信宿主注入的当前流；目标不明或身份冲突时拒绝。"""
        try:
            return self._screen_tool_allowed_checked(context)
        except Exception:
            return False

    def _screen_tool_allowed_checked(self, context: dict[str, Any]) -> bool:
        if not getattr(self, "_screen_scope_ready", True):
            return False
        config = getattr(self, "config", None)
        if not bool(getattr(getattr(config, "plugin", None), "enabled", False)):
            return False
        if not bool(getattr(getattr(config, "patrol", None), "enabled", False)):
            return False
        if not bool(getattr(screen_config_of(config), "tool_enabled", False)):
            return False
        target = target_qq_of(config)
        if target in ("", "0", "123456789"):
            return False
        if context.get("group_id") or context.get("is_group_chat") is True:
            return False
        target_stream = self._mai_target_stream_id()
        supplied = [str(context[k]).strip() for k in ("stream_id", "chat_id", "session_id") if context.get(k)]
        if not target_stream or not supplied or any(sid != target_stream for sid in supplied):
            return False
        if context.get("user_id") and str(context["user_id"]).strip() != target:
            return False
        if context.get("platform") and str(context["platform"]).lower() != "qq":
            return False
        return not any(k in context for k in ("target_qq", "target_user_id", "target_stream_id"))

    @HookHandler("maisaka.planner.before_request", name="lbm_screen_tool_scope",
        description="仅向陪伴目标私聊提供屏幕窥探工具", mode=HookMode.BLOCKING,
        order=HookOrder.LATE, timeout_ms=1000, error_policy=ErrorPolicy.SKIP)
    async def screen_hook_tool_scope(self, **kwargs: Any) -> dict[str, Any]:
        definitions = kwargs.get("tool_definitions")
        if not isinstance(definitions, list):
            return {"action": "continue"}
        allowed = self._screen_tool_allowed(kwargs)
        filtered = [d for d in definitions if allowed or not _is_screen_tool(d)]
        # 留证只包含作用域、工具是否可见；不记录屏幕、提示词或消息内容。
        scope = str(kwargs.get("session_id") or kwargs.get("stream_id") or "")
        audit = (allowed, any(_is_screen_tool(d) for d in filtered))
        previous = getattr(self, "_screen_scope_audit", {})
        if previous.get(scope) != audit:
            if len(previous) >= 128:
                previous = {}
            previous[scope] = audit
            self._screen_scope_audit = previous
            try:
                self.ctx.logger.info("屏幕工具列表 scope=%s allowed=%s present=%s", scope or "unknown", *audit)
            except Exception:
                pass
        return {"action": "continue", "modified_kwargs": {**kwargs, "tool_definitions": filtered}}

    @Tool(SCREEN_TOOL_NAME, description=SCREEN_TOOL_DESCRIPTION, parameters={}, platforms="qq",
          visibility="visible", timeout_ms=45000)
    async def screen_tool_peek(self, **kwargs: Any) -> str:
        if not self._screen_tool_allowed(kwargs):
            return json.dumps({"status": "denied", "message": "屏幕窥探仅在已启用的陪伴目标私聊中可用。"}, ensure_ascii=False)
        companion = self._mai_companion()
        client = getattr(companion, "cateye", None)
        if client is None:
            return json.dumps({"status": "unavailable", "message": "屏幕服务尚未就绪。"}, ensure_ascii=False)
        now = datetime.now().astimezone()
        peek = await client.peek_screen(now, manual=True)
        if not self._screen_tool_allowed(kwargs):
            return json.dumps({"status": "denied", "message": "屏幕权限或陪伴目标已变更，本次结果不再提供。"}, ensure_ascii=False)
        result = {"requested_at": now.isoformat(), "completed_at": datetime.now().astimezone().isoformat(),
                  "status": getattr(peek, "status", "unavailable")}
        if peek is not None and peek.ok:
            result["description"] = peek.description
        else:
            result["message"] = "电脑未连接。" if result["status"] == "offline" else "本次未能看清屏幕，请如实说明，稍后再看。"
        return json.dumps(result, ensure_ascii=False)
