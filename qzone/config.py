"""QQ 空间模块 — 配置段。"""

from __future__ import annotations

from typing import ClassVar, Dict

from maibot_sdk import Field, PluginConfigBase


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class QzoneSectionConfig(PluginConfigBase):
    """QQ 空间模块。"""

    __ui_label__ = "QQ 空间"
    __ui_icon__ = "cloud"
    __ui_order__ = 5
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "QQ Zone", "description": "Post moments by schedule, auto reply to comments."}
    }

    enabled: bool = Field(
        default=False,
        description="启用 QQ 空间模块；需要 SnowLuma 适配器（NapCat 画像不支持 QZone API）",
        json_schema_extra={"label": "启用空间模块", **_ui_i18n("Enable QZone module")},
    )
    check_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=360,
        description="空间巡检间隔（分钟）：检查发说说的时机与新评论",
        json_schema_extra={"label": "空间巡检间隔(分钟)", **_ui_i18n("Check interval (min)")},
    )
    daily_max_posts: int = Field(
        default=2,
        ge=0,
        le=20,
        description="每日最多发几条说说；0=不发",
        json_schema_extra={"label": "每日说说上限", **_ui_i18n("Daily post limit")},
    )
    post_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="每次空间巡检命中发帖时机时的实际发出概率",
        json_schema_extra={"label": "发帖概率", "step": 0.05, **_ui_i18n("Post rate")},
    )
    emotion_filter: bool = Field(
        default=True,
        description="情绪低落时跳过发帖：当前情绪效价 V<−0.6 时本轮不发说说（低落不硬营业，M4）",
        json_schema_extra={"label": "低落实时跳过发帖", **_ui_i18n("Emotion filter")},
    )
    post_by_schedule: bool = Field(
        default=True,
        description="根据日程发说说：优先挑当前活动节点，把活动内容转化成生活动态",
        json_schema_extra={"label": "按日程发帖", **_ui_i18n("Post by schedule")},
    )
    require_activity: bool = Field(
        default=True,
        description="只在没有静默/睡眠且日程里有当前活动时才发说说",
        json_schema_extra={"label": "仅活动时发帖", **_ui_i18n("Only post during activity")},
    )
    reply_comments_enabled: bool = Field(
        default=True,
        description="监听自己说说下的评论并自动回复：走 Web CGI 直连读取评论正文与楼中楼（cookie 经适配器 get_cookies 获取）",
        json_schema_extra={"label": "自动回评", **_ui_i18n("Auto reply comments")},
    )
    reply_number: int = Field(
        default=5,
        ge=1,
        le=20,
        description="自动回评检查自己最近多少条说说下的评论",
        json_schema_extra={"label": "回评检查条数", **_ui_i18n("Reply window (feeds)")},
    )
    reply_probability: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="每轮自动回评的触发概率（1.0=每次巡检都检查）",
        json_schema_extra={"label": "回评概率", "step": 0.05, **_ui_i18n("Reply probability")},
    )
    notify_new_comments: bool = Field(
        default=True,
        description="回退路径：cookie 不可用无法读评论正文时，检测到评论数变化经 maisaka 主动提醒",
        json_schema_extra={"label": "新评论主动提醒", **_ui_i18n("Notify new comments")},
    )
    llm_task_name: str = Field(
        default="utils",
        description="说说文案/回评生成的模型任务名（utils/replyer 等，走宿主任务路由）",
        json_schema_extra={"label": "生成任务名", **_ui_i18n("LLM task")},
    )
    silence_start: str = Field(
        default="01:00",
        description="空间静默时段开始（HH:MM）：静默期内巡检不发说说、不监听评论（支持跨午夜；配置非法按静默处理）",
        json_schema_extra={"label": "空间静默开始", **_ui_i18n("QZone silence start")},
    )
    silence_end: str = Field(
        default="08:00",
        description="空间静默时段结束（HH:MM）",
        json_schema_extra={"label": "空间静默结束", **_ui_i18n("QZone silence end")},
    )
    napcat_http_host: str = Field(
        default="",
        description="备选：NapCat HTTP 服务器地址（cookie 获取降级通道 2，如 127.0.0.1）",
        json_schema_extra={"label": "NapCat HTTP 地址", "placeholder": "127.0.0.1", **_ui_i18n("NapCat HTTP host")},
    )
    napcat_http_port: str = Field(
        default="",
        description="备选：NapCat HTTP 服务器端口（如 9999）；留空则不用该通道",
        json_schema_extra={"label": "NapCat HTTP 端口", "placeholder": "9999", **_ui_i18n("NapCat HTTP port")},
    )
    napcat_http_token: str = Field(
        default="",
        description="备选：NapCat HTTP 服务器的 access token",
        json_schema_extra={"label": "NapCat HTTP Token", **_ui_i18n("NapCat HTTP token")},
    )
