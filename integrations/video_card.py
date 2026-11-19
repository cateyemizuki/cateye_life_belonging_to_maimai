"""Build one standard Bilibili link card and transport it through OneBot.

The JSON segment goes via the adapter's raw action. Private sends are confirmed
against unanchored QQ friend history, because SnowLuma can assign a local ID
before receiving a real private sequence. No result triggers a second send.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable, Dict, Iterator

from integrations.video_web_card import build_bili_web_card

PASSTHROUGH_API = "adapter.napcat.action.call"
_CALL_TIMEOUT_SECONDS = 30.0
_PRIVATE_HISTORY_TIMEOUT_SECONDS = 3.0
_PRIVATE_HISTORY_DELAYS_SECONDS = (0.4, 0.9)
_PRIVATE_HISTORY_COUNT = 12

_QQ_RE = re.compile(r"^[0-9]{5,12}$")
_MESSAGE_ID_RE = re.compile(r"-?[0-9]{1,20}\Z")

ApiCaller = Callable[..., Awaitable[Any]]


def _response_layers(response: Any) -> Iterator[Dict[str, Any]]:
    """Walk bounded OneBot and SDK envelopes without accepting arbitrary nested data."""
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


def _adapter_delivery_unconfirmed(value: Any) -> bool:
    """Recognize SnowLuma's explicit no-receipt state, including the host's error wrapper."""
    if not isinstance(value, str):
        return False
    message = value.casefold()
    return ("qq returned no message receipt" in message
            or "group message delivery was not confirmed" in message
            or "群消息投递未确认" in message)


def _unconfirmed_adapter_receipt() -> Dict[str, Any]:
    return {"success": False, "uncertain": True, "error_class": "adapter_delivery_unconfirmed",
            "error": "QQ 未返回可核验的消息回执，投递结果待确认"}


def _onebot_failed(reply: Dict[str, Any]) -> bool:
    retcode = reply.get("retcode")
    return reply.get("status") == "failed" or (type(retcode) is int and retcode != 0)


def _qq_message_id(value: Any) -> str:
    if type(value) not in (int, str):
        return ""
    text = str(value)
    return str(int(text)) if _MESSAGE_ID_RE.fullmatch(text) and int(text) else ""


def _same_card(message: Any, card: Dict[str, Any]) -> bool:
    if not isinstance(message, list):
        return False
    segments = [item for item in message if isinstance(item, dict) and item.get("type") == "json"]
    if len(segments) != 1:
        return False
    data = segments[0].get("data")
    body = data.get("data") if isinstance(data, dict) else None
    if isinstance(body, str) and len(body) <= 65536:
        try:
            body = json.loads(body)
        except (ValueError, RecursionError):
            return False
    return isinstance(body, dict) and body == card


class VideoCardService:
    """B站卡片生成及已核验群/私聊路由；不拥有授权、额度或防重状态。"""

    def __init__(self, api_caller: ApiCaller, logger: Any = None,
                 call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS) -> None:
        self._api = api_caller
        self._log = logger
        self._timeout = max(1.0, float(call_timeout_seconds))

    async def generate_ark(self, *, bvid: str, title: str, desc: str = "",
                           pic_url: str = "") -> Dict[str, Any]:
        """在本地构造 QQ 标准网页分享卡片，正文落到规范 BV 视频页。"""
        try:
            card = build_bili_web_card(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
            ark_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return {"success": False, "error_class": "card_generation_failed",
                    "error": f"卡片生成失败：{exc}"}
        return {"success": True, "ark_json": ark_json}

    async def send_video_card(self, *, user_id: Any, bvid: str, title: str,
                              desc: str = "", pic_url: str = "") -> Dict[str, Any]:
        """生成并发送视频卡片到指定 QQ 私聊（收件人由装配侧决定）。"""
        user = str(user_id or "").strip()
        if not _QQ_RE.fullmatch(user):
            return {"success": False, "error_class": "invalid_target",
                    "error": "卡片收件人 QQ 号无效，不发送"}
        generated = await self.generate_ark(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
        if not generated.get("success"):
            return generated
        receipt = await self.send_ark({"kind": "private", "user_id": user}, generated["ark_json"])
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

    async def _private_card_history(self, peer: str, account: str,
                                    card: Dict[str, Any]) -> dict[str, int] | None:
        """Read one roaming page. An anchored query could silently return local echoes."""
        try:
            response = await asyncio.wait_for(self._api(
                PASSTHROUGH_API, action_name="get_friend_msg_history",
                params={"user_id": int(peer), "count": _PRIVATE_HISTORY_COUNT},
            ), timeout=min(self._timeout, _PRIVATE_HISTORY_TIMEOUT_SECONDS))
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
            matches: dict[str, int] = {}
            for row in messages[:_PRIVATE_HISTORY_COUNT]:
                if (not isinstance(row, dict) or row.get("message_type") != "private"
                        or str(row.get("user_id")) != account or str(row.get("target_id")) != peer):
                    continue
                sender = row.get("sender")
                if isinstance(sender, dict) and str(sender.get("user_id")) != account:
                    continue
                receipt_id = _qq_message_id(row.get("message_id"))
                if row.get("real_id") is not None and _qq_message_id(row["real_id"]) != receipt_id:
                    continue
                stamp, sequence = row.get("time"), row.get("message_seq")
                if (receipt_id and type(stamp) is int and type(sequence) is int and sequence > 0
                        and _same_card(row.get("message"), card)):
                    matches[receipt_id] = stamp
            return matches
        return None

    async def _confirm_private_card(
        self, peer: str, account: str, card: Dict[str, Any], started: int, adapter_message_id: Any,
    ) -> str:
        """Empty success means the exact receipt and card appeared in QQ server history."""
        receipt_id = _qq_message_id(adapter_message_id)
        history_read = False
        for delay in _PRIVATE_HISTORY_DELAYS_SECONDS:
            await asyncio.sleep(delay)
            matches = await self._private_card_history(peer, account, card)
            if matches is None:
                continue
            history_read = True
            stamp = matches.get(receipt_id)
            if stamp is not None and started - 2 <= stamp <= int(time.time()) + 5:
                return ""
        return "private_card_not_confirmed" if history_read else "private_card_readback_failed"

    async def send_ark(self, route: Dict[str, Any], ark_json: str, *, before_send: Any = None) -> Dict[str, Any]:
        """Send one raw JSON card. Private success requires a new matching QQ server record."""
        kind = route.get("kind")
        field = "group_id" if kind == "group" else "user_id"
        identifier = str(route.get(field) or "")
        if kind not in ("group", "private") or not _QQ_RE.fullmatch(identifier):
            return {"success": False, "not_sent": True, "error_class": "invalid_target"}
        try:
            card = json.loads(ark_json)
            if not isinstance(card, dict) or not ("meta" in card or "app" in card):
                raise ValueError()
        except (TypeError, ValueError, RecursionError):
            return {"success": False, "not_sent": True, "error_class": "invalid_card"}

        # There is no sender-account selector on the raw adapter action.
        account = str(route.get("account_id") or "")
        actual = ""
        if account or kind == "private":
            actual = await self._logged_in_account()
            if not actual or (account and actual != account):
                return {"success": False, "not_sent": True, "error_class": "route_account_unverified"}
        if before_send is not None:
            allowed, _reason = before_send()
            if not allowed:
                return {"success": False, "not_sent": True, "error_class": "gate_changed"}

        action = "send_group_msg" if kind == "group" else "send_private_msg"
        params = {field: int(identifier), "message": [{"type": "json", "data": {"data": ark_json}}]}
        started = int(time.time())
        try:
            response = await asyncio.wait_for(self._api(PASSTHROUGH_API, action_name=action, params=params),
                                              timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _adapter_delivery_unconfirmed(str(exc)):
                return _unconfirmed_adapter_receipt()
            return {"success": False, "uncertain": True, "error_class": "unknown",
                    "error": f"{action} 结果不确定：{type(exc).__name__}"}

        accepted = False
        message_id = None
        for layer in _response_layers(response):
            if any(_adapter_delivery_unconfirmed(layer.get(field))
                   for field in ("error", "wording", "message", "msg")):
                return _unconfirmed_adapter_receipt()
            if _onebot_failed(layer):
                return {"success": False, "not_sent": True, "error_class": "send_rejected"}
            if layer.get("success") is False:
                return {"success": False, "uncertain": True, "error_class": "unknown"}
            retcode = layer.get("retcode")
            accepted = (accepted or layer.get("status") == "ok" or (type(retcode) is int and retcode == 0)
                        or layer.get("sent") is True or layer.get("success") is True)
            mid = layer.get("message_id")
            if type(mid) in (str, int):
                message_id = mid
        if not accepted:
            return {"success": False, "uncertain": True, "error_class": "unknown"}
        if not _qq_message_id(message_id):
            return _unconfirmed_adapter_receipt()
        result = {"success": True, "channel": "video_card", "delivery_state": "accepted", "message_id": message_id}
        if kind == "private":
            unconfirmed = await self._confirm_private_card(identifier, actual, card, started, message_id)
            if unconfirmed:
                detail = ("QQ 服务器历史中尚未找到这张私聊视频卡片" if unconfirmed == "private_card_not_confirmed"
                          else "暂时无法读取 QQ 服务器私聊历史确认卡片")
                log_debug = getattr(self._log, "debug", None)
                if callable(log_debug):
                    log_debug(f"私聊卡片确认结果：{unconfirmed}")
                return {**_unconfirmed_adapter_receipt(), "error_class": unconfirmed,
                        "error": detail, "message_id": message_id}
            result["delivery_verified"] = True
        return result
