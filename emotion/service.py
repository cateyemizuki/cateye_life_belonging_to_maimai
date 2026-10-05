"""情绪系统服务：状态持久化 / 睡眠检测循环 / 人设初始化 / 情绪注入文本。"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.storage import load_json, save_json_atomic
from core.time_utils import parse_hhmm
from planning.schedule_query import ScheduleQuery

from .referee import ask_sleep_referee, derive_params_from_persona, evaluate_reply_delta
from .state_machine import (DAILY_V_KEEP_DAYS, LOW_MOOD_MIN_DAYS, SCHEDULE_TONE_CLAMP,
                            EmotionParams, EmotionStateMachine)
from .vad import VADState, _clip, _level, map_emotions, mood_summary

#: 单事件增量硬限幅（§6.3-2：所有增量在代码层硬限幅；§6.2 A「单事件经 ±0.5 限幅」）
EVENT_DELTA_CLAMP = 0.5
#: 用户侧最后一条消息的反查窗口（分钟）与拉取条数（§4.2 评估上下文增强）
USER_MESSAGE_LOOKBACK_MINUTES = 20.0
USER_MESSAGE_LOOKBACK_LIMIT = 10
#: 调用方传入的 K 的防御性上限（target K = 1+step×(档+1) ≤ 6，留余量）
EXTERNAL_K_MAX = 10.0
#: 跨来源去重（§2.4.6 A3）：自动回复评估通道 ⇄ 群聊工具上报通道，同一聊天流 + 时间窗内只计一次
CROSS_SOURCE_DEDUP_SOURCES = ("replyer", "tool:emotion_report")
#: 跨来源去重时间窗缺省（秒；配置 ``emotion.dedup_window_seconds`` 可调，0 = 关闭窗口判定）
DEDUP_WINDOW_DEFAULT_SECONDS = 180
#: 去重登记表容量上限（防无界增长；纯内存态，热重载随进程重置——只影响重载后窗口内的判重机会）
_DEDUP_REGISTRY_MAX = 64
#: mood_effect 数值夹取范围（附录 C：全部 clamp [0.7, 1.3]；post_rate 允许 0~1.2）
MOOD_EFFECT_MIN = 0.7
MOOD_EFFECT_MAX = 1.3
MOOD_POST_RATE_MAX = 1.2
#: 睡眠条目名称关键词（M5 修复：日程生成 prompt 让"睡觉"条目用 goal_type=daily_routine，
#: 仅按 goal_type=="sleep" 会漏判；候选见 prompt_builder「睡觉活动」段与无睡眠模式词表）
SLEEP_NAME_KEYWORDS = ("睡觉", "睡眠", "安睡", "午休", "午睡", "小憩", "打盹")
#: 作息锚点缺省（配置缺失时按 23:00 入睡 / 07:00 起床）
DEFAULT_SLEEP_MINUTES = 23 * 60
DEFAULT_WAKE_MINUTES = 7 * 60


def _event_fingerprint(text: str) -> str:
    """事件指纹（审计留痕用）：去空白、小写、截断 80 字；不参与跨来源重复判定（口径见 dedup_policy）。"""
    return "".join(str(text or "").split()).lower()[:80]


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
        #: 跨来源去重登记：stream → (最近一次真正入账的 source, monotonic 时间, 事件指纹)（§2.4.6 A3）
        self._cross_source_last: Dict[str, Tuple[str, float, str]] = {}
        self._stop_event: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._bg_tasks: list[asyncio.Task] = []
        # M0 事件：睡眠进出观测（schedule.sleep_started / woke_up 只在进出沿各发一次）
        self._in_sleep = False
        self._sleep_started_at: Optional[datetime] = None
        self._sleep_planned_hours = 0.0
        # M5 睡眠大修缓存（§4.2）：冻结期计算并缓存，醒后一次性应用；运行期状态、不落盘
        self._pending_overhaul: Optional[Dict[str, Any]] = None
        #: M5 睡眠周期窗口 (start, end)：按作息锚点整夜（条目会被截断，不能作窗口）
        self._sleep_period: Optional[Tuple[datetime, datetime]] = None
        #: M5 连续低迷达标状态（streak ≥ 门槛时的事件载荷；供 planning 强制插入恢复类活动）
        self._low_mood_day: Dict[str, Any] = {}

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
        （§6.2 A：final = delta × global_event_multiplier × K）。跨来源去重（§2.4.6 A3）
        在评估 LLM 之前先判：命中则本次不调 LLM、不入账，仅落一条去重决策日志。
        """
        if not self._enabled():
            return
        if self._dedup_skip_result("replyer", session_id, reply_text) is not None:
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

    # ── 跨来源去重（§2.4.6 A3：自动评估 ⇄ 工具上报，同一流+窗口只计一次）──
    def dedup_policy(self) -> Dict[str, Any]:
        """跨来源去重口径（写进 record_external_event 返回值；§2.4.6 A3）。

        ``policy=cross_source_stream_window``：仅 ``replyer``（自动回复评估主通道）与
        ``tool:emotion_report``（群聊工具上报）两条来源互斥；同一聊天流（session_id /
        stream_id 归一口径，空流归入 ``-``）内、时间窗（``emotion.dedup_window_seconds``，
        默认 180s）内**先入账者生效**——真实时序上工具上报在回复生成前、自动评估在回复后，
        因此群聊 target 事件通常由工具侧的 K（含档位加成）计入、自动评估被去重；后到者判
        ``duplicate_cross_source`` 不入账（不发 emotion.updated、不写 emotion_log）；
        窗口相对"最近一次真正入账"滚动，被去重的调用不刷新窗口；模板事件（bridge:* 等）
        与其它来源不参与（频控/语义各异）。事件指纹仅作审计留痕，不参与匹配判定。
        """
        return {
            "policy": "cross_source_stream_window",
            "sources": list(CROSS_SOURCE_DEDUP_SOURCES),
            "enabled": self._dedup_enabled(),
            "window_seconds": round(self._dedup_window(), 3),
        }

    def _dedup_enabled(self) -> bool:
        return bool(self._emotion_cfg("dedup_enabled", True))

    def _dedup_window(self) -> float:
        raw = self._emotion_cfg("dedup_window_seconds", DEDUP_WINDOW_DEFAULT_SECONDS)
        try:
            window = float(raw)
        except (TypeError, ValueError):
            window = float(DEDUP_WINDOW_DEFAULT_SECONDS)
        return max(0.0, min(3600.0, window))

    @staticmethod
    def _dedup_stream_key(session_id: str) -> str:
        return str(session_id or "").strip() or "-"

    def _dedup_duplicate(self, source: str, session_id: str,
                         event_text: str) -> Optional[Dict[str, Any]]:
        """判定本次入账是否与最近一次跨来源入账重复；命中返回去重信息（调用方不入账）。"""
        normalized = str(source or "").strip()
        if normalized not in CROSS_SOURCE_DEDUP_SOURCES or not self._dedup_enabled():
            return None
        window = self._dedup_window()
        if window <= 0:
            return None
        key = self._dedup_stream_key(session_id)
        last = self._cross_source_last.get(key)
        if last is None:
            return None
        last_source, last_ts, last_fingerprint = last
        if last_source == normalized or last_source not in CROSS_SOURCE_DEDUP_SOURCES:
            return None
        elapsed = max(0.0, time.monotonic() - last_ts)
        if elapsed > window:
            return None
        return {
            "duplicate": True,
            "first_source": last_source,
            "first_fingerprint": last_fingerprint,
            "fingerprint": _event_fingerprint(event_text),
            "stream": key,
            "elapsed_seconds": round(elapsed, 3),
            **self.dedup_policy(),
        }

    def _dedup_note(self, source: str, session_id: str, event_text: str) -> None:
        """登记一次真正入账的跨来源事件（窗口相对"最近一次真正入账"滚动）。"""
        normalized = str(source or "").strip()
        if normalized not in CROSS_SOURCE_DEDUP_SOURCES:
            return
        self._cross_source_last[self._dedup_stream_key(session_id)] = (
            normalized, time.monotonic(), _event_fingerprint(event_text))
        while len(self._cross_source_last) > _DEDUP_REGISTRY_MAX:
            self._cross_source_last.pop(next(iter(self._cross_source_last)), None)

    def _dedup_skip_result(self, source: str, session_id: str,
                           event_text: str) -> Optional[Dict[str, Any]]:
        """检查跨来源重复：命中则记决策日志并返回"未入账"结果，未命中返回 None。"""
        duplicate = self._dedup_duplicate(source, session_id, event_text)
        if duplicate is None:
            return None
        self._log_decision(
            "emotion.external_event_dedup", trigger_type="emotion",
            reason=f"跨来源重复上报：{duplicate['first_source']} → {str(source or '').strip()}",
            source=str(source or "").strip(), **duplicate,
        )
        return {"ok": False, "reason": "duplicate_cross_source", "dedup": duplicate}

    def _apply_external_delta(self, event_text: str, delta: VADState, *, source: str,
                              multiplier: float, llm: bool = False,
                              is_target: Optional[bool] = None,
                              session_id: str = "") -> Dict[str, Any]:
        """倍率 → ±0.5 硬限幅 → 入账 → 决策日志（evaluate_reply 与外部事件共用）。

        三层倍率与裁剪前/后值全部落 decision_logs（§6.3-7）：``k``（调用方 K）/
        ``global_multiplier`` / ``multiplier``（最终总倍率）+ ``delta_before`` /
        ``delta_scaled`` / ``delta_applied``。

        跨来源去重（§2.4.6 A3）在入账前判定：命中返回 ``duplicate_cross_source`` 且不写
        emotion_log / 不发 emotion.updated（口径见 :meth:`dedup_policy`）。
        """
        skip = self._dedup_skip_result(source, session_id, event_text)
        if skip is not None:
            return skip
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
        self._dedup_note(source, session_id, event_text)   # §2.4.6 A3：登记本次真正入账
        return {"ok": True, "reason": "", "event": str(event_text or ""), "delta": applied,
                "delta_before": delta, "multiplier": round(factor, 4),
                "k": round(float(multiplier), 4), "global_multiplier": round(global_mul, 4),
                "clipped": bool(clipped),
                "dedup": {**self.dedup_policy(), "duplicate": False}}

    async def record_external_event(self, source: str, description: str,
                                    delta: Optional[VADState] = None, *,
                                    use_llm: bool = False,
                                    multiplier: float = 1.0,
                                    session_id: str = "") -> Dict[str, Any]:
        """统一外部事件入口（§4.2 行 1 / §6.2 A）。

        - ``delta`` 给出模板增量 → 直接 ``record_event``（×multiplier）；
        - ``delta`` 为 None 且 ``use_llm=True`` → 走既有 :func:`evaluate_reply_delta`
          有界评估（±0.5；裁判/评估看不到绝对 VAD，§6.3-6；与回复评估共用节流）；
        - ``multiplier`` 是调用方按 §6.2 A 算好的 K（target = 1+档位加成 /
          非 target = non_target_multiplier）；入口再统一乘 ``global_event_multiplier``，
          即 ``final_delta = delta × global_event_multiplier × K``；
        - ``session_id``（宿主 ToolInvocation 的 stream_id / 聊天流）供跨来源去重
          （§2.4.6 A3）使用；缺省空串归入同一"未知流"；
        - 跨来源去重先于 LLM 评估判定：命中返回 ``{"ok": False,
          "reason": "duplicate_cross_source", "dedup": {...}}``，不调 LLM、不入账；
        - 最终增量经 ±:data:`EVENT_DELTA_CLAMP` 硬限幅后入账，三层倍率与裁剪前/后
          值落 decision_logs（§6.3-7）。

        Returns:
            ``{"ok", "reason", "event", "delta", "delta_before", "multiplier",
            "k", "global_multiplier", "clipped", "dedup"}``；未入账时 ok=False 且
            reason 说明（``disabled`` / ``empty_event`` / ``throttled`` / ``llm_error`` /
            ``llm_failed`` / ``duplicate_cross_source``）；``dedup`` 恒带上口径与
            ``duplicate`` 标记（口径见 :meth:`dedup_policy`）。
        """
        if not self._enabled():
            return {"ok": False, "reason": "disabled"}
        event_text = str(description or "").strip()
        skip = self._dedup_skip_result(source, session_id, description)
        if skip is not None:
            return skip
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
                                             multiplier=k, llm=llm_used, session_id=session_id)
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

    # ── M5 睡眠冻结 / 大修缓存（§4.2 睡眠结算改造）──────────────
    def is_frozen(self) -> bool:
        """当前是否处于睡眠冻结态（冻结期：事件只入队、衰减与评估结算暂停）。"""
        return bool(getattr(self._machine, "frozen", False))

    def sleep_delta_limit(self) -> float:
        """睡眠裁判增量动态限幅 = 0.4 + 0.4×``schedule.emotion_influence``（§4.2 睡眠大修 v2）。

        替代旧固定 ±0.8；``emotion_influence`` 夹在 [0, 1]（0=下限 ±0.4，1=上限 ±0.8），
        配置缺失/非法一律回退默认 0.6（→ ±0.64）。
        """
        influence = self._to_float(self._schedule_cfg("emotion_influence", 0.6), 0.6)
        influence = max(0.0, min(1.0, influence))
        return 0.4 + 0.4 * influence

    def _schedule_cfg(self, name: str, default: Any) -> Any:
        """运行期动态读 [schedule] 配置项（热重载即时生效；缺省回退默认值）。"""
        cfg = getattr(self._plugin.config, "schedule", None)
        value = getattr(cfg, name, default)
        return default if value is None else value

    @staticmethod
    def schedule_tone_sum(items: Any) -> VADState:
        """日程条目 ``emotional_tone`` 逐维求和（前日残差 R_yesterday / 明日预期 E_tomorrow）。

        §6.2 E：条目 tone 入库时已由 planning 侧裁剪 ±0.5，这里求和后再逐维夹
        ±:data:`SCHEDULE_TONE_CLAMP`（双保险，防多项求和发散）；非数值/缺失按 0 计，
        拿不到日程就退化为零向量（可空降级，§八.2）。
        """
        axes = ("valence", "arousal", "dominance")
        totals = [0.0, 0.0, 0.0]
        for item in items or []:
            if not isinstance(item, dict):
                continue
            params = item.get("parameters")
            tone = params.get("emotional_tone") if isinstance(params, dict) else None
            if not isinstance(tone, dict):
                continue
            for idx, axis in enumerate(axes):
                try:
                    totals[idx] += float(tone.get(axis) or 0.0)
                except (TypeError, ValueError):
                    continue
        return VADState(*(_clip(totals[i], -SCHEDULE_TONE_CLAMP, SCHEDULE_TONE_CLAMP)
                          for i in range(3)))

    def _day_items(self, query: Optional[ScheduleQuery], date: str) -> list:
        """读取某天日程条目（拿不到返回空列表）。"""
        if query is None:
            return []
        try:
            return query.get_day_schedule(date) or []
        except Exception:
            return []

    def _commitment_net_today(self) -> float:
        """当日约定净兑现次数（kept − broken）：按桥写入的描述前缀从当天日志统计（§4.2 第三输入）。"""
        from .bridge import COMMITMENT_BROKEN_PREFIX, COMMITMENT_KEPT_PREFIX

        kept = broken = 0
        for event in self._machine.emotion_log:
            desc = str(getattr(event, "event_description", "") or "")
            if desc.startswith(COMMITMENT_KEPT_PREFIX):
                kept += 1
            elif desc.startswith(COMMITMENT_BROKEN_PREFIX):
                broken += 1
        return float(kept - broken)

    def _current_sleep_item(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """当前活动若为睡眠条目则返回它（sleep 需 include_sleep 才可见）。"""
        query = self._schedule_query()
        now = now or datetime.now()
        current = query.find_current_activity(now.strftime("%Y-%m-%d"), now, include_sleep=True) \
            if query else None
        if not self._is_sleep_item(current):
            return None
        return current

    # ── 睡眠时段判定（M5 修复：条目会被 _normalize_items 的作息锚点截断）──
    @staticmethod
    def _is_sleep_item(item: Any) -> bool:
        """是否为睡眠条目（goal_type=="sleep"，或名称含睡觉类关键词）。

        ⚠️ 日程生成 prompt 明确让"睡觉"条目用 ``goal_type: daily_routine``（prompt_builder
        「睡觉活动」段），仅按 goal_type 判定会漏掉生产条目；模板兜底条目才用 "sleep"。
        """
        if not isinstance(item, dict):
            return False
        if str(item.get("goal_type") or "") == "sleep":
            return True
        name = str(item.get("name") or "")
        return any(keyword in name for keyword in SLEEP_NAME_KEYWORDS)

    def _sleep_anchors(self) -> Tuple[int, int]:
        """``(sleep_min, wake_min)`` 作息锚点（[schedule] 配置，缺省 23:00 / 07:00）。"""
        cfg = getattr(self._plugin.config, "schedule", None)
        sleep_time = str(getattr(cfg, "sleep_time", "") or "")
        wake_time = str(getattr(cfg, "wake_time", "") or "")
        return (parse_hhmm(sleep_time, DEFAULT_SLEEP_MINUTES),
                parse_hhmm(wake_time, DEFAULT_WAKE_MINUTES))

    @staticmethod
    def _minute_in_night(minute: int, sleep_min: int, wake_min: int) -> bool:
        if sleep_min > wake_min:  # 常规作息：当晚 → 次日 wake（跨午夜）
            return minute >= sleep_min or minute < wake_min
        return sleep_min <= minute < wake_min  # 夜猫子作息：同日 sleep → 同日 wake

    @staticmethod
    def _planned_sleep_hours(sleep_min: int, wake_min: int) -> float:
        """作息锚点推出的整夜计划时长（小时，≥0.5）。"""
        span = (1440 - sleep_min + wake_min) if sleep_min > wake_min else (wake_min - sleep_min)
        return max(0.5, span / 60.0)

    @staticmethod
    def _at_minute(day: datetime, minute: int) -> datetime:
        return day.replace(hour=(minute // 60) % 24, minute=minute % 60, second=0, microsecond=0)

    def _night_window(self, now: datetime) -> Tuple[datetime, datetime]:
        """当前所处/最近的作息夜间窗口 ``(start, end)``（按 [schedule] 锚点算整夜）。"""
        sleep_min, wake_min = self._sleep_anchors()
        cur = now.hour * 60 + now.minute
        start_day = now if cur >= sleep_min else now - timedelta(days=1)
        start = self._at_minute(start_day, sleep_min)
        end = self._at_minute(start + timedelta(days=1), wake_min) if sleep_min > wake_min \
            else self._at_minute(start, wake_min)
        return start, end

    @staticmethod
    def _cycle_key(period_start: datetime) -> str:
        """睡眠周期身份 = ``周期起始日@起始分钟``（同一周期只结算一次；旧裸日期值兼容读取）。"""
        return f"{period_start:%Y-%m-%d}@{period_start.hour * 60 + period_start.minute}"

    def _sleep_entry_period(self, item: Optional[Dict[str, Any]], now: datetime
                            ) -> Tuple[datetime, datetime, float]:
        """进入睡眠时的周期窗口与计划时长 ``(start, end, planned_hours)``。

        - 夜间睡眠（处于作息夜间窗口，或条目起点落在窗口内）→ 按**锚点整夜**：
          条目本身会被 ``_normalize_items`` 截断到入睡锚点（最长为 23:00–23:15），
          不能作为整夜窗口，否则入睡约 15 分钟即误判"醒来"；
        - 其它（午休/小睡）→ 按条目自身的 start/end 窗口。
        """
        sleep_min, wake_min = self._sleep_anchors()
        night_start, night_end = self._night_window(now)
        item_start = int((item or {}).get("start_minute") or sleep_min)
        if item is None or self._minute_in_night(item_start, sleep_min, wake_min) \
                or night_start <= now < night_end:
            return night_start, night_end, self._planned_sleep_hours(sleep_min, wake_min)
        span = max(15, int((item or {}).get("end_minute") or 0) - item_start)
        start = self._at_minute(now, item_start)
        return start, start + timedelta(minutes=span), max(0.5, span / 60.0)

    def _in_sleep_period(self, now: datetime) -> bool:
        """当前是否处于睡眠周期窗口（冻结中按已建立窗口；否则按夜间锚点窗口）。"""
        if self._sleep_period is not None:
            return self._sleep_period[0] <= now < self._sleep_period[1]
        night_start, night_end = self._night_window(now)
        return night_start <= now < night_end

    async def compute_sleep_overhaul(self, now: Optional[datetime] = None,
                                     sleep_hours: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """冻结期计算大修并返回缓存载荷（**不落状态**）；裁判失败返回 None（下一轮重试）。

        输入（§4.2 睡眠大修 v2）：当天日志 + 前日残差 R_yesterday + 明日预期 E_tomorrow
        （均来自日程条目 ``emotional_tone``）+ 当日约定净兑现 + 动态限幅。
        ``sleep_hours`` 缺省按条目时长；服务侧传作息锚点整夜时长（见 ``_sleep_entry_period``）。

        E_tomorrow 在入睡时刻按"次日"读取，而次日日程要到 ``auto_generate_time``（默认
        00:30）才生成——故这里只为裁判提供参考文本，**状态侧数值在醒来时会重算一次**
        （见 ``_apply_cached_overhaul``），保证"明日预期"在生产时序下确实生效。
        """
        cfg = getattr(self._plugin.config, "emotion", None)
        query = self._schedule_query()
        now = now or datetime.now()
        current = self._current_sleep_item(now)
        # 时长的来源优先级：调用方给的计划时长（作息锚点整夜） > 条目时长 > 锚点整夜兜底
        # （冻结期重试时可能已无当前条目——条目只有 15 分钟窗口，不能作为必要前提）
        if sleep_hours is not None:
            hours = float(sleep_hours)
        elif current is not None:
            hours = self._sleep_hours(current)
        else:
            hours = self._planned_sleep_hours(*self._sleep_anchors())
        r_yesterday = self.schedule_tone_sum(
            self._day_items(query, (now - timedelta(days=1)).strftime("%Y-%m-%d")))
        e_tomorrow = self.schedule_tone_sum(
            self._day_items(query, (now + timedelta(days=1)).strftime("%Y-%m-%d")))
        commitment_net = self._commitment_net_today()
        delta_limit = self.sleep_delta_limit()

        referee_task = str(getattr(cfg, "referee_task_name", "replyer") or "replyer")
        vad = self._machine.effective_vad()  # 裁判接收已应用偏移的值（§九.3）
        result = await ask_sleep_referee(
            self._llm(),
            persona=self._persona_text(), vad=vad,
            emotion_log_text=self._machine.emotion_log_text(),
            yesterday_schedule=self._day_summary(query, (now - timedelta(days=1)).strftime("%Y-%m-%d")),
            tomorrow_schedule=self._day_summary(query, (now + timedelta(days=1)).strftime("%Y-%m-%d")),
            long_term_status=self._long_term_status(),
            sleep_hours=hours, task_name=referee_task,
            delta_limit=delta_limit, r_yesterday=r_yesterday, e_tomorrow=e_tomorrow,
            commitment_net=commitment_net,
        )
        if result is None:
            return None
        delta, reasons = result
        cached = {
            "delta": delta, "reasons": reasons, "sleep_hours": hours,
            "r_yesterday": r_yesterday, "e_tomorrow": e_tomorrow,
            "commitment_net": commitment_net, "delta_limit": delta_limit, "computed_at": now,
        }
        self._log_decision(
            "emotion.sleep_overhaul_cached", trigger_type="emotion",
            reason="冻结期计算并缓存大修（未落状态）",
            sleep_hours=round(hours, 2), delta_limit=round(delta_limit, 4),
            delta_requested=delta.to_dict(), r_yesterday=r_yesterday.to_dict(),
            e_tomorrow=e_tomorrow.to_dict(), commitment_net=commitment_net,
        )
        return cached

    # ── 连续低迷软约束（M5；§4.2 / §6.2 F）──────────────────────
    def low_mood_days(self) -> int:
        """当前连续"日均线 V<−0.5"的天数（未满门槛也返回实数，供观测/命令）。"""
        return int(getattr(self._machine, "low_mood_streak", 0) or 0)

    def low_mood_state(self) -> Dict[str, Any]:
        """连续低迷达标状态：满 :data:`LOW_MOOD_MIN_DAYS` 天返回 ``{"days","date","mean_valence"}``，
        否则 ``{}``（planning 据此强制插入一次恢复类活动，§6.2 F）。重启后可从落盘计数派生。"""
        days = self.low_mood_days()
        if days < LOW_MOOD_MIN_DAYS:
            return {}
        state = dict(self._low_mood_day or {})
        if not state:
            state = {"days": days, "date": str(self._machine.low_mood_last_eval_date or ""),
                     "mean_valence": self._machine.daily_v_mean(
                         self._machine.low_mood_last_eval_date)}
        return state

    def _tick_daily_mood(self, now: datetime) -> Optional[Dict[str, Any]]:
        """每个检测 tick：采样当天 effective V（**仅清醒时段**）；日期翻页后评估连续低迷。

        「日」口径 = **清醒时段**的 effective V 采样均值（冻结的睡眠期不采样：
        否则 23:00→07:00 会贡献约 1/3 的重复冻结值，稀释日均线）。

        连续判定按**日历逐日**补评（不是只遍历有采样的日子）：从
        ``max(已评估日+1, 昨天−(保留天数−1))`` 走到昨天，缺失/无采样的日子
        ``daily_v_mean`` 为 None → 该日按非低迷处理并**归零**（与状态机语义一致），
        因此停机多日后不会拿陈旧采样批量拼出假连续，事件 ``date`` 恒为昨天（新鲜）。

        Returns:
            连续天数达门槛时返回事件载荷（由调用方 ``publish``），否则 None；
            每日最多评估/发布一次（``low_mood_checked_date`` 幂等）。
        """
        today = now.strftime("%Y-%m-%d")
        if not self.is_frozen():
            try:
                self._machine.sample_daily_v(today, float(self._machine.effective_vad().valence))
            except Exception:
                pass
        if self._machine.low_mood_checked_date == today:
            return None
        prev = (now - timedelta(days=1)).date()
        last = None
        last_text = str(self._machine.low_mood_last_eval_date or "")
        if last_text:
            try:
                last = datetime.strptime(last_text, "%Y-%m-%d").date()
            except ValueError:
                last = None
        start = prev - timedelta(days=max(1, int(DAILY_V_KEEP_DAYS)) - 1)
        if last is not None and (last + timedelta(days=1)) > start:
            start = last + timedelta(days=1)
        cursor = start
        while cursor <= prev:  # 日历逐日（含无采样日 → 归零）
            self._machine.update_low_mood_streak(cursor.strftime("%Y-%m-%d"))
            cursor += timedelta(days=1)
        self._machine.low_mood_checked_date = today
        self._save_state()
        streak = self.low_mood_days()
        if streak >= LOW_MOOD_MIN_DAYS:
            mean = self._machine.daily_v_mean(self._machine.low_mood_last_eval_date)
            self._low_mood_day = {
                "days": int(streak), "date": str(self._machine.low_mood_last_eval_date or ""),
                "mean_valence": round(float(mean), 4) if mean is not None else None,
            }
            return dict(self._low_mood_day)
        self._low_mood_day = {}  # 未达标 → 清空（planning 侧不再强制）
        return None

    def _wake_side_inputs(self, ref: datetime,
                          cached: Dict[str, Any]) -> Tuple[VADState, VADState, float]:
        """醒来时重算状态侧三输入（R/E/约定）；日期基准 = 入睡时刻（``computed_at``）。

        读不到/异常时回退缓存值（可空降级，§八.2）。
        """
        query = self._schedule_query()
        try:
            r_yesterday = self.schedule_tone_sum(
                self._day_items(query, (ref - timedelta(days=1)).strftime("%Y-%m-%d")))
            e_tomorrow = self.schedule_tone_sum(
                self._day_items(query, (ref + timedelta(days=1)).strftime("%Y-%m-%d")))
            commitment_net = self._commitment_net_today()
        except Exception as exc:
            self._ctx.logger.debug("睡眠大修三输入重算失败（回退缓存值）：%s", exc)
            return (cached.get("r_yesterday") or VADState(),
                    cached.get("e_tomorrow") or VADState(),
                    float(cached.get("commitment_net") or 0.0))
        return r_yesterday, e_tomorrow, commitment_net

    async def _apply_cached_overhaul(self, now: datetime) -> Optional[Dict[str, Any]]:
        """醒后应用缓存的大修并解冻（一次性）；无缓存时只解冻（裁判失败夜不结算）。

        M5 修复（E_tomorrow 生产时序）：入睡时刻（23 点）读取"次日"日程时，次日日程还没到
        ``auto_generate_time``（默认 00:30）生成，恒为零向量；醒来时按**入睡日**为基准重算
        R_yesterday（入睡日前一天）/E_tomorrow（入睡日的次日＝醒来日，此时已生成）与约定净兑现，
        保证"明日预期"与"当日约定"在状态侧真实生效（裁判文本仍用缓存时刻的输入，仅供判断）。

        Returns:
            应用结果 dict（含 ``ok``/``recovery_factor``/三输入留痕）；无缓存返回 None。
        """
        cached, self._pending_overhaul = self._pending_overhaul, None
        self._sleep_period = None  # 周期结束（醒沿）：后续 tick 重新按条目/锚点判定
        if cached is None:
            if self.is_frozen():
                self._machine.unfreeze(now)
                self._save_state()
            return None
        ref = cached.get("computed_at") or now  # 入睡时刻（日期基准：R=入睡日-1，E=入睡日+1）
        query = self._schedule_query()
        r_yesterday, e_tomorrow, commitment_net = self._wake_side_inputs(ref, cached)
        applied = self._machine.apply_sleep_overhaul(
            cached["delta"], cached["sleep_hours"], now=now,
            r_yesterday=r_yesterday, e_tomorrow=e_tomorrow,
            commitment_net=commitment_net, delta_limit=cached.get("delta_limit"))
        applied["reasons"] = cached.get("reasons", {})
        applied["ok"] = True
        applied["r_yesterday"] = r_yesterday.to_dict()
        applied["e_tomorrow"] = e_tomorrow.to_dict()
        applied["commitment_net"] = commitment_net
        applied["delta_limit"] = cached.get("delta_limit")
        applied["r_yesterday_cached"] = cached.get("r_yesterday", VADState()).to_dict()
        applied["e_tomorrow_cached"] = cached.get("e_tomorrow", VADState()).to_dict()
        self._machine.unfreeze(now)  # 队列并入日志（观测保留，不二次入账）
        self._save_state()
        state = load_json(self._state_path, {}) or {}
        state["applied_sleep_date"] = str(cached.get("cycle_key") or now.strftime("%Y-%m-%d"))
        save_json_atomic(self._state_path, state)
        self._log_decision(
            "emotion.sleep_overhaul", trigger_type="emotion",
            reason="醒后一次性应用缓存大修",
            recovery_factor=applied.get("recovery_factor", 0.0),
            sleep_hours=applied.get("sleep_hours", 0.0),
            delta_limit=applied.get("delta_limit"),
            delta_requested=applied.get("delta_requested"), delta_applied=applied.get("delta_applied"),
            r_yesterday=applied.get("r_yesterday"), e_tomorrow=applied.get("e_tomorrow"),
            commitment_net=applied.get("commitment_net"),
            r_yesterday_cached=applied.get("r_yesterday_cached"),
            e_tomorrow_cached=applied.get("e_tomorrow_cached"),
        )
        # M0 事件：醒后 VAD + 恢复系数（附录 A ★，consumer=planning/qzone）；
        # M5：payload 补低分辨率 VAD 三档词（levels），供 planning 的 emotion_context 槽
        # 直接消费而无需解析文本（§6.2 F；数值一律不出现在提示词里）。
        mood = self.mood_text()
        eff = self._machine.effective_vad()
        levels = {"valence": _level(eff.valence), "arousal": _level(eff.arousal),
                  "dominance": _level(eff.dominance)}
        await self._publish("emotion.overhauled", mood=mood, levels=levels,
                            recovery_factor=applied.get("recovery_factor", 0.0),
                            sleep_hours=applied.get("sleep_hours", 0.0))
        await self._publish("emotion.updated", mood=mood, trigger="sleep_overhaul")
        self._ctx.logger.info(
            "睡眠情绪大修完成（醒后结算）：恢复系数 %.2f（睡 %.1f 小时），ΔVAD 已应用",
            applied.get("recovery_factor", 0.0), applied.get("sleep_hours", 0.0),
        )
        return applied

    async def run_sleep_overhaul(self) -> Dict[str, Any]:
        """立即执行一次睡眠期情绪大修（兼容入口：计算 + 应用 + 解冻，不等待醒沿）。

        M5 起正常路径走「冻结 → 醒后应用缓存」（``_check_and_overhaul``）；本方法保留
        给直接调用方，返回语义与旧版一致（``ok`` / ``reason`` / ``recovery_factor`` 等）。
        """
        now = datetime.now()
        item = self._current_sleep_item(now)
        if item is None:
            return {"ok": False, "reason": "当前无睡眠日程条目"}
        period_start, period_end, planned = self._sleep_entry_period(item, now)
        self._sleep_period = (period_start, period_end)
        if self._pending_overhaul is None:
            self._pending_overhaul = await self.compute_sleep_overhaul(now, sleep_hours=planned)
        if self._pending_overhaul is None:
            self._sleep_period = None
            return {"ok": False, "reason": "睡眠裁判请求/解析失败（下一轮重试）"}
        self._pending_overhaul.setdefault("cycle_key", self._cycle_key(period_start))
        return await self._apply_cached_overhaul(now) or {"ok": False, "reason": "大修应用失败"}

    async def _loop(self) -> None:
        """睡眠检测循环：进 sleep 条目 → 冻结+缓存大修；离开 → 应用缓存并解冻（M5）。"""
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
        """一个检测周期：进睡眠时段 → 冻结并在冻结期计算/缓存大修；醒沿 → 应用缓存 + 解冻。

        睡眠时段判定（M5 修复）：条目本身会被 ``_normalize_items`` 的作息锚点截断
        （最长为 23:00–23:15，夜间无条目覆盖），故**冻结窗口按 [schedule] 作息锚点整夜**
        （``_sleep_entry_period``），条目只作入口/小睡；醒沿 = 离开该窗口，或日程出现
        非睡眠的清醒活动（文档口径「醒来的第一个活动开始」）。
        """
        query = self._schedule_query()
        now = datetime.now()
        # M5 连续低迷日统计（不依赖日程：无 query 也要采样/评估）
        low_mood_payload = self._tick_daily_mood(now)
        if low_mood_payload is not None:
            await self._publish("emotion.low_mood_day", **low_mood_payload)
        if query is None:
            return
        current = query.find_current_activity(now.strftime("%Y-%m-%d"), now, include_sleep=True)
        sleep_item = current if self._is_sleep_item(current) else None
        awake_activity = current is not None and sleep_item is None

        # 醒沿：日程出现清醒活动，或已离开睡眠窗口（含条目被截断后的深夜/清晨）
        if awake_activity or (self.is_frozen() and not self._in_sleep_period(now)):
            await self._publish_woke_up_if_needed(now)   # M0：离开睡眠 → schedule.woke_up
            await self._apply_cached_overhaul(now)       # M5：应用缓存并解冻
            return

        if self.is_frozen():
            if self._pending_overhaul is None:           # 上轮裁判失败 → 冻结期内重试
                planned = float(getattr(self, "_sleep_planned_hours", 0.0) or 0.0)
                cached = await self.compute_sleep_overhaul(now, sleep_hours=planned or None)
                if cached is not None:
                    period = self._sleep_period or (now, now)
                    cached["cycle_key"] = self._cycle_key(period[0])
                    self._pending_overhaul = cached
                else:
                    self._ctx.logger.debug("睡眠大修计算失败：保持冻结，下一轮重试")
            return

        # 未冻结：睡眠入口 = 睡眠条目，或已处于作息夜间窗口（如夜间启动/热重载后无条目）
        night_start, night_end = self._night_window(now)
        if sleep_item is None and not (night_start <= now < night_end):
            return                                        # 白天且无睡眠条目：无事
        period_start, period_end, planned = self._sleep_entry_period(sleep_item, now)
        await self._publish_sleep_started_if_needed(sleep_item, now, planned_hours=planned)
        cycle_key = self._cycle_key(period_start)
        state = load_json(self._state_path, {}) or {}
        # 本睡眠周期已结算过 → 跳过（兼容旧值：裸日期；热重载后不重复冻结/不重复请求裁判）
        if state.get("applied_sleep_date") in (now.strftime("%Y-%m-%d"), cycle_key):
            return
        self._sleep_period = (period_start, period_end)
        self._machine.freeze(now)                        # M5：冻结（事件只入队、暂停衰减/结算）
        cached = await self.compute_sleep_overhaul(now, sleep_hours=planned)
        if cached is None:
            self._ctx.logger.debug("睡眠大修计算失败：保持冻结，下一轮重试")
            return
        cached["cycle_key"] = cycle_key
        self._pending_overhaul = cached

    # ── 睡眠进出事件（M0：只发事件；M5 起与冻结/结算绑定）──
    async def _publish_sleep_started_if_needed(self, item: Optional[Dict[str, Any]], now: datetime,
                                               planned_hours: Optional[float] = None) -> None:
        """进入睡眠的上升沿 → schedule.sleep_started（payload: sleep_hours）。

        ``planned_hours`` 给出时优先（夜间睡眠按作息锚点整夜，而非被截断的条目时长）；
        缺省仍按条目 start/end（旧行为/小睡/直接调用方）。
        """
        if self._in_sleep:
            return
        self._in_sleep = True
        self._sleep_started_at = now
        self._sleep_planned_hours = float(planned_hours) if planned_hours is not None \
            else self._sleep_hours(item or {})
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
