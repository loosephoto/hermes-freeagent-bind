"""npm（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_npm(query: str, limit: int = 5) -> dict:
    """npm 公式レジストリのパッケージ検索。説明は登録者の自己申告（審査結果ではない）。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "npm", "error": "query は必須です"}
    def produce():
        data, err = S._kb_new_json("https://registry.npmjs.org/-/v1/search?"
                                + urllib.parse.urlencode({"text": query, "size": limit}), 1.0)
        if err:
            return {"source": "npm", "error": err}
        rows = S._kb_array(S._kb_object(data, "npm応答").get("objects"), "objects")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("npm object はオブジェクトではありません")
            pkg = S._kb_object(row.get("package"), "package")
            name = S.as_str(pkg.get("name"))
            if not name:
                continue
            links = S._kb_object(pkg.get("links"), "links")
            link = S.as_str(links.get("npm")) or "https://www.npmjs.com/package/" + urllib.parse.quote(name, safe="")
            summary = S._plain_text(S.as_str(pkg.get("description")), 400)
            keywords = [S.as_str(k) for k in S._kb_array(pkg.get("keywords"), "keywords") if S.as_str(k)]
            items.append({"title": name, "url": link, "version": S.as_str(pkg.get("version")),
                          "year": S.as_str(pkg.get("date"))[:4], "summary": summary,
                          "repository_url": S.as_str(links.get("repository")),
                          "keywords": keywords[:8], "metadata_only": not bool(summary)})
        return S._kb_package_result("npm", items,
                                  "データ提供: npm public registry https://registry.npmjs.org/")
    return S._kb_new_cached(f"npm:{query}:{limit}", "npm", produce)



EXPORTS = {"kb_npm": kb_npm}


def backend(q, limit, opts):
    return S.kb_npm(q, limit)


register("npm", kb_npm, backend)
