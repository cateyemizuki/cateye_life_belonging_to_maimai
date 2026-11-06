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
from collections.abc import Sequence
from typing import Any, Callable, Dict, Optional

from core.llm_usage import (
    DEFAULT_USAGE_WINDOW_HOURS,
    KEEP_HOURS,
    LLMUsageTracker,
)


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
    #: 账本 v2（2026-11-02）：模块/事件/缓存/耗时与不可变回执；旧调用方零感知
    module: str = ""
    event: str = ""
    cache_hit_tokens: Optional[int] = None
    cache_miss_tokens: Optional[int] = None
    duration_ms: int = 0
    receipt: Optional[Any] = None


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
        usage_keep_hours: Optional[int] = None,
        usage_default_window_hours: Optional[int] = None,
    ) -> None:
        self._ctx = ctx
        self._task_name = task_name
        self._model_name = model_name
        self._call_logger = call_logger  # LLMCallLogger（可选）
        #: 返回插件实时配置的回调（judge() 读 [judge] 段用；None = 全部走内置默认）
        self._config_provider = config_provider
        self._default_window_hours = int(
            usage_default_window_hours or DEFAULT_USAGE_WINDOW_HOURS)
        # F3（2026-10-11）：LLM 消耗账本（data_dir 不可得 → None = 统计降级不可用）。
        # 2026-11-02 账本 v2：保留期由 [llm].usage_retention_hours 控制（默认 30 天）。
        self._usage_tracker: Optional[LLMUsageTracker] = None
        try:
            data_dir = getattr(getattr(self._ctx, "paths", None), "data_dir", None)
            if data_dir:
                self._usage_tracker = LLMUsageTracker(
                    data_dir,
                    keep_hours=int(usage_keep_hours or KEEP_HOURS))
        except Exception:
            self._usage_tracker = None
        #: 统一入口（懒建：LLMService 保留薄壳签名，内部唯一出口在 tools.llm_gateway）
        self._llm_gateway: Optional[Any] = None

    def _gateway(self) -> Any:
        """懒建 Gateway（避免 core 与 tools 的模块级循环导入）。"""
        gateway = getattr(self, "_llm_gateway", None)
        if gateway is None:
            from tools.llm_gateway import LLMGateway
            gateway = LLMGateway(
                self._ctx,
                task_name=getattr(self, "_task_name", "utils"),
                model_name=getattr(self, "_model_name", ""),
                call_logger=getattr(self, "_call_logger", None),
                usage_tracker=getattr(self, "_usage_tracker", None),
                optimization_enabled_provider=lambda: bool(getattr(
                    _llm_section(self._config_provider()) if self._config_provider else None,
                    "prompt_optimization_enabled", True)),
            )
            self._llm_gateway = gateway
        return gateway

    def update_task(self, task_name: str, model_name: str = "") -> None:
        self._task_name = task_name or "utils"
        self._model_name = model_name or ""
        gateway = getattr(self, "_llm_gateway", None)
        if gateway is not None:
            gateway.update_task(self._task_name, self._model_name)

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
    ) -> str:
        """生成文本；失败/异常统一返回空串（旧契约不变，统一入口在 Gateway）。

        ``module`` = 调用方业务模块（planning/emotion/qzone/companion/video/core）；
        缺失时 Gateway 记 ``unknown`` 并留 warning。``strict_params`` 仅给需要
        「单请求 + 参数不漂移」的调用方（事件库），语义与旧版逐字一致。
        """
        result = await self.generate_detailed(
            prompt, module=module, system_prompt=system_prompt,
            task_name=task_name, temperature=temperature,
            timeout_ms=timeout_ms, event=event, model=model,
            strict_params=strict_params,
        )
        return result.text

    async def generate_detailed(
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
    ) -> LLMResult:
        """生成文本并返回本次请求关联的详细结果（委托 tools.llm_gateway）。

        路由/降级/失败三态与旧实现逐字一致；新增 ``module`` 进入账本 v2，
        ``LLMResult.receipt`` 携带模块、缓存命中与耗时等对账字段。
        """
        return await self._gateway().generate_detailed(
            prompt, module=module, system_prompt=system_prompt,
            task_name=task_name, temperature=temperature,
            timeout_ms=timeout_ms, event=event, model=model,
            strict_params=strict_params,
        )

    def _exception_result(self, exc: BaseException, *, event: str, task: str,
                          request_id: str, attempts: int, category: str,
                          strict_params: bool, module: str = "") -> LLMResult:
        """兼容旧直接调用：委托 Gateway 构造失败结果（不伪造用量、照常留日志）。"""
        import time as _time
        return self._gateway()._exception_result(
            exc, event=event, task=task, request_id=request_id,
            attempts=attempts, category=category, strict_params=strict_params,
            module=str(module or "unknown"), started=_time.monotonic())

    def _result_from_response(self, result: Any, *, event: str, task: str,
                              request_id: str, attempts: int, strict_params: bool,
                              requested_model: str = "", module: str = "") -> LLMResult:
        """兼容旧直接调用：委托 Gateway 翻译宿主响应并写账本 v2。"""
        import time as _time
        return self._gateway()._result_from_response(
            result, event=event, task=task, request_id=request_id,
            attempts=attempts, strict_params=strict_params,
            requested_model=requested_model, module=str(module or "unknown"),
            started=_time.monotonic())

    def _interpret(self, result: Any, event: str, task: str) -> str:
        """旧入口：保留「宿主返回 → 正文」语义，内部复用同一详细结果。"""
        return self._result_from_response(
            result, event=event, task=task, request_id=uuid.uuid4().hex,
            attempts=1, strict_params=False).text

    def _record_usage(self, event: str, task: str, result: Any, *,
                      success: bool = False, module: str = "") -> None:
        """兼容旧直接调用：委托 Gateway 写账本 v2（traps 内吞，不打断业务）。"""
        try:
            self._gateway()._record_usage(
                event, task, result, success=success, module=module)
        except Exception:
            pass

    def usage_summary(self, hours: int = 0) -> Dict[str, Any]:
        """最近 ``hours`` 小时的消耗聚合；``0``/缺省 = [llm] 默认窗口（30 天）。"""
        window = int(hours or getattr(self, "_default_window_hours", DEFAULT_USAGE_WINDOW_HOURS))
        tracker = getattr(self, "_usage_tracker", None)
        try:
            if tracker is None:
                tracker = LLMUsageTracker(None)
            return tracker.summary(window)
        except Exception:
            return LLMUsageTracker(None).summary(window)

    def usage_rows(self, hours: int = 0, *, limit: int = 50,
                   offset: int = 0) -> Dict[str, Any]:
        """逐笔明细（WebUI 分页用）；账本缺失 → 空页。"""
        window = int(hours or getattr(self, "_default_window_hours", DEFAULT_USAGE_WINDOW_HOURS))
        tracker = getattr(self, "_usage_tracker", None)
        if tracker is None:
            tracker = LLMUsageTracker(None)
        try:
            return tracker.rows(window, limit=limit, offset=offset)
        except Exception:
            return {"window_hours": window, "total": 0, "offset": 0,
                    "limit": limit, "rows": [], "truncated": False}

    async def judge(self, prompt: str, *, event: str = "judge",
                    module: str = "companion") -> str:
        """判定类调用（当前唯一调用点 = 想念把关；角色裁判/睡眠裁判走各自任务名，不读本入口）：
        模型与参数来自顶层 ``[llm]`` 段。

        与 :meth:`generate` 的区别：调用方只给提示词拿结论；用哪个模型、温度集中在配置里（config_provider 动态读，热更新即时生效）——
        判定要的是稳定结论，与生成类调用的参数策略解耦。
        旧 ``[judge]`` 段（model/temperature）在 ``[llm]`` 段对应字段缺省时兜底。

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
            temperature = float(getattr(section, "judge_temperature", None)
                                if getattr(section, "judge_temperature", None) is not None
                                else getattr(legacy, "temperature", 0.2))
        except (TypeError, ValueError):
            temperature = 0.2
        return await self.generate(
            prompt,
            module=module,
            temperature=temperature,

            event=event,
            model=model,
        )

    async def describe_image(
        self,
        image_base64: str,
        prompt: str,
        *,
        extra_images: Optional[Sequence[str]] = None,
        task_name: str = "",
        temperature: float = 0.3,
        module: str = "companion",
        event: str = "describe_image",
    ) -> str:
        """视觉理解：把一张 base64 图片 + 提示词交给视觉模型，返回一句话描述。

        2026-10-10 由 companion/cateye_client.py 收编进本模块（所有 LLM 调用的唯一出口）。
        **消息形态与原实现逐字一致**（Data URL 的 ``image_url`` 段），避免宿主多模态解析口径漂移；
        失败返回空串由调用方降级。
        2026-10-30 多屏适配：新增 ``extra_images``（追加的 base64 图，按给出顺序排在首图之后，
        各自一个 ``image_url`` 段）；不传/空序列时消息形态与旧实现逐字一致，既有调用方零感知。
        """
        content: list[dict[str, Any]] = [
            {"type": "text", "text": str(prompt or "")},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_base64}"},
            },
        ]
        for extra in extra_images or ():
            if isinstance(extra, str) and extra.strip():
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{extra}"},
                })
        message = [{"role": "user", "content": content}]
        try:
            result = await self._gateway().generate_messages(
                message, module=module, task_name=str(task_name or self._task_name),
                temperature=temperature, event=event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(event, str(task_name or self._task_name), False, "", str(e))
            try:
                self._ctx.logger.warning(f"视觉理解调用异常: {e}")
            except Exception:
                pass
            return ""
        return result.text

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
