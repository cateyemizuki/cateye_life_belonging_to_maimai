"""video_watch 配置分组（B 计划 §3：并入生活插件总配置，[video_watch] 后端分组）。

键与独立原型 cateye.bili-watch 的 ``bili_watch`` 节（plugin.py:63-92, cad3d2d）
同源同约束（ge/le 逐字保留），旧原型配置值可按 suite_config 的迁移器映射加载；
不设 Token 上限——Token 只记录宿主回传 usage（2026-10-29 用户决策）。
"""

from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase

#: 旧独立插件配置节名 → 新分组名的映射（suite_config 迁移器读取）
LEGACY_SECTION_NAME = "bili_watch"


def _ui_i18n(title_en: str, desc_en: str = "") -> dict:
    payload = {"en": {"label": title_en}}
    if desc_en:
        payload["en"]["hint"] = desc_en
    return {"_ui_i18n": payload}


class VideoWatchSectionConfig(PluginConfigBase):
    """观看流水线参数（资源控制；队列/时限/时长上限有界，媒体抽帧另设上限）。"""

    enabled: bool = Field(
        default=True,
        description="是否启用内置视频观看模块（关闭时工具如实返回未启用，日程跳过）",
        json_schema_extra={"label": "启用视频观看", **_ui_i18n("Enable video watching")},
    )
    queue_capacity: int = Field(
        default=3, ge=1, le=8,
        description="观看队列容量（超出如实拒绝 queue_full）",
        json_schema_extra={"label": "队列容量", **_ui_i18n("Queue capacity")},
    )
    task_deadline_seconds: int = Field(
        default=900, ge=60, le=3600,
        description="单任务总时限（秒），超时终止 worker 并如实标 failed/timeout",
        json_schema_extra={"label": "任务总时限(秒)", **_ui_i18n("Task deadline (s)")},
    )
    max_video_duration_seconds: int = Field(
        default=1800, ge=60, le=7200,
        description="可观看的视频时长上限（秒）；超长视频明确拒绝，不宣称能完整观看",
        json_schema_extra={"label": "视频时长上限(秒)", **_ui_i18n("Max video length (s)")},
    )
    summary_impression: bool = Field(
        default=True,
        description="观看完成后用宿主 LLM 生成简短观后感（失败如实降级）",
        json_schema_extra={"label": "生成观后感", **_ui_i18n("Generate impression")},
    )
    impression_task_name: str = Field(
        default="",
        description="观后感使用的宿主任务名（留空 = utils 默认任务）",
        json_schema_extra={"label": "观后感任务名", **_ui_i18n("Impression task name")},
    )
    watchlist: List[str] = Field(
        default_factory=list,
        description="待看列表：BV 号或 bilibili 视频页链接（每行一个）；日程刷视频事件从这里选片",
        json_schema_extra={"label": "待看列表", **_ui_i18n("Watchlist (BV or URL per line)")},
    )
    visual_enabled: bool = Field(
        default=True,
        description="尝试真实画面采样（抽帧→拼图→vlm 图像任务）；通道不可得时如实降级为文本来源",
        json_schema_extra={"label": "画面采样", **_ui_i18n("Visual sampling")},
    )
    visual_task_name: str = Field(
        default="vlm",
        description="画面理解使用的宿主任务名（须为宿主配置的多模态任务；不可用自动降级）",
        json_schema_extra={"label": "画面任务名", **_ui_i18n("Visual task name")},
    )
    visual_max_frames: int = Field(
        default=6, ge=1, le=12,
        description="单任务最多抽取的帧数（等间隔采样；有界，不做逐秒观看）",
        json_schema_extra={"label": "抽帧上限", **_ui_i18n("Max frames")},
    )
    visual_max_download_mb: int = Field(
        default=32, ge=4, le=128,
        description="单任务媒体下载预算（MB，超限终止采样并如实标注）",
        json_schema_extra={"label": "媒体下载预算(MB)", **_ui_i18n("Media budget (MB)")},
    )
