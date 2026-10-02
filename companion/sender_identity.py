"""发送者身份缓存与 planner 前缀解析（来源：虚拟恋人插件 sender_identity.py，照搬）。

**为什么需要这个模块**：宿主写进 planner 上下文的真实聊天消息只有 ``msg_id`` 与显示名
（昵称 / 群名片），**没有 user_id**。而"这条消息是不是恋人发的"必须**按 QQ 号**判定——
昵称与群名片任何人都能改，按名字判定会出现「小美的**小号**」这种误命中，也会被改名冒充。

做法（与 cateye_admin_identity 同一套机制，已被线上验证）：

1. ``chat.receive.before_process``（入站链路，所有消息都经过）里记录两份缓存：
   「消息 ID → 发送者 QQ 号」(:class:`SenderCache`) 与「会话 ID → 是否群聊」
   (:class:`SessionKindCache`)；
2. planner 请求前，从条目文本里取出 ``msg_id``，反查发送者 QQ 号，按 QQ 号判定。

**查不到就当作"不是恋人"**（插件启动前的历史消息、缓存淘汰、通知消息等）：
宁可漏注入，也绝不靠名字兜底——与「身份只认 ID、名字只作展示」的原则一致。
"""

from __future__ import annotations

import re
import time
from typing import Any, List, Mapping, Optional, Sequence, Tuple

#: 平台前缀（``qq:123456`` → ``123456``）
_PLATFORM_PREFIXES = ("qq:", "wx:", "wechat:", "telegram:", "tg:", "discord:", "kook:")

#: planner 前缀必须锚定在文本开头（真实消息条目由宿主把前缀写在首段最前）
_PREFIX_LEAD_RE = re.compile(r"^[ \t]*(<message\b[^>]*>)")
_ATTR_MSG_ID_RE = re.compile(r'\bmsg_id="([^"]*)"')
#: 说话人可见文本格式（兜底提取 msg_id，理论上不出现在请求条目里）
_SPEAKER_MSG_ID_RE = re.compile(r"\[msg_id:([^\]]+)\]")

#: 发送者缓存默认参数
SENDER_CACHE_MAX_SIZE = 4096
SENDER_CACHE_TTL_SEC = 24 * 3600.0
#: 会话类型缓存默认参数（类型不会变化，只做容量上限，不设 TTL）
SESSION_CACHE_MAX_SIZE = 1024


def qq_part(value: Any) -> str:
    """取 ID 部分：剥离平台前缀（``qq:123456`` → ``123456``）。"""
    text = str(value or "").strip()
    lowered = text.lower()
    for prefix in _PLATFORM_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


# ==================== 缓存 ====================


class SenderCache:
    """入站消息的 ``message_id → 发送者 QQ 号`` 缓存（TTL + 容量上限）。

    局限（表现为"判定为不是恋人"，不会误判为是）：
    - 插件启动前就已在上下文里的历史消息查不到；
    - 超过 TTL 或被容量淘汰的消息查不到；
    - 宿主未把 ``msg_id`` 写进前缀的消息（如通知类）查不到。
    """

    def __init__(
        self,
        *,
        max_size: int = SENDER_CACHE_MAX_SIZE,
        ttl_sec: float = SENDER_CACHE_TTL_SEC,
    ) -> None:
        self.max_size = max(1, int(max_size))
        self.ttl_sec = max(1.0, float(ttl_sec))
        #: message_id -> (user_id, monotonic 记录时间)；dict 保持插入序便于淘汰最旧
        self._data: dict[str, Tuple[str, float]] = {}

    def record(self, message_id: Any, user_id: Any, *, now: Optional[float] = None) -> None:
        """记录一条「消息 ID → 发送者 QQ 号」；任一为空时忽略。"""
        mid = str(message_id or "").strip()
        uid = qq_part(user_id)
        if not mid or not uid:
            return
        current = time.monotonic() if now is None else float(now)
        if mid not in self._data and len(self._data) >= self.max_size:
            self._data.pop(next(iter(self._data)), None)
        self._data[mid] = (uid, current)

    def get_user_id(self, message_id: Any, *, now: Optional[float] = None) -> str:
        """按消息 ID 反查发送者 QQ 号；不存在或已过期返回空串（过期项顺带清除）。"""
        mid = str(message_id or "").strip()
        if not mid:
            return ""
        entry = self._data.get(mid)
        if entry is None:
            return ""
        user_id, recorded_at = entry
        current = time.monotonic() if now is None else float(now)
        if current - recorded_at > self.ttl_sec:
            self._data.pop(mid, None)
            return ""
        return user_id

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


class SessionKindCache:
    """入站消息的 ``session_id → 是否群聊`` 缓存（仅容量上限）。

    会话类型不会变化，因此不设 TTL；只保留最近 ``max_size`` 个会话。
    局限：插件启动/重载后、该会话还没有新消息流入时类型未知（``None``），
    群聊相关判定按"不生效"处理，随消息流入自动补齐。
    """

    def __init__(self, *, max_size: int = SESSION_CACHE_MAX_SIZE) -> None:
        self.max_size = max(1, int(max_size))
        #: session_id -> is_group
        self._data: dict[str, bool] = {}

    def record(self, session_id: Any, is_group: Any) -> None:
        """记录一条「会话 ID → 是否群聊」；参数不合法时忽略。"""
        sid = str(session_id or "").strip()
        if not sid or not isinstance(is_group, bool):
            return
        if sid not in self._data and len(self._data) >= self.max_size:
            self._data.pop(next(iter(self._data)), None)
        self._data[sid] = is_group

    def is_group(self, session_id: Any) -> Optional[bool]:
        """查询会话是否群聊；未知返回 ``None``。"""
        sid = str(session_id or "").strip()
        if not sid:
            return None
        return self._data.get(sid)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


# ==================== 入站消息提取 ====================


def extract_user_id_from_message(message: Any) -> str:
    """从入站消息 Hook 载荷提取发送者 QQ 号；取不到返回空串。

    ``chat.receive.before_process`` 的 message 载荷结构与宿主
    ``PluginMessageUtils._session_message_to_dict`` 对齐：
    ``message["message_info"]["user_info"]["user_id"]``。
    """
    if not isinstance(message, Mapping):
        return ""
    message_info = message.get("message_info")
    if isinstance(message_info, Mapping):
        user_info = message_info.get("user_info")
        if isinstance(user_info, Mapping):
            user_id = qq_part(user_info.get("user_id"))
            if user_id:
                return user_id
    # 兜底：部分路径可能直接给 user_id
    return qq_part(message.get("user_id"))


def extract_session_info_from_message(message: Any) -> Tuple[str, Optional[bool]]:
    """从入站消息 Hook 载荷提取 ``(session_id, is_group)``。

    会话类型看 ``message["message_info"]["group_info"]``：宿主序列化时**总会带上这个键**，
    群聊是 ``{"group_id": …}``、私聊是 ``None``。因此：

    - ``group_info`` 是 Mapping → 群聊（``True``）；
    - ``group_info`` 是 ``None`` → 私聊（``False``）；
    - **整键缺失/类型异常** → 类型未知（``None``）——不猜，让调用方按"不注入"处理。
    """
    if not isinstance(message, Mapping):
        return "", None

    session_id = str(message.get("session_id") or "").strip()
    is_group: Optional[bool] = None

    message_info = message.get("message_info")
    if isinstance(message_info, Mapping) and "group_info" in message_info:
        group_info = message_info.get("group_info")
        if isinstance(group_info, Mapping):
            is_group = True
        elif group_info is None:
            is_group = False

    if is_group is None and str(message.get("group_id") or "").strip():
        # 兜底：部分路径可能直接给群号
        is_group = True
    return session_id, is_group


# ==================== 上下文条目解析 ====================


def first_text_part(item: Any) -> str:
    """取条目的第一段模型可见文本（planner 前缀写在第一个文本 part 的开头）。"""
    if not isinstance(item, Mapping):
        return ""
    parts = item.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, Mapping) and str(part.get("type") or "") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
            # 图片等非文本段只可能出现在文本段之后
            return ""
        return ""
    content = item.get("content")
    return content if isinstance(content, str) else ""


def message_id_from_item(item: Any) -> str:
    """从上下文条目里取消息 ID（宿主写在 planner 前缀 ``msg_id="…"`` 里）。

    兼容两种投影：ContextItem 快照（``item_type`` + ``parts``）与旧
    ``role/content``。旧投影里没有前缀，返回空串。
    """
    text = first_text_part(item)
    if not text:
        return ""
    lead = _PREFIX_LEAD_RE.match(text)
    if lead:
        match = _ATTR_MSG_ID_RE.search(lead.group(1))
        if match and match.group(1).strip():
            return match.group(1).strip()
    # 兜底：可见文本格式 [msg_id:xxx]
    speaker = _SPEAKER_MSG_ID_RE.search(text)
    if speaker and speaker.group(1).strip():
        return speaker.group(1).strip()
    return ""


def is_user_message_item(item: Any) -> bool:
    """判断条目是否为用户消息（兼容快照与旧 ``role/content`` 投影）。"""
    if not isinstance(item, Mapping):
        return False
    item_type = item.get("item_type")
    if isinstance(item_type, str) and item_type:
        return item_type == "UserMessageItem"
    return str(item.get("role") or "") == "user"


def sender_ids_in_window(
    items: Sequence[Any],
    cache: SenderCache,
    window: int,
) -> List[str]:
    """取上下文里**最后 ``window`` 条用户消息**的发送者 QQ 号。

    反查不到的元素为空串（调用方按"不是目标用户"处理）。
    """
    sender_ids: List[str] = []
    for item in items or ():
        if not is_user_message_item(item):
            continue
        sender_ids.append(cache.get_user_id(message_id_from_item(item)))
    limit = int(window or 0)
    if limit > 0:
        sender_ids = sender_ids[-limit:]
    return sender_ids
