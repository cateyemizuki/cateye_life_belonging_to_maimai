"""双语双根命令注册面（2026-11-13 命令体系重构批）。

只注册两个根命令
================
- ``/lbm <group> <action> [args]``（英文根）
- ``/麦麦生活 <组> <动作> [参数]``（全中文根，分支也中文）

两者共用 :mod:`core.command_registry`（单一真源）与 :mod:`core.command_dispatcher`
（双语归一 + public/admin 权限闸 + handler 调用）。业务 handler 仍在
``companion`` / ``planning`` / ``qzone`` / ``emotion`` 各组件内——本模块只做
宿主注册面与帮助投递（``tools/`` 的唯一职责：注册面；不持业务状态）。

帮助投递（用户裁定）
====================
根命令无参 / ``help`` / ``帮助``：
1. 合并转发第一节点 = help 总览 + 分支概览；
2. 每个组各占一个独立节点（状态/日程/空间/情绪/诊断/运维）；
3. 可选第 8 节点 = 命令图（``core/command_image``，render_kit 面板风格）；
4. forward 失败 → 纯文本回退（拼接 7 个文字节点），图片另走 ``send.image``
   尽力而为，绝不把 base64 混进文本。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from maibot_sdk import Command

from core import command_registry as registry
from core.command_dispatcher import (
    DISPATCHER,
    ROOT_PATTERNS,
    DispatchContext,
    ParsedCommand,
    extract_root_args,
)
from core.command_image import render_command_image


class CommandRootComponentsMixin:
    """根命令注册 + 帮助合并转发（插件主类多继承）。"""

    command_root_components_info: ClassVar[Dict[str, str]] = {
        "/lbm": "Command：英文根命令（/lbm <group> <action> [args]；6 组 + help）",
        "/麦麦生活": "Command：全中文根命令（/麦麦生活 <组> <动作> [参数]；分支中文）",
    }

    # ------------------------------------------------------------------
    # 权限口径（与既有 admin_users / 本机控制台一致）
    # ------------------------------------------------------------------
    def _cmd_is_admin(self, user_id: str, is_local_operator: bool = False) -> bool:
        """优先复用 qzone Mixin 的 ``_qz_is_admin``；孤立宿主下按同口径回退。"""
        checker = getattr(self, "_qz_is_admin", None)
        if callable(checker):
            try:
                return bool(checker(str(user_id or ""), bool(is_local_operator)))
            except Exception:
                return False
        if is_local_operator:
            return True
        try:
            admins = {
                str(item).rsplit(":", 1)[-1]
                for item in (getattr(getattr(self.config, "admin", None),
                                     "admin_users", None) or [])
            }
        except Exception:
            admins = set()
        return bool(admins) and str(user_id or "").rsplit(":", 1)[-1] in admins

    # ------------------------------------------------------------------
    # 根命令
    # ------------------------------------------------------------------
    @Command(
        name="/lbm",
        pattern=ROOT_PATTERNS["en"],
        description="属于麦麦的生活 · 英文根命令（status/plan/qzone/emotion/diag/ops + help）。"
                    "无参显示合并转发帮助；/lbm help <group> 查看组内用法。",
    )
    async def cmd_root_lbm(self, **kwargs: Any) -> tuple:
        return await self._cmd_root_entry("en", kwargs)

    @Command(
        name="/麦麦生活",
        pattern=ROOT_PATTERNS["cn"],
        description="属于麦麦的生活 · 全中文根命令（状态/日程/空间/情绪/诊断/运维 + 帮助）。"
                    "无参显示合并转发帮助；/麦麦生活 帮助 <组> 查看组内用法。",
    )
    async def cmd_root_cn(self, **kwargs: Any) -> tuple:
        return await self._cmd_root_entry("cn", kwargs)

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------
    async def _cmd_root_entry(self, lang: str, kwargs: Dict[str, Any]) -> tuple:
        stream_id = str(kwargs.get("stream_id", "") or "")
        parsed: ParsedCommand = DISPATCHER.parse(lang, extract_root_args(lang, kwargs))

        async def _send_text(text: str) -> None:
            await self.ctx.send.text(text=text, stream_id=stream_id)

        async def _send_overview(overview_lang: str) -> None:
            await self._cmd_send_help(overview_lang, stream_id)

        ctx = DispatchContext(
            stream_id=stream_id,
            user_id=str(kwargs.get("user_id", "") or ""),
            is_local_operator=bool(kwargs.get("is_local_operator", False)),
            is_admin=self._cmd_is_admin,
            resolve=lambda name: getattr(self, name, None),
            send_text=_send_text,
            send_overview=_send_overview,
        )
        try:
            return await DISPATCHER.execute(parsed, ctx)
        except Exception as exc:
            logger = getattr(self.ctx, "logger", None)
            if logger is not None:
                logger.error(f"根命令执行失败（{parsed.root} {parsed.args}）: {exc}")
            try:
                await _send_text(f"命令执行失败：{exc}")
            except Exception as send_exc:
                if logger is not None:
                    logger.error(f"根命令失败提示发送失败: {send_exc}")
            return False, f"命令执行失败: {exc}", True

    # ------------------------------------------------------------------
    # 帮助投递（1 总览 + 6 组 + 可选命令图）
    # ------------------------------------------------------------------
    async def _cmd_send_help(self, lang: str, stream_id: str) -> tuple:
        nodes = registry.help_forward_nodes(lang)
        records: List[Dict[str, Any]] = [
            {"user_id": "0", "nickname": nickname,
             "segments": [{"type": "text", "content": text}]}
            for nickname, text in nodes
        ]
        image_base64 = await self._cmd_draw_help_image()
        if image_base64:
            records.append({
                "user_id": "0",
                "nickname": "命令图 · Command map",
                "segments": [{"type": "image", "content": image_base64}],
            })

        logger = getattr(self.ctx, "logger", None)
        try:
            await self.ctx.send.forward(records, stream_id)
            return True, "帮助已发送（合并转发）", True
        except Exception as exc:
            if logger is not None:
                logger.warning(f"命令帮助合并转发失败，回退纯文本: {exc}")

        fallback = "\n\n".join(text for _nickname, text in nodes)
        try:
            await self.ctx.send.text(text=fallback, stream_id=stream_id)
        except Exception as exc:
            if logger is not None:
                logger.error(f"命令帮助文本回退失败: {exc}")
            return False, f"发送失败: {exc}", True
        if image_base64:
            try:
                await self.ctx.send.image(image_base64, stream_id)
            except Exception as exc:
                if logger is not None:
                    logger.info(f"命令图发送失败（帮助文字已送达，忽略）: {exc}")
        return True, "帮助已发送（文本回退）", True

    async def _cmd_draw_help_image(self) -> Optional[str]:
        """命令图（可选节点）；PIL/字体/渲染/超时任一失败静默返回 None。"""
        data_dir = getattr(self, "_data_dir", None)
        output_dir = Path(data_dir) / "images" if data_dir is not None else None
        try:
            _path, image_base64 = await asyncio.wait_for(
                asyncio.to_thread(render_command_image, output_dir), timeout=15.0)
            return image_base64
        except Exception as exc:
            logger = getattr(self.ctx, "logger", None)
            if logger is not None:
                logger.info(f"命令图绘制放弃（静默降级为文字帮助）: {exc}")
            return None
