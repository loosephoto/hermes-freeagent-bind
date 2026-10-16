#!/usr/bin/env python3
"""measure_relevance.py — 知識バックエンドの**関連性**（速さではなく中身）を測る。

なぜ必要か
----------
`measure_kb.py` は**所要時間**、`measure_adoption.py` は**自発利用率**を測る。**「速いが無関係」**は
どちらにも映らない（実測 2026-10-17: ja.wikipedia は 0.6 秒で正常に見えていたが、「大規模言語モデル」で
マークアップ言語・機械翻訳を返していた。所要時間では絶対に検出できない）。

固定のクエリ集合 × ソースごとの「トップ 1 に含まれるべき語」で的中を数え、JSONL に追記する。
**上流の順位は変わるので、外れても失敗にはしない**（記録して傾向を見る。実行する検証であってゲートではない）。

使い方（リポジトリ直下で）::

    python scripts/measure_relevance.py                # 1 巡測って追記
    python scripts/measure_relevance.py --json         # 記録する内容を表示（追記しない）
    python scripts/measure_relevance.py --report       # 追記済みの記録を集計
    FREEAGENT_RELEVANCE_PATH=/tmp/rel.jsonl python scripts/measure_relevance.py

ネットワークを使う（キー不要のソースだけを対象にする）。モデルは呼ばない。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from freeagent_bind import server as S  # noqa: E402

# (query, source, トップ1に含まれるべき語 or None, lang or None)
# lang=None は「指定しない」＝実装の自動判定（CJK→ja / それ以外→en）をそのまま測る。
CASES: list[tuple[str, str, str | None, str | None]] = [
    ("大規模言語モデル", "wikipedia", "大規模言語モデル", None),
    ("強化学習", "wikipedia", "強化学習", None),
    ("量子コンピュータ", "wikipedia", "量子コンピュータ", None),
    ("Transformer", "wikipedia", "transformer", None),
    ("diffusion model", "wikipedia", "diffusion model", None),
    ("GRPO", "wikipedia", None, None),                      # 期待タイトル無し（領域の記録のみ）
    ("強化学習", "wikidata", "強化学習", None),
    ("大規模言語モデル", "wikidata", "大規模言語モデル", None),
    # wikidata は曖昧語で 1 位が別分野になる（実測: "Transformer" は 1 位=変圧器、2 位=トランスフォーマー
    # （機械学習アーキテクチャ）。en でも電気機器が 1 位なので、言語では直らない＝記録のみにする）
    ("Transformer", "wikidata", None, None),
    ("強化学習", "openalex", "強化学習", None),
    ("Transformer", "openalex", "transformer", None),
    ("大規模言語モデル", "crossref", "大規模言語モデル", None),
]

# トップ 1 を取り出すフィールド（wikidata の items は label を使う）
FIELD = {"wikidata": "label"}


def record_path() -> str:
    override = os.environ.get("FREEAGENT_RELEVANCE_PATH", "")
    if override:
        return override
    return os.path.join(S.state_dir(), "relevance.jsonl")


def top_of(source: str, query: str, limit: int = 3) -> tuple[str, str]:
    """(トップ1のタイトル, 状態) を返す。状態は ok / empty / error。"""
    fn = S.KB_BACKENDS.get(source)
    if fn is None:
        return "", "unknown_source"
    try:
        got = fn(query, limit, {"lang": ""})
    except Exception as exc:                      # 例外は漏らさない（規約 1）
        return f"{type(exc).__name__}: {exc}", "error"
    if got.get("error"):
        kind = "rate_limited" if "429" in got["error"] else "error"
        return got["error"][:120], kind
    items = got.get("items") or []
    if not items:
        return "", "empty"
    field = FIELD.get(source, "title")
    return str(items[0].get(field) or ""), "ok"


def run(json_only: bool = False) -> int:
    rows = []
    for query, source, expect, lang in CASES:
        t0 = time.perf_counter()
        top, status = top_of(source, query)
        ms = round((time.perf_counter() - t0) * 1000)
        hit = None
        if status == "ok" and expect:
            hit = expect.casefold() in top.casefold()
        rows.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": source, "query": query,
                     "expect": expect, "lang": lang or "(自動)", "top": top, "status": status,
                     "hit": hit, "ms": ms})
        mark = {True: "✓", False: "✗", None: "・"}[hit]
        print(f"{mark} {source:9} {query!r:20} {ms:>5}ms  top={top[:60]!r}"
              f"{'' if status == 'ok' else f'  status={status}'}")
    if json_only:
        return 0
    path = record_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    scored = [r for r in rows if r["hit"] is not None]
    print(f"\n的中 {sum(1 for r in scored if r['hit'])}/{len(scored)}（期待を書いたケースのみ）"
          f" / 記録のみ {sum(1 for r in rows if r['hit'] is None)} 件 → {path}")
    return 0


def report() -> int:
    path = record_path()
    if not os.path.exists(path):
        print(f"記録がありません: {path}")
        return 1
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    if not rows:
        print("記録が空です")
        return 1
    latest = rows[-1]["at"][:10]
    by_source: dict[str, list] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
    print(f"記録 {len(rows)} 件（最終 {latest}）/ {path}\n")
    print("| ソース | 試行 | 採点 | 的中 | 的中率 | 外れ（直近のトップ1） |")
    print("|---|---:|---:|---:|---:|---|")
    for source, group in sorted(by_source.items()):
        scored = [r for r in group if r["hit"] is not None]
        hits = sum(1 for r in scored if r["hit"])
        misses = [r for r in scored if not r["hit"]]
        rate = f"{hits / len(scored) * 100:.0f}%" if scored else "-"
        miss_note = "; ".join(f"{r['query']}→{r['top'][:34]}" for r in misses[-3:]) or "-"
        print(f"| {source} | {len(group)} | {len(scored)} | {hits} | {rate} | {miss_note} |")
    print("\n※ 上流の順位は変わるので、外れ＝こちらの退行とは限らない（実行する検証であってゲートではない）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="知識バックエンドの関連性を測る")
    parser.add_argument("--report", action="store_true", help="追記済みの記録を集計する")
    parser.add_argument("--json", action="store_true", help="記録する内容を表示する（追記しない）")
    args = parser.parse_args()
    return report() if args.report else run(json_only=args.json)


if __name__ == "__main__":
    sys.exit(main())
