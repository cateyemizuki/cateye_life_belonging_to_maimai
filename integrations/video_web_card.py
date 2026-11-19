"""Prepare the video URL and display fields for SnowLuma's native mini-app generator."""

from __future__ import annotations

from core.media_urls import media_cdn_url
from integrations.video_share_policy import BV


def build_bili_miniapp_request(
    *, bvid: str, title: str, desc: str = "", pic_url: str = "",
) -> dict[str, str]:
    """Use the canonical Bilibili page and optional public CDN cover."""
    bvid = str(bvid or "").strip()
    if not BV.fullmatch(bvid):
        raise ValueError("invalid Bilibili video ID")
    request = {
        "type": "bili",
        "title": str(title or "").strip()[:200] or bvid,
        "desc": str(desc or "").strip()[:300] or "哔哩哔哩视频分享",
        "jumpUrl": "https://www.bilibili.com/video/" + bvid,
    }
    preview = media_cdn_url(pic_url)
    if preview:
        request["picUrl"] = preview
    return request
