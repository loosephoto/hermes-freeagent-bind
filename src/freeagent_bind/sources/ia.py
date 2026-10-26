"""ia（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

_IA_CJK = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff00-\uffef]")
IA_INTERVAL = 1.0                                  # IA は数値レートを公表していない（自粛の間隔）
IA_SEARCH_BUDGET = "archive.org/advancedsearch"    # 同一ホストの metadata API と予算を分ける（規約 32）
_IA_SORTS = ("downloads desc", "week desc", "publicdate desc", "title asc")


def _ia_query(query: str) -> str:
    """自然語を `title:(語 AND 語)` に組み直す。素の語だと順位が崩れる（実測）。"""
    words = [word for word in re.split(r"\s+", query.strip()) if word]
    if len(words) == 1:
        return f"title:({words[0]})"
    return "title:(" + " AND ".join(words) + ")"


def kb_internetarchive(query: str, limit: int = 5, sort: str = "downloads desc") -> dict:
    """Internet Archive のアイテム検索（メタデータのみ）。query は**英語の語**で指定する。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    sort = S.as_str(sort, "downloads desc")
    if not query:
        return {"source": "ia", "error": "query は必須です"}
    if _IA_CJK.search(query):
        return {"source": "ia", "items": [], "citations": [],
                "error": "Internet Archive のメタデータ検索は日本語（CJK）クエリを受け付けません"
                         "（実測 10/10 が QUERY_NOT_READY）。英語の語に直して指定してください"}
    attribution = "データ提供: Internet Archive https://archive.org/（各アイテムの条件に従う）"

    def produce() -> dict:
        params = [("q", _ia_query(query)), ("rows", limit), ("page", 1), ("output", "json")]
        for field in ("identifier", "title", "creator", "year", "mediatype", "description",
                      "downloads", "licenseurl"):
            params.append(("fl[]", field))
        if sort in _IA_SORTS:
            params.append(("sort[]", sort))
        data, err = S._kb_new_json("https://archive.org/advancedsearch.php?"
                                 + urllib.parse.urlencode(params), IA_INTERVAL,
                                 budget_key=IA_SEARCH_BUDGET)
        if err:
            return {"source": "ia", "error": err}
        # 上流は構文エラーも QUERY_NOT_READY も **HTTP 200 で** {"error": "..."} として返す。
        # 状態コードではなく応答の形で判定する（実測）。
        if not isinstance(data, dict) or "response" not in data:
            detail = S.as_str(data.get("error"))[:200] if isinstance(data, dict) else ""
            return {"source": "ia", "items": [], "citations": [],
                    "error": "Internet Archive の検索が失敗しました: " + (detail or "応答形式が不正です")}
        response = data.get("response")
        rows = response.get("docs") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            return {"source": "ia", "error": "Internet Archive の応答形式が不正です"}
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Internet Archive の doc はオブジェクトではありません")
            identifier, title = S.as_str(row.get("identifier")), S.as_str(row.get("title"))
            if not identifier or not title:
                continue
            description = row.get("description")
            if isinstance(description, list):
                description = " ".join(S.as_str(part) for part in description)
            creator = row.get("creator")
            if isinstance(creator, list):
                creator = ", ".join(S.as_str(part) for part in creator if S.as_str(part))
            downloads = row.get("downloads")
            mediatype, year = S.as_str(row.get("mediatype")), S.as_str(row.get("year"))
            items.append({"title": S._plain_text(title, 300),
                          "url": "https://archive.org/details/" + urllib.parse.quote(identifier, safe=""),
                          "summary": S._plain_text(S.as_str(description), 600),
                          "identifier": identifier, "kind": "archive-item",
                          "summary_kind": "structured_record", "year": year, "container": mediatype,
                          "extra": {"mediatype": mediatype, "creator": S.as_str(creator),
                                    "downloads": downloads if type(downloads) is int else None,
                                    "licenseurl": S.as_str(row.get("licenseurl"))}})
        return S._kb_generic_result("ia", items, attribution)

    return S._kb_new_cached(f"ia:{_ia_query(query)}:{limit}:{sort}", "ia", produce)



EXPORTS = {"_IA_CJK": _IA_CJK, "IA_INTERVAL": IA_INTERVAL, "IA_SEARCH_BUDGET": IA_SEARCH_BUDGET, "_IA_SORTS": _IA_SORTS, "_ia_query": _ia_query, "kb_internetarchive": kb_internetarchive}


def backend(q, limit, opts):
    return S.kb_internetarchive(q, limit)


register("ia", kb_internetarchive, backend)
