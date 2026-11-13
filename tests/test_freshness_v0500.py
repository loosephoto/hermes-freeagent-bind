"""v0.50.0: 既定モデルの鮮度（直近 404/410 を既定選抜から外す・理由つきで一覧に残す）。

issue #12 の実測（`model` 省略の ask が廃止モデルで 404・`.env` のキーは届くのに推論できない・
probe で Free 71→63/64 件）を回帰にする。**在庫（一覧）は消さず、既定選抜と `usable_now` から外す**
のが要点で、隠すことではない（理由は `gone_models` / `freshness` に残す）。
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402
from freeagent_bind import stats as ST  # noqa: E402

NOW = 1_700_000_000.0


def _stats_with(entries: dict) -> dict:
    """`ref -> [(kind, error か None, 時刻)]` から model_stats の models 辞書を作る。"""
    models: dict = {}
    for ref, rows in entries.items():
        for kind, error, at in rows:
            ST._stats_apply(models, ref, kind, now=at, error=error or "")
    return models


class FreshnessCoreTest(unittest.TestCase):
    def setUp(self):
        self.models = {}
        patch = mock.patch.object(S, "_STATS", {"models": self.models})
        patch.start()
        self.addCleanup(patch.stop)
        enabled = mock.patch.object(S, "STATS_ENABLED", False)   # ディスクを読まない
        enabled.start()
        self.addCleanup(enabled.stop)
        # 鮮度は「いま」との差で決まる。テストの時刻（NOW）に固定する（実時刻だと TTL 切れになる）。
        clock = mock.patch.object(S, "now_ts", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)

    def _apply(self, ref, kind="ask", error="", at=NOW):
        ST._stats_apply(self.models, ref, kind, now=at, error=error)

    def test_gone_is_recorded_with_a_reason(self):
        self._apply("nvidia/01-ai/yi-large", error="HTTP 404: Not found for account 'x'")
        info = ST.model_freshness("nvidia/01-ai/yi-large")
        self.assertTrue(info["gone"])
        self.assertIn("404", info["gone_reason"])
        self.assertEqual(info["last_gone_at"], NOW)
        self.assertGreater(info["gone_expires_in_s"], 0)
        self.assertNotIn("last_ok_at", info)

    def test_success_clears_gone(self):
        self._apply("p/m", error="HTTP 404: gone")
        self.assertTrue(ST.is_gone("p/m"))
        self._apply("p/m", at=NOW + 10)
        self.assertFalse(ST.is_gone("p/m"))
        info = ST.model_freshness("p/m")
        self.assertEqual(info["last_ok_at"], NOW + 10)
        self.assertFalse(info["gone"])

    def test_empty_response_counts_as_alive_but_is_not_a_success(self):
        self._apply("p/m", error="HTTP 404: gone")
        self._apply("p/m", error="空応答（finish_reason=length）", at=NOW + 5)
        # 空応答は「答えた」＝存在する。廃止の記録は打ち消える（quality は上がらない）。
        self.assertFalse(ST.is_gone("p/m"))
        entry = self.models["p/m"]["kinds"]["ask"]
        self.assertEqual(entry["ok"], 0.0)
        self.assertEqual(entry["err"]["no_output"], 1.0)

    def test_rate_limited_and_timeout_do_not_mark_gone(self):
        for ref, err in (("p/rl", "HTTP 429: rate limited"), ("p/to", "TimeoutError: timed out")):
            self._apply(ref, error=err)
            info = ST.model_freshness(ref)
            self.assertFalse(info.get("gone"), ref)
            self.assertNotIn("gone_reason", info)
        self.assertTrue(ST.model_freshness("p/rl")["last_rate_limited_at"])

    def test_gone_ttl_expires_and_recheck_returns(self):
        self._apply("p/m", error="HTTP 404: gone")
        with mock.patch.object(S, "now_ts", return_value=NOW + 3600):
            self.assertTrue(ST.is_gone("p/m"))
        with mock.patch.object(S, "now_ts", return_value=NOW + ST.GONE_TTL_S + 1):
            self.assertFalse(ST.is_gone("p/m"), "TTL を過ぎたら再確認に戻す（復活を拾う）")
        with mock.patch.object(ST, "GONE_TTL_S", 60.0), \
                mock.patch.object(S, "now_ts", return_value=NOW + 61):
            self.assertFalse(ST.is_gone("p/m"))

    def test_decay_does_not_move_timestamps(self):
        self._apply("p/m", at=NOW)
        later = NOW + 30 * 86400
        self._apply("p/m", error="HTTP 404: gone", at=later)
        entry = self.models["p/m"]["kinds"]["ask"]
        self.assertEqual(entry["last_ok_at"], NOW, "減衰で時刻を割り引かない")
        self.assertEqual(entry["last_gone_at"], later)
        # 古い成功（30 日前）より新しい廃止が勝つ
        with mock.patch.object(S, "now_ts", return_value=later + 60):
            self.assertTrue(ST.is_gone("p/m"))

    def test_gone_refs_returns_reason_for_each(self):
        self._apply("p/a", error="HTTP 410: EOL")
        self._apply("p/b", at=NOW)
        self._apply("p/c", error="HTTP 404: page not found")
        refs = ST.gone_refs()
        self.assertEqual(sorted(refs), ["p/a", "p/c"])
        self.assertIn("410", refs["p/a"]["gone_reason"])
        self.assertIn("page not found", refs["p/c"]["gone_reason"])

    def test_unknown_ref_has_no_freshness(self):
        self.assertEqual(ST.model_freshness("p/none"), {})
        self.assertFalse(ST.is_gone("p/none"))


class SelectionAvoidsGoneTest(unittest.TestCase):
    POOL = ["p/gone", "p/alive1", "p/alive2"]

    def _select(self, **kwargs):
        with mock.patch.object(S, "free_model_refs", return_value=list(self.POOL)), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "provider_auth_blocked", return_value=""), \
                mock.patch.object(S, "gone_refs", return_value={"p/gone": {"gone": True, "gone_reason": "404"}}), \
                mock.patch.object(S, "is_gone", side_effect=lambda ref: ref == "p/gone"), \
                mock.patch.object(S, "diverse_order", side_effect=lambda refs: list(refs)), \
                mock.patch.object(S, "RANK_ENABLED", False):
            return S.select_models(**kwargs)

    def test_auto_selection_skips_gone_and_says_why(self):
        chosen, info = self._select(size=2)
        self.assertNotIn("p/gone", chosen)
        self.assertEqual(chosen, ["p/alive1", "p/alive2"])
        self.assertTrue(any("404/410" in note for note in info["notes"]), info["notes"])

    def test_explicit_request_still_tries_gone_with_a_note(self):
        chosen, info = self._select(size=1, requested=["p/gone"])
        self.assertEqual(chosen, ["p/gone"], "明示指定は復活の確認のために試す")
        self.assertTrue(any("廃止" in note for note in info["notes"]), info["notes"])

    def test_default_model_falls_back_past_gone(self):
        with mock.patch.object(S, "DEFAULT_MODEL", ""), \
                mock.patch.object(S, "select_models", return_value=([], {})), \
                mock.patch.object(S, "free_model_refs", return_value=["p/gone", "p/ok"]), \
                mock.patch.object(S, "is_gone", side_effect=lambda ref: ref == "p/gone"):
            self.assertEqual(S.default_model(), "p/ok")

    def test_default_model_returns_empty_when_everything_is_gone(self):
        with mock.patch.object(S, "DEFAULT_MODEL", ""), \
                mock.patch.object(S, "select_models", return_value=([], {})), \
                mock.patch.object(S, "free_model_refs", return_value=["p/gone"]), \
                mock.patch.object(S, "is_gone", return_value=True):
            self.assertEqual(S.default_model(), "")


class ModelsReportTest(unittest.TestCase):
    ROWS = [{"provider": "p", "id": "gone", "free": True},
            {"provider": "p", "id": "alive", "free": True}]
    GONE = {"p/gone": {"gone": True, "gone_reason": "HTTP 404: page not found",
                       "gone_age_s": 120.0, "gone_expires_in_s": 80000.0}}

    def _models(self, args):
        with mock.patch.object(S, "provider_status", return_value=[]), \
                mock.patch.object(S, "free_model_refs", return_value=["p/gone", "p/alive"]), \
                mock.patch.object(S, "gone_refs", return_value=dict(self.GONE)), \
                mock.patch.object(S, "model_freshness",
                                  side_effect=lambda ref: dict(self.GONE.get(ref, {}) or {"last_ok_at": NOW})), \
                mock.patch.object(S, "is_cooling", return_value=False), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "default_model", return_value="p/alive"), \
                mock.patch.object(S, "all_models", return_value=list(self.ROWS)), \
                mock.patch.object(S, "provider_ready", return_value=True), \
                mock.patch.object(S, "harness_status", return_value=None), \
                mock.patch.object(S, "RANK_ENABLED", False):
            return S.tool_models(args)

    def test_inventory_usable_and_gone_are_three_different_numbers(self):
        data = self._models({})
        self.assertEqual(data["free_candidates"], 2, "在庫は廃止を消さない")
        self.assertEqual(data["usable_now"], 1, "使用可は廃止とクールダウンを除く")
        self.assertEqual(data["gone"], 1)
        self.assertEqual(data["gone_models"][0]["ref"], "p/gone")
        self.assertIn("page not found", data["gone_models"][0]["reason"])

    def test_rows_are_marked_gone_by_default_and_can_be_included(self):
        hidden = self._models({"all": True, "include_gone": False})
        self.assertEqual([r["ref"] for r in hidden["models"]], ["p/alive"])
        self.assertEqual(hidden["query"]["gone_hidden"], 1)
        shown = self._models({"all": True, "include_gone": True})
        rows = {r["ref"]: r for r in shown["models"]}
        self.assertEqual(sorted(rows), ["p/alive", "p/gone"])
        self.assertTrue(rows["p/gone"]["gone"])
        self.assertIn("page not found", rows["p/gone"]["freshness"]["gone_reason"])
        self.assertFalse(rows["p/alive"]["gone"])
        self.assertIn("last_ok_at", rows["p/alive"]["freshness"])

    def test_probe_budget_default_is_large_enough_for_reasoning_models(self):
        seen = {}

        def fake_call(ref, prompt, **kwargs):
            seen["max_tokens"] = kwargs.get("max_tokens")
            return {"ref": ref, "text": "1", "served_by": ref, "tokens": {"completion": 1},
                    "max_tokens": kwargs.get("max_tokens")}

        with mock.patch.object(S, "call_model", side_effect=fake_call), \
                mock.patch.object(S, "run_parallel",
                                  side_effect=lambda items, fn, **kw: [fn(item) for item in items]):
            self._models({"probe": True, "probe_limit": 1})
        self.assertEqual(seen["max_tokens"], 64, "16 では思考モデルが本文 0 になる（実測）")


class ProbeVerdictTest(unittest.TestCase):
    def _probe(self, error):
        cfg_rows = [{"provider": "p", "id": "m", "free": True}]
        with mock.patch.object(S, "provider_status", return_value=[]), \
                mock.patch.object(S, "free_model_refs", return_value=["p/m"]), \
                mock.patch.object(S, "gone_refs", return_value={}), \
                mock.patch.object(S, "model_freshness", return_value={}), \
                mock.patch.object(S, "is_cooling", return_value=False), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "default_model", return_value="p/m"), \
                mock.patch.object(S, "all_models", return_value=cfg_rows), \
                mock.patch.object(S, "provider_ready", return_value=True), \
                mock.patch.object(S, "harness_status", return_value=None), \
                mock.patch.object(S, "call_model", return_value={"ref": "p/m", "error": error,
                                                                 "text": "", "tokens": {}}), \
                mock.patch.object(S, "run_parallel",
                                  side_effect=lambda items, fn, **kw: [fn(item) for item in items]), \
                mock.patch.object(S, "RANK_ENABLED", False):
            return S.tool_models({"probe": True, "probe_limit": 1})

    def test_404_is_dropped_as_gone(self):
        data = self._probe("HTTP 404: page not found")
        self.assertEqual(data["query"]["probe_dropped"][0]["verdict"], "gone")
        self.assertEqual([r["ref"] for r in data["models"]], [])

    def test_429_is_kept_as_temporary_error(self):
        data = self._probe("HTTP 429: upstream_provider_shared_pool")
        self.assertEqual(data["models"][0]["probe"], "error")
        self.assertEqual(data["query"]["probe_dropped"], [])

    def test_empty_output_is_not_a_death(self):
        data = self._probe("空応答（finish_reason=length）")
        self.assertEqual(data["models"][0]["probe"], "slow")
        self.assertEqual(data["query"]["probe_dropped"], [])


if __name__ == "__main__":
    unittest.main()
