"""arxiv（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

# CJK（かな・漢字・ハングル）だけの問い。arXiv の索引は英語なので 0 件になる（実測）。
_CJK_ONLY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def kb_arxiv(query: str, limit: int = 5) -> dict:
    """プレプリント検索（Atom XML）。検索語は `all:` に寄せる（フィールド指定はそのまま活かす）。

    **CJK だけの問いは HTTP を出さずに断る**（v0.24.0）。arXiv は日本語クエリで 0 件（実測）なのに、
    呼ぶと `_arxiv_throttle` の 3 秒直列枠を占有し、`grounded` / `lookup` の締め切り（8 秒）を
    その分だけ食う。英語の語が 1 つも無いときは**理由を返す**（「該当なし」と混同させない）。
    """
    limit = S.as_int(limit, 5, 1, 20)
    if _CJK_ONLY_RE.search(query or "") and not re.search(r"[A-Za-z]", query or ""):
        return {"source": "arxiv", "items": [], "citations": [],
                "error": "arXiv は英語の語で引く必要があります（この問いには英字がありません）。"
                         "日本語で調べるなら wikipedia / wikidata / qiita を sources に指定してください"}
    field_q = query if re.search(r"\b(all|ti|au|abs|cat):", query) else f"all:{query}"

    def produce() -> dict:
        # **https でなければならない**（実測: http は 301 を返し、urllib のリダイレクト処理の先で
        # 406 になる。https なら 200）。
        url = ("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": field_q, "start": 0, "max_results": limit,
             "sortBy": "relevance", "sortOrder": "descending"}))
        status, body = 0, ""
        for attempt in range(3):
            S._arxiv_throttle()
            status, body = S.kb_http(url, accept="application/atom+xml")
            if status != 406:
                break
        if status != 200:
            return {"source": "arxiv", "error": f"HTTP {status}: {body[:200]}" if status else body}
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            return {"source": "arxiv", "error": f"XML 解析失敗: {exc}"}
        items, cites = [], []
        for entry in root.findall("a:entry", ns)[:limit]:
            link = ""
            for ln in entry.findall("a:link", ns):
                if ln.get("rel") == "alternate" or not link:
                    link = ln.get("href") or link
            title = " ".join((entry.findtext("a:title", "", ns) or "").split())
            summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
            authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
            items.append({
                "id": entry.findtext("a:id", "", ns),
                "title": S.truncate(title, 300),
                "summary": S.truncate(summary, 1200),
                "published": (entry.findtext("a:published", "", ns) or "")[:10],
                "updated": (entry.findtext("a:updated", "", ns) or "")[:10],
                "authors": authors[:8],
                "primary_category": (entry.find("arxiv:primary_category", ns).get("term")
                                     if entry.find("arxiv:primary_category", ns) is not None else ""),
                "doi": entry.findtext("arxiv:doi", "", ns),
                "url": link,
            })
            cites.append(S._cite("arxiv", title, link,
                               year=(entry.findtext("a:published", "", ns) or "")[:4],
                               authors=authors[:3],
                               summary=S._plain_text(summary, 600)))
        return {"source": "arxiv", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_cached(f"arxiv:{field_q}:{limit}", produce)


EXPORTS = {"kb_arxiv": kb_arxiv}


def backend(q, limit, opts):
    return S.kb_arxiv(q, limit=limit)


register("arxiv", kb_arxiv, backend)
