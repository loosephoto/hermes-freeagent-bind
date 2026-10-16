"""HTTP (StreamableHTTP) トランスポートのオフライン回帰テスト。

ネットワークと実モデルを使わない（ループバックだけ・プロキシ未起動でも通る）。実測でクライアント
（Hermes の `mcp.client.streamable_http`）が壊れる形を固定する:
  * 単一メッセージに配列で応答する（`Failed to parse JSON response` で切れる）
  * 通知に 202 を返さない／要求に 202 を返す
  * initialize の応答に `mcp-session-id` を付けない
  * 未知のセッションに 200 を返す（状態が別セッションへ混ざる）
  * GET(SSE) の受け入れ条件（セッション必須・Accept 必須）
  * §9 のメソッド表を共有化したので、batch と単一で応答が食い違わないこと
"""
import http.client
import json
import socket
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-test-state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402

SESSION_HEADER = S.HTTP_SESSION_HEADER


class HttpCase(unittest.TestCase):
    """ループバックで実際に HTTP を往復させる土台（ポートは 0 で OS に任せる）。"""

    token = ""

    def setUp(self):
        S._harness_reset()
        self._saved_harness = os.environ.pop(S.HARNESS_MARKER_ENV, None)
        self.config = {"transport": "http", "host": "127.0.0.1", "port": 0, "path": "/mcp",
                       "token": self.token}
        self.server = S._http_build_server(self.config)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        finally:
            self.thread.join(timeout=5)
            S._harness_reset()
            if self._saved_harness is not None:
                os.environ[S.HARNESS_MARKER_ENV] = self._saved_harness

    # ---- 補助 ----
    def call(self, body=None, session_id=None, method="POST", path="/mcp", headers=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if session_id:
            hdrs[SESSION_HEADER] = session_id
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=None if body is None else json.dumps(body).encode("utf-8"),
                     headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        out = {"status": resp.status, "headers": {k.lower(): v for k, v in resp.getheaders()},
               "raw": raw, "json": json.loads(raw.decode("utf-8")) if raw else None}
        conn.close()
        return out

    def initialize(self, version="2025-11-25", name="test-client"):
        r = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": version, "capabilities": {},
                                  "clientInfo": {"name": name, "version": "1"}}})
        return r, r["headers"].get(SESSION_HEADER)


class TestHttpProtocol(HttpCase):
    def test_initialize_returns_single_object_with_session(self):
        r, sid = self.initialize()
        self.assertEqual(r["status"], 200)
        self.assertIsInstance(r["json"], dict, "単一メッセージはオブジェクトで返す（配列だと解析に失敗する）")
        self.assertEqual(r["json"]["id"], 1)
        self.assertEqual(r["json"]["result"]["serverInfo"]["version"], S.SERVER_VERSION)
        self.assertTrue(sid, "initialize の応答に mcp-session-id が無い")

    def test_protocol_version_is_negotiated(self):
        r, _ = self.initialize(version="2025-06-18")
        self.assertEqual(r["json"]["result"]["protocolVersion"], "2025-06-18")
        r, _ = self.initialize(version="1999-01-01")
        self.assertEqual(r["json"]["result"]["protocolVersion"], S.DEFAULT_PROTOCOL_VERSION)

    def test_notification_gets_202_without_body(self):
        _, sid = self.initialize()
        r = self.call({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid)
        self.assertEqual(r["status"], 202)
        self.assertEqual(r["raw"], b"", "通知の応答に本文を載せてはいけない")

    def test_tools_list_returns_every_tool(self):
        _, sid = self.initialize()
        r = self.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id=sid)
        tools = r["json"]["result"]["tools"]
        self.assertEqual(len(tools), len(S.TOOLS))
        self.assertEqual({t["name"] for t in tools}, {t["name"] for t in S.TOOLS})

    def test_tools_call_without_llm(self):
        _, sid = self.initialize()
        r = self.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "freeagent_think",
                                  "arguments": {"thought": "HTTP テスト", "thought_number": 1,
                                                "question": "疎通"}}}, session_id=sid)
        result = r["json"]["result"]
        self.assertFalse(result["isError"], result["structuredContent"].get("error"))
        self.assertTrue(result["structuredContent"]["session_id"])
        self.assertTrue(result["content"][0]["text"])

    def test_unknown_tool_is_error_with_next_action(self):
        _, sid = self.initialize()
        r = self.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                       "params": {"name": "freeagent_nope", "arguments": {}}}, session_id=sid)
        result = r["json"]["result"]
        self.assertTrue(result["isError"])
        self.assertTrue(result["structuredContent"]["next_action"])

    def test_unknown_method_returns_jsonrpc_error(self):
        _, sid = self.initialize()
        r = self.call({"jsonrpc": "2.0", "id": 5, "method": "no/such/method"}, session_id=sid)
        self.assertEqual(r["json"]["error"]["code"], -32601)

    def test_batch_returns_array(self):
        _, sid = self.initialize()
        r = self.call([{"jsonrpc": "2.0", "id": 6, "method": "ping"},
                       {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}], session_id=sid)
        self.assertIsInstance(r["json"], list)
        self.assertEqual([m["id"] for m in r["json"]], [6, 7])

    def test_batch_of_notifications_is_202(self):
        _, sid = self.initialize()
        r = self.call([{"jsonrpc": "2.0", "method": "notifications/initialized"}], session_id=sid)
        self.assertEqual(r["status"], 202)
        self.assertEqual(r["raw"], b"")

    def test_invalid_json_is_parse_error(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/mcp", body=b"{not json", headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        self.assertEqual(resp.status, 400)
        self.assertEqual(payload["error"]["code"], -32700)

    def test_session_lifecycle(self):
        _, sid = self.initialize()
        # 未知のセッションは 404（状態を混ぜない）
        r = self.call({"jsonrpc": "2.0", "id": 8, "method": "ping"}, session_id="0" * 32)
        self.assertEqual(r["status"], 404)
        self.assertEqual(r["json"]["error"]["code"], -32001)
        # セッション無しの要求は通す（他のクライアントの互換）
        r = self.call({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["json"]["result"], {})
        # DELETE で終了し、以後は 404
        r = self.call(method="DELETE", session_id=sid)
        self.assertEqual(r["status"], 204)
        r = self.call({"jsonrpc": "2.0", "id": 10, "method": "ping"}, session_id=sid)
        self.assertEqual(r["status"], 404)

    def test_unknown_path_is_404(self):
        r = self.call({"jsonrpc": "2.0", "id": 11, "method": "ping"}, path="/other")
        self.assertEqual(r["status"], 404)

    def test_get_requires_session_and_accept(self):
        r = self.call(method="GET")
        self.assertEqual(r["status"], 400)
        _, sid = self.initialize()
        r = self.call(method="GET", session_id=sid, headers={"Accept": "application/json"})
        self.assertEqual(r["status"], 406)

    def test_sse_delivers_queued_notification(self):
        """§8.6 の警告（other のとき）が GET の SSE で届く。"""
        os.environ[S.HARNESS_MARKER_ENV] = "other"
        _, sid = self.initialize(name="claude-code")
        r = self.call({"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid)
        self.assertEqual(r["status"], 202)
        queue = S._HTTP_SESSIONS.get(sid, {}).get("queue")
        self.assertEqual(len(queue or []), 1,
                         f"§8.6 の通知が待ち行列に入っていない（harness={S._HARNESS.get('info')}）")

        # 生ソケットで読む。http.client の read(amt) は content-length の無い応答で
        # amt バイト揃うまで待つ実装があり、SSE の 1 イベント（数百バイト）では返らない。
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=15)
        sock.sendall(("GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nAccept: text/event-stream\r\n"
                      f"{SESSION_HEADER}: {sid}\r\nConnection: close\r\n\r\n").encode())
        sock.settimeout(10)
        data = b""
        deadline = time.time() + 10
        while time.time() < deadline:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n\n" in data.split(b"\r\n\r\n", 1)[-1]:
                break
        sock.close()
        head, _, body = data.partition(b"\r\n\r\n")
        self.assertIn(b"200", head.split(b"\r\n")[0], head[:80])
        self.assertIn(b"text/event-stream", head.lower(), head[:200])
        data = body
        text = data.decode("utf-8", "replace")
        self.assertIn("event: message", text, f"SSE の通知が届いていない: {text!r}")
        payload = json.loads(text.split("data: ", 1)[1].split("\n", 1)[0])
        self.assertEqual(payload["method"], "notifications/message")
        self.assertEqual(payload["params"]["data"]["harness"], "other")


    def test_harness_header_marker_declares_hermes(self):
        """HTTP では env が渡らないので、目印はヘッダで受ける（stdio は env）。"""
        r = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "mcp", "version": "1"}}},
                      headers={S.HARNESS_MARKER_HEADER: "hermes"})
        self.assertEqual(r["status"], 200)
        self.assertEqual(S._HARNESS["info"]["kind"], "hermes")
        self.assertEqual(S._HARNESS["info"]["source"], "header")
        self.assertIsNone(S.harness_log_notification(), "目印があるのに警告を送ろうとしている")

    def test_header_marker_beats_env_marker(self):
        os.environ[S.HARNESS_MARKER_ENV] = "claude-code"
        r = self.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "mcp", "version": "1"}}},
                      headers={S.HARNESS_MARKER_HEADER: "hermes"})
        self.assertEqual(r["status"], 200)
        self.assertEqual(S._HARNESS["info"]["kind"], "hermes")

    def test_detect_harness_accepts_marker_argument(self):
        info = S.detect_harness({}, env={}, marker="hermes")
        self.assertEqual((info["kind"], info["source"]), ("hermes", "header"))
        info = S.detect_harness({}, env={"FREEAGENT_HARNESS": "hermes"})
        self.assertEqual((info["kind"], info["source"]), ("hermes", "env"))
        info = S.detect_harness({}, env={}, marker="claude-code")
        self.assertEqual(info["kind"], "other")

class TestHttpToken(HttpCase):
    token = "sekrit-token"

    def test_token_required(self):
        r = self.call({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(r["status"], 401)
        r = self.call({"jsonrpc": "2.0", "id": 2, "method": "ping"},
                      headers={"Authorization": f"Bearer {self.token}"})
        self.assertEqual(r["status"], 200)
        r = self.call({"jsonrpc": "2.0", "id": 3, "method": "ping"},
                      headers={"x-freeagent-token": self.token})
        self.assertEqual(r["status"], 200)
        r = self.call({"jsonrpc": "2.0", "id": 4, "method": "ping"},
                      headers={"Authorization": "Bearer wrong"})
        self.assertEqual(r["status"], 401)


class TestHttpDispatchSharing(unittest.TestCase):
    """§9 のメソッド表を共有化したので、単一経路と batch 経路が同じ応答を返すこと。"""

    def setUp(self):
        S._harness_reset()

    def test_replies_for_matches_single_and_batch(self):
        msg = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(S._replies_for(msg), [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        self.assertEqual(S._replies_for({"jsonrpc": "2.0", "method": "ping"}), [], "通知に応答しない")
        self.assertEqual(S._replies_for("junk")[0]["error"]["code"], -32600)
        self.assertEqual(S._replies_for({"jsonrpc": "2.0", "id": 2, "method": "nope"})[0]["error"]["code"], -32601)

    def test_dispatch_batch_and_message_agree(self):
        captured: list = []
        original = S.write_line
        S.write_line = captured.append
        try:
            S._dispatch_batch([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
            batch_reply = json.loads(captured.pop())
            S._dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "ping"}, _NoPool())
            single_reply = json.loads(captured.pop())
        finally:
            S.write_line = original
        self.assertEqual(batch_reply, [single_reply], "batch と単一で応答が食い違っている")

    def test_http_process_accepts_notifications_and_queues_them(self):
        session = {"queue": [], "cv": threading.Condition(), "closed": False}
        payload, accepted = S._http_process({"jsonrpc": "2.0", "id": 1, "method": "ping"}, session)
        self.assertFalse(accepted)
        self.assertEqual(payload["result"], {})
        payload, accepted = S._http_process([{"jsonrpc": "2.0", "method": "notifications/initialized"}], session)
        self.assertTrue(accepted)
        self.assertIsNone(payload)

    def test_http_config_defaults_to_stdio(self):
        cfg = S.http_config([])
        self.assertEqual(cfg["transport"], "stdio")
        cfg = S.http_config(["--transport", "http", "--port", "1234", "--path", "mcp"])
        self.assertEqual((cfg["transport"], cfg["port"], cfg["path"]), ("http", 1234, "/mcp"))
        cfg = S.http_config(["--unknown-flag", "x"])     # 未知の引数で起動を止めない
        self.assertEqual(cfg["transport"], "stdio")

    def test_sessions_prune_by_ttl(self):
        sid = S._http_session_new()
        self.assertIsNotNone(S._http_session_get(sid))
        with S._HTTP_SESSIONS_LOCK:
            S._HTTP_SESSIONS[sid]["seen"] = S.now_ts() - (S.HTTP_SESSION_TTL + 10)
        self.assertGreaterEqual(S._http_sessions_prune(), 1)
        self.assertIsNone(S._http_session_get(sid))


class _NoPool:
    """`_dispatch_message` は tools/call 以外では pool を使わない（未使用でも渡せる形にする）。"""

    def submit(self, *a, **k):  # pragma: no cover - tools/call では使わない
        raise AssertionError("tools/call 以外で pool.submit を呼んでいる")


if __name__ == "__main__":
    unittest.main()
