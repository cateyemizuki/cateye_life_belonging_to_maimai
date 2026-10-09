"""主动巡检调度器（来源：虚拟陪伴插件 scheduler.py，判定链完整对齐）。

单条 asyncio 循环（可打断睡眠：wait_for(stop_event.wait(), timeout)），每轮按优先级判定：
日切 → 静默(fail-safe) → 非夜预算 → 早晚安状态检查 →
想念（沉默阈值每轮随机采样 + 概率 + LLM 把关）→
日程巡检（活动分享 / 日常搭话，受「启用日程巡检」开关与概率控制）。

**与早晚安的关系（2026-10-10 配置整理轮口径）**：早安/晚安只有一条驱动链——
planning 的 L2 漂移机制（[planning_proactive]）。本巡检器**从不**触发问候，
只读取 affection 的按天标记（planning 触发成功后写入）：标记已置位即记一条
``already_sent_today`` 跳过原因，两模块因此不会各发一条。patrol 自身可发的只有
想念 / 日程分享 / 日常搭话三类；早/晚安/问候一律不占陪伴侧预算。

**预算口径**：``[patrol].daily_max_speak`` 即上限（不再有独立上限字段），
只统计发往目标陪伴私聊的「日程巡检 + 想念」两类触发；早/晚安免预算。

所有触发统一走 ctx.maisaka.proactive.trigger（人格化主动回合，不直发文本），
计 0.5 发言数；replyer 真实回复后由 Hook 补计 0.5（credit_reply_half）。

决策日志每轮恰好一条：SKIP_PRIORITY 决定多原因时保留哪个；
触发前先看一眼陪伴对象电脑（cateye_client），屏幕旁白发布到 ScreenContextStore
（尾随锚点注入由 components 的请求 Hook 完成），触发未入队则撤掉旁白。

日程一律消费 planning 子模块：当前活动/未来活动经 planning.schedule_query.ScheduleQuery。
提示词一律读 suite_prompts（[prompts] 配置段 or 出厂默认）。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from typing import Any, Optional

from core.diag import DecisionLogger
from core.llm_utils import LLMService
from core.time_utils import hours_between, parse_hhmm, parse_hours_range
from planning.schedule_query import ScheduleQuery
from planning.time_utils import is_in_time_window
from suite_prompts import (
    COMPANION_ACTIVITY_REASON_DEFAULT,
    COMPANION_DAILY_REASON_DEFAULT,
    COMPANION_MISS_CONFIRM_DEFAULT,
    COMPANION_MISS_REASON_DEFAULT,
    SCREEN_NARRATION_DEFAULT,
    SCREEN_NARRATION_FAILED_DEFAULT,
    SCREEN_NARRATION_OFFLINE_DEFAULT,
    prompt,
    render,
)

from .affection import AffectionManager
from .cateye_client import PEEK_OFFLINE
from .screen_context import ScreenContextStore, format_narration

# 想念被 LLM 驳回后，多久之内不再重复发起把关判断（分钟）
MISS_REJECT_COOLDOWN_MINUTES = 30.0

#: 沉默事件（relation.silence_detected，M3/B1）频控：每日 ≤2 次、间隔 ≥4 小时（§4.2 / §6.2 A）
SILENCE_EVENT_MAX_PER_DAY = 2
SILENCE_EVENT_MIN_INTERVAL_HOURS = 4.0

#: mood_effect 乘数夹取范围（附录 C：全部 clamp [0.7, 1.3]）
MOOD_MULTIPLIER_MIN = 0.7
MOOD_MULTIPLIER_MAX = 1.3

#: 情绪不可得时的恒等 mood_effect（emotion 关闭/缺失 → 乘数恒 1.0，§八.2 可空降级）
NEUTRAL_MOOD_EFFECT: dict[str, Any] = {
    "proactive_rate": 1.0,
    "miss_threshold": 1.0,
    "post_rate": 1.0,
    "intent_hint": "",
}

#: 需要校验格式（HH:MM）的时间窗口配置项（非法时 fail-safe：静默视为在静默）。
#: 早/晚安窗口字段自 B1 合并起已废弃、2026-10-10 配置整理轮**删除**，
#: 现在只剩静默时段一项。
TIME_WINDOW_KEYS = (
    "silence_start",
    "silence_end",
)

#: 跳过原因优先级：一轮巡检里若多个候选触发都被挡，只保留优先级最高的那个原因。
#: 数值越大越"值得报告"；同优先级时保留先出现的（即更高等级的触发）。
SKIP_PRIORITY: dict[str, int] = {
    "invalid_time_config": 100,
    "disabled": 96,
    "daily_max_zero": 96,
    "activity_disabled": 94,
    "silence": 95,
    "planner_trigger_failed": 85,
    "min_interval": 80,
    "cooldown": 70,
    "budget": 60,
    "llm_rejected": 50,
    "dice": 40,
    "miss_duration": 25,
    "already_sent_today": 10,
    "no_user_message": 10,
    "no_candidate": 0,
}

ACTION_TRIGGER = "trigger"
ACTION_SKIP = "skip"


class PatrolService:
    """巡检调度引擎（判定链与源插件 scheduler.py 逐条对齐）。"""

    def __init__(
        self,
        ctx: Any,
        config: Any,
        affection: AffectionManager,
        schedule_query: Optional[ScheduleQuery],
        llm: Optional[LLMService],
        decision_logger: Optional[DecisionLogger] = None,
        *,
        cateye: Optional[Any] = None,
        screen_store: Optional[ScreenContextStore] = None,
    ) -> None:
        self._ctx = ctx
        self._config = config  # plugin 引用：运行时动态读 plugin.config.patrol
        self._affection = affection
        self._schedule = schedule_query  # planning.ScheduleQuery，可能为 None（降级）
        self._llm = llm
        self._decision_logger = decision_logger
        self._cateye = cateye  # CateyeClient，可能为 None（不看电脑）
        self._screen_store = screen_store  # ScreenContextStore，可能为 None（不注入旁白）
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._target_qq = ""
        self._stream_id = ""
        self._lover_name = "麦麦"
        self._personality = ""
        self._relation = None  # companion.relation.RelationProfile（关系档案）
        # 目标用户显示名（屏幕旁白里写「你看了眼 X 的电脑屏幕」，不带 QQ 号）
        self._target_display_name = ""
        self._last_trigger_time: Optional[datetime] = None
        self._last_trigger_intent = ""
        self._miss_last_reject_at: Optional[datetime] = None
        self._miss_threshold_hours: Optional[float] = None  # 本轮随机阈值（沉默越久越易过）
        self._last_tick_at: Optional[datetime] = None
        # M3 沉默事件频控（内存态，进程重启即清零；§4.2：每日 ≤2 次、间隔 ≥4h、仅 target）
        self._silence_pub_date = ""
        self._silence_pub_count = 0
        self._silence_last_pub_at: Optional[datetime] = None
        # 非法时间窗口配置的告警节流：配置项 -> 已告警过的原始值
        self._invalid_time_warned: dict[str, str] = {}

    # ---- 事件总线（M0；self._config 可能是 plugin 或配置对象，均可空降级）----
    def _event_bus(self) -> Any:
        bus = getattr(self._config, "_bus", None)
        if bus is None:
            bus = getattr(getattr(self._config, "config", None), "_bus", None)
        return bus

    # ---- 配置段访问（self._config 可能是 plugin（.config 承载各段）或配置对象本身）----
    def _patrol_cfg(self) -> Any:
        cfg_section = getattr(self._config, "patrol", None)
        if cfg_section is not None:
            return cfg_section
        return getattr(getattr(self._config, "config", None), "patrol", None)

    # ---- 目标会话与人设 ----
    def set_target(self, target_qq: str, stream_id: str) -> None:
        self._target_qq = target_qq
        self._stream_id = stream_id

    def set_persona(self, lover_name: str, personality: str = "") -> None:
        """设置陪伴对象名称与人设（服务束的 set_persona 入口透传到这里）。"""
        if lover_name:
            self._lover_name = lover_name
        self._personality = personality or ""

    def set_relation(self, relation: Any) -> None:
        """注入关系档案（companion.relation.RelationProfile；miss 提示词按关系称谓生成）。"""
        if relation is not None:
            self._relation = relation

    def set_target_display_name(self, name: str) -> None:
        """设置目标用户显示名（屏幕旁白文案用；取不到时旁白回退「陪伴对象」）。"""
        if name:
            self._target_display_name = str(name).strip()

    def has_target(self) -> bool:
        return bool(self._stream_id)

    def patrol_status(self) -> dict[str, Any]:
        """巡检运行状态快照（诊断与注入范围判定共用）。"""
        task = self._task
        return {
            "stream_id": self._stream_id,
            "stream_id_ready": bool(self._stream_id),
            "running": bool(task is not None and not task.done()),
            "last_tick_at": self._last_tick_at.strftime("%Y-%m-%d %H:%M:%S") if self._last_tick_at else "",
            "interval_minutes": int(getattr(self._patrol_cfg(), "check_interval_minutes", 0) or 0),
            "trigger_enabled": bool(getattr(self._patrol_cfg(), "proactive_trigger_enabled", True)),
            "last_trigger_time": self._last_trigger_time.isoformat() if self._last_trigger_time else "",
            "last_trigger_intent": self._last_trigger_intent,
        }

    def get_last_trigger_time(self) -> Optional[datetime]:
        """上次成功 proactive trigger 的时间（主动回合软约束注入用）。"""
        return self._last_trigger_time

    def get_last_trigger_intent(self) -> str:
        return self._last_trigger_intent

    def clear_last_trigger_time(self) -> None:
        """清除待确认触发（replyer Hook 补计后调用，保证 0.5 只补一次）。"""
        self._last_trigger_time = None
        self._last_trigger_intent = ""

    # ---- 生命周期 ----
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                pass  # 单轮失败不影响循环
            interval = max(1, int(getattr(self._patrol_cfg(), "check_interval_minutes", 10))) * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def credit_reply_half(self) -> bool:
        """replyer 真实回复后补计 0.5 发言数（触发后 90 秒窗口内才计，只计一次）。"""
        if self._last_trigger_time and (datetime.now() - self._last_trigger_time).total_seconds() <= 90:
            self._affection.increment_speak(0.5)
            self.clear_last_trigger_time()
            return True
        return False

    # ------------------------------------------------------------------
    # 单轮巡检：完整判定链（每轮恰好写一条决策日志）
    # ------------------------------------------------------------------
    async def _tick(self) -> None:
        patrol = self._patrol_cfg()
        if not bool(getattr(patrol, 'enabled', True)):
            return  # 模块独立总开关：停用期间巡检不作任何触发
        now = datetime.now()
        current_hhmm = f"{now.hour:02d}:{now.minute:02d}"
        self._last_tick_at = now

        # M4（§6.2 C）：概率掷骰前读 mood_effect（emotion 关闭/缺失/异常 → 恒 1.0，可空降级）；
        # activity_trigger_rate/default_speak_rate × proactive_rate，乘后概率落 decision_logs。
        mood = self._mood_effect()
        proactive_rate = self._mood_multiplier(
            mood.get("proactive_rate"), 1.0, lo=MOOD_MULTIPLIER_MIN, hi=MOOD_MULTIPLIER_MAX)
        mood_intent_hint = str(mood.get("intent_hint") or "").strip()
        activity_rate = (
            self._to_float(getattr(patrol, "activity_trigger_rate", 1.0), 1.0) * proactive_rate
        )
        default_rate = (
            self._to_float(getattr(patrol, "default_speak_rate", 1.0), 1.0) * proactive_rate
        )

        # 1) 日切（保留 last_speak_time：跨午夜间隔约束仍生效）
        if self._affection.today_date() != now.strftime("%Y-%m-%d"):
            self._affection.reset_daily(now.strftime("%Y-%m-%d"))
            # M1 日衰减（§5.5）：score 向 baseline 回归 + baseline 缓慢漂移，每自然日一次
            self._affection.apply_daily_decay()

        daily_max_speak = int(patrol.daily_max_speak)
        # 预算口径（2026-10-10 配置整理轮）：每日预算即上限，只统计巡检与想念；
        # 早/晚安由 planning 漂移机制驱动且免预算，不再为其预留配额。
        non_night_budget = daily_max_speak

        # 最小间隔 / 时间配置校验（早/晚安始终豁免本限制，且不由本巡检器触发）
        minutes_since_last_speak = self._minutes_since_last_speak(now)
        min_interval = max(0, int(getattr(patrol, "min_trigger_interval_minutes", 0)))
        interval_blocked = (
            min_interval > 0
            and minutes_since_last_speak is not None
            and minutes_since_last_speak < min_interval
        )
        interval_desc = (
            f"距上次主动发言 {minutes_since_last_speak:.0f} 分钟 < 最小间隔 {min_interval} 分钟"
            if interval_blocked
            else ""
        )
        invalid_time_keys = self._collect_invalid_time_keys()

        # 本轮判定（每轮恰好写一条；真的发言后，后续门控的"跳过"不再覆盖它）
        decision: dict[str, str] = {
            "action": ACTION_SKIP,
            "reason": "no_candidate",
            "trigger_type": "",
            "detail": "本轮没有满足条件的触发",
        }
        base_fields: dict[str, Any] = {
            "minutes_since_last_speak": (
                None if minutes_since_last_speak is None else round(minutes_since_last_speak, 1)
            ),
            "min_interval_minutes": min_interval,
            "cooldown_minutes": int(patrol.user_cooldown_minutes),
            "budget_used": round(self._affection.today_speak_count(), 2),
            "budget_limit": non_night_budget,
            # M4（§6.2 C / §6.3-7）：情绪乘数与乘后概率留痕（中性时逐值为 1.0）
            "mood_proactive_rate": round(proactive_rate, 4),
            "activity_trigger_rate_effective": round(activity_rate, 4),
            "default_speak_rate_effective": round(default_rate, 4),
        }
        if mood_intent_hint:
            base_fields["mood_intent_hint"] = mood_intent_hint
        if invalid_time_keys:
            base_fields["invalid_time_config"] = ",".join(invalid_time_keys)

        def mark_skip(reason: str, trigger_type: str = "", detail: str = "") -> None:
            """记录"本轮跳过"；多原因时只保留优先级更高的（同优先级保留先出现的）。"""
            if decision["action"] == ACTION_TRIGGER:
                return
            if decision["reason"] != "no_candidate" and (
                SKIP_PRIORITY.get(reason, 0) <= SKIP_PRIORITY.get(decision["reason"], 0)
            ):
                return
            decision.update(action=ACTION_SKIP, reason=reason, trigger_type=trigger_type, detail=detail)

        def mark_trigger(trigger_type: str, detail: str = "") -> None:
            decision.update(
                action=ACTION_TRIGGER,
                reason=f"{trigger_type}_trigger",
                trigger_type=trigger_type,
                detail=detail,
            )

        def finish() -> None:
            self._log_decision(**decision, **base_fields)

        # 总开关 / 每日上限
        if not patrol.proactive_trigger_enabled:
            mark_skip("disabled", detail="主动触发开关（proactive_trigger_enabled）已关闭")
            finish()
            return
        if daily_max_speak <= 0:
            mark_skip("daily_max_zero", detail="每日发言上限为 0（完全静音）")
            finish()
            return

        # 静默时段（fail-safe：配置非法视为静默，宁可不发）
        if is_in_time_window(patrol.silence_start, patrol.silence_end, current_hhmm, invalid_result=True):
            if invalid_time_keys:
                mark_skip(
                    "invalid_time_config",
                    detail=f"时间窗口配置非法（{','.join(invalid_time_keys)}），按静默处理（fail-safe）",
                )
            else:
                mark_skip("silence", detail=f"处于静默时段 {patrol.silence_start}-{patrol.silence_end}")
            finish()
            return

        # ---- 早安 / 晚安的状态检查（patrol 从不触发问候，只做去重观察）----
        # 唯一驱动链 = planning 的 L2 漂移机制（[planning_proactive] 的漂移区间 + 开关）。
        # 旧的 [patrol] morning_start/end、night_start/end 四个窗口字段已于 2026-10-10
        # 配置整理轮**删除**（问候时刻一律用 [schedule].wake_time/sleep_time + 漂移）。
        # planning 触发成功后写入 affection 的 morning_sent/night_sent；这里见已发即记一条
        # 跳过原因——同一会话同一天早安/晚安各至多一条，两模块不会各发一条。
        if self._affection.morning_sent():
            mark_skip("already_sent_today", "morning", "今天已经发过早安（planning 漂移机制已发）")
        if self._affection.night_sent():
            mark_skip("already_sent_today", "night", "今天已经发过晚安（planning 漂移机制已发）")

        # ---- A 级：想念 / 沉默事件 ----
        # M3（B1）：沉默超过 miss_hours_min 时发布 relation.silence_detected（独立于
        # 想念触发本身；每日 ≤2 次、间隔 ≥4h 的内存频控在 _maybe_publish_silence 内）。
        await self._maybe_publish_silence(now)
        miss_speak_rate = float(patrol.miss_speak_rate)
        if not patrol.miss_enabled:
            pass  # 开关关闭：整段跳过（记 no_candidate）
        elif self._affection.miss_sent_today():
            mark_skip("already_sent_today", "miss", "今天已经发过想念")
        else:
            last_msg = self._affection.last_user_msg_time()
            if last_msg is None:
                # 用户从未发过消息 → 不触发想念（避免首次启动就喊想你了）
                mark_skip("no_user_message", "miss", "还没有用户消息记录，不触发想念")
            else:
                hours_since_last = hours_between(now, last_msg)
                # M4（附录 B/C，§7 M4 行）：阈值 = 区间随机采样 × 情绪系数 × affection 档位系数
                # （双系数；emotion 关闭/档位不可得 → 各自恒 1.0，可空降级）
                mood_miss_mult, tier_miss_mult = self._miss_threshold_factors(mood)
                miss_threshold = self._sample_miss_threshold(
                    mood_mult=mood_miss_mult, tier_mult=tier_miss_mult)
                base_fields.update(
                    miss_threshold_hours=round(miss_threshold, 2),
                    mood_miss_threshold=round(mood_miss_mult, 4),
                    tier_miss_factor=round(tier_miss_mult, 4),
                )
                if hours_since_last <= miss_threshold:
                    mark_skip(
                        "miss_duration",
                        "miss",
                        f"用户沉默 {hours_since_last:.1f}h ≤ 本轮随机阈值 {miss_threshold:.1f}h"
                        f"（区间 {self._miss_range_text()}h"
                        f"×情绪 {mood_miss_mult:.2f}×档位 {tier_miss_mult:.2f}）",
                    )
                elif self._affection.today_speak_count() >= non_night_budget:
                    mark_skip(
                        "budget",
                        "miss",
                        f"当日预算已用 {self._affection.today_speak_count():.1f}/{non_night_budget}",
                    )
                elif interval_blocked:
                    # 想念不豁免最小间隔（2026-10-10 口径：早/晚安豁免，想念照旧受控）
                    mark_skip("min_interval", "miss", interval_desc)
                elif self._is_in_cooldown(now):
                    mark_skip(
                        "cooldown",
                        "miss",
                        f"处于用户冷却期（{int(patrol.user_cooldown_minutes)} 分钟）内",
                    )
                elif random.random() >= miss_speak_rate:
                    mark_skip("dice", "miss", f"想念概率未通过（miss_speak_rate={miss_speak_rate}）")
                elif self._miss_llm_check_enabled() and not await self._confirm_missing_with_llm(
                    hours_since_last, now
                ):
                    mark_skip("llm_rejected", "miss", "LLM 把关驳回（30 分钟内不重复判断）")
                elif await self._trigger_missing(hours_since_last):
                    mark_trigger("miss", f"想念触发已入队（用户沉默 {hours_since_last:.1f}h"
                                         f" > 阈值 {miss_threshold:.1f}h）")
                else:
                    mark_skip("planner_trigger_failed", "miss", "触发未入队（stream_id 缺失或触发开关关闭）")

        # ---- B 级：日程巡检（活动分享 / 日常搭话；受「启用日程巡检」开关控制）----
        activity = self._find_current_activity(now)
        if not self._patrol_activity_enabled():
            mark_skip("activity_disabled", detail="日程巡检开关（patrol_activity_enabled）已关闭")
        elif activity is not None:
            if interval_blocked:
                mark_skip("min_interval", "activity", interval_desc)
            elif self._is_in_cooldown(now):
                mark_skip(
                    "cooldown",
                    "activity",
                    f"处于用户冷却期（{int(patrol.user_cooldown_minutes)} 分钟）内",
                )
            elif self._affection.today_speak_count() >= non_night_budget:
                mark_skip(
                    "budget",
                    "activity",
                    f"当日预算已用 {self._affection.today_speak_count():.1f}/{non_night_budget}",
                )
            elif random.random() >= activity_rate:
                mark_skip(
                    "dice",
                    "activity",
                    f"日程巡检概率未通过（activity_trigger_rate={patrol.activity_trigger_rate}"
                    f"×情绪 {proactive_rate:.2f} = {activity_rate:.3f}）",
                )
            elif await self._trigger_activity(activity):
                mark_trigger("activity", f"日程节点触发已入队：{activity.get('name', '')}")
            else:
                mark_skip("planner_trigger_failed", "activity", "触发未入队（stream_id 缺失或触发开关关闭）")
        else:
            if interval_blocked:
                mark_skip("min_interval", "daily", interval_desc)
            elif self._is_in_cooldown(now):
                mark_skip(
                    "cooldown",
                    "daily",
                    f"处于用户冷却期（{int(patrol.user_cooldown_minutes)} 分钟）内",
                )
            elif self._affection.today_speak_count() >= non_night_budget:
                mark_skip(
                    "budget",
                    "daily",
                    f"当日预算已用 {self._affection.today_speak_count():.1f}/{non_night_budget}",
                )
            elif random.random() >= default_rate:
                mark_skip(
                    "dice",
                    "daily",
                    f"日常搭话概率未通过（default_speak_rate={patrol.default_speak_rate}"
                    f"×情绪 {proactive_rate:.2f} = {default_rate:.3f}）",
                )
            elif await self._trigger_daily():
                mark_trigger("daily", "日常巡检触发已入队")
            else:
                mark_skip("planner_trigger_failed", "daily", "触发未入队（stream_id 缺失或触发开关关闭）")

        finish()

    # ------------------------------------------------------------------
    # 各类触发（屏幕感知统一在 _trigger_planner 里先取）
    # ------------------------------------------------------------------
    async def _trigger_missing(self, hours_since_last: float) -> bool:
        reason = self._format_miss_reason(hours_since_last)
        success = await self._trigger_planner("missing", reason)
        if success:
            self._affection.set_miss_sent()
        return success

    async def _trigger_activity(self, activity: dict[str, Any]) -> bool:
        label = str(activity.get("name") or "")
        template = prompt(self._config, "companion_activity_reason")
        reason = render(
            template,
            fallback=COMPANION_ACTIVITY_REASON_DEFAULT,
            lover_name=self._lover_name,
            activity=label,
        )
        return await self._trigger_planner("activity", reason)

    async def _trigger_daily(self) -> bool:
        template = prompt(self._config, "companion_daily_reason")
        reason = render(template, fallback=COMPANION_DAILY_REASON_DEFAULT,
                        lover_name=self._lover_name)
        return await self._trigger_planner("daily", reason)

    async def _trigger_planner(self, intent: str, reason: str) -> bool:
        """统一触发 planner：主动打扰预算（仅目标陪伴私聊）查询 → proactive.used → 看电脑 → proactive.trigger。

        M0 预算接线（§六.2 H）：五类触发都走本入口，触发前先查主动打扰预算，
        超额直接放弃；入队失败/异常时把刚发布的旁白撤掉，避免它被之后某轮普通对话锚上。
        M11（附录 II §3.3）：预算只对**发往目标陪伴私聊**的主动打扰计数——本入口的
        ``self._stream_id``（``plugin._apply_target`` 注入的目标流）按 stream 维度传参，
        非目标流不查计数、不入账（自身 ``daily_max_speak`` 与 miss/daily 频控不变）。

        M4（§6.2 C）：reason 在此追加情绪 ``intent_hint``（低分辨成语义行，如
        「你今天有点低落，语气可以软一点」）；零新 Hook，情绪不可得时原样透传。
        """
        if not self._stream_id or not self._patrol_cfg().proactive_trigger_enabled:
            return False
        reason = self._append_intent_hint(reason)
        bus = self._event_bus()
        if bus is not None:
            if not bus.allow_proactive("patrol", str(intent or ""), stream_id=self._stream_id):
                return False
            try:
                await bus.publish("proactive.used", source="companion",
                                  channel="patrol", intent=str(intent or ""),
                                  stream_id=self._stream_id)
            except Exception:
                pass
        await self._publish_screen_narration()
        try:
            result = await self._ctx.maisaka.proactive.trigger(
                stream_id=self._stream_id, intent=intent, reason=reason
            )
        except Exception:
            # 判定结论由 _tick 的 finish() 统一落一条日志，这里只负责撤旁白
            self._discard_screen_narration()
            return False
        if isinstance(result, dict) and result.get("success") is False:
            self._discard_screen_narration()
            return False
        self._affection.increment_speak(0.5)  # 先计 0.5，replyer 回复后由 Hook 补 0.5
        self._last_trigger_time = datetime.now()
        self._last_trigger_intent = str(intent or "")
        return True

    # ------------------------------------------------------------------
    # 屏幕感知（v2.6.0「先看屏幕，再叫醒 planner」）
    # ------------------------------------------------------------------
    async def _publish_screen_narration(self) -> None:
        """看一眼陪伴对象电脑，把屏幕旁白发布到注入缓存。

        三种结果对应 [cateye] 的三个模板，模板留空 = 该情况不注入；
        功能关闭 / 整体超时 / 任何异常 → 不注入，绝不阻塞主动触发。
        """
        try:
            if self._screen_store is None or self._cateye is None:
                return
            now = datetime.now()
            peek = await self._cateye.peek_screen(now)
            if peek is None:
                return
            if peek.ok:
                template = prompt(self._config, "screen_narration")
                default_template = SCREEN_NARRATION_DEFAULT
            elif peek.status == PEEK_OFFLINE:
                template = prompt(self._config, "screen_narration_offline")
                default_template = SCREEN_NARRATION_OFFLINE_DEFAULT
            else:
                template = prompt(self._config, "screen_narration_failed")
                default_template = SCREEN_NARRATION_FAILED_DEFAULT
            if not template.strip():
                return
            text = format_narration(
                template,
                now=now,
                user_name=self._target_display_name,
                description=peek.description,
                default_template=default_template,
            )
            if self._screen_store.publish(self._stream_id, text):
                self._ctx.logger.info("屏幕旁白已挂入注入缓存（stream=%s，%s）：%s",
                                      self._stream_id, peek.status, text)
        except Exception as e:  # 锦上添花，绝不阻塞触发
            self._ctx.logger.warning(f"发布屏幕旁白失败（忽略）: {e}")

    def _discard_screen_narration(self) -> None:
        if self._screen_store is not None:
            self._screen_store.discard(self._stream_id)

    # ------------------------------------------------------------------
    # 想念机制辅助 / 沉默事件发布（M3，B1；§4.2 / §6.2 A）
    # ------------------------------------------------------------------
    async def _maybe_publish_silence(self, now: datetime) -> None:
        """last_user_msg_time 超过 miss_hours_min → 发布 ``relation.silence_detected``。

        频控（内存态，§4.2）：每日 ≤ :data:`SILENCE_EVENT_MAX_PER_DAY` 次、间隔 ≥
        :data:`SILENCE_EVENT_MIN_INTERVAL_HOURS` 小时；仅 target——巡检只服务目标私聊流
        （``has_target()`` 为假时不发布）。payload：``hours``（沉默时长）与
        ``target_bonus``（档位加算加成，留痕用；emotion 侧桥按同一公式重算 K，
        单一事实来源仍是 affection 档位 + 情绪服务的 ``target_bonus_step()``——M12/D7：
        该值含「JSON 覆盖 > config > 默认」三层合并，服务不可得时回退 config 直读）。

        本方法只发布事件，不参与想念触发判定；任何异常都静默吞掉，绝不影响巡检判定链。
        """
        try:
            if not self.has_target():
                return
            bus = self._event_bus()
            if bus is None:
                return
            last_msg = self._affection.last_user_msg_time()
            if last_msg is None:
                return
            hours = hours_between(now, last_msg)
            if hours <= self._miss_hours_min():
                return
            if not self._silence_rate_ok(now):
                return
            await bus.publish("relation.silence_detected", source="companion",
                              hours=round(hours, 2),
                              target_bonus=round(self._silence_target_bonus(), 4))
            self._silence_pub_count += 1
            self._silence_last_pub_at = now
        except Exception:
            pass

    def _silence_rate_ok(self, now: datetime) -> bool:
        """沉默事件频控检查（按日计数跨天清零 + 最小间隔；满足即放行）。"""
        today = now.strftime("%Y-%m-%d")
        if self._silence_pub_date != today:
            self._silence_pub_date = today
            self._silence_pub_count = 0
        if self._silence_pub_count >= SILENCE_EVENT_MAX_PER_DAY:
            return False
        if self._silence_last_pub_at is not None:
            elapsed_hours = (now - self._silence_last_pub_at).total_seconds() / 3600.0
            if elapsed_hours < SILENCE_EVENT_MIN_INTERVAL_HOURS:
                return False
        return True

    def _silence_target_bonus(self) -> float:
        """当前档位的 target 加算加成 = step × (档位+1)（§5.3 / §6.2 A；读不到按 0）。

        step 优先取情绪服务（``plugin._emotion.target_bonus_step()``；M12/D7 起该路径已含
        「JSON 覆盖 > config > 默认」三层合并，与桥内入账的 K 同源），服务不可得（模块停用/
        热重载窗口/异常）时回退 ``[emotion].target_bonus_step`` 直读。档位读 affection 状态层；
        任一不可得时返回 0（不猜档位、不猜配置）。
        """
        tier_getter = getattr(self._affection, "tier", None)
        if not callable(tier_getter):
            return 0.0
        try:
            tier = max(0, min(4, int(tier_getter())))
        except Exception:
            return 0.0
        step: Optional[float] = None
        service = self._emotion_service()
        step_getter = getattr(service, "target_bonus_step", None)
        if callable(step_getter):
            try:
                step = float(step_getter())
            except Exception:
                step = None
        if step is None:
            cfg = self._emotion_cfg()
            try:
                step = float(getattr(cfg, "target_bonus_step", 0.1) or 0.1)
            except (TypeError, ValueError):
                step = 0.1
        return max(0.0, min(1.0, step)) * (tier + 1)

    def _emotion_cfg(self) -> Any:
        """[emotion] 配置段（self._config 可能是 plugin 或配置对象本身；均可空降级）。"""
        cfg_section = getattr(self._config, "emotion", None)
        if cfg_section is not None:
            return cfg_section
        return getattr(getattr(self._config, "config", None), "emotion", None)

    # ---- M4 情绪消费（§6.2 C；跨模块 getattr 判空 + 恒等降级，§八.2）----
    def _emotion_service(self) -> Any:
        """情绪模块服务束（plugin._emotion；self._config 可能是 plugin 或配置对象本身）。"""
        emotion = getattr(self._config, "_emotion", None)
        if emotion is None:
            emotion = getattr(getattr(self._config, "config", None), "_emotion", None)
        return emotion

    def _mood_effect(self) -> dict[str, Any]:
        """读 ``EmotionService.mood_effect()``（M4；缺失/停用/异常 → 恒等中性值）。

        情绪模块自身在停用时已返回中性值；这里再做一次开关与异常兜底，保证
        「emotion 关闭 → patrol 乘数恒 1.0」不依赖对方实现细节（§八.2 可空降级）。
        """
        emotion = self._emotion_service()
        getter = getattr(emotion, "mood_effect", None)
        if not callable(getter):
            return dict(NEUTRAL_MOOD_EFFECT)
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return dict(NEUTRAL_MOOD_EFFECT)
            except Exception:
                return dict(NEUTRAL_MOOD_EFFECT)
        try:
            raw = getter()
        except Exception:
            return dict(NEUTRAL_MOOD_EFFECT)
        return raw if isinstance(raw, dict) else dict(NEUTRAL_MOOD_EFFECT)

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def _mood_multiplier(self, value: Any, default: float, *, lo: float, hi: float) -> float:
        """mood_effect 系数的防御性夹取（附录 C 范围；非法值取默认）。"""
        return max(lo, min(hi, self._to_float(value, default)))

    def _append_intent_hint(self, reason: str) -> str:
        """M4（§6.2 C）：proactive intent 的 reason 追加情绪 ``intent_hint``。

        中性/情绪不可得（intent_hint 为空串）时原样返回，既有 reason 字节不变。
        """
        hint = str(self._mood_effect().get("intent_hint") or "").strip()
        if not hint:
            return reason
        base = str(reason or "").strip()
        return f"{base}。{hint}" if base else hint

    def _affection_tier_miss_factor(self) -> float:
        """affection 档位 → miss 阈值系数（附录 B；读不到恒 1.0，可空降级）。

        档位读 affection 状态层（``tier()``），系数读当前关系档案
        （``RelationProfile.miss_factor_for_tier``，M1 已存数据）；任一不可得/异常回退 1.0。
        """
        tier_getter = getattr(self._affection, "tier", None)
        if not callable(tier_getter):
            return 1.0
        try:
            tier = max(0, min(4, int(tier_getter())))
        except Exception:
            return 1.0
        factor_getter = getattr(getattr(self, "_relation", None), "miss_factor_for_tier", None)
        if not callable(factor_getter):
            return 1.0
        try:
            factor = float(factor_getter(tier))
        except Exception:
            return 1.0
        return factor if factor > 0 else 1.0

    def _miss_threshold_factors(self, mood_effect: Optional[dict[str, Any]] = None) -> tuple[float, float]:
        """M4 双系数（附录 B/C）：(情绪系数, affection 档位系数)，各自不可得恒 1.0。"""
        mood = mood_effect if isinstance(mood_effect, dict) else self._mood_effect()
        mood_mult = self._mood_multiplier(mood.get("miss_threshold"), 1.0,
                                          lo=MOOD_MULTIPLIER_MIN, hi=MOOD_MULTIPLIER_MAX)
        return mood_mult, self._affection_tier_miss_factor()

    def _miss_hours_range(self) -> tuple[float, float]:
        """想念阈值区间（小时）：``[patrol].miss_silence_hours``（``"4-8"`` 或单数字 ``8`` = 0~8）。

        2026-10-10 阈值合并轮：旧 ``miss_hours_min`` / ``miss_hours_max`` 两字段合为一个
        区间字符串（配置层的迁移器已把旧值搬过来）；非法值一律回退 4~8。
        """
        return parse_hours_range(getattr(self._patrol_cfg(), "miss_silence_hours", "4-8"), (4.0, 8.0))

    def _miss_range_text(self) -> str:
        """阈值区间的展示文本（决策日志用）。"""
        low, high = self._miss_hours_range()
        return f"{low:g}-{high:g}"

    def _miss_hours_min(self) -> float:
        """想念阈值下限（小时）：沉默事件（relation.silence_detected）的触发门槛。"""
        return self._miss_hours_range()[0]

    def _sample_miss_threshold(self, *, mood_mult: float = 1.0, tier_mult: float = 1.0) -> float:
        """在配置区间内随机取本轮想念阈值（小时）。

        M4（§7 M4 行）：采样值再 × ``mood_effect.miss_threshold`` × affection 档位系数
        （双系数；调用方缺省 1.0 时退化为旧行为）。
        """
        low, high = self._miss_hours_range()
        return max(0.0, random.uniform(low, high) * float(mood_mult) * float(tier_mult))

    def _patrol_activity_enabled(self) -> bool:
        """日程巡检开关（``[patrol].patrol_activity_enabled``，取不到按开）。"""
        return bool(getattr(self._patrol_cfg(), "patrol_activity_enabled", True))

    def _miss_llm_check_enabled(self) -> bool:
        if self._llm is None:
            return False
        return bool(getattr(self._patrol_cfg(), "miss_llm_check_enabled", False))

    def _relation_word(self) -> str:
        """当前关系称谓（relation profile 未注入时回退「恋人」）。"""
        return getattr(self._relation, "word", None) or "恋人"

    def _format_miss_reason(self, hours_since_last: float) -> str:
        """想念触发的 reason（模板可配置；占位符坏掉回退出厂文案）。

        2026-10-10：模板首行自带「【触发原因·想念】」标注（用户要求：注入上下文要
        标明为什么触发），文案同时做了精简。
        """
        template = prompt(self._config, "companion_miss_reason")
        return render(
            template,
            fallback=COMPANION_MISS_REASON_DEFAULT,
            lover_name=self._lover_name,
            hours=f"{hours_since_last:.1f}",
            relation=self._relation_word(),
        )

    async def _confirm_missing_with_llm(self, hours_since_last: float, now: datetime) -> bool:
        """想念触发前的 LLM 把关：Y=放行，N/空/无法解析=驳回（30 分钟冷却）。"""
        if self._miss_recently_rejected(now):
            return False
        template = prompt(self._config, "companion_miss_confirm")
        activity = self._find_current_activity(now)
        activity_line = self._format_activity_line(activity, now) if activity else ""
        activity_context = f"你当前的活动：{activity_line}。" if activity else "你现在没有安排中的活动。"
        prompt_text = render(
            template,
            fallback=COMPANION_MISS_CONFIRM_DEFAULT,
            lover_name=self._lover_name,
            relation=self._relation_word(),
            personality=self._personality or "（未配置人设）",
            current_time=now.strftime("%H:%M"),
            hours=f"{hours_since_last:.1f}",
            activity_context=activity_context,
        )
        # mai-love v2.6.3：判定参数（模型/max_tokens/温度）集中在 [llm] 配置段（原 [judge]）——
        # 此前与日程共用 LLM 任务且 max_tokens 写死 16，任务切到思考型模型后
        # reasoning 吃满预算、正文恒空、把关整批失败；现可在 WebUI 单独指定。
        response = await self._llm.judge(prompt_text, event="miss_confirm")
        verdict = self._parse_missing_confirm(response)
        if verdict is True:
            return True
        self._miss_last_reject_at = now  # 只在新鲜驳回时记录，冷却可自然过期
        return False

    def _miss_recently_rejected(self, now: datetime) -> bool:
        if self._miss_last_reject_at is None:
            return False
        elapsed = (now - self._miss_last_reject_at).total_seconds() / 60
        return elapsed < MISS_REJECT_COOLDOWN_MINUTES

    @staticmethod
    def _parse_missing_confirm(response: str) -> Optional[bool]:
        """解析 LLM 把关回复：Y=放行，N/空/无法解析=驳回。"""
        text = str(response or "").strip()
        if not text:
            return None
        upper = text.upper()
        for ch in upper:
            if ch == "Y":
                return True
            if ch == "N":
                return False
        if any(kw in text for kw in ("不", "否", "拒绝", "驳回")):
            return False
        if any(kw in text for kw in ("是", "可以", "自然", "合适")):
            return True
        return None

    # ------------------------------------------------------------------
    # 通用小工具
    # ------------------------------------------------------------------
    def _find_current_activity(self, now: datetime) -> Optional[dict[str, Any]]:
        """当前日程活动（ScheduleQuery 不可用时返回 None，按"无活动"降级）。"""
        if self._schedule is None:
            return None
        try:
            return self._schedule.find_current_activity(now.strftime("%Y-%m-%d"), now)
        except Exception:
            return None

    def _format_activity_line(self, activity: dict[str, Any], now: datetime) -> str:
        if self._schedule is None:
            return str(activity.get("name") or "")
        try:
            return self._schedule.format_activity_line(activity, now)
        except Exception:
            return str(activity.get("name") or "")

    def _minutes_since_last_speak(self, now: datetime) -> Optional[float]:
        last_speak = self._affection.last_speak_time()
        if last_speak is None:
            return None
        return max(0.0, (now - last_speak).total_seconds() / 60)

    def _is_in_cooldown(self, now: datetime) -> bool:
        """用户冷却：自己或对方最近一次发言在冷却期内（早晚安不受控）。"""
        cooldown_minutes = int(self._patrol_cfg().user_cooldown_minutes)
        recent_times = [
            value
            for value in (self._affection.last_speak_time(), self._affection.last_user_msg_time())
            if value is not None
        ]
        if not recent_times:
            return False
        elapsed = (now - max(recent_times)).total_seconds() / 60
        return elapsed < cooldown_minutes

    def _collect_invalid_time_keys(self) -> list[str]:
        """收集格式非法的（HH:MM）时间窗口配置项，并对每个非法值告警一次。"""
        patrol = self._patrol_cfg()
        invalid: list[str] = []
        for key in TIME_WINDOW_KEYS:
            raw = str(getattr(patrol, key, "") or "")
            if parse_hhmm(raw, -1) < 0:
                invalid.append(key)
                if self._invalid_time_warned.get(key) != raw:
                    self._invalid_time_warned[key] = raw
                    try:
                        self._ctx.logger.warning(
                            f"时间窗口配置 {key}={raw!r} 不是 HH:MM 格式；已按 fail-safe 处理"
                            f"（静默时段视为静默、早晚安窗口视为不触发）。请检查插件配置。"
                        )
                    except Exception:
                        pass
        return invalid

    def _log(self, action: str, *, trigger_type: str = "", reason: str = "", detail: str = "",
             **fields: Any) -> None:
        """写一条巡检决策日志（未启用时静默 no-op，日志故障绝不影响巡检）。"""
        if self._decision_logger is None:
            return
        try:
            self._decision_logger.record(
                action=action, trigger_type=trigger_type, reason=reason, detail=detail, **fields
            )
        except Exception:
            pass

    def _log_decision(self, *, action: str, **fields: Any) -> None:
        self._log(action, **fields)
