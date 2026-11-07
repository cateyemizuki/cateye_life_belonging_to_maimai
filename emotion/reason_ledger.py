"""短期情绪原因账本（PR-C1 H4，2026-11-07；计划书 §5 H4 / §5.4 / §8.2.3）。

给短期情绪变化一个**可核验、有时效**的「为什么」：每条原因
``{reason_id, summary(≤120字), source_ref(消息/事件引用，不存正文),
delta_vad, created_at, expires_at, salience}``。

红线（规格 §1，逐条落实）：
- **不造竞争 VAD**：本账本只作解释与注入措辞参考，绝不二次加减 VAD——
  情绪数值仍由 :mod:`emotion.state_machine` 的既有结算/衰减唯一持有；
- 同一事件 dedupe：``source_ref`` 唯一，重复登记返回 False 不重复记账；
- 原因影响随时间减弱：``effective_salience`` 按剩余寿命线性衰减（只作
  观测/排序口径）；到期（``expires_at``）后不再进入任何只读消费口；
- 延迟影响/渐进恢复由既有 decay 承担，本模块不改 decay 曲线。

本模块 0 LLM、不 import 业务模块（只用标准库 + 同包 vad）；全部入参防御性
夹取，非法输入静默拒绝/降级，绝不拖挂情绪结算。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from .vad import VADState

#: 缺省 TTL（小时；规格 §1「缺省 ttl 配置如 6h」，配置键 emotion_reason_ttl_hours）
DEFAULT_REASON_TTL_HOURS = 6
#: TTL 夹取（与 [emotion].emotion_reason_ttl_hours ge=1 le=48 同口径）
REASON_TTL_MIN_HOURS = 1
REASON_TTL_MAX_HOURS = 48
#: 摘要长度上限（规格 §1：summary ≤120 字）
REASON_SUMMARY_MAX_CHARS = 120
#: 账本容量上限（防无界增长；超限淘汰最旧）
REASON_MAX_ENTRIES = 64
#: 「高 salience」注入阈值（规格 §1：最近 1 条未过期高 salience 原因才附注入）
REASON_INJECT_SALIENCE_MIN = 0.5
#: 登记缺省 salience（调用方未给时；保守固定值，规格未给定派生公式）
REASON_DEFAULT_SALIENCE = 0.6


def _clip_text(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _unit(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(0.0, min(1.0, number))


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.strip())
    except ValueError:
        return None


class ReasonLedger:
    """短期原因账本：登记/dedupe/过期/有界只读消费（纯内存 + 随状态机落盘）。"""

    def __init__(self) -> None:
        self._entries: List[Dict[str, Any]] = []

    # ── 登记（source_ref 唯一；非法/重复 → False）──────────────────────
    def record(self, *, summary: str, source_ref: str,
               delta_vad: Optional[VADState] = None,
               now: Optional[datetime] = None,
               ttl_hours: Optional[float] = None,
               salience: Optional[float] = None) -> bool:
        """登记一条原因。``source_ref`` 为空 → False（无引用不可核验，不记）；
        同一 ``source_ref`` 已在账 → False（不重复记账，也不刷新旧条目）。"""
        ref = _clip_text(source_ref, 300)
        text = _clip_text(summary, REASON_SUMMARY_MAX_CHARS)
        if not ref or not text:
            return False
        moment = now or datetime.now()
        self._prune(moment)
        if any(entry.get("source_ref") == ref for entry in self._entries):
            return False
        try:
            ttl = float(ttl_hours) if ttl_hours is not None else float(DEFAULT_REASON_TTL_HOURS)
        except (TypeError, ValueError):
            ttl = float(DEFAULT_REASON_TTL_HOURS)
        if ttl != ttl:  # NaN
            ttl = float(DEFAULT_REASON_TTL_HOURS)
        ttl = max(float(REASON_TTL_MIN_HOURS), min(float(REASON_TTL_MAX_HOURS), ttl))
        delta = (delta_vad or VADState()).clipped()
        self._entries.append({
            "reason_id": "reason_" + uuid.uuid4().hex[:16],
            "summary": text,
            "source_ref": ref,
            "delta_vad": delta.to_dict(),
            "created_at": moment.isoformat(timespec="seconds"),
            "expires_at": (moment + timedelta(hours=ttl)).isoformat(timespec="seconds"),
            "salience": round(_unit(salience, REASON_DEFAULT_SALIENCE), 4),
        })
        if len(self._entries) > REASON_MAX_ENTRIES:
            # 超限淘汰最旧（created_at 升序；同刻按登记顺序）
            self._entries.sort(key=lambda e: str(e.get("created_at") or ""))
            self._entries = self._entries[-REASON_MAX_ENTRIES:]
        return True

    # ── 过期清理与有效重要度 ──────────────────────────────────────────
    def _prune(self, now: datetime) -> int:
        before = len(self._entries)
        kept = []
        for entry in self._entries:
            expires = _parse_ts(entry.get("expires_at"))
            if expires is not None and now >= expires:
                continue
            kept.append(entry)
        self._entries = kept
        return before - len(kept)

    @staticmethod
    def effective_salience(entry: Dict[str, Any], now: datetime) -> float:
        """原因重要度随时间减弱：salience × 剩余寿命比例（线性，到期为 0）。

        只作「为什么」的可核验解释口径（观测/排序），**绝不回写 VAD**。
        时钟回拨不放大（剩余比例封顶 1）。
        """
        created = _parse_ts(entry.get("created_at"))
        expires = _parse_ts(entry.get("expires_at"))
        base = _unit(entry.get("salience"), 0.0)
        if created is None or expires is None or expires <= created:
            return 0.0
        try:
            remaining = (expires - now).total_seconds() / (expires - created).total_seconds()
        except (TypeError, ZeroDivisionError):
            return 0.0
        return round(base * max(0.0, min(1.0, remaining)), 4)

    # ── 只读消费口 ────────────────────────────────────────────────────
    def active(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """未过期原因（新→旧；附 effective_salience 观测字段；不写盘）。"""
        moment = now or datetime.now()
        out: List[Dict[str, Any]] = []
        for index, entry in enumerate(self._entries):
            expires = _parse_ts(entry.get("expires_at"))
            if expires is not None and moment >= expires:
                continue
            row = dict(entry)
            row["effective_salience"] = self.effective_salience(entry, moment)
            out.append((index, row))
        # 新→旧；created_at 只精确到秒，同秒并列按登记顺序取最新（index 大者）
        out.sort(key=lambda pair: (str(pair[1].get("created_at") or ""), pair[0]), reverse=True)
        return [row for _, row in out]

    def latest_injectable(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """最近 1 条未过期且 salience ≥ :data:`REASON_INJECT_SALIENCE_MIN` 的原因。

        过期/低 salience/空账本 → None（调用方不附注入）。只读，不改账本。
        """
        moment = now or datetime.now()
        for entry in self.active(moment):
            if _unit(entry.get("salience"), 0.0) >= REASON_INJECT_SALIENCE_MIN:
                return entry
        return None

    def clear(self) -> None:
        """清空（人设重新初始化时随情绪残留一起清）。"""
        self._entries = []

    # ── 序列化（随 emotion_state.json 落盘；旧文件缺键 → 空账本）────────
    def to_list(self) -> List[Dict[str, Any]]:
        return [dict(entry) for entry in self._entries]

    def load_list(self, rows: Any) -> None:
        """防御性装载：非列表/坏行跳过，绝不因旧状态文件启动失败。"""
        self._entries = []
        if not isinstance(rows, list):
            return
        for row in rows[:REASON_MAX_ENTRIES]:
            if not isinstance(row, dict):
                continue
            if not _clip_text(row.get("reason_id"), 80) \
                    or not _clip_text(row.get("summary"), REASON_SUMMARY_MAX_CHARS) \
                    or not _clip_text(row.get("source_ref"), 300):
                continue
            if _parse_ts(row.get("created_at")) is None \
                    or _parse_ts(row.get("expires_at")) is None:
                continue
            self._entries.append({
                "reason_id": _clip_text(row.get("reason_id"), 80),
                "summary": _clip_text(row.get("summary"), REASON_SUMMARY_MAX_CHARS),
                "source_ref": _clip_text(row.get("source_ref"), 300),
                "delta_vad": VADState.from_dict(row.get("delta_vad")).to_dict(),
                "created_at": str(row.get("created_at")),
                "expires_at": str(row.get("expires_at")),
                "salience": round(_unit(row.get("salience"), REASON_DEFAULT_SALIENCE), 4),
            })
