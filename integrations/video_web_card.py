"""Normalize the links and display fields for a native Bilibili mini-app card."""

from __future__ import annotations

from urllib.parse import urlsplit

from core.media_urls import media_cdn_url
from integrations.video_share_policy import BV


BILI_VIDEO_PATH = "/pages/video/video"


def build_bili_miniapp_request(
    *, bvid: str, title: str, desc: str = "", pic_url: str = "",
) -> dict[str, str]:
    """Separate the mini-app destination from the canonical webpage."""
    bvid = str(bvid or "").strip()
    if not BV.fullmatch(bvid):
        raise ValueError("invalid Bilibili video ID")
    request = {
        "type": "bili",
        "title": str(title or "").strip()[:200] or bvid,
        "desc": str(desc or "").strip()[:300] or "哔哩哔哩视频分享",
        "jumpUrl": f"{BILI_VIDEO_PATH}?bvid={bvid}",
        "webUrl": "https://www.bilibili.com/video/" + bvid,
    }
    preview = media_cdn_url(pic_url)
    if preview:
        request["picUrl"] = preview
    return request


def normalized_qq_launch_url(launch: object) -> str:
    """Normalize an official opaque launch URL for comparison."""
    if not isinstance(launch, str) or any(ch.isspace() for ch in launch):
        return ""
    # Providers may omit the scheme. The original URL is used in QQ requests.
    candidate = "https:" + launch if launch.startswith("//") else (
        "https://" + launch if launch.casefold().startswith("m.q.qq.com/") else launch)
    try:
        url = urlsplit(candidate)
        if (url.scheme == "https" and url.hostname == "m.q.qq.com" and url.port in (None, 443)
                and url.username is None and url.password is None
                and url.path.startswith("/a/s/") and len(url.path) > len("/a/s/")):
            return url.geturl()
    except ValueError:
        pass
    return ""
