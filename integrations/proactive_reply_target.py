"""Bind this plugin's proactive private turns to the user's real context message.

No message lookup, generation or sending happens here. The planner request is the
authority: it already carries the host-formatted sender name and display msg_id.
Only the deep setting changes reply arguments, before the host executes the tool.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from html.parser import HTMLParser
import re
import time
from typing import Any

from core.inject import CATEYE_PLUGIN_ID, PROACTIVE_BLOCK_STALE_MS

_TARGET_MARKER = "【本轮主动回复目标】"
_HEADER = re.compile(r"^\s*(<([a-z_]+)\b[^>]*>)", re.IGNORECASE)
_END_TOOLS = frozenset(("stop", "wait"))


class _Attributes(HTMLParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tag = tag
        self.attrs = dict(attrs)


def _header(text: str, tag: str) -> dict[str, str | None] | None:
    match = _HEADER.match(text[:4096])
    if match is None or match[2].lower() != tag:
        return None
    parser = _Attributes(convert_charrefs=True)
    try:
        parser.feed(match[1])
    except (ValueError, TypeError):
        return None
    return getattr(parser, "attrs", None)


def _text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for part in item.get("parts", ()) if isinstance(item.get("parts"), list) else ():
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            return part["text"]
    return ""


def target_mode(value: Any) -> str:
    """Unknown/missing values always keep the prompt-only behavior."""
    return "deep" if value in ("deep", "深度处理") else "shallow"


@dataclass(frozen=True)
class ReplyTarget:
    message_id: str
    name: str
    index: int


def last_user_target(items: list[Any], target_user: str, stream_id: str) -> ReplyTarget | None:
    """Read only host chat wrappers, never instructions or tool text.

    The installed host omits is_self_message for incoming messages and emits
    is_self_message="true" for its own messages (planner_messages.py). Explicit
    user/session attributes, when present, must match the resolved private scope.
    """
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        attrs = _header(_text(item), "message")
        if attrs is None or str(attrs.get("is_self_message") or "false").lower() not in ("false", "0"):
            continue
        if attrs.get("session_id") and attrs["session_id"] != stream_id:
            continue
        if attrs.get("user_id") and attrs["user_id"] != target_user:
            continue
        message_id = str(attrs.get("msg_id") or "").strip()
        # IDs are host display identifiers: keep negative QQ IDs and safe aliases.
        if not message_id or len(message_id) > 160 or any(not c.isprintable() or c.isspace() or c in '<>"\'`' for c in message_id):
            continue
        name = str(attrs.get("user") or "").strip()
        if not name:
            continue
        return ReplyTarget(message_id, name, index)
    return None


def active_task(items: list[Any], now_ms: float) -> tuple[int, str] | None:
    """Latest plugin task only; later real input or stop/wait closes its scope."""
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        attrs = _header(_text(item), "plugin_proactive_task")
        if attrs is None:
            continue
        task_id = str(attrs.get("id") or "")
        prefix = "proactive:" + CATEYE_PLUGIN_ID + ":"
        if attrs.get("plugin_id") != CATEYE_PLUGIN_ID or not task_id.startswith(prefix):
            return None  # Another plugin's newer task must never inherit our policy.
        stamp = task_id[len(prefix):]
        if not stamp.isdigit() or not -120_000 <= now_ms - int(stamp) <= PROACTIVE_BLOCK_STALE_MS:
            return None
        for later in items[index + 1:]:
            if not isinstance(later, dict):
                continue
            call = later.get("tool_call")
            if later.get("item_type") == "FunctionCallItem" and isinstance(call, dict) and call.get("func_name") in _END_TOOLS:
                return None
            # A genuine new incoming message supersedes the plugin-only turn.
            if later.get("item_type") == "UserMessageItem":
                sender = _header(_text(later), "message")
                if sender is not None and str(sender.get("is_self_message") or "false").lower() in ("false", "0"):
                    return None
        return index, task_id
    return None


def _target_prompt(target: ReplyTarget | None) -> str:
    if target is None:
        return (_TARGET_MARKER + "当前上下文没有可核验的用户来信目标。"
                "不要编造msg_id，不要选自己发送的消息；你可以不调用reply，直接结束。")
    # Keep the context name recognizable while preventing markup/control injection.
    name = re.sub(r"[\x00-\x1f\x7f]", " ", target.name)[:120]
    name = name.translate(str.maketrans({"<": "＜", ">": "＞", "`": "｀"}))
    return (_TARGET_MARKER + "本轮由插件触发，你仍可自行决定是否回复。"
            f"如果决定调用reply，以 {name} 发送的 msg_id为 {target.message_id} 的消息作为回复目标。"
            "这是对方在本轮上下文中最后发送的消息；不要选择自己发送的消息，"
            "不要复述自己先前的发言。若决定不回复，不要调用reply。")


def prepare_request(items: list[Any], stream_id: str, target_user: str, *,
                    now_ms: float | None = None) -> tuple[list[Any], ReplyTarget | None, str]:
    active = active_task(items, time.time() * 1000 if now_ms is None else now_ms)
    if active is None:
        return items, None, ""
    index, task_id = active
    target = last_user_target(items[:index], target_user, stream_id)
    text = _text(items[index])
    # Replace only our own previous suffix, leaving the original intent intact.
    body = text.split(_TARGET_MARKER, 1)[0].rstrip().removesuffix("</plugin_proactive_task>").rstrip()
    text = body + "\n" + _target_prompt(target) + "\n</plugin_proactive_task>"
    updated = list(items)
    entry = deepcopy(items[index])
    for part in entry["parts"]:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            part["text"] = text
            break
    updated[index] = entry
    return updated, target, task_id


def rewrite_reply_calls(output_items: list[Any], target: ReplyTarget) -> tuple[list[Any], int]:
    """Copy-on-write, replacing only canonical reply tool args.msg_id."""
    updated = list(output_items)
    count = 0
    for index, item in enumerate(output_items):
        if not isinstance(item, dict) or item.get("item_type") != "FunctionCallItem":
            continue
        call = item.get("tool_call")
        if not isinstance(call, dict) or call.get("func_name") != "reply" or not isinstance(call.get("args"), dict):
            continue
        if call["args"].get("msg_id") == target.message_id:
            continue
        changed = deepcopy(item)
        changed["tool_call"]["args"]["msg_id"] = target.message_id
        updated[index] = changed
        count += 1
    return updated, count


@dataclass
class _Request:
    created: float
    task_id: str
    target: ReplyTarget | None
    mode: str
    target_user: str


class ProactiveReplyTargets:
    """Pair request/output hooks conservatively, with no sticky per-user target.

    The installed host supplies the same selection fields to both hooks. Message
    counts after injection can change, so built_message_count is deliberately not
    a correlation key. Overlapping requests with different bindings are never
    rewritten; identical target/mode bindings remain safe after cancellation.
    """
    def __init__(self) -> None:
        self._pending: dict[tuple[Any, ...], list[_Request]] = {}

    @staticmethod
    def _key(kwargs: dict[str, Any]) -> tuple[Any, ...] | None:
        stream = kwargs.get("session_id")
        count = kwargs.get("selected_history_count")
        reason = kwargs.get("selection_reason")
        if not isinstance(stream, str) or not stream or type(count) is not int or not isinstance(reason, str):
            return None
        return stream, count, reason

    def before(self, kwargs: dict[str, Any], *, stream_id: str, target_user: str,
               mode: Any, now_ms: float | None = None) -> dict[str, Any] | None:
        cutoff = time.monotonic() - 900
        self._pending = {key: fresh for key, rows in self._pending.items()
                         if (fresh := [row for row in rows if row.created >= cutoff])}
        key = self._key(kwargs)
        items = kwargs.get("items")
        target = None
        task_id = ""
        updated = items
        if stream_id and target_user and kwargs.get("session_id") == stream_id and isinstance(items, list):
            updated, target, task_id = prepare_request(items, stream_id, target_user, now_ms=now_ms)
        if key is not None:
            rows = self._pending.setdefault(key, [])
            rows.append(_Request(time.monotonic(), task_id, target, target_mode(mode), target_user))
            if len(self._pending) > 128:
                self._pending.pop(next(iter(self._pending)))
        return {**kwargs, "items": updated} if updated is not items else None

    def after(self, kwargs: dict[str, Any], *, stream_id: str, target_user: str) -> tuple[dict[str, Any] | None, int]:
        key = self._key(kwargs)
        if key is None:
            return None, 0
        rows = self._pending.pop(key, [])
        # Missing after hooks (cancellation/failure) must not block a later request
        # with the SAME binding. Distinct bindings remain genuinely ambiguous.
        bindings = {(row.mode, row.target_user, bool(row.task_id),
                     row.target.message_id if row.target else None) for row in rows}
        if len(bindings) > 1:
            self._pending[key] = [_Request(row.created, "", None, "shallow", "") for row in rows[1:]]
            return None, 0
        if len(rows) > 1:
            self._pending[key] = rows[1:]
        if not rows or not stream_id or kwargs.get("session_id") != stream_id or not target_user:
            return None, 0
        request = rows[0]
        if (request.mode != "deep" or not request.task_id or request.target is None
                or request.target_user != target_user or time.monotonic() - request.created > 900):
            return None, 0
        output = kwargs.get("output_items")
        if not isinstance(output, list):
            return None, 0
        updated, count = rewrite_reply_calls(output, request.target)
        return ({**kwargs, "output_items": updated} if count else None), count
