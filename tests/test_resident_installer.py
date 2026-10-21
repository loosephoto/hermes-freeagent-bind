"""Windows 常駐配線の生成器（`scripts/install_resident_windows.py`）の契約（ネットワーク不要）。

守るべき性質は 3 つ:

1. **生成物がコンソールを生まない**。`WshShell.Exec("cmd /c …")` / `cmd /c …` を実行行に書かない
   （Windows 11 は既定のターミナルが Windows Terminal なので、5 分ごとの見張りで黒い窓が一瞬出る。
   実測で毎ティック 1 個）。生存確認は WMI、起動は `pythonw.exe`、ログはファイル追記。
2. **WMI の照合が「いまのサーバー」に当たる**。`pythonw.exe run_server.pyw <marker>` の目印と、
   旧来の `server.py … --port <port>` の両方に当たること（片方だけだと二重起動する）。
3. **書き込みの作法**。`.vbs` は ASCII + CRLF（WSH は BOM 無しを ANSI として読む）、
   既存ファイルは `force` が無ければ温存、既定（`--write` なし）は 1 バイトも書かない。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_module():
    path = os.path.join(ROOT, "scripts", "install_resident_windows.py")
    spec = importlib.util.spec_from_file_location("install_resident_windows", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _code_lines(text: str) -> str:
    """VBS/シェルのコメント行を落とす（説明文に "cmd /c" と書くのは正しい）。"""
    keep = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("'") or stripped.startswith("#"):
            continue
        keep.append(line)
    return "\n".join(keep)


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.m = _load_module()
        self.tmp = tempfile.mkdtemp(prefix="resident-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.home = os.path.join(self.tmp, "hermes")
        py_dir = os.path.join(self.home, "tools", "python-9.9.9+build-win32-x64")
        os.makedirs(py_dir)
        self.python = os.path.join(py_dir, "python.exe")
        self.pythonw = os.path.join(py_dir, "pythonw.exe")
        for path in (self.python, self.pythonw):
            with open(path, "w", encoding="ascii") as fh:
                fh.write("stub")
        self.repo = os.path.join(self.tmp, "hermes-freeagent-bind")
        os.makedirs(os.path.join(self.repo, "src", "freeagent_bind"))
        with open(os.path.join(self.repo, "src", "freeagent_bind", "server.py"),
                  "w", encoding="utf-8") as fh:
            fh.write("# stub\n")
        self.cfg = self.m.build_config(self.home, self.repo, "127.0.0.1", 8790)

    # --- 生成物の契約 ---------------------------------------------------

    def test_discover_python_enumerates_tools_folder(self):
        """フォルダ名はインストールごとに変わるので、名前を書かずに列挙して見つけること。"""
        found, found_w = self.m.discover_python(self.home)
        self.assertEqual(found, self.python)
        self.assertEqual(found_w, self.pythonw)
        self.assertEqual(self.m.discover_python(os.path.join(self.tmp, "missing")), ("", ""))

    def test_worker_never_spawns_a_console(self):
        vbs = self.m.render_worker(self.cfg)
        code = _code_lines(vbs)
        for forbidden in ("netstat", ".Exec(", "cmd /c", "cmd.exe", "wscript.exe "):
            self.assertNotIn(forbidden, code, "実行行に %r があると黒い窓が出る" % forbidden)
        # 生存確認は WMI、起動は pythonw、ログはファイル追記。
        self.assertIn("Win32_Process", code)
        self.assertIn("pythonw.exe", code)
        self.assertIn("OpenTextFile", code)
        self.assertIn("WinHttp.WinHttpRequest.5.1", code, "WMI が使えないときの控えが要る")

    def test_worker_recognises_current_and_legacy_command_lines(self):
        vbs = self.m.render_worker(self.cfg)
        self.assertIn(self.m.marker_for(8790), vbs, "pythonw + run_server.pyw のプロセスに当たること")
        self.assertIn('InStr(cl, "--port 8790")', vbs, "旧来の server.py 起動にも当たること")
        other = self.m.build_config(self.home, self.repo, "127.0.0.1", 8801)
        self.assertIn(self.m.marker_for(8801), self.m.render_worker(other))
        self.assertNotIn(self.m.marker_for(8790), self.m.render_worker(other))

    def test_worker_honours_host_and_port(self):
        cfg = self.m.build_config(self.home, self.repo, "127.0.0.1", 8802)
        vbs = self.m.render_worker(cfg)
        self.assertIn("http://127.0.0.1:8802/", vbs)
        self.assertIn("--port 8802", vbs)

    def test_vbs_artifacts_are_ascii_only(self):
        """WSH は BOM 無しの .vbs を ANSI として読むので、非 ASCII を混ぜない。"""
        for text in (self.m.render_worker(self.cfg), self.m.render_startup(self.cfg)):
            for ch in text:
                self.assertLess(ord(ch), 128, "非 ASCII 文字 %r が混ざっている" % ch)

    def test_launcher_is_console_free_and_runs_the_server(self):
        pyw = self.m.render_launcher(self.cfg)
        body = pyw.split('"""', 2)[2]  # docstring は説明（本文の実行行だけを見る）
        self.assertNotIn("cmd /c", body)
        self.assertNotIn("subprocess", body)
        self.assertIn("runpy.run_path", pyw)
        self.assertIn('"src", "freeagent_bind", "server.py"', pyw, "repo 相対で本体を指すこと")
        self.assertIn('"--transport", "http"', pyw)
        self.assertIn("os.chdir(REPO)", pyw)
        self.assertIn("sys.stderr = _stream", pyw, "ログへ stderr を流す（cmd のリダイレクト代替）")
        compile(pyw, "run_server.pyw", "exec")  # 構文が通ること

    def test_startup_launcher_delegates_to_the_worker(self):
        vbs = self.m.render_startup(self.cfg)
        self.assertIn(self.cfg["paths"]["worker"], vbs)
        code = _code_lines(vbs)
        for forbidden in ("cmd /c", ".Exec(", "netstat"):
            self.assertNotIn(forbidden, code)

    def test_config_switches_name_the_same_endpoint(self):
        apply_sh = self.m.render_apply(self.cfg)
        revert_sh = self.m.render_revert(self.cfg)
        self.assertIn("mcp_servers.freeagent-bind.url http://127.0.0.1:8790/mcp", apply_sh)
        self.assertIn("headers.x-freeagent-harness hermes", apply_sh)
        self.assertIn(self.python, revert_sh.replace("/", os.sep), "戻しは実在の python を指すこと")
        self.assertIn("FREEAGENT_HARNESS hermes", revert_sh)
        # 切替は片方を消してから入れる（command と url の同時指定は不可）。
        for key in ("command", "args", "env"):
            self.assertLess(apply_sh.index("unset mcp_servers.freeagent-bind.%s" % key),
                            apply_sh.index("set mcp_servers.freeagent-bind.url"))

    # --- 書き込みの作法 -------------------------------------------------

    def test_write_plan_uses_ascii_crlf_for_vbs(self):
        plan = self.m.build_plan(self.cfg)
        for line in self.m.write_plan(plan, force=True):
            self.assertTrue(line.startswith("wrote:"), line)
        for path in (self.cfg["paths"]["worker"], self.cfg["paths"]["startup"]):
            with open(path, "rb") as fh:
                raw = fh.read()
            self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), ".vbs は全行 CRLF であること")

            raw.decode("ascii")  # 非 ASCII が混ざれば UnicodeDecodeError
        with open(self.cfg["paths"]["launcher"], "rb") as fh:
            launcher = fh.read()
        self.assertNotIn(b"\r\n", launcher, "pythonw が読む .pyw は LF でよい")

    def test_existing_files_are_kept_without_force(self):
        plan = self.m.build_plan(self.cfg)
        for path, _ in plan:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("keep me\n")
        lines = self.m.write_plan(plan, force=False)
        self.assertTrue(all(line.startswith("skipped") for line in lines), lines)
        with open(self.cfg["paths"]["worker"], encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep me\n")
        lines = self.m.write_plan(plan, force=True)
        self.assertTrue(all(line.startswith("wrote:") for line in lines), lines)
        with open(self.cfg["paths"]["worker"], encoding="utf-8") as fh:
            self.assertIn("Win32_Process", fh.read())

    def test_plan_targets_the_expected_locations(self):
        paths = self.cfg["paths"]
        self.assertEqual(paths["service_dir"], os.path.join(self.home, "freeagent-service"))
        self.assertTrue(paths["worker"].endswith(os.path.join("freeagent-service",
                                                             "Hermes_Freeagent.vbs")))
        self.assertTrue(paths["startup"].endswith(os.path.join("Programs", "Startup",
                                                               "Hermes_Freeagent.vbs")))
        self.assertEqual([p for p, _ in self.m.build_plan(self.cfg)],
                         [paths["launcher"], paths["worker"], paths["startup"],
                          paths["apply"], paths["revert"]])

    def test_default_invocation_writes_nothing(self):
        """引数なしの実行は表示だけ（実機の配線を勝手に触らない）。"""
        rc = self.m.main(["--hermes-home", self.home, "--repo", self.repo])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.cfg["paths"]["worker"]))
        rc = self.m.main(["--hermes-home", self.home, "--repo", self.repo, "--dry-run"])
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.cfg["paths"]["worker"]))

    def test_write_and_task_registration_are_opt_in(self):
        rc = self.m.main(["--hermes-home", self.home, "--repo", self.repo, "--write"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.cfg["paths"]["worker"]))
        self.assertTrue(os.path.exists(self.cfg["paths"]["startup"]))
        # --task を付けない限り schtasks は呼ばれない。
        self.assertEqual(self.m.register_task.__module__, "install_resident_windows")


if __name__ == "__main__":
    unittest.main()
