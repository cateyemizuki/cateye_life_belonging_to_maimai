"""SnowLuma's two QQ operations for signing and verifying Bilibili mini-app cards."""

from __future__ import annotations

import re
from urllib.parse import parse_qs

from integrations.video_web_card import BILI_VIDEO_PATH, build_bili_miniapp_request, normalized_qq_launch_url


BILI_APP_ID = "1109937557"
_SDK_ID = "V1_PC_MINISDK_99.99.99_1_APP_A"
_BILI_ICON = "https://miniapp.gtimg.cn/public/appicon/51f90239b78a2e4994c11215f4c4ba15_200.jpg"
_SIGN_COMMAND = "LightAppSvc.mini_app_share.AdaptShareInfo"
_LOOKUP_COMMAND = "LightAppSvc.mini_app_info.GetAppInfoByLink"
_LOOKUP_SCENE = 31
_MAX_PACKET_BYTES = 262144
_MAX_CARD_BYTES = 65536
_MAX_LAUNCH_URL = 240
_MAX_ROUTE_BYTES = 2048
_HEX = re.compile(r"[0-9a-fA-F]+")
_INTERNAL_TEST = "该小程序内测中".encode("utf-8")


class MiniAppProtocolError(ValueError):
    """A classified failure whose message never contains QQ response contents."""


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value >= 128:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _uint_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _text_field(number: int, value: str | bytes) -> bytes:
    payload = value.encode("utf-8") if isinstance(value, str) else value
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def sign_packet(*, bvid: str, title: str, desc: str, pic_url: str) -> dict[str, str | bool]:
    request = build_bili_miniapp_request(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
    body = b"".join((
        _text_field(2, BILI_APP_ID),
        _text_field(3, request["title"]),
        _text_field(4, request["desc"]),
        _text_field(9, request.get("picUrl", "")),
        _text_field(11, request["jumpUrl"]),
        _text_field(12, _BILI_ICON),
        _uint_field(13, 3),  # Formal release of the mini-app.
        _text_field(17, request["webUrl"]),
    ))
    wire = _text_field(2, _SDK_ID) + _text_field(4, body)
    return {"cmd": _SIGN_COMMAND, "data": wire.hex(), "rsp": True}


def lookup_packet(launch: str) -> dict[str, str | bool]:
    if (not isinstance(launch, str) or len(launch) > _MAX_LAUNCH_URL
            or any(ord(ch) < 33 or ord(ch) > 126 or ch == "\\" for ch in launch)
            or not normalized_qq_launch_url(launch)):
        raise MiniAppProtocolError("qq_card_invalid")
    body = _text_field(2, launch) + _uint_field(3, 0)
    wire = _uint_field(1, _LOOKUP_SCENE) + _text_field(2, _SDK_ID) + _text_field(4, body)
    return {"cmd": _LOOKUP_COMMAND, "data": wire.hex(), "rsp": True}


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 64, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise ValueError("invalid protobuf varint")


def _fields(data: bytes) -> dict[int, list[int | bytes | None]]:
    fields: dict[int, list[int | bytes | None]] = {}
    offset = 0
    for _ in range(256):
        if offset == len(data):
            return fields
        tag, offset = _read_varint(data, offset)
        number, wire = tag >> 3, tag & 7
        if not 1 <= number < 1 << 29:
            raise ValueError("invalid protobuf field number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        elif wire == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise ValueError("truncated protobuf field")
            value, offset = data[offset:end], end
        elif wire in (1, 5):
            # Skip unrelated fixed-width fields while retaining their presence.
            offset += 8 if wire == 1 else 4
            if offset > len(data):
                raise ValueError("truncated protobuf field")
            value = None
        else:
            raise ValueError("unsupported protobuf field type")
        fields.setdefault(number, []).append(value)
    raise ValueError("too many protobuf fields")


def _message_field(fields: dict[int, list[int | bytes | None]], number: int) -> bytes:
    values = fields.get(number, ())
    if len(values) != 1 or not isinstance(values[0], bytes):
        raise ValueError("missing or ambiguous protobuf field")
    return values[0]


def _response_body(payload: str, *, lookup: bool = False) -> bytes:
    unreadable = "qq_launch_unverified" if lookup else "qq_sign_unverified"
    if len(payload) > 2 * _MAX_PACKET_BYTES or len(payload) % 2 or not _HEX.fullmatch(payload):
        raise MiniAppProtocolError(unreadable)
    try:
        fields = _fields(bytes.fromhex(payload))
        if lookup and fields.get(1) != [_LOOKUP_SCENE]:
            raise ValueError("unexpected lookup response type")
        messages = fields.get(3, [])
        if lookup and any(isinstance(item, bytes) and _INTERNAL_TEST in item for item in messages):
            raise MiniAppProtocolError("qq_launch_internal_test")
        if fields.get(2) != [0] or messages != [b""]:
            raise MiniAppProtocolError("qq_launch_rejected" if lookup else "qq_sign_rejected")
        return _message_field(fields, 4)
    except MiniAppProtocolError:
        raise
    except ValueError:
        raise MiniAppProtocolError(unreadable) from None


def signed_card_json(payload: str) -> str:
    body = _response_body(payload)
    try:
        card = _message_field(_fields(body), 2)
        if not card or len(card) > _MAX_CARD_BYTES:
            raise ValueError("invalid card size")
        return card.decode("utf-8")
    except ValueError:
        raise MiniAppProtocolError("qq_card_invalid") from None


def verify_video_route(payload: str, bvid: str) -> None:
    body = _response_body(payload, lookup=True)
    try:
        # GetAppInfoByLink's page path is exactly field 4.2.11.1.
        for number in (2, 11, 1):
            body = _message_field(_fields(body), number)
        if not body or len(body) > _MAX_ROUTE_BYTES:
            raise ValueError("invalid route size")
        path = body.decode("utf-8")
        if any(ch.isspace() or ord(ch) < 32 for ch in path):
            raise ValueError("invalid route")
        page, _, query = path.partition("?")
        if page not in (BILI_VIDEO_PATH, BILI_VIDEO_PATH.lstrip("/")) or "#" in query:
            raise MiniAppProtocolError("qq_launch_mismatch")
        params = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=16, errors="strict")
    except MiniAppProtocolError:
        raise
    except ValueError:
        raise MiniAppProtocolError("qq_launch_unverified") from None
    if params.get("bvid") != [bvid]:
        raise MiniAppProtocolError("qq_launch_mismatch")
