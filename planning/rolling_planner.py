"""滚动规划层 RollingPlanner（R7 依据 = 用户 2026-10-25 直派【日程系统滚动化改造，
设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，
主设计 §5 全文 + 附录 A.3/A.5/A.7/A.8 + 附录 B.4.1/B.5.2/B.6】）。

职责（主设计 §3.1）：候选生成、打分、采样、重规划节流、恢复池、随机小事件——
**全部确定性规则、0 次 LLM 调用**（红线 1）；硬约定/睡眠/吃饭锚点不可被随机采样
覆盖（红线 2）；当前活动只从 episode 日志读（红线 3）。

tick 七步（§3.2）：
    1. 取状态快照（PR2 定稿 §3/§4.1.4：planner 不再单独调用 life.tick——状态推进
       统一由 plugin tick 编排，编排方先 ``LifeStateService.tick``（含 VAD 调制）再把
       快照经 ``life_snapshot`` 传入；不传快照的旧调用由本方法自行推进，行为不变）；
    2. 关闭到期 episode（planned_end 到点 → completed）；
    3. 处理硬约定/吃饭/睡觉锚点（硬约定提前 15 分钟预警收尾、到点抢占、错过如实登记）；
    4. 当前 episode 终结判定（饥饿跨阈值时按节流提前收尾去吃饭）；
    5. 无当前 episode → 候选六源 → 打分（§5.2 公式逐字）→ top2-3 softmax 采样（温度 0.25）；
    6. 写 episode（ScheduleDB.activity_episodes）与决策日志；
    7. 发布轻量事件（life.episode_* / life.state_changed / life.random_event，附录 B.5.4，
       经 core/event_bus.publish_soon 同步上下文发布，失败静默 best-effort）。

候选六源（§5.1）：硬约定 / 锚点（下一顿饭、睡觉）/ 窗口与当前及未来 90 分钟相交的
未完成软意图（从 ``schedule_items`` 读 ``parameters.spine=true`` 行，键契约见模块
【spine 意图存储契约】）/ 恢复池 / idle 待机 / 转场随机事件。

【spine 意图存储契约（已钉死）】spine 模式生成的意图写入 schedule_items，每意图一行；
parameters JSON 列附加键：spine=true、min_minutes(int)、max_minutes(int)、
priority("high"|"medium"|"low")、hardness("hard"|"soft")、interruptibility(float 0~1)、
note(str，可选)；hardness="hard" = 硬约定（时间固定不可采样覆盖）；其余列
（name/goal_type/start_minute/end_minute/day_offset/description/priority）照旧使用。

RNG：按日期派生种子（``random.Random(f"{date}|rolling")``，可复现），随机状态不落盘、
不跨日复用；preview 用独立派生种子（读态纯函数，不消耗当日 RNG 推进）。

本模块不新增任何主动开口/主动触发入口（纪律 5：2026-10-20 §二十二口径）；
事件只发布到总线供既有订阅者消费，发布失败静默。
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.time_utils import minutes_of_now

from . import activity_profiles
from .time_utils import (DAY_MINUTES, TimezoneManager, is_in_sleep_ring,
                         normalize_day_minutes)

logger = logging.getLogger(__name__)

# ── 事件名（附录 B.5.4）────────────────────────────────────────────────────
EVENT_EPISODE_STARTED = "life.episode_started"
EVENT_EPISODE_COMPLETED = "life.episode_completed"
EVENT_EPISODE_INTERRUPTED = "life.episode_interrupted"
EVENT_EPISODE_RESUMED = "life.episode_resumed"
EVENT_EPISODE_ABANDONED = "life.episode_abandoned"
EVENT_STATE_CHANGED = "life.state_changed"
EVENT_RANDOM_EVENT = "life.random_event"

# ── 配置默认值（附录 B.4.1 [life_state]；配置段未落码时按此生效）──────────
DEFAULT_HORIZON_MINUTES = 90
DEFAULT_REPLAN_COOLDOWN_MINUTES = 20
DEFAULT_MAX_REPLANS_PER_HOUR = 2
DEFAULT_RANDOM_EVENT_RATE = 0.08
DEFAULT_MAX_RANDOM_EVENTS_PER_DAY = 3
DEFAULT_RESUME_POOL_SIZE = 3
DEFAULT_RESUME_EXPIRE_MINUTES = 240
DEFAULT_LOW_ENERGY_THRESHOLD = 0.30
DEFAULT_HIGH_HUNGER_THRESHOLD = 0.70
DEFAULT_HIGH_STRESS_THRESHOLD = 0.80
#: 低可中断活动进度 ≥ 此值 → brief（A.3 表）；硬约定/进度 ≥ late → later
DEFAULT_BRIEF_ATTENTION_PROGRESS = 0.30
DEFAULT_LATE_ATTENTION_PROGRESS = 0.70
#: top2-3 softmax 采样温度（§5.2「温度可配置；默认不要太高」）
DEFAULT_SAMPLING_TEMPERATURE = 0.25

# ── 算法常量（设计原文冻结数值）───────────────────────────────────────────
#: 打分权重（§5.2 公式逐字：1.4/1.2/1.0/0.9/0.8/0.6/0.4/-0.7/-0.8 + noise 0.10）
SCORE_WEIGHTS: Dict[str, float] = {
    "priority": 1.4, "due": 1.2, "energy": 1.0, "hunger": 0.9, "focus": 0.8,
    "resume": 0.6, "novelty": 0.4, "switch": 0.7, "overrun": 0.8,
}
SCORE_NOISE = 0.10
#: priority 分位（§5.2：硬约定 1.0、高 0.8、普通 0.5、低 0.3）
PRIORITY_SCORES: Dict[str, float] = {"hard": 1.0, "commitment": 1.0, "high": 0.8,
                                     "medium": 0.5, "normal": 0.5, "low": 0.3}
#: 恢复加成每小时衰减 ×0.85（§5.5/A.5）
RESUME_BONUS_DECAY_PER_HOUR = 0.85
#: 恢复门槛（B.6.3：bonus > 0.25 才恢复；progress < 0.2 允许不了了之）
RESUME_BONUS_FLOOR = 0.25
RESUME_MIN_PROGRESS = 0.2
#: 不了了之概率区间（§5.5「30–50% 概率不了了之」）
GIVE_UP_PROBABILITY_RANGE: Tuple[float, float] = (0.30, 0.50)
#: 硬约定提前预警/错过宽限（§5.7「硬约定临近（≤15 min）」；错过宽限保守 10 分钟）
HARD_COMMITMENT_WARN_MINUTES = 15
HARD_COMMITMENT_GRACE_MINUTES = 10
#: 状态阈值滞回（§5.7：进入 0.30 / 退出 0.40；hunger/stress 同形各错开 0.10）
HYSTERESIS_ENTER: Dict[str, float] = {"energy": 0.30, "hunger": 0.70, "stress": 0.80}
HYSTERESIS_EXIT: Dict[str, float] = {"energy": 0.40, "hunger": 0.60, "stress": 0.70}
#: 常规三餐参考窗（分钟；锚点候选仅在窗内或饥饿跨阈值时出现——保守固定值，
#: 后续可由 spine 锚点行覆盖，登记于模块 notes）
MEAL_WINDOWS: Tuple[Tuple[int, int], ...] = (
    (6 * 60 + 30, 9 * 60 + 30), (11 * 60 + 30, 13 * 60 + 30), (17 * 60 + 30, 19 * 60 + 30))
#: 同窗已有用餐 episode 的判定窗（餐后 N 分钟内不再排正餐）
MEAL_REPEAT_GUARD_MINUTES = 180
#: 各类型精力需求位（energy_fit 用：精力低于需求 → 降分；rest/idle/meal 需求最低）
ENERGY_DEMAND: Dict[str, float] = {
    "work": 0.7, "study": 0.7, "exercise": 0.6, "entertainment": 0.4,
    "social": 0.4, "daily_routine": 0.3, "meal": 0.1, "rest": 0.0,
    "idle": 0.0, "transition": 0.0, "sleep": 0.0,
}
#: 各类型专注需求位（focus_fit 用：专注高 → work/study 高，专注低 → daily/idle 高）
FOCUS_DEMAND: Dict[str, float] = {
    "work": 0.7, "study": 0.7, "exercise": 0.4, "entertainment": 0.3,
    "social": 0.3, "daily_routine": 0.2, "meal": 0.2, "rest": 0.1,
    "idle": 0.1, "transition": 0.0, "sleep": 0.0,
}
#: 注意力分档边界（A.3：≥0.85 高可中断；0.45~0.85 中可中断；<0.45 低可中断）
ATTENTION_HIGH_INTERRUPTIBLE = 0.85
ATTENTION_MID_INTERRUPTIBLE = 0.45
#: 睡眠 episode 的 goal_type（与 schedule_items / _SKIP_TYPES 口径一致）
SLEEP_GOAL_TYPE = "sleep"
#: 睡眠 episode 的可中断性（接近不可打断；A.3「开会/硬约定 0.05」同档）
SLEEP_INTERRUPTIBILITY = 0.05

#: 睡眠类型条目（与 schedule_query._SKIP_TYPES / schedule_items 夜间睡眠口径对齐）
_SKIP_SLEEP_TYPES = {SLEEP_GOAL_TYPE}

# ── 随机事件池（附录 A.7 表逐条；模板 1 句描述、0 LLM）────────────────────
RANDOM_EVENT_POOL: Tuple[Dict[str, Any], ...] = (
    {"key": "find_thing", "name": "找东西", "min_minutes": 5, "max_minutes": 15,
     "interruptibility": 0.95, "goal_type": "daily_routine",
     "state_deltas": {"focus": -0.05, "stress": 0.02},
     "templates": ("突然想不起来东西放哪了，翻了一会儿", "在找一件小东西，把抽屉翻了个遍")},
    {"key": "phone_call", "name": "接电话", "min_minutes": 5, "max_minutes": 20,
     "interruptibility": 0.90, "goal_type": "daily_routine",
     "state_deltas": {"social_battery": -0.05},
     "templates": ("来了个电话，接了一会儿", "接了个电话，说了几句")},
    {"key": "package", "name": "拿快递", "min_minutes": 5, "max_minutes": 5,
     "interruptibility": 0.95, "goal_type": "daily_routine",
     "state_deltas": {"energy": -0.01},
     "templates": ("快递到了，下去拿了一趟", "去门口取了个快递")},
    {"key": "friend_msg", "name": "朋友消息", "min_minutes": 5, "max_minutes": 10,
     "interruptibility": 0.95, "goal_type": "social",
     "state_deltas": {"social_battery": 0.05, "stress": -0.02},
     "templates": ("朋友发来消息，聊了几句", "回了朋友一条消息，闲聊了一会儿")},
    {"key": "errand", "name": "临时差事", "min_minutes": 15, "max_minutes": 30,
     "interruptibility": 0.80, "goal_type": "daily_routine",
     "state_deltas": {"energy": -0.03, "stress": 0.03},
     "templates": ("临时有件小事要处理一下", "被喊去搭了把手，花了些时间")},
)
#: 同一事件 4 小时内不重复（A.7）
RANDOM_EVENT_DEDUP_MINUTES = 240


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _as_number(value: Any, default: Optional[float] = None) -> Optional[float]:
    """宽松数值解析（bool 不算数值）；失败 → default。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


# ── spine 意图提取（存储契约唯一解析位）──────────────────────────────────
def extract_spine_intents(items: Any) -> List[Dict[str, Any]]:
    """schedule_items 行 → spine 愈图 dict 列表（只留 ``parameters.spine=true`` 行）。

    键契约见模块头【spine 意图存储契约】；缺省键保守回退
    （hardness 默认 soft、priority 默认 medium、interruptibility 默认 0.5）。
    """
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        params = item.get("parameters")
        if not isinstance(params, dict) or not params.get("spine"):
            continue
        itp = _as_number(params.get("interruptibility"), 0.5)
        out.append({
            "item_id": str(item.get("item_id") or ""),
            "name": str(item.get("name") or "未命名意图"),
            "goal_type": str(item.get("goal_type") or "daily_routine"),
            "description": str(item.get("description") or ""),
            "priority": str(params.get("priority") or item.get("priority") or "medium").strip().lower(),
            "hardness": "hard" if str(params.get("hardness") or "soft").strip().lower() == "hard" else "soft",
            "min_minutes": int(_as_number(params.get("min_minutes"), 0.0) or 0.0),
            "max_minutes": int(_as_number(params.get("max_minutes"), 0.0) or 0.0),
            "interruptibility": _clamp(itp if itp is not None else 0.5),
            "note": str(params.get("note") or ""),
            "start_minute": int(item.get("start_minute") or 0),
            "end_minute": int(item.get("end_minute") or 0),
            "day_offset": item.get("day_offset"),
        })
    return out


# ── 注意力模式派生（附录 A.3 表；唯一实现）────────────────────────────────
def derive_attention_mode(interruptibility: Any, progress: Any, *,
                          source: str = "",
                          late_progress: float = DEFAULT_LATE_ATTENTION_PROGRESS,
                          brief_progress: float = DEFAULT_BRIEF_ATTENTION_PROGRESS) -> str:
    """可中断性 + 进度 → attention_mode（now/brief/later；A.3 表逐行）。

    - 高可中断（≥0.85，吃饭/休息/娱乐/待机）→ 任意进度 ``now``；
    - 中可中断（0.45~0.85，学习/日常）→ 进度 <0.6 ``now`` / ≥0.6 ``brief``；
    - 低可中断（<0.45，写报告/开会）→ 进度 <brief_progress ``now`` / ≥brief_progress
      ``brief``；低可中断**且硬约定/关键阶段**（progress ≥ late_progress）→ ``later``。
    """
    itp = _clamp(_as_number(interruptibility, 0.5) or 0.5)
    prog = _clamp(_as_number(progress, 0.0) or 0.0)
    if itp >= ATTENTION_HIGH_INTERRUPTIBLE:
        return "now"
    if itp >= ATTENTION_MID_INTERRUPTIBLE:
        return "now" if prog < 0.6 else "brief"
    if str(source or "") == "commitment" or prog >= late_progress:
        return "later"
    return "now" if prog < brief_progress else "brief"


# ── 早晨确定性调整（设计 §4.3；0 LLM，只裁剪不重写）──────────────────────
#: 睡眠 <5h：高负荷意图（work/study）时长缩减系数（30~50% 区间内的确定性取值 40%）
SHORT_SLEEP_CUT = 0.60
#: 睡眠 5~7h：max_minutes 下调系数
MID_SLEEP_CUT = 0.85
#: 上午硬边界的钟点上限（12:00 前开始的硬约定视为「上午硬边界」）
MORNING_BOUNDARY_MAX_MINUTE = 12 * 60


def adjust_spine_for_morning(items: Any, sleep_hours: Any, wake_delay_minutes: Any,
                             now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """睡醒后对当日 spine 意图做确定性裁剪（§4.3，0 LLM；返回新列表，不改入参）。

    - 睡眠 <5h：高负荷意图（goal_type ∈ {work, study}）min/max_minutes 同缩至 60%
      （30~50% 缩短区间的保守定值）；energy 初值低由 LifeState ``handle_sleep`` 的
      short 档负责，本函数不动状态；
    - 睡眠 5~7h：max_minutes ×0.85；
    - 睡眠 ≥7h / 时长非法：不动时长；
    - 起床比锚点晚 >60 分钟：上午（start < 12:00）**硬约定**整体顺延 wake_delay 分钟
      （start/end 同移、时长不变；软意图窗口不重写——「不重写全部意图」原文）；
    - 硬约定永远保留（只顺延时间，绝不删除/取消）。

    非 spine 行原样透传；``now`` 仅留签名兼容（当前规则不依赖钟点）。
    """
    hours = _as_number(sleep_hours, None)
    delay = _as_number(wake_delay_minutes, 0.0) or 0.0
    out: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            out.append(item)
            continue
        it = dict(item)
        params = it.get("parameters")
        if not isinstance(params, dict) or not params.get("spine"):
            out.append(it)
            continue
        new_params = dict(params)
        hardness = str(new_params.get("hardness") or "soft").strip().lower()
        goal_type = str(it.get("goal_type") or "").strip().lower()
        if hardness != "hard" and hours is not None:
            if hours < 5.0 and goal_type in ("work", "study"):
                # 睡眠不足：高负荷意图缩短（min/max 同缩，保住比例语义；硬约定除外）
                for key in ("min_minutes", "max_minutes"):
                    value = _as_number(new_params.get(key), None)
                    if value is not None and value > 0:
                        new_params[key] = max(5, int(round(value * SHORT_SLEEP_CUT)))
            elif hours < 7.0:
                value = _as_number(new_params.get("max_minutes"), None)
                if value is not None and value > 0:
                    new_params["max_minutes"] = max(5, int(round(value * MID_SLEEP_CUT)))
        if hardness == "hard" and delay > 60.0:
            try:
                start = int(it.get("start_minute") or 0)
                end = int(it.get("end_minute") or 0)
            except (TypeError, ValueError):
                start, end = 0, 0
            # 仅上午硬边界顺延（跨午夜 end>1440 的「次日凌晨」段不在此列）
            if 0 <= start < MORNING_BOUNDARY_MAX_MINUTE and end > start:
                shift = int(round(delay))
                it["start_minute"] = start + shift
                it["end_minute"] = end + shift
        it["parameters"] = new_params
        out.append(it)
    return out


class RollingPlanner:
    """滚动执行规划器（0 LLM）：tick 推进、候选打分采样、恢复池、随机事件、事件发布。

    Args:
        db: :class:`~planning.schedule_db.ScheduleDB`（episode 日志 + schedule_items 读取）。
        life_state: :class:`~planning.life_state.LifeStateService`（唯一状态推进入口）。
        config_provider: 返回插件配置对象的可调用（读 ``[life_state]`` / ``[schedule]`` 段；
            段缺失时全部按附录 B.4.1 默认值生效——配置段由后续接线阶段落码）。
        event_bus: :class:`core.event_bus.EventBus`（``publish_soon`` 发布 life.* 事件；
            None = 不发布，纯静默）。
        decision_logger: 决策日志（``record(action=...)``；None = 不落账）。
    """

    def __init__(self, db: Any, life_state: Any, *, config_provider: Any = None,
                 event_bus: Any = None, timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None,
                 decision_logger: Any = None) -> None:
        self._db = db
        self._life = life_state
        self._config_provider = config_provider
        self._bus = event_bus
        self._decision_logger = decision_logger
        self._tz = timezone_manager or TimezoneManager(timezone_str)
        # 进程内运行态（不落盘；热重载即重置——与 §八.8 热重载口径一致）
        self._rng_date: str = ""
        self._rng: random.Random = random.Random()
        #: 状态阈值滞回旗标（enter/exit 双门限，§5.7）
        self._threshold_flags: Dict[str, bool] = {"energy": False, "hunger": False, "stress": False}
        #: 重规划节流：上次重规划时刻 + 本小时窗口
        self._last_replan_at: Optional[datetime] = None
        self._replan_hour: List[datetime] = []
        #: 随机事件当日计数与最近发生时刻（4h 去重）
        self._random_date: str = ""
        self._random_used: int = 0
        self._random_last: Dict[str, datetime] = {}
        #: 转场掷骰挂起旗标（进入空闲后只掷一次，防每 tick 掷骰）
        self._idle_roll_pending: bool = True
        #: 上一次 tick 观察到的日期（跨日重置当日计数）
        self._last_date: str = ""

    # ── 配置读取（段缺失 → 附录 B.4.1 默认值）────────────────────────────
    def _cfg_section(self) -> Any:
        provider = self._config_provider
        if provider is None:
            return None
        try:
            cfg = provider() if callable(provider) else provider
        except Exception:
            return None
        return getattr(cfg, "life_state", None)

    def _cfg(self, key: str, default: Any) -> Any:
        section = self._cfg_section()
        value = getattr(section, key, None)
        return default if value is None else value

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self._cfg(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        value = _as_number(self._cfg(key, None), None)
        return default if value is None else float(value)

    @property
    def enabled(self) -> bool:
        """``[life_state].enabled``（配置段未落码时恒 False——默认关纪律）。"""
        return bool(getattr(self._cfg_section(), "enabled", False))

    # ── 时间/日期助手（时区真源 = TimezoneManager）────────────────────────
    def _now(self, now: Optional[datetime]) -> datetime:
        dt = now or self._tz.get_now()
        if dt.tzinfo is None:
            tz = getattr(self._tz, "_tz", None)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt

    def _date_of(self, now: datetime) -> str:
        return now.strftime("%Y-%m-%d")

    def _day_rng(self, date: str) -> random.Random:
        """按日期派生种子的当日 RNG（可复现；不保存随机状态、不跨日复用）。"""
        if date != self._rng_date:
            self._rng_date = date
            self._rng = random.Random(f"{date}|rolling-planner")
        return self._rng

    def _sleep_anchors(self) -> Tuple[int, int]:
        """``[schedule]`` 作息锚点（sleep_time/wake_time 钟点分钟；非法回退出厂 23:00/07:00）。

        保守解析：不走 parse_hhmm 全链（本模块只取钟点分钟做环判定），解析失败回退默认。
        """
        provider = self._config_provider
        sleep_min, wake_min = 23 * 60, 7 * 60
        try:
            cfg = provider() if callable(provider) else provider
            schedule = getattr(cfg, "schedule", None)
            from core.time_utils import parse_hhmm
            s = parse_hhmm(str(getattr(schedule, "sleep_time", "") or ""), -1)
            w = parse_hhmm(str(getattr(schedule, "wake_time", "") or ""), -1)
            if s >= 0:
                sleep_min = s
            if w >= 0:
                wake_min = w
        except Exception:
            pass
        return sleep_min, wake_min

    def _in_sleep_ring(self, cur_minute: int) -> bool:
        sleep_min, wake_min = self._sleep_anchors()
        return is_in_sleep_ring(cur_minute, wake_min, sleep_min)

    # ── 事件发布（best-effort 静默）与决策日志 ───────────────────────────
    def _publish(self, event: str, **payload: Any) -> None:
        if self._bus is None:
            return
        try:
            data = dict(payload)
            # B.5.4 的 episode 事件自带 ``source``（episode 来源 spine/rolling/...）——
            # 调用方已给时保留其值（与 event_bus._prepare_payload 的保留语义一致）
            data.setdefault("source", "rolling_planner")
            self._bus.publish_soon(event, **data)
        except Exception:
            logger.debug("事件发布失败（静默）：%s", event, exc_info=True)

    def _log(self, action: str, **fields: Any) -> None:
        if self._decision_logger is None:
            return
        try:
            self._decision_logger.record(action=action, trigger_type="rolling_planner", **fields)
        except Exception:
            pass

    # ── 状态片段（episode 的 state_before/after 只存精简字段，附录 B.1.1）──
    @staticmethod
    def _state_brief(snapshot: Any) -> Dict[str, float]:
        brief: Dict[str, float] = {}
        for key in ("energy", "hunger", "focus", "stress", "social_battery"):
            value = _as_number(snapshot.get(key) if isinstance(snapshot, dict) else None, None)
            if value is not None:
                brief[key] = round(value, 4)
        return brief

    # ================================================================
    # tick（§3.2 七步）
    # ================================================================
    def resolve_profile_key(self, active_profile: Any = None,
                            current: Any = None) -> Any:
        """步 1 同款「当前活动画像」解析（只读、不推进任何状态）。

        PR2（2026-10-26），计划书 §三十四/定稿 §3/§4.1.4：状态推进统一由 plugin tick
        编排（编排方先 ``life.tick`` 再把快照经 :attr:`life_snapshot` 传入 :meth:`tick`），
        本方法把步 1 的画像解析规则（调用方显式画像优先；睡眠类型不给速率——睡眠恢复
        走 handle_sleep；表外类型不给速率）开放给编排方复用，避免规则两处漂移。
        返回 ``life.tick(active_profile=...)`` 应使用的画像键（可能为 None / dict /
        画像对象原样透传）。
        """
        if current is None:
            current = self._safe_active_episode()
        profile_key = active_profile
        if profile_key is None and current:
            profile_key = current.get("goal_type")
        if isinstance(profile_key, str) and profile_key in _SKIP_SLEEP_TYPES:
            return None  # 睡眠恢复走 handle_sleep（情绪/睡眠链），不按画像速率积分
        if isinstance(profile_key, str) and \
                activity_profiles.active_profiles().get(profile_key) is None:
            return None  # 表外类型不给速率（避免 get_profile 回退告警刷屏）
        return profile_key

    def tick(self, now: Optional[datetime] = None,
             active_profile: Any = None,
             *,
             life_snapshot: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """推进一拍（每 5 分钟由接线层调用；0 LLM）。返回本次动作报告（诊断/测试用）。

        ``active_profile``：调用方显式给定的当前活动画像（None = 自动取当前 episode
        的 goal_type 解析画像；解析不到按纯被动漂移）。
        ``life_snapshot``（PR2，2026-10-26，定稿 §3/§4.1.4；可选参数，缺省 None =
        旧行为）：plugin tick 编排已先调用 ``life.tick`` 时传入的状态快照——本方法
        **不再单独调用 life.tick**，直接消费传入快照（避免 VAD gain 与 sleep rates
        重复应用）；不传时保持原行为，由步 1 自行推进（逐字不变）。
        """
        report: Dict[str, Any] = {"actions": []}
        try:
            self._tick_inner(now, active_profile, report, life_snapshot=life_snapshot)
        except Exception as exc:
            logger.warning("rolling_planner: tick 异常（本拍跳过，不阻塞主循环）：%s", exc, exc_info=True)
            report["error"] = str(exc)
        return report

    def _tick_inner(self, now: Optional[datetime], active_profile: Any,
                    report: Dict[str, Any],
                    life_snapshot: Optional[Dict[str, Any]] = None) -> None:
        now = self._now(now)
        date = self._date_of(now)
        cur = minutes_of_now(now)
        report.update({"date": date, "cur_minute": cur})
        self._roll_daily_counters(date)

        # 步 1：状态快照（PR2 定稿 §3/§4.1.4：planner 不再单独调用 life.tick——
        # 状态推进统一由 plugin tick 编排，编排方先 life.tick（含 VAD 调制）再经
        # life_snapshot 传入；不传快照（旧调用/单测）保持原行为，由本方法推进）
        current = self._safe_active_episode()
        if life_snapshot is None:
            snapshot = self._life.tick(
                now, active_profile=self.resolve_profile_key(active_profile, current))
        else:
            snapshot = life_snapshot
        report["state"] = snapshot
        self._publish_state_changed(snapshot, now)

        # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.1/0.2：运行中 episode
        # 每拍刷新 progress（已用/计划、上限 0.95）并按新进度重算 attention_mode
        # 写回（低可中断 now→brief→later 随进度分档，A.3；derive_attention_mode
        # 本体不改，只补运行期重算与写回）
        if current is not None:
            self._refresh_active_episode(current, cur)

        # 步 2：关闭到期 episode
        if current is not None:
            try:
                planned_end = int(current.get("planned_end_minute") or 0)
            except (TypeError, ValueError):
                planned_end = 0
            if planned_end and cur >= planned_end:
                self._finish_episode(current, now, snapshot,
                                     actual_end_minute=planned_end, reason="planned_end_reached")
                report["actions"].append("closed_due_episode")
                current = None

        # 步 3：硬约定 / 吃饭 / 睡觉锚点
        self._handle_commitments(date, cur, now, snapshot, report)
        current = self._safe_active_episode()
        if current is None and self._in_sleep_ring(cur):
            self._start_sleep_episode(date, cur, now, snapshot, report)
            return  # 睡眠环内不安排任何其他活动（验收：睡眠窗内不安排主动活动）

        # 步 4：当前 episode 终结判定（饥饿跨阈值 + 节流通过 → 提前收尾去吃饭）
        current = self._safe_active_episode()
        if current is not None:
            self._maybe_force_break(current, snapshot, now, cur, date, report)

        # 步 5：无当前 → 候选六源 → 打分 → softmax 采样 → 步 6/7 写 episode 与发布
        current = self._safe_active_episode()
        if current is None and not self._in_sleep_ring(cur):
            self._select_and_start_next(date, cur, now, snapshot, report,
                                        just_transition=report["actions"] != [])
        report["active_episode_id"] = (self._safe_active_episode() or {}).get("episode_id")

    # ── 步 1.5/2/4：运行中刷新与 episode 关闭 ────────────────────────────
    def _refresh_active_episode(self, episode: Dict[str, Any], cur: int) -> None:
        """运行中 episode 的 progress / attention_mode 运行期刷新（只动 active 行；失败静默）。

        PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.1/0.2：进度 = 已用/计划
        （上限 0.95，ScheduleDB.update_active_progress 默认口径）；attention 按
        derive_attention_mode（``[life_state].brief/late_attention_progress`` 配置）
        随新进度重算，档位变化才二次写回（大多数拍次只写 progress 一行）。
        """
        eid = str(episode.get("episode_id") or "")
        if not eid:
            return
        progress = self._db.update_active_progress(eid, cur_minute=cur)
        if progress is None:
            return
        episode["progress"] = progress
        attention = derive_attention_mode(
            episode.get("interruptibility"), progress,
            source=str(episode.get("source") or ""),
            late_progress=self._cfg_float("late_attention_progress",
                                          DEFAULT_LATE_ATTENTION_PROGRESS),
            brief_progress=self._cfg_float("brief_attention_progress",
                                           DEFAULT_BRIEF_ATTENTION_PROGRESS))
        if attention == episode.get("attention_mode"):
            return
        self._db.update_active_progress(eid, attention_mode=attention)
        episode["attention_mode"] = attention

    @staticmethod
    def _random_event_deltas(metadata: Any) -> Optional[Dict[str, float]]:
        """episode 行 metadata → 随机事件 state_deltas（PR1 P0 修复（2026-10-26），
        计划书 §三十四/定稿 §2 0.4）。

        按 ``metadata.event_key`` 回查 RANDOM_EVENT_POOL（二期后由 EventCatalog.pick
        供池）取增量；返回 None = 非随机事件行 / 已应用过（``state_deltas_applied``
        持久标记，一次性保证）/ 池内无此 key——三种情况都不应用、不标记。
        close-active（被 start/resume 挤掉 → interrupted）路径本轮不应用也不标记，
        未来接线时按标记判定不会双应用。
        """
        if not isinstance(metadata, dict) or metadata.get("state_deltas_applied"):
            return None
        event_key = str(metadata.get("event_key") or "")
        if not event_key:
            return None
        for event in RANDOM_EVENT_POOL:
            if event["key"] == event_key:
                deltas = event.get("state_deltas")
                return dict(deltas) if isinstance(deltas, dict) else None
        return None

    def _finish_episode(self, episode: Dict[str, Any], now: datetime,
                        snapshot: Any, *, actual_end_minute: Optional[int] = None,
                        reason: str = "") -> None:
        eid = str(episode.get("episode_id") or "")
        end = actual_end_minute
        if end is None:
            end = episode.get("actual_end_minute") or episode.get("planned_end_minute")
        # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.4：随机事件行按
        # metadata.event_key 取 state_deltas，随关闭一并交 LifeState.apply_episode
        # （事件结束后五项 ± 增量）；应用过即写 metadata.state_deltas_applied 持久
        # 标记，重复关闭路径不会二次应用
        deltas = self._random_event_deltas(episode.get("metadata"))
        extra_meta: Optional[Dict[str, Any]] = {"close_reason": reason} if reason else None
        if deltas is not None:
            extra_meta = dict(extra_meta or {})
            extra_meta["state_deltas_applied"] = True
        if not self._db.finish(eid, actual_end_minute=end, metadata=extra_meta):
            return
        # 吃饭/休息完成 → 盖 last_meal_at / last_break_at（时长内变化由 tick 速率记账，不重复）
        try:
            payload: Dict[str, Any] = {"goal_type": episode.get("goal_type"),
                                       "status": "completed"}
            if deltas:
                payload["state_deltas"] = deltas
            self._life.apply_episode(payload, now=now)
        except Exception:
            pass
        self._publish(EVENT_EPISODE_COMPLETED, episode_id=eid,
                      name=episode.get("name"), goal_type=episode.get("goal_type"),
                      progress=episode.get("progress"),
                      actual_start_minute=episode.get("actual_start_minute"),
                      actual_end_minute=end)
        self._log("rolling.episode_finished", episode_id=eid, reason=reason,
                  name=str(episode.get("name") or ""))

    def _pause_episode(self, episode: Dict[str, Any], now: datetime,
                       *, reason: str) -> Optional[str]:
        eid = str(episode.get("episode_id") or "")
        # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.3：硬约定抢占落真实
        # 实际结束分钟（= 抢占时刻），progress 走默认口径（已用/计划、上限 0.95）——
        # 恢复池按 progress×priority 排序与 <0.2 放弃门槛（§5.5/B.6.3）才能真实命中；
        # ScheduleDB.pause「不传不虚构」的既有语义不变；原未用的 snapshot 形参顺带清理
        if not self._db.pause(eid, actual_end_minute=minutes_of_now(now),
                              metadata={"pause_reason": reason}):
            return None
        self._publish(EVENT_EPISODE_INTERRUPTED, episode_id=eid,
                      name=episode.get("name"), goal_type=episode.get("goal_type"),
                      interrupted_by=reason, progress=episode.get("progress"))
        self._log("rolling.episode_paused", episode_id=eid, reason=reason,
                  progress=_as_number(episode.get("progress"), 0.0))
        return eid

    def _maybe_force_break(self, current: Dict[str, Any], snapshot: Any, now: datetime,
                           cur: int, date: str, report: Dict[str, Any]) -> None:
        """饥饿跨阈值（>0.70，滞回退出 0.60）且当前不是吃饭 → 按节流提前收尾。"""
        hunger = _as_number(snapshot.get("hunger"), 0.0) or 0.0
        if not self._threshold_flags.get("hunger"):
            return  # 未进入饥饿阈值（滞回未触发）——正常等 planned_end
        goal_type = str(current.get("goal_type") or "")
        if goal_type in ("meal", SLEEP_GOAL_TYPE):
            return
        if str(current.get("source") or "") == "commitment":
            return  # 硬约定不可被状态阈值提前收尾（红线 2：锚点不可被覆盖）
        # 低可中断且进度高 → 不硬拽（A.3/§5.4：用户/事件都不硬打断，等收尾）
        itp = _as_number(current.get("interruptibility"), 0.5)
        progress = _as_number(current.get("progress"), 0.0)
        if (itp is not None and itp < ATTENTION_MID_INTERRUPTIBLE
                and progress is not None and progress >= 0.6):
            self._log("rolling.force_break_deferred", episode_id=current.get("episode_id"),
                      reason="low_interruptibility_high_progress")
            return
        if not self._replan_allowed(now):
            return
        self._note_replan(now)
        self._finish_episode(current, now, snapshot, reason="hunger_threshold")
        report["actions"].append("force_break_hunger")

    # ── 步 3：硬约定（提前 15 分钟预警 / 到点抢占 / 错过如实登记）─────────
    def _handle_commitments(self, date: str, cur: int, now: datetime,
                            snapshot: Any, report: Dict[str, Any]) -> None:
        intents = self._spine_intents_cached(date)
        commitments = [i for i in intents if i["hardness"] == "hard"]
        if not commitments:
            return
        recorded = self._commitment_recorded_ids(date)
        for c in commitments:
            if str(c.get("day_offset")) == "1":
                continue  # 下一午夜段不属于今天帧（§7.8 标记口径；保守跳过当日判定）
            ws, we = c["start_minute"], c["end_minute"]
            if we <= ws:
                continue
            item_key = c["item_id"]
            current = self._safe_active_episode()
            if cur >= ws and cur < we and item_key not in recorded:
                # 到点：抢占当前 episode（可抢占——硬约定优先级最高）
                if current is not None:
                    self._pause_episode(current, now, reason="hard_commitment_preempt")
                self._start_commitment(c, date, cur, now, snapshot, report)
                recorded.add(item_key)
            elif ws - HARD_COMMITMENT_WARN_MINUTES <= cur < ws and current is not None:
                # 提前 15 分钟预警：当前 episode 会吃掉约定窗 → 提前收尾（§5.7 触发 2）
                try:
                    cur_end = int(current.get("planned_end_minute") or 0)
                except (TypeError, ValueError):
                    cur_end = 0
                if cur_end > ws:
                    self._finish_episode(current, now, snapshot, actual_end_minute=ws,
                                         reason="hard_commitment_upcoming")
                    report["actions"].append("commitment_warn_wrapup")
            elif cur >= we + HARD_COMMITMENT_GRACE_MINUTES and item_key not in recorded:
                # 错过：标 broken 并记原因，不装作没发生（A.8；状态枚举无 broken →
                # 保守用 plan+abandon 落一行 abandoned，原因入 metadata.abandon_reason）
                eid = self._db.plan_episode(
                    schedule_date=date, name=c["name"], goal_type=c["goal_type"],
                    source="commitment", planned_start_minute=ws, planned_end_minute=we,
                    interruptibility=c["interruptibility"],
                    metadata={"commitment_item_id": item_key,
                              "close_reason": "missed_hard_commitment"})
                if eid:
                    self._db.abandon(eid, reason="missed_hard_commitment",
                                     metadata={"commitment_item_id": item_key})
                    self._publish(EVENT_EPISODE_ABANDONED, episode_id=eid,
                                  name=c["name"], reason="missed_hard_commitment", progress=0.0)
                    self._log("rolling.commitment_missed", episode_id=eid,
                              commitment=item_key, window=f"{ws}-{we}")
                recorded.add(item_key)

    def _commitment_recorded_ids(self, date: str) -> set:
        """今日已处理（started/abandoned）的硬约定 item_id 集合（防重复抢占/重复登记）。"""
        ids: set = set()
        try:
            rows = self._db.episodes_today(date)
        except Exception:
            return ids
        for row in rows:
            meta = row.get("metadata")
            if isinstance(meta, dict) and meta.get("commitment_item_id"):
                ids.add(str(meta["commitment_item_id"]))
        return ids

    def _start_commitment(self, intent: Dict[str, Any], date: str, cur: int,
                          now: datetime, snapshot: Any, report: Dict[str, Any]) -> None:
        """硬约定到点开播：时间固定（window 原样）、不采样覆盖（红线 2）。"""
        ws, we = intent["start_minute"], intent["end_minute"]
        eid = self._db.start_episode(
            schedule_date=date, name=intent["name"], goal_type=intent["goal_type"],
            source="commitment", planned_start_minute=ws, planned_end_minute=we,
            day_offset=intent.get("day_offset"),
            interruptibility=intent["interruptibility"], switch_cost=1.0,
            attention_mode=derive_attention_mode(
                intent["interruptibility"], 0.0, source="commitment",
                late_progress=self._cfg_float("late_attention_progress",
                                              DEFAULT_LATE_ATTENTION_PROGRESS)),
            state_before=self._state_brief(snapshot),
            metadata={"commitment_item_id": intent["item_id"], "trigger": "hard_commitment"},
        )
        if eid:
            self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=intent["name"],
                          goal_type=intent["goal_type"], planned_start_minute=ws,
                          planned_end_minute=we, source="commitment")
            self._log("rolling.commitment_started", episode_id=eid,
                      commitment=intent["item_id"], window=f"{ws}-{we}")
            report["actions"].append("commitment_started")

    # ── 睡觉锚点 ─────────────────────────────────────────────────────────
    def _start_sleep_episode(self, date: str, cur: int, now: datetime,
                             snapshot: Any, report: Dict[str, Any]) -> None:
        """睡眠环内无当前 episode → 开一条 sleep episode（锚点，不采样覆盖）。"""
        sleep_min, wake_min = self._sleep_anchors()
        remain = (normalize_day_minutes(wake_min) - normalize_day_minutes(cur)) % DAY_MINUTES
        if remain <= 0:
            remain = DAY_MINUTES
        eid = self._db.start_episode(
            schedule_date=date, name="睡觉", goal_type=SLEEP_GOAL_TYPE, source="rolling",
            planned_start_minute=cur, planned_end_minute=cur + remain,
            interruptibility=SLEEP_INTERRUPTIBILITY, switch_cost=1.0,
            attention_mode="later", state_before=self._state_brief(snapshot),
            metadata={"trigger": "sleep_anchor", "sleep_ring": f"{sleep_min}-{wake_min}"},
        )
        if eid:
            self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name="睡觉",
                          goal_type=SLEEP_GOAL_TYPE, planned_start_minute=cur,
                          planned_end_minute=cur + remain, source="rolling")
            self._log("rolling.sleep_started", episode_id=eid, planned_minutes=remain)
            report["actions"].append("sleep_started")

    # ── 重规划节流（§5.7：冷却 20 分钟 + 每小时 ≤2 次）────────────────────
    def _replan_allowed(self, now: datetime) -> bool:
        cooldown = max(0, self._cfg_int("replan_cooldown_minutes",
                                        DEFAULT_REPLAN_COOLDOWN_MINUTES))
        if self._last_replan_at is not None:
            elapsed = (now - self._last_replan_at).total_seconds() / 60.0
            if elapsed < cooldown:
                return False
        hour_ago = [t for t in self._replan_hour
                    if (now - t).total_seconds() < 3600.0]
        self._replan_hour = hour_ago
        return len(hour_ago) < max(1, self._cfg_int("max_replans_per_hour",
                                                    DEFAULT_MAX_REPLANS_PER_HOUR))

    def _note_replan(self, now: datetime) -> None:
        self._last_replan_at = now
        self._replan_hour.append(now)

    # ── 候选六源（§5.1）──────────────────────────────────────────────────
    def _spine_intents_cached(self, date: str) -> List[Dict[str, Any]]:
        try:
            items = self._db.get_day_schedule(date)
        except Exception:
            return []
        return extract_spine_intents(items)

    def _intent_done_today(self, date: str, item_id: str) -> bool:
        try:
            rows = self._db.episodes_today(date)
        except Exception:
            return False
        for row in rows:
            meta = row.get("metadata")
            if (isinstance(meta, dict) and meta.get("intent_item_id") == item_id
                    and row.get("status") in ("completed", "active")):
                return True
        return False

    def _recent_meal_today(self, date: str, cur: int) -> bool:
        """近 MEAL_REPEAT_GUARD_MINUTES 内已有用餐 episode（completed/active）→ 不重复排餐。"""
        try:
            rows = self._db.episodes_today(date)
        except Exception:
            return False
        for row in rows:
            if str(row.get("goal_type")) != "meal":
                continue
            if row.get("status") not in ("completed", "active"):
                continue
            end = row.get("actual_end_minute")
            if end is None:
                end = row.get("planned_end_minute")
            try:
                if end is not None and 0 <= cur - int(end) <= MEAL_REPEAT_GUARD_MINUTES:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _next_boundary_minutes(self, date: str, cur: int, intents: List[Dict[str, Any]]) -> Optional[int]:
        """距下一个硬边界（硬约定开始 / 睡眠环起点）的分钟数；无 → None。"""
        candidates: List[int] = []
        for c in intents:
            if c["hardness"] == "hard" and str(c.get("day_offset")) != "1":
                ws = c["start_minute"]
                if ws > cur:
                    candidates.append(ws - cur)
        sleep_min, _wake = self._sleep_anchors()
        d = (normalize_day_minutes(sleep_min) - normalize_day_minutes(cur)) % DAY_MINUTES
        if 0 < d < DAY_MINUTES:
            candidates.append(d)
        return min(candidates) if candidates else None

    def _build_candidates(self, date: str, cur: int, snapshot: Any,
                          current_profile: Optional[Any]) -> List[Dict[str, Any]]:
        """候选六源（§5.1）；返回未打分的候选 dict 列表（随机事件不走此路——转场掷骰）。"""
        horizon = max(15, self._cfg_int("horizon_minutes", DEFAULT_HORIZON_MINUTES))
        intents = self._spine_intents_cached(date)
        candidates: List[Dict[str, Any]] = []

        # 源 1：硬约定临近（start ∈ (cur, cur+horizon] 的提前入围；到点开播在 tick 步 3）
        for c in intents:
            if c["hardness"] != "hard" or str(c.get("day_offset")) == "1":
                continue
            if cur < c["start_minute"] <= cur + horizon:
                candidates.append(self._candidate_from_intent(c, kind="commitment"))

        # 源 2：锚点（下一顿饭）
        hunger = _as_number(snapshot.get("hunger"), 0.0) or 0.0
        hunger_threshold = self._cfg_float("high_hunger_threshold", DEFAULT_HIGH_HUNGER_THRESHOLD)
        in_meal_window = any(ws <= cur < we for ws, we in MEAL_WINDOWS)
        if (hunger >= hunger_threshold or in_meal_window) and not self._recent_meal_today(date, cur):
            candidates.append({
                "kind": "anchor_meal", "name": "吃饭", "goal_type": "meal",
                "priority_score": PRIORITY_SCORES["high"] if hunger >= hunger_threshold
                else PRIORITY_SCORES["medium"],
                "min_minutes": 0, "max_minutes": 0,
                "interruptibility": activity_profiles.get_profile("meal").interruptibility,
                "profile_key": "meal", "window": None, "resume_of": None,
                "resume_bonus": 0.0, "intent_item_id": None, "note": "",
            })

        # 源 3：软意图（窗口与 [cur, cur+horizon] 相交、未完成；day_offset=1 属今晚午夜段，
        # 保守只在窗口起点已过帧首后入围）
        for c in intents:
            if c["hardness"] == "hard":
                continue
            ws, we = c["start_minute"], c["end_minute"]
            if we <= ws:
                continue
            if str(c.get("day_offset")) == "1":
                continue  # 保守：午夜段不参与当日相交判定（§7.8 帧首双关，避免误排）
            if self._intent_done_today(date, c["item_id"]):
                continue
            intersects = (ws <= cur + horizon and we > cur) or (we > DAY_MINUTES and cur < we - DAY_MINUTES)
            if not intersects:
                continue
            candidates.append(self._candidate_from_intent(c, kind="intent"))

        # 源 4：恢复池
        for row in self.resume_pool():
            candidates.append({
                "kind": "resume", "name": row.get("name"), "goal_type": row.get("goal_type"),
                "priority_score": _as_number(row.get("priority_score"), 0.5) or 0.5,
                "min_minutes": 0, "max_minutes": 0,
                "interruptibility": _as_number(row.get("interruptibility"), 0.5),
                "profile_key": str(row.get("goal_type") or "daily_routine"),
                "window": None, "resume_of": row.get("episode_id"),
                "resume_bonus": _as_number(row.get("resume_bonus"), 0.0) or 0.0,
                "intent_item_id": None,
                "note": (row.get("metadata") or {}).get("note", "")
                if isinstance(row.get("metadata"), dict) else "",
            })

        # 源 5：idle 待机（永远存在的低优先候选——A.10.1 一等公民）
        idle_profile = activity_profiles.get_profile("idle")
        candidates.append({
            "kind": "idle", "name": "待机", "goal_type": "idle",
            "priority_score": PRIORITY_SCORES["low"],
            "min_minutes": 0, "max_minutes": 0,
            "interruptibility": idle_profile.interruptibility,
            "profile_key": "idle", "window": None, "resume_of": None,
            "resume_bonus": 0.0, "intent_item_id": None, "note": "",
        })
        return candidates

    @staticmethod
    def _candidate_from_intent(c: Dict[str, Any], *, kind: str) -> Dict[str, Any]:
        return {
            "kind": kind, "name": c["name"], "goal_type": c["goal_type"],
            "priority_score": PRIORITY_SCORES.get(c["priority"], PRIORITY_SCORES["medium"]),
            "min_minutes": c["min_minutes"], "max_minutes": c["max_minutes"],
            "interruptibility": c["interruptibility"],
            "profile_key": c["goal_type"], "window": (c["start_minute"], c["end_minute"]),
            "resume_of": None, "resume_bonus": 0.0,
            "intent_item_id": c["item_id"], "note": c.get("note", ""),
        }

    # ── 打分（§5.2 公式逐字）─────────────────────────────────────────────
    def _score_candidate(self, c: Dict[str, Any], snapshot: Any, cur: int,
                         boundary: Optional[int], counts: Dict[str, int],
                         current_profile: Optional[Any]) -> float:
        energy = _as_number(snapshot.get("energy"), 1.0) or 1.0
        hunger = _as_number(snapshot.get("hunger"), 0.0) or 0.0
        focus = _as_number(snapshot.get("focus"), 1.0) or 1.0
        profile = activity_profiles.get_profile(c["profile_key"])
        goal_type = c["goal_type"]

        priority = _clamp(c.get("priority_score", 0.5))
        due = self._due_urgency(c, cur)
        energy_fit = self._fit_curve(energy, ENERGY_DEMAND.get(goal_type, 0.5))
        hunger_fit = hunger if goal_type == "meal" else 0.5 * (1.0 - hunger)
        focus_fit = self._fit_curve(focus, FOCUS_DEMAND.get(goal_type, 0.4))
        resume_bonus = _clamp(c.get("resume_bonus", 0.0))
        novelty = 1.0 / (1.0 + counts.get(goal_type, 0))
        switch_cost = self._switch_cost(current_profile)
        overrun_risk = self._overrun_risk(c, profile, boundary)

        score = (SCORE_WEIGHTS["priority"] * priority
                 + SCORE_WEIGHTS["due"] * due
                 + SCORE_WEIGHTS["energy"] * energy_fit
                 + SCORE_WEIGHTS["hunger"] * hunger_fit
                 + SCORE_WEIGHTS["focus"] * focus_fit
                 + SCORE_WEIGHTS["resume"] * resume_bonus
                 + SCORE_WEIGHTS["novelty"] * novelty
                 - SCORE_WEIGHTS["switch"] * switch_cost
                 - SCORE_WEIGHTS["overrun"] * overrun_risk)
        return score

    @staticmethod
    def _fit_curve(value: float, demand: float) -> float:
        """状态贴合度：状态 ≥ 需求 → 1.0；不足则线性降（rest/idle 需求 0 → 恒 1.0）。"""
        if demand <= 0.0:
            return 1.0
        return _clamp(1.0 - max(0.0, demand - value) / demand)

    def _due_urgency(self, c: Dict[str, Any], cur: int) -> float:
        """窗口剩余越少越高；硬约定临近最高（§5.2）。"""
        if c["kind"] == "commitment":
            return 1.0
        window = c.get("window")
        if not window:
            return 0.2  # 无窗口锚点/idle/恢复：低基线
        ws, we = window
        span = max(1, we - ws)
        if cur >= we:
            return 1.0
        if cur >= ws:
            return _clamp(0.3 + 0.7 * (cur - ws) / span)
        horizon = max(15, self._cfg_int("horizon_minutes", DEFAULT_HORIZON_MINUTES))
        return _clamp(0.3 * (1.0 - (ws - cur) / horizon), 0.0, 0.3)

    def _switch_cost(self, current_profile: Optional[Any]) -> float:
        """从当前活动切走的成本（§5.2）：无当前（空闲/转场）→ 0。"""
        if current_profile is None:
            return 0.0
        return _clamp(_as_number(getattr(current_profile, "switch_cost", None), 0.0) or 0.0)

    @staticmethod
    def _overrun_risk(c: Dict[str, Any], profile: Any, boundary: Optional[int]) -> float:
        """靠近下一个硬边界时长活动降分（§5.2）：boundary 相对候选上限时长越紧风险越高。"""
        if boundary is None:
            return 0.0
        hi = _as_number(c.get("max_minutes"), None)
        if hi is None or hi <= 0:
            hi = _as_number(getattr(profile.duration, "max_minutes", None), None) or 0.0
        if hi <= 0:
            return 0.0
        return _clamp(1.0 - boundary / hi)

    # ── top2-3 softmax 加权采样（§5.2：不是 argmax）──────────────────────
    @staticmethod
    def _softmax_sample(scored: List[Tuple[float, Dict[str, Any]]], rng: random.Random,
                        temperature: float) -> Tuple[Dict[str, Any], float,
                                                     List[Tuple[float, Dict[str, Any]]]]:
        """前 2~3 名按 ``softmax(score / temperature)`` 加权采样（保留「今天就是不想写」的人味）。

        返回 ``(选中候选, 其分数, top 列表)``；top 不足 2 条时退化为单选。
        """
        top = scored[:3] if len(scored) >= 3 else scored[:2]
        if not top:
            raise ValueError("empty scored list")
        weights = [math.exp(score / max(0.01, temperature)) for score, _ in top]
        total = sum(weights)
        pick = rng.random() * total
        acc = 0.0
        for (score, cand), weight in zip(top, weights):
            acc += weight
            if pick <= acc:
                return cand, score, top
        return top[-1][1], top[-1][0], top

    # ── 时长采样 + 最小块取舍（§5.3：min_block 不足不启动——改短活动或跳过）──
    def _sample_block(self, c: Dict[str, Any], snapshot: Any, cur: int,
                      boundary: Optional[int], rng: random.Random) -> Optional[int]:
        """返回本块分钟数；剩余窗口不足最小块 → None（调用方改选下一个候选或跳过）。"""
        profile = activity_profiles.get_profile(c["profile_key"])
        window = None
        if boundary is not None:
            window = max(0, boundary)
        if c["kind"] == "resume" and c.get("resume_of"):
            # 恢复时长 = 剩余时长，不再重新采样完整块（§5.3 原文）
            row = self._db.get_episode(str(c["resume_of"]))
            if row is None:
                return None
            span = int(row.get("planned_end_minute") or 0) - int(row.get("planned_start_minute") or 0)
            progress = _as_number(row.get("progress"), 0.0) or 0.0
            remaining = max(0, int(round(span * (1.0 - min(1.0, progress))))) if span > 0 else 0
            duration = max(profile.min_block, remaining) if remaining > 0 else int(profile.min_block)
        else:
            sampled = activity_profiles.sample_duration(profile, snapshot,
                                                        remaining_window=window, rng=rng)
            duration = sampled
            lo = c.get("min_minutes") or 0
            hi = c.get("max_minutes") or 0
            if lo > 0:
                duration = max(duration, lo)
            if hi > 0:
                duration = min(duration, hi)
        if window is not None and duration > window:
            duration = window
        if duration < profile.min_block:
            return None  # 剩余时间不足最小块：不硬塞（改短活动由上层换候选 / 直接跳过）
        return int(duration)

    # ── 步 5/6/7：选下一个并开播 ─────────────────────────────────────────
    def _select_and_start_next(self, date: str, cur: int, now: datetime,
                               snapshot: Any, report: Dict[str, Any],
                               *, just_transition: bool) -> None:
        rng = self._day_rng(date)
        # 恢复池维护（过期/跨锚点/不了了之——先清理再候选）
        self._maintain_resume_pool(date, now, snapshot, report, rng)
        # 转场/空闲掷骰（§5.6：不在 tick 里每 5 分钟掷——每次进入空闲只掷一次）
        if self._idle_roll_pending or just_transition:
            self._idle_roll_pending = False
            if self._maybe_random_event(date, cur, now, snapshot, report, rng):
                return
        boundary = self._next_boundary_minutes(date, cur, self._spine_intents_cached(date))
        candidates = self._build_candidates(date, cur, snapshot, None)
        if not candidates:
            return
        counts = self._completed_counts_today(date)
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for cand in candidates:
            score = self._score_candidate(cand, snapshot, cur, boundary, counts, None)
            score += rng.uniform(-SCORE_NOISE, SCORE_NOISE)
            scored.append((score, cand))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        temperature = max(0.01, self._cfg_float("sampling_temperature",
                                                DEFAULT_SAMPLING_TEMPERATURE))
        # top2-3 softmax 加权采样（不是 argmax）；选中候选不可启（min_block 不足 /
        # 硬约定未到点）→ 从 top 余下候选按分数序改选（§5.3「改短活动或直接跳过」）
        chosen, _score, top = self._softmax_sample(scored, rng, temperature)
        top_ids = {id(c) for _s, c in top}
        order: List[Dict[str, Any]] = [c for _s, c in top]
        order += [c for _s, c in scored if id(c) not in top_ids]
        seen_ids: set = set()
        for cand in order:
            if id(cand) in seen_ids:
                continue
            seen_ids.add(id(cand))
            if cand["kind"] == "commitment" and cand["window"] and cand["window"][0] > cur:
                continue  # 硬约定时间固定：不到点不开播（tick 步 3 到点处理，红线 2）
            duration = self._sample_block(cand, snapshot, cur, boundary, rng)
            if duration is None:
                continue
            self._start_episode_for_candidate(cand, duration, date, cur, now, snapshot,
                                              scored, rng, report)
            return
        self._log("rolling.no_block_started", reason="min_block_insufficient",
                  candidates=len(scored))
        report["actions"].append("no_block_started")

    def _completed_counts_today(self, date: str) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        try:
            rows = self._db.completed_today(date)
        except Exception:
            return counts
        for row in rows:
            key = str(row.get("goal_type") or "")
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _start_episode_for_candidate(self, cand: Dict[str, Any], duration: int,
                                     date: str, cur: int, now: datetime, snapshot: Any,
                                     scored: List[Tuple[float, Dict[str, Any]]], rng: random.Random,
                                     report: Dict[str, Any]) -> None:
        profile = activity_profiles.get_profile(cand["profile_key"])
        source = {"commitment": "commitment", "intent": "rolling", "anchor_meal": "rolling",
                  "resume": "resume", "idle": "rolling"}.get(cand["kind"], "rolling")
        progress0 = 0.0
        attention = derive_attention_mode(
            cand["interruptibility"], progress0, source=source,
            late_progress=self._cfg_float("late_attention_progress",
                                          DEFAULT_LATE_ATTENTION_PROGRESS),
            brief_progress=self._cfg_float("brief_attention_progress",
                                           DEFAULT_BRIEF_ATTENTION_PROGRESS))
        top3 = [{"name": c["name"], "goal_type": c["goal_type"], "score": round(s, 4)}
                for s, c in scored[:3]]
        metadata: Dict[str, Any] = {"trigger": "rolling_tick", "top3": top3,
                                    "kind": cand["kind"],
                                    "seed_date": self._rng_date}
        if cand.get("intent_item_id"):
            metadata["intent_item_id"] = cand["intent_item_id"]
        if cand.get("note"):
            metadata["note"] = cand["note"]
        eid: Optional[str] = None
        if cand["kind"] == "resume" and cand.get("resume_of"):
            # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.4：恢复行透传原
            # 随机事件 event_key——被抢占的随机事件恢复后再关闭时仍能按 key 取
            # state_deltas（一次性应用由 metadata.state_deltas_applied 标记保证）
            origin = self._db.get_episode(str(cand["resume_of"])) or {}
            origin_meta = (origin.get("metadata")
                           if isinstance(origin.get("metadata"), dict) else {})
            if origin_meta.get("event_key"):
                metadata["event_key"] = str(origin_meta["event_key"])
            eid = self._db.resume(
                str(cand["resume_of"]),
                planned_start_minute=cur,
                planned_end_minute=cur + duration,
                actual_start_minute=cur, attention_mode=attention,
                state_before=self._state_brief(snapshot),
                metadata=metadata)
            if eid:
                self._publish(EVENT_EPISODE_RESUMED, episode_id=eid,
                              resume_of=cand["resume_of"], name=origin.get("name"),
                              goal_type=origin.get("goal_type"))
        else:
            eid = self._db.start_episode(
                schedule_date=date, name=str(cand["name"]), goal_type=str(cand["goal_type"]),
                source=source, planned_start_minute=cur, planned_end_minute=cur + duration,
                interruptibility=cand["interruptibility"], switch_cost=profile.switch_cost,
                attention_mode=attention, state_before=self._state_brief(snapshot),
                metadata=metadata,
            )
            if eid:
                self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=cand["name"],
                              goal_type=cand["goal_type"], planned_start_minute=cur,
                              planned_end_minute=cur + duration, source=source)
        if eid:
            self._log("rolling.episode_started", episode_id=eid, kind=cand["kind"],
                      name=str(cand["name"]), duration_minutes=duration, top3=top3)
            report["actions"].append(f"started_{cand['kind']}")
        else:
            logger.warning("rolling_planner: episode 写入失败（本拍不开新块）")

    # ── 恢复池维护（§5.5 / A.5 / B.6.3）─────────────────────────────────
    def resume_pool(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """恢复池候选（只读）：paused 且无恢复行，按 ``progress × priority`` 排序，≤3 条。

        每行附 ``priority_score`` / ``resume_bonus``（×0.85^小时衰减）/ ``age_minutes``。
        """
        now = self._now(now)
        pool_size = max(1, self._cfg_int("resume_pool_size", DEFAULT_RESUME_POOL_SIZE))
        expire = max(1, self._cfg_int("resume_expire_minutes", DEFAULT_RESUME_EXPIRE_MINUTES))
        try:
            rows = self._db.paused_recent(limit=pool_size * 3)
        except Exception as exc:
            logger.warning("rolling_planner: 恢复池读取失败（按空池处理）：%s", exc)
            return []
        entries: List[Dict[str, Any]] = []
        for row in rows:
            updated = self._parse_ts(row.get("updated_at"))
            age_minutes = int((now - updated).total_seconds() / 60.0) if updated else expire + 1
            progress = _as_number(row.get("progress"), 0.0) or 0.0
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            priority = PRIORITY_SCORES.get(str((meta or {}).get("priority") or "medium"),
                                           PRIORITY_SCORES["medium"])
            profile = activity_profiles.get_profile(str(row.get("goal_type") or "daily_routine"))
            age_h = age_minutes / 60.0
            bonus = _clamp((_as_number(profile.resume_bias, 0.0) or 0.0)
                           * (RESUME_BONUS_DECAY_PER_HOUR ** age_h))
            entries.append({
                **row,
                "priority_score": priority,
                "resume_bonus": bonus,
                "age_minutes": age_minutes,
            })
        entries.sort(key=lambda e: (_as_number(e["progress"], 0.0) or 0.0)
                     * (_as_number(e["priority_score"], 0.5) or 0.5), reverse=True)
        return entries[:pool_size]

    def _maintain_resume_pool(self, date: str, now: datetime, snapshot: Any,
                              report: Dict[str, Any], rng: random.Random) -> None:
        """恢复池过期/放弃维护：>4h（配置 expire）、跨过大锚点（吃饭/睡觉）、
        progress<0.2 以 30~50% 概率不了了之——放弃必须留原因（§5.5/A.5）。"""
        expire = max(1, self._cfg_int("resume_expire_minutes", DEFAULT_RESUME_EXPIRE_MINUTES))
        last_meal = self._parse_ts((snapshot or {}).get("last_meal_at"), tz=getattr(self._tz, "_tz", None))
        last_sleep = self._parse_ts((snapshot or {}).get("last_sleep_at"), tz=getattr(self._tz, "_tz", None))
        for row in self.resume_pool(now):
            eid = str(row.get("episode_id") or "")
            if not eid:
                continue
            age = _as_number(row.get("age_minutes"), 0) or 0
            updated = self._parse_ts(row.get("updated_at"), tz=getattr(self._tz, "_tz", None))
            reason: Optional[str] = None
            if age > expire:
                reason = "resume_expired"
            elif last_meal is not None and updated is not None and last_meal > updated:
                reason = "resume_crossed_meal_anchor"
            elif last_sleep is not None and updated is not None and last_sleep > updated:
                reason = "resume_crossed_sleep_anchor"
            else:
                progress = _as_number(row.get("progress"), 0.0) or 0.0
                if progress < RESUME_MIN_PROGRESS:
                    lo, hi = GIVE_UP_PROBABILITY_RANGE
                    if rng.random() < lo + (hi - lo) * rng.random():
                        reason = "give_up_low_progress"
            if reason:
                if self._db.abandon(eid, reason=reason):
                    self._publish(EVENT_EPISODE_ABANDONED, episode_id=eid,
                                  name=row.get("name"), reason=reason,
                                  progress=row.get("progress"))
                    self._log("rolling.resume_abandoned", episode_id=eid, reason=reason)
                    report["actions"].append(f"resume_abandoned:{reason}")

    @staticmethod
    def _parse_ts(raw: Any, tz: Any = None) -> Optional[datetime]:
        """ISO 文本 → datetime（naive 按 tz 补齐；非法/缺失 → None，可空降级）。"""
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            dt = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
        if dt.tzinfo is None and tz is not None:
            dt = dt.replace(tzinfo=tz)
        return dt

    # ── 随机小事件（§5.6 / A.7：模板 1 句、0 LLM）────────────────────────
    def _maybe_random_event(self, date: str, cur: int, now: datetime, snapshot: Any,
                            report: Dict[str, Any], rng: random.Random) -> bool:
        """转场/空闲掷骰；命中且约束全过 → 开一条随机事件 episode。返回是否插入。"""
        rate = self._cfg_float("random_event_rate", DEFAULT_RANDOM_EVENT_RATE)
        if rate <= 0 or rng.random() >= rate:
            return False
        if self._random_used >= max(1, self._cfg_int("max_random_events_per_day",
                                                     DEFAULT_MAX_RANDOM_EVENTS_PER_DAY)):
            return False
        eligible = [e for e in RANDOM_EVENT_POOL
                    if e["key"] not in self._random_last
                    or (now - self._random_last[e["key"]]).total_seconds()
                    >= RANDOM_EVENT_DEDUP_MINUTES * 60]
        if not eligible:
            return False  # 同事件 4h 内不重复（A.7）——池内全部冷却中
        event = rng.choice(eligible)
        duration = rng.randint(int(event["min_minutes"]), int(event["max_minutes"]))
        profile = activity_profiles.get_profile(event["goal_type"])
        template = rng.choice(event["templates"])
        eid = self._db.start_episode(
            schedule_date=date, name=event["name"], goal_type=event["goal_type"],
            source="random", planned_start_minute=cur, planned_end_minute=cur + duration,
            interruptibility=event["interruptibility"], switch_cost=profile.switch_cost,
            attention_mode="now", state_before=self._state_brief(snapshot),
            metadata={"trigger": "random_event", "event_key": event["key"],
                      "description": template},
        )
        if not eid:
            return False
        self._random_used += 1
        self._random_last[event["key"]] = now
        self._publish(EVENT_RANDOM_EVENT, name=event["name"], duration_minutes=duration,
                      source="random", description=template)
        self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=event["name"],
                      goal_type=event["goal_type"], planned_start_minute=cur,
                      planned_end_minute=cur + duration, source="random")
        self._log("rolling.random_event", episode_id=eid, event_key=event["key"],
                  duration_minutes=duration)
        report["actions"].append(f"random_event:{event['key']}")
        return True

    def _roll_daily_counters(self, date: str) -> None:
        if date != self._random_date:
            self._random_date = date
            self._random_used = 0
            self._random_last = {}
        if date != self._last_date:
            self._last_date = date
            self._idle_roll_pending = True

    # ── 状态阈值事件（滞回：只在跨越时发，非每 tick）──────────────────────
    def _publish_state_changed(self, snapshot: Any, now: datetime) -> None:
        if not isinstance(snapshot, dict):
            return
        crossed: List[Dict[str, Any]] = []
        for key, enter in HYSTERESIS_ENTER.items():
            exit_at = HYSTERESIS_EXIT[key]
            value = _as_number(snapshot.get(key), None)
            if value is None:
                continue
            above = key != "energy"  # hunger/stress 是「高」有害；energy 是「低」有害
            if above:
                entered = value > enter
                exited = value < exit_at
            else:
                entered = value < enter
                exited = value > exit_at
            flag = self._threshold_flags.get(key, False)
            if not flag and entered:
                self._threshold_flags[key] = True
                crossed.append({"field": key, "direction": "high" if above else "low",
                                "value": round(value, 4)})
            elif flag and exited:
                self._threshold_flags[key] = False
                crossed.append({"field": key, "direction": "recover",
                                "value": round(value, 4)})
        if not crossed:
            return
        self._publish(EVENT_STATE_CHANGED,
                      energy=_as_number(snapshot.get("energy"), None),
                      hunger=_as_number(snapshot.get("hunger"), None),
                      focus=_as_number(snapshot.get("focus"), None),
                      stress=_as_number(snapshot.get("stress"), None),
                      crosses=crossed)
        self._log("rolling.state_changed", crosses=crossed)

    # ── 只读查询 ─────────────────────────────────────────────────────────
    def _safe_active_episode(self) -> Optional[Dict[str, Any]]:
        try:
            return self._db.active_episode()
        except Exception as exc:
            logger.warning("rolling_planner: active_episode 读取失败：%s", exc)
            return None

    def current(self) -> Optional[Dict[str, Any]]:
        """当前 active episode（B.5.2；只读，直接透传 DB 行）。"""
        return self._safe_active_episode()

    def preview(self, now: Optional[datetime] = None, limit: int = 2) -> List[Dict[str, Any]]:
        """只读预演（B.5.2）：候选打分排序后的前 ``limit`` 条，**不落库、不消耗当日 RNG**。

        每项：``{name, goal_type, kind, score, window, priority_score, min/max_minutes,
        interruptibility, resume_of}``；睡眠环内返回睡觉锚点单项。
        """
        now = self._now(now)
        date = self._date_of(now)
        cur = minutes_of_now(now)
        snapshot = self._life.snapshot() if self._life is not None else {}
        if self._in_sleep_ring(cur):
            return [{"name": "睡觉", "goal_type": SLEEP_GOAL_TYPE, "kind": "anchor_sleep",
                     "score": 0.0, "window": None, "priority_score": 1.0,
                     "min_minutes": 0, "max_minutes": 0,
                     "interruptibility": SLEEP_INTERRUPTIBILITY, "resume_of": None}][:max(0, int(limit))]
        boundary = self._next_boundary_minutes(date, cur, self._spine_intents_cached(date))
        candidates = self._build_candidates(date, cur, snapshot, None)
        counts = self._completed_counts_today(date)
        rng = random.Random(f"{date}|preview|{cur}")
        scored = []
        for cand in candidates:
            score = self._score_candidate(cand, snapshot, cur, boundary, counts, None)
            score += rng.uniform(-SCORE_NOISE, SCORE_NOISE)
            scored.append((score, cand))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        out: List[Dict[str, Any]] = []
        for score, cand in scored[:max(0, int(limit))]:
            out.append({
                "name": cand["name"], "goal_type": cand["goal_type"], "kind": cand["kind"],
                "score": round(score, 4),
                "window": cand.get("window"),
                "priority_score": cand["priority_score"],
                "min_minutes": cand["min_minutes"], "max_minutes": cand["max_minutes"],
                "interruptibility": cand["interruptibility"],
                "resume_of": cand.get("resume_of"),
            })
        return out
