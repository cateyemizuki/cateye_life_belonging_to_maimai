"""生活状态层 LifeStateService（R7 依据 = 用户 2026-10-25 直派【日程系统滚动化改造，
设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，
附录 B.2 / B.5.1 / B.6.1】）：精力/饥饿/专注/压力/社交电量的连续状态 + 睡眠债，
独立于 VAD 情绪机（主设计 §6.1：情绪管「感觉好不好」，本模块管「有没有精力/饿不饿」，
两者经事件总线小幅耦合，不混在同一份状态里；本模块不碰 emotion/ 任何代码）。

状态文件 ``life_state.json``（附录 B.2，相对 ``plugin._data_dir``）::

    {"version": 1, "energy": 0.62, "hunger": 0.35, "focus": 0.72, "stress": 0.25,
     "social_battery": 0.55, "sleep_debt": 0.3,
     "last_meal_at": "...ISO...", "last_break_at": "...", "last_sleep_at": "...",
     "last_tick_at": "...", "updated_at": "..."}

- 全部 0~1（``sleep_debt`` 0~12h）；``mental_state`` **不单独存**——派生读数
  ``energy × focus × (1 - stress)``（:meth:`LifeStateService.mental_state` /
  snapshot 派生键），避免多个变量互相打架（附录 B.2 说明）；
- 原子写：``core.storage.save_json_atomic``；加载失败/损坏：重置默认值 + 告警日志，
  **不阻塞**（B.2 原文）；重启冷启动：从 ``last_tick_at`` 补算被动漂移，**最多补 24h**；
- 时区：一律经 ``planning.time_utils.TimezoneManager``（禁手写本地时间换算）；
  tick 的 ``dt`` 钳 ``[0, DAY_MINUTES]``（24h 常量取自 planning/time_utils，
  全仓唯一 24h 常量纪律），时钟回拨按 0；
- 变化规则（B.6.1）：被动漂移（清醒基准）+ 当前活动画像速率（每分钟，取自
  ``planning/activity_profiles`` 生效表）；全部 clamp；
- 睡眠三档（B.2 默认值表 + §6.3）：<5h / 5~7h / ≥7h，质量 ``quality``∈[0,1] 做小幅
  修调；睡眠事件重置 ``last_tick_at``（睡眠期间的被动漂移不重算）；
- 所有方法失败静默回退、不阻塞主循环（B.5.1 原文）；``tick()`` 是唯一推进入口，
  其他服务不要直接改状态；``snapshot()`` 返回只读 dict（浅拷贝），调用方不得修改。

纯离线零 LLM：本模块不调用任何 LLM 任务（红线 1）；不发布事件、不触碰主动消息链
（纪律 5：不新增任何主动开口入口）——事件发布（``life.*``）由接线阶段在 tick 消费侧做。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from core.storage import save_json_atomic

from .time_utils import DAY_MINUTES, TimezoneManager

logger = logging.getLogger(__name__)

# ── 常量（附录 B.2 / B.6.1 冻结数值）─────────────────────────────────────
#: 状态文件版本（结构变更时递增；读到不认识的版本 → 重置默认 + 告警）
STATE_VERSION = 1
#: 状态文件名（B.4.1：state_file 相对 plugin._data_dir）
STATE_FILE_NAME = "life_state.json"
#: 五项 0~1 状态字段
STATE_FIELDS: Tuple[str, ...] = ("energy", "hunger", "focus", "stress", "social_battery")
#: 新装/未知默认值（B.2 默认值表第一行）
DEFAULT_STATE: Dict[str, float] = {
    "energy": 0.65, "hunger": 0.35, "focus": 0.65,
    "stress": 0.25, "social_battery": 0.55,
}
#: 睡眠债上限（小时；B.2「sleep_debt 0–12h」）
SLEEP_DEBT_MAX = 12.0
#: 清醒被动漂移速率（每分钟；B.6.1）
DRIFT_ENERGY_PER_MIN = -0.00005
DRIFT_HUNGER_PER_MIN = 0.00035
DRIFT_FOCUS_PER_MIN = -0.00015
DRIFT_STRESS_PER_MIN = -0.00005
#: 单次 tick 最大推进量 = 24h（B.6.1「dt 钳 0~24*60」；常量取 time_utils 唯一真源）
DT_MAX_MINUTES = float(DAY_MINUTES)
#: 睡眠三档（B.2 默认值表「睡醒 7–8h」「睡醒 <5h」两行 + §6.3「sleep_debt +1.5h」；
#: 5~7h 档 B.2 无现成行，取两档之间偏保守的固定中值——裁量登记于模块落码 notes）
SLEEP_TIERS: Dict[str, Dict[str, float]] = {
    # <5h：低恢复 + 睡眠债 +1.5h
    "short": {"energy": 0.42, "hunger": 0.40, "focus": 0.50,
              "stress": 0.35, "social_battery": 0.50, "debt_add": 1.5, "debt_reset": 0.0},
    # 5~7h：两档之间的保守中值 + 睡眠债小幅 +0.5h
    "mid": {"energy": 0.62, "hunger": 0.37, "focus": 0.64,
            "stress": 0.28, "social_battery": 0.55, "debt_add": 0.5, "debt_reset": 0.0},
    # >=7h：充分恢复 + 睡眠债清零（B.2「睡醒 7–8h」行 sleep_debt=0.0）
    "full": {"energy": 0.80, "hunger": 0.35, "focus": 0.75,
             "stress": 0.20, "social_battery": 0.60, "debt_add": 0.0, "debt_reset": 1.0},
}
#: 睡眠质量修调幅度（quality∈[0,1]，0.5 = 无修调；保守小系数，避免盖过档位语义）
QUALITY_ENERGY_SPAN = 0.10   # energy ±0.05
QUALITY_FOCUS_SPAN = 0.10    # focus ±0.05


class LifeStateService:
    """生活状态服务：tick 推进 + 事件应用 + 睡眠处理 + 持久化 + 描述生成（0 LLM）。

    ``state_path``：状态文件路径（或目录——传目录时取 ``<目录>/life_state.json``）；
    ``timezone_str`` / ``timezone_manager``：时区真源（缺省 Asia/Shanghai，经
    ``planning.time_utils.TimezoneManager``，不手写本地时间换算）；
    构造即装载（损坏/缺失重置默认 + 告警不阻塞），装载后按 ``last_tick_at`` 补算
    被动漂移（最多 24h）。
    """

    def __init__(self, state_path: Any, *, timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None) -> None:
        path = Path(state_path)
        if path.is_dir():
            path = path / STATE_FILE_NAME
        self._path: Path = path
        self._tz = timezone_manager or TimezoneManager(timezone_str)
        #: tzinfo 直取（TimezoneManager 内部字段；仅本模块使用，避免每判一次再查）
        self._tzinfo = getattr(self._tz, "_tz", None)
        #: 五项 0~1 状态（内存真源；``sleep_debt`` 单独存 0~12h）
        self._state: Dict[str, float] = dict(DEFAULT_STATE)
        self._sleep_debt: float = 0.0
        #: 时间戳（aware datetime 或 None）
        self._last_meal_at: Optional[datetime] = None
        self._last_break_at: Optional[datetime] = None
        self._last_sleep_at: Optional[datetime] = None
        self._last_tick_at: Optional[datetime] = None
        self._updated_at: Optional[datetime] = None
        self._load()

    # ── 时间助手（时区唯一真源 = TimezoneManager；禁手写换算）─────────────
    def _now(self, now: Optional[datetime] = None) -> datetime:
        dt = now or self._tz.get_now()
        return self._aware(dt)

    def _aware(self, dt: datetime) -> datetime:
        """naive datetime 补 tzinfo（aware 原样；跨 tz 相减由 datetime 处理）。

        时区真源降级链与 ``TimezoneManager`` 一致：zoneinfo/pytz 不可用时退系统本地
        时区（``datetime.now().astimezone().tzinfo``——系统时区对象的取用，非手写
        本地时间换算）；保证落盘 ISO 文本恒带偏移、与历史 aware 戳可相减。
        """
        if dt.tzinfo is None:
            tz = self._tzinfo
            if tz is None:
                tz = datetime.now().astimezone().tzinfo
                self._tzinfo = tz
            return dt.replace(tzinfo=tz)
        return dt

    def _parse_ts(self, raw: Any) -> Optional[datetime]:
        """ISO 文本 → aware datetime；非法/缺失 → None（可空降级，不抛异常）。"""
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            dt = datetime.fromisoformat(raw.strip())
        except ValueError:
            return None
        return self._aware(dt)

    @staticmethod
    def _fmt_ts(dt: Optional[datetime]) -> Optional[str]:
        return dt.isoformat() if dt is not None else None

    # ── 装载（损坏重置默认 + 告警不阻塞；从 last_tick_at 补算最多 24h）────
    def _load(self) -> None:
        raw: Any = None
        try:
            text = self._path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except FileNotFoundError:
            raw = None  # 首次运行：纯默认，不算故障
        except (OSError, ValueError) as exc:
            logger.warning("生活状态文件读取失败，重置默认（不阻塞）：%s（%s）",
                           self._path, exc)
            raw = None
        if raw is not None:
            self._apply_raw(raw)
        # 冷启动补算：从 last_tick_at 补被动漂移，最多 24h；时钟回拨按 0
        now = self._now()
        dt_min = self._dt_minutes(now)
        if dt_min > 0:
            self._apply_drift(dt_min, rates=None)
        self._last_tick_at = now
        self._updated_at = now
        self._clamp_all()
        if dt_min > 0:
            # 补算落盘 best-effort：失败只告警，下次 tick 会再写
            self.save()

    def _apply_raw(self, raw: Any) -> None:
        """已解析文档 → 内存状态（字段级容错：坏字段单独回退默认，不整体作废）。"""
        if not isinstance(raw, dict):
            logger.warning("生活状态文件结构非法（顶层非对象），重置默认（不阻塞）")
            return
        version = raw.get("version")
        if version is not None and (not isinstance(version, int)
                                    or isinstance(version, bool)
                                    or version != STATE_VERSION):
            logger.warning("生活状态文件 version 不认识（期望 %d，实得 %r），重置默认"
                           "（不阻塞）", STATE_VERSION, version)
            return
        for key in STATE_FIELDS:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._state[key] = float(value)
            else:
                logger.warning("生活状态字段 %s 非法（%r），回退默认 %.2f",
                               key, value, DEFAULT_STATE[key])
                self._state[key] = DEFAULT_STATE[key]
        debt = raw.get("sleep_debt")
        if isinstance(debt, (int, float)) and not isinstance(debt, bool):
            self._sleep_debt = float(debt)
        else:
            self._sleep_debt = 0.0
        self._last_meal_at = self._parse_ts(raw.get("last_meal_at"))
        self._last_break_at = self._parse_ts(raw.get("last_break_at"))
        self._last_sleep_at = self._parse_ts(raw.get("last_sleep_at"))
        self._last_tick_at = self._parse_ts(raw.get("last_tick_at"))
        self._updated_at = self._parse_ts(raw.get("updated_at"))

    # ── 推进与钳制 ─────────────────────────────────────────────────────────
    def _dt_minutes(self, now: datetime) -> float:
        """距 ``last_tick_at`` 的经过分钟：钳 [0, 24h]；无基准/时钟回拨 → 0（B.6.1）。"""
        if self._last_tick_at is None:
            return 0.0
        try:
            seconds = (now - self._last_tick_at).total_seconds()
        except TypeError:  # naive/aware 混撞（防御性；_aware 已归一，理论不可达）
            return 0.0
        if seconds <= 0:
            return 0.0  # 时钟回拨按 0
        return min(seconds / 60.0, DT_MAX_MINUTES)

    def _apply_drift(self, dt_min: float, rates: Optional[Dict[str, float]]) -> None:
        """被动漂移 +（可选）活动画像速率积分（每分钟；B.6.1）。"""
        self._state["energy"] += DRIFT_ENERGY_PER_MIN * dt_min
        self._state["hunger"] += DRIFT_HUNGER_PER_MIN * dt_min
        self._state["focus"] += DRIFT_FOCUS_PER_MIN * dt_min
        self._state["stress"] += DRIFT_STRESS_PER_MIN * dt_min
        if rates:
            for key in STATE_FIELDS:
                rate = rates.get(f"{key}_rate")
                if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                    self._state[key] += float(rate) * dt_min

    def _clamp_all(self) -> None:
        """全部 0~1 clamp；睡眠债 clamp [0, 12]（B.6.1「所有值 clamp」原文）。"""
        for key in STATE_FIELDS:
            self._state[key] = max(0.0, min(1.0, float(self._state[key])))
        self._sleep_debt = max(0.0, min(SLEEP_DEBT_MAX, float(self._sleep_debt)))

    @staticmethod
    def _profile_rates(active_profile: Any) -> Optional[Dict[str, float]]:
        """当前活动画像 → 五项速率 dict；解析失败 → None（只走被动漂移，不阻塞）。"""
        if active_profile is None:
            return None
        rates: Dict[str, float] = {}
        try:
            if isinstance(active_profile, str):
                # 类型 id → 生效画像表取画像（缺类型回退 daily_routine 由表侧兜底）
                from . import activity_profiles  # 延迟导入：保持本模块可独立使用
                profile = activity_profiles.get_profile(active_profile)
                source: Any = profile
            else:
                source = active_profile
            for key in STATE_FIELDS:
                value = getattr(source, f"{key}_rate", None)
                if value is None and isinstance(source, dict):
                    value = source.get(f"{key}_rate")
                rates[f"{key}_rate"] = float(value) if value is not None else 0.0
            return rates
        except Exception as exc:
            logger.debug("活动画像速率解析失败（按纯被动漂移处理）：%s", exc)
            return None

    # ── 对外接口（B.5.1）───────────────────────────────────────────────────
    def tick(self, now: Optional[datetime] = None,
             active_profile: Any = None) -> Dict[str, Any]:
        """唯一推进入口：被动漂移 + 当前活动画像速率积分，钳制后落盘。

        ``now`` 缺省取配置时区当前时间；``active_profile`` 支持
        :class:`~planning.activity_profiles.ActivityProfile` / 同构 dict /
        画像类型 id 字符串（str 经 ``activity_profiles.get_profile`` 解析）。
        返回 :meth:`snapshot`。落盘失败仅告警不阻塞（B.5.1「失败静默回退」）。
        """
        now = self._now(now)
        dt_min = self._dt_minutes(now)
        if dt_min > 0:
            self._apply_drift(dt_min, rates=self._profile_rates(active_profile))
        self._last_tick_at = now
        self._updated_at = now
        self._clamp_all()
        self.save()
        return self.snapshot()

    def apply_episode(self, episode: Any, now: Optional[datetime] = None) -> Dict[str, Any]:
        """应用 episode 事件（吃饭/休息时间戳 + 可选显式状态增量；不重复积分时长）。

        - ``episode`` 为 dict；``profile``（或 ``goal_type``）命中 ``meal`` 且
          ``status``/``event`` 为完成态（completed/ended/done/finish）→ 盖
          ``last_meal_at``；命中 ``rest`` → 盖 ``last_break_at``（时长内的状态变化已由
          :meth:`tick` 按画像速率积分，这里不重复算，避免双重记账）；
        - ``state_deltas``（可选 dict）：对五项字段做显式加法增量后统一 clamp
          （随机小事件的状态影响走这里，附录 A.7）；
        - 非 dict / 解析失败 → 原样返回 snapshot（不抛异常、不阻塞）。
        """
        now = self._now(now)
        if isinstance(episode, dict):
            ptype = str(episode.get("profile") or episode.get("goal_type") or "").strip().lower()
            status = str(episode.get("status") or episode.get("event") or "completed").strip().lower()
            finished = status in ("completed", "ended", "done", "finish", "finished")
            if finished and ptype == "meal":
                self._last_meal_at = now
            elif finished and ptype == "rest":
                self._last_break_at = now
            deltas = episode.get("state_deltas")
            if isinstance(deltas, dict):
                for key in STATE_FIELDS:
                    value = deltas.get(key)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        self._state[key] += float(value)
        self._updated_at = now
        self._clamp_all()
        self.save()
        return self.snapshot()

    def handle_sleep(self, hours: Any, quality: Any = 0.5,
                     now: Optional[datetime] = None) -> Dict[str, Any]:
        """睡眠事件：按时长三档 + 质量小幅修调重置状态（B.2 默认值表 / §6.3）。

        - ``hours``：睡眠时长（小时）；非数值或 <=0 → 不做任何变更原样返回；
          ``<5`` → short 档（睡眠债 +1.5h）；``<7`` → mid 档（+0.5h）；
          ``>=7`` → full 档（睡眠债清零）；
        - ``quality``：睡眠质量 [0,1]（0.5 = 无修调）；越界钳入 [0,1]；
          energy/focus 按 ``(quality - 0.5) × 0.05`` 幅度修调；
        - 睡眠期间不做被动漂移：``last_tick_at`` 直接盖为 ``now``；
        - ``last_sleep_at`` 盖为 ``now``；落盘 best-effort；返回 :meth:`snapshot`。
        """
        now = self._now(now)
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) \
                or not float(hours) > 0:
            logger.warning("handle_sleep 收到非法时长 %r，忽略本次睡眠事件", hours)
            return self.snapshot()
        if not isinstance(quality, (int, float)) or isinstance(quality, bool) \
                or quality != quality:  # NaN 防御
            quality = 0.5
        quality = max(0.0, min(1.0, float(quality)))
        h = float(hours)
        tier = SLEEP_TIERS["short"] if h < 5.0 else (SLEEP_TIERS["mid"] if h < 7.0
                                                     else SLEEP_TIERS["full"])
        for key in STATE_FIELDS:
            self._state[key] = tier[key]
        self._state["energy"] += (quality - 0.5) * QUALITY_ENERGY_SPAN
        self._state["focus"] += (quality - 0.5) * QUALITY_FOCUS_SPAN
        if tier["debt_reset"]:
            self._sleep_debt = 0.0
        else:
            self._sleep_debt += tier["debt_add"]
        self._last_sleep_at = now
        self._last_tick_at = now
        self._updated_at = now
        self._clamp_all()
        self.save()
        return self.snapshot()

    def mental_state(self) -> float:
        """派生读数 ``energy × focus × (1 - stress)``（不单独存，附录 B.2 说明）。"""
        value = self._state["energy"] * self._state["focus"] * (1.0 - self._state["stress"])
        return max(0.0, min(1.0, value))

    def snapshot(self) -> Dict[str, Any]:
        """只读状态 dict（浅拷贝；调用方不得修改——B.5.1 原文）。

        键：``version`` / 五项状态 / ``sleep_debt`` / ``mental_state``（派生）/
        ``last_meal_at`` / ``last_break_at`` / ``last_sleep_at`` / ``last_tick_at`` /
        ``updated_at``（时间戳为 ISO 文本，未发生为 None）。
        """
        return {
            "version": STATE_VERSION,
            **{key: float(self._state[key]) for key in STATE_FIELDS},
            "sleep_debt": float(self._sleep_debt),
            "mental_state": self.mental_state(),
            "last_meal_at": self._fmt_ts(self._last_meal_at),
            "last_break_at": self._fmt_ts(self._last_break_at),
            "last_sleep_at": self._fmt_ts(self._last_sleep_at),
            "last_tick_at": self._fmt_ts(self._last_tick_at),
            "updated_at": self._fmt_ts(self._updated_at),
        }

    # ── 描述（最显著 1~2 项；零 emoji）─────────────────────────────────────
    def describe(self, now: Optional[datetime] = None, max_items: int = 2) -> str:
        """取最显著的状态项拼一行（附录 B.2/§6.4 口径，如
        「状态：有点累（精力 0.38），有点饿（饥饿 0.72）」；无显著项 →「状态：平稳」）。

        ``max_items`` 钳 [1, 3]；显著性 = 偏离中性带的距离（越偏越靠前）。
        """
        max_items = max(1, min(3, int(max_items))) if isinstance(max_items, (int, float)) \
            and not isinstance(max_items, bool) else 2
        s = self._state
        items: list = []  # (severity, text)

        def add(sev: float, text: str) -> None:
            if sev > 0:
                items.append((sev, text))

        energy, hunger = s["energy"], s["hunger"]
        focus, stress, social = s["focus"], s["stress"], s["social_battery"]
        if energy < 0.15:
            add(0.40 + (0.15 - energy), f"快撑不住（精力 {energy:.2f}）")
        elif energy < 0.40:
            add(0.40 - energy, f"有点累（精力 {energy:.2f}）")
        elif energy >= 0.85:
            add(energy - 0.85, f"精力充沛（精力 {energy:.2f}）")
        elif energy >= 0.72:
            add(energy - 0.72, f"精神不错（精力 {energy:.2f}）")
        if hunger >= 0.85:
            add(0.10 + (hunger - 0.85), f"很饿（饥饿 {hunger:.2f}）")
        elif hunger >= 0.60:
            add(hunger - 0.60, f"有点饿（饥饿 {hunger:.2f}）")
        if focus < 0.30:
            add(0.25 + (0.30 - focus), f"注意力涣散（专注 {focus:.2f}）")
        elif focus < 0.50:
            add(0.50 - focus, f"注意力一般（专注 {focus:.2f}）")
        elif focus >= 0.80:
            add(focus - 0.80, f"注意力集中（专注 {focus:.2f}）")
        if stress >= 0.80:
            add(0.10 + (stress - 0.80), f"压力很大（压力 {stress:.2f}）")
        elif stress >= 0.60:
            add(stress - 0.60, f"有点紧张（压力 {stress:.2f}）")
        if social < 0.20:
            add(0.20 - social, f"不太想说话（社交电量 {social:.2f}）")
        if self._sleep_debt >= 6.0:
            add(self._sleep_debt / SLEEP_DEBT_MAX + 0.10,
                f"缺觉明显（睡眠欠账 {self._sleep_debt:.1f}h）")
        elif self._sleep_debt >= 3.0:
            add(self._sleep_debt / SLEEP_DEBT_MAX,
                f"有点缺觉（睡眠欠账 {self._sleep_debt:.1f}h）")
        if not items:
            return "状态：平稳"
        items.sort(key=lambda pair: pair[0], reverse=True)
        return "状态：" + "，".join(text for _, text in items[:max_items])

    # ── 持久化（core.storage.save_json_atomic；失败告警不阻塞）────────────
    def save(self) -> bool:
        """原子写状态文件（B.2「原子写」原文）；失败仅告警返回 False。"""
        doc = {
            "version": STATE_VERSION,
            **{key: float(self._state[key]) for key in STATE_FIELDS},
            "sleep_debt": float(self._sleep_debt),
            "last_meal_at": self._fmt_ts(self._last_meal_at),
            "last_break_at": self._fmt_ts(self._last_break_at),
            "last_sleep_at": self._fmt_ts(self._last_sleep_at),
            "last_tick_at": self._fmt_ts(self._last_tick_at),
            "updated_at": self._fmt_ts(self._updated_at),
        }
        ok = save_json_atomic(self._path, doc)
        if not ok:
            logger.warning("生活状态文件写入失败（忽略，内存状态继续生效）：%s", self._path)
        return ok
