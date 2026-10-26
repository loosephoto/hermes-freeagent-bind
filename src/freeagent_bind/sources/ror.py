"""ror（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def _ror_id_valid(value: str) -> bool:
    """Crockford Base32 + ROR公式のMOD 97-10。誤ったIDは修復しない。"""
    match = re.fullmatch(r"https://ror[.]org/0([0-9a-hjkmnp-tv-z]{6})([0-9]{2})", value)
    if not match:
        return False
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    number = 0
    for char in match.group(1):
        number = number * 32 + alphabet.index(char)
    return int(match.group(2)) == 98 - ((number * 100) % 97)


def kb_ror(query: str, limit: int = 3) -> dict:
    """論文検索ではなく機関候補検索。登録された属性だけを根拠文にする。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 3, 1, 10)
    if not query:
        return {"source": "ror", "error": "query は必須です"}
    def produce():
        data, err = S._kb_new_json("https://api.ror.org/v2/organizations?" + urllib.parse.urlencode({"query": query}), 6.1)
        if err:
            return {"source": "ror", "error": err}
        rows = S._kb_object(data, "ROR応答").get("items")
        if not isinstance(rows, list):
            raise ValueError("ROR items は配列ではありません")
        items, cites = [], []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("ROR record はオブジェクトではありません")
            names = S._kb_array(row.get("names"), "names")
            if any(not isinstance(n, dict) for n in names):
                raise ValueError("ROR names の要素はオブジェクトではありません")
            title = next((S._open_metadata_text(n.get("value"), 300) for n in names
                          if "ror_display" in S._kb_array(n.get("types"), "names.types")), "")
            link = S.as_str(row.get("id"))
            if not title or not _ror_id_valid(link):
                raise ValueError("ROR の表示名またはIDの形式が不正です")
            types = [S.as_str(v) for v in S._kb_array(row.get("types"), "types") if S.as_str(v)]
            locations = [S._kb_object(l, "location") for l in S._kb_array(row.get("locations"), "locations")]
            countries, cities = [], []
            for location in locations:
                geo = S._kb_object(location.get("geonames_details"), "geonames_details")
                countries.append(S.as_str(geo.get("country_name")))
                cities.append(S.as_str(geo.get("name")))
            countries = list(dict.fromkeys(c for c in countries if c))
            cities = list(dict.fromkeys(c for c in cities if c))
            established = row.get("established")
            if type(established) is not int or not 1 <= established <= 9999:
                established = None
            status = S.as_str(row.get("status"))
            facts = []
            for label, value in (("機関種別", ", ".join(types)), ("国", ", ".join(countries)),
                                 ("都市", ", ".join(cities)), ("状態", status)):
                if value:
                    facts.append(label + ": " + value)
            if established is not None:
                facts.append("設立年（出版年ではない）: " + str(established))
            summary = S._open_metadata_text("; ".join(facts))
            sites = [S.as_str(S._kb_object(v, "link").get("value")) for v in S._kb_array(row.get("links"), "links")
                     if S._kb_object(v, "link").get("type") == "website"]
            for site in sites:
                S._kb_http_url(site)
            item = {"title": title, "url": link, "year": "", "summary": summary,
                    "name_variants": [S._open_metadata_text(n.get("value"), 300) for n in names],
                    "organization_types": types, "countries": countries, "cities": cities,
                    "established": established, "status": status, "websites": sites,
                    "license": "CC0-1.0", "summary_kind": "structured_metadata", "metadata_only": not bool(summary)}
            cite = S._cite("ror", title, link, attribution="データ提供: ROR（CC0・研究機関候補） https://ror.org/")
            cite.update({k: v for k, v in item.items() if k not in ("name_variants", "websites")})
            items.append(item)
            cites.append(cite)
        return {"source": "ror", "items": items, "citations": cites, "search_candidates": True,
                "attribution": "データ提供: ROR（CC0・研究機関候補） https://ror.org/",
                "error": "" if items else "該当なし"}
    return S._kb_new_cached(f"ror:{query}:{limit}", "ror", produce)



EXPORTS = {"_ror_id_valid": _ror_id_valid, "kb_ror": kb_ror}


def backend(q, limit, opts):
    return S.kb_ror(q, limit)


register("ror", kb_ror, backend)
