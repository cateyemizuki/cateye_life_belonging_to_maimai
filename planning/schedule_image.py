"""日程图片渲染器（来源：自主规划插件 utils/schedule_image_generator.py，v2 移植版）。

设计要点（照搬源插件 v2）：
- **展示全天全部日程**：按条目数动态计算画布高度，一行一条完整铺开；
- 干净的清单排版：浅色渐变背景 + 白色圆角卡片行；当前进行中的行用主题色
  描边高亮，已完成整行淡化；
- **自带字体**：打包 Noto Sans SC（SIL OFL 1.1，planning/assets/fonts/），
  找不到打包字体时回退系统字体链；
- 文字截断：活动名 / 描述按可用宽度测量并裁剪加省略号；
- 跨午夜时间：``23:00-31:00``（累计分钟）与 ``23:00-07:00``（回绕写法）都能解析。

对整合契约的适配：
- PIL（pillow）为可选依赖：import 惰性化，未安装时抛 RuntimeError，
  由调用方静默降级为纯文本（见 INTEGRATION.md 依赖清单）；
- 输出目录经 ``generate_schedule_image(output_dir=...)`` 传入（宿主隔离数据目录）。
"""

from __future__ import annotations

import base64
import io
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .time_utils import TimezoneManager

logger = logging.getLogger(__name__)


class ScheduleImageGenerator:
    """生成 /plan list 日程图片（v2 清单式排版）。"""

    # 并发限制（最多 3 个并发生成）
    _generation_semaphore = threading.Semaphore(3)

    # 打包字体（Noto Sans SC，SIL OFL 1.1，可随插件再分发）
    BUNDLED_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"

    # 分辨率限制（防止 OOM）
    MAX_WIDTH = 1920
    MAX_HEIGHT = 4096
    DEFAULT_WIDTH = 1080

    # ===== 活动类型 → 主题色（时间条与类型标记） =====
    TYPE_COLORS = {
        "meal": (242, 153, 74),
        "study": (74, 144, 226),
        "exercise": (39, 174, 96),
        "entertainment": (155, 81, 224),
        "social_maintenance": (235, 86, 142),
        "learn_topic": (0, 168, 168),
        "health_check": (22, 191, 194),
        "daily_routine": (127, 143, 166),
        "rest": (163, 183, 206),
        "free_time": (109, 143, 176),
        "sleep": (90, 105, 130),
        "custom": (110, 127, 149),
    }
    DEFAULT_TYPE_COLOR = (110, 127, 149)

    # ===== 配色 =====
    COLOR_TEXT_PRIMARY = (43, 58, 74)       # 活动名
    COLOR_TEXT_SECONDARY = (122, 140, 163)  # 描述
    COLOR_TEXT_TIME = (74, 105, 148)        # 时间
    COLOR_TEXT_MUTED = (168, 180, 196)      # 已完成淡化
    COLOR_ACCENT = (74, 144, 226)           # 主题色（进行中高亮）
    COLOR_CARD = (255, 255, 255)
    COLOR_CARD_BORDER = (226, 233, 242)
    COLOR_CURRENT_BG = (234, 243, 255)

    # 性能优化：字体缓存（PIL 对象在首次调用时才创建）
    _cached_fonts: Dict[Tuple[int, bool], Any] = {}

    # ------------------------------------------------------------
    # 资源加载（PIL 惰性导入；未安装时抛 RuntimeError 由调用方静默降级）
    # ------------------------------------------------------------

    @staticmethod
    def _require_pil():
        """惰性导入 PIL；未安装 pillow 时抛 RuntimeError（调用方静默降级为文本）。"""
        try:
            from PIL import Image, ImageDraw, ImageFont  # noqa: F401
            return Image, ImageDraw, ImageFont
        except ImportError as exc:
            raise RuntimeError(f"pillow 未安装，无法绘制日程图片: {exc}") from exc

    @classmethod
    def _load_logo(cls, Image: Any):
        """加载标题头像；目标插件暂无 logo 资源，使用主题色圆形占位。"""
        return Image.new("RGBA", (100, 100), (74, 144, 226, 255))

    @classmethod
    def _get_font(cls, ImageFont: Any, size: int, bold: bool = False):
        """获取字体（带缓存）；优先打包字体，失败回退系统常见中文字体。"""
        key = (size, bold)
        if key in cls._cached_fonts:
            return cls._cached_fonts[key]

        candidates = []
        if cls.BUNDLED_FONT_PATH.exists():
            candidates.append(str(cls.BUNDLED_FONT_PATH))
        candidates += [
            "C:/Windows/Fonts/msyh.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        ]

        for path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                # 验证中英数字都能渲染
                bbox = font.getbbox("日程09:30")
                if bbox[2] - bbox[0] > 0:
                    cls._cached_fonts[key] = font
                    if path != str(cls.BUNDLED_FONT_PATH):
                        logger.info(f"打包字体不可用，回退系统字体: {path} (size={size})")
                    return font
            except Exception:
                continue

        raise RuntimeError("未找到可用的中文字体（含打包字体 planning/assets/fonts/）")

    @classmethod
    def _draw_text(cls, draw: Any, xy: Tuple[int, int], text: str, fill: Tuple[int, ...],
                   font: Any, bold: bool = False) -> None:
        """绘制文字；bold 用同色 1px 描边模拟（打包字体为单一字重）。"""
        if bold:
            draw.text(xy, text, fill=fill, font=font, stroke_width=1, stroke_fill=fill)
        else:
            draw.text(xy, text, fill=fill, font=font)

    @classmethod
    def _text_width(cls, draw: Any, text: str, font: Any) -> int:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    @classmethod
    def _ellipsis(cls, draw: Any, text: str, font: Any, max_width: int) -> str:
        """按像素宽度裁剪文字，超出部分以 … 结尾。"""
        if not text:
            return ""
        if cls._text_width(draw, text, font) <= max_width:
            return text
        ell = "…"
        chars = list(text)
        while len(chars) > 1:
            candidate = "".join(chars) + ell
            if cls._text_width(draw, candidate, font) <= max_width:
                return candidate
            chars.pop()
        return ell

    # ------------------------------------------------------------
    # 时间与状态
    # ------------------------------------------------------------

    @staticmethod
    def _parse_time_str(time_str: str) -> Tuple[int, int]:
        """解析 ``HH:MM-HH:MM``，返回 (开始分钟, 结束分钟)。

        兼容两种跨午夜写法：累计分钟（``23:00-31:00``）与回绕写法（``23:00-07:00``）。
        解析失败返回 (0, 0)。
        """
        try:
            parts = str(time_str).split("-")
            if len(parts) != 2:
                return (0, 0)

            def _to_minutes(seg: str) -> Optional[int]:
                seg = seg.strip().split(":")[0:2]
                if len(seg) != 2:
                    return None
                hour, minute = int(seg[0]), int(seg[1])
                if minute < 0 or minute > 59 or hour < 0:
                    return None
                return hour * 60 + minute

            start = _to_minutes(parts[0])
            end = _to_minutes(parts[1])
            if start is None or end is None:
                return (0, 0)
            if 0 < end <= start:
                end += 24 * 60  # 回绕写法的跨午夜活动
            return (start, end)
        except (ValueError, IndexError, AttributeError):
            return (0, 0)

    @classmethod
    def _activity_status(cls, time_str: str, current_minutes: int) -> str:
        """获取活动状态: current / completed / upcoming（支持跨午夜）。"""
        start, end = cls._parse_time_str(time_str)
        if end == 0:
            return "upcoming"
        now = current_minutes
        if end > 24 * 60:
            in_evening = now >= start
            in_early_morning = now < end - 24 * 60
            return "current" if (in_evening or in_early_morning) else "upcoming"
        if start <= now < end:
            return "current"
        if now >= end:
            return "completed"
        return "upcoming"

    # ------------------------------------------------------------
    # 布局
    # ------------------------------------------------------------

    @classmethod
    def _build_rows(cls, draw: Any, schedule_items: List[Dict[str, Any]], now_minutes: int,
                    s: float, fonts: Dict[str, Any], max_text_width: int) -> List[Dict[str, Any]]:
        """为每个日程项计算展示信息（状态 / 颜色 / 截断后的文字）。"""
        rows = []
        for item in schedule_items:
            time_str = str(item.get("time", ""))
            name = str(item.get("name", "") or "")
            desc = str(item.get("description", "") or "")
            goal_type = str(item.get("goal_type", "custom") or "custom")
            status = cls._activity_status(time_str, now_minutes)

            if status == "current":
                pill_text = "进行中"
                pill_fill = (*cls.COLOR_ACCENT, 255)
                pill_text_color = (255, 255, 255)
            elif status == "completed":
                pill_text = "已完成"
                pill_fill = (245, 247, 250, 255)
                pill_text_color = cls.COLOR_TEXT_MUTED
            else:
                pill_text = "未开始"
                pill_fill = (240, 243, 248, 255)
                pill_text_color = cls.COLOR_TEXT_SECONDARY

            dim = status == "completed"
            rows.append({
                "time": time_str,
                "name": cls._ellipsis(draw, name, fonts["name"], max_text_width),
                "desc": cls._ellipsis(draw, desc, fonts["desc"], max_text_width),
                "status": status,
                "pill_text": pill_text,
                "pill_fill": pill_fill,
                "pill_text_color": pill_text_color,
                "color": cls.TYPE_COLORS.get(goal_type, cls.DEFAULT_TYPE_COLOR),
                "dim": dim,
                "has_desc": bool(desc),
            })
        return rows

    @classmethod
    def _draw_header(cls, img: Any, draw: Any, title: str, stats_text: str,
                     logo: Any, s: float, fonts: Dict[str, Any], ImageDraw: Any) -> None:
        """绘制头部：圆形头像 + 标题 + 统计副标题。"""
        margin_x = int(48 * s)

        logo_size = int(72 * s)
        logo_y = int(40 * s)

        # 圆形裁剪头像 + 细描边
        logo_avatar = logo.resize((logo_size, logo_size))
        mask = Image.new("L", (logo_size, logo_size), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, logo_size, logo_size], fill=255)
        img.paste(logo_avatar, (margin_x, logo_y), mask)
        draw.ellipse(
            [margin_x, logo_y, margin_x + logo_size, logo_y + logo_size],
            outline=(210, 222, 238), width=max(1, int(2 * s)),
        )

        text_x = margin_x + logo_size + int(24 * s)
        title_y = logo_y + int(4 * s)
        cls._draw_text(draw, (text_x, title_y), title, cls.COLOR_TEXT_PRIMARY, fonts["title"], bold=True)
        stats_y = title_y + int(52 * s)
        cls._draw_text(draw, (text_x, stats_y), stats_text, cls.COLOR_TEXT_SECONDARY, fonts["stats"])

    @classmethod
    def _draw_row_card(cls, draw: Any, row: Dict[str, Any], x: int, y: int,
                       w: int, h: int, s: float, fonts: Dict[str, Any]) -> None:
        """绘制单行日程卡片。"""
        radius = int(16 * s)
        is_current = row["status"] == "current"
        is_done = row["status"] == "completed"

        # 阴影（单层低透明度）
        shadow_offset = max(2, int(3 * s))
        draw.rounded_rectangle(
            [x, y + shadow_offset, x + w, y + h + shadow_offset],
            radius=radius, fill=(90, 115, 150, 28),
        )

        # 卡片背景与描边
        if is_current:
            fill = (*cls.COLOR_CURRENT_BG, 255)
            outline = (*cls.COLOR_ACCENT, 255)
            outline_width = max(2, int(2 * s))
        else:
            fill = (*cls.COLOR_CARD, 250)
            outline = (*cls.COLOR_CARD_BORDER, 255)
            outline_width = 1
        draw.rounded_rectangle(
            [x, y, x + w, y + h], radius=radius,
            fill=fill, outline=outline, width=outline_width,
        )

        # 左侧类型色条
        bar_x = x + int(16 * s)
        draw.rounded_rectangle(
            [bar_x, y + int(16 * s), bar_x + int(5 * s), y + h - int(16 * s)],
            radius=int(2 * s) + 1, fill=(*row["color"], 255),
        )

        text_color = cls.COLOR_TEXT_MUTED if is_done else cls.COLOR_TEXT_PRIMARY
        time_color = cls.COLOR_TEXT_MUTED if is_done else cls.COLOR_TEXT_TIME
        if is_current:
            time_color = cls.COLOR_ACCENT

        content_x = bar_x + int(5 * s) + int(20 * s)
        line1_y = y + int(16 * s)

        # 第一行：时间 + 活动名
        cls._draw_text(draw, (content_x, line1_y), row["time"], time_color, fonts["time"], bold=is_current)
        name_x = content_x + int(190 * s)
        cls._draw_text(draw, (name_x, line1_y - int(3 * s)), row["name"], text_color, fonts["name"], bold=is_current)

        # 第二行：描述（有才画）
        if row["has_desc"]:
            desc_y = line1_y + int(38 * s)
            cls._draw_text(draw, (name_x, desc_y), row["desc"],
                           cls.COLOR_TEXT_MUTED if is_done else cls.COLOR_TEXT_SECONDARY, fonts["desc"])

        # 右侧状态胶囊
        pill_w, pill_h = int(92 * s), int(34 * s)
        pill_x = x + w - pill_w - int(20 * s)
        pill_y = y + (h - pill_h) // 2
        draw.rounded_rectangle(
            [pill_x, pill_y, pill_x + pill_w, pill_y + pill_h],
            radius=pill_h // 2, fill=row["pill_fill"],
        )
        pill_font = fonts["pill"]
        text_w = cls._text_width(draw, row["pill_text"], pill_font)
        cls._draw_text(
            draw,
            (pill_x + (pill_w - text_w) // 2, pill_y + int(5 * s)),
            row["pill_text"], row["pill_text_color"], pill_font,
        )

    # ------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------

    @classmethod
    def generate_schedule_image(
        cls,
        title: str,
        schedule_items: List[Dict[str, Any]],
        width: Optional[int] = None,
        tz_name: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> Tuple[str, str]:
        """生成日程图片（v2：动态高度清单排版，展示全部条目）。

        Args:
            title: 标题文字（如 "今日日程 2026-09-19 周六"）
            schedule_items: 日程项列表，每项
                ``{"time": "HH:MM-HH:MM", "name", "description", "goal_type"}``
            width: 图片宽度（None=默认 1080）
            tz_name: 用于判定「进行中/已完成」的时区（IANA 名；None=默认）
            output_dir: 图片输出目录（宿主隔离数据目录；None=系统临时目录）

        Returns:
            (图片路径, base64编码字符串)

        Raises:
            RuntimeError: pillow 未安装或找不到可用字体（调用方静默降级为文本）。
        """
        cls._generation_semaphore.acquire()
        try:
            return cls._generate(title, schedule_items, width, tz_name, output_dir)
        finally:
            cls._generation_semaphore.release()

    @classmethod
    def _generate(
        cls,
        title: str,
        schedule_items: List[Dict[str, Any]],
        width: Optional[int],
        tz_name: Optional[str],
        output_dir: Optional[Path],
    ) -> Tuple[str, str]:
        Image, ImageDraw, ImageFont = cls._require_pil()

        # 1. 画布尺寸：宽度可配，高度按条目数动态计算
        width = min(width or cls.DEFAULT_WIDTH, cls.MAX_WIDTH)
        s = width / 1080

        # 2. 当前时刻与状态
        tz_manager = TimezoneManager(tz_name) if tz_name else TimezoneManager()
        now = tz_manager.get_now()
        now_minutes = now.hour * 60 + now.minute

        # 3. 字体与度量
        fonts = {
            "title": cls._get_font(ImageFont, int(34 * s)),
            "stats": cls._get_font(ImageFont, int(20 * s)),
            "time": cls._get_font(ImageFont, int(24 * s)),
            "name": cls._get_font(ImageFont, int(27 * s)),
            "desc": cls._get_font(ImageFont, int(21 * s)),
            "pill": cls._get_font(ImageFont, int(19 * s)),
            "footer": cls._get_font(ImageFont, int(17 * s)),
        }

        margin_x = int(48 * s)
        card_w = width - margin_x * 2
        pill_reserve = int(92 * s) + int(40 * s)   # 状态胶囊 + 右边距
        name_x_offset = int(190 * s)               # 时间列宽
        max_text_width = card_w - pill_reserve - name_x_offset - int(20 * s)

        # 预渲染一行用于测量（截断需要 draw 对象）
        measure_img = Image.new("RGB", (8, 8))
        measure_draw = ImageDraw.Draw(measure_img)
        rows = cls._build_rows(measure_draw, schedule_items or [], now_minutes, s, fonts, max_text_width)

        # 行高：有描述的两行，无描述的单行
        card_h_with_desc = int(88 * s)
        card_h_plain = int(58 * s)
        card_gap = int(12 * s)
        header_h = int(148 * s)
        footer_h = int(56 * s)
        body_padding_top = int(4 * s)

        cards_height = sum(
            card_h_with_desc if r["has_desc"] else card_h_plain for r in rows
        ) + max(0, len(rows) - 1) * card_gap
        height = int(header_h + body_padding_top + cards_height + (card_gap if rows else 0) + footer_h)
        height = min(height, cls.MAX_HEIGHT)

        # 4. 画布：浅色纵向渐变
        img = Image.new("RGB", (width, height), (246, 248, 252))
        draw = ImageDraw.Draw(img, "RGBA")
        top_color, bottom_color = (247, 249, 253), (235, 240, 248)
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = tuple(
                int(top_color[i] + (bottom_color[i] - top_color[i]) * ratio)
                for i in range(3)
            )
            draw.line([(0, y), (width, y)], fill=(*color, 255))

        # 5. 头部
        logo = cls._load_logo(Image)
        stats = cls._build_stats_text(rows)
        cls._draw_header(img, draw, title, stats, logo, s, fonts, ImageDraw)

        # 6. 日程卡片（全部条目）
        y = header_h + body_padding_top
        for row in rows:
            row_h = card_h_with_desc if row["has_desc"] else card_h_plain
            cls._draw_row_card(draw, row, margin_x, y, card_w, row_h, s, fonts)
            y += row_h + card_gap

        # 7. 底部签名
        signature = "Powered by Mai-Bot"
        sig_w = cls._text_width(draw, signature, fonts["footer"])
        cls._draw_text(
            draw,
            ((width - sig_w) // 2, height - footer_h + int(14 * s)),
            signature, (159, 176, 198), fonts["footer"],
        )

        # 8. 保存并编码
        save_dir = Path(output_dir) if output_dir else Path(cls.BUNDLED_FONT_PATH).parent.parent.parent
        save_dir.mkdir(parents=True, exist_ok=True)
        image_path = save_dir / "schedule_today.jpg"
        img.save(str(image_path), format="JPEG", quality=90, optimize=True)

        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="JPEG", quality=90, optimize=True)
        img_base64 = base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")

        return str(image_path), img_base64

    @classmethod
    def _build_stats_text(cls, rows: List[Dict[str, Any]]) -> str:
        """头部统计文案：共 N 项 · 已完成 x · 进行中 y"""
        total = len(rows)
        if total == 0:
            return "今天暂无日程"
        completed = sum(1 for r in rows if r["status"] == "completed")
        current = sum(1 for r in rows if r["status"] == "current")
        parts = [f"共 {total} 项"]
        if completed:
            parts.append(f"已完成 {completed}")
        if current:
            parts.append(f"进行中 {current}")
        return " · ".join(parts)
