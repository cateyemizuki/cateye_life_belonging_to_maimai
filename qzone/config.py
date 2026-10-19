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
    __ui_order__ = 6
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
        json_schema_extra={
            "label": "每日说说上限",
            "hint": "0 = 当天不发说说",
            **_ui_i18n("Daily post limit"),
        },
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
    # ── 配图（2026-10-14 §18.1~§18.3；默认 emoji = 2026-10-20 用户改判，原默认 off）──────
    image_mode: str = Field(
        default="emoji",
        description="说说配图来源：off=不配图｜emoji=宿主表情包（按说说正文语义匹配）｜"
                    "library=本地随机图库｜random=两者各半掷骰。"
                    "任何一步取图失败都只跳过该图，全部失败发纯文本（配图失败永不阻断发布）；"
                    "旧配置里显式写过的 image_mode 值保持不变（默认值只影响未显式配置的用户）",
        json_schema_extra={
            "label": "配图来源",
            "hint": "off/emoji/library/random；emoji 与 library 都失败时自动退回纯文本",
            **_ui_i18n("Image source", "off / emoji / library / random"),
        },
    )
    image_library_dir: str = Field(
        default="",
        description="本地随机图库目录（填**绝对路径**；image_mode=library/random 时生效）："
                    "目录不存在、为空或读不出图 → 静默退回纯文本",
        json_schema_extra={
            "label": "图库目录",
            "placeholder": "D:/pictures/qzone",
            **_ui_i18n("Image library folder", "Absolute path; empty or missing folder falls back to text-only."),
        },
    )
    image_number: int = Field(
        default=1,
        ge=1,
        le=4,
        description="单条说说最多配几张图（1~4；表情包/图库随机取图共用该上限）",
        json_schema_extra={"label": "配图张数", **_ui_i18n("Images per post")},
    )
    # ── 发帖判定链（2026-10-14 §18.4；MaiTrace 判定链移植）────────────────────
    post_cooldown_minutes: int = Field(
        default=120,
        ge=0,
        le=1440,
        description="两条说说之间的最短间隔（分钟；0=不冷却）。锚点只在**发送成功后**更新，"
                    "失败不重置（防失败风暴）",
        json_schema_extra={
            "label": "发帖冷却(分钟)",
            "hint": "0 = 不冷却；冷却锚点成功发送后才前移",
            **_ui_i18n("Post cooldown (min)"),
        },
    )
    # 注（2026-10-20 用户指示）：原 post_blocked_activities（活动黑名单）已删除——「是否允许
    # 发空间」不再按活动类型判定，时段维度只看静默（恒复用 [schedule] 作息；开启
    # [patrol].nap_quiet_enabled 时叠加午休静默），内容质量由 LLM 严判把关。
    # 旧配置里的显式值由迁移器一并丢弃并登记。
    post_llm_gate_enabled: bool = Field(
        default=True,
        description="发帖前让模型严格判断「此刻发一条是否自然」（正反例 + 强烈压制「是」，"
                    "只认回复首字符的 是/否，格式异常一律按否）；关 = 只跑规则闸不花模型钱",
        json_schema_extra={"label": "发帖 LLM 严判", **_ui_i18n("LLM post gate")},
    )
    post_max_chance: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="LLM 严判通过后的掷骰上限（1.0=不掷骰；设 0.3 = 通过后仅 30% 真发，防模型误判刷屏）",
        json_schema_extra={"label": "严判后掷骰上限", "step": 0.05, **_ui_i18n("Post chance cap")},
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
        json_schema_extra={
            "label": "回评检查条数",
            "hint": "越大越不会漏回评论，检查量与模型成本也越大",
            **_ui_i18n("Reply window (feeds)"),
        },
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
    # 注（2026-10-20 用户指示）：原 silence_start / silence_end 两键已移除——空间静默窗恒复用
    # 日程作息（[schedule] sleep_time~wake_time，入睡~起床整夜不发说说、不监听评论），
    # 与陪伴静默共用同一解析真源 companion.patrol.resolve_silence_window；
    # 旧配置里的显式值由迁移器丢弃并登记。
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
