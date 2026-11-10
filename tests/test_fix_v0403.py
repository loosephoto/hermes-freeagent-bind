# -*- coding: utf-8 -*-
"""v0.40.3 の修正の回帰テスト。

監査（2026-11-11）で見つかった 4 型の再発防止:
  1. devdocs の `doc` 引数が tool_lookup / tool_grounded / knowledge_lookup に届かない
  2. ハンドラが読む引数が inputSchema に無い（consult.basis / grounded.github_kind・exclude /
     agent.exclude / map.reduce_max_tokens・system）
  3. 空応答の引き上げ再試行が例外を握り潰し、429・環境障害を「空応答（モデルの成績）」に潰す
  4. `_kind_quality` の失敗二重減点（空応答が 2 乗で効く）
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402
from tests import _tmpstate  # noqa: E402


def _tool(name):
    return next(t for t in S.TOOLS if t["name"] == name)


class DevdocsDocWiring(unittest.TestCase):
    """`doc` が docs.json（一覧）ではなく docset の index.json に届くこと。"""

    def setUp(self):
        _tmpstate.isolated_state("fa-fix-v0403-")

    def test_tool_lookup_passes_doc_to_the_docset_index(self):
        urls = []

        def fake_kb_new_json(url, *a, **kw):
            urls.append(url)
            return [], "mocked"

        with mock.patch.object(S, "_kb_new_json", side_effect=fake_kb_new_json):
            S.tool_lookup({"query": "asyncio", "sources": ["devdocs"], "doc": "python~3.14"})
        self.assertTrue(any("/python~3.14/index.json" in u for u in urls), urls)
        self.assertFalse(any("/docs.json" in u for u in urls), urls)

    def test_tool_lookup_without_doc_lists_docsets(self):
        urls = []

        def fake_kb_new_json(url, *a, **kw):
            urls.append(url)
            return [], "mocked"

        with mock.patch.object(S, "_kb_new_json", side_effect=fake_kb_new_json):
            S.tool_lookup({"query": "asyncio", "sources": ["devdocs"]})
        self.assertTrue(any("/docs.json" in u for u in urls), urls)

    def test_knowledge_lookup_forwards_doc_through_opts(self):
        captured = {}

        def fake_backend(query, limit, opts):
            captured["opts"] = dict(opts)
            return {"source": "devdocs", "citations": [], "error": ""}

        with mock.patch.dict(S.KB_BACKENDS, {"devdocs": fake_backend}):
            S.knowledge_lookup("asyncio", ["devdocs"], doc="python~3.14")
        self.assertEqual(captured["opts"].get("doc"), "python~3.14")

    def test_schema_declares_doc_for_both_tools(self):
        for name in ("freeagent_lookup", "freeagent_grounded"):
            self.assertIn("doc", _tool(name)["inputSchema"]["properties"], name)


class HandlerArgsAreInSchema(unittest.TestCase):
    """ハンドラが読む引数は schema に宣言する（規約 5・48 の窓口契約）。"""

    def test_expected_properties_exist(self):
        expected = {
            "freeagent_consult": ["basis"],
            "freeagent_grounded": ["github_kind", "exclude"],
            "freeagent_agent": ["exclude"],
            "freeagent_map": ["reduce_max_tokens", "system"],
        }
        for name, keys in expected.items():
            props = _tool(name)["inputSchema"]["properties"]
            for key in keys:
                self.assertIn(key, props, f"{name}.{key}")


class KindQualitySinglePenalty(unittest.TestCase):
    """空応答（soft）は 1 回だけ減点する（二重減点の撤去）。"""

    def row(self, ok=0, n=10, err=None):
        return {"n": n, "ok": ok, "trunc": 0, "leak": 0, "empty": 0, "err": err or {}}

    def test_soft_failure_is_penalized_once(self):
        soft = S._kind_quality(self.row(ok=6, n=10, err={"no_output": 4}))[0]
        self.assertAlmostEqual(soft, 0.6, places=3)

    def test_hard_failure_keeps_the_extra_penalty(self):
        hard = S._kind_quality(self.row(ok=6, n=10, err={"gone": 4}))[0]
        self.assertAlmostEqual(hard, 0.36, places=3)
        self.assertLess(hard, S._kind_quality(self.row(ok=6, n=10, err={"no_output": 4}))[0])

    def test_fifty_percent_model_is_not_double_penalized(self):
        # 本番実測の型: 勝率 50% の品質が旧実装の二重減点で実質 1 勝 3 敗相当に沈んでいた。
        self._seed("p/half", {"ask": self.row(ok=2, n=4)})
        self.assertAlmostEqual(S.model_quality("p/half"), 0.5, places=3)
        # 同じ成功数でもハード失敗が多い方は下（追加減点はハード失敗だけに残る）。
        self._seed("p/hard", {"ask": self.row(ok=2, n=6, err={"gone": 4})})
        self.assertLess(S._rank_score("p/hard"), S._rank_score("p/half"))

    def _seed(self, ref, kinds):
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS["models"][ref] = {"kinds": kinds, "updated_at": S.now_ts()}
        S._STATS_LOADED = True


class EmptyRetryAccounting(unittest.TestCase):
    """引き上げ再試行の失敗を「空応答」に潰さない（規約 21）。"""

    def setUp(self):
        # **テストごとに状態ディレクトリを分ける**（note_observation はディスクを読んで足すので、
        # isolated_state の「設定済みなら再利用」だと前のテストの行が積み上がる）。
        self._orig_state = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp_state = tempfile.mkdtemp(prefix="fa-fix-v0403-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp_state
        self.addCleanup(self._restore_state)
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = True

    def _restore_state(self):
        import shutil
        shutil.rmtree(self._tmp_state, ignore_errors=True)
        if self._orig_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig_state

    @staticmethod
    def _seq(effects):
        """side_effect を関数化する（list を渡すと呼び出し可能な要素が呼ばれない mock の挙動のため）。"""
        it = iter(effects)

        def call(*a, **kw):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return item

        return call

    def _run(self, side_effects, max_tokens=200):
        with mock.patch.object(S, "resolve_ref", return_value=("nous", "m:free")), \
             mock.patch.object(S, "provider_model_allowed", return_value=True), \
             mock.patch.object(S, "_is_known_paid_model", return_value=False), \
             mock.patch.object(S, "provider_ready", return_value=True), \
             mock.patch.object(S, "cooling_refs", return_value={}), \
             mock.patch.object(S, "_candidates", return_value=["nous/m:free"]), \
             mock.patch.object(S, "_gated_call_once", side_effect=self._seq(side_effects)), \
             mock.patch.object(S, "PROVIDER_GATE") as gate:
            gate.acquire.return_value = 0.0
            gate.report.return_value = {"per_provider": {}}
            return S._call_model_uncached("nous/m:free", "q", max_tokens=max_tokens)

    def test_retry_429_is_not_recorded_as_model_fault(self):
        empty = {"text": "", "latency_s": 0.1, "finish_reason": "length", "truncated": True,
                 "redactions": 0, "tokens": {"prompt": 10, "completion": 200}}
        out = self._run([(dict(empty), 0.0),
                         S.HttpStatusError(429, "upstream", None)])
        self.assertIn("再試行（max_tokens=600）でも失敗", out["error"], out)
        self.assertIn("429", out["error"], out)
        # モデルの成績には空応答（no_output）として記録しない（429 は提供元の都合）。
        row = (S._STATS["models"].get("nous/m:free") or {}).get("kinds", {}).get("ask") or {}
        self.assertNotIn("no_output", row.get("err") or {}, row)
        self.assertEqual(row.get("err", {}).get("rate_limited"), 1.0, row)

    def test_retry_env_failure_breaks_without_recording(self):
        empty = {"text": "", "latency_s": 0.1, "finish_reason": "length", "truncated": True,
                 "redactions": 0, "tokens": {}}
        out = self._run([(dict(empty), 0.0),
                         OSError("connection refused")])
        self.assertIn("環境障害", out["error"], out)
        self.assertEqual(S._STATS["models"].get("nous/m:free"), None)

    def test_persistent_empty_reports_finish_reason_and_retry_tokens(self):
        empty = {"text": "", "latency_s": 0.1, "finish_reason": "length", "truncated": True,
                 "redactions": 0, "tokens": {"prompt": 10, "completion": 200}}
        out = self._run([(dict(empty), 0.0), (dict(empty), 0.0)])
        self.assertIn("finish_reason=length", out["error"], out)
        self.assertIn("再試行 600", out["error"], out)  # max_tokens=200 → 3 倍の 600
        row = S._STATS["models"]["nous/m:free"]["kinds"]["ask"]
        self.assertEqual(row["err"].get("no_output"), 1.0)
        self.assertEqual(row["trunc"], 0.0, "エラー行は trunc に二重計上しない")


class DescriptionConsistency(unittest.TestCase):
    """説明文の誤記（ask-* / search_query / story）の再発防止。"""

    def test_no_underscore_ask_pattern_in_tool_descriptions(self):
        import json
        blob = json.dumps(S.TOOLS, ensure_ascii=False)
        self.assertNotIn("ask_*", blob)

    def test_grounded_sources_refer_to_search_query(self):
        desc = _tool("freeagent_grounded")["inputSchema"]["properties"]["sources"]["description"]
        self.assertNotIn("question は英語の語", desc)
        self.assertEqual(desc.count("search_query は英語の語"), 3, desc[:200])
        self.assertNotIn("故事的", desc)

    def test_lookup_keeps_query_wording(self):
        desc = _tool("freeagent_lookup")["inputSchema"]["properties"]["sources"]["description"]
        self.assertIn("query は英語の語", desc)


if __name__ == "__main__":
    unittest.main()
