"""Offline regression tests for quality measurement audit findings."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src"), os.path.join(ROOT, "scripts")]
import measure_quality as MQ

CASE = {"id": "c", "tier": 1, "kind": "numeric", "expect": ["630"], "question": "?"}


class QualityAuditTests(unittest.TestCase):
    def test_truncated_selected_answer_for_each_strategy(self):
        bad = {"text": "Reasoning 629", "truncated": True}
        for name in ("single", "panel", "moa", "refine", "cascade"):
            with self.subTest(strategy=name), \
                 mock.patch.object(MQ, "_call", return_value=bad), \
                 mock.patch.object(MQ.S, "ask_many", return_value=[bad]), \
                 mock.patch.object(MQ.S, "tool_panel", return_value={
                     "refined": bad, "answers": [{"answer": "Final answer: 630"}], "calls": 2}):
                row = MQ.run_strategy(name, ["fake/model"], CASE, 400)
                MQ.grade_row(row, CASE, None, 400)
                self.assertTrue(row["truncated"])
                self.assertEqual(row["verdict"], "no_answer")

    def test_vote_tracks_selected_answer_not_other_truncation(self):
        results = [{"text": "Final answer: 630", "truncated": False},
                   {"text": "Final answer: 629", "truncated": True}]
        for name in ("panel", "cascade"):
            with self.subTest(strategy=name), mock.patch.object(MQ.S, "ask_many", return_value=results):
                row = MQ.run_strategy(name, ["fake/a", "fake/b"], CASE, 400)
                MQ.grade_row(row, CASE, None, 400)
                self.assertEqual(row["verdict"], "correct")
                self.assertFalse(row["truncated"])

    def test_refine_uses_actual_panel_answer_key_for_metrics(self):
        data = {"refined": {"text": "Final answer: 630"}, "calls": 3,
                "answers": [{"answer": "Final answer: 630"}, {"answer": "Final answer: 629"}]}
        with mock.patch.object(MQ.S, "tool_panel", return_value=data):
            row = MQ.run_strategy("refine", ["fake/a", "fake/b"], CASE, 400)
            MQ.grade_row(row, CASE, None, 400)
        self.assertEqual(row["attempts"], ["Final answer: 630", "Final answer: 629"])
        self.assertGreater(row["cluster_entropy"], 0)
        self.assertEqual(row["agreement"], 0.5)

    def test_all_is_both_and_unknown_case_source_fails(self):
        both = MQ.load_cases("both", per_tier=4, seed=5, limit=None)
        self.assertEqual(len(both), 22)
        self.assertEqual(MQ.load_cases("all", per_tier=4, seed=5, limit=None), both)
        with self.assertRaises((ValueError, SystemExit)):
            MQ.load_cases("typo", per_tier=4, seed=5, limit=None)

    def test_invalid_case_selection_rejected_before_model_lookup(self):
        for source in ("typo", "0"):
            with self.subTest(source=source), \
                 mock.patch.object(MQ.S, "free_model_refs", return_value=[]) as lookup, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    MQ.main(["--cases", source, "--no-write"])
                self.assertNotEqual(exc.exception.code, 0)
                lookup.assert_not_called()

    def test_json_selection_output_is_json_only_and_all_runs(self):
        output = io.StringIO()
        with mock.patch.object(MQ.S, "select_models", return_value=(["fake/model"], {})), \
             mock.patch.object(MQ, "_call", return_value={"text": "Final answer: 630"}) as call, \
             mock.patch.dict(os.environ), contextlib.redirect_stdout(output), \
             contextlib.redirect_stderr(io.StringIO()):
            code = MQ.main(["--cases", "all", "--strategies", "single", "--no-retry",
                            "--select-strategy", "quality", "--json", "--no-write"])
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["cases"], 22)
        self.assertEqual(payload["strategies"]["single"]["runs"], 22)
        self.assertEqual(call.call_count, 22)

    def test_empty_or_unknown_strategy_rejected_before_model_lookup(self):
        for strategies in ("", " , ", "single,not-real"):
            with self.subTest(strategies=strategies), \
                 mock.patch.object(MQ.S, "free_model_refs", return_value=[]) as lookup, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    MQ.main(["--cases", "1", "--strategies", strategies, "--no-write"])
                self.assertNotEqual(exc.exception.code, 0)
                lookup.assert_not_called()

    def test_retry_exposes_its_own_truncation_without_losing_original(self):
        for retry_truncated in (True, False):
            with self.subTest(retry_truncated=retry_truncated):
                row = {"text": "unfinished", "calls": 1, "attempts": [], "truncated": True}
                with mock.patch.object(MQ, "_call", return_value={
                        "text": "Reasoning 629" if retry_truncated else "Final answer: 630",
                        "truncated": retry_truncated}):
                    MQ.grade_row(row, CASE, "fake/model", 400)
                self.assertTrue(row["truncated"])
                self.assertEqual(row["retry_truncated"], retry_truncated)
                self.assertEqual(row["verdict"], "no_answer" if retry_truncated else "correct")

    def test_empty_case_result_is_rejected_before_model_lookup(self):
        with mock.patch.object(MQ, "load_cases", return_value=[]), \
             mock.patch.object(MQ.S, "free_model_refs") as lookup, \
             contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as exc:
                MQ.main(["--cases", "gen", "--no-write"])
            self.assertNotEqual(exc.exception.code, 0)
            lookup.assert_not_called()

    def test_truncated_retry_is_no_answer(self):
        row = {"text": "incomplete", "calls": 1, "attempts": [], "truncated": True}
        with mock.patch.object(MQ, "_call", return_value={"text": "Reasoning 629", "truncated": True}):
            MQ.grade_row(row, CASE, "fake/model", 400)
        self.assertEqual(row["verdict"], "no_answer")
        self.assertTrue(row["truncated"])
