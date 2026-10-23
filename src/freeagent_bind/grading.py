"""採点と評価指標（**純関数だけ**・依存ゼロ）。

`scripts/measure_quality.py` が使う。サーバー本体（`server.py`）からも import できるよう、
ここには I/O もネットワークも置かない（規約: 標準ライブラリのみ・ツールを増やさない）。

この module が解く問題（v0.14.0 で実測して特定した 2 つの誤り）:

1. **言及と回答を区別しない**: 旧採点は本文の全数値を走査していたため
   ``"The cube of 17 is 4913? No - I compute 2900. Final answer: 2900"`` を**正解**にしていた。
   さらに部分一致だったので ``"This is not silver, it is mercury."`` も正解にしていた。
   → **最終回答を先に抽出**し、抽出した値だけを厳密比較する（`extract_final_answer`）。
2. **切り詰めを誤答に変換する**: 実測で truncated 3/24、うち 2 件は reasoning が正しく最終行だけが無い。
   → 抽出できないことを ``no_answer`` として**区別して記録**し、必要なら 1 回だけ再質問する
   （再質問は API 呼び出しなので呼び出し側＝`measure_quality.py` が行う。ここでは `needs_retry` を返す）。
"""
from __future__ import annotations

import math
import re
import random
from fractions import Fraction

# 最終回答を示すラベル（英語・日本語）。**最後に出てきたもの**を採る（モデルは途中で言い直すため）。
_FINAL_LABELS = (
    "final answer", "final_result", "answer is", "answer:", "the answer",
    "最終回答", "最終的な答え", "答は", "答えは", "答え:",
)
# 単位として落とす語（末尾にだけ現れるもの）。% は先に数値へ変換するのでここには無い。
_UNITS = ("items", "item", "units", "unit", "pieces", "piece", "metres", "meters", "meter",
          "metre", "km/h", "mph", "km", "cm", "mm", "m", "kg", "g", "mg", "hours", "hour",
          "hrs", "hr", "h", "minutes", "minute", "mins", "min", "seconds", "second", "secs",
          "sec", "s", "degrees", "degree", "people", "persons", "yen", "usd")

_INTERVAL_RE = re.compile(
    r"^([\[\]\(\)])\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\s*,\s*(-?\d+(?:\.\d+)?(?:/\d+)?)\s*([\[\]\(\)])$")
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:/\d+)?$")
_EMPTY_WORDS = ("empty", "emptyset", "∅", "{}", "no solution", "なし", "空集合", "解なし")
_NEGATION_RE = re.compile(r"(?:not|no|never|ではなく|ではない|じゃない|違い)\s*$", re.I)


def _fold_unicode(text: str) -> str:
    """全角・異体字・各種ダッシュを ASCII へ寄せる。"""
    out = []
    for ch in text:
        code = ord(ch)
        if 0xFF10 <= code <= 0xFF19:          # 全角数字（０-９）
            out.append(chr(code - 0xFF10 + ord("0")))
        elif ch in "−–—‐‑‒":
            out.append("-")
        elif ch in "，、":
            out.append(",")
        elif ch in "．":
            out.append(".")
        elif ch in "／":
            out.append("/")
        elif ch in "（）":
            out.append("()" ["（）".index(ch)])
        elif ch == "\u00a0":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _strip_units(text: str) -> str:
    """末尾の単位語を落とす（``4913.0 units`` → ``4913.0``）。"""
    out = text
    for _ in range(3):
        changed = False
        for unit in sorted(_UNITS, key=len, reverse=True):
            pattern = re.compile(r"\s*" + re.escape(unit) + r"\s*$", re.I)
            if pattern.search(out):
                stripped = pattern.sub("", out).strip()
                if stripped and _NUM_RE.match(stripped.split(",")[-1].strip()):
                    out = stripped
                    changed = True
        if not changed:
            break
    return out


def _latex_to_plain(text: str) -> str:
    """LaTeX の表記を数値・集合の比較ができる平文へ寄せる（Math-Verify の転用部分）。"""
    out = text
    out = re.sub(r"\\[\(\[]|\\[\)\]]|\$\$?", "", out)          # $ \( \[ など
    out = re.sub(r"\\(?:left|right|quad|qquad|[;!,])", "", out)
    out = re.sub(r"\\(?:text|mathrm|mathbf|operatorname)\{([^{}]*)\}", r"\1", out)
    # \frac13 → 1/3、\frac{2}{7} → 2/7、\dfrac / \tfrac も同じ扱い
    for _ in range(3):
        out = re.sub(r"\\(?:d|t|c)?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}", r"\1/\2", out)
        out = re.sub(r"\\(?:d|t|c)?frac\s*(\d)\s*(\d)", r"\1/\2", out)
    out = out.replace("\\boxed", "").replace("\\cup", ",").replace("\\cdot", "*")
    out = out.replace("\\times", "*").replace("\\%", "%").replace("\\pmod", " mod ")
    out = out.replace("\\pi", "pi").replace("\\infty", "inf")
    out = re.sub(r"\\sqrt\s*\{([^{}]+)\}", r"sqrt(\1)", out)
    out = out.replace("\\", "")
    out = re.sub(r"(\d)\s*%\s*", r"\1/100 ", out)              # 10% → 10/100
    out = re.sub(r"(\d+(?:\.\d+)?)\s*%", lambda m: str(float(m.group(1)) / 100), out)
    return out


def _drop_thousands(text: str) -> str:
    """``1,234`` のような桁区切りのカンマだけ落とす（集合の区切りは残す）。

    「全体が 1 つの数値」のときだけ落とす。``{100,200,300}`` を ``100200,300`` に
    壊さないため（実測で踏んだ罠）。
    """
    stripped = text.strip()
    if re.match(r"^-?\d{1,3}(?:,\d{3})+(?:\.\d+)?$", stripped):
        return stripped.replace(",", "")
    return text


def normalize_value(text: str) -> str:
    """比較用の正準形にする（表層ゆれ・LaTeX・単位・桁区切りを落とす）。"""
    out = _fold_unicode(text or "")
    out = _latex_to_plain(out)
    out = out.replace("\u00d7", "*").replace("\u00f7", "/")
    out = out.lower()
    out = _strip_units(out)          # 単位を先に落とす（"1,234 items" のカンマ判定のため）
    out = _drop_thousands(out)
    out = re.sub(r"[\s]+", "", out)
    out = out.strip(" .。!！?？:：;；\"'`*")
    out = out.strip("()") if re.match(r"^\(-?\d+(?:\.\d+)?(?:/\d+)?\)$", out) else out
    return out


def _to_number(token: str):
    """``a/b`` は Fraction、それ以外は float で返す（厳密比較のため分ける）。"""
    token = token.strip().strip("()")
    if "/" in token:
        num, _, den = token.partition("/")
        try:
            return Fraction(int(num.strip()), int(den.strip()))
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(token)
    except ValueError:
        return None


def _as_fraction(value) -> Fraction:
    """数値を厳密な Fraction にする（float は 10 進表記を経由して誤差を持ち込まない）。"""
    if isinstance(value, int):
        return Fraction(value)
    return Fraction(str(value))


def parse_value(text: str) -> dict:
    """正規化した文字列を ``{kind, value}`` に分解する。

    kind: ``number`` / ``interval`` / ``set`` / ``empty`` / ``text``
    """
    norm = normalize_value(text)
    if not norm or any(word in norm for word in _EMPTY_WORDS) or norm in ("-", "n/a", "none"):
        return {"kind": "empty", "value": frozenset()}
    match = _INTERVAL_RE.match(norm)
    if match:
        lo, hi = _to_number(match.group(2)), _to_number(match.group(3))
        if lo is not None and hi is not None:
            return {"kind": "interval", "value": (lo, hi, match.group(1) == "[",
                                                 match.group(4) == "]")}
    number = _to_number(norm)
    if number is not None:
        return {"kind": "number", "value": number}
    body = norm.strip("{}")
    parts = [p for p in body.split(",") if p != ""]
    numbers = [_to_number(p) for p in parts]
    if parts and all(n is not None for n in numbers):
        return {"kind": "set", "value": frozenset(n for n in numbers if n is not None)}
    return {"kind": "text", "value": norm}


def _interval_ints(value: tuple) -> frozenset | None:
    """整数域の区間を集合へ（有限で小さいときだけ）。"""
    lo, hi, lo_closed, hi_closed = value
    if not (float(lo).is_integer() and float(hi).is_integer()):
        return None
    start = int(lo) if lo_closed else int(lo) + 1
    end = int(hi) if hi_closed else int(hi) - 1
    if end < start or (end - start) > 10000:
        return frozenset() if end < start else None
    return frozenset(Fraction(x) for x in range(start, end + 1))


def values_equal(got: str, want: str, kind: str = "", tol: float = 1e-6) -> bool:
    """抽出した回答と期待値を**厳密に**比べる（言及や否定文は通さない）。

    * kind=``set`` / ``interval``: 整数域なら**集合として**比較（``[-6,-3)`` ≡ ``{-6,-5,-4}``）
    * 数値: ``max(0.5, |want| * tol)`` の許容誤差（旧実装と同じ閾値）
    * 文字列: 正規化後の完全一致（部分一致はしない）
    """
    left, right = parse_value(got), parse_value(want)
    if right["kind"] == "empty" or left["kind"] == "empty":
        return left["kind"] == right["kind"]
    if right["kind"] in ("interval", "set"):
        lset = left["value"] if left["kind"] == "set" else (
            _interval_ints(left["value"]) if left["kind"] == "interval" else None)
        rset = right["value"] if right["kind"] == "set" else (
            _interval_ints(right["value"]) if right["kind"] == "interval" else None)
        if lset is not None and rset is not None:
            return lset == rset
        if left["kind"] == right["kind"] == "interval":
            # 集合に落とせない（無限・非整数）場合は境界と開閉で比較
            return (float(left["value"][0]) == float(right["value"][0])
                    and float(left["value"][1]) == float(right["value"][1])
                    and left["value"][2:] == right["value"][2:])
        return False
    if left["kind"] == "number" and right["kind"] == "number":
        a, b = left["value"], right["value"]
        # kind="exact"（分数で答える問題）は**厳密一致**。1/3 と 0.333 を通してはいけない。
        if kind == "exact":
            return _as_fraction(a) == _as_fraction(b)
        if isinstance(a, Fraction) and isinstance(b, Fraction):
            return a == b
        diff = abs(float(a) - float(b))
        return diff <= max(0.5, abs(float(b)) * tol)
    return left["kind"] == "text" and right["kind"] == "text" and left["value"] == right["value"]


def _clean_tail(tail: str) -> str:
    """ラベル直後の文字列から LaTeX の残骸と区切り記号を落とす。

    実測で踏んだ 2 つの形に対応する:
    * ``\\text{Final answer: [-4, 0)}`` → 閉じ括弧が 1 つ余る（``[-4, 0)}``）
    * ``\\text{Final answer: } \\]`` → ラベルだけで中身が無い（``} \\]`` が残る）
    """
    out = tail.strip()
    out = re.sub(r"^[\s:：=＝\-—–]+", "", out)
    while out.endswith("}") and out.count("}") > out.count("{"):
        out = out[:-1].rstrip()
    for suffix in ("\\]", "\\)", "$$", "$"):
        if out.endswith(suffix):
            out = out[: -len(suffix)].rstrip()
    return out.strip(" \t\\")


def _is_junk(tail: str) -> bool:
    """英数字も日本語も含まない＝回答として意味が無い（括弧・記号だけ）。"""
    return not re.search(r"[0-9A-Za-z\u3040-\u30ff\u4e00-\u9fff]", tail)


def extract_final_answer(text: str, truncated: bool = False) -> tuple[str | None, str]:
    """最終回答だけを取り出す。``(回答, 取り出し方)``。取れなければ ``(None, reason)``。

    優先順: ① ``Final answer:`` 系のラベルの**最後**の出現 → ② ``\\boxed{}`` →
    ③ 短い本文なら最終行の値。本文全体を走査しない（言及を拾わないため）。

    ただし **長い本文や切断された応答からは推測しない**（実測: 途中で切れた推論の末尾の数値を
    拾って「7」「11」のような誤答にしていた。切断は ``no_answer`` として再質問に回すのが正しい）。
    """
    body = (text or "").strip()
    if not body:
        return None, "empty"

    lowered = body.lower()
    best, best_label = -1, ""
    for label in _FINAL_LABELS:
        idx = lowered.rfind(label)
        if idx > best:
            best, best_label = idx, label
    if best >= 0:
        # ラベル本体は切り落とす（``The answer is 630`` → ``630``）。区切りの記号も落とす。
        tail = _clean_tail(body[best + len(best_label):].split("\n", 1)[0])
        # 中身が無いラベル（実測: ``\text{Final answer: } \]``）は**次の手にフォールバック**する。
        if tail and not _is_junk(tail):
            return tail, "final_line"

    boxed = list(re.finditer(r"\\boxed\s*\{([^{}]*)\}", body))
    if boxed:
        return boxed[-1].group(1).strip(), "boxed"

    # ここから先は「本文が短い」ときだけ許す推測。長文・切断からは値を拾わない。
    if truncated:
        return None, "truncated_no_answer"
    if len(body) > 200:
        return None, "no_answer_prose"

    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if lines:
        last = lines[-1]
        if len(last) <= 80:
            nums = list(re.finditer(r"-?\d+(?:\.\d+)?(?:/\d+)?", _fold_unicode(last)))
            if nums:
                picked = None
                for match in reversed(nums):          # 最後の数値を優先
                    prefix = last[max(0, match.start() - 12):match.start()]
                    if _NEGATION_RE.search(prefix) and len(nums) > 1:
                        continue                       # 「not 4913」の 4913 は採らない
                    picked = match.group(0)
                    break
                if picked:
                    return picked, "last_line"
            # 値ひとつだけの行（"silver" / "銀" など）。**文をそのまま答えにしない**
            # （実測: "no value here at all" を回答として拾ってしまった）。
            token = last.strip(" .。!！?？\"'`*")
            if token and len(token) <= 40 and " " not in token and "\t" not in token:
                return token, "last_line"
    return None, "no_answer"


def needs_retry(text: str, truncated: bool = False) -> bool:
    """最終回答を取り出せない＝1 回だけ再質問する価値がある（切り詰め・CoT 混入の救済）。"""
    answer, _ = extract_final_answer(text, truncated=truncated)
    return answer is None


def grade(text: str, case: dict, tol: float = 1e-6, truncated: bool = False) -> dict:
    """1 つの回答を採点する。``ok`` と、**なぜそう判定したか**を返す。

    規約 8（失敗を黙って落とさない）に従い、``no_answer``（答えが書かれていない）と
    ``wrong``（書いてあるが違う）を区別する。この区別が無いと切り詰めを誤答に変換してしまう。
    ``truncated`` が真なら、本文に最終回答のラベルが無い限り**推測しない**。
    """
    answer, how = extract_final_answer(text, truncated=truncated)
    if answer is None:
        return {"ok": False, "answer": None, "how": how, "verdict": "no_answer"}
    for want in case.get("expect", []):
        if values_equal(answer, want, case.get("kind", ""), tol):
            return {"ok": True, "answer": answer, "how": how, "verdict": "correct",
                    "matched": want}
    return {"ok": False, "answer": answer, "how": how, "verdict": "wrong"}


def verdict_counts(grades: list[dict]) -> dict:
    """正解 / 誤答 / 無回答 を数える（正答率だけでは切り詰めが見えないため）。"""
    out: dict = {"correct": 0, "wrong": 0, "no_answer": 0, "runs": len(grades)}
    for row in grades:
        key = row.get("verdict", "wrong")
        out[key] = out.get(key, 0) + 1
    out["accuracy"] = round(out["correct"] / out["runs"], 3) if out["runs"] else None
    out["no_answer_rate"] = round(out["no_answer"] / out["runs"], 3) if out["runs"] else None
    return out


# --------------------------------------------------------------------------- 連続指標
def cluster_distribution(texts: list[str]) -> dict[str, int]:
    """正規化した回答の分布（semantic entropy のクラスタを文字列で近似する）。"""
    counts: dict[str, int] = {}
    for text in texts:
        answer, _ = extract_final_answer(text)
        key = normalize_value(answer) if answer else ""
        if key:
            counts[key] = counts.get(key, 0) + 1
    return counts


def cluster_entropy(texts: list[str]) -> float | None:
    """回答クラスタの Shannon 熵（bit）。1 サンプルでは測れないので None。"""
    counts = cluster_distribution(texts)
    total = sum(counts.values())
    if total < 2:
        return None
    return round(-sum((c / total) * math.log2(c / total) for c in counts.values()), 4)


def agreement_rate(texts: list[str]) -> float | None:
    """最大クラスタの割合（0〜1）。panel の合意度と同じ意味だが、抽出後の値で数える。"""
    counts = cluster_distribution(texts)
    total = sum(counts.values())
    if total < 2:
        return None
    return round(max(counts.values()) / total, 3)


def brier(pairs: list[tuple[float, bool]]) -> float | None:
    """自信（0〜1）と正誤の二乗誤差の平均。小さいほど良い。"""
    if not pairs:
        return None
    return round(sum((conf - (1.0 if ok else 0.0)) ** 2 for conf, ok in pairs) / len(pairs), 4)


def ece(pairs: list[tuple[float, bool]], bins: int = 5) -> float | None:
    """Expected Calibration Error（ビンごとの |正答率 - 平均自信| を重み付き平均）。"""
    if not pairs:
        return None
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for conf, ok in pairs:
        idx = min(bins - 1, max(0, int(conf * bins)))
        buckets[idx].append((conf, ok))
    total = len(pairs)
    err = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        avg_conf = sum(c for c, _ in bucket) / len(bucket)
        acc = sum(1 for _, ok in bucket if ok) / len(bucket)
        err += (len(bucket) / total) * abs(acc - avg_conf)
    return round(err, 4)


def mcnemar_exact_p(b: int, c: int) -> float:
    """対応のある二値比較（不一致対の二項検定・両側）。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return round(min(1.0, 2 * tail), 6)


def bootstrap_ci(values: list[float], level: float = 0.95, resamples: int = 2000,
                 seed: int = 20261024) -> tuple[float, float] | None:
    """平均の percentile bootstrap 信頼区間（seed 固定＝再現可能）。"""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(resamples):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((1 - level) / 2 * resamples)]
    hi = means[min(resamples - 1, int((1 + level) / 2 * resamples))]
    return round(lo, 4), round(hi, 4)


def paired_summary(rows: list[dict], baseline: str = "single") -> dict:
    """戦略ごとに、基準戦略との**対応のある差**（正答率・連続指標）を出す。

    点推定だけでは「改善した」と言えないので、95% 信頼区間と不一致対の検定を必ず添える
    （実測: 10 ポイント差を 80% 検出力で見るには 300〜400 ケース必要。連続指標なら 30〜50）。
    """
    index: dict[tuple, dict] = {}
    for row in rows:
        index[(row.get("case"), row.get("repeat", 1), row.get("strategy"))] = row
    pairs_by_key: dict[tuple, dict[str, dict]] = {}
    for (case, repeat, strategy), row in index.items():
        pairs_by_key.setdefault((case, repeat), {})[strategy] = row

    out: dict[str, dict] = {}
    strategies = sorted({row.get("strategy") for row in rows})
    for name in strategies:
        ok_diffs: list[float] = []
        b = c = 0
        entropy_diffs: list[float] = []
        extraction_diffs: list[float] = []
        for slot in pairs_by_key.values():
            mine, base = slot.get(name), slot.get(baseline)
            if not mine or not base or name == baseline:
                continue
            ok_diffs.append(float(bool(mine.get("ok"))) - float(bool(base.get("ok"))))
            if mine.get("ok") and not base.get("ok"):
                b += 1
            elif base.get("ok") and not mine.get("ok"):
                c += 1
            if mine.get("cluster_entropy") is not None and base.get("cluster_entropy") is not None:
                entropy_diffs.append(float(mine["cluster_entropy"])
                                     - float(base["cluster_entropy"]))
            if mine.get("no_answer") is not None and base.get("no_answer") is not None:
                extraction_diffs.append(float(bool(mine["no_answer"]))
                                        - float(bool(base["no_answer"])))
        if name == baseline or not ok_diffs:
            out[name] = {"baseline": name == baseline, "pairs": len(ok_diffs)}
            continue
        ci = bootstrap_ci(ok_diffs)
        out[name] = {
            "baseline": False,
            "pairs": len(ok_diffs),
            "accuracy_diff": round(sum(ok_diffs) / len(ok_diffs), 3),
            "accuracy_ci95": ci,
            "mcnemar_p": mcnemar_exact_p(b, c) if (b or c) else 1.0,
            "no_answer_diff": (round(sum(extraction_diffs) / len(extraction_diffs), 3)
                               if extraction_diffs else None),
            "entropy_diff": (round(sum(entropy_diffs) / len(entropy_diffs), 4)
                             if entropy_diffs else None),
            "verdict": ("有意（CI が 0 を跨がない）" if ci and (ci[0] > 0 or ci[1] < 0)
                        else "差を検出できない"),
        }
    return out