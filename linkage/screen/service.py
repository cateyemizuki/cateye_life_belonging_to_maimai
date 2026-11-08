"""屏幕感知联动服务（PR-LINKAGE-SCREEN，3.17.16；首个联动适配器 ``cateye_screen``）。

从 ``companion/patrol.py`` 抽取 ``_publish_screen_narration`` /
``_discard_screen_narration`` 的旁白逻辑收拢于此：peek 陪伴对象电脑 → 按结果选
``[prompts]`` 旁白模板（screen_narration / offline / failed）→ 渲染 → 发布到
:class:`ScreenContextStore` 注入缓存。patrol 与 planning 问候链统一委托本服务，
行为语义（模板选择、文案渲染、降级口径）与抽取前逐字一致。

降级 resilient：cateye_client 缺失 / 功能关闭 / 整体超时 / 任何异常 → 不注入，
绝不阻塞主动触发（离线/失败仍按既有模板降级注入「电脑没开」/「没看清」旁白）。

隐私红线：本服务只把旁白发布进注入缓存，注入作用域由消费端
（companion.components 的请求 Hook）保证——只有陪伴目标私聊流才注入，
群聊与其他会话一律不注入；调用方（patrol / planning 问候链）负责只对目标私聊 peek。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from suite_prompts import (
    SCREEN_NARRATION_DEFAULT,
    SCREEN_NARRATION_FAILED_DEFAULT,
    SCREEN_NARRATION_OFFLINE_DEFAULT,
    prompt,
)

# ScreenContextStore 保留原位 companion/screen_context.py（实施者核实依赖方向后择「保留」）：
# 该缓存是注入消费端 companion.components 请求 Hook 的私有尾随锚点状态，且依赖
# companion.sender_identity 消息身份工具；若随迁 linkage 会形成 linkage↔companion 双向
# 模块级环（companion.components/patrol 经 companion/__init__  eager 消费 linkage）。保留原位
# 则 companion 侧零 linkage 模块级依赖（仅 services 装配期惰性 import），导入图无环。
# 本服务经 companion.screen_context 复用同一缓存类与 format_narration 渲染（文案逐字不变）。
from companion.screen_context import ScreenContextStore, format_narration
from .cateye_client import PEEK_OFFLINE

logger = logging.getLogger(__name__)

#: 适配器注册名（linkage 注册表键）
ADAPTER_NAME = "cateye_screen"


class ScreenPerceptionService:
    """陪伴对象电脑屏幕感知适配器（peek + 旁白渲染 + 注入缓存发布/丢弃）。

    构造接受 plugin（运行时动态读 ``[prompts]`` 模板与配置，热重载即时生效），
    并经显式注入持有 cateye 客户端与注入缓存（二者由 companion 服务束装配，
    手动屏幕工具与请求 Hook 仍消费同一实例）；``user_name_provider`` 惰性取目标
    显示名（旁白「你看了眼 X 的电脑屏幕」），取不到回退「陪伴对象」（与旧 patrol 同源）。
    """

    #: LinkageAdapter 协议：唯一名
    name = ADAPTER_NAME

    def __init__(
        self,
        plugin: Any,
        *,
        cateye: Any = None,
        screen_store: Optional[ScreenContextStore] = None,
        user_name_provider: Optional[Callable[[], str]] = None,
    ) -> None:
        self._plugin = plugin
        self._cateye = cateye  # CateyeClient，可能为 None（不看电脑）
        self._screen_store = screen_store  # ScreenContextStore，可能为 None（不注入旁白）
        self._user_name_provider = user_name_provider
        #: 最近一次成功发布的流（discard() 无参时撤这条；显式传 stream_id 优先）
        self._last_stream_id = ""

    # ---- LinkageAdapter 协议 ----
    def available(self) -> bool:
        """客户端与注入缓存均装配齐全才算可用（缺失消费方降级，不阻塞触发）。"""
        return self._cateye is not None and self._screen_store is not None

    def describe(self) -> str:
        return "陪伴对象电脑屏幕感知（connect-hub 截图 + VLM 转述 → 目标私聊旁白注入）"

    # ---- 内部工具 ----
    def _user_name(self) -> str:
        """惰性取目标显示名；提供者缺失/异常回退空串（format_narration 再兜底「陪伴对象」）。"""
        if self._user_name_provider is None:
            return ""
        try:
            return str(self._user_name_provider() or "")
        except Exception:
            return ""

    # ---- 对外入口（patrol / planning 问候链委托）----
    async def publish_narration(self, stream_id: str) -> bool:
        """看一眼陪伴对象电脑，把屏幕旁白发布到注入缓存（指定流）。

        三种结果对应 ``[prompts]`` 的三个模板，模板留空 = 该情况不注入；
        功能关闭 / 整体超时 / 任何异常 → 不注入，绝不阻塞主动触发。
        逻辑与抽取前 patrol._publish_screen_narration 逐字一致（仅流 id 改为入参、
        显示名经提供者惰性取）。返回 True 仅当旁白真实挂入缓存——调用方据此决定
        触发失败时是否 discard（评审修复：no-op 时不得误删他轮待注入旁白）。
        """
        try:
            if self._screen_store is None or self._cateye is None:
                return False
            sid = str(stream_id or "").strip()
            if not sid:
                return False
            now = datetime.now()
            peek = await self._cateye.peek_screen(now)
            if peek is None:
                return False
            if peek.ok:
                template = prompt(self._plugin, "screen_narration")
                default_template = SCREEN_NARRATION_DEFAULT
            elif peek.status == PEEK_OFFLINE:
                template = prompt(self._plugin, "screen_narration_offline")
                default_template = SCREEN_NARRATION_OFFLINE_DEFAULT
            else:
                template = prompt(self._plugin, "screen_narration_failed")
                default_template = SCREEN_NARRATION_FAILED_DEFAULT
            if not template.strip():
                return False
            text = format_narration(
                template,
                now=now,
                user_name=self._user_name(),
                description=peek.description,
                default_template=default_template,
            )
            if self._screen_store.publish(sid, text):
                self._last_stream_id = sid
                logger.info("屏幕旁白已挂入注入缓存（stream=%s，%s）：%s",
                            sid, peek.status, text)
                return True
            return False
        except Exception as e:  # 锦上添花，绝不阻塞触发
            logger.warning(f"发布屏幕旁白失败（忽略）: {e}")
            return False

    def discard(self, stream_id: Optional[str] = None) -> None:
        """撤掉待注入旁白；``stream_id`` 缺省撤最近一次发布的流（触发失败回滚用）。"""
        if self._screen_store is None:
            return
        sid = str(stream_id or "").strip() if stream_id is not None else self._last_stream_id
        if sid:
            self._screen_store.discard(sid)
        if sid and sid == self._last_stream_id:
            self._last_stream_id = ""


__all__ = ["ScreenPerceptionService", "ADAPTER_NAME"]