"""Coordinate durable offline sleep settlement between the life state and schedule stores."""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.storage import save_json_atomic
from core.time_utils import elapsed_seconds, shift_elapsed, wall_time
from planning.life_state import DT_MAX_MINUTES
from planning.offline_sleep import plan_offline_settlement
from planning.sleep_engine import EVENT_SLEEP_WOKE, SleepEngine


def offline_episode_id(plan: dict, *, now: datetime | None = None) -> str:
    """复核 P2-4c / CF-6：离线结算幂等键。

    优先用计划携带的 ``sleep_date``（入睡日历日）+ ``start_minute``（入睡时刻在
    该日程日帧内的分钟，跨午夜 >1440）→ ``offline:{sleep_date}:{start_minute}``。
    这两个字段由 :meth:`OfflineSettlementService.apply_offline_settlement` 按配置
    时区与 ``gap_seconds`` 现算后注入到幂等键；键只由该日帧决定，不读本地墙钟
    日期，也不受跨午夜/本地时区漂移影响。对已携带这两个字段的计划，``now`` 完全
    不参与——同一计划在不同本地 ``now`` 下得到同一键。旧实现用
    ``int(effective_minutes)`` 取整，同日不同起始分钟（或不同有效时长）的合法新段
    会碰撞；新键按帧分钟区分，不再误拦。

    仅在计划缺 ``sleep_date``/``start_minute``（含非法值）时回退旧实现（本地日期
    + 有效分钟）；旧格式保持不变，供既有调用方/回归兼容。回退路径不抛异常。
    """
    sleep_date = str(plan.get("sleep_date") or "").strip()
    start_minute = plan.get("start_minute")
    if sleep_date and start_minute is not None and not isinstance(start_minute, bool):
        try:
            # 规范成 YYYY-MM-DD；非法日历日/格式 → 走旧键回退
            normalized = datetime.strptime(sleep_date, "%Y-%m-%d").strftime("%Y-%m-%d")
            sm = float(start_minute)
            if math.isfinite(sm):
                suffix = ":fold1" if plan.get("start_fold") == 1 else ""
                return f"offline:{normalized}:{int(sm)}{suffix}"
        except (TypeError, ValueError, OverflowError):
            pass
    day = (now or datetime.now()).strftime("%Y-%m-%d")
    try:
        raw_minutes = float(plan.get("effective_minutes") or 0.0)
        minutes = int(raw_minutes) if math.isfinite(raw_minutes) else 0
    except (TypeError, ValueError, OverflowError):
        minutes = 0
    return f"offline:{day}:{minutes}"


class OfflineSettlementService:
    """Save the immutable plan before either business store advances its offline watermark."""

    def __init__(
        self,
        *,
        db: Any,
        config_provider: Callable[[], Any],
        wake_recorder: Callable[..., Any],
        event_bus_provider: Optional[Callable[[], Any]] = None,
        logger_provider: Optional[Callable[[], Any]] = None,
        settlement_v2_module: Any = None,
        data_dir: Any = None,
    ) -> None:
        self._db = db
        self._config_provider = config_provider
        self._wake_recorder = wake_recorder
        self._event_bus_provider = event_bus_provider
        self._logger_provider = logger_provider
        self._settlement_v2 = settlement_v2_module
        self._path = Path(data_dir) / "offline_sleep_settlement.json" if data_dir is not None else None
        self._record = None
        self._record_saved = False
        self._loaded = False
        self._gap = None

    def _log(self, level: str, message: str, *args: Any) -> None:
        try:
            logger = self._logger_provider() if self._logger_provider else None
            getattr(logger or logging.getLogger(__name__), level)(message, *args)
        except Exception:
            pass

    def _load(self) -> None:
        if self._loaded:
            return
        if self._path is not None:
            try:
                with self._path.open(encoding="utf-8") as handle:
                    record = json.load(handle)
            except FileNotFoundError:
                record = None
            if record is not None:
                if (
                    not isinstance(record, dict)
                    or record.get("version") != 1
                    or record.get("state") not in ("pending", "done")
                    or not isinstance(record.get("plan"), dict)
                    or not record["plan"].get("wake_at")
                ):
                    raise ValueError("无法识别离线睡眠结算登记")
                self._record = record
                self._record_saved = True
        self._loaded = True

    def _save(self, record: dict) -> None:
        if self._path is not None and not save_json_atomic(self._path, record):
            raise OSError("离线睡眠结算登记写入失败")
        self._record = record
        self._record_saved = True

    def _now(self) -> datetime:
        from planning.time_utils import TimezoneManager

        zone = getattr(getattr(self._config_provider(), "schedule_advanced", None), "timezone", None)
        return TimezoneManager(str(zone or "Asia/Shanghai")).get_now()

    def _pin(self, plan: dict, wake: datetime) -> dict:
        plan = dict(plan)
        if plan.get("wake_at"):
            stamp = datetime.fromisoformat(plan["wake_at"])
            zone = self._now().tzinfo
            wake = stamp.astimezone(zone) if stamp.tzinfo and zone else stamp
        start = shift_elapsed(wake, -max(0.0, float(plan.get("gap_seconds") or 0)))
        if plan.get("sleep_date") and plan.get("start_minute") is not None:
            day = datetime.strptime(str(plan["sleep_date"]), "%Y-%m-%d").date()
            minute = int(plan["start_minute"])
            if plan.get("sleep_start"):
                start = datetime.fromisoformat(plan["sleep_start"])
            elif (start.date(), start.hour * 60 + start.minute) != (day, minute):
                start = wall_time(day, minute, wake.tzinfo)
        else:
            day = start.date()
            minute = SleepEngine._frame_minutes(start, day.isoformat())
        plan.update(
            wake_at=wake.isoformat(),
            sleep_date=day.isoformat(),
            start_minute=minute,
            sleep_start=start.isoformat(),
            start_fold=int(plan.get("start_fold", start.fold)),
        )
        return plan

    @staticmethod
    def _identity(plan: dict) -> str:
        if plan.get("settlement_kind") == "awake":
            return f"awake:{plan['sleep_start']}:{plan['wake_at']}"
        return offline_episode_id(plan)

    def settle_offline_gap(
        self,
        life: Any,
        planner: Any,
        *,
        engine_claimed: bool,
        plan_settlement_fn: Optional[Callable[..., Any]] = None,
        baseline_hours: float = 8.0,
    ) -> bool:
        try:
            self._load()
            if self._gap is None:
                interval = life.consume_pending_offline_gap()
                consume = getattr(life, "consume_pending_awake_before_offline_seconds", None)
                awake = float(consume() or 0) if callable(consume) else 0.0
                if interval is not None or awake:
                    self._gap = (max(0.0, float(interval or 0)), awake, self._now())
            if self._record and self._record["state"] == "pending":
                if not self._commit(life, planner, self._record["plan"]):
                    return False
            if self._gap is None or engine_claimed:
                self._gap = None
                return True
            gap, awake, wake = self._gap
            # A restart before the life receipt may still contain the original, overlapping gap.
            if self._record:
                old_wake = datetime.fromisoformat(self._record["plan"]["wake_at"])
                remaining = max(0.0, elapsed_seconds(wake, old_wake))
                awake = min(awake, max(0.0, remaining - gap))
                gap = min(gap, remaining)
            if gap <= 0 and awake <= 0:
                self._gap = None
                return True
            try:
                debt = float(life.snapshot().get("sleep_debt") or 0.0)
            except Exception:
                debt = 0.0
            schedule = getattr(self._config_provider(), "schedule", None)
            plan = None
            if not getattr(schedule, "no_sleep_mode", False):
                strategy = plan_settlement_fn or plan_offline_settlement
                try:
                    options = (
                        {"baseline_hours": float(baseline_hours)} if float(baseline_hours) != 8.0 else {}
                    )
                    plan = strategy(gap, debt, **options)
                except Exception as exc:
                    self._log("warning", "离线计划生成失败，按清醒漂移结算：%s", exc)
            if not isinstance(plan, dict) or not plan.get("settle"):
                plan = {
                    "settlement_kind": "awake",
                    "awake_minutes": min((gap + awake) / 60.0, DT_MAX_MINUTES),
                }
            else:
                plan = {**plan, "awake_minutes": min(awake / 60.0, DT_MAX_MINUTES)}
            pinned = self._pin({**plan, "gap_seconds": gap}, wake)
            result = self.apply_offline_settlement(life, planner, pinned)
            if result is not False:
                self._gap = None
            return result is not False
        except Exception as exc:
            self._log("warning", "离线睡眠结算待重试：%s", exc)
            return False

    def apply_offline_settlement(self, life: Any, planner: Any, plan: Dict[str, Any]) -> bool:
        try:
            self._load()
            wake = datetime.fromisoformat(plan["wake_at"]) if plan.get("wake_at") else self._now()
            pinned = self._pin(plan, wake)
            identity = self._identity(pinned)
            if self._record:
                existing = self._identity(self._record["plan"])
                if existing == identity and self._record["state"] == "done":
                    return True
                if self._record["state"] == "pending" and existing != identity:
                    if not self._commit(life, planner, self._record["plan"]):
                        return False
            if not self._record or self._record["state"] != "pending":
                self._record = {"version": 1, "state": "pending", "plan": pinned}
                self._record_saved = False
            return self._commit(life, planner, self._record["plan"])
        except Exception as exc:
            self._log("warning", "离线睡眠结算待重试：%s", exc)
            return False

    def _commit(self, life: Any, planner: Any, plan: dict) -> bool:
        if not self._record_saved:
            self._save(self._record)
        wake = datetime.fromisoformat(plan["wake_at"])
        identity = self._identity(plan)
        if plan.get("settlement_kind") == "awake":
            minutes = float(plan["awake_minutes"])
            commit_awake = getattr(life, "apply_offline_awake_once", None)
            if callable(commit_awake):
                commit_awake(receipt_id=identity, minutes=minutes, observed_at=wake)
            elif self._path is None:
                life.apply_offline_awake(minutes)
                if life.save() is False:
                    raise OSError("离线清醒漂移保存失败")
            else:
                raise RuntimeError("缺少离线清醒漂移提交接口")
            self._save({"version": 1, "state": "done", "plan": plan})
            return True
        start = datetime.fromisoformat(plan["sleep_start"])
        day = plan["sleep_date"]
        minute = plan["start_minute"]
        end = SleepEngine._frame_minutes(wake, day)
        session_id = "sleep:" + identity.removeprefix("offline:")
        state_method = getattr(life, "apply_offline_sleep_once", None)
        if callable(state_method):
            edges = state_method(
                receipt_id=identity,
                wake_at=wake,
                awake_minutes=float(plan.get("awake_minutes") or 0),
                effective_hours=float(plan.get("effective_hours") or 0),
                sleep_debt_before=float(plan.get("sleep_debt_before") or 0),
                sleep_debt_after=float(plan.get("sleep_debt_after") or 0),
            )
        elif self._path is None:
            # Legacy, independently supplied in-memory ports. Production always uses the durable protocol.
            edges = self._legacy_state(life, plan, identity, wake)
        else:
            raise RuntimeError("生活状态未提供离线结算接口")
        metadata = dict(plan.get("metadata") or {})
        session = {
            "session_id": session_id,
            "schedule_date": day,
            "planned_start_minute": minute,
            "planned_end_minute": end,
            "actual_start_minute": minute,
            "actual_end_minute": end,
            "wake_reason": "offline_reconnect",
            "continuous_minutes": int(plan.get("effective_minutes") or 0),
            "interruption_count": 0,
            "energy_start": edges["energy_start"],
            "energy_end": edges["energy_end"],
            "sleep_debt_before": plan.get("sleep_debt_before"),
            "sleep_debt_after": plan.get("sleep_debt_after"),
            "quality": plan.get("quality"),
            "metadata": {**metadata, "settled": True, "offline_settlement": True, "emitted": ["woke"]},
            "created_at": start.isoformat(),
            "updated_at": wake.isoformat(),
        }
        metadata.update(
            close_reason="sleep_engine_offline_reconnect", offline_settlement=True, session_id=session_id
        )
        label = str(plan.get("label") or "睡眠")
        commit = getattr(self._db, "commit_offline_sleep", None)
        if callable(commit):
            if not commit(session, episode_id=identity, label=label, metadata=metadata):
                raise OSError("离线睡眠会话与足迹事务未提交")
        elif self._path is None:
            self._legacy_rows(session, identity, label, metadata)
        else:
            raise RuntimeError("日程库未提供离线结算接口")
        if plan.get("category") in ("long", "overlong"):
            if self._wake_recorder(when=wake) is False:
                raise OSError("真实醒来周期尚未保存")
            adjust = getattr(planner, "apply_offline_morning_once", None)
            if callable(adjust):
                if not adjust(receipt_id=identity, wake_at=wake, sleep_hours=float(plan["effective_hours"])):
                    raise OSError("晨间日程调整尚未确认")
            elif self._path is None:
                planner.apply_morning_spine_adjustment(
                    sleep_hours=plan.get("effective_hours"), wake_delay_minutes=0.0
                )
            else:
                raise RuntimeError("缺少离线晨间日程确认接口")
        # A wake notification is advisory. Reserve it after the durable business steps commit.
        # On restart, the last completed plan prevents a repeated wake or morning adjustment.
        self._save({"version": 1, "state": "done", "plan": plan})
        try:
            bus = self._event_bus_provider() if self._event_bus_provider else None
            if bus is not None:
                bus.publish_soon(
                    EVENT_SLEEP_WOKE,
                    event_id=identity,
                    session_id=session_id,
                    wake_reason="offline_reconnect",
                    actual_minutes=round(plan["gap_seconds"] / 60, 1),
                    sleep_hours=round(float(plan.get("effective_hours") or 0), 4),
                    energy=edges["energy_end"],
                    quality=plan.get("quality"),
                    sleep_debt=plan.get("sleep_debt_after"),
                    retroactive=True,
                )
        except Exception as exc:
            self._log("warning", "离线睡眠已提交，醒沿通知失败：%s", exc)
        self._log(
            "info", "离线睡眠结算完成（%s，%.2f 小时）", identity, float(plan.get("effective_hours") or 0)
        )
        return True

    def _legacy_state(self, life: Any, plan: dict, identity: str, wake: datetime) -> dict:
        awake = float(plan.get("awake_minutes") or 0)
        if awake > 0:
            life.apply_offline_awake(awake)
        before = life.snapshot()
        rates = (
            life.settlement_params
            if isinstance(life.settlement_params, dict)
            else self._settlement_v2.V2_PARAMS
        )
        gains = self._settlement_v2.sleep_state_gains(
            before, float(plan.get("effective_hours") or 0), rates, float(plan.get("sleep_debt_before") or 0)
        )
        after = life.apply_episode(
            {
                "state_deltas": dict(gains),
                "settlement_kind": "offline_sleep",
                "diag_dt_min": float(plan.get("effective_minutes") or 0),
                "episode_id": identity,
            }
        )
        life.apply_sleep_settlement(
            {"sleep_debt": plan.get("sleep_debt_after"), "wake_reason": "offline_reconnect", "now": wake}
        )
        return {"energy_start": before.get("energy"), "energy_end": after.get("energy")}

    def _legacy_rows(self, session: dict, identity: str, label: str, metadata: dict) -> None:
        start, end = session["actual_start_minute"], session["actual_end_minute"]
        active = self._db.active_episode()
        if isinstance(active, dict) and active.get("status") == "active":
            if (
                self._db.interrupt(
                    str(active["episode_id"]),
                    interrupted_by="offline_gap",
                    actual_end_minute=start,
                    metadata={"close_reason": "offline_gap_settlement", "offline_reconnect": True},
                )
                is False
            ):
                raise OSError("离线前活动未能结束")
        if self._db.upsert_sleep_session(session) is False:
            raise OSError("离线睡眠会话未能保存")
        eid = self._db.start_episode(
            session["schedule_date"],
            label,
            "sleep",
            "offline",
            start,
            end,
            episode_id=identity,
            actual_start_minute=start,
            metadata=metadata,
        )
        if not eid or self._db.finish(eid, actual_end_minute=end) is False:
            raise OSError("离线睡眠足迹未能保存")
