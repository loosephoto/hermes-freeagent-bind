"""§5 知識バックエンド（取得・キャッシュ・合流）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import copy
import http
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib

from . import server as S  # noqa: E402

_KB_CACHE: dict[str, dict] = {}


_KB_CACHE_LOCK = threading.Lock()


_KB_INFLIGHT: dict[str, threading.Event] = {}


_KB_BLOCKED: dict[str, float] = {}


_KB_BLOCK_LOCK = threading.Lock()


_KB_BLOCK_S = 600.0


SOURCES = ("wikipedia", "wikidata", "arxiv", "crossref", "openalex", "github")


def _kb_block(host: str, secs: float = _KB_BLOCK_S) -> None:
    with _KB_BLOCK_LOCK:
        _KB_BLOCKED[host] = S.now_ts() + secs


def _kb_is_blocked(host: str) -> bool:
    with _KB_BLOCK_LOCK:
        until = _KB_BLOCKED.get(host)
        if until is None:
            return False
        if until <= S.now_ts():
            _KB_BLOCKED.pop(host, None)
            return False
        return True


def _kb_socket_of(resp):
    """HTTP 応答のソケットを取り出す（見つからなければ None）。"""
    for path in (("fp", "raw", "_sock"), ("fp", "_sock"), ("fp", "raw", "socket")):
        obj = resp
        try:
            for attr in path:
                obj = getattr(obj, attr)
            return obj
        except AttributeError:
            continue
    return None


def _kb_set_socket_timeout(resp, seconds: float) -> None:
    """応答のソケットに読み取りタイムアウトを張り直す（残りの壁時計予算）。

    `urlopen(timeout=…)` は接続前のソケットに設定した値をそのまま使い続けるため、
    読み取り中に 1 回の `read` が壁時計予算を突き抜ける。ここでは残りの予算を
    ソケットに張り直し、1 回の recv 自体を予算内に収める。
    """
    sock = _kb_socket_of(resp)
    if sock is None:
        return
    try:
        sock.settimeout(max(0.1, seconds))
    except (OSError, AttributeError, ValueError):
        pass


def _kb_content_length(resp) -> int | None:
    """応答の `Content-Length` を返す（無ければ None）。

    終端判定に使う（§5.8）。`read1` は 1 バイトずつしか返さないことがあるため、
    「読了多少バイトか」をヘッダの長さで照合して終端を判断する。
    """
    try:
        headers = getattr(resp, "headers", None)
        raw = headers.get("Content-Length") if headers is not None else None
        if raw is None:
            return None
        return int(raw)
    except (AttributeError, TypeError, ValueError):
        return None


def _kb_read_within(resp, deadline: float) -> str:
    """応答本文を**壁時計予算内**で読み、UTF-8 の文字列にして返す。

    `urlopen(timeout=…)` はソケット 1 回の待ちの上限であってリクエスト全体の壁時計上限ではない。
    1 バイトずつ drip する提供元では読みが終わるまで待ち続ける（実測: 98.0 秒 / `S.KB_TIMEOUT`=20 秒）。

    2 段構えで止める:
      1. **チャンク単位の読み**（`read1`）— 大きな `read(n)` は n バイト揃うまでブロックする
         （drip 応答では本文が終わるまで戻らない）ので、届いたぶんだけ読む。
      2. **残りの壁時計予算をソケットに張り直す**— 1 回の recv も予算内に収める。

    予算超過は TimeoutError として外へ出す（呼び出し側の `S.kb_http` が失敗として扱い、
    ホストを短く遮断する）。
    """
    chunks: list[bytes] = []
    # **読む量ではなく予算で打ち切る**。
    # `read(n)` は n バイト揃うまでブロックする（drip 応答では本文が終わるまで戻らない）ので、
    # 大きな n を指定すると予算を突き抜ける。`read1(n)` は「届いたぶんだけ」を返す。
    #
    # 終端の判定は **Content-Length** で行う（応答ヘッダに長さがある）。
    # `read1` は 1 バイトずつしか返らないことがある（実測: drip 応答で 1 バイト / 回）ので、
    # 読み進めるたびに「We've read all the data」になるまで繰り返す。
    # Content-Length が無い（chunked / 長さ不明）ときは、`read1` が空を返すまで読む。
    # ソケットの読み取りタイムアウトは残りの壁時計予算に張り直すので、
    # recv 自体が予算内に収まる（`kb_http` の timeout は「ソケット 1 回の待ち」の上限で、
    # リクエスト全体の壁時計上限ではない）。
    reader = getattr(resp, "read1", None)
    expected = _kb_content_length(resp)
    total = 0
    previous: bytes | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"読み取りが {S.KB_TIMEOUT:g} 秒の予算を超えました")
        _kb_set_socket_timeout(resp, remaining)
        try:
            if reader is not None:
                chunk = reader(65536)
            else:
                try:
                    chunk = resp.read(65536)
                except TypeError:
                    chunk = resp.read()
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError(f"読み取りが {S.KB_TIMEOUT:g} 秒の予算を超えました") from exc
        if not chunk:
            break
        if previous is not None and chunk == previous and expected is None:
            # 同じ内容を返す実装（テストの FakeResp）= 読み終端。
            break
        previous = chunk
        chunks.append(chunk)
        total += len(chunk)
        if expected is not None and total >= expected:
            break
    return b"".join(chunks).decode("utf-8", "replace")


def kb_http(url: str, *, accept: str = "application/json", extra_headers: dict | None = None,
            timeout: float | None = None, body: bytes | None = None,
            method: str = "GET") -> tuple[int, str]:
    """(status, body) を返す。例外は投げず、失敗も status で表す（呼び出し側で分岐する）。

    `body` を渡すと POST になる（§5.18 の OSV のように POST 専用の API があるため）。

    **読み取りは壁時計でも打ち切る**（§5.8）。`urlopen(timeout=…)` の上限は「ソケット 1 回の待ち」に対する
    もので、**リクエスト全体の壁時計制限ではない**。1 バイトずつ drip する応答では
    ソケット毎回がタイムアウト内なので、応答が終わるまで何分も待ち続ける（実測: ローカル drip
    サーバで 98.0 秒・`S.KB_TIMEOUT`=20 秒の 5 倍、zenodo 実測で 122.75 秒）。
    → 読み取りループに残りの壁時計予算を渡し、超過したら打ち切る。
    """
    host = urllib.parse.urlparse(url).netloc
    if _kb_is_blocked(host):
        return 0, f"blocked: {host} は直近の失敗により一時的にスキップしています"
    headers = {"User-Agent": S.KB_USER_AGENT, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    budget = timeout if timeout is not None else S.KB_TIMEOUT
    deadline = time.monotonic() + max(0.1, budget)
    # **429 は再試行する**（実測 2026-10-20）。
    # crossref の 429 は**レート制限ではなく共有プールのTransientな拒否**で、
    # 間隔（0.4〜1.0 秒）不管で 10 回中 1〜3 回出る。→ 再試行すると 10 試行すべて成功した
    # （待つ必要なし・11〜14 リクエスト）。**待っても直らない**ので待たない。
    for attempt in range(S._KB_HTTP_429_RETRIES + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with S._urlopen(req, budget) as resp:
                return int(resp.status), _kb_read_within(resp, deadline)
        except urllib.error.HTTPError as exc:
            # **応答本文と要求本文で名前を分ける**（同じ変数名を使うと、再試行時に
            # 文字列の本文を送って `TypeError: POST data should be bytes` になる）。
            resp_body = ""
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            status = int(exc.code)
            try:
                resp_body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            finally:
                exc.close()
            if status == 429 and attempt < S._KB_HTTP_429_RETRIES:
                # **遮断しない**（= 一時的な拒否。ホストをブロックすると他のソースまで巻き込む）。
                # Retry-After があれば尊重する（Google 等は数秒〜数分）。無ければ待たない。
                if retry_after:
                    wait_s = min(S._parse_retry_after(retry_after, None), S._KB_HTTP_429_MAX_WAIT_S)
                    if wait_s > 0 and time.monotonic() + wait_s < deadline:
                        time.sleep(wait_s)
                continue
            if status in (403, 429, 503):
                _kb_block(host, S._parse_retry_after(retry_after, None) if status == 429 else _KB_BLOCK_S)
            return status, resp_body
        except Exception as exc:
            _kb_block(host, 120.0)  # 接続不可・タイムアウトも短くブロックする
            return 0, f"{type(exc).__name__}: {exc}"
    return 0, "HTTP 429: 再試行しても応答がありませんでした"


def kb_json(url: str, *, extra_headers: dict | None = None,
            payload: dict | None = None) -> tuple[dict | None, str]:
    """JSON を取る。`payload` を渡すと POST（Content-Type: application/json）。

    **持続接続の使えるホスト（`_KA_TRUSTED_HOSTS`）は §5.2.1 の経路へ委譲する**（v0.24.0）。
    `urllib` は応答ごとに接続を閉じるので、同じホストへ続けて引くソースは毎回 TCP + TLS を
    張り直していた（実測: crossref 2 連続 722ms → 261ms / wikipedia 2 連続 978ms → 831ms）。
    """
    if payload is None and not extra_headers and _ka_usable_here(url):
        return S.kb_json_keepalive(url)
    if payload is None:
        status, body = S.kb_http(url, extra_headers=extra_headers)
    else:
        headers = {"Content-Type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        status, body = S.kb_http(url, extra_headers=headers,
                               body=S._dumps(payload).encode("utf-8"),
                               method="POST")
    if status != 200:
        return None, f"HTTP {status}: {body[:200]}" if status else body
    try:
        return json.loads(body), ""
    except ValueError as exc:
        return None, f"JSON 解析失敗: {exc}"


def _kb_cached(key: str, producer):
    """結果をキャッシュする。**同時実行は 1 回の上流取得に畳まれる**（単一飛行）。

    待ち（`event.wait()`）には**上限**がある（`S._KB_INFLIGHT_WAIT_MAX_S`、既定は `KB_DEADLINE`）。
    理由: 待ちに上限が無いと、**取得が 14 秒かかるソースで 2 番目は 13.8 秒待つ**ことになり
    締め切り（`KB_DEADLINE`=8 秒）を超えて**利用者を待たせる**（実測）。
    上限に達したら**待たずに自分の取得を開始する**（重複取得より早い応答）。
    """
    wait_deadline = time.monotonic() + S._KB_INFLIGHT_WAIT_MAX_S
    while True:
        with _KB_CACHE_LOCK:
            hit = _KB_CACHE.get(key)
            if hit and S.now_ts() - S.as_float(hit.get("at"), 0.0) < S.KB_TTL:
                return copy.deepcopy(hit.get("value"))
            event = _KB_INFLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _KB_INFLIGHT[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        # 待ちの上限。切れたら**自分で取得する**（待つより早い）。
        remaining = wait_deadline - time.monotonic()
        if remaining <= 0:
            break
        event.wait(min(remaining, 0.25))
        if event.is_set():
            with _KB_CACHE_LOCK:
                hit = _KB_CACHE.get(key)
                if hit and S.now_ts() - S.as_float(hit.get("at"), 0.0) < S.KB_TTL:
                    return copy.deepcopy(hit.get("value"))
            # 取得は終わったがキャッシュに残っていない（失敗）→ 自分で取る。
            break
    try:
        value = producer()
        if not (isinstance(value, dict) and value.get("error")):
            with _KB_CACHE_LOCK:
                _KB_CACHE[key] = {"at": S.now_ts(), "value": copy.deepcopy(value)}
                if len(_KB_CACHE) > 512:
                    for old in sorted(_KB_CACHE, key=lambda k: S.as_float(_KB_CACHE[k].get("at"), 0.0))[:128]:
                        _KB_CACHE.pop(old, None)
        return copy.deepcopy(value)
    finally:
        with _KB_CACHE_LOCK:
            _KB_INFLIGHT.pop(key, None)
            event.set()


def _plain_text(text: str, limit: int = 600) -> str:
    """マークアップ（JATS/HTML）と余分な空白を落として本文だけにする。

    Crossref の abstract は `<jats:p>…</jats:p>` 形式で返るため、そのまま注入すると
    タグが本文を占めてサブLLMが読めない。
    """
    return S.truncate(" ".join(re.sub(r"<[^>]+>", " ", text or "").split()), limit)


def _cite(source: str, title: str, url: str, **extra) -> dict:
    row = {"source": source, "title": S.truncate((title or "").strip(), 300), "url": url}
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            row[key] = value
    return row


_KA_POOL_MAX = max(0, min(8, S._env_int("FREEAGENT_KB_POOL_MAX", 2)))


_KA_POOL_IDLE = max(1.0, min(3600.0, S._env_float("FREEAGENT_KB_POOL_IDLE", 120.0)))


_KA_IDLE: dict[str, list[tuple[http.client.HTTPConnection, float]]] = {}


_KA_LOCK = threading.Lock()


_KA_OPENS = [0]        # 接続を作った回数（回帰テストと実測のため）


def _ka_close(conn) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _ka_reset() -> None:
    """在庫を空にする（テストと切り分け用）。**貸出中の接続には触らない**。"""
    with _KA_LOCK:
        pools = list(_KA_IDLE.values())
        _KA_IDLE.clear()
    for entries in pools:
        for conn, _at in entries:
            _ka_close(conn)


def _ka_ssl_context() -> ssl.SSLContext:
    """`S._OPENER` と同じ `SSLContext` を使う（`_refresh_ssl_context` の更新が効く）。"""
    for handler in getattr(S._OPENER, "handlers", ()):
        if isinstance(handler, S._HTTPSHandler):
            ctx = getattr(handler, "_context", None)
            if isinstance(ctx, ssl.SSLContext):
                return ctx
    return ssl.create_default_context()


def _ka_proxied(host: str) -> bool:
    """プロキシ経由が必要な環境か（`urllib` と同じ判定を使う）。判定できないときは True（共用経路へ）。"""
    try:
        proxies = urllib.request.getproxies()
        if not proxies:
            return False
        return not urllib.request.proxy_bypass(host)
    except Exception:
        return True


def _ka_allowed(url: str) -> bool:
    """接続を使い回して良いか（プロキシ環境と `FREEAGENT_KB_KEEPALIVE=0` では共用経路へ）。"""
    if S._KA_POOL_MAX <= 0 or not S._env_on("FREEAGENT_KB_KEEPALIVE", True):
        return False
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    return not S._ka_proxied(host)


_KA_TRUSTED_HOSTS = ("wikidata.org", "wikipedia.org", "www.crossref.org", "api.crossref.org",
                     "api.openalex.org", "hn.algolia.com")


def _ka_usable_here(url: str) -> bool:
    """`S.kb_json` から持続接続経路へ委譲してよいか（プール有効・非プロキシ・許可ホスト）。"""
    if not _ka_allowed(url):
        return False
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == name or host.endswith("." + name) for name in _KA_TRUSTED_HOSTS)


def _ka_new_conn(parts, budget: float) -> http.client.HTTPConnection:
    """新しい接続を作る（`timeout` は**接続と読み取りの両方**に掛ける）。"""
    port = parts.port or (443 if parts.scheme == "https" else 80)
    timeout = max(0.5, min(budget, S.CONNECT_TIMEOUT))
    _KA_OPENS[0] += 1
    if parts.scheme == "https":
        return http.client.HTTPSConnection(parts.hostname, port, timeout=timeout,
                                           context=_ka_ssl_context())
    return http.client.HTTPConnection(parts.hostname, port, timeout=timeout)


def _ka_set_timeout(conn, end: float) -> float:
    """残り予算を `conn.timeout` とソケットの両方に張る（片方だけだと 1 回の recv が予算を超える）。"""
    left = max(0.2, end - time.monotonic())
    conn.timeout = left
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.settimeout(left)
        except (OSError, ValueError):
            pass
    return left


def _ka_borrow(origin: str):
    """在庫から 1 本借りる（期限切れは捨てる）。無ければ None（呼び出し側が新しく作る）。"""
    cutoff = time.monotonic() - _KA_POOL_IDLE
    stale: list = []
    with _KA_LOCK:
        entries = _KA_IDLE.get(origin) or []
        while entries:
            conn, at = entries.pop()
            if at >= cutoff:
                _KA_IDLE[origin] = entries
                for old in stale:
                    _ka_close(old)
                return conn
            stale.append(conn)
        _KA_IDLE.pop(origin, None)
    for old in stale:
        _ka_close(old)
    return None


def _ka_give_back(origin: str, conn) -> None:
    """在庫へ返す（満杯なら閉じる）。**壊れた接続は返さない**（呼び出し側が `_ka_close` する）。"""
    if S._KA_POOL_MAX <= 0:
        _ka_close(conn)
        return
    with _KA_LOCK:
        entries = _KA_IDLE.setdefault(origin, [])
        if len(entries) < S._KA_POOL_MAX:
            entries.append((conn, time.monotonic()))
            return
    _ka_close(conn)


def _ka_read_body(resp, deadline: float) -> str:
    """持続接続用の読み取り（`_kb_read_within` と同じ 2 段構え・**重複ヒューリスティック無し**）。

    在庫へ返す条件は「**本文を読み切った**」こと。`_kb_read_within` はテストの `FakeResp` 用に
    「同じ内容が 2 回続いたら終端」という推測を持つが、その推測で止まると**未読を残したまま**
    接続を返すことになり、次のリクエストが壊れる。ここは終端の理由を区別できるよう、
    `read1` が空を返すか `Content-Length` に達するまでしか止めない
    （chunked の終端でも `read1` は空を返す。実測: 1.0 MB の chunked 応答で確認）。
    """
    chunks: list[bytes] = []
    total = 0
    expected = _kb_content_length(resp)
    reader = getattr(resp, "read1", None)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"読み取りが {S.KB_TIMEOUT:g} 秒の予算を超えました")
        _kb_set_socket_timeout(resp, remaining)
        try:
            chunk = reader(65536) if reader is not None else resp.read(65536)
        except (TimeoutError, socket.timeout) as exc:
            raise TimeoutError(f"読み取りが {S.KB_TIMEOUT:g} 秒の予算を超えました") from exc
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if expected is not None and total >= expected:
            break
    return b"".join(chunks).decode("utf-8", "replace")


def kb_json_keepalive(url: str, *, timeout: float | None = None) -> tuple[dict | None, str]:
    """JSON を取る（**接続を使い回す版**・§5.2.1）。戻り値の形は `S.kb_json` と同じ。

    プロキシ環境と `FREEAGENT_KB_KEEPALIVE=0` では `S.kb_json` へ委譲する（挙動を変えない）。
    現時点の呼び出し元は wikidata のみ。
    """
    if not _ka_allowed(url):
        return S.kb_json(url)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError as exc:
        return None, f"URL が不正です: {exc}"
    host = parts.netloc
    if _kb_is_blocked(host):
        return None, f"blocked: {host} は直近の失敗により一時的にスキップしています"
    end = time.monotonic() + max(0.1, S.KB_TIMEOUT if timeout is None else timeout)
    deadline_at = S._kb_deadline_at()
    if deadline_at is not None:
        end = min(end, deadline_at)
    origin = f"{parts.scheme}://{parts.netloc}"
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    headers = {"User-Agent": S.KB_USER_AGENT, "Accept": "application/json"}
    error = "応答がありませんでした"
    # 429 の再試行回数は `kb_http` と同じにする（持続接続経路に委譲したソースが
    # 「1 回で遮断」に変わらないため。実測: crossref の 429 は再試行で直る）。
    for attempt in range(S._KB_HTTP_429_RETRIES + 2):
        remaining = end - time.monotonic()
        if remaining <= 0.2:
            error = "締め切りの残りが足りません"
            break
        conn = _ka_borrow(origin)
        if conn is None:
            conn = S._ka_new_conn(parts, remaining)
        else:
            _ka_set_timeout(conn, end)
        try:
            conn.request("GET", target, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            # 在庫の接続が失効していた（サーバー側のアイドル切断など）／接続できない。
            _ka_close(conn)
            error = f"{type(exc).__name__}: {exc}"
            if attempt == 0:
                continue                      # 作り直して 1 回だけやり直す
            _kb_block(host, 120.0)
            return None, error
        retry_after = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
        reusable = not getattr(resp, "will_close", True)
        try:
            body = _ka_read_body(resp, end)
        except Exception as exc:
            _ka_close(conn)
            error = f"{type(exc).__name__}: {exc}"
            if attempt == 0 and time.monotonic() < end:
                continue
            _kb_block(host, 120.0)
            return None, error
        if reusable:
            _ka_give_back(origin, conn)
        else:
            _ka_close(conn)
        status = int(resp.status)
        if status == 429 and attempt < S._KB_HTTP_429_RETRIES + 1:
            # `kb_http` と同じ扱い: 429 は**再試行**で直る（遮断しない）。
            wait_s = min(S._parse_retry_after(retry_after, None), S._KB_HTTP_429_MAX_WAIT_S)
            if wait_s > 0 and time.monotonic() + wait_s < end:
                time.sleep(wait_s)
            continue
        if status in (403, 429, 503):
            _kb_block(host, S._parse_retry_after(retry_after, None) if status == 429 else _KB_BLOCK_S)
        if status != 200:
            return None, f"HTTP {status}: {body[:200]}"
        try:
            return json.loads(body), ""
        except ValueError as exc:
            return None, f"JSON 解析失敗: {exc}"
    return None, error


_ARXIV_LOCK = threading.Lock()


_ARXIV_LAST = 0.0


_ARXIV_MIN_INTERVAL = S._env_float("FREEAGENT_ARXIV_INTERVAL", 3.0)


def _arxiv_throttle() -> None:
    global _ARXIV_LAST
    with _ARXIV_LOCK:
        wait = _ARXIV_MIN_INTERVAL - (S.now_ts() - _ARXIV_LAST)
        if wait > 0:
            time.sleep(min(wait, 10.0))
        _ARXIV_LAST = S.now_ts()


KB_BACKENDS = {
    # wikipedia だけ「言語の自動判定」を使う（opts が空なら kb_wikipedia 側が問いから決める）。
    "wikipedia": lambda q, limit, opts: S.kb_wikipedia(q, lang=opts.get("lang") or "", limit=limit),
    "wikidata": lambda q, limit, opts: S.kb_wikidata(q, lang=opts.get("lang") or "ja", limit=limit),
    "arxiv": lambda q, limit, opts: S.kb_arxiv(q, limit=limit),
    "crossref": lambda q, limit, opts: S.kb_crossref(q, limit=limit,
                                                 need_evidence=opts.get("need_evidence") is True),
    "openalex": lambda q, limit, opts: S.kb_openalex(q, limit=limit),
    "github": lambda q, limit, opts: S.kb_github(q, kind=opts.get("kind") or "repo", limit=limit),
}


KB_DEADLINE = max(1.0, min(120.0, S._env_float("FREEAGENT_KB_DEADLINE", 8.0)))


def _kb_gather(picked: list[str], query: str, limit: int, opts: dict,
               deadline: float | None = None) -> tuple[dict, dict]:
    """(ソース → 結果, ソース → 所要秒) を返す。締め切りを過ぎたソースは timed_out の結果にする。"""
    deadline = KB_DEADLINE if deadline is None else deadline
    opts = {**opts, "deadline_at": time.monotonic() + deadline}
    done: dict[str, dict] = {}
    spent: dict[str, float] = {}
    lock = threading.Lock()
    finished = threading.Event()
    remaining = [len(picked)]

    def work(src: str) -> None:
        start = time.monotonic()
        # このスレッドの締め切りを載せる（予算待ちの判断に使う。§5.11）。
        # **24 個の `kb_*` の signatures を変えずに**届けるための経路。
        _KB_LOCAL.deadline_at = opts.get("deadline_at")
        try:
            res = _kb_source_result(src, query, limit, opts)
        except Exception as exc:  # 例外を外へ漏らさない（規約 1）
            res = {"error": f"{type(exc).__name__}: {exc}"}
        with lock:
            done[src] = res if isinstance(res, dict) else {"error": "不正な結果"}
            spent[src] = round(time.monotonic() - start, 2)
            remaining[0] -= 1
            if remaining[0] <= 0:
                finished.set()

    if not picked:
        return {}, {}
    for src in picked:
        threading.Thread(target=work, args=(src,), name=f"kb-{src}", daemon=True).start()
    finished.wait(deadline)
    with lock:
        by_source = {src: done[src] for src in picked if src in done}
        timings = dict(spent)
    for src in picked:
        if src not in by_source:
            by_source[src] = {
                "error": (f"締め切り {deadline:g} 秒に間に合いませんでした（取得は続けており、"
                          "終われば同じ問いの次回はキャッシュから返ります）"),
                "timed_out": True, "items": [], "citations": []}
            timings[src] = None
    return {src: by_source[src] for src in picked}, timings


def knowledge_lookup(query: str, sources: list[str] | None = None, *, limit: int = 3,
                     lang: str = "ja", kind: str = "repo", max_workers: int | None = None,
                     deadline: float | None = None, datacite_kind: str = "all", fallback: bool = False,
                     need_evidence: bool = False, doc: str = "") -> dict:
    """指定ソースを**並列に**引いて、出典つきでまとめる。LLM を使わないので幻覚が入らない。

    全体の締め切り（`KB_DEADLINE`）までに届いた分だけ返す（§5.8）。`max_workers` は互換のため
    受け取るが使わない（ソースごとに専用スレッド）。
    """
    query = S.as_str(query)
    if not query:
        return {"error": "query は必須です", "query": query}
    requested = S.as_str_list(sources)
    picked = list(dict.fromkeys(s for s in requested if s in S.KB_BACKENDS)) if requested else [s for s in DEFAULT_SOURCES if s in S.KB_BACKENDS]
    unknown = [s for s in requested if s not in S.KB_BACKENDS]
    if requested and not picked:
        message = "有効な sources がありません。利用可能: " + ", ".join(S.KB_BACKENDS)
        return {"query": query, "sources": [], "unknown_sources": unknown,
                "results": {}, "citations": [], "citation_count": 0,
                "errors": {"sources": message}, "error": message, "llm_used": False}
    limit = S.as_int(limit, 3, 1, 10)
    # lang 未指定は「空」で渡す（wikipedia が問いから自動判定する。wikidata は or "ja" で従来どおり）。
    opts = {"lang": S.as_str(lang, ""), "kind": S.as_str(kind, "repo"),
            "datacite_kind": S.as_str(datacite_kind, "all"), "fallback": fallback is True,
            # devdocs の docset 選択（v0.40.3）。**ここで渡さないと docset 内検索が届かない**
            # （sources/devdocs.py の backend() は opts.get("doc") を読む。実測: v0.40.2 では
            # tool_lookup に doc を渡しても docs.json の一覧検索になっていた）。
            "doc": S.as_str(doc),
            # 「本文根拠が要る場面か」= `grounded`。**根拠の確保を最優先する場面**と
            # **書誌の網羅性を最優先する場面**は違う（§6.18）。
            "need_evidence": need_evidence is True}
    by_source, timings = _kb_gather(picked, query, limit, opts, deadline)
    citations: list[dict] = []
    errors = {}
    for src, res in by_source.items():
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            errors[src] = res["error"]
        citations.extend(res.get("citations") or [])
    citations = _kb_merge_citations(citations)
    late = [src for src, res in by_source.items() if isinstance(res, dict) and res.get("timed_out")]
    # **同じ引用を 2 回返さない**（v0.22.0）。ソースごとの `citations` は全ソース統合の `citations` の
    # 部分集合で、統合側には `metadata_only` / `aliases` も付く（実測: limit=3 の lookup で構造化
    # 30,589c → 22,722c・26% 減、情報の欠落なし）。`results[source]` は items と状態だけを持つ。
    public_results = {src: ({k: v for k, v in res.items() if k != "citations"}
                            if isinstance(res, dict) else res)
                      for src, res in by_source.items()}
    out = {"query": query, "sources": picked, "unknown_sources": unknown,
           "results": public_results, "citations": citations, "citation_count": len(citations),
           # 引用を `results` の中で二重に持たないことを**明示する**（黙って減らさない・規約 8）。
           "citations_in_results": False,
           "errors": errors, "llm_used": False, "timings": timings,
           "deadline_s": KB_DEADLINE if deadline is None else deadline}
    if late:
        out["timed_out"] = late
    return out


def _literal_search(query: str) -> str:
    """自然語の各語を引用して AND 結合する。提供元の演算子として解釈させない。"""
    return " AND ".join(S._dumps(word) for word in query.split())


def _kb_new_cached(key: str, source: str, producer) -> dict:
    """新規ソースの破損した上流データも、直接呼び出し時に例外を漏らさない。"""
    def safe():
        try:
            result = producer()
            for cite in result.get("citations") or []:
                _kb_http_url(cite.get("url"))
            return result
        except Exception as exc:
            return {"source": source, "error": f"応答解析失敗: {type(exc).__name__}: {exc}"}
    return S._kb_cached(key, safe)


DEFAULT_SOURCES = SOURCES


KB_HEDGE_DELAY = max(0.05, min(30.0, S.as_float(os.environ.get("FREEAGENT_KB_HEDGE_DELAY"), 2.0)))


KB_HEDGE_MIN_REMAINING = max(0.0, min(1.0, S.as_float(os.environ.get("FREEAGENT_KB_HEDGE_MIN_REMAINING"), 0.05)))


def _kb_source_result(src: str, query: str, limit: int, opts: dict) -> dict:
    """主系と代替は別ホスト。遅延時も主系をキャンセルせず、そのキャッシュを温める。"""
    primary_fn = S.KB_BACKENDS[src]
    allowed = (src == "arxiv" and opts.get("fallback") is True and "datacite" in S.KB_BACKENDS
               and not re.search(r'[:"()\[\]]|\b(?:AND|OR|NOT)\b|\b\d{4}\.\d{4,5}(?:v\d+)?\b'
                                 r'|\b[A-Za-z][A-Za-z.-]*/\d{7}(?:v\d+)?\b', query))
    if not allowed:
        return primary_fn(query, limit, opts)
    done = {}
    lock = threading.Lock()
    changed = threading.Event()

    def invoke(key, fn, options):
        try:
            if (key == "alternate"
                    and options.get("deadline_at", float("inf")) - time.monotonic() < S.KB_HEDGE_MIN_REMAINING):
                res = {"error": "締め切り後の代替取得は開始しません", "timed_out": True}
            else:
                res = fn(query, limit, options)
            if not isinstance(res, dict):
                res = {"error": "不正な結果"}
        except Exception as exc:
            res = {"error": f"{type(exc).__name__}: {exc}"}
        with lock:
            done[key] = res
            changed.set()

    def retryable(res):
        err = S.as_str(res.get("error"))
        return bool(err and (S.is_env_failure(err) or err.startswith("blocked:")
                    or re.search(r"HTTP (?:429|5\d\d)\b", err)))

    threading.Thread(target=invoke, args=("primary", primary_fn, opts), daemon=True).start()
    end = opts.get("deadline_at", time.monotonic() + KB_DEADLINE)
    changed.wait(min(S.KB_HEDGE_DELAY, max(0.0, end - time.monotonic())))
    with lock:
        primary = done.get("primary")
    if primary is not None and not retryable(primary):
        return primary
    if end - time.monotonic() < S.KB_HEDGE_MIN_REMAINING:
        return primary if primary is not None else {"error": "締め切りに間に合いませんでした",
                                                   "timed_out": True, "items": [], "citations": []}
    alternate_opts = {**opts, "datacite_kind": "arxiv", "fallback": False}
    threading.Thread(target=invoke, args=("alternate", S.KB_BACKENDS["datacite"], alternate_opts), daemon=True).start()
    while True:
        with lock:
            primary = done.get("primary")
            alternate = done.get("alternate")
            changed.clear()
        if primary is not None and not primary.get("error"):
            return primary
        if alternate is not None and not alternate.get("error") and alternate.get("items"):
            return {**alternate, "fallback": {"requested_source": src, "served_by": "datacite",
                    "primary_error": (primary or {}).get("error") or "主系が応答待ち（遅延時の代替）"}}
        if primary is not None and alternate is not None:
            return {**primary, "fallback_attempt": {"source": "datacite", "error": alternate.get("error") or "該当なし"}}
        changed.wait()


_KB_RATE_NEXT: dict[str, float] = {}


_KB_RATE_LOCK = threading.Lock()


_KB_RATE_WAIT_MAX_S = max(0.0, S._env_float("FREEAGENT_KB_RATE_WAIT_MAX", 0.0)
                           or S._env_float("FREEAGENT_KB_DEADLINE", 8.0))


_KB_RATE_WAIT_MARGIN_S = max(0.0, S._env_float("FREEAGENT_KB_RATE_WAIT_MARGIN", 1.0))


_KB_LOCAL = threading.local()


def _kb_deadline_at() -> float | None:
    """このスレッドの締め切り（無ければ None）。"""
    value = getattr(_KB_LOCAL, "deadline_at", None)
    return value if isinstance(value, (int, float)) else None


def _kb_rate_acquire(host: str, interval: float) -> float:
    """HTTP実行の枠を原子的に取る。足りなければ待機秒を返し、枠を予約しない。"""
    with _KB_RATE_LOCK:
        now = time.monotonic()
        wait = _KB_RATE_NEXT.get(host, 0.0) - now
        if wait > 0:
            return wait
        _KB_RATE_NEXT[host] = now + interval
        return 0.0


def _kb_rate_reserve(host: str, interval: float, at: float) -> None:
    """指定時刻から叩けるよう枠を予約する（**待ってから叩く**ために使う）。"""
    with _KB_RATE_LOCK:
        _KB_RATE_NEXT[host] = max(_KB_RATE_NEXT.get(host, 0.0), at)


def _kb_fetch_json(url: str, payload: dict | None,
                   extra_headers: dict | None) -> tuple[dict | None, str]:
    """JSON を 1 回取る（POST は `payload` を渡す）。予算の話は `_kb_new_json` 側。"""
    # payload 無しのときは従来どおり位置引数 1 つで呼ぶ（呼び出し側の互換を壊さない）。
    if payload is None:
        return S.kb_json(url, extra_headers=extra_headers) if extra_headers else S.kb_json(url)
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return S.kb_json(url, extra_headers=headers, payload=payload)


def _kb_new_json(url: str, interval: float, payload: dict | None = None,
                 budget_key: str = "", extra_headers: dict | None = None) -> tuple[dict | None, str]:
    """ホスト別の予算を取ってから JSON を取る。`payload` を渡すと POST。

    `budget_key` は §5.18 / §5.19 用の逃げ道で、**同じホストに別サービスが同居する**ときや
    **1 回の取得で同じホストの複数エンドポイントを叩く**ときに、予算を分けるために使う
    （遮断の記憶は netloc のまま＝ホスト単位）。

    **枠が埋まっているときは、待てるなら待って叩く**（§5.11）。
    実測: 同じホストへ 2 本同時に投げると片方が「HTTP 429: ローカルのアクセス間隔制御」で
    落ちた（fan-out で成功 1/4）。**上流には枠が残っていた**（GitHub の `X-RateLimit-Remaining`
    が 9→8→7 と減る）＝ サーバーのローカル予算だけが原因だった。
    → 待たずに落とすのではなく**待つ**。ただし待つのは
    **(a) 待ち時間が `_KB_RATE_WAIT_MAX_S` 以下**、**(b) 締め切り内に収まる**、のときだけ。
    どちらかを満たさないときは待たずに 429 を返す（利用者を待たせない）。
    """
    host = urllib.parse.urlsplit(url).netloc
    if _kb_is_blocked(host):
        return None, f"blocked: {host} は一時停止中です"
    key = budget_key or host
    wait = S._kb_rate_acquire(key, interval)
    if wait:
        deadline_at = S._kb_deadline_at()
        left = (deadline_at - time.monotonic()) if deadline_at is not None else 0.0
        can_wait = (wait <= _KB_RATE_WAIT_MAX_S
                    and deadline_at is not None
                    and wait + _KB_RATE_WAIT_MARGIN_S <= left)
        if can_wait:
            time.sleep(wait)
            _kb_rate_reserve(key, interval, time.monotonic())
            return _kb_fetch_json(url, payload, extra_headers)
        return None, (f"HTTP 429: ローカルのアクセス間隔制御（あと {wait:.1f} 秒）。"
                      "同一ホストの枠を共有します")
    return _kb_fetch_json(url, payload, extra_headers)


def _kb_array(value, field: str) -> list:
    """上流配列を辞書キーや文字列として走査しない（欠落/Noneだけは空配列）。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} は配列ではありません")
    return value


def _kb_http_url(value: str) -> str:
    if not isinstance(value, str) or re.search(r"\s", value):
        raise ValueError("出典URLはHTTP(S)の文字列が必要です")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("出典URLの形式が不正です")
    return value


def _kb_paper_result(source: str, items: list[dict], **extra) -> dict:
    cites = []
    for item in items:
        summary = item.get("summary") or ""
        cite = _cite(source, item["title"], item["url"], year=item.get("year") or "",
                     doi=item.get("doi") or "", metadata_only=not bool(summary))
        cite["summary"] = summary
        cite["year"] = item.get("year") or ""
        for field in ("publication_types", "repository", "license", "licenses"):
            if item.get(field):
                cite[field] = item[field]
        if extra.get("attribution"):
            cite["attribution"] = extra["attribution"]
        cites.append(cite)
    return {"source": source, "items": items, "citations": cites,
            "error": "" if items else "該当なし", **extra}


def _kb_has_evidence(cite: dict) -> bool:
    return isinstance(cite, dict) and not cite.get("metadata_only") and bool(S.as_str(cite.get("summary")))


def _kb_citation_aliases(cite: dict) -> tuple[set[str], set[str]]:
    stored = cite.get("aliases") if isinstance(cite.get("aliases"), dict) else {}
    raw_dois = [cite.get("doi")] + (stored.get("dois") if isinstance(stored.get("dois"), list) else [])
    raw_urls = [cite.get("url")] + (stored.get("urls") if isinstance(stored.get("urls"), list) else [])
    dois = {re.sub(r"^https?://(?:dx\.)?doi\.org/", "", S.as_str(d).lower()) for d in raw_dois if S.as_str(d)}
    urls = {S.as_str(u) for u in raw_urls if S.as_str(u)}
    return dois, urls


def _kb_citation_key(cite: dict) -> tuple:
    """agentも明示DOIの異なる版をURL一致だけで潰さない。"""
    dois, _ = _kb_citation_aliases(cite)
    source = S.as_str(cite.get("source"))
    return (source, "doi", tuple(sorted(dois))) if dois else (source, "url", S.as_str(cite.get("url")))


def _kb_merge_citations(citations: list[dict]) -> list[dict]:
    """全入力のDOI/URL別名を先に索引化し、曖昧なURL-only引用を版に割り当てない。"""
    prepared = [(n, copy.deepcopy(c), *_kb_citation_aliases(c))
                for n, c in enumerate(citations) if isinstance(c, dict)]
    # 別名の連鎖も含め、到達するDOIを成分全体から求める（直接対応だけでは順序依存になる）。
    parent = {}
    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node
    for _, _, dois, urls in prepared:
        nodes = [("doi", d) for d in dois] + [("url", u) for u in urls]
        if nodes:
            root = find(nodes[0])
            for node in nodes[1:]:
                parent[find(node)] = root
    component_dois = {}
    for _, _, dois, _ in prepared:
        for doi in dois:
            component_dois.setdefault(find(("doi", doi)), set()).add(doi)
    url_dois = {u: component_dois.get(find(("url", u)), set())
                for _, _, _, urls in prepared for u in urls}
    groups = []
    for number, cite, dois, urls in prepared:
        claims = set().union(*(url_dois.get(u, set()) for u in urls))
        join_urls = {u for u in urls if len(url_dois.get(u, set())) <= 1}
        if not dois and len(claims) > 1:
            join_urls = set()
        matches = [g for g in groups if (dois and dois & g["dois"]) or
                   (not dois and not g["dois"] and urls & g["urls"]) or
                   (join_urls & g["join_urls"] and (not dois or not g["dois"] or g["dois"] == dois))]
        if len(dois | set().union(*(g["dois"] for g in matches))) > 1:
            matches = []
        group = {"dois": set(dois), "urls": set(urls), "join_urls": set(join_urls), "rows": [(number, cite)]}
        for match in matches:
            group["dois"].update(match["dois"])
            group["urls"].update(match["urls"])
            group["join_urls"].update(match["join_urls"])
            group["rows"].extend(match["rows"])
            groups.remove(match)
        groups.append(group)
    merged = []
    for group in sorted(groups, key=lambda g: min(n for n, _ in g["rows"])):
        rows = [r for _, r in sorted(group["rows"], key=lambda p: p[0])]
        selected = max(rows, key=lambda r: len(S.as_str(r.get("summary"))) if not r.get("metadata_only") else 0)
        cite = copy.deepcopy(selected)
        cite["aliases"] = {"dois": sorted(group["dois"]), "urls": sorted(group["urls"])}
        if not S.as_str(cite.get("doi")) and len(group["dois"]) == 1:
            cite["doi"] = next((r["doi"] for r in rows if S.as_str(r.get("doi"))), next(iter(group["dois"])))
        cite["metadata_only"] = not bool(S.as_str(cite.get("summary"))) or bool(cite.get("metadata_only"))
        if len(rows) > 1:
            providers = [p for r in rows for p in (r.get("providers") or [r.get("source")]) if p]
            cite["providers"] = list(dict.fromkeys(providers))
            cite["summary_source"] = cite.get("source") if not cite["metadata_only"] else ""
            metadata = []
            for row in rows:
                if isinstance(row.get("provider_metadata"), list):
                    metadata.extend(copy.deepcopy(row["provider_metadata"]))
                else:
                    metadata.append({k: copy.deepcopy(row[k]) for k in
                        ("source", "url", "doi", "license", "licenses", "publication_types", "resource_type", "repository", "attribution",
                         "file_license", "access_right", "summary_kind")
                        if k in row})
            cite["provider_metadata"] = metadata
            credits = [c for r in rows for c in ((r.get("attributions") or []) +
                       ([r["attribution"]] if r.get("attribution") else []))]
            if credits:
                cite["attributions"] = list(dict.fromkeys(credits))
        merged.append(cite)
    return merged


def _kb_object(value, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} はオブジェクトではありません")
    return value


def _open_metadata_text(value, limit: int = 600) -> str:
    """非本文HTMLとメールを除外する。除外だけの値は空で返す。"""
    from html.parser import HTMLParser
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], []
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "template", "noscript", "head"):
                self.hidden.append(tag)
            elif not self.hidden:
                self.parts.append(" ")
        def handle_endtag(self, tag):
            if tag in self.hidden:
                self.hidden = self.hidden[:self.hidden.index(tag)]
            elif not self.hidden:
                self.parts.append(" ")
        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)
    reader = Reader()
    reader.feed(S.as_str(value))
    reader.close()
    text = " ".join("".join(reader.parts).split())
    text = re.sub(r'(?:"[^"]*"|[^ @<>]+) *@ *[^ <>]+', " ", text)
    if "@" in text:
        return ""
    return S.truncate(" ".join(text.split()), limit)


def _kb_package_result(source: str, items: list[dict], attribution: str) -> dict:
    """パッケージ検索の正規化。説明文はレジストリ登録者の自己申告で、品質・安全性の審査結果ではない。"""
    cites = []
    for item in items:
        summary = item.get("summary") or ""
        cite = _cite(source, item["title"], item["url"], year=item.get("year") or "",
                     metadata_only=not bool(summary), attribution=attribution,
                     summary_kind="registry_description")
        cite["summary"] = summary
        for field in ("version", "downloads", "repository_url", "keywords"):
            if item.get(field):
                cite[field] = item[field]
        cites.append(cite)
    return {"source": source, "items": items, "citations": cites, "attribution": attribution,
            "error": "" if items else "該当なし"}


def _kb_generic_result(source: str, items: list[dict], attribution: str, **extra) -> dict:
    """検索系ソースの共通整形。summary が空の行は metadata_only（本文根拠に使わせない）。"""
    cites = []
    for item in items:
        summary = item.get("summary") or ""
        cite = _cite(source, item["title"], item["url"], metadata_only=not bool(summary),
                     attribution=attribution)
        cite["summary"] = summary
        for field in ("year", "doi", "identifier", "container", "authors", "kind", "version",
                      "package", "aliases", "severity", "detail_url", "license", "summary_kind"):
            if item.get(field):
                cite[field] = item[field]
        detail = {k: v for k, v in (item.get("extra") or {}).items() if v not in (None, "", [], {})}
        if detail:
            cite["extra"] = detail
        cites.append(cite)
    return {"source": source, "items": items, "citations": cites, "attribution": attribution,
            "error": "" if items else "該当なし", **extra}


def _ietf_terms(query: str) -> list[str]:
    """問いの語（2 文字以上）。日本語は語に割れないので 1 語として扱われる。"""
    return [w for w in re.split(r"[^\w./-]+", query) if len(w) >= 2]


EXPORTS = {
    "_KB_CACHE": _KB_CACHE,
    "_KB_CACHE_LOCK": _KB_CACHE_LOCK,
    "_KB_INFLIGHT": _KB_INFLIGHT,
    "_KB_BLOCKED": _KB_BLOCKED,
    "_KB_BLOCK_LOCK": _KB_BLOCK_LOCK,
    "_KB_BLOCK_S": _KB_BLOCK_S,
    "SOURCES": SOURCES,
    "_kb_block": _kb_block,
    "_kb_is_blocked": _kb_is_blocked,
    "_kb_socket_of": _kb_socket_of,
    "_kb_set_socket_timeout": _kb_set_socket_timeout,
    "_kb_content_length": _kb_content_length,
    "_kb_read_within": _kb_read_within,
    "kb_http": kb_http,
    "kb_json": kb_json,
    "_kb_cached": _kb_cached,
    "_plain_text": _plain_text,
    "_cite": _cite,
    "_KA_POOL_MAX": _KA_POOL_MAX,
    "_KA_POOL_IDLE": _KA_POOL_IDLE,
    "_KA_IDLE": _KA_IDLE,
    "_KA_LOCK": _KA_LOCK,
    "_KA_OPENS": _KA_OPENS,
    "_ka_close": _ka_close,
    "_ka_reset": _ka_reset,
    "_ka_ssl_context": _ka_ssl_context,
    "_ka_proxied": _ka_proxied,
    "_ka_allowed": _ka_allowed,
    "_KA_TRUSTED_HOSTS": _KA_TRUSTED_HOSTS,
    "_ka_usable_here": _ka_usable_here,
    "_ka_new_conn": _ka_new_conn,
    "_ka_set_timeout": _ka_set_timeout,
    "_ka_borrow": _ka_borrow,
    "_ka_give_back": _ka_give_back,
    "_ka_read_body": _ka_read_body,
    "kb_json_keepalive": kb_json_keepalive,
    "_ARXIV_LOCK": _ARXIV_LOCK,
    "_ARXIV_LAST": _ARXIV_LAST,
    "_ARXIV_MIN_INTERVAL": _ARXIV_MIN_INTERVAL,
    "_arxiv_throttle": _arxiv_throttle,
    "KB_BACKENDS": KB_BACKENDS,
    "KB_DEADLINE": KB_DEADLINE,
    "_kb_gather": _kb_gather,
    "knowledge_lookup": knowledge_lookup,
    "_literal_search": _literal_search,
    "_kb_new_cached": _kb_new_cached,
    "DEFAULT_SOURCES": DEFAULT_SOURCES,
    "KB_HEDGE_DELAY": KB_HEDGE_DELAY,
    "KB_HEDGE_MIN_REMAINING": KB_HEDGE_MIN_REMAINING,
    "_kb_source_result": _kb_source_result,
    "_KB_RATE_NEXT": _KB_RATE_NEXT,
    "_KB_RATE_LOCK": _KB_RATE_LOCK,
    "_KB_RATE_WAIT_MAX_S": _KB_RATE_WAIT_MAX_S,
    "_KB_RATE_WAIT_MARGIN_S": _KB_RATE_WAIT_MARGIN_S,
    "_KB_LOCAL": _KB_LOCAL,
    "_kb_deadline_at": _kb_deadline_at,
    "_kb_rate_acquire": _kb_rate_acquire,
    "_kb_rate_reserve": _kb_rate_reserve,
    "_kb_fetch_json": _kb_fetch_json,
    "_kb_new_json": _kb_new_json,
    "_kb_array": _kb_array,
    "_kb_http_url": _kb_http_url,
    "_kb_paper_result": _kb_paper_result,
    "_kb_has_evidence": _kb_has_evidence,
    "_kb_citation_aliases": _kb_citation_aliases,
    "_kb_citation_key": _kb_citation_key,
    "_kb_merge_citations": _kb_merge_citations,
    "_kb_object": _kb_object,
    "_open_metadata_text": _open_metadata_text,
    "_kb_package_result": _kb_package_result,
    "_kb_generic_result": _kb_generic_result,
    "_ietf_terms": _ietf_terms,
}
