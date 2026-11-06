"""Legacy 3.6.1 isolated compatibility; not used by the current plugin.

The current ManualVideoShares uses the shared SQLite ledger and raw card sender.
The old JSON receipts below are migration inputs; send.custom is not a live path.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any


async def send_card_json(sender: Any, stream: str, ark: str, description: str) -> dict:
    """宿主 SDK custom(custom_type, data) / SendResult(sent) 的适配。"""
    result = await sender.custom(custom_type="json", data=ark, stream_id=stream,
        return_details=True, processed_plain_text=description,
        storage_message=True, sync_to_maisaka_history=True)
    return {"success": isinstance(result, dict) and result.get("sent") is True,
            "message_id": result.get("message_id") if isinstance(result, dict) else None}


class RecentVideoShare:
    def __init__(self, data_dir: Any, *, recent_provider: Any, card_generator: Any,
                 send_json: Any, now_provider: Any = time.time, chooser: Any = random.choice):
        self._path = Path(data_dir) / "recent_video_share_receipts.json"
        self._recent = recent_provider
        self._card = card_generator
        self._send = send_json
        self._now = now_provider
        self._choose = chooser
        self._lock = asyncio.Lock()
        self._receipts = {}
        self._available = True
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._receipts = {k: v for k, v in raw.get("receipts", {}).items()
                              if isinstance(v, dict)}
            if len(self._receipts) > 200:
                raise ValueError("回执容量异常")
            for item in self._receipts.values():
                float(item.get("at", 0))
            for item in self._receipts.values():
                if item.get("state") == "sending":
                    item["state"] = "uncertain"
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self._available = False

    def _save(self) -> None:
        now = self._now()
        self._receipts = {k: v for k, v in self._receipts.items()
                          if 0 <= now-float(v.get("at", 0)) < 15*86400}
        if len(self._receipts) > 200:
            raise OSError("分享回执已满，保留防重状态，不继续发送")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name+".tmp")
        if self._path.is_symlink() or tmp.is_symlink():
            raise OSError("分享回执路径不可用")
        tmp.write_text(json.dumps({"version": 1, "receipts": self._receipts}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    @staticmethod
    def _result(item: dict, *, duplicate: bool = False) -> dict:
        accepted = item.get("state") == "accepted"
        return {"success": accepted, "bvid": item.get("bvid"), "title": item.get("title"),
                "delivery_state": item.get("state"), "duplicate": duplicate,
                "message_id": item.get("message_id"), "stop_after_execution": False,
                "content": (("本次已经处理过，不重复发送。" if duplicate else "") +
                    (f"已向当前聊天提交视频卡片：《{item.get('title')}》 {item.get('bvid')}。"
                     "如需了解实际内容，可用 lbm_watch_bili_video 的 submit 传该 BV，优先读取观看缓存；不必重新观看。"
                     if accepted else "发送结果不确定，未重试；不要声称已送达或再次调用工具补发。"))}

    async def share(self, stream_id: str, request_id: str = "", *, preferred_bvid: str = "") -> dict:
        stream_id = str(stream_id or "").strip()
        if not stream_id:
            return {"success": False, "error_class": "missing_current_stream", "content": "无法确认当前聊天，不发送。"}
        scope = hashlib.sha256(stream_id.encode()).hexdigest()
        request_key = hashlib.sha256((stream_id+"|"+request_id).encode()).hexdigest() if request_id else ""
        async with self._lock:
            if not self._available:
                return {"success": False, "error_class": "receipt_unavailable", "content": "分享记录不可用，本次不发送以免重复。"}
            if request_key and request_key in self._receipts:
                return self._result(self._receipts[request_key], duplicate=True)
            now = self._now()
            self._save()
            if len(self._receipts) >= 200:
                return {"success": False, "error_class": "receipt_full", "content": "分享回执容量已满，本次不发送。"}
            latest = [v for v in self._receipts.values() if v.get("scope") == scope]
            recent = [v for v in latest if 0 <= now-float(v.get("at", 0)) < 120]
            if recent:
                return self._result(max(recent, key=lambda v: v.get("at", 0)), duplicate=True)
            history = self._recent()
            pool, seen = [], set()
            for row in history or []:
                if not isinstance(row, dict):
                    continue
                bvid = str(row.get("bvid") or "")
                title = str(row.get("title") or "").strip()[:200]
                if not re.fullmatch(r"BV[A-Za-z0-9]{10}", bvid) or not title or bvid in seen:
                    continue
                if any(v.get("bvid") == bvid and (v.get("state") == "uncertain"
                    or 0 <= now-float(v.get("at", 0)) < 21600) for v in latest):
                    continue
                seen.add(bvid);pool.append({"bvid": bvid, "title": title})
            if not pool:
                return {"success": False, "error_class": "no_recent_video", "content": "最近没有可分享且有观看记忆的视频，不搜索、不编造。"}
            if preferred_bvid:
                pool = [row for row in pool if row['bvid'] == preferred_bvid]
                if not pool:
                    return {'success': False, 'error_class': 'not_in_recent_memory',
                            'content': '这个BV不在可分享的近期观看记忆中，不搜索、不编造。'}
            selected = self._choose(pool)
            generated = await self._card(**selected)
            if not isinstance(generated, dict) or not generated.get("success"):
                return {"success": False, "error_class": "card_generation_failed", "content": "视频卡片生成失败，未发送。"}
            key = request_key or hashlib.sha256((stream_id+"|"+selected["bvid"]).encode()).hexdigest()
            item = {**selected, "scope": scope, "state": "sending", "at": now}
            self._receipts[key] = item
            self._save()  # 必须先落盘；失败时不得发送。
            try:
                result = await self._send(stream_id, generated["ark_json"],
                    f"[视频分享] {selected['title']} {selected['bvid']}")
                if isinstance(result, dict) and result.get("success") is True:
                    item["state"] = "accepted"
                    item["message_id"] = result.get("message_id")
                else:
                    item["state"] = "uncertain"
            except asyncio.CancelledError:
                item["state"] = "uncertain"
                self._save()
                raise
            except Exception:
                item["state"] = "uncertain"
            try:
                self._save()
            except OSError:
                # 发送前的 sending 回执已持久化，重启会按 uncertain 阻止补发。
                item["receipt_persisted"] = False
            return self._result(item)
