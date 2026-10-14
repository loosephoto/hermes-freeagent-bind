"""第9段階ソース（Qiita）のオフライン回帰。

HTTP のみ模擬する。**引用文は `rendered_body`（HTML）から作り、`body`（Markdown）を使わない**ことを
固定する（`_plain_text` は `<[^>]+>` しか落とさないので、`body` だと画像記法が引用文に残る — 実測:
`body` は画像 1・リンク 1 が残存、`rendered_body` は 0/0）。あわせて
**未認証の間隔は 60 秒**（公称 60 回/時）であることと、引用の形（identifier / summary_kind=discussion）を
固定する。
"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S

STAGE9 = ("qiita",)

QIITA_ROW = {
    "id": "c2df4f23d73a8e05f37f",
    "title": "GWも勉強したい方向けに、よくオススメされる技術書をカテゴリ別にまとめてみた",
    "url": "https://qiita.com/miruky/items/c2df4f23d73a8e05f37f",
    "created_at": "2026-04-27T10:00:00+09:00",
    "updated_at": "2026-04-28T09:00:00+09:00",
    "body": "![図](https://qiita-image-store.example/x.png) **太字**の本文 [リンク](https://example.com)",
    "rendered_body": '<p><img src="https://qiita-image-store.example/x.png"><b>太字</b>の本文</p>',
    "tags": [{"name": "Python"}, {"name": "AWS"}, {"name": "初心者"}],
    "likes_count": 12, "stocks_count": 345, "comments_count": 2,
    "user": {"id": "miruky"},
}


class TestStage9Registry(unittest.TestCase):
    def test_qiita_is_opt_in_and_advertised(self):
        self.assertIn("qiita", S.SOURCES)
        self.assertIn("qiita", S.KB_BACKENDS)
        self.assertNotIn("qiita", S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            self.assertIn("qiita", desc, tool)

    def test_interval_follows_the_token(self):
        """公称レートで割る（github と同じ型）: 認証 1000/時 → 3.6 秒 / 未認証 60/時 → 60.0 秒。"""
        self.assertEqual(S.QIITA_INTERVAL, 3.6 if S.QIITA_TOKEN else 60.0)
        self.assertEqual(S.QIITA_BUDGET, "qiita.com/api/v2/items")

    def test_never_touches_the_network_without_a_query(self):
        with mock.patch.object(S, "kb_json") as http:
            self.assertTrue(S.KB_BACKENDS["qiita"](123, 3, {}).get("error"))
            self.assertTrue(S.KB_BACKENDS["qiita"]("", 3, {}).get("error"))
            http.assert_not_called()


class TestQiita(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_search_uses_the_query_language_and_the_service_budget(self):
        with mock.patch.object(S, "kb_json", return_value=([QIITA_ROW], "")) as http:
            S.kb_qiita("tag:Python stocks:>100", limit=3)
        url = http.call_args[0][0]
        params = parse_qs(urlsplit(url).query)
        self.assertIn("/api/v2/items", url)
        self.assertEqual(params["query"], ["tag:Python stocks:>100"])
        self.assertEqual(params["per_page"], ["3"])

    def test_summary_comes_from_rendered_body(self):
        """Markdown の body を使うと画像記法・リンク記法が引用文に残る（実測）。"""
        with mock.patch.object(S, "kb_json", return_value=([QIITA_ROW], "")):
            cite = S.kb_qiita("tag:Python")["citations"][0]
        self.assertNotIn("![", cite["summary"])
        self.assertNotIn("](", cite["summary"])
        self.assertNotIn("<", cite["summary"])
        self.assertIn("太字", cite["summary"])

    def test_row_becomes_a_discussion_citation(self):
        with mock.patch.object(S, "kb_json", return_value=([QIITA_ROW], "")):
            got = S.kb_qiita("tag:Python")
        cite = got["citations"][0]
        self.assertEqual(cite["source"], "qiita")
        self.assertEqual(cite["title"], QIITA_ROW["title"])
        self.assertEqual(cite["url"], QIITA_ROW["url"])
        self.assertEqual(cite["identifier"], QIITA_ROW["id"])
        self.assertEqual(cite["summary_kind"], "discussion")
        self.assertEqual(cite["container"], "Qiita")
        self.assertEqual(cite["authors"], ["miruky"])
        self.assertEqual(cite["year"], "2026")
        self.assertEqual(cite["extra"]["tags"], ["Python", "AWS", "初心者"])
        self.assertEqual(cite["extra"]["stocks_count"], 345)
        self.assertEqual(cite["extra"]["likes_count"], 12)
        self.assertEqual(cite["extra"]["comments_count"], 2)
        self.assertEqual(cite["extra"]["updated_at"], "2026-04-28T09:00:00+09:00")
        self.assertFalse(cite["metadata_only"])
        self.assertTrue(S._kb_has_evidence(cite))
        S._kb_http_url(cite["url"])

    def test_token_is_sent_only_when_configured(self):
        with mock.patch.object(S, "QIITA_TOKEN", "dummy-token"):
            with mock.patch.object(S, "kb_json", return_value=([QIITA_ROW], "")) as http:
                S._KB_CACHE.clear()
                S.kb_qiita("tag:Python")
            self.assertEqual(http.call_args.kwargs.get("extra_headers"),
                             {"Authorization": "Bearer dummy-token"})
        with mock.patch.object(S, "QIITA_TOKEN", ""):
            with mock.patch.object(S, "kb_json", return_value=([QIITA_ROW], "")) as http:
                S._KB_CACHE.clear()
                S.kb_qiita("tag:Python")
            self.assertIsNone(http.call_args.kwargs.get("extra_headers"))

    def test_body_without_a_rendered_body_is_metadata_only(self):
        row = {**QIITA_ROW, "rendered_body": ""}
        with mock.patch.object(S, "kb_json", return_value=([row], "")):
            cite = S.kb_qiita("tag:Python")["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertFalse(S._kb_has_evidence(cite))

    def test_rows_without_title_or_url_are_skipped(self):
        rows = [{**QIITA_ROW, "title": ""}, {**QIITA_ROW, "url": ""}, QIITA_ROW]
        with mock.patch.object(S, "kb_json", return_value=(rows, "")):
            got = S.kb_qiita("tag:Python", limit=5)
        self.assertEqual(len(got["citations"]), 1)

    def test_non_dict_tag_and_user_do_not_leak(self):
        row = {**QIITA_ROW, "tags": ["Python", None, {"name": ""}], "user": "miruky"}
        with mock.patch.object(S, "kb_json", return_value=([row], "")):
            cite = S.kb_qiita("tag:Python")["citations"][0]
        # 空の値は extra から落ちる（_kb_generic_result の仕様）
        self.assertNotIn("tags", cite["extra"])
        self.assertNotIn("authors", cite)      # 空の authors も落ちる（同仕様）
        self.assertFalse(cite["metadata_only"])

    def test_upstream_error_is_reported_not_swallowed(self):
        for err in ("HTTP 429: Qiita のレート制限", "HTTP 403: blocked"):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(None, err)):
                got = S.kb_qiita("tag:Python")
            self.assertEqual(got["error"], err)
            self.assertEqual(got.get("citations") or [], [])

    def test_malformed_response_does_not_leak(self):
        for data in (None, {}, "x", {"items": []}, [None]):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                self.assertTrue(S.kb_qiita("tag:Python").get("error"), data)

    def test_empty_result_is_reported_as_no_hits(self):
        with mock.patch.object(S, "kb_json", return_value=([], "")):
            got = S.kb_qiita("tag:nothing-matches")
        self.assertEqual(got["error"], "該当なし")
        self.assertEqual(got["citations"], [])

    def test_local_interval_error_is_surfaced(self):
        """未認証は 60 秒間隔。別の問いはローカルの 429 として見える（隠さない・規約 8）。"""
        with mock.patch.object(S, "_kb_rate_acquire", return_value=59.6):
            got = S.kb_qiita("tag:Go")
        self.assertIn("ローカルのアクセス間隔制御", got["error"])


if __name__ == "__main__":
    unittest.main()
