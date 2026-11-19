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
JUMP = "/pages/video/video?bvid=" + BV
SDK_ID = "V1_PC_MINISDK_99.99.99_1_APP_A"
BILI_ICON = "https://miniapp.gtimg.cn/public/appicon/51f90239b78a2e4994c11215f4c4ba15_200.jpg"
SIGN = "LightAppSvc.mini_app_share.AdaptShareInfo"
LOOKUP = "LightAppSvc.mini_app_info.GetAppInfoByLink"
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


def version(name):
    return {"success": True, "result": okay({"app_name": name, "app_version": "2.2.1-node", "protocol_version": "v11"})}


def wire_value(value):
    if value < 128:
        return bytes((value,))
    return bytes(((value & 127) | 128,)) + wire_value(value >> 7)


def protobuf_text(field, text):
    data = text.encode("utf-8") if isinstance(text, str) else text
    return wire_value((field << 3) | 2) + wire_value(len(data)) + data


def provider_card(*, web=PAGE, launch="m.q.qq.com/a/s/qq-issued"):
    original = native(launch=launch)
    original["meta"]["detail_1"].update(qqdocurl=web, provider_extension={"source": 17})
    return {"appName": original["app"], "appView": original["view"], "metaData": original["meta"],
            "config": original["config"], "ver": "1.0.0.103", "desc": "provider display", "prompt": "provider prompt"}


def signed(provider, *, business=b"\x10\x00\x1a\x00"):
    raw = json.dumps(provider, ensure_ascii=False)
    packet = business + protobuf_text(4, protobuf_text(2, raw))
    return {"success": True, "result": okay(packet.hex())}


def resolved(path, *, business=b"\x10\x00\x1a\x00"):
    nested = protobuf_text(2, protobuf_text(11, protobuf_text(1, path)))
    return {"success": True, "result": okay((b"\x08\x1f" + business + protobuf_text(4, nested)).hex())}


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
                api = AsyncMock(side_effect=[version("NapCatQQ"), {"success": True, "result": okay({"data": raw})}])
                result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
                self.assertEqual(result, {"success": True, "card": {"ark_json": raw}})
                self.assertEqual(actions(api), ["get_version_info", "get_mini_app_ark"])
                self.assertEqual(api.await_args.kwargs["params"]["type"], "bili")
                self.assertEqual(api.await_args.kwargs["params"]["jumpUrl"], JUMP)
                self.assertEqual(api.await_args.kwargs["params"]["webUrl"], PAGE)
                self.assertNotIn("qqdocurl", json.loads(raw)["meta"]["detail_1"])

    async def test_provider_dict_is_only_serialized_without_editing_signed_fields(self):
        supplied = native()
        supplied["needShareCallBack"] = True
        supplied["meta"]["detail_1"].update(qqdocurl="https://b23.tv/provider", scene=1036)
        api = AsyncMock(side_effect=[okay({"app_name": "NapCat"}), okay({"data": supplied})])
        result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
        self.assertEqual(json.loads(result["card"]["ark_json"]), supplied)
        self.assertEqual(actions(api), ["get_version_info", "get_mini_app_ark"])

    async def test_invalid_provider_card_does_not_fallback(self):
        bad = [native("http://m.q.qq.com/a/s/untrusted"), native("https://example.com/a/s/untrusted")]
        bad.append({"app": "com.tencent.structmsg", "meta": {"news": {"title": TITLE}}})
        for data in bad:
            api = AsyncMock(side_effect=[version("NapCat"), okay({"data": data})])
            result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
            self.assertFalse(result["success"])
            self.assertEqual(result["error_class"], "card_generation_failed")
            self.assertEqual(actions(api), ["get_version_info", "get_mini_app_ark"])
        api = AsyncMock()
        self.assertFalse((await VideoCardService(api).generate_card(bvid="bad", title=TITLE))["success"])
        api.assert_not_awaited()

    async def test_snowluma_only_returns_cards_with_matching_web_and_qq_app_destination(self):
        provider = provider_card()
        cover = "https://i0.hdslb.com/bfs/archive/fixture.png"
        api = AsyncMock(side_effect=[version("SnowLuma"), signed(provider), resolved(JUMP)])
        result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE, desc="视频介绍", pic_url=cover)
        self.assertTrue(result["success"])
        ark = json.loads(result["card"]["ark_json"])
        self.assertEqual(ark, {
            "app": provider["appName"], "view": provider["appView"], "meta": provider["metaData"],
            "config": provider["config"], "ver": provider["ver"], "prompt": provider["prompt"],
            "miniappShareOrigin": 3, "miniappOpenRefer": "10002",
        })
        self.assertEqual(actions(api), ["get_version_info", "send_packet", "send_packet"])
        sign_args = api.await_args_list[1].kwargs["params"]
        expected_body = b"".join((
            protobuf_text(2, "1109937557"), protobuf_text(3, TITLE), protobuf_text(4, "视频介绍"),
            protobuf_text(9, cover), protobuf_text(11, JUMP), protobuf_text(12, BILI_ICON),
            b"\x68\x03", protobuf_text(17, PAGE),
        ))
        expected_sign = protobuf_text(2, SDK_ID) + protobuf_text(4, expected_body)
        self.assertEqual(sign_args, {"cmd": SIGN, "rsp": True, "data": expected_sign.hex()})
        launch = provider["metaData"]["detail_1"]["url"]
        expected_lookup = b"\x08\x1f" + protobuf_text(2, SDK_ID) + protobuf_text(4, protobuf_text(2, launch) + b"\x18\x00")
        self.assertEqual(api.await_args.kwargs["params"], {"cmd": LOOKUP, "rsp": True, "data": expected_lookup.hex()})

    async def test_snowluma_blocks_unconfirmed_cards_without_sending(self):
        provider = provider_card()
        internal_test = b"\x08\x1f\x10\x00" + protobuf_text(3, "该小程序内测中")
        business_rejection = b"\x10\x09" + protobuf_text(3, "failure")
        for response, reason in (
            (okay(internal_test.hex()), "qq_launch_internal_test"),
            (resolved(JUMP.replace(BV, "BV0000000002")), "qq_launch_mismatch"),
            (resolved(JUMP + "&bvid=" + BV), "qq_launch_mismatch"),
            (resolved("https://m.q.qq.com" + JUMP), "qq_launch_mismatch"),
            (resolved("//" + JUMP), "qq_launch_mismatch"),
            (resolved(JUMP, business=business_rejection), "qq_launch_rejected"),
            (okay("080f10001a00"), "qq_launch_unverified"),
            (okay("080"), "qq_launch_unverified"),
        ):
            with self.subTest(reason=reason):
                api = AsyncMock(side_effect=[version("SnowLuma"), signed(provider), response])
                service = VideoCardService(api)
                result = await service.send_video_card(user_id=PEER, bvid=BV, title=TITLE)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_class"], "card_generation_failed")
                self.assertEqual(result["reason"], reason)
                self.assertNotIn("card", result)
                self.assertEqual(actions(api), ["get_version_info", "send_packet", "send_packet"])

    async def test_snowluma_stops_before_lookup_for_malformed_or_rejected_signature(self):
        provider = provider_card()
        bad_launch = provider_card(launch="https://evil.example/a/s/fake")
        for response, reason in (
            (signed(provider_card(web="")), "qq_web_fallback_mismatch"),
            (signed(provider_card(web=PAGE + "/")), "qq_web_fallback_mismatch"),
            (signed(bad_launch), "qq_card_invalid"),
            (signed(provider, business=b"\x10\x09\x1a\x00"), "qq_sign_rejected"),
            (okay("00" * 262145), "qq_sign_unverified"),
            ({"success": False, "data": signed(provider)}, "qq_sign_unverified"),
        ):
            with self.subTest(reason=reason):
                api = AsyncMock(side_effect=[version("SnowLuma"), response])
                result = await VideoCardService(api).generate_card(bvid=BV, title=TITLE)
                self.assertFalse(result["success"])
                self.assertEqual(result["reason"], reason)
                self.assertEqual(actions(api), ["get_version_info", "send_packet"])

    async def test_generation_timeout_and_unconfirmed_adapter_never_fall_back_to_legacy(self):
        api = AsyncMock(return_value={"success": False, "error": "sensitive adapter detail"})
        service = VideoCardService(api)
        result = await service.generate_card(bvid=BV, title=TITLE)
        self.assertEqual(result["reason"], "adapter_identity_unavailable")
        self.assertNotIn("sensitive", str(result))
        self.assertEqual(actions(api), ["get_version_info"])

        api = AsyncMock(side_effect=[version("SnowLuma"), asyncio.TimeoutError("sensitive adapter detail")])
        service = VideoCardService(api)
        result = await service.generate_card(bvid=BV, title=TITLE)
        self.assertEqual(result["reason"], "card_generation_timeout")
        self.assertNotIn("sensitive", str(result))
        self.assertEqual(actions(api), ["get_version_info", "send_packet"])

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
        self.assertEqual(result["message_id"], MID)
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

    async def test_history_wait_allows_late_group_reply_and_keeps_the_private_budget(self):
        for kind, latency, budget, verified in (
            ("group", 5.188, 8.0, True),
            ("group", 8.1, 8.0, False),
            ("private", 5.188, 3.0, False),
        ):
            with self.subTest(kind=kind, latency=latency):
                history_result = history(event(kind))
                api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": MID}),
                                             history_result, history_result])
                observed_budgets = []

                async def wait_for(awaitable, *, timeout):
                    result = await awaitable
                    if result is history_result:
                        observed_budgets.append(timeout)
                        if latency > timeout:
                            raise asyncio.TimeoutError
                    return result

                with patch("integrations.video_card.asyncio.wait_for", new=wait_for):
                    result = await VideoCardService(api).send_card(route(kind), card())

                history_action = "get_group_msg_history" if kind == "group" else "get_friend_msg_history"
                reads = 1 if verified else 2
                self.assertEqual(observed_budgets, [budget] * reads)
                self.assertEqual(actions(api), ["get_login_info", f"send_{kind}_msg"] + [history_action] * reads)
                self.assertEqual(result["message_id"], MID)
                self.assertEqual(result["success"], verified)
                if verified:
                    self.assertTrue(result["delivery_verified"])
                else:
                    self.assertTrue(result["uncertain"])
                    self.assertEqual(result["error_class"], "card_history_unavailable")

    async def test_empty_group_history_keeps_the_send_receipt_without_confirming(self):
        api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": MID}), history(), history()])
        result = await VideoCardService(api).send_card(route("group"), card())
        self.assertFalse(result["success"])
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "card_not_confirmed")
        self.assertEqual(result["message_id"], MID)
        self.assertEqual(actions(api), ["get_login_info", "send_group_msg",
                                        "get_group_msg_history", "get_group_msg_history"])

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
                self.assertEqual(result["message_id"], MID)
                self.assertEqual(actions(api).count("send_private_msg"), 1)

    async def test_missing_adapter_id_and_opaque_gate_failure_do_not_start_retries(self):
        api = AsyncMock(side_effect=[okay({"user_id": BOT}), okay({"message_id": 0})])
        result = await VideoCardService(api).send_card(route(), card())
        self.assertTrue(result["uncertain"])
        self.assertEqual(result["error_class"], "adapter_delivery_unconfirmed")
        self.assertNotIn("message_id", result)
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
