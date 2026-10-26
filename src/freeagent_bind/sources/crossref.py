"""crossref（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

# Crossref のローカル予算（秒）。**実測に基づく**（2026-10-20）:
# 429 は**レート制限ではなく共有プールのTransientな拒否**で、間隔（0.4/0.6/0.8/1.0 秒）不管で
# 10 回中 1〜3 回出る。→ 間隔では直らない。**再試行で直る**（§5.8 の 429 再試行）。
# よって予算は「詰まらせない」程度に緩くする（0.5 秒だと 429 の原因を作ってしまう）。
_CROSSREF_INTERVAL = max(0.0, S._env_float("FREEAGENT_CROSSREF_INTERVAL", 0.1))


def kb_crossref(query: str, limit: int = 5, need_evidence: bool = False) -> dict:
    """DOI 登録機関のメタデータ（書誌）。`mailto` を付けると polite pool に入る。

    **`need_evidence=True`（= `grounded` の場面）なら `filter=has-abstract:true` を付ける**（§6.18）。
    実測: Crossref の abstract の充填率は**平均 27%**（9 クエリ × 5 件 = 45 件中 12 件）。
    → そのままでは「引用は返るのに本文根拠 0 件」になる（= grounded の根拠が尽きる）。
    `has-abstract:true` を付けると **9 クエリすべてで 5/5（100%）** になる。
    代償は関連度が少し落ちる（上位に別の論文が混ざる）が、
    **根拠が要る場面では「根拠が無い書誌」より「少し関連度の落ちた根拠」の方が有用**。
    `need_evidence=False`（= `lookup` の場面）は**書誌の網羅性**を優先するのでフィルタしない。
    """
    limit = S.as_int(limit, 5, 1, 20)

    def produce() -> dict:
        params = {"query": query, "rows": limit,
                  "select": "DOI,title,author,issued,container-title,type,URL,abstract,is-referenced-by-count,publisher"}
        if S.KB_MAILTO:
            params["mailto"] = S.KB_MAILTO
        if need_evidence:
            params["filter"] = "has-abstract:true"
        data, err = S._kb_new_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params),
                                 _CROSSREF_INTERVAL)
        if err:
            return {"source": "crossref", "error": err}
        rows = ((data or {}).get("message") or {}).get("items") or []
        items, cites = [], []
        for row in rows[:limit]:
            title = (row.get("title") or [""])[0]
            year = ""
            parts = ((row.get("issued") or {}).get("date-parts") or [[]])[0]
            if parts:
                year = str(parts[0])
            authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                       for a in (row.get("author") or [])]
            url = row.get("URL") or (f"https://doi.org/{row.get('DOI')}" if row.get("DOI") else "")
            items.append({"title": S.truncate(title, 300), "doi": row.get("DOI") or "",
                          "year": year, "type": row.get("type") or "",
                          "container": (row.get("container-title") or [""])[0],
                          "publisher": row.get("publisher") or "",
                          "cited_by": row.get("is-referenced-by-count"),
                          "authors": authors[:8], "url": url})
            cites.append(S._cite("crossref", title, url, year=year, doi=row.get("DOI") or "",
                               summary=S._plain_text(row.get("abstract") or "", 500)))
        return {"source": "crossref", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    # キャッシュキーに**根拠の有無を入れる**（同じクエリでも返すべき引用が違う。§6.18）。
    return S._kb_cached(f"crossref:{query}:{limit}:{'evi' if need_evidence else 'bib'}", produce)


EXPORTS = {"_CROSSREF_INTERVAL": _CROSSREF_INTERVAL, "kb_crossref": kb_crossref}


def backend(q, limit, opts):
    return S.kb_crossref(q, limit=limit,
                                                 need_evidence=opts.get("need_evidence") is True)


register("crossref", kb_crossref, backend)
