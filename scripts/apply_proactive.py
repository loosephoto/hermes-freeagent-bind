#!/usr/bin/env python3
"""apply_proactive.py — **この MCP が率先して選ばれる状態**を、非対話で整える設定道具。

背景（旧実装の実測）
--------------------
モデルが見るのは `description` 文字列だけ（Hermes は MCP `instructions` を読まない）。
記述の工夫（条件・差分・競合の実名）だけでは自発率 **1/2** で頭打ちだった。効くのは 2 つの併用で **2/2**:

1. **毎ターン注入される場所に判断規則を置く**（memory か `$HERMES_HOME/SOUL.md`）。
   `AGENTS.md` は git root→cwd の連鎖でしか読まれず、ホームに置いても全 cwd には効かない。
2. **競合サーバーの汎用面を外す**（`mcp_servers.<name>.tools.exclude`）。
   用途の近いサーバーが併存すると「先に見つけた方」が選ばれ、名前を明記しても覆らない。

ただし**除外パターンは必ず実ツール名に照合してから書く**。Hermes の照合は `fnmatchcase`（大小文字
区別）で、`*` / `?` / `[` を含まない項目は**完全一致**として扱われる。実測で 2 回踏んでいる:

1. `ask_*` は**どの実名にも一致しない**（実名は `ask-all` / `ask-one` / `consensus-step` … と
   **ハイフン**区切り）。流布していた例をそのまま書くと**空振り**する。
2. **`cache/mcp_schema_cache.json` は不完全**（実測: 18 件しか無く、実在する `panel` / `consensus` /
   `consensus-step` が載っていなかった ＝ キャッシュを信じると「存在しない」と誤判定する）。

そこで照合は **`hermes mcp test <server>` のライブ一覧を最優先**し、取れないときだけキャッシュへ
落ちる（その場合は「キャッシュに無い」＝実在の否定ではない、と表示する）。設定済みの除外が一致 0 件
なら**終了コード 1**（存在しない候補は情報表示のみで、設定には書かない）。

使い方
------
    python scripts/apply_proactive.py                  # 何をするか見るだけ（安全）
    python scripts/apply_proactive.py --check          # 照合結果だけ見る（空振りの検出）
    python scripts/apply_proactive.py --snippet        # 貼る文面だけ出す
    python scripts/apply_proactive.py --apply          # 設定を適用（Hermes の再起動が必要）
    python scripts/apply_proactive.py --apply --yes    # 確認なしで適用

終了コード: 0=正常 / 1=**設定済みの除外が空振り**（または適用対象が無い） / 2=設定が読めない。
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
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

# 競合サーバーごとに「外す汎用面」。**サーバーごと無効化はしない**（使えるツールまで失う）。
# `patterns` は**候補**。実ツール名に照合して一致したものだけを設定に書く（match_report）。
# `panel` / `consensus*` は現行サーバーには存在しないが、将来復活したときに拾えるよう候補として残す
# （候補に残すだけで、一致しなければ設定には書かれず、空振りとして警告される）。
COMPETING = {
    "deliberation": {
        "patterns": ["ask-*", "panel", "consensus*"],
        "why": "多モデルに「聞く」汎用面だけを外す。researcher / code-reviewer / debugger / "
               "architect / security-analyst / scope-analyst / plan-reviewer / analyze / session-* は残す",
    },
}

SNIPPET = (
    "複数視点が要るときは freeagent-bind を優先（有効なら）: 設計判断・リスク抽出・意見が割れそうな問いは "
    "mcp__freeagent_bind__freeagent_panel（往復して前提を更新するなら freeagent_consult）、"
    "出典が要るなら freeagent_lookup / freeagent_grounded。delegate_task は同一モデルの分身で多様性ゼロ、"
    "deliberation の ask-* は単発集約。"
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


def _parse_flow_list(value: str) -> list[str]:
    """`['a', 'b']` / `["a"]` のような 1 行リストを解析する（厳密な YAML 解析はしない）。"""
    body = value.strip()
    if body.startswith("["):
        body = body[1:]
    if body.endswith("]"):
        body = body[:-1]
    return [part.strip().strip("\"'") for part in body.split(",") if part.strip()]


def configured_exclude(server: str) -> list[str]:
    """設定済みの `tools.exclude` を取り出す（その節だけを見る。YAML 全体は解析しない）。

    実測（踏んだ不具合）: `hermes config set` が書くリストは**項目が 8 スペース**で、
    6 スペースを期待した実装は**空リストを返していた**（＝壊れた除外を見逃し、空振りを検出できない）。
    ブロック形式（`- item` 行）と 1 行形式（`['a', 'b']`）の両方を受ける。
    """
    out: list[str] = []
    inside_server = inside_exclude = False
    exclude_indent = 0
    for line in read_config().splitlines():
        if re.match(r"^  " + re.escape(server) + r":\s*$", line):
            inside_server = True
            continue
        if not inside_server:
            continue
        if line.strip() and not line.startswith(" "):
            break                                   # 次のトップレベルキー＝節の終わり
        if not inside_exclude:
            m = re.match(r"^(\s+)exclude:\s*(.*)$", line)
            if m:
                inside_exclude = True
                exclude_indent = len(m.group(1))
                if m.group(2).strip():              # 1 行形式
                    out += _parse_flow_list(m.group(2))
                    inside_exclude = False
            continue
        m = re.match(r"^(\s*)-\s+(.+?)\s*$", line)
        if m and len(m.group(1)) > exclude_indent:
            out.append(m.group(2).strip().strip("\"'"))
            continue
        if line.strip():                            # 列挙の終わり（別のキー）
            inside_exclude = False
    return out


def _tool_names_from_test(server: str, timeout: float = 90.0) -> list[str] | None:
    """`hermes mcp test <server>` の出力から**ライブの**ツール名を取る（最優先の情報源）。

    実測: この経路は実在するツールをすべて返す（deliberation で 21 件）。キャッシュ（18 件）は
    取りこぼすので、**存在の判定はライブでしか行わない**。
    """
    try:
        proc = subprocess.run(["hermes", "mcp", "test", server], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    names: list[str] = []
    started = False
    for line in (proc.stdout or "").splitlines():
        if "Tools discovered" in line:
            started = True
            continue
        if not started:
            continue
        m = re.match(r"^\s{2,}([A-Za-z0-9_.\-]+)\s{2,}\S", line)
        if m:
            names.append(m.group(1))
        elif names and not line.strip():
            break
    return names or None


def _tool_names_from_cache(server: str) -> list[str] | None:
    """schema キャッシュから取る（**不完全なことがある**。ライブが取れないときの代替）。"""
    path = os.path.join(hermes_home(), "cache", "mcp_schema_cache.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = data.get(server) if isinstance(data, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("tools"), list):
        return None
    return [str(t.get("name")) for t in entry["tools"]
            if isinstance(t, dict) and t.get("name")]


def live_tool_names(server: str) -> tuple[list[str] | None, str]:
    """実ツール名を返す。優先順: ライブ（`hermes mcp test`）→ schema キャッシュ。"""
    names = _tool_names_from_test(server)
    if names:
        return names, "live"
    names = _tool_names_from_cache(server)
    if names:
        return names, "cache"
    return None, "none"


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def pattern_hits(pattern: str, names: list[str]) -> list[str]:
    """Hermes と同じ照合（fnmatchcase。glob でなければ完全一致）で一致したツール名を返す。"""
    if _is_glob(pattern):
        return sorted(n for n in names if fnmatch.fnmatchcase(n, pattern))
    return sorted(n for n in names if n == pattern)


def match_report(patterns: list[str], names: list[str]) -> tuple[list[str], list[str]]:
    """(一致したパターン, 一致 0 件のパターン) を返す。"""
    matched, empty = [], []
    for pat in patterns:
        (matched if pattern_hits(pat, names) else empty).append(pat)
    return matched, empty


def main() -> int:
    ap = argparse.ArgumentParser(description="freeagent-bind が率先して選ばれるように設定する")
    ap.add_argument("--apply", action="store_true", help="実際に hermes config set を実行する")
    ap.add_argument("--yes", action="store_true", help="確認プロンプトを出さない")
    ap.add_argument("--snippet", action="store_true", help="貼る文面だけを出す")
    ap.add_argument("--check", action="store_true", help="照合結果だけを出す（設定は書かない）")
    ap.add_argument("--write-snippet", default=None,
                    help="文面を指定ファイルへ追記する（既に入っていれば何もしない）")
    args = ap.parse_args()

    if args.snippet:
        print(SNIPPET)
        return 0

    cfg = read_config()
    servers = find_servers(cfg)
    if not args.check:
        print(f"Hermes home: {hermes_home()}")
        print(f"登録済み MCP サーバー: {', '.join(servers) if servers else '（読めませんでした）'}")
        print()

    commands: list[list[str]] = []
    broken = 0
    for name, spec in COMPETING.items():
        if name not in servers:
            if not args.check:
                print(f"· {name}: 未登録なので対象外（競合が無ければ記述だけで選ばれる）")
            continue
        names, source = live_tool_names(name)
        if not names:
            broken += 1
            print(f"· {name}: ⚠ 実ツール名を照合できません（ライブもキャッシュも取れない）。"
                  f"設定済み: {configured_exclude(name) or '（なし）'}")
            continue
        matched, empty = match_report(spec["patterns"], names)
        already = configured_exclude(name)
        how = "ライブ（hermes mcp test）" if source == "live" else "schema キャッシュ（**不完全なことがある**）"
        print(f"· {name}: 実ツール {len(names)} 件 / 情報源: {how}")
        print(f"    {', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
        for pat in matched:
            print(f"    ✓ {pat!r} → {len(pattern_hits(pat, names))} 件 "
                  f"({', '.join(pattern_hits(pat, names)[:6])})")
        for pat in empty:
            if source == "live":
                print(f"    ℹ {pat!r} → 現行には存在しない（候補としてのみ保持。設定には書きません）")
            else:
                print(f"    ? {pat!r} → キャッシュに無い（**実在の否定ではない**。"
                      f"ライブで確かめること）")
        if already:
            stale = [pat for pat in already if not pattern_hits(pat, names)]
            print(f"    現在の設定: {already}")
            for pat in stale:
                # **これが本当の空振り**: 設定済みなのに何にも一致していない。
                if source == "live":
                    broken += 1
                    print(f"    ✗ 設定済みの {pat!r} は一致 0 件（**空振り。設定しても何も変わらない**）")
                else:
                    print(f"    ? 設定済みの {pat!r} はキャッシュに一致無し"
                          f"（ライブで確認するまで判断しない）")
        if matched:
            commands.append(["hermes", "config", "set", f"mcp_servers.{name}.tools.exclude",
                             json.dumps(matched, ensure_ascii=False)])
            if not args.check:
                print(f"    {spec['why']}")
    print()

    if args.check:
        print(f"照合できた競合: {len(commands)} 件 / 空振り・未確認: {broken} 件")
        if broken:
            print("  設定済みの除外が空振りしています（実ツール名を確認して書き直すこと）")
        return 1 if broken else 0

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
        print("【2】適用できる設定はありません（競合が未登録か、パターンが実名に一致しません）。")
        if broken:
            print("     ⚠ 空振りのパターンがあります（実ツール名を確認してください）:")
            print("       python scripts/apply_proactive.py --check")
        return 1 if broken else 0

    print("【2】競合の汎用面を外す設定（実ツール名に一致したものだけ）")
    for cmd in commands:
        print("  " + " ".join(f"'{c}'" if " " in c else c for c in cmd))

    if not args.apply:
        print()
        print("（表示のみ。適用するには --apply を付けて実行してください）")
        print("反映には **Hermes の再起動** が必要です（MCP はホットリロードしない）。")
        return 1 if broken else 0

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
    print("確認: python scripts/apply_proactive.py --check           # 照合（空振りが無いこと）")
    print("      python scripts/measure_adoption.py --sessions 20    # 採用率（最低 2 標本）")
    return 1 if (failed or broken) else 0


if __name__ == "__main__":
    sys.exit(main())