# -*- coding: utf-8 -*-
"""F4 社交边界（提示词与 planner 推动，绝不硬拦截）。

设计依据：``union/20261111_结构性延后项_设计方案_v2.md`` §5（F4）与
``union/20261111_实验配置迁移映射.md``（实验期配置全部集中在 ``[experimental]``）。

职责边界（只表达状态与语气，不做硬决定）：

- **不拦截、不延迟投递、不建账本、不收缩预算**；全模块无 ``abort`` 返回；
- 群聊只在「深工 / 已睡」时给 planner 一行状态：陈述事实，不写命令句；
  私聊与目标用户群内发言不注入；
- 目标私聊的极端状态（高压 / 低精力 / 低 mental_state）才切「拒绝 / 脆弱」
  语气分支；分支始终回扣宿主 ``personality.personality`` 文本，并按人设脾气
  动态选择「禁止人身攻击」或「按人设更冲但仍不脱人设」口径；
- 睡眠群保留 3.17.33 的「短、慢、软 + 吵醒记账」基线：本模块只提示可以短慢回、
  也可以先不理，**不改** ``emotion.service.note_night_reply`` 与睡眠债链；
- wait 提示只写宿主 wait 工具的 ``seconds`` 参数（1.3.16 兼容），不写 ``until``；
- 碎片化只在提示词层建议（不真分段），首期由 replyer 状态块追加；
- ``BoundaryFacts`` 任一取数异常 → 被动侧 fail-open（不注入行、不切语气），
  主动侧不消费本模块。

默认关（``[experimental].social_boundary_enabled=False``）时所有入口短路：
无新行、无语气切换、无提示、零随机、零状态写，3.17.33 文本逐字不变。
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.internal_tuning import INTERNAL_TUNING_DEFAULTS
from suite_prompts import (
    BOUNDARY_FRAGMENT_HINT_DEFAULT,
    BOUNDARY_GROUP_ASLEEP_LINE_DEFAULT,
    BOUNDARY_GROUP_DEEP_WORK_LINE_DEFAULT,
    BOUNDARY_PERSONA_BOTTOM_LINE_DEFAULT,
    BOUNDARY_PERSONA_HARSHNESS_ALLOWED,
    BOUNDARY_PERSONA_HARSHNESS_STRICT,
    BOUNDARY_SHORT_REPLY_HINT_DEFAULT,
    BOUNDARY_TONE_REFUSAL_DEFAULT,
    BOUNDARY_TONE_VULNERABLE_DEFAULT,
    BOUNDARY_WAIT_HINT_DEFAULT,
)

#: F4 参数默认值单一真源（转正后目标 = internal_tuning.json [boundary]）
_BD: Dict[str, Any] = dict(INTERNAL_TUNING_DEFAULTS["boundary"])

#: 人设脾气关键词（动态读宿主 ``personality.personality`` 文本；命中 = 人设本就暴躁）。
#: 只做保守关键词判定，不调用 LLM、不新增配置键。
HOT_TEMPER_MARKERS: Tuple[str, ...] = (
    "暴躁", "易怒", "脾气不好", "脾气差", "毒舌", "刻薄", "尖酸", "攻击性",
    "刀子嘴", "不好惹", "凶", "会怼", "爱怼", "怼人", "阴阳怪气", "犀利",
)

#: 人设回扣片段最大字数（代码层截断，避免长文本进 prompt）。
MAX_PERSONA_CHARS = 80

#: 入站「刚被点名/目标用户刚发言」的抑制窗（秒）：与 F1 的 direct 语义同源。
ADDRESS_LOOKBACK_SECONDS = 120.0

#: 进程内跟踪流上限（有界；LRU 淘汰最旧）。
MAX_TRACKED_STREAMS = 256

#: 无 ``[experimental]`` 段时的人设兜底锚点。
PERSONA_ANCHOR_FALLBACK = "按你原本的人设"

#: 回复侧沿用「被点名」结构化字段（宿主/兼容面）。
_ADDRESS_FLAG_KEYS = frozenset({
    "is_at", "is_mentioned", "is_mention", "mentioned", "at_bot",
    "is_at_bot", "mention_bot", "is_mentioned_bot", "at_me",
})
#: 宿主 ``<message ...>`` 前缀里的点名标志。
_ADDRESS_ATTR_PATTERN = re.compile(
    r"(?:is_at|is_mentioned)\s*=\s*[\"']true[\"']", re.IGNORECASE)


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    """把任意值夹取到 ``[low, high]``；非法/NaN/Inf 返回 ``default``（再夹取）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(float(low), min(float(high), number))


def _safe_int(value: Any, low: int, high: int, default: int) -> int:
    return int(_clamp(value, low, high, default=default))


def _num(value: Any) -> Optional[float]:
    """可空数值读取：bool/非数/NaN/Inf → None。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _now_dt(value: Any = None) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return datetime.now()
    return datetime.now()


def _stamp(value: Any = None) -> float:
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        try:
            return float(value.timestamp())
        except (OverflowError, OSError, ValueError):
            return time.time()
    try:
        return float(value)
    except (TypeError, ValueError):
        return time.time()


def _exp_section(plugin: Any) -> Any:
    return getattr(getattr(plugin, "config", None), "experimental", None)


def _cfg(plugin: Any, key: str, default: Any = None) -> Any:
    """读 ``[experimental].social_boundary_*``；缺失 → 内置默认。"""
    section = _exp_section(plugin)
    if section is not None:
        value = getattr(section, "social_boundary_" + key, None)
        if value is not None:
            return value
    return _BD.get(key, default)


def _str_list(value: Any, default: List[str]) -> List[str]:
    """字符串列表读取（非法/空 → 默认副本）；元素去空白、小写。"""
    if isinstance(value, (list, tuple, set)):
        out = [str(item).strip().lower() for item in value if str(item).strip()]
        return out or list(default)
    return list(default)


def _session_id(plugin: Any, session_id: Any) -> str:
    return str(session_id or "").strip()


def is_target_private(plugin: Any, session_id: Any) -> bool:
    """目标陪伴私聊判定（与 ``InjectService._is_target_private_session`` 同源）。"""
    target_stream = str(getattr(plugin, "_stream_id", "") or "")
    return bool(target_stream) and _session_id(plugin, session_id) == target_stream


def is_group_session(plugin: Any, session_id: Any) -> bool:
    """会话是否群聊：只读 ``SessionKindCache``（未记录 ⊥ 不当群）。"""
    sid = _session_id(plugin, session_id)
    if not sid:
        return False
    companion = getattr(plugin, "_companion", None)
    kinds = getattr(companion, "session_kinds", None)
    getter = getattr(kinds, "is_group", None)
    if callable(getter):
        try:
            return getter(sid) is True
        except Exception:
            return False
    return "group" in sid.lower()


def normalize_group_id(value: Any) -> str:
    """群号归一化（复用 F1 单一真源，不在本模块自建副本）。"""
    from companion.group_attention import normalize_group_id as _normalize

    return _normalize(value)


def group_allowed(section: Any, group_id: Any) -> bool:
    """群生效集判定（复用 F1 单一真源）。"""
    from companion.group_attention import group_allowed as _allowed

    return bool(_allowed(section, group_id))


def persona_text(plugin: Any) -> str:
    """宿主人设原文（``plugin._personality`` 由 ``_refresh_persona`` 注入）。"""
    return str(getattr(plugin, "_personality", "") or "").strip()


def persona_anchor(text: Any) -> str:
    """人设回扣片段：压缩空白 + 截断；空人设 → 固定兜底句。"""
    body = " ".join(str(text or "").split())
    if not body:
        return PERSONA_ANCHOR_FALLBACK
    return body[:MAX_PERSONA_CHARS]


def persona_is_hot_tempered(text: Any) -> bool:
    """人设脾气动态判定：命中 :data:`HOT_TEMPER_MARKERS` 即视为「本就暴躁」。"""
    body = str(text or "")
    return any(marker in body for marker in HOT_TEMPER_MARKERS)


def compose_persona_bottom_line(persona: Any) -> str:
    """拼人设底线：{harshness} 按人设脾气动态选择，{persona} 回扣人设基本设定。"""
    body = str(persona or "")
    harshness = (BOUNDARY_PERSONA_HARSHNESS_ALLOWED if persona_is_hot_tempered(body)
                 else BOUNDARY_PERSONA_HARSHNESS_STRICT)
    return BOUNDARY_PERSONA_BOTTOM_LINE_DEFAULT.format(
        harshness=harshness, persona=persona_anchor(body))


def _walk_addressed(payload: Any, names: Tuple[str, ...], depth: int = 0) -> bool:
    """递归查结构化点名标志/文本 @；异常由调用方按「已点名」保守处理。"""
    if depth > 8 or payload is None:
        return False
    if isinstance(payload, dict):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in _ADDRESS_FLAG_KEYS and bool(value):
                return True
            if isinstance(value, (dict, list)):
                if _walk_addressed(value, names, depth + 1):
                    return True
            elif isinstance(value, str):
                if _ADDRESS_ATTR_PATTERN.search(value):
                    return True
                if "@" in value and any(name and name in value for name in names):
                    return True
        return False
    if isinstance(payload, (list, tuple)):
        return any(_walk_addressed(item, names, depth + 1) for item in payload)
    if isinstance(payload, str):
        if _ADDRESS_ATTR_PATTERN.search(payload):
            return True
        if "@" in payload:
            return any(name and name in payload for name in names)
    return False


def payload_is_addressed(payload: Any, names: Tuple[str, ...]) -> bool:
    """本轮载荷是否 @/点名 Bot；检测异常 → True（宁可不注入，也不猜错）。"""
    if not payload:
        return False
    try:
        return _walk_addressed(payload, tuple(n for n in names if n))
    except Exception:
        return True


def _read_life_snapshot(plugin: Any) -> Dict[str, Any]:
    """只读生活状态快照；读取异常**上抛**给 :meth:`BoundaryFacts.collect` 统一 fail-open。"""
    life = getattr(plugin, "_life_state", None)
    if life is None:
        planning = getattr(plugin, "_planning", None)
        model = getattr(planning, "life_schedule", None)
        life = getattr(model, "_life", None) if model is not None else None
    if life is None:
        return {}
    snapshot = life.snapshot()
    if not isinstance(snapshot, dict):
        raise TypeError("life snapshot 非 dict")
    return snapshot


def _active_episode(plugin: Any, now: datetime) -> Dict[str, Any]:
    """当前活动（只读；DB → runtime 读模型；异常由 collect 统一 fail-open）。"""
    db = getattr(plugin, "_db", None)
    getter = getattr(db, "active_episode", None)
    if callable(getter):
        active = getter()
        if isinstance(active, dict):
            return active
    planning = getattr(plugin, "_planning", None)
    model = getattr(planning, "life_schedule", None)
    enabled = getattr(model, "enabled", False)
    if model is not None and enabled:
        active = model.current(now, include_sleep=True)
        if isinstance(active, dict):
            return active
    return {}


@dataclass(frozen=True)
class BoundaryFacts:
    """社交边界只读事实集合（零自建副本；所有数值可为 None = 未知）。"""

    asleep: bool = False
    napping: bool = False
    silent: bool = False
    deep_work: bool = False
    exhausted: bool = False
    high_stress: bool = False
    energy: Optional[float] = None
    stress: Optional[float] = None
    focus: Optional[float] = None
    mental_state: Optional[float] = None
    activity_type: str = ""
    activity_name: str = ""
    attention_mode: str = ""
    group_id: str = ""
    is_group: bool = False
    is_target_private: bool = False
    persona_hot_tempered: bool = False
    persona_anchor: str = ""

    @property
    def busy(self) -> bool:
        """需要「晚点回」提示的忙/困/高压集合。"""
        return bool(self.deep_work or self.asleep or self.napping
                    or self.exhausted or self.high_stress)

    @classmethod
    def collect(
        cls,
        plugin: Any,
        session_id: Any,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
        now: Any = None,
    ) -> Optional["BoundaryFacts"]:
        """组合既有权威读数；任一取数异常 → ``None``（被动侧 fail-open）。"""
        try:
            stamp_dt = _now_dt(now)
            state = dict(snapshot) if isinstance(snapshot, dict) else _read_life_snapshot(plugin)
            active = _active_episode(plugin, stamp_dt)

            energy = _num(state.get("energy"))
            stress = _num(state.get("stress"))
            focus = _num(state.get("focus"))
            mental = _num(state.get("mental_state"))

            energy_threshold = _clamp(
                _cfg(plugin, "refusal_energy_threshold"), 0.0, 1.0, default=0.25)
            stress_threshold = _clamp(
                _cfg(plugin, "refusal_stress_threshold"), 0.0, 1.0, default=0.80)
            mental_floor = _clamp(
                _cfg(plugin, "refusal_mental_floor"), 0.0, 1.0, default=0.15)
            deep_types = set(_str_list(
                _cfg(plugin, "deep_work_types"), ["work", "study"]))
            deep_modes = set(_str_list(
                _cfg(plugin, "deep_work_modes"), ["brief", "later"]))

            activity_type = str(active.get("goal_type") or "").strip().lower()
            activity_name = str(active.get("name") or "").strip()
            attention_mode = str(active.get("attention_mode") or "").strip().lower()
            deep_work = activity_type in deep_types and attention_mode in deep_modes

            asleep = False
            engine = getattr(plugin, "_sleep_engine", None)
            getter = getattr(engine, "is_asleep", None)
            if callable(getter):
                asleep = getter(stamp_dt) is True

            napping = False
            gate = getattr(plugin, "companion_silence_gate", None)
            if callable(gate) and active:
                try:
                    napping = bool(gate().nap_item_matches(active))
                except Exception:
                    napping = False

            # silent = 睡眠/午休（不引入 planning 依赖；群状态行只用 asleep/napping）
            silent = bool(asleep or napping)

            body = persona_text(plugin)
            sid = _session_id(plugin, session_id)
            return cls(
                asleep=asleep,
                napping=napping,
                silent=silent,
                deep_work=deep_work,
                exhausted=((energy is not None and energy < energy_threshold)
                           or (mental is not None and mental < mental_floor)),
                high_stress=(stress is not None and stress > stress_threshold),
                energy=energy,
                stress=stress,
                focus=focus,
                mental_state=mental,
                activity_type=activity_type,
                activity_name=activity_name,
                attention_mode=attention_mode,
                group_id=normalize_group_id(sid),
                is_group=is_group_session(plugin, sid),
                is_target_private=is_target_private(plugin, sid),
                persona_hot_tempered=persona_is_hot_tempered(body),
                persona_anchor=persona_anchor(body),
            )
        except Exception:
            return None


class BoundaryService:
    """社交边界单一入口：群状态行、极端语气、wait 提示、碎片化提示。

    普通类（**不是** HookHandler / Tool / API / Command）；进程内、有界、不落盘。
    装配于组合根 ``plugin._social_boundary``；planner/replyer 经鸭子类型只读消费。
    """

    def __init__(self, plugin: Any, *, clock: Any = None) -> None:
        self._plugin = plugin
        self._clock = clock
        self._lock = threading.Lock()
        #: stream_id -> {group_id, last_direct_at, last_target_at,
        #:                last_group_line_at, last_wait_hint_at}（OrderedDict 保插入序）
        self._states: "Dict[str, Dict[str, Any]]" = {}

    # ------------------------------------------------------------------
    # 配置 / 会话判定
    # ------------------------------------------------------------------
    def enabled(self) -> bool:
        """总开关（默认关；读取失败按关处理 = 旧行为）。"""
        try:
            return bool(_cfg(self._plugin, "enabled", False))
        except Exception:
            return False

    def _now(self, value: Any = None) -> float:
        if value is not None:
            return _stamp(value)
        clock = self._clock
        try:
            return float(clock() if callable(clock) else time.time())
        except Exception:
            return time.time()

    def _bot_names(self) -> Tuple[str, ...]:
        names = [str(getattr(self._plugin, "_lover_name", "") or ""),
                 str(getattr(self._plugin, "_bot_name", "") or ""), "麦麦"]
        return tuple(dict.fromkeys(name for name in names if name))

    def _planning_section(self) -> Any:
        return getattr(getattr(self._plugin, "config", None), "planning_proactive", None)

    def _state(self, stream_id: str) -> Dict[str, Any]:
        """取（或创建）流状态；调用方须持锁。"""
        state = self._states.get(stream_id)
        if state is None:
            state = {}
            self._states[stream_id] = state
            while len(self._states) > MAX_TRACKED_STREAMS:
                self._states.pop(next(iter(self._states)))
        return state

    # ------------------------------------------------------------------
    # 入站观测（只在群钩子尾部调用；O(1)、零 IO、零 LLM）
    # ------------------------------------------------------------------
    def note_incoming(
        self,
        stream_id: Any,
        *,
        direct: bool = False,
        target_user: bool = False,
        group_id: Any = "",
        now: Any = None,
    ) -> None:
        """记录某群「刚被点名」/「目标用户刚发言」；总开关关时直接短路。"""
        if not self.enabled():
            return
        sid = str(stream_id or "").strip()
        if not sid:
            return
        try:
            stamp = self._now(now)
            gid = normalize_group_id(group_id)
            with self._lock:
                state = self._state(sid)
                if gid:
                    state["group_id"] = gid
                if direct:
                    state["last_direct_at"] = stamp
                if target_user:
                    state["last_target_at"] = stamp
        except Exception:
            return

    # ------------------------------------------------------------------
    # 事实 / 状态行
    # ------------------------------------------------------------------
    def facts(
        self,
        session_id: Any,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
        now: Any = None,
    ) -> Optional[BoundaryFacts]:
        """只读事实集合；总开关关 → ``None``（调用方不注入）。"""
        if not self.enabled():
            return None
        return BoundaryFacts.collect(
            self._plugin, session_id, snapshot=snapshot, now=now)

    def group_state_line(
        self,
        session_id: Any,
        *,
        payload: Any = None,
        now: Any = None,
    ) -> str:
        """群聊 0/1 行状态；私聊、已点名、目标用户发言、非生效集、限频未到 → 空串。"""
        if not self.enabled():
            return ""
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        try:
            facts = self.facts(sid, now=now)
            if facts is None or not facts.is_group or facts.is_target_private:
                return ""
            if not (facts.deep_work or facts.asleep or facts.napping):
                return ""
            if payload_is_addressed(payload, self._bot_names()):
                return ""
            stamp = self._now(now)
            with self._lock:
                state = self._state(sid)
                if self._recent_direct_locked(state, stamp):
                    return ""
                gid = str(state.get("group_id") or facts.group_id or "")
                if not group_allowed(self._planning_section(), gid):
                    return ""
                interval = _safe_int(
                    _cfg(self._plugin, "group_line_interval_minutes"), 1, 1440, 30) * 60.0
                last = state.get("last_group_line_at")
                if last is not None and stamp - float(last) < interval:
                    return ""
                state["last_group_line_at"] = stamp
            if facts.asleep or facts.napping:
                line = BOUNDARY_GROUP_ASLEEP_LINE_DEFAULT
                kind = "asleep"
            else:
                name = facts.activity_name or "手头的事"
                line = BOUNDARY_GROUP_DEEP_WORK_LINE_DEFAULT.format(activity=name)
                kind = "deep_work"
            self._decision("group_line", kind=kind, stream=sid)
            return line
        except Exception:
            return ""

    def _recent_direct_locked(self, state: Dict[str, Any], stamp: float) -> bool:
        if not state:
            return False
        last = max(float(state.get("last_direct_at") or 0.0),
                   float(state.get("last_target_at") or 0.0))
        return last > 0.0 and stamp - last <= ADDRESS_LOOKBACK_SECONDS

    # ------------------------------------------------------------------
    # wait 提示（只写 seconds；按流冷却）
    # ------------------------------------------------------------------
    def wait_hint_line(
        self,
        session_id: Any,
        *,
        facts: Optional[BoundaryFacts] = None,
        has_real_message: bool = True,
        now: Any = None,
    ) -> str:
        """忙/困/高压时的 wait 能力提示；只含 ``seconds``，不写 ``until``。"""
        if not self.enabled():
            return ""
        if not bool(_cfg(self._plugin, "wait_hint_enabled", True)):
            return ""
        if not has_real_message:
            return ""
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        try:
            if facts is None:
                facts = self.facts(sid, now=now)
            if facts is None or not facts.busy:
                return ""
            if not (facts.is_group or facts.is_target_private):
                return ""
            stamp = self._now(now)
            with self._lock:
                state = self._state(sid)
                cooldown = _safe_int(
                    _cfg(self._plugin, "wait_hint_cooldown_minutes"), 0, 1440, 30) * 60.0
                last = state.get("last_wait_hint_at")
                if last is not None and stamp - float(last) < cooldown:
                    return ""
                low = _safe_int(
                    _cfg(self._plugin, "wait_hint_min_seconds"), 1, 3600, 45)
                high = _safe_int(
                    _cfg(self._plugin, "wait_hint_max_seconds"), 1, 3600, 180)
                if low > high:
                    low, high = high, low
                rng = random.Random(f"{sid}|{int(stamp // 60)}|boundary_wait")
                seconds = int(rng.randint(low, high))
                state["last_wait_hint_at"] = stamp
            self._decision("wait_hint", seconds=seconds, stream=sid)
            return BOUNDARY_WAIT_HINT_DEFAULT.format(seconds=seconds)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 极端状态语气（仅目标私聊；拒绝 / 脆弱）
    # ------------------------------------------------------------------
    def extreme_tone(
        self,
        session_id: Any,
        *,
        snapshot: Optional[Dict[str, Any]] = None,
        now: Any = None,
    ) -> str:
        """目标私聊的拒绝/脆弱语气分支；非目标私聊/未命中阈值 → 空串。"""
        if not self.enabled():
            return ""
        sid = str(session_id or "").strip()
        if not sid or not is_target_private(self._plugin, sid):
            return ""
        try:
            facts = self.facts(sid, snapshot=snapshot, now=now)
            if facts is None or not (facts.exhausted or facts.high_stress):
                return ""
            tone = (BOUNDARY_TONE_REFUSAL_DEFAULT if facts.high_stress
                    else BOUNDARY_TONE_VULNERABLE_DEFAULT)
            kind = "refusal" if facts.high_stress else "vulnerable"
            self._decision(
                "tone", kind=kind, stream=sid,
                energy=facts.energy, stress=facts.stress,
                mental_state=facts.mental_state,
                hot_tempered=facts.persona_hot_tempered,
            )
            text = compose_persona_bottom_line(persona_text(self._plugin))
            return f"{tone}\n{text}"
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 碎片化 / 短回复提示（首期只有提示词层，不做真分段）
    # ------------------------------------------------------------------
    def reply_hints(self, session_id: Any, *, now: Any = None) -> List[str]:
        """目标私聊的回复节奏提示（概率命中；非目标私聊/未命中 → 空列表）。"""
        if not self.enabled():
            return []
        sid = str(session_id or "").strip()
        if not sid or not is_target_private(self._plugin, sid):
            return []
        try:
            stamp = self._now(now)
            fragment_p = _clamp(
                _cfg(self._plugin, "fragment_hint_probability"), 0.0, 1.0, default=0.20)
            short_p = _clamp(
                _cfg(self._plugin, "rhythm_short_reply_probability"), 0.0, 1.0,
                default=0.25)
            rng = random.Random(f"{sid}|{int(stamp // 60)}|boundary_rhythm")
            out: List[str] = []
            if rng.random() < fragment_p:
                out.append(BOUNDARY_FRAGMENT_HINT_DEFAULT)
            if rng.random() < short_p:
                out.append(BOUNDARY_SHORT_REPLY_HINT_DEFAULT)
            return out
        except Exception:
            return []

    # ------------------------------------------------------------------
    # 只读快照 / 留痕
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """只读状态快照（诊断/测试用）。"""
        with self._lock:
            return {sid: dict(state) for sid, state in self._states.items()}

    def _decision(self, name: str, **fields: Any) -> None:
        """决策留痕（复用 DecisionLogger；缺失/异常静默，绝不拖挂主链）。"""
        logger = getattr(self._plugin, "_decision_logger", None)
        recorder = getattr(logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(action="info", trigger_type="boundary",
                     reason=f"boundary.{name}", **fields)
        except Exception:
            return


__all__ = [
    "ADDRESS_LOOKBACK_SECONDS",
    "BoundaryFacts",
    "BoundaryService",
    "HOT_TEMPER_MARKERS",
    "MAX_PERSONA_CHARS",
    "compose_persona_bottom_line",
    "group_allowed",
    "is_group_session",
    "is_target_private",
    "normalize_group_id",
    "payload_is_addressed",
    "persona_anchor",
    "persona_is_hot_tempered",
    "persona_text",
]
