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
- 关联唯一：同一 episode 在同一时刻只推进一个观看任务；可串行续投多支，不重复提交；
- 专属观看按 25–55 分钟口径约束单场预算，同时核对日程实际剩余时间；完成事件
  驱动续投，不等待生活 tick。生产单段最多 10 支，达到配置支数/时长或 episode
  结束即停止；同场只首次预留额度，结束时按真实任务处理用时结算；
- 独立 casual 路径只在明确闲暇时为每个原日程意图持久化一次概率结果，最多看一支；
  恢复与跨日沿用原意图身份及累计支数；无原计划身份的小事件回退原 episode 链身份；
  无日程的 gap 路径保留自己的冷却和每日额度，三类均受自主开关与注意力门守护；
- 活动结束/被抢占/聊天让位 → 释放本 episode 的自动 owner，不终止已受理任务
  或误杀共享任务；晚到的真实完成结果照实存档（不重开 episode、不回填结束时间、
  不自行发消息）。用户显式取消仍按观看服务的任务取消契约执行。
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
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from core.coerce import to_float as _as_float, to_int as _as_int
from integrations.video_autonomy import (config_value,quota_limits,episode_watch_kind,casual_eligible,EpisodeOpportunities)
from integrations.video_watch_budget import IntentWatchLedger,watch_budget_identity

#: 刷视频事件的 event 标识（event_catalog.BUILTIN_EVENTS / RANDOM_EVENT_POOL 同口径）
WATCH_EVENT_KEY = "watch_bili"
#: 日程刷视频单场目标时长（与 rolling_planner 的 25–55 分钟事件时长同口径；
#: 只用于 episode 内续投与额度记账，不复制/修改事件池定义）。
WATCH_SESSION_MIN_MINUTES = 25
WATCH_SESSION_MAX_MINUTES = 55
#: task_history 保留条数（有界；每续投一支归档一条旧任务摘要）
WATCH_CONTINUE_MAX_TASKS = 8
#: 未注入配置的旧独立调用提交上限；生产读取 video_autonomous.schedule_max_videos
#: （默认 10）。同场还受 episode 结束及实际剩余时间/单场预算约束。
WATCH_CONTINUE_MAX_SUBMITS = 20
#: 续投/首次选片时，因「候选时长 > 剩余预算」而跳过的最多候选数（防一次抽干候选池）
WATCH_CANDIDATE_FIT_ATTEMPTS = 5
#: 脱离 owner 后轻量轮询的上限次数（约 4 小时/5 分钟拍；覆盖默认任务总时限 3h，
#: 保证 detached 任务仍能轮询到终态，同时防止永久滞留）
_DETACHED_MAX_POLLS = 48
#: 链接库有界：closed 记录保留天数与条数上限（防无限增长）
_CLOSED_KEEP_DAYS = 7
_CLOSED_MAX_ENTRIES = 100

# ----------------------------------------------------------------------
# SURF_* 的 3/120/2 保留旧独立调用的兼容口径，不是生产配置默认。
# 实际额度由 video_autonomy.quota_limits 与注入的配置 provider 统一解析；
# plugin 装配读取 video_autonomous，生产默认总 8 场/240 分钟、专属 4 场。
# 以下兼容数值不作为生产调参入口；单场 dedicated 时长常量仍用于预算夹取。
#: 旧独立调用兼容总场次值（生产使用 daily_session_limit）
SURF_MAX_TOTAL_SESSIONS = 3
#: 旧独立调用兼容总分钟值（生产使用 daily_minutes_limit，默认 240）；
#: 同场预留与结算由 SurfQuotaLedger 负责，续投不会重复申请一场。
SURF_MAX_TOTAL_MINUTES = 120
#: 单场 dedicated 的入账上限（分钟）：与 watch_bili episode 的 25–55 分钟同口径；
#: 实际入账取 episode 计划时长（25–55），不足 55 时不按 55 虚记。
SURF_MAX_DEDICATED_MINUTES = WATCH_SESSION_MAX_MINUTES
#: 旧独立调用兼容 gap 场次值（生产使用 daily_gap_limit）
SURF_MAX_GAP_SESSIONS = 2
#: 旧独立调用兼容 dedicated 场次值（生产使用 daily_dedicated_limit）
SURF_MAX_DEDICATED_SESSIONS = 2
#: 旧独立调用兼容 gap 间隔值；生产使用 gap_min_interval_minutes（默认 60）
GAP_MIN_INTERVAL_SECONDS = 3600

# ── 2026-11-04 刷视频功能批：空隙冲浪（gap）触发与候选准备（内部常量）──────
#: 空闲拍触发 gap 冲浪的概率（当前状态.md 遗留「空隙10%触发」；每冷却窗掷一次）
GAP_TRIGGER_PROBABILITY = 0.10
#: 两次 gap 掷点/重试的最小间隔（秒；防每拍刷屏掷点，与额度 60 分钟门叠加）
GAP_ROLL_COOLDOWN_SECONDS = 1800
#: gap 单场预留时长（分钟）；独立 casual 按候选时长及日程剩余时间预留
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
            "gap_used": 0, "dedicated_used": 0, "casual_used":0, "last_start_epoch": None}


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
    _KIND_FIELD = {"gap": "gap_used", "dedicated": "dedicated_used", "casual":"casual_used"}

    def __init__(self, store_dir: Path, day_provider: Optional[Callable[[], str]] = None,
                 clock: Optional[Callable[[], float]] = None, config_provider: Any = None) -> None:
        self._path = Path(store_dir) / "surf_quota.json"
        self._day_provider = day_provider or _default_day  # 缺省：本地 date
        self._clock = clock
        self._config_provider=config_provider
        self._lock = threading.RLock()  # reserve_once shares the same counter owner

    def limits(self):
        try:config=self._config_provider() if callable(self._config_provider) else None
        except Exception:config=None
        return quota_limits(config,legacy=self._config_provider is None)

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
            out["casual_used"] = max(0, _as_int(doc.get("casual_used")))
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
        dedicated_used/casual_used/last_start_epoch；另含逐 episode claims）。"""
        with self._lock:
            return dict(self._load())

    def _reserve_minutes(self, doc: dict, kind: str, minutes: float, at: float) -> bool:
        field = self._KIND_FIELD.get(kind)
        limits = self.limits()
        if field is None or surf_gate_check(kind, doc, at, limits=limits) is not None:
            return False
        if doc["minutes_used"] + minutes > limits["daily_minutes_limit"]:
            return False
        doc["sessions_used"] += 1
        doc["minutes_used"] = round(doc["minutes_used"] + minutes, 3)
        doc[field] += 1
        doc["last_start_epoch"] = at
        return True

    def try_consume(self, kind: str, minutes: float, now: Any = None) -> bool:
        with self._lock:
            doc = self._load()
            if not self._reserve_minutes(doc, kind, max(0.0, _as_float(minutes)), self._now(now)):
                return False
            _atomic_write(self._path, doc)
            return True

    def reserve_once(self, episode_id: str, kind: str, minutes: float) -> bool:
        """Persist the debit and its owner in the same atomic replacement."""
        with self._lock:
            doc = self._load()
            claims = doc.setdefault("claims", {})
            if episode_id in claims:
                return False
            at = self._now()
            previous = doc.get("last_start_epoch")
            amount = max(0.0, _as_float(minutes))
            if not self._reserve_minutes(doc, kind, amount, at):
                return False
            claims[episode_id] = {"kind": kind, "minutes": amount, "state": "reserved",
                                  "previous_start": previous, "started_at": at}
            _atomic_write(self._path, doc)
            return True

    def claim_snapshot(self, episode_id: str) -> Dict[str, Any]:
        """某 episode 的入账 claim 只读快照（诊断/结算判定；无 → {}）。"""
        with self._lock:
            claim = self._load().get("claims", {}).get(str(episode_id or ""))
            return dict(claim) if isinstance(claim, dict) else {}

    def _unwind_reserved(self, doc: Dict[str, Any], claim: Dict[str, Any],
                         *, state: str, keep_minutes: float = 0.0) -> None:
        """回退 state=reserved 的 claim（调用方须已持锁且已校验状态）。

        ``keep_minutes`` = 实际观看分钟：场次保留、时长按差额回收；
        0 = 一场没看，与全额退款同口径（场次与分钟都退）。
        """
        reserved = max(0.0, _as_float(claim.get("minutes")))
        actual = max(0.0, min(reserved, _as_float(keep_minutes)))
        if actual <= 0.0:
            doc["sessions_used"] = max(0, int(doc["sessions_used"]) - 1)
            field = self._KIND_FIELD.get(str(claim.get("kind") or ""))
            if field is not None:
                doc[field] = max(0, int(doc[field]) - 1)
            doc["minutes_used"] = max(0.0, float(doc["minutes_used"]) - reserved)
            # 2026-11-04 模块评估问题5：整场退款还原启动间隔锚点（claim 已记录
            # previous_start），gap 配置间隔不再从被退款的申请时刻起算。
            # 保守守卫：若此前已有更晚的真实启动（last_start_epoch 晚于本 claim
            # 的登记时刻），不覆盖那次启动的锚点。
            started = claim.get("started_at")
            last = doc.get("last_start_epoch")
            if started is None or last is None or _as_float(last) <= _as_float(started):
                doc["last_start_epoch"] = claim.get("previous_start")
        else:
            # 实际看过一部分：场次与启动锚点都保留（gap 间隔仍从真实启动起算）。
            doc["minutes_used"] = max(
                0.0, round(float(doc["minutes_used"]) - (reserved - actual), 3))
        claim["state"] = state
        claim["settled_minutes"] = actual

    def refund_unstarted(self, episode_id: str) -> bool:
        """Only a confirmed rejected/cached submission can release a reserved session."""
        with self._lock:
            doc = self._load()
            claim = doc.get("claims", {}).get(episode_id)
            if not claim or claim.get("state") != "reserved":
                return False
            self._unwind_reserved(doc, claim, state="not_started")
            _atomic_write(self._path, doc)
            return True

    def settle(self, episode_id: str, watched_minutes: float,
               now: Any = None, *, expected_day: str = "") -> bool:
        """按实际观看结算一场已预留的额度（取消/失败/中断，幂等）。

        - only ``state=reserved`` is settled; a second call returns False and never
          charges twice;
        - ``watched_minutes`` 夹到 ``[0, reserved]``：看满保留全额，部分观看回收差额，
          一场没看按全额退款（场次/分钟都退）；
        - 场次计数保留（只要实际看过），与「本场已发起」口径一致。
        """
        episode_id = str(episode_id or "")
        with self._lock:
            doc = self._load()
            if expected_day and doc["date"] != expected_day:
                return False
            claim = doc.get("claims", {}).get(episode_id)
            if not claim or claim.get("state") != "reserved":
                return False
            self._unwind_reserved(doc, claim, state="settled",
                                  keep_minutes=watched_minutes)
            claim["settled_at"] = self._now(now)
            _atomic_write(self._path, doc)
            return True


def surf_gate_check(kind: str, quota_snapshot: Dict[str, Any], now: float, *, limits=None) -> Optional[str]:
    """One quota interpretation for the ledger and its coordinator, including casual watches."""
    limits=limits or quota_limits(legacy=True)
    q=quota_snapshot if isinstance(quota_snapshot,dict) else {}
    if kind not in SurfQuotaLedger._KIND_FIELD:return '未知冲浪类型'
    sessions=_as_int(q.get('sessions_used'));minutes=_as_float(q.get('minutes_used'))
    if sessions>=limits['daily_session_limit']:return f"今日自动观看场次已用完（{sessions}/{limits['daily_session_limit']} 场）"
    if minutes>=limits['daily_minutes_limit']:return f"今日自动观看时长已用完（{minutes:g}/{limits['daily_minutes_limit']} 分钟）"
    field=SurfQuotaLedger._KIND_FIELD[kind]
    cap=limits[{'gap':'daily_gap_limit','dedicated':'daily_dedicated_limit','casual':'schedule_casual_daily_limit'}[kind]]
    used=_as_int(q.get(field))
    if used>=cap:return f"今日{kind}观看场次已用完（{used}/{cap} 场）"
    last=q.get('last_start_epoch');interval=limits['gap_min_interval_minutes']*60
    if kind=='gap' and last is not None and float(now)-_as_float(last)<interval:
        return f"距上次自动观看不足 {limits['gap_min_interval_minutes']} 分钟"
    return None


class VideoWatchCoordinator:
    """刷视频 episode ↔ 公共观看接口协调器；完成事件、短轮询与生活 tick 单飞推进。"""

    def __init__(self, store_dir: Path, api_caller: ApiCaller,
                 get_active_episode: Callable[[], Optional[Dict[str, Any]]],
                 get_episode: EpisodeLookup,
                 logger: Any = None,
                 *, attention: Any = None, quota: Any = None,
                 share_recorder: Any = None,
                 candidate_preparer: Optional[Callable[[], Awaitable[Any]]] = None,
                 frequency_refresh: Optional[Callable[[], None]] = None,
                 rng: Optional[random.Random] = None,
                 clock: Optional[Callable[[], float]] = None,
                 config_provider: Any = None, automatic_guard: Any = None,
                 episode_remaining: Any = None) -> None:
        self._links_path = Path(store_dir) / "video_watch_links.json"
        self._opportunities=EpisodeOpportunities(Path(store_dir)/"video_episode_opportunities.json")
        self._intent_usage=IntentWatchLedger(Path(store_dir)/"video_intent_usage.sqlite3")
        self._budget_seeded=set()
        self._config_provider=config_provider
        self._automatic_guard=automatic_guard
        self._episode_remaining=episode_remaining
        self._sync_lock=asyncio.Lock()
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
        #: 时间源（默认 time.time；测试可注入以验证 episode 内 55 分钟预算停止）
        self._clock = clock
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
    # 周期在跑则不叠加。PR-WATCH：/lbm plan watch 以 force_debug=True 强制并行——
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
                            or (GAP_SESSION_MINUTES if entry.get("scenario") == "gap"
                                else SURF_MAX_DEDICATED_MINUTES))
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

        PR-WATCH：``force_debug=True``（仅 /lbm plan watch 调试命令路径置位）→
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
        """单飞联动推进；完成事件可立即再推进，异常隔离，不等待生活 tick。"""
        try:
            async with self._sync_lock:
                await self._sync_inner()
        except Exception as exc:
            if self._log:
                self._log.warning("刷视频联动推进失败（忽略，不影响其他日程）：%s", exc)

    async def _sync_inner(self) -> None:
        links_doc = self._load_links()
        links: Dict[str, Any] = links_doc.get("links") or {}
        changed = self._reconcile_budget_receipts(links)

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
            result = (await self._archive_if_completed(
                entry, task_id, episode_id=episode_id)) if task_id else None
            self._accumulate_watch(
                entry, result,
                assume_completed=str(entry.get("stage") or "") in
                ("completed", "cached_completed"))
            # 中断/抢占/放弃按真实任务用时结算（未开始则退款）；自然结束不在此重复结算。
            # 这里只释放自动 owner，已受理任务仍可继续处理，由晚到轮询补归档。
            if entry.get("result"):
                self._settle_quota(episode_id, entry, reason=f"episode_{status}")
            # 复核修复（3.17.33）：释放 owner 不等于任务终止（manager 继续跑完）——
            # 保留轻量轮询到终态，晚到完成的分享候选/结果仍能归档。
            if self._should_detach_poll(entry, task_id):
                entry["detached"] = True
                entry["detached_skip_once"] = True
            entry.setdefault("finished_note", f"episode {status}，观看关联已收尾")
            changed = True

        # 2) 活动episode是刷视频事件 → 首次建链（选片 + 提交，回执先记）
        if active_id:
            metadata = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
            event_key = str(metadata.get("event_key") or "")
            entry = links.get(active_id)
            if self._enabled() and episode_watch_kind(active)=="dedicated":
                if not isinstance(entry, dict):
                    entry = {}
                    links[active_id] = entry
                if not entry.get("task_id") and not entry.get("closed"):
                    await self._start_watching(
                        active_id, entry,
                        session_minutes=self._episode_watch_minutes(active))
                    changed = True
                elif entry.get("closed") and self._can_continue_watching(entry):
                    # 复核修复（3.17.33）：单个视频看完 ≠ episode 结束——在仍活跃的
                    # watch_bili episode 内续投下一支候选，实际观看连续到 25–55 分钟。
                    await self._continue_watching(active_id, entry, active)
                    changed = True
            elif self._enabled() and not isinstance(entry,dict) and casual_eligible(active):
                if config_value(self._config(),'schedule_casual_enabled',True) and self._casual_context_ready(active_id):
                    probability=max(0.0,min(1.0,_as_float(config_value(self._config(),'schedule_casual_probability',.10))))
                    hit,budget_key=self._roll_intent_once(active_id,active,links,probability)
                    if hit is not True and self._attention is not None:self._attention.release(f'schedule:{active_id}')
                    if hit is not None:
                        entry={'scenario':'casual','budget_key':budget_key,'started_at':self._now(),'closed':not hit,
                               'stage':'opportunity_missed' if not hit else 'preparing'}
                        links[active_id]=entry;changed=True
                        if hit:
                            remaining=self._remaining_episode(active)
                            await self._start_watching(active_id,entry,kind='casual',session_minutes=remaining or GAP_SESSION_MINUTES)
            # 3) 已链接任务 → 本次同步只查一次状态，完成事件可另触发下一次同步
            if isinstance(entry, dict) and entry.get("task_id") and not entry.get("closed") \
                    and str(entry.get("stage")) not in ("completed", "unavailable", "skipped", "failed_submit"):
                progressed = await self._poll_status(active_id, entry)
                if entry.get("closed"):
                    progressed = True  # 租约撤销即收尾：本拍必须落账（否则 stage 丢）
                # 本拍刚看完且仍在 episode 预算内 → 立即续投下一支（不等下一拍），
                # 让 25–55 分钟由多支视频的实际连续观看填满。
                if entry.get("closed") and self._can_continue_watching(entry):
                    await self._continue_watching(active_id, entry, active)
                    progressed = True
                if entry.get("closed") and entry.get("closed_reason")=="task_completed" and not self._can_continue_watching(entry):
                    self._settle_quota(active_id,entry,reason="session_completed")
                changed = changed or progressed

        # 3.5) gap 冲浪条目轮询（2026-11-04 刷视频功能批）：日程恢复即收尾；
        #      否则与 dedicated 同口径核对租约/状态，终态存档并释放租约
        for gap_id, entry in list(links.items()):
            if not (isinstance(entry, dict) and entry.get("scenario") == "gap"
                    and entry.get("task_id") and not entry.get("closed")):
                continue
            # PR-WATCH：forced_debug（/lbm plan watch）与日程并行——日程恢复不收回
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
                archived = await self._archive_if_completed(entry, str(entry["task_id"]), episode_id=gap_id)
                if archived is not None:
                    self._accumulate_watch(entry, archived)
                    self._settle_quota(gap_id, entry, reason="session_completed")
                if self._should_detach_poll(entry, str(entry["task_id"])):
                    entry["detached"] = True
                    entry["detached_skip_once"] = True
                changed = True
                continue
            progressed = await self._poll_status(gap_id, entry)
            if entry.get("closed"):
                progressed = True
            changed = changed or progressed

        # 3.6) 空闲 gap 触发（无活动 episode 才掷点；概率/冷却/额度三重门）
        if not active_id and self._enabled():
            changed = await self._maybe_gap_surf(links) or changed

        # 3.7) detached 轻量轮询：租约撤销/日程收尾后任务由 manager 继续跑完，
        #      终态时补归档（不发新候选、不再占注意力/额度）。
        for episode_id, entry in list(links.items()):
            if not (isinstance(entry, dict) and entry.get("detached")
                    and entry.get("task_id")):
                continue
            progressed = await self._poll_detached(episode_id, entry)
            changed = changed or progressed

        # 4) 链接库收口（有界）
        # Reconcile terminal links written by earlier versions or a failed settlement write.
        for eid, entry in links.items():
            if (isinstance(entry, dict) and entry.get("closed") and not entry.get("detached")
                    and entry.get("reserved_minutes") and entry.get("result")
                    and not entry.get("submit_uncertain")
                    and (eid != active_id or not self._can_continue_watching(entry))):
                changed = self._settle_quota(eid, entry, reason="session_completed") or changed
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
    def _config(self):
        try:return self._config_provider() if callable(self._config_provider) else None
        except Exception:return None

    def _enabled(self):
        return bool(config_value(self._config(),'enabled',True))

    def _limits(self):
        getter=getattr(self._quota,'limits',None)
        return getter() if callable(getter) else quota_limits(self._config(),legacy=self._config_provider is None)

    def _watch_limit(self,kind):
        return 1 if kind=='casual' else max(1,min(100,_as_int(config_value(self._config(),'schedule_max_videos',WATCH_CONTINUE_MAX_SUBMITS))))

    def _prepare_intent_budget(self,episode_id,entry,links=None):
        active=self._get_active_episode() or {}
        episode=active if active.get('episode_id')==episode_id else self._get_episode(episode_id)
        episode=episode if isinstance(episode,dict) else {'episode_id':episode_id}
        identity=watch_budget_identity(episode,self._get_episode)
        if identity is None:raise ValueError('watch_budget_identity_unavailable')
        links=links if links is not None else self._load_links().get('links',{})
        key=entry.get('budget_key') or identity['key']
        # A saved parent association retains the original date even after old episode pruning.
        if not entry.get('budget_key') and identity.get('intent_item_id'):
            for alias in identity['aliases']:
                old=(links.get(alias) or {}).get('budget_key')
                if old:
                    try:parts=json.loads(old)
                    except (ValueError,TypeError):continue
                    if len(parts)==3 and parts[0]=='intent' and parts[2]==identity['intent_item_id']:key=old
        entry['budget_key']=key
        if key in self._budget_seeded:return key
        try:
            with self._opportunities.path.open(encoding='utf-8') as stream:old_rolls=json.load(stream)
            roll_unknown=not isinstance(old_rolls,dict)
            if roll_unknown:old_rolls={}
        except FileNotFoundError:old_rolls={};roll_unknown=False
        except (OSError,ValueError):old_rolls={};roll_unknown=True
        aliases=set(identity['aliases'])
        for eid,old in links.items():
            if not isinstance(old,dict) or old.get('budget_key'):continue
            row=self._get_episode(eid)
            other=watch_budget_identity(row,self._get_episode) if isinstance(row,dict) and row.get('episode_id')==eid else None
            if eid not in aliases and (other is None or other['key']!=key):continue
            ids={str(item.get('task_id')) for item in old.get('task_history',[]) if isinstance(item,dict) and item.get('task_id')}
            if old.get('task_id'):ids.add(str(old['task_id']))
            chance=old_rolls.get(eid)
            if chance is None and old.get('scenario')=='casual':
                chance={'at':old.get('started_at'),'hit':old.get('stage')!='opportunity_missed'}
            self._intent_usage.seed_legacy(key,eid,task_ids=ids,reported_count=_as_int(old.get('submitted_count')),
                chance=chance,now=self._now())
            aliases.add(eid)
        for eid in aliases:
            if isinstance(old_rolls.get(eid),dict):
                self._intent_usage.seed_legacy(key,eid,reported_count=0,chance=old_rolls[eid],now=self._now())
        if roll_unknown:self._intent_usage.seed_legacy(key,'legacy_chance_unknown',chance={'hit':None},now=self._now())
        self._budget_seeded.add(key)
        return key

    def _roll_intent_once(self,episode_id,active,links,probability):
        try:
            entry={};key=self._prepare_intent_budget(episode_id,entry,links)
            return self._intent_usage.roll_once(key,probability,rng=self._rng,now=self._now()),key
        except Exception as exc:
            if self._log:self._log.warning('日程观看机会账不可核验，本次不掷点：%s',type(exc).__name__)
            return None,''

    def _reconcile_budget_receipts(self,links):
        changed=False
        for entry in links.values():
            if not isinstance(entry,dict) or not entry.get('budget_key') or not entry.get('budget_reservation'):continue
            try:
                if entry.get('task_id'):
                    facts=self._intent_usage.confirm(entry['budget_key'],entry['budget_reservation'],entry['task_id'],now=self._now())
                    entry['intent_submitted_count']=facts['accepted_count']
                elif entry.get('budget_release_pending'):
                    self._intent_usage.reject(entry['budget_key'],entry['budget_reservation'])
                else:continue
                entry.pop('budget_reservation',None);entry.pop('budget_release_pending',None);entry.pop('counter_unconfirmed',None);changed=True
            except Exception:continue  # The durable pending slot keeps this intent frozen.
        return changed

    def _casual_context_ready(self,episode_id):
        if self._attention is not None:
            lease=self._attention.try_acquire_surf(f'schedule:{episode_id}')
            if not lease:return False
        if callable(self._automatic_guard) and not self._automatic_guard(episode_id):
            if self._attention is not None:self._attention.release(f'schedule:{episode_id}')
            return False
        return True

    def _remaining_episode(self,episode):
        if callable(self._episode_remaining):
            try:return self._episode_remaining(episode)
            except Exception:return 0.0
        return None

    def needs_poll(self):
        return any(isinstance(row,dict) and row.get('task_id') and (not row.get('closed') or row.get('detached'))
                   for row in self._load_links().get('links',{}).values())

    def _release_surf(self, episode_id: str, entry: Dict[str, Any]) -> None:
        if self._attention is not None and entry.get("lease"):
            self._attention.release(f"schedule:{episode_id}")
        entry.pop("lease", None)

    def _now(self, value: Any = None) -> float:
        if value is not None:
            return float(value)
        return float(self._clock() if callable(self._clock) else time.time())

    @staticmethod
    def _episode_watch_minutes(episode: Dict[str, Any]) -> Optional[float]:
        """活动 episode 的计划时长（25–55 分钟口径）；缺失/非法 → None（沿用旧默认）。"""
        start = _as_float((episode or {}).get("planned_start_minute"))
        end = _as_float((episode or {}).get("planned_end_minute"))
        if end <= start:
            return None
        return max(float(WATCH_SESSION_MIN_MINUTES),
                   min(float(WATCH_SESSION_MAX_MINUTES), float(end) - float(start)))

    @staticmethod
    def _session_budget_minutes(entry: Dict[str, Any]) -> Optional[float]:
        """episode 内实际观看预算（分钟）：优先 episode 计划时长，回退已入账时长。

        dedicated 使用 25–55 分钟预算口径；gap/casual 沿用各自预留时长。
        选片另核对日程剩余时间；只有 dedicated 可在支数与时长限制内续投。
        """
        target = _as_float(entry.get("session_target_minutes"))
        if target <= 0:
            target = _as_float(entry.get("reserved_minutes"))
        if target <= 0:
            return None
        return (max(0.0,float(target)) if entry.get("scenario") in ("gap","casual") else
                max(float(WATCH_SESSION_MIN_MINUTES),min(float(WATCH_SESSION_MAX_MINUTES),float(target))))

    def _remaining_watch_minutes(self, entry: Dict[str, Any]) -> Optional[float]:
        """本 episode 剩余观看预算（分钟）；预算未知 → None（不做候选时长过滤）。"""
        budget = self._session_budget_minutes(entry)
        if budget is None:
            return None
        watched = _as_float(entry.get("watched_minutes_accumulated"))
        return max(0.0, budget - watched)

    async def _pick_candidate_within_budget(
            self, entry: Dict[str, Any], first_pick: Any) -> Tuple[Optional[Dict[str, Any]], str]:
        """取下一支候选；跳过单支时长超过剩余预算的候选（防实际观看超 55 分钟）。

        返回 ``(candidate, reason)``：无候选时 reason 来自端口或 ``candidate_too_long``。
        候选时长缺失/非正（未知）不拦截；每次最多再取 ``WATCH_CANDIDATE_FIT_ATTEMPTS``
        支，避免一次续投把候选池无界抽干。
        """
        pick = first_pick
        remaining = self._remaining_watch_minutes(entry)
        active=self._get_active_episode() or {}
        actual_remaining=self._remaining_episode(active) if active.get("episode_id")==entry.get("episode_id") else None
        if actual_remaining is not None:remaining=min(remaining,actual_remaining) if remaining is not None else actual_remaining
        for attempt in range(WATCH_CANDIDATE_FIT_ATTEMPTS):
            candidate = pick.get("candidate") if isinstance(pick, dict) else None
            if not isinstance(candidate, dict):
                return None, (str(pick.get("reason") or "") if isinstance(pick, dict) else "")
            duration = _as_float(candidate.get("duration_seconds"))
            if remaining is None or duration <= 0 or duration <= remaining * 60.0:
                return candidate, ""
            if attempt + 1 >= WATCH_CANDIDATE_FIT_ATTEMPTS:
                break
            pick = await self._api(action="next_candidate")
        return None, "candidate_too_long"

    def _can_continue_watching(self, entry: Dict[str, Any],
                               now: Optional[float] = None) -> bool:
        """上一个视频自然看完且 episode 仍活跃、仍在时长预算内 → 续投下一支。"""
        if entry.get("scenario") in ("gap","casual") or not self._enabled():return False
        if str(entry.get("closed_reason") or "") != "task_completed":
            return False
        if entry.get("detached") or entry.get("detached_finished") or entry.get("cached"):
            return False
        limit=max(1,min(100,_as_int(config_value(self._config(),"schedule_max_videos",WATCH_CONTINUE_MAX_SUBMITS))))
        if entry.get('budget_key'):
            try:usage=self._intent_usage.snapshot(entry['budget_key'])
            except Exception:return False
            if usage['pending_count'] or usage['accepted_count']>=limit:return False
        elif int(entry.get("submitted_count") or 0) >= limit:
            return False
        budget = self._session_budget_minutes(entry)
        if budget is not None:
            watched = _as_float(entry.get("watched_minutes_accumulated"))
            if watched <= 0:
                started = _as_float(entry.get("watch_started_at")
                                    or entry.get("started_at"))
                if started > 0:
                    watched = (self._now(now) - started) / 60.0
            if watched >= budget:
                return False
        return True

    def _accumulate_watch(self, entry: Dict[str, Any],
                          result: Optional[Dict[str, Any]] = None,
                          *, assume_completed: bool = True) -> None:
        """累计本 episode 已真实看完的视频分钟数（续投后的实际观看时长口径）。"""
        started = _as_float(entry.get("task_started_at"))
        if started <= 0:
            return
        if isinstance(result, dict):
            if str(result.get("status") or "") not in ("", "completed"):
                return  # 非完成结果不冒充已观看时长
        elif not assume_completed:
            return  # 拿不到完成结果且未确认完成 → 不计入
        finished = _as_float((result or {}).get("finished_at")) if isinstance(result, dict) else 0.0
        if finished <= 0:
            finished = self._now()
        minutes = max(0.0, (finished - started) / 60.0)
        # 已计入累计后清掉本任务的起点：同拍后续按实际观看结算时不再重复加一次。
        entry["task_started_at"] = None
        if minutes <= 0:
            return
        total = _as_float(entry.get("watched_minutes_accumulated")) + minutes
        budget = self._session_budget_minutes(entry)
        if budget is not None:
            total = min(total, budget)
        entry["watched_minutes_accumulated"] = round(total, 2)

    def _settle_quota(self, episode_id: str, entry: Dict[str, Any],
                      *, reason: str = "", finished_at: Any = None) -> bool:
        """按任务的实际处理时间结算关联场次。

        实际观看起点取任务真正开始处理的时刻（manager status 的 started_at）；
        缺失（仍在排队/任务未启动）→ 按 0 分钟结算 = 全额退款，绝不把排队时间
        算成观看。储存在 claim 里的入账时长是上限，结算不会超过它。
        幂等：claim 已结算过则账本不变。
        """
        if self._quota is None or not entry.get("reserved_minutes"):
            return False
        settle = getattr(self._quota, "settle", None)
        if not callable(settle):
            return False
        started = _as_float(entry.get("task_started_at"))
        watched = _as_float(entry.get("watched_minutes_accumulated"))
        if started > 0:
            stopped = _as_float(finished_at) or self._now()
            watched += max(0.0, (stopped - started) / 60.0)
        try:
            if isinstance(self._quota, SurfQuotaLedger):
                done = bool(settle(episode_id, watched, expected_day=entry.get("quota_day") or ""))
            else:
                done = bool(settle(episode_id, watched))
        except Exception as exc:
            if self._log:
                self._log.warning("观看额度结算失败（忽略，不二次扣账）：%s", exc)
            return False
        reserved = _as_float(entry.get("reserved_minutes"))
        if not done:
            # Crash after quota commit but before link-file replace: adopt the existing receipt.
            if not isinstance(self._quota, SurfQuotaLedger):
                return False
            snapshot = self._quota.snapshot()
            if entry.get("quota_day") and entry["quota_day"] != snapshot.get("date"):
                return False
            claim = (snapshot.get("claims") or {}).get(episode_id) or {}
            if claim.get("state") != "settled":
                return False
            watched = _as_float(claim.get("settled_minutes"))
        entry["settled_minutes"] = round(max(0.0, min(reserved, watched)), 2)
        entry["settled_reason"] = reason or "task_settled"
        entry.pop("reserved_minutes", None)
        return True

    @staticmethod
    def _should_detach_poll(entry: Dict[str, Any], task_id: str) -> bool:
        """任务已脱离日程但仍可能跑完 → 保留轻量轮询补归档（不重开/不回填）。"""
        if not task_id or entry.get("detached") or entry.get("result"):
            return False
        return str(entry.get("stage") or "") not in (
            "skipped", "failed_submit", "unavailable", "cancelled", "cached_completed")

    async def _continue_watching(self, episode_id: str, entry: Dict[str, Any],
                                 active: Dict[str, Any]) -> None:
        """episode 内续投：归档上一支任务摘要，重置关联后选下一支候选。

        不重新申请额度（同一 episode 已在首次提交时入账）；保留 ``reserved_minutes``、
        ``session_target_minutes``、``watch_started_at``（实际观看预算的起点）与
        ``started_at``，仅替换 task/bvid/stage；task_history 上限
        ``WATCH_CONTINUE_MAX_TASKS``（task_history 有界）；生产提交上限读取
        ``schedule_max_videos``，旧独立调用才回退 ``WATCH_CONTINUE_MAX_SUBMITS``。
        """
        history = entry.get("task_history")
        if not isinstance(history, list):
            history = []
        snapshot = {key: entry.get(key) for key in
                    ("task_id", "bvid", "stage", "result", "cached", "reason",
                     "error_class", "closed_reason", "task_started_at",
                     "settled_minutes", "settled_reason")
                    if entry.get(key) is not None}
        if snapshot:
            history.append(snapshot)
        entry["task_history"] = history[-WATCH_CONTINUE_MAX_TASKS:]
        for key in ("task_id", "bvid", "stage", "result", "cached", "reason",
                    "error_class", "closed_reason", "detached", "task_started_at"):
            entry.pop(key, None)
        entry["closed"] = False
        entry["continued_count"] = int(entry.get("continued_count") or 0) + 1
        await self._start_watching(episode_id, entry,
                                   session_minutes=self._episode_watch_minutes(active))

    async def _poll_detached(self, episode_id: str, entry: Dict[str, Any]) -> bool:
        """脱离 owner 后的轻量状态轮询：终态补归档；有界次数后停止。"""
        if entry.pop("detached_skip_once", False):
            return False  # 收尾当拍不额外轮询（与旧行为一致），下一拍再补
        task_id = str(entry.get("task_id") or "")
        if self._now()-_as_float(entry.get("detached_last_poll"))<300:return False
        entry["detached_last_poll"]=self._now()
        polls = int(entry.get("detached_polls") or 0) + 1
        entry["detached_polls"] = polls
        try:
            receipt = await self._api(action="status", task_id=task_id)
        except Exception:
            receipt = {"success": False}
        if not isinstance(receipt, dict) or not receipt.get("success"):
            if polls >= _DETACHED_MAX_POLLS:
                entry["detached"] = False
                entry["closed_reason"] = entry.get("closed_reason") or "detached_poll_exhausted"
                self._settle_quota(episode_id, entry,
                                   reason="detached_poll_exhausted")
                return True
            return True
        stage = str(receipt.get("status") or "")
        if entry.get("task_started_at") is None:
            started = receipt.get("started_at")
            if started is not None:
                entry["task_started_at"] = _as_float(started)
        changed = bool(stage and entry.get("detached_stage") != stage)
        if stage:
            # 运行中用 detached_stage 记账，不覆盖「为何停止观看」的 stage
            # （revoked_by_chat / episode_* 需要保留给面板与测试判定）。
            entry["detached_stage"] = stage
        if stage in ("completed", "failed", "cancelled"):
            await self._finish_watch_task(episode_id, entry, receipt, detached=True)
            if self._log:
                self._log.info("刷视频事件 %s：脱离 owner 后任务终态 %s，已补归档", episode_id, stage)
            return True
        if polls >= _DETACHED_MAX_POLLS:
            entry["detached"] = False
            entry["closed_reason"] = entry.get("closed_reason") or "detached_poll_exhausted"
            self._settle_quota(episode_id, entry, reason="detached_poll_exhausted")
            return True
        return True

    async def _start_watching(self, episode_id: str, entry: Dict[str, Any],
                              kind: str = "dedicated", *, force_debug: bool = False,
                              session_minutes: Optional[float] = None) -> None:
        entry.setdefault("started_at", self._now())
        entry["episode_id"]=episode_id
        # 场景标记（S2 频率倍率读 entry["scenario"]：dedicated=0.45 / gap=0.70；
        # 2026-11-04 前此处未写标记，dedicated 曾被误读成 gap 倍率）
        entry["scenario"] = kind if kind in ("gap","casual") else "dedicated"
        # 本 episode 的实际观看预算（分钟）：episode 计划 25–55 优先；gap 按空隙口径；
        # dedicated 无计划信息时取 55 上限（不再固定 20 分钟）。续投沿用同一预算起点。
        planned_minutes = (float(session_minutes) if session_minutes is not None
                           else GAP_SESSION_MINUTES if kind == "gap"
                           else float(SURF_MAX_DEDICATED_MINUTES))
        if planned_minutes <= 0:
            planned_minutes = (GAP_SESSION_MINUTES if kind == "gap"
                               else float(SURF_MAX_DEDICATED_MINUTES))
        entry["session_target_minutes"] = planned_minutes
        reserved = False
        accepted = False
        budget_token=None
        phase = "candidate"
        retryable = False
        try:
            # PR-WATCH：force_debug（/lbm plan watch 调试命令）不申请 SURF 租约——
            # 与当前日程/聊天并行，不占注意力；无租约则收尾 _release_surf 空操作。
            # 随机 gap/日程 dedicated 路径 force_debug 恒 False，注意力语义不变。
            if self._attention is not None and not force_debug:
                lease = self._attention.try_acquire_surf(f"schedule:{episode_id}")
                if not lease:
                    retryable = True  # No side effect yet; a later quiet tick may try again.
                    entry.update(stage="skipped", reason="聊天占用或覆盖面未确立，不开始自动观看")
                    return
                entry["lease"] = lease
            if not self._enabled() and not force_debug:
                entry.update(stage='skipped',reason='自主观看已关闭');return
            if callable(self._automatic_guard) and not force_debug and not self._automatic_guard(episode_id):
                entry.update(stage='skipped',reason='当前活动、睡眠或真实聊天不允许自主观看');return
            if kind in ('dedicated','casual'):
                budget_key=self._prepare_intent_budget(episode_id,entry)
                usage=self._intent_usage.snapshot(budget_key)
                entry['intent_submitted_count']=usage['accepted_count']
                if usage['pending_count'] or usage['accepted_count']>=self._watch_limit(kind):
                    entry.update(stage='skipped',reason='日程观看上限已到或已有受理结果待确认');return
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
            candidate, pick_reason = await self._pick_candidate_within_budget(entry, pick)
            if not candidate:
                if pick_reason == "candidate_too_long":
                    entry.update(stage="skipped",
                                 reason="候选视频时长超出本 episode 剩余观看预算，本拍不提交")
                else:
                    entry.update(stage="skipped",
                                 reason=("无真实候选，不编造 BV 号"
                                         + (f"（{pick_reason}）" if pick_reason else "")))
                return
            if (not force_debug and self._attention is not None
                    and not self._attention.lease_valid(entry.get("lease"))):
                entry.update(stage="skipped", reason="选片期间聊天开始")
                return
            # 同一 episode 首次提交才入账；episode 内续投/忙后重试沿用原 claim。
            # dedicated 按 25–55 分钟口径预留；casual 随后取候选与日程剩余时长。
            # 同时检查配置的日总额度，放不下就如实跳过，不虚记或重复申请。
            if self._quota is not None and not entry.get("reserved_minutes"):
                snap = self._quota.snapshot()
                gate = surf_gate_check(kind, snap, self._now(),limits=self._limits())
                used_m = _as_float(snap.get("minutes_used"))
                if kind=="casual":
                    planned_minutes=max(.01,min(planned_minutes,_as_float(candidate.get("duration_seconds"))/60 or planned_minutes))
                    entry["session_target_minutes"]=planned_minutes
                if gate is None and used_m + planned_minutes > self._limits()["daily_minutes_limit"]:
                    gate = (f"今日自动观看时长不足（本场需 {planned_minutes:g} 分钟，"
                            f"今日仅剩 {max(0.0, self._limits()['daily_minutes_limit'] - used_m):g} 分钟）")
                if gate:
                    entry.update(stage="skipped", reason=gate)
                    return
                reserve = getattr(self._quota, "reserve_once", None)
                reserved = (reserve(episode_id, kind, planned_minutes) if callable(reserve)
                            else self._quota.try_consume(kind, planned_minutes))
                if not reserved:
                    entry.update(stage="skipped", reason="自动观看额度不足或本事件已申请")
                    return
                entry["reserved_minutes"] = planned_minutes
                entry["quota_day"] = snap.get("date")
            if callable(self._automatic_guard) and not force_debug and not self._automatic_guard(episode_id):
                entry.update(stage="skipped",reason="候选准备后活动或聊天状态已变化");return
            if kind in ('dedicated','casual'):
                claim=self._intent_usage.reserve(entry['budget_key'],episode_id,self._watch_limit(kind),now=self._now())
                budget_token=claim['token']
                if not budget_token:
                    entry.update(stage='skipped',reason=claim['reason']);return
                entry['budget_reservation']=budget_token
            phase = "submit"
            # PR-WATCH：debug 强制路径以 source="debug" 送达观看服务——submit 注意力闸
            # 与运行期守卫因 source!="schedule" 自然放行；端口缺省仍 schedule（其余路径不变）。
            receipt = await self._api(action="submit", video=str(candidate["bvid"]),
                                      purpose=("空隙刷视频" if kind == "gap" else "日程顺便看视频" if kind=="casual" else "日程刷视频"),
                                      episode_id=episode_id,
                                      **({"source": "debug"} if force_debug else {}))
            if receipt.get("success") and receipt.get("cached"):
                # 旧观看记忆不是新任务，不登记分享、不伪造task_id、不占观看额度。
                # 续投场景的 claim 属于整个 episode（前几支已真实看过），只收尾不退。
                if reserved:
                    self._refund_reservation(episode_id, entry)
                entry.update(stage="cached_completed", cached=True, bvid=str(candidate["bvid"]),
                             result=receipt.get("result"), closed=True)
                return
            if not receipt.get("success") or not receipt.get("task_id"):
                entry.update(stage="failed_submit", reason=str(receipt.get("error") or "提交被拒绝")[:200],
                             error_class=receipt.get("error_class"))
                if reserved:
                    self._refund_reservation(episode_id, entry)
                return
            entry.update(task_id=str(receipt["task_id"]), bvid=str(candidate["bvid"]),
                         stage=str(receipt.get("status") or "queued"),
                         cached=bool(receipt.get("cached")))
            receipt_ids=set(entry.get('receipt_task_ids') or [])
            if str(receipt['task_id']) not in receipt_ids:
                receipt_ids.add(str(receipt['task_id']))
                entry["submitted_count"] = int(entry.get("submitted_count") or 0) + 1
            entry['receipt_task_ids']=sorted(receipt_ids)
            accepted=True
            if budget_token:
                try:
                    facts=self._intent_usage.confirm(entry['budget_key'],budget_token,str(receipt['task_id']),now=self._now())
                    entry['intent_submitted_count']=facts['accepted_count']
                    entry.pop('budget_reservation',None)
                except Exception:entry['counter_unconfirmed']=True
            if not entry.get("watch_started_at"):
                # 实际观看预算起点：任务真正受理时（而非早前被忙闲跳过的拍）。
                entry["watch_started_at"] = self._now()
            accepted = True
        except asyncio.CancelledError:
            entry.update(stage='submit_uncertain' if phase=='submit' else 'cancelled',closed=True)
            if phase=='submit':
                entry['submit_uncertain']=True
                document=self._load_links();document.setdefault('links',{})[episode_id]=entry;self._save_links(document)
            elif reserved:self._refund_reservation(episode_id,entry)
            raise
        except Exception as exc:
            entry.update(stage="failed_submit" if phase == "submit" else "unavailable",
                         reason=("提交失败：" if phase == "submit" else "观看能力不可用：") + str(exc)[:160])
            # An uncertain submission is not refunded or retried.
            if phase == "submit":
                entry["submit_uncertain"] = True
            if reserved and phase != "submit":
                self._refund_reservation(episode_id, entry)
        finally:
            if not accepted and budget_token:
                if entry.get('submit_uncertain'):
                    try:self._intent_usage.mark_uncertain(entry['budget_key'],budget_token)
                    except Exception:pass  # An unresolved reservation already blocks another submission.
                else:
                    try:
                        self._intent_usage.reject(entry['budget_key'],budget_token)
                        entry.pop('budget_reservation',None)
                    except Exception:entry['budget_release_pending']=True
            if not accepted:
                entry["closed"] = not retryable
                self._release_surf(episode_id, entry)
                # 续投阶段停下（无候选/缓存/提交失败）：本 episode 已看过的部分按
                # 实际观看结算，剩余额度退回；首次提交的提交前失败已在上面退款。
                # 提交结果不确定（phase=submit 异常）不退款、不结算。
                if (not retryable and entry.get("reserved_minutes")
                        and not entry.get("submit_uncertain")):
                    self._settle_quota(episode_id, entry, reason="watch_stopped")

    def _refund_reservation(self, episode_id: str, entry: Dict[str, Any]) -> None:
        """首次提交未成功（缓存/拒绝/提交前异常）→ 退还本场额度。"""
        if self._quota is not None:
            refund = getattr(self._quota, "refund_unstarted", None)
            if callable(refund):
                try:
                    refund(episode_id)
                except Exception as exc:
                    if self._log:
                        self._log.warning("观看额度退款失败（忽略，不重复入账）：%s", exc)
        entry.pop("reserved_minutes", None)

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
        """单次有界状态轮询；核对上下文与 SURF 租约，失效时释放本方 owner 并收尾。"""
        task_id = str(entry.get("task_id") or "")
        if callable(self._automatic_guard) and not entry.get('forced_debug'):
            if not self._automatic_guard(episode_id):
                try:await self._api(action='cancel',task_id=task_id,owner=f'schedule:{episode_id}')
                except Exception:pass
                entry.update(stage='revoked_by_context',closed=True,closed_reason='revoked_by_context',detached=True)
                self._release_surf(episode_id,entry)
                return True
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
                # 只释放 owner，不取消任务：保留轻量轮询补归档晚到完成结果。
                if self._should_detach_poll(entry, task_id):
                    entry["detached"] = True
                    entry["detached_skip_once"] = True
                if self._log:
                    self._log.info("刷视频事件 %s：SURF 租约被撤销，停止自动观看", episode_id)
                return False
        try:
            receipt = await self._api(action="status", task_id=task_id)
        except Exception as exc:
            entry['poll_error']='观看状态暂不可核验：'+type(exc).__name__
            return True
        if not receipt.get("success"):
            return False
        stage = str(receipt.get("status") or "")
        if entry.get("task_started_at") is None:
            started = receipt.get("started_at")
            if started is not None:
                entry["task_started_at"] = _as_float(started)
        if entry.get("stage") != stage:
            entry["stage"] = stage
            if self._log and stage in ("running", "completed", "failed", "cancelled"):
                self._log.info("观看任务 %s 阶段：%s", task_id, stage)
            if stage in ("completed", "failed", "cancelled"):
                await self._finish_watch_task(episode_id, entry, receipt)
                return True
            return True
        return False

    async def _finish_watch_task(self, episode_id: str, entry: Dict[str, Any],
                                 receipt: Dict[str, Any], *, detached: bool = False) -> None:
        """Close a confirmed worker terminal. Dedicated live sessions decide continuation upstream."""
        stage = str(receipt["status"])
        task_id = str(entry.get("task_id") or "")
        if stage == "completed":
            result = await self._archive_if_completed(entry, task_id, episode_id=episode_id)
            self._accumulate_watch(entry, result or dict(receipt, status="completed"))
        if stage != "completed" or detached:
            self._settle_quota(episode_id, entry, reason=f"task_{stage}",
                               finished_at=receipt.get("finished_at"))
        elif entry.get("scenario") in ("gap", "casual"):
            self._settle_quota(episode_id, entry, reason="session_completed")
        self._release_surf(episode_id, entry)
        if detached:
            entry["detached"] = False
            entry["detached_finished"] = True
        entry.update(stage=stage, closed=True, closed_reason=f"task_{stage}")

    async def _archive_if_completed(self, entry: Dict[str, Any], task_id: str,
                                    episode_id: str = "") -> Optional[Dict[str, Any]]:
        """晚到/收尾时的真实完成结果存档（不重开 episode、不回填时间）。

        返回真实结果 dict（供 episode 内实际观看时长累计）；不可得 → None。
        """
        try:
            receipt = await self._api(action="result", task_id=task_id)
        except Exception:
            return None
        if isinstance(receipt, dict):
            result_started = None
            if isinstance(receipt.get("result"), dict):
                result_started = receipt["result"].get("started_at")
            started = receipt.get("started_at") or result_started
            if entry.get("task_started_at") is None and started is not None:
                # 未轮询到 running 就中断时，结果回执的 started_at 仍可用于
                # 「按实际观看结算」，不把排队时间算成观看。
                entry["task_started_at"] = _as_float(started)
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
                return result
        return None

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
