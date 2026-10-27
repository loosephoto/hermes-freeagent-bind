"""v0.26.0 の契約: `freeagent_grounded` は**検索語と質問文を分けられる**.

日本語の質問のまま arXiv / Crossref / IA を引くと 0 件になりやすい（arXiv は CJK を早期拒否）。
`search_query` を渡したときだけ検索語が置き換わり、**モデルへ渡る質問文は元のまま**であることを
固定する。ネットワークも推論も使わない（`knowledge_lookup` / 選抜 / `ask_many` を差し替える）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402

QUESTION = "大規模言語モデルのスケーリング則は何ですか"
ENGLISH = "large language model scaling laws"


def _kb_stub(calls):
    def fake(query, sources=None, **kwargs):
        calls.append({"query": query, "sources": sources, **kwargs})
        return {"source": "wikipedia", "sources": ["wikipedia"],
                "citations": [{"source": "wikipedia", "title": "Scaling law",
                               "url": "https://example.org/scaling",
                               "summary": "scaling laws describe loss as a power law of compute"}],
                "errors": {}, "unknown_sources": []}
    return fake


def _answer(text):
    return [{"text": text, "served_by": "nous/m", "truncated": False, "cot_leak": False}]


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-v0260-")
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.pop("FREEAGENT_STATE_DIR", None)

    def _run(self, args, calls, answer="答え [1]"):
        with mock.patch.object(S, "knowledge_lookup", side_effect=_kb_stub(calls)), \
                mock.patch.object(S, "_select_or_error", return_value=(["nous/m"], {})), \
                mock.patch.object(S, "ask_many", return_value=_answer(answer)) as ask:
            out = S.tool_grounded(args)
        return out, ask


class TestSearchQuery(_Base):
    def test_english_search_query_keeps_the_japanese_question_for_the_model(self):
        calls = []
        out, ask = self._run({"question": QUESTION, "search_query": ENGLISH}, calls)
        self.assertEqual(calls[0]["query"], ENGLISH, "検索語が置き換わっていない")
        self.assertEqual(out["search_query"], ENGLISH)
        prompt = ask.call_args[0][1]
        self.assertIn(QUESTION, prompt, "質問文が英語に置き換わっている（モデルへ渡す文は元のまま）")
        self.assertNotIn(ENGLISH, prompt.split("【質問】")[0].replace("Scaling law", ""),
                         "検索語が根拠ブロック以外に混ざっている")

    def test_without_search_query_the_question_is_the_query(self):
        calls = []
        out, _ask = self._run({"question": QUESTION}, calls)
        self.assertEqual(calls[0]["query"], QUESTION)
        self.assertEqual(out["search_query"], QUESTION)

    def test_evidence_flag_is_still_requested(self):
        calls = []
        self._run({"question": QUESTION, "search_query": ENGLISH}, calls)
        self.assertTrue(calls[0].get("need_evidence"), "grounded が本文根拠を要求していない")

    def test_blank_search_query_falls_back_to_the_question(self):
        calls = []
        self._run({"question": QUESTION, "search_query": "   "}, calls)
        self.assertEqual(calls[0]["query"], QUESTION)


class TestSchema(_Base):
    def _schema(self, name):
        for tool in S.TOOLS:
            if tool["name"] == name:
                return tool["inputSchema"]
        raise AssertionError(f"{name} が無い")

    def test_search_query_is_declared_with_a_hint(self):
        props = self._schema("freeagent_grounded")["properties"]
        self.assertIn("search_query", props)
        desc = props["search_query"]["description"]
        self.assertIn("英語", desc)
        self.assertIn("省略", desc)

    def test_required_stays_question(self):
        self.assertEqual(self._schema("freeagent_grounded")["required"], ["question"])

    def test_lookup_is_unchanged(self):
        # lookup の `query` は検索語そのものなので、引数を増やさない（schema を太らせない）。
        self.assertNotIn("search_query", self._schema("freeagent_lookup")["properties"])


class TestRender(_Base):
    def test_render_shows_the_search_term_when_it_differs(self):
        out, _ask = self._run({"question": QUESTION, "search_query": ENGLISH}, [])
        text = S._render_grounded(out)
        self.assertIn("検索語", text)
        self.assertIn(ENGLISH, text)

    def test_render_hides_it_when_identical(self):
        out, _ask = self._run({"question": ENGLISH}, [])
        self.assertNotIn("検索語", S._render_grounded(out))


if __name__ == "__main__":
    unittest.main()
