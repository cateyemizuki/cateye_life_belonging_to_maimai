"""日程信息查询（四视图只读）/ 角色裁判式日程更新 / 日程重生成的工具业务实现
（来源：自主规划插件 services/tools_service.py，移植版）。

通过 ``self._plugin.config`` 强类型访问插件配置，通过 ``self._plugin.ctx``
访问 SDK 能力代理；LLM 调用经宿主 ``ctx.llm.generate``（角色裁判）或
core.llm_utils.LLMService（日程生成）。

T6 注册面收口后本服务只服务两个 Tool：``get_schedule_info``（只读四视图）与
``update_schedule``（唯一日程写入口，role_judge 裁判路径）。目标的写 API 仍在
GoalManager（生成流水线与约定消费在用），只是不再有 Tool 注册入口。

M9（附录 §4.3/§4.4 赖床提醒，用户裁决 C4/A2）新增第三个 Tool：``nap_lie_in`` ——
夜间被吵醒后收到提醒才可用（``metadata["visibility"]="hidden"``，注册期不进名册）；
只把**今日 sleep 条目的结束时间**推到目标时刻（走 ``apply_schedule_items`` 共享底层，
不消费未来约定、不触发重生成），并通知 emotion 延长本周期窗口 + 计一次"有效提醒次数"。
工具名常量 :data:`NAP_TOOL_NAME` 是**唯一定义点**，提醒文本与组件注册同源引用。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from core.time_utils import parse_hhmm

from core.llm_utils import llm_task_name_of
from suite_prompts import (
    NAP_TOOL_BAD_TIME_DEFAULT,
    NAP_TOOL_DENIED_DEFAULT,
    prompt as prompt_of,
)

from .config import (
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
    section_or_default,
)
from .activity_properties import resolve_property
from .cycle_state import (REASON_TOOL_EDIT, enter_schedule_cycle,  # §31.2 进账血缘；R7 2026-10-23
                          entry_context_from_plugin)
from .goal_manager import get_goal_manager
from .role_judge import judge_schedule_request
from .schedule_generator import ScheduleGenerator
from .schedule_items import find_night_sleep_item, is_night_sleep_item
from .time_utils import (DAY_MINUTES, TimezoneManager, format_day_minute,
                         is_in_day_frame_marked, is_upcoming_day_minute_marked)

logger = logging.getLogger(__name__)

# ── L3 赖床工具常量（附录 §4.3/§4.4；用户裁决 A2/C4）────────────────────────
#: 赖床工具名（**唯一定义点**）：组件注册（planning/components.py）与提醒文本
#: （emotion/service.py 的 NAP_REMINDER_TEMPLATE）同源引用本常量——A2 明确"工具后续会改名，
#: 注意同步"，改名只需动这一处。
NAP_TOOL_NAME = "lbm_nap_lie_in"
#: 钳制上限的缺省值（[schedule_advanced] lie_in_max_until；HH:MM），§4.4「传入时间 > 上限 → 钳到上限」
DEFAULT_LIE_IN_MAX_UNTIL = "12:00"
#: 非睡眠周期调用的拒绝话术（出厂文案见 suite_prompts.NAP_TOOL_DENIED_DEFAULT，
#: 2026-10-10 起可在 [prompts].nap_tool_denied 覆盖）
NAP_LIE_IN_DENIED_TEXT = NAP_TOOL_DENIED_DEFAULT
#: 解析失败/缺参的友好提示（可在 [prompts].nap_tool_bad_time 覆盖）
NAP_LIE_IN_BAD_TIME_TEXT = NAP_TOOL_BAD_TIME_DEFAULT
# 口径说明（M10 第 6 段）：夜晚睡眠条目的判定**唯一真源** = ``planning/schedule_items.py``，
# 供本模块的 M9 赖床工具与 ``auto_scheduler.py`` 的 B2 生成锚点共用，避免两处口径漂移。


def parse_nap_target(value: Any) -> Optional[int]:
    """赖床时间解析（§4.4「解析兜底」）：容忍 ``HH:MM`` / ``H:MM`` / 纯小时数。

    纯小时数按整点（``"9"`` → 09:00）；全角冒号容错；非法/缺参/越界一律返回 None
    （调用方据此给友好错误，**绝不抛异常**）。
    """
    raw = str(value if value is not None else "").strip().replace("：", ":")
    if not raw:
        return None
    head, sep, tail = raw.partition(":")
    try:
        hour = int(head)
        if not sep:
            minute = 0                       # 纯小时数 → 整点
        else:
            if not tail.strip():
                return None                  # "9:" 缺分钟
            minute = int(tail)
    except (TypeError, ValueError):
        return None
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour * 60 + minute
    return None


def normalize_hhmm(value: Any, default: str = DEFAULT_LIE_IN_MAX_UNTIL) -> str:
    """规范成 ``"HH:MM"`` 文本（非法一律回退 ``default``）。

    供钳制上限的**展示与判定同源**：提醒文本里的「最晚 {lie_in_max_until}」与工具侧实际
    钳到的分钟数由同一个函数产出，不会出现"提示 12:00、实际钳到别的值"。
    """
    minute = parse_nap_target(value)
    if minute is None:
        minute = parse_nap_target(default)
    if minute is None:                       # default 也被写坏 → 退到规格缺省
        minute = parse_nap_target(DEFAULT_LIE_IN_MAX_UNTIL) or 12 * 60
    return f"{minute // 60:02d}:{minute % 60:02d}"


def lie_in_max_minutes(value: Any) -> int:
    """``lie_in_max_until`` 配置值 → 当天分钟数（非法回退缺省 12:00）。"""
    return parse_nap_target(normalize_hhmm(value)) or 12 * 60


class ToolsService:
    """承载 3 个 LLM Tool 的业务逻辑（get_schedule_info / update_schedule / nap_lie_in）。"""

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
            # PR1 P0 修复（2026-10-26），计划书 §三十四 / 定稿 §2 第 0.7 项：regenerate 链
            # （/lbm_plan regenerate 与 update_schedule 的「今天」分支两个调用点自动受益）
            # 跟随 [schedule].spine_mode——开启时生成 3-6 条日意图，不再恒 daily（与
            # auto_scheduler 自动生成侧同口径）；generator 侧 **kwargs 透传，零改动。
            "mode": "spine" if bool(getattr(cfg, "spine_mode", False)) else "daily",
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
            return {"type": "schedule_day", "content": f"今日 ({today}) 暂无日程。"}
        lines = [f"今日完整日程 ({today})"]
        for item in items:
            start = int(item.get("start_minute") or 0)
            end = int(item.get("end_minute") or 0)
            # 跨午夜条目（end>1440）标「次日」，不再渲染成 31:00 这类帧外钟点
            lines.append(f"  [{format_day_minute(start)}-{format_day_minute(end)}] "
                         f"{item.get('name', '未知')}")
        return {"type": "schedule_day", "content": "\n".join(lines)}

    def _view_goals(self) -> Dict[str, Any]:
        """view=goals：进行中的目标列表（只读 get_active_goals，不进行任何写操作）。"""
        goals = get_goal_manager().get_active_goals(chat_id="global")
        if not goals:
            return {"type": "schedule_goals", "content": "当前没有进行中的目标"}
        lines = [f"进行中的目标（{len(goals)} 个）"]
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
                window = f"{format_day_minute(tw[0])}-{format_day_minute(tw[1])} "
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
                target_date = str(parsed.get("date") or today_str)
                if target_date <= today_str:
                    return {
                        "type": "schedule_unchanged",
                        "content": f"注意：未来约定日期 {target_date} 无效，日程不变",
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
                        f"已记录 {target_date} 的约定：{parsed.get('title')}\n"
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
                    f"已根据请求调整今日日程，共 {len(items)} 个活动\n"
                    f"{('理由：' + reason) if reason else ''}"
                ).strip(),
            }

        except Exception as exc:
            logger.error(f"update_schedule 失败: {exc}", exc_info=True)
            return {"type": "error", "content": f"更新日程失败: {exc}"}

    # ------------------------------------------------------------
    # Tool 3：nap_lie_in（赖床：推迟今日睡眠条目的结束时间；§4.3/§4.4）
    # ------------------------------------------------------------

    def _emotion_bundle(self) -> Optional[Any]:
        """emotion 服务束（``plugin._emotion``）。

        ⚠ 单向依赖：planning **不 import** emotion（避免循环依赖），只按鸭子类型取束上用到的
        三个入口：``in_night_sleep_cycle`` / ``extend_night_sleep_until`` / ``note_nap_lie_in_applied``；
        取不到（插件未装配 emotion，或单测骨架插件）→ 返回 None，工具按"非睡眠周期"拒绝。
        """
        return getattr(self._plugin, "_emotion", None)

    def _lie_in_max_minutes(self) -> int:
        """[schedule_advanced] lie_in_max_until 的分钟数（非法回退 12:00；钳制与提示同源）。"""
        return lie_in_max_minutes(getattr(self._advanced_cfg(), "lie_in_max_until",
                                          DEFAULT_LIE_IN_MAX_UNTIL))

    @staticmethod
    def _find_sleep_item(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """今日日程里的**夜晚睡眠条目**（§4.4「读今日日程找 sleep 条目（goal_type=sleep）」）。

        判定与读数委托**共享口径** ``planning/schedule_items.py``（M10 第 6 段抽出）：
        主判 ``goal_type == "sleep"``（规格口径）；取不到时按名称含夜晚睡眠词兜底——
        依据同 ``emotion/service.py`` 的 ``_is_sleep_item``（M5 实测：生产条目是
        ``goal_type=daily_routine`` + 名称「睡觉」）。两轮判定都只认夜间词，
        午休/午睡/小憩/打盹不算（§八.7 / C4-补充：午休本轮不动）。
        """
        rows = [item for item in (items or []) if isinstance(item, dict)]  # 本工具只吃 DB 行
        return find_night_sleep_item(rows)

    @staticmethod
    def _nap_end_datetime(now: datetime, minute: int) -> datetime:
        """把"当天分钟数"落成 ``now`` 之后最近的时刻（跨午夜自动 +1 天）。"""
        end = now.replace(hour=(int(minute) // 60) % 24, minute=int(minute) % 60,
                          second=0, microsecond=0)
        return end if end > now else end + timedelta(days=1)

    # ── P6 修复（W2）：赖床延长窗口内的清醒条目顺延 ─────────────────────
    # 缺陷：lie-in 只把 sleep 条目 end 推后时，与延长窗口重叠的**非睡眠**条目会在下一 tick
    # 被 ``find_current_activity(include_sleep=True)`` 命中（按 start 升序取首条匹配）→ 醒沿
    # 判定（emotion/service.py ``_check_and_overhaul``）第一析支 ``awake_activity`` 抢先为真
    # → woke_up 仍按旧端点触发（emotion 侧 ``_sleep_period`` 延长其实已生效，窗口统计却是
    # 延长后的值——登记行 11:06:53 woke_up + sleep_hours=1.417 即此形态）。
    # 修法：写回时把这类条目**顺延**到延长端点之后，只动赖床写回路径（生成/归一化契约不动）。

    @staticmethod
    def _postpone_items_for_lie_in(items: List[Dict[str, Any]], lo: int, hi: int) -> int:
        """把赖床延长窗口 ``[lo, hi)`` 内重叠的**非睡眠**条目顺延到 ``hi`` 之后（就地改）。

        红线：
        - 只动与窗口重叠的条目（``start < hi`` 且 ``end > lo``），其余原样；
        - 保留时长，``end`` 封顶 :data:`DAY_MINUTES`（24:00）——不产生跨午夜/``start>end`` 倒挂；
        - 夜晚睡眠条目（共享口径 :func:`find_night_sleep_item`）**不动**：它的 ``end`` 由调用方
          单独写（M9 as-built 口径不变，倒挂面不扩大）。

        Returns:
            被顺延的条目数（0 = 无需改动，调用方跳过写回）。
        """
        moved = 0
        for item in items or []:
            if not isinstance(item, dict) or is_night_sleep_item(item):
                continue
            try:
                start = int(item.get("start_minute") or 0)
                end = int(item.get("end_minute") or 0)
            except (TypeError, ValueError):
                continue
            if end <= start or start >= hi or end <= lo:
                continue
            item["start_minute"] = int(hi)
            item["end_minute"] = min(DAY_MINUTES, int(hi) + (end - start))
            moved += 1
        return moved

    def _postpone_wake_day_schedule(self, query: Optional[Any], day: str, lo: int, hi: int) -> int:
        """跨午夜赖床（凌晨）：**醒来日**日程的顺延写回（醒沿查询读的正是那一天的日程）。

        入睡日的 sleep 条目属于入睡日（:meth:`_nap_schedule_date`），凌晨醒沿查询却按**日历日**
        取醒来日日程——与延长窗口重叠的清醒条目在那份日程里，须单独顺延。写回走
        ``ScheduleQuery.db`` 的 ``replace_day_schedule``（即 ``apply_schedule_items`` 的落库底层；
        该日条目从库中原样带回元数据，且**不消费**未来约定）；失败静默（不影响本次赖床）。
        v4 进账血缘（§31.2，R7 2026-10-23）：写库前先经进账漏斗 rotate，行级盖戳
        ``entry_reason="tool_edit"``（与入睡日当日写回同 reason——同属一次赖床工具修改）。
        """
        try:
            if query is None or not callable(getattr(query, "get_day_schedule", None)):
                return 0
            items = [dict(row) for row in (query.get_day_schedule(day) or [])
                     if isinstance(row, dict)]
            if not items:
                return 0
            moved = self._postpone_items_for_lie_in(items, lo, hi)
            db = getattr(query, "db", None)
            if not moved or db is None:
                return 0
            stamps = enter_schedule_cycle(
                getattr(self._plugin, "_data_dir", None), day, items,
                reason=REASON_TOOL_EDIT,
                decision_logger=getattr(self._plugin, "_decision_logger", None),
                **entry_context_from_plugin(self._plugin),
            )
            db.replace_day_schedule(
                day, items, entry_reason=REASON_TOOL_EDIT,
                cycle_start_ts=(stamps[0] if stamps else None),
                cycle_end_ts=(stamps[1] if stamps else None),
            )
            logger.info("赖床顺延：%s 的 %d 个日程条目已移到 %s 之后",
                        day, moved, format_day_minute(hi))
            return moved
        except Exception as exc:
            logger.warning("赖床顺延失败（不影响本次赖床）: %s", exc, exc_info=True)
            return 0

    def _nap_schedule_date(self, now: datetime) -> str:
        """"今日日程"的日期（F16/B4：**新一天起点 = [schedule] wake_time 锚点**）。

        夜间睡眠条目属于**入睡日**（跨午夜条目保留原时长，如 23:00 入睡 + 8h ⇒
        ``23:00–次日 07:00``——2026-10-10 N2/N4 起不再被作息锚点截断成 ``23:00–23:15``），
        故凌晨（早于 wake_time 锚点的跨午夜段）仍读**昨天**那条日程，否则读今天；夜猫子作息
        （sleep ≤ wake，同日）一律读今天。取不到锚点一律按"今天"（缺省 07:00/23:00）。
        """
        sleep_min = parse_hhmm(self._schedule_cfg().sleep_time, 23 * 60)
        wake_min = parse_hhmm(self._schedule_cfg().wake_time, 7 * 60)
        cur = now.hour * 60 + now.minute
        if sleep_min > wake_min and cur < wake_min:
            return (now - timedelta(days=1)).strftime("%Y-%m-%d")
        return now.strftime("%Y-%m-%d")

    async def nap_lie_in(self, time: str = "", now: Optional[datetime] = None) -> Dict[str, Any]:
        """把**今日 sleep 条目**的结束时间推到目标时刻（附录 §4.4，用户裁决 C4；M9 落地）。

        口径（规格原文逐条）：

        1. **解析容错**：``HH:MM`` / ``H:MM`` / 纯小时数（``"9"`` → 09:00）；非法/缺参 →
           友好错误 + 格式示例，**不抛异常**；
        2. **非睡眠周期调用 → 拒绝**：经 ``plugin._emotion`` 服务束查询「是否处于夜间睡眠
           周期」（白天/午休/未装配 emotion 一律拒绝），话术「你现在还不能想要赖床」；
        3. **上限钳制**：目标 > ``[schedule_advanced] lie_in_max_until``（默认 12:00）→ 钳到该值；
        4. **修改日程**：找**今日日程**的 sleep 条目（"今日"按 [schedule] wake_time 锚点定义的新一天：
           凌晨仍属入睡日，见 :meth:`_nap_schedule_date`）并把 ``end_minute`` 推到目标，经**共享底层**
           ``ScheduleGenerator.apply_schedule_items(items, today, consume_commitments=False)``
           （:274）写回——不消费未来约定、不触发重生成；无 sleep 条目或写回失败 →
           失败文本且**不计数**；**P6（W2）**：与延长窗口重叠的**非睡眠**条目一并**顺延**到延长
           端点之后（同日 = 同一次写回；跨午夜 = 醒来日日程另写一次），否则下一 tick 的醒沿判定
           会先命中这些清醒条目（``awake_activity``）→ 醒沿不随端点推迟；
        5. **通知 emotion 延长本周期窗口结束到目标时刻**（否则醒沿会在 wake 锚点照常触发）；
        6. **计数**：调用**并成功修改日程** → emotion 侧「有效提醒次数」+1（按 ``cycle_key``，
           内存）；未调用/失败不计次；额度用尽只停提醒、**不阻止**本工具（本方法不判额度）。
           口径澄清（附录 §六.补 C4，执行既有 C4「成功修改日程才计次」）：目标 ≤ 当前醒沿时
           **先经 ``night_sleep_end_minute`` 预检**（写回前拒绝，避免日程被误写）且
           ``extend_night_sleep_until`` 返回 False（双保险）→ 均**不计次**，返回友好提示而非
           成功文案。

        Args:
            time: 目标时间（"HH:MM" / "H:MM" / 纯小时数）。
            now: 当前时刻（缺省 ``datetime.now()``；测试注入时钟用）。

        Returns:
            Tool 返回载荷 ``{"type", "content"}``（宿主按 type 渲染）；任何异常都吞成
            失败文案，绝不上抛（§八.2 可空降级）。
        """
        try:
            if not self._module_enabled():
                return {"type": "error", "content": "日程规划模块未启用（配置 [日程系统] enabled）"}
            # 1) 解析容错（先解析：缺参/非法给格式示例，不惊动任何状态）
            target_minute = parse_nap_target(time)
            if target_minute is None:
                return {"type": "nap_error",
                        "content": prompt_of(self._plugin, "nap_tool_bad_time")}
            # 2) 非睡眠周期调用 → 拒绝（emotion 侧门控；无 emotion 束同样拒绝）
            emotion = self._emotion_bundle()
            if emotion is None or not emotion.in_night_sleep_cycle():
                return {"type": "nap_rejected",
                        "content": prompt_of(self._plugin, "nap_tool_denied")}
            current = now or datetime.now()
            today = self._nap_schedule_date(current)
            # 3) 上限钳制
            limit_minute = self._lie_in_max_minutes()
            clamped = min(int(target_minute), int(limit_minute))
            # 3.5) 预检（附录 §六.补 C4）：目标不晚于当前醒沿 → 不写日程、不计次
            #      （否则会出现"文案说没改动、库里 sleep 条目已被写坏"）；权威判定仍在
            #      写回后的 extend_night_sleep_until（双保险）
            wake_edge = emotion.night_sleep_end_minute()
            if wake_edge is not None and clamped <= wake_edge:
                return {"type": "nap_error",
                        "content": "这个时间不比现在的起床时间晚，起床时间没有改动，就不多睡啦。"}
            # 4) 写回今日日程（共享底层；只动 sleep 条目，其余条目原样保留）
            query = self._schedule_query()
            items = list(query.get_day_schedule(today)) if query is not None else []
            item = self._find_sleep_item(items)
            if item is None:
                return {"type": "nap_error",
                        "content": "今天没有找到睡觉日程，暂时没法往后推起床时间。"}
            item["end_minute"] = int(clamped)
            # P6：延长窗口内的清醒条目顺延到延长端点之后（否则下一 tick 醒沿被它们抢占）。
            # 窗口在"醒来日"帧内的起点：同日 → 当前时刻；跨午夜 → 该日 0 点（见 _nap_end_datetime）。
            end_dt = self._nap_end_datetime(current, clamped)
            wake_day = end_dt.strftime("%Y-%m-%d")
            window_lo = current.hour * 60 + current.minute if wake_day == today else 0
            if wake_day == today:
                self._postpone_items_for_lie_in(items, window_lo, clamped)
            generator = self._get_generator()
            if generator is None:
                return {"type": "error", "content": "日程生成器不可用（插件未完成初始化）"}
            # §31.2 进账血缘（R7 2026-10-23）：赖床工具修改以 tool_edit 原因进账（行级盖戳 +
            # rotate 由共享漏斗完成；写库前轮换 best-effort）。
            written = generator.apply_schedule_items(items, today, consume_commitments=False,
                                                     reason=REASON_TOOL_EDIT)
            if not written:
                return {"type": "nap_error", "content": "日程写回失败，起床时间没有改动。"}
            if wake_day != today:
                # 跨午夜（凌晨赖床）：醒来日日程单独顺延（唤醒日 ≠ 入睡日，须另一份写回）
                self._postpone_wake_day_schedule(query, wake_day, window_lo, clamped)
            # 5) + 6) 成功才通知 emotion：延长本周期窗口端点（醒沿推迟）并计一次有效提醒
            extended = emotion.extend_night_sleep_until(end_dt)
            if not extended:
                # 附录 §六.补 C4 口径澄清（执行既有 C4「成功修改日程才计次」）：目标不比当前醒沿晚
                # → 窗口端点未变（未实际延长）→ 不计次，返回友好提示
                return {"type": "nap_error",
                        "content": "这个时间不比现在的起床时间晚，起床时间没有改动，就不多睡啦。"}
            emotion.note_nap_lie_in_applied()
            hhmm = f"{int(clamped) // 60:02d}:{int(clamped) % 60:02d}"
            note = "" if clamped == target_minute else f"（超过最晚时间，已按 {hhmm} 算）"
            return {"type": "nap_lie_in",
                    "content": f"好，那就再睡一会儿——今天的起床时间已经推到 {hhmm}。{note}"}
        except Exception as exc:
            logger.error(f"nap_lie_in 失败: {exc}", exc_info=True)
            return {"type": "nap_error", "content": f"赖床设置失败: {exc}"}
