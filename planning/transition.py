# -*- coding: utf-8 -*-
"""F3 转场拖延（2026-11-11 v2 §4）：纯函数 + 日限持久 + 自我揭露去重。

职责边界（**只读/纯函数层 + 两个小状态仓库，不 import maibot_sdk**）：

- 高反差判定：上一活动 ∈ ``low_focus_types`` → 目标软意图 ∈ ``high_focus_types``；
- 过渡窗时长采样：``window_min..window_max``，按下一硬边界/意图窗口右界夹取；
- 拖延时长采样：``delay_minutes ± delay_jitter_minutes``，越界（贴边界/窗口不足）
  放弃（返回 ``None``，调用方按原路径启动）；
- 名称：确定性生活细节（独立种子，不消耗 planner 主 ``_day_rng``）；
- :class:`TransitionUsageStore`：``data_dir/transition_usage.json`` 日限计数
  （过渡窗 / 拖延各一条），原子写、只在计数变化时落盘，跨 reload 不重置；
- :class:`ConfessionLedger`：replyer 自我揭露按 ``(聊天流, episode_id)`` 去重、
  同流最小间隔、进程内有界（256，按插入序淘汰），重载后最多重复一次（不落盘）。

实验期配置真源是用户可见 ``[experimental].transition_*``（见
``union/20261111_实验配置迁移映射.md`` §2）；本模块的 :data:`DEFAULTS` 与
``core.internal_tuning.INTERNAL_TUNING_DEFAULTS["transition"]``（转正后目标 J 段）
同值同源。运行期**不读** ``mappings/internal_tuning.json`` 的该段。
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from core.internal_tuning import INTERNAL_TUNING_DEFAULTS

from . import settlement_v2

logger = logging.getLogger(__name__)

#: 实验期默认值 / 转正后 ``internal_tuning.json[transition]`` 目标段（16 键）。
#: 键名与 ``[experimental].transition_<key>`` 一一对应（实验期字段加前缀）。
#: 2026-11-12 结构审计批（R13）：不再维护字面副本，唯一真源 =
#: :data:`core.internal_tuning.INTERNAL_TUNING_DEFAULTS` ``["transition"]``
#: （列表值复制一份，防止调用方意外原地修改常量）。
DEFAULTS: Dict[str, Any] = {
    key: (list(value) if isinstance(value, list) else value)
    for key, value in INTERNAL_TUNING_DEFAULTS["transition"].items()
}

#: 过渡 episode 的确定性生活细节（goal_type=transition；不含延续关键词）。
TRANSITION_DISPLAY_DETAILS: Tuple[str, ...] = (
    "起身伸了个懒腰",
    "去接了杯水",
    "在窗边站了一会儿",
    "把桌上的东西挪了挪",
    "慢慢回过神来",
    "发了会儿呆，缓一缓",
)

#: 日限持久文件名（``data_dir/`` 下；与 rolling_event_usage.json 同目录互不干扰）。
USAGE_FILE_NAME = "transition_usage.json"

#: 自我揭露去重表上限（进程内；按插入序淘汰最旧）。
CONFESSION_MAX_ENTRIES = 256


def _clamp_int(value: Any, lo: int, hi: int, default: int) -> int:
    """宽松整数解析 + 夹取；bool/坏值 → default（配置坏值 fail-open 到内置默认）。"""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _clamp_float(value: Any, lo: float, hi: float, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):
        return default
    return max(lo, min(hi, number))


def _types(value: Any, default: Iterable[str]) -> Tuple[str, ...]:
    """goal_type 列表归一（去空、strip、小写、去重保序）；非列表 → 内置默认。"""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return tuple(default)
    out = []
    for item in value:
        if not isinstance(item, str):
            continue
        token = item.strip().lower()
        if token and token not in out:
            out.append(token)
    return tuple(out)


@dataclass(frozen=True)
class TransitionParams:
    """F3 生效参数（不可变；由 ``[experimental]`` section 归一得到）。

    字段默认值全部取自 :data:`DEFAULTS`（→ ``INTERNAL_TUNING_DEFAULTS["transition"]``
    单源），本类不再维护第三份字面副本。
    """

    enabled: bool = DEFAULTS["enabled"]
    window_min: int = DEFAULTS["window_min"]
    window_max: int = DEFAULTS["window_max"]
    procrastination_prob: float = DEFAULTS["procrastination_prob"]
    delay_minutes: int = DEFAULTS["delay_minutes"]
    delay_jitter_minutes: int = DEFAULTS["delay_jitter_minutes"]
    daily_buffer_limit: int = DEFAULTS["daily_buffer_limit"]
    daily_procrastination_limit: int = DEFAULTS["daily_procrastination_limit"]
    low_focus_types: Tuple[str, ...] = tuple(DEFAULTS["low_focus_types"])
    high_focus_types: Tuple[str, ...] = tuple(DEFAULTS["high_focus_types"])
    boundary_margin_minutes: int = DEFAULTS["boundary_margin_minutes"]
    focus_target: float = DEFAULTS["focus_target"]
    focus_tau_min: float = DEFAULTS["focus_tau_min"]
    confess_probability: float = DEFAULTS["confess_probability"]
    confess_min_interval_minutes: int = DEFAULTS["confess_min_interval_minutes"]
    procrastination_event_branch: bool = DEFAULTS["procrastination_event_branch"]

    @classmethod
    def from_section(cls, section: Any) -> "TransitionParams":
        """从 ``[experimental]`` section 读取 ``transition_*``；缺失/坏值 → 内置默认。

        ``section is None``（测试桩/段未落码）→ 全默认（``enabled=False``）。
        """
        if section is None:
            return cls()
        def raw(key: str, default: Any) -> Any:
            return getattr(section, "transition_" + key, default)

        window_min = _clamp_int(raw("window_min", DEFAULTS["window_min"]), 1, 30,
                                DEFAULTS["window_min"])
        window_max = _clamp_int(raw("window_max", DEFAULTS["window_max"]), 1, 60,
                                DEFAULTS["window_max"])
        if window_max < window_min:
            window_max = window_min
        return cls(
            enabled=bool(raw("enabled", DEFAULTS["enabled"])),
            window_min=window_min,
            window_max=window_max,
            procrastination_prob=_clamp_float(
                raw("procrastination_prob", DEFAULTS["procrastination_prob"]),
                0.0, 1.0, DEFAULTS["procrastination_prob"]),
            delay_minutes=_clamp_int(raw("delay_minutes", DEFAULTS["delay_minutes"]),
                                     1, 180, DEFAULTS["delay_minutes"]),
            delay_jitter_minutes=_clamp_int(
                raw("delay_jitter_minutes", DEFAULTS["delay_jitter_minutes"]),
                0, 60, DEFAULTS["delay_jitter_minutes"]),
            daily_buffer_limit=_clamp_int(
                raw("daily_buffer_limit", DEFAULTS["daily_buffer_limit"]),
                0, 20, DEFAULTS["daily_buffer_limit"]),
            daily_procrastination_limit=_clamp_int(
                raw("daily_procrastination_limit", DEFAULTS["daily_procrastination_limit"]),
                0, 20, DEFAULTS["daily_procrastination_limit"]),
            low_focus_types=_types(raw("low_focus_types", DEFAULTS["low_focus_types"]),
                                   DEFAULTS["low_focus_types"]),
            high_focus_types=_types(raw("high_focus_types", DEFAULTS["high_focus_types"]),
                                    DEFAULTS["high_focus_types"]),
            boundary_margin_minutes=_clamp_int(
                raw("boundary_margin_minutes", DEFAULTS["boundary_margin_minutes"]),
                0, 60, DEFAULTS["boundary_margin_minutes"]),
            focus_target=_clamp_float(raw("focus_target", DEFAULTS["focus_target"]),
                                      0.0, 1.0, DEFAULTS["focus_target"]),
            focus_tau_min=_clamp_float(raw("focus_tau_min", DEFAULTS["focus_tau_min"]),
                                       0.5, 120.0, DEFAULTS["focus_tau_min"]),
            confess_probability=_clamp_float(
                raw("confess_probability", DEFAULTS["confess_probability"]),
                0.0, 1.0, DEFAULTS["confess_probability"]),
            confess_min_interval_minutes=_clamp_int(
                raw("confess_min_interval_minutes", DEFAULTS["confess_min_interval_minutes"]),
                0, 1440, DEFAULTS["confess_min_interval_minutes"]),
            procrastination_event_branch=bool(
                raw("procrastination_event_branch", DEFAULTS["procrastination_event_branch"])),
        )


def is_high_focus(goal_type: Any, params: TransitionParams) -> bool:
    return str(goal_type or "").strip().lower() in params.high_focus_types


def is_low_focus(goal_type: Any, params: TransitionParams) -> bool:
    return str(goal_type or "").strip().lower() in params.low_focus_types


def sample_buffer_minutes(params: TransitionParams, rng: random.Random, *,
                          boundary: Optional[int] = None,
                          window_end_remaining: Optional[int] = None) -> Optional[int]:
    """采样过渡窗分钟数；剩余窗口不足最小窗 → ``None``（调用方按原路径启动）。

    ``boundary``：距下一硬边界（睡眠环/硬约定）分钟；``window_end_remaining``：
    candidate 自身 soft 窗口右界的剩余分钟。两者都留 ``boundary_margin_minutes``
    余量，且剩余不足 ``window_min`` 时放弃插入（不把缓冲顶出窗口）。
    """
    lo, hi = params.window_min, max(params.window_min, params.window_max)
    limits = []
    margin = max(0, params.boundary_margin_minutes)
    if boundary is not None:
        limits.append(int(boundary) - margin)
    if window_end_remaining is not None:
        limits.append(int(window_end_remaining) - margin)
    limit = min(limits) if limits else None
    if limit is not None and limit < lo:
        return None
    duration = rng.randint(lo, hi)
    if limit is not None:
        duration = min(duration, limit)
    return max(1, int(duration))


def plan_delay(params: TransitionParams, rng: random.Random, *,
               boundary: Optional[int] = None,
               window_end_remaining: Optional[int] = None) -> Optional[int]:
    """采样拖延分钟数（``delay_minutes ± jitter``）；放不下 → ``None``（放弃拖延）。

    硬边界/soft 窗口右界只留 ``boundary_margin_minutes`` 余量；余量不足
    ``delay_minutes``（意图时长）时直接放弃——不允许把软意图顶出窗口。
    """
    jitter = max(0, int(params.delay_jitter_minutes))
    base = int(params.delay_minutes) + rng.randint(-jitter, jitter)
    base = max(1, base)
    margin = max(0, params.boundary_margin_minutes)
    limits = []
    if boundary is not None:
        limits.append(int(boundary) - margin)
    if window_end_remaining is not None:
        limits.append(int(window_end_remaining) - margin)
    limit = min(limits) if limits else None
    if limit is not None:
        if limit < int(params.delay_minutes):
            return None
        base = min(base, limit)
    if base < 1:
        return None
    return int(base)


def transition_display_name(date: str, minute: int) -> str:
    """过渡 episode 的确定性生活细节名（独立种子，不耗 planner 主 RNG）。"""
    try:
        seed = f"{date}|{int(minute)}|transition_display"
        return TRANSITION_DISPLAY_DETAILS[
            random.Random(seed).randrange(len(TRANSITION_DISPLAY_DETAILS))]
    except (TypeError, ValueError, IndexError):
        return TRANSITION_DISPLAY_DETAILS[0]


class TransitionUsageStore:
    """过渡窗/拖延日限持久计数（``data_dir/transition_usage.json``）。

    - 首次访问按当前日期装载；文件日期不是当天 → 从 0 起算（不沿用昨日额度）；
    - 每次计数变化立刻原子写（文件只有 2 个整数，天然低频；热重载不刷额度）；
    - 无 ``data_dir`` / 读写异常 → 纯内存降级（本进程内仍正确计数，不阻塞调度）。
    """

    def __init__(self, data_dir: Any = None) -> None:
        self._data_dir = data_dir
        self._loaded_date = ""
        self._date = ""
        self._buffer = 0
        self._procrastination = 0

    def _path(self) -> Optional[Path]:
        if not self._data_dir:
            return None
        return Path(self._data_dir) / USAGE_FILE_NAME

    def _load(self, date: str) -> None:
        if self._loaded_date == date:
            return
        self._loaded_date = date
        self._date = date
        self._buffer = 0
        self._procrastination = 0
        path = self._path()
        if path is None:
            return
        try:
            if not path.exists():
                return
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("transition_usage.json 读取失败（按 0 起算）：%s", exc)
            return
        if not isinstance(doc, dict) or str(doc.get("date") or "") != date:
            return
        try:
            self._buffer = max(0, int(doc.get("buffer") or 0))
            self._procrastination = max(0, int(doc.get("procrastination") or 0))
        except (TypeError, ValueError):
            self._buffer = 0
            self._procrastination = 0

    def counts(self, date: str) -> Tuple[int, int]:
        """``(今日过渡窗数, 今日拖延数)``（只读取；不改文件）。"""
        self._load(str(date))
        return self._buffer, self._procrastination

    def allow_buffer(self, date: str, limit: int) -> bool:
        return self.counts(date)[0] < max(0, int(limit))

    def allow_procrastination(self, date: str, limit: int) -> bool:
        return self.counts(date)[1] < max(0, int(limit))

    def note_buffer(self, date: str) -> int:
        self._load(str(date))
        self._buffer += 1
        self._save()
        return self._buffer

    def note_procrastination(self, date: str) -> int:
        self._load(str(date))
        self._procrastination += 1
        self._save()
        return self._procrastination

    def _save(self) -> None:
        path = self._path()
        if path is None:
            return
        doc = {"version": 1, "date": self._date,
               "buffer": self._buffer, "procrastination": self._procrastination}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp",
                                            dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                os.replace(tmp_name, str(path))
            except Exception:
                try:
                    os.unlink(tmp_name)
                except OSError as cleanup_exc:
                    # 清理失败留痕（P2-08 静默失败守卫）；原异常照常上抛。
                    logger.debug("transition_usage.json 临时文件清理失败（原异常照常上抛）：%s",
                                 cleanup_exc)
                raise
        except Exception as exc:
            logger.debug("transition_usage.json 落盘失败（纯内存降级）：%s", exc)


class ConfessionLedger:
    """replyer 自我揭露去重台账（进程内；按 ``(聊天流, episode_id)`` 去重）。

    - 每流两次揭露至少间隔 ``confess_min_interval_minutes``；
    - 去重表上限 :data:`CONFESSION_MAX_ENTRIES`，按插入序淘汰最旧；
    - 不落盘：热重载后同一 episode 最多重复一次（过渡窗 ≤8 分钟，可接受）。
    """

    def __init__(self) -> None:
        self._seen: "OrderedDict[Tuple[str, str], float]" = OrderedDict()
        self._last_at: Dict[str, float] = {}

    def should_confess(self, stream_id: Any, episode_id: Any, *,
                       now_ts: float,
                       probability: float,
                       min_interval_minutes: int,
                       rng: random.Random) -> bool:
        """独立掷骰 + 去重 + 同流最小间隔；命中即记账并返回 True。"""
        stream = str(stream_id or "global")
        episode = str(episode_id or "")
        if not episode:
            return False
        key = (stream, episode)
        if key in self._seen:
            return False
        try:
            probability = float(probability)
            now_value = float(now_ts)
        except (TypeError, ValueError):
            return False
        if not (probability > 0.0):
            return False
        interval = max(0.0, float(min_interval_minutes)) * 60.0
        last = self._last_at.get(stream)
        if interval > 0.0 and last is not None and now_value - last < interval:
            return False
        if rng.random() >= min(1.0, probability):
            return False
        self._seen[key] = now_value
        self._last_at[stream] = now_value
        while len(self._seen) > CONFESSION_MAX_ENTRIES:
            self._seen.popitem(last=False)
        return True

    def seen_count(self) -> int:
        return len(self._seen)

    def has_seen(self, stream_id: Any, episode_id: Any) -> bool:
        return (str(stream_id or "global"), str(episode_id or "")) in self._seen


# ── 唯一结算模型参数接线（R2 结构审计批自组合根逐字下沉；不改历史数值）──────


def transition_focus_override(config: Any) -> Optional[Tuple[float, float]]:
    """F3 过渡窗 focus 覆盖 ``(target, tau_min)``（``[experimental]``；坏值 → None）。

    R2 结构审计批（2026-11-12）：自 ``plugin._transition_focus_override`` 逐字
    下沉——实验期配置只有一个用户可见入口（``[experimental].transition_focus_*``）；
    这里的值经 ``settlement_params`` 注入 ``activity.transition.focus``，
    与 ``[life_state].settlement_params_json`` 的覆盖同路生效（热重载经
    组合根 ``_sync_life_settlement`` 重新推送）。
    """
    exp = getattr(config, "experimental", None)
    if exp is None:
        return None
    try:
        target = float(getattr(exp, "transition_focus_target", 0.68))
        tau = float(getattr(exp, "transition_focus_tau_min", 3.0))
    except (TypeError, ValueError):
        return None
    try:
        if not math.isfinite(target) or not math.isfinite(tau):
            return None
    except Exception:
        return None
    return (max(0.0, min(1.0, target)), max(0.5, tau))


def life_settlement_params(config: Any, warn: Any,
                           logger: Any = None) -> Optional[Dict[str, Any]]:
    """v2 参数覆盖（``[life_state].settlement_params_json``；空/坏值 → None=内置默认）。

    R2 结构审计批（2026-11-12）：自 ``plugin._life_settlement_params`` 逐字下沉。
    返回**已合并**参数 dict（:func:`planning.settlement_v2.merge_params`：未知键
    丢弃并告警、坏值保留默认），装配/热重载两处共用同一口径。

    F3（2026-11-11 B2）：``[experimental].transition_focus_*`` 与
    ``V2_PARAMS.activity.transition.focus`` 默认值 ``[0.68, 3.0]`` 不同时，
    把实验值注入 ``params.activity.transition.focus`` 后返回（此时不再是
    ``None``）；值等于默认时保持 ``None``（旧行为逐字不变）。

    ``warn(message, exc)`` 为组合根注入的装配期容错告警端口；``logger`` 为
    merge_params 的宿主 logger（可空）。
    """
    section = getattr(config, "life_state", None)
    raw = ""
    try:
        raw = str(getattr(section, "settlement_params_json", "") or "").strip()
    except Exception:
        raw = ""
    focus = transition_focus_override(config)
    try:
        default_focus = tuple(settlement_v2.V2_PARAMS["activity"]["transition"]["focus"])
    except Exception:
        default_focus = (0.68, 3.0)
    needs_focus = focus is not None and tuple(focus) != default_focus
    if not raw and not needs_focus:
        return None
    overrides: Optional[Dict[str, Any]] = None
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            warn("settlement_params_json 解析失败（按内置默认处理）：%s", exc)
            parsed = None
        if isinstance(parsed, dict):
            overrides = parsed
        elif parsed is not None:
            warn("settlement_params_json 顶层需为对象（按内置默认处理）：得 %s",
                 type(parsed).__name__)
    try:
        if overrides is None:
            if not needs_focus:
                return None
            params = settlement_v2.default_params()
        else:
            params = settlement_v2.merge_params(overrides, logger)
            if not needs_focus:
                return params
        activity = params.get("activity")
        if isinstance(activity, dict):
            entry = dict(activity.get("transition") or {})
            entry["focus"] = [float(focus[0]), float(focus[1])]
            activity["transition"] = entry
        return params
    except Exception as exc:
        warn("settlement_v2 参数合并失败（按内置默认处理）：%s", exc)
        return None
