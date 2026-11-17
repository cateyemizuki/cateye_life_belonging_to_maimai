"""稳定外部事件回执与冻结队列的同文件快照；不改变普通 event_id 留痕语义。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Any

from .vad import EmotionEvent

STATE_KEY = "external_event_receipts"


def restore(machine: Any, document: Any) -> dict:
    """只有持有稳定排队事实时恢复冻结态，旧文件仍由睡眠循环判断。"""
    raw = document.get(STATE_KEY) if isinstance(document, dict) else None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return {}
    receipts = deepcopy(raw.get("receipts")) if isinstance(raw.get("receipts"), dict) else {}
    if raw.get("frozen") and any(isinstance(value, dict) and value.get("state") == "queued"
                                 for value in receipts.values()):
        try:
            frozen_at = datetime.fromisoformat(str(raw.get("frozen_at")))
        except (ValueError, TypeError):
            frozen_at = datetime.now()
        machine.freeze(frozen_at)
        machine.pending_events = []
        for item in raw.get("pending_events") or []:
            if not isinstance(item, dict):
                continue
            event = EmotionEvent.from_dict(item)
            event._external_receipt_key = str(item.get("idempotency_key") or "")
            machine.pending_events.append(event)
        protect_pending(machine, receipts)
    return receipts


def protect_pending(machine: Any, receipts: dict) -> None:
    """普通队列的容量裁剪不能挤掉已经确认的稳定排队事件。"""
    if not machine.frozen:
        return
    present = {getattr(event, "_external_receipt_key", "") for event in machine.pending_events}
    for key, receipt in receipts.items():
        if (key in present or not isinstance(receipt, dict) or receipt.get("state") != "queued"
                or not isinstance(receipt.get("event"), dict)):
            continue
        event = EmotionEvent.from_dict(receipt["event"])
        event._external_receipt_key = key
        machine.pending_events.append(event)


def snapshot(machine: Any, receipts: dict) -> dict:
    """不提前消费内存回执；调用者只在原子写盘成功后采用返回的 receipts。"""
    protect_pending(machine, receipts)
    saved = deepcopy(receipts)
    if not machine.frozen:
        for receipt in saved.values():
            if isinstance(receipt, dict) and receipt.get("state") == "queued":
                # 既有睡眠语义：夜间事件解冻后只并入日志，不重复加 VAD。
                receipt["state"] = "drained"
                receipt.pop("event", None)
    pending = []
    if machine.frozen:
        for event in machine.pending_events:
            item = event.to_dict()
            key = getattr(event, "_external_receipt_key", "")
            if key:
                item["idempotency_key"] = key
            pending.append(item)
    frozen_at = getattr(machine, "_frozen_at", None)
    return {"version": 1, "receipts": saved, "frozen": bool(machine.frozen),
            "frozen_at": frozen_at.isoformat() if frozen_at else "", "pending_events": pending}
