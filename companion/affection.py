"""好感度管理 v2 —— 状态层（MAINTENANCE_PLAN §5.2/§5.3/§5.5/§5.7）。

镜像情绪系统的三层架构，本文件只承担「状态层 + 纯计算」：
- 状态：``score``（0~100 连续分，初始 30）+ ``baseline``（慢速 EMA 关系基线）+ 档位映射
  （恋人 5 档，含迟滞生效值 ``tier``）+ ``daily_delta``（日累计）+ ``event_log``（当日事件）；
- 规则事件的记账入口在 ``companion/affection_events.py``（频控 + 日累计限幅 + 档位重估）；
- 日衰减（小周期）由日切调用：patrol 日切分支挂 ``apply_daily_decay()``（§5.5）；
- M2 状态：``impressions``（滚动 10 条 / 7 天过期）+ ``tone``（最近一次日结关系温度）
  + ``review``（会话采样每日次数 / 日结尝试日期），写入方是 ``companion/affection_referee.py``；
- 档位变化发布 ``relation.affection_tier_changed``（old/new/score，经 M0 事件总线）。

v1 字段原地保留（§5.3）：``habits``（memory_manager 的底层存储）、``last_user_msg_time``、
``last_speak_time``、``today_speak_count``、``today_date``、``morning/night/miss_sent_today``。
注意 ``today_speak_count`` 只是巡检「预算计数」，与好感分完全无关（§5.4/§5.6），
``increment_speak()`` 等预算方法语义不变。

兼容契约（§八.2）：
- ``level()`` 保持 v1 的 0/1/2 语义（v2 档位经 §5.3 兼容映射折算：0/1→旧0，2/3→旧1，4→旧2），
  既有消费点（relation 标签/后缀池、Tool/命令、companion_get_affection_level API、状态报告）
  零改动即为旧口径；新的 5 档口径走 ``tier()`` / ``tier_label()``。
- JSON 状态文件向后兼容读取 v1 + 一次性迁移（见 ``_build_v2_state``）；JSON 损坏按全新档案
  处理并告警；单用户档案，只服务 target_qq 一人（§5.3；多用户扩展已裁撤，见 §九.1）。

C3 备注：本文件顶部的 ``LEVEL_LABELS`` / ``LEVEL_DESCRIPTIONS`` 死常量按 CLEANUP 约定
原样保留（随后续里程碑清理），此处一行不动。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Tuple

from core.storage import save_json_atomic

logger = logging.getLogger(__name__)

# 各档位备注（注入用短描述）
LEVEL_LABELS = {0: "熟悉", 1: "亲密", 2: "热恋"}

# 各档位完整描述（planner 注入用；来源：源插件 constants.AFFECTION_DESCRIPTIONS）
LEVEL_DESCRIPTIONS = {
    0: "初识阶段，语气温柔但有分寸感，保持适当距离",
    1: "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
    2: "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
}

# ---------------------------------------------------------------------------
# v2 状态层常量（§5.3）
# ---------------------------------------------------------------------------

#: 状态文件 schema 版本（写回即升级）
STATE_VERSION = 2

SCORE_MIN = 0.0
SCORE_MAX = 100.0
#: 全新档案初始分（§5.3「初始 30」；对应 v2 档位 1「熟悉」，兼容口径为旧 0 档）
INITIAL_SCORE = 30.0

#: 恋人预设 5 档名称 / 描述（relation.py 的 tier 标签随关系类型变化，此处为「恋人」基底）
TIER_LABELS = {0: "生疏", 1: "熟悉", 2: "亲密", 3: "热恋", 4: "挚爱"}
TIER_DESCRIPTIONS = {
    0: "初识阶段，语气温柔但有分寸感，保持适当距离",
    1: "熟悉阶段，语气亲昵自然，像好朋友一样聊天",
    2: "亲密阶段，语气甜蜜亲近，会主动分享日常与心情",
    3: "热恋阶段，语气甜蜜亲密，可以肉麻撒娇",
    4: "挚爱阶段，语气完全放松、坦率依赖，把TA当作最重要的人",
}

#: 各档分数区间 ``[下界, 上界)``（档 4 右端闭合）
TIER_BOUNDS = {
    0: (0.0, 20.0),
    1: (20.0, 45.0),
    2: (45.0, 70.0),
    3: (70.0, 88.0),
    4: (88.0, 100.0),
}

#: v2 档位 → v1 三档「对外展示」兼容映射（§5.3：0/1→旧0，2/3→旧1，4→旧2）
TIER_TO_LEGACY_LEVEL = {0: 0, 1: 0, 2: 1, 3: 1, 4: 2}

#: v1 三档 → v2 档位代表档（旧标签 熟悉/亲密/热恋 分别对应 v2 的 1/2/3 档语义；
#: 旧 2 档「热恋」按兼容映射须落 4 档才能对外仍显示为旧 2）
_LEGACY_LEVEL_TO_TIER = {0: 1, 1: 2, 2: 4}

#: v1 三档 → 代表分数（旧 0 档取 §5.3「初始 30」；旧 2 档取 4 档中值 94）
_LEGACY_LEVEL_TO_SCORE = {0: INITIAL_SCORE, 1: 57.5, 2: 94.0}

#: v2 档位 → 代表分数（各档区间中值；set_tier 用）
TIER_REPRESENTATIVE_SCORE = {0: 10.0, 1: 32.5, 2: 57.5, 3: 79.0, 4: 94.0}

#: v1 档位 → 迁移分数（§5.3 迁移表，逐条照办）
V1_LEVEL_TO_MIGRATED_SCORE = {0: 25.0, 1: 55.0, 2: 80.0}

#: 日衰减 / 迟滞默认值（配置缺省时的回退，与 §5.7 默认一致）
DEFAULT_DECAY_PULL = 0.06
DEFAULT_BASELINE_EMA = 0.03
DEFAULT_TIER_HYSTERESIS = 3.0
DEFAULT_TIER_MIN_DWELL_HOURS = 24.0

# ---- M2：滚动印象 / 关系温度（§5.3 v2 schema、§5.4 日结裁判）----
#: impressions 滚动保留条数（§5.3：保留 10 条）
IMPRESSION_KEEP_LIMIT = 10
#: impressions / tone 过期天数（§5.3：7 天过期）
IMPRESSION_EXPIRE_DAYS = 7
#: 单条印象 / 关系温度文本上限（LLM 输出兜底裁剪）
IMPRESSION_TEXT_LIMIT = 80
#: 单次日结最多追加的印象条数（§5.4：0~3 条；裁判已裁剪，此处二次兜底）
IMPRESSION_PER_DAY_LIMIT = 3


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _to_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clamp_score(value: float) -> float:
    return max(SCORE_MIN, min(SCORE_MAX, float(value)))


def _clamp_tier(value: int) -> int:
    return max(0, min(4, int(value)))


def _normalize_legacy_level(value: Any, default: int = 0) -> int:
    """把 v1 档位规整到 0/1/2（非法值回退 default）。"""
    try:
        level = int(value)
    except (TypeError, ValueError):
        level = int(default)
    return max(0, min(2, level))


def tier_for_score(score: float) -> int:
    """分数 → 档位（不含迟滞；§5.3 档位映射表）。"""
    value = _clamp_score(_to_float(score, SCORE_MIN))
    for tier in range(4, -1, -1):
        if value >= TIER_BOUNDS[tier][0]:
            return tier
    return 0


def legacy_level_for_tier(tier: Any) -> int:
    """v2 档位 0~4 → v1 三档 0~2（§5.3 兼容映射；Tool/命令/API 过渡期口径）。"""
    return TIER_TO_LEGACY_LEVEL.get(_clamp_tier(_to_int(tier, 0)), 0)


def _read_json_file(path: Path) -> Tuple[dict, str]:
    """读取状态文件。

    Returns:
        ``(data, status)``，status ∈ {"missing", "ok", "corrupt"}。损坏与缺失区分开，
        以便迁移/告警走不同路径（§5.3：损坏按全新档案处理并告警）。
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, "missing"
    except OSError as exc:
        logger.warning("好感档案读取失败（按全新档案处理）：%s（%s）", path, exc)
        return {}, "corrupt"
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}, "corrupt"
    if not isinstance(data, dict):
        return {}, "corrupt"
    return data, "ok"


def _build_v2_state(raw: Any, default_level: int = 0,
                    *, now: Optional[datetime] = None) -> Tuple[dict, bool]:
    """从（可能为 v1 的）原始 JSON 构造 v2 状态字典。

    迁移规则（§5.3，逐条照办）：读到无 ``version`` 且档案非空时，按 v1 迁移——
    ``score = {0:25, 1:55, 2:80}[affection_level]``、``baseline = score``、``tier = level``，
    其余 v1 字段（habits / 一日标记 / 时间戳 / 预算计数）平铺保留；写回即升级。
    无档案时为全新档案：``score = 30``（旧 0 档）；``default_level`` 非 0 时按旧档
    代表分数落在对应档区间（保持旧口径的 level() 输出不变）。

    Returns:
        ``(state, migrated)``。
    """
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    src: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    migrated = False

    has_version = "version" in src
    legacy_level = _normalize_legacy_level(src.get("affection_level", default_level), default_level)

    if not has_version and "score" not in src:
        if src:
            # ---- v1 → v2 一次性迁移 ----
            score = V1_LEVEL_TO_MIGRATED_SCORE[legacy_level]
            tier = legacy_level  # §5.3：tier = level（随后由迟滞重估接管）
            baseline = score
            migrated = True
        else:
            # ---- 全新档案 ----
            score = _LEGACY_LEVEL_TO_SCORE[legacy_level]
            tier = tier_for_score(score)
            baseline = score
    else:
        # ---- v2 档案（含未来版本：已知字段照读，未知字段原样保留）----
        score = _clamp_score(_to_float(src.get("score"), INITIAL_SCORE))
        tier = _clamp_tier(_to_int(src.get("tier"), tier_for_score(score)))
        baseline = _clamp_score(_to_float(src.get("baseline"), score))

    state: dict[str, Any] = dict(src)
    state.update({
        "version": STATE_VERSION,
        "score": round(_clamp_score(score), 4),
        "baseline": round(_clamp_score(baseline), 4),
        "tier": _clamp_tier(tier),
        "tier_entered_at": str(src.get("tier_entered_at") or now.isoformat()),
        "daily_delta": _normalize_daily_delta(src.get("daily_delta"), today),
        "event_log": _normalize_event_log(src.get("event_log")),
        # ---- M2 字段（§5.3：impressions 为 v2 schema 既有字段，加载时规整）----
        "impressions": _normalize_impressions(src.get("impressions")),
        "tone": _normalize_tone(src.get("tone")),
        "review": _normalize_review_meta(src.get("review"), today),
        # ---- v1 字段原地保留 ----
        # affection_level 只是给旧读方的回滚镜像：迁移时保留原值，其余时候跟随档位兼容口径
        "affection_level": legacy_level if migrated else legacy_level_for_tier(_clamp_tier(tier)),
        "habits": dict(src.get("habits", {}) or {}),
        "today_speak_count": _to_float(src.get("today_speak_count"), 0.0),
        "today_date": str(src.get("today_date", "") or ""),
        "morning_sent_today": bool(src.get("morning_sent_today", False)),
        "night_sent_today": bool(src.get("night_sent_today", False)),
        "miss_sent_today": bool(src.get("miss_sent_today", False)),
        "last_user_msg_time": src.get("last_user_msg_time"),
        "last_speak_time": src.get("last_speak_time"),
    })
    return state, migrated


def _normalize_daily_delta(value: Any, today: str) -> dict[str, Any]:
    data = value if isinstance(value, dict) else {}
    return {
        "date": str(data.get("date") or today),
        "positive": max(0.0, _to_float(data.get("positive"), 0.0)),
        "negative": min(0.0, _to_float(data.get("negative"), 0.0)),
    }


def _normalize_event_log(value: Any) -> list:
    if not isinstance(value, list):
        return []
    entries = []
    for item in value:
        if isinstance(item, dict):
            entries.append(dict(item))
    return entries


def _normalize_impressions(value: Any) -> list:
    """规整 ``impressions``（§5.3 v2 schema）：``[{"date": "YYYY-MM-DD", "text": "..."}]``。"""
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        entries.append({
            "date": str(item.get("date") or ""),
            "text": text[:IMPRESSION_TEXT_LIMIT],
        })
    return entries


def _normalize_tone(value: Any) -> Optional[dict]:
    """规整 ``tone``（最近一次日结产出的一句话关系温度）：``{"date": ..., "text": ...}``。"""
    if not isinstance(value, dict):
        return None
    text = str(value.get("text") or "").strip()
    if not text:
        return None
    return {"date": str(value.get("date") or ""), "text": text[:IMPRESSION_TEXT_LIMIT]}


def _normalize_review_meta(value: Any, today: str) -> dict:
    """规整 M2 裁判计数段（会话采样当日次数 + 日结尝试日期）。"""
    data = value if isinstance(value, dict) else {}
    return {
        "date": str(data.get("date") or today),
        "samples": max(0, _to_int(data.get("samples"), 0)),
        "daily_reviewed_date": str(data.get("daily_reviewed_date") or ""),
    }


def _within_days(date_text: Any, days: int, *, now: Optional[datetime] = None) -> bool:
    """日期字符串是否落在最近 ``days`` 天内（含今天）；解析失败视为过期。"""
    try:
        parsed = datetime.strptime(str(date_text), "%Y-%m-%d")
    except (TypeError, ValueError):
        return False
    today = (now or datetime.now()).date()
    delta_days = (today - parsed.date()).days
    return 0 <= delta_days <= max(0, int(days))


class AffectionManager:
    """好感度状态层（v2）：score/baseline/档位（迟滞）/日累计/事件日志 + v1 字段。"""

    #: 当日事件日志上限（防止刷屏撑爆档案；超出丢弃最旧并告警）
    EVENT_LOG_LIMIT = 300

    def __init__(
        self,
        data_dir: Path,
        default_level: int = 0,
        *,
        config_provider: Any = None,
        decision_logger: Any = None,
        bus: Any = None,
    ) -> None:
        self._path = Path(data_dir) / "affection_memory.json"
        self._dirty = False
        self._last_flush = 0.0
        #: 运行期动态配置来源：可调用对象（返回顶层配置或 [affection] 段）或配置对象本身
        self._config_provider = config_provider
        self._decision_logger = decision_logger
        #: M0 事件总线（可空降级；档位变化时发布 relation.affection_tier_changed）
        self._bus = bus
        #: M3 B2：情绪效价提供者（返回 float 或 None；未接线/情绪停用时乘数恒 1.0）
        self._emotion_vad_provider: Any = None
        self._level_labels: dict = {}

        loaded, status = _read_json_file(self._path)
        if status == "corrupt":
            logger.warning("好感档案 JSON 损坏，已按全新档案处理并重建：%s", self._path)
            loaded = {}
        default_level = _normalize_legacy_level(default_level, 0)
        self._state, migrated = _build_v2_state(loaded, default_level)
        if migrated:
            logger.info(
                "好感档案已从 v1 迁移到 v2（score=%.1f，baseline=%.1f，tier=%d；原档位=%s）",
                self._state["score"], self._state["baseline"], self._state["tier"],
                loaded.get("affection_level"),
            )
            if save_json_atomic(self._path, self._state):
                self._dirty = False
            else:
                self._dirty = True
                logger.warning("好感档案 v1→v2 迁移写回失败，将在下次 flush 重试：%s", self._path)

    # ------------------------------------------------------------------
    # 配置（运行期动态读 [affection] 段：热重载即时生效，缺省回退代码默认值）
    # ------------------------------------------------------------------
    def _affection_cfg(self) -> Any:
        provider = self._config_provider
        if provider is None:
            return None
        try:
            cfg = provider() if callable(provider) else provider
        except Exception:
            return None
        section = getattr(cfg, "affection", None)
        return section if section is not None else cfg

    def _cfg_float(self, name: str, default: float) -> float:
        return _to_float(getattr(self._affection_cfg(), name, default), default)

    def _cfg_bool(self, name: str, default: bool) -> bool:
        value = getattr(self._affection_cfg(), name, default)
        return bool(default if value is None else value)

    def is_enabled(self) -> bool:
        """[affection] enabled：false 时回退 v1 三档行为（不做规则记账/日衰减/迟滞重估）。"""
        return self._cfg_bool("enabled", True)

    def emotion_modulation_enabled(self) -> bool:
        """B2 情绪调制开关（[affection] emotion_modulation，默认开；关时乘数恒 1.0）。"""
        return self._cfg_bool("emotion_modulation", True)

    def set_emotion_vad_provider(self, provider: Any) -> None:
        """注入情绪效价提供者（M3 B2；可空降级）。

        provider 为无参可调用对象，返回当前生效 V（float）或 None（情绪未接线/停用/异常）。
        companion/services.py 装配期注入；未注入时 :meth:`emotion_valence` 恒返回 None，
        好感入账乘数保持 1.0——单模块停用不影响其余模块（§八.2）。
        """
        self._emotion_vad_provider = provider if callable(provider) else None

    def emotion_valence(self) -> Optional[float]:
        """当前生效 V（调制输入，§6.2 B）；未接线/异常时返回 None。"""
        provider = self._emotion_vad_provider
        if not callable(provider):
            return None
        try:
            value = provider()
        except Exception:
            return None
        if value is None:
            return None
        try:
            return max(-1.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return None

    def daily_caps(self) -> dict[str, float]:
        """日累计限幅（§5.5）：positive ≤ +4.0、negative ≥ −3.0（[affection] 配置，缺省即默认）。"""
        return {
            "positive": max(0.0, self._cfg_float("daily_positive_cap", 4.0)),
            "negative": max(0.0, self._cfg_float("daily_negative_cap", 3.0)),
        }

    # ------------------------------------------------------------------
    # 事件总线 / 决策日志
    # ------------------------------------------------------------------
    def set_event_bus(self, bus: Any) -> None:
        """注入 M0 事件总线（可空；为空时档位变化只记日志不发布）。"""
        self._bus = bus

    def log_decision(self, action: str, **fields: Any) -> None:
        """写一条决策日志（未启用/异常时静默，绝不影响状态层）。"""
        logger_ = self._decision_logger
        if logger_ is None:
            return
        try:
            logger_.record(action=action, **fields)
        except Exception:
            pass

    def _publish_tier_changed(self, old: int, new: int, score: float, reason: str = "") -> None:
        """发布 relation.affection_tier_changed（§5.5；总线缺失时静默跳过）。"""
        bus = self._bus
        if bus is None:
            return
        try:
            bus.publish_soon(
                "relation.affection_tier_changed",
                source="companion", old=int(old), new=int(new),
                score=round(float(score), 4), reason=str(reason or ""),
            )
        except Exception:
            pass

    # ---- 读：v2 状态 ----
    def score(self) -> float:
        return _to_float(self._state.get("score"), INITIAL_SCORE)

    def baseline(self) -> float:
        return _to_float(self._state.get("baseline"), self.score())

    def tier(self) -> int:
        """当前生效档位 0~4（含迟滞结果；恋人预设：生疏/熟悉/亲密/热恋/挚爱）。"""
        return _clamp_tier(_to_int(self._state.get("tier"), tier_for_score(self.score())))

    def tier_label(self) -> str:
        """当前 5 档档位名称（「恋人」预设基底；关系定制标签走 ``relation.tier_label(tier)``）。"""
        return TIER_LABELS.get(self.tier(), TIER_LABELS[0])

    def tier_description(self) -> str:
        return TIER_DESCRIPTIONS.get(self.tier(), TIER_DESCRIPTIONS[0])

    def tier_entered_at(self) -> Optional[datetime]:
        return self._parse_dt(self._state.get("tier_entered_at"))

    def daily_delta(self) -> dict[str, Any]:
        return dict(self._state.get("daily_delta", {}) or {})

    def event_log(self) -> list:
        """当日事件日志（副本；M2 日结裁判的输入）。"""
        return [dict(item) for item in (self._state.get("event_log", []) or []) if isinstance(item, dict)]

    # ---- 读：M2 滚动印象 / 关系温度 / 裁判计数 ----
    def impressions(self) -> list[dict]:
        """滚动印象列表（§5.3 v2 schema：``{"date", "text"}``，由关系日结裁判产出）。"""
        return [dict(item) for item in (self._state.get("impressions", []) or [])
                if isinstance(item, dict)]

    def tone(self, *, now: Optional[datetime] = None) -> Optional[dict]:
        """最近一次日结产出的一句话关系温度；无记录或已过 §5.3 的 7 天窗口 → None。"""
        data = _normalize_tone(self._state.get("tone"))
        if data is None:
            return None
        if not _within_days(data.get("date"), IMPRESSION_EXPIRE_DAYS, now=now):
            return None
        return data

    def review_state(self, *, now: Optional[datetime] = None) -> dict:
        """M2 裁判计数段：``{"date", "samples", "daily_reviewed_date"}``。

        ``date`` 跨天自动滚动并清零 ``samples``（会话采样每日上限）；
        ``daily_reviewed_date`` 保留最近一次日结**成功**日期（失败不落，跨重启防重复日结）。
        """
        today = (now or datetime.now()).strftime("%Y-%m-%d")
        data = _normalize_review_meta(self._state.get("review"), today)
        if data["date"] != today:
            data["date"] = today
            data["samples"] = 0
            self._state["review"] = dict(data)
            self._mark_dirty()
        return data

    # ---- 读：v1 兼容口径 ----
    def level(self) -> int:
        """v1 三档口径 0/1/2（§5.3 兼容映射；5 档口径请用 ``tier()``）。"""
        return legacy_level_for_tier(self.tier())

    def level_label(self) -> str:
        labels = getattr(self, "_level_labels", None) or LEVEL_LABELS
        return labels.get(self.level(), labels.get(0, "熟悉"))

    def level_description(self) -> str:
        """当前档位的完整描述（planner 注入用）。"""
        return LEVEL_DESCRIPTIONS.get(self.level(), LEVEL_DESCRIPTIONS[0])

    # ---- 用户习惯（memory_manager 的底层存储）----
    def habits(self) -> dict[str, Any]:
        """获取用户习惯字典。"""
        return dict(self._state.get("habits", {}) or {})

    def set_habit(self, key: str, value: Any) -> None:
        """设置一条用户习惯。"""
        habits = dict(self._state.get("habits", {}) or {})
        habits[str(key)] = value
        self._state["habits"] = habits
        self._mark_dirty()

    def get_habit(self, key: str, default: Any = None) -> Any:
        """获取指定习惯；不存在返回 default。"""
        return self.habits().get(str(key), default)

    def all_habits(self) -> dict[str, Any]:
        """获取所有习惯（同 habits()，别名保持与源插件 MemoryManager 对齐）。"""
        return self.habits()

    def build_personal_context(self) -> str:
        """构造个性化上下文文案（如「用户习惯：wednesday overtime → 周三加班」）。"""
        habits = self.habits()
        if not habits:
            return ""
        parts = [f"用户习惯：{str(k).replace('_', ' ')} → {v}" for k, v in habits.items()]
        return "。".join(parts)

    def today_speak_count(self) -> float:
        """当日巡检预算计数（与好感分无关；§5.4）。"""
        return _to_float(self._state.get("today_speak_count"), 0.0)

    def today_date(self) -> str:
        return str(self._state.get("today_date", "") or "")

    def morning_sent(self) -> bool:
        return bool(self._state.get("morning_sent_today", False))

    def night_sent(self) -> bool:
        return bool(self._state.get("night_sent_today", False))

    def miss_sent_today(self) -> bool:
        return bool(self._state.get("miss_sent_today", False))

    def _parse_dt(self, value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    def last_user_msg_time(self) -> Optional[datetime]:
        return self._parse_dt(self._state.get("last_user_msg_time"))

    def last_speak_time(self) -> Optional[datetime]:
        return self._parse_dt(self._state.get("last_speak_time"))

    # ---- 写：档位（含迟滞重估）----
    def update_level(self, level: int) -> None:
        """v1 兼容入口（Tool/命令/热重载兜底）：把档位设到旧 3 档中的某一档。

        v2 语义 = 把 score 移到该旧档的代表分数、强制重估档位（跳过迟滞），
        保证 ``level()`` 输出与输入一致；5 档直设请用 ``set_tier()``。
        """
        legacy = _normalize_legacy_level(level, self.level())
        self._force_tier(_LEGACY_LEVEL_TO_TIER[legacy], _LEGACY_LEVEL_TO_SCORE[legacy],
                         reason="update_level")

    def set_tier(self, tier: int, *, reason: str = "set_tier") -> None:
        """把 score 移到目标 v2 档区间中值并强制重估档位（跳过迟滞；管理员兜底）。"""
        target = _clamp_tier(_to_int(tier, self.tier()))
        self._force_tier(target, TIER_REPRESENTATIVE_SCORE[target], reason=reason)

    def _force_tier(self, tier: int, score: float, *, reason: str) -> None:
        old = self.tier()
        self._state["score"] = round(_clamp_score(score), 4)
        self._state["tier"] = _clamp_tier(tier)
        self._state["tier_entered_at"] = datetime.now().isoformat()
        self._mark_dirty()
        self.log_decision(
            action="affection.tier_set", trigger_type="affection", reason=str(reason or ""),
            old=old, new=self.tier(), score=self.score(),
        )
        if old != self.tier():
            self._publish_tier_changed(old, self.tier(), self.score(), reason)

    def set_score(self, value: float, *, reason: str = "set_score") -> Optional[Tuple[int, int]]:
        """直接设置 score（越界裁剪）并按迟滞规则重估档位。返回档位变化或 None。"""
        self._state["score"] = round(_clamp_score(_to_float(value, self.score())), 4)
        self._mark_dirty()
        return self.reevaluate_tier(reason=reason)

    def reevaluate_tier(self, *, reason: str = "") -> Optional[Tuple[int, int]]:
        """迟滞判定（§5.3）：升档需 score ≥ 上界 + hysteresis 且满最短停留时长；
        降档需 score ≤ 下界 − hysteresis 且满时长；不满足保持原档并记日志。

        Returns:
            ``(old, new)`` 当档位变化，否则 None。
        """
        if not self.is_enabled():
            return None  # enabled=false：回退 v1 静态档位，不做自动重估
        old = self.tier()
        target = tier_for_score(self.score())
        if target == old:
            return None
        hysteresis = max(0.0, self._cfg_float("tier_hysteresis", DEFAULT_TIER_HYSTERESIS))
        min_dwell = max(0.0, self._cfg_float("tier_min_dwell_hours", DEFAULT_TIER_MIN_DWELL_HOURS))
        if not self._dwell_satisfied(min_dwell):
            self.log_decision(
                action="affection.tier_hold", trigger_type="affection", reason="dwell",
                detail=f"距进入当前档位不足 {min_dwell:g}h，保持档位 {old}（目标 {target}）",
                old=old, target=target, score=self.score(),
            )
            logger.debug("好感档位迟滞：停留时长不足，保持 %d（目标 %d，score=%.1f）",
                         old, target, self.score())
            return None
        if target > old:
            boundary = TIER_BOUNDS[old][1]
            if self.score() < boundary + hysteresis:
                self.log_decision(
                    action="affection.tier_hold", trigger_type="affection", reason="hysteresis",
                    detail=(f"升档需 score ≥ {boundary + hysteresis:.1f}（上界 {boundary:.0f} + "
                            f"迟滞 {hysteresis:g}），当前 {self.score():.1f}"),
                    old=old, target=target, score=self.score(),
                )
                logger.debug("好感档位迟滞：升档边界未过，保持 %d（目标 %d，score=%.1f）",
                             old, target, self.score())
                return None
        else:
            boundary = TIER_BOUNDS[old][0]
            if self.score() > boundary - hysteresis:
                self.log_decision(
                    action="affection.tier_hold", trigger_type="affection", reason="hysteresis",
                    detail=(f"降档需 score ≤ {boundary - hysteresis:.1f}（下界 {boundary:.0f} − "
                            f"迟滞 {hysteresis:g}），当前 {self.score():.1f}"),
                    old=old, target=target, score=self.score(),
                )
                logger.debug("好感档位迟滞：降档边界未过，保持 %d（目标 %d，score=%.1f）",
                             old, target, self.score())
                return None
        self._state["tier"] = target
        self._state["tier_entered_at"] = datetime.now().isoformat()
        self._mark_dirty()
        logger.info("好感档位变化：%d → %d（score=%.1f，原因=%s）",
                    old, target, self.score(), reason or "-")
        self.log_decision(
            action="affection.tier_changed", trigger_type="affection", reason=str(reason or ""),
            old=old, new=target, score=self.score(),
        )
        self._publish_tier_changed(old, target, self.score(), reason)
        return old, target

    def _dwell_satisfied(self, min_hours: float) -> bool:
        if min_hours <= 0:
            return True
        entered = self.tier_entered_at()
        if entered is None:
            return True  # 时间戳缺失（手改档案/旧档）→ 不阻塞重估
        return (datetime.now() - entered).total_seconds() >= min_hours * 3600.0

    # ---- 写：v2 记账 / 日衰减 / 日切 ----
    def commit_rule_delta(self, delta: float) -> float:
        """把已通过频控/限幅的好感增量写进 score 与日累计，返回实际入账值。

        score 触顶/触底时返回可能小于请求值（差额由调用方记日志丢弃）。
        """
        requested = _to_float(delta, 0.0)
        before = self.score()
        after = _clamp_score(before + requested)
        applied = round(after - before, 4)
        if applied == 0.0:
            return 0.0
        self._state["score"] = round(after, 4)
        daily = _normalize_daily_delta(self._state.get("daily_delta"),
                                       datetime.now().strftime("%Y-%m-%d"))
        if applied > 0:
            daily["positive"] = round(daily["positive"] + applied, 4)
        else:
            daily["negative"] = round(min(0.0, daily["negative"] + applied), 4)
        self._state["daily_delta"] = daily
        self._mark_dirty()
        return applied

    def append_event_entry(self, entry: dict) -> None:
        """追加一条当日事件（超上限丢弃最旧；event_log 为 M2 日结裁判的输入）。"""
        if not isinstance(entry, dict):
            return
        log = self._state.setdefault("event_log", [])
        if not isinstance(log, list):
            log = []
            self._state["event_log"] = log
        log.append(dict(entry))
        if len(log) > self.EVENT_LOG_LIMIT:
            overflow = len(log) - self.EVENT_LOG_LIMIT
            del log[:overflow]
            logger.warning("好感事件日志超过上限 %d 条，已丢弃最旧 %d 条", self.EVENT_LOG_LIMIT, overflow)
        self._mark_dirty()

    def clear_event_log(self) -> int:
        """清空当日事件日志（关系日结应用成功后调用，§5.4）。返回清掉的条数。"""
        removed = len(self._state.get("event_log", []) or [])
        self._state["event_log"] = []
        if removed:
            self._mark_dirty()
        return removed

    # ---- 写：M2 滚动印象 / 关系温度 / 裁判计数 ----
    def append_impressions(self, entries: Any, *, date: str = "",
                           now: Optional[datetime] = None) -> list[dict]:
        """追加日结印象并按 §5.3 裁剪：单次 ≤3 条、滚动保留 10 条、7 天过期。

        Args:
            entries: 字符串序列，或 ``{"text", "date"?}`` 序列（缺 date 用 ``date`` 参数）。
            date: 本次印象归属日期（被日结的那一天），缺省今天。
        """
        stamp = str(date or (now or datetime.now()).strftime("%Y-%m-%d"))
        added: list[dict] = []
        for item in list(entries or []):
            if isinstance(item, dict):
                text = str(item.get("text") or "").strip()
                entry_date = str(item.get("date") or stamp)
            else:
                text = str(item or "").strip()
                entry_date = stamp
            if not text:
                continue
            added.append({"date": entry_date, "text": text[:IMPRESSION_TEXT_LIMIT]})
            if len(added) >= IMPRESSION_PER_DAY_LIMIT:
                break  # 每日结 0~3 条（二次兜底）

        merged = self.impressions() + added
        kept = [entry for entry in merged
                if _within_days(entry.get("date"), IMPRESSION_EXPIRE_DAYS, now=now)]
        kept = kept[-IMPRESSION_KEEP_LIMIT:]  # 滚动保留 10 条
        self._state["impressions"] = [dict(entry) for entry in kept]
        if added or kept != merged:
            self._mark_dirty()
        return [dict(entry) for entry in kept]

    def set_tone(self, text: Any, *, date: str = "",
                 now: Optional[datetime] = None) -> Optional[dict]:
        """记录最近一次日结的关系温度（空文本 = 清空）。"""
        content = str(text or "").strip()
        if not content:
            if self._state.get("tone"):
                self._state["tone"] = None
                self._mark_dirty()
            return None
        data = {
            "date": str(date or (now or datetime.now()).strftime("%Y-%m-%d")),
            "text": content[:IMPRESSION_TEXT_LIMIT],
        }
        self._state["tone"] = dict(data)
        self._mark_dirty()
        return dict(data)

    def update_review_state(self, *, samples: Optional[int] = None,
                            daily_reviewed_date: str = "") -> dict:
        """更新 M2 裁判计数段（会话采样当日次数 / 最近一次日结尝试日期）。"""
        data = self.review_state()
        if samples is not None:
            data["samples"] = max(0, _to_int(samples, data.get("samples", 0)))
        if daily_reviewed_date:
            data["daily_reviewed_date"] = str(daily_reviewed_date)
        self._state["review"] = dict(data)
        self._mark_dirty()
        return data

    def apply_daily_decay(self) -> dict[str, Any]:
        """日衰减（小周期，§5.5）：score 向 baseline 回归，随后 baseline 缓慢漂移。

        调用点：日切（patrol 日切分支）。每自然日恰好调用一次；enabled=false 时不动作。
        """
        if not self.is_enabled():
            return {"applied": False, "reason": "disabled"}
        pull = max(0.0, min(1.0, self._cfg_float("decay_pull", DEFAULT_DECAY_PULL)))
        ema = max(0.0, min(1.0, self._cfg_float("baseline_ema", DEFAULT_BASELINE_EMA)))
        before_score, before_base = self.score(), self.baseline()
        new_score = _clamp_score(before_score + (before_base - before_score) * pull)
        new_base = _clamp_score(before_base + (new_score - before_base) * ema)
        self._state["score"] = round(new_score, 4)
        self._state["baseline"] = round(new_base, 4)
        self._mark_dirty()
        changed = self.reevaluate_tier(reason="daily_decay")
        self.log_decision(
            action="affection.daily_decay", trigger_type="affection",
            reason=f"日切衰减 pull={pull:g} baseline_ema={ema:g}",
            score_before=round(before_score, 4), score_after=round(new_score, 4),
            baseline_before=round(before_base, 4), baseline_after=round(new_base, 4),
            tier_before=changed[0] if changed else self.tier(),
            tier_after=self.tier(),
        )
        return {
            "applied": True,
            "score_before": round(before_score, 4), "score_after": round(new_score, 4),
            "baseline_before": round(before_base, 4), "baseline_after": round(new_base, 4),
            "tier_changed": changed,
        }

    def sync_daily(self) -> bool:
        """跨天兜底：把每日计数/日累计/事件日志滚到「今天」，并补一次日衰减。

        日切正常路径是 patrol 日切分支（``reset_daily`` + ``apply_daily_decay``）；
        本方法是事件先于巡检到达（如 00:05 的消息）时的兜底，保证「每自然日恰好一次衰减」。
        返回是否真的发生了跨天滚动。
        """
        today = datetime.now().strftime("%Y-%m-%d")
        daily = self._state.get("daily_delta", {}) or {}
        if self.today_date() == today and str(daily.get("date") or "") == today:
            return False
        self._reset_daily_fields(today)
        self._mark_dirty()
        self.apply_daily_decay()
        return True

    def _reset_daily_fields(self, date: str) -> None:
        self._state["today_date"] = str(date or datetime.now().strftime("%Y-%m-%d"))
        self._state["today_speak_count"] = 0.0
        self._state["morning_sent_today"] = False
        self._state["night_sent_today"] = False
        self._state["miss_sent_today"] = False
        self._state["daily_delta"] = {"date": self._state["today_date"], "positive": 0.0, "negative": 0.0}
        self._state["event_log"] = []

    def reset_daily(self, date: str) -> None:
        """日切：清每日标记与计数 + v2 日累计/事件日志；保留 last_speak_time（跨午夜间隔约束仍生效）。"""
        self._reset_daily_fields(str(date or datetime.now().strftime("%Y-%m-%d")))
        self._mark_dirty()

    # ---- 写：预算计数与 v1 标记（语义不变，与好感分无关）----
    def increment_speak(self, amount: float = 1.0) -> None:
        """巡检预算计数 +amount（好感记账走 record_rule_event，不混入本计数）。"""
        self._state["today_speak_count"] = _to_float(self._state.get("today_speak_count"), 0.0) + _to_float(amount, 0.0)
        self._state["last_speak_time"] = datetime.now().isoformat()
        self._mark_dirty()

    def set_morning_sent(self) -> None:
        self._state["morning_sent_today"] = True
        self._mark_dirty()

    def set_night_sent(self) -> None:
        self._state["night_sent_today"] = True
        self._mark_dirty()

    def set_miss_sent(self) -> None:
        self._state["miss_sent_today"] = True
        self._mark_dirty()

    def update_last_user_msg_time(self, dt: Optional[datetime] = None) -> None:
        """记录用户最后发言时间；顺带做 user_initiated 规则事件检测（§5.4）。

        检测语义：bot 沉默 ≥1h 后对方先开口（本条消息是 bot 上次发言后的第一条用户消息）
        → ``user_initiated``（+0.15，日累计 ≤ +0.5）。检测放在状态层，
        既有 chat Hook（components.py 目标私聊观察）零改动即生效。
        触发点是「对方先开口」而非 patrol 主动发言，故不在 patrol 侧重复补发（避免二次计分）。
        """
        now = dt or datetime.now()
        try:
            from .affection_events import note_user_message

            note_user_message(self, now)
        except Exception as exc:  # 记账失败绝不影响时间戳记录
            logger.debug("user_initiated 检测失败（忽略）：%s", exc)
        self._state["last_user_msg_time"] = now.isoformat()
        self._mark_dirty()

    # ---- 规则事件统一入口（实现见 companion/affection_events.py，§5.4）----
    def record_rule_event(self, kind: str, delta: Optional[float] = None, desc: str = "",
                          *, caps: Any = None, key: str = "") -> dict[str, Any]:
        """记录一条规则事件：频控 → 日累计限幅 → 入账 → 档位重估 → event_log。

        Args:
            kind: 事件类型（M1 表：chat_round/user_initiated/morning_exchange/
                night_exchange/commitment_kept/commitment_broken）。
            delta: 增量；None 时取规则表默认值。
            desc: 事件描述（写入 event_log，供日结裁判引用）。
            caps: 可选的本次调用限幅覆盖（见 affection_events.record_rule_event）。
            key: 去重键（commitment_* 用约定 ID；缺省回退 desc）。
        """
        from .affection_events import record_rule_event as _record

        return _record(self, kind, delta, desc, caps=caps, key=key)

    # ---- 持久化 ----
    def _mark_dirty(self) -> None:
        self._dirty = True
        now = time.monotonic()
        if now - self._last_flush > 1.0:  # 1 秒节流写盘
            self.flush()

    def flush(self) -> None:
        if not self._dirty:
            return
        # v1 读方回滚安全：同步旧档位字段（v2 内部真值仍是 score/tier）
        self._state["affection_level"] = self.level()
        if save_json_atomic(self._path, self._state):
            self._dirty = False
        self._last_flush = time.monotonic()

    # ---- 注入用标签 ----
    def set_level_labels(self, labels: dict) -> None:
        """注入 v1 三档档位名称（companion.relation.RelationProfile）。"""
        self._level_labels = dict(labels or {})

    def status_text(self) -> str:
        labels = getattr(self, "_level_labels", None) or LEVEL_LABELS
        label = labels.get(self.level(), labels.get(0, "熟悉"))
        return (
            f"好感度：{label}（{self.level()}）｜今日主动发言：{self.today_speak_count():g} 次"
            f"｜早安{'已' if self.morning_sent() else '未'}发｜晚安{'已' if self.night_sent() else '未'}发"
            f"｜score {self.score():.1f}／baseline {self.baseline():.1f}"
            f"｜档位 {self.tier()}·{self.tier_label()}"
        )