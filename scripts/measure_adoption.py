#!/usr/bin/env python3
"""measure_adoption.py — **Hermes の記録から「実際に使われたか」を数える**測定器。

なぜ必要か（旧実装の実測）
--------------------------
`description` を直しても自発率は **1/2 で頭打ち**だった。効くのは「毎ターン注入される場所
（memory / `SOUL.md`）に判断規則を置く」＋「競合サーバーの汎用面を外す」の併用で **2/2**。
つまり**記述やスキルを変えるたびに測り直さないと、効果があったか分からない**。
旧実装は手順書と手打ちの python ワンライナーで測っていたので、ここをスクリプトにする。

判定は回答本文や CLI の表示ではなく **`state.db` の `messages.tool_calls` の記録**で行う。
遅延カタログ経由の呼び出しは `tool_call` という名前で記録され、実際のツール名は
`arguments.calls[].name` に入る（素朴に名前を数えると **0 件に見えてしまう**）。

使い方
------
    python scripts/measure_adoption.py                     # 直近 20 セッション
    python scripts/measure_adoption.py --sessions 40 --json
    python scripts/measure_adoption.py --since-hours 24
    python scripts/measure_adoption.py --min-rate 0.5      # ゲートとして使う（下回れば exit 1）

終了コード: 0=正常（`--min-rate` 未指定なら常に 0） / 1=採用率が下限未満 / 2=state.db が読めない。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

SERVER_PREFIX = "mcp__freeagent_bind__"
FREAGENT_PREFIX = "freeagent_"

# 競合（このサーバーが「代わりに使われる」相手）。旧実装の実測で選ばれていたもの。
COMPETING = {
    "delegate_task": "Hermes 内蔵（同一モデルの分身＝多様性ゼロ）",
    "deliberation.ask_all": "deliberation の一括問い合わせ",
    "deliberation.panel": "deliberation のパネル",
    "web_search": "単一視点の検索",
    "web_extract": "単一視点の抽出",
    "openrouter.send_message": "openrouter の単発推論",
}
LAZY_WRAPPERS = {"tool_call", "tool_describe"}


def find_db(explicit: str | None) -> str | None:
    if explicit:
        return explicit if os.path.exists(explicit) else None
    home = os.environ.get("HERMES_HOME")
    cands = []
    if home:
        cands.append(os.path.join(home, "state.db"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(os.path.join(local, "hermes", "state.db"))
    cands.append(os.path.join(os.path.expanduser("~"), ".hermes", "state.db"))
    return next((c for c in cands if os.path.exists(c)), None)


def tool_names_of(raw: str) -> list[str]:
    """`messages.tool_calls` の JSON から実際に呼ばれたツール名を取り出す。

    - OpenAI 形式: `[{"function": {"name": ..., "arguments": "{...}"}}]`
    - 遅延カタログ経由: `name == "tool_call"` で、実際の名前は `arguments.calls[].name`
    """
    try:
        arr = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[str] = []
    for call in (arr if isinstance(arr, list) else []):
        if not isinstance(call, dict):
            continue
        fn = (call.get("function") or {}).get("name") or call.get("name")
        args = (call.get("function") or {}).get("arguments") or call.get("arguments")
        if fn in LAZY_WRAPPERS:
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            for inner in ((args or {}).get("calls") or []):
                if isinstance(inner, dict) and inner.get("name"):
                    out.append(str(inner["name"]))
            if fn == "tool_call":
                continue
        if fn:
            out.append(str(fn))
    return out


def normalize(name: str) -> str:
    return name[len(SERVER_PREFIX):] if name.startswith(SERVER_PREFIX) else name


def main() -> int:
    ap = argparse.ArgumentParser(description="freeagent-bind の自発利用率を state.db から測る")
    ap.add_argument("--db", default=None, help="state.db のパス（既定は自動検出）")
    ap.add_argument("--sessions", type=int, default=20, help="対象にする直近セッション数（既定 20）")
    ap.add_argument("--since-hours", type=float, default=0.0, help="直近 N 時間だけを対象にする")
    ap.add_argument("--json", action="store_true", help="機械可読な JSON で出す")
    ap.add_argument("--min-rate", type=float, default=None,
                    help="採用率がこの値未満なら exit 1（ゲートとして使う）")
    args = ap.parse_args()

    db = find_db(args.db)
    if not db:
        print("✗ state.db が見つかりません（--db で指定するか HERMES_HOME を設定してください）",
              file=sys.stderr)
        return 2

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    where = "tool_calls is not null and tool_calls != ''"
    params: list = []
    if args.since_hours:
        where += " and timestamp >= datetime('now', ?)"
        params.append(f"-{args.since_hours} hours")
    rows = con.execute(
        f"select session_id, tool_calls, timestamp from messages where {where} order by id desc",
        params).fetchall()

    per_session: dict[str, collections.Counter] = {}
    order: list[str] = []
    for sid, raw, _ts in rows:
        if sid not in per_session:
            if len(order) >= args.sessions:
                continue
            order.append(sid)
            per_session[sid] = collections.Counter()
        for name in tool_names_of(raw):
            per_session[sid][normalize(name)] += 1

    total_sessions = len(order)
    using = [s for s in order if any(n.startswith(FREAGENT_PREFIX) for n in per_session[s])]
    rate = (len(using) / total_sessions) if total_sessions else 0.0
    tool_counts: collections.Counter = collections.Counter()
    for s in order:
        for name, count in per_session[s].items():
            if name.startswith(FREAGENT_PREFIX):
                tool_counts[name] += count

    competing: collections.Counter = collections.Counter()
    for s in order:
        for name in per_session[s]:
            if name in COMPETING or name.startswith("deliberation."):
                competing[name] += per_session[s][name]

    if args.json:
        print(json.dumps({
            "db": db, "sessions": total_sessions, "sessions_using_freeagent": len(using),
            "adoption_rate": round(rate, 4),
            "freeagent_calls": dict(tool_counts.most_common()),
            "competing_calls": dict(competing.most_common()),
        }, ensure_ascii=False, indent=2))
    else:
        print(f"state.db: {db}")
        print(f"対象セッション: {total_sessions} 件"
              + (f"（直近 {args.since_hours} 時間）" if args.since_hours else "（直近の記録から）"))
        print(f"freeagent を使ったセッション: {len(using)} 件 → **採用率 {rate:.0%}**")
        print()
        if tool_counts:
            print("  呼ばれた freeagent ツール:")
            for name, count in tool_counts.most_common():
                print(f"    {count:4}  {name}")
        else:
            print("  呼ばれた freeagent ツール: なし")
        if competing:
            print()
            print("  競合（代わりに使われたもの）:")
            for name, count in competing.most_common(8):
                note = COMPETING.get(name, "")
                print(f"    {count:4}  {name}" + (f"   — {note}" if note else ""))
        print()
        print("  測定は「新プロセスの `hermes chat -q \"<ツール名を含まない依頼>\"`」で行う")
        print("  （実行中セッションは起動時のツール一覧を保持するので変更が反映されない）。")
        print("  最低 2 標本。1/2 と 2/2 の差は標本 1 つでは判定できない。")

    if args.min_rate is not None and rate < args.min_rate:
        print(f"\n✗ 採用率 {rate:.0%} が下限 {args.min_rate:.0%} を下回りました", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())