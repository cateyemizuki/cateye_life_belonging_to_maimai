"""情绪事件桥（M3；MAINTENANCE_PLAN §4.2 / §6.2 A·E·G）。

把 companion / planning / qzone 的跨模块事件翻译成**有界情绪增量**，全部经
:meth:`emotion.service.EmotionService.record_external_event` 入账（统一入口再乘
``global_event_multiplier``）。订阅注册/注销挂在 ``EmotionServices.start()/stop()``。

订阅清单（附录 A）与口径：

- ``relation.message_received``   仅记账/日志（不产生情绪增量；对话情绪走
  replyer.after_response 自动评估主通道）；
- ``relation.silence_detected``   模板 ΔV=−0.12、ΔA=+0.04；target 事件
  （K = 1+档位加成；频控/仅 target 由发布方 patrol 负责）；
- ``schedule.commitment_kept``    模板 ΔV=+0.2；归属 target 事件（K=1+档位加成）；
- ``schedule.commitment_broken``  模板 ΔV=−0.15；归属 target 事件（K=1+档位加成）；
- ``qzone.comment_received``      模板 ΔV ∈ [+0.05, +0.1] 区间取值；K=1（仅乘全局
  倍率；**不**为评论者建立好感档案，§九.1）；
- 其余 ``relation.*`` / ``qzone.*`` 事件仅 debug 日志（附录 A 事件表全量登记）。

倍率口径（§6.2 A，加算非乘算）：``final = delta × global_event_multiplier × K``，
``K = 1 + target_bonus_step×(档位+1)``（target）或 ``non_target_multiplier``（非 target）。
档位从 affection 状态层读取（``getattr(plugin, "_affection", None)`` 可空降级，
取不到时 K=1，不给加成）。所有处理器绝不抛异常——总线已做异常隔离，这里再兜一层，
保证单模块停用不影响其余模块（§八.2）。
"""

from __future__ import annotations

import logging
import random
from typing import Any, Callable, Dict, List, Optional

from .vad import VADState

logger = logging.getLogger(__name__)

#: 沉默事件模板（§6.2 A：轻度不安而非悲伤）
SILENCE_DELTA = VADState(-0.12, 0.04, 0.0)
#: 约定兑现 / 违约模板（§6.2 E）
COMMITMENT_KEPT_DELTA = VADState(0.2, 0.0, 0.0)
COMMITMENT_BROKEN_DELTA = VADState(-0.15, 0.0, 0.0)
#: 约定事件的描述前缀（M5 睡眠大修 v2 的「当日约定兑现情况」按此前缀从当天日志统计）
COMMITMENT_KEPT_PREFIX = "约定兑现"
COMMITMENT_BROKEN_PREFIX = "约定落空"
#: 空间评论正事件 ΔV 区间（§6.2 G：+0.05~+0.1，「被在乎的感觉」）
QZONE_COMMENT_V_MIN = 0.05
QZONE_COMMENT_V_MAX = 0.1

#: 订阅的「仅 debug 日志」事件（附录 A 里其余 relation.* / qzone.* 事件）
IGNORED_EVENTS: tuple[str, ...] = (
    "relation.interaction_event",
    "relation.affection_tier_changed",
    "relation.daily_reviewed",
    "qzone.posted",
    "qzone.comment_replied",
)


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


class EmotionBridge:
    """跨模块事件 → 情绪增量 的订阅处理器集合（无状态，只持插件/服务引用）。"""

    def __init__(self, plugin: Any, service: Any) -> None:
        self._plugin = plugin
        self._service = service
        #: relation.message_received 的记账计数（仅观测，不参与情绪结算）
        self.message_count = 0

    # ------------------------------------------------------------------
    # 注册（服务束 start() 调用；返回反订阅闭包列表）
    # ------------------------------------------------------------------
    def subscribe(self, bus: Any) -> List[Callable[[], None]]:
        """订阅本桥的全部事件，返回反订阅闭包列表（总线为空时返回空列表）。"""
        if bus is None:
            return []
        unsubs: List[Callable[[], None]] = []
        handlers = (
            ("relation.message_received", self._on_message_received),
            ("relation.silence_detected", self._on_silence_detected),
            ("schedule.commitment_kept", self._on_commitment_kept),
            ("schedule.commitment_broken", self._on_commitment_broken),
            ("qzone.comment_received", self._on_qzone_comment),
        )
        for event, handler in handlers:
            unsubs.append(bus.subscribe(event, handler, owner="emotion"))
        for event in IGNORED_EVENTS:
            unsubs.append(bus.subscribe(event, self._on_ignored, owner="emotion"))
        return unsubs

    # ------------------------------------------------------------------
    # 处理器
    # ------------------------------------------------------------------
    async def _on_message_received(self, payload: Dict[str, Any]) -> None:
        """仅记账/日志：消息事件本身不产生情绪增量（§6.2 A 输入侧）。"""
        data = payload if isinstance(payload, dict) else {}
        self.message_count += 1
        logger.debug("emotion bridge：relation.message_received（is_target=%s，stream_kind=%s，累计 %d）",
                     data.get("is_target"), data.get("stream_kind"), self.message_count)

    async def _on_silence_detected(self, payload: Dict[str, Any]) -> None:
        """沉默事件（B1 核心）：模板 ΔVAD × K（target），走统一入口。"""
        data = payload if isinstance(payload, dict) else {}
        hours = _to_float(data.get("hours"), 0.0)
        result = await self._record(
            source="bridge:silence",
            description=f"重要的人已沉默 {hours:.1f} 小时没有消息",
            delta=SILENCE_DELTA,
            is_target=True,
        )
        logger.debug("emotion bridge：silence_detected hours=%.1f target_bonus=%s → ok=%s",
                     hours, data.get("target_bonus"), result.get("ok"))

    async def _on_commitment_kept(self, payload: Dict[str, Any]) -> None:
        """约定兑现（§6.2 E）：模板 ΔV=+0.2，归属 target 事件。"""
        await self._record_commitment("kept", payload, COMMITMENT_KEPT_DELTA)

    async def _on_commitment_broken(self, payload: Dict[str, Any]) -> None:
        """约定违约（§6.2 E）：模板 ΔV=−0.15，归属 target 事件。"""
        await self._record_commitment("broken", payload, COMMITMENT_BROKEN_DELTA)

    async def _record_commitment(self, kind: str, payload: Dict[str, Any], delta: VADState) -> None:
        data = payload if isinstance(payload, dict) else {}
        summary = str(data.get("summary") or data.get("commitment") or "约定").strip()[:60]
        prefix = COMMITMENT_KEPT_PREFIX if kind == "kept" else COMMITMENT_BROKEN_PREFIX
        result = await self._record(
            source=f"bridge:commitment_{kind}",
            description=f"{prefix}：{summary}",
            delta=delta,
            is_target=True,
        )
        logger.debug("emotion bridge：commitment_%s summary=%r → ok=%s", kind, summary, result.get("ok"))

    async def _on_qzone_comment(self, payload: Dict[str, Any]) -> None:
        """空间收到评论（§6.2 G）：ΔV 在 [+0.05, +0.1] 区间取值，K=1（仅全局倍率）。

        K=1 是 §6.2 G 的明确口径（「走全局倍率」），**不**走 non_target_multiplier——
        评论者只是空间访客，既不加成也不额外淡化。不为评论者建立好感档案（§九.1），
        回评语气仍由宿主 PersonInfo 印象承担。
        """
        data = payload if isinstance(payload, dict) else {}
        nickname = str(data.get("nickname") or "好友").strip()[:20] or "好友"
        summary = str(data.get("summary") or "").strip()[:40]
        value = random.uniform(QZONE_COMMENT_V_MIN, QZONE_COMMENT_V_MAX)
        description = f"空间收到 {nickname} 的评论" + (f"：{summary}" if summary else "")
        result = await self._record(source="bridge:qzone_comment", description=description,
                                    delta=VADState(value, 0.0, 0.0), multiplier=1.0)
        logger.debug("emotion bridge：qzone.comment_received nickname=%s → ΔV=%.3f ok=%s",
                     nickname, value, result.get("ok"))

    async def _on_ignored(self, payload: Dict[str, Any]) -> None:
        """其余 relation.* / qzone.* 事件：仅 debug 日志，不做任何情绪结算。"""
        data = payload if isinstance(payload, dict) else {}
        logger.debug("emotion bridge：忽略事件（无情绪通道）ts=%s source=%s",
                     data.get("ts"), data.get("source"))

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    async def _record(self, *, source: str, description: str, delta: VADState,
                      is_target: Optional[bool] = None,
                      multiplier: Optional[float] = None) -> Dict[str, Any]:
        """统一入账：K 缺省按 §6.2 A 由 emotion 侧计算后传给 record_external_event。

        ``multiplier`` 显式给出时直接采用（空间评论 K=1 的口径，§6.2 G）；
        否则按 ``is_target`` 走 target（1+档位加成）/非 target（non_target_multiplier）。
        """
        service = self._service
        if service is None:
            return {"ok": False, "reason": "no_service"}
        try:
            k = float(multiplier) if multiplier is not None \
                else float(service.target_multiplier(bool(is_target)))
            return await service.record_external_event(source=source, description=description,
                                                       delta=delta, multiplier=k)
        except Exception as exc:  # 桥自身绝不抛异常（总线已隔离，这里兜底）
            logger.debug("emotion bridge：%s 入账异常（忽略）：%s", source, exc)
            return {"ok": False, "reason": "error"}


def register_bridge(bus: Any, service: Any, plugin: Any) -> List[Callable[[], None]]:
    """装配入口（EmotionServices._register_event_subscriptions 调用）。

    Returns:
        反订阅闭包列表；总线为空时返回空列表（可空降级，§八.2）。
    """
    if bus is None:
        return []
    return EmotionBridge(plugin, service).subscribe(bus)