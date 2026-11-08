"""属于麦麦的生活（lbm）插件 — 顶层配置聚合。

- 文件名刻意不叫 ``config.py``：宿主 Runner 加载插件时 sys.path 含宿主 ``src``，
  且同一进程可能加载多个插件，顶层 ``config`` 会被宿主 ``src/config`` 或
  兄弟插件抢占（L1 实证），故改名并由 plugin.py 顶部引导头接管自身模块解析。
- 各业务子模块各自拥有自己的配置 section 类（模块化维护）：
  - planning/config.py  日程系统：schedule / schedule_advanced / planning_proactive
  - companion/config.py 陪伴模块：patrol（含目标用户与屏幕感知）/ affection
  - suite_prompts.py    提示词集中模块：prompts
  - qzone/config.py     QQ 空间：qzone / qzone_video（空间视频候选·影子模式，PR-A0）
  - emotion/config.py   情绪系统：emotion
- 顶层只保留跨模块共享的基础段（plugin / admin）与新增的 LLM 调用段（llm），
  并把各子模块 section 扁平挂载为聚合模型的顶层字段（嵌套段 WebUI 不渲染）。

## 2026-10-10 配置整理轮（本次改动的总说明）

**段布局变化**：原 [target] 并入 [patrol]（陪伴模块）、原 [cateye] 并入 [patrol]、
原 [judge] 并入新增 [llm]、原 [inject] 与 [planning_cleanup] 并入新增 [schedule_advanced]、
提示词集中进新增 [prompts]。段名保持不变的段（plugin/patrol/affection/schedule/
planning_proactive/admin/qzone/emotion）里的同名键，值会照旧保留。

**config_version 与迁移保障（2026-10-10 第二版，已订正；3.0.0 起版本随 PR8 递增）**：
开发规范（红线 R6）要求 `config_version` **与 `_manifest.json` 的插件版本同步**，故本值随插件版本递增（当前 `3.17.7`；历史：3.1.0 为 2026-11-04 配置精简版、3.0.0 为 2026-10-27 PR8 清理版、2.2.0 为 2026-10-27 二期 PR7 开发版、2.1.0 于 2026-10-21 归档发布）；
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

**3.1.0 迁移记录（2026-11-04 配置精简轮，用户「全量推进」裁决；方案 =
union/20261104_全量推进/配置精简方案.md）**：224 叶子字段 → 179（keep 179 / json 40 /
hardcode 5）。① **J 类 40 项**内部调参键移入 ``mappings/internal_tuning.json``（表层
用户表）+ ``default_mappings/internal_tuning.json``（种子）：旧 TOML 键由
:func:`core.config_migration.migrate_internal_tuning` 纯内存摘出，plugin.py
``normalize_plugin_config`` 在 SDK merge 出厂默认**之前**把捕获值落盘（只补用户表
缺失键，幂等不覆盖现场微调）；各 section 以同名只读 property 桥接 JSON 读取，
全部既有消费点零改动。② **H 类 5 项**固化为代码常量（生产现值=默认已获 evidence
证实）：``[life_state].tick_seconds``/``sleep_wake_check_seconds``（=300 内部节拍）、
``state_file``（逐字 'life_state.json'）、``[event_catalog].temperature``（锁定 0.9，
resolve_config 本就不读配置）、``max_events``（兼容死键，migrate_legacy_event_catalog
继续 pop）——旧键登记 :data:`LEGACY_ROUTE_KEYS` 入口 pop（只 pop 不搬运）。
③ ``CURRENT_LAYOUT_VERSION`` **不换新标记**（C-12 教训）：J 类落盘走独立磁盘捕获
通道（不受布局哨兵门控），不重跑 2026-10-10 值搬运体。④ 睡眠锚点/时钟/预算上限/
settlement_model/名单/凭据等 179 项 keep 逐字不动；4 个 evidence 未列段
（affection/schedule_advanced/prompts/dashboard_display）一律不 hardcode。

**3.2.0 迁移记录（2026-11-04 主动行为重写，用户直派规格；设计 =
union/20261104_主动行为与视频排查/主动行为重写设计.md）**：179 → **177 叶子字段**
（删 4 增 2）。① 删 ``[patrol]`` 四键（:data:`LEGACY_PATROL_RETIRED_KEYS`：
patrol_activity_enabled / activity_trigger_rate / default_speak_rate / miss_speak_rate）——
日程巡检触发路径整条删除、想念改状态驱动概率公式（companion.miss_trigger 内部常量），
旧键纯 pop 丢弃并登记（入口两分支共经 + plugin.normalize raw pop，无值搬运）。
② 增 ``[planning_proactive]`` 两个用户可感知字段：``greeting_morning_latest``
（早安有界上限时刻，默认 "10:00"）与 ``greeting_noon_enabled``（中午问好开关，默认 true，
仅目标陪伴私聊）。③ 按 R6 递增 SUPPORTED_CONFIG_VERSION=3.2.0；**_manifest.json 不在
本任务文件边界内，部署批须同步 version=3.2.0**。④ ``CURRENT_LAYOUT_VERSION`` 不换新
（C-12）；睡眠锚点/时钟/每日预算 5 红线逐字不动；磁盘抢救通道保住 keep 键用户值。

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
from typing import Any, ClassVar, Dict, List, Literal

import pydantic
from maibot_sdk import Field, PluginConfigBase

from companion.config import (
    AffectionSectionConfig,
    PatrolSectionConfig,
    migrate_legacy_cateye,
    migrate_legacy_patrol,
)
from companion.screen_config import ScreenSectionConfig, migrate_screen_config
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
from qzone.config import QzoneSectionConfig, QzoneVideoSectionConfig
from video_watch.config import VideoWatchSectionConfig
from video_watch.share_config import VideoShareSectionConfig
from core.config_migration import migrate_display_config, migrate_internal_tuning, migrate_release_config
from core.i18n import ui_i18n as _ui_i18n
from core.internal_tuning import tuning_value as _tuning_value

from suite_prompts import (
    COMPANION_MISS_REASON_DEFAULT,
    PromptsSectionConfig,
)

# 配置版本（**必须与插件版本 `_manifest.json` 的 version 同步**，红线 R6）：
# 插件版本表达"功能发布代次"；配置版本表达"配置文件结构代次"，宿主在它变化时会按新骨架
# 重建设置文件（丢弃跨段旧键）。因此递增本值必须同时具备"磁盘旧文件抢救"（plugin.py 的
# normalize_plugin_config：把尚未被覆写的磁盘 config.toml 里的用户值并回输入），
# 否则跨段/改名键的用户值会在插件看到它们之前被丢弃 —— 见模块 docstring 的教训段。
# 3.2.0（2026-11-04 主动行为重写）：删 4 个 [patrol] Field（日程巡检三键 + miss_speak_rate）
# 按 R6 递增本值；_manifest.json 不在本任务文件边界内，**部署批须同步递增至 3.2.0**
# （见设计文档 §配置治理）。磁盘抢救 + 入口纯 pop 保证旧值不丢、退役键不落盘。
# 3.10.0（2026-11-07 PR-A0 视频空间候选与权限边界·影子模式）：新增 [qzone_video] 段
# 4 字段（mode/candidate_ttl_hours/max_evaluations/decision_daily_limit），17→18 段、
# 182→186 字段；新段取默认、既有用户值不变，按 R6 双号同步递增。
# 3.10.1（2026-11-07 PR-A1 高清非拼图截图资产）：[qzone_video] +4 字段
# （min_width/max_assets_per_video/asset_ttl_hours/asset_max_total_mb），186→190 字段；
# 新键取默认、既有用户值不变，按 R6 双号同步递增。
# 3.10.2（2026-11-07 PR-A2 内容驱动的空间视频帖子接通）：[qzone_video] +2 字段
# （caption_max_chars/attach_source），190→192 字段；新键取默认、既有用户值不变，
# 按 R6 双号同步递增。
# 3.10.3（2026-11-07 PR-A3 回执幂等与失败恢复）：无新增配置字段（重试上限/尝试容量为
# 模块常量），schema 保持 18 段 192 字段；纯功能递增，按 R6 双号同步递增。
# 3.11.0（2026-11-07 PR-B1 生活经历索引，B 线起始）：[life_state] +2 字段
# （experience_index_enabled/experience_ttl_days），192→194 字段；新键取默认、
# 既有用户值不变，按 R6 双号同步递增。
# 3.12.0（2026-11-07 PR-B2 跨日事务 + 持续动机）：[life_state] +3 字段
# （motives_enabled/motive_max_count/motive_half_life_days），194→197 字段；
# 新键取默认、既有用户值不变，按 R6 双号同步递增。
# 3.13.0（2026-11-07 PR-C1 情绪原因时效 + 经历驱动的交流动机）：[emotion] +2 字段
# （emotion_reason_ttl_hours/emotion_reason_inject_enabled）、[planning_proactive] +1
# 字段（experience_share_motives_enabled），197→200 字段；新键取默认、
# 既有用户值不变，按 R6 双号同步递增。
# 3.14.0（2026-11-07 PR-C2 真实观看驱动的兴趣演变）：[video_watch] +2 字段
# （interest_watch_feedback_enabled/interest_watch_half_life_days），200→202 字段；
# 新键取默认、既有用户值不变，按 R6 双号同步递增。
# 3.15.0（2026-11-07 PR-C3 动态睡眠与硬意图一致性回归加固）：无新增配置字段
# （schema 不变，202 叶子）；3.8.0 已落地 P1–P3 不重复开发，本批以 14 例回归
# 矩阵加固为主（scratch/test_sleep_consistency_c3.py），按 R6 双号同步递增。
# 3.16.0（2026-11-07 PR-D1 空间多日主题延续与全链诊断）：无新增配置字段
# （schema 不变，202 叶子）；主题记忆复用 B1 索引不新建表，诊断挂既有 /lbm_diag
# 只读子输出（命令总数不变），WebUI 系统板块加计数芯片，按 R6 双号同步递增。
# 3.16.1（2026-11-07 PR-AUDIT 提示词统一人设前缀与重复人设清理）：无新增配置字段
# （schema 不变，18 段 202 叶子）；QZONE_POST_GATE_DEFAULT 默认模板内容变更但
# [prompts] 字段面不变（自定义模板保持），按 R6 双号同步递增。
# 3.17.0（2026-11-07 PR-WATCH /lbm_plan watch 改为强制 debug 命令并行触发）：无新增
# 配置字段（schema 不变，18 段 202 叶子）；debug 强制是命令固有语义（force_debug
# 仅命令路径置位），不新增配置键，按 R6 双号同步递增。
# 3.17.1（2026-11-07 PR-FIX-ASSET 修复空间视频判定 asset_ids 恒空）：无新增配置字段
# （schema 不变，18 段 202 叶子）；QZONE_VIDEO_POLICY 输出 Schema 增 asset_ids 与
# available_assets 选择规则、publish 资产闸增确定性回退选择，均不新增配置键，按 R6
# 双号同步递增。
# 3.17.2（2026-11-07 PR-PROACTIVE-REPLY 主动激活轮 reply 目标指令改写）：无新增配置
# 字段（schema 不变，18 段 202 叶子）；INJECT_PROACTIVE_TURN_RULE_DEFAULT 默认模板
# 主动语义化、proactive_reply_target_mode 描述更新（字段面/类型/默认值不变），
# 不新增配置键，按 R6 双号同步递增。
# 3.17.3（2026-11-08 PR-FIX-TZ 修复跨日 sleep_engine naive/aware datetime 比较异常）：
# 无新增配置字段（schema 不变，18 段 202 叶子）；睡眠引擎比较点时区规范化与 qzone
# 冷却锚点混比修复均为纯内部一致性修补，不新增配置键，按 R6 双号同步递增。
# 3.17.4（2026-11-08 PR-FIX-SLOT 修复 event_catalog.reload 丢失 time_slot 致生成
# 小事件全被时段过滤）：无新增配置字段（schema 不变，18 段 202 叶子）；装载
# normalized 字典补携 time_slot 为纯内部修补，时段过滤/内置/一次性语义不变，
# 不新增配置键，按 R6 双号同步递增。
# 3.17.5（2026-11-08 PR-MINSAMPLE 视频最小采样数 + 默认采样间隔改 10）：
# [video_watch] +1 可见键 min_sample_count（默认 16，ge1 le256；按间隔切分不足
# 此帧数时按此数平均抽样），18 段 202→203 叶子；sample_interval_seconds 默认
# 25→10（ge5 le600 不变）；拼图上限拒绝保护与有界采样不变，按 R6 双号同步递增。
# 3.17.6（2026-11-08 PR-REMOVE-POOLCAP 移除独立总池配额 + E_LLM 截断修复）：
# max_pool_events 本就非 schema Field（J 类 internal_tuning 键/只读属性），退役
# 不改变 schema 字段数（18 段 203 叶子不变）；旧 TOML 键经 LEGACY_ROUTE_KEYS 纯
# pop；事件生成 max_tokens 动态有界 override 为纯内部修补，按 R6 双号同步递增。
# 3.17.7（2026-11-08 PR-UI 三改进：行程原因人话化 draw_reason_zh + 日程池时段
# badge + 缓存命中 tooltip 左侧）：纯旁路展示改动，不影响调度，无新增配置字段
# （schema 不变，18 段 203 叶子），按 R6 双号同步递增。
# 3.17.8（2026-11-08 PR-WEBUI-TIMELINE 跨午夜渲染修复 + 当日活动概括横条）：
# 纯展示层改动（dashboard 只读投影 + WebUI），不影响调度/滚动/门控，无新增配置
# 字段（schema 不变，18 段 203 叶子），按 R6 双号同步递增。
# 3.17.9（2026-11-08 PR-TOOLBAR-LEFT 面板头部按钮改左侧纵向排列）：
# 纯布局改动（mast 双列 grid + toolbar 纵列 + 更多菜单右展开），不改按钮
# ID/行为/事件绑定，不影响调度，无新增配置字段（schema 不变，18 段 203 叶子），
# 按 R6 双号同步递增。
# 3.17.10（2026-11-08 PR-OVERVIEW-FULLWIDTH 当日活动概括全宽缩放 + 日程顶部常驻）：
# 纯展示改动（横条移出翻页内容置 #panel-schedule 首子 + 去 min-width/横滑改全宽
# 按比例缩放 + 渲染调用移 renderSchedulePage 顶部），24h 轴语义/颜色/浮窗/跨日
# 逻辑不变，不影响调度，无新增配置字段（schema 不变，18 段 203 叶子），
# 按 R6 双号同步递增。
# 3.17.11（2026-11-08 PR-TABS-LEFT 栏目导航改左侧纵向栏）：
# 纯布局改动（panel-layout 双列 grid + panel-tabs 纵向 sticky + panel-body 包裹
# datebar/5 tabpanel + 按钮顺序 此刻→日程→历史→心情与分享→系统 + 键盘加上下箭头），
# tab ID/data-tab/aria/切换逻辑不变，不影响调度，无新增配置字段（schema 不变，
# 18 段 203 叶子），按 R6 双号同步递增。
# 3.17.12（2026-11-08 PR-TOPBAR-ROW 刷新/更多恢复顶部横排，内容紧接其下）：
# 纯布局改动（mast 恢复 flex 横排 + toolbar row 可折行 + more-menu 向下展开，
# 级联尾部追加段压过 3.17.9 纵列段），按钮 ID/行为/事件绑定不变，3.17.11 纵向栏目
# 与 3.17.10 横条全宽保留，不影响调度，无新增配置字段（schema 不变，18 段 203
# 叶子），按 R6 双号同步递增。
# 3.17.13（2026-11-08 PR-ENTERTAINMENT 心情与分享→娱乐重做 + 情绪并此刻 + 翻页符号居中）：
# 只读投影展示层改动（tab 文案娱乐/glyph ▶、情绪卡移入 panel-now、娱乐面板新增
# 最近观看+兴趣话题 video 投影、空间与分享保留、翻页 ‹ › grid 居中），情绪功能
# 本身零改动（仅展示位置迁移），不影响调度/情绪引擎/视频观看运行态，无新增配置
# 字段（schema 不变，18 段 203 叶子），按 R6 双号同步递增。
# 3.17.14（2026-11-08 PR-PROACTIVE-GATE 主动激活统一入口 + 分类定制提示词）：
# 纯后端改动（tools/proactive_turn.py 登记簿 + 触发点登记 + replyer 钩子按
# chat_type×kind 定制改写 + 问候轮改写失效 bug 修复），不触碰预算/静默/好感
# 门控与计数口径，无新增配置字段（schema 不变，18 段 203 叶子），按 R6 双号同步递增。
# 3.17.16（2026-11-08 PR-LINKAGE-SCREEN 跨插件联动层架构 + 问候屏幕感知修复）：
# 纯后端架构改动（根目录 linkage/ 联动层 + LinkageAdapter 协议/注册表 + 首个适配器
# cateye_screen；companion/cateye_client.py 迁 linkage/screen/，screen_context.py 保留原位；
# ScreenPerceptionService 抽取 patrol 旁白逻辑；问候触发前对目标私聊 peek），屏幕键
# 仍在原 section、数据文件路径不变，无新增配置字段（schema 不变，18 段 203 叶子），
# 按 R6 双号同步递增。
# 3.17.17（2026-11-08 PR-RELIABILITY 外部评估可靠性修复 + 视频说说文案自然化）：
# 五项后端可靠性修复（registry 原子领取+登记前置 / append 限 user / optional_action
# 保存失败回滚 / save_json_atomic 唯一 tmp / qzone 评论触发异常留痕）均不新增配置
# 字段；[qzone_video].attach_source 退役（署名拼接不自然，文案改纯 LLM 自然分享，
# 提示词禁元信息），18 段 203→202 叶子；旧 TOML 键经 LEGACY_ROUTE_KEYS 纯 pop
# （RELEASE_RETIRED 口径，生产旧值自动清除），按 R6 双号同步递增。
# 3.17.18（2026-11-08 PR-RESPONSIVE-MAST mast 自适应收缩 + 时间戳 ellipsis 防溢出）：
# 纯 CSS 展示层修复（panel.css 级联尾部追加段：mast/mast-side 收缩约束 + #updated
# 单行省略号 + 嵌入态右距）+ panel.js render() 时间戳 title 全文；不新增配置字段
# （schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.19（2026-11-08 PR-SLEEP-RECOVERY 睡眠债补偿性恢复目标）：
# 纯后端睡眠引擎改动（会话级夜间恢复目标 8+min(1,债/12) 持久化 + 高债≥6h 窗口内
# 提前睡 + 阈值醒最小有效睡眠保护 + 边界冲突如实记 recovery_unmet_hours）；调参
# 走 internal_tuning dynamic_sleep 段（debt_trigger/target_max_bonus），不新增
# 用户可见配置字段（schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.20（2026-11-08 PR-REALISM-TUNING 社交/压力拟人化补全 + 宽屏填充）：
# 真实聊天消耗社交电量（replyer after_response 目标私聊真实回复 -0.015/次，
# 既有 apply_episode 显式增量门，diag event 桶 kind=chat）+ 睡眠债抬升压力基线
# （settlement_v2 stress floor = debt_stress_per_h × debt）+ ≥1600px 宽屏 shell
# 放宽 1760px；调参走 internal_tuning life_state 段（chat_social_drain_per_reply/
# debt_stress_per_h），不新增用户可见配置字段（schema 不变，18 段 202 叶子），
# 按 R6 双号同步递增。
# 3.17.21（2026-11-08 PR-UI-DETAIL 详情弹层调窄 + 心情详情曲线移除 + 历史心情修复）：
# 详情弹层 1040→720px 单列堆叠；心情详情不再重复 VAD 曲线（历史页独立呈现）；
# 历史心情稀疏采样断线修复（真实点圆点可见 + 缺口桥点线，不插入估算点）。
# 纯展示，不新增用户可见配置字段（schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.22（2026-11-08 PR-STATE-CONTRACT 状态字段契约修复 + 社交消耗平衡）：
# _social_state() 过滤键改与 LifeState.snapshot() 一致（social_battery/sleep_debt），
# 视频社交/空间视频决策拿到社交电量与睡眠债；计划社交消耗幅度有界上限
# （internal_tuning planned_social_drain_cap_per_min=0.006/min）软化一次触底，
# 不改用户画像表层映射；与真实聊天消耗来源分离。不新增用户可见配置字段
# （schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.23（2026-11-08 PR-VERTICAL-FILL 宽屏纵向填充）：≥1600px 收回 shell 底部预留
# 184→16px、panel-layout align-items:stretch、此刻页 flex 列 + now-duo flexGrow 填满
# 剩余高度，消除固定框架下底部空白带；纯展示，不新增用户可见配置字段
# （schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.24（2026-11-09 PR-TABS-STICKY 宽屏左栏固定回归修复）：3.17.23 的
# panel-layout align-items:stretch 把 sticky 左栏撑满滚动容器致其随内容滚动；
# ≥1600px 追加 .panel-tabs{align-self:start} 恢复内容高+sticky 固定，右栏 body
# 仍 stretch 保持纵向填充。纯展示，不新增用户可见配置字段（schema 不变，
# 18 段 202 叶子），按 R6 双号同步递增。
# 3.17.25（2026-11-09 PR-DETAIL-TWOCOL 详情弹层改回双列窄宽）：用户裁定保留双列+
# 窄宽（720px），meta 行各占一列压缩标签—值中间空白（3.17.21 单列反而拉大空白）；
# 摘要/折叠组/区块跨双列。逐页目检 11 详情弹层观感确认。纯展示，不新增用户可见
# 配置字段（schema 不变，18 段 202 叶子），按 R6 双号同步递增。
# 3.17.26（2026-11-09 动态睡眠状态注入与晚安漏发修复）：P1 注入处显式包含 sleep +
# 权威睡眠表现态（已睡优先/冲突降级/不伪造空闲/睡眠期不附待机预演）；P2 睡眠侧只读
# 晚安参考目标（高债窗口起点/引擎准备/bedtime）+ 问候期限跟随 + 目标更新复用漂移 +
# 突发入睡 2 分钟有界衔接；P3 晚安任务保存自身周期/night_id、跨周期推进旧任务、
# pending 持久化恢复（fired 不恢复防重靠 marks）。不新增用户可见配置字段
# （schema 不变，18 段 202 叶子），按 R6 双号同步递增。
SUPPORTED_CONFIG_VERSION = "3.17.26"

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

#: 3.2.0（2026-11-04 主动行为重写，用户直派规格）：日程巡检整条删除后退役的 [patrol] 键。
#: patrol_activity_enabled / activity_trigger_rate / default_speak_rate = 日程巡检开关与
#: 两类概率（触发路径已删，无新语义可搬运）；miss_speak_rate = 旧想念掷点概率（想念改
#: 状态驱动公式，概率为 companion.miss_trigger 内部常量）。四键纯 pop 丢弃并登记
#: （§9.7 口径：不 setdefault、不做值搬运）；入口两分支共经 + plugin.normalize raw pop。
LEGACY_PATROL_RETIRED_KEYS = {
    "patrol": (
        "patrol_activity_enabled",
        "activity_trigger_rate",
        "default_speak_rate",
        "miss_speak_rate",
    ),
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
    # 3.1.0（2026-11-04 配置精简轮 H 类）：temperature 锁定 0.9（resolve_config 恒返回
    # DEFAULT_TEMPERATURE，配置值本就不生效）——删隐藏 Field 消除死键，只 pop 不搬运。
    # max_events 兼容死键不在此登记：由 migrate_legacy_event_catalog 继续消费（搬进
    # 规范键 max_generated_events 后 pop，保留旧值语义）。
    # 3.17.6（PR-REMOVE-POOLCAP）：max_pool_events/enforce_pool_cap 退役——独立总
    # 配额移除，池上限派生自三时段配额和；旧键只 pop 不搬运（RELEASE_RETIRED 口径）。
    "event_catalog": ("max_tokens", "daily_token_budget", "temperature",
                      "max_pool_events", "enforce_pool_cap"),
    "judge": ("max_tokens",),
    # 3.1.0 H 类：tick_seconds / sleep_wake_check_seconds（=300 内部节拍常量）、
    # state_file（逐字 'life_state.json' 常量）——生产现值=默认已证实，只 pop 不搬运。
    "life_state": ("enabled", "sleep_engine_enabled", "profile_path",
                   "tick_seconds", "sleep_wake_check_seconds", "state_file"),
    # 2026-11-01 U 计划（§4.2）：入口不再搬进面板标题栏，「面板内麦麦尺寸」随之退役——
    # 详情展开以原入口为原点的粘连气泡替代（入口原位保留）。只 pop、不做值搬运。
    "maimai_interaction": ("detail_fish_size_px",),
    # 3.17.17（PR-RELIABILITY §1G）：attach_source 退役——视频说说确定性署名拼接
    # （视频：标题｜UP：…）不自然，文案改纯 LLM 自然分享（提示词禁元信息）。
    # 旧键只 pop 不搬运（RELEASE_RETIRED 口径），生产旧值自动清除。
    "qzone_video": ("attach_source",),
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
    """插件启用与基础设置。"""

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
        description="配置结构标记，由宿主管理。",
        json_schema_extra={
            "label": "布局版本",
            "hidden": True,
            **_ui_i18n("Layout version"),
        },
    )


class LlmSectionConfig(PluginConfigBase):
    """选择各项功能使用的宿主模型任务。"""

    __ui_label__ = "LLM 调用"
    __ui_icon__ = "cpu"
    __ui_order__ = 11
    prompt_optimization_enabled: bool = Field(
        default=True,
        description="短任务出现多余思考输出后，后续请求追加一次直接输出提示；不增加重试或延长超时。",
        json_schema_extra={"label": "自动约束短任务输出"},
    )
    over_limit_auto_trim: bool = Field(
        default=True,
        description="生成结果超出条数上限时自动裁剪兜底（默认开）：小事件取前N条避免整批作废；日程按时间排序后保留末尾，晚间计划不被裁掉。关闭则小事件超量整批拒绝、日程沿用旧的输出序截断。",
        json_schema_extra={"label": "超限自动裁剪",
                           **_ui_i18n("Auto-trim over-limit", "Gracefully trim over-limit generation results instead of failing; schedule keeps the tail (evening).")},
    )
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

    # 2026-11-04 配置精简轮（J 类）：judge_temperature 移入
    # mappings/internal_tuning.json[llm]（内部采样温度）；同名只读 property 见类尾。
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
        description="睡眠情绪裁判使用的宿主任务名；建议使用能稳定输出JSON正文的任务。",
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

    usage_retention_hours: int = Field(
        default=720,
        ge=24,
        le=720,
        description="LLM 用量账本保留期（小时）：默认 720 = 30 天硬上限，只清理更早的行",
        json_schema_extra={
            "label": "用量账本保留期（小时）",
            "hint": "默认 30 天；面板 24 小时 / 7 天 / 30 天三档都落在保留期内。调小后更早的记录会被清理",
            **_ui_i18n("Usage ledger retention (hours)"),
        },
    )
    usage_default_window_hours: int = Field(
        default=720,
        ge=24,
        le=720,
        description="模型消耗面板默认显示窗口（小时）：24 / 168 / 720，默认 720 = 30 天",
        json_schema_extra={
            "label": "用量默认窗口（小时）",
            "hint": "WebUI 打开时默认展示的窗口；页面上的 24 小时 / 7 天 / 30 天选择器仍可切换",
            **_ui_i18n("Default usage window (hours)"),
        },
    )

    # ── J 类内部调参（只读桥接 → mappings/internal_tuning.json[llm]）──────────
    @property
    def judge_temperature(self) -> float:
        """判断调用采样温度（越低结论越稳定）。"""
        return float(_tuning_value("llm", "judge_temperature"))


class LifeStateSuiteSectionConfig(LifeStateSectionConfig):
    """设置精力、饥饿、专注、压力、社交状态及睡眠恢复。"""

    __ui_order__ = 8


class AdminSectionConfig(PluginConfigBase):
    """设置命令权限、告状接收人和诊断日志。"""

    __ui_label__ = "管理与日志"
    __ui_icon__ = "shield"
    __ui_order__ = 13
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
        description="受骚扰时向配置对象转达情况；关闭后不提供该工具。",
        json_schema_extra={
            "label": "告状工具",
            "hint": "bot 被恶意刷屏/辱骂/攻击时把情况转达给维护者；关闭后不注册该 Tool",
            **_ui_i18n("Harass report"),
        },
    )
    harass_report_target_qq: str = Field(
        default="",
        description="告状接收人的QQ；留空时依次使用陪伴目标、宿主全局管理员；均不可用时不发送。",
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
    """调整常驻麦麦的尺寸、动作和详情展开效果。"""

    __ui_label__ = "麦麦互动"
    __ui_icon__ = "heart"
    __ui_order__ = 15
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
    preset: Literal["fish", "human"] = Field(
        default="human",
        description="fish=萨卡班麦麦；human=麦麦",
        json_schema_extra={"label": "形象预设", "hint": "fish=萨卡班麦麦；human=麦麦",
                           **_ui_i18n("Mascot preset", "fish = Sakabamai; human = Maimai.")},
    )
    mascot_size_px: int = Field(
        default=144, ge=48, le=192,
        description="形象基准尺寸（像素）；萨卡班麦麦与麦麦两个形象共用",
        json_schema_extra={"label": "形象尺寸（共用）", "step": 1, **_ui_i18n("Mascot size (px)")},
    )
    # 旧形象（萨卡班麦麦）具体参数已于 2026-11-03 撤出配置，按小主机当时配置现值
    # （angle 12 / hover_out 200/240ms / squash 1.2·0.5 / bounce 774ms 默认缓动 /
    #  detail 3px·220ms·0.6）固化为前端常量，见 webui/source/maimai.js。


class DashboardDisplaySectionConfig(PluginConfigBase):
    """设置历史明细的条数和变化显示阈值。"""
    __ui_label__ = "数据展示"
    __ui_icon__ = "chart-line"
    __ui_order__ = 14

    history_changes_limit: int = Field(
        default=30, ge=5, le=30,
        description="详情「最近变化」最大条数（5–30）",
        json_schema_extra={"label": "最近变化条数", "step": 1, **_ui_i18n("Recent changes limit")},
    )
    # 2026-11-04 配置精简轮（J 类）：history_state_epsilon / history_debt_epsilon 移入
    # mappings/internal_tuning.json[dashboard_display]（内部记录阈值；本段生产现值未证实，
    # 走值搬运不硬编码）。面板投影 _maimai_view 经下方同名 property 读到 JSON 生效值，
    # 前端 fish.js 的 pick 兜底口径不变。

    @property
    def history_state_epsilon(self) -> float:
        """状态变化记录阈值（占比）。"""
        return float(_tuning_value("dashboard_display", "history_state_epsilon"))

    @property
    def history_debt_epsilon(self) -> float:
        """睡眠债变化记录阈值（小时）。"""
        return float(_tuning_value("dashboard_display", "history_debt_epsilon"))


class CateyeSuiteConfig(PluginConfigBase):
    """插件完整配置。

    注意：子分组必须扁平挂为顶层字段（嵌套段 WebUI 不渲染）。
    各 section 类定义在各子模块的 config.py / suite_prompts.py 中（模块化维护）。
    """

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    patrol: PatrolSectionConfig = Field(default_factory=PatrolSectionConfig)
    screen: ScreenSectionConfig = Field(default_factory=ScreenSectionConfig)
    affection: AffectionSectionConfig = Field(default_factory=AffectionSectionConfig)
    schedule: ScheduleSectionConfig = Field(default_factory=ScheduleSectionConfig)
    schedule_advanced: ScheduleAdvancedSectionConfig = Field(default_factory=ScheduleAdvancedSectionConfig)
    planning_proactive: PlanningProactiveSectionConfig = Field(default_factory=PlanningProactiveSectionConfig)
    emotion: EmotionSectionConfig = Field(default_factory=EmotionSectionConfig)
    life_state: LifeStateSuiteSectionConfig = Field(default_factory=LifeStateSuiteSectionConfig)
    qzone: QzoneSectionConfig = Field(default_factory=QzoneSectionConfig)
    qzone_video: QzoneVideoSectionConfig = Field(default_factory=QzoneVideoSectionConfig)
    video_watch: VideoWatchSectionConfig = Field(default_factory=VideoWatchSectionConfig)
    video_share: VideoShareSectionConfig = Field(default_factory=VideoShareSectionConfig)
    llm: LlmSectionConfig = Field(default_factory=LlmSectionConfig)
    prompts: PromptsSectionConfig = Field(default_factory=PromptsSectionConfig)
    admin: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    dashboard_display: DashboardDisplaySectionConfig = Field(default_factory=DashboardDisplaySectionConfig)
    maimai_interaction: MaimaiInteractionSectionConfig = Field(default_factory=MaimaiInteractionSectionConfig)

    @property
    def event_catalog(self) -> EventCatalogSectionConfig:
        """Typed service view; only schedule.event_catalog_* is serialized."""
        from core.config_migration import EVENT_CATALOG_FIELDS
        return EventCatalogSectionConfig.model_validate({
            key: getattr(self.schedule, "event_catalog_" + key) for key in EVENT_CATALOG_FIELDS})

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
        migrated = migrate_screen_config(migrate_display_config(migrated))
        # 0-t) 3.1.0 配置精简轮 J 类：40 个内部调参旧键在此显式 pop（两分支共经）。
        #      捕获值的落盘由 plugin.normalize_plugin_config 在 SDK merge 之前完成
        #      （core.internal_tuning.merge_captured，只补用户表缺失键）；本处 pop 保证
        #      离线 model_validate 路径也不会把已删键写回 TOML。须在 migrate_display_config
        #      之后（旧 [maimai_interaction] 的 history_*_epsilon 先搬进本段再被摘出）。
        migrated, _tuning_captured = migrate_internal_tuning(migrated)
        # Preserve canonical explicit defaults before the ancient layout cleaner
        # strips default-valued keys; legacy aliases must already be gone then.
        migrated = migrate_release_config(migrated)
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
        #    3.2.0 主动行为重写：日程巡检三键 + miss_speak_rate 退役（纯 pop，两分支共经；
        #    生产存量配置已带 layout 标记，必须在 already_migrated 分支之前清理）。
        for section_name, stale_keys in LEGACY_PATROL_RETIRED_KEYS.items():
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
            return migrate_release_config(migrated)

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

        # 3b) [patrol] 的 miss_reason_prompt（1.0.0）→ [prompts].companion_miss_reason
        #     （**仅非出厂值才搬**，与 [cateye] 提示词同口径；T3 问题表 #3 用户裁决 A：
        #     不搬会静默回退出厂文案）。
        #     miss_confirm_prompt 只 pop 清退、不再搬运（2026-11-04 评估 F4）：
        #     companion_miss_confirm 属 INTERNAL_PROMPT_NAMES（数值把关协议，prompt()
        #     恒返回出厂默认，_ignore_internal_overrides 会滤掉该键）——旧分支把它搬进
        #     [prompts] 等于没搬，与内部化裁定自相矛盾，故删除该无效迁移分支。
        patrol_data.pop("miss_confirm_prompt", None)
        legacy_text = patrol_data.pop("miss_reason_prompt", None)
        if (isinstance(legacy_text, str) and legacy_text.strip()
                and legacy_text not in (_LEGACY_MISS_REASON_PROMPT_DEFAULT,
                                        COMPANION_MISS_REASON_DEFAULT)):
            prompts_data.setdefault("companion_miss_reason", legacy_text)

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
        # 3.1.0 J 类二次捕获：上面的古老段搬迁可能把值搬进 J 类新键
        # （[cateye].context_ttl_minutes → patrol.screen_context_ttl_minutes、
        # [judge].temperature → llm.judge_temperature、[inject]/[planning_cleanup]
        # 的缓存键 → schedule_advanced）——入口 pop 发生在搬迁之前，这里再摘一次。
        # 校验器保持无 IO：捕获值入队，由 plugin.normalize_plugin_config 在校验成功
        # 后统一落盘（只补用户表缺失键）；纯离线 model_validate 不写任何文件。
        migrated = migrate_screen_config(migrated)
        migrated, _late_tuning_captured = migrate_internal_tuning(migrated)
        if _late_tuning_captured:
            from core.internal_tuning import stage_captured
            stage_captured(_late_tuning_captured)
        return migrate_release_config(migrated)
