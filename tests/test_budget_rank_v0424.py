"""予算超過を統計に入れて選抜で降格できる（v0.42.4 / issue #8 の発展）.

固定する契約:
  * `note_observation(..., over_budget=True)` が `model_stats` の kind 行に `over_budget` を足す
    （成功・失敗とは独立に数える）。
  * `observe_call` は結果（`max_tokens` と `tokens.completion`）から超過を判定して記録する。
  * 選抜の降格は **既定 off**（`FREEAGENT_RANK_BUDGET`）。off では順位を 1 ミリも変えない。
  * on では観測のうち超過した割合だけ引く（上限 0.20・2 件未満は効かせない）。
  * `diagnose` の advice が「空応答」のときに予算超過モデルを名指しする。
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


class _State(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-budget-rank-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(self._restore)
        self._reset_stats()
        self._orig_flag = S.RANK_BUDGET
        self.addCleanup(lambda: setattr(S, "RANK_BUDGET", self._orig_flag))

    def _restore(self):
        if self._orig is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig

    def _reset_stats(self):
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = False
        S._STATS_MTIME = None


class OverBudgetColumn(_State):
    def test_flag_lands_in_the_kind_row(self):
        S.note_observation("m/x", "consult", over_budget=True, lat_ms=10.0)
        row = S._STATS["models"]["m/x"]["kinds"]["consult"]
        self.assertEqual(row["n"], 1)
        self.assertEqual(row["over_budget"], 1)
        # 超過は品質（ok）とは独立。成功したが予算を超えた呼び出しは成功のまま数え、
        # 降格は `_budget_penalty` 側で行う（二重減点を避ける）。
        self.assertEqual(row["ok"], 1)

    def test_flag_is_independent_of_success(self):
        S.note_observation("m/y", "consult", lat_ms=10.0)
        S.note_observation("m/y", "consult", over_budget=True, lat_ms=10.0)
        row = S._STATS["models"]["m/y"]["kinds"]["consult"]
        self.assertAlmostEqual(row["n"], 2, places=6)
        self.assertAlmostEqual(row["ok"], 2, places=6, msg="超過しても成功は成功のまま")
        self.assertAlmostEqual(row["over_budget"], 1, places=6)

    def test_detection_from_a_result(self):
        self.assertTrue(S._is_over_budget({"max_tokens": 64, "tokens": {"completion": 232}}))
        self.assertFalse(S._is_over_budget({"max_tokens": 512, "tokens": {"completion": 400}}))
        self.assertFalse(S._is_over_budget({"max_tokens": None, "tokens": {"completion": 400}}))
        self.assertFalse(S._is_over_budget({}))


class RankingPenalty(_State):
    def _seed(self, ref, over, normal):
        for _ in range(normal):
            S.note_observation(ref, "panel", lat_ms=100.0)
        for _ in range(over):
            S.note_observation(ref, "panel", over_budget=True, lat_ms=100.0)

    def test_off_by_default_changes_nothing(self):
        S.RANK_BUDGET = False
        self._seed("m/over", over=3, normal=1)
        self.assertEqual(S._budget_penalty("m/over"), 0.0)

    def test_on_uses_the_ratio_with_a_cap(self):
        S.RANK_BUDGET = True
        self._seed("m/over", over=3, normal=1)
        self.assertAlmostEqual(S._budget_penalty("m/over"), 0.20, places=4)  # 3/4 は上限で頭打ち

    def test_minimum_two_observations(self):
        S.RANK_BUDGET = True
        self._seed("m/once", over=1, normal=0)
        self.assertEqual(S._budget_penalty("m/once"), 0.0)

    def test_ranking_prefers_the_model_that_respects_the_budget(self):
        S.RANK_BUDGET = True
        self._seed("a-good", over=0, normal=4)
        self._seed("z-bad", over=2, normal=2)
        order = S.rank_models(["z-bad", "a-good"])
        self.assertEqual(order[0], "a-good")


class AdviceMentionsBudget(_State):
    def test_empty_output_advice_points_at_the_over_budget_model(self):
        path = S.trace_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        rows = []
        for _ in range(3):
            rows.append({"run": "r9", "tool": "freeagent_panel", "kind": "panel",
                         "ref": "openrouter/cohere/x:free", "served_by": "openrouter/cohere/x:free",
                         "ok": False, "error": "空応答", "error_class": "no_output",
                         "max_tokens": 64, "tokens": {"completion": 240}, "ts": S.now_ts()})
        for _ in range(4):
            rows.append({"run": "r9", "tool": "freeagent_panel", "kind": "panel",
                         "ref": "good/model", "served_by": "good/model", "ok": True,
                         "max_tokens": 512, "tokens": {"completion": 300}, "ts": S.now_ts()})
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        data = S.tool_diagnose({"days": 7})
        self.assertEqual(data["advice"]["kind"], "empty_output")
        self.assertIn("FREEAGENT_RANK_BUDGET", data["advice"]["check"])
        self.assertIn("openrouter/cohere/x:free", data["advice"]["advice"])


if __name__ == "__main__":
    unittest.main()
