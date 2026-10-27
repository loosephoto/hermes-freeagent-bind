"""v0.28.0 の契約: 統合役（refine）は**品質の帯の中だけ**速さで選ぶ.

`panel(refine)` の壁時計の 35〜63% は refine 単発の推論（実測）で、前段に依存するため並列化できない。
代わりに「誰に統合させるか」を速さで選ぶが、**品質の帯（`FREEAGENT_REFINE_BAND`）を出ない**ことと、
**学習前は従来と同一**（品質順の先頭）であることを固定する。
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402

_ORIG_STATE = os.environ.get("FREEAGENT_STATE_DIR")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-v0280-")
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        self.addCleanup(self._restore_env)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self._reset)
        self._reset()

    def _restore_env(self):
        if _ORIG_STATE is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = _ORIG_STATE

    def _reset(self):
        S._STATS["models"].clear()
        S._STATS_LOADED = True
        S._STATS_MTIME = None

    def observe(self, ref, ok, n, lat_ms):
        S._STATS["models"][ref] = {
            "updated_at": S.now_ts(),
            "kinds": {"ask": {"n": float(n), "ok": float(ok), "leak": 0.0, "trunc": 0.0,
                              "empty": 0.0, "lat_ms": float(lat_ms) * float(n),
                              "err": {"server": float(n - ok)} if n > ok else {}}},
        }


class TestPickRefiner(_Base):
    def test_band_zero_is_the_top_ranked(self):
        self.observe("nous/slow", 10, 10, 25000.0)
        self.observe("nous/fast", 10, 10, 1000.0)
        with mock.patch.object(S, "REFINE_BAND", 0.0):
            model, why = S._pick_refiner(["nous/fast", "nous/slow"])
        self.assertEqual(model, S.rank_models(["nous/fast", "nous/slow"])[0])
        self.assertIn("品質順", why)

    def test_within_the_band_the_faster_model_wins(self):
        # 品質は僅差（帯の中）・速さは 20 倍差 → 速い方が統合役
        self.observe("nous/slow-best", 40, 40, 25000.0)   # 品質の先頭だが遅い
        self.observe("nous/fast-second", 39, 40, 1200.0)  # 僅差で 2 番手・速い
        with mock.patch.object(S, "REFINE_BAND", 0.05):
            self.assertEqual(S.rank_models(["nous/fast-second", "nous/slow-best"])[0],
                             "nous/slow-best", "このテストは『品質の先頭が遅い』前提")
            model, why = S._pick_refiner(["nous/slow-best", "nous/fast-second"])
        self.assertEqual(model, "nous/fast-second")
        self.assertIn("最も速い", why)

    def test_equal_quality_stays_consistent_with_rank_models(self):
        # 品質が完全に同点なら `rank_models` の同点タイブレーク（速い順）で既に速い方が先頭
        self.observe("nous/slow", 10, 10, 25000.0)
        self.observe("nous/fast", 10, 10, 1200.0)
        with mock.patch.object(S, "REFINE_BAND", 0.05):
            model, _why = S._pick_refiner(["nous/slow", "nous/fast"])
        self.assertEqual(model, "nous/fast")
        self.assertEqual(model, S.rank_models(["nous/slow", "nous/fast"])[0])

    def test_quality_outside_the_band_is_never_sacrificed(self):
        # good: 40/40（品質 ~0.98）/ weak: 20/40（~0.5）→ 帯の外なので速くても選ばない
        self.observe("nous/good-slow", 40, 40, 25000.0)
        self.observe("nous/weak-fast", 20, 40, 500.0)
        with mock.patch.object(S, "REFINE_BAND", 0.05):
            model, _why = S._pick_refiner(["nous/weak-fast", "nous/good-slow"])
        self.assertEqual(model, "nous/good-slow")

    def test_unobserved_pool_behaves_like_before(self):
        # 学習前（レイテンシ観測なし）は品質順の先頭＝v0.27.0 と同じ
        with mock.patch.object(S, "REFINE_BAND", 0.05):
            model, why = S._pick_refiner(["nous/a", "nous/b"])
        self.assertEqual(model, S.rank_models(["nous/a", "nous/b"])[0])
        self.assertIn("品質順", why)

    def test_mixed_band_ignores_unobserved_members(self):
        self.observe("nous/slow", 10, 10, 25000.0)
        with mock.patch.object(S, "REFINE_BAND", 0.05):
            model, _why = S._pick_refiner(["nous/slow", "nous/never-seen"])
        # 未観測は inf なので「速い」と判定されない（従来どおり品質順）
        self.assertEqual(model, S.rank_models(["nous/slow", "nous/never-seen"])[0])

    def test_empty_pool_is_safe(self):
        self.assertEqual(S._pick_refiner([]), ("", ""))


class TestRefineUsesThePicker(_Base):
    def test_refine_model_comes_from_pick_refiner(self):
        S._STATS_LOADED = True
        answers = [{"model": "nous/p1", "answer": "A"}]
        with mock.patch.object(S, "free_model_refs", return_value=["nous/p1", "nous/r1"]), \
                mock.patch.object(S, "_pick_refiner", return_value=("nous/r1", "テスト")) as picker, \
                mock.patch.object(S, "call_model",
                                  return_value={"text": "結論: X\n確信度: 80", "served_by": "nous/r1"}) as call:
            out = S._refine_answers("Q", answers, ["nous/p1"], {})
        self.assertTrue(picker.called, "統合役の選抜を通っていない")
        self.assertEqual(call.call_args[0][0], "nous/r1")
        self.assertEqual(out["model"], "nous/r1")
        self.assertEqual(out["reason"], "テスト")


if __name__ == "__main__":
    unittest.main()
