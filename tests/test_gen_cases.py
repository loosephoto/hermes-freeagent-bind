"""自前生成の難問ケース（`scripts/gen_cases.py`）の回帰テスト。

固定する契約:
  * **決定的**: 同じ seed・同じ件数なら同じケース（対応のある比較が崩れない）
  * **独立検算が通る**: 生成とは別の経路で再計算した答えが一致する（期待値の写し間違いが無い）
  * **帯は事前登録**: 帯 1/2/3 が同数・各ケースに tier が付く
  * id は一意（件数を増やしても衝突しない）
  * どの問いも「Final answer:」で終わる＝抽出できる形を強制している
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import gen_cases  # noqa: E402


class TestGenerator(unittest.TestCase):
    def test_deterministic(self):
        first = gen_cases.build(per_tier=4, seed=20261024)
        second = gen_cases.build(per_tier=4, seed=20261024)
        self.assertEqual([c["id"] for c in first], [c["id"] for c in second])
        self.assertEqual([c["expect"] for c in first], [c["expect"] for c in second])
        other = gen_cases.build(per_tier=4, seed=1)
        self.assertNotEqual([c["id"] for c in first], [c["id"] for c in other])

    def test_independent_verification_holds(self):
        # 複数の seed で検算（生成とは別経路の再計算）
        for seed in (1, 7, 99, 20261024):
            for case in gen_cases.build(per_tier=6, seed=seed):
                ok, detail = gen_cases.verify(case)
                self.assertTrue(ok, f"{case['id']} の検算が不一致: {detail}")

    def test_tier_composition_is_preregistered(self):
        cases = gen_cases.build(per_tier=5, seed=3)
        counts = {}
        for case in cases:
            counts[case["tier"]] = counts.get(case["tier"], 0) + 1
        self.assertEqual(counts, {1: 5, 2: 5, 3: 5})
        self.assertEqual(len(cases), 15)

    def test_ids_unique_with_many_cases(self):
        cases = gen_cases.build(per_tier=12, seed=11)
        self.assertEqual(len({c["id"] for c in cases}), len(cases))

    def test_questions_are_extractable_shape(self):
        for case in gen_cases.build(per_tier=2, seed=8):
            self.assertIn("Final answer:", case["question"])
            self.assertIn("params", case)
            self.assertTrue(case["expect"], case["id"])
            self.assertTrue(case["traps"], case["id"])

    def test_answers_are_exact_strings(self):
        # 期待値は正準形（採点側の正規化で一致する形）で持つ
        for case in gen_cases.build(per_tier=3, seed=42):
            self.assertIsInstance(case["expect"][0], str)
            self.assertNotIn(" ", case["expect"][0])


class TestVerifierItself(unittest.TestCase):
    def test_verifier_catches_a_wrong_expectation(self):
        # 検算が「壊れた期待値」を実際に落とせること（検算機能そのものの試験）
        case = gen_cases.build(per_tier=1, seed=5)[0]
        broken = dict(case, expect=["999999999"])
        ok, detail = gen_cases.verify(broken)
        self.assertFalse(ok)
        self.assertIn("999999999", detail)

    def test_unknown_family_is_reported(self):
        ok, detail = gen_cases.verify({"family": "nope", "expect": ["1"], "params": {}})
        self.assertFalse(ok)
        self.assertIn("未知の family", detail)


if __name__ == "__main__":
    unittest.main()