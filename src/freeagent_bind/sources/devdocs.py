"""devdocs（DevDocs の docset 索引検索）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
DevDocs は freeCodeCamp が運営する開発者向けドキュメント集約サービス。
docset ごとの index.json を公開しており、認証不要・robots は /settings のみ disallow。
779+ の docset（Python / JavaScript / React / Django / PostgreSQL 等）をカバーする。
"""
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

_DEVDOCS_INTERVAL = 1.0


def _devdocs_filter_entries(entries, query, limit):
    q = query.lower()
    out = []
    for row in entries:
        if not isinstance(row, dict):
            continue
        name = S.as_str(row.get("name"))
        if not name or q not in name.lower():
            continue
        path = S.as_str(row.get("path"))
        typ = S.as_str(row.get("type"))
        out.append({"name": name, "path": path, "type": typ})
        if len(out) >= limit:
            break
    return out


def kb_devdocs(query="", doc="", limit=5):
    query = S.as_str(query)
    doc = S.as_str(doc).strip()
    limit = S.as_int(limit, 5, 1, 20)
    if not query and not doc:
        return {"source": "devdocs", "error": "query または doc の指定が必要です"}

    attribution = ("DevDocs (freeCodeCamp) - content from upstream documentation "
                   "under their respective licenses. https://devdocs.io/")

    if not doc:
        def produce_list():
            url = "https://devdocs.io/docs.json"
            data, err = S._kb_new_json(url, _DEVDOCS_INTERVAL,
                                       budget_key="devdocs.io/docs")
            if err:
                return {"source": "devdocs", "error": err}
            if not isinstance(data, list):
                return {"source": "devdocs", "error": "DevDocs の応答が JSON ではありません"}
            q = query.lower() if query else ""
            items = []
            for row in data:
                if not isinstance(row, dict):
                    continue
                name = S.as_str(row.get("name"))
                slug = S.as_str(row.get("slug"))
                if not name or not slug:
                    continue
                if q and q not in name.lower() and q not in slug.lower():
                    continue
                items.append({"title": name, "url": "https://devdocs.io/" + slug,
                              "summary": "", "slug": slug,
                              "version": S.as_str(row.get("version") or row.get("release")),
                              "attribution": S.as_str(row.get("attribution")),
                              "summary_kind": "metadata_only"})
                if len(items) >= limit:
                    break
            return S._kb_generic_result("devdocs", items, attribution)

        return S._kb_new_cached("devdocs:list:" + query + ":" + str(limit), "devdocs", produce_list)

    def produce_search():
        url = "https://documents.devdocs.io/" + urllib.parse.quote(doc, safe="/~") + "/index.json"
        data, err = S._kb_new_json(url, _DEVDOCS_INTERVAL,
                                   budget_key="documents.devdocs.io/index")
        if err:
            return {"source": "devdocs", "error": err}
        if not isinstance(data, dict):
            return {"source": "devdocs", "error": "DevDocs index の応答が JSON ではありません"}
        entries = data.get("entries")
        if not isinstance(entries, list):
            return {"source": "devdocs", "error": "DevDocs index に entries がありません"}
        if query:
            filtered = _devdocs_filter_entries(entries, query, limit)
        else:
            filtered = []
            for row in entries:
                if not isinstance(row, dict):
                    continue
                name = S.as_str(row.get("name"))
                if not name:
                    continue
                filtered.append({"name": name, "path": S.as_str(row.get("path")),
                                 "type": S.as_str(row.get("type"))})
                if len(filtered) >= limit:
                    break
        items = []
        for row in filtered:
            name = row["name"]
            path = row["path"]
            typ = row["type"]
            page_url = "https://devdocs.io/" + doc + "/" + path if path else "https://devdocs.io/" + doc
            items.append({"title": name, "url": page_url, "summary": "",
                          "type": typ, "summary_kind": "metadata_only"})
        return S._kb_generic_result("devdocs", items, attribution, doc=doc)

    cache_key = "devdocs:" + doc + ":" + (query or "*") + ":" + str(limit)
    return S._kb_new_cached(cache_key, "devdocs", produce_search)


EXPORTS = {"_DEVDOCS_INTERVAL": _DEVDOCS_INTERVAL, "kb_devdocs": kb_devdocs,
           "_devdocs_filter_entries": _devdocs_filter_entries}


def backend(q, limit, opts):
    return S.kb_devdocs(q, doc=opts.get("doc") or "", limit=limit)


register("devdocs", kb_devdocs, backend)
