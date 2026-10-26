"""swh（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_swh(query: str, limit: int = 5) -> dict:
    """Software Heritage の公開コードアーカイブ。リポジトリ（origin）の存在・URL・取得方式を引く。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "swh", "error": "query は必須です"}
    attribution = ("データ提供: Software Heritage（Inria。API 利用規約に従う） "
                   "https://www.softwareheritage.org/")

    def produce() -> dict:
        pattern = urllib.parse.quote(query.strip(), safe="")
        data, err = S._kb_new_json("https://archive.softwareheritage.org/api/1/origin/search/"
                                 + pattern + "/?" + urllib.parse.urlencode({"limit": limit}), 1.0)
        if err:
            return {"source": "swh", "error": err}
        rows = S._kb_array(data, "origins")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Software Heritage の origin はオブジェクトではありません")
            origin = S.as_str(row.get("url"))
            if not origin:
                continue
            visit_types = [S.as_str(v) for v in S._kb_array(row.get("visit_types"), "visit_types")
                           if S.as_str(v)]
            facts = [f"アーカイブ済み origin: {origin}"]
            if visit_types:
                facts.append("取得方式 " + ", ".join(visit_types[:3]))
            items.append({"title": origin, "url": origin, "summary": "／".join(facts),
                          "identifier": origin, "kind": "software-origin",
                          "summary_kind": "structured_record",
                          "extra": {"visit_types": visit_types,
                                    "browse_url": "https://archive.softwareheritage.org/browse/origin/"
                                                  "?origin_url=" + urllib.parse.quote(origin, safe="")}})
        return S._kb_generic_result("swh", items, attribution)

    return S._kb_new_cached(f"swh:{query}:{limit}", "swh", produce)



EXPORTS = {"kb_swh": kb_swh}


def backend(q, limit, opts):
    return S.kb_swh(q, limit)


register("swh", kb_swh, backend)
