"""Requested tool-result delivery to its original stream, independent of social sharing."""
from __future__ import annotations

import asyncio
import json
import math
import os
import time
from pathlib import Path


class CompletionOutbox:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()
        self.blocked = False
        try:
            self.rows = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else []
            if not isinstance(self.rows, list) or any(not isinstance(r, dict) for r in self.rows):
                raise ValueError("invalid outbox")
            if any(not isinstance(r.get("task_id"), str) or not r["task_id"]
                   or not isinstance(r.get("stream_id"), str) or not r["stream_id"]
                   or r.get("state") not in ("pending", "dispatching", "enqueued", "unknown", "rejected", "expired")
                   or isinstance(r.get("created_at"), bool)
                   or not isinstance(r.get("created_at"), (int, float))
                   or not math.isfinite(r["created_at"]) for r in self.rows):
                raise ValueError("invalid subscription")
        except (ValueError, OSError):
            self.rows = [];self.blocked = True
        changed = False
        for row in self.rows:
            if row.get("state") == "dispatching":
                row["state"] = "unknown";changed = True
        if changed:self._save()

    def _save(self):
        if self.blocked:raise RuntimeError("完成通知记录异常，保留原文件，停止自动通知")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(self.rows, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.path)

    def subscribe(self, task_id: str, stream_id: str) -> bool:
        if self.blocked or not task_id or not stream_id:return False
        if any(r.get("task_id") == task_id and r.get("stream_id") == stream_id for r in self.rows):return True
        if len(self.rows) >= 100:
            self.rows = [r for r in self.rows if r.get("state") in ("pending", "dispatching")]
        if len(self.rows) >= 100:return False
        self.rows.append({"task_id": task_id, "stream_id": stream_id, "state": "pending", "created_at": time.time()})
        self._save();return True

    async def flush(self, status, result, notify):
        if self.blocked:return
        async with self._lock:
            for row in self.rows:
                if row.get("state") != "pending":continue
                if time.time() - row["created_at"] > 86400:
                    row["state"] = "expired";self._save();continue
                task = status(row["task_id"])
                if task.get("status") not in ("completed", "failed", "cancelled"):continue
                receipt = result(row["task_id"])
                row["state"] = "dispatching";self._save()
                try:
                    response = await asyncio.wait_for(notify(row["task_id"], row["stream_id"], receipt), 20)
                    if isinstance(response, dict) and response.get("deferred") is True:
                        # 睡眠门控暂缓投递（2026-11-04 修复4）：保留 pending、不落 finished_at，
                        # 下一次 flush（醒沿/completion tick）重检再投；不丢通知、不重复投递。
                        row["state"] = "pending";self._save();continue
                    row["state"] = ("enqueued" if isinstance(response, dict) and response.get("success") is True
                                    else "rejected" if isinstance(response, dict) and response.get("success") is False else "unknown")
                except asyncio.CancelledError:
                    row["state"] = "unknown";self._save();raise
                except Exception:row["state"] = "unknown"
                row["finished_at"] = time.time();self._save()
