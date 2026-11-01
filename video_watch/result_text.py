"""Planner与观看文字缓存共用的输出边界；不转交逐帧记录或媒体。"""
from __future__ import annotations

def result_text(result: dict) -> str:
    cached = result.get("cached_text")
    if isinstance(cached, str):
        return ("你以前看过这个视频。以下是当时保存的文字记录，本次没有重新观看；"
                "不要把旧记忆当作刚刚重看的结果。\n" + cached)
    video = result.get("video") or {}
    description = str(result.get("content_description") or "").strip()
    if not description:
        brief = str(video.get("desc") or video.get("description") or "").strip()
        description = ("未生成独立内容描述；视频简介：" + brief if brief else
                       "未生成独立内容描述，现有资料不足，不能补造内容。")
        if result.get("content_description_error"):
            description += "（" + str(result["content_description_error"]) + "）"
    impression = str(result.get("impression") or "").strip()
    if not impression:
        impression = str(result.get("impression_error") or result.get("error") or "暂无观后感")
    return (f"视频标题：{video.get('title') or '未知标题'}\n"
            f"UP主：{video.get('uploader') or '未知'}\n"
            f"时长：{video.get('duration_seconds') or '?'}秒\n"
            f"视频内容描述：{description}\n观后感：{impression}")
