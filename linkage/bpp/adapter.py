# -*- coding: utf-8 -*-
"""life → bpp 跨插件联动适配器 ``cateye_bpp``（R1 结构审计批，2026-11-12）。

把原 ``companion/post_process_bridge.py`` 的跨插件认领能力收拢为 linkage 适配器：
实现 :class:`linkage.LinkageAdapter` 协议（``name`` / ``available()`` / ``describe()``），
经组合根 ``on_load`` 装配期 ``register_adapter`` 登记；消费方
（``companion.components._mai_apply_typing_delay_with_bridge``）只经
``linkage.get_adapter("cateye_bpp")`` 查询，未登记/不可用按 None 降级（fail-open）。

依赖经注入 provider：客户端（:class:`PostProcessBridge`，自 companion 逐字迁移，
git 100% rename）由服务束装配后注入，或经 ``client_provider`` 惰性解析；本适配器
不反向持有 companion 业务流程/服务束，也不 import companion（linkage→companion
零边，比 screen 先例更干净）。认领/释放/打字归属的业务语义全部留在客户端内
逐字不变：1.5s 总超时（全名+短名共享截止）、全名 ``github.cateye.better-post-
processing.bpp_life_bridge`` → 短名 ``bpp_life_bridge`` 回退、任何失败 fail-open。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .client import PostProcessBridge

#: 适配器注册名（linkage 注册表键）
ADAPTER_NAME = "cateye_bpp"


class BppLinkageAdapter:
    """life → bpp 后处理桥适配器（认领/释放/打字归属让位的协议封装）。

    构造接受显式注入的客户端实例，或惰性 ``client_provider``（调用时才解析，
    返回 None = 能力未装配）；二者都缺省时所有动作按 fail-open 降级
    （claim/typing_owner → None，release → False，enabled → False）。
    """

    #: LinkageAdapter 协议：唯一名
    name = ADAPTER_NAME

    def __init__(
        self,
        client: Optional[PostProcessBridge] = None,
        *,
        client_provider: Optional[Callable[[], Optional[PostProcessBridge]]] = None,
    ) -> None:
        self._client = client
        self._client_provider = client_provider

    # ---- LinkageAdapter 协议 ----
    def available(self) -> bool:
        """客户端装配齐全且主开关开启才算可用（缺失/关闭消费方降级，不阻塞回复）。"""
        client = self._resolve_client()
        return client is not None and bool(client.enabled())

    def describe(self) -> str:
        return ("life → bpp 跨插件后处理桥（发送前认领 + typing_owner 让位打字；"
                "全名→短名回退、1.5s 总超时、任何失败 fail-open）")

    # ---- 内部工具 ----
    def _resolve_client(self) -> Optional[PostProcessBridge]:
        """惰性解析注入的客户端；provider 异常按未装配降级（绝不拖挂回复链）。"""
        if self._client is not None:
            return self._client
        if self._client_provider is not None:
            try:
                return self._client_provider()
            except Exception:
                return None
        return None

    # ---- 对 bpp 的动作面（签名与客户端逐字一致，消费方零语义迁移）----
    def enabled(self) -> bool:
        """``[experimental].bpp_bridge_enabled``（实验期主开关，默认关）。"""
        client = self._resolve_client()
        return bool(client.enabled()) if client is not None else False

    async def claim(
        self,
        stream_id: Any,
        text: Any,
        directive: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """登记本轮认领；返回 bpp 响应（含 ``active/accepted/typing_owner``）或 ``None``。"""
        client = self._resolve_client()
        if client is None:
            return None
        return await client.claim(stream_id, text, directive)

    async def typing_owner(self, stream_id: Any) -> Optional[str]:
        """只读查询当前打字归属（``status``，不会创建认领）。"""
        client = self._resolve_client()
        if client is None:
            return None
        return await client.typing_owner(stream_id)

    async def release(
        self,
        claim_id: Any = None,
        stream_id: Any = None,
    ) -> bool:
        """释放认领（幂等；失败静默）。供异常/空回复/跨轮清理使用。"""
        client = self._resolve_client()
        if client is None:
            return False
        return await client.release(claim_id=claim_id, stream_id=stream_id)

    def pending_claim(self, stream_id: Any) -> str:
        """该流最近一次被接受的 claim_id（诊断/测试）。"""
        client = self._resolve_client()
        return client.pending_claim(stream_id) if client is not None else ""


__all__ = ["ADAPTER_NAME", "BppLinkageAdapter"]
