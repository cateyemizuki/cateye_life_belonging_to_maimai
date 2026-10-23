"""真实作息锚点解析器（实现前冻结口径 = MAINTENANCE_PLAN_10.05 §24.2 任务B；
R7 依据 = 用户 2026-10-21 直派）。

背景：静默窗 / 夜窗 / 晚安期限 / 情绪夜窗此前恒复用 ``[schedule]`` 的
``sleep_time`` / ``wake_time`` **配置锚点**（planning/config.py 出厂 07:00/23:00），
用户今天真实睡到几点只动写库侧、消费侧跟不上。24.2 起消费锚点改为
**真实睡眠环**（schedule.db 夜晚睡眠条目经本模块解析）+ 配置回退，"动态同步口径"。

纯模块边界（§24.2）：只依赖 ``planning.time_utils`` 与 ``planning.schedule_items``，
无插件/宿主依赖，便于对桩直测；today/yesterday 条目集合一律由调用方传入
（勿在此自取数据）；跨午夜判定一律走 :func:`is_in_day_frame_marked` /
:func:`is_in_time_window` / :func:`is_in_sleep_ring` 等 planning.time_utils 日帧
助手（禁止手写 ``//60``、``%60``、``−1440``）。

解析口径（§24.2 冻结原文照抄）：

① 今天与昨天日程各取**夜晚睡眠条目**（``schedule_items.find_night_sleep_item``：
   ``goal_type==sleep`` 或名称含夜间词；A-16 跨夜归属——今晨所属睡眠环可能写在
   **昨天**的日程里）。
② ``now``（当天帧分钟）落在哪条环内（``is_in_day_frame_marked``，昨条优先——
   入睡日的环才是"今晨正在发生"的事实，赖床顺延也是写在入睡日条目上）→ 真实锚点
   = 该条目（start 取钟点原文、end 若跨午夜按 end<start 的 ``HH:MM`` 写法返回，
   消费侧交 :func:`is_in_time_window` 判定）。
③ ``now`` 不在任一睡眠环 → 取今天夜条目（今夜描述），否则昨条（昨夜仍描述
   last night，跨午夜字符串不影响白天判定），否则回退配置锚点。
④ 条目缺 minutes / 非夜晚条目 / 环退化（零时长） → 该候选不可用。

fail-safe（§7.9 契约）：日程缺失 / DB 不可用 / 解析异常由 wiring 侧（companion.patrol
的真源入口）统一兜住，一律回退 ``[schedule]`` 配置锚点（现行行为），**绝不静默回复
成"可打扰"**。

§31.2 锚点合并（Q6 用户裁定，R7 = 用户 2026-10-23 第二轮直派）：读路径改为以**周期四变量**
（``planning/cycle_state.py`` 状态文件 + schedule.db 行级戳）为真源——真实锚点并入新时间戳、
时间戳与锚点一样**进账即更新**（实时生效）。映射：入睡 = ``current_cycle_end``、
起床 = ``current_cycle_start``（:func:`anchors_from_cycle_state`）；fail-safe 回退**现行
配置路径**（下面的条目解析 + 配置兜底原样保留，``real_sleep_anchors`` 原接口语义不变、
保留为兼容门面）；``anchored`` 状态（无日程回退 ``[schedule]`` 两键构造）经 ``source``
= ``config_schedule`` 透传进消费侧 ``source_note``。消费面（静默窗/夜窗/晚安期限/早安
基准）零改动——统一经周期变量取数、自动随真源切换；跨插件 API 与 tools 查询路径不受影响。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional, Tuple

from .schedule_items import find_night_sleep_item
from .time_utils import (DAY_MINUTES, format_minutes_to_time, is_in_day_frame,
                         is_in_day_frame_marked, is_in_sleep_ring,
                         normalize_day_minutes)
from .cycle_state import format_epoch_clock, try_load_persisted

#: 真实锚点来源标记（真实睡眠环 = 实际条目原文）
SOURCE_REAL_SCHEDULE = "real_schedule"
#: 配置锚点来源标记（``[schedule]`` 配置兜底；消费侧静默窗沿用 ``"schedule"`` 字样）
SOURCE_CONFIG_SCHEDULE = "config_schedule"


@dataclass(frozen=True)
class RealAnchors:
    """解析得到的作息锚点 ``(start, end, source)``。

    - ``start`` / ``end``：``HH:MM`` 原文（**未规范化**）。真实锚点时 ``end`` 对
      跨午夜睡眠按 end<start 的钟点写法（如 23:00 入睡 + 赖床到 09:00 ⇒
      ``"23:00"``, ``"09:00"``；常规 8h ⇒ ``"23:00"``, ``"07:00"``），由消费方的
      :func:`planning.time_utils.is_in_time_window` 统一按跨午夜判窗；
    - ``source``：:data:`SOURCE_REAL_SCHEDULE`（真实睡眠环）或
      :data:`SOURCE_CONFIG_SCHEDULE`（``[schedule]`` 配置锚点兜底）。
    """

    start: str
    end: str
    source: str = SOURCE_CONFIG_SCHEDULE


def _clock_minute_of_now(now: Any) -> Optional[int]:
    """``now`` → 当天帧内钟点分钟 ``[0, 1440)``（取不到 → ``None`` 走配置兜底）。

    - ``datetime`` → ``hour*60+minute`` 构造（入口归一，别处不重复写）；
    - 整数 → 直接当作帧内钟点分钟（§24.2 口径原文"now（当天帧分钟）"）返回，
      经 :func:`normalize_day_minutes` 落回 ``[0, 1440)``；
    - 其他形态（``None`` / 空串 / 垃圾数据）→ ``None``。
    """
    if isinstance(now, datetime):
        return normalize_day_minutes(now.hour * 60 + now.minute)
    if isinstance(now, (int, float)):
        return normalize_day_minutes(now)
    return None


def _item_minute(item: Any, key: str) -> Optional[int]:
    """dict 行（schedule_items 表）与对象（Goal 形态）两形态取分钟数字段。

    缺失 / 非数值（§24.2 ④"缺 minutes"）→ ``None``（候选不可用）。
    """
    value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _night_candidate_of(items: Optional[Iterable[Any]]) -> Optional[Any]:
    """一天条目集合里的**夜晚睡眠条目**（委托 ``schedule_items`` 共享判定，§24.2 ①）。

    集合不可迭代 / 全空 → ``None``；命中返回原条目（dict 行或对象原样）。
    """
    try:
        rows = [item for item in (items or []) if item is not None]
    except TypeError:
        return None
    try:
        return find_night_sleep_item(rows)
    except Exception:
        return None


def _item_ring(item: Any) -> Optional[Tuple[int, int]]:
    """夜晚睡眠条目的睡眠环 ``(start, end_frame)``（不可用 → ``None``，§24.2 ④）。

    - ``end_frame`` 允许 ``>1440``（生成链保留的跨午夜写法，如 23:00 + 8h ⇒
      ``1380 → 1860``）；恰等于“ 入睡→午夜”收尾的 ``1440`` 原样保留（归一在展示步）；
    - ``end < start``（赖床写回的"次日起床钟点"写法，``tools_service.nap_lie_in``
      只把 end 推到 ``09:00`` ⇒ ``1380 → 540``）→ 环抬 ``+ DAY_MINUTES`` 成
      ``[start, end+1440)``（同 :func:`is_in_day_frame` 内部的跨午夜环位写法）；
    - ``end == start``（零时长倒挂；§24.2 ④ 病态行）→ ``None``。
    """
    start = _item_minute(item, "start_minute")
    end = _item_minute(item, "end_minute")
    if start is None or end is None:
        return None
    start = normalize_day_minutes(start)
    if end < start:
        end += DAY_MINUTES
    if end <= start:
        return None
    return start, end


def _now_in_item_ring(item: Any, ring: Tuple[int, int], cur: Optional[int]) -> bool:
    """``now`` 是否落在该条目的睡眠环内（跨午夜判定唯一实现 = planning.time_utils）。

    - 常规行（``day_offset ∈ {None, 0}``）：:func:`is_in_day_frame_marked`
      （同一个 24h 环 + A-16 日期帧标记；昨条靠 ``cur+1440`` 环位命中今晨延续）；
    - ``day_offset == 1``（"下一个午夜段"行，自身日期帧恒不进行中）：按钟点环
      :func:`is_in_sleep_ring(cur, wake=归一 end, sleep=归一 start)` 判定——
      ``end=1440``（24:00 收尾）归一成 0 也落在该助手 ``[sleep, 1440) ∪ [0, wake)``
      的语义里，不会因归一漏判；
    - ``cur=None``（now 已不可解析）→ ``False``。
    """
    if cur is None:
        return False
    start, end = ring
    day_offset = item.get("day_offset") if isinstance(item, dict) else getattr(item, "day_offset", None)
    if day_offset == 1:
        end_clock = normalize_day_minutes(end)
        return is_in_sleep_ring(cur, end_clock, normalize_day_minutes(start))
    return is_in_day_frame_marked(start, end, cur, day_offset)


def _anchor_pair_of(item: Any, ring: Tuple[int, int]) -> Optional[Tuple[str, str]]:
    """条目环 → 静默窗用的 ``(入睡 HH:MM, 起床 HH:MM)`` 文本对（退化 → ``None``）。

    两端都经 :func:`format_minutes_to_time(normalize_day_minutes(...))` 渲染：
    ``end > 1440`` 归一成次日钟点（如 1860 → "07:00"），与 ``start`` 比较自然构成
    end<start 的跨午夜写法，片段交给消费侧 :func:`is_in_time_window`。零时长环
    （成环后 end 钟点回到 start，§24.2 ④ 病态）→ ``None``。
    """
    start, end = ring
    start_text = format_minutes_to_time(normalize_day_minutes(start))
    end_text = format_minutes_to_time(normalize_day_minutes(end))
    if end_text == start_text:
        return None
    return start_text, end_text


def anchors_from_cycle_state(state: Any, *, tz_name: str = "") -> Optional[RealAnchors]:
    """周期四变量 → 生效锚点对（§31.2 锚点合并 Q6；真源读取的唯一实现）。

    映射（用户原话「入睡 = current_cycle_end、起床 = current_cycle_start」）：

    - ``RealAnchors.start``（入睡钟点）= ``current_cycle_end`` 的 HH:MM；
    - ``RealAnchors.end``（起床钟点）= ``current_cycle_start`` 的 HH:MM；
    - ``source``：状态文件未打 ``anchored``（真实日程进账轮换）= ``real_schedule``；
      ``anchored=True``（无日程回退 ``[schedule]`` 两键构造）= ``config_schedule``
      ——消费方 ``source_note`` 据此透传「跟随日程作息」。

    病态窗（缺端点 / end≤start / 端点渲染不出钟点 / 钟点对退化同一）→ ``None``（wiring
    侧按"状态不可得"回退现行条目解析路径）。纯 epoch/字符串消费，无 IO。
    """
    if state is None:
        return None
    start_epoch = getattr(state, "current_cycle_start", None)
    end_epoch = getattr(state, "current_cycle_end", None)
    if start_epoch is None or end_epoch is None:
        return None
    try:
        if float(end_epoch) <= float(start_epoch):
            return None
    except (TypeError, ValueError):
        return None
    sleep_text = format_epoch_clock(end_epoch, tz_name)   # 入睡 = cycle_end
    wake_text = format_epoch_clock(start_epoch, tz_name)  # 起床 = cycle_start
    if not sleep_text or not wake_text or sleep_text == wake_text:
        return None
    source = SOURCE_CONFIG_SCHEDULE if bool(getattr(state, "anchored", False)) \
        else SOURCE_REAL_SCHEDULE
    return RealAnchors(start=sleep_text, end=wake_text, source=source)


def real_sleep_anchors(items_today: Any, items_yesterday: Any, *, now: Any,
                       cfg_sleep: str, cfg_wake: str) -> RealAnchors:
    """解析**生效作息锚点**（真实睡眠环优先，配置锚点兜底；§24.2 冻结口径）。

    Args:
        items_today / items_yesterday: 调用方传入的两个日期帧条目集合（含 A-16 跨夜
            归属：今晨的睡眠环可能写在昨天的日程里）。
        now: 当天帧分钟（int/float）或 ``datetime``（取钟点分钟）；不可解析 → 配置兜底。
        cfg_sleep / cfg_wake: ``[schedule].sleep_time`` / ``wake_time`` 原文（兜底时
            **原文透传**，非法由消费方按 fail-safe 留痕）。

    Returns:
        :class:`RealAnchors`：``source = real_schedule``（真实睡眠环）或
        ``config_schedule``（配置锚点）。
    """
    fallback = RealAnchors(start=cfg_sleep, end=cfg_wake, source=SOURCE_CONFIG_SCHEDULE)
    cur = _clock_minute_of_now(now)

    today_item = _night_candidate_of(items_today)
    yesterday_item = _night_candidate_of(items_yesterday)
    if cur is None:
        return fallback

    # ② now 落在哪条环内 → 真实锚点 = 该条目。**昨条优先**：入睡日的环才是"今晨
    #    正在发生"的事实（A-16 跨夜归属；赖床顺延也写在入睡日条目上——今晨起床端
    #    点顺延后，真实锚点随入睡日的顺延值走，不被今天日程里未顺延的今夜条目抢先）。
    for item in (yesterday_item, today_item):
        if item is None:
            continue
        ring = _item_ring(item)
        if ring is not None and _now_in_item_ring(item, ring, cur):
            pair = _anchor_pair_of(item, ring)
            if pair is not None:
                return RealAnchors(start=pair[0], end=pair[1],
                                   source=SOURCE_REAL_SCHEDULE)

    # ③ now 不在任一睡眠环 → 今天夜条目（今夜描述），否则昨条（昨夜仍描述 last night）
    for item in (today_item, yesterday_item):
        if item is None:
            continue
        ring = _item_ring(item)
        if ring is not None:
            pair = _anchor_pair_of(item, ring)
            if pair is not None:
                return RealAnchors(start=pair[0], end=pair[1],
                                   source=SOURCE_REAL_SCHEDULE)

    # 否则（条目缺失/不可用）→ 回退配置锚点（原文透传，非合法性裁定）
    return fallback
