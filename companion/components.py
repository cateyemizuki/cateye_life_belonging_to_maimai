"""companion 组件层（来源：虚拟陪伴插件 plugin.py 的组件面）。

``CompanionComponentsMixin`` 供 plugin.py 的主类多继承：
``class CateyeSuitePlugin(CompanionComponentsMixin, MaiBotPlugin)``。
mixin 方法开头一律判空 ``getattr(self, "_companion", None)``（服务束未挂上时降级）。

组件清单（组件名跟源插件）：
- Hooks ×5：
  - chat.receive.before_process        发送者身份缓存（BLOCKING/EARLY，只记录不拦截）
  - chat.receive.after_process         目标用户私聊时间（OBSERVE/LATE）
  - maisaka.planner.before_request     过期主动任务块剥离 + 屏幕旁白 + 好感度 +
                                       主动回合软约束（BLOCKING/NORMAL；
                                       items 强 schema 协议 + messages 双投影 +
                                       modified_kwargs 全量回传 + >8MB 载荷收缩）
  - maisaka.replyer.before_model_request  过期主动任务块剥离 + 仅屏幕旁白注入
                                       （BLOCKING/NORMAL）
  - maisaka.replyer.after_response     90 秒窗口补计 0.5 发言数 + spoken 回执（OBSERVE/LATE）
- Tools ×3：lbm_mai_lover_send_message / lbm_mai_lover_affection /
  lbm_report_harassment（2026-10-24 告状：受骚扰时转达维护者；纯逻辑见
  companion/harass_report.py，``[admin].harass_report_enabled=false`` 时不注册——
  过滤在 plugin.get_components override）
  （原 current_activity / status / schedule 三工具已随 T6 注册面收口移除：
  当前活动与日程查询见 lbm_get_schedule_info，状态总览见 /lbm_dashboard）
- Commands ×4：/lbm_mai_affection（鉴权：target_qq 白名单 + 本机控制台，
  占位默认值一律拒绝）｜/lbm_mai_help｜/lbm_diag（A5 排障四合一：
  llm|decision|life|streams 子命令，旧名 /lbm_mai_llm_log、/lbm_mai_diag 与
  /lbm_life diag 保留为别名；鉴权 = admin 判定）｜/lbm_dashboard
  （M7 聚合总览：四模块状态图 + 纯文本降级，文本档展开完整日程并附配置摘要；
  鉴权同 admin 判定；渲染失败/超时静默降级为纯文本）
  （原 /mai_status、/mai_schedule、/mai_config、/mai_test 四条命令已移除：状态/日程
  信息面并入 dashboard 文本档，配置摘要同源，测试消息可由 /mai_lover_send_message 替）
- APIs ×3：companion_get_current_activity / companion_get_schedule /
  companion_get_affection_level（源插件 get_* 与 planning 子模块撞名，故加前缀）

「今日日程」一律经 planning.schedule_query.ScheduleQuery 取
（服务束解析 ``plugin._planning``，缺省回退 ``plugin._schedule_query``）。
"""

from __future__ import annotations

import asyncio
import json as _json
import time
from collections import deque as _deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import cateye_dashboard
from maibot_sdk import API, Command, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from companion.config import target_qq_of
from companion.harass_report import (
    HARASS_TOOL_NAME,
    TRIGGER_TYPE,
    build_report_text,
    cooldown_check,
    cooldown_hint,
    decision_fields,
    monotonic_now,
    resolve_maintainer_qq,
)
from companion.patrol import resolve_silence_window
from core.diag import DecisionLogger
from core.inject import pick_payload_key, project_injection, without_stale_proactive_blocks
# §31.3 B3 修复（R7 = 用户 2026-10-23 第二轮直派，A-22 bug③）：读取渲染面时间一律
# 经 time_utils.format_day_minute（禁手写 //60/%60；day_offset=1 行带「次日」标注），
# 日帧行为过滤复用 schedule_query 的 _SKIP_TYPES 同语义（{"sleep"}）。
from planning.schedule_query import ScheduleQuery
from planning.schedule_query import _SKIP_TYPES as _SCHEDULE_SKIP_TYPES
from planning.time_utils import DAY_MINUTES, format_day_minute
from suite_prompts import (
    INJECT_AFFECTION_BLOCK_DEFAULT,
    TOOL_SEND_MESSAGE_DEFAULT,
    TOOL_SEND_MESSAGE_SYSTEM_DEFAULT,
    prompt,
    render,
)

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

#: 主动行为决策日志里的跳过原因 → 中文说明（/lbm_diag decision 展示用）。
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
    "activity_disabled": "日程巡检开关已关闭",
    "greeting_list_parse": "名单存在非法元素（已忽略）",
    "proactive_list_parse": "名单存在非法元素（已忽略）",
    "llm_rejected": "想念把关被 LLM 驳回",
    "planner_trigger_failed": "触发未入队（stream_id 缺失或开关关闭）",
    "disabled": "主动触发开关已关闭",
    "daily_max_zero": "每日发言上限为 0",
    "reply_confirmed": "planner 已生成并发送回复",
    "tool_send_message": "planner 通过 Tool 主动发消息",
}

#: /lbm_diag 用法帮助（无参数或未知子命令时原样回复；五行，含旧名别名对应关系）。
_DIAG_USAGE_TEXT = (
    "用法: /lbm_diag <llm|decision|life|streams> [天数]（管理员）\n"
    "  llm [天数]      — LLM 调用日志，合并转发（1~30 天；旧名 /lbm_mai_llm_log）\n"
    "  decision [天数] — 主动行为决策日志，合并转发（1~30 天；旧名 /lbm_mai_diag）\n"
    "  life [天数]     — 决策日志紧凑文本（1~7 天；旧名 /lbm_life diag）\n"
    "  streams [序号]  — 主动候选流与名单状态；带序号只看该流早/晚安触发状态"
    "（今日最后一条非命令消息时间）"
)

#: 注入作用域（2026-10-10 注入矩阵：目标私聊 / 含陪伴用户的群聊 / 跳过）
_SCOPE_PRIVATE = "private"
_SCOPE_GROUP = "group"
_SCOPE_SKIP = "skip"

#: 群聊「上下文是否包含陪伴用户」的回看条数（按 msg_id 反查发送者 QQ 的窗口）
_GROUP_TARGET_WINDOW = 20

#: 决策日志动作值（core.diag.DecisionLogger 的类属性别名，源插件为模块常量 ACTION_SPOKEN）
_ACTION_SPOKEN = DecisionLogger.ACTION_SPOKEN

#: 目标 QQ 的占位默认值（一律拒绝鉴权 / 不做身份判定）
_PLACEHOLDER_QQ = "123456789"


def _vad_float(value: Any) -> float:
    """三维数值字段的安全读数（缺键/坏类型 → 0.0；随图日志变动计算用）。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


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
    """虚拟陪伴组件面（Hooks/Tools/Commands/APIs），挂在整合插件主类上。"""

    # ==================================================================
    # 内部取用（全部判空降级）
    # ==================================================================

    def _mai_companion(self) -> Any:
        return getattr(self, "_companion", None)

    def _mai_patrol_enabled(self) -> bool:
        """陪伴模块总开关（``[patrol].enabled``）。

        T3 问题表 #4（用户裁决 ②）：此前 enabled=false 只停巡检，文档承诺的
        「不注入好感度 / 命令与工具返回未启用提示」不成立——现按文档逐入口补齐。
        """
        section = getattr(getattr(self, "config", None), "patrol", None)
        return bool(getattr(section, "enabled", True))

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
        """目标 QQ（占位默认值/空值返回空串，用于身份判定）。

        读取位置：``[patrol].target_qq``（2026-10-10 由 [target] 段迁入；
        兼容 helper 仍会在旧段存在时兜底，热重载窗口不丢目标）。
        """
        try:
            raw = target_qq_of(self.config)
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
        description="记录入站消息的「消息 ID → 发送者 QQ 号」与「会话 ID → 是否群聊」，供 planner 注入按 QQ 号判定陪伴对象",
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
    async def mai_hook_planner_inject(self, **kwargs: Any) -> dict[str, Any]:
        """注入陪伴上下文（屏幕旁白 + 好感度 + 主动回合软约束；2026-10-10 注入矩阵）。

        日期行与当前活动**不再由本 Hook 注入**：日期/节假日行归日程模块
        （planning.inject_service._date_header），当前活动归日程块本身。
        本 Hook 只负责：①屏幕旁白（仅目标私聊）；②好感度块（目标私聊恒定 /
        含陪伴用户的群聊受 `[patrol] inject_affection_in_group` 控制）；③主动发言回合的回复目标约束（仅目标私聊）。

        屏幕旁白与好感度注入相互独立，所以先算旁白——保证任何一条早退路径都不会
        把旁白丢掉。items 是强 schema 载荷：新条目 meta 必须完整，modified_kwargs
        必须全量回传（core.inject 负责构造合法快照）。

        session_id 写回（2026-10-10 N6 注入链修复）：宿主对 blocking 处理器返回的
        ``modified_kwargs`` 做整份替换，被 ``**kwargs`` 之外具名参数接住的键不会
        再出现在回传里。故本 Hook 不声明 ``session_id`` 具名参数，改为从 kwargs
        读取，并在**每条带 modified_kwargs 的返回分支**原样写回——否则链上后续
        处理器（schedule_inject_v4）只会拿到空 session_id，好感度块恒 skip、
        日程注入按 ``user=unknown`` 单桶冷却。
        """
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            # 失效路径不再静默：上游没写回/宿主没传时留一条线索
            try:
                self.ctx.logger.warning(
                    "planner 注入未拿到 session_id（上游 Hook 未写回或宿主未传）："
                    "好感度块作用域将退化为 skip，日程注入缓存键退化为 unknown"
                )
            except Exception:
                pass

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
            return {"action": "continue", "modified_kwargs": {**kwargs, "session_id": session_id}}

        scope = self._mai_resolve_injection_scope(session_id, payload)

        # 3) 超限不再整体跳过：先把最大的图片 part 换成文本占位符再注入
        shrunk, omitted = _shrink_payload_for_injection(payload)
        if shrunk is None:
            try:
                self.ctx.logger.warning(
                    f"planner payload 过大且裁剪后仍超限 (>{_MAX_INJECT_PAYLOAD_BYTES} bytes)，跳过活动注入"
                )
            except Exception:
                pass
            return {"action": "continue", "modified_kwargs": {**kwargs, "session_id": session_id}}
        if omitted:
            try:
                self.ctx.logger.info(f"planner payload 过大，已省略 {omitted} 张图片以完成活动注入")
            except Exception:
                pass

        suffix = self._mai_build_affection_suffix(scope)
        if scope == _SCOPE_PRIVATE:
            suffix += self._mai_build_proactive_target_rule()
        if not suffix.strip():
            # 没有可注入文本：不回传收缩后的载荷（避免白丢图片）
            return {"action": "continue", "modified_kwargs": {**kwargs, "session_id": session_id}}

        modified_payload = project_injection(payload_key, shrunk, suffix)
        return {
            "action": "continue",
            "modified_kwargs": {**kwargs, "session_id": session_id, payload_key: modified_payload},
        }

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
            return None  # 屏幕内容绝不漏到非陪伴对象的聊天流
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
        """判断本次请求该注入哪一档陪伴上下文：``private`` / ``group`` / ``skip``。

        2026-10-10 注入矩阵（用户口径）：

        1. **目标陪伴私聊** → ``private``：**始终**注入好感度（陪伴模块启用时）；
        2. **群聊且群内最近消息中有陪伴用户本人发言**（最近若干条消息的发送者含目标 QQ，
           按 QQ 号判定、不识别昵称/代称提及）→ ``group``：注入好感度
           （受 ``[patrol].inject_affection_in_group`` 开关控制，默认开）；
        3. **非陪伴用户的私聊** → ``skip``：**始终不**注入好感度；
        4. 其余（看不到陪伴用户的群聊等）→ ``skip``。

        判定只看 QQ 号（昵称可被伪造）；反查不到发送者按 skip（宁可漏注入，绝不按名字兜底）。
        """
        companion = self._mai_companion()
        if companion is None:
            return _SCOPE_SKIP
        sid = str(session_id or "").strip()
        target_stream = self._mai_target_stream_id()
        target_qq = self._mai_target_qq()
        if target_stream and sid and sid == target_stream:
            return _SCOPE_PRIVATE

        is_group = companion.session_kinds.is_group(sid)
        # 私聊兜底：stream_id 变了（宿主重建会话）但对方确实是目标用户
        if (
            is_group is False
            and target_qq
            and sender_ids_in_window(payload, companion.senders, 1)[-1:] == [target_qq]
        ):
            return _SCOPE_PRIVATE
        # 群聊：上下文里出现过陪伴用户（最近若干条消息的发送者含目标 QQ）
        if (
            is_group is True
            and target_qq
            and target_qq in sender_ids_in_window(payload, companion.senders, _GROUP_TARGET_WINDOW)
        ):
            return _SCOPE_GROUP
        return _SCOPE_SKIP

    def _mai_inject_affection_in_group(self) -> bool:
        """群聊好感度注入开关（``[patrol].inject_affection_in_group``，取不到按开）。"""
        section = getattr(getattr(self, "config", None), "patrol", None)
        return bool(getattr(section, "inject_affection_in_group", True))

    def _mai_build_affection_suffix(self, scope: str) -> str:
        """构造好感度注入块（不含日期行——日期/节假日已归日程模块）。

        - ``private``（目标私聊）：好感度块 + 关系记忆行（最近印象/关系温度/本周大事记）；
        - ``group``（群内含陪伴用户）：只注入好感度档位块——**不带**关系记忆行，
          避免把私聊里的相处细节带进群聊（隐私护栏）；
        - 其余 scope 返回空串。
        """
        if scope == _SCOPE_SKIP:
            return ""
        if not self._mai_patrol_enabled():
            # [patrol].enabled=false：不注入好感度（T3 #4；与字段 description/hint 的承诺对齐）
            return ""
        if scope == _SCOPE_GROUP and not self._mai_inject_affection_in_group():
            return ""
        companion = self._mai_companion()
        if companion is None:
            return ""

        name = self._mai_lover_name()
        target_qq = self._mai_target_qq()
        relation = getattr(companion, "relation", None)
        relation_word = getattr(relation, "word", None) or "恋人"
        target_label = f"QQ {target_qq}" if target_qq else relation_word
        affection = companion.affection
        # M1 注入接线：好感后缀改读 v2 5 档档位描述（经 relation profile；
        # 关系档案缺失时回退 affection 的「恋人」5 档基底文案）。habits 通道不在此处。
        tier = affection.tier()
        if relation is not None:
            tier_label = relation.tier_label(tier)
            tier_desc = relation.tier_description(tier)
        else:
            tier_label = affection.tier_label()
            tier_desc = affection.tier_description()

        block = render(
            prompt(self.config, "inject_affection_block"),
            fallback=INJECT_AFFECTION_BLOCK_DEFAULT,
            name=name, relation_word=relation_word, target_label=target_label,
            tier=tier, tier_label=tier_label, tier_desc=tier_desc,
        )
        if scope == _SCOPE_GROUP:
            return block
        # M2 注入叠加：最近 1~2 条 impressions + 最近一次日结 tone；
        # 无印象/无 tone 时返回空串，注入内容与 M1 完全一致（降级为现状）。
        memory_line = self._mai_affection_memory_line(affection)
        if memory_line:
            block += "\n" + memory_line
        return block

    @staticmethod
    def _mai_affection_memory_line(affection: Any) -> str:
        """好感记忆行（注入与命令共用）：最近 1~2 条印象 + 最近一次日结关系温度 + M6 周结大事记。

        状态层缺失/为空/异常一律返回空串（绝不空指针，也绝不给调用方抛错）；
        周结大事记默认关、只取最近 1 条，未启用时输出与旧版逐字一致。
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
        # M6：周结大事记（默认关，未启用时恒为空表）——只取最近 1 条；无则不加，输出与旧版一致
        try:
            milestones = affection.milestones()[-1:]
        except Exception:
            milestones = []
        for item in milestones or []:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            stamp = str(item.get("date") or "").strip()
            lines.append(f"【本周大事记】{stamp} {text}" if stamp else f"【本周大事记】{text}")
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
        return prompt(self.config, "inject_proactive_turn_rule")

    # ==================================================================
    # Tools
    # ==================================================================

    @Tool(
        "lbm_mai_lover_send_message",
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
        """LLM Tool: 主动向陪伴对象发送消息（带情绪后缀；成功计 1 次发言数并记决策日志）。"""
        if not self._mai_patrol_enabled():
            return "陪伴模块未启用（[patrol].enabled=false）。"   # T3 #4：工具按文档返回未启用提示
        companion = self._mai_companion()
        if companion is None:
            return "陪伴服务未初始化。"
        stream_id = self._mai_target_stream_id()
        name = self._mai_lover_name()
        if not stream_id:
            return f"{name}还没有连接到目标用户，请稍后再试。"

        if not str(message or "").strip():
            relation = getattr(companion, "relation", None)
            relation_word = getattr(relation, "word", None) or "恋人"
            style_word = getattr(relation, "style_word", None) or "温柔亲昵的陪伴者"
            self_desc = getattr(relation, "self_desc", None) or "一个温柔体贴的虚拟陪伴者"
            fallback = ("想你了呢~在忙什么呀？" if relation_word == "恋人"
                        else "在忙什么呀？最近怎么样？~")
            message = await companion.llm.generate_or_fallback(
                prompt=render(
                    prompt(self.config, "tool_send_message"),
                    fallback=TOOL_SEND_MESSAGE_DEFAULT,
                    style_word=style_word, relation_word=relation_word,
                ),
                fallback=fallback,
                system_prompt=render(
                    prompt(self.config, "tool_send_message_system"),
                    fallback=TOOL_SEND_MESSAGE_SYSTEM_DEFAULT,
                    name=name, self_desc=self_desc, relation_word=relation_word,
                ),
                temperature=0.8,
                event="tool_send_message",
            )

        final_text = companion.message.append_affection_suffix(str(message))
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.error(f"[lbm_mai_lover_send_message] 发送异常: {e}")
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
        "lbm_mai_lover_affection",
        description="调整与关系对象的好感度：把 score 移动到指定档位的区间中值并重估档位"
                    "（档位数 1~5 随好感度预设表变化，档位名称如恋人=生疏/熟悉/亲密/热恋/挚爱）",
        parameters={
            "tier": {
                "type": "integer",
                "description": "好感度档位（1 起计数，上限随当前预设 1~5 档）",
                "required": True,
            },
        },
    )
    async def mai_tool_affection(self, tier: int = 1, **kwargs: Any) -> str:
        """LLM Tool：把 score 移到指定档（1~N，1 起计数）的区间中值（管理员兜底，跳过迟滞）。"""
        companion = self._mai_companion()
        if companion is None:
            return "好感度管理器未初始化。"
        preset = companion.affection.preset()
        tier_count = preset.size
        labels = "、".join(f"{i + 1}={label}" for i, label in enumerate(preset.labels()))
        try:
            requested = int(tier)
        except (TypeError, ValueError):
            return f"档位必须是 1~{tier_count} 的整数，收到的是 {tier!r}。"
        if not 1 <= requested <= tier_count:
            return f"档位只能填 1~{tier_count}（当前预设档位：{labels}），收到的是 {requested}。"
        companion.affection.set_tier(requested - 1)
        self.ctx.logger.info(f"好感度已通过 Tool 调整到第 {requested} 档（score 移至对应区间中值）")
        return f"好感度已更新：{self._mai_affection_state_line()}"

    # ==================================================================
    # 告状工具（2026-10-24 用户直派，需求3）
    # ==================================================================

    def _harass_state(self) -> dict:
        """进程内缓存：告状冷却时间戳 + 维护者解析结果（键 = 解析所用 QQ；热重载重建实例即清零）。"""
        state = getattr(self, "_harass_cache", None)
        if state is None:
            state = {"last_ts": 0.0, "resolved_qq": "", "resolved_stream": ""}
            self._harass_cache = state
        return state

    def _harass_log(self, *, action: str, reason: str, detail: str = "",
                    maintainer_qq: str = "", location: str = "") -> None:
        """告状决策日志留痕（trigger_type 统一 ``admin.harass_report``；日志故障不影响流程）。

        三形态（reason 取值）：成功 ``reported`` / 失败 ``send_failed`` 等 /
        冷却跳过 ``cooldown_skip``；``result`` 字段冗余同值便于按天日志 grep。
        """
        fields = decision_fields(result=reason, maintainer_qq=maintainer_qq,
                                 location=location, detail=detail)
        companion = self._mai_companion()
        if companion is not None and getattr(companion, "decision_logger", None) is not None:
            # 注意：fields 已含 detail（decision_fields 构造），不可再显式传 detail——
            # _mai_log_proactive_event 的 detail 是具名参数，重复传会 TypeError。
            self._mai_log_proactive_event(action=action, reason=reason,
                                          trigger_type=TRIGGER_TYPE, **fields)
            return
        logger = getattr(self, "_decision_logger", None)
        if logger is None:
            return
        try:
            logger.record(action=action, reason=reason,
                          trigger_type=TRIGGER_TYPE, **fields)
        except Exception:
            pass

    def _harass_maintainer_qq(self) -> str:
        """维护者 QQ 解析（优先级纯逻辑在 harass_report.resolve_maintainer_qq）。"""
        admin = getattr(getattr(self, "config", None), "admin", None)
        return resolve_maintainer_qq(
            getattr(admin, "harass_report_target_qq", ""),
            list(getattr(admin, "admin_users", []) or []),
        )

    async def _harass_resolve_stream(self, qq: str) -> str:
        """维护者私聊 stream 解析（形态照 plugin._resolve_target_stream :392-414）：
        get_stream_by_user_id 首选 → get_private_streams 遍历回退；失败返回空串。"""
        try:
            info = await self.ctx.chat.get_stream_by_user_id(user_id=qq)
            payload = info.get("stream") if isinstance(info, dict) else None
            payload = payload or info
            if isinstance(payload, dict) and payload.get("stream_id"):
                return str(payload["stream_id"])
        except Exception:
            pass
        try:
            streams = await self.ctx.chat.get_private_streams()
            if isinstance(streams, dict):
                streams = streams.get("streams") or streams.get("data") or []
            for s in streams or []:
                if isinstance(s, dict) and str(s.get("user_id") or "") == qq and s.get("stream_id"):
                    return str(s["stream_id"])
        except Exception:
            pass
        return ""

    @Tool(
        "lbm_report_harassment",
        description="受到骚扰时调用（被恶意刷屏/辱骂/攻击/被故意戏弄到不适）——把情况告诉维护者，"
                    "由维护者处理；普通玩笑与日常聊天不要调用",
        parameters={
            "description": {
                "type": "string",
                "description": "一句话说明发生了什么",
                "required": True,
            },
        },
    )
    async def mai_tool_report_harassment(self, description: str = "", **kwargs: Any) -> str:
        """LLM Tool: 受骚扰时向维护者告状（运维消息，不带情绪后缀；冷却与维护者解析进程内缓存）。"""
        cfg = getattr(self, "config", None)
        admin = getattr(cfg, "admin", None)
        # 双保险：正常情况本工具在 harass_report_enabled=false 时已被 get_components 剔除不注册，
        # 配置热更到关闭但组件仍驻留的窗口期在此兜底。
        if admin is None or not bool(getattr(admin, "harass_report_enabled", True)):
            return "告状工具未启用。"
        text = str(description or "").strip()
        if not text:
            return "告状失败：请先用一句话说明发生了什么。"
        location = str(kwargs.get("stream_id") or "").strip()

        maintainer_qq = self._harass_maintainer_qq()
        if not maintainer_qq:
            self._harass_log(action="info", reason="no_maintainer", detail="未配置维护者",
                             maintainer_qq="", location=location)
            return "告状失败：未配置维护者（[admin] harass_report_target_qq 与 admin_users 皆空），无法转达。"

        state = self._harass_state()
        cooldown_minutes = max(0, int(getattr(admin, "harass_report_cooldown_minutes", 30) or 0))
        now_ts = monotonic_now()
        allowed, remaining = cooldown_check(now_ts, state.get("last_ts", 0.0), cooldown_minutes)
        if not allowed:
            self._harass_log(action="skip", reason="cooldown_skip",
                             detail=f"冷却中跳过（剩 {remaining} 分钟）",
                             maintainer_qq=maintainer_qq, location=location)
            return cooldown_hint(remaining)

        # 维护者解析结果进程内缓存（QQ 变了才重解析）
        stream_id = state.get("resolved_stream") or ""
        if state.get("resolved_qq") != maintainer_qq or not stream_id:
            stream_id = await self._harass_resolve_stream(maintainer_qq)
            state["resolved_qq"] = maintainer_qq
            state["resolved_stream"] = stream_id
        if not stream_id:
            self._harass_log(action="info", reason="stream_resolve_failed",
                             detail=f"维护者 {maintainer_qq} 私聊流解析失败",
                             maintainer_qq=maintainer_qq, location=location)
            return "告状失败：暂时联系不上维护者（私聊流解析失败），稍后再试。"

        final_text = build_report_text(text, location)
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.error(f"[{HARASS_TOOL_NAME}] 发送异常: {e}")
            self._harass_log(action="info", reason="send_failed",
                             detail=f"发送异常: {e}", maintainer_qq=maintainer_qq,
                             location=location)
            return f"告状发送异常: {e}"
        if result:
            state["last_ts"] = now_ts
            self._harass_log(action="trigger", reason="reported",
                             detail=f"bot 告状：{text[:40]}", maintainer_qq=maintainer_qq,
                             location=location)
            return f"告状已发送: {final_text[:60]}..."
        self._harass_log(action="info", reason="send_failed",
                         detail="发送返回 False", maintainer_qq=maintainer_qq,
                         location=location)
        return "告状发送失败: 发送返回 False"

    # ==================================================================
    # Commands
    # ==================================================================

    def _is_authorized_command_user(self, user_id: str, is_local_operator: bool = False) -> bool:
        """校验命令触发者是否有权使用 /lbm_mai_* 命令。

        规则：本机控制台操作员天然放行；其余仅白名单 target_qq 本人可用
        （兼容 ``qq:123456`` 平台前缀写法）。target_qq 为占位默认值/无效值时
        一律拒绝（默认拒绝）。
        """
        if is_local_operator:
            return True
        try:
            target_qq = target_qq_of(self.config)
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
                await self.ctx.send.text("此命令仅限绑定的陪伴用户使用哦~", stream_id)
            except Exception:
                pass
        return False

    @Command(name="/lbm_mai_affection", pattern=r"(?<!\S)/lbm_mai_affection(?:\s+(?P<mai_level>\S+))?\s*$",
             description="查看/调整好感度（score/baseline/档位/今日Δ）。用法: /lbm_mai_affection [档位1~N]")
    async def mai_cmd_affection(self, **kwargs: Any) -> tuple[bool, str, bool]:
        companion = self._mai_companion()
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        if not self._mai_patrol_enabled():
            # T3 #4：指令按文档返回未启用提示（此前静默照跑）
            return True, "陪伴模块未启用（[patrol].enabled=false）。", True
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

        preset = companion.affection.preset() if companion else None
        tier_count = preset.size if preset else 5
        labels = preset.labels() if preset else ()
        usage = "、".join(f"{i + 1}={labels[i]}" for i in range(min(tier_count, len(labels)))) \
            or "1~%d" % tier_count
        if not level_str:
            # 不带参数 = 查看好感状态（M2：含最近印象与关系温度）
            current = self._mai_affection_state_line() if companion else "好感度管理器未初始化。"
            await _reply(
                f"用法: /lbm_mai_affection <1~{tier_count}>（不带参数只看状态）\n"
                f"档位（预设「{companion.affection.preset_name() if companion else '恋人'}」）：{usage}"
                f"（score 移至该档区间中值）\n"
                f"当前状态: {current}"
            )
            return False, "参数错误", True
        try:
            tier_1based = int(level_str)
        except (ValueError, TypeError):
            await _reply(f"「{level_str}」不是有效数字，请使用 1~{tier_count}。")
            return False, "参数错误", True
        if not 1 <= tier_1based <= tier_count:
            await _reply(f"好感度档位只能填 1~{tier_count}（当前预设：{usage}），收到的是 {tier_1based}。")
            return False, "参数错误", True
        if companion is None:
            await _reply("好感度管理器未初始化，无法调整。")
            return False, "管理器未初始化", True
        companion.affection.set_tier(tier_1based - 1)
        self.ctx.logger.info(f"好感度已通过命令调整到第 {tier_1based} 档（score 移至对应区间中值）")
        await _reply(f"好感度已更新：{self._mai_affection_state_line()}")
        return True, "好感度已调整", True

    @Command(name="/lbm_mai_help", pattern=r"(?<!\S)/lbm_mai_help\s*$",
             description="查看麦麦陪伴所有可用命令")
    async def mai_cmd_help(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if not await self._authorize_command(kwargs):
            return True, "没有权限", True
        stream_id = str(kwargs.get("stream_id", "") or "")
        name = self._mai_lover_name()
        help_text = (
            f"{name} 可用命令:\n"
            "/lbm_mai_affection — 查看/调整好感度: /lbm_mai_affection <1~档位数>\n"
            "/lbm_diag          — 排障诊断 [llm|decision|life] [天数]（管理员；旧名 mai_llm_log/mai_diag 仍可用）\n"
            "/lbm_dashboard     — 状态总览图（日程/关系/情绪/空间，管理员）\n"
            "日程与生活管理见 /lbm_plan 与 /lbm_life\n"
            "/lbm_mai_help      — 本帮助"
        )
        try:
            await self.ctx.send.text(text=help_text, stream_id=stream_id)
            return True, "帮助已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_help 发送失败: {e}")
            return False, f"发送失败: {e}", True

    @Command(
        name="/lbm_diag",
        pattern=r"(?<!\S)/lbm_diag(?:\s+(?P<diag_args>.*?))?\s*$"
                r"|(?<!\S)(?P<legacy_llm>/lbm_mai_llm_log)(?:\s+(?P<llm_days>\S+))?\s*$"
                r"|(?<!\S)(?P<legacy_decision>/lbm_mai_diag)(?:\s+(?P<mai_days>\S+))?\s*$",
        description="排障诊断合一：/lbm_diag [llm|decision|life|streams] [天数]"
                    "（旧名 mai_llm_log/mai_diag/life diag 保留为别名）",
    )
    async def mai_cmd_diag(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """A5 排障四合一：llm / decision / life / streams 子命令 + 旧命令名别名直达。

        输出与旧三件套完全同构（llm = 旧 /lbm_mai_llm_log、decision = 旧
        /lbm_mai_diag、life = 旧 /lbm_life diag 紧凑文本）；旧名由
        pattern 的 ``legacy_llm`` / ``legacy_decision`` 命名捕获进入同一逻辑（同一组件，
        零组件成本）。``streams``（2026-10-20 用户指示）输出主动候选流/名单状态，
        §25.2 起改合并转发送出（§26.1 起为多节点布局：固定 2 节点 + 群聊/私聊切片；
        R7：用户 2026-10-21 追加三直派②、2026-10-22 三直派）。
        鉴权统一收归 admin 判定（旧 mai_diag/llm_log 走 target 白名单、
        旧 life diag 走 admin，收归后口径见 USAGE）。
        """
        stream_id = str(kwargs.get("stream_id", "") or "")
        if not self._qz_is_admin(str(kwargs.get("user_id") or ""),
                                 bool(kwargs.get("is_local_operator", False))):
            await self._diag_reply(stream_id, "权限不足：仅管理员可用 /lbm_diag。")
            return True, "没有权限", True

        groups = kwargs.get("matched_groups")
        groups = groups if isinstance(groups, dict) else {}
        if str(groups.get("legacy_llm") or "").strip():
            return await self._diag_llm_log(
                stream_id, self._diag_parse_days_arg(groups.get("llm_days")))
        if str(groups.get("legacy_decision") or "").strip():
            return await self._diag_decision_log(
                stream_id, self._diag_parse_days_arg(groups.get("mai_days")))

        parts = str(groups.get("diag_args") or "").split()
        sub = parts[0].lower() if parts else ""
        raw_days = parts[1] if len(parts) > 1 else ""
        if sub == "llm":
            return await self._diag_llm_log(stream_id, self._diag_parse_days_arg(raw_days))
        if sub == "decision":
            return await self._diag_decision_log(stream_id, self._diag_parse_days_arg(raw_days))
        if sub == "life":
            # 天数保持旧 /lbm_life diag 语义：1~7，缺省/非法一律 1
            days = self._diag_parse_days_arg(raw_days, lo=1, hi=7) or 1
            await self._diag_reply(stream_id, self._qz_diag_text(days))
            return True, "决策日志已发送", True
        if sub == "streams":
            # 2026-10-20 用户指示：主动候选流/名单状态诊断（无 emoji）。
            # R7 依据 = 用户 2026-10-21 追加三直派②（计划书 §二十五）：改合并转发送出——
            # 走 _mai_send_forward 既有 forward+纯文本回退链，forward 失败回退纯文本
            # = 节点 content join；§26.1 起节点数随群聊/私聊切片数变化。
            # §26.2（R7 依据 = 用户 2026-10-22 三直派）：``streams <正整数>`` 单流触发状态，
            # 越界/非数字经 forward 单节点提示可用序号范围。
            if len(parts) > 1:
                return await self._mai_diag_stream_status(stream_id, parts[1])
            records = await self._mai_diag_streams_records()
            return await self._mai_send_forward(records, stream_id, "streams 诊断")
        # 无参数与未知子命令同档：只回用法帮助（帮助属正常应答，不算命令失败）
        await self._diag_reply(stream_id, _DIAG_USAGE_TEXT)
        return True, ("用法已发送" if not sub else f"未知子命令: {sub}"), True

    @Command(
        name="/lbm_dashboard",
        pattern=r"(?<!\S)/lbm_dashboard\s*$",
        description="聚合总览四模块状态（日程/关系/情绪/空间）并渲染状态图，失败自动降级纯文本",
    )
    async def mai_cmd_dashboard(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """M7 聚合状态指令：四模块状态块 → 状态图 + 纯文本。

        发送形态同 planning/command_service.py 的 ``/lbm_plan list``：有图 → 合并转发
        （第一条图片、第二条文字）；转发失败回退 ``send.image`` → ``send.text``；
        无图（PIL 缺失/绘制异常/超时）直接发纯文本，全程静默降级不发失败提示。
        文本档末行追加配置摘要（A5：mai_config Tool 并入此处；读配置异常则跳过该行）。
        """
        if not self._qz_is_admin(str(kwargs.get("user_id") or ""),
                                 bool(kwargs.get("is_local_operator", False))):
            try:
                await self.ctx.send.text("权限不足：仅管理员可用 /lbm_dashboard。",
                                         str(kwargs.get("stream_id", "") or ""))
            except Exception:
                pass
            return True, "没有权限", True

        stream_id = str(kwargs.get("stream_id", "") or "")
        blocks = self._mai_dashboard_blocks()
        text = cateye_dashboard.render_dashboard_text(blocks)
        try:
            text += "\n" + self._mai_config_summary(multiline=False)
        except Exception as exc:
            # 配置字段缺失（如宿主裁剪过 config）不该连带整条总览发不出去：只丢这一行
            self.ctx.logger.info(f"状态总览配置摘要行跳过: {exc}")
        image_base64 = await self._mai_draw_dashboard(blocks)

        if not image_base64:
            try:
                await self.ctx.send.text(text=text, stream_id=stream_id)
                return True, "状态总览已发送", True
            except Exception as e:
                self.ctx.logger.error(f"mai_cmd_dashboard 发送失败: {e}")
                return False, f"发送失败: {e}", True

        # F2（2026-10-11）+ 2026-10-20 用户口径：面板图在前，其后跟随「日程与情绪变动」
        # 日志文字档（48h 窗口；每条消息 ≤50 条日志、单次最多 10 条消息 = 500 条；
        # 无记录时不发文字档）。合并转发上限 99 条，图片占 1 条，分片最多 10 条。
        records: list[dict[str, Any]] = [
            {"user_id": "0", "nickname": "状态总览",
             "segments": [{"type": "image", "content": image_base64}]},
        ]
        change_logs = self._mai_change_log_text(hours=48, cap=500)
        total_logs = len(change_logs)
        for index, segment_text in enumerate(change_logs):
            nickname = ("日程与情绪变动" if total_logs == 1
                        else f"日程与情绪变动 {index + 1}/{total_logs}")
            records.append({
                "user_id": "0", "nickname": nickname,
                "segments": [{"type": "text", "content": segment_text}]},
            )
        try:
            await self.ctx.send.forward(records, stream_id)
            return True, "状态总览已发送", True
        except Exception as forward_exc:
            self.ctx.logger.warning(f"状态总览合并转发失败，回退普通发送: {forward_exc}")
        try:
            await self.ctx.send.image(image_base64, stream_id)
        except Exception as img_exc:
            # 图片只是尽力而为的附加档：它失败也必须继续落到文本档（否则管理员什么都收不到）
            self.ctx.logger.warning(f"状态总览图片回退发送失败: {img_exc}")
        if change_logs:
            try:
                for segment_text in change_logs:
                    await self.ctx.send.text(text=segment_text, stream_id=stream_id)
                return True, "状态总览已发送", True
            except Exception as log_exc:
                self.ctx.logger.error(f"状态总览变动日志回退发送失败: {log_exc}")
        try:
            await self.ctx.send.text(text=text, stream_id=stream_id)
            return True, "状态总览已发送", True
        except Exception as e:
            self.ctx.logger.error(f"mai_cmd_dashboard 回退文本发送失败: {e}")
            return False, f"发送失败: {e}", True

    # ---- 命令辅助 ----

    def _mai_dashboard_blocks(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """聚合四模块状态块（M7 /lbm_dashboard 的数据面）。

        live 判定必须真实走服务束属性链（不是只看束对象是否存在）：属性名与各束实现
        一致，静态可达性由 behavior 的组件门面审计断言（组件调了不存在的属性会当场红）。
        任一束缺失 → 该块 enabled=False，展示为「（未启用）」。
        """
        planning = getattr(self, "_planning", None)
        companion = getattr(self, "_companion", None)
        emotion_svc = getattr(self, "_emotion", None)
        qzone = getattr(self, "_qzone", None)

        planning_live = planning is not None and planning.goal_manager is not None
        companion_live = (companion is not None and companion.affection is not None
                          and companion.patrol is not None)
        emotion_live = emotion_svc is not None and emotion_svc.service is not None
        if emotion_live:
            # 情绪模块总开关只在宿主提供该判定时生效（离线测试宿主可能没有 emotion Mixin）
            getter = getattr(self, "_emo_module_enabled", None)
            if callable(getter):
                emotion_live = bool(getter())
        try:
            qzone_live = qzone is not None and bool(self.config.qzone.enabled)
        except Exception:
            qzone_live = False

        emotion_extra = {}
        described = getattr(emotion_svc, "emotion_description_body", None)
        if callable(described):
            try:
                emotion_extra["description"] = str(described() or "")
            except Exception:
                emotion_extra["description"] = ""
        mode_getter = getattr(emotion_svc, "evaluate_mode", None)
        if callable(mode_getter):
            try:
                emotion_extra["mode"] = str(mode_getter() or "")
            except Exception:
                emotion_extra["mode"] = ""

        usage_summary = None
        llm = getattr(self, "_llm", None)
        if llm is not None and callable(getattr(llm, "usage_summary", None)):
            try:
                usage_summary = llm.usage_summary(24)
            except Exception:
                usage_summary = None

        return {
            "planning": cateye_dashboard.planning_block(
                self._mai_schedule_query(),
                planning.goal_manager if planning_live else None,
                enabled=planning_live, now=now),
            "companion": cateye_dashboard.companion_block(
                companion, self.config, enabled=companion_live, now=now),
            "emotion": cateye_dashboard.emotion_block(
                emotion_svc, self.config, enabled=emotion_live, now=now) | emotion_extra,
            "qzone": cateye_dashboard.qzone_block(
                qzone, self.config, enabled=qzone_live, now=now),
            "usage": {"enabled": True, "lines": self._mai_usage_lines(),
                      "tokens": int((usage_summary or {}).get("tokens") or 0),
                      "calls": int((usage_summary or {}).get("calls") or 0),
                      "models": (usage_summary or {}).get("models") or {},
                      "modules": (usage_summary or {}).get("modules") or {}},
        }

    #: 随图日志的事件中文名（bus:<event> 前缀已剥；未登记事件回退原名）
    _CHANGE_LOG_LABELS = {
        "schedule.generated": "日程已生成",
        "schedule.activity_started": "活动开始",
        "schedule.activity_ended": "活动结束",
        "schedule.sleep_started": "入睡",
        "schedule.woke_up": "醒来",
        "schedule.commitment_kept": "约定兑现",
        "schedule.commitment_broken": "约定违约",
        "emotion.updated": "情绪更新",
        "emotion.overhauled": "睡眠大修完成",
        "emotion.low_mood_day": "连续低迷",
    }

    #: 三维字段 → 随图日志的变动标注名（与 emotion/vad 的低分辨率描述用词一致）
    _VAD_DELTA_LABELS = (("valence", "愉悦度"), ("arousal", "唤醒度"), ("dominance", "支配感"))
    #: 相邻两次情绪更新三维变动全部低于该值 → 视为「无变动」合并展示（防低分辨率文案未跨档时的重复行）
    _VAD_SILENT_EPS = 0.005

    def _mai_change_log_text(self, *, hours: int = 48, cap: int = 500,
                             per_message: int = 50, max_messages: int = 10) -> List[str]:
        """总命令随图文字（2026-10-11 用户口径；2026-10-20 分片与精细度改造）：日程变动 + 情绪变化日志。

        数据源 = 决策账本里的总线事件（EventBus 自动落账 ``bus:<event>``）：
        日程 = ``bus:schedule.*``，情绪 = ``bus:emotion.*``。合并按时间升序、只取最近
        ``hours`` 小时（48h 需覆盖今天/昨天/前天三个日历日文件）、最多 ``cap`` 条
        （超出保最新并在尾注省略数）。

        2026-10-20 用户口径：
        - **分片发送**：返回按消息分片的列表——每片最多 ``per_message`` 条日志、
          最多 ``max_messages`` 片（50 条/消息 × 10 消息 = 500 条）；标题行并入首片
          （不计入 50 条），超出发送上限的部分在末片尾注说明条数。
        - **情绪三维变动标注**：账本条目带三维绝对值（emotion.service._updated_payload
          2026-10-20 起）时，逐条对相邻两次 ``emotion.updated`` 计算愉悦度/唤醒度/支配感
          变动并标注「（愉悦度 +0.05｜唤醒度 −0.02）」；三维全部无变动（< _VAD_SILENT_EPS）
          的条目合并不展示（低分辨率文案未跨档导致的逐字重复行由此消除）。
        - **旧账本兼容**：条目缺三维字段时退化为「文案与上一条情绪更新逐字相同 → 合并」。
        日志开关关闭/无记录 → 空列表（该次转发不发文字档）。
        """
        companion = self._mai_companion()
        logger_ = getattr(companion, "decision_logger", None) if companion else None
        if logger_ is None or not getattr(logger_, "enabled", False):
            return []
        cutoff = datetime.now() - timedelta(hours=max(1, int(hours)))
        max_cap = max(1, int(cap))
        picked: "_json.deque" = _deque(maxlen=max_cap)  # noqa: 有界（账本热修）
        total_matched = 0
        log_dir = getattr(logger_, "_dir", None)   # DecisionLogger 的账本目录（有界直读）
        if log_dir is not None:
            # 热修（2026-10-11）：不再 read_entries 整文件解析——子串预过滤 + deque 封顶，
            # 内存/耗时与账本体量解耦（账本可含全部总线事件，量大）
            # 48h 窗口（2026-10-20）：需覆盖今天/昨天/前天三个日历日文件
            for offset in (0, 1, 2):
                day = datetime.now() - timedelta(days=offset)
                path = Path(log_dir) / f"decisions_{day.strftime('%Y-%m-%d')}.jsonl"
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        for line in f:
                            if "bus:schedule." not in line and "bus:emotion." not in line:
                                continue
                            try:
                                entry = _json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            action = str((entry or {}).get("action") or "")
                            if not (action.startswith("bus:schedule.")
                                    or action.startswith("bus:emotion.")):
                                continue
                            try:
                                when = datetime.fromisoformat(str((entry or {}).get("time") or ""))
                            except ValueError:
                                continue
                            if when < cutoff:
                                continue
                            total_matched += 1
                            picked.append((when, action, entry or {}))
                except OSError:
                    continue
        else:
            # 桩/无 _dir 的兜底：走 read_entries（测试与降级形态）；48h 需覆盖 3 个日历日
            try:
                entries = logger_.read_entries(3)
            except Exception:
                return []
            for entry in entries or []:
                action = str((entry or {}).get("action") or "")
                if not (action.startswith("bus:schedule.") or action.startswith("bus:emotion.")):
                    continue
                try:
                    when = datetime.fromisoformat(str((entry or {}).get("time") or ""))
                except ValueError:
                    continue
                if when < cutoff:
                    continue
                total_matched += 1
                picked.append((when, action, entry or {}))
        dropped = max(0, total_matched - len(picked))
        ordered = sorted(picked, key=lambda item: item[0])
        if not ordered:
            return []
        lines: List[str] = []
        merged_silent = 0
        last_vad: Optional[dict] = None   # 上一条 emotion.updated 的三维绝对值（新账本格式）
        last_detail = ""                  # 上一条 emotion.updated 的 detail 文案（旧账本退化去重）
        for when, action, entry in ordered:
            event = action[4:] if action.startswith("bus:") else action
            label = self._CHANGE_LOG_LABELS.get(event, event)
            clock = when.strftime("%m-%d %H:%M")
            detail = str(entry.get("reason") or "").strip()
            if not detail:
                mood = str(entry.get("mood") or "").strip()
                detail = f"情绪基调 {mood}" if mood else ""
            if event == "emotion.updated":
                if all(axis in entry for axis, _ in self._VAD_DELTA_LABELS):
                    vad = {axis: _vad_float(entry.get(axis)) for axis, _ in self._VAD_DELTA_LABELS}
                    if last_vad is not None:
                        deltas = {axis: vad[axis] - last_vad[axis]
                                  for axis, _ in self._VAD_DELTA_LABELS}
                        if all(abs(delta) < self._VAD_SILENT_EPS for delta in deltas.values()):
                            # 三维无变动：与上一条合并（用户报障的「重复日志」即此形态）
                            merged_silent += 1
                            continue
                        marks = [f"{axis_label} {deltas[axis]:+.2f}"
                                 for axis, axis_label in self._VAD_DELTA_LABELS
                                 if round(deltas[axis], 2) != 0.0]
                        if marks:
                            detail = f"{detail}（{'｜'.join(marks)}）" if detail else f"（{'｜'.join(marks)}）"
                    last_vad = vad
                else:
                    # 旧账本条目（无三维字段）：只做逐字文案去重
                    if detail and detail == last_detail:
                        merged_silent += 1
                        continue
                    last_vad = None
                last_detail = detail
            lines.append(f"[{clock}] {label}" + (f"：{detail}" if detail else ""))
        total_seen = len(ordered) + dropped
        notes: List[str] = []
        if dropped:
            notes.append(f"已省略较早 {dropped} 条")
        if merged_silent:
            notes.append(f"无变动合并 {merged_silent} 条")
        header = (f"日程与情绪变动 · 最近 {hours} 小时 · 共 {total_seen} 条"
                  + (f"（{'；'.join(notes)}）" if notes else ""))
        body = lines
        per = max(1, int(per_message))
        max_msgs = max(1, int(max_messages))
        chunks = [body[i:i + per] for i in range(0, len(body), per)]
        tail_note = ""
        if len(chunks) > max_msgs:
            hidden = len(body) - max_msgs * per
            tail_note = f"……（其余 {hidden} 条超出单次发送上限（{max_msgs} 条消息 × {per} 条）未展示）"
            chunks = chunks[:max_msgs]
        segments: List[str] = []
        for index, chunk in enumerate(chunks):
            msg_lines = ([header] if index == 0 else []) + chunk
            if index == len(chunks) - 1 and tail_note:
                msg_lines.append(tail_note)
            segments.append("\n".join(msg_lines))
        return segments

    def _mai_usage_lines(self) -> List[str]:
        """模型消耗块（F3 2026-10-11）：最近 24 小时按模型/按模块的 token 消耗。

        数据源 = ``plugin._llm`` 的消耗账本（``core/llm_usage.py``，无配置项）；
        服务缺失（降级形态）→ 单行提示。
        """
        from core.llm_usage import MODULE_ORDER

        llm = getattr(self, "_llm", None)
        summary = llm.usage_summary(24) if llm is not None and callable(
            getattr(llm, "usage_summary", None)) else None
        if not summary:
            return ["消耗统计不可用（LLM 服务未就绪）"]
        calls = int(summary.get("calls") or 0)
        tokens = int(summary.get("tokens") or 0)
        if calls == 0:
            return ["最近 24 小时：无 LLM 调用"]
        lines = [f"最近 24 小时：{tokens} tokens / {calls} 次调用"]
        if not summary.get("has_usage"):
            lines.append("（宿主未返回 token 用量，仅计调用次数）")
        models = summary.get("models") or {}
        for model, count in sorted(models.items(), key=lambda kv: kv[1], reverse=True)[:5]:
            lines.append(f"模型 {model or '未知'}：{count} tokens")
        modules = summary.get("modules") or {}
        for module in MODULE_ORDER:
            if module in modules:
                lines.append(f"模块 {module}：{modules[module]} tokens")
        return lines

    async def _mai_draw_dashboard(self, blocks: dict[str, Any]) -> Optional[str]:
        """绘制状态总览图（返回 base64）；PIL 缺失/绘制异常/超时**静默**返回 None。

        PIL 是同步阻塞操作，统一丢给默认线程池；超时上限 10 秒（四区块排版远低于此）。
        """
        data_dir = getattr(self, "_data_dir", None)
        output_dir = Path(data_dir) / "images" if data_dir is not None else None
        title = f"属于麦麦的生活 状态总览 {datetime.now().strftime('%Y-%m-%d')}"
        try:
            _img_path, image_base64 = await asyncio.wait_for(
                asyncio.to_thread(cateye_dashboard.render_dashboard_image, title, blocks,
                                  output_dir=output_dir),
                timeout=10.0,
            )
            return image_base64
        except Exception as exc:
            # 静默降级：不提示失败，仅记录日志供排查
            self.ctx.logger.info(f"/lbm_dashboard 状态图绘制放弃（静默降级为文本）: {exc}")
            return None

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
    def _diag_parse_days_arg(raw: Any, *, lo: int = 1, hi: int = 30) -> Optional[int]:
        """解析诊断子命令的 [天数] 参数；缺省/非法返回 None（= 整个保留期，由 read_entries 兜底）。"""
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return max(lo, min(hi, int(text)))
        except ValueError:
            return None

    async def _diag_reply(self, stream_id: str, text: str) -> None:
        """诊断命令的纯文本回复（发送失败只记日志，不影响命令返回的成败语义）。"""
        try:
            await self.ctx.send.text(text=text, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.warning(f"mai_cmd_diag 回复发送失败: {e}")

    async def _mai_diag_streams_records(self) -> list[dict[str, Any]]:
        """``/lbm_diag streams`` 的合并转发节点主体：主动候选流与名单状态（2026-10-20 用户指示）。

        节点布局（无 emoji，便于日志粘贴；§25.2 records 化，§26.1 展示重构，§29.1 序号根治）：

        1. ``[名单状态]``：``[planning_proactive]`` 的群/私聊名单——标题行（mode + 模式
           后标注留空语义：黑名单「留空=全生效」、白名单「留空=无人」）+ 每个配置 id
           一行（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八：ids 原文逐行
           回显、去引号；空名单保留单行、标题行后内联「（空）」）；另含
           ``[patrol].target_qq`` 与 ``enabled``、早/晚安两个总开关；
        2. ``[候选解析]``：``self._planning.proactive`` 可用时调
           ``_resolve_greeting_streams_universe_readonly()``（§29.1：R7 依据 = 用户
           2026-10-23 直派，计划书 §二十九）——候选/序号基底换**只读全集枚举**
           （群聊生效集 ∪ 其他私聊生效集，只做名单解析、不做今日硬门槛过滤；桩服务无
           该支路时退回只读候选支路 ``_resolve_greeting_candidates_readonly``，不再回退
           正式枚举写缓存分支）与 ``_resolve_target_streams_readonly()``；不可用 →
           「规划束未装配」；``[口径]`` 行并入本节点结尾。§31.3 三流分族（R7 =
           用户 2026-10-23 第二轮直派，计划书 §31.1/§31.3）起**目标流回归诊断可见**：
           「问候·群聊」/「问候·其他私聊」/「问候·目标私聊（无条件）」/「频率调控
           （群聊∪私聊）」四个标题行（§二十八逐行化形态保留：标题行后每流一行、
           两空格缩进 + ``#NN 显示名``——频控行纯显示名、无序号；序号 = 群聊 → 其他
           私聊 → 目标私聊，既有序号零漂移）；[候选解析] 的「问候·群聊」行对判定窗查
           不到消息的群聊流行尾加注「（硬闸窗内无消息·不发晨晚问候）」（§29.1 静默群不
           丢弃语义随新窗重锚），``[patrol].target_qq`` 配置行照旧保留在块 1；
        3. ``[候选流最后一条非命令、非 bot 消息·群聊 时段：… (第 i/N 片)]`` 与
           ``[候选流最后一条非命令、非 bot 消息·私聊 时段：…（目标/其他）(第 i/N 片)]``：
           群聊单列、其他私聊与目标流并入「私聊」节点（§31.3 三流分族：目标流回归诊断
           可见、进私聊切片）；两类各按枚举序**每 10 个流切一片**，**无上限、不折叠**。
           检查窗（§31.3，R7 = 用户 2026-10-23 第二轮直派）：三族同窗 =
           **[上份日程开始 → 本份日程开始)**（早安判定窗，经 ``svc.greeting_cycle_span``
           周期四变量读口，**不再看日历 00:00**；窗端不可得 → 行面「周期不可得」）。
           每行走 ``get_by_time_in_chat``，过滤空白/斜杠命令后取最新一条的 HH:MM；
           无 → 历史无消息（静默群聊照常占序号、进切片节点）；查询异常 → 查询失败(宁不发)。
           无任何候选流 → 单节点 ``[候选流最后一条非命令、非 bot 消息]`` + ``无候选流``。

        序号（§26.1 同源直派 → §27.2 豁免 → §29.1 全集 → §31.3 三流分族，R7 = 用户
        2026-10-23 第二轮直派）：每个问候候选流一个 ``#NN``（两位补零，≥100 自然进位），
        编号顺序 = **群聊（枚举序）→ 其他私聊（枚举序）→ 目标私聊（无条件族殿后）**，
        自 ``#01`` 起；行 = ``#NN {label}: {结果}``，``[候选解析]`` 块的每流行同步带
        ``#NN``。频率调控-only 流（不在问候候选）不编号、不进 [候选流最后…] 块。
        **全集枚举基底 → 跨帧稳定**（只受名单/宿主枚举变化影响），主查询与子查询
        （``_mai_diag_stream_index``）同序号系。

        诊断纯读（§29.1，R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：整链改走
        只读全集枚举，不再调 ``_resolve_greeting_candidates``（正式写缓存分支）/ 不经
        ``_group_floor_ok(use_cache=True)``——diag 不再写 ``_greeting_group_floor``
        与名单告警缓存（``_greeting_list_warned``），§26.2「快照纯读」从子查询扩到全链。

        节点记录形态与 ``_mai_build_llm_log_records`` 逐字一致：
        ``{"user_id": "0", "nickname": "streams 诊断",
        "segments": [{"type": "text", "content": <节点文本>}]}``。

        显示口径（R7 依据 = 用户 2026-10-21 追加三直派①，计划书 §二十五）：块 2 的候选
        列表与块 3 的每行流展示位一律显示**名称而非纯 session_id**——经
        ``ProactiveService.stream_display_labels()`` 取「session_id → 显示名」映射
        （群 = group_name；私聊 = user_cardname 优先、user_nickname 次之）；名称不可得 →
        回显原 session_id（``{label}``，兜底 ``{sid}``）。块 1 名单 ids 与
        ``[patrol].target_qq`` 行仍是**配置原文**（配置回显不换名）。

        整体 try 兜底：任何异常都只降级为单节点错误文本，不向调用方抛。
        """
        def _record(text: str) -> dict[str, Any]:
            return {"user_id": "0", "nickname": "streams 诊断",
                    "segments": [{"type": "text", "content": text}]}

        try:
            b1: List[str] = ["[名单状态]"]
            cfg_all = getattr(self, "config", None)
            cfg_p = getattr(cfg_all, "planning_proactive", None)
            patrol = getattr(cfg_all, "patrol", None)

            if cfg_p is None:
                b1.append("  [planning_proactive] 配置不可用")
            else:
                g_ids = list(getattr(cfg_p, "proactive_group_ids", None) or [])
                g_mode = str(getattr(cfg_p, "proactive_group_list_mode", "blacklist")
                             or "blacklist")
                p_ids = list(getattr(cfg_p, "proactive_private_ids", None) or [])
                p_mode = str(getattr(cfg_p, "proactive_private_list_mode", "whitelist")
                             or "whitelist")
                # §28.1（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八）：名单拆
                # 「标题行（mode + 留空语义）+ 每个配置 id 一行」，ids 原文回显去引号；
                # 空名单保留单行（标题行后内联「（空）」）。
                b1.append(f"  群聊名单: mode={g_mode}"
                          f"{'（留空=全生效）' if g_mode == 'blacklist' else '（留空=无人）'}"
                          f"{'' if g_ids else '（空）'}")
                for gid in g_ids:
                    b1.append(f"  {gid}")
                b1.append(f"  私聊名单: mode={p_mode}"
                          f"{'（留空=全生效）' if p_mode == 'blacklist' else '（留空=无人）'}"
                          f"{'' if p_ids else '（空）'}")
                for pid in p_ids:
                    b1.append(f"  {pid}")
                b1.append(
                    f"  问候开关: morning={bool(getattr(cfg_p, 'greeting_morning_enabled', True))}"
                    f" night={bool(getattr(cfg_p, 'greeting_night_enabled', True))}"
                )
                # §31.3（R7 = 用户 2026-10-23 第二轮直派）：闸门判据一行留痕
                b1.append(
                    f"  判据: necessity={bool(getattr(cfg_p, 'greeting_require_activation', True))}"
                    f" 早安阈值={int(getattr(cfg_p, 'greeting_other_private_min_messages', 5) or 5)}"
                    f" 晚安阈值={int(getattr(cfg_p, 'greeting_private_night_min_messages', 5) or 5)}"
                )
                try:
                    from planning.config import ScheduleAdvancedSectionConfig as _sadv_cls
                    _sadv = getattr(cfg_all, "schedule_advanced", None)
                    b1.append(
                        f"  补发冷静期={int(getattr(_sadv, 'greeting_min_silence_minutes', 3) or 0)} 分钟"
                    )
                except Exception:
                    pass
            if patrol is None:
                b1.append("  [patrol] 配置不可用")
            else:
                b1.append(
                    f"  [patrol] target_qq={getattr(patrol, 'target_qq', '') or '（未设置）'}"
                    f" enabled={bool(getattr(patrol, 'enabled', False))}"
                )

            b2: List[str] = ["[候选解析]"]
            planning = getattr(self, "_planning", None)
            svc = getattr(planning, "proactive", None)
            greeting_cands: List[Any] = []
            target_streams: List[str] = []
            stream_nums: Dict[str, int] = {}
            # 显示名映射：svc 不可用/取映射失败 → 保持空 dict，全部展示位回显原 sid
            stream_labels: Dict[str, str] = {}
            if svc is None:
                b2.append("  规划束未装配（_planning.proactive 不可用）")
            else:
                effective_cfg = cfg_p if cfg_p is not None else svc._proactive_cfg()
                # §29.1（R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：候选/序号基底
                # 换只读全集枚举（群聊生效集 ∪ 其他私聊生效集，不做今日硬门槛过滤；目标流
                # 不进枚举）→ 序号建立在名单全集上、同序号恒指向同一流。整链纯读：不回退
                # 正式枚举写缓存分支（_resolve_greeting_candidates 不再在此出现）。
                universe_resolver = getattr(
                    svc, "_resolve_greeting_streams_universe_readonly", None)
                if callable(universe_resolver):
                    try:
                        greeting_cands = list(await universe_resolver() or [])
                    except Exception as exc:
                        b2.append(f"  问候候选解析失败: {exc}")
                else:
                    # 桩服务无全集支路 → 退回只读候选支路（同样纯读；该支路仍做群聊
                    # floor 只读实查，仅影响容量不影响纯读纪律——universe 存在时不走它）
                    readonly_resolver = getattr(
                        svc, "_resolve_greeting_candidates_readonly", None)
                    if callable(readonly_resolver):
                        try:
                            greeting_cands = list(
                                await readonly_resolver(effective_cfg) or [])
                        except Exception as exc:
                            b2.append(f"  问候候选解析失败: {exc}")
                # §29.1 诊断全链纯读：频控行同步换只读支路（写告警/解析缓存的正式支路
                # 仅供旧桩服务兜底；真实服务的 readonly 支路恒先命中）
                target_resolver = getattr(svc, "_resolve_target_streams_readonly", None)
                try:
                    if callable(target_resolver):
                        target_streams = list(await target_resolver() or [])
                    else:
                        target_streams = list(await svc._resolve_target_streams() or [])
                except Exception as exc:
                    b2.append(f"  频率调控候选解析失败: {exc}")
                try:
                    stream_labels = dict(await svc.stream_display_labels() or {})
                except Exception as exc:
                    self.ctx.logger.debug(f"获取流显示名失败（按纯 sid 回显）: {exc}")

                by_cat = {"target": [], "group": [], "other": []}
                for item in greeting_cands:
                    try:
                        cand_sid, cand_cat = item
                    except (TypeError, ValueError):
                        continue
                    by_cat.setdefault(str(cand_cat), []).append(str(cand_sid))
                # §27.2 序号 → §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：
                # 编号顺序 = 群聊（枚举序）→ 其他私聊（枚举序）→ 目标私聊（无条件族殿后，
                # 既有序号零漂移），自 #01 起；同一次调用内稳定。
                ordered_pairs: List[tuple] = []
                numbered_seen: set = set()
                for cand_cat in ("group", "other", "target"):
                    for cand_sid in by_cat.get(cand_cat, []):
                        if cand_sid and cand_sid not in numbered_seen:
                            ordered_pairs.append((cand_sid, cand_cat))
                            numbered_seen.add(cand_sid)
                for cand_cat, cand_sids in by_cat.items():
                    if cand_cat in ("target", "group", "other"):
                        continue
                    for cand_sid in cand_sids:
                        if cand_sid and cand_sid not in numbered_seen:
                            ordered_pairs.append((cand_sid, cand_cat))
                            numbered_seen.add(cand_sid)
                stream_nums = {
                    s: idx for idx, (s, _cat) in enumerate(ordered_pairs, start=1)}

                def _num_label(cand_sid: str) -> str:
                    """``#NN 显示名``（名称不可得回显原 sid；未编号则只显示名）。"""
                    label = stream_labels.get(cand_sid, cand_sid)
                    num = stream_nums.get(cand_sid)
                    return f"#{num:02d} {label}" if num else label

                # §29.1（R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：消息实查**前移**
                # ——先对全集流做一次 ``get_by_time_in_chat`` 窗查询，产出「流 → 行面状态」
                # 映射（``last_lines``）+ 「当日无消息群聊」集合（``silent_group_sids``），
                # 供 [候选解析] 群聊行尾加注与切片节点共用；同一帧、同一次查询，两处行面
                # 状态恒一致。纯读：直接走宿主消息接口，不经 ``_group_floor_ok``（不写
                # ``_greeting_group_floor`` 门槛缓存）。
                # §31.3（R7 = 用户 2026-10-23 第二轮直派）：检查窗 = **周期判定窗**
                # [上份日程开始 → 本份日程开始)——经 svc 周期四变量读口
                # ``greeting_cycle_span``，不再看日历 0 点；窗端不可得（0/非法）→
                # 行面统一「周期不可得」。
                try:
                    _span = dict(svc.greeting_cycle_span() or {})
                except Exception:
                    _span = {}
                _window_lo = float(_span.get("prev_start") or 0)
                _window_hi = float(_span.get("cur_start") or 0)
                span_ok = _window_lo > 0 and _window_hi > 0
                _window_fmt = "%Y-%m-%d %H:%M"
                if span_ok:
                    span_text = (f"{datetime.fromtimestamp(_window_lo).strftime(_window_fmt)}"
                                 f" → "
                                 f"{datetime.fromtimestamp(_window_hi).strftime(_window_fmt)}")
                else:
                    span_text = "周期四变量不可得"
                group_window = private_window = span_text
                query_start_ts = _window_lo
                query_end_ts = _window_hi

                ctx_obj = getattr(self, "ctx", None)
                getter = getattr(getattr(ctx_obj, "message", None),
                                 "get_by_time_in_chat", None)
                last_lines: Dict[str, str] = {}
                silent_group_sids: set = set()
                if not span_ok:
                    for sid, _cat in ordered_pairs:
                        head = f"  #{stream_nums.get(sid, 0):02d} {stream_labels.get(sid, sid)}"
                        last_lines[sid] = f"{head}: 周期不可得(宁不发)"
                elif callable(getter):
                    for sid, cat in ordered_pairs:
                        label = stream_labels.get(sid, sid)
                        head = f"  #{stream_nums.get(sid, 0):02d} {label}"
                        start_ts = query_start_ts
                        try:
                            msgs = await getter(
                                sid, str(start_ts), str(query_end_ts), limit=10, filter_mai=True)
                        except Exception:
                            try:
                                msgs = await getter(
                                    sid, str(start_ts), str(query_end_ts), limit=10)
                            except Exception:
                                last_lines[sid] = f"{head}: 查询失败(宁不发)"
                                continue
                        if isinstance(msgs, dict):
                            msgs = msgs.get("messages") or msgs.get("data") or []
                        if not isinstance(msgs, list):
                            last_lines[sid] = f"{head}: 查询失败(宁不发)"
                            continue
                        best_ts: Optional[float] = None
                        best_hm = ""
                        found = False
                        for msg in msgs:
                            if not isinstance(msg, dict):
                                continue
                            if any(msg.get(k) is True for k in ("is_mai", "is_bot", "is_self")):
                                continue
                            text = str(msg.get("processed_plain_text") or "").strip()
                            if not text or text.startswith("/"):
                                continue
                            found = True
                            try:
                                ts_f = float(msg.get("timestamp"))
                                hm = datetime.fromtimestamp(ts_f).strftime("%H:%M")
                            except (TypeError, ValueError, OSError):
                                ts_f = 0.0
                                hm = ""
                            if best_ts is None or ts_f >= best_ts:
                                best_ts = ts_f
                                best_hm = hm
                        if not found:
                            # §27.2（R7）：全体面文案统一为「历史无消息」；§29.1：全集中的
                            # 静默群聊不丢弃——行面状态进行尾加注（问候·群聊行）与切片节点
                            if cat == "group":
                                silent_group_sids.add(sid)
                            last_lines[sid] = f"{head}: 历史无消息"
                        else:
                            last_lines[sid] = f"{head}: {best_hm or '有消息（时间未知）'}"

                # R7 依据 = 用户 2026-10-21 追加三直派①（计划书 §二十五）：
                # 流展示位显示名替代纯 sid；名称不可得 → .get(sid, sid) 回显原 sid。
                # §27.2（R7 依据 = 用户 2026-10-22 第二批四直派，计划书 §二十七）：
                # 陪伴目标聊天流整体不出现——目标问候列表行删除。
                # §28.1（R7 依据 = 用户 2026-10-22 第三批直派，计划书 §二十八）：三个
                # 列表行改「标题行 + 每流一行」，候选行 = #NN 显示名（枚举序与切片/序号
                # 一致、stream_nums 既有）、频控行 = 纯显示名无序号；回显去引号，空列表
                # 时标题行内联「无」（唯一单行情形）。
                gre_group_sids = list(by_cat.get("group", []))
                gre_other_sids = list(by_cat.get("other", []))
                if gre_group_sids:
                    b2.append("  问候·群聊:")
                    for cand_sid in gre_group_sids:
                        # §29.1 静默群聊不丢弃 + §31.3 新窗：判定窗查不到消息者行尾加注
                        # 「（硬闸窗内无消息·不发晨晚问候）」（查不到消息 = 行面
                        # 「历史无消息」，判据 = 非 bot、非斜杠命令、非空白文本）。
                        note_text = ("（硬闸窗内无消息·不发晨晚问候）"
                                     if cand_sid in silent_group_sids else "")
                        b2.append(f"  {_num_label(cand_sid)}{note_text}")
                else:
                    b2.append("  问候·群聊: 无")
                if gre_other_sids:
                    b2.append("  问候·其他私聊:")
                    for cand_sid in gre_other_sids:
                        b2.append(f"  {_num_label(cand_sid)}")
                else:
                    b2.append("  问候·其他私聊: 无")
                # §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：目标流族块回归
                gre_target_sids = list(by_cat.get("target", []))
                if gre_target_sids:
                    b2.append("  问候·目标私聊（无条件）:")
                    for cand_sid in gre_target_sids:
                        b2.append(f"  {_num_label(cand_sid)}")
                else:
                    b2.append("  问候·目标私聊（无条件）: 无")
                # 频率调控-only 流不编号（§26.1）：频控行仍是唯一的频控名单键（群聊 ∪ 私聊）
                if target_streams:
                    b2.append("  频率调控（群聊∪私聊）:")
                    for cand_sid in target_streams:
                        b2.append(f"  {stream_labels.get(cand_sid, cand_sid)}")
                else:
                    b2.append("  频率调控（群聊∪私聊）: 无")

            # §25.2 → §31.3（R7 = 用户 2026-10-23 第二轮直派）：[口径] 行随新机制重写
            b2.append("[口径] 闸门窗 = [上份日程开始 → 本份日程开始)（周期四变量，非日历 0 点）；"
                      "群聊早安=硬闸（+必要性）逐 tick 实查、群聊晚安=连锁（marks v2）、"
                      "其他私聊=阈值 OR 连锁、目标私聊=无条件")

            # §26.1 展示重构 + §27.2 目标流豁免 + §31.3 三流分族（R7 = 用户 2026-10-23
            # 第二轮直派）：群聊/私聊（其他+目标）两族节点各按枚举序每 10 个流切一片
            # （无上限、不折叠）；频率调控-only 流不进本块。检查窗 = §31.1 判定窗
            # [上份日程开始 → 本份日程开始)（§31.3 起实查前移，窗口与行面状态在
            # [候选解析] 之前一次产出，经 `greeting_cycle_span` 读口，非日历 0 点）；
            # 标题时段文本每 tick 现算。
            stream_slices: List[List[str]] = []
            if svc is None or not stream_nums:
                stream_slices.append(["[候选流最后一条非命令、非 bot 消息]",
                                      "  无候选流"])
            else:
                group_sids = [sid for sid, cat in ordered_pairs if cat == "group"]
                # 私聊节点只装其他私聊（目标流豁免），顺序 = 枚举序，与编号一致
                private_sids = [sid for sid, cat in ordered_pairs if cat == "other"]

                def _slice_block(sids: List[str], title_fmt: str) -> List[List[str]]:
                    """按枚举序每 10 个流切一片；标题带时段与「第 i/N 片」。"""
                    blocks: List[List[str]] = []
                    total = (len(sids) + 9) // 10
                    for idx in range(total):
                        chunk = sids[idx * 10:(idx + 1) * 10]
                        lines = [title_fmt.format(i=idx + 1, n=total)]
                        if not callable(getter):
                            lines.append("  消息能力不可用（宁不发）")
                        else:
                            lines.extend(last_lines.get(
                                sid, f"  #{stream_nums.get(sid, 0):02d} "
                                     f"{stream_labels.get(sid, sid)}: 查询失败(宁不发)")
                                for sid in chunk)
                        blocks.append(lines)
                    return blocks

                stream_slices.extend(_slice_block(
                    group_sids,
                    "[候选流最后一条非命令、非 bot 消息·群聊 时段："
                    f"{group_window} (第 {{i}}/{{n}} 片)]"))
                stream_slices.extend(_slice_block(
                    private_sids,
                    "[候选流最后一条非命令、非 bot 消息·私聊 时段："
                    f"{private_window}（目标/其他）(第 {{i}}/{{n}} 片)]"))

            return [_record("\n".join(block))
                    for block in ([b1, b2] + stream_slices)]
        except Exception as exc:  # 兜底：诊断命令绝不抛
            return [_record(f"[streams] 诊断失败: {exc}")]

    async def _mai_diag_stream_index(self) -> tuple[Any, Dict[str, str], List[tuple]]:
        """问候候选的序号索引（与 ``_mai_diag_streams_records`` 同一编号口径）。

        §26.2 子查询（R7 依据 = 用户 2026-10-22 三直派，计划书 §二十六）：编号顺序 =
        群聊（枚举序）→ 其他私聊（枚举序），两位补零、同一帧内稳定。§27.2（R7 依据 =
        用户 2026-10-22 第二批四直派，计划书 §二十七）：**陪伴目标聊天流整条豁免**，
        不进索引、不占序号（即使退回正式枚举支路也按 category 剔除）。
        §29.1（R7 依据 = 用户 2026-10-23 直派，计划书 §二十九）：解析支路换**只读全集
        枚举**（``_resolve_greeting_streams_universe_readonly``：群聊生效集 ∪ 其他私聊
        生效集，不做今日硬门槛过滤；目标流不进枚举）——``streams <序号>`` 与主查询
        同序号系、跨帧稳定；桩服务无全集支路时退回只读候选支路（仍按 category 剔除）。

        返回 ``(svc, labels, ordered_pairs)``；``_planning.proactive`` 不可用 →
        ``(None, {}, [])``（调用方输出「规划束未装配」单节点）。枚举/名称映射异常均
        降级为空（宁可不发，不伪造序号）。
        """
        planning = getattr(self, "_planning", None)
        svc = getattr(planning, "proactive", None)
        if svc is None:
            return None, {}, []
        cfg_all = getattr(self, "config", None)
        cfg_p = getattr(cfg_all, "planning_proactive", None)
        effective_cfg = cfg_p if cfg_p is not None else svc._proactive_cfg()
        # §29.1：优先走只读全集枚举（与主查询同一支路 → 同序号系）；桩服务无该支路时
        # 退回只读候选支路（readonly，不写缓存；不再退回正式枚举写缓存分支）
        universe_resolver = getattr(
            svc, "_resolve_greeting_streams_universe_readonly", None)
        if callable(universe_resolver):
            try:
                cands = list(await universe_resolver() or [])
            except Exception as exc:
                self.ctx.logger.debug(f"全集枚举失败（按空候选处理）: {exc}")
                cands = []
        else:
            readonly_resolver = getattr(svc, "_resolve_greeting_candidates_readonly", None)
            if callable(readonly_resolver):
                cands = list(await readonly_resolver(effective_cfg) or [])
            else:
                cands = []
        try:
            labels = dict(await svc.stream_display_labels() or {})
        except Exception as exc:
            self.ctx.logger.debug(f"获取流显示名失败（按纯 sid 回显）: {exc}")
            labels = {}
        by_cat: Dict[str, List[str]] = {"target": [], "group": [], "other": []}
        for item in cands:
            try:
                cand_sid, cand_cat = item
            except (TypeError, ValueError):
                continue
            by_cat.setdefault(str(cand_cat), []).append(str(cand_sid))
        ordered: List[tuple] = []
        seen: set = set()
        # §27.2 目标流豁免 → §31.3 三流分族（R7 = 用户 2026-10-23 第二轮直派）：
        # 编号 = 群聊 → 其他私聊 → 目标私聊（无条件族殿后，既有序号零漂移）
        for cand_cat in ("group", "other", "target"):
            for cand_sid in by_cat.get(cand_cat, []):
                if cand_sid and cand_sid not in seen:
                    ordered.append((cand_sid, cand_cat))
                    seen.add(cand_sid)
        for cand_cat, cand_sids in by_cat.items():
            if cand_cat in ("group", "other", "target"):
                continue
            for cand_sid in cand_sids:
                if cand_sid and cand_sid not in seen:
                    ordered.append((cand_sid, cand_cat))
                    seen.add(cand_sid)
        return svc, labels, ordered

    async def _mai_diag_stream_status(self, stream_id: str,
                                      raw_num: Any) -> tuple[bool, str, bool]:
        """``/lbm_diag streams <序号>``：指定问候候选流的早/晚安触发状态快照（§26.2）。

        单节点 ``[流 #NN {label}]``，早/晚各一行结论 + 一行「当前判定关键量」；
        序号越界/非数字/0/负数 → 单节点提示可用序号范围与用法（经 forward，失败按
        ``_mai_send_forward`` 回退纯文本）。状态数据一律来自
        ``ProactiveService.greeting_trigger_status``（纯读，不写任何问候状态）。

        取不到数据（规划束未装配/候选解析或状态查询异常）→ 「宁不发（取不到）」，不伪造。
        """
        def _record(text: str) -> dict[str, Any]:
            return {"user_id": "0", "nickname": "streams 流状态",
                    "segments": [{"type": "text", "content": text}]}

        try:
            num_text = str(raw_num or "").strip()
            try:
                num = int(num_text)
            except (TypeError, ValueError):
                num = 0
            svc, labels, ordered = await self._mai_diag_stream_index()
            if svc is None:
                return await self._mai_send_forward(
                    [_record("[流状态] 取不到（规划束未装配，_planning.proactive 不可用）")],
                    stream_id, "streams 流状态")
            total = len(ordered)
            if num < 1 or num > total:
                if total:
                    usage = (f"可用序号范围: #01~#{total:02d}；"
                             f"用法: /lbm_diag streams <序号>")
                else:
                    usage = ("当前无问候候选流（可用序号范围为空）；"
                             "用法: /lbm_diag streams <序号>")
                shown = num_text if num_text else "（空）"
                return await self._mai_send_forward(
                    [_record(f"[流状态] 「{shown}」不是有效序号（需正整数）。\n  {usage}")],
                    stream_id, "streams 流状态")
            sid, _cat = ordered[num - 1]
            label = str(labels.get(sid, sid))
            status = await svc.greeting_trigger_status(sid)
            lines = [f"[流 #{num:02d} {label}]"]
            for kind, title in (("morning", "早安"), ("night", "晚安")):
                info = (status.get("kinds") or {}).get(kind) or {}
                conclusion = str(info.get("conclusion") or "取不到（宁不发）")
                lines.append(f"  {title}: {conclusion}")
                detail = str(info.get("detail") or "").strip()
                if detail:
                    lines.append(f"    {detail}")
            return await self._mai_send_forward([_record("\n".join(lines))],
                                                stream_id, "streams 流状态")
        except Exception as exc:  # 兜底：诊断命令绝不抛
            return await self._mai_send_forward(
                [_record(f"[流状态] 取不到（查询失败，宁不发）: {exc}")],
                stream_id, "streams 流状态")

    async def _diag_llm_log(self, stream_id: str, days: Optional[int]) -> tuple[bool, str, bool]:
        """diag llm 子命令 = 旧 /lbm_mai_llm_log（llm_logger 调用日志合并转发）。"""
        companion = self._mai_companion()
        llm_logger = getattr(companion, "llm_logger", None) if companion else None
        if llm_logger is None or not llm_logger.enabled:
            await self._diag_reply(stream_id,
                                   "LLM 调用日志未启用（配置 [admin] llm_log_enabled = true 后开始记录）。")
            return True, "日志未启用", True

        entries = llm_logger.read_entries(days)
        if not entries:
            await self._diag_reply(stream_id,
                                   f"最近 {days or llm_logger.retention_days} 天没有 LLM 调用日志。")
            return True, "暂无日志", True

        records = self._mai_build_llm_log_records(entries, llm_logger.retention_days)
        return await self._mai_send_forward(records, stream_id, "LLM 日志")

    async def _diag_decision_log(self, stream_id: str, days: Optional[int]) -> tuple[bool, str, bool]:
        """diag decision 子命令 = 旧 /lbm_mai_diag（决策日志合并转发）。"""
        companion = self._mai_companion()
        decision_logger = getattr(companion, "decision_logger", None) if companion else None
        if decision_logger is None or not decision_logger.enabled:
            await self._diag_reply(stream_id,
                                   "主动行为日志未启用（配置 [admin] decision_log_enabled = true 后开始记录）。")
            return True, "日志未启用", True

        entries = decision_logger.read_entries(days)
        if not entries:
            msg = (
                f"最近 {days or decision_logger.retention_days} 天没有主动行为决策记录。"
                "（插件刚加载或日志刚开启时属正常）"
            )
            status_line = self._mai_patrol_status_line()
            if status_line:
                msg += f"\n{status_line}\n（若「巡检=未运行」，说明 stream_id 没解析出来，主动发言整条链路都没跑）"
            await self._diag_reply(stream_id, msg)
            return True, "暂无日志", True

        records = self._mai_build_diag_records(entries, decision_logger.retention_days)
        return await self._mai_send_forward(records, stream_id, "决策日志")

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

    def _mai_patrol_status_line(self) -> str:
        """一行巡检运行状态（/lbm_diag decision 汇总与自诊断用）。"""
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

    def _mai_config_affection_line(self) -> str:
        """配置摘要里的好感度段：优先实时读数（score/baseline/档位，0~200），管理器不可用回退占位。

        档位名规则与 :meth:`_mai_affection_state_line` 一致（优先 ``companion.relation.tier_label``）；
        读数任一步失败（未初始化/字段缺失/异常）都降级为占位文案，绝不向调用方抛异常
        （配置摘要行不该连带整条状态总览发不出去）。§19.4：v1 兼容档展示已移除。
        """
        fallback = "好感度: 未知"
        companion = self._mai_companion()
        if companion is None:
            return fallback
        try:
            affection = companion.affection
            score = float(affection.score())
            baseline = float(affection.baseline())
            tier = int(affection.tier())
        except Exception:
            return fallback
        label = ""
        relation = getattr(companion, "relation", None)
        if relation is not None:
            try:
                label = str(relation.tier_label(tier) or "")
            except Exception:
                label = ""
        if not label:
            try:
                label = str(affection.tier_label() or "")
            except Exception:
                label = str(tier)
        return (f"好感度: score {score:.1f}/baseline {baseline:.1f}/"
                f"档位 {tier}·{label}")

    def _mai_config_summary(self, multiline: bool) -> str:
        """配置摘要（单行紧凑版给状态总览文本降级路径；多行版保留完整排版，当前无命令入口）。

        T6 注册面收口后 /lbm_mai_config 命令已移除：单行版仍由
        /lbm_dashboard 的纯文本档末行消费，多行版仅供离线测试与后续入口复用。

        静默行显示**生效值 + 来源**（N5：``[patrol].silence_*`` 留空 = 跟随 ``[schedule]``
        作息锚点）——与巡检静默闸 / 晚安期限共用 ``resolve_silence_window``（唯一真源），
        不再回显原始配置（留空会显示成「静默 ~」）。
        """
        patrol = self.config.patrol
        silence = resolve_silence_window(self.config)
        miss_llm = "开" if getattr(patrol, "miss_llm_check_enabled", False) else "关"
        screen = "开" if getattr(patrol, "screen_enabled", False) else "关"
        patrol_activity = "开" if getattr(patrol, "patrol_activity_enabled", True) else "关"
        group_inject = "开" if getattr(patrol, "inject_affection_in_group", True) else "关"
        if multiline:
            return (
                "麦麦陪伴配置摘要\n"
                f"调度: 巡检间隔 {patrol.check_interval_minutes}min | "
                f"每日预算 {patrol.daily_max_speak} 条（只计巡检与想念，早晚安免预算） | "
                f"最小间隔 {patrol.min_trigger_interval_minutes}min（早/晚安始终豁免） | "
                f"冷却 {patrol.user_cooldown_minutes}min | "
                f"触发开关 {'开启' if patrol.proactive_trigger_enabled else '关闭'}\n"
                f"时间: 静默 {silence.describe()}（早/晚安时刻由日程模块漂移驱动） | "
                f"想念阈值 {getattr(patrol, 'miss_silence_hours', '4-8')}h（LLM把关{miss_llm}）\n"
                f"概率: 日常 {patrol.default_speak_rate} | "
                f"想念 {patrol.miss_speak_rate} | "
                f"日程巡检 {patrol.activity_trigger_rate}（开关{patrol_activity}）\n"
                f"{self._mai_config_affection_line()} | "
                f"好感度注入: 目标私聊恒定 / 群聊{group_inject} | "
                f"屏幕感知: {screen} | "
                f"日志: {self._mai_patrol_status_line() or '（巡检未初始化）'}"
            )
        return (
            f"麦麦陪伴配置: 巡检间隔 {patrol.check_interval_minutes}min | "
            f"每日预算 {patrol.daily_max_speak} 条 | "
            f"最小间隔 {patrol.min_trigger_interval_minutes}min | "
            f"冷却 {patrol.user_cooldown_minutes}min | "
            f"触发开关 {'开' if patrol.proactive_trigger_enabled else '关'} | "
            f"想念 {getattr(patrol, 'miss_silence_hours', '4-8')}h（LLM把关{miss_llm}） | "
            f"日常概率 {patrol.default_speak_rate} | "
            f"想念概率 {patrol.miss_speak_rate} | "
            f"巡检概率 {patrol.activity_trigger_rate} | "
            f"{self._mai_config_affection_line()} | "
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
            "麦麦陪伴 LLM 调用日志\n"
            f"时间范围: {first_time} ~ {last_time}\n"
            f"共 {total} 条（展示最近 {len(shown)} 条） | 保留 {retention_days} 天"
        )
        records: list[dict[str, Any]] = [
            {"user_id": "0", "nickname": "LLM调用日志",
             "segments": [{"type": "text", "content": header}]}
        ]
        for entry in shown:
            status = "成功" if entry.get("success") else "失败"
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
            "麦麦陪伴 主动行为决策日志\n"
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
                line = f"[{entry.get('time', '?')}] 发起触发 · {trigger_type or '?'}\n{entry.get('detail') or ''}"
            elif action == "spoken":
                line = f"[{entry.get('time', '?')}] 确认发言 · {trigger_type or '?'}\n{entry.get('detail') or ''}"
            elif action == "info":
                line = f"[{entry.get('time', '?')}] 信息 · {reason}\n{entry.get('detail') or ''}"
            else:
                label = _DIAG_REASON_LABELS.get(reason, reason)
                line = f"[{entry.get('time', '?')}] 跳过 · {reason}（{label}）\n{entry.get('detail') or ''}"
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
         description="获取陪伴对象当前正在做什么（活动描述字符串，无活动返回空串）",
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
         description="获取今日完整日程。返回节点列表 [{time, activity, day_offset}]（兼容虚拟陪伴插件返回结构；"
                     "A-22 bug③ 修复：time 一律经 time_utils.format_day_minute 渲染，"
                     "day_offset=1 行带「次日」标注并透传 day_offset 帧归属；"
                     "昨日 day_offset=1 行按 schedule_query 同口径跨日合并，sleep 行过滤）",
         version="1", public=True)
    async def mai_api_get_schedule(self, **kwargs: Any) -> list[dict[str, Any]]:
        """今日完整日程（跨插件 API 面）。§31.3 B3 修复，恢复日帧助手与帧合并口径。

        与 ``schedule_query`` 同一条 24h 环（A-22 bug③ 的修法 = 「走日帧助手 + 透传/合并帧」）：

        - ``time`` 只出自 :func:`planning.time_utils.format_day_minute`（本文件不再出现
          手写 ``//60`` / ``%60``）：昨日并入行按**今日真实钟点**渲染（``00:30``），
          今日晨钟行按当天钟点渲染，今日 ``day_offset=1`` 行（夜猫子「下一个午夜段」）经
          ``start + DAY_MINUTES`` 环抬渲染为 ``次日 00:30``；
        - ``day_offset`` 透传行内原值（旧行/常规行 = 0；昨日并入行 = 1 —— 该值的帧归属是
          **明日早期钟点段**，消费方按「time = 今日钟点、origin = 昨日帧」解读）；
        - 跨日合并：昨日日程里 ``day_offset=1`` 的行即本日真实午夜段（与
          ``schedule_query.find_current_activity:66-81`` / ``find_next_activities:106-114``
          同口径）；今日自身 ``day_offset=1`` 行保留在列表末段（经环抬「次日」标注），
          两类帧行对同一环位互斥不重计；
        - 行过滤对齐 :func:`planning.schedule_query._SKIP_TYPES` 语义（该集合 = ``{"sleep"}``
          ——只滤睡眠行；``rest`` 等清醒行保留，与既有 _SKIP_TYPES 契约零扩张）；
        - 排序列表 = 真实时间轴：昨日并入行（今日凌晨）→ 今日晨钟行 → 今日「次日」行，
          消除夜猫子日表「00:30 行排最前」的时序倒挂。
        """
        schedule_query = self._mai_schedule_query()
        if schedule_query is None:
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        try:
            current_items = schedule_query.get_day_schedule(today)
        except Exception:
            return []
        # 跨日合并取数失败不连带今日列表（昨日帧缺行 = 宁缺勿哑的降级）
        yesterday_items: list[dict[str, Any]] = []
        prev_date = ScheduleQuery._previous_date(today)
        if prev_date:
            try:
                yesterday_items = schedule_query.get_day_schedule(prev_date)
            except Exception:
                yesterday_items = []

        rows: list[tuple[int, dict[str, Any]]] = []
        for item in (current_items or []):
            if item.get("goal_type") in _SCHEDULE_SKIP_TYPES:
                continue
            start = int(item.get("start_minute") or 0)
            offset = int(item.get("day_offset") or 0)
            ring = DAY_MINUTES if offset == 1 else 0   # day_offset=1 = 下一个午夜段行
            rows.append((start + ring, {
                "time": format_day_minute(start + ring),
                "activity": str(item.get("name") or ""),
                "day_offset": offset,
            }))
        for item in (yesterday_items or []):
            # 昨日标记的「今晚午夜段」：今天就发生在 start..end 钟点区间（与 schedule_query
            # find_current_activity 的合并分支同判定），按今日真实钟点展示、不环抬。
            if item.get("goal_type") in _SCHEDULE_SKIP_TYPES or item.get("day_offset") != 1:
                continue
            start = int(item.get("start_minute") or 0)
            rows.append((start, {
                "time": format_day_minute(start),
                "activity": str(item.get("name") or ""),
                "day_offset": 1,
            }))
        rows.sort(key=lambda pair: pair[0])
        return [row for _key, row in rows]

    @API("companion_get_affection_level",
         description="获取当前好感度档位索引（0 起计数，0..N-1；N=当前预设档位数 1~5。"
                     "2026-10-17 起 v1 三档 0/1/2 兼容口径已移除，改为返回当前档位索引）",
         version="1", public=True)
    async def mai_api_get_affection_level(self, **kwargs: Any) -> int:
        companion = self._mai_companion()
        if companion is None:
            return 0
        return companion.affection.tier()
