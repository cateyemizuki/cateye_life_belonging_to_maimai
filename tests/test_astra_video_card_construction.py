"""Exercise Tuwen Ark action arguments and independently matched QQ chat receipts."""
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
ANCHOR = -120
MID = -171476788
TITLE = "Example video"
DESC = "哔哩哔哩视频分享"
PLACEHOLDER = "发送者版本过低，无法展示内容"


def descriptor():
    return {"title": TITLE, "desc": DESC, "jump_url": PAGE}


def okay(data):
    return {"status": "ok", "retcode": 0, "data": data}


def event(kind="private", **overrides):
    # The carrier fields emitted by QQ are not prescribed by the action.
    data = {"app": "unknown.QQ.news", "config": {"server_token": "opaque"}, "meta": {
        "variant": {"title": TITLE, "jumpUrl": PAGE, "preview": "https://qq.ugcimg.cn/server-preview"}}}
    row = {"message_type": kind, "message_id": MID, "real_id": MID, "message_seq": 20,
           "time": int(time.time()), "user_id": int(BOT), "sender": {"user_id": int(BOT)},
           "message": [{"type": "json", "data": {"data": json.dumps(data)}}]}
    row["group_id" if kind == "group" else "target_id"] = int(GROUP if kind == "group" else PEER)
    row.update(overrides)
    return row


def history(*messages):
    return okay({"messages": list(messages)})


def route(kind="private"):
    return {"kind": kind, "account_id": BOT,
            **({"group_id": GROUP} if kind == "group" else {"user_id": PEER})}


def placeholder():
    return [{"type": "text", "data": {"text": PLACEHOLDER}}]


def actions(api):
    return [call.kwargs["action_name"] for call in api.await_args_list]


class VideoCardConstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        override = patch("integrations.video_card._HISTORY_DELAYS", (0, 0))
        override.start()
        self.addCleanup(override.stop)

    async def test_prepare_is_local_native_descriptor_only(self):
        api = AsyncMock()
        service = VideoCardService(api)
        result = await service.generate_card(bvid=BV, title=TITLE)
        self.assertEqual(result, {"success": True, "card": descriptor()})
        self.assertFalse((await service.generate_card(bvid="invalid", title=TITLE))["success"])
        api.assert_not_awaited()

    async def test_private_accepts_only_new_qq_roaming_card_after_index_delay(self):
        api = AsyncMock(side_effect=[
            {"success": True, "result": okay({"user_id": int(BOT)})},
            history(), okay(None), history(), {"success": True, "result": history(event())},
        ])
        result = await VideoCardService(api).send_card(route(), descriptor())
        self.assertEqual(result["message_id"], MID)
        self.assertTrue(result["success"])
        self.assertTrue(result["delivery_verified"])
        self.assertEqual(actions(api), ["get_login_info", "get_friend_msg_history", "send_tuwen_ark",
                                        "get_friend_msg_history", "get_friend_msg_history"])
        self.assertEqual(api.await_args_list[2].kwargs["params"], {"user_id": int(PEER), **descriptor()})
        for index in (1, 3, 4):
            args = api.await_args_list[index].kwargs["params"]
            self.assertEqual(args["user_id"], int(PEER))
            self.assertNotIn("message_id", args)

    async def test_group_uses_forward_read_from_exact_prior_anchor(self):
        earlier = event("group", message_id=ANCHOR, real_id=ANCHOR, message_seq=10)
        api = AsyncMock(side_effect=[
            okay({"user_id": BOT}), history(earlier), {"success": True, "data": okay(None)},
            history(earlier, event("group")),
        ])
        result = await VideoCardService(api).send_card(route("group"), descriptor())
        self.assertTrue(result["delivery_verified"])
        self.assertEqual(result["message_id"], MID)
        self.assertEqual(actions(api), ["get_login_info", "get_group_msg_history", "send_tuwen_ark",
                                        "get_group_msg_history"])
        self.assertEqual(api.await_args_list[2].kwargs["params"], {"group_id": int(GROUP), **descriptor()})
        self.assertEqual(api.await_args.kwargs["params"], {"group_id": int(GROUP), "count": 16,
                                                          "message_id": ANCHOR, "reverse_order": False})

    async def test_exact_qq_placeholder_is_observed_but_never_success(self):
        for kind in ("group", "private"):
            with self.subTest(kind=kind):
                translated = event(kind, message=placeholder())
                api = AsyncMock(side_effect=[
                    okay({"user_id": BOT}), history(), okay(None), history(translated), history(translated),
                ])
                result = await VideoCardService(api).send_card(route(kind), descriptor())
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "qq_card_replaced")
                self.assertNotIn("not_sent", result)
                self.assertNotIn("message_id", result)
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)

    async def test_older_own_wrong_peer_and_wrong_sender_never_confirm(self):
        for kind in ("group", "private"):
            with self.subTest(kind=kind):
                wrong_peer = {"target_id" if kind == "private" else "group_id": 43210}
                card_row = event(kind)
                noisy = [
                    card_row,
                    event(kind, message_id=300, real_id=300, message_seq=21, **wrong_peer),
                    event(kind, message_id=301, real_id=301, message_seq=22,
                          user_id=12000, sender={"user_id": 12000}),
                    event(kind, message_id=302, real_id=302, message_seq=23, time=int(time.time()) - 100),
                    event(kind, message_id=303, real_id=303, message_seq=0),
                ]
                api = AsyncMock(side_effect=[
                    okay({"user_id": BOT}), history(card_row), okay(None), history(*noisy), history(*noisy),
                ])
                result = await VideoCardService(api).send_card(route(kind), descriptor())
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "card_not_confirmed")
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)

    async def test_same_url_in_distinct_objects_or_multiple_matching_events_are_ambiguous(self):
        split = [{"type": "json", "data": {"data": {
            "meta": {"first": {"title": TITLE}, "second": {"jump_url": PAGE}}}}}]
        for rows in ([event(message=split)], [event(), event(message_id=77, real_id=77)]):
            with self.subTest(rows=rows):
                api = AsyncMock(side_effect=[
                    okay({"user_id": BOT}), history(), okay(None), history(*rows), history(*rows),
                ])
                result = await VideoCardService(api).send_card(route(), descriptor())
                self.assertEqual(result["error_class"], "card_not_confirmed")
                self.assertNotIn("message_id", result)

    async def test_missing_baseline_and_failed_rechecks_report_history_unavailable(self):
        failed = {"status": "failed", "retcode": 1200}
        for steps in ([failed, okay(None)], [history(), okay(None), failed, RuntimeError("unavailable")]):
            with self.subTest(steps=steps):
                api = AsyncMock(side_effect=[okay({"user_id": BOT}), *steps])
                result = await VideoCardService(api).send_card(route(), descriptor())
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "card_history_unavailable")
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)

    async def test_unsupported_action_and_explicit_native_rejection(self):
        for payload, error_class in (
            ({"success": False, "error": "Unknown action: send_tuwen_ark"}, "card_action_unsupported"),
            ({"status": "failed", "retcode": 1200, "wording": "permission denied"}, "send_rejected"),
            (RuntimeError("unsupported action send_tuwen_ark"), "card_action_unsupported"),
        ):
            with self.subTest(payload=payload):
                api = AsyncMock(side_effect=[okay({"user_id": BOT}), history(), payload])
                result = await VideoCardService(api).send_card(route(), descriptor())
                self.assertTrue(result["not_sent"])
                self.assertEqual(result["error_class"], error_class)
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)

    async def test_transport_uncertainty_has_no_second_send(self):
        for payload in (None, RuntimeError("no response"), {"success": False, "error": "QQ receipt timeout"}):
            with self.subTest(payload=payload):
                api = AsyncMock(side_effect=[okay({"user_id": BOT}), history(), payload])
                result = await VideoCardService(api).send_card(route(), descriptor())
                self.assertTrue(result["uncertain"])
                self.assertNotIn("not_sent", result)
                self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)

    async def test_same_scope_concurrent_invocation_is_not_dispatched(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def api(_name, *, action_name, params):
            if action_name == "get_login_info":
                return okay({"user_id": BOT})
            if action_name == "get_friend_msg_history":
                return history()
            self.assertEqual(action_name, "send_tuwen_ark")
            entered.set()
            await release.wait()
            return okay(None)

        service = VideoCardService(api)
        first = asyncio.create_task(service.send_card(route(), descriptor()))
        await entered.wait()
        try:
            blocked = await service.send_card(route(), descriptor())
            self.assertTrue(blocked["not_sent"])
            self.assertEqual(blocked["error_class"], "card_send_in_progress")
        finally:
            release.set()
        result = await first
        self.assertTrue(result["uncertain"])


if __name__ == "__main__":
    unittest.main()
