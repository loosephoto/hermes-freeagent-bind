"""wikipedia（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

# CJK（かな・漢字・ハングル）を含むかどうかだけを見る。言語判定器は使わない（決定的・依存ゼロ）。
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _kb_auto_lang(query: str, lang: str = "") -> str:
    """ソースの言語を決める。明示があればそれを尊重し、無ければ問いから決める（CJK を含む→ja、それ以外→en）。

    実測（2026-10-17）: **ASCII の問いを ja 索引に投げると無関係な記事が返る**。
    `Transformer` は ja で「トランスフォーマー (曖昧さ回避) / ASUS Eee Pad TF101 / TF201」、
    en では「Transformer / Transformer (deep learning) / Generative pre-trained transformer」。
    `diffusion model` も ja は「フィジカル・レビュー / 拡散律速凝集」で en は「Diffusion model」。
    逆に日本語の問いは ja が正しい（`強化学習` は ja 1 位、en は「安倍晋三暗殺」）。混在（`LLM 大規模言語モデル`）は
    CJK があるので ja。**wikidata は ja のままが良い**（実測: `Transformer` は ja の 2 件目に
    「トランスフォーマー（機械学習アーキテクチャ）」が出るが、en は変圧器とメディア作品）ので、
    この規則は wikipedia にだけ適用する。
    """
    explicit = S.as_str(lang).strip().lower()
    if explicit:
        return explicit
    return "ja" if _CJK_RE.search(S.as_str(query)) else "en"


def _wiki_exact_hit(data) -> dict | None:
    """`titles=` の応答から「存在し、中身のある」1 件を返す（無ければ None）。

    実測（ja.wikipedia, 2026-10-17）: 「大規模言語モデル」は記事があるのに素の検索では 20 件中 **19 位**で、
    既定の `gsrlimit=3` では窓の外に落ちて無関係な項目（マークアップ言語・機械翻訳）だけが返っていた
    （`intitle:大規模言語モデル` なら 1 位、`pageid:4769449` も一致＝索引には載っている）。
    完全一致だけを先頭へ昇格させれば順位に依存せずに済む。
    **中身が空の extract は昇格させない**（実測: 「大規模言語モデルの一覧」は記事があるのに intro が 0 文字で、
    そのまま昇格させると空の項目が先頭に来る）。`missing` / `invalid` はキーの有無で判定する
    （値は空文字で返るため、値の真偽では見分けられない）。
    """
    pages = ((data or {}).get("query") or {}).get("pages") or {}
    for page in pages.values():
        if not isinstance(page, dict) or "missing" in page or "invalid" in page:
            continue
        title = S.as_str(page.get("title"))
        extract = S.as_str(page.get("extract"))
        if title and re.sub(r"<[^>]+>", "", extract).strip():
            return {"title": title, "extract": extract}
    return None


def kb_wikipedia(query: str, lang: str = "", limit: int = 3) -> dict:
    """本文の要約＋検索結果。出典 URL 付き（LLM 不使用）。

    - 検索（`generator=search`）と**タイトル完全一致の lookup（`titles=`）を並列**に投げ、完全一致があれば
      先頭に置く（§5.1 の順位対策）。演算子や引用符を含む問いでは lookup を投げない。
    - `lang` 省略時は**問いから自動**（`_kb_auto_lang`: CJK→ja、それ以外→en）。明示すればそれを尊重する。
    """
    lang = _kb_auto_lang(query, lang)
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", lang):
        return {"source": "wikipedia", "lang": lang, "items": [], "citations": [],
                "error": "lang は有効な Wikipedia 言語コードで指定してください"}
    limit = S.as_int(limit, 3, 1, 8)
    # 検索演算子・引用符を含む問いはタイトルではない（`intitle:` 等をタイトルとして引くと無関係な記事に当たる）。
    # 長すぎる問いもタイトルではない。このときは lookup を投げず、リクエストを増やさない。
    exact_lookup = bool(query) and len(query) <= 80 and not re.search(r'["~:]', query)

    def produce() -> dict:
        base = f"https://{lang}.wikipedia.org"

        def search_request() -> dict:
            q = urllib.parse.urlencode({
                "action": "query", "format": "json", "generator": "search",
                "gsrsearch": query, "gsrlimit": limit, "prop": "extracts",
                "exintro": 1, "explaintext": 1, "exchars": 1200,
            })
            data, err = S.kb_json(f"{base}/w/api.php?{q}")
            return {"data": data, "error": err}

        def exact_request() -> dict:
            """完全一致の記事を引く（`redirects=1` で転送も解決する）。投げない条件では data=None。"""
            if not exact_lookup:
                return {"data": None, "error": ""}
            q = urllib.parse.urlencode({
                "action": "query", "format": "json", "titles": query, "redirects": 1,
                "prop": "extracts", "exintro": 1, "explaintext": 1, "exchars": 1200,
            })
            data, err = S.kb_json(f"{base}/w/api.php?{q}")
            return {"data": data, "error": err}

        # 2 本を**並列**に投げる（実測: 逐次 1.15 秒 → 並列 0.58 秒。wikipedia は `kb_json` 経由で
        # ホスト間隔の制御が無く、同一ホストへの 2 並列でローカル 429 にならないことも実測で確認した）。
        search, exact = S.run_parallel([search_request, exact_request], lambda job: job())
        if not isinstance(search, dict):
            search = {"data": None, "error": "検索の実行に失敗しました"}
        if search.get("error") and not search.get("data"):
            return {"source": "wikipedia", "error": search["error"]}
        data = search.get("data")
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        hits = sorted((page for page in pages.values() if isinstance(page, dict)),
                      key=lambda page: S.as_int(page.get("index"), 999999, 0, 999999))
        # 完全一致を先頭へ（検索側にも同じタイトルがあれば重複を落とす）。失敗しても検索結果は変えない。
        promo = _wiki_exact_hit(exact.get("data") if isinstance(exact, dict) else None)
        if promo:
            hits = [{"title": promo["title"], "extract": promo["extract"]}] + [
                hit for hit in hits if S.as_str(hit.get("title")) != promo["title"]]
        items, cites = [], []
        for hit in hits[:limit]:
            title = S.as_str(hit.get("title"))
            if not title:
                continue
            page_url = f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            summary = re.sub(r"<[^>]+>", "", S.as_str(hit.get("extract")))
            items.append({"title": title, "url": page_url, "summary": S.truncate(summary, 1200),
                          "snippet": S.truncate(summary, 300)})
            cites.append(S._cite("wikipedia", title, page_url, lang=lang,
                               summary=S._plain_text(summary, 600)))
        return {"source": "wikipedia", "lang": lang, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_cached(f"wiki:{lang}:{query}:{limit}", produce)


EXPORTS = {"_CJK_RE": _CJK_RE, "_kb_auto_lang": _kb_auto_lang, "_wiki_exact_hit": _wiki_exact_hit, "kb_wikipedia": kb_wikipedia}


def backend(q, limit, opts):
    return S.kb_wikipedia(q, lang=opts.get("lang") or "", limit=limit)


register("wikipedia", kb_wikipedia, backend)
