"""video_schedule —— 日程「刷视频」事件对内部观看端口的联动编排（B 计划 §6，D 线 integrations 场景文件）。

职责边界（模块解耦计划 §3.2）：
- 本模块回答「哪个事实触发、调用哪个公开能力、先后顺序、如何取消/降级」——
  episode 由滚动日程创建/推进/结算，本模块**不**复制日程权威状态，只保存
  episode ↔ task 的关联与观测结果（计划 §4：日程只保存任务关联）；
- 观看能力经注入的端口（core.contracts.VideoWatchPort 同构动作面）调用，
  **B3 起端口 = 本插件内部 video_watch 服务单实例**（装配侧 plugin._call_watch_api
  直接调 service.handle_action），不再跨插件 ctx.api.call("cateye.bili-watch...")；
  本模块不读 video_watch 的私有配置/任务目录；
- 观看能力不可用时如实标记「观看能力不可用」，绝不拖挂生活 tick
  （计划 §3：不阻塞其他日程）；
- 关联唯一：一个 episode 至多一个观看任务；每拍不重复提交；
- 活动结束/被抢占/取消 → 取消进行中的任务（共享任务取消语义由端口契约保证，
  不误杀其他调用者的任务）；晚到的真实完成结果照实存档
  （不重开 episode、不回填结束时间、不自行发消息）。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from core.coerce import to_float as _as_float, to_int as _as_int

#: 刷视频事件的 event 标识（event_catalog.BUILTIN_EVENTS / RANDOM_EVENT_POOL 同口径）
WATCH_EVENT_KEY = "watch_bili"
#: 链接库有界：closed 记录保留天数与条数上限（防无限增长）
_CLOSED_KEEP_DAYS = 7
_CLOSED_MAX_ENTRIES = 100

# ----------------------------------------------------------------------
# S1b 冲浪额度（冲浪计划书 §3.2/§3.3）：以下**均为建议默认值**（计划书初值，
# 非实测结论）；装配侧若要外化到配置，改常量取值即可，判定口径不变。
#: 每日自动观看（SURF）总场次上限
SURF_MAX_TOTAL_SESSIONS = 3
#: 每日自动观看总时长上限（分钟）
SURF_MAX_TOTAL_MINUTES = 30
#: 「间隔冲浪」（gap）每日场次上限
SURF_MAX_GAP_SESSIONS = 2
#: 「专属冲浪」（dedicated）每日场次上限
SURF_MAX_DEDICATED_SESSIONS = 2
#: gap 两场启动的最小间隔（秒）；60 分钟（计划书 §3.2 建议）
GAP_MIN_INTERVAL_SECONDS = 3600

ApiCaller = Callable[..., Awaitable[Dict[str, Any]]]
EpisodeLookup = Callable[[str], Optional[Dict[str, Any]]]
EpisodeIds = Callable[[], set]


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _default_day() -> str:
    """额度窗口的缺省日期：本地自然日（YYYY-MM-DD，计划书 §3.3）。"""
    return date.today().isoformat()


def _empty_quota(day: str) -> Dict[str, Any]:
    """当日账本零值（字段口径见计划书 §3.3）。"""
    return {"date": str(day), "sessions_used": 0, "minutes_used": 0.0,
            "gap_used": 0, "dedicated_used": 0, "last_start_epoch": None}


class SurfQuotaLedger:
    """冲浪额度账本（计划书 §3.3，S1b）：自然日窗口内的场次/时长/分场景计数。

    - 持久化 ``store_dir/surf_quota.json``（复用本文件 _atomic_write/_read）；
      重启后读文件即生效 = 保守恢复（不假设「新的一天」，不白送额度）；
    - 日切：日期变化即清零并回写（发生在跨日后的首次读取）；
    - 入账原子：先过全部上限，全过才写计数与 ``last_start_epoch``；
      任一不过则**不动账**并返回 False（消费方按拒绝处理，不等待）；
    - 本类只管账，不做注意力判断（租约在 companion.attention）。
    """

    #: 分场景计数字段：kind -> 账本字段
    _KIND_FIELD = {"gap": "gap_used", "dedicated": "dedicated_used"}

    def __init__(self, store_dir: Path, day_provider: Optional[Callable[[], str]] = None,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._path = Path(store_dir) / "surf_quota.json"
        self._day_provider = day_provider or _default_day  # 缺省：本地 date
        self._clock = clock
        self._lock = threading.RLock()  # reserve_once shares the same counter owner

    # ---- 内部 ----
    def _now(self, now: Any = None) -> float:
        if now is not None:
            return float(now)
        return float(self._clock() if callable(self._clock) else time.time())

    def _load(self) -> Dict[str, Any]:
        """读当日账本；缺失/损坏/跨日 → 零值并回写（调用方须已持锁）。"""
        day = str(self._day_provider())
        doc = _read(self._path, None)
        if isinstance(doc, dict) and str(doc.get("date") or "") == day:
            out = _empty_quota(day)
            out["sessions_used"] = max(0, _as_int(doc.get("sessions_used")))
            out["minutes_used"] = max(0.0, _as_float(doc.get("minutes_used")))
            out["gap_used"] = max(0, _as_int(doc.get("gap_used")))
            out["dedicated_used"] = max(0, _as_int(doc.get("dedicated_used")))
            last = doc.get("last_start_epoch")
            out["last_start_epoch"] = None if last is None else _as_float(last)
            if isinstance(doc.get("claims"), dict):
                out["claims"] = dict(doc["claims"])
            return out
        out = _empty_quota(day)  # 日切/首启：清零文案回写，重启后读到的即当日口径
        _atomic_write(self._path, out)
        return out

    # ---- 对外面 ----
    def snapshot(self) -> Dict[str, Any]:
        """当日账本只读快照（字段：date/sessions_used/minutes_used/gap_used/
        dedicated_used/last_start_epoch）。"""
        with self._lock:
            return dict(self._load())

    def try_consume(self, kind: str, minutes: float, now: Any = None) -> bool:
        """申请入账一场（kind ∈ "gap"/"dedicated"）：全部检查通过才入账并返回 True。

        检查口径：当日总场次、当日总分钟（本次时长须放得下）、分场景场次、
        gap 的 60 分钟启动间隔（对照 ``last_start_epoch``）；未知 kind 不入账。
        任一不过 → False 且账本不变（调用方按「本拍不启动」处理）。
        """
        kind = str(kind or "")
        field = self._KIND_FIELD.get(kind)
        if field is None:
            return False  # 未知类型：保守拒绝，不写账
        at = self._now(now)
        need_minutes = max(0.0, _as_float(minutes))
        with self._lock:
            doc = self._load()
            # 1) 余量门控（总场次/总时长/分场景/间隔）与纯函数同口径
            if surf_gate_check(kind, doc, at) is not None:
                return False
            # 2) 本场时长是否放得进当日剩余额度（纯函数无 minutes 入参，由账本校验）
            if float(doc["minutes_used"]) + need_minutes > SURF_MAX_TOTAL_MINUTES:
                return False
            doc["sessions_used"] = int(doc["sessions_used"]) + 1
            doc["minutes_used"] = round(float(doc["minutes_used"]) + need_minutes, 3)
            doc[field] = int(doc[field]) + 1
            doc["last_start_epoch"] = at
            _atomic_write(self._path, doc)
            return True

    def reserve_once(self, episode_id: str, kind: str, minutes: float) -> bool:
        """A durable claim prevents a reload between debit and task linking from replaying it."""
        with self._lock:
            doc = self._load()
            if episode_id in doc.get("claims", {}):
                return False
            previous = doc.get("last_start_epoch")
            if not self.try_consume(kind, minutes):
                return False
            doc = self._load()
            doc.setdefault("claims", {})[episode_id] = {
                "kind": kind, "minutes": minutes, "state": "reserved",
                "previous_start": previous, "started_at": self._now()}
            _atomic_write(self._path, doc)
            return True

    def refund_unstarted(self, episode_id: str) -> bool:
        """Only a confirmed rejected/cached submission can release a reserved session."""
        with self._lock:
            doc = self._load()
            claim = doc.get("claims", {}).get(episode_id)
            if not claim or claim.get("state") != "reserved":
                return False
            doc["sessions_used"] = max(0, doc["sessions_used"] - 1)
            field = self._KIND_FIELD[claim["kind"]]
            doc[field] = max(0, doc[field] - 1)
            doc["minutes_used"] = max(0.0, doc["minutes_used"] - claim["minutes"])
            claim["state"] = "not_started"
            _atomic_write(self._path, doc)
            return True


def surf_gate_check(kind: str, quota_snapshot: Dict[str, Any], now: float) -> Optional[str]:
    """SURF 启动门控（纯函数，计划书 §3.3/§4.2）：返回中文拒绝原因，None=放行。

    ``quota_snapshot`` 取自 :meth:`SurfQuotaLedger.snapshot`（当日账本快照）；
    本函数只判断「还有没有余量」，单场时长是否放得下由账本 try_consume 另查
    （签名不含 minutes）。判定口径与账本入账共用同一组常量，避免两处漂移。
    """
    kind = str(kind or "")
    if kind not in SurfQuotaLedger._KIND_FIELD:
        return f"未知冲浪类型「{kind}」（仅支持 gap / dedicated）"
    q = quota_snapshot if isinstance(quota_snapshot, dict) else {}
    used_n = _as_int(q.get("sessions_used"))
    used_m = _as_float(q.get("minutes_used"))
    gap_n = _as_int(q.get("gap_used"))
    ded_n = _as_int(q.get("dedicated_used"))
    at = _as_float(now)
    if used_n >= SURF_MAX_TOTAL_SESSIONS:
        return (f"今日自动观看场次已用完（{used_n}/{SURF_MAX_TOTAL_SESSIONS} 场，"
                f"剩余 0 场；时长还剩 {max(0.0, SURF_MAX_TOTAL_MINUTES - used_m):g} 分钟）")
    if used_m >= SURF_MAX_TOTAL_MINUTES:
        return (f"今日自动观看时长已用完（{used_m:g}/{SURF_MAX_TOTAL_MINUTES:g} 分钟，"
                f"剩余 0 分钟；场次还剩 {SURF_MAX_TOTAL_SESSIONS - used_n} 场）")
    if kind == "gap" and gap_n >= SURF_MAX_GAP_SESSIONS:
        return (f"今日「间隔冲浪」场次已用完（{gap_n}/{SURF_MAX_GAP_SESSIONS} 场，"
                f"剩余 0 场；总场次还剩 {SURF_MAX_TOTAL_SESSIONS - used_n} 场、"
                f"时长还剩 {max(0.0, SURF_MAX_TOTAL_MINUTES - used_m):g} 分钟）")
    if kind == "dedicated" and ded_n >= SURF_MAX_DEDICATED_SESSIONS:
        return (f"今日「专属冲浪」场次已用完（{ded_n}/{SURF_MAX_DEDICATED_SESSIONS} 场，"
                f"剩余 0 场；总场次还剩 {SURF_MAX_TOTAL_SESSIONS - used_n} 场、"
                f"时长还剩 {max(0.0, SURF_MAX_TOTAL_MINUTES - used_m):g} 分钟）")
    last = q.get("last_start_epoch")
    if kind == "gap" and last is not None:
        elapsed = at - _as_float(last)
        if elapsed < GAP_MIN_INTERVAL_SECONDS:
            wait_min = int(math.ceil((GAP_MIN_INTERVAL_SECONDS - elapsed) / 60.0))
            return (f"距上次自动观看不足 {GAP_MIN_INTERVAL_SECONDS // 60} 分钟"
                    f"（已过 {max(0, int(elapsed)) // 60} 分钟，还需约 {max(1, wait_min)} 分钟）")
    return None


class VideoWatchCoordinator:
    """刷视频 episode ↔ 公共观看接口的联动协调器（单飞 tick 调用）。"""

    def __init__(self, store_dir: Path, api_caller: ApiCaller,
                 get_active_episode: Callable[[], Optional[Dict[str, Any]]],
                 get_episode: EpisodeLookup,
                 logger: Any = None,
                 *, attention: Any = None, quota: Any = None,
                 share_recorder: Any = None) -> None:
        self._links_path = Path(store_dir) / "video_watch_links.json"
        self._api = api_caller
        #: S1b（冲浪计划书 §4.2/§3.3）注入的注意力租约/额度账本；None = 门控直放行，
        #: 保持既有测试/未装配场景行为（注意力权威由装配根传入，本模块不造单例）
        self._attention = attention
        self._quota = quota
        #: S3（冲浪计划书 §7）：观看完成结果 → 分享候选（装配根注入；None=不生成）
        self._share_recorder = share_recorder

        self._get_active_episode = get_active_episode
        self._get_episode = get_episode
        self._log = logger

    # ------------------------------------------------------------------
    def _load_links(self) -> Dict[str, Any]:
        payload = _read(self._links_path, {"links": {}})
        return payload if isinstance(payload, dict) and isinstance(payload.get("links"), dict) else {"links": {}}

    def _save_links(self, links: Dict[str, Any]) -> None:
        _atomic_write(self._links_path, links)

    def episode_state(self, episode_id: str) -> Dict[str, Any]:
        """供面板/诊断读取某 episode 的观看关联（只读快照，含来源与阶段）。"""
        links = self._load_links().get("links") or {}
        entry = links.get(episode_id)
        return dict(entry) if isinstance(entry, dict) else {}

    # ------------------------------------------------------------------
    async def sync(self) -> None:
        """每拍一次的联动推进（单飞 task 内执行；任何异常不外泄）。"""
        try:
            await self._sync_inner()
        except Exception as exc:
            if self._log:
                self._log.warning("刷视频联动推进失败（忽略，不影响其他日程）：%s", exc)

    async def _sync_inner(self) -> None:
        links_doc = self._load_links()
        links: Dict[str, Any] = links_doc.get("links") or {}
        changed = False

        # 1) 已链接 episode 的收尾：非 active（已结束/被抢占/取消）→ 取消任务 + 落账
        active = self._get_active_episode() or {}
        active_id = str(active.get("episode_id") or "")
        for episode_id, entry in list(links.items()):
            if entry.get("closed") or episode_id == active_id:
                continue
            row = self._get_episode(episode_id) or {}
            status = str(row.get("status") or "")
            if status in ("active", "planned", ""):
                continue  # 仍活着（planned 罕见：重启窗口），不动
            entry["closed"] = True
            entry["closed_reason"] = {"completed": "episode_completed",
                                      "interrupted": "episode_interrupted",
                                      "paused": "episode_preempted",
                                      "abandoned": "episode_abandoned"}.get(status, f"episode_{status}")
            task_id = str(entry.get("task_id") or "")
            if task_id and not entry.get("result") and str(entry.get("stage")) not in ("unavailable", "skipped", "failed_submit"):
                try:
                    # S0 所有权：日程收尾只释放自己的引用（owner=本 episode）；
                    # 同一任务若被用户/其他事件共享，端口侧只释放不误杀
                    receipt = await self._api(action="cancel", task_id=task_id,
                                              owner=f"schedule:{episode_id}")
                    entry["cancel_receipt"] = {"status": receipt.get("status"),
                                               "already_terminal": bool(receipt.get("already_terminal")),
                                               "released": bool(receipt.get("released"))}
                except Exception as exc:
                    entry["cancel_receipt"] = {"error": str(exc)[:200]}
            # S1b（计划书 §4.2）：episode 收尾即释放 SURF 租约（幂等；只释放自己的）
            if self._attention is not None and entry.get("lease"):
                try:
                    self._attention.release(f"schedule:{episode_id}")
                except Exception as exc:
                    if self._log:
                        self._log.warning("释放 SURF 租约失败（忽略）：%s", exc)
            # 晚到结果：取消后任务可能已完成（回执 already_terminal=completed）→ 照实存档
            await self._archive_if_completed(entry, task_id, episode_id=episode_id)
            entry.setdefault("finished_note", f"episode {status}，观看关联已收尾")
            changed = True

        # 2) 活动episode是刷视频事件 → 首次建链（选片 + 提交，回执先记）
        if active_id:
            metadata = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
            event_key = str(metadata.get("event_key") or "")
            entry = links.get(active_id)
            if event_key == WATCH_EVENT_KEY and not (
                    isinstance(entry, dict) and (entry.get("task_id") or entry.get("closed"))):
                entry = entry if isinstance(entry, dict) else {}
                links[active_id] = entry
                await self._start_watching(active_id, entry)
                changed = True
            # 3) 已链接进行中 → 有界状态轮询（每拍一次，不忙轮询）
            if isinstance(entry, dict) and entry.get("task_id") and not entry.get("closed") \
                    and str(entry.get("stage")) not in ("completed", "unavailable", "skipped", "failed_submit"):
                progressed = await self._poll_status(active_id, entry)
                if entry.get("closed"):
                    progressed = True  # 租约撤销即收尾：本拍必须落账（否则 stage 丢）
                changed = changed or progressed

        # 4) 链接库收口（有界）
        if changed:
            self._compact(links)
            links_doc["links"] = links
            self._save_links(links_doc)

    # ------------------------------------------------------------------
    def _release_surf(self, episode_id: str, entry: Dict[str, Any]) -> None:
        if self._attention is not None and entry.get("lease"):
            self._attention.release(f"schedule:{episode_id}")
        entry.pop("lease", None)

    async def _start_watching(self, episode_id: str, entry: Dict[str, Any]) -> None:
        entry.setdefault("started_at", time.time())
        reserved = False
        accepted = False
        phase = "candidate"
        retryable = False
        try:
            if self._attention is not None:
                lease = self._attention.try_acquire_surf(f"schedule:{episode_id}")
                if not lease:
                    retryable = True  # No side effect yet; a later quiet tick may try again.
                    entry.update(stage="skipped", reason="聊天占用或覆盖面未确立，不开始自动观看")
                    return
                entry["lease"] = lease
            pick = await self._api(action="next_candidate")
            candidate = pick.get("candidate") if isinstance(pick, dict) else None
            if not candidate:
                entry.update(stage="skipped", reason="无真实候选，不编造 BV 号")
                return
            if self._attention is not None and not self._attention.lease_valid(entry.get("lease")):
                entry.update(stage="skipped", reason="选片期间聊天开始")
                return
            if self._quota is not None:
                snap = self._quota.snapshot()
                minutes = min(20.0, max(0.0, SURF_MAX_TOTAL_MINUTES - float(snap["minutes_used"])))
                reserve = getattr(self._quota, "reserve_once", None)
                reserved = (reserve(episode_id, "dedicated", minutes) if callable(reserve)
                            else self._quota.try_consume("dedicated", minutes))
                if not reserved:
                    entry.update(stage="skipped", reason="自动观看额度不足或本事件已申请")
                    return
                entry["reserved_minutes"] = minutes
            phase = "submit"
            receipt = await self._api(action="submit", video=str(candidate["bvid"]),
                                      purpose="日程刷视频", episode_id=episode_id)
            if receipt.get("success") and receipt.get("cached"):
                # 旧观看记忆不是新任务，不登记分享、不伪造task_id、不占观看额度。
                if reserved and hasattr(self._quota, "refund_unstarted"):
                    self._quota.refund_unstarted(episode_id)
                entry.update(stage="cached_completed", cached=True, bvid=str(candidate["bvid"]),
                             result=receipt.get("result"), closed=True)
                return
            if not receipt.get("success") or not receipt.get("task_id"):
                entry.update(stage="failed_submit", reason=str(receipt.get("error") or "提交被拒绝")[:200],
                             error_class=receipt.get("error_class"))
                if reserved and hasattr(self._quota, "refund_unstarted"):
                    self._quota.refund_unstarted(episode_id)
                return
            entry.update(task_id=str(receipt["task_id"]), bvid=str(candidate["bvid"]),
                         stage=str(receipt.get("status") or "queued"),
                         cached=bool(receipt.get("cached")))
            accepted = True
        except Exception as exc:
            entry.update(stage="failed_submit" if phase == "submit" else "unavailable",
                         reason=("提交失败：" if phase == "submit" else "观看能力不可用：") + str(exc)[:160])
            # An uncertain submission is not refunded or retried.
        finally:
            if not accepted:
                entry["closed"] = not retryable
                self._release_surf(episode_id, entry)

    async def _poll_status(self, episode_id: str, entry: Dict[str, Any]) -> bool:
        """有界状态轮询；S1b 起每次先核对 SURF 租约（撤销即取消任务并收尾）。"""
        task_id = str(entry.get("task_id") or "")
        # S1b（计划书 §4.2）：直接对话让位 CHAT → 租约版本失效 → 不冒充仍在观看
        if self._attention is not None and entry.get("lease"):
            try:
                still_valid = bool(self._attention.lease_valid(entry["lease"]))
            except Exception:
                still_valid = False  # 权威异常按失效处理（保守：不确认在持就不继续看）
            if not still_valid:
                try:
                    receipt = await self._api(action="cancel", task_id=task_id,
                                              owner=f"schedule:{episode_id}")
                    entry["revoke_receipt"] = {"status": receipt.get("status")}
                except Exception as exc:
                    entry["revoke_receipt"] = {"error": str(exc)[:200]}
                entry["stage"] = "revoked_by_chat"
                entry["closed"] = True
                entry["closed_reason"] = "revoked_by_chat"
                if self._log:
                    self._log.info("刷视频事件 %s：SURF 租约被撤销，停止自动观看", episode_id)
                return False
        try:
            receipt = await self._api(action="status", task_id=task_id)
        except Exception as exc:
            entry["stage"] = "unavailable"
            entry["reason"] = f"观看能力不可用：{str(exc)[:200]}"
            return True
        if not receipt.get("success"):
            return False
        stage = str(receipt.get("status") or "")
        if entry.get("stage") != stage:
            entry["stage"] = stage
            if self._log and stage in ("running", "completed", "failed", "cancelled"):
                self._log.info("观看任务 %s 阶段：%s", task_id, stage)
            if stage in ("completed", "failed", "cancelled"):
                if stage == "completed":
                    await self._archive_if_completed(entry, task_id, episode_id=episode_id)
                self._release_surf(episode_id, entry)
                entry["closed"] = True
                return True
            return True
        return False

    async def _archive_if_completed(self, entry: Dict[str, Any], task_id: str,
                                    episode_id: str = "") -> None:
        """晚到/收尾时的真实完成结果存档（不重开 episode、不回填时间）。"""
        try:
            receipt = await self._api(action="result", task_id=task_id)
        except Exception:
            return
        # S3：真实完成结果交分享候选记录器（门在模块内：仅视觉理解成功且有摘要才成候选）
        recorder = self._share_recorder
        if callable(recorder) and isinstance(receipt, dict) and receipt.get("success")                 and isinstance(receipt.get("result"), dict):
            try:
                recorder(receipt["result"], str(episode_id or entry.get("episode_id") or ""))
            except Exception as exc:
                if self._log:
                    self._log.warning("分享候选记录失败（忽略）：%s", exc)
        if isinstance(receipt, dict) and receipt.get("success"):
            result = receipt.get("result")
            if isinstance(result, dict) and str(result.get("status")) == "completed":
                entry["result"] = {
                    "bvid": result.get("bvid") or (result.get("video") or {}).get("bvid"),
                    "title": (result.get("video") or {}).get("title"),
                    "watched_range": result.get("watched_range"),
                    "impression": result.get("impression"),
                    "visual_status": (result.get("visual") or {}).get("status"),
                    "finished_at": result.get("finished_at"),
                }

    # ------------------------------------------------------------------
    def _compact(self, links: Dict[str, Any]) -> None:
        now = time.time()
        stale = [eid for eid, e in links.items()
                 if isinstance(e, dict) and e.get("closed")
                 and now - float(e.get("started_at") or now) > _CLOSED_KEEP_DAYS * 86400]
        for eid in stale:
            links.pop(eid, None)
        closed = [eid for eid, e in links.items() if isinstance(e, dict) and e.get("closed")]
        if len(closed) > _CLOSED_MAX_ENTRIES:
            closed.sort(key=lambda eid: float(links[eid].get("started_at") or 0))
            for eid in closed[:len(closed) - _CLOSED_MAX_ENTRIES]:
                links.pop(eid, None)
