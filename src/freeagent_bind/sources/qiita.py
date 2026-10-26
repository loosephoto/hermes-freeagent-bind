"""qiita（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

# ---------------------------------------------------------------- §5.22 第9段階: 日本語の技術記事（Qiita・明示指定のみ）
#
# Qiita API v2 の `/api/v2/items?query=` はキー不要で動くキーワード検索。実測 0.18〜0.36 秒（6 サンプル）で
# 8 秒の締め切りに余裕で載る。**23 ソース中、日本語のプログラミング記事を返すものが 1 つも無い**
# （cinii は日本語だが学術文献）ので、既存ソースと重複しない。
#   * 絞り込み構文がそのまま使える（実測）: `tag:Python` / `user:foo` / `created:>2026-10-01` / `stocks:>50`。
#     日本語の語もそのまま引ける（実測 0.30 秒）。
#   * **間隔はトークンの有無で 2 定数**（github と同じ型）。公称レートは認証 1,000 回/時（≒16.7/分 → 3.6 秒）、
#     未認証 60 回/時（= 1/分 → 60.0 秒）。未認証では**同じ問いの再実行だけがキャッシュで即返り**（実測 0.000 秒）、
#     別の問いは 1 分待つ。これは Qiita の実クォータそのものなので隠さずエラーで見せる（規約 8）。
#   * **本文は `rendered_body`（HTML）を使い `_plain_text` に通す**。`body`（Markdown）だと `_plain_text` は
#     `<[^>]+>` しか落とさないので画像記法が引用文に残る（実測: `body` は画像 1・リンク 1 が残存、
#     `rendered_body` は 0/0 で本文だけ）。
#   * 証拠の種類は `discussion`（HN と同じユーザー生成の解説。査読論文ではない）。
#   * 見送り（実測）: Reddit（未認証は全経路 403 で遮断。Responsible Builder Policy が事前承認と
#     AI 目的の利用を要求）/ openBD（キーワード検索 API が無く ISBN 直引きのみ。実測 `isbn=abc` は `[null]`）/
#     Botipedia（公開 API が無い。全パスが同一 HTML の catch-all）/ Free Dictionary API（オリジン停止で 522）。
QIITA_INTERVAL = 3.6 if S.QIITA_TOKEN else 60.0     # 認証 1000/時 / 未認証 60/時（公称レートで割る）
QIITA_BUDGET = "qiita.com/api/v2/items"           # 同一ホストの別エンドポイントと予算を分ける（規約 32）


def kb_qiita(query: str, limit: int = 5) -> dict:
    """Qiita の日本語技術記事。query は Qiita の検索構文（tag:Python user:foo created:>2026-01-01）。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "qiita", "error": "query は必須です"}
    attribution = "データ提供: Qiita（Qiita API v2。記事の著作権は投稿者に帰属） https://qiita.com/"

    def produce() -> dict:
        params = {"query": query, "per_page": limit}
        headers = {"Authorization": f"Bearer {S.QIITA_TOKEN}"} if S.QIITA_TOKEN else None
        data, err = S._kb_new_json("https://qiita.com/api/v2/items?"
                                 + urllib.parse.urlencode(params), QIITA_INTERVAL,
                                 budget_key=QIITA_BUDGET, extra_headers=headers)
        if err:
            return {"source": "qiita", "error": err}
        rows = S._kb_array(data, "Qiita応答")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Qiita の item はオブジェクトではありません")
            title = S._plain_text(S.as_str(row.get("title")), 300)
            url = S.as_str(row.get("url"))
            if not title or not url:
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            tags = [S.as_str(tag.get("name")) for tag in S._kb_array(row.get("tags"), "tags")
                    if isinstance(tag, dict) and S.as_str(tag.get("name"))]
            likes, stocks = row.get("likes_count"), row.get("stocks_count")
            comments = row.get("comments_count")
            items.append({"title": title, "url": url,
                          # `body` は Markdown で、`_plain_text` は画像記法を落とせない（実測）。
                          "summary": S._plain_text(S.as_str(row.get("rendered_body")), 600),
                          "identifier": S.as_str(row.get("id")), "kind": "qiita-item",
                          "summary_kind": "discussion",
                          "year": S.as_str(row.get("created_at"))[:4], "container": "Qiita",
                          "authors": [S.as_str(user.get("id"))] if S.as_str(user.get("id")) else [],
                          "extra": {"tags": tags[:8],
                                    "likes_count": likes if type(likes) is int else None,
                                    "stocks_count": stocks if type(stocks) is int else None,
                                    "comments_count": comments if type(comments) is int else None,
                                    "updated_at": S.as_str(row.get("updated_at"))}})
        return S._kb_generic_result("qiita", items, attribution)

    return S._kb_new_cached(f"qiita:{query}:{limit}", "qiita", produce)



EXPORTS = {"QIITA_INTERVAL": QIITA_INTERVAL, "QIITA_BUDGET": QIITA_BUDGET, "kb_qiita": kb_qiita}


def backend(q, limit, opts):
    return S.kb_qiita(q, limit)


register("qiita", kb_qiita, backend)
