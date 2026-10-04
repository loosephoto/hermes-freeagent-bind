"""CiNii Research（明示指定のみ・appid 必須）のオフライン回帰。HTTPのみ模擬。

利用条件は SPEC §6.6 / server.py §5.17 を参照。要点は「利用者自身の appid が無いときは
HTTP を一切出さない」「応答は書誌のみ（抄録を返さない）ので本文根拠にしない」の 2 つ。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


CINII_ROW = {
    "@id": "https://cir.nii.ac.jp/crid/1574231874641506304",
    "@type": "item",
    "title": "圧電材料学の基礎",
    "link": {"@id": "https://cir.nii.ac.jp/crid/1574231874641506304"},
    "rdfs:seeAlso": {"@id": "https://cir.nii.ac.jp/crid/1574231874641506304.json"},
    "dc:creator": ["池田拓郎"],
    "dc:publisher": "オーム社",
    "dc:type": "Article",
    "prism:publicationName": "圧電材料の基礎",
    "prism:startingPage": "221",
    "prism:endingPage": "246",
    "prism:publicationDate": "2008",
    "dc:identifier": [{"@type": "cir:NAID", "@value": "10014997826"}],
}


class TestCiniiSource(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        for patch in (mock.patch.object(S, "_kb_rate_acquire", return_value=0.0),
                      mock.patch.object(S, "CINII_APPID", "test-appid")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_appid_missing_does_not_touch_the_network(self):
        """未設定なら HTTP を出さず、登録先と設定方法を案内する（規約は登録を要求している）。"""
        with mock.patch.object(S, "CINII_APPID", ""), mock.patch.object(S, "kb_json") as http:
            got = S.kb_cinii("圧電材料")
            http.assert_not_called()
        self.assertIn("appid", got["error"])
        self.assertIn("FREEAGENT_CINII_APPID", got["error"])
        self.assertFalse(got.get("citations"))

    def test_appid_is_sent_and_bibliography_is_normalized(self):
        captured = {}

        def fake(url):
            captured["url"] = url
            return ({"items": [CINII_ROW]}, "")

        with mock.patch.object(S, "kb_json", side_effect=fake):
            got = S.kb_cinii("圧電材料", limit=3)
        self.assertIn("cir.nii.ac.jp/opensearch/all", captured["url"])
        self.assertIn("appid=test-appid", captured["url"])
        cite = got["citations"][0]
        self.assertEqual(cite["title"], "圧電材料学の基礎")
        self.assertEqual(cite["url"], "https://cir.nii.ac.jp/crid/1574231874641506304")
        self.assertEqual(cite["year"], "2008")
        item = got["items"][0]
        self.assertEqual(item["container"], "圧電材料の基礎")
        self.assertEqual(item["authors"], ["池田拓郎"])
        self.assertEqual(item["publication_types"], ["Article"])
        self.assertEqual(item["identifier"], "10014997826")
        self.assertIn("CiNii Research", got["attribution"])

    def test_no_abstract_so_items_are_metadata_only(self):
        """抄録を返さない API なので本文根拠にしない（規程 第5条2 の複製の制限にも触れない）。"""
        with mock.patch.object(S, "kb_json", return_value=({"items": [CINII_ROW]}, "")):
            got = S.kb_cinii("圧電材料")
        cite = got["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertEqual(cite.get("summary"), "")
        self.assertFalse(S._kb_has_evidence(cite))
        self.assertEqual(S._evidence_block([cite]), "")

    def test_doi_wins_over_the_cir_page_url(self):
        row = {**CINII_ROW, "dc:identifier": [{"@type": "cir:DOI", "@value": "10.1234/cinii"}]}
        with mock.patch.object(S, "kb_json", return_value=({"items": [row]}, "")):
            item = S.kb_cinii("q")["items"][0]
        self.assertEqual(item["url"], "https://doi.org/10.1234/cinii")
        self.assertEqual(item["cir_uri"], "https://cir.nii.ac.jp/crid/1574231874641506304")
        self.assertEqual(item["doi"], "10.1234/cinii")

    def test_creator_as_plain_string_is_accepted(self):
        row = {**CINII_ROW, "dc:creator": "池田拓郎"}
        with mock.patch.object(S, "kb_json", return_value=({"items": [row]}, "")):
            item = S.kb_cinii("q")["items"][0]
        self.assertEqual(item["authors"], ["池田拓郎"])

    def test_malformed_responses_do_not_leak(self):
        for data in (None, [], {"items": "x"}, {"items": ["x"]}, {"items": [{"title": ""}]},
                     {"items": [{"title": "t", "link": {"@id": "javascript:alert(1)"}}]}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                got = S.kb_cinii("q")
            self.assertTrue(got.get("error"), data)
            self.assertFalse(got.get("citations"))

    def test_bad_arguments_do_not_touch_the_network(self):
        with mock.patch.object(S, "kb_json") as http:
            self.assertTrue(S.kb_cinii(123).get("error"))
            self.assertTrue(S.kb_cinii("").get("error"))
            http.assert_not_called()

    def test_rate_budget_failure_is_reported_not_waited(self):
        with mock.patch.object(S, "_kb_rate_acquire", return_value=1.5), \
             mock.patch.object(S, "kb_json") as http:
            got = S.kb_cinii("q")
            http.assert_not_called()
        self.assertIn("429", got["error"])

    def test_source_is_opt_in_and_schema_exposes_it(self):
        self.assertIn("cinii", S.SOURCES)
        self.assertIn("cinii", S.KB_BACKENDS)
        self.assertNotIn("cinii", S.DEFAULT_SOURCES)
        lookup = next(t for t in S.TOOLS if t["name"] == "freeagent_lookup")
        desc = str(lookup["inputSchema"]["properties"]["sources"]["description"])
        self.assertIn("cinii", desc)
        self.assertIn("FREEAGENT_CINII_APPID", desc)

    def test_lookup_route_reports_the_missing_appid(self):
        """lookup 経由でも同じ案内が出る（例外にならない・他ソースは落ちない）。"""
        with mock.patch.object(S, "CINII_APPID", ""):
            got = S.tool_lookup({"query": "圧電材料", "sources": ["cinii"]})
        errors = got.get("errors") or {}
        self.assertIn("cinii", errors)
        self.assertIn("appid", errors["cinii"])
        self.assertEqual(got.get("citations"), [])


if __name__ == "__main__":
    unittest.main()
