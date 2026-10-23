"""聚合状态总览（M7 / R7 §三十）：四模块状态块 → 结构化 view model → 纯文本 / 状态图。

设计要点：
- **纯函数模块**（不 import SDK、不持状态）：四类状态块按「形状」从各服务对象取数，
  任一对象缺失或取数抛错一律回退安全值（0 / 空列表 / None），绝不外抛——总览的语义是
  「一个模块坏了（或没启用）不拖垮整张图」，对应区块显示「（未启用）」。
- **数据面 / 展示面分离**（指导 §4.10 / §9）：``block_lines`` + ``render_dashboard_text``
  继续输出**完整文字版**（planning 区块展开 ``items_full`` 今日全部条目——被删
  ``/lbm_mai_schedule`` 的信息面）；图片只消费 :func:`build_dashboard_view` 产出的
  结构化 view model，不再把 ``_BLOCK_BODY`` 拼好的整行字符串按字符数截断后塞进右栏。
- **版式**（指导 §7）：左栏 280 KPI 轨（好感/情绪/24h 消耗/关系温度/命令面板）、
  竖分隔线 x=379、右栏 5 节 601（01 日程 / 02 陪伴 / 03 情绪 / 04 空间 / 05 消耗）。
  所有文本先 ``fit_text`` / ``wrap_text`` 按像素宽度测量，anchor 基线绘制。
- **两遍绘制**（指导 §8.3）：``_measure_dashboard`` 先算出每块/每节高度与所有绘制指令，
  ``_draw_dashboard`` 只按测量结果落笔；footer 由内容底部 + 26 反推，不依赖 4096 钳制
  裁掉内容。超限时按右栏节分页（``dashboard_today_p2.jpg`` 等，首文件契约不变：
  返回第一页 ``(path, base64)``）。
- PIL 惰性导入（与 planning/schedule_image.py 同口径）：pillow 未安装或找不到可用中文字体
  时抛 RuntimeError，由调用方静默降级为纯文本。
"""

from __future__ import annotations

import base64
import io
import logging
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core import render_kit
from core.render_kit import (
    FAINT,
    HAIR,
    INK,
    INK_SOFT,
    MUTED,
    PAPER,
    RED,
    ROWLINE,
    TINT,
    TRACK,
    Canvas,
)
from planning.time_utils import format_day_minute

logger = logging.getLogger(__name__)

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

# ---------------------------------------------------------------------------
# 指导 §5.3 / §7 固定网格（1080 宽）
# ---------------------------------------------------------------------------

PAGE_WIDTH = 1080
MARGIN_X = 64
CONTENT_W = 952
TEXT_RIGHT = 1016
LEFT_X = 64
LEFT_W = 280
DIVIDER_X = 379
RIGHT_X = 415
RIGHT_W = 601
HEADER_H = 75
BODY_TOP = 111
DIVIDER_TOP = 113
FOOTER_GAP = 26
FOOTER_H = 62
SECTION_GAP = 22

#: 高度上限（防止 OOM，与 schedule_image 同口径；4096 按 1× 高度判断）
_MAX_HEIGHT = 4096
_DEFAULT_WIDTH = 1080

#: 24h 消耗堆叠条的单色阶（指导 §7.3；不引入第二套彩色语义）
_MODULE_STACK_COLORS: Dict[str, Tuple[int, int, int]] = {
    "planning": (183, 178, 168),   # #B7B2A8
    "companion": (139, 134, 124),  # #8B867C
    "emotion": (43, 43, 43),       # #2B2B2B
    "qzone": (214, 209, 199),      # #D6D1C7
}
_MODULE_LABELS: Dict[str, str] = {
    "planning": "规划",
    "companion": "陪伴",
    "emotion": "情绪",
    "qzone": "空间",
}
_MODULE_KEYS: Tuple[str, ...] = ("planning", "companion", "emotion", "qzone")

#: 唯一命令区（指导 §7.3：命令只出现一次，按 01~05 与右栏节号一一对应）
_COMMAND_ROWS: Tuple[Tuple[str, str, str], ...] = (
    ("01", "日程", "/lbm_plan list"),
    ("02", "陪伴", "/lbm_mai_affection"),
    ("03", "情绪", "/lbm_emotion_config"),
    ("04", "空间", "/lbm_life qzone comments"),
    ("05", "消耗", "/lbm_mai_diag llm 1"),
)

#: 关系/情绪引用块最多行数（指导 §7.3/§7.4）
_QUOTE_MAX_LINES = 3


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
    """日程块：今日条目（≤5）/ 今日全部条目（items_full，文本档用）/ 当前活动 / 今日约定（≤3）。

    R7（指导 §9.2）：在既有字段之上**新增** ``done_count`` / ``upcoming_count`` /
    ``next_items``（当前之后下 3 条）/ ``current`` 结构体 ``{"time","name"}``。
    文字版仍读 ``items`` / ``items_full`` / ``commitments``（字段全保留）；
    ``current`` 由拼好的字符串改为结构体后，``_planning_lines`` 负责拼回原文本，
    文字版输出**逐字不变**。
    """
    now = now or datetime.now()
    date = now.strftime("%Y-%m-%d")
    block: Dict[str, Any] = {
        "enabled": False, "date": date, "item_count": 0, "items": [], "items_full": [],
        "current": None, "current_item": None,
        "done_count": 0, "upcoming_count": 0, "next_items": [],
        "commitment_count": 0, "commitments": [],
    }
    if not enabled:
        return block
    block["enabled"] = True

    items = _rows(schedule_query, "get_day_schedule", date)
    raw_items = [item for item in items if isinstance(item, dict)]
    block["item_count"] = len(items)
    block["items"] = [
        {"time": _time_range(item), "name": str(item.get("name") or "")}
        for item in raw_items[:5]
    ]
    # 文本降级档展开完整日程（被删 /lbm_mai_schedule 的信息面）：图片档仍只画 items
    block["items_full"] = [
        f"{_time_range(item)} {str(item.get('name') or '')}".strip()
        for item in raw_items
    ]

    current = _call(schedule_query, "find_current_activity", date, now)
    if isinstance(current, dict):
        name = str(current.get("name") or "")
        current_struct = {"time": _time_range(current), "name": name}
        block["current"] = current_struct
        # 兼容别名（§9.2 的「current 结构体」语义；保留 current_item 便于外部只读消费）
        block["current_item"] = dict(current_struct)

    now_minutes = now.hour * 60 + now.minute
    done = 0
    for item in raw_items:
        end = _to_int(item.get("end_minute"), 0)
        # 当日帧内已完成：结束分钟已过（跨午夜 end>1440 不计入当日完成，与日程图口径一致）
        if 0 < end <= now_minutes:
            done += 1
    block["done_count"] = done
    block["upcoming_count"] = max(0, len(raw_items) - done - (1 if isinstance(current, dict) else 0))
    block["next_items"] = [
        {"time": _time_range(item), "name": str(item.get("name") or ""), "status": "待开始"}
        for item in sorted(raw_items, key=lambda it: _to_int(it.get("start_minute"), 0))
        if _to_int(item.get("start_minute"), 0) > now_minutes
    ][:3]

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
    """情绪块：取 ``emotion_svc.service.status_snapshot()``（EmotionServices 束形状）。

    R7（指导 §9.2）：透传快照新增的 ``label`` / ``share`` / ``vad_words`` / ``effective``；
    旧快照（无这些键）回退为空值，不破坏既有 consumers。
    R35（2026-10-24 直派）：透传 ``recent_events``（最近 3 条情绪事件的时间/描述/ΔVAD）；
    旧快照无键回退 ``[]``，缺键/形状异常逐条防御。
    """
    section = getattr(config, "emotion", None)
    block: Dict[str, Any] = {
        "enabled": False, "module_enabled": bool(getattr(section, "enabled", True)),
        "initialized": False, "mood_text": "", "labels": [], "event_count": 0,
        "label": "", "share": 0.0, "vad_words": {},
        "effective": {}, "anchor": {}, "offset": {}, "frozen": False,
        "recent_events": [],
    }
    service = getattr(emotion_svc, "service", None) if emotion_svc is not None else None
    if not enabled or service is None:
        return block
    snapshot = _mapping(service, "status_snapshot")
    labels = [str(name) for name in (snapshot.get("labels") or [])]
    label = str(snapshot.get("label") or (labels[0] if labels else ""))
    block.update({
        "enabled": True,
        "initialized": bool(snapshot.get("initialized")),
        "mood_text": str(snapshot.get("mood_text") or ""),
        "labels": labels,
        "label": label,
        "share": _to_float(snapshot.get("share"), 0.0),
        "vad_words": snapshot.get("vad_words") if isinstance(snapshot.get("vad_words"), dict) else {},
        "effective": snapshot.get("effective") if isinstance(snapshot.get("effective"), dict) else {},
        "event_count": _to_int(snapshot.get("event_count")),
        "anchor": snapshot.get("anchor") if isinstance(snapshot.get("anchor"), dict) else {},
        "offset": snapshot.get("offset") if isinstance(snapshot.get("offset"), dict) else {},
        "frozen": bool(snapshot.get("frozen")),
        "recent_events": _recent_event_rows(snapshot),
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
# 文字版展示行（§9.3 铁律：文字版字段全保留；图片不再消费这些拼接串）
# ---------------------------------------------------------------------------


def _vad_text(state: Dict[str, Any]) -> str:
    return (f"V={_to_float(state.get('valence')):+.2f} "
            f"A={_to_float(state.get('arousal')):+.2f} "
            f"D={_to_float(state.get('dominance')):+.2f}")


def _recent_event_rows(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """R35：快照 ``recent_events`` 归一化（最多 3 条；缺键/非列表/条目形状异常 → 安全值）。"""
    events = snapshot.get("recent_events") if isinstance(snapshot, dict) else None
    if not isinstance(events, list):
        return []
    rows: List[Dict[str, Any]] = []
    for event in events[:3]:
        if not isinstance(event, dict):
            continue
        rows.append({
            "time": str(event.get("time") or "").strip(),
            "desc": str(event.get("desc") or "").strip(),
            "v": _to_float(event.get("v")),
            "a": _to_float(event.get("a")),
            "d": _to_float(event.get("d")),
        })
    return rows


def _recent_delta_text(event: Dict[str, Any]) -> str:
    """R35：单条事件的 ΔVAD 标注「（V+0.08|A-0.05|D+0.12）」；|变动| < 0.005 的维度不列
    （与随图日志口径对齐）；三维全部低于阈值 →「（微小波动）」。"""
    parts = []
    for key, letter in (("v", "V"), ("a", "A"), ("d", "D")):
        value = _to_float(event.get(key))
        if abs(value) >= 0.005:
            parts.append(f"{letter}{value:+.2f}")
    return f"（{'|'.join(parts)}）" if parts else "（微小波动）"


def _current_text(block: Dict[str, Any]) -> str:
    """当前活动：兼容结构体（§9.2 新口径）与旧字符串（外部/测试桩）两种形态。"""
    current = block.get("current")
    if isinstance(current, dict):
        return f"{str(current.get('time') or '')} {str(current.get('name') or '')}".strip()
    return str(current or "").strip()


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
    current = _current_text(block)
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
    # R35（2026-10-24 直派）：「近期变化」段——最近 3 条已入账情绪事件的成因与 ΔVAD；
    # §9.3 铁律：既有行零删，只追加。旧快照无 recent_events → 空列表 →「（无记录）」。
    recent = block.get("recent_events") if isinstance(block.get("recent_events"), list) else []
    if recent:
        for event in recent[:3]:
            if not isinstance(event, dict):
                continue
            time_text = str(event.get("time") or "").strip()
            desc = str(event.get("desc") or "").strip() or "（无描述）"
            lines.append(f"近期变化 {time_text} {desc}{_recent_delta_text(event)}".strip())
    else:
        lines.append("近期变化 （无记录）")
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
    ``block_lines``（图片旧一致性断言面）仍按 ≤5 条输出。
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
# 结构化 view model（指导 §9；图片唯一数据面）
# ---------------------------------------------------------------------------


def _dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _fmt_score(value: Any) -> str:
    return f"{_to_float(value):.4f}".rstrip("0").rstrip(".") or "0"


def _fmt_dash(time_text: str) -> str:
    """列式时间统一 en dash（指导 §6.6）；文字版仍用 ``-``。"""
    return str(time_text or "").replace("-", "–")


def _fmt_vad(state: Dict[str, Any]) -> str:
    """V/A/D 单行：``V +0.45  A -0.25  D +0.30``（正负号统一为 ASCII +/-）。

    说明：指导 §7.4 的「−」是排版字符；打包字体对 U+2212 的字形覆盖不可靠
    （实渲染出现缺字空档），故统一用 ASCII ``-``，语义与对齐不变。
    """
    def _one(key: str, letter: str) -> str:
        return f"{letter} {_to_float(state.get(key)):+.2f}"

    return f"{_one('valence', 'V')}  {_one('arousal', 'A')}  {_one('dominance', 'D')}"


def _coarse_word(value: str) -> str:
    return {"很低": "很低", "偏低": "偏低", "中等": "中",
            "偏高": "偏高", "很高": "很高"}.get(str(value), str(value) or "—")


def _coarse_from_mood_text(mood_text: str) -> str:
    """权宜回退（指导 §7.3）：mood_text 第二段「愉悦度：中等；唤醒度：中等；支配感：中等」。

    结构化 ``vad_words`` 优先；缺失时按「。」切分后解析三个档词，绝不把整句塞进 280px 栏。
    """
    text = str(mood_text or "")
    tail = text.split("。", 1)[1] if "。" in text else text
    found = re.findall(r"(?:愉悦度|唤醒度|支配感)[：:]\s*([^；;，,\s]+)", tail)
    labels = ("愉悦", "唤醒", "支配")
    words = [_coarse_word(found[i]) if i < len(found) else "—" for i in range(3)]
    return " · ".join(f"{labels[i]} {words[i]}" for i in range(3))


def _patrol_clock(value: Any) -> str:
    """巡检时间统一 ``HH:MM:SS``（指导 §9.2：数据面/渲染层统一，避免时区格式漂移）。"""
    text = str(value or "").strip()
    if not text:
        return "无"
    match = re.search(r"(\d{2}:\d{2}:\d{2})", text)
    return match.group(1) if match else text


def _compact_tokens(tokens: int) -> str:
    """左栏只给到 k（指导 §7.3）；右栏 05 给精确值。"""
    if tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def _header_identity(date_text: str, now: datetime) -> Tuple[str, str]:
    """日期 + 星期：优先解析 ``YYYY-MM-DD``，失败回退当前时刻。"""
    text = str(date_text or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        try:
            dt = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            return dt.strftime("%Y-%m-%d"), "周" + "一二三四五六日"[dt.weekday()]
        except ValueError:
            pass
    return now.strftime("%Y-%m-%d"), "周" + "一二三四五六日"[now.weekday()]


def _bool_word(flag: bool) -> str:
    return "是" if flag else "否"


def build_dashboard_view(blocks: Any, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """状态块 → 图片用结构化 view model（指导 §8.6 / §9.2）。

    返回值只含**已分层**的字段（label/value/segments/groups），渲染器不再解析
    ``_BLOCK_BODY`` 拼好的整行字符串；文字版 ``render_dashboard_text`` 走另一条路径。
    """
    data = _dict(blocks)
    now = now or datetime.now()
    planning = _dict(data.get("planning"))
    companion = _dict(data.get("companion"))
    emotion = _dict(data.get("emotion"))
    qzone = _dict(data.get("qzone"))
    usage = _dict(data.get("usage"))

    date_text, weekday = _header_identity(str(planning.get("date") or ""), now)

    # ── 左栏：好感 ──────────────────────────────────────────────
    affection_enabled = bool(companion.get("enabled"))
    score = _to_float(companion.get("score"))
    baseline = _to_float(companion.get("baseline"))
    affection = {
        "enabled": affection_enabled,
        "score_text": _fmt_score(score),
        "tier_text": f"档位 {_to_int(companion.get('tier'))}"
                     + (f" · {str(companion.get('tier_label') or '').strip()}"
                        if str(companion.get("tier_label") or "").strip() else ""),
        "fill_frac": max(0.0, min(1.0, score / 100.0)),
        "baseline_frac": max(0.0, min(1.0, baseline / 100.0)),
        "baseline_text": f"基线 {_fmt_score(baseline)}",
        "daily_text": f"今日 {_to_float(companion.get('daily_net')):+.2f}",
    }

    # ── 左栏：情绪（结构化优先，mood_text 权宜回退）──────────────
    emotion_enabled = bool(emotion.get("enabled"))
    label = str(emotion.get("label") or "").strip()
    share = _to_float(emotion.get("share"))
    if not label:
        labels = [str(name) for name in (emotion.get("labels") or []) if str(name).strip()]
        label = labels[0] if labels else ""
    if not label or share <= 0:
        # 权宜回退（指导 §7.3）：旧快照没有 label/share 时，从 mood_text 第一段
        # 「怀旧(100%)、平静(31%)」解析主标签与占比；结构化字段优先。
        match = re.search(r"([^()（）。,，、\s]+)\((\d+(?:\.\d+)?)%\)",
                          str(emotion.get("mood_text") or ""))
        if match:
            if not label:
                label = match.group(1)
            if share <= 0:
                share = _to_float(match.group(2)) / 100.0
    vad_words = _dict(emotion.get("vad_words"))
    if vad_words:
        coarse_text = (f"愉悦 {_coarse_word(vad_words.get('v', ''))} · "
                       f"唤醒 {_coarse_word(vad_words.get('a', ''))} · "
                       f"支配 {_coarse_word(vad_words.get('d', ''))}")
    else:
        coarse_text = _coarse_from_mood_text(str(emotion.get("mood_text") or ""))
    emotion_left = {
        "enabled": emotion_enabled,
        "word": label or "复杂难名",
        "share_text": f"{share:.0%}" if label and share > 0 else "",
        "coarse_text": coarse_text,
    }

    # ── 左栏：24h 消耗 ─────────────────────────────────────────
    tokens = max(0, _to_int(usage.get("tokens")))
    calls = max(0, _to_int(usage.get("calls")))
    modules = _dict(usage.get("modules"))
    module_rows = []
    for key in _MODULE_KEYS:
        value = max(0, _to_int(modules.get(key)))
        module_rows.append({"key": key, "label": _MODULE_LABELS.get(key, key),
                            "value": value, "color": _MODULE_STACK_COLORS[key]})
    module_sum = sum(row["value"] for row in module_rows)
    for row in module_rows:
        row["frac"] = (row["value"] / module_sum) if module_sum else 0.0
        row["compact"] = _compact_tokens(row["value"])
    usage_left = {
        "enabled": bool(usage.get("enabled", True)),
        "tokens": tokens,
        "calls": calls,
        "number_text": _compact_tokens(tokens),
        "sub_text": f"tokens · {calls} 次调用",
        "modules": module_rows,
        "module_sum": module_sum,
    }

    # ── 左栏：关系温度 ─────────────────────────────────────────
    tone = _dict(companion.get("tone"))
    impressions = [str(x).strip() for x in (companion.get("impressions") or []) if str(x).strip()]
    relation = {
        "enabled": affection_enabled,
        "date": str(tone.get("date") or "").strip(),
        "quote": str(tone.get("text") or "").strip(),
        "impression": "；".join(impressions[-2:]),
    }

    # ── 左栏：命令面板（唯一命令区）────────────────────────────
    commands = [{"num": num, "title": title, "command": cmd}
                for num, title, cmd in _COMMAND_ROWS]

    # ── 右栏 01 日程 ───────────────────────────────────────────
    planning_enabled = bool(planning.get("enabled"))
    current = planning.get("current")
    if isinstance(current, dict):
        current_struct = {"time": _fmt_dash(str(current.get("time") or "")),
                          "name": str(current.get("name") or "")}
    elif isinstance(current, str) and current.strip():
        # 兼容旧/测试桩：拼好的字符串按第一个空格拆成 time + name
        text = current.strip()
        head, _, tail = text.partition(" ")
        current_struct = {"time": _fmt_dash(head), "name": tail.strip()}
    else:
        current_struct = None
    next_items = []
    for item in planning.get("next_items") or []:
        if not isinstance(item, dict):
            continue
        next_items.append({"time": _fmt_dash(str(item.get("time") or "")),
                           "name": str(item.get("name") or ""),
                           "status": str(item.get("status") or "待开始")})
        if len(next_items) >= 3:
            break
    summary = (f"今日 {_to_int(planning.get('item_count'))} 项 · "
               f"已完成 {_to_int(planning.get('done_count'))} · "
               f"待开始 {_to_int(planning.get('upcoming_count'))} · "
               f"约定 {_to_int(planning.get('commitment_count'))} 项")
    section_planning = {
        "num": "01", "title": "日程", "enabled": planning_enabled,
        "meta": (f"{_to_int(planning.get('item_count'))} 项在列" if planning_enabled else ""),
        "kind": "schedule",
        "current": current_struct,
        "next_items": next_items,
        "summary": summary,
    }

    # ── 右栏 02 陪伴 ───────────────────────────────────────────
    patrol = _dict(companion.get("patrol"))
    running = bool(patrol.get("running"))
    section_companion = {
        "num": "02", "title": "陪伴", "enabled": affection_enabled,
        "meta": ("运行中" if running else "未运行") if affection_enabled else "",
        "kind": "dl",
        "rows": [
            {"label": "今日主动", "segments": [
                (f"{_to_float(companion.get('speak_count')):g}/{_to_int(companion.get('daily_max'))}", INK),
                (f" · 今日 Δ {_to_float(companion.get('daily_net')):+.2f}", MUTED),
            ]},
            {"label": "问候", "segments": [
                ("早安 ", MUTED), (_bool_word(bool(companion.get("morning_sent"))),
                                  INK if companion.get("morning_sent") else MUTED),
                (" · 晚安 ", MUTED), (_bool_word(bool(companion.get("night_sent"))),
                                     INK if companion.get("night_sent") else MUTED),
                (" · 想念 ", MUTED), (_bool_word(bool(companion.get("miss_sent"))),
                                     INK if companion.get("miss_sent") else MUTED),
            ]},
            {"label": "巡检", "segments": [
                ("运行中" if running else "未运行", INK if running else MUTED),
                (f" · 间隔 {_to_int(patrol.get('interval_minutes'))} min"
                 f" · 上次 {_patrol_clock(patrol.get('last_tick_at'))}", MUTED),
            ]},
            {"label": "通道", "segments": [
                ("stream_id 已就绪" if patrol.get("stream_id_ready") else "stream_id 未解析",
                 INK if patrol.get("stream_id_ready") else MUTED),
                (" · 主动开关 " + ("开" if patrol.get("trigger_enabled") else "关"),
                 INK if patrol.get("trigger_enabled") else MUTED),
            ]},
        ],
    }

    # ── 右栏 03 情绪 ───────────────────────────────────────────
    anchor = _dict(emotion.get("anchor"))
    offset = _dict(emotion.get("offset"))
    quote_text = str(emotion.get("description") or "").strip() \
        or str(emotion.get("mood_text") or "").strip()
    # R35（2026-10-24 直派）：「近期情绪变化」行组——最近 3 条（上限 3）已入账事件，
    # 每条一行「HH:MM desc（V+0.08|A-0.05|D+0.12）」；desc 在测量遍经 render_kit.fit_text
    # 按像素截断（见 _measure_dl_rows 的 recent 分支），ΔVAD 段恒可见；空列表给「（无记录）」。
    recent_rows: List[Dict[str, Any]] = []
    for event in (emotion.get("recent_events") or []):
        if len(recent_rows) >= 3:
            break
        if not isinstance(event, dict):
            continue
        time_text = str(event.get("time") or "").strip()
        desc = str(event.get("desc") or "").strip() or "（无描述）"
        recent_rows.append({
            "label": "近期变化" if not recent_rows else "",
            "recent": True,
            "segments": [
                (f"{time_text} ", MUTED),
                (desc, INK),
                (_recent_delta_text(event), MUTED),
            ],
        })
    if not recent_rows:
        recent_rows.append({"label": "近期变化", "recent": False,
                            "segments": [("（无记录）", MUTED)]})
    section_emotion = {
        "num": "03", "title": "情绪", "enabled": emotion_enabled,
        "meta": (str(emotion.get("mode") or "").strip() or "—") if emotion_enabled else "",
        "kind": "dl",
        "rows": [
            {"label": "标签", "segments": [(label or "复杂难名", INK)]},
            {"label": "当日事件", "segments": [(f"{_to_int(emotion.get('event_count'))} 条", INK)]},
            {"label": "锚点", "segments": [(_fmt_vad(anchor), INK)]},
            {"label": "偏移", "segments": [(_fmt_vad(offset), INK)]},
        ] + recent_rows,
        "quote": quote_text,
    }

    # ── 右栏 04 空间 ───────────────────────────────────────────
    qzone_enabled = bool(qzone.get("enabled"))
    channels = [str(c) for c in (qzone.get("channels") or []) if str(c).strip()]
    cookie_parts = [part.strip() for part in str(qzone.get("cookie_text") or "").split("·")
                    if part.strip()]
    cookie_segments: List[Tuple[str, Tuple[int, int, int]]] = []
    for index, part in enumerate(cookie_parts):
        if index:
            cookie_segments.append((" · ", MUTED))
        cookie_segments.append((part, INK if index == 0 else MUTED))
    if not cookie_segments:
        cookie_segments = [("未知", MUTED)]
    baseline_count = len(qzone.get("comment_baseline") or [])
    section_qzone = {
        "num": "04", "title": "空间", "enabled": qzone_enabled,
        "meta": (f"{_to_int(qzone.get('posts_today'))}/{_to_int(qzone.get('daily_max_posts'))}"
                 if qzone_enabled else ""),
        "kind": "dl",
        "rows": [
            {"label": "今日说说", "segments": [
                (f"{_to_int(qzone.get('posts_today'))}/{_to_int(qzone.get('daily_max_posts'))}", INK),
                (" · 评论监听 " + ("开" if qzone.get("reply_comments_enabled") else "关")
                 + " · 配图 " + (str(qzone.get("image_mode") or "off")), MUTED),
            ]},
            {"label": "动作通道", "segments": [
                ("、".join(channels) if channels else "未连通", INK if channels else MUTED),
                (f" · 评论基线 {baseline_count} 条", MUTED),
            ]},
            {"label": "cookie", "segments": cookie_segments},
        ],
    }

    # ── 右栏 05 消耗 ───────────────────────────────────────────
    models = _dict(usage.get("models"))
    model_rows_all = sorted(
        ((str(name or "未知"), max(0, _to_int(count))) for name, count in models.items()),
        key=lambda kv: kv[1], reverse=True)
    model_rows = [{"name": name, "value": value, "value_text": f"{value:,}"}
                  for name, value in model_rows_all[:3]]
    model_overflow = len(model_rows_all) - len(model_rows)
    model_max = max((row["value"] for row in model_rows), default=0)
    for row in model_rows:
        row["frac"] = (row["value"] / model_max) if model_max else 0.0
    module_usage_rows = []
    order = list(_MODULE_KEYS)
    for name in modules:
        if name not in order:
            order.append(name)
    for name in order:
        if name not in modules:
            continue
        value = max(0, _to_int(modules.get(name)))
        module_usage_rows.append({"name": str(name), "value": value,
                                  "value_text": f"{value:,}"})
    module_max = max((row["value"] for row in module_usage_rows), default=0)
    for row in module_usage_rows:
        row["frac"] = (row["value"] / module_max) if module_max else 0.0
    section_usage = {
        "num": "05", "title": "消耗", "enabled": bool(usage.get("enabled", True)),
        "meta": "24h",
        "kind": "usage",
        "total_text": f"{tokens:,}",
        "sub_text": f"tokens · {calls} 次调用",
        "groups": [
            {"label": "按模型", "rows": model_rows,
             "note": "其余见 /lbm_mai_diag llm 1" if model_overflow > 0 else ""},
            {"label": "按模块", "rows": module_usage_rows, "note": ""},
        ],
    }

    return {
        "header": {"date": date_text, "weekday": weekday,
                   "title": f"状态总览 · {date_text} {weekday}"},
        "left": {
            "affection": affection,
            "emotion": emotion_left,
            "usage": usage_left,
            "relation": relation,
            "commands": commands,
        },
        "sections": [section_planning, section_companion, section_emotion,
                     section_qzone, section_usage],
        "footer": {"left": "属于麦麦的生活 v2.1.0", "right": "Powered by Mai-Bot"},
    }


# ---------------------------------------------------------------------------
# 测量遍 & 绘制遍（指导 §8.3）——所有坐标在这里算好，绘制只落笔
# ---------------------------------------------------------------------------


class _Painter:
    """收集绘制指令并记录文本最大右缘（供 198 限宽断言复用）。"""

    def __init__(self, probe: Canvas, loader: Any) -> None:
        self.probe = probe
        self.loader = loader
        self.items: List[Dict[str, Any]] = []
        self.max_text_right = 0.0

    def font(self, size: float, bold: bool = False) -> Any:
        return self.probe.font(self.loader, size, bold)

    def text(self, x: float, baseline: float, text: Any, size: float, *,
             bold: bool = False, color: Tuple[int, int, int] = INK,
             max_w: Optional[float] = None, tracking: float = 0.0,
             anchor: str = "ls") -> float:
        font = self.font(size, bold)
        value = str(text or "")
        if max_w is not None:
            value = render_kit.fit_text(self.probe, value, font, max_w)
        if not value:
            return 0.0
        width = (render_kit.tracked_width(self.probe, value, font, tracking)
                 if tracking else render_kit.text_width(self.probe, value, font))
        self.items.append({"kind": "text", "x": x, "baseline": baseline, "text": value,
                           "size": size, "bold": bold, "color": color,
                           "tracking": tracking, "anchor": anchor, "max_w": max_w})
        if anchor.startswith("r"):
            self.max_text_right = max(self.max_text_right, float(x))
        elif anchor.startswith("m"):
            self.max_text_right = max(self.max_text_right, float(x) + width / 2.0)
        else:
            self.max_text_right = max(self.max_text_right, float(x) + width)
        return width

    def rect(self, box: Sequence[float], *, fill: Optional[Tuple[int, int, int]] = None,
             outline: Optional[Tuple[int, int, int]] = None, width: float = 1,
             radius: float = 0) -> None:
        self.items.append({"kind": "rect", "box": list(box), "fill": fill,
                           "outline": outline, "width": width, "radius": radius})

    def line(self, box: Sequence[float], color: Tuple[int, int, int], width: float = 1) -> None:
        self.items.append({"kind": "line", "box": list(box), "color": color, "width": width})

    def ellipse(self, box: Sequence[float], *, fill: Optional[Tuple[int, int, int]] = None,
                outline: Optional[Tuple[int, int, int]] = None, width: float = 1) -> None:
        self.items.append({"kind": "ellipse", "box": list(box), "fill": fill,
                           "outline": outline, "width": width})


# 左栏固定节奏（指导 §7.3 / mockup 实测）
_LABEL_H = 23
_SCORE_H = 60
_SUB_H = 22
_BAR_H = 6
_BAR_LABEL_H = 16
_WORD_H = 48
_NUM_H = 48
_STACK_H = 8
_LEGEND_H = 18
_DL_ROW_H = 37
_QUOTE_LINE_H = 28
_NEXT_ROW_H = 36
_CMD_ROW_H = 29
_DISABLED_LINE_H = 24


def _shift_items(items: List[Dict[str, Any]], dy: float) -> List[Dict[str, Any]]:
    if not dy:
        return items
    shifted: List[Dict[str, Any]] = []
    for item in items:
        moved = dict(item)
        if item["kind"] == "text":
            moved["baseline"] = float(item["baseline"]) + dy
        else:
            box = list(item["box"])
            for i in range(0, len(box), 2):
                box[i + 1] = float(box[i + 1]) + dy
            moved["box"] = box
        shifted.append(moved)
    return shifted


def _measure_left(view: Dict[str, Any], probe: Canvas, loader: Any) -> Dict[str, Any]:
    """左栏 5 块（好感/情绪/消耗/关系温度/命令）——返回相对 y=0 的指令与高度。"""
    p = _Painter(probe, loader)
    left = view["left"]
    top = 0.0

    # ── 好感 ──
    aff = left["affection"]
    p.text(LEFT_X, top + probe.ascent(p.font(19, True)), "好感", 19, bold=True,
           color=RED, tracking=1.0, max_w=LEFT_W)
    score_top = top + _LABEL_H + 10
    if aff["enabled"]:
        p.text(LEFT_X, score_top + probe.ascent(p.font(60, True)), aff["score_text"], 60,
               bold=True, color=INK, tracking=-2.0, max_w=LEFT_W)
        sub_top = score_top + _SCORE_H + 8
        p.text(LEFT_X, sub_top + probe.ascent(p.font(16)), aff["tier_text"], 16,
               color=MUTED, max_w=LEFT_W)
        bar_top = sub_top + _SUB_H + 14
        p.rect([LEFT_X, bar_top, LEFT_X + LEFT_W, bar_top + _BAR_H], fill=TRACK)
        fill_w = LEFT_W * float(aff["fill_frac"])
        if fill_w > 0:
            p.rect([LEFT_X, bar_top, LEFT_X + fill_w, bar_top + _BAR_H], fill=RED)
        tick_x = LEFT_X + LEFT_W * float(aff["baseline_frac"])
        p.rect([tick_x - 1, bar_top - 4, tick_x + 1, bar_top + _BAR_H + 4], fill=INK)
        labels_top = bar_top + _BAR_H + 7
        p.text(LEFT_X, labels_top + probe.ascent(p.font(13)), aff["baseline_text"], 13,
               color=MUTED, max_w=LEFT_W / 2 - 6)
        p.text(LEFT_X + LEFT_W, labels_top + probe.ascent(p.font(13)), aff["daily_text"], 13,
               color=MUTED, max_w=LEFT_W / 2 - 6, anchor="rs")
        bottom = labels_top + _BAR_LABEL_H
    else:
        p.text(LEFT_X, score_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16, color=MUTED)
        bottom = score_top + _DISABLED_LINE_H

    # ── 情绪 ──
    emo = left["emotion"]
    top = bottom + 32
    p.line([LEFT_X, top, LEFT_X + LEFT_W, top], HAIR, 1)
    content = top + 27
    p.text(LEFT_X, content + probe.ascent(p.font(19, True)), "情绪", 19, bold=True,
           color=RED, tracking=1.0, max_w=LEFT_W)
    word_top = content + _LABEL_H + 10
    if emo["enabled"]:
        share_w = (render_kit.text_width(probe, emo["share_text"], p.font(18, True))
                   if emo["share_text"] else 0.0)
        word_max = LEFT_W - (share_w + 10 if share_w else 0)
        word_w = p.text(LEFT_X, word_top + probe.ascent(p.font(44, True)), emo["word"], 44,
                        bold=True, color=INK, max_w=word_max)
        if emo["share_text"]:
            p.text(LEFT_X + word_w + 10,
                   word_top + probe.ascent(p.font(44, True)), emo["share_text"], 18,
                   bold=True, color=RED, max_w=share_w + 1)
        sub_top = word_top + _WORD_H + 8
        p.text(LEFT_X, sub_top + probe.ascent(p.font(15)), emo["coarse_text"], 15,
               color=MUTED, max_w=LEFT_W)
        bottom = sub_top + 24
    else:
        p.text(LEFT_X, word_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16, color=MUTED)
        bottom = word_top + _DISABLED_LINE_H

    # ── 24h 消耗 ──
    usg = left["usage"]
    top = bottom + 32
    p.line([LEFT_X, top, LEFT_X + LEFT_W, top], HAIR, 1)
    content = top + 27
    p.text(LEFT_X, content + probe.ascent(p.font(19, True)), "24h 消耗", 19, bold=True,
           color=RED, tracking=1.0, max_w=LEFT_W)
    num_top = content + _LABEL_H + 10
    if usg["enabled"]:
        p.text(LEFT_X, num_top + probe.ascent(p.font(46, True)), usg["number_text"], 46,
               bold=True, color=INK, tracking=-1.0, max_w=LEFT_W)
        sub_top = num_top + _NUM_H + 8
        p.text(LEFT_X, sub_top + probe.ascent(p.font(16)), usg["sub_text"], 16,
               color=MUTED, max_w=LEFT_W)
        stack_top = sub_top + _SUB_H + 14
        p.rect([LEFT_X, stack_top, LEFT_X + LEFT_W, stack_top + _STACK_H], fill=TRACK)
        x = LEFT_X
        for row in usg["modules"]:
            width = LEFT_W * float(row["frac"])
            if width > 0:
                p.rect([x, stack_top, x + width, stack_top + _STACK_H], fill=row["color"])
                x += width
        legend_top = stack_top + _STACK_H + 9
        for index, row in enumerate(usg["modules"]):
            col = index % 2
            line_no = index // 2
            lx = LEFT_X + col * (LEFT_W / 2)
            ly = legend_top + line_no * _LEGEND_H
            p.rect([lx, ly + 4, lx + 8, ly + 12], fill=row["color"])
            p.text(lx + 14, ly + probe.ascent(p.font(12)),
                   f"{row['label']} {row['compact']}", 12, color=MUTED,
                   max_w=LEFT_W / 2 - 18)
        bottom = legend_top + 2 * _LEGEND_H
    else:
        p.text(LEFT_X, num_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16, color=MUTED)
        bottom = num_top + _DISABLED_LINE_H

    # ── 关系温度 ──
    rel = left["relation"]
    top = bottom + 32
    p.line([LEFT_X, top, LEFT_X + LEFT_W, top], HAIR, 1)
    content = top + 27
    p.text(LEFT_X, content + probe.ascent(p.font(19, True)), "关系温度", 19, bold=True,
           color=RED, tracking=1.0, max_w=LEFT_W)
    date_top = content + _LABEL_H + 10
    if rel["enabled"]:
        p.text(LEFT_X, date_top + probe.ascent(p.font(12)), rel["date"] or "（暂无日期）", 12,
               color=FAINT, tracking=1.0, max_w=LEFT_W)
        quote_top = date_top + 14 + 6
        quote_lines = (render_kit.wrap_text(probe, rel["quote"], p.font(17), LEFT_W, 2)
                       if rel["quote"] else ["（暂无关系温度）"])
        for index, line in enumerate(quote_lines):
            p.text(LEFT_X, quote_top + probe.ascent(p.font(17)) + index * 28, line, 17,
                   color=INK if rel["quote"] else MUTED, max_w=LEFT_W)
        imp_top = quote_top + len(quote_lines) * 28 + 10
        imp_lines = (render_kit.wrap_text(probe, rel["impression"], p.font(12.5), LEFT_W - 56, 2)
                     if rel["impression"] else [])
        if imp_lines:
            for index, line in enumerate(imp_lines):
                if index == 0:
                    prefix_w = p.text(LEFT_X, imp_top + probe.ascent(p.font(12.5)),
                                      "最近印象", 12.5, color=FAINT)
                    p.text(LEFT_X + prefix_w + 4,
                           imp_top + probe.ascent(p.font(12.5)), line, 12.5,
                           color=MUTED, max_w=LEFT_W - prefix_w - 4)
                else:
                    p.text(LEFT_X, imp_top + probe.ascent(p.font(12.5)) + index * 20,
                           line, 12.5, color=MUTED, max_w=LEFT_W)
            bottom = imp_top + len(imp_lines) * 20
        else:
            p.text(LEFT_X, imp_top + probe.ascent(p.font(12.5)), "最近印象 （无）", 12.5,
                   color=FAINT, max_w=LEFT_W)
            bottom = imp_top + 20
    else:
        p.text(LEFT_X, date_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16, color=MUTED)
        bottom = date_top + _DISABLED_LINE_H

    # ── 命令面板（唯一命令区）──
    top = bottom + 32
    p.line([LEFT_X, top, LEFT_X + LEFT_W, top], HAIR, 1)
    content = top + 24 + 1
    p.text(LEFT_X, content + probe.ascent(p.font(19, True)), "命令", 19, bold=True,
           color=RED, tracking=1.0, max_w=LEFT_W)
    row_top = content + _LABEL_H + 6
    for index, row in enumerate(left["commands"]):
        baseline = row_top + probe.ascent(p.font(12, True))
        p.text(LEFT_X, baseline, f"{row['num']} {row['title']}", 12, bold=True,
               color=INK, max_w=52)
        cmd_x = LEFT_X + 59
        cmd_max = LEFT_W - 59
        cmd_lines = _wrap_at_spaces(probe, row["command"], p.font(12), cmd_max, 2)
        for line_no, line in enumerate(cmd_lines):
            p.text(cmd_x, baseline + line_no * 17, line, 12, color=MUTED, max_w=cmd_max)
        row_h = _CMD_ROW_H + max(0, len(cmd_lines) - 1) * 17
        p.line([LEFT_X, row_top + row_h - 1, LEFT_X + LEFT_W, row_top + row_h - 1],
               ROWLINE, 1)
        row_top += row_h
    bottom = row_top

    return {"items": p.items, "height": bottom, "max_text_right": p.max_text_right}


def _wrap_at_spaces(probe: Canvas, text: str, font: Any, max_w: float,
                    max_lines: int) -> List[str]:
    """命令面板换行（指导 §7.3：在空格处换行，不省略号）。"""
    value = str(text or "").strip()
    if not value or render_kit.text_width(probe, value, font) <= max_w:
        return [value] if value else [""]
    words = value.split(" ")
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if current and render_kit.text_width(probe, candidate, font) > max_w:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    # 兜底：超长单词（命令常量不会触发）按像素截断，避免越界
    return [render_kit.fit_text(probe, line, font, max_w) if
            render_kit.text_width(probe, line, font) > max_w else line
            for line in (lines or [value])]


def _measure_dl_rows(p: _Painter, probe: Canvas, rows: List[Dict[str, Any]],
                     content_top: float) -> float:
    """02/03/04 定义行：label 72 + 12 gap；行高 37；1px rowline（末行无）。

    R35：``row["recent"]`` 行（03 情绪「近期变化」行组）三段绘制——时刻段 + desc 段 +
    ΔVAD 段；desc 段先按「剩余宽 − ΔVAD 段宽 − 8」经 ``render_kit.fit_text`` 截断，
    保证 ΔVAD 段恒可见（禁止字符数截断，一律像素测量）。
    """
    row_top = content_top + 12
    for index, row in enumerate(rows):
        baseline = row_top + 6 + probe.ascent(p.font(16))
        p.text(RIGHT_X, baseline, row["label"], 13, color=MUTED, max_w=72)
        x = RIGHT_X + 84
        remaining = RIGHT_W - 84
        segments = list(row["segments"])
        if row.get("recent") and len(segments) == 3:
            (time_text, time_color), (desc_text, desc_color), (delta_text, delta_color) = segments
            delta_w = render_kit.text_width(probe, delta_text, p.font(16))
            time_w = p.text(x, baseline, time_text, 16, color=time_color, max_w=remaining)
            x += time_w
            remaining -= time_w
            desc_max = max(10.0, remaining - delta_w - 8)
            desc_w = p.text(x, baseline, desc_text, 16, color=desc_color, max_w=desc_max)
            x += desc_w
            remaining -= desc_w
            p.text(x + 8, baseline, delta_text, 16, color=delta_color,
                   max_w=max(10.0, remaining))
        else:
            for text, color in segments:
                if remaining <= 0:
                    break
                width = p.text(x, baseline, text, 16, color=color, max_w=remaining)
                used = (render_kit.text_width(probe, str(text or ""), p.font(16)))
                x += width
                remaining -= width
                if width < used:  # 已省略 → 后续段不再画
                    break
        if index < len(rows) - 1:
            p.line([RIGHT_X, row_top + _DL_ROW_H - 1, TEXT_RIGHT, row_top + _DL_ROW_H - 1],
                   ROWLINE, 1)
        row_top += _DL_ROW_H
    return row_top - 1


def _measure_section(view_section: Dict[str, Any], probe: Canvas,
                     loader: Any) -> Dict[str, Any]:
    """测量单节（top=0）：节头 + 内容；返回相对坐标指令与内容高度。"""
    p = _Painter(probe, loader)
    num = view_section["num"]
    title = view_section["title"]
    enabled = bool(view_section["enabled"])
    head_baseline = probe.ascent(p.font(25, True))
    p.text(RIGHT_X, head_baseline, num, 19, bold=True, color=RED, max_w=28)
    p.text(RIGHT_X + 40, head_baseline, title, 25, bold=True, color=INK,
           tracking=1.0, max_w=RIGHT_W - 40 - 120)
    if view_section.get("meta"):
        p.text(TEXT_RIGHT, head_baseline, view_section["meta"], 14, color=MUTED,
               max_w=150, anchor="rs")
    content_top = 30 + 13

    if not enabled:
        p.text(RIGHT_X, content_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16,
               color=MUTED, max_w=RIGHT_W)
        content_bottom = content_top + _DISABLED_LINE_H
        return {"items": p.items, "content_h": content_bottom,
                "max_text_right": p.max_text_right}

    kind = view_section["kind"]
    if kind == "schedule":
        band_top = content_top
        band_h = 45
        current = view_section.get("current")
        if current:
            p.rect([RIGHT_X, band_top, TEXT_RIGHT, band_top + band_h], fill=TINT)
            p.rect([RIGHT_X, band_top, RIGHT_X + 4, band_top + band_h], fill=RED)
            baseline = band_top + 10 + probe.ascent(p.font(21, True))
            x = RIGHT_X + 14
            tag_w = p.text(x, baseline, "进行中", 12, bold=True, color=RED,
                           tracking=1.0, max_w=80)
            x += tag_w + 12
            time_w = p.text(x, baseline, current["time"], 16, bold=True, color=RED,
                            max_w=180)
            x += time_w + 12
            p.text(x, baseline, current["name"], 21, bold=True, color=INK,
                   max_w=max(10, TEXT_RIGHT - 14 - x))
        else:
            p.text(RIGHT_X + 14, band_top + 10 + probe.ascent(p.font(16)),
                   "当前活动 无", 16, color=MUTED, max_w=RIGHT_W - 28)
        next_top = band_top + band_h
        for item in view_section.get("next_items") or []:
            baseline = next_top + 6 + probe.ascent(p.font(16))
            p.text(RIGHT_X, baseline, item["time"], 14, color=MUTED, max_w=122)
            p.text(RIGHT_X + 122 + 12, baseline, item["name"], 16, color=INK,
                   max_w=RIGHT_W - 122 - 12 - 58 - 12)
            p.text(TEXT_RIGHT, baseline, item["status"], 12, color=FAINT,
                   max_w=58, anchor="rs")
            p.line([RIGHT_X, next_top + _NEXT_ROW_H - 1, TEXT_RIGHT,
                    next_top + _NEXT_ROW_H - 1], ROWLINE, 1)
            next_top += _NEXT_ROW_H
        summary_baseline = next_top + 10 + probe.ascent(p.font(13))
        p.text(RIGHT_X, summary_baseline, view_section["summary"], 13, color=MUTED,
               max_w=RIGHT_W)
        content_bottom = next_top + 10 + 18
    elif kind == "dl":
        rows_bottom = _measure_dl_rows(p, probe, view_section.get("rows") or [], content_top)
        quote = str(view_section.get("quote") or "").strip()
        if quote:
            quote_top = rows_bottom + 12
            lines = render_kit.wrap_text(probe, quote, p.font(16), 560, _QUOTE_MAX_LINES)
            p.rect([RIGHT_X, quote_top + 2, RIGHT_X + 2,
                    quote_top + len(lines) * _QUOTE_LINE_H - 4], fill=RED)
            for index, line in enumerate(lines):
                p.text(RIGHT_X + 14,
                       quote_top + probe.ascent(p.font(16)) + index * _QUOTE_LINE_H,
                       line, 16, color=INK_SOFT, max_w=560)
            content_bottom = quote_top + len(lines) * _QUOTE_LINE_H
        else:
            content_bottom = rows_bottom
    elif kind == "usage":
        total_top = content_top + 12
        baseline = total_top + probe.ascent(p.font(26, True))
        total_w = p.text(RIGHT_X, baseline, view_section["total_text"], 26, bold=True,
                         color=INK, tracking=-0.5, max_w=RIGHT_W - 200)
        p.text(RIGHT_X + total_w + 10, baseline, view_section["sub_text"], 13,
               color=MUTED, max_w=RIGHT_W - total_w - 10)
        groups_top = total_top + 29 + 12
        group_w = (RIGHT_W - 29) // 2
        group_bottoms: List[float] = []
        for group_index, group in enumerate(view_section.get("groups") or []):
            gx = RIGHT_X + group_index * (group_w + 29)
            p.text(gx, groups_top + probe.ascent(p.font(11, True)), group["label"], 11,
                   color=FAINT, tracking=2.0, max_w=group_w)
            rows_top = groups_top + 17
            rows = group.get("rows") or []
            for row_index, row in enumerate(rows):
                row_top = rows_top + row_index * 29
                baseline = row_top + probe.ascent(p.font(12))
                value_w = render_kit.text_width(probe, row["value_text"], p.font(12))
                p.text(gx + group_w, baseline, row["value_text"], 12, color=MUTED,
                       max_w=group_w, anchor="rs")
                p.text(gx, baseline, row["name"], 12, color=INK,
                       max_w=max(10, group_w - value_w - 8))
                bar_w = group_w * float(row["frac"])
                p.rect([gx, row_top + 17, gx + group_w, row_top + 21], fill=(230, 227, 219))
                if bar_w > 0:
                    p.rect([gx, row_top + 17, gx + bar_w, row_top + 21], fill=INK)
            bottom = rows_top + len(rows) * 29
            note = str(group.get("note") or "").strip()
            if note:
                p.text(gx, bottom + probe.ascent(p.font(12)), note, 12, color=FAINT,
                       max_w=group_w)
                bottom += 18
            if not rows and not note:
                p.text(gx, rows_top + probe.ascent(p.font(12)), "（无调用）", 12,
                       color=FAINT, max_w=group_w)
                bottom = rows_top + 18
            group_bottoms.append(bottom)
        content_bottom = max(group_bottoms, default=groups_top) + 4
    else:  # pragma: no cover - 防御：未知 kind 退化为「未启用」
        p.text(RIGHT_X, content_top + probe.ascent(p.font(16)), _DISABLED_LINE, 16,
               color=MUTED, max_w=RIGHT_W)
        content_bottom = content_top + _DISABLED_LINE_H

    return {"items": p.items, "content_h": content_bottom,
            "max_text_right": p.max_text_right}


def _sections_page_height(sections: List[Dict[str, Any]]) -> float:
    """一页右栏节栈的总高（含 padding/border/margin）。"""
    total = 0.0
    for index, section in enumerate(sections):
        total += section["content_h"] + 22
        if index < len(sections) - 1:
            total += 1 + 22
    return total


def _paginate_sections(sections: List[Dict[str, Any]], left_h: float) -> List[List[Dict[str, Any]]]:
    """超 4096 时按右栏节分页（正常数据恒 1 页；提示 §8.3「先分页，不钳内容」）。"""
    budget = _MAX_HEIGHT - HEADER_H - 36 - FOOTER_GAP - FOOTER_H
    pages: List[List[Dict[str, Any]]] = []
    remaining = list(sections)
    first = True
    while remaining:
        base = left_h if first else 0.0
        take = 0
        for index in range(1, len(remaining) + 1):
            candidate = _sections_page_height(remaining[:index])
            if max(base, candidate) > budget and take > 0:
                break
            take = index
        pages.append(remaining[:take])
        remaining = remaining[take:]
        first = False
    return pages or [[]]


def _measure_dashboard(view: Dict[str, Any], Image: Any, ImageDraw: Any,
                       ImageFont: Any, loader: Any) -> Dict[str, Any]:
    """测量遍：左栏 + 每节 + 分页；返回页面布局列表（含绘制指令、高度与限宽记录）。"""
    probe = Canvas(Image, ImageDraw, PAGE_WIDTH, 1, scale=1)
    left_layout = _measure_left(view, probe, loader)
    section_layouts = [_measure_section(section, probe, loader)
                       for section in view.get("sections") or []]
    pages = _paginate_sections(section_layouts, float(left_layout["height"]))

    layouts: List[Dict[str, Any]] = []
    total_pages = len(pages)
    for page_index, page_sections in enumerate(pages, 1):
        p = _Painter(probe, loader)
        # 页眉
        p.ellipse([MARGIN_X, 34, MARGIN_X + 14, 48], fill=RED)
        p.text(MARGIN_X + 28, 55, "MAI · STATE", 19, bold=True, color=INK, tracking=6.0)
        p.text(TEXT_RIGHT, 55, view["header"]["title"], 18, bold=True, color=INK,
               tracking=0.5, max_w=CONTENT_W - 200, anchor="rs")
        p.line([MARGIN_X, HEADER_H - 2, TEXT_RIGHT, HEADER_H - 2], INK, 2)

        left_bottom = 0.0
        if page_index == 1:
            for item in _shift_items(left_layout["items"], BODY_TOP):
                p.items.append(item)
            left_bottom = BODY_TOP + float(left_layout["height"])

        section_top = float(BODY_TOP)
        for index, section in enumerate(page_sections):
            for item in _shift_items(section["items"], section_top):
                p.items.append(item)
            hair_y = section_top + section["content_h"] + 22
            if index < len(page_sections) - 1:
                p.line([RIGHT_X, hair_y, TEXT_RIGHT, hair_y], HAIR, 1)
                section_top = hair_y + 1 + 22
            else:
                section_top = hair_y
        right_bottom = section_top

        body_bottom = max(left_bottom, right_bottom) if page_index == 1 else right_bottom
        if page_index == 1:
            p.line([DIVIDER_X, DIVIDER_TOP, DIVIDER_X, body_bottom], HAIR, 1)

        footer_top = body_bottom + FOOTER_GAP
        p.line([MARGIN_X, footer_top, TEXT_RIGHT, footer_top], INK, 2)
        baseline = footer_top + 14 + probe.ascent(p.font(14))
        left_footer = view["footer"]["left"]
        if total_pages > 1:
            left_footer += f" · {page_index}/{total_pages}"
        p.text(MARGIN_X, baseline, left_footer, 14, color=MUTED, max_w=CONTENT_W / 2)
        p.text(TEXT_RIGHT, baseline, view["footer"]["right"], 14, color=MUTED,
               max_w=CONTENT_W / 2, anchor="rs")

        layouts.append({
            "items": p.items,
            "height": int(round(footer_top + FOOTER_H)),
            "body_bottom": body_bottom,
            "footer_top": footer_top,
            "page_no": page_index,
            "page_count": total_pages,
            "max_text_right": p.max_text_right,
            "left_max_text_right": (float(left_layout["max_text_right"])
                                    if page_index == 1 else 0.0),
            "left_bottom": left_bottom,
            "right_bottom": right_bottom,
        })
    return {"pages": layouts}


def _draw_dashboard(canvas: Canvas, layout: Dict[str, Any], loader: Any) -> None:
    """绘制遍：只按测量结果落笔（文本再次按 max_w 在目标倍率下 fit）。"""
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
            if item.get("radius"):
                canvas.rounded_rectangle(item["box"], radius=item["radius"],
                                         fill=item.get("fill"), outline=item.get("outline"),
                                         width=item.get("width", 1))
            else:
                canvas.rectangle(item["box"], fill=item.get("fill"),
                                 outline=item.get("outline"), width=item.get("width", 1))
        elif kind == "line":
            canvas.line(item["box"], item["color"], item.get("width", 1))
        elif kind == "ellipse":
            canvas.ellipse(item["box"], fill=item.get("fill"),
                           outline=item.get("outline"), width=item.get("width", 1))


# ---------------------------------------------------------------------------
# 状态图渲染入口（签名/返回/异常契约保持不变）
# ---------------------------------------------------------------------------


def render_dashboard_image(title: str, blocks: Any, *, output_dir: Optional[Path] = None,
                           width: int = _DEFAULT_WIDTH) -> Tuple[str, str]:
    """渲染聚合状态总览图并保存 ``dashboard_today.jpg``（指导 §7 版式）。

    版式（R7 / 计划书 §三十）：纸底/墨字/单红强调；左栏 280 KPI 轨 + 命令面板（唯一命令区），
    竖线 x=379，右栏 01~05 五节；先量后画；footer 由内容底反推；超限按节分页
    （``dashboard_today_p2.jpg`` 等，返回值恒为第一页）。

    Args:
        output_dir: 图片输出目录（宿主隔离数据目录；None=系统临时目录，绝不写插件目录）。

    Returns:
        (第一页图片路径, 第一页 base64)。

    Raises:
        RuntimeError: pillow/字体/渲染任一步失败（调用方静默降级为纯文本）。
    """
    try:
        from planning.schedule_image import ScheduleImageGenerator as _SIG
        Image, ImageDraw, ImageFont = _SIG._require_pil()
        loader = _SIG._font_loader(ImageFont)
    except RuntimeError:
        raise
    except Exception as exc:  # 渲染链导入失败等同「渲染不可用」
        raise RuntimeError(f"聚合状态图渲染链不可用: {exc}") from exc

    if width and int(width) != _DEFAULT_WIDTH:
        logger.info("状态总览版式固定 %s 宽（指导 §5.3），忽略 width=%s",
                    _DEFAULT_WIDTH, width)

    try:
        view = build_dashboard_view(blocks)
        measured = _measure_dashboard(view, Image, ImageDraw, ImageFont, loader)
        save_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
        save_dir.mkdir(parents=True, exist_ok=True)

        first_path: Optional[Path] = None
        first_b64 = ""
        for layout in measured["pages"]:
            scale = render_kit.supersample_scale(layout["height"])
            canvas = Canvas(Image, ImageDraw, PAGE_WIDTH, layout["height"], scale=scale,
                            background=PAPER)
            _draw_dashboard(canvas, layout, loader)
            img = canvas.finish()
            if layout["page_no"] == 1:
                image_path = save_dir / "dashboard_today.jpg"
            else:
                image_path = save_dir / f"dashboard_today_p{layout['page_no']}.jpg"
            img.save(str(image_path), format="JPEG", quality=95, subsampling=0,
                     optimize=True)
            if first_path is None:
                first_path = image_path
                buffer = io.BytesIO()
                img.save(buffer, format="JPEG", quality=95, subsampling=0, optimize=True)
                first_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
        assert first_path is not None
        return str(first_path), first_b64
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"聚合状态图渲染失败: {exc}") from exc
