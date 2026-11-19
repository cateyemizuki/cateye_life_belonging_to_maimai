"""Request the provider's native Bilibili mini-app card and transmit it as-is.

No local card format is substituted. Acknowledgment alone never proves a card
was delivered. QQ history must contain the same message and provider identity.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterator
from urllib.parse import urlsplit

from integrations.video_web_card import build_bili_miniapp_request

PASSTHROUGH_API = "adapter.napcat.action.call"
_CALL_TIMEOUT_SECONDS = 30.0
_HISTORY_TIMEOUT_SECONDS = 3.0
_HISTORY_DELAYS = (0.5, 1.0)
_HISTORY_COUNT = 16

_QQ_RE = re.compile(r"^[0-9]{5,12}$")
_MESSAGE_ID_RE = re.compile(r"-?[0-9]{1,20}\Z")
_MINIAPP_CARD = "com.tencent.miniapp_01"
_BILI_MINIAPP_ID = "1109937557"
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


def _native_card_identity(card: Any) -> tuple[str, str] | None:
    if not isinstance(card, dict) or card.get("app") != _MINIAPP_CARD:
        return None
    meta, config = card.get("meta"), card.get("config")
    detail = meta.get("detail_1") if isinstance(meta, dict) else None
    if not isinstance(detail, dict) or str(detail.get("appid")) != _BILI_MINIAPP_ID:
        return None
    launch = detail.get("url")
    token = config.get("token") if isinstance(config, dict) else None
    if (not isinstance(launch, str) or any(ch.isspace() for ch in launch)
            or not isinstance(token, str) or not token.strip()):
        return None
    # A provider may omit the scheme. Normalize only for verification; the
    # card sent to QQ retains the original launch string unchanged.
    candidate = "https:" + launch if launch.startswith("//") else (
        "https://" + launch if launch.casefold().startswith("m.q.qq.com/") else launch)
    try:
        url = urlsplit(candidate)
        if (url.scheme != "https" or url.hostname != "m.q.qq.com" or url.port not in (None, 443)
                or url.username is not None or url.password is not None
                or not url.path.startswith("/a/s/") or len(url.path) <= len("/a/s/")):
            return None
    except ValueError:
        return None
    return url.geturl(), token


def _parse_native_card(raw: Any) -> dict | None:
    if isinstance(raw, str):
        if not raw or len(raw) > 65536:
            return None
        try:
            raw = json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return raw if _native_card_identity(raw) else None


def _original_provider_card(response: Any) -> str:
    """Preserve returned JSON text; serialize a provider object without field changes."""
    if isinstance(response, str):
        return response if _parse_native_card(response) is not None else ""
    for row in _response_layers(response):
        if row.get("success") is False or _onebot_failed(row):
            return ""
        if _native_card_identity(row):
            raw = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            return raw if len(raw) <= 65536 else ""
        for field in ("data", "result"):
            raw = row.get(field)
            if isinstance(raw, str) and _parse_native_card(raw) is not None:
                return raw
    return ""


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
    """Match the opaque native launch and token; the server can add display metadata."""
    identity = _native_card_identity(body)
    return identity is not None and identity == _native_card_identity(expected)


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
                 call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS) -> None:
        self._api = api_caller
        self._log = logger
        self._timeout = max(1.0, float(call_timeout_seconds))
        self._active: set[tuple[str, str]] = set()

    async def generate_card(self, *, bvid: str, title: str, desc: str = "",
                            pic_url: str = "") -> Dict[str, Any]:
        """Request a fresh genuine Bilibili mini-app card without sending it."""
        try:
            params = build_bili_miniapp_request(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
        except (TypeError, ValueError):
            return {"success": False, "error_class": "card_generation_failed", "error": "无效的 BV 号"}
        try:
            response = await asyncio.wait_for(self._api(
                PASSTHROUGH_API, action_name="get_mini_app_ark", params=params,
            ), timeout=self._timeout)
            raw = _original_provider_card(response)
        except asyncio.CancelledError:
            raise
        except Exception:
            raw = ""
        if not raw:
            return {"success": False, "error_class": "card_generation_failed",
                    "error": "QQ 未返回可用的哔哩哔哩原生小程序卡片"}
        return {"success": True, "card": {"ark_json": raw}}

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
            # Anchor to the exact send receipt. A fallback-only self echo is
            # rejected by the group event shape check.
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
        self, kind: str, peer: str, account: str, receipt_id: int, expected: Dict[str, Any], started: int,
    ) -> Dict[str, Any]:
        if not receipt_id:
            return self._uncertain("adapter_delivery_unconfirmed")
        history_read = False
        for delay in _HISTORY_DELAYS:
            await asyncio.sleep(delay)
            after = await self._read_history(kind, peer, account, receipt_id if kind == "group" else 0)
            if after is None:
                continue
            history_read = True
            upper = int(time.time()) + 5
            rows = [row for row in after if row.own and row.message_id == receipt_id
                    and started - 2 <= row.time <= upper]
            if len(rows) != 1:
                continue
            row = rows[0]
            if row.kind == "json" and _matches_card(row.body, expected):
                return {"success": True, "channel": "video_card", "delivery_state": "accepted",
                        "delivery_verified": True, "message_id": receipt_id}
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
        raw = card.get("ark_json") if isinstance(card, dict) else None
        expected = _parse_native_card(raw) if isinstance(raw, str) else None
        if expected is None:
            return {"success": False, "not_sent": True, "error_class": "invalid_card"}
        scope = (kind, peer)
        if scope in self._active:
            return {"success": False, "not_sent": True, "error_class": "card_send_in_progress"}
        self._active.add(scope)
        try:
            return await self._send_and_confirm(route, kind, field, peer, raw, expected, before_send)
        finally:
            self._active.discard(scope)

    async def _send_and_confirm(self, route, kind, field, peer, raw, expected, before_send):
        account = await self._logged_in_account()
        expected_account = str(route.get("account_id") or "")
        if not account or (expected_account and account != expected_account):
            return {"success": False, "not_sent": True, "error_class": "route_account_unverified"}
        if before_send is not None:
            allowed, _reason = before_send()
            if not allowed:
                return {"success": False, "not_sent": True, "error_class": "gate_changed"}
        action = "send_group_msg" if kind == "group" else "send_private_msg"
        params = {field: int(peer), "message": [{"type": "json", "data": {"data": raw}}]}
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
        return await self._confirm(kind, peer, account, receipt_id, expected, started)
