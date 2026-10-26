"""librariesio（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_librariesio(query: str, limit: int = 5) -> dict:
    """Libraries.io のパッケージ検索（19+ 登録簿を横断）。説明は登録者の自己申告。利用者自身のキーが必要。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "librariesio", "error": "query は必須です"}
    if not S.LIBRARIESIO_KEY:
        return {"source": "librariesio",
                "error": "API キー未設定: Libraries.io は無料の登録キーが必要です。"
                         "https://libraries.io/account で取得し、環境変数 FREEAGENT_LIBRARIESIO_KEY に"
                         "設定してください（設定するまで HTTP は出しません）"}
    attribution = "データ提供: Libraries.io https://libraries.io/（各登録簿の条件に従う）"

    def produce() -> dict:
        params = {"q": query, "per_page": limit, "api_key": S.LIBRARIESIO_KEY}
        data, err = S._kb_new_json("https://libraries.io/api/search?"
                                 + urllib.parse.urlencode(params), 1.0)
        if err:
            return {"source": "librariesio", "error": err}
        rows = S._kb_array(data, "projects")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Libraries.io の project はオブジェクトではありません")
            name, platform = S.as_str(row.get("name")), S.as_str(row.get("platform"))
            if not name or not platform:
                continue
            downloads = row.get("downloads")
            items.append({"title": f"{platform}/{name}",
                          "url": "https://libraries.io/" + urllib.parse.quote(platform, safe="")
                                 + "/" + urllib.parse.quote(name, safe=""),
                          "summary": S._plain_text(S.as_str(row.get("description")), 400),
                          "identifier": name, "version": S.as_str(row.get("latest_release_number")),
                          "year": S.as_str(row.get("latest_release_published_at"))[:4],
                          "repository_url": S.as_str(row.get("repository_url")),
                          "downloads": downloads if type(downloads) is int else None,
                          "keywords": [S.as_str(k) for k in S._kb_array(row.get("keywords"), "keywords")
                                       if S.as_str(k)][:8]})
        return S._kb_package_result("librariesio", items, attribution)

    return S._kb_new_cached(f"librariesio:{query}:{limit}", "librariesio", produce)



EXPORTS = {"kb_librariesio": kb_librariesio}


def backend(q, limit, opts):
    return S.kb_librariesio(q, limit)


register("librariesio", kb_librariesio, backend)
