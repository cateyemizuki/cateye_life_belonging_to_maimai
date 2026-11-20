"""Bounded image reads for the dashboard's sandboxed page."""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from functools import lru_cache
from hashlib import sha256
from io import BytesIO
from threading import BoundedSemaphore
from time import monotonic

import httpx
from PIL import Image, ImageOps

from core.media_urls import media_cdn_url

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 18_000_000
MAX_THUMBNAIL_BYTES = 108 * 1024
_IMAGE_FORMATS = ("JPEG", "PNG", "WEBP", "GIF")
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
_sources: OrderedDict[str, tuple[str, float]] = OrderedDict()
_fetch_slots = BoundedSemaphore(2)


def register_dashboard_image(value: object) -> str:
    """Only images in the dashboard projection get a short-lived lookup entry."""
    url = media_cdn_url(value)
    if not url:
        return ""
    image_id = sha256(url.encode("utf-8")).hexdigest()
    _sources[image_id] = (url, monotonic() + 1800)
    _sources.move_to_end(image_id)
    if len(_sources) > 128:
        _sources.popitem(last=False)
    return image_id


async def dashboard_thumbnail(image_id: str) -> str:
    if not isinstance(image_id, str) or len(image_id) != 64:
        return ""
    entry = _sources.get(image_id)
    if entry is None or entry[1] < monotonic():
        return ""
    try:
        return await asyncio.to_thread(_cached_thumbnail, entry[0])
    except (httpx.HTTPError, Image.DecompressionBombError, OSError, ValueError):
        return ""


@lru_cache(maxsize=80)
def _cached_thumbnail(url: str) -> str:
    if not _fetch_slots.acquire(blocking=False):
        raise ValueError("thumbnail reads busy")
    try:
        raw = _read_image(url)
        jpeg = _thumbnail_jpeg(raw)
        return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    finally:
        _fetch_slots.release()


def _read_image(url: str) -> bytes:
    headers = {"Accept": "image/*", "Accept-Encoding": "identity", "User-Agent": "MaiBot Life Dashboard"}
    deadline = monotonic() + 6
    with httpx.stream("GET", url, headers=headers, timeout=3,
                      follow_redirects=False, trust_env=False) as response:
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        encoding = response.headers.get("content-encoding", "identity").lower()
        declared_size = response.headers.get("content-length", "")
        if (response.status_code != 200 or mime not in _IMAGE_MIMES or encoding != "identity"
                or (declared_size.isdecimal() and int(declared_size) > MAX_DOWNLOAD_BYTES)):
            raise ValueError("unsupported thumbnail response")
        content = bytearray()
        for chunk in response.iter_raw():
            if monotonic() > deadline or len(content) + len(chunk) > MAX_DOWNLOAD_BYTES:
                raise ValueError("thumbnail read limit exceeded")
            content.extend(chunk)
    return bytes(content)


def _thumbnail_jpeg(raw: bytes) -> bytes:
    with Image.open(BytesIO(raw), formats=_IMAGE_FORMATS) as original:
        if original.width * original.height > MAX_IMAGE_PIXELS:
            raise ValueError("thumbnail dimensions too large")
        original.thumbnail((720, 720))
        photo = ImageOps.exif_transpose(original).convert("RGB")
    for size, quality in ((720, 82), (512, 76)):
        photo.thumbnail((size, size))
        with BytesIO() as output:
            photo.save(output, format="JPEG", quality=quality, optimize=True)
            if output.tell() <= MAX_THUMBNAIL_BYTES:
                return output.getvalue()
    raise ValueError("thumbnail size limit exceeded")
