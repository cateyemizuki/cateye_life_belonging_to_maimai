"""QQ 空间 Web CGI 直连客户端 + cookie 管理（实现路线参考插件市场「麦麦空间 Maizone」
v3.0.3 / qzone-toolkit，接口与 SnowLuma 协议层 `protocol/src/web/qzone.ts` 一致）。

为什么需要直连 CGI：SnowLuma v1.12.10 动作面的 get_qzone_msg_list 在 mapMsgList
映射时丢弃了 CGI 返回的 commentlist，动作面读不到评论正文。而
emotion_cgi_msglist_v6 的原始响应自带 commentlist（评论者昵称/QQ/评论ID/时间 +
list_3 楼中楼），配合 emotion_cgi_re_feeds 的 commentId/commentUin 字段即可实现
「回复自己动态下别人的评论」。

cookie 获取（多级降级，与 Maizone 相同思路）：
1. 适配器 API：adapter.napcat.account.get_cookies（domain=user.qzone.qq.com，
   官方 SnowLuma/NapCat 适配器均暴露；影子适配器可走 action.call 透传）；
2. NapCat HTTP 服务器 POST /get_cookies（备选，需在 NapCat 开 http server）；
3. 本地 cookies.json 文件回退。
刷新节流：1 小时内不重复拉取（内存时间戳 + 文件回退）。

g_tk 算法：djb2(skey) & 0x7FFFFFFF——与 SnowLuma 的 getBknFromCookie /
calculateBkn 完全一致，适配器签发的 cookie 通用。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

COOKIE_DOMAIN = "user.qzone.qq.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# get_cookies 的候选 API（与 qzone.py 的动作通道同思路：命中后缓存）
_GET_COOKIES_APIS = (
    ("adapter_semantic", "adapter.napcat.account.get_cookies", False),
    ("adapter_semantic_full", "maibot-team.snowluma-adapter.adapter.napcat.account.get_cookies", False),
    ("shadow_passthrough", "adapter.snowluma.action.call", True),
    ("shadow_passthrough_full", "github.cateye.snowluma-shadow-adapter.adapter.snowluma.action.call", True),
    ("napcat_passthrough", "adapter.napcat.action.call", True),
)


def generate_gtk(skey: str) -> str:
    """djb2(skey) & 0x7FFFFFFF，与 SnowLuma getBknFromCookie 同算法。"""
    hash_val = 5381
    for ch in skey:
        hash_val += (hash_val << 5) + ord(ch)
    return str(hash_val & 2147483647)


def parse_cookie_string(cookie_str: str) -> Dict[str, str]:
    """把 "k1=v1; k2=v2" 形态的 cookie 字符串解析成字典。"""
    result: Dict[str, str] = {}
    for pair in str(cookie_str).split(";"):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key.strip()] = value.strip()
    return result


class QzoneCookieManager:
    """多级降级获取并缓存 qzone.qq.com cookie；刷新有 1 小时节流。"""

    def __init__(
        self,
        ctx: Any,
        data_dir: Path,
        *,
        napcat_http_host: str = "",
        napcat_http_port: str = "",
        napcat_http_token: str = "",
        refresh_interval_hours: float = 1.0,
    ) -> None:
        self._ctx = ctx
        self._path = Path(data_dir) / "qzone_cookies.json"
        self._http_host = napcat_http_host
        self._http_port = napcat_http_port
        self._http_token = napcat_http_token
        self._refresh_interval = max(0.25, float(refresh_interval_hours)) * 3600
        self._last_refresh = 0.0
        self._api_channel = ""  # 命中的适配器通道
        self._cached: Optional[Dict[str, str]] = None

    # ---- 各级来源 ----
    async def _fetch_via_adapter(self) -> Optional[Dict[str, str]]:
        action = "get_cookies"
        params = {"domain": COOKIE_DOMAIN}
        candidates = list(_GET_COOKIES_APIS)
        if self._api_channel:  # 命中过的通道优先
            candidates.sort(key=lambda c: c[0] != self._api_channel)
        for channel, api_name, passthrough in candidates:
            try:
                if passthrough:
                    resp = await self._ctx.api.call(api_name, action_name=action, params=params)
                else:
                    resp = await self._ctx.api.call(api_name, params=params)
            except Exception:
                continue
            if not isinstance(resp, dict) or resp.get("status") != "ok":
                continue
            data = resp.get("data") or {}
            cookie_str = str(data.get("cookies") or "")
            cookies = parse_cookie_string(cookie_str)
            if cookies.get("uin"):
                self._api_channel = channel
                return cookies
        return None

    async def _fetch_via_napcat_http(self) -> Optional[Dict[str, str]]:
        if not (self._http_host and self._http_port):
            return None
        url = f"http://{self._http_host}:{self._http_port}/get_cookies"
        headers = {"Content-Type": "application/json", "User-Agent": _UA}
        if self._http_token:
            headers["Authorization"] = f"Bearer {self._http_token}"
        try:
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.post(url, json={"domain": COOKIE_DOMAIN}, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return None
        if data.get("status") != "ok":
            return None
        cookies = parse_cookie_string(str((data.get("data") or {}).get("cookies") or ""))
        return cookies if cookies.get("uin") else None

    def _load_local(self) -> Optional[Dict[str, str]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("uin") else None
        except (OSError, json.JSONDecodeError):
            return None

    def _save_local(self, cookies: Dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # ---- 对外 ----
    async def get_cookies(self, force: bool = False) -> Optional[Dict[str, str]]:
        import asyncio

        if not force and self._cached and (time.monotonic() - self._last_refresh) < self._refresh_interval:
            return self._cached
        for fetcher in (self._fetch_via_adapter, self._fetch_via_napcat_http, self._load_local):
            try:
                cookies = await fetcher() if asyncio.iscoroutinefunction(fetcher) else fetcher()
            except Exception:
                cookies = None
            if cookies:
                self._cached = cookies
                self._last_refresh = time.monotonic()
                self._save_local(cookies)
                return cookies
        return self._cached  # 全失败时回退旧缓存

    async def get_client(self) -> Optional["QzoneWebClient"]:
        cookies = await self.get_cookies()
        return QzoneWebClient(cookies) if cookies else None

    def status_text(self) -> str:
        if not self._cached:
            return "cookie：未获取"
        uin = str(self._cached.get("uin") or "").lstrip("o0")
        age_min = int((time.monotonic() - self._last_refresh) / 60)
        return f"cookie：uin={uin}（{age_min} 分钟前，通道 {self._api_channel or 'local'}）"


class QzoneWebClient:
    """直连 QZone Web CGI 的最小客户端（文字说说 / 读说说含评论 / 评论与回评 / 点赞）。

    cookie 形态：{"uin": "o0123456", "skey": "...", "p_skey": "...", ...}（适配器 get_cookies 签发）。
    """

    LIST_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
    PUBLISH_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
    LIKE_URL = "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com/cgi-bin/likes/internal_dolike_app"
    COMMENT_URL = "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"

    def __init__(self, cookies: Dict[str, str]) -> None:
        self.cookies = dict(cookies)
        self.uin = str(self.cookies.get("uin") or "").lstrip("o0")
        skey = self.cookies.get("p_skey") or self.cookies.get("skey") or ""
        self.gtk = generate_gtk(skey)

    # ---- 响应解析 ----
    @staticmethod
    def _parse_jsonp(text: str) -> Dict[str, Any]:
        """_preloadCallback({...}) / _Callback({...}) / 纯 JSON → dict。"""
        s = text.strip()
        start, end = s.find("{"), s.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("响应中没有 JSON 对象")
        return json.loads(s[start:end + 1])

    @staticmethod
    def _extract_code(text: str) -> Optional[int]:
        """从 JSON 或 HTML(frameElement.callback) 响应中提取 code。
        先按 JSON 解析，失败用正则容错（避免引入 bs4/json5 依赖）。"""
        try:
            return QzoneWebClient._parse_jsonp(text).get("code")
        except (ValueError, json.JSONDecodeError):
            m = re.search(r"code['\"]?\s*[:=]\s*(-?\d+)", text)
            return int(m.group(1)) if m else None

    def _headers(self, referer_uin: str) -> Dict[str, str]:
        return {
            "User-Agent": _UA,
            "Referer": f"https://user.qzone.qq.com/{referer_uin}",
            "Origin": "https://user.qzone.qq.com",
        }

    # ---- 读说说（含评论）----
    async def get_list(self, target_qq: str, num: int = 5) -> List[Dict[str, Any]]:
        """读 target_qq 的说说列表，含评论（commentlist + list_3 楼中楼）。

        返回 [{tid, content, created_time, comments: [{content, qq_account,
        nickname, comment_tid, created_time, parent_tid}]}]；失败返回 []。
        """
        params = {
            "g_tk": self.gtk,
            "uin": str(target_qq),
            "ftype": 0, "sort": 0, "pos": 0,
            "num": max(1, min(20, int(num))),
            "replynum": 100,
            "callback": "_preloadCallback",
            "code_version": 1, "format": "jsonp",
            "need_comment": 1, "need_private_comment": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.get(self.LIST_URL, params=params,
                                       headers=self._headers(str(target_qq)), cookies=self.cookies)
        except Exception:
            return []
        if res.status_code != 200:
            return []
        try:
            data = self._parse_jsonp(res.text)
        except (ValueError, json.JSONDecodeError):
            return []
        if data.get("code") != 0:
            return []
        feeds: List[Dict[str, Any]] = []
        for msg in data.get("msglist") or []:
            comments: List[Dict[str, Any]] = []

            def _push(item: Dict[str, Any], parent_tid: Optional[int]) -> None:
                raw_tid = item.get("tid")
                comments.append({
                    "content": str(item.get("content") or ""),
                    "qq_account": str(item.get("uin") or ""),
                    "nickname": str(item.get("name") or ""),
                    "comment_tid": int(raw_tid) if str(raw_tid or "").isdigit() else 0,
                    "created_time": str(item.get("createTime") or item.get("createTime2") or ""),
                    "parent_tid": parent_tid,
                })

            for c in msg.get("commentlist") or []:
                if not isinstance(c, dict):
                    continue
                parent_tid = int(c["tid"]) if str(c.get("tid") or "").isdigit() else 0
                for sub in c.get("list_3") or []:  # 楼中楼
                    if isinstance(sub, dict):
                        _push(sub, parent_tid)
                _push(c, None)
            feeds.append({
                "tid": str(msg.get("tid") or ""),
                "content": str(msg.get("content") or ""),
                "created_time": str(msg.get("created_time") or ""),
                "comments": comments,
            })
        return feeds

    # ---- 发说说 / 评论 / 回评 / 点赞 ----
    async def publish(self, content: str) -> Optional[str]:
        """发表纯文字说说，成功返回 tid。（图片说说需先走 cgi_upload_image，TODO）"""
        data = {
            "syn_tweet_verson": "1", "paramstr": "1", "who": "1",
            "con": content, "feedversion": "1", "ver": "1",
            "ugc_right": "1", "to_sign": "0", "hostuin": self.uin,
            "code_version": "1", "format": "json",
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.post(self.PUBLISH_URL, params={"g_tk": self.gtk, "uin": self.uin},
                                        data=data, headers=self._headers(self.uin), cookies=self.cookies)
        except Exception:
            return None
        if res.status_code != 200 or self._extract_code(res.text) != 0:
            return None
        try:
            return str(self._parse_jsonp(res.text).get("tid") or "") or None
        except (ValueError, json.JSONDecodeError):
            return None

    async def comment(self, fid: str, target_qq: str, content: str) -> bool:
        """评论 target_qq 空间的一条说说（fid=tid）。"""
        data = {
            "topicId": f"{target_qq}_{fid}__1", "uin": self.uin, "hostUin": str(target_qq),
            "feedsType": 100, "inCharset": "utf-8", "outCharset": "utf-8",
            "plat": "qzone", "source": "ic", "platformid": 52, "format": "fs",
            "ref": "feeds", "content": content,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.post(self.COMMENT_URL, params={"g_tk": self.gtk},
                                        data=data, headers=self._headers(self.uin), cookies=self.cookies)
        except Exception:
            return False
        return res.status_code == 200 and self._extract_code(res.text) == 0

    async def reply_comment(self, fid: str, target_qq: str, nickname: str,
                            comment_qq: str, content: str, comment_tid: int | str) -> bool:
        """回复 target_qq 空间说说下的一条指定评论（楼中楼）。

        要点（Maizone 验证过的写法）：commentId + commentUin 定位被回评论，
        内容用 @{uin:QQ,nick:昵称,auto:1} 前缀 @ 对方。
        """
        if not str(comment_qq).isdigit() or not str(comment_tid).strip():
            return False
        data = {
            "topicId": f"{target_qq}_{fid}__1", "uin": self.uin, "hostUin": str(target_qq),
            "feedsType": 100, "inCharset": "utf-8", "outCharset": "utf-8",
            "content": f"@{{uin:{comment_qq},nick:{nickname},auto:1}}{content}",
            "format": "fs", "plat": "qzone", "source": "ic", "platformid": 52,
            "ref": "feeds", "commentId": str(comment_tid), "commentUin": str(comment_qq),
            "richtype": "", "richval": "", "paramstr": "1",
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.post(self.COMMENT_URL, params={"g_tk": self.gtk},
                                        data=data, headers=self._headers(self.uin), cookies=self.cookies)
        except Exception:
            return False
        return res.status_code == 200 and self._extract_code(res.text) == 0

    async def like(self, fid: str, target_qq: str) -> bool:
        """点赞 target_qq 空间的一条说说（appid=311=说说）。"""
        unikey = f"http://user.qzone.qq.com/{target_qq}/mood/{fid}"
        data = {
            "qzreferrer": f"https://user.qzone.qq.com/{self.uin}",
            "opuin": self.uin, "unikey": unikey, "curkey": unikey,
            "appid": 311, "from": 1, "typeid": 0, "abstime": int(time.time()),
            "fid": str(fid), "active": 0, "format": "json", "fupdate": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                res = await client.post(self.LIKE_URL, params={"g_tk": self.gtk},
                                        data=data, headers=self._headers(self.uin), cookies=self.cookies)
        except Exception:
            return False
        return res.status_code == 200 and self._extract_code(res.text) == 0
