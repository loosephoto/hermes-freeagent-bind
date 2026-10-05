"""第6段階ソース（プログラミング・標準 / 科学）のオフライン回帰。HTTP のみ模擬する。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S

STAGE6 = ("osv", "ietf", "uniprot", "chembl", "pdb", "quickgo", "reactome", "clinicaltrials",
          "openfda", "inspirehep", "oeis", "gbif", "hfhub")

OSV_VULN = {"id": "GHSA-462w-v97r-4m45", "summary": "Jinja2 sandbox escape via string formatting",
            "details": "In Pallets Jinja before 2.10.1 ...", "aliases": ["CVE-2019-10906"],
            "published": "2019-04-01T00:00:00Z",
            "severity": [{"type": "CVSS_V3", "score": "9.8"}],
            "affected": [{"package": {"name": "jinja2", "ecosystem": "PyPI"}}]}

IETF_DOC = {"name": "rfc9110", "title": "HTTP Semantics",
            "abstract": "   The Hypertext Transfer Protocol (HTTP) is a stateless protocol.",
            "rfc_number": 9110, "pages": 194, "rev": "06",
            "std_level": "/api/v1/name/stdlevelname/std/", "time": "2026-05-20T15:43:39Z"}

UNIPROT_ROW = {"entryType": "UniProtKB reviewed (Swiss-Prot)", "primaryAccession": "P01308",
               "uniProtkbId": "INS_HUMAN",
               "proteinDescription": {"recommendedName": {"fullName": {"value": "Insulin"}}},
               "organism": {"scientificName": "Homo sapiens"},
               "genes": [{"geneName": {"value": "INS"}}], "sequence": {"length": 110},
               "comments": [{"commentType": "FUNCTION",
                             "texts": [{"value": "Insulin decreases blood glucose concentration."}]}]}

CHEMBL_ROW = {"molecule_chembl_id": "CHEMBL25", "pref_name": "ASPIRIN", "max_phase": 4,
              "molecule_properties": {"full_molformula": "C9H8O4", "full_mwt": "180.16"},
              "molecule_structures": {"canonical_smiles": "CC(=O)Oc1ccccc1C(=O)O"},
              "molecule_synonyms": [{"molecule_synonym": "Acetylsalicylic acid"}]}

PDB_DOC = {"pdb_id": "3a59", "title": "Structure of Hemoglobin from flightless bird",
           "experimental_method": ["X-ray diffraction"], "resolution": 3.41,
           "deposition_date": "2009-08-03T01:00:00Z",
           "organism_scientific_name": ["Struthio camelus"], "journal": "To be published"}

QUICKGO_ROW = {"id": "GO:0097194", "name": "execution phase of apoptosis",
               "definition": {"text": "A stage of the apoptotic process ..."},
               "aspect": "biological_process", "isObsolete": False}

# 実測: summation は配列ではなく HTML を含む文字列（旧実装は配列として走査して落ちた）
REACTOME_ROW = {"stId": "R-HSA-109581", "name": "<span class=\"highlighting\" >Apoptosis</span>",
                "type": "Pathway", "species": ["Homo sapiens"], "isDisease": False,
                "summation": "<span class=\"highlighting\" >Apoptosis</span> is a distinct form of cell death."}

CT_STUDY = {"protocolSection": {
    "identificationModule": {"nctId": "NCT05913388", "briefTitle": "GB1211 and Pembrolizumab"},
    "statusModule": {"overallStatus": "ACTIVE_NOT_RECRUITING", "startDateStruct": {"date": "2024-02-29"}},
    "conditionsModule": {"conditions": ["Metastatic Melanoma"]},
    "designModule": {"phases": ["PHASE2"]},
    "descriptionModule": {"briefSummary": "The purpose of this study is to determine ..."}}}

OPENFDA_ROW = {"set_id": "000155a8-709c-44e5", "effective_time": "20240101", "id": "abc",
               "indications_and_usage": ["1 INDICATIONS AND USAGE Naproxen tablets are indicated for ..."],
               "openfda": {"brand_name": ["Naproxen"], "generic_name": ["NAPROXEN"],
                           "manufacturer_name": ["Acme"]}}

INSPIRE_HIT = {"id": "2181837", "metadata": {
    "titles": [{"title": "The Higgs boson in the CP-violating NB-LSSM"}],
    "dois": [{"value": "10.1140/epjc/s10052-026-15520-7"}],
    "arxiv_eprints": [{"value": "2601.12345"}], "earliest_date": "2026-01-05",
    "abstracts": [{"value": "This study investigates the lightest Higgs bosons ..."}],
    "citation_count": 3, "authors": [{"full_name": "A. Author"}]}}

# 実測: A 番号は `number`（45 → A000045）。`id` は旧 M/N 識別子。
OEIS_ROW = {"number": 45, "id": "M0692 N0256", "data": "0,1,1,2,3,5,8,13",
            "name": "Fibonacci numbers: F(n) = F(n-1) + F(n-2).",
            "comment": ["D. E. Knuth writes: ..."], "keyword": "nonn,core,nice"}

GBIF_ROW = {"key": 147345877, "scientificName": "Panthera leo", "canonicalName": "Panthera leo",
            "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "genus": "Panthera",
            "numOccurrences": 0}

HF_ROW = {"id": "meta-llama/Llama-3.2-1B-Instruct", "pipeline_tag": "text-generation",
          "library_name": "transformers", "downloads": 7575085, "likes": 120,
          "createdAt": "2024-09-25T00:00:00.000Z", "tags": ["license:llama3.2", "text-generation"]}


def fetch_by_url(mapping, default=None):
    """URL に含まれる鍵で payload を選ぶ kb_json の差し替え。"""
    def fake(url, **kwargs):
        for key, value in mapping.items():
            if key in url:
                return value, ""
        if default is not None:
            return default, ""
        raise AssertionError(f"unexpected url: {url}")
    return fake


class TestStage6Registry(unittest.TestCase):
    def test_all_stage6_sources_are_opt_in_and_advertised(self):
        for name in STAGE6:
            self.assertIn(name, S.SOURCES)
            self.assertIn(name, S.KB_BACKENDS)
            self.assertNotIn(name, S.DEFAULT_SOURCES)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool in ("freeagent_lookup", "freeagent_grounded"):
            desc = str(next(t for t in S.TOOLS if t["name"] == tool)
                       ["inputSchema"]["properties"]["sources"]["description"])
            for name in STAGE6:
                self.assertIn(name, desc, (tool, name))

    def test_no_stage6_source_touches_the_network_without_arguments(self):
        for name in STAGE6:
            with mock.patch.object(S, "kb_json") as http:
                got = S.KB_BACKENDS[name](123, 3, {})
                self.assertTrue(got.get("error"), name)
                self.assertTrue(S.KB_BACKENDS[name]("", 3, {}).get("error"), name)
                http.assert_not_called()


class TestOsv(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_vulnerability_id_is_fetched_directly(self):
        with mock.patch.object(S, "kb_json", side_effect=fetch_by_url({
                "/v1/vulns/CVE-2019-10906": {"id": "CVE-2019-10906", "details": "Sandbox escape",
                                             "aliases": ["GHSA-462w-v97r-4m45"]}})) as http:
            got = S.kb_osv("CVE-2019-10906")
        self.assertIn("/v1/vulns/CVE-2019-10906", http.call_args[0][0])
        cite = got["citations"][0]
        self.assertEqual(cite["identifier"], "CVE-2019-10906")
        self.assertEqual(cite["url"], "https://osv.dev/vulnerability/CVE-2019-10906")
        self.assertTrue(cite["summary"])

    def test_package_name_resolves_ecosystem_then_fetches_full_records(self):
        batch = {"results": [{"vulns": []}, {"vulns": [{"id": "GHSA-x"}]}] + [{} for _ in range(8)]}
        with mock.patch.object(S, "kb_json", side_effect=fetch_by_url({
                "/v1/querybatch": batch, "/v1/query": {"vulns": [OSV_VULN]}})) as http:
            got = S.kb_osv("jinja2")
        urls = [c[0][0] for c in http.call_args_list]
        self.assertIn("/v1/querybatch", urls[0])
        self.assertIn("/v1/query", urls[1])
        self.assertEqual(got["ecosystem"], "npm")   # 2 番目の生態系が最初のヒット
        cite = got["citations"][0]
        self.assertEqual(cite["aliases"], ["CVE-2019-10906"])
        self.assertEqual(cite["severity"], "9.8")
        self.assertEqual(cite["package"], ["PyPI/jinja2"])

    def test_unknown_package_does_not_issue_a_second_request(self):
        with mock.patch.object(S, "kb_json", side_effect=fetch_by_url(
                {"/v1/querybatch": {"results": [{} for _ in range(10)]}})) as http:
            got = S.kb_osv("no-such-package-xyz")
        self.assertEqual(len(http.call_args_list), 1)
        self.assertIn("該当なし", got["error"])
        self.assertIn("groupId:artifactId", got["error"])

    def test_malformed_response_does_not_leak(self):
        for data in (None, [], {"results": "x"}, {"vulns": "x"}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(data, "")):
                got = S.kb_osv("CVE-2019-10906")
            self.assertTrue(got.get("error"), data)


class TestIetf(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_rfc_number_is_looked_up_by_name(self):
        with mock.patch.object(S, "kb_json", return_value=({"objects": [IETF_DOC]}, "")) as http:
            got = S.kb_ietf("RFC 9110")
        url = http.call_args[0][0]
        self.assertIn("name=rfc9110", url)
        self.assertNotIn("title__contains", url)
        cite = got["citations"][0]
        self.assertEqual(cite["url"], "https://datatracker.ietf.org/doc/rfc9110/")
        self.assertEqual(cite["identifier"], "RFC 9110")
        self.assertEqual(cite["kind"], "RFC")
        self.assertEqual(cite["extra"]["std_level"], "std")
        self.assertTrue(S._kb_has_evidence(cite))

    def test_multiword_query_uses_longest_term_and_draft_rfc_filter(self):
        with mock.patch.object(S, "kb_json", return_value=({"objects": [IETF_DOC]}, "")) as http:
            S.kb_ietf("HTTP semantics")
        url = http.call_args[0][0]
        self.assertIn("title__contains=semantics", url)
        self.assertIn("type__in=draft%2Crfc", url)

    def test_draft_name_is_looked_up_by_name_contains(self):
        with mock.patch.object(S, "kb_json", return_value=({"objects": []}, "")) as http:
            S.kb_ietf("draft-ietf-httpbis-semantics")
        self.assertIn("name__contains=draft-ietf-httpbis-semantics", http.call_args[0][0])

    def test_without_abstract_the_row_is_metadata_only(self):
        doc = {**IETF_DOC, "abstract": ""}
        with mock.patch.object(S, "kb_json", return_value=({"objects": [doc]}, "")):
            cite = S.kb_ietf("RFC 9110")["citations"][0]
        self.assertTrue(cite["metadata_only"])
        self.assertFalse(S._kb_has_evidence(cite))


class TestScienceSources(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_uniprot_function_comment_is_the_evidence(self):
        with mock.patch.object(S, "kb_json", return_value=({"results": [UNIPROT_ROW]}, "")) as http:
            got = S.kb_uniprot("insulin")
        self.assertIn("fields=", http.call_args[0][0])
        cite = got["citations"][0]
        self.assertEqual(cite["title"], "Insulin (P01308)")
        self.assertEqual(cite["summary"], "Insulin decreases blood glucose concentration.")
        self.assertEqual(cite["container"], "Homo sapiens")
        self.assertEqual(cite["url"], "https://www.uniprot.org/uniprotkb/P01308/entry")
        self.assertTrue(S._kb_has_evidence(cite))

    def test_uniprot_without_function_comment_is_metadata_only(self):
        row = {**UNIPROT_ROW, "comments": []}
        with mock.patch.object(S, "kb_json", return_value=({"results": [row]}, "")):
            cite = S.kb_uniprot("insulin")["citations"][0]
        self.assertTrue(cite["metadata_only"])

    def test_structured_record_sources_are_labelled(self):
        cases = [(S.kb_chembl, {"molecules": [CHEMBL_ROW]}, "chembl"),
                 (S.kb_pdb, {"response": {"docs": [PDB_DOC]}}, "pdb"),
                 (S.kb_gbif, {"results": [GBIF_ROW]}, "gbif"),
                 (S.kb_hfhub, [HF_ROW], "hfhub")]
        for fn, payload, name in cases:
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(payload, "")):
                cite = fn("q")["citations"][0]
            self.assertEqual(cite["summary_kind"], "structured_record", name)
            self.assertTrue(cite["summary"], name)
            self.assertFalse(cite["metadata_only"], name)
            self.assertEqual(cite["source"], name)

    def test_chembl_keeps_identifier_and_facts(self):
        with mock.patch.object(S, "kb_json", return_value=({"molecules": [CHEMBL_ROW]}, "")):
            cite = S.kb_chembl("aspirin")["citations"][0]
        self.assertEqual(cite["identifier"], "CHEMBL25")
        self.assertIn("C9H8O4", cite["summary"])
        self.assertEqual(cite["url"], "https://www.ebi.ac.uk/chembl/compound_report_card/CHEMBL25/")

    def test_quickgo_definition_is_the_evidence(self):
        with mock.patch.object(S, "kb_json", return_value=({"results": [QUICKGO_ROW]}, "")):
            cite = S.kb_quickgo("apoptosis")["citations"][0]
        self.assertEqual(cite["title"], "GO:0097194: execution phase of apoptosis")
        self.assertIn("apoptotic process", cite["summary"])
        self.assertEqual(cite["url"], "https://www.ebi.ac.uk/QuickGO/term/GO:0097194")

    def test_reactome_summation_is_a_string_not_a_list(self):
        """実測: summation は HTML を含む文字列。配列として走査すると落ちる（回帰）。"""
        with mock.patch.object(S, "kb_json", return_value=({"results": [{"entries": [REACTOME_ROW]}]}, "")):
            got = S.kb_reactome("apoptosis")
        self.assertFalse(got.get("error"), got)
        cite = got["citations"][0]
        self.assertNotIn("<span", cite["summary"])
        self.assertIn("distinct form of cell death", cite["summary"])
        self.assertEqual(cite["url"], "https://reactome.org/content/detail/R-HSA-109581")

    def test_clinicaltrials_and_openfda_and_inspirehep_use_prose(self):
        with mock.patch.object(S, "kb_json", return_value=({"studies": [CT_STUDY]}, "")):
            ct = S.kb_clinicaltrials("melanoma")["citations"][0]
        self.assertEqual(ct["url"], "https://clinicaltrials.gov/study/NCT05913388")
        self.assertIn("purpose of this study", ct["summary"])
        S._KB_CACHE.clear()
        with mock.patch.object(S, "kb_json", return_value=({"results": [OPENFDA_ROW]}, "")):
            fda = S.kb_openfda("aspirin")["citations"][0]
        self.assertIn("INDICATIONS AND USAGE", fda["summary"])
        self.assertIn("dailymed", fda["url"])
        self.assertIn("医療判断", S.kb_openfda.__doc__ or "")
        S._KB_CACHE.clear()
        with mock.patch.object(S, "kb_json", return_value=({"hits": {"hits": [INSPIRE_HIT]}}, "")):
            hep = S.kb_inspirehep("higgs")["citations"][0]
        self.assertEqual(hep["url"], "https://doi.org/10.1140/epjc/s10052-026-15520-7")
        self.assertIn("lightest Higgs bosons", hep["summary"])
        self.assertEqual(hep["extra"]["arxiv"], "2601.12345")

    def test_oeis_uses_the_a_number_not_the_old_mn_id(self):
        """実測: `id` は "M0692 N0256"、A 番号は `number`（回帰）。"""
        with mock.patch.object(S, "kb_json", return_value=([OEIS_ROW], "")):
            cite = S.kb_oeis("Fibonacci")["citations"][0]
        self.assertEqual(cite["identifier"], "A000045")
        self.assertEqual(cite["url"], "https://oeis.org/A000045")
        self.assertEqual(cite["extra"]["old_id"], "M0692 N0256")
        self.assertEqual(cite["extra"]["keywords"], ["nonn", "core", "nice"])

    def test_oeis_null_result_is_reported_not_crashed(self):
        with mock.patch.object(S, "kb_json", return_value=(None, "")):
            got = S.kb_oeis("zzzznotasequence")
        self.assertTrue(got.get("error"))
        self.assertFalse(got.get("citations"))

    def test_malformed_responses_do_not_leak(self):
        payloads = {
            S.kb_uniprot: (None, [], {"results": "x"}, {"results": ["x"]}),
            S.kb_chembl: (None, [], {"molecules": "x"}, {"molecules": ["x"]}),
            S.kb_pdb: (None, [], {"response": "x"}, {"response": {"docs": "x"}}),
            S.kb_quickgo: (None, [], {"results": "x"}, {"results": ["x"]}),
            S.kb_reactome: (None, [], {"results": "x"}, {"results": [{"entries": "x"}]}),
            S.kb_clinicaltrials: (None, [], {"studies": "x"}, {"studies": ["x"]}),
            S.kb_openfda: (None, [], {"results": "x"}, {"results": ["x"]}),
            S.kb_inspirehep: (None, [], {"hits": "x"}, {"hits": {"hits": "x"}}),
            S.kb_gbif: (None, [], {"results": "x"}, {"results": ["x"]}),
            S.kb_hfhub: (None, [], {"results": "x"}),
        }
        for fn, variants in payloads.items():
            for data in variants:
                S._KB_CACHE.clear()
                with mock.patch.object(S, "kb_json", return_value=(data, "")):
                    got = fn("q")
                self.assertTrue(got.get("error"), (fn.__name__, data, got))
                self.assertFalse(got.get("citations"), (fn.__name__, data))

    def test_rate_budget_failure_is_reported_not_waited(self):
        for name in ("uniprot", "chembl", "pdb", "quickgo", "reactome", "clinicaltrials",
                     "openfda", "inspirehep", "oeis", "gbif", "hfhub"):
            with mock.patch.object(S, "_kb_rate_acquire", return_value=1.5), \
                 mock.patch.object(S, "kb_json") as http:
                got = S.KB_BACKENDS[name]("q", 3, {})
                http.assert_not_called()
            self.assertIn("429", got["error"], name)

    def test_ebi_services_have_separate_budgets_on_one_host(self):
        """www.ebi.ac.uk に同居する chembl / pdb / quickgo が互いを締め出さないこと。"""
        keys = [S._ebi_key("chembl"), S._ebi_key("pdbe"), S._ebi_key("QuickGO")]
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(k.startswith("www.ebi.ac.uk/") for k in keys))

    def test_budget_key_splits_one_host_but_blocking_stays_per_host(self):
        calls = []
        with mock.patch.object(S, "_kb_rate_acquire",
                               side_effect=lambda key, interval: calls.append(key) or 0.0), \
             mock.patch.object(S, "kb_json", return_value=({}, "")):
            S._kb_new_json("https://api.osv.dev/v1/querybatch", 0.5, payload={},
                           budget_key="api.osv.dev/v1/querybatch")
            S._kb_new_json("https://api.osv.dev/v1/query", 0.5, payload={},
                           budget_key="api.osv.dev/v1/query")
        self.assertEqual(calls, ["api.osv.dev/v1/querybatch", "api.osv.dev/v1/query"])

    def test_post_helper_sends_json_body(self):
        seen = {}

        class FakeResp:
            status = 200
            def read(self):
                return b'{"ok": true}'
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        def fake_urlopen(req, timeout=None):
            seen["method"] = req.get_method()
            seen["body"] = req.data
            seen["ctype"] = req.get_header("Content-type")
            return FakeResp()

        with mock.patch.object(S, "_urlopen", side_effect=fake_urlopen):
            data, err = S.kb_json("https://example.invalid/x", payload={"a": 1})
        self.assertEqual(err, "")
        self.assertEqual(data, {"ok": True})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["body"], b'{"a": 1}')
        self.assertIn("application/json", seen["ctype"] or "")


class TestRateBudgetSplit(unittest.TestCase):
    """レート予算は service 単位、遮断の記憶は host 単位であること（設計の回帰）。"""

    def test_same_host_services_do_not_block_each_other(self):
        S._KB_RATE_NEXT.clear()
        self.assertEqual(S._kb_rate_acquire(S._ebi_key("chembl"), 1.0), 0.0)
        self.assertEqual(S._kb_rate_acquire(S._ebi_key("pdbe"), 1.0), 0.0)
        self.assertGreater(S._kb_rate_acquire(S._ebi_key("chembl"), 1.0), 0.0)
        S._KB_RATE_NEXT.clear()

    def test_host_key_still_throttles_a_repeat(self):
        S._KB_RATE_NEXT.clear()
        self.assertEqual(S._kb_rate_acquire("example.invalid", 60), 0.0)
        self.assertGreater(S._kb_rate_acquire("example.invalid", 60), 0.0)
        S._KB_RATE_NEXT.clear()


if __name__ == "__main__":
    unittest.main()
