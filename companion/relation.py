"""关系预设（简单版关系系统：配置「对方是 bot 的谁」，措辞随关系变化）。

设计：`companion/config.py` 的 PatrolSectionConfig 暴露 relation_type / relation_custom
两个配置项；本模块把它们解析成 RelationProfile（称谓、三档好感度名、口吻提示、
消息后缀池、括号小剧场），供 patrol（miss 提示词）、message_service（后缀）、
components（消息生成 prompt 与注入文案）消费。

兼容约定： affection.py 的 LEVEL_LABELS / LEVEL_DESCRIPTIONS 保留为「恋人」预设
的原始出处；运行时一律走这里的 profile。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

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


@dataclass
class RelationProfile:
    """从配置解析出的关系档案（组件/服务层只认这个）。"""

    word: str = "恋人"  # 对方是 bot 的谁（一句话称谓）
    level_labels: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_LEVEL_LABELS["恋人"]))
    level_descriptions: Dict[int, str] = field(default_factory=lambda: dict(_RELATION_LEVEL_DESCRIPTIONS["恋人"]))
    style_word: str = "温柔亲昵的恋人"
    self_desc: str = "一个温柔体贴的虚拟恋人"
    suffixes: Dict[int, List[str]] = field(default_factory=lambda: {k: list(v) for k, v in RELATION_SUFFIXES["恋人"].items()})
    theaters: List[str] = field(default_factory=lambda: list(RELATION_THEATERS["恋人"]))
    is_custom: bool = False

    def level_label(self, level: int) -> str:
        return self.level_labels.get(int(level), self.level_labels.get(0, "熟悉"))

    def level_description(self, level: int) -> str:
        return self.level_descriptions.get(int(level), self.level_descriptions.get(0, ""))

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
        style_word=_RELATION_STYLE[rel]["style"],
        self_desc=_RELATION_STYLE[rel]["self_desc"],
        suffixes={k: list(v) for k, v in RELATION_SUFFIXES[rel].items()},
        theaters=list(RELATION_THEATERS[rel]),
    )
