"""hn（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_hn(query: str, limit: int = 5, need_evidence: bool = False) -> dict:
    """Hacker News（Algolia 検索）の投稿。帰属表示が必要。本文は投稿者による議論テキスト。

    `need_evidence=True`（= `grounded` の場面）なら**コメントも検索する**（§6.18）。
    実測: `tags=story` の `story_text` はほぼ空（リンク投稿）→ 根拠 0 件。
    `tags=comment` の `comment_text` には議論本文が入る（実測 263〜1607 文字）。
    """
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "hn", "error": "query は必須です"}
    attribution = ("データ提供: Hacker News（Algolia HN Search。帰属表示必須） "
                   "https://hn.algolia.com/")

    def produce() -> dict:
        # **story と comment の両方を検索する**（§6.18）。
        # 実測: `tags=story` の `story_text` は**ほぼ空**（リンク投稿のため）→ 根拠 0 件。
        # 一方 `tags=comment` の `comment_text` には**議論本文**が入る（実測 263〜1607 文字）。
        # Algolia は `tags` を複数指定できない（実測: `(story,comment)` は story と同じ結果）。
        # → **別のクエリとして 2 回**取り、コメントを先に（本文が根拠になるため）、story を後に置く。
        params = {"query": query, "tags": "story", "hitsPerPage": limit}
        data, err = S._kb_new_json("https://hn.algolia.com/api/v1/search?"
                                 + urllib.parse.urlencode(params), 0.4)
        if err:
            return {"source": "hn", "error": err}
        rows = S._kb_array(S._kb_object(data, "HN応答").get("hits"), "hits")
        comment_rows: list = []
        if need_evidence:
            cparams = {"query": query, "tags": "comment", "hitsPerPage": limit}
            # **予算キーを分ける**（同じホストの別エンドポイント。`_kb_new_json` の逃げ道）。
            # 分けないと 2 連打で 2 番目が「HTTP 429: ローカルのアクセス間隔制御」で落ち、
            # comment 側の結果が 0 件になっていた（実測）。
            cdata, cerr = S._kb_new_json("https://hn.algolia.com/api/v1/search?"
                                       + urllib.parse.urlencode(cparams), 0.4,
                                       budget_key="hn.algolia.com/search-comment")
            if cerr:
                # comment が落ちても story は生かす（**片方の失敗で全部dropsさせない**）。
                comment_rows = []
            else:
                comment_rows = S._kb_array(S._kb_object(cdata, "HN応答").get("hits"), "hits")
        items = []
        # コメント → story の順（**本文が根拠になる方を先に**出す）。
        for row, is_comment in [(r, True) for r in comment_rows] + [(r, False) for r in rows]:
            if not isinstance(row, dict):
                raise ValueError("HN の hit はオブジェクトではありません")
            oid = S.as_str(row.get("objectID"))
            title = S._plain_text(S.as_str(row.get("title") or row.get("story_title")), 300)
            if not oid or not title:
                continue
            body = S.as_str(row.get("comment_text") if is_comment else row.get("story_text"))
            points, comments = row.get("points"), row.get("num_comments")
            extra = {"points": points if type(points) is int else None,
                     "comments": comments if type(comments) is int else None,
                     "article_url": S.as_str(row.get("url"))}
            if is_comment:
                # コメントは**属する story の ID** を残す（上位 story へ辿れるように）。
                extra["story_id"] = S.as_str(row.get("story_id")) or None
                extra["parent_id"] = S.as_str(row.get("parent_id")) or None
            items.append({"title": title,
                          "url": "https://news.ycombinator.com/item?id=" + urllib.parse.quote(oid, safe=""),
                          "summary": S._plain_text(body, 600),
                          "identifier": oid, "kind": "hn-comment" if is_comment else "hn-story",
                          "summary_kind": "discussion",
                          "year": S.as_str(row.get("created_at"))[:4], "container": "Hacker News",
                          "authors": [S.as_str(row.get("author"))] if S.as_str(row.get("author")) else [],
                          "extra": extra})
            if len(items) >= limit:
                break
        return S._kb_generic_result("hn", items, attribution)

    # キャッシュキーに**根拠の有無を入れる**（コメント検索の有無で結果が違う。§6.18）。
    return S._kb_new_cached(f"hn:{query}:{limit}:{'evi' if need_evidence else 'bib'}", "hn", produce)


EXPORTS = {"kb_hn": kb_hn}


def backend(q, limit, opts):
    return S.kb_hn(q, limit,
                                                    need_evidence=opts.get("need_evidence") is True)


register("hn", kb_hn, backend)
