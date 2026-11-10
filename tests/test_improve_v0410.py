# -*- coding: utf-8 -*-
"""v0.41.0 の改良の回帰テスト。

* 空応答の引き上げ再試行は「length 枯渇（または不明）」のときだけ
* `FREEAGENT_RANK_KIND_BIAS`（既定 off・off は同一の並び）
* `freeagent_diagnose` はローテーション（`.1`）を併読する
* `admission.jsonl` はローテーションし、読みは末尾だけをメモリに載せる
* 説明文の頭 140 字に和文に隣接する ASCII 空白が無い（遅延カタログの切り詰め対策）
* `apply_proactive.py` の SNIPPET に遅延カタログの導線が書いてある
"""
import importlib.util
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


def _tool(name):
    return next(t for t in S.TOOLS if t["name"] == name)


def _empty(finish="length"):
    return {"text": "", "latency_s": 0.1, "finish_reason": finish, "truncated": finish == "length",
            "redactions": 0, "tokens": {"prompt": 10, "completion": 200}}


class RetryOnlyWhenLengthStarved(unittest.TestCase):
    def setUp(self):
        self._orig_state = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-imp-v0410-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(self._restore)
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = True

    def _restore(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._orig_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig_state

    def _run(self, first, second):
        calls = []

        def gated(*a, **kw):
            calls.append(a)
            if len(calls) == 1:
                return first, 0.0
            return second, 0.0

        with mock.patch.object(S, "resolve_ref", return_value=("nous", "m:free")), \
             mock.patch.object(S, "provider_model_allowed", return_value=True), \
             mock.patch.object(S, "_is_known_paid_model", return_value=False), \
             mock.patch.object(S, "provider_ready", return_value=True), \
             mock.patch.object(S, "cooling_refs", return_value={}), \
             mock.patch.object(S, "_candidates", return_value=["nous/m:free"]), \
             mock.patch.object(S, "_gated_call_once", side_effect=gated), \
             mock.patch.object(S, "PROVIDER_GATE") as gate:
            gate.acquire.return_value = 0.0
            gate.report.return_value = {"per_provider": {}}
            out = S._call_model_uncached("nous/m:free", "q", max_tokens=200)
        return out, calls

    def test_stop_empty_is_not_retried(self):
        out, calls = self._run(_empty(finish="stop"), _empty(finish="stop"))
        self.assertEqual(len(calls), 1, "stop で空のときは再試行しない")
        self.assertIn("空応答", out["error"], out)

    def test_length_empty_is_retried(self):
        out, calls = self._run(_empty(finish="length"), _empty(finish="length"))
        self.assertEqual(len(calls), 2, "length 枯渇は引き上げ再試行する")
        self.assertIn("再試行 600", out["error"], out)

    def test_unknown_finish_is_retried(self):
        _, calls = self._run(_empty(finish=""), _empty(finish=""))
        self.assertEqual(len(calls), 2, "finish_reason 不明は保守側（再試行）に倒す")


class KindBiasSelection(unittest.TestCase):
    def _seed(self, ref, kinds):
        S._STATS["models"][ref] = {"kinds": kinds, "updated_at": S.now_ts()}

    def setUp(self):
        self._orig_state = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-imp-v0410-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(self._restore)
        S._STATS.clear()
        S._STATS.update({"models": {}, "updated_at": 0.0})
        S._STATS_LOADED = True

    def _restore(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._orig_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig_state

    def test_bias_off_keeps_the_same_order(self):
        self._seed("p/emptykind", {"consult": {"n": 10.0, "ok": 0.0, "trunc": 0.0, "leak": 0.0,
                                               "empty": 0.0, "err": {"no_output": 10.0}, "lat_ms": 0.0}})
        self._seed("p/cleankind", {"consult": {"n": 10.0, "ok": 10.0, "trunc": 0.0, "leak": 0.0,
                                               "empty": 0.0, "err": {}, "lat_ms": 0.0}})
        refs = ["p/emptykind", "p/cleankind"]
        with mock.patch.object(S, "RANK_KIND_BIAS", False):
            off = S._order_for_selection(refs, "consult")
        with mock.patch.object(S, "RANK_KIND_BIAS", True):
            on = S._order_for_selection(refs, "consult")
        self.assertEqual(off, ["p/cleankind", "p/emptykind"], "off でも品質順で空応答モデルは下")
        self.assertEqual(on, ["p/cleankind", "p/emptykind"])

    def test_bias_demotes_a_model_that_only_fails_this_kind(self):
        # eval（短答）で好成績でも、consult で空応答ばかりなら consult の選抜から下がる。
        self._seed("p/thinky", {"eval": {"n": 50.0, "ok": 50.0, "trunc": 0.0, "leak": 0.0,
                                         "empty": 0.0, "err": {}, "lat_ms": 0.0},
                                "consult": {"n": 6.0, "ok": 0.0, "trunc": 0.0, "leak": 0.0,
                                            "empty": 0.0, "err": {"no_output": 6.0}, "lat_ms": 0.0}})
        self._seed("p/modest", {"eval": {"n": 50.0, "ok": 40.0, "trunc": 0.0, "leak": 0.0,
                                         "empty": 0.0, "err": {}, "lat_ms": 0.0}})
        refs = ["p/thinky", "p/modest"]
        with mock.patch.object(S, "RANK_KIND_BIAS", False):
            off = S._order_for_selection(refs, "consult")
        with mock.patch.object(S, "RANK_KIND_BIAS", True):
            on = S._order_for_selection(refs, "consult")
        self.assertEqual(off[0], "p/thinky", "off では全体スコアで thinky が上のまま")
        self.assertEqual(on[0], "p/modest", "on では consult の空応答が引かれて modest が上")

    def test_bias_ignores_other_kinds_and_small_samples(self):
        self._seed("p/otherkind", {"eval": {"n": 10.0, "ok": 0.0, "trunc": 0.0, "leak": 0.0,
                                            "empty": 0.0, "err": {"no_output": 10.0}, "lat_ms": 0.0}})
        with mock.patch.object(S, "RANK_KIND_BIAS", True):
            self.assertEqual(S._kind_bias("p/otherkind", "consult"), 0.0,
                             "他 kind の失敗は参照しない")
        self._seed("p/few", {"consult": {"n": 2.0, "ok": 0.0, "trunc": 0.0, "leak": 0.0,
                                         "empty": 0.0, "err": {"no_output": 2.0}, "lat_ms": 0.0}})
        with mock.patch.object(S, "RANK_KIND_BIAS", True):
            self.assertEqual(S._kind_bias("p/few", "consult"), 0.0, "観測 3 件未満は効かない")


class DiagnoseReadsRotated(unittest.TestCase):
    def test_rotated_file_covers_the_window(self):
        tmp = tempfile.mkdtemp(prefix="fa-imp-v0410-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = tmp
        self.addCleanup(lambda: (
            os.environ.pop("FREEAGENT_STATE_DIR", None) if old_state is None
            else os.environ.__setitem__("FREEAGENT_STATE_DIR", old_state)))
        now = S.now_ts()
        # main は新しい行だけ・.1 は窓内の古い行を持つ（ローテーション直後の状態）。
        with open(os.path.join(tmp, "traces.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "ask", "ref": "nous/new:free", "ok": True,
                                 "latency_s": 0.5, "ts": now}) + "\n")
        with open(os.path.join(tmp, "traces.jsonl.1"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "ask", "ref": "nous/old:free", "ok": False,
                                 "error": "空応答", "ts": now - 3600}) + "\n")
        rows, info = S._diag_rows(now - 7200)
        refs = {r.get("ref") for r in rows}
        self.assertIn("nous/old:free", refs, f"ローテーション側の行も読む（{refs}）")
        self.assertEqual(str(info.get("rotated_file", "")).endswith(".1"), True)


class AdmissionRotation(unittest.TestCase):
    def setUp(self):
        self._orig_state = os.environ.get("FREEAGENT_STATE_DIR")
        self._tmp = tempfile.mkdtemp(prefix="fa-imp-v0410-")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp
        self.addCleanup(self._restore)

    def _restore(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)
        if self._orig_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._orig_state

    def test_rotation_moves_the_log_and_reads_both(self):
        log = S._admit_log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            for i in range(5):
                fh.write(json.dumps({"trigger_id": f"old-{i}"}) + "\n")
        S._ADMIT_MAX_BYTES = 10  # 強制ローテーション
        try:
            S._admit_append([{"trigger_id": "new-1"}])
        finally:
            S._ADMIT_MAX_BYTES = 5_000_000
        self.assertTrue(os.path.exists(log + ".1"), "退避が起こる")
        rows = S._admit_load(100)
        ids = [r.get("trigger_id") for r in rows]
        self.assertIn("new-1", ids)
        self.assertIn("old-4", ids, "退避側の末尾も読む")
        self.assertLessEqual(len(rows), 100)

    def test_tail_read_does_not_expand_the_whole_file(self):
        log = S._admit_log_path()
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "w", encoding="utf-8") as fh:
            for i in range(200):
                fh.write(json.dumps({"trigger_id": f"t-{i}"}) + "\n")
        rows = S._admit_load(3)
        self.assertEqual([r["trigger_id"] for r in rows], ["t-197", "t-198", "t-199"])


class DenseHead(unittest.TestCase):
    def test_no_cjk_adjacent_ascii_space_in_the_head(self):
        import re
        cjk = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
        for t in S.TOOLS:
            desc = t["description"]
            head = desc[:140]
            for i, ch in enumerate(head):
                if ch != " ":
                    continue
                prev = head[i - 1] if i else ""
                nxt = head[i + 1] if i + 1 < len(head) else ""
                ok = not (cjk.match(prev or "") or cjk.match(nxt or ""))
                self.assertTrue(ok, f"{t['name']} の頭 140 字に和文隣接の空白: ...{head[max(0, i-6):i+6]!r}")

    def test_tail_beyond_limit_is_unchanged(self):
        text = "あ " * 200
        self.assertIn(" ", S._dense_head(text, 140)[140 - 10:], "limit の外は従来どおり")


class SnippetMentionsDeferredCatalog(unittest.TestCase):
    def test_snippet_has_the_tool_search_flow(self):
        here = pathlib.Path(r"C:\Users\oresama\hermes-freeagent-bind\scripts\apply_proactive.py")
        text = here.read_text(encoding="utf-8")
        self.assertIn("tool_search", text)
        self.assertIn("tool_describe", text)
        self.assertIn("遅延カタログ", text)


if __name__ == "__main__":
    unittest.main()
