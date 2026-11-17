"""Stable commitment workflow contracts; all state lives in per-process temporary SQLite."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get("CATEYE_TEST_DIR") or Path(__file__).resolve().parents[1]).resolve()


def run_contracts():
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime
    import socket
    import tempfile
    from types import ModuleType, SimpleNamespace
    from zoneinfo import ZoneInfo
    import pydantic

    sys.path.insert(0, str(ROOT))
    sdk = ModuleType("maibot_sdk")
    sdk.PluginConfigBase = pydantic.BaseModel
    sdk.Field = pydantic.Field
    sys.modules["maibot_sdk"] = sdk
    companion = ModuleType("companion")
    companion.__path__ = [str(ROOT / "companion")]
    sys.modules["companion"] = companion

    def reject_network(*args, **kwargs):
        raise AssertionError("offline workflow tests cannot connect")

    socket.socket.connect = reject_network
    socket.create_connection = reject_network

    from planning.goal_manager import GoalManager
    from planning.schedule_db import ScheduleDB
    from planning.schedule_generator import ScheduleGenerator

    day = "2026-11-17"
    now = datetime(2026, 11, 17, 9, tzinfo=ZoneInfo("Asia/Shanghai"))
    clock = SimpleNamespace(get_now=lambda: now, to_local=lambda value: value or now, _tz=now.tzinfo)
    test = unittest.TestCase()
    with tempfile.TemporaryDirectory(prefix="commitment-workflow-") as temporary:
        root = Path(temporary)
        db = ScheduleDB(root / "test.sqlite")
        manager = GoalManager(data_dir=root, db=db)
        manager.bind_clock_provider(lambda: clock)
        try:
            generator = ScheduleGenerator(db, None, goal_manager=manager)
            published = []

            def publication(date):
                published.append((date, [row["name"] for row in db.get_day_schedule(date)]))
                generator.confirm_commitment_publication(date)

            first = manager.accept_timed_commitment(
                day, "meeting", "10:00", notes="synthetic", duration=30, activity_type="work",
                publish_today=True, publication=publication)
            test.assertEqual(first.goal_type, "pending_commitment")
            item = first.parameters["commitment_plan"]["item_id"]
            test.assertEqual(db.get_schedule_item(item)["parameters"]["commitment_id"], first.goal_id)
            test.assertEqual(published[-1], (day, ["meeting"]))

            def accept_again(_):
                return manager.accept_timed_commitment(
                    day, "meeting", "10:00", notes="synthetic", duration=30,
                    activity_type="work", publish_today=True).goal_id

            with ThreadPoolExecutor(max_workers=4) as pool:
                ids = list(pool.map(accept_again, range(8)))
            test.assertEqual(set(ids), {first.goal_id})
            test.assertEqual(len(db.get_day_schedule(day)), 1)

            future = manager.accept_timed_commitment(
                "2026-11-18", "future", "10:00", publication=publication)
            test.assertNotIn("commitment_plan", future.parameters)
            test.assertEqual(published[-1], ("2026-11-18", []))

            ordinary = generator._spine_row(
                "2026-11-18", "reading", 480, 540, "work", "medium", 20, 60)
            generator.apply_schedule_items([ordinary], "2026-11-18")
            again = manager.accept_timed_commitment(
                "2026-11-18", "future", "10:00", publication=publication)
            test.assertEqual(again.goal_id, future.goal_id)
            test.assertEqual([row["name"] for row in db.get_day_schedule("2026-11-18")].count("future"), 1)

            def fail_publication(date):
                raise OSError("synthetic publication failure")

            with test.assertRaises(OSError):
                manager.accept_timed_commitment(day, "durable", "12:00", publish_today=True,
                                                publication=fail_publication)
            accepted = next(row for row in db.get_all_goals() if row["name"] == "durable")
            retry = manager.accept_timed_commitment(day, "durable", "12:00", publish_today=True,
                                                    publication=publication)
            test.assertEqual(retry.goal_id, accepted["goal_id"])
            test.assertEqual([row["name"] for row in db.get_day_schedule(day)].count("durable"), 1)

            test.assertTrue(manager.delete_goal(item))
            test.assertEqual(db.get_goal(first.goal_id)["status"], "cancelled")
            test.assertIsNone(db.get_schedule_item(item))
            test.assertFalse(db.pending_commitment_events())
            calls = len(published)
            test.assertIsNone(manager.accept_timed_commitment(
                day, "meeting", "10:00", existing_goal_id=first.goal_id, publication=publication))
            test.assertEqual(len(published), calls)
            test.assertTrue(manager.delete_goal(retry.goal_id))

            generator.apply_schedule_items([generator._spine_row(
                day, "ordinary", 480, 540, "work", "medium", 20, 60)], day)
            legacy = manager.add_pending_commitment(day, "legacy-repair", time="15:00", hardness="hard")
            manager.reconcile_commitments(now)
            test.assertTrue(db.get_goal(legacy.goal_id)["parameters"].get("commitment_plan"))
            test.assertEqual([row["name"] for row in db.get_day_schedule(day)].count("legacy-repair"), 1)
            manager.reconcile_commitments(now)
            test.assertEqual([row["name"] for row in db.get_day_schedule(day)].count("legacy-repair"), 1)

            expected = db.get_day_schedule(day)
        finally:
            db.close()
        reopened = ScheduleDB(root / "test.sqlite")
        try:
            restored = GoalManager(data_dir=root, db=reopened)
            restored.bind_clock_provider(lambda: clock)
            restored.reconcile_commitments(now)
            fields = ("item_id", "name", "start_minute", "end_minute", "parameters")
            project = lambda rows: [{key: row[key] for key in fields} for row in rows]
            test.assertEqual(project(reopened.get_day_schedule(day)), project(expected))
            test.assertFalse(reopened.pending_commitment_events())
        finally:
            reopened.close()


class CommitmentWorkflowRegression(unittest.TestCase):
    def test_workflow_contracts(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-B", __file__, "--worker"],
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, encoding="utf-8", timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_contracts()
    else:
        unittest.main()
