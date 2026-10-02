"""恋人电脑屏幕感知的「尾随锚点」上下文注入（来源：虚拟恋人插件 screen_context.py）。

**为什么不用 ``ctx.maisaka.context.append`` 一次性追加**：

宿主对私聊流的身份判定是「发送者决定流」，想以 **bot 身份**入库一条消息会算出
「bot 跟自己聊天」的幽灵流；``is_notify=True`` 的记录会被宿主恢复上下文时跳过；
``maisaka.context.append`` 则是**持久**追加，会长期占一个上下文槽位。

**这里改用「尾随锚点」**（更贴合"感知旁白"的语义，且不污染宿主历史）：

1. 触发主动私聊时先把屏幕转述写进本模块的缓存 (:meth:`ScreenContextStore.publish`)；
2. 之后**每一轮** planner / replyer 请求，都在请求载荷 ``items`` 里定位「锚点」——
   即触发那一刻上下文里**最后一条真实聊天消息**（用户或 bot 发的，不含插件注入块）；
3. 找到就把旁白作为一条 **AssistantMessageItem**（bot 身份）插到它**后面**；
4. 某轮在该通道的上下文里找不到锚点了（一般是超出了上下文条数），
   就**只废弃该通道**（planner / replyer 分开判定），两个通道都废弃后整条缓存清除。

旁白是**请求级**的：只存在于当次 LLM 请求的载荷里，不写消息库、不真发到平台、
也不落进宿主的历史。注入作用域由调用方（components.py 的 Hook）保证：只有
session_id 命中恋人私聊流时才调用本模块，群聊与其他会话一律不注入。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional, Tuple
from uuid import uuid4

from .config import SCREEN_NARRATION_TEMPLATE_DEFAULT
from .sender_identity import first_text_part, message_id_from_item

#: 通道名：planner 与 replyer 的上下文分开识别（两者条目不一定相同）
CHANNEL_PLANNER = "planner"
CHANNEL_REPLYER = "replyer"
CHANNELS = (CHANNEL_PLANNER, CHANNEL_REPLYER)

#: 本模块注入的旁白条目 item_id 前缀（用于识别并跳过自己注入的内容）
NARRATION_ITEM_ID_PREFIX = "companion-screen-"

#: 宿主插件主动任务块的开头（runtime.enqueue_proactive_task 生成），不算真实消息
HOST_PROACTIVE_BLOCK_PREFIX = "<plugin_proactive_task"

#: 同时最多跟踪多少条流的待注入旁白（防止异常情况下缓存无限增长）
MAX_PENDING_STREAMS = 8


# ---------------------------------------------------------------------------
# 文案
# ---------------------------------------------------------------------------


def format_narration(
    template: str,
    *,
    now: datetime,
    user_name: str,
    description: str = "",
    default_template: str = "",
) -> str:
    """按模板拼出旁白文本；模板为空或占位符坏掉时回退到该语义的内置默认文案。

    可用占位符：``{date}``（年月日）/ ``{time}``（时分）/ ``{user_name}`` /
    ``{description}``（视觉模型转述）。
    """
    fields = {
        "date": f"{now.year}年{now.month}月{now.day}日",
        "time": now.strftime("%H:%M"),
        "user_name": user_name or "恋人",
        "description": str(description or "").strip(),
    }

    def _render(candidate: str) -> str:
        """渲染模板；空模板/坏占位符返回空串。"""
        text = str(candidate or "").strip()
        if not text:
            return ""
        try:
            return str(candidate).format(**fields)
        except Exception:  # 占位符被改坏时不打断触发
            return ""

    # 用户模板坏掉（含空模板）时用该语义的内置默认文案，绝不返回空串——
    # 空串会让 publish 静默失败，等于把功能关掉。
    return _render(template) or _render(default_template or SCREEN_NARRATION_TEMPLATE_DEFAULT)


# ---------------------------------------------------------------------------
# 上下文条目工具
# ---------------------------------------------------------------------------


def _item_id_of(item: Any) -> str:
    """取条目快照 ``meta.item_id``（宿主为每条上下文消息生成的稳定 ID）。"""
    if not isinstance(item, Mapping):
        return ""
    meta = item.get("meta")
    if isinstance(meta, Mapping):
        value = meta.get("item_id")
        if isinstance(value, str):
            return value.strip()
    return ""


def _logical_turn_id_of(item: Any) -> Optional[str]:
    """取条目 ``meta.logical_turn_id``（构造新条目时沿用锚点的，保持同一逻辑轮次）。"""
    if not isinstance(item, Mapping):
        return None
    meta = item.get("meta")
    if isinstance(meta, Mapping):
        value = meta.get("logical_turn_id")
        if isinstance(value, str) and value.strip():
            return value
    return None


def item_identity(item: Any) -> Tuple[str, str]:
    """取条目的身份标识 ``(item_id, msg_id)``，任一取不到为空串。

    - ``item_id``：宿主快照 ``meta.item_id``，对同一条上下文消息**跨请求稳定**；
    - ``msg_id``：宿主写在用户消息前缀里的平台消息 ID，**跨宿主重启仍有效**，
      但 bot 自己的消息条目没有。两者都记：命中任意一个即认为是同一条消息。
    """
    return (_item_id_of(item), message_id_from_item(item))


def _is_real_chat_item(item: Any) -> bool:
    """是否是"真实聊天消息"条目（可作为锚点）。

    排除：
    - 非 user/assistant 条目（system / 工具调用 / 工具结果 / 推理 / 参考消息等）；
    - **没有消息前缀的 user 条目**——宿主每轮都会新建一批「合成 user 条目」
      （当前时间、planner 最终提醒、replyer 回复要求等），它们 item_id 每轮都是
      新 uuid，一旦被选成锚点，下一轮就找不回来 → 旁白只会注入一轮。真实用户
      消息由宿主写成 ``<message msg_id="…">正文``，用「有没有消息 ID」区分；
    - 宿主插件主动任务块（``<plugin_proactive_task>``，是插件指令不是聊天）；
    - 本模块自己注入的旁白条目；
    - 文本为空的条目。
    """
    if not isinstance(item, Mapping):
        return False
    item_type = item.get("item_type")
    if isinstance(item_type, str) and item_type:
        if item_type not in ("UserMessageItem", "AssistantMessageItem"):
            return False
        is_user = item_type == "UserMessageItem"
    else:
        # 旧 role/content 投影：只认 user / assistant
        role = str(item.get("role") or "")
        if role not in ("user", "assistant"):
            return False
        is_user = role == "user"

    if _item_id_of(item).startswith(NARRATION_ITEM_ID_PREFIX):
        return False

    text = first_text_part(item).lstrip()
    if not text:
        return False
    if text.startswith(HOST_PROACTIVE_BLOCK_PREFIX):
        return False
    if is_user and not message_id_from_item(item):
        return False
    return True


def find_last_real_item_index(items: Iterable[Any]) -> Optional[int]:
    """取上下文里最后一条真实聊天消息的下标；没有则 None。"""
    found: Optional[int] = None
    for index, item in enumerate(items or ()):
        if _is_real_chat_item(item):
            found = index
    return found


def find_item_index_by_identity(
    items: Iterable[Any], item_id: str, msg_id: str
) -> Optional[int]:
    """按 ``(item_id, msg_id)`` 找回锚点下标；命中任意一个即可，找不到返回 None。"""
    for index, item in enumerate(items or ()):
        current_item_id, current_msg_id = item_identity(item)
        if item_id and current_item_id == item_id:
            return index
        if msg_id and current_msg_id == msg_id:
            return index
    return None


def build_narration_item(text: str, anchor: Any, *, now: datetime) -> dict[str, Any]:
    """构造旁白条目快照（以 **bot 身份**呈现的 AssistantMessageItem）。

    沿用锚点的 ``logical_turn_id``，避免把旁白挂到另一个逻辑轮次上；
    ``item_id`` 用本模块前缀，便于识别与排查。
    """
    return {
        "item_type": "AssistantMessageItem",
        "meta": {
            "item_id": f"{NARRATION_ITEM_ID_PREFIX}{uuid4().hex}",
            "logical_turn_id": _logical_turn_id_of(anchor),
            "timestamp": now.astimezone().isoformat(),
        },
        "parts": [{"type": "text", "text": text}],
    }


# ---------------------------------------------------------------------------
# 缓存
# ---------------------------------------------------------------------------


@dataclass
class _ChannelState:
    """单个通道（planner / replyer）的锚点状态。"""

    #: 是否已经锁定锚点（首轮请求时锁定）
    anchored: bool = False
    #: 锚点的 item_id（跨请求稳定）
    anchor_item_id: str = ""
    #: 锚点的平台 msg_id（跨重启有效，bot 消息为空）
    anchor_msg_id: str = ""
    #: 该通道的锚点已从上下文消失 → 该通道作废，不再注入
    dead: bool = False
    #: 实际注入过的请求轮数（供日志排查）
    injected_rounds: int = 0

    def lock(self, item: Any) -> None:
        """锁定锚点。"""
        self.anchor_item_id, self.anchor_msg_id = item_identity(item)
        self.anchored = True


@dataclass
class _Pending:
    """一条流上待注入的旁白。"""

    stream_id: str
    text: str
    created_monotonic: float
    planner: _ChannelState = field(default_factory=_ChannelState)
    replyer: _ChannelState = field(default_factory=_ChannelState)

    def channel(self, name: str) -> Optional[_ChannelState]:
        """按通道名取状态。"""
        if name == CHANNEL_PLANNER:
            return self.planner
        if name == CHANNEL_REPLYER:
            return self.replyer
        return None

    def all_dead(self) -> bool:
        """两个通道是否都已作废。"""
        return self.planner.dead and self.replyer.dead


class ScreenContextStore:
    """待注入的屏幕旁白缓存（按流 + 按通道维护锚点）。"""

    def __init__(self, ttl_minutes: float = 90.0) -> None:
        """
        Args:
            ttl_minutes: 旁白存活上限（分钟）。锚点还在上下文里也不超过这个时长，
                避免几小时后还把"TA 正在写代码"当成现在的事；``<=0`` 表示不限时。
        """
        self.ttl_seconds: float = max(0.0, float(ttl_minutes)) * 60.0
        self._pending: dict[str, _Pending] = {}

    # ── 写入 ──────────────────────────────────────────────────────────

    def publish(
        self, stream_id: str, text: str, *, now: Optional[float] = None
    ) -> bool:
        """发布一条待注入旁白（同一流上已有的会被覆盖）。"""
        sid = str(stream_id or "").strip()
        content = str(text or "").strip()
        if not sid or not content:
            return False
        if sid not in self._pending and len(self._pending) >= MAX_PENDING_STREAMS:
            # 淘汰最旧的一条，避免异常情况下无限增长
            oldest = min(self._pending.items(), key=lambda kv: kv[1].created_monotonic)[0]
            self._pending.pop(oldest, None)
        current = time.monotonic() if now is None else float(now)
        self._pending[sid] = _Pending(stream_id=sid, text=content, created_monotonic=current)
        return True

    def discard(self, stream_id: Optional[str] = None) -> None:
        """清除待注入旁白；``stream_id`` 为 None 时清空全部。"""
        if stream_id is None:
            self._pending.clear()
            return
        self._pending.pop(str(stream_id or "").strip(), None)

    def has_pending(self, stream_id: str) -> bool:
        """该流是否还有待注入旁白（供日志/诊断）。"""
        return str(stream_id or "").strip() in self._pending

    # ── 读取（Hook 侧调用）────────────────────────────────────────────

    def take_injection(
        self,
        stream_id: str,
        channel: str,
        items: Any,
        *,
        now: Optional[float] = None,
    ) -> Optional[Tuple[int, str]]:
        """在本次请求的 ``items`` 里定位锚点，返回旁白应插入的位置与文本。

        Returns:
            ``(插入下标, 旁白文本)``；``None`` 表示本次不注入
            （没有待注入内容 / 该通道已作废 / 本轮上下文里还没有可作锚点的真实消息）。
        """
        sid = str(stream_id or "").strip()
        pending = self._pending.get(sid)
        if pending is None:
            return None

        current = time.monotonic() if now is None else float(now)
        if self.ttl_seconds > 0 and current - pending.created_monotonic > self.ttl_seconds:
            self._pending.pop(sid, None)
            return None

        state = pending.channel(channel)
        if state is None or state.dead:
            return None

        if not state.anchored:
            anchor_index = find_last_real_item_index(items)
            if anchor_index is None:
                # 本轮上下文里还没有真实消息（例如刚建流）：下一轮再试
                return None
            state.lock(items[anchor_index])
        else:
            anchor_index = find_item_index_by_identity(
                items, state.anchor_item_id, state.anchor_msg_id
            )
            if anchor_index is None:
                # 锚点已滑出该通道的上下文 → 该通道作废
                state.dead = True
                if pending.all_dead():
                    self._pending.pop(sid, None)
                return None

        state.injected_rounds += 1
        return anchor_index + 1, pending.text

    # ── 诊断 ──────────────────────────────────────────────────────────

    def describe(self, stream_id: str) -> dict[str, Any]:
        """返回某条流待注入旁白的诊断快照（供 /mai_diag 之类排查）。"""
        pending = self._pending.get(str(stream_id or "").strip())
        if pending is None:
            return {}
        return {
            "text": pending.text,
            "planner": {
                "anchored": pending.planner.anchored,
                "dead": pending.planner.dead,
                "rounds": pending.planner.injected_rounds,
            },
            "replyer": {
                "anchored": pending.replyer.anchored,
                "dead": pending.replyer.dead,
                "rounds": pending.replyer.injected_rounds,
            },
        }

    def __len__(self) -> int:
        """当前跟踪的流数量。"""
        return len(self._pending)
