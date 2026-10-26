"""ietf（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def _ietf_slug(value) -> str:
    """datatracker は std_level を `/api/v1/name/stdlevelname/ps/` の形で返す。末尾だけ残す。"""
    parts = [p for p in S.as_str(value).split("/") if p]
    return parts[-1] if parts else ""


def _ietf_term(query: str) -> str:
    """API へ渡す 1 語。タイトルの部分一致は AND できない（実測: 2 語指定は 0 件）ので最長語。"""
    words = S._ietf_terms(query)
    return max(words, key=len) if words else query.strip()


def kb_ietf(query: str, limit: int = 5) -> dict:
    """IETF の RFC / Internet-Draft（書誌＋抄録）。検索はタイトルの部分一致（複数語は最長語 1 つ）。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "ietf", "error": "query は必須です"}
    attribution = "データ提供: IETF Datatracker https://datatracker.ietf.org/"
    terms = S._ietf_terms(query)
    term = _ietf_term(query)

    def produce() -> dict:
        # RFC 番号（"RFC 9110" / "rfc9110" / "9110"）と Internet-Draft 名は name で直引きできる
        # （実測 2026-10-05: name=rfc9110 が HTTP Semantics を返す）。
        stripped = query.strip()
        number = re.fullmatch(r"(?:rfc[\s-]*)?(\d{3,5})", stripped, re.I)
        if number:
            params = {"format": "json", "limit": limit, "name": f"rfc{number.group(1)}"}
        elif re.fullmatch(r"draft-[a-z0-9-]+", stripped, re.I):
            params = {"format": "json", "limit": limit,
                      "name__contains": stripped.lower()}
        else:
            # 実測: type__in で draft と rfc に限定できる（slides や chairs を除く）。
            # 複数語はサーバー側で AND できないので多めに取り、全語がタイトルに載るものを優先する。
            params = {"format": "json", "limit": max(limit, min(limit * 5, 25)),
                      "title__contains": term, "type__in": "draft,rfc"}
        data, err = S._kb_new_json("https://datatracker.ietf.org/api/v1/doc/document/?"
                                 + urllib.parse.urlencode(params), 1.0)
        if err:
            return {"source": "ietf", "error": err}
        rows = [r for r in S._kb_array(S._kb_object(data, "IETF応答").get("objects"), "objects")
                if isinstance(r, dict)]
        if len(terms) > 1 and "title__contains" in params:
            exact = [r for r in rows
                     if all(w.lower() in S.as_str(r.get("title")).lower() for w in terms)]
            rows = exact or rows
        items = []
        for row in rows[:limit]:
            name = S.as_str(row.get("name"))
            title = S._plain_text(S.as_str(row.get("title")), 300)
            if not name or not title:
                continue
            number = S.as_int(row.get("rfc_number"), 0, 0, 99999)
            items.append({"title": title,
                          "url": (f"https://datatracker.ietf.org/doc/rfc{number}/" if number
                                  else "https://datatracker.ietf.org/doc/"
                                       + urllib.parse.quote(name, safe="") + "/"),
                          "summary": S._plain_text(S.as_str(row.get("abstract")), 600),
                          "identifier": f"RFC {number}" if number else name,
                          "kind": "RFC" if number else "Internet-Draft",
                          "version": S.as_str(row.get("rev")), "year": S.as_str(row.get("time"))[:4],
                          "extra": {"std_level": _ietf_slug(row.get("std_level")), "term": term,
                                    "pages": row.get("pages") if type(row.get("pages")) is int else None}})
        return S._kb_generic_result("ietf", items, attribution, term=term)

    return S._kb_new_cached(f"ietf:{query}:{limit}", "ietf", produce)



EXPORTS = {"_ietf_slug": _ietf_slug, "_ietf_term": _ietf_term, "kb_ietf": kb_ietf}


def backend(q, limit, opts):
    return S.kb_ietf(q, limit)


register("ietf", kb_ietf, backend)
