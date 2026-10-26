"""openaire（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_openaire(query: str, limit: int = 5) -> dict:
    """Graph V3の論文メタデータ。匿名枠60/hをプロセス内60.1秒間隔で守る。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "openaire", "error": "query は必須です"}
    def produce():
        params = {"search": query, "type": "publication", "pageSize": limit}
        data, err = S._kb_new_json("https://api.openaire.eu/graph/v3/research-products?"
                                + urllib.parse.urlencode(params), 60.1)
        if err:
            return {"source": "openaire", "error": err}
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"source": "openaire", "error": "OpenAIRE の応答形式が不正です"}
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not S.as_str(row.get("mainTitle")):
                continue
            doi = next((S.as_str(p.get("value")) for p in (row.get("pids") or [])
                        if isinstance(p, dict) and p.get("scheme") == "doi"), "")
            instances = S._kb_array(row.get("instances"), "instances")
            if any(not isinstance(i, dict) for i in instances):
                raise ValueError("instances の要素がオブジェクトではありません")
            urls = [S._kb_http_url(u) for i in instances
                    for u in S._kb_array(i.get("urls"), "instances.urls")]
            record_id = S.as_str(row.get("id"))
            link = f"https://doi.org/{doi}" if doi else (urls[0] if urls else
                   "https://api.openaire.eu/graph/v3/research-products/" + urllib.parse.quote(record_id, safe=""))
            if not doi and not urls and not record_id:
                continue
            descriptions = S._kb_array(row.get("descriptions"), "descriptions")
            if any(not isinstance(d, str) for d in descriptions):
                raise ValueError("descriptions の要素が文字列ではありません")
            summary = S._plain_text(" ".join(d for d in descriptions if isinstance(d, str)), 600)
            authors = [S.as_str(a.get("fullName")) for a in (row.get("authors") or []) if isinstance(a, dict)]
            items.append({"title": S.truncate(row["mainTitle"], 300), "summary": summary, "url": link,
                          "doi": doi, "id": record_id, "year": S.as_str(row.get("publicationDate"))[:4],
                          "authors": [a for a in authors if a][:8],
                          "licenses": list(dict.fromkeys(S.as_str(i.get("license")) for i in instances if S.as_str(i.get("license")))),
                          "cited_by": (((row.get("indicators") or {}).get("citationImpact") or {}).get("citationCount")),
                          "metadata_only": not bool(summary)})
        return S._kb_paper_result("openaire", items,
                               attribution="データ提供: OpenAIRE（CC-BY） https://graph.openaire.eu/")
    return S._kb_new_cached(f"openaire:{query}:{limit}", "openaire", produce)



EXPORTS = {"kb_openaire": kb_openaire}


def backend(q, limit, opts):
    return S.kb_openaire(q, limit)


register("openaire", kb_openaire, backend)
