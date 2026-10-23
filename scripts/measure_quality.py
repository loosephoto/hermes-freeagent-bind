"""戦略ごとの**正答率**を測る（単体 / 多数決 / MoA / カスケード）。

速度は `measure_kb.py`、自発率は `measure_adoption.py` が見る。ここが見るのは「答が合っているか」で、
オーケストレーション（何人集めるか・どう統合するか）の良し悪しはこれでしか比べられない。

* 採点は**宣言した期待値に対する機械判定**（数値は許容誤差つき、文字列は受理リストの部分一致）。
  主観的な文章は採点しない（LLM 審判を足すのは別の変更）。
* 呼び出しは実際に走る（Free の枠を使う）。結果は `FREEAGENT_QUALITY_PATH`
  （既定 `<state>/quality.jsonl`）に追記する。
* ネットワークが要る（`hermes proxy start` か各社のキー）。

使い方:
    python scripts/measure_quality.py                      # 6 ケース・4 戦略・プール 8 体
    python scripts/measure_quality.py --cases 10 --repeat 2
    python scripts/measure_quality.py --strategies single,panel --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
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

# 期待値は「機械的に判定できる」ものだけを置く。数値は数値として比較する。
CASES: list[dict] = [
    {"id": "fib25", "kind": "numeric", "expect": ["75025"],
     "question": "What is the 25th Fibonacci number? Answer with the number only."},
    {"id": "cube17", "kind": "numeric", "expect": ["4913"],
     "question": "What is 17 cubed? Answer with the number only."},
    {"id": "westphalia", "kind": "numeric", "expect": ["1648"],
     "question": "In which year was the Treaty of Westphalia signed? Answer with the year only."},
    {"id": "element47", "kind": "text", "expect": ["silver", "銀"],
     "question": "Which element has atomic number 47? Answer with the element name only."},
    {"id": "melting", "kind": "text", "expect": ["tungsten", "wolfram", "タングステン"],
     "question": "Which metal has the highest melting point? Answer with the element name only."},
    {"id": "galilean3", "kind": "text", "expect": ["io"],
     "question": ("Rank the four Galilean moons of Jupiter by diameter, largest first. "
                  "Answer with the third one only.")},
    {"id": "kosovo", "kind": "text", "expect": ["xk"],
     "question": ("Which two-letter country code is commonly used for Kosovo "
                  "(user-assigned ISO 3166-1 alpha-2)? Answer with the code only.")},
    {"id": "strawberry", "kind": "numeric", "expect": ["3"],
     "question": "How many times does the letter r appear in the word strawberry? Answer with the number only."},
    {"id": "ouaga", "kind": "text", "expect": ["ouagadougou", "ワガドゥグ"],
     "question": "What is the capital of Burkina Faso? Answer with the city name only."},
    {"id": "log2_1024", "kind": "numeric", "expect": ["10"],
     "question": "What is log2 of 1024? Answer with the number only."},
]

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def normalize(text: str) -> str:
    low = (text or "").strip().lower()
    low = low.replace("　", " ")
    low = re.sub(r"\s+", " ", low)
    return low.strip(" .。!！?？:：;；\"'`*")


def grade(text: str, case: dict) -> bool:
    """宣言した期待値に合うか（数値は許容誤差、文字列は受理リストの部分一致）。"""
    low = normalize(text)
    if not low:
        return False
    if case["kind"] == "numeric":
        cleaned = low.replace(",", "").replace("，", "")
        for match in _NUM.finditer(cleaned):
            try:
                value = float(match.group(0))
            except ValueError:
                continue
            for want in case["expect"]:
                target = float(want)
                if abs(value - target) <= max(0.5, abs(target) * 1e-6):
                    return True
        return False
    return any(normalize(want) in low for want in case["expect"])


def vote(texts: list[str]) -> str:
    """正規化した文字列の多数決（採点はしない＝期待値に寄せない）。同数は先着。"""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for text in texts:
        key = normalize(text)[:120]
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, text)
    if not counts:
        return ""
    best = sorted(counts.items(), key=lambda kv: (-kv[1], list(counts).index(kv[0])))[0][0]
    return first[best]


MOA_PROMPT = (
    "You are given several candidate answers to a question, produced by different models.\n"
    "Synthesize them into ONE best answer. Resolve disagreements by checking the facts; do not average.\n"
    "Reply with the answer only (no explanation).\n\n"
    "Question: {question}\n\nCandidates:\n{candidates}"
)


def _answers_text(pairs: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    out = []
    for ref, res in pairs:
        out.append((ref, (res.get("text") or "").strip() if not res.get("error") else ""))
    return out


def run_strategy(name: str, refs: list[str], case: dict, max_tokens: int) -> dict:
    """1 ケース 1 戦略を実行して {ok, text, calls, wall_s, detail} を返す。"""
    started = time.time()
    calls = 0
    detail = ""
    text = ""

    if name == "single":
        res = S.call_model(refs[0], case["question"], max_tokens=max_tokens, temperature=0.0,
                           kind="eval")
        calls = 1
        text = res.get("text") or ""
        detail = refs[0] + ("" if not res.get("error") else f" ({res['error'][:60]})")

    elif name == "panel":
        results = S.ask_many(refs, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        calls = len(refs)
        texts = [t for _, t in _answers_text(list(zip(refs, results))) if t]
        text = vote(texts)
        detail = f"{len(texts)}/{len(refs)} が回答"

    elif name == "moa":
        results = S.ask_many(refs, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        texts = [t for _, t in _answers_text(list(zip(refs, results))) if t]
        calls = len(refs)
        if not texts:
            text = ""
        else:
            candidates = "\n\n".join(f"[{i + 1}] {t[:1200]}" for i, t in enumerate(texts))
            agg = S.call_model(refs[-1], MOA_PROMPT.format(question=case["question"],
                                                           candidates=candidates),
                               max_tokens=max_tokens, temperature=0.0, kind="eval")
            calls += 1
            text = agg.get("text") or ""
        detail = f"{len(texts)} 票を統合"

    elif name == "refine":
        # **出荷済みの機能をそのまま測る**（panel + MoA の集約層）。moa との違いは、統合役の選択・
        # 許可リスト・枠・クォータがすべてサーバー側の実装を通ること。
        data = S.tool_panel({"question": case["question"], "models": list(refs),
                             "refine": True, "max_tokens": max_tokens})
        refined = data.get("refined") or {}
        calls = int(data.get("calls") or len(refs))
        text = refined.get("text") or ""
        detail = f"統合役 {refined.get('model')}"
        if refined.get("error"):
            detail += f" ({str(refined['error'])[:50]})"

    elif name == "cascade":
        # 2 体で一致したら止める（FrugalGPT のカスケード）。一致しなければ全員に広げる。
        head = refs[:2] if len(refs) > 2 else refs
        results = S.ask_many(head, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        calls = len(head)
        texts = [t for _, t in _answers_text(list(zip(head, results))) if t]
        agree = len({normalize(t)[:120] for t in texts}) == 1 and len(texts) == len(head)
        if agree:
            text = texts[0]
            detail = f"{len(head)} 体で一致したので打ち切り"
        else:
            rest = refs[len(head):]
            more = S.ask_many(rest, case["question"], max_tokens=max_tokens, temperature=0.0,
                              kind="eval") if rest else []
            calls += len(rest)
            texts += [t for _, t in _answers_text(list(zip(rest, more))) if t]
            text = vote(texts)
            detail = f"一致せず {len(refs)} 体へ拡大"

    else:
        raise SystemExit(f"未知の戦略: {name}")

    return {"strategy": name, "ok": grade(text, case), "text": (text or "")[:160],
            "calls": calls, "wall_s": round(time.time() - started, 2), "detail": detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="戦略ごとの正答率を測る（Free の枠を使う）")
    parser.add_argument("--cases", default="6", help="使うケース数（既定 6・all で全件）")
    parser.add_argument("--strategies", default="single,panel,moa,refine,cascade",
                        help="カンマ区切り（single / panel / moa / refine=サーバーの MoA / cascade）")
    parser.add_argument("--pool", type=int, default=8, help="参加モデル数の上限（既定 8）")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--repeat", type=int, default=1, help="各ケースの繰り返し回数")
    parser.add_argument("--json", action="store_true", help="JSON だけを出す（機械処理用）")
    parser.add_argument("--no-write", action="store_true", help="quality.jsonl に書かない")
    args = parser.parse_args(argv)

    refs = S.free_model_refs(free_only=True, chat_only=True)[: max(1, args.pool)]
    if not refs:
        print("推論可能な Free モデルが 0 件です（`hermes proxy start` か各社のキーを確認）")
        return 2
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    cases = CASES if args.cases == "all" else CASES[: max(1, int(args.cases))]

    rows: list[dict] = []
    for case in cases:
        for repeat in range(max(1, args.repeat)):
            for name in strategies:
                row = run_strategy(name, refs, case, args.max_tokens)
                row.update({"case": case["id"], "repeat": repeat + 1,
                            "pool": len(refs), "server_version": S.SERVER_VERSION})
                rows.append(row)
                if not args.json:
                    mark = "✓" if row["ok"] else "✗"
                    print(f"{mark} {case['id']:12s} {name:8s} {row['wall_s']:6.2f}s "
                          f"calls={row['calls']:2d} {row['detail'][:40]:40s} {row['text'][:60]}",
                          flush=True)

    summary = {}
    for name in strategies:
        picked = [r for r in rows if r["strategy"] == name]
        summary[name] = {
            "correct": sum(1 for r in picked if r["ok"]),
            "runs": len(picked),
            "accuracy": round(sum(1 for r in picked if r["ok"]) / len(picked), 3) if picked else None,
            "calls_mean": round(statistics.mean(r["calls"] for r in picked), 2) if picked else None,
            "wall_mean_s": round(statistics.mean(r["wall_s"] for r in picked), 2) if picked else None,
        }

    if not args.no_write:
        path = os.environ.get("FREEAGENT_QUALITY_PATH") or os.path.join(
            S.state_dir(), "quality.jsonl")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                for row in rows:
                    fh.write(json.dumps({**row, "at": time.time()}, ensure_ascii=False) + "\n")
        except OSError as exc:
            print(f"（記録できませんでした: {exc}）", file=sys.stderr)

    payload = {"server_version": S.SERVER_VERSION, "pool": len(refs), "cases": len(cases),
               "repeat": args.repeat, "strategies": summary, "rows": rows}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("\n=== 正答率 ===")
        print(f"{'戦略':10s} {'正答':>6s} {'実行':>4s} {'正答率':>7s} {'平均呼び出し':>10s} {'平均秒':>7s}")
        for name, row in summary.items():
            print(f"{name:10s} {row['correct']:6d} {row['runs']:4d} {row['accuracy']:7.3f} "
                  f"{row['calls_mean']:10.2f} {row['wall_mean_s']:7.2f}")
        print("\n※ ケース数が少ないと天井（全戦略 100%）に当たります。判断は --cases all と --repeat 2 以上で。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
