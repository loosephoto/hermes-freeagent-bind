"""nvd（NIST National Vulnerability Database・明示指定のみ・v0.39.0）.

差分は「**キーワードで CVE を引ける**」こと。既存の osv は**パッケージ名か CVE-/GHSA- 等の ID**しか
受けないので、「その脆弱性クラス（sql injection / buffer overflow）に関係する CVE」は引けなかった。

実測（2026-11-03・本機）:
  * `/rest/json/cves/2.0?keywordSearch=` は 200・0.62 / 0.81 / 0.86 秒（初回だけ 5.12 秒）。
  * 引用に要る項目は説明文（英語の CVE 記述）・CVSS・参照件数が揃う（10/10）。
  * **無認証は 5 req/30s**（NVD の公表値）。「6 秒間隔で sleep せよ」と明記されているので既定 6.0 秒。
    API キー（`FREEAGENT_NVD_API_KEY`・ヘッダ `apiKey`）があると 50 req/30s → 既定 0.6 秒。
  * robots.txt は 404（制限なし）。ToS は米国政府著作物（17 USC）でパブリックドメインだが、
    「This product uses the NVD API but is not endorsed or certified by the NVD」の表示を求める。
  * 日本語だけの問いは索引が英語なので 0 件（枠を空振りで食わないよう HTTP を出さない）。
"""
import os
import re
import urllib.parse

from .. import server as S
from . import register  # noqa: E402  パッケージ初期化中に登録する

NVD_API_KEY = os.environ.get("FREEAGENT_NVD_API_KEY", "").strip()
# 無認証 5 req/30s（＝6 秒）、キーあり 50 req/30s（＝0.6 秒）。NVD は「6 秒 sleep」を明記している。
NVD_INTERVAL = max(0.0, S._env_float("FREEAGENT_NVD_INTERVAL", 0.6 if NVD_API_KEY else 6.0))
NVD_BUDGET = "services.nvd.nist.gov/cves"
NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
NVD_ATTRIBUTION = ("データ提供: NIST National Vulnerability Database（米国政府著作物・パブリックドメイン）。"
                   "This product uses the NVD API but is not endorsed or certified by the NVD. "
                   "https://nvd.nist.gov/")

_CJK_ONLY_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]")


def _nvd_description(cve: dict) -> str:
    """英語の説明文だけを使う（他の言語が先頭に来ることがある）。"""
    for entry in S._kb_array(cve.get("descriptions"), "descriptions"):
        if isinstance(entry, dict) and S.as_str(entry.get("lang")) == "en":
            return S.as_str(entry.get("value"))
    return ""


def _nvd_cvss(metrics: dict) -> tuple[str, str]:
    """CVSS は v3.1 → v3.0 → v2 の順に採る（同じ CVE でも版によって有無が違う）。"""
    for key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        for metric in S._kb_array(metrics.get(key), key):
            if not isinstance(metric, dict):
                continue
            raw_data = metric.get("cvssData") if isinstance(metric, dict) else None
            data = raw_data if isinstance(raw_data, dict) else {}
            raw_score = data.get("baseScore")
            # NVD の baseScore は**数値**で来る（`as_str` は文字列以外を落とすので型で分ける）。
            score = str(raw_score) if isinstance(raw_score, (int, float)) else S.as_str(raw_score)
            if score:
                severity = S.as_str(data.get("baseSeverity")) or S.as_str(
                    metric.get("baseSeverity") if isinstance(metric, dict) else "")
                return score, severity
    return "", ""


def kb_nvd(query: str, limit: int = 5) -> dict:
    """NVD の CVE を**キーワード**で検索する（説明文・CVSS・参照つき）。"""
    query = S.as_str(query)
    limit = S.as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "nvd", "error": "query は必須です"}
    if _CJK_ONLY_RE.search(query) and not re.search(r"[A-Za-z]", query):
        return {"source": "nvd", "items": [], "citations": [],
                "error": "nvd の索引は英語です（この問いには英字がありません）。"
                         "脆弱性を調べるなら英語の語（sql injection 等）か CVE 番号で引いてください"}

    def produce() -> dict:
        params = {"keywordSearch": " ".join(query.split()), "resultsPerPage": limit}
        url = NVD_API + "?" + urllib.parse.urlencode(params)
        headers = {"apiKey": NVD_API_KEY} if NVD_API_KEY else None
        data, err = S._kb_new_json(url, NVD_INTERVAL, budget_key=NVD_BUDGET, extra_headers=headers)
        if err:
            if "403" in err or "404" in err:
                return {"source": "nvd",
                        "error": "NVD がこのリクエストを受け付けませんでした（" + err[:80] + "）。"
                                 "キーを設定する場合は FREEAGENT_NVD_API_KEY を使ってください"}
            return {"source": "nvd", "error": err}
        cves = S._kb_array(S._kb_object(data, "NVD応答").get("vulnerabilities"), "vulnerabilities")
        items = []
        described = []
        for row in cves[:limit]:
            cve = row.get("cve") if isinstance(row, dict) else None
            if not isinstance(cve, dict):
                continue
            cid = S.as_str(cve.get("id"))
            if not cid:
                continue
            description = _nvd_description(cve)
            metrics = cve.get("metrics") if isinstance(cve.get("metrics"), dict) else {}
            score, severity = _nvd_cvss(metrics)
            references = S._kb_array(cve.get("references"), "references")
            facts = [S._plain_text(description, 500)]
            if score:
                facts.append("CVSS " + score + (f" ({severity})" if severity else ""))
            if references:
                facts.append(f"参照 {len(references)} 件")
            items.append({"title": cid,
                          "url": "https://nvd.nist.gov/vuln/detail/" + urllib.parse.quote(cid, safe=""),
                          "summary": S._plain_text("／".join(f for f in facts if f), 600),
                          "identifier": cid, "kind": "vulnerability",
                          "severity": severity, "year": S.as_str(cve.get("published"))[:4],
                          "summary_kind": "structured_record",
                          "extra": {"cvss": score, "published": S.as_str(cve.get("published")),
                                    "references": [S.as_str(r.get("url")) for r in references[:3]
                                                   if isinstance(r, dict) and S.as_str(r.get("url"))]}})
            described.append(bool(description))
        result = S._kb_generic_result("nvd", items, NVD_ATTRIBUTION)
        # 説明文が無い行は CVSS・参照件数だけ＝**構造化メタデータ**。本文根拠に使わせない（規約 28）。
        for has_text, cite in zip(described, result["citations"]):
            if not has_text:
                cite["metadata_only"] = True
        return result

    return S._kb_new_cached(f"nvd:{query}:{limit}", "nvd", produce)


EXPORTS = {"NVD_INTERVAL": NVD_INTERVAL, "NVD_BUDGET": NVD_BUDGET, "NVD_API": NVD_API,
           "_nvd_description": _nvd_description, "_nvd_cvss": _nvd_cvss, "kb_nvd": kb_nvd}


def backend(q, limit, opts):
    return S.kb_nvd(q, limit=limit)


register("nvd", kb_nvd, backend)
