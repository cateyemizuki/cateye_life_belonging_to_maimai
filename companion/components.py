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
  - maisaka.replyer.before_model_request  过期主动任务块剥离 + 屏幕旁白注入 +
                                       陪伴私聊主动激活轮宿主目标指令改写
                                       （PR-PROACTIVE-REPLY 3.17.2；BLOCKING/NORMAL）
  - maisaka.replyer.after_response     真实回复发送前按字数/精力模拟打字时延
                                       （P2-DEL-1；BLOCKING/LATE）+ 确认真实发言：
                                       提交 pending 预算预留 + 计 1.0 发言数 +
                                       spoken 回执 + 目标私聊真实回复社交电量小幅消耗
                                       （P1-8 / PR-REALISM-TUNING 3.17.20）
                                       （工具/系统出站消息不经此钩子，不加时延）
  - chat.receive.after_process（目标私聊）额外识别「回敬早安/晚安」→ 好感
                                       morning_exchange / night_exchange（P1-AFF-1）
- Tools ×2：lbm_mai_lover_affection（F3 2026-11-04：is_enabled + 目标私聊 scope 校验，
  决策痕带调用者身份）/
  lbm_report_harassment（2026-10-24 告状：受骚扰时转达维护者；纯逻辑见
  companion/harass_report.py，``[admin].harass_report_enabled=false`` 时不注册——
  过滤在 plugin.get_components override；F1 2026-11-04：目标私聊主动打扰口径纳入
  proactive_budget 记账、冷却落盘持久化、回执结构化判定）
  （原 current_activity / status / schedule 三工具已随 T6 注册面收口移除：
  当前活动与日程查询见 lbm_get_schedule_info，状态总览见 /lbm_dashboard）
- Commands ×4：/lbm_mai_affection（鉴权：target_qq 白名单 + 本机控制台，
  占位默认值一律拒绝）｜/lbm_mai_help｜/lbm_diag（A5 排障四合一：
  llm|decision|life|streams 子命令，旧名 /lbm_mai_llm_log、/lbm_mai_diag 与
  /lbm_life diag 保留为别名；鉴权 = admin 判定）｜/lbm_dashboard
  （M7 聚合总览：四模块状态图 + 纯文本降级，文本档展开完整日程并附配置摘要；
  鉴权同 admin 判定；渲染失败/超时静默降级为纯文本）
  （原 /mai_status、/mai_schedule、/mai_config、/mai_test 四条命令已移除：状态/日程
  信息面并入 dashboard 文本档，配置摘要同源。2026-11-03 主动私信工具
  lbm_mai_lover_send_message 已移除：直发绕过主动预算/冷却的缺口关闭，
  主动发言统一经巡检→proactive.trigger 受预算守卫）
- APIs ×3：companion_get_current_activity / companion_get_schedule /
  companion_get_affection_level（源插件 get_* 与 planning 子模块撞名，故加前缀）

「今日日程」一律经 planning.schedule_query.ScheduleQuery 取
（服务束解析 ``plugin._planning``，缺省回退 ``plugin._schedule_query``）。
"""

from __future__ import annotations

import asyncio
import json as _json
import random
import threading
import time
from collections import deque as _deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import cateye_dashboard
from maibot_sdk import API, Command, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from companion.affection_events import classify_greeting_exchange, record_rule_event
from companion.config import target_qq_of
from companion.screen_config import screen_config_of
from companion.screen_tools import ScreenToolsMixin
from companion.harass_report import (
    HARASS_TOOL_NAME,
    TRIGGER_TYPE,
    build_report_text,
    cooldown_check,
    cooldown_hint,
    decision_fields,
    epoch_now,
    resolve_maintainer_qq,
)
from companion.patrol import resolve_silence_window
from .rolling_diag import render_rolling
from integrations.video_share_diag import render_share
from integrations.qzone_video_diag import render_qzone_video, qzone_video_diag_payload
from .streams_diag import build_streams_records
from core.diag import DecisionLogger
from core.storage import load_json, save_json_atomic
from core.inject import pick_payload_key, project_injection, without_stale_proactive_blocks
# §31.3 B3 修复（R7 = 用户 2026-10-23 第二轮直派，A-22 bug③）：读取渲染面时间一律
# 经 time_utils.format_day_minute（禁手写 //60/%60；day_offset=1 行带「次日」标注），
# 日帧行为过滤复用 schedule_query 的 SKIP_TYPES 同语义（{"sleep"}；D1 起为公开常量）。
from planning.schedule_query import ScheduleQuery
from planning.schedule_query import SKIP_TYPES as _SCHEDULE_SKIP_TYPES
from planning.time_utils import DAY_MINUTES, format_day_minute
from suite_prompts import (
    INJECT_AFFECTION_BLOCK_DEFAULT,
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
#: PR-PROACTIVE-GATE（3.17.14）：登记簿惰性单例装配锁（评审修复：并发首次装配
#: 可能构造两个实例，内存态独立且 _save 共用同一 .tmp 互相覆盖 → 丢登记/consumed）。
_PROACTIVE_TURNS_ASSEMBLY_LOCK = threading.Lock()
#: 省略图片时替换成的文本占位符（不能直接删 part，否则 item 可能没有 part）。
_IMAGE_OMITTED_PLACEHOLDER = "[图片已省略：为控制上下文体积]"
#: 打字时延（P2-DEL-1，2026-11-11）：基础 = clamp(1.5 + 0.08×字数×(2−精力), 1, 上限)，
#: 另加 U(-0.5, +1.5) 秒抖动；上限 = [patrol].typing_delay_max_seconds（默认 8，
#: 0 = 不延迟）。只作用于 replyer 的真实回复，工具/系统出站消息不经过该 Hook。
TYPING_DELAY_BASE_SECONDS = 1.5
TYPING_DELAY_PER_CHAR = 0.08
TYPING_DELAY_MIN_SECONDS = 1.0
TYPING_DELAY_JITTER_MIN = -0.5
TYPING_DELAY_JITTER_MAX = 1.5
DEFAULT_TYPING_DELAY_MAX_SECONDS = 8.0


def typing_delay_seconds(chars: Any, energy: Any, *,
                         max_seconds: Any = DEFAULT_TYPING_DELAY_MAX_SECONDS,
                         jitter: Optional[float] = None) -> float:
    """单条真实回复的拟人打字时延（纯函数，P2-DEL-1）。

    ``base = clamp(1.5 + 0.08 × chars × (2 − energy), 1, max_seconds)``；
    ``delay = base + jitter``，jitter 缺省取 ``U(-0.5, 1.5)``，最终钳
    ``[0, max_seconds]``（配置上限是硬上限；0 = 不延迟）。

    - ``chars``：回复正文字符数（负/非法按 0）；``energy``：当前精力 0~1
      （缺失/非法按 0.5 中性，越低打字越慢）；
    - ``jitter`` 显式传入时用于测试/确定性回放，钳在 [-0.5, 1.5]。
    """
    try:
        limit = float(max_seconds)
    except (TypeError, ValueError):
        limit = DEFAULT_TYPING_DELAY_MAX_SECONDS
    if limit != limit or limit <= 0.0:  # NaN / 0 / 负数 → 不延迟
        return 0.0
    limit = min(limit, 60.0)
    try:
        count = max(0, int(chars))
    except (TypeError, ValueError):
        count = 0
    try:
        energy_value = float(energy)
    except (TypeError, ValueError):
        energy_value = 0.5
    if energy_value != energy_value or energy_value in (float("inf"), float("-inf")):
        energy_value = 0.5
    energy_value = max(0.0, min(1.0, energy_value))
    base = TYPING_DELAY_BASE_SECONDS + TYPING_DELAY_PER_CHAR * count * (2.0 - energy_value)
    base = max(TYPING_DELAY_MIN_SECONDS, min(limit, base))
    extra = random.uniform(TYPING_DELAY_JITTER_MIN, TYPING_DELAY_JITTER_MAX) \
        if jitter is None else jitter
    try:
        extra_value = float(extra)
    except (TypeError, ValueError):
        extra_value = 0.0
    if extra_value != extra_value:
        extra_value = 0.0
    extra_value = max(TYPING_DELAY_JITTER_MIN, min(TYPING_DELAY_JITTER_MAX, extra_value))
    return max(0.0, min(limit, base + extra_value))

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

#: /lbm_diag 用法帮助（无参数或未知子命令时原样回复；含旧名别名对应关系）。
#: PR-D1（H10）：追加 qzone_video 只读子输出（命令总数不变，仍是同一个 /lbm_diag）。
_DIAG_USAGE_TEXT = (
    "用法: /lbm_diag <llm|decision|life|streams|video_share|qzone_video> [天数]（管理员）\n"
    "  llm [天数]      — LLM 调用日志，合并转发（1~30 天；旧名 /lbm_mai_llm_log）\n"
    "  decision [天数] — 主动行为决策日志，合并转发（1~30 天；旧名 /lbm_mai_diag）\n"
    "  life [天数]     — 决策日志紧凑文本（1~7 天；旧名 /lbm_life diag）\n"
    "  streams [序号]  — 主动候选流与名单状态；带序号只看该流早/晚安触发状态"
    "（今日最后一条非命令消息时间）\n"
    "  video_share     — 视频分享决策、发送回执与事实同步状态（仅目标私聊或本地）\n"
    "  qzone_video     — 空间视频全链只读诊断：候选状态/最近决策及原因/资产清单/发布回执（含 uncertain）/事实同步/门控拒绝聚合"
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


class CompanionComponentsMixin(ScreenToolsMixin):
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

    def _schedule_enabled(self) -> bool:
        """``[schedule].enabled``（PR8 3.0.0 唯一日程总开关；读不到/异常 → False=如实关断）。

        依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单：束存在 ≠ 日程启用
        （goal_manager 在 plugin:343 无条件装配），对外取数门前必须过本开关。
        """
        try:
            section = getattr(getattr(self, "config", None), "schedule", None)
            return bool(getattr(section, "enabled", False))
        except Exception:
            return False

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

    def _mai_reply_target_guard(self):
        from integrations.proactive_reply_target import ProactiveReplyTargets
        guard = getattr(self, "_proactive_reply_targets", None)
        if guard is None:
            guard = ProactiveReplyTargets()
            self._proactive_reply_targets = guard
        return guard

    def _mai_reply_target_stream(self) -> str:
        plugin_cfg = getattr(getattr(self, "config", None), "plugin", None)
        if not getattr(plugin_cfg, "enabled", True) or not self._mai_patrol_enabled():
            return ""
        return self._mai_target_stream_id()

    def _mai_proactive_turns(self):
        """PR-PROACTIVE-GATE（3.17.14）：主动激活统一登记簿（惰性单例）。

        data_dir 不可得（裸测试桩/装配前窗口）或构造异常 → None，调用方静默
        降级走 3.17.2 旧判定；登记簿本身损坏降级空（见 tools.proactive_turn）。
        """
        registry = getattr(self, "_proactive_turn_registry", None)
        if registry is not None:
            return registry
        data_dir = getattr(self, "_data_dir", None)
        if data_dir is None:
            return None
        # 双重检查加锁：并发首次装配只构造一个实例（实例内另有 threading.Lock）。
        with _PROACTIVE_TURNS_ASSEMBLY_LOCK:
            registry = getattr(self, "_proactive_turn_registry", None)
            if registry is not None:
                return registry
            try:
                from tools.proactive_turn import ProactiveTurnRegistry
                registry = ProactiveTurnRegistry(self, data_dir)
            except Exception:
                log = getattr(getattr(self, "ctx", None), "logger", None)
                if log is not None:
                    log.debug("主动激活登记簿装配失败（降级旧判定路径）", exc_info=True)
                return None
            self._proactive_turn_registry = registry
            return registry

    def _mai_proactive_turn_yields_to_incoming(self, turn: dict[str, Any],
                                               payload_key: str, payload: list[Any]) -> bool:
        """红线让位：上下文最后真实来信比登记时间更新 → 按真实来信轮处理（不改写）。

        items 形态用 meta.timestamp（存在但不可解析 → 保守让位，与旧窗口路径同
        口径）；messages 形态无可解析来信时间戳 → 陪伴目标私聊再核对
        affection.last_user_msg_time（比登记新 → 让位），两信号皆无 → 信任登记
        （问候轮 bug 修复点：问候链 bypass patrol，messages 形态照样改写）。
        **group/其他私聊的 messages 形态**：无 items 时间戳、也无 affection 信号
        （affection 只跟踪目标私聊），来信时序不可核验 → 保守让位不改写（评审
        修复：宁可漏改写，绝不错改真实来信轮；登记保留不消费，真正的主动轮若
        以 items 形态到来仍可改写）。
        """
        from integrations.proactive_reply_target import latest_incoming_timestamp
        created = float(turn.get("created_at") or 0.0)
        items = payload if payload_key == "items" else []
        has_incoming, latest = latest_incoming_timestamp(items)
        if has_incoming:
            if latest is None:
                return True
            if latest.timestamp() > created:
                return True
        chat_type = str(turn.get("chat_type") or "")
        if payload_key != "items" and chat_type != "private_target":
            return True  # 来信时序不可核验 → 保守让位（红线优先于覆盖率）
        if chat_type == "private_target":
            affection = getattr(self._mai_companion(), "affection", None)
            getter = getattr(affection, "last_user_msg_time", None)
            if callable(getter):
                try:
                    last_user = getter()
                except Exception:
                    last_user = None
                if last_user is not None:
                    try:
                        if last_user.timestamp() > created:
                            return True
                    except (AttributeError, TypeError, ValueError, OSError):
                        return True  # 时间不可核验 → 保守让位
        return False

    @HookHandler(
        "maisaka.planner.before_request",
        name="mai_lover_proactive_reply_target",
        description="为陪伴私聊的本插件主动任务指定上下文中用户最后来信的回复目标",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_proactive_reply_target(self, **kwargs: Any) -> dict[str, Any]:
        modified = self._mai_reply_target_guard().before(
            kwargs, stream_id=self._mai_reply_target_stream(),
            target_user=target_qq_of(self.config),
            mode=getattr(getattr(self.config, "patrol", None), "proactive_reply_target_mode", "浅处理"))
        return {"action": "continue", **({"modified_kwargs": modified} if modified is not None else {})}

    @HookHandler(
        "maisaka.planner.after_response",
        name="mai_lover_proactive_reply_target_rewrite",
        description="深度处理：仅将本插件陪伴主动回合的reply目标改为本轮用户最后来信",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=1000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_proactive_reply_target_rewrite(self, **kwargs: Any) -> dict[str, Any]:
        modified, count = self._mai_reply_target_guard().after(
            kwargs, stream_id=self._mai_reply_target_stream(),
            target_user=target_qq_of(self.config))
        if count:
            self.ctx.logger.info("陪伴主动回复目标深度处理：已纠正%d个reply调用", count)
        return {"action": "continue", **({"modified_kwargs": modified} if modified is not None else {})}

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
        # S1（冲浪计划书 §4.1）：全局入站观测（chat.receive.before_process 覆盖所有
        # 已接入流）喂对话忙闲事实——私聊=直接消息；群聊含 @/点名麦麦=直接，其余=环境
        try:
            tracker = getattr(self, "_attention_tracker", None)
            if tracker is not None and isinstance(message, dict):
                sid, grp = extract_session_info_from_message(message)
                if sid:
                    text = str(message.get("raw_message") or message.get("plain_message") or "")
                    lover = str(getattr(self, "_lover_name", "") or "")
                    # 复核 P0-5：群聊仅当 Bot 自身被 @/点名才算直接消息。
                    # 优先宿主结构化 is_mentioned/is_at（宿主语义=Bot 被提及，
                    # 含 @组件目标核对与昵称/别名扫描）；缺失时回退「Bot 昵称
                    # 出现在文本」。不再用任意 "@"——群友互@、邮箱地址都会误判。
                    # 2026-11-10：该忙闲事实只用于后续“能否发起”自动观看，不再
                    # 撤销已进行的观看任务。lover 即 Bot 自身昵称（plugin 注入）。
                    _mentioned = message.get("is_mentioned")
                    _at_bot = message.get("is_at")
                    _structured = bool(_mentioned) or bool(_at_bot)
                    _named = bool(lover) and lover in text
                    direct = (grp is False) or (bool(grp) and (_structured or _named))
                    # 2026-11-10 不打断：忙闲事实只用于“能否发起”自动观看，
                    # 不再撤销已在进行的观看任务（工具/聊天轮不打断）。
                    tracker.note_incoming(str(sid), direct=direct)
        except Exception:
            pass  # 忙闲记录失败绝不影响消息处理
        # F1（v2 §3.3/§3.6，2026-11-11）：群注意力尾部采样。仅群聊、开关开时由
        # 服务内部短路；O(1) 内存追加，零 IO、零 LLM，失败绝不影响消息处理。
        # 目标用户在群内发言视为兴趣信号（刷新 last_direct_at、清空失配缓冲），
        # 但不改变上面 tracker.note_incoming 的 direct 忙闲语义。
        try:
            attention = getattr(self, "_group_attention", None)
            if attention is not None and isinstance(message, dict):
                _sid, _grp = extract_session_info_from_message(message)
                if _sid and _grp is True and not message.get("is_notify"):
                    _text = str(message.get("raw_message")
                                or message.get("plain_message") or "")
                    _lover = str(getattr(self, "_lover_name", "") or "")
                    _mentioned = message.get("is_mentioned")
                    _at_bot = message.get("is_at")
                    _direct = (bool(_mentioned) or bool(_at_bot)
                               or (bool(_lover) and _lover in _text))
                    _uid = extract_user_id_from_message(message)
                    _target_qq = str(self._mai_target_qq() or "")
                    _group_id = ""
                    _info = message.get("message_info")
                    if isinstance(_info, dict):
                        _group_info = _info.get("group_info")
                        if isinstance(_group_info, dict):
                            _group_id = str(_group_info.get("group_id") or "")
                    if not _group_id:
                        _group_id = str(message.get("group_id") or "")
                    attention.observe(
                        str(_sid), _text,
                        direct=_direct,
                        target_user=bool(_target_qq and _uid and _uid == _target_qq),
                        group_id=_group_id,
                    )
        except Exception:
            pass  # 群注意力采样失败绝不影响消息处理
        # F4（v2 §5.5，2026-11-11）：群边界观测——只记「刚被点名/目标用户刚发言」，
        # 供 planner 群状态行抑制；O(1) 内存写、零 IO、零 LLM，失败绝不影响消息处理。
        try:
            boundary = getattr(self, "_social_boundary", None)
            if boundary is not None and isinstance(message, dict):
                _bsid, _bgrp = extract_session_info_from_message(message)
                if _bsid and _bgrp is True and not message.get("is_notify"):
                    _btext = str(message.get("raw_message")
                                 or message.get("plain_message") or "")
                    _blover = str(getattr(self, "_lover_name", "") or "")
                    _bmentioned = bool(message.get("is_mentioned"))
                    _bat = bool(message.get("is_at"))
                    _bdirect = (_bmentioned or _bat
                                or (bool(_blover) and _blover in _btext))
                    _buid = extract_user_id_from_message(message)
                    _btarget = str(self._mai_target_qq() or "")
                    _bgid = ""
                    _binfo = message.get("message_info")
                    if isinstance(_binfo, dict):
                        _bgroup = _binfo.get("group_info")
                        if isinstance(_bgroup, dict):
                            _bgid = str(_bgroup.get("group_id") or "")
                    if not _bgid:
                        _bgid = str(message.get("group_id") or "")
                    boundary.note_incoming(
                        str(_bsid),
                        direct=_bdirect,
                        target_user=bool(_btarget and _buid and _buid == _btarget),
                        group_id=_bgid,
                    )
        except Exception:
            pass  # 群边界观测失败绝不影响消息处理
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
        ``chat_round`` 规则事件（日累计 ≤ +2.0），并刷新 last_user_msg_time
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
                now = datetime.now()
                companion.affection.update_last_user_msg_time(now)
                # 2026-11-11 P1-AFF-1：Bot 30 分钟内发过早/晚安 + 本条是回敬问候
                # → morning_exchange / night_exchange（各 +0.4，每日一次）。
                self._mai_note_greeting_exchange(companion, message, now)
                # 复核 P1-5：chat_round 加 60s/流节流——原实现每条消息直接计数，
                # 19 条连发 2 分钟即刷满全天 +2.0；现静默 60s 后的问答才计一次。
                if self._chat_round_allowed(str(_session_id or ""), now):
                    companion.affection.record_rule_event("chat_round", None, "与 target 一问一答")
                self._mai_note_review_round(companion, message)
        except Exception:
            pass

    def _mai_recent_greeting(self, kind: str) -> bool:
        """该流该 kind 是否 30 分钟内由 Bot 成功触发过问候（P1-AFF-1，只读）。

        真源 = :meth:`planning.proactive_service.ProactiveService.recent_greeting`
        （``_fire_greeting`` 收到宿主 trigger 成功回执时登记，进程内）。规划束未装配/
        该方法缺失/异常 → False（宁可不记，不虚构互道）。

        窗口比较用服务侧 ``time.time()``（不传 naive datetime）：问候时刻是 aware
        时区 epoch，用 naive ``datetime.now()`` 换算在宿主时区≠插件时区时会偏移。
        """
        planning = getattr(self, "_planning", None)
        service = getattr(planning, "proactive", None)
        checker = getattr(service, "recent_greeting", None)
        if not callable(checker):
            return False
        target = self._mai_target_stream_id()
        if not target:
            return False
        try:
            return bool(checker(target, kind, within_minutes=30.0))
        except Exception:
            return False

    def _mai_note_greeting_exchange(self, companion: Any, message: Any,
                                    now: datetime) -> None:
        """用户回敬早安/晚安 → 好感 ``morning_exchange`` / ``night_exchange``（+0.4）。

        前置（全部满足才入账，2026-11-11 P1-AFF-1）：

        1. ``[affection].exchange_events_enabled`` 开（缺省 True；关 = 不记）；
        2. 文本命中 :func:`classify_greeting_exchange`（早/早安/早上好；晚安/睡了/去睡）；
        3. Bot 在 30 分钟内对该目标流成功触发过对应问候（proactive 进程内时间戳）。

        每日一次/不重复入账由 :func:`companion.affection_events.record_rule_event`
        的 ``once_per_day`` 频控保证（第二次调用返回 ``daily_once`` 丢弃，分数不变）。
        任何异常静默降级，绝不影响消息接收链。
        """
        try:
            if not self._mai_section_flag("affection", "exchange_events_enabled", True):
                return
            kind = classify_greeting_exchange(_message_plain_text(message))
            if kind is None:
                return
            greeting_kind = "morning" if kind == "morning_exchange" else "night"
            if not self._mai_recent_greeting(greeting_kind):
                return
            affection = getattr(companion, "affection", None)
            if affection is None:
                return
            record_rule_event(
                affection, kind, 0.4,
                "互道早安" if kind == "morning_exchange" else "互道晚安",
                now=now,
            )
        except Exception:
            try:
                self.ctx.logger.debug("早晚安互动入账失败（忽略）", exc_info=True)
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

        S1（冲浪计划书 §4.1）：planner 请求即回复生成开始——喂在途回复事实。

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
        处理器（schedule_inject）只会拿到空 session_id，好感度块恒 skip、
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
        description="把屏幕旁白以尾随锚点方式插进 replyer 的模型请求上下文；陪伴私聊主动激活轮把宿主目标指令改写为主动激活文案",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_replyer_model_inject(self, **kwargs: Any) -> dict[str, Any]:
        """replyer 通道：屏幕旁白注入 + 主动激活轮目标指令改写，不碰 extra_prompt 等其他入参。

        replyer 的上下文与 planner 不一定相同，所以旁白的锚点在两个通道里
        分开识别、分开失效（见 screen_context）。

        PR-PROACTIVE-REPLY（3.17.2）：陪伴目标私聊 + 插件主动激活轮（active_task
        命中或巡检触发窗口内）时，把宿主渲染的「你想要回复的消息是…」/「你想要
        补充说明你自己…」目标指令整段替换为主动激活文案；未匹配到时保守追加
        尾随锚点并留诊断。非主动轮（用户真实来信回复）逐字不变。
        """
        # mai-love v2.6.2：replyer 的上下文同样携带宿主主动任务块，一并剥离过期块
        _tracker = getattr(self, "_attention_tracker", None)
        if _tracker is not None:
            # 复核 P0-6：传真实流 id——空参时 key 恒 "-"，与 _last_direct 的真实
            # session_id 永不匹配，待答判定失效。
            _sid = str(kwargs.get("session_id") or kwargs.get("stream_id") or "")
            # 2026-11-10 不打断：回复轮/工具调用只记录忙闲事实（用于后续发起闸），
            # 不再撤销正在进行的观看任务。
            _tracker.note_reply_start(_sid)
        stripped = without_stale_proactive_blocks(kwargs)
        if stripped is not None:
            kwargs = stripped

        companion = self._mai_companion()
        if companion is None:
            return {"action": "continue"}
        changed = False
        modified = self._mai_apply_screen_narration(kwargs, CHANNEL_REPLYER)
        if modified is not None:
            kwargs = modified
            changed = True
        rewritten = self._mai_apply_proactive_reply_rewrite(kwargs)
        if rewritten is not None:
            kwargs = rewritten
            changed = True
        if not changed:
            return {"action": "continue"}
        return {"action": "continue", "modified_kwargs": kwargs}

    def _mai_proactive_replyer_active(self, payload_key: str, payload: list[Any]) -> bool:
        """replyer 主动激活轮判定（仅由 _mai_apply_proactive_reply_rewrite 在目标私聊内调用）。

        与 planner 侧同口径：active_task(items) 命中，或巡检触发时间仍在
        ``_PROACTIVE_RULE_WINDOW_SECONDS`` 窗口内。窗口路径额外保守让位：
        上下文里最后一条真实来信比触发时间更新 → 按用户真实来信回复轮处理
        （红线：真实来信轮逐字不变）；来信时间戳不可解析时同样让位。
        """
        import time as _time
        from integrations.proactive_reply_target import active_task, latest_incoming_timestamp
        items = payload if payload_key == "items" else []
        if active_task(items, _time.time() * 1000) is not None:
            return True
        companion = self._mai_companion()
        if companion is None:
            return False
        last_trigger = companion.patrol.get_last_trigger_time()
        if last_trigger is None:
            return False
        if (datetime.now() - last_trigger).total_seconds() > _PROACTIVE_RULE_WINDOW_SECONDS:
            return False
        has_incoming, latest = latest_incoming_timestamp(items)
        if has_incoming:
            if latest is None:
                return False  # 来信时间戳不可解析 → 无法排除真实来信轮，保守让位
            if latest.tzinfo is not None:
                latest = latest.astimezone().replace(tzinfo=None)
            trigger = last_trigger
            if trigger.tzinfo is not None:
                trigger = trigger.astimezone().replace(tzinfo=None)
            if latest > trigger:
                return False
        return True

    def _mai_apply_proactive_reply_rewrite(self, kwargs: dict[str, Any]) -> Optional[dict[str, Any]]:
        """主动激活轮：替换 replyer 宿主目标指令；失败保守追加+诊断。

        PR-PROACTIVE-GATE（3.17.14）：优先消费统一登记簿——所有 chat_type
        （群聊/其他私聊/陪伴目标私聊）× kind（发起原因）按矩阵定制文案。
        PR-RELIABILITY（3.17.17 §1A）：改用 claim 单锁原子领取（查+消费一步，
        并发 replyer 仅一个命中），改写无需再 consume；真实来信比登记更新 →
        让位并 release 回滚领取（红线：逐字不变，登记保留给真正主动轮）。
        registry 未命中时保留 3.17.2 旧判定作 fallback（active_task + patrol 窗，
        仅陪伴目标私聊、兜底文案），兼容未登记路径与旧快照降级。只改提示词
        文本，不触碰预算/静默/好感/隐私门控；非主动轮返回 None（调用方原样继续）。
        """
        from integrations.proactive_reply_target import (
            append_proactive_notice,
            payload_has_proactive_notice,
            rewrite_replyer_payload,
        )
        from tools.proactive_turn import known_notice_texts, rewrite_notice_for
        session_id = str(kwargs.get("session_id") or "")
        payload_key, payload = pick_payload_key(kwargs)
        if not payload_key or not isinstance(payload, list):
            return None
        registry = self._mai_proactive_turns()
        turn = None
        if registry is not None and session_id:
            try:
                # PR-RELIABILITY（3.17.17 §1A）：原子领取——单锁内查+消费一步完成，
                # 并发 replyer 只有一个能拿到同一登记（旧 active_for+consume 两步竞态）。
                turn = registry.claim(session_id)
            except Exception:
                self.ctx.logger.debug("主动激活登记簿读取失败（降级旧判定路径）", exc_info=True)
                turn = None
        if turn is not None:
            if self._mai_proactive_turn_yields_to_incoming(turn, payload_key, payload):
                # 红线：触发后真实来信更新的轮逐字不变。release 回滚本轮领取
                # （claimant 标识防误恢复），登记保留给真正的主动轮。
                try:
                    registry.release(turn.get("turn_id"), turn.get("claimed_by"))
                except Exception:
                    self.ctx.logger.debug("主动激活登记让位回滚失败（忽略）", exc_info=True)
                return None
            # P1-8：本 turn 已确认是主动轮；给 after_response 留进程内标记，
            # 只用于确认预算/发言数，避免把确认窗内的被动回复误算到主动回合。
            self._mai_proactive_reply_marker = {"stream": session_id,
                                                "at": time.time()}
            # 四阶段渗透（2026-11-11）：阶段号随 turn 透传——阶段 1~3 强化独立
            # 想念文案，阶段 4 对任意目标私聊日常主动轮强制覆盖（先日常后追问）。
            try:
                miss_stage = int(turn.get("miss_stage", 0) or 0)
            except (TypeError, ValueError):
                miss_stage = 0
            notice = rewrite_notice_for(turn.get("chat_type"), turn.get("kind"),
                                        miss_stage=miss_stage)
            updated, replaced = rewrite_replyer_payload(payload_key, payload, notice)
            if replaced:
                # claim 已原子消费，无需再 consume。
                self.ctx.logger.info(
                    f"主动激活轮（kind={turn.get('kind')} chat_type={turn.get('chat_type')}）："
                    f"已把 replyer 宿主目标指令替换为定制文案（{replaced} 段）")
                return {**kwargs, payload_key: updated}
            if any(payload_has_proactive_notice(payload_key, payload, text)
                   for text in known_notice_texts()):
                return None  # 已含主动激活文案（重试/重复钩子）：幂等，不二次追加（claim 已消费）
            appended = append_proactive_notice(payload_key, payload, notice)
            if appended is None:
                # 评审修复：注入失败同样一次性消费（claim 已消费）——否则登记在窗内
                # 持续 active，后续任何 replyer 轮（含真实来信轮）都会再次尝试改写。
                self.ctx.logger.warning(
                    f"主动激活轮（kind={turn.get('kind')} chat_type={turn.get('chat_type')}）："
                    "replyer 载荷无可追加文本条目，定制文案未注入（登记已消费）")
                return None
            self.ctx.logger.warning(
                f"主动激活轮（kind={turn.get('kind')} chat_type={turn.get('chat_type')}）："
                "未匹配到宿主目标指令段，已保守追加定制文案作为尾随锚点")
            return {**kwargs, payload_key: appended}
        # ---- 3.17.2 旧路径 fallback（registry 未命中：未登记路径/旧快照降级）----
        stream = self._mai_reply_target_stream()
        if not stream or session_id != stream:
            return None
        if self._mai_companion() is None:
            return None
        if not self._mai_proactive_replyer_active(payload_key, payload):
            return None
        # P1-8：旧路径（registry 未命中）同样标记为主动轮，供 after_response 确认。
        self._mai_proactive_reply_marker = {"stream": session_id,
                                            "at": time.time()}
        updated, replaced = rewrite_replyer_payload(payload_key, payload)
        if replaced:
            self.ctx.logger.info(
                f"陪伴主动激活轮：已把 replyer 宿主目标指令替换为主动激活文案（{replaced} 段）")
            return {**kwargs, payload_key: updated}
        if payload_has_proactive_notice(payload_key, payload):
            return None  # 已含主动激活文案（重试/重复钩子）：幂等，不二次追加
        appended = append_proactive_notice(payload_key, payload)
        if appended is None:
            self.ctx.logger.warning(
                "陪伴主动激活轮：replyer 载荷无可追加文本条目，主动激活文案未注入")
            return None
        self.ctx.logger.warning(
            "陪伴主动激活轮：未匹配到宿主目标指令段，已保守追加主动激活文案作为尾随锚点")
        return {**kwargs, payload_key: appended}

    async def _mai_confirm_optional_reply(self, reply_stream: str) -> dict[str, Any]:
        """P1-8：把目标流上的真实回复回报给可选主动回合回执（提交预算预留）。

        协调器缺失/未装配 → ``not_assembled``；异常静默降级为 ``error``，绝不让
        OBSERVE 钩子拖挂回复链。回执只确认 90 秒窗内最近一笔 pending 回合。
        """
        coordinator = getattr(self, "_optional_actions", None)
        confirm = getattr(coordinator, "confirm_reply", None)
        if not callable(confirm):
            return {"status": "not_assembled"}
        try:
            result = await confirm(reply_stream)
            return result if isinstance(result, dict) else {"status": "unknown"}
        except Exception:
            self.ctx.logger.warning("可选主动回合预算确认失败（忽略）", exc_info=True)
            return {"status": "error"}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="mai_lover_replyer_speak_confirm",
        description="真实回复发送前按字数/精力模拟打字时延，并确认可选回合预算与发言数",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=12000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def mai_hook_replyer_after_response(self, **kwargs: Any) -> None:
        """replyer 实际生成回复时确认可选回合并补计 1.0 发言数。

        P1-8（2026-11-10）：入队不再预扣预算/计数。目标流出现真实回复时先调用
        ``OptionalActionCoordinator.confirm_reply`` 提交最近一笔 pending 回执的
        预算预留，再计 1.0 发言数（替代旧「入队 0.5 + 回复 0.5」）并把 miss claim
        标 ``spoken``。未回复的回合由巡检 ``reconcile_pending`` 在 180 秒后释放，
        因此**不会**出现「只入队也算一次」的旧账。

        仅在最近 90 秒内有 proactive trigger 时计发言数；补计后清除待确认触发，
        因此与顶层 speak_counter 钩子共存时不会重复计数。

        F6（2026-11-04）：补计前比对回复流与目标流——宿主 after_response 携带
        ``session_id``（=回复所在流）；窗口内无关流（群聊/其他用户）的回复不补计、
        不消费待确认触发，spoken 归因不再失真。

        PR-REALISM-TUNING（3.17.20 §1A）：同钩子先对陪伴目标私聊的**真实回复**
        施加社交电量小幅消耗（主动轮按 90 秒窗口同口径排除；见
        :meth:`_mai_chat_social_drain`），再走既有补计逻辑（逐字不变）。

        P2-DEL-1（2026-11-11）：本钩子改为 **BLOCKING**——先 ``await``
        :meth:`_mai_apply_typing_delay` 模拟打字（宿主要在 after_response 返回后才
        发送回复，故阻塞即“发送前等待”；``await asyncio.sleep`` 不阻塞事件循环）。
        挂在本钩子上而不是新增第二个 HookHandler，是为保持注册面 15 个 hook 不变
        （门禁按组件数钉死）；工具/系统出站消息不经过 replyer 钩子，天然不加延迟。
        """
        _tracker = getattr(self, "_attention_tracker", None)
        if _tracker is not None:
            # 复核 P0-6：与 note_reply_start 同口径传真实流 id，保证配对清计数。
            _sid = str(kwargs.get("session_id") or kwargs.get("stream_id") or "")
            _tracker.note_reply_end(_sid)
        # P2-DEL-1：真实回复发送前的拟人打字时延（开关/上限见 [patrol]）。
        await self._mai_apply_typing_delay(kwargs)
        companion = self._mai_companion()
        if companion is None:
            return
        self._mai_chat_social_drain(kwargs, companion)
        # 复核 P1-5：陪伴目标私聊的被动回复也登记「已发言」——原实现只在主动回合
        # 刷 last_speak_time，致 user_initiated 的 prev_user>last_speak 恒真被判
        # already_answered（沉默后再开口永久漏计）。
        _reply_stream = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
        # F1（v2 §3.5）：bot 在该群真实开口 → 刷新 last_bot_reply_at（只刷新已跟踪群；
        # 零 IO、零 LLM，不改变任何回复/预算语义）。
        _group_attention = getattr(self, "_group_attention", None)
        if _group_attention is not None and _reply_stream:
            try:
                _group_attention.note_bot_reply(_reply_stream)
            except Exception:
                pass
        _target_stream = str(companion.patrol.patrol_status().get("stream_id") or "").strip()
        # P1-8：只有 replyer.before_request 的主动轮改写钩子留下的标记，才能证明
        # 这次 after_response 属于主动回合；否则窗内的被动回复会误提交预算/误计发言。
        marker = getattr(self, "_mai_proactive_reply_marker", None)
        marker_ok = (isinstance(marker, dict)
                     and str(marker.get("stream") or "") == _reply_stream
                     and time.time() - float(marker.get("at") or 0.0) <= 300.0)
        if marker_ok:
            self._mai_proactive_reply_marker = None
        confirmation: dict[str, Any] = {"status": "not_target"}
        if _reply_stream and _target_stream and _reply_stream == _target_stream:
            _notify = getattr(companion.affection, "note_bot_replied", None)
            if callable(_notify):
                _notify(datetime.now())
            # P1-8：目标流的**主动轮**真实回复 → 提交最近一笔 pending 可选回合的
            # 预算预留。被动回复（无 marker）只刷 last_speak_time，不动预算。
            if marker_ok:
                confirmation = await self._mai_confirm_optional_reply(_reply_stream)
        patrol = companion.patrol
        last_trigger = patrol.get_last_trigger_time()
        now = datetime.now()
        elapsed = (now - last_trigger).total_seconds() if last_trigger is not None else None
        confirmed_status = (confirmation.get("status")
                            if isinstance(confirmation, dict) else "")
        confirmed_intent = (str(confirmation.get("intent") or "")
                            if isinstance(confirmation, dict) else "")
        # P1-8：命中 pending 回执的确认优先（进程重启后 _last_trigger_time 丢失，
        # 仍能凭持久化回执把这次真实回复记进账）；否则退回既有 90 秒触发窗。
        confirmed_patrol = (confirmed_status == "confirmed"
                            and confirmed_intent in ("missing", "patrol_chat"))
        if not confirmed_patrol:
            if not marker_ok:
                return  # 无主动轮标记 = 被动回复，不补计发言数、不清待确认触发
            if last_trigger is None:
                return  # 没有待确认的 proactive trigger
            if elapsed is None or elapsed >= 90:
                return
            reply_stream = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
            target_stream = str(patrol.patrol_status().get("stream_id") or "").strip()
            if target_stream and reply_stream != target_stream:
                return  # 非目标流的回复不算主动发言确认（保留待确认触发给正确的流）
        trigger_type = patrol.get_last_trigger_intent() or confirmed_intent
        # P1-8：确认实际开口才计 1.0（替代旧「入队 0.5 + 回复 0.5」两段）；
        # 未回复的回合由 patrol._reconcile_stale_optional_actions 超时释放。
        companion.affection.increment_speak(1.0)
        claim_intent = confirmed_intent if confirmed_status == "confirmed" else trigger_type
        if claim_intent == "missing":
            mark_spoken = getattr(patrol, "mark_miss_spoken", None)
            if callable(mark_spoken):
                mark_spoken()
        patrol.clear_last_trigger_time()
        elapsed_text = f"{elapsed:.0f}" if elapsed is not None else "未知"
        self._mai_log_proactive_event(
            action=_ACTION_SPOKEN,
            reason="reply_confirmed",
            trigger_type=trigger_type,
            detail=(f"planner 已生成回复并发送（距触发 {elapsed_text} 秒）｜触发类型={trigger_type or '未知'}"
                    f"｜预算确认={confirmation.get('status') if isinstance(confirmation, dict) else 'unknown'}"),
        )

    async def _mai_apply_typing_delay(self, kwargs: dict[str, Any]) -> None:
        """真实回复发送前插入拟人打字时延（P2-DEL-1，2026-11-11）。

        由 :meth:`mai_hook_replyer_after_response`（BLOCKING）在发送前调用；宿主要在
        after_response 返回后才由 reply 工具发送，故 ``await`` 即「发送前等待」。
        工具调用/系统出站消息不经过 replyer 钩子，天然不加延迟。

        - 开关 ``[patrol].typing_delay_enabled``（缺省 True；关 → 0、不 sleep）；
        - 时长 = :func:`typing_delay_seconds`（字数 + LifeState 精力 + 抖动），
          上限 ``[patrol].typing_delay_max_seconds``（默认 8，0 = 不延迟）；
        - ``response`` 为空（纯工具调用/无正文）→ 不延迟；
        - ``await asyncio.sleep`` 不阻塞事件循环；异常静默降级不拖挂回复链。
        """
        try:
            if not self._mai_typing_delay_enabled():
                return
            text = str(kwargs.get("response") or "").strip()
            if not text:
                return  # 工具调用 / 空正文：不模拟打字
            try:
                max_seconds = float(self._mai_section_attr(
                    "patrol", "typing_delay_max_seconds",
                    DEFAULT_TYPING_DELAY_MAX_SECONDS))
            except (TypeError, ValueError):
                max_seconds = DEFAULT_TYPING_DELAY_MAX_SECONDS
            delay = typing_delay_seconds(len(text), self._mai_life_energy(),
                                         max_seconds=max_seconds)
            if delay <= 0.0:
                return
            await self._mai_typing_sleep(delay)
        except Exception:
            try:
                self.ctx.logger.debug("打字时延施加失败（忽略）", exc_info=True)
            except Exception:
                pass

    #: PR-REALISM-TUNING（3.17.20 §1A）：真实回复社交消耗去重窗（秒）。宿主可能对
    #: 同一回复重试/重复派发 after_response；同 (流, 回复指纹) 窗内只施加一次。
    #: 窗外合法重复的同文案回复不受影响（episode 回执 id 带单调时间戳逐次唯一）。
    _CHAT_SOCIAL_DRAIN_DEDUP_SECONDS = 10.0

    def _chat_round_allowed(self, stream_id: str, now: datetime) -> bool:
        """复核 P1-5：chat_round 每流 60s 最小间隔（进程内节流，重启从头计）。

        返回 True = 本次允许记一轮；无论允许与否都刷新该流时间戳（滚动节流）。
        """
        throttle = getattr(self, "_chat_round_last", None)
        if not isinstance(throttle, dict):
            throttle = {}
            self._chat_round_last = throttle
        key = str(stream_id or "")
        prev = throttle.get(key)
        throttle[key] = now
        return prev is None or (now - prev).total_seconds() >= 60.0

    def _mai_section_attr(self, section_name: str, key: str, default: Any) -> Any:
        """读任意配置段字段（配置/段/键缺失 → ``default``；热重载即时生效）。"""
        section = getattr(getattr(self, "config", None), section_name, None)
        value = getattr(section, key, None)
        return default if value is None else value

    def _mai_section_flag(self, section_name: str, key: str, default: bool) -> bool:
        """读任意配置段布尔开关（缺失 → 缺省值；非法值按 Python 真值语义）。"""
        return bool(self._mai_section_attr(section_name, key, default))

    def _mai_life_state_flag(self, key: str, default: bool) -> bool:
        """读 ``[life_state]`` 布尔开关（配置缺失/非法 → 缺省值；热重载即时生效）。"""
        return self._mai_section_flag("life_state", key, default)

    def _mai_life_energy(self) -> float:
        """当前精力（0~1）：取 LifeState 快照；不可用 → 0.5（中性，不惩罚也不加速）。"""
        life = getattr(self, "_life_state", None)
        getter = getattr(life, "snapshot", None)
        if callable(getter):
            try:
                snapshot = getter()
                if isinstance(snapshot, dict):
                    value = snapshot.get("energy")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        number = float(value)
                        if number == number and number not in (float("inf"), float("-inf")):
                            return max(0.0, min(1.0, number))
            except Exception:
                pass
        return 0.5

    def _mai_typing_delay_enabled(self) -> bool:
        """``[patrol].typing_delay_enabled``（P2-DEL-1）。

        段缺失（配置对象未装配/测试裸桩）→ **False**：宁可不延迟也不睡住发送链，
        与「开关关闭 = 0」同一降级方向；真实配置对象恒有 patrol 段与 schema 默认 True。
        """
        section = getattr(getattr(self, "config", None), "patrol", None)
        if section is None:
            return False
        value = getattr(section, "typing_delay_enabled", None)
        return True if value is None else bool(value)

    async def _mai_typing_sleep(self, seconds: float) -> None:
        """等待 ``seconds`` 秒（独立方法便于测试替换；``await`` 不阻塞事件循环）。"""
        await asyncio.sleep(max(0.0, float(seconds)))

    def _mai_chat_social_drain(self, kwargs: dict[str, Any], companion: Any) -> None:
        """陪伴目标私聊真实回复消耗社交电量（PR-REALISM-TUNING 3.17.20 §1A）。

        - 仅陪伴目标私聊：回复流 == 巡检目标流（F6 同口径；群聊/其他私聊流 id
          不同，天然排除）；流 id 缺失 → 不施加；
        - 主动轮不计：90 秒窗内有 proactive trigger（既有 0.5 补计同口径）→ 跳过；
        - 消耗量 = internal_tuning[life_state].chat_social_drain_per_reply（默认
          0.015，夹取 ≥0；≤0 → 不施加）。经既有 :meth:`LifeStateService.apply_episode`
          显式增量门施加（统一 clamp [0,1]、diag contrib 归 event 桶 kind=chat、
          回执与状态同文件原子保存）——不新增结算通路；
        - 2026-11-11 Gemini 批（报告 §4.1 P1-NUM-2）：目标私聊标记
          ``target_private=True`` 与好感/效价/当前电量一起交给
          :func:`planning.settlement_v2.target_chat_social_delta` 计算实际增量——
          好感折减（1−0.9×好感/200，最低 0.1 倍）与高好感放松对话的微量回充
          都在该纯函数里完成；群聊/其他私聊根本进不了本方法（上方流比对），
          口径不变。**本批只实现目标私聊折减/回充**——群聊与其他私聊的聊天
          社交电量基础消耗仍为 0，报告里「群聊高消耗 vs 目标私聊回血」的对照
          留待后续批次。开关 ``social_battery_affinity_discount`` /
          ``target_chat_social_recharge``（[life_state]，默认开）分别控制；
        - 每回复恰一次：同 (流, 回复指纹) 短窗去重（钩子幂等）；
        - 空闲时 settlement 安静回充仍生效 → 电量随聊天节奏波动而非钉满；
        - 任何异常静默降级（OBSERVE 钩子不拖挂回复链）。
        """
        life = getattr(self, "_life_state", None)
        if life is None:
            return
        try:
            reply_stream = str(kwargs.get("session_id") or kwargs.get("stream_id") or "").strip()
            if not reply_stream:
                return
            target_stream = str(companion.patrol.patrol_status().get("stream_id") or "").strip()
            if not target_stream or reply_stream != target_stream:
                return  # 仅陪伴目标私聊（群聊/其他私聊不计，F6 同口径）
            last_trigger = companion.patrol.get_last_trigger_time()
            if last_trigger is not None and (datetime.now() - last_trigger).total_seconds() < 90:
                return  # 主动轮（既有补计同窗口口径）：不消耗
            import hashlib
            import time as _time
            from core.internal_tuning import tuning_value
            try:
                drain = float(tuning_value("life_state", "chat_social_drain_per_reply"))
            except Exception:
                drain = 0.015
            if drain != drain or drain in (float("inf"), float("-inf")):
                drain = 0.015
            drain = max(0.0, drain)  # 夹取 ≥0（负值调参 → 不消耗）
            if drain <= 0.0:
                return
            fingerprint = hashlib.sha1(
                str(kwargs.get("response") or "").encode("utf-8", "replace")).hexdigest()[:16]
            key = (reply_stream, fingerprint)
            now_mono = _time.monotonic()
            recent = getattr(self, "_mai_chat_drain_recent", None)
            if recent is None:
                recent = self._mai_chat_drain_recent = {}
            for old_key in [k for k, stamp in recent.items()
                            if now_mono - stamp > self._CHAT_SOCIAL_DRAIN_DEDUP_SECONDS]:
                del recent[old_key]
            if key in recent:
                return  # 同一回复重复派发（钩子重试）：幂等跳过
            # ── P1-NUM-2：目标私聊好感折减 / 高好感放松回充（纯函数结算）──
            # target_private 由上方流比对严格成立（群聊/其他私聊进不到这里），
            # 显式标记随 episode 一起交给 apply_episode（其只消费 state_deltas，
            # 未知键忽略——标记用于本处结算判定与审计留痕）。
            from planning import settlement_v2
            affection = getattr(companion, "affection", None)
            affinity: Optional[float] = None
            score_getter = getattr(affection, "score", None)
            if callable(score_getter):
                try:
                    affinity = float(score_getter())
                except Exception:
                    affinity = None
            valence: Optional[float] = None
            valence_getter = getattr(affection, "emotion_valence", None)
            if callable(valence_getter):
                try:
                    valence = valence_getter()
                except Exception:
                    valence = None
            current_social: Optional[float] = None
            try:
                snapshot = life.snapshot()
                if isinstance(snapshot, dict):
                    value = snapshot.get("social_battery")
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        current_social = float(value)
            except Exception:
                current_social = None
            settlement = settlement_v2.target_chat_social_delta(
                drain,
                target_private=True,  # 本方法仅处理目标私聊（上方流比对）
                affinity=affinity,
                social_battery=current_social,
                discount_enabled=self._mai_life_state_flag(
                    "social_battery_affinity_discount", True),
                recharge_enabled=self._mai_life_state_flag(
                    "target_chat_social_recharge", True),
                valence=valence,
            )
            delta = float(settlement.get("delta") or 0.0)
            life.apply_episode({
                "episode_id": f"chat_social_drain:{reply_stream}:{fingerprint}:{now_mono:.6f}",
                "state_deltas": {"social_battery": delta},
                "settlement_kind": "chat",
                "target_private": True,
                "chat_social": {
                    "drain": settlement.get("drain"),
                    "effective_drain": settlement.get("effective_drain"),
                    "factor": settlement.get("factor"),
                    "discount_applied": settlement.get("discount_applied"),
                    "recharge": settlement.get("recharge"),
                    "affinity": settlement.get("affinity"),
                    "relaxed": settlement.get("relaxed"),
                    "reason": settlement.get("reason"),
                },
            })
            recent[key] = now_mono  # 施加成功才记去重戳（失败不挡重试）
            if len(recent) > 64:
                for old_key in sorted(recent, key=recent.get)[:len(recent) - 64]:
                    del recent[old_key]
        except Exception:
            try:
                self.ctx.logger.debug("聊天社交电量消耗施加失败（忽略）", exc_info=True)
            except Exception:
                pass

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
        """主动发言回合给 planner 的主动激活引导（软约束；3.17.2 起不再指认过时消息）。"""
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
        "lbm_mai_lover_affection",
        description="调整与关系对象的好感度：把 score 移动到指定档位的区间中值并重估档位"
                    "（档位数 1~5 随好感度预设表变化，档位名称如恋人=生疏/熟悉/亲密/热恋/挚爱；"
                    "仅限陪伴目标私聊会话使用）",
        parameters={
            "tier": {
                "type": "integer",
                "description": "好感度档位（1 起计数，上限随当前预设 1~5 档）",
                "required": True,
            },
        },
    )
    async def mai_tool_affection(self, tier: int = 1, **kwargs: Any) -> str:
        """LLM Tool：把 score 移到指定档（1~N，1 起计数）的区间中值（管理员兜底，跳过迟滞）。

        F3（2026-11-04，与命令面鉴权契约对齐）：
        - ``[affection].enabled=false`` → 拒绝写入（关=分数/档位不再变化）；
        - 调用会话限**目标陪伴私聊**：stream 精确匹配目标流；或非群聊且调用者本人
          通过命令面 target 白名单（``_is_authorized_command_user`` 同口径）；
          会话/身份不可核验时默认拒绝（防群聊话术诱导改档）；
        - 决策痕：``set_tier`` 的 reason 携带调用者身份（进入 affection.tier_set 留痕）。
        """
        companion = self._mai_companion()
        if companion is None:
            return "好感度管理器未初始化。"
        if not companion.affection.is_enabled():
            return "好感度系统已停用（[affection].enabled=false），不能调整档位。"
        caller_stream = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        caller_user = str(kwargs.get("user_id") or "").strip()
        caller_group = str(kwargs.get("group_id") or "").strip()
        target_stream = self._mai_target_stream_id()
        allowed = bool(target_stream and caller_stream and caller_stream == target_stream)
        if not allowed and not caller_group:
            # 私聊兜底：stream 重建但调用者确为目标用户本人（命令面白名单机制同口径）
            allowed = bool(caller_stream) and self._is_authorized_command_user(
                caller_user, bool(kwargs.get("is_local_operator", False)))
        if not allowed:
            self._mai_log_proactive_event(
                action="skip", reason="tool_scope_denied", trigger_type="affection",
                detail="好感度 Tool 调用会话非目标陪伴私聊（或身份不可核验），已拒绝",
                caller_user=caller_user, caller_stream=caller_stream, caller_group=caller_group)
            return "此工具仅限与陪伴目标的私聊会话中使用。"
        preset = companion.affection.preset()
        tier_count = preset.size
        labels = "、".join(f"{i + 1}={label}" for i, label in enumerate(preset.labels()))
        try:
            requested = int(tier)
        except (TypeError, ValueError):
            return f"档位必须是 1~{tier_count} 的整数，收到的是 {tier!r}。"
        if not 1 <= requested <= tier_count:
            return f"档位只能填 1~{tier_count}（当前预设档位：{labels}），收到的是 {requested}。"
        companion.affection.set_tier(
            requested - 1,
            reason=f"tool user={caller_user or '-'} stream={caller_stream or '-'}")
        self.ctx.logger.info(f"好感度已通过 Tool 调整到第 {requested} 档（score 移至对应区间中值）")
        return f"好感度已更新：{self._mai_affection_state_line()}"

    # ==================================================================
    # 告状工具（2026-10-24 用户直派，需求3）
    # ==================================================================

    def _harass_state_path(self) -> Optional[Path]:
        """告状冷却落盘路径（F1）：``<data_dir>/harass_report_state.json``；取不到目录 → None（纯内存态）。"""
        data_dir = getattr(self, "_data_dir", None)
        if not data_dir:
            data_dir = getattr(self._mai_companion(), "_data_dir", None)
        if not data_dir:
            return None
        return Path(data_dir) / "harass_report_state.json"

    def _harass_state(self) -> dict:
        """告状冷却时间戳 + 维护者解析结果缓存。

        F1（2026-11-04）：``last_ts`` 为 **epoch 秒**并从落盘状态装载——热重载重建
        实例不再清零冷却；损坏/非法时间戳按从未告状处理。维护者解析结果
        （QQ/stream）仍留进程内（热重载后流可能重建，重解析无害）。
        """
        state = getattr(self, "_harass_cache", None)
        if state is None:
            state = {"last_ts": 0.0, "resolved_qq": "", "resolved_stream": ""}
            path = self._harass_state_path()
            if path is not None:
                raw = load_json(path, None)
                if isinstance(raw, dict):
                    ts = raw.get("last_report_ts")
                    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and ts > 0:
                        state["last_ts"] = float(ts)
            self._harass_cache = state
        return state

    def _harass_persist(self, state: dict) -> None:
        """冷却时间戳原子落盘（F1）；失败降级进程内冷却并单次告警，不阻断告状流程。"""
        path = self._harass_state_path()
        if path is None:
            return
        if save_json_atomic(path, {"last_report_ts": float(state.get("last_ts", 0.0))}):
            self._harass_persist_warned = False
            return
        if not getattr(self, "_harass_persist_warned", False):
            self._harass_persist_warned = True
            try:
                self.ctx.logger.warning("告状冷却落盘失败，降级为进程内冷却（热重载将清零）：%s", path)
            except Exception:
                pass

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

    async def _harass_maintainer_qq(self) -> str:
        """按优先级解析目标；只有前两级未配置时才只读请求宿主。"""
        cfg = getattr(self, "config", None)
        direct = getattr(getattr(cfg, "admin", None), "harass_report_target_qq", "")
        companion = getattr(getattr(cfg, "patrol", None), "target_qq", "")
        target = resolve_maintainer_qq(direct, companion_target_qq=companion)
        if target:
            return target
        # 可选宿主全局管理员接口；当前宿主未提供该字段时返回空列表。
        # 不以插件 admin.admin_users 冒充宿主全局管理员。
        try:
            admins = await self.ctx.config.get("permissions.admin_users", [])
            if isinstance(admins, dict):
                admins = admins.get("value") if admins.get("success") is True else []
        except Exception:
            return ""
        return resolve_maintainer_qq(direct, admins, companion)

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
        """LLM Tool: 受骚扰时向维护者告状（运维消息，不带情绪后缀）。

        F1/F2（2026-11-04）：冷却时间戳落盘（热重载不清零）；回退陪伴目标私聊的发送
        受主动打扰预算闸门并以 ``harass_report`` 渠道入账；回执按结构化 ``success is True``
        判定（与 MessageService 同契约）。维护者解析结果仍为进程内缓存。
        """
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

        maintainer_qq = await self._harass_maintainer_qq()
        if not maintainer_qq:
            self._harass_log(action="info", reason="no_maintainer", detail="未配置维护者",
                             maintainer_qq="", location=location)
            return "告状失败：未配置告状对象、陪伴目标或可用的宿主全局管理员，无法转达。"

        state = self._harass_state()
        cooldown_minutes = max(0, int(getattr(admin, "harass_report_cooldown_minutes", 30) or 0))
        now_ts = epoch_now()
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

        # F1（2026-11-04）：纳入主动打扰预算——按 M11 口径仅「发往目标陪伴私聊」受闸/入账
        # （allow_proactive 对非目标流直接放行）；生产未显式配置告状对象时回退陪伴目标，
        # 该直发路径自此与巡检/想念同闸，不再构成预算外例外。
        bus = getattr(self, "_bus", None)
        if bus is not None and not bus.allow_proactive("harass_report", "告状", stream_id=stream_id):
            self._harass_log(action="skip", reason="budget_blocked",
                             detail="当日主动打扰预算已用完，告状未发送",
                             maintainer_qq=maintainer_qq, location=location)
            return "告状未发送：今日主动消息预算已用完，请明天再试。"

        final_text = build_report_text(text, location)
        try:
            result = await self.ctx.send.text(text=final_text, stream_id=stream_id)
        except Exception as e:
            self.ctx.logger.error(f"[{HARASS_TOOL_NAME}] 发送异常: {e}")
            self._harass_log(action="info", reason="send_failed",
                             detail=f"发送异常: {e}", maintainer_qq=maintainer_qq,
                             location=location)
            return f"告状发送异常: {e}"
        # F2（2026-11-04）：结构化回执判定（与 MessageService/视频分享同契约）——
        # 不得用非空失败字典的真值冒充成功；仅 success is True 才置冷却/记账。
        receipt = (dict(result) if isinstance(result, dict) else
                   {"success": result} if isinstance(result, bool) else
                   {"success": False, "uncertain": True})
        if receipt.get("success") is True:
            state["last_ts"] = now_ts
            self._harass_persist(state)
            if bus is not None:
                try:
                    await bus.publish("proactive.used", source="companion",
                                      channel="harass_report", intent="告状",
                                      stream_id=stream_id)
                except Exception as exc:
                    self.ctx.logger.warning(
                        f"[{HARASS_TOOL_NAME}] proactive.used 发布失败（预算记账可能缺失）: {exc}")
            self._harass_log(action="trigger", reason="reported",
                             detail=f"bot 告状：{text[:40]}", maintainer_qq=maintainer_qq,
                             location=location)
            return f"告状已发送: {final_text[:60]}..."
        detail = ("发送回执不确定（无结构化 success）" if receipt.get("uncertain")
                  else "发送返回失败回执")
        self._harass_log(action="info", reason="send_failed",
                         detail=detail, maintainer_qq=maintainer_qq,
                         location=location)
        return "告状发送失败: 发送未返回成功回执"

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

    def _activity_preference_diag_line(self) -> str:
        """P3 复核修复（3.17.33）：偏好缓存/持久 pending/今日请求的只读一行。

        不重建偏好、不发请求、不清计数；未装配/关闭/异常均如实降级。
        """
        getter = getattr(self, 'activity_preferences_status', None)
        if not callable(getter):
            return '日程偏好：未装配'
        try:
            status = getter()
        except Exception as exc:
            return f'日程偏好：读取失败（{type(exc).__name__}）'
        if not isinstance(status, dict) or not status.get('enabled'):
            return '日程偏好：未启用/未装配'
        pending = status.get('pending') if isinstance(status.get('pending'), dict) else {}
        pending_count = max(0, int(status.get('pending_count')
                                   or pending.get('count') or 0))
        bits = [f"缓存 {int(status.get('cached_activities') or 0)} 项",
                f"待评估 {pending_count} 项"]
        since = str(pending.get('since') or '')
        if pending_count and since:
            bits.append('自 ' + since)
        state = str(pending.get('last_status') or '')
        if pending_count and state:
            bits.append('上次 ' + state)
        bits.append(f"今日请求 {int(status.get('requests_today') or 0)}"
                    f"/{int(status.get('daily_limit') or 0)}")
        return '日程偏好：' + ' · '.join(bits)

    @Command(
        name="/lbm_diag",
        pattern=r"(?<!\S)/lbm_diag(?:\s+(?P<diag_args>.*?))?\s*$"
                r"|(?<!\S)(?P<legacy_llm>/lbm_mai_llm_log)(?:\s+(?P<llm_days>\S+))?\s*$"
                r"|(?<!\S)(?P<legacy_decision>/lbm_mai_diag)(?:\s+(?P<mai_days>\S+))?\s*$",
        description="排障诊断合一：/lbm_diag [llm|decision|life|streams|video_share|qzone_video] [天数]"
                    "（旧名 mai_llm_log/mai_diag/life diag 保留为别名）",
    )
    async def mai_cmd_diag(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """A5 排障合一：llm / decision / life / streams / video_share / qzone_video
        子命令 + 旧命令名别名直达（qzone_video 为 PR-D1 H10 只读子输出，命令总数不变）。

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
        if sub == 'video_share':
            if stream_id!=self._mai_target_stream_id() and not kwargs.get('is_local_operator',False):
                await self._diag_reply(stream_id,'请在陪伴目标私聊或本机控制台查看视频分享诊断。')
                return True,'诊断范围受限',True
            service=getattr(self,'_video_social',None)
            payload=service.snapshot() if service is not None else {'status':'unavailable'}
            # PR-B1：经历索引 count/recent 只读诊断（挂既有 /lbm_diag video_share，
            # 命令总数不变；开关关闭时已有数据仍保留只读）。失败空降级。
            index=getattr(self,'_experience_index',None)
            try:
                payload['experience_index']=({'count':index.count(),'recent':index.recent(limit=5)}
                                             if index is not None else {'status':'unavailable'})
            except Exception:
                payload['experience_index']={'status':'unavailable'}
            await self._diag_reply(stream_id,'视频独立分享诊断\n'+_json.dumps(payload,ensure_ascii=False,indent=1)[:10000])
            return True,'视频分享诊断已发送',True
        if sub == 'qzone_video':
            # PR-D1（H10）：既有 /lbm_diag 挂只读子输出（命令总数不变）——候选状态
            # 分布/最近决策及原因/资产清单（id/尺寸/TTL，无路径）/发布回执（含
            # uncertain）/事实同步/门控拒绝聚合。零 LLM、零发送、零落盘；鉴权沿用
            # 顶部 admin 判定（与 /lbm_life qzone_video status 同口径，无聊天正文）。
            payload = qzone_video_diag_payload(self)
            await self._diag_reply(stream_id,'空间视频全链诊断（只读）\n'+_json.dumps(payload,ensure_ascii=False,indent=1)[:10000])
            return True,'空间视频诊断已发送',True
        if sub == "llm":
            return await self._diag_llm_log(stream_id, self._diag_parse_days_arg(raw_days))
        if sub == "decision":
            return await self._diag_decision_log(stream_id, self._diag_parse_days_arg(raw_days))
        if sub == "life":
            # 天数保持旧 /lbm_life diag 语义：1~7，缺省/非法一律 1
            days = self._diag_parse_days_arg(raw_days, lo=1, hi=7) or 1
            # P3 复核修复（3.17.33）：附一行只读偏好缓存/待评估/今日请求计数。
            await self._diag_reply(stream_id, self._qz_diag_text(days) + "\n"
                                   + self._activity_preference_diag_line())
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

        # PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，规则 6）：
        # goal_manager 存在 ≠ 日程启用（plugin 装配期无条件建束）——planning_live 并入
        # [schedule].enabled 单开关，关断时规划块如实展示「（未启用）」，不读残留快照。
        planning_live = (planning is not None and planning.goal_manager is not None
                         and self._schedule_enabled())
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
                usage_summary = llm.usage_summary(0)
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
                      "modules": (usage_summary or {}).get("modules") or {},
                      # 2026-10-29 §2.2 口径：四桶拆分 + 缺用量来源调用（PNG 面板展示用；
                      # 只加键不改既有键，旧消费方不受影响）。
                      "buckets": (usage_summary or {}).get("buckets") or {},
                      "missing_usage_source_calls":
                          int((usage_summary or {}).get("missing_usage_source_calls") or 0)},
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
                            except _json.JSONDecodeError:
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

        数据源 = ``plugin._llm`` 的用量记录（``core/llm_usage.py``，无配置项）；
        服务缺失（降级形态）→ 单行提示。
        """
        from core.llm_usage import MODULE_ORDER

        llm = getattr(self, "_llm", None)
        summary = llm.usage_summary(0) if llm is not None and callable(
            getattr(llm, "usage_summary", None)) else None
        if not summary:
            return ["消耗统计不可用（LLM 服务未就绪）"]
        calls = int(summary.get("calls") or 0)
        tokens = int(summary.get("tokens") or 0)
        window = int(summary.get("window_hours") or 24)
        if calls == 0:
            return [f"最近 {window} 小时：无 LLM 调用"]
        lines = [f"最近 {window} 小时：{tokens} tokens / {calls} 次调用"]
        if not summary.get("has_usage"):
            lines.append("（宿主未返回 token 用量，仅计调用次数）")
        prompt_tokens = summary.get("prompt_tokens")
        completion_tokens = summary.get("completion_tokens")
        if prompt_tokens is not None or completion_tokens is not None:
            lines.append(f"输入 {int(prompt_tokens or 0)} / 输出 {int(completion_tokens or 0)} tokens")
        cache_hit = summary.get("cache_hit_tokens")
        cache_miss = summary.get("cache_miss_tokens")
        if cache_hit is not None or cache_miss is not None:
            lines.append(f"缓存命中 {int(cache_hit or 0)} / 未命中 {int(cache_miss or 0)} tokens")
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
        """``/lbm_diag streams`` 合并转发节点主体；实现见 companion/streams_diag.py（P2-06）。"""
        return await build_streams_records(self)

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

    def _mai_config_summary(self, multiline: bool = False) -> str:
        cfg = self.config.patrol
        companion = getattr(self, '_companion', None)
        patrol = getattr(companion, 'patrol', None)
        memory = getattr(patrol, '_initiative_memory', None)
        profile = memory.profile() if memory is not None else {}
        items = ["麦麦陪伴配置摘要", f"动机评估间隔 {cfg.check_interval_minutes} 分钟",
                 f"每日主动预算 {cfg.daily_max_speak} 次",
                 f"想念锚点 {profile.get('anchor_hours', 6)} 小时",
                 f"联系敏感度 {profile.get('loneliness', .5)}",
                 "同一来信沉默周期最多一次想念，planner可自主跳过",
                 self._mai_config_affection_line()]
        return ('\n' if multiline else ' | ').join(items)

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
         description="获取陪伴对象当前正在做什么（活动描述字符串，无活动或日程未启用返回空串）",
         version="1", public=True)
    async def mai_api_get_current_activity(self, **kwargs: Any) -> str:
        """当前活动描述（跨插件 API 面；UQ-5，用户 2026-10-27 批）。

        PR8 3.0.0（依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单，规则 6）：
        ``find_current_activity`` 已 runtime-only——**日程未启用**（``[schedule].enabled
        =false``）或 runtime 未就绪时返回**空串**（维持 v1「无活动返回空串」契约不变，
        不破坏跨插件空串判定），docstring 标注「日程未启用」；不读残留快照冒充当前活动。
        """
        schedule_query = self._mai_schedule_query()
        if schedule_query is None or not self._schedule_enabled():
            return ""  # 日程未启用：如实空串（UQ-5），不读残留快照冒充
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
                     "昨日 day_offset=1 行按同口径跨日合并，sleep 行过滤；"
                     "日程未启用返回空列表）",
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
        - 跨日合并：昨日日程里 ``day_offset=1`` 的行即本日真实午夜段（本 API 自持判定，
          按 ``ScheduleQuery._previous_date`` 取昨日帧；PR8 3.0.0 后 schedule_query 旧
          时间窗合并分支已随读表 fallback 删除）；今日自身 ``day_offset=1`` 行保留在
          列表末段（经环抬「次日」标注），两类帧行对同一环位互斥不重计；
        - 行过滤对齐 :func:`planning.schedule_query.SKIP_TYPES` 语义（该集合 = ``{"sleep"}``
          ——只滤睡眠行；``rest`` 等清醒行保留，与既有 SKIP_TYPES 契约零扩张）；
        - 排序列表 = 真实时间轴：昨日并入行（今日凌晨）→ 今日晨钟行 → 今日「次日」行，
          消除夜猫子日表「00:30 行排最前」的时序倒挂。

        PR8 3.0.0（UQ-7，依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单，规则 6）：
        **日程未启用**（``[schedule].enabled=false``）时如实返回**空列表**，不返回残留
        当日日表冒充日程仍在。
        """
        schedule_query = self._mai_schedule_query()
        if schedule_query is None or not self._schedule_enabled():
            return []  # 日程未启用：如实空列表（UQ-7），不返回残留日表
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
            # 昨日标记的「今晚午夜段」：今天就发生在 start..end 钟点区间（本 API 自持判定；
            # schedule_query 旧时间窗合并分支已随 PR8 读表 fallback 删除），
            # 按今日真实钟点展示、不环抬。
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

    # ==================================================================
    # /lbm_life rolling（滚动化调试命令；R7 依据 = 用户 2026-10-25 直派）
    # ==================================================================

    def _lbm_life_rolling(self, args: str) -> str:
        """`/lbm_life rolling` 只读诊断主体；实现见 companion/rolling_diag.py（P2-06）。"""
        return render_rolling(self, args)

    def _lbm_life_share(self, args: str) -> str:
        """`/lbm_life share` 视频分享运维主体；实现见 integrations/video_share_diag.py。"""
        return render_share(self, args)

    def _lbm_life_qzone_video(self, args: str) -> str:
        """`/lbm_life qzone_video` 空间视频运维主体（PR-A3）；实现见 integrations/qzone_video_diag.py。"""
        return render_qzone_video(self, args)
