"""陪伴对象电脑联动客户端（来源：虚拟陪伴插件 cateye_client.py）。

读取「cateye 统一连接插件」的电脑状态，经跨插件 API（``ctx.api.call``）：

- 先查 ``cateye.connect-hub.status``：``connected=false``（或 hub 未安装、调用失败）
  → 状态 ``offline``（「陪伴对象的电脑没开」）；
- 已连接 → 调 ``cateye.connect-hub.screenshot`` 截屏（可配模糊半径），
  把截图交给视觉模型（vlm 任务）转成一句话描述；
  2026-10-30 多屏适配：对端 v1.1.0 起逐屏抓取，``[patrol].screen_scope``
  = ``main``（默认，只看主屏）或 ``all``（全部屏幕一次交给视觉模型分屏描述）；
- 截图失败 / 描述失败 → 状态 ``failed``（「没看清 TA 在干什么」）。

调用形态（SDK 约定）：``ctx.api.call`` 目标报错/不存在时返回
``{"success": False, "error": ...}``。本类统一折叠为安全降级，
任何异常都不外抛——电脑状态属于锦上添花，绝不能阻塞主动消息触发。

另含 :func:`describe_image`：多模态消息列表形式的 VLM 调用
（2026-10-10 起 describe_image 收编到 core/llm_utils.LLMService；旧注释保留见下
是共享文件不允许扩展，故落在本模块）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from companion.config import target_qq_of
from core.diag import LLMCallLogger
from core.llm_utils import LLMService, purpose_task
from suite_prompts import prompt as prompt_of

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
    """一次「看陪伴对象电脑」的结果。"""

    #: 取值见 PEEK_OK / PEEK_OFFLINE / PEEK_FAILED
    status: str
    #: 仅 PEEK_OK 时非空：视觉模型给出的原始描述
    description: str = ""

    @property
    def ok(self) -> bool:
        """是否真的看清了（有可用描述）。"""
        return self.status == PEEK_OK and bool(self.description)


def _fallback_llm(plugin: Any) -> Any:
    """兜底 LLM 实例（服务束未注入时用 plugin._llm；两者都缺则 None）。"""
    existing = getattr(plugin, "_llm", None)
    if isinstance(existing, LLMService):
        return existing
    return None


class CateyeClient:
    """陪伴对象电脑读取器：在线判定 + 截图 + 视觉描述，带降级与超时保护。"""

    def __init__(self, plugin: Any, llm: Any = None, call_logger: Optional[LLMCallLogger] = None) -> None:
        """
        Args:
            plugin: 整合插件实例（读 plugin.config.patrol 的 screen_* / [llm] / [prompts]）。
            llm: 统一 LLMService（2026-10-10 起 VLM 调用经它发出）。
            call_logger: 保留参数（调用留痕已由 LLMService 的 call_logger 承担）。
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
    def _screen_cfg(self) -> Any:
        """屏幕感知配置（2026-10-10 起在 [patrol] 段，字段前缀 screen_*）。"""
        section = getattr(self._plugin.config, "patrol", None) or getattr(self._plugin.config, "cateye", None)
        return section

    def _target_qq(self) -> str:
        return target_qq_of(self._plugin.config)

    # ------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------

    def is_enabled(self) -> bool:
        """功能开关（配置段缺失时安全回退 False）。"""
        cfg = self._screen_cfg()
        value = getattr(cfg, "screen_enabled", None)
        if value is None:
            value = getattr(cfg, "enabled", False)   # 旧 [cateye].enabled 兜底
        return bool(value)

    async def peek_screen(self, now: datetime) -> Optional[ScreenPeek]:
        """看一眼陪伴对象电脑。

        Returns:
            :class:`ScreenPeek`（ok / offline / failed）；
            功能关闭、整体超时或调用异常 → ``None``（本轮不注入任何屏幕内容）。
            超时不降级成 failed：按"这轮别提电脑"处理。
        """
        if not self.is_enabled():
            return None
        try:
            cfg = self._screen_cfg()
            timeout = float(getattr(cfg, "screen_timeout_seconds", None)
                            or getattr(cfg, "timeout_seconds", 20.0))
            return await asyncio.wait_for(self._peek(now), timeout=max(5.0, timeout))
        except asyncio.TimeoutError:
            self._ctx.logger.info("查看陪伴对象电脑超时，本轮不注入屏幕感知")
            return None
        except Exception as e:  # 绝不阻塞主动触发
            self._ctx.logger.warning(f"查看陪伴对象电脑异常（忽略）: {e}")
            return None

    # ------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------

    async def _peek(self, now: datetime) -> ScreenPeek:
        """实际的状态获取流程（已在功能开关通过后调用）。"""
        if not await self.is_connected():
            self._ctx.logger.info("陪伴对象电脑未连接（或 cateye 插件不可用），本轮按「电脑没开」处理")
            return ScreenPeek(PEEK_OFFLINE)

        screens = await self._capture_screens()
        if not screens:
            self._ctx.logger.info("陪伴对象电脑截图失败，本轮按「没看清」处理")
            return ScreenPeek(PEEK_FAILED)

        selected = self._select_screens(screens)
        description = await self._describe_screens([s["image_base64"] for s in selected],
                                                   len(selected), len(screens))
        if not description:
            self._ctx.logger.info("屏幕截图视觉理解失败（宿主可能不支持图片输入），本轮按「没看清」处理")
            return ScreenPeek(PEEK_FAILED)

        self._ctx.logger.info(f"陪伴对象电脑状态（{now.strftime('%H:%M')}，{len(selected)}/{len(screens)} 屏）：{description}")
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

    async def _capture_screens(self) -> list:
        """截取陪伴对象电脑屏幕，成功返回规格化屏幕列表，失败返回空列表。

        对端 ``screenshot`` v2（多屏兼容）响应含 ``screens``（每屏一项，主屏恒第 0 位且
        ``primary=True``）与兼容键 ``image_base64``（恒为主屏）。此处统一规格化为
        ``[{"index", "primary", "image_base64"}]``：丢弃空图条目；对端过旧（无 ``screens``
        键）或负载降级（screens 空）时回退用 ``image_base64`` 合成单屏——两种取值下
        单屏客户端行为与改造前一致。
        """
        cfg = self._screen_cfg()
        blur = int(getattr(cfg, "screen_blur", None)
                   if getattr(cfg, "screen_blur", None) is not None
                   else (getattr(cfg, "screenshot_blur", 0) or 0))
        user_id = self._target_qq()
        try:
            resp = await self._ctx.api.call(HUB_API_SCREENSHOT, user_id=user_id, blur=blur)
        except Exception as e:
            self._ctx.logger.debug(f"cateye 截图调用异常: {e}")
            return []
        if not isinstance(resp, dict) or not resp.get("success"):
            error = resp.get("error") if isinstance(resp, dict) else type(resp).__name__
            self._ctx.logger.debug(f"cateye 截图失败: {error}")
            return []
        screens = []
        for raw in (resp.get("screens") or []):
            image = str(raw.get("image_base64", "") or "") if isinstance(raw, dict) else ""
            if not image:
                continue
            screens.append({"index": raw.get("index"), "primary": bool(raw.get("primary")),
                            "image_base64": image})
        if not screens:
            legacy = str(resp.get("image_base64", "") or "")
            if legacy:
                screens = [{"index": 0, "primary": True, "image_base64": legacy}]
        return screens

    def _screen_scope(self) -> str:
        """屏幕范围取值（screen_scope；非法值一律回退 main，绝不因配置炸掉巡检）。"""
        cfg = self._screen_cfg()
        value = getattr(cfg, "screen_scope", "main")
        return value if value in ("main", "all") else "main"

    def _select_screens(self, screens: list) -> list:
        """按 ``[patrol].screen_scope`` 选屏：main = 主屏一条；all = 全部（按 index 升序）。

        ``all`` 只在真有多屏时改变行为：单屏客户端（对端降级 / 旧 hub）两种取值等价。
        """
        if self._screen_scope() != "all" or len(screens) <= 1:
            primary = next((s for s in screens if s["primary"]), screens[0])
            return [primary]
        return sorted(screens, key=lambda s: (s["index"] is None, s["index"] or 0))

    async def _describe_screens(self, images: list, selected: int, total: int) -> str:
        """用视觉模型把截图转成一句话描述，失败返回空串。

        调用统一走 ``core.llm_utils.LLMService.describe_image``（2026-10-10 收编：
        所有 LLM 调用只从这一个模块发出）；提示词与任务名分别来自
        ``[prompts].screen_describe`` 与 ``[llm].vlm_task``。
        2026-10-30 多屏适配：多张图一次调用（``extra_images``），并在提示词后内联
        追加分屏说明——不新增 [prompts] 键，单图时提示词与旧实现逐字一致。
        """
        llm = self._llm or _fallback_llm(self._plugin)
        if llm is None:
            return ""
        describe_prompt = prompt_of(self._plugin, "screen_describe")
        if selected > 1:
            describe_prompt = (f"{describe_prompt}\n"
                               f"（对方有多块屏幕，以下 {selected} 张截图按 屏0(主屏)→屏{selected - 1} "
                               "顺序给出：请按屏各用一小句说明在做什么，再用一句话概括整体状态）")
        vlm_task = purpose_task(self._plugin, "vlm", default="vlm")
        return await llm.describe_image(
            images[0], describe_prompt,
            extra_images=images[1:],
            task_name=vlm_task, temperature=0.3,
            event="cateye_screen_describe",
        )
