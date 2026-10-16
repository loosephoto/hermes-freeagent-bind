"""実クライアント経路（HTTP / StreamableHTTP）の疎通検査。

サーバーを **HTTP モードで別プロセス起動**し、標準ライブラリの `http.client` で
initialize → tools/list → tools/call を往復させる。ネットワークとモデルは使わない
（必須引数を欠いた呼び出し＝エラー経路と、LLM を呼ばない freeagent_think で確認する）。

検査する破綻（いずれも実測でクライアントが壊れる形）:
  * 単一メッセージに**配列**で応答する（`mcp.client.streamable_http` は
    `Failed to parse JSON response` で切る。Hermes のツール呼び出しが全部失敗する）
  * 通知に 202 を返さない（クライアントは要求として扱い、応答待ちで固まる）
  * 要求に 202 を返す（クライアントは「応答が来ない」として失敗にする）
  * initialize の応答に `mcp-session-id` を付けない（以降の要求を関連付けられない）
  * 未知のセッションに 200 を返す（状態が別セッションへ混ざる）
  * 例外を漏らして HTTP 500 の本文が JSON-RPC でない
"""
from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        FAILURES.append(message)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def call(port: int, body, session_id: str | None = None, method: str = "POST",
         path: str = "/mcp", headers: dict | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    hdrs = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        hdrs["mcp-session-id"] = session_id
    if headers:
        hdrs.update(headers)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    conn.request(method, path, body=payload, headers=hdrs)
    resp = conn.getresponse()
    raw = resp.read()
    out = {"status": resp.status, "headers": dict(resp.getheaders()), "raw": raw}
    conn.close()
    if raw:
        try:
            out["json"] = json.loads(raw.decode("utf-8"))
        except Exception:
            out["json"] = None
    else:
        out["json"] = None
    return out


def wait_port(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> int:
    port = free_port()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["FREEAGENT_STATE_DIR"] = os.path.join(os.environ.get("TMPDIR", ROOT), "smoke-http-state")
    env["FREEAGENT_HARNESS"] = "hermes"     # 判定の警告を出さない（hermes は何も出さない）
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "freeagent_bind", "server.py"),
                             "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        if not wait_port(port):
            err = proc.stderr.read(2000).decode("utf-8", "replace") if proc.stderr else ""
            print(f"✗ HTTP モードが待受に入りませんでした\n{err}")
            return 1

        # 1) initialize（単一メッセージ → オブジェクト 1 個）
        r = call(port, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                   "clientInfo": {"name": "smoke-http", "version": "1"}}})
        check(r["status"] == 200, f"initialize が {r['status']}")
        check(isinstance(r["json"], dict), f"initialize の本文が配列/不正: {type(r['json']).__name__}")
        init = r["json"] if isinstance(r["json"], dict) else {}
        check(init.get("result", {}).get("protocolVersion") == "2025-11-25",
              f"protocolVersion を交渉していない: {init.get('result', {}).get('protocolVersion')}")
        check(init.get("result", {}).get("serverInfo", {}).get("name") == "hermes-freeagent-bind",
              "serverInfo.name が違う")
        sid = r["headers"].get("mcp-session-id") or r["headers"].get("Mcp-Session-Id")
        check(bool(sid), "initialize の応答に mcp-session-id が無い")

        # 2) 通知 → 202（本文なし）
        r = call(port, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session_id=sid)
        check(r["status"] == 202, f"通知に 202 を返していない: {r['status']}")
        check(not r["raw"], "通知の応答に本文がある")

        # 3) tools/list（セッション付き）
        r = call(port, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session_id=sid)
        tools = (r["json"] or {}).get("result", {}).get("tools") or []
        check(len(tools) == 11, f"tools/list が {len(tools)} 件")

        # 4) tools/call（LLM を呼ばない freeagent_think）
        r = call(port, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "freeagent_think",
                                   "arguments": {"thought": "HTTP 疎通の確認", "thought_number": 1,
                                                 "question": "疎通"}}}, session_id=sid)
        result = (r["json"] or {}).get("result") or {}
        sc = result.get("structuredContent") or {}
        check(result.get("isError") is False, f"freeagent_think が失敗: {sc.get('error')}")
        check(bool(sc.get("session_id")), "台帳の session_id が返っていない")

        # 5) エラー経路（未知のツール）も JSON-RPC として返る
        r = call(port, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "freeagent_no_such_tool", "arguments": {}}}, session_id=sid)
        result = (r["json"] or {}).get("result") or {}
        check(result.get("isError") is True, "未知のツールが isError になっていない")
        check(bool((result.get("structuredContent") or {}).get("next_action")),
              "未知のツールに next_action が無い（規約 22）")

        # 6) batch → 配列
        r = call(port, [{"jsonrpc": "2.0", "id": 5, "method": "ping"},
                        {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}], session_id=sid)
        check(isinstance(r["json"], list) and len(r["json"]) == 2,
              f"batch の応答が配列 2 件でない: {type(r['json']).__name__}")

        # 7) 未知のセッションは 404
        r = call(port, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
                 session_id="0123456789abcdef0123456789abcdef")
        check(r["status"] == 404, f"未知のセッションに {r['status']} を返した")
        check(((r["json"] or {}).get("error") or {}).get("code") == -32001,
              "未知のセッションのエラーコードが違う")

        # 8) セッション無しでも要求は通る（他クライアントの互換のため）
        r = call(port, {"jsonrpc": "2.0", "id": 8, "method": "ping"})
        check(r["status"] == 200 and (r["json"] or {}).get("result") == {},
              f"セッション無しの ping が通らない: {r['status']}")

        # 9) GET（SSE）はセッション必須
        r = call(port, None, method="GET")
        check(r["status"] == 400, f"セッション無しの GET に {r['status']}")

        # 10) DELETE → 204、以後そのセッションは 404
        r = call(port, None, method="DELETE", session_id=sid)
        check(r["status"] == 204, f"DELETE が {r['status']}")
        r = call(port, {"jsonrpc": "2.0", "id": 9, "method": "ping"}, session_id=sid)
        check(r["status"] == 404, f"終了したセッションが {r['status']} で通った")

        # 11) 待受パス以外は 404
        r = call(port, {"jsonrpc": "2.0", "id": 10, "method": "ping"}, path="/nope")
        check(r["status"] == 404, f"未知のパスに {r['status']}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if FAILURES:
        print("✗ smoke_http: 失敗")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("HTTP 疎通チェック OK（initialize / 通知 202 / tools/list / tools/call / batch / セッション / GET / DELETE）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
