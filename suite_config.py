"""猫眼生活套件（cateye_test）插件 — 顶层配置聚合。

- 文件名刻意不叫 ``config.py``：宿主 Runner 加载插件时 sys.path 含宿主 ``src``，
  且 cateye_harness 与本插件同进程加载，顶层 ``config`` 会被宿主 ``src/config`` 或
  兄弟插件抢占（L1 实证），故改名并由 plugin.py 顶部引导头接管自身模块解析。
- 三个业务子模块各自拥有自己的配置 section 类（模块化维护）：
- planning/config.py   自主规划：schedule / inject / planning_proactive / planning_cleanup
- companion/config.py  虚拟恋人：patrol / affection / cateye
- qzone/config.py      QQ 空间：qzone

顶层只保留跨模块共享的基础段（plugin / target / admin），并把各子模块 section
扁平挂载为聚合模型的顶层字段（嵌套段 WebUI 不渲染）。
"""

from __future__ import annotations

from typing import ClassVar, Dict, List

from maibot_sdk import Field, PluginConfigBase

from companion.config import (
    AffectionSectionConfig,
    CateyeScreenSectionConfig,
    JudgeSectionConfig,
    PatrolSectionConfig,
)
from emotion.config import EmotionSectionConfig
from planning.config import (
    InjectSectionConfig,
    PlanningCleanupSectionConfig,
    PlanningProactiveSectionConfig,
    ScheduleSectionConfig,
)
from qzone.config import QzoneSectionConfig

# 与 _manifest.json 的 version 保持同步。
SUPPORTED_CONFIG_VERSION = "0.2.0"


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            **_ui_i18n("Enable plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "label": "配置版本",
            "hidden": True,
            "disabled": True,
            **_ui_i18n("Config version"),
        },
    )


class TargetSectionConfig(PluginConfigBase):
    """目标用户（虚拟恋人的聊天对象）。"""

    __ui_label__ = "目标用户"
    __ui_icon__ = "person"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Target user", "description": "The private-chat user the companion features revolve around."}
    }

    target_qq: int = Field(
        default=123456789,
        description="恋人私聊对象的 QQ 号（主动聊天、想念、好感度都只对这个会话生效）",
        json_schema_extra={
            "label": "目标 QQ",
            "hint": "保持默认占位值时插件不会主动私聊任何人",
            **_ui_i18n("Target QQ", "Proactive chat stays off while the placeholder is kept."),
        },
    )


class AdminSectionConfig(PluginConfigBase):
    """管理与日志（跨模块共享）。"""

    __ui_label__ = "管理与日志"
    __ui_icon__ = "shield"
    __ui_order__ = 6
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Admin & Logs", "description": "Command permissions and diagnostic logs."}
    }

    admin_users: List[str] = Field(
        default_factory=list,
        description="命令管理员（QQ 号或 qq:123456 形式）；留空=仅本机控制台可用管理子命令",
        json_schema_extra={"label": "管理员", **_ui_i18n("Admin users")},
    )
    allowed_streams: List[str] = Field(
        default_factory=list,
        description="命令/注入允许的会话（session_id）；留空=不限制",
        json_schema_extra={"label": "允许会话", **_ui_i18n("Allowed streams")},
    )
    llm_log_enabled: bool = Field(
        default=True,
        description="记录 LLM 调用日志（JSONL，按天分文件）",
        json_schema_extra={"label": "LLM 日志", **_ui_i18n("LLM log")},
    )
    decision_log_enabled: bool = Field(
        default=True,
        description="记录巡检/空间/规划决策日志（JSONL，按天分文件）",
        json_schema_extra={"label": "决策日志", **_ui_i18n("Decision log")},
    )
    log_retention_days: int = Field(
        default=3,
        ge=1,
        le=30,
        description="日志保留天数",
        json_schema_extra={"label": "日志保留天数", **_ui_i18n("Log retention (days)")},
    )


class CateyeSuiteConfig(PluginConfigBase):
    """插件完整配置。

    注意：子分组必须扁平挂为顶层字段（嵌套段 WebUI 不渲染）。
    各 section 类定义在各子模块的 config.py 中（模块化维护）。
    """

    # 基础段（本文件定义）
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    target: TargetSectionConfig = Field(default_factory=TargetSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    # 自主规划（planning/config.py）
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    inject: InjectSectionConfig = Field(default_factory=InjectSectionConfig)
    planning_proactive: PlanningProactiveSectionConfig = Field(default_factory=PlanningProactiveSectionConfig)
    planning_cleanup: PlanningCleanupSectionConfig = Field(default_factory=PlanningCleanupSectionConfig)
    # 虚拟恋人（companion/config.py）
    patrol: PatrolSectionConfig = Field(default_factory=PatrolSectionConfig)
    affection: AffectionSectionConfig = Field(default_factory=AffectionSectionConfig)
    cateye: CateyeScreenSectionConfig = Field(default_factory=CateyeScreenSectionConfig)
    # 判定类 LLM 调用（companion/config.py；想念把关等）
    judge: JudgeSectionConfig = Field(default_factory=JudgeSectionConfig)
    # QQ 空间（qzone/config.py）
    qzone: QzoneSectionConfig = Field(default_factory=QzoneSectionConfig)
    # 情绪系统（emotion/config.py）
    emotion: EmotionSectionConfig = Field(default_factory=EmotionSectionConfig)
