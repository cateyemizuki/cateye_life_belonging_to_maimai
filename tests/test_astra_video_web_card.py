"""Compare the card builder with the installed adapter's OneBot share mapping."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.video_web_card import build_bili_web_card


VIDEO = "BV0000000001"
URL = f"https://www.bilibili.com/video/{VIDEO}"
COVER = "https://i0.hdslb.com/bfs/archive/fixture.png"


class VideoWebCardTests(unittest.TestCase):
    def test_wire_shape_is_onebot_news_card(self):
        # SnowLuma v2.2.1 message-parser.ts maps the scalar OneBot link-share
        # fields url/title/content/image into this LightApp JSON object.
        card = build_bili_web_card(bvid=VIDEO, title="实际标题", desc="视频介绍", pic_url=COVER)
        self.assertEqual(json.loads(json.dumps(card)), {
            "app": "com.tencent.structmsg",
            "view": "news",
            "prompt": "实际标题",
            "meta": {"news": {
                "title": "实际标题", "desc": "视频介绍", "jumpUrl": URL, "preview": COVER,
            }},
        })

    def test_destination_is_derived_from_bv_only(self):
        for value in (VIDEO + "?t=22", VIDEO + "/extra", "https://b23.tv/example", "BVinvalid"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_bili_web_card(bvid=value, title="title")
        card = build_bili_web_card(bvid=VIDEO, title=" ", pic_url="//i0.hdslb.com/bfs/archive/fixture.png")
        self.assertEqual(card["prompt"], VIDEO)
        self.assertEqual(card["meta"]["news"]["jumpUrl"], URL)
        self.assertEqual(card["meta"]["news"]["preview"], COVER)

    def test_invalid_preview_is_omitted_without_altering_destination(self):
        for pic_url in (
            "file:///Users/private.png",
            "https://127.0.0.1/fixture.png",
            "https://i0.hdslb.com.example.com/fixture.png",
            "https://test:secret@i0.hdslb.com/fixture.png",
        ):
            with self.subTest(pic_url=pic_url):
                card = build_bili_web_card(bvid=VIDEO, title="title", pic_url=pic_url)
                data = card["meta"]["news"]
                self.assertEqual(data["jumpUrl"], URL)
                self.assertEqual(data["preview"], "")


if __name__ == "__main__":
    unittest.main()
