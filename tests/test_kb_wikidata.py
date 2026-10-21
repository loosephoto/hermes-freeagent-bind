"""§5.2.1 接続の再利用（wikidata の api.php）の回帰テスト。

`kb_wikidata` は api.php を最大 3 回直列に呼ぶ（wbsearchentities → wbgetentities(claims) →
wbgetentities(labels)）。ここでは**接続が 1 本で通ること**を固定する（実測: 3 連続の中央値が
3.732 秒 → 3.096 秒・-0.636 秒）。

固定する性質:
  * 連続呼び出しで**同じ接続**が使われる（新規接続は 1 本だけ）
  * 在庫の接続が失効していたら**作り直して 1 回だけやり直す**（遮断しない）
  * 2 回失敗したら**遮断**する（`kb_http` と同じ扱い）
  * `will_close` の応答は在庫へ返さず閉じる
  * 読み取りごとにソケットタイムアウトを**残り予算へ張り直す**（1 回の recv が予算を超えない）
  * Content-Length が無い（chunked）応答も**読み切る**（重複ヒューリスティックで途中終了しない）
  * `FREEAGENT_KB_KEEPALIVE=0` / `FREEAGENT_KB_POOL_MAX=0` では**共用経路へ委譲**する
  * `kb_wikidata` がこの経路を使うこと

ネットワークは一切使わない（偽の接続を差し込む）。
"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-test-state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402

WIKI_URL = "https://www.wikidata.org/w/api.php?action=wbsearchentities&search=foo&language=ja"


class _FakeSock:
    """`settimeout` だけを記録する偽のソケット。"""

    def __init__(self, timeout=5.0):
        self.timeout = timeout
        self.calls = []

    def settimeout(self, value):
        self.timeout = value
        self.calls.append(value)


class _FakeResp:
    """`_ka_read_body` が使う口だけを持つ偽の応答。"""

    def __init__(self, payload, *, sock=None, will_close=False, headers=None, status=200,
                 chunked=False):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.status = status
        self._body = body
        self._sent = False
        head = dict(headers or {})
        if not chunked and "Content-Length" not in head:
            head["Content-Length"] = str(len(body))
        self.headers = head
        self.will_close = will_close
        # `_kb_socket_of` は fp.raw._sock を見る（無ければ None を返す）。
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=sock))

    def read1(self, _n):
        if self._sent:
            return b""
        self._sent = True
        return self._body


class _SplitResp(_FakeResp):
    """本文を**同じ内容の 2 チャンク**で返す偽の応答（重複ヒューリスティックの検出用）。

    `_kb_read_within` の「同じ内容が 2 回続いたら終端」という推測で止まると、
    ここで本文の後半が欠ける。
    """

    def __init__(self, payload, **kw):
        super().__init__(payload, chunked=True, **kw)
        self._body = self._body[: len(self._body) // 2]
        self._count = 0

    def read1(self, _n):
        self._count += 1
        if self._count > 2:
            return b""
        return self._body


class _FakeConn:
    """`kb_json_keepalive` が使う口だけを持つ偽の接続。

    `payloads` は応答の並び（尽きたら最後を繰り返す）。`fail_request` を渡すと
    **次の 1 回だけ** `request` がそれを投げる（在庫の失効を模す）。
    """

    def __init__(self, payloads, *, fail_request=None, fail_read=None, **resp_kw):
        self.timeout = 5.0
        self.sock = _FakeSock()
        self.closed = False
        self.requests = []
        self._payloads = list(payloads) or [{"ok": True}]
        self._fail_request = fail_request
        self._fail_read = fail_read
        self._resp_kw = resp_kw

    def request(self, method, target, headers=None):
        if self._fail_request is not None:
            exc = self._fail_request
            self._fail_request = None
            raise exc
        self.requests.append((method, target, headers))

    def getresponse(self):
        if self._fail_read is not None:
            exc = self._fail_read
            self._fail_read = None
            raise exc
        payload = self._payloads.pop(0) if len(self._payloads) > 1 else self._payloads[0]
        return _FakeResp(payload, sock=self.sock, **self._resp_kw)

    def close(self):
        self.closed = True


class _DeadConn(_FakeConn):
    """`request` が必ず失敗する接続（接続不可を模す）。"""

    def request(self, method, target, headers=None):
        raise ConnectionResetError("connect refused")


class _Factory:
    """`_ka_new_conn` の代わり（作った接続を数える）。"""

    def __init__(self, conns):
        self.conns = list(conns)
        self.count = 0

    def __call__(self, _parts, _budget):
        self.count += 1
        return self.conns.pop(0) if len(self.conns) > 1 else self.conns[0]


class KeepAliveCase(unittest.TestCase):
    """プールと遮断表を毎回まっさらにする土台。"""

    def setUp(self):
        S._ka_reset()
        S._KB_BLOCKED.clear()
        # プロキシ env の有無でテストが揺れないよう、判定を固定する。
        patcher = mock.patch.object(S, "_ka_proxied", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        deadline = mock.patch.object(S, "_kb_deadline_at", return_value=None)
        deadline.start()
        self.addCleanup(deadline.stop)

    def tearDown(self):
        S._ka_reset()
        S._KB_BLOCKED.clear()


class TestPoolReuse(KeepAliveCase):
    """接続が 1 本で通ること（＝この最適化の目的そのもの）。"""

    def test_three_calls_share_one_connection(self):
        conn = _FakeConn([{"call": 1}, {"call": 2}, {"call": 3}])
        factory = _Factory([conn])
        with mock.patch.object(S, "_ka_new_conn", factory):
            got = [S.kb_json_keepalive(WIKI_URL)[0] for _ in range(3)]
        self.assertEqual(got, [{"call": 1}, {"call": 2}, {"call": 3}])
        self.assertEqual(factory.count, 1, "接続が 1 本で通っていない")
        self.assertFalse(conn.closed)
        self.assertEqual(len(conn.requests), 3)
        # 2 回目以降は在庫から借りている（= 新規接続を作っていない）。
        self.assertEqual([r[0] for r in conn.requests], ["GET", "GET", "GET"])
        self.assertIn("wbsearchentities", conn.requests[0][1])

    def test_stale_pooled_connection_is_rebuilt(self):
        """在庫の接続が失効していたら作り直して 1 回だけやり直す（遮断しない）。"""
        stale = _FakeConn([{"never": True}],
                          fail_request=ConnectionResetError("stale pooled connection"))
        S._ka_give_back("https://www.wikidata.org", stale)
        fresh = _FakeConn([{"ok": True}])
        factory = _Factory([fresh])
        with mock.patch.object(S, "_ka_new_conn", factory):
            data, err = S.kb_json_keepalive(WIKI_URL)
        self.assertEqual(data, {"ok": True}, err)
        self.assertEqual(err, "")
        self.assertTrue(stale.closed, "失効した接続を閉じていない")
        self.assertEqual(factory.count, 1, "作り直していない")
        self.assertFalse(S._kb_is_blocked("www.wikidata.org"), "1 回の失効で遮断してはいけない")

    def test_two_failures_block_the_host(self):
        """2 回失敗したら遮断する（`kb_http` と同じ扱い）。"""
        factory = _Factory([_DeadConn([{"never": True}]), _DeadConn([{"never": True}])])
        with mock.patch.object(S, "_ka_new_conn", factory):
            data, err = S.kb_json_keepalive(WIKI_URL)
        self.assertIsNone(data)
        self.assertIn("ConnectionResetError", err)
        self.assertEqual(factory.count, 2, "1 回で諦めている（作り直しの 1 回が無い）")
        self.assertTrue(S._kb_is_blocked("www.wikidata.org"), "2 回失敗したのに遮断していない")

    def test_will_close_response_is_not_pooled(self):
        """`Connection: close` の応答は在庫へ返さない（未読を残さない）。"""
        conn = _FakeConn([{"ok": True}], will_close=True)
        factory = _Factory([conn])
        with mock.patch.object(S, "_ka_new_conn", factory):
            S.kb_json_keepalive(WIKI_URL)
        self.assertTrue(conn.closed)
        self.assertFalse(S._KA_IDLE.get("https://www.wikidata.org"), "閉じる接続を在庫に入れた")
        with mock.patch.object(S, "_ka_new_conn", factory):
            S.kb_json_keepalive(WIKI_URL)
        self.assertEqual(factory.count, 2, "在庫から借りてしまった")

    def test_429_is_retried_on_the_pooled_connection(self):
        """429 は遮断せず再試行する（在庫の接続をそのまま使う）。"""
        conn = _FakeConn([{"ok": True}])
        factory = _Factory([conn])
        first = _FakeResp({"never": True}, status=429, headers={"Retry-After": "0"})
        calls = {"n": 0}
        real_get = conn.getresponse

        def scripted_get():
            calls["n"] += 1
            if calls["n"] == 1:
                return first
            return real_get()

        def scripted_new(_parts, _budget):
            factory.count += 1
            return conn

        with mock.patch.object(S, "_ka_new_conn", scripted_new):
            conn.getresponse = scripted_get
            data, err = S.kb_json_keepalive(WIKI_URL)
        self.assertEqual(data, {"ok": True}, err)
        self.assertEqual(factory.count, 1, "429 の再試行で接続を張り直している")
        self.assertFalse(S._kb_is_blocked("www.wikidata.org"), "429 で遮断してはいけない")

    def test_give_back_closes_when_pool_is_full(self):
        """在庫が満杯なら閉じる（在庫が無制限に増えない）。"""
        origin = "https://www.wikidata.org"
        conns = [_FakeConn([{"ok": True}]) for _ in range(S._KA_POOL_MAX + 1)]
        for conn in conns:
            S._ka_give_back(origin, conn)
        pooled = S._KA_IDLE.get(origin) or []
        self.assertEqual(len(pooled), S._KA_POOL_MAX, "在庫の上限を超えている")
        self.assertTrue(conns[-1].closed, "満杯の在庫でも接続を閉じていない")
        self.assertFalse(conns[0].closed)

    def test_borrowed_connection_leaves_the_pool(self):
        """貸出中は在庫から外す（同時実行が在庫の取り合いにならない）。"""
        origin = "https://www.wikidata.org"
        first = _FakeConn([{"ok": True}])
        S._ka_give_back(origin, first)
        self.assertIs(S._ka_borrow(origin), first)
        self.assertIsNone(S._ka_borrow(origin), "同じ接続を 2 回貸し出した")

    def test_stale_idle_connection_is_dropped(self):
        """`FREEAGENT_KB_POOL_IDLE` を過ぎた在庫は捨てる（失効した接続を貸さない）。"""
        origin = "https://www.wikidata.org"
        conn = _FakeConn([{"ok": True}])
        # 偽の接続は `HTTPConnection` ではない（型注釈だけの都合）。
        S._KA_IDLE[origin] = [(conn, time.monotonic() - S._KA_POOL_IDLE - 1.0)]  # type: ignore[assignment]
        self.assertIsNone(S._ka_borrow(origin))
        self.assertTrue(conn.closed)


class TestDelegation(KeepAliveCase):
    """切り分け用の逃げ道（無効化・プロキシ環境）が効くこと。"""

    def test_disabled_delegates_to_shared_path(self):
        with mock.patch.dict(os.environ, {"FREEAGENT_KB_KEEPALIVE": "0"}), \
             mock.patch.object(S, "kb_json", return_value=({"ok": True}, "")) as shared, \
             mock.patch.object(S, "_ka_new_conn", _Factory([_FakeConn([{"ok": True}])])) as factory:
            data, err = S.kb_json_keepalive(WIKI_URL)
        self.assertEqual(data, {"ok": True})
        self.assertEqual(err, "")
        shared.assert_called_once()
        self.assertEqual(factory.count, 0, "無効化したのに接続を作った")

    def test_pool_max_zero_delegates_to_shared_path(self):
        with mock.patch.object(S, "_KA_POOL_MAX", 0), \
             mock.patch.object(S, "kb_json", return_value=({"ok": True}, "")) as shared, \
             mock.patch.object(S, "_ka_new_conn", _Factory([_FakeConn([{"ok": True}])])) as factory:
            S.kb_json_keepalive(WIKI_URL)
        shared.assert_called_once()
        self.assertEqual(factory.count, 0)

    def test_proxied_environment_delegates_to_shared_path(self):
        with mock.patch.object(S, "_ka_proxied", return_value=True), \
             mock.patch.object(S, "kb_json", return_value=({"ok": True}, "")) as shared:
            S.kb_json_keepalive(WIKI_URL)
        shared.assert_called_once()


class TestReadBody(KeepAliveCase):
    """読み取りの打ち切りとタイムアウトの張り直し。"""

    def test_tightens_socket_timeout_to_remaining_budget(self):
        sock = _FakeSock(5.0)
        resp = _FakeResp({"ok": True}, sock=sock)
        S._ka_read_body(resp, time.monotonic() + 0.05)
        self.assertTrue(sock.calls, "settimeout を呼んでいない")
        self.assertLess(sock.timeout, 5.0, "残り予算へ張り直していない")
        self.assertLessEqual(sock.timeout, 0.2)

    def test_reads_past_duplicate_chunks_without_content_length(self):
        """Content-Length が無い応答を重複ヒューリスティックで途中終了しない。

        `_SplitResp` は本文を**同じ内容の 2 チャンク**で返す（`abab…` を半分ずつ）。
        「同じ内容が 2 回続いたら終端」という推測で止まると、本文が半分で切れる。
        """
        body = ("ab" * 16).encode("utf-8")
        got = S._ka_read_body(_SplitResp(body), time.monotonic() + 5.0)
        self.assertEqual(got, body.decode("utf-8"))
        self.assertEqual(len(got), len(body))

    def test_content_length_stops_the_read(self):
        sock = _FakeSock()
        resp = _FakeResp({"ok": True}, sock=sock)
        got = S._ka_read_body(resp, time.monotonic() + 5.0)
        self.assertEqual(json.loads(got), {"ok": True})

    def test_budget_overrun_raises_timeout(self):
        """予算が尽きたら TimeoutError（呼び出し側が失敗として扱う）。"""

        class _Slow(_FakeResp):
            def read1(self, _n):
                time.sleep(0.2)
                return b"x"

        with self.assertRaises(TimeoutError):
            S._ka_read_body(_Slow({"ok": True}), time.monotonic() + 0.05)


class TestWikidataWiring(KeepAliveCase):
    """`kb_wikidata` がこの経路を使うこと（配線の回帰）。"""

    def test_kb_wikidata_uses_keepalive_json(self):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {"search": []}, ""

        with mock.patch.object(S, "kb_json_keepalive", side_effect=fake):
            S.kb_wikidata("wiring-check", limit=1)
        self.assertTrue(calls, "kb_json_keepalive を呼んでいない")
        self.assertIn("language=ja", calls[0])
        self.assertIn("wbsearchentities", calls[0])

    def test_kb_wikidata_keeps_shared_path_when_disabled(self):
        """無効化したときは `kb_json`（共用経路）を通ること。"""
        with mock.patch.dict(os.environ, {"FREEAGENT_KB_KEEPALIVE": "0"}), \
             mock.patch.object(S, "kb_json", return_value=({"search": []}, "")) as shared, \
             mock.patch.object(S, "kb_json_keepalive", wraps=S.kb_json_keepalive):
            S.kb_wikidata("disabled-check", limit=1)
        self.assertTrue(shared.called, "共用経路へ委譲していない")


if __name__ == "__main__":
    unittest.main()
