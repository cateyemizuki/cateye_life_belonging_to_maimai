"""Regression coverage for Bilibili copy links and mobile web cards."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

sys.path.insert(0, os.environ.get("CATEYE_TEST_DIR") or str(Path(__file__).resolve().parents[1]))
from integrations.video_web_card import bili_copy_short_link, build_bili_web_card, format_bili_copy_share


VIDEO = "BV0000000001"
PAGE = f"https://www.bilibili.com/video/{VIDEO}"
MOBILE = f"https://m.bilibili.com/video/{VIDEO}"
COVER = "https://i0.hdslb.com/bfs/archive/fixture.png"
SHORT = "https://b23.tv/0XUwoXs"


class BiliShareRequestTests(unittest.IsolatedAsyncioTestCase):
    def test_mobile_card_with_public_cover(self):
        card = build_bili_web_card(bvid=VIDEO, title="实际标题", pic_url=COVER.replace("https:", "http:"))
        self.assertEqual(card, {
            "title": "实际标题", "desc": "打开 B 站视频网页", "jump_url": MOBILE, "preview_url": COVER,
        })

    def test_card_never_reuses_an_untrusted_cover_or_a_caller_supplied_jump(self):
        for cover in ("file:///private.png", "https://127.0.0.1/cover.png", "https://hdslb.com.example.com/img.png"):
            with self.subTest(cover=cover):
                card = build_bili_web_card(bvid=VIDEO, title="", pic_url=cover)
                self.assertEqual(card["title"], VIDEO)
                self.assertEqual(card["jump_url"], MOBILE)
                self.assertNotIn("preview_url", card)
        for video in (VIDEO + "/extra", "https://b23.tv/example", "BVinvalid"):
            with self.subTest(video=video), self.assertRaises(ValueError):
                build_bili_web_card(bvid=video, title="title")

    def client(self, *, destination=PAGE, content=f"【实际标题-哔哩哔哩】 {SHORT}"):
        client = httpx.AsyncClient
        def handle(request):
            path = request.url.path
            if path == "/x/web-interface/view":
                self.assertEqual(request.url.params["bvid"], VIDEO)
                return httpx.Response(200, json={"code": 0, "data": {"bvid": VIDEO, "aid": 12345}})
            if path == "/x/frontend/finger/spi":
                return httpx.Response(200, json={"code": 0, "data": {"b_3": "public-visitor-id"}})
            if path == "/x/share/click":
                self.assertEqual(request.method, "POST")
                fields = parse_qs(request.content.decode())
                self.assertEqual(fields["share_channel"], ["COPY"])
                self.assertEqual(fields["oid"], ["12345"])
                return httpx.Response(200, json={"code": 0, "data": {"content": content}})
            self.assertEqual(str(request.url), SHORT)
            return httpx.Response(302, headers={"Location": destination})
        return patch("integrations.video_web_card.httpx.AsyncClient",
                     side_effect=lambda **kw: client(transport=httpx.MockTransport(handle), **kw))

    async def test_copy_short_link_is_minted_by_bilibili_and_checked(self):
        with self.client():
            signed = await bili_copy_short_link(VIDEO)
        self.assertEqual(signed, SHORT)
        self.assertEqual(format_bili_copy_share("实际标题", signed), f"【实际标题-哔哩哔哩】 {SHORT}")

    async def test_rejects_redirects_to_another_video_host_or_episode(self):
        for destination in (PAGE.replace(VIDEO, "BV0000000002"), PAGE + "?p=2",
                            PAGE.replace("www.bilibili.com", "example.com")):
            with self.subTest(destination=destination), self.client(destination=destination):
                with self.assertRaises(ValueError):
                    await bili_copy_short_link(VIDEO)
        with self.client(content="【标题-哔哩哔哩】 https://evil.example/short"):
            with self.assertRaises(ValueError):
                await bili_copy_short_link(VIDEO)


if __name__ == "__main__":
    unittest.main()
