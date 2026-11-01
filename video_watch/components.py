"""video_watch 组件混入：生活插件注册 ``watch_bili_video`` AI 工具（B 计划 §3）。

与生活插件其他组件混入同构（装饰器在 on_load 前由 Runner 收集）；工具只是
:mod:`video_watch.service` 单一服务实例的适配面，返回有界文本回执——结构化
动作面（submit/status/result/cancel/next_candidate）走
``core.contracts.VideoWatchPort``（日程编排共用同一实例，不走本工具）。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import Tool

TOOL_WATCH_DESCRIPTION = (
    "观看用户提供的B站视频。video可以直接传原始长链接、b23.tv短链或包含链接的小程序分享文字；"
    "必须使用上下文中的真实来源，不要猜测或编造BV号。action=resolve可先解析真实BV。"
    "action=submit提交后台观看后结束本轮；完成/失败会由后台通知原发起聊天，"
    "不要在同轮或后续自行反复status/result轮询，也不要重复submit催进度。"
    "status/result仅在用户明确询问时使用一次；cancel取消。"
    "结果只陈述实际采样；通知是点播结果交付，不会广播给其他聊天。"
)


class VideoWatchComponentsMixin:
    """watch_bili_video 工具注册面（宿主 AI 经此提交/查询观看任务）。"""

    @Tool(
        # 身份前缀纪律（smoke 前缀探测/T3）：本插件工具统一 lbm_ 前缀；
        # B 计划 §5 的 watch_bili_video 语义不变，仅加身份前缀。
        "lbm_watch_bili_video",
        description=TOOL_WATCH_DESCRIPTION,
        parameters={
            "action": {
                "type": "string",
                "description": ("动作：resolve（解析原始链接）/ submit（后台观看，完成通知，不轮询）/ status（用户明确问进度）/ "
                                "result（取结果）/ cancel（取消）"),
            },
            "video": {
                "type": "string",
                "description": "submit/resolve 必填：直接复制原始链接、小程序分享文字或BV；不要编造BV。只支持默认分P。",
            },
            "task_id": {
                "type": "string",
                "description": "status/result/cancel 必填：提交回执里的任务 ID",
            },
            "purpose": {
                "type": "string",
                "description": "观看目的（可选，会记录在观看记忆里）",
            },
        },
    )
    async def tool_watch_bili_video(self, action: str = "", video: str = "",
                                    task_id: str = "", purpose: str = "",
                                    **kwargs: Any) -> Any:
        """LLM Tool：看一个B站视频（后台执行，先回执后查询；不自动发消息）。"""
        try:
            service = self._video_watch  # on_load 恒装配（disabled 也建实例，仅不 start）
        except AttributeError:
            return "视频观看模块未启用（[video_watch].enabled=false）或插件未加载。"
        try:
            receipt = await service.handle_action(
                action, video=video, task_id=task_id, purpose=purpose, source="tool")
        except Exception as exc:
            try:
                self.ctx.logger.error("watch_bili_video 执行失败: %s", exc)
            except Exception:
                pass
            return f"观看能力暂不可用：{exc}"
        content = str(receipt.get("content") or "")
        if receipt.get("success") is False:
            error = str(receipt.get("error") or content or "观看请求未成功")
            return {"success": False, "error": error, "content": content or error,
                    "stop_after_execution": True}
        if str(action).strip().lower() == "submit" and receipt.get("success"):
            if receipt.get("cached") and isinstance(receipt.get("result"), dict):
                from .service import _result_summary_text
                return {"success": True, "content": _result_summary_text(receipt["result"]),
                        "stop_after_execution": False}
            origin = str(kwargs.get("stream_id") or kwargs.get("session_id") or kwargs.get("chat_id") or "").strip()
            subscribe = getattr(self, "_subscribe_video_completion", None)
            subscribed = bool(subscribe and subscribe(str(receipt.get("task_id") or ""), origin))
            return {"success": True, "task_id": receipt.get("task_id"),
                    "content": content if subscribed else "任务已受理，但来源聊天通知不可用。请稍后手动查询一次，不要连续轮询。",
                    "completion_notification": subscribed, "stop_after_execution": True}
        if receipt.get("status") in ("queued", "running") or receipt.get("error_class") == "not_ready":
            return {"success": bool(receipt.get("success", True)),
                    "content": content + "；仍在后台处理，完成会通知来源聊天，请结束本轮，勿连续查询。",
                    "stop_after_execution": True}
        if receipt.get("success") and receipt.get("task_id"):
            extra = {k: receipt.get(k) for k in ("status", "stage", "cached", "deduped",
                                                 "error_class") if receipt.get(k)}
            if extra:
                content = f"{content} [{', '.join(f'{k}={v}' for k, v in extra.items())}]"
        return content or str(receipt.get("error") or "无回执")
