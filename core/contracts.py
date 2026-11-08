"""跨模块最小契约（D1，2026-11-01 模块解耦与统一联动架构计划 §3.1/§四）。

只声明消费方实际用到的最小 Protocol 与提供者形态，无业务实现：
本模块不得导入任何业务模块（companion/emotion/planning/qzone）或 plugin，
只可用标准库类型（层间纪律与 core/time_utils.py:3-4 同源）。另含组合根字符串
属性契约白名单（``PLUGIN_ATTR_CONTRACT``，P1-01 结构守卫使用）。

装配口径（组合根 plugin.py 负责）：提供者一律**惰性**——被注入的实例
（睡眠引擎/情绪服务束）晚于消费方服务束装配，调用时才解析；返回 None =
能力未装配，消费方按既有可空降级语义处理（§八.2），不改为启动失败。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Protocol


class SleepEngineGate(Protocol):
    """睡眠引擎对情绪侧的最小门面（D1：emotion 不再直读 ``plugin._sleep_engine``）。

    实现方 = ``planning.sleep_engine.SleepEngine``（鸭子类型兼容既有测试桩）；
    ``is_asleep`` 返回 True/False = 引擎权威，None = 引擎不表态（调用方回退
    旧窗口逻辑，PR3 单一 wake provider 口径不变）。
    """

    def is_asleep(self, now: Any) -> Optional[bool]:
        """引擎是否睡着；None = 不表态（未启用/未装配/无本夜会话）。"""
        ...

    def note_interruption(self, current: Any) -> Any:
        """夜间打断喂给引擎打断状态机（只记账，L1 既有语义）。"""
        ...


class EmotionValenceSource(Protocol):
    """情绪模块对陪伴侧的最小只读面（M3 B2 既有公开入口收拢为显式契约）。

    实现方 = ``emotion.services.EmotionServices``；缺失/停用由消费方降级
    （好感调制乘数恒 1.0，§6.2 B）。
    """

    def is_enabled(self) -> bool:
        """情绪模块是否启用。"""
        ...

    def effective_valence(self) -> Optional[float]:
        """当前生效效价 V；不可用返回 None。"""
        ...


class SleepAnchorsSource(Protocol):
    """真实作息锚点对情绪侧的最小查询面（D3：emotion 不再导入 companion.patrol）。

    实现方 = 组合根 ``plugin.real_sleep_anchors`` 公共口（绑定 companion.patrol 的
    同一 wiring 真源，§24.2 任务B「所有消费点共享，禁止自建副本」）；返回对象鸭子
    兼容 ``RealAnchors``（``start``/``end`` 为 "HH:MM" 字符串、``source`` 为来源
    标记），None = 真实锚点不可得（消费方回退 ``[schedule]`` 配置路径，§7.9 不变）。
    """

    def __call__(self, now: Any = None) -> Optional[Any]:
        """取当前生效的真实作息锚点；``now`` = 调用方判定时刻（同帧口径）。"""
        ...


#: 惰性提供者（装配期注入；调用返回 None = 能力未装配）
SleepEngineGateProvider = Callable[[], Optional[SleepEngineGate]]
EmotionValenceSourceProvider = Callable[[], Optional[EmotionValenceSource]]
SleepAnchorsSourceProvider = Callable[[], Optional[SleepAnchorsSource]]


class VideoWatchPort(Protocol):
    """内置视频观看模块对工具/日程侧的最小动作面（B 计划 §3，2026-11-01）。

    实现方 = ``video_watch.service.VideoWatchService``（生活插件内部单实例；
    AI 工具 ``watch_bili_video`` 与 integrations/video_schedule 日程编排共用，
    替代旧跨插件 ``ctx.api.call("cateye.bili-watch.bili_watch")`` 路径）。
    回执为 dict：``success`` / ``status`` / ``task_id`` / ``error`` /
    ``error_class`` / ``cached`` / ``deduped`` / ``candidate`` 等，与原型
    WatchTaskManager 的同名动作回执保持同构（调度侧零语义迁移）。
    """

    async def handle_action(self, action: str = "", **kwargs: Any) -> Dict[str, Any]:
        """动作分发：submit / status / result / cancel / next_candidate。"""
        ...


#: 视频观看端口提供者（装配期注入；返回 None = 模块未装配，调用方按降级语义处理）
VideoWatchPortProvider = Callable[[], Optional[VideoWatchPort]]


class ConversationActivityPort(Protocol):
    """对话忙闲只读面（冲浪计划书 §4.1，S1；companion.attention 实现）。

    消费方（integrations 注意力门控）据此判断「没有在聊天」；``coverage``
    非 ``"ok"`` 或 ``busy`` 为真时不得启动自动观看（fail-safe，不把
    「没看到消息」当宿主肯定空闲）。快照字段：busy/reasons/coverage/
    last_participation_epoch/silence_seconds/in_flight_replies。
    """

    def snapshot(self) -> Dict[str, Any]:
        """当前忙闲快照（只读 dict；不抛异常，观测失败按不确定返回）。"""
        ...


class AttentionLeasePort(Protocol):
    """自动注意力租约面（冲浪计划书 §4.2，S1；companion.attention 实现）。

    SURF=自动冲浪（至多一个持有者）；直接对话经 ``acquire_chat`` 让位。
    ``lease_valid`` 供消费方在每次模型调用/继续下一条前核对版本。
    """

    def try_acquire_surf(self, holder: str) -> Optional[Dict[str, Any]]:
        """申请 SURF 租约；忙/不确定/被占返回 None（放弃，不等待）。"""
        ...

    def release(self, holder: str) -> bool:
        """释放自己持有的租约（幂等）。"""
        ...

    def current(self) -> Optional[Dict[str, Any]]:
        """当前租约（无则 None；只读副本）。"""
        ...

    def lease_valid(self, lease: Optional[Dict[str, Any]]) -> bool:
        """租约仍在且版本未变。"""
        ...


#: 注意力租约提供者（装配期注入；None = 权威未装配，自动观看门控按拒绝处理）
AttentionLeaseProvider = Callable[[], Optional[AttentionLeasePort]]


#: 组合根字符串属性契约（2026-11-02 质量清单 P1-01 守卫白名单）。
#:
#: 业务包内 ``getattr(plugin, "_xxx")`` 的字面量键必须登记于此；新增键 = 扩契约 +
#: 同步 ``core/plugin_context.PluginContext``，不得在消费点静默发明。收缩 = 删键并
#: 迁移调用点。此清单只守**跨模块读取组合根**的字符串契约；服务束内部私有属性不在此列。
PLUGIN_ATTR_CONTRACT = frozenset({
    "_planning",          # planning 服务束
    "_companion",         # companion 服务束
    "_qzone",             # qzone 服务束
    "_emotion",           # emotion 服务束
    "_decision_logger",   # 决策日志
    "_data_dir",          # 插件数据目录
    "_db",                # 日程 SQLite 连接
    "_llm",               # LLM 服务
    "_llm_logger",        # LLM 调用日志
    "_bus",               # 进程内事件总线
    "_sleep_engine",      # 睡眠引擎
    "_schedule_query",    # 日程只读查询
    "_schedule_gen",      # 日程生成器
    "_life_state",        # 生活状态
    "_life_model",        # 生活状态模型
    "_affection",         # 好感度管理器
    "_lover_name",        # 陪伴对象名
    "_personality",       # 陪伴对象人设
    "_plugin_root",       # 插件根目录
    "_attention",         # 注意力租约/忙闲观测
    "_qzone_video",       # 空间视频候选桥（PR-D1 H10：面板只读计数投影）
    "_video_watch",       # 视频观看服务（PR-ENTERTAINMENT 3.17.13：娱乐面板最近观看只读投影）
    "_screen_perception", # 屏幕感知联动服务（PR-LINKAGE-SCREEN 3.17.16：patrol/问候链委托 peek 旁白）
})
