"""配置字段级英文翻译（2026-11-02 质量清单 P2-05）。

七个 config 模块此前各自定义 ``_ui_i18n``，其中 video_watch 还用了不同的
返回键 ``_ui_i18n``（宿主 WebUI 与其余 14 段读取的是 ``i18n``，因此该段英文
翻译实际不生效）。本模块提供唯一实现；调用方按需 ``as _ui_i18n`` 导入，
保持现有调用点不变。

本模块只依赖标准库，不导入业务模块或 plugin。
"""

from __future__ import annotations

from typing import Dict


def ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 pydantic ``json_schema_extra``）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}
