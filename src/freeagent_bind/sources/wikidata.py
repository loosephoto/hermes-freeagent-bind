"""wikidata（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

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
        # 参照した QID のラベルを 1 回の追加呼び出しでまとめて解決する
        labels: dict[str, str] = {}
        if ref_ids:
            q3 = urllib.parse.urlencode({"action": "wbgetentities", "format": "json",
                                         "ids": "|".join(sorted(ref_ids)[:40]), "props": "labels",
                                         "languages": lang})
            l_data, _ = S.kb_json_keepalive(f"https://www.wikidata.org/w/api.php?{q3}")
            for qid, ent in ((l_data or {}).get("entities") or {}).items():
                lab = (((ent.get("labels") or {}).get(lang) or {}).get("value"))
                if lab:
                    labels[qid] = lab
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


EXPORTS = {"kb_wikidata": kb_wikidata}


def backend(q, limit, opts):
    return S.kb_wikidata(q, lang=opts.get("lang") or "ja", limit=limit)


register("wikidata", kb_wikidata, backend)
