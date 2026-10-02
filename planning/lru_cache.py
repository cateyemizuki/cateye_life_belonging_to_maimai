"""LRU 缓存实现（来源：自主规划插件 cache/lru_cache.py，照搬）。

线程/协程安全的 LRU 缓存，支持异步与同步接口。
"""

from collections import OrderedDict
from typing import Any, Optional, Tuple
import threading
import time


class LRUCache:
    """LRU 缓存，支持异步与同步接口。

    设计要点：
        - 缓存项格式：``(value, expire_time)`` —— 由 LRUCache 自己管理过期
        - 锁：``threading.RLock``。在 maibot 单进程单事件循环模型下，
          每次锁持有时间 < 1ms，对 asyncio 事件循环无可感知阻塞
        - TTL：构造时统一指定，外部不应再二次封装过期时间
        - 不变量：``__contains__`` / ``__getitem__`` / ``get`` 都做被动过期检查
    """

    def __init__(self, max_size: int = 100, ttl: int = 300) -> None:
        self._store: "OrderedDict[Any, Tuple[Any, float]]" = OrderedDict()
        self.max_size: int = max_size
        self.ttl: int = ttl
        self._lock: threading.RLock = threading.RLock()

    @staticmethod
    def _is_expired(expire_time: float) -> bool:
        return time.time() >= expire_time

    # ------------------------------------------------------------
    # 异步接口（推荐在协程代码中使用）
    # ------------------------------------------------------------

    async def get(self, key: Any) -> Optional[Any]:
        """获取缓存值（过期自动清理）。"""
        with self._lock:
            if key not in self._store:
                return None
            value, expire_time = self._store[key]
            if self._is_expired(expire_time):
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    async def set(self, key: Any, value: Any) -> None:
        """写入缓存值，自动 LRU 淘汰。"""
        with self._lock:
            expire_time = time.time() + self.ttl
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expire_time)
            if len(self._store) > self.max_size:
                self._store.popitem(last=False)

    # ------------------------------------------------------------
    # 主动批量清理（供后台任务调用）
    # ------------------------------------------------------------

    def clear(self) -> None:
        """清空全部缓存。"""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def __delitem__(self, key: Any) -> None:
        with self._lock:
            self._store.pop(key, None)

    def __contains__(self, key: Any) -> bool:
        with self._lock:
            if key not in self._store:
                return False
            _, expire_time = self._store[key]
            if self._is_expired(expire_time):
                del self._store[key]
                return False
            return True

    def __getitem__(self, key: Any) -> Any:
        """同步获取（不更新 LRU 顺序；过期抛 KeyError）。"""
        with self._lock:
            if key not in self._store:
                raise KeyError(key)
            value, expire_time = self._store[key]
            if self._is_expired(expire_time):
                del self._store[key]
                raise KeyError(key)
            return value

    def __setitem__(self, key: Any, value: Any) -> None:
        """同步写入（等价于异步 set）。"""
        with self._lock:
            expire_time = time.time() + self.ttl
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expire_time)
            if len(self._store) > self.max_size:
                self._store.popitem(last=False)
