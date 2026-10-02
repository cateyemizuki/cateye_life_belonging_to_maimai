"""Context Loader Module（来源：自主规划插件 planner/generator/context_loader.py，改造版）。

改造点（对齐 cateye 整合契约）：
- 历史消息走 ``ctx.chat.get_all_streams`` + ``ctx.message.get_by_time_in_chat``；
- 知识库走 ``ctx.knowledge.search``；
- 近 N 天日程摘要经 GoalManager 合成视图（schedule_items 数据源）。
"""

import logging
from datetime import datetime, time, timedelta
from typing import Any, Dict, List, Optional

from ..stream_filter import is_stream_allowed
from ..time_utils import TimezoneManager
from ..goal_manager import GoalManager

logger = logging.getLogger(__name__)

# 常量定义
MAX_YESTERDAY_ACTIVITIES = 10  # 单日日程显示的最大活动数
MAX_HISTORY_LINE_CHARS = 80    # 历史消息单行最大字符数


class ScheduleContextLoader:
    """日程上下文加载器 - 单一职责：加载历史上下文（依赖注入）。"""

    def __init__(self, goal_manager: Optional[GoalManager], tz_manager: TimezoneManager):
        self.goal_manager = goal_manager
        self.tz_manager = tz_manager

    def _extract_time_window(self, goal) -> Optional[List[int]]:
        """从目标中提取 time_window（优先 parameters，其次 conditions）。"""
        if goal.parameters and "time_window" in goal.parameters:
            return goal.parameters["time_window"]
        elif goal.conditions and "time_window" in goal.conditions:
            return goal.conditions["time_window"]
        return None

    def load_recent_schedule_summary(self, days: int = 3) -> Optional[str]:
        """加载最近 N 天的日程摘要，让 LLM 生成新日程时避免重复。

        覆盖范围：``yesterday - (days - 1)`` 到 ``yesterday``。

        Args:
            days: 回看天数（1=只看昨天；3-5 推荐；防止交替式重复）

        Returns:
            多天日程摘要字符串；全部为空时返回兜底文案。
        """
        try:
            days = max(1, int(days))
            now = self.tz_manager.get_now()
            all_lines: List[str] = []

            for offset in range(1, days + 1):  # 1=昨天, 2=前天, ...
                day = now - timedelta(days=offset)
                day_str = day.strftime("%Y-%m-%d")
                day_label = day.strftime("%m-%d")
                weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
                day_weekday = weekday_names[day.weekday()]

                goals = self.goal_manager.get_schedule_goals(chat_id="global", date_str=day_str) \
                    if self.goal_manager is not None else []

                day_activities: List[str] = []
                for goal in goals:
                    time_window = self._extract_time_window(goal)
                    if time_window:
                        start_minutes = time_window[0] if isinstance(time_window, list) else 0
                        hour = start_minutes // 60
                        minute = start_minutes % 60
                        time_str = f"{hour:02d}:{minute:02d}"
                        day_activities.append(f"  {time_str} {goal.name}")

                if day_activities:
                    all_lines.append(f"【{day_label} {day_weekday}】")
                    all_lines.extend(day_activities[:MAX_YESTERDAY_ACTIVITIES])

            if all_lines:
                label = "昨天的日程" if days == 1 else f"最近 {days} 天的日程"
                summary = f"{label}:\n" + "\n".join(all_lines)
                logger.debug("加载 %d 天日程摘要: %d 行", days, len(all_lines))
                return summary

            return f"最近 {days} 天没有具体日程记录" if days > 1 else "最近一天普通的日子"

        except Exception as e:
            logger.warning(f"加载最近 {days} 天日程失败: {e}")
            return f"最近{days if days > 1 else '一天'}天的事情记不太清了"

    # ============================================================
    # 跨群动态上下文
    # ============================================================

    async def load_recent_history_across_streams(
        self,
        plugin: Any,
        allowed_streams: List[str],
        limit: int,
    ) -> str:
        """跨白名单聊天流拉取当天最近消息，拼成可读字符串。

        Args:
            plugin: 当前插件实例（提供 ``ctx.chat`` 与 ``ctx.message``）。
            allowed_streams: 会话白名单；为空 = 不限制。
            limit: 累计最多拉取多少条消息；<=0 直接返回空串（节省 RPC）。

        Returns:
            形如 ``[HH:MM] {nick}@{stream}: {text}`` 的多行字符串；无内容时返回空串。
        """
        if limit <= 0 or plugin is None:
            return ""

        ctx_obj = getattr(plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "chat") or not hasattr(ctx_obj, "message"):
            return ""

        try:
            streams_raw = await ctx_obj.chat.get_all_streams(platform="qq")
        except Exception as exc:
            logger.debug(f"获取聊天流列表失败: {exc}")
            return ""
        if not isinstance(streams_raw, list):
            return ""

        # 按白名单过滤
        streams = [s for s in streams_raw if isinstance(s, dict)]
        streams = [
            s for s in streams
            if is_stream_allowed(
                str(s.get("session_id") or s.get("stream_id") or ""),
                allowed_streams,
                stream_info=s,
            )
        ]
        if not streams:
            return ""

        per_stream = max(1, limit // len(streams))
        now = datetime.now()
        start_today = datetime.combine(now.date(), time.min)
        start_ts = str(start_today.timestamp())
        end_ts = str(now.timestamp())

        all_lines: List[str] = []
        for stream in streams:
            sid = str(stream.get("session_id") or stream.get("stream_id") or "").strip()
            if not sid:
                continue
            try:
                msgs = await ctx_obj.message.get_by_time_in_chat(
                    sid, start_ts, end_ts, limit=per_stream,
                )
            except Exception as exc:
                logger.debug(f"读取 {sid} 历史失败: {exc}")
                continue
            if isinstance(msgs, dict):
                msgs = msgs.get("messages") or msgs.get("data") or []
            if not isinstance(msgs, list):
                continue
            stream_label = str(stream.get("group_id") or stream.get("user_id") or sid[:8])
            for msg in msgs:
                line = self._format_history_line(msg, stream_label)
                if line:
                    all_lines.append(line)

        if not all_lines:
            return ""
        return "\n".join(all_lines[-limit:])

    async def load_relevant_knowledge(
        self,
        plugin: Any,
        query_hint: str,
        limit: int,
    ) -> str:
        """从知识库拉取角色 / 日常相关记忆。

        Args:
            plugin: 当前插件实例。
            query_hint: 额外查询线索（通常是日期字符串）。
            limit: 最大结果条数；<=0 直接返回空串。

        Returns:
            知识库返回的可读字符串；失败返回空串。
        """
        if limit <= 0 or plugin is None:
            return ""
        ctx_obj = getattr(plugin, "ctx", None)
        if ctx_obj is None or not hasattr(ctx_obj, "knowledge"):
            return ""
        query = f"日常生活|角色习惯|{query_hint}".strip("|")
        try:
            result = await ctx_obj.knowledge.search(query, limit=limit)
        except Exception as exc:
            logger.debug(f"知识库检索失败: {exc}")
            return ""
        if isinstance(result, dict):
            result = result.get("results") or result.get("data") or result.get("response") or ""
        return str(result or "").strip()

    @staticmethod
    def _format_history_line(msg: Dict[str, Any], stream_label: str) -> str:
        """把单条消息 dict 格式化成可读行；无效消息返回空串。"""
        if not isinstance(msg, dict):
            return ""
        text = str(msg.get("processed_plain_text") or "").strip()
        if not text:
            raw = msg.get("raw_message")
            if isinstance(raw, dict):
                parts: List[str] = []
                comps = raw.get("components")
                if isinstance(comps, list):
                    for c in comps:
                        if isinstance(c, dict):
                            piece = str(c.get("text") or c.get("content") or "").strip()
                            if piece:
                                parts.append(piece)
                text = " ".join(parts)
        if not text:
            return ""
        if len(text) > MAX_HISTORY_LINE_CHARS:
            text = text[:MAX_HISTORY_LINE_CHARS] + "…"

        time_str = ""
        ts = msg.get("timestamp")
        try:
            time_str = datetime.fromtimestamp(float(ts)).strftime("%H:%M")
        except (TypeError, ValueError, OSError):
            time_str = ""

        info = msg.get("message_info") if isinstance(msg.get("message_info"), dict) else {}
        user_info = info.get("user_info") if isinstance(info.get("user_info"), dict) else {}
        nick = (
            str(user_info.get("user_cardname") or "").strip()
            or str(user_info.get("user_nickname") or "").strip()
            or str(user_info.get("user_id") or "").strip()
            or "用户"
        )

        prefix = f"[{time_str}] " if time_str else ""
        return f"{prefix}{nick}@{stream_label}: {text}"
