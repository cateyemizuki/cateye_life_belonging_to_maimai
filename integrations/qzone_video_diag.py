"""`/lbm_life qzone_video` 空间视频联动运维（PR-A3）：只读诊断 + 经授权的人工清除。

参照 3.7.1 `/lbm_life share`（integrations/video_share_diag.py）的实现模式：
status 纯只读（零 LLM、零发送、零副作用）；clear 仅在管理员显式确认某条 uncertain
回执确未送达后，把它标记 rejected（reason=manual_verified_unsent，保留审计行不删证据），
资产 released，候选可重新评估一次——**不自动恢复发送**（再发仍走完整门控）。
命令层（qzone/components.py 分发表）统一鉴权与发送；本模块只做渲染与账本操作。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

_CST = timezone(timedelta(hours=8))


def _fmt_epoch(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value), _CST).strftime('%m-%d %H:%M')
    except (TypeError, ValueError, OverflowError, OSError):
        return '?'


def _host_now(host: Any) -> float:
    surf = getattr(host, '_surf_now', None)
    try:
        if callable(surf):
            return float(surf().timestamp())
    except Exception:
        pass
    return time.time()


def qzone_video_diag_payload(host: Any) -> dict:
    """PR-D1（H10）：``/lbm_diag qzone_video`` 全链只读诊断投影。

    零 LLM、零发送、零落盘：只读 ledger 快照/资产清单（id/尺寸/TTL，无路径）/
    发布回执（含 uncertain）/事实同步状态，并复用 qzone DecisionHistory 聚合
    「门控拒绝原因」（为什么发/不发）。未装配/读取失败如实降级，不编造数据。
    """
    service = getattr(host, '_qzone_video', None)
    if service is None:
        return {'available': False, 'reason': 'not_assembled'}
    try:
        ledger = service.ledger
        snap = ledger.snapshot()
        recent = []
        for cand in (snap.get('candidates') or [])[:8]:
            decision = cand.get('decision') or {}
            recent.append({
                'candidate_id': str(cand.get('candidate_id') or ''),
                'bvid': str(cand.get('bvid') or ''),
                'status': str(cand.get('status') or ''),
                'action': str(decision.get('action') or ''),
                'reason': str(decision.get('reason') or ''),
                'evaluations': int(cand.get('evaluations') or 0),
                'fact_sync': str(decision.get('fact_sync') or ''),
            })
        assets = ledger.assets_overview()
        cfg = getattr(getattr(host, 'config', None), 'qzone_video', None)
        payload = {
            'available': True,
            'mode': str(getattr(cfg, 'mode', 'shadow') or 'shadow'),
            'candidate_states': snap.get('candidate_states', {}),
            'recent_decisions': recent,
            'assets': {'asset_states': assets.get('asset_states', {}),
                       'total_bytes': assets.get('total_bytes', 0),
                       'manifest': ledger.asset_manifest(20)},
            'publish_attempts': snap.get('publish_attempts', {}),
            'recent_receipts': ledger.recent_attempts(10),
            'uncertain': [{'attempt_id': str(row.get('attempt_id') or ''),
                           'bvid': str(row.get('bvid') or ''),
                           'title': str(row.get('title') or ''),
                           'created_at': row.get('created_at')}
                          for row in ledger.uncertain_attempts(20)],
            'fact_sync': snap.get('fact_sync', {}),
        }
    except Exception:
        return {'available': False, 'reason': 'projection_unavailable'}
    # 门控拒绝聚合（复用 qzone DecisionHistory；进程内 deque，热重载清零如实为空）
    history = getattr(getattr(host, '_qzone', None), '_decision_history', None)
    if history is not None and hasattr(history, 'rejection_summary'):
        try:
            payload['gate_rejections'] = history.rejection_summary()
            payload['recent_gate_decisions'] = history.entries()[:5]
        except Exception:
            payload['gate_rejections'] = {}
            payload['recent_gate_decisions'] = []
    else:
        payload['gate_rejections'] = {}
        payload['recent_gate_decisions'] = []
    return payload


def render_qzone_video(host: Any, args: str) -> str:
    usage = (
        "用法: /lbm_life qzone_video <status|clear>（管理员）\n"
        "  status                     — 只读：运行模式/候选与尝试状态/uncertain 明细/事实同步\n"
        "  clear <attempt_id> confirm — 核验确未送达后清除该 uncertain 回执（保留审计，不自动重发）"
    )
    service = getattr(host, '_qzone_video', None)
    if service is None:
        return "空间视频联动未装配（[qzone_video] 不可用或初始化失败），无运维数据。"
    ledger = service.ledger
    parts = (args or '').split()
    sub = parts[0].lower() if parts else 'status'
    if sub in ('help', 'usage'):
        return usage
    if sub == 'status':
        snap = ledger.snapshot()
        uncertain = ledger.uncertain_attempts()
        cfg = getattr(host.config, 'qzone_video', None)
        mode = str(getattr(cfg, 'mode', 'shadow') or 'shadow')
        lines = [
            "【空间视频联动运维·只读】",
            f"运行模式: {mode}",
            f"候选状态: {snap['candidate_states']}｜发布尝试: {snap.get('publish_attempts', {})}",
            f"事实同步: {snap.get('fact_sync', {})}（pending 有界重试，failed 留痕不再试）",
            f"uncertain 回执: {len(uncertain)} 条（永不自动补发；需人工核验）",
        ]
        for row in uncertain[:20]:
            key = str(row.get('request_key') or '')
            lines.append(
                f"  {row['attempt_id']}｜{row['bvid']}｜{row.get('title') or '-'}｜"
                f"{_fmt_epoch(row.get('created_at'))}｜key={key[:12] or '-'}")
        if not uncertain:
            lines.append("  （无 uncertain 回执）")
        lines.append("清除请谨慎：仅在实际确认空间未送达后使用 clear，避免重复发布。")
        return '\n'.join(lines)
    if sub == 'clear':
        if len(parts) < 3 or parts[2].lower() != 'confirm':
            return ("清除需显式确认：/lbm_life qzone_video clear <attempt_id> confirm\n"
                    "务必先核实该说说确未送达，否则可能重复发布。")
        attempt_id = parts[1]
        result = ledger.resolve_uncertain_attempt(attempt_id, _host_now(host))
        if result.get('success'):
            return (f"已清除 uncertain 回执 {attempt_id}（{result['bvid']}）：标记确知未发，保留审计；"
                    f"候选状态={result.get('candidate_status') or '-'}（可重新评估一次，不自动恢复发送）。")
        reason = result.get('reason')
        if reason == 'not_found':
            return f"未找到回执 {attempt_id}。"
        if reason == 'not_uncertain':
            return f"回执 {attempt_id} 当前状态为 {result.get('status')}，不是 uncertain，不清除。"
        return f"清除失败：{reason}"
    return f"未知子命令：{sub}\n{usage}"
