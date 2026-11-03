"""第12段階ソース（nvd / terraform / zbmath）のオフライン回帰.

HTTP のみ模擬する。固定すること:
  * **明示指定のみ**（`DEFAULT_SOURCES` は 6 のまま）で、両ツールの `sources` 説明に出る。
  * **nvd はキーワード検索**（`keywordSearch`）で CVE の説明文を引用にする。CVSS は v3.1 → v3.0 → v2。
    無認証 5 req/30s なので既定 6 秒間隔、キーがあれば 0.6 秒。
  * **terraform は `/v1/modules/search?q=`**（公式文書に明記）。説明は公開者の自己申告。
  * **zbmath は規約同意（`FREEAGENT_ZBMATH_ACK`）が無ければ HTTP を出さない**。レビュー本文が
    無い行は `metadata_only`（"unavailable due to conflicting licenses" を本文根拠にしない）。
  * **CJK だけの問いは 3 本とも HTTP を出さない**（索引が英語）。
"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S
from freeagent_bind.sources import nvd as NVD_MOD
from freeagent_bind.sources import zbmath as ZB_MOD  # noqa: F401  (EXPORTS の参照確認用)

STAGE12 = ("nvd", "terraform", "zbmath")


def nvd_row(cid="CVE-2021-44228", description="JNDI features do not protect against attacker controlled LDAP.",
            metrics=None, published="2021-12-10T10:15:09.143", refs=3):
    return {"cve": {"id": cid, "published": published,
                    "descriptions": [{"lang": "es", "value": "texto"}, {"lang": "en", "value": description}],
                    "metrics": metrics if metrics is not None else {
                        "cvssMetricV31": [{"cvssData": {"baseScore": 10.0, "baseSeverity": "CRITICAL"}}]},
                    "references": [{"url": f"https://example.test/{i}"} for i in range(refs)]}}


def tf_row(namespace="terraform-aws-modules", name="vpc", provider="aws", description="AWS VPC module",
           downloads=1234, published="2026-10-07T21:11:59.223384Z"):
    return {"id": f"{namespace}/{name}/{provider}/1.0.0", "namespace": namespace, "name": name,
            "provider": provider, "description": description, "downloads": downloads,
            "published_at": published, "source": "https://github.com/example/repo", "version": "1.0.0"}


def zb_row(code="1409.68302", title="Semantic spaces", year="2016", review=None,
           url="https://zbmath.org/6751902", keywords=("semantics",), msc=("68T50",)):
    contribs = [{"text": review}] if review is not None else [
        {"text": "zbMATH Open Web Interface contents unavailable due to conflicting licenses."}]
    return {"identifier": code, "title": {"title": title, "subtitle": None, "addition": None},
            "year": year, "zbmath_url": url, "keywords": list(keywords),
            "source": {"source": "J. Symb. Comput. 74, 459-477 (2016)."},
            "editorial_contributions": contribs,
            "msc": [{"code": c} for c in msc],
            "links": [{"identifier": "10.1007/s11786-016-0278-9", "type": "doi",
                       "url": "https://doi.org/10.1007/s11786-016-0278-9"}],
            "contributors": {"authors": [{"name": "A One"}, {"name": "B Two"}]}}


class TestStage12Registry(unittest.TestCase):
    def test_sources_are_opt_in_and_advertised(self):
        for name in STAGE12:
            self.assertIn(name, S.SOURCES)
            self.assertIn(name, S.KB_BACKENDS)
            self.assertNotIn(name, S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            for name in STAGE12:
                self.assertIn(name, desc, tool)


class TestNvd(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()

    def test_interval_follows_published_rate(self):
        """無認証 5 req/30s（6 秒）／キーあり 50 req/30s（0.6 秒）。"""
        self.assertAlmostEqual(S.NVD_INTERVAL, 0.6 if NVD_MOD.NVD_API_KEY else 6.0, places=3)
        self.assertEqual(S.NVD_BUDGET, "services.nvd.nist.gov/cves")

    def test_empty_and_cjk_queries_do_not_touch_the_network(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_nvd("", 3).get("error"))
            self.assertTrue(S.kb_nvd("強化学習", 3).get("error"))
            self.assertTrue(S.KB_BACKENDS["nvd"]("量子力学", 3, {}).get("error"))
            http.assert_not_called()

    def test_mixed_language_query_is_sent(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"vulnerabilities": []}, "")) as http:
            S.kb_nvd("Log4j の 脆弱性", 3)
            http.assert_called_once()

    def test_query_parameters_and_budget(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"vulnerabilities": []}, "")) as http:
            S.kb_nvd("sql   injection", 4)
        parts = urlsplit(http.call_args[0][0])
        qs = parse_qs(parts.query)
        self.assertEqual(parts.netloc, "services.nvd.nist.gov")
        self.assertEqual(parts.path, "/rest/json/cves/2.0")
        self.assertEqual(qs["keywordSearch"], ["sql injection"])
        self.assertEqual(qs["resultsPerPage"], ["4"])
        self.assertEqual(http.call_args[0][1], S.NVD_INTERVAL)
        self.assertEqual(http.call_args[1].get("budget_key"), S.NVD_BUDGET)

    def test_api_key_goes_into_a_header(self):
        with mock.patch.object(NVD_MOD, "NVD_API_KEY", "secret-key"):
            with mock.patch.object(S, "_kb_new_json", return_value=({"vulnerabilities": []}, "")) as http:
                S.kb_nvd("log4j", 3)
        self.assertEqual(http.call_args[1].get("extra_headers"), {"apiKey": "secret-key"})

    def test_citation_shape(self):
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"vulnerabilities": [nvd_row()]}, "")):
            res = S.kb_nvd("log4j", 3)
        self.assertEqual(res["error"], "")
        cite = res["citations"][0]
        for key in ("source", "title", "url", "summary", "year", "severity", "kind"):
            self.assertIn(key, cite)
        self.assertEqual(cite["source"], "nvd")
        self.assertEqual(cite["title"], "CVE-2021-44228")
        self.assertEqual(cite["url"], "https://nvd.nist.gov/vuln/detail/CVE-2021-44228")
        self.assertEqual(cite["year"], "2021")
        self.assertEqual(cite["severity"], "CRITICAL")
        self.assertEqual(cite["summary_kind"], "structured_record")
        self.assertFalse(cite["metadata_only"])
        self.assertIn("JNDI", cite["summary"])
        self.assertIn("CVSS 10.0", cite["summary"])
        S._kb_http_url(cite["url"])

    def test_english_description_wins_over_other_languages(self):
        row = nvd_row()
        row["cve"]["descriptions"] = [{"lang": "es", "value": "texto"}, {"lang": "en", "value": "English"}]
        with mock.patch.object(S, "_kb_new_json", return_value=({"vulnerabilities": [row]}, "")):
            cite = S.kb_nvd("x", 1)["citations"][0]
        self.assertIn("English", cite["summary"])
        self.assertNotIn("texto", cite["summary"])

    def test_missing_description_is_metadata_only(self):
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"vulnerabilities": [nvd_row(description="")]}, "")):
            cite = S.kb_nvd("x", 1)["citations"][0]
        self.assertTrue(cite["metadata_only"])

    def test_cvss_prefers_newest_metric(self):
        metrics = {"cvssMetricV2": [{"cvssData": {"baseScore": 5.0}, "baseSeverity": "MEDIUM"}],
                   "cvssMetricV31": [{"cvssData": {"baseScore": 9.8, "baseSeverity": "CRITICAL"}}]}
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"vulnerabilities": [nvd_row(metrics=metrics)]}, "")):
            cite = S.kb_nvd("x", 1)["citations"][0]
        self.assertEqual(cite["severity"], "CRITICAL")
        self.assertIn("CVSS 9.8", cite["summary"])

    def test_rows_without_id_are_skipped_and_errors_pass_through(self):
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"vulnerabilities": [{"cve": {}}, "junk"]}, "")):
            self.assertEqual(S.kb_nvd("x", 3)["error"], "該当なし")
        with mock.patch.object(S, "_kb_new_json", return_value=(None, "HTTP 503: unavailable")):
            self.assertIn("503", S.kb_nvd("x", 3)["error"])

    def test_limit_is_clamped_in_the_query(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"vulnerabilities": []}, "")) as http:
            S.kb_nvd("x", 999)
        self.assertEqual(parse_qs(urlsplit(http.call_args[0][0]).query)["resultsPerPage"], ["20"])


class TestTerraform(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()

    def test_interval_is_self_imposed(self):
        self.assertEqual(S.TERRAFORM_INTERVAL, 1.0)
        self.assertEqual(S.TERRAFORM_BUDGET, "registry.terraform.io/modules-search")

    def test_empty_and_cjk_queries_do_not_touch_the_network(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_terraform("", 3).get("error"))
            self.assertTrue(S.kb_terraform("仮想ネットワーク", 3).get("error"))
            http.assert_not_called()

    def test_query_parameters_and_budget(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"modules": []}, "")) as http:
            S.kb_terraform("aws  vpc", 2)
        parts = urlsplit(http.call_args[0][0])
        qs = parse_qs(parts.query)
        self.assertEqual(parts.netloc, "registry.terraform.io")
        self.assertEqual(parts.path, "/v1/modules/search")
        self.assertEqual(qs["q"], ["aws vpc"])
        self.assertEqual(qs["limit"], ["2"])
        self.assertEqual(http.call_args[0][1], S.TERRAFORM_INTERVAL)
        self.assertEqual(http.call_args[1].get("budget_key"), S.TERRAFORM_BUDGET)

    def test_citation_shape(self):
        with mock.patch.object(S, "_kb_new_json", return_value=({"modules": [tf_row()]}, "")):
            res = S.kb_terraform("aws vpc", 1)
        cite = res["citations"][0]
        self.assertEqual(cite["source"], "terraform")
        self.assertEqual(cite["title"], "terraform-aws-modules/vpc/aws")
        self.assertEqual(cite["url"], "https://registry.terraform.io/modules/terraform-aws-modules/vpc/aws")
        self.assertEqual(cite["summary_kind"], "registry_description")
        self.assertEqual(cite["year"], "2026")
        self.assertIn("ダウンロード 1234", cite["summary"])
        self.assertEqual(cite["extra"]["version"], "1.0.0")
        S._kb_http_url(cite["url"])

    def test_missing_description_is_metadata_only(self):
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"modules": [tf_row(description="")]}, "")):
            cite = S.kb_terraform("x", 1)["citations"][0]
        self.assertTrue(cite["metadata_only"])

    def test_incomplete_rows_are_skipped(self):
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"modules": [tf_row(namespace=""), "junk"]}, "")):
            self.assertEqual(S.kb_terraform("x", 3)["error"], "該当なし")


class TestZbmath(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        env = mock.patch.dict(os.environ, {})
        env.start()
        os.environ.pop("FREEAGENT_ZBMATH_ACK", None)
        self.addCleanup(env.stop)

    def _ack(self):
        os.environ["FREEAGENT_ZBMATH_ACK"] = "1"

    def test_requires_the_terms_acknowledgement(self):
        with mock.patch.object(S, "_kb_new_json") as http:
            res = S.kb_zbmath("moduli space", 3)
            http.assert_not_called()
        self.assertIn("規約未同意", res["error"])
        self.assertIn("FREEAGENT_ZBMATH_ACK", res["error"])
        self.assertIn("zbmath.org/legal", res["error"])

    def test_interval_and_budget(self):
        self.assertEqual(S.ZBMATH_INTERVAL, 1.0)
        self.assertEqual(S.ZBMATH_BUDGET, "api.zbmath.org/document-search")

    def test_empty_and_cjk_queries_do_not_touch_the_network(self):
        self._ack()
        with mock.patch.object(S, "_kb_new_json") as http:
            self.assertTrue(S.kb_zbmath("", 3).get("error"))
            self.assertTrue(S.kb_zbmath("モジュライ空間", 3).get("error"))
            http.assert_not_called()

    def test_each_word_is_quoted_and_anded(self):
        self._ack()
        with mock.patch.object(S, "_kb_new_json", return_value=({"result": []}, "")) as http:
            S.kb_zbmath("moduli space", 2)
        parts = urlsplit(http.call_args[0][0])
        qs = parse_qs(parts.query)
        self.assertEqual(parts.netloc, "api.zbmath.org")
        self.assertEqual(parts.path, "/v1/document/_search")
        self.assertEqual(qs["search_string"], ['"moduli" AND "space"'])
        self.assertEqual(qs["results_per_page"], ["2"])
        self.assertEqual(http.call_args[0][1], S.ZBMATH_INTERVAL)
        self.assertEqual(http.call_args[1].get("budget_key"), S.ZBMATH_BUDGET)

    def test_bibliographic_rows_are_metadata_only(self):
        self._ack()
        with mock.patch.object(S, "_kb_new_json", return_value=({"result": [zb_row()]}, "")):
            res = S.kb_zbmath("semantic spaces", 1)
        cite = res["citations"][0]
        self.assertEqual(cite["source"], "zbmath")
        self.assertEqual(cite["title"], "Semantic spaces")
        self.assertEqual(cite["url"], "https://zbmath.org/6751902")
        self.assertEqual(cite["year"], "2016")
        self.assertEqual(cite["summary_kind"], "structured_metadata")
        self.assertTrue(cite["metadata_only"])
        self.assertEqual(cite["extra"]["msc"], ["68T50"])
        self.assertEqual(cite["extra"]["doi"], "10.1007/s11786-016-0278-9")
        self.assertIn("キーワード semantics", cite["summary"])
        S._kb_http_url(cite["url"])

    def test_review_text_is_body_evidence(self):
        self._ack()
        row = zb_row(review="The book explains the algebraic geometry behind semantic spaces in detail.")
        with mock.patch.object(S, "_kb_new_json", return_value=({"result": [row]}, "")):
            cite = S.kb_zbmath("semantic spaces", 1)["citations"][0]
        self.assertEqual(cite["summary_kind"], "editorial_review")
        self.assertFalse(cite["metadata_only"])
        self.assertIn("algebraic geometry", cite["summary"])
        self.assertNotIn("unavailable", cite["summary"])

    def test_subtitle_is_kept(self):
        self._ack()
        row = zb_row(title="Collected works")
        row["title"]["subtitle"] = "Vol. 2"
        with mock.patch.object(S, "_kb_new_json", return_value=({"result": [row]}, "")):
            cite = S.kb_zbmath("collected", 1)["citations"][0]
        self.assertEqual(cite["title"], "Collected works : Vol. 2")

    def test_404_is_reported_as_no_hit(self):
        self._ack()
        with mock.patch.object(S, "_kb_new_json",
                               return_value=(None, 'HTTP 404: {"result":null}')):
            res = S.kb_zbmath("zzz", 1)
        self.assertIn("該当がありません", res["error"])

    def test_rows_without_url_are_skipped(self):
        self._ack()
        with mock.patch.object(S, "_kb_new_json",
                               return_value=({"result": [zb_row(url=""), "junk"]}, "")):
            self.assertEqual(S.kb_zbmath("x", 3)["error"], "該当なし")


if __name__ == "__main__":
    unittest.main()
