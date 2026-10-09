"""角色裁判（来源：自主规划插件 planner/role_judge.py，移植版）。

供 ``tools_service.update_schedule_v4`` 使用：把当前日程 + 人设 + 用户描述
扔给 LLM，让它返回 ``{"decision": "today/future/reject", ...}``，调用方
据此决定是重生成当日日程、记录未来约定，还是拒绝不变。
LLM 调用走宿主 ``ctx.llm.generate``（task_name 路由）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

_VALID_DECISIONS = {"today", "future", "reject"}
_RELATIVE_DATE_OFFSETS = {
    "今天": 0, "今日": 0,
    "明天": 1, "明日": 1,
    "后天": 2, "後天": 2,
    "大后天": 3, "大後天": 3,
}


def _strip_leading_plus(text: str) -> str:
    """字符串感知地剥离 JSON 数值的前导 ``+``（镜像 emotion/referee.py 同名函数）。

    只动字符串字面量**之外**、处于数值前导位置（``: , [`` 或其后空白）的 ``+``；
    字符串内部的 ``+`` 与指数记号（``1e+5``）不受影响。宿主 2026-10-07 夜实弹：
    睡眠裁判输出 ``+0.04`` 连续两轮解析失败 → 整夜冻结重试，第三轮模型随机去掉 ``+`` 才自愈。
    """
    if "+" not in text:
        return text
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if (ch == "+" and out and out[-1] in ":,[ \t\r\n"
                and i + 1 < n and text[i + 1].isdigit()):
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _loads_dict(candidate: str) -> Optional[Dict[str, Any]]:
    parsed = None
    try:
        data = json.loads(candidate)
        if isinstance(data, dict):
            parsed = data
    except (json.JSONDecodeError, ValueError):
        pass
    return parsed


def _parse_json_loose(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 响应文本中提取首个 JSON 对象；全失败后剥前导 ``+`` 重试一轮（候选次序不变）。"""
    text = (text or "").strip()
    candidates = [text, *(m.group(1) for m in _JSON_BLOCK_RE.finditer(text))]
    match = _JSON_OBJECT_RE.search(text)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        parsed = _loads_dict(candidate)
        if parsed is not None:
            return parsed
    for candidate in candidates:                     # 前导 '+' 兜底
        normalized = _strip_leading_plus(candidate)
        if normalized == candidate:
            continue
        parsed = _loads_dict(normalized)
        if parsed is not None:
            return parsed
    return None


def _infer_future_date(raw_date: str, today_str: str) -> str:
    """LLM 未给出具体日期时，按「明天/后天」等关键词推断。"""
    try:
        today = datetime.strptime(today_str, "%Y-%m-%d")
    except ValueError:
        return today_str
    for keyword, offset in _RELATIVE_DATE_OFFSETS.items():
        if keyword in (raw_date or ""):
            return (today + timedelta(days=offset)).strftime("%Y-%m-%d")
    return today_str


def _build_judge_prompt(
    *,
    persona: str,
    today_str: str,
    weekday: str,
    current_activities: List[Dict[str, Any]],
    description: str,
) -> str:
    """构造角色裁判 prompt。"""
    if current_activities:
        lines = []
        for act in current_activities:
            time_str = str(act.get("time", "") or "").strip() or "未指定时间"
            name = str(act.get("name", "") or "").strip()
            lines.append(f"- {time_str} {name}")
        current_text = "\n".join(lines)
    else:
        current_text = "暂无可靠日程"

    return (
        f"【角色人设】\n{persona or '未提供'}\n\n"
        f"【日期信息】\n今天是 {today_str}（{weekday}）\n\n"
        f"【当前日程】\n{current_text}\n\n"
        f"【用户请求】\n{description}\n\n"
        "请扮演该角色，判断是否会接受这个日程相关请求。\n"
        "不要机械接受用户安排；如果不符合人设、时间冲突太强、语气像玩笑或没有明确计划，可以拒绝或不改日程。\n"
        "decision 只能是以下值之一：\n"
        "- today: 角色接受或调整今天的安排，需要修改今天日程\n"
        "- future: 角色接受未来某天的预约，只记录预约，不修改今天日程\n"
        "- reject: 角色没有接受，日程不变\n\n"
        "以 JSON 格式返回：\n"
        '{"decision": "today/future/reject", "date": "YYYY-MM-DD 或空", '
        '"time": "HH:MM 或空", "title": "简短标题", "notes": "备注", '
        '"reason": "角色判断理由", "raw_date": "用户原文中的日期描述"}\n'
        "日期必须根据【日期信息】中的真实日期推断。\n"
        "只输出 JSON，不要其他文字。"
    )


async def judge_schedule_request(
    plugin: Any,
    *,
    description: str,
    current_activities: List[Dict[str, Any]],
    persona: str,
    today_str: str,
    weekday: str,
    task_name: str,
    temperature: float = 0.3,
    max_tokens: int = 2000,
    timeout_ms: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """让 LLM 判断用户日程请求该如何处理。

    Args:
        plugin: 当前插件实例（用于 ``ctx.llm.generate``）。
        description: 用户的日程相关请求文本。
        current_activities: 今日日程活动列表（每项含 ``time`` / ``name``）。
        persona: 角色人设文本。
        today_str: 今日日期（``YYYY-MM-DD``）。
        weekday: 今日星期标签。
        task_name: 模型任务名（走宿主任务路由）。
        temperature: LLM 温度。
        max_tokens: LLM 最大 tokens。
        timeout_ms: LLM 调用 RPC 超时（毫秒）。``None`` 使用宿主默认。

    Returns:
        解析后的判断 dict（含 ``decision`` 等字段）；LLM 失败时返回 None，
        调用方应降级到「日程不变」。
    """
    prompt = _build_judge_prompt(
        persona=persona,
        today_str=today_str,
        weekday=weekday,
        current_activities=current_activities,
        description=description,
    )

    llm_kwargs: Dict[str, Any] = {
        "prompt": prompt,
        "task_name": task_name,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if timeout_ms is not None and timeout_ms > 0:
        llm_kwargs["timeout_ms"] = int(timeout_ms)

    try:
        result = await plugin.ctx.llm.generate(**llm_kwargs)
    except Exception as exc:
        logger.warning(f"角色裁判 LLM 调用失败: {exc}")
        return None

    response_text = ""
    if isinstance(result, dict):
        response_text = str(result.get("response", "") or "").strip()
    success = bool(result.get("success", False)) if isinstance(result, dict) else False

    if not success or not response_text:
        return None

    parsed = _parse_json_loose(response_text)
    if not parsed:
        return None

    decision = str(parsed.get("decision", "") or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        logger.info(f"角色裁判返回未知 decision={decision}，按 reject 处理")
        parsed["decision"] = "reject"
    parsed.setdefault("title", description[:40])
    parsed.setdefault("date", "")
    parsed.setdefault("time", "")
    parsed.setdefault("notes", "")
    parsed.setdefault("reason", "")
    parsed.setdefault("raw_date", "")

    # 补全 future 分支的日期推断
    if parsed["decision"] == "future" and not str(parsed.get("date") or "").strip():
        parsed["date"] = _infer_future_date(str(parsed.get("raw_date", "")), today_str)
    return parsed
