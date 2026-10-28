"""第10段階ソース（OpenReview）のオフライン回帰.

HTTP のみ模擬する。固定すること:
  * **明示指定のみ**（`DEFAULT_SOURCES` は 6 のまま）。
  * v2 の `content` が `{value: …}` で包まれる形を剥がす（`_ov_value`）。
  * **venueid から査読状態を機械可読にする**（`Withdrawn` / `Desk_Rejected` / `Rejected` / 未査読）。
    撤回論文を「査読を通った根拠」と誤読させないための情報なので、`kind` を落とさない。
  * **日本語（CJK だけ）の問いは HTTP を出さない**（実測 0 件・1 req/s の枠を空振りで食わない）。
  * 403（ChallengeRequired）・400（空 term）・配列でない応答を、それぞれ違う文言のエラーにする。
"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S

STAGE10 = ("openreview",)


def ov_note(title, abstract="A" * 400, venue="ICLR 2025 Poster", venueid="ICLR.cc/2025/Conference",
            forum="abc123", authors=("A One", "B Two"), pdate=1737562180565):
    """API v2 の note（content の各値が `{value: …}` で包まれる）。"""
    return {
        "id": forum + "_note",
        "forum": forum,
        "pdate": pdate,
        "cdate": pdate,
        "content": {
            "title": {"value": title},
            "abstract": {"value": abstract},
            "venue": {"value": venue},
            "venueid": {"value": venueid},
            "authors": {"value": list(authors)},
            "keywords": {"value": ["kw"]},
        },
    }


class TestStage10Registry(unittest.TestCase):
    def test_openreview_is_opt_in_and_advertised(self):
        self.assertIn("openreview", S.SOURCES)
        self.assertIn("openreview", S.KB_BACKENDS)
        self.assertNotIn("openreview", S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            self.assertIn("openreview", desc, tool)

    def test_interval_is_self_imposed_and_configurable(self):
        """公表レートが無いので既定 1 req/s（npm と同じ型）。budget_key はエンドポイント単位。"""
        self.assertEqual(S.OPENREVIEW_INTERVAL, 1.0)
        self.assertEqual(S.OPENREVIEW_BUDGET, "api2.openreview.net/notes-search")

    def test_never_touches_the_network_without_an_english_query(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_openreview("", 3).get("error"))
            self.assertTrue(S.kb_openreview("強化学習", 3).get("error"))
            self.assertTrue(S.KB_BACKENDS["openreview"]("量子力学", 3, {}).get("error"))
            http.assert_not_called()

    def test_english_query_is_sent(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"notes": []}, "")) as http:
            S.kb_openreview("Transformer の 強化学習", 3)
            http.assert_called_once()
            self.assertIn("Transformer", http.call_args[0][0])


class TestOpenReviewShape(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()

    def _call(self, notes, limit=3, query="mixture of experts"):
        with mock.patch.object(S, "_kb_new_json", return_value=({"notes": list(notes)}, "")):
            return S.kb_openreview(query, limit)

    def test_query_parameters_and_budget(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"notes": []}, "")) as http:
            S.kb_openreview("large language model", 4)
        url = http.call_args[0][0]
        parts = urlsplit(url)
        qs = parse_qs(parts.query)
        self.assertEqual(parts.netloc, "api2.openreview.net")
        self.assertEqual(parts.path, "/notes/search")
        self.assertEqual(qs["term"], ["large language model"])
        self.assertEqual(qs["limit"], ["4"])
        self.assertEqual(qs["source"], ["forum"])
        self.assertEqual(http.call_args[0][1], S.OPENREVIEW_INTERVAL)
        self.assertEqual(http.call_args[1].get("budget_key"), S.OPENREVIEW_BUDGET)

    def test_citation_shape_and_forum_url(self):
        res = self._call([ov_note("A Paper")])
        self.assertEqual(res["error"], "")
        cite = res["citations"][0]
        for key in ("source", "title", "url", "summary", "year", "container", "authors", "kind"):
            self.assertIn(key, cite)
        self.assertEqual(cite["source"], "openreview")
        self.assertEqual(cite["url"], "https://openreview.net/forum?id=abc123")
        self.assertEqual(cite["container"], "ICLR 2025 Poster")
        self.assertEqual(cite["year"], 2025)
        self.assertFalse(cite.get("metadata_only"))
        self.assertEqual(cite["authors"], ["A One", "B Two"])
        S._kb_http_url(cite["url"])   # 形が壊れていれば例外

    def test_empty_abstract_is_metadata_only(self):
        res = self._call([ov_note("No Abstract Paper", abstract="")])
        cite = res["citations"][0]
        self.assertTrue(cite.get("metadata_only"))
        self.assertEqual(cite.get("summary"), "")

    def test_v1_style_plain_values_also_parse(self):
        note = ov_note("Plain Values", pdate=1695325823725)
        note["content"] = {"title": "Plain Values", "abstract": "B" * 300,
                           "venue": "NeurIPS 2024 poster", "venueid": "NeurIPS.cc/2024/Conference",
                           "authors": ["C Three"]}
        res = self._call([note])
        self.assertEqual(res["items"][0]["title"], "Plain Values")
        self.assertEqual(res["citations"][0]["year"], 2023)

    def test_limit_is_clamped_in_the_query(self):
        """limit は 1〜20 に丸める（上流の応答サイズを膨らませない）。"""
        for given, sent in ((99, "20"), (0, "1"), (3, "3")):
            with mock.patch.object(S, "_kb_new_json", return_value=({"notes": []}, "")) as http:
                S._KB_CACHE.clear()
                S.kb_openreview(f"q{given}", given)
            self.assertEqual(parse_qs(urlsplit(http.call_args[0][0]).query)["limit"], [sent], given)

    def test_rows_without_title_or_forum_are_skipped(self):
        broken = {"id": "x", "content": {"title": {"value": ""}, "forum": {"value": "abc"}}}
        no_forum = ov_note("Has Title")
        no_forum.pop("forum")
        no_forum["id"] = ""
        self.assertEqual(self._call([broken]).get("items"), [])
        self.assertEqual(self._call([no_forum]).get("items"), [])

    def test_unknown_result_is_no_hit(self):
        res = self._call([])
        self.assertEqual(res["items"], [])
        self.assertEqual(res["error"], "該当なし")


class TestOpenReviewKinds(unittest.TestCase):
    def test_kind_from_venueid(self):
        self.assertEqual(S._ov_kind("ICLR.cc/2025/Conference/Withdrawn_Submission", ""), "withdrawn")
        self.assertEqual(S._ov_kind("ICLR.cc/2025/Conference/Desk_Rejected_Submission", ""), "desk_rejected")
        self.assertEqual(S._ov_kind("ICLR.cc/2025/Conference/Rejected_Submission", ""), "rejected")
        self.assertEqual(S._ov_kind("ICLR.cc/2026/Conference/Submission", ""), "under_review")
        self.assertEqual(S._ov_kind("ICLR.cc/2025/Conference", "ICLR 2025 Poster"), "published")
        self.assertEqual(S._ov_kind("", ""), "unknown")

    def test_kind_falls_back_to_venue_text(self):
        """venueid が状態を語らない版でも、venue の文字列から撤回を拾う。"""
        self.assertEqual(S._ov_kind("dblp.org/journals/X/2025", "Some Journal Withdrawn"), "withdrawn")
        self.assertEqual(S._ov_kind("dblp.org/journals/X/2025", "Desk Rejected"), "desk_rejected")

    def test_withdrawn_paper_is_labelled(self):
        S._KB_CACHE.clear()
        note = ov_note("Retracted Thing", venue="ICLR 2025 Conference Withdrawn Submission",
                       venueid="ICLR.cc/2025/Conference/Withdrawn_Submission")
        with mock.patch.object(S, "_kb_new_json", return_value=({"notes": [note]}, "")):
            cite = S.kb_openreview("retracted thing", 1)["citations"][0]
        self.assertEqual(cite["kind"], "withdrawn")
        self.assertIn("Withdrawn", cite["container"])


class TestOpenReviewErrors(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()

    def _err(self, err):
        with mock.patch.object(S, "_kb_new_json", return_value=(None, err)):
            return S.kb_openreview("transformer", 3).get("error")

    def test_challenge_is_reported_as_such(self):
        msg = self._err("HTTP 403: ChallengeRequiredError")
        self.assertIn("ChallengeRequired", msg)
        self.assertIn("403", msg)

    def test_empty_term_is_reported(self):
        msg = self._err("HTTP 400: ValidationError: term must NOT have fewer than 1 characters")
        self.assertIn("400", msg)

    def test_other_errors_pass_through(self):
        self.assertIn("timeout", self._err("timeout"))

    def test_non_array_notes_do_not_raise(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"notes": {"a": 1}}, "")):
            res = S.kb_openreview("transformer", 3)
        self.assertEqual(res.get("items"), None)
        self.assertIn("応答解析失敗", res["error"])


if __name__ == "__main__":
    unittest.main()
