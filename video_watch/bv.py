"""BV 号 / 链接规范化（B 计划 §4.3：首期只接收受支持的视频标识与来源）。

- 接受裸 BV 号、bilibili.com/video/BV… 页面链接（含查询参数）；
- b23.tv 短链通过可注入的解析器展开后**校验最终目标**确实是
  bilibili.com/video/BV…，不把任意 URL 交给下载器；
- 其余来源一律拒绝（返回 None），不做猜测性解析。
"""

from __future__ import annotations

import re
from typing import Optional
from urllib import parse as _urlparse
from urllib import request as _urlrequest

_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_BILI_VIDEO_HOSTS = {"www.bilibili.com", "m.bilibili.com", "bilibili.com"}
_SHORT_HOSTS = {"b23.tv"}
#: 短链展开的网络超时（秒）；失败如实返回 None，由调用方按 unsupported 来源上报。
SHORT_LINK_TIMEOUT_SECONDS = 6.0


def extract_bvid(text: str) -> Optional[str]:
    """从任意输入里提取首个形态合法的 BV 号（不做网络校验）。"""
    if not isinstance(text, str):
        return None
    match = _BV_RE.search(text.strip())
    return match.group(0) if match else None


def normalize_video_ref(raw: str) -> Optional[str]:
    """把裸 BV 号 / 视频页链接规范化为 BV 号；不支持的来源返回 None。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    direct = extract_bvid(text)
    # 裸 BV 号：整段就是 BV…（或夹在最少噪声里）→ 直接接受
    if _BV_RE.fullmatch(text):
        return direct
    try:
        parsed = _urlparse.urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    host = (parsed.hostname or "").lower()
    if host in _BILI_VIDEO_HOSTS:
        if not _default_page_only(parsed.query):
            return None
        return extract_bvid(parsed.path) or (direct if _looks_like_video_path(parsed.path) else None)
    if host in _SHORT_HOSTS:
        expanded = expand_short_link(text)
        if not expanded:
            return None
        try:
            final = _urlparse.urlsplit(expanded)
        except ValueError:
            return None
        # 二次校验：展开后的最终目标必须是 bilibili 视频页（不信任解析器单一环节）
        if (final.hostname or "").lower() not in _BILI_VIDEO_HOSTS:
            return None
        if not _default_page_only(final.query):
            return None
        return extract_bvid(final.path) or None
    # 其他主机：若正文里带着 BV 号也只接受 bilibili 域名形态，防任意 URL 投递
    return None


def _looks_like_video_path(path: str) -> bool:
    return "/video/" in (path or "")


def _default_page_only(query: str) -> bool:
    """Explicit non-default pages require their own identity/cache support."""
    pages = _urlparse.parse_qs(query, keep_blank_values=True).get("p")
    return pages is None or pages == ["1"]


def expand_short_link(url: str) -> Optional[str]:
    """展开 b23.tv 短链，返回最终 URL（校验宿主域名为 bilibili）。

    网络失败/非 bilibili 目标 → None（调用方按真实原因上报，不编造）。
    """
    try:
        request = _urlrequest.Request(url, method="GET", headers={"User-Agent": "bili-watch/0.1"})
        with _urlrequest.urlopen(request, timeout=SHORT_LINK_TIMEOUT_SECONDS) as response:
            final_url = str(response.geturl())
    except Exception:
        return None
    try:
        parsed = _urlparse.urlsplit(final_url)
    except ValueError:
        return None
    if (parsed.hostname or "").lower() not in _BILI_VIDEO_HOSTS:
        return None
    return final_url
