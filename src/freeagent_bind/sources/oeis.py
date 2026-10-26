"""oeis（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_oeis(query: str, limit: int = 5) -> dict:
    """OEIS の整数列（CC BY-SA 4.0・出典表示必須）。数列そのものでも語でも引ける。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "oeis", "error": "query は必須です"}
    attribution = ("データ提供: The Online Encyclopedia of Integer Sequences（CC BY-SA 4.0） "
                   "https://oeis.org/")

    def produce() -> dict:
        data, err = S._kb_new_json("https://oeis.org/search?"
                                 + urllib.parse.urlencode({"q": query, "fmt": "json"}), 1.0)
        if err:
            return {"source": "oeis", "error": err}
        if data is None:
            rows = []
        elif isinstance(data, list):
            rows = data
        else:
            rows = S._kb_array(S._kb_object(data, "OEIS応答").get("results"), "results")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("OEIS の結果はオブジェクトではありません")
            # 実測: `id` は旧 M/N 識別子（例 "M0692 N0256"）で、A 番号は `number`（45 → A000045）。
            number = S.as_int(row.get("number"), 0, 0, 999999)
            sid = f"A{number:06d}" if number else ""
            name = S._plain_text(S.as_str(row.get("name")), 300)
            if not sid or not name:
                continue
            terms = S._plain_text(S.as_str(row.get("data")), 200)
            comments = [S.as_str(c) for c in S._kb_array(row.get("comment"), "comment") if S.as_str(c)]
            items.append({"title": f"{sid}: {name}",
                          "url": "https://oeis.org/" + urllib.parse.quote(sid, safe=""),
                          "summary": S._plain_text(" ".join([name] + comments[:2]), 600),
                          "identifier": sid, "kind": "integer-sequence",
                          "extra": {"terms": terms, "old_id": S.as_str(row.get("id")),
                                    "keywords": [k.strip() for k in
                                                 S.as_str(row.get("keyword")).split(",")
                                                 if k.strip()]}})
        return S._kb_generic_result("oeis", items, attribution)

    return S._kb_new_cached(f"oeis:{query}:{limit}", "oeis", produce)



EXPORTS = {"kb_oeis": kb_oeis}


def backend(q, limit, opts):
    return S.kb_oeis(q, limit)


register("oeis", kb_oeis, backend)
