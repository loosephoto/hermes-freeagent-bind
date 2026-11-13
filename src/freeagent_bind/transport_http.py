"""HTTP (StreamableHTTP) トランスポート

`server.py` の § から機械的に切り出した（**共有ヘルパーは `S.<name>` の属性参照**で呼ぶ。
テストが `patch.object(server, ...)` で差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

from .jsonutil import dumps as _dumps
import json
import os
import sys
import threading
import urllib.request

from . import server as S  # noqa: E402

# ================================================================ §10 HTTP (StreamableHTTP)

# 既定は stdio（1 クライアント 1 プロセス）。HTTP モードでは **1 プロセスを複数のクライアントが共有**する。
# 共有されるのは、いままで「プロセスの数だけ複製されていた」もの:
#   * `_KB_CACHE` / `_KB_INFLIGHT` … 同じ問いはキャッシュに当たり、**同時実行は 1 回の上流取得に畳まれる**
#   * `_KB_RATE_NEXT` … ホスト単位のアクセス間隔（規約 28 が警告する「複数 MCP による同一 IP の連打」を防ぐ）
#   * `_MODELS_CACHE`（600 秒）・`_COOLDOWN`・`model_stats`・`_ModelClaims`
#   * `PROVIDER_GATE` … 推論先ごとの同時送信枠は共有。MAX_WORKERS は呼び出し単位の制限
# stdio を N 本立てるとこれらが N 分割され、レート制限も統計も守られない（どちらもプロセス内ロック）。
#
# 実装は**標準ライブラリのみ**（依存ゼロは維持）。仕様の範囲は、Hermes の MCP クライアント
# （`mcp.client.streamable_http`）が実際に要求する経路に絞る。実測で確かめた要求:
#   * POST … 単一メッセージには **JSON オブジェクト 1 個**で応答する（配列で返すとクライアントは
#     `Failed to parse JSON response` で切る）。batch のときだけ配列
#   * POST … 通知（id なし）には **202**（本文なし）。要求に 202 を返すとクライアントは失敗にする
#   * initialize の応答に `mcp-session-id` を付けると、以降クライアントが同じヘッダを返す
#   * GET … `Accept: text/event-stream` でサーバー起点の通知（§8.6 の警告）を流す。セッション必須
#   * DELETE … セッション終了（204）
# 認証は `FREEAGENT_HTTP_TOKEN` を設定したときだけ要求する（既定は 127.0.0.1 のみに束縛）。


_HTTP_SESSIONS: dict = {}
_HTTP_SESSIONS_LOCK = threading.Lock()


def http_config(argv=None) -> dict:
    """HTTP/stdio の設定を env と argv から決める（argv が勝つ）。

    未知の引数は**エラーにしない**（古いクライアントが余分な引数を渡しても起動を止めない）。stderr に
    1 行だけ出して続行する。既定は従来どおり stdio。
    """
    import argparse
    env_transport = (os.environ.get(S.HTTP_TRANSPORT_ENV) or "stdio").strip().lower()
    parser = argparse.ArgumentParser(prog=S.SERVER_NAME, add_help=True,
                                     description="Free モデルをサブ LLM として並列に走らせる MCP サーバー")
    parser.add_argument("--transport", choices=("stdio", "http"), default=env_transport or "stdio",
                        help=f"stdio（既定・1 クライアント 1 プロセス）か http（{S.HTTP_TRANSPORT_ENV}）")
    parser.add_argument("--host", default=os.environ.get(S.HTTP_HOST_ENV, S.HTTP_DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=S._env_int(S.HTTP_PORT_ENV, S.HTTP_DEFAULT_PORT))
    parser.add_argument("--path", default=os.environ.get(S.HTTP_PATH_ENV, S.HTTP_DEFAULT_PATH))
    parser.add_argument("--token", default=os.environ.get(S.HTTP_TOKEN_ENV, ""))
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        try:
            sys.stderr.write(f"[{S.SERVER_NAME}] 未知の引数を無視します: {unknown}\n")
            sys.stderr.flush()
        except Exception:
            pass
    path = args.path if args.path.startswith("/") else "/" + args.path
    return {"transport": args.transport, "host": args.host, "port": args.port,
            "path": path.rstrip("/") or S.HTTP_DEFAULT_PATH, "token": args.token or ""}


def _http_session_new() -> str:
    sid = os.urandom(16).hex()
    with _HTTP_SESSIONS_LOCK:
        if len(_HTTP_SESSIONS) >= S.HTTP_MAX_SESSIONS:
            _http_sessions_prune_locked(force=True)
        _HTTP_SESSIONS[sid] = {"created": S.now_ts(), "seen": S.now_ts(),
                               "queue": [], "cv": threading.Condition(), "closed": False}
    return sid


def _http_session_get(sid: str):
    if not sid:
        return None
    with _HTTP_SESSIONS_LOCK:
        session = _HTTP_SESSIONS.get(sid)
        if session is not None:
            session["seen"] = S.now_ts()
        return session


def _http_session_touch(session) -> None:
    if session is not None:
        session["seen"] = S.now_ts()


def _http_sessions_prune_locked(force: bool = False) -> int:
    """呼び出し側が `_HTTP_SESSIONS_LOCK` を保持した状態で期限切れセッションを落とす。"""
    now = S.now_ts()
    stale = [k for k, v in _HTTP_SESSIONS.items() if now - S.as_float(v.get("seen"), 0.0) > S.HTTP_SESSION_TTL]
    if not stale and force:
        order = sorted(_HTTP_SESSIONS, key=lambda k: S.as_float(_HTTP_SESSIONS[k].get("seen"), 0.0))
        stale = order[:max(1, S.HTTP_MAX_SESSIONS // 4)]
    for k in stale:
        session = _HTTP_SESSIONS.pop(k, None)
        if session is None:
            continue
        session["closed"] = True
        with session["cv"]:
            session["cv"].notify_all()
    return len(stale)


def _http_sessions_prune(force: bool = False) -> int:
    """期限切れのセッションを落とす。上限に達したら古い順に落とす（force）。"""
    with _HTTP_SESSIONS_LOCK:
        return _http_sessions_prune_locked(force)


def _http_session_drop(sid: str) -> bool:
    with _HTTP_SESSIONS_LOCK:
        session = _HTTP_SESSIONS.pop(sid, None)
    if session is None:
        return False
    session["closed"] = True
    with session["cv"]:
        session["cv"].notify_all()
    return True


def _http_queue_notification(session, note) -> None:
    """サーバー起点の通知をセッションの SSE 待ち行列へ入れる（GET が無ければ溜めておく）。"""
    if session is None or not isinstance(note, dict):
        return
    with session["cv"]:
        if len(session["queue"]) < 128:
            session["queue"].append(note)
        session["cv"].notify_all()


def _http_progress_maker(session):
    """HTTP の送り手: セッションの SSE 待ち行列へ入れる（GET が無ければ溜める）。"""

    def maker(token=None):
        def sender(note):
            _http_queue_notification(session, note)

        return sender

    return maker


def _http_process(msg, session, marker=None):
    """POST の本文 1 件を処理する。(本文に載せる応答, 202 を返すべきか) を返す。

    通知は本文に載せずセッションの待ち行列へ回す（MCP 仕様: 通知に応答してはならない）。
    `marker` は `HARNESS_MARKER_HEADER` の値（§8.6 の目印）。
    """
    items = msg if isinstance(msg, list) else [msg]
    replies = []
    # A stateless HTTP POST has no client identity.  Never share its request
    # namespace with another POST (or with the stdio transport).
    request_session = session["cv"] if session is not None else object()
    with S.bound_progress(_http_progress_maker(session)):
        for item in items:
            rid = item.get("id") if isinstance(item, dict) else None
            with S.request_scope(rid, request_session):
                found = S._replies_for(item, marker)
            for reply in found:
                if isinstance(reply, dict) and "id" in reply:
                    replies.append(reply)
                else:
                    _http_queue_notification(session, reply)
    if not replies:
        return None, True
    if isinstance(msg, list):
        return replies, False
    return replies[0], False


def _http_auth_ok(headers, token: str) -> bool:
    """トークン未設定なら素通し。設定時は Authorization: Bearer か x-freeagent-token を要求する。"""
    if not token:
        return True
    import secrets
    given = ""
    auth = headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not given:
        given = (headers.get("x-freeagent-token") or "").strip()
    return bool(given) and secrets.compare_digest(given, token)


def _http_origin_ok(headers, allowed_hosts: set[str]) -> tuple[bool, str]:
    """`Origin` ヘッダを検証する（ブラウザからのクロスサイト呼び出しの抑止）。

    MCP の StreamableHTTP は **`Origin` ヘッダの検証を要求**する（仕様の MUST）。
    素通しだと、ブラウザで開いた別ページが `http://127.0.0.1:8790/mcp` へ
    JSON-RPC を送れて、推論の消費や台帳の書き込みを体外から起こせる（実測: 検証前は
    `Origin: https://evil.example` でも 200 を返していた）。

    判定:
      * `Origin` が無い（CLI / ネイティブクライアント）→ 通し。
      * `Origin` が `http://` / `https://` で、**ホストが待ち受のホストと一致** → 通し。
        （待受が 127.0.0.1 なら `http://127.0.0.1:8790` と同じオリジンだけ許可）
      * `Origin: null`（file:// など）→ 通ししない（ローカルファイルからの呼び出しは
        恣意的なサイトからも生成できる）。
      * 上のどれにも当たらなければ 403。
    """
    origin = (headers.get("origin") or "").strip()
    if not origin:
        return True, ""
    if origin.lower() == "null":
        return False, "Origin: null は拒否しました（ローカルファイルからの呼び出しは遮断します）"
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        return False, f"Origin のスキームが不正です: {origin[:80]}"
    host = (parsed.hostname or "").lower()
    if host in allowed_hosts:
        return True, ""
    return False, (f"Origin が許可されていません: {origin[:80]}"
                   f"（許可: {', '.join(sorted(allowed_hosts))}）")


def _http_host_ok(headers, allowed_hosts: set[str]) -> tuple[bool, str]:
    """`Host` ヘッダが待ち受のホストと一致するか（DNS rebinding の防御）。

    `Host` が偽装されると、攻撃者のドメインが 127.0.0.1 へ解決される。
    ブラウザは `Origin` も付けるので Origin 検証が主だが、非ブラウザの
    クライアントにもこの制約を掛ける。
    """
    host_header = (headers.get("host") or "").strip().lower()
    if not host_header:
        return True, ""   # HTTP/1.0 には Host が無い
    if ":" in host_header:
        host_header = host_header.rsplit(":", 1)[0]
    if host_header in allowed_hosts:
        return True, ""
    return False, f"Host が許可されていません: {host_header[:80]}"


def _http_make_handler(config: dict):
    import http.server

    # Origin / Host の検証で許可するホスト。待受のホスト自身と、そのローカル別名。
    # 待ち受けが 0.0.0.0 / :: なら wildcard 扱いでどの Host も通す（LAN 公開のとき）。
    bind_host = str(config.get("host") or S.HTTP_DEFAULT_HOST).strip().lower()
    if bind_host in ("0.0.0.0", "::", "[::]", "*", ""):
        allowed_hosts: set[str] = set()      # 空 = 制限しない
    else:
        allowed_hosts = {bind_host, "127.0.0.1", "localhost", "::1"}
        allowed_hosts.discard("")
    # 空集合のときは「制限なし」なので判定を緩める（allowed_hosts が空なら常に通す）。
    def _origin_allowed(headers) -> tuple[bool, str]:
        if not allowed_hosts:
            return True, ""
        return _http_origin_ok(headers, allowed_hosts)

    def _host_allowed(headers) -> tuple[bool, str]:
        if not allowed_hosts:
            return True, ""
        return _http_host_ok(headers, allowed_hosts)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{S.SERVER_NAME}/{S.SERVER_VERSION}"
        sys_version = ""

        def log_message(self, fmt, *args):  # noqa: A003 - 親の API 名
            S._debug("http", f"{self.address_string()} {fmt % args}")

        # ---- 応答の共通形 ----
        def _json(self, status: int, payload, *, session_id=None) -> None:
            body = b"" if payload is None else _dumps(payload).encode("utf-8")
            self.send_response(status)
            if body:
                self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if session_id:
                self.send_header(S.HTTP_SESSION_HEADER, session_id)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _error(self, status: int, code: int, message: str) -> None:
            self._json(status, {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}})

        def _path_ok(self) -> bool:
            path = urllib.parse.urlparse(self.path).path
            return path.rstrip("/") == _http_path(config).rstrip("/")

        def _guard(self) -> bool:
            """パス・認証・Origin/Host。通れば True、拒否したら応答を書いて False。"""
            if not self._path_ok():
                self._error(404, -32601, f"unknown path: {self.path}（待受は {_http_path(config)}）")
                return False
            # Origin / Host の検証（MCP StreamableHTTP の仕様 MUST・DNS rebinding の防御）。
            # ブラウザからのクロスサイト呼び出しを体外から起こせないようにする（実測: 検証前は
            # `Origin: https://evil.example` でも 200 を返していた）。
            ok, why = _origin_allowed(self.headers)
            if not ok:
                self._error(403, -32003, why)
                return False
            ok, why = _host_allowed(self.headers)
            if not ok:
                self._error(403, -32004, why)
                return False
            if not _http_auth_ok(self.headers, config.get("token") or ""):
                self._error(401, -32000, f"unauthorized（{S.HTTP_TOKEN_ENV} のトークンが必要です）")
                return False
            return True

        # ---- 本体 ----
        def do_POST(self):  # noqa: N802 - 親の API 名
            if not self._guard():
                return
            try:
                length = int(self.headers.get("content-length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length > S.HTTP_MAX_BODY:
                return self._error(413, -32600, f"request body too large（上限 {S.HTTP_MAX_BODY} バイト）")
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                msg = json.loads(raw.decode("utf-8")) if raw else None
            except Exception:
                return self._error(400, -32700, "parse error")
            if msg is None:
                return self._error(400, -32600, "invalid request")
            sid = (self.headers.get(S.HTTP_SESSION_HEADER) or "").strip()
            session = _http_session_get(sid) if sid else None
            if sid and session is None:
                return self._error(404, -32001, "session not found（initialize からやり直してください）")
            new_sid = None
            if isinstance(msg, dict) and msg.get("method") == "initialize" and session is None:
                new_sid = _http_session_new()
                session = _http_session_get(new_sid)
            marker = (self.headers.get(S.HARNESS_MARKER_HEADER) or "").strip() or None
            try:
                payload, accepted = _http_process(msg, session, marker)
            except Exception as exc:   # ツールは例外を漏らさない（規約 1）。ここも最後の砦
                S._debug("http_error", f"{type(exc).__name__}: {exc}")
                return self._error(500, -32603, f"internal error: {type(exc).__name__}: {exc}")
            _http_session_touch(session)
            if accepted:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                if new_sid:
                    self.send_header(S.HTTP_SESSION_HEADER, new_sid)
                self.end_headers()
                return
            return self._json(200, payload, session_id=new_sid)

        def do_GET(self):  # noqa: N802 - 親の API 名
            if not self._guard():
                return
            sid = (self.headers.get(S.HTTP_SESSION_HEADER) or "").strip()
            if not sid:
                return self._error(400, -32600, f"GET には {S.HTTP_SESSION_HEADER} ヘッダが必要です")
            session = _http_session_get(sid)
            if session is None:
                return self._error(404, -32001, "session not found")
            if "text/event-stream" not in (self.headers.get("accept") or "").lower():
                return self._error(406, -32600, "GET には Accept: text/event-stream が必要です")
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            # MCP Streamable HTTP（2025-06-18 / 2026-07-28）が SHOULD とする指定。リバースプロキシ
            # （nginx 等）が SSE をバッファすると、進捗通知が**まとめて遅れて**届く。
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.flush()
                while True:
                    with session["cv"]:
                        if session["queue"]:
                            note = session["queue"].pop(0)
                        else:
                            session["cv"].wait(S.HTTP_SSE_KEEPALIVE)
                            note = session["queue"].pop(0) if session["queue"] else None
                    if note is None:
                        if session.get("closed"):
                            break
                        self.wfile.write(b": keep-alive\n\n")   # SSE のコメント行（クライアントは無視する）
                    else:
                        data = _dumps(note)
                        self.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            return

        def do_DELETE(self):  # noqa: N802 - 親の API 名
            if not self._guard():
                return
            sid = (self.headers.get(S.HTTP_SESSION_HEADER) or "").strip()
            if sid and not _http_session_drop(sid):
                return self._error(404, -32001, "session not found")
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def _http_path(config: dict) -> str:
    """待受パス（キーが無い / None / 空 のときだけ既定）。

    部分的な config（`{"host","port"}` だけ）でも **KeyError で全リクエストを無言で切らない**
    ために、`config["path"]` の直引きはしない（実測: 直引きだと全リクエストが
    `RemoteDisconnected` になり、サーバー側には traceback だけが残る）。
    **明示された不正値は既定へ読み替えず例外にする**（下の `_http_port` と同じ理由）。
    """
    raw = config.get("path")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return S.HTTP_DEFAULT_PATH
    if not isinstance(raw, str):
        raise ValueError(f"path が不正です: {raw!r}（文字列。省略時は {S.HTTP_DEFAULT_PATH}）")
    path = raw.strip()
    return path if path.startswith("/") else "/" + path


def _http_host(config: dict) -> str:
    """待受ホスト（キーが無い / None / 空 のときだけ既定）。"""
    raw = config.get("host")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return S.HTTP_DEFAULT_HOST
    if not isinstance(raw, str):
        raise ValueError(f"host が不正です: {raw!r}（文字列。省略時は {S.HTTP_DEFAULT_HOST}）")
    return raw.strip()


def _http_port(config: dict) -> int:
    """待受ポート（キーが無い / None のときだけ既定。0 は OS に選ばせる正当な値）。

    **明示された不正値を既定へ読み替えない**。`"not-a-port"` を既定（8765）で読み替えると、
    利用者が指定したのと**違うポートで黙って待ち受ける**（クライアントは指定したポートへ繋ぐので
    気づけない。実測 2026-10-27: この読み替えのせいで既存テストが 8765 で `serve_forever` に入り、
    テストスイートが 51 分ハングした）。
    """
    raw = config.get("port")
    if raw is None:
        return S.HTTP_DEFAULT_PORT
    bad = ValueError(f"port が不正です: {raw!r}（0〜65535 の整数。省略時は {S.HTTP_DEFAULT_PORT}）")
    if isinstance(raw, bool):                       # bool は int の派生なので先に弾く
        raise bad
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, str) and raw.strip().isdigit():   # 全角数字は isdigit も int() も受ける
        value = int(raw.strip())
    else:                                           # "8765.7" / [] / "not-a-port" は切り捨てず拒否
        raise bad
    if not 0 <= value <= 65535:                     # 範囲外も**丸めずに拒否**（70000 → 65535 にしない）
        raise bad
    return value


def _http_build_server(config: dict):
    """待受ソケットを作って返す（`serve_forever` は呼び出し側。テストが自前で止められるように分ける）。"""
    import http.server

    class _Server(http.server.ThreadingHTTPServer):
        # Windows の SO_REUSEADDR は「同じポートへの二重バインド」を許すため False にする。
        # POSIX（Linux / macOS）では SO_REUSEADDR でも生きた LISTEN への二重起動は失敗し、
        # False だと常駐サービスの再起動直後に TIME_WAIT でバインドできなくなる。
        allow_reuse_address = (os.name != "nt")
        daemon_threads = True

    return _Server((_http_host(config), _http_port(config)), _http_make_handler(config))


def serve_http(config: dict) -> None:
    """HTTP モードで待ち受ける（1 プロセスを複数クライアントで共有する）。"""
    try:
        server = _http_build_server(config)
    except (TypeError, ValueError) as exc:
        # 設定の不正は**起動前に**止める（不正値を既定へ読み替えて別のポートで待ち受けない）。
        try:
            sys.stderr.write(f"[{S.SERVER_NAME}] HTTP の設定が不正です: {exc}\n"
                             f"  --host / --port / --path（または {S.HTTP_HOST_ENV} / {S.HTTP_PORT_ENV} / "
                             f"{S.HTTP_PATH_ENV}）を確認してください。\n")
            sys.stderr.flush()
        except Exception:
            pass
        raise SystemExit(2)
    except OSError as exc:
        try:
            sys.stderr.write(f"[{S.SERVER_NAME}] HTTP を {_http_host(config)}:{_http_port(config)} で開始できません: {exc}\n"
                             f"  同じポートで既に動いていませんか（1 プロセス共有が目的なので二重起動は不要です）。"
                             f"別ポートは --port / {S.HTTP_PORT_ENV} で指定できます。\n")
            sys.stderr.flush()
        except Exception:
            pass
        raise SystemExit(2)
    host, port = server.server_address[0], server.server_address[1]
    try:
        sys.stderr.write(f"[{S.SERVER_NAME}] HTTP 待受 http://{host}:{port}{_http_path(config)}"
                         f"（token {'あり' if config.get('token') else 'なし'}・pid {os.getpid()}）\n")
        sys.stderr.flush()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


EXPORTS = {name: globals()[name] for name in (
    '_HTTP_SESSIONS',
    '_HTTP_SESSIONS_LOCK',
    '_http_auth_ok',
    '_http_build_server',
    '_http_host',
    '_http_host_ok',
    '_http_make_handler',
    '_http_origin_ok',
    '_http_path',
    '_http_port',
    '_http_process',
    '_http_progress_maker',
    '_http_queue_notification',
    '_http_session_drop',
    '_http_session_get',
    '_http_session_new',
    '_http_session_touch',
    '_http_sessions_prune',
    '_http_sessions_prune_locked',
    'http_config',
    'serve_http',
)}
