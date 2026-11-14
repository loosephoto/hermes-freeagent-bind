"""常駐 HTTP の実 MCP 疎通を素の urllib で確かめる（issue #13 の検証スクリプトを同梱化）。

常駐配線の再生成・再起動のあとに、**実経路**（initialize → notifications/initialized →
tools/list → tools/call）を 1 往復ずつ通して「登録は成功したが応答しない」無言状態を弾く。
依存ゼロ（標準ライブラリのみ・ネットワークはループバックへの HTTP だけ）。

```bash
python scripts/verify_resident.py                       # http://127.0.0.1:8790/mcp を既定で検査
python scripts/verify_resident.py --url http://127.0.0.1:8790/mcp --tool freeagent_models
python scripts/verify_resident.py --tool freeagent_think --args '{"steps": ["p"]}'
```

判定（issue #13「正常とみなす基準」）:
  * initialize に `serverInfo.version` と `mcp-session-id` が付く
  * 通知（notifications/initialized）に 202 が返る
  * `tools/list` が 1 件以上のツールを返す
  * `tools/call` が `isError` でない結果を返す（session 失効の outcome UNKNOWN は**再送 1 回**で
    回復する・再起動直後の既知の挙動。再送でも失敗したら exit 1）

注意: 常駐 HTTP は 1 プロセスを複数クライアントで共有するため、このスクリプトの initialize が
ハーネス判定を「resident-verify」として上書きします（上書きはサーバーの stderr に 1 行出ます・
§8.6）。Hermes は次回接続時に再度 initialize するので、実用上は自動で戻ります。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8790/mcp"
DEFAULT_TOOL = "freeagent_models"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "resident-verify"


def _post(url: str, payload: dict, session_id: str = "", timeout: float = 30.0):
    """JSON-RPC 1 件を POST する。`(status, headers, 応答 JSON or None)` を返す。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, dict(resp.headers), body
    except urllib.error.HTTPError as exc:
        body = exc.read() if hasattr(exc, "read") else b""
        return exc.code, dict(exc.headers or {}), body


def _rpc(method: str, params: dict | None, _id: int | None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if _id is not None:
        payload["id"] = _id
    return payload


def verify(url: str = DEFAULT_URL, tool: str = DEFAULT_TOOL, tool_args: dict | None = None,
           timeout: float = 30.0) -> tuple[int, list[str]]:
    """実 MCP 疎通を通す。`(exit_code, 報告行のリスト)` を返す（exit 0 = 正常）。"""
    lines: list[str] = []

    status, headers, body = _post(url, _rpc("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
    }, 1), timeout=timeout)
    init = json.loads(body.decode("utf-8")) if body else None
    if status != 200 or not isinstance(init, dict) or "result" not in init:
        return 1, [f"FAIL: initialize (HTTP {status}): {(body[:200].decode('utf-8', 'replace') if body else '本文なし')}"]
    info = (init.get("result") or {}).get("serverInfo") or {}
    lines.append("serverInfo : %s %s" % (info.get("name"), info.get("version")))
    sid = (headers.get("Mcp-Session-Id") or headers.get("mcp-session-id") or "").strip()
    if not sid:
        return 1, lines + ["FAIL: initialize 応答に Mcp-Session-Id がありません"]

    status, _, _ = _post(url, _rpc("notifications/initialized", None, None), sid, timeout=timeout)
    lines.append("initialized: HTTP %d（通知は 202）" % status)
    if status != 202:
        return 1, lines + ["FAIL: notifications/initialized に %d が返りました" % status]

    status, _, body = _post(url, _rpc("tools/list", {}, 2), sid, timeout=timeout)
    listing = json.loads(body.decode("utf-8")) if body else None
    tools = ((listing or {}).get("result") or {}).get("tools") or []
    lines.append("tools/list : %d 件" % len(tools))
    if status != 200 or not tools:
        return 1, lines + ["FAIL: tools/list が空か失敗しました (HTTP %d)" % status]

    for attempt in (1, 2):
        status, _, body = _post(url, _rpc("tools/call", {
            "name": tool, "arguments": dict(tool_args or {}),
        }, 3), sid, timeout=timeout)
        call = json.loads(body.decode("utf-8")) if body else None
        result = (call or {}).get("result") or {}
        if status == 200 and not result.get("isError"):
            lines.append("tools/call : %s 成功（%d 回目）" % (tool, attempt))
            return 0, lines
        lines.append("tools/call : %s 失敗（%d 回目・HTTP %d・isError=%s）"
                     % (tool, attempt, status, result.get("isError")))
    lines.append("FAIL: 再送でも tools/call が成功しませんでした（常駐プロセスのログを確認してください）")
    return 1, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="常駐 HTTP の実 MCP 疎通を確かめる（依存ゼロ）")
    parser.add_argument("--url", default=DEFAULT_URL, help="MCP エンドポイント（既定: %s）" % DEFAULT_URL)
    parser.add_argument("--tool", default=DEFAULT_TOOL, help="呼ぶツール（既定: %s）" % DEFAULT_TOOL)
    parser.add_argument("--args", default="", help="ツール引数の JSON（例: '{\"query\": \"x\"}'）")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        tool_args = json.loads(args.args) if args.args else {}
    except ValueError as exc:
        sys.stderr.write("--args は JSON で指定してください: %s\n" % exc)
        return 2
    code, lines = verify(url=args.url, tool=args.tool, tool_args=tool_args, timeout=args.timeout)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
