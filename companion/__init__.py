"""companion 子模块：虚拟陪伴功能（来源：maibot-community mai-love，MIT）。

模块一览：
- config            配置段（Patrol/Affection/CateyeScreen，供顶层聚合模型挂载）
- affection         好感度管理（含用户习惯 habits 存储）
- patrol            主动巡检调度器（2026-11-04 重写：仅状态驱动想念一类触发；
                    日程巡检已删除；早/午/晚安由 planning 驱动，patrol 只观察不触发）
- miss_trigger      想念概率公式（生活状态×情绪×好感，有界因子）与想念文案常量
- message_service   消息发送（好感度情绪后缀 + 括号小剧场）
- memory_manager    本地记忆库（用户习惯读写与个性化上下文）
- sender_identity   发送者身份缓存（消息 ID → QQ 号；会话 ID → 是否群聊）
- screen_context    屏幕旁白「尾随锚点」注入缓存（planner/replyer 双通道；3.17.16 核实
                    依赖方向后保留原位——注入消费端私有缓存，linkage.screen 经此复用）
- cateye_client     （3.17.16 迁 linkage/screen/）陪伴对象电脑联动（connect-hub 截图 + VLM 转述）
- components        CompanionComponentsMixin（5 Hook + 2 Tool + 4 Command + 3 API）
- services          CompanionServices 服务束（plugin.py: self._companion = CompanionServices(self)）
"""

from .affection import AffectionManager
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
    "MemoryManager",
    "MessageService",
    "PatrolService",
    "ScreenContextStore",
    "SenderCache",
    "SessionKindCache",
]
