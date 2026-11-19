from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.proactive_usage import HOST_USAGE_TOOL, ProactiveUsage, read_host_turn_usage
from core.inject import CATEYE_PLUGIN_ID


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
        before = await self.subject.planner_before({**tick, "items": [item]})
        assert [entry["function"]["name"] for entry in before["tool_definitions"]] == ["reply", "wait"]
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
        assert "只随口说一句" in prepared["reply_tool_args"]["chat_anlyze"]
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
        before = await reloaded.planner_before({**tick, "items": [item, message]})
        assert before["items"] == [message] and before["tool_definitions"] == definitions[:-1]
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


if __name__=="__main__":
    unittest.main()
