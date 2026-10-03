"""日程注入业务实现（来源：自主规划插件 services/inject_service.py，移植版）。

Hook 入口：``maisaka.planner.before_request`` / ``maisaka.replyer.before_request``。

业务子模块（IntentClassifier / InjectOptimizer / ConversationContextCache）位于
``inject/`` 子包；活动状态短语（原 ActivityStateAnalyzer）已删除，精神状态行改读
情绪系统的 ``mood_text()``（M4，§4.2 行 8，两处「精神状态」同源）。

注入协议（照抄源插件）：
- 向 items 注入 SystemMessageItem 快照：meta 三件套（item_id=uuid4().hex、
  logical_turn_id=None（或继承首条）、timestamp=ISO）；
- Hook 返回 ``{"action": "continue"}`` 或
  ``{"action": "continue", "modified_kwargs": {**kwargs, <payload_key>: new, ...}}``
  全量回传（宿主对 modified_kwargs 整体替换，单键返回会丢字段）；
- replyer 注入协作式：保留上游 extra_prompt，自身文本追加在后方。

对整合契约的适配：
- ``inject_schedule`` 配置名对齐为 ``inject_into_planner``；
- 白名单用 ``schedule.inject_scope``（target_private=仅目标私聊 / all）；
  admin 段若挂载了 allowed_streams 则叠加为额外限制；
- 数据查询经 GoalManager 合成视图（schedule_items 单一数据源）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from uuid import uuid4

from .config import InjectSectionConfig, ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .inject import (
    ConversationContextCache,
    InjectOptimizer,
    IntentClassifier,
    UserIntent,
)
from .lru_cache import LRUCache
from .time_utils import TimezoneManager, describe_energy, get_energy_level, parse_time_window, strip_tz

if TYPE_CHECKING:
    from typing import Any as _Plugin

logger = logging.getLogger(__name__)


class InjectService:
    """日程注入服务。"""

    # 主程序 maisaka 会在 messages 末尾追加多条独立 user 消息作为元数据上下文，
    # 这些消息**不是真实用户问题**，不应进入 IntentClassifier，否则会污染意图判定。
    _METADATA_USER_MESSAGE_PATTERNS: List[re.Pattern] = [
        re.compile(r"^当前时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*$"),
        re.compile(r"^【人物画像-内部参考】"),
        re.compile(r"^<system-reminder>"),
    ]

    # 主程序 build_planner_prefix 会给 chat history 里每条 user 消息加
    # ``<message msg_id="..." time="..." user="...">\n真实文本`` 前缀。
    _PLANNER_PREFIX_PATTERN: re.Pattern = re.compile(r"^<message\s+[^>]*>\s*\n?")

    def __init__(self, plugin: Any) -> None:
        """初始化 InjectService，加载智能注入组件、缓存、配置。

        Args:
            plugin: 当前插件实例（动态读 plugin.config，热重载自动生效）
        """
        self._plugin = plugin
        cfg = section_or_default(plugin, "schedule", ScheduleSectionConfig)

        # 缓存：把 TTL 直接传给 LRUCache，由它统一管理过期
        self._schedule_cache: LRUCache = LRUCache(max_size=cfg.cache_max_size, ttl=cfg.cache_ttl)

        # 时区管理器
        self._tz_manager: TimezoneManager = TimezoneManager(cfg.timezone)

        # 智能注入组件（按 inject 段配置；未挂载该段时全部启用默认值）
        inj = section_or_default(plugin, "inject", InjectSectionConfig)
        self._intent_classifier = IntentClassifier() if inj.enable_intent_classification else None
        self._inject_optimizer = (
            InjectOptimizer(
                cache_ttl=cfg.cache_ttl,
                casual_inject_probability=inj.casual_chat_inject_probability,
            )
            if inj.enable_inject_optimization
            else None
        )
        self._context_cache = ConversationContextCache(
            max_turns=inj.context_max_turns,
            ttl=inj.context_ttl,
        )
        logger.debug(
            "InjectService 初始化完成（cache_max=%d, cache_ttl=%d, intent=%s, optimizer=%s）",
            cfg.cache_max_size, cfg.cache_ttl,
            inj.enable_intent_classification, inj.enable_inject_optimization,
        )

    # ------------------------------------------------------------
    # 配置与会话准入
    # ------------------------------------------------------------

    def _schedule_cfg(self) -> ScheduleSectionConfig:
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig)

    def _plugin_enabled(self) -> bool:
        """插件总开关 + 日程模块独立总开关（config.schedule.enabled）任一关闭即不注入。"""
        plugin_section = getattr(self._plugin.config, "plugin", None)
        if not bool(getattr(plugin_section, "enabled", True)):
            return False
        return bool(getattr(self._schedule_cfg(), "enabled", True))

    def _is_session_allowed(self, session_id: str) -> bool:
        """会话准入：插件启用 + inject_scope（target_private=仅目标私聊）+ 叠加白名单。"""
        if not self._plugin_enabled():
            return False
        cfg = self._schedule_cfg()
        if getattr(cfg, "inject_scope", "all") != "all":
            # 只注入目标私聊（与 plugin.py 的 _injection_allowed 同口径）
            patrol = getattr(self._plugin, "_patrol", None)
            target_stream = ""
            if patrol is not None:
                try:
                    target_stream = str(patrol.patrol_status().get("stream_id") or "")
                except Exception:
                    target_stream = ""
            if not target_stream or session_id != target_stream:
                return False
        # admin 段若挂载了 allowed_streams 则叠加限制（留空 = 不限制）
        admin = getattr(self._plugin.config, "admin", None)
        allowed = list(getattr(admin, "allowed_streams", None) or []) if admin else []
        if allowed:
            from .stream_filter import is_stream_allowed
            if not is_stream_allowed(session_id, allowed):
                return False
        return True

    # ------------------------------------------------------------
    # 后台缓存预热
    # ------------------------------------------------------------

    async def preheat_cache(self) -> None:
        """启动后预热缓存（异步任务；失败静默）。"""
        try:
            await asyncio.sleep(5)  # 等待系统初始化
            logger.debug("开始预热日程缓存...")
            await self._get_current_schedule("global")
            logger.debug("日程缓存预热完成")
        except Exception as exc:
            logger.warning(f"缓存预热失败: {exc}")

    # ------------------------------------------------------------
    # Hook 主入口（planner / replyer 两个）
    # ------------------------------------------------------------

    async def inject_into_replyer_extra_prompt(
        self,
        session_id: str = "",
        attempt: int = 1,
        extra_prompt: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.replyer.before_request`` Hook 入口。

        把当前活动作为 extra_prompt 协作式注入（保留上游内容，自身追加在后方），
        与 planner 注入共用 InjectOptimizer 冷却（同会话 + 同活动短时间不重复）。
        """
        cfg = self._schedule_cfg()
        if not self._plugin_enabled() or not cfg.inject_into_replyer:
            return {"action": "continue"}

        # 重试不重复注入
        if attempt and int(attempt) > 1:
            return {"action": "continue"}

        # 会话准入（inject_scope / 白名单）
        if not self._is_session_allowed(session_id):
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"
            user_id = session_id or "unknown"

            current_activity, current_description, future_activities, activity_type = (
                await self._get_current_schedule(chat_id)
            )
            if not current_activity:
                return {"action": "continue"}

            # 复用 InjectOptimizer 冷却（与 planner 共享）
            if self._inject_optimizer is not None:
                neutral_intent = UserIntent.CASUAL_CHAT
                should_inject, skip_reason = self._inject_optimizer.should_inject(
                    user_id, neutral_intent, current_activity, confidence=0.5,
                )
                if not should_inject:
                    logger.debug(f"replyer 注入被冷却跳过: {skip_reason}")
                    return {"action": "continue"}

            # 一次查询拿到剩余分钟（状态情绪短语已随 state_analyzer 删除，精神状态归情绪系统）
            remaining_minutes = self._compute_activity_aux(current_activity, activity_type)
            activity_prompt = self._build_replyer_extra_prompt(
                current_activity=current_activity,
                description=current_description or "",
                future_activities=future_activities,
                remaining_minutes=remaining_minutes,
            )

            if self._inject_optimizer is not None:
                self._inject_optimizer.record_injection(
                    user_id, current_activity, activity_prompt, UserIntent.CASUAL_CHAT,
                )

            # 协作式追加——保留上游 extra_prompt，自身文本追加在后方
            upstream_prompt = str(extra_prompt or "").strip()
            merged_prompt = f"{upstream_prompt}\n\n{activity_prompt}" if upstream_prompt else activity_prompt

            logger.info(f"replyer 注入: {current_activity}")
            return {
                "action": "continue",
                "modified_kwargs": {
                    **kwargs,
                    "session_id": session_id,
                    "extra_prompt": merged_prompt,
                },
            }

        except Exception as exc:
            logger.error(f"replyer 注入失败: {exc}", exc_info=True)
            return {"action": "continue"}

    async def inject_into_planner_messages(
        self,
        items: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        item_schema_version: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.planner.before_request`` Hook 入口。

        按当前日程信息构造 system 条目插入消息列表。兼容两种宿主投影：
        ContextItem 快照（``items``，MaiBot 1.2.x+）与旧 role/content（``messages``）；
        其余 kwargs（tool_definitions 等）全量回传。
        """
        # 兼容新旧 payload 字段：优先 items，回退 messages
        if items is not None and isinstance(items, list):
            payload_key, payload = "items", items
        elif messages is not None and isinstance(messages, list):
            payload_key, payload = "messages", messages
        else:
            return {"action": "continue"}

        if not payload:
            return {"action": "continue"}

        cfg = self._schedule_cfg()
        if not self._plugin_enabled() or not cfg.inject_into_planner:
            return {"action": "continue"}

        # 会话准入（inject_scope / 白名单）
        if not self._is_session_allowed(session_id):
            logger.debug(f"会话 {session_id} 不在注入范围，跳过注入")
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"

            # 提取最后一条 user 消息用于意图分析
            user_message = self._extract_last_user_text(payload)
            user_id = session_id or "unknown"

            # 检查对话上下文：判断是否在连续讨论日程话题
            context_continue_inject = False
            context_reason: Optional[str] = None
            if self._context_cache is not None:
                context_continue_inject, context_reason = self._context_cache.should_continue_inject(
                    user_id, None,
                )

            # 获取当前日程
            current_activity, current_description, all_future_activities, activity_type = (
                await self._get_current_schedule(chat_id)
            )

            # 更新对话上下文：用当前活动重新判断
            if self._context_cache is not None and context_continue_inject:
                context_continue_inject, context_reason = self._context_cache.should_continue_inject(
                    user_id, current_activity,
                )

            # 无当前活动：仅记录上下文，不注入
            if not current_activity:
                if self._context_cache is not None:
                    self._context_cache.add_turn(
                        user_id=user_id,
                        user_message=user_message,
                        intent=None,
                        injected=False,
                        activity=None,
                    )
                return {"action": "continue"}

            # 统一注入管道：分类 → 决策 → 模板路由
            inject_content, injected, detected_intent = self._build_inject_text(
                user_message=user_message,
                current_activity=current_activity,
                current_description=current_description,
                future_activities=all_future_activities,
                activity_type=activity_type,
                context_continue_inject=context_continue_inject,
                context_reason=context_reason,
                user_id=user_id,
            )

            # 记录到上下文缓存
            if self._context_cache is not None:
                self._context_cache.add_turn(
                    user_id=user_id,
                    user_message=user_message,
                    intent=detected_intent,
                    injected=injected,
                    activity=current_activity,
                )

            if not inject_content:
                return {"action": "continue"}

            # 把注入文本作为 system 条目插入消息列表
            modified_payload = self._inject_system_message(payload, inject_content)
            modified_kwargs: Dict[str, Any] = {
                **kwargs,
                "session_id": session_id,
                payload_key: modified_payload,
            }
            if item_schema_version is not None:
                modified_kwargs["item_schema_version"] = item_schema_version
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }

        except Exception as exc:
            logger.error(f"注入日程信息失败: {exc}", exc_info=True)
            return {"action": "continue"}

    # ------------------------------------------------------------
    # 消息提取与插入
    # ------------------------------------------------------------

    @classmethod
    def _strip_planner_prefix(cls, text: str) -> str:
        """剥除主程序 build_planner_prefix 加的 ``<message ...>\\n`` 前缀。"""
        return cls._PLANNER_PREFIX_PATTERN.sub("", text, count=1)

    @classmethod
    def _is_metadata_user_message(cls, text: str) -> bool:
        """判定一条 user 消息文本是否是主程序追加的元数据（非真实用户问题）。"""
        for pattern in cls._METADATA_USER_MESSAGE_PATTERNS:
            if pattern.match(text):
                return True
        return False

    @staticmethod
    def _message_role(msg: Dict[str, Any]) -> str:
        """读取消息角色；兼容 ContextItem 快照投影（item_type）与旧 role/content。"""
        item_type = msg.get("item_type")
        if isinstance(item_type, str) and item_type:
            if item_type == "UserMessageItem":
                return "user"
            if item_type == "SystemMessageItem":
                return "system"
            if item_type == "AssistantMessageItem":
                return "assistant"
            return item_type
        role = msg.get("role")
        return role if isinstance(role, str) else ""

    @staticmethod
    def _message_text_candidates(msg: Dict[str, Any]) -> List[str]:
        """提取消息里的候选文本；兼容快照 parts 与旧 role/content 投影。"""
        if isinstance(msg.get("item_type"), str) and msg.get("item_type"):
            parts = msg.get("parts")
            if not isinstance(parts, list):
                return []
            return [
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ]
        content = msg.get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            candidates: List[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    candidates.append(part)
                elif isinstance(part, dict) and "text" in part:
                    candidates.append(str(part["text"]))
            return candidates
        return []

    @classmethod
    def _extract_last_user_text(cls, messages: List[Dict[str, Any]]) -> str:
        """提取最新一条**含真实文本**的 user 消息（跳过元数据并剥除前缀）。"""
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if cls._message_role(msg) != "user":
                continue
            for candidate in cls._message_text_candidates(msg):
                text = candidate.strip()
                if not text:
                    continue  # 空文本 → 找上一条 user
                if cls._is_metadata_user_message(text):
                    continue  # 元数据消息/part → 跳过
                stripped = cls._strip_planner_prefix(text)
                if cls._is_metadata_user_message(stripped.strip()):
                    continue
                if not stripped.strip():
                    continue
                return stripped
        return ""

    @staticmethod
    def _is_snapshot_payload(messages: List[Dict[str, Any]]) -> bool:
        """判断 payload 是否为宿主 1.2.x 的 ContextItem 快照投影。"""
        return any(
            isinstance(msg, dict) and isinstance(msg.get("item_type"), str) and msg.get("item_type")
            for msg in messages
        )

    @staticmethod
    def _build_snapshot_system_item(
        inject_content: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """构造可被宿主反序列化的 SystemMessageItem 快照（1.2.x payload 契约）。

        - ``item_id`` 必须全局唯一（宿主 validate_context_items 硬性要求）；
        - ``logical_turn_id`` 可为 null（键必须存在）；
        - ``timestamp`` 必须是合法 ISO 时间。
        """
        logical_turn_id: Optional[str] = None
        for msg in messages:
            meta = msg.get("meta") if isinstance(msg, dict) else None
            if isinstance(meta, dict):
                candidate = meta.get("logical_turn_id")
                if isinstance(candidate, str) and candidate.strip():
                    logical_turn_id = candidate
                    break
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"app-schedule-{uuid4().hex}",
                "logical_turn_id": logical_turn_id,
                "timestamp": datetime.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": inject_content}],
        }

    @classmethod
    def _inject_system_message(
        cls,
        messages: List[Dict[str, Any]],
        inject_content: str,
    ) -> List[Dict[str, Any]]:
        """把注入文本作为 system 条目插入消息列表（紧跟第一条 system 之后）。"""
        if cls._is_snapshot_payload(messages):
            injection: Dict[str, Any] = cls._build_snapshot_system_item(inject_content, messages)
        else:
            injection = {"role": "system", "content": inject_content}

        first_system_idx: Optional[int] = None
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and cls._message_role(msg) == "system":
                first_system_idx = idx
                break

        new_messages = list(messages)  # 浅拷贝列表，避免污染调用方
        insert_at = (first_system_idx + 1) if first_system_idx is not None else 0
        new_messages.insert(insert_at, injection)
        return new_messages

    # ------------------------------------------------------------
    # 注入文本构建（统一管道：IntentClassifier + InjectOptimizer + 模板路由）
    # ------------------------------------------------------------

    def _build_inject_text(
        self,
        user_message: str,
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        activity_type: Optional[str],
        context_continue_inject: bool,
        context_reason: Optional[str],
        user_id: str,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """分类 → 决策 → 模板路由。user_id 实际是会话维度 scope ID（session_id）。

        Returns:
            (inject_content, injected, detected_intent)。``inject_content`` 为 None
            表示决策跳过；``detected_intent`` 用于上下文缓存追踪。
        """
        # 1. 意图分类
        intent: UserIntent
        confidence: float
        if self._intent_classifier is not None:
            intent, confidence = self._intent_classifier.classify(user_message)
        else:
            intent, confidence = UserIntent.CASUAL_CHAT, 0.5

        detected_intent = intent.value

        # 2. 决策
        if context_continue_inject:
            logger.info(f"对话上下文触发注入: {context_reason}")
            should_inject = True
        elif self._inject_optimizer is not None:
            should_inject, skip_reason = self._inject_optimizer.should_inject(
                user_id, intent, current_activity, confidence,
            )
        else:
            if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
                should_inject = False
            else:
                should_inject = True

        if not should_inject:
            logger.debug(f"注入跳过: intent={detected_intent}")
            return None, False, detected_intent

        # 3. 查询活动剩余分钟（M4：精神状态行改读情绪 mood_text，见 _render_unified_prompt）
        remaining_minutes = self._compute_activity_aux(current_activity, activity_type)

        # 4. 主动碎碎念判定：仅 CASUAL_CHAT/UNKNOWN 才考虑
        proactive_hit = False
        if (
            intent in (UserIntent.CASUAL_CHAT, UserIntent.UNKNOWN)
            and not context_continue_inject
            and self._inject_optimizer is not None
        ):
            proactive_hit, _proactive_reason = self._inject_optimizer.should_proactive_inject(
                user_id, current_activity,
            )
            if proactive_hit:
                logger.info(f"主动碎碎念触发: scope={user_id}, activity={current_activity}")
                self._inject_optimizer.record_proactive_inject(user_id)

        # 5. 按 intent 渲染统一模板
        inject_content = self._render_unified_prompt(
            intent=intent,
            current_activity=current_activity,
            current_description=current_description,
            future_activities=future_activities,
            remaining_minutes=remaining_minutes,
            proactive_hit=proactive_hit,
            context_continue_inject=context_continue_inject,
            context_reason=context_reason,
        )
        if inject_content is None:
            return None, False, detected_intent

        # 6. 记录注入历史（让 InjectOptimizer 的冷却生效）
        if self._inject_optimizer is not None:
            self._inject_optimizer.record_injection(
                user_id, current_activity, inject_content, intent,
            )

        logger.info(
            f"注入完成: intent={detected_intent}, confidence={confidence:.2f}, "
            f"len={len(inject_content)}, proactive={proactive_hit}"
        )
        return inject_content, True, detected_intent

    def _render_unified_prompt(
        self,
        intent: "UserIntent",
        current_activity: str,
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
        proactive_hit: bool,
        context_continue_inject: bool,
        context_reason: Optional[str],
    ) -> Optional[str]:
        """按 intent 路由统一模板（详略与语气随意图变化；TECH/COMMAND 返回 None）。

        M4（§4.2 行 8）：精神状态行读情绪系统 ``mood_text()``（与 emotion 注入条目同源，
        不得出现两套矛盾的「精神状态」）；情绪不可得时省略该行，不再使用已删除的
        inject/state_analyzer 静态模板。
        """
        if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
            return None

        cfg = self._schedule_cfg()
        enable_detailed = cfg.enable_detailed_description
        max_show = getattr(cfg, "max_future_activities", 3)

        # 当前活动 + 时间衰减（活动状态短语已随 state_analyzer 删除）
        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        if enable_detailed and current_description:
            current_line = f"现在：{current_activity}（{current_description}）{remaining_hint}"
        else:
            current_line = f"现在：{current_activity}{remaining_hint}"

        lines: List[str] = [
            "【可选上下文 - Bot 的当前日程】",
            current_line,
        ]
        mood = self._emotion_mood_line()
        if mood:
            lines.append(f"精神状态：{mood}")

        if intent == UserIntent.QUERY_FUTURE and future_activities and max_show > 0:
            lines.append("接下来的安排:")
            for time_str, name in future_activities[:max_show]:
                lines.append(f"  {time_str} - {name}")
            lines.extend(["", "💡 用户询问未来计划，请自然地介绍后续安排。"])
        elif intent == UserIntent.QUERY_CURRENT:
            if future_activities and max_show > 0:
                time_str, name = future_activities[0]
                lines.append(f"接下来：{time_str} - {name}")
            lines.extend(["", "💡 用户直接询问当前状态，请如实告知，并自然带出此刻的精神状态。"])
        else:
            if context_continue_inject:
                lines.extend(["", f"💡 对话延续中（{context_reason}），可自然带过当前活动。"])
            elif proactive_hit:
                lines.extend(["", "💡 可以在回答中自然带出此刻你正在做的事（一两句即可，不要刻意转移话题）。"])
            else:
                lines.extend(["", "💡 仅供参考；不相关请完全忽略，不要刻意提及。"])

        lines.extend(["", "---", ""])
        return "\n".join(lines)

    def _build_replyer_extra_prompt(
        self,
        current_activity: str,
        description: str,
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
    ) -> str:
        """构建 replyer 极简 extra_prompt（~30 token + 能量基线）。"""
        cfg = self._schedule_cfg()
        enable_detailed = cfg.enable_detailed_description

        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        if enable_detailed and description:
            state_line = f"你现在正在 {current_activity}（{description}）{remaining_hint}。"
        else:
            state_line = f"你现在正在 {current_activity}{remaining_hint}。"

        now = self._tz_manager.get_now()
        energy_desc = describe_energy(get_energy_level(now.hour))

        lines: List[str] = ["【角色当前状态】", state_line, f"精神：{energy_desc}。"]
        if future_activities:
            time_str, name = future_activities[0]
            lines.append(f"接下来 {time_str} 要 {name}。")
        lines.append("⚠️ 不要主动提及；仅在用户问到 / 强相关时自然带过。")
        return "\n".join(lines)

    def _compute_activity_aux(
        self,
        activity_name: str,
        goal_type: Optional[str],
    ) -> Optional[int]:
        """查询某活动剩余分钟（查不到返回 None）。

        M4：活动状态情绪短语随 inject/state_analyzer 删除，本方法只余剩余时间一项；
        ``goal_type`` 参数保留以兼容调用点签名（当前不使用）。
        """
        try:
            goal_manager = get_goal_manager()
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals:
                return None

            now = self._tz_manager.get_now()
            current_min = now.hour * 60 + now.minute

            for goal in goals:
                if goal.name != activity_name:
                    continue
                tw: Optional[List[int]] = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start, end = parse_time_window(tw)
                if start is None or end is None:
                    continue
                # 跨夜归一化：end_minutes > 1440 表示跨夜，凌晨阶段 end 减 1440
                if end > 1440 and current_min < 720:
                    end -= 1440
                remaining = end - current_min
                if remaining <= 0:
                    return None
                return remaining
        except Exception as exc:
            logger.debug("计算活动辅助信息失败: %s", exc)
        return None

    def _emotion_mood_line(self) -> str:
        """M4（§4.2 行 8）：planner 精神状态行读 emotion.mood_text()（与情绪注入条目同源）。

        跨模块可空降级（§八.2）：emotion 未装配/停用/异常一律返回空串，调用方省略该行；
        不再回退 inject/state_analyzer 的静态模板（该模块已按 §4.2 删除）。
        """
        emotion = getattr(self._plugin, "_emotion", None)
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return ""
            except Exception:
                return ""
        getter = getattr(emotion, "mood_text", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""

    # ------------------------------------------------------------
    # 对外公开：当前活动快照（供 @API 转发，跨插件可调）
    # ------------------------------------------------------------

    async def get_current_activity_snapshot(self, chat_id: str = "global") -> Dict[str, Any]:
        """返回当前时间段最新日程活动的结构化快照（与注入走同一条缓存路径）。

        Returns:
            dict::

                {
                    "has_activity": bool,
                    "activity": None | {"name", "description", "goal_type", "time_window"},
                    "next_activities": [{"time": "HH:MM", "name": str}, ...],
                    "as_of": ISO8601 字符串,
                    "timezone": str,
                    "error": None,
                }
        """
        current_activity, current_description, future_activities, activity_type = (
            await self._get_current_schedule(chat_id or "global")
        )

        now = self._tz_manager.get_now()
        activity_payload: Optional[Dict[str, Any]] = None
        if current_activity:
            activity_payload = {
                "name": current_activity,
                "description": current_description or "",
                "goal_type": activity_type or "",
                "time_window": self._lookup_time_window(current_activity, chat_id or "global"),
            }

        return {
            "has_activity": current_activity is not None,
            "activity": activity_payload,
            "next_activities": [{"time": t, "name": n} for t, n in future_activities],
            "as_of": now.isoformat(timespec="seconds"),
            "timezone": self._tz_manager.timezone_str,
            "error": None,  # 成功路径显式给 None，让调用方能统一用 result.get("error") 判断
        }

    def _lookup_time_window(self, activity_name: str, chat_id: str) -> Optional[str]:
        """根据活动名反查时间窗口字符串（HH:MM-HH:MM）。"""
        try:
            goal_manager = get_goal_manager()
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals and chat_id and chat_id != "global":
                goals = goal_manager.get_active_goals(chat_id=chat_id)

            for goal in goals:
                if goal.name != activity_name:
                    continue
                tw = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start_minutes, end_minutes = parse_time_window(tw)
                if start_minutes is None or end_minutes is None:
                    continue
                start_str = f"{start_minutes // 60:02d}:{start_minutes % 60:02d}"
                end_minutes_norm = end_minutes if end_minutes < 1440 else end_minutes - 1440
                end_str = f"{end_minutes_norm // 60:02d}:{end_minutes_norm % 60:02d}"
                return f"{start_str}-{end_str}"
        except Exception as exc:
            logger.debug("反查时间窗口失败: %s", exc)
        return None

    # ------------------------------------------------------------
    # 当前日程查询（带 LRU 缓存）
    # ------------------------------------------------------------

    async def _get_current_schedule(
        self,
        chat_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], List[Tuple[str, str]], Optional[str]]:
        """获取当前日程信息（带缓存；15 分钟窗口缓存键 + LRU TTL）。

        Returns:
            ``(当前活动, 活动描述, 所有未来活动列表, 当前活动类型)``
        """
        try:
            now = self._tz_manager.get_now()
            current_hour = now.hour
            current_minute = now.minute

            # 15 分钟窗口缓存键
            time_window = (current_hour * 60 + current_minute) // 15
            cache_key = f"{chat_id or 'global'}_{now.strftime('%Y%m%d')}_{time_window}"

            # 命中缓存直接返回
            cached_result = await self._schedule_cache.get(cache_key)
            if cached_result is not None:
                return cached_result

            goal_manager = get_goal_manager()
            cross_day_enabled = bool(self._schedule_cfg().cross_day_activity)
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals and chat_id and chat_id != "global":
                goals = goal_manager.get_active_goals(chat_id=chat_id)

            if not goals:
                result = (None, None, [], None)
                await self._schedule_cache.set(cache_key, result)
                return result

            current_time_minutes = current_hour * 60 + current_minute
            today_date = now.strftime("%Y-%m-%d")
            yesterday_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")

            scheduled_goals: List[Tuple[Any, List[int], bool, bool]] = []
            for goal in goals:
                # 跳过 pending_commitment（不是日程活动）
                if goal.goal_type == "pending_commitment":
                    continue
                tw: Optional[List[int]] = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")

                if tw:
                    is_today = False
                    is_yesterday = False
                    if goal.created_at:
                        if isinstance(goal.created_at, str):
                            is_today = goal.created_at.startswith(today_date)
                            is_yesterday = goal.created_at.startswith(yesterday_date)
                        else:
                            goal_date_str = goal.created_at.strftime("%Y-%m-%d")
                            is_today = goal_date_str == today_date
                            is_yesterday = goal_date_str == yesterday_date

                    # 跨夜开关关闭时只看今天；开启时也接受昨天创建的跨夜活动
                    if is_today or (cross_day_enabled and is_yesterday):
                        scheduled_goals.append((goal, tw, is_today, is_yesterday))

            if not scheduled_goals:
                result = (None, None, [], None)
                await self._schedule_cache.set(cache_key, result)
                return result

            def _get_start_minutes(item: Tuple[Any, List[int], bool, bool]) -> int:
                _, tw, _, _ = item
                if not tw or len(tw) < 2:
                    return 0
                start_val = tw[0]
                # 兼容旧格式（小时表示）：end_val > 24 为分钟格式
                return start_val if tw[1] > 24 else start_val * 60

            scheduled_goals.sort(key=_get_start_minutes)

            # 查找当前活动：
            #   今天创建的：直接判 current_time_minutes 是否落在 time_window 内（含跨夜）
            #   昨天创建的：只有 time_window 跨夜（end_minutes > 1440）才可能延续到今天凌晨
            current_activity: Optional[str] = None
            current_description: Optional[str] = None
            current_activity_type: Optional[str] = None
            current_goal_created_at: Any = None

            for goal, tw, is_today, is_yesterday in scheduled_goals:
                start_minutes, end_minutes = parse_time_window(tw)
                if start_minutes is None:
                    continue

                is_cross_day = end_minutes > 1440

                if is_today:
                    if is_cross_day:
                        is_in_window = (start_minutes <= current_time_minutes < 1440) or (
                            0 <= current_time_minutes < (end_minutes - 1440)
                        )
                    else:
                        is_in_window = start_minutes <= current_time_minutes < end_minutes
                elif is_yesterday and is_cross_day:
                    is_in_window = 0 <= current_time_minutes < (end_minutes - 1440)
                else:
                    continue

                if is_in_window:
                    # 多个匹配时，选创建时间最新（防 tz-aware/naive 混比报错）
                    if current_activity is None or (
                        goal.created_at and (
                            current_goal_created_at is None
                            or strip_tz(goal.created_at) > strip_tz(current_goal_created_at)
                        )
                    ):
                        current_activity = goal.name
                        current_description = goal.description
                        current_activity_type = goal.goal_type
                        current_goal_created_at = goal.created_at

            # 收集所有未来活动（仅今天创建的活动）
            all_future_activities: List[Tuple[str, str]] = []
            for goal, tw, is_today, _is_yesterday in scheduled_goals:
                if not is_today:
                    continue
                start_val = tw[0] if len(tw) > 0 else 0
                end_val = tw[1] if len(tw) > 1 else start_val + 60
                start_minutes = start_val if end_val > 24 else start_val * 60

                if start_minutes > current_time_minutes:
                    hour = start_minutes // 60
                    minute = start_minutes % 60
                    time_str = f"{hour:02d}:{minute:02d}"
                    all_future_activities.append((time_str, goal.name))

            result = (current_activity, current_description, all_future_activities, current_activity_type)
            await self._schedule_cache.set(cache_key, result)
            return result
        except Exception as exc:
            logger.warning(f"获取当前日程信息失败: {exc}")
            return (None, None, [], None)
