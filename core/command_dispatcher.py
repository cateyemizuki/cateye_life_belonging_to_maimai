"""双根命令分发器（2026-11-13 命令体系重构批）。

职责边界
========
- **只做路由**：解析 ``<根> <组> <动作> <参数>`` → 组/动作双语归一查
  :mod:`core.command_registry` → 统一权限闸（``public`` / ``admin``）→ 调插件实例上的
  注册 handler。业务逻辑不在此模块（handler 由各组件提供）。
- **纯基础设施**：只依赖 stdlib + :mod:`core.command_registry`；不 import 业务包、
  不 import ``plugin``/SDK、不持状态（core 单向纪律）。
- **admin 判定外置**：调用方传入 ``is_admin(user_id, is_local_operator)``，
  口径仍是既有 ``[admin].admin_users`` / 本机控制台（由宿主组件实现）。

解析容错
========
- ``/lbm`` 用 EN、``/麦麦生活`` 用 CN 是规范写法；**混用也接受**（组/动作双语都映射到
  同一 Command），例如 ``/lbm 状态 面板``。
- 参数里的中文关键字整词归一（``确认``→``confirm``、``清空``→``clear`` 等），
  自由文本不受影响。
- 根命令无参 / ``help`` / ``帮助`` → 帮助总览；``help <组>`` → 组帮助。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from core import command_registry as registry
from core.command_registry import Command, Group


def root_pattern(lang: str) -> str:
    """根命令正则：负向前瞻避开行中片段，``(?=\\s|$)`` 防 ``/lbm_life`` 旧前缀误命中。"""
    literal = re.escape(registry.root(lang))
    return rf"(?<!\S){literal}(?=\s|$)(?:\s+(?P<root_args>.*?))?\s*$"


#: 注册用正则（EN/CN 各一条；供 ``@Command(pattern=...)``）。
ROOT_PATTERNS: Dict[str, str] = {lang: root_pattern(lang) for lang in registry.LANGS}


def extract_root_args(lang: str, kwargs: Dict[str, Any]) -> str:
    """从 SDK kwargs 取根命令参数（优先命名捕获；回退全文正则，兼容引用前缀）。"""
    groups = kwargs.get("matched_groups")
    if isinstance(groups, dict) and "root_args" in groups:
        return str(groups.get("root_args") or "")
    text = str(kwargs.get("text") or "")
    if not text:
        return ""
    match = re.search(ROOT_PATTERNS[lang if lang in registry.ROOTS else "en"], text)
    return str(match.group("root_args") or "") if match else ""


#: 解析结果类型。
KIND_HELP = "help"
KIND_GROUP_HELP = "group_help"
KIND_COMMAND = "command"
KIND_ERROR = "error"


@dataclass(frozen=True)
class ParsedCommand:
    """一条已归一的双语命令请求（不持任何运行态）。"""

    lang: str
    root: str
    kind: str
    group: Optional[Group] = None
    command: Optional[Command] = None
    args: Tuple[str, ...] = ()
    text: str = ""          # 已渲染的错误/帮助文本（仅 kind=error）
    raw: Tuple[str, ...] = ()


@dataclass
class DispatchContext:
    """分发所需的运行期回调（由宿主组件注入；core 不感知 SDK）。"""

    stream_id: str = ""
    user_id: str = ""
    is_local_operator: bool = False
    is_admin: Optional[Callable[[str, bool], bool]] = None
    resolve: Optional[Callable[[str], Any]] = None
    send_text: Optional[Callable[[str], Awaitable[Any]]] = None
    send_overview: Optional[Callable[[str], Awaitable[Any]]] = None
    logger: Any = None


class CommandDispatcher:
    """无状态：双根解析 + 双语归一 + 权限闸 + handler 调用（数据全取注册表）。"""

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def parse(self, lang: str, args_text: str = "") -> ParsedCommand:
        """把根命令后的参数串解析成 :class:`ParsedCommand`。"""
        lang = lang if lang in registry.LANGS else "en"
        root = registry.root(lang)
        tokens = [tok for tok in str(args_text or "").strip().split() if tok]
        raw = tuple(tokens)

        if not tokens:
            return ParsedCommand(lang=lang, root=root, kind=KIND_HELP, raw=raw)

        first = tokens[0]
        if registry.is_help_token(first):
            if len(tokens) == 1:
                return ParsedCommand(lang=lang, root=root, kind=KIND_HELP, raw=raw)
            group = registry.find_group(tokens[1])
            if group is None:
                return ParsedCommand(
                    lang=lang, root=root, kind=KIND_ERROR, raw=raw,
                    text=registry.render_unknown_group(tokens[1], lang))
            return ParsedCommand(lang=lang, root=root, kind=KIND_GROUP_HELP,
                                 group=group, args=tuple(tokens[2:]), raw=raw)

        group = registry.find_group(first)
        if group is None:
            return ParsedCommand(
                lang=lang, root=root, kind=KIND_ERROR, raw=raw,
                text=registry.render_unknown_group(first, lang))

        if len(tokens) == 1:
            return ParsedCommand(lang=lang, root=root, kind=KIND_GROUP_HELP,
                                 group=group, raw=raw)

        action_token = tokens[1]
        if registry.is_help_token(action_token):
            return ParsedCommand(lang=lang, root=root, kind=KIND_GROUP_HELP,
                                 group=group, args=tuple(tokens[2:]), raw=raw)

        command = registry.find_action(group, action_token)
        if command is None:
            hint = (f"未知动作：{action_token}\n" if lang == "cn"
                    else f"Unknown action: {action_token}\n")
            return ParsedCommand(
                lang=lang, root=root, kind=KIND_ERROR, group=group, raw=raw,
                text=hint + "\n" + registry.render_group_help(group, lang))

        args = tuple(registry.normalize_arg(tok) for tok in tokens[2:])
        return ParsedCommand(lang=lang, root=root, kind=KIND_COMMAND,
                             group=group, command=command, args=args, raw=raw)

    # ------------------------------------------------------------------
    # 权限
    # ------------------------------------------------------------------

    def authorized(self, parsed: ParsedCommand, ctx: DispatchContext) -> bool:
        """统一权限闸：public 直通；admin 走宿主 ``is_admin``（缺省拒绝）。"""
        command = parsed.command
        if command is None or command.perm != "admin":
            return True
        checker = ctx.is_admin
        if checker is None:
            return False
        try:
            return bool(checker(ctx.user_id, ctx.is_local_operator))
        except Exception:
            return False

    def denial_text(self, parsed: ParsedCommand) -> str:
        """权限不足提示（按根语言给完整路径）。"""
        command = parsed.command
        assert command is not None
        return f"权限不足：仅管理员可用 {registry.usage(command, parsed.lang)}。"

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    async def execute(self, parsed: ParsedCommand, ctx: DispatchContext) -> Tuple[bool, str, bool]:
        """帮助/错误直接发送；命令经权限闸后调 handler。

        handler 契约：``async def handler(command, args, *, stream_id, user_id,
        is_local_operator, **kwargs) -> (success, response, intercept)``。
        handler 的业务回复由 handler 自行发送（与既有组件口径一致）。
        """
        if parsed.kind == KIND_HELP:
            text = registry.render_overview(parsed.lang)
            if ctx.send_overview is not None:
                await ctx.send_overview(parsed.lang)
            elif ctx.send_text is not None:
                await ctx.send_text(text)
            return True, "帮助已发送", True

        if parsed.kind == KIND_GROUP_HELP:
            assert parsed.group is not None
            text = registry.render_group_help(parsed.group, parsed.lang)
            if ctx.send_text is not None:
                await ctx.send_text(text)
            return True, "组帮助已发送", True

        if parsed.kind == KIND_ERROR:
            if ctx.send_text is not None and parsed.text:
                await ctx.send_text(parsed.text)
            return True, "参数错误", True

        command = parsed.command
        if command is None:  # 防御：解析器不变量
            if ctx.send_text is not None:
                await ctx.send_text(registry.render_overview(parsed.lang))
            return True, "参数错误", True

        if not self.authorized(parsed, ctx):
            if ctx.send_text is not None:
                await ctx.send_text(self.denial_text(parsed))
            return True, "没有权限", True

        handler = ctx.resolve(command.handler) if ctx.resolve is not None else None
        if not callable(handler):
            if ctx.send_text is not None:
                await ctx.send_text(
                    f"命令暂不可用（handler 未装配：{command.handler}）。"
                    if parsed.lang == "cn" else
                    f"Command unavailable (handler not wired: {command.handler}).")
            return False, "handler 未装配", True

        result = await handler(
            command,
            list(parsed.args),
            stream_id=ctx.stream_id,
            user_id=ctx.user_id,
            is_local_operator=bool(ctx.is_local_operator),
            lang=parsed.lang,
        )
        if isinstance(result, tuple) and len(result) == 3:
            return result
        return True, "命令执行完成", True


#: 模块级默认实例（无状态，可复用；测试可自行构造）。
DISPATCHER = CommandDispatcher()


def parse_args(lang: str, args_text: str = "") -> ParsedCommand:
    """便捷函数：默认注册表解析。"""
    return DISPATCHER.parse(lang, args_text)
