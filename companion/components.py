"""companion 组件层（来源：虚拟恋人插件 plugin.py 的组件面）。

``CompanionComponentsMixin`` 供 plugin.py 的主类多继承：
``class CateyeSuitePlugin(CompanionComponentsMixin, MaiBotPlugin)``。
mixin 方法开头一律判空 ``getattr(self, "_companion", None)``（服务束未挂上时降级）。

组件清单（组件名跟源插件）：
- Hooks ×5：
  - chat.receive.before_process        发送者身份缓存（BLOCKING/EARLY，只记录不拦截）
  - chat.receive.after_process         目标用户私聊时间（OBSERVE/LATE）
  - maisaka.planner.before_request     过期主动任务块剥离 + 屏幕旁白 + 活动后缀 +
                                       好感度 + 主动回合软约束（BLOCKING/NORMAL；
                                       items 强 schema 协议 + messages 双投影 +
                                       modified_kwargs 全量回传 + >8MB 载荷收缩）
  - maisaka.replyer.before_model_request  过期主动任务块剥离 + 仅屏幕旁白注入
                                       （BLOCKING/NORMAL）
  - maisaka.replyer.after_response     90 秒窗口补计 0.5 发言数 + spoken 回执（OBSERVE/LATE）
- Tools ×6：cateye_test_mai_lover_current_activity / cateye_test_mai_lover_status /
  cateye_test_mai_lover_schedule / cateye_test_mai_lover_send_message /
  cateye_test_mai_lover_affection / cateye_test_mai_lover_config
- Commands ×8：/cateye_test_mai_status /cateye_test_mai_schedule /cateye_test_mai_affection
  /cateye_test_mai_help /cateye_test_mai_config /cateye_test_mai_llm_log
  /cateye_test_mai_diag /cateye_test_mai_test（鉴权：target_qq 白名单 + 本机控制台，
  占位默认值一律拒绝）
- APIs ×3：companion_get_current_activity / companion_get_schedule /
  companion_get_affection_level（源插件 get_* 与 planning 子模块撞名，故加前缀）

「今日日程」一律经 planning.schedule_query.ScheduleQuery 取
（服务束解析 ``plugin._planning``，缺省回退 ``plugin._schedule_query``）。
"""

from __future__ import annotations

import json as _json
from datetime import datetime
from typing import Any, Optional

from maibot_sdk import API, Command, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from core.diag import DecisionLogger
from core.inject import pick_payload_key, project_injection, without_stale_proactive_blocks
from planning.schedule_query import ScheduleQuery

from .screen_context import (
    CHANNEL_PLANNER,
    CHANNEL_REPLYER,
    ScreenContextStore,
    build_narration_item,
)
from .sender_identity import (
    extract_session_info_from_message,
    extract_user_id_from_message,
    sender_ids_in_window,
)

# ──────────────────────────────────────────────────────────────────────────
# 注入机制常量（来源：源插件 plugin.py 的 [LOCAL-PATCH:cateye] 注释块）
# ──────────────────────────────────────────────────────────────────────────
#: 超过此体积就先省略图片再注入；宿主单帧上限 16MB，留编码余量。
_MAX_INJECT_PAYLOAD_BYTES = 8 * 1024 * 1024
#: 距离上次 proactive trigger 多久内，认为当前 planner 回合是主动发言回合。
_PROACTIVE_RULE_WINDOW_SECONDS = 90.0
#: 省略图片时替换成的文本占位符（不能直接删 part，否则 item 可能没有 part）。
_IMAGE_OMITTED_PLACEHOLDER = "[图片已省略：为控制上下文体积]"

#: 主动行为决策日志里的跳过原因 → 中文说明（/mai_diag 展示用）。
_DIAG_REASON_LABELS: dict[str, str] = {
    "no_candidate": "本轮没有满足条件的触发",
    "silence": "处于静默时段",
    "invalid_time_config": "时间配置非法，按 fail-safe 处理",
    "min_interval": "未到主动发言最小间隔",
    "cooldown": "处于用户冷却期",
    "budget": "当日发言上限已用完",
    "dice": "概率未通过",
    "already_sent_today": "今天已经发过（每天一次）",
    "no_user_message": "还没有用户消息记录",
    "miss_duration": "想念时长条件未满足",
    "future_schedule": "未来窗口内有日程节点",
    "llm_rejected": "想念把关被 LLM 驳回",
    "planner_trigger_failed": "触发未入队（stream_id 缺失或开关关闭）",
    "disabled": "主动触发开关已关闭",
    "daily_max_zero": "每日发言上限为 0",
    "reply_confirmed": "planner 已生成并发送回复",
    "tool_send_message": "planner 通过 Tool 主动发消息",
}

#: 注入作用域
_SCOPE_PRIVATE = "private"
_SCOPE_SKIP = "skip"

#: 决策日志动作值（core.diag.DecisionLogger 的类属性别名，源插件为模块常量 ACTION_SPOKEN）
_ACTION_SPOKEN = DecisionLogger.ACTION_SPOKEN

#: 目标 QQ 的占位默认值（一律拒绝鉴权 / 不做身份判定）
_PLACEHOLDER_QQ = "123456789"


def _message_plain_text(message: Any) -> str:
    """取入站消息纯文本（M2 会话裁判的「对话摘要」来源）。

    ``chat.receive.after_process`` 触发时 ``processed_plain_text`` 已生成（宿主
    message 结构 §10.2），优先取它；缺失时回退 ``raw_message`` 里的文本段拼接。
    取不到返回空串（裁判输入侧自行省略该条）。
    """
    if not isinstance(message, dict):
        return ""
    text = str(message.get("processed_plain_text") or "").strip()
    if text:
        return text
    parts = []
    raw = message.get("raw_message")
    if isinstance(raw, list):
        for segment in raw:
            if isinstance(segment, dict) and str(segment.get("type") or "") == "text":
                parts.append(str(segment.get("data") or ""))
    return " ".join(part for part in parts if part).strip()


# ---------------------------------------------------------------------------
# 纯函数：>8MB 载荷收缩（把图片 part 换成文本占位符后照常注入）
# ---------------------------------------------------------------------------


def _payload_json_size(payload: Any) -> int:
    """估算 payload 的 JSON 体积（与宿主 RPC 帧编码量级一致）。"""
    return len(_json.dumps(payload, default=str, ensure_ascii=False))


def _shrink_payload_for_injection(payload: list[Any]) -> tuple[Optional[list[Any]], int]:
    """payload 超过阈值时，按体积从大到小把图片 part 换成文本占位符。

    宿主把图片以 image_base64 内联进 items 快照，带图的上下文常态就是 2–12MB。
    替换成文本占位符而不是删掉，保证 item 仍有 part、宿主反序列化不会失败。

    Returns:
        ``(可注入的 payload 或 None, 被省略的图片数)``；``None`` 表示裁剪后仍超限。
    """
    size = _payload_json_size(payload)
    if size <= _MAX_INJECT_PAYLOAD_BYTES:
        return payload, 0

    candidates: list[tuple[int, int, int]] = []
    for item_index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        parts = item.get("parts")
        if not isinstance(parts, list):
            continue
        for part_index, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") == "image":
                candidates.append((_payload_json_size(part), item_index, part_index))
    candidates.sort(reverse=True)

    placeholder: dict[str, Any] = {"type": "text", "text": _IMAGE_OMITTED_PLACEHOLDER}
    placeholder_size = _payload_json_size(placeholder)
    items: list[Any] = [dict(item) if isinstance(item, dict) else item for item in payload]
    omitted = 0
    for part_size, item_index, part_index in candidates:
        if size <= _MAX_INJECT_PAYLOAD_BYTES:
            break
        item = items[item_index]
        parts = list(item.get("parts") or [])
        if part_index >= len(parts):
            continue
        parts[part_index] = dict(placeholder)
        item["parts"] = parts
        size = size - part_size + placeholder_size
        omitted += 1

    if omitted == 0 or size > _MAX_INJECT_PAYLOAD_BYTES:
        return None, omitted
    return items, omitted


class CompanionComponentsMixin:
    """虚拟恋人组件面（Hooks/Tools/Commands/APIs），挂在整合插件主类上。"""

    # ==================================================================
    # 内部取用（全部判空降级）
    # ==================================================================

    def _mai_companion(self) -> Any:
        return getattr(self, "_companion", None)

    def _mai_schedule_query(self) -> Optional[ScheduleQuery]:
        companion = self._mai_companion()
        if companion is not None:
            return getattr(companion, "schedule_query", None)
        # 服务束未挂上时直接从 plugin 兜底取（仍走 planning 模块）
        return getattr(self, "_planning", None) or getattr(self, "_schedule_query", None)

    def _mai_lover_name(self) -> str:
        companion = self._mai_companion()
        if companion is not None:
            return str(getattr(companion, "lover_name", "") or "麦麦")
        return str(getattr(self, "_lover_name", "") or "麦麦")

    def _mai_target_qq(self) -> str:
        """目标 QQ（占位默认值/空值返回空串，用于身份判定）。"""
        try:
            raw = str(self.config.target.target_qq or "").strip()
        except Exception:
            return ""
        return "" if raw in ("", "0", _PLACEHOLDER_QQ) else raw

    def _mai_target_stream_id(self) -> str:
        companion = self._mai_companion()
        if companion is not None:
            status = companion.patrol.patrol_status()
            return str(status.get("stream_id") or "")
        return ""

    def _mai_log_proactive_event(self, *, action: str, reason: str, trigger_type: str = "",
                                 detail: str = "", **fields: Any) -> None:
        """把主动发言相关事件写进决策日志（未启用时 no-op，日志故障不影响流程）。"""
        companion = self._mai_companion()
        if companion is None:
            return
        decision_logger = getattr(companion, "decision_logger", None)
        if decision_logger is None:
            return
        try:
            decision_logger.record(action=action, reason=reason, trigger_type=trigger_type,
                                   detail=detail, **fields)
        except Exception as e:
            try:
                self.ctx.logger.debug(f"写主动行为决策日志失败（忽略）: {e}")
            except Exception:
                pass

    # ==================================================================
    # Hooks
    # ==================================================================

    @HookHandler(
        "chat.receive.before_process",
        name="mai_lover_sender_identity",
        description="记录入站消息的「消息 ID → 发送者 QQ 号」与「会话 ID → 是否群聊」，供 planner 注入按 QQ 号判定恋人",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_sender_identity(self, message: dict[str, Any] | None = None,
                                       **kwargs: Any) -> None:
        """入站消息记录发送者身份（只记录，不拦截、不改写）。"""
        companion = self._mai_companion()
        if companion is None:
            return None
        try:
            session_id, is_group = extract_session_info_from_message(message)
            if session_id and is_group is not None:
                companion.session_kinds.record(session_id, is_group)
            user_id = extract_user_id_from_message(message)
            message_id = message.get("message_id") if isinstance(message, dict) else None
            if user_id and message_id:
                companion.senders.record(message_id, user_id)
        except Exception:
            pass  # 记录失败绝不影响消息处理
        return None

    @HookHandler(
        "chat.receive.after_process",
        name="mai_lover_target_private_message_observer",
        description="记录目标用户的私聊发言时间，供主动聊天冷却与想念机制使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_target_private_message(self, message: dict[str, Any] | None = None,
                                              **kwargs: Any) -> None:
        """记录目标用户的入站私聊；群聊和其他用户不影响巡检调度。

        M1 接线（§5.6）：非通知消息发 ``relation.message_received``
        （is_target 判定结果 / source=auto / stream_kind；私聊 target 判定复用
        sender_identity 既有链路；群聊里非 target 的发言不上报，避免群流水刷日志）；
        target 私聊（会话类型未知时按私聊兜底）再经 affection_events 记一条
        ``chat_round`` 规则事件（日累计 ≤ +1.0），并刷新 last_user_msg_time
        （顺带触发 ``user_initiated`` 检测，§5.4）。``is_notify`` 消息只按
        target 私聊口径刷新 last_user_msg_time，不落入其余链路（TEST_PROCEDURE §二）。
        """
        companion = self._mai_companion()
        if companion is None:
            return
        try:
            if not isinstance(message, dict):
                return
            message_info = message.get("message_info")
            if not isinstance(message_info, dict):
                return
            _session_id, is_group = extract_session_info_from_message(message)
            stream_kind = "group" if is_group is True else ("private" if is_group is False else "")
            user_id = extract_user_id_from_message(message)
            target_qq = self._mai_target_qq()
            is_target = bool(user_id and target_qq and user_id == target_qq)
            if message.get("is_notify"):
                # is_notify（撤回/戳一戳等通知，只入库不触发回复）：仅按 target 私聊口径
                # 低成本刷新 last_user_msg_time（沉默检测/想念与主动冷却），不触发
                # relation.message_received / chat_round / 会话采样评估 / 主动行为。
                if is_target and stream_kind != "group":
                    companion.affection.update_last_user_msg_time(datetime.now())
                return
            # 群聊里非 target 的发言不上报（群流水会刷爆决策日志）；其余消息带上判定结果
            if is_group is True and not is_target:
                return
            await self._mai_publish_message_received(companion, is_target=is_target,
                                                     stream_kind=stream_kind)
            if is_target and stream_kind != "group":
                # 会话类型未知时按私聊兜底（与旧实现一致：group_info 缺失视为私聊）
                companion.affection.update_last_user_msg_time(datetime.now())
                companion.affection.record_rule_event("chat_round", None, "与 target 一问一答")
                self._mai_note_review_round(companion, message)
        except Exception:
            pass

    def _mai_note_review_round(self, companion: Any, message: Any) -> None:
        """M2 会话采样触发点（§5.4-1，挂在 chat_round 记账点旁）。

        只做滚动缓冲 + 轮次计数 + 双频控；命中采样时由 AffectionReviewer 调度后台
        LLM 任务并立即返回——本 Hook timeout 只有 1s，绝不内联等待 LLM。
        """
        reviewer = getattr(companion, "reviews", None)
        if reviewer is None:
            return
        try:
            reviewer.note_target_message(_message_plain_text(message), now=datetime.now())
        except Exception as e:
            try:
                self.ctx.logger.debug(f"会话采样触发失败（忽略）: {e}")
            except Exception:
                pass

    async def _mai_publish_message_received(self, companion: Any, *, is_target: bool,
                                            stream_kind: str) -> None:
        """发布 ``relation.message_received``（M1 接线；总线缺失时静默降级）。"""
        bus = getattr(self, "_bus", None) or getattr(companion, "bus", None)
        if bus is None:
            return
        try:
            await bus.publish("relation.message_received", source="auto",
                              is_target=bool(is_target), stream_kind=str(stream_kind or ""))
        except Exception as e:
            try:
                self.ctx.logger.debug(f"发布 relation.message_received 失败（忽略）: {e}")
            except Exception:
                pass

    @HookHandler(
        "maisaka.planner.before_request",
        name="mai_lover_planner_inject",
        description="在 planner 请求前注入屏幕旁白、当前活动、好感度与主动回合回复目标约束",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_planner_inject(self, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        """注入恋人上下文（屏幕旁白 + 日期行 + 当前活动 + 好感度 + 主动回合软约束）。

        屏幕旁白与活动注入相互独立，所以先算旁白——保证任何一条早退路径都不会
        把旁白丢掉。items 是强 schema 载荷：新条目 meta 必须完整，modified_kwargs
        必须全量回传（core.inject 负责构造合法快照）。
        """
        # mai-love v2.6.2：先剥离过期的宿主主动任务块（与后续注入相互独立，
        # 放在最前，保证下面任何早退路径都不受影响）。
        stripped = without_stale_proactive_blocks(kwargs)
        if stripped is not None:
            kwargs = stripped

        companion = self._mai_companion()
        if companion is None:
            return {"action": "continue"}

        # 1) 屏幕旁白（尾随锚点，失败/无待注入时原样返回 None）
        narration_kwargs = self._mai_apply_screen_narration(kwargs, CHANNEL_PLANNER)
        if narration_kwargs is not None:
            kwargs = narration_kwargs

        # 2) 活动注入（日程服务不可用 → 只保留旁白结果）
        payload_key, payload = pick_payload_key(kwargs)
        if not payload_key:
            return {"action": "continue", "modified_kwargs": kwargs}

        scope = self._mai_resolve_injection_scope(str(session_id or ""), payload)

        # 3) 超限不再整体跳过：先把最大的图片 part 换成文本占位符再注入
        shrunk, omitted = _shrink_payload_for_injection(payload)
        if shrunk is None:
            try:
                self.ctx.logger.warning(
                    f"planner payload 过大且裁剪后仍超限 (>{_MAX_INJECT_PAYLOAD_BYTES} bytes)，跳过活动注入"
                )
            except Exception:
                pass
            return {"action": "continue", "modified_kwargs": kwargs}
        if omitted:
            try:
                self.ctx.logger.info(f"planner payload 过大，已省略 {omitted} 张图片以完成活动注入")
            except Exception:
                pass

        suffix = await self._mai_build_activity_suffix(scope)
        if scope != _SCOPE_SKIP:
            suffix += self._mai_build_proactive_target_rule()
        if not suffix.strip():
            # 没有可注入文本：不回传收缩后的载荷（避免白丢图片）
            return {"action": "continue", "modified_kwargs": kwargs}

        modified_payload = project_injection(payload_key, shrunk, suffix)
        return {"action": "continue", "modified_kwargs": {**kwargs, payload_key: modified_payload}}

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="mai_lover_replyer_screen_inject",
        description="把屏幕旁白以尾随锚点方式插进 replyer 的模型请求上下文",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_replyer_model_inject(self, **kwargs: Any) -> dict[str, Any]:
        """replyer 通道只做屏幕旁白注入，不碰 extra_prompt 等其他入参。

        replyer 的上下文与 planner 不一定相同，所以旁白的锚点在两个通道里
        分开识别、分开失效（见 screen_context）。
        """
        # mai-love v2.6.2：replyer 的上下文同样携带宿主主动任务块，一并剥离过期块
        stripped = without_stale_proactive_blocks(kwargs)
        if stripped is not None:
            kwargs = stripped

        companion = self._mai_companion()
        if companion is None:
            return {"action": "continue"}
        modified = self._mai_apply_screen_narration(kwargs, CHANNEL_REPLYER)
        if modified is None:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": modified}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="mai_lover_replyer_speak_confirm",
        description="巡检触发后 90 秒窗口内的真实回复补计 0.5 发言数，并记录 spoken 回执",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_replyer_after_response(self, **kwargs: Any) -> None:
        """planner 实际生成回复时补计 0.5 触发数并记一条"确认发言"。

        与 _trigger_planner 的 0.5 配合：回复成功总计 1.0，未回复总计 0.5。
        仅在最近 90 秒内有 proactive trigger 时生效；补计后清除待确认触发，
        因此与顶层 speak_counter 钩子共存时不会重复计数。
        """
        companion = self._mai_companion()
        if companion is None:
            return
        patrol = companion.patrol
        last_trigger = patrol.get_last_trigger_time()
        if last_trigger is None:
            return  # 没有待确认的 proactive trigger
        now = datetime.now()
        elapsed = (now - last_trigger).total_seconds()
        if elapsed >= 90:
            return
        trigger_type = patrol.get_last_trigger_intent()
        companion.affection.increment_speak(0.5)
        patrol.clear_last_trigger_time()
        self._mai_log_proactive_event(
            action=_ACTION_SPOKEN,
            reason="reply_confirmed",
            trigger_type=trigger_type,
            detail=f"planner 已生成回复并发送（距触发 {elapsed:.0f} 秒）｜触发类型={trigger_type or '未知'}",
        )

    # ---- 注入辅助（屏幕旁白 / 作用域 / 文案）----

    def _mai_apply_screen_narration(self, kwargs: dict[str, Any], channel: str) -> Optional[dict[str, Any]]:
        """在请求载荷里按「尾随锚点」插入屏幕旁白；没有可注入内容时返回 None。"""
        companion = self._mai_companion()
        store: Optional[ScreenContextStore] = getattr(companion, "screen_store", None)
        if store is None:
            return None
        payload_key, payload = pick_payload_key(kwargs)
        if not payload_key:
            return None
        session_id = str(kwargs.get("session_id") or "")
        if self._mai_resolve_injection_scope(session_id, payload) != _SCOPE_PRIVATE:
            return None  # 屏幕内容绝不漏到非恋人的聊天流
        taken = store.take_injection(session_id, channel, payload)
        if taken is None:
            return None
        insert_at, text = taken
        anchor = payload[insert_at - 1] if 0 < insert_at <= len(payload) else None
        item = build_narration_item(text, anchor, now=datetime.now())
        new_payload = list(payload)
        new_payload.insert(insert_at, item)
        return {**kwargs, payload_key: new_payload}

    def _mai_resolve_injection_scope(self, session_id: str, payload: list[Any]) -> str:
        """判断本次请求该注入哪一档上下文：``private`` / ``skip``。

        1. session_id 等于已解析的目标私聊 stream_id → private；
        2. 已知是私聊（会话类型 False）且最近一条用户消息的发送者 QQ 就是目标
           用户 → private（宿主重建会话导致 stream_id 变化时的兜底）；
        3. 其余一律 skip。判定只看 QQ 号（昵称可被伪造）；反查不到按 skip。

        注：源插件的群聊注入档在整合版裁剪（无 [injection] 配置段，群聊不注入
        恋人上下文，见 INTEGRATION.md）。
        """
        companion = self._mai_companion()
        if companion is None:
            return _SCOPE_SKIP
        sid = str(session_id or "").strip()
        target_stream = self._mai_target_stream_id()
        target_qq = self._mai_target_qq()
        if target_stream and sid and sid == target_stream:
            return _SCOPE_PRIVATE

        # 私聊兜底：stream_id 变了（宿主重建会话）但对方确实是目标用户
        is_group = companion.session_kinds.is_group(sid)
        if (
            is_group is False
            and target_qq
            and sender_ids_in_window(payload, companion.senders, 1)[-1:] == [target_qq]
        ):
            return _SCOPE_PRIVATE
        return _SCOPE_SKIP

    async def _mai_build_activity_suffix(self, scope: str) -> str:
        """构造注入文本：skip 档只注入中性的日期行；private 档加状态与好感度。"""
        companion = self._mai_companion()
        if companion is None:
            return ""
        now = datetime.now()
        current_date = now.strftime("%Y-%m-%d")
        weekday = "一二三四五六日"[now.weekday()]
        holiday_info = "工作日"
        holiday_svc = getattr(companion, "holiday", None)
        if holiday_svc is not None:
            try:
                holiday_info = await holiday_svc.get_holiday_info(current_date)
            except Exception:
                holiday_info = "工作日"

        suffix = f"\n【当前日期】今天是 {current_date}（星期{weekday}），{holiday_info}。"
        if scope == _SCOPE_SKIP:
            return suffix

        current_time = now.strftime("%H:%M")
        name = self._mai_lover_name()
        target_qq = self._mai_target_qq()
        relation = getattr(companion, "relation", None)
        relation_word = getattr(relation, "word", None) or "恋人"
        target_label = f"QQ {target_qq}" if target_qq else relation_word
        affection = companion.affection
        # M1 注入接线（§5.6.1）：好感后缀改读 v2 5 档档位描述（经 relation profile；
        # 关系档案缺失时回退 affection 的「恋人」5 档基底文案）。habits 通道不在此处。
        tier = affection.tier()
        if relation is not None:
            tier_label = relation.tier_label(tier)
            tier_desc = relation.tier_description(tier)
        else:
            tier_label = affection.tier_label()
            tier_desc = affection.tier_description()

        # 无日程时不注入日程状态（日切后日程未生成时不出现占位句）
        schedule_query = self._mai_schedule_query()
        activity_line = ""
        if schedule_query is not None:
            try:
                current = schedule_query.find_current_activity(current_date, now)
                if current is not None:
                    activity_line = schedule_query.format_activity_line(current, now)
            except Exception:
                activity_line = ""
        if activity_line:
            suffix += f"\n【{name}当前状态】现在 {current_time}，{name}正在{activity_line}。"

        suffix += (f"\n【{name}与{relation_word}（{target_label}）的好感度】"
                   f"档位 {tier}·{tier_label}（{tier_desc}）")
        # M2 注入叠加（§5.6.1）：最近 1~2 条 impressions + 最近一次日结 tone；
        # 无印象/无 tone 时返回空串，注入内容与 M1 完全一致（降级为现状）。
        memory_line = self._mai_affection_memory_line(affection)
        if memory_line:
            suffix += "\n" + memory_line
        return suffix

    @staticmethod
    def _mai_affection_memory_line(affection: Any) -> str:
        """好感记忆行（注入与命令共用）：最近 1~2 条印象 + 最近一次日结关系温度。

        状态层缺失/为空/异常一律返回空串（绝不空指针，也绝不给调用方抛错）。
        """
        try:
            impressions = affection.impressions()[-2:]
        except Exception:
            impressions = []
        lines = []
        entries = []
        for item in impressions or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if text:
                entries.append(f"{str(item.get('date') or '').strip()} {text}".strip())
        if entries:
            lines.append("【最近印象】" + "；".join(entries))
        try:
            tone = affection.tone()
        except Exception:
            tone = None
        if isinstance(tone, dict) and str(tone.get("text") or "").strip():
            lines.append(f"【关系温度·{str(tone.get('date') or '').strip()}】"
                         f"{str(tone.get('text')).strip()}")
        return "\n".join(lines)

    def _mai_build_proactive_target_rule(self) -> str:
        """主动发言回合给 planner 的回复目标约束（软约束，宿主无法强制目标）。"""
        companion = self._mai_companion()
        if companion is None:
            return ""
        last_trigger = companion.patrol.get_last_trigger_time()
        if last_trigger is None:
            return ""
        if (datetime.now() - last_trigger).total_seconds() > _PROACTIVE_RULE_WINDOW_SECONDS:
            return ""
        return (
            "\n【本轮是主动发言】现在是你在主动找对方说话（不是对方来找你）："
            "\n- 不要回复你自己发送的消息，也不要在发言里引用你自己的消息；"
            "\n- 优先回应对方最后一条发言；如果最后一条发言是你自己的（对方还没回你），"
            "就当作对方还没回，直接说新内容或起一个新话题；"
            "\n- reply 的 msg_id 只能选对方发送的消息。"
        )

    # ==================================================================
    # Tools
    # ==================================================================

    @Tool(
        "cateye_test_mai_lover_current_activity",
        description="查询麦麦现在正在做什么。当用户问'在干嘛''在做什么'或想了解麦麦当前状态时调用。",
    )
    async def mai_tool_current_activity(self, **kwargs: Any) -> str:
        schedule_query = self._mai_schedule_query()
        name = self._mai_lover_name()
        if schedule_query is None:
            return f"{name}的日程服务还没准备好，暂时不清楚在做什么。"
        now = datetime.now()
        try:
            current = schedule_query.find_current_activity(now.strftime("%Y-%m-%d"), now)
        except Exception:
            return f"{name}的日程服务暂时不可用。"
        if current is None:
            return f"{name}现在没有安排中的活动（可能在休息）。"
        return f"{name}现在正在：{schedule_query.format_activity_line(current, now)}。"

    @Tool(
        "cateye_test_mai_lover_status",
        description="查看麦麦恋人的当前状态：好感度档位、今日发言次数、日程摘要",
    )
    async def mai_tool_status(self, **kwargs: Any) -> str:
        return self._mai_status_report()

    @Tool(
        "cateye_test_mai_lover_schedule",
        description="查看麦麦的今日完整日程安排",
    )
    async def mai_tool_schedule(self, **kwargs: Any) -> str:
        return self._mai_schedule_report()

    @Tool(
        "cateye_test_mai_lover_send_message",
        description="以 bot 的口吻向关系对象主动发送一条消息（关系类型见配置 relation_type：恋人/挚友/维护者/家人），口吻与后缀随关系自动变化",
        parameters={
            "message": {
                "type": "string",
                "description": "要发送的消息文本（可选，留空则自动生成）",
                "required": False,
            },
        },
    )
    async def mai_tool_send_message(self, message: str = "", **kwargs: Any) -> str:
        """LLM Tool: 主动发送恋人消息（带情绪后缀；成功计 1 次发言数并记决策日志）。"""
        companion = self._mai_companion()
        if companion is None:
            return "恋人服务未初始化。"
        stream_id = self._mai_target_stream_id()
        name = self._mai_lover_name()
        if not stream_id:
            return f"{name}还没有连接到目标用户，请稍后再试。"

        if not str(message or "").strip():
            relation = getattr(companion, "relation", None)
            relation_word = getattr(relation, "word", None) or "恋人"
            style_word = getattr(relation, "style_word", None) or "温柔亲昵的恋人"
            self_desc = getattr(relation, "self_desc", None) or "一个温柔体贴的虚拟恋人"
            fallback = ("想你了呢~在忙什么呀？" if relation_word == "恋人"
                        else "在忙什么呀？最近怎么样？~")
            message = await companion.llm.generate_or_fallback(
                prompt=f"请用{style_word}的口吻，对你的{relation_word}说一句问候或分享一件小事（20-40字）。",
                fallback=fallback,
                system_prompt=f"你是「{name}」（{self_desc}），此刻正主动找你的{relation_word}说话。",
                temperature=0.8,
                event="tool_send_message",
            )

        final_text = companion.message.append_affection_suffix(str(message))
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.error(f"[cateye_test_mai_lover_send_message] 发送异常: {e}")
            return f"消息发送异常: {e}"
        if result:
            companion.affection.increment_speak()
            self._mai_log_proactive_event(
                action="trigger",
                reason="tool_send_message",
                trigger_type="tool",
                detail=f"planner 通过 Tool 主动发了一条消息：{final_text[:40]}",
            )
            return f"消息已发送: {final_text[:60]}..."
        return "消息发送失败: 发送返回 False"

    @Tool(
        "cateye_test_mai_lover_affection",
        description="调整与关系对象的好感度：把 score 移动到 0/1/2 兼容档对应的 v2 5 档区间中值并重估档位"
                    "（档位名称随关系类型变化，如恋人=熟悉/亲密/热恋）",
        parameters={
            "level": {
                "type": "integer",
                "description": "好感度兼容档位（3 档口径），只能填 0、1 或 2",
                "required": True,
            },
        },
    )
    async def mai_tool_affection(self, level: int = 0, **kwargs: Any) -> str:
        """LLM Tool：按 3 档兼容口径移动 score 到对应 v2 档区间（管理员兜底，跳过迟滞）。"""
        companion = self._mai_companion()
        if companion is None:
            return "好感度管理器未初始化。"
        if level not in (0, 1, 2):
            return f"档位只能填 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。"
        companion.affection.update_level(int(level))
        self.ctx.logger.info(f"好感度已通过 Tool 调整到兼容档 {level}（score 移至对应 v2 档区间）")
        return f"好感度已更新：{self._mai_affection_state_line()}"

    @Tool(
        "cateye_test_mai_lover_config",
        description="查看麦麦恋人当前插件配置：巡检间隔、时间窗口、概率、今日发言上限、好感度",
    )
    async def mai_tool_config(self, **kwargs: Any) -> str:
        return self._mai_config_summary(multiline=False)

    # ==================================================================
    # Commands
    # ==================================================================

    def _is_authorized_command_user(self, user_id: str, is_local_operator: bool = False) -> bool:
        """校验命令触发者是否有权使用 /cateye_test_mai_* 命令。

        规则：本机控制台操作员天然放行；其余仅白名单 target_qq 本人可用
        （兼容 ``qq:123456`` 平台前缀写法）。target_qq 为占位默认值/无效值时
        一律拒绝（默认拒绝）。
        """
        if is_local_operator:
            return True
        try:
            target_qq = str(self.config.target.target_qq or "").strip()
        except Exception:
            return False
        if not target_qq or target_qq in ("0", _PLACEHOLDER_QQ):
            return False
        candidate = str(user_id or "").strip()
        if not candidate:
            return False
        return candidate in (target_qq, f"qq:{target_qq}")

    async def _authorize_command(self, kwargs: dict[str, Any]) -> bool:
        """命令权限守卫：无权限时发送提示并返回 False。"""
        if self._is_authorized_command_user(
            str(kwargs.get("user_id", "") or ""),
            bool(kwargs.get("is_local_operator", False)),
        ):
            return True
        stream_id = str(kwargs.get("stream_id", "") or "")
        if stream_id:
            try:
                await self.ctx.send.text("此命令仅限绑定的恋人用户使用哦~", stream_id)
            except Exception:
                pass
        return False

    @Command(name="/cateye_test_mai_status", pattern=r"(?<!\S)/cateye_test_mai_status\s*$",
             description="查看麦麦恋人状态（好感度、发言计数、日程摘要）")
    async def mai_cmd_status(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        try:
            await self.ctx.send.text(text=self._mai_status_report(), stream_id=stream_id)
            return True, "状态已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_status 发送失败: {e}")
            return False, f"发送失败: {e}", True

    @Command(name="/cateye_test_mai_schedule", pattern=r"(?<!\S)/cateye_test_mai_schedule\s*$",
             description="查看麦麦今日完整日程")
    async def mai_cmd_schedule(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        try:
            await self.ctx.send.text(text=self._mai_schedule_report(), stream_id=stream_id)
            return True, "日程已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_schedule 发送失败: {e}")
            return False, f"发送失败: {e}", True

    @Command(name="/cateye_test_mai_affection", pattern=r"(?<!\S)/cateye_test_mai_affection(?:\s+(?P<mai_level>\S+))?\s*$",
             description="查看/调整好感度（score/baseline/档位/今日Δ）。用法: /cateye_test_mai_affection [0|1|2]")
    async def mai_cmd_affection(self, **kwargs: Any) -> tuple[bool, str, bool]:
        companion = self._mai_companion()
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")

        # 参数优先取正则命名捕获（兼容"引用+命令"场景下 text 前缀混入其他内容）
        level_str = ""
        matched_groups = kwargs.get("matched_groups")
        if isinstance(matched_groups, dict):
            level_str = str(matched_groups.get("mai_level", "") or "").strip()

        async def _reply(text: str) -> None:
            try:
                await self.ctx.send.text(text=text, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.warning(f"mai_cmd_affection 回复发送失败: {e}")

        if not level_str:
            # 不带参数 = 查看好感 v2 状态（M2：含最近印象与关系温度）
            current = self._mai_affection_state_line() if companion else "好感度管理器未初始化。"
            await _reply(
                "用法: /cateye_test_mai_affection <0|1|2>（不带参数只看状态）\n"
                "0 = 熟悉档区间 | 1 = 亲密档区间 | 2 = 热恋档区间（3 档兼容口径，实际按 v2 score 运行）\n"
                f"当前状态: {current}"
            )
            return False, "参数错误", True
        try:
            level = int(level_str)
        except (ValueError, TypeError):
            await _reply(f"「{level_str}」不是有效数字，请使用 0、1 或 2。")
            return False, "参数错误", True
        if level not in (0, 1, 2):
            await _reply(f"好感度档位只能是 0（熟悉）、1（亲密）或 2（热恋），收到的是 {level}。")
            return False, "参数错误", True
        if companion is None:
            await _reply("好感度管理器未初始化，无法调整。")
            return False, "管理器未初始化", True
        companion.affection.update_level(level)
        self.ctx.logger.info(f"好感度已通过命令调整到兼容档 {level}（score 移至对应 v2 档区间）")
        await _reply(f"好感度已更新：{self._mai_affection_state_line()}")
        return True, "好感度已调整", True

    @Command(name="/cateye_test_mai_help", pattern=r"(?<!\S)/cateye_test_mai_help\s*$",
             description="查看麦麦恋人所有可用命令")
    async def mai_cmd_help(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        name = self._mai_lover_name()
        help_text = (
            "🐱 麦麦恋人 可用命令:\n"
            f"/cateye_test_mai_status    — 查看{name}状态（好感度/今日发言/日程摘要）\n"
            "/cateye_test_mai_schedule  — 查看今日完整日程\n"
            "/cateye_test_mai_affection — 查看/调整好感度: /cateye_test_mai_affection [0|1|2]\n"
            "/cateye_test_mai_config    — 查看当前插件配置摘要\n"
            "/cateye_test_mai_llm_log   — 查看 LLM 调用日志: /cateye_test_mai_llm_log [天数]\n"
            "/cateye_test_mai_diag      — 查看主动行为决策日志（为什么发言/为什么没发言）: /cateye_test_mai_diag [天数]\n"
            "/cateye_test_mai_test      — 发送一条测试消息（验证发送通道）\n"
            "/cateye_test_mai_help      — 本帮助"
        )
        try:
            await self.ctx.send.text(text=help_text, stream_id=stream_id)
            return True, "帮助已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_help 发送失败: {e}")
            return False, f"发送失败: {e}", True

    @Command(name="/cateye_test_mai_config", pattern=r"(?<!\S)/cateye_test_mai_config\s*$",
             description="查看麦麦恋人当前配置摘要")
    async def mai_cmd_config(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        try:
            await self.ctx.send.text(text=self._mai_config_summary(multiline=True), stream_id=stream_id)
            return True, "配置已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_config 发送失败: {e}")
            return False, f"发送失败: {e}", True

    @Command(
        name="/cateye_test_mai_llm_log",
        pattern=r"(?<!\S)/cateye_test_mai_llm_log(?:\s+(?P<mai_days>\S+))?\s*$",
        description="查看插件发起的 LLM 调用日志（合并转发）。用法: /cateye_test_mai_llm_log [天数]",
    )
    async def mai_cmd_llm_log(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        companion = self._mai_companion()

        async def _reply(text: str) -> None:
            try:
                await self.ctx.send.text(text=text, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.warning(f"mai_cmd_llm_log 回复发送失败: {e}")

        llm_logger = getattr(companion, "llm_logger", None) if companion else None
        if llm_logger is None or not llm_logger.enabled:
            await _reply("LLM 调用日志未启用（配置 [admin] llm_log_enabled = true 后开始记录）。")
            return True, "日志未启用", True

        days = self._mai_parse_days(kwargs.get("matched_groups"))
        entries = llm_logger.read_entries(days)
        if not entries:
            await _reply(f"最近 {days or llm_logger.retention_days} 天没有 LLM 调用日志。")
            return True, "暂无日志", True

        records = self._mai_build_llm_log_records(entries, llm_logger.retention_days)
        return await self._mai_send_forward(records, stream_id, "LLM 日志")

    @Command(
        name="/cateye_test_mai_diag",
        pattern=r"(?<!\S)/cateye_test_mai_diag(?:\s+(?P<mai_days>\S+))?\s*$",
        description="查看主动行为决策日志（每轮巡检一条：为什么发言/为什么没发言）。用法: /cateye_test_mai_diag [天数]",
    )
    async def mai_cmd_diag(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        companion = self._mai_companion()

        async def _reply(text: str) -> None:
            try:
                await self.ctx.send.text(text=text, stream_id=stream_id)
            except Exception as e:
                self.ctx.logger.warning(f"mai_cmd_diag 回复发送失败: {e}")

        decision_logger = getattr(companion, "decision_logger", None) if companion else None
        if decision_logger is None or not decision_logger.enabled:
            await _reply("主动行为日志未启用（配置 [admin] decision_log_enabled = true 后开始记录）。")
            return True, "日志未启用", True

        days = self._mai_parse_days(kwargs.get("matched_groups"))
        entries = decision_logger.read_entries(days)
        if not entries:
            msg = (
                f"最近 {days or decision_logger.retention_days} 天没有主动行为决策记录。"
                "（插件刚加载或日志刚开启时属正常）"
            )
            status_line = self._mai_patrol_status_line()
            if status_line:
                msg += f"\n{status_line}\n（若「巡检=未运行」，说明 stream_id 没解析出来，主动发言整条链路都没跑）"
            await _reply(msg)
            return True, "暂无日志", True

        records = self._mai_build_diag_records(entries, decision_logger.retention_days)
        return await self._mai_send_forward(records, stream_id, "决策日志")

    @Command(name="/cateye_test_mai_test", pattern=r"(?<!\S)/cateye_test_mai_test\s*$",
             description="发送一条测试消息以验证发送通道")
    async def mai_cmd_test(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "") or self._mai_target_stream_id()
        if not stream_id:
            return False, "暂无 stream_id", True
        try:
            result = await self.ctx.send.text(
                text=f"{self._mai_lover_name()}测试消息~ 发送通道正常 ✅", stream_id=stream_id
            )
        except Exception as e:
            return False, f"发送异常: {e}", True
        if result:
            return True, "测试消息发送成功", True
        return False, "发送返回 False", True

    # ---- 命令辅助 ----

    def _mai_affection_state_line(self) -> str:
        """好感 v2 状态行：score / baseline / 档位（5 档口径）/ 今日 Δ + M2 最近印象与 tone。

        最近印象（impressions）与关系温度（tone）由 M2 关系日结裁判产出；为空时只输出 M1 状态行。
        """
        companion = self._mai_companion()
        if companion is None:
            return "好感度管理器未初始化。"
        affection = companion.affection
        relation = getattr(companion, "relation", None)
        tier = int(affection.tier())
        label = relation.tier_label(tier) if relation is not None else affection.tier_label()
        daily = affection.daily_delta() or {}
        try:
            net = float(daily.get("positive") or 0.0) + float(daily.get("negative") or 0.0)
        except (TypeError, ValueError):
            net = 0.0
        line = (f"score {affection.score():.1f} / baseline {affection.baseline():.1f} / "
                f"档位 {tier}·{label} / 今日 Δ {net:+.2f}")
        memory_line = self._mai_affection_memory_line(affection)
        if memory_line:
            line += "\n" + memory_line
        return line

    @staticmethod
    def _mai_parse_days(matched_groups: Any) -> Optional[int]:
        """解析命令的 [天数] 命名捕获；非法/缺省返回 None（= 整个保留期）。"""
        if not isinstance(matched_groups, dict):
            return None
        raw = str(matched_groups.get("mai_days", "") or "").strip()
        if not raw:
            return None
        try:
            return max(1, min(30, int(raw)))
        except ValueError:
            return None

    async def _mai_send_forward(self, records: list[dict[str, Any]], stream_id: str,
                                label: str) -> tuple[bool, str, bool]:
        """合并转发日志节点；适配器不支持转发时回退纯文本。"""
        try:
            await self.ctx.send.forward(records, stream_id)
            return True, f"{label}已合并转发", True
        except Exception as forward_exc:
            self.ctx.logger.warning(f"{label}合并转发失败，回退纯文本: {forward_exc}")
            fallback = "\n\n".join(str(r["segments"][0]["content"]) for r in records)
            try:
                await self.ctx.send.text(text=fallback, stream_id=stream_id)
                return True, f"{label}已发送（文本回退）", True
            except Exception as e:
                self.ctx.logger.error(f"{label}回退发送失败: {e}")
                return False, f"发送失败: {e}", True

    # ==================================================================
    # 报告构造
    # ==================================================================

    def _mai_status_report(self) -> str:
        """恋人状态报告（好感度/今日发言/早晚安想念标记/日程摘要）。"""
        companion = self._mai_companion()
        if companion is None:
            return "恋人模块尚未初始化完成。"
        affection = companion.affection
        name = self._mai_lover_name()
        today = datetime.now().strftime("%Y-%m-%d")
        daily_max = int(self.config.patrol.daily_max_speak)
        lines = [
            f"❤️ 麦麦恋人状态 ({today})",
            f"好感度档位: {affection.level()} - {affection.level_label()}",
            f"今日主动发言: {affection.today_speak_count():g}/{daily_max}",
            f"早安: {'✅已触发' if affection.morning_sent() else '❌未触发'}  |  "
            f"晚安: {'✅已触发' if affection.night_sent() else '❌未触发'}  |  "
            f"想念: {'✅已触发' if affection.miss_sent_today() else '❌未触发'}",
        ]
        schedule_query = self._mai_schedule_query()
        if schedule_query is not None:
            try:
                items = schedule_query.get_day_schedule(today)
            except Exception:
                items = []
            if items:
                lines.append(f"今日日程 ({len(items)} 个节点):")
                for item in items[:5]:
                    start = int(item.get("start_minute") or 0)
                    end = int(item.get("end_minute") or 0)
                    lines.append(f"  [{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}] "
                                 f"🐱 {item.get('name', '未知')}")
                if len(items) > 5:
                    lines.append(f"  ... 还有 {len(items) - 5} 个节点")
            else:
                lines.append("今日暂无日程。")
        return "\n".join(lines)

    def _mai_schedule_report(self) -> str:
        """今日完整日程报告。"""
        today = datetime.now().strftime("%Y-%m-%d")
        schedule_query = self._mai_schedule_query()
        if schedule_query is None:
            return "日程服务未就绪（planning 子模块不可用）。"
        try:
            items = schedule_query.get_day_schedule(today)
        except Exception:
            return "日程服务暂时不可用。"
        if not items:
            return f"📅 今日 ({today}) 暂无日程。\n可经 /cateye_test_plan regenerate 生成。"
        lines = [f"📅 {self._mai_lover_name()}今日日程 ({today})", ""]
        for item in items:
            start = int(item.get("start_minute") or 0)
            end = int(item.get("end_minute") or 0)
            lines.append(f"  [{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}] "
                         f"🐱 {item.get('name', '未知')}")
        return "\n".join(lines)

    def _mai_patrol_status_line(self) -> str:
        """一行巡检运行状态（/mai_diag 汇总与自诊断用）。"""
        companion = self._mai_companion()
        if companion is None:
            return ""
        parts: list[str] = []
        try:
            status = companion.patrol.patrol_status()
            parts.append(f"巡检={'运行中' if status.get('running') else '未运行'}")
            if status.get("last_tick_at"):
                parts.append(f"上次巡检={status['last_tick_at']}")
            parts.append(f"间隔={status.get('interval_minutes')}分钟")
            parts.append(f"stream_id={'已就绪' if status.get('stream_id_ready') else '未解析'}")
            parts.append(f"主动开关={'开' if status.get('trigger_enabled') else '关'}")
        except Exception:
            pass
        schedule_query = self._mai_schedule_query()
        if schedule_query is not None:
            try:
                nodes = len(schedule_query.get_day_schedule(datetime.now().strftime("%Y-%m-%d")))
                parts.append(f"日程来源=planning（今日节点 {nodes} 个）")
            except Exception:
                pass
        return ("状态: " + "｜".join(parts)) if parts else ""

    def _mai_config_summary(self, multiline: bool) -> str:
        """配置摘要（Tool 用单行紧凑版，命令用多行版）。"""
        patrol = self.config.patrol
        affection = self.config.affection
        cateye_cfg = getattr(self.config, "cateye", None)
        miss_llm = "开" if getattr(patrol, "miss_llm_check_enabled", False) else "关"
        screen = "开" if getattr(cateye_cfg, "enabled", False) else "关"
        if multiline:
            return (
                "⚙️ 麦麦恋人配置摘要\n"
                f"调度: 巡检间隔 {patrol.check_interval_minutes}min | "
                f"每日上限 {patrol.daily_max_speak} 条 | "
                f"最小间隔 {patrol.min_trigger_interval_minutes}min"
                f"（早晚安{'豁免' if getattr(patrol, 'min_interval_exempt_greetings', False) else '不豁免'}"
                f" / 想念{'豁免' if getattr(patrol, 'min_interval_exempt_miss', False) else '不豁免'}） | "
                f"冷却 {patrol.user_cooldown_minutes}min | "
                f"触发开关 {'开启' if patrol.proactive_trigger_enabled else '关闭'}\n"
                f"时间窗口: 早安 {patrol.morning_start}~{patrol.morning_end} | "
                f"晚安 {patrol.night_start}~{patrol.night_end} | "
                f"静默 {patrol.silence_start}~{patrol.silence_end} | "
                f"想念 {patrol.miss_hours_min}~{patrol.miss_hours_max}h（LLM把关{miss_llm}）\n"
                f"概率: 日常 {patrol.default_speak_rate} | "
                f"想念 {patrol.miss_speak_rate} | "
                f"日程节点 {patrol.activity_trigger_rate}\n"
                f"好感度: {affection.current_level} | "
                f"恋人上下文注入: 仅目标私聊 | "
                f"屏幕感知: {screen} | "
                f"日志: {self._mai_patrol_status_line() or '（巡检未初始化）'}"
            )
        return (
            f"⚙️ 麦麦恋人配置: 巡检间隔 {patrol.check_interval_minutes}min | "
            f"每日上限 {patrol.daily_max_speak} 条 | "
            f"最小间隔 {patrol.min_trigger_interval_minutes}min | "
            f"冷却 {patrol.user_cooldown_minutes}min | "
            f"触发开关 {'开' if patrol.proactive_trigger_enabled else '关'} | "
            f"早安 {patrol.morning_start}~{patrol.morning_end} | "
            f"晚安 {patrol.night_start}~{patrol.night_end} | "
            f"想念 {patrol.miss_hours_min}~{patrol.miss_hours_max}h（LLM把关{miss_llm}） | "
            f"日常概率 {patrol.default_speak_rate} | "
            f"想念概率 {patrol.miss_speak_rate} | "
            f"节点概率 {patrol.activity_trigger_rate} | "
            f"好感度 {affection.current_level} | "
            f"屏幕感知 {screen}"
        )

    @staticmethod
    def _mai_build_llm_log_records(entries: list[dict[str, Any]], retention_days: int) -> list[dict[str, Any]]:
        """把 LLM 调用日志构造成合并转发节点（首条汇总，其后每次请求一条）。"""
        max_records = 50
        total = len(entries)
        shown = entries[-max_records:]
        first_time = str(entries[0].get("time", "?"))
        last_time = str(entries[-1].get("time", "?"))
        header = (
            "📋 麦麦恋人 LLM 调用日志\n"
            f"时间范围: {first_time} ~ {last_time}\n"
            f"共 {total} 条（展示最近 {len(shown)} 条） | 保留 {retention_days} 天"
        )
        records: list[dict[str, Any]] = [
            {"user_id": "0", "nickname": "LLM调用日志",
             "segments": [{"type": "text", "content": header}]}
        ]
        for entry in shown:
            status = "✅" if entry.get("success") else "❌"
            body = str(entry.get("response") or "")
            error = str(entry.get("error") or "")
            if error:
                body = f"{body}（错误: {error}）" if body else f"（错误: {error}）"
            line = (
                f"[{entry.get('time', '?')}] "
                f"{entry.get('event', '?')} · "
                f"{entry.get('task') or '默认任务'} · {status}\n"
                f"{body or '（空回复）'}"
            )
            records.append({"user_id": "0", "nickname": "LLM调用日志",
                            "segments": [{"type": "text", "content": line}]})
        return records

    @staticmethod
    def _mai_build_diag_records(entries: list[dict[str, Any]], retention_days: int) -> list[dict[str, Any]]:
        """把决策日志构造成合并转发节点（首条汇总 + 逐轮记录，最多 30 条）。"""
        max_records = 30
        total = len(entries)
        shown = entries[-max_records:]
        triggered = sum(1 for e in entries if str(e.get("action")) == "trigger")
        spoken = sum(1 for e in entries if str(e.get("action")) == "spoken")
        info = sum(1 for e in entries if str(e.get("action")) == "info")
        skipped = sum(1 for e in entries if str(e.get("action")) == "skip")

        reason_counts: dict[str, int] = {}
        for entry in entries:
            if str(entry.get("action")) != "skip":
                continue
            key = str(entry.get("reason") or "unknown")
            reason_counts[key] = reason_counts.get(key, 0) + 1
        top_reasons = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)[:6]

        header = (
            "🔍 麦麦恋人 主动行为决策日志\n"
            f"时间范围: {entries[0].get('time', '?')} ~ {entries[-1].get('time', '?')}\n"
            f"共 {total} 条：发起触发 {triggered} / 确认发言 {spoken} / 跳过 {skipped}"
            + (f" / 信息 {info}" if info else "")
            + f"（展示最近 {len(shown)} 条，保留 {retention_days} 天）\n"
            f"跳过原因: {', '.join(f'{k}×{v}' for k, v in top_reasons) if top_reasons else '无'}"
        )
        records: list[dict[str, Any]] = [
            {"user_id": "0", "nickname": "主动行为日志",
             "segments": [{"type": "text", "content": header}]}
        ]

        for entry in shown:
            action = str(entry.get("action") or "")
            trigger_type = str(entry.get("trigger_type") or "")
            reason = str(entry.get("reason") or "unknown")
            if action == "trigger":
                line = f"[{entry.get('time', '?')}] ✅ 发起触发 · {trigger_type or '?'}\n{entry.get('detail') or ''}"
            elif action == "spoken":
                line = f"[{entry.get('time', '?')}] 🗣️ 确认发言 · {trigger_type or '?'}\n{entry.get('detail') or ''}"
            elif action == "info":
                line = f"[{entry.get('time', '?')}] ℹ️ {reason}\n{entry.get('detail') or ''}"
            else:
                label = _DIAG_REASON_LABELS.get(reason, reason)
                line = f"[{entry.get('time', '?')}] ⏭️ 跳过 · {reason}（{label}）\n{entry.get('detail') or ''}"
            extras: list[str] = []
            minutes_since = entry.get("minutes_since_last_speak")
            if minutes_since is not None:
                extras.append(f"距上次发言 {minutes_since} 分钟")
            if entry.get("budget_used") is not None:
                extras.append(f"预算 {entry.get('budget_used')}/{entry.get('budget_limit', '?')}")
            if entry.get("min_interval_minutes"):
                extras.append(f"最小间隔 {entry.get('min_interval_minutes')} 分钟")
            if extras:
                line += "\n（" + "，".join(extras) + "）"
            records.append({"user_id": "0", "nickname": "主动行为日志",
                            "segments": [{"type": "text", "content": line}]})
        return records

    # ==================================================================
    # APIs（跨插件；与 planning 子模块的 get_* 撞名，故加 companion_ 前缀）
    # ==================================================================

    @API("companion_get_current_activity",
         description="获取恋人当前正在做什么（活动描述字符串，无活动返回空串）",
         version="1", public=True)
    async def mai_api_get_current_activity(self, **kwargs: Any) -> str:
        schedule_query = self._mai_schedule_query()
        if schedule_query is None:
            return ""
        now = datetime.now()
        try:
            current = schedule_query.find_current_activity(now.strftime("%Y-%m-%d"), now)
        except Exception:
            return ""
        if current is None:
            return ""
        return str(current.get("description") or current.get("name") or "")

    @API("companion_get_schedule",
         description="获取今日完整日程。返回节点列表 [{time, activity}]（兼容虚拟恋人插件返回结构）",
         version="1", public=True)
    async def mai_api_get_schedule(self, **kwargs: Any) -> list[dict[str, Any]]:
        schedule_query = self._mai_schedule_query()
        if schedule_query is None:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            items = schedule_query.get_day_schedule(today)
        except Exception:
            return []
        return [{"time": f"{int(i.get('start_minute') or 0) // 60:02d}:{int(i.get('start_minute') or 0) % 60:02d}",
                 "activity": str(i.get("name") or "")}
                for i in items]

    @API("companion_get_affection_level",
         description="获取当前好感度档位。返回 0/1/2（兼容虚拟恋人插件返回结构）",
         version="1", public=True)
    async def mai_api_get_affection_level(self, **kwargs: Any) -> int:
        companion = self._mai_companion()
        if companion is None:
            return 0
        return companion.affection.level()
