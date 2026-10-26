"""inspirehep（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_inspirehep(query: str, limit: int = 5) -> dict:
    """INSPIRE-HEP の素粒子物理文献（CC0）。DOI があれば DOI を出典 URL にする。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "inspirehep", "error": "query は必須です"}
    attribution = "データ提供: INSPIRE-HEP（CC0） https://inspirehep.net/"

    def produce() -> dict:
        params = {"q": query, "size": limit,
                  "fields": "titles,dois,arxiv_eprints,earliest_date,authors,abstracts,citation_count"}
        data, err = S._kb_new_json("https://inspirehep.net/api/literature?"
                                 + urllib.parse.urlencode(params), 1.0)
        if err:
            return {"source": "inspirehep", "error": err}
        hits = S._kb_object(S._kb_object(data, "INSPIRE応答").get("hits"), "hits")
        items = []
        for hit in S._kb_array(hits.get("hits"), "hits"):
            if not isinstance(hit, dict):
                continue
            meta = S._kb_object(hit.get("metadata"), "metadata")
            title = next((S._plain_text(S.as_str(t.get("title")), 300)
                          for t in S._kb_array(meta.get("titles"), "titles")
                          if isinstance(t, dict) and S.as_str(t.get("title"))), "")
            if not title:
                continue
            doi = next((S.as_str(d.get("value")) for d in S._kb_array(meta.get("dois"), "dois")
                        if isinstance(d, dict) and S.as_str(d.get("value"))), "")
            record_id = S.as_str(hit.get("id"))
            url = ("https://doi.org/" + doi) if doi else (
                "https://inspirehep.net/literature/" + urllib.parse.quote(record_id, safe="")
                if record_id else "")
            if not url:
                continue
            abstract = next((S._plain_text(S.as_str(a.get("value")), 600)
                             for a in S._kb_array(meta.get("abstracts"), "abstracts")
                             if isinstance(a, dict) and S.as_str(a.get("value"))), "")
            arxiv = next((S.as_str(e.get("value")) for e in S._kb_array(meta.get("arxiv_eprints"),
                                                                   "arxiv_eprints")
                          if isinstance(e, dict) and S.as_str(e.get("value"))), "")
            authors = [S.as_str(a.get("full_name")) for a in S._kb_array(meta.get("authors"), "authors")
                       if isinstance(a, dict)]
            count = meta.get("citation_count")
            items.append({"title": title, "url": url, "summary": abstract, "doi": doi,
                          "identifier": arxiv or record_id, "kind": "paper",
                          "year": S.as_str(meta.get("earliest_date"))[:4],
                          "authors": [a for a in authors if a][:8],
                          "extra": {"arxiv": arxiv,
                                    "citation_count": count if type(count) is int else None}})
        return S._kb_generic_result("inspirehep", items, attribution)

    return S._kb_new_cached(f"inspirehep:{query}:{limit}", "inspirehep", produce)



EXPORTS = {"kb_inspirehep": kb_inspirehep}


def backend(q, limit, opts):
    return S.kb_inspirehep(q, limit)


register("inspirehep", kb_inspirehep, backend)
