"""结算模型 v2：目标趋近 + 各轴独立生理/行为时钟（2026-11-03 优化方案 §3/§5）。

依据 = ``union/20261103_生活状态极端值成因与数值结算优化方案.md`` §3（推荐模型：
``x_next = target + (x_now - target) * exp(-dt / tau)``，越接近目标变化越慢）与
§5.2（离线实现 v2 并回放：优先修社交回充、待机/休息边际收益、进食饱腹三处）。
R7 依据 = 用户 2026-11-03 直派【v2 结算模型实现】硬要求 2~6。

职责边界：

- 本模块**纯函数层**：输入状态快照 + dt + 画像速率 + 参数，输出各轴增量与来源
  分解（基础/活动/睡眠），不读盘、不写盘、不触 ``LifeStateService`` / 时钟 / 事件，
  0 LLM。状态施加与 clamp/持久化/诊断落盘全部在 :mod:`planning.life_state` 侧。
- 本模块是唯一生活状态结算模型，不存在旧模型回退或情绪速率调制路径。
- **精确积分**：趋近式是 ``dx/dt=(target-x)/tau`` 的闭式解，``exp(-(a+b))`` 与
  分段乘积在浮点误差内一致 → 1/5/15 分钟分片终值天然一致（方案 §5.3）；v2
  路径**不使用**每 tick 限幅（``TICK_DELTA_CAP`` 仅属 v1 调制路径）。
- **替代而非叠加**：被 target/tau 描述的轴（待机/休息/用餐等的恢复轴）不再叠加
  旧线性速率与清醒被动漂移；消耗型轴（work/study 等的精力/专注/压力速率）与
  社交消耗（画像 ``social_rate < 0``）仍按每分钟速率线性积分——它们本身就是
  v1 画像速率语义，不与趋近式重复计算同一条通路。

单位口径（与 v1 一致，审计 2026-11-03）：五项状态内部 0~1（展示 ×100）；
``sleep_debt`` 0~12h；``dt`` 一律分钟（真实经过时间）；速率=每分钟（0~1 尺度）。

参数纪律（硬要求 2/7）：全部可调参数集中在 :data:`V2_PARAMS`，版本常量
:data:`V2_PARAMS_VERSION`；候选值来自方案 §3 供离线调参的初始候选（休息精力
目标 70~85、睡眠目标 85~95、进食后饥饿 10~20、社交安静回充以小时计），**不是
已验证的最终参数**。用户可经 ``[life_state].settlement_params_json`` 以 JSON
覆盖（未知键丢弃并告警，非法 JSON 回退内置默认）。
"""

from __future__ import annotations

import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

#: v2 参数版本（结构/默认值变更时递增；随 ``settlement_diag.version`` 落盘可追溯）
#: 20261110.1：P1-6 复核把 idle 的 focus 靶心 0.35→0.60（见 ``activity.idle``）；
#: 默认参数变更必须同步递增版本，否则 ``settlement_diag.version`` 追溯失真（CF-7）。
V2_PARAMS_VERSION = "20261110.1"

#: v2 结算内置参数（唯一真源；键集固定，未知覆盖键在 :func:`merge_params` 丢弃）
V2_PARAMS: Dict[str, Any] = {
    # ── 睡眠期（v2 睡眠 = 目标趋近，替代 v1 八段曲线速率；方案 §2.3/§3「睡眠目标
    #    85~95、按睡眠债修调可恢复水平」）────────────────────────────────────
    "sleep_energy_target": 0.92,          # 睡眠精力目标（方案候选 85~95 取 92）
    "sleep_energy_target_floor": 0.80,    # 债务修调下限：保证仍可越过觉醒阈值 0.75
    "sleep_energy_target_drain_per_debt_h": 0.02,   # 每小时睡眠债压低目标 2 点
    "sleep_energy_tau_min": 90.0,         # 睡眠精力趋近时间常数（分钟）
    "sleep_focus_target": 0.80,
    "sleep_focus_tau_min": 120.0,
    "sleep_stress_target": 0.0,           # 趋近 0 但渐近、不夹死（允许短时接近 0）
    "sleep_stress_tau_min": 240.0,
    "sleep_hunger_target": 1.0,           # 睡眠饥饿缓慢上行（v1 为 +0.0001/min 线性）
    "sleep_hunger_tau_min": 4800.0,
    "sleep_social_target": 1.0,           # 睡眠社交回充（v1 无任何睡眠回充通路）
    "sleep_social_tau_min": 240.0,
    # ── 清醒消耗（v2 对消耗型轴保留画像线性速率，睡眠债放大疲劳积累；方案 §3
    #    「睡眠债适度降低可恢复水平或增加清醒消耗」）──────────────────────────
    "awake_energy_drain_scale_per_debt_h": 0.04,   # 每小时债 +4% 消耗速率
    # ── 无画像/画像零速率段的基础时钟（评审 2026-11-03 修复：v2 无画像清醒段
    #    不再冻结 energy/focus/stress）。取值 = v1 被动漂移同值，仅在该段
    #    「无 target 且有效速率为 0」时生效（与画像速率互斥、不叠加——仍是替代）；
    #    energy 分量按睡眠债放大（×1 + awake_energy_drain_scale_per_debt_h × 债），
    #    落实方案 §3「清醒时长累积疲劳」「睡眠债增加清醒消耗」────────────────────
    "no_profile_energy_drain_per_min": -0.00005,
    "no_profile_focus_drain_per_min": -0.00015,
    "no_profile_stress_drain_per_min": -0.00005,
    # ── 饥饿（清醒非用餐：基础代谢时钟 + 活动强度合成 tau；方案 §3「由距上次
    #    进食时间、清醒/睡眠与活动强度累计」「正常清醒约 4~6 小时渐升到 50~70」）──
    "hunger_awake_target": 1.0,
    "hunger_awake_tau_min": 480.0,        # 8h：餐后 0.15 → 4h ≈ 0.55、6h ≈ 0.65
    "hunger_intensity_ref_per_h": 4.0,    # 画像 hunger_rate 折算强度基准（点/h）
    "hunger_tau_floor_min": 240.0,        # 活动强度合成 tau 的下限
    # ── 社交电量（安静独处清醒回充，tau 以小时计；方案 §3「安静恢复以数小时计」）──
    "social_recharge_target": 1.0,
    "social_recharge_tau_min_awake": 360.0,        # 6h（睡眠期用 sleep_social_*）
    # ── 活动画像 target/tau（[目标, 时间常数分钟]；缺轴 = 该画像该轴仍走画像
    #    线性速率。候选值：待机精力 78 / 休息精力 82（方案 70~85）、待机专注
    #    35（「低刺激、注意力松散」）、用餐饥饿 15（方案 10~20）等）────────────
    "activity": {
        # 复核 P1-6：idle 的 focus 回归靶心 0.35→0.60（中性恢复水平，与 rest 0.62
        # 同带）——原 0.35 使待机后 work/study 的 focus_fit 惩罚过重，形成
        # 「待机→专注更低→选不上工作→继续待机」的负反馈。
        "idle":         {"energy": [0.78, 300.0], "focus": [0.60, 180.0],
                         "stress": [0.05, 240.0]},
        "rest":         {"energy": [0.82, 90.0], "focus": [0.62, 60.0],
                         "stress": [0.04, 60.0]},
        "meal":         {"energy": [0.70, 240.0], "hunger": [0.15, 15.0],
                         "focus": [0.50, 120.0], "stress": [0.04, 90.0]},
        "transition":   {"stress": [0.05, 120.0]},
        "entertainment": {"energy": [0.72, 240.0], "stress": [0.05, 120.0]},
    },
    # ── 用餐强度建议（调度侧只读接口 :func:`meal_scheduling_hint`；不改调度）────
    "meal_light_below_hunger": 0.20,      # 饥饿低于该值建议轻食/缩短用餐
    "meal_light_duration_scale": 0.6,
    # ── 诊断（硬要求 6：来源分解落 ``life_state.json`` 的 ``settlement_diag``）────
    "diag_max_entries": 8,
}

#: 趋近式适用的五轴（与 life_state.STATE_FIELDS 同序语义；本模块不 import life_state）
AXES: Tuple[str, ...] = ("energy", "hunger", "focus", "stress", "social_battery")

#: 诊断 contrib 的来源桶（硬要求 6：基础/活动/睡眠/事件/情绪）
CONTRIB_BUCKETS: Tuple[str, ...] = ("basal", "activity", "sleep", "event", "emotion")

# ── 目标私聊社交电量折减（2026-11-11 Gemini 批，报告 §4.1 P1-NUM-2）──────────
# 真实聊天事件的结算路径：
#   companion/components.py::_mai_chat_social_drain（目标流真实回复）
#   → LifeStateService.apply_episode(state_deltas={"social_battery": Δ})
#   → life_state.STATE_FIELDS clamp [0,1] + settlement_diag event 桶 kind=chat。
# ``target_private`` 由该处「回复流 == 巡检目标流」严格判定（群聊/其他私聊天然
# 排除），再作为本组纯函数的入参，决定是否进入好感折减/回充分支。
#: 好感满值（与 companion.affection.SCORE_MAX / affection_presets.SCORE_MAX 同口径；
#: 本模块不 import companion，保持纯函数层零依赖）。
TARGET_CHAT_AFFINITY_SCORE_MAX = 200.0
#: 折减强度跨度：满好感时消耗降至 ``1 - 0.9 = 0.1`` 倍。
TARGET_CHAT_AFFINITY_DISCOUNT_SPAN = 0.9
#: 折减系数下限（绝不为 0：保留最低交互成本）。
TARGET_CHAT_DISCOUNT_FLOOR = 0.1
#: 「高好感」门槛：内置预设最高档起点 176/200（affection_presets.BUILTIN_TIER_BOUNDS）。
TARGET_CHAT_RECHARGE_MIN_SCORE = 176.0
#: 单次真实回复的微量回充上限（0~1 尺度；约为默认消耗 0.015 的 1/4）。
TARGET_CHAT_RECHARGE_PER_REPLY = 0.004
#: 「放松对话」判据：情绪效价 V ≥ 0（V 不可得 → 不判定放松、不回充）。
TARGET_CHAT_RELAXED_VALENCE = 0.0


def default_params() -> Dict[str, Any]:
    """内置参数深拷贝（调用方可自由改写而不污染 :data:`V2_PARAMS`）。"""
    return json.loads(json.dumps(V2_PARAMS, ensure_ascii=False))


def _finite(value: Any, default: float) -> float:
    """宽松数值解析：非数值/NaN/inf → 默认。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if number != number or number in (math.inf, -math.inf):
        return default
    return number


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def merge_params(overrides: Any, logger: Any = None) -> Dict[str, Any]:
    """内置默认 ← 用户覆盖的**校验合并**（只收已知键；坏值保留默认并告警）。

    ``overrides`` 为 dict（通常来自 ``settlement_params_json`` 的 json.loads）；
    ``activity`` 子表按 画像→轴→[target, tau] 逐项合并。任何非法项单独丢弃，
    不整体作废（与 life_state._apply_raw 字段级容错同纪律）。
    """
    params = default_params()
    if not isinstance(overrides, dict) or not overrides:
        return params
    for key, value in overrides.items():
        if key not in params:
            _warn(logger, "参数未知键 %r 丢弃（合法键集见 planning/settlement_v2.V2_PARAMS）", key)
            continue
        default = params[key]
        if key == "activity":
            if not isinstance(value, dict):
                _warn(logger, "参数 activity 非对象，忽略该键")
                continue
            activity = params["activity"]
            for profile_key, axes in value.items():
                if not isinstance(axes, dict):
                    _warn(logger, "参数 activity.%s 非对象，忽略", profile_key)
                    continue
                merged_axes: Dict[str, Any] = dict(activity.get(str(profile_key), {}))
                for axis, spec in axes.items():
                    pair = _parse_target_tau(spec)
                    if pair is None:
                        _warn(logger, "参数 activity.%s.%s 非法（需 [0~1目标, >0分钟]），忽略",
                              profile_key, axis)
                        continue
                    merged_axes[str(axis)] = pair
                if merged_axes:
                    activity[str(profile_key)] = merged_axes
            continue
        if isinstance(default, int) and not isinstance(default, bool):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                _warn(logger, "参数 %s 需非负整数（得 %r），保留默认 %s", key, value, default)
                continue
            params[key] = int(value)
        elif isinstance(default, float):
            number = _finite(value, float(default))
            if key.endswith("_target"):
                number = _clamp01(number)
            elif key.startswith("no_profile_"):
                # 基础漂移分量约定 ≤0（消耗方向）；正值无意义 → 保留默认
                if number > 0.0:
                    _warn(logger, "参数 %s 为漂移分量需 ≤0（得 %r），保留默认 %s",
                          key, value, default)
                    continue
            elif number <= 0.0:
                _warn(logger, "参数 %s 需 >0（得 %r），保留默认 %s", key, value, default)
                continue
            params[key] = float(number)
        else:
            _warn(logger, "参数 %s 类型不支持覆盖，保留默认", key)
    return params


def _warn(logger: Any, message: str, *args: Any) -> None:
    """参数合并告警（logger 可为 None：纯函数层无 logger 时静默降级）。"""
    if logger is not None:
        try:
            logger.warning("生活状态结算 v2：" + message, *args)
        except Exception:
            pass


def _parse_target_tau(spec: Any) -> Optional[Tuple[float, float]]:
    """``[target, tau_min]`` 宽松解析：target 钳 [0,1]、tau > 0；非法 → None。"""
    if not isinstance(spec, (list, tuple)) or len(spec) != 2:
        return None
    target = _finite(spec[0], -1.0)
    tau = _finite(spec[1], -1.0)
    if target < 0.0 or tau <= 0.0:
        return None
    return _clamp01(target), tau


def approach_value(x_now: float, target: float, tau_min: float, dt_min: float) -> float:
    """目标趋近闭式解：``target + (x - target) * exp(-dt / tau)``（方案 §3 原式）。

    ``tau <= 0`` / ``dt <= 0`` 防御：tau 非法按无穷 tau（即不动）处理，dt<=0 原值。
    """
    if dt_min <= 0.0:
        return float(x_now)
    if tau_min is None or tau_min <= 0.0 or tau_min != tau_min:
        return float(x_now)
    return target + (float(x_now) - target) * math.exp(-float(dt_min) / float(tau_min))


def target_chat_affinity_factor(affinity: Any, *, enabled: bool = True) -> float:
    """目标私聊好感折减系数 ``max(0.1, 1 - 0.9 × affinity / 200)``（纯函数）。

    - ``enabled=False``（``[life_state].social_battery_affinity_discount`` 关）→ 恒 1.0；
    - ``affinity`` 非法/缺失由调用方决定是否传入；本函数对 ``None`` 按 0 处理
      （系数 1.0），但生产路径在好感读数不可得时**不进入折减**（见
      :func:`target_chat_social_delta` 的 ``affinity=None`` 分支）。
    """
    if not enabled:
        return 1.0
    score = max(0.0, min(TARGET_CHAT_AFFINITY_SCORE_MAX, _finite(affinity, 0.0)))
    return max(TARGET_CHAT_DISCOUNT_FLOOR,
               1.0 - TARGET_CHAT_AFFINITY_DISCOUNT_SPAN
               * score / TARGET_CHAT_AFFINITY_SCORE_MAX)


def target_chat_social_delta(drain: Any, *, target_private: bool,
                             affinity: Any = None,
                             social_battery: Any = None,
                             discount_enabled: bool = True,
                             recharge_enabled: bool = True,
                             valence: Any = None) -> Dict[str, Any]:
    """目标陪伴私聊单次真实回复的社交电量增量（纯函数，P1-NUM-2）。

    结算路径与 ``target_private`` 接线：``companion/components.py`` 的
    ``_mai_chat_social_drain`` 只在「回复流 == 巡检目标流」时进入（F6 同口径；
    群聊/其他私聊/流缺失直接 return），并把该判定显式作为 ``target_private=True``
    连同 ``state_deltas`` 交给 ``LifeStateService.apply_episode``。本函数是该增量
    的唯一计算点，``target_private=False`` 时逐字返回旧消耗（不折减、不回充）。

    规则：

    - 基础消耗 ``drain``（internal_tuning ``chat_social_drain_per_reply``，默认
      0.015/次；≤0/非法 → 0）；
    - 目标私聊 + 好感可得 + 开关开：消耗 × ``target_chat_affinity_factor``
      （满好感 0.1 倍，最低 0.1）；好感读数不可得 → 不折减（保守，宁可按旧口径）；
    - 高好感（≥ :data:`TARGET_CHAT_RECHARGE_MIN_SCORE`）**且**放松对话
      （``valence`` 可得且 ≥ :data:`TARGET_CHAT_RELAXED_VALENCE`）且开关开 →
      额外微量回充 ``TARGET_CHAT_RECHARGE_PER_REPLY``，并按「先扣消耗后的剩余
      电量」封顶，**绝不把电量推过 1.0**（``social_battery=None`` 时退化为
      仅按回充上限，实际落地仍由 apply_episode 的 clamp 兜底）；
    - 返回 ``delta``（可直接放进 ``state_deltas["social_battery"]``）与诊断分解。
    """
    raw = max(0.0, _finite(drain, 0.0))
    score: Optional[float] = None
    if affinity is not None:
        score = max(0.0, min(TARGET_CHAT_AFFINITY_SCORE_MAX, _finite(affinity, 0.0)))
    factor = 1.0
    reason = "not_target_private"
    discount_applied = False
    if target_private:
        if not discount_enabled:
            reason = "discount_disabled"
        elif score is None:
            reason = "affinity_unavailable"
        else:
            factor = target_chat_affinity_factor(score)
            discount_applied = True
            reason = "target_private"
    effective = raw * factor
    relaxed = (valence is not None
               and _finite(valence, -1.0) >= TARGET_CHAT_RELAXED_VALENCE)
    recharge = 0.0
    if (target_private and recharge_enabled and score is not None
            and score >= TARGET_CHAT_RECHARGE_MIN_SCORE and relaxed):
        recharge = TARGET_CHAT_RECHARGE_PER_REPLY
        if social_battery is not None:
            current = _clamp01(_finite(social_battery, 0.0))
            headroom = max(0.0, 1.0 - max(0.0, current - effective))
            recharge = min(recharge, headroom)
        reason = "target_private_recharge" if recharge > 0.0 else reason
    delta = recharge - effective
    if abs(delta) < 1e-12:  # 满电时「扣完即回满」的浮点残差归零，避免 -5e-17 记账
        delta = 0.0
    return {
        "delta": delta,
        "drain": raw,
        "effective_drain": effective,
        "factor": factor,
        "recharge": recharge,
        "affinity": score,
        "target_private": bool(target_private),
        "discount_applied": discount_applied,
        "relaxed": bool(relaxed),
        "reason": reason,
    }


def _activity_axis(params: Mapping[str, Any], profile_key: Optional[str],
                   axis: str) -> Optional[Tuple[float, float]]:
    """画像 → 该轴 [target, tau]；画像未配/轴未配 → None（该轴走画像线性速率）。"""
    if not profile_key:
        return None
    axes = params.get("activity", {}).get(str(profile_key))
    if not isinstance(axes, dict):
        return None
    return _parse_target_tau(axes.get(axis))


def integrate_awake(state: Mapping[str, Any], dt_min: float,
                    profile_key: Optional[str], rates: Optional[Mapping[str, Any]],
                    params: Mapping[str, Any], sleep_debt: float) -> Dict[str, Any]:
    """清醒段 v2 积分（纯函数；返回各轴增量 + target/tau + 来源标注）。

    规则（轴间互斥，替代而非叠加——方案 §3「不能新旧各算一次」）：

    - ``energy``：画像配了 target（idle/rest/meal/entertainment）→ 趋近；否则
      画像线性速率（无则取 ``no_profile_energy_drain_per_min`` 基础疲劳时钟），
      负速率（消耗）按睡眠债放大（×1 + 4%/h 债）——无画像/零速率段不再冻结，
      「清醒时长累积疲劳」与「睡眠债增加清醒消耗」在全场景成立；
    - ``hunger``：用餐画像 → 向饱腹 target 趋近（替代 v1 −0.0180/min 线性）；
      其余 → 基础代谢时钟向 1.0 趋近，画像 ``hunger_rate`` 折算活动强度合成 tau；
    - ``focus`` / ``stress``：画像配了 target → 趋近；否则画像线性速率
      （无则取 ``no_profile_focus/stress_drain_per_min`` 基础分量）。
      stress 额外受睡眠债基线下限（3.17.20 §1B）：趋近 target 取
      ``max(activity_target, debt_stress_per_h × debt)``，线性分支结果不低于
      该 floor（债0 → floor 0 → 与旧数学逐字一致）；
    - ``social_battery``：画像 ``social_rate < 0``（真实交流）→ 线性消耗；否则
      安静回充向 1.0 趋近（tau 以小时计，接近满电自然减慢）。
    """
    params = params if isinstance(params, Mapping) else V2_PARAMS
    rates = rates if isinstance(rates, Mapping) else {}
    debt = max(0.0, _finite(sleep_debt, 0.0))
    dt = max(0.0, _finite(dt_min, 0.0))
    delta: Dict[str, float] = {axis: 0.0 for axis in AXES}
    target: Dict[str, Optional[float]] = {axis: None for axis in AXES}
    tau: Dict[str, Optional[float]] = {axis: None for axis in AXES}
    source: Dict[str, str] = {axis: "activity" for axis in AXES}

    if dt <= 0.0:
        return {"delta": delta, "target": target, "tau_min": tau, "source": source}

    # energy：画像 target 趋近 / 线性速率（无速率 → 基础疲劳时钟；消耗按债放大）
    pair = _activity_axis(params, profile_key, "energy")
    if pair is not None:
        target["energy"], tau["energy"] = pair
        new_value = approach_value(state.get("energy", 0.0), pair[0], pair[1], dt)
        delta["energy"] = new_value - float(state.get("energy", 0.0))
    else:
        rate = _finite(rates.get("energy_rate"), 0.0)
        if rate == 0.0:
            # 画像未提供有效速率（无画像/零速率段）：基础疲劳时钟（v1 被动漂移
            # 同值）——评审 2026-11-03 修复，防无画像段精力长期钉在高值
            rate = _finite(params.get("no_profile_energy_drain_per_min"), -0.00005)
            source["energy"] = "basal"
        if rate < 0.0:
            rate *= 1.0 + params.get("awake_energy_drain_scale_per_debt_h", 0.04) * debt
        delta["energy"] = rate * dt

    # hunger：用餐饱腹趋近 / 基础时钟 + 活动强度合成 tau
    pair = _activity_axis(params, profile_key, "hunger")
    if pair is not None:
        target["hunger"], tau["hunger"] = pair
        new_value = approach_value(state.get("hunger", 0.0), pair[0], pair[1], dt)
        delta["hunger"] = new_value - float(state.get("hunger", 0.0))
        source["hunger"] = "activity"
    else:
        base_tau = _finite(params.get("hunger_awake_tau_min"), 480.0)
        intensity = max(0.0, _finite(rates.get("hunger_rate"), 0.0)) * 60.0 \
            / max(0.001, _finite(params.get("hunger_intensity_ref_per_h"), 4.0))
        tau_eff = max(_finite(params.get("hunger_tau_floor_min"), 240.0),
                      base_tau / (1.0 + intensity))
        target["hunger"] = _clamp01(_finite(params.get("hunger_awake_target"), 1.0))
        tau["hunger"] = tau_eff
        new_value = approach_value(state.get("hunger", 0.0), target["hunger"], tau_eff, dt)
        delta["hunger"] = new_value - float(state.get("hunger", 0.0))
        source["hunger"] = "basal"

    # focus / stress：画像 target 趋近 / 线性速率（无速率 → 基础分量）
    # PR-REALISM-TUNING（3.17.20 §1B）：睡眠债抬升压力基线——有效 stress target =
    # max(activity_target, debt_stress_floor)，floor = debt_stress_per_h × debt
    # （默认 0.01/h，经 internal_tuning 注入 params；债0 → floor 0 → 数学逐字不变）；
    # 线性速率分支结果 likewise 不低于 floor。睡眠期 integrate_sleep 不受影响。
    debt_stress_floor = _clamp01(
        max(0.0, _finite(params.get("debt_stress_per_h"), 0.01)) * debt)
    for axis in ("focus", "stress"):
        pair = _activity_axis(params, profile_key, axis)
        if pair is not None:
            axis_target = pair[0]
            if axis == "stress" and debt_stress_floor > axis_target:
                axis_target = debt_stress_floor
            target[axis], tau[axis] = axis_target, pair[1]
            new_value = approach_value(state.get(axis, 0.0), axis_target, pair[1], dt)
            delta[axis] = new_value - float(state.get(axis, 0.0))
        else:
            rate = _finite(rates.get(f"{axis}_rate"), 0.0)
            if rate == 0.0:
                rate = _finite(params.get(f"no_profile_{axis}_drain_per_min"),
                               -0.00015 if axis == "focus" else -0.00005)
                source[axis] = "basal"
            delta[axis] = rate * dt
            if axis == "stress" and debt_stress_floor > 0.0:
                new_value = float(state.get(axis, 0.0)) + delta[axis]
                if new_value < debt_stress_floor:
                    delta[axis] = debt_stress_floor - float(state.get(axis, 0.0))

    # social_battery：真实交流线性消耗 / 安静回充趋近
    social_rate = _finite(rates.get("social_battery_rate"), 0.0)
    if social_rate < 0.0:
        # PR-STATE-CONTRACT（3.17.22 P1-C）：计划社交消耗幅度有界上限（internal_tuning
        # planned_social_drain_cap_per_min，默认 0.006/min）——旧默认画像 -0.02/min 使
        # 40min 社交一次触底；上限经参数注入生效于生产，不改用户画像表层映射。与真实
        # 聊天每回复消耗（3.17.20，event 桶 kind=chat）来源分离、各自有界，diag 可区分。
        cap = max(0.0, _finite(params.get("planned_social_drain_cap_per_min"), 0.006))
        social_rate = max(social_rate, -cap)
        delta["social_battery"] = social_rate * dt
    else:
        target["social_battery"] = _clamp01(
            _finite(params.get("social_recharge_target"), 1.0))
        tau["social_battery"] = max(0.1, _finite(
            params.get("social_recharge_tau_min_awake"), 360.0))
        new_value = approach_value(state.get("social_battery", 0.0),
                                   target["social_battery"], tau["social_battery"], dt)
        delta["social_battery"] = new_value - float(state.get("social_battery", 0.0))
        source["social_battery"] = "basal"

    return {"delta": delta, "target": target, "tau_min": tau, "source": source}


def integrate_sleep(state: Mapping[str, Any], dt_min: float,
                    params: Mapping[str, Any], sleep_debt: float) -> Dict[str, Any]:
    """睡眠段 v2 积分（纯函数）：五轴全部目标趋近（替代 v1 曲线速率）。

    - ``energy``：目标 = 睡眠精力目标 − 每小时债 × 修调（钳 [floor, target]）——
      「睡眠债适度降低可恢复水平」（方案 §3）；睡前已高于目标则自然回落、
      不再向 1.0 加固定收益（方案 §2.3 clamp 丢弃问题）；
    - ``social_battery``：睡眠回充（v1 恒 0 的通路，方案 §2.1 表「非社交/睡眠」行）；
    - ``hunger`` / ``stress`` / ``focus``：各自的睡眠期 target/tau。
    """
    params = params if isinstance(params, Mapping) else V2_PARAMS
    debt = max(0.0, _finite(sleep_debt, 0.0))
    dt = max(0.0, _finite(dt_min, 0.0))
    delta: Dict[str, float] = {axis: 0.0 for axis in AXES}
    target: Dict[str, Optional[float]] = {axis: None for axis in AXES}
    tau: Dict[str, Optional[float]] = {axis: None for axis in AXES}
    source: Dict[str, str] = {axis: "sleep" for axis in AXES}
    if dt <= 0.0:
        return {"delta": delta, "target": target, "tau_min": tau, "source": source}

    base = _clamp01(_finite(params.get("sleep_energy_target"), 0.92))
    floor = _clamp01(_finite(params.get("sleep_energy_target_floor"), 0.80))
    drain = max(0.0, _finite(params.get("sleep_energy_target_drain_per_debt_h"), 0.02))
    target["energy"] = max(min(floor, base), base - drain * debt)
    tau["energy"] = max(0.1, _finite(params.get("sleep_energy_tau_min"), 90.0))

    target["focus"] = _clamp01(_finite(params.get("sleep_focus_target"), 0.80))
    tau["focus"] = max(0.1, _finite(params.get("sleep_focus_tau_min"), 120.0))
    target["stress"] = _clamp01(_finite(params.get("sleep_stress_target"), 0.0))
    tau["stress"] = max(0.1, _finite(params.get("sleep_stress_tau_min"), 240.0))
    target["hunger"] = _clamp01(_finite(params.get("sleep_hunger_target"), 1.0))
    tau["hunger"] = max(0.1, _finite(params.get("sleep_hunger_tau_min"), 4800.0))
    target["social_battery"] = _clamp01(_finite(params.get("sleep_social_target"), 1.0))
    tau["social_battery"] = max(0.1, _finite(params.get("sleep_social_tau_min"), 240.0))

    for axis in AXES:
        new_value = approach_value(state.get(axis, 0.0), target[axis] or 0.0,
                                   tau[axis] or 1.0, dt)
        delta[axis] = new_value - float(state.get(axis, 0.0))
    return {"delta": delta, "target": target, "tau_min": tau, "source": source}


def sleep_state_gains(state: Mapping[str, Any], hours: float, params: Mapping[str, Any],
                      sleep_debt: float) -> Dict[str, float]:
    """整段睡眠的 v2 状态增量（离线结算/回放共用在线同一积分函数——方案 §5.2）。

    返回键与 v1 ``integrate_sleep_gains`` 输出对齐（五项增量 + ``hours``），可直接
    走 ``LifeStateService.apply_episode({"state_deltas": ...})`` 既有一次性增量门；
    非法 ``hours``（负/NaN）按 0（无增量）。
    """
    dt_min = max(0.0, _finite(hours, 0.0)) * 60.0
    result = integrate_sleep(state, dt_min, params, sleep_debt)
    gains = {axis: result["delta"][axis] for axis in AXES}
    gains["hours"] = dt_min / 60.0
    return gains


def meal_scheduling_hint(state: Mapping[str, Any], params: Mapping[str, Any]) -> Dict[str, Any]:
    """调度侧只读接口（硬要求 4）：按当前饥饿建议用餐强度，**不改变调度行为**。

    返回 ``{"hunger", "mode", "duration_scale", "satiety_target", "note"}``：
    ``mode`` ∈ full（正常正餐）/ light（建议轻食或缩短用餐时长，保留极饿应急由
    调度侧的「高饥饿强制进餐」通路承担）。该接口只读。
    """
    params = params if isinstance(params, Mapping) else V2_PARAMS
    hunger = _clamp01(_finite(_state_value(state, "hunger"), 0.0))
    threshold = _clamp01(_finite(params.get("meal_light_below_hunger"), 0.20))
    scale = max(0.1, _finite(params.get("meal_light_duration_scale"), 0.6))
    pair = _activity_axis(params, "meal", "hunger")
    satiety = pair[0] if pair else 0.15
    if hunger < threshold:
        return {"hunger": hunger, "mode": "light", "duration_scale": scale,
                "satiety_target": satiety,
                "note": "低饥饿：建议轻食或缩短用餐时长（调度侧自行取舍）"}
    return {"hunger": hunger, "mode": "full", "duration_scale": 1.0,
            "satiety_target": satiety, "note": ""}


def _state_value(state: Mapping[str, Any], axis: str) -> Any:
    try:
        return state.get(axis)
    except Exception:
        return None


def empty_contrib() -> Dict[str, Dict[str, float]]:
    """诊断 contrib 骨架（五桶齐备，空桶为空 dict——键形稳定便于对账）。"""
    return {bucket: {} for bucket in CONTRIB_BUCKETS}
