"""合意度・参加者・置換の数え方（v0.42.2 / issue #4 #6 #7 #9）.

固定する契約:
  * 短答（記号・数値・短い語）の合意度は**完全一致**で数える（`agreement_source == "exact"`）。
    長文は表層類似度（`similarity`）で、票は返さない。
  * 実測の再現: 全会一致 `["C", "C", "C"]` は 1.0（旧実装は表層類似度で 0.167〜1.0 に散らばった）。
  * `participants` は要求数・実数・落ちた理由（cooling / busy / empty / timeout / error / not_reached）。
  * `substitutions` は「要求したモデル ≠ served_by」の件数と内訳。
  * `map` は `wall_s` / `sequential_estimate_s` / `speedup` を返す（fanout と同じ形）。
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


class AgreementShape(unittest.TestCase):
    def test_short_answers_use_exact_match(self):
        got = S.agreement_detail(["C", "C", "B", "C"])
        self.assertEqual(got["source"], "exact")
        self.assertEqual(got["value"], 0.75)
        self.assertEqual(got["votes"], {"c": 3, "b": 1})

    def test_unanimous_short_answers_are_one(self):
        self.assertEqual(S.agreement_detail(["29", "29", "29"])["value"], 1.0)

    def test_numbers_and_fractions_count_as_short(self):
        for ans in ("1/216", "1.414", "**31**", "５０４０"):
            self.assertEqual(S.answer_shape(ans), "exact", ans)

    def test_prose_and_labeled_answers_use_similarity(self):
        for ans in ("結論: 2 が答えです。\n確信度: 100", "長い説明です。これは結論です。",
                    "2 の 100 乗は 10 進数で 31 桁である。"):
            self.assertEqual(S.answer_shape(ans), "similarity", ans)
        got = S.agreement_detail(["長い説明です。A", "長い説明です。B"])
        self.assertEqual(got["source"], "similarity")
        self.assertEqual(got["votes"], {})

    def test_fewer_than_two_answers_is_not_scored(self):
        got = S.agreement_detail(["C"])
        self.assertEqual((got["value"], got["source"]), (0.0, "none"))

    def test_note_mentions_the_method(self):
        self.assertIn("完全一致", S._agreement_note("exact"))
        self.assertIn("類似度", S._agreement_note("similarity"))
        self.assertIn("2 体未満", S._agreement_note("none"))


class Participants(unittest.TestCase):
    def test_dropped_participants_carry_a_reason(self):
        rows = [{"model": "a"}, {"model": "b", "error": "冷却中です（残り 12 秒）"},
                {"model": "c", "error": "空応答（nous/c）。max_tokens=400"}]
        got = S.participants_of(["a", "b", "c", "d"], rows)
        self.assertEqual(got["requested"], 4)
        self.assertEqual(got["answered"], 1)
        reasons = {row["model"]: row["reason"] for row in got["dropped"]}
        self.assertEqual(reasons["b"], "cooling")
        self.assertEqual(reasons["c"], "empty")
        self.assertEqual(reasons["d"], "not_reached")

    def test_rate_limit_is_classified_as_busy(self):
        rows = [{"model": "a", "error": "HTTP 429: too many requests"}]
        self.assertEqual(S.participants_of(["a"], rows)["dropped"][0]["reason"], "busy")


class Substitutions(unittest.TestCase):
    def test_counts_requested_vs_served(self):
        rows = [{"model": "a", "served_by": "a"},
                {"model": "b", "served_by": "solar", "answer": "x"},
                {"model": "b", "served_by": "solar", "answer": "y"},
                {"model": "c", "error": "空応答"}]
        got = S.substitutions_of(rows)
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["ratio"], round(2 / 3, 3))
        self.assertEqual(got["models"][0], {"requested": "b", "served_by": "solar", "n": 2})

    def test_no_substitution_is_zero(self):
        got = S.substitutions_of([{"model": "a", "served_by": "a"}])
        self.assertEqual((got["count"], got["models"]), (0, []))


class RenderHidesNothing(unittest.TestCase):
    def test_panel_header_shows_requested_and_answered(self):
        text = S.render("freeagent_panel", {
            "answered": 3, "models": ["a", "b", "c", "d"], "independent_sources": 2,
            "agreement": 0.667, "agreement_source": "exact", "confidence_mean": 97.0,
            "participants": {"requested": 4, "answered": 3,
                             "dropped": [{"model": "d", "reason": "cooling"}]},
            "substitutions": {"count": 1, "ratio": 0.333,
                              "models": [{"requested": "c", "served_by": "solar", "n": 1}]},
            "answers": [], "consensus": []})
        self.assertIn("回答 3/4 体", text)
        self.assertIn("完全一致", text)
        self.assertIn("別モデルが回答", text)
        self.assertIn("cooling 1", text)

    def test_map_shows_wall_and_speedup(self):
        text = S.render("freeagent_map", {"count": 12, "ok": 12, "failed": 0, "model": "x",
                                         "wall_s": 6.3, "sequential_estimate_s": 24.1,
                                         "speedup": 3.8, "results": []})
        self.assertIn("wall 6.3s", text)
        self.assertIn("3.8 倍", text)


class MapReturnsTiming(unittest.TestCase):
    """`map` が fanout と同じ時間の列を返す（issue #9）。"""

    def test_timing_keys_exist_on_a_dry_run(self):
        orig = S.call_model

        def fake(ref, prompt, **kwargs):
            return {"text": "ok", "latency_s": 0.5, "truncated": False, "tokens": {}}

        S.call_model = fake
        try:
            data = S.tool_map({"items": ["a", "b"], "instruction": "x", "model": "nous/x"})
        finally:
            S.call_model = orig
        for key in ("wall_s", "sequential_estimate_s", "speedup"):
            self.assertIn(key, data)
        self.assertAlmostEqual(data["sequential_estimate_s"], 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
