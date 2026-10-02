"""第4段階ソース（DOAJ / npm / crates.io）のオフライン回帰。HTTPのみ模擬。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


DOAJ_ROW = {"bibjson": {
    "title": "Attention survey", "year": "2024", "abstract": "A survey of attention mechanisms.",
    "identifier": [{"type": "DOI", "id": "10.1234/attn"}],
    "link": [{"url": "https://example.org/fulltext"}],
    "journal": {"title": "Journal of Science"},
    "author": [{"name": "Alice"}, {"name": "Bob"}]}}

NPM_ROW = {"package": {
    "name": "ajv", "version": "8.17.1", "date": "2024-05-01T00:00:00Z",
    "description": "Another JSON Schema Validator", "keywords": ["json", "schema"],
    "links": {"npm": "https://www.npmjs.com/package/ajv",
              "repository": "https://github.com/ajv-validator/ajv"}}}

CRATES_ROW = {"name": "serde", "max_stable_version": "1.0.219", "updated_at": "2024-03-01T00:00:00Z",
              "description": "A serialization framework", "repository": "https://github.com/serde-rs/serde",
              "downloads": 500000}


class TestStage4Sources(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_doaj_prefers_doi_url_and_abstract_is_evidence(self):
        with mock.patch.object(S, "kb_json", return_value=({"results": [DOAJ_ROW]}, "")):
            got = S.kb_doaj("attention")
        cite = got["citations"][0]
        self.assertEqual(cite["url"], "https://doi.org/10.1234/attn")
        self.assertEqual(cite["summary"], "A survey of attention mechanisms.")
        self.assertTrue(S._kb_has_evidence(cite))
        self.assertIn("CC0", got["attribution"])

    def test_doaj_without_abstract_is_metadata_only(self):
        row = {"bibjson": {**DOAJ_ROW["bibjson"], "abstract": ""}}
        with mock.patch.object(S, "kb_json", return_value=({"results": [row]}, "")):
            cite = S.kb_doaj("attention")["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertFalse(S._kb_has_evidence(cite))
        self.assertEqual(S._evidence_block([cite]), "")

    def test_package_descriptions_are_registry_claims_not_reviews(self):
        with mock.patch.object(S, "kb_json", return_value=({"objects": [NPM_ROW]}, "")):
            npm_cite = S.kb_npm("json schema")["citations"][0]
        S._KB_CACHE.clear()
        with mock.patch.object(S, "kb_json", return_value=({"crates": [CRATES_ROW]}, "")):
            crates_cite = S.kb_crates("serialization")["citations"][0]
        for cite in (npm_cite, crates_cite):
            self.assertEqual(cite["summary_kind"], "registry_description")
            self.assertTrue(cite["summary"])
        self.assertEqual(npm_cite["url"], "https://www.npmjs.com/package/ajv")
        self.assertEqual(crates_cite["url"], "https://crates.io/crates/serde")
        self.assertEqual(crates_cite["downloads"], 500000)

    def test_crates_non_int_downloads_is_dropped_not_crash(self):
        row = {**CRATES_ROW, "downloads": "many"}
        with mock.patch.object(S, "kb_json", return_value=({"crates": [row]}, "")):
            cite = S.kb_crates("serialization")["citations"][0]
        self.assertNotIn("downloads", cite)

    def test_bad_arguments_and_malformed_responses_do_not_leak(self):
        for fn in (S.kb_doaj, S.kb_npm, S.kb_crates):
            with mock.patch.object(S, "kb_json") as http:
                self.assertTrue(fn(123).get("error"))
                self.assertTrue(fn("").get("error"))
                http.assert_not_called()
            for data in (None, [], {"results": "x", "objects": "x", "crates": "x"},
                         {"results": ["x"], "objects": ["x"], "crates": ["x"]}):
                S._KB_CACHE.clear()
                with mock.patch.object(S, "kb_json", return_value=(data, "")):
                    got = fn("q")
                self.assertTrue(got.get("error"), (fn.__name__, data, got))
                self.assertFalse(got.get("citations"))

    def test_sources_are_opt_in_and_schema_exposes_them(self):
        for name in ("doaj", "npm", "crates"):
            self.assertIn(name, S.SOURCES)
            self.assertIn(name, S.KB_BACKENDS)
            self.assertNotIn(name, S.DEFAULT_SOURCES)
        lookup = next(t for t in S.TOOLS if t["name"] == "freeagent_lookup")
        desc = str(lookup["inputSchema"]["properties"]["sources"]["description"])
        for name in ("doaj", "npm", "crates"):
            self.assertIn(name, desc)

    def test_rate_budget_failure_is_reported_not_waited(self):
        with mock.patch.object(S, "_kb_rate_acquire", return_value=1.5), \
             mock.patch.object(S, "kb_json") as http:
            got = S.kb_doaj("attention")
            http.assert_not_called()
        self.assertIn("429", got["error"])

    def test_failures_are_not_cached(self):
        with mock.patch.object(S, "kb_json", return_value=(None, "HTTP 500: down")):
            self.assertTrue(S.kb_npm("q").get("error"))
        with mock.patch.object(S, "kb_json", return_value=({"objects": [NPM_ROW]}, "")):
            got = S.kb_npm("q")
        self.assertFalse(got.get("error"))
        self.assertEqual(got["citations"][0]["title"], "ajv")


if __name__ == "__main__":
    unittest.main()
