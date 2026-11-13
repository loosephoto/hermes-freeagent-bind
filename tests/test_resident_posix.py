"""Linux (`systemd --user`) / macOS (`launchd` LaunchAgents) 対応を含む
常駐配線生成器（`scripts/install_resident.py`）のオフライン契約テスト。
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


class ResidentPosixCase(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.mkdtemp(prefix="fa-resident-posix-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "hermes")
        self.unit_dir = os.path.join(self.tmp, "units")
        self.repo = os.path.join(self.tmp, "hermes-freeagent-bind")
        os.makedirs(os.path.join(self.repo, "src", "freeagent_bind"), exist_ok=True)
        with open(os.path.join(self.repo, "src", "freeagent_bind", "server.py"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# stub\n")

        py_dir = os.path.join(self.home, "tools", "python-3.14.9+build-linux-x64", "bin")
        os.makedirs(py_dir, exist_ok=True)
        self.posix_py = os.path.join(py_dir, "python3")
        with open(self.posix_py, "w", encoding="ascii", newline="\n") as fh:
            fh.write("#!/bin/sh\n")

    # --- 1. 機種・OS 判別と既定パス ------------------------------------

    def test_detect_platform_handles_auto_aliases_and_rejects_invalid(self):
        self.assertEqual(self.m.detect_platform("auto", sys_platform="win32"), "windows")
        self.assertEqual(self.m.detect_platform("auto", sys_platform="darwin"), "macos")
        self.assertEqual(self.m.detect_platform("auto", sys_platform="linux"), "linux")
        self.assertEqual(self.m.detect_platform("auto", sys_platform="linux-gnu"), "linux")
        self.assertEqual(self.m.detect_platform("darwin"), "macos")
        self.assertEqual(self.m.detect_platform("mac"), "macos")
        self.assertEqual(self.m.detect_platform("win32"), "windows")
        with self.assertRaises(ValueError):
            self.m.detect_platform("freebsd")
        with self.assertRaises(ValueError):
            self.m.detect_platform("auto", sys_platform="sunos5")
        self.assertEqual(self.m.main(["--platform", "invalid-os"]), 2)

    def test_default_homes_and_unit_dirs_per_platform(self):
        env_linux = {"HOME": "/home/alice"}
        self.assertEqual(self.m.hermes_home_default("linux", env=env_linux), "/home/alice/.hermes")
        self.assertEqual(self.m.default_unit_dir("linux", env=env_linux),
                         "/home/alice/.config/systemd/user")
        env_xdg = {"HOME": "/home/alice", "XDG_CONFIG_HOME": "/home/alice/.xdg"}
        self.assertEqual(self.m.default_unit_dir("linux", env=env_xdg),
                         "/home/alice/.xdg/systemd/user")

        env_mac = {"HOME": "/Users/alice"}
        self.assertEqual(self.m.hermes_home_default("macos", env=env_mac), "/Users/alice/.hermes")
        self.assertEqual(self.m.default_unit_dir("macos", env=env_mac),
                         "/Users/alice/Library/LaunchAgents")

    # --- 2. POSIX Python 探索 ------------------------------------------

    def test_discover_python_posix_prefers_newest_and_never_leaks_cross_host_exe(self):
        old_dir = os.path.join(self.home, "tools", "python-3.14.7+old-linux-x64", "bin")
        os.makedirs(old_dir, exist_ok=True)
        with open(os.path.join(old_dir, "python3"), "w", encoding="ascii") as fh:
            fh.write("#!/bin/sh\n")

        with mock.patch.object(self.m, "_host_matches_target", return_value=False):
            found, found_w = self.m.discover_python(self.home, platform_name="linux")
            self.assertEqual(found, self.posix_py)
            self.assertEqual(found_w, "")
            # tools が無い home でクロス生成したとき、ホストの python.exe を混ぜないこと
            empty_home = os.path.join(self.tmp, "empty-home")
            self.assertEqual(self.m.discover_python(empty_home, platform_name="linux"), ("", ""))

        # ホスト一致時は tools が無くても実行中の python へフォールバックできること
        with mock.patch.object(self.m, "_host_matches_target", return_value=True), \
             mock.patch.object(self.m, "_interpreter_ok", return_value=True):
            empty_home = os.path.join(self.tmp, "host-match-home")
            found_host, _ = self.m.discover_python(empty_home, platform_name="linux")
            self.assertTrue(found_host)

    # --- 3. Linux systemd --user ユニット生成 --------------------------

    def test_linux_systemd_units_and_quoting(self):
        spaced_home = "/home/alice smith/.hermes"
        spaced_repo = "/home/alice smith/hermes-freeagent-bind"
        spaced_units = "/home/alice smith/.config/systemd/user"
        cfg = self.m.build_config(
            spaced_home, spaced_repo, "127.0.0.1", 8790,
            python="/opt/python 3.14/bin/python3",
            startup_dir=spaced_units, proxy_port=8645,
            platform_name="linux",
        )
        unit = self.m.render_linux_unit(cfg)
        self.assertIn("[Unit]", unit)
        self.assertIn("[Service]", unit)
        self.assertIn("Type=simple", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartSec=5", unit)
        self.assertIn("WantedBy=default.target", unit)
        # Issue #10: systemd の WorkingDirectory= は引用符を解釈せず bad-setting になるため、
        # 引用符を付けず C 形式（空白 -> \x20）で逃がす。
        self.assertIn(r"WorkingDirectory=/home/alice\x20smith/hermes-freeagent-bind" + "\n", unit)
        self.assertNotIn('WorkingDirectory="', unit)
        self.assertNotIn("WorkingDirectory='", unit)
        self.assertIn('"/opt/python 3.14/bin/python3"', unit)
        self.assertIn('"/home/alice smith/.hermes/freeagent-service/run_server.py"', unit)
        self.assertIn('"freeagent-resident-8790"', unit)

        proxy_unit = self.m.render_linux_proxy_unit(cfg)
        self.assertIn(r"WorkingDirectory=/home/alice\x20smith/.hermes" + "\n", proxy_unit)
        self.assertNotIn('WorkingDirectory="', proxy_unit)
        self.assertNotIn("WorkingDirectory='", proxy_unit)
        self.assertIn('"/home/alice smith/.hermes/proxy-service/run_proxy.py"', proxy_unit)
        self.assertIn('"hermes-proxy-8645"', proxy_unit)

        # 空白の無い通常パス・% を含むパスでも WorkingDirectory= に引用符が付かないこと
        plain_cfg = self.m.build_config(
            "/home/oresama/.hermes", "/home/oresama/hermes-freeagent-bind",
            "127.0.0.1", 8790, python="/usr/bin/python3",
            startup_dir="/home/oresama/.config/systemd/user", platform_name="linux",
        )
        self.assertIn("WorkingDirectory=/home/oresama/hermes-freeagent-bind\n",
                      self.m.render_linux_unit(plain_cfg))
        self.assertIn("WorkingDirectory=/home/oresama/.hermes\n",
                      self.m.render_linux_proxy_unit(plain_cfg))
        self.assertEqual(self.m._systemd_path("/var/lib/job%2F1/a b"),
                         r"/var/lib/job%%2F1/a\x20b")
        self.assertEqual(self.m._systemd_quote("/opt/job%2F1/py"),
                         '"/opt/job%%2F1/py"')

        apply_sh = self.m.render_posix_apply(cfg)
        revert_sh = self.m.render_posix_revert(cfg)
        self.assertIn("systemctl --user start hermes-freeagent-bind.service", apply_sh)
        self.assertIn("systemctl --user disable --now hermes-freeagent-bind.service", revert_sh)
        self.assertIn("headers.x-freeagent-harness hermes", apply_sh)

    def test_linux_build_config_normalizes_relative_paths_on_matching_host(self):
        with mock.patch.object(self.m, "_host_matches_target", return_value=False):
            cross = self.m.build_config(
                "/home/alice/.hermes", "/home/alice/repo", "127.0.0.1", 8790,
                python="/usr/bin/python3", startup_dir="/home/alice/units", platform_name="linux",
            )
        self.assertEqual(cross["repo"], "/home/alice/repo")
        with mock.patch.object(self.m, "_host_matches_target", return_value=True), \
             mock.patch.object(self.m, "_interpreter_ok", return_value=True):
            matched = self.m.build_config(
                ".", ".", "127.0.0.1", 8790,
                python=self.posix_py, startup_dir=".", platform_name="linux",
            )
        self.assertTrue(os.path.isabs(matched["repo"]), matched["repo"])
        self.assertTrue(os.path.isabs(matched["hermes_home"]), matched["hermes_home"])

    @unittest.skipUnless(
        sys.platform.startswith("linux") and bool(shutil.which("systemd-analyze")),
        "Linux + systemd-analyze が要る",
    )
    def test_linux_systemd_analyze_verify_accepts_generated_units(self):
        """実物の `systemd-analyze --user verify` で Issue #10 の再現と修正の両方向を検証する。"""
        rt_dir = os.path.join(self.tmp, "xdg-run")
        os.makedirs(rt_dir, mode=0o700, exist_ok=True)
        os.chmod(rt_dir, 0o700)
        env = dict(os.environ, SYSTEMD_COLORS="0", XDG_RUNTIME_DIR=rt_dir)

        # 陽性対照: 二重引用符つき WorkingDirectory= は systemd-analyze が拒否すること
        bad_unit = os.path.join(self.tmp, "bad-quoted.service")
        with open(bad_unit, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "[Service]\nType=simple\n"
                'WorkingDirectory="/tmp"\n'
                f'ExecStart="{sys.executable}" "-c" "pass"\n'
            )
        bad_proc = subprocess.run(
            ["systemd-analyze", "--user", "verify", bad_unit],
            capture_output=True, text=True, timeout=30, env=env,
        )
        self.assertNotEqual(bad_proc.returncode, 0, "引用符つき WorkingDirectory が通ってしまった")

        # 本検査: 空白と % を含む実在パスで生成した 2 ユニットが verify を exit 0 で通ること
        base = os.path.join(self.tmp, "alice smith", "job%2F1")
        sp_home = os.path.join(base, ".hermes")
        sp_repo = os.path.join(base, "hermes-freeagent-bind")
        sp_units = os.path.join(base, "units")
        os.makedirs(os.path.join(sp_repo, "src", "freeagent_bind"), exist_ok=True)
        with open(os.path.join(sp_repo, "src", "freeagent_bind", "server.py"),
                  "w", encoding="utf-8", newline="\n") as fh:
            fh.write("# stub\n")

        rc = self.m.main([
            "--platform", "linux",
            "--hermes-home", sp_home,
            "--repo", sp_repo,
            "--unit-dir", sp_units,
            "--python", sys.executable,
            "--write", "--force",
        ])
        self.assertEqual(rc, 0)
        unit_main = os.path.join(sp_units, self.m.LINUX_UNIT_NAME)
        unit_proxy = os.path.join(sp_units, self.m.LINUX_PROXY_UNIT_NAME)
        ok_proc = subprocess.run(
            ["systemd-analyze", "--user", "verify", unit_main, unit_proxy],
            capture_output=True, text=True, timeout=30, env=env,
        )
        detail = (ok_proc.stdout + ok_proc.stderr).strip()
        self.assertEqual(ok_proc.returncode, 0, f"systemd-analyze verify failed: {detail}")
        self.assertNotIn("bad-setting", detail)
        self.assertNotIn("not absolute", detail)

    # --- 4. macOS launchd LaunchAgents plist 生成 ----------------------

    def test_macos_launchd_plists_roundtrip_with_plistlib(self):
        odd_home = "/Users/alice & bob/.hermes"
        odd_repo = "/Users/alice & bob/hermes-freeagent-bind"
        odd_units = "/Users/alice & bob/Library/LaunchAgents"
        cfg = self.m.build_config(
            odd_home, odd_repo, "127.0.0.1", 8805,
            python="/opt/homebrew/bin/python3",
            startup_dir=odd_units, proxy_port=8650,
            platform_name="macos",
        )
        raw_plist = self.m.render_macos_plist(cfg)
        parsed = plistlib.loads(raw_plist.encode("utf-8"))
        self.assertEqual(parsed["Label"], "ai.hermes.freeagent-bind")
        self.assertEqual(
            parsed["ProgramArguments"],
            ["/opt/homebrew/bin/python3",
             "/Users/alice & bob/.hermes/freeagent-service/run_server.py",
             "freeagent-resident-8805"],
        )
        self.assertEqual(parsed["WorkingDirectory"], odd_repo)
        self.assertTrue(parsed["RunAtLoad"])
        self.assertEqual(parsed["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(parsed["ThrottleInterval"], 5)
        self.assertIn("/opt/homebrew/bin", parsed["EnvironmentVariables"]["PATH"])
        self.assertTrue(parsed["StandardOutPath"].endswith("/logs/freeagent-autostart.log"))

        raw_proxy = self.m.render_macos_proxy_plist(cfg)
        parsed_proxy = plistlib.loads(raw_proxy.encode("utf-8"))
        self.assertEqual(parsed_proxy["Label"], "ai.hermes.proxy")
        self.assertIn("hermes-proxy-8650", parsed_proxy["ProgramArguments"])
        self.assertTrue(parsed_proxy["StandardOutPath"].endswith("/logs/proxy-service.log"))

        no_proxy_cfg = self.m.build_config(
            odd_home, odd_repo, "127.0.0.1", 8805,
            python="/opt/homebrew/bin/python3",
            startup_dir=odd_units, with_proxy=False, platform_name="macos",
        )
        self.assertEqual(len(self.m.build_plan(no_proxy_cfg)), 4)

    # --- 5. POSIX ランチャ群の構文・.env 読込・未認証待機→自動復帰 -----

    def test_posix_server_launcher_compiles_and_loads_env(self):
        cfg = self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.posix_py, startup_dir=self.unit_dir, platform_name="linux",
        )
        src = self.m.render_posix_launcher(cfg)
        compile(src, "run_server.py", "exec")
        self.assertTrue(src.startswith("#!/usr/bin/env python3\n"))
        self.assertIn("runpy.run_path", src)

        tree = ast.parse(src)
        node = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_load_env_file")
        ns: dict[str, Any] = {"os": os}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<posix_launcher>", "exec"), ns)
        loader = ns["_load_env_file"]

        env_file = os.path.join(self.tmp, "posix.env")
        with open(env_file, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("export FA_TEST_KEY1='val1'\nFA_TEST_KEY2=1  # inline\nFA_TEST_KEEP=from_file\n")
        os.environ["FA_TEST_KEEP"] = "existing"
        for k in ("FA_TEST_KEY1", "FA_TEST_KEY2", "FA_TEST_KEEP"):
            self.addCleanup(os.environ.pop, k, None)

        loaded = loader(env_file)
        self.assertEqual(loaded, ["FA_TEST_KEY1", "FA_TEST_KEY2"])
        self.assertEqual(os.environ["FA_TEST_KEY1"], "val1")
        self.assertEqual(os.environ["FA_TEST_KEY2"], "1")
        self.assertEqual(os.environ["FA_TEST_KEEP"], "existing")

    def test_posix_proxy_launcher_waits_when_unauthenticated_and_execs_once_ready(self):
        cfg = self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.posix_py, startup_dir=self.unit_dir,
            proxy_port=8699, platform_name="macos",
        )
        src = self.m.render_posix_proxy_launcher(cfg)
        compile(src, "run_proxy.py", "exec")
        tree = ast.parse(src)
        nodes = [n for n in tree.body if not isinstance(n, ast.If)]
        ns: dict[str, Any] = {"__builtins__": __builtins__}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<run_proxy.py>", "exec"), ns)

        auth_path = os.path.join(self.home, "auth.json")
        log_path = os.path.join(self.home, "logs", "proxy-service.log")
        hermes_repo = os.path.join(self.home, "hermes-agent")
        os.makedirs(hermes_repo, exist_ok=True)
        ns["AUTH_FILE"] = auth_path
        ns["LOG"] = log_path
        ns["REPO"] = hermes_repo

        exec_calls = []
        chdir_calls = []

        def fake_execvpe(file, args, env):
            exec_calls.append((file, list(args), dict(env)))

        # 1) 未認証のまま max_polls=1 -> skipped ログは 1 行だけ書かれ、execvpe は呼ばれない
        with mock.patch.object(ns["time"], "sleep", return_value=None), \
             mock.patch.object(ns["os"], "execvpe", side_effect=fake_execvpe), \
             mock.patch.object(ns["os"], "dup2", return_value=None), \
             mock.patch.object(ns["os"], "chdir", side_effect=chdir_calls.append), \
             mock.patch.dict(os.environ, {"HERMES_PROXY_FORCE_START": ""}, clear=False):
            rc = ns["main"](["hermes-proxy-8699"], max_polls=1)
            self.assertEqual(rc, 0)
            self.assertEqual(len(exec_calls), 0)
            with open(log_path, encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            self.assertEqual(len(lines), 1, lines)
            self.assertIn("skipped: Nous Portal not authenticated", lines[0])

        # 2) 待機中に auth.json が書かれたら、次のチェックで自動復帰して execvpe する
        def write_auth_on_sleep(_secs):
            with open(auth_path, "w", encoding="utf-8") as fh:
                json.dump({"providers": {"nous": {"agent_key": "sk-test"}}}, fh)

        with mock.patch.object(ns["time"], "sleep", side_effect=write_auth_on_sleep), \
             mock.patch.object(ns["os"], "execvpe", side_effect=fake_execvpe), \
             mock.patch.object(ns["os"], "dup2", return_value=None), \
             mock.patch.object(ns["os"], "chdir", side_effect=chdir_calls.append), \
             mock.patch.dict(os.environ, {"HERMES_PROXY_FORCE_START": ""}, clear=False):
            rc = ns["main"](["hermes-proxy-8699"], max_polls=3)
            self.assertEqual(rc, 0)
            self.assertEqual(len(exec_calls), 1)
            self.assertEqual(chdir_calls, [self.home])
            exe_file, exe_args, exe_env = exec_calls[0]
            self.assertEqual(exe_file, ns["sys"].executable)
            self.assertEqual(exe_args[1:], ["-m", "hermes_cli.main", "proxy", "start", "--port", "8699"])
            self.assertIn(hermes_repo, exe_env["PYTHONPATH"])

    # --- 6. 書き込み作法・実機非汚染 ------------------------------------

    def test_write_uses_lf_utf8_and_never_touches_real_user_dirs(self):
        real_systemd = self.m.default_unit_dir("linux")
        real_launchd = self.m.default_unit_dir("macos")
        real_targets = [
            os.path.join(real_systemd, self.m.LINUX_UNIT_NAME),
            os.path.join(real_systemd, self.m.LINUX_PROXY_UNIT_NAME),
            os.path.join(real_launchd, self.m.MACOS_PLIST_NAME),
            os.path.join(real_launchd, self.m.MACOS_PROXY_PLIST_NAME),
        ]

        def snapshot(paths):
            out = []
            for p in paths:
                if os.path.exists(p):
                    with open(p, "rb") as fh:
                        out.append(fh.read())
                else:
                    out.append(None)
            return out

        before = snapshot(real_targets)
        for plat in ("linux", "macos"):
            sub_unit = os.path.join(self.unit_dir, plat)
            # 既定実行と --dry-run は書き込まない
            self.assertEqual(
                self.m.main(["--platform", plat, "--hermes-home", self.home,
                             "--repo", self.repo, "--unit-dir", sub_unit, "--dry-run"]),
                0,
            )
            self.assertFalse(os.path.exists(sub_unit))

            # --write で隔離ディレクトリにだけ UTF-8 + LF で書かれる
            self.assertEqual(
                self.m.main(["--platform", plat, "--hermes-home", self.home,
                             "--repo", self.repo, "--unit-dir", sub_unit,
                             "--python", self.posix_py, "--write", "--force"]),
                0,
            )
            cfg = self.m.build_config(
                self.home, self.repo, "127.0.0.1", 8790,
                python=self.posix_py, startup_dir=sub_unit, platform_name=plat,
            )
            for path, _ in self.m.build_plan(cfg):
                self.assertTrue(os.path.isfile(path), path)
                with open(path, "rb") as fh:
                    raw = fh.read()
                self.assertNotIn(b"\r\n", raw, "%s に CRLF が混ざっている" % path)
                raw.decode("utf-8")

        after = snapshot(real_targets)
        self.assertEqual(before, after, "実機の systemd / LaunchAgents ディレクトリに触れた")

    # --- 7. systemctl / launchctl サービス登録制御 ----------------------

    def test_register_linux_units_checks_systemd_and_linger(self):
        cfg = self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.posix_py, startup_dir=self.unit_dir, platform_name="linux",
        )
        # systemd ディレクトリ無し -> 失敗理由を返す
        res_missing = self.m.register_linux_units(cfg, systemd_dir=os.path.join(self.tmp, "no-systemd"))
        self.assertTrue(res_missing[0].startswith("service failed:"))

        fake_systemd = os.path.join(self.tmp, "systemd-system")
        fake_linger = os.path.join(self.tmp, "linger")
        os.makedirs(fake_systemd, exist_ok=True)
        os.makedirs(fake_linger, exist_ok=True)

        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="OK\n", stderr="")

        with mock.patch.object(self.m.shutil, "which", return_value="/usr/bin/systemctl"), \
             mock.patch.object(self.m.subprocess, "run", side_effect=fake_run), \
             mock.patch.dict(os.environ, {"USER": "alice"}, clear=False):
            out = self.m.register_linux_units(cfg, systemd_dir=fake_systemd, linger_dir=fake_linger)
            self.assertIn("service enabled: hermes-freeagent-bind.service (systemd --user)", out)
            self.assertIn("service enabled: hermes-proxy.service (systemd --user)", out)
            self.assertTrue(any("enable-linger" in line for line in out), out)

            # linger 有効化済みなら note が出ないこと
            with open(os.path.join(fake_linger, "alice"), "w", encoding="ascii") as fh:
                fh.write("")
            out_lingered = self.m.register_linux_units(cfg, systemd_dir=fake_systemd, linger_dir=fake_linger)
            self.assertFalse(any("enable-linger" in line for line in out_lingered), out_lingered)

        self.assertIn(["systemctl", "--user", "daemon-reload"], calls)
        self.assertIn(["systemctl", "--user", "enable", "--now", "hermes-freeagent-bind.service"], calls)

    def test_register_macos_agents_runs_bootout_bootstrap_kickstart(self):
        cfg = self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.posix_py, startup_dir=self.unit_dir, platform_name="macos",
        )
        calls = []

        def fake_run(cmd, **_kwargs):
            calls.append(list(cmd))
            # 初回 bootout が exit 3（未ロード）でも続行できること
            rc = 3 if cmd[1] == "bootout" else 0
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

        with mock.patch.object(self.m.shutil, "which", return_value="/bin/launchctl"), \
             mock.patch.object(self.m, "_posix_uid", return_value=501), \
             mock.patch.object(self.m.subprocess, "run", side_effect=fake_run):
            out = self.m.register_macos_agents(cfg)

        self.assertEqual(
            out,
            [
                "service bootstrapped: ai.hermes.freeagent-bind (gui/501)",
                "service bootstrapped: ai.hermes.proxy (gui/501)",
            ],
        )
        self.assertEqual(calls[0], ["launchctl", "bootout", "gui/501/ai.hermes.freeagent-bind"])
        self.assertEqual(calls[1], ["launchctl", "bootstrap", "gui/501", cfg["paths"]["unit"]])
        self.assertEqual(calls[2], ["launchctl", "kickstart", "-k", "gui/501/ai.hermes.freeagent-bind"])

    def test_register_services_skips_when_cross_generating(self):
        cfg = self.m.build_config(
            self.home, self.repo, "127.0.0.1", 8790,
            python=self.posix_py, startup_dir=self.unit_dir, platform_name="macos",
        )
        with mock.patch.object(self.m, "_host_matches_target", return_value=False):
            lines = self.m.register_services(cfg)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("service skipped:"), lines)


if __name__ == "__main__":
    unittest.main()
