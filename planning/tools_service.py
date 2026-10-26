"""日程信息查询（多视图只读）/ 角色裁判式日程更新 / 日程重生成的工具业务实现
（来源：自主规划插件 services/tools_service.py，移植版）。

通过 ``self._plugin.config`` 强类型访问插件配置，通过 ``self._plugin.ctx``
访问 SDK 能力代理；LLM 调用经宿主 ``ctx.llm.generate``（角色裁判）或
core.llm_utils.LLMService（日程生成）。

T6 注册面收口后本服务服务两个 Tool：``get_schedule_info``（只读多视图：
status/current/next/episodes/spine/intents）与 ``update_schedule``（唯一日程写入口，
role_judge 裁判路径；规范工具名 ``lbm_update_schedule``）。目标的写 API 仍在
GoalManager（生成流水线与约定消费在用），只是不再有 Tool 注册入口。

PR8 3.0.0 清退（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）：旧别名
``lbm_update_schedule_v4``、旧视图 ``lover_day``/``goals`` 与 M9 赖床工具
``nap_lie_in`` 整链（含提醒链/extend 链）已删除；``[schedule].enabled`` 是唯一
日程总开关，spine 为唯一生成路线。
"""

from __future__ import annotations

import inspect
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.time_utils import parse_hhmm

from core.llm_utils import llm_task_name_of

from .config import (
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
    section_or_default,
)
from .activity_properties import resolve_property
from .cycle_state import REASON_TOOL_EDIT  # §31.2 进账血缘；R7 2026-10-23
from .goal_manager import get_goal_manager
from .role_judge import judge_schedule_request
from .rolling_planner import extract_spine_intents
from .schedule_generator import ScheduleGenerator
from .time_utils import (DAY_MINUTES, TimezoneManager, format_day_minute,
                         is_in_day_frame_marked, is_upcoming_day_minute_marked,
                         ring_minutes_ahead)

logger = logging.getLogger(__name__)


class ToolsService:
    """承载 2 个 LLM Tool 的业务逻辑（get_schedule_info / update_schedule）。"""

    def __init__(self, plugin: Any, generator: Optional[ScheduleGenerator] = None) -> None:
        """
        Args:
            plugin: 当前插件实例（用于访问 ``ctx`` 与 ``config``）
            generator: 完整版 ScheduleGenerator（PlanningServices 注入；
                缺省回退 plugin._schedule_gen）
        """
        self._plugin = plugin
        self._generator = generator

    # ------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------

    def _schedule_cfg(self) -> ScheduleSectionConfig:
        """日程基础段（`[schedule]`：作息 / 活动数量 / 注入范围 / 节假日开关）。"""
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig)

    def _advanced_cfg(self) -> ScheduleAdvancedSectionConfig:
        """日程高级段（`[schedule_advanced]`：多轮生成 / 裁判 / 缓存 / 赖床 / 时区等）。"""
        return section_or_default(self._plugin, "schedule_advanced", ScheduleAdvancedSectionConfig)

    def _make_tz_manager(self) -> TimezoneManager:
        """根据配置创建 TimezoneManager（时区在高级段）。"""
        return TimezoneManager(self._advanced_cfg().timezone)

    def _get_generator(self) -> Optional[ScheduleGenerator]:
        generator = self._generator or getattr(self._plugin, "_schedule_gen", None)
        return generator if isinstance(generator, ScheduleGenerator) else None

    def _schedule_query(self) -> Optional[Any]:
        """本插件的日程查询实例（plugin._schedule_query，带 15min 缓存）。

        鸭子类型判定而非 isinstance：离线测试宿主与整合前的骨架插件都用同名实例，
        只要提供 get_day_schedule / find_current_activity 即可用（缺省返回 None，
        四个视图各自给降级话术）。
        """
        query = getattr(self._plugin, "_schedule_query", None)
        if query is None or not callable(getattr(query, "get_day_schedule", None)):
            return None
        return query

    def _generate_kwargs(self) -> Dict[str, Any]:
        """从 [schedule] + [schedule_advanced] 两段产出 generate_daily_schedule 的完整关键字参数。"""
        cfg = self._schedule_cfg()
        adv = self._advanced_cfg()
        return {
            "personality": str(getattr(self._plugin, "_personality", "") or ""),
            "lover_name": str(getattr(self._plugin, "_lover_name", "") or "麦麦"),
            "wake_time": cfg.wake_time,
            "sleep_time": cfg.sleep_time,
            "min_activities": cfg.min_activities,
            "max_activities": cfg.max_activities,
            "timeout_seconds": adv.generation_timeout,
            "use_multi_round": adv.use_multi_round,
            "max_rounds": adv.max_rounds,
            "quality_threshold": adv.quality_threshold,
            "enable_detailed_description": cfg.enable_detailed_description,
            "min_description_length": adv.min_description_length,
            "max_description_length": adv.max_description_length,
            "max_tokens": adv.max_tokens,
            "custom_prompt": cfg.custom_prompt,
            "recent_schedule_days": adv.recent_schedule_days,
            "history_message_limit": adv.history_message_limit,
            "knowledge_search_limit": adv.knowledge_search_limit,
            "cross_day_activity": adv.cross_day_activity,
            "no_sleep_mode": cfg.no_sleep_mode,
            # PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）：spine 为唯一
            # 生成路线，mode 恒 "spine"（原 [schedule].spine_mode 三元随退役键删除）；
            # regenerate 链（/lbm_plan regenerate 与 update_schedule 的「今天」分支）与
            # auto_scheduler 自动生成侧同口径；generator 侧 **kwargs 透传，零改动。
            "mode": "spine",
            "timezone": adv.timezone,
            "llm_task_name": llm_task_name_of(self._plugin),
        }

    # ------------------------------------------------------------
    # 对外暴露：供 CommandService 直接复用的重生成入口
    # ------------------------------------------------------------

    def _module_enabled(self) -> bool:
        """模块独立总开关：config.plugin.enabled 且 config.schedule.enabled。"""
        plugin_section = getattr(self._plugin.config, "plugin", None)
        cfg = getattr(self._plugin.config, "schedule", None)
        return bool(getattr(plugin_section, "enabled", True)) and bool(getattr(cfg, "enabled", True))

    async def regenerate_today_schedule_now(self, *, extra_prompt: str = "") -> List[Dict[str, Any]]:
        if not self._module_enabled():
            logger.warning("日程规划模块已停用，跳过重生成请求")
            return []
        """立即重新生成今日日程（绕过 LLM 工具链，供 /plan regenerate 调用）。

        Args:
            extra_prompt: 临时追加到 ``custom_prompt`` 的额外要求（可选），
                例如「加入下午跑步」「今天是生日」。

        Returns:
            新生成的日程条目列表（已写入数据库）。

        Raises:
            RuntimeError: 生成器不可用。
        """
        generator = self._get_generator()
        if generator is None:
            raise RuntimeError("ScheduleGenerator 不可用（插件未完成初始化）")
        items = await generator.regenerate_today_schedule(
            extra_prompt=extra_prompt, apply=True, **self._generate_kwargs(),
        )
        # 命令侧日程修改同样提示内部循环（可选口；缺省静默）
        await self._request_life_recheck("schedule_regenerated")
        return items

    # ------------------------------------------------------------
    # Tool 1：get_schedule_info（四视图只读）
    # ------------------------------------------------------------

    #: view 取值（缺省/非法一律回落 status）。
    #: v2（定稿 §4.6 / 附录 A.3.1）：status/current/next/episodes/spine/intents
    #: （PR8 3.0.0 删旧视图 lover_day/goals，依据：用户 2026-10-27 授权直接清理）。
    _SCHEDULE_INFO_VIEWS = ("status", "current", "next", "episodes", "spine", "intents")
    #: 新层（runtime）视图：新层未启用时明确回报"未启用"，不回退成时间窗编造
    _RUNTIME_VIEWS = ("next", "episodes", "spine", "intents")
    #: ``intents`` = ``spine`` 的别名（附录 A.3.1「别名，便于模型理解」）
    _VIEW_ALIASES = {"intents": "spine"}

    async def get_schedule_info(self, view: str = "", **function_args: Any) -> Dict[str, Any]:
        """日程/活动/目标的只读查询（v2）。

        view = ``status`` | ``current`` | ``next`` | ``episodes`` | ``spine`` |
        ``intents``（= spine 别名）。

        **全部视图只读**：不触碰生成/落库/目标写 API，不调用 tick、不掷点、不写状态
        文件。``next`` / ``episodes`` / ``spine`` / ``intents`` 依赖 runtime（episode 日志），
        新层未启用时明确回答"未启用"（不是空成功、也不是编造）。
        """
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        del function_args
        view = str(view or "").strip().lower()
        if view not in self._SCHEDULE_INFO_VIEWS:
            view = "status"  # 缺省与非法值同档：回落状态总览
        view = self._VIEW_ALIASES.get(view, view)
        try:
            if view == "current":
                return self._view_current_v2()
            if view == "next":
                return self._view_next()
            if view == "episodes":
                return self._view_episodes()
            if view == "spine":
                return self._view_spine()
            return self._view_status_v2()
        except Exception as exc:
            logger.error(f"获取日程信息失败（view={view}）: {exc}", exc_info=True)
            return {"type": "error", "content": f"获取日程信息失败: {exc}"}

    # ── runtime（episode 日志）只读句柄 ──────────────────────────────────
    def _runtime_provider(self) -> Optional[Any]:
        """``LifeScheduleReadModel``（``ScheduleQuery._runtime_provider``；未装配 → None）。"""
        query = self._schedule_query()
        provider = getattr(query, "_runtime_provider", None) if query is not None else None
        return provider

    def _runtime_ready(self) -> bool:
        """runtime 是否就绪（= provider 存在且 ``[schedule].enabled``）。"""
        try:
            from .schedule_query import ScheduleQuery as _ScheduleQuery
            return bool(_ScheduleQuery._runtime_ready(self._runtime_provider()))
        except Exception:
            return False

    def _runtime_now(self) -> datetime:
        return self._make_tz_manager().get_now()

    @staticmethod
    def _runtime_item_line(item: Dict[str, Any], *, cur_minute: Optional[int] = None,
                           prefix: str = "") -> str:
        """episode/意图行 → 一行可读文本（来源/预演/暂停标记如实标注）。"""
        name = str(item.get("name") or "未知活动")
        start = item.get("start_minute")
        end = item.get("end_minute")
        try:
            window = f"{format_day_minute(int(start or 0))}-{format_day_minute(int(end or 0))}"
        except (TypeError, ValueError):
            window = ""
        tags: List[str] = []
        if bool(item.get("is_preview")):
            tags.append("预演")
        if bool(item.get("is_spine_intent")):
            tags.append("意图")
        if bool(item.get("paused")):
            tags.append("暂停待恢复")
        source = str(item.get("source") or "").strip()
        if source:
            tags.append(f"来源={source}")
        status = str(item.get("status") or "").strip()
        if status:
            tags.append(f"状态={status}")
        attention = str(item.get("attention_mode") or "").strip()
        if attention:
            tags.append(f"注意力={attention}")
        progress = item.get("progress")
        if isinstance(progress, (int, float)) and not isinstance(progress, bool):
            tags.append(f"进度={int(round(max(0.0, min(1.0, float(progress))) * 100))}%")
        if cur_minute is not None:
            remaining = ToolsService._remaining_minutes(item, cur_minute)
            if remaining is not None:
                tags.append(f"剩余={remaining}分钟")
        head = f"{prefix}{window} {name}".strip()
        return f"{head}（{'｜'.join(tags)}）" if tags else head

    @staticmethod
    def _remaining_minutes(item: Dict[str, Any], cur_minute: int) -> Optional[int]:
        """剩余分钟数（24h 环口径；已过 planned_end 的 active 行按 0 报，不编造负数）。"""
        try:
            end = int(item.get("end_minute") or 0)
            start = int(item.get("start_minute") or 0)
        except (TypeError, ValueError):
            return None
        if end <= start:
            return None
        if str(item.get("status") or "") == "active" and end <= cur_minute:
            return 0
        try:
            return int(ring_minutes_ahead(end, cur_minute))
        except Exception:
            return None

    def _runtime_current_item(self) -> Optional[Dict[str, Any]]:
        """runtime 当前 episode（**不**回退时间窗：空闲即 None）。"""
        provider = self._runtime_provider()
        if not self._runtime_ready():
            return None
        try:
            current = provider.current(self._runtime_now(), include_sleep=False)
        except Exception as exc:
            logger.debug(f"runtime current 读取失败: {exc}")
            return None
        return current if isinstance(current, dict) else None

    def _runtime_next_items(self, limit: int = 2) -> Optional[List[Dict[str, Any]]]:
        """runtime next（只读预演/意图标记原样带出）；未就绪 → None（区分"未启用"与"空"）。"""
        if not self._runtime_ready():
            return None
        provider = self._runtime_provider()
        try:
            items = provider.next(self._runtime_now(), limit=max(1, int(limit)))
        except Exception as exc:
            logger.debug(f"runtime next 读取失败: {exc}")
            return []
        return [it for it in (items or []) if isinstance(it, dict)]

    def _runtime_today(self) -> Optional[Dict[str, Any]]:
        """runtime 当日汇总（intents/episodes/summary）；未就绪 → None。"""
        if not self._runtime_ready():
            return None
        provider = self._runtime_provider()
        today = getattr(provider, "today", None)
        if not callable(today):
            return {"date": "", "intents": [], "episodes": [], "summary": {}}
        try:
            data = today(self._runtime_now())
        except Exception as exc:
            logger.debug(f"runtime today 读取失败: {exc}")
            return {"date": "", "intents": [], "episodes": [], "summary": {}}
        return data if isinstance(data, dict) else {"date": "", "intents": [],
                                                   "episodes": [], "summary": {}}

    def list_runtime_lines(self, limit: int = 2) -> List[str]:
        """``/lbm_plan list`` 的 runtime 只读补充行（供命令层直接追加到既有文字档）。

        纯读、零写、零 LLM、零发送：runtime 未就绪 → 空列表（命令层保持旧输出）。
        命令层接入方式（后续统一接线）：``text += "\\n\\n" + "\\n".join(
        tools.list_runtime_lines())``；图不重绘（复用既有渲染）。
        """
        if not self._runtime_ready():
            return []
        now = self._runtime_now()
        cur_min = now.hour * 60 + now.minute
        lines: List[str] = ["运行态（episode 日志）"]
        current = self._runtime_current_item()
        if current is not None:
            lines.append(self._runtime_item_line(current, cur_minute=cur_min, prefix="当前："))
        else:
            lines.append("当前：没有正在进行的 episode")
        for item in (self._runtime_next_items(limit) or []):
            lines.append(self._runtime_item_line(item, cur_minute=cur_min, prefix="接下来："))
        return lines

    def _spine_intents(self) -> List[Dict[str, Any]]:
        """今日 spine 意图（新层未启用时读当天 schedule_items 的 ``parameters.spine`` 行）。

        纯读：不触发任何写（生成、落库、消费约定都不调）。
        """
        date = self._runtime_now().strftime("%Y-%m-%d")
        rows: List[Dict[str, Any]] = []
        try:
            runtime_today = self._runtime_today()
            if runtime_today is not None and runtime_today.get("intents"):
                rows = [it for it in runtime_today["intents"] if isinstance(it, dict)]
            else:
                query = self._schedule_query()
                day_rows = query.get_day_schedule(date) if query is not None else []
                rows = extract_spine_intents(day_rows)
        except Exception as exc:
            logger.debug(f"spine 意图读取失败: {exc}")
            rows = []
        return rows

    def _view_status_v2(self) -> Dict[str, Any]:
        """view=status：runtime 就绪 → 意图/episode/当前/接下来/状态行；否则旧总览逐字保留。"""
        if not self._runtime_ready():
            return self._view_status()
        now = self._runtime_now()
        cur_min = now.hour * 60 + now.minute
        today = self._runtime_today() or {}
        summary = today.get("summary") if isinstance(today.get("summary"), dict) else {}
        current = self._runtime_current_item()
        next_items = self._runtime_next_items(2) or []
        state_line = ""
        provider = self._runtime_provider()
        try:
            if callable(getattr(provider, "state_line", None)):
                state_line = str(provider.state_line(now) or "").strip()
        except Exception:
            state_line = ""

        date_str = str(today.get("date") or now.strftime("%Y-%m-%d"))
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()]
        lines = [
            f"今日日程 {date_str} {weekday}",
            f"意图 {int(summary.get('intent_count') or 0)} 条"
            f"｜episode {int(summary.get('episode_count') or 0)} 条"
            f"（完成 {int(summary.get('completed_count') or 0)}"
            f"｜打断 {int(summary.get('interrupted_count') or 0)}"
            f"｜放弃 {int(summary.get('abandoned_count') or 0)}）",
        ]
        if current is not None:
            lines.append(self._runtime_item_line(current, cur_minute=cur_min, prefix="当前："))
        else:
            lines.append("当前：没有正在进行的 episode（空闲）")
        if next_items:
            lines.append("接下来：")
            for item in next_items:
                lines.append("  " + self._runtime_item_line(item, cur_minute=cur_min))
        else:
            lines.append("接下来：暂无已排活动")
        if state_line:
            lines.append(state_line)
        return {"type": "schedule_info", "content": "\n".join(lines)}

    def _view_current_v2(self) -> Dict[str, Any]:
        """view=current：runtime 就绪 → 实际 episode（注意力/进度/剩余/来源）；否则旧路径。"""
        if not self._runtime_ready():
            return self._view_current()
        name = str(getattr(self._plugin, "_lover_name", "") or "麦麦")
        now = self._runtime_now()
        cur_min = now.hour * 60 + now.minute
        current = self._runtime_current_item()
        if current is None:
            return {"type": "schedule_current",
                    "content": f"{name}现在没有安排中的活动（可能在休息）。"}
        line = self._runtime_item_line(current, cur_minute=cur_min)
        return {"type": "schedule_current", "content": f"{name}现在正在：{line}。"}

    def _view_next(self) -> Dict[str, Any]:
        """view=next：接下来 2 条（含来源/预演/意图标记）；新层未启用 → 明确未启用。"""
        if not self._runtime_ready():
            return {"type": "schedule_next",
                    "content": "日程未启用（运行态未就绪）：暂不能读取接下来"
                               "的 episode/预演；可改用 view=status 查看状态总览。"}
        now = self._runtime_now()
        cur_min = now.hour * 60 + now.minute
        items = self._runtime_next_items(2) or []
        if not items:
            return {"type": "schedule_next", "content": "接下来没有已排的活动。"}
        lines = ["接下来："]
        for item in items:
            lines.append("  " + self._runtime_item_line(item, cur_minute=cur_min))
        return {"type": "schedule_next", "content": "\n".join(lines)}

    def _episode_rows(self) -> List[Dict[str, Any]]:
        """今日 episode 行（只读；runtime provider 库优先，回退 ``plugin._db``）。"""
        provider = self._runtime_provider()
        db = (getattr(provider, "_db", None) if provider is not None else None)
        if db is None or not callable(getattr(db, "episodes_today", None)):
            query = self._schedule_query()
            db = getattr(query, "db", None) if query is not None else None
        if db is None or not callable(getattr(db, "episodes_today", None)):
            db = getattr(self._plugin, "_db", None)
        if db is None or not callable(getattr(db, "episodes_today", None)):
            return []
        try:
            date = self._runtime_now().strftime("%Y-%m-%d")
            return [row for row in (db.episodes_today(date) or []) if isinstance(row, dict)]
        except Exception as exc:
            logger.debug(f"episode 读取失败: {exc}")
            return []

    def _view_episodes(self) -> Dict[str, Any]:
        """view=episodes：今日实际 episode（有历史就如实列出；新层未启用且无历史 → 明说）。"""
        rows = self._episode_rows()
        if not rows:
            if not self._runtime_ready():
                return {"type": "schedule_episodes",
                        "content": "今日没有 episode 记录（日程未启用："
                                   "运行态未就绪时不会记录实际 episode）。"}
            return {"type": "schedule_episodes", "content": "今日还没有实际 episode 记录。"}
        cur_min = self._runtime_now().hour * 60 + self._runtime_now().minute
        lines = [f"今日实际 episode（{len(rows)} 条）"]
        for row in rows:
            lines.append("  " + self._runtime_item_line(row, cur_minute=cur_min))
        return {"type": "schedule_episodes", "content": "\n".join(lines)}

    def _view_spine(self) -> Dict[str, Any]:
        """view=spine / intents：今日意图与硬约定（spine 存储契约只读投影）。"""
        intents = self._spine_intents()
        if not intents:
            if not self._runtime_ready():
                return {"type": "schedule_spine",
                        "content": "今日没有 spine 意图（日程未启用：运行态未就绪）。"}
            return {"type": "schedule_spine", "content": "今日没有 spine 意图。"}
        hard = [it for it in intents if str(it.get("hardness") or "") == "hard"]
        lines = [f"今日意图（{len(intents)} 条，其中硬约定 {len(hard)} 条）"]
        for it in intents:
            hardness = "硬约定" if str(it.get("hardness") or "") == "hard" else "软意图"
            window = ""
            try:
                window = (f"{format_day_minute(int(it.get('start_minute') or 0))}-"
                          f"{format_day_minute(int(it.get('end_minute') or 0))} ")
            except (TypeError, ValueError):
                window = ""
            lines.append(f"  {window}{it.get('name') or '未命名'}"
                         f"（{hardness}｜优先级={it.get('priority') or 'medium'}）")
            note = str(it.get("note") or "").strip()
            if note:
                lines.append(f"    {note}")
        return {"type": "schedule_spine", "content": "\n".join(lines)}

    def _view_status(self) -> Dict[str, Any]:
        """view=status：今日日程状态总览（正在进行 / 即将到来 / 已完成）。"""
        goal_manager = get_goal_manager()
        enable_detailed_description = self._schedule_cfg().enable_detailed_description

        # 统一获取今日日程目标（合成视图，含日期过滤）
        schedule_goals = goal_manager.get_schedule_goals(chat_id="global")
        if not schedule_goals:
            return {"type": "schedule_info", "content": "今天还没有日程"}

        tz_manager = self._make_tz_manager()
        now = tz_manager.get_now()
        current_minutes = now.hour * 60 + now.minute

        # 提取时间窗口并排序
        schedule_with_time: List[Tuple[Any, List[int]]] = []
        for goal in schedule_goals:
            time_window: Optional[List[int]] = None
            if goal.parameters and "time_window" in goal.parameters:
                time_window = goal.parameters["time_window"]
            elif goal.conditions and "time_window" in goal.conditions:
                time_window = goal.conditions["time_window"]

            if time_window and isinstance(time_window, list) and len(time_window) == 2:
                schedule_with_time.append((goal, time_window))

        schedule_with_time.sort(key=lambda x: x[1][0])

        # 分类：正在进行 / 即将到来 / 已完成（「正在进行」跨午夜 = 同一个 24h 环：
        # 凌晨 00:30 的「23:00 → 次日 07:00」条目归「正在进行」而非「即将到来」；
        # upcoming/completed 保持帧内单调比较——帧首条目的环上双关语义见 time_utils.is_upcoming_day_minute）
        ongoing: List[Tuple[Any, List[int]]] = []
        upcoming: List[Tuple[Any, List[int]]] = []
        completed: List[Tuple[Any, List[int]]] = []
        for goal, tw in schedule_with_time:
            start_min, end_min = tw
            # A-16 日期帧标记（裁决①）：合成 Goal 参数里带生成链写入的 day_offset（旧行/旧档 = None）
            goal_params = getattr(goal, "parameters", None)
            goal_offset = None
            if isinstance(goal_params, dict) and goal_params.get("day_offset") is not None:
                try:
                    goal_offset = int(goal_params["day_offset"])
                except (TypeError, ValueError):
                    goal_offset = None
            if is_in_day_frame_marked(start_min, end_min, current_minutes, goal_offset):
                ongoing.append((goal, tw))
            elif is_upcoming_day_minute_marked(start_min, current_minutes, end_min, goal_offset):
                upcoming.append((goal, tw))
            else:
                completed.append((goal, tw))

        def _fmt_item(goal: Any, tw: List[int], prefix: str = "") -> str:
            start_time = format_day_minute(tw[0])
            end_time = format_day_minute(tw[1])
            # 属性短标签（§24.3 任务C）：goal_type 精确命中 / 名称关键词 / 空闲兜底
            prop_tag = resolve_property(goal.goal_type, goal.name).reply_tag()
            tag_prefix = f"{prop_tag} " if prop_tag else ""
            desc = ""
            if enable_detailed_description and goal.description:
                desc = goal.description
                if len(desc) > 30:
                    desc = desc[:27] + "..."
            if desc:
                return f"{prefix}{start_time}-{end_time} {tag_prefix}{goal.name}\n   描述：{desc}"
            return f"{prefix}{start_time}-{end_time} {tag_prefix}{goal.name}"

        date_str = now.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[now.weekday()]

        content = f"今日日程 {date_str} {weekday}\n"

        if ongoing:
            content += "\n正在进行:\n"
            for goal, tw in ongoing:
                content += _fmt_item(goal, tw, "当前：") + "\n"

        if upcoming:
            content += "\n即将到来:\n"
            for goal, tw in upcoming[:5]:
                content += _fmt_item(goal, tw) + "\n"
            if len(upcoming) > 5:
                content += f"   ...还有 {len(upcoming) - 5} 个活动\n"

        if completed:
            content += "\n已完成:\n"
            for goal, tw in completed[-3:]:
                content += _fmt_item(goal, tw) + "\n"

        content += f"\n共 {len(schedule_goals)} 个活动"
        return {"type": "schedule_info", "content": content}

    def _view_current(self) -> Dict[str, Any]:
        """view=current 旧路径（仅 runtime 未就绪时回落；未就绪=日程未启用，如实回答）。

        PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，规则 6）：
        未启用/未就绪文案改「日程未启用」，不再用「可能在休息」冒充空闲或休息态。
        """
        name = str(getattr(self._plugin, "_lover_name", "") or "麦麦")
        query = self._schedule_query()
        if query is None or not callable(getattr(query, "find_current_activity", None)):
            return {"type": "schedule_current",
                    "content": f"{name}的日程未启用（日程服务还没准备好），暂时不清楚在做什么。"}
        now = datetime.now()
        current = query.find_current_activity(now.strftime("%Y-%m-%d"), now)
        if current is None:
            return {"type": "schedule_current",
                    "content": f"{name}现在没有安排中的活动（日程未启用）。"}
        return {"type": "schedule_current",
                "content": f"{name}现在正在：{query.format_activity_line(current, now)}。"}

    # ------------------------------------------------------------
    # Tool 2：update_schedule（唯一日程写入口，角色裁判式更新）
    # ------------------------------------------------------------

    # ------------------------------------------------------------
    # v2 写入辅助（定稿 §4.6 / 附录 A.3.2）
    # ------------------------------------------------------------

    #: 明确「承诺/绑定/截止」语义的短语：出现即硬（这是承诺本身，不需要时间词佐证）。
    #: 只收承诺动词/截止语，**不收**泛事件词（开会/会议/面试…请看
    #: :data:`HARD_EVENT_KEYWORDS`——它们必须有绑定证据才算硬）。
    HARD_COMMITMENT_KEYWORDS: Tuple[str, ...] = (
        "必须", "务必", "一定要", "一定得", "不得不", "约好", "说好", "约了", "约在",
        "答应", "讲好", "商定", "定好", "定了", "敲定", "签了", "deadline", "截止",
    )
    #: 事件词：**只出现事件词不硬化**（工单缺陷：任何提及会议词都变 hard）；
    #: 必须同时有明确时间/日期绑定证据（:data:`HARD_TIME_EVIDENCE_RE`）才算硬约定。
    HARD_EVENT_KEYWORDS: Tuple[str, ...] = (
        "开会", "会议", "面试", "考试", "体检", "挂号", "预约", "复诊", "看病",
        "航班", "高铁", "飞机", "火车", "签到", "答辩", "汇报", "述职", "婚礼",
        "手术", "开庭",
    )
    #: 否定/取消语境（出现在同一子句 → 事件词不硬化；绑定短语按前置 3 字否定判）
    _HARD_NEGATION_MARKERS: Tuple[str, ...] = (
        "不用", "不必", "不需要", "无需", "没有", "不", "没", "别", "取消", "免了",
        "不去", "不打算", "不想", "不参加", "推掉",
    )
    #: 模糊/未定语境（同一子句出现 → 事件词不硬化：「可能明天开会」「有空想去看病」）
    _HARD_HEDGE_MARKERS: Tuple[str, ...] = (
        "可能", "也许", "或许", "大概", "有空", "如果", "要是", "考虑", "打算",
        "想要", "想去", "看看", "看情况", "到时候", "再说", "随便", "要不要", "是不是",
    )
    #: 绑定证据：明确日期/星期/时刻（「明天上午10点开会」才算约定）
    HARD_TIME_EVIDENCE_RE = re.compile(
        r"(\d{1,2}\s*[点:：]|\d{1,2}\s*月|周[一二三四五六日天]|星期[一二三四五六日天]"
        r"|礼拜[一二三四五六日天]|今天|今晚|明天|明早|明晚|后天|大后天|下[周月]"
        r"|上午|下午|中午|晚上|早上|凌晨|整点|半点)")
    #: typed 参数里表示"这是硬约定"的取值（归一用；来源 = 工具调用方 LLM 的显式声明）
    _HARD_TYPED_VALUES: Tuple[str, ...] = ("hard", "commitment", "appointment", "reservation")
    #: 默认意图时长（分钟）与上限
    DEFAULT_INTENT_MINUTES = 60
    MAX_INTENT_MINUTES = 240

    def _layers(self) -> Dict[str, bool]:
        """新层开关快照（PR8 3.0.0：``[schedule].enabled`` 是唯一日程总开关，spine 恒路线）。

        原 ``[schedule].spine_mode`` / ``[life_state].enabled`` 双键判定随退役键删除
        （依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）。
        """
        life = bool(getattr(self._schedule_cfg(), "enabled", True))
        planning = getattr(self._plugin, "_planning", None)
        planner = (getattr(planning, "rolling_planner", None)
                   or getattr(self._plugin, "_rolling_planner", None))
        rolling = bool(life and planner is not None)
        return {"spine": True, "life": life, "rolling": rolling, "enabled": True}

    @classmethod
    def detect_hard_commitment(cls, description: Any, *,
                               intent: Any = None, commitment: Any = None
                               ) -> Tuple[bool, str]:
        """代码侧硬约定判定（返回 ``(hard, 来源标记)``）。

        **硬化证据只有两类**（2026-10-27 共享接线修补，修复「任何提及会议词都变 hard」）：

        1. ``typed`` 参数的**显式**硬值（``hardness``/``kind`` ∈
           :data:`_HARD_TYPED_VALUES`）——这是工具调用方 LLM 按用户原话给出的显式
           声明，属于"用户要求"的转述；**但角色裁判返回的 ``parsed["hardness"]``
           永不参与**（见 :meth:`update_schedule`），LLM 一句 hardness 不能单独翻转。
        2. 描述文本中的**明确绑定**：
           - 承诺/截止短语（必须/约好/截止…）且前面 3 字内没有否定词；
           - 事件词（开会/体检/看病…）**且**同一子句有日期/时刻绑定证据，且该子句
             既无否定（不用开会）也无模糊（可能/有空/想去）语境。

        因此「可能明天开会」「有空想去看病」「不用开会」都是软意图；「明天上午10点
        开会」「约好周六体检」「必须去开会」才是硬约定。词表全部是代码常量，LLM 既
        不能把软意图硬化，也不能把明确约定降级。
        """
        for holder in (intent, commitment):
            if not isinstance(holder, dict):
                continue
            raw = str(holder.get("hardness") or holder.get("kind") or "").strip().lower()
            if raw in cls._HARD_TYPED_VALUES:
                return True, f"typed:{raw}"
        text = str(description or "")
        if not text.strip():
            return False, ""
        # 时间/日期绑定证据按**整段**判定：一句里说"明天凌晨零点半接机，航班落地"
        # 时事件词与时间可能分处两个子句，不得因此漏判；否定/模糊仍按**事件词所在
        # 子句**判定（"不用开会"/"可能明天开会"/"有空想去看病" 都不会硬化）。
        has_time = bool(cls.HARD_TIME_EVIDENCE_RE.search(text))
        for clause in re.split(r"[，。；;！!？?\n]+", text):
            clause = clause.strip()
            if not clause:
                continue
            hard, source = cls._clause_hard(clause, has_time=has_time)
            if hard:
                return True, source
        return False, ""

    @classmethod
    def _clause_hard(cls, clause: str, *, has_time: bool = False) -> Tuple[bool, str]:
        """单子句判定（绑定短语按前置否定判；事件词需时间证据且非否定/非模糊）。"""
        for keyword in cls.HARD_COMMITMENT_KEYWORDS:
            start = clause.find(keyword)
            while start >= 0:
                prefix = clause[max(0, start - 3):start]
                if not any(marker in prefix for marker in ("不", "没", "别", "无", "免", "取消")):
                    return True, f"keyword:{keyword}"
                start = clause.find(keyword, start + 1)
        negated = any(marker in clause for marker in cls._HARD_NEGATION_MARKERS)
        hedged = any(marker in clause for marker in cls._HARD_HEDGE_MARKERS)
        if negated or hedged:
            return False, ""
        if not (has_time or cls.HARD_TIME_EVIDENCE_RE.search(clause)):
            return False, ""
        for keyword in cls.HARD_EVENT_KEYWORDS:
            if keyword in clause:
                return True, f"binding:{keyword}"
        return False, ""

    def _parse_intent_window(self, intent: Dict[str, Any], parsed: Dict[str, Any],
                             now: datetime) -> Tuple[int, int]:
        """意图时间窗（日帧分钟，``end`` 允许 >1440）：typed window → 裁判 time → 隐式下一刻。"""
        start: Optional[int] = None
        end: Optional[int] = None
        window = intent.get("window")
        if isinstance(window, (list, tuple)) and len(window) >= 2:
            start = parse_hhmm(str(window[0]), -1)
            end = parse_hhmm(str(window[1]), -1)
            start = start if start >= 0 else None
            end = end if end >= 0 else None
        if start is None and intent.get("start"):
            parsed_start = parse_hhmm(str(intent.get("start")), -1)
            start = parsed_start if parsed_start >= 0 else None
        if end is None and intent.get("end"):
            parsed_end = parse_hhmm(str(intent.get("end")), -1)
            end = parsed_end if parsed_end >= 0 else None
        if start is None:
            judged = parse_hhmm(str(parsed.get("time") or ""), -1)
            start = judged if judged >= 0 else (((now.hour * 60 + now.minute) // 30 + 1) * 30) % DAY_MINUTES
        if end is None:
            try:
                duration = int(intent.get("min_minutes") or self.DEFAULT_INTENT_MINUTES)
            except (TypeError, ValueError):
                duration = self.DEFAULT_INTENT_MINUTES
            duration = max(5, min(self.MAX_INTENT_MINUTES, duration))
            end = start + duration
        if end <= start:
            end += DAY_MINUTES  # 跨午夜意图（spine 契约：end 允许 >1440）
        return int(start), int(end)

    def _upsert_spine_intent(self, *, date: str, name: str, goal_type: str,
                             priority: str, hardness: str, start_minute: int,
                             end_minute: int, note: str) -> Optional[Dict[str, Any]]:
        """写入/更新一条今日 spine 意图（走 ``ScheduleGenerator.apply_schedule_items`` 共享漏斗）。

        只动 ``parameters.spine`` 行；不消费未来约定（``consume_commitments=False``）；
        其余条目原样带回写库。返回 ``{"item_id", "written", "row"}``；生成器/库缺失 → None。
        """
        generator = self._get_generator()
        query = self._schedule_query()
        db = getattr(query, "db", None) if query is not None else None
        if db is None:
            db = getattr(self._plugin, "_db", None)
        if generator is None or db is None or not callable(getattr(db, "get_day_schedule", None)):
            return None
        rows = [dict(row) for row in (db.get_day_schedule(date) or []) if isinstance(row, dict)]

        target: Optional[Dict[str, Any]] = None
        for row in rows:
            params = row.get("parameters")
            if not isinstance(params, dict) or not params.get("spine"):
                continue
            if str(row.get("name") or "").strip() == name:
                target = row
                break

        params = dict(target.get("parameters") or {}) if target else {}
        duration = max(5, int(end_minute) - int(start_minute))
        params.update({
            "spine": True,
            "min_minutes": int(params.get("min_minutes") or duration),
            "max_minutes": max(int(params.get("max_minutes") or duration), duration),
            "priority": priority,
            "hardness": hardness,
            "interruptibility": 0.1 if hardness == "hard" else float(params.get("interruptibility") or 0.5),
        })
        if note:
            params["note"] = note

        if target is None:
            row = {
                "item_id": uuid.uuid4().hex,
                "name": name,
                "description": note,
                "goal_type": goal_type,
                "priority": priority,
                "start_minute": int(start_minute),
                "end_minute": int(end_minute),
                "day_offset": 0,
                "parameters": params,
            }
            rows.append(row)
        else:
            row = target
            row.update({
                "name": name,
                "description": note,
                "goal_type": goal_type,
                "priority": priority,
                "start_minute": int(start_minute),
                "end_minute": int(end_minute),
                "day_offset": row.get("day_offset") or 0,
                "parameters": params,
            })
        rows.sort(key=lambda item: int(item.get("start_minute") or 0))
        try:
            written = int(generator.apply_schedule_items(
                rows, date, consume_commitments=False, reason=REASON_TOOL_EDIT) or 0)
        except Exception as exc:
            logger.warning("spine 意图写回失败: %s", exc)
            return None
        if not written:
            return None
        return {"item_id": str(row.get("item_id") or ""), "written": written, "row": row}

    async def _request_life_recheck(self, reason: str) -> None:
        """通知插件内部睡眠/生活循环"有新约定/新日程"（``plugin.request_life_recheck``）。

        2026-10-27 共享接线修补：**签名只绑定一次**，不再用「捕获 TypeError 再重调」
        的通用 adapter（那会在业务方法内部抛 TypeError 时把有副作用的方法再跑一遍）。
        该口只是幂等通知（bool 返回、不推进状态、不发消息、不调模型），因此按签名
        判断是否可传 reason；不可判定时按「无参」调用，绝不重复调用。未装配（旧
        plugin 无此口）→ 静默跳过。
        """
        hook = getattr(self._plugin, "request_life_recheck", None)
        if not callable(hook):
            return
        try:
            positionals = 0
            parameters = inspect.signature(hook).parameters.values()
            for param in parameters:
                if param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD):
                    positionals += 1
                elif param.kind == param.VAR_POSITIONAL:
                    positionals = max(positionals, 1)
            outcome = hook(reason or "schedule_update") if positionals else hook()
            if inspect.isawaitable(outcome):
                await outcome
        except Exception:
            logger.debug("request_life_recheck 调用失败（忽略）", exc_info=True)

    async def _apply_future_decision(self, parsed: Dict[str, Any], description: str,
                                     today_str: str, today: datetime,
                                     layers: Dict[str, bool],
                                     commitment: Dict[str, Any]) -> Dict[str, Any]:
        """decision=future：写 pending_commitment（未来仍走约定）并记 hardness。"""
        target_date = str(parsed.get("date") or commitment.get("date") or today_str)
        if target_date <= today_str:
            return {
                "type": "schedule_unchanged",
                "content": f"注意：未来约定日期 {target_date} 无效，日程不变",
            }
        title = str(parsed.get("title") or commitment.get("title") or description[:40])
        when = str(parsed.get("time") or commitment.get("time") or "")
        note = str(parsed.get("notes") or commitment.get("notes") or "")
        reason = str(parsed.get("reason") or "")
        goal = get_goal_manager().add_pending_commitment(
            commitment_date=target_date,
            title=title,
            time=when,
            notes=note,
            reason=reason,
        )
        goal_id = str(getattr(goal, "goal_id", "") or "")
        hard, hard_source = self.detect_hard_commitment(
            description, commitment=commitment)
        if layers["enabled"]:
            # 显式 hardness 贯通（2026-10-27 用户裁定）：soft 也落库，hard 代码强制
            # 不被 LLM 降级；生成侧/planner 据此保持 soft 不硬化、hard 固定时间。
            params = dict(getattr(goal, "parameters", None) or {})
            params.update({
                "hardness": "hard" if hard else "soft",
                "hard": bool(hard),
                "hard_source": hard_source,
                "source": str(params.get("source") or "user_request"),
            })
            try:
                get_goal_manager().update_goal(goal_id, parameters=params)
            except Exception as exc:
                logger.warning("未来约定 hardness 落库失败（约定本身已写入）: %s", exc)
            goal = get_goal_manager().get_goal(goal_id) or goal
            await self._request_life_recheck("commitment_written")
            hardness_label = "硬约定" if hard else "软约定"
            when_label = f"｜{when}" if when else ""
            content = (
                f"已记录 {target_date} 的约定：{title}（{hardness_label}{when_label}｜"
                f"约定ID={goal_id}）"
            )
            if note:
                content += f"\n备注：{note}"
            if reason:
                content += f"\n理由：{reason}"
            return {
                "type": "schedule_committed",
                "content": content,
                "goal_id": goal_id,
                "intent_id": goal_id,
                "hardness": "hard" if hard else "soft",
                "effective_time": f"{target_date} {when}".strip(),
            }
        # 旧行为分支（新层恒开，正常装配不可达；骨架插件兜底）：约定已写入，文案与旧版逐字一致
        await self._request_life_recheck("commitment_written")
        return {
            "type": "schedule_committed",
            "content": (
                f"已记录 {target_date} 的约定：{parsed.get('title')}\n"
                f"{('备注：' + parsed.get('notes')) if parsed.get('notes') else ''}\n"
                f"{('理由：' + reason) if reason else ''}"
            ).strip(),
        }

    async def _apply_today_decision(self, parsed: Dict[str, Any], description: str,
                                    today_str: str, now: datetime,
                                    intent: Dict[str, Any]) -> Dict[str, Any]:
        """decision=today：新层开时写/更新今日 spine 意图（硬约定代码强制 hardness=hard）。"""
        name = str(intent.get("name") or parsed.get("title") or description[:40]).strip()
        goal_type = str(intent.get("goal_type") or "").strip() or "daily_routine"
        priority = str(intent.get("priority") or "medium").strip().lower()
        if priority not in ("high", "medium", "low"):
            priority = "medium"
        start_minute, end_minute = self._parse_intent_window(intent, parsed, now)
        hard, hard_source = self.detect_hard_commitment(description, intent=intent)
        hardness = "hard" if hard else "soft"
        note = str(parsed.get("notes") or intent.get("note") or "").strip()
        written = self._upsert_spine_intent(
            date=today_str, name=name, goal_type=goal_type, priority=priority,
            hardness=hardness, start_minute=start_minute, end_minute=end_minute, note=note)
        if written is None:
            return {"type": "error",
                    "content": "日程生成器不可用（插件未完成初始化），今日意图未写入"}
        await self._request_life_recheck("schedule_intent_written")
        intent_id = written["item_id"]
        window = f"{format_day_minute(start_minute)}-{format_day_minute(end_minute)}"
        hardness_label = "硬约定" if hard else "软意图"
        content = (f"已更新今日意图：{name}（{hardness_label}｜{window}｜意图ID={intent_id}）")
        if note:
            content += f"\n备注：{note}"
        if hard:
            content += f"\n生效时间：今天 {format_day_minute(start_minute)} 起"
        return {
            "type": "schedule_intent_updated",
            "content": content,
            "intent_id": intent_id,
            "goal_id": intent_id,
            "hardness": hardness,
            "hard_source": hard_source,
            "effective_time": f"{today_str} {format_day_minute(start_minute)}",
        }

    async def update_schedule(self, **function_args: Any) -> Dict[str, Any]:
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        """根据自然语言请求更新日程：角色裁判 → today / future / reject。

        ``role_judge_enabled=False`` 时降级为直接 ``add_pending_commitment``（无 LLM）。

        v2（定稿 §4.6 / 附录 A.3.2；PR8 3.0.0 起 ``_layers()["enabled"]`` 恒 True——
        ``[schedule].enabled`` 唯一总开关、spine 唯一路线）：
        - ``today`` → 写/更新**今日 spine 意图行**（不再整日重生成，零新增 LLM 调用）；
        - ``future`` → 仍写 ``pending_commitment``，并落 ``parameters.hardness``；
        - 明确硬约定由 :meth:`detect_hard_commitment` **代码强制** ``hardness=hard``
          （LLM 返回 soft 也不能降级；软意图不会因为 LLM 说 hard 就被硬化；
          角色裁判返回的 ``parsed["hardness"]`` **永不参与**判定——只认 typed 显式
          声明与描述里的绑定证据，2026-10-27 接线口径）；
        - 返回内容含摘要、意图/约定 ID、是否硬约定、生效时间。
        （旧「新层全关」重生成分支仅为骨架插件兜底保留，正常装配下不可达。）

        ``intent`` / ``commitment`` 是**可选** typed 归一参数（dict），旧调用只传
        ``description`` 完全不受影响。
        """
        try:
            description = str(function_args.get("description", "") or "").strip()
            intent_arg = function_args.get("intent")
            intent_arg = intent_arg if isinstance(intent_arg, dict) else {}
            commitment_arg = function_args.get("commitment")
            commitment_arg = commitment_arg if isinstance(commitment_arg, dict) else {}
            if not description:
                # typed 参数只做"归一辅助"：description 缺省时用它兜底成裁判文本，
                # 旧调用（只有 description）行为完全不变。
                description = str(intent_arg.get("name")
                                  or commitment_arg.get("title") or "").strip()
            if not description:
                return {"type": "error", "content": "请提供日程变更描述"}

            layers = self._layers()
            cfg = self._advanced_cfg()   # 裁判开关/温度/超时/最大 token 均在高级段
            goal_manager = get_goal_manager()
            tz_manager = self._make_tz_manager()
            today = tz_manager.get_now()
            today_str = today.strftime("%Y-%m-%d")
            weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][today.weekday()]

            # 角色裁判关闭：直接当作未来约定记录
            if not cfg.role_judge_enabled:
                goal_manager.add_pending_commitment(
                    commitment_date=today_str,
                    title=description[:40],
                    notes=description,
                )
                return {
                    "type": "schedule_updated",
                    "content": "已记录为今天的候选约定（裁判模式关闭，仅当天重新生成日程时才会纳入）",
                }

            # 准备当前活动摘要 + 人设
            current_activities = []
            for goal in goal_manager.get_schedule_goals(chat_id="global"):
                tw = (goal.parameters or {}).get("time_window") or (goal.conditions or {}).get("time_window")
                time_label = ""
                if tw and len(tw) >= 2:
                    h, m = divmod(int(tw[0]), 60)
                    time_label = f"{h:02d}:{m:02d}"
                current_activities.append({"time": time_label, "name": goal.name})
            persona = str(getattr(self._plugin, "_personality", "") or "")

            parsed = await judge_schedule_request(
                self._plugin,
                description=description,
                current_activities=current_activities,
                persona=persona,
                today_str=today_str,
                weekday=weekday,
                task_name=llm_task_name_of(self._plugin),
                temperature=cfg.role_judge_temperature,
                max_tokens=cfg.max_tokens,
                # 角色裁判同样受 generation_timeout 约束
                timeout_ms=int(float(cfg.generation_timeout) * 1000),
            )

            if parsed is None:
                return {"type": "schedule_unchanged", "content": "没听清你的请求，日程保持不变"}

            decision = parsed["decision"]
            reason = str(parsed.get("reason") or "").strip()

            if decision == "reject":
                msg = "我没接受这个请求，日程保持不变"
                if reason:
                    msg += f"\n理由：{reason}"
                return {"type": "schedule_unchanged", "content": msg}

            if decision == "future":
                return await self._apply_future_decision(
                    parsed, description, today_str, today, layers, commitment_arg)

            # decision == "today"：写/更新 spine 意图（不重生成、不新增 LLM 调用）；
            # 旧「新层全关」重生成分支仅为骨架插件兜底保留（正常装配不可达）
            if layers["enabled"]:
                return await self._apply_today_decision(
                    parsed, description, today_str, today, intent_arg)
            extra_parts = [
                f"今天临时纳入：{parsed.get('title') or description[:40]}",
            ]
            if parsed.get("time"):
                extra_parts.append(f"建议时间：{parsed['time']}")
            if parsed.get("notes"):
                extra_parts.append(f"备注：{parsed['notes']}")
            if reason:
                extra_parts.append(f"角色理由：{reason}")
            extra_prompt = "\n".join(extra_parts)

            generator = self._get_generator()
            if generator is None:
                return {"type": "error", "content": "日程生成器不可用（插件未完成初始化）"}
            items = await generator.regenerate_today_schedule(
                extra_prompt=extra_prompt, apply=True, **self._generate_kwargs(),
            )
            await self._request_life_recheck("schedule_regenerated")
            return {
                "type": "schedule_regenerated",
                "content": (
                    f"已根据请求调整今日日程，共 {len(items)} 个活动\n"
                    f"{('理由：' + reason) if reason else ''}"
                ).strip(),
            }

        except Exception as exc:
            logger.error(f"update_schedule 失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"更新日程失败: {exc}"}

