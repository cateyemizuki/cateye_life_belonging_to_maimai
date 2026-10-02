"""``/cateye_test_plan`` 命令的业务实现（来源：自主规划插件 services/command_service.py，移植版）。

通过 ``self._plugin.ctx.send.text(...) / ctx.send.forward(...)`` 发送消息。

子命令：list / regenerate / delete / clear / help。
- list：今日日程（图片 + 详细文字合并转发；无日程时自动先生成）；
- 图片绘制失败/超时**静默**降级为纯文字（不提示失败）；
- send.forward / send.image 能力宿主未声明时自动回退纯文本。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .config import ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .schedule_image import ScheduleImageGenerator
from .stream_filter import is_stream_allowed
from .time_utils import TimezoneManager, format_minutes_to_time, get_time_window_from_goal, strip_tz

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


# 目标类型 → emoji 映射
_GOAL_TYPE_EMOJI: Dict[str, str] = {
    "meal": "🍽️",
    "study": "📚",
    "entertainment": "🎮",
    "daily_routine": "🏠",
    "social_maintenance": "💬",
    "learn_topic": "📖",
    "exercise": "🏃",
    "rest": "💤",
    "free_time": "🌟",
}

_WEEKDAY_NAMES: Dict[int, str] = {
    0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日",
}


class CommandService:
    """规划命令处理服务。"""

    def __init__(self, plugin: Any, tools_service: Optional[Any] = None) -> None:
        """
        Args:
            plugin: 当前插件实例
            tools_service: ToolsService 实例（list 无日程时触发生成 / regenerate 用）
        """
        self._plugin = plugin
        self._tools_service = tools_service

    # ------------------------------------------------------------
    # 配置访问辅助
    # ------------------------------------------------------------

    @property
    def _enable_detailed_description(self) -> bool:
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig).enable_detailed_description

    def _schedule_cfg(self) -> ScheduleSectionConfig:
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig)

    def _make_tz_manager(self) -> TimezoneManager:
        return TimezoneManager(self._schedule_cfg().timezone)

    def _tools_svc(self) -> Optional[Any]:
        return self._tools_service or getattr(self._plugin, "_planning_tools", None)

    def _admin_cfg(self) -> Any:
        return getattr(self._plugin.config, "admin", None)

    # ------------------------------------------------------------
    # 命令分发入口
    # ------------------------------------------------------------

    async def execute(
        self,
        text: str = "",
        stream_id: str = "",
        user_id: str = "",
        platform: str = "",
        group_id: str = "",
        matched_groups: Dict[str, str] | None = None,
        is_local_operator: bool = False,
    ) -> Tuple[bool, str, bool]:
        """命令执行入口，按子命令分发。

        Returns:
            (success, response_text, intercept_message) 三元组。
        """
        del platform, group_id  # 当前命令未使用，保留参数防止未来扩展
        groups = matched_groups or {}
        command_text = str(groups.get("planning_cmd", text) or "").strip()
        parts = command_text.split()

        # 模块独立总开关：停用期间命令不可用（提示配置位置）
        schedule_cfg = getattr(self._plugin.config, "schedule", None)
        if not bool(getattr(schedule_cfg, "enabled", True)):
            await self._send(stream_id, "🚫 日程规划模块已停用（配置 [日程系统] enabled = false）")
            return True, "模块未启用", True

        # 权限检查：默认拒绝——admin_users 未配置时仅本机控制台可用
        if not self._check_permission(user_id, is_local_operator):
            if not (self._admin_cfg() and self._admin_cfg().admin_users):
                await self._send(
                    stream_id,
                    "🚫 尚未配置管理员：请在插件配置 [管理与日志] 的 admin_users 中"
                    "添加你的用户 ID 后再使用 /cateye_test_plan（本机控制台不受限）",
                )
            else:
                await self._send(stream_id, "🚫 你不是管理员哦~只有管理员才能查看和管理日程呢")
            return True, "没有权限", True

        # 白名单过滤（留空 = 全部允许）
        admin = self._admin_cfg()
        allowed_streams = list(getattr(admin, "allowed_streams", None) or []) if admin else []
        if not is_stream_allowed(stream_id, allowed_streams):
            await self._send(stream_id, "💤 当前会话未启用日程功能")
            return True, "会话未启用", True

        if len(parts) == 1:
            await self._show_help(stream_id)
            return True, "显示帮助", True

        subcommand = parts[1] if len(parts) > 1 else ""

        if subcommand == "list":
            await self._handle_list(stream_id)
        elif subcommand == "regenerate":
            await self._handle_regenerate(stream_id, parts)
        elif subcommand == "delete":
            return await self._handle_delete(stream_id, parts)
        elif subcommand == "clear":
            await self._handle_clear(stream_id, parts)
        elif subcommand == "help":
            await self._show_help(stream_id)
        else:
            await self._send(stream_id, f"未知命令: {subcommand}\n使用 /cateye_test_plan help 查看帮助")

        return True, "命令执行完成", True

    # ------------------------------------------------------------
    # 权限检查
    # ------------------------------------------------------------

    def _check_permission(self, user_id: str, is_local_operator: bool = False) -> bool:
        """检查用户权限（本机控制台天然放行；未配置管理员时默认拒绝 QQ 侧用户）。"""
        if is_local_operator:
            return True

        def _id_part(raw: str) -> str:
            return raw.strip().rsplit(":", 1)[-1]

        candidate = _id_part(str(user_id or ""))
        if not candidate:
            return False
        admin = self._admin_cfg()
        admin_users = list(getattr(admin, "admin_users", None) or []) if admin else []
        return any(_id_part(str(entry)) == candidate for entry in admin_users)

    # ------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------

    async def _send(self, stream_id: str, text: str) -> None:
        """发送文本消息（封装 ctx.send.text）。"""
        if not stream_id:
            logger.warning("无 stream_id，跳过发送: %s", text[:30])
            return
        try:
            await self._plugin.ctx.send.text(text, stream_id)
        except Exception as exc:
            logger.warning(f"发送文本失败: {exc}")

    def _get_today_schedule_goals(self, goal_manager: Any) -> List[Any]:
        """获取今天的日程目标（合成视图）。"""
        return goal_manager.get_schedule_goals(chat_id="global")

    def _sort_schedule_goals(self, goals: List[Any]) -> List[Any]:
        """按时间窗口排序日程目标。"""

        def _get_start(g: Any) -> int:
            tw = (g.parameters.get("time_window") if g.parameters else None) or (
                g.conditions.get("time_window") if g.conditions else None
            ) or [0]
            return tw[0] if tw else 0

        return sorted(goals, key=_get_start)

    def _build_status_text(self, schedule_goals: List[Any]) -> str:
        """构建今日日程的详细文字版。"""
        schedule_goals = self._sort_schedule_goals(schedule_goals)
        tz_manager = self._make_tz_manager()
        now = tz_manager.get_now()
        today = now.strftime("%Y-%m-%d")
        weekday = _WEEKDAY_NAMES[now.weekday()]

        lines: List[str] = [f"📅 今日日程 {today} {weekday}", f"共 {len(schedule_goals)} 项活动", ""]

        for idx, goal in enumerate(schedule_goals, 1):
            start_minutes, end_minutes = get_time_window_from_goal(goal)
            start_time = format_minutes_to_time(start_minutes)
            end_time = format_minutes_to_time(end_minutes if end_minutes < 1440 else end_minutes - 1440)
            type_emoji = _GOAL_TYPE_EMOJI.get(goal.goal_type, "📌")

            lines.append(f"{idx}. ⏰ {start_time}-{end_time}  {type_emoji} {goal.name}")
            if self._enable_detailed_description and goal.description:
                lines.append(f"   📝 {goal.description}")
            lines.append("")  # 空行分隔

        return "\n".join(lines).rstrip()

    async def _draw_schedule_image(self, schedule_goals: List[Any]) -> Optional[str]:
        """绘制日程图片，返回 base64；失败/超时**静默**返回 None。

        超时由 ``image_timeout_seconds`` 配置控制；PIL 是同步阻塞操作，
        统一丢给默认线程池执行，避免阻塞事件循环。
        """
        cfg = self._schedule_cfg()
        if not cfg.list_draw_image:
            return None

        schedule_goals = self._sort_schedule_goals(schedule_goals)
        schedule_items: List[Dict[str, Any]] = []
        for goal in schedule_goals:
            start_minutes, end_minutes = get_time_window_from_goal(goal)
            end_norm = end_minutes if end_minutes < 1440 else end_minutes - 1440
            time_str = f"{format_minutes_to_time(start_minutes)}-{format_minutes_to_time(end_norm)}"
            schedule_items.append({
                "time": time_str,
                "name": goal.name,
                "description": goal.description if self._enable_detailed_description else "",
                "goal_type": goal.goal_type,
            })

        tz_manager = self._make_tz_manager()
        now = tz_manager.get_now()
        today = now.strftime("%Y-%m-%d")
        weekday = _WEEKDAY_NAMES[now.weekday()]
        title = f"今日日程 {today} {weekday}"

        try:
            _img_path, img_base64 = await asyncio.wait_for(
                asyncio.to_thread(
                    ScheduleImageGenerator.generate_schedule_image,
                    title=title,
                    schedule_items=schedule_items,
                    tz_name=cfg.timezone,
                    output_dir=self._image_output_dir(),
                ),
                timeout=max(1.0, float(cfg.image_timeout_seconds)),
            )
            return img_base64
        except Exception as exc:
            # 静默降级：不提示失败、不发图片，仅记录日志供排查
            logger.info(f"/cateye_test_plan list 图片绘制放弃（静默降级为文本）: {exc}")
            return None

    def _image_output_dir(self):
        """图片输出目录（宿主隔离数据目录下 images/；缺数据目录时返回 None）。"""
        data_dir = getattr(self._plugin, "_data_dir", None)
        if data_dir is None:
            return None
        from pathlib import Path
        return Path(data_dir) / "images"

    # ------------------------------------------------------------
    # 子命令实现
    # ------------------------------------------------------------

    async def _handle_list(self, stream_id: str) -> None:
        """``/cateye_test_plan list``：今日日程（图片 + 详细文字，合并转发）。

        流程：先检查今日是否有日程 → 无则代码层触发生成后再发送；
        发送形态：合并转发两条记录（第一条图片（若绘制成功），第二条详细文字）；
        绘制失败/超时静默降级为纯文字。
        """
        goal_manager = get_goal_manager()
        schedule_goals = self._get_today_schedule_goals(goal_manager)

        if not schedule_goals:
            tools_svc = self._tools_svc()
            if tools_svc is None:
                await self._send(stream_id, "❌ 插件未完成初始化，无法生成日程")
                return
            await self._send(
                stream_id,
                "📋 今天还没有日程，正在为你生成（约 30s ~ 2min），完成后会自动发送…",
            )
            try:
                await tools_svc.regenerate_today_schedule_now()
            except Exception as exc:
                logger.error(f"/cateye_test_plan list 触发生成日程失败: {exc}", exc_info=True)
                await self._send(stream_id, f"❌ 日程生成失败: {exc}")
                return
            schedule_goals = self._get_today_schedule_goals(goal_manager)
            if not schedule_goals:
                await self._send(stream_id, "⚠️ 日程生成完成但没有产生有效活动，请稍后用 /cateye_test_plan list 重试")
                return

        text = self._build_status_text(schedule_goals)
        image_base64 = await self._draw_schedule_image(schedule_goals)

        # 合并转发：第一条为图片（若绘制成功），第二条为文字
        records: List[Dict[str, Any]] = []
        if image_base64:
            records.append({
                "user_id": "0",
                "nickname": "日程规划",
                "segments": [{"type": "image", "content": image_base64}],
            })
        records.append({
            "user_id": "0",
            "nickname": "日程规划",
            "segments": [{"type": "text", "content": text}],
        })

        try:
            await self._plugin.ctx.send.forward(records, stream_id)
            logger.info("✅ /cateye_test_plan list 已合并转发（图片=%s，文字 %s 字符）", "有" if image_base64 else "无", len(text))
        except Exception as exc:
            # 合并转发失败（能力未声明 / 平台不支持）→ 回退普通发送
            logger.warning(f"合并转发失败，回退为普通发送: {exc}")
            try:
                if image_base64 and hasattr(self._plugin.ctx.send, "image"):
                    await self._plugin.ctx.send.image(image_base64, stream_id)
            except Exception as img_exc:
                logger.warning(f"回退发送图片失败: {img_exc}")
            await self._send(stream_id, text)

    async def _handle_regenerate(self, stream_id: str, parts: List[str]) -> None:
        """``/cateye_test_plan regenerate [额外要求...]``：立即重新生成今日日程。"""
        tools_svc = self._tools_svc()
        if tools_svc is None:
            await self._send(stream_id, "❌ 插件未完成初始化，无法重新生成日程")
            return

        # 剩余参数拼为 extra_prompt（允许带空格的自然描述）
        extra_prompt = " ".join(parts[2:]).strip()

        hint = "♻️ 正在重新生成今日日程，请稍候（约 30s ~ 2min）..."
        if extra_prompt:
            hint += f"\n📝 额外要求：{extra_prompt}"
        await self._send(stream_id, hint)

        try:
            items = await tools_svc.regenerate_today_schedule_now(extra_prompt=extra_prompt)
        except Exception as exc:
            logger.error(f"重新生成日程失败: {exc}", exc_info=True)
            await self._send(stream_id, f"❌ 重新生成失败: {exc}")
            return

        msg = f"✅ 已重新生成今日日程，共 {len(items)} 项活动\n\n💡 使用 /cateye_test_plan list 查看（图片+文字详情）"
        await self._send(stream_id, msg)

    async def _handle_delete(self, stream_id: str, parts: List[str]) -> Tuple[bool, str, bool]:
        """``/cateye_test_plan delete <id|序号>``：删除目标（含合成视图的日程条目）。"""
        goal_manager = get_goal_manager()

        if len(parts) < 3:
            await self._send(
                stream_id,
                "❌ 请提供要删除的目标 ID 或序号\n\n用法: /cateye_test_plan delete <goal_id或序号>\n\n使用 /cateye_test_plan list 查看所有目标",
            )
            return True, "缺少参数", True

        identifier = parts[2]

        # 尝试作为索引处理
        if identifier.isdigit():
            idx = int(identifier) - 1
            goals = goal_manager.get_all_goals()
            if 0 <= idx < len(goals):
                goal = goals[idx]
                goal_id = goal.goal_id
                goal_name = goal.name
            else:
                await self._send(stream_id, f"❌ 序号 {identifier} 超出范围\n使用 /cateye_test_plan list 查看所有目标")
                return True, "序号无效", True
        else:
            # 作为 goal_id 处理
            goal_id = identifier
            goal = goal_manager.get_goal(goal_id)
            if not goal:
                await self._send(stream_id, f"❌ 目标不存在: {goal_id}")
                return True, "目标不存在", True
            goal_name = goal.name

        success = goal_manager.delete_goal(goal_id)
        if success:
            await self._send(stream_id, f"🗑️ 已删除目标: {goal_name}\n\nID: {goal_id}")
        else:
            await self._send(stream_id, "❌ 删除失败")
        return True, "命令执行完成", True

    async def _handle_clear(self, stream_id: str, parts: List[str]) -> None:
        """``/cateye_test_plan clear [days]``：清理旧日程（days=0 表示仅保留今天）。"""
        goal_manager = get_goal_manager()

        days_to_keep = 0
        if len(parts) >= 3 and parts[2].isdigit():
            days_to_keep = int(parts[2])

        tz_manager = self._make_tz_manager()
        cutoff_date = tz_manager.get_now() - timedelta(days=days_to_keep)

        goals = goal_manager.get_all_goals()
        to_delete: List[Any] = []
        for g in goals:
            # 仅清理日程类型（有 time_window）
            has_time_window = bool(
                (g.parameters and "time_window" in g.parameters)
                or (g.conditions and "time_window" in g.conditions)
            )
            if not has_time_window:
                continue
            if not g.created_at:
                continue
            try:
                goal_datetime = g.created_at.replace(hour=0, minute=0, second=0, microsecond=0)
                cutoff_datetime = cutoff_date.replace(hour=0, minute=0, second=0, microsecond=0)
                # 防 tz-aware/naive 混比报错
                if strip_tz(goal_datetime) < strip_tz(cutoff_datetime):
                    to_delete.append(g)
            except Exception as exc:
                logger.warning(f"解析目标创建时间失败: {g.created_at} - {exc}")
                continue

        if not to_delete:
            await self._send(stream_id, "✨ 没有需要清理的旧日程")
            return

        deleted_count = 0
        for goal in to_delete:
            if goal_manager.delete_goal(goal.goal_id):
                deleted_count += 1

        if deleted_count > 0:
            today_schedule_count = len(self._get_today_schedule_goals(goal_manager))
            await self._send(
                stream_id,
                f"🧹 已清理 {deleted_count} 个旧日程目标\n\n保留了今天的 {today_schedule_count} 个日程",
            )
        else:
            await self._send(stream_id, "❌ 清理失败")

    async def _show_help(self, stream_id: str) -> None:
        """显示帮助（完整帮助文本作为单条消息合并转发）。"""
        help_text = """🤖 日程规划系统

📋 命令列表:
/cateye_test_plan list - 查看今日日程（图片+详细文字合并转发；无日程时自动先生成）
/cateye_test_plan regenerate [额外要求] - 重新生成今日日程（先删今天再重生，可附加临时要求）
/cateye_test_plan delete <goal_id或序号> - 删除指定目标
/cateye_test_plan clear - 清理昨天及更早的旧日程
/cateye_test_plan help - 显示此帮助

💡 使用方式:
1. 对我说 "帮我生成今天的日程" / 直接 /cateye_test_plan list，没有日程时会自动创建
2. 使用 regenerate 在不满意当前日程时立即重新生成
3. 使用 clear 清理旧日程，保持目标列表整洁

♻️ 重新生成示例:
/cateye_test_plan regenerate                # 直接重新生成今日日程
/cateye_test_plan regenerate 加入下午跑步     # 重新生成并临时叠加要求

📌 注意:
- 日程每天自动生成，无需手动创建
- /cateye_test_plan list 在今日无日程时会先自动生成再展示
- regenerate 会丢弃今天已有日程并重新生成，操作不可逆
- clear 命令会自动保留今天的日程
"""
        await self._send_forward(stream_id, help_text.rstrip())

    async def _send_forward(self, stream_id: str, text: str) -> None:
        """把完整长文本作为单条消息合并转发；失败回退为普通文本发送。"""
        if not stream_id:
            logger.warning("无 stream_id，跳过合并转发")
            return

        messages: List[Dict[str, Any]] = [
            {
                "user_id": "0",
                "nickname": "日程规划",
                "segments": [{"type": "text", "content": text}],
            },
        ]
        try:
            await self._plugin.ctx.send.forward(messages, stream_id)
            logger.info("✅ 已合并转发 %s 字符", len(text))
        except Exception as exc:
            logger.warning(f"合并转发失败，回退为普通文本: {exc}")
            await self._send(stream_id, text)
