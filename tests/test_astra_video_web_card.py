"""Regression tests for the native URL-sharing action's content fields."""

from __future__ import annotations

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
    def test_documented_tuwen_action_fields(self):
        card = build_bili_web_card(bvid=VIDEO, title="实际标题", desc="视频介绍", pic_url=COVER)
        self.assertEqual(card, {
            "title": "实际标题", "desc": "视频介绍", "jump_url": URL, "preview_url": COVER,
        })

    def test_destination_is_derived_from_bv_only(self):
        for value in (VIDEO + "?t=22", VIDEO + "/extra", "https://b23.tv/example", "BVinvalid"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    build_bili_web_card(bvid=value, title="title")
        card = build_bili_web_card(bvid=VIDEO, title=" ", pic_url="//i0.hdslb.com/bfs/archive/fixture.png")
        self.assertEqual(card["title"], VIDEO)
        self.assertEqual(card["jump_url"], URL)
        self.assertEqual(card["preview_url"], COVER)

    def test_invalid_preview_is_omitted_without_altering_destination(self):
        for pic_url in (
            "file:///Users/private.png",
            "https://127.0.0.1/fixture.png",
            "https://i0.hdslb.com.example.com/fixture.png",
            "https://test:secret@i0.hdslb.com/fixture.png",
            "",
        ):
            with self.subTest(pic_url=pic_url):
                card = build_bili_web_card(bvid=VIDEO, title="title", pic_url=pic_url)
                self.assertEqual(card["jump_url"], URL)
                self.assertNotIn("preview_url", card)


if __name__ == "__main__":
    unittest.main()
