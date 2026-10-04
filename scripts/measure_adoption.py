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

**全セッション比の採用率は「必要場面での自発率」ではない。** 雑談や単純な質問も分母に入るため、
記述を良くしても数値は動きにくい。そこで依頼文から「この能力が要るはず」と言える場面
（`NEED_CUES`）を近似判定し、**必要場面だけの採用率**と**見逃し（必要なのに使わなかった）**を出す。
判定は言い回しのヒューリスティックであり、必要場面の完全な判定ではない。**依頼文とみなすのは人の依頼だけ**
で、`@url` で貼られた添付ページ本文・機械が差し込む通知（`[ASYNC DELEGATION …]`）・「読み取り専用で読む」
型の自分で読む依頼は除く（実測でこれらが誤検出を水増ししていた。詳細は SPEC §7.6）。

使い方
------
    python scripts/measure_adoption.py                     # 直近 20 セッション
    python scripts/measure_adoption.py --sessions 40 --json
    python scripts/measure_adoption.py --since-hours 24
    python scripts/measure_adoption.py --min-rate 0.5          # 全体採用率のゲート（下回れば exit 1）
    python scripts/measure_adoption.py --min-needed-rate 0.5   # 必要場面だけの採用率でゲート
    python scripts/measure_adoption.py --include-tool-free     # ツール未使用セッションも分母に入れる
    python scripts/measure_adoption.py --excerpt 60            # 見逃しの依頼文を 60 字だけ表示

`--include-tool-free` を付けない既定では、**ツールを 1 つも呼ばなかったセッションは分母に入らない**
（`messages.tool_calls` のあるセッションだけを見る従来の挙動）。「必要場面なのにツールを 1 つも
使わなかった」ケースを見たいときだけ付ける。

`--excerpt` は既定 0（依頼文を出さない）。依頼文には機密が含まれうるので、共有前に内容を確認する。

終了コード: 0=正常 / 1=`--min-rate` または `--min-needed-rate` を下回った / 2=state.db が読めない。
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
import time

# 日本語 Windows のコンソール（cp932/cp1252）でも出力を落とさない。CI の windows-latest は
# cp1252 で、print() が UnicodeEncodeError になり **ゲートが落ちる**（実測）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

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

# 「必要場面」の手掛かり。**依頼文に現れる言い回しだけ**を置く（曖昧な語を入れると誤検出が増える）。
# `expects` は「その場面ならこれが使われるはず」の実ツール名。判定は近似で、完全な判定ではない。
NEED_CUES = (
    {
        "id": "sources",
        "label": "出典・根拠が要る",
        "expects": ("freeagent_lookup", "freeagent_grounded", "freeagent_agent"),
        "patterns": (
            r"出典", r"根拠", r"裏付け", r"エビデンス", r"引用",
            r"調べて", r"調べたい", r"文献", r"論文", r"先行研究", r"サーベイ",
            r"\bsources?\b", r"\bcitations?\b", r"\bevidence\b", r"\bpapers?\b",
            r"\bliterature\b", r"\bresearch\b", r"\bcite\b",
        ),
        # 学術コーパスの語。これがあれば下の `exclude` より優先して必要場面とみなす
        # （「API の設計に関する**論文**を探して」は除外しない）。
        "strong": (r"文献", r"論文", r"先行研究", r"サーベイ", r"\bpapers?\b", r"\bliterature\b"),
        # 実測（2026-10）: サービス・提供元・公式条件を探す依頼は、学術コーパス
        # （arXiv / Crossref / OpenAlex / Wikidata）では答えられない。`freeagent_lookup` に
        # "LLM inference API provider comparison alternatives" を引くと**論文 9 件が返り、
        # 提供元の一覧は返らない**。この型は web_search が正しいので見逃しに数えない。
        "exclude": (r"(?:API|ベンダー|プロバイダ|提供元)", r"利用条件|利用規約|商用承認"),
    },
    {
        "id": "multi_view",
        "label": "複数の視点・レビューが要る",
        "expects": ("freeagent_panel", "freeagent_consult", "freeagent_ask", "freeagent_fanout"),
        "patterns": (
            r"別の(?:AI|モデル|エージェント)", r"他の(?:AI|モデル)", r"複数(?:の)?(?:視点|モデル|意見)",
            r"多角(?:的|度)", r"セカンドオピニオン", r"反論", r"批判(?:的)?", r"弱点",
            r"見落とし", r"意見を聞いて", r"レビュー",
            r"\bsecond opinion\b", r"\bmultiple perspectives\b", r"\bcritique\b",
            r"\breview(?:er)?\b",
        ),
    },
    {
        "id": "stepwise",
        "label": "段階的な検討・仮説検証が要る",
        "expects": ("freeagent_think",),
        "patterns": (
            r"手順に分解", r"段階的", r"ステップに分け", r"仮説", r"切り分け",
            r"計画を立て", r"設計を検討", r"原因を(?:特定|探|切り分け)",
            r"\bstep[- ]by[- ]step\b", r"\bhypothes", r"\broot cause\b",
        ),
    },
    {
        "id": "bulk",
        "label": "多数の要素の一括処理が要る",
        "expects": ("freeagent_map", "freeagent_fanout"),
        "patterns": (
            r"それぞれ(?:要約|分類|処理)", r"一括", r"まとめて(?:要約|分類|処理)",
            r"\d+\s*(?:件|個|本)を(?:要約|分類|処理)", r"全部(?:要約|分類)",
            r"\bclassify\b", r"\bsummarize (?:each|all)\b",
        ),
    },
)

_CUE_RE = {cue["id"]: [re.compile(p, re.IGNORECASE) for p in cue["patterns"]] for cue in NEED_CUES}
# cue ごとの「学術コーパスの語」（`exclude` より優先）と「必要場面ではない」サイン。
_CUE_STRONG = {cue["id"]: [re.compile(p, re.IGNORECASE) for p in cue.get("strong", ())]
               for cue in NEED_CUES}
_CUE_EXCLUDE = {cue["id"]: [re.compile(p, re.IGNORECASE) for p in cue.get("exclude", ())]
                for cue in NEED_CUES}
# どの cue にも共通の「必要場面ではない」サイン。実測: 「読み取り専用の再レビュー。…を読み、」の
# ような**自分で読んで報告する依頼**が「レビュー」「調べて」で誤検出され、見逃しを水増ししていた。
_COMMON_EXCLUDE = (re.compile(r"読み取り専用"),)
# 機械が差し込む user ロールの通知（委譲の完了通知など）は**依頼文ではない**。
# 実測: "[ASYNC DELEGATION BATCH COMPLETE — deleg_0732bc70] …" が user ロールで保存され、
# その本文（委譲先の報告）の英語 "sources" が「出典が要る」と誤検出されていた。
_MACHINE_NOTICE = re.compile(r"^\s*\[[A-Za-z0-9][A-Za-z0-9 _\-—:./]{4,80}\]")


def _request_only(text: str) -> str:
    """依頼文だけを返す（Hermes が貼る添付コンテキスト＝ `@url` のページ本文などを落とす）。

    実測: 添付ページ本文に含まれる英語の `sources`（`utm_source` やページの見出し）が
    「出典が要る」と誤検出され、見逃しに数えられていた。**添付は利用者の依頼ではない**。
    """
    out = text
    for marker in ("\n--- Attached Context ---", "\n--- 添付コンテキスト ---"):
        idx = out.find(marker)
        if idx >= 0:
            out = out[:idx]
    return out


def classify_prompts(texts) -> list[str]:
    """依頼文（user ロールの本文）から「必要場面」の cue id を重複なく返す。

    1 つのセッションに複数の依頼文があるので、どれかに当たれば必要場面とみなす。
    ここは**近似判定**であり、正しさを保証しない（限界は README / SPEC に書く）。
    """
    found: list[str] = []
    for text in texts or ():
        if not isinstance(text, str) or not text:
            continue
        text = _request_only(text)
        if not text.strip() or _MACHINE_NOTICE.match(text):
            continue
        if any(rx.search(text) for rx in _COMMON_EXCLUDE):
            continue
        for cue in NEED_CUES:
            cid = cue["id"]
            if cid in found:
                continue
            # サービス・規約の依頼はコーパス外（学術コーパスの語があるときだけ例外）。
            if (any(rx.search(text) for rx in _CUE_EXCLUDE[cid])
                    and not any(rx.search(text) for rx in _CUE_STRONG[cid])):
                continue
            if any(rx.search(text) for rx in _CUE_RE[cid]):
                found.append(cid)
    return found


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


def load_sessions(con: sqlite3.Connection, sessions: int, since_hours: float,
                  include_tool_free: bool):
    """(order, per_session, prompts) を返す。order は新しい順のセッション ID。

    既定（`include_tool_free=False`）は **ツールを呼んだセッションだけ**を対象にする（従来の挙動）。
    `True` にすると `sessions` テーブルから直近 N 件を取るので、「必要場面なのにツールを
    1 つも使わなかった」セッションも分母に入る。
    """
    if include_tool_free:
        where, params = "", []
        if since_hours:
            where = "where started_at >= ?"
            params.append(time.time() - since_hours * 3600)
        order = [row[0] for row in con.execute(
            f"select id from sessions {where} order by started_at desc limit ?",
            params + [sessions]).fetchall()]
    else:
        where = "tool_calls is not null and tool_calls != ''"
        params: list = []
        if since_hours:
            where += " and timestamp >= datetime('now', ?)"
            params.append(f"-{since_hours} hours")
        order = []
        for (sid,) in con.execute(
                f"select session_id from messages where {where} order by id desc", params):
            if sid not in order:
                order.append(sid)
                if len(order) >= sessions:
                    break

    per_session: dict[str, collections.Counter] = {sid: collections.Counter() for sid in order}
    prompts: dict[str, list[str]] = {sid: [] for sid in order}
    if order:
        marks = ",".join("?" * len(order))
        for sid, raw in con.execute(
                f"select session_id, tool_calls from messages where session_id in ({marks})"
                " and tool_calls is not null and tool_calls != ''", order):
            if sid in per_session:
                for name in tool_names_of(raw):
                    per_session[sid][normalize(name)] += 1
        for sid, content in con.execute(
                f"select session_id, content from messages where session_id in ({marks})"
                " and role = 'user'", order):
            if sid in prompts and isinstance(content, str) and content.strip():
                prompts[sid].append(content)
    return order, per_session, prompts


def needed_scene_report(order, per_session, prompts) -> dict:
    """必要場面（cue 別）の使用・見逃しをまとめる（判定は近似）。"""
    cue_sids: dict[str, list[str]] = {cue["id"]: [] for cue in NEED_CUES}
    for sid in order:
        for cid in classify_prompts(prompts.get(sid) or []):
            cue_sids[cid].append(sid)
    needed = [sid for sid in order if any(sid in cue_sids[cue["id"]] for cue in NEED_CUES)]
    using = [sid for sid in needed if any(n.startswith(FREAGENT_PREFIX) for n in per_session[sid])]

    cues = []
    for cue in NEED_CUES:
        sids = cue_sids[cue["id"]]
        if not sids:
            continue
        hit = [sid for sid in sids if any(n in cue["expects"] for n in per_session[sid])]
        missed = [sid for sid in sids if sid not in hit]
        instead: collections.Counter = collections.Counter()
        for sid in missed:
            for name in per_session[sid]:
                if name in COMPETING or name.startswith("deliberation."):
                    instead[name] += per_session[sid][name]
        cues.append({
            "id": cue["id"], "label": cue["label"], "expects": list(cue["expects"]),
            "sessions": len(sids), "used": len(hit), "missed": len(missed),
            "missed_sessions": missed, "instead": dict(instead.most_common(5)),
        })
    return {
        "needed_sessions": len(needed),
        "needed_using_freeagent": len(using),
        "needed_rate": round(len(using) / len(needed), 4) if needed else 0.0,
        "cues": cues,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="freeagent-bind の自発利用率を state.db から測る")
    ap.add_argument("--db", default=None, help="state.db のパス（既定は自動検出）")
    ap.add_argument("--sessions", type=int, default=20, help="対象にする直近セッション数（既定 20）")
    ap.add_argument("--since-hours", type=float, default=0.0, help="直近 N 時間だけを対象にする")
    ap.add_argument("--json", action="store_true", help="機械可読な JSON で出す")
    ap.add_argument("--min-rate", type=float, default=None,
                    help="全体の採用率がこの値未満なら exit 1（ゲートとして使う）")
    ap.add_argument("--min-needed-rate", type=float, default=None,
                    help="必要場面だけの採用率がこの値未満なら exit 1")
    ap.add_argument("--include-tool-free", action="store_true",
                    help="ツールを呼ばなかったセッションも分母に入れる（既定は除外）")
    ap.add_argument("--excerpt", type=int, default=0,
                    help="見逃しセッションの依頼文を N 字だけ表示する（既定 0=出さない。機密に注意）")
    args = ap.parse_args()

    db = find_db(args.db)
    if not db:
        print("✗ state.db が見つかりません（--db で指定するか HERMES_HOME を設定してください）",
              file=sys.stderr)
        return 2

    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        order, per_session, prompts = load_sessions(
            con, args.sessions, args.since_hours, args.include_tool_free)
    finally:
        # Windows では開いたままだと state.db を掴み続ける（テストの後始末も失敗する）。
        con.close()

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

    scenes = needed_scene_report(order, per_session, prompts)

    if args.json:
        print(json.dumps({
            "db": db, "sessions": total_sessions, "sessions_using_freeagent": len(using),
            "adoption_rate": round(rate, 4),
            "include_tool_free": bool(args.include_tool_free),
            "freeagent_calls": dict(tool_counts.most_common()),
            "competing_calls": dict(competing.most_common()),
            "needed_scenes": scenes,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"state.db: {db}")
        print(f"対象セッション: {total_sessions} 件"
              + (f"（直近 {args.since_hours} 時間）" if args.since_hours else "（直近の記録から）")
              + ("（ツール未使用も含む）" if args.include_tool_free else "（ツールを呼んだセッションのみ）"))
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
        needed = scenes["needed_sessions"]
        if needed:
            print(f"  必要場面（依頼文からの近似判定）: {needed}/{total_sessions} セッション")
            print(f"    そのうち freeagent を使った: {scenes['needed_using_freeagent']} 件"
                  f" → **必要場面の採用率 {scenes['needed_rate']:.0%}**")
            for cue in scenes["cues"]:
                print(f"    • {cue['label']}: {cue['sessions']} 件中 {cue['used']} 件で使用"
                      f"（見逃し {cue['missed']}）")
                print(f"        期待するツール: {', '.join(cue['expects'])}")
                if cue["instead"]:
                    instead = " / ".join(f"{n} {c}" for n, c in cue["instead"].items())
                    print(f"        見逃したセッションで使われていた競合（回数はセッション全体）: {instead}")
                if args.excerpt > 0:
                    for sid in cue["missed_sessions"]:
                        text = " ".join((prompts.get(sid) or [""])[0].split())
                        print(f"        - {sid}: {text[:args.excerpt]}")
            print("    ⚠ 依頼文の言い回しによる近似です。必要場面の完全な判定ではありません。")
        else:
            print("  必要場面（依頼文からの近似判定）: 0 セッション"
                  "（依頼文に手掛かりが無いか、対象が少なすぎます）")
        if not args.include_tool_free:
            print("  ※ ツールを 1 つも呼ばなかったセッションは分母に入りません"
                  "（--include-tool-free で含められます）")

        print()
        print("  測定は「新プロセスの `hermes chat -q \"<ツール名を含まない依頼>\"`」で行う")
        print("  （実行中セッションは起動時のツール一覧を保持するので変更が反映されない）。")
        print("  最低 2 標本。1/2 と 2/2 の差は標本 1 つでは判定できない。")

    failed = []
    if args.min_rate is not None and rate < args.min_rate:
        failed.append(f"採用率 {rate:.0%} < 下限 {args.min_rate:.0%}")
    needed_rate = scenes["needed_rate"]
    if (args.min_needed_rate is not None and scenes["needed_sessions"]
            and needed_rate < args.min_needed_rate):
        failed.append(f"必要場面の採用率 {needed_rate:.0%} < 下限 {args.min_needed_rate:.0%}")
    if failed:
        print("\n✗ " + " / ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())