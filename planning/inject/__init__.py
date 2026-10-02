"""智能日程注入子模块（来源：自主规划插件 handlers/inject/，照搬）。

提供意图分类、时机优化、活动状态分析、上下文缓存等功能，
用于提升日程注入的智能化程度。

核心组件：
    - IntentClassifier: 用户意图分类器
    - InjectOptimizer: 注入时机优化器
    - ActivityStateAnalyzer: 活动状态分析器
    - ConversationContextCache: 对话上下文缓存
"""

from .intent_classifier import IntentClassifier, UserIntent
from .inject_optimizer import InjectOptimizer
from .state_analyzer import ActivityStateAnalyzer, ActivityState
from .context_cache import ConversationContextCache, ConversationTurn

__all__ = [
    "IntentClassifier",
    "UserIntent",
    "InjectOptimizer",
    "ActivityStateAnalyzer",
    "ActivityState",
    "ConversationContextCache",
    "ConversationTurn",
]
