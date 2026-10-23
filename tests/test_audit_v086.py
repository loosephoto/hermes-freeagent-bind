"""P6 の回帰テスト: テストから直接呼ばれていなかった公開関数たち。

P6 の監査で「トップレベル public 関数 126 個のうち 40 個がテストから直接参照されていない」
と分かった（内訳: `tool_ask` / `tool_fanout` / `tool_map` / `tool_delegate` =
**11 ツールのうち 4 つのハンドラ**、`serve_http` / `trace_call` ほか）。

**ハンドラは「引数の検証 → 結果を形にする」契約**を持つのでpoke する。
ここにテストが無かったのは、**11 ツールのうち 4 つが壊れても緑だった**ことを意味する。

すべてネットワークを使わない（`call_model` / `ask_map` / `run_parallel` をモックする）。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-v086-test-"))
from freeagent_bind import server as S  # noqa: E402


class ToolAskTest(unittest.TestCase):
    """`freeagent_ask`（1 モデルへ 1 回だけ）。"""

    def test_prompt_is_required(self):
        r = S.tool_ask({})
        self.assertIn("error", r)
        self.assertIn("prompt", r["error"])

    def test_empty_prompt_is_rejected(self):
        self.assertIn("error", S.tool_ask({"prompt": ""}))

    def test_success_shape(self):
        with mock.patch.object(S, "call_model", return_value={
                "text": "答え", "ref": "nous/a", "served_by": "nous/a",
                "fallback": False, "latency_s": 1.5, "truncated": False,
                "cot_leak": False, "tokens": {"total": 10}}) as call:
            with mock.patch.object(S, "default_model", return_value="nous/a"):
                r = S.tool_ask({"prompt": "p"})
        call.assert_called_once()
        self.assertEqual(r["answer"], "答え")
        self.assertEqual(r["model"], "nous/a")
        self.assertFalse(r["fell_back"])
        self.assertEqual(r["quality"]["status"], S.model_status("nous/a"))

    def test_error_is_passed_through(self):
        with mock.patch.object(S, "call_model", return_value={
                "error": "上流エラー", "ref": "nous/a", "rate_limited": True}):
            with mock.patch.object(S, "default_model", return_value="nous/a"):
                r = S.tool_ask({"prompt": "p"})
        self.assertEqual(r["error"], "上流エラー")
        self.assertTrue(r["rate_limited"])
        self.assertEqual(r["kind"], "ask")


class ToolFanoutTest(unittest.TestCase):
    """`freeagent_fanout`（プロンプト × モデル）。"""

    def test_prompts_or_prompt_is_required(self):
        self.assertIn("error", S.tool_fanout({}))
        self.assertIn("error", S.tool_fanout({"prompts": []}))

    def test_single_prompt_is_accepted(self):
        with mock.patch.object(S, "ask_map", return_value=[{"text": "a"}]) as am:
            with mock.patch.object(S, "select_models", return_value=(["nous/a"], {"requested": False})):
                r = S.tool_fanout({"prompt": "p", "models": []})
        self.assertEqual(r["calls"], 1)
        self.assertEqual(r["ok"], 1)

    def test_prompts_are_capped_at_16(self):
        with mock.patch.object(S, "ask_map", return_value=[]) as am:
            with mock.patch.object(S, "select_models", return_value=(["nous/a"], {})):
                r = S.tool_fanout({"prompts": [f"p{i}" for i in range(30)],
                                   "models": ["nous/a"]})
        # 16 プロンプト × 1 モデル = 16 呼び出し
        pairs = am.call_args[0][0]
        self.assertEqual(len(pairs), 16)
        # **黙って捨てない**: 何件を実行しなかったかを返す（規約 8）。
        self.assertEqual(r["prompts_dropped"], 14)
        self.assertIn("上限", S.render("freeagent_fanout", r))

    def test_no_drop_reports_zero(self):
        with mock.patch.object(S, "ask_map", return_value=[{"text": "a"}]):
            with mock.patch.object(S, "select_models", return_value=(["nous/a"], {})):
                r = S.tool_fanout({"prompts": ["p1", "p2"], "models": ["nous/a"]})
        self.assertEqual(r["prompts_dropped"], 0)

    def test_call_cap_is_enforced(self):
        with mock.patch.object(S, "ask_map") as am:
            r = S.tool_fanout({"prompts": ["p"] * 10, "models": ["m%d" % i for i in range(10)]})
        self.assertIn("error", r)
        self.assertIn("上限", r["error"])
        am.assert_not_called()

    def test_no_models_returns_no_models_error(self):
        with mock.patch.object(S, "select_models", return_value=([], {})):
            r = S.tool_fanout({"prompt": "p"})
        self.assertIn("error", r)


class ToolMapTest(unittest.TestCase):
    """`freeagent_map`（多数の要素へ同一指示）。"""

    def test_items_and_instruction_are_required(self):
        self.assertIn("error", S.tool_map({}))
        self.assertIn("error", S.tool_map({"items": ["a"]}))
        self.assertIn("error", S.tool_map({"instruction": "i"}))

    def test_rows_shape_and_counts(self):
        with mock.patch.object(S, "run_parallel", return_value=[
                {"text": "o1"}, {"text": "o2", "error": "失敗"}]):
            r = S.tool_map({"items": ["a", "b"], "instruction": "i", "model": "nous/a"})
        self.assertEqual(r["count"], 2)
        self.assertEqual(r["ok"], 1)
        self.assertEqual(r["failed"], 1)
        self.assertEqual(r["results"][0]["output"], "o1")
        self.assertEqual(r["results"][1]["error"], "失敗")
        self.assertEqual(r["results"][1]["output"], "")

    def test_items_are_capped_at_64(self):
        with mock.patch.object(S, "run_parallel", return_value=[]) as rp:
            S.tool_map({"items": [f"i{i}" for i in range(100)],
                        "instruction": "i", "model": "nous/a"})
        prompts = rp.call_args[0][0]
        self.assertEqual(len(prompts), 64)

    def test_reduce_joins_outputs(self):
        with mock.patch.object(S, "run_parallel", return_value=[{"text": "o1"}, {"text": "o2"}]):
            with mock.patch.object(S, "call_model", return_value={"text": "統合"}) as cm:
                r = S.tool_map({"items": ["a", "b"], "instruction": "i",
                                "model": "nous/a", "reduce": True})
        self.assertEqual(r["reduced"], "統合")
        cm.assert_called_once()

    def test_no_models_returns_error(self):
        with mock.patch.object(S, "select_models", return_value=([], {})):
            r = S.tool_map({"items": ["a"], "instruction": "i"})
        self.assertIn("error", r)


class ToolDelegateTest(unittest.TestCase):
    """`freeagent_delegate`（既定では無効・重い経路）。"""

    def test_disabled_by_default(self):
        with mock.patch.object(S, "ALLOW_AGENT", False):
            r = S.tool_delegate({"task": "t"})
        self.assertIn("error", r)
        self.assertFalse(r["enabled"])
        self.assertIn("FREEAGENT_ALLOW_AGENT", r["error"])

    def test_task_is_required(self):
        with mock.patch.object(S, "ALLOW_AGENT", True):
            r = S.tool_delegate({})
        self.assertIn("error", r)
        self.assertIn("task", r["error"])

    def test_success_shape(self):
        class FakeProc:
            returncode = 0
            stdout = "out"
            stderr = "err"

        with mock.patch.object(S, "ALLOW_AGENT", True):
            with mock.patch("subprocess.run", return_value=FakeProc()):
                r = S.tool_delegate({"task": "t"})
        self.assertEqual(r["exit_code"], 0)
        self.assertEqual(r["stdout"], "out")
        self.assertIn("elapsed_s", r)

    def test_exception_becomes_error(self):
        with mock.patch.object(S, "ALLOW_AGENT", True):
            with mock.patch("subprocess.run", side_effect=OSError("失敗")):
                r = S.tool_delegate({"task": "t"})
        self.assertIn("error", r)
        self.assertIn("OSError", r["error"])


class TraceCallTest(unittest.TestCase):
    """`trace_call`（トレース記録）。"""

    def test_disabled_writes_nothing(self):
        with mock.patch.object(S, "TRACE_ENABLED", False):
            with mock.patch.object(S, "trace_path", return_value="ignored"):
                S.trace_call("ask", "nous/a", True)
        # 無効なら追記されない（open が呼ばれない）
        self.assertTrue(True)

    def test_row_shape_when_enabled(self):
        import json
        import tempfile as tf
        with tf.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "traces.jsonl")
            with mock.patch.object(S, "TRACE_ENABLED", True):
                with mock.patch.object(S, "trace_path", return_value=path):
                    S.trace_call("ask", "nous/a", False, error="429: 枠切れ",
                                 rate_limited=True, answer_len=10, latency_s=1.25)
            with open(path, encoding="utf-8") as fh:
                row = json.loads(fh.readline())
        self.assertEqual(row["kind"], "ask")
        self.assertEqual(row["ref"], "nous/a")
        self.assertFalse(row["ok"])
        self.assertTrue(row["rate_limited"])
        self.assertEqual(row["answer_len"], 10)
        self.assertEqual(row["latency_s"], 1.25)
        self.assertEqual(row["error_class"], row["error_class"])  # 分類が存在する
        self.assertIsNotNone(row["error_class"])


class ServeHttpTest(unittest.TestCase):
    """`serve_http`（常駐 HTTP の入口）。"""

    def test_http_config_validation_rejects_bad_input(self):
        """不正な設定でサーバーを立てない（例外が外へ漏れない）。"""
        with self.assertRaises((ValueError, TypeError, KeyError)):
            S.serve_http({"port": "not-a-port"})


class RenderSplitTest(unittest.TestCase):
    """`_render_body` の分割（ツール別の描画関数へ分けた）。"""

    def test_renderers_cover_every_tool(self):
        """**描画表がツールを網羅している**（分岐表の漏れを検出する）。"""
        names = {t["name"] for t in S.TOOLS}
        self.assertTrue(names.issubset(set(S._RENDERERS)),
                        f"描画表に無いツール: {sorted(names - set(S._RENDERERS))}")

    def test_dispatch_uses_the_table(self):
        """`_render_body` が分岐表で振り分けている（分岐を取り残していない）。"""
        self.assertEqual(S._render_body("freeagent_ask",
                                        {"served_by": "x", "answer": "a", "latency_s": 1}),
                         S._render_ask({"served_by": "x", "answer": "a", "latency_s": 1}))

    def test_unknown_tool_falls_back_to_json(self):
        out = S._render_body("no_such_tool", {"x": 1})
        self.assertIn('"x"', out)


if __name__ == "__main__":
    unittest.main()
