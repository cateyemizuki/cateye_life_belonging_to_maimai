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
        # 2026-11-04 措辞修复：父侧加工超时/失败时，已完成的画面采样理解
        # （visual.summary）尽量纳入记忆文本；outward 用内容聚焦的中性引导，
        # 不主动声明“非连续观看/资料不足”（诚实约束保留在生成提示词与内部诊断，
        # content_description_error 仍落 result.json，不再拼进对外文本）。
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
        visual_summary = str((visual or {}).get("summary") or "").strip()
        if visual_summary and (visual or {}).get("understanding_status") in (None, "completed"):
            description = "以下是基于已有信息的画面内容概括：" + visual_summary
        else:
            brief = str(video.get("desc") or video.get("description") or "").strip()
            description = ("视频简介：" + brief if brief else
                           "可基于视频标题与UP主信息了解该视频的主题与背景。")
    impression = str(result.get("impression") or "").strip()
    if not impression:
        impression = str(result.get("impression_error") or result.get("error") or "暂无观后感")
    return (f"视频标题：{video.get('title') or '未知标题'}\n"
            f"UP主：{video.get('uploader') or '未知'}\n"
            f"时长：{video.get('duration_seconds') or '?'}秒\n"
            f"视频内容描述：{description}\n观后感：{impression}")
