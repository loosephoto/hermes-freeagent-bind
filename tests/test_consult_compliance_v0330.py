"""v0.33.0: consult の 2 段査読（仕様適合 → 品質）。ネット不要。"""
import os
import tempfile
import unittest
from unittest import mock

from freeagent_bind import server as S

REFS = ["nous/a", "nous/b"]


def _rows(texts):
    return [{"ref": f"nous/{chr(97 + i)}", "served_by": f"nous/{chr(97 + i)}", "text": t}
            for i, t in enumerate(texts)]


COMPLIANT = "結論: 要件は満たしています\n判定: compliant\n指摘: なし\nメインに確認したい点: なし"
NONCOMPLIANT = "結論: 受け入れ条件が未実装\n判定: noncompliant\n指摘: [R2] cancel — 未実装\nメインに確認したい点: なし"
QUALITY_NIT = "結論: 軽微な指摘のみ\n重要度: nit\n指摘: [readability] 命名 — 誤解を招く\nメインに確認したい点: なし"


class TwoStageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-2stage-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, plan=None, **args):
        """plan: system オブジェクトの id → 返すテキスト行。呼ばれた system を順に記録する。"""
        calls = []
        plan = plan or {}

        def fake_ask(refs, prompt, **kwargs):
            system = kwargs.get("system")
            calls.append(system)
            texts = plan.get(id(system), plan.get("default"))
            return _rows(texts * len(refs) if texts else [])

        with mock.patch.object(S, "_select_or_error", return_value=(REFS, {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_consult(dict(args, **{}))
        return got, calls

    def test_without_basis_is_single_quality_stage(self):
        got, calls = self._run(plan={"default": [QUALITY_NIT]},
                               question="Q", mode="review", draft="D")
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0], S.REVIEW_SYSTEM)
        self.assertEqual(got["stages"], ["quality"])
        self.assertIsNone(got["compliance_summary"])

    def test_with_basis_runs_compliance_then_quality(self):
        got, calls = self._run(plan={id(S.COMPLIANCE_SYSTEM): [COMPLIANT],
                                     id(S.REVIEW_SYSTEM): [QUALITY_NIT]},
                               question="Q", mode="review", draft="D", basis="R1/R2")
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[0], S.COMPLIANCE_SYSTEM)
        self.assertIs(calls[1], S.REVIEW_SYSTEM)
        self.assertEqual(got["stages"], ["compliance", "quality"])
        self.assertEqual(got["compliance_summary"]["compliant"], 2)
        self.assertFalse(got["compliance_summary"]["blocking"])
        self.assertEqual(len(got["compliance"]), 2)

    def test_noncompliant_blocks_even_if_quality_is_light(self):
        got, _ = self._run(plan={id(S.COMPLIANCE_SYSTEM): [NONCOMPLIANT],
                                 id(S.REVIEW_SYSTEM): [QUALITY_NIT]},
                           question="Q", mode="review", draft="D", basis="R1/R2")
        self.assertTrue(got["compliance_summary"]["blocking"])
        self.assertTrue(got["findings_summary"]["blocking"], "適合の blocking が品質側に伝播する")
        self.assertTrue(got["findings_summary"]["compliance_blocking"])

    def test_missing_verdict_counts_as_unknown_and_blocks(self):
        got, _ = self._run(plan={id(S.COMPLIANCE_SYSTEM): ["結論: たぶん大丈夫\n指摘: なし"],
                                 id(S.REVIEW_SYSTEM): [QUALITY_NIT]},
                           question="Q", mode="review", draft="D", basis="R1")
        self.assertEqual(got["compliance_summary"]["unknown"], 2)
        self.assertTrue(got["compliance_summary"]["blocking"])

    def test_basis_without_draft_falls_back_to_discuss(self):
        got, calls = self._run(plan={"default": ["結論: OK\n確信度: 80\nメインに確認したい点: なし"]},
                               question="Q", mode="review", basis="R1")
        self.assertIs(calls[0], S.CONSULT_SYSTEM)
        self.assertEqual(got["stages"], ["discuss"])
        self.assertIsNone(got["compliance_summary"])

    def test_resume_reuses_stored_compliance_round(self):
        texts = {id(S.COMPLIANCE_SYSTEM): [COMPLIANT], id(S.REVIEW_SYSTEM): [QUALITY_NIT]}
        first, calls = self._run(plan=texts, question="Q", mode="review", draft="D", basis="R1")
        self.assertEqual(len(calls), 2)

        # 2 回目（再開）: 適合ラウンドは走らず、品質だけ 1 回
        calls2 = []

        def fake_ask(refs, prompt, **kwargs):
            calls2.append(kwargs.get("system"))
            return _rows([QUALITY_NIT] * len(refs))

        with mock.patch.object(S, "_select_or_error", return_value=(REFS, {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            second = S.tool_consult({"session_id": first["session_id"], "main_reply": "R2 は来週対応"})
        self.assertEqual(len(calls2), 1, "保存済みの適合結果を使い回す")
        self.assertIs(calls2[0], S.REVIEW_SYSTEM)
        self.assertEqual(second["compliance_summary"]["reviewed"], 2)

    def test_render_shows_compliance_line(self):
        got, _ = self._run(plan={id(S.COMPLIANCE_SYSTEM): [NONCOMPLIANT],
                                 id(S.REVIEW_SYSTEM): [QUALITY_NIT]},
                           question="Q", mode="review", draft="D", basis="R1/R2")
        text = S.render("freeagent_consult", got)
        self.assertIn("適合: compliant 0 / partial 0 / noncompliant 2", text)
        self.assertIn("blocking", text)


if __name__ == "__main__":
    unittest.main()
