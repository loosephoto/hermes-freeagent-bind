"""v0.23.1 の監査 findings の回帰（予算の共有・既定モデルの選抜・失敗候補の記録）.

固定する契約（すべて実測で再現した不都合）:
  1. `tool_map` / `tool_agent` は `FREEAGENT_CALL_BUDGET` で打ち切られる（以前は予算を渡しておらず、
     1 要素の遅れで 300 秒のクライアント待ちに当たり、取れていた結果まで失われた）。
  2. `consult(debate_depth="deep")` は**1 回の呼び出しで 1 つの予算**を共有する（以前はラウンドごとに
     満額を作り、240 秒 × 2 になった）。`panel(refine)` / `map(reduce)` の後段も残り時間を見る。
  3. `default_model()` は**品質統計・クールダウンを通した並び**の先頭を返す（以前はカタログ順の先頭）。
  4. `_candidates()`（フォールバックの並び）も選抜と同じ規則で並ぶ（以前はカタログ順）。
  5. フォールバックで**落ちた候補も統計に記録**し、返り値の `ref` は**要求したモデル**のまま
     （`served_by` が実際に答えたモデル）。以前は成功した候補だけ記録していたため、毎回落ちる
     モデルが `unobserved` のまま探索ボーナスで第 1 候補に居座った。
  6. `verify` の `used` には `served_by` も入る（検証者へフォールバックしたモデルが、
     続く代替案の提案者に選ばれない＝規約 26）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _isolated():
    tmp = tempfile.mkdtemp(prefix="fa-v0231-")
    os.environ["FREEAGENT_STATE_DIR"] = tmp
    return tmp


import freeagent_bind.server as S  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _isolated()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # 統計・クールダウン・セッションはモジュール全体で共有される（前のテストの行を持ち越さない）。
        self.addCleanup(self._reset_state)
        self._reset_state()

    def _reset_state(self):
        S._STATS["models"].clear()
        S._STATS_LOADED = False
        S._COOLDOWN.clear()
        S._COOLDOWN_LOADED = False
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def _ok(self, text="out", latency=0.0, ref="nous/m1"):
        return {"text": text, "ref": ref, "served_by": ref, "latency_s": latency,
                "truncated": False, "tokens": {}}


class TestToolBudgetIsHonoured(_Base):
    """(1) 打ち切りが**ツール単位**で効く（予算を渡し忘れていない）。"""

    def test_tool_map_stops_at_the_call_budget(self):
        def slow(provider, model, *a, **kw):
            time.sleep(0.8)
            return self._ok(), 0.0

        with mock.patch.object(S, "CALL_BUDGET_S", 0.1), \
                mock.patch.object(S, "select_models", return_value=(["nous/m1"], {})), \
                mock.patch.object(S, "_gated_call_once", side_effect=slow), \
                mock.patch.object(S, "STATS_ENABLED", False), \
                mock.patch.object(S, "TRACE_ENABLED", False):
            started = time.monotonic()
            got = S.handle_tool_call({"name": "freeagent_map",
                                      "arguments": {"items": ["a", "b"], "instruction": "x"}})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, f"予算が効いていない（{elapsed:.2f} 秒）")
        rows = got["structuredContent"]["results"]
        self.assertTrue(all("予算" in (r.get("error") or "") for r in rows), rows)

    def test_tool_agent_stops_at_the_call_budget(self):
        def slow_call(ref, *a, **kw):
            time.sleep(0.8)
            return {"ref": ref, "served_by": ref, "text": '{"answer": "x"}', "latency_s": 0.8}

        with mock.patch.object(S, "CALL_BUDGET_S", 0.1), \
                mock.patch.object(S, "_select_or_error", return_value=(["nous/m1", "nous/m2"], {})), \
                mock.patch.object(S, "call_model", side_effect=slow_call), \
                mock.patch.object(S, "STATS_ENABLED", False), \
                mock.patch.object(S, "TRACE_ENABLED", False):
            started = time.monotonic()
            got = S.handle_tool_call({"name": "freeagent_agent", "arguments": {"task": "t"}})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.5, f"サブエージェントの予算が効いていない（{elapsed:.2f} 秒）")
        self.assertTrue(all("予算" in (a.get("error") or "")
                            for a in got["structuredContent"]["agents"]))


class TestOneBudgetPerCall(_Base):
    """(2) 多段ツールが**1 つの予算**を共有する（ラウンドごとに満額を作らない）。"""

    def test_deep_consult_shares_one_budget_across_rounds(self):
        def slow_call(ref, prompt, **kw):
            time.sleep(0.5)
            return {"ref": ref, "served_by": ref, "latency_s": 0.5,
                    "text": "結論: A\n確信度: 80\nメインに確認したい点: なし\n"
                            "立場: A\n最強の反論: B\n応答: C\n未解決: なし"}

        with mock.patch.object(S, "CALL_BUDGET_S", 0.15), \
                mock.patch.object(S, "_select_or_error", return_value=(["nous/m1"], {})), \
                mock.patch.object(S, "call_model", side_effect=slow_call), \
                mock.patch.object(S, "SESSIONS_ENABLED", False), \
                mock.patch.object(S, "STATS_ENABLED", False), \
                mock.patch.object(S, "TRACE_ENABLED", False):
            started = time.monotonic()
            got = S.handle_tool_call({"name": "freeagent_consult",
                                      "arguments": {"question": "q", "debate_depth": "deep"}})
            elapsed = time.monotonic() - started
        # 旧実装はラウンドごとに満額（0.15 秒 × 2 ラウンド）を新しく作った。
        self.assertLess(elapsed, 0.9, f"ラウンドごとに予算を作っている（{elapsed:.2f} 秒）")
        data = got["structuredContent"]
        dropped = (data.get("debate_summary") or {}).get("dropped") or []
        self.assertTrue(dropped, "打ち切った討論ラウンドが dropped に出ていない（隠している）")

    def test_map_reduce_is_skipped_when_the_budget_is_spent(self):
        with mock.patch.object(S, "CALL_BUDGET_S", 0.2), \
                mock.patch.object(S, "select_models", return_value=(["nous/m1"], {})), \
                mock.patch.object(S, "_gated_call_once",
                                  side_effect=lambda *a, **k: (time.sleep(0.3) or self._ok(), 0.0)), \
                mock.patch.object(S, "STATS_ENABLED", False), \
                mock.patch.object(S, "TRACE_ENABLED", False):
            started = time.monotonic()
            got = S.handle_tool_call({"name": "freeagent_map",
                                      "arguments": {"items": ["a", "b"], "instruction": "x",
                                                    "reduce": True}})
            elapsed = time.monotonic() - started
        data = got["structuredContent"]
        self.assertLess(elapsed, 0.9, f"予算切れの reduce を始めている（{elapsed:.2f} 秒）")
        self.assertIn("予算", data.get("reduce_error") or "")
        self.assertNotIn("reduced", data)

    def test_dropped_result_keeps_sub_second_precision(self):
        msg = S._dropped_result(S.Deadline(0.25), "回収を打ち切り")["error"]
        self.assertIn("0.25", msg, "1 秒未満の予算が「0 秒」と表示される")


class TestDefaultModelUsesSelection(_Base):
    """(3)(4) 既定モデルとフォールバックの並びが**選抜の規則**を通る。"""

    def test_default_model_prefers_quality_over_catalog_order(self):
        with mock.patch.object(S, "DEFAULT_MODEL", ""), \
                mock.patch.object(S, "free_model_refs", return_value=["nous/bad", "nous/good"]), \
                mock.patch.object(S, "_rank_score", side_effect=lambda r: 0.1 if "bad" in r else 0.9), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "provider_auth_blocked", lambda provider: None):
            self.assertEqual(S.default_model(), "nous/good")

    def test_default_model_skips_a_cooling_catalog_head(self):
        cooling = {"nous/bad": {"until": S.now_ts() + 600, "reason": "HTTP 429"}}
        with mock.patch.object(S, "DEFAULT_MODEL", ""), \
                mock.patch.object(S, "free_model_refs", return_value=["nous/bad", "nous/good"]), \
                mock.patch.object(S, "_rank_score", return_value=0.5), \
                mock.patch.object(S, "cooling_refs", return_value=cooling), \
                mock.patch.object(S, "provider_auth_blocked", lambda provider: None):
            self.assertEqual(S.default_model(), "nous/good")

    def test_candidates_follow_the_selection_order(self):
        cooling = {"nous/primary": {"until": S.now_ts() + 600, "reason": "HTTP 503"}}
        with mock.patch.object(S, "free_model_refs",
                               return_value=["nous/primary", "nous/weak", "nous/proven"]), \
                mock.patch.object(S, "_rank_score", side_effect=lambda r: 0.95 if "proven" in r else 0.2), \
                mock.patch.object(S, "cooling_refs", return_value=cooling), \
                mock.patch.object(S, "provider_auth_blocked", lambda provider: None):
            self.assertEqual(S._candidates("nous/primary")[:2], ["nous/proven", "nous/weak"])


class TestFailedCandidateIsRecorded(_Base):
    """(5) フォールバックで落ちた候補を統計に残し、`ref` は要求元のまま返す。"""

    def test_failure_is_observed_and_ref_kept(self):
        def gated(provider, model, *a, **kw):
            if model == "longcat":
                raise S.HttpStatusError(503, "busy")
            return self._ok(ref=f"{provider}/{model}"), 0.0

        seen = []
        with mock.patch.object(S, "resolve_ref", side_effect=lambda ref, free_only=True:
                               tuple(ref.split("/", 1))), \
                mock.patch.object(S, "_is_known_paid_model", lambda p, m: False), \
                mock.patch.object(S, "_gated_call_once", side_effect=gated), \
                mock.patch.object(S, "_candidates", return_value=["nous/longcat", "nous/laguna"]), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "clear_provider_auth", lambda *a, **k: None), \
                mock.patch.object(S, "observe_call",
                                  side_effect=lambda res, kind, text="":
                                  seen.append((res.get("ref"), res.get("error") or ""))):
            out = S._call_model_uncached("nous/longcat", "hello")

        self.assertEqual(out["ref"], "nous/longcat", "要求モデルが上書きされている")
        self.assertEqual(out["served_by"], "nous/laguna")
        self.assertTrue(out["fallback"])
        self.assertIn(("nous/longcat", "HTTP 503: busy"), seen,
                      "落ちた候補が統計に残っていない（unobserved のまま選ばれ続ける）")
        self.assertIn(("nous/laguna", ""), seen, "答えたモデルへの記録が消えた")

    def test_429_is_not_counted_as_a_model_fault(self):
        calls = []

        def gated(provider, model, *a, **kw):
            if model == "m1":
                raise S.HttpStatusError(429, "slow down")
            return self._ok(ref=f"{provider}/{model}"), 0.0

        with mock.patch.object(S, "resolve_ref", side_effect=lambda ref, free_only=True:
                               tuple(ref.split("/", 1))), \
                mock.patch.object(S, "_is_known_paid_model", lambda p, m: False), \
                mock.patch.object(S, "_gated_call_once", side_effect=gated), \
                mock.patch.object(S, "_candidates", return_value=["nous/m1", "nous/m2"]), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "note_rate_limited", return_value=0.0), \
                mock.patch.object(S, "clear_provider_auth", lambda *a, **k: None), \
                mock.patch.object(S, "observe_call",
                                  side_effect=lambda res, kind, text="":
                                  calls.append((res.get("ref"), res.get("error") or ""))):
            S._call_model_uncached("nous/m1", "hello")
        self.assertNotIn("nous/m1", [ref for ref, err in calls if err],
                         "429（提供元の一時制限）をモデルの失敗として記録している")


class TestRenderRobustness(_Base):
    """予算切れの行（`model` を持たない）で表示と structuredContent を失わない。"""

    def test_agent_render_tolerates_dropped_rows(self):
        dropped = S._dropped_result(S.Deadline(30), "回収を打ち切り")
        text = S.render("freeagent_agent", {
            "answered": 0, "models": ["nous/a"], "tool_calls": 0, "citation_count": 0,
            "answers_with_citations": 0, "agreement": 0.0, "agents": [dropped]})
        self.assertIn("予算", text)
        self.assertIn("未実行", text)

    def test_render_failure_keeps_the_structured_result(self):
        with mock.patch.object(S, "_RENDERERS", {"freeagent_agent": lambda d: d["nope"]}), \
                mock.patch.object(S, "HANDLERS", {"freeagent_agent": lambda a: {"agents": [{"model": "nous/a"}]}}):
            got = S.handle_tool_call({"name": "freeagent_agent", "arguments": {}})
        self.assertIn("agents", got["structuredContent"],
                      "表示の失敗で構造化された結果が消えている")
        self.assertIn("render_error", got["structuredContent"])


class TestVerifyUsesServedBy(_Base):
    """(6) 検証者へフォールバックしたモデルを `used` に載せる（規約 26）。"""

    def test_used_includes_the_model_that_actually_answered(self):
        rows = [{"ref": "nous/a", "served_by": "nous/fallback", "text": "hi"}]
        with mock.patch.object(S, "select_models", return_value=(["nous/a"], {"notes": []})), \
                mock.patch.object(S, "ask_many", return_value=rows):
            verification, used, failure = S._think_verify_block(
                {}, "q", [], {"n": 1, "text": "t"}, {}, [], "")
        self.assertIsNone(failure)
        self.assertIsNotNone(verification)
        self.assertIn("nous/a", used)
        self.assertIn("nous/fallback", used,
                      "実際に答えたモデルが used に無い（代替案の提案者に同じモデルが選ばれる）")


if __name__ == "__main__":
    unittest.main()
