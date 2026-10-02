"""QzoneComponentsMixin —— QQ 空间模块的 SDK 组件注册（装饰器方法）。

组件清单：
- Tools ×2：cateye_test_life_post_qzone（发说说，留空自动生成文案）、cateye_test_life_qzone_moments（查最近说说）
- Command ×1：/cateye_test_life（status | qzone [moments/comments/post/reply/like] | diag | help）

约定（与 planning/companion 的 mixin 一致）：
- 方法名 `qzone_` 前缀（私有助手 `_qz_` 前缀），组件名 `cateye_test_life_*` / `cateye_test_life_suite`；
- 方法开头判空 ``getattr(self, "_qzone", None)``（服务束未挂上时降级）；
- 状态聚合（/cateye_test_life status）同时展示 companion / planning / qzone 三个子模块。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar, Dict

from maibot_sdk import Command, Tool


class QzoneComponentsMixin:
    """QQ 空间组件（2 Tool + 1 Command）。"""

    qzone_components_info: ClassVar[Dict[str, str]] = {
        "cateye_test_life_post_qzone": "Tool：发说说（留空自动按当前活动生成文案）",
        "cateye_test_life_qzone_moments": "Tool：查最近说说（内容摘要+评论数）",
        "cateye_test_life_suite": "Command：/cateye_test_life status|qzone|diag|help",
    }

    # ============================================================
    # Tools
    # ============================================================
    @Tool("cateye_test_life_post_qzone",
          description="以 bot 的口吻发一条 QQ 空间说说。message 留空则自动按当前活动生成文案。受每日上限限制。",
          parameters={"message": {"type": "string", "description": "说说正文；留空自动生成", "required": False}})
    async def qzone_tool_post_moment(self, message: str = "", **kwargs: Any) -> dict[str, Any]:
        qz = getattr(self, "_qzone", None)
        if qz is None:
            return {"success": False, "content": "QQ 空间模块未初始化"}
        cfg = self.config.qzone
        if not cfg.enabled:
            return {"success": False, "content": "QQ 空间模块未启用"}
        if self._qz_posts_left_today() <= 0:
            return {"success": False, "content": "今日说说额度已用完"}
        text = message.strip()
        if not text:
            now = datetime.now()
            text = await qz.generate_moment_text(now.strftime("%Y-%m-%d"), now)
            if not text:
                return {"success": False, "content": "文案生成失败，稍后再试"}
        result = await qz.post_moment(text)
        if result:
            return {"success": True, "content": f"已发说说：{text}", "stop_after_execution": True}
        return {"success": False, "content": "发表失败（检查 SnowLuma 适配器与 QZone 能力）"}

    @Tool("cateye_test_life_qzone_moments",
          description="查看 bot 最近发表的 QQ 空间说说（内容摘要、评论数、发表时间）。用户问空间/说说/动态相关话题时调用。")
    async def qzone_tool_moments(self, **kwargs: Any) -> str:
        qz = getattr(self, "_qzone", None)
        if qz is None or not self.config.qzone.enabled:
            return "QQ 空间模块未启用"
        moments = await qz.list_my_moments(num=5)
        if not moments:
            return "最近没有说说，或空间接口不可用。"
        lines = []
        for idx, m in enumerate(moments, 1):
            excerpt = str(m.get("content") or "")[:40]
            lines.append(f"{idx}. [{m.get('time', 0)}] {excerpt}…（评论 {m.get('comment_num', 0)} 条，tid={m.get('tid', '')}）")
        return "最近说说：\n" + "\n".join(lines)

    def _qz_posts_left_today(self) -> int:
        qz = getattr(self, "_qzone", None)
        if qz is None:
            return 0
        return max(0, self.config.qzone.daily_max_posts - qz.posts_today())

    # ============================================================
    # Command：/cateye_test_life
    # ============================================================
    @Command("cateye_test_life_suite",
             description="猫眼生活套件管理命令（/cateye_test_life status|qzone|diag|help）",
             pattern=r"(?P<life_cmd>(?<!\S)/cateye_test_life(?:\s+(?P<life_args>.*))?)\s*$")
    async def qzone_cmd_life(self, text: str = "", stream_id: str = "", user_id: str = "",
                             is_local_operator: bool = False, **kwargs: Any) -> tuple[bool, str, bool]:
        args = str((kwargs.get("matched_groups") or {}).get("life_args") or "").strip()
        sub, *rest = args.split(maxsplit=1) if args else ("help", "")
        admin = self._qz_is_admin(str(user_id), bool(is_local_operator))
        if sub in ("help", ""):
            reply = self._qz_help_text()
        elif sub == "status":
            reply = self._qz_status_text()
        elif sub == "qzone":
            reply = await self._qz_handle_qzone(rest[0].strip() if rest else "", admin)
        elif sub == "diag":
            if not admin:
                reply = "权限不足：仅管理员可用 /cateye_test_life diag。"
            else:
                try:
                    days = max(1, min(7, int((rest[0].strip() or "1") if rest else 1)))
                except ValueError:
                    days = 1
                reply = self._qz_diag_text(days)
        else:
            reply = f"未知子命令：{sub}\n{self._qz_help_text()}"
        await self.ctx.send.text(reply, stream_id)  # 命令回复必须显式发送
        return True, "life done", True

    # ------------------------------------------------------------
    # 私有助手
    # ------------------------------------------------------------
    def _qz_is_admin(self, user_id: str, is_local_operator: bool) -> bool:
        if is_local_operator:
            return True
        admins = {str(a).rsplit(":", 1)[-1] for a in self.config.admin.admin_users}
        # 默认拒绝：未配置管理员时 QQ 侧一律拒绝（本机控制台仍可用）
        return bool(admins) and str(user_id).rsplit(":", 1)[-1] in admins

    def _qz_help_text(self) -> str:
        return (
            "猫眼生活套件命令：\n"
            "/cateye_test_life status - 生活状态总览（好感度/巡检/日程/空间）\n"
            "/cateye_test_life qzone - QQ空间状态｜moments 最近说说｜comments 说说评论列表\n"
            "           （管理员：post 文本、reply 说说序号 [评论序号] 内容、like tid [目标QQ]）\n"
            "/cateye_test_life diag [天数] - 决策日志（管理员）\n"
            "/cateye_test_life help - 本帮助；日程与恋人功能另有 /cateye_test_plan 与 /cateye_test_mai_* 命令"
        )

    def _qz_status_text(self) -> str:
        companion = getattr(self, "_companion", None)
        planning = getattr(self, "_planning", None)
        qz = getattr(self, "_qzone", None)
        lines = []
        if companion is not None:
            lines.append(companion.affection.status_text())
            patrol = companion.patrol.patrol_status()
            lines.append(
                f"巡检：{'运行中' if companion.patrol.has_target() else '等待目标会话'}｜"
                f"上次触发：{patrol.get('last_trigger_intent') or '无'}"
            )
        if planning is not None:
            try:
                lines.append(planning.status_text())
            except Exception:
                pass
        if qz is not None:
            lines.append(qz.status_text())
        return "\n".join(lines) if lines else "服务尚未初始化。"

    def _qz_diag_text(self, days: int) -> str:
        logger = getattr(self, "_decision_logger", None)
        if logger is None:
            return "决策日志未初始化。"
        entries = logger.read_recent(days=days, limit=30)
        if not entries:
            return f"最近 {days} 天没有决策日志。"
        lines = [f"[{e.get('time', '')}] {e.get('action')} {e.get('trigger_type')} {e.get('reason', '')}"
                 for e in entries[:30]]
        return "决策日志（最近 {n} 条）：\n{detail}".format(n=len(lines), detail="\n".join(lines))

    async def _qz_handle_qzone(self, args: str, admin: bool) -> str:
        qz = getattr(self, "_qzone", None)
        if qz is None:
            return "QQ 空间模块未初始化。"
        action, *rest = args.split(maxsplit=1) if args else ("status", "")
        if action in ("status", ""):
            return qz.status_text()
        if action == "moments":
            moments = await qz.list_my_moments(num=10)
            if not moments:
                return "最近没有说说，或空间接口不可用（查看 /cateye_test_life qzone status 的通道状态）。"
            lines = []
            for idx, m in enumerate(moments, 1):
                excerpt = str(m.get("content") or "")[:40] or "（无文字）"
                lines.append(f"{idx}. {excerpt}…（评论 {m.get('comment_num', 0)} 条）")
            return "最近说说：\n" + "\n".join(lines)
        if action == "comments":
            # Web CGI 读自己说说及其评论（含楼中楼），供 reply 定位
            feeds = await qz.list_own_feeds_with_comments(num=10)
            if not feeds:
                return "读不到说说评论（cookie 未就绪或接口失败，见 /cateye_test_life qzone status）。"
            self_uin = self._qz_cookie_uin()
            lines = []
            for idx, feed in enumerate(feeds, 1):
                excerpt = str(feed.get("content") or "")[:24] or "（无文字）"
                lines.append(f"{idx}. {excerpt}…")
                for c_idx, c in enumerate(feed.get("comments") or [], 1):
                    owner = "（自己）" if str(c.get("qq_account")) == self_uin else ""
                    lines.append(f"   {idx}.{c_idx} {c.get('nickname', '?')}{owner}：{str(c.get('content'))[:36]}")
            return "说说评论（序号用于 /cateye_test_life qzone reply 楼中楼回复）：\n" + "\n".join(lines[:40])
        if not admin:
            return "权限不足：需要管理员权限。"
        if action == "post":
            text = (rest[0].strip() if rest else "")
            if not text:
                return "用法：/cateye_test_life qzone post <说说内容>"
            result = await qz.post_moment(text)
            return f"已发表（tid={result.get('tid', '')}）。" if result else "发表失败（检查 SnowLuma 适配器与 QZone 能力）。"
        if action == "reply":
            # 用法：/life qzone reply <说说序号> [评论序号] <内容>
            #   只给说说序号 = 普通评论；给「说说.评论」序号（或两个数字）= 楼中楼回复该评论
            parts = (rest[0].strip() if rest else "").split(maxsplit=2)
            if len(parts) < 2 or not parts[-1].strip():
                return "用法：/cateye_test_life qzone reply <说说序号> [评论序号] <内容>（序号见 /cateye_test_life qzone comments）"
            nums = parts[0].replace(".", " ").split()
            try:
                feed_idx = int(nums[0]) - 1
                comment_idx = int(nums[1]) - 1 if len(nums) > 1 else None
            except (ValueError, IndexError):
                return "序号格式：说说序号 或 说说序号.评论序号（如 2.1）"
            content = parts[-1].strip()
            feeds = await qz.list_own_feeds_with_comments(num=10)
            if not (0 <= feed_idx < len(feeds)):
                return "说说序号超出范围，先 /cateye_test_life qzone comments 查看。"
            feed = feeds[feed_idx]
            fid = str(feed.get("tid") or "")
            if comment_idx is None:
                comment_id = await qz.comment_moment(fid, content)
                return "已评论。" if comment_id is not None else "评论失败。"
            comments = feed.get("comments") or []
            if not (0 <= comment_idx < len(comments)):
                return "评论序号超出范围，先 /cateye_test_life qzone comments 查看。"
            c = comments[comment_idx]
            if str(c.get("qq_account")) == self._qz_cookie_uin():
                return "这是自己发的评论，不需要回复。"
            ok = await qz.reply_to_comment(fid, str(c.get("qq_account")),
                                           str(c.get("nickname")), c.get("comment_tid"), content)
            return f"已楼中楼回复 {c.get('nickname')}。" if ok else "楼中楼回复失败（需要 cookie 就绪）。"
        if action == "like":
            # 用法：/life qzone like <tid> [目标QQ]（自己的说说省略目标QQ；好友说说传对方QQ号）
            parts = (rest[0].strip() if rest else "").split()
            if not parts:
                return "用法：/cateye_test_life qzone like <说说tid> [目标QQ]"
            target_qq = parts[1] if len(parts) > 1 else ""
            ok = await qz.like_moment(parts[0], target_uin=target_qq)
            return "已点赞。" if ok else "点赞失败。"
        return "未知空间子命令，用 /cateye_test_life help 查看。"

    def _qz_cookie_uin(self) -> str:
        """当前空间 cookie 对应的 bot QQ 号（用于标注自己的评论；取不到返回空串）。"""
        qz = getattr(self, "_qzone", None)
        mgr = getattr(qz, "_cookie_manager", None)
        cookies = getattr(mgr, "_cached", None)
        if not cookies:
            return ""
        return str(cookies).get("uin", "").lstrip("o0") if isinstance(cookies, dict) else ""
