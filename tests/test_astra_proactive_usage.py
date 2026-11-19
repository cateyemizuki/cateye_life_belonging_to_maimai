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
