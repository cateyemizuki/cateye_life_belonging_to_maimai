"""日程周期四变量状态层（模块名避宿主 ``src/`` 同名：宿主仅存 lifecycle_* 类模块，
``cycle_state`` 无撞名；§9.1 准入依据 = core/mapping_dir.py 同款「先查宿主 src 无同名」纪律）。

冻结口径 = MAINTENANCE_PLAN_10.05.md §31.2（R7 依据 = 用户 2026-10-23 第二轮直派）：

- 状态文件 ``<data_dir>/schedule_cycle_state.json``（**唯一值替换写**）：
  ``previous_cycle_start/end``、``current_cycle_start/end``（epoch 秒）+ ``current_reason``
  + ``anchored`` 标记 + ``updated_at``；
- 定义（Q4 判读，见下「cycle_end 为何并入入睡时刻」）：
  ``cycle_start`` = 该份日程最早条目的开始（≈起床）；
  ``cycle_end`` = **入睡时刻**（§31.1：「晚安期限 = 本轮日程结束时间戳——替代旧
  min(真实入睡锚点, 静默起点)——两项并入 cycle_end 真源」；prompt_builder 无睡眠模式
  「日程止于入睡时刻」同口径）；跨 0 点照取 ``>1440`` 的环节点、不折回（§31.2 保留：
  夜猫子清醒条目与入睡环抬段皆以 ``>1440`` 日帧分钟记录）；
- 换新日程进账 = 本轮→上轮（抛弃）、新→本轮（四种 reason：``tool_edit`` /
  ``regenerate`` / ``scheduled_generate`` / ``fallback_template``；库列另存迁移兜底
  ``legacy_v3``，非四种之一）均由代码盖新时间戳，LLM 不参与；
- 进账校验（Q5 用户定案）：**跨天 rotate（例行生成 scheduled_generate）**：新窗与旧窗
  时间段重叠 >25% 或起始天数跳动 >3 天 = 异常 → 告警并拒绝替换（保旧窗、记决策日志）；
  **同日重排（tool_edit/regenerate/fallback_template）**：跳过 25%/3 天校验（同日天然
  重叠），只做 end>start 环一致性 + 与配置锚点偏差告警（§17 常识窗「只告警不罚」精神；
  偏差容差 = :data:`ANCHOR_DEVIATION_TOLERANCE_MINUTES`，环距跨午夜取短环）；
- 无日程 fail-safe（§31.1）：四变量缺失/非法 → 回退 ``[schedule]`` 两键构造本周期边界
  并打 ``anchored=True``（含按当前时间推今晚），**只在内存构造、不写盘**——真实条目
  一旦进账即以 anchored=False 覆盖；
- 写盘失败 best-effort 不阻断（状态层任何异常都不影响主写库路径，可空降级）。

纯模块边界（§31.2 血缘、§24.2 同款）：只依赖 ``planning.schedule_items``（夜晚睡眠条目
共享口径）与 ``planning.time_utils``（时区/日帧助手），无插件/宿主依赖；时间换算一律经
:data:`day_cycle_epochs` / :func:`format_epoch_clock`（禁止手写 ``//60``、``−1440``）。

【cycle_end 为何并入入睡时刻 — 判读说明】Q4 字面「最后条目的结束」若把夜晚睡眠条目的
跨午夜尾端（23:00→次日 07:00 的 1860）折进 cycle_end，则入睡锚点 = cycle_end 钟点会变成
次日 07:00，与 §31.1 的晚安期限（≈入睡 23:00）及锚点合并映射（入睡 = current_cycle_end、
起床 = current_cycle_start）直接矛盾。故本实现把夜晚睡眠条目贡献为**入睡时刻**（bed =
其入睡钟点；夜猫子入睡钟点早于起床钟点时按同环 +1440 环抬），非睡眠条目的 end 允许
>1440 照取——该裁量在本轮 notes 回报主会话裁定。

decision log 字段（经 ``plugin._decision_logger``，``core.diag.DecisionLogger.record``）：
``action ∈ {info, skip}``、``trigger_type="schedule_cycle"``、``reason=``摘要文本，
``event ∈ {cycle_rotate, cycle_refuse, cycle_refuse_overlap, cycle_refuse_jump,
cycle_warn, cycle_warn_anchor_deviation, cycle_skip_no_envelope, cycle_skip_bad_date}``。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import date as _date, datetime, timedelta
from typing import Any, Iterable, List, Optional, Tuple

from core.time_utils import parse_hhmm

from .schedule_items import is_night_sleep_item
from .time_utils import DAY_MINUTES, TimezoneManager

logger = logging.getLogger(__name__)

#: 状态文件名（data_dir 下）
STATE_FILE = "schedule_cycle_state.json"
#: 库列 ``entry_reason`` 的四种进账原因（§31.2 血缘 FIN.）
REASON_TOOL_EDIT = "tool_edit"
REASON_REGENERATE = "regenerate"
REASON_SCHEDULED = "scheduled_generate"
REASON_FALLBACK = "fallback_template"
#: 迁移兜底值（v3→v4 补戳行；非四种进账原因之一）
REASON_LEGACY = "legacy_v3"

#: 例行生成（跨天 rotate）异常判据（Q5 用户定案）：新窗与旧窗重叠 >25% 或起始天数跳动 >3 天
OVERLAP_MAX_RATIO = 0.25
DAY_JUMP_MAX_DAYS = 3
#: 同日重排「与配置锚点偏差告警」的容差（分钟；§17 常识窗「只告警不罚」精神，本轮 as-built）
ANCHOR_DEVIATION_TOLERANCE_MINUTES = 90


# ============================================================
# 状态对象
# ============================================================
@dataclass
class CycleState:
    """四变量周期状态（epoch 秒；``None`` = 该侧暂无数据）。"""

    previous_cycle_start: Optional[float] = None
    previous_cycle_end: Optional[float] = None
    current_cycle_start: Optional[float] = None
    current_cycle_end: Optional[float] = None
    current_reason: str = ""
    anchored: bool = False
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "previous_cycle_start": self.previous_cycle_start,
            "previous_cycle_end": self.previous_cycle_end,
            "current_cycle_start": self.current_cycle_start,
            "current_cycle_end": self.current_cycle_end,
            "current_reason": self.current_reason,
            "anchored": bool(self.anchored),
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Optional["CycleState"]:
        """病态 JSON → ``None``（文件缺失/损坏/字段非法一律按"无状态"处理）。"""
        if not isinstance(raw, dict):
            return None

        def _num(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if number == number and number > 0 else None

        fields = {key: _num(raw.get(key)) for key in (
            "previous_cycle_start", "previous_cycle_end",
            "current_cycle_start", "current_cycle_end")}
        return cls(
            previous_cycle_start=fields["previous_cycle_start"],
            previous_cycle_end=fields["previous_cycle_end"],
            current_cycle_start=fields["current_cycle_start"],
            current_cycle_end=fields["current_cycle_end"],
            current_reason=str(raw.get("current_reason") or ""),
            anchored=bool(raw.get("anchored")),
            updated_at=str(raw.get("updated_at") or ""),
        )


# ============================================================
# 时间换算（唯一实现；调用方禁手写数值换算）
# ============================================================
def _tzinfo_of(tz_name: str) -> Optional[Any]:
    """``tz_name`` → tzinfo（失败/空串 → ``None`` 按系统本地时区降级；纯模块可空降级）。"""
    name = str(tz_name or "").strip()
    if not name:
        return None
    tz = TimezoneManager(name)._tz
    return tz


def _tz_now(tz_name: str) -> datetime:
    tz = _tzinfo_of(tz_name)
    if tz is not None:
        return datetime.now(tz)
    return datetime.now()


def _epoch_for_date(day: _date, day_minute: int, tz_name: str = "") -> float:
    """日期 + 日帧分钟（允许 >1440 = 次日环节点）→ epoch 秒（时区按 :data:`tz_name`）。"""
    tz = _tzinfo_of(tz_name)
    base = datetime(day.year, day.month, day.day)
    if tz is not None:
        base = base.replace(tzinfo=tz)
    return base.timestamp() + int(day_minute) * 60


def day_cycle_epochs(schedule_date: str, start_minute: Any, end_minute: Any,
                     tz_name: str = "") -> Optional[Tuple[float, float]]:
    """日程日期 + 包络分钟 → ``(cycle_start_epoch, cycle_end_epoch)``。

    ``end_minute`` 允许 ``>1440``（跨 0 点照取 >0 点真实时刻、不折回，§31.2 Q4）；日期
    解析失败/分钟非法 → ``None``（调用方按"本次不轮换"处理）。
    """
    try:
        parts = str(schedule_date or "").split("-")
        day = _date(int(parts[0]), int(parts[1]), int(parts[2]))
        start, end = int(start_minute), int(end_minute)
    except (TypeError, ValueError, IndexError):
        return None
    return (_epoch_for_date(day, start, tz_name), _epoch_for_date(day, end, tz_name))


def format_epoch_clock(epoch: Any, tz_name: str = "") -> str:
    """epoch 秒 → ``HH:MM`` 钟点文本（锚点渲染唯一实现；非法/非正 → 空串交上层兜底）。"""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    tz = _tzinfo_of(tz_name)
    moment = datetime.fromtimestamp(value, tz) if tz is not None else datetime.fromtimestamp(value)
    return f"{moment.hour:02d}:{moment.minute:02d}"


def epoch_clock_minutes(epoch: Any, tz_name: str = "") -> Optional[int]:
    """epoch → 当地钟点分钟 ``[0, 1440)``（偏差告警取数用；非法 → ``None``）。"""
    text = format_epoch_clock(epoch, tz_name)
    if not text:
        return None
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def _calendar_days_between(new_epoch: Any, old_epoch: Any, tz_name: str) -> int:
    """两个 epoch 各自所在地时区的**日历日差**（新起日 − 旧起日）。"""
    tz = _tzinfo_of(tz_name)
    try:
        new_day = datetime.fromtimestamp(float(new_epoch), tz).date()
        old_day = datetime.fromtimestamp(float(old_epoch), tz).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return 0
    return (new_day - old_day).days


# ============================================================
# 包络（四变量候选窗；纯函数）
# ============================================================
def envelope_minutes(items: Iterable[Any]) -> Optional[Tuple[int, int]]:
    """一天日程条目 → 四变量的日帧分钟包络 ``(cycle_start_min, cycle_end_min)``。

    口径（§31.2 Q4 + §31.1 消费语义，见模块 docstring「cycle_end 为何并入入睡时刻」）：

    - ``cycle_start`` = **非睡眠条目**的最早开始（夜猫子 ``day_offset=1`` 行按 +1440 环抬
      到下一帧）；夜晚睡眠条目的入睡钟点不参与 start；
    - ``cycle_end`` = max(非睡眠条目的 end（允许 >1440 照取、不折回），夜晚睡眠条目的
      **入睡钟点** bed：入睡钟点 ≥ wake_floor 时原值；夜猫子入睡钟点早于起床（如
      01:00 ≤ 09:00）时 +1440 环抬到次日入睡)；``day_offset=1`` 清醒行 end 超过 bed 时
      按 bed 收口（时序不越入睡环）；
    - 一天全为睡眠条目 / 无条目 / 全部缺分钟 → ``None``（调用方按"本轮不轮换、盖戳空"
      处理）。
    """
    rows: List[Any] = [item for item in (items or []) if item is not None]

    def _minute(item: Any, name: str) -> Optional[int]:
        value = item.get(name) if isinstance(item, dict) else getattr(item, name, None)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _offset_one(item: Any) -> bool:
        value = item.get("day_offset") if isinstance(item, dict) else getattr(item, "day_offset", None)
        try:
            return int(value) == 1
        except (TypeError, ValueError):
            return False

    awake_starts: List[int] = []
    awake_ends: List[int] = []
    sleep_starts: List[int] = []
    sleep_lifted: List[bool] = []
    for item in rows:
        start = _minute(item, "start_minute")
        if start is None:
            continue
        lifted = bool(_offset_one(item))
        if is_night_sleep_item(item):
            sleep_starts.append(start)
            sleep_lifted.append(lifted)
            continue
        awake_starts.append(start + (DAY_MINUTES if lifted else 0))
        end = _minute(item, "end_minute")
        if end is not None and end > start:
            # day_offset=1 行 raw end = start+时长或收口钟点：+1440 环抬为候选；
            # 若越过 bed 由下方收口（时序不越入睡环）
            awake_ends.append(end + (DAY_MINUTES if lifted else 0))
    if not awake_starts:
        return None
    wake_floor = min(awake_starts)

    beds: List[int] = []
    for start, lifted in zip(sleep_starts, sleep_lifted):
        # 夜猫子入睡钟点早于起床钟点（同一 24h 环的后段）：+1440 环抬到次日入睡
        beds.append(start if start >= wake_floor else start + DAY_MINUTES)
    end_cands = list(awake_ends)
    if beds:
        bed = max(beds)
        end_cands.append(bed)
        end_cands = [min(value, bed) for value in end_cands]
    if not end_cands:
        return None
    end = max(end_cands)
    if end <= wake_floor:
        return None
    return wake_floor, end


# ============================================================
# 读 / 写（唯一值替换写、best-effort）
# ============================================================
def state_path(data_dir: Any) -> Any:
    from pathlib import Path
    return Path(str(data_dir)) / STATE_FILE


def _read_state(data_dir: Any) -> Optional[CycleState]:
    """只读持久化状态（文件缺失/损坏/字段非法 → ``None``；**不**做 anchored 回退构造）。"""
    try:
        path = state_path(data_dir)
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CycleState.from_dict(raw)
    except Exception:
        logger.debug("cycle_state 读取失败（按无状态处理）", exc_info=True)
        return None


def try_load_persisted(data_dir: Any) -> Optional[CycleState]:
    """锚点读路径接口：取出**已持久化**状态；缺失/非法 → ``None``（读取方回现行行解析路径）。"""
    return _read_state(data_dir)


def save(data_dir: Any, state: CycleState, now: Optional[datetime] = None) -> bool:
    """唯一值替换写（临时文件 + ``os.replace`` 原子落盘）；任何失败 best-effort（False、不抛）。"""
    if state is None or data_dir is None:
        return False
    moment = now or _tz_now("")
    payload = state.to_dict()
    payload["updated_at"] = payload["updated_at"] or moment.isoformat(timespec="seconds")
    try:
        path = state_path(data_dir)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(prefix="cycle_", suffix=".tmp", dir=str(directory))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return True
    except Exception:
        logger.debug("cycle_state 写盘失败（best-effort 忽略）", exc_info=True)
        return False


# ============================================================
# 无日程 fail-safe（anchored 回退构造；只构造内存态、不写盘）
# ============================================================
def anchored_state(cfg_wake: str, cfg_sleep: str, tz_name: str = "",
                   now: Optional[datetime] = None) -> CycleState:
    """``[schedule]`` 两键 → anchored=True 回退构造四变量（含按当前时间推今晚，§31.1 fail-safe）。

    - 起床日：钟点 ≥ wake_clock → 今天；否则昨天（当前帧 = F16 新一天起点口径）；
    - 常规作息（入睡晚于起床）：cycle_end = 起床日同日入睡时刻；夜猫子（入睡 ≤ 起床）：
      入睡落到次日（同环 +1 天，防 end≤start 退化）；
    - 上轮（previous）= 同形再退一天；
    - ``[schedule]`` 两键缺省/非法 → ``planning.config.ScheduleSectionConfig`` 出厂锚点
      （7*60 / 23*60 与生成链 ``_normalize_items`` 同源；config 不可导入时取字面量兜底）。
    """
    fallback_wake, fallback_sleep = 7 * 60, 23 * 60
    try:  # 真源 = planning.config 出厂锚点（不复制第二份字面量；失败按上面兜底）
        from .config import ScheduleSectionConfig
        defaults = ScheduleSectionConfig()
        fallback_wake = parse_hhmm(str(getattr(defaults, "wake_time", "") or ""), fallback_wake)
        fallback_sleep = parse_hhmm(str(getattr(defaults, "sleep_time", "") or ""), fallback_sleep)
    except Exception:
        pass
    wake_clock = parse_hhmm(str(cfg_wake or ""), fallback_wake)
    sleep_clock = parse_hhmm(str(cfg_sleep or ""), fallback_sleep)
    moment = now or _tz_now(tz_name)
    clock = moment.hour * 60 + moment.minute
    wake_day = moment.date() if clock >= wake_clock else moment.date() - timedelta(days=1)
    bed_day = wake_day if sleep_clock > wake_clock else wake_day + timedelta(days=1)

    def _pair(day: _date, bed: _date) -> Tuple[float, float]:
        """某帧 ``(cycle_start=该日起床, cycle_end=该日入睡)`` 两侧 epoch。"""
        return (_epoch_for_date(day, wake_clock, tz_name),
                _epoch_for_date(bed, sleep_clock, tz_name))

    cur_start, cur_end = _pair(wake_day, bed_day)
    prev_start, prev_end = _pair(wake_day - timedelta(days=1), bed_day - timedelta(days=1))
    return CycleState(previous_cycle_start=prev_start, previous_cycle_end=prev_end,
                      current_cycle_start=cur_start, current_cycle_end=cur_end,
                      current_reason="", anchored=True)


def load_or_anchored(data_dir: Any, cfg_wake: str, cfg_sleep: str,
                     tz_name: str = "", now: Optional[datetime] = None) -> CycleState:
    """读真源 + 无日程 fail-safe：状态文件有效即原样；缺失/非法/无当前窗 → anchored 构造。"""
    state = _read_state(data_dir)
    if state is not None and state.current_cycle_start is not None \
            and state.current_cycle_end is not None:
        return state
    return anchored_state(cfg_wake, cfg_sleep, tz_name, now)


# ============================================================
# rotate（进账轮换 + 校验）
# ============================================================
@dataclass
class RotationResult:
    """rotate 结果（决策摘要；``applied=False`` = 拒绝替换保旧窗）。"""

    applied: bool
    reason: str = ""
    message: str = ""


def _log_cycle(decision_logger: Any, action: str, reason_text: str, **fields: Any) -> None:
    """决策日志 best-effort（``plugin._decision_logger`` 缺失 → logging；绝不抛）。

    ``core.diag.DecisionLogger.record`` 的顶层 ``reason`` 为结论摘要字段，本层的进账
    原因以 ``entry_reason`` 键传递（避开同名关键字冲突）。
    """
    try:
        if decision_logger is not None:
            payload = {k: v for k, v in fields.items() if k != "reason"}
            decision_logger.record(action=action, trigger_type="schedule_cycle",
                                   reason=reason_text, **payload)
            return
        logger.info("schedule_cycle %s: %s %s", action, reason_text, fields)
    except Exception:
        logger.debug("周期决策日志写入失败", exc_info=True)


def rotate(new_start: Any, new_end: Any, reason: str, *, data_dir: Any,
           cfg_wake: str = "", cfg_sleep: str = "", tz_name: str = "",
           decision_logger: Any = None, now: Optional[datetime] = None) -> RotationResult:
    """新周期进账：本轮→上轮（抛弃）、新→本轮（§31.2）；返回 :class:`RotationResult`。

    - 例行生成（``reason="scheduled_generate"``，跨天 rotate，Q5 用户定案判据）：新窗与
      旧窗重叠 > :data:`OVERLAP_MAX_RATIO` **或** 起始天数跳动 > :data:`DAY_JUMP_MAX_DAYS`
      = 异常 → 告警并**拒绝替换**（保旧窗、记决策日志）；
    - 同日重排（tool_edit/regenerate/fallback_template）：跳过该判据，只做 end>start 环
      一致性 + 与配置锚点偏差告警（§17「只告警不罚」精神）；
    - end≤start 环 / 数值非法：例行 = 一并拒绝替换；同日 = 告警仍轮换（消费侧 loader 有
      ``end>start`` 兜底，不会把病态窗带出）；
    - 状态文件缺失（首次进账/桩）：缺旧窗则跳过重叠/跳天判据，直接记录新轮；
    - **写盘失败 best-effort 不阻断**（``applied`` 仍 = True，message 标注写盘失败）。
    """
    reasons = (REASON_TOOL_EDIT, REASON_REGENERATE, REASON_SCHEDULED, REASON_FALLBACK)
    normalized_reason = str(reason or "").strip()
    if normalized_reason not in reasons:
        normalized_reason = REASON_LEGACY
    cross_day = normalized_reason == REASON_SCHEDULED
    refusal = RotationResult(applied=False, reason=normalized_reason, message="")
    try:
        new_start_value, new_end_value = float(new_start), float(new_end)
    except (TypeError, ValueError):
        new_start_value, new_end_value = None, None

    if (new_start_value is None or new_start_value <= 0
            or new_end_value is None or new_end_value <= 0):
        text = "周期新窗非正/非法：拒绝轮换"
        _log_cycle(decision_logger, "skip", text, event="cycle_refuse", entry_reason=normalized_reason)
        refusal.message = text
        return refusal
    valid_ring = new_end_value > new_start_value

    state = _read_state(data_dir) if data_dir is not None else None
    old_start = state.current_cycle_start if state else None
    old_end = state.current_cycle_end if state else None
    warnings: List[str] = []

    # ① 环一致性（§31.2 同日重排「end 大于 start 环」；例行生成同样不写病态窗）
    if not valid_ring:
        if cross_day:
            text = "新窗 end≤start（跨夜一致性判据未过）：拒绝替换，保旧窗"
            _log_cycle(decision_logger, "skip", text, event="cycle_refuse",
                       entry_reason=normalized_reason)
            refusal.message = text
            return refusal
        text = "新窗 end≤start（跨夜一致性告警，只告警不罚——§17 常识窗精神）：仍按进账轮换"
        _log_cycle(decision_logger, "info", text, event="cycle_warn",
                   entry_reason=normalized_reason)
        warnings.append("新窗 end≤start（已告警）")

    old_window_ok = (old_start is not None and old_end is not None and old_end > old_start)
    # ② 例行生成异常判据（Q5 用户定案）：重叠 >25% 或起始天数跳动 >3 天 → 拒绝替换保旧窗
    if cross_day and old_window_ok:
        overlap = min(new_end_value, old_end) - max(new_start_value, old_start)
        span = new_end_value - new_start_value
        ratio = (overlap / span) if span > 0 else 0.0
        if ratio > OVERLAP_MAX_RATIO:
            text = f"例行生成与既有周期重叠 {ratio:.0%} > 25%：拒绝替换，保旧窗"
            _log_cycle(decision_logger, "skip", text, event="cycle_refuse_overlap",
                       entry_reason=normalized_reason, overlap_ratio=round(ratio, 4))
            refusal.message = text
            return refusal
        jump_days = _calendar_days_between(new_start_value, old_start, tz_name)
        if abs(jump_days) > DAY_JUMP_MAX_DAYS:
            text = f"例行生成起始天数跳动 {jump_days} 天 > 3 天：拒绝替换，保旧窗"
            _log_cycle(decision_logger, "skip", text, event="cycle_refuse_jump",
                       entry_reason=normalized_reason, jump_days=jump_days)
            refusal.message = text
            return refusal

    # ③ 同日重排：与配置锚点偏差告警（§17 只告警不罚；环距跨午夜取短环）
    if not cross_day:
        tolerance = ANCHOR_DEVIATION_TOLERANCE_MINUTES
        anchor_checks = (("起床", cfg_wake, new_start_value, 7 * 60),
                         ("入睡", cfg_sleep, new_end_value, 23 * 60))
        for label, cfg_text, epoch, default_minute in anchor_checks:
            cfg_minute = parse_hhmm(str(cfg_text or ""), default_minute)
            actual = epoch_clock_minutes(epoch, tz_name)
            if actual is None:
                continue
            delta = abs(actual - cfg_minute)
            ring_distance = delta if delta * 2 <= DAY_MINUTES else abs(delta - DAY_MINUTES)
            if ring_distance > tolerance:
                text = (f"同日重排与配置锚点偏差 >{tolerance} 分钟"
                        f"（{label} {format_epoch_clock(epoch, tz_name)} vs 配置 "
                        f"{cfg_minute // 60:02d}:{cfg_minute % 60:02d}）: "
                        "只告警不罚——§17 常识窗精神")
                _log_cycle(decision_logger, "info", text,
                           event="cycle_warn_anchor_deviation", entry_reason=normalized_reason,
                           anchor=label, deviation_minutes=ring_distance)
                warnings.append(f"{label}锚点偏差 {ring_distance} 分钟（已告警）")

    # ④ 轮换写入（本轮→上轮、新→本轮；写盘失败 best-effort）
    next_state = CycleState(
        previous_cycle_start=old_start,
        previous_cycle_end=old_end,
        current_cycle_start=new_start_value,
        current_cycle_end=new_end_value,
        current_reason=normalized_reason,
        anchored=False,
        updated_at=(now or _tz_now(tz_name)).isoformat(timespec="seconds"),
    )
    written = save(data_dir, next_state, now)
    tail = "；".join(warnings)
    tail_text = f"{tail}；" if tail else ""
    if written:
        text = (f"{tail_text}周期轮换：reason={normalized_reason}，"
                f"生效窗 {format_epoch_clock(new_start_value, tz_name)}"
                f"~{format_epoch_clock(new_end_value, tz_name)}")
        _log_cycle(decision_logger, "info", text, event="cycle_rotate", entry_reason=normalized_reason)
        return RotationResult(applied=True, reason=normalized_reason, message=text)
    text = f"{tail_text}周期轮换（四变量已换，写盘失败 best-effort）：reason={normalized_reason}"
    _log_cycle(decision_logger, "info", text, event="cycle_rotate_write_failed",
               entry_reason=normalized_reason)
    return RotationResult(applied=True, reason=normalized_reason, message=text)


def entry_context_from_plugin(plugin: Any, *, fallback_wake: str = "",
                              fallback_sleep: str = "") -> dict:
    """从插件实例读进账上下文（各写库入口共用，避免各自漂移）。

    ``cfg_wake`` / ``cfg_sleep`` = ``[schedule]`` 两键（空串=fail-safe 无配置）；
    ``tz_name`` = ``[schedule_advanced].timezone``。plugin 为 ``None`` / 桩缺属性 →
    全部按空串降级（rotate 内部对空配置取出厂锚点兜底）。
    """
    config = getattr(plugin, "config", None)
    schedule = getattr(config, "schedule", None)
    advanced = getattr(config, "schedule_advanced", None)
    return {
        "cfg_wake": str(getattr(schedule, "wake_time", "") or "") or fallback_wake,
        "cfg_sleep": str(getattr(schedule, "sleep_time", "") or "") or fallback_sleep,
        "tz_name": str(getattr(advanced, "timezone", "") or ""),
    }


# ============================================================
# 进账漏斗（所有写库入口共用；先算包络并 rotate，再由调用方写库盖戳）
# ============================================================
def enter_schedule_cycle(data_dir: Any, schedule_date: str, items: Iterable[Any], *,
                         reason: str, cfg_wake: str = "", cfg_sleep: str = "",
                         tz_name: str = "", decision_logger: Any = None,
                         now: Optional[datetime] = None) -> Optional[Tuple[int, int]]:
    """一种原因的一次日程进账：算包络 → rotate → 返回 ``(cycle_start_min, cycle_end_min)`` 供盖戳。

    返回 ``None`` = 包络不可得（全睡眠/无 awake 条目/缺分钟），调用方按"不轮换、盖戳空"
    处理（db 层存 NULL）。``data_dir`` 为空（桩/缺数据目录）→ 只算包络、不轮换（测试
    桩与理想降级面；行级盖戳不受影响）。
    """
    envelope = envelope_minutes(items)
    if envelope is None:
        if data_dir is not None:
            _log_cycle(decision_logger, "info", "进账包络不可得（无 awake 条目/缺分钟）：本轮不轮换",
                       event="cycle_skip_no_envelope", entry_reason=reason)
        return None
    if data_dir is not None:
        epochs = day_cycle_epochs(schedule_date, envelope[0], envelope[1], tz_name)
        if epochs is None:
            _log_cycle(decision_logger, "info", "进账日期解析失败：本轮不轮换",
                       event="cycle_skip_bad_date", entry_reason=reason)
        else:
            rotate(epochs[0], epochs[1], reason, data_dir=data_dir, cfg_wake=cfg_wake,
                   cfg_sleep=cfg_sleep, tz_name=tz_name, decision_logger=decision_logger,
                   now=now)
    return envelope
