"""目标管理 / 状态查询 / 应用日程 / 角色裁判更新 的工具业务实现
（来源：自主规划插件 services/tools_service.py，移植版）。

通过 ``self._plugin.config`` 强类型访问插件配置，通过 ``self._plugin.ctx``
访问 SDK 能力代理；LLM 调用经宿主 ``ctx.llm.generate``（角色裁判）或
core.llm_utils.LLMService（日程生成）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

from .config import ScheduleSectionConfig, section_or_default
from .exceptions import InvalidParametersError, InvalidTimeWindowError
from .goal_manager import GoalManager, GoalPriority, get_goal_manager
from .parameter_validator import ParameterValidator
from .role_judge import judge_schedule_request
from .schedule_generator import ScheduleGenerator
from .time_utils import TimezoneManager

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


def _parse_json_parameters(raw_params: Union[str, Dict[str, Any], None]) -> Dict[str, Any]:
    """解析 JSON 参数（字符串或字典）。"""
    if isinstance(raw_params, str):
        try:
            return json.loads(raw_params)
        except json.JSONDecodeError:
            logger.warning(f"无法解析参数 JSON: {raw_params}")
            return {}
    elif isinstance(raw_params, dict):
        return raw_params
    return {}


def _parse_time_window_str(time_window_str: str) -> Optional[List[int]]:
    """解析 ``'HH:MM-HH:MM'`` 时间窗口字符串为分钟数列表。"""
    try:
        parts = time_window_str.split("-")
        if len(parts) != 2:
            return None
        start_parts = parts[0].strip().split(":")
        end_parts = parts[1].strip().split(":")
        start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
        end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])
        return [start_minutes, end_minutes]
    except (ValueError, IndexError):
        return None


def _validate_parameters_schema(params: Dict[str, Any], goal_type: Optional[str] = None) -> Tuple[bool, Optional[str]]:
    """验证目标 parameters 的 schema 结构（time_window / topics / depth 等）。

    Raises:
        InvalidParametersError: 参数验证失败
    """
    if not isinstance(params, dict):
        raise InvalidParametersError("参数必须是字典类型", invalid_value=type(params).__name__)

    if "time_window" in params:
        ParameterValidator.validate_time_window(params["time_window"])

    if goal_type == "learn_topic":
        if "topics" not in params:
            raise InvalidParametersError("learn_topic 类型的目标必须包含 topics 参数", field_name="topics")
        topics = params["topics"]
        if not isinstance(topics, list):
            raise InvalidParametersError(
                f"topics 必须是列表，当前类型: {type(topics).__name__}",
                field_name="topics",
                invalid_value=topics,
            )
        if not all(isinstance(t, str) for t in topics):
            raise InvalidParametersError("topics 的元素必须都是字符串", field_name="topics", invalid_value=topics)
        if len(topics) == 0:
            raise InvalidParametersError("topics 列表不能为空", field_name="topics", invalid_value=topics)

        if "depth" not in params:
            raise InvalidParametersError("learn_topic 类型的目标必须包含 depth 参数", field_name="depth")
        depth = params["depth"]
        if not isinstance(depth, str):
            raise InvalidParametersError(
                f"depth 必须是字符串，当前类型: {type(depth).__name__}",
                field_name="depth",
                invalid_value=depth,
            )
        valid_depths = ["basic", "intermediate", "advanced"]
        if depth not in valid_depths:
            raise InvalidParametersError(
                f"depth 必须是以下之一: {valid_depths}，当前: {depth}",
                field_name="depth",
                invalid_value=depth,
            )

    if "check_plugins" in params:
        check_plugins = params["check_plugins"]
        if not isinstance(check_plugins, bool):
            raise InvalidParametersError(
                f"check_plugins 必须是布尔值，当前类型: {type(check_plugins).__name__}",
                field_name="check_plugins",
                invalid_value=check_plugins,
            )

    if "greeting_type" in params:
        greeting_type = params["greeting_type"]
        if not isinstance(greeting_type, str):
            raise InvalidParametersError(
                f"greeting_type 必须是字符串，当前类型: {type(greeting_type).__name__}",
                field_name="greeting_type",
                invalid_value=greeting_type,
            )

    return True, None


class ToolsService:
    """承载 4 个 LLM Tool 的业务逻辑（manage_goal / get_planning_status /
    update_schedule / apply_schedule）。"""

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
    # Tool 1：manage_goal
    # ------------------------------------------------------------

    async def manage_goal(self, **function_args: Any) -> Dict[str, Any]:
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        """目标管理工具（创建/查看/更新/暂停/恢复/完成/取消/删除）。

        通过字典分发表代替长 if-else 链，便于扩展新 action。
        """
        action = function_args.get("action")
        goal_manager = get_goal_manager()
        # Tool 不注入 chat_id / user_id 上下文，全部走全局
        chat_id = "global"
        user_id = "system"

        try:
            if action == "create":
                return await self._action_create(function_args, goal_manager, chat_id, user_id)
            if action == "list":
                summary = goal_manager.get_goals_summary(chat_id=chat_id)
                return {"type": "goal_list", "content": summary}
            if action == "get":
                return self._action_get(function_args, goal_manager)
            if action == "update":
                return self._action_update(function_args, goal_manager)
            if action in {"pause", "resume", "complete", "cancel"}:
                return self._toggle_action(function_args, goal_manager, action)
            if action == "delete":
                return self._action_delete(function_args, goal_manager)
            return {"type": "error", "content": f"未知操作: {action}"}
        except Exception as exc:
            logger.error(f"目标管理失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"操作失败: {exc}"}

    async def _action_create(
        self,
        function_args: Dict[str, Any],
        goal_manager: GoalManager,
        chat_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """处理 create action。"""
        name = function_args.get("name")
        description = function_args.get("description")

        if not name or not description:
            return {"type": "error", "content": "创建目标需要提供 name 和 description"}

        # 输入长度限制
        if len(name) > 100:
            return {"type": "error", "content": "目标名称过长（最多 100 字符）"}
        if len(description) > 500:
            return {"type": "error", "content": "目标描述过长（最多 500 字符）"}

        # 特殊字符过滤（防注入）
        dangerous_patterns = ["<script>", "{{", "}}", "${", "$(", "`"]
        for pattern in dangerous_patterns:
            if pattern in name or pattern in description:
                return {"type": "error", "content": f"输入包含非法字符: {pattern}"}

        goal_type = function_args.get("goal_type", "custom")
        priority = function_args.get("priority", "medium")
        time_window_str = function_args.get("time_window")
        deadline_hours = function_args.get("deadline_hours")

        # 解析时间窗口
        time_window = None
        if time_window_str:
            time_window = _parse_time_window_str(time_window_str)
            if time_window is None:
                return {"type": "error", "content": "时间窗口格式错误，应为 'HH:MM-HH:MM'"}

        if deadline_hours is not None:
            try:
                deadline_hours = float(deadline_hours)
            except (TypeError, ValueError):
                return {"type": "error", "content": "截止时间必须是数字（小时）"}
            if deadline_hours <= 0:
                return {"type": "error", "content": "截止时间必须大于 0 小时"}
            if deadline_hours > 87600:  # 10 年
                return {"type": "error", "content": "截止时间不能超过 10 年"}

        parameters = _parse_json_parameters(function_args.get("parameters", {}))

        # 计算截止时间
        tz_manager = self._make_tz_manager()
        deadline = tz_manager.get_now() + timedelta(hours=deadline_hours) if deadline_hours else None

        # 将 time_window 存入 parameters
        if time_window:
            parameters["time_window"] = time_window

        # 验证 parameters 的 schema
        try:
            _validate_parameters_schema(parameters, goal_type)
        except (InvalidParametersError, InvalidTimeWindowError) as exc:
            logger.warning(f"参数验证失败: {exc}")
            return {"type": "error", "content": f"参数验证失败: {exc}"}

        goal = goal_manager.create_goal(
            name=name,
            description=description,
            goal_type=goal_type,
            creator_id=user_id,
            chat_id=chat_id,
            priority=priority,
            deadline=deadline,
            parameters=parameters,
        )

        content = f"""✅ 目标创建成功！

{goal.get_summary()}

我会自动执行这个目标~"""

        return {"type": "goal_created", "id": goal.goal_id, "content": content}

    def _action_get(self, function_args: Dict[str, Any], goal_manager: GoalManager) -> Dict[str, Any]:
        """处理 get action。"""
        goal_id = function_args.get("goal_id")
        if not goal_id:
            return {"type": "error", "content": "需要提供 goal_id"}

        goal = goal_manager.get_goal(goal_id)
        if not goal:
            return {"type": "error", "content": f"目标不存在: {goal_id}"}

        return {"type": "goal_info", "content": goal.get_summary()}

    def _action_update(self, function_args: Dict[str, Any], goal_manager: GoalManager) -> Dict[str, Any]:
        """处理 update action。"""
        goal_id = function_args.get("goal_id")
        if not goal_id:
            return {"type": "error", "content": "需要提供 goal_id"}

        update_params: Dict[str, Any] = {}
        if "name" in function_args:
            update_params["name"] = function_args["name"]
        if "description" in function_args:
            update_params["description"] = function_args["description"]
        if "priority" in function_args:
            update_params["priority"] = GoalPriority(function_args["priority"]).value
        if "time_window" in function_args:
            tw = _parse_time_window_str(function_args["time_window"])
            if tw is None:
                return {"type": "error", "content": "时间窗口格式错误，应为 'HH:MM-HH:MM'"}
            goal = goal_manager.get_goal(goal_id)
            if goal:
                params = goal.parameters.copy() if goal.parameters else {}
                params["time_window"] = tw
                update_params["parameters"] = params
        if "parameters" in function_args:
            update_params["parameters"] = _parse_json_parameters(function_args["parameters"])

        success = goal_manager.update_goal(goal_id, **update_params)

        if success:
            goal = goal_manager.get_goal(goal_id)
            if goal:
                return {"type": "goal_updated", "content": f"✅ 目标已更新\n\n{goal.get_summary()}"}
            return {"type": "error", "content": "目标已被删除"}
        return {"type": "error", "content": "更新失败"}

    def _toggle_action(
        self,
        function_args: Dict[str, Any],
        goal_manager: GoalManager,
        action: str,
    ) -> Dict[str, Any]:
        """统一处理 pause / resume / complete / cancel 这 4 个状态切换 action。"""
        goal_id = function_args.get("goal_id")
        if not goal_id:
            return {"type": "error", "content": "需要提供 goal_id"}

        success = False
        if action == "pause":
            success = goal_manager.pause_goal(goal_id)
            label = ("goal_paused", "⏸️ 目标已暂停", "暂停失败")
        elif action == "resume":
            success = goal_manager.resume_goal(goal_id)
            label = ("goal_resumed", "▶️ 目标已恢复", "恢复失败")
        elif action == "complete":
            success = goal_manager.complete_goal(goal_id)
            label = ("goal_completed", "✅ 目标已完成！", "完成失败")
        elif action == "cancel":
            success = goal_manager.cancel_goal(goal_id)
            label = ("goal_cancelled", "❌ 目标已取消", "取消失败")
        else:
            return {"type": "error", "content": f"未知 toggle action: {action}"}

        return {"type": label[0] if success else "error", "content": label[1] if success else label[2]}

    def _action_delete(self, function_args: Dict[str, Any], goal_manager: GoalManager) -> Dict[str, Any]:
        """处理 delete action。"""
        goal_id = function_args.get("goal_id")
        if not goal_id:
            return {"type": "error", "content": "需要提供 goal_id"}
        goal = goal_manager.get_goal(goal_id)
        if not goal:
            return {"type": "error", "content": f"目标不存在: {goal_id}"}
        goal_name = goal.name
        success = goal_manager.delete_goal(goal_id)
        return {
            "type": "goal_deleted" if success else "error",
            "content": f"🗑️ 已删除目标: {goal_name}" if success else "删除失败",
        }

    # ------------------------------------------------------------
    # Tool 2：get_planning_status
    # ------------------------------------------------------------

    async def get_planning_status(self, **function_args: Any) -> Dict[str, Any]:
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        """查询并返回今日日程（正在进行 / 即将到来 / 已完成 分组）。"""
        try:
            goal_manager = get_goal_manager()
            detailed = bool(function_args.get("detailed", False))

            enable_detailed_description = self._schedule_cfg().enable_detailed_description

            # 统一获取今日日程目标（合成视图，含日期过滤）
            schedule_goals = goal_manager.get_schedule_goals(chat_id="global")

            if not schedule_goals:
                return {"type": "planning_status", "content": "📅 今天还没有日程"}

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

            if completed and detailed:
                content += "\n✅ 已完成:\n"
                for goal, tw in completed[-3:]:
                    content += _fmt_item(goal, tw) + "\n"

            content += f"\n📊 共 {len(schedule_goals)} 个活动"
            if not detailed:
                content += " | 详情: detailed=true"

            return {"type": "planning_status", "content": content}

        except Exception as exc:
            logger.error(f"获取规划状态失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"获取状态失败: {exc}"}

    # ------------------------------------------------------------
    # Tool 3：update_schedule（角色裁判式更新）
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

    # ------------------------------------------------------------
    # Tool 4：apply_schedule
    # ------------------------------------------------------------

    async def apply_schedule(self, **function_args: Any) -> Dict[str, Any]:
        if not self._module_enabled():
            return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
        """应用日程并落库（time_window 换算 + 约定匹配）。"""
        try:
            schedule_data = function_args.get("schedule_data")
            if not schedule_data:
                return {"type": "error", "content": "需要提供 schedule_data"}

            # schedule_data 可能是 JSON 字符串
            if isinstance(schedule_data, str):
                try:
                    schedule_data = json.loads(schedule_data)
                except json.JSONDecodeError as exc:
                    return {"type": "error", "content": f"schedule_data 解析失败: {exc}"}

            generator = self._get_generator()
            if generator is None:
                return {"type": "error", "content": "日程生成器不可用（插件未完成初始化）"}

            # 重建日程条目（dict 形态；time_slot + duration_hours 归一化为分钟窗口）
            raw_items: List[Dict[str, Any]] = []
            for item_data in schedule_data.get("items", []):
                raw_items.append({
                    "name": item_data["name"],
                    "description": item_data.get("description", ""),
                    "goal_type": item_data["goal_type"],
                    "priority": item_data["priority"],
                    "time_slot": item_data.get("time_slot"),
                    "duration_hours": item_data.get("duration_hours"),
                    "parameters": item_data.get("parameters", {}),
                })
            if not raw_items:
                return {"type": "error", "content": "schedule_data 中没有有效的日程条目"}

            cfg = self._schedule_cfg()
            tz_manager = self._make_tz_manager()
            today_str = tz_manager.get_now().strftime("%Y-%m-%d")
            items = generator._normalize_items(raw_items, today_str, cfg.wake_time, cfg.sleep_time)
            written = generator.apply_schedule_items(items, today_str, consume_commitments=False)

            content = f"""✅ 日程应用成功！

写入了 {written} 个日程条目（所有聊天共享）
日程名称: {schedule_data.get('name', today_str)}

这些日程已经激活，我会自动执行它们~

使用 /cateye_test_plan list 查看今日安排"""

            return {"type": "schedule_applied", "content": content}

        except Exception as exc:
            logger.error(f"应用日程失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"应用日程失败: {exc}"}
