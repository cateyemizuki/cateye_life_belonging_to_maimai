"""Standalone deterministic cases; invoked in a separate interpreter by unittest."""

from __future__ import annotations
import asyncio
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import patch
import warnings
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
sys.stdout.reconfigure(encoding="utf-8")
repo = Path(os.environ.get("CATEYE_TEST_DIR", Path(__file__).resolve().parents[1])).resolve()
temporary_workspace = tempfile.TemporaryDirectory(prefix="lbm-test-")
work = Path(temporary_workspace.name)
# Windows creates a loopback-only wakeup socketpair for its asyncio loop.
loop = asyncio.new_event_loop()


def guard(name, args):
    if name.startswith("socket.") and name in (
        "socket.connect",
        "socket.bind",
        "socket.sendto",
        "socket.getaddrinfo",
    ):
        raise PermissionError("offline audit")
    if name == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
        path = Path(os.fsdecode(args[0])).resolve()
        if path.is_relative_to(repo / "mappings") and not path.is_relative_to(
            repo / "mappings/default_mappings"
        ):
            raise PermissionError("user mapping excluded")
        if args[2] & (
            os.O_APPEND | os.O_RDWR | os.O_WRONLY | os.O_TRUNC | os.O_CREAT
        ) and not path.is_relative_to(work):
            raise PermissionError(f"write outside evidence directory: {path}")
    if (
        name == "sqlite3.connect"
        and str(args[0]) != ":memory:"
        and not Path(args[0]).resolve().is_relative_to(work)
    ):
        raise PermissionError("only synthetic SQLite databases are allowed")


sys.addaudithook(guard)
logging.disable(logging.CRITICAL)
warnings.filterwarnings("ignore", category=DeprecationWarning)
sys.path.insert(0, str(repo))
import pydantic

sdk = ModuleType("maibot_sdk")
sdk.PluginConfigBase = pydantic.BaseModel
sdk.Field = pydantic.Field
sys.modules["maibot_sdk"] = sdk
# Import the production patrol read adapter directly without initializing host decorators.
companion_package = ModuleType("companion")
companion_package.__path__ = [str(repo / "companion")]
sys.modules["companion"] = companion_package
from core import internal_tuning
from core.time_utils import elapsed_seconds
from planning import cycle_state, settlement_v2
from planning.time_utils import TimezoneManager
from planning.sleep_engine import SleepEngine
from planning.life_state import LifeStateService
from planning.rolling_planner import RollingPlanner
from planning.schedule_db import ScheduleDB
from planning.schedule_generator import ScheduleGenerator
from planning.schedule_query import ScheduleQuery
from planning.life_schedule import LifeScheduleReadModel
from planning.goal_manager import GoalManager
from planning.auto_scheduler import ScheduleAutoScheduler
from planning.tools_service import ToolsService
from planning.command_service import CommandService
from planning import role_judge
from emotion.service import EmotionService
from emotion.vad import VADState
from suite_offline_settlement import OfflineSettlementService

TZ = ZoneInfo("Asia/Shanghai")


def at(text):
    return datetime.fromisoformat(text).replace(tzinfo=TZ)


class Clock(TimezoneManager):
    def __init__(self, current):
        self._tz = current.tzinfo
        self.timezone_str = getattr(self._tz, "key", "Asia/Shanghai")
        self.current = current

    def get_now(self):
        return self.current


class Fixture:
    def __init__(self, root, current, *, no_sleep=False):
        root.mkdir(exist_ok=True)
        self.root = root
        self.clock = Clock(current)
        self.config = NS(
            schedule=NS(enabled=True, no_sleep_mode=no_sleep, sleep_time="23:00", wake_time="07:00"),
            schedule_advanced=NS(timezone="Asia/Shanghai"),
            life_state=NS(),
        )
        self.db = ScheduleDB(root / "test.db")
        self.gm = GoalManager(data_dir=root, db=self.db)
        self.gm.bind_clock_provider(lambda: self.clock)
        self.load_life()

    def load_life(self):
        self.life = LifeStateService(self.root / "life.json", timezone_manager=self.clock)
        self.engine = SleepEngine(
            config_provider=self.config,
            session_store=self.db,
            life_state=self.life,
            timezone_manager=self.clock,
        )
        self.planner = RollingPlanner(
            self.db,
            self.life,
            sleep_engine=self.engine,
            config_provider=self.config,
            goal_manager=self.gm,
            timezone_manager=self.clock,
            data_dir=self.root,
        )
        self.engine._episode_closer = self.planner.close_sleep_episode

    def tick(self, when):
        self.clock.current = when
        pre = self.engine.prepare_tick(
            when,
            sleep_episode=self.db.active_episode(),
            no_sleep_mode=self.config.schedule.no_sleep_mode,
        )
        snapshot = self.life.tick(
            when,
            active_profile=self.planner.resolve_profile_key(None, self.db.active_episode()),
            sleep_rates=pre["rates"],
        )
        result = self.engine.commit_tick(when, energy=snapshot["energy"])
        planner_result = self.planner.tick(when, life_snapshot=snapshot)
        assert "error" not in planner_result, planner_result
        return pre, snapshot, result, planner_result

    def shutdown(self):
        self.db.close()


def no_sleep_case(root):
    fx = Fixture(root, at("2026-11-17T22:59:00"), no_sleep=True)
    try:
        for when in ("2026-11-17T23:00:00", "2026-11-18T01:00:00"):
            fx.tick(at(when))
            assert fx.db.active_episode() is None
            assert fx.db.open_sleep_session() is None
        fx.tick(at("2026-11-18T12:00:00"))
        assert (fx.db.active_episode() or {}).get("goal_type") not in (None, "sleep")
    finally:
        fx.shutdown()


def restored_sleep(root):
    begin, restart = at("2026-11-17T23:00:00"), at("2026-11-18T05:00:00")
    fx = Fixture(root, begin)
    try:
        fx.db.start_episode(begin.date().isoformat(), "睡觉", "sleep", "rolling", 1380, 1950)
        fx.engine.prepare_tick(begin, sleep_episode=fx.db.active_episode())
        fx.engine.commit_tick(begin)
        before = fx.life.snapshot()["energy"]
        fx.clock.current = restart
        fx.load_life()
        gap = fx.life.pending_offline_gap_seconds
        restored = fx.engine.restore(restart)
        assert restored is not None and not restored.closed
        offline = OfflineSettlementService(
            db=fx.db,
            config_provider=lambda: fx.config,
            wake_recorder=lambda **_: None,
            settlement_v2_module=settlement_v2,
        )
        offline.settle_offline_gap(fx.life, fx.planner, engine_claimed=True)
        pre, snap, _, _ = fx.tick(restart)
        control = (
            before
            + settlement_v2.sleep_state_gains({"energy": before}, 6, settlement_v2.V2_PARAMS, 0)[
                "energy"
            ]
        )
        assert (
            round(gap) == 21600
            and pre["continuous_minutes"] == 360
            and pre["rates"]["elapsed_sleep_minutes"] == 0
        )
        assert abs(snap["energy"] - control) < 0.0001 and control > before
        fx.load_life()
        fx.engine.restore(restart)
        assert abs(fx.life.snapshot()["energy"] - control) < 0.0001
    finally:
        fx.shutdown()


def commitments(root, case):
    day = at("2026-11-17T22:40:00")
    fx = Fixture(root, day)
    try:
        plugin = NS(config=fx.config, _db=fx.db, _data_dir=None)
        gen = ScheduleGenerator(fx.db, None, plugin=plugin, goal_manager=fx.gm)
        service = ToolsService(plugin)
        service._get_generator = lambda: gen
        service._schedule_query = lambda: NS(db=fx.db)
        service._make_tz_manager = lambda: fx.clock
        service._advanced_cfg = lambda: NS(
            role_judge_enabled=True, role_judge_temperature=0.3, generation_timeout=10
        )
        plugin._llm = NS(generate=None)

        async def judge(_prompt, **_kwargs):
            future = plugin.decision == "future"
            return json.dumps(
                {
                    "decision": plugin.decision,
                    "date": "2026-11-18" if future else "2026-11-17",
                    "time": "10:00" if future else "23:10",
                    "title": "会议",
                }
            )

        plugin._llm.generate = judge

        def ordinary(date):
            return [gen._spine_row(date, "读书", 480, 600, "work", "medium", 20, 60)]

        for date in ("2026-11-17", "2026-11-18"):
            gen.apply_schedule_items(ordinary(date), date)
        plugin.decision = "today"
        with patch("planning.tools_service.get_goal_manager", return_value=fx.gm):
            today_result = loop.run_until_complete(
                service.update_schedule(description="今天23:10约好开会", intent={"hardness": "hard"})
            )
            assert today_result["type"] == "schedule_intent_updated", today_result
            today_rows = fx.db.get_day_schedule("2026-11-17")
            assert len(today_rows) == 2
            today_g = fx.gm.get_pending_commitments("2026-11-17")
            today_wake = fx.planner.sleep_hard_commitment_start(fx.clock.get_now())
            if case == "today_commitment":
                assert len(today_g) == 1 and today_wake is not None and today_wake.minute == 10
            gen.apply_schedule_items(ordinary("2026-11-17"), "2026-11-17")
            after = fx.db.get_day_schedule("2026-11-17")
            if case == "today_commitment":
                assert [r["name"] for r in after].count("会议") == 1
                return
            plugin.decision = "future"
            fut = loop.run_until_complete(
                service.update_schedule(description="明天10点约好开会", commitment={"hardness": "hard"})
            )
        assert fut["type"] == "schedule_committed", fut
        auto = ScheduleAutoScheduler(plugin)
        assert loop.run_until_complete(auto._generate_today_schedule("2026-11-18"))
        future_rows = fx.db.get_day_schedule("2026-11-18")
        assert [r["name"] for r in future_rows].count("会议") == 1
        assert fx.planner.sleep_hard_commitment_start(at("2026-11-18T09:00:00")).hour == 10
        fx.clock.current = at("2026-11-18T10:00:00")
        fx.tick(fx.clock.current)
        fx.clock.current = at("2026-11-18T11:00:00")
        fx.tick(fx.clock.current)
        fx.gm.reconcile_commitments(at("2026-11-18T11:11:00"))
        outcomes = fx.db.pending_commitment_events()
        assert any(
            r["outcome"] == "kept" and r["payload"]["commitment_id"] == fut["goal_id"] for r in outcomes
        )
    finally:
        fx.shutdown()


def dst(root, case):
    zone = ZoneInfo("America/New_York")
    data = {}
    for day in ("2026-03-08", "2026-11-01"):
        a, b = cycle_state.day_cycle_epochs(day, 7 * 60, 23 * 60, zone.key)
        data[day] = [datetime.fromtimestamp(v, zone).strftime("%H:%M %z") for v in (a, b)]
    spring_a = datetime(2026, 3, 8, 1, 55, tzinfo=zone)
    spring_b = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    autumn_a = datetime(2026, 11, 1, 1, 55, tzinfo=zone, fold=0)
    autumn_b = datetime(2026, 11, 1, 1, 0, tzinfo=zone, fold=1)
    fx = Fixture(root, spring_a)
    try:
        data["integrator_minutes"] = []
        for a, b in ((spring_a, spring_b), (autumn_a, autumn_b)):
            fx.life._last_tick_at = a
            data["integrator_minutes"].append(
                {"real": elapsed_seconds(b, a) / 60, "booked": fx.life._dt_minutes(b)}
            )
        if case == "dst_clock":
            assert all(
                [value.split(" ")[0] for value in data[date]] == ["07:00", "23:00"]
                for date in ("2026-03-08", "2026-11-01")
            )
        else:
            assert data["integrator_minutes"] == [{"real": 5, "booked": 5}, {"real": 5, "booked": 5}]
    finally:
        fx.shutdown()


def cross_midnight_resume(root):
    fx = Fixture(root, at("2026-11-18T00:05:00"))
    try:
        parent = fx.db.start_episode("2026-11-17", "继续看书", "work", "rolling", 1420, 1460)
        fx.db.pause(parent, actual_end_minute=1435)
        snap = fx.life.snapshot()
        cand = next(
            c for c in fx.planner._build_candidates("2026-11-18", 5, snap, None) if c["kind"] == "resume"
        )
        ok = fx.planner._start_episode_for_candidate(
            cand,
            25,
            "2026-11-18",
            5,
            fx.clock.get_now(),
            snap,
            [],
            fx.planner._day_rng("2026-11-18"),
            {"actions": []},
        )
        assert ok
        row = fx.db.active_episode()
        child = row["episode_id"]
        frame = fx.planner._episode_frame_minute(row, at("2026-11-18T00:10:00"))
        fx.planner.tick(at("2026-11-18T00:10:00"), life_snapshot=fx.life.snapshot())
        after = fx.db.get_episode(child)
        assert row["schedule_date"] == "2026-11-18" and frame == 10 and after["status"] == "active"
        fx.planner.tick(at("2026-11-18T00:30:00"), life_snapshot=fx.life.snapshot())
        assert fx.db.get_episode(child)["status"] == "completed"
    finally:
        fx.shutdown()


def role_outputs(root):
    from core import llm_templates

    d = "2026-11-17"
    fx = Fixture(root, at("2026-11-17T09:00:00"))
    try:
        plug = NS(config=fx.config, _db=fx.db, _data_dir=None)
        svc = ToolsService(plug)
        svc._make_tz_manager = lambda: fx.clock
        svc._advanced_cfg = lambda: NS(
            role_judge_enabled=True, role_judge_temperature=0.3, generation_timeout=10
        )
        result = {}

        async def fake(*a, **k):
            raw = json.dumps(response, ensure_ascii=False)
            llm_templates.validate(raw, "role_judge")
            return raw

        plug._llm = NS(generate=fake)
        with (
            patch("planning.tools_service.get_goal_manager", return_value=fx.gm),
            patch.object(llm_templates, "PLUGIN_ROOT", root),
        ):
            for field in ("raw_date", "date"):
                response = {
                    "decision": "future",
                    "title": "约定 " + field,
                    "time": "10:00",
                    field: "大后天",
                }
                out = loop.run_until_complete(svc.update_schedule(description="大后天10点约好开会"))
                actual = fx.gm.get_goal(out["goal_id"]).parameters["commitment_date"]
                result[field] = {
                    "ack": out["effective_time"],
                    "persisted_date": actual,
                    "expected_date": "2026-11-20",
                }
        assert all(
            v["persisted_date"] == "2026-11-20" and v["ack"].startswith("2026-11-20")
            for v in result.values()
        )
        return result
    finally:
        fx.shutdown()


def delete_schedule(root, case):
    fx = Fixture(root, at("2026-11-17T08:00:00"))
    try:
        plug = NS(config=fx.config, _data_dir=None)
        gen = ScheduleGenerator(fx.db, None, plugin=plug, goal_manager=fx.gm)
        date = "2026-11-17"
        gen.apply_schedule_items(
            [gen._spine_row(date, "读书", 540, 600, "work", "medium", 15, 60)], date
        )
        cmd = CommandService(plug)
        fx.config.schedule.enable_detailed_description = False
        cmd._make_tz_manager = lambda: fx.clock
        messages = []

        async def capture(_sid, message):
            messages.append(message)

        cmd._send = capture
        pending = fx.gm.add_pending_commitment("2026-11-18", "明日重要约定", time="10:00")
        with patch("planning.command_service.get_goal_manager", return_value=fx.gm):
            listing = cmd._build_status_text(cmd._get_today_schedule_goals(fx.gm))
            loop.run_until_complete(cmd._handle_delete("fixture", ["plan", "delete", "1"]))
            if case == "delete_index":
                assert (
                    "读书" in listing
                    and "读书" in messages[-1]
                    and fx.gm.get_goal(pending.goal_id) is not None
                )
                assert not fx.db.get_day_schedule(date)
                return
            for form, when in (("goal_id", "10:00"), ("item_id", "14:00")):
                goal = fx.gm.add_pending_commitment(date, "约定 " + form, time=when)
                gen.apply_schedule_items(fx.db.get_day_schedule(date), date)
                item_id = "commitment:" + goal.goal_id
                chosen = goal.goal_id if form == "goal_id" else item_id
                loop.run_until_complete(cmd._handle_delete("fixture", ["plan", "delete", chosen]))
                still_goal = fx.db.get_goal(goal.goal_id) is not None
                still_item = fx.db.get_schedule_item(item_id) is not None
                if form == "goal_id":
                    hard = fx.planner.sleep_hard_commitment_start(fx.clock.get_now())
                    assert not still_item and hard is None
                    assert not still_goal or fx.db.get_goal(goal.goal_id)["status"] == "cancelled"
                else:
                    fx.gm.reconcile_commitments(at("2026-11-17T15:11:00"))
                    assert not still_item and not fx.db.pending_commitment_events()
                    assert not still_goal or fx.db.get_goal(goal.goal_id)["status"] == "cancelled"
    finally:
        fx.shutdown()


def late_start(root):
    fx = Fixture(root, at("2026-11-17T10:50:00"))
    try:
        date = "2026-11-17"
        g = fx.gm.add_pending_commitment(date, "约好的会议", time="10:00")
        gen = ScheduleGenerator(fx.db, None, goal_manager=fx.gm)
        gen.apply_schedule_items([], date)
        fx.planner.tick(fx.clock.get_now(), life_snapshot=fx.life.snapshot())
        row = fx.db.active_episode()
        assert row["metadata"]["commitment_id"] == g.goal_id
        fx.clock.current = at("2026-11-17T11:00:00")
        fx.planner.tick(fx.clock.get_now(), life_snapshot=fx.life.snapshot())
        final = fx.db.get_episode(row["episode_id"])
        assert (
            final["actual_start_minute"] == 650
            and final["actual_end_minute"] == 660
            and final["status"] == "completed"
        )
    finally:
        fx.shutdown()


def terminal_receipt(root):
    fx = Fixture(root, at("2026-11-17T10:00:00"))
    try:
        eid = fx.db.start_episode(
            "2026-11-17",
            "点心",
            "meal",
            "rolling",
            595,
            600,
            metadata={"state_deltas": {"hunger": -0.12}, "event_key": "snack"},
        )
        before = fx.life.snapshot()["hunger"]
        with patch.object(fx.life, "save", return_value=False):
            fx.planner._finish_episode(
                fx.db.active_episode(),
                fx.clock.get_now(),
                fx.life.snapshot(),
                actual_end_minute=600,
                reason="planned_end_reached",
            )
        status = fx.db.get_episode(eid)["status"]
        fx.life.save()
        fx.load_life()
        fx.planner.tick(at("2026-11-17T10:01:00"), life_snapshot=fx.life.snapshot())
        last_meal = fx.life.snapshot()["last_meal_at"]
        assert status == "completed" and eid in fx.life._episode_receipts
        assert abs(fx.life.snapshot()["hunger"] - (before - 0.12)) < 0.00001 and last_meal.startswith(
            "2026-11-17T10:00"
        )
        fx.planner.tick(at("2026-11-17T10:02:00"), life_snapshot=fx.life.snapshot())
        assert abs(fx.life.snapshot()["hunger"] - (before - 0.12)) < 0.00001
    finally:
        fx.shutdown()


def emotion_fixture(root):
    root.mkdir(exist_ok=True)
    plug = NS(
        config=NS(
            emotion=NS(enabled=True),
            schedule_advanced=NS(timezone="Asia/Shanghai"),
            schedule=NS(sleep_time="23:00", wake_time="07:00", enabled=True),
        ),
        _data_dir=root,
        _plugin_root=root,
        ctx=NS(logger=logging.getLogger("fixture")),
    )
    return EmotionService(plug)


def day_receipt(root):
    from emotion import daily_settlement

    svc = emotion_fixture(root)
    when = datetime(2026, 11, 17, 23, 55)
    point = {
        "at": "2026-11-17T14:00:00+08:00",
        "valence": 0.35,
        "arousal": 0,
        "dominance": 0,
        "frozen": False,
    }

    def setup(service):
        service._daily_settlement_points = lambda: [point]
        service._daily_settlement_reserve = lambda now: (
            None
        )  # exercise offline rule path and preserve budget

    setup(svc)
    svc._save_daily_settlement_state("2026-11-16", "rule", 1, False)
    with patch.object(daily_settlement, "save_state", return_value=False):
        outcome = loop.run_until_complete(svc._tick_daily_settlement(when))
    first = json.loads((root / "emotion_state.json").read_text(encoding="utf-8"))["vad_raw"]["valence"]
    nxt = emotion_fixture(root)
    setup(nxt)
    second_out = loop.run_until_complete(nxt._tick_daily_settlement(when))
    second = json.loads((root / "emotion_state.json").read_text(encoding="utf-8"))["vad_raw"]["valence"]
    assert (
        outcome["mode"] == "rule"
        and second_out is None
        and abs(second - first) < 0.00001
        and first > 0.0
    )
    # Conversely, only the main state write fails, but the date is marked complete.
    other = root / "lost"
    third = emotion_fixture(other)
    setup(third)
    third._save_daily_settlement_state("2026-11-16", "rule", 1, False)
    third._save_state()
    with patch("emotion.service.save_json_atomic", return_value=False):
        loop.run_until_complete(third._tick_daily_settlement(when))
    restarted = emotion_fixture(other)
    setup(restarted)
    assert loop.run_until_complete(restarted._tick_daily_settlement(when)) is not None
    assert restarted._machine.vad_raw.valence > 0


def sleep_actual(root):
    begin = at("2026-11-17T23:40:00")
    woke = at("2026-11-18T05:20:00")
    fx = Fixture(root, begin)
    try:
        svc = emotion_fixture(root)
        plug = svc._plugin
        plug._sleep_engine = fx.engine
        plug._schedule_query = ScheduleQuery(
            fx.db,
            runtime_provider=LifeScheduleReadModel(
                fx.db,
                life_state=fx.life,
                planner=fx.planner,
                config_provider=fx.config,
                timezone_manager=fx.clock,
            ),
        )
        from companion.patrol import real_sleep_anchors_from_plugin

        plug.real_sleep_anchors = lambda now=None: real_sleep_anchors_from_plugin(plug, now)
        gen = ScheduleGenerator(fx.db, None, plugin=plug, goal_manager=fx.gm)
        gen.apply_schedule_items(
            [
                gen._spine_row(
                    "2026-11-17", "睡觉", 1380, 1860, "sleep", "high", 480, 480, hardness="hard"
                )
            ],
            "2026-11-17",
        )
        entries = []
        svc._log_decision = lambda action, **fields: entries.append((action, fields))

        async def fixed_referee(*args, **kwargs):
            return '{"delta_vad":{"valence":0.0,"arousal":0.0,"dominance":0.0}}'

        plug._llm = NS(generate=fixed_referee)

        class EmotionClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fx.clock.current.astimezone(tz) if tz else fx.clock.current.replace(tzinfo=None)

        with patch("emotion.service.datetime", EmotionClock):
            anchors = plug.real_sleep_anchors(begin)
            assert (
                anchors
                and anchors.start == "23:00"
                and anchors.end == "07:00"
                and anchors.source == "real_schedule"
            ), anchors
            assert fx.planner.force_sleep(begin)
            loop.run_until_complete(svc._check_and_overhaul())
            assert svc._machine.frozen and svc._pending_overhaul
            fx.clock.current = woke
            fx.gm.add_pending_commitment("2026-11-18", "重要约定", time="05:50")
            gen.apply_schedule_items(
                [gen._spine_row("2026-11-18", "读书", 540, 600, "work", "medium", 15, 60)], "2026-11-18"
            )
            hard = fx.planner.sleep_hard_commitment_start(woke)
            assert hard is not None and hard.hour == 5 and hard.minute == 50, hard
            pre = fx.engine.prepare_tick(
                woke, sleep_episode=fx.db.active_episode(), hard_commitment_start=hard
            )
            snap = fx.life.tick(woke, sleep_rates=pre["rates"])
            commit = fx.engine.commit_tick(woke, energy=snap["energy"])
            assert commit["woke"] and commit["reason"] == "hard_commitment", commit
            loop.run_until_complete(svc._check_and_overhaul())
        from emotion.state_machine import sleep_recovery_factor

        entry = next(
            r for action, r in entries if action == "emotion.sleep_overhaul" and "recovery_factor" in r
        )
        hours = commit["actual_sleep_hours"]
        assert abs(entry["sleep_hours"] - hours) < 0.001 and entry[
            "recovery_factor"
        ] == sleep_recovery_factor(hours)
    finally:
        fx.shutdown()


def offline_transactions(root):
    import sqlite3

    fx = Fixture(root, at("2026-11-18T06:05:00"))
    try:
        args = dict(
            receipt_id="offline:2026-11-18:5",
            wake_at=fx.clock.current,
            awake_minutes=10,
            effective_hours=6,
            sleep_debt_before=2.0,
            sleep_debt_after=1.0,
        )
        before = fx.life.snapshot()
        with patch.object(fx.life, "save", return_value=False):
            try:
                fx.life.apply_offline_sleep_once(**args)
            except OSError:
                pass
            else:
                raise AssertionError("receipt reported success after failed write")
        assert fx.life.snapshot() == before
        endpoints = fx.life.apply_offline_sleep_once(**args)
        settled = fx.life.snapshot()
        assert endpoints["energy_end"] > endpoints["energy_start"] and settled["sleep_debt"] == 1
        fx.load_life()
        assert fx.life.apply_offline_sleep_once(**args) == endpoints
        assert fx.life.snapshot()["energy"] == settled["energy"]
        previous = fx.db.start_episode("2026-11-17", "reading", "work", "rolling", 1435, 1450)
        row = {
            "session_id": "sleep:2026-11-18:5",
            "schedule_date": "2026-11-18",
            "planned_start_minute": 5,
            "planned_end_minute": 365,
            "actual_start_minute": 5,
            "actual_end_minute": 365,
            "wake_reason": "offline_reconnect",
            "continuous_minutes": 360,
            "energy_start": endpoints["energy_start"],
            "energy_end": endpoints["energy_end"],
            "created_at": "2026-11-18T00:05:00+08:00",
            "updated_at": "2026-11-18T06:05:00+08:00",
        }
        params = dict(episode_id=args["receipt_id"], label="offline sleep", metadata={})
        fx.db._conn.execute(
            "CREATE TEMP TRIGGER fail_offline BEFORE INSERT ON activity_episodes "
            "WHEN NEW.source='offline' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        try:
            fx.db.commit_offline_sleep(row, **params)
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("database accepted failed transaction")
        assert (
            fx.db.active_episode()["episode_id"] == previous
            and fx.db.get_sleep_session(row["session_id"]) is None
        )
        fx.db._conn.execute("DROP TRIGGER fail_offline")
        assert fx.db.commit_offline_sleep(row, **params)
        assert fx.db.get_episode(previous)["actual_end_minute"] == 1445
        live = fx.db.start_episode("2026-11-18", "current", "work", "rolling", 370, 390)
        assert fx.db.commit_offline_sleep(row, **params) and fx.db.active_episode()["episode_id"] == live
        assert len([r for r in fx.db.episodes_today("2026-11-18") if r["source"] == "offline"]) == 1
        now = at("2026-11-18T06:15:00")
        fx.life.apply_offline_awake_once(receipt_id="awake:fixture", minutes=10, observed_at=now)
        energy = fx.life.snapshot()["energy"]
        fx.life.apply_offline_awake_once(receipt_id="awake:fixture", minutes=10, observed_at=now)
        assert (
            fx.life.snapshot()["energy"] == energy
            and fx.life.snapshot()["last_sleep_at"] == settled["last_sleep_at"]
        )
    finally:
        fx.shutdown()


def sleep_retry_and_cap(root):
    begin = at("2026-11-17T23:00:00")
    fx = Fixture(root, begin)
    try:
        fx.db.start_episode("2026-11-17", "sleep", "sleep", "rolling", 1380, 2100)
        fx.engine.prepare_tick(begin, sleep_episode=fx.db.active_episode())
        fx.engine.commit_tick(begin)
        fx.clock.current = at("2026-11-18T05:00:00")
        fx.load_life()
        with patch.object(fx.life, "save", return_value=False):
            session = fx.engine.restore(fx.clock.current)
            assert session and not session.closed and fx.engine.recovery_pending
        fx.engine.prepare_tick(fx.clock.current, sleep_episode=fx.db.active_episode())
        assert not fx.engine.recovery_pending and fx.life.snapshot()["energy"] > 0.90
        fx.clock.current = at("2026-11-19T09:00:00")
        fx.load_life()
        closed = fx.engine.restore(fx.clock.current)
        assert closed.closed and closed.closed_at < fx.clock.current
        assert closed.metadata["actual_sleep_hours"] <= 14
        assert fx.life.pending_offline_gap_seconds is None
    finally:
        fx.shutdown()


def dst_sleep(root):
    zone = ZoneInfo("America/New_York")
    pairs = (
        (datetime(2026, 3, 8, 1, 55, tzinfo=zone), datetime(2026, 3, 8, 3, tzinfo=zone)),
        (
            datetime(2026, 11, 1, 1, 55, tzinfo=zone, fold=0),
            datetime(2026, 11, 1, 1, tzinfo=zone, fold=1),
        ),
    )
    root.mkdir()
    for index, (before, after) in enumerate(pairs):
        fx = Fixture(root / str(index), before)
        try:
            fx.db.start_episode(before.date().isoformat(), "sleep", "sleep", "rolling", 115, 420)
            fx.engine.prepare_tick(before, sleep_episode=fx.db.active_episode())
            fx.engine.commit_tick(before)
            fx.clock.current = after
            prepared = fx.engine.prepare_tick(after, sleep_episode=fx.db.active_episode())
            assert prepared["dt_minutes"] == 5 and prepared["continuous_minutes"] == 5
            assert abs(prepared["rates"]["elapsed_sleep_minutes"] - 5) < 1e-6
        finally:
            fx.shutdown()


def morning_receipt(root):
    fx = Fixture(root, at("2026-11-17T07:00:00"))
    try:
        day = fx.clock.get_now().date().isoformat()
        gen = ScheduleGenerator(fx.db, None, goal_manager=fx.gm)
        gen.apply_schedule_items([gen._spine_row(day, "work", 540, 660, "work", "medium", 30, 100)], day)
        args = dict(receipt_id="offline:morning", wake_at=fx.clock.get_now(), sleep_hours=4.5)
        assert fx.planner.apply_offline_morning_once(**args)
        assert fx.db.get_day_schedule(day)[0]["parameters"]["max_minutes"] == 60
        assert fx.planner.apply_offline_morning_once(**args)
        assert fx.db.get_day_schedule(day)[0]["parameters"]["max_minutes"] == 60
        with patch.object(cycle_state, "save", return_value=False):
            result = cycle_state.record_actual_wake(
                root,
                schedule_date=day,
                wake_epoch=fx.clock.get_now().timestamp(),
                cfg_wake="07:00",
                cfg_sleep="23:00",
                tz_name="Asia/Shanghai",
                now=fx.clock.get_now(),
            )
            assert not result.durable
    finally:
        fx.shutdown()


def concurrent_commitment(root):
    from concurrent.futures import ThreadPoolExecutor

    fx = Fixture(root, at("2026-11-17T09:00:00"))
    try:
        day = "2026-11-17"
        common = dict(
            notes="",
            reason="",
            source="typed:hard",
            duration=30,
            activity_type="work",
            priority="high",
            publish_today=True,
        )

        def accept(_index):
            return fx.db.accept_timed_commitment(day, "meeting", "10:00", **common)

        with ThreadPoolExecutor(max_workers=5) as pool:
            ids = list(pool.map(accept, range(8)))
        assert len(set(ids)) == 1 and len(fx.db.get_day_schedule(day)) == 1
        second = fx.db.accept_timed_commitment(day, "meeting", "15:00", **common)
        assert second != ids[0]
        stale = fx.db.get_day_schedule(day)
        assert fx.gm.delete_goal(ids[0])
        gen = ScheduleGenerator(fx.db, None, goal_manager=fx.gm)
        gen.apply_schedule_items(stale, day)
        assert [r["parameters"]["commitment_id"] for r in fx.db.get_day_schedule(day)] == [second]
        fx.clock.current = at("2026-11-17T15:05:00")
        fx.planner.tick(fx.clock.current, life_snapshot=fx.life.snapshot())
        assert fx.db.active_episode()["metadata"]["commitment_id"] == second
        assert fx.gm.delete_goal("commitment:" + second)
        assert fx.db.active_episode() is None and not fx.db.pending_commitment_events()
    finally:
        fx.shutdown()


CASES = {
    "dst_sleep": dst_sleep,
    "morning_receipt": morning_receipt,
    "offline_transactions": offline_transactions,
    "sleep_retry_and_cap": sleep_retry_and_cap,
    "concurrent_commitment": concurrent_commitment,
    "no_sleep_mode": no_sleep_case,
    "restored_sleep": restored_sleep,
    "today_commitment": lambda root: commitments(root, "today_commitment"),
    "future_commitment": lambda root: commitments(root, "future_commitment"),
    "dst_clock": lambda root: dst(root, "dst_clock"),
    "dst_elapsed": lambda root: dst(root, "dst_elapsed"),
    "midnight_resume": cross_midnight_resume,
    "future_date": role_outputs,
    "delete_index": lambda root: delete_schedule(root, "delete_index"),
    "cancel_commitment": lambda root: delete_schedule(root, "cancel_commitment"),
    "actual_commitment": late_start,
    "terminal_receipt": terminal_receipt,
    "daily_emotion": day_receipt,
    "actual_sleep": sleep_actual,
}

if __name__ == "__main__":
    name = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix="lbm-planning-", dir=work) as tmp:
        root = Path(tmp)
        internal_tuning._PLUGIN_ROOT = root
        CASES[name](root / "case")
    loop.close()
    temporary_workspace.cleanup()
