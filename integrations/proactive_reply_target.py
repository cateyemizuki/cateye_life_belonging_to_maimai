"""Proactive private turns: planner prompt alignment + replyer target rewrite.

No message lookup, generation or sending happens here. Since PR-PROACTIVE-REPLY
(3.17.2) proactive turns no longer bind reply to a possibly stale context msg_id:
the planner prompt carries proactive-activation semantics, the deep rewrite is
skipped on proactive turns, and the replyer hook replaces the host-rendered
target instruction («你想要回复的消息是…» / «你想要补充说明你自己…») with the
proactive-activation notice. Non-proactive turns are never touched.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
import re
import time
from typing import Any

from core.inject import CATEYE_PLUGIN_ID, PROACTIVE_BLOCK_STALE_MS

_TARGET_MARKER = "【本轮主动回复目标】"
_HEADER = re.compile(r"^\s*(<([a-z_]+)\b[^>]*>)", re.IGNORECASE)
_END_TOOLS = frozenset(("stop", "wait"))

#: PR-PROACTIVE-REPLY（3.17.2）：主动激活轮 replyer 目标指令的替换文案（规格逐字）。
PROACTIVE_REPLY_NOTICE = (
    "本轮回复为插件主动激活，你需要根据上方planner传回的状况概括，"
    "结合你此刻的状态进行回复，不需要特地回复某个信息"
)

#: 宿主目标指令段的前缀（maisaka_generator_base._build_target_message_block；
#: 前缀匹配容忍宿主措辞微调，段边界 = 后续「- 」明细行，遇空行/非明细行即止）。
_HOST_TARGET_PREFIXES = ("你想要回复的消息是", "你想要补充说明你自己")

#: 宿主 replyer 最终用户消息的起始标记（_build_final_user_message 首节「当前时间：」）。
#: 只改写以此开头的文本 part，绝不碰历史消息（带 <message> 头）或旁白/参考文本。
_FINAL_USER_PREFIX = "当前时间："


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
    # PR-PROACTIVE-REPLY（3.17.2）：主动激活轮不再指认具体 msg_id（上下文里的最后
    # 一条来信可能已过时）；改为主动激活语义。target 仍回传供绑定签名使用。
    return (_TARGET_MARKER + "本轮由插件主动激活，你仍可自行决定是否回复。"
            "请根据你的状况概括，结合你此刻的状态自然发言，不需要特地回复某个信息，"
            "也不要围绕某条过时的消息组织回复。如果决定调用reply，msg_id 只能选对方发送的消息；"
            "不要选择自己发送的消息，不要复述自己先前的发言。若决定不回复，不要调用reply。")


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
    """Copy-on-write, replacing only canonical reply tool args.msg_id.

    PR-PROACTIVE-REPLY（3.17.2）起主动激活轮不再调用本函数（强制 msg_id 会让宿主
    渲染过时目标指令）；保留为纯函数 utility 以兼容既有调用面与测试。
    """
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


def latest_incoming_timestamp(items: list[Any]) -> tuple[bool, datetime | None]:
    """最后一条真实来信（<message> 头、非自己消息）的 meta.timestamp。

    返回 (是否存在真实来信, 时间戳)。供 replyer 主动窗口判定做保守让位：
    窗口内出现比触发更新的真实来信时，该轮按用户真实来信回复处理（不改写）；
    来信存在但时间戳不可解析 → (True, None)，调用方同样让位。
    """
    for index in range(len(items) - 1, -1, -1):
        item = items[index]
        if not isinstance(item, dict) or item.get("item_type") != "UserMessageItem":
            continue
        attrs = _header(_text(item), "message")
        if attrs is None or str(attrs.get("is_self_message") or "false").lower() not in ("false", "0"):
            continue
        meta = item.get("meta")
        stamp = meta.get("timestamp") if isinstance(meta, dict) else None
        if not isinstance(stamp, str) or not stamp.strip():
            return True, None
        try:
            return True, datetime.fromisoformat(stamp)
        except ValueError:
            return True, None
    return False, None


def rewrite_host_target_text(text: str, notice: str = PROACTIVE_REPLY_NOTICE) -> tuple[str, int]:
    """把宿主目标指令整段（前缀行 + 后续「- 」明细行）替换为主动激活文案。

    段边界：明细行以「-」起始（quote=/发言内容/你之前的发言内容等），遇空行或
    非明细行即止——不吞并后续 section。前缀匹配容忍宿主措辞微调。无命中时
    原样返回 (text, 0)。``notice`` 缺省 = 3.17.2 原文；PR-PROACTIVE-GATE
    （3.17.14）起 replyer 钩子按 chat_type×kind 传入定制文案。
    """
    lines = text.split("\n")
    out: list[str] = []
    count = 0
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if any(stripped.startswith(prefix) for prefix in _HOST_TARGET_PREFIXES):
            end = index + 1
            while end < len(lines) and lines[end].lstrip().startswith("-"):
                end += 1
            out.append(notice)
            count += 1
            index = end
        else:
            out.append(lines[index])
            index += 1
    return ("\n".join(out), count) if count else (text, 0)


def _is_final_user_text(text: Any) -> bool:
    return isinstance(text, str) and text.lstrip().startswith(_FINAL_USER_PREFIX)


def payload_has_proactive_notice(payload_key: str, payload: list[Any],
                                 notice: str = PROACTIVE_REPLY_NOTICE) -> bool:
    """载荷里是否已含主动激活文案（幂等守卫：避免重试/重复钩子二次追加）。

    ``notice`` 缺省 = 3.17.2 原文；定制文案轮由调用方逐个已知文案检查。
    """
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        if payload_key == "items":
            parts = entry.get("parts")
            if not isinstance(parts, list):
                continue
            texts = [part.get("text") for part in parts
                     if isinstance(part, dict) and part.get("type") == "text"]
        else:
            texts = [entry.get("content")]
        if any(isinstance(text, str) and notice in text for text in texts):
            return True
    return False


def rewrite_replyer_payload(payload_key: str, payload: list[Any],
                            notice: str = PROACTIVE_REPLY_NOTICE) -> tuple[list[Any], int]:
    """Copy-on-write 改写 replyer 载荷里的宿主目标指令段。

    只触碰宿主最终用户消息（「当前时间：」起始的文本）；历史消息（<message> 头）、
    旁白、planner 参考文本一律不碰。返回 (新载荷, 替换段数)；无命中原样返回。
    ``notice`` 缺省 = 3.17.2 原文（3.17.14 起可按 chat_type×kind 定制）。
    """
    updated = list(payload)
    total = 0
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            continue
        if payload_key == "items":
            parts = entry.get("parts")
            if not isinstance(parts, list):
                continue
            new_parts = None
            for part_index, part in enumerate(parts):
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                if not _is_final_user_text(part.get("text")):
                    continue
                new_text, count = rewrite_host_target_text(part["text"], notice)
                if count:
                    if new_parts is None:
                        new_parts = list(parts)
                    new_parts[part_index] = {**part, "text": new_text}
                    total += count
            if new_parts is not None:
                updated[index] = {**entry, "parts": new_parts}
        elif payload_key == "messages":
            content = entry.get("content")
            if not _is_final_user_text(content):
                continue
            new_text, count = rewrite_host_target_text(content, notice)
            if count:
                updated[index] = {**entry, "content": new_text}
                total += count
    return (updated, total) if total else (payload, 0)


def append_proactive_notice(payload_key: str, payload: list[Any],
                            notice: str = PROACTIVE_REPLY_NOTICE) -> list[Any] | None:
    """替换失败/未找到时的保守尾随锚点：把主动激活文案追加到最后一条文本末尾。

    不破坏原 prompt（copy-on-write）；载荷没有可追加的文本条目时返回 None。
    ``notice`` 缺省 = 3.17.2 原文（3.17.14 起可按 chat_type×kind 定制）。
    """
    for index in range(len(payload) - 1, -1, -1):
        entry = payload[index]
        if not isinstance(entry, dict):
            continue
        if payload_key == "items":
            parts = entry.get("parts")
            if not isinstance(parts, list):
                continue
            for part_index in range(len(parts) - 1, -1, -1):
                part = parts[part_index]
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                    new_parts = list(parts)
                    new_parts[part_index] = {
                        **part, "text": part["text"].rstrip() + "\n\n" + notice,
                    }
                    updated = list(payload)
                    updated[index] = {**entry, "parts": new_parts}
                    return updated
        elif payload_key == "messages":
            content = entry.get("content")
            if isinstance(content, str):
                updated = list(payload)
                updated[index] = {**entry, "content": content.rstrip() + "\n\n" + notice}
                return updated
    return None


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
        # PR-PROACTIVE-REPLY（3.17.2）：task_id 命中即插件主动激活轮——不再强制
        # reply 的 msg_id 指向上下文里可能过时的用户消息（宿主会据该 msg_id 渲染
        # 「你想要回复的消息是…」过时目标指令）。目标指令改写改由 replyer
        # before_model_request 钩子完成；deep 绑定管线保留以兼容配置与钩子契约。
        return None, 0
