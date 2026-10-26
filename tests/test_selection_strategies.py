"""自動選抜の strategy（§3.6）の契約。

固定すること:
  * 既定（`quality`）は **v0.16.0 までと同一の並び**（`rank_models` をそのまま使う）。
  * `catalog` は一覧の順、`least_busy` は提供元の実行中本数が少ない順。
  * 未知の値は `quality` に落ちる（起動を止めない・説明も嘘をつかない）。
  * どの strategy でも**多様性（`diverse_order`）は後段で共通**なので、1 提供元が独占しない。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-select-test-"))

import freeagent_bind.server as S  # noqa: E402

ROSTER = ["nous/a", "nous/b", "openrouter/c", "openrouter/d", "groq/e", "groq/f"]


def fake_stats(**scores):
    """`_rank_score` 相当を差し替える（品質観測の代わりに固定スコア）。"""
    return mock.patch.object(S, "_rank_score", side_effect=lambda ref: scores.get(ref, 0.0))


class TestStrategySelection(unittest.TestCase):
    def test_default_is_quality_and_matches_rank_models(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREEAGENT_SELECT_STRATEGY", None)
            self.assertEqual(S.selection_strategy(), "quality")
            with fake_stats(**{"nous/a": 0.1, "groq/e": 0.9}):
                self.assertEqual(S._order_for_selection(ROSTER), S.rank_models(ROSTER))

    def test_unknown_value_falls_back_to_quality(self):
        with mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": "totally-unknown"}):
            self.assertEqual(S.selection_strategy(), "quality")

    def test_catalog_keeps_the_catalog_order(self):
        with mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": "catalog"}):
            self.assertEqual(S._order_for_selection(ROSTER), ROSTER)

    def test_least_busy_puts_the_busy_provider_last(self):
        report = {"per_provider": {"nous": {"in_flight": 3}, "openrouter": {"in_flight": 0},
                                   "groq": {"in_flight": 1}}}
        with mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": "least_busy"}), \
                mock.patch.object(S.PROVIDER_GATE, "report", return_value=report):
            order = S._order_for_selection(ROSTER)
        providers = [r.split("/", 1)[0] for r in order]
        self.assertLess(providers.index("openrouter"), providers.index("groq"))
        self.assertLess(providers.index("groq"), providers.index("nous"))
        self.assertEqual(sorted(order), sorted(ROSTER))     # 落とさない・増やさない

    def test_least_busy_is_deterministic_on_ties(self):
        report = {"per_provider": {}}
        with mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": "least_busy"}), \
                mock.patch.object(S.PROVIDER_GATE, "report", return_value=report):
            first = S._order_for_selection(ROSTER)
            second = S._order_for_selection(ROSTER)
        self.assertEqual(first, second, "同点で揺れると再現できない（乱数を使わない）")


class TestSelectModelsUsesTheStrategy(unittest.TestCase):
    def test_info_reports_the_strategy(self):
        with mock.patch.object(S, "free_model_refs", return_value=list(ROSTER)), \
                mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": "catalog"}):
            chosen, info = S.select_models(2)
        self.assertEqual(info["selection_strategy"], "catalog")
        self.assertEqual(len(chosen), 2)

    def test_diversity_survives_every_strategy(self):
        """1 提供元が 4 体いても、size=3 で 3 体独占にならない（diverse_order が共通のため）。"""
        roster = ["nous/a", "nous/b", "nous/c", "nous/d", "groq/e"]
        for strategy in S.SELECT_STRATEGIES:
            with mock.patch.object(S, "free_model_refs", return_value=list(roster)), \
                    mock.patch.dict(os.environ, {"FREEAGENT_SELECT_STRATEGY": strategy}), \
                    mock.patch.object(S, "cooling_refs", return_value=[]):
                chosen, _ = S.select_models(3)
            providers = {r.split("/", 1)[0] for r in chosen}
            self.assertGreaterEqual(len(providers), 2, f"{strategy}: {chosen}")


if __name__ == "__main__":
    unittest.main()
