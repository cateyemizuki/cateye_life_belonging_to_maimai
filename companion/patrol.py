"""主动巡检调度器（来源：虚拟陪伴插件 scheduler.py；2026-11-04 主动行为重写）。

单条 asyncio 循环（可打断睡眠：wait_for(stop_event.wait(), timeout)），每轮按优先级判定：
日切 → 静默(fail-safe) → 非夜预算 → 早晚安状态观察 →
想念（**生活状态 × 情绪 × 好感**驱动的概率触发 + LLM 把关）。

**2026-11-04 重写（用户直派规格）**：旧「沉默阈值想念」与「日程巡检（活动分享/日常
搭话）」两条路径合并重写为一条状态驱动想念触发——概率公式与各因子见
:mod:`companion.miss_trigger`（显式可审计、全部有界）；日程相关的主动巡检触发路径
（activity/daily 分支与 ``_trigger_activity``/``_trigger_daily`` 等）**整条删除**，
reactive 日程注入不受影响（planning/inject* 他人负责）。想念不再要求用户沉默超阈值、
不再每日一次闸门（``miss_sent_today`` 降为观察标记，面板继续消费）；每日预算 5、
最小间隔、用户冷却、静默/午休闸与 LLM 把关逐字保留。沉默事件
（``relation.silence_detected``，M3）独立保留，阈值仍读 ``miss_silence_hours``。

**静默窗口径（2026-10-20 用户指示，N5「留空跟随」收官；2026-10-21 §24.2 任务B 真实化）**：
静默窗恒复用作息锚点——**插件实例路径优先真实睡眠环**（schedule.db 夜晚睡条目经
``planning/schedule_anchors`` 解析，读取即解析；赖床顺延后锚点随移），日程不可用时回退
``[schedule]`` 入睡/起床配置锚点（原 ``silence_start/silence_end`` 显式覆盖键已删除，旧值
由迁移器丢弃）；生效值统一由 :func:`resolve_silence_window` 解析（planning 的晚安期限/
早安基准、emotion 夜窗、qzone 的空间静默闸同源），格式非法一律 fail-safe（视作静默，
宁可不发）。

**与早晚安的关系（2026-10-10 配置整理轮口径）**：早安/晚安只有一条驱动链——
planning 的 L2 漂移机制（[planning_proactive]）。本巡检器**从不**触发问候，
只读取 affection 的按天标记（planning 触发成功后写入）做**记录性观察**：标记已置位
写进决策行的独立字段 ``greeting_observation``（2026-10-11 裁决 A-17：不占
SKIP_PRIORITY、不盖掉真实归因），**不构成去重闸门**（patrol 本就没有问候
触发路径，标记置位与否不改变本侧行为）——两模块不会各发一条，靠的是 planning
自查 + affection 已发标记闭环。patrol 自身可发的只有**想念**一类
（2026-11-04 重写后；日程分享/日常搭话已删除）；早/午/晚安问候一律不占陪伴侧预算。

**预算口径**：``[patrol].daily_max_speak`` 即上限（不再有独立上限字段）。
实际拦截闸门 = bus 主动打扰预算（ProactiveBudget，1/次，含 patrol/planning/emotion/
video/harass_report 各渠道，仅目标陪伴私聊；早/晚安免预算）；本巡检器另维护 affection
发言计数（0.5/次、仅 patrol 触发）作候选门控。F4（2026-11-04）起决策行 budget_used/
budget_limit 改显 bus 口径（budget_basis 注明），affection 计数保留为 speak_count_affection。

所有触发统一走 ctx.maisaka.proactive.trigger（人格化主动回合，不直发文本），
计 0.5 发言数；replyer 真实回复后由 Hook 补计 0.5（credit_reply_half）。

决策日志每轮恰好一条：SKIP_PRIORITY 决定多原因时保留哪个；
整轮异常时节流补一条 tick_error 决策行 + warning（F5，至多 10 分钟一次）；
触发前先看一眼陪伴对象电脑（cateye_client），屏幕旁白发布到 ScreenContextStore
（尾随锚点注入由 components 的请求 Hook 完成），触发未入队则撤掉旁白。

日程查询（planning.schedule_query.ScheduleQuery）现仅服务午休静默判定与静默窗真实
锚点解析；想念触发不再读当前活动（防日程 priming）。想念文案在 companion.miss_trigger
（模块内常量，不进 [prompts] 段）；屏幕旁白文案仍读 suite_prompts。
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional, Tuple

from core.diag import DecisionLogger
from core.llm_utils import LLMService
from core.time_utils import hours_between, parse_hhmm, parse_hours_range
from planning.schedule_anchors import (SOURCE_REAL_SCHEDULE, RealAnchors,
                                       real_sleep_anchors)
from planning.schedule_items import is_night_sleep_item
from planning.schedule_query import ScheduleQuery
from planning.time_utils import is_in_day_frame, is_in_time_window
from suite_prompts import (
    SCREEN_NARRATION_DEFAULT,
    SCREEN_NARRATION_FAILED_DEFAULT,
    SCREEN_NARRATION_OFFLINE_DEFAULT,
    prompt,
    render,
)
from tools.proactive_turn import register_turn

from .initiative_profile import InitiativeMemory, evaluate_miss
from . import miss_trigger
from .affection import AffectionManager
from .cateye_client import PEEK_OFFLINE
from .screen_context import ScreenContextStore, format_narration

logger = logging.getLogger(__name__)

# 想念被 LLM 驳回后，多久之内不再重复发起把关判断（分钟）


#: 沉默事件（relation.silence_detected，M3/B1）频控：每日 ≤2 次、间隔 ≥4 小时（§4.2 / §6.2 A）



#: PR-C1 H5：经历分享动机对想念候选概率的**轻微**加成比例（×strength，乘法只升不降；
#: 与 planner 侧 MOTIVE_BONUS_RATIO 同量级）。概率封顶沿用 evaluate_miss 的 0.35
#: （initiative_profile 孤独锚点机制不动，这里只消费其输出，不改其判定）。
EXPERIENCE_SHARE_PROB_RATIO = 0.15
EXPERIENCE_SHARE_PROB_CAP = 0.35
#: 分享 topic 参考行（追加在想念 reason 后；可选话题，不要求提起）
EXPERIENCE_SHARE_TOPIC_PREFIX = "\n最近经历参考（可选话题）："
EXPERIENCE_SHARE_TOPIC_SUFFIX = "。可以自然地带一句，也可以选择不提。"

#: 情绪不可得时的恒等 mood_effect（emotion 关闭/缺失 → 乘数恒 1.0，§八.2 可空降级）
NEUTRAL_MOOD_EFFECT: dict[str, Any] = {
    "proactive_rate": 1.0,
    "miss_threshold": 1.0,
    "post_rate": 1.0,
    "intent_hint": "",
}

#: 需要校验格式（HH:MM）的时间窗口配置项（非法时 fail-safe：静默视为在静默）。
#: 早/晚安窗口字段自 B1 合并起已废弃、2026-10-10 配置整理轮**删除**；
#: 静默时段两键（silence_start/silence_end）也已随 2026-10-20 用户指示删除——
#: 静默窗恒复用 [schedule] 作息，不再存在 patrol 段的时间窗配置键（本常量清空保留占位）。
TIME_WINDOW_KEYS: Tuple[str, ...] = ()

#: [schedule] 段或锚点缺失时的出厂兜底（与 planning.config.ScheduleSectionConfig 默认一致）
_FALLBACK_SCHEDULE_WAKE = "07:00"
_FALLBACK_SCHEDULE_SLEEP = "23:00"


@dataclass(frozen=True)
class SilenceWindow:
    """静默窗的**生效值**（2026-10-20 用户指示：静默窗恒复用作息锚点；2026-10-21 §24.2
    任务B 真实化：插件实例路径优先真实睡眠环）。

    - ``start`` / ``end``：生效的 ``HH:MM`` 原文（未规范化：真实锚点时为实际条目的
      入睡/起床钟点——跨午夜按 end<start 写法，交给 ``is_in_time_window``；配置路径时
      原样回显配置值）；
    - ``start_source`` / ``end_source``：``"schedule"``（``[schedule]`` 配置锚点，桩/
      降级路径）或 ``"real_schedule"``（真实睡眠环条目；§24.2 两侧端点同源同条目）；
    - ``invalid_keys``：格式非法（非 HH:MM）的来源配置键，非空即**整窗非法**——调用方据此走
      fail-safe（巡检视作静默、晚安观测窗关闭），**不得**静默回退成"可打扰"。
      （真实锚点由分钟数构造，恒合法 ⇒ 恒空。）
    """

    start: str
    end: str
    start_source: str = "schedule"
    end_source: str = "schedule"
    invalid_keys: Tuple[str, ...] = ()

    @property
    def invalid(self) -> bool:
        """存在格式非法的来源配置（True = 调用方按 fail-safe 处理）。"""
        return bool(self.invalid_keys)

    @property
    def start_key(self) -> str:
        """生效起点的来源配置键（``invalid_time_config`` 留痕用；真实锚点恒无留痕）。"""
        return "schedule.sleep_time"

    @property
    def end_key(self) -> str:
        """生效终点的来源配置键（``invalid_time_config`` 留痕用）。"""
        return "schedule.wake_time"

    def source_note(self) -> str:
        """可读来源标注（日志用；恒有标注）：配置锚点= 跟随日程作息，真实锚点= 跟随真实日程。"""
        if self.start_source == SOURCE_REAL_SCHEDULE:
            return "（跟随真实日程）"
        return "（跟随日程作息）"

    def describe(self) -> str:
        """日志里的可读形态：``23:00~07:00（跟随日程作息）``。"""
        return f"{self.start}~{self.end}{self.source_note()}"


def _config_section(config: Any, attr: str) -> Any:
    """读配置段：兼容「plugin 实例（各段挂在 .config）」与「配置对象本身」两种形态（可空降级）。"""
    section = getattr(config, attr, None)
    if section is not None:
        return section
    return getattr(getattr(config, "config", None), attr, None)


def sleep_engine_asleep(plugin: Any, now: Optional[datetime] = None) -> bool:
    """睡眠引擎「仍在睡」权威读数（单一 provider 的薄封装；不表态/异常一律 False）。

    晚醒（引擎还在睡、日程端点尚未写回真实醒点）时，陪伴静默与空间可用性必须按**真实
    状态**判静默，不能按 07:00 旧锚点提前开口；引擎关/未装配 → 保持旧窗口语义。
    """
    getter = getattr(plugin, "sleep_asleep", None)
    if not callable(getter):
        return False
    try:
        return getter(now) is True
    except Exception:
        return False


def _fallback_schedule_anchors() -> Tuple[str, str]:
    """``[schedule]`` 锚点缺失时的兜底 ``(wake_time, sleep_time)``。

    真源 = ``planning.config.ScheduleSectionConfig`` 的出厂默认（不复制第二份字面量）；
    该模块不可导入时再退回模块常量。
    """
    try:
        from planning.config import ScheduleSectionConfig

        cfg = ScheduleSectionConfig()
        return str(cfg.wake_time), str(cfg.sleep_time)
    except Exception:  # pragma: no cover - 裁剪部署/桩环境
        return _FALLBACK_SCHEDULE_WAKE, _FALLBACK_SCHEDULE_SLEEP


def real_sleep_anchors_from_plugin(plugin: Any, now: Optional[datetime] = None
                                   ) -> Optional[RealAnchors]:
    """真实作息锚点的**同一 wiring 入口**（§24.2 任务B：所有消费点共享，禁止自建副本）。

    v4 真源顺序（§31.2 锚点合并 Q6，R7 = 用户 2026-10-23 第二轮直派）：

    - **周期四变量路径**（插件实例且 ``plugin._data_dir`` 可得）：经
      ``planning.cycle_state.try_load_persisted`` 读**已持久化**状态（缺失/非法 → 不拦，
      直接走下面的条目路径），再经 ``anchors_from_cycle_state`` 出锚点对：
      入睡 = current_cycle_end、起床 = current_cycle_start；anchored 状态以
      ``source=config_schedule`` 透传（消费方 source_note 自动显示"跟随日程作息"）；
    - **条目解析路径**（fail-safe 回退 = §31.2「现行配置路径」原样保留）：状态缺失/
      anchored/病态时按 §24.2 原口径读 ``plugin._schedule_query.get_day_schedule``
      （无缓存直读，不建内存镜像；今天与昨天两个日期帧都取）；
    - 任何异常 → 返回 ``None``，调用方走 ``[schedule]`` 配置路径（§7.9：绝不静默回复
      成"可打扰"）；
    - **纯配置对象**（无 ``_schedule_query`` 属性，测试桩）→ 仍走原判据：无状态文件
      可读 → 返回 ``None``；无副作用；
    - ``now`` 缺省取本模块的 ``datetime.now()``（决策日志用例的注入时钟同一处，
      ``mock.patch.object(companion.patrol, "datetime", ...)`` 即可生效）。
    """
    # § 31.2 锚点合并：周期状态文件（已持久化）优先 —— 真实进账轮换后即时生效；
    # anchored/缺失/非法 → 落回条目路径（该路径自带配置兜底，consume 面零改动）。
    # PR3 复核（2026-10-27）：条目端点**优先于**季度状态——晚醒真实写回只改入睡日条目的
    # 起床端点，周期四变量的 cycle_start 仍是最早清醒条目时间（07:00）。若继续让状态
    # 抢跑，patrol 静默/qzone 可用性/emotion 夜窗都会在 08:30 仍按 07:00 判断（不一致）。
    # 状态只保留为「条目读不到」时的兜底。
    pair = _item_anchor_pair(plugin, now)
    data_dir = getattr(plugin, "_data_dir", None)
    if data_dir is not None:
        try:
            from planning.schedule_anchors import anchors_from_cycle_state, try_load_persisted
            state = try_load_persisted(data_dir)
            if state is not None:
                advanced = _config_section(plugin, "schedule_advanced")
                tz_name = str(getattr(advanced, "timezone", "") or "").strip()
                from_state = anchors_from_cycle_state(state, tz_name=tz_name)
                if from_state is not None:
                    if pair is None:
                        return from_state
                    if (pair[0], pair[1]) != (from_state.start, from_state.end):
                        return RealAnchors(start=pair[0], end=pair[1],
                                           source=SOURCE_REAL_SCHEDULE)
                    return from_state
        except Exception:
            pass  # 状态层任何异常不阻断 → 条目路径 fail-safe（§7.9 契约）

    if pair is not None:
        return RealAnchors(start=pair[0], end=pair[1], source=SOURCE_REAL_SCHEDULE)
    if not callable(getattr(getattr(plugin, "_schedule_query", None),
                            "get_day_schedule", None)):
        return None
    # 条目读不到有效睡眠环（配置兜底/异常）→ 交给调用方走 [schedule] 路径
    return None


def _item_anchor_pair(plugin: Any, now: Optional[datetime]) -> Optional[Tuple[str, str]]:
    """真实睡眠环条目 → ``(入睡 HH:MM, 起床 HH:MM)``；不可得 → ``None``（纯读）。"""
    query = getattr(plugin, "_schedule_query", None)
    if not callable(getattr(query, "get_day_schedule", None)):
        return None
    try:
        moment = now if now is not None else datetime.now()
        today = moment.strftime("%Y-%m-%d")
        yesterday = (moment - timedelta(days=1)).strftime("%Y-%m-%d")
        items_today = list(query.get_day_schedule(today) or [])
        items_yesterday = list(query.get_day_schedule(yesterday) or [])
        schedule = _config_section(plugin, "schedule")
        fallback_wake, fallback_sleep = _fallback_schedule_anchors()
        cfg_wake = str(getattr(schedule, "wake_time", "") or "").strip() or fallback_wake
        cfg_sleep = str(getattr(schedule, "sleep_time", "") or "").strip() or fallback_sleep
        real = real_sleep_anchors(items_today, items_yesterday, now=moment,
                                  cfg_sleep=cfg_sleep, cfg_wake=cfg_wake)
        if real.source != SOURCE_REAL_SCHEDULE:
            return None
        return (real.start, real.end)
    except Exception:
        return None


#: 午休/小睡条目的名称词表（companion 巡检与空间静默共用；2026-10-20 用户指示
#: `[patrol].nap_quiet_enabled` 跨模块生效——qzone 判定链经 :func:`nap_item_matches` 同源判定）
NAP_NAME_KEYWORDS: Tuple[str, ...] = ("午休", "午睡", "小憩", "打盹")


def nap_item_matches(item: Any) -> bool:
    """日程条目是否命中午休/小睡（名称含关键词，或 ``goal_type == "rest"``）。

    companion 巡检的午休静默与 qzone 判定链的午休静默共用本判定（2026-10-20 跨模块口径）；
    A-21(b)（用户 2026-10-27 批）：**夜间睡眠条目排除**——``goal_type==sleep`` 或夜间名
    （``睡觉/睡眠/安睡``，与 :func:`planning.schedule_items.is_night_sleep_item` 同源）一律
    不算午休，避免午夜「打盹/睡觉」被白天午休静默；午休/午睡/小憩/打盹仍命中。
    非法入参一律 False（不把垃圾数据当成午休）。
    """
    if not isinstance(item, dict):
        return False
    if is_night_sleep_item(item):
        return False
    name = str(item.get("name") or "")
    if any(keyword in name for keyword in NAP_NAME_KEYWORDS):
        return True
    return str(item.get("goal_type") or "") == "rest"


def actual_nap_item_matches(item: Any) -> bool:
    """Actual nap identity; ordinary rest/daydreaming is not a nap."""
    if not isinstance(item, dict) or is_night_sleep_item(item):
        return False
    meta = item.get('metadata')
    if isinstance(meta, dict) and meta.get('kind') == 'nap':
        return True
    return any(word in str(item.get('name') or '') for word in NAP_NAME_KEYWORDS)


def nap_intent_matches_now(items: Any, now: datetime, *, dynamic: bool = False) -> bool:
    """午休静默的 spine 意图时间窗匹配（UQ-4，用户 2026-10-27 批）。

    RollingPlanner 开出 episode 前存在 ≤tick 周期的静默空窗：允许 nap_quiet 判定
    **只读计划表 spine 意图行**（``extract_spine_intents``，``parameters.spine=true``）、
    按同一个 24h 环时间窗（``is_in_day_frame`` 唯一口径）提前静默——**仅静默用途**，
    调用方不得把命中结果对外伪装成「当前活动」（裁定 6）。保守约束：``day_offset==1``
    的午夜段行不参与（与 life_schedule.next 同口径的保守跳过）；无意图/异常 → False。

    Args:
        items: 当日 ``schedule_items`` 行列表（调用方经 ``query.get_day_schedule`` 取）。
        now: 判定时刻。
    """
    try:
        from planning.rolling_planner import extract_spine_intents
        from planning.schedule_items import is_named_nap

        intents = extract_spine_intents(items or [])
    except Exception:
        return False
    if not intents:
        return False
    cur = now.hour * 60 + now.minute
    for intent in intents:
        if not isinstance(intent, dict) or str(intent.get("day_offset")) == "1":
            continue
        if dynamic and intent.get("hardness") != "hard" and is_named_nap(intent):
            continue
        if not nap_item_matches(intent):
            continue
        start = int(intent.get("start_minute") or 0)
        end = int(intent.get("end_minute") or 0)
        if is_in_day_frame(start, end, cur):
            return True
    return False


def resolve_silence_window(config: Any) -> SilenceWindow:
    """解析静默窗的**生效值**（单一真源：2026-10-20 起恒复用作息；2026-10-21 §24.2 任务B
    真实化——插件实例路径优先**真实睡眠环**锚点）。

    - **真实锚点路径**（``config`` 为插件实例且有 ``_schedule_query``）：经
      :func:`real_sleep_anchors_from_plugin` 读真实睡眠环（今晚/今晨睡觉条目，跨午夜
      end<start 写法交 ``is_in_time_window``；赖床顺延后锚点随移），``start_source`` /
      ``end_source`` 恒 = ``"real_schedule"``；
    - **配置路径**（纯配置对象，或日程不可用/解析异常兜底；现行行为）：
      起点取 ``[schedule].sleep_time``（入睡时间）、终点取 ``wake_time``（起床时间），
      即「入睡~起床」整夜静默——**夜晚睡觉时间即静默时间**；原 ``[patrol].silence_start/end``
      显式覆盖能力已随键删除而移除（旧配置显式值由迁移器丢弃并登记）；
      ``[schedule]`` 段/锚点缺失（未装配日程段）→ 用出厂锚点兜底（默认 07:00/23:00，
      仍是静默窗，不会变成可打扰）；
    - **fail-safe**：配置路径的生效值仍按 HH:MM 校验（``parse_hhmm``，与巡检静默闸同一
      口径），任一非法即整窗非法（``invalid_keys`` 留痕）——调用方按「宁可不发」处理，
      绝不静默回退成"可打扰"。真实锚点由分钟数构造恒合法（非法条目在解析层按候选
      不可用丢弃，不会带出非法 HH:MM），恒无 ``invalid_keys``。

    ``config`` 兼容 plugin 实例与配置对象。planning 侧（晚安期限 / 早安基准 / 情绪夜窗）、
    巡检静默闸与 qzone 空间静默闸共用本函数，保证各处取到的是同一个生效值；返回值是
    **未规范化**的原文，跨午夜判定交给 ``is_in_time_window``（start > end 即跨午夜）。
    """
    schedule = _config_section(config, "schedule")
    wake = str(getattr(schedule, "wake_time", "") or "").strip()
    sleep = str(getattr(schedule, "sleep_time", "") or "").strip()
    if not wake or not sleep:
        fallback_wake, fallback_sleep = _fallback_schedule_anchors()
        wake = wake or fallback_wake
        sleep = sleep or fallback_sleep

    invalid: list[str] = []
    if parse_hhmm(sleep, -1) < 0:
        invalid.append("schedule.sleep_time")
    if parse_hhmm(wake, -1) < 0:
        invalid.append("schedule.wake_time")

    # §24.2 任务B（R7 依据 = 用户 2026-10-21 直派）：插件实例路径先真实锚点，读取即解析，
    # 不建内存镜像；任何异常/不可得全走上面的配置路径（fail-safe 语义不变）。
    real = real_sleep_anchors_from_plugin(config)
    if real is not None and real.source == SOURCE_REAL_SCHEDULE:
        return SilenceWindow(
            start=real.start,
            end=real.end,
            start_source=SOURCE_REAL_SCHEDULE,
            end_source=SOURCE_REAL_SCHEDULE,
            invalid_keys=(),
        )
    return SilenceWindow(
        start=sleep,
        end=wake,
        start_source="schedule",
        end_source="schedule",
        invalid_keys=tuple(invalid),
    )

#: 跳过原因优先级：一轮巡检里若多个候选触发都被挡，只保留优先级最高的那个原因。
#: 数值越大越"值得报告"；同优先级时保留先出现的（即更高等级的触发）。
#: 注：``already_sent_today``（早/晚安已发观察）已按 2026-10-11 裁决（A-17）移出本表——
#: 它是纯观察、不构成归因，现记在决策行的独立字段 ``greeting_observation``（见 _tick）。
SKIP_PRIORITY: dict[str, int] = {
    "invalid_time_config": 100,
    "disabled": 96,
    "daily_max_zero": 96,
    "silence": 95,
    #: 午休静默（裁决⑦ 2026-10-11）：午休条目进行中整轮跳过主动开口（默认关）
    "nap_quiet": 93,
    "planner_trigger_failed": 85,
    "min_interval": 80,
    "cooldown": 70,
    "budget": 60,
    "llm_rejected": 50,
    "dice": 40,
    #: 2026-11-04 重写：activity_disabled / miss_duration / miss_already_sent 随
    #: 日程巡检与沉默阈值闸门删除（想念改状态驱动概率，无每日一次闸门）。
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
        self._initiative_memory = InitiativeMemory(getattr(config, "_data_dir", None))
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
        self._last_tick_at: Optional[datetime] = None
        # M3 沉默事件频控（内存态，进程重启即清零；§4.2：每日 ≤2 次、间隔 ≥4h、仅 target）
        # 非法时间窗口配置的告警节流：配置项 -> 已告警过的原始值
        self._invalid_time_warned: dict[str, str] = {}
        # F5（2026-11-04）：整轮异常节流留痕的上次告警时刻（None = 从未告警）
        self._tick_error_warned_at: Optional[datetime] = None

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

    async def initialize_disposition(self, *, force: bool = False) -> dict:
        return await self._initiative_memory.initialize(self._personality, self._llm, force=force)

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
                # 单轮失败不影响循环；F5（2026-11-04）：不再整轮静默——节流留痕
                self._warn_tick_error()
            interval = max(1, int(getattr(self._patrol_cfg(), "check_interval_minutes", 10))) * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    def _warn_tick_error(self) -> None:
        """整轮巡检异常的节流留痕（F5）：warning(exc_info) + 一条决策行，至多 10 分钟一次。

        修复「_tick 在 finish() 前抛异常 → 该轮完全无痕」与「决策日志每轮恰好一条」
        承诺的矛盾；持续故障（如配置字段类型异常）不会刷屏，但排障不再盲区。
        """
        now = datetime.now()
        last = self._tick_error_warned_at
        if last is not None and (now - last).total_seconds() < 600:
            return
        self._tick_error_warned_at = now
        logger.warning("巡检单轮异常（节流留痕，至多 10 分钟一次）", exc_info=True)
        self._log_decision(action=ACTION_SKIP, reason="tick_error", trigger_type="patrol",
                           detail="巡检单轮异常，本轮未完成判定（详情见日志，节流留痕）")

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
        cfg = self._patrol_cfg()
        now = datetime.now()
        self._last_tick_at = now
        fields: dict[str, Any] = {}
        def done(reason: str, action: str = ACTION_SKIP, detail: str = "") -> None:
            self._log_decision(action=action, reason=reason, trigger_type="miss", detail=detail, **fields)
        if not bool(getattr(cfg, 'enabled', True)) or not cfg.proactive_trigger_enabled or not cfg.miss_enabled:
            done('disabled')
            return
        if self._affection.today_date() != now.strftime('%Y-%m-%d'):
            self._affection.reset_daily(now.strftime('%Y-%m-%d'))
            self._affection.apply_daily_decay()
        # Persona initialization is once per fingerprint; no Y/N request per candidate.
        profile = await self.initialize_disposition()
        last = self._affection.last_user_msg_time()
        cycle = self._initiative_memory.cycle(self._target_qq, last, now)
        if cycle is None or last is None:
            done('no_user_message')
            return
        hours = hours_between(now, last)
        energy, social, valence, _tier, _count = self._miss_state_inputs()
        decision = evaluate_miss(hours, float(profile.get('anchor_hours', 6)),
                                 float(profile.get('loneliness', .5)), energy, social, valence)
        fields.update(silence_hours=round(hours, 3), anchor_hours=profile.get('anchor_hours'),
                      loneliness=profile.get('loneliness'), motivation=round(decision.motivation,4),
                      capacity=round(decision.capacity,4), miss_probability=round(decision.probability,4),
                      cycle_status=dict(cycle.get('claims') or {}), profile_source=profile.get('source'))
        # Same anchor for the silence fact and missing candidate; each has its own receipt.
        await self._maybe_publish_silence(now)
        if 'miss' in cycle.get('claims', {}):
            done('silence_cycle_consumed')
            return
        if not decision.eligible:
            done(decision.reason)
            return
        coordinator = getattr(self._config, '_optional_actions', None)
        allowed, denial = coordinator.permission() if coordinator is not None else (False, 'not_assembled')
        if not allowed:
            done(denial)
            return
        # PR-C1 H5：候选阶段读经历分享动机——只作 topic 参考与轻微概率加成；
        # 最终门控（上方 permission：预算/静默/注意力 + dispatch 内复检）不变，
        # 孤独锚点判定（evaluate_miss）不动，动机存在 ≠ 必须联系（dice 仍可放弃）。
        share = self._experience_share_motive()
        probability = decision.probability
        share_topic = ""
        if share is not None:
            share_topic = str(share.get('topic') or '')
            try:
                strength = max(0.0, min(1.0, float(share.get('strength') or 0.0)))
            except (TypeError, ValueError):
                strength = 0.0
            probability = min(EXPERIENCE_SHARE_PROB_CAP,
                              decision.probability * (1.0 + EXPERIENCE_SHARE_PROB_RATIO * strength))
            fields.update(experience_share_ref=str((share.get('source') or {}).get('ref') or ''),
                          experience_share_strength=round(strength, 4),
                          miss_probability_boosted=round(probability, 4))
        if random.random() >= probability:
            done('dice')
            return
        if not self._initiative_memory.claim(self._target_qq, last, now, 'miss'):
            done('recovery_or_cycle_claim')
            return
        key = 'miss:' + str(cycle['anchor'])
        success = await self._trigger_missing(hours, key=key, share_topic=share_topic)
        self._initiative_memory.receipt(self._target_qq, 'miss', 'enqueued' if success else 'not_enqueued_or_unknown')
        done('miss_trigger' if success else 'planner_not_enqueued', ACTION_TRIGGER if success else ACTION_SKIP)


    # ------------------------------------------------------------------
    # 各类触发（屏幕感知统一在 _trigger_planner 里先取）
    # ------------------------------------------------------------------
    async def _trigger_missing(self, hours_since_last: float, *, key: str = "",
                               share_topic: str = "") -> bool:
        reason = self._format_miss_reason(hours_since_last)
        # PR-C1 H5：经历分享 topic 只作可选话题参考（不要求提起；空 = 逐字旧行为）
        if str(share_topic or '').strip():
            reason = (f"{reason}{EXPERIENCE_SHARE_TOPIC_PREFIX}{str(share_topic).strip()}"
                      f"{EXPERIENCE_SHARE_TOPIC_SUFFIX}")
        result = await self._trigger_planner('missing', reason, key=key)
        if result:
            self._affection.set_miss_sent()
        return result

    def _experience_share_motive(self) -> Optional[dict[str, Any]]:
        """PR-C1 H5：读 B2 动机表中当前最强的 experience_share 动机（只读、可空降级）。

        真源 = ``plugin._motive_store``（MotiveStore；开关/衰减/到期在其只读口内
        判定）。未装配/停用/无分享动机/任何异常 → None（候选阶段逐字旧行为）。
        本方法不生成消息、不改任何门控——动机存在 ≠ 发消息。
        """
        store = getattr(self._config, '_motive_store', None)
        if store is None:
            store = getattr(getattr(self._config, 'config', None), '_motive_store', None)
        active = getattr(store, 'active', None)
        if not callable(active):
            return None
        try:
            motives = active() or []
        except Exception:
            return None
        best: Optional[dict[str, Any]] = None
        try:
            for motive in motives:
                if not isinstance(motive, dict):
                    continue
                if str((motive.get('source') or {}).get('kind') or '') != 'experience_share':
                    continue
                if best is None or float(motive.get('strength') or 0.0) > float(best.get('strength') or 0.0):
                    best = motive
        except (TypeError, ValueError):
            return None
        return best


    # 注（2026-11-04 主动行为重写）：_trigger_activity / _trigger_daily（日程巡检的
    # 活动分享/日常搭话触发）已整条删除——patrol 侧不再有任何日程意图主动发言。

    async def _trigger_planner(self, intent: str, reason: str, *, key: str = "") -> bool:
        action = getattr(self._config, 'optional_action', None)
        if not callable(action) or not self._stream_id:
            return False
        result = await action(key, intent, self._append_intent_hint(reason), before=self._publish_screen_narration)
        if result.get('status') != 'enqueued':
            self._discard_screen_narration()
            return False
        self._affection.increment_speak(.5)
        self._last_trigger_time = datetime.now()
        self._last_trigger_intent = str(intent)
        # PR-PROACTIVE-GATE（3.17.14）：成功入队后经统一入口登记（miss/patrol_chat）。
        # 登记不计数——increment_speak 口径（触发 0.5 + 回复确认 0.5）逐字不变。
        register_turn(self._config, self._stream_id,
                      'miss' if intent == 'missing' else 'patrol_chat', reason)
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
        if not self.has_target():
            return
        last = self._affection.last_user_msg_time()
        if last is None:
            return
        hours = hours_between(now, last)
        if hours < float(self._initiative_memory.profile().get('anchor_hours', 6)):
            return
        bus = self._event_bus()
        if bus is None or not self._initiative_memory.claim(self._target_qq, last, now, 'silence'):
            return
        try:
            await bus.publish('relation.silence_detected', source='companion',
                              hours=round(hours, 2), target_bonus=round(self._silence_target_bonus(), 4))
            self._initiative_memory.receipt(self._target_qq, 'silence', 'published')
        except Exception:
            self._initiative_memory.receipt(self._target_qq, 'silence', 'unknown')
            logger.warning('沉默事实发布失败，本周期不重复发布', exc_info=True)



    def _silence_target_bonus(self) -> float:
        """当前档位的 target 加算加成 = step × (档位相对位置 × 4 + 1)（§5.3 / §6.2 A；读不到按 0）。

        step 优先取情绪服务（``plugin._emotion.target_bonus_step()``；M12/D7 起该路径已含
        「JSON 覆盖 > config > 默认」三层合并，与桥内入账的 K 同源），服务不可得（模块停用/
        热重载窗口/异常）时回退 ``[emotion].target_bonus_step`` 直读。档位读 affection 状态层；
        任一不可得时返回 0（不猜档位、不猜配置）。

        2026-10-17 §19.4 / 复核 findings#1：档位数可变（1~5 档预设）后改为与
        ``emotion/service.py`` 的 ``target_bonus()`` **同款 ratio×4+1 连续化**——档位数取
        ``affection.preset().size``（N≤1 时 ratio=0），``tier()`` 已在状态层按生效预设钳制、
        此处只防负数。5 档时与旧 ``step × (tier+1)`` 逐档恒等（ratio=tier/4），
        留痕 payload 与桥内实际入账的 K 不再各写一套口径。
        """
        tier_getter = getattr(self._affection, "tier", None)
        if not callable(tier_getter):
            return 0.0
        try:
            tier = max(0, int(tier_getter()))
        except Exception:
            return 0.0
        preset_getter = getattr(self._affection, "preset", None)
        try:
            preset = preset_getter() if callable(preset_getter) else None
            count = int(getattr(preset, "size", 5)) if preset is not None else 5
        except Exception:
            count = 5
        count = max(1, count)
        ratio = 0.0 if count <= 1 else min(1.0, tier / (count - 1))
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
        return max(0.0, min(1.0, step)) * (ratio * 4.0 + 1.0)

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

    def _miss_state_inputs(self) -> tuple[Any, Any, Any, Any, Any]:
        """想念概率公式的五路输入：``(energy, social_battery, valence, tier, tier_count)``。

        全部只读、可空降级（任一模块缺失/停用/异常 → 对应输入 None → 公式因子恒 1.0，
        §八.2 口径）：生活状态读 ``plugin._life_state.snapshot()``（runtime 权威，不读
        配置不读表）；情绪读 ``EmotionService.effective_valence()``（先判 is_enabled）；
        好感读 affection 状态层 tier + 生效预设档位数（先判 is_enabled）。
        """
        energy = social = valence = tier = tier_count = None
        life = getattr(self._config, "_life_state", None)
        if life is None:
            life = getattr(getattr(self._config, "config", None), "_life_state", None)
        snapshot_getter = getattr(life, "snapshot", None)
        if callable(snapshot_getter):
            try:
                snap = snapshot_getter()
                if isinstance(snap, dict):
                    energy = snap.get("energy")
                    social = snap.get("social_battery")
            except Exception:
                pass
        emotion = self._emotion_service()
        emotion_ok = True
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                emotion_ok = bool(enabled())
            except Exception:
                emotion_ok = False
        if emotion_ok:
            getter = getattr(emotion, "effective_valence", None)
            if callable(getter):
                try:
                    valence = getter()
                except Exception:
                    valence = None
        affection_ok = True
        aff_enabled = getattr(self._affection, "is_enabled", None)
        if callable(aff_enabled):
            try:
                affection_ok = bool(aff_enabled())
            except Exception:
                affection_ok = False
        if affection_ok:
            try:
                tier = int(self._affection.tier())
                tier_count = int(getattr(self._affection.preset(), "size", 5))
            except Exception:
                tier = tier_count = None
        return energy, social, valence, tier, tier_count

    def _append_intent_hint(self, reason: str) -> str:
        """M4（§6.2 C）：proactive intent 的 reason 追加情绪 ``intent_hint``。

        中性/情绪不可得（intent_hint 为空串）时原样返回，既有 reason 字节不变。
        """
        hint = str(self._mood_effect().get("intent_hint") or "").strip()
        if not hint:
            return reason
        base = str(reason or "").strip()
        return f"{base}。{hint}" if base else hint

    # 注（2026-11-04 主动行为重写）：旧沉默阈值机制（_affection_tier_miss_factor /
    # _miss_threshold_factors / _sample_miss_threshold / _miss_range_text）与日程巡检
    # 开关（_patrol_activity_enabled）已删除——想念改状态驱动概率（miss_trigger 公式）。
    # miss_silence_hours 区间保留，仅服务沉默事件（relation.silence_detected）门槛。




    def _relation_word(self) -> str:
        """当前关系称谓（relation profile 未注入时回退「恋人」）。"""
        return getattr(self._relation, "word", None) or "恋人"

    def _format_miss_reason(self, hours_since_last: float) -> str:
        """想念触发的 reason（2026-11-04 重写：文案在 companion.miss_trigger，模块内常量）。

        自然想念表达、不引导汇报日程；可携带一句关系记忆（用户习惯）作可选语境。
        首行保留「【触发原因·想念】」标注（注入上下文要标明为什么触发）。
        """
        try:
            memory = miss_trigger.memory_line(self._affection.habits())
        except Exception:
            memory = ""
        return render(
            miss_trigger.MISS_REASON_DEFAULT,
            fallback=miss_trigger.MISS_REASON_DEFAULT,
            lover_name=self._lover_name,
            hours=f"{hours_since_last:.1f}",
            relation=self._relation_word(),
            memory_line=memory,
        )




    # ------------------------------------------------------------------
    # 通用小工具
    # ------------------------------------------------------------------
    # 注（2026-11-04 主动行为重写）：_find_current_activity / _schedule_enabled /
    # _format_activity_line 已删除——patrol 不再读当前活动（日程巡检触发路径与想念
    # 把关的日程语境一并移除）。self._schedule 仅余午休静默（_nap_in_progress）消费。

    def _minutes_since_last_speak(self, now: datetime) -> Optional[float]:
        last_speak = self._affection.last_speak_time()
        if last_speak is None:
            return None
        return max(0.0, (now - last_speak).total_seconds() / 60)


    def _effective_silence_window(self) -> SilenceWindow:
        """本轮**生效**的静默窗（N5：留空跟随 [schedule] 作息锚点，非空 = 显式覆盖）。

        单一真源 = :func:`resolve_silence_window`；格式非法的值原样带出（由调用方 fail-safe）。
        """
        return resolve_silence_window(self._config)



    def _collect_invalid_time_keys(self, window: Optional[SilenceWindow] = None) -> list[str]:
        """收集**生效**静默窗里格式非法的（HH:MM）来源配置项，并对每个非法值告警一次。

        N5 起校验的是生效值：留空跟随锚点时，``schedule.sleep_time`` / ``schedule.wake_time``
        非法同样要留痕——否则静默窗会悄悄退化成"可打扰"。
        """
        window = window if window is not None else self._effective_silence_window()
        invalid: list[str] = []
        for key, raw in ((window.start_key, window.start), (window.end_key, window.end)):
            if parse_hhmm(raw, -1) >= 0:
                continue
            invalid.append(key)
            if self._invalid_time_warned.get(key) != raw:
                self._invalid_time_warned[key] = raw
                try:
                    source = "（跟随日程作息）" if key.startswith("schedule.") else ""
                    self._ctx.logger.warning(
                        f"静默窗配置 {key}={raw!r} 不是 HH:MM 格式{source}；已按 fail-safe 处理"
                        f"（静默时段视为静默，宁可不发）。请检查插件配置。"
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
