"""多数派シェア（確信度の代理）と probe の予算遵守（v0.47.0 / issue #5 案3・#8 案2）.

固定する契約:
  * `consensus_share` は**最大クラスタの人数 / 参加者数**。短答は完全一致、長文は類似度 0.55 以上の
    単連結クラスタ。自己申告の `confidence` の代わりに「割れていないか」を見るための値。
  * `agreement_detail` は `share` / `clusters` も返す（既存の `value` / `votes` / `source` は不変）。
  * probe の各結果は `budget {asked, effective, completion, chars, respected}` を持ち、
    `probe_budget` に「検査した数」と「守らなかったモデル」を名指しで返す。
    **比較するのは実効の予算**（リトライで引き上げた後）。
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


class ConsensusShare(unittest.TestCase):
    def test_short_answers_use_exact_votes(self):
        got = S.consensus_share(["C", "C", "B"])
        self.assertEqual(got["share"], 0.667)
        self.assertEqual(got["clusters"], 2)
        self.assertEqual(got["source"], "exact")

    def test_short_answers_ignore_confidence_line(self):
        got = S.consensus_share(["C\n確信度: 100", "C\n確信度: 40", "B"])
        self.assertEqual(got["share"], 0.667)

    def test_long_answers_cluster_by_similarity(self):
        got = S.consensus_share([
            "東京は日本の首都です。",
            "東京は日本の首都である。",
            "大阪は西日本の中心です。",
        ])
        self.assertEqual(got["source"], "similarity")
        self.assertEqual(got["clusters"], 2)
        self.assertEqual(got["share"], 0.667)

    def test_single_participant_has_no_share(self):
        got = S.consensus_share(["C"])
        self.assertIsNone(got["share"])
        self.assertEqual(got["n"], 1)

    def test_unanimous_is_one(self):
        self.assertEqual(S.consensus_share(["29", "29", "29"])["share"], 1.0)


class AgreementDetailContract(unittest.TestCase):
    def test_keeps_old_keys_and_adds_share(self):
        got = S.agreement_detail(["C", "C", "B"])
        for key in ("value", "source", "votes", "n", "share", "clusters"):
            self.assertIn(key, got)
        self.assertEqual(got["value"], 0.667)
        self.assertEqual(got["share"], 0.667)

    def test_too_few_answers(self):
        got = S.agreement_detail(["C"])
        self.assertEqual(got["source"], "none")
        self.assertIsNone(got["share"])

    def test_note_says_it_is_a_proxy(self):
        self.assertIn("代理", S.consensus_note())


class ProbeBudgetContract(unittest.TestCase):
    """probe の予算判定は**実効の予算**で行う（v0.42.5 の訂正を壊さない）。"""

    def setUp(self):
        self.src = open(os.path.join(ROOT, "src", "freeagent_bind", "tools_core.py"),
                        encoding="utf-8").read()

    def test_budget_dict_has_the_five_fields(self):
        tree = ast.parse(self.src)
        found = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                if {"asked", "effective", "completion", "chars", "respected"} <= keys:
                    found = keys
                    break
        self.assertIsNotNone(found, "budget の 5 キーがありません")

    def test_compares_against_effective_budget(self):
        self.assertIn('res.get("max_tokens") or probe_tokens', self.src)
        self.assertIn("_comp <= int(_eff)", self.src)

    def test_names_the_offenders(self):
        self.assertIn("not_respected", self.src)
        self.assertIn("probe_budget", self.src)

    def test_panel_and_consult_expose_consensus(self):
        for rel in ("tools_core.py", "answers.py"):
            body = open(os.path.join(ROOT, "src", "freeagent_bind", rel), encoding="utf-8").read()
            self.assertIn('"consensus_share"', body, rel)


if __name__ == "__main__":
    unittest.main()
