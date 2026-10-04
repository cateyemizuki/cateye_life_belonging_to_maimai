"""日程生成器（来源：自主规划插件 planner/schedule_generator.py，演进版）。

在原骨架（单轮 LLM + 模板兜底）基础上补齐源插件 v4.9.1 的完整生成管线：
- 多轮生成 + 质量评分（ScheduleQualityScorer）+ 达标提前结束 + 按反馈重试；
- prompt_builder 按 wake/sleep 锚点动态生成 JSON 示例与衔接演算；
- 语义校验（三餐 / 作息 / 时间连续性）+ 无睡眠模式后处理（销毁睡眠时段日程）；
- 近 N 天日程摘要防重复、跨会话历史消息 / 知识库上下文（需 plugin 引用）；
- 未来约定（pending_commitment）与日程项的最接近时间窗匹配（apply_schedule_items）。

兼容契约（plugin.py 等既有调用方依赖，只增不改）：
- 构造 ``ScheduleGenerator(db, llm)``；
- ``generate_daily_schedule(date, *, personality, lover_name, wake_time, sleep_time,
  min_activities, max_activities, timeout_seconds) -> list[dict]``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.llm_utils import LLMService
from core.time_utils import parse_hhmm
from .exceptions import (
    LLMError,
    LLMInvalidResponseError,
    ScheduleGenerationError,
)
from .generator import (
    BaseScheduleGenerator,
    LLMResponseParser,
    ScheduleGeneratorConfig,
    ScheduleQualityScorer,
    LOW_MOOD_MIN_DAYS,
    ScheduleSemanticValidator,
    build_emotion_context,
    clip_emotional_tone,
)
from .goal_manager import GoalManager, get_goal_manager
from .schedule_db import ScheduleDB

logger = logging.getLogger(__name__)

# 作息解析默认值（配置留空/非法时兜底）：07:00 起床，23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60

# 降级模板（LLM 全轮失败时按工作日/周末取用；time_slot 起始，时长小时）
FALLBACK_TEMPLATE = {
    "workday": [
        ("起床洗漱", "07:00", 0.5, "daily_routine", "medium", "闹钟响了，新的一天开始了"),
        ("吃早餐", "07:30", 0.5, "meal", "medium", "简单吃点东西"),
        ("工作/学习", "08:00", 3.0, "work", "high", "专注处理今天的主要任务"),
        ("午休", "12:00", 1.5, "rest", "medium", "吃午饭，稍微休息一下"),
        ("工作/学习", "13:30", 3.5, "work", "high", "下午继续推进任务"),
        ("运动放松", "17:30", 1.0, "exercise", "low", "活动活动身体"),
        ("吃晚餐", "18:30", 1.0, "meal", "medium", "晚饭时间"),
        ("娱乐时间", "19:30", 2.0, "entertainment", "low", "看看喜欢的视频或书"),
        ("洗漱准备", "21:30", 0.5, "daily_routine", "medium", "洗漱，准备睡觉"),
        ("睡觉", "22:00", 9.0, "sleep", "high", "晚安，明天见"),
    ],
    "weekend": [
        ("自然醒", "08:30", 0.5, "daily_routine", "low", "周末不用早起，赖一会儿床"),
        ("吃早餐", "09:00", 0.5, "meal", "medium", "悠闲地吃个早饭"),
        ("自由安排", "09:30", 2.5, "entertainment", "low", "做点自己喜欢的事"),
        ("吃午餐", "12:00", 1.0, "meal", "medium", "午饭时间"),
        ("午休", "13:00", 1.0, "rest", "medium", "小睡一会儿"),
        ("出门走走", "14:00", 2.5, "entertainment", "low", "出去透透气"),
        ("吃晚餐", "17:30", 1.0, "meal", "medium", "晚饭时间"),
        ("娱乐时间", "19:00", 2.5, "entertainment", "low", "刷刷视频，聊聊天"),
        ("洗漱准备", "21:30", 0.5, "daily_routine", "medium", "洗漱，准备睡觉"),
        ("睡觉", "22:00", 9.0, "sleep", "high", "晚安，明天见"),
    ],
}


def _parse_minutes(value: Any, default: int) -> int:
    """把 HH:MM 解析为当天分钟数；非法/留空返回 default（可为 -1 哨兵）。"""
    try:
        parts = str(value or "").strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour * 60 + minute
    except (ValueError, IndexError, TypeError):
        pass
    return default


def _fmt_minutes(minutes: int) -> str:
    """分钟数 → HH:MM（自动回绕 24 小时）。"""
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


class ScheduleGenerator:
    """日程生成器（协调器：配置 → 上下文 → 多轮生成 → 校验评分 → 落库）。"""

    #: M5 连续低迷软约束（§6.2 F）：恢复类 goal_type（已有则不再强制插入）
    RECOVERY_GOAL_TYPES = ("rest", "social_maintenance")
    #: 无缝日程切分时的优先条目类型（高强度/可压缩；不切 meal/daily_routine）
    RECOVERY_SPLIT_GOAL_TYPES = ("study", "learn_topic", "custom", "entertainment", "exercise")

    def __init__(
        self,
        db: ScheduleDB,
        llm: LLMService,
        plugin: Optional[Any] = None,
        goal_manager: Optional[GoalManager] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Args:
            db: 日程数据库
            llm: LLM 服务（core.llm_utils.LLMService，带任务路由与调用日志）
            plugin: 插件实例（可选；注入后启用跨会话历史 / 知识库上下文，
                    并从 plugin._personality / plugin._lover_name 兜底人设）
            goal_manager: 目标管理器（可选；缺省用全局单例）
            config: 默认配置字典（可选；每次 generate 调用还会用入参覆盖）
        """
        self._db = db
        self._llm = llm
        self._plugin = plugin
        self._goal_manager = goal_manager
        self._default_config: Dict[str, Any] = dict(config or {})

    @property
    def db(self) -> ScheduleDB:
        return self._db

    @property
    def goal_manager(self) -> GoalManager:
        """惰性获取 GoalManager（未显式注入时走全局单例）。"""
        if self._goal_manager is None:
            self._goal_manager = get_goal_manager()
        return self._goal_manager

    # ============================================================
    # 公开 API（兼容契约 + 扩展）
    # ============================================================

    async def generate_daily_schedule(
        self,
        date: str,
        *,
        personality: str = "",
        lover_name: str = "麦麦",
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
        min_activities: int = 6,
        max_activities: int = 12,
        timeout_seconds: float = 120.0,
        use_multi_round: Optional[bool] = None,
        max_rounds: Optional[int] = None,
        quality_threshold: Optional[float] = None,
        enable_detailed_description: bool = True,
        min_description_length: int = 20,
        max_description_length: int = 50,
        max_tokens: int = 8192,
        custom_prompt: str = "",
        recent_schedule_days: int = 3,
        history_message_limit: int = 0,
        knowledge_search_limit: int = 0,
        cross_day_activity: bool = True,
        no_sleep_mode: bool = False,
        timezone: str = "Asia/Shanghai",
        llm_task_name: str = "",
        temperature: float = 0.85,
        bot_profile: Optional[Dict[str, str]] = None,
        apply: bool = True,
    ) -> List[Dict[str, Any]]:
        """生成并落库某天日程；多轮生成 + 质量评分，LLM 全轮失败时模板兜底。

        兼容契约：前 9 个关键字参数与既有调用方一致；其余为 v4.9.1 扩展（带默认值）。

        Args:
            date: 目标日期（``YYYY-MM-DD``；提示词日期取当天，日程落库按此键）。
            apply: True=落库并做约定匹配/消费；False=仅生成（仍写入日程表以便查询）。
        """
        gen_config = ScheduleGeneratorConfig(self._build_config_dict(
            personality=personality, lover_name=lover_name, wake_time=wake_time, sleep_time=sleep_time,
            min_activities=min_activities, max_activities=max_activities, timeout_seconds=timeout_seconds,
            use_multi_round=use_multi_round, max_rounds=max_rounds, quality_threshold=quality_threshold,
            enable_detailed_description=enable_detailed_description,
            min_description_length=min_description_length, max_description_length=max_description_length,
            max_tokens=max_tokens, custom_prompt=custom_prompt, recent_schedule_days=recent_schedule_days,
            history_message_limit=history_message_limit, knowledge_search_limit=knowledge_search_limit,
            cross_day_activity=cross_day_activity, no_sleep_mode=no_sleep_mode, timezone=timezone,
            llm_task_name=llm_task_name, temperature=temperature, bot_profile=bot_profile,
        ))

        # 每次调用装配一套子组件（配置可变，无跨调用状态）
        base = BaseScheduleGenerator(self.goal_manager, gen_config.to_dict())
        validator = ScheduleSemanticValidator()
        scorer = ScheduleQualityScorer(gen_config.to_dict())

        # 上下文加载：近几天摘要 / 今日约定 / 跨会话历史 / 知识库 / 睡醒情绪 / 连续低迷（全部失败静默）
        self._load_sync_contexts(base, gen_config, date)
        self._load_emotion_context(base)
        self._load_low_mood(base)
        await self._load_async_contexts(base, gen_config, date)

        # 多轮 / 单轮生成
        use_multi = gen_config.use_multi_round if use_multi_round is None else bool(use_multi_round)
        try:
            if use_multi:
                raw_items = await self._generate_with_multi_round(base, validator, scorer, gen_config)
            else:
                raw_items = await self._generate_single_round(base, validator, gen_config)
        except ScheduleGenerationError as exc:
            logger.warning("日程 LLM 生成失败（%s），使用模板兜底", exc)
            raw_items = self._build_fallback()
        except LLMError as exc:
            logger.warning("日程 LLM 致命错误（%s），使用模板兜底", exc)
            raw_items = self._build_fallback()

        # 归一化为 DB 记录并落库（含约定匹配 + 消费）
        items = self._normalize_items(raw_items, date, wake_time, sleep_time)
        # M5 连续低迷软约束（§6.2 F）：强制插入一次 rest/social（原本没有恢复类活动时）
        items, inserted = self._ensure_recovery_item(
            items, date=date, low_mood=base.low_mood_days >= LOW_MOOD_MIN_DAYS,
            wake_time=wake_time, sleep_time=sleep_time)
        if inserted:
            logger.info("连续低迷软约束：已为 %s 强制插入一次恢复类活动（rest/social）", date)
        if apply:
            self.apply_schedule_items(items, date, consume_commitments=True)
        else:
            self._db.replace_day_schedule(date, items)
        logger.info("日程生成完成：%s 共 %d 项", date, len(items))
        # M0 事件：日程生成成功（自动生成/重生成统一走本方法）→ emotion/qzone（附录 A ★）
        await self._publish("schedule.generated", date=date, items=len(items))
        return items

    # ============================================================
    # 事件总线（M0；无总线时静默跳过）
    # ============================================================
    async def _publish(self, event: str, **payload: Any) -> None:
        bus = getattr(self._plugin, "_bus", None)
        if bus is None:
            return
        try:
            await bus.publish(event, source="planning", **payload)
        except Exception:
            logger.debug("事件发布失败：%s", event, exc_info=True)

    async def regenerate_today_schedule(
        self,
        date: Optional[str] = None,
        *,
        extra_prompt: str = "",
        personality: str = "",
        lover_name: str = "麦麦",
        apply: bool = True,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """重生成某天（默认今天）日程：先清空已有日程，再走标准生成管线。

        ``extra_prompt`` 会临时并入 custom_prompt（本实现按调用装配配置，无需还原）。
        """
        date = date or datetime.now().strftime("%Y-%m-%d")
        removed = self._db.delete_day_schedule(date)
        if removed:
            logger.info("已清理 %s 的 %d 个旧日程，开始重新生成", date, removed)

        base_custom = str(kwargs.pop("custom_prompt", "") or "")
        merged = f"{base_custom}\n\n{extra_prompt}".strip() if (base_custom or extra_prompt) else ""
        return await self.generate_daily_schedule(
            date, personality=personality, lover_name=lover_name,
            custom_prompt=merged, apply=apply, **kwargs,
        )

    def apply_schedule_items(
        self,
        items: List[Dict[str, Any]],
        date: str,
        *,
        consume_commitments: bool = True,
    ) -> int:
        """把日程条目落库（start/end 分钟已换算 + 约定匹配注入）。

        步骤：
        1. ``_match_commitments_to_items``：把当日 pending_commitments 一对一映射到
           条目（时间差 ≤30 分钟优先，其次标题子串），注入 ``is_commitment`` 元数据
           （ProactiveService 据此用强指令兑现约定）；
        2. ``replace_day_schedule`` 整体写入；
        3. 成功后消费（标记 COMPLETED）当日约定。

        Returns:
            写入条数。
        """
        commitment_meta: Dict[int, Dict[str, Any]] = {}
        try:
            commitment_meta = self._match_commitments_to_items(items, date)
        except Exception as exc:
            logger.warning("约定→日程项 匹配失败（不影响落库）: %s", exc, exc_info=True)

        for idx, meta in commitment_meta.items():
            if idx < len(items):
                params = dict(items[idx].get("parameters") or {})
                params.update(meta)
                items[idx]["parameters"] = params

        written = self._db.replace_day_schedule(date, items)

        if consume_commitments and written:
            try:
                # 匹配到日程项的约定 = 兑现（kept），未匹配 = 违约（broken）——随消费事件上报（M0）
                matched_titles = {
                    str(meta.get("commitment_title") or "")
                    for meta in commitment_meta.values() if meta.get("commitment_title")
                }
                consumed = self.goal_manager.consume_pending_commitments(
                    date, matched_titles=matched_titles)
                if consumed:
                    logger.info("已消费 %d 条 %s 的未来约定", len(consumed), date)
            except Exception as exc:
                logger.warning("消费未来约定失败: %s", exc, exc_info=True)
        return written

    # ============================================================
    # 配置装配
    # ============================================================

    def _build_config_dict(self, **kwargs: Any) -> Dict[str, Any]:
        """合并默认配置与调用入参，产出 ScheduleGeneratorConfig 所需字典。"""
        cfg = dict(self._default_config)
        personality = str(kwargs.get("personality") or "").strip()
        lover_name = str(kwargs.get("lover_name") or "").strip()
        profile = dict(kwargs.get("bot_profile") or {})
        if personality:
            profile.setdefault("personality", personality)
        if lover_name:
            profile.setdefault("bot_name", lover_name)
        if not profile.get("personality") and self._plugin is not None:
            profile.setdefault("personality", str(getattr(self._plugin, "_personality", "") or ""))
        if not profile.get("bot_name") and self._plugin is not None:
            profile.setdefault("bot_name", str(getattr(self._plugin, "_lover_name", "") or ""))

        mapping = {
            "wake_time": "wake_time", "sleep_time": "sleep_time",
            "min_activities": "min_activities", "max_activities": "max_activities",
            "timeout_seconds": "generation_timeout", "use_multi_round": "use_multi_round",
            "max_rounds": "max_rounds", "quality_threshold": "quality_threshold",
            "enable_detailed_description": "enable_detailed_description",
            "min_description_length": "min_description_length",
            "max_description_length": "max_description_length", "max_tokens": "max_tokens",
            "custom_prompt": "custom_prompt", "recent_schedule_days": "recent_schedule_days",
            "history_message_limit": "history_message_limit",
            "knowledge_search_limit": "knowledge_search_limit",
            "cross_day_activity": "cross_day_activity", "no_sleep_mode": "no_sleep_mode",
            "timezone": "timezone", "temperature": "temperature",
        }
        for src_key, dst_key in mapping.items():
            value = kwargs.get(src_key)
            if value is not None and value != "":
                cfg[dst_key] = value
        cfg["bot_profile"] = profile
        task_name = str(kwargs.get("llm_task_name") or "").strip()
        if task_name:
            cfg["llm_task_name"] = task_name
        # 白名单：无 plugin 引用时历史上下文自然不可用；有则用 admin 段（若挂载）
        allowed: List[str] = []
        if self._plugin is not None:
            admin = getattr(self._plugin.config, "admin", None)
            if admin is not None:
                allowed = list(getattr(admin, "allowed_streams", None) or [])
        cfg["allowed_streams"] = allowed
        return cfg

    @staticmethod
    def _raw_of(gen_config: ScheduleGeneratorConfig) -> Dict[str, Any]:
        return getattr(gen_config, "_raw_config", {}) or {}

    def _load_sync_contexts(
        self, base: BaseScheduleGenerator, gen_config: ScheduleGeneratorConfig, date: str
    ) -> None:
        """同步上下文：近几天日程摘要 + 今日约定（失败静默）。"""
        try:
            recent_days = int(self._raw_of(gen_config).get("recent_schedule_days", 3))
            base.yesterday_schedule_summary = base.context_loader.load_recent_schedule_summary(days=recent_days)
        except Exception as exc:
            logger.debug("近几天日程摘要加载失败: %s", exc)
        try:
            pending = self.goal_manager.get_pending_commitments(date)
            base.pending_commitments = [
                {
                    "time": (p.parameters or {}).get("time", ""),
                    "title": p.name,
                    "notes": (p.parameters or {}).get("notes", ""),
                }
                for p in pending
            ] or None
            if pending:
                logger.info("今日有 %d 条未来约定将被纳入日程生成", len(pending))
        except Exception as exc:
            logger.debug("今日约定加载失败: %s", exc)

    def _emotion_influence(self) -> float:
        """``schedule.emotion_influence``（0~1，默认 0.6；0=关闭），运行期活取值。"""
        cfg = getattr(getattr(self._plugin, "config", None), "schedule", None)
        value = getattr(cfg, "emotion_influence", 0.6)
        try:
            influence = float(value)
        except (TypeError, ValueError):
            influence = 0.6
        return max(0.0, min(1.0, influence))

    def _load_emotion_context(self, base: BaseScheduleGenerator) -> None:
        """M5（§6.2 F）：按 ``schedule.emotion_influence`` 把「睡醒情绪描述 + 引导约束」装载进槽位。

        睡醒情绪优先取 planning 缓存的 ``emotion.overhauled``（PlanningServices 订阅，含
        低分辨率三档词 ``levels``）；尚未醒来过（无事件）时退化为实时 ``mood_text()``
        （仅描述 + 通用引导）。``emotion_influence=0`` / 无情绪 / 模块缺失 → 空串（不注入，
        等价关闭，与旧 prompt 逐字一致）。全程静默降级（§八.2）。
        """
        try:
            influence = self._emotion_influence()
            if influence <= 0:
                base.emotion_context = ""
                return
            wake: Dict[str, Any] = {}
            planning = getattr(self._plugin, "_planning", None)
            getter = getattr(planning, "wake_emotion", None)
            if callable(getter):
                wake = getter() or {}
            mood = str(wake.get("mood") or "").strip()
            levels = wake.get("levels") if isinstance(wake.get("levels"), dict) else None
            if not mood:
                emotion = getattr(self._plugin, "_emotion", None)
                mood_getter = getattr(emotion, "mood_text", None)
                if callable(mood_getter):
                    mood = str(mood_getter() or "").strip()
                    levels = None
            base.emotion_context = build_emotion_context(mood, levels, influence)
        except Exception as exc:
            base.emotion_context = ""
            logger.debug("睡醒情绪上下文装载失败（忽略）: %s", exc)

    def _load_low_mood(self, base: BaseScheduleGenerator) -> None:
        """M5（§6.2 F）：装载连续低迷天数（优先情绪服务实时状态，回退 planning 的事件缓存）。

        情绪侧 ``low_mood_state()`` 未满门槛/模块缺失 → 0（不触发软约束，可空降级）。
        """
        try:
            state: Dict[str, Any] = {}
            emotion = getattr(self._plugin, "_emotion", None)
            getter = getattr(emotion, "low_mood_state", None)
            if callable(getter):
                state = getter() or {}
            if not state:
                planning = getattr(self._plugin, "_planning", None)
                getter = getattr(planning, "low_mood_state", None)
                if callable(getter):
                    state = getter() or {}
            base.low_mood_days = int((state or {}).get("days") or 0)
        except Exception as exc:
            base.low_mood_days = 0
            logger.debug("连续低迷状态装载失败（忽略）: %s", exc)

    @classmethod
    def _ensure_recovery_item(cls, items: List[Dict[str, Any]], *, date: str, low_mood: bool,
                              wake_time: str = "07:00", sleep_time: str = "23:00",
                              force_minutes: int = 60) -> tuple:
        """M5 连续低迷软约束（§6.2 F）：确保日程里至少有一段 rest/social，缺失则强制插入一次。

        插入策略（不重叠、不越作息锚点）：
        1. 存在 ≥``force_minutes`` 的空档 → 在最大空档起点插入。空档**含首尾**
           （[wake, 首条目)、条目之间、(末条目, sleep]），避免"活动集中在中间、
           前后全空"的日程被误判为"无空档"；
        2. 无缝日程（生产常态）→ 切出最长的高强度条目（study/learn_topic/custom/entertainment/
           exercise）的尾段作为恢复段；找不到合适条目则放弃（记日志，不破坏日程）。

        Returns:
            ``(items, inserted)``。
        """
        rows = sorted((dict(item) for item in (items or [])),
                      key=lambda it: int(it.get("start_minute") or 0))
        if not low_mood or not rows:
            return rows, False
        if any(str(it.get("goal_type") or "") in cls.RECOVERY_GOAL_TYPES for it in rows):
            return rows, False
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)
        # 1) 最大空档：**含首尾**——[wake, 首条目)、条目之间、(末条目, sleep]
        #    （只统计条目之间会漏掉"活动集中在中间、前后全空"的日程）
        gaps: List[tuple] = []
        cursor = int(wake_min)
        for it in rows:
            start = max(int(it.get("start_minute") or 0), cursor)
            if start - cursor >= force_minutes:
                gaps.append((cursor, start - cursor))
            cursor = max(cursor, int(it.get("end_minute") or 0))
        if int(sleep_min) - cursor >= force_minutes:
            gaps.append((cursor, int(sleep_min) - cursor))
        if gaps:
            at, span = max(gaps, key=lambda gap: gap[1])
            rows.append(cls._make_recovery_item(at, min(force_minutes, span), date))
            return sorted(rows, key=lambda it: int(it.get("start_minute") or 0)), True
        # 2) 无缝日程：切最长的高强度条目尾段
        def _duration(it: Dict[str, Any]) -> int:
            return int(it.get("end_minute") or 0) - int(it.get("start_minute") or 0)

        candidates = [it for it in rows
                      if str(it.get("goal_type") or "") in cls.RECOVERY_SPLIT_GOAL_TYPES
                      and _duration(it) >= force_minutes + 15]
        if not candidates:
            candidates = [it for it in rows if _duration(it) >= force_minutes + 15
                          and str(it.get("goal_type") or "") not in ("meal", "daily_routine")]
        if not candidates:
            logger.info("连续低迷软约束：无合适条目可切分，跳过强制插入（%s）", date)
            return rows, False
        target = max(candidates, key=_duration)
        cut = int(target.get("end_minute") or 0) - force_minutes
        target["end_minute"] = cut
        rows.append(cls._make_recovery_item(cut, force_minutes, date))
        return sorted(rows, key=lambda it: int(it.get("start_minute") or 0)), True

    @staticmethod
    def _make_recovery_item(start_minute: int, minutes: int, date: str) -> Dict[str, Any]:
        """构造一条强制插入的恢复类条目（rest）。"""
        start = max(0, int(start_minute))
        return {"name": "放空休息", "description": "留一段恢复时间，缓一缓（最近有点低落）",
                "goal_type": "rest", "priority": "medium",
                "start_minute": start, "end_minute": start + int(minutes),
                "parameters": {}, "schedule_date": date}

    async def _load_async_contexts(
        self, base: BaseScheduleGenerator, gen_config: ScheduleGeneratorConfig, date: str
    ) -> None:
        """异步上下文：跨会话历史 + 知识库（需要 plugin 引用，失败静默）。"""
        if self._plugin is None:
            return
        raw = self._raw_of(gen_config)
        try:
            base.history_context = await base.context_loader.load_recent_history_across_streams(
                self._plugin, raw.get("allowed_streams", []),
                int(raw.get("history_message_limit", 0) or 0),
            )
        except Exception as exc:
            logger.debug(f"跨群历史加载失败: {exc}")
            base.history_context = ""
        try:
            base.knowledge_context = await base.context_loader.load_relevant_knowledge(
                self._plugin, query_hint=date, limit=int(raw.get("knowledge_search_limit", 0) or 0),
            )
        except Exception as exc:
            logger.debug(f"知识库检索失败: {exc}")
            base.knowledge_context = ""

    # ============================================================
    # 生成逻辑（多轮 / 单轮）
    # ============================================================

    async def _generate_with_multi_round(
        self,
        base: BaseScheduleGenerator,
        validator: ScheduleSemanticValidator,
        scorer: ScheduleQualityScorer,
        gen_config: ScheduleGeneratorConfig,
    ) -> List[Dict[str, Any]]:
        """多轮生成：首轮质量不达标时按反馈重试，保留得分最高的一轮。"""
        max_rounds = gen_config.max_rounds
        quality_threshold = gen_config.quality_threshold

        best_schedule: Optional[List[Dict[str, Any]]] = None
        best_score = 0.0
        validation_warnings: List[str] = []

        for round_num in range(1, max_rounds + 1):
            logger.debug("第 %d 轮生成...", round_num)
            try:
                schema = base.build_json_schema()
                if round_num == 1:
                    prompt = base.build_schedule_prompt("daily", {}, schema)
                else:
                    prompt = base.build_retry_prompt("daily", {}, schema, validation_warnings)

                raw_items = await self._call_llm(prompt, gen_config)

                validated_items, warnings = validator.validate(raw_items)
                validated_items, sleep_warnings = self._apply_no_sleep_postprocess(validated_items, gen_config)
                warnings.extend(sleep_warnings)
                score = scorer.calculate_score(validated_items, warnings)

                logger.debug("第 %d 轮质量分数: %.2f", round_num, score)
                if score > best_score:
                    best_schedule = validated_items
                    best_score = score
                    validation_warnings = warnings
                if score >= quality_threshold:
                    logger.debug("质量达标（%.2f >= %.2f），提前结束", score, quality_threshold)
                    break
            except LLMError as exc:
                # 配额耗尽 / 速率限制属致命错误，直接向上抛（调用方模板兜底）
                logger.warning("第 %d 轮遇到致命 LLM 错误，停止多轮生成: %s", round_num, exc)
                raise
            except Exception as exc:
                # 业务可重试错误（响应解析失败 / 校验失败等），下一轮带反馈重试
                logger.warning("第 %d 轮生成失败: %s", round_num, exc)
                continue

        if best_schedule is None:
            raise ScheduleGenerationError(f"多轮生成全部失败（尝试了{max_rounds}轮）", attempt_count=max_rounds)
        logger.debug("生成 %d 个日程项（质量: %.2f）", len(best_schedule), best_score)
        return best_schedule

    async def _generate_single_round(
        self,
        base: BaseScheduleGenerator,
        validator: ScheduleSemanticValidator,
        gen_config: ScheduleGeneratorConfig,
    ) -> List[Dict[str, Any]]:
        """单轮生成。"""
        schema = base.build_json_schema()
        prompt = base.build_schedule_prompt("daily", {}, schema)
        raw_items = await self._call_llm(prompt, gen_config)
        validated_items, warnings = validator.validate(raw_items)
        validated_items, sleep_warnings = self._apply_no_sleep_postprocess(validated_items, gen_config)
        warnings.extend(sleep_warnings)
        if warnings:
            logger.warning("语义验证发现 %d 个问题", len(warnings))
            for warning in warnings[:3]:
                logger.warning("  ⚠️ %s", warning)
        return validated_items

    def _apply_no_sleep_postprocess(
        self,
        items: List[Dict[str, Any]],
        gen_config: ScheduleGeneratorConfig,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """无睡眠模式后处理——销毁睡眠时段内的日程（部分重叠截断到边界）。

        - 完全落在睡眠时段内的条目 → 直接删除；
        - 部分重叠 → 截断到时段边界（傍晚一侧截到入睡时刻，清晨一侧从起床起算）；
        - 无 time_slot 的条目无法判定归属，原样保留。
        """
        if not bool(getattr(gen_config, "no_sleep_mode", False)):
            return items, []

        sleep_min = _parse_minutes(gen_config.sleep_time, _DEFAULT_SLEEP_MINUTES)
        wake_min = _parse_minutes(gen_config.wake_time, _DEFAULT_WAKE_MINUTES)
        window_len = (wake_min - sleep_min) % (24 * 60)
        if window_len == 0:
            return items, []

        kept: List[Dict[str, Any]] = []
        removed = 0
        truncated = 0
        for item in items:
            start = _parse_minutes(item.get("time_slot"), -1)
            if start < 0:
                kept.append(item)
                continue
            try:
                duration = float(item.get("duration_hours") or 0) * 60
            except (TypeError, ValueError):
                duration = 0.0
            if duration <= 0:
                duration = 60.0
            end = start + duration

            # 睡眠时段是环形区间（可跨午夜）：展开到 24h 周期上求最大重叠
            overlap = 0.0
            best = None
            for k in range(-2, 3):
                win_start = sleep_min + k * 24 * 60
                win_end = win_start + window_len
                o = min(end, win_end) - max(start, win_start)
                if o > overlap:
                    overlap = o
                    best = (win_start, win_end)

            if overlap <= 0:
                kept.append(item)
                continue
            if overlap >= end - start:
                removed += 1
                continue

            win_start, win_end = best
            if start >= win_start:
                # 从窗口内部开始（清晨越界）：起点平移到起床时刻
                new_start = win_end
                new_duration = end - new_start
                if new_duration <= 0:
                    removed += 1
                    continue
                item["time_slot"] = _fmt_minutes(new_start)
                item["duration_hours"] = round(new_duration / 60, 2)
            else:
                # 从清醒时段越入窗口（傍晚越界）：终点截到入睡时刻
                new_duration = win_start - start
                if new_duration <= 0:
                    removed += 1
                    continue
                item["duration_hours"] = round(new_duration / 60, 2)
            truncated += 1
            kept.append(item)

        warnings = []
        if removed or truncated:
            window_text = f"{_fmt_minutes(sleep_min)}-次日{_fmt_minutes(wake_min)}"
            warnings.append(
                f"无睡眠模式：已销毁睡眠时段（{window_text}）内 {removed} 个日程，截断 {truncated} 个越界活动"
            )
            logger.info("无睡眠模式：销毁睡眠时段内 %d 个日程，截断 %d 个越界活动", removed, truncated)
        return kept, warnings

    async def _call_llm(self, prompt: str, gen_config: ScheduleGeneratorConfig) -> List[Dict[str, Any]]:
        """调用 LLM 并解析响应（走 core.llm_utils.LLMService，带任务路由 + 调用日志）。"""
        task_name = gen_config.llm_task_name
        max_tokens = int(gen_config.max_tokens)
        temperature = float(self._raw_of(gen_config).get("temperature", 0.85))
        timeout_ms = int(float(gen_config.generation_timeout) * 1000)

        response = await asyncio.wait_for(
            self._llm.generate(
                prompt,
                task_name=task_name,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout_ms=timeout_ms,
                event="schedule_generation",
            ),
            timeout=float(gen_config.generation_timeout) + 15.0,
        )
        if not response:
            raise LLMError("LLM 调用失败（空响应）")
        return self._parse_response(response)

    # ============================================================
    # 解析 / 兜底 / 归一化 / 约定匹配
    # ============================================================

    @staticmethod
    def _parse_response(response: str) -> List[Dict[str, Any]]:
        """五级容错解析：LLMResponseParser（markdown 清理 + 控制字符清理，必须移植）
        → 整段 JSON → ```json 块 → 最外层 []/{} → 单个 {}。"""
        try:
            return LLMResponseParser.parse_schedule_response(response)
        except LLMInvalidResponseError:
            logger.warning("标准解析失败，退化为宽松正则解析")

        text = (response or "").strip()

        def _loads(candidate: str) -> Any:
            try:
                return json.loads(candidate)
            except (json.JSONDecodeError, ValueError):
                return None

        data = _loads(text)
        if data is None:
            fence = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
            if fence:
                data = _loads(fence.group(1))
        if data is None:
            for pattern in (r"\{.*\}", r"\[.*\]"):
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    data = _loads(m.group(0))
                    if data is not None:
                        break
        if isinstance(data, dict):
            data = data.get("schedule_items")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("name")]

    @staticmethod
    def _build_fallback() -> List[Dict[str, Any]]:
        """模板兜底（LLM 全轮失败时保证总有日程可用）。"""
        now = datetime.now()
        tpl = FALLBACK_TEMPLATE["weekend" if now.weekday() >= 5 else "workday"]
        return [
            {
                "name": name,
                "time_slot": slot,
                "duration_hours": hours,
                "goal_type": goal_type,
                "priority": priority,
                "description": desc,
            }
            for name, slot, hours, goal_type, priority, desc in tpl
        ]

    @staticmethod
    def _normalize_items(
        items: List[Dict[str, Any]],
        date: str,
        wake_time: str,
        sleep_time: str,
    ) -> List[Dict[str, Any]]:
        """把 LLM/模板条目归一化成 DB 记录：time_slot+duration → start/end 分钟，截断到作息锚点内。

        M5（§6.2 E）：条目级 ``emotional_tone`` 经 :func:`clip_emotional_tone` 校验/裁剪
        （±0.5）后落入 ``parameters``（供睡眠大修的前日残差/明日预期读取）；
        非字典/顶层无效一律不落该键。
        """
        wake_min, sleep_min = parse_hhmm(wake_time, 420), parse_hhmm(sleep_time, 1380)
        normalized: List[Dict[str, Any]] = []
        for item in items:
            raw_tone = item.get("emotional_tone")
            if raw_tone is None and isinstance(item.get("parameters"), dict):
                raw_tone = (item.get("parameters") or {}).get("emotional_tone")
            try:
                start = parse_hhmm(str(item.get("time_slot") or "07:00"), wake_min)
                hours = float(item.get("duration_hours") or 1.0)
            except (TypeError, ValueError):
                start, hours = wake_min, 1.0
            hours = max(0.25, min(12.0, hours))
            end = min(sleep_min, start + int(hours * 60))
            if end <= start:
                end = min(sleep_min, start + 30)
            entry = {
                "name": str(item.get("name") or "未命名活动"),
                "description": str(item.get("description") or ""),
                "goal_type": str(item.get("goal_type") or "daily_routine"),
                "priority": str(item.get("priority") or "medium"),
                "start_minute": max(wake_min, start),
                "end_minute": 0,
                "parameters": dict(item.get("parameters") or {}),
                "schedule_date": date,
            }
            tone = clip_emotional_tone(raw_tone)
            if tone:
                entry["parameters"]["emotional_tone"] = tone
            else:
                entry["parameters"].pop("emotional_tone", None)
            entry["end_minute"] = max(entry["start_minute"] + 15, end)
            normalized.append(entry)
        return normalized

    def _match_commitments_to_items(
        self,
        items: List[Dict[str, Any]],
        today_str: str,
    ) -> Dict[int, Dict[str, Any]]:
        """把当日 pending_commitments 一对一匹配到日程条目。

        LLM 生成日程时不会显式标记哪个条目来自约定，本方法在落库前按以下规则映射：
        1. 优先按时间匹配：约定 ``time="HH:MM"`` 与条目起始分钟差 ≤ 30 分钟；
        2. 若约定无时间，按标题子串匹配（约定 title 出现在 name/description 中）；
        3. 一对一约束：每个约定只匹配一个条目，每个条目最多接收一个约定。

        Returns:
            ``{item_index: {"is_commitment": True, "commitment_title": ..., ...}}``。
        """
        pending = self.goal_manager.get_pending_commitments(today_str)
        if not pending:
            return {}

        def _item_start_minutes(it: Dict[str, Any]) -> Optional[int]:
            start = it.get("start_minute")
            if start is not None:
                return int(start)
            slot = it.get("time_slot")
            if slot:
                return _parse_minutes(slot, -1)
            return None

        result: Dict[int, Dict[str, Any]] = {}
        used_item_idx: set = set()

        for commit in pending:
            params = commit.parameters or {}
            commit_time = str(params.get("time") or "").strip()
            commit_title = commit.name.strip()
            commit_notes = str(params.get("notes") or "").strip()
            commit_reason = str(params.get("reason") or "").strip()

            best_idx: Optional[int] = None

            # 规则 1：按时间匹配（≤ 30 分钟）
            if commit_time and ":" in commit_time:
                try:
                    th, tm = commit_time.split(":", 1)
                    commit_minutes = int(th) * 60 + int(tm)
                except ValueError:
                    commit_minutes = None
                if commit_minutes is not None:
                    best_diff = 31
                    for idx, it in enumerate(items):
                        if idx in used_item_idx:
                            continue
                        item_minutes = _item_start_minutes(it)
                        if item_minutes is None:
                            continue
                        diff = abs(item_minutes - commit_minutes)
                        if diff < best_diff:
                            best_diff = diff
                            best_idx = idx

            # 规则 2：标题子串匹配（兜底）
            if best_idx is None and commit_title:
                for idx, it in enumerate(items):
                    if idx in used_item_idx:
                        continue
                    haystack = f"{it.get('name') or ''}{it.get('description') or ''}"
                    if commit_title in haystack:
                        best_idx = idx
                        break

            if best_idx is None:
                logger.warning(
                    "约定 %r 未在日程项中找到对应活动（time=%s）",
                    commit_title, commit_time or "未指定",
                )
                continue

            result[best_idx] = {
                "is_commitment": True,
                "commitment_title": commit_title,
                "commitment_notes": commit_notes,
                "commitment_reason": commit_reason,
                "commitment_time": commit_time,
            }
            used_item_idx.add(best_idx)
            logger.info("约定 %r → 日程项[%d] %r", commit_title, best_idx, items[best_idx].get("name"))

        return result
