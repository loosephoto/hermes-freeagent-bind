"""wikidata（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import threading
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

# ラベル解決のキャッシュ（`wbgetentities(labels)` の往復を省く）。
# 実測: 大規模言語モデルの検索は 検索 1,015ms → claims 634ms → **labels 349ms** で、
# 3 回目の labels で引く QID は `P31`（分類: Q7397 ソフトウェア等）や `P17`（国: Q17 日本等）の
# **検索をまたいで再登場する**ものばかりだった。ラベルは滅多に変わらないので、
# 解決済みの (言語, QID) をプロセス内に持てば 2 回目以降は往復そのものが消える。
_LABEL_CACHE: dict[tuple[str, str], str] = {}
_LABEL_LOCK = threading.Lock()
_LABEL_CACHE_MAX = 2000


def _cached_labels(lang: str, qids: list[str]) -> dict[str, str]:
    """キャッシュにあるラベルだけを返す。"""
    with _LABEL_LOCK:
        return {qid: _LABEL_CACHE[(lang, qid)] for qid in qids
                if (lang, qid) in _LABEL_CACHE}


def _store_labels(lang: str, labels: dict[str, str]) -> None:
    """解決したラベルを保存する（上限を超えたら**足さない**。古いものを追い出して新規を入れる）。"""
    with _LABEL_LOCK:
        for qid, label in labels.items():
            if (lang, qid) in _LABEL_CACHE or not label:
                continue
            if len(_LABEL_CACHE) >= _LABEL_CACHE_MAX:
                # 追い出しは「最初の 1/4」だけ（全走査を毎回しない）。決定的に保つため FIFO 相当。
                # 上限が小さいときも**最低 1 件**は消す（`3 // 4 == 0` で 1 件も消えず、
                # 上限を超えて増え続けた）。
                drop = max(1, _LABEL_CACHE_MAX // 4)
                for old in list(_LABEL_CACHE)[:drop]:
                    _LABEL_CACHE.pop(old, None)
            _LABEL_CACHE[(lang, qid)] = label


def _label_cache_reset() -> None:
    """テスト用。キャッシュを空にする（`kb_wikidata` の配線を測るときに使う）。"""
    with _LABEL_LOCK:
        _LABEL_CACHE.clear()


def kb_wikidata(query: str, lang: str = "ja", limit: int = 3) -> dict:
    """項目を検索し、代表的な主張（P31 分類 / P17 国 / P569 生年月日 など）を**ラベル解決つき**で返す。"""
    lang = S.as_str(lang, "ja")[:8]
    limit = S.as_int(limit, 3, 1, 5)
    # 値を読むときに意味が取れる代表プロパティだけを出す（全主張を並べても読めない）
    PROPS = {"P31": "分類", "P279": "上位分類", "P17": "国", "P569": "生年月日", "P570": "没年月日",
             "P106": "職業", "P361": "一部", "P159": "本社所在地", "P571": "設立", "P50": "著者"}

    def produce() -> dict:
        q = urllib.parse.urlencode({"action": "wbsearchentities", "format": "json", "language": lang,
                                    "uselang": lang, "search": query, "limit": limit})
        data, err = S.kb_json_keepalive(f"https://www.wikidata.org/w/api.php?{q}")
        if err:
            return {"source": "wikidata", "error": err}
        hits = (data or {}).get("search") or []
        ids = [h.get("id") for h in hits if h.get("id")]
        items, cites, ref_ids = [], [], set()
        entities: dict = {}
        if ids:
            q2 = urllib.parse.urlencode({"action": "wbgetentities", "format": "json",
                                         "ids": "|".join(ids[:limit]), "props": "claims"})
            e_data, _ = S.kb_json_keepalive(f"https://www.wikidata.org/w/api.php?{q2}")
            entities = ((e_data or {}).get("entities") or {})
        claims_out: dict[str, list] = {}
        for qid in ids[:limit]:
            ent = entities.get(qid) or {}
            claims = ent.get("claims") or {}
            rows = []
            for prop, label in PROPS.items():
                for claim in (claims.get(prop) or [])[:2]:
                    value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
                    if isinstance(value, dict):
                        if "id" in value:
                            rows.append({"property": prop, "label": label, "value": value["id"]})
                            ref_ids.add(value["id"])
                        elif "time" in value:
                            rows.append({"property": prop, "label": label,
                                         "value": value["time"].lstrip("+")[:10]})
                        elif "text" in value:
                            rows.append({"property": prop, "label": label, "value": value["text"]})
                    elif value is not None:
                        rows.append({"property": prop, "label": label, "value": str(value)})
            claims_out[qid] = rows
        # 参照した QID のラベルを 1 回の追加呼び出しでまとめて解決する。
        # **キャッシュ済みは投げない**（実測: 3 回目の labels が 349ms。P31/P17 の QID は
        # 検索をまたいで再登場するので、2 回目以降はこの往復が丸ごと消える）。
        wanted = sorted(ref_ids)[:40]
        labels: dict[str, str] = _cached_labels(lang, wanted) if wanted else {}
        missing = [qid for qid in wanted if qid not in labels]
        if missing:
            q3 = urllib.parse.urlencode({"action": "wbgetentities", "format": "json",
                                         "ids": "|".join(missing), "props": "labels",
                                         "languages": lang})
            l_data, _ = S.kb_json_keepalive(f"https://www.wikidata.org/w/api.php?{q3}")
            fresh: dict[str, str] = {}
            for qid, ent in ((l_data or {}).get("entities") or {}).items():
                lab = (((ent.get("labels") or {}).get(lang) or {}).get("value"))
                if lab:
                    fresh[qid] = lab
            if fresh:
                _store_labels(lang, fresh)
                labels.update(fresh)
        for hit in hits[:limit]:
            qid = hit.get("id")
            if not qid:
                continue
            rows = [dict(row, value_text=labels.get(str(row.get("value")), "")) for row in claims_out.get(qid, [])]
            url = f"https://www.wikidata.org/wiki/{qid}"
            items.append({"id": qid, "label": hit.get("label") or "",
                          "description": hit.get("description") or "",
                          "url": url, "claims": rows})
            cites.append(S._cite("wikidata", hit.get("label") or qid, url, qid=qid,
                               summary=S._plain_text(hit.get("description") or "", 300)))
        return {"source": "wikidata", "lang": lang, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return S._kb_cached(f"wd:{lang}:{query}:{limit}", produce)


EXPORTS = {"kb_wikidata": kb_wikidata, "_cached_labels": _cached_labels,
           "_store_labels": _store_labels, "_label_cache_reset": _label_cache_reset,
           "_LABEL_CACHE": _LABEL_CACHE}


def backend(q, limit, opts):
    return S.kb_wikidata(q, lang=opts.get("lang") or "ja", limit=limit)


register("wikidata", kb_wikidata, backend)
