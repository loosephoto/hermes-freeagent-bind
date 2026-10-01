"""実クライアント経路（stdio）の疎通検査。

サーバーを**別プロセスで起動**し、改行区切り JSON-RPC で initialize → tools/list → tools/call を
実際に往復させる。ネットワークとモデルは使わない（必須引数を欠いた呼び出し＝エラー経路で確認する）。
`FREEAGENT_PROBE_NET=1` を付けると `freeagent_models` も叩いてバックエンドの生存を確認する。

検査する破綻:
  * 起動直後に応答が返らない（stdio でネイティブ拡張を後から import すると無応答になる環境がある）
  * protocolVersion を固定して返す（クライアントが tools/list を取り消し、60 秒待ちに見える）
  * 日本語が cp932 に落ちて応答が黙って捨てられる
  * ツールが例外を漏らして JSON-RPC が壊れる
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。CI の windows-latest は
# cp1252 で、print() が UnicodeEncodeError になり **ゲートが落ちる**（実測）。
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


def main() -> int:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = os.path.join(ROOT, "src")
    env["FREEAGENT_STATE_DIR"] = env.get("FREEAGENT_STATE_DIR") or os.path.join(
        os.environ.get("TEMP", "/tmp"), "freeagent-smoke")
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("FREEAGENT_HARNESS", None)        # §8.6 を「Hermes 以外（clientInfo=smoke）」で決定的に試す
    env.pop("FREEAGENT_HARNESS_WARN", None)

    proc = subprocess.Popen([sys.executable, "-m", "freeagent_bind"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
    notifications: list[dict] = []
    try:
        def send(msg: dict) -> dict | None:
            """id の一致する応答まで読む。途中の通知（id なし。§8.6 のログ通知など）は取っておく。"""
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            if "id" not in msg:
                return None
            while True:
                line = proc.stdout.readline()
                if not line.strip():
                    check(False, f"応答がありません（{msg.get('method')}）")
                    return None
                reply = json.loads(line)
                if "id" not in reply:
                    notifications.append(reply)
                    continue
                check(reply.get("id") == msg["id"], f"応答の id がずれています: {reply.get('id')} != {msg['id']}")
                return reply

        init = send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "smoke", "version": "0"}}})
        server_info = ((init or {}).get("result") or {}).get("serverInfo") or {}
        check(server_info.get("name") == "hermes-freeagent-bind",
              f"serverInfo.name が想定外: {server_info}")
        check((init or {}).get("result", {}).get("protocolVersion") == "2025-06-18",
              "protocolVersion が提示された版で交渉されていない")

        # §8.6 ハーネス判別: "smoke" は Hermes 以外 → logging 宣言・ログ通知・最初の結果の ⚠
        caps = ((init or {}).get("result") or {}).get("capabilities") or {}
        check("logging" in caps, "notifications/message を送るのに capabilities.logging を宣言していません")
        check(str(((init or {}).get("result") or {}).get("instructions", "")).startswith("【注意】"),
              "Hermes 以外のクライアントに instructions の注記がありません")
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        listing = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = ((listing or {}).get("result") or {}).get("tools") or []
        check(len(tools) == 11, f"tools/list が 11 件ではありません（{len(tools)} 件）")
        lookup = next((t for t in tools if t.get("name") == "freeagent_lookup"), {})
        props = (lookup.get("inputSchema") or {}).get("properties") or {}
        check({"datacite_kind", "fallback"}.issubset(props), "追加検索のスキーマがありません")
        for source in ("datacite", "openaire", "europepmc"):
            check(source in str((props.get("sources") or {}).get("description")),
                  f"sources の説明に {source} がありません")
        check(all(t.get("name", "").startswith("freeagent_") for t in tools),
              "tools/list に名前空間外のツールが混ざっています")

        # エラー経路（必須引数なし＝ネットワークに行かない）
        call = send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "freeagent_ask", "arguments": {}}})
        result = (call or {}).get("result") or {}
        check(result.get("isError") is True, "必須引数欠落がエラーになっていません")
        text = ((result.get("content") or [{}])[0]).get("text", "")
        check("prompt" in text, f"エラー本文が返っていません: {text[:80]!r}")
        check(isinstance(result.get("structuredContent"), dict),
              "structuredContent が返っていません")
        check("⚠️" in text, "日本語（絵文字含む）が往復していません（エンコーディング破綻）")
        check(text.startswith("⚠️ Hermes Agent 以外"), "最初のツール結果にハーネスの警告がありません")
        check((result.get("structuredContent") or {}).get("harness", {}).get("kind") == "other",
              "structuredContent.harness が返っていません")
        logs = [n for n in notifications if n.get("method") == "notifications/message"]
        check(len(logs) == 1 and (logs[0].get("params") or {}).get("level") == "warning",
              f"ハーネスのログ通知が 1 件ではありません（{len(logs)} 件）")

        # 未知名でも JSON-RPC を壊さない
        unknown = send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "does_not_exist", "arguments": {}}})
        check(((unknown or {}).get("result") or {}).get("isError") is True,
              "未知のツール名でエラーが返りません")

        # 未知メソッドでも応答する（-32601）
        bad = send({"jsonrpc": "2.0", "id": 5, "method": "no/such/method", "params": {}})
        check("error" in (bad or {}), "未知メソッドに JSON-RPC エラーが返りません")

        if os.environ.get("FREEAGENT_PROBE_NET") == "1":
            call = send({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                         "params": {"name": "freeagent_models", "arguments": {}}})
            data = ((call or {}).get("result") or {}).get("structuredContent") or {}
            check(not data.get("error"), f"freeagent_models が失敗: {data.get('error')}")
            print(f"  バックエンド: free {data.get('free_candidates')} / "
                  f"usable {data.get('usable_now')} / default {data.get('default_model')}")
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    if FAILURES:
        print("stdio 疎通チェック失敗:")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("stdio 疎通チェック OK（initialize / tools/list / tools/call / エラー経路）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())