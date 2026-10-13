"""聚合状态总览（M7）：四模块状态块 → 展示行 → 纯文本 / 状态图。

设计要点：
- **纯函数模块**（不 import SDK、不持状态）：四类状态块按「形状」从各服务对象取数，
  任一对象缺失或取数抛错一律回退安全值（0 / 空列表 / None），绝不外抛——总览的语义是
  「一个模块坏了（或没启用）不拖垮整张图」，对应区块显示「（未启用）」。
- ``block_lines`` 是**图片**的展示来源；``render_dashboard_text`` 与其共用同一套区块
  行构造，仅 planning 区块额外展开 ``items_full``（今日全部条目，不截断——被删
  ``/lbm_mai_schedule`` 的信息面），图片档照旧 ≤5 条。
- PIL 惰性导入（与 planning/schedule_image.py 同口径）：pillow 未安装或找不到可用中文字体
  时抛 RuntimeError，由调用方静默降级为纯文本。
"""

from __future__ import annotations

import base64
import io
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from planning.time_utils import format_day_minute

#: 区块标题（键 → 固定标题；顺序即展示顺序）
_BLOCK_TITLES: Tuple[Tuple[str, str], ...] = (
    ("planning", "日程规划"),
    ("companion", "伴侣关系"),
    ("emotion", "情绪"),
    ("qzone", "QQ空间"),
    ("usage", "模型消耗"),
)

#: 未启用区块的唯一一行
_DISABLED_LINE = "（未启用）"

#: 模块强调条配色（F2 重排；键与 _BLOCK_TITLES 对齐）
_MODULE_ACCENT: Dict[str, Tuple[int, int, int]] = {
    "planning": (91, 124, 250),
    "companion": (235, 106, 140),
    "emotion": (245, 166, 75),
    "qzone": (90, 190, 140),
    "usage": (130, 120, 220),
}

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
    """条目 → ``HH:MM-HH:MM``（跨午夜经 :func:`format_day_minute` 标「次日」；分钟字段缺失/异常按 00:00 兜底）。"""
    start = _to_int(item.get("start_minute"), 0) if isinstance(item, dict) else 0
    end = _to_int(item.get("end_minute"), 0) if isinstance(item, dict) else 0
    return f"{format_day_minute(start)}-{format_day_minute(end)}"


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
    """日程块：今日条目（≤5）/ 今日全部条目（items_full，文本档用）/ 当前活动 / 今日约定（≤3）。"""
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    block: Dict[str, Any] = {
        "enabled": False, "date": date, "item_count": 0, "items": [], "items_full": [],
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
    # 文本降级档展开完整日程（被删 /lbm_mai_schedule 的信息面）：图片档仍只画 items
    block["items_full"] = [
        f"{_time_range(item)} {str(item.get('name') or '')}".strip()
        for item in items if isinstance(item, dict)
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
        "comment_baseline": [], "image_mode": "off",
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
        # §18.3（2026-10-12）：配图来源（缺键/旧快照 → off）——面板行为照常、多一个读数
        "image_mode": str(snapshot.get("image_mode") or "off").strip().lower() or "off",
    })
    return block


# ---------------------------------------------------------------------------
# 展示行（文本与图片共用的唯一来源）
# ---------------------------------------------------------------------------


def _vad_text(state: Dict[str, Any]) -> str:
    return (f"V={_to_float(state.get('valence')):+.2f} "
            f"A={_to_float(state.get('arousal')):+.2f} "
            f"D={_to_float(state.get('dominance')):+.2f}")


def _planning_lines(block: Dict[str, Any], *, full: bool = False) -> List[str]:
    """日程展示行；``full=True``（文本降级档）用 items_full 展开今日全部条目，不截断。"""
    lines = [f"今日日程 {_to_int(block.get('item_count'))} 项"]
    if full:
        for entry in block.get("items_full") or []:
            if str(entry).strip():
                lines.append(str(entry).strip())
    else:
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
        f"早安 {'是' if block.get('morning_sent') else '否'}"
        f"｜晚安 {'是' if block.get('night_sent') else '否'}"
        f"｜想念 {'是' if block.get('miss_sent') else '否'}",
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
    image_mode = str(block.get("image_mode") or "off").strip().lower() or "off"
    lines = [
        f"今日说说 {_to_int(block.get('posts_today'))}/{_to_int(block.get('daily_max_posts'))}",
        f"评论监听 {'开' if block.get('reply_comments_enabled') else '关'}｜配图 {image_mode}",
        f"动作通道 {'、'.join(channels) if channels else '未连通'}",
        f"cookie {str(block.get('cookie_text') or '未知')}",
    ]
    line = f"评论基线 {len(baseline)} 条"
    if baseline:
        line += f"（{'、'.join(str(t) for t in baseline[:5])}）"
    lines.append(line)
    return lines


def _usage_lines(block: Any) -> List[str]:
    """模型消耗块（F3/F2）：行由 companion 侧 ``_mai_usage_lines`` 生成，这里只做展示投影。"""
    lines = block.get("lines") if isinstance(block, dict) else None
    if isinstance(lines, list) and lines:
        return [str(line) for line in lines]
    return ["最近 24 小时：无 LLM 调用"]


_BLOCK_BODY = {
    "planning": _planning_lines,
    "companion": _companion_lines,
    "emotion": _emotion_lines,
    "qzone": _qzone_lines,
    "usage": _usage_lines,
}


def _block_lines(blocks: Any, *, full_items: bool) -> List[Tuple[str, List[str]]]:
    """四区块展示行 ``[(标题, [行文本, ...]), ...]``；未启用的块只输出「（未启用）」一行。

    ``full_items=True`` 只影响 planning 区块（展开 items_full 完整日程，文本降级档用）；
    图片档走 ``block_lines``（full_items=False，条目 ≤5）。
    """
    blocks = blocks if isinstance(blocks, dict) else {}
    out: List[Tuple[str, List[str]]] = []
    for key, title in _BLOCK_TITLES:
        block = blocks.get(key)
        if not isinstance(block, dict) or not block.get("enabled"):
            out.append((title, [_DISABLED_LINE]))
            continue
        if key == "planning" and full_items:
            out.append((title, _planning_lines(block, full=True)))
            continue
        out.append((title, _BLOCK_BODY[key](block)))
    return out


def block_lines(blocks: Any) -> List[Tuple[str, List[str]]]:
    """图片档的区块展示行（planning 条目 ≤5 截断）；状态图与外部一致性断言面。"""
    return _block_lines(blocks, full_items=False)


def render_dashboard_text(blocks: Any, *, now: Optional[datetime] = None) -> str:
    """纯文本总览（标题行 + 四区块全部行）：图片渲染失败时的降级输出与一致性断言面。

    planning 区块用 items_full 展开今日完整日程（不截断），并逐行带上「今日日程 N 项 /
    当前活动 / 今日约定」——被删 /lbm_mai_schedule 命令的信息面在文本档保留。
    """
    now = now or datetime.now()
    weekday = "一二三四五六日"[now.weekday()]
    lines = [f"属于麦麦的生活 状态总览 {now.strftime('%Y-%m-%d')} 周{weekday}"]
    for title, body in _block_lines(blocks, full_items=True):
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
    """渲染聚合状态图（瑞士极简 · V2 双栏网格）并保存 ``dashboard_today.jpg``。

    版式（2026-10-11 用户选型）：纸底/墨字/单红强调，细线分节；
    左栏三个巨型读数（好感 / 情绪 / 24h tokens），右栏 01~05 编号节
    （日程/陪伴/情绪/空间/消耗），每节右上红色命令指路；情绪节注入
    F5 详细情绪描述（换行排印）。行高与字号较旧卡片版增大。

    Args:
        output_dir: 图片输出目录（宿主隔离数据目录；None=系统临时目录，绝不写插件目录）。

    Returns:
        (图片路径, base64)。

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
        M = int(64 * s)
        inner = width - 2 * M
        PAPER, INK, RED = (242, 241, 237), (17, 17, 17), (232, 64, 42)
        MUTED, HAIR = (107, 107, 107), (205, 203, 196)

        def _font(size, bold=False):
            return _SIG._get_font(ImageFont, int(size * s), bold=bold)

        f_label, f_big = _font(22, True), _font(84, True)
        f_mid, f_sub = _font(56, True), _font(20)
        f_sec, f_cmd = _font(22, True), _font(18)
        f_row, f_foot = _font(22), _font(17)

        data = blocks if isinstance(blocks, dict) else {}
        planning = data.get("planning") if isinstance(data.get("planning"), dict) else {}
        companion = data.get("companion") if isinstance(data.get("companion"), dict) else {}
        emotion = data.get("emotion") if isinstance(data.get("emotion"), dict) else {}
        qzone = data.get("qzone") if isinstance(data.get("qzone"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

        def _spaced(text):
            return " ".join(list(str(text)))

        def _tw(text, font):
            return font.getlength(text)

        def _cap(text, font, max_w):
            """先把超长输入按平均字宽预截到量级内，再交给精细截断（防 O(n²) 与超大串）。"""
            text = str(text)[:2000]
            if _tw(text, font) <= max_w:
                return text
            avg = _tw(text, font) / max(1, len(text))
            return text[:max(1, int(max_w / max(avg, 1.0)) + 2)]

        def _right(x, y, text, font, fill, max_w):
            text = _cap(text, font, max_w)
            while text and _tw(text, font) > max_w:
                text = text[:-1]
            draw.text((x + max_w - _tw(text, font), y), text, font=font, fill=fill)

        def _ellip(text, font, max_w):
            text = _cap(text, font, max_w)
            while text and _tw(text, font) > max_w:
                text = text[:-1]
            return text + ("…" if text and text != str(text) else "")

        def _wrap(text, font, max_w, *, max_lines=12):
            """情绪描述换行（有界版）：输入预截 400 字、输出 ≤ max_lines 行、
            逐字符累计宽度（O(n)，不再每字符重扫全串）。"""
            text = str(text or "").strip()[:400]
            lines, current, width_acc = [], "", 0.0
            for ch in text:
                ch_w = font.getlength(ch)
                if current and width_acc + ch_w > max_w:
                    lines.append(current)
                    current, width_acc = ch, ch_w
                    if len(lines) >= max_lines:
                        break
                else:
                    current += ch
                    width_acc += ch_w
            if current and len(lines) < max_lines:
                lines.append(current)
            return lines or [""]

        def _enabled(key):
            block = data.get(key)
            return isinstance(block, dict) and bool(block.get("enabled"))

        score = _to_float(companion.get("score"), 0.0)
        tier_label = str(companion.get("tier_label") or "")
        baseline = _to_float(companion.get("baseline"), 0.0)
        labels = [str(x) for x in (emotion.get("labels") or []) if str(x)]
        emo_word = labels[0] if labels else "—"
        emo_sub = str(emotion.get("mood_text") or "")
        desc = str(emotion.get("description") or "")
        mode = str(emotion.get("mode") or "")
        tokens = _to_int(usage.get("tokens"), 0)
        calls = _to_int(usage.get("calls"), 0)
        tokens_big = f"{tokens / 1000:.1f}k" if tokens >= 1000 else str(tokens)
        current = str(planning.get("current") or "").strip()

        left_w = int(inner * 0.34)
        right_x = M + left_w + int(48 * s)
        right_w = width - M - right_x
        row_h, sec_head = int(52 * s), int(46 * s)

        sections = [
            ("01", _spaced("日程"), f"{_to_int(planning.get('item_count'), 0)} 项在列",
             _BLOCK_BODY["planning"](planning)[:40], "/lbm_plan list · /lbm_plan regenerate"),
            ("02", _spaced("陪伴"), tier_label,
             _BLOCK_BODY["companion"](companion)[:40], "/lbm_mai_affection"),
            ("03", _spaced("情绪"), mode,
             (_BLOCK_BODY["emotion"](emotion) + (_wrap(desc, f_row, right_w) if desc else []))[:40],
             "/lbm_emotion_config"),
            ("04", _spaced("空间"), "",
             _BLOCK_BODY["qzone"](qzone)[:40], "/lbm_life qzone comments"),
            ("05", _spaced("消耗"), "24h",
             ([str(line) for line in (usage.get("lines") or [])] or ["最近 24 小时：无 LLM 调用"])[:12],
             "/lbm_mai_diag llm 1"),
        ]
        right_h = sum(sec_head + len(lines) * row_h + int(30 * s)
                      for _, _, _, lines, _ in sections)
        left_h = int(640 * s)
        body_h = max(right_h, left_h)
        # 热修（2026-10-11 服务器死机）：高度必须钳制在 _MAX_HEIGHT——旧行数爆炸
        # （如超长情绪描述/异常块）曾让 Image.new 申请 GB 级位图直接 OOM
        height = min(int(120 * s) + body_h + int(96 * s), _MAX_HEIGHT)

        img = Image.new("RGB", (width, height), PAPER)
        draw = ImageDraw.Draw(img)

        # 头部：红点 + 标记 | 标题（含日期）+ 2px 墨线
        draw.ellipse([M, M + 2, M + 18, M + 20], fill=RED)
        draw.text((M + 30, M), _spaced("MAI · STATE"), font=_font(20, True), fill=INK)
        _right(M, M, str(title or ""), _font(20, True), INK, inner - 30)
        draw.line([(M, M + int(44 * s)), (width - M, M + int(44 * s))], fill=INK, width=2)

        y = int(120 * s)
        # 左栏：三个巨型读数
        lx = M
        draw.text((lx, y), "好感", font=f_label, fill=RED)
        draw.text((lx, y + int(34 * s)), f"{score:g}", font=f_big, fill=INK)
        draw.text((lx, y + int(150 * s)),
                  f"{tier_label} · 基线 {baseline:g}".strip(" ·"), font=f_sub, fill=MUTED)
        y2 = y + int(212 * s)
        draw.text((lx, y2), "情绪", font=f_label, fill=RED)
        draw.text((lx, y2 + int(34 * s)), emo_word, font=f_mid, fill=INK)
        draw.text((lx, y2 + int(112 * s)), emo_sub, font=f_sub, fill=MUTED)
        y3 = y2 + int(170 * s)
        draw.text((lx, y3), "24h 消耗", font=f_label, fill=RED)
        draw.text((lx, y3 + int(34 * s)), tokens_big, font=f_mid, fill=INK)
        draw.text((lx, y3 + int(112 * s)), f"tokens · {calls} 次调用", font=f_sub, fill=MUTED)

        # 分隔细线（长度随右栏内容）
        draw.line([(right_x - int(24 * s), y), (right_x - int(24 * s), y + body_h - int(30 * s))],
                  fill=HAIR, width=1)

        # 右栏：编号节（行 = 区块展示行，细线分隔；节尾红色命令指路）
        ry = y
        for num, label, sub, lines, cmd in sections:
            draw.text((right_x, ry), num, font=f_label, fill=RED)
            draw.text((right_x + int(46 * s), ry), label, font=f_sec, fill=INK)
            if sub:
                _right(right_x, ry + int(4 * s), sub, f_sub, MUTED, right_w - int(46 * s))
            ry += int(40 * s)
            for line in lines:
                text = _ellip(line, f_row, right_w - int(46 * s))
                draw.text((right_x + int(46 * s), ry), text, font=f_row,
                          fill=MUTED if str(line) == "（未启用）" else INK)
                ry += row_h
            _right(right_x, ry - int(10 * s), cmd, f_cmd, RED, right_w)
            ry += int(30 * s)

        # 底部签名（2px 墨线 + 左右签名）
        foot_y = height - int(58 * s)
        draw.line([(M, foot_y), (width - M, foot_y)], fill=INK, width=2)
        draw.text((M, foot_y + int(14 * s)), _spaced("属于麦麦的生活 v2.0.0"), font=f_foot, fill=MUTED)
        sig = "Powered by Mai-Bot"
        draw.text((width - M - _tw(sig, f_foot), foot_y + int(14 * s)), sig, font=f_foot, fill=MUTED)

        # output_dir 缺省 = 系统临时目录（与同族渲染器同一契约）：
        # 绝不回退到插件目录/代码目录（宿主隔离数据目录由调用方传入）。
        save_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
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
