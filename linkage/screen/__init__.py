"""屏幕感知联动适配器 ``cateye_screen``（PR-LINKAGE-SCREEN，3.17.16）。

收拢陪伴对象电脑屏幕感知链路：

- ``cateye_client``  陪伴对象电脑联动客户端（connect-hub 截图 + VLM 转述），
  由 ``companion/cateye_client.py`` 逐字迁移（原本即绝对导入，import 路径无需适配）；
- ``service``        ScreenPerceptionService：peek + 旁白模板渲染 + 发布/丢弃，
  从 patrol 抽取，注册为 linkage 首个适配器 ``cateye_screen``。

ScreenContextStore（尾随锚点注入缓存）与 format_narration（文案渲染）**保留原位**
``companion/screen_context.py``（依赖方向核实：注入消费端 companion.components 的私有
缓存 + 依赖 companion.sender_identity；随迁会形成 linkage↔companion 模块级环）。本包
自 companion.screen_context 重导出便于联动层消费，注入端 components 仍走原 companion 路径。
"""

from __future__ import annotations

from companion.screen_context import (
    CHANNEL_PLANNER,
    CHANNEL_REPLYER,
    CHANNELS,
    ScreenContextStore,
    build_narration_item,
    format_narration,
)
from .cateye_client import (
    HUB_API_SCREENSHOT,
    HUB_API_STATUS,
    PEEK_FAILED,
    PEEK_OFFLINE,
    PEEK_OK,
    CateyeClient,
    ScreenPeek,
)
from .service import ADAPTER_NAME, ScreenPerceptionService

__all__ = [
    "ADAPTER_NAME",
    "CHANNELS",
    "CHANNEL_PLANNER",
    "CHANNEL_REPLYER",
    "CateyeClient",
    "HUB_API_SCREENSHOT",
    "HUB_API_STATUS",
    "PEEK_FAILED",
    "PEEK_OFFLINE",
    "PEEK_OK",
    "ScreenContextStore",
    "ScreenPeek",
    "ScreenPerceptionService",
    "build_narration_item",
    "format_narration",
]