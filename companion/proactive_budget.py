"""进程内事件总线 + 主动打扰预算（仅目标陪伴私聊；MAINTENANCE_PLAN §三 / §六.2 H，里程碑 M0）。

设计约定（逐条对应计划书 §3.1）：
- 事件名 ``模块.事件``（snake_case）；payload 自动补 ``ts``（ISO 时间）与 ``source``；
- 发布方只放 ID 与摘要（qq/stream_id/delta/事件名），订阅方需要更多数据时自己拉；
- 每次 publish 自动 ``DecisionLogger.record(action="bus:<event>", **payload 摘要)``
  （决策日志开关关闭时由 logger 自身零开销短路，见 core/diag.py:143）；
- 同一事件递归 publish 深度 >5 时丢弃该次发布并告警（§3.1 深度保护）；
- 订阅注册/注销挂在各服务束 start()/stop()（M0 实际订阅者仅本文件的预算计数器）。

主动打扰预算（目标陪伴私聊导向；§六.2 H，B11）：主动行为触发前发 ``proactive.used``，
按日计数；各发布方触发前查询 :func:`ProactiveBudget.allow_proactive`；
**上限 = ``patrol.daily_max_speak``（每日预算即上限；2026-10-10 配置整理轮移除独立的
``proactive_target_daily_cap`` / 旧键 ``global_proactive_cap``，不再叠加 +2 余量）**。
3.4（2026-11-11）：``patrol.budget_elasticity_enabled`` 出厂默认 **开**（配置默认值轮；
2026-11-12 R4 用户授权保留并修订红线，见 DEV_GUIDE §5/§8 E2）；命中高好感/高 Valence/
节日时允许 +1~2 次，硬上限 base+2（且不越过 50），显式关闭时旧口径逐字不变；
``daily_max_speak=0`` 不复活；弹性只放大额度，夜间/冷却/授权闸仍由各发布方先执行。

**已登记例外（2026-11-12 R4，DEV_GUIDE §8 E3）——阶段 4「视频卡片 + 说话轮同一槽位」**：
卡片经本类 ``reserve`` → accepted → ``commit_reservation`` **恰好一次**；随后同一场景的
阶段 4 说话轮（``integrations/video_share_stage4_turn.py``）属于同一次主动接触，以
``budget_reserved=True`` 跳过 ``remaining()`` 且**不 reserve/commit/release**，不占第二个
槽位。产品口径 = 一槽一次主动消息（卡片 1 条 + 同一场景说话轮 1 条）；测试
``scratch/test_stage4_budget_slot_r4.py`` 锁定；若改为「一槽一句」，须把说话轮计入
``reserve/commit`` 并删除该例外。
计数口径 = **按发送流身份计数**：发往目标陪伴私聊的主动打扰都计入（发布方不限——
陪伴侧巡检/想念、日程侧非问候主动行为、emotion 赖床提醒都发 ``proactive.used``；
2026-11-04 F1 起告状工具回退到陪伴目标私聊的发送同样经 ``allow_proactive`` 闸门
并以渠道 ``harass_report`` 发 ``proactive.used`` 入账）；
早/晚安免预算（planning 侧闸门与计数一起跳），活动切换开口在目标流已解析时不占。

计数范围（M11，附录 II §3.2/§3.3；2026-10-10 口径复核）：**只统计「发往目标陪伴私聊」的主动打扰**——各扣费点
经 ``stream_id`` 维度传参，非目标流（群聊/其他私聊）直接放行且不查计数、不入账（收窄前为
「所有主动行为合计」）。目标私聊 stream_id 的权威来源是 ``plugin._stream_id``（G11；由
``target.target_qq`` 解析、随热重载更新），装配期以 ``target_stream_provider`` 回调注入，
**不新增任何状态**；取不到目标流时（未注入回调 / 尚未解析）沿用旧全量语义，不静默停账。

计数持久化（A2，2026-10-05）：传入 ``data_dir`` 时 ``_used/_date`` 落盘
``proactive_budget.json``（复用 core/storage.py 的 load_json/save_json_atomic）——
启动装载、计数变化即原子写、日切重置写回，使计数跨 file_watcher 热重载连续
（§八.8：热重载会清零一切内存态计数）；落盘异常降级内存态，不影响事件分发。
落盘状态带 ``scope``（§八.6）：旧文件（无 scope = 收窄前旧语义的计数）装载时按新语义
重置当日计数并记一条决策日志，避免把旧语义的计数带进新口径。
"""

from __future__ import annotations
import logging
from datetime import datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4
from typing import Any, Dict, Optional
from core.storage import load_json, save_json_atomic

logger = logging.getLogger(__name__)


def _clamp01(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if numeric != numeric:  # NaN
        return 0.0
    return max(0.0, min(1.0, numeric))


def _as_float(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return numeric if numeric == numeric else float(default)


class ProactiveBudget:
    """主动打扰预算（仅目标陪伴私聊）计数器（§六.2 H）：按日计数，跨天自动清零。

    M11（附录 II §3.2）：计数身份 = **目标陪伴私聊**——只有发往该流的主动打扰才查计数/入账；
    非目标流直接放行、不入账。目标流由 ``target_stream_provider``（装配期注入
    ``plugin._stream_id`` 回调，G11）动态取，热重载改 ``target_qq`` 即时生效，无新状态。

    A2 持久化：``data_dir`` 非空时计数落盘 ``proactive_budget.json``（启动装载 /
    计数变化即原子写 / 日切重置写回），使 file_watcher 热重载后计数不回落；
    落盘异常降级为内存态并告警，不影响事件分发。
    """

    #: 状态文件名（data_dir 下）
    STATE_FILENAME = "proactive_budget.json"

    #: 落盘状态的语义标记（§八.6）：旧文件缺该字段 = 收窄前旧语义计数 → 装载即重置
    STATE_SCOPE = "target_private"

    #: 3.4（2026-11-11 Gemini 批）：预算弹性默认开（配置默认值轮）；开启后最多在每日预算上追加 2 次。
    #: 「硬上限」= daily_max_speak + ELASTIC_MAX_BONUS（再叠加任何信号都不会更高）；
    #: 每日预算为 0（用户明确停止可选主动回合）时弹性不复活预算，恒 0。
    ELASTIC_MAX_BONUS = 2
    #: 绝对硬上限（与 [patrol].daily_max_speak 的 le=50 同口径）：base+2 也不得超过 50。
    ELASTIC_ABSOLUTE_MAX = 50
    #: 高好感判定：provider 给的归一化比例（0~1）达到该值即算一条强信号。
    ELASTIC_AFFECTION_MIN_RATIO = 0.7
    #: 高 Valence 判定（报告 §3.4：Valence > 0.6）。
    ELASTIC_VALENCE_MIN = 0.6
    #: 节日/纪念日字符串里的否定值：这些不算节日信号。
    ELASTIC_HOLIDAY_FALSE = ("", "0", "false", "none", "null", "无", "工作日", "普通日")

    def __init__(self, config_provider: Any = None, decision_logger: Any = None,
                 data_dir: Any = None, target_stream_provider: Any = None,
                 elasticity_provider: Any = None) -> None:
        self._config_provider = config_provider
        self._decision_logger = decision_logger
        #: 目标陪伴私聊 stream 的取值来源（可调用 → 动态取值；None = 不可得）
        self._target_stream_provider = target_stream_provider
        #: 3.4 弹性信号来源（可调用 → dict；见 :meth:`elasticity_breakdown`）。
        #: 缺省 None = 弹性开启但没有高好感/高 Valence/节日信号 → 不追加（保持基础预算）。
        self._elasticity_provider = elasticity_provider
        self._date = ""
        self._used = 0
        self._channels: Dict[str, int] = {}
        self._lock = RLock()
        # Sending slots survive reload; unknown delivery is held until the day rolls.
        self._reservations: Dict[str, Dict[str, str]] = {}
        #: 持久化落点（None = 纯内存态；既有装配不传 data_dir 时行为不变）
        self._state_path: Optional[Path] = (
            Path(data_dir) / self.STATE_FILENAME if data_dir else None
        )
        self._persist_warned = False
        self._scope_reset_logged = False
        self._load()

    # ---- 持久化（A2） ----
    def _load(self) -> None:
        """装载落盘计数：缺失/损坏（含 used 字段类型异常）/非当日/scope 不符 → 重建并回写。

        ``scope`` 兼容（§八.6）：当日文件缺 ``scope``（或值不符）＝收窄前旧语义的计数，
        按新语义重置当日计数并记一条决策日志（``budget.scope_reset``；同签名每日一次——
        重置后立即回写 scope，后续装载不再触发）。
        """
        path = self._state_path
        if path is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        self._date = today
        raw = load_json(path, None)
        if raw is None and not path.exists():
            logger.warning("打扰预算状态文件不存在，按全新计数起步：%s", path)
            self._save()
            return
        if not isinstance(raw, dict):
            logger.warning("打扰预算状态文件损坏，按全新计数起步并重建：%s", path)
            self._save()
            return
        date = raw.get("date")
        if not isinstance(date, str) or not date:
            logger.warning("打扰预算状态文件缺 date 字段，按全新计数起步并重建：%s", path)
            self._save()
            return
        if date != today:
            # 跨日（含停机跨天）：装载即清零并写回
            self._save()
            return
        used = raw.get("used")
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            # used 是整份状态的计数基准：类型异常即判整份损坏，清空重建并回写，
            # 不得保留同一文件里已不可信的 channels 明细
            logger.warning("打扰预算状态文件 used 字段异常，整份状态按损坏处理并重建：%s", path)
            self._save()
            return
        if raw.get("scope") != self.STATE_SCOPE:
            # M11（附录 II §八.6）：收窄前旧语义的当日计数不得带入新语义
            logger.warning(
                "打扰预算状态文件 scope=%r 非 %s（收窄前旧语义计数），按新语义重置当日计数：%s",
                raw.get("scope"), self.STATE_SCOPE, path,
            )
            self._log_scope_reset(raw.get("scope"), path)
            self._save()
            return
        self._date = date
        self._used = used
        self._channels = {}
        reservations = raw.get("reservations")
        if isinstance(reservations, dict):
            self._reservations = {
                str(key): {"channel": str(value.get("channel") or "video_share"),
                           "stream_id": str(value.get("stream_id") or "")}
                for key, value in reservations.items() if isinstance(value, dict)
            }
        channels = raw.get("channels")
        if isinstance(channels, dict):
            for key, value in channels.items():
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    self._channels[str(key)] = value

    def _save(self) -> None:
        """计数变化即原子落盘；失败降级内存态（单次告警，不中断事件分发）。"""
        path = self._state_path
        if path is None:
            return
        state = {"date": self._date, "used": self._used, "channels": dict(self._channels),
                 "scope": self.STATE_SCOPE, "reservations": dict(self._reservations)}
        if save_json_atomic(path, state):
            self._persist_warned = False
            return
        if not self._persist_warned:
            self._persist_warned = True
            logger.warning("打扰预算落盘失败，降级为内存态计数（重启后计数将丢失）：%s", path)

    def _log_scope_reset(self, previous_scope: Any, path: Path) -> None:
        """旧语义计数被重置的留痕（§八.6：「记一条决策日志」；同签名每日一次）。"""
        if self._scope_reset_logged:
            return
        self._scope_reset_logged = True
        logger_ = self._decision_logger
        if logger_ is None:
            return
        try:
            logger_.record(
                action="budget.scope_reset",
                trigger_type="proactive_budget",
                reason=(f"主动打扰预算旧数据无 scope（收窄前旧语义计数），"
                        f"按新语义（{self.STATE_SCOPE}）重置当日计数"),
                previous_scope=str(previous_scope or ""), scope=self.STATE_SCOPE,
                used=0, date=self._date, file=str(path),
            )
        except Exception:
            pass

    # ---- 计数身份（M11 §3.3：仅目标陪伴私聊） ----
    def _target_stream(self) -> str:
        """目标陪伴私聊 stream_id（G11：装配期注入的 ``plugin._stream_id`` 取值回调）。"""
        provider = self._target_stream_provider
        if provider is None:
            return ""
        try:
            value = provider() if callable(provider) else provider
        except Exception:
            return ""
        return str(value or "").strip()

    def _is_target_stream(self, stream_id: str) -> bool:
        """该次主动打扰是否发往目标陪伴私聊（收窄的唯一判据）。

        目标流不可得（未注入回调 / 尚未解析出 stream）→ 返回 True（沿用旧全量语义）：
        此时收窄无从判定，静默停账会让预算彻底失效；装配侧（``plugin.on_load``）恒注入
        ``plugin._stream_id``，运行期按目标私聊收窄。空 ``stream_id``（调用方未给流身份）
        在目标流已知时按**非目标**处理——只对「确证发往目标私聊」的主动打扰计数。
        """
        target = self._target_stream()
        if not target:
            return True
        sid = str(stream_id or "").strip()
        return bool(sid) and sid == target

    # ---- 计数与查询 ----
    def _roll_date(self) -> None:
        """日期变化 → 计数清零并落盘。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._used = 0
            self._channels = {}
            self._reservations = {}
            self._save()

    def _config(self) -> Any:
        """取当前配置（provider 可为「返回配置的可调用对象」或配置对象本身）。"""
        provider = self._config_provider
        if provider is None:
            return None
        try:
            return provider() if callable(provider) else provider
        except Exception:
            return None

    def base_cap(self) -> int:
        """基础每日预算 = ``[patrol].daily_max_speak``（取不到回退 5；恒 ≥ 0）。"""
        cfg = self._config()
        patrol = getattr(cfg, "patrol", None)
        daily = getattr(patrol, "daily_max_speak", 5)
        try:
            daily_int = int(daily if daily is not None else 5)
        except (TypeError, ValueError, OverflowError):
            daily_int = 5
        return max(0, daily_int)

    def _elasticity_enabled(self) -> bool:
        cfg = self._config()
        patrol = getattr(cfg, "patrol", None)
        return bool(getattr(patrol, "budget_elasticity_enabled", False))

    def set_elasticity_provider(self, provider: Any) -> None:
        """装配期注入弹性信号源（可调用 → dict）；不传 provider 即保持基础预算。"""
        self._elasticity_provider = provider

    def _elasticity_signals(self) -> Dict[str, Any]:
        """取弹性信号（provider 可调用 → dict；异常/非 dict → 空信号，保守不追加）。"""
        provider = self._elasticity_provider
        if provider is None:
            return {}
        try:
            value = provider() if callable(provider) else provider
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _affection_ratio(value: Any) -> Optional[float]:
        """好感信号归一化到 0~1：支持比例、v2 分数（0~200）、{ratio|score|tier}。"""
        if isinstance(value, dict):
            ratio = value.get("ratio")
            if ratio is not None:
                return _clamp01(ratio)
            score = value.get("score")
            if score is not None:
                return _clamp01(_as_float(score) / max(1.0, _as_float(value.get("max_score"), 200.0)))
            tier, size = value.get("tier"), value.get("tier_size")
            if tier is not None:
                count = max(1, int(_as_float(size, 5.0)))
                return _clamp01(_as_float(tier) / max(1, count - 1))
            return None
        numeric = _as_float(value, -1.0)
        if numeric < 0:
            return None
        # ≤1 视为已归一化比例；>1 按好感 v2 的 0~200 分制归一
        return _clamp01(numeric if numeric <= 1.0 else numeric / 200.0)

    def elasticity_breakdown(self) -> Dict[str, Any]:
        """3.4 弹性预算分解（诊断/测试）：``{base, bonus, cap, reasons, enabled}``。

        provider 期望返回（键均可缺省）：
        ``{"affection": 0~1 或 0~200 分或 {"ratio"|"score"|"tier"},
        "valence": -1~1 或 "vad": {"valence": …}, "holiday": bool|str}``；
        缺省/异常 → 无信号（不追加），绝不因信号读取失败改动基础预算。

        规则（默认开；显式关闭时不计）：
        - 高好感（归一化 ≥0.7）/ 高 Valence（≥0.6）/ 节日 三条强信号；
        - 命中 1 条 +1，命中 ≥2 条 +2；**硬上限 base+2**，任何信号组合都不再上浮；
        - ``daily_max_speak=0``（用户明确关闭可选主动回合）时弹性不复活，恒 0。
        冷却不在本类：`allow_proactive` 只回答额度，夜间/静默/冷却/授权闸仍在各发布方
        原有链路上先于预算执行，弹性只放大额度、不能绕过任何闸。
        """
        base = self.base_cap()
        reasons: list[str] = []
        if self._elasticity_enabled() and base > 0:
            signals = self._elasticity_signals()
            affection_value = signals.get("affection")
            if affection_value is None:
                affection_value = signals.get("affection_ratio")
            if affection_value is None:
                affection_value = signals.get("affection_score")
            affection = self._affection_ratio(affection_value)
            valence_value = signals.get("valence")
            if valence_value is None and isinstance(signals.get("vad"), dict):
                valence_value = signals["vad"].get("valence")
            if valence_value is None and isinstance(signals.get("emotion"), dict):
                valence_value = signals["emotion"].get("valence")
            valence = _as_float(valence_value, -1.0)
            holiday = signals.get("holiday", signals.get("festival", signals.get("is_holiday")))
            holiday_text = str(holiday).strip().casefold() if holiday is not None else ""
            if affection is not None and affection >= self.ELASTIC_AFFECTION_MIN_RATIO:
                reasons.append("affection")
            if valence >= self.ELASTIC_VALENCE_MIN:
                reasons.append("valence")
            if (holiday is True or (isinstance(holiday, str)
                                    and holiday_text not in self.ELASTIC_HOLIDAY_FALSE)):
                reasons.append("holiday")
        bonus = min(self.ELASTIC_MAX_BONUS, len(reasons))
        # 硬上限：base+2，且不越过配置字段的 50 上限；base 本身异常超限时不被弹性压低。
        hard_cap = max(base, min(base + bonus, self.ELASTIC_ABSOLUTE_MAX))
        return {"base": base, "bonus": hard_cap - base, "cap": hard_cap,
                "reasons": reasons, "enabled": self._elasticity_enabled()}

    def cap(self) -> int:
        """目标陪伴私聊的每日主动打扰上限（基础预算 + 有界弹性）。

        2026-10-10 配置整理轮：独立上限字段（旧 ``proactive_target_daily_cap`` /
        ``global_proactive_cap``）已删除——预算按发送流身份计数：发往目标陪伴私聊的
        主动打扰都计入（发布方不限，含赖床提醒；早/晚安免预算，见 planning/proactive_service
        的免预算口径；活动切换开口在目标流已解析时不占），因此「每日预算」本身就是上限。

        3.4（2026-11-11）：``budget_elasticity_enabled`` 默认开（配置默认值轮）；命中高好感/
        高 Valence/节日时在基础预算上追加 1~2 次，硬上限 base+2（见 :meth:`elasticity_breakdown`）。
        显式关闭时返回值与旧实现逐字一致（取不到配置回退 5）。
        """
        return int(self.elasticity_breakdown()["cap"])

    def used_today(self) -> int:
        # F8（2026-11-04）：查询/记账与预留/提交统一持锁（RLock 可重入，嵌套调用安全）。
        with self._lock:
            self._roll_date()
            return self._used

    def channel_counts(self) -> Dict[str, int]:
        """当日各渠道计数快照（诊断用）。"""
        with self._lock:
            self._roll_date()
            return dict(self._channels)

    def allow_proactive(self, channel: str = "", intent: str = "",
                        stream_id: str = "") -> bool:
        """预算查询（仅目标陪伴私聊）：未达上限放行；达上限拦截并留痕（action="budget.blocked"）。

        ``stream_id`` 非目标陪伴私聊 → 直接放行（不查计数、不留痕；§3.3）。
        """
        with self._lock:
            if not self._is_target_stream(stream_id):
                return True
            self._roll_date()
            limit = self.cap()
            if self._used + len(self._reservations) < limit:
                return True
            self._log_blocked(channel, intent, limit)
            return False

    def _count_one(self, channel: str) -> None:
        """入账一次并落盘（调用方须持锁；**不重查流身份**——身份判定在入口完成）。"""
        self._used += 1
        key = str(channel or "unknown")
        self._channels[key] = self._channels.get(key, 0) + 1
        self._save()

    def record_used(self, channel: str = "", intent: str = "",
                    stream_id: str = "") -> None:
        """记一次主动打扰（proactive.used 订阅入口 / 直接调用）；计数变化即落盘。

        ``stream_id`` 非目标陪伴私聊 → **不入账**（§3.3：只有发往目标私聊的主动打扰计数）。
        """
        with self._lock:
            if not self._is_target_stream(stream_id):
                return
            self._roll_date()
            self._count_one(channel)

    async def on_proactive_used(self, payload: Dict[str, Any]) -> None:
        """``proactive.used`` 订阅者（M0 唯一实际订阅者）：按 payload 的 stream 身份入账。"""
        if not isinstance(payload, dict):
            return
        if payload.get("reservation_id"):
            self.commit_reservation(str(payload["reservation_id"]))
            return
        self.record_used(str(payload.get("channel") or ""), str(payload.get("intent") or ""),
                         str(payload.get("stream_id") or ""))

    def remaining(self) -> int:
        """Available slots include in-flight/uncertain sends; the cap is unchanged."""
        with self._lock:
            self._roll_date()
            return max(0, self.cap() - self._used - len(self._reservations))

    def reserve(self, channel: str, stream_id: str) -> Optional[str]:
        """Persist a slot before I/O, only for the configured companion target."""
        with self._lock:
            if not self._target_stream() or not self._is_target_stream(stream_id):
                return None
            self._roll_date()
            if self.remaining() <= 0:
                return None
            token = uuid4().hex
            self._reservations[token] = {"channel": str(channel), "stream_id": str(stream_id)}
            self._save()
            if self._persist_warned:
                self._reservations.pop(token, None)
                return None  # Cannot persist intent: do not send.
            return token

    def commit_reservation(self, token: str) -> bool:
        """Convert a reserved slot to one successful send, at most once.

        F8（2026-11-04）：按**预留内记录的 stream** 直接入账，不再重查当前目标流——
        reserve 时已校验目标身份；若 reserve→commit 之间热重载重建了目标流
        （同一用户新 stream_id），旧式重查会把已发送的分享弹出预留却不计数（边缘性少计）。
        """
        with self._lock:
            self._roll_date()
            entry = self._reservations.pop(str(token), None)
            if entry is None:
                return False
            self._count_one(str(entry.get("channel") or ""))
            return True

    def release_reservation(self, token: str) -> bool:
        """Release only a confirmed unsent slot. Unknown delivery stays reserved."""
        with self._lock:
            self._roll_date()
            if self._reservations.pop(str(token), None) is None:
                return False
            self._save()
            return True

    # ---- 内部 ----
    def _log_blocked(self, channel: str, intent: str, limit: int) -> None:
        logger.warning(
            "主动打扰预算拦截：渠道=%s，当日已用 %d/%d（intent=%s）",
            channel or "(未署名)", self._used, limit, intent or "-",
        )
        logger_ = self._decision_logger
        if logger_ is None:
            return
        try:
            logger_.record(
                action="budget.blocked",
                trigger_type="proactive_budget",
                reason=f"当日主动打扰已用 {self._used}/{limit}（仅目标陪伴私聊），本次触发被拦截",
                channel=str(channel or ""), intent=str(intent or ""),
                used=self._used, cap=limit, date=self._date,
            )
        except Exception:
            pass


def proactive_elasticity_signals(*, affection: Any, emotion: Any,
                                 holiday_provider: Any) -> Dict[str, Any]:
    """3.4 预算弹性信号只读 provider：``{affection, valence, holiday}``（缺项=不追加）。

    R2 结构审计批（2026-11-12）：自组合根 ``plugin._proactive_elasticity_signals``
    逐字下沉——预算弹性信号的取数算法归预算所有者所在包；组合根只保留薄委托，
    依赖（好感/情绪/节假日端口）由装配根注入，本函数不 import 任何服务束。

    ``affection`` 传好感档位快照 ``{"score": 0~200, "ratio": 0~1,
    "tier": 当前档位, "tier_size": 生效档位数}``——ProactiveBudget 优先读
    ``ratio``（= score/200，与 200 分制一致），档位仅供诊断；``valence`` 取
    emotion 生效 V，``holiday`` 只认 HolidayService 缓存中的法定节假日条目
    （经 ``holiday_provider`` 公开只读端口）。三者都是只读；任何一项读取失败
    只丢该项，绝不改基础预算。
    """
    signals: dict = {}
    try:
        if affection is not None:
            enabled = getattr(affection, 'is_enabled', None)
            if not callable(enabled) or enabled():
                score = float(affection.score())
                signal: dict = {'score': score,
                                'ratio': max(0.0, min(1.0, score / 200.0))}
                tier = getattr(affection, 'tier', None)
                if callable(tier):
                    try:
                        signal['tier'] = int(tier())
                    except Exception:
                        pass
                preset = getattr(affection, 'preset', None)
                if callable(preset):
                    try:
                        signal['tier_size'] = int(preset().size)
                    except Exception:
                        pass
                signals['affection'] = signal
    except Exception:
        pass
    try:
        getter = getattr(emotion, 'effective_valence', None)
        if callable(getter):
            value = getter()
            if value is not None:
                signals['valence'] = float(value)
    except Exception:
        pass
    try:
        signals['holiday'] = holiday_provider()
    except Exception:
        pass
    return signals


