"""进程内事件总线 + 全渠道打扰预算（MAINTENANCE_PLAN §三 / §六.2 H，里程碑 M0）。

设计约定（逐条对应计划书 §3.1）：
- 事件名 ``模块.事件``（snake_case）；payload 自动补 ``ts``（ISO 时间）与 ``source``；
- 发布方只放 ID 与摘要（qq/stream_id/delta/事件名），订阅方需要更多数据时自己拉；
- 每次 publish 自动 ``DecisionLogger.record(action="bus:<event>", **payload 摘要)``
  （决策日志开关关闭时由 logger 自身零开销短路，见 core/diag.py:143）；
- 同一事件递归 publish 深度 >5 时丢弃该次发布并告警（§3.1 深度保护）；
- 订阅注册/注销挂在各服务束 start()/stop()（M0 实际订阅者仅本文件的预算计数器）。

全渠道打扰预算（§六.2 H，B11）：一切主动行为触发前发 ``proactive.used``，
按日计数；各发布方触发前查询 :func:`ProactiveBudget.allow_proactive`；
上限默认 = ``patrol.daily_max_speak + 2``，``patrol.global_proactive_cap > 0`` 时直接采用其值。

计数持久化（A2，2026-10-25）：传入 ``data_dir`` 时 ``_used/_date`` 落盘
``proactive_budget.json``（复用 core/storage.py 的 load_json/save_json_atomic）——
启动装载、计数变化即原子写、日切重置写回，使计数跨 file_watcher 热重载连续
（§八.8：热重载会清零一切内存态计数）；落盘异常降级内存态，不影响事件分发。
"""

from __future__ import annotations

import asyncio
import logging
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.storage import load_json, save_json_atomic

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
        self._budget: Optional["ProactiveBudget"] = None

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
    # 预算挂载（装配期由 install_proactive_budget 调用）
    # ------------------------------------------------------------------
    def attach_budget(self, budget: "ProactiveBudget") -> None:
        self._budget = budget

    @property
    def budget(self) -> Optional["ProactiveBudget"]:
        return self._budget

    def allow_proactive(self, channel: str = "", intent: str = "") -> bool:
        """查询全渠道打扰预算（未安装预算时恒放行）。"""
        if self._budget is None:
            return True
        return self._budget.allow_proactive(channel, intent)

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


class ProactiveBudget:
    """全渠道打扰预算计数器（§六.2 H）：按日计数，跨天自动清零。

    A2 持久化：``data_dir`` 非空时计数落盘 ``proactive_budget.json``（启动装载 /
    计数变化即原子写 / 日切重置写回），使 file_watcher 热重载后计数不回落；
    落盘异常降级为内存态并告警，不影响事件分发。
    """

    #: 状态文件名（data_dir 下）
    STATE_FILENAME = "proactive_budget.json"

    def __init__(self, config_provider: Any = None, decision_logger: Any = None,
                 data_dir: Any = None) -> None:
        self._config_provider = config_provider
        self._decision_logger = decision_logger
        self._date = ""
        self._used = 0
        self._channels: Dict[str, int] = {}
        self._unsubscribe: Optional[Callable[[], None]] = None
        #: 持久化落点（None = 纯内存态；既有装配不传 data_dir 时行为不变）
        self._state_path: Optional[Path] = (
            Path(data_dir) / self.STATE_FILENAME if data_dir else None
        )
        self._persist_warned = False
        self._load()

    # ---- 持久化（A2） ----
    def _load(self) -> None:
        """装载落盘计数：缺失/损坏（含 used 字段类型异常）/非当日 → 全新起步、告警并回写。"""
        path = self._state_path
        if path is None:
            return
        today = datetime.now().strftime("%Y-%m-%d")
        self._date = today
        raw = load_json(path, None)
        if raw is None and not path.exists():
            logger.warning("打扰预算状态文件不存在，按全新计数起步：%s", path)
            self._save()
            return
        if not isinstance(raw, dict):
            logger.warning("打扰预算状态文件损坏，按全新计数起步并重建：%s", path)
            self._save()
            return
        date = raw.get("date")
        if not isinstance(date, str) or not date:
            logger.warning("打扰预算状态文件缺 date 字段，按全新计数起步并重建：%s", path)
            self._save()
            return
        if date != today:
            # 跨日（含停机跨天）：装载即清零并写回
            self._save()
            return
        used = raw.get("used")
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            # used 是整份状态的计数基准：类型异常即判整份损坏，清空重建并回写，
            # 不得保留同一文件里已不可信的 channels 明细
            logger.warning("打扰预算状态文件 used 字段异常，整份状态按损坏处理并重建：%s", path)
            self._save()
            return
        self._date = date
        self._used = used
        self._channels = {}
        channels = raw.get("channels")
        if isinstance(channels, dict):
            for key, value in channels.items():
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    self._channels[str(key)] = value

    def _save(self) -> None:
        """计数变化即原子落盘；失败降级内存态（单次告警，不中断事件分发）。"""
        path = self._state_path
        if path is None:
            return
        state = {"date": self._date, "used": self._used, "channels": dict(self._channels)}
        if save_json_atomic(path, state):
            self._persist_warned = False
            return
        if not self._persist_warned:
            self._persist_warned = True
            logger.warning("打扰预算落盘失败，降级为内存态计数（重启后计数将丢失）：%s", path)

    # ---- 计数与查询 ----
    def _roll_date(self) -> None:
        """日期变化 → 计数清零并落盘。"""
        today = datetime.now().strftime("%Y-%m-%d")
        if today != self._date:
            self._date = today
            self._used = 0
            self._channels = {}
            self._save()

    def _config(self) -> Any:
        """取当前配置（provider 可为「返回配置的可调用对象」或配置对象本身）。"""
        provider = self._config_provider
        if provider is None:
            return None
        try:
            return provider() if callable(provider) else provider
        except Exception:
            return None

    def cap(self) -> int:
        """当日全渠道上限：patrol.global_proactive_cap > 0 时采用其值，否则 daily_max_speak + 2。"""
        cfg = self._config()
        patrol = getattr(cfg, "patrol", None)
        daily = getattr(patrol, "daily_max_speak", 5)
        override = getattr(patrol, "global_proactive_cap", 0)
        try:
            daily_int = int(daily if daily is not None else 5)
        except (TypeError, ValueError):
            daily_int = 5
        try:
            override_int = int(override if override is not None else 0)
        except (TypeError, ValueError):
            override_int = 0
        return override_int if override_int > 0 else daily_int + 2

    def used_today(self) -> int:
        self._roll_date()
        return self._used

    def channel_counts(self) -> Dict[str, int]:
        """当日各渠道计数快照（诊断用）。"""
        self._roll_date()
        return dict(self._channels)

    def allow_proactive(self, channel: str = "", intent: str = "") -> bool:
        """预算查询：未达上限放行；达上限拦截并留痕（action="budget.blocked"）。"""
        self._roll_date()
        limit = self.cap()
        if self._used < limit:
            return True
        self._log_blocked(channel, intent, limit)
        return False

    def record_used(self, channel: str = "", intent: str = "") -> None:
        """记一次主动打扰（proactive.used 订阅入口 / 直接调用）；计数变化即落盘。"""
        self._roll_date()
        self._used += 1
        key = str(channel or "unknown")
        self._channels[key] = self._channels.get(key, 0) + 1
        self._save()

    async def on_proactive_used(self, payload: Dict[str, Any]) -> None:
        """``proactive.used`` 订阅者（M0 唯一实际订阅者）。"""
        if not isinstance(payload, dict):
            return
        self.record_used(str(payload.get("channel") or ""), str(payload.get("intent") or ""))

    # ---- 内部 ----
    def _log_blocked(self, channel: str, intent: str, limit: int) -> None:
        logger.warning(
            "全渠道打扰预算拦截：渠道=%s，当日已用 %d/%d（intent=%s）",
            channel or "(未署名)", self._used, limit, intent or "-",
        )
        logger_ = self._decision_logger
        if logger_ is None:
            return
        try:
            logger_.record(
                action="budget.blocked",
                trigger_type="proactive_budget",
                reason=f"当日全渠道主动打扰已用 {self._used}/{limit}，本次触发被拦截",
                channel=str(channel or ""), intent=str(intent or ""),
                used=self._used, cap=limit, date=self._date,
            )
        except Exception:
            pass


#: 最近一次安装的预算计数器（模块级查询函数的兜底入口）
_installed_budget: Optional[ProactiveBudget] = None


def install_proactive_budget(bus: EventBus, config_provider: Any = None,
                             decision_logger: Any = None,
                             data_dir: Any = None) -> ProactiveBudget:
    """装配期安装全渠道打扰预算：订阅 ``proactive.used`` 并挂到总线上。

    Args:
        bus: 事件总线实例（plugin._bus）。
        config_provider: 返回插件配置的可调用对象（运行期动态读 patrol 段，热重载生效）。
        decision_logger: 决策日志（拦截留痕用；None 时只记 python logger）。
        data_dir: 状态目录（A2；传入时计数落盘 ``proactive_budget.json`` 跨热重载连续，
            None = 纯内存态）。
    """
    global _installed_budget
    budget = ProactiveBudget(config_provider=config_provider, decision_logger=decision_logger,
                             data_dir=data_dir)
    budget._unsubscribe = bus.subscribe("proactive.used", budget.on_proactive_used,
                                        owner="proactive_budget")
    bus.attach_budget(budget)
    _installed_budget = budget
    return budget


def allow_proactive(channel: str = "", intent: str = "") -> bool:
    """模块级预算查询（未安装预算时恒放行；发布方优先用 ``bus.allow_proactive``）。"""
    budget = _installed_budget
    if budget is None:
        return True
    return budget.allow_proactive(channel, intent)