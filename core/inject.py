"""planner / replyer 状态注入。

items 注入遵循**宿主的真实校验**（MaiBot ``ContextItemMeta`` 与
``validate_context_items``，payload_content/context_item.py、context_protocol.py）：
- item_type ∈ 受支持集合（这里用 SystemMessageItem）；
- meta：item_id **非空**且全局唯一（重复 item_id 宿主抛错；无「纯 hex」要求——
  本插件旁白条目用 ``companion-screen-`` 前缀 hex 是有意设计，便于识别与排查，
  见 companion/screen_context.build_narration_item）；timestamp 需可解析；
  logical_turn_id 可空（仅工具条目必须有，普通条目可沿用锚点值或置 None）；
- modified_kwargs 完整替换（**kwargs 展开），item_schema_version 原样带回。
不满足 → 宿主丢弃整份修改（表现就是"注入不生效"）。
（原注释自称遵循「开发文档 §9.7.1 强校验协议」，该文档原文已找不到；
以上按宿主源码逐条核对改写。）

兼容两条投影：1.2.x+ 的 items（ContextItem 快照）与旧 messages（role/content）。

另含 mai-love v2.6.2 合并的「过期宿主主动任务块剥离」（without_stale_proactive_blocks）：
宿主 enqueue_proactive_task 把 <plugin_proactive_task> 块持久化进聊天历史且从不清理，
planner/replyer 请求 Hook 按块内 id 尾段 epoch 判龄，把入队超过 30 分钟**且 id 的
plugin_id 段与本插件一致**的块从请求载荷剥离（只改本次请求，不回写宿主历史）。
F2 修复（2026-11-04）：此前 plugin_id 参数未参与比对，会误剥其他插件的过期块；
现在仅剥离本插件块，其他插件的块原样保留。

PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单）：M9 赖床提醒块剥离
（is_consumed_ephemeral_proactive_block / NAP_BLOCK_KIND / NAP_BLOCK_LINGER_SECONDS）
与旧文本构造器 build_activity_prompt / build_replyer_prompt 已删除——注入文本的
实装在 planning/inject_service.py（30 分钟过期规则保留不动）。
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 宿主主动任务块文本前缀（runtime.enqueue_proactive_task 生成）。
PROACTIVE_BLOCK_PREFIX = "<plugin_proactive_task"
#: 任务块入队多久后视为"过期"并从请求载荷剥离（毫秒）。任务入队后 planner 当场
#: 被唤醒，几分钟内必然消费完（含 wait() 等待也远小于 30 分钟），30 分钟足够保守。
PROACTIVE_BLOCK_STALE_MS = 30 * 60 * 1000
#: 主动任务块 id 的形态标记：``id="proactive:{plugin_id}:{epoch_ms}"``（附录 §一 F10）。
_PROACTIVE_ID_MARKER = 'id="proactive:'
#: 本插件在宿主侧的标识（_manifest.json 的 id）：块 id 里的 plugin_id 段据此比对。
CATEYE_PLUGIN_ID = "cateye.life_belonging_to_maimai"
# PR8 3.0.0：NAP_BLOCK_KIND / NAP_BLOCK_LINGER_SECONDS（M9 赖床提醒块剥离判据）随链删除。


def _first_text(item: Any) -> str:
    """取条目首段模型可见文本（快照 parts 优先，回退旧 role/content）。"""
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict) and str(part.get("type") or "") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    return text
        return ""
    content = item.get("content")
    return content if isinstance(content, str) else ""


def _proactive_block_parts(text: str) -> Optional[Tuple[str, int]]:
    """从主动块文本解析 ``(plugin_id, epoch_ms)``；解析不出来返回 None（宁多勿丢）。

    块 id 形态（F10）：``id="proactive:{plugin_id}:{epoch_ms}"``——取 id 引号内**最后一个**
    冒号后的数字段作 epoch，其前全部作为 plugin_id（plugin_id 自身含点号/连字符均无碍）。
    """
    start = text.find(_PROACTIVE_ID_MARKER)
    if start < 0:
        return None
    rest = text[start + len(_PROACTIVE_ID_MARKER):]
    end = rest.find('"')
    if end <= 0:
        return None
    plugin_part, sep, epoch_part = rest[:end].rpartition(":")
    if not sep or not epoch_part.strip().isdigit():
        return None
    try:
        return plugin_part, int(epoch_part.strip())
    except ValueError:
        return None


def is_stale_proactive_block(item: Any, now_ms: float,
                             plugin_id: Optional[str] = None) -> bool:
    """该条目是否为"入队超过 30 分钟"的宿主主动任务块。

    判据（全部满足才算）：UserMessageItem、首段文本以 ``<plugin_proactive_task``
    开头、能从 ``id="proactive:...:数字"`` 里解析出 epoch 毫秒、且
    ``now_ms - epoch > PROACTIVE_BLOCK_STALE_MS``。任何一步拿不准都返回
    False（宁多勿丢——误删本轮任务块会让 planner 失去触发上下文）。

    ``plugin_id`` 非空时追加归属判据：块 id 解析出的 plugin_id 段必须与之相等，
    只判本插件的块（F2，2026-11-04）；None/空 = 不限归属（旧口径，兼容直接调用方）。
    """
    if not isinstance(item, dict):
        return False
    if item.get("item_type") != "UserMessageItem":
        return False
    text = _first_text(item).lstrip()
    if not text.startswith(PROACTIVE_BLOCK_PREFIX):
        return False
    parts = _proactive_block_parts(text)
    if parts is None:
        return False
    if plugin_id and parts[0] != plugin_id:
        return False
    return now_ms - parts[1] > PROACTIVE_BLOCK_STALE_MS


def without_stale_proactive_blocks(kwargs: Dict[str, Any],
                                   plugin_id: str = CATEYE_PLUGIN_ID) -> Optional[Dict[str, Any]]:
    """从 Hook 请求载荷里移除过期的宿主主动任务块；无可移除内容时返回 None。

    只改**本次请求载荷**（与 filter-assistant 同思路，不回写宿主历史）：
    :func:`is_stale_proactive_block` 判定入队超过 :data:`PROACTIVE_BLOCK_STALE_MS`
    （30 分钟）**且块 id 的 plugin_id 段与 ``plugin_id`` 一致**的块剥离。
    （F2 修复，2026-11-04：此前 plugin_id 为死参数、任何插件的过期块都会被剥离；
    现在只剥离本插件块，与 :data:`CATEYE_PLUGIN_ID` 处注释声明的比对一致。）
    （PR8 3.0.0：原 M9 赖床提醒块剥离判定 is_consumed_ephemeral_proactive_block
    已随赖床提醒链删除；30 分钟过期规则原样保留。）

    本轮正要用的新块（年龄几分钟内）、解析失败的条目与其他插件的块一律保留。
    只处理 items 投影（1.2.x 快照；旧 messages 投影的宿主已不再使用，原样保留）。

    Returns:
        替换过 items 的新 kwargs；没有可剥离内容时 None（调用方保持原载荷）。
    """
    items = kwargs.get("items")
    if not isinstance(items, list) or not items:
        return None
    now_ms = time.time() * 1000
    kept: List[Any] = []
    removed = 0
    for item in items:
        if is_stale_proactive_block(item, now_ms, plugin_id=plugin_id):
            removed += 1
            continue
        kept.append(item)
    if removed == 0:
        return None
    logger.debug("已从请求载荷剥离 %d 个插件 %s 的过期主动任务块（保留 %d 条）",
                 removed, plugin_id, len(kept))
    return {**kwargs, "items": kept}


def _build_system_item_snapshot(text: str, template_meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    meta: Dict[str, Any] = dict(template_meta) if isinstance(template_meta, dict) else {}
    meta["item_id"] = uuid.uuid4().hex  # 必须换新，防撞 ID 导致整份丢弃
    meta["logical_turn_id"] = None  # 系统条目无逻辑轮次，键必须存在
    meta["timestamp"] = datetime.now().isoformat()
    return {"item_type": "SystemMessageItem", "meta": meta, "parts": [{"type": "text", "text": text}]}


def inject_into_items(items: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """把系统条目插到第一条 item 之后（无条目则直接作为首条）。"""
    new_item = _build_system_item_snapshot(text, items[0].get("meta") if items else None)
    return [items[0], new_item, *items[1:]] if items else [new_item]


def inject_into_items_after_system(items: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """把系统条目插到第一条 SystemMessageItem（系统提示词）之后。

    与 inject_into_items 的区别：显式定位第一条系统条目（而非假定 items[0]），
    没有系统条目时插到最前面。meta 协议同 §9.7.1（item_id 换新、logical_turn_id=None、
    timestamp 可解析）。
    """
    new_item = _build_system_item_snapshot(text, items[0].get("meta") if items else None)
    for idx, item in enumerate(items):
        item_type = str(item.get("item_type") or "")
        if item_type.endswith("SystemMessageItem") or str(item.get("role") or "") == "system":
            return [*items[: idx + 1], new_item, *items[idx + 1 :]]
    return [new_item, *items] if items else [new_item]


def inject_into_messages(messages: List[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    system_msg = {"role": "system", "content": text}
    if not messages:
        return [system_msg]
    for idx, msg in enumerate(messages):
        if msg.get("role") == "system":
            return [*messages[: idx + 1], system_msg, *messages[idx + 1 :]]
    return [messages[0], system_msg, *messages[1:]]


def extract_last_user_text(items: Optional[List[Dict[str, Any]]], messages: Optional[List[Dict[str, Any]]]) -> str:
    """从 payload 中提取最后一条真实用户文本（跳过系统元数据），供注入决策。"""

    def _clean(text: str) -> str:
        text = text or ""
        import re

        text = re.sub(r"^<message\s+[^>]*>\s*", "", text)
        stripped = text.strip()
        if not stripped:
            return ""
        if stripped.startswith(("当前时间：", "【人物画像-内部参考】", "<system-reminder>")):
            return ""
        return stripped

    def _item_role(item: Dict[str, Any]) -> str:
        item_type = str(item.get("item_type") or "")
        if item_type.endswith("UserMessageItem"):
            return "user"
        if item_type.endswith(("AssistantMessageItem", "SystemMessageItem")):
            return "assistant" if item_type.endswith("AssistantMessageItem") else "system"
        return str(item.get("role") or "")

    if items:
        for item in reversed(items):
            if _item_role(item) != "user":
                continue
            for part in item.get("parts") or []:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = _clean(str(part.get("text") or ""))
                    if text:
                        return text
    if messages:
        for msg in reversed(messages):
            if str(msg.get("role") or "") != "user":
                continue
            text = _clean(str(msg.get("content") or ""))
            if text:
                return text
    return ""


def project_injection(payload_key: str, payload: Any, text: str) -> Any:
    """按 payload 形态注入并返回新 payload（items / messages 双投影）。"""
    if payload_key == "items" and isinstance(payload, list):
        return inject_into_items(payload, text)
    if payload_key == "messages" and isinstance(payload, list):
        return inject_into_messages(payload, text)
    return payload


def pick_payload_key(kwargs: Dict[str, Any]) -> Tuple[str, Any]:
    """识别载荷形态：items 优先（1.2.x 快照），回退 messages。"""
    items = kwargs.get("items")
    if isinstance(items, list):
        return "items", items
    messages = kwargs.get("messages")
    if isinstance(messages, list):
        return "messages", messages
    return "", None
