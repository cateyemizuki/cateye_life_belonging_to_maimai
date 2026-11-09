# -*- coding: utf-8 -*-
"""PR4 事件库生成 prompt / JSON schema / 响应解析（独立新模块）。

红线（定稿 §4.3.2 / 附录 B.7）：

- 上下文只放「最近 2-3 天日程名称 + 已有事件名」，**不接收聊天原文**；
  人设不再进用户提示词——由 LLMService ``with_identity=True`` 以宿主同款
  人设开头全文注入 system_prompt（共享前缀缓存，不截断）；
- prompt（system + user）总字符数 ≤ 1500；
- 默认使用宿主 ``utils`` 任务，``temperature=0.9``；
- 只要求 JSON，不要解释；响应解析容忍 markdown 围栏。

本模块不调用 LLM、不读写文件；网络调用唯一出口仍是 ``core.llm_utils.LLMService``。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Sequence, Tuple

from .event_catalog_config import (DEFAULT_MAX_GENERATED_EVENTS,
                                   DEFAULT_MIN_GENERATED_EVENTS,
                                   DEFAULT_TEMPERATURE,
                                   EVENT_QUOTA_MAX)

#: prompt 上下文硬上限（system + user 字符数）
PROMPT_MAX_CHARS = 1500
#: 单时段批次条数上界 = 可复用 + 一次性两项配额之和的最大值（v3.9.2，12+12）
_BATCH_COUNT_MAX = 2 * EVENT_QUOTA_MAX
#: NO Puzzle 开关（默认开）追加的反套路引导；稳定句，置于任务规格之后
NO_PUZZLE_GUIDANCE = "避免拼拼图、十字绣等被滥用的套路活动，优先多样化、具体、贴合人设的日常小事。"
#: 可选块裁剪上限（字符）；人设走 system（with_identity），不进用户提示词
#: （PR-AUDIT 3.16.1 PR-3：原「人设：…」重复片段已清理，裁剪仅服务名称块）
_NAMES_MAX = 240
_EXISTING_MAX = 240

_ALLOWED_GOAL_TYPES = ("daily_routine", "rest", "social", "entertainment",
                       "exercise", "idle", "transition")

SYSTEM_PROMPT = "你是生活小事件设计助手。只输出 JSON，不要解释，不要输出 markdown。"

_RULES = """规则：
1 event_id 必须以 llm_ 开头（小写字母/数字/下划线）；
2 name 2-10 字；goal_type 仅 daily_routine/rest/social/entertainment/exercise/idle/transition；
3 min_minutes/max_minutes 取 3-120，且 max>=min；按活动自然时长设定——多数日常小事 5-30，读书/听音乐/散步等放松可 20-60 甚至更长，不要一律压短；
4 interruptibility 取 0-1，weight 取 0.1-1；
5 state_deltas 仅 energy/hunger/focus/stress/social_battery，单项绝对值<=0.08；
6 templates 2-3 条，每条 10-40 字；
7 只做空闲/转场时的日常小事；禁止硬约定、危险/违法/性/自伤/医疗/金融/法律内容、真实联系方式/地址/URL；
8 不得与已有事件重名；
9 events 数组长度必须恰好等于本次任务要求的条数，禁止多于或少于。
只输出 JSON：{"events":[{"event_id":"llm_...","name":"...","goal_type":"daily_routine","min_minutes":5,"max_minutes":10,"interruptibility":0.9,"weight":0.5,"state_deltas":{"focus":-0.02},"templates":["...","..."]}]}"""


class EventPromptError(Exception):
    """LLM 响应不是可解析的事件 JSON。"""


@dataclass(frozen=True)
class PromptBuildResult:
    system_prompt: str
    prompt: str
    char_count: int
    truncated: bool
    prompt_hash: str

    @property
    def full_text(self) -> str:
        return self.system_prompt + self.prompt


def _clip(text: Any, limit: int) -> Tuple[str, bool]:
    value = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _names_text(names: Iterable[Any], limit: int) -> Tuple[str, bool]:
    items: List[str] = []
    for raw in names or ():
        name = str(raw or "").replace("\r", " ").replace("\n", " ").strip()
        if name:
            items.append(name)
    joined = "、".join(items)
    return _clip(joined, limit)


def build_event_prompt(*, persona: Any = "", recent_schedule_names: Sequence[Any] = (),
                       existing_event_names: Sequence[Any] = (),
                       count: int = DEFAULT_MAX_GENERATED_EVENTS,
                       pool_kind: str = 'reusable', min_count: int = DEFAULT_MIN_GENERATED_EVENTS,
                       max_chars: int = PROMPT_MAX_CHARS, time_slot: str = "",
                       no_puzzle: bool = True) -> PromptBuildResult:
    """构造生成 prompt（≤ ``max_chars`` 字符，含 system）。

    ``recent_schedule_names`` / ``existing_event_names`` 只接受名称字符串；
    调用方不得传聊天正文（本函数没有接收正文的参数）。
    ``time_slot`` 非空时按三时段批次生成（v3.9.0），注入时段窗口与场景约束；
    为空时沿用旧 reusable/one_shot 单池措辞（兼容）。
    ``no_puzzle=True``（v3.9.2 默认）时在任务规格后追加反套路引导（稳定句，
    前缀缓存友好：规则在前、变化内容在后）。
    ``persona`` 参数保留签名兼容但不再消费（PR-AUDIT 3.16.1 PR-3）：人设已由
    event_catalog ``with_identity=True`` 全文注入 system_prompt，用户提示词不再
    重复「人设：…」片段。
    """
    try:
        count_int = int(count)
    except (TypeError, ValueError):
        count_int = DEFAULT_MAX_GENERATED_EVENTS
    minimum = max(1, min(int(min_count), DEFAULT_MAX_GENERATED_EVENTS))
    if pool_kind not in ('reusable', 'one_shot'):
        raise ValueError('unknown generated pool kind')
    count_int = max(minimum,
                    min(_BATCH_COUNT_MAX, count_int))
    max_chars = max(600, int(max_chars or PROMPT_MAX_CHARS))
    truncated = False

    # PR-AUDIT（3.16.1 PR-3）：人设已由 with_identity 进 system，用户提示词不再
    # 重复「人设：…」片段；persona 入参保留签名兼容但不消费，裁剪仅服务名称块。
    recent_text, cut = _names_text(recent_schedule_names, _NAMES_MAX)
    truncated = truncated or cut
    existing_text, cut = _names_text(existing_event_names, _EXISTING_MAX)
    truncated = truncated or cut

    def compose(r: str, e: str) -> str:
        # 前缀缓存友好：稳定规则+JSON 示例置顶（跨请求/跨时段复用前缀），
        # 每次变化的任务规格（条数/时段/名称）置后。
        lines = [_RULES]
        if time_slot:
            from .event_slots import SLOT_WINDOW_TEXT, SLOT_GUIDANCE
            lines.append(f"本次任务：为角色恰好生成 {count_int} 条日常小事件（用作空闲/转场 filler，禁止多于或少于），"
                         f"时段 {SLOT_WINDOW_TEXT.get(time_slot, time_slot)}。{SLOT_GUIDANCE.get(time_slot, '')}")
            lines.append('本批次含可复用与一次性小事件（pool_kind 由服务端分配）；每条有具体可完成的小目标，'
                         'goal_type 不要使用 rest/idle/transition。只生成该时段普遍适合开展的活动。')
        else:
            pool_line = ('本次生成：周期内可复用小事件，可在间隔后再次发生。' if pool_kind == 'reusable' else
                         '本次生成：一次性小事件，在本次生成周期内抽中即移除。每条有具体可完成的小目标；'
                         '不要生成反复休息、发呆或转场，goal_type不要使用rest/idle/transition。')
            lines.append(f"本次任务：为角色恰好生成 {count_int} 条日常小事件（用作空闲/转场 filler，禁止多于或少于）。" + pool_line)
        if no_puzzle:
            lines.append(NO_PUZZLE_GUIDANCE)
        if r:
            lines.append(f"最近日程名称：{r}")
        if e:
            lines.append(f"已有事件名（不得重复）：{e}")
        return "\n".join(lines)

    prompt = compose(recent_text, existing_text)
    # 超限时按「日程名 → 已有名」的顺序退让，规则/JSON 永不裁剪
    # （PR-AUDIT 3.16.1 PR-3：人设片段已移除，原「兜底只截人设」级随之退役）
    if len(SYSTEM_PROMPT) + len(prompt) > max_chars:
        truncated = True
        recent_text, _ = _clip(recent_text, 140)
        existing_text, _ = _clip(existing_text, 140)
        prompt = compose(recent_text, existing_text)
    if len(SYSTEM_PROMPT) + len(prompt) > max_chars:
        truncated = True
        prompt = compose("", existing_text)
    if len(SYSTEM_PROMPT) + len(prompt) > max_chars:
        truncated = True
        prompt = compose("", "")
    digest = hashlib.sha256((SYSTEM_PROMPT + prompt).encode("utf-8")).hexdigest()[:16]
    return PromptBuildResult(system_prompt=SYSTEM_PROMPT, prompt=prompt,
                             char_count=len(SYSTEM_PROMPT) + len(prompt),
                             truncated=truncated, prompt_hash=digest)


def build_json_schema(count: int = DEFAULT_MAX_GENERATED_EVENTS) -> Dict[str, Any]:
    """输出 JSON Schema（供未来宿主 response_format / 调试复用）。"""
    try:
        count_int = int(count)
    except (TypeError, ValueError):
        count_int = DEFAULT_MAX_GENERATED_EVENTS
    count_int = max(DEFAULT_MIN_GENERATED_EVENTS,
                    min(_BATCH_COUNT_MAX, count_int))
    delta_props = {key: {"type": "number", "minimum": -0.08, "maximum": 0.08}
                   for key in ("energy", "hunger", "focus", "stress", "social_battery")}
    return {
        "type": "object",
        "required": ["events"],
        "additionalProperties": False,
        "properties": {
            "events": {
                "type": "array",
                "minItems": DEFAULT_MIN_GENERATED_EVENTS,
                "maxItems": count_int,
                "items": {
                    "type": "object",
                    "required": ["event_id", "name", "goal_type", "min_minutes",
                                 "max_minutes", "interruptibility", "weight",
                                 "state_deltas", "templates"],
                    "additionalProperties": True,
                    "properties": {
                        "event_id": {"type": "string", "pattern": "^llm_[a-z0-9_]+$"},
                        "name": {"type": "string", "minLength": 2, "maxLength": 10},
                        "goal_type": {"type": "string", "enum": list(_ALLOWED_GOAL_TYPES)},
                        "min_minutes": {"type": "integer", "minimum": 3, "maximum": 120},
                        "max_minutes": {"type": "integer", "minimum": 3, "maximum": 120},
                        "interruptibility": {"type": "number", "minimum": 0, "maximum": 1},
                        "weight": {"type": "number", "minimum": 0.1, "maximum": 1.0},
                        "state_deltas": {"type": "object", "properties": delta_props,
                                         "additionalProperties": False},
                        "templates": {"type": "array", "minItems": 2, "maxItems": 3,
                                      "items": {"type": "string", "minLength": 10,
                                                "maxLength": 40}},
                        "enabled": {"type": "boolean"},
                    },
                },
            }
        },
    }


_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


def _loads_candidates(text: str) -> Any:
    candidates: List[str] = [text]
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start:end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
    raise EventPromptError(f"LLM 响应不是合法 JSON：{last_error}")


def parse_events_response(text: Any) -> List[Dict[str, Any]]:
    """LLM 响应 → events 列表（容忍 markdown 围栏 / 裸数组 / {"events":[...]}）。

    只做结构提取；字段合法性由 ``EventCatalogValidator`` 全量校验。
    """
    if not isinstance(text, str) or not text.strip():
        raise EventPromptError("LLM 响应为空")
    data = _loads_candidates(text.strip())
    if isinstance(data, dict):
        events = data.get("events")
    elif isinstance(data, list):
        events = data
    else:
        events = None
    if not isinstance(events, list):
        raise EventPromptError("LLM 响应缺少 events 数组")
    return events


#: 供接线工人导入的固定生成参数（不要在各处再写一份字面量）
GENERATION_TEMPERATURE = DEFAULT_TEMPERATURE
