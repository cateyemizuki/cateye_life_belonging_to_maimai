"""Independent social sharing: default-on with audience activity gating."""
from typing import ClassVar, Literal
from maibot_sdk import Field, PluginConfigBase


class VideoShareSectionConfig(PluginConfigBase):
    """根据真实观看内容和允许聊天的近期话题，自主判断是否分享视频。"""
    __ui_label__ = "视频自主分享"
    __ui_icon__ = "share-2"
    __ui_order__ = 10
    __ui_i18n__: ClassVar[dict] = {"en": {"title": "Video social sharing"}}

    mode: Literal["关闭", "影子模式", "自动分享"] = Field(
        default="自动分享", description="关闭时停止自动评估；影子模式只记录决定不发送；自动分享按授权、活跃门槛、静默和额度投递卡片。默认自动分享，评估器可自主选择不分享。手动分享工具独立可用。",
        json_schema_extra={"label": "运行模式"})
    include_companion: bool = Field(
        default=True, description="允许将已配置的陪伴目标私聊作为自动分享候选，仍需判断内容相关性与是否适合打扰。陪伴私聊不受群活跃门槛限制。",
        json_schema_extra={"label": "允许陪伴目标私聊"})
    allow_all_groups: bool = Field(
        default=True, description="允许所有已注册且可路由的QQ群作为自动分享候选，仍受群活跃门槛、静默与额度约束，评估器自主决定是否分享。关闭后仅陪伴私聊与额外允许的聊天可候选。",
        json_schema_extra={"label": "允许所有群"})
    allowed_chats: list[str] = Field(
        default_factory=list, description="在允许所有群之外精确追加的聊天，每项使用group:群号或private:QQ号；只接受已存在且可路由的QQ会话。allow_all_groups关闭时此项与陪伴私聊决定候选范围。",
        json_schema_extra={"label": "额外允许的聊天"})
    min_group_daily_messages: int = Field(default=100, ge=0, le=100000,
        description="群当日非bot消息数低于此值时不建立/更新该群画像、也不纳入分享候选，以节省token并避免误发不活跃小群；0关闭门槛。陪伴私聊不受此限。",
        json_schema_extra={"label": "群每日活跃门槛（非bot消息数）"})
    daily_limit: int = Field(default=3, ge=0, le=10,
        description="所有目标合计的自动视频分享每日上限，包含发送中和结果不确定的预留；0停止自动发送。目标私聊还共用每日主动预算。",
        json_schema_extra={"label": "自动分享每日上限"})
    stream_daily_limit: int = Field(default=1, ge=1, le=5,
        description="同一聊天每天自动分享的视频数上限，同时受总上限约束。",
        json_schema_extra={"label": "单个聊天每日上限"})
    min_interval_hours: float = Field(default=6.0, ge=0.25, le=24.0,
        description="两次自动视频分享的最短间隔，跨午夜保留；不会因重启重新计时。",
        json_schema_extra={"label": "自动分享最短间隔（小时）"})
    candidate_ttl_hours: int = Field(default=24, ge=1, le=72,
        description="观看结果可等待分享的最长时间；过期后不再自动投递，观看记忆仍按原设置保留。",
        json_schema_extra={"label": "候选保留时间（小时）"})
    max_targets_per_video: int = Field(default=1, ge=1, le=3,
        description="同一视频最多自动分享给多少个不同聊天；每个目标仍须独立判断，默认只选一个。",
        json_schema_extra={"label": "单视频最多目标数"})
    profile_refresh_minutes: int = Field(default=30, ge=5, le=1440,
        description="有分享候选时，多久刷新一次允许聊天的有界历史；仅拉取新增内容，不定时扫描所有聊天。",
        json_schema_extra={"label": "话题刷新间隔（分钟）"})
    decision_task_name: str = Field(default="utils",
        description="独立分享判断使用的宿主文本任务名；不调用聊天planner，不给模型提供工具。",
        json_schema_extra={"label": "分享判断任务"})
