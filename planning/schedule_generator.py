"""日程生成器（spine 唯一路线版）。

2026-10-25 滚动化改造引入（R7 依据 = 用户 2026-10-25 直派，设计目录
D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）的
spine 稀疏日意图分支现为唯一生成路线：每天只生成 3-6 条稀疏日意图（+ 硬约定 +
作息锚点），意图按【spine 意图存储契约】写 ``schedule_items``（``parameters`` 附加
``spine/min_minutes/max_minutes/priority/hardness/interruptibility/note`` 键；
窗口 start/end=window 起止分钟，跨午夜 end 允许 >1440，空档合法、不要求覆盖全天）；
进账走 ``_replace_with_entry``（打戳链路一字不动，reason=scheduled_generate）；
LLM 失败回退模板 spine（锚定 [schedule] 作息锚点，禁硬编码时刻），不重试多轮。

PR8 3.0.0 清退（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP4）：
旧 daily 全天生成链（多轮生成 / 质量评分 / 语义校验 / 模板日表兜底 /
``_normalize_items`` 归一化 / ``_ensure_recovery_item`` 低迷软约束）整链删除，
``generate_daily_schedule`` 恒走 spine（``mode`` 形参仅为调用方兼容保留）。

兼容契约（plugin.py / tools_service.py 等既有调用方依赖，只增不改）：
- 构造 ``ScheduleGenerator(db, llm)``；
- ``generate_daily_schedule(date, *, personality, lover_name, wake_time, sleep_time,
  min_activities, max_activities, timeout_seconds) -> list[dict]``；
- 未来约定（pending_commitment）与日程项的最接近时间窗匹配（apply_schedule_items）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.llm_utils import LLMService, over_limit_trim_enabled as _over_limit_trim
from core.time_utils import parse_hhmm
from suite_prompts import (
    SCHEDULE_HOLIDAY_CONTEXT_DEFAULT,
    prompt as prompt_of,
    render,
)
from .exceptions import LLMError
from .generator import BaseScheduleGenerator, ScheduleGeneratorConfig
from .goal_manager import GoalManager, get_goal_manager
from .cycle_state import (REASON_FALLBACK, REASON_REGENERATE, REASON_SCHEDULED,
                          enter_schedule_cycle, entry_context_from_plugin)  # §31.2 进账漏斗；R7 2026-10-23
from .schedule_db import ScheduleDB
from .schedule_items import is_night_sleep_item
from .time_utils import DAY_MINUTES, ring_minutes_ahead

logger = logging.getLogger(__name__)

# 作息解析默认值（配置留空/非法时兜底）：07:00 起床，23:00 入睡
_DEFAULT_WAKE_MINUTES = 7 * 60
_DEFAULT_SLEEP_MINUTES = 23 * 60


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


def _canonical_generation_goal_type(name: Any, goal_type: Any) -> str:
    """新生成条目的规范 goal_type（A-21(f)，用户 2026-10-27 批）。

    夜间睡眠条目统一写 ``sleep``：兼容 LLM 受旧提示词影响仍返回
    ``daily_routine`` 的情况；午休/午睡等名称不是夜间词，不会被误改。
    旧数据不做迁移，读取侧由 :func:`planning.schedule_items.is_night_sleep_item` 兼容。
    """
    value = str(goal_type or "").strip() or "daily_routine"
    if value != "sleep" and is_night_sleep_item({"name": str(name or ""),
                                                 "goal_type": value}):
        return "sleep"
    return value


class ScheduleGenerator:
    """日程生成器（spine 唯一路线：配置 → 上下文 → 稀疏日意图生成 → 落库）。"""

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
        mode: str = "spine",
    ) -> List[Dict[str, Any]]:
        """生成并落库某天日程（3.0.0 起恒走 spine 稀疏日意图分支）。

        Args:
            date: 目标日期（``YYYY-MM-DD``；提示词日期取当天，日程落库按此键）。
            apply: True=落库并做约定匹配/消费；False=仅生成（仍写入日程表以便查询）。
            reason: 进账原因（§31.2 血缘，R7 = 用户 2026-10-23 第二轮直派）：例行生成
                缺省 ``scheduled_generate``；regenerate 指令链透传 ``regenerate``；
                LLM 失败走模板 spine 兜底时改 ``fallback_template``（仅对例行生成路径，
                regenerate 调用点语义优先——原因按调用点区分进记录，见 notes）。
            mode: **兼容形参**（PR8 3.0.0 清退旧 daily 生成链，依据 = 用户
                2026-10-27 授权直接清理 + 逐符号审定清单 WP4；``mode="daily"`` 旧路径
                已整链删除，按裁定 1 不切回旧路线）。取值不再分流，恒走 spine——
                保留形参只为 tools_service/auto_scheduler 既有调用点零破坏。

        其余关键字参数沿用旧契约签名（调用方 ``tools_service._generate_kwargs`` /
        ``auto_scheduler`` 照常透传）；spine 分支只消费其中相关子集，
        见 :meth:`_generate_spine_schedule`。
        """
        return await self._generate_spine_schedule(
            date,
            personality=personality, lover_name=lover_name,
            wake_time=wake_time, sleep_time=sleep_time,
            timeout_seconds=timeout_seconds, custom_prompt=custom_prompt,
            timezone=timezone, llm_task_name=llm_task_name,
            temperature=temperature, bot_profile=bot_profile,
            no_sleep_mode=no_sleep_mode, apply=apply, reason=reason,
        )

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
                # 用户 2026-10-27 最终裁定：明确 time 的 hard 约定**原时刻固定**——
                # 即使 LLM/近窗匹配把条目放在 09:50，也拉回 pending 的 10:00；soft 仍可窗口调度。
                if (meta.get("commitment_hardness") == "hard"
                        and str(meta.get("commitment_time") or "").strip()):
                    commit_minute = _parse_minutes(str(meta["commitment_time"]), -1)
                    if commit_minute >= 0:
                        try:
                            old_start = int(items[idx].get("start_minute") or commit_minute)
                            old_end = int(items[idx].get("end_minute") or (commit_minute + 60))
                        except (TypeError, ValueError):
                            old_start, old_end = commit_minute, commit_minute + 60
                        duration = max(15, old_end - old_start)
                        items[idx]["start_minute"] = commit_minute
                        items[idx]["end_minute"] = commit_minute + duration
                        if items[idx].get("time_slot"):
                            items[idx]["time_slot"] = f"{commit_minute // 60:02d}:{commit_minute % 60:02d}"
                        params["commitment_time_fixed"] = commit_minute
                        if old_start != commit_minute:
                            params["commitment_time_shifted_from"] = old_start
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

        max_intentions = _int("max_intentions", 6, 1)
        min_intentions = _int("min_intentions", 3, 1)
        if min_intentions > max_intentions:
            min_intentions = max_intentions
        return {
            "min_intentions": min_intentions,
            "max_intentions": max_intentions,
            "max_described": _int("max_described_intents", 1, 0),
            "recent_days": _int("spine_recent_days", 2, 0),
            "history_messages": _int("spine_history_messages", 12, 0),
            "knowledge_limit": _int("spine_knowledge_limit", 3, 0),

            "use_multi_round": bool(getattr(cfg, "intention_multi_round", False)),
            "over_limit_auto_trim": _over_limit_trim(self._plugin),
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
            max_description_length=25,
            custom_prompt=custom_prompt, recent_schedule_days=params["recent_days"],
            history_message_limit=params["history_messages"],
            knowledge_search_limit=params["knowledge_limit"],
            cross_day_activity=True, no_sleep_mode=no_sleep_mode, timezone=timezone,
            llm_task_name=llm_task_name, temperature=temperature, bot_profile=bot_profile,
        ))

        # 上下文装载（与旧 daily 同一批槽位；情绪块不注入——其措辞含「无缝衔接要求」，与稀疏口径冲突）
        base = BaseScheduleGenerator(self.goal_manager, gen_config.to_dict())
        self._load_sync_contexts(base, gen_config, date)
        self._load_low_mood(base)
        self._load_experience_context(base)
        self._load_motive_context(base)
        self._load_preference_context(base)
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
        # intention_multi_round=true 时最多 2 次同提示重试（结构校验：拿到可用意图即收），
        # 不带反馈重试、不跑质量评分（设计 §4.2：单轮、结构校验、不强制重试）
        for _ in range(2 if params["use_multi_round"] else 1):
            try:
                response = await self._call_llm(prompt, gen_config, event="schedule_spine")
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
                                                   max_intentions=params["max_intentions"],
                                                   no_sleep_mode=no_sleep_mode)
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
        # P3（2026-11-09 §14.4）：日意图成功更新 → 合并安排偏好增量批次（fire-and-
        # forget，非阻塞；受每日 ≤4/退避/忙闲门控；普通滚动 tick 不走此入口）。
        refresher = getattr(self._plugin, "schedule_preference_refresh", None)
        if callable(refresher):
            try:
                refresher()
            except Exception:
                logger.debug("日程偏好增量刷新触发失败（忽略）", exc_info=True)
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
        max_int = int(params.get("max_intentions", 6))
        if params.get("over_limit_auto_trim", True):
            # 保尾裁剪：先按时间排序，超量时保留最晚的 N 条，晚间计划不被裁掉
            # （避免「晚上永不出现计划」）；睡眠锚点若被裁掉由后续护栏补回。
            rows.sort(key=lambda it: int(it["start_minute"]))
            if len(rows) > max_int:
                rows = rows[-max_int:]
        else:
            if len(rows) > max_int:
                rows = rows[:max_int]
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
        goal_type = _canonical_generation_goal_type(name, raw.get("goal_type"))
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
        no_sleep_mode: bool = False,
    ) -> List[Dict[str, Any]]:
        """锚点护栏：睡觉意图缺失时补一条模板睡觉意图（hardness=hard，时间固定）。

        补入后若超出 ``max_intentions`` 上限，丢掉排序最靠后的软意图给硬锚点让位
        （3~6 条契约含睡觉锚点；hard 锚点优先级高于 LLM 软意图）。
        免睡模式（no_sleep_mode）下不补睡眠意图，与模板兜底路径同口径（P1 一致性）。
        """
        if no_sleep_mode:
            return rows
        for row in rows:
            if is_night_sleep_item(row):
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
            "goal_type": _canonical_generation_goal_type(name, goal_type),
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
        三分点、晚餐锚入睡前3~5小时）；跨午夜作息（sleep <= wake）的清醒跨度经
        :func:`ring_minutes_ahead` 环距计算，不手写 −1440。
        """
        wake_min = parse_hhmm(wake_time, _DEFAULT_WAKE_MINUTES)
        sleep_min = parse_hhmm(sleep_time, _DEFAULT_SLEEP_MINUTES)
        # 清醒跨度（环距；常规作息 = sleep−wake，跨午夜 = wake−sleep+1440；退化 = 全天）
        awake_span = ring_minutes_ahead(sleep_min, wake_min) or DAY_MINUTES
        awake_end = wake_min + awake_span  # = sleep_min（常规）或 sleep_min+1440（跨午夜）

        def row(name: str, start: int, end: int, goal_type: str, priority: str,
                min_minutes: int, max_minutes: int, interruptibility: float,
                hardness: str = "soft") -> Dict[str, Any]:
            return ScheduleGenerator._spine_row(
                date, name, start, end, goal_type, priority,
                min_minutes, max_minutes, interruptibility=interruptibility, hardness=hardness)

        rows: List[Dict[str, Any]] = []
        if not no_sleep_mode:
            span = max(180, min(720, ring_minutes_ahead(wake_min, sleep_min)))
            # 睡眠锚点分类与正常生成/缺项补齐统一为 hard（P1）；显示类别由 WebUI
            # 按 goal_type 独立标"作息参考"，不随生成路径成败改变（P2/P3）。
            rows.append(row("睡觉", sleep_min, sleep_min + span, "sleep", "high",
                            span, span, 0.05, hardness="hard"))
        rows.append(row("起床洗漱", wake_min, wake_min + 30, "daily_routine", "medium", 15, 30, 0.9))
        rows.append(row("早餐", wake_min, wake_min + 120, "meal", "medium", 15, 40, 0.9))
        mid = wake_min + awake_span // 3
        rows.append(row("午餐", max(wake_min, mid - 90), min(awake_end, mid + 90),
                        "meal", "medium", 20, 45, 0.9))
        if awake_span >= 480:
            rows.append(row("自由安排", wake_min + 150, awake_end - 330,
                            "free_time", "low", 30, 90, 0.95))
        if awake_span >= 360:
            rows.append(row("晚餐", awake_end - 300, awake_end - 180,
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
            "max_description_length": "max_description_length",
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
                    # 显式 hardness 贯通：soft 保持 soft；旧无 hardness 按约定旧口径 hard
                    "hardness": (
                        "hard" if str((p.parameters or {}).get("hardness") or "").lower() != "soft"
                        else "soft"
                    ),
                    "source": str((p.parameters or {}).get("hard_source")
                                  or (p.parameters or {}).get("source") or "commitment"),
                    "commitment_id": str(getattr(p, "goal_id", "") or ""),
                }
                for p in pending
            ] or None
            if pending:
                logger.info("今日有 %d 条未来约定将被纳入日程生成", len(pending))
        except Exception as exc:
            logger.debug("今日约定加载失败: %s", exc)

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

    def _load_experience_context(self, base: BaseScheduleGenerator) -> None:
        """PR-B1（2026-11-07 经历索引消费点）：经插件公开只读端口取近期真实经历
        摘要行（watched_video/activity_done，≤5 条）。端口缺失/开关关闭/失败/为空
        → 空串不注入（空降级，旧行为逐字不变）；不改兴趣权重/动机逻辑（B2/C2）。"""
        base.experience_context = ""
        try:
            getter = getattr(self._plugin, "experience_context_lines", None)
            if not callable(getter):
                return
            lines = getter(limit=5) or []
            base.experience_context = "\n".join(str(x) for x in list(lines)[:5])
        except Exception as exc:
            base.experience_context = ""
            logger.debug("近期经历上下文装载失败（忽略）: %s", exc)

    def _load_motive_context(self, base: BaseScheduleGenerator) -> None:
        """PR-B2（2026-11-07 持续动机消费点，规格 §2）：经插件公开只读端口取
        ≤4 条动机摘要行注入 spine 提示词变量区（稳定规则前置不变）。端口缺失/
        开关关闭/失败/为空 → 空串不注入（空降级，旧行为逐字不变）；动机只作
        倾向参考，不构成必须执行的任务，不穿透睡眠/硬约定/注意力/预算。"""
        base.motive_context = ""
        try:
            getter = getattr(self._plugin, "motive_context_lines", None)
            if not callable(getter):
                return
            lines = getter(limit=4) or []
            base.motive_context = "\n".join(str(x) for x in list(lines)[:4])
        except Exception as exc:
            base.motive_context = ""
            logger.debug("近期动机上下文装载失败（忽略）: %s", exc)

    def _load_preference_context(self, base: BaseScheduleGenerator) -> None:
        """P3（2026-11-09 §14.8）：经插件公开只读端口取稳定口味摘要注入 spine
        变量区。纯读缓存——不触发评估请求；端口缺失/关闭/失败/为空 → 空串不注入
        （空降级，旧行为逐字不变）。复用现有生成请求，不另跑请求。"""
        base.preference_context = ""
        try:
            getter = getattr(self._plugin, "preference_context_summary", None)
            if not callable(getter):
                return
            base.preference_context = str(getter() or "")
        except Exception as exc:
            base.preference_context = ""
            logger.debug("稳定口味上下文装载失败（忽略）: %s", exc)

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

    async def _call_llm(
        self,
        prompt: str,
        gen_config: ScheduleGeneratorConfig,
        *,
        event: str = "schedule_spine",
    ) -> str:
        """调用 LLM（走 core.llm_utils.LLMService，带任务路由 + 调用日志）。

        ``event`` = 消耗账本（core/llm_usage.py）的调用点标签：spine 分支传
        ``schedule_spine``。PR8 3.0.0 清退旧 daily 生成链后本方法只剩 spine 唯一
        调用点（依据 = 用户 2026-10-27 授权直接清理），恒返回原始文本——解析由
        spine 自包含的 :meth:`_parse_spine_response` 承担（旧 ``parse`` 参数与
        ``_parse_response`` 已随 daily 链删除）。人设经 ``with_identity=True``
        以宿主同款开头注入 system_prompt（共享前缀缓存），用户提示词不再带人设。
        """
        task_name = gen_config.llm_task_name
        temperature = float(self._raw_of(gen_config).get("temperature", 0.85))
        timeout_ms = int(float(gen_config.generation_timeout) * 1000)

        response = await asyncio.wait_for(
            self._llm.generate(
                prompt,
                task_name=task_name,
                temperature=temperature,

                timeout_ms=timeout_ms,
                module="planning",
                event=event,
                with_identity=True,
            ),
            timeout=float(gen_config.generation_timeout) + 15.0,
        )
        if not response:
            raise LLMError("LLM 调用失败（空响应）")
        return response

    # ============================================================
    # 约定匹配
    # ============================================================

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
            # 2026-10-27 用户裁定：显式 hardness 贯通到生成->DB/Planner；
            # soft 不得被 LLM/默认升级为 hard，hard 不得降级；旧无 hardness 约定兼容 hard。
            raw_hardness = str(params.get("hardness") or "").strip().lower()
            commit_hardness = "soft" if raw_hardness == "soft" else "hard"
            commit_source = str(params.get("hard_source") or params.get("source") or "commitment")
            commit_id = str(getattr(commit, "goal_id", "") or "")

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
                "commitment_hardness": commit_hardness,
                "commitment_source": commit_source,
                "commitment_id": commit_id,
                # 显式覆盖生成侧 hardness（soft 不硬化 / hard 不降级）
                "hardness": commit_hardness,
            }
            used_item_idx.add(best_idx)
            logger.info("约定 %r → 日程项[%d] %r", commit_title, best_idx, items[best_idx].get("name"))

        return result
