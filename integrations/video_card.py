"""video_card —— B站视频卡片分享服务（2026-11-04 卡片分享改造）。

构造配方与实测证据：union/20261104_B站视频卡片分享构造取证.md §3.3/§4、
evidence/bili_ark_generated.json.txt（接收方消息库已核验送达）。

链路（两步均经宿主透传，不走适配器常规消息管线——出站 segment_encoder
无 json 段 builder，常规管线会把卡片降级为 [unsupported:json] 文本）::

    ctx.api.call("adapter.napcat.action.call",
                 action_name="get_mini_app_ark",
                 params={type:"bili", title, desc, picUrl, jumpUrl})
      → QQ 服务器生成全新 ark（新 token/跳转链，host 自动=调用账号）
    ctx.api.call("adapter.napcat.action.call",
                 action_name="send_private_msg",
                 params={user_id, message:[{type:"json", data:{data:<ark JSON字符串>}}]})

契约（与发现清单/任务指令逐条对应）：
- 视频分享/转发一律卡片；**没有纯文本链接兜底**——卡片生成失败则不发送，
  返回确定性失败回执并留诊断（调用方按「不分享」处理）；
- 发送阶段异常/超时 = 结果不确定（uncertain=True），由调用方保守防重，
  不盲重试（与 video_companion 既有投递语义一致）；
- 本模块零业务导入（只标准库 + typing）：透传 API 面经 ``api_caller``
  注入（装配侧传 ctx.api.call）；不自行决定收件人、不拥有预算状态——
  主动预算的查询/预留/入账由装配侧按 companion 同口径接线（只有发往
  目标陪伴私聊的主动打扰计数，proactive_budget 仍是唯一计数所有者）；
- OneBot retcode=0 不等于协议层送达（取证第一轮重放教训）：回执只声称
  「适配器已受理」，最终送达核验属验收事项，不在本服务伪造。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Awaitable, Callable, Dict, Optional

#: 透传 API 名（官方适配器 apis/support.py:249；无白名单、无段重编码）
PASSTHROUGH_API = "adapter.napcat.action.call"
#: 规范视频页链接（卡片 jumpUrl；取证 §3.3：规范 BV 链比短链更稳）
JUMP_URL_TEMPLATE = "https://www.bilibili.com/video/{bvid}"
#: 卡片默认描述（取证实测配方用语；调用方可传真实分享理由覆盖）
DEFAULT_CARD_DESC = "哔哩哔哩视频分享"
#: 单步透传调用超时（秒）
_CALL_TIMEOUT_SECONDS = 30.0

_BV_FULL_RE = re.compile(r"^BV[0-9A-Za-z]{10}$")
_QQ_RE = re.compile(r"^[0-9]{5,12}$")

ApiCaller = Callable[..., Awaitable[Any]]


def _unwrap(resp: Any) -> tuple[bool, Any, str]:
    """透传回执解包（与 qzone/service.py._unwrap 同口径）：成功取 data。"""
    if resp is None:
        return False, None, "无响应"
    if isinstance(resp, dict) and resp.get("success") is False:
        return False, None, str(resp.get("error") or "宿主调用失败")[:200]
    if isinstance(resp, dict):
        status = resp.get("status")
        if status == "failed" or (isinstance(resp.get("retcode"), int) and resp["retcode"] != 0):
            return False, None, str(resp.get("wording") or resp.get("msg")
                                    or f"retcode={resp.get('retcode')}")[:200]
        if status == "ok" or resp.get("retcode") == 0 or "data" in resp:
            return True, resp.get("data"), ""
    return True, resp, ""  # 未知形态按原样透传


def _extract_ark(data: Any) -> Optional[Dict[str, Any]]:
    """从 get_mini_app_ark 回执取卡片 JSON。

    实测形态（evidence/bili_ark_generated.json.txt）：data={"data":{卡片}}；
    兼容 data 本身即卡片 dict 的形态。卡片须含 app/meta 结构，否则视为不可用。
    """
    card = data
    if isinstance(card, dict) and isinstance(card.get("data"), dict):
        card = card["data"]
    if isinstance(card, dict) and card and ("meta" in card or "app" in card):
        return card
    return None


class VideoCardService:
    """B站卡片生成及已核验群/私聊路由；不拥有授权、额度或防重状态。"""

    def __init__(self, api_caller: ApiCaller, logger: Any = None,
                 call_timeout_seconds: float = _CALL_TIMEOUT_SECONDS) -> None:
        self._api = api_caller
        self._log = logger
        self._timeout = max(1.0, float(call_timeout_seconds))

    def _warn(self, message: str) -> None:
        if self._log is not None:
            try:
                self._log.warning(message)
            except Exception:
                pass

    async def generate_ark(self, *, bvid: str, title: str, desc: str = "",
                           pic_url: str = "") -> Dict[str, Any]:
        """经 QQ 服务器生成卡片 ark；失败返回确定性失败（绝不发送兜底文本）。

        返回 ``{"success": True, "ark_json": <str>}`` 或
        ``{"success": False, "error_class": "card_generation_failed", "error": ...}``。
        """
        bvid = str(bvid or "").strip()
        if not _BV_FULL_RE.fullmatch(bvid):
            return {"success": False, "error_class": "card_generation_failed",
                    "error": "无效 BV 号，不构造卡片"}
        params = {
            "type": "bili",
            "title": str(title or "").strip() or bvid,
            "desc": str(desc or "").strip() or DEFAULT_CARD_DESC,
            "picUrl": str(pic_url or "").strip(),
            "jumpUrl": JUMP_URL_TEMPLATE.format(bvid=bvid),
        }
        try:
            resp = await asyncio.wait_for(
                self._api(PASSTHROUGH_API, action_name="get_mini_app_ark", params=params),
                timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 生成阶段失败 = 确定未发送任何消息（无防重负担）
            self._warn(f"视频卡片生成失败（{bvid}，不发送、无文本兜底）：{exc}")
            return {"success": False, "error_class": "card_generation_failed",
                    "error": f"get_mini_app_ark 调用失败：{str(exc)[:200]}"}
        ok, data, err = _unwrap(resp)
        if not ok:
            self._warn(f"视频卡片生成被拒绝（{bvid}，不发送、无文本兜底）：{err}")
            return {"success": False, "error_class": "card_generation_failed",
                    "error": f"get_mini_app_ark 回执失败：{err}"}
        card = _extract_ark(data)
        if card is None:
            self._warn(f"视频卡片回执无可用 ark 结构（{bvid}，不发送、无文本兜底）")
            return {"success": False, "error_class": "card_generation_failed",
                    "error": "get_mini_app_ark 未返回可用卡片结构"}
        try:
            ark_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            return {"success": False, "error_class": "card_generation_failed",
                    "error": f"卡片 JSON 序列化失败：{exc}"}
        return {"success": True, "ark_json": ark_json}

    async def send_video_card(self, *, user_id: Any, bvid: str, title: str,
                              desc: str = "", pic_url: str = "") -> Dict[str, Any]:
        """生成并发送视频卡片到指定 QQ 私聊（收件人由装配侧决定）。

        回执语义（与 video_companion 投递门一致）：
        - 生成失败 → ``success=False``、确定未发送（调用方可安全丢弃候选）；
        - 发送被适配器明确拒绝 → ``success=False``（确定性失败）；
        - 发送阶段异常/超时 → ``success=False, uncertain=True``（保守防重）；
        - ``success=True`` 仅表示适配器受理（retcode=0），不声称协议层送达。
        """
        user = str(user_id or "").strip()
        if not _QQ_RE.fullmatch(user):
            return {"success": False, "error_class": "invalid_target",
                    "error": "卡片收件人 QQ 号无效，不发送"}
        generated = await self.generate_ark(bvid=bvid, title=title, desc=desc, pic_url=pic_url)
        if not generated.get("success"):
            return generated
        receipt=await self.send_ark({'kind':'private','user_id':user},generated['ark_json'])
        return {**receipt,'bvid':bvid}

    async def send_ark(self, route: Dict[str, Any], ark_json: str, *, before_send: Any = None) -> Dict[str, Any]:
        """Send exactly one raw card; accepted is never a delivery assertion."""
        kind=route.get('kind');field='group_id' if kind=='group' else 'user_id'
        identifier=str(route.get(field) or '')
        if kind not in ('group','private') or not _QQ_RE.fullmatch(identifier):
            return {'success':False,'not_sent':True,'error_class':'invalid_target'}
        try:
            card=json.loads(ark_json)
            if not isinstance(card,dict) or not ('meta' in card or 'app' in card):raise ValueError()
        except (TypeError,ValueError):
            return {'success':False,'not_sent':True,'error_class':'invalid_card'}
        # The adapter API is bound to one account and has no account selector.
        # Verify registered-route ownership instead of silently sending from a
        # different logged-in bot when the host contains multiple accounts.
        account=str(route.get('account_id') or '')
        if account:
            try:
                identity=await asyncio.wait_for(self._api(PASSTHROUGH_API,
                    action_name='get_login_info',params={}),timeout=self._timeout)
                actual=''
                for _ in range(6):
                    if not isinstance(identity,dict):break
                    if identity.get('success') is False or identity.get('status')=='failed':break
                    if type(identity.get('retcode')) is int and identity['retcode']!=0:break
                    if identity.get('user_id') is not None:
                        actual=str(identity['user_id']);break
                    identity=identity.get('data')
                if not actual or actual!=account:
                    return {'success':False,'not_sent':True,'error_class':'route_account_unverified'}
            except asyncio.CancelledError:raise
            except Exception:
                return {'success':False,'not_sent':True,'error_class':'route_account_unverified'}
        if before_send is not None:
            allowed,_reason=before_send()
            if not allowed:return {'success':False,'not_sent':True,'error_class':'gate_changed'}
        action='send_group_msg' if kind=='group' else 'send_private_msg'
        params={field:int(identifier),'message':[{'type':'json','data':{'data':ark_json}}]}
        try:
            resp = await asyncio.wait_for(
                self._api(PASSTHROUGH_API, action_name=action, params=params),
                timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return {"success": False, "uncertain": True, "error_class": "unknown",
                    "error": f"{action} 结果不确定：{type(exc).__name__}"}
        accepted=False;message_id=None;current=resp
        for _ in range(6):
            if not isinstance(current,dict):break
            ret=current.get('retcode')
            if current.get('status')=='failed' or (type(ret) is int and ret!=0):
                return {'success':False,'not_sent':True,'error_class':'send_rejected'}
            if current.get('success') is False:
                return {'success':False,'uncertain':True,'error_class':'unknown'}
            accepted=accepted or current.get('status')=='ok' or type(ret) is int and ret==0 or current.get('sent') is True
            mid=current.get('message_id')
            if isinstance(mid,(str,int)) and not isinstance(mid,bool):message_id=mid
            if current.get('success') is True and isinstance(current.get('data'),dict) and current['data'].get('message_id') is not None:
                accepted=True
            current=current.get('data')
        if not accepted:return {'success':False,'uncertain':True,'error_class':'unknown'}
        return {'success':True,'channel':'video_card','delivery_state':'accepted','message_id':message_id}
