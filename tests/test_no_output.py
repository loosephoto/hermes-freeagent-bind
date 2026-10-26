"""空応答（`no_output`）の分類と、選抜スコアへの**重み付き**反映（v0.18.0）.

固定する契約:
  * `classify_error("空応答（…）")` は `no_output`（`other` ではない）。他の分類は従来どおり。
  * 統計では `err: {no_output: n}` として**内訳が見える**（ハード失敗と混ざらない）。
  * 品質スコアは `no_output` を**ハード失敗の半分の重み**で減点する（0 ではない）。
    → 「空でない成功のあるモデル」> 「空応答で落ちるモデル」> 「ハード失敗するモデル」の順になる。
  * 空応答 100% のモデルは、同数の成功があるモデルより**必ず下**（選抜が壊れない）。
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-nooutput-test-"))

import freeagent_bind.server as S  # noqa: E402


class TestClassify(unittest.TestCase):
    def test_empty_answer_is_no_output(self):
        cases = [
            "空応答（nous/poolside/laguna-s-2.1:free）。max_tokens=400 を思考トークンで使い切った可能性があります。",
            "empty response from model",
            "no output",
        ]
        for err in cases:
            self.assertEqual(S.classify_error(err), "no_output", err)

    def test_existing_classes_are_unchanged(self):
        """既存の分類を壊していないこと（no_output を先頭に足した副作用の確認）。"""
        self.assertEqual(S.classify_error("HTTP 429: rate limit"), "rate_limited")
        self.assertEqual(S.classify_error("timed out"), "timeout")
        self.assertEqual(S.classify_error("HTTP 404: gone"), "gone")
        self.assertEqual(S.classify_error("HTTP 403: forbidden"), "auth")
        self.assertEqual(S.classify_error("HTTP 503: upstream"), "server")


class TestQualityWeights(unittest.TestCase):
    def row(self, ok=0, n=10, err=None):
        return {"n": n, "ok": ok, "trunc": 0, "leak": 0, "empty": 0, "err": err or {}}

    def test_no_output_is_half_the_penalty_of_a_hard_error(self):
        soft = S._kind_quality(self.row(ok=6, n=10, err={"no_output": 4}))[0]
        hard = S._kind_quality(self.row(ok=6, n=10, err={"gone": 4}))[0]
        clean = S._kind_quality(self.row(ok=10, n=10))[0]
        self.assertLess(clean, 1.01)
        self.assertAlmostEqual(clean, 1.0, places=3)
        self.assertGreater(soft, hard, "空応答はハード失敗より軽いこと")
        self.assertLess(soft, clean, "空応答でも減点はされる（0 にしない）")

    def test_all_no_output_is_zero_because_there_is_no_success(self):
        """減点が半分でも、成功 0 件なら品質は 0（選抜で上に来ない）。"""
        q = S._kind_quality(self.row(ok=0, n=10, err={"no_output": 10}))[0]
        self.assertEqual(q, 0.0)

    def test_half_penalty_applies_when_there_are_successes(self):
        """成功が同数なら、空応答 4 件のモデルはハード失敗 4 件のモデルより上（実測の狙い）。"""
        soft = S._kind_quality(self.row(ok=6, n=10, err={"no_output": 4}))[0]
        self.assertGreater(soft, 0.0)
        self.assertLess(soft, S._kind_quality(self.row(ok=10, n=10))[0])


class TestRanking(unittest.TestCase):
    def seed_stats(self, stats):
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        for ref, row in stats.items():
            S._STATS["models"][ref] = {"kinds": {"ask": row}, "updated_at": S.now_ts()}

    def test_clean_beats_no_output_beats_hard_error(self):
        clean = "nous/clean"
        soft = "nous/soft"
        hard = "nous/hard"
        self.seed_stats({
            clean: {"n": 10, "ok": 10, "err": {}},
            soft: {"n": 10, "ok": 6, "err": {"no_output": 4}},
            hard: {"n": 10, "ok": 6, "err": {"gone": 4}},
        })
        order = S.rank_models([hard, soft, clean])
        self.assertEqual(order, [clean, soft, hard])

    def test_selection_still_avoids_a_model_that_only_returns_empty(self):
        dead = "nous/dead"
        alive = "nous/alive"
        self.seed_stats({
            dead: {"n": 10, "ok": 0, "err": {"no_output": 10}},
            alive: {"n": 10, "ok": 10, "err": {}},
        })
        self.assertEqual(S.rank_models([dead, alive])[0], alive)


class TestObservationRecordsTheClass(unittest.TestCase):
    def setUp(self):
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = False

    def test_no_output_lands_in_its_own_bucket(self):
        S.observe_call({"ref": "nous/x", "error": "空応答（nous/x）。max_tokens=400 を使い切った"},
                       "ask", text="")
        row = S._STATS["models"]["nous/x"]["kinds"]["ask"]
        self.assertEqual(row["n"], 1)
        self.assertEqual(row["err"], {"no_output": 1})
        self.assertEqual(row["ok"], 0)

    def test_success_is_still_success(self):
        S.observe_call({"ref": "nous/y", "error": ""}, "ask", text="42")
        row = S._STATS["models"]["nous/y"]["kinds"]["ask"]
        self.assertEqual(row["ok"], 1)
        self.assertNotIn("no_output", (row.get("err") or {}))


if __name__ == "__main__":
    unittest.main()
