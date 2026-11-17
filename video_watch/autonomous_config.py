# -*- coding: utf-8 -*-
"""video_autonomous 配置：自主观看开关、日程机会、单段/每日额度及候选质量。

与 [video_watch]（观看机制：采样/队列/观后感）、[video_share]（观看后的分享投递）
分工：本段控制属性日程、旧内置事件与闲暇顺便观看的自主发起，不控制工具点播。
用户经 ``lbm_watch_bili_video`` 直接给链接/BV 的查看仍只受 video_watch.enabled
与观看服务自身约束，不消耗本段的自主场次/分钟额度。

生产默认：每日总 8 场、专属日程 4 场、240 分钟；闲暇日程独立每日 3 场，
每个明确闲暇 episode 只掷一次 10% 概率；单段连续观看最多 10 支。
这些值均从本段配置读取，调整配置不需要修改协调器中的旧兼容常量。

候选质量口径（与 integrations/video_interest.py 一致）：
- 阈值只作用于日程/空隙/调试命令触发的**自主选片**（BiliCandidateSource.prepare/pick）；
- 每项阈值 0 = 不检查该项；播放量默认 50000，其余三项默认 0（保守，不预设压低观看量）；
- 评论/投币/收藏仅在候选阶段**真实可得**时参与检查（popular 响应自带 stat）；
  关键词搜索路拿不到这三项统计 → 缺失即跳过该项，绝不假装检查（降级不编造）。

迁移取舍（3.17.37，对照侦查地图 migrationCandidates 与实际消费方）：
[video_share] 的 video_share_quality_gate/threshold 语义上虽属「内容质量」，但其
唯一消费方是 Qzone 发布桥（质量分=0.6×|ΔVAD|+0.4×目标兴趣重合，信号面与观看候选
完全不同），且默认关是面向用户的分享量承诺——故**保留在 [video_share] 不动**，
观看侧另设本段独立统计阈值（不复用同一开关同时管「看什么」和「发什么」）。
"""
from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

from core.i18n import ui_i18n as _ui_i18n


class VideoAutonomousSectionConfig(PluginConfigBase):
    """自主观看开关、日程机会与额度，以及公开数据质量门槛；工具点播独立。"""

    __ui_label__ = "视频自主观看"
    __ui_icon__ = "tv"
    # 与 video_watch / video_share 同为 order=12（2026-11-14 段序修正批整体顺延，
    # 原 order=10）；声明序插在 video_watch 之后、video_share 之前 →
    # 渲染序 video_watch → video_autonomous → video_share（稳定排序）。
    __ui_order__ = 12
    __ui_i18n__: ClassVar[dict] = {"en": {"title": "Autonomous video watching"}}

    enabled: bool = Field(default=True, description="启用属性日程、内置刷视频事件及闲暇顺便观看；关闭后工具点播仍由视频观看总开关独立控制。",
        json_schema_extra={"label":"启用自主观看"})
    schedule_casual_enabled: bool = Field(default=True, description="明确闲暇、可中断的普通日程，每段只判断一次是否顺便看一支；独立于活动模块属性null。",
        json_schema_extra={"label":"闲暇日程顺便看一支"})
    schedule_casual_probability: float = Field(default=.10, ge=0, le=1, description="每段符合条件的日程只掷一次，结果落盘；不是每次tick重试。",
        json_schema_extra={"label":"每段日程观看概率","step":.05})
    schedule_casual_daily_limit: int = Field(default=3, ge=0, le=30, description="每日闲暇日程顺便观看的场次上限；每段最多一支，0关闭。",
        json_schema_extra={"label":"每日顺便观看上限"})
    schedule_max_videos: int = Field(default=10, ge=1, le=100, description="同一刷视频属性日程最多提交多少支真实候选；仍受该段剩余时间、总额度、睡眠与聊天守护。",
        json_schema_extra={"label":"单段刷视频最多支数"})
    daily_session_limit: int = Field(default=8, ge=0, le=100, description="所有自主观看合计每日场次上限；一段连续刷视频只计一场，0关闭。",
        json_schema_extra={"label":"每日自主观看总场次"})
    daily_dedicated_limit: int = Field(default=4, ge=0, le=100, description="每日刷视频属性日程及旧内置刷视频事件的场次上限。",
        json_schema_extra={"label":"每日刷视频日程上限"})
    daily_minutes_limit: int = Field(default=240, ge=0, le=1440, description="每日自主观看预留分钟总上限，取消或结束时按真实处理用时结算；工具点播不计入。",
        json_schema_extra={"label":"每日自主观看分钟上限"})
    daily_gap_limit: int = Field(default=2, ge=0, le=30, description="没有活动日程时的空隙冲浪每日场次上限；与普通日程顺便观看分别计数。",
        json_schema_extra={"label":"每日空隙观看上限"})
    gap_min_interval_minutes: int = Field(default=60, ge=0, le=1440, description="无日程的两场空隙冲浪启动间隔；不限制每段日程只掷一次的顺便观看。",
        json_schema_extra={"label":"空隙观看最短间隔（分钟）"})

    min_views: int = Field(
        default=50000, ge=0, le=100000000,
        description="主动观看播放量门槛：日程/空隙自主选片时，播放量低于此值的候选不主动看；0=不检查播放量。"
                    "只影响日程触发的主动观看，工具点播（用户直接给链接/BV）不受此阈值约束。",
        json_schema_extra={"label": "播放量门槛", "step": 1000,
                           "hint": "默认 50000；0=不检查。仅主动观看选片生效，工具点播不受影响",
                           **_ui_i18n("Minimum views",
                                      "Proactive candidate gate: skip videos below this view count; 0 disables. "
                                      "Tool-requested watches are never filtered.")},
    )
    min_replies: int = Field(
        default=0, ge=0, le=10000000,
        description="主动观看评论数门槛：评论数低于此值的候选不主动看；0=不检查评论数（默认）。"
                    "评论数仅在候选阶段真实可得时参与检查（热门排行榜自带）；关键词搜索候选拿不到评论数则跳过该项，不假装检查。"
                    "只影响主动观看，工具点播不受约束。",
        json_schema_extra={"label": "评论数门槛", "step": 10,
                           "hint": "默认 0=不检查；仅统计可得时生效，工具点播不受影响",
                           **_ui_i18n("Minimum replies",
                                      "Proactive gate on comment count; 0 disables. Only checked when the stat is "
                                      "available; tool-requested watches are never filtered.")},
    )
    min_coins: int = Field(
        default=0, ge=0, le=10000000,
        description="主动观看投币数门槛：投币数低于此值的候选不主动看；0=不检查投币数（默认）。"
                    "投币数仅在候选阶段真实可得时参与检查（热门排行榜自带）；关键词搜索候选拿不到投币数则跳过该项，不假装检查。"
                    "只影响主动观看，工具点播不受约束。",
        json_schema_extra={"label": "投币数门槛", "step": 10,
                           "hint": "默认 0=不检查；仅统计可得时生效，工具点播不受影响",
                           **_ui_i18n("Minimum coins",
                                      "Proactive gate on coin count; 0 disables. Only checked when the stat is "
                                      "available; tool-requested watches are never filtered.")},
    )
    min_favorites: int = Field(
        default=0, ge=0, le=10000000,
        description="主动观看收藏数门槛：收藏数低于此值的候选不主动看；0=不检查收藏数（默认）。"
                    "收藏数仅在候选阶段真实可得时参与检查（热门排行榜自带）；关键词搜索候选拿不到收藏数则跳过该项，不假装检查。"
                    "只影响主动观看，工具点播不受约束。",
        json_schema_extra={"label": "收藏数门槛", "step": 10,
                           "hint": "默认 0=不检查；仅统计可得时生效，工具点播不受影响",
                           **_ui_i18n("Minimum favorites",
                                      "Proactive gate on favorite count; 0 disables. Only checked when the stat is "
                                      "available; tool-requested watches are never filtered.")},
    )
