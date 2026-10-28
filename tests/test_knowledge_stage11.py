"""第11段階ソース（MDN Web Docs / DevDocs）のオフライン回帰."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


class TestStage11Registry(unittest.TestCase):
    def test_mdn_and_devdocs_are_opt_in_and_advertised(self):
        for name in ("mdn", "devdocs"):
            self.assertIn(name, S.SOURCES)
            self.assertIn(name, S.KB_BACKENDS)
            self.assertNotIn(name, S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)

    def test_mdn_never_touches_network_without_query(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_mdn("", 3).get("error"))
            http.assert_not_called()

    def test_devdocs_never_touches_network_without_query_or_doc(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_devdocs("", "", 3).get("error"))
            http.assert_not_called()

    def test_mdn_parses_documents(self):
        fake = {"documents": [{"title": "Array.map()", "mdn_url": "/en-US/docs/Web/JavaScript/Reference/Global_Objects/Array/map",
                              "summary": "creates a new array"}]}
        with mock.patch.object(S, "_kb_new_json", return_value=(fake, "")):
            with mock.patch.object(S, "_kb_new_cached", side_effect=lambda k, src, prod: prod()):
                res = S.kb_mdn("map", 3)
        self.assertEqual(len(res["items"]), 1)
        self.assertTrue(res["items"][0]["url"].startswith("https://developer.mozilla.org"))
        self.assertEqual(res["items"][0]["summary_kind"], "structured_record")

    def test_devdocs_list_parses_docsets(self):
        fake = [{"name": "Python", "slug": "python~3.14", "version": "3.14"}]
        with mock.patch.object(S, "_kb_new_json", return_value=(fake, "")):
            with mock.patch.object(S, "_kb_new_cached", side_effect=lambda k, src, prod: prod()):
                res = S.kb_devdocs("python", "", 3)
        self.assertEqual(len(res["items"]), 1)
        self.assertEqual(res["items"][0]["slug"], "python~3.14")
        self.assertEqual(res["items"][0]["summary_kind"], "metadata_only")

    def test_devdocs_search_parses_entries(self):
        fake = {"entries": [{"name": "Array.map", "path": "global_objects/array/map", "type": "Methods"}]}
        with mock.patch.object(S, "_kb_new_json", return_value=(fake, "")):
            with mock.patch.object(S, "_kb_new_cached", side_effect=lambda k, src, prod: prod()):
                res = S.kb_devdocs("map", "javascript", 3)
        self.assertEqual(len(res["items"]), 1)
        self.assertIn("javascript", res["items"][0]["url"])


if __name__ == "__main__":
    unittest.main()
