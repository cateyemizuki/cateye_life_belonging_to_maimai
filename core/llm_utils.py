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

from typing import Any, Callable, Dict, Optional


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
    ) -> str:
        """生成文本；失败/异常统一返回空串，由调用方走降级路径。

        Args:
            model: 模型覆盖（宿主模型任务名或 WebUI 模型列表里的具体模型名）；
                留空 = 走默认任务路由（task_name / 配置的具体模型）。非空时**只传
                model 不传 task_name**——宿主先按任务名解析，未命中按具体模型名
                （任务回落 utils），因此 judge.model 填任务名或模型名都合法。
        """
        prompt_arg: Any = prompt
        if system_prompt:
            prompt_arg = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
        override = str(model or "").strip()
        try:
            if override:
                # 覆盖路径：宿主双解析（任务名 → 具体模型名 → 任务回落 utils）
                result = await self._ctx.llm.generate(
                    prompt=prompt_arg,
                    model=override,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                return self._interpret(result, event, override)
            kwargs: dict[str, Any] = {
                "prompt": prompt_arg,
                "task_name": task_name or self._task_name,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            task = kwargs["task_name"]
            if self._model_name:
                # 指定具体模型：失败后降级为纯任务路由再试一次
                try:
                    result = await self._ctx.llm.generate(**{**kwargs, "model": self._model_name})
                    if isinstance(result, dict) and result.get("success"):
                        return self._interpret(result, event, task)
                except Exception:
                    pass
            if timeout_ms:
                kwargs["timeout_ms"] = int(timeout_ms)
            result = await self._ctx.llm.generate(**kwargs)
        except Exception as e:
            self._log(event, override or task_name or self._task_name, False, "", str(e))
            try:
                self._ctx.logger.error(f"LLM 调用异常: {e}")
            except Exception:
                pass
            return ""
        return self._interpret(result, event, override or task_name or self._task_name)

    def _interpret(self, result: Any, event: str, task: str) -> str:
        """把宿主返回翻译成正文；失败三态分诊（mai-love v2.6.2 同款）。

        - 成功且有正文 → 记录并返回（去首尾空白）；
        - 成功但正文为空 → 单独识别：思考型模型会把小 max_tokens 预算花在
          reasoning 上导致正文为空，此前落到 result.get("error", "未知错误")
          兜底，日志只剩"未知错误"误导排查；reasoning 尾部 160 字作为证据；
        - 宿主裸失败（无 error 字段）→ 不再冒充"未知错误"；
        - 非 dict 结果 → 防御性按失败处理，不抛异常。
        """
        if not isinstance(result, dict):
            detail = "宿主返回非字典结果"
            try:
                self._ctx.logger.warning(f"LLM 生成失败: {detail}（{type(result).__name__}）")
            except Exception:
                pass
            self._log(event, task, False, "", detail)
            return ""
        if result.get("success"):
            response = str(result.get("response") or "").strip()
            if response:
                self._log(event, task, True, response)
                return response
            detail = "模型返回空内容（可能被 max_tokens 截断，或任务模型未产出正文）"
            reasoning = str(result.get("reasoning") or "").strip()
            if reasoning:
                detail += f"；思考尾部: {reasoning[-160:]}"
            try:
                self._ctx.logger.warning(f"LLM 生成失败: {detail}")
            except Exception:
                pass
            self._log(event, task, False, "", detail)
            return ""
        error = str(result.get("error") or "宿主返回失败但未携带 error 信息")
        try:
            self._ctx.logger.warning(f"LLM 生成失败: {error}")
        except Exception:
            pass
        self._log(event, task, False, "", error)
        return ""

    async def judge(self, prompt: str, *, event: str = "judge") -> str:
        """判定类调用（想念把关、角色裁判等）：模型与参数来自顶层 ``[llm]`` 段。

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
