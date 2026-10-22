"""共享渲染工具（Pillow）：色板 tokens、像素测量截断/换行、anchor 绘制、2× 超采样。

消费方
======
- ``planning/schedule_image.py``（日程图 §6 版式，2026-10-23 §三十 落地）；
- ``cateye_dashboard.py``（状态总览图 §7 版式，2026-10-23 §三十 同批接线）。

设计纪律（照抄指导 §8.1/§8.2/§8.4 语义）
=========================================
- **先测量、后绘制**：所有文本经 :func:`fit_text` / :func:`wrap_text` 按**像素宽度**裁剪，
  禁止 ``text[:N]`` 字符截断；截断处必有 ``…``。
- **anchor 基线**：绘制统一用 Pillow 的 ``anchor="ls"/"rs"/"ms"``，不再用 ``y-2 / y+4 / y+8``
  目测偏移。
- **2× 超采样**：仅对 **1× 高度 ≤ 2000** 的页面开启 2×（:func:`supersample_scale`）；1× 高度
  先算好，2× 只放大像素、不改变布局，最后 ``resize(..., LANCZOS)`` 回到 1×。
- **4096 红线**：4096 高度上限按 **1× 高度**判断；超限由调用方先分页（正常内容），
  病态输入才由调用方钳制作为最后防线——本模块的 ``fit_text``/``wrap_text`` 输出有界，
  不会放大画布。

本模块不 import PIL（Pillow 是可选依赖）；:class:`Canvas` 由调用方把
``Image`` / ``ImageDraw`` 类传进来，与既有惰性导入契约一致。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ==================== 色板（指导 §5.1）====================
PAPER: Tuple[int, int, int] = (242, 241, 237)      # #F2F1ED 整页底色
INK: Tuple[int, int, int] = (17, 17, 17)           # #111111 主文字 / 2px 页眉页脚线
INK_SOFT: Tuple[int, int, int] = (63, 63, 63)      # #3F3F3F 引用 / 当前行描述
RED: Tuple[int, int, int] = (232, 64, 42)          # #E8402A 当前态 / 日期 / 序号
RED_DEEP: Tuple[int, int, int] = (201, 52, 31)     # #C9341F 小号红色标签（命令）
MUTED: Tuple[int, int, int] = (107, 107, 107)      # #6B6B6B 次级文字 / 时间
FAINT: Tuple[int, int, int] = (154, 150, 141)      # #9A968D 已完成 / 三级信息
HAIR: Tuple[int, int, int] = (205, 203, 196)       # #CDCBC4 分节 1px 线
ROWLINE: Tuple[int, int, int] = (223, 220, 212)    # #DFDCD4 行分隔 1px 线
TINT: Tuple[int, int, int] = (244, 231, 226)       # #F4E7E2 当前行底色
TRACK: Tuple[int, int, int] = (227, 224, 216)      # #E3E0D8 进度/密度条底

# 状态点补充色（指导 §5.4；不进主色板，只服务圆点）
DONE_DOT: Tuple[int, int, int] = (189, 185, 176)   # #BDB9B0 已完成实心灰点
RING_DOT: Tuple[int, int, int] = (168, 163, 154)   # #A8A39A 未开始空心环

#: 2× 超采样阈值（指导 §8.4：只对 ≤2000 高的页面开 2×，控内存）
SUPERSAMPLE_MAX_HEIGHT = 2000
#: 画布高度红线（指导 §8.3 / 计划书 §30.1）
CANVAS_MAX_HEIGHT = 4096
#: fit/wrap 输入预截（指导 §8.1）
TEXT_INPUT_LIMIT = 2000


# ==================== 测量与截断 ====================


def text_width(canvas: "Canvas", text: str, font: Any) -> float:
    """文本像素宽度（逻辑坐标；``Canvas`` 负责把 2× 度量折回 1×）。"""
    return canvas.textlength(text or "", font)


def fit_text(canvas: "Canvas", text: Any, font: Any, max_w: float) -> str:
    """按像素宽度裁剪，末尾加 ``…``（二分；统一走 ``textlength``，不做字符切片）。

    照抄指导 §8.1 的实现语义：整串放得下直接返回；省略号本身都放不下 → 空串。
    """
    value = str(text or "")
    if not value or text_width(canvas, value, font) <= max_w:
        return value
    # 先预截（防病态超长串在二分里反复度量整串）
    if len(value) > TEXT_INPUT_LIMIT:
        value = value[:TEXT_INPUT_LIMIT]
    ell = "…"
    if text_width(canvas, ell, font) > max_w:
        return ""
    lo, hi = 0, len(value)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_width(canvas, value[:mid] + ell, font) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return value[:lo] + ell


def wrap_text(canvas: "Canvas", text: Any, font: Any, max_w: float,
              max_lines: int) -> List[str]:
    """按像素宽度换行；有截断时最后一行加省略号；输入先预截 2000 字。

    照抄指导 §8.1 的实现语义；``max_lines`` 至少为 1，返回值恒非空（至少 ``[""]``）。
    """
    value = str(text or "").strip()[:TEXT_INPUT_LIMIT]
    max_lines = max(1, int(max_lines))
    lines: List[str] = []
    current = ""
    truncated = False
    for ch in value:
        if current and text_width(canvas, current + ch, font) > max_w:
            lines.append(current)
            current = ch
            if len(lines) >= max_lines:
                truncated = True
                break
        else:
            current += ch
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        return [""]
    if truncated:
        ell = "…"
        ell_w = text_width(canvas, ell, font)
        lines[-1] = fit_text(canvas, lines[-1], font, max(1.0, max_w - ell_w)) + ell
    return lines


def tracked_width(canvas: "Canvas", text: str, font: Any, tracking: float) -> float:
    """逐字绘制时的总宽度（``tracking`` = 字间像素，可为负）。"""
    if not text:
        return 0.0
    total = sum(text_width(canvas, ch, font) for ch in text)
    return total + float(tracking) * (len(text) - 1)


def draw_text(canvas: "Canvas", xy: Tuple[float, float], text: str, font: Any,
              fill: Tuple[int, ...], anchor: str = "ls") -> None:
    """anchor 绘制（默认左基线；``rs`` 右基线 / ``ms`` 居中基线）。"""
    if text:
        canvas.text(xy, text, font, fill, anchor=anchor)


def draw_tracked(canvas: "Canvas", xy: Tuple[float, float], text: str, font: Any,
                 fill: Tuple[int, ...], tracking: float = 0.0,
                 anchor: str = "ls") -> None:
    """带字距的文本绘制（拉丁小标签 5–6px；中文正文禁用——不得逐字加空格）。

    ``tracking`` 是**像素**字距，不是空格；中文排序仍连续绘制。
    """
    if not text:
        return
    width = tracked_width(canvas, text, font, tracking)
    x, y = float(xy[0]), float(xy[1])
    if anchor.startswith("r"):
        x -= width
    elif anchor.startswith("m"):
        x -= width / 2.0
    for ch in text:
        canvas.text((x, y), ch, font, fill, anchor="ls")
        x += text_width(canvas, ch, font) + tracking


def blend(color: Sequence[int], background: Sequence[int], alpha: float) -> Tuple[int, int, int]:
    """把 ``color`` 按 ``alpha`` 叠到 ``background`` 上（色条 opacity 0.4 等）。"""
    a = max(0.0, min(1.0, float(alpha)))
    return tuple(
        int(round(color[i] * a + background[i] * (1.0 - a))) for i in range(3)
    )  # type: ignore[return-value]


# ==================== 2× 超采样 ====================


def supersample_scale(height_1x: int,
                      threshold: int = SUPERSAMPLE_MAX_HEIGHT,
                      max_height: int = CANVAS_MAX_HEIGHT) -> int:
    """返回超采样倍率：1× 高度 ≤ ``threshold`` 且未触碰红线 → 2，否则 1。

    4096 红线按 **1× 高度**判断（``height_1x > max_height`` 时不放大，避免 2× 内存翻倍）。
    """
    try:
        height = int(height_1x)
    except (TypeError, ValueError):
        return 1
    if height <= 0 or height > max_height:
        return 1
    return 2 if height <= threshold else 1


# ==================== 绘制画布（逻辑坐标 + 可选 2×）====================


class Canvas:
    """逻辑坐标画布：所有绘制坐标/线宽按 ``scale`` 放大，字号同名放大后 LANCZOS 缩回。

    - ``font(loader, size, bold)``：``loader(size_px, bold) -> ImageFont``（由调用方提供，
      通常是 ``ScheduleImageGenerator._get_font`` 的闭包）；返回的字体已按 ``scale`` 放大。
    - ``textlength`` 返回值除以 ``scale``，因此 fit/wrap 的逻辑始终是 1× 像素。
    - ``finish()`` 在 ``scale > 1`` 时 ``resize((w, h), LANCZOS)``。
    """

    def __init__(self, Image: Any, ImageDraw: Any, width: int, height: int,
                 scale: int = 1, background: Tuple[int, int, int] = PAPER) -> None:
        self.Image = Image
        self.scale = max(1, int(scale))
        self.width = int(width)
        self.height = int(height)
        self.img = Image.new(
            "RGB", (self.width * self.scale, self.height * self.scale), background)
        self.draw = ImageDraw.Draw(self.img)
        self._fonts: Dict[Tuple[int, bool], Any] = {}

    # -- 字体与度量 -------------------------------------------------
    def font(self, loader: Callable[[int, bool], Any], size: float, bold: bool = False) -> Any:
        key = (int(round(size)), bool(bold))
        cached = self._fonts.get(key)
        if cached is None:
            cached = loader(max(1, int(round(size * self.scale))), bold)
            self._fonts[key] = cached
        return cached

    def textlength(self, text: str, font: Any) -> float:
        if not text:
            return 0.0
        return float(self.draw.textlength(text, font=font)) / self.scale

    def ascent(self, font: Any) -> int:
        ascent, _descent = font.getmetrics()
        return int(round(ascent / self.scale))

    def descent(self, font: Any) -> int:
        _ascent, descent = font.getmetrics()
        return int(round(descent / self.scale))

    # -- 绘制 -------------------------------------------------------
    def text(self, xy: Tuple[float, float], text: str, font: Any,
             fill: Tuple[int, ...], anchor: str = "ls") -> None:
        if not text:
            return
        self.draw.text(
            (float(xy[0]) * self.scale, float(xy[1]) * self.scale),
            text, font=font, fill=tuple(fill), anchor=anchor)

    def line(self, xy: Sequence[Any], fill: Tuple[int, ...], width: float = 1) -> None:
        """画线：接受 ``[(x0, y0), (x1, y1)]`` 或扁平 ``[x0, y0, x1, y1]``。"""
        values = list(xy)
        if values and isinstance(values[0], (tuple, list)):
            pts = [(float(p[0]) * self.scale, float(p[1]) * self.scale) for p in values]
        else:
            pts = [(float(values[i]) * self.scale, float(values[i + 1]) * self.scale)
                   for i in range(0, len(values) - 1, 2)]
        self.draw.line(pts, fill=tuple(fill),
                       width=max(1, int(round(width * self.scale))))

    def rectangle(self, box: Sequence[float], fill: Optional[Tuple[int, ...]] = None,
                  outline: Optional[Tuple[int, ...]] = None, width: float = 1) -> None:
        scaled = [float(v) * self.scale for v in box]
        self.draw.rectangle(
            scaled,
            fill=tuple(fill) if fill is not None else None,
            outline=tuple(outline) if outline is not None else None,
            width=max(1, int(round(width * self.scale))))

    def rounded_rectangle(self, box: Sequence[float], radius: float = 0,
                          fill: Optional[Tuple[int, ...]] = None,
                          outline: Optional[Tuple[int, ...]] = None,
                          width: float = 1) -> None:
        scaled = [float(v) * self.scale for v in box]
        self.draw.rounded_rectangle(
            scaled, radius=float(radius) * self.scale,
            fill=tuple(fill) if fill is not None else None,
            outline=tuple(outline) if outline is not None else None,
            width=max(1, int(round(width * self.scale))))

    def ellipse(self, box: Sequence[float], fill: Optional[Tuple[int, ...]] = None,
                outline: Optional[Tuple[int, ...]] = None, width: float = 1) -> None:
        scaled = [float(v) * self.scale for v in box]
        self.draw.ellipse(
            scaled,
            fill=tuple(fill) if fill is not None else None,
            outline=tuple(outline) if outline is not None else None,
            width=max(1, int(round(width * self.scale))))

    # -- 出口 -------------------------------------------------------
    def finish(self) -> Any:
        if self.scale != 1:
            self.img = self.img.resize((self.width, self.height), self.Image.LANCZOS)
        return self.img
