"""Share through OneBot once, with independent and correctly scoped QQ receipts."""

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
from integrations.video_web_card import COPY_LINK, WEB_CARD


BV = "BV0000000001"
PAGE = "https://www.bilibili.com/video/" + BV
MOBILE = "https://m.bilibili.com/video/" + BV
SHORT = "https://b23.tv/example"
PEER = "10001"
GROUP = "20002"
BOT = "40001"
MID = -171476788
BASELINE = 175976788
TITLE = "Bili 视频"
COPY_TEXT = f"【{TITLE}-哔哩哔哩】 {SHORT}"


def okay(data=None):
    return {"status": "ok", "retcode": 0, "data": data}


def text(value):
    return [{"type": "text", "data": {"text": value}}]


def ark(url=MOBILE, title=TITLE):
    body = {"app": "com.tencent.tuwen.lua", "meta": {"news": {"title": title, "jumpUrl": url}}}
    return [{"type": "json", "data": {"data": json.dumps(body)}}]


def descriptor(form=COPY_LINK):
    card = {"format": form, "bvid": BV, "title": TITLE}
    return {**card, "url": SHORT} if form == COPY_LINK else card


def event(kind="private", message=None, **overrides):
    row = {"message_type": kind, "message_id": MID, "message_seq": 20,
           "time": int(time.time()), "user_id": int(BOT), "sender": {"user_id": int(BOT)},
           "message": text(COPY_TEXT) if message is None else message}
    if kind == "group":
        row.update(group_id=int(GROUP), anonymous=None)
        row["sender"].update(role="member")
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


class VideoShareDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        delays = patch("integrations.video_card._HISTORY_DELAYS", (0, 0))
        delays.start()
        self.addCleanup(delays.stop)

    async def test_copy_default_and_safe_long_link_when_short_is_unavailable(self):
        api = AsyncMock()
        service = VideoCardService(api)
        with patch("integrations.video_card.bili_copy_short_link", AsyncMock(return_value=SHORT)):
            self.assertEqual((await service.generate_card(bvid=BV, title=TITLE))["card"], descriptor())
        with patch("integrations.video_card.bili_copy_short_link", AsyncMock(side_effect=ValueError)):
            self.assertEqual((await service.generate_card(bvid=BV, title=TITLE))["card"]["url"], PAGE)
        api.assert_not_awaited()

    async def test_configured_card_prepares_without_signing_or_copy_service(self):
        api = AsyncMock()
        service = VideoCardService(api, format_provider=lambda: WEB_CARD)
        with patch("integrations.video_card.bili_copy_short_link", AsyncMock()) as short:
            card = (await service.generate_card(bvid=BV, title=TITLE))["card"]
        self.assertEqual(card["format"], WEB_CARD)
        self.assertEqual(card["jump_url"], MOBILE)
        short.assert_not_awaited()
        api.assert_not_awaited()

    async def test_copy_uses_text_and_exact_receipt_in_both_scopes(self):
        for kind, field, peer in (("private", "user_id", PEER), ("group", "group_id", GROUP)):
            with self.subTest(kind=kind):
                api = AsyncMock(side_effect=[okay({"user_id": int(BOT)}), okay({"message_id": MID}),
                                             history(event(kind))])
                result = await VideoCardService(api).send_card(route(kind), descriptor())
                self.assertTrue(result["delivery_verified"])
                self.assertEqual(result["channel"], "video_link")
                self.assertEqual(actions(api), ["get_login_info", f"send_{kind}_msg",
                    "get_friend_msg_history" if kind == "private" else "get_group_msg_history"])
                self.assertEqual(api.await_args_list[1].kwargs["params"], {field: int(peer), "message": text(COPY_TEXT)})

    async def test_tuwen_card_uses_anchored_history_and_proves_mobile_target(self):
        for kind in ("private", "group"):
            with self.subTest(kind=kind):
                baseline = event(kind, text("before"), message_id=BASELINE, message_seq=19)
                api = AsyncMock(side_effect=[okay({"user_id": int(BOT)}), history(baseline), okay(),
                                             history(baseline, event(kind, ark(title="Bili…")))])
                result = await VideoCardService(api).send_card(route(kind), descriptor(WEB_CARD))
                self.assertTrue(result["delivery_verified"])
                self.assertEqual(result["message_id"], MID)
                self.assertEqual(actions(api)[2], "send_tuwen_ark")
                self.assertEqual(api.await_args_list[2].kwargs["params"]["jump_url"], MOBILE)
                self.assertEqual(api.await_args.kwargs["params"]["message_id"], BASELINE)
                self.assertFalse(api.await_args.kwargs["params"]["reverse_order"])

    async def test_acknowledgment_without_matching_history_does_not_send_twice(self):
        for returned in (history(event(message=ark(PAGE))),
                         history(event(message=ark(), target_id=12345)), history()):
            with self.subTest(history=returned):
                api = AsyncMock(side_effect=[okay({"user_id": int(BOT)}), history(), okay(), returned, returned])
                result = await VideoCardService(api).send_card(route(), descriptor(WEB_CARD))
                self.assertTrue(result["uncertain"])
                self.assertEqual(actions(api).count("send_tuwen_ark"), 1)
                self.assertNotIn("send_private_msg", actions(api))

    async def test_late_gate_and_wrong_account_do_not_send(self):
        for account, gate in (("12345", True), (BOT, False)):
            with self.subTest(account=account):
                api = AsyncMock(return_value=okay({"user_id": account}))
                result = await VideoCardService(api).send_card(route(), descriptor(), before_send=lambda: (gate, ""))
                self.assertTrue(result["not_sent"])
                self.assertEqual(actions(api), ["get_login_info"])

    async def test_removed_miniapp_and_unrelated_long_link_are_rejected(self):
        api = AsyncMock()
        for card in ({"ark_json": "{}"}, {**descriptor(), "url": PAGE.replace(BV, "BV0000000002")}):
            result = await VideoCardService(api).send_card(route(), card)
            self.assertTrue(result["not_sent"])
        api.assert_not_awaited()

    async def test_concurrent_sends_do_not_duplicate_share_in_a_scope(self):
        reached = asyncio.Event()
        released = asyncio.Event()
        async def adapter(_name, *, action_name, params):
            if action_name == "get_login_info":
                return okay({"user_id": int(BOT)})
            if action_name == "send_private_msg":
                reached.set()
                await released.wait()
                return okay({"message_id": MID})
            return history(event())

        service = VideoCardService(adapter)
        first = asyncio.create_task(service.send_card(route(), descriptor()))
        await reached.wait()
        try:
            second = await service.send_card(route(), descriptor())
            self.assertEqual(second["error_class"], "card_send_in_progress")
        finally:
            released.set()
            await first


if __name__ == "__main__":
    unittest.main()
