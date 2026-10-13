"""第8段階ソース（Internet Archive）のオフライン回帰。

HTTP のみ模擬する。**CJK クエリは HTTP を出さずに断る**こと（実測 10/10 が QUERY_NOT_READY）と、
**上流が HTTP 200 で {"error": ...} を返す**ことを成功として扱わないことを固定する。
"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S

STAGE8 = ("ia",)

IA_DOC = {"identifier": "bitsavers_ibm360oscompilerandLibraryVersion2ProgrammersGuide_26997266",
          "title": "ibm :: 360 :: os :: cobol :: GC28-6399-2 COBOL Compiler and Library Version 2",
          "mediatype": "texts", "year": "1972", "downloads": 5297, "creator": ["bitsavers"],
          "description": ["From the bitsavers.org collection, a scanned-in computer-related document."]}


def response(docs):
    return {"response": {"numFound": len(docs), "start": 0, "docs": docs}}


class TestStage8Registry(unittest.TestCase):
    def test_ia_is_opt_in_and_advertised(self):
        self.assertIn("ia", S.SOURCES)
        self.assertIn("ia", S.KB_BACKENDS)
        self.assertNotIn("ia", S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            self.assertIn("ia", desc, tool)

    def test_ia_never_touches_the_network_without_a_query(self):
        with mock.patch.object(S, "kb_json") as http:
            self.assertTrue(S.KB_BACKENDS["ia"](123, 3, {}).get("error"))
            self.assertTrue(S.KB_BACKENDS["ia"]("", 3, {}).get("error"))
            http.assert_not_called()


class TestInternetArchive(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_query_is_scoped_to_the_title_field(self):
        """素の q= は順位が壊れる（実測）。title: へ組み直すことを固定する。"""
        self.assertEqual(S._ia_query("COBOL"), "title:(COBOL)")
        self.assertEqual(S._ia_query("  FORTRAN   manual "), "title:(FORTRAN AND manual)")

    def test_cjk_query_is_refused_without_http(self):
        for query in ("日本語のマニュアル", "量子力学", "title:日本語"):
            with mock.patch.object(S, "kb_json") as http:
                got = S.kb_internetarchive(query)
            self.assertIn("英語", got["error"], query)
            self.assertEqual(got["citations"], [])
            http.assert_not_called()

    def test_item_becomes_a_structured_record_citation(self):
        with mock.patch.object(S, "kb_json", return_value=(response([IA_DOC]), "")) as http:
            got = S.kb_internetarchive("COBOL compiler")
        params = parse_qs(urlsplit(http.call_args[0][0]).query)
        self.assertIn("/advancedsearch.php", http.call_args[0][0])
        self.assertEqual(params["q"], ["title:(COBOL AND compiler)"])
        self.assertEqual(params["sort[]"], ["downloads desc"])
        self.assertIn("identifier", params["fl[]"])
        cite = got["citations"][0]
        self.assertEqual(cite["source"], "ia")
        self.assertEqual(cite["url"], "https://archive.org/details/" + IA_DOC["identifier"])
        self.assertEqual(cite["identifier"], IA_DOC["identifier"])
        self.assertEqual(cite["summary_kind"], "structured_record")
        self.assertEqual(cite["year"], "1972")
        self.assertEqual(cite["container"], "texts")
        self.assertEqual(cite["extra"]["downloads"], 5297)
        self.assertEqual(cite["extra"]["creator"], "bitsavers")
        self.assertIn("bitsavers", cite["summary"])
        self.assertFalse(cite["metadata_only"])
        self.assertTrue(S._kb_has_evidence(cite))

    def test_unknown_sort_is_not_forwarded(self):
        with mock.patch.object(S, "kb_json", return_value=(response([IA_DOC]), "")) as http:
            S.kb_internetarchive("COBOL", sort="bogus")
        self.assertNotIn("sort%5B%5D", http.call_args[0][0])

    def test_missing_description_is_metadata_only(self):
        doc = {**IA_DOC, "description": None}
        with mock.patch.object(S, "kb_json", return_value=(response([doc]), "")):
            cite = S.kb_internetarchive("COBOL")["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertFalse(S._kb_has_evidence(cite))

    def test_upstream_error_json_is_not_a_success(self):
        """QUERY_NOT_READY 等は HTTP 200 で {"error": ...} として返る（実測）。成功にしない。"""
        for data in (None, [], {},
                     {"error": "New and legacy rewrite both failed with errors: [QUERY_NOT_READY]"}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                got = S.kb_internetarchive("COBOL")
            self.assertTrue(got.get("error"), data)
            self.assertEqual(got.get("citations") or [], [])

    def test_malformed_response_does_not_leak(self):
        for data in ({"response": "x"}, {"response": {"docs": "x"}}, {"response": {"docs": ["x"]}}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                self.assertTrue(S.kb_internetarchive("COBOL").get("error"), data)

    def test_empty_result_is_reported_as_no_hits(self):
        with mock.patch.object(S, "kb_json", return_value=(response([]), "")):
            got = S.kb_internetarchive("nothing matches this")
        self.assertEqual(got["error"], "該当なし")
        self.assertEqual(got["citations"], [])

    def test_identifier_is_url_escaped(self):
        doc = {**IA_DOC, "identifier": "a b/c"}
        with mock.patch.object(S, "kb_json", return_value=(response([doc]), "")):
            cite = S.kb_internetarchive("COBOL")["citations"][0]
        self.assertEqual(cite["url"], "https://archive.org/details/a%20b%2Fc")
        S._kb_http_url(cite["url"])


if __name__ == "__main__":
    unittest.main()
