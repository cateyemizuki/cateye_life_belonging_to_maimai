"""Shared immutable LLM result types; standard-library dependencies only.

Legacy modules re-export these exact classes so isinstance and old imports retain identity.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Optional

@dataclass(frozen=True)
class LLMResult:
    """一次统一生成（可含有界重试）的请求关联详细结果。

    所有字段都来自本次调用自己的返回，不使用共享 ``last_error``/``last_usage``；
    ``request_id`` 用于并发排查。``attempts`` 是真正发给宿主的模型请求次数
    （任务解析失败未发出模型请求时为 0），``request_sent``/``consumed`` 供预算层判断
    是否可安全释放预占。
    """

    ok: bool
    text: str
    category: str
    request_id: str = ""
    error: str = ""
    task_name: str = ""
    model: str = ""
    reasoning: str = ""
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    usage_available: bool = False
    request_sent: bool = False
    consumed: bool = False
    attempts: int = 0
    strict_params: bool = False
    #: 账本 v2（2026-11-02）：模块/事件/缓存/耗时与不可变回执；旧调用方零感知
    module: str = ""
    event: str = ""
    cache_hit_tokens: Optional[int] = None
    cache_miss_tokens: Optional[int] = None
    duration_ms: int = 0
    receipt: Optional[Any] = None
    #: 3.17.6（PR-REMOVE-POOLCAP E_LLM 诊断）：宿主若回传 finish_reason 则透传
    #: （现行宿主 capability payload 无此字段 → 空串；截断诊断另有 completion_tokens
    #: 触顶启发式兜底）。统一策略用明确 length 标志触发一次格式修复。
    finish_reason: str = ""
    used_template: bool = False
    template_name: str = ""
    template_title: str = ""
    fallback_reason: str = ""
    level: str = "info"
    tier: str = "small"
    warning_threshold: int = 4000
    policy_group_id: str = ""

@dataclass(frozen=True)
class LLMReceipt:
    """一次调用（含内部回退）的对账凭据；只读，不参与业务判断。"""

    ok: bool
    module: str
    event: str
    task_name: str
    model: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cache_hit_tokens: Optional[int] = None
    cache_miss_tokens: Optional[int] = None
    duration_ms: int = 0
    attempts: int = 0
    request_id: str = ""
    category: str = ""
    error: str = ""
    schema: int = 2
