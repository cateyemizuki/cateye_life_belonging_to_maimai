"""Regression for the distinction between generated and transported Bili miniapp cards."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.video_card import PASSTHROUGH_API, VideoCardService


BV = "BV0000000001"
PAGE = "https://www.bilibili.com/video/" + BV


def provider_card():
    return {
        "ver": "1.0.0.19",
        "app": "com.tencent.miniapp_01",
        "view": "view_8C8E89B49BE609866298ADDFF2DBABA4",
        "config": {"token": "provider-token"},
        "meta": {"detail_1": {
            "appid": "1109937557",
            "appType": 0,
            "url": "https://m.q.qq.com/a/s/provider-link",
            "scene": 0,
            "host": {"uin": 10001, "nick": "provider"},
            "preview": "https://qq.ugcimg.cn/provider-preview",
            "shareTemplateId": "8C8E89B49BE609866298ADDFF2DBABA4",
            "shareTemplateData": {},
        }},
    }


class VideoCardConstructionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_card_fills_missing_fields_without_touching_provider(self):
        upstream = provider_card()
        original = deepcopy(upstream)
        api = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"data": upstream}})
        result = await VideoCardService(api).generate_ark(bvid=BV, title="Example")
        self.assertTrue(result["success"])
        self.assertEqual(api.await_args.args, (PASSTHROUGH_API,))
        self.assertEqual(api.await_args.kwargs["action_name"], "get_mini_app_ark")
        self.assertEqual(api.await_args.kwargs["params"]["jumpUrl"], PAGE)
        card = json.loads(result["ark_json"])
        self.assertIs(card.pop("needShareCallBack"), False)
        self.assertEqual(card["meta"]["detail_1"].pop("qqdocurl"), PAGE)
        self.assertEqual(card, original)
        self.assertEqual(upstream, original)

    async def test_existing_provider_document_callback_and_scene_are_preserved(self):
        upstream = provider_card()
        upstream["meta"]["detail_1"].update(qqdocurl="https://b23.tv/provider-link", scene=1036)
        upstream["needShareCallBack"] = True
        api = AsyncMock(return_value={"status": "ok", "data": upstream})
        result = await VideoCardService(api).generate_ark(bvid=BV, title="Example")
        self.assertEqual(json.loads(result["ark_json"]), upstream)

    async def test_non_bili_card_does_not_get_bili_metadata(self):
        upstream = provider_card()
        upstream["meta"]["detail_1"]["appid"] = "different-miniapp"
        api = AsyncMock(return_value={"status": "ok", "data": upstream})
        result = await VideoCardService(api).generate_ark(bvid=BV, title="Example")
        self.assertEqual(json.loads(result["ark_json"]), upstream)

    async def test_manual_share_constructs_once_and_raw_transport_keeps_bytes(self):
        upstream = provider_card()
        api = AsyncMock(side_effect=[
            {"status": "ok", "data": upstream},
            {"status": "ok", "data": {"message_id": 20001}},
            {"status": "ok", "data": {"message_id": 20002}},
        ])
        service = VideoCardService(api)
        result = await service.send_video_card(user_id="10001", bvid=BV, title="Example")
        self.assertTrue(result["success"])
        sent = api.await_args_list[1].kwargs
        card = json.loads(sent["params"]["message"][0]["data"]["data"])
        self.assertEqual(sent["action_name"], "send_private_msg")
        self.assertEqual(card["meta"]["detail_1"]["qqdocurl"], PAGE)
        self.assertIs(card["needShareCallBack"], False)

        raw = json.dumps(upstream, indent=2)
        transported = await service.send_ark({"kind": "group", "group_id": "10002"}, raw)
        self.assertTrue(transported["success"])
        sent = api.await_args_list[2].kwargs
        self.assertEqual(sent["action_name"], "send_group_msg")
        self.assertEqual(sent["params"]["message"][0]["data"]["data"], raw)


if __name__ == "__main__":
    unittest.main()
