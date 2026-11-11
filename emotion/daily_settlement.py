"""每日情绪兜底结算的纯函数层（3.17.33，用户直派）。

白天评估配额（默认总 10 = 私聊 6 / 群聊 4）可能被用光，导致当天没有最终情绪结算。
本模块只承载「输入压缩 / 日期判定 / 规则兜底 / 持久日期去重」四件无副作用（或仅
自管状态文件）的事；配额预占、LLM 调用与 VAD 入账仍由 :mod:`emotion.service` 负责。

设计红线：
- **只读** ``EmotionObservations`` 的 VAD 采样点（``at``/``valence``/``arousal``/
  ``dominance``/``frozen``），绝不读取聊天原文；
- 摘要压缩到 ``daily_settlement_max_chars``（默认 800）以内，超限截断；
- 日期去重状态写在 ``emotion_daily_settlement.json``（与 ``emotion_state.json`` 同目录，
  独立文件——不改状态机/state_store 的既有文档结构）。

本模块不 import service / planning / companion，service 侧构造实例即可直测。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.storage import load_json, save_json_atomic

from .vad import VADState, _clip

#: 出厂默认（与 ``core.internal_tuning.INTERNAL_TUNING_DEFAULTS["emotion"]`` 同源口径；
#: 内部调参表缺键时也回退这三项，故「缺键默认开启」在两条读取链上都成立）
DEFAULT_ENABLED = True
DEFAULT_MINUTE = 23 * 60 + 50   # 1430 = 23:50
DEFAULT_MAX_CHARS = 800

#: 摘要长度硬约束（用户口径 ≤800；允许现场调小，但过小会失去可读性，下限 100）
MIN_MAX_CHARS = 100
MAX_MAX_CHARS = 4000

#: 摘要里保留的最近采样点个数（用户口径：最近 3 个点）
RECENT_POINT_COUNT = 3

#: 规则兜底：有当天采样 → 当前 VAD 向当天均值混合 0.3
RULE_BLEND_WITH_POINTS = 0.3
#: 规则兜底：无采样 → 向人设锚点（缺锚点则 0）衰减一小步 0.1
RULE_BLEND_NO_POINTS = 0.1

#: 日期去重状态文件名（与 emotion_state.json 同目录）
STATE_FILE_NAME = "emotion_daily_settlement.json"
STATE_VERSION = 1


# ── 内部调参 ────────────────────────────────────────────────
def settings(root_dir: Any = None) -> Dict[str, Any]:
    """读三项内部调参（``mappings/internal_tuning.json[emotion]``）。

    任何读取异常/坏值都回退出厂默认（开启 / 1430 / 800），绝不让调参故障拖挂结算。
    返回 ``{"enabled": bool, "minute": int, "max_chars": int}``。
    """
    enabled, minute, max_chars = DEFAULT_ENABLED, DEFAULT_MINUTE, DEFAULT_MAX_CHARS
    try:
        from core.internal_tuning import tuning_value
        raw_enabled = tuning_value("emotion", "daily_settlement_enabled", root_dir=root_dir)
        if isinstance(raw_enabled, bool):
            enabled = raw_enabled
        raw_minute = tuning_value("emotion", "daily_settlement_minute", root_dir=root_dir)
        if isinstance(raw_minute, int) and not isinstance(raw_minute, bool):
            minute = raw_minute
        raw_chars = tuning_value("emotion", "daily_settlement_max_chars", root_dir=root_dir)
        if isinstance(raw_chars, int) and not isinstance(raw_chars, bool):
            max_chars = raw_chars
    except Exception:
        pass
    return {"enabled": enabled, "minute": clamp_minute(minute),
            "max_chars": clamp_max_chars(max_chars)}


def clamp_minute(value: Any) -> int:
    """当日第几分钟夹到 [0, 1439]；非法 → 1430（23:50）。"""
    if isinstance(value, bool):
        return DEFAULT_MINUTE
    try:
        minute = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MINUTE
    return max(0, min(24 * 60 - 1, minute))


def clamp_max_chars(value: Any) -> int:
    """摘要字符预算夹到 [100, 4000]；非法 → 800。"""
    if isinstance(value, bool):
        return DEFAULT_MAX_CHARS
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_CHARS
    return max(MIN_MAX_CHARS, min(MAX_MAX_CHARS, limit))


# ── 日期判定（每日一次 + 跨日补结算）──────────────────────────
def _valid_date_key(value: Any) -> str:
    """``YYYY-MM-DD`` 合法则原样返回，否则空串（去重状态坏值不阻塞结算）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def resolve_target(now: datetime, minute: Any,
                   last_date: Any) -> Optional[Tuple[str, bool]]:
    """判定本次 tick 该结算哪一天，返回 ``(date_key, catch_up)``；无需结算 → None。

    规则（用户口径，可微调但语义固定）：
    - 到点（``now`` 的当日分钟 ≥ ``minute``）且今天未结算 → 结算今天，``catch_up=False``；
    - 未到点、且**上一日**未结算（离线/重启/冻结顺延）→ 补结算上一日，``catch_up=True``；
    - 到点但上一日也漏了 → 先补上一日（``catch_up=True``），今天等下一拍——保证每天
      至多一次 LLM、每拍只处理一个日期键；
    - ``last_date`` 为空（首次启用/状态文件丢失）由 service 先写 bootstrap 日期并跳过，
      本函数不补历史；次日 23:50 起正常结算；
    - 未来日期（时钟回拨）不重复结算，按「今天已结算」处理。

    日期比较用 ISO 字符串（``YYYY-MM-DD`` 字典序即时间序）。
    """
    if now is None:
        return None
    minute = clamp_minute(minute)
    today = now.date()
    today_key = today.isoformat()
    yesterday_key = (today - timedelta(days=1)).isoformat()
    last = _valid_date_key(last_date)
    if last >= today_key:
        return None
    due = (now.hour * 60 + now.minute) >= minute
    if due:
        if last < yesterday_key:
            return yesterday_key, True
        return today_key, False
    if last < yesterday_key:
        return yesterday_key, True
    return None


# ── 采样点读取与压缩 ────────────────────────────────────────
def _parse_point_time(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError):
        return None


def point_date_key(value: Any) -> str:
    """采样点 ``at`` → 其自身时区下的 ``YYYY-MM-DD``；解析失败 → 空串。

    采样点由 ``EmotionObservations.sample`` 以配置时区（默认 Asia/Shanghai）的
    aware datetime 写入；``datetime.date()`` 即该时区的日历日。
    """
    stamp = _parse_point_time(value)
    return stamp.date().isoformat() if stamp is not None else ""


def _axis(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return _clip(number)


def points_for_date(points: Any, date_key: str) -> List[Dict[str, Any]]:
    """从观测点序列中筛出目标日并规范化（坏行/坏值跳过；不抛异常）。

    返回 ``[{"at","valence","arousal","dominance","frozen"}, ...]``（按原顺序）。
    """
    target = str(date_key or "")
    rows: List[Dict[str, Any]] = []
    if not target:
        return rows
    for row in list(points or []):
        if not isinstance(row, dict):
            continue
        stamp = str(row.get("at") or "")
        if point_date_key(stamp) != target:
            continue
        valence = _axis(row.get("valence"))
        arousal = _axis(row.get("arousal"))
        dominance = _axis(row.get("dominance"))
        if valence is None or arousal is None or dominance is None:
            continue
        rows.append({"at": stamp, "valence": valence, "arousal": arousal,
                     "dominance": dominance, "frozen": bool(row.get("frozen"))})
    return rows


def _opt_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _quota_text(quota: Any) -> str:
    """配额行（total / private / group 与两种 cap 跳过次数）；全缺 → 空串。"""
    if not isinstance(quota, dict):
        return ""
    parts: List[str] = []
    total = _opt_int(quota.get("total"))
    total_cap = _opt_int(quota.get("total_cap"))
    if total is not None:
        parts.append(f"总 {total}" + (f"/{total_cap}" if total_cap is not None else ""))
    elif total_cap is not None:
        parts.append(f"总上限 {total_cap}")
    skipped_total = _opt_int(quota.get("skipped_total"))
    if skipped_total is not None:
        detail: List[str] = []
        skipped_private = _opt_int(quota.get("skipped_private"))
        skipped_group = _opt_int(quota.get("skipped_group"))
        if skipped_private:
            detail.append(f"私聊 {skipped_private}")
        if skipped_group:
            detail.append(f"群聊 {skipped_group}")
        suffix = f"（{'、'.join(detail)}）" if detail else ""
        parts.append(f"跳过 {skipped_total} 次{suffix}")
    for label, used_key, cap_key in (("私聊", "private", "private_cap"),
                                     ("群聊", "group", "group_cap")):
        used = _opt_int(quota.get(used_key))
        cap = _opt_int(quota.get(cap_key))
        if used is not None:
            parts.append(f"{label} {used}" + (f"/{cap}" if cap is not None else ""))
        elif cap is not None:
            parts.append(f"{label}上限 {cap}")
    return "配额：" + "；".join(parts) if parts else ""


def _fit_segments(segments: Sequence[str], limit: int) -> str:
    """按优先级拼接段落并截断到 ``limit`` 字（段落之间一个换行）。"""
    text = ""
    for segment in segments:
        segment = str(segment or "")
        if not segment:
            continue
        candidate = segment if not text else text + "\n" + segment
        if len(candidate) <= limit:
            text = candidate
            continue
        remaining = limit - len(text) - (1 if text else 0)
        if remaining > 0:
            text = (text + "\n" if text else "") + segment[:remaining]
        break
    return text[:limit]


def summarize_points(rows: Any, *, date_key: str, max_chars: Any = DEFAULT_MAX_CHARS,
                     quota: Any = None) -> Dict[str, Any]:
    """把目标日的采样点压缩成 ≤ ``max_chars`` 的摘要（供 LLM 与决策日志共用）。

    摘要包含：点数 / 冻结占比 / V·A·D 均值·最低·最高 / 最近 3 个点 / 配额使用与
    ``evaluate_daily_cap``·``evaluate_partition_cap`` 跳过次数（取不到则省略配额行）。
    **不含任何聊天原文**。

    Returns:
        ``{"text", "count", "frozen_count", "frozen_ratio", "mean" (VADState|None),
        "recent": [...], "stats": {...}}``；``rows`` 应已由 :func:`points_for_date`
        筛成目标日（本函数仍做逐行坏值防御）。
    """
    limit = clamp_max_chars(max_chars)
    clean: List[Dict[str, Any]] = []
    for row in list(rows or []):
        if not isinstance(row, dict):
            continue
        valence = _axis(row.get("valence"))
        arousal = _axis(row.get("arousal"))
        dominance = _axis(row.get("dominance"))
        if valence is None or arousal is None or dominance is None:
            continue
        clean.append({"at": str(row.get("at") or ""), "valence": valence,
                      "arousal": arousal, "dominance": dominance,
                      "frozen": bool(row.get("frozen"))})
    count = len(clean)
    frozen_count = sum(1 for row in clean if row["frozen"])
    frozen_ratio = (frozen_count / count) if count else 0.0
    stats: Dict[str, Any] = {"count": count, "frozen_count": frozen_count,
                             "frozen_ratio": round(frozen_ratio, 4), "axes": {}}
    mean: Optional[VADState] = None
    if count:
        for axis in ("valence", "arousal", "dominance"):
            values = [row[axis] for row in clean]
            stats["axes"][axis] = {
                "mean": round(sum(values) / count, 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
            }
        mean = VADState(stats["axes"]["valence"]["mean"],
                        stats["axes"]["arousal"]["mean"],
                        stats["axes"]["dominance"]["mean"])

    quota_text = _quota_text(quota)
    header = f"【{date_key} 情绪采样摘要】点数={count}；冻结={frozen_count}/{count}" \
             f"（{frozen_ratio * 100:.0f}%）"
    if count:
        axis_parts = []
        for label, axis in (("V", "valence"), ("A", "arousal"), ("D", "dominance")):
            item = stats["axes"][axis]
            axis_parts.append(
                f"{label} 均值={item['mean']:+.2f} 最低={item['min']:+.2f} 最高={item['max']:+.2f}")
        stats_text = header + "\n" + "；".join(axis_parts)
        recent_parts = []
        for row in clean[-RECENT_POINT_COUNT:]:
            stamp = _parse_point_time(row["at"])
            clock = stamp.strftime("%H:%M") if stamp is not None else "--:--"
            recent_parts.append(
                f"{clock} V{row['valence']:+.2f}/A{row['arousal']:+.2f}/D{row['dominance']:+.2f}")
        recent_text = "最近采样：" + "；".join(recent_parts)
    else:
        stats_text = header + "\n无采样点（当天没有可压缩的 VAD 观测）"
        recent_text = ""
    # 优先级：统计（必需）→ 配额（短、信息密度高）→ 最近点（超预算时先截它）
    text = _fit_segments((stats_text, quota_text, recent_text), limit)
    return {
        "text": text,
        "count": count,
        "frozen_count": frozen_count,
        "frozen_ratio": round(frozen_ratio, 4),
        "mean": mean,
        "recent": [dict(row) for row in clean[-RECENT_POINT_COUNT:]],
        "stats": stats,
    }


# ── 确定性规则兜底 ──────────────────────────────────────────
def rule_delta(current: Any, mean: Optional[VADState],
               anchor: Optional[VADState]) -> VADState:
    """无 LLM 的确定性兜底增量（永不抛异常）。

    - 有当天均值：``delta = (均值 − 当前) × 0.3``（向当天基调混合）；
    - 无采样：``delta = (锚点 − 当前) × 0.1``（向人设锚点衰减一小步；锚点缺失按 0）。

    返回的是 **增量**（由 ``_apply_external_delta`` 叠加到当前 VAD），不是新 VAD 值。
    """
    base = current if isinstance(current, VADState) else VADState.from_dict(current or {})
    if isinstance(mean, VADState):
        target = mean
        blend = RULE_BLEND_WITH_POINTS
    else:
        target = anchor if isinstance(anchor, VADState) else VADState()
        blend = RULE_BLEND_NO_POINTS
    return VADState(
        _clip((target.valence - base.valence) * blend),
        _clip((target.arousal - base.arousal) * blend),
        _clip((target.dominance - base.dominance) * blend),
    )


# ── 日期去重状态（独立小文件，损坏/写入失败不阻塞结算）──────────
def settlement_state_path(state_path: Any) -> Path:
    """``emotion_state.json`` → 同目录 ``emotion_daily_settlement.json``。"""
    return Path(state_path).with_name(STATE_FILE_NAME)


def load_state(path: Any) -> Dict[str, Any]:
    """读去重状态；缺失/损坏/坏类型 → 空默认（``last_date=""`` 表示从未结算）。"""
    raw = load_json(Path(path), {})
    if not isinstance(raw, dict):
        raw = {}
    return {
        "last_date": _valid_date_key(raw.get("last_date")),
        "mode": str(raw.get("mode") or ""),
        "points": _opt_int(raw.get("points")) or 0,
        "catch_up": bool(raw.get("catch_up")),
        "at": str(raw.get("at") or ""),
    }


def save_state(path: Any, *, last_date: str, mode: str, points: int,
               catch_up: bool, at: str = "") -> bool:
    """原子写去重状态（只用于跨日/重启去重，失败返回 False 由调用方留痕）。"""
    doc = {
        "version": STATE_VERSION,
        "last_date": str(last_date or ""),
        "mode": str(mode or ""),
        "points": int(points or 0),
        "catch_up": bool(catch_up),
        "at": str(at or ""),
    }
    try:
        return bool(save_json_atomic(Path(path), doc))
    except Exception:
        return False
