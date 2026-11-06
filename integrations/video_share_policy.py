"""Pure normalization shared by the independent video social pipeline."""
from __future__ import annotations
import json, math, re
from typing import Any

BV = re.compile(r"^BV[A-Za-z0-9]{10}$")
QQ = re.compile(r"^[0-9]{5,12}$")
KEEP_SECONDS = 15 * 86400
ACTIVE_ATTEMPTS = ("preparing", "sending", "accepted", "uncertain")


def clean(value: Any, limit: int = 500) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"(?:https?|file|ftp)://\S+", " ", value)
    value = re.sub(r"(?:cookie|authorization|api[_-]?key|token|secret)\s*[:=]\s*\S+", " ", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError, OverflowError):
        return default


def watched_facts(result: Any, source: str, now: float) -> dict | None:
    if not isinstance(result, dict) or result.get("status") not in (None, "completed"):
        return None
    video = result.get("video") if isinstance(result.get("video"), dict) else {}
    bvid = str(result.get("bvid") or video.get("bvid") or "")
    title = clean(video.get("title") or result.get("title"), 200)
    if not BV.fullmatch(bvid) or not title:
        return None
    visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
    summary = clean(visual.get("summary"), 4000)
    subtitles = clean(result.get("subtitle_text"), 4000)
    if (visual.get("status") == "completed" and visual.get("understanding_status") in (None, "completed")
            and summary):
        evidence = "visual"
    elif len(subtitles) >= 40:
        # subtitle_text is the worker's actual public-subtitle field, not a
        # title/description inferred by the content-description model.
        evidence, summary = "public_subtitles", subtitles
    else:
        return None
    return {"bvid": bvid, "title": title, "summary": summary,
            "description": clean(result.get("content_description"), 4000),
            "impression": clean(result.get("impression"), 1000),
            "evidence": evidence, "uploader": clean(video.get("uploader"), 120),
            "duration_seconds": max(0, number(video.get("duration_seconds"))),
            "cover_url": str(video.get("pic") or video.get("cover") or "")[:1000],
            "source": clean(source, 160), "watched_at": now}


def strict_json(text: str) -> Any:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
    return json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
