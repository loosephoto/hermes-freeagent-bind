"""自前生成の**検証可能な難問**ケース（P6c）。

外部データセット（MATH / DeepMath 等）は同梱しない（再配布条件の確認が要る・規約 60 と同じ考え方）。
代わりに 6 系統を手続き生成し、**正解を生成時に厳密計算**する。利点:

* 採点の期待値が自明（人手でラベルを書かない＝写し間違いが無い）
* ケース数が無制限（統計に必要な数を確保できる。実測: 二値 10pt 差には 300〜400 件必要）
* 難易度を**帯として事前登録**できる（tier1/2/3 を固定比率で出す）

**帯は事前登録する**。単体正答率を見て測る帯を選ぶのは「結果を見て評価セットを選ぶ」型の
バイアス（協議で指摘）なので、既定は各帯同数。`--per-tier` で件数を変えてよいが、結果を見てから
比率を変えないこと。

`solution_steps` は「どう作ったか」の記録で、採点には使わない（人手の期待値を持たない方針）。

使い方:
    python scripts/gen_cases.py                    # 12 件（各帯 4 件）
    python scripts/gen_cases.py --per-tier 20 --json > cases.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from fractions import Fraction

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

TIERS = (1, 2, 3)


# --------------------------------------------------------------------------- tier 1
def percent_batch(rng: random.Random) -> dict:
    """割合＋個数の 2 段（罠: 小数の端数、単価ではなく個数）。"""
    pct = rng.choice([5, 10, 15, 20, 25])
    per = rng.choice([20, 24, 25, 30, 40, 50, 60])
    boxes = rng.randint(12, 40)
    need = 100 // math.gcd(pct, 100)          # 端数が出ない倍数まで増やす
    while (boxes * per) % need != 0:
        boxes += 1
    total = boxes * per
    bad = Fraction(total) * Fraction(pct, 100)
    good = Fraction(total) - bad
    return {
        "id": f"t1_percent_{boxes}x{per}_{pct}pct",
        "params": {"boxes": boxes, "per": per, "pct": pct},
        "tier": 1, "family": "percent_batch", "kind": "numeric",
        "traps": ["percent", "two_step"],
        "question": (f"A warehouse holds {boxes} boxes with {per} items each. "
                     f"{pct}% of all items are defective. How many items are good? "
                     f"End with a line 'Final answer: <number>'."),
        "expect": [str(int(good))],
        "solution_steps": [f"total={boxes}*{per}={total}",
                           f"defective={total}*{pct}/100={bad}", f"good={int(good)}"],
    }


def speed_time(rng: random.Random) -> dict:
    """速度×時間の合算＋単位変換（罠: 分と時間の混在、km→m）。"""
    v1 = rng.choice([45, 60, 72, 90])
    m1 = rng.choice([20, 25, 40, 45])
    v2 = rng.choice([54, 84, 96, 108])
    m2 = rng.choice([50, 75, 90])
    km = Fraction(v1 * m1, 60) + Fraction(v2 * m2, 60)
    metres = km * 1000
    assert metres.denominator == 1, km       # 速度を 3 の倍数に限っているので必ず整数
    return {
        "id": f"t1_speed_{v1}_{m1}_{v2}_{m2}",
        "params": {"v1": v1, "m1": m1, "v2": v2, "m2": m2},
        "tier": 1, "family": "speed_time", "kind": "numeric",
        "traps": ["mixed_time", "unit_conversion"],
        "question": (f"A vehicle drives {v1} km/h for {m1} minutes, then {v2} km/h for {m2} "
                     f"minutes. What is the total distance in metres? "
                     f"End with a line 'Final answer: <number>'."),
        "expect": [str(int(metres))],
        "solution_steps": [f"{v1}*{m1}/60={Fraction(v1 * m1, 60)}km",
                           f"{v2}*{m2}/60={Fraction(v2 * m2, 60)}km",
                           f"sum*1000={int(metres)}m"],
    }


# --------------------------------------------------------------------------- tier 2
def crt(rng: random.Random) -> dict:
    """連立合同式（罠: 探索の打ち切り、法の取り違え）。"""
    a, b = rng.choice([(7, 11), (9, 13), (8, 15), (11, 17)])
    r1, r2 = rng.randint(1, a - 1), rng.randint(1, b - 1)
    lo = rng.choice([1000, 2000, 5000])
    n = lo
    while not (n % a == r1 and n % b == r2):
        n += 1
    return {
        "id": f"t2_crt_{a}_{r1}_{b}_{r2}_from{lo}",
        "params": {"a": a, "r1": r1, "b": b, "r2": r2, "lo": lo},
        "tier": 2, "family": "crt", "kind": "numeric",
        "traps": ["congruence", "search"],
        "question": (f"Find the smallest integer n with n >= {lo} such that n ≡ {r1} (mod {a}) "
                     f"and n ≡ {r2} (mod {b}). End with a line 'Final answer: <number>'."),
        "expect": [str(n)],
        "solution_steps": [f"brute force from {lo} until both congruences hold → {n}"],
    }


def inclusion_exclusion(rng: random.Random) -> dict:
    """包除原理＋否定条件（罠: 二重に引く、'but not' の読み落とし）。"""
    lim = rng.choice([500, 1000, 2000])
    p, q = rng.choice([(3, 5), (4, 6), (6, 10), (3, 7)])
    lcm = p * q // math.gcd(p, q)
    union = lim // p + lim // q - lim // lcm
    answer = union - lim // lcm
    return {
        "id": f"t2_ie_{lim}_{p}or{q}_not{lcm}",
        "params": {"lim": lim, "p": p, "q": q, "lcm": lcm},
        "tier": 2, "family": "inclusion_exclusion", "kind": "numeric",
        "traps": ["double_count", "negation"],
        "question": (f"How many integers from 1 to {lim} inclusive are divisible by {p} or {q}, "
                     f"but not by {lcm}? End with a line 'Final answer: <number>'."),
        "expect": [str(answer)],
        "solution_steps": [f"|A|={lim // p}", f"|B|={lim // q}", f"|A∩B|={lim // lcm}",
                           f"|A∪B|={union}", f"minus |{lcm}| → {answer}"],
    }


# --------------------------------------------------------------------------- tier 3
def integer_interval(rng: random.Random) -> dict:
    """不等式＋整数域＋区間記法（罠: 開閉区間の取り違え、無限の整数解）。"""
    a = rng.randint(2, 9)
    b = rng.randint(3, 20)
    lo = rng.choice([-6, -4, -3, -2, 0, 1, 2, 3])
    hi = lo + rng.randint(2, 6)              # c = a*hi + b とすれば x < hi ちょうど
    c = a * hi + b
    return {
        "id": f"t3_interval_a{a}_b{b}_c{c}_from{lo}",
        "params": {"a": a, "b": b, "c": c, "lo": lo},
        "tier": 3, "family": "integer_interval", "kind": "set",
        "traps": ["inequality", "integer_domain", "interval_notation"],
        "question": (f"Let S be the set of integers x with {a}x + {b} < {c} and x >= {lo}. "
                     f"Give S in interval notation (for example [2,7)). "
                     f"End with a line 'Final answer: <interval>'."),
        "expect": [f"[{lo},{hi})"],
        "solution_steps": [f"x < {hi}", f"x >= {lo}", f"S = {{{lo}..{hi - 1}}}"],
    }


def latex_fraction(rng: random.Random) -> dict:
    """LaTeX 分数＋約分（罠: 小数で答える、約分し忘れ）。"""
    den = rng.choice([3, 5, 7, 9])
    num = rng.choice([n for n in range(2, 10) if math.gcd(n, den) == 1] or [2])
    k = rng.choice([x for x in range(2, 25) if x % den != 0])
    val = Fraction(num, den) * k
    assert val.denominator != 1, val
    return {
        "id": f"t3_frac_{num}over{den}_x{k}",
        "params": {"num": num, "den": den, "k": k},
        "tier": 3, "family": "latex_fraction", "kind": "exact",
        "traps": ["latex_fraction", "simplification"],
        "question": (f"Compute $\\frac{{{num}}}{{{den}}} \\cdot {k}$ and give the answer as a "
                     f"fraction in lowest terms (LaTeX allowed). "
                     f"End with a line 'Final answer: <value>'."),
        "expect": [f"{val.numerator}/{val.denominator}",
                   f"\\frac{{{val.numerator}}}{{{val.denominator}}}"],
        "solution_steps": [f"{num}*{k}/{den}", f"reduce → {val.numerator}/{val.denominator}"],
    }


FAMILIES = {
    1: (percent_batch, speed_time),
    2: (crt, inclusion_exclusion),
    3: (integer_interval, latex_fraction),
}


def build(per_tier: int = 4, seed: int = 20261024) -> list[dict]:
    """帯ごとに同数のケースを作る（帯は事前登録・結果を見て増減しない）。"""
    rng = random.Random(seed)
    cases: list[dict] = []
    for tier in TIERS:
        families = FAMILIES[tier]
        for i in range(max(1, per_tier)):
            cases.append(families[i % len(families)](rng))
    seen: dict[str, int] = {}
    for case in cases:
        base = case["id"]
        seen[base] = seen.get(base, 0) + 1
        if seen[base] > 1:
            # 同じ乱数で同じ問題が出たとき（per_tier を大きくすると起きる）は連番を足す。
            # 連番は seed に対して決定的なので、対応のある比較（同じ case 同士の突き合わせ）が崩れない。
            case["id"] = f"{base}-{seen[base]}"
    if len({case["id"] for case in cases}) != len(cases):
        raise SystemExit("id を一意にできませんでした（生成器を確認）")
    return cases


# --------------------------------------------------------------------------- 独立検算
def verify(case: dict) -> tuple[bool, str]:
    """**生成とは別の経路**で答えを再計算して突き合わせる（期待値の写し間違いを防ぐ）。

    入力は ``case["params"]``（生成に使った値）を使う。id の文字列を切り出すのではなく
    パラメータを明示的に持たせる（実測: id 解析は脆く、``9x18`` と ``9<18`` を取り違えた）。
    """
    family, p = case["family"], case.get("params", {})
    want = case["expect"][0]
    if family == "percent_batch":
        # 独立経路: 「良い品」を残りの割合から直接出す（生成側は「不良を引く」経路）。
        # 端数が出ないこと（不良数が整数）もここで確かめる＝期待値を整数で持てる前提を守る。
        total = p["boxes"] * p["per"]
        if (total * p["pct"]) % 100 != 0:
            return False, f"不良数が整数にならない（total={total}, pct={p['pct']}）"
        good = total * (100 - p["pct"]) // 100
        return str(good) == want, f"{good} vs {want}"
    if family == "speed_time":
        metres = Fraction(p["v1"] * p["m1"] + p["v2"] * p["m2"], 60) * 1000
        got = str(int(metres)) if metres.denominator == 1 else str(metres)
        return got == want, f"{got} vs {want}"
    if family == "crt":
        # 独立経路: 法の逆元を使う（生成側は線形探索）
        a, b, r1, r2, lo = p["a"], p["b"], p["r1"], p["r2"], p["lo"]
        inv = pow(a, -1, b)
        x0 = r1 + a * (((r2 - r1) * inv) % b)
        period = a * b // math.gcd(a, b)
        k = math.ceil((lo - x0) / period)
        got = str(x0 + period * k)
        return got == want, f"{got} vs {want}"
    if family == "inclusion_exclusion":
        lim, q_, lcm = p["lim"], p["q"], p["lcm"]
        got = str(sum(1 for n in range(1, lim + 1)
                      if (n % p["p"] == 0 or n % q_ == 0) and n % lcm != 0))
        return got == want, f"{got} vs {want}"
    if family == "integer_interval":
        xs = [x for x in range(p["lo"] - 20, p["lo"] + 40)
              if p["a"] * x + p["b"] < p["c"] and x >= p["lo"]]
        got = f"[{min(xs)},{max(xs) + 1})" if xs else "empty"
        return got.replace(" ", "") == want.replace(" ", ""), f"{got} vs {want}"
    if family == "latex_fraction":
        val = Fraction(p["num"] * p["k"], p["den"])
        got = f"{val.numerator}/{val.denominator}"
        return got == want, f"{got} vs {want}"
    return False, f"未知の family: {family}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="検証可能な難問ケースを自前で生成する")
    parser.add_argument("--per-tier", type=int, default=4, help="帯ごとの件数（既定 4・事前登録）")
    parser.add_argument("--seed", type=int, default=20261024)
    parser.add_argument("--json", action="store_true", help="JSON だけを出す")
    parser.add_argument("--out", help="JSON を書き出すパス")
    args = parser.parse_args(argv)

    cases = build(args.per_tier, args.seed)
    bad = []
    for case in cases:
        ok, detail = verify(case)
        if not ok:
            bad.append((case["id"], detail))
    if bad:
        for case_id, detail in bad:
            print(f"✗ 検算不一致 {case_id}: {detail}", file=sys.stderr)
        return 3

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(cases, fh, ensure_ascii=False, indent=1)

    if args.json:
        print(json.dumps(cases, ensure_ascii=False))
        return 0
    print(f"生成 {len(cases)} 件（帯 1/2/3 各 {args.per_tier} 件・seed {args.seed}）"
          f"／独立検算 {len(cases)}/{len(cases)} 一致")
    for case in cases:
        print(f"[tier {case['tier']}] {case['id']}")
        print(f"   Q: {case['question']}")
        print(f"   A: {case['expect'][0]}   （罠: {','.join(case['traps'])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())