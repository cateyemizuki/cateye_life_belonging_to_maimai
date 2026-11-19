"""Send a server-generated QQ link card and independently confirm the chat event.

The dedicated SnowLuma Tuwen action supplies no message ID and does not create
a synthetic local send row. An action acknowledgment alone is not a confirmed
card delivery and never triggers a retry through another format.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterator

from integrations.video_web_card import build_bili_web_card

PASSTHROUGH_API = "adapter.napcat.action.call"
_CALL_TIMEOUT_SECONDS = 30.0
_HISTORY_TIMEOUT_SECONDS = 3.0
_HISTORY_DELAYS = (0.5, 1.0)
_HISTORY_COUNT = 16

_QQ_RE = re.compile(r"^[0-9]{5,12}$")
_MESSAGE_ID_RE = re.compile(r"-?[0-9]{1,20}\Z")
_VIDEO_URL_RE = re.compile(r"https://www\.bilibili\.com/video/(BV[0-9A-Za-z]{10})\Z")
_QQ_PLACEHOLDER = "发送者版本过低，无法展示内容"

ApiCaller = Callable[..., Awaitable[Any]]


def _response_layers(response: Any) -> Iterator[Dict[str, Any]]:
    """Walk bounded OneBot and SDK envelopes without trusting unrelated nested data."""
    current = response
    for _ in range(6):
        if not isinstance(current, dict):
            break
        yield current
        if isinstance(current.get("data"), dict):
            current = current["data"]
        elif current.get("success") is True and isinstance(current.get("result"), dict):
            current = current["result"]
        else:
            break


def _onebot_failed(reply: Dict[str, Any]) -> bool:
    retcode = reply.get("retcode")
    return reply.get("status") == "failed" or (type(retcode) is int and retcode != 0)


def _receipt_error(response: Any) -> str:
    messages = " ".join(
        value.casefold() for row in _response_layers(response)
        for field in ("error", "wording", "message", "msg") if isinstance(value := row.get(field), str)
    )
    if any(mark in messages for mark in ("unknown action", "action not found", "unsupported action",
                                         "不支持的动作", "未找到动作", "未找到 api")):
        return "card_action_unsupported"
    if any(mark in messages for mark in ("no message receipt", "delivery was not confirmed",
                                         "投递未确认", "timed out", "timeout", "超时", "no response")):
        return "adapter_delivery_unconfirmed"
    return ""


def _qq_message_id(value: Any) -> int:
    if type(value) not in (int, str):
        return 0
    text = str(value)
    return int(text) if _MESSAGE_ID_RE.fullmatch(text) else 0


def _validated_descriptor(card: Any) -> dict | None:
    if not isinstance(card, dict) or not isinstance(card.get("jump_url"), str):
        return None
    match = _VIDEO_URL_RE.fullmatch(card["jump_url"])
    if match is None:
        return None
    try:
        canonical = build_bili_web_card(bvid=match[1], title=card.get("title", ""),
                                        desc=card.get("desc", ""), pic_url=card.get("preview_url", ""))
    except (TypeError, ValueError):
        return None
    return canonical if canonical == card else None


def _read_segment(message: Any) -> tuple[str, Any]:
    if not isinstance(message, list) or len(message) != 1 or not isinstance(message[0], dict):
        return "", None
    kind, data = message[0].get("type"), message[0].get("data")
    if not isinstance(data, dict):
        return "", None
    if kind == "text":
        return kind, data.get("text")
    body = data.get("data")
    if kind != "json":
        return "", None
    if isinstance(body, str) and len(body) <= 65536:
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            return "", None
    return "json", body if isinstance(body, dict) else None


def _matches_card(body: Any, descriptor: dict) -> bool:
    """QQ may add signature/preview fields. Compare title and URL in one card object."""
    if not isinstance(body, dict):
        return False
    queue: list[tuple[Any, int]] = [(body, 0)]
    for _ in range(60):
        if not queue:
            return False
        node, level = queue.pop()
        if isinstance(node, dict):
            if (node.get("title") == descriptor["title"] and
                    any(node.get(key) == descriptor["jump_url"] for key in ("jumpUrl", "jump_url", "url"))):
                return True
            values = node.values()
        elif isinstance(node, list):
            values = node
        else:
            continue
        if level < 5:
            queue.extend((val, level + 1) for val in list(values)[:24] if isinstance(val, (dict, list)))
    return False


@dataclass(frozen=True)
class _HistoryMessage:
    message_id: int
    sequence: int
    time: int
    own: bool
    kind: str
    body: Any


def _scoped_history_message(row: Any, kind: str, peer: str, account: str) -> _HistoryMessage | None:
    if not isinstance(row, dict) or row.get("message_type") != kind:
        return None
    user = str(row.get("user_id") or "")
    own = user == account
    if kind == "group" and str(row.get("group_id")) != peer:
        return None
    if kind == "private" and ((own and str(row.get("target_id")) != peer) or (not own and user != peer)):
        return None
    sender = row.get("sender")
    if isinstance(sender, dict) and str(sender.get("user_id")) != user:
        return None
    message_id = _qq_message_id(row.get("message_id"))
    if row.get("real_id") is not None and _qq_message_id(row["real_id"]) != message_id:
        return None
    stamp, sequence = row.get("time"), row.get("message_seq")
    if not message_id or type(stamp) is not int or type(sequence) is not int or sequence <= 0:
        return None
    payload_kind, body = _read_segment(row.get("message"))
    return _HistoryMessage(message_id, sequence, stamp, own, payload_kind, body)


class VideoCardService:
    def __init__(self, api_caller: ApiCaller, logger: Any = None,
                 call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS) -> None:
        self._api = api_caller
        self._log = logger
        self._timeout = max(1.0, float(call_timeout_seconds))
        self._active: set[tuple[str, str]] = set()

    async def generate_card(self, *, bvid: str, title: str, desc: str = "",
                            pic_url: str = "") -> Dict[str, Any]:
        """Prepare data for SnowLuma's dedicated QQ Tuwen action."""
        try:
            card = build_bili_web_card(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error_class": "card_generation_failed",
                    "error": f"卡片生成失败：{exc}"}
        return {"success": True, "card": card}

    async def send_video_card(self, *, user_id: Any, bvid: str, title: str,
                              desc: str = "", pic_url: str = "") -> Dict[str, Any]:
        user = str(user_id or "").strip()
        if not _QQ_RE.fullmatch(user):
            return {"success": False, "error_class": "invalid_target",
                    "error": "卡片收件人 QQ 号无效"}
        generated = await self.generate_card(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
        if not generated.get("success"):
            return generated
        receipt = await self.send_card({"kind": "private", "user_id": user}, generated["card"])
        return {**receipt, "bvid": bvid}

    async def _logged_in_account(self) -> str:
        try:
            response = await asyncio.wait_for(self._api(PASSTHROUGH_API, action_name="get_login_info", params={}),
                                              timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            return ""
        for layer in _response_layers(response):
            if layer.get("success") is False or _onebot_failed(layer):
                return ""
            account = str(layer.get("user_id") or "")
            if _QQ_RE.fullmatch(account):
                return account
        return ""

    async def _read_history(self, kind: str, peer: str, account: str,
                            anchor: int = 0) -> list[_HistoryMessage] | None:
        action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
        field = "group_id" if kind == "group" else "user_id"
        params = {field: int(peer), "count": _HISTORY_COUNT}
        if kind == "group" and anchor:
            # Group history otherwise walks back from its local authoritative
            # anchor. Private roaming is only server-backed when unanchored.
            params.update(message_id=anchor, reverse_order=False)
        try:
            response = await asyncio.wait_for(self._api(PASSTHROUGH_API, action_name=action, params=params),
                                              timeout=min(self._timeout, _HISTORY_TIMEOUT_SECONDS))
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        for layer in _response_layers(response):
            if layer.get("success") is False or _onebot_failed(layer):
                return None
            messages = layer.get("messages")
            if not isinstance(messages, list):
                continue
            rows = [_scoped_history_message(row, kind, peer, account) for row in messages[:_HISTORY_COUNT]]
            valid = [row for row in rows if row is not None]
            return valid if valid or not messages else None
        return None

    async def _confirm(
        self, kind: str, peer: str, account: str, before: list[_HistoryMessage] | None,
        descriptor: Dict[str, Any], started: int,
    ) -> Dict[str, Any]:
        if before is None:
            return self._uncertain("card_history_unavailable")
        known_ids = {row.message_id for row in before}
        anchor = max(before, key=lambda row: row.sequence, default=None) if kind == "group" else None
        history_read = False
        observed_own: dict[int, _HistoryMessage] = {}
        for delay in _HISTORY_DELAYS:
            await asyncio.sleep(delay)
            after = await self._read_history(kind, peer, account, anchor.message_id if anchor else 0)
            if after is None:
                continue
            history_read = True
            upper = int(time.time()) + 5
            for row in after:
                if (row.own and row.message_id not in known_ids and started - 2 <= row.time <= upper
                        and (anchor is None or row.sequence > anchor.sequence)):
                    observed_own[row.message_id] = row
            matches = [row for row in observed_own.values()
                       if row.kind == "json" and _matches_card(row.body, descriptor)]
            if len(matches) == 1:
                return {"success": True, "channel": "video_card", "delivery_state": "accepted",
                        "delivery_verified": True, "message_id": matches[0].message_id}
            if len(matches) > 1:
                return self._uncertain("card_not_confirmed")
        if len(observed_own) == 1:
            row = next(iter(observed_own.values()))
            if row.kind == "text" and row.body == _QQ_PLACEHOLDER:
                return self._uncertain("qq_card_replaced")
        return self._uncertain("card_not_confirmed" if history_read else "card_history_unavailable")

    def _uncertain(self, code: str) -> Dict[str, Any]:
        details = {
            "card_not_confirmed": "QQ 历史中未能核验这张视频卡片",
            "card_history_unavailable": "暂时无法读取 QQ 会话历史确认卡片",
            "qq_card_replaced": "QQ 历史中出现了版本不支持的占位提示，原卡未获确认",
            "adapter_delivery_unconfirmed": "QQ 未返回可核验的视频卡片回执",
        }
        logger = getattr(self._log, "debug", None)
        if callable(logger):
            logger(f"视频卡片确认结果：{code}")
        return {"success": False, "uncertain": True, "error_class": code,
                "error": details.get(code, "视频卡片状态待确认")}

    async def send_card(self, route: Dict[str, Any], card: Dict[str, Any], *, before_send: Any = None) -> Dict[str, Any]:
        """Send one native card only, then correlate a new independent QQ chat event."""
        kind = route.get("kind")
        field = "group_id" if kind == "group" else "user_id"
        peer = str(route.get(field) or "")
        if kind not in ("group", "private") or not _QQ_RE.fullmatch(peer):
            return {"success": False, "not_sent": True, "error_class": "invalid_target"}
        descriptor = _validated_descriptor(card)
        if descriptor is None:
            return {"success": False, "not_sent": True, "error_class": "invalid_card"}
        scope = (kind, peer)
        if scope in self._active:
            return {"success": False, "not_sent": True, "error_class": "card_send_in_progress"}
        self._active.add(scope)
        try:
            return await self._send_and_confirm(route, kind, field, peer, descriptor, before_send)
        finally:
            self._active.discard(scope)

    async def _send_and_confirm(self, route, kind, field, peer, descriptor, before_send):
        account = await self._logged_in_account()
        expected = str(route.get("account_id") or "")
        if not account or (expected and account != expected):
            return {"success": False, "not_sent": True, "error_class": "route_account_unverified"}
        before = await self._read_history(kind, peer, account)
        if before_send is not None:
            allowed, _reason = before_send()
            if not allowed:
                return {"success": False, "not_sent": True, "error_class": "gate_changed"}
        params = {field: int(peer), **descriptor}
        started = int(time.time())
        try:
            response = await asyncio.wait_for(self._api(
                PASSTHROUGH_API, action_name="send_tuwen_ark", params=params,
            ), timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = _receipt_error({"error": str(exc)})
            if error == "card_action_unsupported":
                return {"success": False, "not_sent": True, "error_class": error}
            return self._uncertain(error or "adapter_delivery_unconfirmed")
        error = _receipt_error(response)
        if error == "card_action_unsupported":
            return {"success": False, "not_sent": True, "error_class": error}
        if error:
            return self._uncertain(error)
        accepted = False
        for layer in _response_layers(response):
            if _onebot_failed(layer):
                return {"success": False, "not_sent": True, "error_class": "send_rejected"}
            if layer.get("success") is False:
                return self._uncertain("adapter_delivery_unconfirmed")
            retcode = layer.get("retcode")
            accepted = accepted or layer.get("status") == "ok" or (type(retcode) is int and retcode == 0)
        if not accepted:
            return self._uncertain("adapter_delivery_unconfirmed")
        return await self._confirm(kind, peer, account, before, descriptor, started)
