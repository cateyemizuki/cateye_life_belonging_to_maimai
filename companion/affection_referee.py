"""好感度 LLM 裁判（会话采样 + 关系日结；MAINTENANCE_PLAN §5.4 两节 / §5.5 / §6.3-6）。

结构镜像 ``emotion/referee.py``：

- **会话采样裁判** :func:`evaluate_conversation_delta`：最近 ~10 条对话摘要 + 双方称呼与人设
  + 关系档位描述 + 当前情绪低分辨率描述 → ``(event, delta ∈ [-2, +2.5], tag)``；
- **关系日结裁判** :func:`run_daily_review`：当日事件日志 + 互动统计 + 最近印象 + 档位描述
  + 人设 → ``delta_score ∈ [-2, +2] + 0~3 条印象 + tone + reason``；
- 解析一律宽松（整段 → ```json 围栏 → 首个 {...} 块），代码层二次限幅（LLM 数值失真兜底）；
- LLM 全部经 ``core/llm_utils.LLMService.generate`` 并带 ``event=`` 标识
  （``affection_review`` / ``affection_daily_review``），任务名用 ``[affection] review_task_name``；
- **失败哲学**：请求失败或输出垃圾 → 返回 None，调用方静默跳过本轮（不崩溃、不阻塞、不重试轰炸）。

**LLM 隔离铁律（§6.3-6）**：两个 prompt 都只见关系档位描述（名称 + 文案）与低分辨率情绪
描述，**绝不见 score / baseline 等绝对分值**；prompt 里显式声明「你看不到任何好感度数值」。

编排器 :class:`AffectionReviewer`（由 ``CompanionServices`` 构造）：

- 会话采样触发：``note_target_message()`` 在 chat Hook 的 chat_round 记账点旁调用，
  做「每 N 轮 + 每日 ≤ review_max_per_day」双频控；LLM 请求放后台任务，绝不阻塞 Hook；
- 关系日结触发：``on_sleep_started()`` 订阅 ``schedule.sleep_started``（M0 总线，
  服务束 ``start()`` 注册 / ``stop()`` 注销）；日结晚于情绪睡眠大修的保证机制见该方法注释；
- 结果应用：delta 走 ``affection_events.record_rule_event`` 既有入账入口
  （``llm_review`` / ``daily_review`` kind，独立于 kind 上限、受 §5.5 日累计限幅），
  随后追加 impressions（≤3 条/次、滚动 10 条、7 天过期）→ 写 tone → 清空当日 event_log。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .affection_events import record_rule_event

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数值边界（代码层二次限幅；§6.3-2）
# ---------------------------------------------------------------------------

#: 会话采样裁判 delta 区间（§5.4 非对称：[-2, +2.5]）
REVIEW_DELTA_MIN = -2.0
REVIEW_DELTA_MAX = 2.5
#: 关系日结 delta 区间（§5.4：[-2, +2]）
DAILY_DELTA_MIN = -2.0
DAILY_DELTA_MAX = 2.0
#: tag 白名单（§5.4）
REVIEW_TAGS = ("normal", "conflict", "apology", "sweet")
#: 会话采样裁判可见的最近消息条数（§5.4：最近 ~10 条对话摘要）
RECENT_MESSAGE_LIMIT = 10
#: 单次日结最多采纳的裁判印象条数（§5.4：0~3 条）
DAILY_IMPRESSION_LIMIT = 3
#: 日结在 sleep_started 之后等待的秒数（见 AffectionReviewer.on_sleep_started）
DAILY_REVIEW_GRACE_SECONDS = 90.0
#: event_log 送入 prompt 的行数 / 字符数上限（防 prompt 膨胀）
EVENT_LOG_LINE_LIMIT = 40
EVENT_LOG_CHAR_LIMIT = 1200

# ---------------------------------------------------------------------------
# Prompt（隔离要点：只给档位描述与低分辨率情绪描述，不给任何绝对分值）
# ---------------------------------------------------------------------------

CONVERSATION_REVIEW_PROMPT = """你是 AI 角色「{lover_name}」与{target_label}之间关系的会话观察裁判。
你不参与对话，只看下面给出的材料，给这段相处评估一个**有界的好感度增量**。
你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。

【角色人设（判断真心程度的依据）】
{persona}

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【角色近期的情绪底色（低分辨率描述，仅供理解语气）】
{mood_text}

【最近对方发来的消息（按时间从旧到新，只有对方一侧）】
{recent_messages}

【判定标准】
1. event：用不超过 30 字概括这段相处里最值得记住的一件事；没有就写「日常闲聊」；
2. delta：好感度增量，范围 [-2, +2.5]——
   - 阿谀奉承、无意义刷屏、单方面索取：不给正分（0 或负）；
   - 真诚的关心、共同完成一件事、化解了一次矛盾：才给正分；
   - 明显的争吵、冒犯、被冷落：给负分；
   - 平淡的日常闲聊：接近 0；
3. tag 只能是 normal / conflict / apology / sweet 之一；
4. 只输出 JSON：{{"event": "...", "delta": 0.0, "tag": "normal"}}"""

DAILY_REVIEW_PROMPT = """你是 AI 角色「{lover_name}」与{target_label}之间关系的日结裁判。
你看不到任何好感度数值，也看不到分数与基线，只看到关系所处的阶段描述——不要猜测具体数值。
任务：根据 {date} 这一天的相处记录，给这一天对关系的影响做一个有界的总结。

【角色人设】
{persona}

【双方关系所处的阶段】
{tier_label}：{tier_desc}

【今日互动统计】
{stats}

【今日事件日志（时间、类型、摘要、当日入账增量）】
{event_log}

【最近积累的相处印象（旧→新）】
{impressions}

【判定标准】
1. delta_score：这一天对关系的影响，范围 [-2, +2]——
   - 真诚的关心、共同完成一件事、化解矛盾：正分；
   - 阿谀奉承、无意义刷屏：不给正分；
   - 争吵、冒犯、被冷落、整天没有互动：负分；
   - 平淡无事：接近 0；
2. impressions：0~3 条，每条一句话（不超过 40 字）记下今天值得记住的事；
   只写事实与感受，不写分数、不写评价性标签、不编造没有发生的事；
3. tone：一句话概括今天的关系温度（不超过 30 字）；
4. reason：必须直接引用上面事件日志或对方原话的片段作为依据，禁止脑补不存在的事；
5. 只输出 JSON：{{"delta_score": 0.0, "impressions": ["..."], "tone": "...", "reason": "..."}}"""


# ---------------------------------------------------------------------------
# 通用工具（宽松解析 / 限幅 / 文本清洗；镜像 emotion/referee.py）
# ---------------------------------------------------------------------------


def _parse_json_loose(text: Any) -> Optional[Dict[str, Any]]:
    """宽松 JSON 解析：整段 → ```json 围栏 → 首个 {...} 块。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates = [raw]
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", raw, re.DOTALL)
    if fence:
        candidates.insert(0, fence.group(1))
    brace = re.search(r"\{.*\}", raw, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _clip(value: float, lo: float, hi: float) -> float:
    """区间裁剪（镜像 emotion/vad.py:_clip）。"""
    return max(lo, min(hi, float(value)))


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clean_text(value: Any, limit: int) -> str:
    """单行化 + 截断（prompt / 状态里的自由度文本一律经此清洗）。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:max(0, int(limit))]


def _format_messages(messages: Sequence[Any]) -> str:
    lines = [f"- {_clean_text(item, 80)}" for item in list(messages or [])[-RECENT_MESSAGE_LIMIT:]]
    lines = [line for line in lines if line != "- "]
    return "\n".join(lines) or "（最近没有对方的消息记录）"


def _format_event_log(entries: Sequence[Any]) -> str:
    lines: List[str] = []
    for entry in list(entries or [])[-EVENT_LOG_LINE_LIMIT:]:
        if not isinstance(entry, dict):
            continue
        ts = str(entry.get("ts") or "")
        clock = ts[11:16] if len(ts) >= 16 else (ts or "--:--")
        kind = _clean_text(entry.get("kind"), 24)
        desc = _clean_text(entry.get("desc"), 60)
        delta = _to_float(entry.get("delta"), 0.0)
        lines.append(f"[{clock}] {kind}·{desc}（Δ{delta:+.2f}）")
    text = "\n".join(lines)
    if len(text) > EVENT_LOG_CHAR_LIMIT:
        text = text[-EVENT_LOG_CHAR_LIMIT:]
    return text or "（今天没有记录到事件）"


#: 互动统计字段 → (中文标签, 单位)；拿不到的字段由 _format_stats 省略
_STAT_FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("rounds", "今日对方消息", " 轮"),
    ("user_initiated", "对方沉默后主动开口", " 次"),
    ("bot_speaks", "bot 主动发言", " 次"),
    ("silence_hours", "最近一次对方消息距今", " 小时"),
    ("commitments_kept", "约定兑现", " 次"),
    ("commitments_broken", "约定违约", " 次"),
)


def _format_stats(stats: Any) -> str:
    """互动统计文本化：值为 None / 缺键的行省略（拿不到的不编造）。"""
    data = stats if isinstance(stats, dict) else {}
    lines: List[str] = []
    for key, label, unit in _STAT_FIELDS:
        value = data.get(key)
        if value is None:
            continue
        try:
            lines.append(f"{label}：{float(value):.1f}{unit}" if key == "silence_hours"
                         else f"{label}：{_to_int(value)}{unit}")
        except (TypeError, ValueError):
            continue
    return "\n".join(lines) or "（今日没有可用的互动统计）"


def _format_impressions(entries: Sequence[Any]) -> str:
    lines: List[str] = []
    for entry in list(entries or [])[-10:]:
        if not isinstance(entry, dict):
            continue
        text = _clean_text(entry.get("text"), 80)
        if text:
            lines.append(f"{str(entry.get('date') or '')} {text}".strip())
    return "\n".join(lines) or "（还没有积累印象）"


# ---------------------------------------------------------------------------
# 裁判 1：会话采样（§5.4-1）
# ---------------------------------------------------------------------------


async def evaluate_conversation_delta(
    llm: Any,
    *,
    recent_messages: Sequence[Any],
    lover_name: str = "麦麦",
    target_label: str = "重要的人",
    persona: str = "",
    tier_label: str = "",
    tier_desc: str = "",
    mood_text: str = "",
    task_name: str = "utils",
) -> Optional[Tuple[str, float, str]]:
    """会话采样裁判：返回 ``(event ≤30 字, delta ∈ [-2, +2.5], tag)``；失败/垃圾输出返回 None。

    prompt 只见档位描述与低分辨率情绪描述（LLM 隔离铁律），返回的 delta 经代码层二次限幅。
    """
    if llm is None:
        return None
    prompt = CONVERSATION_REVIEW_PROMPT.format(
        lover_name=str(lover_name or "麦麦"),
        target_label=str(target_label or "重要的人"),
        persona=str(persona or "（暂无人设）")[:800],
        tier_label=str(tier_label or "（未知阶段）"),
        tier_desc=str(tier_desc or ""),
        mood_text=str(mood_text or "（情绪状态未知）"),
        recent_messages=_format_messages(recent_messages),
    )
    try:
        response = await llm.generate(prompt, task_name=task_name, temperature=0.3,
                                      max_tokens=300, event="affection_review")
    except Exception as exc:  # 宿主异常一律静默（失败哲学）
        logger.debug("会话采样裁判请求失败（跳过本轮）：%s", exc)
        return None
    data = _parse_json_loose(response)
    if data is None or "delta" not in data:
        return None
    event = _clean_text(data.get("event"), 30) or "日常闲聊"
    tag = _clean_text(data.get("tag"), 16).lower()
    if tag not in REVIEW_TAGS:
        tag = "normal"
    delta = _clip(_to_float(data.get("delta"), 0.0), REVIEW_DELTA_MIN, REVIEW_DELTA_MAX)
    return event, delta, tag


# ---------------------------------------------------------------------------
# 裁判 2：关系日结（§5.4-2）
# ---------------------------------------------------------------------------


async def run_daily_review(
    llm: Any,
    *,
    event_log: Sequence[Any] = (),
    stats: Any = None,
    impressions: Sequence[Any] = (),
    date: str = "",
    lover_name: str = "麦麦",
    target_label: str = "重要的人",
    persona: str = "",
    tier_label: str = "",
    tier_desc: str = "",
    task_name: str = "utils",
) -> Optional[Dict[str, Any]]:
    """关系日结裁判：返回 ``{"delta_score", "impressions", "tone", "reason"}``；失败返回 None。

    - ``delta_score`` 代码层二次限幅到 [-2, +2]；
    - ``impressions`` 代码层裁剪：≤3 条、每条 ≤80 字、由代码打上被日结日期；
    - prompt 只见档位描述（LLM 隔离铁律），不见 score / baseline。
    """
    if llm is None:
        return None
    day = str(date or datetime.now().strftime("%Y-%m-%d"))
    prompt = DAILY_REVIEW_PROMPT.format(
        lover_name=str(lover_name or "麦麦"),
        target_label=str(target_label or "重要的人"),
        date=day,
        persona=str(persona or "（暂无人设）")[:800],
        tier_label=str(tier_label or "（未知阶段）"),
        tier_desc=str(tier_desc or ""),
        stats=_format_stats(stats),
        event_log=_format_event_log(event_log),
        impressions=_format_impressions(impressions),
    )
    try:
        response = await llm.generate(prompt, task_name=task_name, temperature=0.3,
                                      max_tokens=700, event="affection_daily_review")
    except Exception as exc:  # 宿主异常一律静默（失败哲学）
        logger.debug("关系日结裁判请求失败（跳过本轮）：%s", exc)
        return None
    data = _parse_json_loose(response)
    if data is None or not any(key in data for key in ("delta_score", "impressions", "tone")):
        return None

    raw_impressions = data.get("impressions")
    if isinstance(raw_impressions, str):
        raw_impressions = [raw_impressions]
    entries: List[Dict[str, str]] = []
    for item in (raw_impressions if isinstance(raw_impressions, list) else []):
        text = _clean_text(item.get("text") if isinstance(item, dict) else item, 80)
        if text:
            entries.append({"date": day, "text": text})
        if len(entries) >= DAILY_IMPRESSION_LIMIT:
            break
    return {
        "delta_score": round(_clip(_to_float(data.get("delta_score"), 0.0),
                                   DAILY_DELTA_MIN, DAILY_DELTA_MAX), 4),
        "impressions": entries,
        "tone": _clean_text(data.get("tone"), 80),
        "reason": _clean_text(data.get("reason"), 400),
    }


# ---------------------------------------------------------------------------
# 编排器：会话采样频控 + 日结触发/应用（服务束持有）
# ---------------------------------------------------------------------------


class AffectionReviewer:
    """好感 LLM 裁判编排器（M2）：双频控会话采样 + 睡眠时段关系日结。

    - :meth:`note_target_message`：chat Hook 的 chat_round 记账点旁同步调用；只做缓冲与
      频控，命中采样时把 LLM 请求放进后台任务（Hook timeout 1s，绝不内联等待）；
    - :meth:`on_sleep_started`：``schedule.sleep_started`` 订阅入口；arm 延迟任务，
      保证日结晚于情绪睡眠大修执行（机制见方法注释）；
    - :meth:`perform_daily_review`：真正执行日结（可独立单测）——LLM → 入账 delta →
      追加 impressions → 写 tone → 清空当日 event_log；
    - 裁判计数（采样次数 / 日结成功日期）持久化在好感档案 ``review`` 字段，跨重启防重复。
    """

    #: 日结在 sleep_started 之后的等待秒数（实例属性便于测试/运维调参）
    overhaul_grace_seconds = DAILY_REVIEW_GRACE_SECONDS

    def __init__(self, plugin: Any, affection: Any, llm: Any = None, *,
                 bus: Any = None, decision_logger: Any = None,
                 relation_getter: Any = None) -> None:
        self._plugin = plugin
        self._affection = affection
        self._llm = llm
        self._bus = bus
        self._decision_logger = decision_logger
        self._relation_getter = relation_getter
        #: 最近对方消息滚动缓冲（会话裁判输入；不落盘）
        self._recent_messages: List[str] = []
        #: 本轮采样窗口内对方消息轮数（重启后从 0 起；跨天自动清零）
        self._rounds_today = 0
        self._rounds_date = ""
        #: 后台任务（采样请求 / 延迟日结）；close() 统一取消
        self._pending: set = set()
        self._daily_task: Optional[asyncio.Task] = None

    # ---- 配置 / 上下文（全部判空降级，配置热重载即时生效）----
    def _cfg(self, name: str, default: Any) -> Any:
        cfg = getattr(getattr(self._plugin, "config", None), "affection", None)
        value = getattr(cfg, name, default) if cfg is not None else default
        return default if value is None else value

    def _review_enabled(self) -> bool:
        """``[affection] review_enabled``：false 时 M2 两个裁判整体关闭（零 LLM 调用）。"""
        return bool(self._cfg("review_enabled", True))

    def _task_name(self) -> str:
        return str(self._cfg("review_task_name", "utils") or "utils")

    def _max_per_day(self) -> int:
        return max(0, _to_int(self._cfg("review_max_per_day", 2), 2))

    def _sample_every(self) -> int:
        return max(1, _to_int(self._cfg("review_sample_every_rounds", 20), 20))

    def _affection_enabled(self) -> bool:
        is_enabled = getattr(self._affection, "is_enabled", None)
        try:
            return bool(is_enabled()) if callable(is_enabled) else True
        except Exception:
            return True

    def _llm_service(self) -> Any:
        return self._llm if self._llm is not None else getattr(self._plugin, "_llm", None)

    def _relation(self) -> Any:
        getter = self._relation_getter
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return getattr(self._plugin, "relation", None)

    def _lover_name(self) -> str:
        return str(getattr(self._plugin, "_lover_name", "") or "麦麦")

    def _persona(self) -> str:
        return str(getattr(self._plugin, "_personality", "") or "")

    def _target_label(self) -> str:
        """对方称呼（供裁判理解「对方是谁」）：关系名 + QQ；关系档案不可用时退回 QQ 或中性称谓。"""
        cfg = getattr(self._plugin, "config", None)
        target = getattr(cfg, "target", None)
        qq = str(getattr(target, "target_qq", "") or "").strip()
        has_qq = bool(qq) and qq not in ("0", "123456789")
        word = str(getattr(self._relation(), "word", "") or "")
        if has_qq and word:
            return f"{word}（QQ {qq}）"
        if has_qq:
            return f"QQ {qq}"
        return word or "重要的人"

    def _mood_text(self) -> str:
        """当前情绪低分辨率描述（**绝不含 VAD 数值**：emotion.vad.mood_summary 只出标签+档位）。"""
        mood = getattr(getattr(self._plugin, "_emotion", None), "mood_text", None)
        if not callable(mood):
            return "（情绪模块未启用）"
        try:
            return str(mood() or "（情绪状态未知）")
        except Exception:
            return "（情绪状态未知）"

    def _tier_context(self) -> Tuple[str, str]:
        """关系档位描述（名称 + 文案）——裁判可见的全部关系信息，不含分数。"""
        affection = self._affection
        relation = self._relation()
        try:
            tier = _to_int(affection.tier(), 0)
        except Exception:
            tier = 0
        if relation is not None:
            try:
                return str(relation.tier_label(tier)), str(relation.tier_description(tier))
            except Exception:
                pass
        try:
            return str(affection.tier_label()), str(affection.tier_description())
        except Exception:
            return "", ""

    def _log(self, action: str, **fields: Any) -> None:
        logger_ = self._decision_logger
        if logger_ is None:
            return
        try:
            logger_.record(action=action, **fields)
        except Exception:
            pass

    def _spawn(self, coro: Any) -> Optional[asyncio.Task]:
        """后台任务（无运行中事件循环时静默丢弃本轮，镜像 event_bus.publish_soon）。"""
        try:
            task = asyncio.create_task(coro)
        except RuntimeError:
            try:
                coro.close()
            except Exception:
                pass
            logger.debug("好感裁判：无运行中的事件循环，跳过本轮后台任务")
            return None
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return task

    # ------------------------------------------------------------------
    # 会话采样（§5.4-1）
    # ------------------------------------------------------------------
    def note_target_message(self, text: Any = "", *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """会话采样触发点（chat Hook 调用）：滚动缓冲 + 轮次计数 + 双频控。

        频控（§5.4）：每 ``review_sample_every_rounds`` 轮命中一次，且每日 ≤
        ``review_max_per_day`` 次；``review_enabled=false`` 整体关闭。
        命中时只调度后台任务，本方法立即返回（不阻塞 Hook）。

        Returns:
            ``{"sampled", "reason", "rounds", "samples"}``；reason ∈
            ``rounds / scheduled / daily_cap / disabled / affection_disabled / no_loop``。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        content = _clean_text(text, 120)
        if content:
            self._recent_messages.append(content)
            if len(self._recent_messages) > RECENT_MESSAGE_LIMIT:
                del self._recent_messages[:-RECENT_MESSAGE_LIMIT]

        if self._rounds_date != today:
            self._rounds_date = today
            self._rounds_today = 0
        self._rounds_today += 1

        state = self._affection.review_state(now=now)
        samples = _to_int(state.get("samples"), 0)
        result: Dict[str, Any] = {"sampled": False, "reason": "rounds",
                                  "rounds": self._rounds_today, "samples": samples}
        if not self._review_enabled():
            result["reason"] = "disabled"
            return result
        if not self._affection_enabled():
            result["reason"] = "affection_disabled"
            return result
        if samples >= self._max_per_day():
            result["reason"] = "daily_cap"
            return result
        if self._rounds_today < self._sample_every():
            return result

        self._rounds_today = 0
        state = self._affection.update_review_state(samples=samples + 1)
        task = self._spawn(self._run_sample_review(now=now))
        result.update(sampled=task is not None,
                      reason="scheduled" if task is not None else "no_loop",
                      samples=_to_int(state.get("samples"), samples + 1))
        return result

    async def _run_sample_review(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """执行一次会话采样裁判并走既有入账入口（后台任务体；异常一律吞掉）。"""
        try:
            if not self._review_enabled() or not self._affection_enabled():
                return {"ok": False, "reason": "disabled"}
            llm = self._llm_service()
            if llm is None:
                return {"ok": False, "reason": "no_llm"}
            tier_label, tier_desc = self._tier_context()
            parsed = await evaluate_conversation_delta(
                llm,
                recent_messages=list(self._recent_messages),
                lover_name=self._lover_name(), target_label=self._target_label(),
                persona=self._persona(), tier_label=tier_label, tier_desc=tier_desc,
                mood_text=self._mood_text(), task_name=self._task_name(),
            )
            if parsed is None:
                self._log("affection.review_skipped", trigger_type="affection",
                          reason="llm_failed", kind="llm_review")
                return {"ok": False, "reason": "llm_failed"}
            event, delta, tag = parsed
            entry = record_rule_event(self._affection, "llm_review", delta, f"{event}（{tag}）")
            self._log("affection.llm_review", trigger_type="affection", reason=event, tag=tag,
                      requested=round(delta, 4), applied=entry.get("applied"),
                      dropped=bool(entry.get("dropped")), detail=str(entry.get("reason") or ""))
            return {"ok": not entry.get("dropped"), "event": event, "delta": delta, "tag": tag,
                    "applied": entry.get("applied"), "reason": entry.get("reason")}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("会话采样裁判失败（忽略本轮）：%s", exc)
            return {"ok": False, "reason": "error"}

    # ------------------------------------------------------------------
    # 关系日结（§5.4-2）
    # ------------------------------------------------------------------
    async def on_sleep_started(self, payload: Any) -> None:
        """``schedule.sleep_started`` 订阅入口（服务束 start() 注册 / stop() 注销）。

        **晚于情绪睡眠大修的保证机制**：``emotion/service.py`` 的 ``_check_and_overhaul``
        先 ``await bus.publish("schedule.sleep_started")``、**发布返回之后**才
        ``await run_sleep_overhaul()``；事件总线又是按订阅顺序逐个 await 处理器。
        因此日结若在本处理器内联执行，会插在情绪大修之前并阻塞它。这里只 arm 一个后台任务：
        发布立即返回、情绪大修马上开始；后台任务先 ``await asyncio.sleep(overhaul_grace_seconds)``
        （默认 90s，覆盖大修的单次 LLM 请求）再执行日结，从而「晚于大修、不抢 LLM 并发」。
        大修失败/未执行（如当日已大修过）时等待满宽限期后照常日结，不会永久挂起。
        """
        data = payload if isinstance(payload, dict) else {}
        date = str(data.get("date") or datetime.now().strftime("%Y-%m-%d"))
        if not self._review_enabled() or not self._affection_enabled():
            self._log("affection.daily_review_skipped", trigger_type="affection",
                      reason="disabled", date=date)
            return
        state = self._affection.review_state()
        if str(state.get("daily_reviewed_date") or "") == date:
            return  # 当日已成功日结（跨重启防重复 LLM 调用）
        if self._daily_task is not None and not self._daily_task.done():
            return  # 已有待执行日结（幂等；sleep_started 本身只在上升沿发布）
        task = self._spawn(self._deferred_daily_review(date))
        if task is not None:
            self._daily_task = task

    async def _deferred_daily_review(self, date: str) -> None:
        try:
            grace = max(0.0, _to_float(self.overhaul_grace_seconds, DAILY_REVIEW_GRACE_SECONDS))
            if grace > 0:
                await asyncio.sleep(grace)
            await self.perform_daily_review(date=date)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("关系日结延迟执行失败（忽略）：%s", exc)

    def _build_stats(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """互动统计（§5.4）：从既有计数能取多少取多少，取不到的字段不写入（prompt 侧省略）。"""
        now = now or datetime.now()
        affection = self._affection
        stats: Dict[str, Any] = {"rounds": int(self._rounds_today)}
        try:
            log = affection.event_log()
            stats["user_initiated"] = sum(1 for e in log if e.get("kind") == "user_initiated")
            stats["commitments_kept"] = sum(1 for e in log if e.get("kind") == "commitment_kept")
            stats["commitments_broken"] = sum(1 for e in log if e.get("kind") == "commitment_broken")
        except Exception:
            pass
        try:
            stats["bot_speaks"] = float(affection.today_speak_count())
        except Exception:
            pass
        try:
            last_user = affection.last_user_msg_time()
            if last_user is not None:
                stats["silence_hours"] = round(
                    max(0.0, (now - last_user).total_seconds() / 3600.0), 1)
        except Exception:
            pass
        return stats

    async def perform_daily_review(self, *, date: str = "",
                                   now: Optional[datetime] = None) -> Dict[str, Any]:
        """执行一次关系日结：LLM → 入账 delta → 追加 impressions/tone → 清空 event_log。

        失败（请求失败/输出垃圾）静默跳过：不清空 event_log、不追加印象、不落
        ``daily_reviewed_date``——下个 sleep 边沿即可重试（同一睡眠周期只有一个上升沿，
        不会重试轰炸）。可独立单测。
        """
        day = str(date or (now or datetime.now()).strftime("%Y-%m-%d"))
        if not self._review_enabled() or not self._affection_enabled():
            return {"ok": False, "reason": "disabled"}
        state = self._affection.review_state(now=now)
        if str(state.get("daily_reviewed_date") or "") == day:
            return {"ok": False, "reason": "already_reviewed"}
        llm = self._llm_service()
        if llm is None:
            return {"ok": False, "reason": "no_llm"}

        tier_label, tier_desc = self._tier_context()
        try:
            parsed = await run_daily_review(
                llm,
                event_log=self._affection.event_log(),
                stats=self._build_stats(now=now),
                impressions=self._affection.impressions(),
                date=day, lover_name=self._lover_name(), target_label=self._target_label(),
                persona=self._persona(), tier_label=tier_label, tier_desc=tier_desc,
                task_name=self._task_name(),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("关系日结请求异常（忽略本轮）：%s", exc)
            parsed = None
        if parsed is None:
            self._log("affection.daily_review_skipped", trigger_type="affection",
                      reason="llm_failed", date=day)
            return {"ok": False, "reason": "llm_failed"}

        # 1) delta 走既有入账入口（独立于 kind 上限，受 §5.5 日累计限幅）
        entry = record_rule_event(self._affection, "daily_review", parsed["delta_score"],
                                  f"关系日结：{parsed['tone'] or '今日关系温度未知'}")
        # 2) 追加 impressions（≤3 条/次、滚动 10 条、7 天过期）→ 写 tone
        kept = self._affection.append_impressions(parsed["impressions"], date=day, now=now)
        self._affection.set_tone(parsed["tone"], date=day, now=now)
        # 3) 清空当日 event_log
        removed = self._affection.clear_event_log()
        # 4) 仅成功（JSON 解析成功并已入账）才落「当日已日结」；失败保持未落，
        #    以便下个 sleep 边沿重试（宿主验证日实证：失败即落 date 会静默吞掉第二次边沿）
        self._affection.update_review_state(daily_reviewed_date=day)
        self._log("affection.daily_review", trigger_type="affection",
                  reason=parsed["tone"] or "关系日结", date=day,
                  requested=round(parsed["delta_score"], 4), applied=entry.get("applied"),
                  dropped=bool(entry.get("dropped")), detail=str(entry.get("reason") or ""),
                  impressions=len(kept), event_log_cleared=removed,
                  referee_reason=parsed["reason"])
        return {"ok": True, "delta_score": parsed["delta_score"], "applied": entry.get("applied"),
                "impressions": kept, "tone": parsed["tone"], "reason": parsed["reason"],
                "event_log_cleared": removed}

    # ------------------------------------------------------------------
    # 生命周期（服务束 stop() 调用）
    # ------------------------------------------------------------------
    def close(self) -> None:
        """取消未执行的后台任务（幂等；服务束 stop() 时调用）。"""
        for task in list(self._pending):
            try:
                task.cancel()
            except Exception:
                pass
        self._pending.clear()
        self._daily_task = None

    async def drain_pending(self, timeout: float = 10.0) -> None:
        """等待后台任务收尾（测试/卸载用；超时即返回，不阻塞）。"""
        tasks = [task for task in list(self._pending) if not task.done()]
        if not tasks:
            return
        await asyncio.wait(tasks, timeout=max(0.0, float(timeout)))