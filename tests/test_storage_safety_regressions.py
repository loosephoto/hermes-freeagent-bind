import os
import json
import tempfile
import threading
import concurrent.futures
import subprocess
import sys
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class StorageSafety(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fa-storage-safety-")
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp.name,
            "FREEAGENT_QUOTA_PATH": os.path.join(self.tmp.name, "quota.json"),
            "FREEAGENT_THOUGHTS_PATH": os.path.join(self.tmp.name, "thoughts.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False
        S._QUOTA.clear()
        S._QUOTA_LOADED = False
        self.enabled = mock.patch.object(S, "QUOTA_ENABLED", True)
        self.enabled.start()
        self.addCleanup(self.enabled.stop)
        self.limits = mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 1}})
        self.limits.start()
        self.addCleanup(self.limits.stop)

    def test_thought_write_failure_is_error_and_not_cached(self):
        os.mkdir(os.environ["FREEAGENT_THOUGHTS_PATH"])
        response = S.handle_tool_call({"name": "freeagent_think", "arguments": {"thought": "test"}})
        self.assertTrue(response.get("isError"))
        self.assertIn("error", response["structuredContent"])
        self.assertFalse(S._THOUGHTS)

    def test_atomic_ledger_write_failure_is_visible(self):
        with mock.patch.object(S, "_atomic_write", return_value=False):
            response = S.handle_tool_call({"name": "freeagent_think", "arguments": {"thought": "test"}})
        self.assertTrue(response.get("isError"))
        self.assertIn("保存", response["structuredContent"]["error"])
        self.assertFalse(S._THOUGHTS)

    def test_atomic_quota_write_failure_rejects_send(self):
        with mock.patch.object(S, "_atomic_write", return_value=False):
            reason = S.quota_reserve("nous")
        self.assertIn("保存", reason or "")
        self.assertFalse(os.path.exists(os.environ["FREEAGENT_QUOTA_PATH"]))

    def test_corrupt_thoughts_are_not_overwritten(self):
        path = os.environ["FREEAGENT_THOUGHTS_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("broken json")
        response = S.handle_tool_call({"name": "freeagent_think", "arguments": {"thought": "test"}})
        self.assertTrue(response.get("isError"))
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "broken json")

    def test_malformed_chain_is_not_destroyed_by_new_thought(self):
        path = os.environ["FREEAGENT_THOUGHTS_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"chains": {"broken": "not a chain"}}, stream)
        with open(path, "rb") as stream:
            before = stream.read()
        response = S.handle_tool_call({"name": "freeagent_think", "arguments": {"thought": "test"}})
        self.assertTrue(response.get("isError"))
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_malformed_step_is_not_destroyed(self):
        path = os.environ["FREEAGENT_THOUGHTS_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"chains": {"broken": {"updated_at": S.now_ts(), "steps": ["bad"]}}}, stream)
        with open(path, "rb") as stream:
            before = stream.read()
        result = S.thought_merge("broken", {"n": 1, "text": "test"})
        self.assertIn("error", result)
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_drop_does_not_overwrite_malformed_chain(self):
        path = os.environ["FREEAGENT_THOUGHTS_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"chains": {"broken": "not a chain"}}, stream)
        with open(path, "rb") as stream:
            before = stream.read()
        with self.assertRaises(OSError):
            S.thought_drop("some-id")
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_quota_write_failure_rejects_every_reservation(self):
        os.mkdir(os.environ["FREEAGENT_QUOTA_PATH"])
        self.assertIsNotNone(S.quota_reserve("nous"))
        self.assertIsNotNone(S.quota_reserve("nous"))

    def test_corrupt_provider_row_is_not_dropped(self):
        path = os.environ["FREEAGENT_QUOTA_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "providers": {"nous": None}}, stream)
        self.assertIsNotNone(S.quota_reserve("nous"))
        with open(path, encoding="utf-8") as stream:
            self.assertIsNone(json.load(stream)["providers"]["nous"])

    def test_corrupt_quota_is_not_replaced_with_empty_state(self):
        path = os.environ["FREEAGENT_QUOTA_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("broken json")
        self.assertIsNotNone(S.quota_reserve("nous"))
        with open(path, encoding="utf-8") as stream:
            self.assertEqual(stream.read(), "broken json")

    def test_pending_reservations_are_visible(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        self.assertIsNone(S.quota_reserve("nous", 9, "pending-id"))
        report = S.quota_report(["nous"])
        self.assertEqual(report["providers"]["nous"]["pending_reservations"], 1)
        self.assertEqual(report["providers"]["nous"]["reserved_tokens"], 9)

    def test_settlement_failure_keeps_answer_and_warns(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, "PROVIDER_GATE", gate), \
             mock.patch.object(S, "_call_once", return_value={"text": "valuable answer", "tokens": 7}), \
             mock.patch.object(S, "quota_add_tokens", side_effect=S.QuotaExhaustedError("disk error")):
            result, _ = S._gated_call_once("nous", "fake", "x", "", 8, None)
        self.assertEqual(result["text"], "valuable answer")
        self.assertIn("settlement_failed", result.get("quota_accounting", ""))

    def test_failed_settlement_keeps_visible_reservation(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        self.assertIsNone(S.quota_reserve("nous", 9, "inflight"))
        S.quota_report(["nous"])
        with mock.patch.object(S, "_quota_write", return_value=False):
            with self.assertRaises(S.QuotaExhaustedError):
                S.quota_add_tokens("nous", 7, "inflight")
        report = S.quota_report(["nous"])
        self.assertEqual(report["providers"]["nous"]["reserved_tokens"], 9)
        self.assertEqual(report["providers"]["nous"]["tpm"]["used"], 9)

    def test_old_quota_window_migrates_conservatively(self):
        S._QUOTA_LIMITS["nous"] = {"rpm": 2, "tpm": 10}
        path = os.environ["FREEAGENT_QUOTA_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"version": 1, "providers": {"nous": {"day": "1970-01-01",
                "min_start": 1000, "min_req": 2, "min_tok": 10,
                "day_req": 2, "day_tok": 10}}}, stream)
        clock = [1059.0]
        with mock.patch.object(S, "now_ts", side_effect=lambda: clock[0]):
            self.assertIsNotNone(S.quota_reserve("nous"))
            clock[0] = 1119.0
            self.assertIsNotNone(S.quota_reserve("nous"))
            clock[0] = 1120.0
            self.assertIsNone(S.quota_reserve("nous"))

    def test_tpm_rolling_expiration(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 10}
        clock = [1000.0]
        with mock.patch.object(S, "now_ts", side_effect=lambda: clock[0]):
            S.quota_record("nous", 6)
            clock[0] = 1059.0
            S.quota_record("nous", 4)
            self.assertIsNotNone(S.quota_block("nous"))
            clock[0] = 1060.0
            self.assertIsNone(S.quota_block("nous"))
            self.assertEqual(S.quota_report(["nous"])["providers"]["nous"]["tpm"]["used"], 4)

    def test_token_reservation_is_shared_across_processes(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 15}
        self.assertIsNone(S.quota_reserve("nous", 9, "parent"))
        env = os.environ.copy()
        env["PYTHONPATH"] = _FA_SRC + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["FREEAGENT_QUOTA_LIMITS"] = "nous:tpm=15"
        child = subprocess.run([sys.executable, "-c",
            "from freeagent_bind import server as S; print('blocked' if S.quota_reserve('nous',9,'child') else 'admitted')"],
            env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), "blocked")
        with open(os.environ["FREEAGENT_QUOTA_PATH"], encoding="utf-8") as stream:
            pending = json.load(stream)["providers"]["nous"]["pending"]
        self.assertEqual(list(pending), ["parent"])

    def test_pending_survives_unknown_failure_and_midnight(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20, "tpd": 20}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        clock = [86399.0]
        with mock.patch.object(S, "now_ts", side_effect=lambda: clock[0]), \
                mock.patch.object(S, "PROVIDER_GATE", gate), \
                mock.patch.object(S, "_call_once", side_effect=TimeoutError("unknown outcome")):
            with self.assertRaises(TimeoutError):
                S._gated_call_once("nous", "fake", "x", "", 8, None)
            clock[0] = 86401.0
            report = S.quota_report(["nous"])["providers"]["nous"]
            self.assertEqual(report["tpm"]["used"], 9)
            self.assertEqual(report["tpd"]["used"], 9)
            self.assertEqual(report["pending_reservations"], 1)

    def test_bad_minute_event_is_rejected_without_rewrite(self):
        S._QUOTA_LIMITS["nous"] = {"rpm": 2}
        path = os.environ["FREEAGENT_QUOTA_PATH"]
        with open(path, "w", encoding="utf-8") as stream:
            json.dump({"providers": {"nous": {"minute_events": [[1000, 1, 0], "bad"], "min_req": 1, "min_tok": 0}}}, stream)
        with open(path, "rb") as stream:
            before = stream.read()
        with mock.patch.object(S, "now_ts", return_value=1001):
            self.assertIsNotNone(S.quota_reserve("nous"))
        with open(path, "rb") as stream:
            self.assertEqual(stream.read(), before)

    def test_old_writer_counter_change_is_rejected(self):
        S._QUOTA_LIMITS["nous"] = {"rpm": 2}
        with mock.patch.object(S, "now_ts", return_value=1000):
            self.assertIsNone(S.quota_reserve("nous"))
            path = os.environ["FREEAGENT_QUOTA_PATH"]
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            data["providers"]["nous"]["min_req"] += 1
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
            self.assertIsNotNone(S.quota_reserve("nous"))

    def test_rolling_rpm_boundary(self):
        S._QUOTA_LIMITS["nous"] = {"rpm": 2}
        clock = [1000.0]
        with mock.patch.object(S, "now_ts", side_effect=lambda: clock[0]):
            self.assertIsNone(S.quota_reserve("nous"))
            clock[0] = 1059.0
            self.assertIsNone(S.quota_reserve("nous"))
            clock[0] = 1060.0
            self.assertIsNone(S.quota_reserve("nous"))
            clock[0] = 1062.0
            self.assertIsNotNone(S.quota_reserve("nous"))
            clock[0] = 1119.0
            self.assertIsNone(S.quota_reserve("nous"))

    def test_ask_renders_settlement_warning(self):
        text = S._render_ask({"answer": "ok", "served_by": "nous/fake", "quota_accounting": "settlement_failed: disk error"})
        self.assertIn("settlement_failed", text)

    def test_ask_exposes_unknown_usage(self):
        result = {"text": "ok", "ref": "nous/fake", "quota_accounting": "usage_unknown_reservation_retained"}
        with mock.patch.object(S, "call_model", return_value=result), \
             mock.patch.object(S, "model_status", return_value="unknown"), \
             mock.patch.object(S, "model_quality", return_value=0):
            answer = S.tool_ask({"model": "nous/fake", "prompt": "x"})
        self.assertEqual(answer.get("quota_accounting"), "usage_unknown_reservation_retained")

    def test_partial_usage_keeps_reservation(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, "PROVIDER_GATE", gate), \
                mock.patch.object(S, "_call_once", return_value={"text": "ok", "tokens": {"prompt": None, "completion": 2}}):
            result, _ = S._gated_call_once("nous", "fake", "x", "", 8, None)
        self.assertEqual(result.get("quota_accounting"), "usage_unknown_reservation_retained")
        self.assertEqual(S.quota_report(["nous"])["providers"]["nous"]["reserved_tokens"], 9)

    def test_unknown_usage_is_visible_and_remains_reserved(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, "PROVIDER_GATE", gate), \
                mock.patch.object(S, "_call_once", return_value={"text": "ok", "tokens": {}}):
            result, _ = S._gated_call_once("nous", "fake", "x", "", 8, None)
        self.assertEqual(result["quota_accounting"], "usage_unknown_reservation_retained")
        self.assertEqual(S.quota_report(["nous"])["providers"]["nous"]["reserved_tokens"], 9)

    def test_overlarge_max_tokens_is_capped_to_send_short_request(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 20}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, "PROVIDER_GATE", gate), \
             mock.patch.object(S, "_call_once", return_value={"text": "short", "tokens": 4}) as upstream:
            result, _ = S._gated_call_once("nous", "fake", "x", "", 800, None)
        self.assertEqual(result["text"], "short")
        self.assertEqual(upstream.call_args.args[4], 19)
        self.assertIn("max_tokens", result.get("quota_accounting", ""))

    def test_inflight_token_budget_is_reserved_and_settled(self):
        S._QUOTA_LIMITS["nous"] = {"tpm": 15}
        started, finish = threading.Event(), threading.Event()
        calls = []
        def upstream(*args, **kwargs):
            calls.append(1)
            started.set()
            if len(calls) == 1 and not finish.wait(5):
                raise AssertionError("test did not release upstream")
            return {"text": "ok", "tokens": {"prompt": 3, "completion": 4}}
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, "PROVIDER_GATE", gate), mock.patch.object(S, "_call_once", side_effect=upstream):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(S._gated_call_once, "nous", "fake", "x", "", 8, None)
                try:
                    self.assertTrue(started.wait(5))
                    with self.assertRaises(S.QuotaExhaustedError):
                        S._gated_call_once("nous", "fake", "long prompt", "", 8, None)
                finally:
                    finish.set()
                first.result(timeout=5)
        self.assertEqual(S.quota_report(["nous"])["providers"]["nous"]["tpm"]["used"], 7)
