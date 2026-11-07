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

import asyncio
import json
import math
import os
import random
import threading
import time
import uuid
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

# ── 2026-11-04 刷视频功能批：空隙冲浪（gap）触发与候选准备（内部常量）──────
#: 空闲拍触发 gap 冲浪的概率（当前状态.md 遗留「空隙10%触发」；每冷却窗掷一次）
GAP_TRIGGER_PROBABILITY = 0.10
#: 两次 gap 掷点/重试的最小间隔（秒；防每拍刷屏掷点，与额度 60 分钟门叠加）
GAP_ROLL_COOLDOWN_SECONDS = 1800
#: gap 单场入账时长（分钟；比 dedicated 的 20 分钟短——空隙看看口径）
GAP_SESSION_MINUTES = 10.0
#: 候选准备（兴趣检查+公开接口抓取）总超时（秒；单飞 sync 内异步执行）
CANDIDATE_PREPARE_TIMEOUT_SECONDS = 120.0
#: 未提交成功的 gap 条目最大存活（秒；超龄收尾，防链接库滞留）
GAP_ENTRY_MAX_AGE_SECONDS = 3600

#: 观看关联的终态阶段（手动周期防叠加/状态快照判定用；与协调器收尾口径一致）
_TERMINAL_WATCH_STAGES = ("completed", "failed", "cancelled", "unavailable", "skipped",
                          "failed_submit", "revoked_by_chat", "cached_completed")
#: 管理员命令手动刷视频周期的轮询间隔/总等待上限余量（秒）
_MANUAL_POLL_INTERVAL_SECONDS = 5.0
_MANUAL_WAIT_MARGIN_SECONDS = 900.0

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
            # 2026-11-04 模块评估问题5：退款还原启动间隔锚点（claim 已记录
            # previous_start），gap 60 分钟门不再从被退款的申请时刻起算。
            # 保守守卫：若退款前已有更晚的真实启动（last_start_epoch 晚于本
            # claim 的登记时刻），不覆盖那次启动的锚点。
            started = claim.get("started_at")
            last = doc.get("last_start_epoch")
            if started is None or last is None or _as_float(last) <= _as_float(started):
                doc["last_start_epoch"] = claim.get("previous_start")
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
                 share_recorder: Any = None,
                 candidate_preparer: Optional[Callable[[], Awaitable[Any]]] = None,
                 frequency_refresh: Optional[Callable[[], None]] = None,
                 rng: Optional[random.Random] = None) -> None:
        self._links_path = Path(store_dir) / "video_watch_links.json"
        self._api = api_caller
        #: S1b（冲浪计划书 §4.2/§3.3）注入的注意力租约/额度账本；None = 门控直放行，
        #: 保持既有测试/未装配场景行为（注意力权威由装配根传入，本模块不造单例）
        self._attention = attention
        self._quota = quota
        #: S3（冲浪计划书 §7）：观看完成结果 → 分享候选（装配根注入；None=不生成）
        self._share_recorder = share_recorder
        #: 2026-11-04 刷视频功能批：候选准备器（兴趣 TTL 检查 + 公开排行榜抓取，
        #: 异步；在 next_candidate 同步 pick 之前执行）。None = 不准备（旧口径）
        self._candidate_preparer = candidate_preparer
        #: 联动状态变化后的回复倍率刷新回调（装配根 → proactive 频率链；
        #: 事件结束/收尾即恢复原倍率，不依赖任务终态单一触发源）
        self._frequency_refresh = frequency_refresh
        self._rng = rng or random.Random()
        #: 最近一次 gap 掷点/重试时刻（进程内；重载保守重置，额度门仍持久化把关）
        self._gap_last_attempt = 0.0
        #: 管理员命令手动刷视频周期的单飞 task（防叠加；进程内，重载即释放）
        self._manual_task: Optional[asyncio.Task] = None

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
    # 管理员命令：立即触发一次完整刷视频周期（候选→观看→完成→独立分享判断）
    # 复用既有 gap 链路，把随机掷点换成确定性触发；防叠加：已有活跃观看任务或
    # 周期在跑则不叠加。PR-WATCH：/lbm_plan watch 以 force_debug=True 强制并行——
    # 绕过注意力门控（不占聊天租约），额度/候选/防重/账号核验门仍全部保留。
    # ------------------------------------------------------------------
    def _active_watch_entry(self) -> Optional[tuple]:
        """只读找出当前活跃（非终态、未收尾）观看关联；无 → None。"""
        links = self._load_links().get("links") or {}
        for episode_id, entry in links.items():
            if not (isinstance(entry, dict) and entry.get("task_id") and not entry.get("closed")):
                continue
            if str(entry.get("stage") or "") in _TERMINAL_WATCH_STAGES:
                continue
            return episode_id, entry
        return None

    async def watch_status(self) -> Dict[str, Any]:
        """刷视频相关状态快照（当前任务/进度/预计结束时间），供命令防叠加提示。"""
        manual_running = self._manual_task is not None and not self._manual_task.done()
        active = self._active_watch_entry()
        if active is None and not manual_running:
            return {"active": False}
        out: Dict[str, Any] = {"active": True, "manual_cycle_running": manual_running}
        if active is not None:
            episode_id, entry = active
            task_id = str(entry.get("task_id") or "")
            started = float(entry.get("started_at") or 0)
            minutes = float(entry.get("reserved_minutes")
                            or (GAP_SESSION_MINUTES if entry.get("scenario") == "gap" else 20.0))
            out.update(episode_id=episode_id, task_id=task_id, bvid=entry.get("bvid"),
                       scenario=entry.get("scenario"), stage=entry.get("stage"))
            if started:
                out["eta_epoch"] = started + minutes * 60
            try:
                receipt = await self._api(action="status", task_id=task_id)
                if isinstance(receipt, dict) and receipt.get("success"):
                    out["stage"] = receipt.get("status") or out.get("stage")
                    if receipt.get("stage"):
                        out["progress"] = receipt.get("stage")
                    if receipt.get("detail"):
                        out["detail"] = str(receipt.get("detail"))[:120]
            except Exception:
                pass
        return out

    def start_manual_cycle(self, *, social_tick: Optional[Callable[[], Awaitable[Any]]] = None,
                           force_debug: bool = False) -> Dict[str, Any]:
        """命令入口（同步回执）：防叠加；空闲则后台启动一次完整周期。

        返回 ``{"started": bool, "reason": str}``；started=False 时调用方应改用
        :meth:`watch_status` 展示当前任务/进度/预计结束时间，不叠加新任务。

        PR-WATCH：``force_debug=True``（仅 /lbm_plan watch 调试命令路径置位）→
        与当前日程并行强制启动，绕过注意力门控（不占聊天租约）；额度/候选/
        证据/隐私/防重门不变。缺省 False = 旧语义（随机 gap/日程路径恒 False）。
        """
        if self._manual_task is not None and not self._manual_task.done():
            return {"started": False, "reason": "manual_cycle_running"}
        if self._active_watch_entry() is not None:
            return {"started": False, "reason": "watch_active"}
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return {"started": False, "reason": "no_event_loop"}
        self._manual_task = loop.create_task(self._manual_cycle(social_tick, force_debug))
        return {"started": True, "reason": ""}

    async def _manual_cycle(self, social_tick: Optional[Callable[[], Awaitable[Any]]],
                            force_debug: bool = False) -> None:
        """后台推动一次完整刷视频周期；任何异常不外泄（与 sync 同口径）。"""
        try:
            await self._manual_cycle_inner(social_tick, force_debug)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._log:
                self._log.warning("手动刷视频周期失败（忽略，不影响其他日程）：%s", exc)

    async def _manual_cycle_inner(self, social_tick: Optional[Callable[[], Awaitable[Any]]],
                                  force_debug: bool = False) -> None:
        links_doc = self._load_links()
        links: Dict[str, Any] = links_doc.get("links") or {}
        # gap- 前缀沿用空隙冲浪链路；manual_command 标记便于诊断区分。
        # PR-WATCH：force_debug 仅手动命令路径置位——跳过注意力租约、与日程并行；
        # forced_debug 标记供收尾/诊断辨识（日程恢复不收回 debug 任务）。
        episode_id = f"gap-cmd-{uuid.uuid4().hex[:12]}"
        entry: Dict[str, Any] = {"scenario": "gap", "manual_command": True}
        if force_debug:
            entry["forced_debug"] = True
        links[episode_id] = entry
        await self._start_watching(episode_id, entry, kind="gap", force_debug=force_debug)
        task_id = str(entry.get("task_id") or "")
        if not task_id:
            # 手动周期是一次性的：无任务（额度/候选门跳过或缓存命中；debug 路径
            # 不再因注意力跳过）即收尾，不像随机 gap 留待重试，避免非终态条目滞留。
            entry["closed"] = True
            entry.setdefault("closed_reason",
                             "forced_debug_no_task" if force_debug else "manual_no_task")
        links_doc["links"] = links
        self._compact(links)
        self._save_links(links_doc)
        if self._log:
            self._log.info("手动刷视频周期 %s 启动：stage=%s%s", episode_id, entry.get("stage"),
                           f" reason={entry.get('reason')}" if entry.get("reason") else "")
        if not task_id:
            # 无任务：如实记录，仍推动一次分享判断（即使不分享也写日志可验证）。
            if entry.get("cached") and isinstance(entry.get("result"), dict):
                self._record_manual_share(entry["result"], episode_id)
            await self._run_share_tick(social_tick, episode_id)
            return
        # 有界轮询直至终态（任务时长额度 + 余量）；与日程 sync 共用 links，收尾幂等。
        minutes = float(entry.get("reserved_minutes") or GAP_SESSION_MINUTES)
        deadline = time.time() + minutes * 60 + _MANUAL_WAIT_MARGIN_SECONDS
        while time.time() < deadline:
            await asyncio.sleep(_MANUAL_POLL_INTERVAL_SECONDS)
            fresh = (self._load_links().get("links") or {}).get(episode_id)
            if not isinstance(fresh, dict):
                break
            if fresh.get("closed") or str(fresh.get("stage") or "") in _TERMINAL_WATCH_STAGES:
                entry = fresh
                break
            await self._poll_status(episode_id, fresh)
            links_doc = self._load_links()
            links_doc.setdefault("links", {})[episode_id] = fresh
            self._save_links(links_doc)
            entry = fresh
            if entry.get("closed"):
                break
        if self._log:
            self._log.info("手动刷视频周期 %s 观看收尾：stage=%s closed=%s",
                           episode_id, entry.get("stage"), bool(entry.get("closed")))
        # 完成→独立分享判断（即使决定不分享也写日志可验证）。
        await self._run_share_tick(social_tick, episode_id)

    def _record_manual_share(self, result: Dict[str, Any], episode_id: str) -> None:
        recorder = self._share_recorder
        if callable(recorder):
            try:
                recorder(result, episode_id)
            except Exception as exc:
                if self._log:
                    self._log.warning("手动周期分享候选记录失败（忽略）：%s", exc)

    async def _run_share_tick(self, social_tick: Optional[Callable[[], Awaitable[Any]]],
                              episode_id: str) -> None:
        if social_tick is None:
            if self._log:
                self._log.info("手动刷视频周期 %s：独立分享服务未装配，跳过分享判断", episode_id)
            return
        try:
            result = await social_tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._log:
                self._log.warning("手动刷视频周期 %s 分享判断失败（忽略）：%s", episode_id, exc)
            return
        if self._log:
            self._log.info("手动刷视频周期 %s 独立分享判断结果：%s", episode_id,
                           json.dumps(result, ensure_ascii=False, default=str)[:400])

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
            if entry.get("scenario") == "gap":
                continue  # gap 伪 episode 无日程行，收尾由下方 gap 专属轮询负责
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

        # 3.5) gap 冲浪条目轮询（2026-11-04 刷视频功能批）：日程恢复即收尾；
        #      否则与 dedicated 同口径核对租约/状态，终态存档并释放租约
        for gap_id, entry in list(links.items()):
            if not (isinstance(entry, dict) and entry.get("scenario") == "gap"
                    and entry.get("task_id") and not entry.get("closed")):
                continue
            # PR-WATCH：forced_debug（/lbm_plan watch）与日程并行——日程恢复不收回
            # debug 任务（其引用身份为 debug:{episode_id}，schedule owner 也不匹配）；
            # 照常轮询至终态存档。普通 gap 条目语义不变（日程恢复即收尾）。
            if active_id and not entry.get("forced_debug"):
                try:
                    receipt = await self._api(action="cancel", task_id=str(entry["task_id"]),
                                              owner=f"schedule:{gap_id}")
                    entry["cancel_receipt"] = {"status": receipt.get("status"),
                                               "already_terminal": bool(receipt.get("already_terminal")),
                                               "released": bool(receipt.get("released"))}
                except Exception as exc:
                    entry["cancel_receipt"] = {"error": str(exc)[:200]}
                entry["closed"] = True
                entry["closed_reason"] = "schedule_resumed"
                self._release_surf(gap_id, entry)
                await self._archive_if_completed(entry, str(entry["task_id"]), episode_id=gap_id)
                changed = True
                continue
            progressed = await self._poll_status(gap_id, entry)
            if entry.get("closed"):
                progressed = True
            changed = changed or progressed

        # 3.6) 空闲 gap 触发（无活动 episode 才掷点；概率/冷却/额度三重门）
        if not active_id:
            changed = await self._maybe_gap_surf(links) or changed

        # 4) 链接库收口（有界）
        if changed:
            self._compact(links)
            links_doc["links"] = links
            self._save_links(links_doc)
            # 状态变化（含事件收尾/租约释放）→ 刷新回复倍率：刷视频期间下调，
            # 结束/切走即恢复当前活动原有倍率（proactive 频率链重解析，不写回 1.0）
            if self._frequency_refresh is not None:
                try:
                    self._frequency_refresh()
                except Exception as exc:
                    if self._log:
                        self._log.warning("刷视频联动后的频率刷新失败（忽略）：%s", exc)

    # ------------------------------------------------------------------
    def _release_surf(self, episode_id: str, entry: Dict[str, Any]) -> None:
        if self._attention is not None and entry.get("lease"):
            self._attention.release(f"schedule:{episode_id}")
        entry.pop("lease", None)

    async def _start_watching(self, episode_id: str, entry: Dict[str, Any],
                              kind: str = "dedicated", *, force_debug: bool = False) -> None:
        entry.setdefault("started_at", time.time())
        # 场景标记（S2 频率倍率读 entry["scenario"]：dedicated=0.45 / gap=0.70；
        # 2026-11-04 前此处未写标记，dedicated 曾被误读成 gap 倍率）
        entry["scenario"] = "gap" if kind == "gap" else "dedicated"
        reserved = False
        accepted = False
        phase = "candidate"
        retryable = False
        try:
            # PR-WATCH：force_debug（/lbm_plan watch 调试命令）不申请 SURF 租约——
            # 与当前日程/聊天并行，不占注意力；无租约则收尾 _release_surf 空操作。
            # 随机 gap/日程 dedicated 路径 force_debug 恒 False，注意力语义不变。
            if self._attention is not None and not force_debug:
                lease = self._attention.try_acquire_surf(f"schedule:{episode_id}")
                if not lease:
                    retryable = True  # No side effect yet; a later quiet tick may try again.
                    entry.update(stage="skipped", reason="聊天占用或覆盖面未确立，不开始自动观看")
                    return
                entry["lease"] = lease
            # 候选准备（兴趣 TTL 检查 + 公开排行榜抓取；失败不阻断——next_candidate
            # 会如实返回 no_candidate 原因，绝不编造 BV 号）
            if self._candidate_preparer is not None:
                try:
                    await asyncio.wait_for(self._candidate_preparer(),
                                           timeout=CANDIDATE_PREPARE_TIMEOUT_SECONDS)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._log:
                        self._log.warning("候选准备失败（按无候选如实处理）：%s", exc)
            pick = await self._api(action="next_candidate")
            candidate = pick.get("candidate") if isinstance(pick, dict) else None
            if not candidate:
                reason = str(pick.get("reason") or "") if isinstance(pick, dict) else ""
                entry.update(stage="skipped",
                             reason=("无真实候选，不编造 BV 号"
                                     + (f"（{reason}）" if reason else "")))
                return
            if (not force_debug and self._attention is not None
                    and not self._attention.lease_valid(entry.get("lease"))):
                entry.update(stage="skipped", reason="选片期间聊天开始")
                return
            if self._quota is not None:
                snap = self._quota.snapshot()
                session_minutes = GAP_SESSION_MINUTES if kind == "gap" else 20.0
                minutes = min(session_minutes,
                              max(0.0, SURF_MAX_TOTAL_MINUTES - float(snap["minutes_used"])))
                reserve = getattr(self._quota, "reserve_once", None)
                reserved = (reserve(episode_id, kind, minutes) if callable(reserve)
                            else self._quota.try_consume(kind, minutes))
                if not reserved:
                    entry.update(stage="skipped", reason="自动观看额度不足或本事件已申请")
                    return
                entry["reserved_minutes"] = minutes
            phase = "submit"
            # PR-WATCH：debug 强制路径以 source="debug" 送达观看服务——submit 注意力闸
            # 与运行期守卫因 source!="schedule" 自然放行；端口缺省仍 schedule（其余路径不变）。
            receipt = await self._api(action="submit", video=str(candidate["bvid"]),
                                      purpose=("空隙刷视频" if kind == "gap" else "日程刷视频"),
                                      episode_id=episode_id,
                                      **({"source": "debug"} if force_debug else {}))
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

    # ------------------------------------------------------------------
    # gap 冲浪（2026-11-04 刷视频功能批：SurfQuotaLedger gap 类型消费接线）
    # ------------------------------------------------------------------
    def open_gap_entry(self) -> Dict[str, Any]:
        """当前进行中的 gap 关联只读快照（供回复倍率链读租约/任务；无 → {}）。"""
        links = self._load_links().get("links") or {}
        for gap_id, entry in links.items():
            if (isinstance(entry, dict) and entry.get("scenario") == "gap"
                    and entry.get("task_id") and not entry.get("closed")):
                out = dict(entry)
                out["episode_id"] = gap_id
                return out
        return {}

    async def _maybe_gap_surf(self, links: Dict[str, Any]) -> bool:
        """空闲拍尝试 gap 冲浪：返回链接库是否变化（调用方已确认无活动 episode）。

        门序：已有进行中 gap → 不动；未提交成功的旧 gap 条目 → 冷却后重试
        （超龄收尾）；否则冷却窗内掷点一次（10%）→ 命中才建链。额度/注意力/
        候选门在 _start_watching 内与 dedicated 同口径（kind="gap"）。
        """
        now = time.time()
        changed = False
        # 手动刷视频周期在跑 → 不再随机掷点叠加（防叠加；周期自身已占观看链路）。
        if self._manual_task is not None and not self._manual_task.done():
            return False
        has_running = False
        retryable: list = []
        for gap_id, entry in links.items():
            if not (isinstance(entry, dict) and entry.get("scenario") == "gap"
                    and not entry.get("closed")):
                continue
            if entry.get("task_id"):
                has_running = True
            elif not entry.get("manual_command"):
                # 手动周期的候选准备条目由命令 task 独占，不当作随机 gap 重试。
                retryable.append((gap_id, entry))
        if has_running:
            return False
        for gap_id, entry in retryable:
            if now - float(entry.get("started_at") or now) > GAP_ENTRY_MAX_AGE_SECONDS:
                entry["closed"] = True
                entry["closed_reason"] = "gap_expired"
                changed = True
        retryable = [(gid, e) for gid, e in retryable if not e.get("closed")]
        if now - self._gap_last_attempt < GAP_ROLL_COOLDOWN_SECONDS:
            return changed
        self._gap_last_attempt = now
        if retryable:
            gap_id, entry = retryable[0]
            await self._start_watching(gap_id, entry, kind="gap")
            return True
        try:
            hit = self._rng.random() < GAP_TRIGGER_PROBABILITY
        except Exception:
            hit = False
        if not hit:
            return changed
        gap_id = f"gap-{uuid.uuid4().hex[:12]}"
        entry = {"scenario": "gap"}
        links[gap_id] = entry
        await self._start_watching(gap_id, entry, kind="gap")
        return True

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
        # 真实观看事实交独立分享准入；视觉或公开字幕可评估，不唤起分享Planner。
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
