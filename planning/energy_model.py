"""时段能量基线模型（来源：自主规划插件 utils/energy_model.py，照搬）。

按 24 小时时段映射能量值（0-100），用于注入时提示 LLM 当前 bot 的精神状态。
参考真实人类作息：凌晨低 / 上午高峰 / 午后微疲 / 下午次高峰 / 晚间放松 / 深夜撑不住。
"""

from __future__ import annotations

# 24 小时能量曲线（参考真实人类作息）
_ENERGY_TABLE: dict[int, int] = {
    0: 15, 1: 12, 2: 10, 3: 10, 4: 12, 5: 20,
    6: 50, 7: 60, 8: 70, 9: 80, 10: 85, 11: 88,
    12: 70, 13: 60, 14: 65, 15: 75, 16: 75, 17: 70,
    18: 65, 19: 70, 20: 65, 21: 55, 22: 45, 23: 30,
}


def get_energy_level(hour: int) -> int:
    """根据 24 小时制小时数返回能量值（0-100，数字越大越精神）。"""
    clamped = max(0, min(23, int(hour)))
    return _ENERGY_TABLE[clamped]


def describe_energy(energy: int) -> str:
    """把数值能量值映射为简短自然语言描述（5 档）。"""
    if energy >= 80:
        return "精神满满"
    if energy >= 60:
        return "状态不错"
    if energy >= 40:
        return "正常"
    if energy >= 25:
        return "有点累"
    if energy >= 10:
        return "困了"
    return "快撑不住"
