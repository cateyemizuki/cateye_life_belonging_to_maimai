"""智能日程注入子模块（来源：自主规划插件 handlers/inject/，照搬）。

提供意图分类、时机优化、上下文缓存等功能，
用于提升日程注入的智能化程度。

核心组件：
    - IntentClassifier: 用户意图分类器
    - InjectOptimizer: 注入时机优化器
    - ConversationContextCache: 对话上下文缓存

说明：原 ActivityStateAnalyzer（活动状态情绪短语）已按 MAINTENANCE_PLAN §4.2 行 8
随 M4 删除——精神状态行改读情绪系统的 ``mood_text()``（与 emotion 注入条目同源）。
"""

from .intent_classifier import IntentClassifier, UserIntent
from .inject_optimizer import InjectOptimizer
from .context_cache import ConversationContextCache, ConversationTurn

__all__ = [
    "IntentClassifier",
    "UserIntent",
    "InjectOptimizer",
    "ConversationContextCache",
    "ConversationTurn",
]
