"""Build the two supported Bilibili share formats from a verified video ID."""

from __future__ import annotations

import asyncio
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from core.media_urls import media_cdn_url
from integrations.video_share_policy import BV


COPY_LINK = "复制链接"
WEB_CARD = "QQ 图文卡"
_COPY_SHORT = re.compile(r"https://b23\.tv/[A-Za-z0-9_-]{3,24}")
_VIDEO_HOSTS = {"www.bilibili.com", "m.bilibili.com", "bilibili.com"}


def bili_video_metadata(bvid: str, title: str) -> tuple[str, str]:
    bvid = str(bvid or "").strip()
    if not BV.fullmatch(bvid):
        raise ValueError("invalid Bilibili video ID")
    return bvid, " ".join(str(title or "").split())[:200] or bvid


def build_bili_web_card(*, bvid: str, title: str, desc: str = "", pic_url: str = "") -> dict[str, str]:
    bvid, title = bili_video_metadata(bvid, title)
    card = {
        "title": title,
        "desc": str(desc or "").strip()[:300] or "打开 B 站视频网页",
        "jump_url": f"https://m.bilibili.com/video/{bvid}",
    }
    preview = media_cdn_url(pic_url)
    if preview:
        card["preview_url"] = preview
    return card


def format_bili_copy_share(title: str, url: str) -> str:
    return f"【{title}-哔哩哔哩】 {url}"


def valid_bili_copy_url(url: str, bvid: str) -> bool:
    return bool(_COPY_SHORT.fullmatch(url)) or url == f"https://www.bilibili.com/video/{bvid}"


def _bili_data(response: httpx.Response) -> dict:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
        raise ValueError("Bilibili share service unavailable")
    return payload["data"]


async def bili_copy_short_link(bvid: str) -> str:
    """Request Bilibili's own COPY short link and verify its video before use."""
    bvid, _ = bili_video_metadata(bvid, "")
    origin = "https://www.bilibili.com"
    page = f"{origin}/video/{bvid}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": page, "Origin": origin}
    async with httpx.AsyncClient(headers=headers, timeout=5, follow_redirects=False) as client:
        video, visitor = await asyncio.gather(
            client.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bvid}),
            client.get("https://api.bilibili.com/x/frontend/finger/spi"),
        )
        metadata = _bili_data(video)
        aid = metadata.get("aid")
        buvid = _bili_data(visitor).get("b_3")
        if metadata.get("bvid") != bvid or type(aid) is not int or aid <= 0:
            raise ValueError("Bilibili video identity mismatch")
        if not isinstance(buvid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{12,120}", buvid):
            raise ValueError("Bilibili visitor ID unavailable")
        share = _bili_data(await client.post("https://api.bilibili.com/x/share/click", data={
            "platform": "web", "share_channel": "COPY", "share_mode": "4",
            "share_id": "main.ugc-video-detail.0.0.pv", "oid": str(aid), "buvid": buvid,
        }))
        content = share.get("content")
        links = content.rsplit(maxsplit=1) if isinstance(content, str) else []
        if not links or not _COPY_SHORT.fullmatch(links[-1]):
            raise ValueError("No official Bilibili short link")
        short = links[-1]
        response = await client.get(short)
        destination = urlsplit(response.headers.get("location", ""))
        if (response.status_code not in (301, 302, 303, 307, 308)
                or destination.scheme != "https" or destination.netloc not in _VIDEO_HOSTS
                or destination.path.rstrip("/") not in (f"/video/{bvid}", f"/video/av{aid}")
                or parse_qs(destination.query, keep_blank_values=True).get("p", ["1"]) != ["1"]):
            raise ValueError("Bilibili short link target mismatch")
        return short
