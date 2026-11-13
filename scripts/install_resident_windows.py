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

`--task` は見張りタスクを **XML 定義**で登録する（`schtasks /create /tn <名前> /xml <一時ファイル> /f`）。
`/sc minute` の簡易登録は「バッテリー駆動では開始しない／バッテリーに切り替わったら停止する」が
既定で有効になり、ノート PC では見張りが止まる（実測: 簡易登録のタスクは両方 True）。XML では
両方 false・`StartWhenAvailable` true・繰り返しは無期限で登録する。
間隔を空けたくない場合（ログオン時だけ起動すれば十分）は付けない。

worker が毎ティックで行うこと（v0.49.0）:

- **Python の選定は版番号順**（数字の並びを桁揃えして比べる）。素の文字列比較は `python-3.14.10` より
  `python-3.14.9` を上に置く（実測）。標準ライブラリの無い残骸フォルダは選ばない。
- **応答なしの検知**。プロセスが居ても待受ポートが HTTP に答えないティックが `UNRESPONSIVE_STRIKES` 回
  続いたら、そのプロセスを WMI で終了して起動し直す（1 回だけなら起動直後の可能性があるので待つ）。
  見張りの記録は `logs\\<名前>-watchdog.log`（サーバーが掴んでいるログには VBS から追記できない・実測）。
- launcher は **Hermes が起動した親から来たとき**（レジストリに無い `HERMES_*` があるとき）、その親に由来する
  環境変数（レジストリに無い `FREEAGENT_*` / `HERMES_*` / `TERMINAL_*` / `TMPDIR` と env ファイルの定義名）を
  落としてから起動する。
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
import tempfile
from xml.sax.saxutils import escape as _xml_escape

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_PROXY_PORT = 8645
# `hermes proxy start` が `--port` 無しで待ち受けるポート。worker はこれ以外のときだけ `--port` を渡し、
# `--port` の無いコマンドラインをこのポートのプロキシとして扱う。
HERMES_PROXY_DEFAULT_PORT = 8645
DEFAULT_TASK_NAME = "Hermes_Freeagent_Watchdog"
DEFAULT_PROXY_TASK_NAME = "Hermes_Proxy_Watchdog"
DEFAULT_INTERVAL_MINUTES = 5
SERVICE_DIR_NAME = "freeagent-service"
LOG_NAME = "freeagent-autostart.log"
LAUNCHER_NAME = "run_server.pyw"
ENV_NAME = "freeagent.env"
WORKER_NAME = "Hermes_Freeagent.vbs"
PROXY_SERVICE_DIR_NAME = "proxy-service"
PROXY_LOG_NAME = "proxy-service.log"
WATCHDOG_LOG_NAME = "freeagent-watchdog.log"
PROXY_WATCHDOG_LOG_NAME = "proxy-watchdog.log"
# 待受ポートが答えないティックがこの回数続いたら、プロセスを終了して起動し直す。
# 1 回で落とさないのは、起動直後（まだ bind していない数秒）に見張りが当たることがあるため。
UNRESPONSIVE_STRIKES = 2
PROXY_LAUNCHER_NAME = "run_proxy.pyw"
PROXY_WORKER_NAME = "Hermes_Proxy.vbs"
PROXY_TEMPLATE_NAME = "Hermes_Proxy_Launcher.vbs"
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


def proxy_marker_for(port: int) -> str:
    """プロキシのコマンドラインに載る目印。"""
    return "hermes-proxy-%d" % port


def _interpreter_ok(exe: str) -> bool:
    """その `python.exe` が**実際に起動して式を評価できる**か。

    実測（2026-10-27）: `tools/python-3.14.7+<build>` が残骸で、`python.exe --version` は返るのに
    `sys.base_prefix` が**カレントディレクトリ**になり `Could not find platform independent libraries` で
    即死した。`os.path.isfile` だけの判定はこれを通してしまい、**目印の無い pythonw 起動で無言の即死**になる
    （起動ログには `starting` だけが残り、サーバーは LISTENING しない）。だから実行して確かめる。
    """
    if os.name == "nt":
        # 実行形式でないファイルを起動しない。PE でない `python.exe`（空ファイルや壊れた残骸）を
        # コンソールの無い親から起動すると、Windows が 16 ビット扱いで待たされ、1 件ごとに
        # timeout（20 秒）まで戻らない（実測: テキストの stub 1 個で 20 秒・窓なしの親で再現）。
        try:
            with open(exe, "rb") as fh:
                if fh.read(2) != b"MZ":
                    return False
        except OSError:
            return False
    try:
        nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run([exe, "-c", "import sys;print(sys.version_info[0])"],
                              capture_output=True, text=True, timeout=20,
                              creationflags=nw)
    except (OSError, subprocess.SubprocessError):
        return False
    out = (proc.stdout or "").strip()
    return proc.returncode == 0 and out[:1].isdigit()


def _python_dir_key(name: str) -> list:
    """`python-*` フォルダ名を**版番号順**に並べるためのキー（数字の並びを数値として比べる）。

    素の文字列比較は `python-3.14.10` より `python-3.14.9` を上に置く（`"1" < "9"`）。更新で
    3.14.10 が入っても古い 3.14.9 を選び続け、古いフォルダが壊れた時点で `pythonw` の無言即死になる
    （実測: `sorted(reverse=True)` も VBS の `StrComp` も 3.14.9 を選んだ）。worker VBS の `NatKey` と
    同じ規則（数字の並び → 数値、それ以外 → 小文字）にしてあり、生成器と worker の選ぶ版が食い違わない。
    """
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def probe_host(host: str) -> str:
    """応答確認で叩くホスト。`0.0.0.0` / `::` の待受はループバックへ、IPv6 は角括弧つきにする。"""
    host = (host or "").strip()
    if host in ("", "0.0.0.0", "*"):
        return "127.0.0.1"
    if host in ("::", "[::]"):
        return "[::1]"
    if ":" in host and not host.startswith("["):
        return "[%s]" % host
    return host


def discover_python(hermes_home: str) -> tuple[str, str]:
    """`<hermes-home>\\tools\\python-*` から `(python.exe, pythonw.exe)` を探す。

    フォルダ名はインストールごとに変わる（版＋ビルド番号。例 `python-3.14.7+<build>-win32-x64`）ので、
    **名前を書かずに `tools` を列挙する**。選び方は 2 段:
      1. **新しい版から**見る（`_python_dir_key` の版番号順。文字列順だと 3.14.10 < 3.14.9 になる）
      2. `_interpreter_ok()` で**起動できることを確かめる**（stdlib が欠けた残骸を飛ばす）
    見つからなければ空文字を返す（生成側で明示指定を促す）。
    """
    tools = os.path.join(hermes_home, "tools")
    if not os.path.isdir(tools):
        return "", ""
    candidates = sorted(
        (name for name in os.listdir(tools)
         if name.startswith("python-") and os.path.isdir(os.path.join(tools, name))),
        key=_python_dir_key, reverse=True)
    for name in candidates:
        exe = os.path.join(tools, name, "python.exe")
        exe_w = os.path.join(tools, name, "pythonw.exe")
        if os.path.isfile(exe) and _interpreter_ok(exe):
            return exe, (exe_w if os.path.isfile(exe_w) else "")
    return "", ""


# --- worker VBS / launcher が共有する部品（v0.49.0） -------------------------------

# 版番号順のキーと、標準ライブラリの有無（どちらも子プロセスを作らない）。
_VBS_PYTHON_PICK = r"""
' Natural-order key: every digit run is zero-padded, so "3.14.10" sorts above "3.14.9"
' (a plain text compare picked 3.14.9 - measured).
Function NatKey(s)
  Dim i, ch, num, out
  out = ""
  num = ""
  For i = 1 To Len(s)
    ch = Mid(s, i, 1)
    If ch >= "0" And ch <= "9" Then
      num = num & ch
    Else
      If num <> "" Then
        out = out & Right(String(20, "0") & num, 20)
        num = ""
      End If
      out = out & LCase(ch)
    End If
  Next
  If num <> "" Then out = out & Right(String(20, "0") & num, 20)
  NatKey = out
End Function

' A python-* folder without its standard library starts and dies at once (measured: a
' leftover python-3.14.7 kept python.exe but lost Lib). File-system check only: running
' the interpreter from here would allocate a console.
Function HasStdlib(folder)
  Dim z
  HasStdlib = False
  If fso.FileExists(folder.Path & "\Lib\os.py") Then
    HasStdlib = True
    Exit Function
  End If
  For Each z In folder.Files
    If LCase(Left(z.Name, 6)) = "python" And LCase(Right(z.Name, 4)) = ".zip" Then HasStdlib = True
  Next
End Function
"""

# 応答確認・見張りログ・連続回数の記録（どれもコンソールを作らない）。
_VBS_WATCHDOG = r"""
' 1 = the port answered HTTP (any status), 0 = no answer, -1 = the probe is unavailable.
' "Connection: close" matters: without it the server logged one ConnectionResetError
' traceback per probe (measured: 1 per probe without the header, 0 with it).
Function ProbePort(url)
  Dim rq
  ProbePort = -1
  On Error Resume Next
  Set rq = CreateObject("WinHttp.WinHttpRequest.5.1")
  If Err.Number <> 0 Then
    Err.Clear
    Exit Function
  End If
  rq.SetTimeouts 2000, 2000, 5000, 10000
  rq.Open "GET", url, False
  rq.SetRequestHeader "Connection", "close"
  rq.Send
  If Err.Number = 0 Then
    ProbePort = 1
  Else
    ProbePort = 0
  End If
  Err.Clear
End Function

' The watchdog writes to its own file: the server keeps its log open, and a second
' writer from Windows Script Host fails with "permission denied" (measured).
Sub AppendLog(path, msg)
  Dim h
  On Error Resume Next
  Set h = fso.OpenTextFile(path, 8, True)
  h.WriteLine "[" & Now & "] " & msg
  h.Close
  Err.Clear
End Sub

Function ReadStrikes(path)
  Dim h, v
  ReadStrikes = 0
  On Error Resume Next
  If fso.FileExists(path) Then
    Set h = fso.OpenTextFile(path, 1, False)
    v = Trim(h.ReadLine)
    h.Close
    If IsNumeric(v) Then ReadStrikes = CLng(v)
  End If
  Err.Clear
End Function

Sub WriteStrikes(path, n)
  Dim h
  On Error Resume Next
  Set h = fso.OpenTextFile(path, 2, True)
  h.WriteLine CStr(n)
  h.Close
  Err.Clear
End Sub

Sub ClearStrikes(path)
  On Error Resume Next
  If fso.FileExists(path) Then fso.DeleteFile path, True
  Err.Clear
End Sub
"""

# launcher（pythonw から走る Python）が共有する、セッション由来の環境変数の除去。
_PY_SESSION_ENV = r"""
# Hermes のセッション（対話・KANBAN ワーカー・cron）から常駐を起動し直すと、そのセッションの
# 環境変数を引き継ぐ（実測: ログオン起動は .env から 48 個読むのに、セッションからの再起動は 25 個
# ＝残りは親の環境に既にあった）。`実環境 > ファイル` の規則のせいで、そのセッションが一時的に
# export した `FREEAGENT_STATE_DIR`（scratch を指す）などが常駐に居座る。
# ログオン起動と同じ環境にするため、**Hermes が起動した親から来たときだけ**、レジストリ（ユーザー／
# システムの環境変数）に無い `FREEAGENT_*` / `HERMES_*` / `TERMINAL_*` / `TMPDIR` を落とす。
# 判定は「レジストリに無い `HERMES_*` が 1 つでもある」。ログオン起動（Startup・タスクスケジューラ）の
# 環境には `HERMES_*` が無い。特定の名前を並べる方式は漏れる（実測: 対話端末には HERMES_SESSION_ID が
# あるが、コード実行の子プロセスには無く HERMES_RPC_TOKEN などだけがあった）。
SESSION_MARKER_PREFIX = "HERMES_"
SESSION_PREFIXES = ("FREEAGENT_", "HERMES_", "TERMINAL_")
SESSION_EXACT = ("TMPDIR",)
# launcher 自身への指示は残す（テスト・別プロファイル・意図的な持ち込み）。
SESSION_KEEP = ("FREEAGENT_ENV_FILE", "FREEAGENT_KEEP_SESSION_ENV",
                "HERMES_PROXY_LOG", "HERMES_PROXY_FORCE_START")


def _persistent_env_names():
    # レジストリに登録された環境変数名（大文字）。読めなければ None（＝何も落とさない）。
    try:
        import winreg
    except ImportError:
        return None
    names = set()
    found = False
    for root, sub in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, sub) as key:
                found = True
                index = 0
                while True:
                    try:
                        names.add(winreg.EnumValue(key, index)[0].upper())
                    except OSError:
                        break
                    index += 1
        except OSError:
            continue
    return names if found else None


def _drop_session_env(environ, persistent, also=()):
    # セッション由来の変数を `environ` から落とし、落とした名前（昇順）を返す。値は返さない。
    # `also` は接頭辞に依らず落とす名前（launcher が読む env ファイルの定義名。セッションの値ではなく
    # ファイルの値で起動するため。渡さないと、接頭辞の外の名前だけ親の値のまま残る）。
    also_upper = {str(name).upper() for name in also}
    if (environ.get("FREEAGENT_KEEP_SESSION_ENV") or "").strip().lower() in ("1", "true", "yes", "on"):
        return []
    if persistent is None:
        return []
    managed = False
    for name in environ:
        upper = name.upper()
        if (upper.startswith(SESSION_MARKER_PREFIX) and upper not in SESSION_KEEP
                and upper not in persistent and environ.get(name)):
            managed = True
            break
    if not managed:
        return []
    dropped = []
    for name in list(environ):
        upper = name.upper()
        if upper in SESSION_KEEP or upper in persistent:
            continue
        if upper in SESSION_EXACT or upper in also_upper or upper.startswith(SESSION_PREFIXES):
            environ.pop(name, None)
            dropped.append(name)
    return sorted(dropped)
"""


def service_paths(hermes_home: str, startup_dir: str = "") -> dict:
    """生成物のパス。`startup_dir` を与えるとログオン起動の launcher をそこへ置く。

    **テストでは必ず与える**。与えないと実機の `%APPDATA% の Startup フォルダ` を書き換えてしまい、
    実測では実機の launcher が一時ディレクトリを指す状態になり、ログオン起動が壊れた。
    """
    service = os.path.join(hermes_home, SERVICE_DIR_NAME)
    proxy_service = os.path.join(hermes_home, PROXY_SERVICE_DIR_NAME)
    startup_root = startup_dir or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~"),
        "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return {
        "service_dir": service,
        "launcher": os.path.join(service, LAUNCHER_NAME),
        "worker": os.path.join(service, WORKER_NAME),
        "log": os.path.join(hermes_home, "logs", LOG_NAME),
        "watchdog_log": os.path.join(hermes_home, "logs", WATCHDOG_LOG_NAME),
        "service_env": os.path.join(service, ENV_NAME),
        "apply": os.path.join(service, "apply_freeagent_http.sh"),
        "revert": os.path.join(service, "revert_freeagent_stdio.sh"),
        "startup": os.path.join(startup_root, WORKER_NAME),
        "proxy_service_dir": proxy_service,
        "proxy_launcher": os.path.join(proxy_service, PROXY_LAUNCHER_NAME),
        "proxy_worker": os.path.join(proxy_service, PROXY_WORKER_NAME),
        "proxy_startup_template": os.path.join(proxy_service, PROXY_TEMPLATE_NAME),
        "proxy_startup": os.path.join(startup_root, PROXY_WORKER_NAME),
        "proxy_log": os.path.join(hermes_home, "logs", PROXY_LOG_NAME),
        "proxy_watchdog_log": os.path.join(hermes_home, "logs", PROXY_WATCHDOG_LOG_NAME),
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
# 常駐サーバーは別プロセスなので Hermes の MCP `env` が届かない。起動時に .env を読む。
# `FREEAGENT_ENV_FILE` があればそれを最優先（テスト・別プロファイル用）。
ENV_FILES = [os.environ.get("FREEAGENT_ENV_FILE") or os.path.join(HOME, ".env"),
             os.path.join(HOME, "__SERVICE_DIR__", "__ENV_NAME__")]

os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.makedirs(os.path.dirname(LOG), exist_ok=True)
_stream = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
sys.stdout = _stream
sys.stderr = _stream

__SESSION_ENV_HELPER__

def _load_env_file(path):
    """KEY=VALUE を os.environ へ入れる（**既にある値を上書きしない**）。

    Hermes の `.env` は `export KEY=VALUE` や引用符つきの行も混ざるので寛容に読む。
    値は絶対にログへ出さない（返すのは名前だけ）。
    """
    names = []
    if not path or not os.path.isfile(path):
        return names
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                key, sep, value = line.partition("=")
                key = key.strip()
                if not sep or not key or key in os.environ:
                    continue
                value = value.strip()
                # インラインコメント（KEY=1  # 説明）を値に混ぜない（python-dotenv と同じ扱い）。
                if value[:1] not in ('"', "'"):
                    value = value.split(" #", 1)[0].strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if not value:
                    continue
                os.environ[key] = value
                names.append(key)
    except OSError:
        return names
    return names


def _env_file_keys(path):
    # env ファイルが定義する名前だけを返す（値は読まない）。
    keys = []
    if not path or not os.path.isfile(path):
        return keys
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.lower().startswith("export "):
                    line = line[7:].lstrip()
                key, sep, _value = line.partition("=")
                if sep and key.strip():
                    keys.append(key.strip())
    except OSError:
        pass
    return keys


# ファイルを読む**前に**落とす（落とした名前は、このあと .env / freeagent.env の値で埋まる）。
# env ファイルが定義する名前も対象にする: セッションから起動されたときは、親の値ではなくファイルの値で
# 起動する（実測: 接頭辞だけだと 48 個のうち 15 個が親の値のまま残った）。
_file_keys = [key for _env_path in ENV_FILES for key in _env_file_keys(_env_path)]
_dropped = _drop_session_env(os.environ, _persistent_env_names(), _file_keys)
if _dropped:
    print("[env] dropped %d session-inherited vars: %s" % (len(_dropped), ", ".join(_dropped)))
_loaded = []
for _env_path in ENV_FILES:
    _loaded.extend(_load_env_file(_env_path))
print("[env] loaded %d vars: %s" % (len(_loaded), ", ".join(_loaded) or "none"))

sys.argv = ["server.py", "--transport", "http", "--host", "__HOST__", "--port", str(PORT)]
runpy.run_path(os.path.join(REPO, "src", "freeagent_bind", "server.py"), run_name="__main__")
'''.replace("__HERMES_HOME__", cfg["hermes_home"]).replace("__REPO__", cfg["repo"]).replace(
        "__LOG_NAME__", LOG_NAME).replace("__PORT__", str(cfg["port"])).replace(
        "__HOST__", cfg["host"]).replace("__MARKER__", cfg["marker"]).replace(
        "__SERVICE_DIR__", SERVICE_DIR_NAME).replace("__ENV_NAME__", ENV_NAME).replace(
        "__SESSION_ENV_HELPER__", _PY_SESSION_ENV.strip("\n")).lstrip("\n")


def render_worker(cfg: dict) -> str:
    """worker VBS。生存確認は WMI、応答確認は WinHttp、起動は pythonw、ログはファイル追記（すべて窓を作らない）。"""
    return r"""
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
'   - liveness = a WMI query of Win32_Process (python processes only, matched by
'     CommandLine; no child process), with a WinHttp probe only if WMI is unavailable;
'   - responsiveness = a WinHttp GET with "Connection: close". A process that exists
'     but does not answer on __STRIKES__ ticks in a row is terminated through WMI and restarted;
'   - log lines are appended with Scripting.FileSystemObject (no "cmd /c echo");
'   - the server itself is started with pythonw.exe (GUI subsystem: no console).
' ASCII only: Windows Script Host reads a .vbs without a UTF-16 BOM as ANSI.
Option Explicit
Dim sh, fso, hermesHome, repo, logDir, logFile, watchLog, running, py, pyw, bestName, bestKey, tools, f
Dim lf, ts, svc, procs, p, cl, nm, hit, key, wmiOk, probeUrl, strikeFile, strikes, killed

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

hermesHome = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\hermes"
repo = "__REPO__"
logDir = hermesHome & "\logs"
logFile = logDir & "\__LOG_NAME__"
watchLog = logDir & "\__WATCHDOG_LOG_NAME__"
probeUrl = "http://__PROBE_HOST__:__PORT__/"
strikeFile = fso.GetParentFolderName(WScript.ScriptFullName) & "\unresponsive-__PORT__.count"
ts = Now

If Not fso.FileExists(repo & "\src\freeagent_bind\server.py") Then WScript.Quit 0
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

' Liveness: WMI on the server's own command line. Pure COM - no child process.
' Each value is assigned before it is tested: under "On Error Resume Next" a failing
' If-condition runs the If-body (measured), which would mark a dead server as running.
running = False
wmiOk = False
On Error Resume Next
Set svc = GetObject("winmgmts:\\.\root\cimv2")
If Err.Number = 0 And IsObject(svc) Then
  Set procs = svc.ExecQuery("SELECT ProcessId, Name, CommandLine FROM Win32_Process")
  If Err.Number = 0 And IsObject(procs) Then
    wmiOk = True
    For Each p In procs
      nm = ""
      cl = Null
      nm = p.Name
      cl = p.CommandLine
      hit = False
      If Not IsNull(cl) Then hit = IsServer(nm, cl)
      If hit = True Then running = True
    Next
  End If
  Err.Clear
End If
On Error GoTo 0

If Not wmiOk Then
  ' WMI unavailable -> the port itself is the only evidence (still no console window).
  If ProbePort(probeUrl) = 1 Then WScript.Quit 0
ElseIf running Then
  ' The process exists. Make sure it also answers; a hung server is otherwise never
  ' restarted, because the liveness check above only looks at the process list.
  If ProbePort(probeUrl) <> 0 Then
    ClearStrikes strikeFile
    WScript.Quit 0
  End If
  strikes = ReadStrikes(strikeFile) + 1
  If strikes < __STRIKES__ Then
    WriteStrikes strikeFile, strikes
    AppendLog watchLog, "server process is alive but port __PORT__ did not answer (" & strikes & "/__STRIKES__); it is restarted if the next tick fails too"
    WScript.Quit 0
  End If
  killed = KillServers()
  AppendLog watchLog, "port __PORT__ did not answer on __STRIKES__ ticks in a row; terminated " & killed & " process(es)"
  ' Nothing was terminated (WMI failed, or only a legacy-shaped process exists): do not
  ' start a second server. The strike count is kept, so the next tick tries again.
  If killed = 0 Then WScript.Quit 0
  ClearStrikes strikeFile
  WScript.Sleep 2000
End If

' The bundled python lives in a folder whose name changes per install (version plus
' build number), so enumerate the tools folder instead of hardcoding a path.
py = ""
pyw = ""
bestName = ""
bestKey = ""
tools = hermesHome & "\tools"
If fso.FolderExists(tools) Then
  For Each f In fso.GetFolder(tools).SubFolders
    If Left(f.Name, 7) = "python-" Then
      ' Pick the NEWEST usable python-* folder by VERSION ORDER (NatKey), skipping
      ' folders that lost their standard library. Folder order selected the oldest
      ' one and a text compare ranks 3.14.9 above 3.14.10; either way a stale install
      ' dies silently: pythonw has no console and the log shows only "starting".
      If fso.FileExists(f.Path & "\python.exe") And HasStdlib(f) Then
        key = NatKey(f.Name)
        If StrComp(key, bestKey, vbBinaryCompare) > 0 Then
          bestKey = key
          bestName = f.Name
          py = f.Path & "\python.exe"
          pyw = ""
          If fso.FileExists(f.Path & "\pythonw.exe") Then pyw = f.Path & "\pythonw.exe"
        End If
      End If
    End If
  Next
End If
If py = "" Then
  AppendLog watchLog, "no usable python-* folder under " & tools & "; the server was not started"
  WScript.Quit 0
End If

' A strike left by an earlier server must not count against the one started now.
ClearStrikes strikeFile

' Log the start with a plain file append ("cmd /c echo" would spawn a console too).
On Error Resume Next
Set lf = fso.OpenTextFile(logFile, 8, True)
lf.WriteLine "[" & ts & "] starting freeagent-bind http with " & py
lf.Close
On Error GoTo 0

' Launch console-free. Fall back to python.exe (hidden) only if pythonw is missing.
sh.CurrentDirectory = repo
If pyw <> "" Then
  sh.Run "" & Chr(34) & pyw & Chr(34) & " " & Chr(34) & hermesHome & "\__SERVICE_DIR__\__LAUNCHER_NAME__" & Chr(34) & " __MARKER__", 0, False
Else
  sh.Run "" & Chr(34) & py & Chr(34) & " src\freeagent_bind\server.py --transport http --host __HOST__ --port __PORT__", 0, False
End If

' --- helpers ---------------------------------------------------------------

' True for the resident server only. The name test keeps a shell whose own command
' line merely mentions the marker (a search, an editor) from counting as the server.
Function IsServer(name, cmdLine)
  IsServer = False
  If LCase(Left(name, 6)) <> "python" Then Exit Function
  If InStr(cmdLine, "__MARKER__") > 0 Then IsServer = True
  If InStr(cmdLine, "freeagent_bind") > 0 And InStr(cmdLine, "server.py") > 0 And InStr(cmdLine, "--port __PORT__") > 0 Then IsServer = True
End Function

Function KillServers()
  Dim q, x, c, n, count
  count = 0
  On Error Resume Next
  Set q = svc.ExecQuery("SELECT ProcessId, Name, CommandLine FROM Win32_Process")
  For Each x In q
    n = ""
    c = Null
    n = x.Name
    c = x.CommandLine
    hit = False
    If Not IsNull(c) Then hit = IsServer(n, c)
    If hit = True Then
      AppendLog watchLog, "terminating unresponsive server pid=" & x.ProcessId
      x.Terminate
      count = count + 1
    End If
  Next
  Err.Clear
  KillServers = count
End Function
__VBS_PYTHON_PICK____VBS_WATCHDOG__""".replace("__HERMES_HOME__", cfg["hermes_home"]).replace("__REPO__", cfg["repo"]).replace(
        "__LOG_NAME__", LOG_NAME).replace("__WATCHDOG_LOG_NAME__", WATCHDOG_LOG_NAME).replace(
        "__SERVICE_DIR__", SERVICE_DIR_NAME).replace(
        "__LAUNCHER_NAME__", LAUNCHER_NAME).replace("__PORT__", str(cfg["port"])).replace(
        "__PROBE_HOST__", probe_host(cfg["host"])).replace(
        "__HOST__", cfg["host"]).replace("__MARKER__", cfg["marker"]).replace(
        "__STRIKES__", str(UNRESPONSIVE_STRIKES)).replace(
        "__VBS_PYTHON_PICK__", _VBS_PYTHON_PICK).replace(
        "__VBS_WATCHDOG__", _VBS_WATCHDOG).lstrip("\n")


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


def render_proxy_launcher(cfg: dict) -> str:
    """ローカル OpenAI 互換プロキシ（`hermes proxy start`）を黒窓無しで起動するランチャ。

    重要（2026-11-09 実測）:
    - `DETACHED_PROCESS (0x00000008)` を使ってはいけない。`DETACHED_PROCESS` の子は
      「コンソール無し」になるため、`hermes_cli.main` の起動処理が呼ぶコンソール孫プロセス
      （`Scripts\\python.exe` × 2、`cmd\\git.exe` × 2）がそれぞれ自分のコンソールを新造し、
      Windows 11 の Windows Terminal が **1 回の起動につき 4 個の黒い画面**を連続表示する。
      さらに MSDN 仕様により `DETACHED_PROCESS` と `CREATE_NO_WINDOW` の併用は
      `CREATE_NO_WINDOW` が無視される。
    - 正しくは `CREATE_NO_WINDOW (0x08000000) | CREATE_NEW_PROCESS_GROUP (0x00000200)`
      （`DETACHED_PROCESS` は混ぜない）で起動し、親 `python.exe` が所有する非表示コンソール
      （`conhost.exe 0x4`）を孫プロセスに継承させる。
    - Nous Portal が未認証／トークン失効のときは `auth.json` を事前確認して起動自体をスキップする。
    """
    return '''# -*- coding: utf-8 -*-
r"""ローカル OpenAI 互換プロキシを**コンソール無しで**起動する薄いランチャ。

`Hermes_Proxy.vbs`（ログオン launcher と 5 分周期の見張りタスクの両方）が
`pythonw.exe` から呼ぶ。

重要（Windows 11 で黒い画面が複数出る罠と対策）:
  1. `DETACHED_PROCESS (0x00000008)` を使ってはいけない。
     `DETACHED_PROCESS` で起動された `python.exe` は「コンソール無し」になるため、
     `hermes_cli.main` の起動処理中に呼ばれるコンソール子プロセス
     （`pm-runtime\\...\\Scripts\\python.exe` × 2、`cmd\\git.exe` × 2）がそれぞれ新しく
     自分のコンソールを割り当て、Windows 11 の Windows Terminal が **1 回の起動につき
     4 個の黒い画面**を連続表示する（`EnumWindows` で 4 窓・11 イベントを実測）。
     さらに MSDN 仕様により `DETACHED_PROCESS` と `CREATE_NO_WINDOW` を併用すると
     `CREATE_NO_WINDOW` は無視される。
  2. 正しくは **`CREATE_NO_WINDOW (0x08000000) | CREATE_NEW_PROCESS_GROUP (0x00000200)`**
     （`DETACHED_PROCESS` は混ぜない）で起動する。これにより親 `python.exe` が
     非表示コンソール（`conhost.exe 0x4`）を所有し、子孫の `python.exe` や `git.exe` も
     その非表示コンソールを継承するため、黒い画面は **0 個**になる（実測で確認済み）。
  3. Nous Portal が未ログイン／トークン失効（`auth.json` の `providers.nous` に
     `agent_key` も `refresh_token`+`access_token` も無い状態）のときは、起動しても
     即 `exit 2 (Not logged into Nous Portal)` になるため、5 分おきに無駄な起動を
     繰り返さないよう事前判定してスキップする（`HERMES_PROXY_FORCE_START=1` で強制起動可）。
"""
import json
import os
import subprocess
import sys

HOME = os.path.expandvars(r"__HERMES_HOME__")
REPO = os.path.join(HOME, "hermes-agent")
LOG = os.environ.get("HERMES_PROXY_LOG") or os.path.join(HOME, "logs", "__PROXY_LOG_NAME__")
AUTH_FILE = os.path.join(HOME, "auth.json")
MARKER_PREFIX = "hermes-proxy-"

# 0x00000008（コンソール剥奪フラグ）は絶対に入れない（子孫の git.exe / python.exe が黒窓を開くため）。
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


__SESSION_ENV_HELPER__


def _console_python():
    """子に使う `python.exe`（コンソール版）のパス。

    `pythonw.exe` は GUI サブシステムなので、そのまま CLI を回すと stdio 前提の出力や
    子プロセス起動で問題が起きる。必ず `python.exe` を使い、窓は `CREATE_NO_WINDOW`
    （非表示コンソールの所有＋子孫への継承）で抑える。
    """
    exe = sys.executable or ""
    base = os.path.dirname(exe)
    if base:
        cand = os.path.join(base, "python.exe")
        if os.path.isfile(cand):
            return cand
    return exe


def _split_argv(argv):
    """目印（hermes-proxy-<port>）を落として CLI に渡す引数だけを返す。"""
    keep = []
    for arg in argv:
        if arg.startswith(MARKER_PREFIX) and len(arg) > len(MARKER_PREFIX):
            continue
        keep.append(arg)
    return keep


def _target_provider(extra_args):
    """引数から `--provider` の値を取り出す（未指定は既定の `nous`）。"""
    for i, arg in enumerate(extra_args):
        if arg == "--provider" and i + 1 < len(extra_args):
            return (extra_args[i + 1] or "").strip().lower() or "nous"
        if arg.startswith("--provider="):
            return arg.partition("=")[2].strip().lower() or "nous"
    return "nous"


def _nous_authenticated():
    """`auth.json` に Nous Portal の有効な資格情報があるか（`NousPortalAdapter.is_authenticated` と同条件）。"""
    if not os.path.isfile(AUTH_FILE):
        return False
    try:
        with open(AUTH_FILE, encoding="utf-8-sig", errors="replace") as fh:
            store = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(store, dict):
        return False
    providers = store.get("providers")
    if not isinstance(providers, dict):
        return False
    state = providers.get("nous")
    if not isinstance(state, dict):
        return False
    return bool(state.get("agent_key") or (state.get("refresh_token") and state.get("access_token")))


def _last_log_line(path):
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [ln.strip() for ln in fh if ln.strip()]
        return lines[-1] if lines else ""
    except OSError:
        return ""


def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    extra = _split_argv(sys.argv[1:])
    provider = _target_provider(extra)
    force_start = os.environ.get("HERMES_PROXY_FORCE_START", "").strip().lower() in ("1", "true", "yes", "on")

    if not force_start and provider == "nous" and not _nous_authenticated():
        skip_msg = "[launcher] skipped: Nous Portal not authenticated (run `hermes auth add nous` to enable proxy)"
        if _last_log_line(LOG) != skip_msg:
            try:
                with open(LOG, "a", encoding="utf-8", errors="replace", buffering=1) as log:
                    log.write(skip_msg + "\\n")
            except OSError:
                pass
        return 0

    args = ["-m", "hermes_cli.main", "proxy", "start"] + extra

    env = dict(os.environ)
    # Hermes のセッションから起動し直されたときは、そのセッション由来の変数を子へ渡さない
    # （ログオン起動と同じ環境にする。落とすのは子の環境だけで、launcher 自身は触らない）。
    dropped = _drop_session_env(env, _persistent_env_names())
    env.setdefault("HERMES_HOME", HOME)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = REPO + (";" + existing if existing else "")

    with open(LOG, "a", encoding="utf-8", errors="replace", buffering=1) as log:
        child = subprocess.Popen(
            [_console_python(), *args],
            cwd=HOME,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        if dropped:
            log.write("[launcher] dropped %d session-inherited vars: %s\\n" % (len(dropped), ", ".join(dropped)))
        log.write("[launcher] started pid=%d args=%s\\n" % (child.pid, " ".join(args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''.replace("__HERMES_HOME__", cfg["hermes_home"]).replace(
        "__PROXY_LOG_NAME__", PROXY_LOG_NAME).replace(
        "__SESSION_ENV_HELPER__", _PY_SESSION_ENV.strip("\n")).lstrip("\n")


def render_proxy_worker(cfg: dict) -> str:
    """プロキシ用 worker VBS（WMI 生存確認・WinHttp 応答確認・pythonw 起動・ASCII のみ）。"""
    proxy_port = str(cfg.get("proxy_port", DEFAULT_PROXY_PORT))
    return r"""
' Hermes Agent - local OpenAI-compatible proxy autostart
' Serves Nous Portal credentials on 127.0.0.1:__PROXY_PORT__ for local MCP servers
' (e.g. freeagent-bind's sub-LLM tools). Skips when the proxy is already up.
'
' NO CONSOLE PROCESS IS EVER SPAWNED HERE:
'   - liveness = a WMI query of Win32_Process CommandLine (no child process at all),
'     with a WinHttp probe only if WMI is unavailable;
'   - responsiveness = a WinHttp GET with "Connection: close". A proxy process that
'     exists but does not answer on __STRIKES__ ticks in a row is terminated through WMI and restarted;
'   - the server is started by run_proxy.pyw (via pythonw.exe), which launches
'     "python.exe -m hermes_cli.main proxy start" with CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
'     (never DETACHED_PROCESS, so all grandchild git/python processes inherit the hidden console)
'     and skips launch when Nous Portal is not authenticated.
' ASCII only: Windows Script Host reads a .vbs without a UTF-16 BOM as ANSI.
Option Explicit
Dim sh, fso, home, launcher, logDir, watchLog, running, svc, procs, p, cl, nm, hit, wmiOk
Dim tools, f, pyw, bestName, bestKey, key, port, startupDir, startupLauncher, templateLauncher
Dim probeUrl, strikeFile, strikes, killed

Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

home = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\hermes"
launcher = home & "\__PROXY_SERVICE_DIR__\__PROXY_LAUNCHER_NAME__"
logDir = home & "\logs"
watchLog = logDir & "\__PROXY_WATCHDOG_LOG_NAME__"

port = "__PROXY_PORT__"
If WScript.Arguments.Count > 0 Then port = WScript.Arguments(0)
probeUrl = "http://127.0.0.1:" & port & "/"
strikeFile = fso.GetParentFolderName(WScript.ScriptFullName) & "\unresponsive-" & port & ".count"

If Not fso.FileExists(launcher) Then WScript.Quit 0
If Not fso.FolderExists(logDir) Then fso.CreateFolder(logDir)

' Restore the Startup launcher from the template when an update or test removed it.
startupDir = sh.SpecialFolders("Startup")
startupLauncher = startupDir & "\__PROXY_WORKER_NAME__"
templateLauncher = home & "\__PROXY_SERVICE_DIR__\__PROXY_TEMPLATE_NAME__"
On Error Resume Next
If Not fso.FileExists(startupLauncher) Then
  If fso.FileExists(templateLauncher) Then fso.CopyFile templateLauncher, startupLauncher, True
End If
On Error GoTo 0

' Liveness: WMI on the proxy's own command line. Pure COM - no child process.
' Each value is assigned before it is tested: under "On Error Resume Next" a failing
' If-condition runs the If-body (measured), which would mark a dead proxy as running.
running = False
wmiOk = False
On Error Resume Next
Set svc = GetObject("winmgmts:\\.\root\cimv2")
If Err.Number = 0 And IsObject(svc) Then
  Set procs = svc.ExecQuery("SELECT ProcessId, Name, CommandLine FROM Win32_Process")
  If Err.Number = 0 And IsObject(procs) Then
    wmiOk = True
    For Each p In procs
      nm = ""
      cl = Null
      nm = p.Name
      cl = p.CommandLine
      hit = False
      If Not IsNull(cl) Then hit = IsProxy(nm, cl)
      If hit = True Then running = True
      ' Legacy shape (started by the old launcher): the shell parent carries
      ' hermes.cmd + "proxy start". Accept it so a transition never starts a second one.
      hit = False
      If Not IsNull(cl) Then hit = (InStr(cl, "hermes.cmd") > 0 And InStr(cl, "proxy start") > 0)
      If hit = True Then running = True
    Next
  End If
  Err.Clear
End If
On Error GoTo 0

If Not wmiOk Then
  ' WMI unavailable -> the port itself is the only evidence (still no console window).
  If ProbePort(probeUrl) = 1 Then WScript.Quit 0
ElseIf running Then
  ' The process exists. Make sure it also answers; a hung proxy is otherwise never
  ' restarted, because the liveness check above only looks at the process list.
  If ProbePort(probeUrl) <> 0 Then
    ClearStrikes strikeFile
    WScript.Quit 0
  End If
  strikes = ReadStrikes(strikeFile) + 1
  If strikes < __STRIKES__ Then
    WriteStrikes strikeFile, strikes
    AppendLog watchLog, "proxy process is alive but port " & port & " did not answer (" & strikes & "/__STRIKES__); it is restarted if the next tick fails too"
    WScript.Quit 0
  End If
  killed = KillProxies()
  AppendLog watchLog, "port " & port & " did not answer on __STRIKES__ ticks in a row; terminated " & killed & " process(es)"
  ' Nothing was terminated (WMI failed, or only a legacy-shaped process exists): do not
  ' start a second proxy. The strike count is kept, so the next tick tries again.
  If killed = 0 Then WScript.Quit 0
  ClearStrikes strikeFile
  WScript.Sleep 2000
End If

' pythonw.exe lives in a folder whose name changes per install (version + build),
' so enumerate the tools folder instead of hardcoding a path. Version order (NatKey),
' skipping folders without a standard library - see the helpers below.
pyw = ""
bestName = ""
bestKey = ""
tools = home & "\tools"
If fso.FolderExists(tools) Then
  For Each f In fso.GetFolder(tools).SubFolders
    If Left(f.Name, 7) = "python-" Then
      If fso.FileExists(f.Path & "\pythonw.exe") And HasStdlib(f) Then
        key = NatKey(f.Name)
        If StrComp(key, bestKey, vbBinaryCompare) > 0 Then
          bestKey = key
          bestName = f.Name
          pyw = f.Path & "\pythonw.exe"
        End If
      End If
    End If
  Next
End If
If pyw = "" Then
  AppendLog watchLog, "no usable python-* folder under " & tools & "; the proxy was not started"
  WScript.Quit 0
End If

' A strike left by an earlier proxy must not count against the one started now.
ClearStrikes strikeFile

' Console-free launch: pythonw (GUI subsystem) runs the thin launcher, which starts
' the CLI with CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, so no window is ever shown.
sh.CurrentDirectory = home
If port = "__HERMES_DEFAULT_PORT__" Then
  sh.Run "" & Chr(34) & pyw & Chr(34) & " " & Chr(34) & launcher & Chr(34) & " hermes-proxy-" & port, 0, False
Else
  sh.Run "" & Chr(34) & pyw & Chr(34) & " " & Chr(34) & launcher & Chr(34) & " --port " & port & " hermes-proxy-" & port, 0, False
End If

' --- helpers ---------------------------------------------------------------

' True for the proxy's own python process ("-m hermes_cli.main proxy start") ON THIS
' PORT: "--port N" must name it, and a command line without --port is the Hermes
' default (__HERMES_DEFAULT_PORT__). A proxy on another port is neither "already running" nor ours to stop.
Function IsProxy(name, cmdLine)
  IsProxy = False
  If LCase(Left(name, 6)) <> "python" Then Exit Function
  If InStr(cmdLine, "-m hermes_cli.main proxy start") > 0 Then
    If InStr(cmdLine, " --port ") > 0 Then
      If InStr(cmdLine & " ", " --port " & port & " ") > 0 Then IsProxy = True
    ElseIf port = "__HERMES_DEFAULT_PORT__" Then
      IsProxy = True
    End If
  End If
End Function

Function KillProxies()
  Dim q, x, c, n, count
  count = 0
  On Error Resume Next
  Set q = svc.ExecQuery("SELECT ProcessId, Name, CommandLine FROM Win32_Process")
  For Each x In q
    n = ""
    c = Null
    n = x.Name
    c = x.CommandLine
    hit = False
    If Not IsNull(c) Then hit = IsProxy(n, c)
    If hit = True Then
      AppendLog watchLog, "terminating unresponsive proxy pid=" & x.ProcessId
      x.Terminate
      count = count + 1
    End If
  Next
  Err.Clear
  KillProxies = count
End Function
__VBS_PYTHON_PICK____VBS_WATCHDOG__""".replace("__PROXY_SERVICE_DIR__", PROXY_SERVICE_DIR_NAME).replace(
        "__PROXY_LAUNCHER_NAME__", PROXY_LAUNCHER_NAME).replace(
        "__PROXY_WORKER_NAME__", PROXY_WORKER_NAME).replace(
        "__PROXY_TEMPLATE_NAME__", PROXY_TEMPLATE_NAME).replace(
        "__PROXY_WATCHDOG_LOG_NAME__", PROXY_WATCHDOG_LOG_NAME).replace(
        "__PROXY_PORT__", proxy_port).replace(
        "__HERMES_DEFAULT_PORT__", str(HERMES_PROXY_DEFAULT_PORT)).replace(
        "__STRIKES__", str(UNRESPONSIVE_STRIKES)).replace(
        "__VBS_PYTHON_PICK__", _VBS_PYTHON_PICK).replace(
        "__VBS_WATCHDOG__", _VBS_WATCHDOG).lstrip("\n")


def render_proxy_startup(cfg: dict) -> str:
    """ログオン時にプロキシ worker を隠して起動する launcher（ASCII のみ）。"""
    return '''
' Hermes Agent - local OpenAI-compatible proxy autostart (logon launcher)
' ASCII only.
Option Explicit
Dim fso, sh, target
target = "__PROXY_WORKER__"
Set fso = CreateObject("Scripting.FileSystemObject")
If Not fso.FileExists(target) Then WScript.Quit 0
Set sh = CreateObject("WScript.Shell")
sh.Run "wscript.exe " & Chr(34) & target & Chr(34), 0, False
'''.replace("__PROXY_WORKER__", cfg["paths"]["proxy_worker"]).lstrip("\n")


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


def render_service_env(cfg: dict) -> str:
    """常駐サーバー専用の環境変数ファイルの雛形（**無いときだけ**作る）。

    キーは Hermes の `.env`（launcher が読む）にあり、ここへ書くのは**確認フラグ**です。
    フラグは「自分がそのプラン／データ条件で使う」という利用者の申告なので、雛形は
    すべてコメントアウトしておきます（規約 31）。
    """
    return """# freeagent-bind 常駐サーバー専用の環境変数（このファイルは常駐プロセスだけが読みます）。
# 生成: scripts/install_resident_windows.py（**既存ファイルは上書きしません**）。
#
# launcher（run_server.pyw）が起動時に読む順序:
#   1. %LOCALAPPDATA%/hermes/.env        … Hermes のキー（OPENROUTER_API_KEY / NVIDIA_API_KEY /
#                                            HF_TOKEN / GEMINI_API_KEY / OLLAMA_API_KEY など）
#   2. このファイル                        … 常駐サーバー固有の設定・確認フラグ
# すでに環境にある値は上書きしません（launcher は名前だけをログに出します。値は出ません）。
# 反映には常駐サーバーの再起動が要ります（見張りは生存確認だけで、動いているプロセスの
# 環境は読み直しません）。
#
# 書き方: 1 行 1 つ `KEY=VALUE`。値の後ろにインラインコメントを書かないでください
# （`KEY=1  # 説明` は値に混ざります）。`export KEY=VALUE` と引用符は受け付けます。
#
# ここに書くのは「確認フラグ」です。キーだけでは有効化されない提供元と、必要なフラグ:
#   FREEAGENT_GROQ_FREE_TIER=1            # Groq の Free プランであることの申告
#   FREEAGENT_CLOUDFLARE_FREE_PLAN=1      # Workers Free プランであることの申告
#   FREEAGENT_VERCEL_FREE_TIER=1          # Vercel AI Gateway の Free tier であることの申告
#   FREEAGENT_OLLAMA_FREE_PLAN=1          # Ollama Cloud の Free プラン（starter クレジット）
#   FREEAGENT_GEMINI_FREE_TIER=1          # Gemini の Free tier
#   FREEAGENT_GEMINI_UNPAID_DATA_ACK=1    # 非機密データ限定の同意（未払い枠は人手レビュー対象）
"""


def build_config(hermes_home: str, repo: str, host: str, port: int,
                 python: str = "", pythonw: str = "", startup_dir: str = "",
                 proxy_port: int = DEFAULT_PROXY_PORT,
                 with_proxy: bool = True) -> dict:
    """生成に必要な値一式（見つかった python を含む）。

    `startup_dir` を空にすると**実機の Startup フォルダ**を使う。テストは必ず与える。
    """
    paths = service_paths(hermes_home, startup_dir)
    found_py, found_pyw = discover_python(hermes_home)
    return {
        "hermes_home": hermes_home,
        "repo": repo,
        "host": host,
        "port": port,
        "proxy_port": proxy_port,
        "with_proxy": with_proxy,
        "python": python or found_py,
        "pythonw": pythonw or found_pyw,
        "marker": marker_for(port),
        "proxy_marker": proxy_marker_for(proxy_port),
        "paths": paths,
    }


def build_proxy_plan(cfg: dict) -> list[tuple[str, str]]:
    """`proxy-service` 側の `(出力パス, 内容)` の一覧。書き込みはしない。"""
    p = cfg["paths"]
    startup_vbs = render_proxy_startup(cfg)
    return [
        (p["proxy_launcher"], render_proxy_launcher(cfg)),
        (p["proxy_worker"], render_proxy_worker(cfg)),
        (p["proxy_startup_template"], startup_vbs),
        (p["proxy_startup"], startup_vbs),
    ]


def build_plan(cfg: dict) -> list[tuple[str, str]]:
    """`(出力パス, 内容)` の一覧。書き込みはしない。"""
    p = cfg["paths"]
    plan = [
        (p["launcher"], render_launcher(cfg)),
        (p["worker"], render_worker(cfg)),
        (p["startup"], render_startup(cfg)),
        (p["apply"], render_apply(cfg)),
        (p["revert"], render_revert(cfg)),
    ]
    if cfg.get("with_proxy", True):
        plan.extend(build_proxy_plan(cfg))
    return plan


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


def render_task_xml(worker_path: str, interval_minutes: int, start: str = "") -> str:
    """見張りタスクの XML 定義（`schtasks /create /xml` へ渡す）。

    `schtasks /create /sc minute` の簡易登録は電源まわりが既定値になる（実測: `DisallowStartIfOnBatteries`
    と `StopIfGoingOnBatteries` が True）。ノート PC をバッテリーで使っている間は見張りが開始されず、
    落ちた常駐が戻らない。XML で明示する:

    - `DisallowStartIfOnBatteries` / `StopIfGoingOnBatteries` = false（電源に依らず見張る）
    - `StartWhenAvailable` = true（スリープで逃したティックを復帰後に実行する）
    - `Repetition` に `Duration` を書かない（無期限に繰り返す）
    - `MultipleInstancesPolicy` = IgnoreNew（前のティックが残っていたら重ねない）
    - `LogonType` = InteractiveToken・`UserId` なし（登録した利用者の対話セッションで動く。パスワード不要）
    """
    interval = max(1, int(interval_minutes))
    start = start or datetime.datetime.now().replace(microsecond=0).isoformat()
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>hermes-freeagent-bind watchdog: runs the worker script, which starts the server only when it is down or unresponsive.</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT%dM</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>%s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>wscript.exe</Command>
      <Arguments>%s</Arguments>
    </Exec>
  </Actions>
</Task>
""" % (interval, _xml_escape(start), _xml_escape('"%s"' % worker_path))


def _decode_console(raw) -> str:
    """コンソール向けプログラム（schtasks）の出力を復号する。OEM → ANSI → UTF-8 の順に試す。"""
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw
    for encoding in ("oem", "mbcs", "utf-8"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def _register_schtask(worker_path: str, task_name: str, interval_minutes: int) -> str:
    """指定した worker VBS の見張りタスクを XML 定義で登録する（冪等・電源に依らず動く）。"""
    xml = render_task_xml(worker_path, interval_minutes)
    fd, xml_path = tempfile.mkstemp(prefix="freeagent-task-", suffix=".xml")
    os.close(fd)
    try:
        # schtasks は UTF-16（BOM つき）の定義を読む。
        with open(xml_path, "w", encoding="utf-16", newline="\r\n") as fh:
            fh.write(xml)
        cmd = ["schtasks", "/create", "/tn", task_name, "/xml", xml_path, "/f"]
        nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            # バイト列で受けて自分で復号する。`text=True` は PYTHONUTF8=1 の下で schtasks の出力
            # （コンソールのコードページ。日本語 Windows は cp932）を UTF-8 として読み、読み取り
            # スレッドが UnicodeDecodeError で落ちて失敗理由が消える（実測）。
            proc = subprocess.run(cmd, capture_output=True, creationflags=nw)
        except OSError as exc:
            return "task failed: %s" % exc
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass
    if proc.returncode != 0:
        detail = _decode_console(proc.stderr) or _decode_console(proc.stdout)
        return "task failed (%d): %s" % (proc.returncode, detail.strip())
    return "task registered: %s (every %d min, runs on battery too)" % (task_name, max(1, int(interval_minutes)))


def register_task(cfg: dict, task_name: str, interval_minutes: int) -> str:
    """見張りタスクを登録する（落ちたときだけ worker が起動する・冪等）。"""
    return _register_schtask(cfg["paths"]["worker"], task_name, interval_minutes)


def register_proxy_task(cfg: dict, task_name: str = DEFAULT_PROXY_TASK_NAME,
                        interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> str:
    """ローカルプロキシの見張りタスクを登録する（落ちたときだけ worker が起動する・冪等）。"""
    return _register_schtask(cfg["paths"]["proxy_worker"], task_name, interval_minutes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="freeagent-bind の Windows 常駐配線を生成する")
    parser.add_argument("--hermes-home", default=hermes_home_default(),
                        help="既定: %%LOCALAPPDATA%%\\hermes")
    parser.add_argument("--repo", default=REPO_ROOT, help="freeagent-bind のリポジトリ根")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument("--no-proxy", action="store_true",
                        help="proxy-service 側の配線生成・見張りタスク登録を行わない")
    parser.add_argument("--python", default="", help="python.exe を明示（既定は tools から列挙）")
    parser.add_argument("--pythonw", default="", help="pythonw.exe を明示")
    parser.add_argument("--write", action="store_true", help="生成する（既定は表示のみ）")
    parser.add_argument("--force", action="store_true", help="既存ファイルを上書きする")
    parser.add_argument("--dry-run", action="store_true", help="書き込むパスだけ表示する")
    parser.add_argument("--task", action="store_true", help="見張りタスクも登録する（--write と併用）")
    parser.add_argument("--startup-dir", default=os.environ.get("FREEAGENT_STARTUP_DIR", ""),
                        help="ログオン起動 launcher を置くフォルダ（既定: %%APPDATA%%\\...\\Startup）。"
                             "テストは必ず一時ディレクトリを渡す")
    parser.add_argument("--task-name", default=DEFAULT_TASK_NAME)
    parser.add_argument("--proxy-task-name", default=DEFAULT_PROXY_TASK_NAME)
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    args = parser.parse_args(argv)

    cfg = build_config(args.hermes_home, args.repo, args.host, args.port,
                       args.python, args.pythonw, args.startup_dir,
                       proxy_port=args.proxy_port, with_proxy=not args.no_proxy)
    plan = build_plan(cfg)

    print("hermes home : %s" % cfg["hermes_home"])
    print("repo        : %s" % cfg["repo"])
    print("endpoint    : http://%s:%d/mcp" % (cfg["host"], cfg["port"]))
    if cfg.get("with_proxy", True):
        print("proxy       : http://127.0.0.1:%d/v1" % cfg["proxy_port"])
    print("python      : %s" % (cfg["python"] or "(not found - pass --python)"))
    print("pythonw     : %s" % (cfg["pythonw"] or "(not found - pass --pythonw)"))
    print("")

    if args.dry_run or not args.write:
        for path, text in plan:
            state = "overwrite" if os.path.exists(path) else "new"
            print("[%s] %s (%d bytes)" % (state, path, len(text.encode("utf-8"))))
        # 常駐サーバー専用の env は**無いときだけ**作る（--force でも利用者のフラグを消さない）。
        _env_path = cfg["paths"]["service_env"]
        print("[%s] %s (%d bytes)" % ("exists" if os.path.exists(_env_path) else "new",
                                      _env_path, len(render_service_env(cfg).encode("utf-8"))))
        if not args.write:
            print("\n(表示のみ。--write で生成します)")
            return 0

    if args.write:
        for line in write_plan(plan, force=args.force):
            print(line)
        _env_path = cfg["paths"]["service_env"]
        if os.path.exists(_env_path):
            print("skipped (exists): %s" % _env_path)
        else:
            os.makedirs(os.path.dirname(_env_path), exist_ok=True)
            with open(_env_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(render_service_env(cfg))
            print("wrote: %s" % _env_path)
        if args.task:
            print(register_task(cfg, args.task_name, args.interval_minutes))
            if cfg.get("with_proxy", True):
                print(register_proxy_task(cfg, args.proxy_task_name, args.interval_minutes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
