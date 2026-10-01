"""追加知識ソースのオフライン契約（外部HTTPだけを模擬）。"""
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


class TestDataCite(unittest.TestCase):
    def setUp(self):
        with S._KB_CACHE_LOCK:
            S._KB_CACHE.clear()
        if hasattr(S, "_kb_rate_acquire"):
            guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
            guard.start()
            self.addCleanup(guard.stop)

    def test_invalid_mode_and_malformed_response_never_leak(self):
        with mock.patch.object(S, "kb_json") as http:
            self.assertTrue(S.kb_datacite("q", kind="wrong")["error"])
            http.assert_not_called()
        for payload in (None, [], {"data": "wrong"}, {"data": [{"attributes": {"titles": 7}}]}):
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(payload, "")):
                self.assertTrue(S.kb_datacite("q").get("error"))

    def test_invalid_citation_url_is_an_error_not_a_success(self):
        data = {"data": [{"attributes": {"titles": [{"title": "Paper"}], "url": "h"}}]}
        with mock.patch.object(S, "kb_json", return_value=(data, "")):
            got = S.kb_datacite("q")
        self.assertTrue(got.get("error"))
        self.assertFalse(got.get("citations"))

    def test_error_is_not_cached_and_limit_is_defensive(self):
        with mock.patch.object(S, "kb_json", side_effect=[(None, "HTTP 503"), ({"data": []}, "")]) as http:
            self.assertIn("503", S.kb_datacite("q", limit="1e999")["error"])
            self.assertEqual(S.kb_datacite("q", limit="1e999")["error"], "該当なし")
        self.assertEqual(http.call_count, 2)
        self.assertEqual(parse_qs(urlsplit(http.call_args.args[0]).query)["page[size]"], ["5"])

    def test_arxiv_metadata_and_literal_search(self):
        self.assertTrue(callable(getattr(S, "kb_datacite", None)), "DataCite backend is missing")
        payload = {"data": [{"attributes": {
            "doi": "10.48550/arxiv.2401.00001", "url": "https://arxiv.org/abs/2401.00001",
            "titles": [{"title": "Paper"}], "creators": [{"name": "Author"}],
            "publicationYear": 2024, "types": {"resourceTypeGeneral": "Text"},
            "descriptions": [{"descriptionType": "Other", "description": "2 pages"},
                             {"descriptionType": "Abstract", "description": "<p>Real abstract</p>"}]}}]}
        with mock.patch.object(S, "kb_json", return_value=(payload, "")) as http:
            result = S.kb_datacite("language model", limit=2, kind="arxiv")
        params = parse_qs(urlsplit(http.call_args.args[0]).query)
        self.assertEqual(params["query"], ['"language" AND "model"'])
        self.assertEqual(params["sort"], ["relevance"])
        self.assertEqual(params["client-id"], ["arxiv.content"])
        self.assertNotIn("resource-type-id", params)
        cite = result["citations"][0]
        self.assertEqual(cite["source"], "datacite")
        self.assertEqual(cite["summary"], "Real abstract")
        self.assertEqual(cite["year"], 2024)
        self.assertEqual(cite["repository"], "arxiv")
        self.assertEqual(result["items"][0]["authors"], ["Author"])

    def test_dataset_lookup_is_opt_in_and_cache_is_mode_specific(self):
        self.assertIn("datacite", S.KB_BACKENDS)
        calls = []
        def http(url):
            calls.append(parse_qs(urlsplit(url).query))
            return {"data": [{"attributes": {"titles": [{"title": "Dataset"}],
                    "doi": "10.1234/data.1", "types": {"resourceTypeGeneral": "Dataset"}}}]}, ""
        with mock.patch.object(S, "kb_json", side_effect=http):
            got = S.tool_lookup({"query": "test", "sources": ["datacite"], "datacite_kind": "dataset"})
            S.kb_datacite("test", kind="dataset", limit=3)
            S.kb_datacite("test", kind="all", limit=3)
        self.assertEqual(got["sources"], ["datacite"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["resource-type-id"], ["dataset"])
        self.assertNotIn("resource-type-id", calls[1])
        self.assertTrue(got["citations"][0]["metadata_only"])
        self.assertEqual(got["citations"][0]["summary"], "")
        self.assertEqual(got["citations"][0]["url"], "https://doi.org/10.1234/data.1")
        seen = []
        def backend(name):
            return lambda *a: (seen.append(name) or {"items": [], "citations": []})
        with mock.patch.dict(S.KB_BACKENDS, {s: backend(s) for s in S.KB_BACKENDS}, clear=True):
            S.knowledge_lookup("default")
        self.assertNotIn("datacite", seen)
        self.assertEqual(set(seen), set(S.DEFAULT_SOURCES))


class TestArxivFallback(unittest.TestCase):
    def test_permission_provenance_and_primary_error(self):
        alt = {"source": "datacite", "items": [{"title": "Paper", "url": "https://arxiv.org/abs/1"}],
               "citations": [{"source": "datacite", "title": "Paper", "url": "https://arxiv.org/abs/1", "summary": "Abstract"}]}
        with mock.patch.dict(S.KB_BACKENDS, {"arxiv": mock.Mock(return_value={"error": "HTTP 429: Rate exceeded"}),
                                           "datacite": mock.Mock(return_value=alt)}, clear=True):
            disabled = S.tool_lookup({"query": "language model", "sources": ["arxiv"]})
            self.assertIn("arxiv", disabled["errors"])
            S.KB_BACKENDS["datacite"].assert_not_called()
            enabled = S.tool_lookup({"query": "language model", "sources": ["arxiv"], "fallback": True})
            self.assertEqual(enabled["citation_count"], 1)
            row = enabled["results"]["arxiv"]
            self.assertEqual(row["fallback"]["served_by"], "datacite")
            self.assertIn("429", row["fallback"]["primary_error"])
            self.assertEqual(enabled["citations"][0]["source"], "datacite")
            self.assertEqual(S.KB_BACKENDS["datacite"].call_args.args[2]["datacite_kind"], "arxiv")
            self.assertIn("datacite", S.render("freeagent_lookup", enabled))

    def test_slow_primary_is_hedged_within_the_same_deadline(self):
        self.assertTrue(hasattr(S, "KB_HEDGE_DELAY"), "hedge configuration is missing")
        release = threading.Event()
        finished = threading.Event()
        def slow(*args):
            release.wait(2)
            finished.set()
            return {"error": "TimeoutError"}
        with mock.patch.dict(S.KB_BACKENDS, {"arxiv": slow,
                "datacite": lambda *a: {"items": [{"title": "Alternative"}], "citations": []}}, clear=True), \
             mock.patch.object(S, "KB_HEDGE_DELAY", 0.01):
            try:
                start = time.monotonic()
                got = S.knowledge_lookup("language model", ["arxiv"], fallback=True, deadline=0.15)
                self.assertLess(time.monotonic() - start, 0.12)
                self.assertEqual(got["results"]["arxiv"]["fallback"]["served_by"], "datacite")
            finally:
                release.set()
                finished.wait(1)

    def test_no_fallback_for_arxiv_syntax_versions_no_hits_or_permission_strings(self):
        with mock.patch.dict(S.KB_BACKENDS, {"arxiv": mock.Mock(return_value={"error": "HTTP 429"}),
                                           "datacite": mock.Mock()}, clear=True):
            for query in ("cat:cs.AI", "ti:transformer", "2401.12345v2", "a AND b",
                          "hep-th/9901001v2", "math.GT/0309136", "cs/0110001v1"):
                got = S.tool_lookup({"query": query, "sources": ["arxiv"], "fallback": True})
                self.assertIn("arxiv", got["errors"])
            S.KB_BACKENDS["arxiv"].return_value = {"error": "該当なし", "items": []}
            S.tool_lookup({"query": "test", "sources": ["arxiv"], "fallback": True})
            S.KB_BACKENDS["arxiv"].return_value = {"error": "HTTP 429"}
            S.tool_lookup({"query": "test", "sources": ["arxiv"], "fallback": "false"})
            S.KB_BACKENDS["datacite"].assert_not_called()


class TestNewBackendRate(unittest.TestCase):
    def test_datacite_modes_share_a_fail_fast_budget_but_cache_still_works(self):
        self.assertTrue(callable(getattr(S, "_kb_rate_acquire", None)), "rate guard is missing")
        S._KB_CACHE.clear()
        S._KB_RATE_NEXT.clear()
        with mock.patch.object(S.time, "monotonic", return_value=10.0), \
             mock.patch.object(S, "kb_json", return_value=({"data": [{"attributes": {
                 "titles": [{"title": "Paper"}], "url": "https://arxiv.org/abs/1"}}]}, "")) as http:
            first = S.kb_datacite("q", kind="arxiv")
            cached = S.kb_datacite("q", kind="arxiv")
            limited = S.kb_datacite("q", kind="dataset")
        self.assertEqual(first, cached)
        self.assertIn("429", limited["error"])
        self.assertEqual(http.call_count, 1)
        self.assertEqual(S._kb_rate_acquire("other.example", 60), 0)
        S._KB_RATE_NEXT.clear()


class TestAdditionalPaperBackends(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        if hasattr(S, "_kb_rate_acquire"):
            patcher = mock.patch.object(S, "_kb_rate_acquire", return_value=0)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_europepmc_core_abstract_and_preprint_provenance(self):
        self.assertIn("europepmc", S.KB_BACKENDS)
        data = {"resultList": {"result": [{"source": "PPR", "id": "PPR1", "title": "Paper",
                "pubYear": "2025", "abstractText": "<p>Actual abstract</p>", "doi": "10.1/test",
                "authorList": {"author": [{"fullName": "A"}]}, "pubTypeList": {"pubType": ["Preprint"]},
                "isOpenAccess": "Y", "license": "cc-by", "citedByCount": 4}]}}
        with mock.patch.object(S, "kb_json", return_value=(data, "")) as http:
            out = S.tool_lookup({"query": "CRISPR", "sources": ["europepmc"], "limit": 1})
        self.assertFalse(out["errors"])
        self.assertEqual(parse_qs(urlsplit(http.call_args.args[0]).query)["resultType"], ["core"])
        self.assertEqual(out["citations"][0]["summary"], "Actual abstract")
        self.assertEqual(out["citations"][0]["source"], "europepmc")
        item = out["results"]["europepmc"]["items"][0]
        self.assertEqual(item["publication_types"], ["Preprint"])
        self.assertEqual(item["url"], "https://europepmc.org/article/PPR/PPR1")
        self.assertEqual(item["license"], "cc-by")

    def test_openaire_v3_credit_pid_and_description(self):
        self.assertIn("openaire", S.KB_BACKENDS)
        data = {"results": [{"id": "id1", "mainTitle": "Paper", "descriptions": ["<p>Abstract</p>"],
                "publicationDate": "2024-01-01", "authors": [{"fullName": "A"}],
                "pids": [{"scheme": "doi", "value": "10.1/test"}],
                "indicators": {"citationImpact": {"citationCount": 2}},
                "instances": [{"urls": ["https://doi.org/10.1/test"], "license": "CC BY NC ND"}]}]}
        with mock.patch.object(S, "kb_json", return_value=(data, "")) as http:
            got = S.tool_lookup({"query": "climate", "sources": ["openaire"], "limit": 1})
        self.assertIn("/graph/v3/research-products?", http.call_args.args[0])
        cite = got["citations"][0]
        self.assertEqual(cite["summary"], "Abstract")
        self.assertEqual(cite["year"], "2024")
        self.assertEqual(cite["url"], "https://doi.org/10.1/test")
        self.assertIn("OpenAIRE", S.render("freeagent_lookup", got))
        item = got["results"]["openaire"]["items"][0]
        self.assertEqual(item["cited_by"], 2)
        self.assertEqual(item["licenses"], ["CC BY NC ND"])

    def test_missing_abstract_is_not_injected_as_body_evidence(self):
        cites = [{"source": "openaire", "title": "Only title", "url": "https://example.org/1",
                  "summary": "", "metadata_only": True}]
        with mock.patch.object(S, "knowledge_lookup", return_value={"citations": cites, "sources": ["openaire"]}), \
             mock.patch.object(S, "ask_many") as infer:
            got = S.tool_grounded({"question": "q", "sources": ["openaire"]})
        self.assertIn("error", got)
        infer.assert_not_called()
        self.assertNotIn("Only title", S._evidence_block(cites))

    def test_openaire_rejects_wrong_container_types(self):
        for fields in ({"descriptions": {"not_an_abstract": "actual value"}},
                       {"instances": [{"urls": "https://example.org/p"}]},
                       {"descriptions": "not a list"},
                       {"instances": {"urls": ["https://example.org/p"]}},
                       {"instances": [{"urls": ["h"]}]}):
            S._KB_CACHE.clear()
            row = {"mainTitle": "Paper", "id": "id1", **fields}
            with mock.patch.object(S, "_kb_new_json", return_value=({"results": [row]}, "")):
                result = S.kb_openaire("q")
            self.assertTrue(result.get("error"), fields)
            self.assertFalse(result.get("citations"), fields)

    def test_metadata_errors_and_limits(self):
        for source, fn in (("openaire", S.kb_openaire), ("europepmc", S.kb_europepmc)):
            for data in (None, [], {"results": "wrong", "resultList": {"result": "wrong"}}):
                S._KB_CACHE.clear()
                with mock.patch.object(S, "kb_json", return_value=(data, "")):
                    self.assertTrue(fn("q", limit="1e999").get("error"))
            with mock.patch.object(S, "kb_json") as http:
                self.assertTrue(fn(9).get("error"))
                http.assert_not_called()


class TestAgentEvidenceSafety(unittest.TestCase):
    def test_metadata_only_never_receives_an_agent_citation_number(self):
        citation = {"source": "datacite", "title": "Bibliography only", "url": "https://example.org/x",
                    "summary": "", "metadata_only": True}
        lookup = {"citations": [citation], "results": {"datacite": {"items": [citation]}}, "errors": {}}
        replies = [{"text": '{"tool":"lookup","query":"q","sources":["datacite"]}'},
                   {"text": '{"answer":"Claim [1]"}'}]
        prompts = []
        def respond(ref, prompt, **kw):
            prompts.append(prompt)
            return replies[len(prompts) - 1]
        with mock.patch.object(S, "knowledge_lookup", return_value=lookup), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=respond):
            result = S.tool_agent({"task": "q", "max_steps": 2})
        self.assertFalse(result["agents"][0]["cited_ok"])
        self.assertEqual(result["unsupported_citations"], [1])
        self.assertEqual(result["answers_with_citations"], 0)
        self.assertEqual(result["citation_count"], 0)
        self.assertEqual(result["agents"][0]["bibliography"], [citation])
        self.assertIn("書誌のみ", prompts[1])
        self.assertNotIn("[1] Bibliography only", prompts[1])

    def test_agent_keeps_distinct_doi_versions_even_when_provider_and_url_match(self):
        cites = [{"source": "datacite", "title": "Version one", "url": "https://example.org/shared",
                  "doi": "10.1234/x.1", "summary": "Body version one"},
                 {"source": "datacite", "title": "Version two", "url": "https://example.org/shared",
                  "doi": "10.1234/x.2", "summary": "Body version two"}]
        for order in (cites, list(reversed(cites))):
            replies = [{"text": '{"tool":"lookup","query":"q","sources":["datacite"]}'},
                       {"text": '{"answer":"Claim [2]"}'}]
            prompts = []
            def answer(ref, prompt, **kw):
                prompts.append(prompt)
                return replies[len(prompts) - 1]
            with mock.patch.dict(S.KB_BACKENDS, {"datacite": lambda *a: {"items": order, "citations": order}}, clear=True), \
                 mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
                 mock.patch.object(S, "call_model", side_effect=answer):
                result = S.tool_agent({"task": "q", "max_steps": 2})
            self.assertEqual(result["citation_count"], 2)
            agent = result["agents"][0]
            self.assertTrue(agent["cited_ok"])
            self.assertEqual(agent["injected_citations"], [1, 2])
            self.assertEqual({c["doi"] for c in agent["citations"]}, {"10.1234/x.1", "10.1234/x.2"})
            self.assertIn("Body version one", prompts[1])
            self.assertIn("Body version two", prompts[1])

    def test_direct_register_guard_rejects_a_metadata_tool_result(self):
        replies = [{"text": '{"tool":"lookup","query":"q"}'}, {"text": '{"answer":"Claim [1]"}'}]
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=replies), \
             mock.patch.object(S, "_agent_tool_call", return_value={"hits": 1, "brief": ["Only title"],
                 "citations": [{"source": "datacite", "url": "https://example.org/x", "summary": "", "metadata_only": True}]}):
            got = S.tool_agent({"task": "q", "max_steps": 2})
        self.assertEqual(got["citation_count"], 0)
        self.assertFalse(got["agents"][0]["cited_ok"])


class TestEvidenceBudgetSafety(unittest.TestCase):
    def test_grounded_only_accepts_numbers_whose_body_was_injected(self):
        cites = [{"source": "datacite", "title": f"T{i}", "url": f"https://example.org/{i}",
                  "summary": f"BODY_{i}_" + "a" * 600} for i in range(1, 5)]
        prompts = []
        def answer(refs, prompt, **kw):
            prompts.append(prompt)
            return [{"text": "Claim [4]", "served_by": "nous/a"}]
        with mock.patch.object(S, "_EVIDENCE_ITEM_CHARS", 10), \
             mock.patch.object(S, "_EVIDENCE_TOTAL_CHARS", 30), \
             mock.patch.object(S, "knowledge_lookup", return_value={"citations": cites}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many", side_effect=answer):
            result = S.tool_grounded({"question": "q"})
        self.assertFalse(result["answers"][0]["cited_ok"])
        self.assertEqual(result["answers"][0]["unsupported_citations"], [4])
        self.assertEqual(result["injected_citations"], [1, 2, 3])
        self.assertEqual(result["evidence_citation_count"], 3)
        self.assertIn("根拠 3 件", S.render("freeagent_grounded", result))
        self.assertNotIn("[4] T4", prompts[0])

    def test_agent_only_accepts_numbers_whose_body_was_injected(self):
        cites = [{"source": "datacite", "title": f"T{i}", "url": f"https://example.org/{i}",
                  "summary": f"BODY_{i}_" + "a" * 600} for i in range(1, 7)]
        replies = [{"text": '{"tool":"lookup","query":"q"}'}, {"text": '{"answer":"Claim [5]"}'}]
        prompts = []
        def answer(ref, prompt, **kw):
            prompts.append(prompt)
            return replies[len(prompts) - 1]
        with mock.patch.object(S, "_agent_tool_call", return_value={"citations": cites, "brief": [], "hits": 6}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=answer):
            result = S.tool_agent({"task": "q", "max_steps": 2})
        agent = result["agents"][0]
        self.assertFalse(agent["cited_ok"])
        self.assertEqual(agent["unsupported_citations"], [5])
        self.assertEqual(agent["injected_citations"], [1, 2, 3, 4])
        self.assertNotIn("[5] T5", prompts[1])

    def test_agent_can_inject_previously_omitted_numbers_on_a_later_step(self):
        cites = [{"source": "datacite", "title": f"T{i}", "url": f"https://example.org/{i}",
                  "summary": f"BODY_{i}_" + "a" * 600} for i in range(1, 7)]
        replies = [{"text": '{"tool":"lookup","query":"q"}'},
                   {"text": '{"tool":"lookup","query":"q"}'}, {"text": '{"answer":"Claim [5]"}'}]
        prompts = []
        def answer(ref, prompt, **kw):
            prompts.append(prompt)
            return replies[len(prompts) - 1]
        with mock.patch.object(S, "_agent_tool_call", return_value={"citations": cites, "brief": [], "hits": 6}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=answer):
            result = S.tool_agent({"task": "q", "max_steps": 3})
        agent = result["agents"][0]
        self.assertTrue(agent["cited_ok"])
        self.assertEqual(agent["cited"], [5])
        self.assertEqual(agent["injected_citations"], [1, 2, 3, 4, 5, 6])
        self.assertIn("[5] T5", prompts[2])
        self.assertEqual(len(agent["citations"]), 6)

    def test_zero_evidence_budget_does_not_start_inference(self):
        cites = [{"title": "Paper", "url": "https://example.org/x", "summary": "Body"}]
        with mock.patch.object(S, "_EVIDENCE_TOTAL_CHARS", 0), \
             mock.patch.object(S, "knowledge_lookup", return_value={"citations": cites}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many") as infer:
            result = S.tool_grounded({"question": "q"})
        self.assertIn("error", result)
        infer.assert_not_called()


class TestKnowledgeProbeContract(unittest.TestCase):
    def test_probe_rejects_bad_values_and_checks_fallback_permission(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "probe_knowledge_stdio.py")
        spec = importlib.util.spec_from_file_location("probe_extensions", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(getattr(mod, "validate_lookup", None)), "probe validator is missing")
        cite = {"source": "datacite", "title": "Paper", "url": "https://example.org/x", "year": 2025, "summary": "Body"}
        data = {"sources": ["datacite"], "results": {"datacite": {"source": "datacite", "items": [cite]}},
                "citations": [cite], "citation_count": 1}
        self.assertEqual(mod.validate_lookup(data, ["datacite"], False), [cite])
        for field, value in (("url", "h"), ("source", "github"), ("title", ""), ("summary", 42)):
            broken = json.loads(json.dumps(data))
            broken["citations"][0][field] = value
            with self.assertRaises(ValueError):
                mod.validate_lookup(broken, ["datacite"], False)
        alternate = {"sources": ["arxiv"], "results": {"arxiv": {"source": "datacite", "items": [cite],
                     "fallback": {"requested_source": "arxiv", "served_by": "datacite", "primary_error": "HTTP 429"}}},
                     "citations": [cite], "citation_count": 1}
        self.assertEqual(mod.validate_lookup(alternate, ["arxiv"], True), [cite])
        with self.assertRaises(ValueError):
            mod.validate_lookup(alternate, ["arxiv"], False)
        alternate["results"]["arxiv"]["fallback"]["served_by"] = "openaire"
        with self.assertRaises(ValueError):
            mod.validate_lookup(alternate, ["arxiv"], True)


class TestMeasurementOptions(unittest.TestCase):
    def test_selected_mode_and_summary_counts_without_bodies(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "measure_kb.py")
        spec = importlib.util.spec_from_file_location("measure_extensions", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        backend = mock.Mock(return_value={"items": [{"title": "Private title"}],
                           "citations": [{"summary": "Private body"}]})
        with mock.patch.dict(S.KB_BACKENDS, {"datacite": backend}, clear=True):
            rows = mod.measure_round(0, sources=["datacite"], datacite_kind="dataset")
        self.assertEqual(backend.call_args.args[2]["datacite_kind"], "dataset")
        self.assertEqual(rows[0]["summaries"], 1)
        self.assertEqual(rows[0]["datacite_kind"], "dataset")
        self.assertNotIn("Private", json.dumps(rows))


class TestKnowledgeIntegration(unittest.TestCase):
    def test_duplicate_doi_keeps_acquisition_providers_and_best_body(self):
        a = {"source": "datacite", "title": "Paper", "url": "https://doi.org/10.1/X", "doi": "10.1/X", "summary": "", "metadata_only": True}
        b = {"source": "openaire", "title": "Paper", "url": "https://example.org/1", "doi": "10.1/x", "summary": "Actual abstract", "metadata_only": False}
        with mock.patch.dict(S.KB_BACKENDS, {
            "datacite": lambda *a_: {"items": [], "citations": [a]},
            "openaire": lambda *a_: {"items": [], "citations": [b]}}, clear=True):
            out = S.knowledge_lookup("q", ["datacite", "datacite", "openaire"])
        self.assertEqual(out["citation_count"], 1)
        self.assertEqual(out["sources"], ["datacite", "openaire"])
        self.assertEqual(out["citations"][0]["providers"], ["datacite", "openaire"])
        self.assertEqual(out["citations"][0]["summary"], "Actual abstract")
        self.assertFalse(out["citations"][0]["metadata_only"])
        self.assertNotIn("providers", a)

    def test_ambiguous_url_assignment_is_independent_of_input_order(self):
        import itertools
        rows = [
            {"source": "unknown", "url": "https://example.org/x", "summary": "Unassigned body"},
            {"source": "v1", "doi": "10.1234/x.1", "url": "https://example.org/x", "summary": "A"},
            {"source": "v2", "doi": "10.1234/x.2", "url": "https://example.org/x", "summary": "B"}]
        for order in itertools.permutations(rows):
            result = S._kb_merge_citations(list(order))
            self.assertEqual(len(result), 3, [c["source"] for c in order])
            unassigned = next(c for c in result if c["source"] == "unknown")
            self.assertNotIn("doi", unassigned)
        aliases = [{"source": "unknown", "url": "https://example.org/x", "summary": "Body",
                    "aliases": {"urls": ["https://example.org/x", "https://example.org/y"]}},
                   {"source": "v1", "doi": "10.1234/x.1", "url": "https://example.org/x", "summary": "A"},
                   {"source": "v2", "doi": "10.1234/x.2", "url": "https://example.org/y", "summary": "B"}]
        for order in itertools.permutations(aliases):
            self.assertEqual(len(S._kb_merge_citations(list(order))), 3)

    def test_chained_ambiguous_aliases_never_assign_unknown_bodies_to_a_version(self):
        import itertools
        rows = [
            {"source": "v1", "doi": "10.1234/x.1", "url": "https://example.org/u1", "summary": "Version one"},
            {"source": "v2", "doi": "10.1234/x.2", "url": "https://example.org/u2", "summary": "Version two"},
            {"source": "unknown1", "url": "https://example.org/u1", "summary": "Long unassigned one body",
             "aliases": {"urls": ["https://example.org/u1", "https://example.org/bridge"]}},
            {"source": "unknown2", "url": "https://example.org/u2", "summary": "Long unassigned two body",
             "aliases": {"urls": ["https://example.org/u2", "https://example.org/bridge"]}}]
        for order in itertools.permutations(rows):
            result = S._kb_merge_citations(list(order))
            self.assertEqual(len(result), 3, [c["source"] for c in order])
            by_doi = {c.get("doi"): c for c in result if c.get("doi")}
            self.assertEqual(by_doi["10.1234/x.1"]["summary"], "Version one")
            self.assertEqual(by_doi["10.1234/x.2"]["summary"], "Version two")
            unknown = next(c for c in result if not c.get("doi"))
            self.assertEqual(set(unknown["providers"]), {"unknown1", "unknown2"})

    def test_remerging_preserves_identifiers_independently_of_selected_body(self):
        a = {"source": "a", "doi": "10.1234/x", "url": "https://example.org/a", "summary": "A"}
        b = {"source": "b", "url": "https://example.org/a", "summary": "Longer body"}
        c = {"source": "c", "doi": "10.1234/x", "url": "https://example.org/c", "summary": "C"}
        first = S._kb_merge_citations([a, b])
        self.assertEqual(first[0]["doi"], "10.1234/x")
        self.assertEqual(len(S._kb_merge_citations(first + [c])), 1)
        self.assertEqual(len(S._kb_merge_citations([a, b, c])), 1)
        self.assertEqual(first[0]["aliases"]["urls"], ["https://example.org/a"])
        self.assertEqual(S._kb_merge_citations(first)[0]["provider_metadata"], first[0]["provider_metadata"])

    def test_url_aliases_merge_missing_doi_and_bridge_groups(self):
        rows = [
            {"source": "crossref", "doi": "10.1234/x", "url": "https://doi.org/10.1234/x", "summary": "A"},
            {"source": "openalex", "url": "https://doi.org/10.1234/x", "summary": "B"},
            {"source": "openaire", "url": "https://example.org/x", "summary": "C"},
            {"source": "datacite", "doi": "10.1234/x", "url": "https://example.org/x", "summary": "Long body"}]
        got = S._kb_merge_citations(rows)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["providers"], ["crossref", "openalex", "openaire", "datacite"])
        self.assertEqual(got[0]["summary"], "Long body")
        versions = S._kb_merge_citations([
            {"source": "a", "doi": "10.1234/x.1", "url": "https://example.org/x", "summary": "A"},
            {"source": "b", "doi": "10.1234/x.2", "url": "https://example.org/x", "summary": "B"}])
        self.assertEqual(len(versions), 2)

    def test_merged_empty_body_stays_metadata_only(self):
        rows = [{"source": "crossref", "title": "Only title", "doi": "10.1234/x", "url": "https://doi.org/10.1234/x", "summary": ""},
                {"source": "datacite", "title": "Only title", "doi": "10.1234/x", "url": "https://doi.org/10.1234/x", "summary": "", "metadata_only": True}]
        merged = S._kb_merge_citations(rows)
        self.assertTrue(merged[0]["metadata_only"])
        with mock.patch.object(S, "knowledge_lookup", return_value={"citations": merged}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})) as select:
            result = S.tool_grounded({"question": "q"})
        self.assertIn("根拠", result["error"])
        select.assert_not_called()
        self.assertEqual(S._evidence_block(merged), "")

    def test_merged_body_origin_keeps_provider_specific_license_and_credit(self):
        got = S._kb_merge_citations([
            {"source": "openaire", "doi": "10.1234/x", "url": "https://example.org/x", "summary": "Short",
             "attribution": "OpenAIRE CC-BY", "licenses": ["CC BY NC ND"], "publication_types": ["Preprint"]},
            {"source": "europepmc", "doi": "10.1234/x", "url": "https://example.org/y", "summary": "Longer actual body",
             "license": "cc-by"}])[0]
        self.assertEqual(got["summary_source"], "europepmc")
        self.assertEqual(got["license"], "cc-by")
        self.assertNotIn("licenses", got)
        self.assertIn("OpenAIRE CC-BY", got["attributions"])
        info = next(p for p in got["provider_metadata"] if p["source"] == "openaire")
        self.assertEqual(info["licenses"], ["CC BY NC ND"])
        self.assertEqual(info["publication_types"], ["Preprint"])
        self.assertIn("OpenAIRE CC-BY", S._evidence_block([got]))
        self.assertIn("OpenAIRE CC-BY", S.render("freeagent_grounded", {"citations": [got], "citation_count": 1}))

    def test_grounded_and_agent_forward_new_options(self):
        for handler, args in ((S.tool_grounded, {"question": "q"}),
                              (S._agent_tool_call, {"tool": "lookup", "query": "q"})):
            with mock.patch.object(S, "knowledge_lookup", return_value={"citations": [], "results": {}}) as lookup:
                handler({**args, "sources": ["datacite"], "datacite_kind": "dataset", "fallback": True})
            self.assertEqual(lookup.call_args.kwargs["datacite_kind"], "dataset")
            self.assertTrue(lookup.call_args.kwargs["fallback"])

    def test_delayed_alternate_thread_start_rechecks_deadline_inside_worker(self):
        real_start = threading.Thread.start
        completed = threading.Event()
        def delayed_start(thread):
            if getattr(thread, "_args", ()) and thread._args[0] == "alternate":
                time.sleep(0.07)
                real_start(thread)
                thread.join(1)
                completed.set()
            else:
                real_start(thread)
        alternate = mock.Mock(return_value={"items": [{"title": "Wrong late fetch"}], "citations": []})
        with mock.patch.dict(S.KB_BACKENDS, {"arxiv": lambda *a: {"error": "HTTP 429"},
                                           "datacite": alternate}, clear=True), \
             mock.patch.object(threading.Thread, "start", delayed_start):
            result = S.knowledge_lookup("q", ["arxiv"], deadline=0.02, fallback=True)
            self.assertTrue(completed.wait(1))
            alternate.assert_not_called()
            self.assertEqual(result["timed_out"], ["arxiv"])

    def test_short_deadline_does_not_start_an_alternate_after_return(self):
        stop = threading.Event()
        done = threading.Event()
        def slow(*a):
            stop.wait(1)
            done.set()
            return {"error": "TimeoutError"}
        with mock.patch.dict(S.KB_BACKENDS, {"arxiv": slow, "datacite": mock.Mock()}, clear=True), \
             mock.patch.object(S, "KB_HEDGE_DELAY", 0.08):
            try:
                S.knowledge_lookup("q", ["arxiv"], deadline=0.02, fallback=True)
                time.sleep(0.11)
                S.KB_BACKENDS["datacite"].assert_not_called()
            finally:
                stop.set()
                done.wait(1)
