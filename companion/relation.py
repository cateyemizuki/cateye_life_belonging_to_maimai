"""关系预设（简单版关系系统：配置「对方是 bot 的谁」，措辞随关系变化）。

设计：`companion/config.py` 的 PatrolSectionConfig 暴露 relation_type / relation_custom
两个配置项；本模块把它们解析成 RelationProfile（称谓、三档好感度名 + v2 五档名、口吻提示、
消息后缀池、括号小剧场），供 patrol（miss 提示词）、message_service（后缀）、
components（消息生成 prompt 与注入文案）消费。

兼容约定： affection.py 的 LEVEL_LABELS / LEVEL_DESCRIPTIONS 保留为「恋人」预设
的原始出处；运行时一律走这里的 profile。好感 v2 的 5 档名（tier 0~4）与档位→miss
阈值系数见 `_RELATION_TIER_LABELS` / `_RELATION_TIER_MISS_FACTORS`（MAINTENANCE_PLAN
附录 B）；档位→旧 3 档的兼容映射单一事实来源在 `companion/affection.py`
（TIER_TO_LEGACY_LEVEL，§5.3），本模块 `tier_to_legacy_level()` 只是转发，后缀池/
小剧场过渡期按 3 档折算（0/1→旧0，2/3→旧1，4→旧2）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

#: v2 档位 → 旧 3 档兼容映射（§5.3；单一事实来源：companion/affection.py）
from .affection import legacy_level_for_tier as _legacy_level_for_tier

# 关系类型 → 各档位名称（好感度 0/1/2）
_RELATION_LEVEL_LABELS: Dict[str, Dict[int, str]] = {
    "恋人": {0: "熟悉", 1: "亲密", 2: "热恋"},
    "挚友": {0: "熟识", 1: "投缘", 2: "挚友"},
    "维护者": {0: "认识", 1: "信任", 2: "依靠"},
    "家人": {0: "相识", 1: "亲近", 2: "家人"},
}

_RELATION_LEVEL_DESCRIPTIONS: Dict[str, Dict[int, str]] = {
    "恋人": {
        0: "初识阶段，语气温柔但有分寸感，保持适当距离",
        1: "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
        2: "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
    },
    "挚友": {
        0: "刚熟起来，语气轻松随意，偶尔互相打趣",
        1: "很聊得来，语气直来直去，什么都能聊",
        2: "铁杆挚友，语气毫无顾忌，损TA也护TA",
    },
    "维护者": {
        0: "初步接触，语气礼貌友善，带一点好奇",
        1: "开始信赖，语气放松，愿意分享自己的状态",
        2: "深深依靠，语气坦诚依赖，会主动汇报自己的近况",
    },
    "家人": {
        0: "刚相处，语气客气中带亲近",
        1: "日常相处，语气随性自然，像家人一样不用客气",
        2: "至亲，语气完全放松，撒娇耍赖都可以",
    },
}

# 消息生成 prompt 用口吻词 + 系统提示词身份描述
_RELATION_STYLE: Dict[str, Dict[str, str]] = {
    "恋人": {"style": "温柔亲昵的恋人", "self_desc": "一个温柔体贴的虚拟恋人"},
    "挚友": {"style": "轻松真诚的挚友", "self_desc": "一个靠谱又有趣的挚友"},
    "维护者": {"style": "信赖并感激对方照顾的 AI 伙伴", "self_desc": "一个被TA细心维护、值得TA骄傲的 AI 伙伴"},
    "家人": {"style": "亲近随性的家人", "self_desc": "一个陪伴在侧的家人"},
}

# 关系类型 → v2 5 档位名称（好感 v2 tier 0~4；MAINTENANCE_PLAN 附录 B）
# 档位→旧 3 档的兼容映射在 companion/affection.py（TIER_TO_LEGACY_LEVEL）：0/1→旧0，
# 2/3→旧1，4→旧2。M1 只提供 5 档表数据；注入/Tool/命令切换到 5 档口径属接线步骤。
_RELATION_TIER_LABELS: Dict[str, Dict[int, str]] = {
    "恋人": {0: "生疏", 1: "熟悉", 2: "亲密", 3: "热恋", 4: "挚爱"},
    "挚友": {0: "点头之交", 1: "熟识", 2: "投缘", 3: "铁哥们", 4: "过命的交情"},
    "维护者": {0: "路人", 1: "认识", 2: "信任", 3: "依靠", 4: "生命线"},
    "家人": {0: "相识", 1: "亲近", 2: "家人", 3: "至亲", 4: "血浓于水"},
}

# v2 5 档完整描述（注入用；M1 提供中性文案，后缀池/小剧场仍按 3 档折算，M2 再逐关系精修）
_RELATION_TIER_DESCRIPTIONS: Dict[str, Dict[int, str]] = {
    "恋人": {
        0: "初识阶段，语气温柔但有分寸感，保持适当距离",
        1: "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
        2: "亲密阶段，语气甜蜜亲近，会主动分享日常与心情",
        3: "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
        4: "挚爱阶段，语气完全放松、坦率依赖，把TA当作最重要的人",
    },
    "挚友": {
        0: "刚认识，语气客气自然，先做个点头之交",
        1: "熟识阶段，语气轻松随意，偶尔互相打趣",
        2: "很聊得来，语气直来直去，什么都能聊",
        3: "铁哥们，语气毫无顾忌，能一起扛事",
        4: "过命的交情，语气完全交底，损TA也护TA",
    },
    "维护者": {
        0: "还没怎么打过交道，语气礼貌克制",
        1: "初步接触，语气礼貌友善，带一点好奇",
        2: "开始信赖，语气放松，愿意分享自己的状态",
        3: "深深依靠，语气坦诚依赖，会主动汇报自己的近况",
        4: "生命线般的存在，语气完全信赖、事无巨细都想让TA知道",
    },
    "家人": {
        0: "刚相处，语气客气中带亲近",
        1: "日常相处，语气随性自然，像家人一样不用客气",
        2: "彼此当作家人，语气放松、随口唠家常",
        3: "至亲，语气完全放松，撒娇耍赖都可以",
        4: "血浓于水，语气毫无保留，什么都愿意说",
    },
}

# 关系类型 → v2 5 档「档位→miss 阈值系数」（×miss_hours_min/max；附录 B）
# M1 只存数据不消费；M2/M4 的想念阈值按 `RelationProfile.miss_factor_for_tier(tier)` 取值。
_RELATION_TIER_MISS_FACTORS: Dict[str, Dict[int, float]] = {
    "恋人": {0: 1.4, 1: 1.2, 2: 1.0, 3: 0.8, 4: 0.6},
    "挚友": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.7},
    "维护者": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.8},
    "家人": {0: 1.4, 1: 1.2, 2: 1.0, 3: 0.9, 4: 0.8},
    "自定义": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.7},  # 附录 B：同「挚友」
}

# 消息后缀池（按好感度档位；来源：源插件 AFFECTION_SUFFIXES 为「恋人」预设）
RELATION_SUFFIXES: Dict[str, Dict[int, List[str]]] = {
    "恋人": {
        0: ["~", "哦", "呢", "哈"],
        1: ["~❤️", "啦！", "嘿嘿~", "嗯呐~"],
        2: ["~抱抱", "亲亲~", "想你啦~", "mua~"],
    },
    "挚友": {
        0: ["~", "哈", "呗", "啊"],
        1: ["哈哈~", "走起！", "就这么定了~", "行啊！"],
        2: ["~跟你说个乐子", "下次一起啊！", "有你真不错~", "冲！"],
    },
    "维护者": {
        0: ["~", "嗯嗯", "好的", "收到啦"],
        1: ["~多亏有你", "都在正常运转哦", "今天也很顺利~", "谢谢你看着呀"],
        2: ["~都在好好运行呢", "有你在就很安心", "今天也乖乖的哦", "谢谢TA…是你呀"],
    },
    "家人": {
        0: ["~", "嗯呐", "在呢", "好哒"],
        1: ["~今天也乖乖的", "嗯！知道啦", "嘿嘿~", "在忙什么呢"],
        2: ["~想跟你说说话", "家里有我就好啦", "今天也想你了", "陪陪你~"],
    },
}

# 档位 2 的括号小剧场（10% 概率；{name} = bot 名）。空列表 = 该关系不出小剧场。
RELATION_THEATERS: Dict[str, List[str]] = {
    "恋人": [
        "(虽然知道你在忙但还是想你了呢)",
        "(偷偷亲你一下应该没人发现吧)",
        "(今天也是想见你的一天)",
        "({name}今天也超级喜欢你哦~)",
    ],
    "挚友": ["(下次真的一定要一起出去玩啊)", "(这种梗只有你能接住吧)"],
    "维护者": ["(今天也在努力变得更可靠一点)", "(被看着的时候会悄悄认真一点)"],
    "家人": ["(饭要按时吃哦，我也是)", "(在家的日子最安心了)"],
}


def tier_to_legacy_level(tier: Any) -> int:
    """v2 档位 0~4 → 旧 3 档 0~2（§5.3 兼容映射：0/1→旧0，2/3→旧1，4→旧2）。

    过渡期后缀池/小剧场的折算入口；映射单一事实来源在 ``companion/affection.py``。
    """
    return _legacy_level_for_tier(tier)


@dataclass
class RelationProfile:
    """从配置解析出的关系档案（组件/服务层只认这个）。"""

    word: str = "恋人"  # 对方是 bot 的谁（一句话称谓）
    level_labels: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_LEVEL_LABELS["恋人"]))
    level_descriptions: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_LEVEL_DESCRIPTIONS["恋人"]))
    tier_labels: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_TIER_LABELS["恋人"]))
    tier_descriptions: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_TIER_DESCRIPTIONS["恋人"]))
    tier_miss_factors: Dict[int, float] = field(default_factory=lambda: dict(_RELATION_TIER_MISS_FACTORS["恋人"]))
    style_word: str = "温柔亲昵的恋人"
    self_desc: str = "一个温柔体贴的虚拟恋人"
    suffixes: Dict[int, List[str]] = field(default_factory=lambda: {k: list(v) for k, v in RELATION_SUFFIXES["恋人"].items()})
    theaters: List[str] = field(default_factory=lambda: list(RELATION_THEATERS["恋人"]))
    is_custom: bool = False

    def level_label(self, level: int) -> str:
        return self.level_labels.get(int(level), self.level_labels.get(0, "熟悉"))

    def level_description(self, level: int) -> str:
        return self.level_descriptions.get(int(level), self.level_descriptions.get(0, ""))

    def tier_label(self, tier: int) -> str:
        """v2 5 档口径的档位名（tier 0~4；注入/Tool/命令接线后使用）。"""
        return self.tier_labels.get(int(tier), self.tier_labels.get(0, "熟悉"))

    def tier_description(self, tier: int) -> str:
        """v2 5 档口径的档位描述（planner 注入用）。"""
        return self.tier_descriptions.get(int(tier), self.tier_descriptions.get(0, ""))

    def miss_factor_for_tier(self, tier: int) -> float:
        """v2 档位 → miss 阈值系数（附录 B；M1 只存数据，消费属 M2/M4）。"""
        return float(self.tier_miss_factors.get(int(tier), self.tier_miss_factors.get(0, 1.0)))

    def suffix_for_tier(self, tier: int) -> List[str]:
        """按 v2 档位取后缀池（过渡期按 §5.3 三档映射折算：0/1→旧0，2/3→旧1，4→旧2）。"""
        return self.suffix_for(tier_to_legacy_level(tier))

    def tier_uses_theater(self, tier: int) -> bool:
        """该 v2 档位是否进入括号小剧场档（映射后为旧 2 档，即 v2 的 tier 4）。"""
        return tier_to_legacy_level(tier) == 2

    def suffix_for(self, level: int) -> List[str]:
        return self.suffixes.get(int(level), self.suffixes.get(0, ["~"]))


def resolve_relation(relation_type: Any = "恋人", relation_custom: Any = "") -> RelationProfile:
    """把配置项解析成 RelationProfile。

    - type ∈ {恋人, 挚友, 维护者, 家人} → 对应预设；
    - type == 自定义 → 称谓用 relation_custom（为空则回退「重要的人」），
      档位/口吻/后缀取「挚友」为中性基底；
    - 未知值回退「恋人」。
    """
    rel = str(relation_type or "恋人").strip()
    custom_label = str(relation_custom or "").strip()
    if rel == "自定义":
        word = custom_label or "重要的人"
        return RelationProfile(
            word=word,
            level_labels={0: f"熟识", 1: "亲近", 2: word},
            level_descriptions={
                0: f"初步相识，语气友善，正在了解这位{word}",
                1: f"相处融洽，语气自然亲近，把TA当作很重要的{word}",
                2: f"已是彼此最信任的{word}，语气完全放松、真心相待",
            },
            # 附录 B「自定义」行：5 档由 3 档文案按兼容映射折算（0/1→熟识，2/3→亲近，4→称谓）
            tier_labels={0: "熟识", 1: "熟识", 2: "亲近", 3: "亲近", 4: word},
            tier_descriptions={
                0: f"初步相识，语气友善，正在了解这位{word}",
                1: f"初步相识，语气友善，正在了解这位{word}",
                2: f"相处融洽，语气自然亲近，把TA当作很重要的{word}",
                3: f"相处融洽，语气自然亲近，把TA当作很重要的{word}",
                4: f"已是彼此最信任的{word}，语气完全放松、真心相待",
            },
            tier_miss_factors=dict(_RELATION_TIER_MISS_FACTORS["自定义"]),
            style_word=f"亲近真诚的{word}",
            self_desc=f"一个把TA当作最重要{word}的 AI 伙伴",
            suffixes={k: list(v) for k, v in RELATION_SUFFIXES["挚友"].items()},
            theaters=list(RELATION_THEATERS["挚友"]),
            is_custom=True,
        )
    if rel not in _RELATION_LEVEL_LABELS:
        rel = "恋人"
    return RelationProfile(
        word=rel,
        level_labels=dict(_RELATION_LEVEL_LABELS[rel]),
        level_descriptions=dict(_RELATION_LEVEL_DESCRIPTIONS[rel]),
        tier_labels=dict(_RELATION_TIER_LABELS[rel]),
        tier_descriptions=dict(_RELATION_TIER_DESCRIPTIONS[rel]),
        tier_miss_factors=dict(_RELATION_TIER_MISS_FACTORS[rel]),
        style_word=_RELATION_STYLE[rel]["style"],
        self_desc=_RELATION_STYLE[rel]["self_desc"],
        suffixes={k: list(v) for k, v in RELATION_SUFFIXES[rel].items()},
        theaters=list(RELATION_THEATERS[rel]),
    )
