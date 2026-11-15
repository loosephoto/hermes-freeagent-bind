"""v0.52.1 常駐サーバー監査修正の回帰テスト（SPEC §6.68）。

監査（2026-11-15）で実測した不具合を 1 項目 1 テストで固定する。

1. HTTP セッションは**非 SSE なら TTL で失効**し、**SSE 接続中は追い出さない**
   （旧: `_http_session_get` が TTL を見ずに復活させ、SSE 中は `seen` が古いまま
   強制追い出しで切断された）。
2. IPv6（`Host: [::1]:8790` / `--host ::1`）が 403 にも `gaierror` にもならない。
3. `serve_http` は `SIGTERM` / 停止時に SSE を閉じ、`_drain_parallel_futures()` を待つ。
4. `quota.py` が `S._QUOTA_LOADED` / `S._QUOTA_MTIME` を更新する（旧: モジュール局所変数）。
5. `verify_resident.py` は JSON-RPC `error` と空応答を成功にせず、token を送り、
   既定で `x-freeagent-harness: hermes` を添え、終了時にセッションを DELETE する。
6. `freeagent.env` は実環境を壊さず `.env` を上書きでき、引用符つきでもインラインコメントを落とす。
7. クライアント URL は `probe_host` を通る（`0.0.0.0` / `::` を接続先に書かない）。
8. launcher は末尾バックスラッシュの repo でも構文が壊れず、`HERMES_HOME` を設定する。
9. `--hermes-home` / `--startup-dir` / `--python` / `--pythonw` が worker VBS に反映される。
10. `schtasks` の登録失敗で exit 1 を返す（旧: 常に 0）。
11. `_parse_systemd_timestamp` が日本語曜日・数値 TZ を解釈し、`systemctl show` に
    `LC_ALL=C` / `TZ=UTC` を渡す。
12. Windows の `IsServer` はトークン境界で照合する（`879` が `8790` に当たらない・
    `cscript.exe` で実測）。非 ASCII パスでも生成 VBS は純 ASCII。
"""
from __future__ import annotations

import ast
import http.server
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "src") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "src"))
if os.path.join(REPO, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "tests"))
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))

from _tmpstate import isolated_state  # noqa: E402  # type: ignore[import-not-found]

isolated_state("fa-v0521-test-")

from freeagent_bind import quota as Q  # noqa: E402
from freeagent_bind import server as S  # noqa: E402
from freeagent_bind import transport_http as T  # noqa: E402


def _load_script(name: str, filename: str):
    path = os.path.join(REPO, "scripts", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


RES = _load_script("install_resident_v0521_mod", "install_resident.py")
WIN = _load_script("install_resident_win_v0521_mod", "install_resident_windows.py")
VER = _load_script("verify_resident_v0521_mod", "verify_resident.py")


def _extract_function(source: str, name: str) -> str:
    """launcher の Python ソースから 1 関数だけを取り出す（launcher 自体は走らせない）。"""
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    return compile(ast.Module(body=[node], type_ignores=[]), "<launcher>", "exec")


class HttpSessionsTest(unittest.TestCase):
    """1・2. セッション TTL / SSE 維持 / IPv6。"""

    def setUp(self):
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS.clear()

    def addCleanup(self, func, *a, **kw):
        super().addCleanup(func, *a, **kw)

    def tearDown(self):
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS.clear()

    def test_expired_idle_session_is_refused_not_revived(self):
        sid = T._http_session_new()
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS[sid]["seen"] = S.now_ts() - (S.HTTP_SESSION_TTL + 5)
        self.assertIsNone(T._http_session_get(sid), "TTL を過ぎたセッションを復活させている")
        with T._HTTP_SESSIONS_LOCK:
            self.assertNotIn(sid, T._HTTP_SESSIONS)

    def test_fresh_session_stays_and_touches_seen(self):
        sid = T._http_session_new()
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS[sid]["seen"] = S.now_ts() - 60
            before = T._HTTP_SESSIONS[sid]["seen"]
        self.assertIsNotNone(T._http_session_get(sid))
        with T._HTTP_SESSIONS_LOCK:
            self.assertGreater(T._HTTP_SESSIONS[sid]["seen"], before)

    def test_open_sse_session_is_not_evicted_by_prune(self):
        sse_sid = T._http_session_new()
        idle_sid = T._http_session_new()
        now = S.now_ts()
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS[sse_sid]["sse_open"] = 1
            T._HTTP_SESSIONS[sse_sid]["seen"] = now - (S.HTTP_SESSION_TTL + 60)
            T._HTTP_SESSIONS[idle_sid]["seen"] = now - 5
        # SSE 接続中は get でも失効させない
        self.assertIsNotNone(T._http_session_get(sse_sid))
        with T._HTTP_SESSIONS_LOCK:
            T._HTTP_SESSIONS[sse_sid]["seen"] = now - 100
            with mock.patch.object(S, "HTTP_MAX_SESSIONS", 4):
                T._http_sessions_prune_locked(force=True)
            self.assertIn(sse_sid, T._HTTP_SESSIONS, "生存中の SSE セッションを追い出している")
            self.assertNotIn(idle_sid, T._HTTP_SESSIONS)

    def test_ipv6_host_header_is_accepted(self):
        allowed = {"::1", "127.0.0.1", "localhost"}
        self.assertTrue(T._http_host_ok({"host": "[::1]:8790"}, allowed)[0])
        self.assertTrue(T._http_host_ok({"host": "[::1]"}, allowed)[0])
        self.assertTrue(T._http_host_ok({"host": "127.0.0.1:8790"}, allowed)[0])
        self.assertFalse(T._http_host_ok({"host": "[brok"}, allowed)[0])
        self.assertFalse(T._http_host_ok({"host": "evil.example:8790"}, allowed)[0])

    def test_ipv6_loopback_bind_serves(self):
        try:
            probe = socket_tcp6()
            probe.close()
        except OSError:
            self.skipTest("IPv6 ループバックが無効な環境")

        httpd = T._http_build_server({"host": "[::1]", "port": 0})
        self.addCleanup(httpd.server_close)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)

        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "ipv6-check", "version": "1.0"}},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://[::1]:{port}/mcp", data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["result"]["serverInfo"]["version"], S.SERVER_VERSION)


def socket_tcp6():
    import socket
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.bind(("::1", 0))
    return sock


class ServeHttpShutdownTest(unittest.TestCase):
    """3. 停止時に SSE を閉じ、並列ジョブをドレインする。"""

    def test_stop_closes_sessions_and_drains_parallel(self):
        # 規約 65: 実ソケットの serve_forever は回さず、終了経路だけを差し替えて確かめる。
        sid = T._http_session_new()
        drained: list[bool] = []
        fake_server = mock.MagicMock()
        fake_server.server_address = ("127.0.0.1", 12345)
        fake_server.serve_forever.side_effect = KeyboardInterrupt()

        with mock.patch.object(T, "_http_build_server", return_value=fake_server), \
             mock.patch.object(S, "_drain_parallel_futures", side_effect=lambda: drained.append(True)):
            T.serve_http({"host": "127.0.0.1", "port": 0})

        fake_server.server_close.assert_called_once()
        self.assertEqual(drained, [True], "_drain_parallel_futures を呼んでいない")
        with T._HTTP_SESSIONS_LOCK:
            self.assertEqual(len(T._HTTP_SESSIONS), 0)
        self.assertIsNone(T._http_session_get(sid))


class QuotaStateTest(unittest.TestCase):
    """4. `S._QUOTA_LOADED` / `S._QUOTA_MTIME` の更新。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-quota-v0521-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"FREEAGENT_QUOTA_PATH":
                                               os.path.join(self.tmp, "quota.json")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_and_write_update_server_module_flags(self):
        saved = (S._QUOTA_LOADED, S._QUOTA_MTIME, S.QUOTA_ENABLED, dict(S._QUOTA_LIMITS))
        self.addCleanup(lambda: (setattr(S, "_QUOTA_LOADED", saved[0]),
                                 setattr(S, "_QUOTA_MTIME", saved[1]),
                                 setattr(S, "QUOTA_ENABLED", saved[2]),
                                 S._QUOTA_LIMITS.clear(),
                                 S._QUOTA_LIMITS.update(saved[3])))
        S.QUOTA_ENABLED = True
        S._QUOTA_LIMITS.clear()
        S._QUOTA_LIMITS["groq"] = {"rpd": 10}
        S._QUOTA_LOADED = False
        S._QUOTA_MTIME = None
        Q._QUOTA.clear()

        Q._ensure_quota_loaded()
        self.assertTrue(S._QUOTA_LOADED, "_ensure_quota_loaded が S._QUOTA_LOADED を立てていない")

        self.assertIsNone(Q.quota_reserve("groq"), "宣言した枠で 1 回目が止まった")
        self.assertTrue(os.path.isfile(Q.quota_path()))
        self.assertIsNotNone(S._QUOTA_MTIME, "_quota_write が S._QUOTA_MTIME を更新していない")
        first = S._QUOTA_MTIME

        # 読み側も **S の記録** で早期 return すること（mtime 一致ならディスクを読み直さない）。
        def _boom(_path):
            raise AssertionError("mtime が一致しているのに quota.json を読み直している")

        Q._QUOTA.clear()
        with mock.patch.object(Q, "_quota_read", side_effect=_boom):
            Q._ensure_quota_loaded()
        self.assertEqual(S._QUOTA_MTIME, first)


class VerifyResidentTest(unittest.TestCase):
    """5. 検証スクリプトの厳密判定・token・harness・後始末。"""

    def _serve(self, responder):
        state = {"calls": 0, "headers": [], "deleted": [], "sessions": 0}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or "0"))
                msg = json.loads(raw.decode("utf-8"))
                state["headers"].append({k.lower(): v for k, v in self.headers.items()})
                method = msg.get("method")
                if method == "initialize":
                    state["sessions"] += 1
                    sid = "sid-%d" % state["sessions"]
                    self._send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "serverInfo": {"name": "hermes-freeagent-bind", "version": S.SERVER_VERSION},
                    }}, extra={"Mcp-Session-Id": sid})
                    return
                if method == "notifications/initialized":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if method == "tools/list":
                    self._send(200, {"jsonrpc": "2.0", "id": msg["id"],
                                     "result": {"tools": [{"name": "freeagent_models"}]}})
                    return
                state["calls"] += 1
                code, payload = responder(state["calls"], self.headers)
                if isinstance(payload, bytes):
                    body = payload
                else:
                    body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send(self, code, payload, extra=None):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                state["deleted"].append(self.headers.get("Mcp-Session-Id"))
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return "http://127.0.0.1:%d/mcp" % srv.server_address[1], state

    def test_jsonrpc_error_is_not_success(self):
        url, _ = self._serve(lambda _n, _h: (200, {
            "jsonrpc": "2.0", "id": 3, "error": {"code": -32603, "message": "boom"}}))
        rc, lines = VER.verify(url=url)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("rpc_error=-32603" in ln for ln in lines), lines)

    def test_empty_body_is_not_success(self):
        url, _ = self._serve(lambda _n, _h: (200, b""))
        rc, lines = VER.verify(url=url)
        self.assertEqual(rc, 1, lines)
        self.assertTrue(any("no-result" in ln for ln in lines), lines)

    def test_token_harness_reinit_and_session_cleanup(self):
        def responder(n, headers):
            if headers.get("Authorization") != "Bearer sec-tok":
                return 401, {"error": "unauthorized"}
            if n == 1:
                return 404, {"error": "session expired"}
            return 200, {"jsonrpc": "2.0", "id": 3,
                         "result": {"content": [], "isError": False}}

        url, state = self._serve(responder)
        with mock.patch.dict(os.environ, {"FREEAGENT_HTTP_TOKEN": "sec-tok"}):
            rc, lines = VER.verify(url=url)
        self.assertEqual(rc, 0, lines)
        self.assertEqual(state["sessions"], 2, "404 失効時に initialize から張り直していない")
        self.assertEqual(state["deleted"], ["sid-2"], "終了時にセッションを DELETE していない")
        self.assertTrue(all(h.get("x-freeagent-harness") == "hermes" for h in state["headers"]),
                        "既定でハーネスを名乗っていない（稼働中サーバーの判定を汚す）")

    def test_harness_can_be_omitted(self):
        url, state = self._serve(lambda _n, _h: (200, {
            "jsonrpc": "2.0", "id": 3, "result": {"content": [], "isError": False}}))
        rc, _ = VER.verify(url=url, token="", harness="")
        self.assertEqual(rc, 0)
        self.assertTrue(all("x-freeagent-harness" not in h for h in state["headers"]))


class EnvFileTest(unittest.TestCase):
    """6. `.env` → `freeagent.env` の上書きとインラインコメント（両プラットフォーム）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-env-v0521-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.dotenv = os.path.join(self.tmp, ".env")
        self.override = os.path.join(self.tmp, "freeagent.env")
        with open(self.dotenv, "w", encoding="utf-8") as fh:
            fh.write('SHARED="from_dotenv"  # 説明\n'
                     'HASH_IN_QUOTE="a # b"  # 末尾コメント\n'
                     "PLAIN=plain_value\n"
                     "KEEP_REAL=from_dotenv\n")
        with open(self.override, "w", encoding="utf-8") as fh:
            fh.write('SHARED="from_freeagent_env"  # .env を上書きする\n')

    def test_both_launchers_override_dotenv_but_keep_real_environment(self):
        cfg = WIN.build_config(self.tmp + os.sep + "home", self.tmp + os.sep + "repo",
                               "127.0.0.1", 8790, startup_dir=os.path.join(self.tmp, "startup"))
        sources = {"win": WIN.render_launcher(cfg), "posix": RES.render_posix_launcher(cfg)}
        for label, source in sources.items():
            code = _extract_function(source, "_load_env_file")
            ns = {"os": os}
            exec(code, ns)
            loader = ns["_load_env_file"]
            with mock.patch.dict(os.environ, {"KEEP_REAL": "real_env"}, clear=True):
                initial = set(os.environ)
                loader(self.dotenv, initial)
                loader(self.override, initial)
                self.assertEqual(os.environ.get("SHARED"), "from_freeagent_env",
                                 "%s: freeagent.env が .env を上書きできていない" % label)
                self.assertEqual(os.environ.get("HASH_IN_QUOTE"), "a # b", label)
                self.assertEqual(os.environ.get("PLAIN"), "plain_value", label)
                self.assertEqual(os.environ.get("KEEP_REAL"), "real_env",
                                 "%s: 実環境の変数を壊している" % label)


class InstallerUrlTest(unittest.TestCase):
    """7. `0.0.0.0` / `::` の待受で接続できない URL を書かない。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-url-v0521-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")

    def test_apply_scripts_use_probe_host(self):
        cases = (("0.0.0.0", "http://127.0.0.1:8790/mcp"),
                 ("::", "http://[::1]:8790/mcp"),
                 ("::1", "http://[::1]:8790/mcp"))
        for host, expected in cases:
            cfg = WIN.build_config(self.home, self.repo, host, 8790,
                                   startup_dir=os.path.join(self.tmp, "startup"))
            win_apply = WIN.render_apply(cfg)
            self.assertIn(expected, win_apply, "Windows apply（%s）" % host)
            self.assertNotIn("http://%s:8790/mcp" % host, win_apply)
            posix_cfg = RES.build_config(self.home, self.repo, host, 8790,
                                         platform_name="linux", with_proxy=False)
            posix_apply = RES.render_posix_apply(posix_cfg)
            self.assertIn(expected, posix_apply, "POSIX apply（%s）" % host)
            self.assertNotIn("http://%s:8790/mcp" % host, posix_apply)

    def test_launcher_survives_trailing_backslash_and_sets_hermes_home(self):
        cfg = WIN.build_config(self.home + "\\", self.repo + "\\", "127.0.0.1", 8790,
                               startup_dir=os.path.join(self.tmp, "startup"))
        main_launcher = WIN.render_launcher(cfg)
        ast.parse(main_launcher)    # 末尾バックスラッシュで SyntaxError にならない
        # 再起動時に .env が HERMES_HOME を外しても常駐先が既定へ落ちないように戻す（SPEC §6.68）
        self.assertIn('os.environ.setdefault("HERMES_HOME", HOME)', main_launcher)
        proxy_launcher = WIN.render_proxy_launcher(cfg)
        ast.parse(proxy_launcher)
        self.assertIn('env.setdefault("HERMES_HOME", HOME)', proxy_launcher)
        ast.parse(RES.render_posix_launcher(cfg))


class WindowsWorkerTest(unittest.TestCase):
    """8・9・10. worker VBS の生成内容と `--task` の終了コード。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-worker-v0521-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "custom-home")
        self.repo = os.path.join(self.tmp, "repo")
        self.startup = os.path.join(self.tmp, "startup")
        os.makedirs(os.path.join(self.repo, "src", "freeagent_bind"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "freeagent_bind", "server.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("# stub\n")

    def test_worker_reflects_home_startup_python_and_pyw_fallback(self):
        py = os.path.join(self.tmp, "pybin", "python.exe")
        pyw = os.path.join(self.tmp, "pybin", "pythonw.exe")
        cfg = WIN.build_config(self.home, self.repo, "127.0.0.1", 8790,
                               python=py, pythonw=pyw, startup_dir=self.startup)
        worker = WIN.render_worker(cfg)
        self.assertIn(self.home, worker, "--hermes-home が worker に反映されていない")
        self.assertIn(py, worker)
        self.assertIn(pyw, worker)
        self.assertIn("worker-8790.lock", worker, "二重起動の排他ロックが無い")
        # python.exe フォールバックでも run_server.pyw を通す（.env とログを落とさない）
        self.assertNotIn("src\\freeagent_bind\\server.py --transport", worker)

        proxy_worker = WIN.render_proxy_worker(cfg)
        self.assertIn(self.home, proxy_worker)
        self.assertIn(self.startup, proxy_worker, "--startup-dir が proxy に反映されていない")
        self.assertNotIn('sh.SpecialFolders("Startup")', proxy_worker)
        self.assertIn('worker-proxy-" & port & ".lock', proxy_worker)

    def test_default_home_keeps_localappdata_reference(self):
        cfg = WIN.build_config(WIN.hermes_home_default(), self.repo, "127.0.0.1", 8790,
                               startup_dir=self.startup)
        worker = WIN.render_worker(cfg)
        self.assertIn('sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\hermes"', worker)

    def test_task_registration_failure_exits_nonzero(self):
        with mock.patch.object(WIN, "register_task", return_value="task failed (1): access denied"), \
             mock.patch.object(WIN, "register_proxy_task", return_value="task registered: ok"):
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = WIN.main(["--hermes-home", self.home, "--repo", self.repo,
                               "--startup-dir", self.startup,
                               "--no-proxy", "--write", "--force", "--task"])
        self.assertEqual(rc, 1, "schtasks の失敗を成功として返している")
        self.assertIn("task failed", buf.getvalue())


@unittest.skipUnless(shutil.which("systemctl"), "systemctl が無い環境")
class LinuxStaleNoteTest(unittest.TestCase):
    """11. `systemctl show` に C ロケールを強制する。"""

    def test_systemctl_show_runs_with_c_locale_and_utc(self):
        tmp = tempfile.mkdtemp(prefix="fa-linux-v0521-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        cfg = RES.build_config(os.path.join(tmp, "home"), os.path.join(tmp, "repo"),
                               "127.0.0.1", 8790, platform_name="linux", with_proxy=False)
        unit_path = cfg["paths"]["unit"]
        os.makedirs(os.path.dirname(unit_path), exist_ok=True)
        with open(unit_path, "w", encoding="utf-8") as fh:
            fh.write("[Unit]\nDescription=stub\n")
        os.utime(unit_path, (0, 0))     # 生成物を十分に古くする（更新より前のプロセスを作る）
        seen_envs: list[dict] = []

        def fake_run(cmd, **kw):
            seen_envs.append(dict(kw.get("env") or {}))
            proc = subprocess.CompletedProcess(cmd, 0)
            proc.stdout = ("ActiveState=active\nMainPID=4242\n"
                           "ExecMainStartTimestamp=Thu 2020-01-02 03:04:05 UTC\n")
            proc.stderr = ""
            return proc

        with mock.patch.object(RES.subprocess, "run", side_effect=fake_run):
            notes = RES._linux_running_stale_notes(cfg, {"PATH": "/usr/bin"})
        self.assertTrue(seen_envs, "systemctl show を呼んでいない")
        self.assertEqual(seen_envs[0].get("LC_ALL"), "C")
        self.assertEqual(seen_envs[0].get("TZ"), "UTC")
        self.assertEqual(seen_envs[0].get("PATH"), "/usr/bin")
        self.assertEqual(len(notes), 1, notes)


class SystemdTimestampTest(unittest.TestCase):
    """11. ロケール依存の曜日・数値 TZ を解釈する。"""

    def test_japanese_weekday_and_numeric_offsets(self):
        expected = RES._parse_systemd_timestamp("Sun 2026-11-15 01:00:00 UTC")
        self.assertIsNotNone(expected)
        for sample in ("Sun 2026-11-15 10:00:00 +09",
                       "Sun 2026-11-15 10:00:00 +0900",
                       "Sun 2026-11-15 10:00:00 +09:00",
                       "日 2026-11-15 10:00:00 +09:00"):
            self.assertEqual(RES._parse_systemd_timestamp(sample), expected, sample)
        self.assertIsNotNone(RES._parse_systemd_timestamp("日 2026-11-15 10:00:00 JST"))
        self.assertIsNone(RES._parse_systemd_timestamp(""))
        self.assertIsNone(RES._parse_systemd_timestamp("n/a"))


@unittest.skipUnless(os.name == "nt", "cscript.exe による VBScript 実測は Windows のみ")
class VbsIsServerTest(unittest.TestCase):
    """12. `IsServer` のトークン境界一致（実測）と VBS の ASCII 維持。"""

    def test_port_boundary_and_non_ascii_paths(self):
        tmp = tempfile.mkdtemp(prefix="fa-vbs-v0521-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        home = os.path.join(tmp, "home")
        repo = os.path.join(tmp, "repo")
        startup = os.path.join(tmp, "startup")

        cfg = WIN.build_config(home, repo, "127.0.0.1", 879, pythonw="", startup_dir=startup)
        vbs = WIN.render_worker(cfg)
        start = vbs.index("Function IsServer")
        end = vbs.index("End Function", start) + len("End Function")
        body = (
            "Option Explicit\r\n"
            "Dim cmdA, cmdB, cmdC, cmdD\r\n"
            + vbs[start:end].replace("\n", "\r\n")
            + "\r\ncmdA = \"pythonw.exe \" & Chr(34) & \"run_server.pyw\" & Chr(34) & \" freeagent-resident-8790\"\r\n"
            "cmdB = \"pythonw.exe \" & Chr(34) & \"run_server.pyw\" & Chr(34) & \" freeagent-resident-879\"\r\n"
            "cmdC = \"python.exe src\\freeagent_bind\\server.py --port 8790\"\r\n"
            "cmdD = \"python.exe src\\freeagent_bind\\server.py --port 879\"\r\n"
            "WScript.Echo CStr(IsServer(\"pythonw.exe\", cmdA)) & CStr(IsServer(\"pythonw.exe\", cmdB))\r\n"
            "WScript.Echo CStr(IsServer(\"python.exe\", cmdC)) & CStr(IsServer(\"python.exe\", cmdD))\r\n"
        )
        harness = os.path.join(tmp, "is_server.vbs")
        with open(harness, "w", encoding="ascii", newline="") as fh:
            fh.write(body)
        proc = subprocess.run(["cscript.exe", "//nologo", harness],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        lines = [ln.strip() for ln in proc.stdout.strip().splitlines()]
        self.assertEqual(len(lines), 2, lines)
        # 1 行目 = freeagent-resident-<port> の目印 / 2 行目 = --port の照合
        self.assertIn("False", lines[0], "879 の worker が 8790 を誤って自分のものと判定している")
        self.assertIn("True", lines[0])
        self.assertIn("False", lines[1])
        self.assertIn("True", lines[1])

        jp_cfg = WIN.build_config(os.path.join(tmp, "日本語home"),
                                  os.path.join(tmp, "日本語repo"), "127.0.0.1", 8790,
                                  startup_dir=os.path.join(tmp, "日本語startup"))
        rendered = [WIN.render_worker(jp_cfg), WIN.render_startup(jp_cfg),
                    WIN.render_proxy_worker(jp_cfg), WIN.render_proxy_startup(jp_cfg),
                    WIN.render_launcher(jp_cfg), WIN.render_proxy_launcher(jp_cfg)]
        for source in rendered[:4]:
            non_ascii = sorted({ch for ch in source if ord(ch) > 127})
            self.assertEqual(non_ascii, [], "生成 VBS に非 ASCII が残っている（cscript が読めない）")
        for source in rendered[4:]:
            ast.parse(source)   # .pyw 側は日本語のまま（UTF-8）で構文が通ること


if __name__ == "__main__":
    unittest.main()
