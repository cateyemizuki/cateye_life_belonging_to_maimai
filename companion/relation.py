"""关系预设（简单版关系系统：配置「对方是 bot 的谁」，措辞随关系变化）。

设计：`companion/config.py` 的 PatrolSectionConfig 暴露 relation_type / relation_custom
两个配置项；本模块把它们解析成 RelationProfile（称谓、档位标签、口吻提示、消息后缀池、
括号小剧场），供 patrol（miss 提示词）、message_service（后缀）、components（消息生成
prompt 与注入文案）消费。

2026-10-17 §19（用户指示，R7 依据）：
- **v1 三档体系移除**——`_RELATION_LEVEL_LABELS/_RELATION_LEVEL_DESCRIPTIONS`、
  ``tier_to_legacy_level`` 固定映射删除；后缀池/小剧场改按**档位相对位置（ratio）折算**
  到旧 3 档桶（5 档时与旧映射逐档一致）；
- **档位标签/区间的单一真源 = 生效预设表**（``companion/affection_presets``：外置
  ``mappings/affection_presets.json``，1~5 档自由构造，内置五预设兜底）；
  本模块不再自持 5 档标签/描述常量（描述内置表在 ``companion/affection_tier_descriptions``）；
- 档位 → miss 阈值系数按 ratio×4 折算回内置 5 档系数表（``_RELATION_TIER_MISS_FACTORS``，
  MAINTENANCE_PLAN 附录 B；5 档时逐档一致，其他档位数平滑折算）；
- ``[patrol].relation_type`` 的可选值 = 内置五预设 ∪ json 预设（动态下拉，见 config.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from . import affection_presets
from .affection_tier_descriptions import generic_tier_description, tier_description_for

# 消息生成 prompt 用口吻词 + 系统提示词身份描述
_RELATION_STYLE: Dict[str, Dict[str, str]] = {
    "恋人": {"style": "温柔亲昵的陪伴者", "self_desc": "一个温柔体贴的虚拟陪伴者"},
    "挚友": {"style": "轻松真诚的挚友", "self_desc": "一个靠谱又有趣的挚友"},
    "维护者": {"style": "信赖并感激对方照顾的 AI 伙伴", "self_desc": "一个被TA细心维护、值得TA骄傲的 AI 伙伴"},
    "家人": {"style": "亲近随性的家人", "self_desc": "一个陪伴在侧的家人"},
}

# 关系类型 → 内置 5 档「档位→miss 阈值系数」（×miss_hours_min/max；附录 B）。
# 档位数 ≠ 5 时按 ratio×4 折算（miss_factor_for_tier）；「自定义」同「挚友」。
_RELATION_TIER_MISS_FACTORS: Dict[str, Dict[int, float]] = {
    "恋人": {0: 1.4, 1: 1.2, 2: 1.0, 3: 0.8, 4: 0.6},
    "挚友": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.7},
    "维护者": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.8},
    "家人": {0: 1.4, 1: 1.2, 2: 1.0, 3: 0.9, 4: 0.8},
    "自定义": {0: 1.5, 1: 1.3, 2: 1.1, 3: 0.9, 4: 0.7},
}

# 消息后缀池（按 3 档桶；来源：源插件 AFFECTION_SUFFIXES 为「恋人」预设；
# 档位数 ≠ 3 的预设按 ratio 折算取桶）
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

# 桶 2 的括号小剧场（10% 概率；{name} = bot 名）。空列表 = 该关系不出小剧场。
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


def ratio_bucket_index(tier: int, size: int, buckets: int) -> int:
    """档位索引（0..size-1）按相对位置折算到固定桶数（0..buckets-1）。

    折算取 **floor**（相对位置的下半段归低桶）：5 档→3 桶 = 0/0/1/1/2，与 §19.4 之前的
    固定映射逐档一致；``size<=1`` → 中间桶（单档预设落在中性桶）。
    """
    size = max(1, int(size))
    index = max(0, min(size - 1, int(tier)))
    if size == 1:
        return max(0, buckets // 2)
    ratio = index / (size - 1)
    return max(0, min(buckets - 1, int(ratio * (buckets - 1))))


@dataclass
class RelationProfile:
    """从配置解析出的关系档案（组件/服务层只认这个）。"""

    word: str = "恋人"  # 对方是 bot 的谁（一句话称谓）
    preset_name: str = "恋人"
    tier_count: int = 5  # 生效预设档位数（1~5；ratio 折算基准）
    tier_labels: Dict[int, str] = field(default_factory=lambda: {
        i: label for i, label in enumerate(affection_presets.BUILTIN_TIER_LABELS["恋人"])})
    tier_descriptions: Dict[int, str] = field(default_factory=dict)
    tier_miss_factors: Dict[int, float] = field(default_factory=lambda: dict(_RELATION_TIER_MISS_FACTORS["恋人"]))
    style_word: str = "温柔亲昵的陪伴者"
    self_desc: str = "一个温柔体贴的虚拟陪伴者"
    suffixes: Dict[int, List[str]] = field(default_factory=lambda: {k: list(v) for k, v in RELATION_SUFFIXES["恋人"].items()})
    theaters: List[str] = field(default_factory=lambda: list(RELATION_THEATERS["恋人"]))
    is_custom: bool = False

    def tier_label(self, tier: int) -> str:
        """生效预设口径的档位名（越界按档位数钳制）。"""
        index = max(0, min(self.tier_count - 1, int(tier)))
        return self.tier_labels.get(index, self.tier_labels.get(0, ""))

    def tier_description(self, tier: int) -> str:
        """档位描述（解析链已按生效预设 + 描述表解析完毕）。"""
        return self.tier_descriptions.get(self._clamp(tier), "")

    def miss_factor_for_tier(self, tier: int) -> float:
        """档位 → miss 阈值系数（按 ratio×4 折算内置 5 档表；5 档时逐档一致）。"""
        index = ratio_bucket_index(tier, self.tier_count, 5)
        return float(self.tier_miss_factors.get(index, self.tier_miss_factors.get(2, 1.0)))

    def suffix_for_tier(self, tier: int) -> List[str]:
        """按档位相对位置折算 3 档桶取后缀池（§19.4；5 档时与旧固定映射一致）。"""
        return self.suffix_for(ratio_bucket_index(tier, self.tier_count, 3))

    def tier_uses_theater(self, tier: int) -> bool:
        """该档位是否进入括号小剧场档（折算后为桶 2）。"""
        return ratio_bucket_index(tier, self.tier_count, 3) == 2

    def suffix_for(self, level: int) -> List[str]:
        return self.suffixes.get(int(level), self.suffixes.get(0, ["~"]))

    def _clamp(self, tier: int) -> int:
        return max(0, min(self.tier_count - 1, int(tier)))


def _tier_descriptions(preset: Any, labels: Dict[int, str]) -> Dict[int, str]:
    """按生效预设区间 + 描述表解析每档描述（预设名+标签 → 描述链）。"""
    bounds = preset.bounds()
    out: Dict[int, str] = {}
    for index, label in labels.items():
        lo, hi = bounds[index] if index < len(bounds) else (affection_presets.SCORE_MIN,
                                                            affection_presets.SCORE_MAX)
        out[index] = tier_description_for(preset.name, label, lo, hi)
    return out


#: 「自定义」预设的三桶内联档位描述（占位词 ``{word}`` = relation_custom 实际称谓）。
#: 2026-10-17 §19.4 / 复核 findings#3：恢复 §19.4 前的旧内联文案风格——动态称谓档位不再
#: 落 ``tier_description_for`` 的 generic 固定句式，改按折算三桶取这三句（桶序与
#: ``ratio_bucket_index(i, size, 3)`` 一致：0=熟识、1=亲近、2=称谓）。
_CUSTOM_BUCKET_DESCRIPTIONS: Tuple[str, str, str] = (
    "初步相识，语气友善，正在了解这位{word}",
    "相处融洽，语气自然亲近，把TA当作很重要的{word}",
    "已是彼此最信任的{word}，语气完全放松、真心相待",
)


def _custom_tier_descriptions(word: str, preset: Any, labels: Dict[int, str]) -> Dict[int, str]:
    """「自定义」预设的档位描述：生效描述表（用户按实际称谓补键）→ 三桶内联文案。

    描述链命中（用户 json 里按档位标签写了文案）时照旧生效；未命中
    （``tier_description_for`` 落到 generic 固定句式）时改用
    :data:`_CUSTOM_BUCKET_DESCRIPTIONS` 的前述三句，桶序由 ``ratio_bucket_index`` 折算——
    5 档时与旧固定映射逐档一致（0/1→熟识、2/3→亲近、4→称谓）。
    """
    bounds = preset.bounds()
    resolved = _tier_descriptions(preset, labels)
    out: Dict[int, str] = {}
    for index, label in labels.items():
        lo, hi = bounds[index] if index < len(bounds) else (affection_presets.SCORE_MIN,
                                                            affection_presets.SCORE_MAX)
        text = resolved.get(index, "")
        if not text or text == generic_tier_description(label, lo, hi):
            bucket = ratio_bucket_index(index, preset.size, 3)
            text = _CUSTOM_BUCKET_DESCRIPTIONS[bucket].format(word=word)
        out[index] = text
    return out


def _profile_for_builtin(rel: str, preset: Any) -> RelationProfile:
    """内置四预设（或 json 覆盖同名）的档案构造。"""
    labels = {i: label for i, label in enumerate(preset.labels())}
    miss_source = rel if rel in _RELATION_TIER_MISS_FACTORS else "恋人"
    style = _RELATION_STYLE.get(rel, _RELATION_STYLE["恋人"])
    suffix_source = rel if rel in RELATION_SUFFIXES else "恋人"
    theater_source = rel if rel in RELATION_THEATERS else "恋人"
    return RelationProfile(
        word=rel,
        preset_name=rel,
        tier_count=preset.size,
        tier_labels=labels,
        tier_descriptions=_tier_descriptions(preset, labels),
        tier_miss_factors=dict(_RELATION_TIER_MISS_FACTORS[miss_source]),
        style_word=style["style"],
        self_desc=style["self_desc"],
        suffixes={k: list(v) for k, v in RELATION_SUFFIXES[suffix_source].items()},
        theaters=list(RELATION_THEATERS[theater_source]),
    )


def _profile_for_extra(rel: str, preset: Any) -> RelationProfile:
    """json 新增预设（内置五名之外）的档案构造：标签/区间用预设，口吻/后缀取「挚友」中性基底。"""
    labels = {i: label for i, label in enumerate(preset.labels())}
    return RelationProfile(
        word=rel,
        preset_name=rel,
        tier_count=preset.size,
        tier_labels=labels,
        tier_descriptions=_tier_descriptions(preset, labels),
        tier_miss_factors=dict(_RELATION_TIER_MISS_FACTORS["挚友"]),
        style_word=f"亲近真诚的{rel}",
        self_desc=f"一个把TA当作很重要{rel}的 AI 伙伴",
        suffixes={k: list(v) for k, v in RELATION_SUFFIXES["挚友"].items()},
        theaters=list(RELATION_THEATERS["挚友"]),
    )


def _profile_for_custom(word: str, preset: Any) -> RelationProfile:
    """「自定义」预设：区间用生效预设，档位名按相对位置折算三桶（熟识/亲近/称谓）。

    称谓是动态的（relation_custom），故本预设档位名不进描述表内置默认；档位描述优先取
    描述表（用户可在描述表按实际称谓补档位键），未命中时走
    :func:`_custom_tier_descriptions` 的三桶内联文案，**不再落 generic 固定句式**
    （2026-10-17 §19.4 / 复核 findings#3）。
    """
    buckets = ("熟识", "亲近", word)
    labels = {i: buckets[ratio_bucket_index(i, preset.size, 3)] for i in range(preset.size)}
    return RelationProfile(
        word=word,
        preset_name="自定义",
        tier_count=preset.size,
        tier_labels=labels,
        tier_descriptions=_custom_tier_descriptions(word, preset, labels),
        tier_miss_factors=dict(_RELATION_TIER_MISS_FACTORS["自定义"]),
        style_word=f"亲近真诚的{word}",
        self_desc=f"一个把TA当作最重要{word}的 AI 伙伴",
        suffixes={k: list(v) for k, v in RELATION_SUFFIXES["挚友"].items()},
        theaters=list(RELATION_THEATERS["挚友"]),
        is_custom=True,
    )


def resolve_relation(relation_type: Any = "恋人", relation_custom: Any = "") -> RelationProfile:
    """把配置项解析成 RelationProfile。

    - 预设名解析顺序：生效预设表（内置五名 ∪ json 预设）优先；未知值回退内置「恋人」；
    - 「自定义」→ 称谓用 relation_custom（为空则回退「重要的人」），档位名按三桶折算、
      口吻/后缀取「挚友」为中性基底；
    - json 新增预设 → 标签/区间用预设、口吻/后缀取「挚友」中性基底。
    """
    rel = str(relation_type or "恋人").strip()
    custom_label = str(relation_custom or "").strip()
    presets = affection_presets.active_presets()

    if rel == "自定义":
        word = custom_label or "重要的人"
        preset = presets.get("自定义") or affection_presets.preset_for("自定义")
        return _profile_for_custom(word, preset)

    preset = presets.get(rel)
    if preset is None:
        rel = "恋人"
        preset = presets.get(rel) or affection_presets.preset_for(rel)
    if rel in _RELATION_STYLE:
        return _profile_for_builtin(rel, preset)
    return _profile_for_extra(rel, preset)
