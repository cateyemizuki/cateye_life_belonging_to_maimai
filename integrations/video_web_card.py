"""Build the news card emitted by OneBot's standard link-share segment."""

from __future__ import annotations

from core.media_urls import media_cdn_url
from integrations.video_share_policy import BV

LINK_CARD_APP = "com.tencent.structmsg"
LINK_CARD_VIEW = "news"


def build_bili_web_card(
    *, bvid: str, title: str, desc: str = "", pic_url: str = "",
) -> dict:
    """Produce a card with the canonical Bilibili link as its click target."""
    bvid = str(bvid or "").strip()
    if not BV.fullmatch(bvid):
        raise ValueError("invalid Bilibili video ID")
    title = str(title or "").strip()[:200] or bvid
    desc = str(desc or "").strip()[:300] or "哔哩哔哩视频分享"
    return {
        "app": LINK_CARD_APP,
        "view": LINK_CARD_VIEW,
        "prompt": title,
        "meta": {"news": {
            "title": title,
            "desc": desc,
            "jumpUrl": "https://www.bilibili.com/video/" + bvid,
            "preview": media_cdn_url(pic_url),
        }},
    }
