"""统一 LLM 调用入口（2026-11-02 LLM 统一入口与用量对账 S1/S2）。

所有业务模块的模型调用最终都经过这里：它负责

- **模块身份校验**：``module`` 必须来自 :data:`core.llm_usage.MODULE_VALUES`；
  缺失/非法记 ``unknown`` 并留一条可检索 warning，绝不静默混入「其他」；
- **唯一记账点**：每次真正发给宿主的请求（含失败后回退的第一次请求）写一行
  v2 账本，字段含模块、任务名、输入/输出、缓存命中/未命中、耗时与尝试次数；
- **返回契约**：``LLMResult`` 内附不可变 :class:`LLMReceipt`，供调用方与诊断读取。

``core.llm_utils.LLMService`` 保留原签名，内部委托本模块（单一出口，避免双记账）。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from core.llm_usage import (
    LLMUsageTracker,
    MODULE_UNKNOWN,
    MODULE_VALUES,
    normalize_module,
)
from core.llm_utils import (
    LLMResult,
    classify_llm_error,
    extract_cache_usage,
    extract_usage,
    prepare_prompt_arg,
)


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


def receipt_of(result: Any, *, module: str, event: str, task: str,
               request_id: str, attempts: int, duration_ms: int,
               category: str = "") -> LLMReceipt:
    """从宿主响应构造回执（缺失字段保持 None，不估算）。"""
    prompt_tokens, completion_tokens, total_tokens, _available, _present = extract_usage(result)
    cache_hit, cache_miss = extract_cache_usage(result)
    model = ""
    if isinstance(result, dict):
        model = str(result.get("model") or result.get("model_name") or "")
    ok = bool(isinstance(result, dict) and result.get("success") is True
              and str(result.get("response") or "").strip())
    return LLMReceipt(
        ok=ok, module=module, event=str(event or ""), task_name=str(task or ""),
        model=model, prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens, total_tokens=total_tokens,
        cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss,
        duration_ms=max(0, int(duration_ms)), attempts=max(0, int(attempts)),
        request_id=str(request_id or ""), category=str(category or ""),
    )


def record_response(tracker: Optional[LLMUsageTracker], *, event: str, module: str,
                    task: str, result: Any, success: bool,
                    duration_ms: int = 0, attempts: int = 1,
                    degraded: bool = False, abandoned: bool = False) -> None:
    """把一次宿主响应写入账本 v2（缺失用量只计次数，绝不伪造 token）。"""
    if tracker is None:
        return
    if not isinstance(result, dict):
        result = {"success": False}
    try:
        prompt_tokens, completion_tokens, total_tokens, available, present = extract_usage(result)
        cache_hit, cache_miss = extract_cache_usage(result)
        tracker.record_v2(
            event=event, module=normalize_module(module), task_name=task,
            model=result.get("model_name") or result.get("model") or "",
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss, duration_ms=duration_ms,
            attempts=attempts,
            request_id=result.get("request_id") or result.get("id") or "",
            usage_available=available, success=success,
            usage_present=present, degraded=degraded, abandoned=abandoned,
        )
    except Exception:
        # 记账失败不得打断业务链路（与旧 _record_usage 口径一致）
        pass


class LLMGateway:
    """宿主 LLM 能力的唯一调用与记账入口。"""

    def __init__(
        self,
        ctx: Any,
        *,
        task_name: str = "utils",
        model_name: str = "",
        call_logger: Any = None,
        usage_tracker: Optional[LLMUsageTracker] = None,
        optimization_enabled_provider: Any = None,
    ) -> None:
        self._ctx = ctx
        self._task_name = task_name or "utils"
        self._model_name = model_name or ""
        self._call_logger = call_logger
        self._usage_tracker = usage_tracker
        from core.prompt_optimizer import PromptOptimizer
        self._prompt_optimizer = PromptOptimizer(
            getattr(getattr(ctx, "paths", None), "data_dir", None), optimization_enabled_provider)

    @property
    def usage_tracker(self) -> Optional[LLMUsageTracker]:
        return self._usage_tracker

    def update_task(self, task_name: str, model_name: str = "") -> None:
        self._task_name = task_name or "utils"
        self._model_name = model_name or ""

    async def generate(
        self,
        prompt: str,
        *,
        module: str = "",
        system_prompt: str = "",
        task_name: str = "",
        temperature: float = 0.7,
        timeout_ms: Optional[int] = None,
        event: str = "unspecified",
        model: str = "",
        strict_params: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        result = await self.generate_detailed(
            prompt, module=module, system_prompt=system_prompt,
            task_name=task_name, temperature=temperature,
            timeout_ms=timeout_ms, event=event, model=model,
            strict_params=strict_params, max_tokens=max_tokens,
        )
        return result.text

    async def generate_detailed(
        self,
        prompt: Any,
        *,
        module: str = "",
        system_prompt: str = "",
        task_name: str = "",
        temperature: float = 0.7,
        timeout_ms: Optional[int] = None,
        event: str = "unspecified",
        model: str = "",
        strict_params: bool = False,
        max_tokens: Optional[int] = None,
    ) -> LLMResult:
        """生成文本并返回详细结果；路由/回退优先级与旧 LLMService 逐字一致。

        ``max_tokens``（3.17.6 PR-REMOVE-POOLCAP E_LLM 修复）：显式输出上限
        override，经宿主 SDK ``ctx.llm.generate`` 透传（宿主显式值优先于任务/
        模型配置）；None = 不传、沿用宿主配置（旧语义逐字不变）。有界夹取由
        调用方负责（事件库按 requested 动态、封顶），本网关只透传不放大。
        """
        started = time.monotonic()
        module_name = self._validate_module(module)
        request_id = uuid.uuid4().hex
        prompt_arg = prepare_prompt_arg(prompt, system_prompt)
        routed_task = str(task_name or self._task_name or "utils")
        override = str(model or "").strip()
        attempts = 0

        async def host_generate(**kwargs: Any) -> Any:
            task = str(kwargs.get("task_name") or routed_task)
            actual_override = str(kwargs.get("model") or "")
            original = kwargs.get("prompt")
            try:
                kwargs["prompt"], hinted = self._prompt_optimizer.prepare(
                    original, task=task, event=event, model=actual_override, timeout_ms=timeout_ms)
            except Exception:
                kwargs["prompt"], hinted = original, False
            result = await self._ctx.llm.generate(**kwargs)
            try:
                self._prompt_optimizer.observe(result, task=task, event=event,
                    model=actual_override, timeout_ms=timeout_ms, hinted=hinted)
            except Exception:
                pass  # 优化器故障不改变原有结果、计数或兜底路径。
            return result

        if strict_params:
            kwargs: dict[str, Any] = {
                "prompt": prompt_arg,
                "temperature": temperature,
            }
            if override:
                kwargs["model"] = override
            else:
                kwargs["task_name"] = routed_task
            if timeout_ms:
                kwargs["timeout_ms"] = int(timeout_ms)
            if max_tokens is not None:
                kwargs["max_tokens"] = max(1, int(max_tokens))
            attempts = 1
            try:
                result = await host_generate(**kwargs)
            except asyncio.CancelledError:
                self.record_abandoned(module=module_name, event=event, task=routed_task,
                                      reason="cancelled")
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                return self._exception_result(
                    exc, event=event, task=routed_task, request_id=request_id,
                    attempts=attempts, category="timeout", strict_params=True,
                    module=module_name, started=started)
            except Exception as exc:
                return self._exception_result(
                    exc, event=event, task=routed_task, request_id=request_id,
                    attempts=attempts, category="general_error", strict_params=True,
                    module=module_name, started=started)
            return self._result_from_response(
                result, event=event, task=routed_task, request_id=request_id,
                attempts=attempts, strict_params=True, requested_model=override,
                module=module_name, started=started)

        # ---- 以下为旧 generate 的解析/降级优先级，逐字保留 ----
        if override:
            attempts = 1
            try:
                override_kwargs: dict[str, Any] = {
                    "prompt": prompt_arg, "model": override, "temperature": temperature}
                if max_tokens is not None:
                    override_kwargs["max_tokens"] = max(1, int(max_tokens))
                result = await host_generate(**override_kwargs)
            except asyncio.CancelledError:
                self.record_abandoned(module=module_name, event=event, task=override,
                                      reason="cancelled")
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                return self._exception_result(
                    exc, event=event, task=override, request_id=request_id,
                    attempts=attempts, category="timeout", strict_params=False,
                    module=module_name, started=started)
            except Exception as exc:
                return self._exception_result(
                    exc, event=event, task=override, request_id=request_id,
                    attempts=attempts, category="general_error", strict_params=False,
                    module=module_name, started=started)
            return self._result_from_response(
                result, event=event, task=override, request_id=request_id,
                attempts=attempts, strict_params=False, requested_model=override,
                module=module_name, started=started)

        kwargs = {
            "prompt": prompt_arg,
            "task_name": routed_task,
            "temperature": temperature,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, int(max_tokens))
        if self._model_name:
            # 指定具体模型：失败后降级为纯任务路由再试一次（旧语义）
            attempts = 1
            try:
                result = await host_generate(**{**kwargs, "model": self._model_name})
                if isinstance(result, dict) and result.get("success"):
                    return self._result_from_response(
                        result, event=event, task=routed_task, request_id=request_id,
                        attempts=attempts, strict_params=False,
                        requested_model=self._model_name, module=module_name,
                        started=started)
                # Each host response counts, including a failed model attempt
                # followed by the existing task-route fallback.
                self._result_from_response(
                    result, event=event, task=routed_task, request_id=request_id,
                    attempts=attempts, strict_params=False,
                    requested_model=self._model_name, module=module_name,
                    started=started)
            except asyncio.CancelledError:
                self.record_abandoned(module=module_name, event=event, task=routed_task,
                                      reason="cancelled")
                raise
            except Exception as exc:
                # F1（2026-11-04）：首尝已发给宿主，异常后虽回退任务路由仍补一行
                # abandoned 账，与文件头「含失败后回退的第一次请求」声明一致。
                self._record_exception_usage(
                    event=event, task=routed_task, module=module_name,
                    request_id=request_id, attempts=attempts,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=f"[first_attempt] {str(exc) or type(exc).__name__}")
        if timeout_ms:
            kwargs["timeout_ms"] = int(timeout_ms)
        attempts += 1
        try:
            result = await host_generate(**kwargs)
        except asyncio.CancelledError:
            self.record_abandoned(module=module_name, event=event, task=routed_task,
                                  reason="cancelled")
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            return self._exception_result(
                exc, event=event, task=routed_task, request_id=request_id,
                attempts=attempts, category="timeout", strict_params=False,
                module=module_name, started=started)
        except Exception as exc:
            return self._exception_result(
                exc, event=event, task=routed_task, request_id=request_id,
                attempts=attempts, category="general_error", strict_params=False,
                module=module_name, started=started)
        return self._result_from_response(
            result, event=event, task=routed_task, request_id=request_id,
            attempts=attempts, strict_params=False,
            requested_model=self._model_name, module=module_name,
            started=started)

    async def generate_messages(
        self,
        prompt_arg: Any,
        *,
        module: str = "",
        task_name: str = "",
        temperature: float = 0.3,
        event: str = "unspecified",
        timeout_ms: Optional[int] = None,
    ) -> LLMResult:
        """消息列表形态（视觉理解等）：不套模型回退，保持旧 describe_image 语义。"""
        started = time.monotonic()
        module_name = self._validate_module(module)
        request_id = uuid.uuid4().hex
        task = str(task_name or self._task_name)
        kwargs: dict[str, Any] = {
            "prompt": prompt_arg,
            "task_name": task,
            "temperature": temperature,
        }
        if timeout_ms:
            kwargs["timeout_ms"] = int(timeout_ms)
        try:
            result = await self._ctx.llm.generate(**kwargs)
        except asyncio.CancelledError:
            self.record_abandoned(module=module_name, event=event, task=task,
                                  reason="cancelled")
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            return self._exception_result(
                exc, event=event, task=task, request_id=request_id,
                attempts=1, category="timeout", strict_params=False,
                module=module_name, started=started)
        except Exception as exc:
            return self._exception_result(
                exc, event=event, task=task, request_id=request_id,
                attempts=1, category="general_error", strict_params=False,
                module=module_name, started=started)
        return self._result_from_response(
            result, event=event, task=task, request_id=request_id,
            attempts=1, strict_params=False, module=module_name, started=started)

    def record_abandoned(self, *, module: str, event: str, task: str,
                         reason: str = "abandoned") -> None:
        """记录「插件放弃但宿主可能已计费」的调用（v1 账差主因）。"""
        record_response(
            self._usage_tracker, event=event, module=module, task=task,
            result={"success": False, "error": reason}, success=False,
            duration_ms=0, attempts=1, degraded=True, abandoned=True)

    def _record_exception_usage(self, *, event: str, task: str, module: str,
                                request_id: str, attempts: int, duration_ms: int,
                                error: str) -> None:
        """异常路径补账（F1，2026-11-04）：请求已发给宿主但未收到响应。

        超时/断连时宿主可能已完成并计费，按文件头「每次真正发给宿主的请求写一行」
        补一行 v2：tokens=None 只计次数，标 degraded+abandoned（与 record_abandoned
        同口径），绝不伪造用量；记账失败由 record_response 内吞，不打断业务。
        """
        record_response(
            self._usage_tracker, event=event, module=module, task=task,
            result={"success": False, "error": error, "request_id": request_id},
            success=False, duration_ms=duration_ms, attempts=attempts,
            degraded=True, abandoned=True)

    # ---- 结果翻译（唯一记账/记日志出口） ----
    def _exception_result(self, exc: BaseException, *, event: str, task: str,
                          request_id: str, attempts: int, category: str,
                          strict_params: bool, module: str,
                          started: float) -> LLMResult:
        error = str(exc) or type(exc).__name__
        duration = int((time.monotonic() - started) * 1000)
        self._log(event, task, False, "", error)
        try:
            self._ctx.logger.warning(f"LLM 调用异常: {error}")
        except Exception:
            pass
        # F1（2026-11-04）：超时/连接错误等本地异常此前只进诊断账不进用量账本，
        # 与文件头声明矛盾；request_sent=True 口径下补一行 abandoned/degraded。
        self._record_exception_usage(
            event=event, task=task, module=module, request_id=request_id,
            attempts=attempts, duration_ms=duration,
            error=f"[{category}] {error}")
        receipt = LLMReceipt(
            ok=False, module=module, event=event, task_name=task, model="",
            duration_ms=duration, attempts=attempts, request_id=request_id,
            category=category, error=error)
        return LLMResult(ok=False, text="", category=category, request_id=request_id,
                         error=error, task_name=task, request_sent=True,
                         consumed=True, attempts=attempts,
                         strict_params=strict_params, module=module, event=event,
                         duration_ms=duration, receipt=receipt)

    def _result_from_response(self, result: Any, *, event: str, task: str,
                              request_id: str, attempts: int, strict_params: bool,
                              requested_model: str = "", module: str,
                              started: float) -> LLMResult:
        """把一次宿主响应翻译成 LLMResult（唯一记账/记日志出口）。"""
        duration = int((time.monotonic() - started) * 1000)
        success = bool(isinstance(result, dict) and result.get("success") is True)
        self._record_usage(event, task, result, success=success, module=module,
                           duration_ms=duration, attempts=attempts)
        prompt_tokens, completion_tokens, total_tokens, usage_available, usage_present = extract_usage(result)
        cache_hit, cache_miss = extract_cache_usage(result)
        response_model = ""
        if isinstance(result, dict):
            response_model = str(result.get("model") or result.get("model_name") or "")
        routed_model = response_model or requested_model
        # 3.17.6（E_LLM 截断诊断）：宿主若回传 finish_reason 则透传（顶层或
        # OpenAI 风格 choices[0]）；现行宿主 capability payload 无此字段 → 空串，
        # 调用方另有 completion_tokens 触顶启发式兜底。只读诊断，不改路由/记账。
        finish_reason = ""
        if isinstance(result, dict):
            finish_reason = str(result.get("finish_reason") or "")
            if not finish_reason:
                choices = result.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    finish_reason = str(choices[0].get("finish_reason") or "")
        receipt = receipt_of(
            result, module=module, event=event, task=task,
            request_id=request_id, attempts=attempts, duration_ms=duration)

        def build(ok: bool, text: str, category: str, error: str = "",
                  reasoning: str = "", request_sent: bool = True,
                  consumed: bool = True) -> LLMResult:
            return LLMResult(
                ok=ok, text=text, category=category, request_id=request_id,
                error=error, task_name=task, model=routed_model,
                reasoning=reasoning, prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens, total_tokens=total_tokens,
                usage_available=usage_available, request_sent=request_sent,
                consumed=consumed, attempts=attempts,
                strict_params=strict_params, module=module, event=event,
                cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss,
                duration_ms=duration, receipt=receipt, finish_reason=finish_reason)

        if not isinstance(result, dict):
            detail = "宿主返回非字典结果"
            try:
                self._ctx.logger.warning(f"LLM 生成失败: {detail}（{type(result).__name__}）")
            except Exception:
                pass
            self._log(event, task, False, "", detail)
            return build(False, "", "general_error", detail,
                         consumed=usage_available)

        if result.get("success"):
            response = str(result.get("response") or "").strip()
            reasoning = str(result.get("reasoning") or "").strip()
            if response:
                self._log(event, task, True, response)
                return build(True, response, "ok", reasoning=reasoning)
            detail = "模型返回空内容（可能被 max_tokens 截断，或任务模型未产出正文）"
            if reasoning:
                detail += f"；思考尾部: {reasoning[-160:]}"
            try:
                self._ctx.logger.warning(f"LLM 生成失败: {detail}")
            except Exception:
                pass
            self._log(event, task, False, "", detail)
            return build(False, "", "empty", detail, reasoning=reasoning)

        error = str(result.get("error") or "宿主返回失败但未携带 error 信息")
        category = classify_llm_error(error, result.get("error_code") or result.get("code"))
        try:
            self._ctx.logger.warning(f"LLM 生成失败: {error}")
        except Exception:
            pass
        self._log(event, task, False, "", error)
        if category == "task_missing" and not usage_available:
            # 任务解析失败发生在模型请求之前：明确未发出/无消耗，可安全释放预占
            return build(False, "", category, error, request_sent=False, consumed=False)
        return build(False, "", category, error, consumed=usage_available or True)

    def _record_usage(self, event: str, task: str, result: Any, *,
                      success: bool = False, module: str = "",
                      duration_ms: int = 0, attempts: int = 1) -> None:
        record_response(
            self._usage_tracker, event=event, module=module, task=task,
            result=result, success=success, duration_ms=duration_ms,
            attempts=attempts)

    def _log(self, event: str, task: str, success: bool,
             response: str = "", error: str = "") -> None:
        if self._call_logger is not None:
            try:
                self._call_logger.record(event=event, task=task, success=success,
                                         response=response, error=error)
            except Exception:
                pass

    def _validate_module(self, module: Any) -> str:
        """模块名必须是固定枚举；缺失/非法记 unknown 并留 warning。"""
        raw = str(module or "").strip().lower()
        if raw in MODULE_VALUES:
            return raw
        try:
            # F7（2026-11-04）：f-string 预格式化，不依赖宿主 logger 支持惰性
            # %-format（不支持时 TypeError 会被吞掉，unknown 诊断将丢失）。
            self._ctx.logger.warning(
                f"LLM 调用未标注有效模块（{(raw or None)!r}）；按 unknown 记账，请补 module=。")
        except Exception:
            pass
        return MODULE_UNKNOWN
