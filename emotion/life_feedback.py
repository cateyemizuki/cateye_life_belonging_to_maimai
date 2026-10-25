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
- 同一 ``episode_id`` 只入账一次（entries 去重；无 episode_id 的事件无从去重，照常入账）；
- 开关（``[life_state].enabled`` / ``[life_state].emotion_modulation_enabled`` /
  情绪模块总开关）任一关 → 零订阅零可观测；关闭态退订清理由 EmotionServices.stop() 统一
  执行（本模块订阅闭包已并入服务束 ``_event_unsubs``）；
- 映射表只在装载期读一遍（热重载 = 宿主重导入重跑 on_load 自动重读）：表层缺失 →
  ``ensure_user_table`` 按序补链（旧根迁移优先，仍缺才从深层种子复制）；解析错误 /
  校验不过 → 内置默认（``BUILTIN_TABLE``），**绝不读取或覆盖表层文件内容**（红线同
  ``core/mapping_dir.ensure_user_table``）；root_dir 缺失（独立测试桩）→ 直接内置默认。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


def _to_float(value: Any, default: float = 0.0) -> float:
    """防御性转 float（非法输入回退默认；与 EmotionService._to_float 同语义的模块内版本）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
            section = getattr(getattr(self._plugin, "config", None), "life_state", None)
            if section is None or not bool(getattr(section, "enabled", False)):
                return False
            if not bool(getattr(section, "emotion_modulation_enabled", False)):
                return False
        except Exception:
            return False
        try:
            return bool(self._emotion._enabled())
        except Exception:
            return False

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
            today = datetime.now().strftime("%Y-%m-%d")
            budget = self._load_budget(today)
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
            result = await self._emotion.record_external_event(
                source=LIFE_EPISODE_SOURCE, description=description,
                delta=VADState(**delta_total), use_llm=False, multiplier=1.0,
                session_id="", event_id=f"life.episode_{event_kind}",
                episode_id=episode_id)
            if not (isinstance(result, dict) and result.get("ok")):
                # 未入账（停用/去重/限幅拒绝）→ 预算不动账
                logger.debug("情绪-生活反馈：record_external_event 未入账（%s）",
                             result.get("reason") if isinstance(result, dict) else result)
                return
            budget["rules"].update(pending_rules)
            for axis in _VAD_AXES:
                budget["used"][axis] = _to_float(budget["used"].get(axis)) + delta_total[axis]
            if episode_id:
                budget["entries"][episode_id] = {
                    "rules": applied_rules,
                    "delta": {axis: round(delta_total[axis], 6) for axis in _VAD_AXES},
                    "ts": datetime.now().isoformat(timespec="seconds"),
                }
            self._save_budget(budget)
            logger.debug("情绪-生活反馈：episode %s 入账 %s（规则=%s）",
                         episode_id or "(无)", delta_total, applied_rules)
        except Exception:
            logger.warning("情绪-生活反馈处理异常（忽略，不影响事件链）", exc_info=True)

    @staticmethod
    def _episode_duration_minutes(payload: Dict[str, Any]) -> Optional[float]:
        """completed 事件的 episode 时长（分钟）；payload 无起止端时返回 None。

        跨午夜按同一个 24h 环折算（一律走 planning/time_utils 日帧助手，禁止手写取模）。
        """
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
        fresh: Dict[str, Any] = {"date": today, "used": {}, "rules": {}, "entries": {}}
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
        return budget

    def _save_budget(self, budget: Dict[str, Any]) -> None:
        if self._budget_path is None:
            return
        if not save_json_atomic(self._budget_path, budget):
            logger.debug("情绪-生活反馈：每日预算写盘失败（忽略；仅影响跨重启累计口径）")
