"""LLM 调用封装。

统一走宿主 ctx.llm.generate，按开发文档 §8.3 的推荐写法：
- 显式传 task_name（模型任务名路由，享受任务级 model_list 自动兜底）；
- 具体模型名做成可选配置，默认留空（指定模型后宿主不再跨模型兜底，需自己降级）；
- 失败返回 {"success": False, "error": ...} 字典，不抛异常。

带 system prompt 时必须用消息列表传 prompt（宿主对 str prompt 不处理 system 字段）。

合并自 mai-love v2.6.2 / v2.6.3：
- generate 失败三态分诊（v2.6.2）——"成功但正文为空"不再落进 result.get("error",
  "未知错误") 兜底误导排查；宿主裸失败（无 error 字段）与非 dict 结果单独识别；
- generate 增加可选 model 覆盖参数（v2.6.3）——只传 model 不传 task_name，
  宿主按「先任务名、未命中按具体模型名（任务回落 utils）」双解析；
- judge()（v2.6.3）——判定类调用（想念把关等）专用入口，模型 / 最大 token 数 /
  温度全部读顶层 [judge] 配置板块（经 config_provider 动态取，热更新即时生效）。
"""

from __future__ import annotations

from typing import Any, Callable, Optional


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
        """判定类调用（想念把关等）：模型与参数全部来自顶层 [judge] 配置板块。

        与 :meth:`generate` 的区别：调用方只给提示词拿结论；用哪个模型、最大
        token 数、温度集中在 [judge] 配置里（config_provider 动态读，热更新
        即时生效）——判定要的是稳定结论，与生成类调用的参数策略解耦。

        Returns:
            判定文本，失败返回空字符串（调用方按 fail-closed 处理）。
        """
        judge_cfg = None
        if self._config_provider is not None:
            try:
                judge_cfg = getattr(self._config_provider(), "judge", None)
            except Exception:
                judge_cfg = None
        model = str(getattr(judge_cfg, "model", "") or "").strip()
        try:
            max_tokens = int(getattr(judge_cfg, "max_tokens", 512))
        except (TypeError, ValueError):
            max_tokens = 512
        try:
            temperature = float(getattr(judge_cfg, "temperature", 0.2))
        except (TypeError, ValueError):
            temperature = 0.2
        return await self.generate(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            event=event,
            model=model,
        )

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
