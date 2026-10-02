"""EmotionComponentsMixin —— 情绪系统的 SDK 组件注册（装饰器方法）。

组件清单（组件名带 cateye_test_ 前缀，与其他模块同规则）：
- Command ×2：/cateye_test_emotion_init（人设初始化）、/cateye_test_emotion_config [reset|clear]（查看/重置）
- Tool ×1：cateye_test_emotion_mood（当前情绪底色，供"你现在什么心情"类问题）
- HookHandler ×3：maisaka.planner.before_request（情绪底色注入）、
  maisaka.replyer.before_model_request（情绪底色注入）、
  maisaka.replyer.after_response（回复后 ΔVAD 增量评估，OBSERVE）

注入方式（planner 与 replyer 一致）：以 SystemMessageItem 插到**第一条系统提示词之后**
（items 强 schema 协议，meta 三件套齐全、modified_kwargs 全量回传；messages 旧投影兼容）。

方法名 `emo_` 前缀；开头判空 ``getattr(self, "_emotion", None)``。
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar, Dict, Optional

from maibot_sdk import Command, HookHandler, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from core.inject import (
    inject_into_items_after_system,
    inject_into_messages,
    pick_payload_key,
)


class EmotionComponentsMixin:
    """情绪组件（2 Command + 1 Tool + 2 HookHandler）。"""

    emotion_components_info: ClassVar[Dict[str, str]] = {
        "cateye_test_emotion_init": "Command：由人设自动推导情绪锚点/偏移/乘数（VAD 初始化）",
        "cateye_test_emotion_config": "Command：查看情绪参数；reset 子命令恢复默认",
        "cateye_test_emotion_mood": "Tool：查询当前情绪底色（标签+低分辨率描述）",
        "emotion_planner_inject": "HookHandler：maisaka.planner.before_request 情绪底色注入（系统提示词后）",
        "emotion_replyer_model_inject": "HookHandler：maisaka.replyer.before_model_request 情绪底色注入（系统提示词后）",
        "emotion_reply_evaluate": "HookHandler：maisaka.replyer.after_response 增量评估（OBSERVE）",
    }

    def _emo_service(self) -> Any:
        return getattr(self, "_emotion", None)

    def _emo_module_enabled(self) -> bool:
        """模块独立总开关 + 插件总开关。"""
        cfg = getattr(self.config, "emotion", None)
        plugin_section = getattr(self.config, "plugin", None)
        return bool(getattr(plugin_section, "enabled", True)) and bool(getattr(cfg, "enabled", True))

    # ============================================================
    # Commands
    # ============================================================
    @Command("cateye_test_emotion_init",
             description="由人设自动推导情绪锚点/静态偏移/乘数并生效（VAD 情绪系统初始化）",
             pattern=r"(?<!\S)/cateye_test_emotion_init\s*$")
    async def emo_cmd_init(self, **kwargs: Any) -> tuple[bool, str, bool]:
        svc = self._emo_service()
        stream_id = str(kwargs.get("stream_id", "") or "")
        if svc is None or not self._emo_module_enabled():
            await self.ctx.send.text("情绪模块未启用或未初始化。", stream_id)
            return True, "emotion disabled", True
        if not self._qz_is_admin(str(kwargs.get("user_id") or ""), bool(kwargs.get("is_local_operator"))):
            await self.ctx.send.text("权限不足：仅管理员可执行情绪初始化。", stream_id)
            return True, "没有权限", True
        await self.ctx.send.text("正在根据人设推导情绪参数…", stream_id)
        reply = await svc.initialize_from_persona()
        await self.ctx.send.text(reply, stream_id)
        return True, "emotion init done", True

    @Command("cateye_test_emotion_config",
             description="查看情绪系统参数（锚点/偏移/乘数/当前VAD）；reset 子命令恢复默认",
             pattern=r"(?<!\S)/cateye_test_emotion_config(?:\s+(?P<emo_args>\S+))?\s*$")
    async def emo_cmd_config(self, **kwargs: Any) -> tuple[bool, str, bool]:
        svc = self._emo_service()
        stream_id = str(kwargs.get("stream_id", "") or "")
        if svc is None or not self._emo_module_enabled():
            await self.ctx.send.text("情绪模块未启用或未初始化。", stream_id)
            return True, "emotion disabled", True
        if not self._qz_is_admin(str(kwargs.get("user_id") or ""), bool(kwargs.get("is_local_operator"))):
            await self.ctx.send.text("权限不足：仅管理员可查看/修改情绪参数。", stream_id)
            return True, "没有权限", True
        args = str((kwargs.get("matched_groups") or {}).get("emo_args") or "").strip().lower()
        if args == "reset":
            reply = svc.reset_params()
        elif args == "clear":
            reply = svc.clear_log()
        else:
            reply = svc.params_summary()
        await self.ctx.send.text(reply, stream_id)
        return True, "emotion config done", True

    # ============================================================
    # Tools
    # ============================================================
    @Tool("cateye_test_emotion_mood",
          description="查询 bot 当前的情绪底色（情绪标签与低分辨率描述）。用户问'你现在什么心情''感觉怎么样'时调用。")
    async def emo_tool_mood(self, **kwargs: Any) -> str:
        svc = self._emo_service()
        if svc is None or not self._emo_module_enabled():
            return "情绪模块未启用。"
        return f"当前情绪底色：{svc.mood_text()}"

    # ============================================================
    # Hooks（planner + replyer 双通道：均插到第一条系统提示词之后）
    # ============================================================
    def _emo_inject(self, kwargs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按 payload 形态把情绪底色系统条目插到第一条系统提示词之后。

        Returns:
            Hook 返回 dict；payload 无法识别时返回 None（调用方回 continue）。
        """
        svc = self._emo_service()
        payload_key, payload = pick_payload_key(kwargs)
        if payload_key is None or svc is None:
            return None
        text = svc.injection_text()
        if payload_key == "items":
            modified = inject_into_items_after_system(payload, text)
        else:
            modified = inject_into_messages(payload, text)
        return {"action": "continue", "modified_kwargs": {**kwargs, payload_key: modified}}

    @HookHandler("maisaka.planner.before_request",
                 name="emotion_planner_inject",
                 description="把当前情绪底色（模糊化）以系统条目插入 planner 上下文的第一条系统提示词之后",
                 mode=HookMode.BLOCKING, order=HookOrder.NORMAL,
                 timeout_ms=2000, error_policy=ErrorPolicy.SKIP)
    async def emo_hook_planner_inject(self, session_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self._emo_module_enabled() or not self._injection_enabled():
            return {"action": "continue"}
        result = self._emo_inject(kwargs)
        return result or {"action": "continue"}

    @HookHandler("maisaka.replyer.before_model_request",
                 name="emotion_replyer_model_inject",
                 description="把当前情绪底色（模糊化）以系统条目插入 replyer 模型请求的第一条系统提示词之后",
                 mode=HookMode.BLOCKING, order=HookOrder.NORMAL,
                 timeout_ms=2000, error_policy=ErrorPolicy.SKIP)
    async def emo_hook_replyer_model_inject(self, attempt: int = 1, **kwargs: Any) -> dict[str, Any]:
        if attempt and int(attempt) > 1:  # 重试请求不重复加压
            return {"action": "continue"}
        if not self._emo_module_enabled() or not self._injection_enabled():
            return {"action": "continue"}
        result = self._emo_inject(kwargs)
        return result or {"action": "continue"}

    def _injection_enabled(self) -> bool:
        cfg = getattr(self.config, "emotion", None)
        return bool(getattr(cfg, "injection_enabled", True))

    @HookHandler("maisaka.replyer.after_response",
                 name="emotion_reply_evaluate",
                 description="回复后评估该轮对话的情绪增量 ΔVAD 并记入当天情绪日志",
                 mode=HookMode.OBSERVE, order=HookOrder.LATE,
                 timeout_ms=1000, error_policy=ErrorPolicy.SKIP)
    async def emo_hook_reply_evaluate(self, response: Any = None, **kwargs: Any) -> None:
        svc = self._emo_service()
        if svc is None or not self._emo_module_enabled():
            return
        cfg = getattr(self.config, "emotion", None)
        if not bool(getattr(cfg, "evaluate_on_reply", True)):
            return
        reply_text = str(response or "").strip()
        if not reply_text:
            return
        # 评估带 LLM 往返，不能阻塞 Hook（OBSERVE 后台通道）：fire-and-forget
        task = asyncio.create_task(svc.evaluate_reply(reply_text[:600]))
        svc._bg_tasks.append(task)
        if len(svc._bg_tasks) > 20:  # 防堆积：只留最近的评估任务
            svc._bg_tasks = [t for t in svc._bg_tasks if not t.done()][-10:]
