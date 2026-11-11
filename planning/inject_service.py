"""日程注入业务实现（来源：自主规划插件 services/inject_service.py，移植版）。

Hook 入口：``maisaka.planner.before_request`` / ``maisaka.replyer.before_request``。

业务子模块（IntentClassifier / InjectOptimizer / ConversationContextCache）位于
``inject/`` 子包；活动状态短语（原 ActivityStateAnalyzer）已删除，精神状态行改读
情绪系统的 ``mood_text()``（M4，§4.2 行 8，两处「精神状态」同源）。

注入协议（照抄源插件）：
- 向 items 注入 SystemMessageItem 快照：meta 三件套（item_id=uuid4().hex、
  logical_turn_id=None（或继承首条）、timestamp=ISO）；
- Hook 返回 ``{"action": "continue"}`` 或
  ``{"action": "continue", "modified_kwargs": {**kwargs, <payload_key>: new, ...}}``
  全量回传（宿主对 modified_kwargs 整体替换，单键返回会丢字段）；
- replyer 注入协作式：保留上游 extra_prompt，自身文本追加在后方。

对整合契约的适配：
- ``inject_schedule`` 配置名对齐为 ``inject_into_planner``；
- 白名单用 ``schedule.inject_scope``（**默认 all**：所有会话都注入日程；
  ``target_private`` = 只给目标陪伴私聊注入）；admin 段若挂载了 allowed_streams 则叠加为额外限制；
- **日期/节假日头部**（2026-10-10 由陪伴模块迁入）：``schedule.holiday_inject_enabled`` 开启时，
  日程块头部先注一行「今天是 X（星期Y），节假日状态」，模板见 ``[prompts].inject_date_header``；
- 注入文案（首行/当前行/未来行/引导行/replyer 状态块）全部读 ``[prompts]`` 段（suite_prompts）；
- 数据查询经 GoalManager 合成视图（schedule_items 单一数据源）。

R7 滚动化改造（2026-10-25 用户直派，设计目录
D:/deepseek-harness_workspace/maibot-plugins/2026-10-25-日程系统滚动化改造设计，主设计 §3.3/§6.6/§9.1）：

- runtime 视图就绪时（``LifeScheduleReadModel.enabled`` = ``[schedule].enabled``，
  PR8 3.0.0 起唯一总开关）注入块追加 runtime 实况（经
  :class:`~planning.life_schedule.LifeScheduleReadModel`）：current（当前 episode
  名称/已进行多久/进度/attention_mode/剩余分钟）+ next ≤2 条 + 状态行
  （``LifeStateService.describe`` 最显著 1~2 项）+ recent 1~2 条（completed_today
  尾部实际发生，用于连续性）；当前活动改读 episode（红线 3：不从时间窗编造），
  provider 关闭/缺失/异常一律旁路；
- replyer「精神」静态能量行改优先 LifeState，缺失/关闭回退既有
  ``describe_energy(get_energy_level(...))``（旧函数不删，
  ``planning/time_utils._ENERGY_TABLE`` 链保留为 fallback——§6.6 口径）；
- PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-6）：
  ``_get_current_schedule`` 快照链改读 runtime episode/ReadModel 视图（与注入同
  口径），原 GoalManager 今日/昨日合成时间窗 + 15 分钟缓存编造路径删除；运行态
  未就绪一律如实（不读残留快照冒充当前活动）。

2026-11-04 双通道意图门控 + 实况瘦身（根因取证：union/20261104_主动行为与视频排查/
计划提及根因排查.md——群日程提及 = 注入 priming，planner 实况块与 replyer
next_line 是两条已证实泄漏通道）：

- 目标陪伴私聊（``session_id == plugin._stream_id``）保留全量注入（投诉集中在群）；
- 其余会话（群聊/非目标私聊/会话缺失）实况内容按当前用户消息意图门控：
  问当前/问未来（query_current/query_future，IntentClassifier 调优路径）→ 全量
  实况（当前活动+接下来+状态+刚完成）；闲聊/未知 → 瘦身仅「当前活动实况」一行
  （不含接下来/状态/刚完成），replyer 状态块不携带 next_line（接下来 X 要 Y）。
  分类不确定（空消息 UNKNOWN/无匹配闲聊兜底）天然落在保守侧——宁可少说不漏说；
- 门控只决定「实况给多少」，不改变注入时机决策（optimizer/上下文缓存照旧）；
  与 23:50 批次的空活动兜底（现在行，非 life_lines）及优化器规则2移除正交，
  兜底照常送达闲聊，防编造成果不受影响。非目标私聊同走门控的理由：replyer
  next_line priming 机制群/私聊同效，且真实询问在任何会话都能解锁全量实况，
  问日程的回答能力不丢失。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from core.time_utils import minutes_of_now

from .config import ScheduleAdvancedSectionConfig, ScheduleSectionConfig, section_or_default
from .goal_manager import get_goal_manager
from .inject import (
    ConversationContextCache,
    InjectOptimizer,
    IntentClassifier,
    UserIntent,
)
from .time_utils import (TimezoneManager, describe_energy, format_day_minute,
                         get_energy_level, parse_time_window, ring_minutes_ahead)
from suite_prompts import (
    IMPRESSION_FOLDED_MARK_DEFAULT,
    IMPRESSION_TITLE_ACTIVE_DEFAULT,
    IMPRESSION_TITLE_FOLDED_DEFAULT,
    INJECT_DATE_HEADER_DEFAULT,
    INJECT_HINT_CONTINUE_DEFAULT,
    INJECT_HINT_CURRENT_SUBCONSCIOUS_DEFAULT,
    INJECT_REPLYER_NEXT_LINE_DEFAULT,
    INJECT_REPLYER_STATE_LINE_DEFAULT,
    INJECT_REPLYER_STATUS_DEFAULT,
    INJECT_REPLYER_STATUS_SUBCONSCIOUS_DEFAULT,
    INJECT_SCHEDULE_CURRENT_LINE_DEFAULT,
    INJECT_SCHEDULE_FUTURE_LINE_DEFAULT,
    SUBCONSCIOUS_STATE_LINE_NOTE_DEFAULT,
    SUBCONSCIOUS_TONE_HAPPY_DEFAULT,
    SUBCONSCIOUS_TONE_HUNGRY_DEFAULT,
    SUBCONSCIOUS_TONE_STEADY_DEFAULT,
    SUBCONSCIOUS_TONE_TIRED_DEFAULT,
    prompt,
    render,
)

logger = logging.getLogger(__name__)

#: 复核 P1-10：attention_mode（now/brief/later）→ 自然中文口语（未知值不注入）。
_ATTENTION_MODE_TEXT = {
    "now": "现在能随时搭话",
    "brief": "可以简短聊两句",
    "later": "这时不太方便分心",
}

#: 复核 P1-9：主动分享回合的运行时豁免——只追加在本次注入文本末尾，不新增
#: ``[prompts]`` 可配键（schema 18 段 202 叶子不变）。
_INJECT_PROACTIVE_SHARE_HINT = (
    "（本轮是你主动分享近况/视频的回合：可以自然说出要分享的内容；"
    "上面『不主动提日程 / 刚完成的事』只约束背景信息，不限制本轮分享动机。）"
)
_INJECT_PROACTIVE_SHARE_REPLYER = (
    "（本轮是你主动开口的回合：可以自然说出本轮想分享/想说的内容；"
    "上面的『不要主动提及』只约束日程背景，不限制本轮主动动机。）"
)
_PROACTIVE_SHARE_KINDS = frozenset({"video_share", "surf_chitchat"})
_EXPERIENCE_SHARE_MARKER = "最近经历参考（可选话题）"


#: 无当前活动兜底文案统一经 ``suite_prompts.prompt(plugin, "inject_no_activity")``：
#: 该键属 INTERNAL_PROMPT_NAMES（不在 PromptsSectionConfig 字段面），恒返
#: suite_prompts.INJECT_NO_ACTIVITY_DEFAULT，不接受配置覆盖、不会为空。
#: （2026-11-04 评估问题8：原本地死常量 _INJECT_NO_ACTIVITY_DEFAULT 全文件零引用
#: 且文案与实际默认不同，已删除。）

#: replyer 复用本轮 planner 意图的最大年龄（秒，N6-1）。
#: planner → replyer 同一轮内相隔仅数秒；超过该年龄说明不是同一轮，回落旧口径（闲聊）。
_REPLYER_INTENT_REUSE_SECONDS = 180

#: 结构化提取（2026-11-11 Gemini 批 §2.2 P0-1 残留）：消息项可能携带
#: "这是 Bot 自己说的话"标志的宿主字段（快照之外的兼容面）。任一为真 →
#: 该条不是真实用户输入，直接跳过（优先于旧正则滤镜）。
_STRUCTURED_SELF_FLAG_KEYS = (
    "is_self_message", "is_self", "is_bot", "sender_is_self",
)
#: 结构化来源字段里代表 Bot 自身/引导回复的取值。
_STRUCTURED_SELF_SOURCES = frozenset({"guided_reply", "bot", "self", "assistant"})
#: 宿主系统级 task 标签：整条是插件/宿主任务块，不是真实用户文本。
_STRUCTURED_SYSTEM_TASK_PATTERN = re.compile(
    r"^\s*<(?:plugin_proactive_task|system[_-]?task|task)\b", re.IGNORECASE)
#: `<message ...>` 前缀属性解析（is_self_message 等宿主元数据）。
_MESSAGE_TAG_PATTERN = re.compile(r"^\s*<message\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_MESSAGE_ATTR_PATTERN = re.compile(
    r"(?P<key>[A-Za-z_][\w-]*)\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
    re.DOTALL,
)

#: 日结印象行（companion 记忆行；只处理 system 文本，避免误改用户原话）。
_IMPRESSION_LINE_PATTERN = re.compile(
    r"^(?P<indent>[ \t]*)" + re.escape(IMPRESSION_TITLE_ACTIVE_DEFAULT)
    + r"(?P<body>.*)$")
_IMPRESSION_DATE_PATTERN = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})\b")
#: 条目分隔：只在「；」后面紧跟日期时切分，保留条目正文内部的自然分号。
_IMPRESSION_ENTRY_SPLIT_PATTERN = re.compile(r"[；;]\s*(?=\d{4}-\d{2}-\d{2}\b)")
#: 消极印象关键词（保守集：只认冲突/冷落/失望等明确负向，不把「累/忙」当消极）。
_IMPRESSION_NEGATIVE_MARKERS = (
    "没有互动", "没互动", "没有交流", "没交流", "没有联系", "没联系",
    "冷淡", "冷落", "忽视", "忽略", "冷战", "疏远", "不理",
    "争吵", "吵架", "争执", "闹别扭", "别扭", "敷衍",
    "失望", "难过", "伤心", "生气", "不开心", "委屈", "沉默",
)


class InjectService:
    """日程注入服务。"""

    # 主程序 maisaka 会在 messages 末尾追加多条独立 user 消息作为元数据上下文，
    # 这些消息**不是真实用户问题**，不应进入 IntentClassifier，否则会污染意图判定。
    # 2026-10-10 N6-3 增补：宿主尾部指令（planner 的「时间：…」「你需要输出对…的分析」、
    # replyer 的「当前时间：…」「请简短的回复…」）与内部参考块同样要跳过，否则分类
    # 永远落在最后一条指令上（真机 12/12 判 casual_chat/0.40）。
    _METADATA_USER_MESSAGE_PATTERNS: List[re.Pattern] = [
        # replyer 通道的合成尾消息（``当前时间：…`` 开头，其后再挂目标消息/篇幅要求）
        re.compile(r"^当前时间[：:]\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"),
        # planner 通道尾部追加的「时间：2026-10-10 16:18:48」（整条即时间戳）
        re.compile(r"^时间[：:]\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s*$"),
        re.compile(r"^【人物画像-内部参考】"),
        re.compile(r"^【启发式记忆-内部参考】"),
        re.compile(r"^<system-reminder>"),
        # replyer 篇幅要求（宿主原文：请简短的回复，允许句子残缺…）
        re.compile(r"^请简短的回复[，,]"),
        # planner 尾部的最终提醒（宿主模板：你需要输出对{bot_name}发言的分析…）
        re.compile(r"^你需要输出对\S*发言的分析"),
        # 复核 P0-1：Maisaka 宿主现行尾指令（chat_loop_service.py 三种模式）——
        # 普通 maisaka_chat 模式恒用「请你进行分析…」，passive 模式用「需要信息
        # 或操作时调用工具…」；旧正则只覆盖 focus 模式，致意图分类 100% 误判。
        re.compile(r"^请你进行分析[，,]"),
        re.compile(r"^需要信息或操作时调用工具"),
        # replyer 通道尾指令（cateye_better_post-processing 默认改写文案）
        re.compile(r"^像平常在群里接一句话"),
        # 宿主主动任务块与上下文摘要（插件指令，不是用户发言）
        re.compile(r"^<plugin_proactive_task\b"),
        re.compile(r"^【上下文状态摘要-内部参考】"),
    ]

    #: 复核 P0-1：Bot 自身历史发言也被宿主包装为 UserMessageItem，前缀带
    #: is_self_message="true"（planner_messages.py 渲染）——提取用户文本时跳过。
    _SELF_MESSAGE_PREFIX_PATTERN: re.Pattern = re.compile(
        r"^<message\s+[^>]*is_self_message=[\"']true[\"']")

    # 主程序 build_planner_prefix 会给 chat history 里每条 user 消息加
    # ``<message msg_id="..." time="..." user="...">\n真实文本`` 前缀。
    _PLANNER_PREFIX_PATTERN: re.Pattern = re.compile(r"^<message\s+[^>]*>\s*\n?")

    def __init__(self, plugin: Any, holiday_service: Any = None) -> None:
        """初始化 InjectService，加载智能注入组件、缓存、配置。

        Args:
            plugin: 当前插件实例（动态读 plugin.config，热重载自动生效）
            holiday_service: 节假日服务（planning.HolidayService；None = 不注入日期头部）
        """
        self._plugin = plugin
        self._holiday = holiday_service

        # 时区 / 注入调优（2026-10-10 起全部在 [schedule_advanced] 段）。
        # PR8 3.0.0：原 15 分钟日程 LRU 缓存（``_schedule_cache``）随时间窗编造路径
        # 一并删除——runtime 视图读取轻量，无需缓存。
        adv = section_or_default(plugin, "schedule_advanced", ScheduleAdvancedSectionConfig)
        self._tz_manager: TimezoneManager = TimezoneManager(adv.timezone)

        self._intent_classifier = IntentClassifier(tuning_enabled=True)
        self._inject_optimizer = InjectOptimizer(
            cache_ttl=adv.cache_ttl,
            casual_inject_probability=adv.casual_chat_inject_probability,
        )
        self._context_cache = ConversationContextCache(
            max_turns=adv.context_max_turns,
            ttl=adv.context_ttl,
        )
        logger.debug(
            "InjectService 初始化完成（cache_max=%d, cache_ttl=%d, intent=%s, optimizer=%s）",
            adv.cache_max_size, adv.cache_ttl,
            True, True,
        )

    # ------------------------------------------------------------
    # 配置与会话准入
    # ------------------------------------------------------------

    def _schedule_cfg(self) -> ScheduleSectionConfig:
        return section_or_default(self._plugin, "schedule", ScheduleSectionConfig)

    def _plugin_enabled(self) -> bool:
        """插件总开关 + 日程模块独立总开关（config.schedule.enabled）任一关闭即不注入。"""
        plugin_section = getattr(self._plugin.config, "plugin", None)
        if not bool(getattr(plugin_section, "enabled", True)):
            return False
        return bool(getattr(self._schedule_cfg(), "enabled", True))

    def _is_session_allowed(self, session_id: str) -> bool:
        """会话准入：插件启用 + inject_scope（target_private=仅目标私聊）+ 叠加白名单。"""
        if not self._plugin_enabled():
            return False
        # admin 段若挂载了 allowed_streams 则叠加限制（留空 = 不限制）
        admin = getattr(self._plugin.config, "admin", None)
        allowed = list(getattr(admin, "allowed_streams", None) or []) if admin else []
        if allowed:
            from .stream_filter import is_stream_allowed
            if not is_stream_allowed(session_id, allowed):
                return False
        return True

    def _is_target_private_session(self, session_id: str) -> bool:
        """目标陪伴私聊判定（2026-11-04 群/私聊差异门控）。

        目标流 = ``plugin._stream_id``（plugin 解析 target_qq 得到，随热重载更新，
        与 ``inject_scope=target_private`` 准入同源）。仅目标陪伴私聊保留全量实况
        注入；其余会话（群聊/非目标私聊/session 缺失）一律走意图门控 + 实况瘦身。
        ``inject_scope`` 语义不变：``target_private`` 时只有目标私聊能进注入，
        且其恰为全量口径；``all`` 时目标私聊全量、其余门控。
        """
        target_stream = str(getattr(self._plugin, "_stream_id", "") or "")
        sid = str(session_id or "")
        return bool(target_stream) and sid == target_stream

    @staticmethod
    def _is_schedule_query_intent(intent: "UserIntent") -> bool:
        """实况门控解锁判定：仅「问当前 / 问未来」算日程询问意图。

        闲聊/未知/技术/命令一律走瘦身侧；分类器的不确定兜底（空消息
        UNKNOWN/0.0、无匹配闲聊/0.40）天然落在保守侧——宁可少说不漏说。
        """
        return intent in (UserIntent.QUERY_CURRENT, UserIntent.QUERY_FUTURE)

    async def _date_header(self) -> str:
        """日期/节假日头部（2026-10-10 由陪伴模块迁入日程注入）。

        只在 ``[schedule].holiday_inject_enabled`` 开启时输出；查不到节假日信息时按
        「工作日/周末」兜底（HolidayService 内部已有本地判断，这里再兜一层异常）。
        模板可经 ``[prompts].inject_date_header`` 定制。
        """
        cfg = self._schedule_cfg()
        if not bool(getattr(cfg, "holiday_inject_enabled", True)):
            return ""
        now = self._tz_manager.get_now()
        date_str = now.strftime("%Y-%m-%d")
        weekday = "一二三四五六日"[now.weekday()]
        holiday_info = "周末" if now.weekday() >= 5 else "工作日"
        if self._holiday is not None:
            try:
                holiday_info = await self._holiday.get_holiday_info(date_str)
            except Exception as exc:
                logger.debug("查询节假日失败（按星期兜底）：%s", exc)
        template = prompt(self._plugin, "inject_date_header")
        return render(
            template,
            fallback=INJECT_DATE_HEADER_DEFAULT,
            date=date_str,
            weekday=weekday,
            holiday=holiday_info,
        )

    # ------------------------------------------------------------
    # 后台缓存预热
    # ------------------------------------------------------------

    async def preheat_cache(self) -> None:
        """启动后预热（异步任务；失败静默）。

        PR8 3.0.0：原 15 分钟日程缓存已随时间窗编造路径删除，本方法保留为一次
        runtime 视图预热读取（顺带在启动期暴露取数异常便于排查）。
        """
        try:
            await asyncio.sleep(5)  # 等待系统初始化
            logger.debug("开始预热日程缓存...")
            await self._get_current_schedule("global")
            logger.debug("日程缓存预热完成")
        except Exception as exc:
            logger.warning(f"缓存预热失败: {exc}")

    # ------------------------------------------------------------
    # Hook 主入口（planner / replyer 两个）
    # ------------------------------------------------------------

    async def inject_into_replyer_extra_prompt(
        self,
        session_id: str = "",
        attempt: int = 1,
        extra_prompt: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.replyer.before_request`` Hook 入口。

        把当前活动作为 extra_prompt 协作式注入（保留上游内容，自身追加在后方），
        走 InjectOptimizer 冷却——**本通道独立桶键**（``{session_id}:replyer``）：
        修复前与 planner 共桶 + 意图写死 CASUAL_CHAT，replyer 每次注入都给自己
        盖上 300 秒「同活动同意图」冷却章（真机 2/8 命中），见 2026-10-10 N6。
        无当前活动时走「空活动变体」兜底文案，不再静默早退（N1-2）。
        """
        cfg = self._schedule_cfg()
        if not self._plugin_enabled() or not cfg.inject_into_replyer:
            return {"action": "continue"}

        # 重试不重复注入
        if attempt and int(attempt) > 1:
            return {"action": "continue"}

        # 会话准入（inject_scope / 白名单）
        if not self._is_session_allowed(session_id):
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"
            user_id = session_id or "unknown"
            # 复核 P1-9：主动分享轮判定（登记簿未消费 → kind/reason；已消费 →
            # 载荷里的分享定制文案/经历话题标记）。
            _payload = kwargs.get("items")
            if not isinstance(_payload, list):
                _payload = kwargs.get("messages")
            proactive_share = self._proactive_share_turn(
                session_id, _payload if isinstance(_payload, list) else None)

            current_activity, current_description, future_activities, activity_type = (
                await self._get_current_schedule(chat_id)
            )

            # N6-1：不再写死闲聊意图——优先复用本轮 planner 已判定的真实意图
            # （拿不到时才回落 CASUAL_CHAT/0.5，与修复前逐字一致）。
            intent, confidence = self._resolve_replyer_intent(user_id, kwargs)

            # 2026-11-04 双通道门控（replyer 通道）：非目标私聊且非询问意图 →
            # 状态块不携带「接下来 X 要 Y」（排查 E4/E5 逐字泄漏口：该行位于生成前
            # 倒数第 2 条消息，priming 最直接）；当前活动 state_line 保留以维持角色
            # 连续性。目标陪伴私聊保持旧口径（全量）。意图取不到时回落闲聊 → 保守
            # 不携带接下来条目。
            allow_next_line = (
                self._is_target_private_session(session_id)
                or self._is_schedule_query_intent(intent)
            )
            if not allow_next_line:
                logger.debug(
                    "replyer 实况门控: intent=%s 非询问，next_line 不携带", intent.value,
                )

            # N6-2：optimizer 记录键按通道命名空间化，replyer 与 planner 互不占冷却。
            # 无当前活动统一用 ""（而不是 None）参与冷却比较，保证空活动轮次也能自节流。
            scope_key = f"{user_id}:replyer"
            activity_key = current_activity or ""

            # 复用 InjectOptimizer 冷却（本通道独立桶）
            if self._inject_optimizer is not None:
                should_inject, skip_reason = self._inject_optimizer.should_inject(
                    scope_key, intent, activity_key, confidence=confidence,
                )
                if not should_inject:
                    logger.debug(f"replyer 注入被冷却跳过: {skip_reason}")
                    return {"action": "continue"}

            # R7 runtime 覆盖（runtime 视图就绪时）：当前活动改读 episode、
            # 未来活动改读 runtime next、剩余分钟读 episode 端点；未就绪 → None
            # = 空活动口径（覆盖值只在本方法局部生效，不进缓存）。
            life_view = self._life_runtime_view()
            life_remaining: Optional[int] = None
            if life_view is not None:
                life_ov = self._life_override_from_view(life_view)
                current_activity = life_ov["name"]
                current_description = life_ov["description"]
                activity_type = life_ov["goal_type"]
                if life_ov["future"]:
                    future_activities = life_ov["future"]
                life_remaining = life_ov["remaining"]

            if current_activity:
                # 一次查询拿到剩余分钟（状态情绪短语已随 state_analyzer 删除，精神状态归情绪系统）
                remaining_minutes = life_remaining
                if remaining_minutes is None:
                    remaining_minutes = self._compute_activity_aux(current_activity, activity_type)
                activity_prompt = self._build_replyer_extra_prompt(
                    current_activity=current_activity,
                    description=current_description or "",
                    future_activities=future_activities if allow_next_line else [],
                    remaining_minutes=remaining_minutes,
                )
                logger.info(f"replyer 注入: {current_activity}")
            else:
                # N1-2：无当前活动不静默早退——空活动变体（兜底文案见 [prompts].inject_no_activity）
                # 门控同口径：非询问意图不携带接下来条目（兜底行本身不受门控影响）
                activity_prompt = self._build_replyer_no_activity_prompt(
                    future_activities if allow_next_line else [])
                if not activity_prompt:
                    # 防御守卫：内部键 inject_no_activity 恒返非空默认，本分支正常
                    # 不可达（2026-11-04 评估问题8：原「文案被显式关空」口径已订正）
                    return {"action": "continue"}
                logger.info("replyer 注入: 无当前活动（空活动变体）")

            if proactive_share:
                # 复核 P1-9：分享/主动开口轮给「不要主动提及」一句运行时豁免，
                # 避免状态块与 replyer 改写文案（想念/视频分享）互相抵消。
                activity_prompt = f"{activity_prompt}\n{_INJECT_PROACTIVE_SHARE_REPLYER}"

            if self._inject_optimizer is not None:
                self._inject_optimizer.record_injection(
                    scope_key, activity_key, activity_prompt, intent,
                )

            # 协作式追加——保留上游 extra_prompt，自身文本追加在后方
            upstream_prompt = str(extra_prompt or "").strip()
            merged_prompt = f"{upstream_prompt}\n\n{activity_prompt}" if upstream_prompt else activity_prompt

            return {
                "action": "continue",
                "modified_kwargs": {
                    **kwargs,
                    "session_id": session_id,
                    "attempt": attempt,
                    "extra_prompt": merged_prompt,
                },
            }

        except Exception as exc:
            logger.error(f"replyer 注入失败: {exc}", exc_info=True)
            return {"action": "continue"}

    async def inject_into_planner_messages(
        self,
        items: Optional[List[Dict[str, Any]]] = None,
        messages: Optional[List[Dict[str, Any]]] = None,
        item_schema_version: Any = None,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """``maisaka.planner.before_request`` Hook 入口。

        按当前日程信息构造 system 条目插入消息列表。兼容两种宿主投影：
        ContextItem 快照（``items``，MaiBot 1.2.x+）与旧 role/content（``messages``）；
        其余 kwargs（tool_definitions 等）全量回传。
        """
        # 兼容新旧 payload 字段：优先 items，回退 messages
        if items is not None and isinstance(items, list):
            payload_key, payload = "items", items
        elif messages is not None and isinstance(messages, list):
            payload_key, payload = "messages", messages
        else:
            return {"action": "continue"}

        if not payload:
            return {"action": "continue"}

        # 复核 P1-9：主动分享轮（miss+经历话题 / 视频分享 / 刷QQ闲聊）——
        # 「不主动提日程」的引导行会与分享动机互搏，本轮给 planner 追加豁免。
        proactive_share = self._proactive_share_turn(session_id, payload)

        cfg = self._schedule_cfg()
        if not self._plugin_enabled() or not cfg.inject_into_planner:
            return {"action": "continue"}

        # 会话准入（inject_scope / 白名单）
        if not self._is_session_allowed(session_id):
            logger.debug(f"会话 {session_id} 不在注入范围，跳过注入")
            return {"action": "continue"}

        try:
            chat_id = session_id or "global"

            # 提取最后一条 user 消息用于意图分析
            user_message = self._extract_last_user_text(payload)

            # 4.4b（2026-11-11 Gemini 批）：次日发生真实互动后，旧日消极印象在
            # prompt 中折叠为 [已翻篇] 并移到正文末尾；本轮无真实用户互动
            # （主动开口/纯元数据载荷/仅历史消息）→ 原样。只改 system 文本，
            # 不动状态数据。
            payload_before_fold = payload
            try:
                today = self._tz_manager.get_now().strftime("%Y-%m-%d")
                interacted = self._has_real_interaction_today(payload, today=today)
                payload = self._fold_stale_impressions_in_payload(
                    payload, interacted=interacted)
            except Exception as exc:
                payload = payload_before_fold
                logger.debug("日结印象折叠旁路（保持原样）：%s", exc)

            user_id = session_id or "unknown"
            if not session_id:
                logger.debug(
                    "planner 注入未拿到 session_id：冷却/上下文缓存键退化为 %s（钩子链应已写回 session_id）",
                    user_id,
                )

            # 检查对话上下文：判断是否在连续讨论日程话题
            # （should_continue_inject 的规则 2「活动变化」需要 current_activity，
            # 但该判据自 R0 基线起从未在此路径生效——首调传 None 时恒 False，
            # 且取到活动后的二次调用与首调同 tick 必然返回恒等结果、只多打一条
            # debug 日志（真机 2320 对逐字重复的根因，2026-10-20 取证），故只调一次）
            context_continue_inject = False
            context_reason: Optional[str] = None
            if self._context_cache is not None:
                context_continue_inject, context_reason = self._context_cache.should_continue_inject(
                    user_id, None,
                )

            # 获取当前日程
            current_activity, current_description, all_future_activities, activity_type = (
                await self._get_current_schedule(chat_id)
            )

            # R7 runtime 覆盖与实况块（runtime 视图就绪时；未就绪 → 全部旁路）：
            # 当前活动/未来活动改读 episode 口径，另把
            # 「活动实况（已进行/进度/注意力/剩余）+ next ≤2 + 状态行 + recent ≤2」
            # 作为追加行注入（主设计 §3.3 注入只读清单）。
            life_view = self._life_runtime_view()
            life_lines: List[str] = []
            remaining_override: Optional[int] = None
            # 2026-11-04 双通道门控（planner 通道）：非目标私聊预备瘦身实况变体
            # （仅「当前活动实况」一行），意图分类后由 _build_inject_text 按
            # 「问当前/问未来 → 全量；闲聊/未知 → 瘦身」选用。目标私聊不门控。
            live_gate_enabled = not self._is_target_private_session(session_id)
            life_lines_slim: Optional[List[str]] = None
            if life_view is not None:
                life_ov = self._life_override_from_view(life_view)
                current_activity = life_ov["name"]
                current_description = life_ov["description"]
                activity_type = life_ov["goal_type"]
                if life_ov["future"]:
                    all_future_activities = life_ov["future"]
                remaining_override = life_ov["remaining"]
                life_lines = self._life_block_lines(
                    life_view, life_ov, subconscious=self._state_subconscious_style())
                if live_gate_enabled:
                    life_lines_slim = self._life_block_lines(
                        life_view, life_ov, slim=True,
                        subconscious=self._state_subconscious_style())

            # 无当前活动不再静默早退（N1-2）：照常走「分类 → 决策 → 模板路由」，
            # 模板侧自动切到空活动变体（兜底文案 = suite_prompts 内部键 inject_no_activity）。
            # 决策仍由 optimizer 把关——2026-11-03 规则2移除后空活动对闲聊亦放行，
            # 仅按闲聊概率（默认 50%）与冷却节流，不会变成空档期刷屏。
            inject_content, injected, detected_intent = self._build_inject_text(
                user_message=user_message,
                current_activity=current_activity,
                current_description=current_description,
                future_activities=all_future_activities,
                activity_type=activity_type,
                context_continue_inject=context_continue_inject,
                context_reason=context_reason,
                user_id=user_id,
                remaining_override=remaining_override,
                life_lines=life_lines,
                life_lines_slim=life_lines_slim,
                live_gate_enabled=live_gate_enabled,
                proactive_share=proactive_share,
            )

            # 记录到上下文缓存
            if self._context_cache is not None:
                self._context_cache.add_turn(
                    user_id=user_id,
                    user_message=user_message,
                    intent=detected_intent,
                    injected=injected,
                    activity=current_activity,
                )

            if not inject_content:
                if payload is not payload_before_fold:
                    # 4.4b：本轮不注入日程，但旧日消极印象的折叠仍应送达
                    # （只改 system 文本，不新增条目）。
                    folded_kwargs: Dict[str, Any] = {
                        **kwargs, "session_id": session_id, payload_key: payload,
                    }
                    if item_schema_version is not None:
                        folded_kwargs["item_schema_version"] = item_schema_version
                    return {"action": "continue", "modified_kwargs": folded_kwargs}
                return {"action": "continue"}

            # 日期/节假日头部（2026-10-10：由陪伴模块迁入——只在日程块头部注一行，
            # 开关 [schedule].holiday_inject_enabled；失败/关闭时不出现）
            header = await self._date_header()
            if header:
                inject_content = header + "\n" + inject_content

            # 把注入文本作为 system 条目插入消息列表
            modified_payload = self._inject_system_message(payload, inject_content)
            modified_kwargs: Dict[str, Any] = {
                **kwargs,
                "session_id": session_id,
                payload_key: modified_payload,
            }
            if item_schema_version is not None:
                modified_kwargs["item_schema_version"] = item_schema_version
            return {
                "action": "continue",
                "modified_kwargs": modified_kwargs,
            }

        except Exception as exc:
            logger.error(f"注入日程信息失败: {exc}", exc_info=True)
            return {"action": "continue"}

    # ------------------------------------------------------------
    # 消息提取与插入
    # ------------------------------------------------------------

    @classmethod
    def _strip_planner_prefix(cls, text: str) -> str:
        """剥除主程序 build_planner_prefix 加的 ``<message ...>\\n`` 前缀。"""
        return cls._PLANNER_PREFIX_PATTERN.sub("", text, count=1)

    @classmethod
    def _is_metadata_user_message(cls, text: str) -> bool:
        """判定一条 user 消息文本是否是主程序追加的元数据（非真实用户问题）。"""
        for pattern in cls._METADATA_USER_MESSAGE_PATTERNS:
            if pattern.match(text):
                return True
        return False

    @staticmethod
    def _message_role(msg: Dict[str, Any]) -> str:
        """读取消息角色；兼容 ContextItem 快照投影（item_type）、meta.role 与旧 role/content。"""
        item_type = msg.get("item_type")
        if isinstance(item_type, str) and item_type:
            if item_type.endswith("UserMessageItem"):
                return "user"
            if item_type.endswith("SystemMessageItem"):
                return "system"
            if item_type.endswith("AssistantMessageItem"):
                return "assistant"
            return item_type
        meta = msg.get("meta")
        if isinstance(meta, dict):
            meta_role = meta.get("role")
            if isinstance(meta_role, str) and meta_role:
                return meta_role
        role = msg.get("role")
        return role if isinstance(role, str) else ""

    @staticmethod
    def _message_text_candidates(msg: Dict[str, Any]) -> List[str]:
        """提取消息里的候选文本；兼容快照 parts 与旧 role/content 投影。"""
        if isinstance(msg.get("item_type"), str) and msg.get("item_type"):
            parts = msg.get("parts")
            if not isinstance(parts, list):
                return []
            return [
                part["text"]
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ]
        content = msg.get("content")
        if isinstance(content, str):
            return [content]
        if isinstance(content, list):
            candidates: List[str] = []
            for part in content:
                if isinstance(part, str) and part.strip():
                    candidates.append(part)
                elif isinstance(part, dict) and "text" in part:
                    candidates.append(str(part["text"]))
            return candidates
        return []

    # ---- 结构化元数据优先（2026-11-11 Gemini 批 §2.2 P0-1 残留修复）----------
    # 旧实现只靠字符串正则倒序猜测宿主尾指令，宿主改文案即退化为全量闲聊。
    # 现在先读消息项上的结构化标志（is_self_message / source / role / task 标签），
    # 只有宿主字段缺失时才回退旧正则滤镜。

    @staticmethod
    def _parse_message_attrs(text: str) -> Dict[str, str]:
        """解析 ``<message ...>`` 前缀属性（宿主元数据）；无前缀 → 空 dict。"""
        match = _MESSAGE_TAG_PATTERN.match(text or "")
        if not match:
            return {}
        attrs: Dict[str, str] = {}
        for attr in _MESSAGE_ATTR_PATTERN.finditer(match.group("attrs") or ""):
            attrs[attr.group("key").lower()] = attr.group("value")
        return attrs

    @staticmethod
    def _truthy_flag(value: Any) -> bool:
        """结构化布尔字段判定：bool / "true" / "1" / "yes" 为真。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            return value.strip().lower() in {"true", "1", "yes", "y"}
        return False

    @classmethod
    def _structured_self_state(cls, msg: Dict[str, Any], text: str) -> Optional[bool]:
        """消息元数据判定「是否 Bot 自身发言」。

        Returns:
            True = 宿主明确标注自身发言；False = 宿主字段齐全且明确是真实用户；
            None = 宿主字段缺失（调用方回退旧正则逻辑）。
        """
        saw_field = False
        for key in _STRUCTURED_SELF_FLAG_KEYS:
            if key in msg:
                saw_field = True
                if cls._truthy_flag(msg.get(key)):
                    return True
        meta = msg.get("meta")
        if isinstance(meta, dict):
            for key in _STRUCTURED_SELF_FLAG_KEYS:
                if key in meta:
                    saw_field = True
                    if cls._truthy_flag(meta.get(key)):
                        return True
        for key in ("source", "source_kind", "sender_type"):
            for holder in (msg, meta if isinstance(meta, dict) else None):
                if isinstance(holder, dict) and key in holder:
                    saw_field = True
                    if str(holder.get(key) or "").strip().lower() in _STRUCTURED_SELF_SOURCES:
                        return True
        attrs = cls._parse_message_attrs(text)
        if "is_self_message" in attrs:
            saw_field = True
            if cls._truthy_flag(attrs.get("is_self_message")):
                return True
        return False if saw_field else None

    @classmethod
    def _is_structured_system_task(cls, text: str) -> bool:
        """宿主/插件系统任务块（task 标签）不是真实用户输入。

        只认显式标签；旧正则尾指令过滤属「宿主字段缺失时的回退」，由调用方在
        :meth:`_structured_self_state` 返回 ``None`` 后才启用。
        """
        return bool(_STRUCTURED_SYSTEM_TASK_PATTERN.match(text or ""))

    @staticmethod
    def _message_timestamp_date(msg: Dict[str, Any], text: str) -> Optional[str]:
        """消息时间戳的日期部分（ISO ``YYYY-MM-DD``）；取不到 → None。

        快照 ``meta.timestamp`` / 旧投影 ``timestamp``/``time`` 均可能是 ISO；
        ``<message time="HH:MM:SS">`` 只有时刻没有日期，按无日期处理。
        """
        candidates: List[Any] = []
        meta = msg.get("meta")
        if isinstance(meta, dict):
            candidates.append(meta.get("timestamp"))
        for key in ("timestamp", "time", "created_at"):
            candidates.append(msg.get(key))
        for raw in candidates:
            if not isinstance(raw, str):
                continue
            head = raw.strip()[:10]
            if len(head) == 10 and head[4] == "-" and head[7] == "-" \
                    and head.replace("-", "").isdigit():
                return head
        return None

    @classmethod
    def _has_real_interaction_today(
        cls,
        messages: List[Dict[str, Any]],
        *,
        today: str,
    ) -> bool:
        """本轮载荷里是否存在「今天」的真实用户发言（4.4b 触发条件）。

        - 最新一条真实用户消息带日期且 == ``today`` → True（当天已互动，整日生效）；
        - 带日期且早于今天（仅有历史）→ False（无互动不做折叠）；
        - 全部无日期（旧投影）：载荷带 ``<plugin_proactive_task`` 主动块 → False；
          否则按「最后一条真实用户文本即本轮输入」→ True（旧口径兼容）。
        """
        latest_date: Optional[str] = None
        has_real = False
        has_proactive_block = False
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            for candidate in cls._message_text_candidates(msg):
                text = candidate.strip()
                if not text:
                    continue
                if _STRUCTURED_SYSTEM_TASK_PATTERN.match(text) \
                        and "plugin_proactive_task" in text.lower():
                    has_proactive_block = True
                elif cls._is_structured_system_task(text):
                    continue
        for msg in reversed(messages):
            if not isinstance(msg, dict) or cls._message_role(msg) != "user":
                continue
            for candidate in cls._message_text_candidates(msg):
                text = candidate.strip()
                if not text or cls._is_structured_system_task(text):
                    continue
                self_state = cls._structured_self_state(msg, text)
                if self_state is True:
                    continue
                if self_state is None:
                    # 宿主字段缺失 → 回退旧正则/自身发言前缀过滤
                    if cls._is_metadata_user_message(text):
                        continue
                    if cls._SELF_MESSAGE_PREFIX_PATTERN.match(text):
                        continue
                has_real = True
                latest_date = cls._message_timestamp_date(msg, text)
                break
            if has_real:
                break
        if latest_date is not None:
            return latest_date >= today
        if not has_real:
            return False
        return not has_proactive_block

    @classmethod
    def _extract_last_user_text(cls, messages: List[Dict[str, Any]]) -> str:
        """提取最新一条**含真实文本**的 user 消息。

        结构化优先（2026-11-11 Gemini 批 §2.2）：先看消息元数据
        （``is_self_message`` / ``source`` / ``meta`` / ``<message ...>`` 属性 /
        系统 task 标签）判定真实用户输入；只有宿主字段缺失时才回退旧正则滤镜
        （跳过元数据消息、剥除 planner 前缀）。
        """
        for msg in reversed(messages):
            if not isinstance(msg, dict):
                continue
            if cls._message_role(msg) != "user":
                continue
            for candidate in cls._message_text_candidates(msg):
                text = candidate.strip()
                if not text:
                    continue  # 空文本 → 找上一条 user
                if cls._is_structured_system_task(text):
                    continue  # 宿主系统 task / 尾指令块（结构化标签）
                self_state = cls._structured_self_state(msg, text)
                if self_state is True:
                    continue  # 宿主明确标注 Bot 自身发言
                if self_state is False:
                    # 宿主字段齐全且明确是真实用户：不再用字符串正则猜测
                    stripped = cls._strip_planner_prefix(text)
                    if stripped.strip():
                        return stripped
                    continue
                # 宿主字段缺失 → 回退旧逻辑（逐字保留 2026-10-10 N6-3 口径）
                if cls._is_metadata_user_message(text):
                    continue  # 元数据消息/part → 跳过
                if cls._SELF_MESSAGE_PREFIX_PATTERN.match(text):
                    continue  # 复核 P0-1：Bot 自身历史发言（宿主包装为 user）→ 跳过
                stripped = cls._strip_planner_prefix(text)
                if cls._is_metadata_user_message(stripped.strip()):
                    continue
                if not stripped.strip():
                    continue
                return stripped
        return ""

    @staticmethod
    def _is_snapshot_payload(messages: List[Dict[str, Any]]) -> bool:
        """判断 payload 是否为宿主 1.2.x 的 ContextItem 快照投影。"""
        return any(
            isinstance(msg, dict) and isinstance(msg.get("item_type"), str) and msg.get("item_type")
            for msg in messages
        )

    @staticmethod
    def _build_snapshot_system_item(
        inject_content: str,
        messages: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """构造可被宿主反序列化的 SystemMessageItem 快照（1.2.x payload 契约）。

        - ``item_id`` 必须全局唯一（宿主 validate_context_items 硬性要求）；
        - ``logical_turn_id`` 可为 null（键必须存在）；
        - ``timestamp`` 必须是合法 ISO 时间。
        """
        logical_turn_id: Optional[str] = None
        for msg in messages:
            meta = msg.get("meta") if isinstance(msg, dict) else None
            if isinstance(meta, dict):
                candidate = meta.get("logical_turn_id")
                if isinstance(candidate, str) and candidate.strip():
                    logical_turn_id = candidate
                    break
        return {
            "item_type": "SystemMessageItem",
            "meta": {
                "item_id": f"app-schedule-{uuid4().hex}",
                "logical_turn_id": logical_turn_id,
                "timestamp": datetime.now().astimezone().isoformat(),
            },
            "parts": [{"type": "text", "text": inject_content}],
        }

    @classmethod
    def _inject_system_message(
        cls,
        messages: List[Dict[str, Any]],
        inject_content: str,
    ) -> List[Dict[str, Any]]:
        """把注入文本作为 system 条目插入消息列表（紧跟第一条 system 之后）。"""
        if cls._is_snapshot_payload(messages):
            injection: Dict[str, Any] = cls._build_snapshot_system_item(inject_content, messages)
        else:
            injection = {"role": "system", "content": inject_content}

        first_system_idx: Optional[int] = None
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict) and cls._message_role(msg) == "system":
                first_system_idx = idx
                break

        new_messages = list(messages)  # 浅拷贝列表，避免污染调用方
        insert_at = (first_system_idx + 1) if first_system_idx is not None else 0
        new_messages.insert(insert_at, injection)
        return new_messages

    # ------------------------------------------------------------
    # 注入文本构建（统一管道：IntentClassifier + InjectOptimizer + 模板路由）
    # ------------------------------------------------------------

    def _build_inject_text(
        self,
        user_message: str,
        current_activity: Optional[str],
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        activity_type: Optional[str],
        context_continue_inject: bool,
        context_reason: Optional[str],
        user_id: str,
        remaining_override: Optional[int] = None,
        life_lines: Optional[List[str]] = None,
        life_lines_slim: Optional[List[str]] = None,
        live_gate_enabled: bool = False,
        proactive_share: bool = False,
    ) -> Tuple[Optional[str], bool, Optional[str]]:
        """分类 → 决策 → 模板路由。user_id 实际是会话维度 scope ID（session_id）。

        ``current_activity`` 允许为空（2026-10-10 N1-2）：空活动不再是「静默早退」，
        而是走同一决策管道 + 空活动文案变体；决策权仍交给 optimizer/上下文延续
        （2026-11-03 规则2移除后空活动对闲聊亦放行，仅按概率与冷却节流）。

        R7：``remaining_override``（runtime episode 剩余分钟）与 ``life_lines``
        （runtime 实况追加行）由调用方在 runtime 视图就绪时传入；
        缺省 None = 未就绪口径（既有调用点与测试桩零变化）。

        2026-11-04 双通道门控：``live_gate_enabled=True``（非目标私聊会话）且
        意图非询问（query_current/query_future 之外）→ 实况追加行改用
        ``life_lines_slim``（仅「当前活动实况」一行，无接下来/状态/刚完成）；
        询问意图 → 保留 ``life_lines`` 全量。缺省 False = 旧行为逐字（既有调用点
        与测试桩零变化）。门控只改实况内容详略，不改注入时机决策；空活动兜底
        （现在行）不在 life_lines 内，不受门控影响（23:50 防编造成果保留）。

        Returns:
            (inject_content, injected, detected_intent)。``inject_content`` 为 None
            表示决策跳过；``detected_intent`` 用于上下文缓存追踪。
        """
        # 1. 意图分类
        intent: UserIntent
        confidence: float
        if self._intent_classifier is not None:
            intent, confidence = self._intent_classifier.classify(user_message)
        else:
            intent, confidence = UserIntent.CASUAL_CHAT, 0.5

        detected_intent = intent.value

        # 1.5 实况门控（2026-11-04）：非目标会话且非询问意图 → 实况瘦身。
        # 不确定兜底（UNKNOWN/闲聊低分）落在保守侧——宁可少说不漏说。
        effective_life_lines = life_lines
        if live_gate_enabled and not self._is_schedule_query_intent(intent):
            effective_life_lines = life_lines_slim
            logger.debug(
                "planner 实况门控: intent=%s 非询问，实况瘦身（仅当前活动）", detected_intent,
            )

        # 2. 决策
        if context_continue_inject:
            logger.info(f"对话上下文触发注入: {context_reason}")
            should_inject = True
        elif self._inject_optimizer is not None:
            should_inject, skip_reason = self._inject_optimizer.should_inject(
                user_id, intent, current_activity, confidence,
            )
        else:
            if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
                should_inject = False
            else:
                should_inject = True

        if not should_inject:
            logger.debug(f"注入跳过: intent={detected_intent}")
            return None, False, detected_intent

        # 3. 查询活动剩余分钟（无当前活动时无意义，直接跳过；M4：精神状态行改读
        #    情绪 mood_text，见 _render_unified_prompt）。R7：runtime episode 剩余
        #    分钟由调用方经 remaining_override 传入，查不到才回退旧 GoalManager 查询。
        remaining_minutes = remaining_override
        if remaining_minutes is None and current_activity:
            remaining_minutes = self._compute_activity_aux(current_activity, activity_type)

        # 4. 主动碎碎念判定：仅 CASUAL_CHAT/UNKNOWN 才考虑
        proactive_hit = False
        if (
            intent in (UserIntent.CASUAL_CHAT, UserIntent.UNKNOWN)
            and not context_continue_inject
            and self._inject_optimizer is not None
        ):
            proactive_hit, _proactive_reason = self._inject_optimizer.should_proactive_inject(
                user_id, current_activity,
            )
            if proactive_hit:
                logger.info(f"主动碎碎念触发: scope={user_id}, activity={current_activity}")
                self._inject_optimizer.record_proactive_inject(user_id)

        # 5. 按 intent 渲染统一模板
        inject_content = self._render_unified_prompt(
            intent=intent,
            current_activity=current_activity,
            current_description=current_description,
            future_activities=future_activities,
            remaining_minutes=remaining_minutes,
            proactive_hit=proactive_hit,
            context_continue_inject=context_continue_inject,
            context_reason=context_reason,
            life_lines=effective_life_lines,
            proactive_share=proactive_share,
        )
        if inject_content is None:
            return None, False, detected_intent

        # 6. 记录注入历史（让 InjectOptimizer 的冷却生效）
        if self._inject_optimizer is not None:
            self._inject_optimizer.record_injection(
                user_id, current_activity, inject_content, intent,
            )

        logger.info(
            f"注入完成: intent={detected_intent}, confidence={confidence:.2f}, "
            f"len={len(inject_content)}, proactive={proactive_hit}"
        )
        return inject_content, True, detected_intent

    def _render_unified_prompt(
        self,
        intent: "UserIntent",
        current_activity: Optional[str],
        current_description: Optional[str],
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
        proactive_hit: bool,
        context_continue_inject: bool,
        context_reason: Optional[str],
        life_lines: Optional[List[str]] = None,
        proactive_share: bool = False,
    ) -> Optional[str]:
        """按 intent 路由统一模板（详略与语气随意图变化；TECH/COMMAND 返回 None）。

        M4（§4.2 行 8）：精神状态行读情绪系统 ``mood_text()``（与 emotion 注入条目同源，
        不得出现两套矛盾的「精神状态」）；情绪不可得时省略该行，不再使用已删除的
        inject/state_analyzer 静态模板。

        ``current_activity`` 为空（2026-10-10 N1-2）：当前行换成内部键
        ``inject_no_activity`` 兜底文案（suite_prompts.INJECT_NO_ACTIVITY_DEFAULT，
        恒非空、不接受配置覆盖）。2026-11-04 评估问题8：原「文案被显式关空→返回
        None」分支不可达（该键不在 PromptsSectionConfig 字段面，prompt() 恒返默认），
        已随死代码清理移除。
        """
        if intent in (UserIntent.TECH_QUESTION, UserIntent.COMMAND_EXECUTION):
            return None

        cfg = self._schedule_cfg()
        enable_detailed = cfg.enable_detailed_description
        max_show = getattr(section_or_default(
            self._plugin, "schedule_advanced", ScheduleAdvancedSectionConfig),
            "max_future_activities", 3)

        # 当前活动 + 时间衰减
        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        if not current_activity:
            # P1：引擎确认已睡即使 episode 缺失也表达睡眠中，不降为「空闲」兜底文案。
            engine0 = getattr(self._plugin, "_sleep_engine", None)
            try:
                asleep0 = engine0.is_asleep() if engine0 is not None else None
            except Exception:
                asleep0 = None
            if asleep0 is True:
                current_line = ("你现在已经入睡（睡眠中）；收到消息可自然短回复，"
                                "不要声称正在活动、待机或正准备睡。")
            else:
                # 空活动变体：兜底文案（内部键恒返非空默认，无「显式关空」路径）
                current_line = self._no_activity_text()
        elif enable_detailed and current_description:
            current_line = render(
                prompt(self._plugin, "inject_schedule_current_line"),
                fallback=INJECT_SCHEDULE_CURRENT_LINE_DEFAULT,
                activity=current_activity, description=current_description,
                remaining_hint=remaining_hint,
            )
        else:
            # 无描述：同一模板渲染后把空的「（…）」括注收干净（自定义模板里没有括注时无副作用）
            current_line = render(
                prompt(self._plugin, "inject_schedule_current_line"),
                fallback=INJECT_SCHEDULE_CURRENT_LINE_DEFAULT,
                activity=current_activity, description="", remaining_hint=remaining_hint,
            )
            current_line = current_line.replace("（）", "").replace("()", "").strip()

        engine = getattr(self._plugin, "_sleep_engine", None)
        try:
            asleep_now = engine.is_asleep() if engine is not None else None
        except Exception:
            asleep_now = None
        if asleep_now is True:
            # P1：已睡优先级高于旧的 winding_down 缓存与精力/情绪措辞。
            if "已入睡" not in current_line:
                current_line += "（已入睡）"
        elif getattr(engine, "dynamic_enabled", False):
            # 3.9.3 热修：此处无 now 局部变量（原写法 NameError 致日程注入整体失败）；
            # dynamic_status() 缺省由引擎按自身时区取当前时间。
            band = engine.dynamic_status().get("band")
            cue = {"drowsy": "有点困", "winding_down": "困了，准备休息",
                   "waking": "刚醒，还在缓神"}.get(band)
            if cue:
                current_line += f"（{cue}）"

        lines: List[str] = [
            prompt(self._plugin, "inject_schedule_header"),
            current_line,
        ]
        # 3.17.29（复核 VAD-4.2/P1-11）：日程块不再重复注入心情行——planner 的
        # 情绪底色由 emotion Hook 统一承载（injection_enabled 开关同源），日程块
        # 只负责日程事实。原实现另走 mood_text 摘要（含百分比）且绕过注入开关。
        if intent == UserIntent.QUERY_FUTURE and future_activities and max_show > 0:
            lines.append(prompt(self._plugin, "inject_schedule_future_title"))
            for time_str, name in future_activities[:max_show]:
                lines.append(render(
                    prompt(self._plugin, "inject_schedule_future_line"),
                    fallback=INJECT_SCHEDULE_FUTURE_LINE_DEFAULT,
                    time=time_str, name=name,
                ))
            lines.extend(["", prompt(self._plugin, "inject_hint_future")])
        elif intent == UserIntent.QUERY_CURRENT:
            if future_activities and max_show > 0:
                time_str, name = future_activities[0]
                line = render(
                    prompt(self._plugin, "inject_schedule_future_line"),
                    fallback=INJECT_SCHEDULE_FUTURE_LINE_DEFAULT,
                    time=time_str, name=name,
                ).strip()
                lines.append(f"接下来：{line}")
            if self._state_subconscious_style():
                # 潜意识风格：问当前只如实说活动，状态用语气体现、不逐项汇报
                current_hint = INJECT_HINT_CURRENT_SUBCONSCIOUS_DEFAULT
            else:
                current_hint = prompt(self._plugin, "inject_hint_current")
            lines.extend(["", current_hint])
        else:
            if context_continue_inject:
                lines.extend(["", render(
                    prompt(self._plugin, "inject_hint_continue"),
                    fallback=INJECT_HINT_CONTINUE_DEFAULT,
                    reason=context_reason or "",
                )])
            elif proactive_hit:
                lines.extend(["", prompt(self._plugin, "inject_hint_proactive")])
            else:
                lines.extend(["", prompt(self._plugin, "inject_hint_idle")])
            if proactive_share:
                # 复核 P1-9：主动分享轮追加运行时豁免（代码层，不改模板/schema）。
                lines.append(_INJECT_PROACTIVE_SHARE_HINT)

        lines.extend(["", "---", ""])
        # R7 runtime 实况追加行（runtime 视图就绪时由调用方传入；未就绪 → None
        # = 追加块缺席）。位置：引导行之后、文本末尾。
        # 2026-11-04 双通道门控：内容详略已在上游 _build_inject_text 按会话类型 +
        # 意图选好（目标私聊/询问意图 = 全量；其余会话闲聊/未知 = 仅当前活动实况
        # 一行），此处照单追加。与 23:50 空活动兜底正交：兜底是上方「现在行」，
        # 不在 life_lines 内，门控不影响其送达。
        if life_lines:
            lines.append("")
            lines.extend(life_lines)
        return "\n".join(lines)

    def _no_activity_text(self) -> str:
        """无当前活动的固定上下文，不接受配置覆盖。"""
        return prompt(self._plugin, "inject_no_activity")

    # ------------------------------------------------------------
    # 状态潜意识化（2026-11-11 Gemini 批 P2-EXP-1 / 报告 §5.2）
    # ------------------------------------------------------------

    def _prompts_section(self) -> Any:
        """``[prompts]`` 配置段（与 :func:`suite_prompts.prompt` 同取数序）。"""
        section = getattr(self._plugin, "prompts", None)
        if section is None:
            section = getattr(getattr(self._plugin, "config", None), "prompts", None)
        return section

    def _state_subconscious_style(self) -> bool:
        """``[prompts].state_subconscious_style``（出厂默认 True；段缺失按默认）。"""
        section = self._prompts_section()
        if section is None:
            return True
        value = getattr(section, "state_subconscious_style", True)
        return True if value is None else bool(value)

    def _life_state_snapshot(self) -> Dict[str, float]:
        """只读五维状态快照（精力/饥饿/社交电量）；缺失/异常 → 空 dict。

        取数序：``plugin._life_state`` → runtime 读模型的 ``_life`` 句柄。
        只用于选择语气分支，不改任何状态数值。
        """
        life = getattr(self._plugin, "_life_state", None)
        if life is None:
            model = self._life_read_model()
            life = getattr(model, "_life", None) if model is not None else None
        if life is None:
            return {}
        try:
            snapshot = life.snapshot()
        except Exception:
            return {}
        if not isinstance(snapshot, dict):
            return {}
        out: Dict[str, float] = {}
        for key in ("energy", "hunger", "social_battery"):
            try:
                out[key] = float(snapshot.get(key))
            except (TypeError, ValueError):
                continue
        return out

    def _emotion_valence(self) -> Optional[float]:
        """情绪效价（开心分支辅助判据）；情绪模块缺失/异常 → None。"""
        emotion = getattr(self._plugin, "_emotion", None)
        service = getattr(emotion, "service", None)
        getter = getattr(service, "effective_valence", None)
        if not callable(getter):
            return None
        try:
            return float(getter())
        except Exception:
            return None

    def _subconscious_tone_rule(self) -> str:
        """按当前状态选语气分支（疲惫 > 饥饿 > 开心 > 平稳）。

        疲惫：精力 < 0.40 或社交电量 < 0.20；饥饿：饥饿 >= 0.60；
        开心：精力 >= 0.72 或社交电量 >= 0.85 或情绪效价 >= 0.30。
        数据缺失时落到平稳分支。
        """
        snapshot = self._life_state_snapshot()
        energy = snapshot.get("energy")
        hunger = snapshot.get("hunger")
        social = snapshot.get("social_battery")
        valence = self._emotion_valence()
        if (energy is not None and energy < 0.40) or (
                social is not None and social < 0.20):
            return SUBCONSCIOUS_TONE_TIRED_DEFAULT
        if hunger is not None and hunger >= 0.60:
            return SUBCONSCIOUS_TONE_HUNGRY_DEFAULT
        if (energy is not None and energy >= 0.72) or (
                social is not None and social >= 0.85) or (
                valence is not None and valence >= 0.30):
            return SUBCONSCIOUS_TONE_HAPPY_DEFAULT
        return SUBCONSCIOUS_TONE_STEADY_DEFAULT

    def _build_replyer_extra_prompt(
        self,
        current_activity: str,
        description: str,
        future_activities: List[Tuple[str, str]],
        remaining_minutes: Optional[int],
    ) -> str:
        """构建 replyer 极简 extra_prompt（~30 token + 能量基线；模板可在 [prompts] 段定制）。"""
        cfg = self._schedule_cfg()
        enable_detailed = cfg.enable_detailed_description

        remaining_hint = ""
        if remaining_minutes is not None and 0 < remaining_minutes <= 15:
            remaining_hint = f"（约还剩 {remaining_minutes} 分钟）"

        state_line = render(
            prompt(self._plugin, "inject_replyer_state_line"),
            fallback=INJECT_REPLYER_STATE_LINE_DEFAULT,
            activity=current_activity,
            description=description if (enable_detailed and description) else "",
            remaining_hint=remaining_hint,
        )
        if not (enable_detailed and description):
            state_line = state_line.replace("（）", "").replace("()", "").strip()
        return self._render_replyer_status_block(state_line, future_activities)

    def _build_replyer_no_activity_prompt(self, future_activities: List[Tuple[str, str]]) -> str:
        """replyer extra_prompt 的空活动变体（2026-10-10 N1-2）。

        与有活动版本共用 ``[prompts]`` 模板：``state_line`` 换成「无当前活动」兜底行
        （内部键 ``inject_no_activity``，恒非空、不接受配置覆盖），能量行 / 下一条
        活动行 / 不主动提及提示照常。2026-11-04 评估问题8：原「文案被显式关空→
        返回空串」分支不可达，已随死代码清理移除。
        """
        state_line = self._no_activity_text()
        return self._render_replyer_status_block(state_line, future_activities)

    def _replyer_energy_desc(self) -> str:
        """replyer「精神」能量行取值（R7 滚动化改造 §6.6：LifeState 优先）。

        runtime 视图就绪且 ``LifeStateService.describe`` 可得 → 用生活状态
        行（剥「状态：」前缀适配 ``精神：{energy}`` 模板槽）；LifeState 缺失/未就绪/
        异常 → 回退既有静态曲线 ``describe_energy(get_energy_level(...))``
        （旧函数不删，``planning/time_utils._ENERGY_TABLE`` 链保留为 fallback）。
        """
        view = self._life_runtime_view()
        if view is not None:
            # 复核 P1-10：replyer 走无数值口语版（planner 仍保留数值口径）；
            # 直接从读模型取，runtime 视图键集不变。取不到再回退原 state_line。
            now = view.get("now") or self._tz_manager.get_now()
            state_line = ""
            model = self._life_read_model()
            if model is not None:
                try:
                    state_line = str(model.state_line(
                        now, max_items=2, include_numbers=False) or "").strip()
                except Exception:
                    state_line = ""
            if not state_line:
                state_line = str(view.get("state_line") or "").strip()
            if state_line:
                prefix = "状态："
                return state_line[len(prefix):] if state_line.startswith(prefix) else state_line
        now = self._tz_manager.get_now()
        return describe_energy(get_energy_level(now.hour))

    def _render_replyer_status_block(
        self,
        state_line: str,
        future_activities: List[Tuple[str, str]],
    ) -> str:
        """按 ``inject_replyer_status`` 模板拼 replyer 状态块（能量 + 下一条活动）。

        R7：能量行改经 :meth:`_replyer_energy_desc`（LifeState 优先，缺失/关闭
        回退静态 ``describe_energy``——关闭路径取值逐字不变）。

        2026-11-11（Gemini 批 P2-EXP-1 / §5.2）：``state_subconscious_style``
        开（默认）→ 走潜意识模板：状态数据原样保留，但明确禁止直接复述状态词，
        改由 ``{tone_rule}`` 约束语气/句长（疲惫→短句/省略号、开心→轻快多句、
        饥饿→随口提食欲）；关 → 旧模板逐字不变。
        """
        energy_desc = self._replyer_energy_desc()

        next_line = ""
        if future_activities:
            time_str, name = future_activities[0]
            next_line = render(
                prompt(self._plugin, "inject_replyer_next_line"),
                fallback=INJECT_REPLYER_NEXT_LINE_DEFAULT,
                time=time_str, name=name,
            )
        if self._state_subconscious_style():
            # 内部固定模板（不新增用户 schema 键）：数据原样保留，口径改为语气约束
            return render(
                INJECT_REPLYER_STATUS_SUBCONSCIOUS_DEFAULT,
                fallback=INJECT_REPLYER_STATUS_SUBCONSCIOUS_DEFAULT,
                state_line=state_line, energy=energy_desc,
                tone_rule=self._subconscious_tone_rule(), next_line=next_line,
            )
        return render(
            prompt(self._plugin, "inject_replyer_status"),
            fallback=INJECT_REPLYER_STATUS_DEFAULT,
            state_line=state_line, energy=energy_desc, next_line=next_line,
        )

    def _resolve_replyer_intent(
        self,
        session_id: str,
        kwargs: Dict[str, Any],
    ) -> Tuple["UserIntent", float]:
        """replyer 通道的「真实意图」（2026-10-10 N6-1：不再写死闲聊）。

        取数顺序（全程可空降级，取不到就回到修复前口径 ``CASUAL_CHAT``/0.5）：

        1. 请求载荷里若带 ``items``/``messages``（部分宿主版本会传），就地跑既有
           ``IntentClassifier``；
        2. 否则复用本轮 planner 在该会话记下的意图（``ConversationContextCache``；
           同一轮 planner 请求先于 replyer，键与 planner 的 ``user_id`` 一致），
           只接受 ``_REPLYER_INTENT_REUSE_SECONDS`` 内的记录，避免隔轮误用；
        3. 都没有 → ``(CASUAL_CHAT, 0.5)``（与修复前逐字一致）。

        效果：用户明确问当前/未来状态时不再被 50% 硬币挡掉；冷却只对「同活动 +
        同意图」生效，replyer 不再被自己写死的同一种意图反复盖冷却章。
        """
        if self._intent_classifier is not None:
            payload = kwargs.get("items")
            if not isinstance(payload, list):
                payload = kwargs.get("messages")
            if isinstance(payload, list) and payload:
                text = self._extract_last_user_text(payload)
                if text:
                    return self._intent_classifier.classify(text)
        if self._context_cache is not None:
            try:
                turns = self._context_cache.get_recent_turns(session_id or "unknown", count=1)
            except Exception:
                turns = []
            if turns:
                last = turns[-1]
                if last.intent and (time.time() - float(last.timestamp or 0)) <= _REPLYER_INTENT_REUSE_SECONDS:
                    try:
                        return UserIntent(str(last.intent)), 0.5
                    except ValueError:
                        pass
        return UserIntent.CASUAL_CHAT, 0.5

    def _compute_activity_aux(
        self,
        activity_name: str,
        goal_type: Optional[str],
    ) -> Optional[int]:
        """查询某活动剩余分钟（查不到返回 None）。

        M4：活动状态情绪短语随 inject/state_analyzer 删除，本方法只余剩余时间一项；
        ``goal_type`` 参数保留以兼容调用点签名（当前不使用）。
        """
        try:
            goal_manager = get_goal_manager()
            goals = goal_manager.get_active_goals(chat_id="global")
            if not goals:
                return None

            now = self._tz_manager.get_now()
            current_min = now.hour * 60 + now.minute

            for goal in goals:
                if goal.name != activity_name:
                    continue
                tw: Optional[List[int]] = None
                if goal.parameters and "time_window" in goal.parameters:
                    tw = goal.parameters.get("time_window")
                elif goal.conditions:
                    tw = goal.conditions.get("time_window")
                if not tw:
                    continue
                start, end = parse_time_window(tw)
                if start is None or end is None:
                    continue
                # 跨夜归一化：end_minutes > 1440 表示跨夜，凌晨阶段 end 减 1440
                if end > 1440 and current_min < 720:
                    end -= 1440
                remaining = end - current_min
                if remaining <= 0:
                    return None
                return remaining
        except Exception as exc:
            logger.debug("计算活动辅助信息失败: %s", exc)
        return None

    # 3.17.29（复核 VAD-4.2/P1-11）：删除原 `_emotion_mood_line`——日程块与情绪
    # Hook 重复注入且绕过 injection_enabled；planner 情绪底色统一由 Hook 承载。

    # ------------------------------------------------------------
    # R7 runtime 注入视图（runtime 视图；未就绪/缺失/异常一律旁路）
    # ------------------------------------------------------------

    def _life_read_model(self) -> Optional[Any]:
        """:class:`~planning.life_schedule.LifeScheduleReadModel` 句柄解析（可空降级）。

        取数序：接线服务束 ``plugin._planning.life_schedule`` → ``ScheduleQuery``
        的 runtime provider（接线阶段 ``sq.set_runtime_provider(model)`` 注入，
        :attr:`planning.schedule_query.ScheduleQuery._runtime_provider`）。
        都缺失 → ``None``（= 旧路径逐字；服务束未挂上/测试桩不抛异常）。
        """
        planning = getattr(self._plugin, "_planning", None)
        model = getattr(planning, "life_schedule", None)
        if model is not None:
            return model
        query = getattr(self._plugin, "_schedule_query", None)
        provider = getattr(query, "_runtime_provider", None)
        return provider or None

    def _life_runtime_view(self) -> Optional[Dict[str, Any]]:
        """一次注入的 runtime 视图（只读；``model.enabled``（= ``[schedule].enabled``）关
        或 provider 缺失 → None 旁路）。

        键：``current``（episode 兼容行或 None）/ ``next``（≤2 条）/ ``state_line``
        （LifeStateService.describe 最显著 1~2 项，可空串）/ ``completed``
        （completed_today 尾部 ≤2 行——recent 连续性）/ ``now``（判定时刻）。
        任何读取异常 → ``None``（旁路，绝不阻塞注入）。
        """
        model = self._life_read_model()
        if model is None:
            return None
        try:
            if not bool(getattr(model, "enabled", False)):
                return None
        except Exception:
            return None
        try:
            now = self._tz_manager.get_now()
            # P1（2026-11-09 睡眠注入修复）：仅注入调用处显式包含睡眠，不全局改读模型
            # 默认（其他消费者有意排除睡眠的行为保留）。过滤后的空值不再被误当真实空闲。
            current = model.current(now, include_sleep=True) or None
            nxt = list(model.next(now, limit=2) or [])
            try:
                state_line = str(model.state_line(now, max_items=2) or "").strip()
            except Exception:
                state_line = ""
            try:
                today = model.today(now) or {}
            except Exception:
                today = {}
            completed = [row for row in (today.get("completed") or [])
                         if isinstance(row, dict)][-2:]
            return {"current": current, "next": nxt, "state_line": state_line,
                    "completed": completed, "now": now,
                    "sleep": self._sleep_presentation(now)}
        except Exception as exc:
            logger.debug("life runtime 注入视图读取失败（回退旧路径）：%s", exc)
            return None

    def _proactive_share_turn(self, session_id: str, payload: Any = None) -> bool:
        """复核 P1-9：本轮是否为需要分享近况/视频的主动回合。

        判定顺序（全程可空降级，异常一律按 False = 旧行为）：
        1. 主动激活登记簿：kind ∈ {video_share, surf_chitchat}，或 miss 的 reason
           带经历分享话题（``最近经历参考（可选话题）``）；
        2. 登记已被 replyer 改写钩子消费时，从载荷文本反推：经历分享标记，
           或 ``REWRITE_MATRIX`` 的分享类定制文案。

        只用于给 planner/replyer 的「不主动提日程」加一句运行时豁免，
        不触碰预算/静默/好感/发送门控。
        """
        getter = getattr(self._plugin, "_mai_proactive_turns", None)
        if callable(getter) and session_id:
            try:
                registry = getter()
                turn = registry.active_for(session_id) if registry is not None else None
            except Exception:
                turn = None
            if turn:
                kind = str(turn.get("kind") or "")
                reason = str(turn.get("reason") or "")
                if kind in _PROACTIVE_SHARE_KINDS or _EXPERIENCE_SHARE_MARKER in reason:
                    return True
        if payload:
            try:
                blob = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                blob = str(payload)
            if _EXPERIENCE_SHARE_MARKER in blob:
                return True
            try:
                from tools.proactive_turn import (
                    CHAT_TYPE_PRIVATE_TARGET,
                    rewrite_notice_for,
                )
                for kind in ("video_share", "surf_chitchat"):
                    if rewrite_notice_for(CHAT_TYPE_PRIVATE_TARGET, kind) in blob:
                        return True
            except Exception:
                logger.debug("主动分享轮判定：定制文案探测失败（降级 False）", exc_info=True)
        return False

    def _sleep_presentation(self, now) -> Dict[str, Any]:
        """P1 权威睡眠表现态：SleepEngine 会话优先，episode 仅名称/时间佐证。

        区分 asleep/awake/drowsy/winding_down/waking/unknown；引擎确认已睡即使 episode
        缺失也表达睡眠中；引擎确认已醒而 episode 残留睡觉按醒态并记不一致；引擎不表态
        且有可信 active sleep 保守表达睡眠；证据不足表达 unknown（不伪造空闲）。
        """
        out: Dict[str, Any] = {"state": "unknown", "started_at": None, "source": None}
        engine = getattr(self._plugin, "_sleep_engine", None)
        try:
            asleep = engine.is_asleep(now) if engine is not None else None
        except Exception:
            asleep = None
        started = None
        try:
            sess = getattr(engine, "_session", None)
            if sess is not None:
                started = (getattr(sess, "metadata", None) or {}).get("sleep_started_at")
        except Exception:
            started = None
        ep_sleep = False
        try:
            model = self._life_read_model()
            cur = model.current(now, include_sleep=True) if model is not None else None
            ep_sleep = bool(cur) and str(cur.get("goal_type") or "") == "sleep"
        except Exception:
            ep_sleep = False
        if asleep is True:
            out.update(state="asleep", started_at=started, source="engine")
            return out
        if asleep is False and ep_sleep:
            logger.debug("sleep presentation: 引擎已醒但 sleep episode 残留（按醒态表达）")
            out.update(state="awake", source="engine_episode_conflict")
            return out
        if asleep is None and ep_sleep:
            out.update(state="asleep", started_at=started, source="episode_conservative")
            return out
        # 尚未入睡：困倦/准备休息/刚醒 仅在此分支使用（已睡优先级更高，上面已返回）
        try:
            band = engine.dynamic_status().get("band") if engine is not None else None
        except Exception:
            band = None
        if band in ("drowsy", "winding_down", "waking"):
            out.update(state=band, source="engine_band")
        else:
            out.update(state="awake", source="engine" if asleep is False else None)
        return out

    @staticmethod
    def _life_override_from_view(view: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """runtime 视图 → 注入四元组覆盖值（R7 红线 3：当前活动只读 episode）。

        返回 ``{name, description, goal_type, remaining, future}``：runtime 空闲时
        ``name/description/goal_type`` 为 ``None``（调用方据此走空活动变体，不从
        schedule_items 时间窗编造「正在做」）；``future`` = runtime next ≤2 条的
        ``(format_day_minute(start), name)`` 列表；``remaining`` = episode 端点
        ``ring_minutes_ahead`` 环归一剩余分钟（跨午夜条目凌晨也能算对）。
        """
        empty = {"name": None, "description": None, "goal_type": None,
                 "remaining": None, "future": []}
        if not isinstance(view, dict):
            return empty
        current = view.get("current")
        name: Optional[str] = None
        description: Optional[str] = None
        goal_type: Optional[str] = None
        remaining: Optional[int] = None
        if isinstance(current, dict):
            name = str(current.get("name") or "").strip() or None
            description = str(current.get("description") or "").strip() or None
            goal_type = str(current.get("goal_type") or "").strip() or None
            try:
                end = int(current.get("end_minute") or 0)
            except (TypeError, ValueError):
                end = 0
            if end > 0:
                try:
                    remaining = ring_minutes_ahead(end, minutes_of_now(view.get("now")))
                except Exception:
                    remaining = None
        future: List[Tuple[str, str]] = []
        for item in (view.get("next") or [])[:2]:
            if not isinstance(item, dict):
                continue
            try:
                start = int(item.get("start_minute") or 0)
            except (TypeError, ValueError):
                continue
            item_name = str(item.get("name") or "").strip()
            if item_name:
                future.append((format_day_minute(start), item_name))
        return {"name": name, "description": description, "goal_type": goal_type,
                "remaining": remaining, "future": future}

    @staticmethod
    def _life_block_lines(view: Optional[Dict[str, Any]],
                          override: Optional[Dict[str, Any]],
                          slim: bool = False,
                          subconscious: bool = False) -> List[str]:
        """runtime 实况追加行（主设计 §3.3 注入清单；零 emoji）。

        current（名称/已进行/进度/注意力/剩余）+ next ≤2 + 状态行 + recent ≤2
        （completed_today 尾部实际发生）。视图为空 → 空列表（不追加任何行）。

        2026-11-04 实况瘦身：``slim=True`` 仅输出「当前活动实况」一行（不含
        接下来/状态/刚完成）——供非目标私聊会话的非询问意图使用，移除未来计划
        与刚完成语料（排查 E4/E5/E6 泄漏主源），保留当前活动维持角色连续性。
        缺省 False = 全量逐字（既有调用点与测试桩零变化）。

        2026-11-11（Gemini 批 P2-EXP-1 / §5.2）：``subconscious=True`` 时在状态
        行尾追加「只用于调整语气 / 不要直接复述」注记，状态数据本身不改；
        缺省 False = 旧文案逐字（开关关闭路径）。
        """
        if not isinstance(view, dict):
            return []
        lines: List[str] = []
        current = view.get("current")
        now = view.get("now")
        cur_min = minutes_of_now(now)
        if isinstance(current, dict):
            segs: List[str] = []
            try:
                start = int(current.get("start_minute") or 0)
            except (TypeError, ValueError):
                start = 0
            if start > 0:
                # 已进行 = cur 相对 start 的环距（「经过」方向）：ring_minutes_ahead(cur, start)
                # ——2026-10-25 门禁员修新代码 bug（R7 依据 = 用户 2026-10-25 直派【滚动化改造】）：
                # 原写法 ring_minutes_ahead(start, cur) 是「还剩」方向，start<cur 时会渲染
                # 「已进行 1430 分钟」这类环回大数；换参后同帧 10:00→10:10 = 10、
                # 跨午夜 23:00→00:30 = 90，两态皆对。
                elapsed = ring_minutes_ahead(cur_min, start)
                if elapsed > 0:
                    segs.append(f"已进行 {elapsed} 分钟")
            # 复核 P1-10：进度百分比不进 planner 文本；注意力档位换自然中文，
            # 未知值直接略过（不再把 now/brief/later 英文枚举直出给模型）。
            attention = _ATTENTION_MODE_TEXT.get(
                str(current.get("attention_mode") or "").strip().lower())
            if attention:
                segs.append(attention)
            remaining = (override or {}).get("remaining")
            if isinstance(remaining, int) and remaining > 0:
                segs.append(f"约还剩 {remaining} 分钟")
            head = str(current.get("name") or "").strip() or "当前活动"
            if segs:
                lines.append(f"当前活动实况：{head}，" + "，".join(segs))
        if slim:
            # 瘦身口径：仅当前活动实况一行；接下来/状态/刚完成不输出
            # （无当前活动时 lines 为空 → 不追加任何行，空活动兜底走现在行）
            return lines
        nxt_parts: List[str] = []
        # P1：已睡时不附「接下来…（可能）」等易被误读为正在行动的内容；未来计划保留在查询/页面。
        _next_items = [] if (view.get("sleep") or {}).get("state") == "asleep" \
            else (view.get("next") or [])[:2]
        for item in _next_items:
            if not isinstance(item, dict):
                continue
            try:
                start = int(item.get("start_minute") or 0)
            except (TypeError, ValueError):
                continue
            item_name = str(item.get("name") or "").strip()
            if not item_name:
                continue
            mark = ""
            if item.get("paused"):
                mark = "（可恢复）"
            elif item.get("is_preview"):
                mark = "（可能）"
            elif item.get("is_spine_intent"):
                mark = "（打算）"
            nxt_parts.append(f"{format_day_minute(start)} {item_name}{mark}")
        if nxt_parts:
            lines.append("接下来：" + "；".join(nxt_parts))
        state_line = str(view.get("state_line") or "").strip()
        if state_line:
            if subconscious:
                # 内部键（不接受配置覆盖），直接取出厂文案，保持 staticmethod 无 plugin 依赖
                state_line += SUBCONSCIOUS_STATE_LINE_NOTE_DEFAULT
            lines.append(state_line)
        for row in (view.get("completed") or [])[-2:]:
            if not isinstance(row, dict):
                continue
            row_name = str(row.get("name") or "").strip()
            if not row_name:
                continue
            start = row.get("actual_start_minute")
            end = row.get("actual_end_minute")
            if start is None:
                start = row.get("planned_start_minute")
            if end is None:
                end = row.get("planned_end_minute")
            span = ""
            try:
                if start is not None and end is not None:
                    span = f"（{format_day_minute(int(start))}-{format_day_minute(int(end))}）"
            except (TypeError, ValueError):
                span = ""
            lines.append(f"刚完成：{row_name}{span}")
        return lines

    # ------------------------------------------------------------
    # 日结印象次日折叠（2026-11-11 Gemini 批 4.4b / 报告 §4.4）
    # ------------------------------------------------------------

    @staticmethod
    def _is_negative_impression(text: str) -> bool:
        """保守判定一条印象是否消极（冲突/冷落/失望等明确负向）。"""
        return any(marker in (text or "") for marker in _IMPRESSION_NEGATIVE_MARKERS)

    @classmethod
    def _fold_stale_impression_text(
        cls,
        text: str,
        *,
        today: str,
        interacted: bool,
        day_negative: Optional[Dict[str, bool]] = None,
    ) -> str:
        """把「日期早于今天 + 消极」的印象折叠到文本末尾并打 ``[已翻篇]``。

        - ``interacted=False``（今天还没有真实用户互动）→ 原样返回；
        - 条目日期缺失/坏值/等于今天/晚于今天 → 视为未过期，保持原样；
        - 消极判定优先用 ``day_negative``（日结 week_log 的当日 delta < 0，结构化
          数据）；该日无记录时才回退关键词保守判定；
        - 积极/中性旧印象 → 不折叠（只折叠旧日消极印象）；
        - 折叠块移到该文本末尾（不占【最近印象】首位），活跃条目保留；
        - 幂等：折叠后的标题不再匹配【最近印象】，重复调用不叠加标记。
        """
        if not interacted or not isinstance(text, str) or not text:
            return text
        active_title = IMPRESSION_TITLE_ACTIVE_DEFAULT
        folded_title = IMPRESSION_TITLE_FOLDED_DEFAULT
        folded_mark = IMPRESSION_FOLDED_MARK_DEFAULT
        flags = day_negative if isinstance(day_negative, dict) else {}
        out_lines: List[str] = []
        pending_folded: List[str] = []
        for line in text.splitlines():
            match = _IMPRESSION_LINE_PATTERN.match(line)
            if not match:
                out_lines.append(line)
                continue
            indent = match.group("indent") or ""
            body = (match.group("body") or "").strip()
            entries = [entry.strip() for entry in
                       _IMPRESSION_ENTRY_SPLIT_PATTERN.split(body) if entry.strip()]
            active_entries: List[str] = []
            folded_entries: List[str] = []
            for entry in entries:
                date_match = _IMPRESSION_DATE_PATTERN.match(entry)
                stale = bool(date_match and date_match.group("date") < today)
                if stale:
                    day = date_match.group("date")
                    if day in flags:
                        negative = bool(flags[day])
                    else:
                        negative = cls._is_negative_impression(entry)
                else:
                    negative = False
                if stale and negative:
                    folded_entries.append(entry)
                else:
                    active_entries.append(entry)
            if not folded_entries:
                out_lines.append(line)
                continue
            if active_entries:
                out_lines.append(f"{indent}{active_title}" + "；".join(active_entries))
            pending_folded.append(
                folded_title + "；".join(f"{folded_mark} {entry}" for entry in folded_entries))
        if not pending_folded:
            return text
        out_lines.extend(pending_folded)
        return "\n".join(out_lines)

    def _impression_day_flags(self) -> Dict[str, bool]:
        """日结 ``week_log`` 的「当日是否消极」映射（``date → delta < 0``）。

        只读 companion 好感状态（8 天滚动）；未装配/异常 → 空 dict，调用方回退
        关键词判定。不写任何数据。
        """
        try:
            companion = getattr(self._plugin, "_companion", None)
            affection = getattr(companion, "affection", None)
            getter = getattr(affection, "week_log", None)
            if not callable(getter):
                return {}
            rows = getter() or {}
        except Exception:
            return {}
        flags: Dict[str, bool] = {}
        if isinstance(rows, dict):
            for day, row in rows.items():
                if not isinstance(row, dict):
                    continue
                try:
                    flags[str(day)] = float(row.get("delta") or 0.0) < 0.0
                except (TypeError, ValueError):
                    continue
        return flags

    def _fold_stale_impressions_in_payload(
        self,
        payload: List[Dict[str, Any]],
        *,
        interacted: bool,
    ) -> List[Dict[str, Any]]:
        """只改 system 文本里的【最近印象】行，用户原话与状态数据不动。

        兼容 items 快照（``parts``）与旧 messages 投影（``content``）。companion
        的好感记忆行在 planner Hook 链中先于本服务写入 system 条目（宿主持久化
        Hook 按名称排序），本服务在注入前做最后一道口径归一；载荷里没有该行时
        原样返回（组件未装配/已折叠均零副作用）。
        """
        if not interacted or not isinstance(payload, list) or not payload:
            return payload
        try:
            today = self._tz_manager.get_now().strftime("%Y-%m-%d")
        except Exception:
            today = datetime.now().strftime("%Y-%m-%d")
        day_negative = self._impression_day_flags()
        changed = False
        out: List[Dict[str, Any]] = []
        for msg in payload:
            if not isinstance(msg, dict) or self._message_role(msg) != "system":
                out.append(msg)
                continue
            replacement = msg
            parts = msg.get("parts")
            if isinstance(parts, list):
                new_parts: List[Any] = []
                for part in parts:
                    if (isinstance(part, dict) and part.get("type") == "text"
                            and isinstance(part.get("text"), str)):
                        new_text = self._fold_stale_impression_text(
                            part["text"], today=today, interacted=True,
                            day_negative=day_negative)
                        if new_text != part["text"]:
                            changed = True
                            part = {**part, "text": new_text}
                    new_parts.append(part)
                if new_parts != parts:
                    replacement = {**msg, "parts": new_parts}
            else:
                content = msg.get("content")
                if isinstance(content, str):
                    new_text = self._fold_stale_impression_text(
                        content, today=today, interacted=True,
                        day_negative=day_negative)
                    if new_text != content:
                        changed = True
                        replacement = {**msg, "content": new_text}
                elif isinstance(content, list):
                    new_content: List[Any] = []
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            new_text = self._fold_stale_impression_text(
                                part["text"], today=today, interacted=True,
                                day_negative=day_negative)
                            if new_text != part["text"]:
                                changed = True
                                part = {**part, "text": new_text}
                        new_content.append(part)
                    if new_content != content:
                        replacement = {**msg, "content": new_content}
            out.append(replacement)
        return out if changed else payload

    # ------------------------------------------------------------
    # 对外公开：当前活动快照（供 @API 转发，跨插件可调）
    # ------------------------------------------------------------

    async def get_current_activity_snapshot(self, chat_id: str = "global") -> Dict[str, Any]:
        """返回当前活动快照（runtime episode/ReadModel 视图，与注入同口径）。

        PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-6 裁定）：
        不再走 GoalManager 今日/昨日合成时间窗读 schedule_items；runtime 视图未就绪
        （``[schedule].enabled=false`` 或 provider 未装配）→ 如实「暂不可用」
        （``error="runtime_not_ready"``），不读残留快照冒充当前活动（规则 5/6）。

        Returns:
            dict::

                {
                    "has_activity": bool,
                    "activity": None | {"name", "description", "goal_type", "time_window"},
                    "next_activities": [{"time": "HH:MM", "name": str}, ...],
                    "as_of": ISO8601 字符串,
                    "timezone": str,
                    "error": None | "runtime_not_ready",
                }
        """
        now = self._tz_manager.get_now()
        view = self._life_runtime_view()
        if view is None:
            return {
                "has_activity": False,
                "activity": None,
                "next_activities": [],
                "as_of": now.isoformat(timespec="seconds"),
                "timezone": self._tz_manager.timezone_str,
                "error": "runtime_not_ready",  # 运行态未就绪 = 暂不可用（如实，不编造）
            }
        current = view.get("current")
        activity_payload: Optional[Dict[str, Any]] = None
        if isinstance(current, dict) and str(current.get("name") or "").strip():
            window = ""
            try:
                window = (f"{format_day_minute(int(current.get('start_minute') or 0))}-"
                          f"{format_day_minute(int(current.get('end_minute') or 0))}")
            except (TypeError, ValueError):
                window = ""
            activity_payload = {
                "name": str(current.get("name") or "").strip(),
                "description": str(current.get("description") or "").strip(),
                "goal_type": str(current.get("goal_type") or "").strip(),
                "time_window": window,
            }
        future = self._life_override_from_view(view)["future"]
        return {
            "has_activity": activity_payload is not None,
            "activity": activity_payload,
            "next_activities": [{"time": t, "name": n} for t, n in future],
            "as_of": now.isoformat(timespec="seconds"),
            "timezone": self._tz_manager.timezone_str,
            "error": None,  # 成功路径显式给 None，让调用方能统一用 result.get("error") 判断
        }

    # ------------------------------------------------------------
    # 当前日程查询（runtime 视图；与注入同口径）
    # ------------------------------------------------------------

    async def _get_current_schedule(
        self,
        chat_id: Optional[str] = None,
    ) -> Tuple[Optional[str], Optional[str], List[Tuple[str, str]], Optional[str]]:
        """获取当前日程信息（runtime episode/ReadModel 视图，与注入同口径）。

        PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，UQ-6 裁定）：
        原「GoalManager 今日/昨日合成时间窗读 schedule_items + 15 分钟 LRU 缓存」
        编造路径已删除；runtime 视图未就绪 → 全空四元组（调用方按「暂不可用」处理，
        不读残留快照冒充当前活动）。

        Returns:
            ``(当前活动, 活动描述, 所有未来活动列表, 当前活动类型)``
        """
        del chat_id  # runtime 视图全局一致（会话维度不再影响取数）
        view = self._life_runtime_view()
        if view is None:
            return (None, None, [], None)
        override = self._life_override_from_view(view)
        return (override["name"], override["description"],
                list(override["future"]), override["goal_type"])
