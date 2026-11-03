"""JSON の入出力を 1 か所に寄せたヘルパー（標準ライブラリのみ・server に依存しない）。

実測で見つかった 3 つの穴を塞ぐのが目的:

* `json.dumps` は既定で `NaN` / `Infinity` を**そのまま出す**。JSON として不正なので、厳格なクライアントは
  応答行ごと捨てる（接続が固まる）。→ `dumps` は `allow_nan=False` で試し、落ちたら `null` に直す。
* 上流の JSON に孤立サロゲート（`"\\ud83d"` 単独）が混ざると、`ensure_ascii=False` の文字列は
  UTF-8 に符号化できず `UnicodeEncodeError` になる。stdio では応答が黙って失われる。
  → `dumps` は U+FFFD に置き換える（速い経路は正規表現 1 回の走査だけ）。
* モデルの返答から JSON を取り出すとき、貪欲な `\\{.*\\}` は「`{...}` のあとに別の `{`」で全体が
  不正になる。→ `extract_object` は各 `{` から `raw_decode` を試し、最初の dict を返す。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

_SURROGATE = re.compile("[\ud800-\udfff]")
_DECODER = json.JSONDecoder()


def _clean(value: Any) -> Any:
    """NaN / ±Infinity → None、孤立サロゲート → U+FFFD（新しい構造を返す。入力は変えない）。"""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return _SURROGATE.sub("\ufffd", value)
    if isinstance(value, dict):
        return {(_SURROGATE.sub("\ufffd", k) if isinstance(k, str) else k): _clean(v)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def dumps(obj: Any, **kwargs: Any) -> str:
    """厳密な JSON 文字列を返す（既定 `ensure_ascii=False`・UTF-8 に必ず符号化できる）。

    速い経路は `json.dumps(..., allow_nan=False)` 1 回＋非 ASCII のときだけサロゲート走査 1 回。
    不正値（NaN・孤立サロゲート）があったときだけ全体を走査して直す。
    """
    kwargs.setdefault("ensure_ascii", False)
    try:
        text = json.dumps(obj, allow_nan=False, **kwargs)
    except ValueError:                      # NaN / Infinity（`allow_nan=False` が投げる）
        return json.dumps(_clean(obj), allow_nan=False, **kwargs)
    if not kwargs["ensure_ascii"] and not text.isascii() and _SURROGATE.search(text):
        return json.dumps(_clean(obj), allow_nan=False, **kwargs)
    return text


def deep_copy(value: Any) -> Any:
    """JSON で表せる値の複製（`json.loads(json.dumps(x))` の置き換え）。

    文字列・数値・真偽・None は不変なので共有し、dict / list だけを作り直す。往復のシリアライズより
    速く、NaN・サロゲートでも落ちない（`json` を通さないので値は変わらない）。
    """
    if isinstance(value, dict):
        return {k: deep_copy(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [deep_copy(v) for v in value]
    return value


def extract_object(text: str | None) -> dict:
    """文章に埋まった最初の JSON オブジェクト（dict）を返す。無ければ `{}`。

    コードフェンス・前置き・末尾の補足に `{` `}` が混ざっても、**各 `{` から順に** `raw_decode` を
    試す。配列や文字列だけの JSON は dict ではないので飛ばして次の `{` を探す。
    """
    source = text or ""
    pos = source.find("{")
    while pos != -1:
        try:
            obj, _end = _DECODER.raw_decode(source, pos)
        except (ValueError, RecursionError):
            obj = None
        if isinstance(obj, dict):
            return obj
        pos = source.find("{", pos + 1)
    return {}


def loads_safe(raw: str | bytes | None, default: Any = None) -> Any:
    """不正・空・深すぎる入れ子（`RecursionError`）でも例外を出さず `default` を返す。"""
    if raw is None or not raw:
        return default
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        return default
