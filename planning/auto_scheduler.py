"""每日定时生成循环（来源：自主规划插件 planner/auto_scheduler.py，移植版）。

后台任务：每天到点且**目标日**当天无日程时自动生成并落库。
完全静默运行，不发送任何消息到聊天，只记录日志。

M10 第 6 段（附录 §五，裁决 B2/B4）：**到期时刻从「绝对时刻」改为「相对锚点」**。

- 新字段 ``[schedule] auto_generate_after_last_end_minutes``（分钟，``ge=0``）= *最后日程结束后
  多久生成新一天日程*；基准 = **当天最后一个非睡眠日程条目的结束时间**，当天没有日程时回退
  ``[schedule] sleep_time`` 锚点（B4："有日程用日程、无日程用锚点"）。
- 硬约束（B2）：**生成时刻 ≤ 新一天起点 − 30 分钟**（新一天起点 = ``[schedule] wake_time``
  锚点，与 L2 早安基准同一概念）；相对延迟算出的时刻更晚 → **强制拉回**该阈值。
- 旧字段 ``auto_generate_time``（绝对时刻）**deprecated、兼容读取**（2026-10-10 起住在
  ``[schedule_advanced]`` 隐藏位，判定经 :class:`TimingConfigView` 两段合并读取），
  且同样受 30 分钟硬约束钳制；
  两字段都被**显式配置**时新字段优先（判定见 :func:`resolve_generate_timing`，与 T7
  ``evaluate_mode``/``evaluate_on_reply`` 迁移同一 ``model_fields_set`` 惯例）。
- "非睡眠条目"的判定与读数**不在本模块另立口径**：走 ``planning/schedule_items.py``
  （M9 赖床工具同源，避免两处漂移）。

框架口径（当日帧）：帧起点 = **当日的 wake 锚点**，帧长 24h（相对分钟 ``0..1440``），
帧内 ``1440`` 即**新一天起点**（次日 wake 锚点）。当日帧的**目标日** = 帧起点 +1 天 ——
即"新一天"；在本帧内到期即生成该目标日的日程（跨午夜后仍在同一帧：如 07:00 起床时，
00:30 仍属前一天的帧）。

防抖/重入行为与既有实现一致：只换"到期时刻怎么算"，循环结构、单次失败退避、
"每日一次"防重（``_last_schedule_date`` 记**目标日**）全部保持原样。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.llm_utils import llm_task_name_of
from core.time_utils import parse_hhmm

from .config import (
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
    section_or_default,
)
from .schedule_items import last_non_sleep_end_minute

logger = logging.getLogger(__name__)

#: 硬约束（B2）：生成时刻距**新一天起点**的最小提前量（分钟）——"至少新一天开始前 30 分钟生成"
GENERATE_LEAD_MINUTES = 30
#: 一天的分钟数（当日帧相对分钟的回绕长度）
_DAY_MINUTES = 24 * 60
#: 缺省作息锚点（配置缺失/非法时兜底）：07:00 起床 / 23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60
#: 旧字段（绝对时刻）的兜底值：与字段默认值同源（非法输入回退它）
_DEFAULT_ABSOLUTE_TIME = "00:30"
#: 相对延迟的出厂值（resolve_generate_timing 的值判断口径）
_DEFAULT_DELAY_MINUTES = 90
#: 同上，分钟形态（``parse_hhmm`` 的兜底）
_DEFAULT_ABSOLUTE_MINUTES = 30
#: 生成时刻口径（``resolve_generate_timing`` 的返回值）
TIMING_RELATIVE = "relative"   # 相对：最后日程结束 + 延迟（新字段）
TIMING_ABSOLUTE = "absolute"   # 绝对：配置的时刻（旧字段，deprecated 兼容路径）


def _normalize_time_str(time_str: str, default: str) -> str:
    """规范化 HH:MM；非法输入回退默认值。"""
    try:
        hour_str, minute_str = str(time_str).split(":", 1)
        hour, minute = int(hour_str), int(minute_str)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except Exception:
        pass
    return default


def _as_minutes(value: Any, default: int) -> int:
    """非负整数分钟数（非法/负数一律回退 ``default``——``ge=0`` 的运行时兜底）。"""
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return int(default)
    return minutes if minutes >= 0 else int(default)


def in_frame_minutes(clock_minutes: int, wake_minutes: int) -> int:
    """钟点分钟数 → **当日帧相对分钟**：``(clock − wake) mod 1440``（帧起点 = 当日 wake 锚点）。

    早于帧起点的钟点回绕到**帧尾**——夜猫子作息（sleep ≤ wake）的入睡锚点、以及跨午夜
    条目落在凌晨的那一段（如 23:00 入睡 + 8h ⇒ 1380→1860，次日 00:00–07:00 段）都落在
    帧尾（2026-10-10 N2/N4 起条目跨午夜保留原时长，不再被截断到入睡锚点）。
    """
    return (int(clock_minutes) - int(wake_minutes)) % _DAY_MINUTES


def anchor_in_frame_minutes(anchor_minutes: int, wake_minutes: int) -> int:
    """**作息锚点**在当日帧内的相对分钟（B4 的兜底基准 = ``sleep_time`` 锚点）。

    常规作息（sleep > wake）：``sleep − wake``；夜猫子（sleep ≤ wake）：锚点落在**帧尾**
    （``sleep − wake + 1440``，与 M10-1 ``_next_daily_anchor_ts`` 的"+1 天"口径一致）；
    两者**相等**（睡眠窗口退化）时锚点即新一天起点，按帧尾 ``1440`` 处理 —— 任何延迟都会被
    30 分钟硬约束拉回，不会在"新一天开始的那一刻"才生成。
    """
    rel = int(anchor_minutes) - int(wake_minutes)
    return rel if rel > 0 else rel + _DAY_MINUTES


def clamp_due_minutes(due_minutes: int) -> int:
    """硬约束（B2）：帧内到期分钟数 ≤ ``1440 − 30``（= 新一天起点前 30 分钟）；超出强制拉回。"""
    return min(int(due_minutes), _DAY_MINUTES - GENERATE_LEAD_MINUTES)


def baseline_minutes(items: Optional[List[Dict[str, Any]]], *,
                     wake_minutes: int, sleep_minutes: int) -> int:
    """当日帧内的**基准**相对分钟数（B2/B4）。

    有日程 → 当天最后一个**非睡眠**条目的结束时间（判定口径走 ``planning/schedule_items.py``，
    与 M9 赖床工具同源）；无日程（或非睡眠条目都取不到结束时间）→ ``sleep_time`` 锚点（B4）。
    """
    end = last_non_sleep_end_minute(items)
    if end is None:
        return anchor_in_frame_minutes(sleep_minutes, wake_minutes)
    return in_frame_minutes(end, wake_minutes)


class TimingConfigView:
    """生成时刻判定视图：``[schedule]`` 的延迟字段 + ``[schedule_advanced]`` 的旧绝对时刻字段。

    2026-10-10 段拆分后 ``auto_generate_time`` 住在 ``[schedule_advanced]``（隐藏兼容位），
    而 ``auto_generate_after_last_end_minutes`` 仍在 ``[schedule]``——判定需要同时看到两段，
    故用本视图按"先基础段、后高级段"的属性查找合并（找不到的属性照旧抛 AttributeError）。
    """

    def __init__(self, schedule_cfg: Any, advanced_cfg: Any) -> None:
        object.__setattr__(self, "_sections", (schedule_cfg, advanced_cfg))

    def __getattr__(self, name: str) -> Any:
        for section in object.__getattribute__(self, "_sections"):
            if section is not None and hasattr(section, name):
                return getattr(section, name)
        raise AttributeError(name)


def resolve_generate_timing(cfg: Any) -> Tuple[str, Any]:
    """裁定生效的生成时刻口径（B2 + 旧字段兼容读取 + 优先级）。

    - 新字段 ``auto_generate_after_last_end_minutes`` 偏离缺省（90） → ``(TIMING_RELATIVE, 分钟数)``
      （**优先**：两字段都像"配过"时以新字段为准）；
    - 否则旧字段 ``auto_generate_time`` 偏离出厂值（00:30） → ``(TIMING_ABSOLUTE, "HH:MM")``
      （deprecated 兼容路径，按绝对时刻理解）；
    - 两者都是出厂值 → ``(TIMING_RELATIVE, 缺省 90)``。

    口径说明（2026-10-10）：宿主会把缺失键用默认值补齐后再交给插件，``model_fields_set``
    因此**不再能**区分"用户配过/没配过"；改为**值判断**——偏离出厂值即视为用户配置过。
    旧字段已隐藏进 ``[schedule_advanced]``（不占界面），只为兼容既有配置保留——
    因此生产调用点传的是 :class:`TimingConfigView`（两段合并视图），直调 ``[schedule]`` 单段时
    旧字段自然取不到（无兼容路径生效，行为 = 相对延迟）。
    """
    delay = _as_minutes(getattr(cfg, "auto_generate_after_last_end_minutes", None), 90)
    if delay != _DEFAULT_DELAY_MINUTES:
        return TIMING_RELATIVE, delay
    legacy_raw = str(getattr(cfg, "auto_generate_time", "") or "").strip()
    if legacy_raw and legacy_raw != _DEFAULT_ABSOLUTE_TIME:
        return TIMING_ABSOLUTE, _normalize_time_str(legacy_raw, _DEFAULT_ABSOLUTE_TIME)
    return TIMING_RELATIVE, delay


def schedule_day_start(now: datetime, wake_minutes: int) -> datetime:
    """当前所处的「日程日」起点 = 不晚于 ``now`` 的最近一次 **wake 锚点**（F16 新一天起点）。

    例：wake 07:00 时，00:30 仍属**前一天**的帧（帧跨越午夜），07:00 之后属当天。
    """
    start = now.replace(hour=int(wake_minutes) // 60, minute=int(wake_minutes) % 60,
                        second=0, microsecond=0)
    if start > now:
        start -= timedelta(days=1)
    return start


class ScheduleAutoScheduler:
    """日程自动调度器：在每天（相对锚点算出的）到期时刻自动生成新一天的日程。

    Args:
        plugin: 插件实例（读配置 / 取 generator / 取 ScheduleDB）
        generator_factory: 返回 ScheduleGenerator 的可调用（PlanningServices 注入；
            缺省回退用 plugin._schedule_gen）
    """

    def __init__(self, plugin: Any, generator_factory: Optional[Any] = None) -> None:
        self.plugin = plugin
        self._generator_factory = generator_factory
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        # 指数退避参数（单次失败不阻塞后续执行）
        self._retry_count = 0
        self._max_retry_wait = 300  # 最大等待 5 分钟
        # "每日一次"防重：记录**已生成/已处理的目标日**（= 新一天，非当前日历日）
        self._last_schedule_date: Optional[str] = None

    # ------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------

    async def start(self) -> None:
        """启动定时任务（幂等；未启用配置时只记日志）。"""
        if self.is_running:
            return
        cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
        if not cfg.auto_generate_enabled:
            logger.info("日程定时生成功能未启用")
            return
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("日程定时生成已启动 - %s", self._describe_timing(self._timing_view()))

    async def stop(self) -> None:
        """停止定时任务并等待退出。"""
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        logger.info("日程定时任务已停止")

    # ------------------------------------------------------------
    # 到期时刻（B2：按日期 + 相对锚点；全部可注入时钟直测）
    # ------------------------------------------------------------

    def _timing_view(self) -> TimingConfigView:
        """两段合并的生成时刻视图（延迟在 [schedule]、旧绝对时刻在 [schedule_advanced]）。"""
        return TimingConfigView(
            section_or_default(self.plugin, "schedule", ScheduleSectionConfig),
            section_or_default(self.plugin, "schedule_advanced", ScheduleAdvancedSectionConfig),
        )

    def _describe_timing(self, cfg: Any) -> str:
        """启动日志用的口径描述（不参与判定）。"""
        mode, value = resolve_generate_timing(cfg)
        if mode == TIMING_ABSOLUTE:
            return f"兼容读取绝对时刻 {value}（已废弃字段；仍受新一天起点前 {GENERATE_LEAD_MINUTES} 分钟硬约束）"
        return (f"最后日程结束后 {value} 分钟（无日程回退入睡锚点；"
                f"不晚于新一天起点前 {GENERATE_LEAD_MINUTES} 分钟）")

    def _anchors(self, cfg: Any) -> Tuple[int, int]:
        """``(wake_minutes, sleep_minutes)`` 作息锚点（F16；缺省/非法 07:00 / 23:00）。"""
        wake = parse_hhmm(str(getattr(cfg, "wake_time", "") or ""), _DEFAULT_WAKE_MINUTES)
        sleep = parse_hhmm(str(getattr(cfg, "sleep_time", "") or ""), _DEFAULT_SLEEP_MINUTES)
        return wake, sleep

    def _day_items(self, date_str: str) -> List[Dict[str, Any]]:
        """当日（帧）日程条目；ScheduleDB 不可得/读失败 → 空表（回退入睡锚点，B4）。"""
        db = getattr(self.plugin, "_db", None)
        if db is None:
            return []
        try:
            return list(db.get_day_schedule(date_str))
        except Exception as exc:
            logger.debug(f"读取当日日程失败（{date_str}）：{exc}")
            return []

    def _due_minutes_in_frame(self, frame_start: datetime, cfg: Any) -> int:
        """当日帧内的到期分钟数 = ``clamp(基准 + 延迟, ≤ 新一天起点 − 30 分钟)``。"""
        wake_min, sleep_min = self._anchors(cfg)
        mode, value = resolve_generate_timing(cfg)
        if mode == TIMING_ABSOLUTE:
            # 旧字段兼容路径：绝对时刻落进当日帧（同一钟点在本帧内的位置），同样受硬约束钳制
            return clamp_due_minutes(in_frame_minutes(parse_hhmm(value, _DEFAULT_ABSOLUTE_MINUTES),
                                                       wake_min))
        items = self._day_items(frame_start.strftime("%Y-%m-%d"))
        base = baseline_minutes(items, wake_minutes=wake_min, sleep_minutes=sleep_min)
        return clamp_due_minutes(base + int(value))

    def _due_for_frame(self, frame_start: datetime, cfg: Any) -> Tuple[str, datetime]:
        """某「日程日」帧对应的 ``(目标日, 到期时刻)``（**目标日** = 帧起点 +1 天 = 新一天）。"""
        target_date = (frame_start.date() + timedelta(days=1)).strftime("%Y-%m-%d")
        due_at = frame_start + timedelta(minutes=self._due_minutes_in_frame(frame_start, cfg))
        return target_date, due_at

    def _pending_generation(self, now: datetime, cfg: Any) -> Tuple[str, datetime]:
        """下一个**尚未生成**的目标日 ``T`` 与其到期时刻（已生成 → 顺延到下一帧）。

        顺延只可能发生在"本帧目标日刚生成过"的场景（每日至多一次），故两帧足够；
        到期时刻用**目标日的上一帧**（该帧的日程已存在）计算，与生产时序一致。
        """
        frame_start = schedule_day_start(now, self._anchors(cfg)[0])
        target_date, due_at = self._due_for_frame(frame_start, cfg)
        if self._last_schedule_date != target_date:
            return target_date, due_at
        return self._due_for_frame(frame_start + timedelta(days=1), cfg)

    def _is_time_due(self, now: datetime, target_date: str, due_at: datetime) -> bool:
        """到期判定（B2）：目标日**尚未生成**，且当前时刻已到/已过它的到期时刻。

        沿用既有"每日一次"语义：已过到期时刻且目标日尚未生成 → 到期（立即生成）；
        ``_last_schedule_date`` 已记该目标日 → 不再触发（跨日由 :meth:`_pending_generation`
        推进到下一帧，重新判定）。
        """
        return self._last_schedule_date != target_date and now >= due_at

    def _seconds_until_next(self, now: datetime, cfg: Any) -> float:
        """距下一个待生成目标日到期还有多少秒（已到期未生成 → 1s，交给到期判定立刻触发）。"""
        _target_date, due_at = self._pending_generation(now, cfg)
        return max(1.0, (due_at - now).total_seconds())

    # ------------------------------------------------------------
    # 定时循环
    # ------------------------------------------------------------

    async def _schedule_loop(self) -> None:
        """定时任务循环：计算下次执行时间并等待；单次失败不影响后续执行（结构不变）。"""
        while self.is_running:
            try:
                now = datetime.now()
                cfg = self._timing_view()
                target_date, due_at = self._pending_generation(now, cfg)

                if self._is_time_due(now, target_date, due_at):
                    self._last_schedule_date = target_date
                    if not bool(getattr(cfg, "enabled", True)):
                        logger.info("日程规划模块已停用（schedule.enabled=false），跳过 %s 的自动生成", target_date)
                        self._retry_count = 0
                        continue
                    await self._generate_today_schedule(target_date)
                    self._retry_count = 0
                    continue

                wait_seconds = max(15.0, self._seconds_until_next(now, cfg))
                await asyncio.sleep(wait_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定时任务出错: {e}", exc_info=True)
                # 指数退避重试（30s, 60s, 120s, 240s, 300s）
                self._retry_count += 1
                wait_time = min(30 * (2 ** (self._retry_count - 1)), self._max_retry_wait)
                logger.info(f"将在 {wait_time} 秒后重试（第 {self._retry_count} 次）")
                await asyncio.sleep(wait_time)

    def _target_date_for(self, now: Optional[datetime] = None) -> str:
        """缺省目标日：按 wake 锚点口径推出的"新一天"（``_generate_today_schedule`` 直调用）。"""
        now = now or datetime.now()
        cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
        frame_start = schedule_day_start(now, self._anchors(cfg)[0])
        return (frame_start.date() + timedelta(days=1)).strftime("%Y-%m-%d")

    async def _generate_today_schedule(self, date_str: Optional[str] = None) -> None:
        """生成指定日期（缺省 = 当前帧的**新一天**）的日程；该日已有日程则跳过；完全静默。"""
        try:
            target = date_str or self._target_date_for()
            logger.info(f"开始自动生成日程: {target}")

            # 目标日已有日程 → 跳过（plugin._db 即 ScheduleDB）
            db = getattr(self.plugin, "_db", None)
            if db is not None and db.has_day_schedule(target):
                count = len(db.get_day_schedule(target))
                logger.info(f"{target} 已有 {count} 个日程，跳过自动生成")
                return

            generator = self._resolve_generator()
            if generator is None:
                logger.warning("自动生成日程失败：无可用的 ScheduleGenerator")
                return

            cfg = section_or_default(self.plugin, "schedule", ScheduleSectionConfig)
            adv = section_or_default(self.plugin, "schedule_advanced", ScheduleAdvancedSectionConfig)
            personality = str(getattr(self.plugin, "_personality", "") or "")
            lover_name = str(getattr(self.plugin, "_lover_name", "") or "麦麦")

            items = await generator.generate_daily_schedule(
                target,
                personality=personality,
                lover_name=lover_name,
                wake_time=cfg.wake_time,
                sleep_time=cfg.sleep_time,
                min_activities=cfg.min_activities,
                max_activities=cfg.max_activities,
                timeout_seconds=adv.generation_timeout,
                use_multi_round=adv.use_multi_round,
                max_rounds=adv.max_rounds,
                quality_threshold=adv.quality_threshold,
                enable_detailed_description=cfg.enable_detailed_description,
                min_description_length=adv.min_description_length,
                max_description_length=adv.max_description_length,
                max_tokens=adv.max_tokens,
                custom_prompt=cfg.custom_prompt,
                recent_schedule_days=adv.recent_schedule_days,
                history_message_limit=adv.history_message_limit,
                knowledge_search_limit=adv.knowledge_search_limit,
                cross_day_activity=adv.cross_day_activity,
                no_sleep_mode=cfg.no_sleep_mode,
                timezone=adv.timezone,
                llm_task_name=llm_task_name_of(self.plugin),
            )
            logger.info("自动生成日程成功: %s - 共 %d 个活动", target, len(items))

        except Exception as e:
            logger.error(f"自动生成日程出错: {e}", exc_info=True)

    def _resolve_generator(self) -> Optional[Any]:
        """取生成器：优先工厂注入；回退 plugin._schedule_gen。"""
        if self._generator_factory is not None:
            try:
                return self._generator_factory()
            except Exception as exc:
                logger.warning(f"generator_factory 调用失败: {exc}")
        return getattr(self.plugin, "_schedule_gen", None)
