"""video_watch 服务：生活插件内部单一观看服务实例（B 计划 §3/§5）。

职责：装配 :class:`video_watch.manager.WatchTaskManager`（一条观看流水线）、
对 AI 工具 ``watch_bili_video`` 与 integrations/video_schedule 日程编排提供
同一动作面（core.contracts.VideoWatchPort）、在结果落缓存前完成父侧加工
（画面摘要 vlm 调用 + 文本观后感，均经注入的宿主 LLM 面，只记实际 usage）。

边界（B 计划 §1/§5 逐条）：
- 看视频与发消息分离：观看完成**绝不**自动向任何群/私聊发送消息；
- 结果只来自真实读取的来源；画面/文本/音频各自的状态与原因如实上报，
  文本-only 是 partial，不冒充看过画面；
- 不暴露播放鉴权直链、凭据、原始帧或超大返回（帧在摘要后从结果剥离）；
- Token 只记录宿主回传 usage，不向 generate 传 max_tokens（宿主限制唯一）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

from .manager import WatchTaskManager

#: 观看记忆/缓存结果模式标记（缓存键组成部分：文本来源 vs 抽帧采样，B 计划 §5.1）
MODE_TEXT_SOURCES = "text_sources"
MODE_SAMPLED_FRAMES = "sampled_frames"


def _brief_receipt_text(receipt: Dict[str, Any]) -> str:
    """把任务回执压缩成给宿主 AI 看的短句（不掺入编造内容）。"""
    if receipt.get("cached"):
        return f"复用该视频此前相同处理模式的缓存结果（任务 {receipt.get('task_id')}）。"
    if receipt.get("deduped"):
        return (f"同一视频已有进行中的观看任务（{receipt.get('task_id')}，"
                f"状态 {receipt.get('status')}），不重复提交。")
    if receipt.get("success"):
        return (f"已受理观看任务 {receipt.get('task_id')}（当前 {receipt.get('status')}）；"
                f"用 action=status/result 查询进度与结果。")
    return f"提交失败（{receipt.get('error_class')}）：{receipt.get('error')}"


def _result_summary_text(result: Dict[str, Any]) -> str:
    """把结构化结果压成有界摘要（不塞原始帧/完整提示词/凭据）。"""
    video = result.get("video") or {}
    parts = [
        f"《{video.get('title') or '未知标题'}》UP：{video.get('uploader') or '未知'}，"
        f"时长 {video.get('duration_seconds') or '?'}s",
        str(result.get("watched_range") or {}).strip("{}'"),
    ]
    visual = result.get("visual") or {}
    if isinstance(visual, dict) and visual.get("status") == "completed":
        summary = str(visual.get("summary") or "").strip()
        if summary:
            parts.append(f"画面采样理解：{summary[:300]}")
    elif isinstance(visual, dict) and visual.get("status") not in (None, "", "not_attempted"):
        parts.append(f"画面采样：{visual.get('status')}（{(visual.get('reason') or '')[:120]}）")
    if result.get("impression"):
        parts.append(f"观后感：{result.get('impression')}")
    if result.get("error"):
        parts.append(f"备注：{result.get('error')}")
    return "\n".join(p for p in parts if p)


class VideoWatchService:
    """观看任务/媒体/结果/缓存的单一所有者（core.contracts.VideoWatchPort 实现方）。

    ``llm_generate_detailed`` = 组合根注入的宿主 LLM 面（core.llm_utils
    LLMService.generate_detailed；prompt 透传消息列表，usage 由插件既有
    tracker 记账）；``config_provider`` 惰性热读 [video_watch] 配置段。
    """

    def __init__(self, data_dir: Path, logger: Any = None, *,
                 config_provider: Optional[Callable[[], Any]] = None,
                 llm_generate_detailed: Optional[Callable[..., Awaitable[Any]]] = None,
                 automatic_guard: Optional[Callable[[str], bool]] = None,
                 state_changed: Optional[Callable[[], None]] = None) -> None:
        self._data_dir = Path(data_dir)
        self._logger = logger
        self._config_provider = config_provider
        self._llm_generate_detailed = llm_generate_detailed
        self._automatic_guard = automatic_guard
        self._state_changed = state_changed
        self._manager: Optional[WatchTaskManager] = None

    # ---- 生命周期（on_load / on_unload 各一次；热重载 = 全量重载重建实例）----
    def start(self) -> Optional[WatchTaskManager]:
        """构造唯一 manager 实例；未启用返回 None（工具按「未启用」如实回执）。"""
        cfg = self._config()
        if self._manager is not None:
            return self._manager
        if cfg is not None and not bool(getattr(cfg, "enabled", True)):
            if self._logger is not None:
                self._logger.info("视频观看模块未启用（[video_watch].enabled=false），仅注册工具")
            return None
        # B2：visual_enabled=True → worker 携带抽帧参数（无 ffmpeg/受限/超限时 worker
        # 如实降级，结果按实际模式落缓存键）；False → 纯文本流水线（原型行为）
        visual_provider = None
        if cfg is None or bool(getattr(cfg, "visual_enabled", True)):
            visual_provider = lambda: {
                "capability": "frames-v1",
                "max_frames": self._int_of("visual_max_frames", 6),
                "max_download_mb": self._int_of("visual_max_download_mb", 32),
            }
        self._manager = WatchTaskManager(
            self._data_dir, logger=self._logger,
            queue_capacity=self._int_of("queue_capacity", 3),
            task_deadline_seconds=self._int_of("task_deadline_seconds", 900),
            max_video_duration_seconds=self._int_of("max_video_duration_seconds", 1800),
            visual_provider=visual_provider,
        )
        self._manager.attach_watchlist(
            lambda: list(getattr(self._config(), "watchlist", None) or []))
        self._manager.set_impression_hook(self._make_result_hook())
        self._manager.set_task_guard(self._automatic_task_allowed)
        if self._state_changed is not None:
            self._manager.set_state_listener(self._state_changed)
        if self._logger is not None:
            self._logger.info("视频观看模块已加载（队列=%d，任务时限=%ds）",
                              self._int_of("queue_capacity", 3),
                              self._int_of("task_deadline_seconds", 900))
        return self._manager

    async def stop(self) -> None:
        """停机（S0 生命周期修复）：真正取消任务泵并终止在跑 worker——不只是丢引用。

        卸载/热重载后不再有本模块的泵轮询或父侧模型调用；队列/进行中任务保留
        台账，下次装配 ``_recover()`` 如实标记 ``interrupted_by_restart``。
        """
        manager = self._manager
        self._manager = None
        if manager is not None:
            await manager.shutdown()

    # ---- 动作面（VideoWatchPort；工具与日程共用同一实例/队列/缓存）----
    async def handle_action(self, action: str = "", video: str = "", task_id: str = "",
                            purpose: str = "", episode_id: str = "",
                            source: str = "tool", owner: str = "", force: bool = False,
                            **kwargs: Any) -> Dict[str, Any]:
        del kwargs  # stream_id/platform 等宿主注入参数不参与观看语义
        manager = self._require_manager()
        action = str(action or "").strip().lower()
        # S0 所有权：source=tool 视为用户显式点播（取消即 force）；日程侧由
        # episode 推导引用身份（事件收尾只释放自己的引用，不误杀其他调用者）
        if not owner:
            if source == "tool":
                # 用户经工具显式点播：取消语义为强制（S0 所有权：用户意志优先）
                owner, force = "tool", True
            elif source == "schedule" and episode_id:
                owner = f"schedule:{episode_id}"
        if action == "submit":
            if source == "schedule" and not self._automatic_task_allowed(
                    {"source": source, "episode_id": episode_id, "owners": [owner]}):
                return {"success": False, "error_class": "attention_revoked",
                        "error": "当前没有有效的自动观看注意力，不启动任务"}
            receipt = manager.submit(video, purpose=purpose, source=source,
                                     episode_id=episode_id, owner=owner)
            receipt["content"] = _brief_receipt_text(receipt)
            return receipt
        if action == "status":
            receipt = manager.status(task_id)
            receipt["content"] = (f"任务 {task_id} 状态：{receipt.get('status')}"
                                  + (f"（{receipt.get('stage')}）" if receipt.get("stage") else ""))
            return receipt
        if action == "result":
            receipt = manager.result(task_id)
            if receipt.get("success") and isinstance(receipt.get("result"), dict):
                receipt["content"] = _result_summary_text(receipt["result"])
            else:
                receipt["content"] = str(receipt.get("error") or "暂无结果")
            return receipt
        if action == "cancel":
            receipt = manager.cancel(task_id, owner=owner, force=force)
            if receipt.get("released"):
                receipt["content"] = str(receipt.get("content")
                                         or f"任务 {task_id} 仍有其他调用者，本次只释放引用不取消")
            else:
                receipt["content"] = (f"任务 {task_id} 已是终态（{receipt.get('status')}），返回原终态不重跑"
                                      if receipt.get("already_terminal")
                                      else f"任务 {task_id} 取消请求已受理（{receipt.get('status')}）")
            return receipt
        if action == "next_candidate":
            receipt = manager.next_candidate()
            receipt["content"] = (f"候选：{receipt['candidate'].get('bvid')}"
                                  if receipt.get("candidate") else f"暂无候选（{receipt.get('reason')}）")
            return receipt
        return {"success": False,
                "error": f"未知 action：{action}（支持 submit/status/result/cancel/next_candidate）"}

    # ---- 父侧结果加工（manager._finish 在落缓存/写历史前 await 本钩子）----
    def _make_result_hook(self) -> Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]:
        async def hook(result: Dict[str, Any]) -> Dict[str, Any]:
            self._check_result_attention(result)
            result = await self._maybe_visual_summary(result)
            self._check_result_attention(result)
            result = await self._maybe_impression(result)
            return result
        return hook

    async def _maybe_visual_summary(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Always remove raw frames, including cancellation/time-budget exits."""
        try:
            return await self._summarize_visual_frames(result)
        finally:
            self._strip_frames(result)
            visual = result.get("visual")
            if isinstance(visual, dict) and visual.get("understanding_status") == "running":
                visual["understanding_status"] = "cancelled"
                visual["summary"] = None

    async def _summarize_visual_frames(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """worker 带回的抽帧 → vlm 图像任务摘要；失败/未启用如实标注并剥离原始帧。"""
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else None
        frames = (visual or {}).get("frames") if isinstance(visual, dict) else None
        if not (isinstance(frames, list) and frames):
            return result
        cfg = self._config()
        task_name = str(getattr(cfg, "visual_task_name", "vlm") or "vlm").strip() or "vlm"
        if self._llm_generate_detailed is None:
            visual["understanding_status"] = "not_attempted"
            visual["summary"] = None
            visual["summary_error"] = "画面理解不可用：宿主 LLM 面未注入"
            self._strip_frames(result)
            return result
        observations = "；".join(str(o) for o in (result.get("observations") or []))
        subtitle_text = str(result.get("subtitle_text") or "")[:1200]
        prompt = (
            "以下是同一个B站视频按时间等间隔抽出的采样帧（附读取到的文本来源）。"
            "请用两三句中文描述画面里实际看到的内容与整体氛围；只描述帧中可见的内容，"
            "帧与帧之间的过程是推断，不要编造没出现的细节、对白或音乐。"
            + (f"\n来源：{observations}" if observations else "")
            + (f"\n字幕节选：{subtitle_text}" if subtitle_text else "")
        )
        content: list = [{"type": "text", "text": prompt}]
        for frame in frames:
            image_base64 = str((frame or {}).get("image_base64") or "")
            if not image_base64:
                continue
            content.append({"type": "image", "image_format": "jpeg",
                            "image_base64": image_base64})
        if len(content) == 1:
            visual["understanding_status"] = "not_attempted"
            visual["summary"] = None
            visual["summary_error"] = "画面理解不可用：worker 未带回有效帧"
            self._strip_frames(result)
            return result
        visual["understanding_status"] = "running"
        try:
            llm_result = await self._llm_generate_detailed(
                [{"role": "user", "content": content}],
                task_name=task_name, temperature=0.5, strict_params=True,
                event="video_watch_visual")
        except Exception as exc:
            llm_result = None
            visual["summary_error"] = f"画面理解调用异常：{exc}"
        ok = getattr(llm_result, "ok", False)
        if ok:
            visual["summary"] = str(getattr(llm_result, "text", "") or "")[:500]
            visual["understanding_status"] = "completed" if visual["summary"].strip() else "failed"
            visual["summary_task"] = task_name
            visual["summary_mode"] = MODE_SAMPLED_FRAMES
            visual["summary_usage_total_tokens"] = getattr(llm_result, "total_tokens", None)
        elif not visual.get("summary_error"):
            visual["summary"] = None
            visual["summary_error"] = (f"画面理解生成失败（{getattr(llm_result, 'category', '')}）："
                                       f"{getattr(llm_result, 'error', '') or '任务不可用'}")
        if not ok:
            visual["understanding_status"] = "failed"
        self._strip_frames(result)
        return result

    async def _maybe_impression(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """文本观后感（原型口径逐字保留；画面摘要存在时一并作为来源）。"""
        cfg = self._config()
        if cfg is not None and not bool(getattr(cfg, "summary_impression", True)):
            return result
        observations = "；".join(str(o) for o in (result.get("observations") or []))
        subtitle_text = str(result.get("subtitle_text") or "")[:1500]
        visual = result.get("visual") if isinstance(result.get("visual"), dict) else {}
        visual_summary = str((visual or {}).get("summary") or "").strip()
        purpose = str(result.get("purpose") or "")
        if not visual_summary:
            prompt = (
                "你刚通过工具看了一个B站视频，以下是真实读取到的来源（只有标题/简介/公开字幕，"
                "没有画面与音频）。请用两三句中文写一段简短、口语的观后感；"
                "只依据给出的来源，不要编造画面细节、对白或音乐。\n"
                f"来源：{observations}\n"
                + (f"字幕节选：{subtitle_text}\n" if subtitle_text else "")
            )
            mode = MODE_TEXT_SOURCES
        else:
            prompt = (
                "你刚通过工具看了一个B站视频，以下是真实读取到的来源（元信息/公开字幕，"
                "以及按时间等间隔抽帧后的画面采样理解）。请用两三句中文写一段简短、口语的观后感；"
                "只依据给出的来源，不要编造没出现的细节、对白或音乐。\n"
                f"来源：{observations}\n"
                + (f"字幕节选：{subtitle_text}\n" if subtitle_text else "")
                + f"画面采样理解：{visual_summary[:400]}\n"
            )
            mode = MODE_SAMPLED_FRAMES
        task_name = str(getattr(cfg, "impression_task_name", "") or "").strip()
        if self._llm_generate_detailed is None:
            result["impression"] = None
            result["impression_error"] = "观后感不可用：宿主 LLM 面未注入"
            return result
        try:
            llm_result = await self._llm_generate_detailed(
                prompt, task_name=task_name or "utils", temperature=0.7,
                strict_params=True, event="video_watch_impression")
        except Exception as exc:
            llm_result = None
            result["impression_error"] = f"观后感生成异常：{exc}"
        if getattr(llm_result, "ok", False):
            result["impression"] = str(getattr(llm_result, "text", "") or "")[:500]
            result["impression_task"] = task_name or "utils"
            result["impression_mode"] = mode
        elif not result.get("impression_error"):
            result["impression"] = None
            result["impression_error"] = (f"观后感生成失败（{getattr(llm_result, 'category', '')}）："
                                          f"{getattr(llm_result, 'error', '') or '任务不可用'}")
        return result

    # ---- 内部 ----
    def _require_manager(self) -> WatchTaskManager:
        if self._manager is None or not bool(getattr(self._config(), "enabled", True)):
            raise RuntimeError("视频观看模块未启用（[video_watch].enabled=false）或未加载")
        return self._manager

    def _automatic_task_allowed(self, task: Dict[str, Any]) -> bool:
        if task.get("source") != "schedule" or "tool" in (task.get("owners") or []):
            return True
        if self._automatic_guard is None:
            return False
        try:
            return bool(self._automatic_guard(str(task.get("episode_id") or "")))
        except Exception:
            return False

    def _check_result_attention(self, result: Dict[str, Any]) -> None:
        manager = self._manager
        task = manager._store.get_task(str(result.get("task_id") or "")) if manager else None
        if task and not self._automatic_task_allowed(task):
            self.revoke_automatic()
            raise RuntimeError("自动观看注意力失效，停止后续模型调用")

    def task_state(self, task_id: str) -> Dict[str, Any]:
        return self._require_manager().status(task_id)

    def revoke_automatic(self) -> int:
        """Message hooks immediately detach automatic owners; manual owners survive."""
        manager = self._manager
        if manager is None:
            return 0
        count = 0
        for task in manager._store.load_tasks():
            if manager._store.is_terminal(str(task.get("status"))):
                continue
            owners = list(task.get("owners") or [])
            for owner in owners:
                if str(owner).startswith("schedule:"):
                    manager.cancel(str(task["task_id"]), owner=str(owner))
                    count += 1
        return count

    def _config(self) -> Any:
        provider = self._config_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _int_of(self, name: str, default: int) -> int:
        value = getattr(self._config(), name, default)
        try:
            return int(value) if not isinstance(value, bool) else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _strip_frames(result: Dict[str, Any]) -> None:
        """原始帧只在父侧加工瞬时存在；摘要后从结果剥离（不进缓存/回执/历史）。"""
        visual = result.get("visual")
        if isinstance(visual, dict):
            visual.pop("frames", None)


def result_mode_of(result: Dict[str, Any]) -> str:
    """结果模式标记（缓存键组成部分；模式不同不互相命中，旧文本缓存不挡画面观看）。"""
    watched = result.get("watched_range") if isinstance(result.get("watched_range"), dict) else {}
    if str((watched or {}).get("mode") or "") == MODE_SAMPLED_FRAMES:
        return MODE_SAMPLED_FRAMES
    return MODE_TEXT_SOURCES


def cache_payload_text(result: Dict[str, Any]) -> str:
    """紧凑 JSON 文本（调试/日志用；不含帧与凭据）。"""
    return json.dumps({k: v for k, v in result.items() if k != "visual"},
                      ensure_ascii=False, sort_keys=True)[:400]
