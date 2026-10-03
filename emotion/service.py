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
from .vad import VADState, _clip, map_emotions, mood_summary

#: 单事件增量硬限幅（§6.3-2：所有增量在代码层硬限幅；§6.2 A「单事件经 ±0.5 限幅」）
EVENT_DELTA_CLAMP = 0.5
#: 用户侧最后一条消息的反查窗口（分钟）与拉取条数（§4.2 评估上下文增强）
USER_MESSAGE_LOOKBACK_MINUTES = 20.0
USER_MESSAGE_LOOKBACK_LIMIT = 10
#: 调用方传入的 K 的防御性上限（target K = 1+step×(档+1) ≤ 6，留余量）
EXTERNAL_K_MAX = 10.0
#: mood_effect 数值夹取范围（附录 C：全部 clamp [0.7, 1.3]；post_rate 允许 0~1.2）
MOOD_EFFECT_MIN = 0.7
MOOD_EFFECT_MAX = 1.3
MOOD_POST_RATE_MAX = 1.2


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

    def effective_valence(self) -> float:
        """当前生效 V（含静态偏移/乘数结算；B2 情绪调制好感的输入，§6.2 B）。

        只给内部跨模块读数用，绝不出现在任何 LLM 提示词里（§6.3-6 LLM 隔离）。
        """
        return float(self._machine.effective_vad().valence)

    def injection_text(self) -> str:
        """情绪底色注入文本（planner/replyer 双通道共用，以系统条目插在第一条系统提示词后）。"""
        return (
            f"【当前情绪底色】{self.mood_text()}。\n"
            "要求：语气、措辞、节奏与该情绪保持一致；情绪会随时间自然衰减、随事件变化；"
            "严禁在回复中直接说出情绪标签词汇或任何数值；不要主动提及或解释本提示本身。"
        )

    # ── 情绪 → 行为结构化系数（M4；§4.2 行 5 / §6.2 C/D，附录 C）──────
    def mood_effect(self) -> Dict[str, Any]:
        """当前情绪 → 行为系数（纯规则、零 LLM 成本；附录 C 默认映射）。

        四个消费字段（消费方：patrol 概率乘数 / miss 阈值 / qzone 发帖门控与文案）：

        - ``proactive_rate``：patrol 主动概率乘数（× activity_trigger_rate/default_speak_rate）；
        - ``miss_threshold``：想念随机阈值乘数（再 × affection 档位系数，M4 双系数）；
        - ``post_rate``：qzone 发帖率乘数（0 = 低落时不硬营业；0~1.2）；
        - ``intent_hint``：低分辨成语义行（拼进 proactive intent 的 reason）；中性为空串。

        映射（附录 C，逐条）：proactive V>0.3&A>0.2→1.2、V<−0.3→0.8、否则 1.0；
        miss V<−0.3→0.8、V>0.3→1.1、否则 1.0；post V<−0.6→0、V<−0.3→0.8、V>0.3→1.2、否则 1.0；
        数值一律 clamp [0.7, 1.3]（post_rate 允许 0~1.2）。模块停用（emotion.enabled=false /
        plugin.enabled=false）时返回恒等中性值，消费方无需再判开关（§八.2 可空降级）。
        """
        if not self._enabled():
            return {"proactive_rate": 1.0, "miss_threshold": 1.0, "post_rate": 1.0, "intent_hint": ""}
        eff = self._machine.effective_vad()
        valence = float(eff.valence)
        arousal = float(eff.arousal)

        # 主动开口：心情好且精神足 → 更主动；低落 → 收敛
        if valence > 0.3 and arousal > 0.2:
            proactive_rate = 1.2
        elif valence < -0.3:
            proactive_rate = 0.8
        else:
            proactive_rate = 1.0

        # 想念阈值：低落 → 更快想念重要的人；兴奋 → 不急着打扰（阈值放宽）
        if valence < -0.3:
            miss_threshold = 0.8
        elif valence > 0.3:
            miss_threshold = 1.1
        else:
            miss_threshold = 1.0

        # 发帖：很低落 → 不硬营业；低落 → 减少；兴奋 → 分享欲上升
        if valence < -0.6:
            post_rate = 0.0
        elif valence < -0.3:
            post_rate = 0.8
        elif valence > 0.3:
            post_rate = 1.2
        else:
            post_rate = 1.0

        # 低分辨成语义行：只给语气方向、不含任何数值；中性为空串
        # （V<−0.3 的文案为规格给定；V>0.3 为对称补充——中性/正向不再是空白）
        if valence < -0.3:
            intent_hint = "你今天有点低落，语气可以软一点"
        elif valence > 0.3:
            intent_hint = "你今天心情不错，语气可以轻快一点"
        else:
            intent_hint = ""

        return {
            "proactive_rate": _clip(proactive_rate, MOOD_EFFECT_MIN, MOOD_EFFECT_MAX),
            "miss_threshold": _clip(miss_threshold, MOOD_EFFECT_MIN, MOOD_EFFECT_MAX),
            "post_rate": _clip(post_rate, 0.0, MOOD_POST_RATE_MAX),
            "intent_hint": intent_hint,
        }

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
    def _llm_interval_ok(self) -> bool:
        """评估节流（与 evaluate_reply / record_external_event 共用同一时间戳）。

        §4.2 行 1「全程既有节流不变」：LLM 路径一律先过 ``evaluate_min_interval_seconds``；
        模板事件零 LLM 成本，不走节流（其频控在发布方，如沉默事件的每日 ≤2 次）。
        """
        cfg = getattr(self._plugin.config, "emotion", None)
        min_interval = int(getattr(cfg, "evaluate_min_interval_seconds", 60) or 0)
        if min_interval and (time.monotonic() - self._last_evaluate_at) < min_interval:
            return False
        self._last_evaluate_at = time.monotonic()
        return True

    async def _last_user_message_text(self, session_id: str) -> str:
        """反查该聊天流最近一条用户消息文本（§4.2 行 2；拿不到就跳过，绝不阻塞评估）。

        ``ctx.message.get_by_time_in_chat(chat_id, start_time, end_time, filter_mai=True)``
        返回 list（兼容 dict 形态旧代码，陷阱 #12；SDK 归一化见开发文档 §8.5）；
        filter_mai=True 已过滤 bot 自身消息，这里只取最后一条有文本的。
        """
        sid = str(session_id or "").strip()
        message_api = getattr(self._ctx, "message", None)
        getter = getattr(message_api, "get_by_time_in_chat", None)
        if not sid or not callable(getter):
            return ""
        end = time.time()
        start = end - USER_MESSAGE_LOOKBACK_MINUTES * 60.0
        try:
            result = await getter(chat_id=sid, start_time=start, end_time=end,
                                  limit=USER_MESSAGE_LOOKBACK_LIMIT, filter_mai=True)
        except Exception:
            return ""
        if isinstance(result, dict):  # 兼容 dict 形态旧代码（messages 键）
            result = result.get("messages") or []
        if not isinstance(result, list):
            return ""
        for item in reversed(result):
            if not isinstance(item, dict):
                continue
            text = str(item.get("processed_plain_text") or "").strip()
            if text:
                return text[:300]
        return ""

    async def evaluate_reply(self, reply_text: str, session_id: str = "") -> None:
        """评估一轮回复的情绪增量并记录（节流 + 静默失败）。

        M3（§4.2 行 2 / §7 M3 行）：prompt 追加"用户侧最后一条消息"（经
        ``ctx.message.get_by_time_in_chat`` 反查该流最近消息，拿不到就跳过）；
        target 识别走 replyer.after_response 的 ``session_id`` 链路（== 目标私聊
        stream_id → K=1+档位加成，其余按非 target），加算倍率与 bridge 同口径
        （§6.2 A：final = delta × global_event_multiplier × K）。
        """
        if not self._enabled():
            return
        if not self._llm_interval_ok():
            return
        cfg = getattr(self._plugin.config, "emotion", None)
        task_name = str(getattr(cfg, "evaluator_task_name", "utils") or "utils")
        user_message = await self._last_user_message_text(session_id)
        try:
            result = await evaluate_reply_delta(self._llm(), reply_text, self._persona,
                                                self._lover_name, task_name,
                                                user_message=user_message)
        except Exception:
            return
        if not result:
            return
        event, delta = result
        is_target = self.is_target_session(session_id)
        self._apply_external_delta(event or "日常对话", delta, source="replyer",
                                   multiplier=self.target_multiplier(is_target),
                                   is_target=is_target, session_id=session_id, llm=True)
        # M0 事件：VAD 结算后发低分辨率 VAD（附录 A ★，consumer=调试日志）
        await self._publish("emotion.updated", mood=self.mood_text(), trigger="reply_evaluated")

    # ── M3：外部事件入口 + target 加算倍率（§4.2 行 1 / §6.2 A）──────
    def _emotion_cfg(self, name: str, default: Any) -> Any:
        """运行期动态读 [emotion] 配置项（热重载即时生效；缺省回退默认值）。"""
        cfg = getattr(self._plugin.config, "emotion", None)
        value = getattr(cfg, name, default)
        return default if value is None else value

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def global_event_multiplier(self) -> float:
        """全局事件幅度旋钮（默认 1.0，夹在 [0.1, 3.0]）。"""
        value = self._to_float(self._emotion_cfg("global_event_multiplier", 1.0), 1.0)
        return max(0.1, min(3.0, value))

    def non_target_multiplier(self) -> float:
        """非 target 事件系数（默认 1.0，夹在 [0, 1]）。"""
        value = self._to_float(self._emotion_cfg("non_target_multiplier", 1.0), 1.0)
        return max(0.0, min(1.0, value))

    def target_bonus_step(self) -> float:
        """target 每档加成步长（默认 0.1，夹在 [0, 1]；附录 C）。"""
        value = self._to_float(self._emotion_cfg("target_bonus_step", 0.1), 0.1)
        return max(0.0, min(1.0, value))

    def affection_tier(self) -> Optional[int]:
        """从 affection 状态层读当前档位（跨模块 getattr 判空；读不到返回 None）。"""
        affection = getattr(self._plugin, "_affection", None)
        getter = getattr(affection, "tier", None)
        if not callable(getter):
            return None
        try:
            return max(0, min(4, int(getter())))
        except Exception:
            return None

    def target_bonus(self) -> float:
        """target 加算加成 = step × (档位+1)（§5.3 表 / §6.2 A；档位不可得时 0）。"""
        tier = self.affection_tier()
        if tier is None:
            return 0.0
        return self.target_bonus_step() * (tier + 1)

    def target_multiplier(self, is_target: bool) -> float:
        """§6.2 A 的 K：target = 1+加成（加算非乘算）；非 target = non_target_multiplier。"""
        if is_target:
            return 1.0 + self.target_bonus()
        return self.non_target_multiplier()

    def is_target_session(self, session_id: str) -> bool:
        """私聊 target 判定（§5.3）：session_id == 目标私聊 stream_id（after_response 链路）。

        stream_id 由顶层 ``_resolve_target_stream`` 从 ``target.target_qq`` 解析并
        随配置热重载更新（plugin._apply_target）；取不到时按非 target 计。
        """
        sid = str(session_id or "").strip()
        target = str(getattr(self._plugin, "_stream_id", "") or "").strip()
        return bool(sid and target and sid == target)

    def _log_decision(self, action: str, **fields: Any) -> None:
        """写一条决策日志（无 logger 时静默；日志故障绝不影响情绪结算）。"""
        logger_ = getattr(self._plugin, "_decision_logger", None)
        if logger_ is None:
            return
        try:
            logger_.record(action=action, **fields)
        except Exception:
            pass

    def _apply_external_delta(self, event_text: str, delta: VADState, *, source: str,
                              multiplier: float, llm: bool = False,
                              is_target: Optional[bool] = None,
                              session_id: str = "") -> Dict[str, Any]:
        """倍率 → ±0.5 硬限幅 → 入账 → 决策日志（evaluate_reply 与外部事件共用）。

        三层倍率与裁剪前/后值全部落 decision_logs（§6.3-7）：``k``（调用方 K）/
        ``global_multiplier`` / ``multiplier``（最终总倍率）+ ``delta_before`` /
        ``delta_scaled`` / ``delta_applied``。
        """
        global_mul = self.global_event_multiplier()
        factor = float(multiplier) * global_mul
        scaled = VADState(delta.valence * factor, delta.arousal * factor, delta.dominance * factor)
        applied = VADState(
            _clip(scaled.valence, -EVENT_DELTA_CLAMP, EVENT_DELTA_CLAMP),
            _clip(scaled.arousal, -EVENT_DELTA_CLAMP, EVENT_DELTA_CLAMP),
            _clip(scaled.dominance, -EVENT_DELTA_CLAMP, EVENT_DELTA_CLAMP),
        )
        clipped = any(abs(getattr(scaled, axis)) > EVENT_DELTA_CLAMP + 1e-9
                      for axis in ("valence", "arousal", "dominance"))
        self._machine.record_event(event_text or "外部事件", applied)
        self._save_state()
        self._log_decision(
            "emotion.external_event", trigger_type="emotion", reason=str(event_text or "")[:120],
            source=str(source or ""), session_id=str(session_id or ""),
            is_target=(bool(is_target) if is_target is not None else None), tier=self.affection_tier(),
            target_bonus=round(self.target_bonus(), 4),
            non_target_multiplier=round(self.non_target_multiplier(), 4),
            k=round(float(multiplier), 4), global_multiplier=round(global_mul, 4),
            multiplier=round(factor, 4), llm=bool(llm), clipped=bool(clipped),
            delta_before=delta.to_dict(), delta_scaled=scaled.to_dict(), delta_applied=applied.to_dict(),
        )
        return {"ok": True, "reason": "", "event": str(event_text or ""), "delta": applied,
                "delta_before": delta, "multiplier": round(factor, 4),
                "k": round(float(multiplier), 4), "global_multiplier": round(global_mul, 4),
                "clipped": bool(clipped)}

    async def record_external_event(self, source: str, description: str,
                                    delta: Optional[VADState] = None, *,
                                    use_llm: bool = False,
                                    multiplier: float = 1.0) -> Dict[str, Any]:
        """统一外部事件入口（§4.2 行 1 / §6.2 A）。

        - ``delta`` 给出模板增量 → 直接 ``record_event``（×multiplier）；
        - ``delta`` 为 None 且 ``use_llm=True`` → 走既有 :func:`evaluate_reply_delta`
          有界评估（±0.5；裁判/评估看不到绝对 VAD，§6.3-6；与回复评估共用节流）；
        - ``multiplier`` 是调用方按 §6.2 A 算好的 K（target = 1+档位加成 /
          非 target = non_target_multiplier）；入口再统一乘 ``global_event_multiplier``，
          即 ``final_delta = delta × global_event_multiplier × K``；
        - 最终增量经 ±:data:`EVENT_DELTA_CLAMP` 硬限幅后入账，三层倍率与裁剪前/后
          值落 decision_logs（§6.3-7）。

        Returns:
            ``{"ok", "reason", "event", "delta", "delta_before", "multiplier",
            "k", "global_multiplier", "clipped"}``；未入账时 ok=False 且 reason 说明。
        """
        if not self._enabled():
            return {"ok": False, "reason": "disabled"}
        event_text = str(description or "").strip()
        k = max(0.0, min(EXTERNAL_K_MAX, self._to_float(multiplier, 1.0)))
        delta_before: Optional[VADState] = None
        llm_used = False
        if delta is not None:
            delta_before = VADState(delta.valence, delta.arousal, delta.dominance).clipped()
        elif use_llm:
            if not event_text:
                return {"ok": False, "reason": "empty_event"}
            if not self._llm_interval_ok():
                return {"ok": False, "reason": "throttled"}
            cfg = getattr(self._plugin.config, "emotion", None)
            task_name = str(getattr(cfg, "evaluator_task_name", "utils") or "utils")
            try:
                result = await evaluate_reply_delta(self._llm(), event_text, self._persona,
                                                    self._lover_name, task_name)
            except Exception:
                return {"ok": False, "reason": "llm_error"}
            if not result:
                return {"ok": False, "reason": "llm_failed"}
            llm_event, delta_before = result
            event_text = str(llm_event or "").strip()[:60] or event_text
            llm_used = True
        else:
            return {"ok": False, "reason": "no_delta"}
        outcome = self._apply_external_delta(event_text, delta_before, source=source,
                                             multiplier=k, llm=llm_used)
        if outcome.get("ok"):
            # M0 事件：VAD 结算后发低分辨率 VAD（附录 A ★，consumer=调试日志）
            await self._publish("emotion.updated", mood=self.mood_text(), trigger="external_event")
        return outcome

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
