"""Architecture regression cases. Run directly with Python or discover with unittest."""

from __future__ import annotations
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("CATEYE_TEST_DIR", HERE.parent)).resolve()


class ArchitectureCases(unittest.TestCase):
    def test_module_config_sync(self):
        self.run_case("case_module_config_sync")

    def test_four_failed_ticks_hold_business_progress(self):
        self.run_case("case_four_failed_ticks_hold_business_progress")

    def test_sleep_tick_retry_respects_boundary(self):
        self.run_case("case_sleep_tick_retry_respects_boundary")

    def test_gateway_imports_share_patches(self):
        self.run_case("case_gateway_imports_share_patches")

    def test_trusted_command_policy(self):
        self.run_case("case_trusted_command_policy")

    def test_offline_failure_replay_and_dst(self):
        self.run_case("case_offline_failure_replay_and_dst")

    def test_no_sleep_gap_is_idempotent(self):
        self.run_case("case_no_sleep_gap_is_idempotent")

    def test_morning_failure_is_recoverable(self):
        self.run_case("case_morning_failure_is_recoverable")

    def test_engine_claim_survives_retry(self):
        self.run_case("case_engine_claim_survives_retry")

    def test_life_receipt_then_journal_retry(self):
        self.run_case("case_life_receipt_then_journal_retry")

    def test_usage_window_is_instant_based(self):
        self.run_case("case_usage_window_is_instant_based")

    def test_failed_screen_receipt(self):
        self.run_case("case_failed_screen_receipt")

    def test_partial_init_does_not_skip_upgrade(self):
        self.run_case("case_partial_init_does_not_skip_upgrade")

    def test_registry_failure_report(self):
        self.run_case("case_registry_failure_report")

    def test_upgrade_invalidation_failure_preserves_version(self):
        self.run_case("case_upgrade_invalidation_failure_preserves_version")

    def test_day_session_boundary(self):
        self.run_case("case_day_session_boundary")

    def test_offline_sleep_labels(self):
        self.run_case("case_offline_sleep_labels")

    def test_separate_energy_and_social_tones(self):
        self.run_case("case_separate_energy_and_social_tones")

    def test_dashboard_image_references(self):
        self.run_case("case_dashboard_image_references")

    def test_screen_registration_refresh(self):
        self.run_case("case_screen_registration_refresh")

    def test_component_metadata_before_context(self):
        self.run_case("case_component_metadata_before_context")

    def test_reload_waiter_cancellation(self):
        self.run_case("case_reload_waiter_cancellation")

    def test_changed_target_rejects_old_route(self):
        self.run_case("case_changed_target_rejects_old_route")

    def run_case(self, name):
        proc = subprocess.run(
            [sys.executable, "-I", "-B", str(Path(__file__).resolve()), name],
            capture_output=True,
            text=True,
            timeout=40,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


async def case_module_config_sync(root):
    from plugin import CateyeSuitePlugin

    p = object.__new__(CateyeSuitePlugin)
    p.config = NS(
        plugin=NS(enabled=True),
        schedule=NS(enabled=True),
        emotion=NS(enabled=True),
        patrol=NS(enabled=False, target_qq=""),
        qzone=NS(enabled=True),
        video_watch=NS(enabled=False),
        llm=NS(),
    )
    p.ctx = NS(logger=Mock(), config=NS(get=AsyncMock(return_value=[])))
    p._video_watch = NS(stop=AsyncMock())
    p._companion = NS(sync_from_config=Mock(), sync_background=AsyncMock(), patrol=NS(start=Mock()))
    p._planning = NS(sync_background=AsyncMock(), sync_event_catalog=AsyncMock())
    p._emotion = NS(sync_from_config=Mock(), sync_background=AsyncMock())
    p._qzone = NS(start=Mock(), stop=AsyncMock())
    p._llm = Mock()
    p._bg_started = True
    p._bg_tasks = []
    p._resolve_target_stream = AsyncMock(return_value="private-test")
    for name in (
        "_apply_target",
        "_setup_life_services",
        "_sync_life_settlement",
        "_sync_sleep_engine",
        "_ensure_life_tick_task",
    ):
        setattr(p, name, Mock())
    p.flush_target_frequency_calibration = AsyncMock()
    p._calibrate_target_frequency = AsyncMock()
    await p.on_config_update("self", {}, "version")
    p._planning.sync_background.assert_awaited_once()
    p._emotion.sync_background.assert_awaited_once()
    p._companion.sync_background.assert_awaited_once()
    p._qzone.start.assert_called_once()
    p._companion.patrol.start.assert_not_called()


async def case_gateway_imports_share_patches(root):
    import ast
    import tools.llm_gateway as legacy
    import core.llm_gateway as current
    from core.llm_utils import LLMService

    assert legacy is current
    assert legacy.LLMGateway is current.LLMGateway
    assert legacy.receipt_of is current.receipt_of
    assert legacy.record_response is current.record_response
    assert legacy.LLMReceipt is current.LLMReceipt
    for source in (SOURCE / "core").rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8-sig"))):
            assert not (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "tools")
            if isinstance(node, ast.Import):
                assert all(name.name.split(".")[0] != "tools" for name in node.names)
    ctx = NS(
        paths=NS(data_dir=root),
        logger=Mock(),
        llm=NS(generate=AsyncMock(return_value={"success": True, "response": "ready", "total_tokens": 3})),
    )
    service = LLMService(ctx)
    replacement = Mock()
    with patch.object(legacy, "LLMGateway", return_value=replacement):
        assert service._gateway() is replacement
    gateway = current.LLMGateway(ctx)
    with patch.object(legacy, "record_response") as record:
        result = await gateway.generate_once("fixture", module="planning")
        assert result.ok and result.total_tokens == 3
        record.assert_called_once()


async def case_trusted_command_policy(root):
    from tools.command_components import CommandRootComponentsMixin
    from core.command_dispatcher import DISPATCHER
    from companion.components import CompanionComponentsMixin

    class Fake(CommandRootComponentsMixin, CompanionComponentsMixin):
        async def cmd_status_dashboard(self, *_args, **kwargs):
            calls.append(kwargs)
            return True, "verified", True

    p = Fake()
    p.config = NS(admin=NS(admin_users=["qq:10203"], allowed_streams=["s-one"]), patrol=NS(target_qq=""))
    p.ctx = NS(send=NS(text=AsyncMock()), logger=Mock())
    params = {"matched_groups": {"root_args": "status dashboard"}}
    calls = []
    fixtures = [
        ("qq", "10203", "s-one", False, True),
        ("qq", "10203", "s-other", False, False),
        ("telegram", "10203", "s-one", False, False),
        ("qq", "10000", "s-one", False, False),
        ("", "10203", "s-one", False, False),
        ("console", "", "s-other", True, True),
    ]
    for platform, user, stream, local, allowed in fixtures:
        before = len(calls)
        await p._cmd_root_entry(
            "en",
            {
                **params,
                "platform": platform,
                "user_id": user,
                "stream_id": stream,
                "is_local_operator": local,
            },
        )
        assert (len(calls) > before) == allowed, (platform, stream, allowed)
    p.config.admin.allowed_streams = ["qq:private:10203"]
    before = len(calls)
    await p._cmd_root_entry(
        "en",
        {
            **params,
            "platform": "qq",
            "user_id": "10203",
            "group_id": "group-test",
            "stream_id": "group-session",
        },
    )
    assert len(calls) == before
    assert DISPATCHER.parse("en", "help").kind == "help"

    p.config.plugin = NS(enabled=True)
    p.config.video_watch = NS(enabled=True)
    test_share = AsyncMock(return_value={"success": True})
    test_planner = AsyncMock(return_value={"success": True, "content": "Planner 分享判断调试已提交。"})
    test_confirm = AsyncMock()
    p._recent_video_share_service = lambda: NS(
        share_debug=test_share, share_debug_planner=test_planner, share_debug_ticket=test_confirm,
    )
    kwargs = {
        "matched_groups": {"root_args": "运维 分享 调试"},
        "platform": "qq", "user_id": "10203", "stream_id": "s-one",
        "message_id": "incorrect-top-level-id", "message": {"message_id": "actual-ingress-id"},
    }
    await p._cmd_root_entry("cn", {**kwargs, "user_id": "10456"})
    await p._cmd_root_entry("cn", {**kwargs, "group_id": "198765"})
    test_share.assert_not_awaited()
    await p._cmd_root_entry("cn", kwargs)
    test_share.assert_awaited_once_with("s-one", "actual-ingress-id", group_id="", user_id="10203")
    await p._cmd_root_entry("en", {**kwargs, "matched_groups": {"root_args": "ops share debug 198765"}})
    assert test_share.await_count == 1
    planner_args = {**kwargs, "matched_groups": {"root_args": "ops share debug planner"}}
    await p._cmd_root_entry("en", {**planner_args, "user_id": "10456"})
    test_planner.assert_not_awaited()
    await p._cmd_root_entry("en", planner_args)
    assert test_planner.await_args.args == ("s-one", "actual-ingress-id")
    assert callable(test_planner.await_args.kwargs["trigger"])
    p.ctx.send.text.assert_awaited_with("Planner 分享判断调试已提交。", "s-one",
                                       storage_message=False, sync_to_maisaka_history=False)
    assert test_share.await_count == 1

    p.ctx.send.text.reset_mock()
    test_confirm.return_value = {"success": False, "content": "未授权"}
    failed = await p.mai_tool_confirm_debug_share(ticket="fake", stream_id="s-one", chat_id="s-one")
    assert not failed["success"] and not p.ctx.send.text.await_count
    test_confirm.return_value = {"debug_executed": True, "success": False, "delivery_state": "uncertain",
                                 "content": "QQ 未返回送达回执。"}
    result = await p.mai_tool_confirm_debug_share(ticket="valid", stream_id="s-one", chat_id="s-one")
    assert result["success"] and result["stop_after_execution"] and result["delivery_state"] == "uncertain"
    p.ctx.send.text.assert_awaited_once_with("QQ 未返回送达回执。", "s-one",
                                            storage_message=False, sync_to_maisaka_history=False)


async def case_offline_failure_replay_and_dst(root):
    from zoneinfo import ZoneInfo
    from planning.schedule_db import ScheduleDB
    from planning.life_state import LifeStateService
    from planning import settlement_v2
    from planning.time_utils import TimezoneManager
    from suite_offline_settlement import OfflineSettlementService

    config = NS(schedule_advanced=NS(timezone="America/New_York"))
    eastern = ZoneInfo(config.schedule_advanced.timezone)
    wake = datetime(2026, 11, 1, 2, 30, tzinfo=eastern)
    p = Mock()
    bus = Mock()
    db = ScheduleDB(root / "schedule.db")
    try:
        kwargs = dict(
            db=db,
            config_provider=lambda: config,
            wake_recorder=Mock(),
            event_bus_provider=lambda: bus,
            logger_provider=lambda: Mock(),
            settlement_v2_module=settlement_v2,
        )
        s = OfflineSettlementService(**kwargs, data_dir=root)
        life = LifeStateService(root / "life_state.json", timezone_str=config.schedule_advanced.timezone)
        life._pending_offline_gap_seconds = 10800
        db._conn.execute("PRAGMA query_only=ON")
        with patch.object(TimezoneManager, "get_now", return_value=wake):
            assert s.settle_offline_gap(life, p, engine_claimed=False) is False
        assert db._conn.execute("SELECT count(*) FROM sleep_sessions").fetchone()[0] == 0
        assert not bus.publish_soon.called
        db._conn.execute("PRAGMA query_only=OFF")
        s = OfflineSettlementService(**kwargs, data_dir=root)
        with patch.object(TimezoneManager, "get_now", return_value=wake):
            life = LifeStateService(root / "life_state.json", timezone_str=config.schedule_advanced.timezone)
            assert s.settle_offline_gap(life, p, engine_claimed=False) is True
            assert s.settle_offline_gap(life, p, engine_claimed=False) is True
        sessions = db._conn.execute(
            "SELECT schedule_date,actual_start_minute,continuous_minutes FROM sleep_sessions"
        ).fetchall()
        assert [tuple(r) for r in sessions] == [("2026-11-01", 30, 180)], sessions
        assert (
            db._conn.execute(
                "SELECT count(*) FROM activity_episodes WHERE source='offline' AND status='completed'"
            ).fetchone()[0]
            == 1
        )
        assert bus.publish_soon.call_count == 1
    finally:
        db.close()


async def case_usage_window_is_instant_based(root):
    from core.llm_usage import LLMUsageTracker
    from core.generation_evidence import record_outcome
    from core.monitoring import usage_summary

    local = datetime.now().astimezone()
    shifted = timezone(local.utcoffset() - timedelta(hours=3))
    tracker = LLMUsageTracker(root)
    tracker.record_v2(
        module="emotion",
        event="emotion_evaluate",
        success=True,
        usage_available=True,
        usage_present=True,
        total_tokens=18,
        request_id="fixture",
    )
    record_outcome(
        root,
        {
            "module": "emotion",
            "event": "emotion_evaluate",
            "request_id": "fixture",
            "level": "info",
            "category": "ok",
        },
    )
    now = datetime.now().astimezone() + timedelta(seconds=1)
    for instant in (now, now.astimezone(shifted), now.astimezone(timezone.utc)):
        view = usage_summary(root, instant, 24, include_rows=True)
        assert (view["calls"], view["tokens"], view["generation_logs_total"]) == (1, 18, 1)
    # Old plugin journals used host-local wall time without an offset.
    file = root / "llm_usage_events.jsonl"
    row = json.loads(file.read_text(encoding="utf-8").strip())
    row["ts"] = now.astimezone().replace(tzinfo=None, microsecond=0).isoformat()
    file.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert usage_summary(root, now.astimezone(shifted), 24)["calls"] == 1


async def case_engine_claim_survives_retry(root):
    from plugin import CateyeSuitePlugin

    p = object.__new__(CateyeSuitePlugin)
    p._offline_work_pending = True
    p._offline_engine_claimed = True
    p._sleep_engine = NS(recovery_pending=True)
    p._rolling_planner = Mock()
    p._life_state = Mock()
    p._settle_offline_gap = Mock(return_value=False)
    p._life_tick_once()
    p._settle_offline_gap.assert_called_once_with(p._life_state, p._rolling_planner, engine_claimed=True)
    p._rolling_planner.tick.assert_not_called()


async def case_four_failed_ticks_hold_business_progress(root):
    from plugin import CateyeSuitePlugin

    p = object.__new__(CateyeSuitePlugin)
    p.ctx = NS(logger=Mock())
    p.config = NS(life_state=NS())
    p._rolling_planner = NS(tick=Mock(return_value={}))
    p._sleep_engine = NS(recovery_pending=False)
    p._sleep_prepare = Mock(return_value={"asleep": True, "rates": {"elapsed_sleep_minutes": 5}})
    p._life_tick_state = Mock(return_value=None)
    p._rollback_sleep_tick = Mock(return_value=True)
    p._sleep_commit = Mock(return_value={})
    for name in (
        "_kick_video_watch_sync",
        "_kick_share_delivery",
        "_kick_qzone_video_eval",
        "_kick_preference_refresh",
    ):
        setattr(p, name, Mock())
    for _ in range(4):
        p._life_tick_once()
    assert p._rollback_sleep_tick.call_count == 4
    p._sleep_commit.assert_not_called()
    p._rolling_planner.tick.assert_not_called()
    p._kick_share_delivery.assert_not_called()
    p._life_tick_state.return_value = {"energy": 0.8}
    p._life_tick_once()
    p._sleep_commit.assert_called_once()
    p._rolling_planner.tick.assert_called_once()
    assert p._sleep_tick_failures == 0


async def case_sleep_tick_retry_respects_boundary(root):
    from zoneinfo import ZoneInfo
    from planning.life_state import LifeStateService
    from planning.schedule_db import ScheduleDB
    from planning.sleep_engine import SleepEngine
    from planning.rolling_planner import RollingPlanner
    from planning.time_utils import TimezoneManager
    from suite_config import CateyeSuiteConfig
    from plugin import CateyeSuitePlugin

    zone = ZoneInfo("Asia/Shanghai")
    beginning = datetime(2026, 11, 17, 23, tzinfo=zone)
    boundary = datetime(2026, 11, 18, 7, tzinfo=zone)
    hard = boundary + timedelta(minutes=30)

    class Clock(TimezoneManager):
        def __init__(self):
            super().__init__("Asia/Shanghai")
            self.current = beginning

        def get_now(self):
            return self.current

    def fixture(path, known=True):
        path.mkdir()
        clock = Clock()
        cfg = CateyeSuiteConfig()
        db = ScheduleDB(path / "schedule.db")
        life = LifeStateService(path / "life.json", timezone_manager=clock)
        life._sleep_debt = 2.0
        life.tick(beginning)
        bus = Mock()
        engine = SleepEngine(
            config_provider=cfg, session_store=db, life_state=life, timezone_manager=clock, event_bus=bus
        )
        planner = RollingPlanner(
            db, life, sleep_engine=engine, config_provider=cfg, timezone_manager=clock, data_dir=path
        )
        engine._episode_closer = planner.close_sleep_episode
        planner.sleep_hard_commitment_start = Mock(return_value=hard)
        planner.tick = Mock(return_value={})
        planner.apply_morning_spine_adjustment = Mock()
        db.start_episode("2026-11-17", "sleep", "sleep", "rolling", 1380, 1920)
        engine.prepare_tick(
            beginning, sleep_episode=db.active_episode(), hard_commitment_start=hard if known else None
        )
        engine.commit_tick(beginning)
        p = object.__new__(CateyeSuitePlugin)
        p.ctx = NS(logger=Mock())
        p.config = cfg
        p._db = db
        p._life_state = life
        p._rolling_planner = planner
        p._sleep_engine = engine
        p._record_actual_wake_cycle = Mock(return_value=True)
        for name in (
            "_kick_video_watch_sync",
            "_kick_share_delivery",
            "_kick_qzone_video_eval",
            "_kick_preference_refresh",
        ):
            setattr(p, name, Mock())
        return p, clock, bus

    p, clock, bus = fixture(root / "failed")
    control, control_clock, _ = fixture(root / "control")
    late, late_clock, _ = fixture(root / "late", known=False)
    learned, learned_clock, _ = fixture(root / "learned", known=False)
    try:
        before = p._life_state.snapshot()
        with patch.object(p._life_state, "save", return_value=False):
            for offset in (-2, 0, 5, 10):
                clock.current = boundary + timedelta(minutes=offset)
                p._life_tick_once()
        assert p._life_state.snapshot() == before
        assert not p._sleep_engine._session.closed
        p._rolling_planner.tick.assert_not_called()
        assert all(call.args[0] != "life.sleep_woke" for call in bus.publish_soon.call_args_list)
        clock.current = boundary + timedelta(minutes=20)
        p._life_tick_once()
        assert p._sleep_engine._session.closed
        assert p._sleep_engine._session.closed_at == boundary
        control_clock.current = boundary
        control._life_tick_once()
        control_clock.current = clock.current
        control._life_tick_once()
        assert p._sleep_engine._session.energy_end == control._sleep_engine._session.energy_end
        assert p._sleep_engine._session.energy_end > p._life_state.snapshot()["energy"]
        for field in ("energy", "focus", "stress", "social_battery", "hunger", "sleep_debt"):
            assert abs(p._life_state.snapshot()[field] - control._life_state.snapshot()[field]) < 1e-7, field
        settled = p._life_state.snapshot()
        p._life_tick_once()
        assert p._life_state.snapshot() == settled
        assert sum(call.args[0] == "life.sleep_woke" for call in bus.publish_soon.call_args_list) == 1
        with patch.object(learned._life_state, "save", return_value=False):
            for offset in (-2, 0, 5, 10):
                learned_clock.current = boundary + timedelta(minutes=offset)
                learned._life_tick_once()
        learned_clock.current = clock.current
        learned._life_tick_once()
        assert learned._sleep_engine._session.closed_at == boundary
        assert learned._sleep_engine._session.energy_end == control._sleep_engine._session.energy_end
        for field in ("energy", "focus", "stress", "social_battery", "hunger", "sleep_debt"):
            assert abs(learned._life_state.snapshot()[field] - control._life_state.snapshot()[field]) < 1e-7
        first_observed = boundary + timedelta(minutes=20)
        late_clock.current = first_observed
        with patch.object(late._life_state, "save", return_value=False):
            late._life_tick_once()
        late_clock.current += timedelta(minutes=5)
        late._life_tick_once()
        assert late._sleep_engine._session.closed_at == first_observed
    finally:
        p._db.close()
        control._db.close()
        late._db.close()
        learned._db.close()


async def case_changed_target_rejects_old_route(root):
    from plugin import CateyeSuitePlugin
    from core.targeting import private_qq_stream

    p = object.__new__(CateyeSuitePlugin)
    p.config = NS(patrol=NS(target_qq="10203"))
    p._apply_target = Mock()
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def lookup():
        entered.set()
        await resume.wait()
        return "old-private-stream"

    p._resolve_target_stream = lookup
    task = asyncio.create_task(p._refresh_target_binding())
    await entered.wait()
    p.config = NS(patrol=NS(target_qq="40506"))
    resume.set()
    assert await task == ""
    p._apply_target.assert_not_called()
    assert private_qq_stream({"stream_id": "s", "platform": "telegram", "user_id": "10203"}, "10203") == ""
    assert private_qq_stream({"stream_id": "s", "group_id": "g", "user_id": "10203"}, "10203") == ""
    assert private_qq_stream({"stream_id": "s", "user_id": "40506"}, "10203") == ""
    assert private_qq_stream({"stream_id": "s", "user_id": "10203", "platform": "qq"}, "10203") == "s"


async def case_no_sleep_gap_is_idempotent(root):
    from planning.schedule_db import ScheduleDB
    from planning.life_state import LifeStateService
    from planning.time_utils import TimezoneManager
    from suite_offline_settlement import OfflineSettlementService

    clock = datetime.now(timezone.utc).replace(microsecond=0)
    cfg = NS(schedule=NS(no_sleep_mode=True), schedule_advanced=NS(timezone="UTC"))
    db = ScheduleDB(root / "state.db")
    try:
        service = OfflineSettlementService(
            db=db, data_dir=root, config_provider=lambda: cfg, wake_recorder=Mock()
        )
        with patch.object(TimezoneManager, "get_now", return_value=clock):
            life = LifeStateService(root / "life.json", timezone_str="UTC")
            life._pending_offline_gap_seconds = 3600
            assert service.settle_offline_gap(life, Mock(), engine_claimed=False)
            previous = life.snapshot()
            life._pending_offline_gap_seconds = 3600
            assert service.settle_offline_gap(life, Mock(), engine_claimed=False)
            assert life.snapshot() == previous
        assert db._conn.execute("SELECT count(*) FROM sleep_sessions").fetchone()[0] == 0
        assert previous.get("last_wake_reason") != "offline_reconnect"
    finally:
        db.close()


async def case_morning_failure_is_recoverable(root):
    from planning.schedule_db import ScheduleDB
    from planning.life_state import LifeStateService
    from planning.offline_sleep import plan_offline_settlement
    from suite_offline_settlement import OfflineSettlementService

    db = ScheduleDB(root / "state.db")
    bus = Mock()
    planner = Mock()
    planner.apply_offline_morning_once.side_effect = [False, True]
    wake = Mock(return_value=True)
    kwargs = dict(
        db=db,
        data_dir=root,
        config_provider=lambda: NS(schedule_advanced=NS(timezone="UTC")),
        wake_recorder=wake,
        event_bus_provider=lambda: bus,
    )
    try:
        life = LifeStateService(root / "life.json", timezone_str="UTC")
        svc = OfflineSettlementService(**kwargs)
        at = datetime.now(timezone.utc).replace(microsecond=0)
        plan = {**plan_offline_settlement(5 * 3600, 0), "wake_at": at.isoformat()}
        assert not svc.apply_offline_settlement(life, planner, plan)
        assert not bus.publish_soon.called
        recovered = OfflineSettlementService(**kwargs)
        assert recovered.apply_offline_settlement(life, planner, plan)
        assert recovered.apply_offline_settlement(life, planner, plan)
        assert db._conn.execute("SELECT count(*) FROM sleep_sessions").fetchone()[0] == 1
        assert (
            db._conn.execute("SELECT count(*) FROM activity_episodes WHERE source='offline'").fetchone()[0]
            == 1
        )
        assert bus.publish_soon.call_count == 1
        assert (
            planner.apply_offline_morning_once.call_args_list[0]
            == planner.apply_offline_morning_once.call_args_list[1]
        )
    finally:
        db.close()


async def case_life_receipt_then_journal_retry(root):
    from planning.schedule_db import ScheduleDB
    from planning.life_state import LifeStateService
    from planning.offline_sleep import plan_offline_settlement
    from suite_offline_settlement import OfflineSettlementService
    import suite_offline_settlement as offline

    db = ScheduleDB(root / "state.db")
    bus = Mock()
    kwargs = dict(
        db=db,
        data_dir=root,
        config_provider=lambda: NS(schedule_advanced=NS(timezone="UTC")),
        wake_recorder=Mock(return_value=True),
        event_bus_provider=lambda: bus,
    )
    try:
        life = LifeStateService(root / "life.json", timezone_str="UTC")
        plan = {**plan_offline_settlement(2 * 3600, 1), "wake_at": datetime.now(timezone.utc).isoformat()}
        service = OfflineSettlementService(**kwargs)
        original = offline.save_json_atomic
        calls = 0

        def fail_second(path, value):
            nonlocal calls
            calls += 1
            return False if calls == 2 else original(path, value)

        with patch.object(offline, "save_json_atomic", side_effect=fail_second):
            assert service.apply_offline_settlement(life, Mock(), plan) is False
        first = life.snapshot()
        assert db._conn.execute("SELECT count(*) FROM sleep_sessions").fetchone()[0] == 1
        assert not bus.publish_soon.called
        recovered = OfflineSettlementService(**kwargs)
        reloaded = LifeStateService(root / "life.json", timezone_str="UTC")
        assert recovered.apply_offline_settlement(reloaded, Mock(), plan)
        for name in ("energy", "hunger", "focus", "stress", "social_battery", "sleep_debt"):
            assert reloaded.snapshot()[name] == first[name], name
        assert (
            db._conn.execute("SELECT count(*) FROM activity_episodes WHERE source='offline'").fetchone()[0]
            == 1
        )
        assert bus.publish_soon.call_count == 1
    finally:
        db.close()


async def case_failed_screen_receipt(root):
    from linkage.screen.cateye_client import CateyeClient
    from core.llm_utils import LLMService

    ctx = NS(
        paths=NS(data_dir=root),
        logger=Mock(),
        llm=NS(generate=AsyncMock(return_value={"success": False, "error": "authentication failed"})),
    )
    p = NS(
        ctx=ctx,
        config=NS(
            screen=NS(enabled=True, tool_enabled=True, scope="main"), llm=NS(vlm_task="vlm"), prompts=NS()
        ),
    )
    client = CateyeClient(p, llm=LLMService(ctx))
    client.is_connected = AsyncMock(return_value=True)
    client._capture_screens = AsyncMock(
        return_value=[{"index": 0, "primary": True, "image_base64": "Zml4dHVyZQ=="}]
    )
    result = await client.peek_screen(datetime.now().astimezone(), manual=True)
    assert result.status == "failed" and not result.ok and not result.description


def init_driver(root):
    from tools.init.registry import InitRegistry
    from tools.init.manifest import INIT_FIELDS
    from tools.init.auto_init import AutoInitDriver
    from tools.init.service import InitResult, StageResult

    reg = InitRegistry(root)
    for field in INIT_FIELDS:
        reg.mark_true(field, "3.17.33")
    reg.set_plugin_version("3.17.33")
    cfg = NS(plugin=NS(enabled=True), video_watch=NS(enabled=False), schedule=NS(enabled=True))
    p = NS(config=cfg, ctx=NS(logger=Mock()), _data_dir=root)

    async def stage(name, **kwargs):
        return InitResult("field", True, "skipped", (StageResult(name, "skipped", reason="disabled"),))

    svc = NS(
        preflight=lambda: StageResult("preflight", "success"),
        config=lambda: cfg,
        versions=lambda: {"plugin_version": "3.18.7"},
        run_stage=stage,
    )
    return AutoInitDriver(p, service=svc, registry=reg), reg


async def case_partial_init_does_not_skip_upgrade(root):
    from tools.init.auto_init import compute_need

    driver, reg = init_driver(root)
    assert "activity_preferences" in compute_need(reg.load(), "3.18.7")
    await driver.run(manual=True, fields=["video_interest"], force=True)
    assert "activity_preferences" in compute_need(reg.load(), "3.18.7")


async def case_registry_failure_report(root):
    driver, reg = init_driver(root)
    reg.invalidate("video_interest")
    with patch.object(reg, "mark_true", return_value=False):
        result = await driver.run(manual=True, fields=["video_interest"])
    assert result.outcomes[0].status == "failed"
    assert reg.field_state("video_interest")["initialized"] is False


async def case_upgrade_invalidation_failure_preserves_version(root):
    from tools.init.auto_init import compute_need

    driver, reg = init_driver(root)
    with patch.object(reg, "invalidate", return_value=False):
        result = await driver.run(manual=True)
    assert result.outcomes[0].status == "failed"
    assert "activity_preferences" in compute_need(reg.load(), "3.18.7")
    assert reg.load()["plugin_version"] == "3.17.33"


async def case_offline_sleep_labels(root):
    js = (SOURCE / "webui/source/panel.js").read_text(encoding="utf-8")
    functions = js[js.index("function spanEnd("):js.index("function activityNameNode(")]
    assertions = """
const assert = require('node:assert/strict');
const display = (start, end, overrides={}) => displayActivityName({
    name:'睡觉', goal_type:'sleep', source:'offline',
    actual_start_minute:start, actual_end_minute:end, ...overrides
});
assert.equal(display(981, 988), '小憩一会');
assert.equal(display(981, 1011), '休息了一阵');
assert.equal(display(1380, 60), '补了一觉');
assert.equal(display(1380, 1740), '睡了个长觉');
assert.equal(display(981, null), '离线休息');
assert.equal(display(981, 988, { name:'补眠', source:'', wake_reason:'offline_reconnect' }), '小憩一会');
assert.equal(display(1380, 1740, { source:'rolling' }), '晚安，世界');
assert.equal(display(750, 780, { name:'午睡', source:'rolling' }), '午休');
assert.equal(display(750, 780, { name:'散步', goal_type:'exercise' }), '散步');
"""
    subprocess.run(["node", "-e", "const num = v => Number.isFinite(v) ? v : null;\n"
                    + functions + assertions], check=True)


async def case_separate_energy_and_social_tones(root):
    from planning.inject_service import InjectService

    snapshot = {"energy": 0.6896, "social_battery": 0.0592}
    service = NS(_life_state_snapshot=lambda: snapshot, _emotion_valence=lambda: 0.31,
                 _boundary_extreme_tone=lambda _: "")
    rule = InjectService._subconscious_tone_rule
    assert "想少聊" in rule(service)
    snapshot["energy"] = 0.30
    assert "精力偏低" in rule(service) and "很疲惫" not in rule(service)
    snapshot["energy"] = 0.24
    assert "很疲惫" in rule(service)
    service._boundary_extreme_tone = lambda _: "目标私聊边界"
    assert rule(service, "target") == "目标私聊边界"


async def case_dashboard_image_references(root):
    import base64
    from io import BytesIO

    from PIL import Image

    from planning.dashboard import _qzone_recent_posts, _video_recent_row
    from planning.dashboard_components import LifeDashboardComponentsMixin
    from planning import dashboard_media as media

    cover = "https://i1.hdslb.com/test.png"
    post = "https://pic.qpic.cn/test.png"
    video = _video_recent_row({"cover": cover})
    posts = _qzone_recent_posts(
        NS(_publish_ledger=NS(recent_sent=lambda _: [{"images": [post]}])),
        NS(qzone_video=None),
    )
    assert video["cover"] == cover and video["cover_id"]
    assert posts[0]["images"] == [post] and posts[0]["image_ids"][0]
    assert not _video_recent_row({"cover": "https://example.invalid/file.png"})["cover_id"]

    source = BytesIO()
    Image.new("RGB", (1280, 640), "red").save(source, "PNG")
    dashboard = LifeDashboardComponentsMixin()
    with patch.object(media, "_read_image", return_value=source.getvalue()) as read:
        unknown = await dashboard.life_dashboard_thumbnail("0" * 64)
        assert unknown == {"src": ""} and not read.called
        for image_id in (video["cover_id"], posts[0]["image_ids"][0], video["cover_id"]):
            result = await dashboard.life_dashboard_thumbnail(image_id)
            mime, data = result["src"].split(",", 1)
            raw = base64.b64decode(data, validate=True)
            with Image.open(BytesIO(raw)) as thumbnail:
                assert mime == "data:image/jpeg;base64"
                assert thumbnail.format == "JPEG" and thumbnail.size == (720, 360)
                assert len(raw) <= media.MAX_THUMBNAIL_BYTES
        assert [call.args[0] for call in read.call_args_list] == [cover, post]


async def case_day_session_boundary(root):
    from planning.schedule_db import ScheduleDB
    from planning.dashboard import build_life_dashboard, _module_status
    from suite_config import CateyeSuiteConfig

    db = ScheduleDB(root / "day.db")
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        cfg = CateyeSuiteConfig()
        cfg.patrol.enabled = False
        day = now.date().isoformat()
        p = NS(config=cfg, _db=db, _life_model=NS(), _data_dir=root)
        for i in range(16):
            assert db.upsert_sleep_session(
                {
                    "session_id": str(i),
                    "schedule_date": day,
                    "actual_start_minute": i * 30,
                    "actual_end_minute": i * 30 + 20,
                    "continuous_minutes": 20,
                    "wake_reason": "manual",
                }
            )
        snap = build_life_dashboard(p, hours=24, day=day, now=now)
        assert len(snap["day"]["sleep"]["sessions"]) == 16
        js = (SOURCE / "webui/source/panel.js").read_text(encoding="utf-8")
        fragments = [
            s
            for s in js.splitlines()
            if s.startswith(("function date(", "function validDay(", "function validateSnapshot("))
        ]
        code = "\n".join(fragments) + "\nvalidateSnapshot(JSON.parse(require('fs').readFileSync(0,'utf8')));"
        subprocess.run(["node", "-e", code], input=json.dumps(snap), text=True, check=True)
        assert _module_status(p, "companion") == "停用"
    finally:
        db.close()


async def case_screen_registration_refresh(root):
    from plugin import CateyeSuitePlugin
    from companion.screen_tools import SCREEN_TOOL_NAME
    from core.targeting import refresh_host_target
    from suite_config import CateyeSuiteConfig

    ctx = NS(
        paths=NS(data_dir=root),
        logger=Mock(),
        config=NS(get=AsyncMock(return_value=["qq:10203"])),
        component=NS(reload_plugin=AsyncMock(return_value={"success": True})),
    )

    def fixture():
        p = object.__new__(CateyeSuitePlugin)
        p.config = CateyeSuiteConfig()
        p.config.screen.tool_enabled = True
        p.ctx = ctx
        p._companion = Mock()
        p._qzone = Mock()
        p._schedule_target_frequency_calibration = Mock()
        return p

    def decl(p):
        rows = p.get_components()
        return next((r for r in rows if r.get("name") == SCREEN_TOOL_NAME), None)

    p = fixture()
    assert decl(p) is None
    await refresh_host_target(p)
    p._stream_id = "private-verified"
    p._apply_target("10203", p._stream_id)
    for _ in range(5):
        await asyncio.sleep(0)
    ctx.component.reload_plugin.assert_awaited_once()
    fresh = fixture()
    registered = decl(fresh)
    assert (
        registered
        and registered["chat_scope"] == "private"
        and registered["allowed_session"] == ["private-verified"]
    )
    await refresh_host_target(fresh)
    fresh._stream_id = "private-verified"
    fresh._apply_target("10203", fresh._stream_id)
    for _ in range(5):
        await asyncio.sleep(0)
    ctx.component.reload_plugin.assert_awaited_once()


async def case_component_metadata_before_context(root):
    from plugin import CateyeSuitePlugin, create_plugin
    from companion.screen_tools import SCREEN_TOOL_NAME
    from core.targeting import refresh_host_target
    from suite_config import CateyeSuiteConfig

    def context(instance):
        value = getattr(instance, "_fixture_context", None)
        if value is None:
            raise RuntimeError("Plugin context has not been bound")
        return value

    def names(instance):
        rows = instance.get_components()
        assert not hasattr(instance, "_target_binding_coordinator")
        assert any(row.get("name") == "/lbm" for row in rows)
        return {row.get("name"): row for row in rows}

    ctx = NS(
        plugin_id="fixture.precontext",
        paths=NS(data_dir=root),
        config=NS(get=AsyncMock(return_value=["qq:10203"])),
        component=NS(reload_plugin=AsyncMock(return_value={"success": True})),
        logger=Mock(),
    )
    with patch.object(CateyeSuitePlugin, "ctx", property(context), create=True):
        plugin = create_plugin()
        assert SCREEN_TOOL_NAME not in names(plugin)
        for version, supports_preprocess, supports_split in (
            ("1.0.0", False, False),
            ("1.2.0", True, False),
            ("v1.3.19", True, False),
            ("v1.4.0", True, True),
            ("1.4.6", True, True),
        ):
            with patch.dict(os.environ, {"MAIBOT_HOST_VERSION": version}):
                registered = names(plugin)
                assert ("mai_prepare_proactive_delivery" in registered) is supports_preprocess
                assert ("mai_prepare_proactive_segments" in registered) is supports_split
                assert "mai_acknowledge_proactive_send" in registered
        plugin.config = CateyeSuiteConfig()
        plugin.config.screen.tool_enabled = True
        assert SCREEN_TOOL_NAME not in names(plugin)
        plugin._fixture_context = ctx
        assert SCREEN_TOOL_NAME not in names(plugin)
        await refresh_host_target(plugin)
        plugin._companion = Mock()
        plugin._qzone = Mock()
        plugin._schedule_target_frequency_calibration = Mock()
        plugin._resolve_target_stream = AsyncMock(return_value="verified-private")
        await plugin._refresh_target_binding()
        for _ in range(5):
            await asyncio.sleep(0)
        ctx.component.reload_plugin.assert_awaited_once_with(plugin_name="fixture.precontext")

        # Host registration with an already-bound context reuses only the verified single stream.
        loaded = create_plugin()
        loaded.config = CateyeSuiteConfig()
        loaded.config.screen.tool_enabled = True
        loaded._fixture_context = ctx
        declaration = names(loaded)[SCREEN_TOOL_NAME]
        assert declaration["chat_scope"] == "private"
        assert declaration["allowed_session"] == ["verified-private"]
        await refresh_host_target(loaded)
        loaded._companion = Mock()
        loaded._qzone = Mock()
        loaded._schedule_target_frequency_calibration = Mock()
        loaded._resolve_target_stream = AsyncMock(return_value="verified-private")
        await loaded._refresh_target_binding()
        for _ in range(5):
            await asyncio.sleep(0)
        ctx.component.reload_plugin.assert_awaited_once()


def run_isolated(name):
    sys.path.insert(0, str(HERE))
    from astra_arch_support import install_sdk, stage_plugin

    with tempfile.TemporaryDirectory(prefix="maibot-arch-") as temp:
        root = Path(temp)
        stage_plugin(SOURCE, root / "plugin")
        install_sdk()

        async def offline():
            with patch.object(
                socket.socket, "connect", side_effect=AssertionError("External network disabled")
            ):
                await globals()[name](root)

        asyncio.run(offline())


async def case_reload_waiter_cancellation(root):
    from plugin import CateyeSuitePlugin

    commands = asyncio.Queue()
    stopped = asyncio.Event()
    p = object.__new__(CateyeSuitePlugin)
    p._stream_id = "verified"
    p.config = NS(plugin=NS(enabled=True), patrol=NS(enabled=True), screen=NS(tool_enabled=True))
    p.config.plugin.id = "different.user.supplied.name"

    async def sdk_reload(*, plugin_name):
        # The SDK sends a cap.call frame, then waits on its own runner-side future.
        await commands.put(plugin_name)
        await asyncio.Future()

    p.ctx = NS(component=NS(reload_plugin=sdk_reload), logger=Mock())
    p.ctx.plugin_id = "fixture.copy"
    p._target_lifecycle().declare("")
    p._target_lifecycle().bound("verified", saved=True)
    p._shutdown_chain = AsyncMock()
    p._stop_life_services = Mock()

    async def host():
        assert await commands.get() == "fixture.copy"
        await p.on_unload()  # Local awaiter is cancelled; no cancellation frame goes to the host.
        stopped.set()

    host_task = asyncio.create_task(host())
    p._queue_screen_registration_reload()
    p._queue_screen_registration_reload()
    await asyncio.wait_for(host_task, 1)
    assert stopped.is_set() and commands.empty() and p._target_lifecycle()._reload_task is None


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1].startswith("case_"):
        run_isolated(sys.argv[1])
    else:
        unittest.main()
