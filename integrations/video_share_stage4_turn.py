# -*- coding: utf-8 -*-
"""阶段 4 视频卡片说话轮编排（R2 结构审计批，2026-11-12）。

自组合根 ``plugin._on_video_share_delivered`` / ``plugin._run_video_share_stage4_turn``
与 ``optional_action`` / 点播完成通知的阶段 4 拼接逐字下沉（DEV_GUIDE 结构守则
§2 模块联动：本插件内 ≥2 个模块串成一个业务场景 → ``integrations/<scene>.py``）。

场景链：卡片 accepted 回调 → 阶段 4 复检 → 预算/睡眠预检（``budget_reserved=True``，
卡片已占本次主动接触的槽位——同槽位例外口径见组合根 ``_social_preflight`` 注释与
DEV_GUIDE §5 预算登记）→ 拼 intent/reason → ``register_turn`` → 宿主 trigger →
失败 ``discard_turn``。

**依赖全部由组合根注入**（只读端口/回调）；本模块不 import 任何业务包与 tools
（硬边界 #1：不新增 integrations↔tools 包级边），不持预算/日程/好感状态；
进程内幂等集有界（64），重启后由 accepted 终态自然不重放。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable


def stage4_context_fields(context: Any) -> tuple:
    """阶段 4 上下文解析为 ``(stage, hours, relation)``；非阶段 4 → ``(0, 0.0, '恋人')``。

    与旧组合根内联口径逐字一致：``int/float/str`` 转换异常上抛由调用方
    try/except 收敛（点播通知路径 stage 归 0；optional_action 路径在 try 外）。
    """
    if isinstance(context, dict) and int(context.get('stage') or 0) == 4:
        return (4, float(context.get('hours') or 0.0),
                str(context.get('relation') or '恋人'))
    return (0, 0.0, '恋人')


def stage4_reason_suffix(reason: str, intent: str, context: Any, *,
                         append_prompt: Callable) -> tuple:
    """optional_action 阶段 4 reason 统一拼接（视频类用视频版）。

    返回 ``(reason, stage, hours, relation)``。标题标记幂等：调用方已拼过尾缀
    （``深层心理覆盖·失联阶段4``）时不重复追加（协调器侧同样按此标记判重）；
    非阶段 4 reason 逐字不变。``append_prompt`` 由组合根注入（阶段 4 文案拼接
    纯函数），本模块不 import 文案归属包。
    """
    stage, stage_hours, stage_relation = stage4_context_fields(context)
    if stage == 4:
        if '深层心理覆盖·失联阶段4' not in str(reason):
            is_video = 'video' in str(intent).lower() or '视频' in str(intent)
            reason = append_prompt(
                reason, hours=stage_hours, relation=stage_relation, is_video=is_video)
    return reason, stage, stage_hours, stage_relation


class VideoShareStage4Turn:
    """视频卡片送达后的阶段 4 说话轮单场景编排（组合根只注入依赖与回调）。

    端口契约（全部装配根注入，调用期解析）：

    - ``is_unloading() -> bool``：卸载中短路；
    - ``is_target_route(route) -> bool``：路由仍是当前陪伴目标私聊；
    - ``preflight(route, *, budget_reserved, ignore_spoke_at) -> (bool, str)``：
      夜间/睡眠/忙闲/300s 冷却复检（预算已由卡片占槽，``budget_reserved=True``）；
    - ``stage4_context() -> dict``：阶段 4 只读上下文（patrol 单一只读出口）；
    - ``last_card_spoke_at() -> datetime | None``：本卡片自己写入的开口时间；
    - ``trigger(**kwargs) -> awaitable``：宿主主动激活入口；
    - ``register(stream, channel, kind, *, miss_stage=...) -> turn_id`` /
      ``discard(turn_id)``：统一主动激活登记簿（组合根转发，含旧登记簿兼容）；
    - ``append_prompt(base, *, hours, relation, is_video) -> str``：阶段 4 文案拼接；
    - ``logger()``：宿主 logger（可空降级）。
    """

    def __init__(self, *, is_unloading: Callable[[], bool],
                 is_target_route: Callable[[dict], bool],
                 preflight: Callable, stage4_context: Callable[[], dict],
                 last_card_spoke_at: Callable[[], Any],
                 trigger: Callable, register: Callable, discard: Callable,
                 append_prompt: Callable, logger: Callable[[], Any]) -> None:
        self._is_unloading = is_unloading
        self._is_target_route = is_target_route
        self._preflight = preflight
        self._stage4_context = stage4_context
        self._last_card_spoke_at = last_card_spoke_at
        self._trigger = trigger
        self._register = register
        self._discard = discard
        self._append_prompt = append_prompt
        self._logger = logger
        #: 进程内幂等集（有界 64；旧组合根 ``_stage4_video_notified`` 同语义）
        self._notified: set = set()

    async def on_delivered(self, *, attempt_id: str, route: dict,
                           facts: dict, stage: int = 0) -> None:
        """VideoSocialService 卡片 accepted 后的阶段 4 回调（scheduler 隔离）。

        设计方案 §3.3：卡片 + 事实写入后，阶段 4 追加一次说话轮。这里只做快速
        校验与 ``create_task`` 调度——``_deliver`` 持有分享调度锁，绝不能在回调里
        await 完整 planner 触发。回调异常不影响已 accepted 的卡片回执。
        """
        if int(stage or 0) != 4:
            return
        if self._is_unloading():
            return
        if not self._is_target_route(route):
            return
        seen = self._notified
        if attempt_id in seen:
            return
        seen.add(attempt_id)
        while len(seen) > 64:  # 进程内幂等集有界；重启后由 accepted 终态自然不重放
            seen.pop()
        try:
            asyncio.get_running_loop().create_task(
                self.run_turn(
                    attempt_id=attempt_id, route=dict(route), facts=dict(facts or {})))
        except RuntimeError:
            return
        except Exception as exc:
            try:
                self._logger().warning('阶段4视频分享说话轮调度失败（忽略）：%s',
                                       type(exc).__name__)
            except Exception:
                pass

    async def run_turn(self, *, attempt_id: str, route: dict, facts: dict) -> None:
        """阶段 4 视频卡片后的说话轮：登记 → 宿主 trigger，失败撤登记。

        不 reserve/commit 任何预算（卡片已占本次主动接触的槽位）；夜间/睡眠/忙闲
        与 300s 冷却经 ``preflight(budget_reserved=True)`` 复检，复检不过
        一次性跳过（不重试，避免醒来补发旧话）。intent 明确禁止再次发卡/调分享工具。
        """
        try:
            if self._is_unloading():
                return
            if not self._is_target_route(route):
                return
            allowed, _reason = self._preflight(
                route, budget_reserved=True,
                ignore_spoke_at=self._last_card_spoke_at())
            if not allowed:
                return
            context = self._stage4_context()
            if int(context.get('stage') or 0) != 4:
                return  # 卡片与说话轮之间用户真实来信/周期重置 → 不再补阶段 4 尾缀
            hours = float(context.get('hours') or 0.0)
            relation = str(context.get('relation') or '恋人')
            title = str((facts or {}).get('title') or (facts or {}).get('bvid') or '').strip()
            summary = str((facts or {}).get('summary') or (facts or {}).get('description') or '').strip()
            if len(summary) > 200:
                summary = summary[:200] + '…'
            head = f"你刚给陪伴目标分享了刚看完的B站视频《{title}》，可结合观看摘要简评几句。"
            if summary:
                head += f"观看摘要：{summary}。"
            head += ("卡片已经发出：本轮不要再次发送卡片或链接，不要调用分享工具"
                     "（lbm_watch_share_recent_video），也不要转发给其他聊天。")
            intent = self._append_prompt(
                head, hours=hours, relation=relation, is_video=True)
            # 设计 §3.3 第 2 条：reason 也带同一份阶段 4 视频拼接文案（保留
            # video_share_card 机器标记作前缀，便于回执/诊断按原口径检索）。
            stage4_reason = self._append_prompt(
                'video_share_card', hours=hours, relation=relation, is_video=True)
            stream = str(route.get('stream_id') or '')
            try:
                turn_id = self._register(stream, 'video_share', 'video_share_card',
                                         miss_stage=4)
            except TypeError:
                # 兼容尚未扩展 miss_stage 参数的登记簿版本（S2 落地后走上面分支）。
                turn_id = self._register(stream, 'video_share', 'video_share_card')
            try:
                reply = await self._trigger(
                    stream_id=stream, intent=intent, reason=stage4_reason,
                    metadata={'attempt_id': attempt_id,
                              'delivery_kind': 'video_share_card', 'miss_stage': 4})
            except Exception:
                self._discard(turn_id)
                return
            if not ((isinstance(reply, dict) and reply.get('success') is True) or reply is True):
                self._discard(turn_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self._logger().warning('阶段4视频分享说话轮失败（忽略）：%s',
                                       type(exc).__name__)
            except Exception:
                pass
