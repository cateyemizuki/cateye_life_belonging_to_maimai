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

from .publish_guard import PublishLedger, preview_post, definitely_unavailable

import asyncio
import base64
import logging
import math
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.diag import DecisionLogger
from core.llm_utils import LLMService, purpose_task
from tools.proactive_turn import discard_turn, register_turn
from suite_prompts import QZONE_POST_DEFAULT, QZONE_REPLY_DEFAULT
from suite_prompts import (
    QZONE_COMMENT_REMINDER_DEFAULT,
    QZONE_POST_GATE_DEFAULT,
    prompt as prompt_of,
    render,
)
from .cgi import QzoneCookieManager, QzoneImagesRequiredError
from .decision import (
    DecisionHistory,
    parse_llm_decision,
)
from .image_sources import collect_images
from planning.schedule_query import ScheduleQuery
from core.storage import load_json, save_json_atomic
from planning.time_utils import is_in_time_window

logger = logging.getLogger(__name__)


def _companion_gate_of(plugin: Any) -> Any:
    """陪伴静默/午休判定门面取值（D3，2026-11-01 模块解耦计划 §3.1）。

    优先经组合根公共口 ``plugin.companion_silence_gate``（鸭子调用，本模块不导入
    companion）；公共口缺失（裸测试桩）→ 返回 None，调用方走旧 patrol 懒导入
    shim（``_PatrolGateShim``，D5 注入化收口后随退役）。
    """
    getter = getattr(plugin, "companion_silence_gate", None)
    if not callable(getter):
        return None
    try:
        return getter()
    except Exception:
        return None


class _PatrolGateShim:
    """D3 兼容 shim：无组合根门面时（裸测试桩）按旧路径懒导入 patrol 绑定。

    生产路径（``plugin.companion_silence_gate`` 恒可得）不会走到。shim 只转发、
    不持状态（§6 计划：shim 逐字转发语义，D5 全部装配显式接线后删除）。
    """

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin

    def resolve_silence_window(self) -> Any:
        from companion.patrol import resolve_silence_window

        return resolve_silence_window(self._plugin)

    def nap_item_matches(self, activity: Any) -> bool:
        from companion.patrol import nap_item_matches

        return nap_item_matches(activity)

    def actual_nap_item_matches(self, activity: Any) -> bool:
        from companion.patrol import actual_nap_item_matches
        return actual_nap_item_matches(activity)

    def nap_intent_matches_now(self, day_schedule: Any, now: Any = None) -> bool:
        from companion.patrol import nap_intent_matches_now

        return nap_intent_matches_now(day_schedule, now)

    def sleep_engine_asleep(self, now: Any = None) -> bool:
        from companion.patrol import sleep_engine_asleep

        return sleep_engine_asleep(self._plugin, now)


def _sleep_silence_window(plugin: Any) -> Tuple[str, str]:
    """空间静默窗生效值 ``(入睡 HH:MM, 起床 HH:MM)``（2026-10-20 用户指示：恒复用作息。

    §24.2 任务B（R7 依据 = 用户 2026-10-21 直派）真实化随主链自动生效：与陪伴静默共用
    同一解析真源——D3 起经组合根公共口 ``plugin.companion_silence_gate`` 绑定
    ``companion.patrol.resolve_silence_window``（插件实例路径**优先解析真实睡眠环锚点**：
    今晚/今晨睡觉条目读取即解析，赖床顺延后随移）；公共口缺失的裸桩走
    ``_PatrolGateShim`` 旧懒导入路径；companion 未装配时本地的 ``[schedule]``
    直读降级、锚点缺失再退出厂兜底（23:00/07:00，仍是静默窗）不变。
    返回**未规范化**原文，跨午夜与非法 fail-safe 交给 ``is_in_time_window``。
    """
    gate = _companion_gate_of(plugin)
    if gate is None:
        gate = _PatrolGateShim(plugin)
    try:
        window = gate.resolve_silence_window()
        return window.start, window.end
    except Exception:  # pragma: no cover - companion 未装配时的降级路径
        schedule = getattr(getattr(plugin, "config", None), "schedule", None)
        wake = str(getattr(schedule, "wake_time", "") or "").strip()
        sleep = str(getattr(schedule, "sleep_time", "") or "").strip()
        return (sleep or "23:00"), (wake or "07:00")

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

QZONE_POST_PROMPT = QZONE_POST_DEFAULT

QZONE_REPLY_PROMPT = QZONE_REPLY_DEFAULT

# 已处理记录的容量上限（LRU，参考 Maizone processed_list 设计）
_MAX_PROCESSED_FEEDS = 200
_MAX_PROCESSED_COMMENTS = 200

#: P1-12：单轮自动回评的尝试总量上限（每条尝试 = 1 次 LLM 生成 + 至多 1 次楼中楼发送，
#: 另含 3~4s 拟人间隔）。理论读面为 reply_number(≤20) 条说说 × 各 100 条评论，不设上限
#: 时极端评论涌入会让单轮成本与时长不可控；超限评论留待下轮，不标记已处理。
_MAX_REPLIES_PER_ROUND = 5

#: mood_effect.post_rate 的上限（附录 C：post_rate 允许 0~1.2；乘到配置发帖率后按概率语义夹到 1.0）
MOOD_POST_RATE_MAX = 1.2

#: 情绪不可得时的恒等 mood_effect（emotion 关闭/缺失 → 不门控、不拼文案，§八.2）
NEUTRAL_MOOD_EFFECT: Dict[str, Any] = {
    "proactive_rate": 1.0,
    "miss_threshold": 1.0,
    "post_rate": 1.0,
    "intent_hint": "",
}

#: PR-D1（H9）：主题记忆有界读取——B1 索引 published/qzone_posted recent ≤5
#: （与 A2 文案防复读 CAPTION_RECENT_LIMIT 同口径）；不新建表，复用 B1。
THEME_RECENT_LIMIT = 5
#: PR-D1：主题摘要合并去重后的总行数上限（索引 ≤5 + 自己说说 ≤5，有界不拖挂 prompt）
THEME_BLOCK_MAX_LINES = 8
#: PR-D1：公开/私密分域防御过滤——含这些标记的摘要绝不进空间公开 prompt
#: （B1 正常不存聊天正文；此为写入方失守时的第二道闸，保守宁缺毋滥）。
_PRIVATE_MARKERS = ("私聊", "群聊")
#: PR-D1：主题块稳定规则前缀（规则前置、事实变量后置；3.9.1 前缀缓存口径）。
#: 红线措辞：记得自己发过 ≠ 别人看过/点赞过；跨渠道独立表达不混淆接收者。
QZONE_THEME_BLOCK_HEADER = (
    "【近期自己的空间主题】以下是你自己已在空间公开发布的内容事实"
    "（记得自己发过≠别人看过或点赞过，不要声称任何人的浏览/点赞/评论反馈）。\n"
    "避免自我复读：不重复同样的措辞与内容；允许主题自然延续——"
    "连续几天对同一兴趣可以递进出新角度、新进展，而不是原样重发。\n"
    "这里只面向空间公开读者：不提及私聊/群聊里的人与内容；"
    "聊天里分享过什么与空间发过什么是两回事，各自独立表达、不混淆接收者。"
)


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
        #: PR-RELIABILITY（3.17.17 §1E）：评论提醒触发异常告警节流（tid → 上次告警 epoch 秒）
        self._comment_trigger_warn_at: Dict[str, float] = {}
        self._publish_ledger = PublishLedger()
        self._publish_lock = asyncio.Lock()
        self._send_ambiguous = False
        #: §18.4 发帖判定历史（deque，最多 20 条，最新在前；进程内、热重载清零——调试用途）
        self._decision_history = DecisionHistory(20)
        #: 进程内事件总线（M0；plugin._bus 在服务束构造前已就绪，可空降级）
        self.bus = getattr(self._plugin, "_bus", None)
        #: 本束的事件反订阅闭包（M0 无订阅者；M3+ 在 _register_event_subscriptions 里追加）
        self._event_unsubs: list = []

    # ---- 跨模块只读面（D3，2026-11-01 模块解耦计划 §3.1） ----
    def _companion_gate(self) -> Any:
        """陪伴静默/午休判定门面：组合根公共口鸭子调用，不导入 companion。

        公共口缺失（裸测试桩）→ ``_PatrolGateShim`` 旧 patrol 懒导入路径逐字保留
        （shim 登记：qzone→companion.patrol 兼容转发，D5 全部装配显式接线后删除）。
        """
        return _companion_gate_of(self._plugin) or _PatrolGateShim(self._plugin)

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
        #: §18.4 冷却锚点（**只在发送成功后更新**，落盘跨热重载；ISO 字符串）
        self._state.setdefault("last_post_at", "")
        self._publish_ledger = PublishLedger(Path(data_dir) / "qzone_publish_receipts.json")

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
        candidates += [
            ("semantic", f"adapter.napcat.qzone.{action}"),
            ("semantic_full", f"maibot-team.snowluma-adapter.adapter.napcat.qzone.{action}"),
            ("passthrough_snowluma", _PASSTHROUGH_APIS[0]),
            ("passthrough_snowluma_full", _PASSTHROUGH_APIS[1]),
            ("passthrough_napcat", _PASSTHROUGH_APIS[2]),
        ]
        if cached:
            candidates.sort(key=lambda row: row[0] != cached)
        if kind == "send":
            self._send_ambiguous = False
        last_error = ""
        for channel, api_name in candidates:
            try:
                if channel.startswith("passthrough"):
                    resp = await self._ctx.api.call(api_name, action_name=action, params=params)
                else:
                    resp = await self._ctx.api.call(api_name, params=params)
            except Exception as e:
                last_error = f"{api_name}: {e}"
                if kind == "send" and not definitely_unavailable(e):
                    self._send_ambiguous = True
                    return False, None, "发布结果待核对（动作未返回确定回执）"
                continue
            ok, data, err = self._unwrap(resp)
            if ok:
                self._channel_cache[kind] = channel
                return True, data, ""
            if kind == "send" and not definitely_unavailable(err):
                self._send_ambiguous = True
                return False, None, "发布结果待核对（适配器未给出可确认回执）"
            last_error = f"{api_name}: {err}"
        return False, None, last_error or "所有调用通道均不可用（检查 SnowLuma 适配器）"

    # ---- 说说读写 ----
    async def post_moment(self, text: str, *, images: Optional[List[bytes]] = None,
                          ugc_right: int = 1, require_images: bool = False,
                          enforce_daily_limit: bool = False) -> Optional[Dict[str, Any]]:
        """Serialize publishing and persist intent before dispatch; never replay uncertainty.

        ``require_images``（PR-A2，默认 False 逐字保持普通说说基线）：True 时图片为硬性
        要求——空图/图全挂确定未发（返回 None，不置 ``_send_ambiguous``），绝不退纯文本。

        ``enforce_daily_limit``（PR-A3，默认 False 逐字保持手动/命令直发基线）：True 时
        在发布锁内复检共享每日额度——两套自动触发器（日程普通说说 + 视频候选）共用同一
        把 ``_publish_lock`` 串行分发，锁内复检消除「先查额度后分发」的 TOCTOU 竞争，
        保证并发不超额（计划书 §8.1.7）。额度已用尽 → 确定未发（None，不置 ambiguous）。
        """
        if not str(text or "").strip():
            return None
        visibility = int(ugc_right) if ugc_right in (1, 4, 16, 64, 128) else 1
        if require_images:
            # PR-A2：空图确定未发——在回执账本 begin 之前早退，不留 image_count=0 的
            # retry_wait 行（否则 _process_delivery_queue 会把它当纯文本重放，违反红线）。
            valid = [img for img in (images or [])[:4]
                     if isinstance(img, (bytes, bytearray)) and img]
            if not valid:
                self._send_ambiguous = False
                self._log("skip", trigger_type="qzone_post",
                          reason="require_images 但无有效图片（确定未发，不退纯文本）")
                return None
        preview = preview_post(text, images, visibility)
        async with self._publish_lock:
            if enforce_daily_limit:
                # PR-A3：锁内复检共享额度（分发串行点），并发触发器不抢额度、不超额。
                cfg = self._qzone_cfg()
                limit = int(getattr(cfg, "daily_max_posts", 0) or 0)
                if limit > 0 and self.posts_today() >= limit:
                    self._send_ambiguous = False
                    self._log("skip", trigger_type="qzone_post",
                              reason="共享每日额度已用尽（发布锁内复检，确定未发）")
                    return None
            allowed, receipt = self._publish_ledger.begin(preview)
            if not allowed:
                if receipt.get("status") == "sent":
                    return dict(receipt.get("result") or {}) or None
                self._log("skip", trigger_type="qzone_post", reason="发布已排队或结果待核对，不重复发送",
                          delivery_state=str(receipt.get("status") or "unknown"))
                return None
            try:
                result = await self._post_moment_once(text, images=images, ugc_right=visibility,
                                                      require_images=require_images)
            except BaseException:
                self._publish_ledger.finish(preview["key"], "uncertain", error="interrupted")
                raise
            status = "sent" if result else "uncertain" if self._send_ambiguous else "retry_wait"
            self._publish_ledger.finish(preview["key"], status, result=result,
                                        error="" if result else "outcome_unknown" if self._send_ambiguous else "unavailable")
            return result

    async def _post_moment_once(self, text: str, *, images: Optional[List[bytes]] = None,
                                ugc_right: int = 1, require_images: bool = False) -> Optional[Dict[str, Any]]:
        """发表说说（可带图）。优先 SnowLuma 动作通道（客户端持续维护），失败回退 Web CGI 直连。

        ``images`` 为收集层产出的 ``list[bytes]``（§18.1 契约：收集层只产出 bytes，与发布层解耦）：
        - 动作通道把 bytes 编成 ``base64://`` 形态（SnowLuma ``send_qzone_msg`` 既有能力）；
        - CGI 回退通道直传 bytes（``QzoneWebClient.publish`` 内逐张上传 + 拼 pic_bo/richval）。
        配图失败（含超限跳过）**永不阻断发布**——单张失败跳过，全部失败发纯文本。

        ``require_images=True``（PR-A2）走独立分支：图片硬性要求，保守直走受控 CGI（见下），
        默认 False 路径逐字保持。成功返回 {tid, time}（CGI 通道 time 为空串）。"""
        if not text.strip():
            return None
        image_list = [bytes(img) for img in (images or [])[:4]
                      if isinstance(img, (bytes, bytearray)) and img]
        params: Dict[str, Any] = {"content": text.strip(), "ugc_right": int(ugc_right) if ugc_right in (1, 4, 16, 64, 128) else 1}
        if image_list:
            params["images"] = [f"base64://{base64.b64encode(img).decode('ascii')}" for img in image_list]
        if require_images:
            # PR-A2：图片硬性要求。实测 SnowLuma 适配器 send_qzone_msg 为纯透传
            # （maibot-team_snowluma-adapter/apis/qzone.py 只 _call_napcat_action，不识别
            # require_images、无法保证「图全挂拒发纯文本」）→ 保守直走受控 CGI 通道
            # （自行上传、图全挂分发前拒绝），不经动作通道（规格 §2/§6：适配器不支持则走 CGI 或不发）。
            self._send_ambiguous = False
            params["require_images"] = True
            result: Optional[Dict[str, Any]] = None
            if not image_list:
                err = "require_images 但无有效图片（确定未发，不退纯文本）"
            elif params["ugc_right"] not in (1, 4, 64):
                err = f"可见性 {params['ugc_right']} 不支持 CGI（未分发，防静默改公开）"
            elif self._cookie_manager is None:
                err = "require_images 需 CGI 受控通道，cookie 管理未启用（确定未发）"
            else:
                client = await self._cookie_manager.get_client()
                if client is None:
                    err = "require_images CGI 客户端不可用（确定未发）"
                else:
                    try:
                        tid = await client.publish(text.strip(), images=image_list,
                                                   visibility=int(params["ugc_right"]),
                                                   require_images=True)
                    except QzoneImagesRequiredError:
                        tid = None
                        err = "图片全部上传失败（确定未发，不退纯文本）"
                    else:
                        if tid:
                            result = {"tid": tid, "time": ""}
                        else:
                            # 已分发但回执丢失/非零码 → 可能已发出，保守 uncertain（不自动重发）。
                            self._send_ambiguous = True
                            err = "CGI 直连失败（结果待核对）"
            self._log("trigger" if result else "skip", trigger_type="qzone_post",
                      reason=("发说说成功（含 %d 张配图）" % len(image_list)) if result
                      else f"发说说失败: {err}")
            if result:
                self._count_post()
                await self._publish("qzone.posted", tid=str(result.get("tid") or ""),
                                    summary=str(text).strip()[:40])
            return result
        ok, data, err = await self.call_action("send", params)
        result = data if ok and isinstance(data, dict) and data.get("tid") else None
        if ok and result is None:
            self._send_ambiguous = True
        if result is None and not self._send_ambiguous and self._cookie_manager is not None:
            if params["ugc_right"] not in (1, 4, 64):
                # P1-13：16/128 需要指定名单参数（target_uins），CGI 回退无法表达——
                # 拒绝回退并如实报错，绝不把非公开帖静默改成公开。未分发任何请求，
                # 不置 _send_ambiguous（结果确定未发出，走既有 retry_wait 语义）。
                err = f"{err}; 可见性 {params['ugc_right']} 不支持 CGI 回退（未回退，防静默改公开）"
            else:
                # 动作通道失败 → Web CGI 直连（发布端点相同，cookie/g_tk 与 SnowLuma 同源）
                client = await self._cookie_manager.get_client()
                if client is not None:
                    # P1-13：透传调用方可见性，不再硬编码公开
                    tid = await client.publish(text.strip(), images=image_list or None,
                                               visibility=int(params["ugc_right"]))
                    result = {"tid": tid, "time": ""} if tid else None
                    if result is None:
                        # CGI may have accepted the post before its response was lost.
                        self._send_ambiguous = True
                        err = f"{err}; CGI 直连也失败"
        self._log("trigger" if result else "skip", trigger_type="qzone_post",
                  reason=("发说说成功（含 %d 张配图）" % len(image_list)) if result and image_list
                  else ("发说说成功" if result else f"发说说失败: {err}"))
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
        # §18.4 冷却锚点：**只在发送成功后更新**（失败不重置，防失败风暴）；落盘跨热重载
        self._state["last_post_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state()

    def posts_today(self) -> int:
        counter = self._state.get("posted_today") or {}
        if counter.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        return int(counter.get("count") or 0)

    async def generate_and_post_by_schedule(self, date: str, now: datetime,
                                            post_rate: Optional[float] = None) -> bool:
        """按日程生成文案并发表（自动发帖路径）。返回是否发出。

        ``post_rate``（M4，§6.2 D）为 _tick 传入的「配置发帖率 × mood_effect.post_rate」
        乘后概率（上限按概率语义夹到 1.0）；缺省 None 时回退配置原文，兼容既有调用方。

        链路（§18.4 融合）：``require_activity``（既有前置）→ ``post_rate`` 概率层（既有）
        → **判定链**（归类 → 无活动 → 睡眠硬跳 → 冷却 → 静默复检 + 活动黑名单 → LLM 严判
        → 掷骰；MaiTrace v3.1 移植）→ 生成文案 → 收集配图 → 发表。
        """
        cfg = self._qzone_cfg()
        activity = self._schedule.find_current_activity(date, now)
        if cfg.require_activity and activity is None:
            # PR8 3.0.0（依据：用户 2026-10-27 授权直接清理 + 逐符号审定清单，规则 6）：
            # find_current_activity 已 runtime-only——日程关断时 skip 理由如实「日程未启用」，
            # 不再与 enabled=true 空闲共用「当前无日程活动」文案。
            reason = ("日程未启用（[schedule].enabled=false）" if not self._schedule_enabled()
                      else "当前无日程活动")
            self._log("skip", trigger_type="qzone_post", reason=reason)
            return False
        base_rate = self._clamp_float(getattr(cfg, "post_rate", 1.0), 1.0, 0.0, 1.0)
        effective_rate = base_rate if post_rate is None else self._clamp_float(post_rate, base_rate, 0.0, 1.0)
        if random.random() >= effective_rate:
            self._log("skip", trigger_type="qzone_post",
                      reason=f"未命中发帖概率（乘后 {effective_rate:.3f}）")
            return False
        # ── §18.4 判定链（成本从低到高：规则闸在前、LLM 在后；每道闸的拒绝都写决策历史）──
        if not await self._post_gate_chain(date, now):
            return False
        text = await self.generate_moment_text(date, now)
        if not text:
            self._record_decision("skip", gate="generate", reason="文案生成失败")
            return False
        images = await self.collect_moment_images(text)
        # PR-A3：自动日程路径与视频候选共享每日额度，锁内复检防并发超额（§8.1.7）。
        result = bool(await self.post_moment(text, images=images, enforce_daily_limit=True))
        self._decision_history.add(
            action="send" if result else "skip", gate="post",
            reason=f"发表{'成功' if result else '失败'}（配图 {len(images)} 张）",
            result="ok" if result else "fail")
        return result

    # ---- §18.4 发帖判定链（MaiTrace v3.1 移植骨架；2026-10-20 用户指示精简）----
    async def _post_gate_chain(self, date: str, now: datetime) -> bool:
        """判定链主入口：全部闸通过返回 True；任一闸拒绝 → 记历史并返回 False。

        闸序（成本从低到高）：静默时段复检 → 无活动 → 午休静默（``[patrol]
        nap_quiet_enabled`` 开启时跨模块生效，2026-10-20 用户指示）→ 冷却 →
        LLM 严判（可关）→ 掷骰上限（可关）。
        **不再按活动类型拦**：原睡眠硬跳与活动黑名单两闸已移除——「是否允许发空间」
        的时段维度只看静默（恒复用 `[schedule]` 作息 + 可选午休静默），内容质量由
        LLM 严判把关；夜间睡觉条目天然落在静默窗内，白天午睡由午休静默/LLM 闸承接。
        """
        cfg = self._qzone_cfg()
        # 闸 0a：睡眠引擎判定仍在睡（晚醒真实状态）→ 不发；日程端点在醒来时写回真实醒点，
        # 晚醒区间内窗口还写着旧 07:00 锚点，只看窗口会提前发帖（PR3 复核 2026-10-27）。
        try:
            gate = _companion_gate_of(self._plugin) or _PatrolGateShim(self._plugin)
            if gate.sleep_engine_asleep(now):
                self._record_decision("skip", gate="silence", reason="睡眠引擎判定仍在睡")
                return False
        except Exception:
            pass
        # 闸 0：静默时段（2026-10-20 起恒复用 [schedule] 作息，与陪伴静默同一真源；
        # _tick 外层已有同口径早退，此处保证链自足可单测）
        silence_start, silence_end = _sleep_silence_window(self._plugin)
        if is_in_time_window(silence_start, silence_end,
                             now.strftime("%H:%M"), invalid_result=True):
            self._record_decision("skip", gate="silence", reason="静默时段（跟随日程作息）")
            return False
        # 闸 1：无活动 → 不发（最保守兜底；activity_type 维度拦截已按 2026-10-20 指示移除）
        activity = self._schedule.find_current_activity(date, now, include_sleep=True)
        if activity is None:
            # PR8 3.0.0（规则 6）：find_current_activity runtime-only——日程关断（
            # [schedule].enabled=false）时 skip 理由如实「日程未启用」，不冒充「无活动」。
            reason = ("日程未启用（[schedule].enabled=false）" if not self._schedule_enabled()
                      else "当前无日程活动（含睡眠）")
            self._record_decision("skip", gate="no_activity", reason=reason)
            return False
        # 闸 2：午休静默（2026-10-20 用户指示：nap_quiet_enabled 由陪伴巡检扩展到空间——
        # 午休/小睡条目中不发说说、不监听评论；判定与 companion.patrol 同源——D3 起经
        # 组合根公共门面，不导入 companion）
        if self._actual_nap(now):
            self._record_decision('skip', gate='nap_quiet', reason='实际午休/小睡中，空间静默')
            return False
        activity_name = str(activity.get("name") or activity.get("description") or "活动")
        # 闸 3：冷却（锚点只在发送成功后更新；失败不重置）
        remain_minutes = self._cooldown_remain_minutes(now)
        if remain_minutes > 0:
            self._record_decision("skip", gate="cooldown",
                                  reason=f"冷却中（剩余约 {remain_minutes} 分钟）")
            return False
        # 闸 4：LLM 严判（可关；只认回复首字符「是/否」，格式异常一律按否）
        if bool(getattr(cfg, "post_llm_gate_enabled", True)):
            allowed, reason, answer = await self._llm_post_gate(activity, now)
            if not allowed:
                self._record_decision("skip", gate="llm",
                                      reason=f"LLM 严判不发（{reason or '未说明'}）",
                                      llm_answer=answer)
                return False
            self._record_decision("pass", gate="llm",
                                  reason=f"LLM 严判通过（{reason or '未说明'}）", llm_answer=answer)
        # 闸 5：可选掷骰（LLM 通过后的概率上限，防误判刷屏；1.0 = 不掷骰）
        max_chance = self._clamp_float(getattr(cfg, "post_max_chance", 1.0), 1.0, 0.0, 1.0)
        if max_chance < 1.0 and random.random() > max_chance:
            self._record_decision("skip", gate="chance",
                                  reason=f"掷骰未过（上限 {max_chance:.2f}）")
            return False
        self._record_decision("allow", gate="chain",
                              reason=f"判定链通过（{activity_name}）")
        return True

    async def check_post_gates(self, now: Optional[datetime] = None) -> Tuple[bool, str]:
        """Tool/命令直发路径的闸门复检（P1-14：与 _tick/_post_gate_chain 同口径）。

        强制 2026-10-20 用户指示「整夜不发说说」：睡眠引擎 → 静默窗 → 午休静默
        （nap_quiet_enabled 开启时，含意图行提前静默，与 _tick 同布局）→ 冷却 →
        LLM 严判（可关）。**不查**「无活动」与掷骰上限（直发允许空闲时段人工裁量，
        内容质量仍由 LLM 严判把关）；每日额度由调用方先行检查。

        每道闸拒绝都写决策历史与决策账（与自动路径同痕迹口径）。
        返回 ``(放行?, 拒绝原因)``；放行时原因为空串。
        """
        now = now or datetime.now()
        today = now.strftime("%Y-%m-%d")
        # 闸 0a：睡眠引擎判定仍在睡（晚醒真实状态；与判定链同口径，异常时由静默窗兜底）
        try:
            if self._companion_gate().sleep_engine_asleep(now):
                reason = "睡眠引擎判定仍在睡"
                self._record_decision("skip", gate="silence", reason=reason)
                return False, reason
        except Exception:
            pass
        # 闸 0：静默窗（恒复用 [schedule] 作息，与陪伴静默同一真源）
        silence_start, silence_end = _sleep_silence_window(self._plugin)
        if is_in_time_window(silence_start, silence_end,
                             now.strftime("%H:%M"), invalid_result=True):
            reason = "静默时段（跟随日程作息，整夜不发说说）"
            self._record_decision("skip", gate="silence", reason=reason)
            return False, reason
        # 午休静默（与 _tick 同口径：条目命中 + 意图行时间窗提前静默）
        if self._actual_nap(now):
            self._record_decision('skip', gate='nap_quiet', reason='实际午休/小睡中，空间静默')
            return False, "实际午休/小睡中"
        # 冷却（锚点只在发送成功后更新；与判定链闸 3 同口径）
        remain_minutes = self._cooldown_remain_minutes(now)
        if remain_minutes > 0:
            reason = f"冷却中（剩余约 {remain_minutes} 分钟）"
            self._record_decision("skip", gate="cooldown", reason=reason)
            return False, reason
        # LLM 严判（可关；与判定链闸 4 同口径，调用异常保守按不发）
        cfg = self._qzone_cfg()
        if bool(getattr(cfg, "post_llm_gate_enabled", True)):
            try:
                activity = self._schedule.find_current_activity(today, now)
            except Exception:
                activity = None
            allowed, llm_reason, answer = await self._llm_post_gate(activity, now)
            if not allowed:
                reason = f"LLM 严判不发（{llm_reason or '未说明'}）"
                self._record_decision("skip", gate="llm", reason=reason, llm_answer=answer)
                return False, reason
            self._record_decision("pass", gate="llm",
                                  reason=f"LLM 严判通过（{llm_reason or '未说明'}）", llm_answer=answer)
        return True, ""

    def _actual_nap(self, now: datetime) -> bool:
        """Qzone alone is quiet during an actual nap, never a skipped intention."""
        if not self._schedule_enabled():
            return False
        db = getattr(self._plugin, '_db', None)
        try:
            active = db.active_episode() if db is not None else None
            return bool(active and self._companion_gate().actual_nap_item_matches(active))
        except Exception:
            logger.warning('QQ空间午休事实读取失败，本轮不发布', exc_info=True)
            return True

    def _schedule_enabled(self) -> bool:
        """``[schedule].enabled``（PR8 3.0.0 唯一日程总开关；读不到/异常 → False=如实关断）。"""
        try:
            cfg = getattr(self._plugin, "config", None)
            section = getattr(cfg, "schedule", None) or getattr(self._plugin, "schedule", None)
            return bool(getattr(section, "enabled", False))
        except Exception:
            return False

    async def _llm_post_gate(self, activity: Dict[str, Any], now: datetime) -> Tuple[bool, str, str]:
        """闸 5：LLM 严判「此刻发一条说说是否自然」。返回 ``(放行, 理由, 原文)``。

        提示词从 ``[prompts].qzone_post_gate`` 渲染（写坏占位符时 render 回退默认模板）；
        判定解析走最严口径（:func:`parse_llm_decision`）；调用异常按不通过处理（保守）。
        """
        activity_desc = (self._schedule.format_activity_line(activity, now) if activity else
                         ("日程未启用" if not self._schedule_enabled() else "自由活动"))
        prompt = render(
            prompt_of(self._plugin, "qzone_post_gate"),
            fallback=QZONE_POST_GATE_DEFAULT,
            lover_name=self._lover_name,
            # PR-AUDIT（3.16.1 PR-2）：默认模板不再消费 {personality}（人设经
            # with_identity 进 system）；仍传值兼容旧自定义模板（3.9.2 占位移除先例）。
            personality=self._personality or "（暂无人设）",
            current_time=now.strftime("%H:%M"),
            activity=activity_desc,
        )
        try:
            answer = await self._llm.generate(
                prompt,
                task_name=purpose_task(self._plugin, "qzone", default="utils"),
                temperature=0.2,

                module="qzone",
                event="qzone_post_gate",
                with_identity=True,
            )
        except Exception:
            return False, "严判调用异常（保守按不发）", ""
        allowed, reason = parse_llm_decision(answer)
        return allowed, reason, str(answer or "")[:80]

    def _cooldown_remain_minutes(self, now: datetime) -> int:
        """距冷却结束还有几分钟（0 = 可发）。锚点 = ``last_post_at``（成功发送才更新）。

        锚点缺失/损坏 → 不冷却（fail-open，交 LLM 闸兜底；不让坏状态永久锁死发帖）；
        时钟回拨（锚点在将来）→ 视为冷却中（保守）。
        """
        minutes = int(self._clamp_float(
            getattr(self._qzone_cfg(), "post_cooldown_minutes", 0), 0.0, 0.0, 1440.0))
        if minutes <= 0:
            return 0
        anchor = str(self._state.get("last_post_at") or "")
        if not anchor:
            return 0
        try:
            last = datetime.fromisoformat(anchor)
        except ValueError:
            return 0
        # PR-FIX-TZ（3.17.3）：锚点由 datetime.now().isoformat() 写入（naive 本地墙钟），
        # 闸2 复检 now 为业务时区 aware → 统一 tz 后再相减，消除 TypeError 导致的
        # 「门控复检异常（保守不发）」；冷却语义（回拨保守/损坏 fail-open）不变。
        if last.tzinfo is None and now.tzinfo is not None:
            last = last.replace(tzinfo=now.tzinfo)
        elif last.tzinfo is not None and now.tzinfo is None:
            last = last.astimezone().replace(tzinfo=None)
        elapsed = (now - last).total_seconds() / 60.0
        if elapsed < 0:
            return minutes
        remaining = minutes - elapsed
        return int(math.ceil(remaining)) if remaining > 0 else 0

    def _record_decision(self, action: str, *, activity_type: str = "", gate: str = "",
                         reason: str = "", llm_answer: str = "", result: str = "") -> None:
        """写一条判定历史（deque 20）并同步既有决策日志（skip 归 qzone_post 触发类型）。"""
        self._decision_history.add(action=action, activity_type=activity_type, gate=gate,
                                   reason=reason, llm_answer=llm_answer, result=result)
        if action == "skip":
            self._log("skip", trigger_type="qzone_post", reason=f"[{gate}] {reason}")

    def decision_history_text(self, limit: int = 10) -> str:
        """发帖判定历史文本（/lbm_life qzone decisions 用；进程内 deque，重启清零）。"""
        return self._decision_history.format_text(limit=limit)

    async def collect_moment_images(self, text: str) -> List[bytes]:
        """按 ``[qzone].image_mode`` 收集本条说说的配图（收集层只产出 bytes）。

        铁律（§18.2/§18.3）：任何来源失败都静默降级——单张失败跳过、全部失败发纯文本，
        配图永不阻断发布。``off``（默认）直接返回空列表（老行为逐字一致）。
        """
        cfg = self._qzone_cfg()
        mode = str(getattr(cfg, "image_mode", "off") or "off")
        if mode.strip().lower() not in ("emoji", "library", "random"):
            return []
        images = await collect_images(
            mode=mode,
            count=int(getattr(cfg, "image_number", 1) or 1),
            description=str(text or ""),
            emoji_api=getattr(self._ctx, "emoji", None),
            library_dir=str(getattr(cfg, "image_library_dir", "") or ""),
        )
        if images:
            self._log("trigger", trigger_type="qzone_post",
                      reason=f"配图收集 {len(images)} 张（{mode.strip().lower()}）")
        else:
            self._log("skip", trigger_type="qzone_post",
                      reason=f"配图收集为空（{mode.strip().lower()}），发纯文本")
        return images

    async def _recent_theme_lines(self) -> List[str]:
        """PR-D1（H9）：近期自己空间主题的只读摘要行（稳定顺序：索引新→旧，再说说列表序）。

        来源只有两处（跨渠道分域）：
        - B1 经历索引 ``published/qzone_posted`` recent ≤5（空间发布事实；不消费
          watched_video/聊天分享等其它渠道事实，不混淆接收者）；
        - 既有 ``list_my_moments`` 有界读取 ≤5（A2 已接的同源端口；``is_private``
          的仅自己可见合并不进公开 prompt）。
        隐私红线：含私聊/群聊标记的摘要一律过滤（第二道闸）。索引缺失/开关关闭/
        读取失败/说说列表失败 → 对应来源空降级，绝不拖挂文案生成。零副作用。
        """
        lines: List[str] = []
        seen: set = set()

        def _add(text: Any) -> None:
            summary = str(text or "").strip()[:120]
            if not summary or summary in seen:
                return
            if any(marker in summary for marker in _PRIVATE_MARKERS):
                return
            seen.add(summary)
            lines.append(summary)

        index = getattr(self._plugin, "_experience_index", None)
        if index is not None:
            enabled = True
            checker = getattr(self._plugin, "_experience_enabled", None)
            if callable(checker):
                try:
                    enabled = bool(checker())
                except Exception:
                    enabled = False  # 开关不可读保守不消费（与 B1 消费口同口径）
            if enabled:
                try:
                    rows = index.recent(kinds=["qzone_posted"], limit=THEME_RECENT_LIMIT)
                except Exception:
                    rows = []
                for row in rows or []:
                    # 防御复核：只认 published 层（平台明确成功回执），其它真伪层不进。
                    if isinstance(row, dict) and str(row.get("type") or "") == "published":
                        _add(row.get("summary"))
        try:
            moments = await self.list_my_moments(num=THEME_RECENT_LIMIT)
        except Exception:
            moments = []
        for moment in moments or []:
            if isinstance(moment, dict) and not moment.get("is_private"):
                _add(moment.get("content"))
        return lines[:THEME_BLOCK_MAX_LINES]

    async def generate_moment_text(self, date: str, now: datetime) -> str:
        activity = self._schedule.find_current_activity(date, now)
        # PR8 3.0.0（规则 6）：find_current_activity runtime-only——日程关断时占位描述
        # 如实「日程未启用」，不再用「自由活动」冒充；enabled=true 空闲时保留原文案。
        activity_desc = (self._schedule.format_activity_line(activity, now) if activity else
                         ("日程未启用" if not self._schedule_enabled() else "自由活动"))
        prompt = render(
            prompt_of(self._plugin, "qzone_post"),
            fallback=QZONE_POST_PROMPT,
            lover_name="",
            personality="",
            date=date,
            time=now.strftime("%H:%M"),
            activity=activity_desc,
        )
        # M4（§6.2 D / B8）：说说随当日情绪定调（拼一行 mood_text；情绪不可得时不拼）
        mood_line = self._mood_line("请让这条说说的语气与心情保持一致")
        if mood_line:
            prompt = f"{prompt}\n{mood_line}"
        # PR-D1（H9）：近期自己空间主题摘要（变量区末尾追加；稳定规则前缀 + 事实行后置）。
        # 空索引/空说说 → 不注入，prompt 与旧行为一致（空降级）；防复读/自然延续规则
        # 同时写在 QZONE_POST_DEFAULT 稳定模板里，定制模板丢占位也不丢规则底线。
        theme_lines = await self._recent_theme_lines()
        if theme_lines:
            prompt = (f"{prompt}\n{QZONE_THEME_BLOCK_HEADER}\n"
                      + "\n".join(f"- {line}" for line in theme_lines))
        # 人设经 with_identity 以宿主同款开头注入 system_prompt（共享前缀缓存）；
        # lover_name/personality 渲染传空，兼容旧定制模板残留占位（防 KeyError 回退）。
        text = await self._llm.generate(
            prompt,
            task_name=purpose_task(self._plugin, "qzone", default="utils"),
            temperature=0.8,

            module="qzone",
            event="qzone_post",
            with_identity=True,
        )
        return text.strip().strip('"“”')

    # ---- 评论监听与回复 ----
    async def reply_comments(self) -> int:
        """自动回评主路径（Web CGI 直连，路线参考麦麦空间 Maizone）。

        读自己最近 N 条说说的评论（CGI 原始响应自带 commentlist+楼中楼）→
        过滤自己的评论与已处理评论 → LLM 生成回复 → 楼中楼回评 → 标记去重。

        标记策略（P1-12）：发送后无论成败都标记（防重复打扰）；**生成失败（空串、
        未发送）不标记**，保留下轮重试机会。单轮尝试总量受 ``_MAX_REPLIES_PER_ROUND``
        约束，超限评论留待下轮。

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
        attempts = 0
        capped = False
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
                    continue  # 已处理过（发送成败都标记，防重复打扰；生成失败不标记见下）
                # P1-12：单轮尝试总量上限——超限评论本轮不触达（不标记），留待下轮
                if attempts >= _MAX_REPLIES_PER_ROUND:
                    capped = True
                    break
                attempts += 1
                # M0 事件：收到新评论（楼中楼路径可读正文）→ emotion（附录 A ★）
                await self._publish(
                    "qzone.comment_received", tid=fid,
                    nickname=str(comment.get("nickname") or "好友"),
                    summary=str(comment.get("content") or "")[:40], nested=True,
                )
                await asyncio.sleep(3 + random.random())  # 拟人间隔
                prompt = render(
                    prompt_of(self._plugin, "qzone_reply"),
                    fallback=QZONE_REPLY_PROMPT,
                    lover_name="",
                    personality="",
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
                    task_name=purpose_task(self._plugin, "qzone", default="utils"),
                    temperature=0.8,
                    module="qzone",
                    event="qzone_reply",
                    with_identity=True,
                )
                reply_text = reply_text.strip()
                if not reply_text:
                    # P1-12：LLM 生成失败（空串、什么都没发）不标记已处理，保留下轮
                    # 重试机会；重试成本由单轮尝试上限约束。发送失败仍按既有策略标记。
                    self._log("skip", trigger_type="qzone_reply",
                              reason=f"回评文案生成失败（未发送、不标记，下轮重试）tid={fid} comment={comment_tid}")
                    continue
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
                # 发送后无论成败立即标记（Maizone 同策略：防下一轮重复回复同一条评论）；
                # 生成失败已在上方 continue，不标记（P1-12）
                self._mark_processed(fid, int(comment_tid))
            if capped:
                break
        if capped:
            self._log("skip", trigger_type="qzone_reply",
                      reason=f"单轮自动回评尝试已达上限（{_MAX_REPLIES_PER_ROUND} 条），剩余评论下轮处理")
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
                # M11（附录 II §3.3）：空间侧评论提醒**不计入**主动打扰预算——不再查闸门、
                # 不再发预算记账事件；空间侧约束仍是既有 qzone.daily_max_posts/reply_number，
                # 预算耗尽也不得拦掉本条提醒。
                turn_id = None
                try:
                    # PR-RELIABILITY（3.17.17 §1A）：登记前置到 trigger 调用前——
                    # 空间评论提醒直接经宿主 trigger 激活 planner（全仓 grep 核定的
                    # 第四处宿主调用点）→ 统一登记 other（目标私聊兜底文案）；
                    # 免预算口径不变，登记不计数；失败/异常 → discard 撤登记。
                    turn_id = register_turn(self._plugin, self._target_stream_id, "other",
                                  f"qzone_comment:{tid[:12]}")
                    reply = await self._ctx.maisaka.proactive.trigger(
                        stream_id=self._target_stream_id,
                        intent="qzone_comment",
                        reason=render(
                            prompt_of(self._plugin, "qzone_comment_reminder"),
                            fallback=QZONE_COMMENT_REMINDER_DEFAULT,
                            excerpt=excerpt, added=added,
                        ),
                    )
                    if not ((isinstance(reply, dict) and reply.get("success") is True) or reply is True):
                        discard_turn(self._plugin, turn_id)
                except Exception as exc:
                    discard_turn(self._plugin, turn_id)
                    # PR-RELIABILITY（3.17.17 §1E）：异常留痕替代静默吞——节流
                    # warning（同 tid 60s 内不重复），只记 tid 前缀+异常类名，
                    # 不记隐私正文；提醒失败不阻断基线轮询（降级 resilient 不变）。
                    now = time.time()
                    if now - self._comment_trigger_warn_at.get(tid, 0.0) >= 60.0:
                        self._comment_trigger_warn_at[tid] = now
                        if len(self._comment_trigger_warn_at) > 128:
                            oldest = min(self._comment_trigger_warn_at,
                                         key=self._comment_trigger_warn_at.get)
                            self._comment_trigger_warn_at.pop(oldest, None)
                        logger.warning("空间评论提醒触发异常 tid=%s…: %s",
                                       tid[:12], type(exc).__name__)
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
            prompt = render(
                prompt_of(self._plugin, "qzone_reply"),
                fallback=QZONE_REPLY_PROMPT,
                lover_name="", personality="",
                moment=content[:200], nickname=commenter, comment=comment_text[:200],
                time="", impression="",
            )
            # M4（§6.2 D / B8）：与主回评路径同口径拼一行情绪底色
            mood_line = self._mood_line("请让这条回复的语气与心情保持一致")
            if mood_line:
                prompt = f"{prompt}\n{mood_line}"
            reply_text = await self._llm.generate(
                prompt,
                task_name=purpose_task(self._plugin, "qzone", default="utils"),
                temperature=0.8,  module="qzone", event="qzone_reply",
                with_identity=True,
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
        """订阅注册挂点（start() 调用）。M0 本束无订阅者（M11 附录 II §3.3：空间侧不参与
        主动打扰预算——评论提醒不查闸门、不记用量；记账事件的唯一订阅者由 core/event_bus
        在装配时安装）。
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
            except Exception as exc:
                # P1-11：捕获必留痕（logger + 决策账），不再静默吞掉整轮故障；
                # 留痕自身失败也不允许杀死循环任务。
                try:
                    logger = getattr(self._ctx, "logger", None)
                    if logger is not None:
                        logger.warning(f"QQ 空间巡检轮异常（本轮中止，下轮继续）: {exc}")
                    self._log("skip", trigger_type="qzone_post",
                              reason=f"巡检轮异常（本轮中止，下轮继续）: {exc}")
                except Exception:
                    pass
            # P1-11：interval 计算纳入保护——配置段不可读/非法时回退默认 30 分钟，
            # 循环任务不得因此永久退出。
            try:
                interval = max(5, int(self._qzone_cfg().check_interval_minutes)) * 60
            except Exception:
                interval = 30 * 60
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue

    async def _process_delivery_queue(self, now: datetime) -> None:
        """Only replay posts proven not dispatched, within existing quiet/budget gates."""
        cfg = self._qzone_cfg()
        if not bool(getattr(cfg, "enabled", False)):
            return
        limit = int(getattr(cfg, "daily_max_posts", 0) or 0)
        if limit <= 0 or self.posts_today() >= limit:
            return
        last = self._state.get("last_post_at")
        try:
            if last and (now-datetime.fromisoformat(last)).total_seconds() < max(0, int(getattr(cfg,"post_cooldown_minutes",0) or 0))*60:
                return
        except (ValueError, TypeError):
            return
        for row in list(self._publish_ledger.rows.values()):
            if row.get("status") == "retry_wait" and int(row.get("attempts",0)) < 2 \
                    and float(row.get("retry_at",0)) <= now.timestamp() \
                    and row.get("image_count") == 0 and int(row.get("characters",0)) <= 5000:
                await self.post_moment(str(row.get("content") or ""), ugc_right=int(row.get("visibility",1)))
                break  # One queued attempt per existing scheduler tick.

    async def _tick(self) -> None:
        cfg = self._qzone_cfg()
        if not cfg.enabled:
            return
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        current_hhmm = now.strftime("%H:%M")
        # 静默闸（2026-10-20 起恒复用 [schedule] 作息：入睡~起床整夜不发说说、不监听评论）
        silence_start, silence_end = _sleep_silence_window(self._plugin)
        if is_in_time_window(silence_start, silence_end, current_hhmm, invalid_result=True):
            return
        # 午休静默（2026-10-20 用户指示：[patrol] nap_quiet_enabled 跨模块——命中午休/小睡
        # 条目时整轮 silent 早退，发说说与评论监听都不；与外层静默闸同布局。
        # 判定链内另有同口径闸 2，保证手动/链路直接调用时留 gate=nap_quiet 痕迹）
        if self._actual_nap(now):
            self._record_decision('skip', gate='nap_quiet', reason='实际午休/小睡中，空间静默')
            return
        await self._process_delivery_queue(now)
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
        image_mode = str(getattr(cfg, "image_mode", "off") or "off").strip().lower()
        return (
            f"QQ空间模块：{'启用' if cfg.enabled else '停用'}｜今日说说 {self.posts_today()}/{cfg.daily_max_posts}"
            f"｜配图 {image_mode}｜评论监听 {'开' if cfg.reply_comments_enabled else '关'}"
            f"｜动作通道：{channels}{cookie}"
        )

    def status_snapshot(self) -> Dict[str, Any]:
        """聚合总览用的只读状态快照（M7 /lbm_dashboard 消费）。

        只读：不发起任何空间动作、不落盘；数值与 ``status_text()`` 同源
        （``posts_today`` / 通道缓存 / cookie 文案 / 评论监听开关），
        ``comment_baseline`` 为基线覆盖的说说 tid 列表（空 = 尚未建立基线）；
        §18.4 追加 ``image_mode`` / ``cooldown_minutes`` / ``last_post_at`` / ``last_decision``
        （判定历史最近一条，热重载后为空 dict）。
        """
        cfg = self._qzone_cfg()
        try:
            cookie_text = (self._cookie_manager.status_text() if self._cookie_manager
                           else "cookie 管理：未启用")
        except Exception:
            cookie_text = "cookie 管理：读取失败"
        return {
            "enabled": bool(getattr(cfg, "enabled", False)),
            "posts_today": self.posts_today(),
            "daily_max_posts": int(getattr(cfg, "daily_max_posts", 0) or 0),
            "reply_comments_enabled": bool(getattr(cfg, "reply_comments_enabled", False)),
            "channels": sorted(set(self._channel_cache.values())) or [],
            "cookie_text": str(cookie_text),
            "comment_baseline": sorted(self._state.get("comment_baseline") or {}),
            "image_mode": str(getattr(cfg, "image_mode", "off") or "off").strip().lower(),
            "cooldown_minutes": int(getattr(cfg, "post_cooldown_minutes", 0) or 0),
            "last_post_at": str(self._state.get("last_post_at") or ""),
            "last_decision": dict(self._decision_history.last() or {}),
            "cookie_health": self._cookie_manager.status_snapshot() if self._cookie_manager and hasattr(self._cookie_manager, "status_snapshot") else {},
            "delivery": self._publish_ledger.summary(),
        }
