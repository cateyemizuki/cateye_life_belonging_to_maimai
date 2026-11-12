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

from maibot_sdk import API, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder


class PlanningComponentsMixin:
    """日程规划组件（2 Tool + 0 独立 Command + 1 API + 2 HookHandler）。

    PR8 3.0.0 清退（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）：
    旧别名 Tool ``lbm_update_schedule_v4`` 与 M9 赖床 Tool ``lbm_nap_lie_in``
    已删除；保留 ``schedule_inject`` / ``schedule_inject_replyer``。
    2026-11-13 命令体系重构：``lbm_planning`` / ``lbm_init`` 两个 @Command 注册已删除，
    日程组/运维组由双语根命令 ``/lbm`` / ``/麦麦生活`` 经注册表引用本 Mixin 的 handler。
    """

    # 供 SDK / 调试用的组件清单说明（不影响注册）
    planning_components_info: ClassVar[Dict[str, str]] = {
        "lbm_get_schedule_info": "Tool：日程/活动/目标/运行时的只读查询"
                                 "（view=status/current/next/episodes/spine/intents）",
        "lbm_update_schedule": "Tool（规范名）：自然语言日程变更（唯一日程写入口；"
                               "角色裁判 today/future/reject；写 spine 意图/"
                               "pending_commitment，硬约定代码强制 hardness=hard）",
        "lbm_command_root": "Command：双语双根 /lbm 与 /麦麦生活（日程组 plan/*、运维组 ops/init）",
        "get_current_activity": "API：当前活动快照（跨插件可调；日程未启用时 has_activity=False）",
        "schedule_inject": "HookHandler：maisaka.planner.before_request 日程注入",
        "schedule_inject_replyer": "HookHandler：maisaka.replyer.before_request 活动提醒注入",
    }

    # ============================================================
    # Tools（2 个 LLM 工具）
    # ============================================================

    @Tool(
        "lbm_get_schedule_info",
        description="查询日程/活动/目标的只读信息（多视图）",
        parameters={
            "view": {"type": "string", "description": "status=今日状态总览（默认）| current=当前活动 | "
                                                     "next=接下来两条 | episodes=今日实际 episode | "
                                                     "spine=今日意图与硬约定 | intents=spine 别名",
                     "required": False},
        },
    )
    async def plan_tool_schedule_info(self, **kwargs: Any) -> Dict[str, Any]:
        """日程/活动/目标只读查询工具入口（view 缺省/非法回落 status）。"""
        svc = getattr(self, "_planning", None)
        if svc is None:
            return {"type": "error", "content": "插件未启用"}
        return await svc.tools.get_schedule_info(**kwargs)

    @Tool(
        "lbm_update_schedule",
        description="根据自然语言请求维护日程：角色裁判决定接受/未来约定/拒绝；接受时更新今日意图，"
                    "未来约定写入约定清单。唯一日程写入口。",
        parameters={
            "description": {"type": "string", "description": "日程变更的自然语言描述（如\"下午两点一起学习\"、\"周末打游戏\"）", "required": True},
            "intent": {"type": "object", "description": "可选：今日意图的归一字段（name/goal_type/priority/hardness/window 等）；缺省时按 description 判定", "required": False},
            "commitment": {"type": "object", "description": "可选：未来约定的归一字段（date/time/title/hardness）；缺省时按 description 判定", "required": False},
        },
    )
    async def plan_tool_update_schedule(self, **kwargs: Any) -> Dict[str, Any]:
        """角色裁判式日程更新工具入口（规范名）。"""
        svc = getattr(self, "_planning", None)
        if svc is None:
            return {"type": "error", "content": "插件未启用"}
        return await svc.tools.update_schedule(**kwargs)

    # ============================================================
    # 日程组 / 运维组 handler（2026-11-13 双语根命令注册表引用）
    # ============================================================

    async def cmd_plan_entry(self, cmd: Any, args: list[str], **kwargs: Any) -> tuple:
        """plan/* 与 status/schedule → 既有 CommandService（含模块开关/权限/白名单）。

        注册表 entry 的 ``key`` 已把 status/schedule 归一为 ``list``；业务分支、
        输出格式与权限口径完全沿用 CommandService。
        """
        stream_id = str(kwargs.get("stream_id", "") or "")
        svc = getattr(self, "_planning", None)
        if svc is None:
            if stream_id:
                await self.ctx.send.text("插件未启用", stream_id)
            return False, "插件未启用", True
        return await svc.commands.execute_action(
            action=str(getattr(cmd, "key", "") or ""),
            args=[str(item) for item in args],
            stream_id=stream_id,
            user_id=str(kwargs.get("user_id", "") or ""),
            is_local_operator=bool(kwargs.get("is_local_operator", False)),
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
        """内部兼容入口（不再注册）；新用户面走 ``cmd_plan_entry``。"""
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

    async def cmd_ops_init(self, cmd: Any, args: list[str], **kwargs: Any) -> tuple:
        """ops/init → 统一初始化驱动；args 双语已在分发器归一。"""
        del cmd
        # 兼容旧三参数（emotion/events/status）与新增七字段/全部。
        arg = " ".join(str(item).strip().lower() for item in args).strip()
        return await self.plan_command_init(
            matched_groups={"init_args": arg}, **kwargs)

    async def plan_command_init(
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
        """``/lbm ops init [字段|all|status]``（2026-11-13 自动初始化统一入口）。

        顺序：先 ``[plugin].enabled``，再管理员（``admin_users`` / 本机控制台）。
        - 缺省 / ``all``：七字段全量（幂等；显式字段才强制重 init）；
        - ``emotion|events|state_sleep|frequency|video_interest|
          companion_disposition|activity_preferences``：只跑该字段并强制重 init；
        - ``status``：只读 ``init_registry.json`` 逐字段展示，不触发任何 init。
        **手动不受 attempts 退避限制**；自动驱动（``plugin.on_load``）才受退避。
        """
        del kwargs, platform, group_id
        from tools.init.auto_init import run_manual, status_text
        from tools.init.manifest import INIT_FIELDS

        groups = matched_groups if isinstance(matched_groups, dict) else {}
        raw = str(groups.get("init_cmd") or text or "").strip()
        arg = str(groups.get("init_args") or "").strip().lower()
        if not arg and raw:
            parts = raw.split()
            if len(parts) > 1:
                arg = parts[1].strip().lower()
        valid_fields = tuple(INIT_FIELDS)
        if arg not in valid_fields and arg not in ("", "all", "status"):
            arg = "__invalid__"

        async def _reply(message: str) -> None:
            if stream_id:
                try:
                    await self.ctx.send.text(message, stream_id)
                except Exception:
                    pass

        enabled = True
        try:
            enabled = bool(self.config.plugin.enabled)
        except Exception:
            enabled = True
        if not enabled:
            await _reply("插件未启用（配置 [plugin] enabled = false），初始化不可用")
            return True, "插件未启用", True
        if not self._init_is_admin(user_id, is_local_operator, self._init_admin_users()):
            await _reply("你不是管理员哦~只有管理员才能执行初始化（本机控制台不受限）")
            return True, "没有权限", True

        if arg == "__invalid__":
            await _reply(
                "参数无效。用法：/lbm ops init [字段|all|status]\n"
                "字段：" + " / ".join(valid_fields) + "\n"
                "中文：/麦麦生活 运维 初始化 [字段|全部|状态]"
            )
            return True, "参数无效", True
        if arg == "status":
            await _reply(status_text(self) or "暂无初始化状态")
            return True, "初始化状态", True

        svc = getattr(self, "_planning", None)
        init_svc = getattr(svc, "init", None) if svc is not None else None
        if init_svc is None:
            await _reply("初始化服务未装配（插件未完成加载）")
            return True, "初始化服务未装配", True
        fields = None if arg in ("", "all") else [arg]
        try:
            report = await run_manual(self, fields)
            await _reply(report.text or "初始化完成")
            return True, "初始化完成", True
        except Exception as exc:
            await _reply(f"初始化失败：{exc}")
            return True, "初始化失败", True

    def _init_admin_users(self) -> Any:
        """``[admin].admin_users``（读不到 → 空列表 = 仅本机控制台可执行）。"""
        try:
            return list(getattr(getattr(self.config, "admin", None), "admin_users", None) or [])
        except Exception:
            return []

    @staticmethod
    def _init_is_admin(user_id: str, is_local_operator: bool = False,
                       admin_users: Any = None) -> bool:
        """``/lbm ops init`` 权限：本机控制台直通；否则精确匹配 ``admin_users``（末段 ID）。"""
        if is_local_operator:
            return True

        def _id_part(raw: Any) -> str:
            return str(raw or "").strip().rsplit(":", 1)[-1]

        candidate = _id_part(user_id)
        if not candidate:
            return False
        return any(_id_part(entry) == candidate for entry in (admin_users or ()))

    # ============================================================
    # API（对外暴露，可被其他插件通过 ctx.api.call 调用）
    # ============================================================

    @API(
        "get_current_activity",
        description="返回当前活动快照（读 runtime episode/ReadModel 视图，与注入同口径）",
        version="1",
        public=True,
    )
    async def plan_api_get_current_activity(
        self,
        chat_id: str = "global",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """对外 API：当前活动快照（行为转 InjectService.get_current_activity_snapshot）。

        PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-5/UQ-6 裁定）：
        - **读 runtime episode/ReadModel 视图**（与注入同口径），不从 schedule_items
          时间窗编造「正在做」；
        - ``[schedule].enabled=false``（日程未启用）→ ``has_activity=False``，不读残留
          快照冒充当前活动；runtime 未就绪 → 如实「暂不可用」（``error`` 字段标注）。

        其他插件调用方式::

            snapshot = await self.ctx.api.call(
                "<plugin_id>.get_current_activity", chat_id="global",
            )
            if snapshot["has_activity"]:
                print(snapshot["activity"]["name"])
        """
        del kwargs
        if not bool(getattr(getattr(self.config, "schedule", None), "enabled", True)):
            return {
                "has_activity": False,
                "activity": None,
                "next_activities": [],
                "as_of": "",
                "timezone": "",
                "error": "schedule_disabled",
            }
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
        name="schedule_inject",
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
        name="schedule_inject_replyer",
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
