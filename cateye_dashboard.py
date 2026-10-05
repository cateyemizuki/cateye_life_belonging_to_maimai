"""聚合状态总览（M7）：四模块状态块 → 展示行 → 纯文本 / 状态图。

设计要点：
- **纯函数模块**（不 import SDK、不持状态）：四类状态块按「形状」从各服务对象取数，
  任一对象缺失或取数抛错一律回退安全值（0 / 空列表 / None），绝不外抛——总览的语义是
  「一个模块坏了（或没启用）不拖垮整张图」，对应区块显示「（未启用）」。
- ``block_lines`` 是文本与图片的唯一展示来源：``render_dashboard_text`` 与
  ``render_dashboard_image`` 都只消费它，保证图片四区块数值与文本降级输出完全一致。
- PIL 惰性导入（与 planning/schedule_image.py 同口径）：pillow 未安装或找不到可用中文字体
  时抛 RuntimeError，由调用方静默降级为纯文本。
"""

from __future__ import annotations

import base64
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: 区块标题（键 → 固定标题；顺序即展示顺序）
_BLOCK_TITLES: Tuple[Tuple[str, str], ...] = (
    ("planning", "📅 日程规划"),
    ("companion", "❤️ 伴侣关系"),
    ("emotion", "🎭 情绪"),
    ("qzone", "🌐 QQ空间"),
)

#: 未启用区块的唯一一行
_DISABLED_LINE = "（未启用）"

# ---------------------------------------------------------------------------
# 取数助手（全部防御式：缺失/异常/None → 安全值）
# ---------------------------------------------------------------------------


def _value(obj: Any, name: str, default: Any) -> Any:
    """取 ``obj.name``（可调用则调用）；对象缺失/属性缺失/调用抛错/结果为 None → default。"""
    if obj is None:
        return default
    attr = getattr(obj, name, None)
    if attr is None:
        return default
    try:
        result = attr() if callable(attr) else attr
    except Exception:
        return default
    return default if result is None else result


def _call(obj: Any, name: str, *args: Any, default: Any = None) -> Any:
    """取 ``obj.name(*args)`` 的结果；对象/属性缺失或调用抛错 → default。"""
    if obj is None:
        return default
    attr = getattr(obj, name, None)
    if not callable(attr):
        return default
    try:
        result = attr(*args)
    except Exception:
        return default
    return default if result is None else result


def _mapping(obj: Any, name: str) -> Dict[str, Any]:
    """取 ``obj.name()`` 的 dict 结果；非 dict/异常一律空 dict。"""
    result = _value(obj, name, None)
    return result if isinstance(result, dict) else {}


def _rows(obj: Any, name: str, *args: Any) -> List[Any]:
    """取 ``obj.name(*args)`` 的列表结果；非列表/异常一律空列表。"""
    result = _call(obj, name, *args, default=[])
    return list(result) if isinstance(result, (list, tuple)) else []


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _time_range(item: Any) -> str:
    """条目 → ``HH:MM-HH:MM``（分钟字段缺失/异常按 00:00 兜底）。"""
    start = _to_int(item.get("start_minute"), 0) if isinstance(item, dict) else 0
    end = _to_int(item.get("end_minute"), 0) if isinstance(item, dict) else 0
    return f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"


def _tier_label(companion: Any, affection: Any, tier: int) -> str:
    """档位名：关系档案（relation.tier_label(tier)）优先，缺失时回退好感管理器的基底标签。"""
    relation = getattr(companion, "relation", None) if companion is not None else None
    getter = getattr(relation, "tier_label", None) if relation is not None else None
    if callable(getter):
        try:
            return str(getter(tier))
        except Exception:
            pass
    return str(_value(affection, "tier_label", ""))


# ---------------------------------------------------------------------------
# 状态块（供命令层组装；enabled=False 表示该模块未启用/未装配）
# ---------------------------------------------------------------------------


def planning_block(schedule_query: Any, goal_manager: Any, *, enabled: bool,
                   now: Optional[datetime] = None) -> Dict[str, Any]:
    """日程块：今日条目（≤5）/ 当前活动 / 今日约定（≤3）。"""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    block: Dict[str, Any] = {
        "enabled": False, "date": date, "item_count": 0, "items": [],
        "current": None, "commitment_count": 0, "commitments": [],
    }
    if not enabled:
        return block
    block["enabled"] = True

    items = _rows(schedule_query, "get_day_schedule", date)
    block["item_count"] = len(items)
    block["items"] = [
        {"time": _time_range(item), "name": str(item.get("name") or "")}
        for item in items[:5] if isinstance(item, dict)
    ]

    current = _call(schedule_query, "find_current_activity", date, now)
    if isinstance(current, dict):
        name = str(current.get("name") or "")
        block["current"] = f"{_time_range(current)} {name}".strip()

    commitments = _rows(goal_manager, "get_pending_commitments", date)
    block["commitment_count"] = len(commitments)
    block["commitments"] = [
        str(getattr(goal, "name", "") or "") for goal in commitments[:3]
        if str(getattr(goal, "name", "") or "").strip()
    ]
    return block


def companion_block(companion: Any, config: Any, *, enabled: bool,
                    now: Optional[datetime] = None) -> Dict[str, Any]:
    """伴侣块：好感（score/baseline/档位/今日Δ）/ 今日发言与预算 / 早晚安想念 / 印象与温度 / 巡检。"""
    block: Dict[str, Any] = {
        "enabled": False, "score": 0.0, "baseline": 0.0, "tier": 0, "tier_label": "",
        "speak_count": 0.0, "daily_max": 0, "daily_net": 0.0,
        "morning_sent": False, "night_sent": False, "miss_sent": False,
        "impressions": [], "tone": None,
        "patrol": {"running": False, "last_tick_at": "", "interval_minutes": 0,
                   "stream_id_ready": False, "trigger_enabled": False},
    }
    affection = getattr(companion, "affection", None) if companion is not None else None
    if not enabled or affection is None:
        return block
    block["enabled"] = True

    block["score"] = _to_float(_value(affection, "score", 0.0))
    block["baseline"] = _to_float(_value(affection, "baseline", 0.0))
    block["tier"] = _to_int(_value(affection, "tier", 0))
    block["tier_label"] = _tier_label(companion, affection, block["tier"])
    block["speak_count"] = _to_float(_value(affection, "today_speak_count", 0.0))
    block["daily_max"] = _to_int(getattr(getattr(config, "patrol", None), "daily_max_speak", 0))
    daily = _mapping(affection, "daily_delta")
    # 今日 Δ = 正负两路相抵后的净值（与 /mai_affection 的展示口径一致）
    block["daily_net"] = _to_float(daily.get("positive")) + _to_float(daily.get("negative"))
    block["morning_sent"] = bool(_value(affection, "morning_sent", False))
    block["night_sent"] = bool(_value(affection, "night_sent", False))
    block["miss_sent"] = bool(_value(affection, "miss_sent_today", False))

    impressions = []
    for item in _rows(affection, "impressions")[-2:]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if text:
            impressions.append(f"{str(item.get('date') or '').strip()} {text}".strip())
    block["impressions"] = impressions
    tone = _mapping(affection, "tone")
    if str(tone.get("text") or "").strip():
        block["tone"] = {"date": str(tone.get("date") or "").strip(),
                         "text": str(tone.get("text")).strip()}

    status = _mapping(getattr(companion, "patrol", None), "patrol_status")
    block["patrol"] = {
        "running": bool(status.get("running")),
        "last_tick_at": str(status.get("last_tick_at") or ""),
        "interval_minutes": _to_int(status.get("interval_minutes")),
        "stream_id_ready": bool(status.get("stream_id_ready")),
        "trigger_enabled": bool(status.get("trigger_enabled")),
    }
    return block


def emotion_block(emotion_svc: Any, config: Any, *, enabled: bool,
                  now: Optional[datetime] = None) -> Dict[str, Any]:
    """情绪块：取 ``emotion_svc.service.status_snapshot()``（EmotionServices 束形状）。"""
    section = getattr(config, "emotion", None)
    block: Dict[str, Any] = {
        "enabled": False, "module_enabled": bool(getattr(section, "enabled", True)),
        "initialized": False, "mood_text": "", "labels": [], "event_count": 0,
        "anchor": {}, "offset": {}, "frozen": False,
    }
    service = getattr(emotion_svc, "service", None) if emotion_svc is not None else None
    if not enabled or service is None:
        return block
    snapshot = _mapping(service, "status_snapshot")
    block.update({
        "enabled": True,
        "initialized": bool(snapshot.get("initialized")),
        "mood_text": str(snapshot.get("mood_text") or ""),
        "labels": [str(name) for name in (snapshot.get("labels") or [])],
        "event_count": _to_int(snapshot.get("event_count")),
        "anchor": snapshot.get("anchor") if isinstance(snapshot.get("anchor"), dict) else {},
        "offset": snapshot.get("offset") if isinstance(snapshot.get("offset"), dict) else {},
        "frozen": bool(snapshot.get("frozen")),
    })
    return block


def qzone_block(qzone: Any, config: Any, *, enabled: bool,
                now: Optional[datetime] = None) -> Dict[str, Any]:
    """空间块：取 ``qzone.status_snapshot()``（QzoneService 形状）。"""
    block: Dict[str, Any] = {
        "enabled": False, "posts_today": 0, "daily_max_posts": 0,
        "reply_comments_enabled": False, "channels": [], "cookie_text": "",
        "comment_baseline": [],
    }
    if not enabled or qzone is None:
        return block
    snapshot = _mapping(qzone, "status_snapshot")
    block.update({
        "enabled": True,
        "posts_today": _to_int(snapshot.get("posts_today")),
        "daily_max_posts": _to_int(snapshot.get("daily_max_posts")),
        "reply_comments_enabled": bool(snapshot.get("reply_comments_enabled")),
        "channels": [str(c) for c in (snapshot.get("channels") or [])],
        "cookie_text": str(snapshot.get("cookie_text") or ""),
        "comment_baseline": list(snapshot.get("comment_baseline") or []),
    })
    return block


# ---------------------------------------------------------------------------
# 展示行（文本与图片共用的唯一来源）
# ---------------------------------------------------------------------------


def _vad_text(state: Dict[str, Any]) -> str:
    return (f"V={_to_float(state.get('valence')):+.2f} "
            f"A={_to_float(state.get('arousal')):+.2f} "
            f"D={_to_float(state.get('dominance')):+.2f}")


def _planning_lines(block: Dict[str, Any]) -> List[str]:
    lines = [f"今日日程 {_to_int(block.get('item_count'))} 项"]
    for item in block.get("items") or []:
        if isinstance(item, dict):
            lines.append(f"{str(item.get('time') or '')} {str(item.get('name') or '')}".strip())
    current = block.get("current")
    lines.append(f"当前活动 {current}" if current else "当前活动 无")
    commitments = [str(c) for c in (block.get("commitments") or []) if str(c).strip()]
    line = f"今日约定 {_to_int(block.get('commitment_count'))} 项"
    if commitments:
        line += f"（{'；'.join(commitments)}）"
    lines.append(line)
    return lines


def _companion_lines(block: Dict[str, Any]) -> List[str]:
    patrol = block.get("patrol") if isinstance(block.get("patrol"), dict) else {}
    lines = [
        f"score {_to_float(block.get('score')):.1f} / baseline {_to_float(block.get('baseline')):.1f} / "
        f"档位 {_to_int(block.get('tier'))}·{str(block.get('tier_label') or '')}",
        f"今日主动发言 {_to_float(block.get('speak_count')):g}/{_to_int(block.get('daily_max'))}"
        f"｜今日Δ {_to_float(block.get('daily_net')):+.2f}",
        f"早安 {'✅' if block.get('morning_sent') else '❌'}"
        f"｜晚安 {'✅' if block.get('night_sent') else '❌'}"
        f"｜想念 {'✅' if block.get('miss_sent') else '❌'}",
        f"巡检 {'运行中' if patrol.get('running') else '未运行'}"
        f"｜上次巡检 {str(patrol.get('last_tick_at') or '无')}"
        f"｜间隔 {_to_int(patrol.get('interval_minutes'))}min"
        f"｜stream_id {'已就绪' if patrol.get('stream_id_ready') else '未解析'}"
        f"｜主动开关 {'开' if patrol.get('trigger_enabled') else '关'}",
    ]
    if block.get("impressions"):
        lines.append("最近印象 " + "；".join(str(i) for i in block["impressions"]))
    tone = block.get("tone")
    if isinstance(tone, dict) and tone.get("text"):
        lines.append(f"关系温度·{tone.get('date') or ''} {tone.get('text')}")
    return lines


def _emotion_lines(block: Dict[str, Any]) -> List[str]:
    labels = [str(name) for name in (block.get("labels") or [])]
    anchor = block.get("anchor") if isinstance(block.get("anchor"), dict) else {}
    offset = block.get("offset") if isinstance(block.get("offset"), dict) else {}
    lines = [
        f"情绪底色 {str(block.get('mood_text') or '未知')}",
        f"标签 {'、'.join(labels) if labels else '复杂难名'}",
        f"当日事件 {_to_int(block.get('event_count'))} 条",
        f"锚点 {_vad_text(anchor)} / 偏移 {_vad_text(offset)}",
        f"初始化 {'已完成' if block.get('initialized') else '未初始化'}",
    ]
    if block.get("frozen"):
        lines.append("睡眠冻结中（醒后一次性结算）")
    return lines


def _qzone_lines(block: Dict[str, Any]) -> List[str]:
    channels = [str(c) for c in (block.get("channels") or [])]
    baseline = list(block.get("comment_baseline") or [])
    lines = [
        f"今日说说 {_to_int(block.get('posts_today'))}/{_to_int(block.get('daily_max_posts'))}",
        f"评论监听 {'开' if block.get('reply_comments_enabled') else '关'}",
        f"动作通道 {'、'.join(channels) if channels else '未连通'}",
        f"cookie {str(block.get('cookie_text') or '未知')}",
    ]
    line = f"评论基线 {len(baseline)} 条"
    if baseline:
        line += f"（{'、'.join(str(t) for t in baseline[:5])}）"
    lines.append(line)
    return lines


_BLOCK_BODY = {
    "planning": _planning_lines,
    "companion": _companion_lines,
    "emotion": _emotion_lines,
    "qzone": _qzone_lines,
}


def block_lines(blocks: Any) -> List[Tuple[str, List[str]]]:
    """四区块展示行 ``[(标题, [行文本, ...]), ...]``；未启用的块只输出「（未启用）」一行。"""
    blocks = blocks if isinstance(blocks, dict) else {}
    out: List[Tuple[str, List[str]]] = []
    for key, title in _BLOCK_TITLES:
        block = blocks.get(key)
        if not isinstance(block, dict) or not block.get("enabled"):
            out.append((title, [_DISABLED_LINE]))
            continue
        out.append((title, _BLOCK_BODY[key](block)))
    return out


def render_dashboard_text(blocks: Any, *, now: Optional[datetime] = None) -> str:
    """纯文本总览（标题行 + 四区块全部行）：图片渲染失败时的降级输出与一致性断言面。"""
    now = now or datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [f"🐱 猫眼生活套件 状态总览 {now.strftime('%Y-%m-%d')} 周{weekday}"]
    for title, body in block_lines(blocks):
        lines.append("")
        lines.append(title)
        lines.extend(f"  {line}" for line in body)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 状态图渲染（PIL 惰性导入；任何异常 → RuntimeError，由调用方降级纯文本）
# ---------------------------------------------------------------------------

#: 分辨率上限（防止 OOM，与 schedule_image 同口径）
_MAX_WIDTH = 1920
_MAX_HEIGHT = 4096
_DEFAULT_WIDTH = 1080

COLOR_TEXT_PRIMARY = (43, 58, 74)
COLOR_TEXT_SECONDARY = (122, 140, 163)
COLOR_CARD = (255, 255, 255, 250)
COLOR_CARD_BORDER = (226, 233, 242, 255)
COLOR_GRADIENT_TOP = (247, 249, 253)
COLOR_GRADIENT_BOTTOM = (235, 240, 248)


def render_dashboard_image(title: str, blocks: Any, *, output_dir: Optional[Path] = None,
                           width: int = _DEFAULT_WIDTH) -> Tuple[str, str]:
    """渲染聚合状态图并保存 ``output_dir/dashboard_today.jpg``，返回 (路径, base64)。

    字体与文字度量复用 planning.schedule_image.ScheduleImageGenerator（打包 NotoSansSC +
    系统字体回退），逐行绘制 ``block_lines`` 的展示行（超宽截断）。高度按行数动态计算，
    上限 4096——超出部分不绘制（宁可少画不裁半行）。

    Raises:
        RuntimeError: pillow/字体/渲染任一步失败（调用方静默降级为纯文本）。
    """
    try:
        from planning.schedule_image import ScheduleImageGenerator as _SIG
        Image, ImageDraw, ImageFont = _SIG._require_pil()
    except RuntimeError:
        raise
    except Exception as exc:  # 渲染链导入失败等同「渲染不可用」
        raise RuntimeError(f"聚合状态图渲染链不可用: {exc}") from exc

    try:
        width = min(int(width or _DEFAULT_WIDTH), _MAX_WIDTH)
        s = width / _DEFAULT_WIDTH

        fonts = {
            "title": _SIG._get_font(ImageFont, int(34 * s)),
            "subtitle": _SIG._get_font(ImageFont, int(20 * s)),
            "block": _SIG._get_font(ImageFont, int(25 * s)),
            "body": _SIG._get_font(ImageFont, int(21 * s)),
            "footer": _SIG._get_font(ImageFont, int(17 * s)),
        }

        margin_x = int(48 * s)
        card_w = width - margin_x * 2
        padding = int(24 * s)
        text_max_w = card_w - padding * 2
        block_title_h = int(48 * s)
        body_line_h = int(34 * s)
        card_gap = int(16 * s)
        header_h = int(152 * s)
        footer_h = int(56 * s)

        # 四区块卡片（标题行 + 展示行）
        layout: List[Tuple[str, List[str], int]] = []
        for block_title, body in block_lines(blocks):
            layout.append((block_title, body, block_title_h + len(body) * body_line_h + padding))
        height = header_h + sum(h for _, _, h in layout) + card_gap * max(0, len(layout) - 1) + footer_h
        height = min(max(int(height), int(240 * s)), _MAX_HEIGHT)

        img = Image.new("RGB", (width, height), (246, 248, 252))
        draw = ImageDraw.Draw(img, "RGBA")

        # 浅色纵向渐变画布
        top_color, bottom_color = COLOR_GRADIENT_TOP, COLOR_GRADIENT_BOTTOM
        for y in range(height):
            ratio = y / max(1, height - 1)
            color = tuple(int(top_color[i] + (bottom_color[i] - top_color[i]) * ratio) for i in range(3))
            draw.line([(0, y), (width, y)], fill=(*color, 255))

        # 头部：标题 + 生成时间副标题
        _SIG._draw_text(draw, (margin_x, int(46 * s)), str(title or ""),
                        COLOR_TEXT_PRIMARY, fonts["title"], bold=True)
        _SIG._draw_text(
            draw, (margin_x, int(46 * s) + int(54 * s)),
            f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}｜四模块聚合（空白区块表示该模块未启用）",
            COLOR_TEXT_SECONDARY, fonts["subtitle"],
        )

        # 逐块卡片：block_lines 的行原样画，超宽截断
        y = header_h
        for block_title, body, block_h in layout:
            draw.rounded_rectangle(
                [margin_x, y, margin_x + card_w, y + block_h],
                radius=int(18 * s), fill=COLOR_CARD, outline=COLOR_CARD_BORDER, width=1,
            )
            _SIG._draw_text(draw, (margin_x + padding, y + int(14 * s)), block_title,
                            COLOR_TEXT_PRIMARY, fonts["block"], bold=True)
            line_y = y + block_title_h
            for line in body:
                if line_y + body_line_h > height - footer_h:
                    break  # 触到画布上限：剩余行不画（不裁半行）
                text = _SIG._ellipsis(draw, str(line), fonts["body"], text_max_w)
                _SIG._draw_text(draw, (margin_x + padding, line_y), text,
                                COLOR_TEXT_SECONDARY, fonts["body"])
                line_y += body_line_h
            y += block_h + card_gap

        # 底部签名
        signature = "Powered by Mai-Bot"
        sig_w = _SIG._text_width(draw, signature, fonts["footer"])
        _SIG._draw_text(draw, ((width - sig_w) // 2, height - footer_h + int(14 * s)),
                        signature, (159, 176, 198), fonts["footer"])

        save_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent
        save_dir.mkdir(parents=True, exist_ok=True)
        image_path = save_dir / "dashboard_today.jpg"
        img.save(str(image_path), format="JPEG", quality=90, optimize=True)
        img_byte_arr = io.BytesIO()
        img.save(img_byte_arr, format="JPEG", quality=90, optimize=True)
        return str(image_path), base64.b64encode(img_byte_arr.getvalue()).decode("utf-8")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"聚合状态图渲染失败: {exc}") from exc
