"""v0.27.0 の契約: 遅延ペナルティは**既定 off**で、オンにしたときだけ品質点から引く.

固定すること:
  * `FREEAGENT_RANK_LATENCY=0`（既定）では `_latency_penalty` が**厳密に 0.0**、`rank_models` の並びは
    v0.26.0 と同一（品質点の降順＋同点は平均所要）。
  * オンにすると、**品質差が小さい範囲でだけ**速いモデルが前に出る（品質が大きく劣るモデルは動かない）。
  * ペナルティは重みで頭打ち（20 秒を超えても増えない）、観測の無いモデルは 0。
  * `_rank_score` は knob で**変化しない**（純粋な関数のまま＝他の読み手を壊さない）。
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
        self.tmp = tempfile.mkdtemp(prefix="fa-v0270-")
        # **本番の状態ディレクトリを読まない**（`_reset` の中で env を消すと既定へ戻り、
        # `%LOCALAPPDATA%\hermes-freeagent-bind` の実データを読んでしまう）。
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
        S._STATS_LOADED = True          # ディスクを見ない（このテストは並びだけを見る）
        S._STATS_MTIME = None

    def observe(self, ref, ok, n, lat_ms):
        """`lat_ms` は**1 回あたり**の平均。`model_mean_latency` は `lat_ms / n` を返すので合計で入れる。"""
        S._STATS["models"][ref] = {
            "updated_at": S.now_ts(),
            "kinds": {"ask": {"n": float(n), "ok": float(ok), "leak": 0.0, "trunc": 0.0,
                              "empty": 0.0, "lat_ms": float(lat_ms) * float(n),
                              "err": {"server": float(n - ok)} if n > ok else {}}},
        }


class TestPenaltyMath(_Base):
    def test_off_by_default_is_exactly_zero(self):
        self.observe("nous/slow", 10, 10, 30000.0)
        with mock.patch.object(S, "RANK_LATENCY_W", 0.0):
            self.assertEqual(S._latency_penalty("nous/slow"), 0.0)
            self.assertEqual(S._latency_penalty("nous/never"), 0.0)

    def test_scales_with_latency_and_caps_at_the_weight(self):
        self.observe("nous/two", 1, 1, 2000.0)
        self.observe("nous/twenty", 1, 1, 20000.0)
        self.observe("nous/hour", 1, 1, 3600000.0)
        with mock.patch.object(S, "RANK_LATENCY_W", 0.5), \
                mock.patch.object(S, "RANK_LATENCY_FULL_S", 20.0):
            self.assertAlmostEqual(S._latency_penalty("nous/two"), 0.05, places=6)
            self.assertAlmostEqual(S._latency_penalty("nous/twenty"), 0.5, places=6)
            self.assertAlmostEqual(S._latency_penalty("nous/hour"), 0.5, places=6,
                                   msg="20 秒を超えても引く量が増えている（頭打ちが効いていない）")

    def test_unobserved_model_has_no_penalty(self):
        with mock.patch.object(S, "RANK_LATENCY_W", 1.0):
            self.assertEqual(S._latency_penalty("nous/never-seen"), 0.0)


class TestOrdering(_Base):
    def test_off_matches_the_v0260_order(self):
        self.observe("nous/fast", 9, 10, 1500.0)
        self.observe("nous/slow", 10, 10, 25000.0)
        with mock.patch.object(S, "RANK_LATENCY_W", 0.0):
            got = S.rank_models(["nous/fast", "nous/slow"])
        expected = sorted(["nous/fast", "nous/slow"],
                          key=lambda r: (-S._rank_score(r), S._rank_latency_key(r), r))
        self.assertEqual(got, expected)
        self.assertEqual(got[0], "nous/slow", "品質が上のモデルが落ちている（既定 off のはず）")

    def test_on_prefers_the_fast_model_when_quality_is_close(self):
        self.observe("nous/fast", 9, 10, 1500.0)
        self.observe("nous/slow", 10, 10, 25000.0)
        with mock.patch.object(S, "RANK_LATENCY_W", 0.5), \
                mock.patch.object(S, "RANK_LATENCY_FULL_S", 20.0):
            self.assertEqual(S.rank_models(["nous/slow", "nous/fast"])[0], "nous/fast")

    def test_on_does_not_override_a_large_quality_gap(self):
        self.observe("nous/good-slow", 40, 40, 25000.0)     # 100% 正答
        self.observe("nous/bad-fast", 4, 10, 1000.0)        # 40% 正答
        with mock.patch.object(S, "RANK_LATENCY_W", 0.5), \
                mock.patch.object(S, "RANK_LATENCY_FULL_S", 20.0):
            self.assertEqual(S.rank_models(["nous/bad-fast", "nous/good-slow"])[0],
                             "nous/good-slow", "品質差を遅延でひっくり返している")

    def test_rank_score_is_unaffected_by_the_knob(self):
        self.observe("nous/m", 7, 10, 9000.0)
        with mock.patch.object(S, "RANK_LATENCY_W", 0.0):
            before = S._rank_score("nous/m")
        with mock.patch.object(S, "RANK_LATENCY_W", 1.0):
            after = S._rank_score("nous/m")
        self.assertEqual(before, after)

    def test_env_default_is_zero(self):
        # 既定は必ず off（モジュール読み込み時の値）。起動時に env で上書きされていないこと。
        self.assertEqual(max(0.0, min(1.0, float(os.environ.get("FREEAGENT_RANK_LATENCY", 0) or 0))),
                         0.0 if "FREEAGENT_RANK_LATENCY" not in os.environ else S.RANK_LATENCY_W)


if __name__ == "__main__":
    unittest.main()
