"""好感规则事件层（§5.3「规则事件层」+ §5.4 M1 行；零 LLM 成本）。

统一入口 :func:`record_rule_event` —— 频控 → 日累计限幅 → 入账 → 档位重估 → 事件日志：

- 频控（§5.4）：``chat_round`` 日累计 ≤ +1.0；``user_initiated`` 日 ≤ +0.5；
  ``morning_exchange`` / ``night_exchange`` 各 1 次/日；``commitment_kept`` /
  ``commitment_broken`` 每约定 1 次（去重键见 ``key`` 参数）。
- 日累计限幅（§5.5）：positive ≤ ``daily_positive_cap``(4.0)、negative ≥
  ``-daily_negative_cap``(3.0)，超出部分记日志丢弃。
- 情绪调制（§六.2 B，M3 接线）：``delta_affection_final = delta × clamp(1 + 0.25 ×
  V_current × sign(delta), 0.6, 1.4)``；V_current 由 ``AffectionManager.emotion_valence()``
  读取（emotion 模块未接线/停用时返回 None → 乘数恒 1.0），开关
  ``affection.emotion_modulation``。调制系数与裁剪前后值落 decision_logs（§6.3-7）。

调用方式二选一（等价）：

    from companion.affection_events import record_rule_event
    record_rule_event(affection, "chat_round", 0.05, "一问一答")
    affection.record_rule_event("chat_round", 0.05, "一问一答")

M2 的好感 LLM 裁判 kind（``llm_review`` / ``daily_review``）已登记：增量由裁判输出并在
referee 侧二次限幅，本层只做日累计限幅与入账。其余 M3/M6 的 kind（``silence_day`` /
``conflict`` / ``apology_accepted`` / ``qzone_*``）仍不在本表，传入即按 ``unknown_kind``
丢弃并告警——不猜测、不静默扩表。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: bot 沉默多久后「对方先开口」才算 user_initiated（小时；§5.4）
USER_INITIATED_SILENCE_HOURS = 1.0

#: M1 规则事件表（§5.4；delta 为默认增量，daily_kind_cap = 同日同 kind 正增量累计上限）
RULE_EVENT_TABLE: dict[str, dict[str, Any]] = {
    "chat_round": {
        "delta": 0.05,
        "daily_kind_cap": 1.0,
        "desc": "与 target 一问一答",
        "note": "日累计 ≤ +1.0（防刷分；消息轰炸无效）",
    },
    "user_initiated": {
        "delta": 0.15,
        "daily_kind_cap": 0.5,
        "desc": "bot 沉默 ≥1h 后对方先开口",
        "note": "日 ≤ +0.5",
    },
    "morning_exchange": {
        "delta": 0.2,
        "once_per_day": True,
        "desc": "互道早安",
        "note": "各 1 次/日",
    },
    "night_exchange": {
        "delta": 0.2,
        "once_per_day": True,
        "desc": "互道晚安",
        "note": "各 1 次/日",
    },
    "commitment_kept": {
        "delta": 1.0,
        "per_key_once": True,
        "desc": "约定兑现",
        "note": "每约定 1 次",
    },
    "commitment_broken": {
        "delta": -0.5,
        "per_key_once": True,
        "desc": "约定违约",
        "note": "每约定 1 次",
    },
    # ---- M2：LLM 裁判入账 kind（delta 由裁判输出并已在 referee 侧裁剪；仅受日累计限幅，
    #      无 kind 频控——裁判本身已有每日调用上限，见 affection_referee）----
    "llm_review": {
        "delta": 0.0,
        "desc": "会话采样裁判",
        "note": "M2：delta ∈ [-2, +2.5] 由裁判传入（缺省 0 表示无事件，会被丢弃）",
    },
    "daily_review": {
        "delta": 0.0,
        "desc": "关系日结",
        "note": "M2：delta ∈ [-2, +2] 由裁判传入；独立于 kind 上限、受 §5.5 日累计限幅",
    },
}

DEFAULT_DAILY_POSITIVE_CAP = 4.0
DEFAULT_DAILY_NEGATIVE_CAP = 3.0

#: 情绪调制兜底乘数（无情绪上下文/开关关闭时的恒等值；M3 起真实调制见 _emotion_multiplier）
_EMOTION_MULTIPLIER = 1.0

#: 情绪调制参数（§六.2 B）：1 + 0.25 × V × sign(delta)，乘数 clamp [0.6, 1.4]
EMOTION_MODULATION_K = 0.25
EMOTION_MODULATION_MIN = 0.6
EMOTION_MODULATION_MAX = 1.4

#: 限幅类丢弃的告警节流（内存态，按「manager+kind+原因+日期」只警告一次；
#: 逐条留痕仍在 decision_logs，日志级别降为 debug）
_cap_warned: dict[tuple, str] = {}


def _should_warn(affection: Any, kind: str, reason: str, now: datetime) -> bool:
    today = now.strftime("%Y-%m-%d")
    for key in [k for k in _cap_warned if k[3] != today]:
        _cap_warned.pop(key, None)
    key = (id(affection), str(kind), str(reason), today)
    if key in _cap_warned:
        return False
    _cap_warned[key] = today
    return True


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _emotion_multiplier(affection: Any, delta: float) -> tuple:
    """情绪调制乘数（§6.2 B，M3）：``1 + 0.25 × V × sign(delta)``，clamp [0.6, 1.4]。

    心理学含义（情绪-一致性偏差）：开心时正事件加分更多、负事件更宽容；低落时
    正事件打折、负事件扣得更狠；乘数对称保正负同时缩放（不会"心情差所以骂人也不扣分"）。

    Returns:
        ``(multiplier, valence)``；V 不可得（emotion 模块未接线/停用/异常）或
        ``affection.emotion_modulation=false`` 时返回 ``(1.0, None)``——乘数恒 1，
        与 M1 占位行为一致（降级路径）。
    """
    if affection is None or not delta:
        return _EMOTION_MULTIPLIER, None
    enabled = getattr(affection, "emotion_modulation_enabled", None)
    if callable(enabled):
        try:
            if not enabled():
                return _EMOTION_MULTIPLIER, None
        except Exception:
            return _EMOTION_MULTIPLIER, None
    get_valence = getattr(affection, "emotion_valence", None)
    if not callable(get_valence):
        return _EMOTION_MULTIPLIER, None
    try:
        valence = get_valence()
    except Exception:
        valence = None
    if valence is None:
        return _EMOTION_MULTIPLIER, None
    try:
        v = float(valence)
    except (TypeError, ValueError):
        return _EMOTION_MULTIPLIER, None
    sign = 1.0 if delta > 0 else -1.0
    multiplier = 1.0 + EMOTION_MODULATION_K * v * sign
    return max(EMOTION_MODULATION_MIN, min(EMOTION_MODULATION_MAX, multiplier)), v


def _resolve_caps(affection: Any, caps: Any) -> dict[str, Any]:
    """解析本次调用的限幅表：配置（[affection]）为底，``caps`` 覆盖。

    ``caps`` 允许的键：``daily_positive`` / ``daily_negative``（日累计限幅），
    以及任一规则事件 kind（覆盖该 kind 的 ``daily_kind_cap``）。
    """
    daily_positive, daily_negative = DEFAULT_DAILY_POSITIVE_CAP, DEFAULT_DAILY_NEGATIVE_CAP
    getter = getattr(affection, "daily_caps", None)
    if callable(getter):
        try:
            cfg = getter() or {}
            daily_positive = _to_float(cfg.get("positive"), daily_positive)
            daily_negative = _to_float(cfg.get("negative"), daily_negative)
        except Exception:
            pass
    kind_caps = {
        kind: float(rule["daily_kind_cap"])
        for kind, rule in RULE_EVENT_TABLE.items()
        if rule.get("daily_kind_cap") is not None
    }
    override = caps if isinstance(caps, dict) else {}
    daily_positive = _to_float(override.get("daily_positive"), daily_positive)
    daily_negative = _to_float(override.get("daily_negative"), daily_negative)
    for key, value in override.items():
        if key in RULE_EVENT_TABLE:
            try:
                kind_caps[key] = float(value)
            except (TypeError, ValueError):
                continue
    return {
        "daily_positive": max(0.0, daily_positive),
        "daily_negative": max(0.0, daily_negative),
        "kind_caps": kind_caps,
    }


def _drop(affection: Any, result: dict[str, Any], reason: str, detail: str = "",
          *, requested: float = 0.0, warn: bool = False,
          now: Optional[datetime] = None) -> dict[str, Any]:
    """丢弃本次事件：留痕（python logger + 决策日志），不改状态。"""
    result.update(dropped=True, reason=reason, requested=round(requested, 4), applied=0.0)
    log_line = f"好感规则事件丢弃：kind={result.get('kind')} reason={reason} requested={requested:.3f}"
    if detail:
        log_line += f"（{detail}）"
    if warn and _should_warn(affection, str(result.get("kind") or ""), reason,
                             now or datetime.now()):
        logger.warning(log_line)
    else:
        logger.debug(log_line)
    log = getattr(affection, "log_decision", None)
    if callable(log) and affection is not None:
        try:
            log("affection.rule_event_dropped", trigger_type="affection", reason=reason,
                kind=str(result.get("kind") or ""), detail=detail,
                requested=round(requested, 4),
                multiplier=result.get("multiplier", _EMOTION_MULTIPLIER),
                requested_raw=result.get("requested_raw", round(requested, 4)),
                emotion_valence=result.get("valence"))
        except Exception:
            pass
    if affection is not None:
        try:
            result["score"] = float(affection.score())
            result["tier"] = int(affection.tier())
        except Exception:
            pass
    return result


def record_rule_event(affection: Any, kind: str, delta: Optional[float] = None, desc: str = "",
                      *, caps: Any = None, key: str = "", bus: Any = None,
                      now: Optional[datetime] = None) -> dict[str, Any]:
    """记录一条规则事件（M1 规则表见 :data:`RULE_EVENT_TABLE`）。

    Args:
        affection: :class:`companion.affection.AffectionManager`（可空 → 记录 no_manager 后返回）。
        kind: 事件类型。
        delta: 增量；``None`` 时取规则表默认值。
        desc: 事件描述（写入 event_log，供 M2 日结裁判引用）。
        caps: 本次调用的限幅覆盖（见 :func:`_resolve_caps`）。
        key: 去重键（``commitment_*`` 用约定 ID；缺省回退 ``desc``）。
        bus: 预留（档位变化发布走 manager 自己的总线；此处仅为兼容签名保留）。
        now: 事件时间（测试/补偿用；缺省当前时间）。

    Returns:
        ``{"kind", "requested", "applied", "dropped", "reason", "score", "tier",
        "tier_changed", "multiplier", "requested_raw", "valence"}``
        （``requested_raw`` = 情绪调制前，``requested`` = 调制后/限幅前，``applied`` =
        实际入账，``multiplier`` = 情绪调制系数（未接线时 1.0），``valence`` = 调制
        所用当前 V（不可得时为 None）；``tier_changed`` = (old,new) 或 None）。
    """
    now = now or datetime.now()
    kind = str(kind or "").strip()
    result: dict[str, Any] = {
        "kind": kind, "requested": 0.0, "applied": 0.0, "dropped": True,
        "reason": "", "score": 0.0, "tier": 0, "tier_changed": None,
        "multiplier": _EMOTION_MULTIPLIER,
    }
    if affection is None:
        result["reason"] = "no_manager"
        return result
    rule = RULE_EVENT_TABLE.get(kind)
    if rule is None:
        logger.warning("好感规则事件：未登记的 kind=%s（M1 仅支持：%s）",
                       kind or "(空)", ", ".join(RULE_EVENT_TABLE))
        result["reason"] = "unknown_kind"
        return result
    is_enabled = getattr(affection, "is_enabled", None)
    if callable(is_enabled) and not is_enabled():
        result["reason"] = "disabled"
        logger.debug("好感规则事件跳过：affection.enabled=false（回退 v1 三档行为）")
        return result

    # 跨天兜底：事件先于巡检日切到达时，保证日累计/事件日志属于「今天」
    sync_daily = getattr(affection, "sync_daily", None)
    if callable(sync_daily):
        try:
            sync_daily()
        except Exception as exc:
            logger.debug("好感日切兜底失败（忽略）：%s", exc)

    requested = _to_float(rule.get("delta"), 0.0) if delta is None else _to_float(delta, 0.0)
    if requested == 0.0:
        return _drop(affection, result, "zero_delta", now=now)

    # ---- 情绪调制（B2，§六.2 B，M3 接线）：delta × clamp(1+0.25×V×sign(delta), 0.6, 1.4) ----
    # 调制在频控/限幅之前应用，后续 kind 日累计与日累计限幅按调制后的值计算；
    # 裁剪前后值（requested_raw → requested）与调制系数落决策日志（§6.3-7）。
    requested_raw = round(requested, 4)
    modulation, valence = _emotion_multiplier(affection, requested)
    if modulation != _EMOTION_MULTIPLIER:
        requested = round(requested * modulation, 4)
        logger.debug("好感情绪调制：%.4f × %.4f（V=%s）→ %.4f",
                     requested_raw, modulation, valence, requested)
    result.update(multiplier=round(modulation, 4), requested_raw=requested_raw,
                  valence=(round(float(valence), 4) if valence is not None else None))

    resolved = _resolve_caps(affection, caps)
    today_events = [e for e in affection.event_log() if e.get("kind") == kind]
    notes: list[str] = []

    # ---- 频控 ----
    if rule.get("once_per_day") and today_events:
        return _drop(affection, result, "daily_once",
                     f"{kind} 每天只记 1 次", requested=requested, now=now)
    dedup = str(key or desc or "")
    if rule.get("per_key_once"):
        if dedup:
            if any(str(e.get("key") or e.get("desc") or "") == dedup for e in today_events):
                return _drop(affection, result, "duplicate_key",
                             f"{kind} 同一约定只记 1 次（key={dedup}）", requested=requested, now=now)
        elif today_events:
            return _drop(affection, result, "daily_once_fallback",
                         f"{kind} 缺 key/desc，降级为同日一次", requested=requested, now=now)
        else:
            notes.append("commitment 事件缺 key/desc，降级为同日一次")

    kind_cap = resolved["kind_caps"].get(kind)
    if kind_cap is not None and requested > 0:
        if kind_cap <= 0:
            return _drop(affection, result, "kind_cap",
                         f"{kind} 日累计上限配置为 {kind_cap:g}", requested=requested, warn=True, now=now)
        used = sum(float(e.get("delta") or 0.0) for e in today_events
                   if _to_float(e.get("delta"), 0.0) > 0)
        room = kind_cap - used
        if room <= 0:
            return _drop(affection, result, "kind_cap",
                         f"{kind} 今日已记 {used:.3f}，达日累计上限 {kind_cap:g}",
                         requested=requested, warn=True, now=now)
        if requested > room:
            notes.append(f"kind 日累计限幅：{requested:.3f} → {room:.3f}（上限 {kind_cap:g}）")
            logger.info("好感规则事件裁剪：kind=%s 日累计限幅 %.3f → %.3f（上限 %g）",
                        kind, requested, room, kind_cap)
            requested = room

    # ---- 日累计限幅（正 ≤ cap / 负 ≥ −cap），超出部分记日志丢弃 ----
    daily = affection.daily_delta() or {}
    pos_used = max(0.0, _to_float(daily.get("positive"), 0.0))
    neg_used = min(0.0, _to_float(daily.get("negative"), 0.0))
    if requested > 0:
        room = resolved["daily_positive"] - pos_used
        if room <= 0:
            return _drop(affection, result, "daily_cap",
                         f"今日正向累计已达 {pos_used:.3f}／上限 {resolved['daily_positive']:g}",
                         requested=requested, warn=True, now=now)
        planned = min(requested, room)
        if planned < requested:
            notes.append(f"日正向限幅：{requested:.3f} → {planned:.3f}"
                         f"（上限 {resolved['daily_positive']:g}）")
            logger.info("好感日累计限幅：正向 %.3f → %.3f（上限 %g，今日已记 %.3f）",
                        requested, planned, resolved["daily_positive"], pos_used)
    else:
        room = resolved["daily_negative"] - abs(neg_used)
        if room <= 0:
            return _drop(affection, result, "daily_cap",
                         f"今日负向累计已达 {neg_used:.3f}／下限 -{resolved['daily_negative']:g}",
                         requested=requested, warn=True, now=now)
        planned = -min(abs(requested), room)
        if planned > requested:
            notes.append(f"日负向限幅：{requested:.3f} → {planned:.3f}"
                         f"（下限 -{resolved['daily_negative']:g}）")
            logger.info("好感日累计限幅：负向 %.3f → %.3f（下限 -%g，今日已记 %.3f）",
                        requested, planned, resolved["daily_negative"], neg_used)

    # ---- 入账（情绪调制已在前面应用；M3 起 multiplier 即 B2 调制系数）----
    applied = affection.commit_rule_delta(planned)
    if applied == 0.0:
        return _drop(affection, result, "score_bound",
                     f"score 已在边界（{affection.score():.1f}），增量无法入账", requested=requested, now=now)

    entry: dict[str, Any] = {
        "ts": now.isoformat(timespec="seconds"),
        "kind": kind,
        "desc": str(desc or rule.get("desc") or ""),
        "delta": round(applied, 4),
        "requested": round(requested, 4),
        "multiplier": round(float(modulation), 4),
    }
    if modulation != _EMOTION_MULTIPLIER:
        entry["requested_raw"] = requested_raw
    if valence is not None:
        entry["emotion_valence"] = round(float(valence), 4)
    if dedup:
        entry["key"] = dedup
    if notes:
        entry["clipped"] = "; ".join(notes)
    affection.append_event_entry(entry)

    changed = affection.reevaluate_tier(reason=f"rule_event:{kind}")

    result.update(dropped=False, reason="", requested=round(requested, 4),
                  applied=round(applied, 4), score=round(affection.score(), 4),
                  tier=int(affection.tier()), tier_changed=changed)
    daily_after = affection.daily_delta() or {}
    log = getattr(affection, "log_decision", None)
    if callable(log):
        try:
            log("affection.rule_event", trigger_type="affection",
                reason=entry["desc"], kind=kind,
                requested=round(requested, 4), applied=round(applied, 4),
                multiplier=round(float(modulation), 4),
                requested_raw=requested_raw,
                emotion_valence=(round(float(valence), 4) if valence is not None else None),
                detail="; ".join(notes) or "入账",
                daily_positive=_to_float(daily_after.get("positive"), 0.0),
                daily_negative=_to_float(daily_after.get("negative"), 0.0),
                score=result["score"], tier=result["tier"])
        except Exception:
            pass
    logger.debug("好感规则事件入账：kind=%s applied=%.3f（requested=%.3f，调制 ×%.3f）score=%.1f tier=%d",
                 kind, applied, requested, modulation, result["score"], result["tier"])
    return result


def note_user_message(affection: Any, dt: Optional[datetime] = None) -> dict[str, Any]:
    """对方来消息时的 ``user_initiated`` 检测（§5.4：bot 沉默 ≥1h 后对方先开口）。

    由 :meth:`AffectionManager.update_last_user_msg_time` 调用（既有 chat Hook 零改动）；
    判定条件：bot 有发言记录、本条是 bot 上次发言后的第一条用户消息、
    且距 bot 上次发言 ≥ :data:`USER_INITIATED_SILENCE_HOURS` 小时。
    """
    now = dt or datetime.now()
    if affection is None:
        return {"dropped": True, "reason": "no_manager"}
    try:
        last_speak = affection.last_speak_time()
        prev_user = affection.last_user_msg_time()
    except Exception:
        return {"dropped": True, "reason": "no_state"}
    if last_speak is None:
        return {"dropped": True, "reason": "no_bot_speak"}
    if prev_user is not None and prev_user > last_speak:
        return {"dropped": True, "reason": "already_answered"}
    silence_hours = (now - last_speak).total_seconds() / 3600.0
    if silence_hours < USER_INITIATED_SILENCE_HOURS:
        return {"dropped": True, "reason": "silence_short"}
    return record_rule_event(
        affection, "user_initiated", None,
        f"沉默 {silence_hours:.1f}h 后对方先开口", now=now,
    )