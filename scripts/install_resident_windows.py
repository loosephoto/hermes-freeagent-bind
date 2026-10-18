"""Windows で freeagent-bind を常駐させる「配線」を生成する（ネットワーク不要）。

常駐 HTTP（`--transport http`）は 1 プロセスを複数のクライアントで共有するための経路だが、
**そのプロセスを機械に定着させる部分はこれまで手作業だった**（`%LOCALAPPDATA%\\hermes\\` 配下の
VBS とタスクスケジューラの登録）。このスクリプトはその配線を repo から再現可能にする。

生成物（`--hermes-home` の既定は `%LOCALAPPDATA%\\hermes`）:

| 出力 | 役割 |
|---|---|
| `<hermes-home>\\freeagent-service\\run_server.pyw` | コンソールを作らない起動役（`pythonw.exe` から走る） |
| `<hermes-home>\\freeagent-service\\Hermes_Freeagent.vbs` | worker。既に待受していれば何もせず終了（二重起動防止） |
| `<Startup>\\Hermes_Freeagent.vbs` | ログオン時に worker を隠して起動する launcher |
| `<hermes-home>\\freeagent-service\\apply_freeagent_http.sh` | `hermes config` を HTTP へ切り替える |
| `<hermes-home>\\freeagent-service\\revert_freeagent_stdio.sh` | stdio へ戻す |

**コンソールを生む書き方をしない**（この repo の常駐配線で唯一の実害があった点）:
`WshShell.Exec("cmd /c netstat …")` や `sh.Run "cmd /c …"` は子プロセスにコンソールを割り当てる。
Windows 11 は既定のターミナルが Windows Terminal なので、**数分おきに黒いウィンドウが一瞬出る**
（実測: 5 分周期の見張りタスクで毎回 1 個。`SW_HIDE` を指定しても Windows Terminal は表示する）。
このスクリプトが生成する worker は

1. 生存確認を **WMI（`Win32_Process` の CommandLine 照合）** で行い（子プロセスを作らない）、
   WMI が使えないときだけ **WinHttp** の接続確認へ落ちる（どちらもコンソールを作らない）、
2. 起動ログを **VBScript のファイル追記**で書く（`cmd /c echo` を使わない）、
3. サーバー本体を **`pythonw.exe`**（GUI サブシステム＝コンソール無し）で起動する。

使い方:

```bash
python scripts/install_resident_windows.py                 # 生成内容を表示（書き込みなし）
python scripts/install_resident_windows.py --dry-run       # 書き込むパスだけ表示
python scripts/install_resident_windows.py --write         # 生成（既存ファイルは温存）
python scripts/install_resident_windows.py --write --force # 上書き
python scripts/install_resident_windows.py --write --force --task   # 見張りタスクも登録
```

`--task` は `schtasks /create /sc minute /mo <間隔> /tr "wscript.exe <worker>" /f` を実行する。
間隔を空けたくない場合（ログオン時だけ起動すれば十分）は付けない。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_TASK_NAME = "Hermes_Freeagent_Watchdog"
DEFAULT_INTERVAL_MINUTES = 5
SERVICE_DIR_NAME = "freeagent-service"
LOG_NAME = "freeagent-autostart.log"
LAUNCHER_NAME = "run_server.pyw"
WORKER_NAME = "Hermes_Freeagent.vbs"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def hermes_home_default() -> str:
    """`%LOCALAPPDATA%\\hermes`（Hermes の HERMES_HOME の既定）。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "hermes")


def marker_for(port: int) -> str:
    """サーバーのコマンドラインに載る目印。WMI の照合はこれで行う。

    `pythonw.exe run_server.pyw <marker>` の形で渡す。launcher は `sys.argv` を自分で
    組み直すので、この余分な引数は**読み捨てられる**（照合のためだけに存在する）。
    """
    return "freeagent-resident-%d" % port


def discover_python(hermes_home: str) -> tuple[str, str]:
    """`<hermes-home>\\tools\\python-*` から `(python.exe, pythonw.exe)` を探す。

    フォルダ名はインストールごとに変わる（版＋ビルド番号。例 `python-3.14.7+<build>-win32-x64`）ので、
    **名前を書かずに `tools` を列挙して `python-` で始まる最初のものを使う**。
    見つからなければ空文字を返す（生成側で明示指定を促す）。
    """
    tools = os.path.join(hermes_home, "tools")
    if not os.path.isdir(tools):
        return "", ""
    candidates = sorted(
        name for name in os.listdir(tools)
        if name.startswith("python-") and os.path.isdir(os.path.join(tools, name))
    )
    for name in candidates:
        exe = os.path.join(tools, name, "python.exe")
        exe_w = os.path.join(tools, name, "pythonw.exe")
        if os.path.isfile(exe):
            return exe, (exe_w if os.path.isfile(exe_w) else "")
    return "", ""


def service_paths(hermes_home: str) -> dict:
    service = os.path.join(hermes_home, SERVICE_DIR_NAME)
    return {
        "service_dir": service,
        "launcher": os.path.join(service, LAUNCHER_NAME),
        "worker": os.path.join(service, WORKER_NAME),
        "log": os.path.join(hermes_home, "logs", LOG_NAME),
        "apply": os.path.join(service, "apply_freeagent_http.sh"),
        "revert": os.path.join(service, "revert_freeagent_stdio.sh"),
        "startup": os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"),
            "Microsoft", "Windows", "Start Menu", "Programs", "Startup", WORKER_NAME,
        ),
    }


def render_launcher(cfg: dict) -> str:
    """コンソールを作らない起動役（`pythonw.exe` から走る）。"""
    return '''# -*- coding: utf-8 -*-
"""freeagent-bind の常駐 HTTP サーバーを**コンソール無しで**起動する。

`Hermes_Freeagent.vbs` が `pythonw.exe` から呼ぶ。素の `python.exe` はコンソールを持ち、
Windows 11 では既定のターミナル（Windows Terminal）が窓を出すため、起動のたびに黒い
ウィンドウが一瞬出る（実測）。`pythonw.exe` は GUI サブシステムなので窓が作られない。

stdout/stderr は worker が使っていたのと同じログへ追記する（`cmd /c … >> log 2>&1` の代替）。
コマンドラインの `__MARKER__` は worker が WMI で自分のサーバーを見分けるための目印で、
このスクリプトは `sys.argv` を組み直すので読み捨てる。
"""
import os
import runpy
import sys

HOME = os.path.expandvars(r"__HERMES_HOME__")
REPO = r"__REPO__"
LOG = os.path.join(HOME, "logs", "__LOG_NAME__")
PORT = __PORT__

os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.makedirs(os.path.dirname(LOG), exist_ok=True)
_stream = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
sys.stdout = _stream
sys.stderr = _stream

sys.argv = ["server.py", "--transport", "http", "--host", "__HOST__", "--port", str(PORT)]
runpy.run_path(os.path.join(REPO, "src", "freeagent_bind", "server.py"), run_name="__main__")
'''.replace("__HERMES_HOME__", cfg["hermes_home"]).replace("__REPO__", cfg["repo"]).replace(
        "__LOG_NAME__", LOG_NAME).replace("__PORT__", str(cfg["port"])).replace(
        "__HOST__", cfg["host"]).replace("__MARKER__", cfg["marker"]).lstrip("\n")


def render_worker(cfg: dict) -> str:
    """worker VBS。生存確認は WMI、起動は pythonw、ログはファイル追記（すべて窓を作らない）。"""
    return '''
' Hermes Agent - freeagent-bind resident MCP server over HTTP (StreamableHTTP)
' Serves the freeagent-bind MCP server on __HOST__:__PORT__ so every Hermes process
' (gateway, interactive sessions, KANBAN workers) shares ONE server process.
' Skips when the server is already running, so a second logon or a watchdog tick
' never duplicates it.
'
' NO CONSOLE PROCESS IS EVER SPAWNED HERE. `WshShell.Exec("cmd /c netstat")` and
' `sh.Run "cmd /c ..."` allocate a console; Windows 11 hands that console to Windows
' Terminal and shows a black window for a moment (measured: one per 5-minute
' watchdog tick, even with a hidden window style). So:
'   - liveness = a WMI query of Win32_Process CommandLine (no child process),
'     with a WinHttp connection probe only if WMI is unavailable;
'   - the start line is appended with Scripting.FileSystemObject (no "cmd /c echo");
'   - the server itself is started with pythonw.exe (GUI subsystem: no console).
' ASCII only: Windows Script Host reads a .vbs without a UTF-16 BOM as ANSI.
Option Explicit
Dim sh, fso, hermesHome, repo, logDir, logFile, running, py, pyw, tools, f
Dim http, lf, ts, svc, procs, p, cl, wmiOk

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

hermesHome = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\\hermes"
repo = "__REPO__"
logDir = hermesHome & "\\logs"
logFile = logDir & "\\__LOG_NAME__"
ts = Now

If Not fso.FileExists(repo & "\\src\\freeagent_bind\\server.py") Then WScript.Quit 0
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

' Liveness: WMI on the server's own command line. Pure COM - no child process and no
' stray connection (an HTTP probe made the server log ConnectionResetError every tick).
running = False
wmiOk = False
On Error Resume Next
Set svc = GetObject("winmgmts:\\\\.\\root\\cimv2")
If Err.Number = 0 And IsObject(svc) Then
  Set procs = svc.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process")
  If Err.Number = 0 And IsObject(procs) Then
    wmiOk = True
    For Each p In procs
      cl = p.CommandLine
      If Not IsNull(cl) Then
        If InStr(cl, "__MARKER__") > 0 Then running = True
        If InStr(cl, "freeagent_bind") > 0 And InStr(cl, "server.py") > 0 And InStr(cl, "--port __PORT__") > 0 Then running = True
      End If
    Next
  End If
  Err.Clear
End If
If running Then WScript.Quit 0

' WMI unavailable -> connection probe with WinHttp (still no console window).
If Not wmiOk Then
  Set http = CreateObject("WinHttp.WinHttpRequest.5.1")
  If Err.Number = 0 And IsObject(http) Then
    http.SetTimeouts 1000, 1000, 1000, 2000
    http.Open "GET", "http://__HOST__:__PORT__/", False
    http.Send
    If Err.Number = 0 Then running = True
    Err.Clear
  End If
  If running Then WScript.Quit 0
End If
On Error GoTo 0

' The bundled python lives in a folder whose name changes per install (version plus
' build number), so enumerate the tools folder instead of hardcoding a path.
py = ""
pyw = ""
tools = hermesHome & "\\tools"
If fso.FolderExists(tools) Then
  For Each f In fso.GetFolder(tools).SubFolders
    If Left(f.Name, 7) = "python-" Then
      If fso.FileExists(f.Path & "\\python.exe") Then py = f.Path & "\\python.exe"
      If fso.FileExists(f.Path & "\\pythonw.exe") Then pyw = f.Path & "\\pythonw.exe"
      If py <> "" Then Exit For
    End If
  Next
End If
If py = "" Then WScript.Quit 0

' Log the start with a plain file append ("cmd /c echo" would spawn a console too).
On Error Resume Next
Set lf = fso.OpenTextFile(logFile, 8, True)
lf.WriteLine "[" & ts & "] starting freeagent-bind http with " & py
lf.Close
On Error GoTo 0

' Launch console-free. Fall back to python.exe (hidden) only if pythonw is missing.
sh.CurrentDirectory = repo
If pyw <> "" Then
  sh.Run """" & pyw & """ """ & hermesHome & "\\__SERVICE_DIR__\\__LAUNCHER_NAME__"" __MARKER__", 0, False
Else
  sh.Run """" & py & """ src\\freeagent_bind\\server.py --transport http --host __HOST__ --port __PORT__", 0, False
End If
'''.replace("__HERMES_HOME__", cfg["hermes_home"]).replace("__REPO__", cfg["repo"]).replace(
        "__LOG_NAME__", LOG_NAME).replace("__SERVICE_DIR__", SERVICE_DIR_NAME).replace(
        "__LAUNCHER_NAME__", LAUNCHER_NAME).replace("__PORT__", str(cfg["port"])).replace(
        "__HOST__", cfg["host"]).replace("__MARKER__", cfg["marker"]).lstrip("\n")


def render_startup(cfg: dict) -> str:
    """ログオン時に worker を隠して起動する launcher（Windows Script Host は GUI サブシステム）。"""
    return '''
' Hermes Agent - freeagent-bind resident HTTP server autostart (logon launcher)
' Delegates to the service script, which itself skips when the server is already
' running. Safe to run at every logon. No console window is created (wscript.exe is a
' GUI-subsystem host; the service script spawns nothing but pythonw.exe).
' ASCII only: Windows Script Host reads .vbs as ANSI unless it has a UTF-16 BOM.
Option Explicit
Dim fso, sh, target
target = "__WORKER__"
Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(target) Then WScript.Quit 0
Set sh = CreateObject("WScript.Shell")
sh.Run "wscript.exe " & Chr(34) & target & Chr(34), 0, False
'''.replace("__WORKER__", cfg["paths"]["worker"]).lstrip("\n")


def render_apply(cfg: dict) -> str:
    """`hermes config` を常駐 HTTP へ切り替える（stdio のキーは消してから url を入れる）。"""
    return '''#!/usr/bin/env bash
# freeagent-bind を常駐 HTTP（__HOST__:__PORT__ を全プロセスで共有）に切り替える。
# 先に常駐サーバーを起動: wscript.exe "$LOCALAPPDATA/hermes/__SERVICE_DIR__/__WORKER_NAME__"
set -e
hermes config unset mcp_servers.freeagent-bind.command
hermes config unset mcp_servers.freeagent-bind.args
hermes config unset mcp_servers.freeagent-bind.env
hermes config set mcp_servers.freeagent-bind.url http://__HOST__:__PORT__/mcp
hermes config set mcp_servers.freeagent-bind.headers.x-freeagent-harness hermes
echo '--- 現在の設定 ---' && hermes config get mcp_servers.freeagent-bind
echo '--- 接続テスト ---' && hermes mcp test freeagent-bind
'''.replace("__HOST__", cfg["host"]).replace("__PORT__", str(cfg["port"])).replace(
        "__SERVICE_DIR__", SERVICE_DIR_NAME).replace("__WORKER_NAME__", WORKER_NAME)


def render_revert(cfg: dict) -> str:
    """stdio へ戻す（`args` は YAML のリストとして入れる）。"""
    py = cfg.get("python") or "<tools>\\python-<ver>\\python.exe"
    return '''#!/usr/bin/env bash
# freeagent-bind を stdio（1 セッション 1 プロセス）に戻す。
# 常駐サーバーは残っても害はないが、止めるなら: MSYS_NO_PATHCONV=1 taskkill /F /PID <__PORT__ の PID>
set -e
hermes config unset mcp_servers.freeagent-bind.url
hermes config unset mcp_servers.freeagent-bind.headers
hermes config set mcp_servers.freeagent-bind.command '__PYTHON__'
hermes config set mcp_servers.freeagent-bind.args '["__SERVER_PY__"]'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_HARNESS hermes
echo '--- 現在の設定 ---' && hermes config get mcp_servers.freeagent-bind
echo '--- 接続テスト ---' && hermes mcp test freeagent-bind
'''.replace("__PYTHON__", py).replace(
        "__SERVER_PY__", os.path.join(cfg["repo"], "src", "freeagent_bind", "server.py").replace("\\", "/")).replace(
        "__PORT__", str(cfg["port"]))


def build_config(hermes_home: str, repo: str, host: str, port: int,
                 python: str = "", pythonw: str = "") -> dict:
    """生成に必要な値一式（見つかった python を含む）。"""
    paths = service_paths(hermes_home)
    found_py, found_pyw = discover_python(hermes_home)
    return {
        "hermes_home": hermes_home,
        "repo": repo,
        "host": host,
        "port": port,
        "python": python or found_py,
        "pythonw": pythonw or found_pyw,
        "marker": marker_for(port),
        "paths": paths,
    }


def build_plan(cfg: dict) -> list[tuple[str, str]]:
    """`(出力パス, 内容)` の一覧。書き込みはしない。"""
    p = cfg["paths"]
    return [
        (p["launcher"], render_launcher(cfg)),
        (p["worker"], render_worker(cfg)),
        (p["startup"], render_startup(cfg)),
        (p["apply"], render_apply(cfg)),
        (p["revert"], render_revert(cfg)),
    ]


def write_plan(plan: list[tuple[str, str]], force: bool = False) -> list[str]:
    """生成物を書く（`.vbs` は ASCII + CRLF で書く — WSH は BOM 無しを ANSI として読む）。

    既存ファイルは `force` が無い限り温存し、`skipped` として返す。
    """
    written: list[str] = []
    for path, text in plan:
        if os.path.exists(path) and not force:
            written.append("skipped (exists): %s" % path)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        newline = "\r\n" if path.lower().endswith(".vbs") else "\n"
        encoding = "ascii" if path.lower().endswith(".vbs") else "utf-8"
        body = text if text.endswith("\n") else text + "\n"
        with open(path, "w", encoding=encoding, newline=newline) as fh:
            fh.write(body)
        written.append("wrote: %s" % path)
    return written


def register_task(cfg: dict, task_name: str, interval_minutes: int) -> str:
    """見張りタスクを登録する（落ちたときだけ worker が起動する・冪等）。"""
    tr = 'wscript.exe "%s"' % cfg["paths"]["worker"]
    cmd = ["schtasks", "/create", "/tn", task_name, "/sc", "minute",
           "/mo", str(interval_minutes), "/tr", tr, "/f"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        return "task failed (%d): %s" % (proc.returncode, (proc.stderr or proc.stdout).strip())
    return "task registered: %s (every %d min)" % (task_name, interval_minutes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="freeagent-bind の Windows 常駐配線を生成する")
    parser.add_argument("--hermes-home", default=hermes_home_default(),
                        help="既定: %%LOCALAPPDATA%%\\hermes")
    parser.add_argument("--repo", default=REPO_ROOT, help="freeagent-bind のリポジトリ根")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--python", default="", help="python.exe を明示（既定は tools から列挙）")
    parser.add_argument("--pythonw", default="", help="pythonw.exe を明示")
    parser.add_argument("--write", action="store_true", help="生成する（既定は表示のみ）")
    parser.add_argument("--force", action="store_true", help="既存ファイルを上書きする")
    parser.add_argument("--dry-run", action="store_true", help="書き込むパスだけ表示する")
    parser.add_argument("--task", action="store_true", help="見張りタスクも登録する（--write と併用）")
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    args = parser.parse_args(argv)

    cfg = build_config(args.hermes_home, args.repo, args.host, args.port,
                       args.python, args.pythonw)
    plan = build_plan(cfg)

    print("hermes home : %s" % cfg["hermes_home"])
    print("repo        : %s" % cfg["repo"])
    print("endpoint    : http://%s:%d/mcp" % (cfg["host"], cfg["port"]))
    print("python      : %s" % (cfg["python"] or "(not found - pass --python)"))
    print("pythonw     : %s" % (cfg["pythonw"] or "(not found - pass --pythonw)"))
    print("")

    if args.dry_run or not args.write:
        for path, text in plan:
            state = "overwrite" if os.path.exists(path) else "new"
            print("[%s] %s (%d bytes)" % (state, path, len(text.encode("utf-8"))))
        if not args.write:
            print("\n(表示のみ。--write で生成します)")
            return 0

    if args.write:
        for line in write_plan(plan, force=args.force):
            print(line)
        if args.task:
            print(register_task(cfg, args.task_name, args.interval_minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
