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


def _episode_view(row: Any) -> dict[str, Any]:
    """episode 只读投影 + 跨午夜标记（PR-WEBUI-TIMELINE 1A，3.17.8）。

    ``actual_end_minute>1440`` 的行附 ``crosses_midnight:True``——纯展示标记，
    分钟值逐字不改（前端 minute() 对 >1440 已显示「次日 HH:MM」）。
    """
    view = _row(row)
    end = view.get('actual_end_minute')
    if isinstance(end, (int, float)) and end > 1440:
        view['crosses_midnight'] = True
    return view


#: 单日跨日续段硬上限（1A 体量守卫，实施者择一：续段 ≤8 条硬上限）。续段只是
#: 前一日行的展示投影，8 条足以覆盖真实跨午夜场景；与当日行合并后仍统一按 120 截断。
CROSSDAY_CONTINUATION_CAP = 8


def _crossday_continuations(db: Any, *, prev_day: str, read: Any) -> list[dict[str, Any]]:
    """前一日跨午夜 episode → 当日溢出续段（只读投影；不改归属、不填真实 gap）。

    只取 ``actual_end_minute>1440`` 且 ``actual_start_minute<1440`` 的行
    （active 且 end=None 不算溢出）；投影为 0 起、end-1440 止，``schedule_date``
    保留原日期（前端 idle 合并要求同 schedule_date，续段不会与今日行合并），
    附 ``cross_day_continuation:True``。读失败走既有 read() 降级（warnings+空，不抛）。
    """
    rows = read('前日跨日活动暂不可读', lambda: db.episodes_today(prev_day), [])
    continuations: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        start, end = row.get('actual_start_minute'), row.get('actual_end_minute')
        if not (type(start) in (int, float) and type(end) in (int, float)):
            continue
        if not (end > 1440 and start < 1440):
            continue
        view = _row(row)
        view.update(actual_start_minute=0, actual_end_minute=_number(end - 1440),
                    planned_start_minute=0, planned_end_minute=_number(end - 1440),
                    schedule_date=str(row.get('schedule_date') or prev_day)[:10],
                    cross_day_continuation=True)
        view['time_label'] = ' — '.join(format_day_minute(int(v)) for v in (0, end - 1440))
        continuations.append(view)
    continuations.sort(key=lambda item: item['actual_end_minute'])
    return continuations[:CROSSDAY_CONTINUATION_CAP]


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


#: P4（2026-11-09 §8）：每列展示行上限（超出明确总数与截断标记，不悄悄隐藏）。
POOL_ROW_CAP = 40


def _schedule_pools(planning: Any, model: Any, now: datetime, read: Any,
                    plugin: Any = None) -> dict[str, Any]:
    """Read the existing pools without rolling, refreshing or exposing raw metadata.

    P4（2026-11-09 滚动日程计划 §8/§14.8）：每行带来源标记（origin_kind/source_file/
    definition_version 与 pool_kind 分开）、冷却/时段/状态作用、偏好标签；初始池
    加载失败/部分跳过有简短状态与原因；每列超 POOL_ROW_CAP 明确总数与截断标记。
    纯读统一 Catalog 快照与偏好缓存——查看/hover/翻页不抽选、不消耗、不发请求。
    """
    from .rolling_planner import RANDOM_EVENT_POOL

    prefs = getattr(plugin, '_activity_preferences', None) if plugin is not None else None

    def preference(row: dict) -> Any:
        if prefs is None:
            return None
        try:
            p = prefs.preference_for(row)
        except Exception:
            return None
        aff = _number(p.get('affinity')) or 0.0
        src = str(p.get('source') or '')
        evaluated = bool(p.get('evaluated'))
        # 3.17.33：标签按真实评估状态分档，不再把所有未命中都落到同一个兜底文案。
        # evaluated=True → LLM 逐活动结论；rule_fallback → 人设 traits 的保守匹配
        # （有分但未逐项评估）；neutral → 该项从未评估；persona_changed/cache_corrupt
        # 及未知来源 → 未记录。state 供前端区分『待评估』与『未记录』，不改变
        # 既有 label/source/evaluated 契约。
        if evaluated:
            state = 'evaluated'
        elif src == 'rule_fallback':
            state = 'fallback'
        elif src == 'neutral':
            state = 'pending'
        else:
            state = 'unrecorded'
        if state in ('evaluated', 'fallback'):
            if aff >= 0.35:
                label = '喜欢'
            elif aff <= -0.35:
                label = '不太想做'
            else:
                label = '一般'
        else:
            label = '待评估' if state == 'pending' else '未记录'
        return {'label': label, 'source': src[:24], 'evaluated': evaluated,
                'state': state}

    def events(rows: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in rows[:POOL_ROW_CAP]:
            if not isinstance(row, dict) or not row.get('name'):
                continue
            deltas = row.get('state_deltas') or {}
            item = {
                'name': str(row['name'])[:160], 'goal_type': str(row.get('goal_type') or '')[:40],
                'min_minutes': _number(row.get('min_minutes')),
                'max_minutes': _number(row.get('max_minutes')),
                'event_id': str(row.get('event_id') or row.get('key') or '')[:80],
                'pool_kind': str(row.get('pool_kind') or 'reusable')[:20],
                'origin_kind': str(row.get('origin_kind') or '')[:20],
                'source_file': str(row.get('source_file') or '')[:80],
                'definition_version': str(row.get('definition_version') or '')[:24],
                'cooldown_minutes': _number(row.get('cooldown_minutes')),
                'interruptibility': _number(row.get('interruptibility')),
                'tags': [str(t)[:20] for t in (row.get('tags') or [])][:3],
                'state_deltas': {str(k)[:20]: _number(v) for k, v in deltas.items()
                                 if _number(v) is not None} if isinstance(deltas, dict) else {},
                'generation_id': str(row.get('generation_id') or '')[:96],
                'consumption_status': str((row.get('consumption') or {}).get('status') or '')[:24],
                'consumed_at': str((row.get('consumption') or {}).get('picked_at') or '')[:48],
                'time_slot': _event_slot_label(row)}
            pref = preference(row)
            if pref is not None:
                item['preference'] = pref
            out.append(item)
        return out

    def total(rows: Any) -> int:
        return sum(1 for r in rows if isinstance(r, dict) and r.get('name'))

    catalog = getattr(planning, 'event_catalog', None)
    snapshot = read('小事件库暂不可读', lambda: catalog.store.snapshot, None) if catalog else None
    resume = getattr(model, 'resume_candidates', None)
    paused = read('恢复池暂不可读', lambda: resume(now), []) if callable(resume) else []
    builtin_rows = snapshot.builtin_events if snapshot else RANDOM_EVENT_POOL
    generated_rows = snapshot.reusable_events if snapshot else []
    one_shot_rows = snapshot.one_shot_events if snapshot else []
    consumed_rows = snapshot.consumed_events if snapshot else []
    initial = getattr(catalog, 'initial_events_result', None)
    initial_status = None
    if initial is not None:
        initial_status = {
            'accepted': len(initial.events), 'files_seen': initial.files_seen,
            'disabled': initial.disabled_count, 'dir_exists': initial.dir_exists,
            'content_version': initial.content_version,
            'skipped': [{'file': str(n)[:80], 'reason': str(r)[:120]}
                        for n, r in initial.skipped[:10]]}
    return {
        'recovery': [
            {'name': str(row['name'])[:160], 'goal_type': str(row.get('goal_type') or '')[:40],
             'schedule_date': str(row.get('schedule_date') or '')[:10],
             'progress': _number(row.get('progress'))}
            for row in paused[:20] if isinstance(row, dict) and row.get('name')
        ],
        'builtin': events(builtin_rows),
        'generated': events(generated_rows),
        'one_shot': events(one_shot_rows),
        'consumed': events(consumed_rows),
        'builtin_total': total(builtin_rows),
        'generated_total': total(generated_rows),
        'consumed_total': total(consumed_rows),
        'pool_row_cap': POOL_ROW_CAP,
        'pools_truncated': any(total(r) > POOL_ROW_CAP
                               for r in (builtin_rows, generated_rows, one_shot_rows, consumed_rows)),
        'initial_events': initial_status,
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


def _sleep_bounds(row: Any, midnight: datetime) -> tuple[datetime, datetime | None] | None:
    """sleep_sessions 行 → (绝对起点, 绝对终点)；跨午夜 ``end<start`` 统一 +1440。

    DB 通常直接存 >1440 的 end，但 ``end < start`` 的跨午夜写法也按同一日帧
    规则归一，供 sessions 窗口判定与 estimated_segments 投影共用，避免一边
    保留、一边因 ``segment_end <= segment_start`` 丢弃而出现面板空白带。
    未收口行返回终点 None；缺起点/非法分钟返回 None。
    """
    if not isinstance(row, dict):
        return None
    start_minute = row.get('actual_start_minute')
    if start_minute is None:
        return None
    try:
        start_raw = int(start_minute)
        end_raw = row.get('actual_end_minute')
        end_minutes = None if end_raw is None else int(end_raw)
        if end_minutes is not None and end_minutes < start_raw:
            end_minutes += 1440
        start = midnight + timedelta(minutes=start_raw)
        end = None if end_minutes is None else midnight + timedelta(minutes=end_minutes)
    except (TypeError, ValueError, OverflowError):
        return None
    return start, end


def _sleep_segment(row: Any, midnight: datetime, *, date_text: str,
                   clip_start: datetime, clip_end: datetime) -> dict[str, Any] | None:
    """单条 sleep_sessions 行 → 裁剪到窗口的估算段（未收口/非法行返回 None）。"""
    bounds = _sleep_bounds(row, midnight)
    if bounds is None:
        return None
    session_start, session_end = bounds
    if session_end is None:
        return None  # 未收口（非 completed）→ 不进估算段
    segment_start = max(session_start, clip_start)
    segment_end = min(session_end, clip_end)
    if segment_end <= segment_start:
        return None
    metadata = row.get('metadata') if isinstance(row.get('metadata'), dict) else {}
    wake_reason = str(row.get('wake_reason'))[:40] if row.get('wake_reason') else None
    note = metadata.get('unrecorded_note')
    return {
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
    }


def _sleep_session_in_window(row: Any, midnight: datetime, *,
                             window_start: datetime,
                             window_end: datetime) -> bool:
    """sleep_sessions 行的绝对会话区间是否与窗口重叠（未收口按起点判定）。

    会话起止按 schedule_date 日帧分钟映射；跨午夜 end<start 时补 1440（DB 通常
    直接存 >1440，此处只做防御）。非法/缺开始分钟的行不进入窗口投影。
    """
    if not isinstance(row, dict):
        return False
    bounds = _sleep_bounds(row, midnight)
    if bounds is None:
        return False
    start, end = bounds
    if end is None:
        return start < window_end
    return start < window_end and end > window_start


def _window_sleep_projection(db: Any, *, window_start: datetime,
                             window_end: datetime) -> tuple[
                                 list[dict[str, Any]], list[dict[str, Any]]]:
    """任意窗口内的会话视图 + completed 会话估算段（一次逐日读取）。

    ``sessions`` 覆盖与窗口重叠的**全部**行（含未收口的当前会话），顶层
    ``sleep.sessions`` 不再固定截 8 条；``estimated_segments`` 只含
    ``actual_end_minute`` 非 NULL 的 completed 行（含 ``offline_reconnect``
    离线补全会话，不按 wake_reason 过滤）。会话真实起止按 schedule_date 日帧分钟
    （跨午夜 >1440）映射为绝对时刻后裁剪到窗口；会话不覆盖的剩余大间隙**保持
    空洞**（如实缺口，不伪造）。只读投影：不写任何库/历史；段上的
    ``estimated:true`` 表示「图表估算段」，不代表会话记录本身是估算。多取前一天
    以覆盖跨午夜会话。
    """
    sessions: list[dict[str, Any]] = []
    segments: list[dict[str, Any]] = []
    seen: set = set()
    midnight = datetime.combine(window_start.date() - timedelta(days=1),
                                datetime.min.time()).replace(tzinfo=window_start.tzinfo)
    last_midnight = datetime.combine(window_end.date(),
                                     datetime.min.time()).replace(tzinfo=window_start.tzinfo)
    while midnight <= last_midnight:
        date_text = midnight.date().isoformat()
        try:
            rows = db.query_sleep_sessions(schedule_date=date_text, limit=32)
        except Exception:
            rows = []
        for row in rows if isinstance(rows, list) else []:
            if _sleep_session_in_window(row, midnight, window_start=window_start,
                                        window_end=window_end):
                sessions.append(_sleep_session_view(row))
            segment = _sleep_segment(row, midnight, date_text=date_text,
                                     clip_start=window_start, clip_end=window_end)
            if segment is None:
                continue
            key = (segment['session_id'], segment['start'], segment['end'])
            if key in seen:
                continue
            seen.add(key)
            segments.append(segment)
        midnight += timedelta(days=1)
    sessions.sort(key=lambda row: (row.get('schedule_date') or '',
                                   row.get('actual_start_minute') or 0), reverse=True)
    segments.sort(key=lambda item: item['start'])
    return sessions, segments


def _window_estimated_sleep_segments(db: Any, *, window_start: datetime,
                                     window_end: datetime) -> list[dict[str, Any]]:
    """任意窗口内 completed 会话的估算段（顶层 6/24/72/168h 与按日共用）。

    实现见 :func:`_window_sleep_projection`；此处只返回估算段，保持按日视图
    既有调用面不变。
    """
    return _window_sleep_projection(db, window_start=window_start,
                                    window_end=window_end)[1]


def _estimated_sleep_segments(db: Any, *, day_start: datetime,
                              day_end: datetime) -> list[dict[str, Any]]:
    """单日窗口的睡眠会话估算段（按日视图口径；实现见普通窗口函数）。"""
    return _window_estimated_sleep_segments(db, window_start=day_start,
                                            window_end=day_end)


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
    # 1A（3.17.8）：前一日跨午夜溢出续段插头部（start=0 自然在前）；续段计入
    # 120 体量口径（先合并再截断），自身另有 ≤8 条硬上限（见 _crossday_continuations）。
    # summary.episode_count 仍按当日真实行计（续段是展示投影，不进 KPI）。
    prev_day = (day_date - timedelta(days=1)).isoformat()
    continuations = _crossday_continuations(db, prev_day=prev_day, read=read) if ready else []
    merged_episodes = continuations + [_episode_view(row) for row in episode_rows[:120]]
    display_total = episode_total + len(continuations)
    keep_raw = getattr(getattr(cfg, 'life_state', None), 'keep_episode_days', 14)
    keep_days = keep_raw if type(keep_raw) is int and not isinstance(keep_raw, bool) and keep_raw > 0 else 14
    footprint = {
        'episodes': merged_episodes[:120],
        'total': display_total, 'truncated': display_total > 120,
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
    sleep_segments: list[dict[str, Any]] = []
    if ready and db:
        # 复核修复（3.17.33）：顶层窗口投影不再只取最近 8 条 sessions——
        # 一次逐日读取同时得到窗口内全部会话视图与 completed 会话估算段；
        # 6/24/72/168h 与按日视图共用同一裁剪口径（跨午夜会话多取前一天）。
        window_start = now - timedelta(hours=hours)
        projection = read(
            '睡眠记录暂不可读',
            lambda: _window_sleep_projection(
                db, window_start=window_start, window_end=now), ([], []))
        if (isinstance(projection, tuple) and len(projection) == 2
                and isinstance(projection[0], list)
                and isinstance(projection[1], list)):
            sessions, sleep_segments = projection
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
    # 1A（3.17.8）：今日快照路径与 day 视图同口径——跨午夜原行带 crosses_midnight，
    # 前一日（now.date()-1）溢出续段插头部，合并后按 120 截断（续段 ≤8 硬上限）。
    today_episodes = [_episode_view(row) for row in today.get('episodes', [])[-120:]]
    if ready and db:
        prev_day = (now.date() - timedelta(days=1)).isoformat()
        today_episodes = (_crossday_continuations(db, prev_day=prev_day, read=read)
                          + today_episodes)[:120]
    result = {
        'schema_version': 1, 'generated_at': now.isoformat(), 'timezone': tz_name,
        'window_hours': hours, 'state': safe_state, 'state_available': bool(state),
        'state_age_seconds': age, 'stale': age is None or age > 900 or future_state,
        'history': history,
        'schedule': {'enabled': enabled, 'runtime_ready': ready, 'date': date,
                     'pools': _schedule_pools(ctx.planning, model, now, read, plugin) if ready else {},
                     'current': _row(current) if current else None,
                     'intents': [_row(row) for row in today.get('intents', [])[:80]],
                     'episodes': today_episodes,
                     'summary': {key: _number(today.get('summary', {}).get(key)) for key in
                                 ('intent_count', 'episode_count', 'completed_count', 'interrupted_count', 'abandoned_count')}},
        'sleep': {'current': safe_sleep, 'sessions': sessions,
                  'estimated_segments': sleep_segments,
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
        elif any(len(result.get('schedule', {}).get('pools', {}).get(k, [])) > 8
                 for k in ('builtin', 'generated', 'one_shot', 'consumed')):
            # P4（2026-11-09 §8）：新增来源/偏好字段进入现有裁剪链——池行减半，
            # 总数/截断标记保留（前端据此显示“其余 N 项”不悄悄隐藏）。
            pools = result['schedule']['pools']
            for k in ('builtin', 'generated', 'one_shot', 'consumed'):
                rows = pools.get(k, [])
                if len(rows) > 8:
                    pools[k] = rows[:max(8, len(rows)//2)]
            pools['pools_truncated'] = True
        else:
            # Independent projections must never trap the read handler in a size loop.
            result.update(emotion={'available': False, 'reason': 'payload_limit'},
                          qzone={'available': False, 'reason': 'payload_limit'},
                          qzone_video={'available': False, 'reason': 'payload_limit'},
                          video={'available': False, 'reason': 'payload_limit'},
                          usage={}, health={}, init={'available': False, 'reason': 'payload_limit'})
            if result.get('day') is not None:
                result['day'] = {'available': False, 'reason': 'payload_limit'}
            break
    return result


def _qzone_recent_posts(service: Any, ctx: PluginContext) -> list[dict[str, Any]]:
    """PR-FIXED-CHROME（3.17.15）：空间「最近动态」只读投影（≤5 条已确认发送）。

    数据源 ``PublishLedger.recent_sent(5)``（status=sent 按 created_at 降序，content
    原样（账本最多 5000 字）/created_at/tid/image_count）。来源判定：tid 命中空间视频发布回执
    （``QzoneVideoLedger.recent_attempts(100)`` 的 receipt_tid 集合，既有公开只读面）
    →「视频分享」，否则「日程说说」；视频桥接缺失/读取失败 → 全部「日程说说」降级。
    服务缺失/ledger 异常 → ``[]`` 降级，不抛到读端。只读，不含路径/私聊正文。
    """
    try:
        ledger = getattr(service, '_publish_ledger', None)
        if ledger is None or not hasattr(ledger, 'recent_sent'):
            return []
        rows = ledger.recent_sent(5)
        if not isinstance(rows, list):
            return []
    except Exception:
        return []
    video_tids: set[str] = set()
    try:
        bridge = ctx.qzone_video
        if bridge is not None:
            for attempt in bridge.ledger.recent_attempts(100):
                tid = str(attempt.get('receipt_tid') or '') if isinstance(attempt, dict) else ''
                if tid:
                    video_tids.add(tid)
    except Exception:
        video_tids = set()
    out: list[dict[str, Any]] = []
    for row in rows[:5]:
        if not isinstance(row, dict):
            continue
        tid = str(row.get('tid') or '')
        out.append({'content': str(row.get('content') or ''),
                    'created_at': row.get('created_at'),
                    'tid': tid,
                    'image_count': row.get('image_count', 0),
                    'source': '视频分享' if tid and tid in video_tids else '日程说说'})
    return out


#: 字段 → 数据目录内持久化初始化产物（与 manifest 校验器同口径；只读，分块 bounded）。
_INIT_ARTIFACT_REL = {
    'emotion': 'emotion_config.json',
    'events': 'generated_events.json',
    'state_sleep': 'life_state.json',
    'frequency': None,                       # 频率校准为配置项，无独立数据文件
    'video_interest': 'video_watch/video_interest.json',
    'companion_disposition': 'initiative_state.json',
    'activity_preferences': 'activity_preferences.json',
}

#: 顶层模块（与日志 module 字段后缀首段一致）→ 中文名。模块日志已按 __name__ 区分，
#: 统一日志工具解耦见 union/20261113_模块日志解耦计划书.md（未来实现）。
_MODULE_DEFS = [
    ('planning', '日程规划'), ('emotion', '情绪'), ('companion', '陪伴'),
    ('qzone', '空间'), ('integrations', '联动'), ('core', '核心基础'),
    ('tools', '工具'), ('linkage', '跨插件'),
]
_PLUGIN_MODULE_PREFIX = 'plugins.cateye_life_belonging_to_maimai.'


def _read_init_artifact(path: Any, cap: int = 3500) -> dict[str, Any]:
    """只读 bounded 读取初始化产物原文：完整解析取摘要，预览截断防 payload 膨胀。"""
    from pathlib import Path
    out: dict[str, Any] = {'exists': False, 'bytes': 0, 'preview': '', 'truncated': False, 'doc': None}
    try:
        p = Path(path)
        if not p.exists():
            return out
        raw = p.read_bytes()
        out['exists'] = True
        out['bytes'] = len(raw)
        text = raw.decode('utf-8', errors='replace')
        try:
            doc = json.loads(text)
        except ValueError:
            doc = None
        out['doc'] = doc
        pretty = json.dumps(doc, ensure_ascii=False, indent=2) if doc is not None else text
        out['truncated'] = len(pretty) > cap
        out['preview'] = pretty[:cap]
    except OSError:
        pass
    return out


def _init_field_data(data_dir: Any, plugin_root: Any, field: str, config: Any) -> dict[str, Any]:
    """逐字段读数据目录已记录的初始化产物（不依赖 init 运行，始终可得）。"""
    from pathlib import Path
    empty = {'artifact': None, 'exists': False, 'bytes': 0, 'preview': '', 'truncated': False, 'summary': {}}
    if data_dir is None:
        return empty
    if field == 'frequency':
        sec = getattr(config, 'planning_proactive', None)
        always_one = bool(getattr(sec, 'target_frequency_always_one', True))
        return {'artifact': None, 'exists': True, 'bytes': 0,
                'preview': json.dumps({'target_frequency_always_one': always_one}, ensure_ascii=False, indent=2),
                'truncated': False, 'summary': {'target_frequency_always_one': always_one},
                'note': '频率校准为配置项，无独立数据文件'}
    rel = _INIT_ARTIFACT_REL.get(field)
    if rel is None:
        return empty
    path = Path(data_dir) / rel
    if field == 'events' and not path.exists() and plugin_root:
        alt = Path(plugin_root) / 'mappings' / 'generated_events.json'
        if alt.exists():
            path = alt
    art = _read_init_artifact(path)
    doc = art['doc'] if isinstance(art['doc'], dict) else {}
    summary: dict[str, Any] = {}
    if field == 'emotion':
        params = doc.get('params') or {}
        summary = {'initialized': params.get('initialized'),
                   'persona_digest': str(params.get('persona_digest') or '')[:40],
                   'positive_gain': params.get('positive_gain'), 'negative_decay': params.get('negative_decay')}
    elif field == 'events':
        ev = doc.get('events')
        summary = {'event_count': len(ev) if isinstance(ev, list) else 0}
    elif field == 'state_sleep':
        summary = {'version': doc.get('version'), 'key_count': len(doc), 'sleep_debt': doc.get('sleep_debt')}
    elif field == 'video_interest':
        kw = doc.get('keywords')
        summary = {'keyword_count': len(kw) if isinstance(kw, list) else 0, 'obtained_at': doc.get('obtained_at')}
    elif field == 'companion_disposition':
        prof = doc.get('profile') or {}
        summary = {'version': doc.get('version'), 'anchor_hours': prof.get('anchor_hours'), 'loneliness': prof.get('loneliness')}
    elif field == 'activity_preferences':
        summary = {'schema_version': doc.get('schema_version'),
                   'activity_count': len(doc.get('activities') or {}), 'trait_count': len(doc.get('traits') or {})}
    return {'artifact': rel, 'exists': art['exists'], 'bytes': art['bytes'],
            'preview': art['preview'], 'truncated': art['truncated'], 'summary': summary}


def _module_status(plugin: Any, module: str) -> str:
    cfg = getattr(plugin, 'config', None)
    section = {'planning': 'schedule', 'emotion': 'emotion', 'companion': 'companion',
               'qzone': 'qzone', 'integrations': 'video_watch'}.get(module)
    if section is None:
        return '运行中'
    sec = getattr(cfg, section, None)
    if sec is None:
        return '运行中'
    return '启用' if bool(getattr(sec, 'enabled', True)) else '停用'


def _module_health(plugin: Any, now: datetime) -> dict[str, Any]:
    """各模块状态 + 最近日志报错（bounded 日志尾，按 module 字段聚合；只读）。"""
    from pathlib import Path
    from core.monitoring import tail_records, _log_time
    modules = {name: {'module': name, 'label': label, 'status': _module_status(plugin, name),
                      'error_count': 0, 'last_error_at': None, 'errors': [], 'recent_rows': 0}
               for name, label in _MODULE_DEFS}
    truncated = False
    root = getattr(plugin, '_plugin_root', None)
    if root:
        try:
            folder = Path(root).parent.parent / 'logs'
            files = list(folder.glob('app_*.log.jsonl'))
            if files:
                rows, truncated = tail_records(max(files, key=lambda f: f.stat().st_mtime))
                for row in rows:
                    mod = str(row.get('module') or '')
                    if not mod.startswith(_PLUGIN_MODULE_PREFIX):
                        continue
                    entry = modules.get(mod[len(_PLUGIN_MODULE_PREFIX):].split('.')[0])
                    if entry is None:
                        continue
                    entry['recent_rows'] += 1
                    level = str(row.get('level') or '').lower()
                    if level in ('error', 'critical'):
                        entry['error_count'] += 1
                        at = _log_time(row.get('timestamp'), now)
                        if at:
                            entry['last_error_at'] = at
                        entry['errors'].append({'at': at, 'level': level,
                                                'event': str(row.get('event') or '')[:200]})
                        entry['errors'] = entry['errors'][-6:]   # 保留最近 6 条原文
        except OSError:
            pass
    return {'modules': list(modules.values()), 'truncated': truncated, 'scope': 'bounded_recent_log_tail'}


def _init_summary(plugin: Any, now: datetime) -> dict[str, Any]:
    """「核心」页投影：registry 解析状态（唯一真源）+ 数据目录已记录的初始化产物原文
    + 各模块状态/最近日志报错。只读、零模型调用；不依赖 init 运行（产物始终可得）。
    """
    from pathlib import Path
    try:
        from tools.init.auto_init import FIELD_LABELS, last_report, registry_snapshot
        from tools.init.manifest import INIT_FIELDS
        snap = registry_snapshot(plugin)
        report = last_report(plugin)
    except Exception:
        return {'available': False, 'reason': 'projection_unavailable'}
    reg_path = snap.get('path') or ''
    data_dir = Path(reg_path).parent if reg_path else None
    plugin_root = getattr(plugin, '_plugin_root', None)
    config = getattr(plugin, 'config', None)
    fields_raw = snap.get('fields') or {}
    outcomes = report.get('outcomes') or {}
    fields: list[dict[str, Any]] = []
    initialized_count = 0
    for name in INIT_FIELDS:
        state = fields_raw.get(name) or {}
        initialized = bool(state.get('initialized'))
        if initialized:
            initialized_count += 1
        raw = outcomes.get(name) if isinstance(outcomes.get(name), dict) else None
        fields.append({
            'field': name,
            'label': FIELD_LABELS.get(name, name),
            'initialized': initialized,
            'attempts': int(state.get('attempts') or 0),
            'version': state.get('version'),
            'validated_at': state.get('validated_at'),
            'last_failed_at': state.get('last_failed_at'),
            'last_error': state.get('last_error'),
            'data': _init_field_data(data_dir, plugin_root, name, config),
            'raw': ({
                'status': raw.get('status'),
                'stage_status': raw.get('stage_status'),
                'message': raw.get('message'),
                'errors': [str(e) for e in (raw.get('errors') or [])],
                'details': raw.get('details') or {},
                'validated': raw.get('validated'),
                'run_at': raw.get('run_at'),
            } if raw else None),
        })
    preflight = snap.get('preflight') or {}
    total = len(fields)
    module_health = _module_health(plugin, now)
    return {
        'available': True,
        'plugin_version': snap.get('plugin_version'),
        'registry_path': snap.get('path'),
        'preflight': {'ok': preflight.get('ok'), 'checked_at': preflight.get('checked_at'),
                      'reason': preflight.get('reason')},
        'fields': fields,
        'modules': module_health['modules'],
        'modules_truncated': module_health['truncated'],
        'summary': {'total': total, 'initialized': initialized_count,
                    'pending': total - initialized_count},
        'last_run_at': report.get('generated_at'),
        'last_manual': report.get('manual'),
        'report_available': bool(report.get('available')),
    }


def _extension_summary(plugin: Any, now: datetime, hours: int,
                       usage_hours: int = 720, usage_rows_limit: int = 0,
                       usage_rows_offset: int = 0) -> dict[str, Any]:
    """Independent module projections; never propagate raw event descriptions/logs."""
    result: dict[str, Any] = {'emotion': {'available': False}, 'qzone': {'available': False},
                              'qzone_video': {'available': False}, 'usage': {}, 'health': {},
                              'init': {'available': False}}
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
            # PR-FIXED-CHROME（3.17.15）：娱乐面板「最近动态」只读投影（≤5 条已确认
            # 发送）。独立 try 降级——recent_posts 读取失败不影响 qzone 头部额度芯片。
            result['qzone']['recent_posts'] = _qzone_recent_posts(service, ctx)
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
    # PR-ENTERTAINMENT（3.17.13）：娱乐面板视频信息投影（只读；零模型调用，不触发
    # 兴趣刷新/观看运行态）。recent=VideoWatchService.recent_videos(10)（真实完成
    # ≤15 天且文字缓存有效，service.py 口径）；interests=兴趣文档 keywords 按最终
    # 权重（persona_base + clamp(watch_score, ±50%×base)）排序 top8。文档实际路径
    # data_dir/video_watch/video_interest.json（plugin.py 装配 store_dir=data_dir/
    # "video_watch"；规格 §0 写的 data_dir 直下为笔误，已按代码核实）。读盘经
    # VideoInterestService.load() 公开只读面（损坏/非法→None，不改写用户文件）；
    # 展示口径不做半衰期衰减（衰减属候选排序口径 effective_weights，此处如实展示
    # 落盘分值）。服务缺失/读失败/文档损坏 → 该源降级空；任一源可读 available=True。
    # 3.17.33（S2）：recent 行在既有 bvid/title/watched_at 之上原样透出 S3 约定字段
    # source（tool/autonomous_schedule/autonomous_event，空串=来源未记录）与
    # first_watched_at（epoch 秒，口径同 watched_at）；旧历史行缺字段 → ''/None，
    # 不猜来源、不抛错。前端只读，不再二次加工。
    # 只暴露公开视频标题/BV/观看时间与话题权重，不含路径/私聊正文。
    video: dict[str, Any] = {'available': False, 'recent': [], 'interests': []}
    recent_ok = False
    try:
        watch = getattr(plugin, '_video_watch', None)
        if watch is not None:
            rows = watch.recent_videos(10)
            video['recent'] = [
                {'bvid': str(row.get('bvid') or '')[:20],
                 'title': str(row.get('title') or '')[:120],
                 'watched_at': _number(row.get('watched_at')),
                 'source': str(row.get('source') or '')[:32],
                 'first_watched_at': _number(row.get('first_watched_at'))}
                for row in (rows or [])[:10] if isinstance(row, dict)]
            recent_ok = True
    except Exception:
        video['recent'] = []
    interests_ok = False
    try:
        data_dir = ctx.data_dir
        if data_dir:
            from pathlib import Path
            from integrations.video_interest import (
                PERSONA_BASE_WEIGHT, WATCH_SCORE_CAP_RATIO, VideoInterestService)
            doc = VideoInterestService(Path(data_dir) / 'video_watch').load()
            if doc is not None:
                terms = (doc.get('evidence_weights') or {}).get('terms') or {}
                scored = []
                for keyword in doc.get('keywords') or []:
                    entry = terms.get(str(keyword).lower())
                    entry = entry if isinstance(entry, dict) else None
                    base = entry['persona_base'] if entry else PERSONA_BASE_WEIGHT
                    score = entry['watch_score'] if entry else 0.0
                    # 评审修复（3.17.13）：损坏文档可能含 Infinity/溢出巨值（json.loads
                    # 默认接受 Infinity 字面量），非有限值退默认，避免 inf 权重穿过
                    # json.dumps(allow_nan=False) 炸掉整个 dashboard。
                    if not isinstance(base, (int, float)) or not math.isfinite(base) or base <= 0:
                        base = PERSONA_BASE_WEIGHT
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        score = 0.0
                    cap = base * WATCH_SCORE_CAP_RATIO
                    weight = _number(round(base + max(-cap, min(cap, score)), 3))
                    if weight is None:
                        continue
                    scored.append({'topic': str(keyword)[:40], 'weight': weight})
                scored.sort(key=lambda row: (-row['weight'], row['topic']))
                video['interests'] = scored[:8]
                interests_ok = True
    except Exception:
        video['interests'] = []
    video['available'] = bool(recent_ok or interests_ok)
    result['video'] = video
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
    result['init'] = _init_summary(plugin, now)
    return result
