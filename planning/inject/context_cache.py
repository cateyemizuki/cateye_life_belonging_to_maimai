"""对话上下文缓存模块

管理用户的对话历史，支持多轮对话的连续性判断。
用于智能注入决策，判断是否需要在连续对话中继续注入日程信息。
"""

from collections import deque
from typing import Dict, Optional, Deque
import logging
import time

from dataclasses import dataclass

from .intent_classifier import UserIntent


logger = logging.getLogger(__name__)


#: 「用户在询问日程」的意图证据集合（2026-11-04 反馈环修复）：只有用户消息的
#: 意图分类结果（query_current/query_future）才算话题证据。注入历史
#: （ConversationTurn.injected）是 bot 自身行为记录，不得反推「用户在追问」。
_SCHEDULE_QUERY_INTENT_VALUES = frozenset({
    UserIntent.QUERY_CURRENT.value,
    UserIntent.QUERY_FUTURE.value,
})


@dataclass
class ConversationTurn:
    """对话轮次数据类"""
    user_message: str      # 用户消息
    timestamp: float       # 时间戳
    intent: Optional[str]  # 识别的意图
    injected: bool         # 是否注入了日程
    activity: Optional[str] # 当时的活动


class ConversationContextCache:
    """对话上下文缓存

    功能：
    1. 记录用户最近N轮对话历史
    2. 判断是否在连续讨论日程相关话题
    3. 支持上下文连续注入决策

    设计：
    - 每个用户独立的对话历史队列
    - 固定窗口大小（默认3轮）
    - 自动过期清理（TTL=600秒）

    Attributes:
        max_turns: 每个用户保留的最大轮数
        ttl: 对话历史过期时间（秒）
        user_contexts: 用户对话历史字典
    """

    def __init__(self, max_turns: int = 3, ttl: int = 600):
        """初始化对话上下文缓存

        Args:
            max_turns: 每个用户保留的最大轮数，默认3轮
            ttl: 对话历史过期时间（秒），默认600秒（10分钟）
        """
        self.max_turns = max_turns
        self.ttl = ttl
        self.user_contexts: Dict[str, Deque[ConversationTurn]] = {}
        self._last_cleanup = time.time()

        logger.debug(
            f"对话上下文缓存初始化完成: max_turns={max_turns}, ttl={ttl}秒"
        )

    def add_turn(
        self,
        user_id: str,
        user_message: str,
        intent: Optional[str] = None,
        injected: bool = False,
        activity: Optional[str] = None
    ):
        """添加一轮对话记录

        Args:
            user_id: 用户ID
            user_message: 用户消息内容
            intent: 识别的意图
            injected: 是否注入了日程
            activity: 当时的活动
        """
        # 创建对话轮次
        turn = ConversationTurn(
            user_message=user_message,
            timestamp=time.time(),
            intent=intent,
            injected=injected,
            activity=activity
        )

        # 获取或创建用户的对话队列
        if user_id not in self.user_contexts:
            self.user_contexts[user_id] = deque(maxlen=self.max_turns)

        # 添加到队列（自动淘汰最旧的）
        self.user_contexts[user_id].append(turn)

        logger.debug(
            f"记录对话: user={user_id}, "
            f"message='{user_message[:30]}...', "
            f"intent={intent}, injected={injected}"
        )

        # 定期清理过期缓存
        if time.time() - self._last_cleanup > 300:  # 每5分钟清理一次
            self.cleanup_expired()

    def get_recent_turns(
        self,
        user_id: str,
        count: Optional[int] = None
    ) -> list[ConversationTurn]:
        """获取用户最近的对话历史

        Args:
            user_id: 用户ID
            count: 获取的轮数，默认全部

        Returns:
            对话轮次列表（从旧到新）
        """
        if user_id not in self.user_contexts:
            return []

        turns = list(self.user_contexts[user_id])

        # 过滤过期的
        current_time = time.time()
        turns = [t for t in turns if current_time - t.timestamp < self.ttl]

        # 限制数量
        if count:
            turns = turns[-count:]

        return turns

    def is_schedule_topic_ongoing(self, user_id: str) -> bool:
        """判断用户是否在连续讨论日程话题

        规则（2026-11-04 反馈环修复）：
        1. 最近2轮中至少有1轮的用户意图被分类为询问日程
           （query_current / query_future）——只有用户消息意图算话题证据；
        2. 最近1轮的消息在60秒内（对话仍活跃）。

        旧版以「最近2轮是否注入过日程」为判据、却把 reason 标成「用户在追问
        日程相关内容」：bot 上一轮自己被注入/提了日程，下一轮就被误判成用户在
        追问，绕过闲聊概率直接强制再注入，形成「注入→误判追问→再注入」的自我
        强化环（生产转储 E1/E6 两次实测命中该假 reason）。注入历史是 bot 自身
        行为记录，不得作为用户意图证据；现改为要求近期轮次确有询问意图。

        Args:
            user_id: 用户ID

        Returns:
            True表示用户近期询问过日程且对话仍活跃，False表示不是

        Examples:
            >>> cache.add_turn("user1", "你在干嘛？", intent="query_current", injected=True)
            >>> cache.add_turn("user1", "学什么呢？", intent="query_current")
            >>> cache.is_schedule_topic_ongoing("user1")
            True  # 近期轮次确有询问意图
            >>> cache.add_turn("user1", "哈哈", intent="casual_chat")
            >>> cache.add_turn("user1", "不错", intent="casual_chat", injected=True)
            >>> cache.is_schedule_topic_ongoing("user1")
            False  # 注入历史不算追问证据
        """
        recent_turns = self.get_recent_turns(user_id, count=2)

        if not recent_turns:
            return False

        # 检查最近1轮是否在60秒内（对话仍活跃）
        current_time = time.time()
        if current_time - recent_turns[-1].timestamp > 60:
            return False

        # 话题证据 = 近期轮次的用户意图为询问日程；injected 标志不参与判定
        query_count = sum(
            1 for turn in recent_turns
            if turn.intent in _SCHEDULE_QUERY_INTENT_VALUES
        )

        if query_count > 0:
            logger.debug(
                f"检测到连续日程话题: user={user_id}, "
                f"最近{len(recent_turns)}轮中{query_count}轮为询问意图"
            )
            return True

        return False

    def get_last_activity(self, user_id: str) -> Optional[str]:
        """获取用户上次对话时的活动

        用于判断活动是否发生变化，辅助注入决策。

        Args:
            user_id: 用户ID

        Returns:
            活动名称，如果没有历史则返回None
        """
        recent_turns = self.get_recent_turns(user_id, count=1)
        if recent_turns and recent_turns[0].activity:
            return recent_turns[0].activity
        return None

    def should_continue_inject(
        self,
        user_id: str,
        current_activity: Optional[str]
    ) -> tuple[bool, Optional[str]]:
        """判断是否应该在连续对话中继续注入

        决策逻辑：
        1. 如果用户近期询问过日程（意图证据，非注入历史）且对话活跃 → 继续注入
        2. 如果活动发生了变化 → 注入新活动
        3. 其他情况 → 交给常规决策

        Args:
            user_id: 用户ID
            current_activity: 当前活动

        Returns:
            (是否继续注入, 原因说明)
        """
        # 规则1：连续讨论日程话题（2026-11-04 起以用户询问意图为证据，
        # reason 文案与判据一致——不再把「上轮注入过」谎称为「用户在追问」）
        if self.is_schedule_topic_ongoing(user_id):
            return True, "连续对话：用户近期询问过日程相关内容"

        # 规则2：活动发生变化
        last_activity = self.get_last_activity(user_id)
        if last_activity and current_activity and last_activity != current_activity:
            return True, f"活动变化：{last_activity} → {current_activity}"

        return False, None

    def cleanup_expired(self):
        """清理过期的对话历史"""
        current_time = time.time()
        expired_users = []

        for user_id, turns in self.user_contexts.items():
            # 过滤过期的轮次
            valid_turns = deque(
                (turn for turn in turns if current_time - turn.timestamp < self.ttl),
                maxlen=self.max_turns
            )

            if len(valid_turns) == 0:
                expired_users.append(user_id)
            else:
                self.user_contexts[user_id] = valid_turns

        # 删除完全过期的用户
        for user_id in expired_users:
            del self.user_contexts[user_id]

        if expired_users:
            logger.debug(f"清理过期对话历史: 删除{len(expired_users)}个用户")

        self._last_cleanup = current_time

    def clear_user_context(self, user_id: str):
        """清除指定用户的对话历史

        Args:
            user_id: 用户ID
        """
        if user_id in self.user_contexts:
            del self.user_contexts[user_id]
            logger.debug(f"清除用户对话历史: user={user_id}")
