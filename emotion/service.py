"""情绪系统服务：状态持久化 / 睡眠检测循环 / 人设初始化 / 情绪注入文本。"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.llm_utils import purpose_task
from core.storage import load_json, save_json_atomic
from core.time_utils import parse_hhmm
from planning.schedule_query import ScheduleQuery
# M9（附录 §4.3 用户裁决 A2）：赖床工具名与钳制上限的**唯一定义点**在 planning/tools_service.py；
# 提醒文本与组件注册同源引用。planning 侧不 import emotion（单向），故无循环依赖。
from suite_prompts import (
    EMOTION_INJECTION_DEFAULT,
    prompt as prompt_of,
    render,
)

from planning.tools_service import (DEFAULT_LIE_IN_MAX_UNTIL, NAP_TOOL_NAME,
                                    normalize_hhmm, parse_nap_target)

from . import descriptions, mapping_override
from .bridge import apply_template_events
from .referee import ask_sleep_referee, derive_params_from_persona, evaluate_reply_delta
from .state_machine import (DAILY_V_KEEP_DAYS, LOW_MOOD_MIN_DAYS, SCHEDULE_TONE_CLAMP,
                            EmotionParams, EmotionStateMachine)
from .vad import VADState, _clip, _level, apply_mapping_tables, map_emotions, mood_summary

logger = logging.getLogger(__name__)

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
#: 情绪评估模式合法值（REGISTRY §二.5：episode=会话闲置后整段评估 / per_reply=旧每回复 / off=不评估）
EVALUATE_MODES = ("episode", "per_reply", "off")
#: episode 片段的消息条数 / 字符数上限（超出时保留**最近**的往来并在头部标注省略）
EPISODE_SEGMENT_MAX_MESSAGES = 40
EPISODE_SEGMENT_MAX_CHARS = 3000
#: 片段进评估 prompt 的字符预算 = 片段上限 + 头部标注余量：保证完整片段（含省略标注）
#: 原样进 prompt；若沿用评估器默认的 600 字头部预算，模型只会看到片段最旧的一段
EPISODE_PROMPT_BUDGET_CHARS = EPISODE_SEGMENT_MAX_CHARS + 200
#: 该流从未评估过（水位缺失）时的回看窗口（分钟）：绝不把 start_time=0 递给宿主，
#: 否则 limit 取回的是该流**最早**的若干条消息，与"最近一段往来"语义相反
EPISODE_BOOTSTRAP_LOOKBACK_MINUTES = 120.0
#: 片段行里判定 bot 自身的条目标记键（宿主形态不一，取到即用；取不到统一标 "msg"）
_BOT_MARK_KEYS = ("is_mai", "is_bot", "is_self")
#: 插件侧 bot 自身 QQ 的属性名（与片段条目的 user_info.user_id 比对）
_BOT_QQ_ATTRS = ("_bot_qq", "_self_qq")
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
#: 冻结态注入的深夜引导行（附录 §2.5.5，逐字定稿）：非冻结态绝不出现，冻结态追加在既有文本后
NIGHT_INJECTION_HINT = ("（现在是深夜，你本该在睡觉。回复尽量短、慢、软一些；"
                        "不要主动开启新话题；不要提及本提示。）")
#: 夜间事件摘要的单条描述截断 / 落 decision_logs 的条数上限（§2.5.3：超限只留最近 N 条，
#: ``drained_events_count`` 仍报真实条数——摘要只作可追溯留痕，不参与任何结算）
DRAINED_SUMMARY_DESC_CHARS = 60
DRAINED_EVENTS_MAX = 20
#: L1 夜间打扰记账的配置缺省（附录 §2.1 定稿值：窗口内 3 次 reply / 窗口 10min / 重新入睡 5min）
DEFAULT_INTERRUPT_REPLY_THRESHOLD = 3
DEFAULT_INTERRUPT_WINDOW_MINUTES = 10
DEFAULT_RESLEEP_MINUTES = 5
#: 打扰段归档条数上限（纯内存，供观测与醒沿整夜合计结算消费；decision_logs 才是权威留痕，
#: 超限丢最旧只防长跑进程无界增长）
NIGHT_SEGMENTS_MAX = 128
#: 整夜合计的净正 ΔV 曲线（附录 §2.3 定稿，M8 段 3）：首档 +0.05、每次递减 0.01、下限 0
#: （第 3 次 > 第 4 次的正向收益；被需要 > 被吵醒）
SLEEP_INTERRUPT_DELTA_FIRST = 0.05
SLEEP_INTERRUPT_DELTA_STEP = 0.01
#: 整夜合计上限 |ΔV| ≤ 0.15（附录 §2.3 表；远小于单事件 ±0.5 硬限幅——
#: 夜里"决定次日情绪的主语"仍是睡眠大修，打扰的暖意只在其结果之上叠加）
SLEEP_INTERRUPT_DELTA_CAP = 0.15
#: 赖床提醒轮（L3，附录 §4.5）的豁免窗口时长（秒）：提醒发出后的这段时间内，该流若意外
#: 触发 reply，不刷新 resleep 计时、不重开吵醒态、不额外限制工具调用。
#: 与 core/inject.py 的 NAP_BLOCK_LINGER_SECONDS 同值（提醒块"已过提醒轮"的口径）——
#: core 不 import emotion，故两处各留一份常量，同批改动需一起改。
NAP_REMINDER_ROUND_SECONDS = 120.0
#: 有效提醒次数上限的缺省（[planning_proactive] nap_reminder_max_per_day，§4.6 默认 2）
DEFAULT_NAP_REMINDER_MAX_PER_DAY = 2
#: 赖床提醒文本模板（附录 §4.3 逐字定稿，占位符照抄）：``{nap_tool_name}`` 引用
#: planning/tools_service.py 的 NAP_TOOL_NAME（A2：工具改名只需动那一处）；
#: ``{planned_wake_hhmm}`` = 当前睡眠窗口结束 / wake 锚点；``{lie_in_max_until}`` = [schedule_advanced] 配置值。
NAP_REMINDER_TEMPLATE = (
    "【临时提醒·不进下一轮上下文】你半夜被重要的人吵醒了，现在是 {now_hhmm}；"
    "你本来打算睡到 {planned_wake_hhmm}。如果还想再睡一会儿，可以调用 {nap_tool_name} "
    "把起床时间往后推（最晚 {lie_in_max_until}）。这条提醒只在本轮有效，"
    "不必向任何人提起\"昨晚被吵醒\"。是否调用由你决定。"
)


def sleep_interrupt_delta_v(count: Any) -> float:
    """整夜合计的净正 ΔV（附录 §2.3 固化为纯函数，便于直测）。

    单次净正 ΔV_i = ``max(0, 0.05 − 0.01×(i−1))``（i 从 1 起；首档 +0.05、每次递减 0.01、
    下限 0），整夜 ``delta_v = min(Σ ΔV_i, 0.15)``。只作用 V 维（净正），A/D 恒为 0。
    ``count`` 非数/负数一律按 0（零打扰 → 0.0，调用方据此不入账）。
    """
    try:
        total = max(0, int(count))
    except (TypeError, ValueError):
        total = 0
    delta = 0.0
    for index in range(total):
        delta += max(0.0, SLEEP_INTERRUPT_DELTA_FIRST - SLEEP_INTERRUPT_DELTA_STEP * index)
    return min(delta, SLEEP_INTERRUPT_DELTA_CAP)


def summarize_sleep_window(window_start: datetime, window_end: datetime, segments: Any,
                           resleep_minutes: float = DEFAULT_RESLEEP_MINUTES) -> Dict[str, Any]:
    """睡眠窗口口径的**纯计算**（附录 §2.4 / 用户裁决 A4；M8 段 3 落地，无副作用、可直接单测）。

    打扰区间_i = ``[首次 reply_i, 最后一次 reply_i + resleep_minutes]``（``resleep`` 缓冲计入
    被吵醒时长），逐段**封顶到窗口** ``[window_start, window_end]``；多段**取并集**——相邻/重叠
    只扣一次（"多个打扰段"不等于"多段时长相加"）。

    输入 ``segments`` = 本周期**已确认**（窗口计数达阈值）的打扰段记录（第 2 段的归档形态，
    取 ``started_ts`` / ``last_reply_ts`` 两个 epoch 秒字段）；缺失/坏值段跳过。

    Returns:
        ``{"sleep_window_minutes", "interrupted_minutes", "actual_sleep_hours", "interrupt_count"}``；
        ``∪`` 为空 → ``interrupted_minutes=0``、实际 = 窗口总长（与现状一致）；
        窗口倒挂（end ≤ start）→ 三个时长字段全 0，绝不出现负值。
    """
    try:
        start_ts = float(window_start.timestamp())
        end_ts = float(window_end.timestamp())
    except (AttributeError, TypeError, ValueError, OSError):
        start_ts = end_ts = 0.0
    span_minutes = max(0.0, (end_ts - start_ts) / 60.0)
    try:
        buffer_seconds = max(0.0, float(resleep_minutes)) * 60.0
    except (TypeError, ValueError):
        buffer_seconds = float(DEFAULT_RESLEEP_MINUTES) * 60.0
    intervals: list = []
    count = 0
    for seg in list(segments or []):
        if not isinstance(seg, dict):
            continue
        count += 1
        try:
            begin = max(start_ts, float(seg.get("started_ts")))
            finish = min(end_ts, float(seg.get("last_reply_ts")) + buffer_seconds)
        except (TypeError, ValueError):
            continue
        if finish > begin:
            intervals.append((begin, finish))
    intervals.sort()
    merged: list = []
    for begin, finish in intervals:
        if merged and begin <= merged[-1][1]:      # 相邻/重叠 → 合并（重叠只扣一次）
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    interrupted = sum(finish - begin for begin, finish in merged) / 60.0
    return {
        "sleep_window_minutes": round(span_minutes, 3),
        "interrupted_minutes": round(interrupted, 3),
        "actual_sleep_hours": round(max(0.0, span_minutes - interrupted) / 60.0, 6),
        "interrupt_count": count,
    }


def _event_fingerprint(text: str) -> str:
    """事件指纹（审计留痕用）：去空白、小写、截断 80 字；不参与跨来源重复判定（口径见 dedup_policy）。"""
    return "".join(str(text or "").split()).lower()[:80]


class EmotionService:
    """VAD 情绪系统的运行时载体（状态机 + 持久化 + 睡眠循环 + LLM 请求编排）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        self._ctx = plugin.ctx
        # 数据目录只在插件数据目录内（宿主隔离）；未接线场景绝不落宿主工作目录（插件中心评审阻断项）
        _raw_data_dir = getattr(plugin, "_data_dir", None)
        if not _raw_data_dir:
            _raw_data_dir = tempfile.gettempdir()
            logger.warning("EmotionService 未收到 plugin._data_dir，回退系统临时目录：%s", _raw_data_dir)
        data_dir = Path(_raw_data_dir)
        self._state_path = data_dir / "emotion_state.json"
        self._config_path = data_dir / "emotion_config.json"

        # ── M12（V3 接线）：情绪映射表覆盖层（可选 <data_dir>/emotion_mapping.json）──────
        # 与 emotion_config.json 同处装载一次 → 「默认 ⊕ 覆盖」合并 → 推入 vad/bridge 生效位
        # （两 action 在 load 内已落账，含 severity）；热重载 = on_load 重建本服务 → 自动重读
        # （plugin.py:65-113）。文件不存在 = 纯代码默认（静默、零 action），下游取值与接线前
        # 逐字一致。结果留服务实例（倍率三层取值的 JSON 侧 + 观测/直测用）。
        try:
            self._mapping_result = mapping_override.load_effective(
                data_dir, getattr(plugin, "_decision_logger", None),
                root_dir=getattr(plugin, "_plugin_root", None))
        except Exception as exc:  # 模块自身缺陷不拖挂情绪子系统（V2 模块 docstring 明文委托）
            logger.warning("情绪映射覆盖层装载异常（按纯默认处理）：%s", exc)
            self._mapping_result = None
        self._mapping_tables = mapping_override.merge_tables(self._mapping_result)
        apply_mapping_tables(prototypes=self._mapping_tables["prototypes"],
                             level_thresholds=self._mapping_tables["level_thresholds"])
        apply_template_events(self._mapping_tables["template_events"])
        # F5（2026-10-11）：情绪描述表（插件根目录 json 可编辑；只在加载/重载时读一遍）
        try:
            _proto_labels = [str(p.get("label") or "").strip()
                             for p in (self._mapping_tables.get("prototypes") or [])]
            self._emotion_descriptions = descriptions.load_descriptions(
                known_labels=[label for label in _proto_labels if label],
                decision_logger=getattr(plugin, "_decision_logger", None),
                root_dir=getattr(plugin, "_plugin_root", None))
        except Exception as exc:  # 描述表故障不拖挂情绪子系统（同覆盖层口径）
            logger.warning("情绪描述表装载异常（按内置默认处理）：%s", exc)
            self._emotion_descriptions = dict(descriptions.DEFAULT_EMOTION_DESCRIPTIONS)
        ctx_logger = getattr(self._ctx, "logger", None)
        if ctx_logger is not None and self._mapping_result is not None:
            if self._mapping_result.error is not None:
                ctx_logger.warning("情绪映射覆盖层拒绝（%s @ %s）→ 全量回退代码默认",
                                   self._mapping_result.error.get("error_kind"),
                                   self._mapping_result.error.get("error_key"))
            elif self._mapping_result.applied_keys:
                ctx_logger.info("情绪映射覆盖层已生效：%s（%s）",
                                "、".join(self._mapping_result.applied_keys),
                                self._mapping_result.counts())

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
        #: episode 模式的待评登记（纯内存，零 LLM）：stream → {"last_activity", "first_pending"}
        #: 丢失的最坏后果只是下个片段窗口变长（水位兜底，消息不会漏）
        self._pending_episodes: Dict[str, Dict[str, float]] = {}
        #: episode 水位（stream → 上次评估的 epoch 秒）与日计数（与状态同文件落盘，
        #: 缺键/坏类型 → 空默认，绝不因旧状态文件启动失败）
        self._episode_state: Dict[str, Any] = self._parse_episode_state(state.get("episode"))
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
        # M8 段 2 L1 夜间打扰记账（附录 §2.1/§2.2）：纯运行期状态、不落 emotion_state.json（F5，
        # 热重载丢账可接受——每次段闭合都有 decision_logs 留痕）
        #: 当前打扰段 {"cycle_key","started_at","last_reply_at","reply_count","window_reply_count",
        #: "stream_ids","last_stream_id","awake","confirmed_at","_stamps"}；无段为 None
        self._night_segment: Optional[Dict[str, Any]] = None
        #: 已闭合的打扰段归档（供醒沿整夜合计结算消费；含未达阈值的候选段，``awake`` 区分）
        self._night_archived: list = []
        #: §4.5 提醒轮豁免窗口的截止时刻（epoch 秒）：由 :meth:`note_reminder_round` 登记
        #: （赖床提醒发送成功后 = now + :data:`NAP_REMINDER_ROUND_SECONDS`）。
        self._reminder_round_until = 0.0
        # M9（附录 §4.1–§4.5 L3 赖床提醒）：纯内存运行期状态、不落任何状态文件（F5 同口径）
        #: 「有效提醒次数」按 cycle_key 计数（提醒发送成功 + 工具成功改日程各 +1）；
        #: 只留最近 4 个周期键（防长跑进程无界增长）
        self._nap_reminder_counts: Dict[str, int] = {}
        #: 已提醒过的段标记 ``(cycle_key, closed_at)``：保证"本轮每段至多一次"（额度之外的第二道闸）
        self._nap_reminded_segments: set = set()

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

    def _updated_payload(self, trigger: str) -> Dict[str, Any]:
        """``emotion.updated`` 事件载荷：``mood`` 低分辨率文案 + ``vad`` 三维绝对值。

        2026-10-20 用户口径（随图变动日志精细度）：账本条目必须带三维绝对值，
        渲染侧（companion/components）据此计算相邻两次更新的三维变动量，
        并把「三维无变动」的重复条目合并——低分辨率文案未跨档时连续多条
        逐字相同的日志（用户报障）由该字段支撑消除。
        """
        eff = self._machine.effective_vad()
        return {
            "mood": self.mood_text(),
            "trigger": str(trigger),
            "valence": round(float(eff.valence), 4),
            "arousal": round(float(eff.arousal), 4),
            "dominance": round(float(eff.dominance), 4),
        }

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
    @staticmethod
    def _parse_episode_state(raw: Any) -> Dict[str, Any]:
        """解析状态文件里的 ``episode`` 键（水位 + 日计数）；缺键/坏类型 → 空默认，不抛。"""
        out: Dict[str, Any] = {"last_eval_ts": {}, "eval_date": "", "eval_count": 0}
        if not isinstance(raw, dict):
            return out
        raw_ts = raw.get("last_eval_ts")
        if isinstance(raw_ts, dict):
            for stream, value in raw_ts.items():
                try:
                    out["last_eval_ts"][str(stream)] = float(value)
                except (TypeError, ValueError):
                    continue
        out["eval_date"] = str(raw.get("eval_date") or "")
        try:
            out["eval_count"] = max(0, int(raw.get("eval_count") or 0))
        except (TypeError, ValueError):
            out["eval_count"] = 0
        return out

    def _save_state(self) -> None:
        """状态机 + episode 水位/日计数写同一份 emotion_state.json。

        状态机 ``load_dict`` 只读自己认识的键（未知键忽略），故 ``episode`` 与
        ``applied_sleep_date`` 可以安全共存于同一文件；且落盘前**本方法主动保留合并**
        盘上已有的 ``applied_sleep_date``（醒沿结算的"本周期已结算"标记，状态机
        ``to_dict`` 不含该键）——否则 episode 冲刷/日切等任意共用出口都会把它抹掉。
        """
        state = self._machine.to_dict()
        state["episode"] = {
            "last_eval_ts": {str(k): round(float(v), 3)
                             for k, v in self._episode_state["last_eval_ts"].items()},
            "eval_date": str(self._episode_state["eval_date"]),
            "eval_count": int(self._episode_state["eval_count"]),
        }
        # P4 落盘护栏（最小 diff）：醒沿把 applied_sleep_date 直接写入文件，而本方法整体重写
        # 该文件；覆盖式并入盘上原值（状态机不认识该键，load_dict 忽略；不改其值、不新增字段）。
        previous = load_json(self._state_path, {}) or {}
        if isinstance(previous, dict) and "applied_sleep_date" in previous:
            state["applied_sleep_date"] = previous["applied_sleep_date"]
        save_json_atomic(self._state_path, state)

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

    def _primary_emotion_description(self) -> str:
        """当前主导情绪的详细文字描述（F5；映射表外/无命中 → 通用固定描述）。"""
        try:
            labels, _nearest = map_emotions(self._machine.effective_vad())
        except Exception:
            labels = []
        descriptions_map = getattr(self, "_emotion_descriptions", None) or {}
        if not labels:
            return descriptions_map.get(descriptions.GENERIC_LABEL) or                 descriptions.generic_description(descriptions.GENERIC_LABEL)
        primary, _share = labels[0]
        desc = descriptions_map.get(str(primary))
        if not desc:
            desc = descriptions.generic_description(str(primary))
        try:
            if len(labels) > 1 and float(labels[1][1]) >= 0.25:
                desc = f"{desc}同时夹杂着一点「{labels[1][0]}」的成分。"
        except (TypeError, ValueError):
            pass
        return desc

    def emotion_description_body(self) -> str:
        """当前主导情绪的详细描述正文（F5；面板展示用，无人称前缀）。"""
        return self._primary_emotion_description()

    def emotion_description_text(self, channel: str = "planner") -> str:
        """F5 双人称渲染：planner 第三人称（bot 名）、replyer 第二人称。

        返回值作为情绪注入模板的 ``mood`` 槽 / planner 日程块的精神状态行——
        详细文字描述替代「百分比 + 三维约值」（用户口径：数值渲染不得再进 LLM 面向文本）。
        """
        body = self._primary_emotion_description()
        if channel == "replyer":
            return f"你此刻的情绪：{body}"
        name = str(getattr(self, "_lover_name", "") or "麦麦")
        return f"{name}此刻的情绪：{body}"

    def injection_text(self, channel: str = "replyer") -> str:
        """情绪底色注入文本（planner/replyer 双通道共用，以系统条目插在第一条系统提示词后）。

        冻结态（睡眠窗口内）在既有文本**后追加一行**深夜引导（§2.5.5，零结构改动）：
        深夜回复该短/慢/软、不主动开新话题；非冻结态输出与本行加入前逐字一致
        （不得改动既有字节，否则等于改了白天的提示词契约）。
        """
        text = render(
            prompt_of(self._plugin, "emotion_injection"),
            fallback=EMOTION_INJECTION_DEFAULT,
            mood=self.emotion_description_text(channel),
        )
        if self.is_frozen():
            text = f"{text}\n{prompt_of(self._plugin, 'emotion_night_hint')}"
        return text

    # ── 情绪 → 行为结构化系数（M4；§4.2 行 5 / §6.2 C/D，附录 C）──────
    # 注：M6 emoji 情绪联动（emoji_enabled / emoji_for_mood / emoji_description）已于
    # 2026-10-11 按用户裁决移除（自 T6 起无消费组件；DEV_GUIDE 附录 A-7）；manifest 的
    # emoji.get_by_description 能力声明随后于 2026-10-12 恢复（§18.2 空间表情包配图成为
    # 新消费点——[emotion] emoji_enabled 配置键维持移除不变）。
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
        """/lbm_emotion_config 查看文本。"""
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

    def status_snapshot(self) -> Dict[str, Any]:
        """聚合总览用的只读状态快照（M7 /lbm_dashboard 消费）。

        只读：不落盘、不改状态机任何字段（含冻结标记）；数值口径与 ``params_summary`` /
        ``mood_text`` 同源（``labels`` 即 map_emotions 的前 3 个标签名，mood_text 取其中前 2 个）。
        """
        eff = self._machine.effective_vad()
        labels, _nearest = map_emotions(eff)
        params = self._machine.params
        return {
            "initialized": bool(params.initialized),
            "mood_text": self.mood_text(),
            "labels": [str(name) for name, _share in labels[:3]],
            "event_count": len(self._machine.emotion_log),
            "anchor": params.anchor.to_dict(),
            "offset": params.offset.to_dict(),
            "frozen": bool(getattr(self._machine, "frozen", False)),
        }

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

    # ── 评估模式与日上限（T7；REGISTRY §二.3 / §二.5，两模式共用一个计数）──
    def _evaluate_mode(self) -> str:
        """当前回复后评估模式：``episode``（默认）/ ``per_reply`` / ``off``。

        ``evaluate_mode`` 的值**≠出厂默认**时以其为准（非法值回落 episode，防手改配置写坏）；
        保持出厂值（或没写）时按旧布尔 ``evaluate_on_reply`` 迁移（true→episode / false→off）。
        2026-10-11 裁决（DEV_GUIDE 6.5-H）：弃 ``model_fields_set`` 信任——宿主把出厂默认
        merge 进配置后 fields_set 不能区分"配过/没配过"（与 auto_scheduler 的"值≠默认"口径
        统一）；代价是显式写回默认值时会退回旧布尔迁移，属已登记的口径取舍。
        """
        cfg = getattr(self._plugin.config, "emotion", None)
        configured = str(getattr(cfg, "evaluate_mode", "") or "").strip().lower()
        factory_default = "episode"
        fields = getattr(type(cfg), "model_fields", None) if cfg is not None else None
        field = fields.get("evaluate_mode") if isinstance(fields, dict) else None
        if field is not None:
            factory_default = str(getattr(field, "default", "") or "episode").strip().lower() or "episode"
        if configured and configured != factory_default:
            return configured if configured in EVALUATE_MODES else "episode"
        legacy = self._emotion_cfg("evaluate_on_reply", True)
        return "episode" if bool(legacy) else "off"

    def _evaluate_max_per_day(self) -> int:
        """情绪评估每日上限（次，夹 [0, 200]；0 = 当日不评估）。"""
        try:
            cap = int(self._emotion_cfg("evaluate_max_per_day", 10))
        except (TypeError, ValueError):
            cap = 10
        return max(0, min(200, cap))

    def _daily_quota_ok(self, now: datetime, *, label: str) -> bool:
        """日上限判定（episode / per_reply 共用同一计数）：跨日清零；超限记决策日志。

        只判定不计数——计数在真正花掉一次评估 LLM 时 +1（见 :meth:`_note_evaluation_used`）。
        """
        day = now.strftime("%Y-%m-%d")
        if str(self._episode_state.get("eval_date") or "") != day:
            self._episode_state["eval_date"] = day
            self._episode_state["eval_count"] = 0
        used = int(self._episode_state.get("eval_count") or 0)
        cap = self._evaluate_max_per_day()
        if used >= cap:
            self._log_decision(
                "emotion.evaluate_daily_cap", trigger_type="emotion",
                reason=f"daily_cap：当日情绪评估已达上限（{used}/{cap} 次），本次不再调 LLM",
                mode=str(label or ""), eval_date=day, eval_count=used, cap=cap,
            )
            return False
        return True

    def _note_evaluation_used(self) -> None:
        """记一次已发生的评估：日计数 +1 并落盘（跨日由 :meth:`_daily_quota_ok` 清零）。"""
        self._episode_state["eval_count"] = int(self._episode_state.get("eval_count") or 0) + 1
        self._save_state()

    def _advance_watermark(self, stream_id: str, ts: float) -> None:
        """推进某流的评估水位并落盘（= 丢弃该片段：同一段文本不再重复消费）。"""
        self._episode_state["last_eval_ts"][str(stream_id)] = float(ts)
        self._save_state()

    async def evaluate_reply(self, reply_text: str, session_id: str = "") -> None:
        """评估一轮回复的情绪增量并记录（节流 + 静默失败）。

        M3（§4.2 行 2 / §7 M3 行）：prompt 追加"用户侧最后一条消息"（经
        ``ctx.message.get_by_time_in_chat`` 反查该流最近消息，拿不到就跳过）；
        target 识别走 replyer.after_response 的 ``session_id`` 链路（== 目标私聊
        stream_id → K=1+档位加成，其余按非 target），加算倍率与 bridge 同口径
        （§6.2 A：final = delta × global_event_multiplier × K）。跨来源去重（§2.4.6 A3）
        在评估 LLM 之前先判：命中则本次不调 LLM、不入账，仅落一条去重决策日志。

        T7（REGISTRY §二.9）：本方法保留 per_reply 旧语义（含 60s 节流），但与 episode
        模式共用 ``evaluate_max_per_day`` 硬封顶——超限直接返回，不动节流时间戳、不动水位。
        """
        if not self._enabled():
            return
        if self._dedup_skip_result("replyer", session_id, reply_text) is not None:
            return
        if not self._llm_interval_ok():
            return
        if not self._daily_quota_ok(datetime.now(), label=f"per_reply:{str(session_id or '-')}"):
            return
        task_name = purpose_task(self._plugin, "emotion_evaluator", default="utils")
        user_message = await self._last_user_message_text(session_id)
        try:
            result = await evaluate_reply_delta(self._llm(), reply_text, self._persona,
                                                self._lover_name, task_name,
                                                user_message=user_message,
                                                plugin=self._plugin)
        except Exception:
            return
        if not result:
            return
        self._note_evaluation_used()          # 日计数按"实际花掉的评估"记账
        event, delta = result
        is_target = self.is_target_session(session_id)
        self._apply_external_delta(event or "日常对话", delta, source="replyer",
                                   multiplier=self.target_multiplier(is_target),
                                   is_target=is_target, session_id=session_id, llm=True)
        # M0 事件：VAD 结算后发低分辨率 VAD（附录 A ★，consumer=调试日志）
        await self._publish("emotion.updated", **self._updated_payload("reply_evaluated"))

    # ── 会话片段制（T7；REGISTRY §二：Hook 只记录 → 闲置冲刷 → 整段一次评估）──
    def note_reply_for_episode(self, session_id: str) -> None:
        """Hook 记录入口（episode 模式）：登记该流存在未结往来；纯内存、零 LLM、零任务。"""
        stream = str(session_id or "").strip()
        if not stream:
            return
        now = time.time()
        entry = self._pending_episodes.get(stream)
        if entry is None:
            self._pending_episodes[stream] = {"last_activity": now, "first_pending": now}
            return
        entry["last_activity"] = now
        entry.setdefault("first_pending", now)

    def _bot_qq(self) -> str:
        """插件侧 bot 自身 QQ（取不到返回空串；取不到时片段不猜说话人）。"""
        for attr in _BOT_QQ_ATTRS:
            value = str(getattr(self._plugin, attr, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _episode_user_id(item: Dict[str, Any]) -> str:
        """条目里发送者 QQ（``message_info.user_info.user_id``；取不到返回空串）。"""
        info = item.get("message_info") if isinstance(item.get("message_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        return str(user_info.get("user_id") or "").strip()

    def _episode_speaker(self, item: Dict[str, Any]) -> str:
        """片段行的说话人标注：能判定 bot → ``bot``，能判定非 bot → ``user``，判定不了 → ``msg``。

        判定来源二选一（都取不到就用 ``msg``，不做任何猜测）：条目标记键；条目
        ``user_info.user_id`` 与插件侧 bot QQ 比对。
        """
        for key in _BOT_MARK_KEYS:
            if item.get(key) is True:
                return "bot"
        user_id = self._episode_user_id(item)
        bot_qq = self._bot_qq()
        if user_id and bot_qq:
            return "bot" if user_id == bot_qq else "user"
        return "msg"

    def _episode_line(self, item: Any) -> str:
        """单条消息 → ``[HH:MM] 说话人: 文本``（无文本/非 dict 返回空串）；时间戳缺失则省去时间。"""
        if not isinstance(item, dict):
            return ""
        text = str(item.get("processed_plain_text") or "").strip()
        if not text:
            return ""
        try:
            stamp = datetime.fromtimestamp(float(item.get("timestamp"))).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            stamp = ""
        prefix = f"[{stamp}] " if stamp else ""
        return f"{prefix}{self._episode_speaker(item)}: {text}"

    async def _fetch_episode_segment(self, stream_id: str, since_ts: float) -> str:
        """读取某流自 ``since_ts`` 以来的完整往来（bot+user）并组装成片段文本；无内容返回 ""。

        调用形态与 :meth:`_last_user_message_text` 同款防御（getattr 判空、dict 形态兼容、
        异常返回空串，绝不阻塞冲刷）；``filter_mai=False`` 取全量——片段要的就是双方往来。
        条数/字符双上限，超出时**保留最近的往来**（丢弃最旧的整行）并在**头部**标注省略：
        评估最关心刚发生的事，砍头部会只剩最旧的对话（与"去短视"相反）；标注在头部也保证
        它落在下游 prompt 预算内、不会成为死代码。
        """
        sid = str(stream_id or "").strip()
        message_api = getattr(self._ctx, "message", None)
        getter = getattr(message_api, "get_by_time_in_chat", None)
        if not sid or not callable(getter):
            return ""
        end = time.time()
        start = float(since_ts or 0.0)
        if start <= 0:  # 该流从未评估过：给一个有界回看窗，start_time=0 会取回最早的若干条
            start = end - EPISODE_BOOTSTRAP_LOOKBACK_MINUTES * 60.0
        try:
            result = await getter(chat_id=sid, start_time=start, end_time=end,
                                  limit=EPISODE_SEGMENT_MAX_MESSAGES, filter_mai=False)
        except Exception:
            return ""
        if isinstance(result, dict):  # 兼容 dict 形态旧代码（messages 键，陷阱 #12）
            result = result.get("messages") or []
        if not isinstance(result, list):
            return ""
        lines = [line for line in (self._episode_line(item) for item in result) if line]
        if not lines:
            return ""
        kept = lines[-EPISODE_SEGMENT_MAX_MESSAGES:]
        dropped = len(lines) - len(kept)          # 被省略的整行条数（含下面被截断的那一条）
        total = sum(len(line) + 1 for line in kept)
        while len(kept) > 1 and total > EPISODE_SEGMENT_MAX_CHARS:
            total -= len(kept.pop(0)) + 1
            dropped += 1
        segment = "\n".join(kept)
        if len(segment) > EPISODE_SEGMENT_MAX_CHARS:   # 单条就超上限：只留它最近的部分
            segment = segment[-EPISODE_SEGMENT_MAX_CHARS:]
            dropped += 1
        if dropped:
            segment = f"…（较早往来已省略：{dropped} 条）\n" + segment
        return segment

    def _pending_seconds(self, stream_id: str) -> Optional[float]:
        """该流未结时长（登记表 ``first_pending`` → 现在，秒）；无登记返回 None。

        只作观测：写进片段冲刷的决策日志（``first_pending`` 的唯一消费点）。
        """
        entry = self._pending_episodes.get(str(stream_id or ""))
        if not isinstance(entry, dict):
            return None
        try:
            first = float(entry.get("first_pending") or 0.0)
        except (TypeError, ValueError):
            return None
        if first <= 0:
            return None
        return round(max(0.0, time.time() - first), 3)

    async def flush_episode(self, stream_id: str, *, reason: str,
                            now: Optional[datetime] = None) -> bool:
        """冲刷一个流的未结片段：读取 → A3 去重 → 整段一次评估 → 入账 → 水位/日计数落盘。

        返回 True 表示片段已评估并入账。False 覆盖：非 episode 模式 / 模块停用 / 日上限 /
        片段为空 / 去重命中 / 入账被拒 / LLM 失败——**除 LLM 失败外各分支都推进水位**
        （丢弃该片段：同一段文本不再重复消费，也防死循环重试）；LLM 失败不推进，下轮重试。
        评估不经 ``_llm_interval_ok``：60s 节流是 per_reply 模式的语义，片段制按水位去重；
        正文预算放宽到片段上限并保留最近往来（评估器默认 600 字/保留头部是 per_reply 语义，
        会把长片段砍成最旧的一段，最新交流进不了 prompt）。
        """
        stream = str(stream_id or "").strip()
        if not stream or not self._enabled():
            return False
        if self._evaluate_mode() != "episode":
            return False
        current = now or datetime.now()
        ts = current.timestamp()
        flush_reason = str(reason or "")
        pending_seconds = self._pending_seconds(stream)
        if not self._daily_quota_ok(current, label=f"episode:{flush_reason or 'flush'}"):
            # 日上限（裁定 REGISTRY §二.3）：该片段**丢弃不并入次日**（避免陈旧上下文），
            # 水位推进防重复取同一段；已有决策日志可回放
            self._advance_watermark(stream, ts)
            return False
        since = float(self._episode_state["last_eval_ts"].get(stream, 0.0) or 0.0)
        segment = await self._fetch_episode_segment(stream, since)
        if not segment:
            # 无可评内容（窗口内没有消息/宿主不可用）：水位推进防死循环重试
            self._advance_watermark(stream, ts)
            self._log_decision("emotion.episode_flush_skipped", trigger_type="emotion",
                               reason="empty_segment：片段窗口内没有可评内容",
                               stream=stream, flush_reason=flush_reason,
                               pending_seconds=pending_seconds)
            return False
        skip = self._dedup_skip_result("replyer", stream, segment)
        if skip is not None:
            # A3 照旧：同流窗口内工具上报已入账 → 本片段不入账（去重日志已由上面落账）
            self._advance_watermark(stream, ts)
            self._log_decision("emotion.episode_flush_skipped", trigger_type="emotion",
                               reason="duplicate_cross_source：与工具上报同流窗口内重复",
                               stream=stream, flush_reason=flush_reason,
                               pending_seconds=pending_seconds)
            return False
        task_name = purpose_task(self._plugin, "emotion_evaluator", default="utils")
        try:
            result = await evaluate_reply_delta(self._llm(), segment, self._persona,
                                                self._lover_name, task_name, user_message="",
                                                reply_max_chars=EPISODE_PROMPT_BUDGET_CHARS,
                                                reply_keep_recent=True,
                                                plugin=self._plugin)
        except Exception:
            return False          # 水位不推进：下轮重试（费用已花，不重复取同一段）
        if not result:
            return False
        event, delta = result
        is_target = self.is_target_session(stream)
        outcome = self._apply_external_delta(event or "会话片段", delta, source="replyer",
                                             multiplier=self.target_multiplier(is_target),
                                             is_target=is_target, session_id=stream, llm=True)
        if not outcome.get("ok"):
            # 评估期间工具上报抢先入账（唯一竞态）：LLM 已消费该片段，水位照推防重复扣费
            self._advance_watermark(stream, ts)
            self._log_decision("emotion.episode_flush_skipped", trigger_type="emotion",
                               reason=f"apply_rejected：{outcome.get('reason') or '未入账'}",
                               stream=stream, flush_reason=flush_reason,
                               pending_seconds=pending_seconds)
            return False
        self._episode_state["eval_count"] = int(self._episode_state.get("eval_count") or 0) + 1
        self._episode_state["last_eval_ts"][stream] = ts
        self._save_state()
        await self._publish("emotion.updated", **self._updated_payload("episode_evaluated"))
        self._log_decision(
            "emotion.episode_flushed", trigger_type="emotion",
            reason=f"会话片段评估入账（{flush_reason or 'flush'}）",
            stream=stream, flush_reason=flush_reason, pending_seconds=pending_seconds,
            messages=int(segment.count("\n")) + 1, chars=len(segment),
            eval_count=int(self._episode_state["eval_count"]), cap=self._evaluate_max_per_day(),
            is_target=bool(is_target), k=round(self.target_multiplier(is_target), 4),
        )
        return True

    async def flush_all_episodes(self, *, reason: str) -> int:
        """串行冲刷全部已登记流（避免 LLM 并发）；无论成败清空登记，返回成功条数。

        两个挂点，语义不同（都不改片段制本身的闲置口径）：

        - 睡眠入口（``_check_and_overhaul``，``reason="sleep_entry"``）：先结清未评片段再跑
          睡眠裁判（REGISTRY §二.4：冲刷的入账发生在冻结前才能进当天日志、被裁判读到）；
        - **醒沿**（``reason="wake"``，§2.5.1）：离开冻结时先结清"夜里最后一轮对话"，
          使其当夜被评估而不是攒到下一夜。此刻机器仍冻结 → 评估事件按 ``record_event``
          语义只入 ``pending_events``（不直接改 VAD），随后被解冻并入日志；夜里被打扰的
          情绪后果由 sleep_interrupt 整夜合计统一入账，这里入账会双重记账。
        """
        streams = list(self._pending_episodes.keys())
        done = 0
        for stream in streams:
            self._pending_episodes.pop(stream, None)   # 先移除本条（成败都不再登记），
            try:                                       # 同时保留 first_pending 供冲刷日志观测
                if await self.flush_episode(stream, reason=reason):
                    done += 1
            except Exception:
                continue
        return done

    async def flush_idle_episodes(self) -> None:
        """扫描登记表：把闲置超阈值（``episode_idle_minutes``）的流逐个冲刷。

        扫描精度 = 睡眠检测循环间隔（``check_interval_minutes``，默认 10min，见 ``_loop``）。
        成功或水位已推进（片段被丢弃）的流移出登记；LLM 失败（水位未动）保留待下轮重试；
        单流异常不影响其他流。
        """
        try:
            if self._evaluate_mode() != "episode" or not self._enabled():
                self._pending_episodes.clear()   # 模式热重载/停用：登记无意义，清掉防空转
                return
            cfg = getattr(self._plugin.config, "emotion", None)
            idle_seconds = max(1, int(getattr(cfg, "episode_idle_minutes", 10) or 10)) * 60
            now = datetime.now()
            wall = time.time()
            for stream in list(self._pending_episodes.keys()):
                try:
                    entry = self._pending_episodes.get(stream) or {}
                    if wall - float(entry.get("last_activity") or 0.0) < idle_seconds:
                        continue
                    before = float(self._episode_state["last_eval_ts"].get(stream, 0.0) or 0.0)
                    await self.flush_episode(stream, reason="idle", now=now)
                    after = float(self._episode_state["last_eval_ts"].get(stream, 0.0) or 0.0)
                    if after > before:
                        self._pending_episodes.pop(stream, None)
                except Exception:
                    continue
        except Exception:
            return

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

    def _merged_multiplier(self, name: str, default: float) -> float:
        """三层倍率取值：**JSON 合法覆盖 > config（含用户显式值）> 代码默认**。

        V1 裁定 precedence（`emotion/mapping_override.py` §③）。此处不复用
        ``merge_tables`` 的 multipliers 位：那里的默认基线是 config 的**字段默认值**
        （不含用户显式值），照搬会把用户配置顶掉；故直接读装载结果（JSON 侧）的 multipliers，
        缺表/被拒/无文件时回落 :meth:`_emotion_cfg`（用户 config 与字段默认都由它给出）。
        """
        result = self._mapping_result
        if result is not None and result.error is None and result.multipliers \
                and name in result.multipliers:
            return float(result.multipliers[name])
        return self._to_float(self._emotion_cfg(name, default), default)

    def global_event_multiplier(self) -> float:
        """全局事件幅度旋钮（JSON 覆盖 > config > 默认 1.0，夹在 [0.1, 3.0]）。"""
        value = self._merged_multiplier("global_event_multiplier", 1.0)
        return max(0.1, min(3.0, value))

    def non_target_multiplier(self) -> float:
        """非 target 事件系数（JSON 覆盖 > config > 默认 1.0，夹在 [0, 1]）。"""
        value = self._merged_multiplier("non_target_multiplier", 1.0)
        return max(0.0, min(1.0, value))

    def target_bonus_step(self) -> float:
        """target 每档加成步长（JSON 覆盖 > config > 默认 0.1，夹在 [0, 1]；附录 C）。

        D7：``companion/patrol.py`` 的沉默事件留痕字段经 ``EmotionServices``
        转发入口读本方法，与桥内实际入账的 K 同源。
        """
        value = self._merged_multiplier("target_bonus_step", 0.1)
        return max(0.0, min(1.0, value))

    def affection_tier(self) -> Optional[int]:
        """从 affection 状态层读当前档位（跨模块 getattr 判空；读不到返回 None）。

        档位数随生效预设（1~5 档，2026-10-17 §19）——tier() 在状态层已按预设钳制，
        此处仅防负数。
        """
        affection = getattr(self._plugin, "_affection", None)
        getter = getattr(affection, "tier", None)
        if not callable(getter):
            return None
        try:
            return max(0, int(getter()))
        except Exception:
            return None

    def target_bonus(self) -> float:
        """target 加算加成 = step × (档位相对位置 × 4 + 1)（§6.2 A）。

        2026-10-17 §19.4：档位数可变（1~5）后按 ratio×4 连续化——5 档时与旧
        ``step × (tier+1)`` 逐档一致（ratio=tier/4 → ×4+1 恒等）；档位不可得时 0。
        """
        tier = self.affection_tier()
        if tier is None:
            return 0.0
        size = getattr(getattr(self._plugin, "_affection", None), "preset", None)
        try:
            preset = size() if callable(size) else None
            count = int(getattr(preset, "size", 5)) if preset is not None else 5
        except Exception:
            count = 5
        count = max(1, count)
        ratio = 0.0 if count <= 1 else min(1.0, tier / (count - 1))
        return self.target_bonus_step() * (ratio * 4.0 + 1.0)

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
                "delta_before": delta, "delta_scaled": scaled, "multiplier": round(factor, 4),
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
            ``{"ok", "reason", "event", "delta", "delta_before", "delta_scaled", "multiplier",
            "k", "global_multiplier", "clipped", "dedup"}``（``delta_before``/``delta_scaled``/
            ``delta`` 即裁剪前/乘倍率后/入账值，供调用方留痕，M8 段 3 的整夜合计结算取用）；
            未入账时 ok=False 且
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
            task_name = purpose_task(self._plugin, "emotion_evaluator", default="utils")
            try:
                result = await evaluate_reply_delta(self._llm(), event_text, self._persona,
                                                    self._lover_name, task_name,
                                                    plugin=self._plugin)
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
            await self._publish("emotion.updated", **self._updated_payload("external_event"))
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
        """睡眠裁判增量动态限幅 = 0.4 + 0.4×``[schedule_advanced].emotion_influence``（§4.2 睡眠大修 v2）。

        替代旧固定 ±0.8；``emotion_influence`` 夹在 [0, 1]（0=下限 ±0.4，1=上限 ±0.8），
        配置缺失/非法一律回退默认 0.6（→ ±0.64）。
        """
        influence = self._to_float(self._schedule_cfg("emotion_influence", 0.6), 0.6)
        influence = max(0.0, min(1.0, influence))
        return 0.4 + 0.4 * influence

    def _schedule_cfg(self, name: str, default: Any) -> Any:
        """运行期动态读日程配置项（热重载即时生效；缺省回退默认值）。

        2026-10-10：`emotion_influence` 等深水项在 ``[schedule_advanced]``，先查高级段再回退基础段。
        """
        cfg = getattr(self._plugin.config, "schedule_advanced", None)
        if cfg is None or getattr(cfg, name, None) is None:
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
          条目本身跨午夜保留原时长（如 23:00 入睡 + 8h ⇒ ``23:00–次日 07:00``，2026-10-10
          N2/N4 起不再被截断到入睡锚点），但整夜窗口仍以 [schedule] 入睡/起床锚点为唯一
          真源（赖床延长、锚点漂移都会让条目端点与整夜不等），不能拿条目端点当窗口，
          否则会提前误判"醒来"；
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

    # ── L1 夜间打扰记账：判定与归属门控（附录 §2.1/§2.2；M8 第 2 段）──────────
    # 判定 + 留痕 + 内存归档（第 2 段）；整夜合计入账与实际睡着时长消费同一份归档
    # （第 3 段，见下面「L1 整夜合计结算」）；赖床提醒与 §4.5 豁免分属第 4 段。
    def _no_sleep_mode(self) -> bool:
        """``[schedule].no_sleep_mode`` 的**显式**读数（附录 §〇 C1 + §一 F1）。

        F1：该开关现有副作用只在日程后处理（删除/截断睡眠窗口条目、睡眠时段不触发活动切换），
        情绪层读不到任何"已静默"信号——L1 全线静默必须在新代码里自己判这个字段。
        """
        return bool(self._schedule_cfg("no_sleep_mode", False))

    def _in_night_sleep_cycle(self, now: datetime) -> bool:
        """是否处于**夜间睡眠周期**内（L1 计数前置；F15 既有上下文，不新增状态字段）。

        ``_in_sleep``（睡眠上升沿置位、醒沿清除）或 ``_sleep_period``（本周期窗口）任一在场，
        **且**时刻落在 [schedule] 锚点推出的整夜窗口内。小睡/午休（§八.7：其分支保持现状）
        因此天然不计入——与 §〇「午休被打扰本轮不做」一致。
        """
        if not (self._in_sleep or self._sleep_period is not None):
            return False
        night_start, night_end = self._night_window(now)
        return night_start <= now < night_end

    def _night_cycle_key(self, now: datetime) -> str:
        """打扰段归属的睡眠周期键（F6 既有形态 ``周期起始日@起始分钟``）。"""
        period = self._sleep_period
        start = period[0] if period else self._night_window(now)[0]
        return self._cycle_key(start)

    def _night_int(self, name: str, default: int) -> int:
        """运行期读 [emotion] 的打扰参数（热重载即时生效；非数/越界一律回退缺省，≥1）。"""
        try:
            value = int(self._emotion_cfg(name, default))
        except (TypeError, ValueError):
            return int(default)
        return value if value >= 1 else int(default)

    def note_night_reply(self, session_id: str, *, now: Optional[datetime] = None) -> bool:
        """登记一次「bot 回复了」（replyer.after_response 的夜间通道）；计入打扰计数返回 True。

        判定口径（附录 §2.1，用户裁决 A3）与门控：

        - **只登记真的 reply**：本方法唯一的计数入口（Hook 调用）；只触发 planner、没有 reply
          的轮次不经过这里 → 天然不算被吵醒（用户裁决原文）；
        - **归属门控**（§2.2）：仅 **target 私聊** 的 reply 计入（``is_target_session``）；
          群聊/其他私聊既不计数也不重置计时；计数**跨聊天流**（任一 target 私聊 reply 都计入/刷新）；
        - **生效门控**（§〇 C1 + F1/F15）：``[schedule].no_sleep_mode == true`` 时整条 L1 静默
          （显式判该字段，不依赖其日程侧副作用）；模块停用或非夜间睡眠周期内同样不记账；
        - **窗口滚动**：窗口 = ``[最后一次 reply − interrupt_window_minutes, 最后一次 reply]``，
          窗内累计 ≥ ``interrupt_reply_threshold`` → 确认"吵醒态"（时刻记 ``confirmed_at``）；
          此后直到 ``最后一次 reply + resleep_minutes`` 到期无新 reply 才回到睡眠态（§2.1）。

        登记前先懒结算（:meth:`_roll_night_segment`）：已过 resleep 的段就地闭合，
        故闭段留痕不依赖睡眠检测 tick（tick 内另有一处 resleep 扫描，见 ``_check_and_overhaul``）。
        """
        current = now or datetime.now()
        closed = self._roll_night_segment(current)
        if closed is not None:
            self._dispatch_nap_reminder_soon(closed, current)   # L3：resleep 闭合 → 提醒轮
        if self._no_sleep_mode() or not self._enabled() or not self._in_night_sleep_cycle(current):
            return False
        # §4.5 提醒轮豁免：提醒轮窗口内的 reply 不刷新 resleep 计时、不重开吵醒态
        # （只是不入账，reply 本身照常返回 False —— 与"非 target 不计数"同档语义）
        if self.in_reminder_round(current):
            return False
        stream = str(session_id or "").strip()
        if not self.is_target_session(stream):
            return False
        seg = self._night_segment
        if seg is None:
            seg = self._night_segment = {
                "cycle_key": self._night_cycle_key(current),
                "started_at": current,
                "last_reply_at": current,
                "reply_count": 0,          # 段内累计计入的 reply 次数（跨窗口不清零）
                "window_reply_count": 0,   # 最后一条 reply 时刻的窗口内计数（阈值判定口径）
                "stream_ids": [],
                "last_stream_id": stream,
                "awake": False,
                "confirmed_at": None,
                "_stamps": [],             # 滚动窗时间戳（epoch 秒；窗口按最后一次 reply 回看）
            }
        seg["last_reply_at"] = current
        seg["last_stream_id"] = stream
        if stream not in seg["stream_ids"]:
            seg["stream_ids"].append(stream)
        seg["_stamps"].append(current.timestamp())
        cutoff = current.timestamp() - self._night_int(
            "interrupt_window_minutes", DEFAULT_INTERRUPT_WINDOW_MINUTES) * 60.0
        seg["_stamps"] = [stamp for stamp in seg["_stamps"] if stamp >= cutoff]
        seg["reply_count"] = int(seg["reply_count"]) + 1
        seg["window_reply_count"] = len(seg["_stamps"])
        if not seg["awake"] and seg["window_reply_count"] >= self._night_int(
                "interrupt_reply_threshold", DEFAULT_INTERRUPT_REPLY_THRESHOLD):
            seg["awake"] = True
            seg["confirmed_at"] = current
        return True

    def _roll_night_segment(self, now: datetime) -> Optional[Dict[str, Any]]:
        """懒结算：当前段「最后一次 reply + resleep_minutes」已过 → 立即闭合（§2.1 回到睡眠）。"""
        seg = self._night_segment
        if seg is None:
            return None
        deadline = seg["last_reply_at"] + timedelta(
            minutes=self._night_int("resleep_minutes", DEFAULT_RESLEEP_MINUTES))
        if now <= deadline:
            return None
        return self.close_night_interrupt_segment("resleep", now=now)

    @staticmethod
    def _night_ts(value: Any) -> Any:
        """时间戳 → ISO 秒级字符串（decision_logs 经 ``json.dumps``：datetime 会整条写不进去）。

        用 ``isoformat`` 鸭子类型判定而非 isinstance：测试的注入时钟会把本模块的 ``datetime``
        名字换成子类，isinstance 对"非该子类的 datetime 实例"会误判为不可序列化。
        """
        return value.isoformat(timespec="seconds") if hasattr(value, "isoformat") else None

    def _night_segment_record(self, seg: Dict[str, Any], *, closed_at: Optional[datetime],
                              close_reason: str) -> Dict[str, Any]:
        """打扰段 → 对外记录（快照与归档同一形态；内部 ``_stamps`` 不外露）。

        ``is_target`` **恒为真**：归属门控（§2.2）已把群聊/其他私聊挡在计数之外，
        段内 reply 全部来自 target 私聊——字段保留以满足 §2.6 的留痕口径。
        ``stream_id`` 取段内**最后一条** reply 的流，``stream_ids`` 给出跨流合并的全集。
        """
        return {
            "cycle_key": str(seg.get("cycle_key") or ""),
            "started_at": self._night_ts(seg.get("started_at")),
            "last_reply_at": self._night_ts(seg.get("last_reply_at")),
            "started_ts": float(seg["started_at"].timestamp()),
            "last_reply_ts": float(seg["last_reply_at"].timestamp()),
            "reply_count": int(seg.get("reply_count") or 0),
            "window_reply_count": int(seg.get("window_reply_count") or 0),
            "stream_id": str(seg.get("last_stream_id") or ""),
            "stream_ids": list(seg.get("stream_ids") or []),
            "is_target": True,
            "awake": bool(seg.get("awake")),
            "confirmed_at": self._night_ts(seg.get("confirmed_at")),
            "closed_at": self._night_ts(closed_at),
            "close_reason": str(close_reason or ""),
        }

    def close_night_interrupt_segment(self, reason: str, *,
                                      now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """闭合当前打扰段并归档；**已确认（窗口计数达阈值）的段落一条** ``emotion.sleep_interrupt``。

        闭合时机（§2.6 → §2.1）：``resleep`` 到期（懒结算/tick 扫描）、睡眠窗口结束、醒沿。

        落日志口径（本段实现裁定，§2.6 表 + 附录 §八.4）：**每段至多一条完整记录、只在闭合时落**。
        确认成立时刻不另落一行，而是记进 ``confirmed_at``——与闭合时刻 ``closed_at`` 一起可分辨
        "何时判定成立、何时回到睡眠"；未达阈值的候选段（只触发 planner 的夜里寒暄）不落
        ``sleep_interrupt``，仅进内存归档（``awake=False``）供观测与醒沿结算消费。
        打扰段/计数只进内存 + decision_logs，**不新增 emotion_state.json 字段**（F5/§八.4；
        热重载丢账可接受，日志留痕在）。
        """
        seg, self._night_segment = self._night_segment, None
        if seg is None:
            return None
        current = now or datetime.now()
        closed = self._night_segment_record(seg, closed_at=current, close_reason=reason)
        self._night_archived.append(closed)
        if len(self._night_archived) > NIGHT_SEGMENTS_MAX:
            del self._night_archived[:-NIGHT_SEGMENTS_MAX]
        if closed["awake"]:
            self._log_decision(
                "emotion.sleep_interrupt", trigger_type="emotion",
                reason=f"夜间打扰段闭合（{closed['close_reason']}）："
                       f"窗口内 {closed['window_reply_count']} 次 target 私聊 reply",
                cycle_key=closed["cycle_key"], started_at=closed["started_at"],
                last_reply_at=closed["last_reply_at"], reply_count=closed["reply_count"],
                is_target=closed["is_target"], stream_id=closed["stream_id"],
                stream_ids=closed["stream_ids"], window_reply_count=closed["window_reply_count"],
                confirmed_at=closed["confirmed_at"], closed_at=closed["closed_at"],
                close_reason=closed["close_reason"],
            )
        return closed

    def night_interrupt_snapshot(self) -> Dict[str, Any]:
        """当前打扰段的只读快照（无段返回 ``{}``）；形态与归档条目一致（``closed_at=None``）。"""
        seg = self._night_segment
        if seg is None:
            return {}
        return self._night_segment_record(seg, closed_at=None, close_reason="")

    def night_interrupt_segments(self, cycle_key: Optional[str] = None) -> list:
        """已闭合打扰段的归档副本（可按 ``cycle_key`` 过滤；供观测与整夜合计结算消费——
        结算（:meth:`settle_night_interrupts`）会清空本周期条目，故醒沿之后同周期读数为空）。"""
        key = str(cycle_key or "")
        return [dict(seg) for seg in self._night_archived if not key or seg["cycle_key"] == key]

    def note_reminder_round(self, until: Optional[datetime] = None) -> None:
        """§4.5 提醒轮豁免的登记入口（赖床提醒发送成功后由提醒轮自行调用）。

        赖床提醒轮若意外触发 reply，不应刷新 ``resleep_minutes`` 计时、不应重新进入吵醒态。
        登记的是**截止时刻**（``until``，提醒轮 = ``now + NAP_REMINDER_ROUND_SECONDS``）；
        是否落在提醒轮内由 :meth:`in_reminder_round` 判定，豁免判定在 ``note_night_reply`` 内生效。
        """
        stamp = (until or datetime.now()).timestamp()
        self._reminder_round_until = max(float(self._reminder_round_until), float(stamp))

    def in_reminder_round(self, now: Optional[datetime] = None) -> bool:
        """当前是否处于提醒轮豁免窗口内（§4.5；无提醒轮登记时恒 False）。"""
        return float((now or datetime.now()).timestamp()) < float(self._reminder_round_until)

    # ── L3 赖床提醒与工具接线（附录 §4.1–§4.5；M9 落地）────────────────────
    def _nap_cfg(self, name: str, default: Any) -> Any:
        """运行期动态读 [planning_proactive] 的赖床配置（热重载即时生效；缺省回退默认值）。"""
        cfg = getattr(self._plugin.config, "planning_proactive", None)
        value = getattr(cfg, name, default) if cfg is not None else default
        return default if value is None else value

    def nap_reminder_max_per_day(self) -> int:
        """每睡眠周期的有效提醒上限（§4.1②/§4.6；``ge=1``，运行期非法值回退缺省 2）。"""
        try:
            value = int(self._nap_cfg("nap_reminder_max_per_day", DEFAULT_NAP_REMINDER_MAX_PER_DAY))
        except (TypeError, ValueError):
            return DEFAULT_NAP_REMINDER_MAX_PER_DAY
        return value if value >= 1 else DEFAULT_NAP_REMINDER_MAX_PER_DAY

    def nap_reminder_enabled(self) -> bool:
        """``[planning_proactive] nap_reminder_enabled``（§4.1④，默认 true）。"""
        return bool(self._nap_cfg("nap_reminder_enabled", True))

    def lie_in_max_minutes(self) -> int:
        """``[schedule_advanced] lie_in_max_until`` 的分钟数（与工具侧同源；非法回退 12:00）。

        提醒文本里的「最晚 {lie_in_max_until}」与本方法同源，保证提示与实际钳制一致。
        """
        raw = self._schedule_cfg("lie_in_max_until", DEFAULT_LIE_IN_MAX_UNTIL)
        return parse_nap_target(normalize_hhmm(raw, DEFAULT_LIE_IN_MAX_UNTIL)) or 12 * 60

    def nap_reminder_count(self, cycle_key: str = "", now: Optional[datetime] = None) -> int:
        """本 ``cycle_key`` 内已用的「有效提醒次数」（纯内存；缺省按当前周期键）。"""
        key = str(cycle_key or self._night_cycle_key(now or datetime.now()))
        return int(self._nap_reminder_counts.get(key, 0))

    def note_nap_reminder(self, cycle_key: str = "", now: Optional[datetime] = None) -> int:
        """「有效提醒次数」+1（§4.1②/§4.4 计数口径），返回自增后的值。

        两个入口共用同一计数：①提醒发送成功；②赖床工具成功修改日程（经
        :meth:`note_nap_lie_in_applied`）。**未调用/失败不计次**；额度用尽只停提醒，
        不阻止工具调用（本方法不判额度）。
        """
        key = str(cycle_key or self._night_cycle_key(now or datetime.now()))
        count = self.nap_reminder_count(key) + 1
        self._nap_reminder_counts[key] = count
        if len(self._nap_reminder_counts) > 4:      # 只留最近 4 个周期键（纯内存观测计数）
            for stale in sorted(self._nap_reminder_counts)[:-4]:
                self._nap_reminder_counts.pop(stale, None)
        return count

    def note_nap_lie_in_applied(self, now: Optional[datetime] = None) -> int:
        """赖床工具**成功修改日程**后由 planning 侧回调：本周期有效提醒次数 +1（§4.4）。"""
        current = now or datetime.now()
        used = self.note_nap_reminder(now=current)
        self._log_decision(
            "emotion.nap_lie_in_applied", trigger_type="emotion",
            reason="赖床工具已改写今日日程：有效提醒次数 +1",
            cycle_key=self._night_cycle_key(current), count=used,
            limit=self.nap_reminder_max_per_day())
        return used

    def in_night_sleep_cycle(self, now: Optional[datetime] = None) -> bool:
        """当前是否处于**夜间睡眠周期**内（§4.4「非睡眠周期调用 → 拒绝」的门控）。

        周期窗口在册（``_sleep_period``）时按该窗口判定——**含赖床延长后的端点**：工具把
        窗口推后之后，本工具的"还能赖床"窗口同步延长；无周期上下文时回退与 L1 记账同源的
        夜间周期判定（:meth:`_in_night_sleep_cycle`，其本身的语义与调用方均不改）。
        ``no_sleep_mode == true`` 一律 False（§〇 C1：L1/L3 全线静默）。
        """
        current = now or datetime.now()
        if self._no_sleep_mode():
            return False
        if self._sleep_period is not None:
            return bool(self._sleep_period[0] <= current < self._sleep_period[1])
        return bool(self._in_night_sleep_cycle(current))

    def extend_night_sleep_until(self, target: Any = None, *, now: Optional[datetime] = None) -> bool:
        """把**当前**睡眠周期窗口的结束端推到 ``target``（§4.4 第 5 条；工具成功后由 planning 回调）。

        三个条件全满足才改端点，否则原样返回 False（不改任何状态）：

        1. 本周期窗口在册（未在睡眠周期内/已醒 → 无事可延）；
        2. 目标端 **> 旧端**（只往后推，绝不提前起床）；
        3. 目标时刻受**同一** ``[schedule_advanced] lie_in_max_until`` 钳制（与工具侧同源，双保险）。

        端点一变，醒沿判定（``_in_sleep_period``）自然推迟——不必改别处状态（F15：复用既有
        周期上下文，不新增状态字段）。
        """
        if self._sleep_period is None or target is None:
            return False
        start, old_end = self._sleep_period
        try:
            minute = int(target.hour) * 60 + int(target.minute)
        except (AttributeError, TypeError, ValueError):
            return False
        minute = min(minute, self.lie_in_max_minutes())
        new_end = old_end.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
        if new_end <= old_end:                 # 同日内未后移（或落到当天更早）→ 无需延长
            return False
        self._sleep_period = (start, new_end)
        self._log_decision(
            "emotion.nap_lie_in_extended", trigger_type="emotion",
            reason="赖床工具已延长本睡眠周期窗口端点（醒沿推迟）",
            cycle_key=self._night_cycle_key(now or datetime.now()),
            old_end=old_end.isoformat(timespec="seconds"),
            new_end=new_end.isoformat(timespec="seconds"))
        return True

    def night_sleep_end_minute(self) -> Optional[int]:
        """当前夜间睡眠周期窗口结束的「当日分钟」（未在夜间周期 → None）。

        nap 工具写回日程**之前**的预检用（附录 §六.补 C4：目标不晚于当前醒沿时
        不写日程、不计次——避免"文案说没改、库里已被写坏"）；权威判定仍在
        ``extend_night_sleep_until``（本访问器只读，不改任何状态）。
        """
        if self._sleep_period is None:
            return None
        end = self._sleep_period[1]
        return end.hour * 60 + end.minute

    def nap_planned_wake(self, now: Optional[datetime] = None) -> datetime:
        """提醒文本里的「本来打算睡到」= 当前睡眠窗口结束（周期在册值优先，回退 wake 锚点）。"""
        current = now or datetime.now()
        if self._sleep_period is not None:
            return self._sleep_period[1]
        return self._night_window(current)[1]

    def nap_reminder_text(self, now: Optional[datetime] = None,
                          planned_wake: Optional[datetime] = None) -> str:
        """渲染赖床提醒文本（§4.3 模板；占位符全部由本方法填充，工具名引用 NAP_TOOL_NAME）。"""
        current = now or datetime.now()
        wake = planned_wake or self.nap_planned_wake(current)
        return render(
            prompt_of(self._plugin, "nap_reminder"),
            fallback=NAP_REMINDER_TEMPLATE,
            now_hhmm=f"{current:%H:%M}",
            planned_wake_hhmm=f"{wake:%H:%M}",
            nap_tool_name=NAP_TOOL_NAME,
            lie_in_max_until=normalize_hhmm(
                self._schedule_cfg("lie_in_max_until", DEFAULT_LIE_IN_MAX_UNTIL),
                DEFAULT_LIE_IN_MAX_UNTIL),
        )

    def _nap_trigger(self) -> Optional[Any]:
        """宿主主动开口入口（F9/F12）：``ctx.maisaka.proactive.trigger``（含旧形态回退）。

        宿主未提供该能力（离线测试骨架 / 未声明能力）→ None，调用方按"不发送"处理（§八.2）。
        """
        ctx = getattr(self, "_ctx", None)
        maisaka = getattr(ctx, "maisaka", None) if ctx is not None else None
        if maisaka is None:
            return None
        proactive = getattr(maisaka, "proactive", None)
        trigger = getattr(proactive, "trigger", None) if proactive is not None else None
        if trigger is None:
            trigger = getattr(maisaka, "trigger_proactive", None)   # 兼容旧形态
        return trigger if callable(trigger) else None

    async def maybe_send_nap_reminder(self, segment: Any = None, *,
                                      now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """赖床提醒轮（附录 §4.1–§4.5）：打扰段以 ``resleep`` 闭合后判定是否发一条提醒。

        判定顺序（§4.1；任一不满足即不发、不计数，并落一条 ``emotion.nap_reminder_blocked``）：

        0. 段有效性：该段**确认成立过**（``awake`` 且 ``confirmed_at`` 在）；每段至多提醒一次；
        1. 额度：本 ``cycle_key`` 已用次数 < ``nap_reminder_max_per_day``；
        2. 窗口：当前仍在睡眠窗口内（``_in_sleep_period``，含赖床延长后的端点）；
        3. ``no_sleep_mode == false``（§〇 C1：L1/L3 全线静默）；
        4. ``nap_reminder_enabled``（默认 true）。

        发送（F9/F12）：查主动打扰预算（仅目标陪伴私聊；按 ``stream_id`` 维度判定）→
        发预算记账事件（带 ``stream_id``）→
        ``ctx.maisaka.proactive.trigger(stream_id=段末条 reply 流, intent="nap_reminder",
        reason=提醒文本, metadata={"kind": "nap_reminder"})``。预算拒绝/触发失败 → 不发送不计数。
        （M11 附录 II §3.3：赖床提醒发往目标陪伴私聊 → 计入；非目标流的段不计入、不受限。）
        成功后：有效提醒次数 +1、登记 §4.5 提醒轮豁免窗口、落 ``emotion.nap_reminder_sent``。

        Returns:
            发送成功返回载荷 dict；未发送返回 None。
        """
        current = now or datetime.now()
        seg = dict(segment) if isinstance(segment, dict) else self.night_interrupt_snapshot()
        cycle_key = str((seg or {}).get("cycle_key") or self._night_cycle_key(current))
        used = self.nap_reminder_count(cycle_key, now=current)
        limit = self.nap_reminder_max_per_day()
        stream_id = str((seg or {}).get("stream_id") or "")

        def _blocked(reason: str, **extra: Any) -> None:
            self._log_decision(
                "emotion.nap_reminder_blocked", trigger_type="emotion",
                reason=f"赖床提醒未发送（{reason}）", block_reason=str(reason),
                cycle_key=cycle_key, count=used, limit=limit, stream_id=stream_id, **extra)

        # 0) 段有效性（§4.1⑤）+ 每段至多一次
        if not seg or not seg.get("awake") or not seg.get("confirmed_at"):
            _blocked("segment_not_confirmed")
            return None
        marker = (cycle_key, str(seg.get("closed_at") or ""))
        if marker in self._nap_reminded_segments:
            _blocked("segment_already_reminded")
            return None
        # 1) 额度（§4.1②）
        if used >= limit:
            _blocked("quota")
            return None
        # 2) 仍在睡眠窗口内（§4.1③）
        if not self._in_sleep_period(current):
            _blocked("gate_sleep_window")
            return None
        # 3) no_sleep_mode == false（§4.1④ + §〇 C1）
        if self._no_sleep_mode():
            _blocked("gate_no_sleep_mode")
            return None
        # 4) 提醒开关（§4.1 隐含项 / §4.6）
        if not self.nap_reminder_enabled():
            _blocked("gate_disabled")
            return None

        # 发送（F9：先查主动打扰预算，再记 proactive.used，最后触发；预算拒绝 → 不发送不计数）
        # M11（附录 II §3.3）：预算按 stream 维度判定——本提醒发往目标陪伴私聊故计入。
        trigger = self._nap_trigger()
        if trigger is None:
            _blocked("trigger_missing")
            return None
        bus = self._bus()
        if bus is not None and not bus.allow_proactive("emotion", "nap_reminder",
                                                       stream_id=stream_id):
            _blocked("budget")
            return None
        if bus is not None:
            try:
                await bus.publish("proactive.used", source="emotion",
                                  channel="emotion", intent="nap_reminder",
                                  stream_id=stream_id)
            except Exception:
                pass
        text = self.nap_reminder_text(current)
        try:
            result = await trigger(stream_id=stream_id, intent="nap_reminder", reason=text,
                                   metadata={"kind": "nap_reminder"})
        except Exception as exc:
            _blocked("trigger_error", error=str(exc)[:120])
            return None
        if isinstance(result, dict) and not result.get("success", False):
            _blocked("trigger_rejected", error=str(result.get("error") or "")[:120])
            return None

        # 成功后：计数 +1、登记豁免窗口（§4.5）、留痕
        used = self.note_nap_reminder(cycle_key, now=current)
        self._nap_reminded_segments.add(marker)
        if len(self._nap_reminded_segments) > 64:   # 防无界增长（纯内存标记）
            self._nap_reminded_segments = set(list(self._nap_reminded_segments)[-32:])
        self.note_reminder_round(current + timedelta(seconds=NAP_REMINDER_ROUND_SECONDS))
        self._log_decision(
            "emotion.nap_reminder_sent", trigger_type="emotion",
            reason=f"赖床提醒轮已发送（本周期第 {used}/{limit} 次）",
            cycle_key=cycle_key, count=used, limit=limit, stream_id=stream_id,
            text=text[:120], kind="nap_reminder")
        return {"sent": True, "cycle_key": cycle_key, "count": used, "limit": limit,
                "stream_id": stream_id, "text": text}

    def _dispatch_nap_reminder_soon(self, closed: Any, now: Optional[datetime] = None) -> None:
        """同步上下文（reply Hook 的懒结算）派发提醒轮：有事件循环就 create_task。

        ``note_night_reply`` 是同步入口，不能 await；提醒链路绝不能影响 reply 记账主路径，
        故这里只投递任务（失败静默、留 debug）。仅在段以 ``resleep`` 闭合时进入
        （§4.1：被吵醒且已回到睡眠）。
        """
        if str((closed or {}).get("close_reason") or "") != "resleep":
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._ctx.logger.debug("赖床提醒：无运行中的事件循环，跳过本次派发")
            return
        try:
            task = loop.create_task(self.maybe_send_nap_reminder(closed, now=now))
            self._bg_tasks.append(task)
            if len(self._bg_tasks) > 20:
                self._bg_tasks = [t for t in self._bg_tasks if not t.done()][-10:]
        except Exception as exc:
            self._ctx.logger.debug("赖床提醒派发失败（忽略）：%s", exc)

    async def _after_night_segment_closed(self, closed: Any, now: Optional[datetime] = None
                                          ) -> Optional[Dict[str, Any]]:
        """段闭合后的 L3 接线（§4.1第 1 条）：只在 ``resleep`` 闭合时进入提醒轮判定。"""
        if str((closed or {}).get("close_reason") or "") != "resleep":
            return None
        return await self.maybe_send_nap_reminder(closed, now=now)

    # ── L1 整夜合计结算（附录 §2.3/§2.4/§2.6；M8 第 3 段）────────────────────
    def _awake_night_segments(self, cycle_key: str) -> list:
        """本 ``cycle_key`` 内**已确认**（窗口计数达阈值）的打扰段（第 2 段内存归档的消费口）。

        §2.3 的 ``n`` = "每次达到阈值算 1 次打扰段"：只触发 planner 的夜里寒暄/未达阈值的候选段
        （``awake=False``）既不算打扰次数、也不扣睡眠时长——它们本来就没"被吵醒"。
        """
        key = str(cycle_key or "")
        if not key:
            return []
        return [dict(seg) for seg in self._night_archived
                if str(seg.get("cycle_key") or "") == key and bool(seg.get("awake"))]

    def _cycle_window(self, now: datetime,
                      period: Optional[Tuple[datetime, datetime]] = None
                      ) -> Tuple[datetime, datetime]:
        """本睡眠周期的窗口 ``(start, end)``：周期在册值优先，缺失回退作息锚点整夜窗口。"""
        if period and len(period) == 2 and period[0] and period[1]:
            return period[0], period[1]
        return self._night_window(now)

    def _sleep_window_summary(self, now: datetime,
                              period: Optional[Tuple[datetime, datetime]] = None,
                              cycle_key: str = "") -> Dict[str, Any]:
        """本周期睡眠窗口的 §2.4 摘要（``sleep_window_minutes``/``interrupted_minutes``/
        ``actual_sleep_hours``/``interrupt_count``；实际睡着时长替代计划时长进恢复系数）。"""
        window = self._cycle_window(now, period)
        key = str(cycle_key or self._cycle_key(window[0]))
        return summarize_sleep_window(
            window[0], window[1], self._awake_night_segments(key),
            self._night_int("resleep_minutes", DEFAULT_RESLEEP_MINUTES))

    async def settle_night_interrupts(self, now: Optional[datetime] = None, *,
                                      period: Optional[Tuple[datetime, datetime]] = None,
                                      cycle_key: str = "") -> Optional[Dict[str, Any]]:
        """睡眠周期结束（醒沿）时的**整夜合计结算**：把该周期打扰的净正暖意一次性入账（§2.3）。

        口径（附录 §2.3 + 用户裁决 C2/C3）：

        - ``n`` = 本 ``cycle_key`` 内已确认的打扰段数；``delta_v = min(Σ ΔV_i, 0.15)``
          （曲线见 :func:`sleep_interrupt_delta_v`）；
        - 入账走**既有** ``record_external_event``（§6.2 A 三层倍率同源，不新开路径）：
          ``multiplier`` 实参 = ``target_multiplier(True)``（被打扰唯一归属 target），
          ``global_event_multiplier`` 由入口内既有逻辑相乘——调用方**不再重复乘**；
        - 每 ``cycle_key`` 至多结算 1 次（C3）：结算后**清空本周期归档**，二次调用无段可结
          → 返回 None（天然幂等，不落任何日志）；
        - ``n == 0``（含 no_sleep_mode 静默/未被吵醒的夜）→ **不入账、不落 settled 日志**，
          返回 None；
        - **零 LLM**（模板直记）：整条路径没有模型调用；
        - **不破坏落盘护栏**：入账会把 ``emotion_state.json`` 重写（``_save_state`` 只写状态机 +
          episode），故本方法先留 ``applied_sleep_date`` 原值、入账后补回（F6「同周期不重复结算」
          的落盘护栏语义不变；不新增任何状态字段）。

        Args:
            now: 结算时刻（醒沿）。
            period: 本周期窗口（醒沿调用方在 apply 前留存，apply 会清 ``_sleep_period``）。
            cycle_key: 本周期键（缺省按窗口起点推导；醒沿调用方传 apply 载荷中的键）。

        Returns:
            结算载荷（含 ``delta_v`` 曲线值 / ``delta_v_applied`` 实际入账净 V 与 ``record``
            原始回执）；未结算返回 None。
        """
        current = now or datetime.now()
        window = self._cycle_window(current, period)
        key = str(cycle_key or self._cycle_key(window[0]))
        segments = self._awake_night_segments(key)
        # 周期已结束 → 本周期归档一律清空（含未达阈值的候选段）：归档的消费方只有本次结算，
        # 留着只会让热重载后的白天 tick 拿到陈旧账；"每周期至多 1 次"由此获得幂等保证。
        self._night_archived = [seg for seg in self._night_archived
                                if str(seg.get("cycle_key") or "") != key]
        count = len(segments)
        if count <= 0:
            return None
        summary = summarize_sleep_window(
            window[0], window[1], segments,
            self._night_int("resleep_minutes", DEFAULT_RESLEEP_MINUTES))
        delta_v = sleep_interrupt_delta_v(count)
        if delta_v <= 0:
            return None
        description = f"夜间被重要的人打扰 {count} 次（净正合计）"
        # 护栏（F6）：入账经 record_external_event → _apply_external_delta → _save_state，而
        # `_save_state` 只写状态机 + episode —— 会把醒沿刚写入的 `applied_sleep_date` 冲掉。
        # 先留一份、入账后原值补回（本函数不改该键的值，只是不让自己的入账把它抹掉；否则同一夜
        # 会因"本周期已结算"落盘护栏失效而被重新冻结 + 重复跑裁判）。
        guard = (load_json(self._state_path, {}) or {}).get("applied_sleep_date")
        # 入账经既有外部事件入口：K 实参 = target 档（被打扰唯一归属 target），全局倍率在
        # record_external_event/_apply_external_delta 内既有逻辑里乘一次（三层倍率同源）。
        outcome = await self.record_external_event(
            source="sleep_interrupt", description=description,
            delta=VADState(delta_v, 0.0, 0.0), multiplier=self.target_multiplier(True),
            session_id=str(segments[-1].get("stream_id") or ""))
        if guard is not None:
            state = load_json(self._state_path, {}) or {}
            if state.get("applied_sleep_date") != guard:
                state["applied_sleep_date"] = guard
                save_json_atomic(self._state_path, state)
        before = outcome.get("delta_before")
        scaled = outcome.get("delta_scaled")
        applied_delta = outcome.get("delta")
        net_v = float(applied_delta.valence) if isinstance(applied_delta, VADState) else 0.0
        self._log_decision(
            "emotion.sleep_interrupt_settled", trigger_type="emotion",
            reason=f"睡眠周期结束合并结算：{count} 次夜间打扰（净正合计，零 LLM）",
            cycle_key=key,
            interrupt_count=count,
            sleep_window_minutes=summary["sleep_window_minutes"],
            interrupted_minutes=summary["interrupted_minutes"],
            actual_sleep_hours=summary["actual_sleep_hours"],
            delta_v=round(float(delta_v), 4),          # 曲线合计（乘倍率前）
            delta_v_applied=round(net_v, 4),           # 实际入账净 V（乘倍率、限幅后）
            k=outcome.get("k"), global_multiplier=outcome.get("global_multiplier"),
            multiplier=outcome.get("multiplier"),
            record_ok=bool(outcome.get("ok")), record_reason=str(outcome.get("reason") or ""),
            # 与既有 emotion.external_event 日志同语义：裁剪前 / 乘倍率后 / 入账值（dict 三维）
            delta_before=before.to_dict() if hasattr(before, "to_dict") else None,
            delta_scaled=scaled.to_dict() if hasattr(scaled, "to_dict") else None,
            delta_applied=applied_delta.to_dict() if hasattr(applied_delta, "to_dict") else None,
            stream_id=str(segments[-1].get("stream_id") or ""),
            reply_count=sum(int(seg.get("reply_count") or 0) for seg in segments),
        )
        return {
            "ok": bool(outcome.get("ok")), "reason": str(outcome.get("reason") or ""),
            "cycle_key": key, "interrupt_count": count,
            "sleep_window_minutes": summary["sleep_window_minutes"],
            "interrupted_minutes": summary["interrupted_minutes"],
            "actual_sleep_hours": summary["actual_sleep_hours"],
            "delta_v": round(float(delta_v), 4), "delta_v_applied": round(net_v, 4),
            "description": description, "record": outcome,
        }

    async def compute_sleep_overhaul(self, now: Optional[datetime] = None,
                                     sleep_hours: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """冻结期计算大修并返回缓存载荷（**不落状态**）；裁判失败返回 None（下一轮重试）。

        输入（§4.2 睡眠大修 v2）：当天日志 + 前日残差 R_yesterday + 明日预期 E_tomorrow
        （均来自日程条目 ``emotional_tone``）+ 当日约定净兑现 + 动态限幅。
        ``sleep_hours`` 缺省按条目时长；服务侧传作息锚点整夜时长（见 ``_sleep_entry_period``）。

        E_tomorrow 在入睡时刻按"次日"读取，而次日日程要到生成时刻（M10 起 = 最后日程
        结束 + ``auto_generate_after_last_end_minutes``，缺省锚点等价旧 00:30）才生成——
        故这里只为裁判提供参考文本，**状态侧数值在醒来时会重算一次**
        （见 ``_apply_cached_overhaul``），保证"明日预期"在生产时序下确实生效。
        """
        cfg = getattr(self._plugin.config, "emotion", None)
        query = self._schedule_query()
        now = now or datetime.now()
        current = self._current_sleep_item(now)
        # 时长的来源优先级：调用方给的计划时长（作息锚点整夜） > 条目时长 > 锚点整夜兜底
        # （冻结期重试时可能已无当前条目——条目只有 15 分钟窗口，不能作为必要前提）
        # M8 段 3（附录 §2.5.4 / §2.4 / 用户裁决 A4）：本值仍是**计划**整夜时长——它既是裁判
        # prompt 的"预计睡眠时长"输入，也是缓存载荷的对照值；**恢复系数的输入在醒沿应用时改成
        # 实际睡着时长**（`_apply_cached_overhaul` 用 `summarize_sleep_window` 现算并传给
        # apply_sleep_overhaul），故收回执时 `cached["sleep_hours"]` 只表示"计划"。
        # 本函数另把"此刻窗口内的实际值"快照进 `actual_sleep_hours`（入睡时无扰段 → = 计划；
        # 裁判失败重试路径可能已有段），供对照与留痕，不参与裁判。
        if sleep_hours is not None:
            hours = float(sleep_hours)
        elif current is not None:
            hours = self._sleep_hours(current)
        else:
            hours = self._planned_sleep_hours(*self._sleep_anchors())
        window_now = self._sleep_window_summary(now, self._sleep_period)
        r_yesterday = self.schedule_tone_sum(
            self._day_items(query, (now - timedelta(days=1)).strftime("%Y-%m-%d")))
        e_tomorrow = self.schedule_tone_sum(
            self._day_items(query, (now + timedelta(days=1)).strftime("%Y-%m-%d")))
        commitment_net = self._commitment_net_today()
        delta_limit = self.sleep_delta_limit()

        referee_task = purpose_task(self._plugin, "emotion_referee", default="replyer")
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
            plugin=self._plugin,
        )
        if result is None:
            return None
        delta, reasons = result
        cached = {
            "delta": delta, "reasons": reasons, "sleep_hours": hours,
            # §2.4 摘要快照：`sleep_hours` 是**计划**整夜时长（裁判输入），
            # `actual_sleep_hours` 是**此刻**窗口内按扰段并集扣减后的实际值（醒沿应用时会重算，
            # 以届时归档为准——缓存只作对照留痕）。
            "sleep_window_minutes": window_now["sleep_window_minutes"],
            "interrupted_minutes": window_now["interrupted_minutes"],
            "actual_sleep_hours": window_now["actual_sleep_hours"],
            "interrupt_count": window_now["interrupt_count"],
            "r_yesterday": r_yesterday, "e_tomorrow": e_tomorrow,
            "commitment_net": commitment_net, "delta_limit": delta_limit, "computed_at": now,
        }
        self._log_decision(
            "emotion.sleep_overhaul_cached", trigger_type="emotion",
            reason="冻结期计算并缓存大修（未落状态）",
            sleep_hours=round(hours, 2), delta_limit=round(delta_limit, 4),
            delta_requested=delta.to_dict(), r_yesterday=r_yesterday.to_dict(),
            e_tomorrow=e_tomorrow.to_dict(), commitment_net=commitment_net,
            # §2.4 留痕（M8 段 3）：入睡时段的窗口/扰段快照（裁判失败重试路径可能已有段；
            # 恢复系数在醒沿用届时实算值，不取本快照）
            sleep_window_minutes=window_now["sleep_window_minutes"],
            interrupted_minutes=window_now["interrupted_minutes"],
            actual_sleep_hours=window_now["actual_sleep_hours"],
            interrupt_count=window_now["interrupt_count"],
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

    @staticmethod
    def _drained_events_summary(events: Any) -> Dict[str, Any]:
        """把解冻出的夜间事件压成可落 decision_logs 的紧凑摘要（§2.5.3）。

        返回 ``{"count": 条数, "events": ["描述（ΔV=…,ΔA=…,ΔD=…）", …]}``；``count`` 是
        真实条数，``events`` 超 :data:`DRAINED_EVENTS_MAX` 只保留最近 N 条（防日志膨胀）。
        条目形态异常一律跳过（留痕尽力而为，绝不影响结算）。
        """
        items = list(events or [])
        summary: list = []
        for event in items[-DRAINED_EVENTS_MAX:]:
            raw = event.to_dict() if hasattr(event, "to_dict") else event
            if not isinstance(raw, dict):
                continue
            delta = raw.get("delta_vad")
            if not isinstance(delta, dict):
                delta = {}
            try:
                line = (
                    f"{str(raw.get('event_description') or '')[:DRAINED_SUMMARY_DESC_CHARS]}"
                    f"（ΔV={float(delta.get('valence') or 0.0):+.2f},"
                    f"ΔA={float(delta.get('arousal') or 0.0):+.2f},"
                    f"ΔD={float(delta.get('dominance') or 0.0):+.2f}）"
                )
            except (TypeError, ValueError):
                continue
            summary.append(line)
        return {"count": len(items), "events": summary}

    async def _apply_cached_overhaul(self, now: datetime) -> Optional[Dict[str, Any]]:
        """醒后应用缓存的大修并解冻（一次性）；无缓存时只解冻（裁判失败夜不结算）。

        M5 修复（E_tomorrow 生产时序）：入睡时刻（23 点）读取"次日"日程时，次日日程还没到
        生成时刻（M10 相对锚点，缺省等价旧 00:30）生成，恒为零向量；醒来时按**入睡日**为基准重算
        R_yesterday（入睡日前一天）/E_tomorrow（入睡日的次日＝醒来日，此时已生成）与约定净兑现，
        保证"明日预期"与"当日约定"在状态侧真实生效（裁判文本仍用缓存时刻的输入，仅供判断）。

        M8 段 1（附录 §2.5.2/§2.5.3）结算顺序 = **先解冻再结算**：
        `unfreeze(drain_to_log=True)` 必须早于 `apply_sleep_overhaul`，否则 apply 尾部的清日志
        （M5 语义：大修后 `emotion_log = []`）会把刚并入的夜间事件连同 ΔVAD 一起抹掉（§一 F3）。
        解冻出的队列在此压成「夜间事件摘要」并随 ``emotion.sleep_overhaul`` 落 decision_logs
        （§2.5.3「至少落其一」的落点：这批事件的 ΔVAD **不进结算**——夜里被打扰的情绪后果由
        M8 第 3 段的 sleep_interrupt 整夜合计统一入账，此处入账即双重记账）。

        M8 段 3（附录 §2.5.4/§2.4，用户裁决 A4）恢复系数改用**实际睡着时长**：本周期窗口减去
        打扰区间并集（第 2 段归档，此刻醒沿已闭段）；计划时长只留作对照
        （``sleep_hours_planned``）与裁判输入。窗口/扰段三字段一并落 ``emotion.sleep_overhaul``
        与 ``emotion.overhauled`` 载荷（§2.4 留痕要求）。

        Returns:
            应用结果 dict（含 ``ok``/``recovery_factor``/``sleep_hours``（实际）/
            ``sleep_window_minutes``/``interrupted_minutes``/``interrupt_count``/三输入/
            夜间事件摘要留痕）；无缓存返回 None。
        """
        cached, self._pending_overhaul = self._pending_overhaul, None
        # 本周期窗口在下面即被清（周期结束）：先留一份给"实际睡着时长"与醒沿结算用
        period = self._sleep_period
        self._sleep_period = None  # 周期结束（醒沿）：后续 tick 重新按条目/锚点判定
        if cached is None:
            if self.is_frozen():
                self._machine.unfreeze(now)
                self._save_state()
            return None
        cycle_key = str(cached.get("cycle_key") or "")
        # §2.4/A4：恢复系数输入 = 实际睡着时长（窗口总长 − 打扰区间并集），不是缓存里的计划时长
        window_stats = self._sleep_window_summary(now, period, cycle_key)
        actual_hours = float(window_stats["actual_sleep_hours"])
        ref = cached.get("computed_at") or now  # 入睡时刻（日期基准：R=入睡日-1，E=入睡日+1）
        query = self._schedule_query()
        r_yesterday, e_tomorrow, commitment_net = self._wake_side_inputs(ref, cached)
        drained = self._machine.unfreeze(now)  # §2.5.2：先解冻（队列并入日志，观测保留）
        night = self._drained_events_summary(drained)  # §2.5.3：解冻即采集，apply 之后日志即清空
        applied = self._machine.apply_sleep_overhaul(
            cached["delta"], actual_hours, now=now,
            r_yesterday=r_yesterday, e_tomorrow=e_tomorrow,
            commitment_net=commitment_net, delta_limit=cached.get("delta_limit"))
        applied["reasons"] = cached.get("reasons", {})
        applied["ok"] = True
        applied["cycle_key"] = cycle_key or self._cycle_key(self._cycle_window(now, period)[0])
        # `sleep_hours` = 本次结算**实际**用的时长（进恢复系数）；计划值单独留痕供对照
        applied["sleep_hours"] = round(actual_hours, 3)
        applied["sleep_hours_planned"] = round(float(cached.get("sleep_hours") or 0.0), 3)
        applied["sleep_window_minutes"] = window_stats["sleep_window_minutes"]
        applied["interrupted_minutes"] = window_stats["interrupted_minutes"]
        applied["interrupt_count"] = window_stats["interrupt_count"]
        applied["r_yesterday"] = r_yesterday.to_dict()
        applied["e_tomorrow"] = e_tomorrow.to_dict()
        applied["commitment_net"] = commitment_net
        applied["delta_limit"] = cached.get("delta_limit")
        applied["r_yesterday_cached"] = cached.get("r_yesterday", VADState()).to_dict()
        applied["e_tomorrow_cached"] = cached.get("e_tomorrow", VADState()).to_dict()
        applied["drained_events_count"] = night["count"]
        applied["drained_events"] = night["events"]
        self._save_state()
        state = load_json(self._state_path, {}) or {}
        state["applied_sleep_date"] = str(cached.get("cycle_key") or now.strftime("%Y-%m-%d"))
        save_json_atomic(self._state_path, state)
        self._log_decision(
            "emotion.sleep_overhaul", trigger_type="emotion",
            reason="醒后一次性应用缓存大修",
            recovery_factor=applied.get("recovery_factor", 0.0),
            sleep_hours=applied.get("sleep_hours", 0.0),
            # §2.4 留痕（M8 段 3）：实际睡着时长口径 + 计划对照
            sleep_hours_planned=applied.get("sleep_hours_planned"),
            sleep_window_minutes=applied.get("sleep_window_minutes"),
            interrupted_minutes=applied.get("interrupted_minutes"),
            interrupt_count=applied.get("interrupt_count"),
            delta_limit=applied.get("delta_limit"),
            delta_requested=applied.get("delta_requested"), delta_applied=applied.get("delta_applied"),
            r_yesterday=applied.get("r_yesterday"), e_tomorrow=applied.get("e_tomorrow"),
            commitment_net=applied.get("commitment_net"),
            r_yesterday_cached=applied.get("r_yesterday_cached"),
            e_tomorrow_cached=applied.get("e_tomorrow_cached"),
            # §2.5.3 夜间事件可追溯（解冻队列摘要；apply 清日志后 decision_logs 是唯一留痕）
            drained_events_count=night["count"], drained_events=night["events"],
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
        await self._publish("emotion.updated", **self._updated_payload("sleep_overhaul"))
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
        """睡眠检测循环：进 sleep 条目 → 冻结+缓存大修；离开 → 应用缓存并解冻（M5）。

        同一循环顺带做 episode 闲置扫描（REGISTRY §二.2）——**扫描精度 = 本循环间隔**
        （``check_interval_minutes``，默认 10min），故片段实际冲刷时刻可能比
        ``episode_idle_minutes`` 晚最多一个循环间隔。
        """
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                if self._enabled():
                    await self._check_and_overhaul()
                    await self.flush_idle_episodes()
            except Exception:
                pass
            cfg = getattr(self._plugin.config, "emotion", None)
            interval = max(1, int(getattr(cfg, "check_interval_minutes", 10) or 10)) * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _check_and_overhaul(self) -> None:
        """一个检测周期：进睡眠时段 → 冻结并在冻结期计算/缓存大修；醒沿 → 冲刷片段 + 应用缓存 + 解冻。

        睡眠时段判定（M5 修复）：夜间睡眠条目跨午夜保留原时长（如 23:00 入睡 + 8h ⇒
        ``23:00–次日 07:00``，2026-10-10 N2/N4 起不再被截断到入睡锚点），但**冻结窗口仍按
        [schedule] 作息锚点整夜**（``_sleep_entry_period``，条目端点不作窗口），条目只作
        入口/小睡；醒沿 = 离开该窗口，或日程出现
        非睡眠的清醒活动（文档口径「醒来的第一个活动开始」）。醒沿顺序（M8 段 1 + 段 3，附录
        §2.5/§2.3）：``close_night_interrupt_segment("wake")`` → ``flush_all_episodes("wake")``
        → ``publish_woke_up`` → ``_apply_cached_overhaul``（内部先 ``unfreeze`` 并采集夜间事件
        摘要、按**实际睡着时长**结算大修）→ ``settle_night_interrupts``（整夜合计一次性入账，
        在**大修之后**：打扰的暖意叠加在大修结果之上，不被向锚点收敛稀释）。
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
            # M8 段 2（附录 §2.1/§2.6）：睡眠窗口结束/醒沿 → 闭合夜间打扰段（本周期已结束，
            # 段不能再延长；已确认的段在此落唯一一条 emotion.sleep_interrupt）。放在最前，
            # 保证后续冲刷/结算即便抛错也不丢这条留痕。
            self.close_night_interrupt_segment("wake", now=now)
            # §2.5.1 醒沿先冲刷片段：让「夜里最后一轮对话」当夜被评估，而不是攒到下一夜。
            # ⚠ 只在**睡眠周期真的结束**时冲刷（仍冻结 / 本周期已发过 sleep_started）：
            # 本分支白天每个 tick 都会命中（有清醒活动即进），无条件冲刷会让 episode
            # 闲置机制失效——每 tick 都评估一次、日上限被瞬间吃光。
            # 顺序约束：此刻机器仍处于冻结态，冲刷产生的评估事件按 record_event 既有语义
            # 只入 pending_events（**不直接改 VAD**）；随后解冻并入日志。绝不为"让冲刷增量
            # 生效"而绕过冻结——夜里被打扰的情绪后果由 M8 第 3 段的 sleep_interrupt 整夜合计
            # 统一入账（附录 §2.3），这里入账会造成双重记账。
            if self.is_frozen() or self._in_sleep:
                await self.flush_all_episodes(reason="wake")
            await self._publish_woke_up_if_needed(now)   # M0：离开睡眠 → schedule.woke_up
            # 醒沿最终顺序（附录 §2.3 + §2.5.4）：flush → unfreeze → 采集摘要 →
            # apply_sleep_overhaul → **sleep_interrupt 整夜合计结算**。
            # 本 tick 是否真的是"周期结束"（而非白天任意 tick 命中本分支）：与冲刷判定同源，
            # 另加"本周期窗口在册"；`_apply_cached_overhaul` 开头会清窗口，故先留一份。
            ended_cycle = bool(self.is_frozen() or self._in_sleep
                               or self._sleep_period is not None)
            period = self._sleep_period
            applied = await self._apply_cached_overhaul(now)     # M5：应用缓存并解冻
            # 顺序理由（附录 §2.3 上限注释）：大修是"夜里决定次日情绪的主语"，被打扰的暖意叠加
            # 在**大修结果之上**；若在大修前入账，会被 (raw − anchor) × recovery_factor 的向锚点
            # 收敛稀释（且恒被下界 0.3~0.85 的系数打折），与"净正合计整夜入账"的口径不符。
            # 无打扰周期 → 结算内部返回 None（不入账、不落 settled 日志）。
            if ended_cycle:
                settle_key = str(applied.get("cycle_key") or "") if applied else ""
                await self.settle_night_interrupts(now, period=period, cycle_key=settle_key)
            return

        if self.is_frozen():
            # M8 段 2（附录 §2.1）：冻结期（= 睡眠周期内）每个 tick 扫一次 resleep 到期，
            # 让「回到睡眠」的闭段留痕不必等到醒沿或下一条 reply。
            # M9（附录 §4.1）：同一次扫描闭段后进入赖床提醒轮判定（仅 resleep 闭段）。
            closed = self._roll_night_segment(now)
            if closed is not None:
                await self._after_night_segment_closed(closed, now)
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
        if await self._sleep_entry_settled_gate(sleep_item, now, period_start, planned):
            return
        self._sleep_period = (period_start, period_end)
        cycle_key = self._cycle_key(period_start)
        # 顺序约束（REGISTRY §二.4）：先冲刷全部未结片段，再冻结/跑睡眠裁判——冲刷的入账
        # 发生在冻结前才会进当天 emotion_log（冻结期事件只入队），裁判的日志输入才完整
        await self.flush_all_episodes(reason="sleep_entry")
        self._machine.freeze(now)                        # M5：冻结（事件只入队、暂停衰减/结算）
        cached = await self.compute_sleep_overhaul(now, sleep_hours=planned)
        if cached is None:
            self._ctx.logger.debug("睡眠大修计算失败：保持冻结，下一轮重试")
            return
        cached["cycle_key"] = cycle_key
        self._pending_overhaul = cached

    async def _sleep_entry_settled_gate(self, sleep_item: Optional[Dict[str, Any]], now: datetime,
                                        period_start: datetime, planned: Optional[float]) -> bool:
        """睡眠入口闸门：已结算周期 → 跳过重冻结/重裁判，且**不重发** sleep_started。

        R1 裁决（2026-10-11，锚点整夜口径维持）：宣告守卫**前置**——原实现先发布
        ``schedule.sleep_started`` 再查 ``applied_sleep_date`` 结算标记，热重载清零
        ``_in_sleep`` 内存态后，已结算周期的重入必然重发一次宣告。现在先查落盘的
        结算标记：已结算 → 直接跳过（不宣告、不冻结、不重跑裁判）；未结算 → 照常
        宣告（订阅方同样因重载丢了状态，需要重新宣告）。``ended_cycle`` 维持内存态
        口径不变——重入路径在此提前返回，不会触发第二次结算（每 cycle_key 结算 1 次）。
        放行后调用方需自行重取 ``cycle_key``（本方法只做结算判定与宣告）。
        """
        cycle_key = self._cycle_key(period_start)
        state = load_json(self._state_path, {}) or {}
        # 本睡眠周期已结算过 → 跳过（兼容旧值：裸日期；热重载后不重复冻结/不重复请求裁判）
        if state.get("applied_sleep_date") in (now.strftime("%Y-%m-%d"), cycle_key):
            return True
        await self._publish_sleep_started_if_needed(sleep_item, now, planned_hours=planned)
        return False

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
        """/lbm_emotion_init 主体：人设 → LLM 推导 → 校验落盘 → 生效。"""
        task_name = purpose_task(self._plugin, "emotion_evaluator", default="utils")
        params = await derive_params_from_persona(self._llm(), self._persona_text(), task_name,
                                                  plugin=self._plugin)
        if params is None:
            return "情绪初始化失败：模型未返回有效 JSON。请重试一次；若持续失败请检查模型配置。"
        self._machine.apply_params(params)
        self._save_params()
        self._save_state()
        lines = [
            "情绪系统初始化完成（参数已落盘，重启后依然生效）：",
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
        return "已恢复默认情绪参数（零锚点、零偏移、中性乘数），当前 VAD 归零。"

    def clear_log(self) -> str:
        """清空当天情绪日志（隐私接口，设计文档 §十三）。"""
        self._machine.emotion_log = []
        self._save_state()
        return "已清空当天情绪日志。"

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
