"""video_companion —— 观看事实 → 陪伴分享候选的适配与投递（冲浪计划书 §6/§7/§8，S3）。

职责边界（计划书 §8 末行 integrations/video_companion.py）：
- 只做「真实观看完成结果 → 分享候选 → 条件投递」的适配，不复制视频任务、
  日程或主动预算的权威状态；
- 本模块**零业务导入**（只标准库 + typing）：陪伴对象/预算/发送能力一律经
  ``ShareDelivery`` 注入的可调用对象提供，装配侧在 plugin.py 接线
  （结构守卫 scratch/test_backend_boundaries.py 的 integrations 白名单不新增违规）；
- 候选生成是条件式的，不是「看完必发」：仅视觉理解成功且有可用摘要、
  且有观后感时入候选（计划书 §6 末段、§7.1）；
- 发送节流：每日最多 1 条、最小间隔 6 小时、并计入现有每日主动 5 次
  （``budget_remaining`` 为调用方给出的剩余主动额度；None=不可得则保守拒绝，
  计划书 §7.2）；忙时最多保留 1 条 pending 候选，TTL 2 小时；
- 失败不盲重试；结果不确定按保守防重丢弃（status="dropped"），
  不靠内存标志防重复（计划书 §7.2 末两条）。

持久化：``store_dir/share_candidates.json``（临时文件 + ``os.replace`` 原子落盘；
读取失败按损坏处理返回默认值，不抛出拖挂装配）。文件结构::

    {
      "version": 1,
      "candidates": [ {share_id, bvid, title, summary, impression, episode_id,
                       created_at, expire_at, status, ...} ],
      "daily": {"date": "YYYY-MM-DD", "sent_count": 0, "last_sent_epoch": null}
    }
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from core.coerce import to_float as _as_float, to_int as _as_int

#: 候选 TTL（秒）：忙时最多保留 2 小时（计划书 §7.2 建议值）
SHARE_CANDIDATE_TTL_SECONDS = 7200
#: 自动分享每日上限（计划书 §7.2：每天最多 1 条）
SHARE_DAILY_MAX = 1
#: 自动分享最小间隔（秒）：6 小时（计划书 §7.2）
SHARE_MIN_INTERVAL_SECONDS = 21600
#: 规范视频链接模板（计划书 §7.1：规范 BV/视频链接）
SHARE_LINK_TEMPLATE = "https://www.bilibili.com/video/{bvid}"

#: 摘要/观后感入候选的长度上限（计划书 §6：有界摘要）
_SHARE_TEXT_MAX = 300
#: 候选库有界：保留条数上限与已终态候选的保留天数（计划书 §6/§8 有界持久化）
_MAX_CANDIDATES = 100
_TERMINAL_KEEP_DAYS = 7

#: 与 video_watch/bv.py 同口径的 BV 形态（本模块零业务导入，故自带同式正则）
_BV_RE = re.compile(r"\bBV[0-9A-Za-z]{10}\b")
_BV_FULL_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
#: 出站文本清洗：普通 URL / 媒体直链、原始帧文件名、凭据片段一律不随分享外发
_URL_RE = re.compile(r"(?:https?|ftp|file)://\S+", re.IGNORECASE)
_FRAME_RE = re.compile(
    r"\b(?:frame|frames|img|image|thumb)[_-]?\d{1,6}\.(?:jpe?g|png|webp|bmp)\b",
    re.IGNORECASE)
_CRED_RE = re.compile(
    r"(?:authorization|cookie|token|secret|api[_-]?key)\s*[:=]\s*(?:\S+\s+)?\S+",
    re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
#: 「不确定」的常见回执分类（无法证明未送达 → 保守防重）
_UNCERTAIN_ERROR_CLASSES = frozenset({
    "timeout", "timed_out", "deadline", "unknown", "uncertain", "indeterminate",
    "receipt_lost", "no_receipt", "network", "transport", "exception",
})

DeliverCallable = Callable[[str], Awaitable[Dict[str, Any]]]
BudgetCallable = Callable[[], Optional[int]]


# ----------------------------------------------------------------------
# 持久化小工具（与 integrations/video_schedule.py 同构；按需求复制，不跨模块 import）
def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _default_day() -> str:
    """日切口径：本地自然日（YYYY-MM-DD，与 video_schedule 额度账本同口径）。"""
    return date.today().isoformat()


def _empty_daily(day: str) -> Dict[str, Any]:
    return {"date": str(day), "sent_count": 0, "last_sent_epoch": None}


def _json_safe(value: Any) -> Any:
    """把回执压成可落盘的普通 JSON（不可序列化字段降级为字符串）。"""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return {"repr": str(value)[:300]}


# ----------------------------------------------------------------------
# 文本与 BV 规范化
def _normalize_bvid(raw: Any) -> str:
    """只接受完整 BV 号（BV + 10 位字母数字），不从噪声里猜 ID。"""
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    return text if _BV_FULL_RE.fullmatch(text) else ""


def _extract_bvid(raw: Any) -> str:
    """从结果自带的视频页 URL 中提取首个形态合法的 BV 号（不联网、不改写）。"""
    if not isinstance(raw, str):
        return ""
    match = _BV_RE.search(raw)
    return match.group(0) if match else ""


def _collapse(text: Any, limit: int) -> str:
    """压缩空白并按字符数截断（中文按字符计，计划书 §6 的「有界摘要」）。"""
    if not isinstance(text, str):
        return ""
    text = _WS_RE.sub(" ", text).strip()
    text = re.sub(r"\s+([，,。；;！!？?、])", r"\1", text)  # 删除清洗留下的悬空标点前空格
    return text[:max(0, int(limit))].strip(" ，,;；、")


def _clean_source(text: Any, limit: int) -> str:
    """候选入库前的清洗：去掉直链/凭据/原始帧名，再压缩截断。"""
    if not isinstance(text, str):
        return ""
    text = _FRAME_RE.sub(" ", text)
    text = _URL_RE.sub(" ", text)
    text = _CRED_RE.sub(" ", text)
    return _collapse(text, limit)


def _clean_outbound(text: Any, limit: int) -> str:
    """出站文本清洗：与入库同口径（候选可能由旧文件/手改带入脏文本）。"""
    return _clean_source(text, limit)


def _ensure_period(text: str) -> str:
    """给口语理由补句号；已有终止标点则不重复。"""
    text = text.strip()
    if not text:
        return ""
    if text[-1] in "。！？.!?~～…":
        return text
    return text + "。"


def build_share_text(candidate: Dict[str, Any]) -> str:
    """生成分享文本（计划书 §7.1）：1–2 句口语理由 + 规范 BV 链接。

    - 理由只来自候选里真实落盘的标题/观后感/画面摘要，不声称看完全部、
      不虚构共同经历、不写「任务已完成」式模板；
    - 其他 URL/凭据/原始帧名不会外发；链接只由合法 BV 号拼出；
    - 无合法 BV 号或没有任何可用理由 → 返回 ""（调用方按「不分享」处理，
      绝不编造链接或理由）。
    """
    cand = candidate if isinstance(candidate, dict) else {}
    bvid = _normalize_bvid(cand.get("bvid")) or _extract_bvid(cand.get("url"))
    if not bvid:
        return ""
    title = _clean_outbound(cand.get("title"), 60)
    impression = _clean_outbound(cand.get("impression"), 200)
    summary = _clean_outbound(cand.get("summary"), 160)
    reason = impression or summary
    if not reason:
        return ""
    body = f"刚刷到《{title or bvid}》，{_ensure_period(reason)}"
    # 观后感之外补一句画面摘要（仍来自真实视觉理解，最多凑成 2 句理由）
    if impression and summary and summary not in impression:
        body += f" 画面里大概是{_ensure_period(summary)}"
    link = SHARE_LINK_TEMPLATE.format(bvid=bvid)
    return f"{body} {link}"[:500]


# ----------------------------------------------------------------------
class ShareCandidateStore:
    """分享候选 + 每日发送计数的唯一持久化所有者（计划书 §7.2/§8）。

    - 候选生成条件：``visual.status == "completed"``、``visual.summary`` 非空、
      ``impression`` 非空（§6：仅元信息/字幕的部分结果不进主动分享链）；
    - 同 BV / 同 episode 不重复生成；pending 至多 1 条，新候选替换旧候选；
    - ``due_share`` 返回未过期的 pending；``mark_expired``/``prune`` 负责 TTL 收口；
    - 每日口径 ``{date, sent_count, last_sent_epoch}``，日期变化即清零回写。
    """

    def __init__(self, store_dir: Path, day_provider: Optional[Callable[[], str]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._path = Path(store_dir) / "share_candidates.json"
        self._day_provider = day_provider or _default_day  # 缺省：本地 date
        self._clock = clock
        self._lock = threading.Lock()  # 进程内互斥：并发生成/投递不丢计数

    # ---- 内部 ----
    def _now(self, now: Any = None) -> float:
        if now is not None:
            return float(now)
        return float(self._clock() if callable(self._clock) else time.time())

    def _load(self) -> Dict[str, Any]:
        """读候选库 + 当日计数；缺失/损坏按零值处理，跨日即清零回写（调用方须已持锁）。"""
        day = str(self._day_provider())
        doc = _read(self._path, None)
        if not isinstance(doc, dict):
            doc = {}
        raw = doc.get("candidates")
        candidates = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
        daily_raw = doc.get("daily") if isinstance(doc.get("daily"), dict) else {}
        last_sent = (None if daily_raw.get("last_sent_epoch") is None
                     else _as_float(daily_raw.get("last_sent_epoch")))
        if str(daily_raw.get("date") or "") == day:
            daily = {
                "date": day,
                "sent_count": max(0, _as_int(daily_raw.get("sent_count"))),
                "last_sent_epoch": last_sent,
            }
        else:
            # 日切/首启：**当日计数**清零并回写；上次成功分享时刻跨日保留，
            # 否则午夜一过就能绕过 §7.2 的 6 小时最小间隔（保守恢复）。
            daily = _empty_daily(day)
            daily["last_sent_epoch"] = last_sent
        # In-flight/unknown attempts retain their slot through a reload. Date roll
        # resets the daily hold, but the six-hour attempt boundary remains.
        if str(daily_raw.get("date") or "") == day and "held_count" in daily_raw:
            daily["held_count"] = max(0, _as_int(daily_raw["held_count"]))
        if daily_raw.get("last_attempt_epoch") is not None:
            daily["last_attempt_epoch"] = _as_float(daily_raw["last_attempt_epoch"])
        out = {"version": 1, "candidates": candidates, "daily": daily}
        if out != doc:
            _atomic_write(self._path, out)
        return out

    @staticmethod
    def _created(candidate: Dict[str, Any]) -> float:
        return _as_float(candidate.get("created_at"))

    def _compact(self, doc: Dict[str, Any], at: float) -> int:
        """有界收口：终态候选超 7 天或总数超上限时淘汰最旧的（pending 优先保留）。"""
        candidates = doc.get("candidates") or []
        kept = []
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            terminal = str(cand.get("status") or "") != "pending"
            if terminal and at - self._created(cand) > _TERMINAL_KEEP_DAYS * 86400:
                continue
            kept.append(cand)
        removed = len(candidates) - len(kept)
        if len(kept) > _MAX_CANDIDATES:
            pending = [c for c in kept if str(c.get("status") or "") == "pending"]
            terminal = [c for c in kept if str(c.get("status") or "") != "pending"]
            terminal.sort(key=self._created, reverse=True)
            room = max(0, _MAX_CANDIDATES - len(pending))
            trimmed = pending + terminal[:room]
            removed += len(kept) - len(trimmed)
            kept = trimmed
        doc["candidates"] = kept
        return removed

    def _save(self, doc: Dict[str, Any], at: float) -> int:
        removed = self._compact(doc, at)
        _atomic_write(self._path, doc)
        return removed

    # ---- 对外面：候选 ----
    def record_from_result(self, result: Dict[str, Any], episode_id: str,
                           target_stream: str = "") -> Optional[Dict[str, Any]]:
        """观看结果 → 分享候选（计划书 §6/§7.1）：不满足条件返回 None。

        仅当视觉理解成功、摘要可用且观后感非空才生成；同 BV/同 episode 不重复；
        新候选成立即作废旧 pending（忙时最多保留 1 条）。
        """
        if not isinstance(result, dict):
            return None
        visual = result.get("visual")
        if not isinstance(visual, dict) or str(visual.get("status") or "") != "completed":
            return None  # 未尝试/失败/text-only 部分结果不进主动分享链
        if visual.get("understanding_status") not in (None, "completed"):
            return None
        summary = _clean_source(visual.get("summary"), _SHARE_TEXT_MAX)
        if not summary:
            return None
        impression = _clean_source(result.get("impression"), _SHARE_TEXT_MAX)
        if not impression:
            return None
        episode = str(episode_id or "").strip()
        if not episode:
            return None  # 无 episode 关联无法去重/追溯，不生成
        video = result.get("video") if isinstance(result.get("video"), dict) else {}
        bvid = (_normalize_bvid(result.get("bvid"))
                or _normalize_bvid(video.get("bvid"))
                or _extract_bvid(video.get("url")))
        if not bvid:
            return None  # 不编视频 ID（计划书 §6）
        title = _clean_source(video.get("title") or result.get("title"), 120) or bvid

        with self._lock:
            doc = self._load()
            candidates = doc.get("candidates") or []
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                if str(cand.get("bvid") or "") == bvid \
                        or str(cand.get("episode_id") or "") == episode:
                    return None  # 同 BV / 同 episode 不重复生成
            at = self._now()
            candidate = {
                "share_id": f"share_{bvid}_{int(at)}",
                "bvid": bvid,
                "title": title,
                "summary": summary,
                "impression": impression,
                "episode_id": episode,
                "target_stream": str(target_stream),
                "created_at": at,
                "expire_at": at + SHARE_CANDIDATE_TTL_SECONDS,
                "status": "pending",
            }
            for cand in candidates:
                if isinstance(cand, dict) and str(cand.get("status") or "") == "pending":
                    cand["status"] = "replaced"
                    cand["replaced_at"] = at
                    cand["replaced_by"] = candidate["share_id"]
            candidates.append(candidate)
            doc["candidates"] = candidates
            self._save(doc, at)
            return dict(candidate)

    def due_share(self, now: Any = None) -> Optional[Dict[str, Any]]:
        """返回未过期（expire_at > now）的 pending 候选；没有则 None。"""
        at = self._now(now)
        with self._lock:
            doc = self._load()
            fresh = [c for c in doc.get("candidates") or []
                     if isinstance(c, dict) and str(c.get("status") or "") == "pending"
                     and _as_float(c.get("expire_at")) > at]
        if not fresh:
            return None
        fresh.sort(key=self._created, reverse=True)  # 理论上至多 1 条；多出时取最新
        return dict(fresh[0])

    def claim_delivery(self, share_id: str, now: Any = None) -> bool:
        """Persist sending intent before network I/O, under the store's single lock."""
        at = self._now(now)
        with self._lock:
            doc = self._load()
            daily = doc["daily"]
            if _as_int(daily.get("sent_count")) + _as_int(daily.get("held_count")) >= SHARE_DAILY_MAX:
                return False
            last = max(_as_float(daily.get("last_sent_epoch")),
                       _as_float(daily.get("last_attempt_epoch")))
            if last and at - last < SHARE_MIN_INTERVAL_SECONDS:
                return False
            for cand in doc["candidates"]:
                if cand.get("share_id") != share_id or cand.get("status") != "pending":
                    continue
                if _as_float(cand.get("expire_at")) <= at:
                    return False
                cand.update(status="sending", attempted_at=at,
                            previous_attempt=daily.get("last_attempt_epoch"))
                daily["held_count"] = _as_int(daily.get("held_count")) + 1
                daily["last_attempt_epoch"] = at
                self._save(doc, at)
                return True
            return False

    def defer_claim(self, share_id: str) -> None:
        """Restore only a confirmed unsent intent, never a timeout or unknown send."""
        with self._lock:
            doc = self._load()
            for cand in doc["candidates"]:
                if cand.get("share_id") == share_id and cand.get("status") == "sending":
                    cand["status"] = "pending"
                    doc["daily"]["held_count"] = max(0, _as_int(doc["daily"].get("held_count")) - 1)
                    doc["daily"]["last_attempt_epoch"] = cand.get("previous_attempt")
                    self._save(doc, self._now())
                    return

    def mark_sent(self, share_id: str, receipt: Any) -> Optional[Dict[str, Any]]:
        """记录成功投递（只对 pending 生效，重复调用不重复计数）。"""
        target_id = str(share_id or "")
        at = self._now()
        with self._lock:
            doc = self._load()
            target = None
            for cand in doc.get("candidates") or []:
                if isinstance(cand, dict) and str(cand.get("share_id") or "") == target_id:
                    target = cand
                    break
            if target is None:
                return None
            previous_status = str(target.get("status") or "")
            if previous_status not in ("pending", "sending"):
                return dict(target)  # 已 sent/dropped：幂等，不再计一次
            if previous_status == "sending":
                doc["daily"]["held_count"] = max(0, _as_int(doc["daily"].get("held_count")) - 1)
            target["status"] = "sent"
            target["sent_at"] = at
            target["receipt"] = _json_safe(receipt)
            daily = doc.get("daily") or _empty_daily(str(self._day_provider()))
            daily["sent_count"] = max(0, _as_int(daily.get("sent_count"))) + 1
            daily["last_sent_epoch"] = at
            doc["daily"] = daily
            self._save(doc, at)
            return dict(target)

    def mark_dropped(self, share_id: str, reason: str, *, uncertain: bool = False) -> Optional[Dict[str, Any]]:
        """丢弃候选（失败/不确定都不盲重试）：status="dropped" + 原因。"""
        target_id = str(share_id or "")
        at = self._now()
        with self._lock:
            doc = self._load()
            target = None
            for cand in doc.get("candidates") or []:
                if isinstance(cand, dict) and str(cand.get("share_id") or "") == target_id:
                    target = cand
                    break
            if target is None:
                return None
            previous_status = str(target.get("status") or "")
            if previous_status not in ("pending", "sending"):
                return dict(target)
            if previous_status == "sending" and not uncertain:
                doc["daily"]["held_count"] = max(0, _as_int(doc["daily"].get("held_count")) - 1)
                doc["daily"]["last_attempt_epoch"] = target.get("previous_attempt")
            elif uncertain and previous_status == "pending":
                doc["daily"]["held_count"] = _as_int(doc["daily"].get("held_count")) + 1
                doc["daily"]["last_attempt_epoch"] = at
            target["status"] = "dropped"
            target["dropped_at"] = at
            target["drop_reason"] = str(reason or "")[:300]
            target["uncertain"] = bool(uncertain)
            self._save(doc, at)
            return dict(target)

    def mark_expired(self, now: Any = None) -> int:
        """把已过 TTL 的 pending 置为 expired；返回本次新过期的条数。"""
        at = self._now(now)
        with self._lock:
            doc = self._load()
            count = 0
            for cand in doc.get("candidates") or []:
                if (isinstance(cand, dict) and str(cand.get("status") or "") == "pending"
                        and _as_float(cand.get("expire_at")) <= at):
                    cand["status"] = "expired"
                    cand["expired_at"] = at
                    count += 1
            if count:
                self._save(doc, at)
            return count

    def prune(self, now: Any = None) -> int:
        """过期清理 + 有界收口；返回本次过期/淘汰的条数（无变化不落盘）。"""
        at = self._now(now)
        with self._lock:
            doc = self._load()
            count = 0
            for cand in doc.get("candidates") or []:
                if (isinstance(cand, dict) and str(cand.get("status") or "") == "pending"
                        and _as_float(cand.get("expire_at")) <= at):
                    cand["status"] = "expired"
                    cand["expired_at"] = at
                    count += 1
            removed = self._compact(doc, at)
            if count or removed:
                _atomic_write(self._path, doc)
            return count + removed

    # ---- 对外面：每日计数与发送门 ----
    def daily_snapshot(self) -> Dict[str, Any]:
        """当日发送计数只读快照（date/sent_count/last_sent_epoch）。"""
        with self._lock:
            return dict(self._load().get("daily") or _empty_daily(str(self._day_provider())))

    def snapshot(self) -> Dict[str, Any]:
        """候选库 + 当日计数只读快照（诊断/面板用，不暴露内部引用）。"""
        with self._lock:
            doc = self._load()
            return {
                "candidates": [dict(c) for c in doc.get("candidates") or [] if isinstance(c, dict)],
                "daily": dict(doc.get("daily") or _empty_daily(str(self._day_provider()))),
            }

    def can_send(self, now: Any = None, budget_remaining: Optional[int] = None) -> tuple:
        """自动分享门控（计划书 §7.2）：返回 (是否可发, 中文原因；放行时原因为空)。

        条件：今日已发 < 1 条；距上次成功分享 ≥ 6 小时；主动预算余量 > 0。
        ``budget_remaining is None`` 表示预算面不可得——保守拒绝并注明，
        不把「查不到」当成「还有额度」。
        """
        at = self._now(now)
        with self._lock:
            daily = dict(self._load().get("daily") or _empty_daily(str(self._day_provider())))
        sent = max(0, _as_int(daily.get("sent_count")))
        if sent + _as_int(daily.get("held_count")) >= SHARE_DAILY_MAX:
            return False, f"今日自动分享已达上限（{sent}/{SHARE_DAILY_MAX} 条，计划书 §7.2）"
        last = max(_as_float(daily.get("last_sent_epoch")),
                   _as_float(daily.get("last_attempt_epoch")))
        if last:
            elapsed = at - last
            if elapsed < SHARE_MIN_INTERVAL_SECONDS:
                need_min = max(1, int((SHARE_MIN_INTERVAL_SECONDS - elapsed) // 60) + 1)
                return False, (f"距上次自动分享不足 {SHARE_MIN_INTERVAL_SECONDS // 3600} 小时"
                               f"（已过 {max(0, int(elapsed)) // 60} 分钟，还需约 {need_min} 分钟）")
        if budget_remaining is None:
            return False, "主动消息预算余量不可得，保守不自动分享（计划书 §7.2）"
        try:
            budget = int(budget_remaining)
        except (TypeError, ValueError):
            return False, "主动消息预算余量不可解析，保守不自动分享（计划书 §7.2）"
        if budget <= 0:
            return False, f"主动消息预算已用尽（剩余 {budget} 条），不自动分享"
        return True, ""


# ----------------------------------------------------------------------
class ShareDelivery:
    """候选投递门面：发送能力与预算面均由装配侧注入（计划书 §7.2/§8）。

    ``deliver`` 接收最终文本、返回回执 dict（``receipt.success`` 为真=成功）；
    ``budget_remaining`` 返回当前每日主动消息剩余额度（None=不可得）。
    本类不导入陪伴/规划/观看模块，也不自行决定收件人。
    """

    def __init__(self, store: ShareCandidateStore, *,
                 deliver: DeliverCallable,
                 budget_remaining: BudgetCallable,
                 preflight: Optional[Callable[[], tuple]] = None,
                 target_provider: Optional[Callable[[], str]] = None) -> None:
        self._store = store
        self._deliver = deliver
        self._budget_remaining = budget_remaining
        self._preflight = preflight
        self._target_provider = target_provider
        self._delivery_lock = asyncio.Lock()

    @staticmethod
    def _receipt_uncertain(receipt: Any) -> bool:
        if not isinstance(receipt, dict):
            return True  # 没有可核验回执 → 结果不确定
        for key in ("uncertain", "unknown", "indeterminate", "result_unknown"):
            if receipt.get(key):
                return True
        return str(receipt.get("error_class") or "").strip().lower() in _UNCERTAIN_ERROR_CLASSES

    @staticmethod
    def _receipt_reason(receipt: Any) -> str:
        if not isinstance(receipt, dict):
            return "回执不是字典，无法确认送达"
        detail = (receipt.get("error") or receipt.get("reason")
                  or receipt.get("error_class") or "未返回成功回执")
        return str(detail)[:200]

    def _drop(self, candidate: Dict[str, Any], reason: str, uncertain: bool) -> Dict[str, Any]:
        share_id = str(candidate.get("share_id") or "")
        dropped = self._store.mark_dropped(share_id, reason, uncertain=uncertain)
        return {"sent": False, "share_id": share_id, "reason": reason,
                "uncertain": bool(uncertain),
                "candidate": dropped}

    async def deliver_due(self, now: Any = None) -> Dict[str, Any]:
        async with self._delivery_lock:
            return await self._deliver_due_locked(now)

    async def _deliver_due_locked(self, now: Any = None) -> Dict[str, Any]:
        at = self._store._now(now)
        self._store.prune(at)
        if self._preflight is not None:
            try:
                allowed, reason = self._preflight()
            except Exception:
                allowed, reason = False, "发送所需状态不完整"
            if not allowed:
                return {"sent": False, "reason": reason, "deferred": True}
        try:
            budget = self._budget_remaining()
        except Exception:
            budget = None
        allowed, reason = self._store.can_send(at, budget)
        if not allowed:
            return {"sent": False, "reason": reason}
        candidate = self._store.due_share(at)
        if not candidate:
            return {"sent": False, "reason": "无新鲜待分享候选"}
        if self._target_provider is not None:
            target = str(self._target_provider() or "")
            if not target or target != candidate.get("target_stream"):
                return self._drop(candidate, "候选对象未知或已变化，不转投其他联系人", False)
        text = build_share_text(candidate)
        if not text:
            return self._drop(candidate, "候选缺少有效 BV 号/可用理由", False)
        share_id = str(candidate["share_id"])
        if not self._store.claim_delivery(share_id, at):
            return {"sent": False, "reason": "候选已被领取或门控已变化"}
        try:
            receipt = await self._deliver(text)
        except asyncio.CancelledError:
            raise  # A durable sending intent is deliberately not retried after reload.
        except Exception as exc:
            return self._drop(candidate, f"投递异常，结果不确定（保守防重）：{str(exc)[:200]}", True)
        if isinstance(receipt, dict) and receipt.get("deferred") is True:
            self._store.defer_claim(share_id)
            return {"sent": False, "deferred": True, "reason": receipt.get("error")}
        if isinstance(receipt, dict) and receipt.get("success") is True and not receipt.get("uncertain"):
            sent = self._store.mark_sent(share_id, receipt)
            return {"sent": True, "share_id": share_id, "text": text,
                    "receipt": receipt, "candidate": sent}
        uncertain = self._receipt_uncertain(receipt)
        prefix = "投递结果不确定（保守防重）：" if uncertain else "投递失败："
        return self._drop(candidate, prefix + self._receipt_reason(receipt), uncertain)
