"""openalex（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def _openalex_abstract(row: dict, limit: int = 500) -> str:
    """OpenAlex は本文を `abstract_inverted_index`（語 → 位置の配列）で返すので復元する。

    そのままでは人間にもサブLLMにも読めず、citation の summary が空になっていた。
    """
    inv = row.get("abstract_inverted_index")
    if not isinstance(inv, dict):
        return ""
    placed: dict[int, str] = {}
    for word, positions in inv.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for pos in positions:
            index = S.as_int(pos, -1, 0, 10**6)
            if index >= 0:
                placed.setdefault(index, str(word))
    if not placed:
        return ""
    return S.truncate(" ".join(placed[k] for k in sorted(placed)), limit)


def kb_openalex(query: str, limit: int = 5) -> dict:
    """書誌＋被引用数＋OA リンク。`mailto` を付けると polite pool に入る（任意）。"""
    limit = S.as_int(limit, 5, 1, 20)

    def produce() -> dict:
        params = {"search": query, "per-page": limit}
        if S.KB_MAILTO:
            params["mailto"] = S.KB_MAILTO
        if S.OPENALEX_API_KEY:
            params["api_key"] = S.OPENALEX_API_KEY
        data, err = S.kb_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        if err:
            # 提供元側の一時停止は「自分のバグ」と区別して伝える（再試行の判断が変わる）
            low = err.lower()
            if "temporarily unavailable" in low or "paused" in low or "503" in low:
                return {"source": "openalex", "items": [], "citations": [],
                        "provider_status": "paused",
                        "error": "OpenAlex の検索基盤が提供元側で一時停止中です（匿名検索の停止）。"
                                 "OPENALEX_API_KEY を設定すると回避できます"}
            return {"source": "openalex", "error": err}
        items, cites = [], []
        for row in (data or {}).get("results") or []:
            title = row.get("title") or row.get("display_name") or ""
            authors = [(a.get("author") or {}).get("display_name")
                       for a in (row.get("authorships") or [])]
            authors = [a for a in authors if a]
            source_name = (((row.get("primary_location") or {}).get("source") or {}).get("display_name")) or ""
            url = row.get("doi") or row.get("id") or ""
            oa = row.get("open_access") or {}
            items.append({"title": S.truncate(title, 300), "year": row.get("publication_year"),
                          "cited_by": row.get("cited_by_count"), "container": source_name,
                          "authors": authors[:8], "url": url,
                          "oa_url": oa.get("oa_url") or "", "is_oa": bool(oa.get("is_oa")),
                          "type": row.get("type") or ""})
            cites.append(S._cite("openalex", title, url, year=row.get("publication_year"),
                               cited_by=row.get("cited_by_count"),
                               summary=_openalex_abstract(row)))
        return {"source": "openalex", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_cached(f"openalex:{query}:{limit}", produce)


EXPORTS = {"_openalex_abstract": _openalex_abstract, "kb_openalex": kb_openalex}


def backend(q, limit, opts):
    return S.kb_openalex(q, limit=limit)


register("openalex", kb_openalex, backend)
