"""日程图片渲染器 —— 指导 §6 固定列网格版式（R7：2026-10-23 用户直派【按样张重绘指导】）。

规格真源：``开发文档/2026-10-23-渲染图重绘指导/重绘指导.md``（计划书 §三十冻结口径）。
本文件是 ``core/render_kit.py`` 的第一个消费者：

- **固定列网格** ``44 / 172 / 132 / 1fr / 92``（内容原点 x=64）：序号 64、时间 108、
  类型 280（色条固定 x=300、标签 313）、名称 412（最大宽 488）、状态右缘 1016；
  行底色出血 48..1032，文字永不越过 64..1016。
- **行高** ``15 + 58 + 15 + 1 = 89``：无描述也保留 58 的名称区，所有行活动名基线一致。
- **三种状态**：已完成（灰实心点 + faint + 色条 0.4）、进行中（tint 出血 + 4px 红左线 +
  红实心点）、未开始（空心环 + muted）。
- **跨午夜两行制**：主行 ``23:00–07:00``（en dash）+ 第二行 12px ``次日``；时钟文本一律由
  :func:`planning.time_utils.format_day_minute` 派生，不手写 ``//60`` / ``−1440``。
- **分页**（R7/§6.10/§8.3）：1~24 条单页；25~40 条每页 24 条（页脚左侧 ``1/2`` 页码）；
  >40 条只画当日窗口（前 24 条）并在脚注注明「其余见文字版」；0 条一条 20px muted 空态。
- **先量后画**：第一遍量行数/脚注行数算总高，第二遍绘制；footer 由内容底反推。
- **输出契约不变**（R6）：文件名 ``schedule_today.jpg``；多页时附页写
  ``schedule_today_p2.jpg`` 等，返回值仍是**第一页** ``(path, base64)``，调用方零改动。
  JPEG 参数按指导 §8.5 提升为 ``quality=95, subsampling=0``（4:4:4）。
- **2× 超采样**：仅 1× 高度 ≤2000 的页面开 2×（``core.render_kit.supersample_scale``）；
  4096 红线按 1× 高度判断，正常内容靠分页满足，病态输入由 ``MAX_HEIGHT`` 钳制作最后防线。
- **异常契约不变**：Pillow 缺失 / 无可用中文字体 / 渲染异常 → ``RuntimeError``，
  调用方（``CommandService._draw_schedule_image``）静默降级为文字。
"""

from __future__ import annotations

import base64
import io
import logging
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core import render_kit
from core.render_kit import (
    FAINT,
    HAIR,
    INK,
    INK_SOFT,
    MUTED,
    PAPER,
    RED,
    RED_DEEP,
    RING_DOT,
    DONE_DOT,
    ROWLINE,
    TINT,
    TRACK,
    Canvas,
)

from .time_utils import DAY_MINUTES, TimezoneManager, format_day_minute

logger = logging.getLogger(__name__)


class ScheduleImageGenerator:
    """生成 ``/lbm_plan list`` 日程图片（指导 §6 固定列网格版式）。"""

    # 并发限制（最多 3 个并发生成）
    _generation_semaphore = threading.Semaphore(3)

    # 打包字体（Noto Sans SC，SIL OFL 1.1，可随插件再分发）
    BUNDLED_FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Regular.ttf"
    BUNDLED_FONT_BOLD_PATH = (
        Path(__file__).resolve().parent / "assets" / "fonts" / "NotoSansSC-Bold.otf")

    # 分辨率限制（防止 OOM）
    MAX_WIDTH = 1920
    MAX_HEIGHT = 4096
    DEFAULT_WIDTH = 1080

    # ===== 活动类型 → 主题色（属性色缺失时的回退链，§24.3 冻结）=====
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

    #: 类型列标签兜底（调用方 ``CommandService`` 恒注入 ``property``；此处只服务直接调用/测试）。
    TYPE_LABELS = {
        "meal": "用餐",
        "study": "学习",
        "work": "工作",
        "exercise": "运动",
        "entertainment": "娱乐",
        "social_maintenance": "社交",
        "learn_topic": "兴趣",
        "health_check": "健康",
        "daily_routine": "日常",
        "rest": "休息",
        "free_time": "空闲",
        "sleep": "睡觉",
        "custom": "空闲",
    }

    # ===== 固定列网格（§5.3 / §8.2；相对内容原点 x=64）=====
    COLS = {
        "idx": (0, 44, "left"),
        "time": (44, 172, "left"),
        "cat": (216, 132, "left"),    # 列 x=280；色条 x=300（+20 内边距）
        "name": (348, 512, "left"),   # 绝对 x=412；最大宽 488（状态列前留 24）
        "status": (860, 92, "right"), # 绝对 x=924..1016
    }
    MARGIN_X = 64
    CONTENT_W = 952
    ROW_BLEED_X0 = 48
    ROW_BLEED_X1 = 1032
    TEXT_RIGHT = 1016
    ABS_IDX = 64
    ABS_TIME = 108
    ABS_CAT = 280
    BAR_X = 300
    CAT_TEXT_X = 313
    ABS_NAME = 412
    NAME_MAX_W = 488
    ABS_STATUS_RIGHT = 1016

    # ===== 纵向节奏（§6.1 实测；整数化落 Pillow）=====
    HEADER_H = 68
    HERO_H = 119
    RIBBON_MARGIN_TOP = 26
    RIBBON_H = 28
    COLHEAD_MARGIN_TOP = 22
    COLHEAD_H = 29
    ROWS_TOP = HEADER_H + HERO_H + RIBBON_MARGIN_TOP + RIBBON_H \
        + COLHEAD_MARGIN_TOP + COLHEAD_H          # 292
    ROW_H = 89                                    # 15 + 58 + 15 + 1
    ROW_PAD_TOP = 15
    ROW_NAME_BLOCK_H = 58
    #: 描述基线 = 名称基线 + round(17 * 1.35)（指导 §8.1）
    DESC_BASELINE_OFFSET = int(round(17 * 1.35))
    #: 跨午夜第二行「次日」基线偏移（23px 主行 ×1.1 + 3px 间距，§6.6 mockup）
    TIME_NEXT_OFFSET = 28
    EMPTY_BLOCK_H = 58
    NOTES_MARGIN_TOP = 30
    NOTES_PAD_TOP = 18
    NOTE_LINE_H = 26
    COMMANDS_GAP = 12
    CMD_LINE_H = 24
    FOOTER_MARGIN_TOP = 32
    FOOTER_H = 62

    #: 每页条数（§6.10：25~40 每页 24 条）
    PAGE_SIZE = 24

    #: 脚注口径行（§6.8）
    NOTE_TEXT = "跨午夜条目按 24h 环标注「次日」；凌晨 00:30 起与昨日帧合并判定。"
    #: 命令唯一一次（§6.8）
    COMMAND_SEGMENTS = (
        "完整日程 /lbm_plan list",
        "重新生成 /lbm_plan regenerate",
        "今日更新 /lbm_mai_diag decision 1",
    )

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
    def _get_font(cls, ImageFont: Any, size: int, bold: bool = False):
        """获取字体（带缓存）；优先打包字体，失败回退系统常见中文字体。

        指导 §5.2 / §8.6：保留既有字体链与打包字体，不引入 emoji 字体。
        """
        key = (size, bold)
        if key in cls._cached_fonts:
            return cls._cached_fonts[key]

        candidates = []
        # F2（2026-10-11）：bold 优先用打包的真粗体字重（NotoSansSC-Bold，OFL 随包分发）；
        # 缺失时回退 Regular（bold 降级为同字重，不再依赖描边模拟）
        if bold and cls.BUNDLED_FONT_BOLD_PATH.exists():
            candidates.append(str(cls.BUNDLED_FONT_BOLD_PATH))
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

        bundled = {str(cls.BUNDLED_FONT_PATH), str(cls.BUNDLED_FONT_BOLD_PATH)}
        for path in candidates:
            try:
                font = ImageFont.truetype(path, size)
                # 验证中英数字都能渲染
                bbox = font.getbbox("日程09:30")
                if bbox[2] - bbox[0] > 0:
                    cls._cached_fonts[key] = font
                    if path not in bundled:
                        logger.info(f"打包字体不可用，回退系统字体: {path} (size={size})")
                    return font
            except Exception:
                continue

        raise RuntimeError("未找到可用的中文字体（含打包字体 planning/assets/fonts/）")

    @classmethod
    def _font_loader(cls, ImageFont: Any):
        """返回 ``loader(size, bold)`` 闭包，供 :class:`core.render_kit.Canvas` 使用。"""
        def _load(size: int, bold: bool = False):
            return cls._get_font(ImageFont, size, bold)
        return _load

    # ------------------------------------------------------------
    # 时间与状态（指导 §3.1 明确保留：解析/判定本身正确）
    # ------------------------------------------------------------

    @staticmethod
    def _parse_time_str(time_str: str) -> Tuple[int, int]:
        """解析 ``HH:MM-HH:MM``，返回 (开始分钟, 结束分钟)。

        兼容三种跨午夜写法：累计分钟（``23:00-31:00``）、回绕写法（``23:00-07:00``）与
        :func:`time_utils.format_day_minute` 前缀（``23:00-次日 07:00``，读取侧统一渲染后的
        现行形态）。解析失败返回 (0, 0)。
        """
        try:
            parts = str(time_str).split("-")
            if len(parts) != 2:
                return (0, 0)

            def _to_minutes(seg: str) -> Optional[int]:
                text = seg.strip()
                offset = 0
                if text.startswith("次日"):        # format_day_minute 的跨午夜前缀：1440+钟点
                    offset, text = 24 * 60, text[2:].strip()
                elif text.startswith("前日"):      # 防御性（正常链路不产生负分钟条目）
                    offset, text = -24 * 60, text[2:].strip()
                parts = text.split(":")[0:2]
                if len(parts) != 2:
                    return None
                hour, minute = int(parts[0]), int(parts[1])
                if minute < 0 or minute > 59 or hour < 0:
                    return None
                return hour * 60 + minute + offset

            start = _to_minutes(parts[0])
            end = _to_minutes(parts[1])
            if start is None or end is None:
                return (0, 0)
            if 0 < end <= start:
                end += 24 * 60  # 回绕写法的跨午夜活动（带「次日」前缀时已≥start，不会进这分支）
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

    @staticmethod
    def _clock_text(minutes: int) -> str:
        """日帧分钟 → ``HH:MM`` 钟点（**派生自** ``format_day_minute``，不手写 //60/−1440）。"""
        return format_day_minute(minutes).split(" ", 1)[-1]

    @classmethod
    def _time_display(cls, time_str: str) -> Tuple[str, bool]:
        """时间列展示：``(主行 HH:MM–HH:MM, 是否跨午夜)``（§6.6 两行制）。"""
        start, end = cls._parse_time_str(time_str)
        if end == 0 and start == 0:
            raw = str(time_str or "")
            return raw, raw.startswith("次日") or raw.startswith("前日")
        main = f"{cls._clock_text(start)}–{cls._clock_text(end)}"
        next_day = start >= DAY_MINUTES or end > DAY_MINUTES
        return main, next_day

    # ------------------------------------------------------------
    # 日程属性标注（§24.3 任务C 2026-10-21：条目行属性标签 + 属性色）
    # ------------------------------------------------------------

    @staticmethod
    def _property_tag(item: Dict[str, Any]) -> str:
        """条目行的 ``[属性]`` 短标签（调用方注入 ``property`` 文案；缺失/空白 → 不画）。

        长度纪律（_cap 同思路的定长截断）：标签预截 4 字 — 属性表校验推荐 2~3 字，4 字为
        排版硬上限，防用户超长标签挤爆列位。
        """
        label = str(item.get("property") or "").strip()
        if not label:
            return ""
        return f"[{label[:4]}]"

    @staticmethod
    def _parse_hex_color(value: Any) -> Optional[Tuple[int, int, int]]:
        """``#RRGGBB`` 十六进制色 → RGB 三元组（非法形态 → ``None`` 交给回退链）。"""
        text = str(value or "").strip()
        if len(text) != 7 or text[0] != "#":
            return None
        try:
            return (int(text[1:3], 16), int(text[3:5], 16), int(text[5:7], 16))
        except ValueError:
            return None

    @classmethod
    def _row_item_color(cls, item: Dict[str, Any], goal_type: str) -> Tuple[int, int, int]:
        """条目色（类型色条/密度条）：属性 color → ``TYPE_COLORS[goal_type]`` → 兜底色。

        §24.3 冻结回退链：注入的 ``property_color`` 非法（非 ``#RRGGBB`` 形态）/ 缺失 →
        回退 ``TYPE_COLORS[goal_type]``；未收录 goal_type → ``DEFAULT_TYPE_COLOR``。
        """
        parsed = cls._parse_hex_color(item.get("property_color"))
        if parsed is not None:
            return parsed
        return cls.TYPE_COLORS.get(goal_type, cls.DEFAULT_TYPE_COLOR)

    @classmethod
    def _type_label(cls, item: Dict[str, Any]) -> str:
        """类型列标签：属性短标签优先（去方括号），缺失回退 goal_type 中文名。"""
        tag = cls._property_tag(item)
        if tag:
            return tag[1:-1]
        goal_type = str(item.get("goal_type", "custom") or "custom")
        return cls.TYPE_LABELS.get(goal_type, "其他")

    # ------------------------------------------------------------
    # 视图模型与分页
    # ------------------------------------------------------------

    @classmethod
    def _build_view_model(cls, schedule_items: List[Dict[str, Any]],
                          now_minutes: int) -> List[Dict[str, Any]]:
        """条目 → 行视图（状态/颜色/时间两行制），不依赖 draw（先算后画）。"""
        rows: List[Dict[str, Any]] = []
        for item in schedule_items or []:
            if not isinstance(item, dict):
                continue
            time_str = str(item.get("time", ""))
            goal_type = str(item.get("goal_type", "custom") or "custom")
            time_main, next_day = cls._time_display(time_str)
            rows.append({
                "raw_time": time_str,      # 密度条复用（解析口径与展示同一实现）
                "time_main": time_main,
                "next_day": next_day,
                "name": str(item.get("name", "") or ""),
                "desc": str(item.get("description", "") or ""),
                "status": cls._activity_status(time_str, now_minutes),
                "label": cls._type_label(item),
                "color": cls._row_item_color(item, goal_type),
            })
        return rows

    @classmethod
    def _plan_pages(cls, total: int) -> List[Dict[str, Any]]:
        """分页计划（§6.10）：1~24 单页；25~40 每页 24；>40 当日窗口前 24 + 文字版出口。"""
        if total <= 0:
            return [{"start": 0, "end": 0, "page_no": 1, "page_count": 1,
                     "omitted": 0, "total": 0}]
        if total <= cls.PAGE_SIZE:
            return [{"start": 0, "end": total, "page_no": 1, "page_count": 1,
                     "omitted": 0, "total": total}]
        if total <= 40:
            pages = []
            slices = [(s, min(s + cls.PAGE_SIZE, total))
                      for s in range(0, total, cls.PAGE_SIZE)]
            for idx, (start, end) in enumerate(slices, 1):
                pages.append({"start": start, "end": end, "page_no": idx,
                              "page_count": len(slices), "omitted": 0, "total": total})
            return pages
        # >40：只画当日窗口（前 24 条），其余交给文字版（绝不静默截断）
        return [{"start": 0, "end": cls.PAGE_SIZE, "page_no": 1, "page_count": 1,
                 "omitted": total - cls.PAGE_SIZE, "total": total}]

    # ------------------------------------------------------------
    # 测量（第一遍）
    # ------------------------------------------------------------

    @classmethod
    def _note_lines(cls, canvas: Canvas, font: Any, page: Dict[str, Any]) -> List[str]:
        """脚注口径行；>40 时追加「其余见文字版」出口（§6.8 / §6.10）。"""
        lines = [cls.NOTE_TEXT]
        if page.get("omitted"):
            lines.append(
                f"当日窗口仅示前 {page['end'] - page['start']} 项（共 {page['total']} 项），"
                f"其余见文字版。")
        return [render_kit.fit_text(canvas, line, font, cls.CONTENT_W) for line in lines]

    @classmethod
    def _layout_commands(cls, canvas: Canvas, f_label: Any, f_cmd: Any) -> List[Dict[str, Any]]:
        """命令块布局（唯一一次）：标签 + 三段命令，超宽在 ``·`` 前换行（不缩字号）。"""
        items: List[Dict[str, Any]] = [{"line": 0, "x": cls.MARGIN_X, "text": "命令",
                                        "kind": "label"}]
        label_w = render_kit.text_width(canvas, "命令", f_label)
        sep = " · "
        sep_w = render_kit.text_width(canvas, sep, f_cmd)
        x = cls.MARGIN_X + label_w + 10
        line = 0
        for idx, seg in enumerate(cls.COMMAND_SEGMENTS):
            seg_w = render_kit.text_width(canvas, seg, f_cmd)
            if idx and x + sep_w + seg_w > cls.TEXT_RIGHT:
                line += 1
                x = cls.MARGIN_X
            if idx and x > cls.MARGIN_X:
                items.append({"line": line, "x": x, "text": sep, "kind": "sep"})
                x += sep_w
            items.append({"line": line, "x": x, "text": seg, "kind": "cmd"})
            x += seg_w
        return items

    @classmethod
    def _measure_page(cls, Image: Any, ImageDraw: Any, ImageFont: Any,
                      rows: List[Dict[str, Any]], page: Dict[str, Any]) -> Dict[str, Any]:
        """测量遍：脚注/命令行数 + 行区高度 → 页面总高（不画）。"""
        probe = Canvas(Image, ImageDraw, cls.DEFAULT_WIDTH, 1, scale=1)
        loader = cls._font_loader(ImageFont)
        f_note = probe.font(loader, 16)
        f_label = probe.font(loader, 15, bold=True)
        f_cmd = probe.font(loader, 15)

        note_lines = cls._note_lines(probe, f_note, page)
        cmd_items = cls._layout_commands(probe, f_label, f_cmd)
        cmd_lines = max((it["line"] for it in cmd_items), default=0) + 1

        rows_h = cls.EMPTY_BLOCK_H if not rows else len(rows) * cls.ROW_H
        notes_top = cls.ROWS_TOP + rows_h + cls.NOTES_MARGIN_TOP
        notes_h = (cls.NOTES_PAD_TOP + len(note_lines) * cls.NOTE_LINE_H
                   + cls.COMMANDS_GAP + cmd_lines * cls.CMD_LINE_H)
        footer_top = notes_top + notes_h + cls.FOOTER_MARGIN_TOP
        height = footer_top + cls.FOOTER_H
        # 4096 红线：正常分页已满足；钳制仅作病态输入的最后防线（不裁正常内容）
        if height > cls.MAX_HEIGHT:
            logger.warning("日程图测量高度 %s 超过 4096，触发最后防线钳制", height)
            height = cls.MAX_HEIGHT
        return {
            "height": int(height),
            "rows_h": int(rows_h),
            "notes_top": int(notes_top),
            "footer_top": int(footer_top),
            "note_lines": note_lines,
            "cmd_items": cmd_items,
        }

    # ------------------------------------------------------------
    # 绘制（第二遍）
    # ------------------------------------------------------------

    @classmethod
    def _draw_page_header(cls, canvas: Canvas, loader: Any) -> None:
        """页眉：红点 + MAI · STATE / MAI · SCHEDULE（19/700，拉丁 tracking 6）+ 2px ink。"""
        f_brand = canvas.font(loader, 19, bold=True)
        canvas.ellipse([cls.MARGIN_X, 37, cls.MARGIN_X + 14, 51], fill=RED)
        baseline = 55
        render_kit.draw_tracked(canvas, (cls.MARGIN_X + 28, baseline), "MAI · STATE",
                                f_brand, INK, 6.0)
        render_kit.draw_tracked(canvas, (cls.TEXT_RIGHT, baseline), "MAI · SCHEDULE",
                                f_brand, INK, 6.0, anchor="rs")
        canvas.line([(cls.MARGIN_X, cls.HEADER_H - 1),
                     (cls.TEXT_RIGHT, cls.HEADER_H - 1)], INK, width=2)

    @classmethod
    def _draw_hero(cls, canvas: Canvas, loader: Any, giant: str, weekday: str,
                   now_text: str, counts_text: str) -> None:
        """主视觉：日期 96/700 + 星期 40/700 red 同基线 + 现在/统计右对齐实测。"""
        f_date = canvas.font(loader, 96, bold=True)
        f_wd = canvas.font(loader, 40, bold=True)
        f_now = canvas.font(loader, 22, bold=True)
        f_counts = canvas.font(loader, 17)

        hero_top = cls.HEADER_H
        date_baseline = hero_top + 121
        render_kit.draw_tracked(canvas, (cls.MARGIN_X, date_baseline), giant,
                                f_date, INK, -3.0)
        if weekday:
            date_w = render_kit.tracked_width(canvas, giant, f_date, -3.0)
            render_kit.draw_tracked(canvas, (cls.MARGIN_X + date_w + 28, date_baseline),
                                    weekday, f_wd, RED, 0.0)

        counts_text = render_kit.fit_text(canvas, counts_text, f_counts, 560)
        now_text = render_kit.fit_text(canvas, now_text, f_now, 300)
        counts_baseline = hero_top + cls.HERO_H - 8
        render_kit.draw_text(canvas, (cls.TEXT_RIGHT, counts_baseline), counts_text,
                             f_counts, MUTED, anchor="rs")
        render_kit.draw_text(canvas, (cls.TEXT_RIGHT, counts_baseline - 30), now_text,
                             f_now, INK, anchor="rs")

    @classmethod
    def _draw_ribbon(cls, canvas: Canvas, rows: List[Dict[str, Any]],
                     now_minutes: int) -> None:
        """24h 密度条（§6.4）：6px track + 属性色裁段 + 1px 间隔 + 当前 2×22 红 tick。"""
        top = cls.HEADER_H + cls.HERO_H + cls.RIBBON_MARGIN_TOP + 11
        canvas.rectangle([cls.MARGIN_X, top, cls.MARGIN_X + cls.CONTENT_W, top + 6],
                         fill=TRACK)
        for row in rows:
            start, end = cls._parse_time_str(row.get("raw_time", ""))
            if end <= 0:
                continue
            seg_start = max(0, min(DAY_MINUTES, start))
            seg_end = max(0, min(DAY_MINUTES, end))
            if seg_end <= seg_start:
                continue
            x0 = cls.MARGIN_X + int(round(seg_start / DAY_MINUTES * cls.CONTENT_W))
            x1 = cls.MARGIN_X + int(round(seg_end / DAY_MINUTES * cls.CONTENT_W))
            x1 = min(cls.MARGIN_X + cls.CONTENT_W, x1)
            canvas.rectangle([x0, top, max(x0 + 1, x1 - 1), top + 6], fill=row["color"])
        tick_x = cls.MARGIN_X + int(round(
            max(0, min(DAY_MINUTES, now_minutes)) / DAY_MINUTES * cls.CONTENT_W))
        tick_top = cls.HEADER_H + cls.HERO_H + cls.RIBBON_MARGIN_TOP + 2
        canvas.rectangle([tick_x, tick_top, tick_x + 2, tick_top + 22], fill=RED)

    @classmethod
    def _draw_colhead(cls, canvas: Canvas, loader: Any) -> None:
        """列头（§6.5）：12/400 faint tracking 1.5 + 1px ink 下划线。"""
        f_col = canvas.font(loader, 12)
        top = cls.ROWS_TOP - cls.COLHEAD_H
        baseline = top + 18
        render_kit.draw_tracked(canvas, (cls.ABS_TIME, baseline), "时间", f_col, FAINT, 1.5)
        render_kit.draw_tracked(canvas, (cls.ABS_CAT + 20, baseline), "类型", f_col, FAINT, 1.5)
        render_kit.draw_tracked(canvas, (cls.ABS_NAME, baseline), "安排", f_col, FAINT, 1.5)
        render_kit.draw_tracked(canvas, (cls.TEXT_RIGHT, baseline), "状态", f_col, FAINT,
                                1.5, anchor="rs")
        canvas.line([(cls.MARGIN_X, cls.ROWS_TOP - 1),
                     (cls.TEXT_RIGHT, cls.ROWS_TOP - 1)], INK, width=1)

    @classmethod
    def _draw_rows(cls, canvas: Canvas, loader: Any, rows: List[Dict[str, Any]],
                   start_index: int) -> None:
        """行区（§6.6/§6.7）：固定列 + 三种状态 + 两行制时间。"""
        f_idx = canvas.font(loader, 19, bold=True)
        f_time = canvas.font(loader, 23)
        f_time_cur = canvas.font(loader, 23, bold=True)
        f_next = canvas.font(loader, 12)
        f_cat = canvas.font(loader, 17)
        f_name = canvas.font(loader, 26, bold=True)
        f_desc = canvas.font(loader, 17)
        f_status = canvas.font(loader, 17)
        f_status_cur = canvas.font(loader, 17, bold=True)

        if not rows:
            f_empty = canvas.font(loader, 20)
            render_kit.draw_text(canvas, (cls.MARGIN_X, cls.ROWS_TOP + 38),
                                 "今天暂无日程", f_empty, MUTED)
            return

        for offset, row in enumerate(rows):
            index = start_index + offset + 1
            top = cls.ROWS_TOP + offset * cls.ROW_H
            row_base = top + cls.ROW_PAD_TOP + canvas.ascent(f_name)
            status = row["status"]
            is_current = status == "current"
            is_done = status == "completed"

            # 行底：进行中 tint 出血 48..1032 + 4px 红左边线
            if is_current:
                canvas.rectangle([cls.ROW_BLEED_X0, top, cls.ROW_BLEED_X1, top + cls.ROW_H],
                                 fill=TINT)
                canvas.rectangle([cls.ROW_BLEED_X0, top, cls.ROW_BLEED_X0 + 4,
                                  top + cls.ROW_H], fill=RED)

            # 序号
            idx_color = (199, 164, 157) if is_done else RED
            render_kit.draw_text(canvas, (cls.ABS_IDX, row_base), f"{index:02d}",
                                 f_idx, idx_color)

            # 时间（主行 + 次日第二行）
            time_text = render_kit.fit_text(canvas, row["time_main"],
                                            f_time_cur if is_current else f_time,
                                            cls.COLS["time"][1])
            time_color = FAINT if is_done else (RED if is_current else MUTED)
            render_kit.draw_text(canvas, (cls.ABS_TIME, row_base), time_text,
                                 f_time_cur if is_current else f_time, time_color)
            if row.get("next_day"):
                render_kit.draw_text(canvas, (cls.ABS_TIME, row_base + cls.TIME_NEXT_OFFSET),
                                     "次日", f_next, FAINT)

            # 类型：色条固定 x=300 + 标签 313
            bar_color = render_kit.blend(row["color"], PAPER, 0.4) if is_done else row["color"]
            canvas.rounded_rectangle([cls.BAR_X, row_base - 21, cls.BAR_X + 3,
                                      row_base + 1], radius=2, fill=bar_color)
            cat_text = render_kit.fit_text(canvas, row["label"], f_cat,
                                           cls.COLS["cat"][1] - 20 - 3 - 10)
            cat_color = FAINT if is_done else (INK if is_current else MUTED)
            render_kit.draw_text(canvas, (cls.CAT_TEXT_X, row_base), cat_text, f_cat, cat_color)

            # 名称 / 描述（均 1 行省略；描述基线 = 名称基线 + round(17*1.35)）
            name_text = render_kit.fit_text(canvas, row["name"], f_name, cls.NAME_MAX_W)
            name_color = FAINT if is_done else INK
            render_kit.draw_text(canvas, (cls.ABS_NAME, row_base), name_text, f_name, name_color)
            if row["desc"]:
                desc_text = render_kit.fit_text(canvas, row["desc"], f_desc, cls.NAME_MAX_W)
                desc_color = FAINT if is_done else (INK_SOFT if is_current else MUTED)
                render_kit.draw_text(canvas, (cls.ABS_NAME,
                                              row_base + cls.DESC_BASELINE_OFFSET),
                                     desc_text, f_desc, desc_color)

            # 状态：圆点 + 中文词，右缘 1016
            status_text = {"completed": "已完成", "current": "进行中",
                           "upcoming": "未开始"}[status]
            f_st = f_status_cur if is_current else f_status
            st_color = FAINT if is_done else (RED if is_current else MUTED)
            render_kit.draw_text(canvas, (cls.ABS_STATUS_RIGHT, row_base), status_text,
                                 f_st, st_color, anchor="rs")
            st_w = render_kit.text_width(canvas, status_text, f_st)
            dot_cx = cls.ABS_STATUS_RIGHT - st_w - 7 - 3
            dot_cy = row_base - 7
            if is_current:
                canvas.ellipse([dot_cx - 3, dot_cy - 3, dot_cx + 3, dot_cy + 3], fill=RED)
            elif is_done:
                canvas.ellipse([dot_cx - 3, dot_cy - 3, dot_cx + 3, dot_cy + 3],
                               fill=DONE_DOT)
            else:
                canvas.ellipse([dot_cx - 3, dot_cy - 3, dot_cx + 3, dot_cy + 3],
                               outline=RING_DOT, width=1)

            # 行分隔线（1px rowline，文字区内 64..1016）
            canvas.line([(cls.MARGIN_X, top + cls.ROW_H - 1),
                         (cls.TEXT_RIGHT, top + cls.ROW_H - 1)], ROWLINE, width=1)

    @classmethod
    def _draw_notes(cls, canvas: Canvas, loader: Any, layout: Dict[str, Any]) -> None:
        """脚注（§6.8）：1px hair + 口径行 + 命令唯一一次。"""
        notes_top = layout["notes_top"]
        canvas.line([(cls.MARGIN_X, notes_top), (cls.TEXT_RIGHT, notes_top)],
                    HAIR, width=1)
        f_note = canvas.font(loader, 16)
        f_label = canvas.font(loader, 15, bold=True)
        f_cmd = canvas.font(loader, 15)

        baseline = notes_top + cls.NOTES_PAD_TOP + canvas.ascent(f_note)
        for line in layout["note_lines"]:
            render_kit.draw_text(canvas, (cls.MARGIN_X, baseline), line, f_note, MUTED)
            baseline += cls.NOTE_LINE_H

        cmd_baseline = baseline + cls.COMMANDS_GAP + canvas.ascent(f_cmd)
        for item in layout["cmd_items"]:
            y = cmd_baseline + item["line"] * cls.CMD_LINE_H
            if item["kind"] == "label":
                render_kit.draw_text(canvas, (item["x"], y), item["text"], f_label, RED_DEEP)
            elif item["kind"] == "sep":
                render_kit.draw_text(canvas, (item["x"], y), item["text"], f_cmd, FAINT)
            else:
                render_kit.draw_text(canvas, (item["x"], y), item["text"], f_cmd, INK_SOFT)

    @staticmethod
    def _footer_left_text(page: Dict[str, Any]) -> str:
        """页脚左侧文本（§6.9）：版本 + 多页 ``1/2`` 页码 / 超限「其余见文字版」。"""
        text = "属于麦麦的生活 v2.1.0"
        if page.get("page_count", 1) > 1:
            text += f" · {page['page_no']}/{page['page_count']}"
        if page.get("omitted"):
            text += " · 其余见文字版"
        return text

    @classmethod
    def _draw_footer(cls, canvas: Canvas, loader: Any, layout: Dict[str, Any],
                     page: Dict[str, Any]) -> None:
        """页脚（§6.9）：2px ink + 左版本（含页码）/ 右签名，14/400 muted，中文不 tracking。"""
        f_foot = canvas.font(loader, 14)
        footer_top = layout["footer_top"]
        canvas.line([(cls.MARGIN_X, footer_top), (cls.TEXT_RIGHT, footer_top)], INK, width=2)
        baseline = footer_top + 2 + 14 + canvas.ascent(f_foot)
        left = cls._footer_left_text(page)
        render_kit.draw_text(canvas, (cls.MARGIN_X, baseline), left, f_foot, MUTED)
        render_kit.draw_text(canvas, (cls.TEXT_RIGHT, baseline), "Powered by Mai-Bot",
                             f_foot, MUTED, anchor="rs")

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
        """生成日程图片（固定 1080 列网格，展示当日条目；多页时首文件契约不变）。

        Args:
            title: 标题文字（如 "今日日程 2026-09-19 周六"）
            schedule_items: 日程项列表，每项
                ``{"time": "HH:MM-HH:MM", "name", "description", "goal_type"}``
                （另可选 ``property`` / ``property_color`` 属性标注字段）
            width: 兼容旧签名；版式固定 1080（指导 §5.3），传入其他值会记录日志后忽略
            tz_name: 用于判定「进行中/已完成」的时区（IANA 名；None=默认）
            output_dir: 图片输出目录（宿主隔离数据目录；None=系统临时目录）

        Returns:
            (第一页图片路径, 第一页 base64 编码字符串)；多页时附页为
            ``schedule_today_p2.jpg`` 等（R6 文件名契约不变）。

        Raises:
            RuntimeError: pillow 未安装、找不到可用字体或渲染失败（调用方静默降级为文本）。
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
        """编排：视图模型 → 分页 → 每页测量/绘制 → JPEG 落盘（quality=95, 4:4:4）。"""
        Image, ImageDraw, ImageFont = cls._require_pil()

        if width and int(width) != cls.DEFAULT_WIDTH:
            logger.info("日程图版式固定 %s 宽（指导 §5.3），忽略 width=%s",
                        cls.DEFAULT_WIDTH, width)
        canvas_width = cls.DEFAULT_WIDTH

        try:
            try:
                now = TimezoneManager(tz_name).get_now() if tz_name else datetime.now()
            except Exception:
                now = datetime.now()
            now_minutes = now.hour * 60 + now.minute

            rows = cls._build_view_model(schedule_items, now_minutes)
            total = len(rows)
            done = sum(1 for r in rows if r["status"] == "completed")
            current = sum(1 for r in rows if r["status"] == "current")
            upcoming = total - done - current

            giant, weekday = cls._hero_identity(title)
            now_text = f"现在 {now.hour:02d}:{now.minute:02d}"
            if total:
                counts_text = (f"{total} 项 · 完成 {done} · 进行中 {current} · "
                               f"待开始 {upcoming}")
            else:
                counts_text = "0 项"

            pages = cls._plan_pages(total)
            save_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
            save_dir.mkdir(parents=True, exist_ok=True)
            loader = cls._font_loader(ImageFont)
            first_path: Optional[Path] = None
            first_b64 = ""

            for page in pages:
                page_rows = rows[page["start"]:page["end"]]
                layout = cls._measure_page(Image, ImageDraw, ImageFont, page_rows, page)
                scale = render_kit.supersample_scale(layout["height"])
                canvas = Canvas(Image, ImageDraw, canvas_width, layout["height"],
                                scale=scale, background=PAPER)
                cls._draw_page_header(canvas, loader)
                cls._draw_hero(canvas, loader, giant, weekday, now_text, counts_text)
                cls._draw_ribbon(canvas, page_rows, now_minutes)
                cls._draw_colhead(canvas, loader)
                cls._draw_rows(canvas, loader, page_rows, page["start"])
                cls._draw_notes(canvas, loader, layout)
                cls._draw_footer(canvas, loader, layout, page)
                img = canvas.finish()

                if page["page_no"] == 1:
                    image_path = save_dir / "schedule_today.jpg"
                else:
                    image_path = save_dir / f"schedule_today_p{page['page_no']}.jpg"
                img.save(str(image_path), format="JPEG", quality=95,
                         subsampling=0, optimize=True)
                if first_path is None:
                    first_path = image_path
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=95,
                             subsampling=0, optimize=True)
                    first_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

            assert first_path is not None
            return str(first_path), first_b64
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"日程图片渲染失败: {exc}") from exc

    @staticmethod
    def _hero_identity(title: str) -> Tuple[str, str]:
        """标题 → (巨型日期 ``MM.DD``, 星期单字)；无日期 → ("今日", "")。"""
        match = re.search(r"(\d{4})-(\d{2})-(\d{2})", str(title or ""))
        if not match:
            return "今日", ""
        giant = f"{match.group(2)}.{match.group(3)}"
        try:
            weekday = "一二三四五六日"[datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3))).weekday()]
        except ValueError:
            weekday = ""
        return giant, weekday
