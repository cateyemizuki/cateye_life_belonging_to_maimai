"""Small, bounded, read-only dashboard projection. No context prompts or raw logs."""
from __future__ import annotations

import math
import json
from datetime import datetime, time as dt_time, timedelta
from typing import Any

from core.plugin_context import PluginContext

from .time_utils import TimezoneManager, format_day_minute
from .life_history import FIELDS, MAX_POINTS, SAMPLE_SECONDS

def _companion_summary(plugin: Any, *, date: str) -> dict[str, Any]:
    ctx = PluginContext(plugin)
    if ctx.companion is None:
        return {'available': False}
    # D3（2026-11-01 模块解耦计划 §3.1）：经组合根公共口取陪伴摘要，planning 不导入
    # companion；公共口缺失（裸测试桩）→ 旧懒导入 shim 保留，D5 注入化后退役。
    getter = getattr(plugin, 'companion_summary', None)
    if callable(getter):
        return getter(date=date)
    from companion.dashboard import companion_summary
    return companion_summary(plugin, date=date)


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return round(float(value), 6)
    return None


def _row(row: Any) -> dict[str, Any]:
    """Explicit fields keep prompt metadata, user IDs and chat text out of the UI."""
    if not isinstance(row, dict):
        return {}
    result = {key: str(row[key])[:160] for key in
              ('name', 'goal_type', 'status', 'source', 'attention_mode', 'schedule_date', 'hardness')
              if row.get(key) is not None}
    for key in ('start_minute', 'end_minute', 'planned_start_minute', 'planned_end_minute',
                'actual_start_minute', 'actual_end_minute', 'day_offset', 'progress'):
        if key in row:
            result[key] = _number(row[key])
    start = result.get('planned_start_minute', result.get('start_minute'))
    end = result.get('planned_end_minute', result.get('end_minute'))
    result['time_label'] = ' — '.join(format_day_minute(int(v)) if v is not None else '待定'
                                      for v in (start, end))
    for key in ('state_before', 'state_after'):
        values = row.get(key)
        if isinstance(values, dict):
            result[key] = {field: _number(values.get(field)) for field in (*FIELDS, 'sleep_debt')}
    # 3.17.7 改进1：只暴露人话化抽取原因这一个策展字段（其余 metadata 仍不上 UI）。
    metadata = row.get('metadata')
    if isinstance(metadata, dict):
        reason = metadata.get('draw_reason_zh')
        if isinstance(reason, str) and reason:
            result['draw_reason_zh'] = reason[:80]
    return result


def _event_slot_label(row: dict[str, Any]) -> str:
    """事件时段展示标签（3.17.7 改进2，只读投影）：生成事件 → 自身 time_slot；
    内置事件 allowed_slots 覆盖全部三段 → 『全天』，收窄 → 以 / 连接；无信息 → ''。"""
    from .event_slots import TIME_SLOTS
    slot = row.get('time_slot')
    if isinstance(slot, str) and slot in TIME_SLOTS:
        return slot
    slots = row.get('allowed_slots')
    if isinstance(slots, (list, tuple)):
        valid = [s for s in TIME_SLOTS if s in slots]
        if len(valid) == len(TIME_SLOTS):
            return '全天'
        if valid:
            return '/'.join(valid)
    return ''


def _schedule_pools(planning: Any, model: Any, now: datetime, read: Any) -> dict[str, Any]:
    """Read the existing pools without rolling, refreshing or exposing raw metadata."""
    from .rolling_planner import RANDOM_EVENT_POOL

    def events(rows: Any) -> list[dict[str, Any]]:
        return [
            {'name': str(row['name'])[:160], 'goal_type': str(row.get('goal_type') or '')[:40],
             'min_minutes': _number(row.get('min_minutes')),
             'max_minutes': _number(row.get('max_minutes')),
             'event_id': str(row.get('event_id') or row.get('key') or '')[:80],
             'pool_kind': str(row.get('pool_kind') or 'reusable')[:20],
             'generation_id': str(row.get('generation_id') or '')[:96],
             'consumption_status': str((row.get('consumption') or {}).get('status') or '')[:24],
             'consumed_at': str((row.get('consumption') or {}).get('picked_at') or '')[:48],
             'time_slot': _event_slot_label(row)}
            for row in rows[:40] if isinstance(row, dict) and row.get('name')
        ]

    catalog = getattr(planning, 'event_catalog', None)
    snapshot = read('小事件库暂不可读', lambda: catalog.store.snapshot, None) if catalog else None
    resume = getattr(model, 'resume_candidates', None)
    paused = read('恢复池暂不可读', lambda: resume(now), []) if callable(resume) else []
    return {
        'recovery': [
            {'name': str(row['name'])[:160], 'goal_type': str(row.get('goal_type') or '')[:40],
             'schedule_date': str(row.get('schedule_date') or '')[:10],
             'progress': _number(row.get('progress'))}
            for row in paused[:20] if isinstance(row, dict) and row.get('name')
        ],
        'builtin': events(snapshot.builtin_events if snapshot else RANDOM_EVENT_POOL),
        'generated': events(snapshot.reusable_events) if snapshot else [],
        'one_shot': events(snapshot.one_shot_events) if snapshot else [],
        'consumed': events(snapshot.consumed_events) if snapshot else [],
        'generation_id': str(snapshot.generation_id)[:96] if snapshot else '',
        'one_shot_total': len(snapshot.one_shot_events) + len(snapshot.consumed_events) if snapshot else 0,
        'generated_at': str(snapshot.generated_at)[:48] if snapshot and snapshot.generated_at else None,
        'next_update_at': str(snapshot.next_update_at)[:48] if snapshot and snapshot.next_update_at else None,
    }


#: day 路径单日序列点上限（5 分钟采样 × 1440 分钟 = 288；结果体量守卫前的装配上限）。
DAY_POINT_CAP = 288


def _sleep_session_view(row: Any) -> dict[str, Any]:
    """sleep_sessions 行 → 面板展示面（与 hours 旧路径逐字同字段；只读投影共用）。"""
    data = row if isinstance(row, dict) else {}
    selected = {key: _number(data.get(key)) for key in
                ('actual_start_minute', 'actual_end_minute', 'continuous_minutes',
                 'interruption_count', 'energy_start', 'energy_end', 'sleep_debt_before',
                 'sleep_debt_after', 'quality')}
    selected.update({key: str(data[key])[:80] if data.get(key) is not None else None for key in
                     ('schedule_date', 'wake_reason', 'created_at', 'updated_at')})
    return selected


def _normalize_day(value: Any) -> tuple[str, str]:
    """day 参数严格口径（宿主参数严格校验不做隐式转换，本层同样不宽容）：

    恰好 ``YYYY-MM-DD``（10 字符零填充）且是有效日期 → 原样返回；空/None → 空串
    （不影响既有 hours 路径）；其余一律按空串处理并返回 warning 文本（不猜日期）。
    """
    if value is None or value == '':
        return '', ''
    if not isinstance(value, str):
        return '', 'day 参数应为字符串（YYYY-MM-DD），已忽略'
    text = value.strip()
    if not text:
        return '', ''
    if len(text) != 10:
        return '', 'day 参数格式应为 YYYY-MM-DD，已忽略'
    try:
        datetime.strptime(text, '%Y-%m-%d')
    except ValueError:
        return '', 'day 参数不是有效日期（YYYY-MM-DD），已忽略'
    return text, ''


def _point_time(value: Any, tz: Any) -> datetime | None:
    """序列点 ``at`` → datetime（naive 防御性按当前 tz 贴靠；非法 → None 不猜）。"""
    try:
        stamp = datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=tz)
    return stamp


def _decimate_points(points: list[dict[str, Any]], cap: int) -> tuple[list[dict[str, Any]], bool]:
    """均匀抽稀到 ``cap`` 点（保留首尾，跳过点的 gap_before 标记随带到保留点）。"""
    total = len(points)
    cap = max(2, int(cap))
    if total <= cap:
        return points, False
    indices = [round(i * (total - 1) / (cap - 1)) for i in range(cap)]
    reduced: list[dict[str, Any]] = []
    previous = -1
    for index in indices:
        row = dict(points[index])
        if any(points[k].get('gap_before') for k in range(previous + 1, index + 1)):
            row['gap_before'] = True
        reduced.append(row)
        previous = index
    return reduced, True


#: 缺口补全口径（报告 §2.4/§2.5，2026-10-29）：单段 ≤2h 的**中间**缺口线性插值；
#: >2h 大间隙不再线性，改由 sleep_sessions 会话映射 estimated 睡眠段（或保持空洞）。
LINEAR_GAP_MAX_SECONDS = 2 * 3600
#: 线性插值合成点的落点步长 = 各序列自身采样节拍（life_history / emotion 观测均 5 分钟）。
ESTIMATED_STEP_SECONDS = SAMPLE_SECONDS


def _linear_gap_points(left: dict[str, Any], right: dict[str, Any], *,
                       time_key: str, fields: tuple[str, ...],
                       agree_bools: tuple[str, ...]) -> list[dict[str, Any]]:
    """一对真实观测点之间的线性插值合成点（仅 ≤2h 中间缺口；全部 ``estimated:true``）。

    数值字段线性内插；布尔字段仅当两端一致才随带（不一致 = 状态翻转发生过，
    不猜落在哪一刻 → 缺省）。``reason='estimated_gap_fill'`` 与真实采样点的
    ``reason`` 值域区分。跨度越界（无缺口 / >2h）返回空列表。
    """
    left_at = _point_time(left.get(time_key), None)
    right_at = _point_time(right.get(time_key), None)
    if left_at is None or right_at is None:
        return []
    try:
        span = (right_at - left_at).total_seconds()
    except TypeError:  # aware/naive 混比防御：不补（保持如实缺口）
        return []
    if span <= ESTIMATED_STEP_SECONDS or span > LINEAR_GAP_MAX_SECONDS:
        return []
    synthetics: list[dict[str, Any]] = []
    offset = ESTIMATED_STEP_SECONDS
    while offset < span:
        fraction = offset / span
        point: dict[str, Any] = {
            time_key: (left_at + timedelta(seconds=offset)).isoformat(),
            'estimated': True, 'reason': 'estimated_gap_fill'}
        for field in fields:
            start = left.get(field)
            end = right.get(field)
            if (type(start) in (int, float) and not isinstance(start, bool)
                    and type(end) in (int, float) and not isinstance(end, bool)):
                point[field] = round(start + (end - start) * fraction, 6)
        for field in agree_bools:
            if isinstance(left.get(field), bool) and left.get(field) == right.get(field):
                point[field] = left[field]
        synthetics.append(point)
        offset += ESTIMATED_STEP_SECONDS
    return synthetics


def _fill_small_gaps(rows: list[dict[str, Any]], *, time_key: str,
                     fields: tuple[str, ...],
                     agree_bools: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """在线小缺口线性补全（报告 §2.4；**投影层 only，绝不写回真实历史**）。

    仅补窗口序列**中间**、两端都有真实观测点且单段 ≤2h 的缺口：按 5 分钟节拍
    线性插值合成点，逐点带 ``estimated:true``。首尾缺口（窗口外无锚点）天然
    无两端观测、与 >2h 缺口一律不补——后者归 :func:`_estimated_sleep_segments`
    的会话估算或如实保持空洞。合成点只进本 API 载荷：不写 life_history /
    emotion 观测环，不参与 ``total_points`` 与任何真实统计（调用方按真实行计数）。
    """
    filled: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if index:
            previous = rows[index - 1]
            if isinstance(previous, dict) and isinstance(row, dict) \
                    and not (previous.get('estimated') or row.get('estimated')):
                filled.extend(_linear_gap_points(previous, row, time_key=time_key,
                                                 fields=fields, agree_bools=agree_bools))
        filled.append(row if isinstance(row, dict) else {})
    return filled


def _large_gap_count(rows: list[dict[str, Any]], *, time_key: str) -> int:
    """窗口内 >2h 真实缺口的段数（如实计数；是否被睡眠估算段覆盖不改变该值）。"""
    count = 0
    for index in range(1, len(rows)):
        left = _point_time(rows[index - 1].get(time_key), None) \
            if isinstance(rows[index - 1], dict) else None
        right = _point_time(rows[index].get(time_key), None) \
            if isinstance(rows[index], dict) else None
        if left is None or right is None:
            continue
        try:
            if (right - left).total_seconds() > LINEAR_GAP_MAX_SECONDS:
                count += 1
        except TypeError:  # aware/naive 混比防御：该对不计数
            continue
    return count


def _estimated_sleep_segments(db: Any, *, day_start: datetime,
                              day_end: datetime) -> list[dict[str, Any]]:
    """窗口内大间隙的睡眠会话估算段（报告 §2.5：图表用会话数据画虚线/斜纹）。

    从 ``sleep_sessions`` 取与窗口重叠的 **completed** 会话（``actual_end_minute``
    非 NULL = 已收口；schedule_db 无 status 列，completed 口径与会话写库方
    plugin.py 一致；含 ``offline_reconnect`` 离线补全会话，不按 wake_reason 过滤）。
    会话真实起止按 schedule_date 日帧分钟（跨午夜 >1440）映射为绝对时刻后裁剪到
    窗口。会话不覆盖的剩余大间隙**保持空洞**（如实缺口，不伪造）。只读投影：
    不写任何库/历史；段上的 ``estimated:true`` 表示「图表估算段」（该段无 5 分钟
    真实观测样本、由会话记录覆盖渲染），不代表会话记录本身是估算。
    """
    segments: list[dict[str, Any]] = []
    for midnight in (day_start - timedelta(days=1), day_start):
        date_text = midnight.date().isoformat()
        try:
            rows = db.query_sleep_sessions(schedule_date=date_text, limit=32)
        except Exception:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            start_minute = row.get('actual_start_minute')
            end_minute = row.get('actual_end_minute')
            if start_minute is None or end_minute is None:
                continue  # 未收口（非 completed）→ 不进估算段
            try:
                session_start = midnight + timedelta(minutes=int(start_minute))
                session_end = midnight + timedelta(minutes=int(end_minute))
            except (TypeError, ValueError, OverflowError):
                continue
            segment_start = max(session_start, day_start)
            segment_end = min(session_end, day_end)
            if segment_end <= segment_start:
                continue
            metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
            wake_reason = str(row.get('wake_reason'))[:40] if row.get('wake_reason') else None
            note = metadata.get('unrecorded_note')
            segments.append({
                'kind': 'sleep', 'estimated': True,
                'start': segment_start.isoformat(), 'end': segment_end.isoformat(),
                'session_start': session_start.isoformat(),
                'session_end': session_end.isoformat(),
                'session_id': str(row.get('session_id') or '')[:80],
                'schedule_date': str(row.get('schedule_date') or date_text)[:10],
                'wake_reason': wake_reason,
                'source': 'sleep_sessions',
                'offline': bool(metadata.get('offline_settlement')
                                or metadata.get('offline_reconnect')
                                or wake_reason == 'offline_reconnect'),
                'continuous_minutes': _number(row.get('continuous_minutes')),
                'interruption_count': _number(row.get('interruption_count')),
                'quality': _number(row.get('quality')),
                'sleep_debt_after': _number(row.get('sleep_debt_after')),
                'unrecorded_note': str(note)[:80] if note else None,
            })
    segments.sort(key=lambda item: item['start'])
    return segments


def _build_day_view(plugin: Any, *, cfg: Any, tz_name: str, now: datetime, day: str,
                    warnings: list[str]) -> dict[str, Any]:
    """day 路径投影（报告 §2.3「按日历史」）：单日实际足迹 / 计划意图 / 睡眠会话 /
    五维与 VAD 序列 / 当日 KPI，全部只读、分键承载（footprint / intent 同级）。

    保留期真源：episode = ``[life_state].keep_episode_days``（默认 14，
    plugin.py 清理调用处）；计划意图（schedule_items 清理）= 3 天
    （goal_manager.cleanup_expired_schedules → cleanup_old_days(keep_days=3)，
    归档表 schedule_archive 随行快照保留）；五维/VAD = 7 天环（life_history /
    emotion observations 各自 retention）。计划被保留期清理时给 plans_available=false
    之类明确提示字段，不伪造数据。
    """
    from .rolling_planner import extract_spine_intents
    ctx = PluginContext(plugin)

    def read(label: str, callback: Any, fallback: Any) -> Any:
        try:
            return callback()
        except Exception:
            warnings.append(label)
            return fallback

    tz = now.tzinfo
    day_date = datetime.strptime(day, '%Y-%m-%d').date()
    day_start = datetime.combine(day_date, dt_time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    today = now.date()
    enabled = bool(getattr(getattr(cfg, 'schedule', None), 'enabled', False))
    model = ctx.life_model
    db = ctx.db
    ready = enabled and model is not None and db is not None
    view: dict[str, Any] = {
        'available': True, 'date': day, 'timezone': tz_name,
        'window_start': day_start.isoformat(), 'window_end': day_end.isoformat(),
        'is_today': day_date == today,
    }

    # ---- 实际足迹（activity_episodes 该日全部；≤120，超出截断并标记）----
    episode_rows = read('该日活动记录暂不可读',
                        lambda: db.episodes_today(day), []) if ready else []
    if not isinstance(episode_rows, list):
        episode_rows = []
    episode_total = len(episode_rows)
    keep_raw = getattr(getattr(cfg, 'life_state', None), 'keep_episode_days', 14)
    keep_days = keep_raw if type(keep_raw) is int and not isinstance(keep_raw, bool) and keep_raw > 0 else 14
    footprint = {
        'episodes': [_row(row) for row in episode_rows[:120]],
        'total': episode_total, 'truncated': episode_total > 120,
        'retention_days': keep_days,
        'retention_expired': day_date < today - timedelta(days=keep_days),
    }

    # ---- 计划意图（该日 schedule_items + 进账归档；≤80，超出截断）----
    item_rows = read('该日计划意图暂不可读',
                     lambda: db.get_day_schedule(day), []) if ready else []
    archived_rows = read('该日计划归档暂不可读',
                         lambda: db.get_archived_day_schedule(day, limit=200), []) if ready else []
    item_rows = item_rows if isinstance(item_rows, list) else []
    archived_rows = archived_rows if isinstance(archived_rows, list) else []
    current_intents = extract_spine_intents(item_rows)
    archived_items: list[dict[str, Any]] = []
    for row in archived_rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        params = item.get('parameters')
        if isinstance(params, str):
            try:
                item['parameters'] = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                item['parameters'] = {}
        archived_items.append(item)
    # 归档按 archive_id 升序返回：同 item_id 保留最后一次归档快照；现行计划优先于归档。
    deduped: dict[str, dict[str, Any]] = {}
    for row in extract_spine_intents(archived_items):
        if row.get('item_id'):
            deduped[str(row['item_id'])] = row
    intent_rows: list[dict[str, Any]] = [{**_row(row), 'origin': 'current'}
                                         for row in current_intents]
    seen_ids = {str(row.get('item_id') or '') for row in current_intents}
    for item_id, row in deduped.items():
        if item_id not in seen_ids:
            intent_rows.append({**_row(row), 'origin': 'archived'})
    intent_total = len(intent_rows)
    # 计划意图保留期 = 3 天（goal_manager.cleanup_expired_schedules 的 keep_days 硬值）。
    intent_retention_expired = day_date < today - timedelta(days=3)
    if intent_rows:
        note = '该日计划行已过 3 天保留期，以下含进账归档快照' if intent_retention_expired else ''
    elif intent_retention_expired:
        note = '该日计划意图已超出 3 天保留期被清理，归档中亦无该日快照'
    else:
        note = '保留期内未查询到该日计划意图（该日可能未生成计划）'
    intent = {
        'items': intent_rows[:80], 'total': intent_total, 'truncated': intent_total > 80,
        'available': bool(intent_rows),
        'plans_available': bool(intent_rows) or not intent_retention_expired,
        'retention_expired': intent_retention_expired, 'retention_days': 3,
        'sources': {'schedule_items': len(current_intents), 'schedule_archive': len(deduped)},
        'note': note,
    }

    # ---- 该日睡眠会话（sleep_sessions 按 schedule_date 读面，与旧路径同字段）----
    session_rows = read('该日睡眠会话暂不可读',
                        lambda: db.query_sleep_sessions(schedule_date=day, limit=16),
                        []) if ready else []
    if not isinstance(session_rows, list):
        session_rows = []
    day_sessions = [_sleep_session_view(row) for row in session_rows]
    # 大间隙估算段（§2.5）：completed 会话 → estimated 睡眠段（虚线/斜纹渲染用）；
    # 独立分键承载，绝不混入 sessions 真实行，summary.sleep_minutes 口径不变。
    estimated_segments = read('该日睡眠估算段暂不可读',
                              lambda: _estimated_sleep_segments(db, day_start=day_start,
                                                                day_end=day_end),
                              []) if ready else []
    if not isinstance(estimated_segments, list):
        estimated_segments = []

    def day_points(points: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for point in points if isinstance(points, list) else []:
            if not isinstance(point, dict):
                continue
            stamp = _point_time(point.get('at'), tz)
            if stamp is not None and day_start <= stamp < day_end:
                rows.append(dict(point))
        return rows

    # ---- 该日五维序列（life_history 7 天环，取满保留窗后按日窗过滤）----
    state_history: dict[str, Any] = {'points': [], 'total_points': 0,
                                     'downsampled': False, 'available': False}
    life = ctx.life_state
    if enabled and life is not None:
        def _state_day() -> dict[str, Any]:
            full = life.history_snapshot(now=now, hours=168, limit=MAX_POINTS)
            rows = day_points(full.get('points') if isinstance(full, dict) else None)
            large_gap_count = _large_gap_count(rows, time_key='at')
            # 小缺口线性补全（≤2h 中间缺口，estimated:true）→ 再抽稀到日上限；
            # total_points 恒按真实行计（合成点不进统计口径，§2.4/§2.5）。
            filled = _fill_small_gaps(rows, time_key='at',
                                      fields=(*FIELDS, 'sleep_debt', 'mental_state'),
                                      agree_bools=('sleeping',))
            kept, downsampled = _decimate_points(filled, DAY_POINT_CAP)
            return {'points': kept, 'total_points': len(rows),
                    'downsampled': downsampled, 'available': True,
                    'estimated_points': sum(1 for point in kept if point.get('estimated')),
                    'large_gap_count': large_gap_count}
        # 过滤/比较一并与读取同入 read() 隔离（时区降级环境下 aware/naive 混比不炸面板）。
        state_history = read('该日生活历史暂不可读', _state_day,
                             {'points': [], 'total_points': 0, 'downsampled': False,
                              'available': False})
    # ---- 该日 VAD 序列（emotion observations 7 天环，同口径过滤）----
    vad_history: dict[str, Any] = {'points': [], 'total_points': 0,
                                   'downsampled': False, 'available': False}
    observations = getattr(ctx.emotion, 'service', None)
    observations = getattr(observations, '_observations', None) if observations else None
    if observations is not None:
        def _vad_day() -> dict[str, Any]:
            full = observations.snapshot(now, 168)
            rows = day_points(full.get('points') if isinstance(full, dict) else None)
            large_gap_count = _large_gap_count(rows, time_key='at')
            filled = _fill_small_gaps(rows, time_key='at',
                                      fields=('valence', 'arousal', 'dominance'),
                                      agree_bools=('frozen',))
            kept, downsampled = _decimate_points(filled, DAY_POINT_CAP)
            return {'points': kept, 'total_points': len(rows),
                    'downsampled': downsampled, 'available': True,
                    'estimated_points': sum(1 for point in kept if point.get('estimated')),
                    'large_gap_count': large_gap_count}
        vad_history = read('该日情绪序列暂不可读', _vad_day,
                           {'points': [], 'total_points': 0, 'downsampled': False,
                            'available': False})

    # ---- 当日 KPI 汇总（由该日真实行统计；不虚构）----
    statuses = [str(row.get('status') or '') for row in episode_rows]
    sleep_minutes = sum(row.get('continuous_minutes') for row in day_sessions
                        if type(row.get('continuous_minutes')) in (int, float))
    summary = {
        'episode_count': episode_total,
        'completed_count': statuses.count('completed'),
        'interrupted_count': statuses.count('interrupted'),
        'abandoned_count': statuses.count('abandoned'),
        'skipped_count': statuses.count('skipped'),
        'paused_count': statuses.count('paused'),
        'planned_count': statuses.count('planned'),
        'active_count': statuses.count('active'),
        'intent_count': len(current_intents),
        'archived_intent_count': len(deduped),
        'sleep_session_count': len(day_sessions),
        'sleep_minutes': _number(sleep_minutes),
    }
    view.update(footprint=footprint, intent=intent,
                sleep={'sessions': day_sessions, 'estimated_segments': estimated_segments},
                state_history=state_history, vad_history=vad_history,
                summary=summary,
                # 聚合面估算计数（③）：线性合成点 + 睡眠估算段；估算点恒不计入
                # total_points / KPI 等真实统计（各序列按真实行计，见上）。
                estimated={'linear_points':
                               int(state_history.get('estimated_points') or 0)
                               + int(vad_history.get('estimated_points') or 0),
                           'sleep_segments': len(estimated_segments)})
    return view


#: maimai 互动白名单投影字段（2026-10-30 交互改造；2026-11-03 收敛）：
#: 字段名 → (类型, 出厂默认, 下限, 上限)。
#: 只含展示/交互参数；整份配置绝不下发前端（前端默认值沿本表同值兜底，不另造第二套语义）。
#: 2026-11-03 起 old 形象具体参数撤出配置（前端按生产现值固化常量），白名单只留
#: 两形象共用的 mascot_size_px 与 history_* 展示参数。
_MAIMAI_PROJECTION: dict[str, tuple[type, Any, float, float]] = {
    'mascot_size_px': (int, 144, 48, 192),
    'history_changes_limit': (int, 30, 5, 30),
    'history_state_epsilon': (float, 0.01, 0.001, 0.1),
    'history_debt_epsilon': (float, 0.1, 0.01, 1.0),
}


def _maimai_view(cfg: Any) -> dict[str, Any]:
    """``[maimai_interaction]`` → 面板 ``maimai`` 只读投影（白名单 + 范围钳制）。

    2026-11-03 起 old 形象具体参数不再投影（前端按生产现值固化常量）；
    ``preset`` 为固定枚举原样携带。配置段缺失（防御路径，正常构造下恒存在）
    时按白名单出厂默认出全量视图，与前端 CSS 同值兜底口径一致。
    """
    section = getattr(cfg, 'maimai_interaction', None)
    view: dict[str, Any] = {'enabled': bool(getattr(section, 'enabled', True))}
    display = getattr(cfg, 'dashboard_display', None)
    for name, (kind, default, low, high) in _MAIMAI_PROJECTION.items():
        source = display if name.startswith('history_') else section
        raw = getattr(source, name, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
                or not math.isfinite(float(raw)):
            raw = default
        value = min(high, max(low, float(raw)))
        view[name] = int(round(value)) if kind is int else round(value, 4)
    preset = getattr(section, 'preset', None)
    view['preset'] = {'old': 'fish', 'new': 'human'}.get(preset, preset) if preset in ('old', 'new', 'fish', 'human') else 'human'
    return view


def build_life_dashboard(plugin: Any, *, hours: int = 24,
                         usage_hours: int = 720,
                         usage_rows_limit: int = 0,
                         usage_rows_offset: int = 0,
                         now: datetime | None = None, day: str = "") -> dict[str, Any]:
    """只读生活面板投影。

    ``day``（可选，``YYYY-MM-DD``）：非空 → 结果附加 ``day`` 键（单日投影，
    见 :func:`_build_day_view`）；空串/非法 → 行为与既有 hours 路径逐字一致
    （非法值附 warning 字段，不改变任何既有字段语义）。
    """
    ctx = PluginContext(plugin)
    cfg = ctx.config
    tz_name = str(getattr(getattr(cfg, 'schedule_advanced', None), 'timezone', '') or 'Asia/Shanghai')
    clock = TimezoneManager(tz_name)
    now = now or clock.get_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=clock.get_now().tzinfo)
    date = now.strftime('%Y-%m-%d')
    hours = hours if type(hours) is int and hours in (6, 24, 72, 168) else 24
    usage_hours = (usage_hours if type(usage_hours) is int
                   and usage_hours in (24, 168, 720) else 720)
    enabled = bool(getattr(getattr(cfg, 'schedule', None), 'enabled', False))
    life = ctx.life_state
    model = ctx.life_model
    db = ctx.db
    warnings: list[str] = []

    def read(label: str, callback: Any, fallback: Any) -> Any:
        try:
            return callback()
        except Exception:
            warnings.append(label)
            return fallback

    state = read('生活状态暂不可读', life.snapshot, {}) if life is not None and enabled else {}
    safe_state = {key: _number(state.get(key)) for key in (*FIELDS, 'sleep_debt', 'mental_state')}
    for key in ('updated_at', 'last_tick_at', 'last_meal_at', 'last_break_at', 'last_sleep_at',
                'sleep_started_at', 'sleep_wake_target', 'sleep_lie_in_cap', 'last_wake_reason'):
        safe_state[key] = str(state[key])[:80] if state.get(key) is not None else None
    safe_state['sleep_interrupted'] = bool(state.get('sleep_interrupted'))
    safe_state['continuous_sleep_minutes'] = _number(state.get('continuous_sleep_minutes'))
    age = None
    future_state = False
    if safe_state['updated_at']:
        try:
            updated = datetime.fromisoformat(safe_state['updated_at'])
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=now.tzinfo)
            elapsed = (now-updated).total_seconds()
            future_state = elapsed < -300
            age = max(0., elapsed)
        except (ValueError, TypeError):
            pass
    ready = enabled and model is not None
    today = read('当日活动暂不可读', lambda: model.today(now), {}) if ready else {}
    current = read('运行中活动暂不可读', lambda: db.active_episode(), None) if ready and db else None
    if not isinstance(current, dict) or current.get('status') != 'active':
        current = None
    history = read('生活历史暂不可读', lambda: life.history_snapshot(now=now, hours=hours), {'points': []}) if enabled and life else {'points': []}
    sessions: list[dict[str, Any]] = []
    if ready and db:
        for offset in range(7):
            # 局部名 scan_day：不得遮蔽 build_life_dashboard 的 day 形参（day 路径入参）。
            scan_day = (now-timedelta(days=offset)).strftime('%Y-%m-%d')
            rows = read('睡眠记录暂不可读', lambda d=scan_day: db.query_sleep_sessions(schedule_date=d, limit=16), [])
            for row in rows:
                sessions.append(_sleep_session_view(row))
            if len(sessions) >= 8:
                break
        sessions.sort(key=lambda row: (row.get('schedule_date') or '', row.get('actual_start_minute') or 0), reverse=True)
    engine = ctx.sleep_engine
    sleep_live = read('睡眠引擎暂不可读', lambda: engine.sleep_status(now), None) if enabled and engine else None
    safe_sleep = None
    if isinstance(sleep_live, dict):
        safe_sleep = {key: sleep_live.get(key) for key in
                      ('asleep', 'wake_reason', 'continuous_minutes', 'interruption_count',
                       'wake_at', 'lie_in_cap', 'closed_at')}
    dynamic_sleep = (read('动态睡眠状态暂不可读', lambda: engine.dynamic_status(now), None)
                     if getattr(engine, "dynamic_enabled", False) else None)
    companion = read('陪伴状态暂不可读', lambda: _companion_summary(plugin, date=date), {'available': False})
    observations = []
    if safe_state['focus'] is not None and safe_state['focus'] < .3:
        observations.append('专注偏低，滚动日程会提供休息恢复机会；数值不会被直接抬高。')
    if safe_state['stress'] == 0:
        observations.append('压力为零表示当前很放松；减压速率累积并钳到零是允许的状态。')
    if safe_state['energy'] is not None and safe_state['energy'] < .3:
        observations.append('精力偏低，留意后续休息与恢复活动。')
    if safe_state['hunger'] is not None and safe_state['hunger'] > .7:
        observations.append('饥饿较高，留意当前计划中的用餐安排。')
    if safe_state['stress'] is not None and safe_state['stress'] > .8:
        observations.append('压力较高，适合放慢节奏。')
    if safe_state['sleep_debt'] is not None and safe_state['sleep_debt'] > 2:
        observations.append('睡眠债仍在，后续完整睡眠会逐步偿还。')
    if enabled and not ready:
        warnings.append('日程运行态尚未就绪')
    result = {
        'schema_version': 1, 'generated_at': now.isoformat(), 'timezone': tz_name,
        'window_hours': hours, 'state': safe_state, 'state_available': bool(state),
        'state_age_seconds': age, 'stale': age is None or age > 900 or future_state,
        'history': history,
        'schedule': {'enabled': enabled, 'runtime_ready': ready, 'date': date,
                     'pools': _schedule_pools(ctx.planning, model, now, read) if ready else {},
                     'current': _row(current) if current else None,
                     'intents': [_row(row) for row in today.get('intents', [])[:80]],
                     'episodes': [_row(row) for row in today.get('episodes', [])[-120:]],
                     'summary': {key: _number(today.get('summary', {}).get(key)) for key in
                                 ('intent_count', 'episode_count', 'completed_count', 'interrupted_count', 'abandoned_count')}},
        'sleep': {'current': safe_sleep, 'sessions': sessions[:8],
                  'configured_sleep': str(getattr(getattr(cfg, 'schedule', None), 'sleep_time', '') or '')[:8],
                  'configured_wake': str(getattr(getattr(cfg, 'schedule', None), 'wake_time', '') or '')[:8]},
        'companion': companion, 'observations': observations, 'warnings': list(dict.fromkeys(warnings))[:8],
    }
    result['maimai'] = _maimai_view(cfg)
    if isinstance(dynamic_sleep, dict):
        result['sleep']['dynamic'] = {key: dynamic_sleep[key] for key in (
            'band', 'pressure', 'circadian', 'debt', 'schedule', 'activity', 'score',
            'window_start', 'window_end', 'earliest_wake', 'latest_wake', 'phase_minutes',
            'onset_reason', 'decision', 'nap_reason', 'interruption') if key in dynamic_sleep}
    result.update(_extension_summary(plugin, now, hours, usage_hours,
                                     usage_rows_limit, usage_rows_offset))
    if future_state:
        result['warnings'].append('状态时间晚于当前时钟，请核对服务器时间')
    if history.get('clock_regressed'):
        result['warnings'].append('时钟回拨，历史暂缓采样以保留时间顺序')
    # day 路径（报告 §2.3「按日历史」）：附加 day 键，既有字段与语义逐字不动。
    day_text, day_warning = _normalize_day(day)
    if day_text:
        result['day'] = _build_day_view(plugin, cfg=cfg, tz_name=tz_name, now=now,
                                        day=day_text, warnings=result['warnings'])
    elif day_warning:
        # 非法 day 按空串处理（hours 路径原样返回）；warning 字段随附，不猜日期。
        result['warnings'].append(day_warning)
        result['day'] = {'available': False, 'requested': str(day)[:40], 'warning': day_warning}
    # Host result limit is 512 KiB. Reserve room for its envelope and JSON spacing.
    result['truncated'] = False
    while len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 420*1024:
        result['truncated'] = True
        rows = result['history'].get('points', [])
        if len(rows) > 60:
            result['history']['points'] = rows[::2] + ([rows[-1]] if rows[-1] not in rows[::2] else [])
            result['history']['downsampled'] = True
        elif len(result['schedule']['episodes']) > 10:
            result['schedule']['episodes'] = result['schedule']['episodes'][len(result['schedule']['episodes'])//2:]
        elif len(result.get('emotion', {}).get('history', {}).get('points', [])) > 30:
            series = result['emotion']['history']
            points = series['points']
            # Keep gap flags visible when removing intermediate samples.
            reduced = []
            for index in range(0, len(points), 2):
                point = dict(points[index])
                if index and any(p.get('gap_before') for p in points[max(0,index-1):index+1]):
                    point['gap_before'] = True
                reduced.append(point)
            if reduced[-1].get('at') != points[-1].get('at'):
                reduced.append(points[-1])
            series.update(points=reduced, downsampled=True)
        elif result['schedule']['intents']:
            result['schedule']['intents'] = result['schedule']['intents'][:len(result['schedule']['intents'])//2]
        elif len(result.get('day', {}).get('state_history', {}).get('points', [])) > 60:
            points = result['day']['state_history']['points']
            reduced, _ = _decimate_points(points, max(2, len(points)//2))
            result['day']['state_history'].update(points=reduced, downsampled=True)
        elif len(result.get('day', {}).get('vad_history', {}).get('points', [])) > 60:
            points = result['day']['vad_history']['points']
            reduced, _ = _decimate_points(points, max(2, len(points)//2))
            result['day']['vad_history'].update(points=reduced, downsampled=True)
        elif len(result.get('day', {}).get('footprint', {}).get('episodes', [])) > 10:
            day_footprint = result['day']['footprint']
            day_footprint['episodes'] = day_footprint['episodes'][len(day_footprint['episodes'])//2:]
            day_footprint['truncated'] = True
        elif len(result.get('day', {}).get('intent', {}).get('items', [])) > 10:
            day_intent = result['day']['intent']
            day_intent['items'] = day_intent['items'][:len(day_intent['items'])//2]
            day_intent['truncated'] = True
        else:
            # Independent projections must never trap the read handler in a size loop.
            result.update(emotion={'available': False, 'reason': 'payload_limit'},
                          qzone={'available': False, 'reason': 'payload_limit'},
                          qzone_video={'available': False, 'reason': 'payload_limit'},
                          usage={}, health={})
            if result.get('day') is not None:
                result['day'] = {'available': False, 'reason': 'payload_limit'}
            break
    return result


def _extension_summary(plugin: Any, now: datetime, hours: int,
                       usage_hours: int = 720, usage_rows_limit: int = 0,
                       usage_rows_offset: int = 0) -> dict[str, Any]:
    """Independent module projections; never propagate raw event descriptions/logs."""
    result: dict[str, Any] = {'emotion': {'available': False}, 'qzone': {'available': False},
                              'qzone_video': {'available': False}, 'usage': {}, 'health': {}}
    ctx = PluginContext(plugin)
    try:
        service = getattr(ctx.emotion, 'service', None)
        if service is not None:
            from core.llm_utils import purpose_task
            raw = service.status_snapshot()
            history = getattr(service, '_observations', None)
            result['emotion'] = {
                'available': True, 'enabled': bool(service._enabled()), 'frozen': bool(raw.get('frozen')),
                'label': str(raw.get('label') or raw.get('mood_text') or '')[:80],
                'effective': {key: _number(raw.get('effective', {}).get(key)) for key in ('valence', 'arousal', 'dominance')},
                'event_count': int(raw.get('event_count') or 0),
                'recent_events': [{key: row.get(key) for key in ('time', 'v', 'a', 'd')} for row in raw.get('recent_events', [])[-3:]],
                'history': history.snapshot(now, hours) if history else {'points': []},
                'referee': {'task': purpose_task(plugin, 'emotion_referee', default='utils'),
                            'attempts': int(getattr(service, '_overhaul_retry_count', 0)),
                            'cached': bool(getattr(service, '_pending_overhaul', None)),
                            'abandoned': bool(getattr(service, '_overhaul_abandoned', False)),
                            **{k: v for k, v in getattr(service, '_referee_health', {}).items()
                               if k in ('last_attempt_at', 'last_success_at', 'error_class')}}}
    except Exception:
        result['emotion'] = {'available': False, 'reason': 'projection_unavailable'}
    try:
        service = ctx.qzone
        if service is not None:
            raw = service.status_snapshot()
            result['qzone'] = {'available': True, **{key: raw.get(key) for key in
                                ('enabled', 'posts_today', 'daily_max_posts', 'reply_comments_enabled',
                                 'cookie_health', 'delivery', 'last_post_at', 'cooldown_minutes')}}
    except Exception:
        result['qzone'] = {'available': False, 'reason': 'projection_unavailable'}
    # PR-D1（H10）：空间视频候选/资产/回执计数投影（只读；系统板块计数芯片数据源）。
    # 未装配/读取失败 → available=False 如实降级；只暴露计数与状态分布，不含路径/正文。
    try:
        bridge = ctx.qzone_video
        if bridge is not None:
            snap = bridge.ledger.snapshot()
            assets = bridge.ledger.assets_overview()
            cfg = getattr(getattr(plugin, 'config', None), 'qzone_video', None)
            result['qzone_video'] = {
                'available': True,
                'mode': str(getattr(cfg, 'mode', 'shadow') or 'shadow'),
                'candidate_states': snap.get('candidate_states', {}),
                'publish_attempts': snap.get('publish_attempts', {}),
                'fact_sync': snap.get('fact_sync', {}),
                'asset_states': assets.get('asset_states', {}),
                'asset_total_bytes': assets.get('total_bytes', 0),
            }
    except Exception:
        result['qzone_video'] = {'available': False, 'reason': 'projection_unavailable'}
    try:
        from core.monitoring import usage_summary, runtime_health
        data_dir = ctx.data_dir
        if data_dir:
            result['usage'] = usage_summary(data_dir, now, usage_hours)
            if int(usage_rows_limit or 0) > 0:
                from core.llm_usage import LLMUsageTracker
                result['usage']['rows_page'] = LLMUsageTracker(data_dir).rows(
                    usage_hours, now=now, limit=int(usage_rows_limit),
                    offset=max(0, int(usage_rows_offset or 0)))
        result['health'] = runtime_health(plugin, now)
    except Exception:
        pass
    return result
