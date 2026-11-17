"""Public image links only; never expose local paths or executable URL schemes."""
from __future__ import annotations
from ipaddress import ip_address
from urllib.parse import urlsplit, urlunsplit

IMAGE_HTTPS_CDNS = ('hdslb.com', 'biliimg.com', 'qpic.cn', 'photo.store.qq.com', 'gtimg.cn', 'gtimg.com')


def public_image_url(value: object, *, limit: int = 3000) -> str:
    if not isinstance(value, str):
        return ""
    url = value.strip()
    if url.startswith('//'):
        url = 'https:' + url
    if not url or len(url) > limit or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return ""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or '').lower().rstrip('.')
        if parts.scheme.lower() not in ('http', 'https') or not host or parts.username or parts.password:
            return ""
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return ""
        try:
            if not ip_address(host).is_global:
                return ""
        except ValueError:
            pass
        parts.port  # Reject malformed ports even though this module never fetches links.
        if parts.scheme.lower() == 'http' and any(host == domain or host.endswith('.' + domain) for domain in IMAGE_HTTPS_CDNS):
            # These providers expose the same image path over HTTPS; preserve all URL data.
            netloc = parts.netloc.rsplit(':', 1)[0] if parts.port == 80 else parts.netloc
            url = urlunsplit(('https', netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return ""
    return url
