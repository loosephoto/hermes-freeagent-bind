#!/usr/bin/env python3
"""check_offline.py — **バックエンド全滅（＝MCP が実質使えない状態）でも副作用を残さない**ことを検証する。

検証する契約（`AGENTS.md` 規約 21〜23 / SPEC §10）:

1. **例外を漏らさない**: どのツールも JSON-RPC エラーや無応答にならず、`isError` か正常応答で返る。
2. **ハングしない**: 各呼び出しが既定 20 秒以内に返る（TCP が blackhole したホストへ素の呼び出しを
   投げると分単位で固まり、並列で走っている他の呼び出しまで待たされる）。
3. **次の一手を返す**: `isError` の応答には `structuredContent.next_action` があり、
   `kind` と `advice` を持つ（メイン LLM が再試行でターンを捨てないため）。
4. **状態を汚さない（副作用ゼロ）**: 呼び出しの前後で状態ディレクトリに**新しいファイルが増えない**。
   環境障害（プロキシ停止・DNS 不達）は品質統計にもトレースにも書かない設計なので、
   クールダウン・統計・トレースのどれも生成されないこと。

使い方（リポジトリ直下で）:

    env -u PYTHONPATH PYTHONPATH=src python scripts/check_offline.py

終了コード: 0=正常 / 1=契約違反（詳細を stdout に出す）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。CI の windows-latest は
# cp1252 で、print() が UnicodeEncodeError になり **ゲートが落ちる**（実測）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALL_TIMEOUT_S = float(os.environ.get("FREEAGENT_OFFLINE_CALL_TIMEOUT", "20"))

# 全ツールの最小呼び出し（引数はスキーマ上有効な最小値）。
CASES: list[tuple[str, dict]] = [
    ("freeagent_models", {}),
    ("freeagent_ask", {"prompt": "1+1 は？"}),
    ("freeagent_fanout", {"prompts": ["1+1 は？"]}),
    ("freeagent_panel", {"question": "1+1 は？", "size": 2}),
    ("freeagent_lookup", {"query": "test"}),
    ("freeagent_grounded", {"question": "1+1 は？", "size": 2}),
    ("freeagent_map", {"items": ["a", "b"], "instruction": "1 行に要約"}),
    ("freeagent_consult", {"question": "1+1 は？", "size": 2}),
    # 思考台帳は verify を付けて呼ぶ（環境障害では**書かない**契約を検証する。台帳のみなら
    # ネットワークに触れないので、この検査は「検証に到達できないとき」の経路を突く）。
    ("freeagent_think", {"thought": "前提を分解する", "verify": True}),
    # 代替案（propose_alternatives）も同じ契約: 提案者に到達できなければ台帳に書かない。
    ("freeagent_think", {"thought": "仮説を立てる", "kind": "hypothesis", "propose_alternatives": True}),
    ("freeagent_agent", {"task": "調べる", "size": 1, "max_steps": 1}),
    ("freeagent_delegate", {"task": "調べる"}),
    ("freeagent_unknown_tool_xyz", {}),   # 未知ツール＝無効化されている場合の経路
]


def state_snapshot(path: str) -> set[str]:
    out: set[str] = set()
    for base, _dirs, files in os.walk(path):
        for name in files:
            out.add(os.path.relpath(os.path.join(base, name), path))
    return out


def main() -> int:
    state = tempfile.mkdtemp(prefix="fa-offline-")
    # バックエンドを意図的に全滅させる: 死んだポート（接続拒否）＋キー無し＋nous のみ。
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "FREEAGENT_STATE_DIR": state,
        "FREEAGENT_PROVIDER_ORDER": "nous",
        "FREEAGENT_BASE_URL": "http://127.0.0.1:9/v1",     # 接続拒否（discard ポート）
        "FREEAGENT_CONNECT_TIMEOUT": "3",
        "FREEAGENT_READ_TIMEOUT": "3",
        "FREEAGENT_KB_TIMEOUT": "0.001",                    # 知識側も即失敗させる（決定性のため）
        "FREEAGENT_PROBE_TIMEOUT": "3",
    })
    for name in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY", "HF_TOKEN",
                 "HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN", "GROQ_API_KEY",
                 "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "GEMINI_API_KEY",
                 "GOOGLE_API_KEY", "FREEAGENT_GROQ_FREE_TIER",
                 "FREEAGENT_CLOUDFLARE_FREE_PLAN", "FREEAGENT_GEMINI_FREE_TIER",
                 "FREEAGENT_GEMINI_UNPAID_DATA_ACK"):
        env.pop(name, None)

    cmd = [sys.executable, os.path.join(ROOT, "src", "freeagent_bind", "server.py")]
    proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            bufsize=1)

    def send(obj: dict) -> None:
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_reply(msg_id: int, timeout: float) -> dict | None:
        """id が一致する応答が来るまで読む（timeout で None）。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                return msg
        return None

    failures: list[str] = []
    checks = 0
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                         "clientInfo": {"name": "check_offline", "version": "1"}}})
        init = read_reply(1, 15)
        checks += 1
        if not init:
            failures.append("initialize に応答がない")
        else:
            if not (init.get("result") or {}).get("instructions"):
                failures.append("initialize に instructions が無い（他クライアント向けの記述）")

        before = state_snapshot(state)
        for idx, (name, args) in enumerate(CASES, start=10):
            t0 = time.monotonic()
            send({"jsonrpc": "2.0", "id": idx, "method": "tools/call",
                  "params": {"name": name, "arguments": args}})
            reply = read_reply(idx, CALL_TIMEOUT_S)
            dt = time.monotonic() - t0
            checks += 1
            if reply is None:
                failures.append(f"{name}: {CALL_TIMEOUT_S:.0f} 秒以内に応答が無い（ハング）")
                continue
            if reply.get("error"):
                failures.append(f"{name}: JSON-RPC エラー（例外が漏れている）: {reply['error']}")
                continue
            result = reply.get("result") or {}
            sc = result.get("structuredContent") or {}
            if result.get("isError"):
                advice = sc.get("next_action") or {}
                if not advice.get("kind") or not advice.get("advice"):
                    failures.append(f"{name}: isError だが next_action（kind/advice）が無い")
                else:
                    print(f"  {name:24} error={str(sc.get('error'))[:48]!r:52} "
                          f"advice={advice['kind']} ({dt:.1f}s)")
            else:
                print(f"  {name:24} ok（エラーではない） ({dt:.1f}s)")

        after = state_snapshot(state)
        added = sorted(after - before)
        checks += 1
        if added:
            failures.append(f"状態ディレクトリに新しいファイルが出来ている（副作用）: {added}")
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    if failures:
        print(f"✗ check_offline: {len(failures)} 件の契約違反（検査 {checks} 件）")
        for f in failures:
            print("   -", f)
        shutil.rmtree(state, ignore_errors=True)
        return 1
    print(f"✓ check_offline: 全契約を満たしました（検査 {checks} 件）")
    print("  バックエンド全滅でも: 例外漏れ 0 / ハング 0 / next_action あり / 状態ディレクトリは空のまま")
    shutil.rmtree(state, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())