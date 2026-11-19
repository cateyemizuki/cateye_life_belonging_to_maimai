"""Prepare validated fields for SnowLuma's native graph-and-text sharing action."""

from __future__ import annotations

from core.media_urls import media_cdn_url
from integrations.video_share_policy import BV


def build_bili_web_card(
    *, bvid: str, title: str, desc: str = "", pic_url: str = "",
) -> dict[str, str]:
    """Use the canonical Bilibili page and optional public CDN cover."""
    bvid = str(bvid or "").strip()
    if not BV.fullmatch(bvid):
        raise ValueError("invalid Bilibili video ID")
    card = {
        "title": str(title or "").strip()[:200] or bvid,
        "desc": str(desc or "").strip()[:300] or "哔哩哔哩视频分享",
        "jump_url": "https://www.bilibili.com/video/" + bvid,
    }
    preview = media_cdn_url(pic_url)
    if preview:
        card["preview_url"] = preview
    return card
