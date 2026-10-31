"""对话忙闲事实与自动注意力租约（冲浪生活计划书 §4，S1；companion 所有）。

职责边界（计划书 §8：companion 持有交流忙闲事实；integrations/video_schedule
持注意力门控与额度；本模块不做日程判断、不选片、不调模型、不发消息）：

- :class:`ConversationActivityTracker`：收消息/回复前后 hook 喂事实，产出
  只读忙闲快照。判定口径（计划书 §4.1）：在途回复、待答直接消息、最近
  实际参与对话 3 分钟内 = 忙；普通群里别人彼此聊天不算忙；状态过期或
  观测不确定 → 快照按「不确定」返回，消费方不得据此启动自动观看。
- :class:`SurfLeaseAuthority`：进程内单一注意力权威（装配根注入，无全局
  单例）。SURF（自动冲浪）租约同一时刻至多一个持有者；直接对话到来立即
  撤销 SURF 让位 CHAT；版本号随每次迁移递增，消费方在每次模型调用前核对。

租约是运行态：停机/重载即消失，重启后保守恢复（不自动续租）；额度窗口/
冷却由 integrations/video_schedule 持久化（计划书 §3.3），不在本模块。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

#: 最近实际参与对话的静默窗（秒）：不足即视为忙（计划书 §4.1 建议 3 分钟）
CHAT_SILENCE_SECONDS = 180.0
#: 在途回复的自动过期（秒）：hook 丢失 end 事件时自愈，避免永久忙
REPLY_INFLIGHT_TTL_SECONDS = 120.0


def _now(clock: Any = None) -> float:
    return float(clock() if callable(clock) else time.time())


class ConversationActivityTracker:
    """对话忙闲事实单写者：hook 喂事件，snapshot() 供注意力门控只读消费。"""

    def __init__(self, clock: Any = None) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._last_incoming: Dict[str, float] = {}      # stream_id -> 最近入站
        self._last_direct: Dict[str, float] = {}        # 直接待答（私聊/@/点名）入站
        self._last_participation: float = 0.0           # 最近实际参与（入站直接或我方回复）
        self._reply_inflight: Dict[str, float] = {}     # stream_id -> 回复开始时刻
        self._last_event: float = 0.0                   # 覆盖面暖机判定

    # ---- hook 喂入（全部吞异常，绝不影响消息处理）----
    def note_incoming(self, stream_id: str, *, direct: bool, ts: Any = None) -> None:
        now = _now(self._clock) if ts is None else float(ts)
        key = str(stream_id or "")
        try:
            with self._lock:
                self._last_event = now
                if key:
                    self._last_incoming[key] = now
                    if direct:
                        self._last_direct[key] = now
                if direct or not key:
                    self._last_participation = max(self._last_participation, now)
        except Exception:
            pass

    def note_reply_start(self, stream_id: str = "", ts: Any = None) -> None:
        now = _now(self._clock) if ts is None else float(ts)
        key = str(stream_id or "-")
        try:
            with self._lock:
                self._last_event = now
                self._reply_inflight[key] = now
                self._last_participation = max(self._last_participation, now)
        except Exception:
            pass

    def note_reply_end(self, stream_id: str = "", ts: Any = None) -> None:
        now = _now(self._clock) if ts is None else float(ts)
        key = str(stream_id or "-")
        try:
            with self._lock:
                self._last_event = now
                self._reply_inflight.pop(key, None)
                self._last_participation = max(self._last_participation, now)
        except Exception:
            pass

    # ---- 只读快照 ----
    def snapshot(self, ts: Any = None) -> Dict[str, Any]:
        """忙闲快照（只读；不确定/暖机期 coverage=partial，消费方不得启动自动观看）。"""
        now = _now(self._clock) if ts is None else float(ts)
        with self._lock:
            inflight = {k: v for k, v in self._reply_inflight.items()
                        if now - v <= REPLY_INFLIGHT_TTL_SECONDS}
            reasons: list = []
            if inflight:
                reasons.append("reply_in_flight")
            for key, seen in self._last_direct.items():
                last_reply_after = max(
                    (v for k, v in inflight.items() if k == key), default=0.0)
                if seen > self._last_participation - 0.0001 and seen >= last_reply_after \
                        and now - seen <= CHAT_SILENCE_SECONDS:
                    # 直接入站后尚无我方参与收尾（近似：参与时刻未推进到该消息之后）
                    reasons.append(f"direct_pending:{key}")
                    break
            if self._last_participation and now - self._last_participation < CHAT_SILENCE_SECONDS:
                reasons.append("recent_participation")
            coverage = "ok" if self._last_event else "partial"
            return {
                "busy": bool(reasons),
                "reasons": reasons,
                "coverage": coverage,
                "last_participation_epoch": self._last_participation or None,
                "silence_seconds": round(now - self._last_participation, 3)
                if self._last_participation else None,
                "in_flight_replies": len(inflight),
            }


class SurfLeaseAuthority:
    """自动注意力租约权威：SURF 至多一个持有者；直接对话让位 CHAT（计划书 §4.2）。"""

    def __init__(self, activity: ConversationActivityTracker, clock: Any = None) -> None:
        self._activity = activity
        self._clock = clock
        self._lock = threading.Lock()
        self._lease: Optional[Dict[str, Any]] = None
        self._version = 0

    # ---- 内部 ----
    def _bump(self) -> int:
        self._version += 1
        return self._version

    # ---- 对外面 ----
    def try_acquire_surf(self, holder: str) -> Optional[Dict[str, Any]]:
        """申请 SURF 租约：忙/不确定/CHAT 在持 → None（消费方放弃，不等待）。"""
        snap = self._activity.snapshot()
        if snap.get("busy") or snap.get("coverage") != "ok":
            return None
        with self._lock:
            current = self._lease
            if current is not None:
                if current["kind"] == "surf" and current["holder"] == holder:
                    current["acquired_at"] = _now(self._clock)  # 幂等续持
                    return dict(current)
                return None  # 其他持有者（SURF 或 CHAT）在持
            lease = {"kind": "surf", "holder": str(holder or ""),
                     "version": self._bump(), "acquired_at": _now(self._clock)}
            self._lease = lease
            return dict(lease)

    def acquire_chat(self, holder: str) -> Dict[str, Any]:
        """直接对话到来：撤销自动注意力（若有）并持 CHAT 租约（计划书 §4.2 让位）。"""
        with self._lock:
            current = self._lease
            if current is not None and current["kind"] == "surf" \
                    and current["holder"] != str(holder or ""):
                self._lease = None
                self._bump()  # 撤销即版本递增：在途消费方下一拍核对即失效
                current = None
            if current is None or current["kind"] != "chat" or current["holder"] != holder:
                lease = {"kind": "chat", "holder": str(holder or ""),
                         "version": self._bump(), "acquired_at": _now(self._clock)}
                self._lease = lease
                return dict(lease)
            current["acquired_at"] = _now(self._clock)
            return dict(current)

    def release(self, holder: str) -> bool:
        """释放自己持有的租约（幂等；非持有者调用无效果）。"""
        with self._lock:
            current = self._lease
            if current is not None and current["holder"] == str(holder or ""):
                self._lease = None
                self._bump()
                return True
            return False

    def current(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return dict(self._lease) if self._lease is not None else None

    def lease_valid(self, lease: Optional[Dict[str, Any]]) -> bool:
        """消费方在每次模型调用/继续下一条前核对：租约仍在且版本未变。"""
        if not isinstance(lease, dict):
            return False
        current = self.current()
        return bool(current and current.get("kind") == lease.get("kind")
                    and current.get("holder") == lease.get("holder")
                    and current.get("version") == lease.get("version"))
