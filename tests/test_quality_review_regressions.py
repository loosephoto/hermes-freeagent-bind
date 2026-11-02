"""Independent review regressions for quality measurement."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")]
import measure_quality as MQ
from freeagent_bind import grading as G

CASE = {"id": "c", "tier": 1, "kind": "numeric", "expect": ["630"], "question": "?"}


class ArgumentValidationTests(unittest.TestCase):
    def test_invalid_numbers_and_duplicate_strategies_rejected_before_lookup(self):
        inputs = [[flag, value] for flag in ("--pool", "--per-tier", "--max-tokens", "--repeat")
                  for value in ("0", "-1")] + [["--strategies", "single,panel,single"]]
        for options in inputs:
            with self.subTest(options=options), mock.patch.object(MQ.S, "free_model_refs", return_value=[]) as lookup, \
                 mock.patch.object(MQ.S, "select_models") as select, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    MQ.main(options + ["--no-write"])
                self.assertNotEqual(exc.exception.code, 0)
                lookup.assert_not_called()
                select.assert_not_called()


    def test_refine_rejects_token_values_server_would_clamp(self):
        for value in ("15", "4001"):
            with self.subTest(value=value), mock.patch.object(MQ.S, "free_model_refs", return_value=[]) as lookup, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    MQ.main(["--strategies", "refine", "--max-tokens", value, "--no-write"])
                lookup.assert_not_called()


class RetryProvenanceTests(unittest.TestCase):
    def test_retry_preserves_original_and_reports_actual_effective_response(self):
        original = {"text": "Final answer: 629", "truncated": True, "served_by": "provider/original"}
        retry = {"text": "Final answer: 630", "truncated": False, "served_by": "provider/fallback"}
        with mock.patch.object(MQ, "_call", side_effect=[original, retry]) as call:
            row = MQ.run_strategy("single", ["provider/requested"], CASE, 400)
            MQ.grade_row(row, CASE, "provider/retry", 400)
        self.assertEqual(row["original_text"], original["text"])
        self.assertEqual(row["original_model"], "provider/requested")
        self.assertEqual(row["original_served_by"], "provider/original")
        self.assertTrue(row["truncated"])
        self.assertEqual(row["retry_model"], "provider/retry")
        self.assertEqual(row["retry_served_by"], "provider/fallback")
        self.assertEqual(row["retry_text"], retry["text"])
        self.assertEqual(row["text"], retry["text"])
        self.assertFalse(row["effective_truncated"])
        self.assertEqual(row["effective_model"], "provider/retry")
        self.assertEqual(row["effective_served_by"], "provider/fallback")
        self.assertEqual(row["verdict"], "correct")
        self.assertEqual(call.call_args_list[1].args[0], "provider/retry")
        self.assertNotIn("Your previous answer", call.call_args_list[1].args[1])

    def test_empty_retry_response_is_not_replaced_with_original(self):
        row = {"text": "incomplete", "calls": 1, "attempts": [], "truncated": True}
        with mock.patch.object(MQ, "_call", return_value={"text": "", "truncated": False}):
            MQ.grade_row(row, CASE, "retry", 400)
        self.assertEqual(row["original_text"], "incomplete")
        self.assertEqual(row["text"], "")
        self.assertFalse(row["effective_truncated"])
        self.assertEqual(row["verdict"], "no_answer")

    def test_retry_error_keeps_original_effective_and_reports_error(self):
        row = {"text": "incomplete", "calls": 1, "attempts": [], "truncated": True,
               "model": "original", "served_by": "actual"}
        with mock.patch.object(MQ, "_call", return_value={"error": "429"}):
            MQ.grade_row(row, CASE, "retry", 400)
        self.assertEqual(row["text"], "incomplete")
        self.assertEqual(row["retry_error"], "429")
        self.assertTrue(row["effective_truncated"])
        self.assertEqual(row["effective_model"], "original")
        self.assertEqual(row["verdict"], "no_answer")


class TruncationTests(unittest.TestCase):
    def test_incomplete_votes_excluded_from_panel_cascade_and_metrics(self):
        results = [{"text": "Final answer: 629", "truncated": True},
                   {"text": "Final answer: 629", "truncated": True},
                   {"text": "Final answer: 630", "truncated": False},
                   {"text": "Final answer: 630", "truncated": False}]
        for name in ("panel", "cascade"):
            def ask(refs, *args, **kwargs):
                return [results[int(ref)] for ref in refs]
            with self.subTest(strategy=name), mock.patch.object(MQ.S, "ask_many", side_effect=ask):
                row = MQ.run_strategy(name, ["0", "1", "2", "3"], CASE, 400)
                MQ.grade_row(row, CASE, None, 400)
                self.assertEqual(row["verdict"], "correct")
                self.assertEqual(row["calls"], 4)
                self.assertFalse(row["truncated"])
                self.assertEqual(row["agreement"], 1.0)
                self.assertEqual(row["cluster_entropy"], 0.0)

    def test_same_text_as_incomplete_vote_does_not_taint_selected_vote(self):
        results = [{"text": "Final answer: 630", "truncated": True},
                   {"text": "Final answer: 630", "truncated": False}]
        with mock.patch.object(MQ.S, "ask_many", return_value=results):
            row = MQ.run_strategy("panel", ["a", "b"], CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["verdict"], "correct")
        self.assertFalse(row["truncated"])
        self.assertIsNone(row["agreement"])

    def test_all_incomplete_votes_preserve_source_for_retry(self):
        results = [{"text": "Final answer: 629", "truncated": True, "served_by": "actual/a"},
                   {"text": "Final answer: 630", "truncated": True}]
        with mock.patch.object(MQ.S, "ask_many", return_value=results):
            row = MQ.run_strategy("panel", ["a", "b"], CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["original_text"], "Final answer: 629")
        self.assertEqual(row["original_model"], "a")
        self.assertEqual(row["original_served_by"], "actual/a")
        self.assertTrue(row["truncated"])
        self.assertEqual(row["verdict"], "no_answer")

    def test_moa_metrics_exclude_incomplete_candidates(self):
        results = [{"text": "Final answer: 629", "truncated": True},
                   {"text": "Final answer: 630"}, {"text": "Final answer: 630"}]
        with mock.patch.object(MQ.S, "ask_many", return_value=results), \
             mock.patch.object(MQ, "_call", return_value={"text": "Final answer: 630"}):
            row = MQ.run_strategy("moa", ["a", "b", "c"], CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["agreement"], 1.0)

    def test_refine_metrics_exclude_incomplete_answers(self):
        data = {"refined": {"text": "Final answer: 630"}, "calls": 4,
                "answers": [{"answer": "Final answer: 629", "truncated": True},
                            {"answer": "Final answer: 630"}, {"answer": "Final answer: 630"}]}
        with mock.patch.object(MQ.S, "tool_panel", return_value=data):
            row = MQ.run_strategy("refine", ["a", "b", "c"], CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["agreement"], 1.0)

    def test_truncated_labeled_and_boxed_answers_are_never_scored(self):
        for text in ("Final answer: 630", "Final answer: 629", r"\boxed{630}", ""):
            with self.subTest(text=text):
                result = G.grade(text, CASE, truncated=True)
                self.assertEqual(result["verdict"], "no_answer")
                self.assertIsNone(result["answer"])
                self.assertTrue(G.needs_retry(text, truncated=True))
        self.assertEqual(G.grade("Final answer: 630", CASE)["verdict"], "correct")
        self.assertEqual(G.grade("Final answer: 629", CASE)["verdict"], "wrong")
