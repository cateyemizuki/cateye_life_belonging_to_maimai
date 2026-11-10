# -*- coding: utf-8 -*-
"""LLM JSON 载荷统一提取（复核 P1-12：各裁判/策略解析器脆弱、无统一入口）。

提取顺序（保留旧 _parse_json_loose 候选优先级，前两步为新增鲁棒层）：
1. 剥闭合 `````` 块（大小写不敏感；think 内含花括号不再污染贪婪 brace 候选）；
2. 候选 = 围栏内容（```json/```JSON 均可）→ 整段 → 首个贪婪 {...} 块；
3. 逐候选严格 json.loads（拒绝 NaN/Infinity 非有限常量）；
4. 全失败 → 字符串感知剥数值前导 ``+`` 重试一轮（2026-10-07 夜实弹兜底，原 referee 迁入）；
5. 仍失败 → raw_decode 逐 ``{`` 起点扫描（think 前缀裸 JSON / JSON 后拖尾文字可救回）。

只认 dict 载荷；任何入参都不抛异常，失败返回 None 由调用方走既有兜底。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

__all__ = ["extract_json_payload", "strip_think_blocks"]

_THINK_TAG = chr(60) + "think" + chr(62)
_THINK_END = chr(60) + "/think" + chr(62)
_THINK_CLOSED = re.compile(re.escape(_THINK_TAG) + r".*?" + re.escape(_THINK_END),
                           re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"```\s*(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)
_BRACE = re.compile(r"\{.*\}", re.DOTALL)


def _reject_constant(value: str) -> Any:
    raise ValueError(value)


def strip_think_blocks(text: str) -> str:
    """移除闭合 think 块；未闭合标记留给候选/raw_decode 层处理。"""
    return _THINK_CLOSED.sub("", text)


def _strip_leading_plus(text: str) -> str:
    """字符串感知地剥离 JSON 数值的前导 ``+``（部分模型输出 ``+0.04``，严格 JSON 非法）。

    只动字符串字面量**之外**、处于数值前导位置（``: , [`` 或其后空白）的 ``+``；
    字符串内部的 ``+`` 与指数记号（``1e+5``）不受影响。
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


def _loads(candidate: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(candidate, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def extract_json_payload(text: Any) -> Optional[Dict[str, Any]]:
    """从 LLM 回复中提取首个 JSON dict 载荷；失败返回 None（不抛异常）。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = strip_think_blocks(raw).strip()
    if not raw:
        return None
    candidates = [raw]
    fence = _FENCE.search(raw)
    if fence:
        candidates.insert(0, fence.group(1))
    brace = _BRACE.search(raw)
    if brace:
        candidates.append(brace.group(0))
    for candidate in candidates:
        data = _loads(candidate)
        if data is not None:
            return data
    for candidate in candidates:                     # 前导 '+' 兜底（候选优先级不变）
        normalized = _strip_leading_plus(candidate)
        if normalized == candidate:
            continue
        data = _loads(normalized)
        if data is not None:
            return data
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    for match in re.finditer(r"\{", raw):
        try:
            data, _ = decoder.raw_decode(raw[match.start():])
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None