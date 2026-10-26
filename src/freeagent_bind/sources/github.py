"""github（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def _code_excerpt(text: str, limit: int = 400) -> str:
    """コード断片を引用用に整える。**`_plain_text` は使わない**。

    `_plain_text` は `<[^>]+>` を落とすので、Java / C# / TypeScript のジェネリクス
    （`List<String>` など）が消えて意味が変わる。改行とインデントは保つ。
    """
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    return S.truncate("\n".join(lines).strip(), limit)


_GH_SEARCH_INTERVAL = 2.0          # repo / issue（認証あり・30/分）
_GH_SEARCH_INTERVAL_UNAUTH = 6.0   # repo / issue（未認証・10/分）
_GH_CODE_INTERVAL = 6.0            # code（10/分・認証必須）


def _gh_fragment(row: dict) -> str:
    """code 検索の `text_matches` から一致断片を取り出す（同じファイルで最大 2 件まで返る）。"""
    matches = row.get("text_matches")
    if not isinstance(matches, list):
        return ""
    parts = [S.as_str(m.get("fragment"), "") for m in matches[:2] if isinstance(m, dict)]
    return "\n…\n".join(p for p in parts if p)


def kb_github(query: str, kind: str = "repo", limit: int = 5) -> dict:
    """リポジトリ / コード / Issue の検索。**コード検索はトークン必須**（未設定なら理由を返す）。

    `kind="code"` で返るのは **ファイルへのポインタと一致箇所の断片**だけで、
    **ライセンス情報を含まない**（取り込む前に `kind="repo"` などで別途確認する）。
    GitHub 側の索引も default branch のみ・384KB 未満・直近 1 年に活動のあるリポジトリなどに
    限られるので、「探して出ない」は「存在しない」を意味しない。
    """
    kind = S.as_str(kind, "repo")
    if kind not in ("repo", "code", "issue"):
        kind = "repo"
    limit = S.as_int(limit, 5, 1, 20)

    def produce() -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if S.GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {S.GITHUB_TOKEN}"
        if kind == "code":
            if not S.GITHUB_TOKEN:
                return {"source": "github", "kind": kind, "items": [], "citations": [],
                        "error": "コード検索は GITHUB_TOKEN（または GH_TOKEN）が必要です。"
                                 "トークンを設定するか kind=\"repo\"/\"issue\" を使ってください"}
            # 一致箇所の断片（text_matches.fragment）を受け取る。付けないと本文が一切返らない。
            headers["Accept"] = "application/vnd.github.text-match+json"
        endpoint = {"repo": "repositories", "code": "code", "issue": "issues"}[kind]
        sort = "&sort=stars&order=desc" if kind == "repo" else ""
        url = (f"https://api.github.com/search/{endpoint}?"
               + urllib.parse.urlencode({"q": query, "per_page": limit}) + sort)
        interval = (_GH_CODE_INTERVAL if kind == "code" else
                    (_GH_SEARCH_INTERVAL if S.GITHUB_TOKEN else _GH_SEARCH_INTERVAL_UNAUTH))
        # **予算キーは search 全体で 1 枠**（実測 2026-10-20: repo と issue はどちらも
        # `X-RateLimit-Resource: search` で、交互に叩くと Remaining が 9→9→8→8 と減る＝枠が共通）。
        # エンドポイントごとに別のキーにすると、**同じホストの枠を奪い合う**
        # （fan-out で成功 1/4 になっていた）。`code` 検索は枠が違うので別キーにする。
        budget_key = "api.github.com/search/code" if kind == "code" else "api.github.com/search"
        data, err = S._kb_new_json(url, interval, budget_key=budget_key,
                                 extra_headers=headers)
        if err:
            return {"source": "github", "kind": kind, "error": err}
        rows = (data or {}).get("items") or []
        items, cites = [], []
        for row in rows[:limit]:
            if kind == "repo":
                title = row.get("full_name") or ""
                items.append({"title": title, "url": row.get("html_url") or "",
                              "description": S.truncate(row.get("description") or "", 400),
                              "stars": row.get("stargazers_count"), "language": row.get("language"),
                              "updated_at": row.get("updated_at"), "topics": (row.get("topics") or [])[:8],
                              "license": ((row.get("license") or {}).get("spdx_id"))})
            elif kind == "issue":
                title = row.get("title") or ""
                items.append({"title": S.truncate(title, 300), "url": row.get("html_url") or "",
                              "state": row.get("state"), "comments": row.get("comments"),
                              "created_at": row.get("created_at"),
                              "repository_url": row.get("repository_url") or ""})
            else:
                repo = row.get("repository") or {}
                title = row.get("name") or ""
                fragment = _code_excerpt(_gh_fragment(row), 400)
                items.append({"title": title, "url": row.get("html_url") or "",
                              "repository": repo.get("full_name") or "",
                              "repo_url": repo.get("html_url") or "",
                              "path": row.get("path") or "",
                              "snippet": fragment})
            if kind == "repo":
                summary = S._plain_text(row.get("description") or "", 400)
            elif kind == "issue":
                summary = S._plain_text(row.get("body") or "", 400)
            else:
                # 断片はコードなので _plain_text を通さない（ジェネリクスが消える）。
                summary = fragment or f"{((row.get('repository') or {}).get('full_name')) or ''} {row.get('path') or ''}"
            cites.append(S._cite("github", title, row.get("html_url") or "", kind=kind, summary=summary))
        return {"source": "github", "kind": kind, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_cached(f"gh:{kind}:{query}:{limit}:{bool(S.GITHUB_TOKEN)}", produce)


EXPORTS = {"_code_excerpt": _code_excerpt, "_GH_SEARCH_INTERVAL": _GH_SEARCH_INTERVAL, "_GH_SEARCH_INTERVAL_UNAUTH": _GH_SEARCH_INTERVAL_UNAUTH, "_GH_CODE_INTERVAL": _GH_CODE_INTERVAL, "_gh_fragment": _gh_fragment, "kb_github": kb_github}


def backend(q, limit, opts):
    return S.kb_github(q, kind=opts.get("kind") or "repo", limit=limit)


register("github", kb_github, backend)
