"""v0.38.0: 呼び出し単位の ID（traces / diagnose）と MCP の引数補完。ネット不要。

固定すること:
  * `run_scope` がスレッドローカルを正しく張り、**並列ワーカーへ引き継がれる**（別スレッドに伝播しないため）
  * `trace_call` が `run` / `tool` を記録し、`handle_tool_call` は**呼び出しごとに違う ID** を振る
  * `diagnose` が run 単位の内訳（失敗数・成功率・エラー内訳）を返し、`run` で絞れる
  * 旧行（run 無し）は `unattributed_calls` に数える（黙って捨てない）
  * `completion/complete` は **ref/resource のみ**補完する（ツール引数の補完は仕様に無い）
  * `initialize` の capabilities に `completions` が載る
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _tmpstate import register_state_dir, remove_tree  # noqa: E402  # type: ignore[import-not-found]

from freeagent_bind import server as S  # noqa: E402


class RunScopeTest(unittest.TestCase):
    def test_scope_sets_and_restores(self):
        self.assertEqual("", S.current_run())
        with S.run_scope("r1", "freeagent_panel"):
            self.assertEqual("r1", S.current_run())
            self.assertEqual("freeagent_panel", S.current_tool())
            with S.run_scope("r2"):
                self.assertEqual("r2", S.current_run())
                self.assertEqual("freeagent_panel", S.current_tool())  # tool は引き継ぐ
            self.assertEqual("r1", S.current_run())
        self.assertEqual("", S.current_run())
        self.assertEqual("", S.current_tool())

    def test_run_ids_are_unique_and_short(self):
        ids = {S.new_run_id() for _ in range(50)}
        self.assertEqual(50, len(ids))
        self.assertTrue(all(i.startswith("r") and len(i) == 13 for i in ids))


class TraceRunFieldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = register_state_dir(tempfile.mkdtemp(prefix="fa-run-"))
        self.path = os.path.join(self.tmp, "traces.jsonl")
        self.addCleanup(lambda: remove_tree(self.tmp))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_TRACE_PATH": self.path,
                                              "FREEAGENT_STATE_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_trace_call_uses_scope(self):
        with S.run_scope("rABC", "freeagent_ask"):
            S.trace_call("ask", "nous/x:free", True, latency_s=1.0)
        row = self.rows()[-1]
        self.assertEqual("rABC", row["run"])
        self.assertEqual("freeagent_ask", row["tool"])

    def test_explicit_values_win(self):
        with S.run_scope("rABC", "freeagent_ask"):
            S.trace_call("ask", "nous/x:free", True, run="rOTHER", tool="freeagent_map")
        row = self.rows()[-1]
        self.assertEqual("rOTHER", row["run"])
        self.assertEqual("freeagent_map", row["tool"])

    def test_parallel_workers_inherit_run(self):
        """別スレッドにはスレッドローカルが伝播しないので、`run_parallel` が包んでいることを確かめる。"""
        def worker(job):
            S.trace_call("eval", f"m/{job}", True, latency_s=0.1)
            return S.current_run()

        with S.run_scope("rPARENT", "freeagent_panel"):
            got = S.run_parallel([1, 2, 3], worker, max_workers=3)
        self.assertEqual(["rPARENT"] * 3, got)
        rows = self.rows()
        self.assertEqual(3, len(rows))
        self.assertEqual({"rPARENT"}, {r["run"] for r in rows})
        self.assertEqual({"freeagent_panel"}, {r["tool"] for r in rows})

    def test_handle_tool_call_stamps_distinct_runs(self):
        seen = []

        def fake(args):
            seen.append(S.current_run())
            return {"ok": True}

        with mock.patch.dict(S.HANDLERS, {"fake_tool": fake}, clear=False):
            S.handle_tool_call({"name": "fake_tool", "arguments": {}})
            S.handle_tool_call({"name": "fake_tool", "arguments": {}})
        self.assertEqual(2, len(seen))
        self.assertTrue(all(run.startswith("r") for run in seen))
        self.assertNotEqual(seen[0], seen[1])
        self.assertEqual("", S.current_run())  # 呼び出しの外へ漏れない


class DiagnoseRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = register_state_dir(tempfile.mkdtemp(prefix="fa-diagrun-"))
        self.path = os.path.join(self.tmp, "traces.jsonl")
        self.addCleanup(lambda: remove_tree(self.tmp))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_TRACE_PATH": self.path,
                                               "FREEAGENT_STATE_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        now = float(int(S.now_ts()))
        rows = [
            {"kind": "panel", "ref": "a/1", "ok": True, "run": "rA", "tool": "freeagent_panel",
             "error": None, "error_class": None, "served_by": "a/1", "latency_s": 1.0, "tokens": {}, "ts": now},
            {"kind": "panel", "ref": "a/2", "ok": False, "run": "rA", "tool": "freeagent_panel",
             "error": "HTTP 429: rate limit", "error_class": "rate_limited", "served_by": "a/2",
             "latency_s": 0.0, "tokens": {}, "ts": now},
            {"kind": "panel", "ref": "a/3", "ok": False, "run": "rA", "tool": "freeagent_panel",
             "error": "空応答（a/3）", "error_class": "no_output", "served_by": "a/3",
             "latency_s": 0.0, "tokens": {}, "ts": now},
            {"kind": "ask", "ref": "b/1", "ok": True, "run": "rB", "tool": "freeagent_ask",
             "error": None, "error_class": None, "served_by": "b/1", "latency_s": 2.0, "tokens": {}, "ts": now},
            {"kind": "ask", "ref": "c/1", "ok": True, "run": "", "tool": "",
             "error": None, "error_class": None, "served_by": "c/1", "latency_s": 2.0, "tokens": {}, "ts": now},
        ]
        with open(self.path, "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def test_runs_aggregation(self):
        out = S.tool_diagnose({"days": 30})
        self.assertEqual(2, out["runs_total"])
        self.assertEqual(1, out["unattributed_calls"])
        top = out["runs"][0]
        self.assertEqual("rA", top["run"])
        self.assertEqual("freeagent_panel", top["tool"])
        self.assertEqual(3, top["calls"])
        self.assertEqual(1, top["ok"])
        self.assertEqual(2, top["failed"])
        self.assertEqual(0.333, top["ok_rate"])
        self.assertEqual({"no_output": 1, "rate_limited": 1}, top["error_classes"])
        self.assertEqual("rB", out["runs"][1]["run"])

    def test_run_filter(self):
        out = S.tool_diagnose({"days": 30, "run": "rA"})
        self.assertEqual(3, out["totals"]["calls"])
        self.assertEqual(["rA"], [r["run"] for r in out["runs"]])
        self.assertEqual(0, out["unattributed_calls"])
        self.assertEqual("rA", out["window"]["filters"]["run"])
        empty = S.tool_diagnose({"days": 30, "run": "rNOPE"})
        self.assertEqual(0, empty["totals"]["calls"])
        self.assertEqual("no_data", empty["advice"]["kind"])

    def test_keys_stable_with_run_filter(self):
        base = set(S.tool_diagnose({"days": 30}))
        filtered = set(S.tool_diagnose({"days": 30, "run": "rA", "limit": 1}))
        self.assertEqual(base, filtered)

    def test_render_mentions_runs(self):
        text = S._render_diagnose(S.tool_diagnose({"days": 30}))
        self.assertIn("呼び出し単位の内訳", text)
        self.assertIn("rA", text)


class CompletionTest(unittest.TestCase):
    def test_initialize_survives_cleared_state(self):
        """状態辞書が空でも initialize が落ちない（フルスイートで KeyError になった実測の回帰）。"""
        fake = {}
        with mock.patch.object(S, "_HARNESS", fake):
            result = S.harness_on_initialize({"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "t", "version": "0"}})
            self.assertIn("announced", fake)  # 既定値が入る
        self.assertIn("completions", result["capabilities"])

    def test_capability_declared(self):
        caps = S.harness_on_initialize({"protocolVersion": "2025-06-18", "capabilities": {},
                                        "clientInfo": {"name": "t", "version": "0"}})["capabilities"]
        self.assertIn("completions", caps)

    def test_template_ref_completes_skill_uris(self):
        out = S.completion_complete({"ref": {"type": "ref/resource",
                                             "uri": "resource://freeagent/{name}"},
                                     "argument": {"name": "uri", "value": "sk"}})
        values = out["completion"]["values"]
        self.assertIn("resource://freeagent/skills", values)
        self.assertTrue(all("skills" in v for v in values))
        self.assertFalse(out["completion"]["hasMore"])

    def test_concrete_ref_uses_value_as_prefix(self):
        out = S.completion_complete({"ref": {"type": "ref/resource",
                                             "uri": "resource://freeagent/skills"},
                                     "argument": {"value": "resource://freeagent/skills/"}})
        self.assertEqual(4, out["completion"]["total"])

    def test_prompt_ref_returns_empty(self):
        out = S.completion_complete({"ref": {"type": "ref/prompt", "name": "x"},
                                     "argument": {"value": "a"}})
        self.assertEqual([], out["completion"]["values"])
        self.assertEqual(0, out["completion"]["total"])

    def test_caps_values_at_100(self):
        many = [{"uri": f"resource://freeagent/x{i}"} for i in range(150)]
        with mock.patch.object(S, "resource_list", return_value=many):
            out = S.completion_complete({"ref": {"type": "ref/resource",
                                                 "uri": "resource://freeagent/{name}"},
                                         "argument": {"value": "x"}})
        self.assertEqual(100, len(out["completion"]["values"]))
        self.assertEqual(150, out["completion"]["total"])
        self.assertTrue(out["completion"]["hasMore"])

    def test_unknown_ref_type_is_empty(self):
        out = S.completion_complete({"ref": {"type": "ref/tool", "name": "t"},
                                     "argument": {"value": "a"}})
        self.assertEqual([], out["completion"]["values"])


if __name__ == "__main__":
    unittest.main()
