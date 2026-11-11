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
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.time_utils import minutes_of_now

from . import activity_profiles
from .activity_preferences import interest_bonus
from .circadian import nap_reason
from .goal_manager import GoalStatus
from .motives import MOTIVE_BONUS_RATIO
from .sleep_engine import resolve_hard_commitment_start
from .schedule_items import is_named_nap, is_night_sleep_item
from .time_utils import (DAY_MINUTES, TimezoneManager, is_in_sleep_ring,
                         normalize_day_minutes, ring_minutes_ahead)

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
#: D2①（定稿 §4.1.3；用户 2026-10-27 批）：极饿时吃饭候选强制最高优先级。
#: 仅在新调制 ``[life_state].emotion_modulation_enabled=true`` 时适用；不改进食窗口
#: 常规打分公式，也不覆盖睡眠环/硬约定最高层。
HIGH_HUNGER_MEAL_GUARD = 0.85
#: 常规三餐参考窗（分钟；锚点候选仅在窗内或饥饿跨阈值时出现——保守固定值，
#: 后续可由 spine 锚点行覆盖，登记于模块 notes）
MEAL_WINDOWS: Tuple[Tuple[int, int], ...] = (
    (6 * 60 + 30, 9 * 60 + 30), (11 * 60 + 30, 13 * 60 + 30), (17 * 60 + 30, 19 * 60 + 30))
#: 同窗已有用餐 episode 的判定窗（餐后 N 分钟内不再排正餐）
MEAL_REPEAT_GUARD_MINUTES = 180
#: P0（2026-11-09）：软意图有限提前弹性（分钟）。horizon 仅用于前瞻/预告/冲突判断，
#: 不默认授权把晚些时候的意图提前整段执行；确需弹性提前仅在此有限窗内。
SOFT_INTENT_EARLY_MINUTES = 10
#: P1（2026-11-09 滚动日程计划 §5/§7）：代码内置·模块联动事件——只有装配层登记的
#: 这两项能触发模块能力；仍走 _maybe_random_event 的转场掷骰+独立日预算路径。
WATCH_BILI_EVENT_ID = "watch_bili"
MODULE_LINKED_EVENT_IDS: Tuple[str, ...] = (WATCH_BILI_EVENT_ID, "browse_qq")
#: 自主刷视频（watch_bili）单次行为时长（分钟）：代码侧覆盖内置事件库的
#: 10–25 分钟抽样，不改 `planning/small_events.py` 事件定义/文案。实际控制
#: episode 的 planned_end_minute；剩余时段不足下限则本拍不启动（不静默缩到 10）。
WATCH_BILI_MIN_MINUTES = 25
WATCH_BILI_MAX_MINUTES = 55


def sample_watch_bili_duration(rng: Any, remaining_minutes: Any) -> Optional[int]:
    """在 25–55 分钟内采样一次刷视频时长；剩余时段不足 25 分钟 → None。

    ``remaining_minutes`` 为当前时段距右边界（08:00/16:00/24:00）的整分钟数；
    上界按剩余时段夹取（仍落在 25–55 内），不越过时段边界。
    """
    try:
        remaining = int(remaining_minutes)
    except (TypeError, ValueError):
        remaining = 0
    if remaining < WATCH_BILI_MIN_MINUTES:
        return None
    upper = min(WATCH_BILI_MAX_MINUTES, remaining)
    return int(rng.randint(WATCH_BILI_MIN_MINUTES, upper))


#: P1 §5.2 来源组平衡：同一来源/用途组内第 k 个候选的递减因子（防 30 个近义 JSON
#: 项挤满 top3 饿死日意图）；组间基础权重见 EVENT_ORIGIN_PRIORS。
EVENT_GROUP_PRESENCE_DECAY = 0.85
#: P1 §5.2：来源组基础先验（初始 JSON 状态事件 / 生成池）。低于日意图 medium=0.5
#: 的满权重 1.0 口径，保证临近且可执行的日意图保留合理优势；一次性另乘 pacing。
EVENT_ORIGIN_PRIORS: Dict[str, float] = {
    "json_file": 0.85, "code_builtin": 0.85, "llm_generated": 0.80,
}
#: P1 §5.3：存在可行非 idle 候选时，累计待机超过此分钟后开始递增降权 idle。
IDLE_PENALTY_START_MINUTES = 10.0
#: 每多待机 5 分钟追加的降权（线性递增，上限 IDLE_PENALTY_MAX）。
IDLE_PENALTY_PER_5_MIN = 0.15
IDLE_PENALTY_MAX = 0.9
#: 2.1（2026-11-11 Gemini 批）：idle 的**对外口径**。内部仍按 idle 调度（kind /
#: goal_type / profile_key 不变，打分与采样结构零改动），只把写进 episode 的名称与
#: 抽取原因换成具体生活细节——聊天/详情/预演不再出现「待机」这类系统词。
IDLE_DISPLAY_DETAILS: Tuple[str, ...] = (
    "伸了个懒腰", "对着窗外看了一会儿", "放着歌发了会儿呆",
    "靠在椅背上放空了一会儿", "揉了揉眼睛", "起身走了两步",
)


def idle_display_name(date: Any, minute: Any) -> str:
    """idle 对外生活细节（确定性映射，2.1）。

    只以 ``date|minute`` 派生独立 ``random.Random`` 种子，**不消耗当日 RNG、
    不参与打分/采样**，因此同一帧重复调用结果一致、调度轨迹与改动前逐字相同。
    任何异常回退第一项，绝不阻塞开播。
    """
    try:
        seed = f"{date}|{int(minute)}|idle_display"
        return IDLE_DISPLAY_DETAILS[random.Random(seed).randrange(len(IDLE_DISPLAY_DETAILS))]
    except (TypeError, ValueError, IndexError):
        return IDLE_DISPLAY_DETAILS[0]


#: P3 §14.6：偏好只施加给可行软候选（硬约定/生理锚点/睡眠/idle 不参与偏好竞争）。
PREFERENCE_SOFT_KINDS = frozenset({"intent", "event", "resume", "continuation", "anchor_rest"})
#: P3 §14.6：普通候选约 10% 受控探索（在合法替代项中；不绕过时间/状态/次数限制）。
EXPLORATION_RATE = 0.10
#: 复核 P2-2：探索只在 top 之后至多 5 个替代项中抽（scored[3:8]），避免盲抽长尾劣质项。
EXPLORATION_WINDOW = 5
#: 复核 P2-2：探索候选与 top1 分差不得超过该值，否则视为劣质替代不参与探索。
EXPLORATION_SCORE_GAP = 0.25
#: P3 §14.6：偏好时长微调——采样中心移动不超过区间宽度的 15%（不扩展 min/max）。
PREFERENCE_DURATION_SHIFT_RATIO = 0.15
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
#: 睡眠 episode 的 goal_type（与 schedule_items / schedule_query.SKIP_TYPES 口径一致）
SLEEP_GOAL_TYPE = "sleep"
#: 睡眠 episode 的可中断性（接近不可打断；A.3「开会/硬约定 0.05」同档）
SLEEP_INTERRUPTIBILITY = 0.05

# ── 活动三分类（PR-B2，2026-11-07；计划书 §5.1 / 规格 §1）────────────────
#: 确定性规则先行，LLM 不参与分类；拿不准 → 无延续价值（保守）。
#: 明确承诺（hardness=hard / source=commitment）由既有硬约定机制处理，不进分类器。
#: 类型直判「无延续价值」：结束即结束（发呆/休息/吃饭/睡觉/转场/随手社交娱乐）。
CONTINUITY_NONE_GOAL_TYPES = frozenset({
    "rest", "idle", "meal", "sleep", "transition", "social", "entertainment",
})
#: 名称关键词直判「无延续价值」（优先于类型/延续关键词，保守否决）
CONTINUITY_NONE_KEYWORDS = ("发呆", "零食", "刷QQ", "刷qq", "随手", "躺一会",
                            "待机", "打盹", "闲逛")
#: 类型直判「可能延续」：工作/学习天然有未完成价值
CONTINUITY_TYPE_GOAL_TYPES = frozenset({"work", "study"})
#: 名称关键词判「可能延续」（收拾收藏夹/研究新话题/整理图片等；可经注入补充）
CONTINUITY_KEYWORDS = ("整理", "收拾", "收藏", "研究", "学习", "练习", "写作",
                       "规划", "清理", "修复", "搭建", "阅读", "复盘", "准备",
                       "剪辑", "写")


def classify_activity_continuity(name: Any, goal_type: Any, *,
                                 extra_keywords: Any = ()) -> str:
    """活动三分类的确定性判定（PR-B2 规格 §1；返回 ``none`` / ``continuable``）。

    判定顺序（确定性）：无延续价值关键词（保守否决）→ 延续关键词（规格 §1
    例：研究新话题/收拾收藏夹，即使类型是娱乐/日常）→ 无延续价值类型 →
    work/study 类型 → 其余拿不准 ``none``（保守缺省）。明确承诺不走本函数
    （既有机制不动）。``extra_keywords``：可配置补充的延续关键词（注入式；
    非法条目忽略）。
    """
    text = str(name or "")
    gtype = str(goal_type or "").strip().lower()
    if any(kw in text for kw in CONTINUITY_NONE_KEYWORDS):
        return "none"
    keywords = list(CONTINUITY_KEYWORDS)
    for kw in extra_keywords or ():
        if isinstance(kw, str) and kw.strip():
            keywords.append(kw.strip())
    if any(kw in text for kw in keywords):
        return "continuable"
    if gtype in CONTINUITY_NONE_GOAL_TYPES:
        return "none"
    if gtype in CONTINUITY_TYPE_GOAL_TYPES:
        return "continuable"
    return "none"

#: 睡眠类型条目（与 schedule_query.SKIP_TYPES / schedule_items 夜间睡眠口径对齐）
_SKIP_SLEEP_TYPES = {SLEEP_GOAL_TYPE}

# ── 随机事件池（P2 起仅代码联动两项；普通状态事件在 mappings/initial_events/）──
from .small_events import LEGACY_STATE_EVENTS, builtin_events
RANDOM_EVENT_POOL: Tuple[Dict[str, Any], ...] = tuple(builtin_events())

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
            "commitment_hardness": str(params.get("commitment_hardness") or "").strip().lower(),
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
    - **固定时间 hard 约定永不平移**（用户 2026-10-27 最终裁定：晚起不挪约定，迟到如实、
      错过则 abandoned；只调整软意图；睡眠锚点按真实会话处理）；
    - 硬约定永远保留（不删除/不取消，时间原样）。

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
                 decision_logger: Any = None,
                 sleep_engine: Any = None,
                 schedule_writer: Optional[Callable[..., Any]] = None,
                 event_catalog: Any = None,
                 wake_recorder: Optional[Callable[..., Any]] = None,
                 goal_manager: Any = None,
                 motive_store: Any = None,
                 continuity_keywords_provider: Optional[Callable[[], Any]] = None,
                 data_dir: Any = None,
                 activity_preferences: Any = None) -> None:
        self._db = db
        self._life = life_state
        # P1（2026-11-09）：事件冷却/当日用量持久目录（None = 仅进程内，测试桩口径）
        self._data_dir = data_dir
        # P3（2026-11-09 §14）：日程偏好只读端口（None = 未装配 → 偏好分支整体
        # 旁路，旧行为逐字不变）；planner 绝不通过它发请求。
        self._activity_preferences = activity_preferences
        self._config_provider = config_provider
        self._bus = event_bus
        self._decision_logger = decision_logger
        # PR3（2026-10-26）：睡眠引擎（可后注入；未启用 → 全部新分支旁路）
        self._sleep_engine = sleep_engine
        # PR4（2026-10-27 共享接线）：事件库服务（唯一实例由 PlanningServices 持有）。
        # None = 未装配/装配失败 → 随机事件逐字回退旧 RANDOM_EVENT_POOL 路径。
        self._event_catalog = event_catalog
        # PR-B2（2026-11-07 跨日事务 + 持续动机）：GoalManager（唯一目标表，不造
        # 第二个目标表）与 MotiveStore（只改候选权重）。None = 未装配 → 全部新分支
        # 旁路，旧行为逐字不变。continuity_keywords_provider = 分类关键词可配置补充。
        self._goal_manager = goal_manager
        self._motive_store = motive_store
        self._continuity_keywords_provider = continuity_keywords_provider
        # D9 晨间 spine 调整写回（插件注入 generator.apply_schedule_items 鸭子口；
        # 缺失 → 只返回调整结果不落库，旧行为零变化）
        self._schedule_writer = schedule_writer
        #: 真实醒来记录口（插件注入 plugin._record_actual_wake_cycle；硬约定 force_wake
        #: 不在 plugin tick 的 commit 分支内，需由 planner 成功后回调，避免漏记周期起点）
        self._wake_recorder = wake_recorder
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
        self._nap_skipped_date: str = ""
        self._nap_date = ""
        self._nap_reason = ""

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
        """``[schedule].enabled``（PR8 3.0.0 起唯一日程总开关；配置段未落码时恒 False
        ——默认关纪律。依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP1，
        旧 ``[life_state].enabled`` 退役）。"""
        provider = self._config_provider
        if provider is None:
            return False
        try:
            cfg = provider() if callable(provider) else provider
        except Exception:
            return False
        return bool(getattr(getattr(cfg, "schedule", None), "enabled", False))


    # ── 睡眠引擎接线（PR3，2026-10-26）─────────────────────────────────
    def set_sleep_engine(self, engine: Any) -> None:
        """后注入睡眠引擎（plugin 先建 planner 再建 engine 时用；幂等）。"""
        self._sleep_engine = engine

    @property
    def dynamic_sleep(self) -> bool:
        return bool(getattr(self._sleep_engine, "dynamic_enabled", False))

    def _engine_enabled(self) -> bool:
        engine = self._sleep_engine
        if engine is None:
            return False
        enabled = getattr(engine, "enabled", None)
        try:
            return bool(enabled() if callable(enabled) else enabled)
        except Exception:
            return False

    def _engine_owns_sleep(self, episode: Any) -> bool:
        """睡眠 episode 的 planned_end 自动关闭是否交给引擎（引擎启用且是 sleep）。"""
        if not self._engine_enabled():
            return False
        return str((episode or {}).get("goal_type") or "") == SLEEP_GOAL_TYPE

    def _engine_allows_sleep_start(self, now: datetime) -> bool:
        """引擎闸：本夜已睡/已醒（含硬约定早醒）→ 不再开新 sleep episode。"""
        engine = self._sleep_engine
        gate = getattr(engine, "should_start_sleep_episode", None)
        if not callable(gate):
            return True
        try:
            outcome = gate(now)
        except Exception:
            return True
        return outcome is not False

    def sleep_hard_commitment_start(self, now: Optional[datetime] = None
                                    ) -> Optional[datetime]:
        """睡眠引擎的硬约定开始时刻（**真实公共链路**：今日帧 + 次日帧，含已进窗）。

        引擎 ``prepare_tick`` 需要绝对 ``hard_commitment_start`` 才能做「约定 − lead」正式
        起床判定；此前 plugin 未接线 → 生产路径硬约定永不触发（只有单测手动喂）。
        ``include_active=True``：已进入约定窗口（睡着跨过约定开始）也算 → 立即正式起床，
        不虚构准时。纯读 DB（0 LLM），无硬约定 → None。
        """
        current = self._now(now)
        frame = self._date_of(current)
        dates = [frame]
        dynamic = self.dynamic_sleep
        if dynamic:
            dates.insert(0, self._date_of(current - timedelta(days=1)))
        try:
            dates.append((datetime.strptime(frame, "%Y-%m-%d") + timedelta(days=1))
                         .strftime("%Y-%m-%d"))
        except (TypeError, ValueError):
            pass
        best: Optional[datetime] = None
        for date in dates:
            intents = self._spine_intents_cached(date)
            if dynamic:
                intents = [{**item, "goal_type": "rest"} if is_named_nap(item) else item
                           for item in intents]
            resolved = resolve_hard_commitment_start(
                intents, date, current, include_active=True, allow_midnight=dynamic)
            if resolved is not None and (best is None or resolved < best):
                best = resolved
        return best

    def _note_actual_wake(self, now: datetime) -> None:
        """硬约定正式起床后同步周期起点（best-effort；回调缺失 = 旧行为）。"""
        if self._wake_recorder is None:
            return
        try:
            self._wake_recorder(now)
        except Exception:
            logger.debug("真实醒来周期记录失败（忽略）", exc_info=True)

    def _engine_wake_for_commitment(self, now: datetime) -> bool:
        """硬约定正式起床：交给睡眠引擎完成/关闭会话（不 paused、不恢复同一活动）。"""
        engine = self._sleep_engine
        force = getattr(engine, "force_wake", None)
        if not callable(force):
            return False
        try:
            outcome = force(now, reason="hard_commitment")
        except Exception:
            return False
        return bool(isinstance(outcome, dict) and outcome.get("woke"))

    @staticmethod
    def _episode_frame_minute(episode: Any, now: datetime) -> int:
        """episode 所属日程日帧的分钟（跨午夜 >1440，与 schedule_items 同口径）。

        睡 episode 的 ``schedule_date`` 是入睡日；晚醒时刻落到次日，不能直接用
        ``minutes_of_now``（会写成 08:32 而非 32:32）——「严格记录真实 wake 时间」。
        """
        date = str((episode or {}).get("schedule_date") or "")
        try:
            midnight = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=now.tzinfo)
        except (TypeError, ValueError):
            return minutes_of_now(now)
        return int((now - midnight).total_seconds() // 60)

    def _lie_in_cap_minute(self) -> int:
        """``[schedule_advanced].lie_in_max_until`` 分钟（非法回退 12:00）。"""
        provider = self._config_provider
        try:
            cfg = provider() if callable(provider) else provider
            advanced = getattr(cfg, "schedule_advanced", None)
            from core.time_utils import parse_hhmm
            minute = parse_hhmm(str(getattr(advanced, "lie_in_max_until", "") or ""), -1)
            if minute >= 0:
                return minute
        except Exception:
            pass
        return 12 * 60

    @staticmethod
    def _planned_sleep_end(cur: int, wake_min: int, cap_minute: int,
                           engine_enabled: bool) -> int:
        """sleep episode 的 planned_end：引擎启用 → lie-in 上限（安全末端），否则醒锚点。"""
        remain = (normalize_day_minutes(wake_min) - normalize_day_minutes(cur)) % DAY_MINUTES
        if remain <= 0:
            remain = DAY_MINUTES
        wake_abs = cur + remain
        if not engine_enabled:
            return wake_abs
        cap = wake_abs - (wake_abs % DAY_MINUTES) + normalize_day_minutes(cap_minute)
        while cap <= wake_abs:
            cap += DAY_MINUTES
        return cap

    def close_sleep_episode(self, episode_id: str, actual_end_minute: Any,
                            reason: str = "") -> bool:
        """引擎醒沿回调：关闭 sleep episode（写 actual_end、发事件、状态回写各一次）。"""
        try:
            row = self._db.get_episode(str(episode_id))
        except Exception:
            return False
        if not isinstance(row, dict) or row.get("status") != "active":
            return False
        try:
            end = int(actual_end_minute)
        except (TypeError, ValueError):
            end = None
        snapshot = self._state_brief(self._life.snapshot()
                                     if callable(getattr(self._life, "snapshot", None))
                                     else {})
        self._finish_episode(row, self._now(None), snapshot,
                             actual_end_minute=end,
                             reason=f"sleep_engine_{reason}" if reason else "sleep_engine")
        return True

    def apply_morning_spine_adjustment(self, now: Optional[datetime] = None, *,
                                       sleep_hours: Any, wake_delay_minutes: Any = 0,
                                       date: Optional[str] = None) -> List[Dict[str, Any]]:
        """D9：按实际睡醒时长/延迟裁剪今日 spine 并**写回**（0 LLM；仅一次）。

        读今日日程 → :func:`adjust_spine_for_morning`（纯函数）→ 有变化才经注入的
        ``schedule_writer(items, date, reason)`` 写回（generator.apply_schedule_items，
        consume_commitments=False）。未注入 writer/读不到日程 → 只返回调整结果。
        """
        current = self._now(now)
        day = date or self._date_of(current)
        try:
            items = [dict(row) for row in (self._db.get_day_schedule(day) or [])
                     if isinstance(row, dict)]
        except Exception:
            return []
        if not items:
            return []
        adjusted = adjust_spine_for_morning(items, sleep_hours, wake_delay_minutes, current)
        changed = any(a != b for a, b in zip(adjusted, items))
        if not changed or self._schedule_writer is None:
            return adjusted
        try:
            self._schedule_writer(adjusted, day, "morning_wake_adjust")
        except TypeError:
            try:
                self._schedule_writer(adjusted, day)
            except Exception:
                pass
        except Exception:
            pass
        self._log("rolling.morning_adjusted", sleep_hours=_as_number(sleep_hours, 0.0),
                  wake_delay_minutes=_as_number(wake_delay_minutes, 0.0), date=day)
        return adjusted

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
        本方法把步 1 的画像解析规则（调用方显式画像优先；睡眠类型不给速率——
        PR2.5 订正（2026-10-26）：PR3 SleepEngine 接线前睡眠状态走被动漂移，
        ``handle_sleep`` 暂未接线；表外类型不给速率）开放给编排方复用，避免规则两处漂移。
        返回 ``life.tick(active_profile=...)`` 应使用的画像键（可能为 None / dict /
        画像对象原样透传）。
        """
        if current is None:
            current = self._safe_active_episode()
        profile_key = active_profile
        if profile_key is None and current:
            profile_key = current.get("goal_type")
        if isinstance(profile_key, str) and profile_key in _SKIP_SLEEP_TYPES:
            return None  # 睡眠类型不按画像速率积分（PR2.5 订正：handle_sleep 暂未接线，
                         # PR3 SleepEngine 接线前睡眠恢复走被动漂移）
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

        # 步 2：关闭到期 episode（引擎启用的 sleep episode 交给 SleepEngine 决定
        # actual_end——PR3，2026-10-26：不再按 wake 锚点自动关）
        if current is not None:
            try:
                planned_end = int(current.get("planned_end_minute") or 0)
            except (TypeError, ValueError):
                planned_end = 0
            # 跨午夜口径（2026-10-28 真机缺陷修复）：planned_end 按 episode 自身日帧
            # 存储（>1440 = 次日），关闭比较必须用同一日帧的当前分钟。裸 cur 在次日
            # 回绕 0~1439 后对跨午夜 planned_end（如前日 23:42 idle、end=1454）永假
            # → 步 2 永不关闭，步 3 睡眠锚点（要求无当前 episode）与步 5 选活动被
            # 无限阻塞（真机实测卡 391 分钟、今日 episode=0，重启不解）。
            cur_frame = self._episode_frame_minute(current, now)
            if planned_end and cur_frame >= planned_end \
                    and not self._engine_owns_sleep(current):
                self._finish_episode(current, now, snapshot,
                                     actual_end_minute=planned_end, reason="planned_end_reached")
                report["actions"].append("closed_due_episode")
                current = None

        # 步 3：硬约定 / 吃饭 / 睡觉锚点
        self._handle_commitments(date, cur, now, snapshot, report)
        current = self._safe_active_episode()
        dynamic = self.dynamic_sleep
        if dynamic:
            if self._try_dynamic_sleep(date, cur, now, snapshot, current, report):
                return
            if self._roll_nap(date, cur, now, snapshot, current, report):
                return
        elif current is None and self._in_sleep_ring(cur):
            self._start_sleep_episode(date, cur, now, snapshot, report)
            return  # 睡眠环内不安排任何其他活动（验收：睡眠窗内不安排主动活动）

        # 步 4：当前 episode 终结判定（饥饿跨阈值 + 节流通过 → 提前收尾去吃饭）
        current = self._safe_active_episode()
        if current is not None:
            self._maybe_force_break(current, snapshot, now, cur, date, report)

        # 步 5：无当前 → 候选六源 → 打分 → softmax 采样 → 步 6/7 写 episode 与发布
        current = self._safe_active_episode()
        if current is None and (dynamic or not self._in_sleep_ring(cur)):
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
    def _random_event_deltas(metadata: Any, event_catalog: Any = None
                             ) -> Optional[Dict[str, float]]:
        """episode 行 metadata → 随机事件 state_deltas（PR1 P0 / PR4 共享接线）。

        结算顺序（2026-10-27 接线契约）：
        1. **快照优先**：创建 episode 时写进 ``metadata.state_deltas`` 的五项增量
           （新库替换后老活动仍按快照结算，绝不因 key 不在当前池就静默丢增量）；
        2. 旧行兼容：无快照 → 按 ``metadata.event_key`` 回查内置池
           :data:`RANDOM_EVENT_POOL`（旧 episode 的既有回放口径）；
        3. 仍找不到 → 再问运行中的事件库（同版本未替换时可解析）。

        返回 None = 非随机事件行 / 已应用过（``state_deltas_applied`` 持久标记，
        一次性保证）/ 确实无快照也无从回查——三种情况都不应用、不标记。
        """
        if not isinstance(metadata, dict) or metadata.get("state_deltas_applied"):
            return None
        snapshot = metadata.get("state_deltas")
        if isinstance(snapshot, dict) and snapshot:
            return {str(k): float(v) for k, v in snapshot.items()
                    if isinstance(v, (int, float)) and not isinstance(v, bool)}
        event_key = str(metadata.get("event_key") or metadata.get("event_id") or "")
        if not event_key:
            return None
        # P2：回查链含历史纯状态内置项档案（三项已迁 JSON，旧 episode 语义连续）。
        for event in (*RANDOM_EVENT_POOL, *LEGACY_STATE_EVENTS):
            if str(event.get("key") or event.get("event_id") or "") == event_key:
                deltas = event.get("state_deltas")
                return dict(deltas) if isinstance(deltas, dict) else None
        lookup = getattr(event_catalog, "get_state_deltas", None) if event_catalog is not None else None
        if callable(lookup):
            try:
                found = lookup(event_key)
            except Exception:
                found = None
            if isinstance(found, dict) and found:
                return {str(k): float(v) for k, v in found.items()
                        if isinstance(v, (int, float)) and not isinstance(v, bool)}
        return None

    @staticmethod
    def _ring_duration(start_minute: Any, end_minute: Any) -> Optional[int]:
        """actual_start → actual_end 的时长分钟（跨午夜按同一 24h 环折算，唯一算式走
        planning/time_utils.ring_minutes_ahead；PR2.5 修复（2026-10-26），交接
        §五.18 D6/D1——interrupted 事件 payload 的 duration_minutes 用）。

        任一端缺失/非法 → None（不虚构时长）。
        """
        if start_minute is None or end_minute is None:
            return None
        try:
            start = int(start_minute)
            end = int(end_minute)
        except (TypeError, ValueError):
            return None
        return int(ring_minutes_ahead(end, start))

    def _handle_squeezed_active(self, pre_active: Optional[Dict[str, Any]],
                                now: datetime, new_eid: str) -> None:
        """close-active 被挤行的补发布与状态回写（PR2.5 修复（2026-10-26），交接
        §五.18 D1，定稿 §4.1.3 反馈闭环）。

        背景：episode 被 start/resume 挤掉走 ``_close_active_episodes_locked`` →
        interrupted（schedule_db.py 一带）时原先不发 ``life.episode_interrupted``、
        不进 LifeState、随机事件 deltas 不应用——反馈闭环对「被新活动挤掉」这一
        打断形态是断的。修法取「planner 调用点检测」最小方案：调用点开播前抓当前
        active 行、开播成功后重查——状态变 interrupted 且 ``interrupted_by`` = 新行 id
        （close-active 的唯一写法；pause 走 paused、finish 走 completed，均不在此列）
        即判定被挤，补三件事：

        1. 发布 ``life.episode_interrupted``（payload 带 ``interrupted_by="close_active"``
           与 ``duration_minutes``（环折算）与 close_reason）；
        2. 随机事件行按既有 ``state_deltas_applied`` 标记补应用 deltas（应用后经
           ScheduleDB.update_episode_metadata 补写标记，防双应用沿用同一口径）；
        3. ``LifeState.apply_episode(status=interrupted)`` 入账。

        全程失败静默不阻塞主循环（与 tick 内其余链路同款纪律）。
        """
        if not isinstance(pre_active, dict):
            return
        eid = str(pre_active.get("episode_id") or "")
        if not eid:
            return
        try:
            row = self._db.get_episode(eid)
        except Exception as exc:
            logger.warning("rolling_planner: 被挤行重查失败（跳过补发布）：%s", exc)
            return
        if not isinstance(row, dict) or row.get("status") != "interrupted" \
                or row.get("interrupted_by") != new_eid:
            return  # 未被挤（正常开播/pause/finish 路径）→ 零动作
        meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        if meta.get("close_active_handled"):
            return  # 已补发布过（幂等保护，防同对 (被挤行, 新行) 重放重复发事件）
        # PR-B2：被挤掉 = 中断形态之一 → 父目标保留 paused（规格 §1）
        self._settle_parent_goal(row, "interrupted", progress=row.get("progress"))
        # 随机事件 deltas：按标记补应用一次；**先应用后补写持久标记**
        # （state_deltas_applied=防双应用沿用 PR1 0.4 口径；close_active_handled=本路径幂等）。
        # 2026-11-04 修复（评估问题6 同款顺序隐患）：原实现先写标记后应用，
        # apply_episode 抛错即永久漏记——现应用成功后才盖 state_deltas_applied，
        # 失败留 warning 不盖增量标记（close_active_handled 照常写，事件发布幂等不变）。
        deltas = self._random_event_deltas(meta, self._event_catalog)
        # 状态回写：interrupted 不盖 last_meal_at/last_break_at（apply_episode 既有语义），
        # 只做 state_deltas 加法与 clamp/落盘
        applied = False
        try:
            episode_payload: Dict[str, Any] = {"episode_id": eid, "goal_type": row.get("goal_type"),
                                               "status": "interrupted"}
            if deltas:
                episode_payload["state_deltas"] = deltas
            self._life.apply_episode(episode_payload, now=now)
            applied = True
        except Exception as exc:
            logger.warning("rolling_planner: 被挤行状态入账失败（episode=%s）：%s", eid, exc)
        marks: Dict[str, Any] = {"close_active_handled": True}
        if deltas is not None and applied:
            marks["state_deltas_applied"] = True
        try:
            self._db.update_episode_metadata(eid, marks)
        except Exception as exc:
            logger.warning("rolling_planner: 被挤行标记写回失败（episode=%s）：%s", eid, exc)
        if deltas is not None and not applied:
            logger.warning("rolling_planner: 被挤行随机事件增量未应用（episode=%s, deltas=%s），"
                           "未盖 state_deltas_applied 标记", eid, deltas)
        payload: Dict[str, Any] = {
            "episode_id": eid, "name": row.get("name"),
            "goal_type": row.get("goal_type"), "interrupted_by": "close_active",
            "progress": row.get("progress"),
            "close_reason": str(meta.get("close_reason") or ""),
        }
        duration = self._ring_duration(row.get("actual_start_minute"),
                                       row.get("actual_end_minute"))
        if duration is not None:
            payload["duration_minutes"] = duration
        self._publish(EVENT_EPISODE_INTERRUPTED, **payload)
        self._finish_event_claim(row, 'interrupted', reason='close_active')
        self._log("rolling.episode_closed_active", episode_id=eid,
                  interrupted_by="close_active",
                  close_reason=str(meta.get("close_reason") or ""))

    def _finish_event_claim(self, episode: Dict[str, Any], status: str, *, reason: str = '') -> None:
        """Attach the outcome to this generation's receipt without returning it to a pool."""
        meta = episode.get('metadata') or {}
        if not isinstance(meta, dict) or meta.get('event_pool_kind') != 'one_shot':
            return
        catalog = getattr(self, '_event_catalog', None)
        if catalog is None:
            return
        try:
            catalog.finish_claim({'pool_kind': 'one_shot',
                                  'event_id': meta.get('event_id') or meta.get('event_key'),
                                  'generation_id': meta.get('event_generation_id')},
                                 status, str(episode.get('episode_id') or ''), reason=reason)
        except Exception:
            logger.warning('随机事件结果未补记，已消费事件不返池', exc_info=True)

    def _finish_episode(self, episode: Dict[str, Any], now: datetime,
                        snapshot: Any, *, actual_end_minute: Optional[int] = None,
                        reason: str = "") -> None:
        eid = str(episode.get("episode_id") or "")
        end = actual_end_minute
        if end is None:
            end = episode.get("actual_end_minute") or episode.get("planned_end_minute")
        # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.4：随机事件行按
        # metadata.event_key 取 state_deltas，随关闭一并交 LifeState.apply_episode
        # （事件结束后五项 ± 增量）；应用成功后才写 metadata.state_deltas_applied
        # 持久标记，重复关闭路径不会二次应用。
        # 2026-11-04 修复（评估问题6）：原实现把标记随 finish 先落库、apply_episode
        # 在 except-pass 内——应用抛错即永久漏记且无日志。现改为**先应用后标记**，
        # 应用失败留 warning 且不盖标记（后续回放路径仍有机会补应用）。
        deltas = self._random_event_deltas(episode.get("metadata"), self._event_catalog)
        extra_meta: Optional[Dict[str, Any]] = {"close_reason": reason} if reason else None
        if not self._db.finish(eid, actual_end_minute=end, metadata=extra_meta):
            return
        self._finish_event_claim(episode, 'completed', reason=reason)
        # PR-B2：活动完成 → 父目标完成（不长留待办）；无父目标零动作
        self._settle_parent_goal(episode, "completed")
        # 吃饭/休息完成 → 盖 last_meal_at / last_break_at（时长内变化由 tick 速率记账，不重复）
        applied = False
        try:
            payload: Dict[str, Any] = {"episode_id": eid, "goal_type": episode.get("goal_type"),
                                       "status": "completed"}
            if deltas:
                payload["state_deltas"] = deltas
            self._life.apply_episode(payload, now=now)
            applied = True
        except Exception as exc:
            logger.warning("rolling_planner: episode 关闭状态入账失败（episode=%s）：%s", eid, exc)
        if deltas is not None:
            if applied:
                try:
                    self._db.update_episode_metadata(eid, {"state_deltas_applied": True})
                except Exception as exc:
                    logger.warning("rolling_planner: 增量已应用但标记写回失败"
                                   "（episode=%s，回放可能重复应用）：%s", eid, exc)
            else:
                logger.warning("rolling_planner: 随机事件增量未应用（episode=%s, deltas=%s），"
                               "未盖 state_deltas_applied 标记", eid, deltas)
        self._publish(EVENT_EPISODE_COMPLETED, episode_id=eid,
                      name=episode.get("name"), goal_type=episode.get("goal_type"),
                      event_key=(episode.get("metadata") or {}).get("event_key"),
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
        self._finish_event_claim(episode, 'interrupted', reason=reason)
        # PR-B2：活动中断 ≠ 目标消失——父目标保留 paused（规格 §1）
        self._settle_parent_goal(episode, "paused", progress=episode.get("progress"))
        # PR2.5 修复（2026-10-26），交接 §五.18 D6：interrupted 事件补 duration_minutes
        # （actual_end-actual_start 跨午夜按同一 24h 环折算，走 planning/time_utils；
        # 任一端缺失不虚构——反馈侧对打断形态即可设时长门槛）
        payload: Dict[str, Any] = {
            "episode_id": eid, "name": episode.get("name"),
            "goal_type": episode.get("goal_type"), "interrupted_by": reason,
            "progress": episode.get("progress"),
        }
        duration = self._ring_duration(episode.get("actual_start_minute"),
                                       minutes_of_now(now))
        if duration is not None:
            payload["duration_minutes"] = duration
        self._publish(EVENT_EPISODE_INTERRUPTED, **payload)
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
        # P0：提前收尾写当时的实际终点（cur），不回退计划终点造成与下一条重叠/记到未来。
        self._finish_episode(current, now, snapshot, actual_end_minute=cur,
                             reason="hunger_threshold")
        report["actions"].append("force_break_hunger")

    # ── 步 3：硬约定（提前 15 分钟预警 / 到点抢占 / 错过如实登记）─────────
    def _handle_commitments(self, date: str, cur: int, now: datetime,
                            snapshot: Any, report: Dict[str, Any]) -> None:
        intents = self._spine_intents_cached(date)
        commitments = [i for i in intents if i["hardness"] == "hard"
                       and not (self.dynamic_sleep and is_night_sleep_item(i) and not is_named_nap(i))]
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
                    if self._engine_owns_sleep(current):
                        # 用户 2026-10-27 裁定：硬约定 = **正式起床**——完成当前睡眠
                        # 并移出恢复池；actual_end = 真实唤醒时刻，不倒填约定开始/阈值。
                        if not self._engine_wake_for_commitment(now):
                            # 引擎已不在睡（会话缺失/已关闭）：睡眠行照样 completed，
                            # 绝不 paused（paused 会被恢复池重新取出，同一活动不可恢复）
                            self._finish_episode(
                                current, now, snapshot,
                                actual_end_minute=self._episode_frame_minute(current, now),
                                reason="hard_commitment_preempt")
                            report["actions"].append("sleep_commitment_finished")
                        else:
                            # 2026-10-27 综合验收：硬约定在 planner 内 force_wake，
                            # 不走 plugin._sleep_commit 的 woke 分支，必须在此同步
                            # 真实醒来时刻（否则 cycle_start 漏记）。
                            self._note_actual_wake(now)
                    elif self.dynamic_sleep and (current.get("metadata") or {}).get("kind") == "nap":
                        self._finish_episode(current, now, snapshot,
                                             actual_end_minute=self._episode_frame_minute(current, now),
                                             reason="nap_hard_commitment")
                    else:
                        self._pause_episode(current, now, reason="hard_commitment_preempt")
                self._start_commitment(c, date, cur, now, snapshot, report)
                recorded.add(item_key)
            elif ws - HARD_COMMITMENT_WARN_MINUTES <= cur < ws and current is not None:
                # 提前 15 分钟预警：当前 episode 会吃掉约定窗 → 提前收尾（§5.7 触发 2）
                if self._engine_owns_sleep(current):
                    # 睡眠 episode 的醒来时刻由引擎（约定 − lead）决定；这里用约定开始
                    # 回填 actual_end 会提前宣告"已醒"并留下假历史 → 交给引擎。
                    continue
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
        if self.dynamic_sleep and is_named_nap(intent):
            intent = {**intent, "goal_type": "rest"}
        # PR2.5 修复（2026-10-26），交接 §五.18 D1：开播前抓当前 active——上方抢占
        # pause 失败的兜底场景下本行会被 close-active 挤掉，补发布与状态回写
        pre_active = self._safe_active_episode()
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
            self._handle_squeezed_active(pre_active, now, eid)
            self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=intent["name"],
                          goal_type=intent["goal_type"], planned_start_minute=ws,
                          planned_end_minute=we, source="commitment")
            self._log("rolling.commitment_started", episode_id=eid,
                      commitment=intent["item_id"], window=f"{ws}-{we}")
            report["actions"].append("commitment_started")

    # ── 睡觉锚点 ─────────────────────────────────────────────────────────
    def _start_sleep_episode(self, date: str, cur: int, now: datetime,
                             snapshot: Any, report: Dict[str, Any], *,
                             dynamic: Optional[Dict[str, Any]] = None) -> bool:
        """睡眠环内无当前 episode → 开一条 sleep episode（锚点，不采样覆盖）。

        PR3（2026-10-26，定稿 §4.2.3）：
        - 引擎启用时 ``planned_end`` = lie-in 上限（安全末端），实际 ``actual_end``
          由 SleepEngine 决定；引擎未启用 → 逐字旧行为（wake 锚点）。
        - 引擎闸 ``should_start_sleep_episode`` 为 False（本夜已在睡/已醒，尤其硬约定
          早于 wake 锚点醒来后仍处原睡眠环）→ 不再开新 episode，避免每拍重开/重复结算。
        """
        if not self._engine_allows_sleep_start(now):
            return False
        sleep_min, wake_min = self._sleep_anchors()
        engine_on = self._engine_enabled()
        planned_end = self._planned_sleep_end(cur, wake_min, self._lie_in_cap_minute(),
                                              engine_on)
        metadata: Dict[str, Any] = {"trigger": "sleep_anchor",
                                    "sleep_ring": f"{sleep_min}-{wake_min}"}
        if engine_on:
            metadata["sleep_engine"] = True
        if dynamic:
            latest = datetime.fromisoformat(dynamic["latest_wake"])
            planned_end = (latest.date() - now.date()).days * DAY_MINUTES + minutes_of_now(latest)
            metadata["dynamic_sleep"] = dynamic
            metadata["trigger"] = dynamic["onset_reason"]
        eid = self._db.start_episode(
            schedule_date=date, name="睡觉", goal_type=SLEEP_GOAL_TYPE, source="rolling",
            planned_start_minute=cur, planned_end_minute=planned_end,
            interruptibility=SLEEP_INTERRUPTIBILITY, switch_cost=1.0,
            attention_mode="later", state_before=self._state_brief(snapshot),
            metadata=metadata,
        )
        if eid:
            self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name="睡觉",
                          goal_type=SLEEP_GOAL_TYPE, planned_start_minute=cur,
                          planned_end_minute=planned_end, source="rolling")
            if dynamic:
                self._sleep_engine.prepare_tick(
                    now, sleep_episode={"episode_id": eid, "goal_type": SLEEP_GOAL_TYPE,
                                        "planned_end_minute": planned_end, "metadata": metadata})
            self._log("rolling.sleep_started", episode_id=eid,
                      planned_minutes=max(0, planned_end - cur),
                      **({"dynamic": dynamic} if dynamic else {}))
            report["actions"].append("sleep_started")
        return bool(eid)

    def _try_dynamic_sleep(self, date, cur, now, snapshot, current, report, *, manual=False):
        if self._engine_owns_sleep(current):
            return False
        interruptibility = _as_number((current or {}).get("interruptibility"), 1.0)
        engagement = _as_number((current or {}).get("switch_cost"), .5) if current else 0
        blocked = bool(current and (current.get("source") == "commitment" or interruptibility < .65))
        decision = self._sleep_engine.evaluate_onset(
            now, snapshot, hard_start=self.sleep_hard_commitment_start(now),
            interruptibility=interruptibility, engagement=engagement, blocked=blocked, manual=manual)
        if not decision:
            return False
        if current and not self._pause_episode(current, now, reason="dynamic_sleep"):
            return False
        return self._start_sleep_episode(date, cur, now, snapshot, report, dynamic=decision)

    def force_sleep(self, now: Optional[datetime] = None) -> bool:
        """Administrator request, bounded by the same night and commitment guards."""
        if not self.dynamic_sleep:
            return False
        now = self._now(now)
        return self._try_dynamic_sleep(self._date_of(now), minutes_of_now(now), now,
                                       self._life.snapshot(), self._safe_active_episode(),
                                       {"actions": []}, manual=True)

    def _roll_nap(self, date, cur, now, snapshot, current, report) -> bool:
        if cur < 13 * 60 or self._nap_skipped_date == date:
            return False
        if self._nap_date != date:
            self._nap_date, self._nap_reason = date, ""
        rows = self._db.episodes_today(date)
        if any(is_named_nap(row)
               and row.get("actual_start_minute") is not None for row in rows):
            self._nap_skipped_date = date
            self._nap_reason = "taken"
            self._sleep_engine.record_nap(date, "taken")
            return False
        if cur > 15 * 60 + 15:
            self._nap_skipped_date = date
            reason = self._nap_reason or "window_closed"
            self._sleep_engine.record_nap(date, reason)
            self._log("rolling.nap_skipped", reason=reason)
            return False
        reason = nap_reason(now, self._sleep_engine.actual_wake_today(now),
                            snapshot.get("energy"), snapshot.get("sleep_debt"))
        hard = self.sleep_hard_commitment_start(now)
        remaining = 15 * 60 + 30 - cur
        if hard:
            remaining = min(remaining, int((hard.timestamp() - now.timestamp()) // 60))
        if remaining < 15:
            reason = "hard_commitment"
        elif current and (current.get("goal_type") != "idle" or current.get("source") == "commitment"):
            reason = "busy"
        self._nap_reason = reason
        self._sleep_engine.record_nap(date, reason)
        if reason != "ready":
            return False
        if current:
            self._finish_episode(current, now, snapshot,
                                 actual_end_minute=self._episode_frame_minute(current, now),
                                 reason="nap_start")
            if self._safe_active_episode() is not None:
                return False
        candidate = {"kind": "nap", "name": "午休", "goal_type": "rest",
                     "profile_key": "rest", "interruptibility": .9}
        self._start_episode_for_candidate(candidate, min(20, remaining), date, cur, now,
                                          snapshot, [], self._day_rng(date), report)
        if (self._safe_active_episode() or {}).get("goal_type") == "rest":
            self._nap_reason = "taken"
            self._nap_skipped_date = date
            self._sleep_engine.record_nap(date, "taken")
            self._log("rolling.nap_started", minutes=min(20, remaining))
            return True
        return False

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
        # The engine owns night sleep anchors. Do not also offer them as hard
        # waking commitments, soft work candidates, or competing boundaries.
        return extract_spine_intents([item for item in items
                                      if not is_night_sleep_item(item)])

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

    def _meal_intent_satisfied(self, date: str, cur: int, cand: Dict[str, Any]) -> bool:
        """P0（2026-11-09）：跨来源餐食语义去重——近期已用餐，或同餐次（早/午/晚）已有
        用餐 episode（anchor 或意图）完成/进行中，则不再排该 meal 意图（已吃早餐不被后续
        早餐意图强制再吃）；紧急饥饿保护仍由状态阈值链保留，不在此绕过。"""
        if self._recent_meal_today(date, cur):
            return True

        def part(m: int) -> int:
            return 0 if m < 600 else (1 if m < 900 else 2)

        ws = int((cand.get("window") or (0, 0))[0])
        try:
            rows = self._db.episodes_today(date)
        except Exception:
            return False
        for row in rows:
            if str(row.get("goal_type")) != "meal":
                continue
            if row.get("status") not in ("completed", "active"):
                continue
            start = row.get("actual_start_minute")
            if start is None:
                start = row.get("planned_start_minute")
            try:
                if start is not None and part(int(start)) == part(ws):
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _event_usage_path(self):
        data_dir = getattr(self, "_data_dir", None)
        if not data_dir:
            return None
        return Path(data_dir) / "rolling_event_usage.json"

    def _load_event_usage(self) -> None:
        """P1：冷却/当日用量从持久记录恢复（热重载不重置）；损坏忽略不补造。

        同一文件兼存模块联动事件的当日计数/最近发生（linked_*），供热重载恢复。
        """
        self._event_last_used = {}
        self._event_day_counts = {}
        self._event_usage_date = ""
        self._linked_usage_restored = False
        path = self._event_usage_path()
        if path is None or not path.exists():
            return
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            self._event_last_used = dict(doc.get("last_used") or {})
            self._event_usage_date = str(doc.get("date") or "")
            self._event_day_counts = dict(doc.get("day_counts") or {})
            if str(doc.get("linked_date") or "") == self._random_date:
                self._random_used = max(0, int(doc.get("linked_used") or 0))
                self._random_last = {}
                for key, raw in (doc.get("linked_last") or {}).items():
                    parsed = self._parse_ts(raw, getattr(self._tz, "_tz", None))
                    if parsed is not None:
                        self._random_last[str(key)] = parsed
                self._linked_usage_restored = True
        except Exception as exc:
            logger.debug("rolling_event_usage 读取失败（按空恢复）：%s", exc)

    def _save_event_usage(self, date: str) -> None:
        path = self._event_usage_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({
                "date": date, "last_used": self._event_last_used,
                "day_counts": self._event_day_counts,
                "linked_date": self._random_date, "linked_used": self._random_used,
                "linked_last": {k: v.isoformat() for k, v in self._random_last.items()},
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:
            logger.debug("rolling_event_usage 落盘失败（忽略）：%s", exc)

    def _catalog_active_events(self) -> List[Dict[str, Any]]:
        """只读当前活跃事件（初始+生成，未消耗）。兼容服务/store/测试桩三种形态。"""
        catalog = getattr(self, "_event_catalog", None)
        if catalog is None:
            return []
        for holder in (getattr(catalog, "store", None), catalog):
            if holder is None:
                continue
            try:
                events = holder.active_events
                if callable(events):
                    events = events()
                return [dict(e) for e in (events or []) if isinstance(e, dict)]
            except Exception:
                continue
        return []

    def _one_shot_pacing(self, events: List[Dict[str, Any]], now: datetime) -> float:
        """P1 §5.2：一次性按剩余条目/刷新剩余天数温和分散（不首日耗尽、不追加生成）。"""
        remaining = sum(1 for e in events if e.get("pool_kind") == "one_shot")
        if remaining <= 0:
            return 1.0
        days_left = 7.0
        catalog = getattr(self, "_event_catalog", None)
        for holder in (getattr(catalog, "store", None), catalog):
            raw = getattr(holder, "next_update_at", None) if holder is not None else None
            parsed = self._parse_ts(raw, getattr(self._tz, "_tz", None))
            if parsed is not None:
                days_left = max(1.0, (parsed - now).total_seconds() / 86400.0)
                break
        return _clamp(0.5 + 0.5 * min(1.0, remaining / days_left), 0.5, 1.0)

    def _event_candidates(self, date: str, cur: int, snapshot: Any) -> List[Dict[str, Any]]:
        """P1 只读可行事件候选：初始 JSON/生成池普通事件进入主选取器；评分/预览不消耗
        一次性。代码联动事件（watch_bili/browse_qq）不在此（走模块预算路径）。

        过滤原因计数落 ``self._last_event_filter_stats`` 供决策诊断（§5.5）；
        来源组平衡（§5.2）：组内按 权重×状态适配×当日新颖度 排序后逐项递减，
        防止近义项挤满 top3；一次性另乘剩余量/刷新期 pacing。
        """
        self._last_event_filter_stats = {"before": 0, "after": 0, "excluded": {}}
        catalog = getattr(self, "_event_catalog", None)
        if catalog is None:
            return []
        if not hasattr(self, "_event_last_used"):
            self._load_event_usage()
        if self._event_usage_date != date:
            self._event_day_counts = {}
            self._event_usage_date = date
        from .event_slots import time_slot_of, event_allowed_in_slot
        from .event_catalog import state_affinity
        now = self._now(datetime.fromisoformat(date) + timedelta(minutes=cur))
        slot = time_slot_of(now, getattr(self._tz, "_tz", None))
        events = self._catalog_active_events()
        stats = self._last_event_filter_stats
        stats["before"] = len(events)

        def exclude(reason: str) -> None:
            stats["excluded"][reason] = stats["excluded"].get(reason, 0) + 1

        eligible: List[Tuple[float, Dict[str, Any]]] = []
        for ev in events:
            eid = str(ev.get("event_id") or ev.get("key") or "")
            if not eid:
                exclude("no_event_id")
                continue
            if eid in MODULE_LINKED_EVENT_IDS:
                exclude("module_linked_path")
                continue  # 联动事件走模块预算路径
            if ev.get("enabled") is False:
                exclude("disabled")
                continue
            if not event_allowed_in_slot(ev, slot):
                exclude("slot_mismatch")
                continue
            cooldown = _as_number(ev.get("cooldown_minutes"), 0.0) or 0.0
            last = self._event_last_used.get(eid)
            if cooldown > 0 and last is not None:
                try:
                    if (now.timestamp() - float(last)) / 60.0 < cooldown:
                        exclude("cooldown")
                        continue
                except (TypeError, ValueError):
                    pass
            lo = int(_as_number(ev.get("min_minutes"), 0.0) or 0)
            hi = int(_as_number(ev.get("max_minutes"), 0.0) or 0)
            if lo <= 0 or hi < lo:
                exclude("bad_duration_contract")
                continue
            weight = _clamp(_as_number(ev.get("weight"), 0.5) or 0.5)
            used = int(self._event_day_counts.get(eid, 0) or 0)
            novelty = 1.0 / (1.0 + used)
            attract = weight * novelty * state_affinity(ev, self._state_brief(snapshot))
            eligible.append((attract, ev))
        # 组内按吸引度降序 → 同来源组第 k 项递减（文件遍历顺序不影响结果）
        eligible.sort(key=lambda pair: (-pair[0], str(pair[1].get("event_id") or "")))
        pacing = self._one_shot_pacing(events, now)
        group_seen: Dict[str, int] = {}
        out: List[Dict[str, Any]] = []
        for attract, ev in eligible:
            eid = str(ev.get("event_id") or ev.get("key") or "")
            origin = str(ev.get("origin_kind")
                         or ("code_builtin" if ev.get("source") == "builtin" else "llm_generated"))
            pool_kind = str(ev.get("pool_kind") or "reusable")
            group = f"{origin}:{'one_shot' if pool_kind == 'one_shot' else 'normal'}"
            k = group_seen.get(group, 0)
            group_seen[group] = k + 1
            prior = EVENT_ORIGIN_PRIORS.get(origin, 0.8)
            factor = prior * (EVENT_GROUP_PRESENCE_DECAY ** k)
            if pool_kind == "one_shot":
                factor *= pacing
            goal = str(ev.get("goal_type") or "daily_routine")
            out.append({
                "kind": "event", "name": ev.get("name"), "goal_type": goal,
                "priority_score": _clamp(attract * factor),
                "min_minutes": int(ev.get("min_minutes") or 0),
                "max_minutes": int(ev.get("max_minutes") or 0),
                "interruptibility": _as_number(ev.get("interruptibility"), 0.7),
                "profile_key": goal, "window": None, "resume_of": None,
                "resume_bonus": 0.0, "intent_item_id": None,
                "event_id": eid, "pool_kind": pool_kind, "origin_kind": origin,
                "note": str(ev.get("name") or ""),
                "_event_def": ev,
            })
        stats["after"] = len(out)
        return out

    def _idle_score_context(self, candidates: List[Dict[str, Any]], date: str,
                            cur: int) -> Dict[str, Any]:
        """P1 §5.3：idle 降权只在确有可行非 idle 候选时生效（窗口未到/不可执行不算）；
        选取与 preview 共用同一口径。"""
        def _feasible_nonidle(c: Dict[str, Any]) -> bool:
            if c["kind"] == "idle":
                return False
            if c["kind"] == "commitment" and c.get("window") and c["window"][0] > cur:
                return False
            if c["kind"] == "intent" and c.get("window") \
                    and cur < c["window"][0] - SOFT_INTENT_EARLY_MINUTES:
                return False
            return True
        return {"_nonidle_feasible": any(_feasible_nonidle(c) for c in candidates),
                "_idle_minutes": self._idle_minutes_today(date, cur)}

    def _idle_minutes_today(self, date: str, cur: int) -> float:
        """P1 §5.3：当日累计待机分钟（已结束按实际区间，进行中按 cur 折算）。"""
        total = 0.0
        try:
            rows = self._db.episodes_today(date)
        except Exception:
            return 0.0
        for row in rows:
            if str(row.get("goal_type") or "") != "idle":
                continue
            start = row.get("actual_start_minute")
            if start is None:
                start = row.get("planned_start_minute")
            if start is None:
                continue
            end = row.get("actual_end_minute")
            status = str(row.get("status") or "")
            if end is None:
                end = cur if status in ("active", "paused") else row.get("planned_end_minute")
            if end is None:
                continue
            try:
                span = ring_minutes_ahead(int(end), int(start))
                if status in ("active", "paused"):
                    span = min(span, max(0, cur - int(start)))
                total += max(0.0, float(span))
            except (TypeError, ValueError):
                continue
        return total

    def _next_boundary_minutes(self, date: str, cur: int, intents: List[Dict[str, Any]]) -> Optional[int]:
        """距下一个硬边界（硬约定开始 / 睡眠环起点）的分钟数；无 → None。"""
        candidates: List[int] = []
        for c in intents:
            if c["hardness"] == "hard" and str(c.get("day_offset")) != "1":
                ws = c["start_minute"]
                if ws > cur:
                    candidates.append(ws - cur)
        sleep_min, _wake = self._sleep_anchors()
        if self.dynamic_sleep:
            midnight = datetime.fromisoformat(date)
            now = self._now(midnight + timedelta(minutes=cur))
            window = self._sleep_engine.dynamic_window(now)
            boundary = window.start if now < window.start else window.latest_start
            d = int((boundary - now).total_seconds() // 60)
        else:
            d = (normalize_day_minutes(sleep_min) - normalize_day_minutes(cur)) % DAY_MINUTES
        if 0 < d < DAY_MINUTES:
            candidates.append(d)
        return min(candidates) if candidates else None

    def _build_candidates(self, date: str, cur: int, snapshot: Any,
                          current_profile: Optional[Any]) -> List[Dict[str, Any]]:
        """候选多源（§5.1）；返回未打分的候选 dict 列表。P1 起普通小事件经
        ``_event_candidates`` 进入统一候选；仅模块联动两项仍走转场掷骰独立预算。"""
        horizon = max(15, self._cfg_int("horizon_minutes", DEFAULT_HORIZON_MINUTES))
        intents = self._spine_intents_cached(date)
        candidates: List[Dict[str, Any]] = []

        # 源 1：硬约定临近（start ∈ (cur, cur+horizon] 的提前入围；到点开播在 tick 步 3）
        for c in intents:
            if c["hardness"] != "hard" or str(c.get("day_offset")) == "1":
                continue
            if self.dynamic_sleep and is_night_sleep_item(c) and not is_named_nap(c):
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
            if self.dynamic_sleep and (is_named_nap(c) or is_night_sleep_item(c)):
                continue
            ws, we = c["start_minute"], c["end_minute"]
            if we <= ws:
                continue
            if str(c.get("day_offset")) == "1":
                continue  # 保守：午夜段不参与当日相交判定（§7.8 帧首双关，避免误排）
            if self._intent_done_today(date, c["item_id"]):
                continue
            # 这里只读date所属日帧的意图；>1440是未来次日，不可折回当天凌晨。
            # 已启动的昨日跨午夜活动由_episode_frame_minute独立延续/收尾。
            intersects = ws <= cur + horizon and we > cur
            if not intersects:
                continue
            candidate = self._candidate_from_intent(c, kind="intent")
            if self.dynamic_sleep and c["priority"] == "low" and c["goal_type"] in {"work", "study"}:
                energy = _as_number(snapshot.get("energy"), 1.0)
                profile_min = math.ceil(activity_profiles.get_profile(c["goal_type"]).min_block)
                short_block = max(c["min_minutes"], profile_min, 20)
                if energy < .35 and ws + 15 + short_block <= we:
                    if cur < ws + 15:
                        continue
                    candidate["max_minutes"] = min(c["max_minutes"] or short_block, short_block)
            candidates.append(candidate)

        # 源 4：恢复池
        for row in self.resume_pool():
            if self.dynamic_sleep and (is_named_nap(row) or is_night_sleep_item(row)):
                continue
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

        # A depleted mind needs a real recovery opportunity even when today's
        # spine has no rest intent. This never preempts sleep/hard commitments.
        focus = _as_number(snapshot.get("focus"), 1.0)
        energy = _as_number(snapshot.get("energy"), 1.0)
        stress = _as_number(snapshot.get("stress"), 0.0)
        needs_rest = focus < 0.30 or energy < 0.30 or stress > 0.80
        recent_rest = False
        try:
            recent_rest = any(row.get("goal_type") == "rest" and row.get("status") == "completed"
                              and row.get("actual_end_minute") is not None
                              and 0 <= cur-int(row["actual_end_minute"]) < 15
                              for row in self._db.episodes_today(date))
        except Exception:
            pass
        if needs_rest and not recent_rest:
            rest = activity_profiles.get_profile("rest")
            candidates.append({
                "kind": "anchor_rest", "name": "休息与恢复", "goal_type": "rest",
                "priority_score": PRIORITY_SCORES["high"], "min_minutes": 15,
                "max_minutes": 30, "interruptibility": rest.interruptibility,
                "profile_key": "rest", "window": None, "resume_of": None,
                "resume_bonus": 0.0, "intent_item_id": None,
                "note": "专注偏低，给自己一段恢复时间" if focus < 0.30 else "身心需要放缓节奏",
            })

        # 源 4.5（PR-B2 规格 §1）：跨日继续候选——paused 且有延续价值的目标，
        # 每日此类候选 ≤1（每次构建至多挑 1 条），energy 低时不出现；权重温和、
        # 允许放弃（只是普通候选，不形成压力队列，不穿透睡眠/硬约定）。
        continuation = self._continuation_candidate(date, cur, snapshot)
        if continuation is not None:
            candidates.append(continuation)

        # 源 4.6（P1，2026-11-09 滚动日程计划）：普通小事件（JSON 状态 + 生成池）提升为
        # 正常滚动候选，统一评分/抽样/认领；不再依赖 8% 转场掷骰填充日常。代码联动事件
        # （watch_bili/browse_qq）仍走 _maybe_random_event 的模块预算路径，不在此重复。
        candidates.extend(self._event_candidates(date, cur, snapshot))

        # 源 5：idle 待机（永远存在的低优先候选——A.10.1 一等公民）。
        # 2.1：内部仍是 idle 候选；名称只作对外口径，写库/聊天/详情看到的是具体生活细节。
        idle_profile = activity_profiles.get_profile("idle")
        candidates.append({
            "kind": "idle", "name": idle_display_name(date, cur), "goal_type": "idle",
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
        energy = _as_number(snapshot.get("energy"), 1.0)
        hunger = _as_number(snapshot.get("hunger"), 0.0) or 0.0
        focus = _as_number(snapshot.get("focus"), 1.0)
        profile = activity_profiles.get_profile(c["profile_key"])
        goal_type = c["goal_type"]

        priority = _clamp(c.get("priority_score", 0.5))
        due = self._due_urgency(c, cur)
        energy_fit = self._fit_curve(energy, ENERGY_DEMAND.get(goal_type, 0.5))
        hunger_fit = hunger if goal_type == "meal" else 0.5 * (1.0 - hunger)
        focus_fit = self._fit_curve(focus, FOCUS_DEMAND.get(goal_type, 0.4))
        resume_bonus = _clamp(c.get("resume_bonus", 0.0))
        novelty = 1.0 / (1.0 + counts.get(goal_type, 0))
        # P1 §5.2：事件新颖度按 event_id 当日已启动次数（持久用量），不只按
        # goal_type 粗分——"看视频"和"吃零食"不因同属 entertainment 互相视为重复。
        if c.get("kind") == "event":
            used = int((getattr(self, "_event_day_counts", None) or {})
                       .get(str(c.get("event_id") or ""), 0) or 0)
            novelty = 1.0 / (1.0 + used)
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
        # P1 §5.3：确有可行非 idle 候选时，累计待机 10 分钟后递增降权 idle；
        # 无可行候选/临近硬边界时不罚（等待是真实兜底，不造事凑占比）。
        if c.get("kind") == "idle" and counts.get("_nonidle_feasible"):
            idle_min = float(counts.get("_idle_minutes", 0) or 0)
            if idle_min >= IDLE_PENALTY_START_MINUTES:
                steps = (idle_min - IDLE_PENALTY_START_MINUTES) / 5.0 + 1.0
                score -= min(IDLE_PENALTY_MAX, IDLE_PENALTY_PER_5_MIN * steps)
        # PR-B2（规格 §2）+ P3（§14.6）：持续动机与稳定偏好只改候选权重，受联合
        # 上限约束；状态适配已在 base 算一次，不重复奖励。偏好只施加给通过门控
        # 的可行软候选——硬约定/正式睡眠/紧急生理保护不受偏好竞争；不穿透
        # 睡眠/硬约定/注意力/预算（那些判定在 tick 步 3/5 与本公式之外）。
        motive_add = 0.0
        store = self._motive_store
        if store is not None:
            try:
                ratio = float(store.bonus_for(c))
            except Exception:
                ratio = 0.0
            ratio = max(0.0, min(MOTIVE_BONUS_RATIO, ratio))
            motive_add = ratio * max(score, 0.0)
        pref_affinity = 0.0
        prefs = getattr(self, "_activity_preferences", None)
        if prefs is not None and c.get("kind") in PREFERENCE_SOFT_KINDS:
            try:
                pref_affinity = _clamp(float(prefs.preference_for(c).get("affinity", 0.0)),
                                       -1.0, 1.0)
            except Exception:
                pref_affinity = 0.0
        if motive_add > 0.0 or pref_affinity != 0.0:
            score += interest_bonus(pref_affinity, motive_add)
        # P3 §14.6：最近重复项逐步降权（做得多不等于更喜欢；冷却仍是独立硬限制）。
        if c.get("kind") == "event":
            used = int((getattr(self, "_event_day_counts", None) or {})
                       .get(str(c.get("event_id") or ""), 0) or 0)
            if used > 1:
                score -= min(0.20, 0.05 * (used - 1))
        return score

    # ── 抽取原因人话化（3.17.7 改进1：纯旁路展示，0 LLM，不改打分/选择）──
    def _draw_reason_zh(self, c: Dict[str, Any], snapshot: Any, cur: int,
                        date: str) -> str:
        """把主导打分分量 + 候选 kind + motive_bonus 翻译成确定性中文短句。

        只读重算与 :meth:`_score_candidate` 相同的分量（同公式同权重；
        current_profile=None 与选择路径一致），取最大正分量映射人话；
        并列时按固定顺序取第一个（确定性）。任何异常 → 兜底短句，
        绝不阻塞开播（纯旁路：不参与打分、不改选择、不写状态）。
        """
        fallback = "按当前状态顺位选了这件事"
        try:
            kind = str(c.get("kind") or "")
            goal_type = str(c.get("goal_type") or "")
            state = snapshot if isinstance(snapshot, dict) else {}
            energy = _as_number(state.get("energy"), 1.0)
            hunger = _as_number(state.get("hunger"), 0.0) or 0.0
            # kind / 状态直译（确定性优先，不走分量比较）
            if kind == "idle":
                # 2.1：对外口径落到具体生活细节，不再输出「待机」。
                detail = str(c.get("name") or "").strip()
                if detail not in IDLE_DISPLAY_DETAILS:
                    detail = idle_display_name(date, cur)
                return f"没什么特别想做的，就{detail}"
            if kind in ("resume", "continuation"):
                return "刚才的事还没做完，接着做"
            if kind == "commitment":
                return "到约定的时间了，按计划去做"
            if kind == "nap":
                return "有点困了，先眯一会儿"
            if goal_type == "meal" and hunger >= 0.5:
                return "饿了，想找点吃的"
            if goal_type == "rest" and energy <= 0.35:
                return "精力有点低，想休息一下"
            # 主导分量：与 _score_candidate 逐字同公式（只读重算）
            focus = _as_number(state.get("focus"), 1.0)
            profile = activity_profiles.get_profile(c["profile_key"])
            boundary = self._next_boundary_minutes(
                date, cur, self._spine_intents_cached(date))
            counts = self._completed_counts_today(date)
            priority = _clamp(c.get("priority_score", 0.5))
            due = self._due_urgency(c, cur)
            energy_fit = self._fit_curve(energy, ENERGY_DEMAND.get(goal_type, 0.5))
            hunger_fit = hunger if goal_type == "meal" else 0.5 * (1.0 - hunger)
            focus_fit = self._fit_curve(focus, FOCUS_DEMAND.get(goal_type, 0.4))
            resume_bonus = _clamp(c.get("resume_bonus", 0.0))
            novelty = 1.0 / (1.0 + counts.get(goal_type, 0))
            if c.get("kind") == "event":
                used = int((getattr(self, "_event_day_counts", None) or {})
                           .get(str(c.get("event_id") or ""), 0) or 0)
                novelty = 1.0 / (1.0 + used)
            overrun_risk = self._overrun_risk(c, profile, boundary)
            base = (SCORE_WEIGHTS["priority"] * priority
                    + SCORE_WEIGHTS["due"] * due
                    + SCORE_WEIGHTS["energy"] * energy_fit
                    + SCORE_WEIGHTS["hunger"] * hunger_fit
                    + SCORE_WEIGHTS["focus"] * focus_fit
                    + SCORE_WEIGHTS["resume"] * resume_bonus
                    + SCORE_WEIGHTS["novelty"] * novelty
                    - SCORE_WEIGHTS["overrun"] * overrun_risk)
            motive_add = 0.0
            store = self._motive_store
            if store is not None:
                try:
                    ratio = max(0.0, min(MOTIVE_BONUS_RATIO, float(store.bonus_for(c))))
                except Exception:
                    ratio = 0.0
                motive_add = ratio * max(base, 0.0)
            # 固定顺序取最大（并列取先出现者 → 确定性）
            components = [
                ("priority", SCORE_WEIGHTS["priority"] * priority),
                ("due", SCORE_WEIGHTS["due"] * due),
                ("energy", SCORE_WEIGHTS["energy"] * energy_fit),
                ("hunger", SCORE_WEIGHTS["hunger"] * hunger_fit),
                ("focus", SCORE_WEIGHTS["focus"] * focus_fit),
                ("resume", SCORE_WEIGHTS["resume"] * resume_bonus),
                ("novelty", SCORE_WEIGHTS["novelty"] * novelty),
                ("motive", motive_add),
            ]
            dominant = max(components, key=lambda pair: pair[1])[0]
            phrases = {
                "priority": "这件事优先级比较高，先做它",
                "due": "这件事到该做的时间了",
                "energy": "现在精力适合做这件事",
                "hunger": ("到了吃饭的点，先吃饭" if goal_type == "meal"
                           else "现在不饿，适合做点别的"),
                "focus": "现在专注状态适合做这件事",
                "resume": "刚才的事还没做完，接着做",
                "novelty": "今天还没做过这类事，换换花样",
                "motive": "对这类活动感兴趣，想多做一点",
            }
            return phrases.get(dominant, fallback)
        except Exception:
            return fallback

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
    def _meal_priority_candidate(self, scored: List[Tuple[float, Dict[str, Any]]],
                                 snapshot: Any, cur: int) -> Optional[Tuple[float,
                                                                           Dict[str, Any]]]:
        """D2① 极饿吃饭护栏：新调制开 + hunger > 0.85 → meal 候选最高。

        只重排**已按 §5.2 公式算出的候选**，不修改公式/权重；睡眠环与「到点硬约定」
        优先级不变（前者由 tick 步 3/5 直接返回，后者在步 3 已抢占当前 episode，
        这里再防御性让位）；``emotion_modulation_enabled=false`` 恒返回 None = 旧行为。
        """
        hunger = _as_number(snapshot.get("hunger") if isinstance(snapshot, dict) else None, 0.0)
        if hunger is None or hunger <= HIGH_HUNGER_MEAL_GUARD:
            return None
        if self._in_sleep_ring(cur):
            return None  # 睡眠最高层：睡眠窗内不排活动
        for _score, cand in scored:
            window = cand.get("window")
            if (cand.get("kind") == "commitment" and window
                    and window[0] <= cur):
                return None  # 硬约定到点最高层（正常已在步 3 开播）
        for score, cand in scored:
            if str(cand.get("goal_type") or "") == "meal":
                return score, cand
        return None

    def _choose_candidate(self, scored: List[Tuple[float, Dict[str, Any]]],
                          snapshot: Any, cur: int, rng: random.Random
                          ) -> Tuple[Dict[str, Any], float,
                                     List[Tuple[float, Dict[str, Any]]]]:
        """选下一候选：极饿 meal 护栏优先；否则 top2-3 softmax（§5.2 原逻辑）。"""
        guarded = self._meal_priority_candidate(scored, snapshot, cur)
        if guarded is not None:
            return guarded[1], guarded[0], [guarded]
        # P3 §14.6：约 10% 受控探索——在合法替代项（已过可行性过滤）中探索；
        # 启动循环仍逐项核窗口/最小块，探索不绕过时间/状态/次数限制。
        # 复核 P2-2：只在 top 之后至多 EXPLORATION_WINDOW 个、且与 top1 分差
        # ≤ EXPLORATION_SCORE_GAP 的替代项中抽；无合格项则回落 softmax，不盲抽长尾。
        if len(scored) > 3 and rng.random() < EXPLORATION_RATE:
            top_score = scored[0][0]
            pool = [(s, c) for s, c in scored[3:3 + EXPLORATION_WINDOW]
                    if s >= top_score - EXPLORATION_SCORE_GAP]
            if pool:
                pick_score, pick_cand = rng.choice(pool)
                return pick_cand, pick_score, scored[:3]
        temperature = max(0.01, self._cfg_float("sampling_temperature",
                                                DEFAULT_SAMPLING_TEMPERATURE))
        # top2-3 softmax 加权采样（不是 argmax）；选中候选不可启（min_block 不足 /
        # 硬约定未到点）→ 从 top 余下候选按分数序改选（§5.3「改短活动或直接跳过」）
        return self._softmax_sample(scored, rng, temperature)

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
        # P1：事件按自身 min/max 契约采样（不盲用 goal_type 画像 min_block，避免 5–12
        # 分钟零食被娱乐 15 / 运动 20 最小块拒绝）；画像仍提供状态/注意力属性。
        if c["kind"] == "event":
            lo = int(c.get("min_minutes") or 0) or 3
            hi = int(c.get("max_minutes") or 0) or max(lo, 30)
            hi = max(hi, lo)
            # P3 §14.6：偏好在同一事件合法时长区间内温和偏向长/短端——采样中心
            # 移动 ≤ 区间宽度 15%；不扩展 min/max、不推迟硬约定或睡眠。
            shift = 0.0
            prefs = getattr(self, "_activity_preferences", None)
            if prefs is not None and hi > lo:
                try:
                    pref = prefs.preference_for(c)
                    if pref.get("source") in ("llm", "rule_fallback"):
                        shift = (_clamp(float(pref.get("affinity", 0.0)), -1.0, 1.0)
                                 * PREFERENCE_DURATION_SHIFT_RATIO * (hi - lo))
                except Exception:
                    shift = 0.0
            if shift != 0.0:
                center = (lo + hi) / 2.0 + shift
                duration = int(round(rng.gauss(center, max(1.0, (hi - lo) / 4.0))))
                duration = max(lo, min(hi, duration))
            else:
                duration = rng.randint(lo, hi)
            if window is not None:
                duration = min(duration, window)
            if duration < lo:
                return None  # 剩余不足事件最小块：换候选，不硬塞
            return int(duration)
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
        # PR-B2：候选阶段刷新持续动机（衰减/到期/加强/新增；失败本拍跳过）。
        # preview 是读态纯函数，不走此写入口。
        if self._motive_store is not None:
            try:
                self._motive_store.refresh(now, state=snapshot)
            except Exception as exc:
                logger.debug("动机刷新失败（本拍跳过，不阻塞）：%s", exc)
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
        counts.update(self._idle_score_context(candidates, date, cur))
        scored: List[Tuple[float, Dict[str, Any]]] = []
        for cand in candidates:
            score = self._score_candidate(cand, snapshot, cur, boundary, counts, None)
            score += rng.uniform(-SCORE_NOISE, SCORE_NOISE)
            scored.append((score, cand))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen, _score, top = self._choose_candidate(scored, snapshot, cur, rng)
        # P1 §5.5：每个真实决策边界记录一次来源过滤/抽中/等待诊断（只记 ID 与
        # 分量，不复制提示词；preview 读态不走此写入口）。
        source_counts: Dict[str, int] = {}
        for cand in candidates:
            key = str(cand.get("kind") or "?")
            source_counts[key] = source_counts.get(key, 0) + 1
        self._log("rolling.decision", date=date, minute=cur,
                  sources=source_counts,
                  event_filter=getattr(self, "_last_event_filter_stats", None),
                  chosen=str(chosen.get("name") or ""), chosen_kind=chosen.get("kind"),
                  chosen_event_id=chosen.get("event_id"),
                  top3=[{"name": c.get("name"), "kind": c.get("kind"),
                         "score": round(s, 4)} for s, c in top],
                  boundary_minutes=boundary,
                  idle_minutes_today=round(float(counts["_idle_minutes"]), 1))
        top_ids = {id(c) for _s, c in top}
        # P0（2026-11-09 滚动日程计划）：有效抽中项必须排启动首位，确实无法启动才进入
        # 有序后备（修复抽样结果被按分数头部忽略的确定缺陷）。
        order: List[Dict[str, Any]] = [chosen] if chosen is not None else []
        order += [c for _s, c in top if id(c) != id(chosen)]
        order += [c for _s, c in scored if id(c) not in top_ids and id(c) != id(chosen)]
        seen_ids: set = set()
        for cand in order:
            if id(cand) in seen_ids:
                continue
            seen_ids.add(id(cand))
            if cand["kind"] == "commitment" and cand["window"] and cand["window"][0] > cur:
                continue  # 硬约定时间固定：不到点不开播（tick 步 3 到点处理，红线 2）
            # P0：软意图前瞻入围但不到可执行窗（仅有限弹性）→ 不提前消耗晚些时候的内容。
            if cand["kind"] == "intent" and cand.get("window") \
                    and cur < cand["window"][0] - SOFT_INTENT_EARLY_MINUTES:
                continue
            # P0：跨来源餐食去重——近期已用餐/同餐次已满足则不再排该 meal 意图。
            if cand["kind"] == "intent" and str(cand.get("goal_type")) == "meal" \
                    and self._meal_intent_satisfied(date, cur, cand):
                continue
            duration = self._sample_block(cand, snapshot, cur, boundary, rng)
            if duration is None:
                continue
            # P1：启动失败（一次性认领失败/写库失败）→ 换有序后备，不静默吞掉本拍。
            if self._start_episode_for_candidate(cand, duration, date, cur, now, snapshot,
                                                 scored, rng, report):
                return
        self._log("rolling.no_block_started", reason="min_block_insufficient",
                  candidates=len(scored),
                  event_filter=getattr(self, "_last_event_filter_stats", None))
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
                                     report: Dict[str, Any]) -> bool:
        """启动成功返回 True；认领失败/写库失败返回 False（调用方换有序后备）。"""
        profile = activity_profiles.get_profile(cand["profile_key"])
        source = {"commitment": "commitment", "intent": "rolling", "anchor_meal": "rolling",
                  "resume": "resume", "idle": "rolling", "event": "rolling",
                  "continuation": "rolling"}.get(cand["kind"], "rolling")
        # PR2.5 修复（2026-10-26），交接 §五.18 D1：开播/恢复前抓当前 active 行，
        # 写入成功后若被 close-active 挤掉（interrupted 且 interrupted_by=新行 id）
        # 则补发布 life.episode_interrupted 与状态回写（原路径静默）
        pre_active = self._safe_active_episode()
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
                                    "seed_date": self._rng_date,
                                    # 3.17.7 改进1：人话化抽取原因（纯旁路展示字段）
                                    "draw_reason_zh": self._draw_reason_zh(
                                        cand, snapshot, cur, date)}
        if cand.get("intent_item_id"):
            metadata["intent_item_id"] = cand["intent_item_id"]
        if cand.get("note"):
            metadata["note"] = cand["note"]
        eid: Optional[str] = None
        if cand["kind"] == "event":
            # P1 统一入口（§5.1 步5/6）：先持久认领一次性，再启动；定义快照随
            # episode（库替换后仍按快照结算）；启动失败/被打断不返池。普通短事件
            # 完成即结束——不建长期父目标。
            eid = self._start_event_episode(cand, duration, date, cur, now, snapshot,
                                            metadata, attention, profile, rng, pre_active)
        elif cand["kind"] == "resume" and cand.get("resume_of"):
            # PR1 P0 修复（2026-10-26），计划书 §三十四/定稿 §2 0.4：恢复行透传原
            # 随机事件 event_key——被抢占的随机事件恢复后再关闭时仍能按 key 取
            # state_deltas（一次性应用由 metadata.state_deltas_applied 标记保证）
            origin = self._db.get_episode(str(cand["resume_of"])) or {}
            origin_meta = (origin.get("metadata")
                           if isinstance(origin.get("metadata"), dict) else {})
            # PR2.5 修复（2026-10-26），交接 §五.18 D1：origin 已按 close-active 路径
            # 应用过 deltas（state_deltas_applied 标记在案）→ 不再透传 event_key，恢复行
            # 关闭时不会二次应用（防双应用沿用同一标记口径）。
            # 2026-10-27 event-review F1：**未应用**时必须把整个结算快照随恢复行带走
            # （state_deltas / event_id / 版本），否则事件库替换移除旧 key 后 finish
            # 无法回查、增量整份漏记。``state_deltas_applied`` 本身绝不复制。
            # PR-B2：恢复行透传父目标（episode 恢复 ≠ 新目标；paused 目标重新 active）
            carried_goal = str(origin_meta.get("parent_goal_id") or "")
            if carried_goal:
                metadata["parent_goal_id"] = carried_goal
                self._ensure_parent_goal(str(origin.get("name") or ""),
                                         str(origin.get("goal_type") or ""),
                                         existing_goal_id=carried_goal)
            if not origin_meta.get("state_deltas_applied"):
                if origin_meta.get("event_key"):
                    metadata["event_key"] = str(origin_meta["event_key"])
                snapshot_deltas = origin_meta.get("state_deltas")
                if isinstance(snapshot_deltas, dict) and snapshot_deltas:
                    metadata["state_deltas"] = dict(snapshot_deltas)
                for key in ("event_id", "event_catalog_version", "catalog_version",
                            "event_name"):
                    if origin_meta.get(key) is not None:
                        metadata[key] = origin_meta[key]
            eid = self._db.resume(
                str(cand["resume_of"]),
                planned_start_minute=cur,
                planned_end_minute=cur + duration,
                actual_start_minute=cur, attention_mode=attention,
                state_before=self._state_brief(snapshot),
                metadata=metadata)
            if eid:
                self._handle_squeezed_active(pre_active, now, eid)
                self._publish(EVENT_EPISODE_RESUMED, episode_id=eid,
                              resume_of=cand["resume_of"], name=origin.get("name"),
                              goal_type=origin.get("goal_type"))
        else:
            # PR-B2（规格 §1）：「可能延续」类活动启动时关联/创建长期目标
            # （确定性分类；拿不准 → 无延续价值，不建目标）。跨日继续候选自带
            # parent_goal_id → 复用原目标。明确承诺（commitment）不走此路。
            parent_goal_id: Optional[str] = None
            if cand.get("parent_goal_id"):
                parent_goal_id = self._ensure_parent_goal(
                    str(cand["name"]), str(cand["goal_type"]),
                    existing_goal_id=str(cand["parent_goal_id"]))
            elif cand["kind"] != "commitment" and classify_activity_continuity(
                    cand.get("name"), cand.get("goal_type"),
                    extra_keywords=self._continuity_extra_keywords()) == "continuable":
                parent_goal_id = self._ensure_parent_goal(
                    str(cand["name"]), str(cand["goal_type"]))
            if parent_goal_id:
                metadata["parent_goal_id"] = parent_goal_id
            eid = self._db.start_episode(
                schedule_date=date, name=str(cand["name"]), goal_type=str(cand["goal_type"]),
                source=source, planned_start_minute=cur, planned_end_minute=cur + duration,
                interruptibility=cand["interruptibility"], switch_cost=profile.switch_cost,
                attention_mode=attention, state_before=self._state_brief(snapshot),
                metadata=metadata,
            )
            if eid:
                self._handle_squeezed_active(pre_active, now, eid)
                self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=cand["name"],
                              goal_type=cand["goal_type"], planned_start_minute=cur,
                              planned_end_minute=cur + duration, source=source)
        if eid:
            self._log("rolling.episode_started", episode_id=eid, kind=cand["kind"],
                      name=str(cand["name"]), duration_minutes=duration, top3=top3,
                      draw_reason_zh=metadata.get("draw_reason_zh"))
            report["actions"].append(f"started_{cand['kind']}")
            return True
        # 启动失败换后备是正常取舍（时段临界/最小块不足/认领失败）——debug 级，
        # 决策诊断已落 rolling.decision / no_block_started，不刷 warning。
        logger.debug("rolling_planner: episode 启动失败（换后备候选 kind=%s）", cand.get("kind"))
        return False

    def _start_event_episode(self, cand: Dict[str, Any], duration: int, date: str,
                             cur: int, now: datetime, snapshot: Any,
                             metadata: Dict[str, Any], attention: str, profile: Any,
                             rng: random.Random, pre_active: Optional[Dict[str, Any]]
                             ) -> Optional[str]:
        """P1：统一入口的事件启动——时段复核 → 一次性持久认领 → 快照写库 →
        认领回执 → 冷却/当日用量落盘。任一步失败返回 None（不返池、不伪造成功）。"""
        ev = cand.get("_event_def") or {}
        catalog = getattr(self, "_event_catalog", None)
        if catalog is None or not ev:
            return None
        event_key = str(cand.get("event_id") or ev.get("event_id") or ev.get("key") or "")
        templates = [str(t) for t in (ev.get("templates") or ()) if str(t or "").strip()]
        template = rng.choice(templates) if templates else str(ev.get("name") or "")
        try:
            snap_meta = catalog.metadata_for_event(ev, trigger="rolling_event",
                                                   description=template)
        except Exception:
            logger.warning("事件快照失败，本次不执行", exc_info=True)
            return None
        if not isinstance(snap_meta, dict) or not snap_meta:
            return None
        once = str(ev.get("pool_kind") or "") == "one_shot"
        if once and not snap_meta.get("event_generation_id"):
            return None
        # 抽选与开始之间可能跨时段边界/热重载：启动前再核一次（认领必须在核后）。
        from .event_slots import time_slot_of, slot_remaining_minutes, event_allowed_in_slot
        _tz = getattr(self._tz, "_tz", None)
        if not event_allowed_in_slot(ev, time_slot_of(now, _tz)):
            return None
        if int(duration) > slot_remaining_minutes(now, _tz):
            return None
        if once and not catalog.claim_event(ev, now=now):
            self._log("rolling.event_not_claimed", event_id=event_key)
            return None
        metadata.update(snap_meta)
        metadata["origin_kind"] = str(cand.get("origin_kind") or "")
        for key in ("source_file", "definition_version", "tags"):
            if ev.get(key) is not None:
                metadata[key] = ev[key]
        try:
            eid = self._db.start_episode(
                schedule_date=date, name=str(cand["name"]), goal_type=str(cand["goal_type"]),
                source="rolling", planned_start_minute=cur, planned_end_minute=cur + int(duration),
                interruptibility=cand["interruptibility"], switch_cost=profile.switch_cost,
                attention_mode=attention, state_before=self._state_brief(snapshot),
                metadata=metadata,
            )
        except Exception:
            if once:
                catalog.finish_claim(ev, "start_failed", reason="episode_start_error")
            logger.warning("事件启动失败，本次抽选不返池", exc_info=True)
            return None
        if once:
            catalog.finish_claim(ev, "started" if eid else "start_failed", str(eid or ""),
                                 reason="" if eid else "episode_start_rejected")
        if not eid:
            return None
        # 冷却/当日用量：启动即记（中断不返还），持久化跨热重载。
        if not hasattr(self, "_event_last_used"):
            self._load_event_usage()
        self._event_last_used[event_key] = now.timestamp()
        self._event_day_counts[event_key] = int(self._event_day_counts.get(event_key, 0) or 0) + 1
        self._save_event_usage(date)
        self._handle_squeezed_active(pre_active, now, eid)
        self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=cand["name"],
                      goal_type=cand["goal_type"], planned_start_minute=cur,
                      planned_end_minute=cur + int(duration), source="rolling")
        self._log("rolling.event_started", episode_id=eid, event_id=event_key,
                  origin_kind=cand.get("origin_kind"), pool_kind=ev.get("pool_kind"),
                  duration_minutes=int(duration))
        return str(eid)

    # ── 跨日事务（PR-B2，2026-11-07；规格 §1 / 计划书 §5 H1 / §5.1）─────
    def _continuity_extra_keywords(self) -> Any:
        """分类关键词的可配置补充（注入式只读口；失败 → 空，保守缺省不变）。"""
        provider = self._continuity_keywords_provider
        if provider is None:
            return ()
        try:
            return provider() or ()
        except Exception:
            return ()

    def _ensure_parent_goal(self, name: str, goal_type: str, *,
                            existing_goal_id: Optional[str] = None) -> Optional[str]:
        """「可能延续」活动启动时关联/创建长期目标（复用 GoalManager.create_goal，
        来源标 simulated_activity，不伪装现实经历；不造第二个目标表）。

        已有目标（跨日继续/恢复透传）→ 复用并 resume；同名 simulated_activity
        目标已存在 → 复用；都没有 → 新建。失败只告警：episode 照常启动、无父目标。
        """
        gm = self._goal_manager
        if gm is None:
            return None
        try:
            if existing_goal_id:
                goal = gm.get_goal(str(existing_goal_id))
                if goal is None:
                    return None
                status = getattr(goal.status, "value", goal.status)
                if status not in ("active", "paused"):
                    return None  # 已完成/已取消的目标不复活
                if status == "paused":
                    gm.resume_goal(goal.goal_id)
                return goal.goal_id
            for status in (GoalStatus.ACTIVE, GoalStatus.PAUSED):
                for goal in gm.get_all_goals(status=status):
                    params = goal.parameters or {}
                    if params.get("source") == "simulated_activity" \
                            and str(params.get("activity_name") or "") == name:
                        if status == GoalStatus.PAUSED:
                            gm.resume_goal(goal.goal_id)
                        return goal.goal_id
            goal = gm.create_goal(
                name=name,
                description=f"来自虚拟日程活动「{name}」（simulated_activity，非现实操作）",
                goal_type=goal_type or "daily_routine",
                creator_id="system", chat_id="global", priority="low",
                parameters={"source": "simulated_activity", "activity_name": name},
            )
            self._log("rolling.parent_goal_created", goal_id=goal.goal_id, name=name)
            return goal.goal_id
        except Exception as exc:
            logger.warning("rolling_planner: 父目标关联失败（episode 照常启动）：%s", exc)
            return None

    def _settle_parent_goal(self, episode: Dict[str, Any], outcome: str, *,
                            progress: Any = None) -> None:
        """episode 终结 → 父目标结算（best-effort）。

        completed → 目标完成（活动做完了，不长留待办）；paused/interrupted/
        abandoned → 目标保留 paused（**活动中断/放弃 ≠ 目标消失**，规格 §1），
        进度按 episode 真实 progress 折算（上限 95，不虚构完成）。
        """
        gm = self._goal_manager
        meta = episode.get("metadata") if isinstance(episode, dict) else None
        gid = str((meta or {}).get("parent_goal_id") or "") \
            if isinstance(meta, dict) else ""
        if gm is None or not gid:
            return
        try:
            if outcome == "completed":
                gm.complete_goal(gid)
            else:
                gm.pause_goal(gid)
                p = _as_number(progress, None)
                if p is not None:
                    gm.update_goal_progress(gid, min(95, max(0, int(round(p * 100)))))
        except Exception as exc:
            logger.warning("rolling_planner: 父目标结算失败（goal=%s）：%s", gid, exc)

    def _continuation_candidate(self, date: str, cur: int,
                                snapshot: Any) -> Optional[Dict[str, Any]]:
        """跨日继续候选（规格 §1）：paused 且有延续价值的 simulated_activity 目标，
        按精力/完成度/近期重复度加权挑 **至多 1 条**；energy 低于阈值不出现；
        睡眠环内不出现（时间因素另由 _sample_block 的最小块/硬边界检查兜底）。
        今日已继续过（同父目标 active/completed）的目标不再重复入围。
        """
        gm = self._goal_manager
        if gm is None or self._in_sleep_ring(cur):
            return None
        energy = _as_number(snapshot.get("energy"), 1.0)
        if energy is None:
            energy = 1.0
        if energy < self._cfg_float("low_energy_threshold", DEFAULT_LOW_ENERGY_THRESHOLD):
            return None
        try:
            goals = gm.get_all_goals(status=GoalStatus.PAUSED)
        except Exception as exc:
            logger.debug("跨日目标读取失败（本拍无继续候选）：%s", exc)
            return None
        done_ids: set = set()
        try:
            for row in self._db.episodes_today(date):
                meta = row.get("metadata")
                if isinstance(meta, dict) and meta.get("parent_goal_id") \
                        and row.get("status") in ("active", "completed"):
                    done_ids.add(str(meta["parent_goal_id"]))
        except Exception as exc:
            logger.debug("今日 episode 读取失败（继续候选重复度按 0 处理）：%s", exc)
        best: Any = None
        best_weight = -1.0
        for goal in goals:
            params = goal.parameters or {}
            if params.get("source") != "simulated_activity":
                continue
            if goal.goal_type in ("sleep", "meal", "pending_commitment"):
                continue
            if goal.goal_id in done_ids:
                continue
            progress = max(0.0, min(100.0, float(goal.progress or 0))) / 100.0
            if progress >= 1.0:
                continue
            repeats = max(0, int(goal.execution_count or 0))
            # 温和加权：精力贴合 + 完成度（快做完的优先收尾）+ 近期重复度惩罚
            weight = 0.5 * energy + 0.3 * progress + 0.2 / (1.0 + repeats)
            if weight > best_weight:
                best, best_weight = goal, weight
        if best is None:
            return None
        goal_type = str(best.goal_type or "daily_routine")
        return {
            "kind": "continuation", "name": f"继续{best.name}", "goal_type": goal_type,
            "priority_score": _clamp(0.3 + 0.2 * best_weight, 0.0, 0.6),
            "min_minutes": 0, "max_minutes": 0,
            "interruptibility": 0.6, "profile_key": goal_type,
            "window": None, "resume_of": None, "resume_bonus": 0.0,
            "intent_item_id": None, "note": "之前没做完的事，今天接着试试",
            "parent_goal_id": best.goal_id,
        }

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
            # 2026-10-27 event-review F2：DB updated_at 是 naive ISO，生产 now 为 aware；
            # 必须与同函数 last_meal/last_sleep 一样传时区再比较。
            updated = self._parse_ts(row.get("updated_at"),
                                      tz=getattr(self._tz, "_tz", None))
            age_minutes = int((now - updated).total_seconds() / 60.0) if updated else expire + 1
            progress = _as_number(row.get("progress"), 0.0) or 0.0
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            if meta.get('event_pool_kind') == 'one_shot':
                continue  # a consumed one-shot never enters a recovery pool
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
                    # PR-B2（规格 §1）：resume_pool 的 abandon 只结束 episode，
                    # 父目标保留 paused（活动中断/放弃 ≠ 目标消失）
                    self._settle_parent_goal(row, "abandoned",
                                             progress=row.get("progress"))
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
                            report: Dict[str, Any], rng: random.Random,
                            context: Optional[str] = None) -> bool:
        """转场/空闲掷骰；命中且约束全过 → 开一条联动事件 episode。返回是否插入。

        P1（2026-11-09 §7.2）：本入口只保留代码内置·模块联动两项（watch_bili/
        browse_qq）——普通状态/生成事件已提升为主候选（_event_candidates），不再
        走 8% 稀有事件骰。``max_random_events_per_day`` 语义迁为联动事件合计日上限
        （默认 3，0 真正禁用）；计数/4h 去重从持久记录恢复，热重载不重置。
        创建时把 ``metadata.state_deltas`` + ``event_id`` + ``event_catalog_version``
        快照进 episode（库替换后仍按快照结算；见 :meth:`_random_event_deltas`）。
        """
        rate = self._cfg_float("random_event_rate", DEFAULT_RANDOM_EVENT_RATE)
        if rate <= 0 or rng.random() >= rate:
            return False
        # P0：0 必须真正禁用（原 max(1,…) 使 0 仍触发一次）；旧值 0 的禁用意图保留。
        limit = self._cfg_int("max_random_events_per_day", DEFAULT_MAX_RANDOM_EVENTS_PER_DAY)
        if limit <= 0 or self._random_used >= limit:
            return False
        catalog = getattr(self, "_event_catalog", None)
        event: Optional[Dict[str, Any]] = None
        if catalog is not None:
            # P1：只从联动两项取池；模块门控/时段/冷却仍由 pick_event 内部把关。
            linked = [e for e in self._catalog_active_events()
                      if str(e.get("event_id") or e.get("key") or "")
                      in MODULE_LINKED_EVENT_IDS]
            if not linked:
                return False
            try:
                event = catalog.pick_event(
                    rng, self._state_brief(snapshot), self._random_last,
                    now=now, context=context, events=linked)
            except Exception as exc:
                logger.warning("事件库取池失败（本次跳过）：%s", exc)
                event = None
            if not isinstance(event, dict) or not event.get('name'):
                return False  # do not bypass catalog cooldown/module gates via an unfiltered fallback
        if isinstance(event, dict) and event.get("name"):
            event_key = str(event.get("event_id") or event.get("key") or "")
            templates = [str(t) for t in (event.get("templates") or ()) if str(t or "").strip()]
            template = rng.choice(templates) if templates else str(event.get("name") or "")
            duration = event.get("duration_minutes")
            try:
                duration = int(duration) if duration is not None else \
                    rng.randint(int(event["min_minutes"]), int(event["max_minutes"]))
            except (TypeError, ValueError, KeyError):
                duration = rng.randint(5, 30)
            try:
                metadata = catalog.metadata_for_event(event, description=template)
            except Exception:
                logger.warning('随机事件快照失败，本次不执行', exc_info=True)
                return False
            if not isinstance(metadata, dict) or (event.get('pool_kind') == 'one_shot'
                    and not metadata.get('event_generation_id')):
                return False
            metadata.update({"trigger": "random_event", "event_key": event_key,
                             "description": template})
            event = dict(event)
            event["key"] = event_key or event.get("key", "")
        else:
            # 服务未装配：内置池 + 时段过滤 + 4h 去重 + 时长采样（降级路径同样受时段约束）。
            from .event_slots import time_slot_of, slot_remaining_minutes, event_allowed_in_slot
            _tz = getattr(self._tz, "_tz", None)
            slot = time_slot_of(now, _tz)
            remaining = slot_remaining_minutes(now, _tz)
            eligible = [e for e in RANDOM_EVENT_POOL
                        if str(e.get("key") or "") in MODULE_LINKED_EVENT_IDS
                        and event_allowed_in_slot(e, slot)
                        and int(e.get("min_minutes", 5)) <= remaining
                        and (e["key"] not in self._random_last
                             or (now - self._random_last[e["key"]]).total_seconds()
                             >= RANDOM_EVENT_DEDUP_MINUTES * 60)]
            if not eligible:
                return False  # 时段不符或同事件 4h 内冷却中——不绕过时段规则
            picked = rng.choice(eligible)
            duration = rng.randint(int(picked["min_minutes"]),
                                   min(int(picked["max_minutes"]), remaining))
            template = rng.choice(picked["templates"])
            event = dict(picked)
            event_key = str(picked["key"])
            metadata = {"trigger": "random_event", "event_key": event_key,
                        "description": template}
        from .event_slots import time_slot_of, slot_remaining_minutes, event_allowed_in_slot
        _tz = getattr(self._tz, "_tz", None)
        # 自主刷视频时长（2026-11-10 刷视频批）：真正控制“刷视频行为持续时长”的是
        # episode 的 duration_minutes（库里 watch_bili 仍是 10–25）。代码侧覆盖为
        # 25–55 分钟，不修改内置库文件/文案；剩余时段不足下限则本拍如实不启动。
        if event_key == WATCH_BILI_EVENT_ID:
            watch_duration = sample_watch_bili_duration(
                rng, slot_remaining_minutes(now, _tz))
            if watch_duration is None:
                return False
            duration = watch_duration
            # episode 快照与 planned_end 同口径：库内 watch_bili 的 duration_minutes
            # 仍是旧值（10–25），不覆盖会让面板/历史显示一个比 episode 实际计划
            # 时长更短的数，误以为视频几分钟就结束（实际由多支视频续投填满）。
            metadata["duration_minutes"] = int(watch_duration)
        # 抽选与开始之间可能跨越 08:00/16:00/00:00 或热重载失配：启动前再核一次时段，
        # 一次性 claim 必须在此校验通过后才执行，避免未真正开始就消耗一次性事件。
        if not event_allowed_in_slot(event, time_slot_of(now, _tz)):
            return False
        if int(duration) > slot_remaining_minutes(now, _tz):
            return False
        profile = activity_profiles.get_profile(event["goal_type"])
        once = event.get('pool_kind') == 'one_shot'
        if once and (catalog is None or not catalog.claim_event(event, now=now)):
            self._log('rolling.random_event_not_claimed', event_key=event_key)
            return False
        try:
            eid = self._db.start_episode(
                schedule_date=date, name=event["name"], goal_type=event["goal_type"],
                source="random", planned_start_minute=cur, planned_end_minute=cur + int(duration),
                interruptibility=event["interruptibility"], switch_cost=profile.switch_cost,
                attention_mode="now", state_before=self._state_brief(snapshot),
                metadata=metadata,
            )
        except Exception:
            if once:
                catalog.finish_claim(event, 'start_failed', reason='episode_start_error')
            logger.warning('随机事件启动失败，本次抽选不返池', exc_info=True)
            return False
        if once:
            catalog.finish_claim(event, 'started' if eid else 'start_failed', str(eid or ''),
                                 reason='' if eid else 'episode_start_rejected')
        if not eid:
            return False
        self._random_used += 1
        self._random_last[event_key] = now
        # P1：联动日配额/4h 去重持久化（热重载不重置当天配额）。
        self._save_event_usage(date)
        self._publish(EVENT_RANDOM_EVENT, name=event["name"], duration_minutes=duration,
                      source="random", description=template)
        self._publish(EVENT_EPISODE_STARTED, episode_id=eid, name=event["name"],
                      goal_type=event["goal_type"], planned_start_minute=cur,
                      planned_end_minute=cur + int(duration), source="random")
        self._log("rolling.random_event", episode_id=eid, event_key=event_key,
                  duration_minutes=duration)
        report["actions"].append(f"random_event:{event_key}")
        return True

    def _roll_daily_counters(self, date: str) -> None:
        if date != self._random_date:
            self._random_date = date
            self._random_used = 0
            self._random_last = {}
            # P1 §7.2：联动日配额/4h 去重从持久记录恢复（重载/跨日不重置当天配额；
            # 文件日期不是当天 → 按 0 起算，不沿用昨日计数）。
            path = self._event_usage_path()
            if path is not None and path.exists():
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                    if str(doc.get("linked_date") or "") == date:
                        self._random_used = max(0, int(doc.get("linked_used") or 0))
                        for key, raw in (doc.get("linked_last") or {}).items():
                            parsed = self._parse_ts(raw, getattr(self._tz, "_tz", None))
                            if parsed is not None:
                                self._random_last[str(key)] = parsed
                except Exception as exc:
                    logger.debug("联动配额持久恢复失败（按 0 起算）：%s", exc)
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
        counts.update(self._idle_score_context(candidates, date, cur))
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
