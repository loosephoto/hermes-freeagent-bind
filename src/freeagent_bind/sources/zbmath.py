"""zbmath（zbMATH Open API・明示指定のみ・利用者自身の規約同意が前提・v0.39.0）.

差分は「**数学の分類（MSC）と書誌を zbMATH の識別子つきで引ける**」こと。Crossref / OpenAlex /
arXiv / OEIS では MSC 分類が取れない。

実測（2026-11-03・本機）:
  * `/v1/document/_search?search_string=` は 200・1.26〜2.32 秒。
  * **抄録は返らない**（`editorial_contributions` は "contents unavailable due to conflicting licenses"
    のことが多く、レビュー本文があるのは一部）。→ 書誌だけの行は `metadata_only` にする。
  * 素の語は緩い OR になる（実測: "scaling laws of large language models" で無関係な 3 件）。
    各語を引用して AND 結合すると当たる（"large language" AND "scaling" で 7 件・先頭は関連論文）。
  * **日本語だけの問いは HTTP 404**（`Entry not found!`）。索引は英語。
  * robots.txt は 404（制限なし）。**T&C への同意が前提**（FIZ Karlsruhe・1 年契約・データは CC BY-SA 4.0・
    帰属表示と項目ごとの license 遵守が義務）。サーバーは利用者の代わりに同意しない（規約 29）ので、
    `FREEAGENT_ZBMATH_ACK=1` が無ければ HTTP を出さない。
  * レートの公表値は無く「reasonable rate」とのみ規定。既定 1 req/s に自制する。
"""
import os
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

ZBMATH_INTERVAL = max(0.0, S._env_float("FREEAGENT_ZBMATH_INTERVAL", 1.0))
ZBMATH_BUDGET = "api.zbmath.org/document-search"
ZBMATH_API = "https://api.zbmath.org/v1/document/_search"
ZBMATH_TERMS = "https://static.zbmath.org/legal/api-terms-and-conditions.html"
ZBMATH_ATTRIBUTION = ("データ提供: zbMATH Open API（FIZ Karlsruhe）https://zbmath.org/ "
                      "CC BY-SA 4.0（各項目の license に従う）")

_CJK_ONLY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")
# レビュー本文が無いことを示す定型（実測: "zbMATH Open Web Interface contents unavailable due to
# conflicting licenses."）。これを本文根拠として引用しない。
_UNAVAILABLE = "unavailable"


def zbmath_ready() -> bool:
    """利用者が T&C に同意した申告を持っているか（既定は未同意）。"""
    return S._env_on("FREEAGENT_ZBMATH_ACK", False)


def _zbmath_title(row: dict) -> str:
    title = row.get("title")
    if isinstance(title, dict):
        parts = [S.as_str(title.get(key)) for key in ("title", "subtitle", "addition")]
        return " : ".join(p for p in parts if p)
    return S.as_str(title)


def _zbmath_review(row: dict) -> str:
    """レビュー本文（無い・ライセンス上出せない場合は空）。"""
    texts = []
    for entry in S._kb_array(row.get("editorial_contributions"), "editorial_contributions"):
        if not isinstance(entry, dict):
            continue
        text = S.as_str(entry.get("text"))
        if text and _UNAVAILABLE not in text.lower():
            texts.append(text)
    return " ".join(texts)


def kb_zbmath(query: str, limit: int = 5) -> dict:
    """zbMATH Open（数学の書誌・レビュー・MSC 分類）を検索する。英語の語と規約同意が必要。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "zbmath", "error": "query は必須です"}
    if not zbmath_ready():
        return {"source": "zbmath",
                "error": "規約未同意: zbMATH Open API は利用規約への同意が前提です（FIZ Karlsruhe・"
                         "データは CC BY-SA 4.0・帰属表示と項目ごとの license 遵守が義務）。"
                         + ZBMATH_TERMS + " を読み、同意する場合だけ環境変数 FREEAGENT_ZBMATH_ACK=1 を"
                         "設定してください（同意の判断は利用者。サーバーは代わりに同意せず、設定するまで"
                         "HTTP は出しません）"}
    if _CJK_ONLY_RE.search(query) and not re.search(r"[A-Za-z]", query):
        return {"source": "zbmath", "items": [], "citations": [],
                "error": "zbmath の索引は英語です（日本語だけの問いは 404 になります）。"
                         "英語の語（moduli space 等）で引くか、wikipedia / qiita を使ってください"}

    def produce() -> dict:
        params = {"search_string": S._literal_search(query), "results_per_page": limit}
        data, err = S._kb_new_json(ZBMATH_API + "?" + urllib.parse.urlencode(params),
                                   ZBMATH_INTERVAL, budget_key=ZBMATH_BUDGET)
        if err:
            if "404" in err:
                return {"source": "zbmath",
                        "error": "zbMATH に該当がありません（404 Entry not found。英語の語で引いてください）"}
            return {"source": "zbmath", "error": err}
        rows = S._kb_array(S._kb_object(data, "zbMATH応答").get("result"), "result")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            title = _zbmath_title(row)
            url = S.as_str(row.get("zbmath_url"))
            if not title or not url:
                continue
            ident = S.as_str(row.get("identifier"))
            raw_source = row.get("source")
            source = raw_source if isinstance(raw_source, dict) else {}
            review = _zbmath_review(row)
            keywords = [S.as_str(k) for k in S._kb_array(row.get("keywords"), "keywords") if S.as_str(k)]
            links = [entry for entry in S._kb_array(row.get("links"), "links") if isinstance(entry, dict)]
            doi = next((S.as_str(entry.get("identifier")) for entry in links
                        if S.as_str(entry.get("type")) == "doi"), "")
            facts = []
            if source.get("source"):
                facts.append(S._plain_text(S.as_str(source.get("source")), 200))
            if keywords:
                facts.append("キーワード " + ", ".join(keywords[:6]))
            if review:
                facts.append("レビュー " + S._plain_text(review, 500))
            summary = S._plain_text("／".join(f for f in facts if f), 600)
            items.append({"title": S._plain_text(title, 300), "url": url, "summary": summary,
                          "identifier": ident, "kind": "math-literature",
                          "year": S.as_str(row.get("year")),
                          "summary_kind": "editorial_review" if review else "structured_metadata",
                          "authors": [S.as_str(a.get("name")) for a in
                                      S._kb_array((row.get("contributors") or {}).get("authors"), "authors")
                                      if isinstance(a, dict) and S.as_str(a.get("name"))][:8],
                          "extra": {"doi": doi,
                                    "msc": [S.as_str(m.get("code")) for m in
                                            S._kb_array(row.get("msc"), "msc")
                                            if isinstance(m, dict) and S.as_str(m.get("code"))],
                                    "zbmath": ident,
                                    "links": [S.as_str(entry.get("url")) for entry in links
                                              if S.as_str(entry.get("url"))][:4]}})
        result = S._kb_generic_result("zbmath", items, ZBMATH_ATTRIBUTION)
        # レビュー本文が無い行は**本文根拠に使わせない**（書誌だけ・規約 28）。
        for item, cite in zip(items, result["citations"]):
            if item.get("summary_kind") != "editorial_review":
                cite["metadata_only"] = True
        return result

    return S._kb_new_cached(f"zbmath:{query}:{limit}", "zbmath", produce)


EXPORTS = {"ZBMATH_INTERVAL": ZBMATH_INTERVAL, "ZBMATH_BUDGET": ZBMATH_BUDGET,
           "ZBMATH_API": ZBMATH_API, "ZBMATH_TERMS": ZBMATH_TERMS,
           "zbmath_ready": zbmath_ready, "_zbmath_title": _zbmath_title,
           "_zbmath_review": _zbmath_review, "kb_zbmath": kb_zbmath}


def backend(q, limit, opts):
    return S.kb_zbmath(q, limit=limit)


register("zbmath", kb_zbmath, backend)
