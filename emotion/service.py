"""情绪系统服务：状态持久化 / 睡眠检测循环 / 人设初始化 / 情绪注入文本。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from core.storage import load_json, save_json_atomic
from planning.schedule_query import ScheduleQuery

from .referee import ask_sleep_referee, derive_params_from_persona, evaluate_reply_delta
from .state_machine import EmotionParams, EmotionStateMachine
from .vad import map_emotions, mood_summary


class EmotionService:
    """VAD 情绪系统的运行时载体（状态机 + 持久化 + 睡眠循环 + LLM 请求编排）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._ctx = plugin.ctx
        data_dir = Path(getattr(plugin, "_data_dir", Path(".")))
        self._state_path = data_dir / "emotion_state.json"
        self._config_path = data_dir / "emotion_config.json"

        cfg = getattr(plugin.config, "emotion", None)
        self._machine = EmotionStateMachine(
            params=EmotionParams.from_dict((load_json(self._config_path, {}) or {}).get("params")),
            decay_rate=float(getattr(cfg, "decay_rate", 0.08) or 0.08),
        )
        # 兼容旧状态文件：state 里的 params 以 config 文件为准（初始化结果不丢）
        state = load_json(self._state_path, {}) or {}
        if state.get("params"):
            self._machine.params = self._machine.params.from_dict(state.get("params"))
        self._machine.load_dict({**state, "params": self._machine.params.to_dict()})

        self._persona = str(getattr(plugin, "_personality", "") or "")
        self._lover_name = str(getattr(plugin, "_lover_name", "") or "麦麦")
        self._last_evaluate_at = 0.0
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._bg_tasks: list[asyncio.Task] = []
        # M0 事件：睡眠进出观测（schedule.sleep_started / woke_up 只在进出沿各发一次）
        self._in_sleep = False
        self._sleep_started_at: Optional[datetime] = None
        self._sleep_planned_hours = 0.0

    # ── 事件总线（M0；无总线时静默跳过）─────────────────────
    def _bus(self) -> Any:
        return getattr(self._plugin, "_bus", None)

    async def _publish(self, event: str, **payload: Any) -> None:
        bus = self._bus()
        if bus is None:
            return
        try:
            await bus.publish(event, source="emotion", **payload)
        except Exception:
            pass

    # ── 开关与门控 ──────────────────────────────────────────
    def _enabled(self) -> bool:
        """模块独立总开关 + 插件总开关。"""
        cfg = getattr(self._plugin.config, "emotion", None)
        plugin_section = getattr(self._plugin.config, "plugin", None)
        return bool(getattr(cfg, "enabled", True)) and bool(getattr(plugin_section, "enabled", True))

    def set_persona(self, lover_name: str, personality: str) -> None:
        self._lover_name = lover_name or self._lover_name
        self._persona = personality or self._persona

    # ── 持久化 ──────────────────────────────────────────────
    def _save_state(self) -> None:
        save_json_atomic(self._state_path, self._machine.to_dict())

    def _save_params(self) -> None:
        save_json_atomic(self._config_path, {"params": self._machine.params.to_dict(),
                                             "saved_at": datetime.now().isoformat(timespec="seconds")})

    # ── 情绪查询（注入 / Tool 用）────────────────────────────
    def mood_text(self) -> str:
        """当前情绪底色一句话（标签 + 低分辨率描述）。"""
        return mood_summary(self._machine.effective_vad())

    def injection_text(self) -> str:
        """情绪底色注入文本（planner/replyer 双通道共用，以系统条目插在第一条系统提示词后）。"""
        return (
            f"【当前情绪底色】{self.mood_text()}。\n"
            "要求：语气、措辞、节奏与该情绪保持一致；情绪会随时间自然衰减、随事件变化；"
            "严禁在回复中直接说出情绪标签词汇或任何数值；不要主动提及或解释本提示本身。"
        )

    def params_summary(self) -> str:
        """/cateye_test_emotion_config 查看文本。"""
        p = self._machine.params
        eff = self._machine.effective_vad()
        labels, _ = map_emotions(eff)
        label_text = "、".join(f"{n}({s:.0%})" for n, s in labels[:3]) or "复杂难名"
        lines = [
            f"初始化：{'已完成' if p.initialized else '未初始化（零锚点零偏移）'}",
            f"锚点：V={p.anchor.valence:+.2f}，A={p.anchor.arousal:+.2f}，D={p.anchor.dominance:+.2f}",
            f"静态偏移：V={p.offset.valence:+.2f}，A={p.offset.arousal:+.2f}，D={p.offset.dominance:+.2f}",
            f"乘数：正向增益 ×{p.positive_gain:.2f}，负向衰减 ×{p.negative_decay:.2f}",
            f"当前 VAD（含偏移）：V={eff.valence:+.2f}，A={eff.arousal:+.2f}，D={eff.dominance:+.2f}｜情绪底色：{label_text}",
            f"当天情绪事件：{len(self._machine.emotion_log)} 条",
        ]
        if p.reasons:
            for key in ("anchor", "offset", "multiplier"):
                if p.reasons.get(key):
                    lines.append(f"推导理由·{key}：{p.reasons[key]}")
        return "\n".join(lines)

    # ── 事件增量（reply Hook 调用）───────────────────────────
    async def evaluate_reply(self, reply_text: str) -> None:
        """评估一轮回复的情绪增量并记录（节流 + 静默失败）。"""
        if not self._enabled():
            return
        cfg = getattr(self._plugin.config, "emotion", None)
        min_interval = int(getattr(cfg, "evaluate_min_interval_seconds", 60) or 0)
        if min_interval and (time.monotonic() - self._last_evaluate_at) < min_interval:
            return
        self._last_evaluate_at = time.monotonic()
        task_name = str(getattr(cfg, "evaluator_task_name", "utils") or "utils")
        try:
            result = await evaluate_reply_delta(self._llm(), reply_text, self._persona,
                                                self._lover_name, task_name)
        except Exception:
            return
        if not result:
            return
        event, delta = result
        self._machine.record_event(event or "日常对话", delta)
        self._save_state()
        # M0 事件：VAD 结算后发低分辨率 VAD（附录 A ★，consumer=调试日志）
        await self._publish("emotion.updated", mood=self.mood_text(), trigger="reply_evaluated")

    def _llm(self) -> Any:
        llm = getattr(self._plugin, "_llm", None)
        if llm is None:  # 服务独立使用时的兜底
            from core.llm_utils import LLMService
            llm = LLMService(self._ctx)
        return llm

    # ── 睡眠检测与大修 ───────────────────────────────────────
    def _schedule_query(self) -> Optional[ScheduleQuery]:
        return getattr(self._plugin, "_schedule_query", None)

    def _persona_text(self) -> str:
        parts = [f"名称：{self._lover_name}"]
        if self._persona:
            parts.append(f"性格与人设：{self._persona[:800]}")
        custom = str(getattr(getattr(self._plugin.config, "schedule", None), "custom_prompt", "") or "")
        if custom:
            parts.append(f"当前生活阶段：{custom[:300]}")
        return "\n".join(parts)

    def _long_term_status(self) -> str:
        return str(getattr(getattr(self._plugin.config, "schedule", None), "custom_prompt", "") or "普通时期")

    @staticmethod
    def _day_summary(query: Optional[ScheduleQuery], date: str) -> str:
        if query is None:
            return "（没有记录）"
        try:
            return query.format_day_summary(date)
        except Exception:
            return "（没有记录）"

    @staticmethod
    def _sleep_hours(item: Dict[str, Any]) -> float:
        """从 sleep 条目的 start/end 分钟算睡眠时长（跨午夜自动 +1440）。"""
        start = int(item.get("start_minute") or 0)
        end = int(item.get("end_minute") or 0)
        if end <= start:
            end += 24 * 60
        return max(0.5, (end - start) / 60.0)

    async def run_sleep_overhaul(self) -> Dict[str, Any]:
        """执行一次睡眠期情绪大修（裁判 LLM → 锚点收敛 → 清空日志）。"""
        cfg = getattr(self._plugin.config, "emotion", None)
        query = self._schedule_query()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")

        # 当前活动必须是睡眠条目，且今天尚未大修过（sleep 需 include_sleep 才可见）
        current = query.find_current_activity(today, include_sleep=True) if query else None
        if current is None or str(current.get("goal_type") or "") != "sleep":
            return {"ok": False, "reason": "当前无睡眠日程条目"}
        sleep_hours = self._sleep_hours(current)

        referee_task = str(getattr(cfg, "referee_task_name", "replyer") or "replyer")
        vad = self._machine.effective_vad()  # 裁判接收已应用偏移的值（§九.3）
        result = await ask_sleep_referee(
            self._llm(),
            persona=self._persona_text(), vad=vad,
            emotion_log_text=self._machine.emotion_log_text(),
            yesterday_schedule=self._day_summary(query, (now - timedelta(days=1)).strftime("%Y-%m-%d")),
            tomorrow_schedule=self._day_summary(query, (now + timedelta(days=1)).strftime("%Y-%m-%d")),
            long_term_status=self._long_term_status(),
            sleep_hours=sleep_hours, task_name=referee_task,
        )
        if result is None:
            return {"ok": False, "reason": "睡眠裁判请求/解析失败（下一轮重试）"}
        delta, reasons = result
        applied = self._machine.apply_sleep_overhaul(delta, sleep_hours)
        applied["reasons"] = reasons
        applied["ok"] = True
        self._save_state()
        # M0 事件：醒后 VAD + 恢复系数（附录 A ★，consumer=planning/qzone）；
        # 同步发一次 emotion.updated（低分辨率 VAD），保持「VAD 结算即有事件流」。
        mood = self.mood_text()
        await self._publish("emotion.overhauled", mood=mood,
                            recovery_factor=applied.get("recovery_factor", 0.0),
                            sleep_hours=applied.get("sleep_hours", 0.0))
        await self._publish("emotion.updated", mood=mood, trigger="sleep_overhaul")
        return applied

    async def _loop(self) -> None:
        """睡眠检测循环：进入日程睡眠时段且当天未大修 → 触发裁判大修。"""
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                if self._enabled():
                    await self._check_and_overhaul()
            except Exception:
                pass
            cfg = getattr(self._plugin.config, "emotion", None)
            interval = max(1, int(getattr(cfg, "check_interval_minutes", 10) or 10)) * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _check_and_overhaul(self) -> None:
        query = self._schedule_query()
        if query is None:
            return
        now = datetime.now()
        current = query.find_current_activity(now.strftime("%Y-%m-%d"), now, include_sleep=True)
        if current is None or str(current.get("goal_type") or "") != "sleep":
            await self._publish_woke_up_if_needed(now)  # M0：离开睡眠条目 → schedule.woke_up
            return
        await self._publish_sleep_started_if_needed(current, now)  # M0：进入睡眠 → sleep_started
        state = load_json(self._state_path, {}) or {}
        if state.get("applied_sleep_date") == now.strftime("%Y-%m-%d"):
            return
        outcome = await self.run_sleep_overhaul()
        if outcome.get("ok"):
            state = load_json(self._state_path, {}) or {}
            state["applied_sleep_date"] = now.strftime("%Y-%m-%d")
            save_json_atomic(self._state_path, state)
            self._ctx.logger.info(
                "睡眠情绪大修完成：恢复系数 %.2f（睡 %.1f 小时），ΔVAD 已应用",
                outcome.get("recovery_factor", 0.0), outcome.get("sleep_hours", 0.0),
            )
        else:
            self._ctx.logger.debug("睡眠情绪大修未执行：%s", outcome.get("reason"))

    # ── 睡眠进出事件（M0：只发事件，不做冻结/结算——那是 M5）──
    async def _publish_sleep_started_if_needed(self, item: Dict[str, Any], now: datetime) -> None:
        """进入睡眠条目的上升沿 → schedule.sleep_started（payload: sleep_hours）。"""
        if self._in_sleep:
            return
        self._in_sleep = True
        self._sleep_started_at = now
        self._sleep_planned_hours = self._sleep_hours(item)
        await self._publish("schedule.sleep_started",
                            sleep_hours=round(self._sleep_planned_hours, 2),
                            date=now.strftime("%Y-%m-%d"))

    async def _publish_woke_up_if_needed(self, now: datetime) -> None:
        """离开睡眠条目的下降沿 → schedule.woke_up（sleep_hours 醒后补实际观测时长）。"""
        if not self._in_sleep:
            return
        started = self._sleep_started_at
        self._in_sleep = False
        self._sleep_started_at = None
        elapsed = (now - started).total_seconds() / 3600.0 if started else self._sleep_planned_hours
        await self._publish("schedule.woke_up",
                            sleep_hours=round(max(0.0, elapsed), 2),
                            date=now.strftime("%Y-%m-%d"))

    # ── 初始化（人设推导）────────────────────────────────────
    async def initialize_from_persona(self) -> str:
        """/cateye_test_emotion_init 主体：人设 → LLM 推导 → 校验落盘 → 生效。"""
        cfg = getattr(self._plugin.config, "emotion", None)
        task_name = str(getattr(cfg, "evaluator_task_name", "utils") or "utils")
        params = await derive_params_from_persona(self._llm(), self._persona_text(), task_name)
        if params is None:
            return "情绪初始化失败：模型未返回有效 JSON。请重试一次；若持续失败请检查模型配置。"
        self._machine.apply_params(params)
        self._save_params()
        self._save_state()
        lines = [
            "✅ 情绪系统初始化完成（参数已落盘，重启后依然生效）：",
            self.params_summary(),
        ]
        if params.reasons.get("anchor"):
            lines.append(f"推导理由·锚点：{params.reasons['anchor']}")
        return "\n".join(lines)

    def reset_params(self) -> str:
        """恢复默认（清空自定义锚点/偏移/乘数）。"""
        self._machine.reset_params()
        self._save_params()
        self._save_state()
        return "✅ 已恢复默认情绪参数（零锚点、零偏移、中性乘数），当前 VAD 归零。"

    def clear_log(self) -> str:
        """清空当天情绪日志（隐私接口，设计文档 §十三）。"""
        self._machine.emotion_log = []
        self._save_state()
        return "✅ 已清空当天情绪日志。"

    # ── 生命周期 ────────────────────────────────────────────
    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        for task in self._bg_tasks:
            task.cancel()
        for task in self._bg_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_tasks.clear()
        self._save_state()
