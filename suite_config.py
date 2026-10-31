"""属于麦麦的生活（lbm）插件 — 顶层配置聚合。

- 文件名刻意不叫 ``config.py``：宿主 Runner 加载插件时 sys.path 含宿主 ``src``，
  且同一进程可能加载多个插件，顶层 ``config`` 会被宿主 ``src/config`` 或
  兄弟插件抢占（L1 实证），故改名并由 plugin.py 顶部引导头接管自身模块解析。
- 各业务子模块各自拥有自己的配置 section 类（模块化维护）：
  - planning/config.py  日程系统：schedule / schedule_advanced / planning_proactive
  - companion/config.py 陪伴模块：patrol（含目标用户与屏幕感知）/ affection
  - suite_prompts.py    提示词集中模块：prompts
  - qzone/config.py     QQ 空间：qzone
  - emotion/config.py   情绪系统：emotion
- 顶层只保留跨模块共享的基础段（plugin / admin）与新增的 LLM 调用段（llm），
  并把各子模块 section 扁平挂载为聚合模型的顶层字段（嵌套段 WebUI 不渲染）。

## 2026-10-10 配置整理轮（本次改动的总说明）

**段布局变化**：原 [target] 并入 [patrol]（陪伴模块）、原 [cateye] 并入 [patrol]、
原 [judge] 并入新增 [llm]、原 [inject] 与 [planning_cleanup] 并入新增 [schedule_advanced]、
提示词集中进新增 [prompts]。段名保持不变的段（plugin/patrol/affection/schedule/
planning_proactive/admin/qzone/emotion）里的同名键，值会照旧保留。

**config_version 与迁移保障（2026-10-10 第二版，已订正；3.0.0 起版本随 PR8 递增）**：
开发规范（红线 R6）要求 `config_version` **与 `_manifest.json` 的插件版本同步**，故本值 = `3.0.0`（2026-10-27 PR8 清理版；2.2.0 为 2026-10-27 二期 PR7 开发版、2.1.0 于 2026-10-21 归档发布）；
迁移保障改由 **plugin.py 的「磁盘旧文件抢救」**（`CateyeSuitePlugin.normalize_plugin_config` 先读尚未被
宿主覆写的磁盘 `config.toml`，把用户值并回输入）＋ 下面的 `mode="before"` 一次性迁移器共同承担。
**旧口径（已作废）如下，仅留作教训**：宿主 Runner 在「配置版本号变化」时会
**先**用最新默认结构重建设置文件（只保留新旧同名的键），之后才把结果交给插件模型校验——
一旦递增，跨段搬迁（如 target_qq → patrol.target_qq）与所有改名键的用户值都会在插件
看到它们之前被丢弃。因此本轮的旧键迁移放在 :class:`CateyeSuiteConfig` 的
``mode="before"`` 校验器里（版本号不变 → 宿主把原始配置交给插件 → 插件搬迁 → 落盘时
旧键自然消失），:data:`SUPPORTED_CONFIG_VERSION` 保持不变是有意为之。

**迁移哨兵的历史缺陷（2026-10-10 第二轮修复，别再犯）**：``[plugin] layout_version`` 曾被
无条件写进 ``mode="before"`` 校验器——而 Pydantic 在**无参构造 / 空输入**时也会跑该校验器，
于是「出厂默认配置」本身就带上了布局标记。宿主与 SDK 的归一化都会**先 merge 出厂默认**，
``already_migrated`` 因此恒为 True、**迁移器在真实宿主路径下从不执行**（跨段旧键全丢；
离线用例直接 ``model_validate(旧字典)``、不经 merge，所以当时没抓到）。
现在只在**输入非空**（真拿到了配置数据）时才写标记；抢救逻辑见 plugin.py。

**3.0.0 迁移记录（2026-10-27 PR8，用户授权直接清理；R7 依据 = 逐符号审定清单 WP2）**：
退役键清退为**纯 pop、无值搬运**（:data:`LEGACY_ROUTE_KEYS` + :data:`LEGACY_PROACTIVE_KEYS`
追加两 nap 键，入口显式 pop、两个分支都经过）：``[schedule].spine_mode``、
``[schedule].planning_daily_token_budget``、``[life_state].enabled``、
``[life_state].sleep_engine_enabled``、``[life_state].profile_path``、
``[planning_proactive].nap_reminder_enabled``、``[planning_proactive].nap_reminder_max_per_day``。
**全程不写 ``[schedule].enabled``**（裁定 2）：显式 ``false`` 必须保持 ``false``（不得被旧
开关 ``true`` OR 成 ``true``）；缺省场景由 ``[schedule].enabled`` 出厂默认 ``True`` 承担
（UQ-2：旧用户升级后 spine 生成/生活状态/睡眠引擎随默认 True 全开）。``CURRENT_LAYOUT_VERSION``
**不换新标记**（C-12）：3.0.0 迁移是纯 pop 且已放两分支共经入口，换标记反而会让存量已迁移
配置重跑 2026-10-10 值搬运体（非幂等缺陷史，见下）。UQ-1：显式 ``schedule.enabled=false``
且任一旧开关为 true 时写一条一次性停摆告知日志（:func:`_warn_retired_route_switches`）。
plugin.py ``normalize_plugin_config`` 对原始输入与磁盘抢救结果各做一次同一组 raw pop
（在 SDK merge 出厂默认之前生效），聚合校验器入口 pop 对离线 ``model_validate`` 路径兜底。

**迁移映射**（旧 → 新，全部在 :meth:`CateyeSuiteConfig._migrate_legacy_sections`）：- ``[target].target_qq`` → ``[patrol].target_qq``；
- ``[cateye].*`` → ``[patrol].screen_*`` / ``[llm].vlm_task`` / ``[prompts].screen_*``；
- ``[judge].*`` → ``[llm].judge_*``；
- ``[schedule].llm_task_name`` → ``[llm].task_name``；
- ``[affection].review_task_name`` → ``[llm].review_task_name``；
- ``[emotion].evaluator_task_name / referee_task_name`` → ``[llm].*``；
- ``[qzone].llm_task_name`` → ``[llm].qzone_task_name``；
- ``[inject].*`` / ``[planning_cleanup].*`` → ``[schedule_advanced].*``；
- ``[schedule]`` 的 23 个深水字段（timezone / generation_timeout / use_multi_round /
  max_rounds / quality_threshold / recent_schedule_days / history_message_limit / knowledge_search_limit /
  cross_day_activity / min_description_length / max_description_length / emotion_influence / lie_in_max_until /
  list_draw_image / image_timeout_seconds / role_judge_enabled / role_judge_temperature / inject_cache_minutes /
  cache_ttl / cache_max_size / max_future_activities / auto_generate_time）→ ``[schedule_advanced]``（键名不变）。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List

import pydantic
from maibot_sdk import Field, PluginConfigBase

from companion.config import (
    AffectionSectionConfig,
    PatrolSectionConfig,
    migrate_legacy_cateye,
    migrate_legacy_patrol,
)
from emotion.config import EmotionSectionConfig
from planning.config import (
    LifeStateSectionConfig,
    PlanningProactiveSectionConfig,
    ScheduleAdvancedSectionConfig,
    ScheduleSectionConfig,
)
from planning.event_catalog_config import (
    EventCatalogSectionConfig,
    migrate_legacy_event_catalog,
)
from qzone.config import QzoneSectionConfig
from video_watch.config import VideoWatchSectionConfig
from suite_prompts import (
    COMPANION_MISS_CONFIRM_DEFAULT,
    COMPANION_MISS_REASON_DEFAULT,
    PromptsSectionConfig,
)

# 配置版本（**必须与插件版本 `_manifest.json` 的 version 同步**，红线 R6）：
# 插件版本表达"功能发布代次"；配置版本表达"配置文件结构代次"，宿主在它变化时会按新骨架
# 重建设置文件（丢弃跨段旧键）。因此递增本值必须同时具备"磁盘旧文件抢救"（plugin.py 的
# normalize_plugin_config：把尚未被覆写的磁盘 config.toml 里的用户值并回输入），
# 否则跨段/改名键的用户值会在插件看到它们之前被丢弃 —— 见模块 docstring 的教训段。
SUPPORTED_CONFIG_VERSION = "3.0.0"

#: 本轮（2026-10-10 配置整理轮）的段布局标记；写入 [plugin] layout_version 用于迁移幂等
CURRENT_LAYOUT_VERSION = "2026-10-10"

#: 2026-10-10 配置整理轮删除的 5 个旧段（迁移器整段搬运；plugin.py 的磁盘抢救也整段带回）。
#: 保持单一真源：迁移器与抢救逻辑都读这里，别再各写一份字面量元组。
LEGACY_SECTIONS = ("target", "cateye", "judge", "inject", "planning_cleanup")

#: [affection] 段内已删键（2026-10-17 §19.4 好感度 0~200 重构）：v1 三档口径的**初始档位键**
#: ``current_level``。登记于此供迁移器入口显式 pop（见 :meth:`CateyeSuiteConfig._migrate_legacy_sections`）：
#: v1 的 0/1/2 静态档位无法映射到 0~200 连续标度、也无可回填的等价键（§9.7 教训：不猜、
#: 不静默换标度），故只清旧键、**不做任何值搬运**（无 setdefault、无键缺失判断）。
LEGACY_AFFECTION_KEYS = ("current_level",)

#: 静默窗复用作息（2026-10-20 用户指示）后已删除的键：``[patrol]`` / ``[qzone]`` 各一对
#: ``silence_start`` / ``silence_end``。旧配置里的显式值无新语义（静默窗恒取
#: ``[schedule]`` sleep~wake，睡觉时间即静默时间），登记于此供迁移器入口显式 pop
#: （§9.7 教训口径：显式 pop 丢弃并登记，不做任何值搬运）。
LEGACY_SILENCE_KEYS = {
    "patrol": ("silence_start", "silence_end"),
    # post_blocked_activities 随「发空间不再按活动判定」移除（2026-10-20 用户指示）
    "qzone": ("silence_start", "silence_end", "post_blocked_activities"),
}

#: 日程主动行为整条移除（2026-10-20 用户指示）后已删除的 ``[planning_proactive]`` 键：
#: ``enable_proactive_trigger``（活动切换主动开口）、``proactive_fresh_window_minutes``
#: （触发窗口）。
#: 2026-10-22 用户裁定回归（R7 依据 = 用户 2026-10-22 第二批四直派，计划书 §二十七）：
#: ``greeting_require_activation`` **已恢复为真配置键**（群聊问候必要性检查，默认 True），
#: 因此从本元组移除——旧用户配置中的显式值自本版起保留生效，不再被迁移器 pop。
#: 其余旧值无新语义可搬运（主动入口只剩早/晚安 + 陪伴 patrol），登记于此供迁移器入口
#: 显式 pop（§9.7 口径：显式丢弃并登记，不做值搬运）。
LEGACY_PROACTIVE_KEYS = {
    "planning_proactive": (
        "enable_proactive_trigger",
        "proactive_fresh_window_minutes",
        # PR8 3.0.0（R7 依据 = 用户 2026-10-27 授权直接清理 + 逐符号审定清单 WP2）：
        # 赖床提醒链退役（UQ-3/裁定 3 口径），两键随链清退——只 pop、不做值搬运。
        "nap_reminder_enabled",
        "nap_reminder_max_per_day",
    ),
}

#: PR8 3.0.0（2026-10-27 用户授权直接清理）：旧生成路线/运行期开关退役键。
#: ``[schedule].enabled`` 是唯一日程总开关——spine 为唯一生成路线（``spine_mode`` 退役）、
#: 生活状态与睡眠引擎随日程开关生效或停摆（``[life_state].enabled`` /
#: ``sleep_engine_enabled`` / 死键 ``profile_path`` 退役）。登记于此供迁移器入口显式 pop
#: （两分支都经过；§9.7 口径：只 pop 丢弃、不 setdefault、不做键缺失判断）。
#: **绝不写 ``[schedule].enabled``**（裁定 2）：显式 ``false`` 不得被旧开关 ``true`` OR 成
#: ``true``；缺省场景由 ``[schedule].enabled`` 的出厂默认 ``True`` 承担（UQ-2：旧用户升级
#: 后 spine/生活状态/睡眠引擎随默认 True 全开，CHANGELOG/README 迁移段已说明）。
LEGACY_ROUTE_KEYS = {
    "schedule": ("spine_mode", "planning_daily_token_budget", "max_tokens", "spine_max_tokens"),
    "schedule_advanced": ("max_tokens",),
    "llm": ("judge_max_tokens", "max_tokens"),
    "event_catalog": ("max_tokens", "daily_token_budget"),
    "judge": ("max_tokens",),
    "life_state": ("enabled", "sleep_engine_enabled", "profile_path"),
    # 2026-11-01 U 计划（§4.2）：入口不再搬进面板标题栏，「面板内麦麦尺寸」随之退役——
    # 详情展开以原入口为原点的粘连气泡替代（入口原位保留）。只 pop、不做值搬运。
    "maimai_interaction": ("detail_fish_size_px",),
}

#: 1.0.0（配置整理轮之前）[patrol] 「想念触发说明」的出厂文案——仅用于迁移时判断「用户改过没有」。
#: 为什么不拿新默认当参照：配置整理轮把该文案重写过（新版带【触发原因·想念】前缀），
#: 拿新默认比较会把老用户的出厂值误判成「用户自定义」并搬进 [prompts]（等于把文案回退成旧版）。
_LEGACY_MISS_REASON_PROMPT_DEFAULT = (
    "你已经有 {hours} 个小时没收到{relation}的消息了，你有点想TA了。可以主动开口问问近况、表达一下想念；但不必强行找话题，如果觉得此刻开口不自然，平淡地打个招呼也可以。"
)

#: 2026-10-10 段拆分：从 [schedule] 迁往 [schedule_advanced] 的字段（键名不变，仅换段）
_SCHEDULE_TO_ADVANCED_KEYS = (
    "timezone", "generation_timeout", "use_multi_round", "max_rounds",
    "quality_threshold", "recent_schedule_days", "history_message_limit", "knowledge_search_limit",
    "cross_day_activity", "min_description_length", "max_description_length", "emotion_influence",
    "lie_in_max_until", "list_draw_image", "image_timeout_seconds", "role_judge_enabled",
    "role_judge_temperature", "inject_cache_minutes", "cache_ttl", "cache_max_size",
    "max_future_activities", "auto_generate_time",
)


def _ui_i18n(en_label: str, en_hint: str = "") -> dict:
    """字段级英文翻译（并入 json_schema_extra；WebUI 按 i18n[locale]['label'/'hint'] 取用）。"""
    entry: Dict[str, str] = {"label": en_label}
    if en_hint:
        entry["hint"] = en_hint
    return {"i18n": {"en": entry}}


#: 出厂默认配置缓存（``CateyeSuiteConfig()`` 的 model_dump；无参构造不会递归——空输入在
#: 聚合校验器里被早退放行）。取不到时退回 ``{}``（搬运逻辑退化为老行为，不阻塞校验）。
_FACTORY_DEFAULTS: Dict[str, Any] = {}

logger = logging.getLogger(__name__)

#: UQ-1 一次性告警哨兵（进程级）：显式 ``[schedule].enabled=false`` 且存在任一旧开关为
#: true 的升级场景，整个进程生命周期只告警一次（迁移器可能因宿主重建+抢救被多次调用）。
_ROUTE_RETIRE_WARNED = False


def _warn_retired_route_switches(schedule_data: Any, life_data: Any) -> None:
    """UQ-1（2026-10-27 主协调代裁）：迁移 pop 退役键前的一次性停摆告知。

    仅当用户**显式**配置 ``[schedule].enabled=false`` 且 ``[life_state].enabled`` /
    ``[life_state].sleep_engine_enabled`` 任一为 true 时告警：按裁定 2，日程开关保持
    false（不得被旧开关 OR 成 true），原本在跑的生活状态/睡眠引擎随升级停摆——写一条
    告警日志告知，不阻断迁移、不改任何配置值。
    """
    global _ROUTE_RETIRE_WARNED
    if _ROUTE_RETIRE_WARNED:
        return
    try:
        explicit_schedule_off = (isinstance(schedule_data, dict)
                                 and schedule_data.get("enabled") is False)
        legacy_on = (isinstance(life_data, dict)
                     and (bool(life_data.get("enabled"))
                          or bool(life_data.get("sleep_engine_enabled"))))
        if explicit_schedule_off and legacy_on:
            logger.warning(
                "PR8 3.0.0 迁移提示：检测到 [schedule].enabled=false 且旧开关"
                "（[life_state].enabled / [life_state].sleep_engine_enabled）为 true。"
                "日程总开关保持关闭（不会因旧开关为 true 而开启），生活状态与睡眠引擎"
                "随升级停摆；两个旧开关键已退役，如需启用请设置 [schedule].enabled=true。")
            _ROUTE_RETIRE_WARNED = True
    except Exception:  # 告警失败绝不阻塞迁移
        pass


def factory_defaults() -> Dict[str, Any]:
    """出厂默认配置字典（带缓存；供迁移器比较"用户是否显式配过"）。"""
    if not _FACTORY_DEFAULTS:
        try:
            _FACTORY_DEFAULTS.update(CateyeSuiteConfig().model_dump(mode="python"))
        except Exception:  # pragma: no cover - 理论不可达（模型恒可构造）
            return {}
    return _FACTORY_DEFAULTS


def _strip_factory_defaults(data: Dict[str, Any]) -> Dict[str, Any]:
    """剔除「仍等于出厂默认」的段内键，还原出「用户显式配置过」的视图。

    **为什么必须这样做**（2026-10-10 实证）：宿主 Runner 与 SDK 归一化都会**先把出厂默认
    merge 进输入**，于是旧键搬运用的 ``setdefault``/`键缺失` 判断永远命中不到缺失分支——
    跨段旧值（target_qq / timezone / miss_silence_hours 等）会被静默丢弃，用户升级后配置
    看似"迁移成功"实则全回默认。剔除等于默认的值后再搬运，`setdefault` 语义即恢复正确；
    被剔掉的键由随后的 pydantic 校验按默认值补回，无需手工回填。

    ``[plugin]`` 段不参与剔除（它的 ``config_version``/``layout_version`` 由迁移器显式管理）。
    非 dict 段原样保留。
    """
    defaults = factory_defaults()
    if not defaults:
        return data
    out: Dict[str, Any] = {}
    for section, value in data.items():
        dflt = defaults.get(section)
        if section == "plugin" or not isinstance(value, dict) or not isinstance(dflt, dict):
            out[section] = value
            continue
        out[section] = {k: v for k, v in value.items() if k not in dflt or v != dflt[k]}
    return out


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Plugin", "description": "Basic plugin settings."}
    }

    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关 = 全部模块停摆（含命令与工具）",
            **_ui_i18n("Enable plugin"),
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "label": "配置版本",
            "hidden": True,
            "disabled": True,
            **_ui_i18n("Config version"),
        },
    )
    layout_version: str = Field(
        default="",
        description="段布局标记（2026-10-10 配置整理轮的迁移幂等用；用户不要改）",
        json_schema_extra={
            "label": "布局版本",
            "hidden": True,
            **_ui_i18n("Layout version"),
        },
    )


class LlmSectionConfig(PluginConfigBase):
    """LLM 调用集中段（2026-10-10 新增）。

    插件里**所有**模型调用都经 ``core/llm_utils.LLMService`` 这一个模块发出；
    本段集中管理它们的模型路由与判定参数（原先散在 schedule/judge/affection/emotion/qzone/cateye 六处）。
    任务名 = 宿主 ``model_config.toml`` 里的任务键（如 utils / replyer / planner）或具体模型名。
    """

    __ui_label__ = "LLM 调用"
    __ui_icon__ = "cpu"
    __ui_order__ = 8
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "LLM Calls",
               "description": "Model routing for every LLM call the plugin makes (task names and judge parameters)."}
    }

    task_name: str = Field(
        default="replyer",
        description="主任务名：日程生成、工具发言等「生成类」调用默认用它（宿主模型任务键，如 replyer/utils/planner）",
        json_schema_extra={
            "label": "主任务名",
            "hint": "决定 bot「说人话」的能力；换模型后行为变化最明显的就是这一项",
            **_ui_i18n("Main task", "Task used by generation-style calls."),
        },
    )
    judge_model: str = Field(
        default="",
        description="判断用模型：填宿主任务名或具体模型名；留空 = 跟随「主任务名」。"
                    "仅判定类 ``judge()`` 调用（当前唯一调用点 = 想念把关）使用；"
                    "角色裁判 / 睡眠裁判不读它（各走自己的任务名）",
        json_schema_extra={
            "label": "判断用模型",
            "hint": "建议选择能稳定返回简短结论的模型；输出长度由宿主任务配置控制。"
                    "只影响想念把关这类 judge() 判定，角色裁判/睡眠裁判不受影响",
            **_ui_i18n("Judge model", "Empty = follow the main task. "
                                      "Only used by judge()-style verdict calls (miss referee); "
                                      "the role judge and sleep referee use their own task names."),
        },
    )

    judge_temperature: float = Field(
        default=0.2,
        ge=0.0,
        le=2.0,
        description="判断调用的采样温度：越低结论越稳定（判定不需要创意）",
        json_schema_extra={
            "label": "判断温度",
            "step": 0.1,
            "hint": "默认 0.2，一般不用动",
            **_ui_i18n("Judge temperature"),
        },
    )
    review_task_name: str = Field(
        default="utils",
        description="好感裁判任务名：会话采样评估与关系日结使用（建议便宜、非思考型）",
        json_schema_extra={
            "label": "好感裁判任务名",
            **_ui_i18n("Affection review task"),
        },
    )
    emotion_referee_task_name: str = Field(
        default="utils",
        description="情绪睡眠裁判任务名：睡眠期情绪大修使用，默认 utils，建议选择能稳定输出结构化正文的非思考任务。"
                    "注意：宿主主任务为思考型时，思考内容可能用尽宿主输出长度而没有正文、"
                    "裁判连续失败后本周期大修放弃（2026-10-28 真机实测）——此时应改绑非思考任务（如 utils）",
        json_schema_extra={
            "label": "情绪裁判任务名",
            "hint": "宿主主任务为思考型时建议改绑便宜非思考任务，否则裁判正文可能为空",
            **_ui_i18n("Emotion referee task"),
        },
    )
    emotion_evaluator_task_name: str = Field(
        default="utils",
        description="情绪评估任务名：回复/事件的 ΔVAD 增量评估使用（调用频繁，建议便宜、非思考型）",
        json_schema_extra={
            "label": "情绪评估任务名",
            **_ui_i18n("Emotion evaluator task"),
        },
    )
    qzone_task_name: str = Field(
        default="utils",
        description="QQ 空间任务名：说说文案与评论回复使用",
        json_schema_extra={
            "label": "空间任务名",
            **_ui_i18n("Qzone task"),
        },
    )
    vlm_task: str = Field(
        default="vlm",
        description="视觉任务名：屏幕截图理解使用（必须是支持图片输入的视觉模型任务）",
        json_schema_extra={
            "label": "视觉任务名",
            "hint": "指错成纯文本模型时屏幕感知只会返回「看不清」",
            **_ui_i18n("Vision task"),
        },
    )


class LifeStateSuiteSectionConfig(LifeStateSectionConfig):
    """生活状态段（滚动化）——聚合挂载专用子类，仅覆盖 WebUI 段序。

    R7 依据 = 用户 2026-10-25 直派（日程系统滚动化改造，设计目录
    D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计）：
    ``__ui_order__`` 增 life_state=11（排末尾，不打乱既有 0~10 段序）。键位与默认值
    全部沿用 planning/config.py 的 :class:`LifeStateSectionConfig`（单一真源，17 键、
    enabled=False 默认关）；planning.config 上的原类保持 ``__ui_order__=6`` 不动，
    供 ``planning.config.section_or_default`` 的未挂载兜底路径继续引用。
    """

    __ui_order__ = 11


class AdminSectionConfig(PluginConfigBase):
    """管理与日志（跨模块共享）。"""

    __ui_label__ = "管理与日志"
    __ui_icon__ = "shield"
    __ui_order__ = 10
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Admin & Logs", "description": "Command permissions and diagnostic logs."}
    }

    admin_users: List[str] = Field(
        default_factory=list,
        description="命令管理员（QQ 号或 qq:123456 形式）；留空 = 仅本机控制台可用管理子命令",
        json_schema_extra={"label": "管理员", **_ui_i18n("Admin users")},
    )
    allowed_streams: List[str] = Field(
        default_factory=list,
        description="命令/注入允许的会话（session_id）；留空 = 不限制",
        json_schema_extra={"label": "允许会话", **_ui_i18n("Allowed streams")},
    )
    llm_log_enabled: bool = Field(
        default=True,
        description="记录 LLM 调用日志（JSONL，按天分文件）",
        json_schema_extra={
            "label": "LLM 日志",
            "hint": "排查「模型答非所问」时必看；按天滚动、默认保留 3 天",
            **_ui_i18n("LLM log"),
        },
    )
    decision_log_enabled: bool = Field(
        default=True,
        description="记录巡检/空间/规划决策日志（JSONL，按天分文件）",
        json_schema_extra={
            "label": "决策日志",
            "hint": "排查「为什么没主动说话」时必看",
            **_ui_i18n("Decision log"),
        },
    )
    log_retention_days: int = Field(
        default=3,
        ge=1,
        le=30,
        description="日志保留天数",
        json_schema_extra={
            "label": "日志保留天数",
            "hint": "超过天数的日志自动清理（1-30 天）",
            **_ui_i18n("Log retention (days)"),
        },
    )
    # ── 告状工具（2026-10-24 用户直派）：bot 受骚扰时向维护者转达 ──
    harass_report_enabled: bool = Field(
        default=True,
        description="告状工具：bot 受骚扰时可向维护者告状（false = 工具不注册）",
        json_schema_extra={
            "label": "告状工具",
            "hint": "bot 被恶意刷屏/辱骂/攻击时把情况转达给维护者；关闭后不注册该 Tool",
            **_ui_i18n("Harass report"),
        },
    )
    harass_report_target_qq: str = Field(
        default="",
        description="告状对象 QQ；留空 = 回退 admin_users 第一个纯 QQ 元素（两者皆空则工具提示未配置维护者）",
        json_schema_extra={
            "label": "告状对象 QQ",
            "hint": "维护者（能处理骚扰问题的人）的 QQ 号",
            **_ui_i18n("Harass report target"),
        },
    )
    harass_report_cooldown_minutes: int = Field(
        default=30,
        ge=0,
        description="告状冷却分钟数（0 = 不冷却）",
        json_schema_extra={
            "label": "告状冷却（分钟）",
            "hint": "两次告状之间的最小间隔，防止刷屏维护者",
            **_ui_i18n("Harass cooldown (min)"),
        },
    )


class MaimaiInteractionSectionConfig(PluginConfigBase):
    """麦麦互动段（2026-10-30 交互改造新增，聚合配置的最后一段）。

    集中管理 WebUI 左下角常驻麦麦角色与全屏详情的展示/交互参数。
    这些值经 ``planning.dashboard.build_life_dashboard`` 的只读 ``maimai``
    白名单投影下发给前端（§投影只含展示/交互参数，绝不下发整份配置）；
    前端不写第二套默认值，投影缺失时按同值 CSS 兜底显示。

    正式显示名尚未定稿，暂按主计划以「？？？」占位；技术节名 maimai_interaction
    已定稿（TOML 裸键不支持问号，见主计划第三章整合口径 §5）。
    """

    __ui_label__ = "？？？"
    __ui_icon__ = "heart"
    __ui_order__ = 13
    __ui_i18n__: ClassVar[Dict[str, Dict[str, str]]] = {
        "en": {"title": "Maimai Interaction",
               "description": "Corner mascot and fullscreen detail look & feel (display-only)."}
    }

    enabled: bool = Field(
        default=True,
        description="常驻麦麦与互动总开关；关闭后整个页面不显示常驻麦麦，板块详情入口仍可用",
        json_schema_extra={
            "label": "常驻麦麦开关",
            "hint": "只影响左下角常驻角色；各板块详情入口不受影响",
            **_ui_i18n("Mascot enabled", "Hides the corner mascot; detail entries stay."),
        },
    )
    mascot_size_px: int = Field(
        default=128, ge=48, le=192,
        description="常驻麦麦基准尺寸（像素；2026-11-01 目检修正 96→128，探头更大更清晰）",
        json_schema_extra={"label": "常驻麦麦尺寸", "step": 1, **_ui_i18n("Mascot size (px)")},
    )
    peek_angle_deg: int = Field(
        default=12, ge=0, le=90,
        description="收起探头时麦麦的头部倾斜角（度；2026-11-01 目检修正 45→12，贴近 logo 冒头效果）",
        json_schema_extra={"label": "冒头角度", "step": 1, **_ui_i18n("Peek angle (deg)")},
    )
    hover_out_px: int = Field(
        default=200, ge=60, le=320,
        description="指针离左下角交互区域多远后自动收起（像素；进入阈值更小，进出不同阈值防抖）",
        json_schema_extra={"label": "收起距离", "step": 1, **_ui_i18n("Retreat distance (px)")},
    )
    hover_out_ms: int = Field(
        default=240, ge=80, le=600,
        description="收起动画时长（毫秒）",
        json_schema_extra={"label": "收起时长", "step": 10, **_ui_i18n("Retreat duration (ms)")},
    )
    press_squash_x: float = Field(
        default=1.20, ge=0.8, le=1.6,
        description="按住时的横向扩张系数",
        json_schema_extra={"label": "按压横向扩张", "step": 0.05, **_ui_i18n("Press squash X")},
    )
    press_squash_y: float = Field(
        default=0.50, ge=0.2, le=1.0,
        description="按住时的纵向压扁系数",
        json_schema_extra={"label": "按压纵向压扁", "step": 0.05, **_ui_i18n("Press squash Y")},
    )
    bounce_ms: int = Field(
        default=774, ge=200, le=3000,
        description="一次回弹动画周期（毫秒）",
        json_schema_extra={"label": "回弹周期", "step": 10, **_ui_i18n("Bounce duration (ms)")},
    )
    bounce_easing: str = Field(
        default="cubic-bezier(.34,1.56,.64,1)",
        description="回弹缓动曲线（CSS easing；无法识别时前端回退为 overshoot）",
        json_schema_extra={"label": "回弹缓动", **_ui_i18n("Bounce easing")},
    )
    detail_blur_px: int = Field(
        default=3, ge=0, le=40,
        description="详情背景轻微高斯模糊半径（像素，建议 2–4；0 = 不模糊。2026-11-01 起默认 3，不再浓遮罩）",
        json_schema_extra={"label": "详情背景模糊", "step": 1, **_ui_i18n("Detail blur (px)")},
    )
    detail_fade_ms: int = Field(
        default=220, ge=80, le=600,
        description="详情进入/退出动画时长（毫秒）",
        json_schema_extra={"label": "详情动画时长", "step": 10, **_ui_i18n("Detail fade (ms)")},
    )
    detail_scale_from: float = Field(
        default=0.6, ge=0.4, le=1.0,
        description="详情气泡从入口展开时的初始缩放（2026-11-01 起从入口原点展开）",
        json_schema_extra={"label": "详情初始缩放", "step": 0.01, **_ui_i18n("Detail scale from")},
    )
    history_changes_limit: int = Field(
        default=30, ge=5, le=30,
        description="详情「最近变化」最大条数（5–30）",
        json_schema_extra={"label": "最近变化条数", "step": 1, **_ui_i18n("Recent changes limit")},
    )
    history_state_epsilon: float = Field(
        default=0.01, ge=0.001, le=0.1,
        description="状态变化记录阈值（占比 0.001–0.1）",
        json_schema_extra={"label": "状态变化阈值", "step": 0.001, **_ui_i18n("State epsilon")},
    )
    history_debt_epsilon: float = Field(
        default=0.1, ge=0.01, le=1.0,
        description="睡眠债变化记录阈值（小时）",
        json_schema_extra={"label": "睡眠债阈值", "step": 0.01, **_ui_i18n("Debt epsilon (h)")},
    )


class CateyeSuiteConfig(PluginConfigBase):
    """插件完整配置。

    注意：子分组必须扁平挂为顶层字段（嵌套段 WebUI 不渲染）。
    各 section 类定义在各子模块的 config.py / suite_prompts.py 中（模块化维护）。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    # 陪伴模块（companion/config.py；含目标用户与屏幕感知）
    patrol: PatrolSectionConfig = Field(default_factory=PatrolSectionConfig)
    affection: AffectionSectionConfig = Field(default_factory=AffectionSectionConfig)
    # 日程（planning/config.py）
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    schedule_advanced: ScheduleAdvancedSectionConfig = Field(default_factory=ScheduleAdvancedSectionConfig)
    planning_proactive: PlanningProactiveSectionConfig = Field(default_factory=PlanningProactiveSectionConfig)
    # 生活状态（滚动化，2026-10-25 新增段；默认关——enabled=false 时插件行为与 2.1.0 一致）
    life_state: LifeStateSuiteSectionConfig = Field(default_factory=LifeStateSuiteSectionConfig)
    # 事件库（PR4，2026-10-26 新增段；默认 mode=off——连生成库都不读，只用内置池）。
    # 段类定义在 planning/event_catalog_config.py（与运行期只读视图同源）。
    event_catalog: EventCatalogSectionConfig = Field(default_factory=EventCatalogSectionConfig)
    # LLM 调用集中段（本文件）与提示词集中段（suite_prompts.py）
    llm: LlmSectionConfig = Field(default_factory=LlmSectionConfig)
    prompts: PromptsSectionConfig = Field(default_factory=PromptsSectionConfig)
    # 管理与日志（本文件）
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    # QQ 空间（qzone/config.py）
    qzone: QzoneSectionConfig = Field(default_factory=QzoneSectionConfig)
    # 情绪系统（emotion/config.py）
    emotion: EmotionSectionConfig = Field(default_factory=EmotionSectionConfig)
    # 视频观看（video_watch/config.py，2026-11-01 B 线内置模块）
    video_watch: VideoWatchSectionConfig = Field(default_factory=VideoWatchSectionConfig)
    # 麦麦互动（本文件，2026-10-30 交互改造新增）：放在最后一个字段，
    # 保证 config.toml 的最后一节就是 [maimai_interaction]（主计划第三章口径）。
    maimai_interaction: MaimaiInteractionSectionConfig = Field(default_factory=MaimaiInteractionSectionConfig)

    @pydantic.model_validator(mode="before")
    @classmethod
    def _migrate_legacy_sections(cls, data: object) -> object:
        """跨段旧键迁移（2026-10-10 配置整理轮；**一次性、幂等**）。

        宿主在版本号不变时把原始配置（含已删段）交到这里，本校验器负责：
        1. [target] / [cateye] / [judge] / [inject] / [planning_cleanup] 五个已删段的
           字段搬进新家（搬完即从字典里 pop，落盘/校验都不再看到它们）；
        2. 各段内部改名键的搬运（llm_task_name→[llm].task_name 等）；
        3. [patrol] 段内部已删键的清理与阈值合并（companion.config.migrate_legacy_patrol）；
        4. [affection] 段内部已删键的清理（:data:`LEGACY_AFFECTION_KEYS`；2026-10-17 §19.4
           加：v1 三档的 ``current_level`` 在入口显式 pop，两个分支都经过）；
        5. [patrol]/[qzone] 的已删静默键（:data:`LEGACY_SILENCE_KEYS`）与
           [planning_proactive] 的已删主动行为键（:data:`LEGACY_PROACTIVE_KEYS`，2026-10-20
           用户指示）同口径在入口显式 pop（两个分支都经过）。2026-10-22 §27.1（R7）起
           ``greeting_require_activation`` 已回归真配置键，不在该元组内、旧显式值保留生效。
        6. [event_catalog] 旧键 ``max_events`` → 规范字段 ``max_generated_events``（2026-10-27
           用户裁定）：在**默认值填充之前**按原始字典搬迁（见
           :func:`planning.event_catalog_config.migrate_legacy_event_catalog`），迁完显式 pop
           旧键；规范字段原始显式值优先。

        **幂等**：迁移完成时在 ``[plugin] layout_version`` 写下本轮布局标记；下一次校验
        （迁移结果被宿主落盘后再读回）看到该标记即跳过全部值搬运——新旧同名的键
        （如 ``proactive_group_ids``）不会被二次解释。本校验器还会给
        ``[planning_proactive]`` 放一枚 ``_layout_migrated`` 哨兵，通知段级校验器是否跳过
        它自己的旧名单映射（见 PlanningProactiveSectionConfig._migrate_legacy_lists）。

        **标记只在输入非空时写入**（2026-10-10 第二轮修复）：Pydantic 在无参构造（出厂默认）
        时也会调用本校验器，若此时写字面标记，出厂默认本身就会带标记 → 宿主/SDK 归一化
        merge 默认后 ``already_migrated`` 恒为 True → 迁移器在真实宿主路径下永不执行。
        故 ``if not data: return data``（空输入原样返回，标记留空）。

        缺陷史（2026-10-10 离线自检抓到）：无标记版迁移在"落盘 → 再读回"时会把新白名单
        当成旧白名单再映射一遍（群聊名单在黑名单模式下被误转白名单）→ 非幂等，故加标记。
        非 dict 输入（模型实例 / None）原样放行。
        """
        if not isinstance(data, dict):
            return data
        if not data:
            # 出厂默认路径（无参构造 / 空输入）：不写布局标记，也不放哨兵
            return data
        migrated = dict(data)
        # 0a) [event_catalog] 旧键 max_events → 规范字段 max_generated_events（2026-10-27 用户裁定）。
        #     必须发生在「默认值填充之前」：宿主/SDK 先 merge 出厂默认，之后 pydantic 的
        #     model_fields_set 会把每个字段都当显式值，段级 resolve_config 的「规范字段优先」
        #     判据失效 → 只写 max_events=6 的旧配置会被默认 8 吞掉。此处按原始字典搬迁后
        #     删旧键，落盘就是规范形态（幂等：第二次进入不再有 max_events）。
        if "event_catalog" in migrated:
            migrated["event_catalog"] = migrate_legacy_event_catalog(
                migrated.get("event_catalog"),
                (factory_defaults() or {}).get("event_catalog"))
        # 0) 段内已删键清理——放在两个分支**都经过**的入口处（清旧键性质，与 already_migrated
        #    分支只清旧段同精神：不搬运、不回填）。2026-10-17 §19.4 / 复核 findings#4：
        #    v1 三档口径的 [affection].current_level 无法映射 0~200 新标度（§9.7 教训），
        #    无条件 pop 丢弃（显式 pop，不用 setdefault、不做键缺失判断——§9.5）。
        legacy_affection = migrated.get("affection")
        if isinstance(legacy_affection, dict):
            legacy_affection = dict(legacy_affection)
            for stale_key in LEGACY_AFFECTION_KEYS:
                legacy_affection.pop(stale_key, None)
            migrated["affection"] = legacy_affection
        #    2026-10-20：[patrol]/[qzone] 的 silence_start/silence_end 同口径显式 pop
        #    （静默窗恒复用 [schedule] 作息，显式覆盖能力已移除）。
        for section_name, stale_keys in LEGACY_SILENCE_KEYS.items():
            section_data = migrated.get(section_name)
            if isinstance(section_data, dict):
                section_data = dict(section_data)
                for stale_key in stale_keys:
                    section_data.pop(stale_key, None)
                migrated[section_name] = section_data
        #    2026-10-20 用户指示：日程主动行为整条移除 → [planning_proactive] 两个旧键
        #    （enable_proactive_trigger / proactive_fresh_window_minutes）同口径在此显式
        #    pop（两分支都经过；§9.7）。greeting_require_activation 已于 2026-10-22 用户
        #    裁定回归为真配置键（群聊必要性检查，R7 计划书 §二十七），不在此 pop。
        for section_name, stale_keys in LEGACY_PROACTIVE_KEYS.items():
            section_data = migrated.get(section_name)
            if isinstance(section_data, dict):
                section_data = dict(section_data)
                for stale_key in stale_keys:
                    section_data.pop(stale_key, None)
                migrated[section_name] = section_data
        #    PR8 3.0.0：旧路线/运行期开关键（:data:`LEGACY_ROUTE_KEYS`）同口径在入口
        #    显式 pop（两分支都经过；幂等天然成立）。**绝不写 [schedule].enabled**：
        #    显式 false 保持 false（裁定 2），缺省由出厂默认 True 承担（UQ-2）。pop 前
        #    按 UQ-1 做一次性停摆告知。
        _warn_retired_route_switches(migrated.get("schedule"), migrated.get("life_state"))
        for section_name, stale_keys in LEGACY_ROUTE_KEYS.items():
            section_data = migrated.get(section_name)
            if isinstance(section_data, dict):
                section_data = dict(section_data)
                for stale_key in stale_keys:
                    section_data.pop(stale_key, None)
                migrated[section_name] = section_data
        #    B 计划（2026-11-01）：旧独立观看插件节名 [bili_watch] → 内置 [video_watch]
        #    段（仅当新段缺位时整体搬迁，幂等：video_watch 在场即不再触发；
        #    旧节 6 键与新段字段同名同约束，值原样保留）。
        legacy_bili = migrated.get("bili_watch")
        if isinstance(legacy_bili, dict) and not isinstance(migrated.get("video_watch"), dict):
            migrated["video_watch"] = dict(legacy_bili)
        plugin_section = dict(migrated.get("plugin") or {})
        already_migrated = str(plugin_section.get("layout_version") or "") == CURRENT_LAYOUT_VERSION
        # 结构版本以插件当前值为准（宿主已按"版本变化"决定升级；旧值不参与落盘）。
        # 若把旧版本号原样落回去，下一次加载会再次触发重建 + 抢救（且磁盘已带标记 → 抢救跳过），
        # 跨段旧值会在这条路径上被丢掉——必须强制归位。
        plugin_section["config_version"] = SUPPORTED_CONFIG_VERSION

        if already_migrated:
            # 已迁移：只清掉理论上不该再出现的旧段（用户手工加回时忽略不炸），不做任何值搬运
            proactive_data = dict(migrated.get("planning_proactive") or {})
            proactive_data["_layout_migrated"] = True
            migrated["planning_proactive"] = proactive_data
            for stale in LEGACY_SECTIONS:
                migrated.pop(stale, None)
            migrated["plugin"] = plugin_section
            return migrated

        # 值搬运前先剔除"仍等于出厂默认"的键（把 merge 了默认的输入还原成用户显式值视图；
        # 否则下面的 setdefault 永远命中不到缺失分支，旧值被静默丢弃——见 _strip_factory_defaults）
        migrated = _strip_factory_defaults(migrated)

        # [planning_proactive] 哨兵：True = 已迁移形态（段级校验器跳过旧名单映射）
        proactive_data = dict(migrated.get("planning_proactive") or {})
        proactive_data["_layout_migrated"] = False
        migrated["planning_proactive"] = proactive_data

        # 1) [target] → [patrol].target_qq（其余旧键丢弃）
        legacy_target = migrated.pop("target", None)
        patrol_data = migrate_legacy_patrol(migrated.get("patrol") or {}, legacy_target)
        migrated["patrol"] = patrol_data

        # 2) [cateye] → [patrol].screen_* / [llm].vlm_task / [prompts].screen_*
        cateye_migrated = migrate_legacy_cateye(migrated.pop("cateye", None))
        if cateye_migrated["patrol"]:
            for key, value in cateye_migrated["patrol"].items():
                migrated["patrol"].setdefault(key, value)
        llm_data = dict(migrated.get("llm") or {})
        if cateye_migrated["llm"].get("vlm_task"):
            llm_data.setdefault("vlm_task", cateye_migrated["llm"]["vlm_task"])
        prompts_data = dict(migrated.get("prompts") or {})
        for key, value in cateye_migrated["prompts"].items():
            prompts_data.setdefault(key, value)

        # 3) [judge] → [llm].judge_*
        judge_data = migrated.pop("judge", None)
        if isinstance(judge_data, dict):
            for old, new in (("model", "judge_model"),
                             ("temperature", "judge_temperature")):
                if old in judge_data:
                    llm_data.setdefault(new, judge_data.get(old))

        # 4) 各段任务名 → [llm]
        schedule_data = dict(migrated.get("schedule") or {})
        if schedule_data.get("llm_task_name"):
            llm_data.setdefault("task_name", schedule_data.pop("llm_task_name"))
        else:
            schedule_data.pop("llm_task_name", None)
        affection_data = dict(migrated.get("affection") or {})
        if affection_data.get("review_task_name"):
            llm_data.setdefault("review_task_name", affection_data.pop("review_task_name"))
        else:
            affection_data.pop("review_task_name", None)
        emotion_data = dict(migrated.get("emotion") or {})
        if emotion_data.get("referee_task_name"):
            llm_data.setdefault("emotion_referee_task_name", emotion_data.pop("referee_task_name"))
        else:
            emotion_data.pop("referee_task_name", None)
        if emotion_data.get("evaluator_task_name"):
            llm_data.setdefault("emotion_evaluator_task_name", emotion_data.pop("evaluator_task_name"))
        else:
            emotion_data.pop("evaluator_task_name", None)
        qzone_data = dict(migrated.get("qzone") or {})
        if qzone_data.get("llm_task_name"):
            llm_data.setdefault("qzone_task_name", qzone_data.pop("llm_task_name"))
        else:
            qzone_data.pop("llm_task_name", None)

        # 3b) [patrol] 的两个可编提示词（1.0.0 的 miss_reason_prompt / miss_confirm_prompt）
        #     → [prompts] companion_miss_reason / companion_miss_confirm（**仅非出厂值才搬**，
        #     与 [cateye] 提示词同口径；T3 问题表 #3 用户裁决 A：不搬会静默回退出厂文案）
        for old_key, new_key, factory_texts in (
            ("miss_reason_prompt", "companion_miss_reason",
             (_LEGACY_MISS_REASON_PROMPT_DEFAULT, COMPANION_MISS_REASON_DEFAULT)),
            ("miss_confirm_prompt", "companion_miss_confirm",
             (COMPANION_MISS_CONFIRM_DEFAULT,)),
        ):
            legacy_text = patrol_data.pop(old_key, None)
            if (isinstance(legacy_text, str) and legacy_text.strip()
                    and legacy_text not in factory_texts):
                prompts_data.setdefault(new_key, legacy_text)

        # 5) [schedule] 深水字段 → [schedule_advanced]（键名不变；2026-10-10 段拆分）
        advanced_data = dict(migrated.get("schedule_advanced") or {})
        for key in _SCHEDULE_TO_ADVANCED_KEYS:
            if key in schedule_data:
                advanced_data.setdefault(key, schedule_data.pop(key))

        # 6) [inject] / [planning_cleanup] → [schedule_advanced]（键名不变）
        for legacy_section in ("inject", "planning_cleanup"):
            legacy = migrated.pop(legacy_section, None)
            if isinstance(legacy, dict):
                for key, value in legacy.items():
                    advanced_data.setdefault(key, value)

        # 迁移完成：写下本轮布局标记（下次校验据此跳过值搬运，保证幂等）
        plugin_section["layout_version"] = CURRENT_LAYOUT_VERSION
        migrated["plugin"] = plugin_section

        migrated["llm"] = llm_data
        migrated["prompts"] = prompts_data
        migrated["schedule"] = schedule_data
        migrated["affection"] = affection_data
        migrated["emotion"] = emotion_data
        migrated["qzone"] = qzone_data
        migrated["schedule_advanced"] = advanced_data
        return migrated
