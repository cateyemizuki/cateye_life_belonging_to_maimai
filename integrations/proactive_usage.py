"""Correlate the plugin's proactive model work and verified outbound messages."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
import json
import re
import time
import uuid
from typing import Any
from unicodedata import normalize

from core.inject import CATEYE_PLUGIN_ID, pick_payload_key
from integrations.proactive_reply_target import _header
from integrations.video_share_comment import VIDEO_SHARE_REPLY_ANALYSIS
from integrations.video_share_policy import (
    COMMENT_PLANNER_KIND, COMMENT_PLANNER_WINDOW_SECONDS, DEBUG_CARD_TOOL, DEBUG_PLANNER_KIND,
)

HOST_USAGE_TOOL = "lbm_host_turn_usage"
HOST_REQUESTS = {"planner": "maisaka.planner", "replyer": "maisaka.replyer"}
_REPLY_TOKEN = "_lbm_proactive_reply"
_REPLY_MARKER = re.compile(r"\n\[lbm-proactive-reply:([0-9a-f]{32})\]\s*$")
_TASK_META_PREFIX = "附加信息："
_DEBUG_END_TOOLS = ("wait", "stop")
_COMMENT_END_TOOLS = (*_DEBUG_END_TOOLS, "finish", "no_action")
_BLOCKED_VIDEO_COMMENT = "video_comment_blocked"
_REPLY_NOTE_FIELDS = ("chat_anlyze", "reply_reference")
_SCOPES = {"greeting": "planning", "video_share": "video", "video_completion": "video", "qzone_comment": "qzone"}


def _stream(kwargs):
    return str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()


def _text(item, payload_key):
    if not isinstance(item, dict):
        return ""
    if payload_key == "messages":
        if item.get("role") != "user":
            return ""
        content = item.get("content")
        if isinstance(content, list):
            # The legacy host interleaves plain text parts with image tuples.
            return "".join(part for part in content if isinstance(part, str))
        return content if isinstance(content, str) else ""
    if item.get("item_type") != "UserMessageItem":
        return ""
    parts = item.get("parts")
    return next((part["text"] for part in parts or [] if isinstance(part, dict) and part.get("type") == "text"
                 and isinstance(part.get("text"), str)), "")


def _task(text):
    attrs = _header(text, "plugin_proactive_task")
    prefix = "proactive:" + CATEYE_PLUGIN_ID + ":"
    if not attrs or attrs.get("plugin_id") != CATEYE_PLUGIN_ID:
        return None
    task_id = str(attrs.get("id") or "")
    if not task_id.startswith(prefix) or not task_id[len(prefix):].isdigit():
        return None
    meta = {}
    for line in text.splitlines():
        if line.startswith(_TASK_META_PREFIX):
            try:
                value = json.loads(line[len(_TASK_META_PREFIX):])
            except (RecursionError, ValueError):
                break
            if isinstance(value, dict):
                meta = value
            break
    return task_id, meta


def _has_later_input(items, key):
    for item in items:
        text = _text(item, key)
        header = _header(text, "message")
        if header is not None and str(header.get("is_self_message") or "false").lower() in ("false", "0"):
            return True
        if _header(text, "plugin_proactive_task") is not None:
            return True
        if not isinstance(item, dict):
            continue
        call = item.get("tool_call")
        if isinstance(call, dict) and call.get("func_name") in (*_COMMENT_END_TOOLS, DEBUG_CARD_TOOL):
            return True
        if key == "messages":
            for call in item.get("tool_calls") or []:
                func = call.get("function") if isinstance(call, dict) else None
                if isinstance(func, dict) and func.get("name") in (*_COMMENT_END_TOOLS, DEBUG_CARD_TOOL):
                    return True
    return False


def active_video_comment_task(items, key):
    """Check the current host task before the generic reply-target prompt runs."""
    for index, item in enumerate(items):
        task = _task(_text(item, key))
        if task and task[1].get("delivery_kind") == COMMENT_PLANNER_KIND and not _has_later_input(items[index + 1:], key):
            return True
    return False


def _tool_name(definition):
    if not isinstance(definition, dict):
        return ""
    nested = definition.get("function")
    return str((nested.get("name") if isinstance(nested, dict) else None) or definition.get("name") or "")


def _comment_reply_definition(definition, *, legacy):
    nested = definition.get("function")
    tool = nested if isinstance(nested, dict) else definition
    parameters = tool.get("parameters") or {}
    properties = parameters.get("properties") or {}
    note_key = next((key for key in _REPLY_NOTE_FIELDS if key in properties), "chat_anlyze")
    needs_id = legacy or "msg_id" in (parameters.get("required") or ())
    reply_end_options = (properties.get("after_reply") or {}).get("enum") or ()
    after_reply = "stop" if "stop" in reply_end_options else "wait"
    options = {"video_reply_note_key": note_key, "video_reply_needs_id": needs_id, "video_reply_end": after_reply}
    visible = {**tool, "description": "可选。只有想对当前会话的人顺手带一句时才调用一次。"}
    if note_key in properties:
        note = {**properties[note_key], "description": VIDEO_SHARE_REPLY_ANALYSIS}
        visible["parameters"] = {**parameters, "properties": {**properties, note_key: note}}
    return ({**definition, "function": visible} if isinstance(nested, dict) else visible), options


def _reply_note_key(arguments):
    return next((key for key in _REPLY_NOTE_FIELDS if isinstance(arguments.get(key), str)
                 and arguments[key].strip()), "")


def _hide_task_metadata(item, key):
    def strip(text):
        return "\n".join(line for line in text.splitlines() if not line.startswith(_TASK_META_PREFIX))

    if key == "messages":
        content = item.get("content")
        if isinstance(content, list):
            return {**item, "content": [strip(part) if isinstance(part, str) else part for part in content]}
        return {**item, "content": strip(content)}
    return {**item, "parts": [
        {**part, "text": strip(part["text"])}
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        else part for part in item.get("parts", [])
    ]}


def _canonical_text(value):
    return "".join(ch for ch in normalize("NFKC", str(value or "")) if ch.isalnum())[:2400]


def _digest(value):
    return sha256(value.encode()).digest()


def _outbound_text(message):
    raw = message.get("raw_message")
    if isinstance(raw, list):
        parts = []
        for part in raw:
            if not isinstance(part, dict) or part.get("type") != "text":
                continue
            data = part.get("data")
            if isinstance(data, dict):
                parts.append(str(data.get("text") or ""))
            elif isinstance(data, str):
                parts.append(data)
        if parts:
            return "".join(parts)
    return message.get("processed_plain_text") or ""


@dataclass(frozen=True)
class PlannerClaim:
    at: float
    turn: dict
    request_id: str


class ProactiveUsage:
    def __init__(self, plugin, *, clock=time.time):
        self._plugin = plugin
        self._clock = clock
        self._planner = {}
        self._reply_calls = {}
        self._reply_attempts = {}
        self._grants = {}
        self._pending_sends = {}
        self._built_sends = {}
        self._sent_turns = {}

    def _prune(self):
        now = self._clock()
        for bucket in (self._planner, self._reply_calls, self._reply_attempts, self._grants,
                       self._pending_sends, self._built_sends, self._sent_turns):
            for key, row in list(bucket.items()):
                if now - row[0] > 300:
                    bucket.pop(key, None)
            while len(bucket) > 256:
                bucket.pop(next(iter(bucket)))

    def _resolve(self, stream, task_id, metadata):
        registry = self._plugin._mai_proactive_turns()
        if registry is None:
            return None
        token = metadata.get("lbm_turn_id")
        if isinstance(token, str) and token:
            turn = registry.identified(stream, turn_id=token)
            if turn and turn.get("host_task_id") not in (None, "", task_id):
                return None
            return turn
        return registry.identified(stream, host_task_id=task_id)

    @staticmethod
    def _planner_key(kwargs):
        return _stream(kwargs), kwargs.get("selected_history_count"), str(kwargs.get("selection_reason") or "")

    def video_comment_policy_applies(self, kwargs):
        row = self._planner.get(self._planner_key(kwargs))
        return row is not None and row[3] in (COMMENT_PLANNER_KIND, _BLOCKED_VIDEO_COMMENT)

    async def planner_before(self, kwargs):
        """Veto identified old/busy greetings and capture the task before model invocation."""
        self._prune()
        key, payload = pick_payload_key(kwargs)
        stream = _stream(kwargs)
        if not key or not stream:
            return kwargs
        visible = list(payload)
        greet = getattr(getattr(self._plugin, "_planning", None), "proactive", None)
        identified = None
        comment_blocked = False
        # A blocked greeting may remain in the host's history for later calls.
        # Do not let that stale block become a suggestion in an ordinary reply.
        for index, item in enumerate(payload):
            block = _task(_text(item, key))
            if block is None:
                continue
            task_id, metadata = block
            turn = self._resolve(stream, task_id, metadata)
            active = not _has_later_input(payload[index + 1:], key)
            category = (turn or {}).get("category") or metadata.get("lbm_proactive_kind")
            if metadata.get("lbm_card_debug") is True or (turn or {}).get("kind") == DEBUG_PLANNER_KIND:
                proof = metadata.get("lbm_card_ticket")
                if (not active or (turn or {}).get("kind") != DEBUG_PLANNER_KIND
                        or not isinstance(proof, str) or not proof):
                    visible[index] = None
                    continue
                visible[index] = _hide_task_metadata(item, key)
                turn = {**turn, "debug_card_ticket": proof}
            if metadata.get("delivery_kind") == COMMENT_PLANNER_KIND or (turn or {}).get("kind") == COMMENT_PLANNER_KIND:
                if not active:
                    visible[index] = None
                    continue
                service = getattr(self._plugin, "_video_share_comment", None)
                route_check = service() if callable(service) else None
                allowed = ((turn or {}).get("kind") == COMMENT_PLANNER_KIND
                           and (turn or {}).get("tracked") is True
                           and await self.gate_check(route_check, "allowed", stream,
                                                     metadata.get("lbm_video_sent_from")))
                registry = self._plugin._mai_proactive_turns() if allowed else None
                if registry is None or not registry.begin_video_comment(
                    stream, turn["turn_id"], COMMENT_PLANNER_WINDOW_SECONDS,
                ):
                    visible[index] = None
                    comment_blocked = True
                    continue
                visible[index] = _hide_task_metadata(item, key)
                turn = {**turn, "video_sent_from": metadata["lbm_video_sent_from"]}
            if category == "greeting":
                scope = metadata.get("greeting_scope")
                if turn:
                    scope = scope or {"private_target": "target", "group": "group"}.get(turn.get("chat_type"))
                allowed = active and turn is not None
                if allowed:
                    if scope == "target":
                        allowed = await self.gate_check(greet, "target_greeting_proactive_allowed", stream)
                    elif scope == "group" and turn.get("kind") == "morning_greeting":
                        allowed = await self.gate_check(greet, "group_morning_proactive_allowed", stream)
                if not allowed:
                    visible[index] = None
                    continue
                turn = {**turn, "greeting_scope": scope}
            if active and turn is not None and turn.get("tracked") is True:
                identified = turn
        definitions = kwargs.get("tool_definitions")
        kind = (identified or {}).get("kind")
        if kind in (DEBUG_PLANNER_KIND, COMMENT_PLANNER_KIND):
            restriction = kind
        else:
            restriction = _BLOCKED_VIDEO_COMMENT if comment_blocked else ""
        modified = {}
        terminal = ""
        if isinstance(definitions, list):
            names = {_tool_name(tool) for tool in definitions}
            if not names.intersection(("reply", HOST_USAGE_TOOL, DEBUG_CARD_TOOL, *_COMMENT_END_TOOLS)):
                identified = None  # A host sub-agent may use the same prompt hooks.
            if restriction == DEBUG_PLANNER_KIND:
                allowed_tools = (*_DEBUG_END_TOOLS, DEBUG_CARD_TOOL)
            elif restriction:
                end_tools = _COMMENT_END_TOOLS if key == "messages" else _DEBUG_END_TOOLS
                allowed_tools = (*end_tools, "reply") if restriction == COMMENT_PLANNER_KIND else end_tools
                if key == "messages":
                    terminal = next((name for name in ("finish", "no_action", "stop") if name in names), "")
            else:
                allowed_tools = None
            filtered = [tool for tool in definitions if (
                _tool_name(tool) in allowed_tools if allowed_tools is not None else _tool_name(tool) != DEBUG_CARD_TOOL
            )]
            if restriction == COMMENT_PLANNER_KIND and identified is not None:
                for index, tool in enumerate(filtered):
                    if _tool_name(tool) == "reply":
                        filtered[index], options = _comment_reply_definition(tool, legacy=key == "messages")
                        identified = {**identified, **options}
                        break
            if filtered != definitions:
                modified["tool_definitions"] = filtered
        planner_key = self._planner_key(kwargs)
        old = self._planner.get(planner_key)
        claim = (PlannerClaim(self._clock(), identified, uuid.uuid4().hex) if identified else None)
        # A restricted turn that overlaps another request loses its authority.
        if old is not None:
            since, _, overlap, old_restriction, old_terminal = old
            blocked = _BLOCKED_VIDEO_COMMENT if restriction or old_restriction else ""
            self._planner[planner_key] = (since, None, overlap + 1, blocked, terminal or old_terminal)
        else:
            self._planner[planner_key] = (self._clock(), claim, 1, restriction, terminal)
        if visible != payload:
            modified[key] = [item for item in visible if item is not None]
        return {**kwargs, **modified} if modified else kwargs

    @staticmethod
    async def gate_check(service, method, *args):
        import asyncio
        check = getattr(service, method, None)
        if not callable(check):
            return False
        try:
            return bool(await asyncio.wait_for(check(*args), 1.5))
        except Exception:
            return False

    def _record(self, turn, role, kwargs, request_id):
        tracker = getattr(getattr(self._plugin, "_llm", None), "_usage_tracker", None)
        if tracker is None:
            return
        category = str(turn.get("category") or "other")
        counts = {name: kwargs.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        reported = any(type(value) is int and value > 0 for value in counts.values())
        tracker.record_v2(
            event=f"proactive_{category}_{role}", module=_SCOPES.get(category, "companion"),
            task_name=kwargs.get("task_name") or role, request_id=request_id,
            usage_available=reported, usage_present=reported, success=True, **counts,
        )

    def planner_after(self, kwargs):
        self._prune()
        stream = _stream(kwargs)
        key = self._planner_key(kwargs)
        row = self._planner.get(key)
        claim = None
        restriction = terminal = ""
        if row is not None:
            since, claim, count, restriction, terminal = row
            if count > 1:
                self._planner[key] = (since, None, count - 1, restriction, terminal)
            else:
                self._planner.pop(key, None)
        if claim is not None and self._clock() - claim.at <= 300:
            self._record(claim.turn, "planner", kwargs, f"{claim.turn['turn_id']}:p:{claim.request_id}")
            if claim.turn.get("category") == "greeting":
                output = kwargs.get("output_items")
                if isinstance(output, list) and not any(
                    isinstance(item, dict) and item.get("item_type") == "FunctionCallItem" for item in output
                ):
                    # Host action completion otherwise converts prose-only
                    # replies into a new, unmarked reply tool call.
                    return {**kwargs, "output_items": []} if output else kwargs
                if not isinstance(output, list) and not kwargs.get("tool_calls") and kwargs.get("response"):
                    return {**kwargs, "response": ""}
        if restriction == DEBUG_PLANNER_KIND:
            return self._debug_planner_output(kwargs, claim.turn.get("debug_card_ticket") if claim is not None else None)
        if restriction:
            allow_reply = (restriction == COMMENT_PLANNER_KIND and claim is not None
                           and "video_reply_note_key" in claim.turn
                           and self._plugin._video_share_comment().active(stream, claim.turn["video_sent_from"]))
            kwargs = self._comment_planner_output(kwargs, reply_turn=claim.turn if allow_reply else None,
                                                  terminal=terminal)
        return self._rewrite_tool_calls(kwargs, stream, claim)

    @staticmethod
    def _debug_planner_output(kwargs, proof):
        output = isinstance(kwargs.get("output_items"), list)
        key, field = ("output_items", "tool_call") if output else ("tool_calls", "function")
        selected = []
        for item in kwargs.get(key) or []:
            call = item.get(field) if isinstance(item, dict) else None
            if not isinstance(call, dict):
                continue
            name = call.get("func_name" if output else "name")
            if name in _DEBUG_END_TOOLS:
                selected = [item]
                break
            if name == DEBUG_CARD_TOOL and proof:
                # Use the host-associated ticket, never a proposed model argument.
                args = "args" if output else "arguments"
                selected = [{**item, field: {**call, args: {"ticket": proof}}}]
                break
        if output:
            return {**kwargs, key: selected}
        return {**kwargs, key: selected, "response": ""}

    @staticmethod
    def _comment_planner_output(kwargs, *, reply_turn, terminal):
        modern = isinstance(kwargs.get("output_items"), list)
        key, field = ("output_items", "tool_call") if modern else ("tool_calls", "function")
        name_field, args_field = ("func_name", "args") if modern else ("name", "arguments")
        end_tools = _DEBUG_END_TOOLS if modern else _COMMENT_END_TOOLS
        selected = []
        note_key = (reply_turn or {}).get("video_reply_note_key", "chat_anlyze")
        for item in kwargs.get(key) or []:
            call = item.get(field) if isinstance(item, dict) else None
            if not isinstance(call, dict):
                continue
            name, args = call.get(name_field), call.get(args_field)
            if name in end_tools:
                # A decline wins even when the model also supplied reply or prose.
                # An empty modern output ends silently across both wait contracts.
                if modern:
                    return {**kwargs, key: []}
                name = terminal or name
                safe = {"seconds": 0} if name == "wait" else {}
                selected = [{**item, field: {**call, name_field: name, args_field: safe}}]
                break
            if name != "reply" or reply_turn is None or selected or not isinstance(args, dict):
                continue
            note = args.get(note_key)
            note = note.strip()[:400] if isinstance(note, str) else ""
            if modern and not note:
                continue
            safe = {note_key: note or "当前会话刚完成一次视频分享。"}
            if reply_turn.get("video_reply_needs_id", not modern):
                message_id = args.get("msg_id")
                if type(message_id) not in (str, int) or not str(message_id).strip():
                    continue
                safe["msg_id"] = str(message_id).strip()
            if modern:
                safe["after_reply"] = reply_turn.get("video_reply_end", "wait")
            else:
                safe["set_quote"] = False
            selected = [{**item, field: {**call, args_field: safe}}]
        if not modern and terminal and (not selected or selected[0][field][name_field] == "reply"):
            # Legacy hosts retry empty outputs and continue thinking after reply.
            selected.append({"id": "lbm-comment-end-" + uuid.uuid4().hex,
                             "function": {"name": terminal, "arguments": {}}})
        return {**kwargs, key: selected, **({} if modern else {"response": ""})}

    def _rewrite_tool_calls(self, kwargs, stream, claim):
        key = "output_items" if isinstance(kwargs.get("output_items"), list) else "tool_calls"
        items = kwargs.get(key)
        if not stream or not isinstance(items, list):
            return kwargs
        updated = list(items)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            field = "tool_call" if key == "output_items" else "function"
            call = item.get(field)
            if not isinstance(call, dict):
                continue
            name = call.get("func_name" if field == "tool_call" else "name")
            args_field = "args" if field == "tool_call" else "arguments"
            arguments = call.get(args_field)
            if not isinstance(arguments, dict):
                continue
            nonce = uuid.uuid4().hex
            if name == HOST_USAGE_TOOL:
                self._grants[nonce] = (self._clock(), stream)
                arguments = {**arguments, "scope_proof": nonce}
            elif name == "reply" and claim is not None:
                note_key = _reply_note_key(arguments)
                if not note_key:
                    continue
                self._reply_calls[nonce] = (self._clock(), stream, claim.turn)
                arguments = {**arguments, note_key: _REPLY_MARKER.sub("", arguments[note_key]).rstrip()
                              + f"\n[lbm-proactive-reply:{nonce}]"}
            else:
                continue
            updated[index] = {**item, field: {**call, args_field: arguments}}
        return {**kwargs, key: updated} if updated != items else kwargs

    def reply_before(self, kwargs):
        args = kwargs.get("reply_tool_args")
        if not isinstance(args, dict):
            return kwargs
        note_key = _reply_note_key(args)
        text = args.get(note_key, "")
        match = _REPLY_MARKER.search(text)
        if match is None:
            return kwargs
        self._prune()
        nonce = match[1]
        row = self._reply_calls.get(nonce)
        cleaned = {**args, note_key: text[:match.start()].rstrip()}
        cleaned.pop(_REPLY_TOKEN, None)
        if row is not None and row[1] == _stream(kwargs):
            cleaned[_REPLY_TOKEN] = nonce
        return {**kwargs, "reply_tool_args": cleaned}

    def _reply_identified(self, kwargs):
        args = kwargs.get("reply_tool_args")
        if not isinstance(args, dict):
            return None
        nonce = args.get(_REPLY_TOKEN)
        if not isinstance(nonce, str) or not nonce:
            match = _REPLY_MARKER.search(args.get(_reply_note_key(args), ""))
            nonce = match[1] if match is not None else ""
        row = self._reply_calls.get(nonce)
        if row is not None and row[1] == _stream(kwargs) and self._clock() - row[0] <= 300:
            return nonce, row[2]
        return None

    def reply_turn(self, kwargs):
        identity = self._reply_identified(kwargs)
        return identity[1] if identity else None

    def reply_after(self, kwargs):
        self._prune()
        identity = self._reply_identified(kwargs)
        if identity is None:
            return None
        nonce, turn = identity
        key = nonce, kwargs.get("attempt", 1)
        if key not in self._reply_attempts:
            self._reply_attempts[key] = (self._clock(),)
            self._record(turn, "replyer", kwargs, f"{turn['turn_id']}:r:{nonce}:{key[1]}")
        # Hosts without before_post_process can still prove an exact unsplit send.
        self._stage_send(kwargs, nonce, turn)
        return turn

    def before_post_process(self, kwargs):
        identity = self._reply_identified(kwargs)
        if identity:
            self._stage_send(kwargs, *identity)
        else:
            # An unrelated reply with identical text makes delivery ambiguous.
            self._stage_send(kwargs, uuid.uuid4().hex, None)

    def _stage_send(self, kwargs, nonce, turn):
        stream = _stream(kwargs)
        body = _canonical_text(kwargs.get("response"))
        if not stream or not body:
            return
        if turn and turn.get("category") != "greeting" and not turn.get("optional_key"):
            turn = None  # Keep it as a possible ambiguous match, never as a greeting.
        mark = _digest(body)
        self._pending_sends[stream, nonce] = (self._clock(), turn, mark, {mark})

    def after_split(self, kwargs):
        stream = _stream(kwargs)
        parts = kwargs.get("segments")
        if not stream or not isinstance(parts, list) or len(parts) > 16:
            return
        texts = [_canonical_text(row.get("text")) for row in parts if isinstance(row, dict)]
        full = _digest("".join(texts))
        matches = [row for key, row in self._pending_sends.items() if key[0] == stream and row[2] == full]
        if len(matches) != 1:
            return  # No reliable linkage when a concurrent reply or a transform changed the text.
        marks = matches[0][3]
        # Maisaka may merge several adjacent segments before it sends.
        for start in range(len(texts)):
            for end in range(start + 1, len(texts) + 1):
                combined = "".join(texts[start:end])
                if combined:
                    marks.add(_digest(combined))

    def _matching_send(self, stream, message, now):
        body = _canonical_text(_outbound_text(message))
        if not stream or not body:
            return None
        mark = _digest(body)
        matches = [(key, row) for key, row in self._pending_sends.items()
                   if key[0] == stream and 0 <= now - row[0] <= 120 and mark in row[3]]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _outbound_timestamp(message):
        try:
            stamp = float(message.get("timestamp"))
        except (ValueError, TypeError, OverflowError):
            return None
        return stamp if 0 < stamp < float("inf") else None

    def after_build_message(self, kwargs):
        """Identify a greeting while the outgoing message still carries the full text."""
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        self._prune()
        now = self._clock()
        stream = _stream(message)
        stamp = self._outbound_timestamp(message)
        if stamp is None:
            return
        matched = self._matching_send(stream, message, now)
        if matched is None:
            return
        key, pending = matched
        turn = pending[1] or {}
        if turn.get("category") == "greeting" and turn.get("greeting_scope") == "target":
            # Post-process plugins can replace the body during this hook and QQ
            # may replace message_id on success. The host keeps the build time.
            self._built_sends[stream, stamp] = now, key

    def confirm_sent(self, kwargs):
        self._prune()
        message = kwargs.get("message")
        if kwargs.get("sent") is not True or not isinstance(message, dict):
            return None
        stream = _stream(message)
        stamp = self._outbound_timestamp(message)
        now = self._clock()
        built = self._built_sends.pop((stream, stamp), None)
        if built is not None:
            key = built[1]
            pending = self._pending_sends.get(key)
            if pending is None or not 0 <= now - pending[0] <= 120:
                return None
        else:
            matched = self._matching_send(stream, message, now)
            if matched is None:
                return None
            key, pending = matched
        self._pending_sends.pop(key, None)
        turn = pending[1]
        if turn is None:
            return None
        turn_id = turn["turn_id"]
        if turn_id in self._sent_turns:
            return None
        self._sent_turns[turn_id] = (now,)
        return turn, stamp if stamp is not None and -5 <= now - stamp <= 120 else now

    def tool_stream(self, proof):
        self._prune()
        row = self._grants.pop(proof, None) if isinstance(proof, str) else None
        return row[1] if row else ""


async def read_host_turn_usage(reader, stream: str, stage: str, minutes: int, limit: int, row_id: int | None = None):
    """Use the host's read-only model; only expose numeric per-invocation receipts."""
    if not stream or not callable(reader) or stage not in ("both", *HOST_REQUESTS):
        return None
    if type(minutes) is not int or not 1 <= minutes <= 1440 or type(limit) is not int or not 1 <= limit <= 20:
        return None
    if row_id is not None and (type(row_id) is not int or not 1 <= row_id <= 2**63 - 1):
        return None
    stages = tuple(HOST_REQUESTS) if stage == "both" else (stage,)
    result = {}
    cutoff = datetime.now().astimezone() - timedelta(minutes=minutes)
    for role in stages:
        filters = {"session_id": stream, "request_type": HOST_REQUESTS[role]}
        if row_id is not None:
            filters["id"] = row_id
        try:
            rows = await reader("ModelUsage", filters=filters, order_by="-id", limit=1 if row_id is not None else limit + 1)
        except Exception:
            return None
        if not isinstance(rows, list):
            return None
        receipts = []
        for row in rows:
            if not isinstance(row, dict) or any(row.get(key) != value for key, value in filters.items()):
                continue
            try:
                stamp = datetime.fromisoformat(str(row.get("timestamp") or "")).astimezone()
            except (ValueError, OSError, OverflowError):
                continue
            if row_id is None and stamp < cutoff:
                continue
            counts = {name: row.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
            if not all(type(value) is int and value >= 0 for value in counts.values()):
                continue
            receipts.append({"id": row.get("id"), "time": stamp.isoformat(timespec="seconds"),
                             "task": str(row.get("task_name") or "")[:100], **counts})
        page = receipts[:limit]
        result[role] = {
            "rows": page, "truncated": len(receipts) > limit,
            "subtotal": {"calls": len(page), **{name: sum(row[name] for row in page)
                for name in ("prompt_tokens", "completion_tokens", "total_tokens")}},
        }
    return result
