"""日程信息查询（四视图只读）/ 角色裁判式日程更新 / 日程重生成的工具业务实现
（来源：自主规划插件 services/tools_service.py，移植版）。

通过 ``self._plugin.config`` 强类型访问插件配置，通过 ``self._plugin.ctx``
访问 SDK 能力代理；LLM 调用经宿主 ``ctx.llm.generate``（角色裁判）或
core.llm_utils.LLMService（日程生成）。

T6 注册面收口后本服务只服务两个 Tool：``get_schedule_info``（只读四视图）与
``update_schedule``（唯一日程写入口，role_judge 裁判路径）。目标的写 API 仍在
GoalManager（生成流水线与约定消费在用），只是不再有 Tool 注册入口。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .config import ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .role_judge import judge_schedule_request
from .schedule_generator import ScheduleGenerator
from .time_utils import TimezoneManager

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
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig)

    def _make_tz_manager(self) -> TimezoneManager:
        """根据配置创建 TimezoneManager。"""
        return TimezoneManager(self._schedule_cfg().timezone)

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
        """从 schedule 配置段产出 generate_daily_schedule 的完整关键字参数。"""
        cfg = self._schedule_cfg()
        return {
            "personality": str(getattr(self._plugin, "_personality", "") or ""),
            "lover_name": str(getattr(self._plugin, "_lover_name", "") or "麦麦"),
            "wake_time": cfg.wake_time,
            "sleep_time": cfg.sleep_time,
            "min_activities": cfg.min_activities,
            "max_activities": cfg.max_activities,
            "timeout_seconds": cfg.generation_timeout,
            "use_multi_round": cfg.use_multi_round,
            "max_rounds": cfg.max_rounds,
            "quality_threshold": cfg.quality_threshold,
            "enable_detailed_description": cfg.enable_detailed_description,
            "min_description_length": cfg.min_description_length,
            "max_description_length": cfg.max_description_length,
            "max_tokens": cfg.max_tokens,
            "custom_prompt": cfg.custom_prompt,
            "recent_schedule_days": cfg.recent_schedule_days,
            "history_message_limit": cfg.history_message_limit,
            "knowledge_search_limit": cfg.knowledge_search_limit,
            "cross_day_activity": cfg.cross_day_activity,
            "no_sleep_mode": cfg.no_sleep_mode,
            "timezone": cfg.timezone,
            "llm_task_name": cfg.llm_task_name,
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
        return await generator.regenerate_today_schedule(
            extra_prompt=extra_prompt, apply=True, **self._generate_kwargs(),
        )

    # ------------------------------------------------------------
    # Tool 1：get_schedule_info（四视图只读）
    # ------------------------------------------------------------

    #: view 取值（缺省/非法一律回落 status）
    _SCHEDULE_INFO_VIEWS = ("status", "current", "lover_day", "goals")

    async def get_schedule_info(self, view: str = "", **function_args: Any) -> Dict[str, Any]:
        """日程/活动/目标的只读查询（view = status | current | lover_day | goals）。

        四视图全部只读，不触碰任何写路径（生成/落库/目标写 API 都不调用）。
        """
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        del function_args
        view = str(view or "").strip().lower()
        if view not in self._SCHEDULE_INFO_VIEWS:
            view = "status"  # 缺省与非法值同档：回落状态总览
        try:
            if view == "current":
                return self._view_current()
            if view == "lover_day":
                return self._view_lover_day()
            if view == "goals":
                return self._view_goals()
            return self._view_status()
        except Exception as exc:
            logger.error(f"获取日程信息失败（view={view}）: {exc}", exc_info=True)
            return {"type": "error", "content": f"获取日程信息失败: {exc}"}

    def _view_status(self) -> Dict[str, Any]:
        """view=status：今日日程状态总览（正在进行 / 即将到来 / 已完成）。"""
        goal_manager = get_goal_manager()
        enable_detailed_description = self._schedule_cfg().enable_detailed_description

        # 统一获取今日日程目标（合成视图，含日期过滤）
        schedule_goals = goal_manager.get_schedule_goals(chat_id="global")
        if not schedule_goals:
            return {"type": "schedule_info", "content": "📅 今天还没有日程"}

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

        # 分类：正在进行 / 即将到来 / 已完成
        ongoing: List[Tuple[Any, List[int]]] = []
        upcoming: List[Tuple[Any, List[int]]] = []
        completed: List[Tuple[Any, List[int]]] = []
        for goal, tw in schedule_with_time:
            start_min, end_min = tw
            if start_min <= current_minutes <= end_min:
                ongoing.append((goal, tw))
            elif current_minutes < start_min:
                upcoming.append((goal, tw))
            else:
                completed.append((goal, tw))

        def _fmt_time(minutes: int) -> str:
            h, m = minutes // 60, minutes % 60
            return f"{h:02d}:{m:02d}"

        def _fmt_item(goal: Any, tw: List[int], emoji: str = "") -> str:
            start_time = _fmt_time(tw[0])
            end_time = _fmt_time(tw[1] if tw[1] < 1440 else tw[1] - 1440)
            desc = ""
            if enable_detailed_description and goal.description:
                desc = goal.description
                if len(desc) > 30:
                    desc = desc[:27] + "..."
            if desc:
                return f"{emoji}{start_time}-{end_time} {goal.name}\n   💭 {desc}"
            return f"{emoji}{start_time}-{end_time} {goal.name}"

        date_str = now.strftime("%Y-%m-%d")
        weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        weekday = weekday_names[now.weekday()]

        content = f"📅 今日日程 {date_str} {weekday}\n"

        if ongoing:
            content += "\n🔵 正在进行:\n"
            for goal, tw in ongoing:
                content += _fmt_item(goal, tw, "▶️ ") + "\n"

        if upcoming:
            content += "\n⏰ 即将到来:\n"
            for goal, tw in upcoming[:5]:
                content += _fmt_item(goal, tw) + "\n"
            if len(upcoming) > 5:
                content += f"   ...还有 {len(upcoming) - 5} 个活动\n"

        if completed:
            content += "\n✅ 已完成:\n"
            for goal, tw in completed[-3:]:
                content += _fmt_item(goal, tw) + "\n"

        content += f"\n📊 共 {len(schedule_goals)} 个活动"
        return {"type": "schedule_info", "content": content}

    def _view_current(self) -> Dict[str, Any]:
        """view=current：当前正在进行的活动（无活动给休息话术）。"""
        name = str(getattr(self._plugin, "_lover_name", "") or "麦麦")
        query = self._schedule_query()
        if query is None or not callable(getattr(query, "find_current_activity", None)):
            return {"type": "schedule_current",
                    "content": f"{name}的日程服务还没准备好，暂时不清楚在做什么。"}
        now = datetime.now()
        current = query.find_current_activity(now.strftime("%Y-%m-%d"), now)
        if current is None:
            return {"type": "schedule_current", "content": f"{name}现在没有安排中的活动（可能在休息）。"}
        return {"type": "schedule_current",
                "content": f"{name}现在正在：{query.format_activity_line(current, now)}。"}

    def _view_lover_day(self) -> Dict[str, Any]:
        """view=lover_day：今日完整日程逐条（时间轴 + 名称，不截断）。"""
        query = self._schedule_query()
        today = self._make_tz_manager().get_now().strftime("%Y-%m-%d")
        if query is None:
            return {"type": "schedule_day", "content": "日程服务未就绪（planning 子模块不可用）。"}
        items = query.get_day_schedule(today)
        if not items:
            return {"type": "schedule_day", "content": f"📅 今日 ({today}) 暂无日程。"}
        lines = [f"📅 今日完整日程 ({today})"]
        for item in items:
            start = int(item.get("start_minute") or 0)
            end = int(item.get("end_minute") or 0)
            lines.append(f"  [{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}] "
                         f"🐱 {item.get('name', '未知')}")
        return {"type": "schedule_day", "content": "\n".join(lines)}

    def _view_goals(self) -> Dict[str, Any]:
        """view=goals：进行中的目标列表（只读 get_active_goals，不进行任何写操作）。"""
        goals = get_goal_manager().get_active_goals(chat_id="global")
        if not goals:
            return {"type": "schedule_goals", "content": "当前没有进行中的目标"}
        lines = [f"🎯 进行中的目标（{len(goals)} 个）"]
        for goal in goals:
            summary = ""
            getter = getattr(goal, "get_summary", None)
            if callable(getter):
                try:
                    summary = str(getter() or "").strip()
                except Exception:
                    summary = ""
            if summary:
                lines.append(f"- {summary}")
                continue
            # get_summary 不可用（旧档案/桩对象）时退化为紧凑行：名称/时间窗/进度
            tw = (getattr(goal, "parameters", None) or {}).get("time_window")
            window = ""
            if isinstance(tw, list) and len(tw) == 2:
                window = f"{int(tw[0]) // 60:02d}:{int(tw[0]) % 60:02d}-{int(tw[1]) // 60:02d}:{int(tw[1]) % 60:02d} "
            lines.append(f"- {window}{getattr(goal, 'name', '')}（进度 {getattr(goal, 'progress', 0)}%）")
        return {"type": "schedule_goals", "content": "\n".join(lines)}

    # ------------------------------------------------------------
    # Tool 2：update_schedule（唯一日程写入口，角色裁判式更新）
    # ------------------------------------------------------------

    async def update_schedule(self, **function_args: Any) -> Dict[str, Any]:
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        """根据自然语言请求更新日程：角色裁判 → today / future / reject。

        ``role_judge_enabled=False`` 时降级为直接 ``add_pending_commitment``（无 LLM）。
        """
        try:
            description = str(function_args.get("description", "") or "").strip()
            if not description:
                return {"type": "error", "content": "请提供日程变更描述"}

            cfg = self._schedule_cfg()
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
                    "content": "📌 已记录到日程候选清单（裁判模式关闭，下次生成日程时纳入）",
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
                task_name=cfg.llm_task_name,
                temperature=cfg.role_judge_temperature,
                max_tokens=cfg.max_tokens,
                # 角色裁判同样受 generation_timeout 约束
                timeout_ms=int(float(cfg.generation_timeout) * 1000),
            )

            if parsed is None:
                return {"type": "schedule_unchanged", "content": "🤔 没听清你的请求，日程保持不变"}

            decision = parsed["decision"]
            reason = str(parsed.get("reason") or "").strip()

            if decision == "reject":
                msg = "😶 我没接受这个请求，日程保持不变"
                if reason:
                    msg += f"\n理由：{reason}"
                return {"type": "schedule_unchanged", "content": msg}

            if decision == "future":
                target_date = str(parsed.get("date") or today_str)
                if target_date <= today_str:
                    return {
                        "type": "schedule_unchanged",
                        "content": f"⚠️ 未来约定日期 {target_date} 无效，日程不变",
                    }
                goal_manager.add_pending_commitment(
                    commitment_date=target_date,
                    title=str(parsed.get("title") or description[:40]),
                    time=str(parsed.get("time") or ""),
                    notes=str(parsed.get("notes") or ""),
                    reason=reason,
                )
                return {
                    "type": "schedule_committed",
                    "content": (
                        f"📅 已记录 {target_date} 的约定：{parsed.get('title')}\n"
                        f"{('备注：' + parsed.get('notes')) if parsed.get('notes') else ''}\n"
                        f"{('理由：' + reason) if reason else ''}"
                    ).strip(),
                }

            # decision == "today"：重生成今日日程
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
            return {
                "type": "schedule_regenerated",
                "content": (
                    f"✅ 已根据请求调整今日日程，共 {len(items)} 个活动\n"
                    f"{('理由：' + reason) if reason else ''}"
                ).strip(),
            }

        except Exception as exc:
            logger.error(f"update_schedule 失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"更新日程失败: {exc}"}
