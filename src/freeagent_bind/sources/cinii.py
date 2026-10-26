"""cinii（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def _cinii_creators(row: dict) -> list[str]:
    """dc:creator は配列で来る（文字列のときもある）。辞書キーや文字列を走査しない。"""
    value = row.get("dc:creator")
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [S.as_str(v) for v in S._kb_array(value, "dc:creator") if S.as_str(v)]


def _cinii_identifiers(row: dict) -> dict[str, str]:
    """dc:identifier の配列から種別ごとの値を取り出す（同じ種別は最初の1件）。"""
    out: dict[str, str] = {}
    for entry in S._kb_array(row.get("dc:identifier"), "dc:identifier"):
        if not isinstance(entry, dict):
            continue
        kind, value = S.as_str(entry.get("@type")), S.as_str(entry.get("@value"))
        if kind and value and kind not in out:
            out[kind] = value
    return out


def kb_cinii(query: str, limit: int = 5) -> dict:
    """CiNii Research の日本語文献メタデータ（書誌のみ・抄録は返らない）。利用者自身の appid が必要。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "cinii", "error": "query は必須です"}
    if not S.CINII_APPID:
        return {"source": "cinii",
                "error": "appid 未設定: CiNii Research は利用登録（アプリケーションID）が必要です。"
                         "https://support.nii.ac.jp/ja/cinii/api/developer で取得し、環境変数 "
                         "FREEAGENT_CINII_APPID に設定してください（利用目的に当たるかの判断は利用者。"
                         "設定するまで HTTP は出しません）"}

    def produce() -> dict:
        params = {"q": S._literal_search(query), "format": "json", "count": limit, "appid": S.CINII_APPID}
        data, err = S._kb_new_json("https://cir.nii.ac.jp/opensearch/all?"
                                 + urllib.parse.urlencode(params), 2.0)
        if err:
            return {"source": "cinii", "error": err}
        rows = S._kb_array(S._kb_object(data, "CiNii応答").get("items"), "items")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("CiNii item はオブジェクトではありません")
            title = S._plain_text(S.as_str(row.get("title")), 300)
            page = S.as_str(S._kb_object(row.get("link"), "link").get("@id")) or S.as_str(row.get("@id"))
            if not title or not page:
                continue
            ident = _cinii_identifiers(row)
            doi = ident.get("cir:DOI", "")
            doc_type = S.as_str(row.get("dc:type"))
            items.append({"title": title, "url": S._kb_http_url("https://doi.org/" + doi if doi else page),
                          "cir_uri": S._kb_http_url(page), "doi": doi,
                          "year": S.as_str(row.get("prism:publicationDate"))[:4],
                          "container": S._plain_text(S.as_str(row.get("prism:publicationName")), 200),
                          "publisher": S._plain_text(S.as_str(row.get("dc:publisher")), 200),
                          "authors": _cinii_creators(row)[:8],
                          "publication_types": [doc_type] if doc_type else [],
                          "identifier": ident.get("cir:NAID") or ident.get("cir:NCID")
                                        or ident.get("cir:ISBN", ""),
                          "metadata_only": True})
        return S._kb_paper_result("cinii", items,
                                attribution="データ提供: CiNii Research（国立情報学研究所） "
                                            "https://cir.nii.ac.jp/")

    return S._kb_new_cached(f"cinii:{query}:{limit}", "cinii", produce)



EXPORTS = {"_cinii_creators": _cinii_creators, "_cinii_identifiers": _cinii_identifiers, "kb_cinii": kb_cinii}


def backend(q, limit, opts):
    return S.kb_cinii(q, limit)


register("cinii", kb_cinii, backend)
