# -*- coding: utf-8 -*-
"""F1 群注意力衰减（频率调控）——进程内只读状态仓库 + 纯函数。

设计依据：``union/20261111_结构性延后项_设计方案_v2.md`` §3（F1）与
``union/20261111_实验配置迁移映射.md``（实验期配置全部集中在 ``[experimental]``）。

职责边界（只注入状态 / 调控频率 / 推动 planner，不做硬决定）：

- **不忽略消息**、不返回 abort、不写持久账本、不做群浮出、不设群日配额；
- 入站钩子只做 O(1) 追加采样（零 IO、零 LLM）；采样失败绝不影响消息处理；
- 频率乘数只经既有 ``ProactiveService._apply_frequency`` → 宿主
  ``ctx.frequency.set_adjust`` 下发；本模块不直接调宿主频率能力；
- planner 状态行由 :meth:`GroupAttentionService.build_planner_line` 生成，
  一行、只陈述事实、不写命令句；是否回复由 planner 决定；
- 睡眠完全豁免：不采样、不推进乘数、不参与 ``×0.5`` 下限；
- 安静群（宽限窗内无任何消息）目标回 1.0，不继续衰减。

默认关（``[experimental].group_attention_decay_enabled=False``）时本模块所有
入口短路：不采样、不 tick、不注入状态行，频率链与 3.17.33 逐字一致。
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import OrderedDict, deque
from datetime import datetime
from typing import Any, Deque, Dict, Iterable, List, Optional, Tuple

from core.internal_tuning import INTERNAL_TUNING_DEFAULTS
from suite_prompts import (
    GROUP_ATTENTION_HINT_MISMATCH_DEFAULT,
    GROUP_ATTENTION_HINT_QUIET_DEFAULT,
    GROUP_ATTENTION_HINT_TOPIC_DEFAULT,
)

#: 注意力乘数下限比例：下限 = 宿主当拍因子 × 0.5（相对衰减，跟随宿主活动值）
ATTENTION_FLOOR_RATIO = 0.5
#: 全局兜底下限（与 ``planning/event_catalog.py`` 的 state_affinity 同数值口径，
#: 仅用于 ``min(base, max(0.05, base×m))``；不是第二条频率通道）
ATTENTION_GLOBAL_FLOOR = 0.05
#: 缓冲少于该条数时 mismatch 按 0（避免两三条就下结论）
MIN_MISMATCH_SAMPLES = 3
#: 进程内跟踪流上限（有界，防无界增长；LRU 淘汰最旧）
MAX_TRACKED_STREAMS = 256
#: 状态行里回显的最近话题最大字数（代码层截断，避免长文本进 prompt）
TOPIC_TEXT_LIMIT = 24

#: 首期纯关键词话题表（goal_type → 关键词；零 IO、零 LLM）。
#: 清洗/无关词不在此表内；表内没有的 goal_type 视为「无法判断」→ mismatch=0。
TOPIC_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "work": ("工作", "上班", "项目", "会议", "开会", "代码", "需求", "文档", "报表",
             "客户", "老板", "加班", "周报", "deadline", "bug", "写代码", "改bug"),
    "study": ("学习", "上课", "作业", "考试", "复习", "论文", "课", "老师", "实验",
              "背单词", "刷题", "自习", "读书"),
    "learn_topic": ("兴趣", "课题", "研究", "教程", "课程", "学习"),
    "meal": ("吃", "饭", "早餐", "早饭", "午餐", "午饭", "晚餐", "晚饭", "外卖",
             "食堂", "做饭", "菜", "火锅", "奶茶", "咖啡", "零食", "宵夜"),
    "entertainment": ("游戏", "电影", "剧", "综艺", "视频", "动漫", "番", "音乐",
                      "歌", "直播", "b站", "王者", "原神", "steam", "刷视频"),
    "rest": ("睡", "困", "午休", "午睡", "休息", "躺", "放松", "小憩", "打盹"),
    "exercise": ("跑步", "健身", "运动", "锻炼", "球", "瑜伽", "散步"),
    "chores": ("家务", "打扫", "洗", "收拾", "买菜", "快递", "做饭"),
    "social": ("聊天", "朋友", "聚会", "约", "出去", "玩", "群友"),
    "daily_routine": ("起床", "洗漱", "通勤", "上班", "出门"),
    "sleep": ("睡", "晚安", "困", "睡觉"),
}

#: 无 ``[experimental]`` 段时的安全回退值（与 ``INTERNAL_TUNING_DEFAULTS`` 同源）
_GA_DEFAULTS: Dict[str, Any] = dict(INTERNAL_TUNING_DEFAULTS.get("group_attention") or {})

#: 数值宽容差（属性测试与平滑步进比较用）
_EPS = 1e-9


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    """把任意值夹取到 ``[low, high]``；非法值返回 ``default``（再夹取）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    return max(float(low), min(float(high), number))


def _normalize_group_id(value: Any) -> str:
    """群号归一化：剥离 ``group:`` / ``qq:group_`` 等前缀，取末段数字。"""
    parts = re.findall(r"\d+", str(value or ""))
    return parts[-1] if parts else ""


def activity_keywords(goal_type: Any, name: Any = "") -> Tuple[str, ...]:
    """当前活动 → 关键词元组（纯函数；未知活动返回空 = 不做话题判断）。

    关键词来自 :data:`TOPIC_KEYWORDS`（按 goal_type）与活动名切分（中文连续段
    直接作为整词，英文/数字按非字母数字切分）。零 IO、零 LLM。
    """
    key = str(goal_type or "").strip()
    words: List[str] = list(TOPIC_KEYWORDS.get(key, ()))
    raw_name = str(name or "").strip()
    if raw_name:
        for token in re.split(r"[^\w\u4e00-\u9fff]+", raw_name):
            token = token.strip()
            if len(token) >= 2 and token.lower() not in {w.lower() for w in words}:
                words.append(token)
    # 去重且保持顺序（大小写不敏感）
    seen: set = set()
    result: List[str] = []
    for word in words:
        lowered = word.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(word)
    return tuple(result)


def topic_mismatch(text: Any, activity_keywords: Iterable[str]) -> bool:
    """群消息是否与当前活动关键词都不沾边（纯关键词，首期口径）。

    - 空文本 / 无关键词 → ``False``（无法判断，不制造失配）；
    - 任一关键词出现在文本中 → ``False``（相关）；
    - 都不出现 → ``True``（失配）。
    """
    body = str(text or "").lower()
    keywords = tuple(str(w) for w in (activity_keywords or ()) if str(w).strip())
    if not body or not keywords:
        return False
    return not any(word.lower() in body for word in keywords)


def attention_target(silence: Any, mismatch: Any, *,
                     silence_weight: Any = 0.45,
                     mismatch_weight: Any = 0.55) -> float:
    """失配分数 → 目标乘数，天然落在 ``[0.5, 1.0]``。

    ``target = 1 - 0.5 × (silence_weight×silence + mismatch_weight×mismatch)``；
    权重与分项均夹取到 ``[0,1]``，合计分数再夹取到 1，保证不会跌破下限比例。
    """
    sil = _clamp(silence, 0.0, 1.0)
    mis = _clamp(mismatch, 0.0, 1.0)
    sw = _clamp(silence_weight, 0.0, 1.0)
    mw = _clamp(mismatch_weight, 0.0, 1.0)
    score = min(1.0, max(0.0, sw * sil + mw * mis))
    return 1.0 - (1.0 - ATTENTION_FLOOR_RATIO) * score


def step_toward(current: Any, target: Any, step: Any) -> float:
    """向目标移动至多 ``step``（不直接赋值；夹取在 ``[0.5, 1.0]``）。"""
    cur = _clamp(current, ATTENTION_FLOOR_RATIO, 1.0, default=1.0)
    tgt = _clamp(target, ATTENTION_FLOOR_RATIO, 1.0, default=1.0)
    max_step = max(0.0, _clamp(step, 0.0, 1.0))
    if cur < tgt:
        return min(tgt, cur + max_step)
    if cur > tgt:
        return max(tgt, cur - max_step)
    return cur


def effective_frequency_factor(base_factor: Any, multiplier: Any) -> float:
    """宿主当拍因子 × 注意力乘数 → 最终下发因子。

    ``factor = min(base, max(0.05, base × m))``（v2 §3.5 逐字口径）：
    - ``m=1.0`` → 原样返回 ``base``（默认关/无衰减逐字不变）；
    - 下限跟随宿主值：``base=0.3`` 时最低 0.15，而不是绝对 0.5；
    - ``base<0.1``（含 sleep 0.05）由全局 0.05 兜底，不会被乘到更低；
    - 非数值/NaN/Inf → 原样返回（fail-open，绝不把坏值写进频率链）。
    """
    try:
        base = float(base_factor)
    except (TypeError, ValueError):
        return base_factor  # type: ignore[return-value]
    if not math.isfinite(base):
        return base_factor  # type: ignore[return-value]
    m = _clamp(multiplier, 0.0, 1.0, default=1.0)
    if m >= 1.0 - _EPS:
        return base
    return min(base, max(ATTENTION_GLOBAL_FLOOR, base * m))


class GroupAttentionService:
    """群注意力单一入口：入站采样、60s tick、planner 状态行都经此对象。

    普通类（**不是** HookHandler / Tool / API / Command）；进程内、有界、不落盘。
    装配于组合根 ``plugin._group_attention``；规划束经鸭子类型只读消费。
    """

    def __init__(self, plugin: Any, *, clock: Any = None) -> None:
        self._plugin = plugin
        self._clock = clock
        self._lock = threading.Lock()
        #: stream_id -> 状态 dict（OrderedDict 保插入序，容量满淘汰最旧）
        self._states: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        #: 观测到过的群流（入站钩子只对群调用；用于 planner 侧群判定兜底）
        self._known_groups: set = set()
        #: 最近一拍传给 tick 的群生效集（planner 状态行只对这些流开放）
        self._active_groups: set = set()

    # ------------------------------------------------------------------
    # 配置读取（全部读 pydantic 配置对象，零文件 IO；缺失回退内置默认）
    # ------------------------------------------------------------------
    def _section(self) -> Any:
        config = getattr(self._plugin, "config", None)
        return getattr(config, "experimental", None)

    def _cfg_value(self, key: str) -> Any:
        section = self._section()
        if section is not None:
            value = getattr(section, "group_attention_" + key, None)
            if value is not None:
                return value
        return _GA_DEFAULTS.get(key)

    def enabled(self) -> bool:
        """总开关（默认关；读取失败按关处理 = 旧行为）。"""
        try:
            section = self._section()
            if section is None:
                return False
            return bool(getattr(section, "group_attention_decay_enabled", False))
        except Exception:
            return False

    def sample_size(self) -> int:
        return int(_clamp(self._cfg_value("sample_size"), 3, 200, default=20))

    def decay_window_minutes(self) -> int:
        return int(_clamp(self._cfg_value("decay_window_minutes"), 1, 1440, default=45))

    def silence_weight(self) -> float:
        return _clamp(self._cfg_value("silence_weight"), 0.0, 1.0, default=0.45)

    def mismatch_weight(self) -> float:
        return _clamp(self._cfg_value("mismatch_weight"), 0.0, 1.0, default=0.55)

    def step_per_tick(self) -> float:
        return _clamp(self._cfg_value("step_per_tick"), 0.0, 0.5, default=0.02)

    def recover_step_per_tick(self) -> float:
        return _clamp(self._cfg_value("recover_step_per_tick"), 0.0, 0.5, default=0.05)

    def planner_hint_min_multiplier(self) -> float:
        return _clamp(self._cfg_value("planner_hint_min_multiplier"), 0.0, 1.0, default=0.85)

    def planner_hint_interval_minutes(self) -> int:
        return int(_clamp(self._cfg_value("planner_hint_interval_minutes"), 1, 1440,
                          default=30))

    def quiet_group_grace_minutes(self) -> int:
        return int(_clamp(self._cfg_value("quiet_group_grace_minutes"), 1, 1440,
                          default=30))

    # ------------------------------------------------------------------
    # 时间 / 群判定 / 睡眠豁免
    # ------------------------------------------------------------------
    def _now(self, value: Any = None) -> float:
        if value is None:
            clock = self._clock
            try:
                return float(clock() if callable(clock) else time.time())
            except Exception:
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

    def _session_is_group(self, stream_id: Any) -> Optional[bool]:
        """会话是否群聊（``None`` = 未知）；只读已有缓存，不查宿主。"""
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        companion = getattr(self._plugin, "_companion", None)
        kinds = getattr(companion, "session_kinds", None)
        getter = getattr(kinds, "is_group", None)
        if callable(getter):
            try:
                value = getter(sid)
                if isinstance(value, bool):
                    return value
            except Exception:
                return None
        return None

    def _engine_asleep(self, now: float) -> bool:
        """睡眠引擎是否睡着（纯内存；供 1s 入站钩子零 IO 使用）。"""
        engine = getattr(self._plugin, "_sleep_engine", None)
        getter = getattr(engine, "is_asleep", None)
        if not callable(getter):
            return False
        try:
            return getter(datetime.fromtimestamp(now)) is True
        except Exception:
            return False

    def _group_allowed(self, group_id: Any) -> bool:
        """群是否在 ``[planning_proactive]`` 群生效集内（同步、纯内存）。

        黑名单：名单内不生效（留空 = 全部生效）；白名单：只有名单内生效
        （留空 = 全部不生效）。群号未知时：黑名单放行、白名单拒绝（无法确认）。
        """
        gid = _normalize_group_id(group_id)
        config = getattr(self._plugin, "config", None)
        section = getattr(config, "planning_proactive", None)
        if section is None:
            return False
        raw_ids = getattr(section, "proactive_group_ids", None) or []
        ids = {_normalize_group_id(item) for item in raw_ids if _normalize_group_id(item)}
        mode = str(getattr(section, "proactive_group_list_mode", "blacklist")
                   or "blacklist").strip().lower()
        if mode == "whitelist":
            return bool(gid) and gid in ids
        if not gid:
            return True
        return gid not in ids

    # ------------------------------------------------------------------
    # 状态仓库
    # ------------------------------------------------------------------
    @staticmethod
    def effective_factor(base_factor: Any, multiplier: Any) -> float:
        """频率链取数口（委托纯函数，保证下限口径唯一）。"""
        return effective_frequency_factor(base_factor, multiplier)

    def _ensure_state_locked(self, stream_id: str, now: float) -> Dict[str, Any]:
        state = self._states.get(stream_id)
        size = self.sample_size()
        if state is None:
            state = {
                "first_seen": now,
                "last_msg_at": now,
                "last_direct_at": None,
                "last_bot_reply_at": None,
                "last_tick_at": None,
                "last_hint_at": None,
                "last_log_at": None,
                "multiplier": 1.0,
                "buffer": deque(maxlen=size),
                "last_silence": 0.0,
                "last_mismatch": 0.0,
                "last_target": 1.0,
            }
            self._states[stream_id] = state
            while len(self._states) > MAX_TRACKED_STREAMS:
                self._states.popitem(last=False)
            return state
        if state["buffer"].maxlen != size:
            state["buffer"] = deque(state["buffer"], maxlen=size)
        return state

    def observe(self, stream_id: Any, text: Any, *, direct: bool = False,
                target_user: bool = False, group_id: Any = "",
                now: Any = None) -> None:
        """入站采样（只在群钩子尾部调用；O(1) 追加、零 IO、零 LLM）。

        - 总开关关 / 非群生效集 / 睡眠期 → 直接返回；
        - @/点名 或 目标用户在本群发言 → 刷新 ``last_direct_at`` 并清空失配缓冲；
        - 其余 → 环形缓冲追加 ``(ts, text)``（话题匹配留到 tick 时按当拍活动计算）。
        """
        if not self.enabled():
            return
        sid = str(stream_id or "").strip()
        if not sid:
            return
        if not self._group_allowed(group_id):
            return
        stamp = self._now(now)
        try:
            # 睡眠期冻结：不采样、不推进（醒来不跳变）。只用睡眠引擎的纯内存
            # 判定，绝不在这条 1s 钩子路径里读 runtime_current（SQLite）。
            if self._engine_asleep(stamp):
                return
            with self._lock:
                state = self._ensure_state_locked(sid, stamp)
                self._known_groups.add(sid)
                state["last_msg_at"] = stamp
                if direct or target_user:
                    state["last_direct_at"] = stamp
                    state["buffer"].clear()
                    return
                body = " ".join(str(text or "").split())
                if body:
                    state["buffer"].append((stamp, body[:TOPIC_TEXT_LIMIT * 2]))
        except Exception:
            # 采样失败绝不影响消息处理（被动侧 fail-open）
            return

    def note_bot_reply(self, stream_id: Any, *, now: Any = None) -> None:
        """记录 bot 在某群的真实回复时刻（只刷新已跟踪流；零 IO、零 LLM）。"""
        if not self.enabled():
            return
        sid = str(stream_id or "").strip()
        if not sid:
            return
        stamp = self._now(now)
        try:
            with self._lock:
                state = self._states.get(sid)
                if state is None:
                    return
                state["last_bot_reply_at"] = stamp
        except Exception:
            return

    # ------------------------------------------------------------------
    # 60s 一拍：推进乘数
    # ------------------------------------------------------------------
    def tick(self, now: Any = None, *, group_streams: Optional[Iterable[Any]] = None,
             asleep: bool = False, activity_goal_type: Any = "",
             activity_name: Any = "") -> Dict[str, float]:
        """推进并返回 ``{stream: multiplier}``；开关关/无群流 → ``{}``。

        - 每拍最多移动 ``step_per_tick``（向下）/``recover_step_per_tick``（向上），
          按经过时间比例缩放（60s 恰好一整步），不会一次撞下限；
        - 睡眠（``asleep=True`` 或引擎/日程判定睡眠）→ 冻结当前乘数，不推进；
        - 安静群（宽限窗内无任何消息/回复）→ target=1.0，逐步恢复、不继续衰减。
        """
        if not self.enabled():
            return {}
        requested = [str(sid) for sid in (group_streams or []) if str(sid or "").strip()]
        if not requested:
            return {}
        stamp = self._now(now)
        if not asleep and self._engine_asleep(stamp):
            asleep = True
        keywords = () if asleep else activity_keywords(activity_goal_type, activity_name)
        result: Dict[str, float] = {}
        logs: List[Dict[str, Any]] = []
        try:
            with self._lock:
                self._active_groups = set(requested)
                for sid in requested:
                    state = self._states.get(sid)
                    if state is None:
                        # 从未观测过的群：不凭空衰减（目标恒 1.0）
                        result[sid] = 1.0
                        continue
                    if asleep:
                        state["last_tick_at"] = stamp
                        result[sid] = float(state["multiplier"])
                        continue
                    log_entry = self._advance_locked(sid, state, stamp, keywords)
                    result[sid] = float(state["multiplier"])
                    if log_entry is not None:
                        logs.append(log_entry)
        except Exception:
            return result
        for entry in logs:
            self._decision("adjust", **entry)
        return result

    def _advance_locked(self, sid: str, state: Dict[str, Any], now: float,
                        keywords: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
        grace = self.quiet_group_grace_minutes()
        window = float(self.decay_window_minutes()) * 60.0
        first_seen = float(state.get("first_seen") or now)
        last_activity = max(
            first_seen,
            float(state.get("last_msg_at") or 0.0),
            float(state.get("last_bot_reply_at") or 0.0),
        )
        quiet = (now - last_activity) > float(grace) * 60.0
        if quiet:
            silence = 1.0
            mismatch_ratio = 0.0
            target = 1.0
        else:
            last_interest = max(
                first_seen,
                float(state.get("last_direct_at") or 0.0),
                float(state.get("last_bot_reply_at") or 0.0),
            )
            silence = _clamp((now - last_interest) / window if window > 0 else 1.0,
                             0.0, 1.0)
            buffer: Deque[Tuple[float, str]] = state["buffer"]
            if len(buffer) < MIN_MISMATCH_SAMPLES or not keywords:
                mismatch_ratio = 0.0
            else:
                mismatches = sum(1 for _ts, text in buffer
                                 if topic_mismatch(text, keywords))
                mismatch_ratio = mismatches / float(len(buffer))
            target = attention_target(
                silence, mismatch_ratio,
                silence_weight=self.silence_weight(),
                mismatch_weight=self.mismatch_weight(),
            )
        state["last_silence"] = silence
        state["last_mismatch"] = mismatch_ratio
        state["last_target"] = target

        current = _clamp(state.get("multiplier"), ATTENTION_FLOOR_RATIO, 1.0, default=1.0)
        last_tick = state.get("last_tick_at")
        elapsed = 60.0 if last_tick is None else max(0.0, now - float(last_tick))
        state["last_tick_at"] = now
        if elapsed <= 0.0:
            return None
        ratio = min(1.0, elapsed / 60.0)
        step_base = self.step_per_tick() if target < current else self.recover_step_per_tick()
        new_value = step_toward(current, target, step_base * ratio)
        state["multiplier"] = new_value
        if abs(new_value - current) < 0.005:
            return None
        last_log = state.get("last_log_at")
        if last_log is not None and now - float(last_log) < 300.0:
            return None
        state["last_log_at"] = now
        return {
            "reason": "group_attention.adjust",
            "stream": sid,
            "multiplier": round(new_value, 4),
            "target": round(target, 4),
            "silence": round(silence, 4),
            "mismatch": round(mismatch_ratio, 4),
            "quiet": quiet,
        }

    # ------------------------------------------------------------------
    # planner 状态行
    # ------------------------------------------------------------------
    def build_planner_line(self, stream_id: Any, *, now: Any = None) -> str:
        """生成一行【群聊注意力】状态；不满足条件返回空串。

        条件：开关开 ∧ 已跟踪 ∧ 该流仍是群聊 ∧ 属于群生效集 ∧
        乘数 < ``planner_hint_min_multiplier`` ∧ 距上次状态行 ≥ 间隔。一行、无命令句。
        """
        if not self.enabled():
            return ""
        sid = str(stream_id or "").strip()
        if not sid:
            return ""
        stamp = self._now(now)
        with self._lock:
            state = self._states.get(sid)
            if state is None:
                return ""
            if float(state.get("multiplier") or 1.0) >= self.planner_hint_min_multiplier():
                return ""
            last_hint = state.get("last_hint_at")
            if last_hint is not None and \
                    stamp - float(last_hint) < float(self.planner_hint_interval_minutes()) * 60.0:
                return ""
            if self._session_is_group(sid) is False:
                return ""
            if self._active_groups and sid not in self._active_groups:
                return ""
            minutes = int(max(0.0, stamp - max(
                float(state.get("first_seen") or stamp),
                float(state.get("last_direct_at") or 0.0),
                float(state.get("last_bot_reply_at") or 0.0),
            )) // 60)
            if minutes <= 0:
                return ""  # 刚被点名/刚接话：不出一行「0 分钟」
            state["last_hint_at"] = stamp
            mismatch_ratio = float(state.get("last_mismatch") or 0.0)
            topic = ""
            buffer: Deque[Tuple[float, str]] = state.get("buffer") or deque()
            for _ts, text in reversed(buffer):
                cleaned = " ".join(str(text or "").split()).strip("「」\"'")
                if cleaned:
                    topic = cleaned[:TOPIC_TEXT_LIMIT]
                    break
        if mismatch_ratio >= 0.5 and topic:
            line = GROUP_ATTENTION_HINT_TOPIC_DEFAULT.format(
                topic=topic, minutes=minutes)
        elif mismatch_ratio >= 0.5:
            line = GROUP_ATTENTION_HINT_MISMATCH_DEFAULT.format(minutes=minutes)
        else:
            line = GROUP_ATTENTION_HINT_QUIET_DEFAULT.format(minutes=minutes)
        self._decision("hint", reason="group_attention.hint", stream=sid,
                       multiplier=round(float(state.get("multiplier") or 1.0), 4))
        return line

    # ------------------------------------------------------------------
    # 只读快照 / 留痕
    # ------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """只读快照（诊断/测试用；不暴露内部 deque 本体）。"""
        with self._lock:
            return {
                sid: {
                    "multiplier": round(float(state.get("multiplier") or 1.0), 4),
                    "last_msg_at": state.get("last_msg_at"),
                    "last_direct_at": state.get("last_direct_at"),
                    "last_bot_reply_at": state.get("last_bot_reply_at"),
                    "buffer_size": len(state.get("buffer") or ()),
                    "last_silence": round(float(state.get("last_silence") or 0.0), 4),
                    "last_mismatch": round(float(state.get("last_mismatch") or 0.0), 4),
                    "last_target": round(float(state.get("last_target") or 1.0), 4),
                }
                for sid, state in self._states.items()
            }

    def _decision(self, action: str, **fields: Any) -> None:
        """决策留痕（复用 DecisionLogger；缺失/异常静默，绝不拖挂主链）。"""
        logger = getattr(self._plugin, "_decision_logger", None)
        recorder = getattr(logger, "record", None)
        if not callable(recorder):
            return
        try:
            recorder(action=action, trigger_type="group_attention", **fields)
        except Exception:
            return
