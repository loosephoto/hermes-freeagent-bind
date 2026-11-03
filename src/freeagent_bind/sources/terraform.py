"""terraform（Terraform Registry・明示指定のみ・v0.39.0）.

差分は「**IaC モジュールをキーワードで引ける**」こと。libraries.io は 19+ の登録簿を横断するが
Terraform モジュールは対象外で、`terraform-aws-modules/vpc/aws` のような**公開モジュールの実装**は
引けなかった。

実測（2026-11-03・本機）:
  * `/v1/modules/search?q=` は 200・0.34〜0.53 秒。`q` は公式 API 文書
    （developer.hashicorp.com/terraform/registry/api-docs）に**検索文字列として明記**されている。
  * 引用に要る項目（namespace / name / provider / description / downloads / source）は全件充填。
  * robots.txt は `User-agent: *` の Disallow が空（全許可）。**レート制限の公表値は無い**ので
    npm と同じ「公表が無いので自制」型で既定 1 req/s（`FREEAGENT_TERRAFORM_INTERVAL`）。
  * 説明文は**モジュール公開者の自己申告**（審査結果ではない）ので `summary_kind=registry_description`。
"""
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

TERRAFORM_INTERVAL = max(0.0, S._env_float("FREEAGENT_TERRAFORM_INTERVAL", 1.0))
TERRAFORM_BUDGET = "registry.terraform.io/modules-search"
TERRAFORM_API = "https://registry.terraform.io/v1/modules/search"
TERRAFORM_ATTRIBUTION = ("データ提供: Terraform Registry（HashiCorp）https://registry.terraform.io/ "
                         "（説明文はモジュール公開者の自己申告・各モジュールのライセンスに従う）")

_CJK_ONLY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def kb_terraform(query: str, limit: int = 5) -> dict:
    """Terraform Registry の公開モジュールを検索する。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "terraform", "error": "query は必須です"}
    if _CJK_ONLY_RE.search(query) and not re.search(r"[A-Za-z]", query):
        return {"source": "terraform", "items": [], "citations": [],
                "error": "terraform のモジュール名・説明は英語です（この問いには英字がありません）。"
                         "英語の語（aws vpc 等）で引いてください"}

    def produce() -> dict:
        url = TERRAFORM_API + "?" + urllib.parse.urlencode(
            {"q": " ".join(query.split()), "limit": limit})
        data, err = S._kb_new_json(url, TERRAFORM_INTERVAL, budget_key=TERRAFORM_BUDGET)
        if err:
            return {"source": "terraform", "error": err}
        rows = S._kb_array(S._kb_object(data, "Terraform応答").get("modules"), "modules")
        items = []
        described = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                continue
            namespace = S.as_str(row.get("namespace"))
            name = S.as_str(row.get("name"))
            provider = S.as_str(row.get("provider"))
            if not (namespace and name and provider):
                continue
            facts = [S._plain_text(S.as_str(row.get("description")), 400)]
            description = S.as_str(row.get("description"))
            downloads = row.get("downloads")
            if type(downloads) is int:
                facts.append(f"ダウンロード {downloads}")
            items.append({"title": f"{namespace}/{name}/{provider}",
                          "url": "https://registry.terraform.io/modules/" + "/".join(
                              urllib.parse.quote(part, safe="") for part in (namespace, name, provider)),
                          "summary": S._plain_text("／".join(f for f in facts if f), 600),
                          "identifier": S.as_str(row.get("id")), "kind": "terraform-module",
                          "summary_kind": "registry_description",
                          "year": S.as_str(row.get("published_at"))[:4],
                          "extra": {"source": S.as_str(row.get("source")),
                                    "version": S.as_str(row.get("version"))}})
            described.append(bool(description))
        result = S._kb_generic_result("terraform", items, TERRAFORM_ATTRIBUTION)
        # 説明文が無い行はダウンロード数だけ＝**メタデータ**。本文根拠に使わせない。
        for has_text, cite in zip(described, result["citations"]):
            if not has_text:
                cite["metadata_only"] = True
        return result

    return S._kb_new_cached(f"terraform:{query}:{limit}", "terraform", produce)


EXPORTS = {"TERRAFORM_INTERVAL": TERRAFORM_INTERVAL, "TERRAFORM_BUDGET": TERRAFORM_BUDGET,
           "TERRAFORM_API": TERRAFORM_API, "kb_terraform": kb_terraform}


def backend(q, limit, opts):
    return S.kb_terraform(q, limit=limit)


register("terraform", kb_terraform, backend)
