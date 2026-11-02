"""v0.38.2: 健全性監査で見つかった不都合の回帰テスト（ネット・推論不要）。

固定する契約:
  1. `FREEAGENT_MODEL_EXCLUDE` が `select_models` / `_candidates` / `_refine_answers` / `tool_agent` で効く
  2. `classify_error` が日本語の枠待ち・クォータ・締め切り・予算超過を分類し、`tool_diagnose` が
     v0.18.0 以前の `error_class="other"` 空応答行を `no_output` として再分類する（`weird` 等の明示値は保持）
  3. `tool_diagnose` が Python 3.14 で `DeprecationWarning`（`utcfromtimestamp`）を出さない
  4. `tool_agent` の並列ワーカーが `_ModelClaims` を共有して別枠のモデルへ重複退避せず、予算切れで次ステップを止める
  5. `_refine_answers` がフォールバック後の実応答モデル（`served_by`）・冷却中・除外モデルを非参加プールから外す
  6. `tool_consult` のセッション再開が 4 体以上の参加モデルを `[:3]` で切り捨てない
  7. `resource://freeagent/api-rules` が全ツール（`S.TOOLS`）を名指ししている
  8. `test_grounded_search_v0260` の後片付けが既存の `FREEAGENT_STATE_DIR` を消さない
"""
import json
import os
import re
import sys
import tempfile
import unittest
import warnings
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from _tmpstate import isolated_state, register_state_dir, remove_tree  # noqa: E402  # type: ignore[import-not-found]

isolated_state("fa-v0382-test-")

from freeagent_bind import server as S  # noqa: E402


class ModelExcludeEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = register_state_dir(tempfile.mkdtemp(prefix="fa-v0382-ex-"))
        self.addCleanup(lambda: remove_tree(self.tmp))
        p = mock.patch.dict(os.environ, {
            "FREEAGENT_STATE_DIR": self.tmp,
            "FREEAGENT_MODEL_EXCLUDE": "nous/bad:free, openrouter/broken",
        })
        p.start()
        self.addCleanup(p.stop)

    def test_select_models_excludes_env_models_and_base_names(self):
        pool = ["nous/bad:free", "openrouter/broken:free", "nous/good:free"]
        with mock.patch.object(S, "free_model_refs", return_value=pool), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "provider_auth_blocked", return_value=""):
            chosen, info = S.select_models(3)
        self.assertEqual(["nous/good:free"], chosen)
        self.assertIn("nous/bad:free", info["exclude"])
        self.assertIn("openrouter/broken", info["exclude"])

    def test_candidates_excludes_env_models_on_fallback(self):
        pool = ["nous/primary:free", "nous/bad:free", "openrouter/broken:free", "nous/backup:free"]
        with mock.patch.object(S, "free_model_refs", return_value=pool), \
                mock.patch.object(S, "cooling_refs", return_value={}), \
                mock.patch.object(S, "provider_auth_blocked", return_value=""):
            cands = S._candidates("nous/primary:free")
        self.assertEqual(["nous/primary:free", "nous/backup:free"], cands)


class DiagnoseAndClassifyRegressionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = register_state_dir(tempfile.mkdtemp(prefix="fa-v0382-diag-"))
        self.path = os.path.join(self.tmp, "traces.jsonl")
        self.addCleanup(lambda: remove_tree(self.tmp))
        p = mock.patch.dict(os.environ, {
            "FREEAGENT_STATE_DIR": self.tmp,
            "FREEAGENT_TRACE_PATH": self.path,
        })
        p.start()
        self.addCleanup(p.stop)

    def test_classify_japanese_rate_limit_and_timeout_messages(self):
        self.assertEqual(
            "rate_limited",
            S.classify_error("提供元 openrouter の同時実行枠（3 本）が埋まったままで、20.0 秒待っても取れませんでした"),
        )
        self.assertEqual(
            "rate_limited",
            S.classify_error("提供元 nous の RPD 上限 10 に達しました（使用 10）"),
        )
        self.assertEqual(
            "timeout",
            S.classify_error("並列実行の予算（240 秒）で回収を打ち切りになりました（この結果は脱落です）"),
        )
        self.assertEqual(
            "timeout",
            S.classify_error("呼び出しの予算を使い切ったため統合を実行しませんでした"),
        )
        self.assertEqual("timeout", S.classify_error("締め切りの残りが足りません"))

    def test_legacy_other_empty_output_rows_are_reclassified_and_trigger_advice(self):
        now = float(int(S.now_ts()))
        rows = [
            # 先頭 1 件だけ別のモデルを入れ、挿入順ではなく失敗件数順（上位）になることも固定する
            {"kind": "eval", "ref": "nous/once:free", "ok": False,
             "error": "空応答（nous/once:free）。max_tokens=400 を思考トークンで使い切った可能性があります",
             "error_class": "other", "served_by": "nous/once:free", "latency_s": 0.0, "tokens": {}, "ts": now},
            # v0.18.0 より前の記録: 空応答だが error_class="other" で保存されている
            {"kind": "eval", "ref": "nous/laguna:free", "ok": False,
             "error": "空応答（nous/laguna:free）。max_tokens=400 を思考トークンで使い切った可能性があります",
             "error_class": "other", "served_by": "nous/laguna:free", "latency_s": 0.0, "tokens": {}, "ts": now},
            {"kind": "eval", "ref": "nous/laguna:free", "ok": False,
             "error": "空応答（nous/laguna:free）。max_tokens=400 を思考トークンで使い切った可能性があります",
             "error_class": "other", "served_by": "nous/laguna:free", "latency_s": 0.0, "tokens": {}, "ts": now},
            {"kind": "eval", "ref": "nous/good:free", "ok": True,
             "error": None, "error_class": None, "served_by": "nous/good:free", "latency_s": 1.2, "tokens": {}, "ts": now},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", DeprecationWarning)
            out = S.tool_diagnose({"days": 7})
        dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        self.assertEqual([], dep, f"DeprecationWarning が出た: {dep}")
        self.assertEqual({"no_output": 3}, out["by_error_class"])
        self.assertEqual("empty_output", out["advice"]["kind"])
        self.assertIn("上位: nous/laguna:free, nous/once:free", out["advice"]["advice"])
        self.assertEqual("no_output", out["recent_failures"][0]["error_class"])


class CrossCuttingContractsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = register_state_dir(tempfile.mkdtemp(prefix="fa-v0382-cc-"))
        self.addCleanup(lambda: remove_tree(self.tmp))
        p = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp})
        p.start()
        self.addCleanup(p.stop)

    def test_refine_excludes_served_by_and_cooling_models(self):
        # パネルの枠 1 は nous/a を要求して nous/b にフォールバックした → nous/b は参加者なので統合役に選ばない
        answers = [
            {"model": "nous/a:free", "served_by": "nous/b:free", "answer": "結論: 1", "conclusion": "1"},
            {"model": "nous/c:free", "served_by": "nous/c:free", "answer": "結論: 1", "conclusion": "1"},
        ]
        pool = ["nous/a:free", "nous/b:free", "nous/c:free", "nous/cool:free", "nous/fresh:free"]
        with mock.patch.object(S, "free_model_refs", return_value=pool), \
                mock.patch.object(S, "cooling_refs", return_value={"nous/cool:free": {}}), \
                mock.patch.object(S, "provider_auth_blocked", return_value=""), \
                mock.patch.object(S, "rank_models", side_effect=lambda rs: list(rs)), \
                mock.patch.object(S, "call_model", return_value={"text": "結論: 1\n確信度: 90", "served_by": "nous/fresh:free"}):
            out = S._refine_answers("Q", answers, ["nous/a:free", "nous/c:free"], {})
        self.assertEqual("nous/fresh:free", out["model"])
        self.assertFalse(out["from_panel"])

    def test_consult_resume_keeps_more_than_three_models(self):
        models4 = ["nous/m1:free", "nous/m2:free", "nous/m3:free", "nous/m4:free"]
        sid = "s_test4models"
        S._SESSIONS[sid] = {
            "question": "設計の論点は？",
            "models": list(models4),
            "rounds": [{"round": 1, "kind": "initial", "answers": []}],
            "main_replies": [],
            "updated_at": S.now_ts(),
        }
        seen_refs = []

        def fake_ask_many(refs, prompt, **kw):
            seen_refs.extend(refs)
            return [{"text": "結論: OK\n確信度: 80\nメインに確認したい点: なし", "served_by": r} for r in refs]

        with mock.patch.object(S, "session_get", return_value=S._SESSIONS[sid]), \
                mock.patch.object(S, "session_put"), \
                mock.patch.object(S, "is_cooling", return_value=False), \
                mock.patch.object(S, "ask_many", side_effect=fake_ask_many):
            out = S.tool_consult({"session_id": sid, "main_reply": "前提Aです"})
        self.assertEqual(models4, seen_refs)
        self.assertEqual(models4, out["models"])
        self.assertEqual(4, len(out["consensus"]))

    def test_agent_passes_claims_and_stops_when_budget_expired(self):
        seen_claims = []

        def fake_call_model(ref, prompt, **kw):
            seen_claims.append(kw.get("claims"))
            return {"text": '{"answer": "完了 [1]"}', "served_by": ref}

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a:free", "nous/b:free"], {})), \
                mock.patch.object(S, "call_model", side_effect=fake_call_model):
            out = S.tool_agent({"task": "調査して", "max_steps": 2})
        self.assertEqual(2, len(seen_claims))
        self.assertIsNotNone(seen_claims[0])
        self.assertIs(seen_claims[0], seen_claims[1], "並列エージェント間で同じ _ModelClaims を共有すべき")

        # 予算切れなら call_model を 1 回も呼ばずに dropped として返る
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a:free"], {})), \
                mock.patch.object(S, "budget_expired", return_value=True), \
                mock.patch.object(S, "call_model", side_effect=AssertionError("予算切れで呼んではならない")):
            out_expired = S.tool_agent({"task": "調査して", "max_steps": 2})
        self.assertTrue(out_expired["agents"][0].get("timed_out"))


class ResourceAndDocsConsistencyTest(unittest.TestCase):
    def test_api_rules_resource_lists_every_tool(self):
        text = S.resource_read("resource://freeagent/api-rules") or ""
        missing = [t["name"] for t in S.TOOLS if t["name"] not in text]
        self.assertEqual([], missing, f"api-rules Resource に載っていないツール: {missing}")

    def test_readme_env_defaults_match_server_constants(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("`FREEAGENT_MODEL_EXCLUDE`", readme)
        self.assertIn("`FREEAGENT_QUOTA_PATH`", readme)
        self.assertRegex(readme, r"\|\s*`FREEAGENT_SESSION_MAX`\s*\|\s*32\s*\|")
        self.assertRegex(readme, r"\|\s*`FREEAGENT_STATS_HALFLIFE_DAYS`\s*\|\s*14\.0\s*\|")
        self.assertRegex(readme, r"\|\s*`FREEAGENT_TRACE_MAX_BYTES`\s*\|\s*5000000\s*\|")


if __name__ == "__main__":
    unittest.main()
