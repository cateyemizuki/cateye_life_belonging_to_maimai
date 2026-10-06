"""PlanningComponentsMixin —— planning 模块的 SDK 组件注册（装饰器方法）。

用法（整合者在 plugin.py 中执行，见 INTEGRATION.md）::

    class CateyeSuitePlugin(PlanningComponentsMixin, MaiBotPlugin):
        ...

约定：
- Python 方法名一律 ``plan_`` 前缀；组件名（装饰器第一个参数）跟源插件语义；
- Hook 返回契约照抄源插件：``{"action": "continue"}`` 或
  ``{"action": "continue", "modified_kwargs": {**kwargs, ...}}`` 全量回传；
- Command 返回 (success, response, intercept) 三元组，回复显式 ``ctx.send.text``；
- 所有方法开头判空：``svc = getattr(self, "_planning", None)``——整合前
  self._planning 不存在时返回安全默认值。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List

from maibot_sdk import API, Command, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


class PlanningComponentsMixin:
    """日程规划组件（2 Tool + 1 Command + 1 API + 2 HookHandler）。"""

    # 供 SDK / 调试用的组件清单说明（不影响注册）
    planning_components_info: ClassVar[Dict[str, str]] = {
        "cateye_test_get_schedule_info": "Tool：日程/活动/目标的只读查询（view=status/current/lover_day/goals）",
        "cateye_test_update_schedule_v4": "Tool：自然语言日程变更（唯一日程写入口，角色裁判 today/future/reject）",
        "cateye_test_planning_v4": "Command：/cateye_test_plan 或 /cateye_test_规划（list/regenerate/delete/clear/help）",
        "get_current_activity": "API：当前活动快照（跨插件可调）",
        "schedule_inject_v4": "HookHandler：maisaka.planner.before_request 日程注入",
        "schedule_inject_replyer_v4": "HookHandler：maisaka.replyer.before_request 活动提醒注入",
    }

    # ============================================================
    # Tools（2 个 LLM 工具）
    # ============================================================

    @Tool(
        "cateye_test_get_schedule_info",
        description="查询日程/活动/目标的只读信息（四视图）",
        parameters={
            "view": {"type": "string", "description": "status=今日日程状态总览（默认）| current=当前活动 | lover_day=今日完整日程 | goals=活动目标列表", "required": False},
        },
    )
    async def plan_tool_schedule_info(self, **kwargs: Any) -> Dict[str, Any]:
        """日程/活动/目标只读查询工具入口（四视图，view 缺省/非法回落 status）。"""
        svc = getattr(self, "_planning", None)
        if svc is None:
            return {"type": "error", "content": "插件未启用"}
        return await svc.tools.get_schedule_info(**kwargs)

    @Tool(
        "cateye_test_update_schedule_v4",
        description="根据自然语言请求维护日程：角色裁判决定接受/未来约定/拒绝；接受时调整今日日程，"
                    "未来约定写入候选清单。唯一日程写入口。",
        parameters={
            "description": {"type": "string", "description": "日程变更的自然语言描述（如\"下午两点一起学习\"、\"周末打游戏\"）", "required": True},
        },
    )
    async def plan_tool_update_schedule(self, **kwargs: Any) -> Dict[str, Any]:
        """角色裁判式日程更新工具入口。"""
        svc = getattr(self, "_planning", None)
        if svc is None:
            return {"type": "error", "content": "插件未启用"}
        return await svc.tools.update_schedule(**kwargs)

    # ============================================================
    # Command（/cateye_test_plan 或 /cateye_test_规划）
    # ============================================================

    @Command(
        "cateye_test_planning_v4",
        description="日程规划系统管理命令（支持 /cateye_test_plan 与 /cateye_test_规划）",
        # 用负向前瞻代替 ^ 锚点——回复引用场景下命令不在文本开头；
        # (?<!\S) 同时避免命中行中片段（照抄源插件 pattern）
        pattern=r"(?P<planning_cmd>(?<!\S)/cateye_test_(?:plan|规划)(?:\s.*)?)$",
    )
    async def plan_command_planning(
        self,
        text: str = "",
        stream_id: str = "",
        user_id: str = "",
        platform: str = "",
        group_id: str = "",
        matched_groups: Any = None,
        is_local_operator: bool = False,
        **kwargs: Any,
    ) -> tuple:
        """``/cateye_test_plan`` 命令入口，转发给 CommandService。"""
        del kwargs
        svc = getattr(self, "_planning", None)
        if svc is None:
            if stream_id:
                await self.ctx.send.text("插件未启用", stream_id)
            return False, "插件未启用", True
        return await svc.commands.execute(
            text=text,
            stream_id=stream_id,
            user_id=user_id,
            platform=platform,
            group_id=group_id,
            matched_groups=matched_groups if isinstance(matched_groups, dict) else {},
            is_local_operator=bool(is_local_operator),
        )

    # ============================================================
    # API（对外暴露，可被其他插件通过 ctx.api.call 调用）
    # ============================================================

    @API(
        "get_current_activity",
        description="返回当前时间段最新的日程活动快照（含描述、时间窗口、即将到来活动）",
        version="1",
        public=True,
    )
    async def plan_api_get_current_activity(
        self,
        chat_id: str = "global",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """对外 API：当前活动快照（行为转 InjectService.get_current_activity_snapshot）。

        其他插件调用方式::

            snapshot = await self.ctx.api.call(
                "<plugin_id>.get_current_activity", chat_id="global",
            )
            if snapshot["has_activity"]:
                print(snapshot["activity"]["name"])
        """
        del kwargs
        svc = getattr(self, "_planning", None)
        if svc is None:
            return {
                "has_activity": False,
                "activity": None,
                "next_activities": [],
                "as_of": "",
                "timezone": "",
                "error": "plugin_not_initialized",
            }
        return await svc.get_current_activity_snapshot(chat_id or "global")

    # ============================================================
    # HookHandler（注入入口 ×2）
    # ============================================================

    @HookHandler(
        "maisaka.planner.before_request",
        name="schedule_inject_v4",
        description="在 Maisaka 向 LLM 发起规划请求前注入当前日程信息",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def plan_hook_inject_planner(
        self,
        items: List[Dict[str, Any]] | None = None,
        messages: List[Dict[str, Any]] | None = None,
        item_schema_version: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """日程注入 Hook 入口，转发给 InjectService。

        MaiBot 1.2.x payload 字段为 ``items``（ContextItem 快照投影），更早版本为
        ``messages``。两者兼容：注入结果回写到入参实际使用的键，其余 kwargs
        （tool_definitions / item_schema_version 等）全量回传。
        """
        svc = getattr(self, "_planning", None)
        if svc is None or not self.config.schedule.inject_into_planner:
            return {"action": "continue"}
        return await svc.inject.inject_into_planner_messages(
            items=items,
            messages=messages,
            item_schema_version=item_schema_version,
            session_id=session_id,
            **kwargs,
        )

    @HookHandler(
        "maisaka.replyer.before_request",
        name="schedule_inject_replyer_v4",
        description="在 Maisaka replyer 调 LLM 前把当前活动注入到 extra_prompt（突出活人感，不要主动提及）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def plan_hook_inject_replyer(
        self,
        session_id: str = "",
        attempt: int = 1,
        extra_prompt: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """replyer 注入 Hook 入口，转发给 InjectService（协作式保留上游 extra_prompt）。"""
        svc = getattr(self, "_planning", None)
        if svc is None or not self.config.schedule.inject_into_replyer:
            return {"action": "continue"}
        return await svc.inject.inject_into_replyer_extra_prompt(
            session_id=session_id,
            attempt=attempt,
            extra_prompt=extra_prompt,
            **kwargs,
        )
