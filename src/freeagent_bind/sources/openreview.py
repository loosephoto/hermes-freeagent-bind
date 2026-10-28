"""openreview（OpenReview API v2・明示指定のみ・v0.29.0）.

会議論文の検索。差分は「会議の採択状況（Poster / Spotlight / Oral / Workshop /
**Withdrawn** / **Desk Rejected** / 未査読の Submission）」が `venue` に出ることで、
arXiv（プレプリント）・OpenAlex・Crossref（DOI）では取れない。

実測（2026-10-28・本機）:
  * `/notes/search` は 5/5 が 200・0.21〜1.78 秒（暖機後 0.21 秒）。
  * 引用に要る項目の充填率 10/10（title / authors / venue / forum / pdate、abstract 200 字超 10/10）。
  * 空 term は 400 `ValidationError`、未知語・記号のみ・**日本語は 0 件**（英語の語が要る）。
  * **一覧経路 `/notes?content.venueid=` は 403 `ChallengeRequiredError`**（チャレンジ）。`/notes/search` は
    影響を受けないが、将来同じ扱いになる可能性があるので 403 は「自動アクセス拒否」として明示エラーにする。
  * レート制限の**公表値が無い**（openreview/openreview#459 が未解決）。npm と同じ「公表が無いので自制」型で
    既定 1 req/s に間隔を置く（`FREEAGENT_OPENREVIEW_INTERVAL` で変更可）。
  * robots.txt は `Disallow: /*?*email=` のみ（検索は許可）。ToS は API を
    「data upload, search, and download のプログラム的インターフェース」と定義している。
"""
import re
import time
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

OPENREVIEW_INTERVAL = max(0.0, S._env_float("FREEAGENT_OPENREVIEW_INTERVAL", 1.0))
OPENREVIEW_BUDGET = "api2.openreview.net/notes-search"
OPENREVIEW_API = "https://api2.openreview.net/notes/search"
OPENREVIEW_ATTRIBUTION = "データ提供: OpenReview https://openreview.net/（各論文のライセンスに従う）"

# CJK（かな・漢字・ハングル）だけの問い。索引は英語なので 0 件になる（実測）。
_CJK_ONLY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")

# venueid の末尾で査読状態を機械可読にする（venue 文字列は会議ごとに揺れる）。
_VENUE_KIND = (
    ("/Withdrawn_Submission", "withdrawn"),
    ("/Desk_Rejected_Submission", "desk_rejected"),
    ("/Rejected_Submission", "rejected"),
    ("/Submission", "under_review"),
)


def _ov_value(content: dict, key: str):
    """v2 の content は `{value: …}` で包まれる（v1 の素の値も受ける）。"""
    value = (content or {}).get(key)
    return value.get("value") if isinstance(value, dict) else value


def _ov_kind(venueid: str, venue: str) -> str:
    for suffix, kind in _VENUE_KIND:
        if venueid.endswith(suffix):
            return kind
    lowered = (venue or "").lower()
    if "withdraw" in lowered:
        return "withdrawn"
    if "desk reject" in lowered:
        return "desk_rejected"
    return "published" if venueid else "unknown"


def kb_openreview(query: str, limit: int = 5) -> dict:
    """OpenReview（ICLR / NeurIPS / ICML などの会議論文と DBLP 収録書誌）を検索する。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "openreview", "error": "query は必須です"}
    if _CJK_ONLY_RE.search(query) and not re.search(r"[A-Za-z]", query):
        # 締め切り（8 秒）と 1 req/s の枠を空振りで食わない（arxiv と同じ型）。
        return {"source": "openreview", "items": [], "citations": [],
                "error": "openreview は英語の語で引く必要があります（この問いには英字がありません）。"
                         "日本語で調べるなら wikipedia / wikidata / qiita を sources に指定してください"}

    def produce() -> dict:
        url = OPENREVIEW_API + "?" + urllib.parse.urlencode(
            {"term": " ".join(query.split()), "limit": limit, "source": "forum"})
        data, err = S._kb_new_json(url, OPENREVIEW_INTERVAL, budget_key=OPENREVIEW_BUDGET)
        if err:
            if "400" in err:
                return {"source": "openreview",
                        "error": "OpenReview は検索語を 1 文字以上要求します（400 ValidationError）"}
            if "403" in err:
                return {"source": "openreview",
                        "error": "OpenReview が自動アクセスにチャレンジを要求しました（403 ChallengeRequired）。"
                                 "時間を置くか、openreview 以外の学術ソースを使ってください"}
            return {"source": "openreview", "error": err}
        notes = S._kb_array(data.get("notes") if isinstance(data, dict) else None, "notes")
        items = []
        for note in notes[:limit]:
            if not isinstance(note, dict):
                continue
            content = note.get("content") or {}
            title = " ".join((S.as_str(_ov_value(content, "title")) or "").split())
            if not title:
                continue
            forum = S.as_str(note.get("forum")) or S.as_str(note.get("id"))
            if not forum:
                continue
            summary = S.truncate(S._plain_text(S.as_str(_ov_value(content, "abstract")) or ""), 1200)
            venue = " ".join((S.as_str(_ov_value(content, "venue")) or "").split())
            venueid = S.as_str(_ov_value(content, "venueid")) or ""
            authors = [S.as_str(a) for a in S._kb_array(_ov_value(content, "authors"), "authors")
                       if S.as_str(a)]
            ms = note.get("pdate") or note.get("cdate") or 0
            year = time.gmtime(ms / 1000).tm_year if isinstance(ms, (int, float)) and ms else ""
            item = {
                "title": S.truncate(title, 300),
                "url": "https://openreview.net/forum?id=" + urllib.parse.quote(forum, safe=""),
                "summary": summary,
                "year": year,
                "container": venue or venueid or "OpenReview",
                "authors": authors[:8],
                "kind": _ov_kind(venueid, venue),
                "identifier": forum,
            }
            items.append(item)
        return S._kb_generic_result("openreview", items, OPENREVIEW_ATTRIBUTION)

    return S._kb_new_cached(f"openreview:{query}:{limit}", "openreview", produce)


EXPORTS = {"OPENREVIEW_INTERVAL": OPENREVIEW_INTERVAL, "OPENREVIEW_BUDGET": OPENREVIEW_BUDGET,
           "_ov_value": _ov_value, "_ov_kind": _ov_kind, "kb_openreview": kb_openreview}


def backend(q, limit, opts):
    return S.kb_openreview(q, limit=limit)


register("openreview", kb_openreview, backend)
