"""診断の測定精度（v0.42.1）.

固定する契約:
  * `traces` の 1 行に `finish_reason` / `max_tokens` / `retries` が入る
    （空応答が「予算切れ（length）」か「stop で空」かを後から切り分けるため）。
  * `freeagent_diagnose` は **本番（tool / run を持つ行）と ベンチ直呼びを分けて**返す。
  * advice の根拠は、本番が `DIAG_PROD_MIN` 件以上あれば**本番だけ**
    （ベンチの連打で「本番の次の一手」が歪まない）。
  * `finish_reason=length` の件数を別枠で数える。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import freeagent_bind.server as S  # noqa: E402


def _row(ok=True, tool=None, run=None, kind="ask", error=None, finish_reason=None, max_tokens=None,
         ref="nous/a:free", ts=None):
    row = {"run": run, "tool": tool, "kind": kind, "ref": ref, "ok": ok,
           "error": error, "error_class": S.classify_error(error) if error else None,
           "served_by": ref, "answer_len": 10 if ok else 0, "truncated": finish_reason == "length",
           "latency_s": 1.0 if ok else 0.0, "tokens": {}, "retries": 0,
           "finish_reason": finish_reason, "max_tokens": max_tokens,
           "ts": ts if ts is not None else S.now_ts()}
    return row


class _StateDir(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-diag-v0421-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self.addCleanup(self._restore)
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = False

    def _restore(self):
        if self._orig is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig

    def write_traces(self, rows):
        path = S.trace_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path

    def read_traces(self):
        with open(S.trace_path(), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class TraceColumns(_StateDir):
    def test_trace_call_records_finish_reason_and_budget(self):
        S.trace_call("consult", "nous/a:free", False, error="空応答（nous/a:free）",
                     finish_reason="length", max_tokens=800, retries=1, run="r1", tool="freeagent_consult")
        row = self.read_traces()[-1]
        self.assertEqual(row["finish_reason"], "length")
        self.assertEqual(row["max_tokens"], 800)
        self.assertEqual(row["retries"], 1)
        self.assertEqual(row["error_class"], "no_output")

    def test_observe_call_copies_finish_reason_from_the_result(self):
        S.observe_call({"ref": "nous/b:free", "error": "空応答（nous/b:free）",
                        "finish_reason": "stop", "max_tokens": 400, "truncated": False},
                       "ask", text="")
        row = self.read_traces()[-1]
        self.assertEqual(row["finish_reason"], "stop")
        self.assertEqual(row["max_tokens"], 400)

    def test_success_row_carries_the_budget_that_was_used(self):
        S.observe_call({"ref": "nous/c:free", "error": "", "finish_reason": "stop",
                        "max_tokens": 512, "retries": 0}, "ask", text="42")
        row = self.read_traces()[-1]
        self.assertEqual(row["finish_reason"], "stop")
        self.assertEqual(row["max_tokens"], 512)


class ChannelSplit(_StateDir):
    def test_production_and_bench_are_counted_separately(self):
        rows = ([_row(ok=True, tool="freeagent_think", run="r%d" % i) for i in range(12)] +
                [_row(ok=False, tool="freeagent_think", run="b%d" % i,
                      error="空応答（nous/a:free）") for i in range(3)] +
                [_row(ok=False, kind="eval", error="空応答（nous/a:free）") for _ in range(20)])
        self.write_traces(rows)
        d = S.tool_diagnose({"days": 7})
        chans = d["totals_by_channel"]
        self.assertEqual(chans["tool"]["calls"], 15)
        self.assertEqual(chans["tool"]["failed"], 3)
        self.assertEqual(chans["bench"]["calls"], 20)
        self.assertEqual(chans["bench"]["failed"], 20)
        self.assertEqual(d["totals"]["calls"], 35)

    def test_advice_uses_production_rows_when_there_are_enough(self):
        rows = ([_row(ok=True, tool="freeagent_think", run="r%d" % i) for i in range(12)] +
                [_row(ok=True, kind="eval") for _ in range(30)])
        self.write_traces(rows)
        d = S.tool_diagnose({"days": 7})
        self.assertEqual(d["advice_channel"], "tool")
        self.assertEqual(d["advice"]["kind"], "ok",
                         "ベンチ 30 件の失敗ではなく本番 12 件（全成功）を見ること")

    def test_advice_falls_back_to_all_rows_when_production_is_thin(self):
        rows = ([_row(ok=True, tool="freeagent_think", run="r0")] +
                [_row(ok=False, kind="eval", error="空応答（nous/a:free）") for _ in range(5)])
        self.write_traces(rows)
        d = S.tool_diagnose({"days": 7})
        self.assertEqual(d["advice_channel"], "all")

    def test_bench_rows_do_not_trigger_the_run_gap_note(self):
        self.write_traces([_row(ok=True, kind="eval") for _ in range(12)])
        d = S.tool_diagnose({"days": 7})
        joined = " ".join(d["notes"])
        self.assertIn("ベンチ直呼び", joined)
        self.assertNotIn("run を持たない本番の行", joined)


class FinishReasonRollup(_StateDir):
    def test_length_is_counted_and_noted(self):
        rows = ([_row(ok=False, tool="freeagent_consult", run="r%d" % i,
                      error="空応答（nous/a:free）", finish_reason="length", max_tokens=800)
                 for i in range(4)] +
                [_row(ok=False, tool="freeagent_consult", run="s%d" % i,
                      error="空応答（nous/a:free）", finish_reason="stop", max_tokens=400)
                 for i in range(2)])
        self.write_traces(rows)
        d = S.tool_diagnose({"days": 7})
        self.assertEqual(d["by_finish_reason"], {"length": 4, "stop": 2})
        self.assertTrue(any("finish_reason=length" in note for note in d["notes"]))

    def test_render_shows_the_channels(self):
        rows = ([_row(ok=True, tool="freeagent_think", run="r%d" % i) for i in range(12)] +
                [_row(ok=False, kind="eval", error="空応答（nous/a:free）") for _ in range(3)])
        self.write_traces(rows)
        text = S.render("freeagent_diagnose", S.tool_diagnose({"days": 7}))
        self.assertIn("チャネル", text)
        self.assertIn("本番（ツール経由）", text)


if __name__ == "__main__":
    unittest.main()
