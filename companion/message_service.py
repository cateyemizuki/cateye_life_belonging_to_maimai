"""消息发送服务（来源：虚拟恋人插件 message_service.py）。

封装：
1. 按好感度档位追加情绪锚点后缀（档位 2 有 10% 概率额外追加括号小剧场）；
2. 调用 ctx.send.text 发送文本消息；
3. 主动发言时更新发言计数与冷却基准。

主动聊天主链路走 maisaka.proactive.trigger（patrol.py），本服务主要供
mai_lover_send_message Tool 直发消息使用。
"""

from __future__ import annotations

import random
from typing import Any

from .affection import AffectionManager

# 情绪锚点后缀池（按好感度档位；「恋人」预设原文，运行时优先用 RelationProfile）
AFFECTION_SUFFIXES: dict[int, list[str]] = {
    0: ["~", "哦", "呢", "哈"],
    1: ["~❤️", "啦！", "嘿嘿~", "嗯呐~"],
    2: ["~抱抱", "亲亲~", "想你啦~", "mua~"],
}

# 档位 2 额外括号小剧场（10% 概率触发；{name} = 恋人名称）
BRACKET_THEATERS: list[str] = [
    "(虽然知道你在忙但还是想你了呢)",
    "(偷偷亲你一下应该没人发现吧)",
    "(今天也是想见你的一天)",
    "({name}今天也超级喜欢你哦~)",
]


class MessageService:
    """消息发送服务：情绪后缀 + 实际发送 + 计数更新。"""

    def __init__(self, plugin: Any, affection: AffectionManager, lover_name: str = "麦麦") -> None:
        self._plugin = plugin
        self._ctx = plugin.ctx
        self._affection = affection
        self._lover_name = lover_name or "麦麦"
        self._relation = None  # companion.relation.RelationProfile（注入后按关系取后缀/小剧场）

    def set_lover_name(self, name: str) -> None:
        if name:
            self._lover_name = name

    def set_relation(self, relation: Any) -> None:
        """注入关系档案（后缀池与小剧场随关系类型变化）。"""
        if relation is not None:
            self._relation = relation

    async def send_to_target(self, text: str, stream_id: str, is_proactive: bool = True) -> bool:
        """发送消息给目标用户；主动发言成功后计 1 次发言数。"""
        if not stream_id:
            self._ctx.logger.error("stream_id 为空，无法发送消息")
            return False
        final_text = self.append_affection_suffix(text)
        try:
            result = await self._ctx.send.text(text=final_text, stream_id=stream_id)
            if result:
                if is_proactive:
                    self._affection.increment_speak()
                    self._ctx.logger.info("主动消息发送成功: %s...", final_text[:50])
                else:
                    self._ctx.logger.info("被动回复发送成功: %s...", final_text[:50])
                return True
            self._ctx.logger.error("消息发送失败: 发送返回 False")
            return False
        except Exception as e:
            self._ctx.logger.error(f"消息发送异常: {e}")
            return False

    def append_affection_suffix(self, text: str) -> str:
        """根据好感度档位追加情绪锚点后缀；档位 2 有 10% 概率追加括号小剧场。

        后缀池与小剧场来自关系档案（RelationProfile）——恋人/挚友/维护者/家人各有各的说法。
        """
        level = self._affection.level()
        relation = self._relation
        if relation is not None:
            suffixes = relation.suffix_for(level)
            theaters = relation.theaters
        else:  # 未注入关系档案时回退「恋人」预设
            suffixes = AFFECTION_SUFFIXES.get(level, AFFECTION_SUFFIXES[0])
            theaters = BRACKET_THEATERS
        suffix = self._random_suffix(suffixes)
        result = text + suffix
        if level == 2 and theaters and random.random() < 0.1:
            theater = self._random_suffix(theaters)
            try:
                theater = theater.format(name=self._lover_name)
            except Exception:
                pass
            result = result + theater
        return result

    @staticmethod
    def _random_suffix(suffixes: list[str]) -> str:
        if not suffixes:
            return ""
        return random.choice(suffixes)
