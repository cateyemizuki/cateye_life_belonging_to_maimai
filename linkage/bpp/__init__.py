# -*- coding: utf-8 -*-
"""bpp 跨插件联动适配器 ``cateye_bpp``（R1 结构审计批，2026-11-12）。

收拢 life → bpp（更好的消息后处理）跨插件桥：

- ``client``   PostProcessBridge 认领客户端（``ctx.api.call`` claim/status/release），
  由 ``companion/post_process_bridge.py`` 逐字迁移（git 100% rename，旧文件删除、
  旧 import 清零；原本即纯 stdlib 依赖，import 路径无需适配）；
- ``adapter``  BppLinkageAdapter：实现 LinkageAdapter 协议（name/available()/
  describe()），封装 claim/release/typing_owner 动作面，注册为 linkage 适配器
  ``cateye_bpp``（screen 之后第二个）。

装配：服务束（companion/services.py）装配期惰性 import 本包 :func:`build_bpp_adapter`
构造适配器（与 screen 同模式，避免模块级环），组合根 ``on_load`` 只
``register_adapter``；消费方只 ``linkage.get_adapter("cateye_bpp")``，不直接 import
具体实现。本包零 companion import（linkage→companion 无新增边）。
"""

from __future__ import annotations

from typing import Any

from .adapter import ADAPTER_NAME, BppLinkageAdapter
from .client import (
    BPP_API_FULL_NAME,
    BPP_API_NAME,
    BPP_PLUGIN_ID,
    CLAIM_TIMEOUT_SECONDS,
    RELEASE_TIMEOUT_SECONDS,
    PostProcessBridge,
    text_fingerprint,
)


def build_bpp_adapter(plugin: Any) -> BppLinkageAdapter:
    """装配入口：构造认领客户端并封装为适配器（服务束调用，组合根只登记）。

    客户端动态读 ``plugin.config`` / ``plugin.ctx``（热重载即时生效），
    与迁移前 ``PostProcessBridge(plugin)`` 的依赖口径逐字一致。
    """
    return BppLinkageAdapter(client=PostProcessBridge(plugin))


__all__ = [
    "ADAPTER_NAME",
    "BPP_API_FULL_NAME",
    "BPP_API_NAME",
    "BPP_PLUGIN_ID",
    "BppLinkageAdapter",
    "CLAIM_TIMEOUT_SECONDS",
    "PostProcessBridge",
    "RELEASE_TIMEOUT_SECONDS",
    "build_bpp_adapter",
    "text_fingerprint",
]
