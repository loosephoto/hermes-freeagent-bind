"""v0.49.0: 常駐プロセス生成の監査で見つかった 4 点の回帰（ネットワーク不要）。

各テストは監査の**実測値**を固定する。

1. **Python の選定は版番号順**。素の文字列比較は `python-3.14.10` より `python-3.14.9` を上に置く
   （実測: 生成器の `sorted(reverse=True)` も worker VBS の `StrComp` も 3.14.9 を選んだ）。
2. **見張りタスクは電源に依らず動く**。`schtasks /create /sc minute` の簡易登録は
   「バッテリーでは開始しない／停止する」が True になる（実測）。XML 定義で false にする。
3. **応答なしの検知**。プロセスが居てもポートが答えないティックが 2 回続いたら終了して起動し直す
   （1 回では落とさない・答えていれば落とさない・別ポートのプロセスは触らない）。
4. **セッション由来の環境変数を常駐へ持ち込まない**。Hermes のセッションから起動し直すと、
   親の環境を引き継ぐ（実測: ログオン起動は 48 個・セッションからは 25 個を .env から読んだ）。

VBS を実際に走らせるテストは Windows だけ（`cscript.exe`）。それ以外の OS では生成物の文面を検査する。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402

HAS_CSCRIPT = os.name == "nt" and bool(shutil.which("cscript.exe"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


def _load(name: str):
    path = os.path.join(ROOT, "scripts", name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cscript(path: str, *args: str, env=None, timeout: int = 120) -> str:
    proc = subprocess.run(["cscript.exe", "//nologo", path, *args], capture_output=True,
                          env=env, creationflags=NO_WINDOW, timeout=timeout)
    return (proc.stdout + proc.stderr).decode("mbcs" if os.name == "nt" else "utf-8", "replace").strip()


def _vbs_function(vbs: str, name: str) -> str:
    """生成された VBS から `Function <name> … End Function` を 1 つ取り出す。"""
    match = re.search(r"^Function %s\(.*?^End Function\s*$" % re.escape(name), vbs, re.S | re.M)
    assert match, "Function %s が見つからない" % name
    return match.group(0)


class _Base(unittest.TestCase):
    def setUp(self):
        self.m = _load("install_resident_windows")
        self.tmp = tempfile.mkdtemp(prefix="resident-v0490-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.local = os.path.join(self.tmp, "Local")
        self.home = os.path.join(self.local, "hermes")
        self.startup_dir = os.path.join(self.tmp, "Startup")
        self.repo = os.path.join(self.tmp, "hermes-freeagent-bind")
        os.makedirs(os.path.join(self.repo, "src", "freeagent_bind"))
        with open(os.path.join(self.repo, "src", "freeagent_bind", "server.py"), "w",
                  encoding="utf-8") as fh:
            fh.write("# stub\n")
        os.makedirs(self.home)

    def _cfg(self, port: int = 8790, proxy_port: int = 8645, host: str = "127.0.0.1"):
        with mock.patch.object(self.m, "_interpreter_ok", return_value=True):
            return self.m.build_config(self.home, self.repo, host, port,
                                       startup_dir=self.startup_dir, proxy_port=proxy_port)

    def _python_dirs(self, names):
        for name in names:
            folder = os.path.join(self.home, "tools", name)
            os.makedirs(folder, exist_ok=True)
            for exe in ("python.exe", "pythonw.exe"):
                with open(os.path.join(folder, exe), "w", encoding="ascii") as fh:
                    fh.write("stub")


# --------------------------------------------------------------------------- 1. 版番号順

class PythonSelectionCase(_Base):
    NAMES = ["python-3.14.7+20260901-win32-x64",
             "python-3.14.9+20261021-win32-x64",
             "python-3.14.10+20261201-win32-x64"]

    def test_plain_string_order_is_the_bug(self):
        """陽性対照: 文字列順は 3.14.9 を選ぶ（監査の実測そのまま）。"""
        self.assertIn("3.14.9+", sorted(self.NAMES, reverse=True)[0])

    def test_key_orders_by_version_then_build(self):
        key = self.m._python_dir_key
        self.assertIn("3.14.10+", sorted(self.NAMES, key=key, reverse=True)[0])
        self.assertGreater(key("python-3.15.0+20270101-win32-x64"), key("python-3.14.99+20270101-win32-x64"))
        self.assertGreater(key("python-3.100.0+2026-win32-x64"), key("python-3.99.0+2026-win32-x64"))
        # 同じ版ならビルド番号の新しい方。
        self.assertGreater(key("python-3.14.9+20261105-win32-x64"), key("python-3.14.9+20261021-win32-x64"))

    @unittest.skipUnless(os.name == "nt", "PE ヘッダの検査は Windows だけ")
    def test_a_file_that_is_not_an_executable_is_never_spawned(self):
        """PE でない `python.exe` を窓なしの親から起動すると timeout（20 秒）まで戻らない（実測）。"""
        self._python_dirs(self.NAMES[:1])
        stub = os.path.join(self.home, "tools", self.NAMES[0], "python.exe")
        with mock.patch.object(self.m.subprocess, "run") as run:
            self.assertFalse(self.m._interpreter_ok(stub))
            self.assertFalse(self.m._interpreter_ok(os.path.join(self.tmp, "missing.exe")))
        run.assert_not_called()
        self.assertTrue(self.m._interpreter_ok(sys.executable), "本物のインタプリタは通すこと")

    def test_generator_picks_3_14_10_over_3_14_9(self):
        self._python_dirs(self.NAMES)
        with mock.patch.object(self.m, "_interpreter_ok", return_value=True):
            found, found_w = self.m.discover_python(self.home)
        self.assertIn("python-3.14.10+", found, found)
        self.assertIn("python-3.14.10+", found_w, found_w)

    def test_posix_generator_uses_the_same_order(self):
        posix = _load("install_resident")
        for name in ("python-3.14.9+20261021-linux-x64", "python-3.14.10+20261201-linux-x64"):
            bin_dir = os.path.join(self.home, "tools", name, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "python3"), "w", encoding="ascii", newline="\n") as fh:
                fh.write("#!/bin/sh\n")
        with mock.patch.object(posix, "_interpreter_ok", return_value=True):
            found, _ = posix.discover_python(self.home, platform_name="linux")
        self.assertIn("python-3.14.10+", found, found)

    def test_workers_compare_natural_keys_not_folder_names(self):
        cfg = self._cfg()
        for vbs in (self.m.render_worker(cfg), self.m.render_proxy_worker(cfg)):
            self.assertIn("key = NatKey(f.Name)", vbs)
            self.assertIn("StrComp(key, bestKey, vbBinaryCompare) > 0", vbs)
            self.assertNotIn("StrComp(f.Name, bestName", vbs, "フォルダ名を直接比べると 3.14.9 を選ぶ")
            self.assertIn("HasStdlib(f)", vbs, "標準ライブラリの無い残骸を選ばないこと")
            self.assertIn("Function NatKey(s)", vbs)
            self.assertIn("Function HasStdlib(folder)", vbs)

    @unittest.skipUnless(HAS_CSCRIPT, "cscript.exe（Windows）が要る")
    def test_vbs_picks_3_14_10_and_skips_a_folder_without_stdlib(self):
        """生成物の関数を**実際に走らせる**。Lib を失った最新フォルダは選ばない。"""
        tools = os.path.join(self.home, "tools")
        self._python_dirs(self.NAMES + ["python-3.14.11+20270101-win32-x64"])
        for name in self.NAMES:   # 3.14.11 だけ Lib 無し（残骸）
            os.makedirs(os.path.join(tools, name, "Lib"))
            with open(os.path.join(tools, name, "Lib", "os.py"), "w", encoding="ascii") as fh:
                fh.write("")
        harness = "\r\n".join([
            "Option Explicit",
            "Dim fso, f, key, bestKey, bestName",
            'Set fso = CreateObject("Scripting.FileSystemObject")',
            'bestKey = ""', 'bestName = ""',
            "For Each f In fso.GetFolder(WScript.Arguments(0)).SubFolders",
            "  If HasStdlib(f) Then",
            "    key = NatKey(f.Name)",
            "    If StrComp(key, bestKey, vbBinaryCompare) > 0 Then",
            "      bestKey = key",
            "      bestName = f.Name",
            "    End If",
            "  End If",
            "Next",
            "WScript.Echo bestName",
            self.m._VBS_PYTHON_PICK.replace("\n", "\r\n"),
        ])
        path = os.path.join(self.tmp, "pick.vbs")
        with open(path, "w", encoding="ascii", newline="") as fh:
            fh.write(harness)
        self.assertEqual(_cscript(path, tools), "python-3.14.10+20261201-win32-x64")


# --------------------------------------------------------------------------- 2. 見張りタスク

class WatchdogTaskCase(_Base):
    def _settings(self, xml: str):
        root = ET.fromstring(xml.split("?>", 1)[1])
        settings = root.find(TASK_NS + "Settings")
        return {child.tag.replace(TASK_NS, ""): (child.text or "").strip() for child in settings}, root

    def test_task_runs_regardless_of_power_source(self):
        worker = r"C:\Users\a & b\freeagent-service\Hermes_Freeagent.vbs"
        xml = self.m.render_task_xml(worker, 5, start="2026-11-13T12:00:00")
        settings, root = self._settings(xml)
        self.assertEqual(settings["DisallowStartIfOnBatteries"], "false")
        self.assertEqual(settings["StopIfGoingOnBatteries"], "false")
        self.assertEqual(settings["StartWhenAvailable"], "true")
        self.assertEqual(settings["RunOnlyIfIdle"], "false")
        self.assertEqual(settings["MultipleInstancesPolicy"], "IgnoreNew")
        repetition = root.find(f"{TASK_NS}Triggers/{TASK_NS}TimeTrigger/{TASK_NS}Repetition")
        self.assertEqual(repetition.find(TASK_NS + "Interval").text, "PT5M")
        self.assertIsNone(repetition.find(TASK_NS + "Duration"), "Duration があると繰り返しが期限で止まる")
        action = root.find(f"{TASK_NS}Actions/{TASK_NS}Exec")
        self.assertEqual(action.find(TASK_NS + "Command").text, "wscript.exe")
        # `&` を含むパスも壊れずに 1 引数（引用符つき）で渡ること。
        self.assertEqual(action.find(TASK_NS + "Arguments").text, '"%s"' % worker)
        principal = root.find(f"{TASK_NS}Principals/{TASK_NS}Principal")
        self.assertEqual(principal.find(TASK_NS + "LogonType").text, "InteractiveToken")
        self.assertIsNone(principal.find(TASK_NS + "UserId"), "利用者名を定義へ書き込まない")

    def test_interval_follows_the_argument(self):
        _, root = self._settings(self.m.render_task_xml("w.vbs", 12))
        self.assertEqual(root.find(f"{TASK_NS}Triggers/{TASK_NS}TimeTrigger/{TASK_NS}Repetition/"
                                   f"{TASK_NS}Interval").text, "PT12M")

    def test_registration_goes_through_the_xml_definition(self):
        calls = []

        class _Done:
            returncode = 0
            stdout = b"ok"
            stderr = b""

        def fake_run(cmd, **kwargs):
            xml_path = cmd[cmd.index("/xml") + 1]
            with open(xml_path, "rb") as fh:
                raw = fh.read()
            calls.append((cmd, xml_path, raw, kwargs))
            return _Done()

        with mock.patch.object(self.m.subprocess, "run", side_effect=fake_run):
            message = self.m._register_schtask(r"C:\x\Hermes_Freeagent.vbs", "T_Watchdog", 5)
        self.assertEqual(len(calls), 1)
        cmd, xml_path, raw, kwargs = calls[0]
        self.assertFalse(kwargs.get("text"), "text=True は PYTHONUTF8=1 の下で cp932 の出力を復号できず落ちる")
        self.assertEqual(cmd[:4], ["schtasks", "/create", "/tn", "T_Watchdog"])
        self.assertIn("/f", cmd)
        self.assertNotIn("/sc", cmd, "簡易登録（/sc minute）は電源の既定値が付く")
        self.assertTrue(raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"), "UTF-16（BOM つき）で渡すこと")
        self.assertIn("StopIfGoingOnBatteries>false", raw.decode("utf-16"))
        self.assertFalse(os.path.exists(xml_path), "一時ファイルを残さないこと")
        self.assertIn("task registered: T_Watchdog", message)

    def test_registration_failure_is_reported_and_cleans_up(self):
        seen = []

        class _Failed:
            returncode = 1
            stdout = b""
            # schtasks はコンソールのコードページで書く（日本語 Windows は cp932）。UTF-8 では復号できない。
            stderr = "エラー: access denied".encode("cp932")

        def fake_run(cmd, **kwargs):
            seen.append(cmd[cmd.index("/xml") + 1])
            return _Failed()

        with mock.patch.object(self.m.subprocess, "run", side_effect=fake_run):
            message = self.m._register_schtask("w.vbs", "T", 5)
        self.assertIn("task failed (1)", message)
        self.assertIn("access denied", message, "失敗理由が消えている")
        self.assertEqual(self.m._decode_console(b""), "")
        self.assertEqual(self.m._decode_console(None), "")
        self.assertEqual(self.m._decode_console("already text"), "already text")
        self.assertIn("access denied", self.m._decode_console(b"\x90\xac\x8c\xf7 access denied"),
                      "UTF-8 として不正なバイト列でも例外にしない")
        self.assertFalse(os.path.exists(seen[0]))


# --------------------------------------------------------------------------- 3. 応答なしの検知

class UnresponsiveDetectionCase(_Base):
    HANG = ("import socket,sys,time\n"
            "s=socket.socket();s.bind(('127.0.0.1',int(sys.argv[1])));s.listen(5)\n"
            "time.sleep(600)")
    ANSWER = ("import http.server,sys\n"
              "class H(http.server.BaseHTTPRequestHandler):\n"
              "    def log_message(self,*a):\n        pass\n"
              "http.server.ThreadingHTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()")

    def test_workers_probe_with_connection_close_and_count_strikes(self):
        cfg = self._cfg(port=8802)
        for vbs in (self.m.render_worker(cfg), self.m.render_proxy_worker(cfg)):
            self.assertIn('rq.SetRequestHeader "Connection", "close"', vbs,
                          "無いとサーバーが接続確認 1 回ごとに ConnectionResetError を吐く（実測）")
            self.assertIn("If strikes < %d Then" % self.m.UNRESPONSIVE_STRIKES, vbs)
            self.assertIn("x.Terminate", vbs)
            self.assertNotIn("__", re.sub(r"\b__main__\b", "", vbs), "プレースホルダが残っている")
        self.assertGreaterEqual(self.m.UNRESPONSIVE_STRIKES, 2, "1 回で落とすと起動直後のサーバーを殺す")
        worker = self.m.render_worker(cfg)
        self.assertIn('probeUrl = "http://127.0.0.1:8802/"', worker)
        self.assertIn(self.m.WATCHDOG_LOG_NAME, worker)
        self.assertIn(self.m.PROXY_WATCHDOG_LOG_NAME, self.m.render_proxy_worker(cfg))

    def test_probe_targets_loopback_when_listening_on_any_address(self):
        """`0.0.0.0` へは接続できないので、そのまま叩くと健全なサーバーを「応答なし」と誤判定する。"""
        self.assertEqual(self.m.probe_host("0.0.0.0"), "127.0.0.1")
        self.assertEqual(self.m.probe_host("::"), "[::1]")
        self.assertEqual(self.m.probe_host("::1"), "[::1]")
        self.assertEqual(self.m.probe_host("127.0.0.1"), "127.0.0.1")
        worker = self.m.render_worker(self._cfg(port=8803, host="0.0.0.0"))
        self.assertIn('probeUrl = "http://127.0.0.1:8803/"', worker)
        self.assertIn("--host 0.0.0.0 --port 8803", worker, "待受の指定は変えない")

    def test_only_python_processes_count_as_the_server(self):
        """目印を自分のコマンドラインに含むだけのシェルを「起動済み」「終了対象」にしない。"""
        cfg = self._cfg()
        for vbs, name in ((self.m.render_worker(cfg), "IsServer"), (self.m.render_proxy_worker(cfg), "IsProxy")):
            body = _vbs_function(vbs, name)
            self.assertIn('If LCase(Left(name, 6)) <> "python" Then Exit Function', body)

    @unittest.skipUnless(HAS_CSCRIPT, "cscript.exe（Windows）が要る")
    def test_proxy_match_is_bound_to_its_port(self):
        """`--port` 無しは Hermes 既定（8645）のプロキシ。別ポートのプロキシを巻き込まない。"""
        vbs = self.m.render_proxy_worker(self._cfg(proxy_port=8645))
        base = r"C:\t\python.exe -m hermes_cli.main proxy start"
        cases = [("python.exe", base), ("python.exe", base + " --port 9001"),
                 ("python.exe", base + " --port 90010"), ("cmd.exe", base),
                 ("pythonw.exe", r"C:\t\pythonw.exe run_proxy.pyw hermes-proxy-8645")]
        lines = ["Option Explicit", "Dim port", "port = WScript.Arguments(0)"]
        for name, cmd in cases:
            lines.append('WScript.Echo CStr(IsProxy("%s", "%s"))' % (name, cmd))
        lines.append(_vbs_function(vbs, "IsProxy").replace("\n", "\r\n"))
        path = os.path.join(self.tmp, "isproxy.vbs")
        with open(path, "w", encoding="ascii", newline="") as fh:
            fh.write("\r\n".join(lines))
        truth = {"True": True, "False": False}
        as_8645 = [truth[x.strip()] for x in _cscript(path, "8645").splitlines()]
        as_9001 = [truth[x.strip()] for x in _cscript(path, "9001").splitlines()]
        self.assertEqual(as_8645, [True, False, False, False, False])
        self.assertEqual(as_9001, [False, True, False, False, False])

    def _run_worker(self, worker_path: str) -> str:
        env = dict(os.environ, LOCALAPPDATA=self.local)
        return _cscript(worker_path, env=env)

    def _spawn(self, code: str, port: int, marker: str):
        proc = subprocess.Popen([sys.executable, "-c", code, str(port), marker],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=NO_WINDOW)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                return proc
            except OSError:
                time.sleep(0.1)
        self.fail("補助プロセスが待受に入らない")

    @unittest.skipUnless(HAS_CSCRIPT, "cscript.exe（Windows）が要る")
    def test_worker_restarts_a_hung_server_only_after_two_ticks(self):
        """worker VBS を実際に走らせる。1 回目は記録だけ、2 回目で終了、健全なら何もしない。"""
        port = _free_port()
        cfg = self._cfg(port=port)
        self.m.write_plan([(cfg["paths"]["worker"], self.m.render_worker(cfg))], force=True)
        worker = cfg["paths"]["worker"]
        strike = os.path.join(os.path.dirname(worker), "unresponsive-%d.count" % port)
        watch = os.path.join(self.home, "logs", self.m.WATCHDOG_LOG_NAME)
        marker = self.m.marker_for(port)

        def log():
            if not os.path.exists(watch):
                return ""
            with open(watch, encoding="mbcs", errors="replace") as fh:
                return fh.read()

        # (a) サーバー無し: python が無いので起動せず、理由をログに残して終わる（実行時エラーなし）。
        self.assertEqual(self._run_worker(worker), "")
        self.assertIn("no usable python-* folder", log())

        # (b) 答えるサーバー: 終了させない。前回の記録（1 回分）は消える。
        healthy = self._spawn(self.ANSWER, port, marker)
        with open(strike, "w", encoding="ascii") as fh:
            fh.write("1\n")
        self.assertEqual(self._run_worker(worker), "")
        self.assertIsNone(healthy.poll(), "答えているサーバーを終了させた")
        self.assertFalse(os.path.exists(strike), "健全なら連続回数を消すこと")
        healthy.kill()
        healthy.wait(10)

        # (c) プロセスは居るが答えない: 1 回目は記録だけ。
        hung = self._spawn(self.HANG, port, marker)
        self.assertEqual(self._run_worker(worker), "")
        self.assertIsNone(hung.poll(), "1 回の無応答で終了させた（起動直後のサーバーを殺す）")
        with open(strike, encoding="ascii") as fh:
            self.assertEqual(fh.read().strip(), "1")
        self.assertIn("did not answer (1/2)", log())

        # (d) 2 回続いたら終了させ、記録を消す。
        self.assertEqual(self._run_worker(worker), "")
        hung.wait(10)
        self.assertIsNotNone(hung.poll(), "2 回連続の無応答でも終了していない")
        self.assertFalse(os.path.exists(strike))
        self.assertIn("terminating unresponsive server pid=%d" % hung.pid, log())


# --------------------------------------------------------------------------- 4. セッション由来の環境変数

class SessionEnvCase(_Base):
    def _helper(self, source: str) -> dict:
        tree = ast.parse(source)
        wanted = {"SESSION_MARKER_PREFIX", "SESSION_PREFIXES", "SESSION_EXACT", "SESSION_KEEP"}
        nodes = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("_drop_session_env", "_persistent_env_names"):
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in wanted for t in node.targets):
                nodes.append(node)
        namespace: dict = {}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<launcher>", "exec"), namespace)
        self.assertEqual(wanted - set(namespace), set())
        return namespace

    def _environ(self) -> dict:
        return {
            "HERMES_SESSION_ID": "s-1", "HERMES_UI_SESSION_ID": "u-1", "HERMES_QUIET": "1",
            "TERMINAL_CWD": r"C:\w", "TMPDIR": r"C:\scratch",
            "FREEAGENT_STATE_DIR": r"C:\scratch\fa-state", "FREEAGENT_HTTP_TOKEN": "persistent",
            "FREEAGENT_ENV_FILE": r"C:\t\x.env", "HERMES_PROXY_FORCE_START": "1",
            "OPENROUTER_API_KEY": "k", "PATH": "p", "TEMP": r"C:\Temp",
        }

    def test_both_launchers_drop_session_variables_only(self):
        cfg = self._cfg()
        for source in (self.m.render_launcher(cfg), self.m.render_proxy_launcher(cfg)):
            ns = self._helper(source)
            environ = self._environ()
            dropped = ns["_drop_session_env"](environ, {"FREEAGENT_HTTP_TOKEN", "PATH", "TEMP"})
            self.assertEqual(dropped, ["FREEAGENT_STATE_DIR", "HERMES_QUIET", "HERMES_SESSION_ID",
                                       "HERMES_UI_SESSION_ID", "TERMINAL_CWD", "TMPDIR"])
            # レジストリにある値・launcher への指示・キー・一般の変数は残る。
            self.assertEqual(sorted(environ), ["FREEAGENT_ENV_FILE", "FREEAGENT_HTTP_TOKEN",
                                               "HERMES_PROXY_FORCE_START", "OPENROUTER_API_KEY",
                                               "PATH", "TEMP"])

    def test_nothing_is_dropped_outside_a_session_or_without_the_registry(self):
        ns = self._helper(self.m.render_launcher(self._cfg()))
        drop = ns["_drop_session_env"]
        plain = {"FREEAGENT_STATE_DIR": "x", "TMPDIR": "y", "PATH": "p"}   # 利用者が手で設定して起動
        self.assertEqual(drop(dict(plain), set()), [])
        session = self._environ()
        self.assertEqual(drop(dict(session), None), [], "レジストリが読めないときは何も落とさない")
        keep = dict(session, FREEAGENT_KEEP_SESSION_ENV="1")
        self.assertEqual(drop(keep, set()), [], "意図的な持ち込みは opt-out で残せる")
        self.assertIn("FREEAGENT_STATE_DIR", keep)
        # launcher への指示だけ・レジストリに登録した HERMES_* だけでは「Hermes から起動された」としない。
        forced = {"HERMES_PROXY_FORCE_START": "1", "FREEAGENT_STATE_DIR": "x"}
        self.assertEqual(drop(dict(forced), set()), [])
        registered = {"HERMES_HOME": r"D:\hermes", "FREEAGENT_STATE_DIR": "x"}
        self.assertEqual(drop(dict(registered), {"HERMES_HOME"}), [])

    def test_any_unregistered_hermes_variable_marks_a_hermes_parent(self):
        """名前を並べる判定は漏れる（実測: コード実行の子には HERMES_SESSION_ID が無かった）。"""
        ns = self._helper(self.m.render_launcher(self._cfg()))
        kernel_shape = {"HERMES_RPC_TOKEN": "secret", "HERMES_KERNEL_SENTINEL": "1",
                        "TERMINAL_TIMEOUT": "180", "FREEAGENT_STATE_DIR": "x", "PATH": "p"}
        dropped = ns["_drop_session_env"](kernel_shape, set())
        self.assertEqual(dropped, ["FREEAGENT_STATE_DIR", "HERMES_KERNEL_SENTINEL", "HERMES_RPC_TOKEN",
                                   "TERMINAL_TIMEOUT"])
        self.assertEqual(kernel_shape, {"PATH": "p"})

    def test_server_launcher_drops_before_loading_env_files(self):
        """落とすのが後だと、`実環境 > ファイル` の規則で .env の値が入らないまま起動する。"""
        source = self.m.render_launcher(self._cfg())
        compile(source, "run_server.pyw", "exec")
        self.assertNotIn("__SESSION_ENV_HELPER__", source)
        drop_at = source.index("_dropped = _drop_session_env(os.environ, _persistent_env_names(), _file_keys)")
        load_at = source.index("for _env_path in ENV_FILES:")
        self.assertLess(drop_at, load_at)
        self.assertIn("[env] dropped %d session-inherited vars", source)

    def test_names_defined_by_the_env_files_take_the_file_value_in_a_session(self):
        """接頭辞の外の名前も、env ファイルが定義していれば親の値を捨てる（実測: 15 個が残っていた）。"""
        source = self.m.render_launcher(self._cfg())
        ns = self._helper(source)
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_env_file_keys")
        scope = {"os": os}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<launcher>", "exec"), scope)
        path = os.path.join(self.tmp, "keys.env")
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# c\nexport BROWSER_SESSION_TIMEOUT=300\nWEB_TOOLS_DEBUG=false\nEMPTY=\nnot a pair\n")
        keys = scope["_env_file_keys"](path)
        self.assertEqual(keys, ["BROWSER_SESSION_TIMEOUT", "WEB_TOOLS_DEBUG", "EMPTY"])
        self.assertEqual(scope["_env_file_keys"](os.path.join(self.tmp, "missing.env")), [])
        environ = dict(self._environ(), BROWSER_SESSION_TIMEOUT="from-session", WEB_TOOLS_DEBUG="true")
        dropped = ns["_drop_session_env"](environ, {"WEB_TOOLS_DEBUG"}, keys)
        self.assertIn("BROWSER_SESSION_TIMEOUT", dropped)
        self.assertNotIn("WEB_TOOLS_DEBUG", dropped, "レジストリに登録した値は残す")
        self.assertNotIn("PATH", dropped)
        # セッションの外では、ファイルが定義する名前でも実環境が勝つ（従来の規則）。
        plain = {"BROWSER_SESSION_TIMEOUT": "user-set", "PATH": "p"}
        self.assertEqual(ns["_drop_session_env"](plain, set(), keys), [])

    def test_proxy_launcher_cleans_the_child_environment_only(self):
        source = self.m.render_proxy_launcher(self._cfg())
        compile(source, "run_proxy.pyw", "exec")
        self.assertNotIn("__SESSION_ENV_HELPER__", source)
        self.assertIn("dropped = _drop_session_env(env, _persistent_env_names())", source)
        self.assertNotIn("_drop_session_env(os.environ", source,
                         "launcher 自身の環境（HERMES_PROXY_LOG 等の指示）は触らない")

    @unittest.skipUnless(os.name == "nt", "レジストリは Windows だけ")
    def test_registry_names_are_readable_on_windows(self):
        ns = self._helper(self.m.render_launcher(self._cfg()))
        names = ns["_persistent_env_names"]()
        self.assertIsInstance(names, set)
        self.assertIn("PATH", names, "HKLM の Path が読めていない")


# --------------------------------------------------------------------------- 接続確認の後始末

class ClientDisconnectCase(unittest.TestCase):
    """相手が keep-alive の接続を黙って切っても、常駐ログにトレースバックを残さない。"""

    def setUp(self):
        cfg = S.http_config(["--transport", "http", "--host", "127.0.0.1", "--port", "0"])
        self.server = S._http_build_server(cfg)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_reset_by_peer_is_not_a_traceback(self):
        seen = threading.Event()
        original = type(self.server).handle_error

        def spy(server, request, client_address):
            try:
                return original(server, request, client_address)
            finally:
                seen.set()

        captured = io.StringIO()
        with mock.patch.object(type(self.server), "handle_error", spy), \
                mock.patch.object(sys, "stderr", captured):
            sock = socket.create_connection(("127.0.0.1", self.port), 5)
            sock.sendall(b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")   # keep-alive のまま
            self.assertIn(b"404", sock.recv(4096))
            # SO_LINGER(on, 0) で閉じると FIN ではなく RST が飛ぶ（WinHttp が接続を捨てるときと同じ）。
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            struct.pack("hh" if os.name == "nt" else "ii", 1, 0))
            sock.close()
            self.assertTrue(seen.wait(10), "切断が handle_error まで届いていない（テストが空振り）")
        self.assertNotIn("Traceback", captured.getvalue())
        self.assertNotIn("ConnectionResetError", captured.getvalue())

    def test_other_errors_still_reach_the_default_handler(self):
        captured = io.StringIO()
        with mock.patch.object(sys, "stderr", captured):
            try:
                raise ValueError("boom-v0490")
            except ValueError:
                self.server.handle_error(None, ("127.0.0.1", 1))
        self.assertIn("boom-v0490", captured.getvalue(), "切断以外の例外まで握り潰している")


if __name__ == "__main__":
    unittest.main()
