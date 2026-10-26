"""doaj（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_doaj(query: str, limit: int = 5) -> dict:
    """DOAJ のオープンアクセス記事メタデータ（CC0）。抄録を本文根拠に使う。全文は取得しない。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "doaj", "error": "query は必須です"}
    def produce():
        path = urllib.parse.quote(S._literal_search(query), safe="")
        data, err = S._kb_new_json("https://doaj.org/api/search/articles/" + path
                                + "?" + urllib.parse.urlencode({"pageSize": limit}), 0.51)
        if err:
            return {"source": "doaj", "error": err}
        rows = S._kb_array(S._kb_object(data, "DOAJ応答").get("results"), "results")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("DOAJ record はオブジェクトではありません")
            bib = S._kb_object(row.get("bibjson"), "bibjson")
            title = S._plain_text(S.as_str(bib.get("title")), 300)
            if not title:
                continue
            doi = next((S.as_str(i.get("id")) for i in S._kb_array(bib.get("identifier"), "identifier")
                        if isinstance(i, dict) and S.as_str(i.get("type")).lower() == "doi"), "")
            link = next((S.as_str(l.get("url")) for l in S._kb_array(bib.get("link"), "link")
                         if isinstance(l, dict) and S.as_str(l.get("url"))), "")
            if doi:
                link = "https://doi.org/" + doi
            if not link:
                continue
            journal = S._kb_object(bib.get("journal"), "journal")
            authors = [S.as_str(a.get("name")) for a in S._kb_array(bib.get("author"), "author")
                       if isinstance(a, dict)]
            summary = S._plain_text(S.as_str(bib.get("abstract")), 600)
            items.append({"title": title, "url": link, "doi": doi, "year": S.as_str(bib.get("year")),
                          "summary": summary, "authors": [a for a in authors if a][:8],
                          "journal": S._plain_text(S.as_str(journal.get("title")), 200),
                          "metadata_only": not bool(summary)})
        return S._kb_paper_result("doaj", items,
                               attribution="データ提供: DOAJ（記事メタデータCC0） https://doaj.org/")
    return S._kb_new_cached(f"doaj:{query}:{limit}", "doaj", produce)



EXPORTS = {"kb_doaj": kb_doaj}


def backend(q, limit, opts):
    return S.kb_doaj(q, limit)


register("doaj", kb_doaj, backend)
