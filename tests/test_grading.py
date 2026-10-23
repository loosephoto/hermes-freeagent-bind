"""採点と評価指標（`freeagent_bind.grading`）の回帰テスト。

固定する契約:
  * **言及と回答を区別する**: 誤答が本文で正解値を言及していても合格にしない（旧実装の誤り）
  * 否定文（``not silver``）や部分一致で合格にしない
  * LaTeX 分数・``%``・単位・桁区切り・区間/集合を正規化して厳密に比べる
  * 最終回答が無い回答は ``no_answer``（誤答と区別する＝切り詰めを誤答に変換しない）
  * 連続指標（クラスタ熵・一致率・Brier/ECE）と信頼区間は seed 固定で再現できる
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import grading as G  # noqa: E402

NUM = {"id": "n", "kind": "numeric", "expect": ["4913"]}
TXT = {"id": "t", "kind": "text", "expect": ["silver", "銀"]}
SET = {"id": "s", "kind": "set", "expect": ["[-6,-3)"]}
FRAC = {"id": "f", "kind": "exact", "expect": ["30/7"]}


class TestExtraction(unittest.TestCase):
    def test_final_answer_line(self):
        answer, how = G.extract_final_answer("some reasoning\nFinal answer: 630")
        self.assertEqual((answer, how), ("630", "final_line"))

    def test_japanese_label(self):
        answer, _ = G.extract_final_answer("計算しました。\n最終回答: 630")
        self.assertEqual(answer, "630")

    def test_last_label_wins(self):
        # モデルは途中で言い直すので**最後**のラベルを採る
        answer, _ = G.extract_final_answer("Final answer: 400\nWait, correction.\nFinal answer: 630")
        self.assertEqual(answer, "630")

    def test_boxed(self):
        answer, how = G.extract_final_answer("so $\\boxed{30/7}$ is the value")
        self.assertEqual((answer, how), ("30/7", "boxed"))

    def test_last_line_fallback(self):
        answer, how = G.extract_final_answer("The number of good items is 630.")
        self.assertEqual((answer, how), ("630", "last_line"))

    def test_label_inside_latex_text_keeps_the_value(self):
        # 実測: \text{Final answer: [-4, 0)} → 閉じ括弧が 1 つ余った
        text = "Thus, the final answer is:\n\n\\[\n\\text{Final answer: [-4, 0)}\n\\]"
        answer, how = G.extract_final_answer(text)
        self.assertEqual(how, "final_line")
        self.assertEqual(answer, "[-4, 0)")
        self.assertTrue(G.grade(text, {"kind": "set", "expect": ["[-4,0)"]})["ok"])

    def test_empty_label_falls_back(self):
        # 実測: \text{Final answer: } \] → 中身が無いラベル。ゴミを答えにしない。
        text = "we get \\([-4, 0)\\).\n\n\\[\n\\text{Final answer: } \\]"
        answer, how = G.extract_final_answer(text)
        self.assertFalse(how == "final_line" and answer in (")", "}", "\\"),
                         f"ラベルの残骸を答えにした: {answer!r}")

    def test_no_answer_is_distinct(self):
        # 切り詰められた長文（最終行に値が無い）→ no_answer。誤答ではない。
        text = ("Let me solve this step by step. First 35 * 24 = 840, then 25% of 840 is 210, "
                "so good items = 840 - 210 = 630. Checking: 210 * 4 = 840 which confirms the "
                "defective fraction is exactly one quarter of the total, so the remaining "
                "items number six hundred and thirty in total")
        graded = G.grade(text, NUM)
        self.assertEqual(graded["verdict"], "no_answer")
        self.assertFalse(graded["ok"])

    def test_needs_retry(self):
        self.assertTrue(G.needs_retry("no value here at all"))
        self.assertFalse(G.needs_retry("Final answer: 630"))

    def test_truncated_text_is_not_guessed(self):
        # 実測: 途中で切れた推論の末尾の数値を拾って「7」「11」を誤答にしていた。
        text = "We need n >= 1000 with n ≡ 3 (mod 7) and n ≡ 1 (mod 11). Solving 3 + 7k"
        graded = G.grade(text, {"kind": "numeric", "expect": ["1044"]}, truncated=True)
        self.assertEqual(graded["verdict"], "no_answer")
        self.assertIsNone(graded["answer"])

    def test_truncated_but_labeled_answer_survives(self):
        text = "long reasoning that was cut off\nFinal answer: 1044"
        graded = G.grade(text, {"kind": "numeric", "expect": ["1044"]}, truncated=True)
        self.assertEqual(graded["verdict"], "correct")

    def test_long_prose_without_label_is_not_guessed(self):
        # 200 字を超える本文の末尾の数値を答えと見なさない（"7" 誤答の再発防止）
        text = ("Let me work through this carefully. The conditions are n ≡ 3 (mod 7) and "
                "n ≡ 1 (mod 11), so n = 3 + 7k and 3 + 7k ≡ 1 (mod 11), giving 7k ≡ 9, and "
                "since the inverse of 7 mod 11 is 8 we get k ≡ 6, so candidates are spaced by 77 "
                "and the smallest one above 1000 works out to 7")
        graded = G.grade(text, {"kind": "numeric", "expect": ["1044"]})
        self.assertEqual(graded["verdict"], "no_answer", "長文の末尾の数値を回答として拾った")


class TestNoFalsePositives(unittest.TestCase):
    """**旧採点の誤判定 4 件**を回帰テストとして固定する（実測で見つけたもの）。"""

    def test_mention_of_correct_number_is_not_an_answer(self):
        text = "The cube of 17 is 4913? No - I compute 2900. Final answer: 2900"
        graded = G.grade(text, NUM)
        self.assertEqual(graded["answer"], "2900")
        self.assertFalse(graded["ok"], "正解値を言及しただけの誤答を合格にした")

    def test_negated_number(self):
        text = "It is definitely not 4913, the answer is 2900."
        self.assertFalse(G.grade(text, NUM)["ok"])

    def test_negated_word(self):
        text = "This is not silver, it is mercury."
        graded = G.grade(text, TXT)
        self.assertFalse(graded["ok"], "否定文の部分一致で合格にした")

    def test_partial_match_inside_another_word(self):
        graded = G.grade("The silversmith works with mercury. Final answer: mercury", TXT)
        self.assertFalse(graded["ok"])
        self.assertEqual(graded["answer"], "mercury")

    def test_correct_answers_still_pass(self):
        for text, case, want in [
            ("Final answer: 4913", NUM, "4913"),
            ("Final answer: 4913.0 units", NUM, "4913.0 units"),
            ("Final answer: 4,913", NUM, "4,913"),
            ("最終回答: 4913", NUM, "4913"),
            ("Final answer: silver", TXT, "silver"),
            ("Final answer: 銀", TXT, "銀"),
        ]:
            graded = G.grade(text, case)
            self.assertTrue(graded["ok"], f"{text!r} を落とした")
            self.assertEqual(graded["answer"], want)


class TestNormalization(unittest.TestCase):
    def test_latex_fraction_forms(self):
        self.assertTrue(G.values_equal("\\frac{30}{7}", "30/7"))
        self.assertTrue(G.values_equal("$\\frac{30}{7}$", "30/7"))
        self.assertTrue(G.values_equal("30 / 7", "30/7"))

    def test_fraction_is_exact(self):
        # kind="exact"（分数問題）は厳密一致。1/3 と 0.333 を通してはいけない。
        self.assertFalse(G.values_equal("1/3", "0.333", kind="exact"))
        self.assertTrue(G.values_equal("1/3", "2/6", kind="exact"))
        self.assertTrue(G.values_equal("30/7", "\\frac{30}{7}", kind="exact"))
        # 数値問題は許容誤差つき（旧実装と同じ閾値）
        self.assertTrue(G.values_equal("1/3", "0.333", kind="numeric"))

    def test_percent(self):
        self.assertTrue(G.values_equal("10%", "0.1"))
        self.assertFalse(G.values_equal("10%", "10"))

    def test_units_and_thousands(self):
        self.assertTrue(G.values_equal("4913.0 units", "4913"))
        self.assertTrue(G.values_equal("1,234 items", "1234"))
        self.assertTrue(G.values_equal("2 km", "2"))

    def test_sets_are_order_independent(self):
        self.assertTrue(G.values_equal("{1,2,3,4}", "4,3,2,1"))
        self.assertTrue(G.values_equal("\\{2, 4, 6\\}", "2,4,6"))
        self.assertFalse(G.values_equal("{100,200,300}", "100200"))

    def test_interval_equals_integer_set(self):
        case = {"kind": "set", "expect": ["[-6,-3)"]}
        self.assertTrue(G.grade("Final answer: [-6,-3)", case)["ok"])
        self.assertTrue(G.grade("Final answer: {-6,-5,-4}", case)["ok"])
        self.assertFalse(G.grade("Final answer: [-6,-4)", case)["ok"])
        self.assertFalse(G.grade("Final answer: (-6,-3)", case)["ok"])

    def test_empty_set(self):
        case = {"kind": "set", "expect": ["empty"]}
        for text in ("Final answer: empty", "Final answer: \\emptyset", "最終回答: なし"):
            self.assertTrue(G.grade(text, case)["ok"], text)

    def test_tolerance(self):
        case = {"kind": "numeric", "expect": ["630"]}
        self.assertTrue(G.grade("Final answer: 630.2", case)["ok"],
                        "許容誤差 0.5 以内の丸めは合格（旧実装と同じ閾値）")
        self.assertFalse(G.grade("Final answer: 610", case)["ok"],
                         "20 の差を許容誤差で通してはいけない")
        self.assertFalse(G.grade("Final answer: 540", case)["ok"])


class TestMetrics(unittest.TestCase):
    def test_cluster_entropy_and_agreement(self):
        same = ["Final answer: 630", "The answer is 630", "630"]
        self.assertEqual(G.cluster_entropy(same), 0.0)
        self.assertEqual(G.agreement_rate(same), 1.0)
        split = ["Final answer: 630", "Final answer: 630", "Final answer: 540"]
        self.assertAlmostEqual(G.agreement_rate(split), 0.667, places=3)
        self.assertGreater(G.cluster_entropy(split), 0.0)
        self.assertIsNone(G.cluster_entropy(["Final answer: 630"]))

    def test_entropy_matches_known_value(self):
        # 4 件が 2 種類に 2:2 → 1 bit
        texts = ["Final answer: 1", "Final answer: 1", "Final answer: 2", "Final answer: 2"]
        self.assertAlmostEqual(G.cluster_entropy(texts), 1.0, places=4)

    def test_brier_and_ece(self):
        # 自信 100% で正解 2 件・誤り 2 件 → Brier 0.5、ECE 0.5
        pairs = [(1.0, True), (1.0, True), (1.0, False), (1.0, False)]
        self.assertAlmostEqual(G.brier(pairs), 0.5, places=4)
        self.assertAlmostEqual(G.ece(pairs), 0.5, places=4)
        self.assertIsNone(G.brier([]))

    def test_mcnemar_exact(self):
        self.assertEqual(G.mcnemar_exact_p(0, 0), 1.0)
        self.assertLess(G.mcnemar_exact_p(10, 0), 0.01)

    def test_bootstrap_ci_is_deterministic(self):
        values = [1.0] * 10 + [0.0] * 10
        first = G.bootstrap_ci(values)
        self.assertEqual(first, G.bootstrap_ci(values), "seed 固定で再現しない")
        self.assertLess(first[0], first[1])
        self.assertIsNone(G.bootstrap_ci([1.0]))

    def test_paired_summary_reports_no_difference(self):
        rows = []
        for case in ("a", "b", "c"):
            for name in ("single", "panel"):
                rows.append({"case": case, "repeat": 1, "strategy": name, "ok": True,
                             "no_answer": False, "cluster_entropy": 0.0})
        out = G.paired_summary(rows, baseline="single")
        self.assertEqual(out["panel"]["accuracy_diff"], 0.0)
        self.assertEqual(out["panel"]["verdict"], "差を検出できない")
        self.assertTrue(out["single"]["baseline"])

    def test_paired_summary_detects_consistent_gain(self):
        rows = []
        for i in range(40):
            rows.append({"case": f"c{i}", "repeat": 1, "strategy": "single", "ok": i % 2 == 0,
                         "no_answer": False})
            rows.append({"case": f"c{i}", "repeat": 1, "strategy": "panel", "ok": True,
                         "no_answer": False})
        out = G.paired_summary(rows, baseline="single")
        self.assertGreater(out["panel"]["accuracy_diff"], 0)
        self.assertEqual(out["panel"]["verdict"], "有意（CI が 0 を跨がない）")
        self.assertEqual(out["panel"]["no_answer_diff"], 0.0)


class TestVerdictCounts(unittest.TestCase):
    def test_counts_and_rates(self):
        counts = G.verdict_counts([{"verdict": "correct"}, {"verdict": "wrong"},
                                   {"verdict": "no_answer"}, {"verdict": "correct"}])
        self.assertEqual(counts["correct"], 2)
        self.assertEqual(counts["wrong"], 1)
        self.assertEqual(counts["no_answer"], 1)
        self.assertEqual(counts["accuracy"], 0.5)
        self.assertEqual(counts["no_answer_rate"], 0.25)


if __name__ == "__main__":
    unittest.main()