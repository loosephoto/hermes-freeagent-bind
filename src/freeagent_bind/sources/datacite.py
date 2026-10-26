"""datacite（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_datacite(query: str, limit: int = 5, kind: str = "all") -> dict:
    """公開 DOI メタデータ。all / arxiv / dataset。版・arXiv検索式の互換は保証しない。"""
    query = S.as_str(query)
    kind = S.as_str(kind, "all")
    limit = S.as_int(limit, 5, 1, 20)
    if not query or kind not in ("all", "arxiv", "dataset"):
        return {"source": "datacite", "error": "query と有効な datacite_kind（all / arxiv / dataset）が必要です"}

    def produce() -> dict:
        params = {"query": S._literal_search(query), "sort": "relevance", "page[size]": limit}
        if kind == "arxiv":
            params["client-id"] = "arxiv.content"
        elif kind == "dataset":
            params["resource-type-id"] = "dataset"
        if S.KB_MAILTO:
            params["mailto"] = S.KB_MAILTO
        data, err = S._kb_new_json("https://api.datacite.org/dois?" + urllib.parse.urlencode(params), 0.61)
        if err:
            return {"source": "datacite", "error": err}
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"source": "datacite", "error": "DataCite の応答形式が不正です"}
        items, cites = [], []
        for record in rows[:limit]:
            row = record.get("attributes") if isinstance(record, dict) else None
            if not isinstance(row, dict):
                continue
            title = next((S.as_str(t.get("title")) for t in (row.get("titles") or [])
                          if isinstance(t, dict) and S.as_str(t.get("title"))), "")
            doi = S.as_str(row.get("doi"))
            link = S.as_str(row.get("url")) or (f"https://doi.org/{doi}" if doi else "")
            if not title or not link:
                continue
            descriptions = [d for d in (row.get("descriptions") or []) if isinstance(d, dict)]
            abstract = next((S.as_str(d.get("description")) for d in descriptions
                             if d.get("descriptionType") == "Abstract" and S.as_str(d.get("description"))), "")
            summary = S._plain_text(abstract, 600)
            authors = [S.as_str(a.get("name")) for a in (row.get("creators") or []) if isinstance(a, dict)]
            year = row.get("publicationYear")
            item = {"title": S.truncate(title, 300), "url": link, "doi": doi, "year": year,
                    "authors": [a for a in authors if a][:8], "summary": summary,
                    "type": (row.get("types") or {}).get("resourceTypeGeneral"),
                    "metadata_only": not bool(summary)}
            items.append(item)
            cite = S._cite("datacite", title, link, year=year, doi=doi,
                         repository="arxiv" if kind == "arxiv" else "", metadata_only=not bool(summary))
            cite["summary"] = summary
            cite["year"] = year or ""
            cite["resource_type"] = item.get("type") or ""
            cites.append(cite)
        return {"source": "datacite", "datacite_kind": kind, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_new_cached(f"datacite:{kind}:{query}:{limit}", "datacite", produce)


EXPORTS = {"kb_datacite": kb_datacite}


def backend(q, limit, opts):
    return S.kb_datacite(q, limit, opts.get("datacite_kind", "all"))


register("datacite", kb_datacite, backend)
