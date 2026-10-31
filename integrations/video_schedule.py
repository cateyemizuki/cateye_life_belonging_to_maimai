"""video_schedule —— 日程「刷视频」事件对公共观看接口的联动编排（B 计划 §5，D 线 integrations 场景文件）。

职责边界（模块解耦计划 §3.2）：
- 本模块回答「哪个事实触发、调用哪个公开能力、先后顺序、如何取消/降级」——
  episode 由滚动日程创建/推进/结算，本模块**不**复制日程权威状态，只保存
  episode ↔ task 的关联与观测结果（计划 §4：日程只保存任务关联）；
- 观看能力经注入的端口（公共跨插件 API）调用，不读对方私有配置/任务目录；
- 独立观看插件不可用时如实标记「观看能力不可用」，绝不拖挂生活 tick
  （计划 §3：不阻塞其他日程）；
- 关联唯一：一个 episode 至多一个观看任务；每拍不重复提交；
- 活动结束/被抢占/取消 → 取消进行中的任务；晚到的真实完成结果照实存档
  （不重开 episode、不回填结束时间、不自行发消息）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional

#: 刷视频事件的 event 标识（event_catalog.BUILTIN_EVENTS / RANDOM_EVENT_POOL 同口径）
WATCH_EVENT_KEY = "watch_bili"
#: 链接库有界：closed 记录保留天数与条数上限（防无限增长）
_CLOSED_KEEP_DAYS = 7
_CLOSED_MAX_ENTRIES = 100

ApiCaller = Callable[..., Awaitable[Dict[str, Any]]]
EpisodeLookup = Callable[[str], Optional[Dict[str, Any]]]
EpisodeIds = Callable[[], set]


def _atomic_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


class VideoWatchCoordinator:
    """刷视频 episode ↔ 公共观看接口的联动协调器（单飞 tick 调用）。"""

    def __init__(self, store_dir: Path, api_caller: ApiCaller,
                 get_active_episode: Callable[[], Optional[Dict[str, Any]]],
                 get_episode: EpisodeLookup,
                 logger: Any = None) -> None:
        self._links_path = Path(store_dir) / "video_watch_links.json"
        self._api = api_caller
        self._get_active_episode = get_active_episode
        self._get_episode = get_episode
        self._log = logger

    # ------------------------------------------------------------------
    def _load_links(self) -> Dict[str, Any]:
        payload = _read(self._links_path, {"links": {}})
        return payload if isinstance(payload, dict) and isinstance(payload.get("links"), dict) else {"links": {}}

    def _save_links(self, links: Dict[str, Any]) -> None:
        _atomic_write(self._links_path, links)

    def episode_state(self, episode_id: str) -> Dict[str, Any]:
        """供面板/诊断读取某 episode 的观看关联（只读快照，含来源与阶段）。"""
        links = self._load_links().get("links") or {}
        entry = links.get(episode_id)
        return dict(entry) if isinstance(entry, dict) else {}

    # ------------------------------------------------------------------
    async def sync(self) -> None:
        """每拍一次的联动推进（单飞 task 内执行；任何异常不外泄）。"""
        try:
            await self._sync_inner()
        except Exception as exc:
            if self._log:
                self._log.warning("刷视频联动推进失败（忽略，不影响其他日程）：%s", exc)

    async def _sync_inner(self) -> None:
        links_doc = self._load_links()
        links: Dict[str, Any] = links_doc.get("links") or {}
        changed = False

        # 1) 已链接 episode 的收尾：非 active（已结束/被抢占/取消）→ 取消任务 + 落账
        active = self._get_active_episode() or {}
        active_id = str(active.get("episode_id") or "")
        for episode_id, entry in list(links.items()):
            if entry.get("closed") or episode_id == active_id:
                continue
            row = self._get_episode(episode_id) or {}
            status = str(row.get("status") or "")
            if status in ("active", "planned", ""):
                continue  # 仍活着（planned 罕见：重启窗口），不动
            entry["closed"] = True
            entry["closed_reason"] = {"completed": "episode_completed",
                                      "interrupted": "episode_interrupted",
                                      "paused": "episode_preempted",
                                      "abandoned": "episode_abandoned"}.get(status, f"episode_{status}")
            task_id = str(entry.get("task_id") or "")
            if task_id and not entry.get("result") and str(entry.get("stage")) not in ("unavailable", "skipped", "failed_submit"):
                try:
                    receipt = await self._api(action="cancel", task_id=task_id)
                    entry["cancel_receipt"] = {"status": receipt.get("status"),
                                               "already_terminal": bool(receipt.get("already_terminal"))}
                except Exception as exc:
                    entry["cancel_receipt"] = {"error": str(exc)[:200]}
            # 晚到结果：取消后任务可能已完成（回执 already_terminal=completed）→ 照实存档
            await self._archive_if_completed(entry, task_id)
            entry.setdefault("finished_note", f"episode {status}，观看关联已收尾")
            changed = True

        # 2) 活动episode是刷视频事件 → 首次建链（选片 + 提交，回执先记）
        if active_id:
            metadata = active.get("metadata") if isinstance(active.get("metadata"), dict) else {}
            event_key = str(metadata.get("event_key") or "")
            entry = links.get(active_id)
            if event_key == WATCH_EVENT_KEY and not (isinstance(entry, dict) and entry.get("task_id")):
                entry = entry if isinstance(entry, dict) else {}
                links[active_id] = entry
                await self._start_watching(active_id, entry)
                changed = True
            # 3) 已链接进行中 → 有界状态轮询（每拍一次，不忙轮询）
            if isinstance(entry, dict) and entry.get("task_id") and not entry.get("closed") \
                    and str(entry.get("stage")) not in ("completed", "unavailable", "skipped", "failed_submit"):
                progressed = await self._poll_status(entry)
                changed = changed or progressed

        # 4) 链接库收口（有界）
        if changed:
            self._compact(links)
            links_doc["links"] = links
            self._save_links(links_doc)

    # ------------------------------------------------------------------
    async def _start_watching(self, episode_id: str, entry: Dict[str, Any]) -> None:
        entry.setdefault("started_at", time.time())
        try:
            pick = await self._api(action="next_candidate")
        except Exception as exc:
            entry["stage"] = "unavailable"
            entry["reason"] = f"观看能力不可用：{str(exc)[:200]}"
            if self._log:
                self._log.info("刷视频事件 %s：观看能力不可用，事件照常进行（%s）", episode_id, entry["reason"])
            return
        candidate = pick.get("candidate") if isinstance(pick, dict) else None
        if not candidate:
            entry["stage"] = "skipped"
            entry["reason"] = f"无真实候选（{pick.get('reason') if isinstance(pick, dict) else 'unknown'}）；不编造 BV 号"
            if self._log:
                self._log.info("刷视频事件 %s：跳过（%s）", episode_id, entry["reason"])
            return
        try:
            receipt = await self._api(action="submit", video=str(candidate.get("bvid")),
                                      purpose="日程刷视频", episode_id=episode_id)
        except Exception as exc:
            entry["stage"] = "failed_submit"
            entry["reason"] = f"提交失败：{str(exc)[:200]}"
            if self._log:
                self._log.warning("刷视频事件 %s 提交失败：%s", episode_id, entry["reason"])
            return
        if receipt.get("success") and receipt.get("task_id"):
            entry["task_id"] = str(receipt["task_id"])
            entry["bvid"] = str(candidate.get("bvid"))
            entry["stage"] = str(receipt.get("status") or "queued")
            entry["cached"] = bool(receipt.get("cached"))
            if self._log:
                self._log.info("刷视频事件 %s 关联观看任务 %s（%s）", episode_id, entry["task_id"], entry["stage"])
        else:
            entry["stage"] = "failed_submit"
            entry["reason"] = str(receipt.get("error") or "提交被拒绝")[:200]
            entry["error_class"] = receipt.get("error_class")

    async def _poll_status(self, entry: Dict[str, Any]) -> bool:
        task_id = str(entry.get("task_id") or "")
        try:
            receipt = await self._api(action="status", task_id=task_id)
        except Exception as exc:
            entry["stage"] = "unavailable"
            entry["reason"] = f"观看能力不可用：{str(exc)[:200]}"
            return True
        if not receipt.get("success"):
            return False
        stage = str(receipt.get("status") or "")
        if entry.get("stage") != stage:
            entry["stage"] = stage
            if self._log and stage in ("running", "completed", "failed", "cancelled"):
                self._log.info("观看任务 %s 阶段：%s", task_id, stage)
            if stage == "completed":
                # episode 仍在进行：真实完成结果即刻存档到关联（简短结果/感想记录到该活动）
                await self._archive_if_completed(entry, task_id)
                return True
            return True
        return False

    async def _archive_if_completed(self, entry: Dict[str, Any], task_id: str) -> None:
        """晚到/收尾时的真实完成结果存档（不重开 episode、不回填时间）。"""
        try:
            receipt = await self._api(action="result", task_id=task_id)
        except Exception:
            return
        if isinstance(receipt, dict) and receipt.get("success"):
            result = receipt.get("result")
            if isinstance(result, dict) and str(result.get("status")) == "completed":
                entry["result"] = {
                    "bvid": result.get("bvid") or (result.get("video") or {}).get("bvid"),
                    "title": (result.get("video") or {}).get("title"),
                    "watched_range": result.get("watched_range"),
                    "impression": result.get("impression"),
                    "visual_status": (result.get("visual") or {}).get("status"),
                    "finished_at": result.get("finished_at"),
                }

    # ------------------------------------------------------------------
    def _compact(self, links: Dict[str, Any]) -> None:
        now = time.time()
        stale = [eid for eid, e in links.items()
                 if isinstance(e, dict) and e.get("closed")
                 and now - float(e.get("started_at") or now) > _CLOSED_KEEP_DAYS * 86400]
        for eid in stale:
            links.pop(eid, None)
        closed = [eid for eid, e in links.items() if isinstance(e, dict) and e.get("closed")]
        if len(closed) > _CLOSED_MAX_ENTRIES:
            closed.sort(key=lambda eid: float(links[eid].get("started_at") or 0))
            for eid in closed[:len(closed) - _CLOSED_MAX_ENTRIES]:
                links.pop(eid, None)
