"""Verify native Bilibili mini-app generation, unchanged transport and independent QQ receipt matching."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.video_card import VideoCardService

BV = "BV0000000001"
PAGE = "https://www.bilibili.com/video/" + BV
PEER = "10001"
GROUP = "20002"
BOT = "40001"
MID = -171476788
TITLE = "Bili 视频"
PLACEHOLDER = "发送者版本过低，无法展示内容"


def native(launch="m.q.qq.com/a/s/provider-launch"):
    return {"app": "com.tencent.miniapp_01", "view": "view_8C8E89B49BE609866298ADDFF2DBABA4",
            "config": {"token": "provider-signed-config-token", "forward": 1},
            "meta": {"detail_1": {"title": TITLE, "appid": "1109937557", "scene": 0, "url": launch,
                                  "host": {"uin": int(BOT), "nick": "Source"}}},
            "miniappShareOrigin": 3, "miniappOpenRefer": "10002"}


def card():
    return {"ark_json": json.dumps(native(), ensure_ascii=False, indent=2)}


def okay(data):
    return {"status": "ok", "retcode": 0, "data": data}


def event(kind="private", **overrides):
    row = {"message_type": kind, "message_id": MID, "real_id": MID, "message_seq": 20,
           "time": int(time.time()), "user_id": int(BOT), "sender": {"user_id": int(BOT)},
           "message": [{"type": "json", "data": {"data": json.dumps(native())}}]}
    if kind == "group":
        row.update(group_id=int(GROUP), anonymous=None)
        row["sender"].update(role="member", card="")
    else:
        row["target_id"] = int(PEER)
    row.update(overrides)
    return row


def history(*messages):
    return okay({"messages": list(messages)})


def route(kind="private"):
    return {"kind": kind, "account_id": BOT,
            **({"group_id": GROUP} if kind == "group" else {"user_id": PEER})}


def actions(api):
    return [call.kwargs["action_name"] for call in api.await_args_list]


class VideoCardConstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        override = patch("integrations.video_card._HISTORY_DELAYS", (0, 0))
        override.start()
        self.addCleanup(override.stop)

    async def test_generate_only_native_miniapp_and_preserve_provider_json_bytes(self):
        for launch in ("m.q.qq.com/a/s/native", "//m.q.qq.com/a/s/native", "https://m.q.qq.com/a/s/native"):
            with self.subTest(launch=launch):
                raw = json.dumps(native(launch), ensure_ascii=False, indent=2) + "\n"
                api = AsyncMock(return_value={"success": True, "result": okay({"data": raw})})
                result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
                self.assertEqual(result, {"success": True, "card": {"ark_json": raw}})
                self.assertEqual(actions(api), ["get_mini_app_ark"])
                self.assertEqual(api.await_args.kwargs["params"]["type"], "bili")
                self.assertEqual(api.await_args.kwargs["params"]["jumpUrl"], PAGE)
                self.assertNotIn("qqdocurl", json.loads(raw)["meta"]["detail_1"])

    async def test_provider_dict_is_only_serialized_without_editing_signed_fields(self):
        supplied = native()
        supplied["needShareCallBack"] = True
        supplied["meta"]["detail_1"].update(qqdocurl="https://b23.tv/provider", scene=1036)
        api = AsyncMock(return_value=okay({"data": supplied}))
        result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
        self.assertEqual(json.loads(result["card"]["ark_json"]), supplied)
        self.assertEqual(actions(api), ["get_mini_app_ark"])

    async def test_invalid_provider_card_does_not_fallback(self):
        bad = [native("http://m.q.qq.com/a/s/untrusted"), native("https://example.com/a/s/untrusted")]
        bad.append({"app": "com.tencent.structmsg", "meta": {"news": {"title": TITLE}}})
        for data in bad:
            api = AsyncMock(return_value=okay({"data": data}))
            result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_class"], "card_generation_failed")
            self.assertEqual(actions(api), ["get_mini_app_ark"])
        api = AsyncMock()
        self.assertFalse((await VideoCardService(api).generate_card(bvid="bad", title=TITLE))["success"])
        api.assert_not_awaited()

    async def test_private_accepts_only_matching_signed_qq_roaming_record(self):
        # QQ may normalize the launch scheme or include interface fields, but
        # opaque launch identity and token remain identical to the sent card.
        replayed = native("https://m.q.qq.com/a/s/provider-launch")
        replayed["needShareCallBack"] = False
        row = event(message=[{"type": "json", "data": {"data": json.dumps(replayed)}}])
        api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": MID}), history(), history(row)])
        result = await VideoCardService(api).send_card(route(), card())
        self.assertEqual(result["message_id"], MID)
        self.assertTrue(result["delivery_verified"])
        self.assertEqual(actions(api), ["get_login_info", "send_private_msg",
                                        "get_friend_msg_history", "get_friend_msg_history"])
        self.assertEqual(api.await_args_list[1].kwargs["params"], {"user_id": int(PEER), "message": [
            {"type": "json", "data": {"data": card()["ark_json"]}}]})
        for call in api.await_args_list[2:]:
            self.assertNotIn("message_id", call.kwargs["params"])

    async def test_group_rejects_synthetic_self_echo_even_with_matching_id_and_payload(self):
        synthetic = event("group", sender={"user_id": int(BOT), "nickname": "self"})
        synthetic.pop("anonymous")
        api = AsyncMock(side_effect=[
            okay({"user_id": BOT}), okay({"message_id": MID}), history(synthetic), history(synthetic),
        ])
        result = await VideoCardService(api).send_card(route("group"), card())
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "card_history_unavailable")
        self.assertEqual(api.await_args.kwargs["params"], {"group_id": int(GROUP), "count": 16,
                                                          "message_id": MID, "reverse_order": False})
        self.assertEqual(actions(api).count("send_group_msg"), 1)

    async def test_group_accepts_canonical_qq_group_row(self):
        api = AsyncMock(side_effect=[
            okay({"user_id": BOT}), {"success": True, "data": {"message_id": MID}}, history(event("group")),
        ])
        result = await VideoCardService(api).send_card(route("group"), card())
        self.assertTrue(result["delivery_verified"])
        self.assertEqual(result["message_id"], MID)
        self.assertEqual(actions(api), ["get_login_info", "send_group_msg", "get_group_msg_history"])
        self.assertEqual(api.await_args_list[1].kwargs["params"]["message"], [
            {"type": "json", "data": {"data": card()["ark_json"]}}])

    async def test_exact_qq_placeholder_is_seen_but_never_confirms_delivery(self):
        for kind in ("group", "private"):
            with self.subTest(kind=kind):
                row = event(kind, message=[{"type": "text", "data": {"text": PLACEHOLDER}}])
                api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": MID}), history(row)])
                result = await VideoCardService(api).send_card(route(kind), card())
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "qq_card_replaced")
                self.assertNotIn("not_sent", result)
                self.assertNotIn("message_id", result)
                self.assertEqual(actions(api).count(f"send_{kind}_msg"), 1)

    async def test_same_title_or_wrong_id_peer_token_and_scope_are_unconfirmed(self):
        different_token = native()
        different_token["config"]["token"] = "other-provider-token"
        different = [{"type": "json", "data": {"data": json.dumps(different_token)}}]
        for bad in (
            event(message_id=2000, real_id=2000), event(target_id=12000),
            event(user_id=12000, sender={"user_id": 12000}),
            event(message=different), event(message_seq=0),
            event(time=int(time.time())-100),
        ):
            with self.subTest(bad=bad):
                api = AsyncMock(side_effect=[
                    okay({"user_id": BOT}), okay({"message_id": MID}), history(bad), history(),
                ])
                result = await VideoCardService(api).send_card(route(), card())
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "card_not_confirmed")
                self.assertEqual(actions(api).count("send_private_msg"), 1)

    async def test_missing_adapter_id_and_opaque_gate_failure_do_not_start_retries(self):
        api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": 0})])
        result = await VideoCardService(api).send_card(route(), card())
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
        self.assertEqual(actions(api), ["get_login_info", "send_private_msg"])
        api = AsyncMock(side_effect=[okay({"user_id": BOT})])
        result = await VideoCardService(api).send_card(route(), card(), before_send=lambda: (False, "changed"))
        self.assertTrue(result["not_sent"])
        self.assertEqual(result["error_class"], "gate_changed")
        self.assertEqual(actions(api), ["get_login_info"])

    async def test_same_scope_concurrency_does_not_redeliver_native_payload(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def api(_name, *, action_name, params):
            if action_name == "get_login_info":
                return okay({"user_id": BOT})
            if action_name == "get_friend_msg_history":
                return history()
            self.assertEqual(action_name, "send_private_msg")
            entered.set()
            await release.wait()
            return okay({"message_id": MID})

        service = VideoCardService(api)
        first = asyncio.create_task(service.send_card(route(), card()))
        await asyncio.wait_for(entered.wait(), timeout=2)
        try:
            blocked = await service.send_card(route(), card())
            self.assertTrue(blocked["not_sent"])
            self.assertEqual(blocked["error_class"], "card_send_in_progress")
        finally:
            release.set()
        self.assertTrue((await first)["uncertain"])


if __name__ == "__main__":
    unittest.main()
