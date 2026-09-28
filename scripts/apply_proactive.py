#!/usr/bin/env python3
"""apply_proactive.py — **この MCP が率先して選ばれる状態**を、非対話で整える設定道具。

背景（旧実装の実測）
--------------------
モデルが見るのは `description` 文字列だけ（Hermes は MCP `instructions` を読まない）。
記述の工夫（条件・差分・競合の実名）だけでは自発率 **1/2** で頭打ちだった。効くのは 2 つの併用で **2/2**:

1. **毎ターン注入される場所に判断規則を置く**（memory か `$HERMES_HOME/SOUL.md`）。
   `AGENTS.md` は git root→cwd の連鎖でしか読まれず、ホームに置いても全 cwd には効かない。
2. **競合サーバーの汎用面を外す**（例 `tools.exclude = ["ask_*","panel","consensus*"]`）。
   用途の近いサーバーが併存すると「先に見つけた方」が選ばれ、名前を明記しても覆らない。

このスクリプトは 2 の設定コマンドを**検出したサーバー名に対してだけ**生成・実行し、
1 の文面をそのまま貼れる形で出す。既定は**表示のみ（dry-run）**で、書き換えるには `--apply` が要る。

使い方
------
    python scripts/apply_proactive.py                  # 何をするか見るだけ（安全）
    python scripts/apply_proactive.py --snippet        # 貼る文面だけ出す
    python scripts/apply_proactive.py --apply          # 設定を適用（Hermes の再起動が必要）
    python scripts/apply_proactive.py --apply --yes    # 確認なしで適用

終了コード: 0=正常 / 1=設定の読み取りに失敗。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

# 競合サーバーごとに「外す汎用面」。**サーバーごと無効化はしない**（使えるツールまで失う）。
COMPETING = {
    "deliberation": {
        "exclude": ["ask_*", "panel", "consensus*"],
        "why": "多モデルに「聞く」汎用面だけを外す。researcher / code_reviewer / debugger / "
               "architect / security_analyst / session_* は残す",
    },
}

SNIPPET = (
    "複数視点が要るときは freeagent-bind を優先（有効なら）: 設計判断・リスク抽出・意見が割れそうな問いは "
    "mcp__freeagent_bind__freeagent_panel（往復して前提を更新するなら freeagent_consult）、"
    "出典が要るなら freeagent_lookup / freeagent_grounded。delegate_task は同一モデルの分身で多様性ゼロ、"
    "deliberation の ask_*/panel は単発集約。"
    "**freeagent-bind が無効・不通のときは、存在しないツールを探さず delegate_task / web_search / "
    "web_extract で回答を完遂し、実際に応答した独立ソースの件数を明記する**"
    "（1 件で「複数視点で検討した」と書かない）。"
)

MARKER = "<!-- freeagent-bind: proactive-usage -->"


def hermes_home() -> str:
    home = os.environ.get("HERMES_HOME")
    if home:
        return home
    local = os.environ.get("LOCALAPPDATA")
    if local and os.path.isdir(os.path.join(local, "hermes")):
        return os.path.join(local, "hermes")
    return os.path.join(os.path.expanduser("~"), ".hermes")


def read_config() -> str:
    for name in ("config.yaml", "cli-config.yaml"):
        path = os.path.join(hermes_home(), name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
    return ""


def find_servers(cfg: str) -> list[str]:
    """`mcp_servers:` ブロック直下（インデント 2）のサーバー名を拾う（厳密な YAML 解析はしない）。"""
    out: list[str] = []
    inside = False
    for line in cfg.splitlines():
        if re.match(r"^mcp_servers:\s*$", line):
            inside = True
            continue
        if inside:
            if line.strip() and not line.startswith(" "):
                break                      # 次のトップレベルキーで終わり
            m = re.match(r"^  ([A-Za-z0-9_.\-]+):\s*$", line)
            if m:
                out.append(m.group(1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="freeagent-bind が率先して選ばれるように設定する")
    ap.add_argument("--apply", action="store_true", help="実際に hermes config set を実行する")
    ap.add_argument("--yes", action="store_true", help="確認プロンプトを出さない")
    ap.add_argument("--snippet", action="store_true", help="貼る文面だけを出す")
    ap.add_argument("--write-snippet", default=None,
                    help="文面を指定ファイルへ追記する（既に入っていれば何もしない）")
    args = ap.parse_args()

    if args.snippet:
        print(SNIPPET)
        return 0

    cfg = read_config()
    servers = find_servers(cfg)
    print(f"Hermes home: {hermes_home()}")
    print(f"登録済み MCP サーバー: {', '.join(servers) if servers else '（読めませんでした）'}")
    print()

    commands: list[list[str]] = []
    for name, spec in COMPETING.items():
        if name not in servers:
            print(f"· {name}: 未登録なので対象外（競合が無ければ記述だけで選ばれる）")
            continue
        value = json.dumps(spec["exclude"], ensure_ascii=False)
        commands.append(["hermes", "config", "set", f"mcp_servers.{name}.tools.exclude", value])
        print(f"· {name}: 汎用面を外す → {value}")
        print(f"    {spec['why']}")
    print()

    print("【1】毎ターン注入される場所に置く判断規則（memory に保存するか、下の文面を SOUL.md へ）")
    print("-" * 72)
    print(SNIPPET)
    print("-" * 72)
    print("  memory へ入れる場合: エージェントに「この文面を memory に保存して」と頼む、")
    print(f"  または `{os.path.join(hermes_home(), 'SOUL.md')}` に追記する"
          "（`--write-snippet <path>` でも書ける）。")
    print()

    if args.write_snippet:
        path = args.write_snippet
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                existing = fh.read()
        if MARKER in existing:
            print(f"· {path}: 既に入っています（何もしません）")
        else:
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(f"\n{MARKER}\n{SNIPPET}\n")
            print(f"✓ {path} に文面を追記しました")
        print()

    if not commands:
        print("【2】競合サーバーが無いので、適用する設定はありません。")
        print("     Hermes を再起動して終わりです（MCP はホットリロードしない）。")
        return 0

    print("【2】競合の汎用面を外す設定")
    for cmd in commands:
        print("  " + " ".join(f"'{c}'" if " " in c else c for c in cmd))

    if not args.apply:
        print()
        print("（表示のみ。適用するには --apply を付けて実行してください）")
        print("反映には **Hermes の再起動** が必要です（MCP はホットリロードしない）。")
        return 0

    if not args.yes:
        try:
            answer = input("適用しますか？ [y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("中止しました（何も変更していません）")
            return 0

    failed = 0
    for cmd in commands:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"  ✗ {' '.join(cmd)} → {type(exc).__name__}: {exc}")
            failed += 1
            continue
        ok = proc.returncode == 0
        print(f"  {'✓' if ok else '✗'} {' '.join(cmd)}"
              + ("" if ok else f" → {(proc.stderr or proc.stdout or '').strip()[:120]}"))
        failed += 0 if ok else 1
    print()
    print("**Hermes を再起動**して反映してください（MCP はホットリロードしない）。")
    print("確認: python scripts/measure_adoption.py --sessions 20   # 採用率を測る（最低 2 標本）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())