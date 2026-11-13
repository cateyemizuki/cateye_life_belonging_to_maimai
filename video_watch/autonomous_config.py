# -*- coding: utf-8 -*-
"""video_autonomous 配置分组（3.17.37 视频自主观看批：日程主动观看的数据过滤）。

承载「bot 日程触发的主动视频观看」的候选数据门槛。与 [video_watch]（观看机制：
采样/队列/观后感）、[video_share]（观看后的分享投递）职责分离：本段只管「主动
观看前按视频公开数据筛掉低热度候选」，**不影响工具点播**——用户经
``lbm_watch_bili_video`` 直接给链接/BV 的查看走 service.submit，从不经过候选
prepare/pick，故本段任何阈值都不约束工具路径（分叉点之后、仅主动侧加闸）。

口径（与 integrations/video_interest.py 候选源一致）：
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
    """日程触发的主动视频观看：按播放量/评论/投币/收藏筛掉低热度候选。只影响自主选片，工具点播不受任何阈值约束。"""

    __ui_label__ = "视频自主观看"
    __ui_icon__ = "tv"
    # 与 video_watch / video_share 同为 order=12（2026-11-14 段序修正批整体顺延，
    # 原 order=10）；声明序插在 video_watch 之后、video_share 之前 →
    # 渲染序 video_watch → video_autonomous → video_share（稳定排序）。
    __ui_order__ = 12
    __ui_i18n__: ClassVar[dict] = {"en": {"title": "Autonomous video watching"}}

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
