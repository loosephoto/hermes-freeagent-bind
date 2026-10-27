"""§3.7 タスク分類カスケード（v0.23.0）の回帰テスト.

固定する契約:

* **既定 off** では並びが 1 ミリも変わらない（v0.21.0 までの `rank_models` / strategy の結果と同一）
* `FREEAGENT_ROUTING_TIER` で層を強制できる（測定用）。未知の値は無視（既定へ落ちる）
* light 層は「品質の下限を満たす観測済み」→「未観測」→「品質不足/遅い」の順に並べ、
  同じ段では平均所要時間の速い順（**乱数を使わない**）
* long 層は打ち切り率の低い順（観測が無いモデルは 0.5 として品質順の位置）
* 層は**呼び出し側スレッド**にだけ置かれ、`handle_tool_call` の外へ漏れない
* 選んだ層は `select_models` の info の `cascade_tier` に出る（隠さない）

ネットワークは使わない（在庫と統計はスタブ）。
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


def _stats(**kinds):
    """`_STATS["models"][ref]["kinds"]` を作る。kinds = kind -> dict(n=, ok=, trunc=, lat_ms=)."""
    return {"models": {ref: {"kinds": rows, "updated_at": 0} for ref, rows in kinds.items()}}


class TierChoiceTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for key in (S.ROUTING_ENV, S.ROUTING_TIER_ENV):
            os.environ.pop(key, None)

    def test_default_is_off(self):
        self.assertFalse(S.routing_enabled())
        self.assertEqual(S.route_tier("freeagent_ask", 100, 100), "off")
        self.assertEqual(S.route_tier("freeagent_map", 100, 50), "off")

    def test_auto_picks_light_for_short_extraction(self):
        os.environ[S.ROUTING_ENV] = "auto"
        self.assertEqual(S.route_tier("freeagent_map", 500, 200), "light")
        self.assertEqual(S.route_tier("freeagent_map", 2500, 400), "light")

    def test_auto_picks_reason_when_unclear(self):
        os.environ[S.ROUTING_ENV] = "auto"
        # max_tokens が大きい / ツールが panel / 本文が長い
        self.assertEqual(S.route_tier("freeagent_map", 500, 2000), "reason")
        self.assertEqual(S.route_tier("freeagent_panel", 500, 200), "reason")
        # 既定の max_tokens（未指定 = 0）は light にしない
        self.assertEqual(S.route_tier("freeagent_map", 500, 0), "reason")
        # `ask` は選抜を通らない（default_model）ので対象外
        self.assertEqual(S.route_tier("freeagent_ask", 100, 100), "reason")

    def test_auto_picks_long_for_big_input(self):
        os.environ[S.ROUTING_ENV] = "auto"
        self.assertEqual(S.route_tier("freeagent_ask", S.ROUTING_LONG_CHARS + 1, 200), "long")

    def test_forced_tier_wins_and_unknown_is_ignored(self):
        os.environ[S.ROUTING_TIER_ENV] = "light"
        self.assertEqual(S.route_tier("freeagent_panel", 10, 100), "light")
        os.environ[S.ROUTING_TIER_ENV] = "nonsense"
        self.assertEqual(S.route_tier("freeagent_panel", 10, 100), "off")


class OrderingTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(S.ROUTING_ENV, None)
        os.environ.pop(S.ROUTING_TIER_ENV, None)

    def tearDown(self):
        S._HARNESS.clear() if hasattr(S, "_HARNESS") else None

    def _patch_stats(self, table):
        return mock.patch.object(S, "_STATS", table, create=False)

    def test_off_is_identical_to_baseline(self):
        refs = ["p1/slow-good", "p2/fast-good", "p3/unobserved"]
        table = _stats(**{
            "p1/slow-good": {"ask": {"n": 10, "ok": 9, "lat_ms": 9000}},
            "p2/fast-good": {"ask": {"n": 10, "ok": 9, "lat_ms": 1200}},
        })
        with self._patch_stats(table), \
             mock.patch.object(S, "_STATS_LOADED", True), \
             mock.patch.object(S, "rank_models", lambda r: list(r)):
            self.assertEqual(S._order_for_selection(refs), list(refs))

    def test_light_prefers_fast_good_then_unobserved(self):
        """段は「実績のある速い順」→「未観測」→「品質不足」。速さだけで未観測を飛び越えさせない。"""
        refs = ["p1/slow-good", "p2/fast-good", "p3/unobserved"]
        table = _stats(**{
            "p1/slow-good": {"ask": {"n": 10, "ok": 9, "lat_ms": 9000}},
            "p2/fast-good": {"ask": {"n": 10, "ok": 9, "lat_ms": 1200}},
        })
        os.environ[S.ROUTING_TIER_ENV] = "light"
        with self._patch_stats(table), \
             mock.patch.object(S, "_STATS_LOADED", True), \
             mock.patch.object(S, "rank_models", lambda r: list(r)):
            got = S._order_for_selection(refs)
        self.assertEqual(got, ["p2/fast-good", "p1/slow-good", "p3/unobserved"])

    def test_light_demotes_low_quality_even_if_fast(self):
        refs = ["p1/fast-bad", "p2/slow-good"]
        table = _stats(**{
            "p1/fast-bad": {"ask": {"n": 10, "ok": 1, "err": {"CAPACITY": 9.0}, "lat_ms": 500}},
            "p2/slow-good": {"ask": {"n": 10, "ok": 10, "lat_ms": 8000}},
        })
        os.environ[S.ROUTING_TIER_ENV] = "light"
        with self._patch_stats(table), \
             mock.patch.object(S, "_STATS_LOADED", True), \
             mock.patch.object(S, "rank_models", lambda r: list(r)):
            got = S._order_for_selection(refs)
        self.assertEqual(got[0], "p2/slow-good", "品質の下限を割ったモデルを速いだけで先頭にしない")

    def test_long_prefers_low_truncation(self):
        refs = ["p1/truncates", "p2/clean"]
        table = _stats(**{
            "p1/truncates": {"ask": {"n": 10, "ok": 4, "trunc": 6.0}},
            "p2/clean": {"ask": {"n": 10, "ok": 10, "lat_ms": 9000}},
        })
        os.environ[S.ROUTING_TIER_ENV] = "long"
        with self._patch_stats(table), \
             mock.patch.object(S, "_STATS_LOADED", True), \
             mock.patch.object(S, "rank_models", lambda r: list(r)):
            got = S._order_for_selection(refs)
        self.assertEqual(got, ["p2/clean", "p1/truncates"])

    def test_cascade_tier_is_reported(self):
        os.environ[S.ROUTING_TIER_ENV] = "long"
        with mock.patch.object(S, "free_model_refs", lambda **kw: ["p1/m"]):
            _, info = S.select_models(1)
        self.assertEqual(info["cascade_tier"], "long")
        os.environ.pop(S.ROUTING_TIER_ENV, None)
        with mock.patch.object(S, "free_model_refs", lambda **kw: ["p1/m"]):
            _, info = S.select_models(1)
        self.assertEqual(info["cascade_tier"], "off")


class ScopeTest(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        os.environ.pop(S.ROUTING_ENV, None)
        os.environ.pop(S.ROUTING_TIER_ENV, None)

    def test_tier_is_set_inside_the_call_and_restored_after(self):
        seen = {}

        def fake_handler(args):
            seen["inside"] = S.current_tier()
            return {"ok": True}

        os.environ[S.ROUTING_ENV] = "auto"
        with mock.patch.dict(S.HANDLERS, {"freeagent_map": fake_handler}, clear=False):
            S.handle_tool_call({"name": "freeagent_map",
                                "arguments": {"items": ["a", "b"], "prompt": "x", "max_tokens": 200}})
        self.assertEqual(seen["inside"], "light")
        self.assertEqual(S.current_tier(), "", "層が呼び出しの外へ漏れている")

    def test_handler_exception_still_restores(self):
        def boom(args):
            raise RuntimeError("失敗")

        os.environ[S.ROUTING_ENV] = "auto"
        with mock.patch.dict(S.HANDLERS, {"freeagent_ask": boom}, clear=False):
            out = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": "x"}})
        self.assertTrue(out["isError"])
        self.assertEqual(S.current_tier(), "")

    def test_text_chars_counts_only_strings(self):
        got = S._routing_text_chars({"prompt": "abc", "items": ["de", 5, None], "max_tokens": 3})
        self.assertEqual(got, 5)


if __name__ == "__main__":
    unittest.main()
