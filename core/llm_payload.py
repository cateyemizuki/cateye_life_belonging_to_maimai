"""Pure SDK payload preparation and host-response parsing.

No facade, transport, SDK, persistence, policy, routing, retry or model I/O dependencies.
"""
from __future__ import annotations
import json
import re
from typing import Any, Optional

#: 只有宿主明确表示「任务路由不存在」才允许调用方做一次 utils 回退。
TASK_MISSING_CODES = frozenset({"task_missing", "unknown_task"})
#: 官方文档（v1.3.2 附录 B）唯一给出的任务解析失败文案：反引号 + 「的模型配置」。
#: 具体模型不存在是「未找到名为 'xxx' 的模型」（无「的模型配置」），绝不能当任务缺失。
_TASK_MISSING_MESSAGE_RE = re.compile(r"未找到名为\s*`[^`]*`\s*的模型配置")

def prepare_prompt_arg(prompt: Any, system_prompt: str = "") -> Any:
    """按宿主约定把 str prompt + system 转成消息列表（唯一构造点）。"""
    if not system_prompt:
        return prompt
    system = {"role": "system", "content": system_prompt}
    if isinstance(prompt, (list, tuple)) and all(isinstance(item, dict) and "role" in item for item in prompt):
        return [system, *prompt]
    return [system, {"role": "user", "content": prompt}]

def prompt_payload_bytes(prompt_arg: Any) -> int:
    """实际传给 SDK 的 prompt 载荷 UTF-8 字节数（不含 RPC 封包）。"""
    if isinstance(prompt_arg, str):
        return len(prompt_arg.encode("utf-8"))
    try:
        text = json.dumps(prompt_arg, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(prompt_arg)
    return len(text.encode("utf-8"))

def classify_llm_error(error: Any, error_code: Any = None) -> str:
    """把宿主错误归类为 ``task_missing`` / ``timeout`` / ``general_error``。

    任务缺失只认两种有依据的信号：结构化错误码 ``task_missing``/``unknown_task``，
    或官方文档给出的任务解析文案（反引号 + 「的模型配置」）。不做「未找到」之类的
    含糊子串匹配，避免把具体模型名不存在误判为任务不存在。
    """
    code = str(error_code or "").strip().lower()
    if code in TASK_MISSING_CODES:
        return "task_missing"
    text = str(error or "")
    if _TASK_MISSING_MESSAGE_RE.search(text):
        return "task_missing"
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered or "超时" in text:
        return "timeout"
    return "general_error"

def extract_cache_usage(result: Any) -> tuple[Optional[int], Optional[int]]:
    """读 prompt 缓存命中/未命中（宿主字段或 OpenAI 风格 details）。

    供应商未回传时返回 ``(None, None)``，与「真 0」区分；不推算。
    """
    if not isinstance(result, dict):
        return None, None
    usage = result.get("usage")
    sources = [result]
    if isinstance(usage, dict):
        sources.append(usage)
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            sources.append(details)
    details_top = result.get("prompt_tokens_details")
    if isinstance(details_top, dict):
        sources.append(details_top)

    def pick(names: tuple[str, ...]) -> Optional[int]:
        for source in sources:
            for name in names:
                if source.get(name) is not None:
                    return _optional_int(source.get(name))
        return None

    hit = pick(("prompt_cache_hit_tokens", "cache_hit_tokens", "cached_tokens"))
    miss = pick(("prompt_cache_miss_tokens", "cache_miss_tokens"))
    return hit, miss

def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None

def extract_usage(result: Any) -> tuple[Optional[int], Optional[int], Optional[int], bool, bool]:
    """从宿主结果读真实 usage：兼容顶层字段与 OpenAI 风格 ``usage`` 子字典。

    返回 ``(prompt, completion, total, available, usage_present)``。
    - ``available`` 只在 total 可得，或 prompt/completion 同时可得时为 True；
      部分字段缺失时保持 None，不用插件估算补足。
    - ``usage_present``（2026-10-29 口径修正新增位）：宿主响应里是否存在可识别的
      用量字段——**字段在场即真，含 0 值**（宿主失败响应常带 ``total_tokens: 0``，
      旧口径把它当"可用"，是失败被记成 host_reported 0 的根因）。成败不再由
      usage 推断：失败归失败桶由 ``success`` 布尔承担（见 :meth:`_record_usage`）。
    """
    if not isinstance(result, dict):
        return None, None, None, False, False
    usage = result.get("usage")
    sources = (result, usage if isinstance(usage, dict) else {})
    usage_present = isinstance(usage, dict) and bool(usage)

    def pick(names: tuple[str, ...]) -> Optional[int]:
        nonlocal usage_present
        for source in sources:
            for name in names:
                if source.get(name) is not None:
                    usage_present = True
                    value = _optional_int(source.get(name))
                    if value is not None:
                        return value
        return None

    prompt = pick(("prompt_tokens", "prompt_token_count", "input_tokens"))
    completion = pick(("completion_tokens", "completion_token_count", "output_tokens"))
    total = pick(("total_tokens", "total_token_count"))
    if total is None and prompt is not None and completion is not None:
        total = prompt + completion
    available = total is not None
    return prompt, completion, total, available, usage_present
