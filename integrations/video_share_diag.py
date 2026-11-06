"""`/lbm_life share` 独立视频分享运维：只读诊断 + 经授权的人工清除。

status 纯只读：零 LLM、零发送、零副作用。clear 仅在管理员显式确认某条 uncertain
回执确未送达后，把它标记为确知未发（保留审计行、不删除证据），并将对应视频置终态
skipped，使自动分享不会自行恢复——只解除人工再次分享的同对阻挡。命令层统一鉴权与发送。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

_CST = timezone(timedelta(hours=8))


def _fmt_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), _CST).strftime('%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return '?'


def render_share(host: Any, args: str) -> str:
    usage = (
        "用法: /lbm_life share <status|clear>（管理员）\n"
        "  status                     — 只读：运行模式/候选状态/uncertain 明细\n"
        "  clear <attempt_id> confirm — 核验确未送达后清除该 uncertain 回执（保留审计）"
    )
    service = getattr(host, '_video_social', None)
    if service is None:
        return "独立视频分享未装配（[video_share] 不可用或初始化失败），无运维数据。"
    ledger = service.ledger
    parts = (args or '').split()
    sub = parts[0].lower() if parts else 'status'
    if sub in ('help', 'usage'):
        return usage
    if sub == 'status':
        snap = ledger.snapshot()
        uncertain = ledger.uncertain_receipts()
        cfg = host.config.video_share
        lines = [
            "【视频自主分享运维·只读】",
            f"运行模式: {cfg.mode}｜陪伴私聊: {cfg.include_companion}｜额外允许: {len(cfg.allowed_chats or [])}",
            f"候选状态: {snap['candidate_states']}｜画像数: {snap['profile_count']}｜旧数据已迁移: {snap['legacy_imported']}",
            f"uncertain 回执: {len(uncertain)} 条（永不自动补发；需人工核验）",
        ]
        for row in uncertain[:20]:
            lines.append(
                f"  {row['id']}｜{row['bvid']}｜{row['kind']}｜流={row['stream']}｜"
                f"{_fmt_epoch(row['created'])}｜{row['reason'] or '-'}")
        if not uncertain:
            lines.append("  （无 uncertain 回执）")
        lines.append("清除请谨慎：仅在实际确认对方未收到后使用 clear，避免重复发送。")
        return '\n'.join(lines)
    if sub == 'clear':
        if len(parts) < 3 or parts[2].lower() != 'confirm':
            return ("清除需显式确认：/lbm_life share clear <attempt_id> confirm\n"
                    "务必先核实该卡片确未送达，否则可能重复发送。")
        attempt_id = parts[1]
        result = ledger.resolve_uncertain(attempt_id)
        if result.get('success'):
            return (f"已清除 uncertain 回执 {attempt_id}（{result['bvid']}）：标记确知未发，保留审计；"
                    "自动分享不会自行恢复，如需可手动再次分享。")
        reason = result.get('reason')
        if reason == 'not_found':
            return f"未找到回执 {attempt_id}。"
        if reason == 'not_uncertain':
            return f"回执 {attempt_id} 当前状态为 {result.get('status')}，不是 uncertain，不清除。"
        return f"清除失败：{reason}"
    return f"未知子命令：{sub}\n{usage}"