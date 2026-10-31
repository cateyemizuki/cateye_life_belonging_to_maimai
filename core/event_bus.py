"""Process-local event delivery, logging and recursion protection. No business policy."""
from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Tuple


logger = logging.getLogger(__name__)

#: 同一事件在一条 publish 链上的最大递归深度（超过丢弃并告警）
MAX_RECURSION_DEPTH = 5

#: 这些键名是 DecisionLogger.record 的保留参数/自动字段，不能作为 **fields 透传
_RESERVED_LOG_KEYS = ("action", "trigger_type", "reason", "time")

#: publish_soon 创建的游离任务强引用（防止被 GC 提前回收；完成后自动移除）
_pending_tasks: set = set()

#: 当前协程上下文的发布链（事件名元组，用于递归深度保护）
_publish_chain: ContextVar[Tuple[str, ...]] = ContextVar("cateye_bus_chain", default=())

Handler = Callable[[Dict[str, Any]], Awaitable[None]]


class EventBus:
    """轻量进程内事件总线：订阅 / 发布 / 异常隔离 / 自动决策日志。"""

    def __init__(self, decision_logger: Any = None) -> None:
        self._decision_logger = decision_logger
        self._subscribers: Dict[str, List[Tuple[str, Handler]]] = {}

    # ------------------------------------------------------------------
    # 订阅 / 注销
    # ------------------------------------------------------------------
    def subscribe(self, event: str, handler: Handler, *, owner: str = "") -> Callable[[], None]:
        """订阅事件，返回**反订阅闭包**（幂等，可安全重复调用）。"""
        name = str(event or "").strip()
        if not name:
            raise ValueError("事件总线：事件名不能为空")
        if not callable(handler):
            raise TypeError("事件总线：handler 必须可调用")
        entry = (str(owner or ""), handler)
        self._subscribers.setdefault(name, []).append(entry)

        def _unsubscribe() -> None:
            handlers = self._subscribers.get(name)
            if not handlers:
                return
            try:
                handlers.remove(entry)
            except ValueError:
                return
            if not handlers:
                self._subscribers.pop(name, None)

        return _unsubscribe

    def unsubscribe_all(self, owner: str) -> int:
        """注销某 owner 的全部订阅（服务束 stop() 兜底用），返回注销条数。"""
        key = str(owner or "")
        removed = 0
        for name in list(self._subscribers):
            handlers = self._subscribers[name]
            kept = [e for e in handlers if e[0] != key]
            removed += len(handlers) - len(kept)
            if kept:
                self._subscribers[name] = kept
            else:
                self._subscribers.pop(name, None)
        return removed

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------
    async def publish(self, event: str, **payload: Any) -> int:
        """发布事件：逐个 await 订阅者，单订阅者异常隔离并记日志。

        Returns:
            成功执行的订阅者数量（抛异常的订阅者不计入）。
        """
        name = str(event or "").strip()
        if not name:
            logger.warning("事件总线：忽略空事件名发布")
            return 0
        chain = _publish_chain.get()
        if chain.count(name) >= MAX_RECURSION_DEPTH:
            logger.warning(
                "事件总线：%s 递归发布深度超过 %d，丢弃本次发布（payload 键=%s）",
                name, MAX_RECURSION_DEPTH, sorted(payload),
            )
            return 0
        data = self._prepare_payload(payload)
        self._record(name, data)
        handlers = list(self._subscribers.get(name) or [])
        if not handlers:
            return 0
        token = _publish_chain.set(chain + (name,))
        delivered = 0
        try:
            for owner, handler in handlers:
                try:
                    await handler(dict(data))
                    delivered += 1
                except Exception as exc:  # 单订阅者异常不得影响其余订阅者/发布方
                    logger.warning("事件总线：订阅者 %s 处理 %s 异常：%s",
                                   owner or "(未署名)", name, exc, exc_info=True)
        finally:
            _publish_chain.reset(token)
        return delivered

    def publish_soon(self, event: str, **payload: Any) -> None:
        """create_task 包装的发布（供同步 Hook / 同步上下文使用；无事件循环时静默跳过）。"""
        try:
            task = asyncio.get_running_loop().create_task(self.publish(event, **payload))
        except RuntimeError:
            logger.debug("事件总线：%s 无运行中的事件循环，publish_soon 跳过", event)
            return
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    @staticmethod
    def _prepare_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """补 ts/source 并置于首字段（发布方已带同名键时保留其值）。"""
        data: Dict[str, Any] = {
            "ts": payload.pop("ts", None) or datetime.now().isoformat(timespec="seconds"),
            "source": payload.pop("source", "") or "",
        }
        data.update(payload)
        return data

    def _record(self, event: str, data: Dict[str, Any]) -> None:
        """自动落决策日志（日志开关关闭时由 DecisionLogger 自身短路，零额外开销）。"""
        logger_ = self._decision_logger
        if logger_ is None:
            return
        fields = {k: v for k, v in data.items() if k not in _RESERVED_LOG_KEYS}
        try:
            logger_.record(action=f"bus:{event}", **fields)
        except Exception:
            pass


