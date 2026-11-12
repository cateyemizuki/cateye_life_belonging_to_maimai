# -*- coding: utf-8 -*-
"""life → bpp 跨插件后处理桥（B4 联动批，v2 §6.3）。

**唯一职责**：在 ``mai_hook_replyer_after_response`` 发送前，向「更好的消息后处理」
（bpp）插件登记本轮认领，并按对方返回的 ``typing_owner`` 决定 life 是否让出打字
时延。**不改变** life 的任何回复内容、后处理或发送链。

契约（与 bpp ``modules/life_bridge.py`` 对应）：

- 调用名：先全名 ``github.cateye.better-post-processing.bpp_life_bridge``，
  失败（API 不存在/未激活/异常）再回退短名 ``bpp_life_bridge``；
- 总超时 1.5 秒（两次调用共享同一截止时间，绝不把 12s 钩子拖长）；
- 任何失败/超时/无响应 → 返回 ``None``，调用方**照常施加打字时延**（与 3.17.33
  逐字一致，不劣化）；
- ``typing_owner == "bpp"`` 才让位（即使 ``accepted=False``：bpp 生效时它仍会
  分段并模拟打字，life 必须让位，v2 §6.4）；
- 认领单轮有效：bpp 侧 TTL + 同流新认领覆盖旧认领，life 侧不跨轮复用。

留痕：``bpp.bridge.claim_accepted / rejected / failed / released``（写主日志；
DecisionLogger 缺失时静默）。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import time
import uuid
from typing import Any, Dict, Optional

#: bpp 的插件 id（与 bpp ``_manifest.json`` 一致）
BPP_PLUGIN_ID = "github.cateye.better-post-processing"
BPP_API_NAME = "bpp_life_bridge"
BPP_API_FULL_NAME = f"{BPP_PLUGIN_ID}.{BPP_API_NAME}"

#: 单轮认领总超时（秒）：全名 + 短名回退共享该截止时间
CLAIM_TIMEOUT_SECONDS = 1.5

#: 释放只做 best-effort，超时更短
RELEASE_TIMEOUT_SECONDS = 0.5


def text_fingerprint(text: Any) -> str:
    """回复文本指纹（与 bpp 侧同算法，用于同一条消息的认领匹配）。"""
    return hashlib.sha1(str(text or "").encode("utf-8", "replace")).hexdigest()[:16]


class PostProcessBridge:
    """跨插件认领客户端（纯网络边界；无状态持久化）。"""

    def __init__(self, plugin: Any) -> None:
        self._plugin = plugin
        #: stream_id -> 最近一次被接受的 claim_id（仅用于诊断/release）
        self._claims: Dict[str, str] = {}
        #: 最近一次失败原因（诊断）
        self.last_error = ""

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    def enabled(self) -> bool:
        """``[experimental].bpp_bridge_enabled``（主开关；2026-11-12 裁决 3 起默认 True）。"""
        section = getattr(getattr(self._plugin, "config", None), "experimental", None)
        value = getattr(section, "bpp_bridge_enabled", False)
        return bool(value) if value is not None else False

    # ------------------------------------------------------------------
    # 调用面
    # ------------------------------------------------------------------

    async def _invoke(self, payload: Dict[str, Any], *, timeout: float) -> Optional[Dict[str, Any]]:
        """全名优先、短名兜底；失败/超时/格式错 → ``None``。"""
        api = getattr(getattr(self._plugin, "ctx", None), "api", None)
        call = getattr(api, "call", None)
        if not callable(call):
            self.last_error = "api_unavailable"
            return None
        deadline = time.monotonic() + max(0.05, float(timeout))
        last_error = ""
        for api_name in (BPP_API_FULL_NAME, BPP_API_NAME):
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                last_error = "timeout"
                break
            try:
                result = call(api_name, **payload)
                if inspect.isawaitable(result):
                    result = await asyncio.wait_for(result, timeout=remaining)
            except asyncio.TimeoutError:
                last_error = "timeout"
                continue
            except Exception as exc:  # API 不存在 / 未激活 / 宿主拒绝 → 换短名
                last_error = f"{type(exc).__name__}: {exc}"
                continue
            normalized = self._normalize(result)
            if normalized is not None:
                return normalized
            last_error = "invalid_response"
        self.last_error = last_error or "no_response"
        return None

    @staticmethod
    def _normalize(result: Any) -> Optional[Dict[str, Any]]:
        """兼容宿主包装 ``{"success": true, "result": {...}}`` 与裸 dict。"""
        if not isinstance(result, dict):
            return None
        if "success" in result:
            if not bool(result.get("success")):
                return None
            inner = result.get("result")
            if isinstance(inner, dict):
                result = inner
        if not isinstance(result, dict):
            return None
        owner = str(result.get("typing_owner") or "").strip().lower()
        if owner not in ("bpp", "life"):
            return None
        return result

    # ------------------------------------------------------------------
    # 对 bpp 的三个动作
    # ------------------------------------------------------------------

    async def claim(
        self,
        stream_id: Any,
        text: Any,
        directive: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """登记本轮认领；返回 bpp 响应（含 ``active/accepted/typing_owner``）或 ``None``。"""
        if not self.enabled():
            return None
        sid = str(stream_id or "").strip()
        body = str(text or "")
        if not sid or not body.strip():
            return None
        claim_id = uuid.uuid4().hex
        payload = {
            "action": "claim",
            "claim_id": claim_id,
            "stream_id": sid,
            "text_fingerprint": text_fingerprint(body),
            "mode": "policy",
            # life 是发起方；真正的打字归属由 bpp 的响应决定（见下）。
            "typing_owner": "life",
            "fragment_hint": bool((directive or {}).get("fragment_hint")),
        }
        response = await self._invoke(payload, timeout=CLAIM_TIMEOUT_SECONDS)
        if response is None:
            self._trace("bpp.bridge.claim_failed", stream_id=sid,
                        reason=self.last_error)
            return None
        active = bool(response.get("active"))
        accepted = bool(response.get("accepted"))
        owner = str(response.get("typing_owner") or "")
        if accepted and active and owner == "bpp":
            self._claims[sid] = str(response.get("claim_id") or claim_id)
            self._trace("bpp.bridge.claim_accepted", stream_id=sid,
                        claim_id=self._claims[sid])
        elif active and owner == "bpp":
            self._trace("bpp.bridge.claim_rejected", stream_id=sid,
                        reason=str(response.get("reason") or ""))
        else:
            self._trace("bpp.bridge.claim_failed", stream_id=sid,
                        reason=str(response.get("reason") or "inactive"))
        return response

    async def typing_owner(self, stream_id: Any) -> Optional[str]:
        """只读查询当前打字归属（``status``，不会创建认领）。"""
        if not self.enabled():
            return None
        response = await self._invoke(
            {"action": "status", "stream_id": str(stream_id or "").strip()},
            timeout=CLAIM_TIMEOUT_SECONDS,
        )
        if response is None:
            return None
        if not bool(response.get("active")):
            return "life"
        return str(response.get("typing_owner") or "")

    async def release(
        self,
        claim_id: Any = None,
        stream_id: Any = None,
    ) -> bool:
        """释放认领（幂等；失败静默）。供异常/空回复/跨轮清理使用。"""
        cid = str(claim_id or "").strip()
        sid = str(stream_id or "").strip()
        if not cid and not sid:
            return False
        if cid:
            for key in [key for key, value in self._claims.items() if value == cid]:
                self._claims.pop(key, None)
        elif sid:
            self._claims.pop(sid, None)
        if not self.enabled():
            return False
        response = await self._invoke(
            {"action": "release", "claim_id": cid, "stream_id": sid},
            timeout=RELEASE_TIMEOUT_SECONDS,
        )
        if response is None:
            return False
        self._trace("bpp.bridge.released", stream_id=sid, claim_id=cid,
                    reason=str(response.get("reason") or ""))
        return True

    def pending_claim(self, stream_id: Any) -> str:
        """该流最近一次被接受的 claim_id（诊断/测试）。"""
        return self._claims.get(str(stream_id or "").strip(), "")

    # ------------------------------------------------------------------
    # 留痕
    # ------------------------------------------------------------------

    def _trace(self, event: str, **fields: Any) -> None:
        """写一行决策痕（DecisionLogger 缺失/异常静默，绝不拖挂回复链）。"""
        recorder = getattr(getattr(self._plugin, "_decision_logger", None), "record", None)
        if callable(recorder):
            try:
                recorder(action="info", trigger_type="bpp_bridge",
                         reason=event, **fields)
                return
            except Exception:
                pass
        try:
            self._plugin.ctx.logger.debug("%s %s", event, fields)
        except Exception:
            pass


__all__ = [
    "BPP_API_FULL_NAME",
    "BPP_API_NAME",
    "BPP_PLUGIN_ID",
    "CLAIM_TIMEOUT_SECONDS",
    "PostProcessBridge",
    "RELEASE_TIMEOUT_SECONDS",
    "text_fingerprint",
]
