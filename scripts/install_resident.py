"""Windows / Linux / macOS で freeagent-bind を常駐させる「配線」を生成する（ネットワーク不要）。

常駐 HTTP（`--transport http`）は 1 プロセスを複数のクライアントで共有するための経路で、
OS ごとに次の常駐機構へ配線する（実行時依存ゼロ・標準ライブラリのみ）:

| OS (`--platform`) | 判別 (`sys.platform`) | 常駐機構 | 定義ファイルの既定配置先 (`--unit-dir`) |
|---|---|---|---|
| `windows` | `win32` | Startup VBS + `pythonw.exe` + `schtasks` | `%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup` |
| `linux` | `linux*` | `systemd --user` (`Restart=on-failure`) | `~/.config/systemd/user` (`$XDG_CONFIG_HOME/systemd/user`) |
| `macos` | `darwin` | `launchd` LaunchAgent (`KeepAlive.SuccessfulExit=false`) | `~/Library/LaunchAgents` |

Windows 向け生成は既存の `scripts/install_resident_windows.py` と同一の契約・生成物を保つ。
Linux / macOS では:
  1. `<hermes-home>/freeagent-service/run_server.py` が `<hermes-home>/.env` と `freeagent.env` を読み、
     ログを `<hermes-home>/logs/freeagent-autostart.log` へ追記しつつ同一プロセスで HTTP サーバーを動かす。
  2. `<hermes-home>/proxy-service/run_proxy.py` が Nous Portal 資格情報（`auth.json`）を確認し、
     未認証時はログへ 1 行だけ出してポーリング待機、認証済みになり次第 `os.execvpe` で同一 PID のまま
     `hermes proxy start` に置き換わる（無駄なクラッシュ再起動ループを起こさず、認証後は自動復帰する）。

使い方:

```bash
python scripts/install_resident.py                        # 自動判別して表示のみ（1 バイトも書かない）
python scripts/install_resident.py --dry-run              # 書き込むパスだけ表示
python scripts/install_resident.py --write --force        # 配線を生成
python scripts/install_resident.py --write --force --task # サービス登録・起動（schtasks / systemctl / launchctl）も実行
```
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import argparse
import importlib.util
import os
import plistlib
import posixpath
import shutil
import stat
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None)
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

VALID_PLATFORMS = ("windows", "linux", "macos")
PLATFORM_ALIASES = {
    "windows": "windows",
    "win32": "windows",
    "nt": "windows",
    "linux": "linux",
    "macos": "macos",
    "darwin": "macos",
    "mac": "macos",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
DEFAULT_PROXY_PORT = 8645
DEFAULT_INTERVAL_MINUTES = 5

SERVICE_DIR_NAME = "freeagent-service"
PROXY_SERVICE_DIR_NAME = "proxy-service"
LOG_NAME = "freeagent-autostart.log"
PROXY_LOG_NAME = "proxy-service.log"
ENV_NAME = "freeagent.env"

POSIX_LAUNCHER_NAME = "run_server.py"
POSIX_PROXY_LAUNCHER_NAME = "run_proxy.py"

LINUX_UNIT_NAME = "hermes-freeagent-bind.service"
LINUX_PROXY_UNIT_NAME = "hermes-proxy.service"

MACOS_LABEL = "ai.hermes.freeagent-bind"
MACOS_PROXY_LABEL = "ai.hermes.proxy"
MACOS_PLIST_NAME = MACOS_LABEL + ".plist"
MACOS_PROXY_PLIST_NAME = MACOS_PROXY_LABEL + ".plist"
LINUX_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
MACOS_DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# ユーザー領域の bin（launchd / systemd はログインシェルの PATH を引き継がない・SPEC §6.66）。
USER_BIN_DIRS = (".local/bin", "bin", ".hermes/bin")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_windows_installer():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install_resident_windows.py")
    spec = importlib.util.spec_from_file_location("_install_resident_windows", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("install_resident_windows.py を読み込めません: %s" % path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_WIN = _load_windows_installer()
marker_for = _WIN.marker_for
proxy_marker_for = _WIN.proxy_marker_for


def detect_platform(override: str = "auto", sys_platform: str | None = None) -> str:
    """OS 種別を `'windows'` / `'linux'` / `'macos'` に正規化する。

    `override` が空または `'auto'` のときは `sys.platform` で判別する。
    明示された不正値は既定へ丸めず `ValueError` にする。
    """
    raw = (override or "").strip().lower()
    if raw and raw != "auto":
        mapped = PLATFORM_ALIASES.get(raw)
        if not mapped:
            raise ValueError(
                "未対応の --platform 指定です: %r（auto / windows / linux / macos）" % override
            )
        return mapped
    sp = (sys_platform if sys_platform is not None else sys.platform).strip().lower()
    if sp == "win32":
        return "windows"
    if sp == "darwin":
        return "macos"
    if sp.startswith("linux"):
        return "linux"
    raise ValueError("未対応の実行プラットフォームです: %r（windows / linux / macos）" % sp)


def _host_matches_target(platform_name: str) -> bool:
    try:
        return detect_platform("auto") == platform_name
    except ValueError:
        return False


def _target_join(platform_name: str, base: str, *parts: str) -> str:
    if platform_name in ("linux", "macos") and base.startswith("/") and ":" not in base[:3]:
        return posixpath.join(base, *parts)
    return os.path.join(base, *parts)


def _posix_uid() -> int | None:
    fn: Any = getattr(os, "getuid", None)
    if callable(fn):
        res = fn()
        if isinstance(res, int):
            return res
    return None


def _account_home(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    home = (environ.get("HOME") or "").strip()
    if home:
        return home
    uid = _posix_uid()
    if uid is not None:
        try:
            import pwd  # POSIX のみ
            getpwuid: Any = getattr(pwd, "getpwuid", None)
            if callable(getpwuid):
                entry: Any = getpwuid(uid)
                return str(entry.pw_dir)
        except (ImportError, KeyError, OSError):
            pass
    return os.path.expanduser("~")


def platform_path(platform_name: str, home: str, hermes_home: str = "") -> str:
    r"""その OS の `PATH`（ユーザー領域の bin を先頭に足し、重複は落とす）。

    launchd / systemd は**ログインシェルの PATH を引き継がない**。`hermes` CLI の現実的な置き場
    （uv tool / pipx / `pip --user` は `~/.local/bin`）を入れないと、`run_proxy.py` の
    `which("hermes")` が空になり `python -m hermes_cli.main` の受け皿へ落ちる。その受け皿は
    「選んだ python に `hermes_cli` が入っている」ときしか動かないので、外れると 5 秒ごとの
    失敗再起動になる。plist は実行時に `$HOME` を展開しないため、ここで絶対パスへ直す。
    """
    target = detect_platform(platform_name)
    base = MACOS_DEFAULT_PATH if target == "macos" else LINUX_DEFAULT_PATH
    root = home.replace("\\", "/").rstrip("/")
    entries: list[str] = []
    if hermes_home.startswith("/"):
        entries.append(posixpath.join(hermes_home.rstrip("/") or "/", "bin"))
    if root.startswith("/"):
        # クロス生成（Windows ホスト）で home が POSIX パスでないときは、存在しない
        # `/.local/bin` のような項目を作らない（PATH の先頭に嘘を置かない）。
        entries.extend(posixpath.join(root, rel) for rel in USER_BIN_DIRS)
    entries.extend(base.split(":"))
    seen: set[str] = set()
    kept: list[str] = []
    for entry in entries:
        if entry and entry not in seen:
            seen.add(entry)
            kept.append(entry)
    return ":".join(kept)


def hermes_home_default(platform_name: str | None = None,
                        env: Mapping[str, str] | None = None) -> str:
    """プラットフォーム別の HERMES_HOME 既定パス（Windows は `%LOCALAPPDATA%\\hermes`、他は `~/.hermes`）。"""
    target = detect_platform(platform_name or "auto")
    environ = os.environ if env is None else env
    if target == "windows":
        local = environ.get("LOCALAPPDATA") or _account_home(environ)
        return os.path.join(local, "hermes")
    return _target_join(target, _account_home(environ), ".hermes")


def default_unit_dir(platform_name: str, env: Mapping[str, str] | None = None) -> str:
    """OS の自動起動／サービス定義ディレクトリ既定パス。

    テストでは必ず `unit_dir`（または `--unit-dir` / `--startup-dir`）で一時ディレクトリを渡し、
    実機の Startup / `~/.config/systemd/user` / `~/Library/LaunchAgents` を触らないこと。
    """
    environ = os.environ if env is None else env
    if platform_name == "windows":
        base = environ.get("APPDATA") or _account_home(environ)
        return os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    home = _account_home(environ)
    if platform_name == "linux":
        xdg = (environ.get("XDG_CONFIG_HOME") or "").strip()
        cfg_root = xdg if xdg else _target_join("linux", home, ".config")
        return _target_join("linux", cfg_root, "systemd", "user")
    if platform_name == "macos":
        return _target_join("macos", home, "Library", "LaunchAgents")
    raise ValueError("unknown platform: %r" % platform_name)


def _interpreter_ok(exe: str) -> bool:
    return _WIN._interpreter_ok(exe)


def discover_python(hermes_home: str, platform_name: str | None = None) -> tuple[str, str]:
    """`<hermes-home>/tools/python-*`（POSIX は必要なら実行環境の python3 も）からインタプリタを探す。

    返り値は `(python, pythonw)`。POSIX には `pythonw` が無いため第 2 要素は常に `""`。
    """
    target = detect_platform(platform_name or "auto")
    if target == "windows":
        return _WIN.discover_python(hermes_home)

    check_exec = _host_matches_target(target)
    tools = os.path.join(hermes_home, "tools")
    if os.path.isdir(tools):
        candidates = sorted(
            (name for name in os.listdir(tools)
             if name.startswith("python-") and os.path.isdir(os.path.join(tools, name))),
            key=_WIN._python_dir_key,   # 版番号順（文字列順だと 3.14.10 < 3.14.9 になる）
            reverse=True,
        )
        rel_bins = (
            os.path.join("bin", "python3"),
            os.path.join("bin", "python"),
            "python3",
            "python",
        )
        for name in candidates:
            folder = os.path.join(tools, name)
            for rel in rel_bins:
                exe = os.path.join(folder, rel)
                if os.path.isfile(exe) and (not check_exec or _interpreter_ok(exe)):
                    return exe, ""

    # Linux / macOS では tools/ 同梱でなく venv / uv / Homebrew の python3 で動かす構成もある。
    # クロス生成時（例: Windows ホストで --platform linux を試すとき）はホストの python.exe を混ぜない。
    if check_exec:
        for cand in (sys.executable, shutil.which("python3") or "", shutil.which("python") or ""):
            if cand and os.path.isfile(cand) and _interpreter_ok(cand):
                return cand, ""
    return "", ""


def service_paths(hermes_home: str, platform_name: str = "windows", unit_dir: str = "") -> dict:
    """生成物のパス辞書。"""
    target = detect_platform(platform_name)
    if target == "windows":
        return _WIN.service_paths(hermes_home, startup_dir=unit_dir)

    service = _target_join(target, hermes_home, SERVICE_DIR_NAME)
    proxy_service = _target_join(target, hermes_home, PROXY_SERVICE_DIR_NAME)
    target_unit_dir = unit_dir or default_unit_dir(target)
    unit_file = LINUX_UNIT_NAME if target == "linux" else MACOS_PLIST_NAME
    proxy_unit_file = LINUX_PROXY_UNIT_NAME if target == "linux" else MACOS_PROXY_PLIST_NAME
    return {
        "service_dir": service,
        "launcher": _target_join(target, service, POSIX_LAUNCHER_NAME),
        "unit_dir": target_unit_dir,
        "unit": _target_join(target, target_unit_dir, unit_file),
        "startup": _target_join(target, target_unit_dir, unit_file),
        "log": _target_join(target, hermes_home, "logs", LOG_NAME),
        "service_env": _target_join(target, service, ENV_NAME),
        "apply": _target_join(target, service, "apply_freeagent_http.sh"),
        "revert": _target_join(target, service, "revert_freeagent_stdio.sh"),
        "proxy_service_dir": proxy_service,
        "proxy_launcher": _target_join(target, proxy_service, POSIX_PROXY_LAUNCHER_NAME),
        "proxy_unit": _target_join(target, target_unit_dir, proxy_unit_file),
        "proxy_startup": _target_join(target, target_unit_dir, proxy_unit_file),
        "proxy_log": _target_join(target, hermes_home, "logs", PROXY_LOG_NAME),
    }


def render_posix_launcher(cfg: dict) -> str:
    """Linux / macOS 共通の HTTP サーバー起動ランチャ（同一プロセスで実行）。"""
    return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""freeagent-bind の常駐 HTTP サーバーを起動する（systemd / launchd 用）。

常駐サーバーは別プロセスで起動されるため Hermes の MCP `env` が届かない。
起動時に `<hermes-home>/.env` と `freeagent-service/freeagent.env` を読み込み、
ログを `<hermes-home>/logs/__LOG_NAME__` へ追記しながら同一プロセスで待ち受ける
（子プロセスへ fork しないため systemd `Type=simple` / launchd がそのまま PID を監視できる）。
"""
import os
import runpy
import sys

HOME = os.path.expanduser(os.path.expandvars(__HERMES_HOME_REPR__))
REPO = os.path.expanduser(os.path.expandvars(__REPO_REPR__))
LOG = os.path.join(HOME, "logs", __LOG_NAME_REPR__)
PORT = __PORT__
ENV_FILES = [
    os.environ.get("FREEAGENT_ENV_FILE") or os.path.join(HOME, ".env"),
    os.path.join(HOME, __SERVICE_DIR_REPR__, __ENV_NAME_REPR__),
]

os.chdir(REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.makedirs(os.path.dirname(LOG), exist_ok=True)
_stream = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
sys.stdout = _stream
sys.stderr = _stream


def _load_env_file(path):
    """KEY=VALUE を os.environ へ入れる（既にある値は上書きしない）。"""
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


_loaded = []
for _env_path in ENV_FILES:
    _loaded.extend(_load_env_file(_env_path))
print("[env] loaded %d vars: %s" % (len(_loaded), ", ".join(_loaded) or "none"))

sys.argv = ["server.py", "--transport", "http", "--host", __HOST_REPR__, "--port", str(PORT)]
runpy.run_path(os.path.join(REPO, "src", "freeagent_bind", "server.py"), run_name="__main__")
'''.replace("__HERMES_HOME_REPR__", repr(cfg["hermes_home"])).replace(
        "__REPO_REPR__", repr(cfg["repo"])).replace(
        "__LOG_NAME__", LOG_NAME).replace(
        "__LOG_NAME_REPR__", repr(LOG_NAME)).replace(
        "__PORT__", str(cfg["port"])).replace(
        "__HOST_REPR__", repr(cfg["host"])).replace(
        "__SERVICE_DIR_REPR__", repr(SERVICE_DIR_NAME)).replace(
        "__ENV_NAME_REPR__", repr(ENV_NAME))


def render_posix_proxy_launcher(cfg: dict) -> str:
    """Linux / macOS 共通の Nous Portal プロキシ起動ランチャ。

    未認証時はクラッシュ再起動ループを避けつつ、後から `hermes auth add nous` されたら自動復帰できるよう、
    ログに 1 行だけ出して `auth.json` をポーリング待機し、資格情報が揃い次第 `os.execvpe` で同一 PID のまま
    `hermes proxy start` に置換する。
    """
    proxy_port = int(cfg.get("proxy_port", DEFAULT_PROXY_PORT))
    return '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ローカル OpenAI 互換プロキシ（`hermes proxy start`）の POSIX 常駐ランチャ。

systemd (`Type=simple`, `Restart=on-failure`) / launchd (`KeepAlive.SuccessfulExit=false`) で動かす。
1. Nous Portal が未認証（`auth.json` に有効なトークンが無い）のときは即失敗終了せず、
   skipped を 1 行だけ記録して `FREEAGENT_PROXY_AUTH_POLL_S`（既定 60 秒）おきに再確認する。
   これにより無駄な再起動ループを起こさず、利用者がログインした時点で自動的に起動へ移行する。
2. 資格情報が確認できたら `os.execvpe` で同一 PID のままプロキシ本体に置換する（監視 PID が変わらない）。
"""
import json
import os
import shutil
import signal
import sys
import time

HOME = os.path.expanduser(os.path.expandvars(__HERMES_HOME_REPR__))
REPO = os.path.join(HOME, "hermes-agent")
LOG = os.environ.get("HERMES_PROXY_LOG") or os.path.join(HOME, "logs", __PROXY_LOG_NAME_REPR__)
AUTH_FILE = os.path.join(HOME, "auth.json")
DEFAULT_PORT = __PROXY_PORT__
MARKER_PREFIX = "hermes-proxy-"
_STOP = False


def _handle_stop(_signum, _frame):
    global _STOP
    _STOP = True


def _split_argv(argv):
    keep = []
    for arg in argv:
        if arg.startswith(MARKER_PREFIX) and len(arg) > len(MARKER_PREFIX):
            continue
        keep.append(arg)
    return keep


def _has_port_arg(extra_args):
    for arg in extra_args:
        if arg == "--port" or arg.startswith("--port="):
            return True
    return False


def _target_provider(extra_args):
    for i, arg in enumerate(extra_args):
        if arg == "--provider" and i + 1 < len(extra_args):
            return (extra_args[i + 1] or "").strip().lower() or "nous"
        if arg.startswith("--provider="):
            return arg.partition("=")[2].strip().lower() or "nous"
    return "nous"


def _nous_authenticated():
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


def _append_log(msg):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8", errors="replace", buffering=1) as fh:
            fh.write(msg + "\\n")
    except OSError:
        pass


def _wait_or_stop(seconds):
    """`_STOP` が立つまで待つ（SIGTERM で 0.25 秒以内に抜ける）。

    `time.sleep()` は Python 3.5 以降、シグナルハンドラが例外を投げない限り**残り時間を寝続ける**
    （PEP 475）。そのため停止が最大ポーリング間隔（既定 60 秒）ぶん遅れ、`launchctl bootout` の直後の
    `bootstrap` で旧プロセスがポートを掴んだままになる。細切れに sleep して毎回 `_STOP` を見る。
    """
    deadline = time.monotonic() + max(0.0, float(seconds))
    while not _STOP:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.25, remaining))


def _poll_interval():
    raw = (os.environ.get("FREEAGENT_PROXY_AUTH_POLL_S") or "").strip()
    try:
        val = float(raw) if raw else 60.0
    except ValueError:
        val = 60.0
    return max(1.0, min(3600.0, val))


def _candidate_bins():
    """`hermes` CLI の置き場所（plist / unit の PATH に依存せず自前で探す）。"""
    return [
        os.path.join(HOME, "bin", "hermes"),
        os.path.expanduser("~/.local/bin/hermes"),
        os.path.expanduser("~/bin/hermes"),
    ]


def _resolve_command(extra):
    if os.path.isdir(REPO):
        return [sys.executable, "-m", "hermes_cli.main", "proxy", "start", *extra]
    for cand in _candidate_bins():
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return [cand, "proxy", "start", *extra]
    found = shutil.which("hermes") or ""
    if found:
        return [found, "proxy", "start", *extra]
    return [sys.executable, "-m", "hermes_cli.main", "proxy", "start", *extra]


def main(argv=None, max_polls=None):
    global _STOP
    _STOP = False
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _handle_stop)
            except (OSError, ValueError):
                pass

    raw_argv = sys.argv[1:] if argv is None else list(argv)
    extra = _split_argv(raw_argv)
    if DEFAULT_PORT != 8645 and not _has_port_arg(extra):
        extra = ["--port", str(DEFAULT_PORT), *extra]
    provider = _target_provider(extra)
    force_start = os.environ.get("HERMES_PROXY_FORCE_START", "").strip().lower() in ("1", "true", "yes", "on")

    polls = 0
    if not force_start and provider == "nous":
        while not _nous_authenticated():
            skip_msg = "[launcher] skipped: Nous Portal not authenticated (run `hermes auth add nous` to enable proxy)"
            if _last_log_line(LOG) != skip_msg:
                _append_log(skip_msg)
            if _STOP or (max_polls is not None and polls >= max_polls):
                return 0
            polls += 1
            _wait_or_stop(_poll_interval())
            if _STOP:
                return 0

    env = dict(os.environ)
    env.setdefault("HERMES_HOME", HOME)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    if os.path.isdir(REPO):
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = REPO + (os.pathsep + existing if existing else "")

    cmd = _resolve_command(extra)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8", errors="replace", buffering=1) as log_fh:
        log_fh.write("[launcher] exec pid=%d cmd=%s\\n" % (os.getpid(), " ".join(cmd)))
        log_fh.flush()
        os.dup2(log_fh.fileno(), 1)
        os.dup2(log_fh.fileno(), 2)
    if os.path.isdir(HOME):
        os.chdir(HOME)
    os.execvpe(cmd[0], cmd, env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''.replace("__HERMES_HOME_REPR__", repr(cfg["hermes_home"])).replace(
        "__PROXY_LOG_NAME_REPR__", repr(PROXY_LOG_NAME)).replace(
        "__PROXY_PORT__", str(proxy_port))


def _systemd_quote(value: str) -> str:
    """systemd unit ファイルの引数・環境変数（`ExecStart=` / `Environment=`）用にダブルクォートで囲む。

    `%` は systemd が specifier（`%h` 等）として先に展開するため、`%%` でリテラル化する。
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace("%", "%%")
        .replace('"', '\\"')
    )
    return '"%s"' % escaped


def _systemd_path(value: str) -> str:
    r"""systemd unit ファイルのパス設定（`WorkingDirectory=` 等）用にエスケープする。

    `WorkingDirectory=` は二重引用符を解釈しない（`WorkingDirectory="/home/..."` は
    先頭が `"` の相対パスと見なされ、`WorkingDirectory= path is not absolute` の
    `bad-setting` で起動が拒否される・実測 Issue #10）。引用符は付けず、空白や制御文字は
    C 形式（`\x20` 等）、`%` は specifier 展開防止のため `%%` に逃がす。
    """
    return (
        value.replace("\\", "\\\\")
        .replace("%", "%%")
        .replace(" ", "\\x20")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
    )


def _exec_args_for(cfg: dict, launcher_path: str, marker: str) -> list[str]:
    py = cfg.get("python") or ""
    if py:
        return [py, launcher_path, marker]
    return ["/usr/bin/env", "python3", launcher_path, marker]


def render_linux_unit(cfg: dict) -> str:
    """Linux `systemd --user` ユニット（freeagent-bind HTTP サーバー）。"""
    args = _exec_args_for(cfg, cfg["paths"]["launcher"], cfg["marker"])
    exec_start = " ".join(_systemd_quote(a) for a in args)
    env_home = _systemd_quote("HERMES_HOME=%s" % cfg["hermes_home"])
    env_path = _systemd_quote("PATH=%s" % (cfg.get("path") or LINUX_DEFAULT_PATH))
    return """# Hermes Agent - freeagent-bind resident MCP HTTP server (systemd --user)
# Generated by scripts/install_resident.py
[Unit]
Description=Hermes freeagent-bind resident MCP server (%s:%d)
After=network.target

[Service]
Type=simple
WorkingDirectory=%s
Environment=%s
Environment=%s
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONUTF8=1"
ExecStart=%s
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""" % (
        cfg["host"],
        cfg["port"],
        _systemd_path(cfg["repo"]),
        env_home,
        env_path,
        exec_start,
    )


def render_linux_proxy_unit(cfg: dict) -> str:
    """Linux `systemd --user` ユニット（Nous Portal ローカルプロキシ）。"""
    proxy_port = int(cfg.get("proxy_port", DEFAULT_PROXY_PORT))
    args = _exec_args_for(cfg, cfg["paths"]["proxy_launcher"], cfg["proxy_marker"])
    exec_start = " ".join(_systemd_quote(a) for a in args)
    env_home = _systemd_quote("HERMES_HOME=%s" % cfg["hermes_home"])
    env_path = _systemd_quote("PATH=%s" % (cfg.get("path") or LINUX_DEFAULT_PATH))
    return """# Hermes Agent - local OpenAI-compatible proxy (systemd --user)
# Generated by scripts/install_resident.py
[Unit]
Description=Hermes local OpenAI-compatible proxy (127.0.0.1:%d)
After=network.target

[Service]
Type=simple
WorkingDirectory=%s
Environment=%s
Environment=%s
Environment="PYTHONUNBUFFERED=1"
Environment="PYTHONUTF8=1"
ExecStart=%s
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""" % (
        proxy_port,
        _systemd_path(cfg["hermes_home"]),
        env_home,
        env_path,
        exec_start,
    )


def render_macos_plist(cfg: dict) -> str:
    """macOS `launchd` LaunchAgent plist（freeagent-bind HTTP サーバー）。"""
    payload = {
        "Label": cfg.get("label") or MACOS_LABEL,
        "ProgramArguments": _exec_args_for(cfg, cfg["paths"]["launcher"], cfg["marker"]),
        "WorkingDirectory": cfg["repo"],
        "EnvironmentVariables": {
            "HERMES_HOME": cfg["hermes_home"],
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PATH": cfg.get("path") or MACOS_DEFAULT_PATH,
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": cfg["paths"]["log"],
        "StandardErrorPath": cfg["paths"]["log"],
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def render_macos_proxy_plist(cfg: dict) -> str:
    """macOS `launchd` LaunchAgent plist（Nous Portal ローカルプロキシ）。"""
    payload = {
        "Label": cfg.get("proxy_label") or MACOS_PROXY_LABEL,
        "ProgramArguments": _exec_args_for(cfg, cfg["paths"]["proxy_launcher"], cfg["proxy_marker"]),
        "WorkingDirectory": cfg["hermes_home"],
        "EnvironmentVariables": {
            "HERMES_HOME": cfg["hermes_home"],
            "PYTHONUNBUFFERED": "1",
            "PYTHONUTF8": "1",
            "PATH": cfg.get("path") or MACOS_DEFAULT_PATH,
        },
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": cfg["paths"]["proxy_log"],
        "StandardErrorPath": cfg["paths"]["proxy_log"],
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True).decode("utf-8")


def render_posix_apply(cfg: dict) -> str:
    """`hermes config` を常駐 HTTP へ切り替える（Linux / macOS 用案内コメントつき）。"""
    if cfg["platform"] == "linux":
        hint = "systemctl --user start %s" % LINUX_UNIT_NAME
    else:
        hint = 'launchctl kickstart -k "gui/$(id -u)/%s"' % (cfg.get("label") or MACOS_LABEL)
    return """#!/usr/bin/env bash
# freeagent-bind を常駐 HTTP（__HOST__:__PORT__ を全プロセスで共有）に切り替える。
# 先に常駐サーバーを起動: __START_HINT__
set -e
hermes config unset mcp_servers.freeagent-bind.command
hermes config unset mcp_servers.freeagent-bind.args
hermes config unset mcp_servers.freeagent-bind.env
hermes config set mcp_servers.freeagent-bind.url http://__HOST__:__PORT__/mcp
hermes config set mcp_servers.freeagent-bind.headers.x-freeagent-harness hermes
echo '--- 現在の設定 ---' && hermes config get mcp_servers.freeagent-bind
echo '--- 接続テスト ---' && hermes mcp test freeagent-bind
""".replace("__HOST__", cfg["host"]).replace("__PORT__", str(cfg["port"])).replace(
        "__START_HINT__", hint)


def render_posix_revert(cfg: dict) -> str:
    """stdio へ戻す（Linux / macOS 用停止コメントつき）。"""
    py = cfg.get("python") or "python3"
    server_py = os.path.join(cfg["repo"], "src", "freeagent_bind", "server.py").replace("\\", "/")
    if cfg["platform"] == "linux":
        stop_hint = "systemctl --user disable --now %s" % LINUX_UNIT_NAME
    else:
        stop_hint = 'launchctl bootout "gui/$(id -u)" "%s"' % cfg["paths"]["unit"]
    return """#!/usr/bin/env bash
# freeagent-bind を stdio（1 セッション 1 プロセス）に戻す。
# 常駐サーバーを止めるなら: __STOP_HINT__
set -e
hermes config unset mcp_servers.freeagent-bind.url
hermes config unset mcp_servers.freeagent-bind.headers
hermes config set mcp_servers.freeagent-bind.command '__PYTHON__'
hermes config set mcp_servers.freeagent-bind.args '["__SERVER_PY__"]'
hermes config set mcp_servers.freeagent-bind.env.FREEAGENT_HARNESS hermes
echo '--- 現在の設定 ---' && hermes config get mcp_servers.freeagent-bind
echo '--- 接続テスト ---' && hermes mcp test freeagent-bind
""".replace("__PYTHON__", py).replace("__SERVER_PY__", server_py).replace(
        "__STOP_HINT__", stop_hint)


def render_service_env(cfg: dict) -> str:
    """常駐サーバー専用の環境変数ファイルの雛形（無いときだけ作る）。"""
    if cfg.get("platform") == "windows":
        return _WIN.render_service_env(cfg)
    return """# freeagent-bind 常駐サーバー専用の環境変数（このファイルは常駐プロセスだけが読みます）。
# 生成: scripts/install_resident.py（**既存ファイルは上書きしません**）。
#
# launcher（run_server.py）が起動時に読む順序:
#   1. ~/.hermes/.env                   … Hermes のキー（OPENROUTER_API_KEY / NVIDIA_API_KEY /
#                                          HF_TOKEN / GEMINI_API_KEY / OLLAMA_API_KEY など）
#   2. このファイル                      … 常駐サーバー固有の設定・確認フラグ
# すでに環境にある値は上書きしません（launcher は名前だけをログに出します。値は出ません）。
# 反映には常駐サーバーの再起動が要ります。
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
                 with_proxy: bool = True,
                 platform_name: str = "auto") -> dict:
    """生成に必要な値一式を組み立てる（`startup_dir` は全 OS 共通のユニット出力先上書きとして扱う）。"""
    target = detect_platform(platform_name)
    if target == "windows":
        cfg = _WIN.build_config(
            hermes_home, repo, host, port,
            python=python, pythonw=pythonw, startup_dir=startup_dir,
            proxy_port=proxy_port, with_proxy=with_proxy,
        )
        cfg["platform"] = "windows"
        return cfg

    host_match = _host_matches_target(target)
    if host_match:
        hermes_home = os.path.abspath(os.path.expanduser(hermes_home))
        repo = os.path.abspath(os.path.expanduser(repo))
        if startup_dir:
            startup_dir = os.path.abspath(os.path.expanduser(startup_dir))
    paths = service_paths(hermes_home, platform_name=target, unit_dir=startup_dir)
    # クロス生成（Windows ホストで --platform macos 等）はホストの HOME を混ぜない。
    account_home = (_account_home() if host_match
                    else posixpath.dirname(hermes_home.replace("\\", "/").rstrip("/")))
    found_py, _ = ("", "") if python else discover_python(hermes_home, platform_name=target)
    return {
        "platform": target,
        "hermes_home": hermes_home,
        "repo": repo,
        "host": host,
        "port": port,
        "proxy_port": proxy_port,
        "with_proxy": with_proxy,
        "python": python or found_py,
        "pythonw": "",
        "marker": marker_for(port),
        "proxy_marker": proxy_marker_for(proxy_port),
        "label": MACOS_LABEL,
        "proxy_label": MACOS_PROXY_LABEL,
        "path": platform_path(target, account_home, hermes_home),
        "paths": paths,
    }


def build_plan(cfg: dict) -> list[tuple[str, str]]:
    """`(出力パス, 内容)` の一覧（書き込みはしない）。"""
    target = cfg.get("platform") or "windows"
    if target == "windows":
        return _WIN.build_plan(cfg)

    p = cfg["paths"]
    unit_renderer = render_linux_unit if target == "linux" else render_macos_plist
    proxy_unit_renderer = render_linux_proxy_unit if target == "linux" else render_macos_proxy_plist
    plan = [
        (p["launcher"], render_posix_launcher(cfg)),
        (p["unit"], unit_renderer(cfg)),
        (p["apply"], render_posix_apply(cfg)),
        (p["revert"], render_posix_revert(cfg)),
    ]
    if cfg.get("with_proxy", True):
        plan.extend([
            (p["proxy_launcher"], render_posix_proxy_launcher(cfg)),
            (p["proxy_unit"], proxy_unit_renderer(cfg)),
        ])
    return plan


def _is_executable_artifact(path: str) -> bool:
    lower = path.lower()
    if lower.endswith(".sh"):
        return True
    base = os.path.basename(lower)
    return base in (POSIX_LAUNCHER_NAME, POSIX_PROXY_LAUNCHER_NAME)


def write_plan(plan: list[tuple[str, str]], force: bool = False) -> list[str]:
    """生成物を書く（`.vbs` は ASCII+CRLF、それ以外は UTF-8+LF、POSIX スクリプトには実行ビットを付与）。"""
    written: list[str] = []
    for path, text in plan:
        if os.path.exists(path) and not force:
            written.append("skipped (exists): %s" % path)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        is_vbs = path.lower().endswith(".vbs")
        newline = "\r\n" if is_vbs else "\n"
        encoding = "ascii" if is_vbs else "utf-8"
        body = text if text.endswith("\n") else text + "\n"
        with open(path, "w", encoding=encoding, newline=newline) as fh:
            fh.write(body)
        if not is_vbs and _is_executable_artifact(path):
            try:
                mode = os.stat(path).st_mode
                os.chmod(path, mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except OSError:
                pass
        written.append("wrote: %s" % path)
    return written


def ensure_runtime_dirs(cfg: dict) -> list[str]:
    """常駐機構が**起動前**に開くディレクトリを先に作る。

    launchd は `StandardOutPath` / `StandardErrorPath` を exec の前に開く。ファイルは作るが
    **中間ディレクトリは作らない**ため、`<hermes-home>/logs` が無いとジョブは起動せず
    `last exit status = 78` で黙って終わる（`bootstrap` の rc は 0 のまま）。systemd 側も
    launcher が自分で作るので害はない。Windows の VBS は自分で `logs` を作っている。
    """
    target = cfg.get("platform") or "windows"
    if target == "windows":
        return []
    log_dir = os.path.dirname(cfg["paths"]["log"])
    if not log_dir or os.path.isdir(log_dir):
        return []
    os.makedirs(log_dir, exist_ok=True)
    return ["created dir: %s" % log_dir]


def verify_generated(cfg: dict) -> list[str]:
    """生成物が「常駐機構の起動前検査」を通るかを、書いた直後に自分で確かめる。

    ここで落ちる条件は、そのまま `last exit status = 78`（macOS）/ `status=200/CHDIR`（Linux）として
    **無言**で現れる。クロス生成（ホストと対象 OS が違う）では検査しない。
    """
    target = cfg.get("platform") or "windows"
    if target == "windows" or not _host_matches_target(target):
        return []
    keys = ["log"] + (["proxy_log"] if cfg.get("with_proxy", True) else [])
    files = ["launcher", "unit"] + (["proxy_launcher", "proxy_unit"] if cfg.get("with_proxy", True) else [])
    problems: list[str] = []
    for key in keys:
        path = cfg["paths"].get(key) or ""
        parent = os.path.dirname(path)
        if path and not os.path.isdir(parent):
            problems.append(
                "%s の親ディレクトリがありません（常駐機構が起動前に開けず失敗します）: %s" % (key, parent))
    for key in files:
        path = cfg["paths"].get(key) or ""
        if path and not os.path.isfile(path):
            problems.append("%s が生成されていません: %s" % (key, path))
    py = cfg.get("python") or ""
    if py:
        if not os.path.isabs(py):
            problems.append("python が絶対パスではありません（launchd / systemd は PATH 検索をしません）: %s" % py)
        elif not os.path.isfile(py):
            problems.append("python が見つかりません: %s" % py)
    elif not (shutil.which("python3") or shutil.which("python")):
        problems.append(
            "python3 が見つかりません（ユニット / plist は /usr/bin/env python3 に落ち、起動に失敗します）: "
            "--python で明示してください")
    return problems


def detect_linux_service_ready(systemd_dir: str = "/run/systemd/system",
                               env: Mapping[str, str] | None = None) -> tuple[bool, str, dict[str, str]]:
    """Linux で `systemctl --user` が到達可能か検査し、`(ok, detail, run_env)` を返す。"""
    environ = dict(os.environ if env is None else env)
    if not os.path.isdir(systemd_dir) or not shutil.which("systemctl"):
        return (
            False,
            "systemd が有効ではありません（WSL なら /etc/wsl.conf の [boot] systemd=true を確認してください）",
            environ,
        )
    uid = _posix_uid()
    if uid is not None and not environ.get("XDG_RUNTIME_DIR"):
        runtime_dir = "/run/user/%d" % uid
        if os.path.isdir(runtime_dir):
            environ["XDG_RUNTIME_DIR"] = runtime_dir
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    proc = subprocess.run(
        ["systemctl", "--user", "show-environment"],
        capture_output=True, text=True, timeout=10, env=environ, creationflags=nw,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return (
            False,
            "systemctl --user に接続できません（loginctl enable-linger $USER を確認してください）: %s" % detail,
            environ,
        )
    return True, "systemd-user", environ


def _linux_linger_enabled(linger_dir: str = "/var/lib/systemd/linger",
                          env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    user = (environ.get("USER") or environ.get("LOGNAME") or "").strip()
    if not user:
        uid = _posix_uid()
        if uid is not None:
            try:
                import pwd
                getpwuid: Any = getattr(pwd, "getpwuid", None)
                if callable(getpwuid):
                    entry: Any = getpwuid(uid)
                    user = str(entry.pw_name)
            except (ImportError, KeyError, OSError):
                return False
    return bool(user) and os.path.exists(os.path.join(linger_dir, user))


def register_linux_units(cfg: dict, systemd_dir: str = "/run/systemd/system",
                         linger_dir: str = "/var/lib/systemd/linger") -> list[str]:
    """`systemctl --user daemon-reload` と `enable --now` を実行する。"""
    ok, reason, run_env = detect_linux_service_ready(systemd_dir=systemd_dir)
    if not ok:
        return ["service failed: %s" % reason]
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    reload_proc = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True, timeout=15, env=run_env, creationflags=nw,
    )
    if reload_proc.returncode != 0:
        detail = (reload_proc.stderr or reload_proc.stdout or "").strip()
        return ["service failed (daemon-reload %d): %s" % (reload_proc.returncode, detail)]

    units = [os.path.basename(cfg["paths"]["unit"])]
    if cfg.get("with_proxy", True):
        units.append(os.path.basename(cfg["paths"]["proxy_unit"]))
    results: list[str] = []
    for unit in units:
        proc = subprocess.run(
            ["systemctl", "--user", "enable", "--now", unit],
            capture_output=True, text=True, timeout=20, env=run_env, creationflags=nw,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            results.append("service failed (%s -> %d): %s" % (unit, proc.returncode, detail))
        else:
            results.append("service enabled: %s (systemd --user)" % unit)
    if not _linux_linger_enabled(linger_dir=linger_dir):
        results.append(
            "note: loginctl enable-linger $USER is not enabled; user services stop after logout/SSH disconnect"
        )
    return results


def detect_macos_service_ready() -> tuple[bool, str, str]:
    """macOS で `launchctl` と GUI ドメインが利用可能か**実際に叩いて**検査する。

    Linux 側は `systemctl --user show-environment` でユーザーバスへの到達を確かめる。macOS で
    `launchctl` の有無と UID だけを見ると、SSH / 非 GUI セッションで `gui/<uid>` へ bootstrap できず
    `Input/output error` になるのを手前で弾けない（Linux の linger 案内に相当する情報も出せない）。
    """
    if not shutil.which("launchctl"):
        return False, "launchctl が見つかりません", ""
    uid = _posix_uid()
    if uid is None or uid < 0:
        return False, "POSIX UID を取得できません", ""
    domain = "gui/%d" % uid
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.run(
            ["launchctl", "print", domain],
            capture_output=True, text=True, timeout=10, creationflags=nw,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "launchctl print %s が失敗しました: %s" % (domain, exc), ""
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return (
            False,
            "launchd の %s ドメインに到達できません（GUI ログインセッションで実行してください。"
            "SSH 越しなら、そのユーザーで GUI へログインしてから再実行します）: %s"
            % (domain, detail[0] if detail else "rc=%d" % proc.returncode),
            "",
        )
    return True, "launchd", domain


def _parse_launchctl_print(text: str) -> tuple[str, int | None]:
    """`launchctl print` の出力から `state` と `last exit status` を取り出す。"""
    state = ""
    exit_status: int | None = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            state = stripped.partition("=")[2].strip()
        elif stripped.startswith("last exit status ="):
            raw = stripped.partition("=")[2].strip()
            try:
                exit_status = int(raw, 0)
            except ValueError:
                exit_status = None
    return state, exit_status


def register_macos_agents(cfg: dict) -> list[str]:
    """`launchctl bootout`（既存掃除）→ `bootstrap` → `kickstart -k` で LaunchAgent を有効化する。"""
    ok, reason, domain = detect_macos_service_ready()
    if not ok:
        return ["service failed: %s" % reason]
    nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    items = [(cfg.get("label") or MACOS_LABEL, cfg["paths"]["unit"])]
    if cfg.get("with_proxy", True):
        items.append((cfg.get("proxy_label") or MACOS_PROXY_LABEL, cfg["paths"]["proxy_unit"]))

    results: list[str] = []
    for label, plist_path in items:
        target = "%s/%s" % (domain, label)
        subprocess.run(
            ["launchctl", "bootout", target],
            capture_output=True, text=True, timeout=10, creationflags=nw,
        )
        boot = subprocess.run(
            ["launchctl", "bootstrap", domain, plist_path],
            capture_output=True, text=True, timeout=15, creationflags=nw,
        )
        if boot.returncode != 0:
            detail = (boot.stderr or boot.stdout or "").strip()
            results.append("service failed (%s bootstrap %d): %s" % (label, boot.returncode, detail))
            continue
        kick = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True, text=True, timeout=15, creationflags=nw,
        )
        if kick.returncode != 0:
            detail = (kick.stderr or kick.stdout or "").strip()
            results.append("service failed (%s kickstart %d): %s" % (label, kick.returncode, detail))
            continue
        # `bootstrap` の rc 0 は「ジョブを登録した」だけで、起動可否は別。起動前の失敗
        # （ログの親ディレクトリが無い・WorkingDirectory が無い・実行ファイルが無い）は
        # exit status 78 で黙って終わるので、`launchctl print` で状態を見る（SPEC §6.66）。
        state, exit_status, print_err = "", None, ""
        for attempt in range(3):
            printed = subprocess.run(
                ["launchctl", "print", target],
                capture_output=True, text=True, timeout=15, creationflags=nw,
            )
            state, exit_status = _parse_launchctl_print(printed.stdout or "")
            if printed.returncode != 0 and not state:
                detail = (printed.stderr or printed.stdout or "").strip()
                print_err = detail or ("rc=%d（出力なし）" % printed.returncode)
                break
            if exit_status not in (None, 0) or state == "running":
                break
            if attempt < 2:
                time.sleep(1)
        if print_err:
            results.append("service failed (%s: launchctl print に失敗): %s" % (label, print_err))
            continue
        if state == "running":
            # 生きているなら過去の `last exit status`（前回のクラッシュ）より現在を優先する。
            results.append("service bootstrapped: %s (%s)" % (label, domain))
            continue
        if exit_status not in (None, 0):
            results.append(
                "service failed (%s: last exit status = %d。ログの親ディレクトリ・WorkingDirectory・"
                "実行ファイルを確認してください): launchctl print %s" % (label, exit_status, target))
            continue
        if state:
            results.append("service started: %s (%s・state = %s。running になるまで数秒かかることがあります)"
                           % (label, domain, state))
            continue
        results.append("service bootstrapped: %s (%s)" % (label, domain))
    return results


def register_services(cfg: dict, task_name: str = _WIN.DEFAULT_TASK_NAME,
                      proxy_task_name: str = _WIN.DEFAULT_PROXY_TASK_NAME,
                      interval_minutes: int = DEFAULT_INTERVAL_MINUTES) -> list[str]:
    """プラットフォーム別の常駐登録を実行する。"""
    target = cfg.get("platform") or "windows"
    if not _host_matches_target(target):
        return [
            "service skipped: host platform (%s) does not match target (%s)"
            % (sys.platform, target)
        ]
    if target == "windows":
        out = [_WIN.register_task(cfg, task_name, interval_minutes)]
        if cfg.get("with_proxy", True):
            out.append(_WIN.register_proxy_task(cfg, proxy_task_name, interval_minutes))
        return out
    if target == "linux":
        return register_linux_units(cfg)
    if target == "macos":
        return register_macos_agents(cfg)
    raise ValueError("unknown platform: %r" % target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="freeagent-bind の常駐配線を生成する（Windows / Linux / macOS 対応）"
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("FREEAGENT_RESIDENT_PLATFORM", "auto"),
        help="対象 OS: auto（既定・sys.platform で判別）/ windows / linux / macos",
    )
    parser.add_argument(
        "--hermes-home",
        default="",
        help="既定: Windows は %%LOCALAPPDATA%%\\hermes、Linux / macOS は ~/.hermes",
    )
    parser.add_argument("--repo", default=REPO_ROOT, help="freeagent-bind のリポジトリ根")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--proxy-port", type=int, default=DEFAULT_PROXY_PORT)
    parser.add_argument(
        "--no-proxy", action="store_true",
        help="proxy-service 側の配線生成・サービス登録を行わない",
    )
    parser.add_argument("--python", default="", help="Python 実行ファイルを明示")
    parser.add_argument("--pythonw", default="", help="Windows 用 pythonw.exe を明示")
    parser.add_argument("--write", action="store_true", help="生成する（既定は表示のみ）")
    parser.add_argument("--force", action="store_true", help="既存ファイルを上書きする")
    parser.add_argument("--dry-run", action="store_true", help="書き込むパスだけ表示する")
    parser.add_argument(
        "--task", "--enable", dest="task", action="store_true",
        help="常駐サービスの登録・起動も行う（Windows: schtasks / Linux: systemctl --user / macOS: launchctl）",
    )
    parser.add_argument(
        "--unit-dir", "--startup-dir", dest="unit_dir",
        default=(os.environ.get("FREEAGENT_UNIT_DIR")
                 or os.environ.get("FREEAGENT_STARTUP_DIR")
                 or ""),
        help="ユニット / Startup の出力先ディレクトリ（テストは必ず一時ディレクトリを渡す）",
    )
    parser.add_argument("--task-name", default=_WIN.DEFAULT_TASK_NAME)
    parser.add_argument("--proxy-task-name", default=_WIN.DEFAULT_PROXY_TASK_NAME)
    parser.add_argument("--interval-minutes", type=int, default=DEFAULT_INTERVAL_MINUTES)
    args = parser.parse_args(argv)

    try:
        target = detect_platform(args.platform)
    except ValueError as exc:
        sys.stderr.write("[install_resident] %s\n" % exc)
        return 2

    hermes_home = args.hermes_home or hermes_home_default(target)
    cfg = build_config(
        hermes_home, args.repo, args.host, args.port,
        python=args.python, pythonw=args.pythonw, startup_dir=args.unit_dir,
        proxy_port=args.proxy_port, with_proxy=not args.no_proxy,
        platform_name=target,
    )
    plan = build_plan(cfg)

    print("platform    : %s" % cfg["platform"])
    print("hermes home : %s" % cfg["hermes_home"])
    print("repo        : %s" % cfg["repo"])
    print("endpoint    : http://%s:%d/mcp" % (cfg["host"], cfg["port"]))
    if cfg.get("with_proxy", True):
        print("proxy       : http://127.0.0.1:%d/v1" % cfg["proxy_port"])
    print("python      : %s" % (cfg["python"] or "(not found - fallback /usr/bin/env python3 or pass --python)"))
    if target == "windows":
        print("pythonw     : %s" % (cfg["pythonw"] or "(not found - pass --pythonw)"))
    print("")

    if args.dry_run or not args.write:
        for path, text in plan:
            state = "overwrite" if os.path.exists(path) else "new"
            print("[%s] %s (%d bytes)" % (state, path, len(text.encode("utf-8"))))
        _env_path = cfg["paths"]["service_env"]
        print("[%s] %s (%d bytes)" % (
            "exists" if os.path.exists(_env_path) else "new",
            _env_path,
            len(render_service_env(cfg).encode("utf-8")),
        ))
        _log_dir = os.path.dirname(cfg["paths"]["log"])
        if cfg["platform"] != "windows" and _log_dir:
            print("[%s] %s (dir・常駐機構が起動前に開く)" % (
                "exists" if os.path.isdir(_log_dir) else "new", _log_dir))
        if not args.write:
            print("\n(表示のみ。--write で生成します)")
            return 0

    if args.write:
        for line in ensure_runtime_dirs(cfg):
            print(line)
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
        problems = verify_generated(cfg)
        for problem in problems:
            print("error: %s" % problem)
        if problems:
            # 壊れた定義を登録すると「登録成功・起動失敗」の無言状態を作るので、ここで止める。
            print("(生成物が常駐機構の起動前検査を通らないため、--task は実行していません)")
            return 1
        if args.task:
            lines = register_services(
                cfg,
                task_name=args.task_name,
                proxy_task_name=args.proxy_task_name,
                interval_minutes=args.interval_minutes,
            )
            for line in lines:
                print(line)
            if any(line.startswith("service failed") or line.startswith("task failed") for line in lines):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
