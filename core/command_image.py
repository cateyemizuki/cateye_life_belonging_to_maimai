"""命令图（help 图片节点）：注册表 → 左栏命令树 + 右栏分组详情。

设计纪律（与 ``cateye_dashboard.py`` / ``planning/schedule_image.py`` 同口径）
==========================================================================
- 色板复用 :mod:`core.render_kit`：PAPER / INK / INK_SOFT / MUTED / FAINT / HAIR /
  ROWLINE / TINT / TRACK / RED，不新增颜色。
- **先测量、后绘制**：:func:`_measure` 只算布局与绘制指令，:func:`_draw` 按测量结果落笔；
  ``fit_text`` 按像素宽度截断（禁字符切片）。
- **PIL 惰性导入**：pillow 未安装 / 找不到中文字体 / 渲染异常 → ``RuntimeError``，
  调用方静默降级纯文本（命令帮助仍有 7 个文字节点，图片只是可选节点）。
- **双语可见**：左栏同时画 ``/lbm`` 与 ``/麦麦生活`` 两组命令树；右栏每条命令给
  双语完整用法 + 中文说明 + 权限标记。
- 输出文件 ``command_help.jpg``；返回值 ``(path, base64)``，只画 1 页（内容远低于
  4096 高度红线）。
"""

from __future__ import annotations

import base64
import io
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import command_registry as registry
from core import render_kit
from core.render_kit import (
    HAIR,
    INK,
    INK_SOFT,
    MUTED,
    PAPER,
    RED,
    ROWLINE,
    Canvas,
)

logger = logging.getLogger(__name__)

#: 版式（与 dashboard 同 1080 宽；左栏 280 命令树轨 + 竖分隔 + 右栏详情）。
PAGE_WIDTH = 1080
MARGIN_X = 64
LEFT_W = 280
DIVIDER_X = 380
RIGHT_X = 412
TEXT_RIGHT = 1016
HEADER_H = 96
BODY_TOP = 128
FOOTER_GAP = 26
FOOTER_H = 62
MAX_HEIGHT = 4096

#: 打包中文字体（与 planning/schedule_image.py 同一份资产；只按路径引用，不 import 业务包）。
_FONT_DIR = Path(__file__).resolve().parent.parent / "planning" / "assets" / "fonts"
_FONT_CANDIDATES = (
    str(_FONT_DIR / "NotoSansSC-Regular.ttf"),
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)

_FONTS: Dict[Tuple[int, bool], Any] = {}


def _require_pil():
    """惰性导入 PIL；未安装 pillow 时抛 RuntimeError（调用方静默降级）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont  # noqa: F401
        return Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError(f"pillow 未安装，无法绘制命令图: {exc}") from exc


def _get_font(ImageFont: Any, size: int, bold: bool = False):
    """中文字体（打包字体优先 → 系统字体回退）；无可用字体抛 RuntimeError。"""
    key = (int(size), bool(bold))
    if key in _FONTS:
        return _FONTS[key]
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, int(size))
            box = font.getbbox("命令图 Command 09:30")
            if box[2] - box[0] > 0:
                _FONTS[key] = font
                return font
        except Exception:
            continue
    raise RuntimeError("未找到可用的中文字体（含 planning/assets/fonts/）")


def _loader(ImageFont: Any):
    def _load(size: int, bold: bool = False):
        return _get_font(ImageFont, size, bold)
    return _load


def _add_text(items: List[Dict[str, Any]], x: float, baseline: float, value: Any,
              size: float, *, bold: bool = False, color: Tuple[int, int, int] = INK,
              max_w: Optional[float] = None, tracking: float = 0.0,
              anchor: str = "ls") -> None:
    items.append({"kind": "text", "x": float(x), "baseline": float(baseline),
                  "text": str(value or ""), "size": float(size), "bold": bool(bold),
                  "color": color, "max_w": max_w, "tracking": float(tracking),
                  "anchor": anchor})


def _measure(probe: Canvas, loader: Any) -> Dict[str, Any]:
    """测量遍：返回全部绘制指令、页面高度与左右栏底部（无 PIL 画布依赖）。"""
    items: List[Dict[str, Any]] = []

    # ── 页眉 ──
    items.append({"kind": "rect", "box": [MARGIN_X, 34, MARGIN_X + 14, 48], "fill": RED})
    _add_text(items, MARGIN_X + 28, 55, "MAI · COMMANDS", 19, bold=True, tracking=5.0)
    _add_text(items, TEXT_RIGHT, 55, "命令图 · /lbm · /麦麦生活", 18, bold=True,
              max_w=620, anchor="rs")
    items.append({"kind": "line", "box": [MARGIN_X, HEADER_H - 2, TEXT_RIGHT, HEADER_H - 2],
                  "color": INK, "width": 2})

    # ── 左栏：双根命令树（组级，动作数在括号内）──
    left_y = float(BODY_TOP)
    _add_text(items, MARGIN_X, left_y + 14, "命令树 COMMAND TREE", 13, bold=True,
              color=MUTED, tracking=1.0, max_w=LEFT_W)
    left_y += 34
    for lang in registry.LANGS:
        _add_text(items, MARGIN_X, left_y + 20, registry.root(lang), 21, bold=True,
                  color=RED, max_w=LEFT_W)
        left_y += 34
        for index, group in enumerate(registry.GROUPS):
            last = index == len(registry.GROUPS) - 1
            branch = "└─" if last else "├─"
            name = group.cn if lang == "cn" else group.en
            count = len(registry.group_commands(group))
            _add_text(items, MARGIN_X + 6, left_y + 15, f"{branch} {name} {count}",
                      15, color=INK_SOFT, max_w=LEFT_W)
            left_y += 25
        _add_text(items, MARGIN_X + 6, left_y + 15,
                  ("└─ 帮助" if lang == "cn" else "└─ help"), 15,
                  color=MUTED, max_w=LEFT_W)
        left_y += 38
    left_bottom = left_y

    # ── 右栏：分组详情（每条命令双语用法 + 中文一句 + 权限）──
    right_y = float(BODY_TOP)
    for index, group in enumerate(registry.GROUPS, 1):
        _add_text(items, RIGHT_X, right_y + 18,
                  f"{index:02d}  {group.en.upper()} / {group.cn}", 17, bold=True,
                  color=RED, tracking=1.0, max_w=TEXT_RIGHT - RIGHT_X)
        right_y += 27
        _add_text(items, RIGHT_X, right_y + 13, group.summary_cn, 13, color=MUTED,
                  max_w=TEXT_RIGHT - RIGHT_X)
        right_y += 26
        for cmd in registry.group_commands(group):
            _add_text(items, RIGHT_X, right_y + 15, registry.usage(cmd, "en"), 15,
                      bold=True, color=INK, max_w=TEXT_RIGHT - RIGHT_X)
            right_y += 21
            _add_text(items, RIGHT_X, right_y + 14, registry.usage(cmd, "cn"), 13,
                      color=MUTED, max_w=TEXT_RIGHT - RIGHT_X)
            right_y += 19
            perm = registry.perm_label(cmd.perm, "cn")
            _add_text(items, RIGHT_X, right_y + 14,
                      f"{cmd.action_cn} · {cmd.help_cn}  [{perm}]", 14,
                      color=INK_SOFT, max_w=TEXT_RIGHT - RIGHT_X)
            right_y += 24
            items.append({"kind": "line",
                          "box": [RIGHT_X, right_y, TEXT_RIGHT, right_y],
                          "color": ROWLINE, "width": 1})
            right_y += 9
        right_y += 14
    right_bottom = right_y

    body_bottom = max(left_bottom, right_bottom)
    items.append({"kind": "line",
                  "box": [DIVIDER_X, HEADER_H, DIVIDER_X, body_bottom],
                  "color": HAIR, "width": 1})

    footer_top = min(body_bottom + FOOTER_GAP, float(MAX_HEIGHT - FOOTER_H))
    items.append({"kind": "line",
                  "box": [MARGIN_X, footer_top, TEXT_RIGHT, footer_top],
                  "color": INK, "width": 2})
    footer_size = 14
    baseline = footer_top + 14 + probe.ascent(probe.font(loader, footer_size))
    _add_text(items, MARGIN_X, baseline,
              f"属于麦麦的生活 · 命令图 · {datetime.now().strftime('%Y-%m-%d')}",
              footer_size, color=MUTED, max_w=(TEXT_RIGHT - MARGIN_X) / 2)
    _add_text(items, TEXT_RIGHT, baseline, "真源：core/command_registry.py", footer_size,
              color=MUTED, max_w=(TEXT_RIGHT - MARGIN_X) / 2, anchor="rs")

    return {
        "items": items,
        "height": int(round(footer_top + FOOTER_H)),
        "left_bottom": left_bottom,
        "right_bottom": right_bottom,
        "body_bottom": body_bottom,
        "footer_top": footer_top,
    }


def _draw(canvas: Canvas, layout: Dict[str, Any], loader: Any) -> None:
    """绘制遍：只按测量结果落笔（文本在目标倍率下再次 fit）。"""
    for item in layout["items"]:
        kind = item["kind"]
        if kind == "text":
            font = canvas.font(loader, item["size"], item["bold"])
            text = item["text"]
            if item.get("max_w") is not None:
                text = render_kit.fit_text(canvas, text, font, item["max_w"])
            if not text:
                continue
            if item.get("tracking"):
                render_kit.draw_tracked(canvas, (item["x"], item["baseline"]), text, font,
                                        item["color"], item["tracking"],
                                        item.get("anchor", "ls"))
            else:
                render_kit.draw_text(canvas, (item["x"], item["baseline"]), text, font,
                                     item["color"], item.get("anchor", "ls"))
        elif kind == "rect":
            canvas.rectangle(item["box"], fill=item.get("fill"))
        elif kind == "line":
            canvas.line(item["box"], item["color"], item.get("width", 1))


def render_command_image(output_dir: Optional[Path] = None) -> Tuple[str, str]:
    """渲染命令图并保存 ``command_help.jpg``，返回 ``(path, base64)``。

    Raises:
        RuntimeError: pillow/字体/渲染任一步失败（调用方静默降级为纯文本）。
    """
    try:
        Image, ImageDraw, ImageFont = _require_pil()
        loader = _loader(ImageFont)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"命令图渲染链不可用: {exc}") from exc

    try:
        probe = Canvas(Image, ImageDraw, PAGE_WIDTH, 1, scale=1)
        layout = _measure(probe, loader)
        save_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
        save_dir.mkdir(parents=True, exist_ok=True)
        scale = render_kit.supersample_scale(layout["height"])
        canvas = Canvas(Image, ImageDraw, PAGE_WIDTH, layout["height"], scale=scale,
                        background=PAPER)
        _draw(canvas, layout, loader)
        image = canvas.finish()
        path = save_dir / "command_help.jpg"
        image.save(str(path), format="JPEG", quality=95, subsampling=0, optimize=True)
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True)
        return str(path), base64.b64encode(buffer.getvalue()).decode("utf-8")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"命令图渲染失败: {exc}") from exc
