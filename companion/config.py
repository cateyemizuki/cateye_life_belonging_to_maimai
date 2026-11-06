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
  每日预算（daily_max_speak）即上限（2026-11-04 重写后巡检侧仅想念一类触发）；
- 想念阈值合并为一个区间字段 miss_silence_hours（``"4-8"`` 或单数字 ``8`` = 0~8；
  2026-11-04 重写后仅服务沉默事件，不再参与想念判定）；
- 想念避开未来日程及其窗口**删除**（该功能整体移除）；
- 2026-11-04 主动行为重写：日程巡检三键（patrol_activity_enabled / activity_trigger_rate /
  default_speak_rate）与 miss_speak_rate 退役（纯 pop 迁移）；想念改状态驱动概率公式
  （companion.miss_trigger 内部常量）；
- 屏幕感知（原 [cateye] 段）并入本段，前缀 screen_*；提示词一律移入 [prompts]；
- 静默窗（silence_start/silence_end）**留空 = 跟随 [schedule] 的入睡/起床锚点**（N5，第三轮）：
  非空 = 显式覆盖；巡检静默闸与 planning 晚安期限共用同一生效值解析
  （companion.patrol.resolve_silence_window），配置非法仍按静默 fail-safe 处理。
  **（2026-10-20 用户指示：两键已移除——静默窗恒复用日程作息，入睡~起床整夜即静默。）**

WebUI 元数据约定与顶层 suite_config.py 一致：
- 每个字段带中文短标题 label + 英文 i18n + hint（**hint 必须写"改了会怎样"的短效果描述**）；
- description 写完整中文说明，作为界面未命中翻译时的回退文案。
"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, Literal, Tuple

from pydantic import field_validator

from maibot_sdk import Field, PluginConfigBase

from core.i18n import ui_i18n as _ui_i18n
from core.internal_tuning import tuning_value as _tuning_value


def _relation_type_choices() -> Tuple[str, ...]:
    """[patrol].relation_type 的下拉名单：内置五预设 ∪ ``mappings/affection_presets.json``
    里的合法预设名（2026-10-17 用户指示：下拉选项可随每次启动动态注册）。

    宿主每次 schema 请求现场重算、热重载清 ``sys.modules`` 重导入插件模块，而本函数在
    **配置模型类体构造时**（= 模块导入期）求值一次 → json 变更后每次启动（含热重载）
    下拉选项即变更（宿主侦察结论 dwfrun-b11fe5a6：唯一机制 = pydantic Literal，无缓存层）。
    导入期只读不写（不生成默认文件、不落账）；读取失败/非法 → 静默回退内置名单。
    内置名恒在名单内 → 出厂默认值「恋人」永不因 json 内容而非法。
    """
    try:
        from .affection_presets import available_preset_names

        return available_preset_names()
    except Exception:  # 导入期绝不抛（配置模型构造失败会拖挂整个插件装载）
        return ("恋人", "挚友", "维护者", "家人", "自定义")


#: 动态下拉类型（导入期冻结一次；``Literal[tuple]`` 与多参数形式等价）
RelationType = Literal[_relation_type_choices()]  # type: ignore[valid-type]


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
    """设置陪伴对象、日常主动聊天、想念和打扰限制。"""

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
    relation_type: RelationType = Field(
        default="恋人",
        description="目标用户是 bot 的谁：影响想念提示词、消息口吻、好感度档位名称与消息后缀；"
                    "选项含 mappings/affection_presets.json 中的自定义预设（改 json 后重启/热重载生效）",
        json_schema_extra={
            "label": "关系类型",
            "hint": "恋人=甜蜜亲昵；挚友=轻松直来直去；维护者=信赖依赖；家人=随性亲近；自定义=用下方称谓；"
                    "其余选项来自好感度预设表（mappings/affection_presets.json）",
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
    proactive_reply_target_mode: Literal["浅处理", "深度处理"] = Field(
        default="浅处理",
        description="陪伴私聊的插件主动任务如何选择回复目标：浅处理提示planner使用用户最后一条来信；深度处理还会在执行reply前改写目标消息ID。",
        json_schema_extra={
            "label": "主动回复目标处理",
            "hint": "默认只加提示；深度处理仅改本轮reply的目标ID，保留不回复的选择，不影响普通来信回合。",
            **_ui_i18n("Proactive reply target", "Prompt only by default; deep mode also corrects the reply target before execution."),
        },
    )

    @field_validator("proactive_reply_target_mode", mode="before")
    @classmethod
    def _normalize_reply_target_mode(cls, value: Any) -> str:
        return "深度处理" if value in ("deep", "深度处理") else "浅处理"

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
    # 注（2026-11-04 主动行为重写，用户直派规格）：日程巡检整条删除——
    # patrol_activity_enabled / activity_trigger_rate / default_speak_rate 三键退役
    # （旧值经 suite_config.LEGACY_PATROL_RETIRED_KEYS 入口纯 pop，无新语义可搬运）；
    # miss_speak_rate 一并退役——想念概率改由状态驱动公式内部常量决定
    # （companion.miss_trigger：base 0.65 × 生活状态 × 情绪 × 好感，各因子有界）。
    miss_enabled: bool = Field(
        default=True,
        description="结合人设联系倾向、沉默时间和当前状态产生想念动机，由planner自主决定是否交流。",
        json_schema_extra={
            "label": "启用想念",
            "hint": "同一来信沉默周期最多受理一次；精力不足、正在聊天或不适合打扰时跳过。",
            **_ui_i18n("Enable miss trigger", "Off: never starts a chat out of missing someone."),
        },
    )
    daily_max_speak: int = Field(
        default=5,
        ge=0,
        le=50,
        description="目标私聊的每日可选主动回合上限，包含想念、刷QQ和视频分享动机；早午晚安免预算。",
        json_schema_extra={
            "label": "每日预算",
            "hint": "0 = 停止可选主动回合；仅目标私聊计数，受理回合不等于实际发出消息。",
            **_ui_i18n("Daily budget", "Counts optional proactive turns in the target private chat; greetings are exempt."),
        },
    )
    # 注（2026-10-20 用户指示）：原 silence_start / silence_end 两键已移除——静默窗恒复用
    # 日程作息（[schedule] sleep_time~wake_time，入睡~起床整夜即静默），解析唯一真源 =
    # companion.patrol.resolve_silence_window；旧配置里的显式值由迁移器丢弃并登记。


    # ── 好感度注入 ───────────────────────────────────────────
    inject_affection_in_group: bool = Field(
        default=True,
        description="群聊好感度注入：开启后，群里最近的消息中有陪伴用户本人发言时（按发送者判定，"
                    "不识别昵称/代称提及），给 bot 注入一点好感度上下文；"
                    "目标私聊始终注入好感度，其他私聊始终不注入",
        json_schema_extra={
            "label": "群聊注入好感度",
            "hint": "开 = 群里最近的消息中有陪伴用户本人发言时才注入（按发送者判定，"
                    "不识别昵称/代称提及；不影响其他会话）；关 = 群里永不注入好感度",
            **_ui_i18n("Inject affection in group chats",
                       "Only when the companion user spoke in the group recently "
                       "(sender-based; no nickname/mention matching)."),
        },
    )

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[patrol]）──────
    @property
    def screen_timeout_seconds(self) -> float:
        """查看电脑整体超时（秒）；现场微调改 mappings/internal_tuning.json。"""
        return float(_tuning_value("patrol", "screen_timeout_seconds"))

    @property
    def screen_context_ttl_minutes(self) -> int:
        """屏幕旁白存活上限（分钟）；0 = 不限时。"""
        return int(_tuning_value("patrol", "screen_context_ttl_minutes"))


# ---------------------------------------------------------------------------
# [affection] 好感度
# ---------------------------------------------------------------------------


class AffectionSectionConfig(PluginConfigBase):
    """设置好感度的记录、变化和关系回顾。"""

    __ui_label__ = "好感度"
    __ui_icon__ = "favorite"
    __ui_order__ = 3
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Affection", "description": "Companion affection system (score 0-200, JSON presets)."}
    }

    enabled: bool = Field(
        default=True,
        description="启用好感度系统：开启后按 score/baseline 连续分（0~200）运行"
                    "（规则事件记账 + 日衰减 + 档位迟滞）；关闭则好感度完全停用（不记账、不变化）",
        json_schema_extra={
            "label": "启用好感度",
            "hint": "关 = 好感度停用（分数/档位不再变化）；文件保留，可随时再开",
            **_ui_i18n("Enable affection",
                       "Off = affection frozen: no decay, no rule events, no tier changes."),
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
        default=8.0,
        ge=0.0,
        le=200.0,
        description="单日正向好感增量上限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日正向上限",
            "step": 0.5,
            "hint": "防止一天之内好感暴涨；0 = 当日不再增加好感",
            **_ui_i18n("Daily positive cap"),
        },
    )
    daily_negative_cap: float = Field(
        default=6.0,
        ge=0.0,
        le=200.0,
        description="单日负向好感扣减下限（含规则事件与后续裁判）：超出部分记日志丢弃",
        json_schema_extra={
            "label": "单日负向下限",
            "step": 0.5,
            "hint": "防止一次矛盾把好感打崩；0 = 当日不再扣好感",
            **_ui_i18n("Daily negative cap"),
        },
    )
    # 2026-11-04 配置精简轮（J 类）：tier_hysteresis 移入
    # mappings/internal_tuning.json[affection]（纯内部阈值；本段生产现值未证实，
    # 走值搬运而非硬编码）。旧 TOML 键经 migrate_internal_tuning 消费，不丢值。
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

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[affection]）────
    @property
    def tier_hysteresis(self) -> float:
        """档位迟滞带（分）：防边界横跳；现场微调改 internal_tuning.json。"""
        return float(_tuning_value("affection", "tier_hysteresis"))


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
      单值口径：min-only → ``"N-N"``、max-only → ``"0-N"``，不复现旧有效区间）；
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
        # 单值口径：min-only → "N-N"、max-only → "0-N"（不复现旧有效区间；
        # 注意与单数字写法 "N" = 0~N 的解析口径不同，见 patrol.parse_hours_range）
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
