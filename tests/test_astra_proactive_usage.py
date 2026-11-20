from __future__ import annotations

import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.proactive_usage import HOST_USAGE_TOOL, ProactiveUsage, active_video_comment_task, read_host_turn_usage
from core.inject import CATEYE_PLUGIN_ID
from integrations.proactive_reply_target import (
    PROACTIVE_REPLY_NOTICE,
    append_proactive_notice,
    payload_has_proactive_notice,
    rewrite_replyer_payload,
)


def block(token, kind="greeting", scope="target"):
    return {"item_type": "UserMessageItem", "parts": [{"type": "text", "text":
        f'<plugin_proactive_task id="proactive:{CATEYE_PLUGIN_ID}:1" plugin_id="{CATEYE_PLUGIN_ID}">\n'
        f'附加信息：{{"lbm_turn_id":"{token}","lbm_proactive_kind":"{kind}","greeting_scope":"{scope}"}}\n'
        "</plugin_proactive_task>"}]}


def call(name, **args):
    return {"item_type": "FunctionCallItem", "tool_call": {"func_name": name, "args": args}}


class ProactiveUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.turn = {"kind":"morning_greeting","category":"greeting",
                     "tracked":True,"stream_id":"s","turn_id":"pt-123","chat_type":"private_target"}
        self.record = Mock()
        self.service = NS(target_greeting_proactive_allowed=AsyncMock(return_value=True),
                          group_morning_proactive_allowed=AsyncMock(return_value=True))
        plugin=NS(_mai_proactive_turns=lambda:NS(
            identified=lambda stream,turn_id="",host_task_id="": self.turn if stream=="s" and turn_id=="pt-123" else None),
            _llm=NS(_usage_tracker=NS(record_v2=self.record)),_planning=NS(proactive=self.service))
        self.now = 100.0
        self.subject = ProactiveUsage(plugin,clock=lambda:self.now)
        self.planner = {"session_id":"s","selected_history_count":6,"selection_reason":"trigger"}
        self.usage = {"prompt_tokens":50,"completion_tokens":10,"total_tokens":60}

    async def test_declined_or_blocked_greetings(self):
        other={"item_type":"UserMessageItem","parts":[{"type":"text","text":"<message>在读最新消息"}]}
        queued=block("pt-123","greeting","group")
        self.service.group_morning_proactive_allowed.return_value=False
        before=await self.subject.planner_before({**self.planner,"items":[queued]})
        assert before["items"]==[]
        assert self.subject.planner_after({**self.planner,**self.usage,"output_items":[]})["output_items"]==[]
        self.record.assert_not_called()
        before=await self.subject.planner_before({**self.planner,"items":[queued,other]})
        assert before["items"]==[other]
        self.subject.planner_after({**self.planner,**self.usage,"output_items":[]})
        assert self.service.group_morning_proactive_allowed.await_count==1
        await self.subject.planner_before({**self.planner,"items":[block("pt-123")]})
        self.subject.planner_after({**self.planner,**self.usage,"output_items":[call("wait")]})
        assert self.record.call_count==1
        assert self.record.call_args.kwargs["event"]=="proactive_greeting_planner"
        await self.subject.planner_before({**self.planner,"items":[block("pt-123")]})
        declined=self.subject.planner_after({**self.planner,**self.usage,"output_items":[
            {"item_type":"AssistantMessageItem","parts":[{"type":"text","text":"这会儿先等等"}]}
        ]})
        assert declined["output_items"]==[]
        ordinary=self.subject.planner_after({**self.planner,**self.usage,"output_items":[
            {"item_type":"AssistantMessageItem","parts":[{"type":"text","text":"普通回复"}]}
        ]})
        assert len(ordinary["output_items"])==1

    async def test_idempotent_role_usage_and_actual_send(self):
        await self.subject.planner_before({**self.planner,"items":[block("pt-123")]})
        output=self.subject.planner_after({**self.planner,**self.usage,
                                          "output_items":[call("reply",chat_anlyze="内容")]})
        args=output["output_items"][0]["tool_call"]["args"]
        assert "lbm-proactive-reply" in args["chat_anlyze"]
        wrong=self.subject.reply_before({"session_id":"other","reply_tool_args":args})
        assert self.subject.reply_turn(wrong) is None
        request=self.subject.reply_before({"session_id":"s","reply_tool_args":args,"attempt":1})
        assert request["reply_tool_args"]["chat_anlyze"]=="内容"
        assert self.subject.reply_turn(request)["turn_id"]=="pt-123"
        result={**request,**self.usage,"response":"今天阳光挺好。要不要出去走走"}
        reply=self.subject.reply_after(result)
        self.subject.reply_after(result)
        self.subject.reply_after({**result,"attempt":2})
        assert [call.kwargs["event"] for call in self.record.call_args_list]==[
            "proactive_greeting_planner","proactive_greeting_replyer","proactive_greeting_replyer"]
        self.subject.before_post_process({**result,"reply_tool_args":args})
        self.subject.after_split({"session_id":"s","segments":[
               {"text":"今天阳光挺好。"},{"text":"要不要出去走走"}]})
        sent={"message":{"session_id":"s","timestamp":"98.5","raw_message":[
                {"type":"at","data":{"target_user_id":"guest"}},{"type":"text","data":"今天阳光挺好！"}]}}
        assert self.subject.confirm_sent({**sent,"sent":False}) is None
        assert self.subject.confirm_sent({**sent,"sent":True})==(reply,98.5)
        assert self.subject.confirm_sent({**sent,"sent":True}) is None

    async def test_concurrent_same_stream_delivery_isolated_by_type_and_unique_body(self):
        missing = {"kind":"miss","category":"missing","tracked":True,"stream_id":"s",
                   "turn_id":"pt-miss","optional_key":"m","optional_intent":"missing"}
        self.subject._stage_send({"session_id":"s","response":"早上好"},"greeting-nonce",self.turn)
        self.subject._stage_send({"session_id":"s","response":"想问问你今天怎么样"},"miss-nonce",missing)
        self.subject.before_post_process({"session_id":"s","response":"普通回复"})
        def sent(body):
            return {"sent":True,"message":{"session_id":"s","processed_plain_text":body}}
        assert self.subject.confirm_sent(sent("普通回复")) is None
        assert self.subject.confirm_sent(sent("早上好"))[0]["category"]=="greeting"
        assert self.subject.confirm_sent(sent("想问问你今天怎么样"))[0]["category"]=="missing"

        self.subject._stage_send({"session_id":"s","response":"早上好"},"another-greeting",self.turn)
        self.subject.before_post_process({"session_id":"s","response":"早上好"})
        assert self.subject.confirm_sent(sent("早上好")) is None

    async def test_ambiguous_planner_overlap_and_legacy(self):
        await self.subject.planner_before({**self.planner,"items":[block("pt-123")]})
        await self.subject.planner_before({**self.planner,"items":[]})
        for _ in range(2):
            out=self.subject.planner_after({**self.planner,**self.usage,"output_items":[call("reply",chat_anlyze="内容")]})
            assert out["output_items"][0]["tool_call"]["args"]["chat_anlyze"]=="内容"
        self.record.assert_not_called()
        legacy=block("pt-123")["parts"][0]["text"]
        await self.subject.planner_before({**self.planner,"messages":[{"role":"user","content":legacy}]})
        result=self.subject.planner_after({**self.planner,**self.usage,
                    "tool_calls":[{"function":{"name":"reply","arguments":{"chat_anlyze":"内容"}}}]})
        assert "[lbm-proactive-reply:" in result["tool_calls"][0]["function"]["arguments"]["chat_anlyze"]

    async def test_legacy_mixed_media_input_supersedes_greeting(self):
        queued = {"role": "user", "content": block("pt-123", "greeting", "group")["parts"][0]["text"]}
        incoming = {"role": "user", "content": [
            '<message msg_id="latest" user="guest">',
            ["png", "aW1hZ2U="],
            "Here is the picture.</message>",
        ]}
        before = await self.subject.planner_before({**self.planner, "messages": [queued, incoming]})
        assert before["messages"] == [incoming]
        self.service.group_morning_proactive_allowed.assert_not_awaited()
        tool_calls = [{"function": {"name": "reply", "arguments": {"chat_anlyze": "Context"}}}]
        result = self.subject.planner_after({**self.planner, **self.usage, "tool_calls": tool_calls})
        assert result["tool_calls"] == tool_calls
        self.record.assert_not_called()

    async def test_card_debug_filters_native_functions_and_preserves_new_input(self):
        from integrations.video_share_policy import DEBUG_CARD_TOOL, DEBUG_PLANNER_KIND

        self.turn.update(kind=DEBUG_PLANNER_KIND, category="video_share")
        queued = block("pt-123", "video_share")
        queued["parts"][0]["text"] = queued["parts"][0]["text"].replace(
            '"greeting_scope":"target"', '"lbm_card_debug":true,"lbm_card_ticket":"actual-proof"'
        )
        definitions = [{"type": "function", "function": {"name": name}} for name in
                       ("reply", "wait", "lbm_share_recent_video", DEBUG_CARD_TOOL)]
        request = {**self.planner, "tool_definitions": definitions}
        before = await self.subject.planner_before({**request, "items": [queued]})
        assert [item["function"]["name"] for item in before["tool_definitions"]] == ["wait", DEBUG_CARD_TOOL]
        assert "actual-proof" not in before["items"][0]["parts"][0]["text"]
        result = self.subject.planner_after({**self.planner, **self.usage, "output_items": [
            call("lbm_share_recent_video", to_companion=True), call(DEBUG_CARD_TOOL, ticket="injected-by-model"),
            call(DEBUG_CARD_TOOL),
        ]})
        assert result["output_items"] == [call(DEBUG_CARD_TOOL, ticket="actual-proof")]
        assert self.record.call_args.kwargs["event"] == "proactive_video_share_planner"

        await self.subject.planner_before({**request, "items": [queued]})
        decline = self.subject.planner_after({**self.planner, **self.usage, "output_items": [
            {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "I will skip."}]},
            call("wait"), call(DEBUG_CARD_TOOL),
        ]})
        assert decline["output_items"] == [call("wait")]

        incoming = {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "<message>latest</message>"}]}
        ordinary = await self.subject.planner_before({**request, "items": [queued, incoming]})
        assert ordinary["items"] == [incoming]
        assert ordinary["tool_definitions"] == definitions[:-1]
        reply = call("reply", chat_anlyze="ordinary")
        assert self.subject.planner_after({**self.planner, **self.usage, "output_items": [reply]})["output_items"] == [reply]

        legacy = {"role": "user", "content": queued["parts"][0]["text"]}
        await self.subject.planner_before({**request, "messages": [legacy]})
        legacy_response = self.subject.planner_after({**self.planner, **self.usage, "response": "No additional text.",
              "tool_calls": [{"function": {"name": DEBUG_CARD_TOOL, "arguments": {}}}]})
        assert legacy_response["response"] == ""
        assert legacy_response["tool_calls"] == [{"function": {"name": DEBUG_CARD_TOOL, "arguments": {"ticket": "actual-proof"}}}]

    async def test_video_comment_once_decline_and_usage_on_both_host_formats(self):
        from integrations.video_share_policy import COMMENT_PLANNER_KIND

        definitions = [{"type": "function", "function": {"name": name}}
                       for name in ("reply", "wait", "lbm_share_recent_video", "lbm_debug_share_confirm")]
        plugin = self.subject._plugin
        service = NS(allowed=AsyncMock(return_value=True), active=Mock(return_value=True))
        plugin._video_share_comment = lambda: service
        turns = {}

        def begin(stream, token, window):
            assert stream == "s" and window == 90
            if turns[token].get("planner_started"):
                return False
            turns[token]["planner_started"] = True
            return True

        plugin._mai_proactive_turns = lambda: NS(
            identified=lambda stream, turn_id="", **kwargs: turns.get(turn_id) if stream == "s" else None,
            begin_video_comment=begin,
        )

        def queued(key, *, payload_key):
            token = key
            turns[token] = {"turn_id": token, "kind": COMMENT_PLANNER_KIND,
                            "category": "video_share", "tracked": True}
            item = block(token, "video_share")
            item["parts"][0]["text"] = item["parts"][0]["text"].replace(
                '"greeting_scope":"target"', '"delivery_kind":"video_share_comment","lbm_video_sent_from":99.0')
            if payload_key == "messages":
                item = {"role": "user", "content": item["parts"][0]["text"]}
            return token, item

        tick = {**self.planner, "tool_definitions": definitions}
        token, item = queued("reply", payload_key="items")
        assert active_video_comment_task([item], "items")
        before = await self.subject.planner_before({**tick, "items": [item]})
        assert [entry["function"]["name"] for entry in before["tool_definitions"]] == ["reply", "wait"]
        assert "可选" in before["tool_definitions"][0]["function"]["description"]
        assert self.subject.video_comment_policy_applies(before)
        assert token not in str(before["items"])
        after = self.subject.planner_after({**self.planner, **self.usage, "output_items": [
            call("lbm_share_recent_video", to_companion=True),
            call("reply", chat_anlyze="a thought", msg_id="other", attach_at="123",
                 after_reply="continue", wait_seconds=2, session_id="other"),
            call("reply", chat_anlyze="second reply"),
        ]})
        assert len(after["output_items"]) == 1
        reply = after["output_items"][0]["tool_call"]
        assert reply["func_name"] == "reply" and set(reply["args"]) == {"chat_anlyze", "after_reply"}
        assert reply["args"]["after_reply"] == "wait"
        prepared = self.subject.reply_before({"session_id": "s", "reply_tool_args": reply["args"]})
        assert prepared["reply_tool_args"]["chat_anlyze"] == "a thought"
        self.subject.reply_after({**prepared, **self.usage})
        assert [entry.kwargs["event"] for entry in self.record.call_args_list] == [
            "proactive_video_share_planner", "proactive_video_share_replyer",
        ]
        assert all(entry.kwargs["module"] == "video" for entry in self.record.call_args_list)

        reloaded = ProactiveUsage(plugin, clock=lambda: self.now)
        assert turns[token]["planner_started"] is True
        before = await reloaded.planner_before({**tick, "items": [item]})
        assert before["items"] == []
        assert reloaded.planner_after({**self.planner, "output_items": [
            call("reply", chat_anlyze="should not run"),
        ]})["output_items"] == []

        _, item = queued("modern decline", payload_key="items")
        await reloaded.planner_before({**tick, "items": [item]})
        declined = reloaded.planner_after({**self.planner, **self.usage, "output_items": [
            {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "let it be"}]},
            call("reply", chat_anlyze="conflicts with decline"),
            call("wait", seconds=90),
        ]})
        assert declined["output_items"] == []
        assert not reloaded._reply_calls
        assert self.record.call_args.kwargs["event"] == "proactive_video_share_planner"

        _, item = queued("modern prose", payload_key="items")
        await reloaded.planner_before({**tick, "items": [item]})
        assert reloaded.planner_after({**self.planner, **self.usage, "output_items": [
            {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "internal decision"}]},
        ]})["output_items"] == []

        _, item = queued("older modern reply", payload_key="items")
        older_tool = {"type": "function", "function": {"name": "reply", "parameters": {
            "type": "object", "required": ["msg_id", "reply_reference"], "properties": {
                "after_reply": {"enum": ["continue", "stop", "wait"]},
                "reply_reference": {"type": "string", "description": "old prompt"},
            },
        }}}
        before = await reloaded.planner_before({**tick, "items": [item],
                                               "tool_definitions": [older_tool, definitions[1]]})
        visible = before["tool_definitions"][0]["function"]["parameters"]["properties"]["reply_reference"]
        assert "实际关系" in visible["description"] and "old prompt" not in visible["description"]
        older = reloaded.planner_after({**self.planner, **self.usage, "output_items": [
            call("reply", reply_reference="missing a real message"),
            call("reply", msg_id="m", reply_reference="related to their joke", after_reply="wait", wait_seconds=10),
        ]})["output_items"][0]["tool_call"]["args"]
        assert set(older) == {"msg_id", "reply_reference", "after_reply"}
        assert older["msg_id"] == "m" and older["after_reply"] == "stop"
        older_reply = reloaded.reply_before({"session_id": "s", "reply_tool_args": older})
        assert older_reply["reply_tool_args"]["reply_reference"] == "related to their joke"
        reloaded.reply_after({**older_reply, **self.usage})
        assert self.record.call_args.kwargs["event"] == "proactive_video_share_replyer"

        legacy_tick = {**tick, "tool_definitions": definitions + [
            {"type": "function", "function": {"name": name}} for name in ("finish", "no_action")]}
        _, item = queued("decline", payload_key="messages")
        before = await reloaded.planner_before({**legacy_tick, "messages": [item]})
        assert [entry["function"]["name"] for entry in before["tool_definitions"]] == [
            "reply", "wait", "finish", "no_action"]
        complete = reloaded.planner_after({**self.planner, **self.usage, "response": "别打扰他们",
            "tool_calls": [{"function": {"name": "wait", "arguments": {"seconds": 90}}},
                           {"function": {"name": "reply", "arguments": {"msg_id": "m"}}}],
        })
        assert complete["response"] == "" and complete["tool_calls"] == [
            {"function": {"name": "finish", "arguments": {}}}]
        assert self.record.call_args.kwargs["event"] == "proactive_video_share_planner"

        _, item = queued("legacy empty", payload_key="messages")
        await reloaded.planner_before({**legacy_tick, "messages": [item]})
        complete = reloaded.planner_after({**self.planner, "response": "", "tool_calls": []})
        tool = complete["tool_calls"][0]
        assert len(complete["tool_calls"]) == 1 and tool["id"]
        assert tool["function"] == {"name": "finish", "arguments": {}}

        _, item = queued("legacy reply", payload_key="messages")
        await reloaded.planner_before({**legacy_tick, "messages": [item]})
        complete = reloaded.planner_after({**self.planner, **self.usage, "response": "unused",
            "tool_calls": [{"function": {"name": "reply", "arguments": {"msg_id": "same-chat",
                                                                       "set_quote": True}}}],
        })
        legacy = complete["tool_calls"][0]["function"]["arguments"]
        assert legacy["msg_id"] == "same-chat" and legacy["set_quote"] is False
        assert "lbm-proactive-reply" in legacy["chat_anlyze"]
        assert len(complete["tool_calls"]) == 2 and complete["tool_calls"][1]["function"]["name"] == "finish"

        _, item = queued("ordinary", payload_key="items")
        message = {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "<message>new</message>"}]}
        assert not active_video_comment_task([item, message], "items")
        before = await reloaded.planner_before({**tick, "items": [item, message]})
        assert before["items"] == [message] and before["tool_definitions"] == definitions[:-1]
        assert not reloaded.video_comment_policy_applies(before)
        ordinary = call("reply", chat_anlyze="answer the new message")
        assert reloaded.planner_after({**self.planner, "output_items": [ordinary]})["output_items"] == [ordinary]

        _, item = queued("expired", payload_key="items")
        await reloaded.planner_before({**tick, "items": [item]})
        service.active.return_value = False
        expired = reloaded.planner_after({**self.planner, "output_items": [call("reply", chat_anlyze="too late")]})
        assert expired["output_items"] == []
        service.active.return_value = True

        service.allowed.return_value = False
        _, item = queued("busy", payload_key="items")
        before = await reloaded.planner_before({**tick, "items": [item]})
        assert before["items"] == [] and before["tool_definitions"] == [definitions[1]]
        after = reloaded.planner_after({**self.planner, "output_items": [
            {"item_type": "AssistantMessageItem", "parts": [{"type": "text", "text": "stray prose"}]},
            call("reply", chat_anlyze="busy"),
        ]})
        assert after["output_items"] == []


    async def test_host_query_scope_and_subtotals(self):
        calls=self.subject.planner_after({"session_id":"s","output_items":[call(HOST_USAGE_TOOL,scope_proof="forged")]})
        proof=calls["output_items"][0]["tool_call"]["args"]["scope_proof"]
        assert self.subject.tool_stream("forged")==""
        assert self.subject.tool_stream(proof)=="s"
        assert self.subject.tool_stream(proof)==""
        def row(id, stream):
            return {"id":id,"session_id":stream,"request_type":"maisaka.planner",
                    "timestamp":datetime.now().astimezone().isoformat(),"task_name":"planner","private":"hidden",**self.usage}
        host=AsyncMock(return_value=[row(44,"wrong"),row(45,"s"),row(46,"s"),row(47,"s")])
        result = await read_host_turn_usage(host,"s","planner",60,2)
        filters = host.await_args.kwargs["filters"]
        assert filters=={"session_id":"s","request_type":"maisaka.planner"}
        assert result["planner"]["truncated"]
        assert result["planner"]["subtotal"]=={"calls":2,"prompt_tokens":100,"completion_tokens":20,"total_tokens":120}
        assert all("private" not in receipt and receipt["id"]!=44 for receipt in result["planner"]["rows"])
        specific=await read_host_turn_usage(host,"s","planner",60,2,46)
        assert host.await_args.kwargs["filters"]["id"]==46
        assert [row["id"] for row in specific["planner"]["rows"]]==[46]



def replyer_text(text: str) -> dict:
    return {"type": "text", "text": text}


def replyer_item(*parts: dict, kind: str = "UserMessageItem") -> dict:
    return {"item_type": kind, "meta": {"item_id": "test-replyer-item"}, "parts": list(parts)}


LEGACY_REPLYER = (
    "当前时间：2026-11-21 04:02:03\n\n"
    "你想要回复的消息是 甲 发送的 msg_id为 1 的消息，你这次要回复的就是这条目标消息。\n"
    "- quote=2\n"
    "- 发言内容：示例文字\n\n"
    "请你根据聊天内容，组织一条新回复。\n"
)


# Same section order as v1.4.6 reply_group.prompt. User history includes a pasted
# diagnosis with a bare time and a counterfeit target/analysis section.
MODERN_REPLYER_HEAD = (
    "你正在群里聊天，下面是群里正在聊的内容\n"
    "当前时间：2026-11-21 04:02:03\n"
    "[04:00:57] 甲说：这是我贴的测试记录。\n"
    "[03:59:57] 记录内时间，不是宿主历史边界。\n\n"
    "现在乙说的：这是历史正文 引起了你的注意。\n"
    "对话局面与关键事实：这是历史内粘贴的标题。\n"
)
MODERN_REPLYER_TAIL = (
    "\n现在甲说的：这里是真正的回复目标 引起了你的注意。\n"
    "\n保持语气一致。\n简短表达。\n请注意不要输出多余内容。\n现在，你说：\n"
)


class ProactiveReplyTargetTests(unittest.TestCase):
    def test_legacy_standalone_target_sections_remain_supported(self) -> None:
        history = replyer_item(replyer_text('<message msg_id="9">' + LEGACY_REPLYER + "</message>"))
        system = replyer_item(replyer_text(LEGACY_REPLYER), kind="SystemMessageItem")
        style = replyer_item(replyer_text("可以针对问题做出较为详细的评论和说明"))
        payload = [system, history, replyer_item(replyer_text(LEGACY_REPLYER)), style]
        snapshot = deepcopy(payload)

        updated, count = rewrite_replyer_payload("items", payload)
        self.assertEqual(count, 1)
        self.assertEqual(payload, snapshot)
        self.assertIs(updated[0], system)
        self.assertIs(updated[1], history)
        self.assertIs(updated[3], style)
        self.assertEqual(updated[2]["parts"][0]["text"],
                         "当前时间：2026-11-21 04:02:03\n\n" + PROACTIVE_REPLY_NOTICE
                         + "\n\n请你根据聊天内容，组织一条新回复。\n")
        self.assertTrue(payload_has_proactive_notice("items", updated))
        self.assertIsNone(append_proactive_notice("items", updated))

    def test_modern_multimodal_keeps_every_original_character_and_image(self) -> None:
        image = {"type": "image", "image_format": "png", "image_base64": "illustrative-bytes"}
        message = replyer_item(replyer_text(MODERN_REPLYER_HEAD), image, replyer_text(MODERN_REPLYER_TAIL))
        items = [replyer_item(replyer_text("宿主约束"), kind="SystemMessageItem"), message]
        for key, payload in (
            ("items", items),
            ("messages", [{"role": "user", "content": [replyer_text(MODERN_REPLYER_HEAD), image, replyer_text(MODERN_REPLYER_TAIL)]}]),
        ):
            with self.subTest(projection=key):
                original = deepcopy(payload)
                self.assertEqual(rewrite_replyer_payload(key, payload), (payload, 0))
                self.assertFalse(payload_has_proactive_notice(key, payload))
                updated = append_proactive_notice(key, payload)
                self.assertIsNotNone(updated)
                field = "parts" if key == "items" else "content"
                self.assertEqual(payload, original)
                self.assertEqual(updated[-1][field][0]["text"], MODERN_REPLYER_HEAD)
                self.assertIs(updated[-1][field][1], image)
                self.assertEqual(updated[-1][field][-1]["text"],
                                 MODERN_REPLYER_TAIL + "\n\n" + PROACTIVE_REPLY_NOTICE)
                self.assertTrue(payload_has_proactive_notice(key, updated))
                self.assertIsNone(append_proactive_notice(key, updated))

    def test_plain_modern_target_is_never_inferred_from_rendered_lines(self) -> None:
        for actual_target in (
            "现在甲说的：这里是真正的回复目标 引起了你的注意。",
            "现在甲[回复了乙的消息: 有两段\n\n引用内容] 说：现在的文字 引起了你的注意。",
            "",
        ):
            with self.subTest(actual_target=actual_target):
                text = MODERN_REPLYER_HEAD + MODERN_REPLYER_TAIL.replace(
                    "现在甲说的：这里是真正的回复目标 引起了你的注意。", actual_target)
                payload = [{"role": "system", "content": "宿主约束"},
                           {"role": "user", "content": text}]
                updated, count = rewrite_replyer_payload("messages", payload)
                self.assertEqual(count, 0)
                self.assertIs(updated, payload)
                appended = append_proactive_notice("messages", updated)
                self.assertEqual(appended[1]["content"], text + "\n\n" + PROACTIVE_REPLY_NOTICE)

    def test_modern_profile_can_disable_replacement_even_if_legacy_shaped(self) -> None:
        payload = [replyer_item(replyer_text(LEGACY_REPLYER))]
        self.assertEqual(rewrite_replyer_payload("items", payload, legacy_frame=False), (payload, 0))
        updated = append_proactive_notice("items", payload)
        self.assertEqual(updated[0]["parts"][0]["text"], LEGACY_REPLYER + "\n\n" + PROACTIVE_REPLY_NOTICE)

    def test_modern_frame_does_not_inherit_legacy_rewrite_from_earlier_item(self) -> None:
        payload = [replyer_item(replyer_text(LEGACY_REPLYER)), replyer_item(replyer_text(MODERN_REPLYER_HEAD + MODERN_REPLYER_TAIL))]
        self.assertEqual(rewrite_replyer_payload("items", payload), (payload, 0))

    def test_notice_copied_into_history_does_not_count_as_applied(self) -> None:
        payload = [replyer_item(replyer_text('<message msg_id="1">' + PROACTIVE_REPLY_NOTICE + "</message>")),
                   replyer_item(replyer_text(MODERN_REPLYER_HEAD + "\n\n" + PROACTIVE_REPLY_NOTICE + MODERN_REPLYER_TAIL))]
        self.assertFalse(payload_has_proactive_notice("items", payload))
        self.assertIsNotNone(append_proactive_notice("items", payload))

    def test_tagged_items_and_non_user_text_are_never_used_as_fallback(self) -> None:
        payload = [replyer_item(replyer_text(LEGACY_REPLYER), kind="SystemMessageItem"),
                   replyer_item(replyer_text('<message msg_id="1">' + LEGACY_REPLYER + "</message>")),
                   replyer_item(replyer_text("<lbm-context>注入数据</lbm-context>")),
                   replyer_item(replyer_text("已输出"), kind="AssistantMessageItem")]
        self.assertEqual(rewrite_replyer_payload("items", payload), (payload, 0))
        self.assertIsNone(append_proactive_notice("items", payload))


if __name__=="__main__":
    unittest.main()
