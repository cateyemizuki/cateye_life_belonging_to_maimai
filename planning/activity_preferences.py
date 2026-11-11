# -*- coding: utf-8 -*-
"""人设驱动的日程偏好服务（P3，2026-11-09 滚动日程计划 §14）。

四层分开（§14.2）：本服务只管**稳定人设倾向**与**逐活动喜好**两层；当前意愿
（状态适配）仍在 planner 打分公式，临时动机/厌倦仍归 MotiveStore 与新颖度项。

红线（§14.1/§14.4/§14.5）：

- 人设真源 = 宿主 ``personality.personality``（注入只读 provider，与 LLMService
  身份前缀同源）；不读用户私聊、不从陪伴目标推断 bot 性格。
- 普通滚动 tick / 页面读取 / hover / 预览 / 翻页 / 热重载 **不发请求**；
  ``preference_for`` 是纯读口。请求只在首次启用、显式初始化/管理员重建、
  人设变化、日意图生成后合并安排（``ensure_evaluated``）；事件库 reload/
  refresh 不直接触发，新定义由下一次日意图/初始化合并（3.17.33 复核项
  PREF-EVENT-REFRESH-TRIGGER 选择「注释对齐实际调用点」，不改事件库路径）。
- 统一走 ``LLMService.generate_detailed(module='planning', task_name='utils',
  with_identity=True)``，独立 event 标签；每本地日 ≤4 次（含发出后失败），
  单批 ≤24（容量未知先 12），超时 30s，失败退避 ≥30min；不设 max_tokens 覆盖。
- 批次异步单飞；缓存未就绪按规则/中性运行，不阻塞睡眠/活动结束/下一项选取；
  正式睡眠/硬约定/回复生成中（busy provider）推迟自动增量；推迟/日限/退避/
  失败把剩余项（含仅画像待评估）持久化到 ``pending`` 并写决策日志，清醒拍
  （life tick 尾部）自动续排，不静默丢弃。
- 语义身份 = 名称+类型+标签（不含随机 item_id/日期/窗口钟点/当前数值）；
  恢复/改计划/跨日延续映射回原语义；event_id 只是别名。
- 人设改变 → 旧评分立即停止参与（persona_hash 不符 → 中性），新画像评估前
  不沿用旧口味；单项定义改变只失效该项；输出越界/布尔冒充数字/未知重复 key
  拒绝相应记录，合法部分可发布；旧响应晚到先复核 persona/descriptor 哈希。
- 缓存损坏原件留证（.corrupt 备份）后中性降级，不在读取方法中顺手覆盖原件；
  热重载恢复缓存/退避/已发送次数；查看诊断不清空计数。
- 不 import companion/emotion/video 实现；不写 config.toml/用户 JSON/生成库。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.storage import save_json_atomic

logger = logging.getLogger(__name__)

PREFERENCES_FILE_NAME = "activity_preferences.json"
SCHEMA_VERSION = 1
SCORING_VERSION = 1
#: 稳定倾向维度（内部解释轴，不新增用户配置；值 [-1,1]）
TRAIT_AXES: Tuple[str, ...] = (
    "leisure_recovery", "physical_activity", "focused_activity",
    "social", "novelty", "regularity")
#: 运行缓存容量上限（装载/批次提交时清理长期未引用旧项）
MAX_CACHE_ENTRIES = 512
#: 单批上限与保守起步值（宿主输出容量未知先 12）
MAX_BATCH_SIZE = 24
DEFAULT_BATCH_SIZE = 12
#: 每本地日自动请求上限（含实际发送后失败的请求）
DAILY_REQUEST_LIMIT = 4
#: 单次排空最多尝试的批数（与每日上限同阶；失败/退避/上限会提前终止）
MAX_DRAIN_BATCHES = DAILY_REQUEST_LIMIT
#: 失败退避（分钟）与默认超时（毫秒）
BACKOFF_MINUTES = 30
REQUEST_TIMEOUT_MS = 30_000
#: §14.6 评分系数（待回放校准的初值）
PREFERENCE_BONUS_SCALE = 0.30
INTEREST_BONUS_MIN = -0.35
INTEREST_BONUS_MAX = 0.45
#: 规则降级混信权重（证据保守，不当事实概率）
FALLBACK_CONFIDENCE = 0.35
#: 短原因代码（有限集合；模型只能从中选）
REASON_CODES = frozenset({
    "quiet_relax", "physical_fit", "focus_fit", "social_fit", "novelty_fit",
    "routine_fit", "neutral_no_evidence", "dislike_effort", "dislike_social",
    "dislike_quiet", "dislike_novelty", "persona_says_like", "persona_says_dislike",
})
_EVENT_TAG_PROFILE = "activity_preference_profile"
_EVENT_TAG_SCORE = "activity_preference_score"


def _is_finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value == value and abs(value) != float("inf"))


def _clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def activity_identity(definition: Mapping[str, Any]) -> Tuple[str, str]:
    """稳定语义身份 ``(activity_key, descriptor_hash)``。

    只取动作语义（名称）、类型、标签——不含随机 item_id、日期、窗口钟点、
    当前数值。同一活动明天重新生成仍命中；实质改名/改定义才重新评估。
    """
    name = str(definition.get("name") or "").strip().casefold()
    goal = str(definition.get("goal_type") or "").strip().casefold()
    tags = sorted({str(t).strip().casefold() for t in (definition.get("tags") or ())
                   if str(t or "").strip()})
    descriptor = json.dumps({"name": name, "goal_type": goal, "tags": tags},
                            ensure_ascii=False, sort_keys=True)
    digest = _sha(descriptor)[:16]
    return f"act_{digest}", digest


def trait_fit(traits: Mapping[str, Any], definition: Mapping[str, Any]) -> float:
    """稳定倾向 × 活动保守特征 → [-1,1] 适配度（纯函数；缺证据轴按 0）。"""
    goal = str(definition.get("goal_type") or "")
    tags = {str(t) for t in (definition.get("tags") or ())}
    weights: Dict[str, float] = {}
    if goal == "rest":
        weights["leisure_recovery"] = 1.0
    elif goal == "entertainment":
        weights["leisure_recovery"] = 0.7
        weights["novelty"] = 0.2
    elif goal == "exercise":
        weights["physical_activity"] = 1.0
    elif goal in ("work", "study"):
        weights["focused_activity"] = 1.0
    elif goal == "social":
        weights["social"] = 1.0
    elif goal == "daily_routine":
        weights["regularity"] = 0.6
        weights["leisure_recovery"] = 0.2
    if tags & {"安静", "放松", "休息"}:
        weights["leisure_recovery"] = weights.get("leisure_recovery", 0.0) + 0.2
    if tags & {"运动", "户外", "体力"}:
        weights["physical_activity"] = weights.get("physical_activity", 0.0) + 0.3
    if tags & {"兴趣", "新鲜", "探索"}:
        weights["novelty"] = weights.get("novelty", 0.0) + 0.2
    if tags & {"专注", "学习", "整理"}:
        weights["focused_activity"] = weights.get("focused_activity", 0.0) + 0.2
    if tags & {"社交", "联系"}:
        weights["social"] = weights.get("social", 0.0) + 0.3
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    acc = 0.0
    for axis, weight in weights.items():
        value = traits.get(axis)
        acc += weight * (float(value) if _is_finite_number(value) else 0.0)
    return _clamp(acc / total)


def interest_bonus(preference_affinity: float, motive_bonus: float) -> float:
    """§14.6 联合上限：偏好加分 + 既有动机加成（各自先按原口径取值）。"""
    bonus = PREFERENCE_BONUS_SCALE * _clamp(preference_affinity) + max(0.0, motive_bonus)
    return max(INTEREST_BONUS_MIN, min(INTEREST_BONUS_MAX, bonus))


class ActivityPreferencesService:
    """存储 + 批量评估 + 纯本地评分（组合根注入只读端口）。"""

    def __init__(self, data_dir: Any, *,
                 persona_provider: Callable[[], str],
                 llm: Any = None,
                 definitions_provider: Optional[Callable[[], Sequence[Mapping[str, Any]]]] = None,
                 now_provider: Optional[Callable[[], datetime]] = None,
                 enabled_provider: Optional[Callable[[], bool]] = None,
                 busy_provider: Optional[Callable[[], bool]] = None,
                 decision_logger: Any = None,
                 batch_size: int = DEFAULT_BATCH_SIZE) -> None:
        self._path = Path(data_dir) / PREFERENCES_FILE_NAME if data_dir is not None else None
        self._persona_provider = persona_provider
        self._llm = llm
        self._definitions_provider = definitions_provider
        self._now_provider = now_provider
        self._enabled_provider = enabled_provider
        self._busy_provider = busy_provider
        self._decision_logger = decision_logger
        self._batch_size = max(1, min(MAX_BATCH_SIZE, int(batch_size)))
        self._write_lock = threading.RLock()
        self._batch_lock = asyncio.Lock()
        self._corrupt = False
        self._persona_hash = ""
        self._traits: Dict[str, float] = {}
        self._traits_source = "neutral"
        self._traits_stale = False
        self._traits_updated_at = ""
        self._activities: Dict[str, Dict[str, Any]] = {}
        self._aliases: Dict[str, str] = {}
        self._request_date = ""
        self._requests_today = 0
        self._total_requests = 0
        self._total_tokens = 0
        self._backoff_until: Optional[datetime] = None
        #: 最近一次评估/排空状态（诊断只读；pending 清除后仍保留，便于 /lbm_diag
        #: 看到「上次为何停下」）。
        self._last_status = ""
        #: 持久 pending：睡眠中被忙闲门/上限/退避挡下时留痕，醒后/下次清醒拍续排。
        self._pending: Dict[str, Any] = {}
        self._load()

    # ---- 时间/开关 ----
    def _now(self) -> datetime:
        if self._now_provider is not None:
            try:
                value = self._now_provider()
                if isinstance(value, datetime):
                    return value
            except Exception:
                pass
        return datetime.now().astimezone()

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return False

    def _busy(self) -> bool:
        if self._busy_provider is None:
            return False
        try:
            return bool(self._busy_provider())
        except Exception:
            return True  # 忙闲未知 → 保守推迟自动增量

    def _persona(self) -> str:
        try:
            return str(self._persona_provider() or "")
        except Exception:
            return ""

    def _log(self, action: str, **fields: Any) -> None:
        if self._decision_logger is None:
            return
        try:
            self._decision_logger.record(action=action, trigger_type="activity_preferences",
                                         **fields)
        except Exception:
            pass

    # ---- 持久化（原子写 + 单写入者锁；损坏原件留证）----
    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            doc = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or doc.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("schema_version 不符")
            self._persona_hash = str(doc.get("persona_hash") or "")
            traits = doc.get("traits")
            if isinstance(traits, dict) and all(
                    a in traits and _is_finite_number(traits[a])
                    and -1.0 <= float(traits[a]) <= 1.0 for a in TRAIT_AXES):
                self._traits = {a: float(traits[a]) for a in TRAIT_AXES}
                self._traits_source = str(doc.get("traits_source") or "llm")
            self._traits_stale = bool(doc.get("traits_stale"))
            self._traits_updated_at = str(doc.get("traits_updated_at") or "")
            activities = doc.get("activities")
            if isinstance(activities, dict):
                for key, entry in activities.items():
                    if isinstance(entry, dict) and self._valid_entry(entry):
                        self._activities[str(key)] = dict(entry)
            aliases = doc.get("aliases")
            if isinstance(aliases, dict):
                self._aliases = {str(k): str(v) for k, v in aliases.items()}
            ledger = doc.get("ledger") or {}
            self._request_date = str(ledger.get("request_date") or "")
            self._requests_today = max(0, int(ledger.get("requests_today") or 0))
            self._total_requests = max(0, int(ledger.get("total_requests") or 0))
            self._total_tokens = max(0, int(ledger.get("total_tokens") or 0))
            raw_backoff = ledger.get("backoff_until")
            if isinstance(raw_backoff, str) and raw_backoff:
                try:
                    self._backoff_until = datetime.fromisoformat(raw_backoff)
                except ValueError:
                    self._backoff_until = None
            pending = doc.get("pending")
            if isinstance(pending, dict):
                pending_count = max(0, int(pending.get("count") or 0))
                pending_traits = bool(pending.get("traits"))
                # 只恢复真正有待办的状态；空 pending 不复活，避免每次加载后
                # 首次调用误判「有待排空」而写盘/写日志。
                if pending_count or pending_traits:
                    self._pending = {
                        "since": str(pending.get("since") or "")[:40],
                        "count": pending_count,
                        "last_status": str(pending.get("last_status") or "")[:40],
                        "traits": pending_traits,
                    }
            self._last_status = str(doc.get("last_status")
                                    or self._pending.get("last_status") or "")[:40]
        except Exception as exc:
            # 损坏原件留证（一次性备份），运行中性降级；不在读取中覆盖原件。
            self._corrupt = True
            logger.warning("activity_preferences 缓存损坏（中性降级，原件留证）：%s", exc)
            try:
                backup = self._path.with_suffix(
                    f".corrupt-{self._now().strftime('%Y%m%d-%H%M%S')}.bak")
                if not backup.exists():
                    backup.write_bytes(self._path.read_bytes())
            except Exception:
                logger.debug("activity_preferences 损坏备份失败（忽略）", exc_info=True)

    @staticmethod
    def _valid_entry(entry: Mapping[str, Any]) -> bool:
        return (isinstance(entry.get("descriptor_hash"), str)
                and _is_finite_number(entry.get("affinity"))
                and -1.0 <= float(entry["affinity"]) <= 1.0
                and _is_finite_number(entry.get("confidence"))
                and 0.0 <= float(entry["confidence"]) <= 1.0)

    def _document(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "scoring_version": SCORING_VERSION,
            "persona_hash": self._persona_hash,
            "traits": dict(self._traits),
            "traits_source": self._traits_source,
            "traits_stale": self._traits_stale,
            "traits_updated_at": self._traits_updated_at,
            "activities": dict(self._activities),
            "aliases": dict(self._aliases),
            "ledger": {
                "request_date": self._request_date,
                "requests_today": self._requests_today,
                "total_requests": self._total_requests,
                "total_tokens": self._total_tokens,
                "backoff_until": (self._backoff_until.isoformat()
                                  if self._backoff_until is not None else ""),
            },
            "pending": dict(self._pending),
            "last_status": self._last_status,
        }

    def _save(self) -> None:
        if self._path is None:
            return
        with self._write_lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                if save_json_atomic(self._path, self._document()):
                    self._corrupt = False
            except Exception as exc:
                logger.warning("activity_preferences 落盘失败（忽略）：%s", exc)

    # ---- 纯读口（planner/页面/提示词；绝不发请求）----
    def preference_for(self, definition: Mapping[str, Any]) -> Dict[str, Any]:
        """解析一份可解释偏好：喜欢程度、依据来源、是否已评估（§14.1）。

        人设不符 → 旧评分立即停止参与（中性）；定义改变 → 该项失效降级；
        未评估 → 稳定倾向保守特征匹配（rule_fallback），无倾向 → neutral。
        """
        key, dhash = activity_identity(definition)
        base = {"activity_key": key, "affinity": 0.0, "confidence": 0.0,
                "source": "neutral", "evaluated": False, "reason_code": "neutral_no_evidence"}
        current_persona = _sha(self._persona())
        if self._persona_hash and current_persona != self._persona_hash:
            base["source"] = "persona_changed"
            return base
        if self._corrupt:
            base["source"] = "cache_corrupt"
            return base
        entry = self._activities.get(key)
        tf = trait_fit(self._traits, definition) if self._traits else 0.0
        if entry is not None and entry.get("descriptor_hash") == dhash:
            conf = float(entry["confidence"])
            blended = conf * float(entry["affinity"]) + (1.0 - conf) * tf
            base.update({"affinity": _clamp(blended), "confidence": conf,
                         "source": "llm", "evaluated": True,
                         "reason_code": str(entry.get("reason_code") or "neutral_no_evidence")})
            return base
        if self._traits and self._traits_source == "llm" and not self._traits_stale:
            base.update({"affinity": tf, "confidence": FALLBACK_CONFIDENCE,
                         "source": "rule_fallback"})
        return base

    def summary_for_prompt(self, max_items: int = 3) -> str:
        """少量稳定喜欢/不太喜欢摘要（spine 变量段；不重复整份人设）。"""
        current_persona = _sha(self._persona())
        if self._persona_hash and current_persona != self._persona_hash:
            return ""
        liked: List[Tuple[float, str]] = []
        disliked: List[Tuple[float, str]] = []
        for entry in self._activities.values():
            if not self._valid_entry(entry):
                continue
            name = str(entry.get("name") or "")
            if not name:
                continue
            aff = float(entry["affinity"])
            if aff >= 0.35:
                liked.append((aff, name))
            elif aff <= -0.35:
                disliked.append((aff, name))
        liked.sort(reverse=True)
        disliked.sort()
        parts: List[str] = []
        if liked:
            parts.append("偏好喜欢：" + "、".join(n for _, n in liked[:max_items]))
        if disliked:
            parts.append("不太想做：" + "、".join(n for _, n in disliked[:max_items]))
        if not parts:
            return ""
        return "【稳定口味参考（非今日状态）】" + "；".join(parts)

    def status(self) -> Dict[str, Any]:
        sources: Dict[str, int] = {}
        current_persona = _sha(self._persona())
        persona_match = (not self._persona_hash) or current_persona == self._persona_hash
        for entry in self._activities.values():
            src = "llm" if persona_match else "persona_changed"
            sources[src] = sources.get(src, 0) + 1
        return {
            "enabled": self._enabled(),
            "persona_hash": self._persona_hash[:12],
            "persona_match": persona_match,
            "traits_source": self._traits_source,
            "traits_stale": self._traits_stale,
            "traits": dict(self._traits),
            "cached_activities": len(self._activities),
            "sources": sources,
            "requests_today": self._requests_today,
            "daily_limit": DAILY_REQUEST_LIMIT,
            "total_requests": self._total_requests,
            "total_tokens": self._total_tokens,
            "backoff_until": (self._backoff_until.isoformat()
                              if self._backoff_until is not None else ""),
            "pending": dict(self._pending),
            "pending_count": max(0, int(self._pending.get("count") or 0)),
            "pending_traits": bool(self._pending.get("traits")),
            "last_status": self._last_status,
            "corrupt": self._corrupt,
            "scoring_version": SCORING_VERSION,
        }

    # ---- 批量评估（异步单飞；普通 tick/页面绝不调用）----
    def _definitions(self) -> List[Dict[str, Any]]:
        if self._definitions_provider is None:
            return []
        try:
            return [dict(d) for d in (self._definitions_provider() or [])
                    if isinstance(d, Mapping)]
        except Exception:
            return []

    def _roll_request_ledger(self, now: datetime) -> None:
        date = now.strftime("%Y-%m-%d")
        if date != self._request_date:
            self._request_date = date
            self._requests_today = 0

    def _collect_needed(self, defs: Sequence[Mapping[str, Any]], *, force: bool,
                        persona_changed: bool,
                        skip_keys: Optional[set] = None
                        ) -> Tuple[List[Tuple[str, str, Dict[str, Any]]], bool]:
        """本轮仍需评估的语义项与是否需补画像（纯本地判定）。

        ``skip_keys``：force 排空模式下本调用已处理过的语义 key（仍计入去重，
        但不再重复请求），使 ``force=True`` 的重建也能跨批排空而不是每轮只取前 12。
        """
        needed: List[Tuple[str, str, Dict[str, Any]]] = []
        seen: set = set()
        skip = skip_keys or ()
        for d in defs:
            key, dhash = activity_identity(d)
            if key in seen:
                continue
            seen.add(key)
            if key in skip:
                continue
            entry = self._activities.get(key)
            if force or persona_changed or entry is None \
                    or entry.get("descriptor_hash") != dhash:
                needed.append((key, dhash, d))
        need_traits = (force or persona_changed or not self._traits
                       or self._traits_source != "llm")
        return needed, need_traits

    def _mark_pending(self, count: int, status: str, now: Optional[datetime] = None,
                      *, traits: bool = False) -> None:
        """持久化「还有待评估项」：睡眠忙闲/日限/退避/失败后由清醒拍续排。

        ``traits`` 单独标记「只有画像待评估」（活动定义为空的启动/换人设场景），
        避免 deferred_busy 在无定义时无处可记而被静默丢弃。凡 pending 状态
        发生变化都写一条 ``planning.preferences_pending`` 决策日志；同一状态
        反复重试不重复刷日志。
        """
        count = max(0, int(count))
        traits = bool(traits)
        if count <= 0 and not traits:
            return
        moment = now or self._now()
        status_text = str(status or "")[:40]
        previous = (int(self._pending.get("count") or 0),
                    str(self._pending.get("last_status") or ""),
                    bool(self._pending.get("traits")))
        changed = (not self._pending) or previous != (count, status_text, traits)
        since = str(self._pending.get("since") or "") \
            or moment.isoformat(timespec="seconds")
        self._pending = {"since": since, "count": count,
                         "last_status": status_text, "traits": traits}
        self._last_status = status_text
        if not changed:
            return
        self._save()
        self._log("planning.preferences_pending", count=count,
                  status=status_text, traits=traits, since=since,
                  requests_today=self._requests_today)

    def _clear_pending(self, status: str = "") -> None:
        """清除持久 pending；由「已排空」状态显式闭环并写决策日志。

        无 pending 时只更新 ``last_status``（不写日志），避免普通 cached 触发
        制造噪声；pending 真正被处理掉时记 ``planning.preferences_pending_cleared``。
        """
        had_pending = (int(self._pending.get("count") or 0) > 0
                       or bool(self._pending.get("traits")))
        cleared_count = int(self._pending.get("count") or 0)
        cleared_traits = bool(self._pending.get("traits"))
        status_text = str(status or "")[:40]
        # 普通 cached 命中不再覆盖「上次评估结果」（诊断里保留 ok/日限/退避等原因）；
        # pending 真正被清除时，cached 也是一次有意义的闭环原因。
        update_last = had_pending or (bool(status_text) and status_text != "cached")
        changed = had_pending or (update_last and status_text != self._last_status)
        if had_pending:
            self._pending = {}
        if update_last and status_text:
            self._last_status = status_text
        if changed:
            self._save()
        if had_pending:
            self._log("planning.preferences_pending_cleared",
                      status=status_text or "cleared", count=cleared_count,
                      traits=cleared_traits, requests_today=self._requests_today)

    def has_pending(self) -> bool:
        """只读：是否有待评估项（供清醒拍有界续排；不触发请求）。"""
        try:
            return (int(self._pending.get("count") or 0) > 0
                    or bool(self._pending.get("traits")))
        except Exception:
            return False

    async def _evaluate_once(self, defs: Sequence[Mapping[str, Any]], *, force: bool,
                             phash: str, persona_changed: bool,
                             skip_keys: Optional[set] = None,
                             include_traits_override: Optional[bool] = None
                             ) -> Dict[str, Any]:
        """单批评估（一次 LLM 请求）；排空循环据此逐批推进。

        ``skip_keys`` 供 force 排空跳已处理项；``include_traits_override`` 让
        force 重建只在首批请求画像（后续批次 ``force=True`` 仍扫描缓存项，但不
        重复索要 traits）。返回结果在成功时带 ``processed``（本批语义 key）。
        """
        needed, need_traits = self._collect_needed(
            defs, force=force, persona_changed=persona_changed,
            skip_keys=skip_keys)
        include_traits = (need_traits if include_traits_override is None
                          else bool(include_traits_override and need_traits))
        if not needed and not include_traits:
            return {"status": "cached", "pending": 0}
        now = self._now()
        if self._requests_today >= DAILY_REQUEST_LIMIT:
            return {"status": "daily_limit", "pending": len(needed)}
        if self._backoff_until is not None and now < self._backoff_until:
            return {"status": "backoff", "pending": len(needed),
                    "until": self._backoff_until.isoformat()}
        if not force and self._busy():
            # 睡眠/回复中的推迟：留待清醒拍重试（不静默丢弃）。
            return {"status": "deferred_busy", "pending": len(needed)}
        batch = needed[:self._batch_size]
        prompt = self._build_prompt(batch, include_traits=include_traits)
        event_tag = _EVENT_TAG_PROFILE if include_traits else _EVENT_TAG_SCORE
        # 请求计数在发送前落账（含发出后失败）；热重载恢复不重置。
        self._requests_today += 1
        self._total_requests += 1
        self._save()
        try:
            result = await self._llm.generate_detailed(
                prompt, module="planning", task_name="utils",
                with_identity=True, event=event_tag,
                timeout_ms=REQUEST_TIMEOUT_MS, temperature=0.4)
        except Exception as exc:
            self._backoff_until = self._now() + timedelta(minutes=BACKOFF_MINUTES)
            self._save()
            self._log("planning.preferences_request_failed", error=str(exc))
            return {"status": "failed", "error": str(exc), "pending": len(needed)}
        tokens = int(getattr(result, "total_tokens", None) or 0)
        self._total_tokens += max(0, tokens)
        if not getattr(result, "ok", False):
            self._backoff_until = self._now() + timedelta(minutes=BACKOFF_MINUTES)
            self._save()
            self._log("planning.preferences_request_failed",
                      error=str(getattr(result, "error", ""))[:200], tokens=tokens)
            return {"status": "failed", "error": str(getattr(result, "error", "")),
                    "tokens": tokens, "pending": len(needed)}
        # 旧响应晚到复核：人设已换 → 整份丢弃（不写入已更换的人设）。
        if _sha(self._persona()) != phash:
            return {"status": "stale_persona_discarded", "tokens": tokens,
                    "pending": len(needed)}
        parsed = self._parse_response(str(getattr(result, "text", "") or ""),
                                      batch, include_traits=include_traits)
        committed = self._commit(parsed, phash, defs, now)
        self._trim_cache(defs)
        # 无效/截断/空结果（ok 但零合法发布）→ 如实降级 + 退避，不在同轮追加请求。
        if committed["evaluated"] == 0 and not committed["traits"]:
            self._backoff_until = self._now() + timedelta(minutes=BACKOFF_MINUTES)
            self._save()
            self._log("planning.preferences_invalid_result", tokens=tokens)
            return {"status": "failed", "error": "invalid_or_empty_result",
                    "tokens": tokens, **committed}
        self._save()
        self._log("planning.preferences_evaluated",
                  evaluated=committed["evaluated"], traits=committed["traits"],
                  tokens=tokens, requests_today=self._requests_today)
        return {"status": "ok", "tokens": tokens,
                "processed": [key for key, _h, _d in batch], **committed}

    async def ensure_evaluated(self, definitions: Optional[Sequence[Mapping[str, Any]]] = None,
                               *, force: bool = False, drain: bool = True) -> Dict[str, Any]:
        """合并安排增量批次：首次/显式初始化/人设变化/新定义。

        ``drain=True``（默认）时在同一单飞锁内连续排空批次（单批 ≤12，最多
        ``MAX_DRAIN_BATCHES`` 批且受每本地日 ≤4 次请求上限）；``force=True`` =
        管理员明确重建（含画像）——同样排空全部批次，不再只取首批 12 项，画像
        只随首批请求。忙闲/日限/退避/失败时把剩余项（含仅画像待评估）持久化到
        ``pending`` 并写决策日志，由清醒拍/下次显式触发重试。返回诊断 dict；
        任何失败中性降级不抛出。
        """
        if not self._enabled():
            return {"status": "disabled"}
        if self._llm is None:
            return {"status": "no_llm"}
        # force=True 的契约是「完整重建」→ 必须排空，不受调用方 drain=False 削弱。
        drain = bool(drain or force)
        async with self._batch_lock:
            self._roll_request_ledger(self._now())
            phash = _sha(self._persona())
            defs = [dict(d) for d in (definitions if definitions is not None
                                      else self._definitions())]
            totals = {"evaluated": 0, "traits": False, "tokens": 0, "batches": 0}
            # force 重建：记下本次应强制重评的语义 key，逐批跳过已处理项，
            # 从而「每轮只取前 12」不再成立；首批后不再重复索要 traits。
            force_keys: List[str] = []
            force_done: set = set()
            if force:
                initial_needed, _initial_traits = self._collect_needed(
                    defs, force=True, persona_changed=False)
                force_keys = [key for key, _h, _d in initial_needed]

            def _remaining() -> Tuple[List[str], bool, bool]:
                """按最新人设/缓存重算剩余语义 key（force 未处理项也算剩余）。"""
                changed = (bool(self._persona_hash)
                           and _sha(self._persona()) != self._persona_hash)
                still_needed, need_traits_now = self._collect_needed(
                    defs, force=False, persona_changed=changed)
                keys = [key for key, _h, _d in still_needed]
                if force:
                    seen_keys = set(keys)
                    keys.extend(key for key in force_keys
                                if key not in force_done and key not in seen_keys)
                return keys, need_traits_now, changed

            for index in range(MAX_DRAIN_BATCHES if drain else 1):
                # 每批前重读人设：首批提交画像后不再把全部定义误判为「人设变化」。
                current_changed = (bool(self._persona_hash)
                                   and _sha(self._persona()) != self._persona_hash)
                if force:
                    result = await self._evaluate_once(
                        defs, force=True, phash=phash,
                        persona_changed=current_changed,
                        skip_keys=force_done,
                        include_traits_override=(index == 0))
                else:
                    result = await self._evaluate_once(
                        defs, force=False,
                        phash=phash, persona_changed=current_changed)
                status = str(result.get("status") or "")
                if status in ("disabled", "no_llm"):
                    return result
                if status == "cached":
                    # 缓存已覆盖定义（含 pending 项已不可评估/已评估）：闭环清除。
                    self._clear_pending("cached")
                    if totals["batches"]:
                        return {"status": "ok", **totals}
                    return result
                if status == "ok":
                    totals["evaluated"] += int(result.get("evaluated") or 0)
                    totals["traits"] = bool(totals["traits"] or result.get("traits"))
                    totals["tokens"] += int(result.get("tokens") or 0)
                    totals["batches"] += 1
                    if force:
                        force_done.update(result.get("processed") or ())
                    # 提交后按最新人设/缓存重算剩余；force 未处理项继续参与排空。
                    remaining, need_traits, current_changed = _remaining()
                    # 人设变化但本批没拿到有效画像：后续批次不会补画像，继续排空
                    # 只会重复请求 → 停在这里，留 pending 等下一次触发。
                    if remaining and need_traits and current_changed \
                            and not result.get("traits"):
                        self._mark_pending(len(remaining), "traits_missing",
                                           traits=True)
                        return {"status": "ok", **totals, "pending": len(remaining)}
                    if remaining and drain \
                            and self._requests_today < DAILY_REQUEST_LIMIT:
                        continue
                    if remaining:
                        pending_status = ("daily_limit"
                                          if self._requests_today >= DAILY_REQUEST_LIMIT
                                          else "partial")
                        self._mark_pending(len(remaining), pending_status,
                                           traits=need_traits)
                    else:
                        self._clear_pending("ok")
                    return {"status": "ok", **totals,
                            "pending": len(remaining)}
                # 失败/推迟/退避/日限/人设切换：留 pending，等清醒拍或退避结束重试。
                remaining, need_traits, _changed = _remaining()
                if remaining or need_traits:
                    # deferred_busy（睡眠/忙闲）与失败/日限/退避一律落盘 + 日志，
                    # 不静默丢弃；同一状态重复重试由 _mark_pending 去重。
                    self._mark_pending(len(remaining), status, traits=need_traits)
                if totals["batches"]:
                    return {"status": "partial", **totals,
                            "pending": len(remaining),
                            "error": str(result.get("error") or status)}
                return result
            # 达到排空批数上限仍未完：留 pending 给下一拍（不丢）。
            remaining, need_traits, _changed = _remaining()
            if remaining or need_traits:
                self._mark_pending(len(remaining), "drain_capped",
                                   traits=need_traits)
            return {"status": "ok", **totals, "pending": len(remaining)}

    async def rebuild(self) -> Dict[str, Any]:
        """管理员偏好重建入口（默认命中缓存由调用方控制；明确重建才换画像）。

        明确重建同样走排空循环（force 只影响首批是否强制重评 + 是否附带画像）。
        """
        return await self.ensure_evaluated(force=True, drain=True)

    def _build_prompt(self, batch: Sequence[Tuple[str, str, Dict[str, Any]]],
                      *, include_traits: bool) -> str:
        lines = []
        for key, _dhash, d in batch:
            tags = "、".join(str(t) for t in (d.get("tags") or ())) or "无"
            lines.append(f"- activity_key={key} | 名称：{d.get('name')} | "
                         f"类型：{d.get('goal_type')} | 标签：{tags}")
        activities_block = "\n".join(lines)
        trait_instructions = ""
        trait_schema = ""
        if include_traits:
            trait_instructions = (
                "同时估计 bot 的稳定倾向（不是今日状态）：六个维度各给 [-1,1] 数值，"
                "维度为 leisure_recovery(休闲恢复)、physical_activity(体力活动)、"
                "focused_activity(专注活动)、social(社交)、novelty(新鲜感)、regularity(规律性)。"
                "人设没提到的维度给 0。\n")
            trait_schema = ('  "traits": {"leisure_recovery": 0.0, "physical_activity": 0.0, '
                            '"focused_activity": 0.0, "social": 0.0, "novelty": 0.0, '
                            '"regularity": 0.0},\n')
        return (
            "只依据 bot 人设（已在系统开头给出）和下列活动定义，估计 bot 对每项活动的"
            "稳定喜好。规则：\n"
            "- 缺证据给中性 0；不把善良推断成必须工作，不把内向推断成孤独，"
            "不把懒散推断成无视约定；不根据当前疲惫/高债/用户行为改人格。\n"
            "- 懒不自动等于孤独、缺觉、爱刷QQ或爱看某类视频。\n"
            "- affinity ∈ [-1,1]（喜欢程度），confidence ∈ [0,1]（仅供混合权重，"
            "不是事实概率），reason_code 从有限集合选："
            + "、".join(sorted(REASON_CODES)) + "。\n"
            "- 只返回请求中存在的 activity_key；禁止返回工具、账号、计划时间、"
            "新增活动、reasoning 或长篇性格分析。\n"
            + trait_instructions +
            "活动定义：\n" + activities_block + "\n\n"
            "只输出一个 JSON 对象，格式：\n{\n" + trait_schema +
            '  "activities": [{"activity_key": "...", "affinity": 0.0, '
            '"confidence": 0.0, "reason_code": "..."}]\n}')

    def _parse_response(self, text: str, batch: Sequence[Tuple[str, str, Dict[str, Any]]],
                        *, include_traits: bool) -> Dict[str, Any]:
        """严格解析：越界/布尔冒充数字/未知重复 key 拒绝相应记录，合法部分可发布。"""
        out: Dict[str, Any] = {"traits": None, "entries": []}
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            return out
        try:
            doc = json.loads(stripped[start:end + 1])
        except ValueError:
            return out
        if not isinstance(doc, dict):
            return out
        if include_traits:
            traits = doc.get("traits")
            if isinstance(traits, dict) and all(
                    a in traits and _is_finite_number(traits[a])
                    and -1.0 <= float(traits[a]) <= 1.0 for a in TRAIT_AXES):
                out["traits"] = {a: float(traits[a]) for a in TRAIT_AXES}
        requested = {key for key, _h, _d in batch}
        seen: set = set()
        raw_entries = doc.get("activities")
        if isinstance(raw_entries, list):
            for item in raw_entries:
                if not isinstance(item, dict):
                    continue
                key = item.get("activity_key")
                if not isinstance(key, str) or key not in requested or key in seen:
                    continue
                aff, conf = item.get("affinity"), item.get("confidence")
                if not _is_finite_number(aff) or not -1.0 <= float(aff) <= 1.0:
                    continue
                if not _is_finite_number(conf) or not 0.0 <= float(conf) <= 1.0:
                    continue
                reason = item.get("reason_code")
                if not isinstance(reason, str) or reason not in REASON_CODES:
                    reason = "neutral_no_evidence"
                seen.add(key)
                out["entries"].append({"activity_key": key, "affinity": float(aff),
                                       "confidence": float(conf), "reason_code": reason})
        return out

    def _commit(self, parsed: Dict[str, Any], phash: str,
                defs: Sequence[Mapping[str, Any]], now: datetime) -> Dict[str, Any]:
        by_key: Dict[str, Tuple[str, Mapping[str, Any]]] = {}
        for d in defs:
            key, dhash = activity_identity(d)
            by_key[key] = (dhash, d)
        evaluated = 0
        for entry in parsed["entries"]:
            key = entry["activity_key"]
            info = by_key.get(key)
            if info is None:
                continue
            dhash, d = info
            # descriptor 复核：定义已变 → 不写旧结果
            current_key, current_hash = activity_identity(d)
            if current_key != key or current_hash != dhash:
                continue
            self._activities[key] = {
                "descriptor_hash": dhash,
                "affinity": entry["affinity"],
                "confidence": entry["confidence"],
                "reason_code": entry["reason_code"],
                "name": str(d.get("name") or ""),
                "source": "llm",
                "updated_at": now.isoformat(timespec="seconds"),
            }
            eid = str(d.get("event_id") or d.get("item_id") or "")
            if eid:
                self._aliases[eid] = key
            evaluated += 1
        traits_ok = False
        if parsed["traits"] is not None:
            self._traits = parsed["traits"]
            self._traits_source = "llm"
            self._traits_stale = False
            self._traits_updated_at = now.isoformat(timespec="seconds")
            self._persona_hash = phash
            traits_ok = True
        elif self._persona_hash != phash:
            # 人设已换但本批未含画像：旧画像标过期（同人设旧有效画像保留口径仅
            # 适用于画像无效时；人设变化时旧评分不参与由 preference_for 把关）。
            self._traits_stale = True
        return {"evaluated": evaluated, "traits": traits_ok}

    def _trim_cache(self, defs: Sequence[Mapping[str, Any]]) -> None:
        if len(self._activities) <= MAX_CACHE_ENTRIES:
            return
        referenced = {activity_identity(d)[0] for d in defs}
        # 优先保留仍被现有池/意图引用的记录；其余按更新时间淘汰最旧。
        items = sorted(self._activities.items(),
                       key=lambda kv: (kv[0] in referenced,
                                       str(kv[1].get("updated_at") or "")),
                       reverse=True)
        self._activities = dict(items[:MAX_CACHE_ENTRIES])
        self._aliases = {k: v for k, v in self._aliases.items()
                         if v in self._activities}