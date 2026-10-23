"""測定器（`scripts/measure_quality.py`）の v0.14.0 契約。

固定する契約:
  * ケースは生成（gen）／従来（builtin）／両方（both）から選べる。生成分は検算を通ったものだけ
  * **切り詰めの救済**: 最終回答が取れない回答は 1 回だけ再質問し、呼び出し数に数える
  * 再質問は 1 回まで（無限に呼ばない）・失敗したら `no_answer` のまま
  * 多数決は「抽出した最終回答」で数える（本文の表層一致ではない）
  * 集計は帯別・無回答率・切断数・再質問数を返す
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import measure_quality as MQ  # noqa: E402

CASE = {"id": "c", "tier": 2, "kind": "numeric", "expect": ["630"],
        "question": "How many are good? Final answer: <number>."}


class TestLoadCases(unittest.TestCase):
    def test_generated(self):
        cases = MQ.load_cases("gen", per_tier=1, seed=5, limit=None)
        self.assertEqual(sorted(c["tier"] for c in cases), [1, 2, 3])

    def test_builtin(self):
        cases = MQ.load_cases("builtin", per_tier=1, seed=5, limit=None)
        self.assertEqual(len(cases), len(MQ.BUILTIN_CASES))
        self.assertTrue(all("expect" in c and "tier" in c for c in cases))

    def test_both_with_limit(self):
        cases = MQ.load_cases("both", per_tier=1, seed=5, limit=4)
        self.assertEqual(len(cases), 4)


class TestConfidence(unittest.TestCase):
    def test_parses_last_confidence(self):
        self.assertEqual(MQ.parse_confidence("Confidence: 80"), 80.0)
        self.assertEqual(MQ.parse_confidence("blah\nConfidence: 70\nblah\n確信度: 90"), 90.0)

    def test_missing_is_none(self):
        self.assertIsNone(MQ.parse_confidence("no number here"))
        self.assertIsNone(MQ.parse_confidence(""))

    def test_clamped(self):
        self.assertEqual(MQ.parse_confidence("Confidence: 250"), 100.0)


class TestVote(unittest.TestCase):
    def test_votes_on_extracted_answer(self):
        texts = ["Reasoning... Final answer: 630", "The answer is 630", "Final answer: 540"]
        self.assertIn("630", MQ.vote(texts))

    def test_ignores_unanswered(self):
        texts = ["I cannot solve this", "Final answer: 7", "Final answer: 7"]
        self.assertIn("7", MQ.vote(texts))

    def test_empty(self):
        self.assertEqual(MQ.vote(["no answer", "still nothing"]), "")


class TestRetry(unittest.TestCase):
    def test_no_answer_triggers_one_retry(self):
        row = {"text": "Reasoning that got truncated before any final line", "calls": 1,
               "attempts": [], "wall_s": 0.1, "detail": "", "truncated": True, "strategy": "single"}
        with mock.patch.object(MQ, "_call", return_value={"text": "Final answer: 630"}) as call:
            MQ.grade_row(row, CASE, retry_ref="nous/x:free", max_tokens=400)
        self.assertEqual(call.call_count, 1)
        self.assertTrue(row["retried"])
        self.assertEqual(row["verdict"], "correct")
        self.assertEqual(row["calls"], 2)

    def test_retry_failure_keeps_no_answer(self):
        row = {"text": "truncated nonsense", "calls": 1, "attempts": [], "wall_s": 0.1,
               "detail": "", "truncated": True, "strategy": "single"}
        with mock.patch.object(MQ, "_call", return_value={"error": "429 rate limited"}):
            MQ.grade_row(row, CASE, retry_ref="nous/x:free", max_tokens=400)
        self.assertTrue(row["retried"])
        self.assertEqual(row["verdict"], "no_answer")
        self.assertTrue(row["no_answer"])

    def test_no_retry_when_disabled(self):
        row = {"text": "no final line", "calls": 1, "attempts": [], "wall_s": 0.1,
               "detail": "", "truncated": True, "strategy": "single"}
        with mock.patch.object(MQ, "_call") as call:
            MQ.grade_row(row, CASE, retry_ref=None, max_tokens=400)
        call.assert_not_called()
        self.assertEqual(row["verdict"], "no_answer")

    def test_correct_answer_is_not_retried(self):
        row = {"text": "Final answer: 630", "calls": 1, "attempts": [], "wall_s": 0.1,
               "detail": "", "truncated": False, "strategy": "single"}
        with mock.patch.object(MQ, "_call") as call:
            MQ.grade_row(row, CASE, retry_ref="nous/x:free", max_tokens=400)
        call.assert_not_called()
        self.assertEqual(row["verdict"], "correct")

    def test_entropy_only_for_multiple_attempts(self):
        row = {"text": "Final answer: 630", "calls": 3, "attempts": ["Final answer: 630",
                                                                   "Final answer: 630",
                                                                   "Final answer: 540"],
               "wall_s": 1.0, "detail": "", "truncated": False, "strategy": "panel"}
        MQ.grade_row(row, CASE, retry_ref=None, max_tokens=400)
        self.assertIsNotNone(row["cluster_entropy"])
        self.assertGreater(row["cluster_entropy"], 0)
        single = {"text": "Final answer: 630", "calls": 1, "attempts": ["Final answer: 630"],
                  "wall_s": 1.0, "detail": "", "truncated": False, "strategy": "single"}
        MQ.grade_row(single, CASE, retry_ref=None, max_tokens=400)
        self.assertIsNone(single["cluster_entropy"])


class TestSummarize(unittest.TestCase):
    def _rows(self):
        rows = []
        for tier in (1, 2):
            for name, verdict in (("single", "correct"), ("panel", "wrong"),
                                  ("single", "no_answer"), ("panel", "correct")):
                rows.append({"strategy": name, "tier": tier, "case": f"c{tier}", "repeat": 1,
                             "ok": verdict == "correct", "verdict": verdict,
                             "no_answer": verdict == "no_answer", "calls": 1, "wall_s": 1.0,
                             "cluster_entropy": 0.5 if name == "panel" else None,
                             "agreement": 0.5 if name == "panel" else None,
                             "truncated": False, "retried": False})
        return rows

    def test_by_tier_and_rates(self):
        summary = MQ.summarize(self._rows(), ["single", "panel"])
        self.assertIn("1", summary["single"]["by_tier"])
        self.assertEqual(summary["single"]["runs"], 4)
        self.assertEqual(summary["single"]["correct"], 2)   # 2 帯 × 1 件ずつ
        self.assertEqual(summary["single"]["no_answer"], 2)
        self.assertEqual(summary["single"]["no_answer_rate"], 0.5)
        self.assertIsNotNone(summary["panel"]["cluster_entropy_mean"])
        self.assertIsNone(summary["single"]["cluster_entropy_mean"])

    def test_calibration_requires_enough_pairs(self):
        summary = MQ.summarize(self._rows(), ["single"])
        self.assertIsNone(summary["single"]["brier"], "4 件未満で校正誤差を出してはいけない")


if __name__ == "__main__":
    unittest.main()