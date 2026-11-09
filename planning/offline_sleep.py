"""离线睡眠补全策略层（纯模块；2026-10-29 分级睡眠补全，外置报告 §2.5 默认值）。

R7 依据 = 用户 2026-10-29 直派【A1：离线睡眠补全·策略层】；分级与默认值按
``D:/deepseek-harness_workspace/maibot-plugins/20261029_0343_生活面板美术与后续改动规划报告.md``
§2.5「掉线重连后的分级睡眠补全」执行（5 个待确认项按推荐默认值，用户未否决即采用）。

职责边界：

- 本模块**只做分类与结算计划**：输入原始离线 gap 秒数 + 结算前睡眠债，输出结算
  计划 dict（分级 / 日程名 / 有效时长 / 状态增量 / 睡眠债终值 / 元数据）。
  不读盘、不写盘、不触 ``LifeStateService`` / ``SleepEngine`` 实例、不发事件、
  0 LLM——pending gap 的取数（``LifeStateService.consume_pending_offline_gap``）、
  会话/episode 落库与事件发布由编排方（plugin 装配链）执行。
- 状态结算复用 :func:`planning.sleep_engine.integrate_sleep_gains`（0 起点分段
  精确积分；``quality=1.0``、``interruption_count=0``、连续从未被打断），**不得**
  走 :meth:`planning.life_state.LifeStateService.handle_sleep` 旧三档覆盖（防双记账）。
- 过睡口径：离线独立路径按 ``oversleep_start_hours=10.0`` 积分（报告 §2.5 默认值
  #1：上限 10h），有效时长 ≤10h 时精力增量恒为正——离线长睡**不触发**默认 9h
  曲线的过睡惩罚；超出 10h 的部分不补任何状态，只记「离线未记录」时长元数据。
- 分类用**原始 gap**（秒）：调用方必须传 ``LifeStateService`` 冷启动解析出的原始
  值，不得先经 ``LifeState._dt_minutes`` 的 24h 钳制（钳制只属于正常运行 tick）。
- 依赖纪律：只 import ``planning.sleep_engine`` 既有纯函数（本模块不涉日帧钟点，
  故不需要 ``planning.time_utils``）；时区/落盘全部留在编排方侧。
"""

from __future__ import annotations

from typing import Any, Dict

from .sleep_engine import SLEEP_DEBT_MAX, integrate_sleep_gains, settle_sleep_debt

# ── 分级边界（秒；报告 §2.5 分级表默认值）────────────────────────────────
#: grace 上界（不含）：离线 <5 分钟 = 宿主重启级抖动，不记录
GRACE_MAX_SECONDS = 300.0
#: nap 上界（不含）：5–60 分钟「稍微打了个瞌睡」
NAP_MAX_SECONDS = 3600.0
#: medium 上界（不含）：1–4 小时「困了睡觉去了」
MEDIUM_MAX_SECONDS = 14400.0
#: long 上界（不含）：4–10 小时「睡了很久」；≥10 小时 = overlong「睡了很久（离线）」
LONG_MAX_SECONDS = 36000.0

#: 分类名集合（``classify_offline_gap`` 返回值只取其一）
GAP_CATEGORIES: Dict[str, str] = {
    "grace": "",                    # 不记录（无日程名）
    "nap": "稍微打了个瞌睡",
    "medium": "困了睡觉去了",
    "long": "睡了很久",
    "overlong": "睡了很久（离线）",
}

#: 离线有效睡眠时长上限（小时；报告 §2.5 默认值 #1：overlong 只算前 10h）
EFFECTIVE_SLEEP_CAP_HOURS = 10.0
#: 离线独立路径的过睡段起点（小时）：避开引擎默认 9h 过睡惩罚（报告 §2.5 时序注）
OFFLINE_OVERSLEEP_START_HOURS = 10.0
#: 整晚规则睡眠债结算的最短有效时长（小时；报告 §2.5「long 在 ≥8h 时结算」）
DEBT_SETTLE_MIN_HOURS = 8.0
#: overlong 超出有效上限部分的元数据标记（报告 §2.5「其余记“离线未记录”」）
UNRECORDED_NOTE = "离线未记录"


def classify_offline_gap(seconds: Any) -> str:
    """原始离线 gap 秒数 → 分级名（``grace/nap/medium/long/overlong``）。

    边界口径（左闭右开）：``[0, 300)`` grace / ``[300, 3600)`` nap /
    ``[3600, 14400)`` medium / ``[14400, 36000)`` long / ``[36000, ∞)`` overlong。

    非法输入（``None`` / 非数值 / NaN / 负数）一律按 ``grace``（不记录，可空降级）；
    时钟回拨（``last_tick_at`` 在未来）已在 ``LifeStateService`` 侧拦为 ``None``，
    不会进入本函数。
    """
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "grace"
    if value != value or value < GRACE_MAX_SECONDS:  # NaN / 负数防御
        return "grace"
    if value < NAP_MAX_SECONDS:
        return "nap"
    if value < MEDIUM_MAX_SECONDS:
        return "medium"
    if value < LONG_MAX_SECONDS:
        return "long"
    return "overlong"


def plan_offline_settlement(gap_seconds: Any, sleep_debt_before: Any = 0.0,
                            *, baseline_hours: Any = 8.0) -> Dict[str, Any]:
    """按分级生成一次离线睡眠结算计划（纯函数；不触状态/不落盘/不发事件）。

    Args:
        gap_seconds: 原始离线 gap 秒数（未钳制；非法/负数按 grace 处理）。
        sleep_debt_before: 结算前睡眠债（小时，0~12；非法按 0）。
        baseline_hours: 复核 P0-4：零债基准（与在线 settle 同口径，默认 8.0）——
            短窗口用户离线睡满配置时长（≥基准）即按整晚规则结算，不再被 8h
            硬门槛完全拒结。

    Returns:
        计划 dict（编排方据此落库/应用，键集固定）：

        - ``category`` / ``label``：分级名与日程名（grace 的 ``label`` 为空 = 不记录）；
        - ``gap_seconds``：原始 gap（透传，诊断用）；
        - ``effective_seconds`` / ``effective_minutes`` / ``effective_hours``：
          有效睡眠时长 = ``min(gap, 10h)``（grace = 0）；
        - ``unrecorded_seconds``：overlong 超出 10h 的未记录时长（其余分级为 0）；
        - ``settle``：是否产生结算（grace = ``False``，编排方直接跳过并收口 pending）；
        - ``gains``：:func:`~planning.sleep_engine.integrate_sleep_gains` 的分段精确
          积分增量（``energy/focus/hunger/stress/hours``；0 起点至有效时长）；
        - ``quality`` / ``interruption_count`` / ``continuous_minutes``：
          恒 ``1.0`` / ``0`` / 有效分钟（离线整段从未被打断）；
        - ``oversleep_start_hours``：积分所用过睡起点（离线路径恒 10h）；
        - ``sleep_debt_before`` / ``sleep_debt_after`` / ``debt_settled``：
          睡眠债口径——nap/medium 不变；long 在有效时长 ≥8h 按整晚规则
          （:func:`~planning.sleep_engine.settle_sleep_debt`）结算；overlong 同
          long 按有效 10h 口径结算；其余不变；
        - ``metadata``：建议随会话/episode 落库的元数据（``unrecorded_seconds``、
          ``offline_reconnect`` 标记与「离线未记录」注记）。

    睡眠债终值恒钳 ``[0, 12]``（与 :data:`~planning.sleep_engine.SLEEP_DEBT_MAX`
    同口径）；无任何键会触发 ``handle_sleep`` 三档覆盖或 ``life.sleep_started``
    补发——事件与写库全由编排方决定。
    """
    category = classify_offline_gap(gap_seconds)
    try:
        gap = float(gap_seconds)
    except (TypeError, ValueError):
        gap = 0.0
    if gap != gap or gap < 0.0:  # NaN/负数防御：按 0 计，分类已归 grace
        gap = 0.0
    if category == "grace":
        # grace = 抖动级离线，**不记录**：有效时长归 0、gains 全零，
        # 编排方按 settle=False 跳过结算并收口 pending（不得残留半段积分）
        gap = 0.0
    debt_before = sleep_debt_before if isinstance(sleep_debt_before, (int, float)) \
        and not isinstance(sleep_debt_before, bool) and sleep_debt_before == sleep_debt_before \
        else 0.0
    debt_before = max(0.0, min(SLEEP_DEBT_MAX, float(debt_before)))

    effective_seconds = min(gap, EFFECTIVE_SLEEP_CAP_HOURS * 3600.0)
    effective_minutes = effective_seconds / 60.0
    effective_hours = effective_seconds / 3600.0
    unrecorded_seconds = max(0.0, gap - effective_seconds)

    # 复核 P0-4：基准与在线 settle 同源（钳 [4,12]，非法回退 8.0）；long 的
    # 结债门槛取 min(8h, 基准)，短窗口睡满配置时长即可结算（按比例公式）。
    try:
        baseline = max(4.0, min(12.0, float(baseline_hours)))
    except (TypeError, ValueError):
        baseline = 8.0
    if baseline != baseline:  # NaN 防御
        baseline = 8.0
    debt_settled = False
    sleep_debt_after = debt_before
    if category == "long" and effective_hours >= min(DEBT_SETTLE_MIN_HOURS, baseline):
        sleep_debt_after = settle_sleep_debt(debt_before, effective_hours,
                                             baseline_hours=baseline)
        debt_settled = True
    elif category == "overlong":
        # 有效时长恒 = 10h：同 long 按整晚规则、按 10h 口径结算
        sleep_debt_after = settle_sleep_debt(debt_before, effective_hours,
                                             baseline_hours=baseline)
        debt_settled = True

    gains = integrate_sleep_gains(
        0.0, effective_hours,
        oversleep_start_hours=OFFLINE_OVERSLEEP_START_HOURS,
        quality=1.0)
    return {
        "category": category,
        "label": GAP_CATEGORIES[category],
        "gap_seconds": gap,
        "effective_seconds": effective_seconds,
        "effective_minutes": effective_minutes,
        "effective_hours": effective_hours,
        "unrecorded_seconds": unrecorded_seconds,
        "settle": category != "grace",
        "gains": gains,
        "quality": 1.0,
        "interruption_count": 0,
        "continuous_minutes": effective_minutes,
        "oversleep_start_hours": OFFLINE_OVERSLEEP_START_HOURS,
        "sleep_debt_before": debt_before,
        "sleep_debt_after": sleep_debt_after,
        "debt_settled": debt_settled,
        "metadata": {
            "offline_reconnect": True,
            "gap_seconds": round(gap, 4),
            "unrecorded_seconds": round(unrecorded_seconds, 4),
            "unrecorded_note": UNRECORDED_NOTE if unrecorded_seconds > 0 else "",
        },
    }
