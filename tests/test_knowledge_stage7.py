"""第7段階ソース（プログラミング特化: HN / Software Heritage / Libraries.io）のオフライン回帰。

HTTP のみ模擬する。librariesio は利用者自身のキーが前提なので、未設定時は HTTP を出さないことを固定する。
"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S

STAGE7 = ("hn", "swh", "librariesio")

HN_HIT = {"objectID": "12345", "title": "Show HN: a JSON schema validator in Rust",
          "url": "https://example.org/validator", "story_text": None, "points": 210,
          "num_comments": 42, "author": "alice", "created_at": "2024-03-01T10:00:00Z"}

HN_ASK = {"objectID": "999", "title": "Ask HN: how do you structure a Rust workspace?",
          "url": None, "story_text": "I have a monorepo and I want to split it. <p>Ideas?</p>",
          "points": 5, "num_comments": 3, "author": "bob", "created_at": "2024-04-02T10:00:00Z"}

SWH_ORIGIN = {"url": "https://github.com/rust-lang/cargo", "visit_types": ["git"],
              "visits_url": "https://archive.softwareheritage.org/api/1/origin/x/visits/"}

LIO_PROJECT = {"name": "json-schema", "platform": "npm", "description": "JSON Schema validator",
               "latest_release_number": "1.2.3", "latest_release_published_at": "2024-05-06T00:00:00Z",
               "repository_url": "https://github.com/example/json-schema", "downloads": 1234567,
               "keywords": ["json", "schema"], "licenses": ["MIT"]}


def fetch_by_url(mapping, default=None):
    def fake(url, **kwargs):
        for key, value in mapping.items():
            if key in url:
                return value, ""
        if default is not None:
            return default, ""
        raise AssertionError(f"unexpected url: {url}")
    return fake


class TestStage7Registry(unittest.TestCase):
    def test_stage7_sources_are_opt_in_and_advertised(self):
        for name in STAGE7:
            self.assertIn(name, S.SOURCES)
            self.assertIn(name, S.KB_BACKENDS)
            self.assertNotIn(name, S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            for name in STAGE7:
                self.assertIn(name, desc, (tool, name))

    def test_no_stage7_source_touches_the_network_without_arguments(self):
        for name in STAGE7:
            with mock.patch.object(S, "kb_json") as http:
                self.assertTrue(S.KB_BACKENDS[name](123, 3, {}).get("error"), name)
                self.assertTrue(S.KB_BACKENDS[name]("", 3, {}).get("error"), name)
                http.assert_not_called()


class TestHackerNews(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_story_uses_the_hn_item_url_and_keeps_the_article_in_extra(self):
        with mock.patch.object(S, "kb_json", return_value=({"hits": [HN_HIT]}, "")) as http:
            cite = S.kb_hn("json schema")["citations"][0]
        self.assertIn("/api/v1/search", http.call_args[0][0])
        self.assertIn("tags=story", http.call_args[0][0])
        self.assertEqual(cite["url"], "https://news.ycombinator.com/item?id=12345")
        self.assertEqual(cite["extra"]["article_url"], "https://example.org/validator")
        self.assertEqual(cite["extra"]["points"], 210)
        self.assertEqual(cite["summary_kind"], "discussion")
        self.assertTrue(cite["metadata_only"])   # リンク投稿は本文が無い

    def test_ask_hn_story_text_is_the_body(self):
        with mock.patch.object(S, "kb_json", return_value=({"hits": [HN_ASK]}, "")):
            cite = S.kb_hn("rust workspace")["citations"][0]
        self.assertIn("monorepo", cite["summary"])
        self.assertNotIn("<p>", cite["summary"])
        self.assertFalse(cite["metadata_only"])
        self.assertTrue(S._kb_has_evidence(cite))

    def test_malformed_response_does_not_leak(self):
        for data in (None, [], {"hits": "x"}, {"hits": ["x"]}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                self.assertTrue(S.kb_hn("q").get("error"), data)


class TestSoftwareHeritage(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_origin_search_is_a_structured_record(self):
        with mock.patch.object(S, "kb_json", return_value=([SWH_ORIGIN], "")) as http:
            got = S.kb_swh("rust-lang/cargo")
        self.assertIn("/api/1/origin/search/", http.call_args[0][0])
        cite = got["citations"][0]
        self.assertEqual(cite["url"], "https://github.com/rust-lang/cargo")
        self.assertEqual(cite["summary_kind"], "structured_record")
        self.assertEqual(cite["extra"]["visit_types"], ["git"])
        self.assertIn("browse/origin", cite["extra"]["browse_url"])
        self.assertTrue(cite["summary"])

    def test_query_is_url_escaped(self):
        with mock.patch.object(S, "kb_json", return_value=([], "")) as http:
            S.kb_swh("a b/c")
        self.assertIn("/api/1/origin/search/a%20b%2Fc/", http.call_args[0][0])

    def test_malformed_response_does_not_leak(self):
        for data in (None, {"not": "a list"}, ["x"]):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                self.assertTrue(S.kb_swh("q").get("error"), data)


class TestLibrariesIo(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_missing_key_is_an_error_without_any_http(self):
        with mock.patch.object(S, "LIBRARIESIO_KEY", ""), \
             mock.patch.object(S, "kb_json") as http:
            got = S.kb_librariesio("json schema")
        self.assertIn("FREEAGENT_LIBRARIESIO_KEY", got["error"])
        http.assert_not_called()

    def test_key_is_sent_and_project_is_normalised(self):
        with mock.patch.object(S, "LIBRARIESIO_KEY", "secret-key"), \
             mock.patch.object(S, "kb_json", return_value=([LIO_PROJECT], "")) as http:
            got = S.kb_librariesio("json schema")
        params = parse_qs(urlsplit(http.call_args[0][0]).query)
        self.assertEqual(params["q"], ["json schema"])
        self.assertEqual(params["api_key"], ["secret-key"])
        cite = got["citations"][0]
        self.assertEqual(cite["title"], "npm/json-schema")
        self.assertEqual(cite["url"], "https://libraries.io/npm/json-schema")
        self.assertEqual(cite["summary_kind"], "registry_description")
        self.assertEqual(cite["summary"], "JSON Schema validator")
        self.assertEqual(cite["version"], "1.2.3")
        self.assertEqual(cite["downloads"], 1234567)
        self.assertEqual(cite["repository_url"], "https://github.com/example/json-schema")
        self.assertEqual(cite["keywords"], ["json", "schema"])

    def test_empty_description_is_metadata_only(self):
        with mock.patch.object(S, "LIBRARIESIO_KEY", "k"), \
             mock.patch.object(S, "kb_json", return_value=([{**LIO_PROJECT, "description": ""}], "")):
            cite = S.kb_librariesio("q")["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertFalse(S._kb_has_evidence(cite))

    def test_malformed_response_does_not_leak(self):
        for data in (None, {"projects": "x"}, ["x"]):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "LIBRARIESIO_KEY", "k"), \
                 mock.patch.object(S, "kb_json", return_value=(data, "")):
                self.assertTrue(S.kb_librariesio("q").get("error"), data)


if __name__ == "__main__":
    unittest.main()
