"""戦略ごとの**推論品質**を測る（単体 / 多数決 / MoA / サーバーの refine / カスケード）。

速度は `measure_kb.py`、自発率は `measure_adoption.py` が見る。ここが見るのは「答が合っているか」と
「なぜ外したか」で、オーケストレーション（何人集めるか・どう統合するか）の良し悪しはこれでしか比べられない。

v0.14.0 で**測り方そのものを作り直した**（実測で見つけた 2 つの誤りを直したため）:

* **言及と回答の区別**: 旧採点は本文の全数値を走査していたので、誤答でも正解値を言及していれば合格し、
  部分一致のため ``"This is not silver, it is mercury."`` も合格していた（9 例のプローブで 4 件誤判定）。
  → 採点は `freeagent_bind.grading` に移し、**最終回答を抽出してから厳密比較**する。
* **切り詰めの扱い**: 実測で truncated 3/24、うち 2 件は reasoning が正しく最終行だけが無かった。
  → ``no_answer``（答えが書かれていない）を誤答と別に数え、**1 回だけ再質問**して救済する。

さらに:

* ケースは **自前生成の難問**（`scripts/gen_cases.py`・帯 1/2/3 を事前登録）を持ち込める。既存 10 問は
  1 段で解ける易問で、3〜6 問では全戦略 100%（天井）になった。ただし難問化だけでは天井は壊れない
  （実測 22/24）。だから**難易度は帯として固定し、全帯の結果を報告**する（結果を見て帯を選ばない）。
* **連続指標**を主に出す（クラスタ熵・戦略内一致率・校正誤差）。実測: 二値の 10 ポイント差を 80%
  検出力で見るには 300〜400 ケース必要だが、連続指標なら 30〜50 で d=0.5 を検出できる。
* 点推定だけで語らない。基準戦略（既定 `single`）との**対応のある差・95% 信頼区間・McNemar の p 値**を出す。

呼び出しは実際に走る（Free の枠を使う）。結果は `FREEAGENT_QUALITY_PATH`（既定 `<state>/quality.jsonl`）
に追記する。ネットワークが要る（`hermes proxy start` か各社のキー）。

使い方:
    python scripts/measure_quality.py                                   # 生成 12 件（帯 1/2/3・各 4 件）
    python scripts/measure_quality.py --cases builtin --strategies single,panel
    python scripts/measure_quality.py --per-tier 12 --repeat 2 --calibrate --json
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
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from freeagent_bind import grading as G  # noqa: E402
from freeagent_bind import server as S  # noqa: E402
import gen_cases  # noqa: E402

# 期待値は「機械的に判定できる」ものだけを置く（数値・集合・区間・語）。文章は採点しない。
BUILTIN_CASES: list[dict] = [
    {"id": "fib25", "tier": 1, "kind": "numeric", "expect": ["75025"],
     "question": "What is the 25th Fibonacci number? "
                 "End with a line 'Final answer: <number>'."},
    {"id": "cube17", "tier": 1, "kind": "numeric", "expect": ["4913"],
     "question": "What is 17 cubed? End with a line 'Final answer: <number>'."},
    {"id": "westphalia", "tier": 1, "kind": "numeric", "expect": ["1648"],
     "question": "In which year was the Treaty of Westphalia signed? "
                 "End with a line 'Final answer: <year>'."},
    {"id": "element47", "tier": 1, "kind": "text", "expect": ["silver", "銀"],
     "question": "Which element has atomic number 47? "
                 "End with a line 'Final answer: <element>'."},
    {"id": "melting", "tier": 1, "kind": "text", "expect": ["tungsten", "wolfram"],
     "question": "Which metal has the highest melting point? "
                 "End with a line 'Final answer: <element>'."},
    {"id": "galilean3", "tier": 2, "kind": "text", "expect": ["io"],
     "question": ("Rank the four Galilean moons of Jupiter by diameter, largest first. "
                  "End with a line 'Final answer: <the third one>'.")},
    {"id": "strawberry", "tier": 2, "kind": "numeric", "expect": ["3"],
     "question": "How many times does the letter r appear in the word strawberry? "
                 "End with a line 'Final answer: <number>'."},
    {"id": "log2_1024", "tier": 2, "kind": "numeric", "expect": ["10"],
     "question": "What is log2 of 1024? End with a line 'Final answer: <number>'."},
    {"id": "ouaga", "tier": 2, "kind": "text", "expect": ["ouagadougou"],
     "question": "What is the capital of Burkina Faso? "
                 "End with a line 'Final answer: <city>'."},
    {"id": "kosovo", "tier": 2, "kind": "text", "expect": ["xk"],
     "question": ("Which two-letter country code is commonly used for Kosovo "
                  "(user-assigned ISO 3166-1 alpha-2)? End with a line 'Final answer: <code>'.")},
]

MOA_PROMPT = (
    "You are given several candidate answers to a question, produced by different models.\n"
    "Synthesize them into ONE best answer. Resolve disagreements by checking the facts; do not "
    "average.\nReply with the answer only, then a final line 'Final answer: <value>'.\n\n"
    "Question: {question}\n\nCandidates:\n{candidates}"
)

# 抽出できなかったときの再質問（1 回だけ）。切り詰めで最終行が落ちた回答を救済する。
RETRY_PROMPT = (
    "{question}\n\nYour previous answer could not be scored because it did not end with a final "
    "answer line. Do not explain. Reply with exactly one line:\nFinal answer: <value>\n\n"
    "Previous answer (may be truncated):\n{previous}"
)

CALIBRATION_SUFFIX = "\nThen add one more line: 'Confidence: <0-100>'."

CONFIDENCE_RE = re.compile(r"(?:confidence|確信度)\s*[:：]?\s*(\d{1,3})", re.I)


def parse_confidence(text: str) -> float | None:
    """自己申告の確信度（0〜100）を拾う。無ければ None（無いものを 100 と見なさない）。"""
    if not text:
        return None
    matches = CONFIDENCE_RE.findall(text)
    if not matches:
        return None
    return min(100.0, max(0.0, float(matches[-1])))


def load_cases(source: str, per_tier: int, seed: int, limit: int | None) -> list[dict]:
    """ケースを用意する（`builtin` / `gen` / `both`）。生成分は独立検算を通ったものだけ使う。"""
    cases: list[dict] = []
    if source in ("builtin", "both"):
        cases += [dict(case) for case in BUILTIN_CASES]
    if source in ("gen", "both"):
        generated = gen_cases.build(per_tier=per_tier, seed=seed)
        bad = [case["id"] for case in generated if not gen_cases.verify(case)[0]]
        if bad:
            raise SystemExit(f"生成ケースの独立検算に失敗しました: {bad[:3]}")
        cases += generated
    if limit:
        cases = cases[:limit]
    return cases


def _answers_text(pairs: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    out = []
    for ref, res in pairs:
        out.append((ref, (res.get("text") or "").strip() if not res.get("error") else ""))
    return out


def vote(texts: list[str]) -> str:
    """**抽出した最終回答**の多数決（正規化して数える。期待値には寄せない）。同数は先着。"""
    counts: dict[str, int] = {}
    first: dict[str, str] = {}
    for text in texts:
        answer, _ = G.extract_final_answer(text)
        key = G.normalize_value(answer) if answer else ""
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, text)
    if not counts:
        return ""
    best = sorted(counts.items(), key=lambda kv: (-kv[1], list(counts).index(kv[0])))[0][0]
    return first[best]


def _call(ref: str, prompt: str, max_tokens: int) -> dict:
    return S.call_model(ref, prompt, max_tokens=max_tokens, temperature=0.0, kind="eval")


def run_strategy(name: str, refs: list[str], case: dict, max_tokens: int) -> dict:
    """1 ケース 1 戦略を実行して ``{text, calls, wall_s, detail, attempts, truncated}`` を返す。"""
    started = time.time()
    calls = 0
    detail = ""
    text = ""
    attempts: list[str] = []
    truncated = False

    if name == "single":
        res = _call(refs[0], case["question"], max_tokens)
        calls = 1
        text = res.get("text") or ""
        truncated = bool(res.get("truncated"))
        attempts = [text]
        detail = refs[0] + ("" if not res.get("error") else f" ({str(res['error'])[:60]})")

    elif name == "panel":
        results = S.ask_many(refs, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        calls = len(refs)
        attempts = [t for _, t in _answers_text(list(zip(refs, results))) if t]
        text = vote(attempts)
        detail = f"{len(attempts)}/{len(refs)} が回答"

    elif name == "moa":
        results = S.ask_many(refs, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        calls = len(refs)
        attempts = [t for _, t in _answers_text(list(zip(refs, results))) if t]
        if attempts:
            candidates = "\n\n".join(f"[{i + 1}] {t[:1200]}" for i, t in enumerate(attempts))
            agg = _call(refs[-1], MOA_PROMPT.format(question=case["question"],
                                                   candidates=candidates), max_tokens)
            calls += 1
            text = agg.get("text") or ""
            truncated = bool(agg.get("truncated"))
        detail = f"{len(attempts)} 票を統合"

    elif name == "refine":
        # **出荷済みの機能をそのまま測る**（panel + MoA の集約層）。統合役の選択・枠・クォータを
        # すべてサーバー側の実装が通る。
        data = S.tool_panel({"question": case["question"], "models": list(refs),
                             "refine": True, "max_tokens": max_tokens})
        refined = data.get("refined") or {}
        calls = int(data.get("calls") or len(refs))
        attempts = [a.get("text") or "" for a in data.get("answers", []) if not a.get("error")]
        text = refined.get("text") or ""
        detail = f"統合役 {refined.get('model')}"
        if refined.get("error"):
            detail += f" ({str(refined['error'])[:50]})"

    elif name == "cascade":
        # 2 体で**抽出後の回答**が一致したら止める（FrugalGPT のカスケード）。一致しなければ広げる。
        head = refs[:2] if len(refs) > 2 else refs
        results = S.ask_many(head, case["question"], max_tokens=max_tokens, temperature=0.0,
                             kind="eval")
        calls = len(head)
        attempts = [t for _, t in _answers_text(list(zip(head, results))) if t]
        keys = [G.normalize_value(G.extract_final_answer(t)[0] or "") for t in attempts]
        agree = len(attempts) == len(head) and bool(keys[0]) and len(set(keys)) == 1
        if agree:
            text = attempts[0]
            detail = f"{len(head)} 体で一致したので打ち切り"
        else:
            rest = refs[len(head):]
            more = S.ask_many(rest, case["question"], max_tokens=max_tokens, temperature=0.0,
                              kind="eval") if rest else []
            calls += len(rest)
            attempts += [t for _, t in _answers_text(list(zip(rest, more))) if t]
            text = vote(attempts)
            detail = f"一致せず {len(refs)} 体へ拡大"

    else:
        raise SystemExit(f"未知の戦略: {name}")

    return {"strategy": name, "text": (text or ""), "calls": calls,
            "wall_s": round(time.time() - started, 2), "detail": detail,
            "attempts": attempts, "truncated": truncated}


def grade_row(row: dict, case: dict, retry_ref: str | None, max_tokens: int) -> dict:
    """採点し、必要なら 1 回だけ再質問する（``no_answer`` を誤答に変換しない）。"""
    graded = G.grade(row["text"], case, truncated=bool(row.get("truncated")))
    row.update({"ok": graded["ok"], "answer": graded["answer"], "how": graded["how"],
                "verdict": graded["verdict"], "retried": False})
    if graded["verdict"] == "no_answer" and retry_ref:
        repair = _call(retry_ref, RETRY_PROMPT.format(question=case["question"],
                                                      previous=(row["text"] or "")[:1500]),
                       max(64, min(max_tokens, 256)))
        row["calls"] += 1
        row["retried"] = True
        row["retry_text"] = (repair.get("text") or "")[:400]
        if not repair.get("error"):
            second = G.grade(repair.get("text") or "", case)
            row["text"] = repair.get("text") or row["text"]
            row.update({"ok": second["ok"], "answer": second["answer"], "how": second["how"],
                        "verdict": "correct" if second["ok"] else second["verdict"]})
    row["no_answer"] = row["verdict"] == "no_answer"
    row["cluster_entropy"] = (G.cluster_entropy(row["attempts"])
                              if len(row["attempts"]) > 1 else None)
    row["agreement"] = (G.agreement_rate(row["attempts"])
                        if len(row["attempts"]) > 1 else None)
    return row


def summarize(rows: list[dict], strategies: list[str]) -> dict:
    """戦略ごとの正答率・無回答率・連続指標をまとめる（帯別も出す）。"""
    out: dict[str, dict] = {}
    for name in strategies:
        picked = [r for r in rows if r["strategy"] == name]
        if not picked:
            continue
        tiers = sorted({r.get("tier", 0) for r in picked})
        entropy = [r["cluster_entropy"] for r in picked if r.get("cluster_entropy") is not None]
        agree = [r["agreement"] for r in picked if r.get("agreement") is not None]
        pairs = [(r["confidence"] / 100.0, bool(r["ok"])) for r in picked
                 if r.get("confidence") is not None]
        out[name] = {
            **G.verdict_counts(picked),
            "by_tier": {str(tier): G.verdict_counts([r for r in picked if r.get("tier") == tier])
                        for tier in tiers},
            "calls_mean": round(statistics.mean(r["calls"] for r in picked), 2),
            "wall_mean_s": round(statistics.mean(r["wall_s"] for r in picked), 2),
            "cluster_entropy_mean": round(statistics.mean(entropy), 4) if entropy else None,
            "agreement_mean": round(statistics.mean(agree), 3) if agree else None,
            "brier": G.brier(pairs) if len(pairs) >= 4 else None,
            "ece": G.ece(pairs) if len(pairs) >= 4 else None,
            "confidence_mean": (round(statistics.mean(c for c, _ in pairs) * 100, 1)
                                if pairs else None),
            "truncated": sum(1 for r in picked if r.get("truncated")),
            "retried": sum(1 for r in picked if r.get("retried")),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="戦略ごとの推論品質を測る（Free の枠を使う）")
    parser.add_argument("--cases", default="gen",
                        help="gen（自前生成・既定）/ builtin（従来 10 問）/ both / 数字（両方から N 件）")
    parser.add_argument("--strategies", default="single,panel,moa,refine,cascade",
                        help="カンマ区切り（single / panel / moa / refine / cascade）")
    parser.add_argument("--pool", type=int, default=6, help="参加モデル数の上限（既定 6＝nous の枠）")
    parser.add_argument("--per-tier", type=int, default=4, help="生成ケースの帯ごとの件数（既定 4）")
    parser.add_argument("--seed", type=int, default=20261024, help="生成の乱数種（再現用）")
    parser.add_argument("--max-tokens", type=int, default=400)
    parser.add_argument("--repeat", type=int, default=1, help="各ケースの繰り返し回数")
    parser.add_argument("--retry", dest="retry", action="store_true", default=True,
                        help="抽出できないとき 1 回だけ再質問する（既定で有効）")
    parser.add_argument("--no-retry", dest="retry", action="store_false")
    parser.add_argument("--calibrate", action="store_true",
                        help="確信度も答えさせて Brier/ECE（校正誤差）を出す")
    parser.add_argument("--json", action="store_true", help="JSON だけを出す（機械処理用）")
    parser.add_argument("--no-write", action="store_true", help="quality.jsonl に書かない")
    args = parser.parse_args(argv)

    refs = S.free_model_refs(free_only=True, chat_only=True)[: max(1, args.pool)]
    if not refs:
        print("推論可能な Free モデルが 0 件です（`hermes proxy start` か各社のキーを確認）")
        return 2
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    if args.cases.isdigit():
        source, limit = "both", max(1, int(args.cases))
    else:
        source, limit = args.cases, None
    cases = load_cases(source, args.per_tier, args.seed, limit)

    rows: list[dict] = []
    for case in cases:
        question = case["question"] + (CALIBRATION_SUFFIX if args.calibrate else "")
        scorable = dict(case, question=question)
        for repeat in range(max(1, args.repeat)):
            for name in strategies:
                row = run_strategy(name, refs, scorable, args.max_tokens)
                grade_row(row, scorable, refs[0] if args.retry else None, args.max_tokens)
                row = {k: v for k, v in row.items() if k != "attempts"}
                if args.calibrate:
                    row["confidence"] = parse_confidence(row["text"])
                row.update({"case": case["id"], "tier": case.get("tier", 0),
                            "repeat": repeat + 1, "pool": len(refs),
                            "server_version": S.SERVER_VERSION})
                rows.append(row)
                if not args.json:
                    mark = {"correct": "✓", "wrong": "✗", "no_answer": "∅"}[row["verdict"]]
                    extra = " +再質問" if row["retried"] else ""
                    print(f"{mark} {case['id']:34s} {name:9s} {row['wall_s']:6.2f}s "
                          f"calls={row['calls']:2d}{extra:9s} {row['detail'][:28]:28s} "
                          f"ans={str(row['answer'])[:16]}", flush=True)

    summary = summarize(rows, strategies)
    paired = G.paired_summary(rows, baseline=strategies[0])

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
               "source": source, "seed": args.seed, "repeat": args.repeat,
               "calibrate": args.calibrate, "strategies": summary, "paired": paired}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    tiers = sorted({r.get("tier", 0) for r in rows})
    print("\n=== 結果 ===")
    print(f"{'戦略':9s} {'正答率':>7s} {'無回答':>6s} {'誤答':>4s} " +
          " ".join(f"{'t' + str(t):>6s}" for t in tiers) +
          f" {'呼出':>5s} {'秒':>6s} {'熵':>6s} {'切断':>4s} {'再質':>4s}")
    for name, row in summary.items():
        cells = " ".join(f"{row['by_tier'].get(str(t), {}).get('accuracy', 0):>6.2f}"
                         for t in tiers)
        entropy = row["cluster_entropy_mean"]
        print(f"{name:9s} {row['accuracy']:>7.3f} {row['no_answer_rate']:>6.3f} "
              f"{row['wrong']:>4d} {cells} {row['calls_mean']:>5.2f} {row['wall_mean_s']:>6.2f} "
              f"{(f'{entropy:.2f}' if entropy is not None else '-'):>6s} "
              f"{row['truncated']:>4d} {row['retried']:>4d}")
    if args.calibrate:
        print("\n=== 校正（自己申告の確信度 vs 正誤）===")
        print(f"{'戦略':9s} {'Brier':>7s} {'ECE':>7s} {'申告平均':>8s}")
        for name, row in summary.items():
            if row["brier"] is None:
                continue
            print(f"{name:9s} {row['brier']:>7.3f} {row['ece']:>7.3f} "
                  f"{row['confidence_mean']:>8.1f}")

    print(f"\n=== 基準（{strategies[0]}）との対応比較 ===")
    print(f"{'戦略':9s} {'差':>7s} {'95%CI':>19s} {'McNemar p':>10s} {'無回答差':>8s}  判定")
    for name, row in paired.items():
        if row.get("baseline") or not row.get("pairs"):
            continue
        ci = row.get("accuracy_ci95") or (None, None)
        na = row["no_answer_diff"]
        print(f"{name:9s} {row['accuracy_diff']:>7.3f} "
              f"{(str(ci[0]) + ' 〜 ' + str(ci[1])):>19s} {row['mcnemar_p']:>10.3f} "
              f"{(f'{na:.3f}' if na is not None else '-'):>8s}  {row['verdict']}")

    print("\n※ 二値の 10 ポイント差を 80% 検出力で見るには 300〜400 ケース必要"
          "（連続指標なら 30〜50）。")
    print("※ 帯は事前登録。結果を見てから測る帯を選ばないこと（選択バイアス）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())