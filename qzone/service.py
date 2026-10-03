"""QQ 空间模块（基于 SnowLuma v1.12.10 客户端动作面实测反推）。

接口来源：SnowLuma ``../onebot/src/actions/qzone.ts``（config-CLf1PhZ6.js）与
``../protocol/src/web/qzone.ts``（index.mjs），动作参数与返回结构如下（okResponse
包裹为 ``{"status":"ok","retcode":0,"data":...}``，失败 ``{"status":"failed","retcode":...,"wording":...}``）：

- ``send_qzone_msg``      params: content(必填), images[](file://|http(s)://|base64://，自动上传),
                          ugc_right(1|4|16|64|128, 默认1), target_uins[](ugc_right=16/128 必填)
                          → data: {tid, time}
- ``get_qzone_msg_list``  params: target_uin?(省略=自己), pos(默认0), num(1~100, 默认20)
                          → data: {total, msglist: [{tid, content, time, comment_num, is_private, images[]}]}
- ``comment_qzone``       params: tid(必填), content(必填), target_uin?(省略=自己), images?[]
                          → data: {comment_id}（尽力而为）
- ``like_qzone`` / ``unlike_qzone``
                          params: tid, target_uin?(省略=自己), abstime(说说发表时间 unix 秒, 传真实值更可靠)
- ``delete_qzone_msg``    params: tid
- ``get_qzone_feeds``     params: page_num(仅首页可靠), count → data: {feeds:[{uin,nickname,time,appid,key,html}], has_more}
                          （好友动态不含 tid，无法直接点赞好友说说——已知限制）
- ``set_qzone_msg_right`` params: tid, ugc_right, target_uins?（官方适配器未单独暴露，走通用透传）

⚠️ 已知限制（v1.12.10）：``get_qzone_msg_list`` 的 CGI 响应本身带 commentlist，但客户端
mapMsgList 映射时丢弃，动作面没有任何动作能返回评论正文，也没有评论事件推送。因此：
- 「自动回评」仅在返回数据里出现评论列表字段时启用（向前兼容）；
- 当前版本的落地行为 = 评论数基线检测 + 新评论时经 maisaka 主动提醒 + 手动回评命令。
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.diag import DecisionLogger
from core.llm_utils import LLMService
from .cgi import QzoneCookieManager, QzoneWebClient
from planning.schedule_query import ScheduleQuery
from core.storage import load_json, save_json_atomic
from planning.time_utils import is_in_time_window

# ---- 调用通道（按序尝试，命中后缓存）----
# 通道1：官方 SnowLuma 适配器的语义化 QZone API（params 包一层）
_QZONE_ACTIONS = {
    "send": "send_qzone_msg",
    "list": "get_qzone_msg_list",
    "comment": "comment_qzone",
    "like": "like_qzone",
    "unlike": "unlike_qzone",
    "delete": "delete_qzone_msg",
    "feeds": "get_qzone_feeds",
    "set_right": "set_qzone_msg_right",
}
# 通道2/3：通用动作透传（影子适配器 SnowLuma 名 / NapCat 兼容名）
_PASSTHROUGH_APIS = (
    "adapter.snowluma.action.call",
    "github.cateye.snowluma-shadow-adapter.adapter.snowluma.action.call",
    "adapter.napcat.action.call",
)

QZONE_POST_PROMPT = (
    "你是「{lover_name}」，人设：{personality}\n"
    "现在是 {date} {time}，你正在：{activity}。请以第一人称发一条 QQ 空间说说，"
    "记录此刻的生活。要求：口语化、像真人发的、20~60 字、不要话题标签、不要引号、只输出说说正文。"
)

QZONE_REPLY_PROMPT = (
    "你是「{lover_name}」，人设：{personality}\n"
    "你在 QQ 空间发了这条说说：\n「{moment}」\n"
    "好友「{nickname}」（{time}）评论了：「{comment}」\n{impression}"
    "请写一条自然的回复（10~40 字，口语化，只输出回复正文）。"
)

# 已处理记录的容量上限（LRU，参考 Maizone processed_list 设计）
_MAX_PROCESSED_FEEDS = 200
_MAX_PROCESSED_COMMENTS = 200

#: mood_effect.post_rate 的上限（附录 C：post_rate 允许 0~1.2；乘到配置发帖率后按概率语义夹到 1.0）
MOOD_POST_RATE_MAX = 1.2

#: 情绪不可得时的恒等 mood_effect（emotion 关闭/缺失 → 不门控、不拼文案，§八.2）
NEUTRAL_MOOD_EFFECT: Dict[str, Any] = {
    "proactive_rate": 1.0,
    "miss_threshold": 1.0,
    "post_rate": 1.0,
    "intent_hint": "",
}


class QzoneService:
    def __init__(
        self,
        ctx: Any,
        config: Any,
        schedule_query: ScheduleQuery,
        llm: LLMService,
        decision_logger: Optional[DecisionLogger] = None,
        *,
        plugin: Any = None,
        lover_name: str = "麦麦",
        personality: str = "",
        cookie_manager: Optional[QzoneCookieManager] = None,
    ) -> None:
        self._ctx = ctx
        self._config = config
        #: 插件对象：bus/emotion 与热重载配置的动态来源（离线只传配置样对象时回退为 config 本身）
        self._plugin = plugin if plugin is not None else config
        self._schedule = schedule_query
        self._llm = llm
        self._decision_logger = decision_logger
        self._lover_name = lover_name
        self._personality = personality
        self._cookie_manager = cookie_manager
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._state_path: Optional[Path] = None
        self._state: Dict[str, Any] = {}
        self._target_stream_id = ""
        self._channel_cache: Dict[str, str] = {}  # 动作 -> 命中的通道描述
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(self._plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3+ 在 _register_event_subscriptions 里追加）
        self._event_unsubs: list = []

    # ---- 配置段访问 ----
    def _qzone_cfg(self) -> Any:
        """[qzone] 配置段。

        优先经 ``plugin.config`` 动态读取：宿主热重载会整体替换 SDK 的强类型配置实例
        （``set_plugin_config``），不能用 on_load 期的 ``self.config`` 快照，否则热重载失效；
        离线 stub 只传配置样对象（含 ``.qzone``）时回退到构造入参。
        """
        section = getattr(getattr(self._plugin, "config", None), "qzone", None)
        if section is not None:
            return section
        return getattr(self._config, "qzone", None)

    # ---- 状态与目标 ----
    def bind_state_dir(self, data_dir: Path) -> None:
        self._state_path = Path(data_dir) / "qzone_state.json"
        self._state = load_json(self._state_path, {}) or {}
        self._state.setdefault("posted_today", {"date": "", "count": 0})
        self._state.setdefault("comment_baseline", {})
        self._state.setdefault("replied_comment_ids", [])
        self._state.setdefault("processed_list", {})  # {fid: [已回评的 comment_tid, ...]}

    def set_target(self, stream_id: str) -> None:
        """目标私聊 stream（新评论主动提醒用）。"""
        self._target_stream_id = stream_id

    def set_persona(self, lover_name: str, personality: str) -> None:
        self._lover_name = lover_name or self._lover_name
        self._personality = personality or self._personality

    def _save_state(self) -> None:
        if self._state_path is not None:
            save_json_atomic(self._state_path, self._state)

    # ---- 底层动作调用 ----
    @staticmethod
    def _unwrap(resp: Any) -> tuple[bool, Any, str]:
        """统一解包：返回 (成功?, data, 错误信息)。兼容 api.call 宿主失败形态与
        SnowLuma 原始响应 {status, retcode, data, wording}。"""
        if resp is None:
            return False, None, "无响应"
        if isinstance(resp, dict) and resp.get("success") is False:
            return False, None, str(resp.get("error") or "宿主调用失败")
        if isinstance(resp, dict):
            status = resp.get("status")
            if status == "failed" or (isinstance(resp.get("retcode"), int) and resp["retcode"] != 0):
                return False, None, str(resp.get("wording") or resp.get("msg") or f"retcode={resp.get('retcode')}")
            if status == "ok" or resp.get("retcode") == 0 or "data" in resp:
                return True, resp.get("data"), ""
        return True, resp, ""  # 未知形态按原样透传

    async def call_action(self, kind: str, params: Dict[str, Any]) -> tuple[bool, Any, str]:
        """按通道顺序调用 QZone 动作；成功后把通道缓存下来，后续直接命中。"""
        action = _QZONE_ACTIONS[kind]
        cached = self._channel_cache.get(kind)
        candidates: list[tuple[str, str]] = []
        if cached:
            candidates.append((cached, ""))
        candidates += [
            ("semantic", f"adapter.napcat.qzone.{action}"),
            ("semantic_full", f"maibot-team.snowluma-adapter.adapter.napcat.qzone.{action}"),
            ("passthrough_snowluma", _PASSTHROUGH_APIS[0]),
            ("passthrough_snowluma_full", _PASSTHROUGH_APIS[1]),
            ("passthrough_napcat", _PASSTHROUGH_APIS[2]),
        ]
        last_error = ""
        for channel, api_name in candidates:
            try:
                if channel.startswith("passthrough"):
                    resp = await self._ctx.api.call(api_name, action_name=action, params=params)
                else:
                    resp = await self._ctx.api.call(api_name, params=params)
            except Exception as e:
                last_error = f"{api_name}: {e}"
                continue
            ok, data, err = self._unwrap(resp)
            if ok:
                self._channel_cache[kind] = channel
                return True, data, ""
            last_error = f"{api_name}: {err}"
        return False, None, last_error or "所有调用通道均不可用（检查 SnowLuma 适配器）"

    # ---- 说说读写 ----
    async def post_moment(self, text: str, *, images: Optional[List[str]] = None, ugc_right: int = 1) -> Optional[Dict[str, Any]]:
        """发表说说。优先 SnowLuma 动作通道（客户端持续维护），失败回退 Web CGI 直连。

        成功返回 {tid, time}（CGI 通道 time 为空串）。"""
        if not text.strip():
            return None
        params: Dict[str, Any] = {"content": text.strip(), "ugc_right": int(ugc_right) if ugc_right in (1, 4, 16, 64, 128) else 1}
        if images:
            params["images"] = [str(i) for i in images]
        ok, data, err = await self.call_action("send", params)
        result = data if ok and isinstance(data, dict) and data.get("tid") else None
        if result is None and self._cookie_manager is not None:
            # 动作通道失败 → Web CGI 直连（发布端点相同，cookie/g_tk 与 SnowLuma 同源）
            client = await self._cookie_manager.get_client()
            if client is not None:
                tid = await client.publish(text.strip())
                result = {"tid": tid, "time": ""} if tid else None
                if result is None:
                    err = f"{err}; CGI 直连也失败"
        self._log("trigger" if result else "skip", trigger_type="qzone_post",
                  reason="发说说成功" if result else f"发说说失败: {err}")
        if result:
            self._count_post()
            # M0 事件：发帖成功（含 _tick 日程发帖与手动/命令发帖）→ emotion（附录 A ★）
            await self._publish("qzone.posted", tid=str(result.get("tid") or ""),
                                summary=str(text).strip()[:40])
        return result

    async def list_my_moments(self, num: int = 10, pos: int = 0) -> List[Dict[str, Any]]:
        """拉自己最近的说说：[{tid, content, time, comment_num, is_private, images}]。"""
        ok, data, _ = await self.call_action("list", {"pos": int(pos), "num": max(1, min(100, int(num)))})
        if not ok or not isinstance(data, dict):
            return []
        msglist = data.get("msglist")
        return [m for m in msglist if isinstance(m, dict)] if isinstance(msglist, list) else []

    async def comment_moment(self, tid: str, content: str, *, target_uin: str = "") -> Optional[str]:
        """评论一条说说（普通评论，非楼中楼；target_uin 省略=自己空间）。

        优先 SnowLuma 动作通道，失败回退 Web CGI 直连；返回 comment_id（尽力而为）或 None。"""
        if not tid or not content.strip():
            return None
        params: Dict[str, Any] = {"tid": str(tid), "content": content.strip()}
        if target_uin:
            params["target_uin"] = str(target_uin)
        ok, data, err = await self.call_action("comment", params)
        comment_id = str((data or {}).get("comment_id") or "") if ok and isinstance(data, dict) else ""
        if not ok and self._cookie_manager is not None:
            client = await self._cookie_manager.get_client()
            if client is not None:
                owner = str(target_uin or client.uin)
                if await client.comment(str(tid), owner, content.strip()):
                    ok = True  # CGI 已发出；comment_id 保持空串（该端点不回传）
                else:
                    err = f"{err}; CGI 直连也失败"
        if not comment_id and not ok:
            self._log("skip", trigger_type="qzone_reply", reason=f"评论失败 tid={tid}: {err}")
            return None
        return comment_id

    async def reply_to_comment(self, fid: str, comment_qq: str, nickname: str,
                               comment_tid: int | str, content: str) -> bool:
        """楼中楼回复自己说说下的一条指定评论（只能回自己空间的：hostUin 固定为自己）。

        定位字段（commentId/commentUin/@前缀）只有 Web CGI 端点支持，动作层无此能力。"""
        if self._cookie_manager is None or not content.strip():
            return False
        client = await self._cookie_manager.get_client()
        if client is None or not client.uin:
            return False
        ok = await client.reply_comment(str(fid), client.uin, nickname or "好友",
                                        str(comment_qq), content.strip(), comment_tid)
        if ok:
            self._log("trigger", trigger_type="qzone_reply",
                      reason=f"楼中楼回复 {nickname}（{comment_qq}）：{content.strip()[:40]}")
        else:
            self._log("skip", trigger_type="qzone_reply",
                      reason=f"楼中楼回复失败 tid={fid} comment={comment_tid}")
        return ok

    async def list_own_feeds_with_comments(self, num: int = 10) -> List[Dict[str, Any]]:
        """Web CGI 读自己最近的说说（含评论+楼中楼），供手动回评命令定位。失败返回 []。"""
        if self._cookie_manager is None:
            return []
        client = await self._cookie_manager.get_client()
        if client is None or not client.uin:
            return []
        return await client.get_list(client.uin, num=max(1, min(20, int(num))))

    async def like_moment(self, tid: str, *, target_uin: str = "", abstime: int = 0) -> bool:
        """点赞一条说说（target_uin 省略=自己空间；好友说说传对方 QQ）。

        优先 SnowLuma 动作通道，失败回退 Web CGI 直连。"""
        params: Dict[str, Any] = {"tid": str(tid), "abstime": int(abstime)}
        if target_uin:
            params["target_uin"] = str(target_uin)
        ok, _, err = await self.call_action("like", params)
        if not ok and self._cookie_manager is not None:
            client = await self._cookie_manager.get_client()
            if client is not None:
                ok = await client.like(str(tid), str(target_uin or client.uin))
                if not ok:
                    err = f"{err}; CGI 直连也失败"
        if not ok:
            self._log("skip", trigger_type="qzone_like", reason=f"点赞失败 tid={tid}: {err}")
        return ok

    async def delete_moment(self, tid: str) -> bool:
        ok, _, _ = await self.call_action("delete", {"tid": str(tid)})
        return ok

    async def set_moment_right(self, tid: str, ugc_right: int, target_uins: Optional[List[str]] = None) -> bool:
        """修改说说查看权限（官方适配器未暴露，走通用透传通道）。"""
        params: Dict[str, Any] = {"tid": str(tid), "ugc_right": int(ugc_right)}
        if target_uins:
            params["target_uins"] = [str(u) for u in target_uins]
        ok, _, _ = await self.call_action("set_right", params)
        return ok

    # ---- 发帖时机 ----
    def _count_post(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        counter = self._state.setdefault("posted_today", {"date": "", "count": 0})
        if counter.get("date") != today:
            counter.update({"date": today, "count": 0})
        counter["count"] = int(counter.get("count") or 0) + 1
        self._save_state()

    def posts_today(self) -> int:
        counter = self._state.get("posted_today") or {}
        if counter.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        return int(counter.get("count") or 0)

    async def generate_and_post_by_schedule(self, date: str, now: datetime,
                                            post_rate: Optional[float] = None) -> bool:
        """按日程生成文案并发表。返回是否发出。

        ``post_rate``（M4，§6.2 D）为 _tick 传入的「配置发帖率 × mood_effect.post_rate」
        乘后概率（上限按概率语义夹到 1.0）；缺省 None 时回退配置原文，兼容既有调用方。
        """
        cfg = self._qzone_cfg()
        activity = self._schedule.find_current_activity(date, now)
        if cfg.require_activity and activity is None:
            self._log("skip", trigger_type="qzone_post", reason="当前无日程活动")
            return False
        base_rate = self._clamp_float(getattr(cfg, "post_rate", 1.0), 1.0, 0.0, 1.0)
        effective_rate = base_rate if post_rate is None else self._clamp_float(post_rate, base_rate, 0.0, 1.0)
        if random.random() >= effective_rate:
            self._log("skip", trigger_type="qzone_post",
                      reason=f"未命中发帖概率（乘后 {effective_rate:.3f}）")
            return False
        text = await self.generate_moment_text(date, now)
        if not text:
            self._log("skip", trigger_type="qzone_post", reason="文案生成失败")
            return False
        return bool(await self.post_moment(text))

    async def generate_moment_text(self, date: str, now: datetime) -> str:
        activity = self._schedule.find_current_activity(date, now)
        activity_desc = self._schedule.format_activity_line(activity, now) if activity else "自由活动"
        prompt = QZONE_POST_PROMPT.format(
            lover_name=self._lover_name,
            personality=self._personality or "（暂无人设）",
            date=date,
            time=now.strftime("%H:%M"),
            activity=activity_desc,
        )
        # M4（§6.2 D / B8）：说说随当日情绪定调（拼一行 mood_text；情绪不可得时不拼）
        mood_line = self._mood_line("请让这条说说的语气与心情保持一致")
        if mood_line:
            prompt = f"{prompt}\n{mood_line}"
        text = await self._llm.generate(
            prompt,
            task_name=self._qzone_cfg().llm_task_name,
            temperature=0.8,
            max_tokens=200,
            event="qzone_post",
        )
        return text.strip().strip('"“”')

    # ---- 评论监听与回复 ----
    async def reply_comments(self) -> int:
        """自动回评主路径（Web CGI 直连，路线参考麦麦空间 Maizone）。

        读自己最近 N 条说说的评论（CGI 原始响应自带 commentlist+楼中楼）→
        过滤自己的评论与已处理评论 → LLM 生成回复 → 楼中楼回评 → 立即标记去重。

        返回回评条数；cookie 不可用返回 -1（调用方应回退到 poll_comments 基线提醒）。
        """
        cfg = self._qzone_cfg()
        if self._cookie_manager is None:
            return -1
        client = await self._cookie_manager.get_client()
        if client is None or not client.uin:
            return -1
        if random.random() >= max(0.0, min(1.0, float(cfg.reply_probability))):
            self._log("skip", trigger_type="qzone_reply", reason="未命中回评概率")
            return 0
        feeds = await client.get_list(client.uin, num=max(1, int(cfg.reply_number)))
        if not feeds:
            self._log("skip", trigger_type="qzone_reply", reason="CGI 读取自己的说说列表为空")
            return 0
        reply_count = 0
        for feed in feeds:
            fid = str(feed.get("tid") or "")
            if not fid:
                continue
            moment_text = str(feed.get("content") or "")
            for comment in feed.get("comments") or []:
                comment_tid = comment.get("comment_tid") or 0
                comment_qq = str(comment.get("qq_account") or "").strip()
                if not comment_qq.isdigit() or not comment_tid:
                    continue  # 缺少评论者QQ/评论ID时无法定位楼中楼，跳过
                if comment_qq == client.uin:
                    continue  # 自己的评论不回
                if self._is_processed(fid, comment_tid):
                    continue  # 已处理过（无论成败都标记，防重复打扰）
                # M0 事件：收到新评论（楼中楼路径可读正文）→ emotion（附录 A ★）
                await self._publish(
                    "qzone.comment_received", tid=fid,
                    nickname=str(comment.get("nickname") or "好友"),
                    summary=str(comment.get("content") or "")[:40], nested=True,
                )
                await asyncio.sleep(3 + random.random())  # 拟人间隔
                prompt = QZONE_REPLY_PROMPT.format(
                    lover_name=self._lover_name,
                    personality=self._personality or "（暂无人设）",
                    moment=moment_text[:200],
                    nickname=comment.get("nickname") or "好友",
                    comment=str(comment.get("content") or "")[:200],
                    time=str(comment.get("created_time") or ""),
                    impression=await self._get_impression(comment_qq),
                )
                # M4（§6.2 D / B8）：回评语气随当日情绪定调（拼一行 mood_text；不可得时不拼）
                mood_line = self._mood_line("请让这条回复的语气与心情保持一致")
                if mood_line:
                    prompt = f"{prompt}\n{mood_line}"
                reply_text = await self._llm.generate(
                    prompt,
                    task_name=cfg.llm_task_name, temperature=0.8, max_tokens=120,
                    event="qzone_reply",
                )
                reply_text = reply_text.strip()
                ok = False
                if reply_text:
                    ok = await client.reply_comment(fid, client.uin, comment.get("nickname") or "好友",
                                                    comment_qq, reply_text, comment_tid)
                if ok:
                    reply_count += 1
                    self._log("trigger", trigger_type="qzone_reply",
                              reason=f"已回复 {comment.get('nickname')} 的评论：{reply_text[:40]}")
                    # M0 事件：回评成功 → emotion/companion（附录 A ★）
                    await self._publish(
                        "qzone.comment_replied", tid=fid,
                        nickname=str(comment.get("nickname") or "好友"),
                        summary=str(reply_text)[:40], nested=True,
                    )
                # 无论成败立即标记（Maizone 同策略：防下一轮重复回复同一条评论）
                self._mark_processed(fid, int(comment_tid))
        return reply_count

    def _is_processed(self, fid: str, comment_tid: int) -> bool:
        return comment_tid in (self._state.get("processed_list") or {}).get(fid, [])

    def _mark_processed(self, fid: str, comment_tid: int) -> None:
        processed: Dict[str, List[int]] = dict(self._state.get("processed_list") or {})
        tids = processed.pop(fid, [])  # pop + 重新插入 = LRU touch
        if comment_tid not in tids:
            tids.append(int(comment_tid))
            tids = tids[-_MAX_PROCESSED_COMMENTS:]
        processed[fid] = tids
        while len(processed) > _MAX_PROCESSED_FEEDS:
            processed.pop(next(iter(processed)))
        self._state["processed_list"] = processed
        self._save_state()

    async def _get_impression(self, qq: str) -> str:
        """从宿主 PersonInfo 取对评论者的印象（能力未声明/查询失败时回退）。"""
        try:
            rows = await self._ctx.db.get(model_name="PersonInfo", filters={"user_id": str(qq)})
            if rows:
                return f"你对TA的印象：{rows[0].get('memory_points') or '无特别印象'}"
        except Exception:
            pass
        return "暂无特别印象"

    async def poll_comments(self) -> Dict[str, int]:
        """评论数基线检测。

        返回 {tid: 新增评论数}。基线持久化（每条说说上轮的 comment_num）；
        首次见到某条说说只记基线不算新增（避免冷启动刷屏）。
        """
        cfg = self._qzone_cfg()
        moments = await self.list_my_moments(num=10)
        baseline: Dict[str, int] = dict(self._state.get("comment_baseline") or {})
        new_comments: Dict[str, int] = {}
        for moment in moments:
            tid = str(moment.get("tid") or "")
            if not tid:
                continue
            count = int(moment.get("comment_num") or 0)
            if tid in baseline and count > baseline[tid]:
                new_comments[tid] = count - baseline[tid]
                # 若返回里带评论列表（未来兼容）→ 自动回评
                await self._try_auto_reply(moment)
            baseline[tid] = count
        # 基线只保留本轮见到的说说
        if moments:
            self._state["comment_baseline"] = {
                str(m.get("tid")): baseline[str(m.get("tid"))] for m in moments if m.get("tid")
            }
            self._save_state()
        # 主动提醒（经 maisaka，把「收到新评论」交给 planner 人格化表达）
        if new_comments and cfg.notify_new_comments and self._target_stream_id:
            total_new = sum(new_comments.values())
            moments_by_tid = {str(m.get("tid")): m for m in moments}
            for tid, added in new_comments.items():
                moment = moments_by_tid.get(tid) or {}
                excerpt = str(moment.get("content") or "")[:30]
                # M0 预算接线（§六.2 H）：查询预算 → 发布 proactive.used → 执行触发
                if self.bus is not None and not self.bus.allow_proactive("qzone", "qzone_comment"):
                    break  # 全渠道预算已满：本条及其余提醒不再触发
                if self.bus is not None:
                    try:
                        await self.bus.publish("proactive.used", source="qzone",
                                               channel="qzone", intent="qzone_comment")
                    except Exception:
                        pass
                try:
                    await self._ctx.maisaka.proactive.trigger(
                        stream_id=self._target_stream_id,
                        intent="qzone_comment",
                        reason=f"你刚发的说说「{excerpt}」收到了 {added} 条新评论，可以在聊天里自然地提到这件事",
                    )
                except Exception:
                    pass
                self._log("trigger", trigger_type="qzone_comment",
                          reason=f"说说 {tid[:12]}… 新增 {added} 条评论（共 {total_new}）")
        return new_comments

    async def _try_auto_reply(self, moment: Dict[str, Any]) -> None:
        """动作通道返回里带评论列表时的向前兼容回评路径（v1.12.10 不会进入）。

        主路径见 reply_comments()（CGI 直连，可读评论正文与楼中楼）。"""
        cfg = self._qzone_cfg()
        comments = self._extract_comments(moment)
        if not comments:
            return
        client = await self._cookie_manager.get_client() if self._cookie_manager else None
        if client is None or not client.uin:
            return
        tid = str(moment.get("tid") or "")
        content = str(moment.get("content") or "")
        for comment in comments:
            comment_id = str(comment.get("comment_id") or comment.get("id") or comment.get("cid") or "")
            commenter = str(comment.get("nickname") or comment.get("uin") or "好友")
            comment_text = str(comment.get("content") or "")
            if not comment_id or self._is_processed(tid, int(comment_id) if comment_id.isdigit() else 0):
                continue
            prompt = QZONE_REPLY_PROMPT.format(
                lover_name=self._lover_name, personality=self._personality or "（暂无人设）",
                moment=content[:200], nickname=commenter, comment=comment_text[:200],
                time="", impression="",
            )
            # M4（§6.2 D / B8）：与主回评路径同口径拼一行情绪底色
            mood_line = self._mood_line("请让这条回复的语气与心情保持一致")
            if mood_line:
                prompt = f"{prompt}\n{mood_line}"
            reply_text = await self._llm.generate(
                prompt,
                task_name=cfg.llm_task_name, temperature=0.8, max_tokens=120, event="qzone_reply",
            )
            reply_text = reply_text.strip()
            if reply_text and await self.comment_moment(tid, reply_text):
                self._mark_processed(tid, int(comment_id) if comment_id.isdigit() else 0)
                self._log("trigger", trigger_type="qzone_reply", reason=f"已回复 {commenter} 的评论")

    @staticmethod
    def _extract_comments(moment: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从动作通道的说说条目提取评论列表（字段名容错，向前兼容）。"""
        for key in ("commentlist", "comment_list", "comments"):
            value = moment.get(key)
            if isinstance(value, list):
                return [c for c in value if isinstance(c, dict)]
        return []

    # ---- 事件订阅挂点（M0：登记于此，M3+ 消费者按需 subscribe 并存入 _event_unsubs）----
    def _register_event_subscriptions(self) -> None:
        """订阅注册挂点（start() 调用）。M0 本束无订阅者：
        唯一的实际订阅（proactive.used → 预算计数器）由 core/event_bus 在装配时安装。
        M3+ 消费者在此 ``self._event_unsubs.append(self.bus.subscribe(...))``。"""
        if self.bus is None:
            return

    def _unregister_event_subscriptions(self) -> None:
        """订阅注销挂点（stop() 调用）：执行并清空全部反订阅闭包。"""
        for unsub in self._event_unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._event_unsubs.clear()

    # ---- 循环 ----
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._register_event_subscriptions()
            self._stop_event.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._unregister_event_subscriptions()
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                pass
            interval = max(5, int(self._qzone_cfg().check_interval_minutes)) * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        cfg = self._qzone_cfg()
        if not cfg.enabled:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_hhmm = now.strftime("%H:%M")
        if is_in_time_window(cfg.silence_start, cfg.silence_end, current_hhmm, invalid_result=True):
            return
        # 按日程发说说
        if cfg.daily_max_posts > 0 and self.posts_today() < cfg.daily_max_posts and cfg.post_by_schedule:
            # M4（§6.2 D）：发帖判定 = post_rate × mood_effect.post_rate；
            # V<−0.6 且 emotion_filter（默认 true）→ 本轮跳过发帖（低落不硬营业），记 skip 原因。
            base_post_rate = self._clamp_float(getattr(cfg, "post_rate", 1.0), 1.0, 0.0, 1.0)
            mood_post_rate = self._clamp_float(
                self._mood_effect().get("post_rate"), 1.0, 0.0, MOOD_POST_RATE_MAX)
            if bool(getattr(cfg, "emotion_filter", True)) and mood_post_rate <= 0.0:
                self._log("skip", trigger_type="qzone_post",
                          reason="情绪低落（V<−0.6），本轮跳过发帖（emotion_filter 开启）")
            else:
                await self.generate_and_post_by_schedule(
                    today, now, post_rate=base_post_rate * mood_post_rate)
        # 评论监听与自动回评：CGI 直连为主（可读评论正文+楼中楼）；cookie 不可用时回退基线提醒
        if cfg.reply_comments_enabled:
            replied = await self.reply_comments()
            if replied < 0:
                await self.poll_comments()

    def _log(self, action: str, *, trigger_type: str = "", reason: str = "") -> None:
        if self._decision_logger is not None:
            self._decision_logger.record(action=action, trigger_type=trigger_type, reason=reason)

    # ---- 情绪联动（M4；§6.2 D，跨模块 getattr 判空 + 恒等降级，§八.2）----
    def _emotion_service(self) -> Any:
        """情绪模块服务束（plugin._emotion；离线 stub 只传配置样对象时同源回退）。"""
        emotion = getattr(self._plugin, "_emotion", None)
        if emotion is None:
            emotion = getattr(getattr(self._plugin, "config", None), "_emotion", None)
        return emotion

    def _mood_effect(self) -> Dict[str, Any]:
        """读 ``EmotionService.mood_effect()``（缺失/停用/异常 → 恒等中性值）。"""
        emotion = self._emotion_service()
        getter = getattr(emotion, "mood_effect", None)
        if not callable(getter):
            return dict(NEUTRAL_MOOD_EFFECT)
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return dict(NEUTRAL_MOOD_EFFECT)
            except Exception:
                return dict(NEUTRAL_MOOD_EFFECT)
        try:
            raw = getter()
        except Exception:
            return dict(NEUTRAL_MOOD_EFFECT)
        return raw if isinstance(raw, dict) else dict(NEUTRAL_MOOD_EFFECT)

    @staticmethod
    def _clamp_float(value: Any, default: float, lo: float, hi: float) -> float:
        """浮点夹取（非法值取默认；M4 概率/乘数统一防御）。"""
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = float(default)
        return max(lo, min(hi, number))

    def _mood_text(self) -> str:
        """当前情绪底色（M4/B8；情绪模块停用/缺失/异常 → 空串，故 prompt 与旧行为一致）。"""
        emotion = self._emotion_service()
        enabled = getattr(emotion, "is_enabled", None)
        if callable(enabled):
            try:
                if not enabled():
                    return ""
            except Exception:
                return ""
        getter = getattr(emotion, "mood_text", None)
        if not callable(getter):
            return ""
        try:
            return str(getter() or "").strip()
        except Exception:
            return ""

    def _mood_line(self, purpose: str) -> str:
        """情绪底色行（M4：generate_moment_text / QZONE_REPLY_PROMPT 各拼一行 mood_text）。

        ``purpose`` 是语气要求短语（说说/回复定调）；情绪不可得时返回空串，
        prompt 与旧行为完全一致（只在 .format(...) 结果后追加一行，不改字段结构）。
        """
        mood = self._mood_text()
        if not mood:
            return ""
        return f"你此刻的心情：{mood}（{purpose}）"

    # ---- 事件总线（M0；无总线时静默跳过）----
    async def _publish(self, event: str, **payload: Any) -> None:
        bus = self.bus
        if bus is None:
            return
        try:
            await bus.publish(event, source="qzone", **payload)
        except Exception:
            pass

    def status_text(self) -> str:
        cfg = self._qzone_cfg()
        channels = "、".join(sorted(set(self._channel_cache.values()))) or "未连通"
        cookie = f"｜{self._cookie_manager.status_text()}" if self._cookie_manager else "｜cookie 管理：未启用"
        return (
            f"QQ空间模块：{'启用' if cfg.enabled else '停用'}｜今日说说 {self.posts_today()}/{cfg.daily_max_posts}"
            f"｜评论监听 {'开' if cfg.reply_comments_enabled else '关'}｜动作通道：{channels}{cookie}"
        )
