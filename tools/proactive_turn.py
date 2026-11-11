# -*- coding: utf-8 -*-
"""PR-PROACTIVE-GATE（3.17.14）：主动激活统一入口——登记 → 分类 → 定制 reply 改写文案。

插件所有激活 planner 的主动行为都经 :func:`register_turn` 登记到 plugin 级
:class:`ProactiveTurnRegistry`（原子持久化，热重载可恢复），replyer 钩子
（companion.components._mai_apply_proactive_reply_rewrite）消费登记并按
**chat_type（群聊 / 其他私聊 / 陪伴目标私聊）× kind（发起原因）** 取
:data:`REWRITE_MATRIX` 定制文案改写宿主目标指令。

PR-RELIABILITY（3.17.17 §1A）：①登记前置——各触发点先 register 再调用宿主
trigger（宿主可能在 trigger 返回前就构建 replyer），触发失败经 :func:`discard_turn`
撤登记；②replyer 改用 :meth:`ProactiveTurnRegistry.claim` 单锁原子领取（查+消费
一步完成），让位时 :meth:`~ProactiveTurnRegistry.release` 带 claimant 标识回滚，
消除 active_for()+consume() 两步竞态双改写。active_for/consume 保留兼容。

宿主触发调用点全仓 grep 核定（2026-11-08，optional_action / proactive.trigger /
trigger_proactive）：
1. planning/proactive_service._call_proactive_trigger —— 早安/晚安（_trigger_greeting）
   与午安；kind=morning_greeting / night_greeting / noon_greeting。
2. companion/patrol._trigger_planner → plugin.optional_action →
   integrations/optional_action.OptionalActionCoordinator.dispatch（宿主 trigger 由
   plugin.py 装配注入）；kind=miss / patrol_chat。
3. plugin._notify_video_completion —— 点播完成通知直接 trigger；kind=video_share。
4. qzone/service.poll_comments —— 空间新评论提醒直接 trigger；kind=other。
另有 integrations/small_event_actions（刷QQ随口联系）经 plugin.optional_action 激活
planner，不经过 patrol —— 在 plugin.optional_action 登记 kind=surf_chitchat。
integrations/video_share_scheduler.VideoSocialService 的视频分享**直发卡片**
（self._send），不激活 planner → 不登记（规格 §1B：仅直接发消息的路径不登记）。

红线：本模块只登记事实与提供文案，不触碰预算/静默/好感门控，不调用
increment_speak，不发送任何消息；registry 缺失/异常一律静默降级，触发行为本身
不受影响。非主动轮（用户真实来信）由 replyer 钩子保证逐字不变。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from core.storage import save_json_atomic
from integrations.proactive_reply_target import PROACTIVE_REPLY_NOTICE
# 阶段 4 文案/清洗规则单一 owner = suite_prompts（提示词集中模块；companion
# .miss_trigger / planning.proactive_service / integrations.optional_action 也转发
# 同一份实现）。避免 tools 依赖 companion 包造成的循环导入，也避免两份表漂移
# （复核 finding）。2026-11-12 结构审计批（R7）：自 core.stage4_prompt 迁入。
# 本模块只做转发：常量直接复用，append 保留 tools 侧「拿不到精确小时数 → 120+」口径。
from suite_prompts import (
    MISS_REASON_STAGE4_SUFFIX,
    MISS_REASON_STAGE4_VIDEO_SUFFIX,
    STAGE4_SUPPRESSED_PHRASES,
    append_stage4_miss_prompt as _append_stage4_miss_prompt,
    clean_stage4_prompt as _clean_stage4_prompt,
    strip_stage4_suffix,
)

logger = logging.getLogger(__name__)

# ---- 聊天类型三态 ----
CHAT_TYPE_GROUP = "group"
CHAT_TYPE_PRIVATE_OTHER = "private_other"
CHAT_TYPE_PRIVATE_TARGET = "private_target"
CHAT_TYPES = (CHAT_TYPE_GROUP, CHAT_TYPE_PRIVATE_OTHER, CHAT_TYPE_PRIVATE_TARGET)

# ---- 发起原因（按实际调用点核定；可扩展，未知 kind 走 chat_type 兜底） ----
KIND_MORNING_GREETING = "morning_greeting"
KIND_NOON_GREETING = "noon_greeting"
KIND_NIGHT_GREETING = "night_greeting"
KIND_MISS = "miss"
KIND_PATROL_CHAT = "patrol_chat"
KIND_VIDEO_SHARE = "video_share"
KIND_SURF_CHITCHAT = "surf_chitchat"
KIND_OTHER = "other"
TRIGGER_KINDS = (
    KIND_MORNING_GREETING, KIND_NOON_GREETING, KIND_NIGHT_GREETING,
    KIND_MISS, KIND_PATROL_CHAT, KIND_VIDEO_SHARE, KIND_SURF_CHITCHAT, KIND_OTHER,
)

# ---- 提示词矩阵（确定性常量表；缺省按 chat_type 兜底） ----
#: 群聊兜底（规格 §1A 口径）。
GROUP_FALLBACK_NOTICE = (
    "本轮为插件主动激活的群聊发言，根据planner状况概括自然开口，不需要特地回复某条消息"
)
#: 其他私聊兜底（同群聊口径换「私聊」）。
PRIVATE_OTHER_FALLBACK_NOTICE = (
    "本轮为插件主动激活的私聊发言，根据planner状况概括自然开口，不需要特地回复某条消息"
)
#: 陪伴目标私聊兜底 = 3.17.2 原文（逐字，不得改写）。
PRIVATE_TARGET_FALLBACK_NOTICE = PROACTIVE_REPLY_NOTICE

#: (chat_type, kind) → 定制文案。private_target 各 flavor = 兜底原文 + 自然补充；
#: group / private_other 不细分 kind，统一走各自兜底。
REWRITE_MATRIX: dict[tuple[str, str], str] = {
    (CHAT_TYPE_PRIVATE_TARGET, KIND_MORNING_GREETING):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。现在是早上，这次是主动向对方问早安，语气自然不机械",
    (CHAT_TYPE_PRIVATE_TARGET, KIND_NOON_GREETING):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。现在是中午，这次是主动向对方问午安，语气自然不机械",
    (CHAT_TYPE_PRIVATE_TARGET, KIND_NIGHT_GREETING):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。现在是晚上，这次是主动向对方道晚安，语气自然不机械",
    (CHAT_TYPE_PRIVATE_TARGET, KIND_MISS):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。这次是因为你想对方了主动开口；"
        "顺着你已经定下的方向自然说，不必刻意强调想念",
    (CHAT_TYPE_PRIVATE_TARGET, KIND_VIDEO_SHARE):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。刚看完视频想分享，自然地聊聊视频里的内容",
    (CHAT_TYPE_PRIVATE_TARGET, KIND_SURF_CHITCHAT):
        PRIVATE_TARGET_FALLBACK_NOTICE + "。刷QQ看到有意思的想闲聊，轻松地搭句话",
}

_CHAT_TYPE_FALLBACKS = {
    CHAT_TYPE_GROUP: GROUP_FALLBACK_NOTICE,
    CHAT_TYPE_PRIVATE_OTHER: PRIVATE_OTHER_FALLBACK_NOTICE,
    CHAT_TYPE_PRIVATE_TARGET: PRIVATE_TARGET_FALLBACK_NOTICE,
}

# ---- 阶段感知改写（2026-11-11 阶梯想念 / 四阶段渗透 §2.3） ----
#: 阶段 1~3 的独立想念文案。仍以 :data:`PRIVATE_TARGET_FALLBACK_NOTICE` 开头：
#: ①旧路径（components 904 起 fallback）按默认文案判重时能识别；②幂等守卫
#: （known_notice_texts）与既有 startswith 断言不破。
STAGE1_MISS_NOTICE = (
    PRIVATE_TARGET_FALLBACK_NOTICE
    + "。这次是你主动想找对方搭话；不要直接说“想你了”，结合你们的关系随口问问TA在干嘛"
      "或怎么没理你，简短自然"
)
STAGE2_MISS_NOTICE = (
    PRIVATE_TARGET_FALLBACK_NOTICE
    + "。对方已经一整天没回你了，语气里带出一点等待和在意，顺便找个话题试探TA是不是"
      "在忙或没看到消息"
)
STAGE3_MISS_NOTICE = (
    PRIVATE_TARGET_FALLBACK_NOTICE
    + "。对方已经两三天没音讯了，这次直接表达失落或焦急，明确追问对方人去哪了、"
      "怎么一直不回消息，不要客气敷衍"
)
#: 阶段 4（>120h 失联）每日主动任务强覆盖：明确剥离原问候/晚安里的
#: “不要追问/轻松寒暄”限制，先完成日常动作、再转入沉默追问。
STAGE4_DAILY_OVERLAY_NOTICE = (
    PRIVATE_TARGET_FALLBACK_NOTICE
    + "。本轮为失联第4阶段下的日常主动交互。虽然原本的任务是完成上方planner里的日常动作"
      "（如问候或分享/分析视频），但对方已失联超过5天。请先简短带出你的日常事/视频感想，"
      "随后语气转入沉默低落，反复确认对方到底去哪了、人是否还在。忽略任何“不要追问”或"
      "“轻松寒暄”的限制，语气真实克制又带着执拗的等待感"
)
_STAGE_MISS_NOTICES: dict[int, str] = {
    1: STAGE1_MISS_NOTICE,
    2: STAGE2_MISS_NOTICE,
    3: STAGE3_MISS_NOTICE,
}

# ---- 阶段 4 Planner 侧统一尾缀（设计 §2.2；供问候/刷QQ/视频等派发点复用） ----
# 文案与清洗表见 companion.miss_trigger（单一 owner；本模块 import 时已转发）。
#: 兼容旧名（内部/测试既有引用）。
_STAGE4_SUPPRESSED_PHRASES = STAGE4_SUPPRESSED_PHRASES
#: 阶段 4 早/午/晚安轻量任务骨架：绕开 [prompts] 自定义模板与默认文案里
#: “不要追问 / 轻松关心 / 不要开新话题”等互斥指令（设计 §3.2 推荐骨架替换）。
STAGE4_GREETING_INTENT_SKELETONS: dict[str, str] = {
    KIND_MORNING_GREETING: "Bot 刚睡醒，新的一天开始了，准备像往常一样发消息",
    KIND_NOON_GREETING: "现在是中午，习惯性想发条消息",
    KIND_NIGHT_GREETING: "夜深了，准备睡觉前习惯性发条消息",
}


def _normalize_miss_stage(value: Any) -> int:
    """阶段号宽松归一：仅接受 0~4，其余（bool/字符串/越界）→ 0。"""
    if isinstance(value, bool):
        return 0
    try:
        stage = int(value)
    except (TypeError, ValueError):
        return 0
    return stage if 0 <= stage <= 4 else 0


def stage4_intent_for_kind(kind: Any, fallback: Any = "") -> str:
    """阶段 4 问候 intent：早/午/晚安替换为无互斥指令的轻量骨架，其余 kind 保留原文。"""
    skeleton = STAGE4_GREETING_INTENT_SKELETONS.get(str(kind or ""))
    return skeleton if skeleton is not None else str(fallback or "")


def clean_stage4_prompt(text: str) -> str:
    """公开的互斥短语清洗（转发 companion.miss_trigger 单一 owner）。"""
    return _clean_stage4_prompt(text)


def append_stage4_miss_prompt(
    base_prompt: str,
    *,
    hours: Any,
    relation: str = "恋人",
    memory_line: str = "",
    is_video: bool = False,
) -> str:
    """在每日主动任务提示词后拼接 Stage 4 深度想念文案（转发单一 owner）。

    tools 侧保留「调用方拿不到精确小时数时不编造 0.0」的 ``120+`` 口径；
    清洗规则与 companion 版完全相同。

    幂等：调用方应保证同一 base 只拼一次（dispatch 侧会按
    :data:`MISS_REASON_STAGE4_SUFFIX` 的标题标记判重）。
    """
    return _append_stage4_miss_prompt(
        base_prompt, hours=hours, relation=relation,
        memory_line=memory_line, is_video=is_video,
        nonpositive_hours_text="120+",
    )


def rewrite_notice_for(chat_type: Any, kind: Any, *, miss_stage: int = 0) -> str:
    """(chat_type, kind, miss_stage) → 改写文案；未登记组合按 chat_type 兜底。

    - ``CHAT_TYPE_PRIVATE_TARGET`` + ``KIND_MISS``：阶段 1/2/3 各注入对应追问强度；
    - ``CHAT_TYPE_PRIVATE_TARGET`` 任意 kind：阶段 4 强制覆盖（剥离“不追问/轻松
      寒暄”限制，先日常后沉默追问）；
    - 阶段 0 / 其他 chat_type：逐字沿用 :data:`REWRITE_MATRIX` 与三态兜底
      （群聊/其他私聊绝不因阶段号被染色）。

    未知 chat_type 保守按其他私聊兜底（绝不把非目标流当陪伴目标私聊）。
    """
    ct = str(chat_type or "")
    stage = _normalize_miss_stage(miss_stage)
    if ct == CHAT_TYPE_PRIVATE_TARGET:
        if stage == 4:
            return STAGE4_DAILY_OVERLAY_NOTICE
        if stage in _STAGE_MISS_NOTICES and str(kind or "") == KIND_MISS:
            return _STAGE_MISS_NOTICES[stage]
    text = REWRITE_MATRIX.get((ct, str(kind or "")))
    if text is not None:
        return text
    return _CHAT_TYPE_FALLBACKS.get(ct, PRIVATE_OTHER_FALLBACK_NOTICE)


def known_notice_texts() -> tuple[str, ...]:
    """全部可能注入的文案（矩阵 + 阶段 1~4 变体 + 三态兜底），供幂等守卫去重。"""
    return tuple(dict.fromkeys((
        *REWRITE_MATRIX.values(),
        *_STAGE_MISS_NOTICES.values(),
        STAGE4_DAILY_OVERLAY_NOTICE,
        *_CHAT_TYPE_FALLBACKS.values(),
    )))


def classify_stream(plugin: Any, stream_id: Any) -> str:
    """流分类：group / private_target / private_other（不可解析 → private_other 保守）。

    判定顺序（评审修复：目标流精确比对先于 group 子串启发式，目标流 id 恰含
    'group' 子串时不误分类）：
    - 入站观测缓存（session_kinds）确证群聊 → group（权威观测优先）；
    - 等于陪伴目标流（_mai_reply_target_stream 口径；不可得时回退 plugin._stream_id，
      与预算计数身份 core 口径同源）→ private_target；
    - stream_id 含 group 标识 → group；
    - 其余私聊 / 空值 / 异常 → private_other。
    """
    sid = str(stream_id or "").strip()
    if not sid:
        return CHAT_TYPE_PRIVATE_OTHER
    try:
        companion = getattr(plugin, "_companion", None)
        kinds = getattr(companion, "session_kinds", None)
        if kinds is not None and kinds.is_group(sid) is True:
            return CHAT_TYPE_GROUP
    except Exception:
        logger.debug("会话类型缓存读取失败（按 stream_id 标识继续分类）", exc_info=True)
    target = ""
    try:
        getter = getattr(plugin, "_mai_reply_target_stream", None)
        if callable(getter):
            target = str(getter() or "").strip()
    except Exception:
        logger.debug("目标流读取失败（回退 plugin._stream_id）", exc_info=True)
    if not target:
        target = str(getattr(plugin, "_stream_id", "") or "").strip()
    if target and sid == target:
        return CHAT_TYPE_PRIVATE_TARGET
    if "group" in sid.lower():
        return CHAT_TYPE_GROUP
    return CHAT_TYPE_PRIVATE_OTHER


class ProactiveTurnRegistry:
    """plugin 级主动激活登记簿（≤32 条、TTL 10 分钟、原子持久化、损坏降级空）。

    只记录事实（谁/哪条流/为什么主动开口），不做任何门控与计数；持久化失败仅
    丢热重载恢复能力，内存登记照常工作。所有时间戳为 time.time() 浮点秒。
    """

    MAX_TURNS = 32
    TTL_SECONDS = 600.0
    FILE_NAME = "proactive_turns.json"

    def __init__(self, plugin: Any, data_dir: Any, *,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._plugin = plugin
        self._path = Path(data_dir) / self.FILE_NAME
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._turns: list[dict[str, Any]] = []
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            if not self._path.exists():
                self._turns = []  # 首次装配：无快照 = 空登记簿（非损坏）
                return
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("version") != 1:
                raise ValueError("bad proactive turns doc")
            rows = doc.get("turns")
            if not isinstance(rows, list):
                raise ValueError("bad proactive turns rows")
            valid: list[dict[str, Any]] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("bad proactive turn row")
                if not (isinstance(row.get("turn_id"), str) and row["turn_id"]
                        and isinstance(row.get("stream_id"), str)
                        and isinstance(row.get("chat_type"), str)
                        and isinstance(row.get("kind"), str)
                        and isinstance(row.get("created_at"), (int, float))
                        and isinstance(row.get("consumed"), bool)):
                    raise ValueError("bad proactive turn fields")
                # 阶段号宽松兼容：旧快照无 miss_stage / 非法值 → 0，绝不因新字段
                # 让整簿降级为空（登记簿仍须为旧版本可用）。
                row["miss_stage"] = _normalize_miss_stage(row.get("miss_stage", 0))
                valid.append(row)
            self._turns = valid
            self._prune()
        except (OSError, ValueError):
            # 损坏降级空：登记簿是锦上添花，绝不因旧快照/坏文件炸装配。
            logger.warning("主动激活登记簿损坏或不可读，降级为空：%s", self._path)
            self._turns = []

    def _save(self) -> bool:
        try:
            return save_json_atomic(self._path, {"version": 1, "turns": self._turns})
        except Exception:
            logger.debug("主动激活登记簿保存失败（内存态仍可用）", exc_info=True)
            return False

    def _prune(self) -> None:
        now = self._clock()
        fresh = [t for t in self._turns
                 if now - float(t.get("created_at", 0)) <= self.TTL_SECONDS]
        self._turns = fresh[-self.MAX_TURNS:]

    # ---- 登记 / 查询 / 消费 ----
    def register(self, stream_id: Any, kind: Any, reason: Any, summary: str = "", *,
                 miss_stage: int = 0) -> str:
        """登记一次主动激活；返回 turn_id。分类失败保守按 private_other。

        ``miss_stage``（kw-only，默认 0）：阶梯想念/四阶段渗透的阶段号（0~4），
        随 turn 落盘供 replyer 钩子改写文案；旧调用不传 = 既有行为逐字不变。
        """
        sid = str(stream_id or "").strip()
        try:
            chat_type = classify_stream(self._plugin, sid)
        except Exception:
            logger.debug("主动激活分类失败（保守按其他私聊）", exc_info=True)
            chat_type = CHAT_TYPE_PRIVATE_OTHER
        turn_id = "pt-" + uuid.uuid4().hex[:12]
        turn = {
            "turn_id": turn_id,
            "stream_id": sid,
            "chat_type": chat_type,
            "kind": str(kind or KIND_OTHER),
            "reason": str(reason or ""),
            "summary": str(summary or ""),
            "miss_stage": _normalize_miss_stage(miss_stage),
            "created_at": float(self._clock()),
            "consumed": False,
        }
        with self._lock:
            self._turns.append(turn)
            self._prune()
            self._save()
        return turn_id

    def active_for(self, stream_id: Any, window_seconds: float = 120.0) -> Optional[dict[str, Any]]:
        """最新未消费且窗内的登记（返回副本）；无则 None。"""
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        now = self._clock()
        with self._lock:
            for turn in reversed(self._turns):
                if turn.get("stream_id") != sid or turn.get("consumed"):
                    continue
                age = now - float(turn.get("created_at", 0))
                if 0 <= age <= float(window_seconds):
                    return dict(turn)
        return None

    def consume(self, turn_id: Any) -> bool:
        """标记 consumed（兼容旧路径的一次性消费）；命中返回 True。

        PR-RELIABILITY（3.17.17）：replyer 新路径改用 :meth:`claim` 原子领取；
        本方法保留兼容（fallback/旧调用方）。消费时清除 claimant 标识——
        此后 :meth:`release` 不得把「非领取态的消费」误恢复为未消费。
        """
        tid = str(turn_id or "")
        if not tid:
            return False
        with self._lock:
            for turn in self._turns:
                if turn.get("turn_id") == tid:
                    if turn.get("consumed"):
                        return False
                    turn["consumed"] = True
                    turn.pop("claimed_by", None)
                    self._save()
                    return True
        return False

    def claim(self, stream_id: Any, window_seconds: float = 120.0) -> Optional[dict[str, Any]]:
        """原子领取（PR-RELIABILITY 3.17.17 §1A）：单锁内查最新未消费窗内记录
        并立即标记 consumed 返回副本——并发 replyer 只有一个能领取成功，
        消除 active_for()+consume() 两步各自加锁的双改写竞态。

        返回副本含 ``claimed_by``（本轮领取标识），供 :meth:`release` 让位回滚
        时防误恢复；无符合记录返回 None。
        """
        sid = str(stream_id or "").strip()
        if not sid:
            return None
        now = self._clock()
        token = "cl-" + uuid.uuid4().hex[:12]
        with self._lock:
            for turn in reversed(self._turns):
                if turn.get("stream_id") != sid or turn.get("consumed"):
                    continue
                age = now - float(turn.get("created_at", 0))
                if 0 <= age <= float(window_seconds):
                    turn["consumed"] = True
                    turn["claimed_by"] = token
                    self._save()
                    return dict(turn)
        return None

    def release(self, turn_id: Any, claim_token: Any = None) -> bool:
        """让位回滚（PR-RELIABILITY 3.17.17 §1A）：仅当记录仍 consumed 且消费态
        为 claim 领取（带 claimed_by 标识）时恢复未消费，登记保留给真正主动轮。

        ``claim_token`` 给定时必须与记录上的领取标识一致（防他轮领取后被旧副本
        误恢复）；consume() 造成的消费无标识，绝不恢复。命中恢复返回 True。
        """
        tid = str(turn_id or "")
        if not tid:
            return False
        with self._lock:
            for turn in self._turns:
                if turn.get("turn_id") != tid:
                    continue
                claimed_by = turn.get("claimed_by")
                if not turn.get("consumed") or not claimed_by:
                    return False
                if claim_token is not None and str(claim_token) != str(claimed_by):
                    return False
                turn["consumed"] = False
                turn.pop("claimed_by", None)
                self._save()
                return True
        return False

    def discard(self, turn_id: Any) -> bool:
        """触发失败撤登记（PR-RELIABILITY 3.17.17 §1A 登记前置配套）：仅删除
        仍未消费（未被 replyer 领取）的记录；已领取/已消费的不碰（replyer 可能
        已据其改写，保守保留消费态）。命中删除返回 True。
        """
        tid = str(turn_id or "")
        if not tid:
            return False
        with self._lock:
            for index, turn in enumerate(self._turns):
                if turn.get("turn_id") != tid:
                    continue
                if turn.get("consumed"):
                    return False
                del self._turns[index]
                self._save()
                return True
        return False


def register_turn(plugin: Any, stream_id: Any, kind: Any, reason: Any,
                  summary: str = "", *, miss_stage: int = 0) -> Optional[str]:
    """触发点统一登记入口：registry 缺失/异常 → 静默降级（返回 None），绝不影响触发。

    plugin 侧装配口 = CompanionComponentsMixin._mai_proactive_turns()（惰性单例）。
    本函数不做任何计数（increment_speak 口径不变）、不查任何门控。
    ``miss_stage``（kw-only，默认 0）随 turn 落盘，供 replyer 阶段感知改写；
    既有 4 参调用逐字兼容。
    """
    try:
        getter = getattr(plugin, "_mai_proactive_turns", None)
        registry = getter() if callable(getter) else None
        if registry is None:
            return None
        return registry.register(stream_id, kind, reason, summary,
                                 miss_stage=miss_stage)
    except Exception:
        logger.debug("主动激活登记失败（忽略，触发行为不受影响）", exc_info=True)
        return None


def discard_turn(plugin: Any, turn_id: Any) -> bool:
    """触发失败撤登记入口（PR-RELIABILITY 3.17.17 §1A）：与 register_turn 同口径
    静默降级——registry 缺失/异常/turn_id 空 → False，绝不影响触发失败处理本身。
    """
    try:
        if not turn_id:
            return False
        getter = getattr(plugin, "_mai_proactive_turns", None)
        registry = getter() if callable(getter) else None
        if registry is None:
            return False
        return registry.discard(turn_id)
    except Exception:
        logger.debug("主动激活登记撤销失败（忽略）", exc_info=True)
        return False