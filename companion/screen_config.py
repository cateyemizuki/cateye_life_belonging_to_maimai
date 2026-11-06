"""屏幕感知配置及旧字段的纯内存迁移。"""
from __future__ import annotations

from typing import Any, ClassVar, Literal

from maibot_sdk import Field, PluginConfigBase

from core.i18n import ui_i18n
from core.internal_tuning import tuning_value


class ScreenSectionConfig(PluginConfigBase):
    """通过本地客户端查看陪伴对象的电脑屏幕。"""

    __ui_label__ = "屏幕感知"
    __ui_icon__ = "monitor"
    # 同序时沿用模型字段顺序：紧接 patrol，早于 affection。
    __ui_order__ = 2
    __ui_i18n__: ClassVar[dict] = {
        "en": {"title": "Screen awareness", "description": "Companion screen awareness and on-demand viewing."}
    }

    enabled: bool = Field(default=False, description="陪伴主动聊天前查看电脑屏幕，并将屏幕概况提供给 bot。",
        json_schema_extra={"label": "启用自动屏幕感知", "hint": "需要统一连接插件及在线的本地客户端。",
                           **ui_i18n("Automatic screen awareness")})
    tool_enabled: bool = Field(default=False, description="允许 bot 在陪伴目标的私聊中调用工具查看当前电脑屏幕。",
        json_schema_extra={"label": "启用屏幕窥探工具", "hint": "默认关闭；仅陪伴目标私聊可用，与自动感知开关独立。",
                           **ui_i18n("Enable screen viewing tool")})
    blur: int = Field(default=0, ge=0, le=64, description="截图的高斯模糊半径；0 保持清晰。",
        json_schema_extra={"label": "截图模糊半径", "hint": "数值越大，画面细节越模糊。", **ui_i18n("Screenshot blur")})
    scope: Literal["main", "all"] = Field(default="main", description="main 查看主屏，all 查看全部可用屏幕。",
        json_schema_extra={"label": "屏幕范围", "hint": "多屏需要本地客户端支持；负载过大时可能降级。",
                           **ui_i18n("Screen scope")})

    @property
    def screen_timeout_seconds(self) -> float:
        return float(tuning_value("patrol", "screen_timeout_seconds"))

    @property
    def screen_context_ttl_minutes(self) -> int:
        return int(tuning_value("patrol", "screen_context_ttl_minutes"))


def screen_config_of(config: Any) -> Any:
    """新段优先；旧测试桩或升级中的配置仍可读取旧段。"""
    section = getattr(config, "screen", None)
    return section if section is not None else getattr(config, "patrol", None) or getattr(config, "cateye", None)


def migrate_screen_config(value: Any) -> Any:
    """在 SDK 填默认值之前搬段；显式新值（包括 false/0）优先。"""
    if not isinstance(value, dict):
        return value
    out = dict(value)
    screen = dict(out.get("screen") or {})
    for section, mapping in (
        ("patrol", {"screen_enabled": "enabled", "screen_blur": "blur", "screen_scope": "scope"}),
        ("cateye", {"enabled": "enabled", "screenshot_blur": "blur", "screen_scope": "scope"}),
    ):
        old = out.get(section)
        if not isinstance(old, dict):
            continue
        old = dict(old)
        for before, after in mapping.items():
            if before in old:
                screen.setdefault(after, old.pop(before))
        out[section] = old
    if screen or "screen" in out:
        out["screen"] = screen
    return out


def rescue_screen_config(value: Any, disk: Any) -> Any:
    """宿主已按新版本补默认时，从尚未覆写的旧磁盘文件保住跨段值。

    layout_version 只描述早期总布局，不能阻止本次独立搬段。磁盘新段已显式
    存在的键不抢救，保持用户的新设置（包括 false/0）；只搬尚未迁移的旧键。
    """
    out = migrate_screen_config(value)
    if not isinstance(out, dict) or not isinstance(disk, dict):
        return out
    explicit = disk.get("screen") if isinstance(disk.get("screen"), dict) else {}
    rescued = migrate_screen_config(disk).get("screen", {})
    legacy = disk.get("patrol") if isinstance(disk.get("patrol"), dict) else {}
    ancient = disk.get("cateye") if isinstance(disk.get("cateye"), dict) else {}
    screen = dict(out.get("screen") or {})
    for old, new, oldest in (("screen_enabled", "enabled", "enabled"),
                             ("screen_blur", "blur", "screenshot_blur"),
                             ("screen_scope", "scope", "screen_scope")):
        if new not in explicit and (old in legacy or oldest in ancient):
            screen[new] = rescued[new]
    if screen:
        out = dict(out)
        out["screen"] = screen
    return out
