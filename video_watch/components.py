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
    "用这个工具你可以真的去「看」一个B站视频：提交 BV 号或视频链接后立即返回任务回执，"
    "后台处理后用 action=status 查询进度、action=result 取回结构化观看结果"
    "（标题/UP主/时长/实际读取的文本来源与画面采样/限制与失败原因），"
    "action=cancel 取消任务。注意：结果是给你看的观看记录，不会自动发给任何群或私聊；"
    "失效、需要会员/登录、时长超预算的视频会如实返回原因，不会假装看过。"
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
                "description": ("动作：submit（提交观看）/ status（查进度）/ "
                                "result（取结果）/ cancel（取消）"),
            },
            "video": {
                "type": "string",
                "description": "submit 必填：BV 号或 bilibili 视频页链接（含 b23.tv 短链）",
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
                                    **kwargs: Any) -> str:
        """LLM Tool：看一个B站视频（后台执行，先回执后查询；不自动发消息）。"""
        service = getattr(self, "_video_watch", None)
        if service is None:
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
        if receipt.get("success") and receipt.get("task_id"):
            extra = {k: receipt.get(k) for k in ("status", "stage", "cached", "deduped",
                                                 "error_class") if receipt.get(k)}
            if extra:
                content = f"{content} [{', '.join(f'{k}={v}' for k, v in extra.items())}]"
        return content or str(receipt.get("error") or "无回执")
