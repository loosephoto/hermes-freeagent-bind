"""issue #13（Linux 常駐配線の再生成・検証レポート）への対処の回帰テスト。

  * B: `--task` は稼働中プロセスを反映しない → `--restart` opt-in・stale 警告・出力への明記
  * C: `verify_generated()` が `systemd-analyze --user verify` を通す
  * D: ハーネス判定の上書きが stderr に 1 行出る
  * E: proxy が「active だが未認証待機中」のときの note
  * F: `scripts/verify_resident.py` の実 MCP 疎通（擬似サーバーでプロトコルを検証）

ネットワークと実 systemd を使わない（`subprocess.run` は差し替える）。
"""
from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import subprocess  # noqa: F401  # CompletedProcess を使うため
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
sys.path.insert(0, HERE)
from _tmpstate import isolated_state  # noqa: E402  # type: ignore[import-not-found]

isolated_state("fa-resident-v0520-")

import install_resident as ir  # noqa: E402  # type: ignore[import-not-found]
from freeagent_bind import server as S  # noqa: E402

TS_OLD = "Tue 2026-11-14 08:17:44 UTC"
TS_NEW = "Tue 2026-11-14 08:26:53 UTC"


def _completed(cmd, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr=stderr)


class ResidentV0520Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-v0520-")
        self.home = os.path.join(self.tmp, "hermes")
        self.repo = os.path.join(self.tmp, "repo")
        self.unit_dir = os.path.join(self.tmp, "units")
        os.makedirs(self.repo, exist_ok=True)
        self.python = os.path.join(self.tmp, "python3")
        with open(self.python, "w", encoding="ascii", newline="\n") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(self.python, 0o755)
        os.makedirs(os.path.join(self.home, "logs"), exist_ok=True)

    def _cfg(self, platform_name="linux"):
        return ir.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.python, startup_dir=self.unit_dir,
            platform_name=platform_name,
        )

    def _write_units(self, cfg):
        ir.write_plan(ir.build_plan(cfg), force=True)

    def _fake_systemd(self):
        path = os.path.join(self.tmp, "systemd-system")
        os.makedirs(path, exist_ok=True)
        return path


class TestRestartAndStaleNotes(ResidentV0520Base):
    """B: --restart opt-in と稼働中プロセスの stale 警告。"""

    def test_restart_opt_in_runs_systemctl_restart_and_drops_note(self):
        cfg = self._cfg()
        self._write_units(cfg)
        calls = []

        def fake_run(cmd, **_kw):
            calls.append(list(cmd))
            return _completed(cmd)

        with mock.patch.object(ir.shutil, "which", return_value="/usr/bin/systemctl"), \
             mock.patch.object(ir.subprocess, "run", side_effect=fake_run):
            systemd_dir = self._fake_systemd()
            out_restart = ir.register_linux_units(cfg, systemd_dir=systemd_dir, restart=True)
            out_plain = ir.register_linux_units(cfg, systemd_dir=systemd_dir, restart=False)
        for unit in ("hermes-freeagent-bind.service", "hermes-proxy.service"):
            self.assertIn(["systemctl", "--user", "restart", unit], calls)
            self.assertIn("service restarted: %s" % unit, out_restart)
        self.assertFalse(any("--task は稼働中" in ln for ln in out_restart), out_restart)
        self.assertTrue(any("--task は稼働中" in ln for ln in out_plain), out_plain)

    def test_stale_running_process_is_reported(self):
        cfg = self._cfg()
        self._write_units(cfg)
        # unit の mtime を「稼働プロセスの起動時刻より新しい」へ進める
        future = time.time() + 3600
        for key in ("unit", "proxy_unit"):
            os.utime(cfg["paths"][key], (future, future))
        started = time.gmtime(time.time() - 3600)
        ts_old = time.strftime("%a %Y-%m-%d %H:%M:%S UTC", started)
        show_out = ("ActiveState=active\nMainPID=12421\n"
                    "ExecMainStartTimestamp=%s\n" % ts_old)

        def fake_run(cmd, **_kw):
            if "show" in cmd:
                return _completed(cmd, stdout=show_out)
            return _completed(cmd)

        with mock.patch.object(ir.shutil, "which", return_value="/usr/bin/systemctl"), \
             mock.patch.object(ir.subprocess, "run", side_effect=fake_run):
            out = ir.register_linux_units(cfg, systemd_dir=self._fake_systemd(), restart=False)
        self.assertTrue(
            any("生成より前のプロセスが稼働中" in ln and "hermes-freeagent-bind.service" in ln
                for ln in out),
            out)
        self.assertTrue(
            any("生成より前のプロセスが稼働中" in ln and "hermes-proxy.service" in ln for ln in out),
            out)

    def test_fresh_process_and_inactive_units_are_not_reported(self):
        cfg = self._cfg()
        self._write_units(cfg)
        fresh = ("ActiveState=active\nMainPID=12421\n"
                 "ExecMainStartTimestamp=%s\n" % TS_NEW)
        inactive = ("ActiveState=inactive\nMainPID=0\n"
                    "ExecMainStartTimestamp=%s\n" % TS_OLD)
        for stdout in (fresh, inactive):
            def fake_run(cmd, **_kw):
                if "show" in cmd:
                    return _completed(cmd, stdout=stdout)
                return _completed(cmd)

            with mock.patch.object(ir.shutil, "which", return_value="/usr/bin/systemctl"), \
                 mock.patch.object(ir.subprocess, "run", side_effect=fake_run):
                out = ir.register_linux_units(cfg, systemd_dir=self._fake_systemd(), restart=False)
            self.assertFalse(any("生成より前のプロセス" in ln for ln in out), out)

    def test_register_services_non_linux_notes_restart_scope(self):
        cfg = self._cfg(platform_name="macos")
        self._write_units(cfg)
        with mock.patch.object(ir, "_host_matches_target", return_value=True), \
             mock.patch.object(ir, "register_macos_agents",
                               return_value=["service bootstrapped: ai.hermes.freeagent-bind (gui/501)"]):
            out = ir.register_services(cfg, restart=True)
        self.assertTrue(any("--restart は Linux" in ln for ln in out), out)


class TestProxyWaitingNote(ResidentV0520Base):
    """E: active だが未認証で待機中の proxy を note に出す。"""

    def test_note_when_log_ends_with_skip_message(self):
        cfg = self._cfg()
        self._write_units(cfg)
        log = cfg["paths"]["proxy_log"]
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[launcher] exec pid=1 cmd=x\n")
            fh.write(ir.PROXY_SKIP_LOG_MSG + "\n")
        note = ir.proxy_waiting_note(cfg)
        self.assertIsNotNone(note)
        self.assertIn("待機中", note)
        self.assertIn("hermes-proxy.service", note)
        self.assertIn("8645", note)

    def test_no_note_when_authenticated_or_absent(self):
        cfg = self._cfg()
        self._write_units(cfg)
        self.assertIsNone(ir.proxy_waiting_note(cfg))
        log = cfg["paths"]["proxy_log"]
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write("[launcher] exec pid=2 cmd=y\n")
        self.assertIsNone(ir.proxy_waiting_note(cfg))
        self.assertIsNone(ir.proxy_waiting_note({**cfg, "with_proxy": False}))


class TestSystemdAnalyzeInVerify(ResidentV0520Base):
    """C: verify_generated() が systemd-analyze --user verify を通す。"""

    def _verify(self, cfg, analyze_rc=0, analyze_out=""):
        def fake_run(cmd, **_kw):
            if cmd[:1] == ["systemd-analyze"]:
                return _completed(cmd, rc=analyze_rc, stdout=analyze_out)
            return _completed(cmd)

        with mock.patch.object(ir, "_host_matches_target", return_value=True), \
             mock.patch.object(ir.shutil, "which", return_value="/usr/bin/systemd-analyze"), \
             mock.patch.object(ir.subprocess, "run", side_effect=fake_run):
            return ir.verify_generated(cfg)

    def test_reports_verify_failure(self):
        cfg = self._cfg()
        self._write_units(cfg)
        problems = self._verify(cfg, analyze_rc=1,
                                analyze_out="hermes-freeagent-bind.service: bad-setting")
        self.assertTrue(any("systemd-analyze verify" in p and "bad-setting" in p for p in problems),
                        problems)

    def test_passes_when_verify_clean_and_skips_when_tool_absent(self):
        cfg = self._cfg()
        self._write_units(cfg)
        self.assertEqual(self._verify(cfg), [])
        with mock.patch.object(ir, "_host_matches_target", return_value=True), \
             mock.patch.object(ir.shutil, "which", return_value=None):
            self.assertEqual(ir.verify_generated(cfg), [])

    def test_basic_problems_short_circuit_verify(self):
        """基本検査が落ちている環境（stub python 等）では verify を二重報告しない。"""
        cfg = self._cfg()
        self._write_units(cfg)
        cfg = {**cfg, "python": os.path.join(self.tmp, "no-such-python")}

        def fail_run(cmd, **_kw):
            raise AssertionError("基本検査が落ちたのに systemd-analyze を実行した")

        with mock.patch.object(ir, "_host_matches_target", return_value=True), \
             mock.patch.object(ir.shutil, "which", return_value="/usr/bin/systemd-analyze"), \
             mock.patch.object(ir.subprocess, "run", side_effect=fail_run):
            problems = ir.verify_generated(cfg)
        self.assertTrue(any("python が見つかりません" in p for p in problems), problems)

    def test_bus_noise_is_dropped(self):
        cfg = self._cfg()
        self._write_units(cfg)
        problems = self._verify(
            cfg, analyze_rc=1,
            analyze_out="Failed to connect to bus: No such file or directory")
        self.assertEqual(problems, [])


class TestParseSystemdTimestamp(unittest.TestCase):
    def test_utc_and_naive_and_garbage(self):
        import calendar
        want = calendar.timegm(time.strptime("2026-11-14 08:17:44", "%Y-%m-%d %H:%M:%S"))
        self.assertAlmostEqual(ir._parse_systemd_timestamp(TS_OLD), want, delta=1)
        # TZ 略号が JST のときはローカル時刻として読む（epoch 化できることだけ固定）
        local = ir._parse_systemd_timestamp("Tue 2026-11-14 08:17:44 JST")
        self.assertIsInstance(local, float)
        self.assertEqual(ir._parse_systemd_timestamp("not a timestamp"), None)
        self.assertEqual(ir._parse_systemd_timestamp(""), None)


class TestHarnessOverwriteLog(unittest.TestCase):
    """D: HTTP 共有プロセスでの判定上書きが stderr に 1 行出る。"""

    def setUp(self):
        S._harness_reset()

    def tearDown(self):
        S._harness_reset()

    def _init(self, client):
        return S.harness_on_initialize(
            {"protocolVersion": "2025-06-18", "capabilities": {},
             "clientInfo": {"name": client, "version": "1.0"}},
            marker="hermes" if client == "hermes" else None)

    def test_overwrite_is_logged_once_per_change(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._init("hermes")
            self._init("resident-verify")          # hermes → other に上書き
            self._init("resident-verify")          # 同一クライアントの再接続は出ない
            self._init("another")                  # 別クライアントに上書き
        text = err.getvalue()
        self.assertEqual(text.count("ハーネス判定を上書き"), 2, text)
        self.assertIn("resident-verify (other)", text)
        self.assertIn("最後に initialize したクライアント", text)

    def test_same_kind_different_client_is_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self._init("claude-code")
            self._init("codex")
        self.assertEqual(err.getvalue().count("ハーネス判定を上書き"), 1)


class _FakeMcpHandler(http.server.BaseHTTPRequestHandler):
    """verify_resident.py をループバックで往復させる擬似 MCP サーバー。"""

    protocol_version = "HTTP/1.1"
    call_error_first = False
    call_error_always = False

    def log_message(self, *args):  # noqa: A003
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        msg = json.loads(self.rfile.read(length).decode("utf-8"))
        method = msg.get("method")
        if method == "initialize":
            self._json({
                "jsonrpc": "2.0", "id": msg["id"],
                "result": {"protocolVersion": msg["params"]["protocolVersion"],
                           "serverInfo": {"name": "freeagent-bind", "version": "test"}},
            }, session_id="sid-test")
        elif method == "tools/call":
            error = type(self).call_error_always or (
                type(self).call_error_first and msg["id"] == 3)
            if error:
                self._json({"jsonrpc": "2.0", "id": msg["id"],
                            "result": {"isError": True, "content": [{"type": "text", "text": "session expired"}]}})
                type(self).call_error_first = False
            else:
                self._json({"jsonrpc": "2.0", "id": msg["id"],
                            "result": {"isError": False, "content": [{"type": "text", "text": "ok"}]}})
        elif method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": msg["id"],
                        "result": {"tools": [{"name": "freeagent_models"}, {"name": "freeagent_ask"}]}})
        else:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def _json(self, payload, session_id=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(body)


class TestVerifyResidentScript(unittest.TestCase):
    """F: scripts/verify_resident.py の実 MCP 往復（擬似サーバー）。"""

    def setUp(self):
        import verify_resident as vr
        self.vr = vr
        _FakeMcpHandler.call_error_first = False
        _FakeMcpHandler.call_error_always = False
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FakeMcpHandler)
        self.url = "http://127.0.0.1:%d/mcp" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_roundtrip_ok(self):
        code, lines = self.vr.verify(url=self.url)
        self.assertEqual(code, 0, lines)
        self.assertTrue(any("tools/list : 2 件" in ln for ln in lines), lines)
        self.assertTrue(any("tools/call : freeagent_models 成功" in ln for ln in lines), lines)

    def test_session_expired_retries_once(self):
        _FakeMcpHandler.call_error_first = True
        code, lines = self.vr.verify(url=self.url)
        self.assertEqual(code, 0, lines)
        self.assertEqual(sum("失敗" in ln for ln in lines), 1, lines)

    def test_persistent_failure_exits_1(self):
        _FakeMcpHandler.call_error_always = True
        code, lines = self.vr.verify(url=self.url)
        self.assertEqual(code, 1, lines)
        self.assertTrue(any("再送でも" in ln for ln in lines), lines)


if __name__ == "__main__":
    unittest.main()
