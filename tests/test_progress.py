"""`notifications/progress`（規約 66）の回帰テスト.

固定する契約（実測の型をそのまま書く）:

* クライアントが `_meta.progressToken` を出したときだけ送る（**出さなければ 1 件も送らない**）
* 送り先は transport ごとに違う（stdio = stdout の 1 行 / HTTP = セッションの SSE 待ち行列）
* `run_parallel` の回収 1 件ごとに 1 件、最後は必ず `total` に届く
* 予算切れのときは「打ち切り」を 1 件足す（黙って途中で止めない）
* 進捗の送信が失敗しても**本体の結果は壊さない**

ネットワークは使わない（worker はスタブ）。
"""

import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


class _Capture:
    """送り手の代わりに note を溜める（transport に依存せず確かめるため）."""

    def __init__(self, boom: bool = False):
        self.notes: list = []
        self.lock = threading.Lock()
        self.boom = boom

    def __call__(self, note):
        if self.boom:
            raise RuntimeError("送り先が落ちている")
        with self.lock:
            self.notes.append(note)

    def progress_values(self):
        with self.lock:
            return [n["params"]["progress"] for n in self.notes]


class ProgressTokenTest(unittest.TestCase):
    """`_meta.progressToken` の読み方（str / int だけを受ける）."""

    def test_accepts_str_and_int(self):
        self.assertEqual(S._progress_token_of({"name": "x", "_meta": {"progressToken": "t1"}}), "t1")
        self.assertEqual(S._progress_token_of({"_meta": {"progressToken": 7}}), 7)
        self.assertEqual(S._progress_token_of({"_meta": {"progressToken": 0}}), 0)

    def test_rejects_everything_else(self):
        for params in (None, "nope", [], {}, {"_meta": None}, {"_meta": []}, {"_meta": {}},
                       {"_meta": {"progressToken": None}}, {"_meta": {"progressToken": True}},
                       {"_meta": {"progressToken": 1.5}}, {"_meta": {"progressToken": ["t"]}}):
            self.assertIsNone(S._progress_token_of(params), repr(params))


class ProgressLabelTest(unittest.TestCase):
    def test_label_is_short_single_line(self):
        self.assertEqual(S.progress_label("wikipedia"), "wikipedia")
        self.assertEqual(S.progress_label(("nous/x:free", "prompt")), "nous/x:free")
        self.assertEqual(len(S.progress_label("a" * 200)), 40)
        self.assertNotIn("\n", S.progress_label("a\nb"))
        self.assertEqual(S.progress_label({"not": "a job"}), "")


class RunParallelProgressTest(unittest.TestCase):
    """`run_parallel` が出す note の中身（呼び出し側スレッドで回収する）."""

    def setUp(self):
        self.capture = _Capture()
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "sink", None)
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "maker", None)

    def _run(self, jobs, *, token="tok", workers=4, deadline=None, worker=None):
        worker = worker or (lambda job: {"ok": job})
        with S.bound_progress(lambda t: self.capture):
            with S.progress_for({"_meta": {"progressToken": token}} if token else {}):
                return S.run_parallel(jobs, worker, max_workers=workers, deadline=deadline)

    def test_one_note_per_slot_and_finishes_at_total(self):
        out = self._run(["a", "b", "c"])
        self.assertEqual([r["ok"] for r in out], ["a", "b", "c"])
        self.assertEqual(sorted(self.capture.progress_values()), [1.0, 2.0, 3.0])
        for note in self.capture.notes:
            self.assertEqual(note["method"], "notifications/progress")
            self.assertEqual(note["params"]["progressToken"], "tok")
            self.assertEqual(note["params"]["total"], 3.0)
            self.assertNotIn("id", note)          # 通知に id を付けない（MCP 仕様）
        self.assertEqual(max(self.capture.progress_values()), 3.0)

    def test_sequential_path_reports_too(self):
        self._run(["a", "b"], workers=1)
        self.assertEqual(sorted(self.capture.progress_values()), [1.0, 2.0])

    def test_no_token_sends_nothing(self):
        self._run(["a", "b"], token=None)
        self.assertEqual(self.capture.notes, [])

    def test_env_flag_off_sends_nothing(self):
        with mock.patch.object(S, "PROGRESS_ENABLED", False):
            self._run(["a", "b"])
        self.assertEqual(self.capture.notes, [])

    def test_timeout_reports_the_cut_and_keeps_partial_results(self):
        def slow(job):
            time.sleep(0.4)
            return {"ok": job}

        out = self._run(["a", "b"], workers=2, deadline=S.Deadline(0.05), worker=slow)
        self.assertTrue(all(r.get("dropped") for r in out))
        self.assertEqual(self.capture.notes[-1]["params"]["message"], "予算切れで打ち切りました")
        self.assertEqual(self.capture.notes[-1]["params"]["total"], 2.0)

    def test_sender_failure_does_not_break_the_call(self):
        broken = _Capture(boom=True)
        with S.bound_progress(lambda t: broken):
            with S.progress_for({"_meta": {"progressToken": "tok"}}):
                out = S.run_parallel(["a", "b"], lambda job: {"ok": job}, max_workers=2)
        self.assertEqual([r["ok"] for r in out], ["a", "b"])

    def test_binding_is_restored_after_the_block(self):
        self.assertIsNone(getattr(S._PROGRESS_LOCAL, "sink", None))
        self._run(["a"])
        self.assertIsNone(getattr(S._PROGRESS_LOCAL, "sink", None))
        self.assertEqual(len(self.capture.notes), 1)


class StdioProgressTest(unittest.TestCase):
    """stdio: `notifications/progress` が stdout に 1 行で出る."""

    def setUp(self):
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "maker", None)
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "sink", None)

    def test_tool_call_with_token_writes_progress_lines(self):
        lines: list = []

        def handler(args):
            return {"rows": S.run_parallel(args["jobs"], lambda job: {"ok": job}, max_workers=2)}

        with mock.patch.dict(S.HANDLERS, {"probe_tool": handler}), \
             mock.patch.object(S, "write_line", lines.append):
            with S.bound_progress(S._stdio_progress_maker):
                result = S.handle_tool_call({"name": "probe_tool", "arguments": {"jobs": ["a", "b"]},
                                             "_meta": {"progressToken": "tok-1"}})
        self.assertFalse(result.get("isError"))
        notes = [json.loads(line) for line in lines]
        self.assertEqual([n["method"] for n in notes], ["notifications/progress"] * 2)
        self.assertEqual([n["params"]["progress"] for n in notes], [1.0, 2.0])
        self.assertEqual({n["params"]["progressToken"] for n in notes}, {"tok-1"})
        self.assertNotIn("id", notes[0])

    def test_tool_call_without_token_writes_nothing(self):
        lines: list = []

        def handler(args):
            return {"rows": S.run_parallel(args["jobs"], lambda job: {"ok": job}, max_workers=2)}

        with mock.patch.dict(S.HANDLERS, {"probe_tool": handler}), \
             mock.patch.object(S, "write_line", lines.append):
            with S.bound_progress(S._stdio_progress_maker):
                S.handle_tool_call({"name": "probe_tool", "arguments": {"jobs": ["a", "b"]}})
        self.assertEqual(lines, [])


class HttpProgressTest(unittest.TestCase):
    """HTTP: 進捗は本文に載らず、セッションの SSE 待ち行列へ回る."""

    def setUp(self):
        self.sid = S._http_session_new()
        self.addCleanup(S._http_session_drop, self.sid)
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "maker", None)
        self.addCleanup(setattr, S._PROGRESS_LOCAL, "sink", None)

    def _tool(self):
        def handler(args):
            return {"rows": S.run_parallel(args["jobs"], lambda job: {"ok": job}, max_workers=2)}
        return handler

    def test_progress_goes_to_the_session_queue(self):
        session = S._http_session_get(self.sid)
        with mock.patch.dict(S.HANDLERS, {"probe_tool": self._tool()}):
            payload, accepted = S._http_process(
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "probe_tool", "arguments": {"jobs": ["a", "b"]},
                            "_meta": {"progressToken": "tok-http"}}},
                session)
        self.assertFalse(accepted)
        self.assertEqual(payload["id"], 5)
        self.assertFalse(payload["result"]["isError"])
        # 通知は本文に混ざらず、待ち行列にだけ入る（MCP 仕様）
        self.assertTrue(all(isinstance(r, dict) and "id" in r for r in [payload]))
        queued = list(session["queue"])
        self.assertEqual([n["method"] for n in queued], ["notifications/progress"] * 2)
        self.assertEqual(queued[-1]["params"]["progressToken"], "tok-http")

    def test_without_token_the_queue_stays_empty(self):
        session = S._http_session_get(self.sid)
        with mock.patch.dict(S.HANDLERS, {"probe_tool": self._tool()}):
            S._http_process(
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                 "params": {"name": "probe_tool", "arguments": {"jobs": ["a", "b"]}}},
                session)
        self.assertEqual(session["queue"], [])

    def test_queue_notification_is_not_used_when_session_is_missing(self):
        # セッションが無い（統計用の初期化前など）でも落ちない
        with mock.patch.dict(S.HANDLERS, {"probe_tool": self._tool()}):
            payload, _ = S._http_process(
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                 "params": {"name": "probe_tool", "arguments": {"jobs": ["a"]},
                            "_meta": {"progressToken": "tok"}}},
                None)
        self.assertEqual(payload["id"], 7)


if __name__ == "__main__":
    unittest.main()
