"""双语命令注册表（2026-11-13 命令体系重构批，单一真源）。

设计纪律
========
- **纯数据 + 纯渲染**：本模块只依赖 stdlib；不 import 业务包、不 import ``plugin``、
  不碰 SDK、不持状态（``scratch/test_backend_boundaries.py`` 的 core 单向纪律）。
- **双语同源**：每个组/动作/帮助/参数都带 EN/CN 两份；``/lbm`` 走 EN 渲染，
  ``/麦麦生活`` 走 CN 渲染，分发器对组/动作做双语归一（混用也容错，如
  ``/lbm 状态 面板``）。
- **旧命令已移除（用户 2026-11-13 裁决：无别名）**：注册表不登记任何
  ``/lbm_life`` / ``/lbm_plan`` / ``/lbm_diag`` / ``/lbm_dashboard`` /
  ``/lbm_mai_*`` / ``/lbm_emotion_config`` / ``/lbm_init`` 入口；旧命令仅在本文件
  顶部的迁移注释中留档。
- **handler 只引用不实现**：``handler`` 是插件实例上的方法名；业务逻辑仍在
  ``companion`` / ``planning`` / ``qzone`` / ``emotion`` 各组件内（注册表零业务）。

迁移对照（旧 → 新，旧入口已完全删除）
====================================
``/lbm_dashboard`` → status/dashboard（状态/面板）
``/lbm_life status`` → status/life（状态/生活）
``/lbm_mai_affection`` → status/affection（状态/好感）
``/lbm_plan list|regenerate|events|delete|clear|sleep|watch`` →
plan/list|regenerate|events|delete|clear|sleep|watch（日程/…）
``/lbm_life qzone ...`` → qzone/...（空间/…）
``/lbm_life qzone_video`` → qzone/video（空间/视频）
``/lbm_emotion_config`` → emotion/config（情绪/配置）
``/lbm_diag ...`` → diag/...（诊断/…）
``/lbm_life rolling`` → diag/rolling（诊断/滚动）
``/lbm_init`` → ops/init（运维/初始化）
``/lbm_life share`` → ops/share（运维/分享）
``/lbm_mai_help`` + ``/lbm_life help`` → 根命令无参 / help（帮助）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

#: 双根：EN 根 + 全中文根（分支也中文）。
ROOTS: Dict[str, str] = {"en": "/lbm", "cn": "/麦麦生活"}
ROOT_TO_LANG: Dict[str, str] = {root: lang for lang, root in ROOTS.items()}
LANGS: Tuple[str, ...] = ("en", "cn")

#: 权限两级；admin 判定由宿主侧 ``admin_users`` / 本机控制台口径执行。
PERMS: Tuple[str, ...] = ("public", "admin")
PERM_LABELS: Dict[str, Tuple[str, str]] = {
    "public": ("public", "公开"),
    "admin": ("admin", "管理员"),
}

#: 帮助令牌（根命令无参 / ``help`` / ``帮助`` 等价）。
HELP_TOKENS: Tuple[str, ...] = ("help", "帮助", "?", "？")


@dataclass(frozen=True)
class Group:
    """命令组（第二分支）：``en``/``cn`` 是同一组的两种写法。"""

    en: str
    cn: str
    summary_en: str
    summary_cn: str


@dataclass(frozen=True)
class Command:
    """一条叶子命令（第三分支）。

    Attributes:
        group_en / group_cn: 所属组双语名（必须命中 :data:`GROUPS`）。
        action_en / action_cn: 动作双语名（组内唯一）。
        perm: ``public`` 或 ``admin``（分发器统一闸）。
        help_en / help_cn: 一句话说明（组帮助/图片版/文档共用）。
        handler: 插件实例方法名（业务组件提供；注册表只引用）。
        args_en / args_cn: 参数占位（可为空）。
        action_key: 传给 handler 的规范动作键；缺省 = ``action_en``。
    """

    group_en: str
    group_cn: str
    action_en: str
    action_cn: str
    perm: str
    help_en: str
    help_cn: str
    handler: str
    args_en: str = ""
    args_cn: str = ""
    action_key: str = ""

    @property
    def key(self) -> str:
        """handler 侧规范动作键（``schedule`` → ``list`` 这类复用在此登记）。"""
        return self.action_key or self.action_en


# ---------------------------------------------------------------------------
# 组（6 组；help 是根级动作，不占组序号）
# ---------------------------------------------------------------------------

GROUPS: Tuple[Group, ...] = (
    Group("status", "状态",
          "State overview: dashboard / life / affection / schedule",
          "状态总览：面板 / 生活 / 好感 / 行程"),
    Group("plan", "日程",
          "Daily schedule: list / regenerate / events / delete / clear / sleep / watch",
          "日程管理：列表 / 重新生成 / 事件 / 删除 / 清空 / 睡眠 / 刷视频"),
    Group("qzone", "空间",
          "QQ Zone: status / moments / comments / post / reply / like / decisions / video",
          "QQ 空间：状态 / 说说 / 评论 / 发布 / 回复 / 点赞 / 决策 / 视频"),
    Group("emotion", "情绪",
          "Emotion parameters and log",
          "情绪参数与日志"),
    Group("diag", "诊断",
          "Diagnostics: llm / decision / life / streams / video_share / qzone_video / rolling",
          "排障诊断：模型 / 决策 / 生活 / 会话 / 视频分享 / 空间视频 / 滚动"),
    Group("ops", "运维",
          "Operations: init / share",
          "运维：初始化 / 分享"),
)

HELP_GROUP = Group("help", "帮助", "Command help", "命令帮助")

# ---------------------------------------------------------------------------
# 叶子命令（29 条 + help）
# ---------------------------------------------------------------------------

COMMANDS: Tuple[Command, ...] = (
    # ── status / 状态 ────────────────────────────────────────────────
    Command("status", "状态", "dashboard", "面板", "admin",
            "Aggregated state dashboard (schedule/relationship/emotion/qzone), image + text",
            "聚合状态总览（日程/关系/情绪/空间），图+文",
            "cmd_status_dashboard"),
    Command("status", "状态", "life", "生活", "public",
            "Life status overview (affection/patrol/schedule/qzone)",
            "生活状态总览（好感/巡检/日程/空间）",
            "cmd_status_life"),
    Command("status", "状态", "affection", "好感", "public",
            "View/adjust affection tier (bound companion target only)",
            "查看/调整好感档位（仅陪伴目标）",
            "cmd_status_affection",
            args_en="[level 1-N]", args_cn="[档位 1-N]"),
    Command("status", "状态", "schedule", "行程", "admin",
            "Today's schedule (image + detailed text); same as plan list",
            "今日日程（图+详细文字）；等同 日程/列表",
            "cmd_plan_entry", action_key="list"),

    # ── plan / 日程 ──────────────────────────────────────────────────
    Command("plan", "日程", "list", "列表", "admin",
            "Today's schedule; generates automatically when empty",
            "今日日程；无日程时自动生成",
            "cmd_plan_entry"),
    Command("plan", "日程", "regenerate", "重新生成", "admin",
            "Regenerate today's schedule (optional extra request)",
            "重新生成今日日程（可附加要求）",
            "cmd_plan_entry",
            args_en="[extra request]", args_cn="[额外要求]"),
    Command("plan", "日程", "events", "事件", "admin",
            "Regenerate the small-event library by three time bands",
            "按三时段重新生成小事件库",
            "cmd_plan_entry", args_en="[force]", args_cn="[force]"),
    Command("plan", "日程", "delete", "删除", "admin",
            "Delete a schedule goal by id or index",
            "按 ID 或序号删除日程目标",
            "cmd_plan_entry", args_en="<goal_id|index>", args_cn="<目标ID|序号>"),
    Command("plan", "日程", "clear", "清空", "admin",
            "Clear old schedules (default keeps today)",
            "清理旧日程（默认保留今天）",
            "cmd_plan_entry", args_en="[days]", args_cn="[天数]"),
    Command("plan", "日程", "sleep", "睡眠", "admin",
            "Enter sleep now within the allowed window",
            "在允许窗口内立即入睡",
            "cmd_plan_entry"),
    Command("plan", "日程", "watch", "刷视频", "admin",
            "Trigger one full video-watch cycle in background",
            "后台立即触发一次完整刷视频周期",
            "cmd_plan_entry"),

    # ── qzone / 空间 ─────────────────────────────────────────────────
    Command("qzone", "空间", "status", "状态", "public",
            "QQ Zone channel status",
            "QQ 空间通道状态",
            "cmd_qzone_entry"),
    Command("qzone", "空间", "moments", "说说", "public",
            "List recent moments",
            "最近说说列表",
            "cmd_qzone_entry"),
    Command("qzone", "空间", "comments", "评论", "public",
            "List own moments with comments (reply indexes)",
            "列出自己说说及评论（供回复序号）",
            "cmd_qzone_entry"),
    Command("qzone", "空间", "post", "发布", "admin",
            "Publish a moment; empty text auto-generates",
            "发布说说；留空自动生成文案",
            "cmd_qzone_entry", args_en="<text>", args_cn="<正文>"),
    Command("qzone", "空间", "reply", "回复", "admin",
            "Comment or reply to a comment",
            "评论或楼中楼回复",
            "cmd_qzone_entry",
            args_en="<feed> [comment] <text>", args_cn="<说说> [评论] <内容>"),
    Command("qzone", "空间", "like", "点赞", "admin",
            "Like a moment",
            "给说说点赞",
            "cmd_qzone_entry", args_en="<tid> [qq]", args_cn="<tid> [QQ]"),
    Command("qzone", "空间", "decisions", "决策", "admin",
            "Moment posting decision history",
            "发帖判定历史",
            "cmd_qzone_entry", args_en="[count]", args_cn="[条数]"),
    Command("qzone", "空间", "video", "视频", "admin",
            "QZone video ops: status / clear <id> confirm",
            "空间视频运维：状态 / 清空 <id> 确认",
            "cmd_qzone_entry",
            args_en="[status|clear <id> confirm]",
            args_cn="[状态|清空 <id> 确认]"),

    # ── emotion / 情绪 ───────────────────────────────────────────────
    Command("emotion", "情绪", "config", "配置", "admin",
            "View emotion params; reset|clear",
            "查看情绪参数；重置/清空",
            "cmd_emotion_config",
            args_en="[reset|clear]", args_cn="[重置|清空]"),

    # ── diag / 诊断 ──────────────────────────────────────────────────
    Command("diag", "诊断", "llm", "模型", "admin",
            "LLM call log (merged forward)",
            "LLM 调用日志（合并转发）",
            "cmd_diag_entry", args_en="[days 1-30]", args_cn="[天数 1-30]"),
    Command("diag", "诊断", "decision", "决策", "admin",
            "Proactive decision log",
            "主动行为决策日志",
            "cmd_diag_entry", args_en="[days 1-30]", args_cn="[天数 1-30]"),
    Command("diag", "诊断", "life", "生活", "admin",
            "Decision log (compact, 1-7 days)",
            "决策日志（紧凑文本，1-7 天）",
            "cmd_diag_entry", args_en="[days 1-7]", args_cn="[天数 1-7]"),
    Command("diag", "诊断", "streams", "会话", "admin",
            "Proactive candidate streams and roster status",
            "主动候选流与名单状态",
            "cmd_diag_entry", args_en="[index]", args_cn="[序号]"),
    Command("diag", "诊断", "video_share", "视频分享", "admin",
            "Video social-share diagnostics",
            "视频独立分享诊断",
            "cmd_diag_entry"),
    Command("diag", "诊断", "qzone_video", "空间视频", "admin",
            "QZone video full-chain diagnostics",
            "空间视频全链只读诊断",
            "cmd_diag_entry", args_en="[days]", args_cn="[天数]"),
    Command("diag", "诊断", "rolling", "滚动", "admin",
            "Rolling schedule debug (read-only)",
            "滚动日程调试（只读）",
            "cmd_diag_entry",
            args_en="[status|state|episodes|spine|sleep|preview]",
            args_cn="[状态|状态机|片段|脊柱|睡眠|预览]"),

    # ── ops / 运维 ───────────────────────────────────────────────────
    Command("ops", "运维", "init", "初始化", "admin",
            "Run initialization: field / all / status",
            "初始化：字段 / 全部 / 状态",
            "cmd_ops_init",
            args_en="[emotion|events|state_sleep|frequency|video_interest|"
                    "companion_disposition|activity_preferences|all|status]",
            args_cn="[情绪|事件|生活状态|频率|视频兴趣|联系倾向|日程偏好|全部|状态]"),
    Command("ops", "运维", "share", "分享", "admin",
            "Video-share ops: status / clear <id> confirm",
            "视频分享运维：状态 / 清空 <id> 确认",
            "cmd_ops_share",
            args_en="[status|clear <id> confirm]",
            args_cn="[状态|清空 <id> 确认]"),
)

HELP_COMMAND = Command(
    "help", "帮助", "help", "帮助", "public",
    "Show command help; optional group filter",
    "显示命令帮助；可带组名只看该组",
    "cmd_help",
    args_en="[group]", args_cn="[组]",
)

ALL_COMMANDS: Tuple[Command, ...] = COMMANDS + (HELP_COMMAND,)

# 参数关键字双语归一（只做整词匹配，避免改到自由文本）。
ARG_ALIASES: Dict[str, str] = {
    "确认": "confirm",
    "状态": "status",
    "清空": "clear",
    "重置": "reset",
    "情绪": "emotion",
    "事件": "events",
    "生活状态": "state_sleep",
    "频率": "frequency",
    "视频兴趣": "video_interest",
    "联系倾向": "companion_disposition",
    "日程偏好": "activity_preferences",
    "全部": "all",
    "所有": "all",
    "状态机": "state",
    "片段": "episodes",
    "脊柱": "spine",
    "睡眠": "sleep",
    "预览": "preview",
}

_GROUP_BY_TOKEN: Dict[str, Group] = {}
_ACTION_BY_TOKEN: Dict[Tuple[str, str], Command] = {}


def _norm(token: object) -> str:
    """组/动作/参数整词归一：ASCII 大小写不敏感，中文原样。"""
    return str(token or "").strip().casefold()


for _group in GROUPS + (HELP_GROUP,):
    _GROUP_BY_TOKEN[_group.en] = _group
    _GROUP_BY_TOKEN[_group.cn] = _group
for _cmd in ALL_COMMANDS:
    if _cmd is HELP_COMMAND:
        continue
    _ACTION_BY_TOKEN[(_cmd.group_en, _cmd.action_en)] = _cmd
    _ACTION_BY_TOKEN[(_cmd.group_en, _cmd.action_cn)] = _cmd


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def root(lang: str) -> str:
    """根命令字面量（未知语言退回 EN）。"""
    return ROOTS.get(lang, ROOTS["en"])


def lang_of_root(token: str) -> Optional[str]:
    """根命令字面量 → 语言键；未知返回 None。"""
    return ROOT_TO_LANG.get(str(token or "").strip())


def find_group(token: object) -> Optional[Group]:
    """组令牌双语归一（EN/CN 都命中同一 Group）。"""
    return _GROUP_BY_TOKEN.get(_norm(token))


def find_action(group: Group, token: object) -> Optional[Command]:
    """组内动作令牌双语归一。"""
    return _ACTION_BY_TOKEN.get((group.en, _norm(token)))


def is_help_token(token: object) -> bool:
    return _norm(token) in {_norm(t) for t in HELP_TOKENS}


def normalize_arg(token: object) -> str:
    """命令参数整词归一（中文关键字 → EN 规范词）。"""
    raw = str(token or "")
    return ARG_ALIASES.get(raw, raw)


def group_commands(group: Group) -> Tuple[Command, ...]:
    """某组的全部叶子命令（保持注册顺序）。"""
    return tuple(cmd for cmd in COMMANDS if cmd.group_en == group.en)


def group_of(command: Command) -> Group:
    """命令所属组（注册表自校验保证存在）。"""
    for group in GROUPS:
        if group.en == command.group_en:
            return group
    raise KeyError(command.group_en)


def find_command(group_en: str, action: str) -> Optional[Command]:
    """按规范 EN 组/动作查命令（测试/文档用）。"""
    for cmd in COMMANDS:
        if cmd.group_en == group_en and cmd.action_en == action:
            return cmd
    return None


def usage(command: Command, lang: str = "en") -> str:
    """单条命令的完整用法（含根/组/动作/参数占位）。"""
    if lang == "cn":
        text = f"{ROOTS['cn']} {command.group_cn} {command.action_cn}"
        return f"{text} {command.args_cn}".strip()
    text = f"{ROOTS['en']} {command.group_en} {command.action_en}"
    return f"{text} {command.args_en}".strip()


def perm_label(perm: str, lang: str = "en") -> str:
    pair = PERM_LABELS.get(perm, PERM_LABELS["public"])
    return pair[1] if lang == "cn" else pair[0]


# ---------------------------------------------------------------------------
# 帮助渲染（QQ 文本 / 合并转发节点 / 图片版 / 文档共用）
# ---------------------------------------------------------------------------


def render_overview(lang: str = "en") -> str:
    """根命令无参帮助总览：根用法 + 6 组一句话 + help 入口。"""
    if lang == "cn":
        lines = [
            "麦麦生活 · 命令帮助",
            "根命令：/麦麦生活 <组> <动作> [参数]（英文：/lbm <group> <action> [args]）",
            "",
            "命令组（6）：",
        ]
        for group in GROUPS:
            lines.append(f"  {group.cn}  {group.summary_cn}")
        lines += [
            "  帮助（help）  命令帮助：/麦麦生活 帮助 [组]",
            "",
            "直接发送 /麦麦生活 显示本帮助；发送 /麦麦生活 帮助 <组> 查看组内用法。",
        ]
        return "\n".join(lines)

    lines = [
        "MAI LIFE · COMMAND HELP",
        "Root: /lbm <group> <action> [args]（Chinese: /麦麦生活 <组> <动作> [参数]）",
        "",
        "Groups (6):",
    ]
    for group in GROUPS:
        lines.append(f"  {group.en} / {group.cn}  {group.summary_en}")
    lines += [
        "  help / 帮助     Command help: /lbm help [group]",
        "",
        "Send /lbm alone for this help; send /lbm help <group> for that group.",
    ]
    return "\n".join(lines)


def render_group_help(group: Group, lang: str = "en") -> str:
    """组帮助：组内逐条用法 + 权限 + 一句话说明。"""
    commands = group_commands(group)
    if lang == "cn":
        lines = [f"【{group.cn}】{group.summary_cn}",
                 f"共 {len(commands)} 个子命令（权限：public=公开，admin=管理员）", ""]
        for cmd in commands:
            args = f" {cmd.args_cn}" if cmd.args_cn else ""
            lines.append(f"  {ROOTS['cn']} {group.cn} {cmd.action_cn}{args}"
                         f"  [{perm_label(cmd.perm, 'cn')}]")
            lines.append(f"    {cmd.help_cn}")
        lines += ["", f"总览：{ROOTS['cn']}；本组：{ROOTS['cn']} 帮助 {group.cn}"]
        return "\n".join(lines)

    lines = [f"[{group.en} / {group.cn}] {group.summary_en}",
             f"{len(commands)} commands (perm: public | admin)", ""]
    for cmd in commands:
        args = f" {cmd.args_en}" if cmd.args_en else ""
        lines.append(f"  {ROOTS['en']} {group.en} {cmd.action_en}{args}"
                     f"  [{perm_label(cmd.perm, 'en')}]")
        lines.append(f"    {cmd.help_en}")
    lines += ["", f"Overview: {ROOTS['en']}; this group: {ROOTS['en']} help {group.en}"]
    return "\n".join(lines)


def help_forward_nodes(lang: str = "en") -> List[Tuple[str, str]]:
    """合并转发节点：``[总览] + [6 组]``（昵称, 文本）。

    用户裁定：第一节点 = help 总览 + 分支概览；每个第二分支（组）各占一个独立节点。
    节点数固定 7，远低于 QQ 合并转发 99 条上限。
    """
    if lang == "cn":
        nodes: List[Tuple[str, str]] = [("命令总览", render_overview("cn"))]
        for group in GROUPS:
            nodes.append((group.cn, render_group_help(group, "cn")))
        return nodes
    nodes = [("Command help · 总览", render_overview("en"))]
    for group in GROUPS:
        nodes.append((f"{group.en.upper()} · {group.cn}", render_group_help(group, "en")))
    return nodes


def render_unknown_group(token: str, lang: str = "en") -> str:
    """未知组提示（总览 + 原 token），由分发器直接发出。"""
    head = (f"未知命令组：{token}\n" if lang == "cn" else f"Unknown command group: {token}\n")
    return head + "\n" + render_overview(lang)


def render_group_usage_hint(group: Group, lang: str = "en") -> str:
    """组内动作缺失/未知时：组帮助 + 根用法提示。"""
    return render_group_help(group, lang)


# ---------------------------------------------------------------------------
# 文档生成（ASCII 命令树 / Mermaid）
# ---------------------------------------------------------------------------


def ascii_tree(lang: str = "en") -> str:
    """单语 ASCII 命令树（根 → 6 组 → 叶子，含权限）。"""
    root_cmd = root(lang)
    lines = [root_cmd]
    for gi, group in enumerate(GROUPS):
        # help 是根级动作（最后一行），6 组一律用 ├─ 保持树干连续
        g_branch = "├─"
        g_name = group.cn if lang == "cn" else group.en
        g_alt = group.en if lang == "cn" else group.cn
        lines.append(f"{g_branch} {g_name} ({g_alt})")
        actions = group_commands(group)
        for ci, cmd in enumerate(actions):
            last_action = ci == len(actions) - 1
            a_branch = "   └─" if last_action else "   ├─"
            a_name = cmd.action_cn if lang == "cn" else cmd.action_en
            lines.append(f"{a_branch} {a_name}  [{perm_label(cmd.perm, lang)}]")
    help_name = "帮助" if lang == "cn" else "help"
    lines.append(f"└─ {help_name}  [{perm_label('public', lang)}]")
    return "\n".join(lines)


def bilingual_tree() -> str:
    """双语 ASCII 命令树（EN 段 + CN 段）。"""
    return "```text\n# EN\n" + ascii_tree("en") + "\n\n# CN\n" + ascii_tree("cn") + "\n```"


def _mermaid_id(value: str) -> str:
    cleaned = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in value)
    return cleaned or "node"


def mermaid() -> str:
    """Mermaid ``graph TD``：根/组/叶子三级，节点双语标注 + 权限。"""
    lines = ["```mermaid", "graph TD", '  ROOT["/lbm · /麦麦生活"]',
             '  HELP["help · 帮助 (public)"]', "  ROOT --> HELP"]
    for group in GROUPS:
        gid = "G_" + _mermaid_id(group.en)
        lines.append(f'  ROOT --> {gid}["{group.en} · {group.cn}"]')
        for cmd in group_commands(group):
            cid = "C_" + _mermaid_id(group.en) + "_" + _mermaid_id(cmd.action_en)
            label = f"{cmd.action_en} · {cmd.action_cn} ({cmd.perm})"
            lines.append(f'  {gid} --> {cid}["{label}"]')
    lines.append("```")
    return "\n".join(lines)


def docs_block() -> str:
    """生成文档块（ASCII 树 + Mermaid，双语）。``tools/gen_command_docs.py`` 使用。"""
    return "\n\n".join([
        "### 命令树（ASCII · 双语）",
        bilingual_tree(),
        "### 命令流程图（Mermaid）",
        mermaid(),
    ])


# 注册表自校验（导入即失败，防止手写漂移）。
def _self_check() -> None:
    assert tuple(g.en for g in GROUPS) == (
        "status", "plan", "qzone", "emotion", "diag", "ops"), [g.en for g in GROUPS]
    seen_groups = set()
    for group in GROUPS + (HELP_GROUP,):
        for token in (group.en, group.cn):
            assert token and _GROUP_BY_TOKEN.get(_norm(token)) is group, token
        assert group.en not in seen_groups, group.en
        seen_groups.add(group.en)
    assert len(GROUPS) == 6
    seen_paths = set()
    for cmd in COMMANDS:
        assert cmd.perm in PERMS, cmd
        assert cmd.group_en in {g.en for g in GROUPS}, cmd
        assert cmd.group_cn == group_of(cmd).cn
        assert cmd.help_en and cmd.help_cn, cmd
        assert cmd.handler, cmd
        slot = (cmd.group_en, cmd.action_en)
        assert slot not in seen_paths, slot
        seen_paths.add(slot)
        assert find_action(group_of(cmd), cmd.action_cn) is cmd
        assert find_action(group_of(cmd), cmd.action_en) is cmd
    assert len(COMMANDS) == 29, len(COMMANDS)
    assert HELP_COMMAND.perm == "public"
    for lang, root_token in ROOTS.items():
        assert root_token.startswith("/")
        assert lang in LANGS


_self_check()
