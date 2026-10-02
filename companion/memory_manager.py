"""本地记忆库（来源：虚拟恋人插件 memory_manager.py）。

记录和读取用户习惯，构造个性化上下文文案；底层委托给
AffectionManager 的 habits 段（见 companion/affection.py）。
"""

from __future__ import annotations

from typing import Any

from .affection import AffectionManager


class MemoryManager:
    """本地记忆库管理器：封装用户习惯读写与个性化上下文构造。"""

    def __init__(self, affection_manager: AffectionManager) -> None:
        self._affection = affection_manager

    def get_habit(self, key: str) -> Any:
        """获取指定习惯（如 'wednesday_overtime'）；不存在返回 None。"""
        return self._affection.habits().get(key)

    def set_habit(self, key: str, value: Any) -> None:
        """设置指定习惯。"""
        self._affection.set_habit(key, value)

    def all_habits(self) -> dict[str, Any]:
        """获取所有习惯。"""
        return self._affection.habits()

    def build_personal_context(self) -> str:
        """构造个性化上下文文案（如「用户习惯：wednesday overtime → 周三加班」）。

        无习惯时返回空串（与源插件的「暂无用户习惯记录。」占位不同——
        注入场景空串更好判断，调用方按需给兜底文案）。
        """
        habits = self.all_habits()
        if not habits:
            return ""
        parts = [f"用户习惯：{str(key).replace('_', ' ')} → {value}" for key, value in habits.items()]
        return "。".join(parts)
