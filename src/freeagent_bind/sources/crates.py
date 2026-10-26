"""crates（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_crates(query: str, limit: int = 5) -> dict:
    """crates.io のクレート検索。Crawler Policy（1 req/s・識別UA）に従う。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "crates", "error": "query は必須です"}
    def produce():
        data, err = S._kb_new_json("https://crates.io/api/v1/crates?"
                                + urllib.parse.urlencode({"q": query, "per_page": limit}), 1.01)
        if err:
            return {"source": "crates", "error": err}
        rows = S._kb_array(S._kb_object(data, "crates.io応答").get("crates"), "crates")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("crates.io record はオブジェクトではありません")
            name = S.as_str(row.get("name"))
            if not name:
                continue
            summary = S._plain_text(S.as_str(row.get("description")), 400)
            items.append({"title": name, "url": "https://crates.io/crates/" + urllib.parse.quote(name, safe=""),
                          "version": S.as_str(row.get("max_stable_version")) or S.as_str(row.get("max_version")),
                          "year": S.as_str(row.get("updated_at"))[:4], "summary": summary,
                          "repository_url": S.as_str(row.get("repository")),
                          "downloads": row.get("downloads") if type(row.get("downloads")) is int else None,
                          "metadata_only": not bool(summary)})
        return S._kb_package_result("crates", items, "データ提供: crates.io https://crates.io/")
    return S._kb_new_cached(f"crates:{query}:{limit}", "crates", produce)



EXPORTS = {"kb_crates": kb_crates}


def backend(q, limit, opts):
    return S.kb_crates(q, limit)


register("crates", kb_crates, backend)
