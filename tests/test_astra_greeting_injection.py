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


def user(item_id: str, text: str) -> dict:
    return {
        "item_type": "UserMessageItem",
        "meta": {
            "item_id": item_id, "logical_turn_id": None, "timestamp": "2026-11-21T04:00:01+08:00"
        },
        "parts": [{"type": "text", "text": text}],
    }


def system(text: str) -> dict:
    return {**user("system", text), "item_type": "SystemMessageItem"}


def first_text(item: dict) -> str:
    return item["parts"][0]["text"]


HOST_TIME = "时间：2026-11-21 04:01:00"
REPLYER_TIME = "当前时间：2026-11-21 04:01:00"
HISTORY = '<message user="A" msg_id="real-a">历史原文'


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

    async def test_split_by_outbound_hook_closes_the_next_greeting_until_target_replies(self):
        from companion.components import CompanionComponentsMixin
        from core.inject import CATEYE_PLUGIN_ID
        from integrations.proactive_usage import ProactiveUsage

        with tempfile.TemporaryDirectory() as directory:
            service = self.greeting_service(directory)
            service._target_qq = Mock(return_value="720001")
            service._target_stream_id_readonly = Mock(return_value="target")
            plugin = CompanionComponentsMixin()
            plugin._planning = NS(proactive=service)
            plugin._proactive_usage_correlation = ProactiveUsage(plugin, clock=NOW.timestamp)
            turn = {"turn_id": "scheduled-greeting", "stream_id": "target", "category": "greeting",
                    "kind": "morning_greeting", "tracked": True, "chat_type": "private_target"}
            plugin._mai_proactive_turns = lambda: NS(identified=lambda *args, **kwargs: turn)
            prepared = {"session_id": "target"}
            task = item(f'<plugin_proactive_task id="proactive:{CATEYE_PLUGIN_ID}:1" '
                        f'plugin_id="{CATEYE_PLUGIN_ID}">\n'
                        '附加信息：{"lbm_turn_id":"scheduled-greeting","greeting_scope":"target"}\n'
                        '</plugin_proactive_task>')
            tracking = plugin._mai_proactive_usage()
            await tracking.planner_before({**prepared, "items": [task]})
            selected = tracking.planner_after({**prepared, "output_items": [
                {"item_type": "FunctionCallItem", "tool_call": {
                    "func_name": "reply", "args": {"chat_anlyze": "到点问候"}}},
            ]})
            args = selected["output_items"][0]["tool_call"]["args"]
            raw_text = "博士早呀，昨晚睡得好不好"
            await plugin.mai_hook_before_proactive_delivery(**prepared, response=raw_text, reply_tool_args=args)

            stamp = NOW.timestamp() - 2
            built = {**prepared, "timestamp": str(stamp), "message_id": "before-qq-receipt",
                     "raw_message": [{"type": "text", "data": raw_text}]}
            await plugin.mai_hook_identify_greeting_send(message=built)
            # A post-processing plugin takes over splitting, so the host never calls after_split.
            sent = {**built, "message_id": "qq-receipt", "raw_message": [{"type": "text", "data": "博士早呀"}]}
            await plugin.mai_hook_acknowledge_proactive_send(sent=False, message=sent)
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))
            await plugin.mai_hook_acknowledge_proactive_send(
                sent=True, message={**sent, "timestamp": str(stamp - 1)})
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))
            await plugin.mai_hook_acknowledge_proactive_send(sent=True, message=sent)

            service._target_greeting_receipts = None  # Verify the stored result survives reload.
            self.assertEqual(service._target_greeting_receipt_store().last_sent("720001"), (True, stamp))
            getter = service._plugin.ctx.message.get_by_time_in_chat
            getter.return_value["messages"] = [message("720001", stamp - 1), message("700001", stamp + 1)]
            self.assertFalse(await service.target_greeting_proactive_allowed("target"))
            getter.return_value["messages"].append(message("720001", stamp + 1))
            self.assertTrue(await service.target_greeting_proactive_allowed("target"))

    async def test_target_periods_share_the_same_optional_time_prompt(self):
        service = self.greeting_service()
        service._call_proactive_trigger = AsyncMock(return_value=True)
        for kind in ("morning", "night"):
            self.assertTrue(await service._trigger_greeting("target", kind, "2026-11-19",
                                                           category="target", cycle_key=77))
        kwargs = [call.kwargs for call in service._call_proactive_trigger.await_args_list]
        self.assertEqual(kwargs[0]["intent_prompt"], kwargs[1]["intent_prompt"])
        self.assertIn("09:47", kwargs[0]["intent_prompt"])
        self.assertIn("直接静默结束", kwargs[0]["intent_prompt"])
        self.assertNotIn("早安", kwargs[0]["intent_prompt"])
        self.assertEqual([entry["key"] for entry in kwargs], ["77:morning", "77:night"])

    def test_schedule_prompt_uses_coarse_time_and_does_not_invent_other_states(self):
        from planning.inject_service import InjectService

        service = InjectService.__new__(InjectService)
        service._plugin = NS()
        service._schedule_cfg = lambda: NS(enable_detailed_description=True)
        service._bot_name = lambda: "麦麦"
        service._replyer_energy_desc = lambda: "有点饿，注意力集中"
        service._subconscious_tone_rule = lambda session_id: ""
        service._boundary_reply_hints = lambda session_id: []
        activity = {"current": {"name": "听音乐"}}
        lines = service._life_block_lines(activity, {"remaining": 8}, slim=True)
        self.assertEqual(lines, ["当前活动实况：听音乐，快结束了"])

        background = service._build_replyer_extra_prompt(
            "听音乐", "听音乐", [], 8, show_hunger=False)
        self.assertIn("听音乐（快结束了）", background)
        self.assertIn("注意力集中", background)
        self.assertNotIn("听音乐（听音乐）", background)
        self.assertNotIn("有点饿", background)
        self.assertNotIn("有点乏", background)

        asked = service._build_replyer_extra_prompt("听音乐", "听音乐", [], 11, show_hunger=True)
        self.assertIn("有点饿", asked)
        self.assertNotIn("快结束了", asked)

    def test_stage4_uses_honest_video_result_and_clears_legacy_markers(self):
        from integrations.optional_action import STAGE4_PLANNER_POLICY
        from suite_prompts import append_stage4_miss_prompt, strip_stage4_suffix
        from tools.proactive_turn import rewrite_notice_for

        failed = append_stage4_miss_prompt(
            "点播任务失败。", hours=126, is_video=True, video_failed=True)
        self.assertIn("点播任务失败。", failed)
        self.assertIn("没看成", failed)
        self.assertNotIn("先顺着视频", failed)
        self.assertIn("本轮必须发出消息", STAGE4_PLANNER_POLICY)
        self.assertIn("没看成", rewrite_notice_for(
            "private_target", "video_completion", miss_stage=4, video_failed=True))
        self.assertIn("实际结果", rewrite_notice_for("private_target", "video_completion", miss_stage=4))
        self.assertEqual(strip_stage4_suffix(failed), "点播任务失败。")
        self.assertEqual(strip_stage4_suffix(
            "原文\n【深层心理覆盖·失联阶段4（已沉默约126小时）】旧尾缀\n【失联阶段4】新尾缀"), "原文")

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


    def test_planner_keeps_history_and_tools_within_stable_prefix(self) -> None:
        from core.inject import LBM_CONTEXT_PREFIX, inject_into_items_before_tail

        previous_time = user("previous", "时间：2026-11-20 23:59:58")
        call = {"item_type": "FunctionCallItem", "meta": {"item_id": "call"}}
        result = {"item_type": "FunctionCallOutputItem", "meta": {"item_id": "result"}}
        history = [
            system("人设"),
            previous_time,
            user("history", HISTORY),
            call,
            result,
            user("after-wait", "[回复后等待结束]\n此前已等待一段时间。"),
            user("restore", "这是启动时恢复的历史上下文提醒"),
        ]
        host_tail = [
            user("profile", "【人物画像-内部参考】\n人物资料"),
            user("memory", "【启发式记忆-内部参考】\n记忆材料"),
            user("clock", HOST_TIME),
            user("details", "当前聊天额外注意事项：\n要结合刚才的聊天"),
            user("final", "新的宿主尾部文案"),
        ]
        items = history + host_tail
        out = inject_into_items_before_tail(items, "日程状态")
        self.assertEqual(out[:len(history)], history)
        self.assertEqual(out[len(history) + 1:], host_tail)
        self.assertEqual(len(items), len(history) + len(host_tail))
        inserted = out[len(history)]
        self.assertEqual(inserted["item_type"], "UserMessageItem")
        self.assertTrue(first_text(inserted).startswith(LBM_CONTEXT_PREFIX))
        self.assertTrue(inserted["meta"]["item_id"].startswith("lbm-ctx-"))
        datetime.fromisoformat(inserted["meta"]["timestamp"])
        self.assertIs(out[3], call)
        self.assertIs(out[4], result)
        self.assertIs(inject_into_items_before_tail(out, "日程状态"), out)

    def test_focus_recognizes_whole_multiline_roster(self) -> None:
        from core.inject import inject_into_items_before_tail

        history = [system("Focus 人设"), user("incoming", HISTORY),
                   user("focus-wait", "[Focus 等待结束]\n已等待。")]
        roster = user("roster", "在 群甲(s-1) 中的注意信息：第一行\n第二行\n\n第三行"
                               "\n在 群乙(s-2) 中的注意信息：无额外注意信息")
        ref = user("reference", '<focus_reference session_id="s-1">\n资料\n</focus_reference>')
        host_tail = [roster, ref, user("time", HOST_TIME), user("later", "宿主本轮指令")]
        out = history + host_tail
        for tag in ("关系", "情绪", "当前活动"):
            out = inject_into_items_before_tail(out, tag)
        self.assertEqual(out[:len(history)], history)
        self.assertEqual(out[-len(host_tail):], host_tail)
        injected = out[len(history):-len(host_tail)]
        self.assertEqual([first_text(entry) for entry in injected],
                         [f"<lbm-context>\n{tag}\n</lbm-context>" for tag in ("关系", "情绪", "当前活动")])
        self.assertEqual(len({entry["meta"]["item_id"] for entry in injected}), 3)

    def test_preserves_legacy_projection(self) -> None:
        from core.inject import project_planner_injection, project_replyer_injection

        messages = [{"role": "system", "content": "人设"}, {"role": "user", "content": "旧版来信"}]
        out = project_planner_injection("messages", messages, "日程")
        self.assertEqual(out, [messages[0], {"role": "system", "content": "日程"}, messages[1]])
        self.assertEqual(len(messages), 2)
        self.assertEqual(project_replyer_injection("messages", messages, "日程"), out)

    def test_synthetic_user_context_is_not_incoming_or_screen_anchor(self) -> None:
        from companion.screen_context import find_last_real_item_index
        from companion.sender_identity import sender_ids_in_window
        from core.inject import extract_last_user_text, inject_into_items_before_tail
        from planning.inject_service import InjectService

        incoming = user("incoming", HISTORY)
        injected = inject_into_items_before_tail([incoming, user("time", HOST_TIME)], "背景资料")[1]
        self.assertEqual(extract_last_user_text([incoming, injected], None), "历史原文")
        self.assertEqual(find_last_real_item_index([incoming, injected]), 0)
        self.assertEqual(InjectService._extract_last_user_text([incoming, injected]), "历史原文")
        cache = NS(get_user_id=lambda msg_id: "700001" if msg_id == "real-a" else "")
        self.assertEqual(sender_ids_in_window([incoming, injected], cache, 1), ["700001"])

    def test_replyer_updates_only_final_text_part_and_preserves_images(self) -> None:
        from core.inject import project_replyer_injection

        initial = {"type": "text", "text": f"额外信息\n{REPLYER_TIME}\n历史"}
        image = {"type": "image", "image_format": "jpeg", "image_base64": "aW1n"}
        closing = {"type": "text", "text": "宿主目标行\n请注意不要输出多余内容。\n现在，你说："}
        prompt = {**user("replyer", ""), "parts": [initial, image, closing]}
        items = [system("宿主人设"), prompt]
        out = project_replyer_injection("items", items, "视频分享事实")
        self.assertIs(out[0], items[0])
        self.assertIs(out[1]["meta"], prompt["meta"])
        self.assertIs(out[1]["parts"][0], initial)
        self.assertIs(out[1]["parts"][1], image)
        self.assertEqual(prompt["parts"][2], closing)
        tail = out[1]["parts"][2]["text"]
        self.assertIn("宿主目标行\n请注意不要输出多余内容。", tail)
        self.assertTrue(tail.endswith("现在，你说："))
        self.assertIs(project_replyer_injection("items", out, "视频分享事实"), out)

    def test_replyer_never_rewrites_wrapped_history(self) -> None:
        from core.inject import project_replyer_injection

        items = [system("人设"), user("real", f"{HISTORY}\n{REPLYER_TIME}")]
        self.assertIs(project_replyer_injection("items", items, "新资料"), items)

    def test_replyer_legacy_multimodal_content_preserved(self) -> None:
        from core.inject import project_replyer_injection

        first = {"type": "text", "text": REPLYER_TIME}
        image = {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aW1n"}}
        last = {"type": "text", "text": "回复指导\n现在，你说："}
        entry = {"role": "user", "content": [first, image, last]}
        out = project_replyer_injection("messages", [entry], "情绪")
        self.assertEqual(len(out), 1)
        self.assertIs(out[0]["content"][0], first)
        self.assertIs(out[0]["content"][1], image)
        self.assertEqual(entry["content"][-1], last)
        self.assertIn("<lbm-context>\n情绪", out[0]["content"][-1]["text"])


if __name__ == "__main__":
    unittest.main()
