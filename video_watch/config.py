"""video_watch 配置分组（B 计划 §3：并入生活插件总配置，[video_watch] 后端分组）。

采用stream+拼图准入；旧原型配置值仍可按 suite_config 的迁移器映射加载。
旧时长/散帧硬上限安全忽略，下载保护值只对显式download生效；
不设 Token 上限——Token 只记录宿主回传 usage（2026-10-29 用户决策）。
"""

from __future__ import annotations

from typing import Literal
from pydantic import model_validator

from maibot_sdk import Field, PluginConfigBase

#: 旧独立插件配置节名 → 新分组名的映射（suite_config 迁移器读取）
LEGACY_SECTION_NAME = "bili_watch"


def _ui_i18n(title_en: str, desc_en: str = "") -> dict:
    payload = {"en": {"label": title_en}}
    if desc_en:
        payload["en"]["hint"] = desc_en
    return {"_ui_i18n": payload}


class VideoWatchSectionConfig(PluginConfigBase):
    """设置视频读取、画面采样、观看队列和观后感生成。"""

    __ui_label__ = "视频观看"
    __ui_icon__ = "video"
    __ui_order__ = 14

    @model_validator(mode="before")
    @classmethod
    def _retire_duration_limits(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value.pop("max_video_duration_seconds", None)
            value.pop("visual_max_frames", None)
            value.pop("watchlist", None)
        return value

    source_mode: Literal["stream", "download"] = Field(
        default="stream", description="stream：直连媒体按时间点读取，不先下载全片；download：显式下载后抽帧，受下载保护值限制；失败不会暗中切换全量下载。",
        json_schema_extra={"label": "媒体读取方式"})
    sampling_mode: Literal["seek", "paced"] = Field(
        default="seek", description="seek：尽快按时间点抽帧；paced：按真实播放时间逐点采样。两者均单任务、低优先级、单解码线程。",
        json_schema_extra={"label": "采样节奏"})
    sample_interval_seconds: int = Field(
        default=25, ge=5, le=600, description="每隔多少秒取一帧；不静默稀释采样。预计帧数=向上取整(视频秒数/间隔)，每16帧拼一张图片。",
        json_schema_extra={"label": "采样间隔（秒）"})
    visual_max_montages: int = Field(
        default=16, ge=1, le=32, description="最终最多向视觉模型提交多少张拼图（每张4×4帧）；超出先拒绝并说明所需张数。默认25秒间隔、16张可容纳约106分钟。只记录实际Token，不推算或限制输出Token。",
        json_schema_extra={"label": "最多提交拼图数"})

    cache_retention_days: int = Field(
        default=15, ge=1, le=365, description="观看记忆保存天数，自结果保存起计算，读取不延长；仅缓存BV号和文字结果，过期后清理。",
        json_schema_extra={"label": "观看记忆保留天数"})

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
        default=10800, ge=60, le=21600,
        description="单任务总时限（秒），超时终止 worker 并如实标 failed/timeout",
        json_schema_extra={"label": "任务总时限(秒)", **_ui_i18n("Task deadline (s)")},
    )
    summary_impression: bool = Field(
        default=True,
        description="观看完成后用宿主 LLM 生成简短观后感（失败如实降级）",
        json_schema_extra={"label": "生成观后感", **_ui_i18n("Generate impression")},
    )
    impression_task_name: str = Field(
        default="",
        description="内容描述与观后感使用的宿主任务名（留空 = utils 默认任务）",
        json_schema_extra={"label": "观后感任务名", **_ui_i18n("Impression task name")},
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
    visual_max_download_mb: int = Field(
        default=256, ge=4, le=1024,
        description="仅download模式的媒体下载保护值（MiB）；stream不先整片下载，不按此值拒绝长视频",
        json_schema_extra={"label": "download下载保护值（MiB）", **_ui_i18n("Media budget (MB)")},
    )
