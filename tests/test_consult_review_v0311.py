"""v0.31.1: consult の査読（5 軸＋重要度）と質問の上限・既定値・カバレッジ。ネットワーク不要。"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


def _rows(texts):
    return [{"ref": f"nous/{chr(97 + i)}", "served_by": f"nous/{chr(97 + i)}", "text": t}
            for i, t in enumerate(texts)]


class ReviewParseTest(unittest.TestCase):
    def test_parse_review_maps_severity_and_axes(self):
        got = S.parse_review(
            "結論: 概ね良い\n重要度: Critical\n"
            "指摘: [security] token をログに出しています — 漏洩\n"
            "メインに確認したい点: なし")
        self.assertEqual(got["severity"], "critical")
        self.assertEqual(len(got["findings"]), 1)
        self.assertIn("security", got["findings"][0])
        self.assertIn("security", got["axes"])

    def test_japanese_severity_is_normalised(self):
        self.assertEqual(S.parse_review("重要度: 重大")["severity"], "critical")
        self.assertEqual(S.parse_review("重要度: 軽微")["severity"], "nit")
        self.assertEqual(S.parse_review("重要度: 必須")["severity"], "required")

    def test_out_of_vocabulary_severity_stays_unknown_and_blocks(self):
        parsed = S.parse_review("重要度: 要検討")
        self.assertEqual(parsed["severity"], "unknown")
        summary = S.summarize_review([{"severity": parsed["severity"]}])
        self.assertEqual(summary["unknown"], 1)
        self.assertTrue(summary["blocking"], "語彙外は安全側に倒す（黙って通さない）")

    def test_missing_severity_blocks_because_it_was_requested(self):
        # 査読の system は重要度の表明を要求している。名乗らない＝「軽い」ではなく「不明」。
        summary = S.summarize_review([{"severity": ""}, {"conclusion": "x"}])
        self.assertTrue(summary["blocking"])
        self.assertEqual(summary["unknown"], 2)
        self.assertEqual(summary["reviewed"], 2)

    def test_failed_rows_do_not_count_as_reviewed(self):
        summary = S.summarize_review([{"error": "HTTP 429"}, {"severity": "nit"}])
        self.assertEqual(summary["reviewed"], 1)
        self.assertEqual(summary["unreviewed"], 1)
        self.assertFalse(summary["blocking"])


class ConsultReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-consult-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_review_mode_uses_review_system_and_returns_summary(self):
        seen = {}

        def fake_ask(refs, prompt, **kwargs):
            seen["system"] = kwargs.get("system")
            seen["max_tokens"] = kwargs.get("max_tokens")
            return _rows(["結論: OK\n重要度: required\n指摘: [correctness] 境界値 — 未処理\n"
                          "メインに確認したい点: なし"] * 2)

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "nous/b"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_consult({"question": "Q", "mode": "review", "draft": "下書き"})
        self.assertIs(seen["system"], S.REVIEW_SYSTEM)
        self.assertEqual(seen["max_tokens"], 800, "査読は 4 行を要求するので既定を上げる")
        self.assertEqual(got["findings_summary"]["required"], 2)
        self.assertTrue(got["findings_summary"]["blocking"])
        self.assertEqual(got["review"][0]["severity"], "required")

    def test_truncated_answers_are_visible(self):
        def fake_ask(refs, prompt, **kwargs):
            return [{"ref": refs[0], "served_by": refs[0], "truncated": True,
                     "text": "結論: OK\n重要度: critical"}]

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_consult({"question": "Q", "mode": "review", "draft": "下書き"})
        self.assertEqual(got["findings_summary"]["truncated"], 1)
        self.assertTrue(got["review"][0]["truncated"])
        self.assertIn("切り詰められた回答 1 件", S.render("freeagent_consult", got))

    def test_discuss_mode_keeps_consult_system_and_no_summary(self):
        seen = {}

        def fake_ask(refs, prompt, **kwargs):
            seen["system"] = kwargs.get("system")
            return _rows(["結論: OK\n確信度: 80\nメインに確認したい点: なし"] * 2)

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "nous/b"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_consult({"question": "Q"})
        self.assertIs(seen["system"], S.CONSULT_SYSTEM)
        self.assertIsNone(got["findings_summary"])
        self.assertEqual(got["review"], [])

    def test_review_without_draft_falls_back_to_discuss_system(self):
        seen = {}

        def fake_ask(refs, prompt, **kwargs):
            seen["system"] = kwargs.get("system")
            return _rows(["結論: OK\n確信度: 80\nメインに確認したい点: なし"])

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            S.tool_consult({"question": "Q", "mode": "review"})
        self.assertIs(seen["system"], S.CONSULT_SYSTEM)


class QuestionBudgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-budget-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _ask_with(self, questions):
        texts = [f"結論: {q}\n確信度: 70\nメインに確認したい点: {q}" for q in questions]

        def fake_ask(refs, prompt, **kwargs):
            return _rows(texts[:len(refs)])

        return mock.patch.object(S, "ask_many", side_effect=fake_ask)

    def test_questions_are_capped_and_deferred_are_not_dropped(self):
        qs = [f"質問{i} は？" for i in range(1, 8)]
        with mock.patch.object(S, "_select_or_error",
                               return_value=([f"nous/m{i}" for i in range(7)], {})), \
             self._ask_with(qs):
            got = S.tool_consult({"question": "Q"})
        self.assertEqual(len(got["open_questions_for_main"]), S.QUESTION_CAP)
        self.assertEqual(len(got["deferred_questions"]), 7 - S.QUESTION_CAP)
        self.assertEqual(got["question_budget"], {"cap": S.QUESTION_CAP, "open": 5, "deferred": 2})
        self.assertEqual(got["coverage"]["deferred"], 2)

    def test_default_is_split_from_the_question(self):
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             self._ask_with(["予算は？既定: 30 秒"]):
            got = S.tool_consult({"question": "Q"})
        self.assertEqual(got["open_questions"][0]["default"], "30 秒")
        self.assertIn("予算は？", got["open_questions"][0]["question"])

    def test_resume_marks_answered_questions_as_resolved(self):
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             self._ask_with(["予算は？"]):
            first = S.tool_consult({"question": "Q"})
        self.assertEqual(first["coverage"]["status"], "outstanding")

        def quiet(refs, prompt, **kwargs):
            return _rows(["結論: 了解\n確信度: 80\nメインに確認したい点: なし"] * len(refs))

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many", side_effect=quiet):
            second = S.tool_consult({"session_id": first["session_id"], "main_reply": "30 秒です"})
        self.assertEqual(second["coverage"]["status"], "clear")
        self.assertIn("予算は？", second["coverage"]["resolved"])
        self.assertIn("表層一致", second["coverage"]["note"])
        self.assertEqual(second["open_questions_for_main"], [])

    def test_render_shows_severity_and_question_lines(self):
        data = {"session_id": "s1", "mode": "review", "rounds_run": 1, "stage": "complete",
                "agreement": 1.0, "confidence_mean": 80, "consensus": [],
                "failed": [], "open_questions_for_main": ["予算は？"],
                "open_questions": [{"model": "m", "question": "予算は？", "default": "30 秒"}],
                "deferred_questions": [], "findings_summary": {"critical": 0, "required": 1,
                                                               "optional": 0, "nit": 2,
                                                               "unknown": 0, "blocking": True},
                "coverage": {"status": "outstanding", "resolved": [], "outstanding": 1, "deferred": 0}}
        text = S.render("freeagent_consult", data)
        self.assertIn("重要度: critical 0 / required 1", text)
        self.assertIn("blocking", text)
        self.assertIn("既定: 30 秒", text)
        self.assertIn("未回答 1", text)


if __name__ == "__main__":
    unittest.main()
