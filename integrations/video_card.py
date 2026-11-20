"""Send Bilibili copy links or official QQ illustrated cards with a scoped receipt."""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterator

import httpx

from integrations.video_web_card import (
    COPY_LINK,
    WEB_CARD,
    bili_copy_short_link,
    bili_video_metadata,
    build_bili_web_card,
    format_bili_copy_share,
    valid_bili_copy_url,
)

PASSTHROUGH_API = "adapter.napcat.action.call"
_CALL_TIMEOUT_SECONDS = 30.0
_GROUP_HISTORY_TIMEOUT_SECONDS = 8.0
_PRIVATE_HISTORY_TIMEOUT_SECONDS = 3.0
_HISTORY_DELAYS = (0.5, 1.0)
_HISTORY_COUNT = 16

_QQ_RE = re.compile(r"^[0-9]{5,12}$")
_MESSAGE_ID_RE = re.compile(r"-?[0-9]{1,20}\Z")
_QQ_WEB_CARD = "com.tencent.tuwen.lua"
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
    if not isinstance(card, dict):
        return None
    mode = card.get("format")
    bvid, title = card.get("bvid"), card.get("title")
    if not isinstance(bvid, str) or not isinstance(title, str):
        return None
    try:
        bvid, title = bili_video_metadata(bvid, title)
    except ValueError:
        return None
    if mode == COPY_LINK:
        url = card.get("url")
        if not isinstance(url, str) or not valid_bili_copy_url(url, bvid):
            return None
        return {"format": mode, "text": format_bili_copy_share(title, url)}
    if mode == WEB_CARD:
        return {"format": mode, **build_bili_web_card(
            bvid=bvid, title=title, desc=card.get("desc", ""), pic_url=card.get("preview_url", ""))}
    return None


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


def _matches_card(body: Any, expected: dict) -> bool:
    if not isinstance(body, dict) or body.get("app") != _QQ_WEB_CARD:
        return False
    meta = body.get("meta")
    news = meta.get("news") if isinstance(meta, dict) else None
    # QQ can shorten the title in the recorded card. The exact mobile destination
    # identifies the requested video within the scoped, newly observed message.
    return isinstance(news, dict) and news.get("jumpUrl") == expected["jump_url"]


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
    if not isinstance(sender, dict) or str(sender.get("user_id")) != user:
        return None
    if kind == "group" and (sender.get("role") not in ("member", "admin", "owner")
                            or "anonymous" not in row):
        # SnowLuma's raw send also stores a local self echo, but that
        # synthetic row has neither a group role nor the anonymous field.
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
                 call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS, *,
                 format_provider: Callable[[], str] | None = None) -> None:
        self._api = api_caller
        self._log = logger
        self._timeout = max(1.0, float(call_timeout_seconds))
        self._format = format_provider or (lambda: COPY_LINK)
        self._active: set[tuple[str, str]] = set()

    async def generate_card(self, *, bvid: str, title: str, desc: str = "",
                            pic_url: str = "") -> Dict[str, Any]:
        """Prepare the current share format without sending a message."""
        try:
            bvid, title = bili_video_metadata(bvid, title)
        except ValueError:
            return {"success": False, "error_class": "card_generation_failed", "reason": "invalid_bvid"}
        if self._format() == WEB_CARD:
            card = build_bili_web_card(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
            return {"success": True, "card": {"format": WEB_CARD, "bvid": bvid, **card}}
        url = f"https://www.bilibili.com/video/{bvid}"
        try:
            url = await asyncio.wait_for(bili_copy_short_link(bvid), timeout=min(self._timeout, 12.0))
        except (httpx.HTTPError, ValueError, OSError, asyncio.TimeoutError) as exc:
            logger = getattr(self._log, "debug", None)
            if callable(logger):
                logger("B 站短链暂不可用，使用同一视频的长链：%s", type(exc).__name__)
        return {"success": True, "card": {"format": COPY_LINK, "bvid": bvid, "title": title, "url": url}}

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
        is_group = kind == "group"
        action = "get_group_msg_history" if is_group else "get_friend_msg_history"
        field = "group_id" if is_group else "user_id"
        timeout = _GROUP_HISTORY_TIMEOUT_SECONDS if is_group else _PRIVATE_HISTORY_TIMEOUT_SECONDS
        params = {field: int(peer), "count": _HISTORY_COUNT}
        if anchor:
            # SnowLuma's unanchored private roaming can omit illustrated cards.
            # An authoritative pre-send anchor uses QQ's conversation history.
            params.update(message_id=anchor, reverse_order=False)
        try:
            response = await asyncio.wait_for(self._api(PASSTHROUGH_API, action_name=action, params=params),
                                              timeout=min(self._timeout, timeout))
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
        self, kind: str, peer: str, account: str, descriptor: dict, started: int,
        *, receipt_id: int, before: list[_HistoryMessage] | None,
    ) -> Dict[str, Any]:
        link = descriptor["format"] == COPY_LINK
        if link and not receipt_id:
            return self._uncertain("adapter_delivery_unconfirmed")
        if not link and before is None:
            return self._uncertain("card_history_unavailable")
        known_ids = {row.message_id for row in before or ()}
        anchor = max(before or (), key=lambda row: (row.time, row.sequence), default=None)
        anchor_id = (receipt_id if kind == "group" else 0) if link else (anchor.message_id if anchor else 0)
        history_read = False
        observed_own: dict[int, _HistoryMessage] = {}
        for delay in _HISTORY_DELAYS:
            await asyncio.sleep(delay)
            rows = await self._read_history(kind, peer, account, anchor_id)
            if rows is None:
                continue
            history_read = True
            upper = int(time.time()) + 5
            for row in rows:
                if not row.own or not started - 2 <= row.time <= upper:
                    continue
                if link:
                    if row.message_id == receipt_id:
                        observed_own[row.message_id] = row
                elif (row.message_id not in known_ids
                      and (kind != "group" or anchor is None or row.sequence > anchor.sequence)):
                    observed_own[row.message_id] = row
            if link:
                matches = [row for row in observed_own.values()
                           if row.kind == "text" and row.body == descriptor["text"]]
            else:
                matches = [row for row in observed_own.values()
                           if row.kind == "json" and _matches_card(row.body, descriptor)]
            if len(matches) == 1:
                return {"success": True, "channel": "video_link" if link else "video_card",
                        "delivery_state": "accepted", "delivery_verified": True,
                        "message_id": matches[0].message_id}
            if len(matches) > 1:
                break
        if not link and len(observed_own) == 1:
            row = next(iter(observed_own.values()))
            if row.kind == "text" and row.body == _QQ_PLACEHOLDER:
                return self._uncertain("qq_card_replaced")
        reason = "card_not_confirmed" if history_read else "card_history_unavailable"
        result = self._uncertain(reason)
        return {**result, "message_id": receipt_id} if receipt_id else result

    def _uncertain(self, code: str) -> Dict[str, Any]:
        details = {
            "card_not_confirmed": "QQ 历史中未能核验这张视频分享",
            "card_history_unavailable": "暂时无法读取 QQ 会话历史确认卡片",
            "qq_card_replaced": "QQ 历史中出现了版本不支持的占位提示，分享未获确认",
            "adapter_delivery_unconfirmed": "QQ 未返回可核验的视频分享回执",
        }
        logger = getattr(self._log, "debug", None)
        if callable(logger):
            logger(f"视频分享确认结果：{code}")
        return {"success": False, "uncertain": True, "error_class": code,
                "error": details.get(code, "视频分享状态待确认")}

    async def send_card(self, route: Dict[str, Any], card: Dict[str, Any], *, before_send: Any = None) -> Dict[str, Any]:
        """Send once, then correlate a new independent QQ chat event."""
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
        expected_account = str(route.get("account_id") or "")
        if not account or (expected_account and account != expected_account):
            return {"success": False, "not_sent": True, "error_class": "route_account_unverified"}
        link = descriptor["format"] == COPY_LINK
        if link:
            before = None
            action = "send_group_msg" if kind == "group" else "send_private_msg"
            params = {field: int(peer), "message": [{"type": "text", "data": {"text": descriptor["text"]}}]}
        else:
            before = await self._read_history(kind, peer, account)
            action = "send_tuwen_ark"
            params = {field: int(peer), **{key: value for key, value in descriptor.items() if key != "format"}}
        if before_send is not None:
            allowed, _reason = before_send()
            if not allowed:
                return {"success": False, "not_sent": True, "error_class": "gate_changed"}
        started = int(time.time())
        try:
            response = await asyncio.wait_for(self._api(
                PASSTHROUGH_API, action_name=action, params=params,
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
        receipt_id = 0
        for layer in _response_layers(response):
            if _onebot_failed(layer):
                return {"success": False, "not_sent": True, "error_class": "send_rejected"}
            if layer.get("success") is False:
                return self._uncertain("adapter_delivery_unconfirmed")
            retcode = layer.get("retcode")
            accepted = (accepted or layer.get("status") == "ok" or (type(retcode) is int and retcode == 0)
                        or layer.get("success") is True or layer.get("sent") is True)
            receipt_id = _qq_message_id(layer.get("message_id")) or receipt_id
        if not accepted:
            return self._uncertain("adapter_delivery_unconfirmed")
        return await self._confirm(kind, peer, account, descriptor, started, receipt_id=receipt_id, before=before)
