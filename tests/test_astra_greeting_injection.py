"""Regression coverage for the live greeting gates and dynamic schedule facts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from astra_arch_support import install_sdk, stage_plugin


SOURCE = Path(os.environ.get("CATEYE_TEST_DIR", Path(__file__).resolve().parents[1]))
NOW = datetime(2026, 11, 19, 9, 47, 17, tzinfo=timezone(timedelta(hours=8)))
HOST_TAIL = ("请分析后决定下一步：用工具收集或处理信息，用 reply 回复，用无时间参数的 wait "
             "静默结束本轮，或给 wait 指定时间稍后再判断。")


def message(sender, stamp, **extra):
    return {"account_id": "700001", "message_info": {"user_info": {"user_id": sender}},
            "timestamp": str(stamp), "processed_plain_text": "测试消息", **extra}


def item(*text, **meta):
    return {"item_type": "UserMessageItem", "meta": meta,
            "parts": [{"type": "text", "text": value} for value in text]}


class GreetingAndInjectionCases(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.staging = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.staging.cleanup)
        stage_plugin(SOURCE, Path(cls.staging.name))
        install_sdk()

    def greeting_service(self, directory=None):
        from planning.proactive_service import ProactiveService

        instance = ProactiveService.__new__(ProactiveService)
        instance._plugin = NS(_data_dir=directory, ctx=NS(message=NS(
            get_by_time_in_chat=AsyncMock(return_value={"success": True, "messages": []}))))
        instance._tz_manager = NS(get_now=lambda: NOW)
        instance._cycle_ctx = Mock(return_value=NS(current_cycle_start=(NOW-timedelta(minutes=23)).timestamp()))
        instance._cycle_pivots = lambda cycle: (cycle.current_cycle_start-86400, cycle.current_cycle_start,
                                                cycle.current_cycle_start+86400)
        instance._morning_cap_ts = Mock(return_value=(NOW+timedelta(minutes=13)).timestamp())
        instance._greeting_kind_enabled = Mock(return_value=True)
        instance._proactive_cfg = Mock(return_value=NS(greeting_morning_silence_hours=2.0))
        return instance

    async def test_recent_group_own_send_uses_current_sdk_and_refuses_before_dispatch(self):
        service = self.greeting_service()
        recent_own = (NOW - timedelta(minutes=2, seconds=14)).timestamp()
        getter = service._plugin.ctx.message.get_by_time_in_chat
        getter.return_value = {"success": True, "messages": [message("700001", recent_own)]}
        cycle = service._cycle_ctx(NOW)
        cfg = service._proactive_cfg()
        self.assertFalse(await service.group_morning_proactive_allowed("group"))
        self.assertGreater(float(getter.await_args.args[2]), cycle.current_cycle_start)

        service._pending_greeting_observe_until = Mock(return_value=(NOW + timedelta(minutes=1)).timestamp())
        service._greeting_fire_gate = AsyncMock(return_value=(True, False))
        service._greeting_deduped_by_persisted_marker = Mock(return_value=False)
        service._stream_greeting_marked = Mock(return_value=False)
        service._decision_log = Mock()
        service._fire_greeting = AsyncMock()
        entry = {"stream_id": "group", "category": "group", "kind": "morning",
                 "status": "pending", "fire_at": NOW.timestamp()-1, "deadline": NOW.timestamp()+60}
        await service._advance_greeting(entry, NOW.timestamp(), "2026-11-19", cfg, cycle=cycle)
        self.assertEqual(entry["reason"], "recent_bot_speech")
        service._fire_greeting.assert_not_awaited()

    async def test_dense_group_uses_scoped_own_history_and_unknown_fails_closed(self):
        service = self.greeting_service()
        getter = service._plugin.ctx.message.get_by_time_in_chat
        getter.return_value = {"success": True, "messages": [
            message("710001", NOW.timestamp()-i-1) for i in range(64)
        ]}
        service._plugin.ctx.database = NS(get=AsyncMock(return_value=[
            {"timestamp": (NOW-timedelta(minutes=12)).isoformat()}]))
        self.assertFalse(await service.group_morning_proactive_allowed("group"))
        call = service._plugin.ctx.database.get.await_args
        self.assertEqual(call.args, ("Messages",))
        self.assertEqual(call.kwargs["filters"], {"session_id": "group", "user_id": "700001"})

        getter.return_value = {"success": False, "error": "unavailable"}
        self.assertFalse(await service.group_morning_proactive_allowed("group"))
        service._proactive_cfg.return_value.greeting_morning_silence_hours = 0
        getter.reset_mock()
        self.assertTrue(await service.group_morning_proactive_allowed("group"))
        getter.assert_not_awaited()

    async def test_target_gate_requires_own_post_greeting_message_after_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            service = self.greeting_service(directory)
            service._target_qq = Mock(return_value="720001")
            service._target_stream_id_readonly = Mock(return_value="target")
            getter = service._plugin.ctx.message.get_by_time_in_chat
            stamp = NOW.timestamp() - 20
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))
            self.assertFalse(service.record_target_greeting_sent("other", stamp))
            self.assertTrue(service.record_target_greeting_sent("target", stamp))
            service._target_greeting_receipts = None  # Reopen the journal with a cold service cache.
            getter.return_value = {"messages": [
                message("720001", stamp-1),
                message("700001", stamp+2),
                message("730001", stamp+2),
                message("720001", stamp+4, is_notify=True),
            ]}
            self.assertFalse(await service.target_greeting_proactive_allowed("target"))
            getter.return_value["messages"].append(message("720001", stamp+3, is_picture=True))
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))
            service._target_qq.return_value = "730001"
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))

    async def test_receipt_retries_same_timestamp_after_atomic_write_failure(self):
        from planning import greeting_guard
        with tempfile.TemporaryDirectory() as directory:
            store = greeting_guard.TargetGreetingReceipts(directory)
            with patch.object(greeting_guard, "save_json_atomic", return_value=False):
                self.assertFalse(store.record("720001", NOW.timestamp()))
            self.assertEqual(store.last_sent("720001"), (True, NOW.timestamp()))
            self.assertTrue(store.record("720001", NOW.timestamp()))
            reopened = greeting_guard.TargetGreetingReceipts(directory)
            self.assertEqual(reopened.last_sent("720001"), (True, NOW.timestamp()))

    async def test_target_periods_share_the_same_optional_time_prompt(self):
        service = self.greeting_service()
        service._call_proactive_trigger = AsyncMock(return_value=True)
        for kind in ("morning", "night"):
            self.assertTrue(await service._trigger_greeting("target", kind, "2026-11-19",
                                                           category="target", cycle_key=77))
        kwargs = [call.kwargs for call in service._call_proactive_trigger.await_args_list]
        self.assertEqual(kwargs[0]["intent_prompt"], kwargs[1]["intent_prompt"])
        self.assertIn("09:47", kwargs[0]["intent_prompt"])
        self.assertIn("可以直接结束", kwargs[0]["intent_prompt"])
        self.assertNotIn("早安", kwargs[0]["intent_prompt"])
        self.assertEqual([entry["key"] for entry in kwargs], ["77:morning", "77:night"])

    async def test_planner_control_tail_is_not_an_asked_schedule_question(self):
        from planning.inject_service import InjectService
        standalone = item(HOST_TAIL, is_self_message=False, timestamp=NOW.isoformat())
        incoming = item('<message msg_id="m1" user="人" time="09:45:00">\n来聊聊天')
        self.assertEqual(InjectService._extract_last_user_text([incoming, standalone]), "来聊聊天")
        self.assertFalse(InjectService._has_real_interaction_today([standalone], today="2026-11-19"))
        # The same words actually sent and wrapped by a human stay available.
        quoted = item('<message msg_id="m2" user="人" time="09:45:30">\n', HOST_TAIL,
                      is_self_message=False)
        self.assertEqual(InjectService._extract_last_user_text([quoted]), HOST_TAIL)
        latest = item('<plugin_proactive_task id="task">消息通知</plugin_proactive_task>')
        self.assertEqual(InjectService._extract_last_user_text([incoming, latest, standalone]), "")

    async def test_replyer_prefers_real_current_match_over_cached_schedule_intent(self):
        from planning.inject_service import InjectService
        from planning.inject.intent_classifier import IntentClassifier, UserIntent
        import time

        service = InjectService.__new__(InjectService)
        service._intent_classifier = IntentClassifier()
        service._context_cache = NS(get_recent_turns=Mock(return_value=[
            NS(intent="query_future", timestamp=time.time())]))
        self.assertIs(service._resolve_replyer_intent("group", {"match_text": "来聊聊天"})[0],
                      UserIntent.CASUAL_CHAT)
        self.assertIs(service._resolve_replyer_intent("group", {"match_text": "接下来打算做什么？"})[0],
                      UserIntent.QUERY_FUTURE)
        self.assertIs(service._resolve_replyer_intent("group", {"match_text": ""})[0],
                      UserIntent.CASUAL_CHAT)
        self.assertIs(service._resolve_replyer_intent("group", {})[0], UserIntent.QUERY_FUTURE)

    async def test_unstarted_sleep_and_awake_conflict_are_not_current_sleep(self):
        from planning.inject_service import InjectService
        service = InjectService.__new__(InjectService)
        engine = NS(is_asleep=Mock(return_value=False), dynamic_status=lambda: {"band": "winding_down"})
        model = NS(enabled=True, current=Mock(return_value={"goal_type": "sleep", "status": "planned", "name": "补觉"}),
                   next=lambda *a, **kw: [], state_line=lambda *a, **kw: "", today=lambda now: {})
        service._plugin = NS(_sleep_engine=engine, _planning=NS(life_schedule=model))
        service._tz_manager = NS(get_now=lambda: NOW)
        self.assertIsNone(service._life_runtime_view()["current"])
        model.current.return_value = {"goal_type": "sleep", "status": "paused", "name": "补觉"}
        self.assertIsNone(service._life_runtime_view()["current"])
        model.current.return_value["status"] = "active"
        self.assertIsNone(service._life_runtime_view()["current"])
        engine.is_asleep.return_value = True
        self.assertEqual(service._life_runtime_view()["current"]["name"], "补觉")
        self.assertEqual(service._life_runtime_view()["sleep"]["state"], "asleep")


if __name__ == "__main__":
    unittest.main()
