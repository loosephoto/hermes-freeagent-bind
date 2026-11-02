"""v0.37.0: `freeagent_diagnose`（traces の決定論診断）とスキル Resource（段階開示）。ネット不要。

固定すること:
  * 集計（成功率・失敗の内訳・分位・モデル別・直近）が**与えた traces と一致**する
  * `error_class` の語彙外は `unknown` として落ちる（黙って消さない）
  * 窓（days）・絞り込み（kind / model）が効き、**返り値のキーはモードで変わらない**
  * traces が無い・壊れている行がある でも例外を出さない（「無い」と返す）
  * **モデルもネットワークも呼ばない**（`provider_http` を爆発するスタブにして確かめる）
  * スキル Resource は目録から 4 本たどれて、中身が実装の定数と一致する
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402

NOW = float(int(S.now_ts()))  # 実時計に合わせる（未来の時刻を窓に入れない）


def row(kind="ask", ref="nous/x:free", ok=True, error="", error_class=None,
        latency=1.5, served_by=None, ts=None):
    return {"kind": kind, "ref": ref, "ok": ok, "error": error or None,
            "error_class": error_class if error_class is not None else (S.classify_error(error) if error else None),
            "served_by": served_by or ref, "latency_s": latency,
            "tokens": {}, "ts": NOW if ts is None else ts}


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-diag-")
        self.path = os.path.join(self.tmp, "traces.jsonl")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_TRACE_PATH": self.path,
                                                "FREEAGENT_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(mock.patch.stopall)

    def write(self, rows, extra_lines=()):
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            for line in extra_lines:
                fh.write(line + "\n")

    def test_totals_and_classes(self):
        self.write([
            row(latency=1.0), row(latency=3.0),
            row(ok=False, error="HTTP 429: rate limit", latency=0.0),
            row(ok=False, error="HTTP 429: rate limit", latency=0.0),
            row(ok=False, error="timeout talking to upstream", latency=0.0),
            row(ok=False, error="空応答（nous/x:free）。max_tokens=64", latency=0.0),
            row(ok=True, error="", latency=5.0),
        ])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual(7, out["totals"]["calls"])
        self.assertEqual(3, out["totals"]["ok"])
        self.assertEqual(4, out["totals"]["failed"])
        self.assertEqual(0.429, out["totals"]["ok_rate"])
        self.assertEqual({"rate_limited": 2, "timeout": 1, "no_output": 1}, out["by_error_class"])
        self.assertEqual([3.0, 5.0, 5.0], [out["latency"]["p50"], out["latency"]["p90"], out["latency"]["max"]])
        self.assertEqual(3, out["latency"]["n"])
        self.assertEqual(0, out["model_calls"])
        self.assertEqual(4, len(out["recent_failures"]))  # 失敗 4 件（既定 limit=5）

    def test_advice_rate_limited(self):
        self.write([row(ok=False, error="HTTP 429 rate limit") for _ in range(4)] + [row()])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual("rate_limited", out["advice"]["kind"])
        self.assertIn("FREEAGENT_QUOTA_LIMITS", out["advice"]["check"])
        self.assertIn("429", out["advice"]["advice"])

    def test_advice_empty_output(self):
        self.write([row(ok=False, error="空応答（nous/x:free）。max_tokens=64 を思考で使い切った") for _ in range(3)]
                   + [row(), row(), row()])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual("empty_output", out["advice"]["kind"])
        self.assertIn("max_tokens", out["advice"]["advice"])

    def test_advice_one_model(self):
        self.write([row(ok=False, ref="nous/bad:free", error="HTTP 500: upstream", latency=0.0) for _ in range(5)]
                   + [row(ref="nous/good:free") for _ in range(5)])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual("one_model", out["advice"]["kind"])
        self.assertIn("nous/bad:free", out["advice"]["advice"])

    def test_advice_ok_when_no_failures(self):
        self.write([row(), row()])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual("ok", out["advice"]["kind"])

    def test_advice_no_data(self):
        out = S.tool_diagnose({"days": 7})
        self.assertEqual(0, out["totals"]["calls"])
        self.assertIsNone(out["totals"]["ok_rate"])
        self.assertEqual("no_data", out["advice"]["kind"])
        self.assertFalse(out["trace"]["exists"])

    def test_window_filters_old_rows(self):
        self.write([row(ts=NOW - 40 * 86400), row(ts=NOW)])
        out = S.tool_diagnose({"days": 7})
        self.assertEqual(1, out["totals"]["calls"])
        out = S.tool_diagnose({"days": 90})
        self.assertEqual(2, out["totals"]["calls"])

    def test_kind_and_model_filters(self):
        self.write([row(kind="ask", ref="a/one"), row(kind="panel", ref="b/two"),
                    row(kind="ask", ref="b/two")])
        self.assertEqual(2, S.tool_diagnose({"days": 30, "kind": "ask"})["totals"]["calls"])
        self.assertEqual(2, S.tool_diagnose({"days": 30, "model": "b/two"})["totals"]["calls"])
        self.assertEqual(1, S.tool_diagnose({"days": 30, "kind": "panel", "model": "b/two"})["totals"]["calls"])

    def test_keys_do_not_change_by_mode(self):
        self.write([row(), row(ok=False, error="HTTP 429")])
        base = set(S.tool_diagnose({"days": 30}))
        filtered = set(S.tool_diagnose({"days": 30, "kind": "ask", "model": "nous/x:free", "limit": 1}))
        self.assertEqual(base, filtered)

    def test_corrupt_lines_are_counted_not_fatal(self):
        self.write([row()], extra_lines=["{not json", "[]", ""])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual(1, out["totals"]["calls"])
        self.assertGreaterEqual(out["trace"]["unreadable"], 2)
        self.assertFalse(out.get("error"))

    def test_unknown_error_class_is_kept(self):
        self.write([row(ok=False, error="謎の失敗", error_class="weird")])
        out = S.tool_diagnose({"days": 30})
        self.assertEqual({"weird": 1}, out["by_error_class"])

    def test_ordinals_with_few_points(self):
        self.write([row(latency=2.0), row(latency=10.0)])
        lat = S.tool_diagnose({"days": 30})["latency"]
        self.assertEqual(2, lat["n"])
        self.assertEqual(2.0, lat["p50"])  # [2,10] の中央は下側
        self.assertEqual(10.0, lat["max"])
        self.assertIn(lat["p95"], (2.0, 10.0))

    def test_slowest_models_need_minimum_calls(self):
        self.write([row(ref="a/slow", latency=9.0), row(ref="a/slow", latency=9.0),
                    row(ref="a/slow", latency=9.0), row(ref="b/fast", latency=0.5)])
        slow = S.tool_diagnose({"days": 30})["slowest_models"]
        self.assertEqual(["a/slow"], [r["model"] for r in slow])

    def test_does_not_call_models_or_network(self):
        self.write([row(), row(ok=False, error="HTTP 429")])
        with mock.patch.object(S, "provider_http", side_effect=AssertionError("HTTP 禁止")), \
             mock.patch.object(S, "call_model", side_effect=AssertionError("推論禁止")):
            out = S.tool_diagnose({"days": 30})
        self.assertFalse(out.get("error"))

    def test_handle_tool_call_renders_summary(self):
        self.write([row(), row(ok=False, error="HTTP 429: rate limit")])
        out = S.handle_tool_call({"name": "freeagent_diagnose", "arguments": {"days": 30}})
        text = out["content"][0]["text"]
        self.assertIn("診断", text)
        self.assertIn("失敗", text)
        self.assertIn("次の一手", text)
        self.assertFalse(out["isError"], text)

    def test_render_survives_missing_keys(self):
        text = S._render_diagnose({})  # 例外を出さない（キー欠けで本文が消えない）
        self.assertIn("診断", text)


class SkillResourceTest(unittest.TestCase):
    SKILLS = ("plan", "review", "debug", "grounded")

    def test_index_lists_every_skill(self):
        text = S.resource_read("resource://freeagent/skills")
        for name in self.SKILLS:
            self.assertIn(f"skills/{name}", text)

    def test_each_skill_is_readable_and_grounded_in_constants(self):
        for name in self.SKILLS:
            text = S.resource_read(f"resource://freeagent/skills/{name}")
            self.assertTrue(text and text.startswith("#"), name)
            self.assertGreater(len(text), 200, name)
        self.assertIn(str(S.QUESTION_CAP), S.resource_read("resource://freeagent/skills/review"))
        self.assertIn(S.REVIEW_AXES[0], S.resource_read("resource://freeagent/skills/review"))
        self.assertIn(f"{S.KB_DEADLINE:g}", S.resource_read("resource://freeagent/skills/grounded"))

    def test_unknown_skill_is_none(self):
        self.assertIsNone(S.resource_read("resource://freeagent/skills/nope"))

    def test_list_contains_skills(self):
        uris = [r["uri"] for r in S.resource_list()]
        self.assertIn("resource://freeagent/skills", uris)
        for name in self.SKILLS:
            self.assertIn(f"resource://freeagent/skills/{name}", uris)


if __name__ == "__main__":
    unittest.main()
