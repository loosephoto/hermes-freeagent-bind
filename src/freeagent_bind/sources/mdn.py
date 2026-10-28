"""mdn（MDN Web Docs の公開検索 API）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
MDN の `/api/v1/search` は公式に公開されたエンドポイント（CORS 有効・認証不要）。
robots.txt の `/api/` disallow は検索エンジンの大量インデックス化を狙ったもので、
人間の問いに応じた on-demand の検索（本ツールの用途）は対象外と判断した（SPEC 記録）。
内容は Mozilla contributors の CC-BY-SA 2.5。
"""
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

_MDN_INTERVAL = 1.0


def kb_mdn(query, limit=5, lang=""):
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "mdn", "error": "query は必須です"}
    locale = S.as_str(lang).strip() or "en-US"

    attribution = ("MDN Web Docs (Mozilla contributors, CC-BY-SA 2.5). "
                   "https://developer.mozilla.org/")

    def produce():
        params = {"q": query, "locale": locale, "size": limit, "sort": "relevance"}
        url = "https://developer.mozilla.org/api/v1/search?" + urllib.parse.urlencode(params)
        data, err = S._kb_new_json(url, _MDN_INTERVAL,
                                   budget_key="developer.mozilla.org/api")
        if err:
            return {"source": "mdn", "error": err}
        if not isinstance(data, dict):
            return {"source": "mdn", "error": "MDN の応答が JSON ではありません"}
        docs = S._kb_array(data.get("documents"), "documents")
        items = []
        for row in docs:
            if not isinstance(row, dict):
                continue
            title = S.as_str(row.get("title"))
            mdn_url = S.as_str(row.get("mdn_url"))
            if not title or not mdn_url:
                continue
            if not mdn_url.startswith("http"):
                mdn_url = "https://developer.mozilla.org" + mdn_url
            summary = S._plain_text(S.as_str(row.get("summary")), 400)
            items.append({"title": title, "url": mdn_url, "summary": summary,
                          "slug": S.as_str(row.get("slug")),
                          "score": row.get("score"),
                          "summary_kind": "structured_record"})
            if len(items) >= limit:
                break
        return S._kb_generic_result("mdn", items, attribution)

    return S._kb_new_cached("mdn:" + locale + ":" + query + ":" + str(limit), "mdn", produce)


EXPORTS = {"_MDN_INTERVAL": _MDN_INTERVAL, "kb_mdn": kb_mdn}


def backend(q, limit, opts):
    return S.kb_mdn(q, limit=limit, lang=opts.get("lang") or "")


register("mdn", kb_mdn, backend)
