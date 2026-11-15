"""常駐 HTTP の実 MCP 疎通を素の urllib で確かめる（issue #13 の検証スクリプトを同梱化）。

常駐配線の再生成・再起動のあとに、**実経路**（initialize → notifications/initialized →
tools/list → tools/call）を 1 往復ずつ通して「登録は成功したが応答しない」無言状態を弾く。
依存ゼロ（標準ライブラリのみ・ネットワークはループバックへの HTTP だけ）。

```bash
python scripts/verify_resident.py                       # http://127.0.0.1:8790/mcp を既定で検査
python scripts/verify_resident.py --url http://127.0.0.1:8790/mcp --tool freeagent_models
python scripts/verify_resident.py --tool freeagent_models --args '{"query": "qwen", "limit": 5}'
```

判定（issue #13「正常とみなす基準」）:
  * initialize に `serverInfo.version` と `mcp-session-id` が付く
  * 通知（notifications/initialized）に 202 が返る
  * `tools/list` が 1 件以上のツールを返す
  * `tools/call` が JSON-RPC `result` を持ち（`error` 無し）、`isError` でない結果を返す
    （session 失効の outcome UNKNOWN / HTTP 404 は**再送 1 回**で回復する・再起動直後の既知の挙動。
    再送でも失敗したら exit 1）

注意: 既定で `x-freeagent-harness: hermes` ヘッダを添えるため、稼働中の Hermes 常駐サーバーへ
実行してもハーネス判定を `other`（警告状態）へ上書きしません（`--harness ""` で未指定の挙動も確認可）。
`FREEAGENT_HTTP_TOKEN`（または `--token`）が設定されていれば `Authorization: Bearer` を付与します。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8790/mcp"
DEFAULT_TOOL = "freeagent_models"
DEFAULT_HARNESS = "hermes"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "resident-verify"


def _json_dict(raw: bytes) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _auth_headers(token: str = "", harness: str = DEFAULT_HARNESS) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if harness:
        headers["x-freeagent-harness"] = harness
    return headers


def _post(url: str, payload: dict, session_id: str = "", timeout: float = 30.0,
          token: str = "", harness: str = DEFAULT_HARNESS):
    """JSON-RPC 1 件を POST する。`(status, headers, 応答 bytes)` を返す。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **_auth_headers(token=token, harness=harness),
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
    except OSError as exc:
        return 0, {}, str(exc).encode("utf-8", "replace")


def _delete_session(url: str, session_id: str, timeout: float = 5.0,
                    token: str = "", harness: str = DEFAULT_HARNESS) -> None:
    if not session_id:
        return
    headers = {"Mcp-Session-Id": session_id, **_auth_headers(token=token, harness=harness)}
    req = urllib.request.Request(url, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except Exception:
        pass


def _rpc(method: str, params: dict | None, _id: int | None) -> dict:
    payload: dict = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        payload["params"] = params
    if _id is not None:
        payload["id"] = _id
    return payload


def _initialize_once(url: str, timeout: float, token: str, harness: str) -> tuple[int, str, dict | None, bytes]:
    status, headers, body = _post(
        url,
        _rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": CLIENT_NAME, "version": "1.0"},
        }, 1),
        timeout=timeout, token=token, harness=harness,
    )
    init = _json_dict(body)
    sid = (headers.get("Mcp-Session-Id") or headers.get("mcp-session-id") or "").strip()
    return status, sid, init, body


def verify(url: str = DEFAULT_URL, tool: str = DEFAULT_TOOL, tool_args: dict | None = None,
           timeout: float = 30.0, token: str | None = None,
           harness: str = DEFAULT_HARNESS) -> tuple[int, list[str]]:
    """実 MCP 疎通を通す。`(exit_code, 報告行のリスト)` を返す（exit 0 = 正常）。"""
    lines: list[str] = []
    effective_token = (os.environ.get("FREEAGENT_HTTP_TOKEN", "") if token is None else token).strip()
    sid = ""
    try:
        status, sid, init, body = _initialize_once(url, timeout, effective_token, harness)
        if status != 200 or init is None or "error" in init or not isinstance(init.get("result"), dict):
            return 1, [f"FAIL: initialize (HTTP {status}): {(body[:200].decode('utf-8', 'replace') if body else '本文なし')}"]
        info = (init.get("result") or {}).get("serverInfo") or {}
        lines.append("serverInfo : %s %s" % (info.get("name"), info.get("version")))
        if not sid:
            return 1, lines + ["FAIL: initialize 応答に Mcp-Session-Id がありません"]

        status, _, _ = _post(url, _rpc("notifications/initialized", None, None), sid,
                             timeout=timeout, token=effective_token, harness=harness)
        lines.append("initialized: HTTP %d（通知は 202）" % status)
        if status != 202:
            return 1, lines + ["FAIL: notifications/initialized に %d が返りました" % status]

        status, _, body = _post(url, _rpc("tools/list", {}, 2), sid,
                                timeout=timeout, token=effective_token, harness=harness)
        listing = _json_dict(body)
        tools = ((listing or {}).get("result") or {}).get("tools") if listing and "error" not in listing else None
        tools = tools if isinstance(tools, list) else []
        lines.append("tools/list : %d 件" % len(tools))
        if status != 200 or not tools:
            return 1, lines + ["FAIL: tools/list が空か失敗しました (HTTP %d)" % status]

        for attempt in (1, 2):
            status, _, body = _post(url, _rpc("tools/call", {
                "name": tool, "arguments": dict(tool_args or {}),
            }, 3), sid, timeout=timeout, token=effective_token, harness=harness)
            call = _json_dict(body)
            has_result = call is not None and "error" not in call and isinstance(call.get("result"), dict)
            result = call["result"] if has_result else {}
            if status == 200 and has_result and not result.get("isError"):
                lines.append("tools/call : %s 成功（%d 回目）" % (tool, attempt))
                return 0, lines
            rpc_err = (call or {}).get("error") if isinstance(call, dict) else None
            detail = (f"rpc_error={rpc_err.get('code')}" if isinstance(rpc_err, dict)
                      else f"isError={result.get('isError') if has_result else 'no-result'}")
            lines.append("tools/call : %s 失敗（%d 回目・HTTP %d・%s）"
                         % (tool, attempt, status, detail))
            if attempt == 1 and status == 404:
                # セッション失効（HTTP 404）の場合は initialize から張り直して再送する。
                re_status, re_sid, re_init, _ = _initialize_once(url, timeout, effective_token, harness)
                if re_status == 200 and re_sid and re_init and "result" in re_init:
                    sid = re_sid
                    _post(url, _rpc("notifications/initialized", None, None), sid,
                          timeout=timeout, token=effective_token, harness=harness)
        lines.append("FAIL: 再送でも tools/call が成功しませんでした（常駐プロセスのログを確認してください）")
        return 1, lines
    finally:
        _delete_session(url, sid, token=effective_token, harness=harness)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="常駐 HTTP の実 MCP 疎通を確かめる（依存ゼロ）")
    parser.add_argument("--url", default=DEFAULT_URL, help="MCP エンドポイント（既定: %s）" % DEFAULT_URL)
    parser.add_argument("--tool", default=DEFAULT_TOOL, help="呼ぶツール（既定: %s）" % DEFAULT_TOOL)
    parser.add_argument("--args", default="", help="ツール引数の JSON（例: '{\"query\": \"x\"}'）")
    parser.add_argument("--token", default=None, help="HTTP 認証トークン（省略時は FREEAGENT_HTTP_TOKEN）")
    parser.add_argument("--harness", default=DEFAULT_HARNESS,
                        help="x-freeagent-harness ヘッダ値（既定: %s、空文字で送信しない）" % DEFAULT_HARNESS)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)
    try:
        tool_args = json.loads(args.args) if args.args else {}
    except ValueError as exc:
        sys.stderr.write("--args は JSON で指定してください: %s\n" % exc)
        return 2
    code, lines = verify(url=args.url, tool=args.tool, tool_args=tool_args,
                         timeout=args.timeout, token=args.token, harness=args.harness)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
