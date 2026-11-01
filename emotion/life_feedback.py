"""情绪-生活反馈（R7 = PR2 双向闭环，2026-10-26，计划书 §三十五/定稿 §4.1.2）。

LifeState/Episode → VAD 反馈侧：订阅滚动执行层发布的 ``life.episode_completed`` /
``life.episode_interrupted``（发布点 planning/rolling_planner，经 publish_soon 异步），
按第七张映射表 ``mappings/emotion_life_modulation.json`` 的 ``feedback`` 节把 episode
事件折算成有界 VAD 增量，经 :meth:`EmotionService.record_external_event` 公共 API 入账
（零 LLM；绝不直改 EmotionStateMachine 内部，VAD 写入只走情绪公共入口）。

表结构（定稿 §4.1.1 + §4.1.2，附录 B.3 与定稿系数结构冲突，按定稿落）：

- 顶层 ``version``（恒 1）+ ``gains``（§4.1.1 VAD→LifeState 五项 rise/fall 调制系数与
  下限——消费方 planning/life_modulation 属后续 PR，本模块只装载与透传，不应用）+
  ``feedback``（§4.1.2 事件类增量与日上限，**本模块消费**）；
- ``feedback`` 每条 = ``match``（event: completed/interrupted/random、goal_types、
  min_duration_minutes）+ ``delta``（V/A/D 增量）+ ``daily_cap``（逐轴日上限，带符号：
  正上限只封正向累计、负上限只封负向累计）；
- 一条事件可同时命中多条（如 ≥4h 的工作块同时命中「深度工作完成」与「高压力长块」），
  逐条独立判日上限后求和入账；全零 delta 条目（随机小事件默认 0）自动跳过，不占预算。

设计红线（定稿 §4.1.2）：

- ``goal_type == "sleep"`` 直接跳过——睡眠期 VAD 由 apply_sleep_overhaul 统一结算，
  在此入账即双记账；
- 每日预算 ``<data_dir>/life_feedback_budget.json``（date + 已用 V/A/D + 逐规则累计 +
  episode_id entries 去重），经 ``core.storage.save_json_atomic`` 原子写；超日上限整条
  丢弃并 debug 日志；**绝不写进 emotion_state.json**（未知键会被 ``_save_state`` 冲掉）；
  日期滚动自然重置（读到的 date 非当天即视为新账本）；
- **全局每日上限口径（2026-10-27 用户裁定）**：``used`` 与 ``GLOBAL_VAD_DAILY_CAP`` 按
  **最终实际入账增量**——即经 ``record_external_event_sync`` 同一倍率真源
  （``delta × global_event_multiplier × K``）与 ±0.5 硬限幅后的 applied 值检查并记账，
  不是映射表规则输入 delta。判定发生在真正入账**之前**（``preview_external_delta``
  只读预览，同一 ``_scaled_external_delta`` 真源；其间无 await），不做「先入账再回滚」；
  实际 delta 全零的事件不占预算、不入账。逐规则 ``daily_cap`` 与 ``rules`` 累计仍按
  **规则输入 delta** 定义（主代理审定：不为此擅改映射表数值）；
- **冻结期（睡眠）跳过（2026-10-27 用户裁定 / Q2）**：情绪 ``is_frozen()`` 为真时事件
  只会在状态机里入队、醒来按设计丢弃 → 没有实际入账，故反馈侧直接跳过、不占预算
  （与 ``goal_type == sleep`` 的跳过原则一致），只留 debug 诊断；
- **账本口径标记与旧账本迁移（2026-10-27 用户裁定）**：账本带
  ``used_basis = applied_v1``（``BUDGET_USED_BASIS``）。同日旧账本缺该标记且 ``used``
  非零时，历史 actual 无法由 raw 规则输入恢复 → **当日暂停新增生活反馈**（其他聊天情绪
  来源不受影响），旧 ``used``/``rules``/``entries`` 原样保留并补
  ``used_basis = legacy_raw_unrecoverable`` + ``budget_hold`` 留痕（不猜历史倍率、不清空
  旧账放大额度、不把未知 actual 伪写成准确已用值）；次日 date 滚动自动进 actual 新口径。
  旧账 ``used`` 净零但 ``rules``/``entries`` 仍有正负抵消的历史消耗时，actual 同样不可由
  raw 恢复 → 仍 hold；只有 used/rules/entries 均空或全零的旧账才正常进入新口径
  （不无故停一天）；同日重启仍 hold；
- 同一 ``episode_id`` 只入账一次（entries 去重；无 episode_id 的事件无从去重，照常入账）；
- **并发（2026-10-26 修复）**：账本读取 → 逐规则/全局日上限判定 → VAD 入账 → 账本提交
  必须是一个**不含 await 的同步区**。入账走 ``record_external_event_sync``（同步核心，
  无模型调用、不发布），随后立即提交账本，最后才 ``await publish_external_updated()``；
  旧实现「``await record_external_event`` 之后再落账本」的 await 窗口会让第二个协程读到
  陈旧账本（同事件重复入账 / 全局日封顶被击穿 / 账本读-改-写丢更新），await 期间被取消
  还会让已入账事件不占预算而在重试时二次入账。账本另持**进程内镜像**：写盘失败或无
  ``_data_dir`` 时同日仍按已入账账本判定（恰一次不依赖写盘成功）；
- 开关（``[schedule].enabled`` / ``[life_state].emotion_modulation_enabled`` /
  情绪模块总开关）任一关 → 零订阅零可观测；关闭态退订清理由 EmotionServices.stop() 统一
  执行（本模块订阅闭包已并入服务束 ``_event_unsubs``）；
- 映射表只在装载期读一遍（热重载 = 宿主重导入重跑 on_load 自动重读）：表层缺失 →
  ``ensure_user_table`` 按序补链（旧根迁移优先，仍缺才从深层种子复制）；解析错误 /
  校验不过 → 内置默认（``BUILTIN_TABLE``），**绝不读取或覆盖表层文件内容**（红线同
  ``core/mapping_dir.ensure_user_table``）；root_dir 缺失（独立测试桩）→ 直接内置默认。
"""

from __future__ import annotations

import copy
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.coerce import to_float as _to_float
from core.storage import load_json, save_json_atomic

from .vad import VADState

logger = logging.getLogger(__name__)

#: 入账来源（定稿 §4.1.2：``record_external_event(source="life_episode", ...)``）
LIFE_EPISODE_SOURCE = "life_episode"

#: 每日预算文件（相对插件数据目录；不进 emotion_state.json，理由见模块 docstring）
FEEDBACK_BUDGET_FILE = "life_feedback_budget.json"

#: 睡眠 episode 的 goal_type（与 planning/rolling_planner.SLEEP_GOAL_TYPE 同值——
#: planning 侧不 import emotion（单向依赖），为免拉入整模块此处留同值常量，两处需一起改）
SLEEP_GOAL_TYPE = "sleep"

#: 订阅的事件名（与 planning/rolling_planner.EVENT_EPISODE_COMPLETED/_INTERRUPTED 同值，
#: 同上口径两处需一起改）
EVENT_EPISODE_COMPLETED = "life.episode_completed"
EVENT_EPISODE_INTERRUPTED = "life.episode_interrupted"

#: VAD 轴名（与 VADState 字段同序；预算/日上限逐轴记账用）
_VAD_AXES = ("valence", "arousal", "dominance")

#: 全零增量的判定容差（低于它视为 0，跳过入账）
_ZERO_EPS = 1e-9

#: 全局每日 VAD 回写封顶（PR2.5 修复（2026-10-26），交接 §五.18 D3 / 定稿
#: §4.1.3「每日 VAD 回写封顶」；常量对齐附录 B.3 ``daily_caps.vad_delta=0.15``。
#: 裁量登记：附录 B.3 的 daily_caps 节在随包种子表内无对应结构，故取常量落码；
#: 语义 = 三轴各自当日累计（含正负向）的绝对值不得超过此上限，超限整条事件丢弃。
#: 2026-10-27 用户裁定：累计与判定都按**最终实际入账增量**（倍率/限幅后），见模块注释。
GLOBAL_VAD_DAILY_CAP = 0.15

#: 账本 ``used`` 的口径标记（2026-10-27 用户裁定：最终实际入账增量）。
#: 旧账本无此标记且 ``used`` 非零 → 无法由 raw 恢复 actual，保守 fail-closed（见模块注释）。
BUDGET_USED_BASIS = "applied_v1"

#: 旧账本 hold 标记（2026-10-27 用户裁定）：同日非空 raw 口径账本无法恢复 actual → 当日
#: 暂停新增生活反馈、旧数据原样保留；次日自然滚动进 actual 新口径。此标记只表示
#: 「当日不可用」，绝不把未知 actual 伪写成准确已用值（``used`` 保持原 raw 数值）。
BUDGET_USED_BASIS_LEGACY = "legacy_raw_unrecoverable"


_TZ_MANAGER: Any = None


def _tz_now() -> datetime:
    """配置时区（Asia/Shanghai）当前时间（PR2.5 修复（2026-10-26），交接 §五.18 D4）。

    与生活状态层同一时区真源链（参照 planning/life_state 的 TimezoneManager 用法），
    预算日界与生活状态日界对齐；zoneinfo/pytz 不可用时 TimezoneManager 内部降级
    系统本地时区。管理器模块级缓存（省重复 zoneinfo 构造开销）；延迟导入避免
    planning 包顶层依赖进 emotion 装载链。
    """
    global _TZ_MANAGER
    if _TZ_MANAGER is None:
        from planning.time_utils import TimezoneManager
        _TZ_MANAGER = TimezoneManager("Asia/Shanghai")
    return _TZ_MANAGER.get_now()


def _builtin_table() -> Dict[str, Any]:
    """内置默认表（与深层种子 ``emotion_life_modulation.json`` 同内容；种子损坏/缺失时兜底）。

    系数来源：定稿 §4.1.1（gains，本 PR 只播种不应用）+ §4.1.2（feedback，本模块消费）。
    """
    return {
        "version": 1,
        "smoothing_minutes": 15,
        "dead_zone": 0.12,
        "gain_min": 0.70,
        "gain_max": 1.30,
        "gains": {
            "energy": {
                "rise": {"low_mood": -0.25, "tension": -0.10},
                "rise_floor": 0.75,
                "fall": {"activation": 0.25, "low_mood": 0.15},
            },
            "hunger": {
                "rise": {"low_mood": 0.20, "activation": -0.10},
                "fall": {"low_mood": -0.10, "activation": 0.05},
            },
            "focus": {
                "rise": {"activation_center": 0.35, "activation_penalty": 1.2,
                         "low_mood": -0.25},
                "rise_floor": 0.60,
                "fall_same_as_rise": True,
            },
            "stress": {
                "rise": {"tension": 0.35, "low_mood": 0.15},
                "fall": {"low_mood": -0.10},
                "fall_floor": 0.80,
            },
            "social_battery": {
                "rise": {"low_mood": -0.15},
                "rise_floor": 0.80,
                "fall": {"low_mood": 0.20, "tension": 0.15},
            },
        },
        "feedback": {
            "deep_work_done": {
                "match": {"event": "completed", "goal_types": ["work", "study"],
                          "min_duration_minutes": 25},
                "delta": {"valence": 0.02, "arousal": 0.0, "dominance": 0.01},
                "daily_cap": {"valence": 0.06},
            },
            "work_interrupted": {
                "match": {"event": "interrupted", "goal_types": ["work", "study"]},
                "delta": {"valence": -0.03, "arousal": 0.02, "dominance": -0.02},
                "daily_cap": {"valence": -0.08},
            },
            "rest_done": {
                "match": {"event": "completed", "goal_types": ["rest", "exercise"]},
                "delta": {"valence": 0.02, "arousal": -0.02, "dominance": 0.0},
                "daily_cap": {"valence": 0.06},
            },
            "meal_done": {
                "match": {"event": "completed", "goal_types": ["meal"]},
                "delta": {"valence": 0.01, "arousal": 0.0, "dominance": 0.0},
                "daily_cap": {"valence": 0.03},
            },
            "high_pressure_long_block": {
                "match": {"event": "completed", "goal_types": ["work", "study"],
                          "min_duration_minutes": 240},
                "delta": {"valence": -0.02, "arousal": 0.02, "dominance": -0.01},
                "daily_cap": {"valence": -0.06},
            },
            "random_minor_event": {
                "match": {"event": "random"},
                "delta": {"valence": 0.0, "arousal": 0.0, "dominance": 0.0},
            },
        },
    }


def _axis_dict(state: Any) -> Dict[str, float]:
    """把 ``VADState`` / dict 统一成三轴 float dict（预算记账用；非法输入按 0）。"""
    if isinstance(state, dict):
        return {axis: _to_float(state.get(axis)) for axis in _VAD_AXES}
    return {axis: _to_float(getattr(state, axis, 0.0)) for axis in _VAD_AXES}


def _match_entries(feedback: Any, event_kind: str, goal_type: str,
                   duration_minutes: Optional[float]) -> List[Tuple[str, Dict[str, Any]]]:
    """按事件类型/goal_type/时长筛出命中的 feedback 条目（保持表内声明序）。

    - ``match.event`` 缺省按 ``completed`` 处理（写全更清晰，容错旧手写条目）；
    - ``match.goal_types`` 缺省 = 不限 goal_type；
    - ``match.min_duration_minutes`` 只对带时长的 completed 事件生效——interrupted 事件
      无时长字段（payload 不含，回查库的口径属后续 PR），凡带该键的条目对 interrupted
      一律不命中。
    """
    matched: List[Tuple[str, Dict[str, Any]]] = []
    if not isinstance(feedback, dict):
        return matched
    for key, spec in feedback.items():
        if not isinstance(spec, dict):
            continue
        match = spec.get("match")
        if not isinstance(match, dict):
            continue
        if str(match.get("event") or "completed") != event_kind:
            continue
        goal_types = match.get("goal_types")
        if isinstance(goal_types, list):
            wanted = {str(g).strip() for g in goal_types}
            if goal_type not in wanted:
                continue
        min_minutes = match.get("min_duration_minutes")
        if min_minutes is not None:
            if duration_minutes is None or duration_minutes + 1e-9 < _to_float(min_minutes):
                continue
        matched.append((str(key), spec))
    return matched


def _cap_allows(rule_used: Dict[str, float], delta_dict: Dict[str, float],
                cap: Dict[str, Any]) -> bool:
    """逐轴日上限判定（带符号：正上限封正向累计、负上限封负向累计；超限整条丢弃）。"""
    for axis, limit_raw in cap.items():
        if axis not in _VAD_AXES:
            continue
        limit = _to_float(limit_raw)
        nxt = _to_float(rule_used.get(axis)) + _to_float(delta_dict.get(axis))
        if limit >= 0 and nxt > limit + 1e-9:
            return False
        if limit < 0 and nxt < limit - 1e-9:
            return False
    return True


class LifeFeedbackService:
    """life.episode_* → VAD 反馈服务（三重开关门控 + 每日预算 + episode 去重 + 零 LLM）。"""

    def __init__(self, plugin: Any, emotion_service: Any) -> None:
        self._plugin = plugin
        self._emotion = emotion_service
        # 每日预算文件 = <plugin._data_dir>/life_feedback_budget.json（B.4.1 相对数据目录；
        # 无 _data_dir 的独立测试桩回退系统临时目录，语义与 EmotionService 同款）
        raw_dir = getattr(plugin, "_data_dir", None) or ""
        self._budget_path = Path(raw_dir) / FEEDBACK_BUDGET_FILE if raw_dir else None
        # 第七张映射表只在装载期读一遍（热重载 = 服务重建自动重读）；``gains`` 节本 PR
        # 只播种不应用（消费方 planning/life_modulation 属后续 PR），留在 self.table 供观测
        self.table = self._load_table()
        self._unsubs: list = []
        #: 当日账本的**进程内镜像**（date + used + rules + entries）：写盘失败 / 无
        #: ``_data_dir`` 的实例仍能在同日按已入账账本去重与累计（恰一次不依赖写盘成功）
        self._budget_cache: Optional[Dict[str, Any]] = None

    # ── 映射表装载（表层缺失补链 / 解析错误回内置默认，红线：绝不动用户文件）──
    def _load_table(self) -> Dict[str, Any]:
        root_dir = getattr(self._plugin, "_plugin_root", None)
        if not root_dir:
            return _builtin_table()
        try:
            # 延迟导入：core.mapping_dir 顶层只依赖标准库，无环；常量登记处见其模块注释
            from core.mapping_dir import (EMOTION_LIFE_MODULATION_FILE_NAME,
                                          ensure_user_table, mappings_path, read_json_text)
            ensure_user_table(root_dir, EMOTION_LIFE_MODULATION_FILE_NAME)
            raw = read_json_text(mappings_path(Path(root_dir),
                                               EMOTION_LIFE_MODULATION_FILE_NAME))
        except Exception as exc:  # 缺文件/解析失败/权限等一律回内置默认（best-effort）
            logger.warning("情绪-生活调制表装载失败（按内置默认处理）：%s", exc)
            return _builtin_table()
        if not isinstance(raw, dict) or raw.get("version") != 1:
            logger.warning("情绪-生活调制表版本不符（需 version=1，按内置默认处理）")
            return _builtin_table()
        if not isinstance(raw.get("feedback"), dict):
            logger.warning("情绪-生活调制表缺 feedback 节（按内置默认处理）")
            return _builtin_table()
        # 「默认 ⊕ 覆盖」合并（与好感度预设表/活动画像表同款语义）：feedback 逐规则名、
        # gains 逐状态名覆盖内置，其余条目保留内置——用户只写想改的条目，不会丢默认。
        builtin = _builtin_table()
        merged = {**builtin, **raw}
        merged["feedback"] = {**builtin["feedback"], **raw["feedback"]}
        raw_gains = raw.get("gains")
        merged["gains"] = {**builtin["gains"],
                           **(raw_gains if isinstance(raw_gains, dict) else {})}
        return merged

    # ── 开关门控（注册时与每条事件各判一次；任一关 → 零动作）──
    def _gate_open(self) -> bool:
        try:
            cfg = getattr(self._plugin, "config", None)
            # PR8 3.0.0：``schedule.enabled`` 是唯一日程总开关（旧 ``[life_state].enabled``
            # 退役）；``emotion_modulation_enabled`` 独立保留（裁定 3）。
            if not bool(getattr(getattr(cfg, "schedule", None), "enabled", False)):
                return False
            if not bool(getattr(getattr(cfg, "life_state", None),
                                "emotion_modulation_enabled", False)):
                return False
        except Exception:
            return False
        try:
            return bool(self._emotion._enabled())
        except Exception:
            return False

    def _frozen(self) -> bool:
        """情绪是否处睡眠冻结态（Q2 裁定：冻结期只入队、醒来丢弃 → 非实际入账）。

        无 ``is_frozen`` 的旧服务/测试桩按「非冻结」处理（保持旧契约）。
        """
        probe = getattr(self._emotion, "is_frozen", None)
        if not callable(probe):
            return False
        try:
            return bool(probe())
        except Exception:
            return False

    def _actual_applied_delta(self, delta_total: Dict[str, float],
                              can_preview: bool) -> Tuple[Dict[str, float], bool]:
        """求本次事件**最终实际入账增量**（判定预算用，发生在入账之前）。

        有 ``preview_external_delta`` 的服务：走与入账同一倍率/限幅真源，返回 applied；
        旧服务/测试桩无预览口 → 无法得知倍率与限幅，按规则 delta 保守代理（旧口径），
        并返回 ``False`` 表示「非 actual 口径」（不写 ``used_basis`` 标记，后续读到该账本
        的 actual 服务会 fail-closed，绝不把 raw 当 actual 放大额度）。
        """
        preview = getattr(self._emotion, "preview_external_delta", None) if can_preview else None
        if callable(preview):
            try:
                result = preview(VADState(**delta_total), multiplier=1.0)
            except Exception:
                # 预览异常 → 无法确认真实增量：按「无实际入账」处理（不入账、不占预算，
                # 留诊断），绝不退回 raw 代理以免低估全局累计而击穿上限。
                logger.warning("情绪-生活反馈：实际增量预览失败（按无实际增量跳过）",
                               exc_info=True)
                return {axis: 0.0 for axis in _VAD_AXES}, False
            applied = result.get("delta") if isinstance(result, dict) else None
            if applied is not None:
                return _axis_dict(applied), True
        return {axis: _to_float(delta_total.get(axis)) for axis in _VAD_AXES}, False

    # ── 订阅（EmotionServices.start 装配；反订阅闭包并入服务束 _event_unsubs）──
    def subscribe(self) -> List[Any]:
        bus = getattr(self._plugin, "_bus", None)
        if bus is None:
            return []
        unsubs: List[Any] = []
        for event, handler in ((EVENT_EPISODE_COMPLETED, self._on_episode_completed),
                               (EVENT_EPISODE_INTERRUPTED, self._on_episode_interrupted)):
            try:
                unsubs.append(bus.subscribe(event, handler, owner="emotion_life_feedback"))
            except Exception as exc:
                logger.warning("情绪-生活反馈订阅 %s 失败（忽略）：%s", event, exc)
        self._unsubs = unsubs
        return unsubs

    def unsubscribe(self) -> None:
        """注销本服务的全部订阅（幂等；EmotionServices.stop 经 _event_unsubs 已覆盖，
        本入口供独立测试/提前停用使用）。"""
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs = []

    # ── 事件处理（completed / interrupted 共用主干；异常绝不外抛——总线单订阅者
    #    隔离之外再加全包裹，与 EmotionService._on_schedule_generated 同款纪律）──
    async def _on_episode_completed(self, payload: Any) -> None:
        await self._handle("completed", payload)

    async def _on_episode_interrupted(self, payload: Any) -> None:
        await self._handle("interrupted", payload)

    async def _handle(self, event_kind: str, payload: Any) -> None:
        try:
            if not isinstance(payload, dict):
                payload = {}
            if not self._gate_open():
                return
            if self._frozen():
                # Q2 裁定（2026-10-27）：冻结期事件只入 pending 且醒后丢弃 → 无实际入账，
                # 不得消耗全局额度（与 sleep goal 跳过同一原则）；只留诊断。
                logger.debug("情绪-生活反馈：情绪冻结态（睡眠）跳过入账，episode=%s",
                             str(payload.get("episode_id") or ""))
                return
            goal_type = str(payload.get("goal_type") or "").strip()
            if goal_type == SLEEP_GOAL_TYPE:
                # 睡眠期 VAD 由 apply_sleep_overhaul 统一结算（定稿 §4.1.2），直接跳过
                logger.debug("情绪-生活反馈：睡眠 episode（%s）跳过入账",
                             str(payload.get("episode_id") or ""))
                return
            episode_id = str(payload.get("episode_id") or "").strip()
            duration = self._episode_duration_minutes(payload)
            matched = _match_entries(self.table.get("feedback"), event_kind, goal_type,
                                     duration)
            if not matched:
                return  # 随机小事件等未命中任何条目 → 零动作零可观测
            today = _tz_now().strftime("%Y-%m-%d")  # PR2.5 D4：配置时区日界（原系统本地）
            budget = self._load_budget(today)
            # 实际增量预览能力（新增同步公共口；旧服务/桩无 → 规则 delta 代理且不标 actual）
            can_preview = callable(getattr(self._emotion, "preview_external_delta", None))
            if budget.get("legacy_basis") and (
                    budget.get("used_basis") == BUDGET_USED_BASIS_LEGACY or can_preview):
                # 用户 2026-10-27 裁定：当日非空旧 raw 口径账本 → 暂停当天新增生活反馈，
                # 次日 date 滚动自动用 actual 新口径恢复；其他聊天情绪来源不受影响。
                # 旧 used/rules/entries 原样保留，只补 basis/hold 留痕（不猜历史倍率、
                # 不清空放额度、不把未知 actual 伪写成准确已用值）。无预览口的旧服务/桩
                # 读到未标 hold 的老文件时保持旧契约直通（无法提供 actual 记账能力），
                # 其写出的账本不带 actual 标记 → 升级后的服务会按本规则 hold。
                logger.debug("情绪-生活反馈：当日账本为旧 raw 口径（actual 不可恢复），"
                             "暂停生活反馈入账至次日")
                if budget.get("used_basis") != BUDGET_USED_BASIS_LEGACY:
                    self._hold_legacy_budget(budget)
                return
            if episode_id and episode_id in budget["entries"]:
                logger.debug("情绪-生活反馈：episode %s 当日已入账，拒绝重复", episode_id)
                return
            name = str(payload.get("name") or "") or goal_type or "活动"
            description = f"生活反馈：{'完成' if event_kind == 'completed' else '被打断'}「{name}」"
            delta_total = {"valence": 0.0, "arousal": 0.0, "dominance": 0.0}
            applied_rules: List[str] = []
            # 日上限只读判定 + 增量先行累计在 pending；入账成功后才落预算（失败不动账）
            pending_rules: Dict[str, Dict[str, float]] = {}
            for key, spec in matched:
                delta_dict = {axis: _to_float((spec.get("delta") or {}).get(axis))
                              for axis in _VAD_AXES}
                if all(abs(delta_dict[axis]) <= _ZERO_EPS for axis in _VAD_AXES):
                    continue  # 全零条目（随机小事件默认 0）不占预算不触发入账
                cap = spec.get("daily_cap")
                rule_used_raw = budget["rules"].get(key)
                rule_used = rule_used_raw if isinstance(rule_used_raw, dict) else {}
                if isinstance(cap, dict) and cap and \
                        not _cap_allows(rule_used, delta_dict, cap):
                    logger.debug("情绪-生活反馈：规则 %s 触及日上限（已用 %s），本次丢弃",
                                 key, rule_used)
                    continue
                for axis in _VAD_AXES:
                    delta_total[axis] += delta_dict[axis]
                pending_rules[key] = {axis: _to_float(rule_used.get(axis)) + delta_dict[axis]
                                      for axis in _VAD_AXES}
                applied_rules.append(key)
            if not applied_rules:
                return
            # 全局每日上限（2026-10-27 用户裁定）：按**最终实际入账增量**判定——先经与
            # 入账同一真源（preview → _scaled_external_delta：三层倍率 + ±0.5 限幅）求
            # applied，再判 budget["used"]（跨规则当日净累计）+ applied 是否超
            # GLOBAL_VAD_DAILY_CAP。真实 delta 可能在倍率后变大（如 global=2 → 0.30），
            # 故绝不能用规则输入 delta 判全局上限；逐规则 daily_cap 仍按规则输入（主代理
            # 审定）。判定在入账之前，超限整条丢弃、预算不动账（不做入账后回滚）。
            actual_total, actual_basis = self._actual_applied_delta(delta_total, can_preview)
            if all(abs(actual_total[axis]) <= _ZERO_EPS for axis in _VAD_AXES):
                logger.debug("情绪-生活反馈：实际入账增量为全零（规则累计 %s），不占预算",
                             delta_total)
                return
            used_raw = budget["used"] if isinstance(budget["used"], dict) else {}
            for axis in _VAD_AXES:
                projected = _to_float(used_raw.get(axis)) + actual_total[axis]
                if abs(projected) > GLOBAL_VAD_DAILY_CAP + _ZERO_EPS:
                    logger.debug("情绪-生活反馈：全局日上限触顶（%s 累计 %s + 实际 %s），"
                                 "本次整条丢弃", axis, used_raw, actual_total)
                    return
            # entries 条目先按本次判定成形（ts 走配置时区链，PR2.5 D4）；入账成功才提交。
            # ``delta`` = 实际入账值，``delta_rule`` = 规则输入值（口径可追溯）。
            entry: Optional[Dict[str, Any]] = None
            if episode_id:
                entry = {
                    "rules": applied_rules,
                    "delta": {axis: round(actual_total[axis], 6) for axis in _VAD_AXES},
                    "delta_rule": {axis: round(delta_total[axis], 6) for axis in _VAD_AXES},
                    "ts": _tz_now().isoformat(timespec="seconds"),  # PR2.5 D4：同上时区链
                }
            # ── 入账 + 旁路记账：同一同步区（其间无 await）────────────────────────
            # 旧实现：``await record_external_event(...)`` 之后才提交账本。该 await 会发布
            # ``emotion.updated`` 并让出事件循环，第二个协程因此能读到陈旧账本 → 同一事件
            # 重复入账 / 全局日封顶被击穿 / 账本读-改-写丢更新；若在 await 期间被取消，还会
            # 「VAD 已入账、账本未落」，重试即二次入账。同步核心不 yield，入账与记账在同一
            # 同步执行内完成，单线程事件循环下即原子。
            apply_sync = getattr(self._emotion, "record_external_event_sync", None)
            if callable(apply_sync):
                outcome = apply_sync(
                    LIFE_EPISODE_SOURCE, description, VADState(**delta_total),
                    multiplier=1.0, session_id="",
                    event_id=f"life.episode_{event_kind}", episode_id=episode_id)
                if not (isinstance(outcome, dict) and outcome.get("ok")):
                    # 未入账（停用/去重拒绝）→ 预算不动账
                    logger.debug("情绪-生活反馈：同步入账未生效（%s）",
                                 outcome.get("reason") if isinstance(outcome, dict) else outcome)
                    return
                # 记账用**真正应用**的增量（同步核心返回的 applied 优先；与预览同真源，
                # 其间无 await 故必然一致），并据此写 actual 口径标记
                applied_outcome = outcome.get("delta")
                commit_delta, commit_basis = actual_total, actual_basis
                if applied_outcome is not None:
                    commit_delta = _axis_dict(applied_outcome)
                    commit_basis = True
                if entry is not None:
                    # entry 的 delta 以真正应用值为准（与 used 同一口径，预览/应用同真源）
                    entry["delta"] = {axis: round(commit_delta[axis], 6) for axis in _VAD_AXES}
                self._commit_budget(budget, pending_rules, commit_delta, episode_id, entry,
                                    commit_basis)
                # 事件面保持不变：账本已提交后才发布（取消发生在发布位时账本已落定）
                publisher = getattr(self._emotion, "publish_external_updated", None)
                if callable(publisher):
                    await publisher()
            else:
                # 兜底：无同步核心的服务桩/旧服务（单协程桩无并发窗口）→ 既有异步入口
                result = await self._emotion.record_external_event(
                    source=LIFE_EPISODE_SOURCE, description=description,
                    delta=VADState(**delta_total), use_llm=False, multiplier=1.0,
                    session_id="", event_id=f"life.episode_{event_kind}",
                    episode_id=episode_id)
                if not (isinstance(result, dict) and result.get("ok")):
                    logger.debug("情绪-生活反馈：record_external_event 未入账（%s）",
                                 result.get("reason") if isinstance(result, dict) else result)
                    return
                self._commit_budget(budget, pending_rules, actual_total, episode_id, entry,
                                    actual_basis)
            logger.debug("情绪-生活反馈：episode %s 实际入账 %s（规则 %s，规则=%s）",
                         episode_id or "(无)", actual_total, delta_total, applied_rules)
        except Exception:
            logger.warning("情绪-生活反馈处理异常（忽略，不影响事件链）", exc_info=True)

    def _commit_budget(self, budget: Dict[str, Any], pending_rules: Dict[str, Dict[str, float]],
                       applied: Dict[str, float], episode_id: str,
                       entry: Optional[Dict[str, Any]], actual_basis: bool = True) -> None:
        """把一次**已真正入账**的反馈写进账本（entries 去重 / used 全局累计 / 逐规则累计）。

        R7 并发修复（2026-10-26）：只在 VAD 入账成功后的**同一同步区**调用（``_handle`` 内
        二者无 await 间隔），使「入账恰一次」与「预算不超、不丢更新」不可分割；未入账 / 失败
        / 入账前取消一律不进入本方法 → 不占预算。``entry`` 由 ``_handle`` 按本次判定预先成形
        （含 ``_tz_now`` 时区链的 ts），本方法只负责并入账本与落盘。

        口径（2026-10-27 用户裁定）：``budget["used"]`` 累计 ``applied``——即最终实际入账
        增量（倍率 + ±0.5 限幅之后），不是规则输入 delta；逐规则 ``rules`` 仍累计规则输入
        delta（``pending_rules``）。``actual_basis=False`` 仅出现在无预览口的旧服务/桩兜底
        路径：此时 applied 只是规则 delta 代理，**不写** ``used_basis`` 标记（防止把 raw
        当 actual 留给后续实例）。
        """
        budget["rules"].update(pending_rules)
        for axis in _VAD_AXES:
            budget["used"][axis] = _to_float(budget["used"].get(axis)) + applied[axis]
        if actual_basis:
            budget["used_basis"] = BUDGET_USED_BASIS
        if episode_id and entry is not None:
            budget["entries"][episode_id] = entry
        self._save_budget(budget)

    @staticmethod
    def _episode_duration_minutes(payload: Dict[str, Any]) -> Optional[float]:
        """episode 时长（分钟）；无可信端点/时长字段时返回 None。

        PR2.5 起（2026-10-26，交接 §五.18 D6）：优先读发布侧补的
        ``payload["duration_minutes"]``（interrupted 事件无 actual 端点也有时长，
        反馈条目即可对打断形态设时长门槛）；缺该字段回退 actual_start/end 环折算
        （completed 事件口径不变）。跨午夜按同一个 24h 环折算（一律走
        planning/time_utils 日帧助手，禁止手写取模）。
        """
        duration = payload.get("duration_minutes")
        if duration is not None:
            try:
                return float(duration)
            except (TypeError, ValueError):
                pass
        start_raw = payload.get("actual_start_minute")
        end_raw = payload.get("actual_end_minute")
        if start_raw is None or end_raw is None:
            return None
        try:
            from planning.time_utils import ring_minutes_ahead
            return float(ring_minutes_ahead(end_raw, start_raw))
        except Exception:
            return None

    # ── 每日预算（原子写；日期滚动自然重置；读写全程 best-effort）──
    def _load_budget(self, today: str) -> Dict[str, Any]:
        """读当日账本：**进程内镜像优先**，再回落到盘上文件；非当日 → 新账本（滚动重置）。

        镜像优先的理由（2026-10-26 并发修复配套）：写盘失败（``save_json_atomic`` 返回
        False）或无 ``_data_dir`` 的实例若每次都只看盘，重试同一事件会读到陈旧账本而重复
        入账；账本已在 ``_save_budget`` 里同步镜像，故当日口径以内存为准。
        """
        fresh: Dict[str, Any] = {"date": today, "used": {}, "rules": {}, "entries": {}}
        cache = self._budget_cache
        if isinstance(cache, dict) and str(cache.get("date") or "") == today:
            return self._mark_legacy_basis(copy.deepcopy(cache))
        if self._budget_path is None:
            return fresh
        data = load_json(self._budget_path, None)
        if not isinstance(data, dict) or str(data.get("date") or "") != today:
            return fresh  # 首日 / 旧日期账本 → 新账本（滚动重置）
        budget: Dict[str, Any] = {"date": today}
        budget["used"] = data.get("used") if isinstance(data.get("used"), dict) else {}
        budget["rules"] = data.get("rules") if isinstance(data.get("rules"), dict) else {}
        budget["entries"] = (data.get("entries")
                             if isinstance(data.get("entries"), dict) else {})
        budget["used_basis"] = str(data.get("used_basis") or "")
        if isinstance(data.get("budget_hold"), dict):
            budget["budget_hold"] = data["budget_hold"]
        self._mark_legacy_basis(budget)
        self._budget_cache = copy.deepcopy(budget)
        return budget

    @staticmethod
    def _legacy_budget_has_history(budget: Dict[str, Any]) -> bool:
        """旧 raw 账本是否存在**无法由净 used 恢复**的历史消耗（独立复核 2026-10-27）。

        只看 ``used`` 净额不够：旧账的正负事件可以互相抵消使 raw ``used`` 恰为 0，
        而历史 ``global_event_multiplier`` 变化 / 逐事件 ±0.5 截断后的 actual 可能非零。
        因此 ``rules`` 累计或 ``entries`` 逐条 delta 任一非零都视为有历史消耗。

        ``entries`` 内逐条 delta 全零（或结构缺 delta 时保守视为有消耗）才不算；
        只有 used/rules/entries 均空或全零（确实没有历史消耗）的旧账才可免 hold。
        """
        used = budget.get("used") if isinstance(budget.get("used"), dict) else {}
        if any(abs(_to_float(used.get(axis))) > _ZERO_EPS for axis in _VAD_AXES):
            return True
        rules = budget.get("rules") if isinstance(budget.get("rules"), dict) else {}
        for value in rules.values():
            axes = value if isinstance(value, dict) else {}
            if any(abs(_to_float(axes.get(axis))) > _ZERO_EPS for axis in _VAD_AXES):
                return True
        entries = budget.get("entries") if isinstance(budget.get("entries"), dict) else {}
        for entry in entries.values():
            delta = entry.get("delta") if isinstance(entry, dict) else None
            if not isinstance(delta, dict):
                # 结构不可判定（历史条目缺 delta）→ 保守视为有历史消耗
                return True
            if any(abs(_to_float(delta.get(axis))) > _ZERO_EPS for axis in _VAD_AXES):
                return True
        return False

    @staticmethod
    def _mark_legacy_basis(budget: Dict[str, Any]) -> Dict[str, Any]:
        """标记「当日非空旧 raw 账本」：缺 actual 口径标记且存在历史消耗，或已带 legacy hold。

        独立复核订正（2026-10-27）：不能只看 ``used`` 净额——正负事件抵消使 raw ``used``
        为 0，但 ``rules``/``entries`` 有历史消耗时 actual 仍不可恢复，必须按用户裁定
        hold；只有 used/rules/entries 均空或全零（确实没有历史消耗）才免 hold。
        """
        basis = str(budget.get("used_basis") or "")
        if basis == BUDGET_USED_BASIS_LEGACY:
            budget["legacy_basis"] = True
        elif basis != BUDGET_USED_BASIS and LifeFeedbackService._legacy_budget_has_history(budget):
            budget["legacy_basis"] = True
        return budget

    def _hold_legacy_budget(self, budget: Dict[str, Any]) -> None:
        """给同日旧 raw 账本补 basis/hold 留痕（2026-10-27 用户裁定）。

        只加元数据，``used`` / ``rules`` / ``entries`` 原样保留：不猜历史倍率、不回填未知
        actual、不清空旧账放大额度。幂等（已有 hold 则不重写）；hold 随日切自然消失。
        """
        raw_basis = str(budget.get("used_basis") or "") or "rule_delta_unmarked"
        budget["used_basis"] = BUDGET_USED_BASIS_LEGACY
        if not isinstance(budget.get("budget_hold"), dict):
            budget["budget_hold"] = {
                "since": _tz_now().isoformat(timespec="seconds"),
                "reason": "legacy_used_basis_unrecoverable",
                "legacy_basis": raw_basis,
                "note": ("当日旧账 used 为规则输入口径，actual 不可恢复；暂停当天新增生活"
                         "反馈，次日 actual 新口径自动恢复"),
            }
        persisted = {key: value for key, value in budget.items() if key != "legacy_basis"}
        self._save_budget(persisted)

    def _save_budget(self, budget: Dict[str, Any]) -> None:
        # 先更新进程内镜像（当日去重与累计不依赖写盘成功），再尽力原子写盘
        self._budget_cache = copy.deepcopy(budget)
        if self._budget_path is None:
            return
        if not save_json_atomic(self._budget_path, budget):
            logger.debug("情绪-生活反馈：每日预算写盘失败（已保留进程内镜像；仅影响跨重启口径）")
