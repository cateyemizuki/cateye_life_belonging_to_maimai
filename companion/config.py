"""companion 子模块配置段（来源：虚拟陪伴插件 mai-love config.py）。

只定义 section 类，不定义聚合模型——聚合由顶层 suite_config.py 的 CateyeSuiteConfig 负责：
- PatrolSectionConfig          挂为顶层 [patrol] 段（**2026-10-10 配置整理轮起 = 「陪伴模块」**
  总段：模块开关 / 目标用户 / 关系 / 巡检 / 想念 / 预算 / 静默 / 屏幕感知，原 [target] 与
  [cateye] 两段并入本段——段名保持不变以保住既有配置值，仅 UI 标题与排序变化）；
- AffectionSectionConfig       挂为顶层 [affection] 段。

配置整理轮（2026-10-10）要点：
- 目标 QQ 从 [target] 段迁入本段（旧段值由 suite_config 的迁移校验器搬过来）；
- 早安/晚安窗口字段（morning_start/end、night_start/end）**删除**——问候时刻一律由
  [schedule] 的 wake/sleep 锚点 + [planning_proactive] 的漂移区间决定；
- 问候豁免最小间隔改为**始终生效**（不再提供配置项）；想念**不豁免**；
- 目标陪伴私聊打扰上限（旧 proactive_target_daily_cap / global_proactive_cap）**删除**：
  每日预算（daily_max_speak）即上限，只统计日程巡检与想念两类触发；
- 想念阈值合并为一个区间字段 miss_silence_hours（``"4-8"`` 或单数字 ``8`` = 0~8）；
- 想念避开未来日程及其窗口**删除**（该功能整体移除）；
- 新增想念开关（miss_enabled，已有）+ 日程巡检开关（patrol_activity_enabled）；
- 屏幕感知（原 [cateye] 段）并入本段，前缀 screen_*；提示词一律移入 [prompts]。

WebUI 元数据约定与顶层 suite_config.py 一致：
- 每个字段带中文短标题 label + 英文 i18n + hint（**hint 必须写"改了会怎样"的短效果描述**）；
- description 写完整中文说明，作为界面未命中翻译时的回退文案。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Literal

import pydantic
from maibot_sdk import Field, PluginConfigBase


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；与顶层 suite_config.py 同一写法）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


def target_qq_of(config: Any) -> str:
    """读陪伴对象 QQ（新位置 ``[patrol].target_qq``；旧 ``[target]`` 段由聚合迁移器搬运）。

    对「plugin 实例 / 配置对象 / 骨架测试桩」三种形态都可空降级：一律返回**字符串**
    （取不到返回空串），调用方自行判定占位值/空值。旧段兜底让尚未迁移的配置对象
    在热重载窗口里也不会丢目标。
    """
    section = getattr(config, "patrol", None)
    value = getattr(section, "target_qq", None)
    if value is None:
        legacy = getattr(config, "target", None)
        value = getattr(legacy, "target_qq", None)
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# [patrol] 陪伴模块（2026-10-10 起：目标用户 + 巡检 + 屏幕感知合并段）
# ---------------------------------------------------------------------------


class PatrolSectionConfig(PluginConfigBase):
    """陪伴模块（原「主动巡检」段：目标用户 / 巡检 / 好感注入 / 屏幕感知）。"""

    __ui_label__ = "陪伴模块"
    __ui_icon__ = "heart"
    __ui_order__ = 1
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Companion",
               "description": "Target user, patrol-based proactive chat (miss / activity / daily), "
                              "budget and screen awareness."}
    }

    # ── 模块独立总开关 ───────────────────────────────────────
    enabled: bool = Field(
        default=True,
        description="陪伴模块总开关：关闭后停止巡检主动聊天与好感度注入（命令与工具返回未启用提示）",
        json_schema_extra={
            "label": "启用陪伴模块",
            "hint": "关 = 不巡检、不注入好感度、不给目标发早晚安；其他模块不受影响",
            **_ui_i18n("Enable companion module", "Off: no patrol, no affection injection, no target greetings."),
        },
    )

    # ── 目标用户（原 [target] 段）────────────────────────────
    target_qq: int = Field(
        default=123456789,
        description="陪伴对象的 QQ 号：主动聊天、想念、好感度、屏幕感知都只对这个私聊会话生效",
        json_schema_extra={
            "label": "目标QQ（陪伴对象）",
            "hint": "保持默认占位值 123456789 时插件不会主动私聊任何人",
            **_ui_i18n("Target QQ", "Proactive chat stays off while the placeholder is kept."),
        },
    )
    relation_type: Literal["恋人", "挚友", "维护者", "家人", "自定义"] = Field(
        default="恋人",
        description="目标用户是 bot 的谁：影响想念提示词、消息口吻、好感度档位名称与消息后缀",
        json_schema_extra={
            "label": "关系类型",
            "hint": "恋人=甜蜜亲昵；挚友=轻松直来直去；维护者=信赖依赖；家人=随性亲近；自定义=用下方称谓",
            **_ui_i18n("Relation type", "Who the target user is to the bot."),
        },
    )
    relation_custom: str = Field(
        default="",
        description="自定义关系的称谓（关系类型选「自定义」时生效，如：姐姐、师兄、搭子）",
        json_schema_extra={
            "label": "自定义称谓",
            "hint": "留空时回退「重要的人」",
            "placeholder": "姐姐",
            **_ui_i18n("Custom relation label", "Used when relation type is 自定义."),
        },
    )

    # ── 巡检循环 ─────────────────────────────────────────────
    proactive_trigger_enabled: bool = Field(
        default=True,
        description="主动巡检总开关：开启后按下方各触发开关与概率判断该不该主动说话",
        json_schema_extra={
            "label": "启用主动巡检",
            "hint": "关 = 陪伴侧完全不主动开口（早/晚安由日程侧驱动，不受本开关影响）",
            **_ui_i18n("Enable proactive patrol", "Off: companion never starts a chat."),
        },
    )
    check_interval_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="巡检循环间隔（分钟）：每隔多久判断一次该不该主动说话",
        json_schema_extra={
            "label": "巡检间隔(分钟)",
            "hint": "越小越灵敏、日志越多；默认 10 分钟一轮",
            **_ui_i18n("Check interval (min)"),
        },
    )
    patrol_activity_enabled: bool = Field(
        default=True,
        description="日程巡检开关：巡检发现 bot 正在某个日程活动时，按下方概率主动分享/搭话",
        json_schema_extra={
            "label": "启用日程巡检",
            "hint": "关 = 不再按日程活动主动开口；想念不受影响",
            **_ui_i18n("Enable activity patrol", "Off: no proactive chat tied to schedule activities."),
        },
    )
    activity_trigger_rate: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="日程巡检概率：巡检命中当前有日程活动时，主动分享一次的概率",
        json_schema_extra={
            "label": "巡检·日程分享概率",
            "step": 0.05,
            "hint": "0 = 完全不分享日程；0.6 ≈ 每 10 轮里有 6 轮会开口",
            **_ui_i18n("Activity share rate"),
        },
    )
    default_speak_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="日程巡检概率：巡检时没有正在进行的日程活动，纯日常搭话一次的概率",
        json_schema_extra={
            "label": "巡检·日常搭话概率",
            "step": 0.05,
            "hint": "0 = 不在没有活动时搭话；与上一项独立",
            **_ui_i18n("Daily chat rate"),
        },
    )
    miss_enabled: bool = Field(
        default=True,
        description="想念开关：目标用户沉默超过阈值后按概率主动说想TA（每天最多一次）",
        json_schema_extra={
            "label": "启用想念",
            "hint": "关 = 永不因沉默主动开口；用户沉默时长、概率与 LLM 把关仍按下方配置",
            **_ui_i18n("Enable miss trigger", "Off: never starts a chat out of missing someone."),
        },
    )
    miss_silence_hours: str = Field(
        default="4-8",
        description="想念阈值（小时）：用户沉默超过该时长才考虑想念。写 \"4-8\" = 区间内每轮随机取阈值"
                    "（沉默越久越容易触发）；只写一个数字如 8 = 0~8 区间的宽松写法",
        json_schema_extra={
            "label": "想念阈值(小时)",
            "placeholder": "4-8",
            "pattern": r"^\d+(\.\d+)?(-\d+(\.\d+)?)?$",
            "hint": '写 "4-8"（推荐）或单个数字 8（= 0~8）；非法值回退 4-8',
            **_ui_i18n("Miss silence threshold (hours)", 'e.g. "4-8", or a single number 8 = 0-8.'),
        },
    )
    miss_speak_rate: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="想念概率：每轮巡检在沉默超过阈值后真的开口说想念的概率",
        json_schema_extra={
            "label": "想念概率",
            "step": 0.05,
            "hint": "0 = 只判断不开口；1 = 一超阈值就开口",
            **_ui_i18n("Miss rate"),
        },
    )
    miss_llm_check_enabled: bool = Field(
        default=True,
        description="想念 LLM 把关：开口前让模型以角色身份判断此刻合不合适（回答 Y 才发），驳回后 30 分钟不再判断",
        json_schema_extra={
            "label": "想念 LLM 把关",
            "hint": "关 = 少一次模型调用、更机械；开 = 深夜/忙碌时更不容易打扰",
            **_ui_i18n("Miss LLM gate"),
        },
    )
    daily_max_speak: int = Field(
        default=5,
        ge=0,
        le=50,
        description="每日预算（次）：陪伴侧主动发言的每日上限，只统计日程巡检与想念；"
                    "早/晚安不占预算（由日程侧驱动）",
        json_schema_extra={
            "label": "每日预算",
            "hint": "0 = 陪伴侧完全静音；只对目标私聊计数（群聊/其他私聊不受限）",
            **_ui_i18n("Daily budget", "Only counts patrol triggers and miss; greetings are exempt."),
        },
    )
    min_trigger_interval_minutes: int = Field(
        default=240,
        ge=0,
        le=1440,
        description="最小间隔（分钟）：任意两次巡检/想念主动发言之间的硬性间隔，0 = 不限制；"
                    "早/晚安始终豁免本限制",
        json_schema_extra={
            "label": "最小间隔(分钟)",
            "hint": "默认 240 分钟（4 小时）；早/晚安不看本项",
            **_ui_i18n("Min interval (min)", "Greetings are always exempt."),
        },
    )
    user_cooldown_minutes: int = Field(
        default=30,
        ge=0,
        le=180,
        description="用户冷却（分钟）：对方最后一次发言后多久内不主动打扰（想念/巡检受控，早/晚安不受控）",
        json_schema_extra={
            "label": "用户冷却(分钟)",
            "hint": "对方刚说完话就别插嘴；默认 30 分钟",
            **_ui_i18n("User cooldown (min)"),
        },
    )
    silence_start: str = Field(
        default="00:00",
        description="静默时段开始（HH:MM）：静默期内巡检不触发、问候不发（支持跨午夜；配置非法按静默处理）",
        json_schema_extra={
            "label": "静默开始",
            "hint": "HH:MM；默认 00:00-07:30，整夜不打扰",
            **_ui_i18n("Silence start"),
        },
    )
    silence_end: str = Field(
        default="07:30",
        description="静默时段结束（HH:MM）",
        json_schema_extra={
            "label": "静默结束",
            "hint": "HH:MM；静默期结束后才恢复巡检与问候",
            **_ui_i18n("Silence end"),
        },
    )

    # ── 好感度注入 ───────────────────────────────────────────
    inject_affection_in_group: bool = Field(
        default=True,
        description="群聊好感度注入：开启后，群里最近有人提到/包含陪伴对象时，给 bot 注入一点好感度上下文；"
                    "目标私聊始终注入好感度，其他私聊始终不注入",
        json_schema_extra={
            "label": "群聊注入好感度",
            "hint": "开 = 群聊里出现陪伴对象才注入（不影响其他会话）；关 = 群里永不注入好感度",
            **_ui_i18n("Inject affection in group chats",
                       "Only when the companion user appears in the group context."),
        },
    )

    # ── 屏幕感知（原 [cateye] 段）────────────────────────────
    screen_enabled: bool = Field(
        default=False,
        description="屏幕感知：每次主动触发前经 cateye.connect-hub 截一张陪伴对象电脑的屏幕并转述成一句旁白",
        json_schema_extra={
            "label": "启用屏幕感知",
            "hint": "需要「cateye 统一连接插件」且对方电脑开着客户端；默认关",
            **_ui_i18n("Enable screen awareness",
                       "Requires the cateye connect-hub plugin and an online client on the lover's PC."),
        },
    )
    screen_blur: int = Field(
        default=0,
        ge=0,
        le=64,
        description="截图模糊半径（0-64）：0 = 清晰；介意隐私可设 30 左右（视觉模型仍能看出大概在干什么）",
        json_schema_extra={
            "label": "截图模糊半径",
            "hint": "越大越看不清细节；0 = 不模糊",
            **_ui_i18n("Screenshot blur"),
        },
    )
    screen_timeout_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=120.0,
        description="查看电脑（截图 + 视觉理解）整体超时（秒）；超时不阻塞巡检，本轮按不可用处理",
        json_schema_extra={
            "label": "查看超时(秒)",
            **_ui_i18n("Peek timeout (s)"),
        },
    )
    screen_context_ttl_minutes: int = Field(
        default=90,
        ge=0,
        le=1440,
        description="屏幕旁白存活上限（分钟）：超过即丢弃不再注入；0 = 不限时",
        json_schema_extra={
            "label": "旁白存活上限(分钟)",
            "hint": "防止几小时前的屏幕画面一直被当成「现在」",
            **_ui_i18n("Narration TTL (min)"),
        },
    )


# ---------------------------------------------------------------------------
# [affection] 好感度
# ---------------------------------------------------------------------------


class AffectionSectionConfig(PluginConfigBase):
    """好感度系统（来源：虚拟陪伴插件；M1 扩展为 v2 状态层配置，全部带默认值可关闭）。

    `enabled=false` 时整体回退 v1 三档行为（§八.2：新增配置关闭时回退旧行为）；
    `review_*` 三项为 M2 好感 LLM 裁判（会话采样 + 关系日结）的开关与频控。
    裁判任务名统一移到 [llm] 段的 review_task_name。
    """

    __ui_label__ = "好感度"
    __ui_icon__ = "favorite"
    __ui_order__ = 2
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Affection", "description": "Companion affection tier system (v2 score/baseline)."}
    }

    current_level: Literal[0, 1, 2] = Field(
        default=0,
        description="好感度档位：0=熟悉（温柔拘谨），1=亲密（活泼热情），2=热恋（撒娇抱抱）",
        json_schema_extra={
            "label": "好感档位",
            "hint": "只作首次建立档案时的初始档位；已有档案时以累积分数为准",
            **_ui_i18n("Affection level"),
        },
    )

    @pydantic.field_validator("current_level", mode="before")
    @classmethod
    def _coerce_current_level(cls, value: Any) -> Any:
        """P7 修复（2026-10-10，退休报告 §7.2 推荐方案 A）：WebUI/TOML 传来的数字字符串归一为 int。

        宿主传输形态是字符串（WebUI 表单控件提交 ``"1"``，或 config.toml 手写
        ``current_level = "1"``），而 ``Literal[0, 1, 2]`` 按「值 + 类型」精确匹配 →
        ``"1" ≠ 1`` → literal_error → 整个 CateyeSuiteConfig 校验失败（宿主回 E_BAD_PAYLOAD）。
        Pydantic 的「数字字符串宽松转 int」只对 int 字段生效、对 Literal 成员不生效，故在此前置归一。

        只放行 strip 后恰为 ``"0"``/``"1"``/``"2"`` 的字符串；``"3"``/``"x"``/浮点等
        一律原样交给 Literal 拒绝（保持原有报错语义）。
        """
        if isinstance(value, str):
            text = value.strip()
            if text in ("0", "1", "2"):
                return int(text)
        return value

    enabled: bool = Field(
        default=True,
        description="启用好感度 v2：开启后按 score/baseline 连续分运行（规则事件记账 + 日衰减 + 档位迟滞）；"
                    "关闭则回退 v1 三档行为（档位仅由命令/工具设置，不自动变化）",
        json_schema_extra={
            "label": "启用好感度 v2",
            "hint": "关 = 保留旧三档手感（无日衰减、无事件记账）；文件仍以 v2 格式保存，可随时再开",
            **_ui_i18n("Enable affection v2",
                       "Off = legacy 3-tier behaviour: tier only changes via commands, no decay or rule events."),
        },
    )
    decay_pull: float = Field(
        default=0.06,
        ge=0.0,
        le=1.0,
        description="日衰减拉回基线的比例：每天日切执行 score += (baseline − score) × 该值（0=不衰减）",
        json_schema_extra={
            "label": "日衰减拉回",
            "step": 0.01,
            "hint": "越大 = 一次甜蜜日忘得越快；0.06 约 5~6 天回落大半",
            **_ui_i18n("Daily decay pull", "0 = no decay."),
        },
    )
    baseline_ema: float = Field(
        default=0.03,
        ge=0.0,
        le=1.0,
        description="关系基线漂移速度：日切执行 baseline += (score − baseline) × 该值（0=基线永不移动）",
        json_schema_extra={
            "label": "基线漂移速度",
            "step": 0.01,
            "hint": "基线是「感情底色」：涨得慢、忘得慢；0.03 表示长期相处才会抬升",
            **_ui_i18n("Baseline EMA", "How fast the long-term baseline follows the recent score. 0 = frozen."),
        },
    )
    daily_positive_cap: float = Field(
        default=4.0,
        ge=0.0,
        le=100.0,
        description="单日正向好感增量上限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日正向上限",
            "step": 0.5,
            "hint": "防止一天之内好感暴涨；0 = 当日不再增加好感",
            **_ui_i18n("Daily positive cap"),
        },
    )
    daily_negative_cap: float = Field(
        default=3.0,
        ge=0.0,
        le=100.0,
        description="单日负向好感扣减下限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日负向下限",
            "step": 0.5,
            "hint": "防止一次矛盾把好感打崩；0 = 当日不再扣好感",
            **_ui_i18n("Daily negative cap"),
        },
    )
    tier_hysteresis: float = Field(
        default=3.0,
        ge=0.0,
        le=30.0,
        description="档位迟滞带（分）：升档需 score ≥ 当前档上界 + 该值，降档需 ≤ 下界 − 该值（0=无迟滞）",
        json_schema_extra={
            "label": "档位迟滞带",
            "step": 0.5,
            "hint": "防止在档位边界反复横跳",
            **_ui_i18n("Tier hysteresis"),
        },
    )
    tier_min_dwell_hours: float = Field(
        default=24.0,
        ge=0.0,
        le=720.0,
        description="档位最短停留时长（小时）：距进入当前档位不足该时长则不升/降档（0=不限）",
        json_schema_extra={
            "label": "档位最短停留(小时)",
            "step": 1.0,
            "hint": "档位变化最多一天一次，避免情绪化跳档",
            **_ui_i18n("Min tier dwell (h)"),
        },
    )
    review_enabled: bool = Field(
        default=True,
        description="好感度 LLM 裁判总开关：会话采样评估 + 睡眠时段关系日结；关闭则完全不做 LLM 评估",
        json_schema_extra={
            "label": "好感裁判开关",
            "hint": "关 = 零模型成本：不做会话评估与关系日结；规则事件与日衰减照常",
            **_ui_i18n("Affection review"),
        },
    )
    review_sample_every_rounds: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="会话裁判采样频率（轮）：每多少轮对方私聊消息抽样一次（与每日上限取先到者）",
        json_schema_extra={
            "label": "会话采样轮数",
            "hint": "越小抽样越勤、模型成本越高；默认 20 轮一次",
            **_ui_i18n("Review sample rounds"),
        },
    )
    review_max_per_day: int = Field(
        default=2,
        ge=0,
        le=50,
        description="会话裁判每日上限（次）：0 = 当天不做采样评估（不影响睡眠时段的关系日结）",
        json_schema_extra={
            "label": "会话裁判每日上限",
            "hint": "控制模型成本；日结每天最多 1 次，与采样评估分开计",
            **_ui_i18n("Review daily cap"),
        },
    )
    emotion_modulation: bool = Field(
        default=True,
        description="情绪调制好感增量：开心时好事加分更多、低落时坏事扣分更狠（乘数 0.6~1.4）",
        json_schema_extra={
            "label": "情绪调制好感",
            "hint": "关 = 所有增量按原值入账；情绪模块停用时本就恒为 1",
            **_ui_i18n("Emotion modulation"),
        },
    )
    weekly_milestones_enabled: bool = Field(
        default=False,
        description="周结大事记：周日的日结后按最近 7 天关系温度与净变化汇总出 1~2 条里程碑，随好感注入展示",
        json_schema_extra={
            "label": "周结大事记",
            "hint": "默认关；开启后每周日最多生成 1 次（纯规则，无模型成本）",
            **_ui_i18n("Weekly milestones"),
        },
    )


# ---------------------------------------------------------------------------
# [patrol] 旧键迁移辅助（供 suite_config 的聚合迁移校验器调用）
# ---------------------------------------------------------------------------


#: 旧 [patrol] 段中已移除的键（迁移器据此判断"用户是否显式配置过"）
LEGACY_PATROL_KEYS = (
    "morning_start",
    "morning_end",
    "night_start",
    "night_end",
    "min_interval_exempt_greetings",
    "min_interval_exempt_miss",
    "proactive_target_daily_cap",
    "global_proactive_cap",
    "miss_hours_min",
    "miss_hours_max",
    "miss_avoid_future_schedule",
    "miss_future_schedule_hours",
)


def migrate_legacy_patrol(data: dict, legacy_target: object = None) -> dict:
    """把旧 [patrol] 段里的已删键搬到新结构（在聚合校验器里按序调用）。

    - ``legacy_target``（旧 [target] 段的字典）里的 ``target_qq`` → 本段的 ``target_qq``
      （段合并的一部分；新段已有显式值时不动）；
    - ``miss_hours_min`` / ``miss_hours_max`` → ``miss_silence_hours``（``"min-max"``；
      只有一个值时按 "0-N" 处理，与新区间语义一致）；
    - ``proactive_target_daily_cap`` / ``global_proactive_cap`` → 丢弃（每日预算即上限）；
    - 其余已删键 → 丢弃（早/晚安窗口、两处豁免开关、想念避让窗口全部不再需要）。

    返回新字典（不改原对象）；非 dict 输入原样返回。
    """
    if not isinstance(data, dict):
        data = {}
    migrated = dict(data)
    if isinstance(legacy_target, dict) and "target_qq" in legacy_target:
        migrated.setdefault("target_qq", legacy_target.get("target_qq"))

    low = migrated.pop("miss_hours_min", None)
    high = migrated.pop("miss_hours_max", None)
    if low is not None or high is not None:
        try:
            low_v = float(low) if low is not None else 0.0
            high_v = float(high) if high is not None else 8.0
        except (TypeError, ValueError):
            low_v, high_v = 4.0, 8.0
        if low_v > high_v:
            low_v, high_v = high_v, low_v
        # 只有一个值时按 0~N 处理（与单数字写法的解析口径一致）
        if low is None:
            low_v = 0.0
        if high is None:
            high_v = low_v
        migrated.setdefault("miss_silence_hours", f"{low_v:g}-{high_v:g}")

    for key in LEGACY_PATROL_KEYS:
        migrated.pop(key, None)
    return migrated


def migrate_legacy_cateye(section_data: object) -> dict:
    """旧 [cateye] 段 → 新 [patrol] 段的 screen_* 字段 / [llm] 的 vlm_task / [prompts] 的提示词。

    返回三键字典 ``{"patrol": {...}, "llm": {...}, "prompts": {...}}``（无内容时为空 dict）；
    文本类提示词只在用户显式改过（与出厂默认不同）时才搬运，避免把默认文案"钉"进用户配置。
    """
    from suite_prompts import (
        SCREEN_DESCRIBE_DEFAULT,
        SCREEN_NARRATION_DEFAULT,
        SCREEN_NARRATION_FAILED_DEFAULT,
        SCREEN_NARRATION_OFFLINE_DEFAULT,
    )

    out: Dict[str, dict] = {"patrol": {}, "llm": {}, "prompts": {}}
    if not isinstance(section_data, dict):
        return out
    mapping = {
        "enabled": "screen_enabled",
        "screenshot_blur": "screen_blur",
        "timeout_seconds": "screen_timeout_seconds",
        "context_ttl_minutes": "screen_context_ttl_minutes",
    }
    for old, new in mapping.items():
        if old in section_data:
            out["patrol"][new] = section_data.get(old)
    if section_data.get("vlm_task"):
        out["llm"]["vlm_task"] = section_data.get("vlm_task")
    prompt_map = {
        "describe_prompt": ("screen_describe", SCREEN_DESCRIBE_DEFAULT),
        "narration_template": ("screen_narration", SCREEN_NARRATION_DEFAULT),
        "offline_narration_template": ("screen_narration_offline", SCREEN_NARRATION_OFFLINE_DEFAULT),
        "failed_narration_template": ("screen_narration_failed", SCREEN_NARRATION_FAILED_DEFAULT),
    }
    for old, (new, default) in prompt_map.items():
        value = section_data.get(old)
        if value is not None and str(value) != str(default):
            out["prompts"][new] = value
    return out
