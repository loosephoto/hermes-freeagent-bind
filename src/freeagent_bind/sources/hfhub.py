"""hfhub（server.py §5 から分割・v0.15.0）.

共有ヘルパーは `S.` 経由で呼ぶ（`patch.object(server, ...)` を効かせるため）。
"""
import json
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

def kb_hfhub(query: str, limit: int = 5) -> dict:
    """Hugging Face Hub のモデル検索。モデルのライセンス・品質はリポジトリごとに異なる。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "hfhub", "error": "query は必須です"}
    attribution = "データ提供: Hugging Face Hub https://huggingface.co/（各モデルの条件に従う）"

    def produce() -> dict:
        params = {"search": query, "limit": limit, "sort": "downloads", "direction": -1,
                  "full": "false"}
        data, err = S._kb_new_json("https://huggingface.co/api/models?"
                                 + urllib.parse.urlencode(params), 1.0)
        if err:
            return {"source": "hfhub", "error": err}
        rows = S._kb_array(data, "models")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Hugging Face の結果はオブジェクトではありません")
            model_id = S.as_str(row.get("id")) or S.as_str(row.get("modelId"))
            if not model_id:
                continue
            facts = [f"モデル {model_id}"]
            if S.as_str(row.get("pipeline_tag")):
                facts.append("タスク " + S.as_str(row.get("pipeline_tag")))
            if S.as_str(row.get("library_name")):
                facts.append("ライブラリ " + S.as_str(row.get("library_name")))
            downloads, likes = row.get("downloads"), row.get("likes")
            if type(downloads) is int:
                facts.append(f"ダウンロード {downloads}")
            if type(likes) is int:
                facts.append(f"likes {likes}")
            tags = [S.as_str(t) for t in S._kb_array(row.get("tags"), "tags") if S.as_str(t)]
            items.append({"title": model_id,
                          "url": "https://huggingface.co/" + urllib.parse.quote(model_id, safe="/"),
                          "summary": "／".join(facts), "identifier": model_id,
                          "summary_kind": "structured_record", "kind": "model",
                          "year": S.as_str(row.get("createdAt"))[:4],
                          "authors": tags[:8],
                          "extra": {"license": next((t.split(":", 1)[1] for t in tags
                                                     if t.startswith("license:")), "")}})
        return S._kb_generic_result("hfhub", items, attribution)

    return S._kb_new_cached(f"hfhub:{query}:{limit}", "hfhub", produce)



EXPORTS = {"kb_hfhub": kb_hfhub}


def backend(q, limit, opts):
    return S.kb_hfhub(q, limit)


register("hfhub", kb_hfhub, backend)
