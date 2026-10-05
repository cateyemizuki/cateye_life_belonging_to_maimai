"""EmotionComponentsMixin —— 情绪系统的 SDK 组件注册（装饰器方法）。

组件清单（组件名带 cateye_test_ 前缀，与其他模块同规则）：
- Command ×2：/cateye_test_emotion_init（人设初始化）、/cateye_test_emotion_config [reset|clear]（查看/重置）
- Tool ×2：cateye_test_emotion_mood（当前情绪底色，供"你现在什么心情"类问题）、
  cateye_test_emotion_report(event, is_target)（上报影响心情的事件；群聊 target 声明，M3）
- HookHandler ×3：maisaka.planner.before_request（情绪底色注入）、
  maisaka.replyer.before_model_request（情绪底色注入）、
  maisaka.replyer.after_response（回复后 ΔVAD 增量评估，OBSERVE；session_id 进 target 识别链路）

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
    """情绪组件（2 Command + 2 Tool + 3 HookHandler）。"""

    emotion_components_info: ClassVar[Dict[str, str]] = {
        "cateye_test_emotion_init": "Command：由人设自动推导情绪锚点/偏移/乘数（VAD 初始化）",
        "cateye_test_emotion_config": "Command：查看情绪参数；reset 子命令恢复默认",
        "cateye_test_emotion_mood": "Tool：查询当前情绪底色（标签+低分辨率描述）",
        "cateye_test_emotion_report": "Tool：上报一件影响心情的事件；is_target 声明是否由重要的人引发（M3）",
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

    @Tool(
        "cateye_test_emotion_report",
        description="上报一件影响 bot 心情的事件（由 bot 判断是否与最重要的人有关），入账有界情绪增量。",
        detailed_description=(
            "上报一件给 bot 的情绪带来波动的事件，由代码侧做有界评估后计入当天情绪。"
            "触发条件（满足其一才调用）：\n"
            "1) 在群聊里，最重要的人（配置 target.target_qq 指定的那位，非普通群友）对 bot 说了值得在意的话，"
            "或做了影响 bot 心情的事（关心、调侃、约定、爽约等）；\n"
            "2) 群聊里发生了由那位重要的人间接引发、明显影响 bot 心情的事（如 TA 被提及、TA 的事有了结果）。\n"
            "参数：\n"
            "- event：string，必填。用一句话概括这轮交流发生了什么（谁说了什么、结果如何）。\n"
            "- is_target：boolean，可选。这件事是否由最重要的人引发；是就填 true。"
            "不填或填了别的值一律按「非重要的人」处理，不会报错。\n"
            "注意：普通群友的日常闲聊不要调用（自动回复评估已覆盖）；私聊里的交流也无需调用；"
            "同一件事不要重复上报。"
        ),
        parameters={
            "event": {
                "type": "string",
                "description": "一句话概括这轮交流发生了什么（谁说了什么、发生了什么）",
                "required": True,
            },
            "is_target": {
                "type": "boolean",
                "description": "这件事是否由最重要的人引发（是=true）；缺失/非法值按非 target 处理",
                "required": False,
            },
        },
    )
    async def emo_tool_report(self, event: str = "", is_target: Any = None, **kwargs: Any) -> str:
        """Tool：上报群聊里与 target 相关的情绪事件（§5.3 / §6.2 A）。

        与自动评估路径的关系：replyer.after_response 的回复后评估始终是主通道，
        本工具只是群聊场景的补充（Tool 默认进 deferred 池，不保证被自发调用）。

        参数安全：``is_target`` 永不拒绝——缺失、类型不对、胡填一律静默按非 target 计
        （最多 debug 级日志），代码不报错；非 bool 值不做任何真值猜测。

        跨来源去重（§2.4.6 A3）：``kwargs['stream_id']``（宿主 ToolInvocation 的聊天流）
        随上报传入 emotion 侧；若同一流在 ``emotion.dedup_window_seconds``（默认 180s）内
        已由 replyer 自动评估入账过同一事件，本次判重复、返回明确"未重复计入"文本，
        不产生第二次情绪增量（口径见 ``EmotionService.dedup_policy``）。
        """
        svc = self._emo_service()
        if svc is None or not self._emo_module_enabled():
            return "情绪模块未启用。"
        text = str(event or "").strip()
        if not text:
            return "缺少 event 参数：请用一句话说明这轮交流发生了什么。"
        target_flag = is_target is True
        if is_target is not None and not isinstance(is_target, bool):
            try:
                self.ctx.logger.debug("emotion_report：is_target 非法值 %r，按非 target 处理", is_target)
            except Exception:
                pass
        multiplier = svc.target_multiplier(target_flag)
        stream_id = str(kwargs.get("stream_id", "") or "")
        result = await svc.record_external_event(
            source="tool:emotion_report", description=text[:300], delta=None,
            use_llm=True, multiplier=multiplier, session_id=stream_id,
        )
        if not result.get("ok"):
            reason = str(result.get("reason") or "")
            if reason == "duplicate_cross_source":
                return "这件事刚才已经计入情绪了（与自动评估重复），未重复计入。"
            return f"这件事暂未计入情绪（{reason or '未知原因'}）。"
        return "已记下这件事给心情带来的变化。"

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
        # M3：session_id 进 target 识别链路（私聊 stream_id == 目标私聊流 → target 事件）
        session_id = str(kwargs.get("session_id") or "")
        # 评估带 LLM 往返，不能阻塞 Hook（OBSERVE 后台通道）：fire-and-forget
        task = asyncio.create_task(svc.evaluate_reply(reply_text[:600], session_id=session_id))
        svc._bg_tasks.append(task)
        if len(svc._bg_tasks) > 20:  # 防堆积：只留最近的评估任务
            svc._bg_tasks = [t for t in svc._bg_tasks if not t.done()][-10:]
