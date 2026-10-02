"""companion 子模块：虚拟恋人功能（来源：maibot-community mai-love，MIT）。

模块一览：
- config            配置段（Patrol/Affection/CateyeScreen，供顶层聚合模型挂载）
- affection         好感度管理（含用户习惯 habits 存储）
- patrol            主动巡检调度器（早安/晚安/想念/节点分享/日常搭话完整判定链）
- message_service   消息发送（好感度情绪后缀 + 括号小剧场）
- memory_manager    本地记忆库（用户习惯读写与个性化上下文）
- sender_identity   发送者身份缓存（消息 ID → QQ 号；会话 ID → 是否群聊）
- screen_context    屏幕旁白「尾随锚点」注入缓存（planner/replyer 双通道）
- cateye_client     恋人电脑联动（connect-hub 截图 + VLM 转述）
- holiday_service   节假日/调休查询（多级降级 + 3 天 TTL 磁盘缓存）
- components        CompanionComponentsMixin（5 Hook + 6 Tool + 8 Command + 3 API）
- services          CompanionServices 服务束（plugin.py: self._companion = CompanionServices(self)）
"""

from .affection import AffectionManager, LEVEL_DESCRIPTIONS, LEVEL_LABELS
from .components import CompanionComponentsMixin
from .memory_manager import MemoryManager
from .message_service import MessageService
from .patrol import PatrolService
from .screen_context import ScreenContextStore
from .sender_identity import SenderCache, SessionKindCache
from .services import CompanionServices

__all__ = [
    "AffectionManager",
    "CompanionComponentsMixin",
    "CompanionServices",
    "LEVEL_DESCRIPTIONS",
    "LEVEL_LABELS",
    "MemoryManager",
    "MessageService",
    "PatrolService",
    "ScreenContextStore",
    "SenderCache",
    "SessionKindCache",
]
