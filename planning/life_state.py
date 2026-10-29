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
  **不阻塞**（B.2 原文）；重启冷启动：**延迟漂移**（2026-10-29 离线睡眠补全重构）——
  只解析 ``last_tick_at`` 算**原始离线 gap 秒数**（不经 24h 钳制）暂存为 pending 暴露给
  编排方，由其按 ``planning.offline_sleep`` 分级决定清醒漂移还是睡眠结算（防与
  ``SleepEngine.restore()`` 双重记账）；正常运行 tick 的 ``dt`` 仍钳 ``[0, DAY_MINUTES]``；
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
from .life_history import LifeStateHistory

logger = logging.getLogger(__name__)

# ── 常量（附录 B.2 / B.6.1 冻结数值）─────────────────────────────────────
#: 状态文件版本（结构变更时递增；读到不认识的版本 → 重置默认 + 告警）
STATE_VERSION = 1
#: 状态文件名（B.4.1：state_file 相对 plugin._data_dir）
STATE_FILE_NAME = "life_state.json"


def resolve_state_file_path(data_dir: Any, configured: Any = "") -> Path:
    """``state_file`` → 数据目录内的安全路径（plugin 装配与 InitService 共用）。

    相对路径落 ``<data_dir>/<name>``；绝对路径同样做包含校验。越界/解析失败 →
    ``<data_dir>/life_state.json`` 并告警（不写、不创建外部路径）。
    """
    root = Path(data_dir).resolve()
    name = str(configured or "").strip() or STATE_FILE_NAME
    candidate = Path(name)
    candidate = candidate if candidate.is_absolute() else root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = candidate
    if resolved != root and root not in resolved.parents:
        logger.warning("life_state.state_file 指向数据目录之外（%s），已回退 %s",
                       name, root / STATE_FILE_NAME)
        return root / STATE_FILE_NAME
    return resolved
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

# ── 稳定性护栏（PR2.5 修复（2026-10-26），定稿 §4.1.3「必须实现」/交接 §五.18 D2；
#    仅调制路径（modulation 非 None）生效，开关关/modulation=None 零影响）────────
#: homeostatic override：精力 < 此值 → energy 的 rise gain 抬到 ≥1.0、fall gain 压到 ≤0.5
HOMEOSTATIC_ENERGY_FLOOR = 0.2
#: homeostatic override：精力见底时 energy fall gain 的上限（下降减半，防情绪加速耗竭）
HOMEOSTATIC_ENERGY_FALL_MAX = 0.5
#: homeostatic override：饥饿 > 此值 → hunger 的 fall gain 抬到 ≥1.0（不因情绪抑制食欲）
HOMEOSTATIC_HUNGER_CEIL = 0.85
#: 每 tick 单状态变化量上限（定稿 §4.1.3「每 tick 状态 delta 上限（如 0.03）」）
TICK_DELTA_CAP = 0.03


class LifeStateService:
    """生活状态服务：tick 推进 + 事件应用 + 睡眠处理 + 持久化 + 描述生成（0 LLM）。

    ``state_path``：状态文件路径（或目录——传目录时取 ``<目录>/life_state.json``）；
    ``timezone_str`` / ``timezone_manager``：时区真源（缺省 Asia/Shanghai，经
    ``planning.time_utils.TimezoneManager``，不手写本地时间换算）；
    构造即装载（损坏/缺失重置默认 + 告警不阻塞）；冷启动**不**直接补算漂移——
    原始离线 gap 经 :attr:`pending_offline_gap_seconds` /
    :meth:`consume_pending_offline_gap` 暴露给编排方（离线睡眠补全，见
    :mod:`planning.offline_sleep`），未消费前不给任何状态施加油。
    """

    def __init__(self, state_path: Any, *, timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None,
                 decision_logger: Any = None) -> None:
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
        # ── 睡眠引擎字段（附录 B.2.1；PR3 2026-10-26 新增，旧文件缺键 → 默认）──
        # 与五项状态同一份文件原子写；睡眠会话历史在 sleep_sessions 表（B.2.2）
        self._sleep_started_at: Optional[datetime] = None
        self._continuous_sleep_minutes: float = 0.0
        self._sleep_interrupted: bool = False
        self._sleep_segment_started_at: Optional[datetime] = None
        self._last_wake_reason: Optional[str] = None
        self._sleep_wake_target: Optional[datetime] = None
        self._sleep_lie_in_cap: Optional[datetime] = None
        #: 未知「未来版本」标记（非 None = 读到不认识的 version）。此状态下
        #: **禁止覆盖用户文件**（2026-10-27 定稿口径：未知版本不猜着覆盖，先备份+告警）。
        self._future_version: Optional[str] = None
        #: 最近一次损坏/未知版本文件的备份路径（诊断用；只读）
        self.last_backup_path: Optional[Path] = None
        #: 损坏文件备份失败（原文件仍存在）→ 禁止 save 覆盖（内存降级，原字节保留）
        self._backup_failed: bool = False
        #: 冷启动待结算原始离线 gap（秒；2026-10-29 离线睡眠补全重构）。``None`` =
        #: 无待结算段（首启无 last_tick_at / 时钟回拨 / 损坏 / 未知版本——均不补全，
        #: 维持既有备份与重建语义）。分类与结算由编排方经
        #: ``planning.offline_sleep.plan_offline_settlement`` 用原始值做。
        self._pending_offline_gap_seconds: Optional[float] = None
        # A graceful stop is an observed-alive checkpoint, not a state integration tick.
        self._last_observed_at: Optional[datetime] = None
        self._pending_awake_before_offline_seconds: float = 0.0
        self._history = LifeStateHistory(path.with_name(f"{path.stem}_history.json"),
                                         decision_logger=decision_logger)
        self._load()
        self._capture_history("start", force=True)

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

    # ── 装载（损坏重置默认 + 告警不阻塞；冷启动离线 gap 延迟给编排方）────
    def _backup_corrupt_file(self, reason: str) -> Optional[Path]:
        """损坏/未知版本文件先**保留备份**再重建（定稿口径；不静默毁字节）。"""
        try:
            if not self._path.exists():
                return None
            stamp = self._now().strftime("%Y%m%d-%H%M%S")
            target = self._path.with_name(f"{self._path.name}.corrupt-{stamp}.bak")
            import shutil
            shutil.copy2(self._path, target)
            self.last_backup_path = target
            logger.warning("生活状态文件不可用（%s），已备份到 %s 后按规范默认重建",
                           reason, target)
            return target
        except Exception as exc:  # pragma: no cover - 备份失败不阻塞内存降级
            self._backup_failed = True
            logger.warning("生活状态文件备份失败（保留原文件、禁止覆盖写盘）：%s", exc)
            return None

    def _load(self) -> None:
        raw: Any = None
        corrupt_reason = ""
        try:
            text = self._path.read_text(encoding="utf-8")
            raw = json.loads(text)
        except FileNotFoundError:
            raw = None  # 首次运行：纯默认，不算故障
        except (OSError, ValueError) as exc:
            corrupt_reason = f"读取/解析失败：{exc}"
            raw = None
        if raw is not None and not isinstance(raw, dict):
            corrupt_reason = "顶层不是对象"
            raw = None
        if isinstance(raw, dict):
            version = raw.get("version")
            if version is not None and (not isinstance(version, int)
                                        or isinstance(version, bool)
                                        or version != STATE_VERSION):
                # 未知（含未来）版本：先备份，再按规范默认在内存重建；落盘由 save() 拒绝，
                # 避免用当前结构覆盖用户的新版本文件（真实歧义交诊断/报告）。
                self._future_version = str(version)
                corrupt_reason = f"version 不认识（期望 {STATE_VERSION}，实得 {version!r}）"
        if corrupt_reason:
            self._backup_corrupt_file(corrupt_reason)
        if raw is not None:
            self._apply_raw(raw)
        # 冷启动补算（2026-10-29 离线睡眠补全重构）：**延迟漂移**——本方法不再直接
        # 施加清醒漂移。原始离线 gap（秒，未经 _dt_minutes 的 24h 钳制）暂存为
        # pending 暴露给编排方（plugin 装配链在 SleepEngine.restore() 之前消费），
        # 由其按 planning.offline_sleep 分级决定清醒漂移还是睡眠结算，避免双重记账。
        # last_tick_at 仍推进到当前时刻：防止后续常规 tick 把同一段 gap 再按 24h
        # 钳制补算一遍；此处**不落盘**——文件保留旧 last_tick_at，进程在结算前崩溃
        # 重启时可重算同一段 gap（离线段不丢）。
        now = self._now()
        self._pending_offline_gap_seconds = self._raw_offline_gap_seconds(now)
        if self._last_observed_at is not None and self._last_tick_at is not None:
            if self._last_tick_at <= self._last_observed_at <= now:
                self._pending_awake_before_offline_seconds = (self._last_observed_at-self._last_tick_at).total_seconds()
                self._pending_offline_gap_seconds = (now-self._last_observed_at).total_seconds()
        # Clear only in memory; until settlement saves, a crash can still retry the original file.
        self._last_observed_at = None
        self._last_tick_at = now
        self._updated_at = now
        self._clamp_all()

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
        self._last_observed_at = self._parse_ts(raw.get("last_observed_at"))
        self._updated_at = self._parse_ts(raw.get("updated_at"))
        # 睡眠引擎字段（B.2.1）：逐字段容错，坏字段回默认、不整体作废
        self._sleep_started_at = self._parse_ts(raw.get("sleep_started_at"))
        continuous = raw.get("continuous_sleep_minutes")
        if isinstance(continuous, (int, float)) and not isinstance(continuous, bool):
            self._continuous_sleep_minutes = max(0.0, float(continuous))
        else:
            self._continuous_sleep_minutes = 0.0
        self._sleep_interrupted = bool(raw.get("sleep_interrupted", False))
        self._sleep_segment_started_at = self._parse_ts(raw.get("sleep_segment_started_at"))
        reason = raw.get("last_wake_reason")
        self._last_wake_reason = str(reason) if isinstance(reason, str) and reason else None
        self._sleep_wake_target = self._parse_ts(raw.get("sleep_wake_target"))
        self._sleep_lie_in_cap = self._parse_ts(raw.get("sleep_lie_in_cap"))

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

    def _raw_offline_gap_seconds(self, now: datetime) -> Optional[float]:
        """冷启动原始离线 gap（秒；**不经** :meth:`_dt_minutes` 的 24h 钳制）。

        仅 ``last_tick_at`` 存在且在过去时返回正秒数；无基准 / 时钟回拨
        （``last_tick_at`` 在未来）→ ``None``（不补全）。分类与结算用原始值
        （``planning.offline_sleep``），正常运行 tick 的钳制仍由 :meth:`_dt_minutes`
        负责，两口径互不干扰。
        """
        if self._last_tick_at is None:
            return None
        try:
            seconds = (now - self._last_tick_at).total_seconds()
        except TypeError:  # naive/aware 混撞（防御性；_aware 已归一，理论不可达）
            return None
        if seconds <= 0:
            return None  # 时钟回拨（last_tick_at 在未来）→ 不补全
        return seconds

    def _apply_drift(self, dt_min: float, rates: Optional[Dict[str, float]],
                     modulation: Optional[Dict[str, Dict[str, float]]] = None) -> None:
        """被动漂移 +（可选）活动画像速率积分（每分钟；B.6.1）。

        PR2（2026-10-26），计划书 §三十四/定稿 §4.1.1：``modulation`` 非 None 时，
        每个状态的**基础 delta**（被动漂移 + 画像速率）按符号乘 rise/fall gain
        （delta > 0 取 rise、< 0 取 fall；不乘当前状态值；缺项/坏值按 1.0 = 不调制）；
        ``None`` = 不调制，保持与 2.1.0/PR1 逐字一致（加法次序不变）。
        """
        if modulation is None:
            self._state["energy"] += DRIFT_ENERGY_PER_MIN * dt_min
            self._state["hunger"] += DRIFT_HUNGER_PER_MIN * dt_min
            self._state["focus"] += DRIFT_FOCUS_PER_MIN * dt_min
            self._state["stress"] += DRIFT_STRESS_PER_MIN * dt_min
            if rates:
                for key in STATE_FIELDS:
                    rate = rates.get(f"{key}_rate")
                    if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                        self._state[key] += float(rate) * dt_min
            return
        # 调制路径：先合成每状态基础 delta，再按符号乘 gain（状态 clamp 仍由
        # _clamp_all 统一做；gain = {state: {"rise": float, "fall": float}}，见
        # planning/life_modulation.LifeModulationService.compute）
        for key in STATE_FIELDS:
            spec = modulation.get(key) if isinstance(modulation.get(key), dict) else None
            gain_pair = spec or {}
            delta = 0.0
            if key == "energy":
                delta += DRIFT_ENERGY_PER_MIN * dt_min
            elif key == "hunger":
                delta += DRIFT_HUNGER_PER_MIN * dt_min
            elif key == "focus":
                delta += DRIFT_FOCUS_PER_MIN * dt_min
            elif key == "stress":
                delta += DRIFT_STRESS_PER_MIN * dt_min
            if rates:
                rate = rates.get(f"{key}_rate")
                if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                    delta += float(rate) * dt_min
            if delta > 0:
                gain = self._modulation_gain(gain_pair.get("rise"))
                # PR2.5 修复（2026-10-26），定稿 §4.1.3 homeostatic override：精力见底时
                # 上升方向不吃情绪抑制（rise gain 抬到 ≥1.0），保证能恢复
                if key == "energy" and self._state["energy"] < HOMEOSTATIC_ENERGY_FLOOR:
                    gain = max(gain, 1.0)
                delta *= gain
            elif delta < 0:
                gain = self._modulation_gain(gain_pair.get("fall"))
                if key == "energy" and self._state["energy"] < HOMEOSTATIC_ENERGY_FLOOR:
                    # PR2.5 修复（2026-10-26），定稿 §4.1.3：精力见底时下降压到 ≤0.5
                    # （耗竭减半，防情绪低落加速掏空）
                    gain = min(gain, HOMEOSTATIC_ENERGY_FALL_MAX)
                elif key == "hunger" and self._state["hunger"] > HOMEOSTATIC_HUNGER_CEIL:
                    # PR2.5 修复（2026-10-26），定稿 §4.1.3：极饿时食欲恢复不被情绪抑制
                    # （hunger 下降方向 gain 不低于 1.0）
                    gain = max(gain, 1.0)
                delta *= gain
            self._state[key] += delta

    def _cap_tick_delta(self, before: Dict[str, float]) -> None:
        """每 tick 单状态变化量上限（PR2.5 修复（2026-10-26），定稿 §4.1.3 /交接
        §五.18 D2 ②；仅调制路径调用——``modulation=None`` 时不裁，逐字旧行为）。

        对五项状态把本拍积分后的值夹回 ``before ± TICK_DELTA_CAP``（clamp 仍由
        :meth:`_clamp_all` 统一做在后）。
        """
        for key in STATE_FIELDS:
            delta = self._state[key] - before[key]
            if delta > TICK_DELTA_CAP:
                self._state[key] = before[key] + TICK_DELTA_CAP
            elif delta < -TICK_DELTA_CAP:
                self._state[key] = before[key] - TICK_DELTA_CAP

    @staticmethod
    def _modulation_gain(value: Any) -> float:
        """调制 gain 宽松解析（bool/NaN/缺省一律 1.0 = 该项不调制；PR2 定稿 §4.1.1）。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 1.0
        number = float(value)
        if number != number:  # NaN 防御
            return 1.0
        return number

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
                if key == "social_battery" and value is None:
                    # ActivityProfile's canonical field is social_rate, while the
                    # state axis remains social_battery. Explicit state aliases win.
                    value = source.get("social_rate") if isinstance(source, dict) else getattr(source, "social_rate", None)
                rates[f"{key}_rate"] = float(value) if value is not None else 0.0
            return rates
        except Exception as exc:
            logger.debug("活动画像速率解析失败（按纯被动漂移处理）：%s", exc)
            return None

    # ── 对外接口（B.5.1）───────────────────────────────────────────────────
    def _apply_sleep_rates(self, dt_min: float, sleep_rates: Dict[str, float]) -> None:
        """睡眠期积分（PR3，2026-10-26，定稿 §3.1 步 3）：只用引擎给的 ``sleep_rates``。

        睡眠期**不再叠加清醒被动漂移**、不乘 VAD 调制 gain、不裁单拍 delta
        （曲线本身已有界：精力/专注 |rate| ≤ 0.02/min）；未知/坏速率按 0。
        """
        for key in STATE_FIELDS:
            rate = sleep_rates.get(f"{key}_rate")
            if isinstance(rate, (int, float)) and not isinstance(rate, bool):
                number = float(rate)
                if number == number:  # NaN 防御
                    self._state[key] += number * dt_min

    def tick(self, now: Optional[datetime] = None,
             active_profile: Any = None,
             modulation: Optional[Dict[str, Dict[str, float]]] = None,
             sleep_rates: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        """唯一推进入口：被动漂移 + 当前活动画像速率积分，钳制后落盘。

        ``now`` 缺省取配置时区当前时间；``active_profile`` 支持
        :class:`~planning.activity_profiles.ActivityProfile` / 同构 dict /
        画像类型 id 字符串（str 经 ``activity_profiles.get_profile`` 解析）。
        ``modulation``（PR2，2026-10-26，定稿 §4.1.1；可选参数，缺省 None 逐字旧行为）：
        五项 rise/fall 增益（形态见 :meth:`planning.life_modulation.LifeModulationService.compute`），
        对每个状态的基础 delta 按符号相乘后走同一 clamp；None = 不调制。
        ``sleep_rates``（PR3，2026-10-26，定稿 §3.1 步 3；可选参数，缺省 None = 逐字旧
        行为）：睡眠引擎给的每分钟速率（``<state>_rate``），非 None 时**只**用它积分、
        不再叠加清醒被动漂移、不乘 ``modulation``、不裁 TICK_DELTA_CAP——连续曲线
        跨分段积分由引擎按区间平均值给出，本方法只做一次乘法，保证 tick 不重复恢复。
        返回 :meth:`snapshot`。落盘失败仅告警不阻塞（B.5.1「失败静默回退」）。
        """
        now = self._now(now)
        dt_min = self._dt_minutes(now)
        if sleep_rates is not None:
            if dt_min > 0:
                self._apply_sleep_rates(dt_min, sleep_rates)
            self._last_tick_at = now
            self._updated_at = now
            self._clamp_all()
            self.save()
            self._capture_history("sleep")
            return self.snapshot()
        # PR2.5 修复（2026-10-26），定稿 §4.1.3：仅调制路径记录本拍前值，供
        # _cap_tick_delta 做单拍变化量上限（modulation=None 零额外动作）
        before = dict(self._state) if modulation is not None else None
        if dt_min > 0:
            self._apply_drift(dt_min, rates=self._profile_rates(active_profile),
                              modulation=modulation)
        if before is not None:
            self._cap_tick_delta(before)
        self._last_tick_at = now
        self._updated_at = now
        self._clamp_all()
        self.save()
        self._capture_history("tick")
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
        self._capture_history("episode", force=True)
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
        self._capture_history("sleep_settlement", force=True)
        return self.snapshot()

    def apply_sleep_fields(self, fields: Any) -> Dict[str, Any]:
        """睡眠连续段字段落盘（PR3，2026-10-26，附录 B.2.1；每拍由引擎推入）。

        只接受白名单键，坏值按忽略/默认处理，**不覆盖五项状态**（精力/专注等由
        ``tick(sleep_rates=...)`` 积分，两处不交叉）。落盘 best-effort。
        """
        if not isinstance(fields, dict):
            return self.snapshot()
        if "sleep_started_at" in fields:
            self._sleep_started_at = self._parse_ts(fields.get("sleep_started_at"))
        if "continuous_sleep_minutes" in fields:
            value = fields.get("continuous_sleep_minutes")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self._continuous_sleep_minutes = max(0.0, float(value))
        if "sleep_interrupted" in fields:
            self._sleep_interrupted = bool(fields.get("sleep_interrupted"))
        if "sleep_segment_started_at" in fields:
            self._sleep_segment_started_at = self._parse_ts(
                fields.get("sleep_segment_started_at"))
        if "last_wake_reason" in fields:
            reason = fields.get("last_wake_reason")
            self._last_wake_reason = str(reason) if isinstance(reason, str) and reason else None
        if "sleep_wake_target" in fields:
            self._sleep_wake_target = self._parse_ts(fields.get("sleep_wake_target"))
        if "sleep_lie_in_cap" in fields:
            self._sleep_lie_in_cap = self._parse_ts(fields.get("sleep_lie_in_cap"))
        self.save()
        return self.snapshot()

    def apply_sleep_settlement(self, payload: Any) -> Dict[str, Any]:
        """**一次性**睡眠结算公共口（PR3，2026-10-26；引擎醒来时恰调一次）。

        与旧 :meth:`handle_sleep` 的区别（防双记账）：

        - ``handle_sleep`` = 旧三档整段覆盖（历史调用/兼容路径保留，逐字不变）；
        - 本方法 = 连续曲线已在睡眠期逐拍积分完成，这里**只**写会话结束事实：
          ``sleep_debt``（引擎按 B.2.2 公式算好，权威值）、``last_sleep_at``、
          ``sleep_started_at/continuous_sleep_minutes/sleep_segment_started_at/
          sleep_interrupted/last_wake_reason/sleep_wake_target/sleep_lie_in_cap``；
          **绝不覆盖 energy/focus**（否则「整段持续恢复 + 醒来三档覆盖」= 双记账）。
        """
        if not isinstance(payload, dict):
            return self.snapshot()
        now = payload.get("now")
        now_dt = self._now(now if isinstance(now, datetime) else None)
        debt = payload.get("sleep_debt")
        if isinstance(debt, (int, float)) and not isinstance(debt, bool):
            self._sleep_debt = max(0.0, min(SLEEP_DEBT_MAX, float(debt)))
        reason = payload.get("wake_reason")
        self._last_wake_reason = (str(reason) if isinstance(reason, str) and reason else None)
        self._continuous_sleep_minutes = 0.0
        self._sleep_started_at = None
        self._sleep_segment_started_at = None
        self._sleep_interrupted = False
        self._sleep_wake_target = None
        self._sleep_lie_in_cap = None
        self._last_sleep_at = now_dt
        self._last_tick_at = now_dt
        self._updated_at = now_dt
        self._clamp_all()
        self.save()
        self._capture_history("wake", force=True)
        return self.snapshot()

    def _capture_history(self, reason: str, *, force: bool = False) -> None:
        """Observations only; history failure never changes the life tick result."""
        if self._future_version is not None or self._backup_failed:
            return
        try:
            self._history.sample(self.snapshot(), reason=reason, force=force)
        except Exception as exc:
            logger.debug("生活状态历史采样失败（忽略）：%s", exc)

    def history_snapshot(self, *, now: Optional[datetime] = None,
                         hours: int = 24, limit: int = 600) -> Dict[str, Any]:
        """Read a detached, bounded observation window without ticking or writing."""
        return self._history.snapshot(now=self._now(now), hours=hours, limit=limit)

    def sleep_status(self) -> Dict[str, Any]:
        """只读睡眠字段块（B.2.1；emotion/面板/测试共用，不含五项状态）。"""
        return {
            "sleep_started_at": self._fmt_ts(self._sleep_started_at),
            "continuous_sleep_minutes": float(self._continuous_sleep_minutes),
            "sleep_interrupted": bool(self._sleep_interrupted),
            "sleep_segment_started_at": self._fmt_ts(self._sleep_segment_started_at),
            "sleep_debt": float(self._sleep_debt),
            "last_sleep_at": self._fmt_ts(self._last_sleep_at),
            "last_wake_reason": self._last_wake_reason,
            "sleep_wake_target": self._fmt_ts(self._sleep_wake_target),
            "sleep_lie_in_cap": self._fmt_ts(self._sleep_lie_in_cap),
        }

    # ── 冷启动离线 gap（离线睡眠补全 2026-10-29；编排方专用）───────────────
    @property
    def pending_offline_gap_seconds(self) -> Optional[float]:
        """冷启动原始离线 gap（秒，未钳制；``None`` = 无待结算段）。

        只读视图：编排方（plugin 装配链）在 ``SleepEngine.restore()`` 之前消费；
        分类/结算口径见 :mod:`planning.offline_sleep`。取走请走
        :meth:`consume_pending_offline_gap`（一次性，防重复结算）。
        """
        # Prefer an observed graceful-stop checkpoint; old files fall back to last tick.
        return self._pending_offline_gap_seconds

    def consume_pending_offline_gap(self) -> Optional[float]:
        """一次性取走冷启动 pending gap（取走即清；``None`` = 无待结算段）。

        编排方判定走睡眠结算或清醒漂移后调用本方法收口；未消费期间
        :meth:`tick` 的常规路径不会重复补算该 gap（``last_tick_at`` 已在装载时
        推进，见 :meth:`_load` 注释）。
        """
        gap = self._pending_offline_gap_seconds
        self._pending_offline_gap_seconds = None
        return gap

    def consume_pending_awake_before_offline_seconds(self) -> float:
        """Unsampled running time before a confirmed stop; consumed with the offline gap."""
        seconds = self._pending_awake_before_offline_seconds
        self._pending_awake_before_offline_seconds = 0.0
        return seconds

    def mark_observed_at(self, now: Optional[datetime] = None) -> None:
        """Record a graceful unload without dropping unintegrated time since the last tick."""
        self._last_observed_at = self._now(now)

    def mental_state(self) -> float:
        """派生读数 ``energy × focus × (1 - stress)``（不单独存，附录 B.2 说明）。"""
        value = self._state["energy"] * self._state["focus"] * (1.0 - self._state["stress"])
        return max(0.0, min(1.0, value))

    def snapshot(self) -> Dict[str, Any]:
        """只读状态 dict（浅拷贝；调用方不得修改——B.5.1 原文）。

        键：``version`` / 五项状态 / ``sleep_debt`` / ``mental_state``（派生）/
        ``last_meal_at`` / ``last_break_at`` / ``last_sleep_at`` / ``last_tick_at`` /
        ``updated_at``（时间戳为 ISO 文本，未发生为 None）；PR3 起追加附录 B.2.1
        睡眠字段（``sleep_started_at``/``continuous_sleep_minutes``/``sleep_interrupted``/
        ``sleep_segment_started_at``/``last_wake_reason``/``sleep_wake_target``/
        ``sleep_lie_in_cap``）。
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
            "sleep_started_at": self._fmt_ts(self._sleep_started_at),
            "continuous_sleep_minutes": float(self._continuous_sleep_minutes),
            "sleep_interrupted": bool(self._sleep_interrupted),
            "sleep_segment_started_at": self._fmt_ts(self._sleep_segment_started_at),
            "last_wake_reason": self._last_wake_reason,
            "sleep_wake_target": self._fmt_ts(self._sleep_wake_target),
            "sleep_lie_in_cap": self._fmt_ts(self._sleep_lie_in_cap),
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
    def _document(self) -> Dict[str, Any]:
        """规范状态文档（**键结构唯一真源**：``save()`` 与默认文档共用本方法）。"""
        return {
            "version": STATE_VERSION,
            **{key: float(self._state[key]) for key in STATE_FIELDS},
            "sleep_debt": float(self._sleep_debt),
            "last_meal_at": self._fmt_ts(self._last_meal_at),
            "last_break_at": self._fmt_ts(self._last_break_at),
            "last_sleep_at": self._fmt_ts(self._last_sleep_at),
            "last_tick_at": self._fmt_ts(self._last_tick_at),
            "last_observed_at": self._fmt_ts(self._last_observed_at),
            "updated_at": self._fmt_ts(self._updated_at),
            # PR3 睡眠引擎字段（附录 B.2.1；旧文件缺键 → 加载默认）
            "sleep_started_at": self._fmt_ts(self._sleep_started_at),
            "continuous_sleep_minutes": float(self._continuous_sleep_minutes),
            "sleep_interrupted": bool(self._sleep_interrupted),
            "sleep_segment_started_at": self._fmt_ts(self._sleep_segment_started_at),
            "last_wake_reason": self._last_wake_reason,
            "sleep_wake_target": self._fmt_ts(self._sleep_wake_target),
            "sleep_lie_in_cap": self._fmt_ts(self._sleep_lie_in_cap),
        }

    def save(self) -> bool:
        """原子写状态文件（B.2「原子写」原文）；失败仅告警返回 False。

        读到**未知版本**（未来版本）时拒绝写盘：不猜着用当前结构覆盖用户文件。
        """
        if self._future_version is not None:
            logger.warning("生活状态文件 version=%s 未知（本代码 %d），拒绝覆盖写盘：%s",
                           self._future_version, STATE_VERSION, self._path)
            return False
        if self._backup_failed:
            logger.warning("损坏状态文件备份失败，拒绝覆盖原文件（内存降级继续）：%s",
                           self._path)
            return False
        ok = save_json_atomic(self._path, self._document())
        if not ok:
            logger.warning("生活状态文件写入失败（忽略，内存状态继续生效）：%s", self._path)
        return ok


def default_state_document() -> Dict[str, Any]:
    """规范默认状态文档（新装 / 损坏重建的唯一真源；**不读盘不写盘**）。

    与 :meth:`LifeStateService.save` 共用 ``_document()``，因此字段集合不会像
    静态清单那样随 PR3 新增睡眠字段而漂移（工单要求：避免 19/20 字段口径漂移）。
    """
    service = LifeStateService.__new__(LifeStateService)
    service._state = dict(DEFAULT_STATE)
    service._sleep_debt = 0.0
    service._last_meal_at = None
    service._last_break_at = None
    service._last_sleep_at = None
    service._last_tick_at = None
    service._last_observed_at = None
    service._updated_at = None
    service._sleep_started_at = None
    service._continuous_sleep_minutes = 0.0
    service._sleep_interrupted = False
    service._sleep_segment_started_at = None
    service._last_wake_reason = None
    service._sleep_wake_target = None
    service._sleep_lie_in_cap = None
    return service._document()
