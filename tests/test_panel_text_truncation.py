"""Panel response truncation and quality measurement regressions."""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")]
import measure_quality as MQ
from freeagent_bind import server as S

CASE = {"id": "truncation", "tier": 1, "kind": "numeric", "expect": ["630"], "question": "?"}


class PanelTextTruncationTests(unittest.TestCase):
    def panel(self, answers, refined):
        refs = [f"test/{i}" for i in range(len(answers))]
        with mock.patch.object(S, "_select_or_error", return_value=(refs, {})), \
             mock.patch.object(S, "ask_many", return_value=answers), \
             mock.patch.object(S, "call_model", return_value=refined), \
             mock.patch.object(S, "_quota_report_after_answers", return_value={}):
            return S.tool_panel({"question": "?", "models": refs, "refine": True,
                                 "refine_model": "test/refiner"})

    def test_panel_reports_token_and_character_truncation_separately_at_boundaries(self):
        for char in ("x", "界", "🙂"):
            for n, upstream in ((10, False), (10, True), (1500, False), (1501, False), (1501, True)):
                with self.subTest(char=char, length=n, upstream=upstream):
                    text = char * n
                    data = self.panel([{"text": text, "truncated": upstream}],
                                      {"text": "short", "truncated": False})
                    answer = data["answers"][0]
                    self.assertEqual(answer["truncated"], upstream)
                    self.assertEqual(answer["text_truncated"], n > 1500)
                    self.assertEqual(answer["answer"], S.truncate(text, 1500))
            for n, upstream in ((10, False), (10, True), (2000, False), (2001, False), (2001, True)):
                with self.subTest(refined=True, char=char, length=n, upstream=upstream):
                    text = char * n
                    data = self.panel([{"text": "short", "truncated": False}],
                                      {"text": text, "truncated": upstream})
                    refined = data["refined"]
                    self.assertEqual(refined["truncated"], upstream)
                    self.assertEqual(refined["text_truncated"], n > 2000)
                    self.assertEqual(refined["text"], S.truncate(text, 2000))

    def test_refine_quality_rejects_clipped_correct_answer_and_excludes_clipped_votes(self):
        clipped_correct = "Final answer: 630\n" + "界" * 2000
        answers = [{"text": "Final answer: 629\n" + "界" * 1500, "truncated": False},
                   {"text": "Final answer: 630", "truncated": False},
                   {"text": "Final answer: 630", "truncated": False}]
        refs = ["test/0", "test/1", "test/2"]
        with mock.patch.object(S, "_select_or_error", return_value=(refs, {})), \
             mock.patch.object(S, "ask_many", return_value=answers), \
             mock.patch.object(S, "call_model", return_value={"text": clipped_correct, "truncated": False}), \
             mock.patch.object(S, "free_model_refs", return_value=[]), \
             mock.patch.object(S, "_quota_report_after_answers", return_value={}):
            row = MQ.run_strategy("refine", refs, CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["verdict"], "no_answer")
        self.assertTrue(row["truncated"])
        self.assertEqual(row["agreement"], 1.0)
        self.assertEqual(row["cluster_entropy"], 0.0)
        self.assertEqual(row["attempts"], ["Final answer: 630", "Final answer: 630"])
