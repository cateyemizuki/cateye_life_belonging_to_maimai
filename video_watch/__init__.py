"""video_watch —— 生活插件内置 B 站观看模块的可测核心包（无 SDK 依赖）。

本包只做任务编排、输入规范化和媒体流水线的纯逻辑；所有 maibot_sdk 交互
（Tool 装饰器、ctx.* 能力代理、LLM 调用）留在 service.py / components.py /
vision_adapter.py，使本包可以在隔离测试中独立驱动（2026-11-01 B 站观看工具
与日程刷视频事件计划书 §3：观看任务、资源隔离、结果与缓存归本模块单一服务
实例所有，AI 工具与日程共用，不是第二个插件）。

包名 ``video_watch`` 为生活插件自有顶层名（DEV_GUIDE「顶层模块名唯一」纪律）；
从独立原型 cateye_bili_watch（cad3d2d）只读迁移 bv/store/manager/worker 四件，
保持其子进程 spec/result 协议与行为契约不变。
"""

from .bv import normalize_video_ref
from .manager import WatchTaskManager

__all__ = ["normalize_video_ref", "WatchTaskManager"]
