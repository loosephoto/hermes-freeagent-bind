"""v0.51.0: macOS（launchd）常駐配線の監査で見つかった 5 点の再発防止（SPEC §6.66）。

Linux 版（`systemd --user`）を元に写したことで抜けていた「macOS 固有の起動前検査」を固定する。

1. launchd は `StandardOutPath` の**親ディレクトリを作らない**（無いと `last exit status = 78` で無言停止）
   → 生成時に `<hermes-home>/logs` を作り、plist が指すログの親が実在することを確かめる。
2. `bootstrap` の rc 0 は「登録できた」だけ。稼働は `launchctl print` の `state` / `last exit status` で見る。
3. `gui/<uid>` ドメインへ到達できるかを `launchctl print gui/<uid>` で確かめる（SSH では登録できない）。
4. `PATH` は OS 別（Linux に `/opt/homebrew` を入れない・macOS に `~/.local/bin` を入れる）。
5. SIGTERM で待機から即座に抜ける（PEP 475 により `time.sleep` は残り時間を寝続ける）。
"""
from __future__ import annotations

from typing import Any
import ast
import importlib.util
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    path = os.path.join(ROOT, "scripts", "install_resident.py")
    spec = importlib.util.spec_from_file_location("install_resident", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ResidentMacosCase(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.mkdtemp(prefix="fa-resident-macos-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "hermes")
        self.unit_dir = os.path.join(self.tmp, "LaunchAgents")
        self.repo = os.path.join(self.tmp, "hermes-freeagent-bind")
        os.makedirs(os.path.join(self.repo, "src", "freeagent_bind"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "freeagent_bind", "server.py"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# stub\n")
        self.python = os.path.join(self.tmp, "python3")
        with open(self.python, "w", encoding="ascii", newline="\n") as fh:
            fh.write("#!/bin/sh\n")

    def _cfg(self, platform_name: str = "macos"):
        return self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.python, startup_dir=self.unit_dir, platform_name=platform_name,
        )

    # --- 1. launchd の起動前検査（ログの親ディレクトリ） -------------------

    def test_write_creates_log_dir_that_launchd_opens_before_exec(self):
        """`StandardOutPath` の親が無いと launchd は起動しない（78）。生成時に作ること。"""
        for platform_name in ("macos", "linux"):
            sub_home = os.path.join(self.tmp, platform_name, "hermes")
            sub_units = os.path.join(self.tmp, platform_name, "units")
            self.assertFalse(os.path.isdir(os.path.join(sub_home, "logs")))
            with mock.patch.object(self.m, "_host_matches_target", return_value=True):
                rc = self.m.main([
                    "--platform", platform_name,
                    "--hermes-home", sub_home,
                    "--repo", self.repo,
                    "--unit-dir", sub_units,
                    "--python", self.python,
                    "--write", "--force",
                ])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(os.path.join(sub_home, "logs")), platform_name)

            cfg = self.m.build_config(
                sub_home, self.repo, "127.0.0.1", 8790,
                python=self.python, startup_dir=sub_units, platform_name=platform_name,
            )
            log_path = cfg["paths"]["log"]
            self.assertEqual(os.path.dirname(log_path), os.path.join(sub_home, "logs"))
            self.assertTrue(os.path.isdir(os.path.dirname(log_path)), "launchd が起動前に開けない")
            self.assertEqual(self.m.verify_generated(cfg), [])

            if platform_name == "macos":
                with open(cfg["paths"]["unit"], "rb") as fh:
                    plist = plistlib.load(fh)
                for key in ("StandardOutPath", "StandardErrorPath"):
                    self.assertEqual(plist[key], log_path)
                with open(cfg["paths"]["proxy_unit"], "rb") as fh:
                    proxy_plist = plistlib.load(fh)
                self.assertEqual(proxy_plist["StandardOutPath"], cfg["paths"]["proxy_log"])
                self.assertEqual(os.path.dirname(proxy_plist["StandardOutPath"]),
                                 os.path.join(sub_home, "logs"))

    def test_dry_run_does_not_create_log_dir(self):
        sub_home = os.path.join(self.tmp, "dry", "hermes")
        rc = self.m.main([
            "--platform", "macos", "--hermes-home", sub_home, "--repo", self.repo,
            "--unit-dir", os.path.join(self.tmp, "dry", "units"), "--dry-run",
        ])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(sub_home), "表示のみでディレクトリを作っている")

    def test_verify_generated_reports_missing_log_dir_and_absent_python(self):
        cfg = self._cfg()
        self.m.write_plan(self.m.build_plan(cfg), force=True)
        with mock.patch.object(self.m, "_host_matches_target", return_value=True):
            problems = self.m.verify_generated(cfg)
        self.assertTrue(any("log" in p and "親ディレクトリ" in p for p in problems), problems)
        self.assertFalse(any("生成されていません" in p for p in problems), problems)

        self.assertEqual(self.m.ensure_runtime_dirs(cfg), ["created dir: %s" % os.path.join(self.home, "logs")])
        with mock.patch.object(self.m, "_host_matches_target", return_value=True):
            self.assertEqual(self.m.verify_generated(cfg), [])
        self.assertEqual(self.m.ensure_runtime_dirs(cfg), [], "2 回目は何も作らない")

    # --- 2/3. launchctl による到達確認と稼働確認 -------------------------

    def test_detect_macos_service_ready_probes_the_gui_domain(self):
        def run_fail(cmd, **_kwargs):
            return subprocess.CompletedProcess(cmd, 113, stdout="",
                                               stderr="Could not find domain for\n")

        with mock.patch.object(self.m.shutil, "which", return_value="/bin/launchctl"), \
             mock.patch.object(self.m, "_posix_uid", return_value=501), \
             mock.patch.object(self.m.subprocess, "run", side_effect=run_fail):
            ok, detail, domain = self.m.detect_macos_service_ready()
        self.assertFalse(ok)
        self.assertEqual(domain, "")
        self.assertIn("GUI ログインセッション", detail)
        self.assertIn("gui/501", detail)

        with mock.patch.object(self.m.shutil, "which", return_value="/bin/launchctl"), \
             mock.patch.object(self.m, "_posix_uid", return_value=501), \
             mock.patch.object(self.m.subprocess, "run",
                               side_effect=lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "gui/501", "")):
            ok, detail, domain = self.m.detect_macos_service_ready()
        self.assertTrue(ok, detail)
        self.assertEqual(domain, "gui/501")

    def _run_register(self, print_stdout: str, print_rc: int = 0):
        cfg = self._cfg()
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            if cmd[1] == "print":
                # 到達確認（gui/<uid>）は成功させる。判定対象はラベル側の print。
                if len(cmd) == 3 and cmd[2].startswith("gui/") and "/" not in cmd[2][4:]:
                    return subprocess.CompletedProcess(cmd, 0, "state = running\n", "")
                return subprocess.CompletedProcess(cmd, print_rc, print_stdout, "")
            if cmd[1] == "bootout":
                return subprocess.CompletedProcess(cmd, 3, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(self.m.shutil, "which", return_value="/bin/launchctl"), \
             mock.patch.object(self.m, "_posix_uid", return_value=501), \
             mock.patch.object(self.m.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(self.m.time, "sleep", return_value=None):
            out = self.m.register_macos_agents(cfg)
        return out, calls

    def test_register_macos_agents_fails_on_exit_status_78(self):
        """bootstrap が 0 でも、起動前の失敗は `last exit status = 78` として現れる。"""
        out, calls = self._run_register("\tstate = exited\n\tlast exit status = 78\n")
        self.assertEqual(len(out), 2, out)  # サーバーとプロキシの両方が 78 で落ちる
        self.assertTrue(out[0].startswith("service failed"), out)
        self.assertIn("78", out[0])
        self.assertIn("launchctl print gui/501/ai.hermes.freeagent-bind", out[0])
        self.assertIn(["launchctl", "print", "gui/501/ai.hermes.freeagent-bind"], calls)

    def test_register_macos_agents_reports_running_and_intermediate_states(self):
        out, _ = self._run_register("\tstate = running\n\tlast exit status = 0\n")
        self.assertEqual(out[0], "service bootstrapped: ai.hermes.freeagent-bind (gui/501)")

        out, _ = self._run_register("\tstate = spawn scheduled\n")
        self.assertTrue(out[0].startswith("service started"), out)
        self.assertIn("spawn scheduled", out[0])

        out, _ = self._run_register("", print_rc=113)
        self.assertTrue(out[0].startswith("service failed"), out)

    def test_parse_launchctl_print(self):
        state, status = self.m._parse_launchctl_print("\tstate = running\n\tlast exit status = 0\n")
        self.assertEqual((state, status), ("running", 0))
        self.assertEqual(self.m._parse_launchctl_print(""), ("", None))
        self.assertEqual(self.m._parse_launchctl_print("\tlast exit status = 78\n"), ("", 78))

    # --- 4. PATH は OS 別 ------------------------------------------------

    def test_path_is_platform_specific(self):
        mac_path = self.m.platform_path("macos", "/Users/alice", "/Users/alice/.hermes")
        self.assertTrue(mac_path.startswith("/Users/alice/.hermes/bin:"), mac_path)
        self.assertIn("/Users/alice/.local/bin", mac_path)
        self.assertIn("/opt/homebrew/bin", mac_path)

        lin_path = self.m.platform_path("linux", "/home/alice", "/home/alice/.hermes")
        self.assertNotIn("/opt/homebrew", lin_path)
        self.assertIn("/usr/local/bin", lin_path)
        self.assertIn("/home/alice/.local/bin", lin_path)

        # 重複は落とす（`~/.hermes/bin` と `<hermes-home>/bin` は同じことが多い）
        entries = self.m.platform_path("macos", "/Users/alice", "/Users/alice/.hermes").split(":")
        self.assertEqual(len(entries), len(set(entries)), entries)

    def test_generated_units_carry_their_own_path(self):
        # POSIX パスで組む（実際に Linux / macOS へ置く形。Windows ホストではクロス生成になる）
        def posix_cfg(platform_name: str):
            # ホストに依らず同じ PATH にする（Linux / macOS のランナーでも同じ判定）
            return self.m.build_config(
                "/home/alice/.hermes", "/home/alice/hermes-freeagent-bind", "127.0.0.1", 8790,
                python="/usr/bin/python3", startup_dir="/home/alice/units",
                platform_name=platform_name, assume_host=False,
            )

        linux_cfg = posix_cfg("linux")
        linux_unit = self.m.render_linux_unit(linux_cfg)
        self.assertNotIn("/opt/homebrew", linux_unit, "Linux ユニットに macOS の PATH が入っている")
        self.assertIn("PATH=%s" % linux_cfg["path"], linux_unit)
        self.assertIn("/home/alice/.local/bin", linux_cfg["path"])

        mac_cfg = self.m.build_config(
            "/Users/alice/.hermes", "/Users/alice/hermes-freeagent-bind", "127.0.0.1", 8790,
            python="/opt/homebrew/bin/python3", startup_dir="/Users/alice/Library/LaunchAgents",
            platform_name="macos", assume_host=False,
        )
        plist = plistlib.loads(self.m.render_macos_plist(mac_cfg).encode("utf-8"))
        self.assertEqual(plist["EnvironmentVariables"]["PATH"], mac_cfg["path"])
        self.assertIn(".local/bin", plist["EnvironmentVariables"]["PATH"])
        proxy_plist = plistlib.loads(self.m.render_macos_proxy_plist(mac_cfg).encode("utf-8"))
        self.assertEqual(proxy_plist["EnvironmentVariables"]["PATH"], mac_cfg["path"])

    # --- 5. 停止の即応性と hermes CLI の探索 -----------------------------

    def _proxy_namespace(self):
        cfg = self._cfg()
        src = self.m.render_posix_proxy_launcher(cfg)
        compile(src, "run_proxy.py", "exec")
        self.assertNotIn("time.sleep(_poll_interval())", src, "PEP 475 で停止が待機時間ぶん遅れる")
        self.assertIn("_wait_or_stop(_poll_interval())", src)
        tree = ast.parse(src)
        nodes = [n for n in tree.body if not isinstance(n, ast.If)]
        ns: dict[str, Any] = {"__builtins__": __builtins__}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<run_proxy>", "exec"), ns)
        return ns

    def test_wait_or_stop_returns_immediately_after_sigterm(self):
        ns = self._proxy_namespace()
        ns["_handle_stop"](None, None)
        started = time.monotonic()
        ns["_wait_or_stop"](60.0)
        self.assertLess(time.monotonic() - started, 1.0, "SIGTERM 後も待機が続いている")

        ns2 = self._proxy_namespace()
        started = time.monotonic()
        ns2["_wait_or_stop"](0.05)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_resolve_command_finds_user_local_hermes(self):
        ns = self._proxy_namespace()
        home = os.path.join(self.tmp, "user-home")
        bin_dir = os.path.join(home, ".local", "bin")
        os.makedirs(bin_dir, exist_ok=True)
        hermes = os.path.join(bin_dir, "hermes")
        with open(hermes, "w", encoding="ascii", newline="\n") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(hermes, 0o755)  # POSIX は X_OK が実行ビットを見る（Windows では実質存在チェック）
        ns["REPO"] = os.path.join(self.tmp, "no-hermes-agent")
        ns["HOME"] = os.path.join(self.tmp, "no-bin")
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}, clear=False):
            resolved = ns["_resolve_command"](["--port", "8699"])
        self.assertEqual(os.path.normpath(resolved[0]), os.path.normpath(hermes),
                         "~/.local/bin/hermes を見つけていない")
        self.assertEqual(resolved[1:], ["proxy", "start", "--port", "8699"])


if __name__ == "__main__":
    unittest.main()
