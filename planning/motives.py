"""持续动机层 MotiveStore（PR-B2，2026-11-07；计划书 §5 H3 / §5.3）。

2–4 条短中期个人动机，少而稳定：``{motive_id, topic, source, strength,
created_at, expires_at}``。来源必须是**明确目标**（goals 表 active/paused，
pending_commitment 除外——硬约定走既有机制，不重复成为动机）、**真实观看/
发布结果**（B1 经历索引 observed/published；inferred 只作低权重动机，强度
封顶 :data:`INFERRED_STRENGTH_CAP`）或**状态驱动**（精力低→想休息、压力大→
想放松、社交电量充足→想找人聊聊）。simulated_activity 经历不直接成为动机
（它经 GoalManager 的 simulated_activity 目标间接进入）。

红线（计划书 §5.3，逐条落实）：
- 动机**只改现有候选权重**：消费点 = rolling_planner 候选打分的 motive_bonus
  （≤15% 基础分，:data:`MOTIVE_BONUS_RATIO`）与 spine 提示词变量区 ≤4 条摘要；
  本模块不触碰睡眠/硬约定/注意力/社交预算的任何判定，也不生成消息；
- 状态低允许不作为：无合适候选时动机不强制外化（本模块没有任何外化入口）；
- 同题材重复出现加强熟悉度但有饱和上限（:data:`STRENGTH_SATURATION`）；
  不匹配的候选不受影响，探索概率由既有 softmax 采样保留。

衰减：strength 随时间指数衰减（半衰期 ``[life_state].motive_half_life_days``，
默认 3 天）；到期移除（expires_at = created_at + 3 个半衰期，保守固定值——
规格未给定到期时长，实施者裁量登记 needsReview）；有效强度 < 0.05 也在
refresh 时清除。开关 ``motives_enabled=false`` → active/bonus/summary 全空，
已有文件数据保留只读不删（与经历索引开关口径一致）。

持久化：``motives.json``（core.storage.save_json_atomic 原子写；损坏/缺失
重置空表 + 告警不阻塞）。本模块 0 LLM、不发布事件、不 import 业务模块
（goals/experience 经注入的只读 provider 进入——D5 结构守卫口径）。
"""
from __future__ import annotations

import json
import logging
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.storage import save_json_atomic

from .time_utils import TimezoneManager

logger = logging.getLogger(__name__)

#: 文件结构版本（不认识 → 重置空表 + 告警，不猜着覆盖）
MOTIVES_VERSION = 1
#: 动机条数上限的出厂值与夹取（规格 §3：motive_max_count ge=2 le=4）
MOTIVE_MAX_COUNT_DEFAULT = 4
MOTIVE_MAX_COUNT_MIN = 2
MOTIVE_MAX_COUNT_MAX = 4
#: 半衰期天数出厂值与夹取（规格 §3：motive_half_life_days ge=1 le=14）
MOTIVE_HALF_LIFE_DAYS_DEFAULT = 3
MOTIVE_HALF_LIFE_MIN = 1
MOTIVE_HALF_LIFE_MAX = 14
#: 同题材饱和上限（规格 §2：strength ≤0.8）
STRENGTH_SATURATION = 0.8
#: inferred 来源的强度封顶（规格 §2：inferred 只低权重）
INFERRED_STRENGTH_CAP = 0.3
#: 候选打分加成比例上限（规格 §2：motive_bonus ≤15% 基础分）
MOTIVE_BONUS_RATIO = 0.15
#: 到期 = 创建后 3 个半衰期（保守裁量：强度已衰减到 ≈0.125；规格未给定）
EXPIRE_HALF_LIVES = 3
#: 有效强度低于此值 → refresh 时清除（衰减殆尽不留僵尸动机）
STRENGTH_FLOOR = 0.05
#: 同题材重复出现的加强步长（温和）
REINFORCE_STEP = 0.1
#: spine 提示词摘要条数上限（规格 §2：≤4 条）
SUMMARY_LIMIT_MAX = 4
#: 状态驱动阈值（保守固定值；只产生倾向，不穿透任何判定）
STATE_REST_ENERGY_BELOW = 0.35
STATE_RELAX_STRESS_ABOVE = 0.60
STATE_SOCIAL_BATTERY_ABOVE = 0.70

#: 合法来源类别（规格 §2：goal_id / experience_id / state 驱动）
SOURCE_KINDS = ("goal", "experience", "state")


def _unit(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return max(0.0, min(1.0, number))


def _clip(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


class MotiveStore:
    """短中期动机存储（确定性规则，0 LLM；全部失败静默降级不阻塞主循环）。

    Args:
        path: ``motives.json`` 路径（相对插件数据目录，装配侧给绝对路径）。
        enabled_provider / max_count_provider / half_life_days_provider:
            ``[life_state]`` 三键的只读回调（缺省按出厂默认；读失败保守降级）。
        goals_provider: 返回 goals 表行 dict 列表（明确目标来源；None=无此来源）。
        experience_provider: 返回 B1 经历索引行 dict 列表（真实观看/发布来源；
            None=索引缺失，空降级——规格 §4 测试点）。
        timezone_str / timezone_manager: 时区真源（禁手写本地时间换算）。
    """

    def __init__(self, path: Any, *,
                 enabled_provider: Optional[Callable[[], bool]] = None,
                 max_count_provider: Optional[Callable[[], int]] = None,
                 half_life_days_provider: Optional[Callable[[], int]] = None,
                 goals_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                 experience_provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
                 timezone_str: str = "Asia/Shanghai",
                 timezone_manager: Optional[TimezoneManager] = None) -> None:
        self._path = Path(path)
        self._enabled_provider = enabled_provider
        self._max_count_provider = max_count_provider
        self._half_life_provider = half_life_days_provider
        self._goals_provider = goals_provider
        self._experience_provider = experience_provider
        self._tz = timezone_manager or TimezoneManager(timezone_str)
        self._motives: List[Dict[str, Any]] = []
        self._load()

    # ── 配置读取（失败保守降级）─────────────────────────────────────────
    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return False

    def _max_count(self) -> int:
        value = MOTIVE_MAX_COUNT_DEFAULT
        if self._max_count_provider is not None:
            try:
                value = int(self._max_count_provider())
            except Exception:
                value = MOTIVE_MAX_COUNT_DEFAULT
        return max(MOTIVE_MAX_COUNT_MIN, min(MOTIVE_MAX_COUNT_MAX, value))

    def _half_life_days(self) -> int:
        value = MOTIVE_HALF_LIFE_DAYS_DEFAULT
        if self._half_life_provider is not None:
            try:
                value = int(self._half_life_provider())
            except Exception:
                value = MOTIVE_HALF_LIFE_DAYS_DEFAULT
        return max(MOTIVE_HALF_LIFE_MIN, min(MOTIVE_HALF_LIFE_MAX, value))

    # ── 时间助手 ─────────────────────────────────────────────────────────
    def _now(self, now: Optional[datetime] = None) -> datetime:
        dt = now or self._tz.get_now()
        if dt.tzinfo is None:
            tz = getattr(self._tz, "_tz", None)
            if tz is not None:
                dt = dt.replace(tzinfo=tz)
        return dt

    @staticmethod
    def _parse_ts(raw: Any) -> Optional[datetime]:
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            return datetime.fromisoformat(raw.strip())
        except ValueError:
            return None

    # ── 装载/持久化（损坏重置空表；写失败仅告警）─────────────────────────
    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            logger.warning("动机文件读取/解析失败（重置空表，不阻塞）：%s", exc)
            return
        if not isinstance(raw, dict) or raw.get("version") != MOTIVES_VERSION:
            logger.warning("动机文件结构/版本不认识（重置空表，不阻塞）：%s", self._path)
            return
        rows = raw.get("motives")
        if not isinstance(rows, list):
            return
        clean: List[Dict[str, Any]] = []
        for row in rows[:MOTIVE_MAX_COUNT_MAX * 2]:
            if not isinstance(row, dict):
                continue
            if not _clip(row.get("motive_id"), 80) or not _clip(row.get("topic"), 80):
                continue
            if str(row.get("source_kind") or "") not in SOURCE_KINDS:
                continue
            if self._parse_ts(row.get("created_at")) is None \
                    or self._parse_ts(row.get("expires_at")) is None:
                continue
            clean.append(row)
        self._motives = clean

    def _save(self) -> None:
        document = {"version": MOTIVES_VERSION, "motives": self._motives}
        if not save_json_atomic(self._path, document):
            logger.warning("动机文件写入失败（忽略，内存动机继续生效）：%s", self._path)

    # ── 衰减与有效强度 ───────────────────────────────────────────────────
    def _effective(self, motive: Dict[str, Any], now: datetime) -> float:
        """指数衰减：base_strength × 2^(−经过天数/半衰期)；锚点缺失按 0。"""
        anchor = self._parse_ts(motive.get("reinforced_at")) \
            or self._parse_ts(motive.get("created_at"))
        if anchor is None:
            return 0.0
        try:
            age_days = (now - anchor).total_seconds() / 86400.0
        except TypeError:
            return 0.0
        if age_days < 0:
            age_days = 0.0  # 时钟回拨不放大强度
        base = _unit(motive.get("base_strength"), 0.0)
        return base * (0.5 ** (age_days / float(self._half_life_days())))

    # ── 来源派生（确定性规则；provider 失败空降级）───────────────────────
    def _derive_from_goals(self) -> List[Dict[str, Any]]:
        if self._goals_provider is None:
            return []
        try:
            rows = self._goals_provider() or []
        except Exception as exc:
            logger.debug("动机目标来源读取失败（空降级）：%s", exc)
            return []
        out: List[Dict[str, Any]] = []
        for row in list(rows)[:50]:
            if not isinstance(row, dict):
                continue
            if str(row.get("status") or "") not in ("active", "paused"):
                continue
            goal_type = str(row.get("goal_type") or "")
            if goal_type == "pending_commitment":
                continue  # 硬约定走既有机制，不重复成为动机
            goal_id = _clip(row.get("goal_id"), 160)
            topic = _clip(row.get("name"), 60)
            if not goal_id or not topic:
                continue
            out.append({
                "topic": topic, "source_kind": "goal", "source_ref": goal_id,
                "base_strength": 0.5,
                "goal_types": [goal_type] if goal_type else [],
                "keywords": [topic],
            })
        return out

    def _derive_from_experiences(self) -> List[Dict[str, Any]]:
        if self._experience_provider is None:
            return []  # B1 索引缺失 → 空降级（规格 §4）
        try:
            rows = self._experience_provider() or []
        except Exception as exc:
            logger.debug("动机经历来源读取失败（空降级）：%s", exc)
            return []
        out: List[Dict[str, Any]] = []
        for row in list(rows)[:20]:
            if not isinstance(row, dict):
                continue
            tier = str(row.get("type") or "")
            if tier not in ("observed", "published", "inferred"):
                continue  # simulated_activity 不直接成为动机（经目标间接进入）
            exp_id = _clip(row.get("id"), 160)
            topic = _clip(row.get("summary"), 60)
            if not exp_id or not topic:
                continue
            if tier == "inferred":
                strength = INFERRED_STRENGTH_CAP  # inferred 只低权重（封顶）
            else:
                strength = min(STRENGTH_SATURATION,
                               0.4 + 0.3 * _unit(row.get("salience"), 0.5))
            out.append({
                "topic": topic, "source_kind": "experience", "source_ref": exp_id,
                "base_strength": strength,
                # 经历动机只作提示词灵感，不直接匹配候选类型（保守：不虚构关联）
                "goal_types": [], "keywords": [],
            })
        return out

    @staticmethod
    def _derive_from_state(state: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not isinstance(state, dict):
            return []
        out: List[Dict[str, Any]] = []
        energy = _unit(state.get("energy"), 1.0)
        stress = _unit(state.get("stress"), 0.0)
        social = _unit(state.get("social_battery"), 0.5)
        if energy < STATE_REST_ENERGY_BELOW:
            out.append({"topic": "想好好休息一下", "source_kind": "state",
                        "source_ref": "energy_low", "base_strength": 0.5,
                        "goal_types": ["rest"], "keywords": ["休息"]})
        if stress > STATE_RELAX_STRESS_ABOVE:
            out.append({"topic": "想放松解压", "source_kind": "state",
                        "source_ref": "stress_high", "base_strength": 0.4,
                        "goal_types": ["entertainment", "rest"],
                        "keywords": ["放松", "解压"]})
        if social >= STATE_SOCIAL_BATTERY_ABOVE:
            out.append({"topic": "想找人聊聊", "source_kind": "state",
                        "source_ref": "social_ready", "base_strength": 0.4,
                        "goal_types": ["social"], "keywords": ["聊"]})
        return out

    # ── refresh：衰减/到期/加强/新增/截断（唯一写入口，tick 候选阶段调用）──
    def refresh(self, now: Optional[datetime] = None,
                state: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """刷新动机表并返回当前有效动机（开关关闭 → 空列表，文件不动）。"""
        if not self._enabled():
            return []
        now = self._now(now)
        derived = (self._derive_from_goals() + self._derive_from_experiences()
                   + self._derive_from_state(state))
        by_key: Dict[str, Dict[str, Any]] = {}
        kept: List[Dict[str, Any]] = []
        for motive in self._motives:
            expires = self._parse_ts(motive.get("expires_at"))
            if expires is not None and now >= expires:
                continue  # 到期移除
            if self._effective(motive, now) < STRENGTH_FLOOR:
                continue  # 衰减殆尽
            key = f"{motive.get('source_kind')}:{motive.get('source_ref')}"
            by_key[key] = motive
            kept.append(motive)
        for seed in derived:
            key = f"{seed['source_kind']}:{seed['source_ref']}"
            existing = by_key.get(key)
            if existing is not None:
                # 同题材重复出现 → 加强熟悉度，饱和上限 0.8；不延长到期
                boosted = min(STRENGTH_SATURATION,
                              self._effective(existing, now) + REINFORCE_STEP)
                existing["base_strength"] = round(boosted, 4)
                existing["reinforced_at"] = now.isoformat()
                continue
            created = now
            motive = {
                "motive_id": "motive_" + uuid.uuid4().hex[:16],
                "topic": seed["topic"],
                "source_kind": seed["source_kind"],
                "source_ref": seed["source_ref"],
                "base_strength": round(min(STRENGTH_SATURATION,
                                           _unit(seed["base_strength"], 0.3)), 4),
                "goal_types": list(seed.get("goal_types") or []),
                "keywords": list(seed.get("keywords") or []),
                "created_at": created.isoformat(),
                "reinforced_at": created.isoformat(),
                "expires_at": (created + timedelta(
                    days=EXPIRE_HALF_LIVES * self._half_life_days())).isoformat(),
            }
            by_key[key] = motive
            kept.append(motive)
        # 少而稳定：按有效强度截断到 max_count（同分新者优先）
        kept.sort(key=lambda m: (self._effective(m, now),
                                 str(m.get("created_at") or "")), reverse=True)
        self._motives = kept[:self._max_count()]
        self._save()
        return self.active(now)

    # ── 只读消费口 ───────────────────────────────────────────────────────
    def active(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """当前有效动机（含衰减后 strength；开关关闭 → []；不写盘）。"""
        if not self._enabled():
            return []
        now = self._now(now)
        out: List[Dict[str, Any]] = []
        for motive in self._motives:
            expires = self._parse_ts(motive.get("expires_at"))
            if expires is not None and now >= expires:
                continue
            strength = self._effective(motive, now)
            if strength < STRENGTH_FLOOR:
                continue
            out.append({
                "motive_id": motive.get("motive_id"),
                "topic": motive.get("topic"),
                "source": {"kind": motive.get("source_kind"),
                           "ref": motive.get("source_ref")},
                "strength": round(min(STRENGTH_SATURATION, strength), 4),
                "created_at": motive.get("created_at"),
                "expires_at": motive.get("expires_at"),
                "goal_types": list(motive.get("goal_types") or []),
                "keywords": list(motive.get("keywords") or []),
            })
        out.sort(key=lambda m: m["strength"], reverse=True)
        return out[:self._max_count()]

    def bonus_for(self, candidate: Dict[str, Any],
                  now: Optional[datetime] = None) -> float:
        """候选匹配到的最大加成比例（0 ≤ 返回值 ≤ MOTIVE_BONUS_RATIO）。

        调用方把它乘到**基础分**上（≤15% 基础分）；不匹配的候选返回 0——
        探索概率不受动机垄断。本方法只读，不改任何判定。
        """
        if not self._enabled() or not isinstance(candidate, dict):
            return 0.0
        now = self._now(now)
        goal_type = str(candidate.get("goal_type") or "")
        name = str(candidate.get("name") or "")
        best = 0.0
        for motive in self._motives:
            expires = self._parse_ts(motive.get("expires_at"))
            if expires is not None and now >= expires:
                continue
            types = motive.get("goal_types") or []
            keywords = motive.get("keywords") or []
            matched = (goal_type and goal_type in types) or any(
                isinstance(kw, str) and kw and kw in name for kw in keywords)
            if matched:
                best = max(best, self._effective(motive, now))
        return MOTIVE_BONUS_RATIO * min(STRENGTH_SATURATION, best)

    def summary_lines(self, limit: int = SUMMARY_LIMIT_MAX,
                      now: Optional[datetime] = None) -> List[str]:
        """spine 提示词变量区摘要行（≤4 条；开关关闭/空 → []；只读）。"""
        try:
            limit = max(1, min(SUMMARY_LIMIT_MAX, int(limit)))
        except (TypeError, ValueError):
            limit = SUMMARY_LIMIT_MAX
        lines: List[str] = []
        for motive in self.active(now)[:limit]:
            topic = _clip(motive.get("topic"), 60)
            if topic:
                lines.append(f"- {topic}（强度 {motive['strength']:.2f}）")
        return lines