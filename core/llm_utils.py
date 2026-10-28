"""LLM 调用封装（**插件内所有模型调用的唯一出口**）。

统一走宿主 ctx.llm.generate，按开发文档 §8.3 的推荐写法：
- 显式传 task_name（模型任务名路由，享受任务级 model_list 自动兜底）；
- 具体模型名做成可选配置，默认留空（指定模型后宿主不再跨模型兜底，需自己降级）；
- 失败返回 {"success": False, "error": ...} 字典，不抛异常。

带 system prompt 时必须用消息列表传 prompt（宿主对 str prompt 不处理 system 字段）。

2026-10-10 配置整理轮：本模块成为「所有 LLM 调用集中模块」——
- 任务名/判定参数统一读顶层 ``[llm]`` 段（:func:`llm_task_name_of` / :func:`purpose_task`
  与 :meth:`LLMService.judge`；旧 ``[judge]`` / 各模块段落仅在自身仍存在时兜底）；
- 原绕过本模块的两处调用收编：角色裁判（planning/role_judge.py 改走 LLMService）与
  屏幕截图理解（:meth:`LLMService.describe_image`，原 companion/cateye_client.py 内联）。

合并自 mai-love v2.6.2 / v2.6.3：
- generate 失败三态分诊（v2.6.2）——"成功但正文为空"不再落进 result.get("error",
  "未知错误") 兜底误导排查；宿主裸失败（无 error 字段）与非 dict 结果单独识别；
- generate 增加可选 model 覆盖参数（v2.6.3）——只传 model 不传 task_name，
  宿主按「先任务名、未命中按具体模型名（任务回落 utils）」双解析；
- judge()（v2.6.3）——判定类调用（想念把关等）专用入口。
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from core.llm_usage import LLMUsageTracker

#: 请求封装/聊天模板/特殊 token 的保守预留（不是估算，是固定额外上界）
REQUEST_WRAPPER_TOKENS = 1024

#: 只有宿主明确表示「任务路由不存在」才允许调用方做一次 utils 回退。
TASK_MISSING_CODES = frozenset({"task_missing", "unknown_task"})
#: 官方文档（v1.3.2 附录 B）唯一给出的任务解析失败文案：反引号 + 「的模型配置」。
#: 具体模型不存在是「未找到名为 'xxx' 的模型」（无「的模型配置」），绝不能当任务缺失。
_TASK_MISSING_MESSAGE_RE = re.compile(r"未找到名为\s*`[^`]*`\s*的模型配置")


def prepare_prompt_arg(prompt: Any, system_prompt: str = "") -> Any:
    """按宿主约定把 str prompt + system 转成消息列表（唯一构造点）。"""
    if not system_prompt:
        return prompt
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]


def prompt_payload_bytes(prompt_arg: Any) -> int:
    """实际传给 SDK 的 prompt 载荷 UTF-8 字节数（不含 RPC 封包）。"""
    if isinstance(prompt_arg, str):
        return len(prompt_arg.encode("utf-8"))
    try:
        text = json.dumps(prompt_arg, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        text = str(prompt_arg)
    return len(text.encode("utf-8"))


def request_token_upper_bound(prompt: Any, *, system_prompt: str = "",
                              max_tokens: int = 512,
                              wrapper_tokens: int = REQUEST_WRAPPER_TOKENS) -> int:
    """一次请求的保守 token 上界：prompt 载荷字节数 + 固定封装预留 + 最大输出。

    字节上界依据：主流 BPE/byte-level 分词器的每个 token 至少覆盖 1 个 UTF-8
    字节，因此 token 数 <= 字节数。它不是精确 tokenizer 计数，但可证明不会低估
    文本部分；封装固定预留覆盖 JSON-RPC/聊天模板/特殊 token。实际 usage 可用时
    调用方应按真实 usage 结算，不用本函数回填。
    """
    try:
        output = max(0, int(max_tokens))
    except (TypeError, ValueError):
        output = 512
    wrapper = max(0, int(wrapper_tokens))
    return prompt_payload_bytes(prepare_prompt_arg(prompt, system_prompt)) + wrapper + output


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


@dataclass(frozen=True)
class LLMResult:
    """单次（含内部降级）LLM 调用的请求关联详细结果。

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


def _optional_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def extract_usage(result: Any) -> tuple[Optional[int], Optional[int], Optional[int], bool]:
    """从宿主结果读真实 usage：兼容顶层字段与 OpenAI 风格 ``usage`` 子字典。

    返回 ``(prompt, completion, total, available)``。``available`` 只在 total 可得，
    或 prompt/completion 同时可得时为 True；部分字段缺失时保持 None，预算层不会
    凭估算释放预占。
    """
    if not isinstance(result, dict):
        return None, None, None, False
    usage = result.get("usage")
    sources = (result, usage if isinstance(usage, dict) else {})

    def pick(names: tuple[str, ...]) -> Optional[int]:
        for source in sources:
            for name in names:
                if source.get(name) is not None:
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
    return prompt, completion, total, available


def _llm_section(config_or_plugin: Any) -> Any:
    """取 ``[llm]`` 段（接受 plugin 实例或配置对象；取不到返回 None）。"""
    section = getattr(config_or_plugin, "llm", None)
    if section is not None:
        return section
    return getattr(getattr(config_or_plugin, "config", None), "llm", None)


def llm_task_name_of(config_or_plugin: Any) -> str:
    """主模型任务名（``[llm].task_name``；旧 ``[schedule].llm_task_name`` 兜底，最后 replyer）。"""
    section = _llm_section(config_or_plugin)
    value = getattr(section, "task_name", None)
    if value is None:
        schedule = getattr(config_or_plugin, "schedule", None)
        if schedule is None:
            schedule = getattr(getattr(config_or_plugin, "config", None), "schedule", None)
        value = getattr(schedule, "llm_task_name", None)
    return str(value or "replyer")


def purpose_task(config_or_plugin: Any, purpose: str, default: str = "utils") -> str:
    """按用途取任务名（``[llm]`` 段的专用字段；缺失时回退到同模块旧字段，最后 default）。

    purpose ∈ {``review`` 好感裁判, ``emotion_referee`` 情绪睡眠裁判,
    ``emotion_evaluator`` 情绪增量评估, ``qzone`` 空间, ``vlm`` 视觉}。
    """
    section = _llm_section(config_or_plugin)
    config = getattr(config_or_plugin, "config", None) or config_or_plugin
    legacy_section_map: Dict[str, str] = {
        "review": "affection",
        "emotion_referee": "emotion",
        "emotion_evaluator": "emotion",
        "qzone": "qzone",
        "vlm": "cateye",
    }
    legacy_attr_map: Dict[str, str] = {
        "review": "review_task_name",
        "emotion_referee": "referee_task_name",
        "emotion_evaluator": "evaluator_task_name",
        "qzone": "llm_task_name",
        "vlm": "vlm_task",
    }
    if section is not None:
        value = getattr(section, f"{purpose}_task_name", None)
        if value is None and purpose == "vlm":
            value = getattr(section, "vlm_task", None)
        if value:
            return str(value)
    legacy = getattr(config, legacy_section_map.get(purpose, ""), None)
    value = getattr(legacy, legacy_attr_map.get(purpose, ""), None)
    if value:
        return str(value)
    return default


class LLMService:
    def __init__(
        self,
        ctx: Any,
        *,
        task_name: str = "utils",
        model_name: str = "",
        call_logger: Any = None,
        config_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._ctx = ctx
        self._task_name = task_name
        self._model_name = model_name
        self._call_logger = call_logger  # LLMCallLogger（可选）
        #: 返回插件实时配置的回调（judge() 读 [judge] 段用；None = 全部走内置默认）
        self._config_provider = config_provider
        # F3（2026-10-11）：LLM 消耗账本（无配置项；data_dir 不可得 → None = 统计降级不可用）
        self._usage_tracker: Optional[LLMUsageTracker] = None
        try:
            data_dir = getattr(getattr(self._ctx, "paths", None), "data_dir", None)
            if data_dir:
                self._usage_tracker = LLMUsageTracker(data_dir)
        except Exception:
            self._usage_tracker = None

    def update_task(self, task_name: str, model_name: str = "") -> None:
        self._task_name = task_name or "utils"
        self._model_name = model_name or ""

    async def generate(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        task_name: str = "",
        temperature: float = 0.7,
        max_tokens: int = 512,
        timeout_ms: Optional[int] = None,
        event: str = "unspecified",
        model: str = "",
        strict_params: bool = False,
    ) -> str:
        """生成文本；失败/异常统一返回空串，旧调用契约与参数优先级保持不变。

        新增可选 ``strict_params`` 仅给需要「单请求 + 参数不漂移」的调用方（事件库）：
        严格模式下忽略实例级 ``model_name`` 自动降级，只把调用方给的值原样发出；
        默认 False 时行为与旧版完全一致。

        完整错误分类/usage 请用 :meth:`generate_detailed`；本方法内部即走该入口。
        """
        result = await self.generate_detailed(
            prompt,
            system_prompt=system_prompt,
            task_name=task_name,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_ms=timeout_ms,
            event=event,
            model=model,
            strict_params=strict_params,
        )
        return result.text

    async def generate_detailed(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        task_name: str = "",
        temperature: float = 0.7,
        max_tokens: int = 512,
        timeout_ms: Optional[int] = None,
        event: str = "unspecified",
        model: str = "",
        strict_params: bool = False,
    ) -> LLMResult:
        """生成文本并返回本次请求关联的详细结果。

        与 :meth:`generate` 的差别只在返回值：文本、错误类别、usage、请求 id 均来自
        同一次响应；不额外探测任务/模型是否存在，不读写共享错误状态。

        ``strict_params=True``：只发一次宿主请求；不套实例级 ``model_name`` 回退，
        不改写调用方给出的 temperature/max_tokens（max_tokens 的业务上限由调用方
        负责，事件库会夹到 1200）。
        """
        request_id = uuid.uuid4().hex
        prompt_arg = prepare_prompt_arg(prompt, system_prompt)
        routed_task = str(task_name or self._task_name or "utils")
        override = str(model or "").strip()
        attempts = 0

        if strict_params:
            kwargs: dict[str, Any] = {
                "prompt": prompt_arg,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if override:
                kwargs["model"] = override
            else:
                kwargs["task_name"] = routed_task
            if timeout_ms:
                kwargs["timeout_ms"] = int(timeout_ms)
            attempts = 1
            try:
                result = await self._ctx.llm.generate(**kwargs)
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                return self._exception_result(exc, event=event, task=routed_task,
                                              request_id=request_id, attempts=attempts,
                                              category="timeout", strict_params=True)
            except Exception as exc:
                return self._exception_result(exc, event=event, task=routed_task,
                                              request_id=request_id, attempts=attempts,
                                              category="general_error", strict_params=True)
            return self._result_from_response(
                result, event=event, task=routed_task, request_id=request_id,
                attempts=attempts, strict_params=True,
                requested_model=override)

        # ---- 以下为旧 generate 的解析/降级优先级，逐字保留 ----
        if override:
            attempts = 1
            try:
                result = await self._ctx.llm.generate(
                    prompt=prompt_arg,
                    model=override,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, TimeoutError) as exc:
                return self._exception_result(exc, event=event, task=override,
                                              request_id=request_id, attempts=attempts,
                                              category="timeout", strict_params=False)
            except Exception as exc:
                return self._exception_result(exc, event=event, task=override,
                                              request_id=request_id, attempts=attempts,
                                              category="general_error", strict_params=False)
            return self._result_from_response(
                result, event=event, task=override, request_id=request_id,
                attempts=attempts, strict_params=False, requested_model=override)

        kwargs = {
            "prompt": prompt_arg,
            "task_name": routed_task,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if self._model_name:
            # 指定具体模型：失败后降级为纯任务路由再试一次（旧语义）
            attempts = 1
            try:
                result = await self._ctx.llm.generate(**{**kwargs, "model": self._model_name})
                if isinstance(result, dict) and result.get("success"):
                    return self._result_from_response(
                        result, event=event, task=routed_task, request_id=request_id,
                        attempts=attempts, strict_params=False,
                        requested_model=self._model_name)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        if timeout_ms:
            kwargs["timeout_ms"] = int(timeout_ms)
        attempts += 1
        try:
            result = await self._ctx.llm.generate(**kwargs)
        except asyncio.CancelledError:
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            return self._exception_result(exc, event=event, task=routed_task,
                                          request_id=request_id, attempts=attempts,
                                          category="timeout", strict_params=False)
        except Exception as exc:
            return self._exception_result(exc, event=event, task=routed_task,
                                          request_id=request_id, attempts=attempts,
                                          category="general_error", strict_params=False)
        return self._result_from_response(
            result, event=event, task=routed_task, request_id=request_id,
            attempts=attempts, strict_params=False,
            requested_model=self._model_name)

    def upper_bound_tokens(self, prompt: Any, *, system_prompt: str = "",
                           max_tokens: int = 512,
                           wrapper_tokens: int = REQUEST_WRAPPER_TOKENS) -> int:
        """本次请求（与实际发给 SDK 的 messages 同构）的保守 token 上界。"""
        return request_token_upper_bound(prompt, system_prompt=system_prompt,
                                         max_tokens=max_tokens,
                                         wrapper_tokens=wrapper_tokens)

    def _exception_result(self, exc: BaseException, *, event: str, task: str,
                          request_id: str, attempts: int, category: str,
                          strict_params: bool) -> LLMResult:
        error = str(exc) or type(exc).__name__
        self._log(event, task, False, "", error)
        try:
            self._ctx.logger.warning(f"LLM 调用异常: {error}")
        except Exception:
            pass
        return LLMResult(ok=False, text="", category=category, request_id=request_id,
                         error=error, task_name=task, request_sent=True,
                         consumed=True, attempts=attempts,
                         strict_params=strict_params)

    def _result_from_response(self, result: Any, *, event: str, task: str,
                              request_id: str, attempts: int, strict_params: bool,
                              requested_model: str = "") -> LLMResult:
        """把一次宿主响应翻译成 LLMResult（唯一记账/记日志出口）。"""
        self._record_usage(event, task, result)
        prompt_tokens, completion_tokens, total_tokens, usage_available = extract_usage(result)
        response_model = ""
        if isinstance(result, dict):
            response_model = str(result.get("model") or result.get("model_name") or "")
        routed_model = response_model or requested_model

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
                strict_params=strict_params)

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

    def _interpret(self, result: Any, event: str, task: str) -> str:
        """旧入口：保留「宿主返回 → 正文」语义，内部复用同一详细结果。"""
        return self._result_from_response(
            result, event=event, task=task, request_id=uuid.uuid4().hex,
            attempts=1, strict_params=False).text

    def _record_usage(self, event: str, task: str, result: Any) -> None:
        """F3：把宿主能力载荷里的真实用量字段记入消耗账本。

        兼容顶层 ``prompt_tokens/completion_tokens/total_tokens`` 与 OpenAI 风格
        ``usage`` 子字典；宿主没给用量时只计调用次数，不伪造 token。
        """
        tracker = self._usage_tracker
        if tracker is None or not isinstance(result, dict):
            return
        try:
            prompt_tokens, completion_tokens, total_tokens, usage_available = extract_usage(result)
            tracker.record(event=event, model=result.get("model_name") or result.get("model"),
                           prompt_tokens=prompt_tokens,
                           completion_tokens=completion_tokens,
                           total_tokens=total_tokens, usage_available=usage_available,
                           request_id=result.get("request_id") or result.get("id") or "")
        except Exception:
            try:
                self._ctx.logger.debug("LLM 消耗记账失败（忽略）", exc_info=True)
            except Exception:
                pass

    def usage_summary(self, hours: int = 24) -> Dict[str, Any]:
        """最近 ``hours`` 小时的消耗聚合（面板指令渲染用；账本缺失 → 空账本）。"""
        if self._usage_tracker is None:
            return {"calls": 0, "tokens": 0, "models": {}, "modules": {}, "has_usage": False}
        try:
            return self._usage_tracker.summary(hours)
        except Exception:
            return {"calls": 0, "tokens": 0, "models": {}, "modules": {}, "has_usage": False}

    async def judge(self, prompt: str, *, event: str = "judge") -> str:
        """判定类调用（当前唯一调用点 = 想念把关；角色裁判/睡眠裁判走各自任务名，不读本入口）：
        模型与参数来自顶层 ``[llm]`` 段。

        与 :meth:`generate` 的区别：调用方只给提示词拿结论；用哪个模型、最大
        token 数、温度集中在配置里（config_provider 动态读，热更新即时生效）——
        判定要的是稳定结论，与生成类调用的参数策略解耦。
        旧 ``[judge]`` 段（model/max_tokens/temperature）在 ``[llm]`` 段对应字段缺省时兜底。

        Returns:
            判定文本，失败返回空字符串（调用方按 fail-closed 处理）。
        """
        cfg = None
        if self._config_provider is not None:
            try:
                cfg = self._config_provider()
            except Exception:
                cfg = None
        section = _llm_section(cfg) if cfg is not None else None
        legacy = getattr(cfg, "judge", None) if cfg is not None else None

        model = str(
            getattr(section, "judge_model", None)
            or getattr(legacy, "model", "")
            or ""
        ).strip()
        try:
            max_tokens = int(getattr(section, "judge_max_tokens", None)
                             or getattr(legacy, "max_tokens", 512))
        except (TypeError, ValueError):
            max_tokens = 512
        try:
            temperature = float(getattr(section, "judge_temperature", None)
                                if getattr(section, "judge_temperature", None) is not None
                                else getattr(legacy, "temperature", 0.2))
        except (TypeError, ValueError):
            temperature = 0.2
        return await self.generate(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            event=event,
            model=model,
        )

    async def describe_image(
        self,
        image_base64: str,
        prompt: str,
        *,
        task_name: str = "",
        temperature: float = 0.3,
        max_tokens: int = 512,
        event: str = "describe_image",
    ) -> str:
        """视觉理解：把一张 base64 图片 + 提示词交给视觉模型，返回一句话描述。

        2026-10-10 由 companion/cateye_client.py 收编进本模块（所有 LLM 调用的唯一出口）。
        **消息形态与原实现逐字一致**（Data URL 的 ``image_url`` 段），避免宿主多模态解析口径漂移；
        失败返回空串由调用方降级。
        """
        content = [
            {"type": "text", "text": str(prompt or "")},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"},
            },
        ]
        kwargs: dict[str, Any] = {
            "prompt": [{"role": "user", "content": content}],
            "task_name": task_name or self._task_name,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        try:
            result = await self._ctx.llm.generate(**kwargs)
        except Exception as e:
            self._log(event, str(task_name or self._task_name), False, "", str(e))
            try:
                self._ctx.logger.warning(f"视觉理解调用异常: {e}")
            except Exception:
                pass
            return ""
        return self._interpret(result, event, str(task_name or self._task_name))

    def _log(self, event: str, task: str, success: bool, response: str = "", error: str = "") -> None:
        if self._call_logger is not None:
            try:
                self._call_logger.record(event=event, task=task, success=success, response=response, error=error)
            except Exception:
                pass

    async def generate_or_fallback(self, prompt: str, fallback: str, **kwargs: Any) -> str:
        text = await self.generate(prompt, **kwargs)
        text = text.strip()
        return text if text else fallback
