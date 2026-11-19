"""Exercise the standard Bili link card transport and private QQ read-back contract."""
from __future__ import annotations

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
BOT = "40001"
MID = -171476788


def raw_link():
    return {"app": "com.tencent.structmsg", "view": "news", "meta": {"news": {"jumpUrl": PAGE}}}


def okay(data):
    return {"status": "ok", "retcode": 0, "data": data}


def row(**overrides):
    return {"message_type": "private", "message_id": MID, "real_id": MID,
            "message_seq": 20, "time": int(time.time()), "user_id": int(BOT), "target_id": int(PEER),
            "sender": {"user_id": int(BOT)}, "message": [{"type": "json", "data": {"data": json.dumps(raw_link())}}],
            **overrides}


class VideoCardConstructionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        override = patch("integrations.video_card._PRIVATE_HISTORY_DELAYS_SECONDS", (0, 0))
        override.start()
        self.addCleanup(override.stop)

    async def test_generation_is_local_and_does_not_invoke_miniapp_signer(self):
        api = AsyncMock()
        service = VideoCardService(api)
        result = await service.generate_ark(bvid=BV, title="Example")
        self.assertTrue(result["success"])
        card = json.loads(result["ark_json"])
        self.assertEqual(card["app"], "com.tencent.structmsg")
        self.assertEqual(card["meta"]["news"]["jumpUrl"], PAGE)
        failed = await service.generate_ark(bvid="invalid", title="Example")
        self.assertFalse(failed["success"])
        api.assert_not_awaited()

    async def test_group_raw_transport_preserves_bytes_and_sdk_envelope(self):
        raw = json.dumps(raw_link(), indent=2)
        api = AsyncMock(return_value={"success": True, "data": {"message_id": MID}})
        transported = await VideoCardService(api).send_ark({"kind": "group", "group_id": "10002"}, raw)
        self.assertTrue(transported["success"])
        self.assertEqual(transported["message_id"], MID)
        sent = api.await_args.kwargs
        self.assertEqual(sent["action_name"], "send_group_msg")
        self.assertEqual(sent["params"]["message"][0]["data"]["data"], raw)
        self.assertEqual(api.await_count, 1)

    async def test_group_ok_missing_message_id_is_not_confirmation(self):
        for mid in (None, 0, "0"):
            with self.subTest(mid=mid):
                api = AsyncMock(return_value=okay({"message_id": mid}))
                result = await VideoCardService(api).send_ark(
                    {"kind": "group", "group_id": "10002"}, json.dumps(raw_link()),
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
                self.assertEqual(api.await_count, 1)

    async def test_private_ok_missing_id_does_not_claim_to_read_history(self):
        api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": 0})])
        result = await VideoCardService(api).send_ark({"kind": "private", "user_id": PEER}, json.dumps(raw_link()))
        self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
        self.assertTrue(result["uncertain"])
        self.assertEqual([call.kwargs["action_name"] for call in api.await_args_list],
                         ["get_login_info", "send_private_msg"])

    async def test_private_verifies_same_signed_message_after_index_lag(self):
        api = AsyncMock(side_effect=[
            {"success": True, "result": okay({"user_id": int(BOT)})},
            {"success": True, "data": {"message_id": MID}},
            okay({"messages": []}),
            {"success": True, "result": okay({"messages": [row()]})},
        ])
        result = await VideoCardService(api).send_ark({"kind": "private", "user_id": PEER}, json.dumps(raw_link()))
        self.assertTrue(result["success"])
        self.assertTrue(result["delivery_verified"])
        self.assertEqual(result["message_id"], MID)
        actions = [call.kwargs["action_name"] for call in api.await_args_list]
        self.assertEqual(actions, ["get_login_info", "send_private_msg", "get_friend_msg_history", "get_friend_msg_history"])
        for call in api.await_args_list[2:]:
            self.assertEqual(call.kwargs["params"]["user_id"], int(PEER))
            self.assertNotIn("message_id", call.kwargs["params"])

    async def test_private_success_with_only_synthetic_or_wrong_message_is_uncertain(self):
        stored = [
            row(message_id=2222, real_id=2222),
            row(message_id=MID, user_id=12345),
            row(message_id=MID, target_id=19999),
            row(message_id=MID, sender={"user_id": 12345}),
            row(message_id=MID, message_seq=0),
            row(message_id=MID, time=int(time.time()) - 100),
            row(message_id=MID, message=[{"type": "json", "data": {"data": json.dumps({"different": "card"})}}]),
        ]
        api = AsyncMock(side_effect=[
            okay({"user_id": BOT}), okay({"message_id": MID}), okay({"messages": stored}), okay({"messages": []}),
        ])
        result = await VideoCardService(api).send_ark(
            {"kind": "private", "user_id": PEER, "account_id": BOT}, json.dumps(raw_link()),
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertNotIn("not_sent", result)
        self.assertEqual(result["error_class"], "private_card_not_confirmed")
        self.assertEqual(result["message_id"], MID)
        self.assertEqual([call.kwargs["action_name"] for call in api.await_args_list].count("send_private_msg"), 1)

    async def test_private_readback_failure_is_distinct(self):
        api = AsyncMock(side_effect=[
            okay({"user_id": BOT}), okay({"message_id": MID}),
            {"status": "failed", "retcode": 1200}, RuntimeError("history unavailable"),
        ])
        result = await VideoCardService(api).send_ark(
            {"kind": "private", "user_id": PEER, "account_id": BOT}, json.dumps(raw_link()),
        )
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "private_card_readback_failed")

    async def test_group_delivery_missing_receipt_stays_uncertain_for_adapter_envelopes(self):
        message = ("group message delivery was not confirmed: QQ returned no message receipt; "
                   "check the conversation before retrying")
        for wrapped in (
            {"success": False, "error": "对端动作返回失败: action=send_group_msg message=" + message},
            {"status": "failed", "retcode": 1200, "wording": message},
            {"success": True, "data": {"status": "failed", "retcode": 1200, "message": message}},
            {"success": False, "error": "群消息投递未确认: action=send_group_msg message_id=20003"},
        ):
            with self.subTest(wrapped=wrapped):
                api = AsyncMock(return_value=wrapped)
                result = await VideoCardService(api).send_ark(
                    {"kind": "group", "group_id": "10002"}, json.dumps(raw_link()),
                )
                self.assertFalse(result["success"])
                self.assertTrue(result["uncertain"])
                self.assertNotIn("not_sent", result)
                self.assertNotIn("message_id", result)
                self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
                self.assertIn("QQ 未返回", result["error"])
                self.assertEqual(api.await_count, 1)

    async def test_raised_adapter_confirmation_error_stays_uncertain(self):
        api = AsyncMock(side_effect=RuntimeError(
            "对端动作返回失败: action=send_group_msg message=QQ returned no message receipt"))
        result = await VideoCardService(api).send_ark(
            {"kind": "group", "group_id": "10002"}, json.dumps(raw_link()),
        )
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
        self.assertEqual(api.await_count, 1)

    async def test_explicit_rejection_is_still_distinct(self):
        api = AsyncMock(return_value={"status": "failed", "retcode": 1200, "wording": "permission denied"})
        result = await VideoCardService(api).send_ark(
            {"kind": "group", "group_id": "10002"}, json.dumps(raw_link()),
        )
        self.assertEqual(result, {"success": False, "not_sent": True, "error_class": "send_rejected"})


if __name__ == "__main__":
    unittest.main()
