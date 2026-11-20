"""Proactive private turns: planner prompt alignment + replyer target rewrite.

No message lookup, generation or sending happens here. Since PR-PROACTIVE-REPLY
(3.17.2) proactive turns no longer bind reply to a possibly stale context msg_id:
the planner prompt carries proactive-activation semantics. The replyer hook
can replace the target in a standalone legacy instruction frame. Newer host
frames combine dialogue and reply instructions in the same item, so we keep the
whole frame intact and attach the proactive notice at its end.
"""
from __future__ import annotations

from collections.abc import Iterator
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

#: 主动激活轮的回复目标提示。
PROACTIVE_REPLY_NOTICE = (
    "这轮是你主动找对方发消息，结合下面的局面说明和你此刻的状态自然开口，"
    "不用硬去回复前面已经过时的旧消息"
)

#: MaiBot 旧版 `_build_final_user_message` 将时钟与目标分段，并将历史放在独立项中。
_HOST_TARGET_PREFIXES = ("你想要回复的消息是", "你想要补充说明你自己")
_LEGACY_TIME_LINE = re.compile(r"当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_LEGACY_TRAILING_STYLES = frozenset({
    "请简短的回复，允许句子残缺，奇怪表达，倒装，省略，符合口语习惯，符合省力随意回复习惯",
    "可以针对问题做出较为详细的评论和说明",
})
_NON_FINAL_USER_PREFIXES = (
    "<message", "<plugin_proactive_task", "<lbm-context", "<focus_reference", "[参考消息]",
)


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
    return (_TARGET_MARKER + "这轮是你主动找对方说话，不用硬接前面过时的旧消息。"
            "结合当前状态自然决定要不要发；调用 reply 时 msg_id 只能选对方发过的消息，"
            "不复述自己之前的话；不合适就直接结束。")


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


def _split_legacy_final_text(text: str) -> list[str]:
    """Split a recognized standalone legacy frame, leaving unknown layouts intact."""
    sections = text.split("\n\n", 2)
    if len(sections) == 3 and _LEGACY_TIME_LINE.fullmatch(sections[0].strip()):
        return sections
    return []


def rewrite_host_target_text(text: str, notice: str = PROACTIVE_REPLY_NOTICE) -> tuple[str, int]:
    """Only replace the target section immediately after a legacy standalone clock.

    Modern host templates combine free-form dialogue, analysis and the actual
    target in one text body. User messages can reproduce every visible anchor.
    Their target slot must not be inferred from rendered text.
    """
    sections = _split_legacy_final_text(text)
    if not sections:
        return text, 0
    heading, _, details = sections[1].partition("\n")
    if not heading.startswith(_HOST_TARGET_PREFIXES):
        return text, 0
    if any(not line.startswith("- ") for line in details.splitlines()):
        return text, 0
    return "\n\n".join((sections[0], notice, sections[2])), 1


@dataclass(frozen=True)
class _UserText:
    entry_index: int
    field: str
    text: str
    part_index: int | None = None
    single_text: bool = True

    def replace(self, payload: list[Any], value: str) -> list[Any]:
        entry = payload[self.entry_index]
        content: Any = value
        if self.part_index is not None:
            parts = list(entry[self.field])
            parts[self.part_index] = {**parts[self.part_index], "text": value}
            content = parts
        result = list(payload)
        result[self.entry_index] = {**entry, self.field: content}
        return result


def _user_texts(payload_key: str, payload: list[Any]) -> Iterator[_UserText]:
    """Find user text without touching a system, tool, image or tagged history item."""
    if payload_key == "items":
        role_field, user_role, field = "item_type", "UserMessageItem", "parts"
    elif payload_key == "messages":
        role_field, user_role, field = "role", "user", "content"
    else:
        return

    for index in range(len(payload) - 1, -1, -1):
        entry = payload[index]
        if not isinstance(entry, dict) or entry.get(role_field) != user_role:
            continue
        content = entry.get(field)
        if payload_key == "messages" and isinstance(content, str):
            if not content.lstrip().startswith(_NON_FINAL_USER_PREFIXES):
                yield _UserText(index, field, content)
        elif isinstance(content, list):
            texts = [(i, part["text"]) for i, part in enumerate(content)
                     if isinstance(part, dict) and part.get("type") == "text"
                     and isinstance(part.get("text"), str)]
            first_text = next((value for _, value in texts if value.strip()), "")
            if not texts or first_text.lstrip().startswith(_NON_FINAL_USER_PREFIXES):
                continue
            last_index, last_text = texts[-1]
            yield _UserText(index, field, last_text, last_index, len(content) == 1)


def _legacy_user_text(payload_key: str, payload: list[Any]) -> _UserText | None:
    entries = _user_texts(payload_key, payload)
    entry = next(entries, None)
    # Older hosts optionally emit a separate final user item for requested style.
    if entry is not None and entry.single_text and entry.text in _LEGACY_TRAILING_STYLES:
        entry = next(entries, None)
    return entry if entry is not None and entry.single_text else None


def payload_has_proactive_notice(payload_key: str, payload: list[Any],
                                 notice: str = PROACTIVE_REPLY_NOTICE) -> bool:
    """Identify our notice in its final placement, without searching pasted history."""
    final = next(_user_texts(payload_key, payload), None)
    if final is None:
        return False
    if final.text == notice or final.text.rstrip().endswith("\n\n" + notice):
        return True
    legacy = _legacy_user_text(payload_key, payload)
    sections = _split_legacy_final_text(legacy.text) if legacy is not None else []
    return bool(sections and sections[1] == notice)


def rewrite_replyer_payload(payload_key: str, payload: list[Any],
                            notice: str = PROACTIVE_REPLY_NOTICE, *,
                            legacy_frame: bool = True) -> tuple[list[Any], int]:
    """Replace a proven legacy target section, preserving all other parts."""
    if not legacy_frame:
        return payload, 0
    entry = _legacy_user_text(payload_key, payload)
    if entry is None:
        return payload, 0
    text, count = rewrite_host_target_text(entry.text, notice)
    return (entry.replace(payload, text), count) if count else (payload, 0)


def append_proactive_notice(payload_key: str, payload: list[Any],
                            notice: str = PROACTIVE_REPLY_NOTICE) -> list[Any] | None:
    """Use the non-destructive fallback for current Replyer frames."""
    if payload_has_proactive_notice(payload_key, payload, notice):
        return None
    entry = next(_user_texts(payload_key, payload), None)
    if entry is None:
        return None
    return entry.replace(payload, entry.text + "\n\n" + notice)


class ProactiveReplyTargets:
    """Pair request/output hooks conservatively, with no sticky per-user target.

    The installed host supplies the same selection fields to both hooks. Message
    counts after injection can change, so built_message_count is deliberately not
    a correlation key. Overlapping requests with different bindings are never
    rewritten; identical target/mode bindings remain safe after cancellation.
    """


    def before(self, kwargs: dict[str, Any], *, stream_id: str, target_user: str,
               now_ms: float | None = None) -> dict[str, Any] | None:
        """主动任务只附加真实激活提示，不重写回复目标。"""
        items = kwargs.get("items")
        if not (stream_id and target_user and kwargs.get("session_id") == stream_id and isinstance(items, list)):
            return None
        updated, _target, _task_id = prepare_request(items, stream_id, target_user, now_ms=now_ms)
        return {**kwargs, "items": updated} if updated is not items else None

    def after(self, kwargs: dict[str, Any], *, stream_id: str, target_user: str) -> tuple[dict[str, Any] | None, int]:
        """保留钩子接口；目标指令由 replyer 的专用钩子处理。"""
        return None, 0
