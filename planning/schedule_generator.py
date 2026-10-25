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

2026-10-25 滚动化改造新增（R7 依据 = 用户 2026-10-25 直派，设计目录
D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）：
- ``generate_daily_schedule(..., mode="spine")`` 分支：每天只生成 3-6 条稀疏日意图
  （+ 硬约定 + 作息锚点），意图按【spine 意图存储契约】写 ``schedule_items``
  （``parameters`` 附加 ``spine/min_minutes/max_minutes/priority/hardness/
  interruptibility/note`` 键；窗口 start/end=window 起止分钟，跨午夜 end 允许 >1440，
  空档合法、不要求覆盖全天）；进账照走既有 ``_replace_with_entry``（打戳链路一字不动，
  reason=scheduled_generate）；LLM 失败回退模板 spine（锚定 [schedule] 作息锚点，
  禁硬编码时刻），不重试多轮；
- ``mode`` 缺省 ``"daily"``：旧全天生成路径零变化。
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
from suite_prompts import (
    SCHEDULE_HOLIDAY_CONTEXT_DEFAULT,
    prompt as prompt_of,
    render,
)
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
from .generator.validator import is_gap_warning
from .goal_manager import GoalManager, get_goal_manager
from .cycle_state import (REASON_FALLBACK, REASON_REGENERATE, REASON_SCHEDULED,
                          enter_schedule_cycle, entry_context_from_plugin)  # §31.2 进账漏斗；R7 2026-10-23
from .schedule_db import ScheduleDB
from .schedule_items import is_night_sleep_item
from .time_utils import (DAY_MINUTES, is_in_sleep_ring, normalize_day_minutes,
                         ring_minutes_ahead)

logger = logging.getLogger(__name__)

# 作息解析默认值（配置留空/非法时兜底）：07:00 起床，23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60

# 降级模板（LLM 全轮失败时按工作日/周末取用；time_slot 起始，时长小时）
# A-24.1（任务A，2026-10-21 R7 直派 / 计划书 §24.1）：起床类首条目与 ``睡觉`` 条目的钟点/时长位
# 只是占位——``_build_fallback`` 会按 [schedule] 生效锚点动态写（``睡觉`` 行不再硬编码入睡时刻，
# 行内书面占位留空）；中间条目按模板钟点保留，但钟点落在睡眠环内的会在构建时丢弃
# （宁缺勿碎，避免 ``_normalize_items`` 的夹逼再产 15 分钟碎片）。
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
        ("睡觉", "", 9.0, "sleep", "high", "晚安，明天见"),
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
        ("睡觉", "", 9.0, "sleep", "high", "晚安，明天见"),
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
        reason: Optional[str] = None,
        mode: str = "daily",
    ) -> List[Dict[str, Any]]:
        """生成并落库某天日程；多轮生成 + 质量评分，LLM 全轮失败时模板兜底。

        兼容契约：前 9 个关键字参数与既有调用方一致；其余为 v4.9.1 扩展（带默认值）。

        Args:
            date: 目标日期（``YYYY-MM-DD``；提示词日期取当天，日程落库按此键）。
            apply: True=落库并做约定匹配/消费；False=仅生成（仍写入日程表以便查询）。
            reason: 进账原因（§31.2 血缘，R7 = 用户 2026-10-23 第二轮直派）：例行生成
                缺省 ``scheduled_generate``；regenerate 指令链透传 ``regenerate``；
                LLM 全轮失败走模板兜底时改 ``fallback_template``（仅对例行生成路径，
                regenerate 调用点语义优先——原因按调用点区分进记录，见 notes）。
            mode: ``"daily"``（缺省，旧全天生成路径，行为零变化）或 ``"spine"``
                （2026-10-25 滚动化改造新增：稀疏日意图分支，见模块 docstring 与
                :meth:`_generate_spine_schedule`）。
        """
        # 滚动化改造（2026-10-25，默认关）：spine 分支——旧行为只在 mode="daily" 时走
        if str(mode or "daily").strip() == "spine":
            return await self._generate_spine_schedule(
                date,
                personality=personality, lover_name=lover_name,
                wake_time=wake_time, sleep_time=sleep_time,
                timeout_seconds=timeout_seconds, custom_prompt=custom_prompt,
                timezone=timezone, llm_task_name=llm_task_name,
                temperature=temperature, bot_profile=bot_profile,
                no_sleep_mode=no_sleep_mode, apply=apply, reason=reason,
            )
        entry_reason = str(reason or REASON_SCHEDULED)
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
        await self._load_holiday_context(base, date)
        await self._load_async_contexts(base, gen_config, date)

        # 多轮 / 单轮生成
        use_multi = gen_config.use_multi_round if use_multi_round is None else bool(use_multi_round)
        try:
            if use_multi:
                raw_items = await self._generate_with_multi_round(
                    base, validator, scorer, gen_config,
                    date=date, wake_time=wake_time, sleep_time=sleep_time)
            else:
                raw_items = await self._generate_single_round(
                    base, validator, gen_config,
                    date=date, wake_time=wake_time, sleep_time=sleep_time)
        except ScheduleGenerationError as exc:
            logger.warning("日程 LLM 生成失败（%s），使用模板兜底", exc)
            raw_items = self._build_fallback(wake_time=wake_time, sleep_time=sleep_time)
            # §31.2 进账原因：例行生成的模板兜底 = fallback_template（regenerate 调用点
            # 语义优先不覆盖，R7 注明；判据面的同日/跨天归类见 planning/cycle_state.rotate）
            if entry_reason == REASON_SCHEDULED:
                entry_reason = REASON_FALLBACK
        except LLMError as exc:
            logger.warning("日程 LLM 致命错误（%s），使用模板兜底", exc)
            raw_items = self._build_fallback(wake_time=wake_time, sleep_time=sleep_time)
            if entry_reason == REASON_SCHEDULED:
                entry_reason = REASON_FALLBACK

        # 归一化为 DB 记录并落库（含约定匹配 + 消费）
        items = self._normalize_items(raw_items, date, wake_time, sleep_time)
        # M5 连续低迷软约束（§6.2 F）：强制插入一次 rest/social（原本没有恢复类活动时）
        items, inserted = self._ensure_recovery_item(
            items, date=date, low_mood=base.low_mood_days >= LOW_MOOD_MIN_DAYS,
            wake_time=wake_time, sleep_time=sleep_time)
        if inserted:
            logger.info("连续低迷软约束：已为 %s 强制插入一次恢复类活动（rest/social）", date)
        if apply:
            self.apply_schedule_items(items, date, consume_commitments=True,
                                      reason=entry_reason)
        else:
            self._replace_with_entry(date, items, entry_reason)
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
        # §31.2 血缘：进账前旧日行归档（先删后写场景的"旧日行"即本次进账所替换者；R7 2026-10-23）
        removed = self._db.delete_day_schedule(date, archive_reason=REASON_REGENERATE)
        if removed:
            logger.info("已清理 %s 的 %d 个旧日程，开始重新生成", date, removed)

        base_custom = str(kwargs.pop("custom_prompt", "") or "")
        merged = f"{base_custom}\n\n{extra_prompt}".strip() if (base_custom or extra_prompt) else ""
        return await self.generate_daily_schedule(
            date, personality=personality, lover_name=lover_name,
            custom_prompt=merged, apply=apply, reason=REASON_REGENERATE, **kwargs,
        )

    def apply_schedule_items(
        self,
        items: List[Dict[str, Any]],
        date: str,
        *,
        consume_commitments: bool = True,
        reason: str = REASON_SCHEDULED,
    ) -> int:
        """把日程条目落库（start/end 分钟已换算 + 约定匹配注入）。

        步骤：
        1. ``_match_commitments_to_items``：把当日 pending_commitments 一对一映射到
           条目（时间差 ≤30 分钟优先，其次标题子串），注入 ``is_commitment`` 元数据
           （ProactiveService 据此用强指令兑现约定）；
        2. ``_replace_with_entry`` 整体写入（进账漏斗：先算包络并 rotate，再写库盖戳；
           §31.2，R7 = 用户 2026-10-23 第二轮直派）；
        3. 成功后消费（标记 COMPLETED）当日约定。

        Args:
            reason: 进账原因（四种之一，§31.2 血缘）：例行生成缺省 ``scheduled_generate``；
                regenerate 指令链 ``regenerate``；bot 工具（赖床）``tool_edit``；模板
                兜底 ``fallback_template``。未按新管线传原因的旧调用方/直调按缺省值。
                **行级盖戳与四变量轮换全部由代码完成，LLM 不参与**。

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

        written = self._replace_with_entry(date, items, reason)

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

    def _replace_with_entry(self, date: str, items: List[Dict[str, Any]], reason: str) -> int:
        """进账漏斗（§31.2，R7 = 用户 2026-10-23 第二轮直派）：写库前先经
        ``planning.cycle_state.enter_schedule_cycle`` 算包络并 rotate（四种 reason 同用），
        再 ``replace_day_schedule`` 落库行级盖戳。轮换与决策日志 best-effort，写库路径
        不因状态层失败中断。"""
        plugin = self._plugin
        stamps = enter_schedule_cycle(
            getattr(plugin, "_data_dir", None), date, items, reason=reason,
            decision_logger=getattr(plugin, "_decision_logger", None),
            **entry_context_from_plugin(plugin),
        )
        return self._db.replace_day_schedule(
            date, items, entry_reason=reason,
            cycle_start_ts=(stamps[0] if stamps else None),
            cycle_end_ts=(stamps[1] if stamps else None),
        )

    # ============================================================
    # spine 日意图分支（2026-10-25 滚动化改造新增；R7 依据 = 用户 2026-10-25 直派，
    # 设计目录 D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）
    # ============================================================

    #: spine 意图 parameters 键的合法取值（【spine 意图存储契约】）
    SPINE_PRIORITIES = ("high", "medium", "low")
    SPINE_HARDNESS = ("hard", "soft")

    def _spine_params(self) -> Dict[str, Any]:
        """读 ``[schedule]`` 的 spine_* 键（运行期活取值；插件未挂载/键非法回退出厂默认）。"""
        cfg = getattr(getattr(self._plugin, "config", None), "schedule", None)

        def _int(key: str, default: int, lo: int) -> int:
            try:
                value = int(getattr(cfg, key, default))
            except (TypeError, ValueError):
                return default
            return value if value >= lo else default

        max_intentions = _int("spine_max_intentions", 6, 1)
        min_intentions = _int("spine_min_intentions", 3, 1)
        if min_intentions > max_intentions:
            min_intentions = max_intentions
        return {
            "min_intentions": min_intentions,
            "max_intentions": max_intentions,
            "max_described": _int("spine_max_described_intents", 1, 0),
            "recent_days": _int("spine_recent_days", 2, 0),
            "history_messages": _int("spine_history_messages", 12, 0),
            "knowledge_limit": _int("spine_knowledge_limit", 3, 0),
            "max_tokens": _int("spine_max_tokens", 2048, 256),
            "use_multi_round": bool(getattr(cfg, "spine_use_multi_round", False)),
        }

    async def _generate_spine_schedule(
        self,
        date: str,
        *,
        personality: str = "",
        lover_name: str = "麦麦",
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
        timeout_seconds: float = 120.0,
        custom_prompt: str = "",
        timezone: str = "Asia/Shanghai",
        llm_task_name: str = "",
        temperature: float = 0.85,
        bot_profile: Optional[Dict[str, str]] = None,
        no_sleep_mode: bool = False,
        apply: bool = True,
        reason: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """spine 分支：生成 3-6 条稀疏日意图并按【spine 意图存储契约】落库。

        - 意图按契约写 ``schedule_items``：窗口 start/end = window 起止分钟
          （跨午夜 end 允许 >1440，空档合法、不要求覆盖全天）；``parameters`` 附加
          ``spine/min_minutes/max_minutes/priority/hardness/interruptibility/note``；
        - 进账照走既有 :meth:`_replace_with_entry`（enter_schedule_cycle 打戳链路
          一字不动）；例行生成 reason=``scheduled_generate``，模板兜底改
          ``fallback_template``（与旧全天路径的血缘口径一致）；
        - LLM 全轮失败/空响应回退模板 spine（:meth:`_build_spine_fallback`，
          锚定 [schedule] 作息锚点、禁硬编码时刻），不强制重试多轮。
        """
        params = self._spine_params()
        entry_reason = str(reason or REASON_SCHEDULED)
        gen_config = ScheduleGeneratorConfig(self._build_config_dict(
            personality=personality, lover_name=lover_name, wake_time=wake_time,
            sleep_time=sleep_time, min_activities=params["min_intentions"],
            max_activities=params["max_intentions"], timeout_seconds=timeout_seconds,
            use_multi_round=False, max_rounds=1, quality_threshold=1.0,
            enable_detailed_description=True, min_description_length=15,
            max_description_length=25, max_tokens=params["max_tokens"],
            custom_prompt=custom_prompt, recent_schedule_days=params["recent_days"],
            history_message_limit=params["history_messages"],
            knowledge_search_limit=params["knowledge_limit"],
            cross_day_activity=True, no_sleep_mode=no_sleep_mode, timezone=timezone,
            llm_task_name=llm_task_name, temperature=temperature, bot_profile=bot_profile,
        ))

        # 上下文装载（与 daily 同一批槽位；情绪块不注入——其措辞含「无缝衔接要求」，与稀疏口径冲突）
        base = BaseScheduleGenerator(self.goal_manager, gen_config.to_dict())
        self._load_sync_contexts(base, gen_config, date)
        self._load_low_mood(base)
        await self._load_holiday_context(base, date)
        await self._load_async_contexts(base, gen_config, date)

        schema = base.build_spine_schema(params["min_intentions"], params["max_intentions"])
        prompt = base.build_spine_prompt(
            schema,
            min_intentions=params["min_intentions"], max_intentions=params["max_intentions"],
            max_described=params["max_described"], no_sleep_mode=no_sleep_mode,
        )

        rows: List[Dict[str, Any]] = []
        last_error: Optional[Exception] = None
        # spine_use_multi_round=true 时最多 2 次同提示重试（结构校验：拿到可用意图即收），
        # 不带反馈重试、不跑质量评分（设计 §4.2：单轮、结构校验、不强制重试）
        for _ in range(2 if params["use_multi_round"] else 1):
            try:
                response = await self._call_llm(prompt, gen_config, event="schedule_spine",
                                                parse=False)
            except LLMError as exc:
                # 配额耗尽 / 速率限制属致命错误，不再重试
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                continue
            try:
                rows = self._spine_rows_from_intentions(
                    self._parse_spine_response(response), date, params)
            except Exception as exc:
                last_error = exc
                rows = []
                continue
            if rows:
                break
        if rows:
            # 锚点护栏：睡觉意图缺失（且非无睡眠模式）时补一条模板睡觉意图（hard）
            rows = self._ensure_spine_sleep_intent(rows, date, wake_time, sleep_time,
                                                   max_intentions=params["max_intentions"])
        else:
            if last_error is not None:
                logger.warning("spine LLM 生成失败（%s），使用模板 spine 兜底", last_error)
            else:
                logger.warning("spine LLM 响应无有效意图，使用模板 spine 兜底")
            rows = self._build_spine_fallback(
                date, wake_time=wake_time, sleep_time=sleep_time,
                no_sleep_mode=no_sleep_mode, max_intentions=params["max_intentions"])
            if entry_reason == REASON_SCHEDULED:
                entry_reason = REASON_FALLBACK

        if apply:
            self.apply_schedule_items(rows, date, consume_commitments=True,
                                      reason=entry_reason)
        else:
            self._replace_with_entry(date, rows, entry_reason)
        logger.info("spine 日意图生成完成：%s 共 %d 条", date, len(rows))
        await self._publish("schedule.generated", date=date, items=len(rows))
        return rows

    @staticmethod
    def _parse_spine_response(response: str) -> List[Dict[str, Any]]:
        """解析 spine 响应：整段 JSON → ```json 块 → 最外层 {} / []（宽松降级）。

        接受 ``{"intentions": [...]}``（提示词要求）、``{"schedule_items": [...]}``
        或裸数组；元素必须带 name。解析失败返回空表（调用方模板兜底，不抛异常）。
        """
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
            data = data.get("intentions") or data.get("schedule_items")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict) and item.get("name")]

    @classmethod
    def _spine_rows_from_intentions(
        cls,
        intentions: List[Dict[str, Any]],
        date: str,
        params: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """把 LLM 意图列表结构化成 DB 行（只做结构校验/钳制，不强制重试）。

        非法意图（缺 name / window 缺失或非法）丢弃；超出 max_intentions 截断；
        按 start_minute 排序。窗口跨午夜（end <= start）沿 24h 环抬到次日
        （end 允许 >1440，日帧口径；禁手写 −1440，走 :data:`DAY_MINUTES`）。
        """
        rows: List[Dict[str, Any]] = []
        for raw in intentions or []:
            if not isinstance(raw, dict):
                continue
            row = cls._spine_intention_to_row(raw, date)
            if row is not None:
                rows.append(row)
        if len(rows) > int(params.get("max_intentions", 6)):
            rows = rows[: int(params.get("max_intentions", 6))]
        rows.sort(key=lambda it: int(it["start_minute"]))
        return rows

    @staticmethod
    def _spine_intention_to_row(raw: Dict[str, Any], date: str) -> Optional[Dict[str, Any]]:
        """单条意图 → DB 行（【spine 意图存储契约】；非法返回 None）。"""
        name = str(raw.get("name") or "").strip()
        window = raw.get("window")
        if not name or not isinstance(window, (list, tuple)) or len(window) < 2:
            return None
        start = parse_hhmm(str(window[0]), -1)
        end = parse_hhmm(str(window[1]), -1)
        if start < 0:
            return None
        if end < 0:
            end = start + 60
        if end <= start:
            # 跨午夜窗口（window 止点钟点不晚于起点，如夜间 21:30 到次日 00:30）：
            # 沿 24h 环抬到次日（end 允许 >1440）
            end += DAY_MINUTES
            if end <= start:
                return None

        def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
            try:
                number = int(value)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, number))

        min_minutes = _clamp_int(raw.get("min_minutes"), 30, 5, 720)
        max_minutes = _clamp_int(raw.get("max_minutes"), min_minutes, 5, 720)
        if max_minutes < min_minutes:
            max_minutes = min_minutes
        goal_type = str(raw.get("goal_type") or "").strip() or "daily_routine"
        priority = str(raw.get("priority") or "").strip()
        if priority not in ScheduleGenerator.SPINE_PRIORITIES:
            priority = "medium"
        hardness = str(raw.get("hardness") or "").strip()
        if hardness not in ScheduleGenerator.SPINE_HARDNESS:
            hardness = "soft"
        try:
            interruptibility = float(raw.get("interruptibility"))
        except (TypeError, ValueError):
            interruptibility = 0.5
        interruptibility = max(0.0, min(1.0, interruptibility))
        note = str(raw.get("note") or "").strip()
        # 【spine 意图存储契约】：parameters JSON 列附加键（note 可选）
        parameters: Dict[str, Any] = {
            "spine": True,
            "min_minutes": int(min_minutes),
            "max_minutes": int(max_minutes),
            "priority": priority,
            "hardness": hardness,
            "interruptibility": interruptibility,
        }
        if note:
            parameters["note"] = note
        return {
            "name": name,
            "description": note,  # description 列照旧使用（= note；无 note 为空串）
            "goal_type": goal_type,
            "priority": priority,
            "start_minute": int(start),
            "end_minute": int(end),
            "day_offset": 0,
            "parameters": parameters,
            "schedule_date": date,
        }

    @classmethod
    def _ensure_spine_sleep_intent(
        cls,
        rows: List[Dict[str, Any]],
        date: str,
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
        max_intentions: int = 6,
    ) -> List[Dict[str, Any]]:
        """锚点护栏：睡觉意图缺失时补一条模板睡觉意图（hardness=hard，时间固定）。

        补入后若超出 ``max_intentions`` 上限，丢掉排序最靠后的软意图给硬锚点让位
        （3~6 条契约含睡觉锚点；hard 锚点优先级高于 LLM 软意图）。
        """
        for row in rows:
            name = str(row.get("name") or "")
            if str(row.get("goal_type") or "") == "sleep" or ("睡" in name or "眠" in name):
                return rows
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)
        span = max(180, min(720, ring_minutes_ahead(wake_min, sleep_min)))
        rows.append(cls._spine_row(date, "睡觉", sleep_min, sleep_min + span, "sleep",
                                   "high", span, span, hardness="hard",
                                   interruptibility=0.05))
        rows.sort(key=lambda it: int(it["start_minute"]))
        try:
            cap = max(1, int(max_intentions))
        except (TypeError, ValueError):
            cap = 6
        while len(rows) > cap:
            # 从尾部丢软意图（睡觉行 hardness=hard，恒保留）
            victim = next((it for it in reversed(rows)
                           if it.get("parameters", {}).get("hardness") != "hard"), None)
            if victim is None:
                break
            rows.remove(victim)
        return rows

    @staticmethod
    def _spine_row(
        date: str,
        name: str,
        start: int,
        end: int,
        goal_type: str,
        priority: str,
        min_minutes: int,
        max_minutes: int,
        *,
        hardness: str = "soft",
        interruptibility: float = 0.5,
        note: str = "",
    ) -> Dict[str, Any]:
        """构造一条 spine 意图 DB 行（模板兜底与锚点护栏共用；分钟数为日帧口径）。"""
        parameters: Dict[str, Any] = {
            "spine": True,
            "min_minutes": int(min_minutes),
            "max_minutes": int(max_minutes),
            "priority": priority,
            "hardness": hardness,
            "interruptibility": float(interruptibility),
        }
        if note:
            parameters["note"] = note
        return {
            "name": name,
            "description": note,
            "goal_type": goal_type,
            "priority": priority,
            "start_minute": int(start),
            "end_minute": int(end),
            "day_offset": 0,
            "parameters": parameters,
            "schedule_date": date,
        }

    @staticmethod
    def _build_spine_fallback(
        date: str,
        *,
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
        no_sleep_mode: bool = False,
        max_intentions: int = 6,
    ) -> List[Dict[str, Any]]:
        """模板 spine 兜底（LLM 全轮失败 / 空响应；不重试多轮，设计 §4.4）。

        口径（behavior 183 守卫同源）：锚定 ``[schedule]`` 生效作息锚点生成
        睡觉/起床/三餐 + 1 个通用意图——**禁硬编码时刻**，所有钟点一律
        ``parse_hhmm(wake_time/sleep_time)`` 同链同源派生（早餐锚起床、午餐锚日间
        三分点、晚餐锚入睡前沿）；跨午夜作息（sleep <= wake）的清醒跨度经
        :func:`ring_minutes_ahead` 环距计算，不手写 −1440。
        """
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)
        # 清醒跨度（环距；常规作息 = sleep−wake，跨午夜 = wake−sleep+1440；退化 = 全天）
        awake_span = ring_minutes_ahead(sleep_min, wake_min) or DAY_MINUTES
        awake_end = wake_min + awake_span  # = sleep_min（常规）或 sleep_min+1440（跨午夜）

        def row(name: str, start: int, end: int, goal_type: str, priority: str,
                min_minutes: int, max_minutes: int, interruptibility: float) -> Dict[str, Any]:
            return ScheduleGenerator._spine_row(
                date, name, start, end, goal_type, priority,
                min_minutes, max_minutes, interruptibility=interruptibility)

        rows: List[Dict[str, Any]] = []
        if not no_sleep_mode:
            span = max(180, min(720, ring_minutes_ahead(wake_min, sleep_min)))
            rows.append(row("睡觉", sleep_min, sleep_min + span, "sleep", "high",
                            span, span, 0.05))
        rows.append(row("起床洗漱", wake_min, wake_min + 30, "daily_routine", "medium", 15, 30, 0.9))
        rows.append(row("早餐", wake_min, wake_min + 120, "meal", "medium", 15, 40, 0.9))
        mid = wake_min + awake_span // 3
        rows.append(row("午餐", max(wake_min, mid - 90), min(awake_end, mid + 90),
                        "meal", "medium", 20, 45, 0.9))
        if awake_span >= 480:
            rows.append(row("自由安排", wake_min + 150, awake_end - 330,
                            "free_time", "low", 30, 90, 0.95))
        if awake_span >= 360:
            rows.append(row("晚餐", awake_end - 300, awake_end,
                            "meal", "medium", 20, 45, 0.9))
        if len(rows) > max_intentions:
            # 截断保睡觉（硬锚点）：睡觉行 + 起始的 (max-1) 条
            sleep_rows = [r for r in rows if r["goal_type"] == "sleep"]
            others = [r for r in rows if r["goal_type"] != "sleep"]
            rows = sleep_rows + others[: max(0, max_intentions - len(sleep_rows))]
        rows.sort(key=lambda it: int(it["start_minute"]))
        return rows

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
        # 2026-10-10：把 plugin 引用带进生成配置字典（提示词读 [prompts] 段用；to_dict 会透传）
        cfg["plugin"] = self._plugin
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
        """``[schedule_advanced].emotion_influence``（0~1，默认 0.6；0=关闭），运行期活取值。"""
        cfg = (getattr(getattr(self._plugin, "config", None), "schedule_advanced", None)
               or getattr(getattr(self._plugin, "config", None), "schedule", None))
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
            base.emotion_context = build_emotion_context(mood, levels, influence,
                                                         plugin=self._plugin)
        except Exception as exc:
            base.emotion_context = ""
            logger.debug("睡醒情绪上下文装载失败（忽略）: %s", exc)

    async def _load_holiday_context(self, base: BaseScheduleGenerator, date: str) -> None:
        """节假日上下文（2026-10-10）：``[schedule] holiday_affects_schedule`` 开启时装载。

        取数走 planning 自己的 ``HolidayService``（``plugin._planning.holiday``；多级降级 +
        3 天 TTL 磁盘缓存），命中节假日/补班时把「今天是 X（星期Y）：节假日状态」写进生成
        提示词；开关关闭 / 服务不可得 / 查询失败一律空串（**行为与旧版逐字一致**）。
        """
        base.holiday_context = ""
        try:
            cfg = (getattr(getattr(self._plugin, "config", None), "schedule", None)
                   if self._plugin is not None else None)
            if not bool(getattr(cfg, "holiday_affects_schedule", False)):
                return
            planning = getattr(self._plugin, "_planning", None)
            holiday_svc = getattr(planning, "holiday", None)
            if holiday_svc is None:
                return
            info = await holiday_svc.get_holiday_info(str(date or ""))
            info = str(info or "").strip()
            if not info:
                return
            try:
                weekday = "一二三四五六日"[datetime.strptime(str(date), "%Y-%m-%d").weekday()]
            except Exception:
                weekday = "?"
            base.holiday_context = render(
                prompt_of(self._plugin, "schedule_holiday_context"),
                fallback=SCHEDULE_HOLIDAY_CONTEXT_DEFAULT,
                date=str(date), weekday=weekday, holiday=info,
            )
        except Exception as exc:
            base.holiday_context = ""
            logger.debug("节假日上下文装载失败（忽略）: %s", exc)

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
        """构造一条强制插入的恢复类条目（rest）。

        N2：切分跨午夜尾段时 ``start_minute`` 可能 ≥1440（如 24:00），按日帧归一落回
        ``[0, 1440)``（同一个 24h 环内的窗口不变，如 24:00-01:00 ⇒ 00:00-01:00），
        避免 DB 行带帧外起点。
        """
        raw_start = max(0, int(start_minute))
        # A-16：起点 ≥1440（跨午夜尾段切分产物，如 24:00）折回当日钟点时同步标 day_offset=1
        day_offset = 1 if raw_start >= 1440 else 0
        start = raw_start - (1440 if raw_start >= 1440 else 0)
        return {"name": "放空休息", "description": "留一段恢复时间，缓一缓（最近有点低落）",
                "goal_type": "rest", "priority": "medium",
                "start_minute": start, "end_minute": start + int(minutes),
                "day_offset": day_offset, "parameters": {}, "schedule_date": date}

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
        *,
        date: str = "",
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
    ) -> List[Dict[str, Any]]:
        """多轮生成：首轮质量不达标时按反馈重试，保留得分最高的一轮。

        N2/N1：每轮在 ``_normalize_items`` **之后**再跑一次空档检查——归一化制造的空档
        （旧实现把条目压成 15 分钟）只有此时才看得见；空档警告随 ``validation_warnings``
        进入下一轮反馈，并在评分里单独重罚，能压到质量阈值以下触发重试。
        ``validator.validate`` 在**原始形态**上报的空档类警告会被丢弃（它按钟点排序、无跨午夜
        环概念，跨午夜作息下会把睡眠时段误报成空档，且与归一化复查重复）；空档的唯一真源 =
        归一化之后的复查结果。
        """
        max_rounds = gen_config.max_rounds
        quality_threshold = gen_config.quality_threshold
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)

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
                # 归一化后复查：空档只有归一化之后才可见（N2/N1），评分同样看归一化结果
                normalized = self._normalize_items(validated_items, date, wake_time, sleep_time)
                gap_warnings = validator.check_normalized_gaps(
                    normalized, wake_min=wake_min, sleep_min=sleep_min)
                # 空档以**归一化后的复查**为准（写库形态的真源）：原始形态按钟点排序，跨午夜作息下
                # 会把睡眠时段误报成空档，且同一条空档会被两种形态各报一次 → 原始形态的空档类警告丢弃。
                # 空档排在最前：重试反馈只列前 5 条（build_retry_prompt），结构性问题优先
                round_warnings = gap_warnings + [w for w in warnings if not is_gap_warning(w)]
                score = scorer.calculate_score(normalized, round_warnings)

                logger.debug("第 %d 轮质量分数: %.2f", round_num, score)
                if score > best_score:
                    best_schedule = validated_items
                    best_score = score
                    validation_warnings = round_warnings
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
        *,
        date: str = "",
        wake_time: str = "07:00",
        sleep_time: str = "23:00",
    ) -> List[Dict[str, Any]]:
        """单轮生成（无重试轮次；归一化后的空档只记日志，N2）。"""
        schema = base.build_json_schema()
        prompt = base.build_schedule_prompt("daily", {}, schema)
        raw_items = await self._call_llm(prompt, gen_config)
        validated_items, warnings = validator.validate(raw_items)
        validated_items, sleep_warnings = self._apply_no_sleep_postprocess(validated_items, gen_config)
        warnings.extend(sleep_warnings)
        # 归一化后复查：空档只有归一化之后才可见（N2/N1）；原始形态的空档类警告丢弃（跨午夜误报）
        normalized = self._normalize_items(validated_items, date, wake_time, sleep_time)
        warnings = validator.check_normalized_gaps(
            normalized,
            wake_min=parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES),
            sleep_min=parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES),
        ) + [w for w in warnings if not is_gap_warning(w)]
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

    async def _call_llm(
        self,
        prompt: str,
        gen_config: ScheduleGeneratorConfig,
        *,
        event: str = "schedule_generation",
        parse: bool = True,
    ) -> Any:
        """调用 LLM（走 core.llm_utils.LLMService，带任务路由 + 调用日志）。

        ``event`` = 消耗账本（core/llm_usage.py）的调用点标签：旧路径恒
        ``schedule_generation``；spine 分支传 ``schedule_spine``（2026-10-25 滚动化
        改造新增，本轮唯一新增 LLM 调用点）。缺省值保证旧调用点零变化。

        ``parse=True``（缺省）按旧口径解析成日程条目列表；``parse=False`` 返回原始
        文本（spine 用自己的 ``_parse_spine_response``——旧解析器要求
        ``schedule_items`` 键，会把 spine 的 ``intentions`` 响应清空）。
        """
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
                event=event,
            ),
            timeout=float(gen_config.generation_timeout) + 15.0,
        )
        if not response:
            raise LLMError("LLM 调用失败（空响应）")
        return self._parse_response(response) if parse else response

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
    def _build_fallback(wake_time: str = "07:00", sleep_time: str = "23:00") -> List[Dict[str, Any]]:
        """模板兜底（LLM 全轮失败时保证总有日程可用）——按生效作息锚定（A-24.1 任务A）。

        口径（计划书 §24.1 FallbackAnchor；调用方透传 [schedule] 生效锚点）：

        - 起床类首条目 start = 起床锚点；``睡觉`` 条目 start = 入睡锚点、时长 =
          睡眠环跨度（入睡锚点 → 起床锚点，:func:`ring_minutes_ahead`；常规作息即
          ``wake+1440−sleep`` 分钟、跨午夜作息即 ``wake−sleep``），钳 3~12 小时；
        - 中间条目保持模板钟点，但钟点落在睡眠环内（帧内早于起床锚点 / 不早于入睡
          锚点，帧内口径唯一实现 = :func:`is_in_sleep_ring`）的条目**丢弃**——兜底是
          填充物，宁缺勿碎，避免 ``_normalize_items`` 的夹逼再产 15 分钟碎片；
        - 锚点解析非法 → 420/1380（07:00/23:00，与生成链 ``_normalize_items`` 同源）。
          任何路径不再出现硬编码入睡时刻的兜底睡觉段，写入钟点一律来自生效锚点。
        """
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)
        sleep_hours = max(3.0, min(12.0, ring_minutes_ahead(wake_min, sleep_min) / 60.0))
        now = datetime.now()
        tpl = FALLBACK_TEMPLATE["weekend" if now.weekday() >= 5 else "workday"]
        anchored: List[Dict[str, Any]] = []
        for idx, (name, slot, hours, goal_type, priority, desc) in enumerate(tpl):
            if idx == 0:
                # 起床类首条目：start = 起床锚点
                slot = _fmt_minutes(wake_min)
            elif goal_type == "sleep":
                # ``睡觉`` 条目：start = 入睡锚点，时长 = 睡眠环跨度（钳 3~12h）
                slot = _fmt_minutes(sleep_min)
                hours = sleep_hours
            else:
                # 中间条目保持模板钟点；钟点落在睡眠环 → 丢弃（宁缺勿碎）
                slot_min = _parse_minutes(slot, -1)
                if slot_min >= 0 and is_in_sleep_ring(slot_min, wake_min, sleep_min):
                    continue
            anchored.append({
                "name": name,
                "time_slot": slot,
                "duration_hours": hours,
                "goal_type": goal_type,
                "priority": priority,
                "description": desc,
            })
        return anchored

    @staticmethod
    def _normalize_items(
        items: List[Dict[str, Any]],
        date: str,
        wake_time: str,
        sleep_time: str,
    ) -> List[Dict[str, Any]]:
        """把 LLM/模板条目归一化成 DB 记录：time_slot+duration → start/end 分钟（跨午夜 = 同一个 24h 环）。

        口径（N2/N4，与 ``prompt_builder`` 「跨午夜用时长表达、不要硬切成两条」的提示词要求对齐）：

        - ``cross = sleep_min <= wake_min``（入睡锚点早于起床锚点 = 跨午夜作息，
          如 01:00 入睡 / 07:00 起床）；
        - **睡眠环内**的条目（:func:`is_in_sleep_ring`）与**夜晚睡眠条目**
          （:func:`is_night_sleep_item`：``goal_type=="sleep"`` 或夜间名）保留 ``start + 时长``，
          ``end`` 允许 >1440（如 23:00 入睡 + 8h ⇒ 1380→1860），交由展示层同环归一；
          不再被起床锚点上钳、不再被 +15 分钟地板压成 15 分钟；
        - **非睡眠**条目的 ``end`` 才夹到入睡锚点：
          ``min(start+时长, sleep_min + (1440 if cross else 0))``。

        这样 ``sleep_min`` 早于条目起点时不会再触发「``end<=start`` → ``start+15``」的连锁塌缩
        （旧实现把全天条目压成 15 分钟、制造 45–225 分钟级空档）。

        M5（§6.2 E）：条目级 ``emotional_tone`` 经 :func:`clip_emotional_tone` 校验/裁剪
        （±0.5）后落入 ``parameters``（供睡眠大修的前日残差/明日预期读取）；
        非字典/顶层无效一律不落该键。
        """
        wake_min, sleep_min = parse_hhmm(wake_time, 420), parse_hhmm(sleep_time, 1380)
        cross = sleep_min <= wake_min
        #: 非睡眠条目的 end 上限 = 下一个入睡时刻（跨午夜作息下落次日，故 +1440）
        awake_end_bound = sleep_min + (1440 if cross else 0)
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
            duration = int(hours * 60)
            sleep_like = bool(is_night_sleep_item(item) or is_in_sleep_ring(start, wake_min, sleep_min))
            if sleep_like:
                # 睡觉 / 睡眠环内条目：保留原时长与跨午夜写法（end 可 >1440）
                end = start + duration
            else:
                end = min(start + duration, awake_end_bound)
            # A-16 日期帧标记（裁决① 2026-10-11）：夜猫子作息（sleep<=wake）下 start 落在
            # [0, sleep_min) 的清醒条目属于「今晚午夜之后」（下一个午夜），显式标 day_offset=1；
            # 其余条目标 0（当天钟点）。读取侧据此消解帧首双关；旧行缺省 NULL = 旧环口径。
            day_offset = 1 if (cross and not sleep_like and start < sleep_min) else 0
            entry = {
                "name": str(item.get("name") or "未命名活动"),
                "description": str(item.get("description") or ""),
                "goal_type": str(item.get("goal_type") or "daily_routine"),
                "priority": str(item.get("priority") or "medium"),
                "start_minute": start,
                "end_minute": end,
                "day_offset": day_offset,
                "parameters": dict(item.get("parameters") or {}),
                "schedule_date": date,
            }
            tone = clip_emotional_tone(raw_tone)
            if tone:
                entry["parameters"]["emotional_tone"] = tone
            else:
                entry["parameters"].pop("emotional_tone", None)
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
