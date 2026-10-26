"""zenodo（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_zenodo(query: str, limit: int = 3) -> dict:
    """Zenodoの説明/注記はメタデータ。ファイルライセンスとは分離し全文・ファイルは取得しない。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 3, 1, 10)
    if not query:
        return {"source": "zenodo", "error": "query は必須です"}
    def produce():
        params = {"q": S._literal_search(query), "size": limit, "sort": "bestmatch"}
        data, err = S._kb_new_json("https://zenodo.org/api/records/?" + urllib.parse.urlencode(params), 2.01)
        if err:
            return {"source": "zenodo", "error": err}
        hits = S._kb_object(data, "Zenodo応答").get("hits")
        rows = S._kb_object(hits, "hits").get("hits")
        if not isinstance(rows, list):
            raise ValueError("Zenodo hits.hits は配列ではありません")
        items, cites = [], []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Zenodo record はオブジェクトではありません")
            meta = S._kb_object(row.get("metadata"), "metadata")
            title = S._open_metadata_text(meta.get("title"), 300)
            if not title:
                continue
            doi = S.as_str(row.get("doi")) or S.as_str(meta.get("doi"))
            links = S._kb_object(row.get("links"), "links")
            link = S.as_str(links.get("self_html")) or ("https://doi.org/" + doi if doi else "")
            if not link:
                continue
            description = " ".join(S.as_str(meta.get(k)) for k in ("description", "notes"))
            summary = S._open_metadata_text(description)
            year = S.as_str(meta.get("publication_date"))[:4]
            authors = [S._open_metadata_text(a.get("name"), 100) for a in S._kb_array(meta.get("creators"), "creators")
                       if isinstance(a, dict)]
            resource_type = S.as_str(S._kb_object(meta.get("resource_type"), "resource_type").get("type"))
            file_license = S.as_str(S._kb_object(meta.get("license"), "license").get("id"))
            item = {"title": title, "url": link, "doi": doi, "year": year, "summary": summary,
                    "authors": [a for a in authors if a][:8], "resource_type": resource_type,
                    "access_right": S.as_str(meta.get("access_right")), "file_license": file_license,
                    "license": "CC0-1.0", "summary_kind": "metadata_description", "metadata_only": not bool(summary)}
            cite = S._cite("zenodo", title, link, doi=doi, attribution="データ提供: Zenodo（メタデータCC0・ファイル条件は別） https://zenodo.org/")
            cite.update({k: v for k, v in item.items() if k != "authors"})
            items.append(item)
            cites.append(cite)
        return {"source": "zenodo", "items": items, "citations": cites,
                "attribution": "データ提供: Zenodo（メタデータCC0・ファイル条件は別） https://zenodo.org/",
                "error": "" if items else "該当なし"}
    return S._kb_new_cached(f"zenodo:{query}:{limit}", "zenodo", produce)



EXPORTS = {"kb_zenodo": kb_zenodo}


def backend(q, limit, opts):
    return S.kb_zenodo(q, limit)


register("zenodo", kb_zenodo, backend)
