"""恋人电脑联动客户端（来源：虚拟恋人插件 cateye_client.py）。

读取「cateye 统一连接插件」的电脑状态，经跨插件 API（``ctx.api.call``）：

- 先查 ``cateye.connect-hub.status``：``connected=false``（或 hub 未安装、调用失败）
  → 状态 ``offline``（「恋人的电脑没开」）；
- 已连接 → 调 ``cateye.connect-hub.screenshot`` 截屏（可配模糊半径），
  把截图交给视觉模型（vlm 任务）转成一句话描述；
- 截图失败 / 描述失败 → 状态 ``failed``（「没看清 TA 在干什么」）。

调用形态（SDK 约定）：``ctx.api.call`` 目标报错/不存在时返回
``{"success": False, "error": ...}``。本类统一折叠为安全降级，
任何异常都不外抛——电脑状态属于锦上添花，绝不能阻塞主动消息触发。

另含 :func:`describe_image`：多模态消息列表形式的 VLM 调用
（源插件放 LLMService.describe_image；本整合插件的 LLMService 在 core/llm_utils
是共享文件不允许扩展，故落在本模块）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from core.diag import LLMCallLogger

# cateye 统一连接插件（cateye.connect-hub）公共 API 全名
HUB_API_STATUS = "cateye.connect-hub.status"
HUB_API_SCREENSHOT = "cateye.connect-hub.screenshot"

# 连接状态 TTL 缓存：该间隔内的重复查询直接复用上次结果
_STATUS_TTL_SECONDS = 60.0

#: 看清楚了
PEEK_OK = "ok"
#: 电脑没开（客户端未连接 / hub 未安装 / 查询失败）
PEEK_OFFLINE = "offline"
#: 看不成（截图失败 / 视觉理解失败）
PEEK_FAILED = "failed"


@dataclass(frozen=True)
class ScreenPeek:
    """一次「看恋人电脑」的结果。"""

    #: 取值见 PEEK_OK / PEEK_OFFLINE / PEEK_FAILED
    status: str
    #: 仅 PEEK_OK 时非空：视觉模型给出的原始描述
    description: str = ""

    @property
    def ok(self) -> bool:
        """是否真的看清了（有可用描述）。"""
        return self.status == PEEK_OK and bool(self.description)


async def describe_image(
    ctx: Any,
    vlm_task: str,
    image_b64: str,
    prompt: str,
    *,
    call_logger: Optional[LLMCallLogger] = None,
    event: str = "cateye_screen_describe",
) -> str:
    """用视觉模型解读一张图片，返回一句话描述；失败返回空字符串。

    图片以多模态 content 段（Data URL 形式）传入；任务名走 ``task_name``
    路由（与 core/llm_utils 同一约定）。max_tokens 512：一句话描述用不完，
    但思考型模型在更小预算下会把 token 全花在思考上、正文为空。

    Args:
        ctx: PluginContext（ctx.llm.generate / ctx.logger）。
        vlm_task: 视觉模型任务名（宿主 model_config 的任务键）。
        image_b64: 图片 base64（PNG）。
        prompt: 视觉理解提示词。
        call_logger: LLM 调用日志记录器（可选）。
        event: 事件来源标注（写入调用日志）。
    """
    message: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                },
            ],
        }
    ]

    def _log(success: bool, response: str = "", error: str = "") -> None:
        if call_logger is None:
            return
        try:
            call_logger.record(event=event, task=vlm_task, success=success,
                               response=response, error=error)
        except Exception:
            pass

    try:
        result: Any = await ctx.llm.generate(
            prompt=message, task_name=vlm_task or "vlm", temperature=0.3, max_tokens=512
        )
    except Exception as e:
        ctx.logger.error(f"视觉模型调用异常: {e}")
        _log(False, "", str(e))
        return ""
    if isinstance(result, dict) and result.get("success"):
        response = str(result.get("response", "") or "").strip()
        if response:
            _log(True, response)
            return response
        # 成功但空回复单独识别：reasoning 通常是思考型模型把 token 花在思考上的直接证据
        detail = "模型返回空内容（可能被 max_tokens 截断，或截图无内容可描述）"
        reasoning = str(result.get("reasoning", "") or "").strip()
        if reasoning:
            detail += f"；思考尾部: {reasoning[-160:]}"
        ctx.logger.warning(f"视觉模型生成失败: {detail}")
        _log(False, "", detail)
        return ""
    error = str((result or {}).get("error") or "宿主返回失败但未携带 error 信息")
    ctx.logger.warning(f"视觉模型生成失败: {error}")
    _log(False, "", error)
    return ""


class CateyeClient:
    """恋人电脑读取器：在线判定 + 截图 + 视觉描述，带降级与超时保护。"""

    def __init__(self, plugin: Any, llm: Any = None, call_logger: Optional[LLMCallLogger] = None) -> None:
        """
        Args:
            plugin: 整合插件实例（读 plugin.config.cateye / plugin.config.target）。
            llm: 未直接使用（保留参数位以便未来复用 core LLMService）。
            call_logger: LLM 调用日志记录器（VLM 调用记录用）。
        """
        self._plugin = plugin
        self._ctx = plugin.ctx
        self._llm = llm
        self._call_logger = call_logger
        self._connected_cache: Optional[bool] = None
        self._connected_cached_at: float = 0.0

    # ------------------------------------------------------------
    # 配置（运行时动态读，热重载即时生效）
    # ------------------------------------------------------------
    def _cateye_cfg(self) -> Any:
        return getattr(self._plugin.config, "cateye", None)

    def _target_qq(self) -> str:
        return str(getattr(self._plugin.config.target, "target_qq", "") or "")

    # ------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------

    def is_enabled(self) -> bool:
        """功能开关（配置段缺失时安全回退 False）。"""
        return bool(getattr(self._cateye_cfg(), "enabled", False))

    async def peek_screen(self, now: datetime) -> Optional[ScreenPeek]:
        """看一眼恋人电脑。

        Returns:
            :class:`ScreenPeek`（ok / offline / failed）；
            功能关闭、整体超时或调用异常 → ``None``（本轮不注入任何屏幕内容）。
            超时不降级成 failed：按"这轮别提电脑"处理。
        """
        if not self.is_enabled():
            return None
        try:
            timeout = float(getattr(self._cateye_cfg(), "timeout_seconds", 20.0))
            return await asyncio.wait_for(self._peek(now), timeout=max(5.0, timeout))
        except asyncio.TimeoutError:
            self._ctx.logger.info("查看恋人电脑超时，本轮不注入屏幕感知")
            return None
        except Exception as e:  # 绝不阻塞主动触发
            self._ctx.logger.warning(f"查看恋人电脑异常（忽略）: {e}")
            return None

    # ------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------

    async def _peek(self, now: datetime) -> ScreenPeek:
        """实际的状态获取流程（已在功能开关通过后调用）。"""
        if not await self.is_connected():
            self._ctx.logger.info("恋人电脑未连接（或 cateye 插件不可用），本轮按「电脑没开」处理")
            return ScreenPeek(PEEK_OFFLINE)

        image_b64 = await self._take_screenshot()
        if not image_b64:
            self._ctx.logger.info("恋人电脑截图失败，本轮按「没看清」处理")
            return ScreenPeek(PEEK_FAILED)

        description = await self._describe_screenshot(image_b64)
        if not description:
            self._ctx.logger.info("屏幕截图视觉理解失败（宿主可能不支持图片输入），本轮按「没看清」处理")
            return ScreenPeek(PEEK_FAILED)

        self._ctx.logger.info(f"恋人电脑状态（{now.strftime('%H:%M')}）：{description}")
        return ScreenPeek(PEEK_OK, description)

    async def is_connected(self) -> bool:
        """本地客户端是否在线（status API，带 60 秒 TTL 缓存）。

        hub 未安装 / 调用异常 / 字段缺失一律视为未连接。
        """
        now_ts = time.monotonic()
        if (
            self._connected_cache is not None
            and now_ts - self._connected_cached_at < _STATUS_TTL_SECONDS
        ):
            return self._connected_cache

        connected = False
        try:
            resp = await self._ctx.api.call(HUB_API_STATUS)
            connected = bool(isinstance(resp, dict) and resp.get("connected"))
        except Exception as e:
            self._ctx.logger.debug(f"查询 cateye 连接状态失败（视为未连接）: {e}")

        self._connected_cache = connected
        self._connected_cached_at = now_ts
        return connected

    async def _take_screenshot(self) -> str:
        """截取恋人电脑屏幕，成功返回 base64 PNG，失败返回空串。"""
        blur = int(getattr(self._cateye_cfg(), "screenshot_blur", 0) or 0)
        user_id = self._target_qq()
        try:
            resp = await self._ctx.api.call(HUB_API_SCREENSHOT, user_id=user_id, blur=blur)
        except Exception as e:
            self._ctx.logger.debug(f"cateye 截图调用异常: {e}")
            return ""
        if not isinstance(resp, dict) or not resp.get("success"):
            error = resp.get("error") if isinstance(resp, dict) else type(resp).__name__
            self._ctx.logger.debug(f"cateye 截图失败: {error}")
            return ""
        return str(resp.get("image_base64", "") or "")

    async def _describe_screenshot(self, image_b64: str) -> str:
        """用视觉模型把截图转成一句话描述，失败返回空串。"""
        cfg = self._cateye_cfg()
        prompt = str(getattr(cfg, "describe_prompt", "") or "")
        vlm_task = str(getattr(cfg, "vlm_task", "vlm") or "vlm")
        return await describe_image(
            self._ctx, vlm_task, image_b64, prompt,
            call_logger=self._call_logger, event="cateye_screen_describe",
        )
