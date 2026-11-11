"""予算超過の検出と確信度の扱い（v0.42.3 / issue #8 #5）.

固定する契約:
  * `max_tokens` を大きく超えて返したモデルを `diagnose` の `budget.models` に出し、注記も出す
    （実測: 64 指定で 931 字・同じ条件 5/5 で逸脱したモデルがあった）。
  * 確信度は**自己申告**であることを明示（`confidence_source="self_report"` /
    `CONFIDENCE_CALIBRATION_NOTE`）。
  * 「確信度 90 以上 + 合意度 0.5 未満」の矛盾だけを警告する（実測: 正解 99.7 / 誤答 100.0）。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


class BudgetDetection(unittest.TestCase):
    def row(self, budget, completion, model="m/x"):
        return {"served_by": model, "max_tokens": budget,
                "tokens": {"prompt": 10, "completion": completion},
                "ok": False, "error": "空応答", "error_class": "no_output", "kind": "panel",
                "ref": model, "ts": S.now_ts()}

    def test_over_budget_models_are_reported(self):
        rows = [self.row(64, 200, "bad/model"), self.row(64, 180, "bad/model"),
                self.row(1024, 300, "good/model")]
        got = S._diag_budget(rows)
        self.assertEqual(got["checked"], 3)
        self.assertEqual(len(got["models"]), 1)
        self.assertEqual(got["models"][0]["model"], "bad/model")
        self.assertEqual(got["models"][0]["n"], 2)
        self.assertAlmostEqual(got["models"][0]["max_ratio"], 200 / 64, places=2)

    def test_single_row_is_not_enough(self):
        got = S._diag_budget([self.row(64, 200, "once/model"), self.row(1024, 100, "ok/model")])
        self.assertEqual(got["models"], [])

    def test_rows_without_budget_or_tokens_are_skipped(self):
        rows = [{"served_by": "a", "max_tokens": None, "tokens": {"completion": 900}},
                {"served_by": "b", "max_tokens": 100, "tokens": {}}]
        got = S._diag_budget(rows)
        self.assertEqual((got["checked"], got["models"]), (0, []))


class ConfidenceContract(unittest.TestCase):
    def test_high_confidence_low_agreement_warns(self):
        text = S.confidence_warning(100, 0.2)
        self.assertIn("合意度 0.2", text)
        self.assertIn("自己申告", text)

    def test_consistent_or_unknown_does_not_warn(self):
        for conf, agree in ((100, 0.8), (70, 0.2), (None, None), (None, 0.1), (95, None)):
            self.assertEqual(S.confidence_warning(conf, agree), "", f"{conf}/{agree}")

    def test_note_names_the_measurement(self):
        self.assertIn("99.7", S.CONFIDENCE_CALIBRATION_NOTE)
        self.assertIn("較正", S.CONFIDENCE_CALIBRATION_NOTE)

    def test_panel_render_shows_the_warning(self):
        text = S.render("freeagent_panel", {
            "answered": 3, "models": ["a", "b", "c"], "independent_sources": 3,
            "agreement": 0.333, "agreement_source": "exact", "confidence_mean": 99.0,
            "confidence_warning": S.confidence_warning(99.0, 0.333),
            "participants": {"requested": 3, "answered": 3, "dropped": []},
            "substitutions": {"count": 0, "ratio": 0.0, "models": []},
            "answers": [], "consensus": []})
        self.assertIn("⚠️", text)
        self.assertIn("自己申告", text)


class DiagnoseWiring(unittest.TestCase):
    """traces に置いた予算超過の行が、diagnose の返り値と content に出る。"""

    def setUp(self):
        self._orig = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-budget-diag-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(self._restore)

    def _restore(self):
        if self._orig is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig

    def test_budget_block_and_note(self):
        path = S.trace_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = []
        for _ in range(2):
            rows.append({"run": "r1", "tool": "freeagent_panel", "kind": "panel",
                         "ref": "openrouter/cohere/x:free", "served_by": "openrouter/cohere/x:free",
                         "ok": False, "error": "空応答", "error_class": "no_output",
                         "max_tokens": 64, "tokens": {"prompt": 10, "completion": 232},
                         "ts": S.now_ts()})
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        data = S.tool_diagnose({"days": 7})
        self.assertEqual(data["budget"]["models"][0]["model"], "openrouter/cohere/x:free")
        self.assertEqual(data["budget"]["models"][0]["n"], 2)
        self.assertTrue(any("max_tokens の" in note for note in data["notes"]))
        text = S.render("freeagent_diagnose", data)
        self.assertIn("予算超過", text)


if __name__ == "__main__":
    unittest.main()
