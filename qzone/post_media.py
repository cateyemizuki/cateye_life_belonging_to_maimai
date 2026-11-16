"""Read-only own-feed images, cached separately from durable publish receipts."""
from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from typing import Any
from core.media_urls import public_image_url
from core.storage import load_json, save_json_atomic

MAX_POSTS = 20
MAX_IMAGES = 4


def image_urls(row: Any) -> list[str]:
    """Normalize adapter/CGI image formats without trusting arbitrary feed fields."""
    if not isinstance(row, dict):
        return []
    values = []
    for field in ('images', 'pic', 'pics', 'photos'):
        raw = row.get(field)
        if isinstance(raw, list):
            values.extend(raw[:MAX_IMAGES * 3])
        elif isinstance(raw, (str, dict)):
            values.append(raw)
    out = []
    for value in values:
        candidates = ([value] if isinstance(value, str) else
                      [value.get(k) for k in ('url', 'largeurl', 'url2', 'url1', 'url3', 'pic', 'src')]
                      if isinstance(value, dict) else [])
        link = next((url for raw in candidates if (url := public_image_url(raw, limit=1000))), '')
        if link and link not in out:
            out.append(link)
        if len(out) == MAX_IMAGES:
            break
    return out


class PostMediaStore:
    def __init__(self, path: Any) -> None:
        self.path = Path(path)
        raw = load_json(self.path, {}) if not self.path.exists() or self.path.stat().st_size <= 128 * 1024 else {}
        self.rows = raw.get('rows', {}) if isinstance(raw, dict) and raw.get('version') == 1 else {}
        if not isinstance(self.rows, dict):
            self.rows = {}

    def merge(self, feeds: Any, known_tids: set[str], now: float) -> bool:
        before = deepcopy(self.rows)
        # At most one adapter page + one CGI page; cache itself remains MAX_POSTS.
        for row in feeds[:MAX_POSTS * 2] if isinstance(feeds, list) else []:
            if not isinstance(row, dict):
                continue
            tid = str(row.get('tid') or row.get('id') or '')
            links = image_urls(row)
            if tid in known_tids and links:
                self.rows[tid] = {'images': links, 'captured_at': now}
        self.rows = {tid: row for tid, row in self.rows.items()
                     if tid in known_tids and isinstance(row, dict)}
        self.rows = dict(sorted(self.rows.items(), key=lambda pair: pair[1].get('captured_at', 0), reverse=True)[:MAX_POSTS])
        if self.rows == before:
            return True
        try:
            saved = save_json_atomic(self.path, {'version': 1, 'rows': self.rows})
        except Exception:
            saved = False
        if not saved:
            self.rows = before
            return False
        return True

    def images_for(self, tid: str) -> dict:
        raw = self.rows.get(str(tid))
        raw = raw if isinstance(raw, dict) else {}
        links = image_urls(raw)
        return {'images': links, 'images_captured_at': raw.get('captured_at') if links else None}
