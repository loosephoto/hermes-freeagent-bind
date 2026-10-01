"""許諾条件を確認できた第3段階ソースのオフライン回帰。HTTPのみ模擬。"""
import os
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


class TestOpenMetadata(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        guard = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        guard.start()
        self.addCleanup(guard.stop)

    def test_email_only_metadata_never_becomes_evidence(self):
        for address in ('user@example.org', '"user"@example.org', 'user@[192.0.2.1]',
                        '"user name"@example.org', '利用者@例え.日本', 'user(comment)@example.org'):
            with self.subTest(address=address):
                S._KB_CACHE.clear()
                payload = {"hits": {"hits": [{"doi": "10.5281/zenodo.123",
                            "metadata": {"title": "T", "description": "<p>" + address + "</p>"}}]}}
                with mock.patch.object(S, "kb_json", return_value=(payload, "")):
                    got = S.kb_zenodo("q")
                cite = got["citations"][0]
                self.assertNotIn("@", str(got))
                self.assertEqual(cite["summary"], "")
                self.assertTrue(cite["metadata_only"])
                self.assertFalse(S._kb_has_evidence(cite))
                self.assertEqual(S._evidence_block([cite]), "")

    def test_noncontent_html_never_becomes_evidence(self):
        for body in ('<script>Fabricated scientific claim</script>', '<STYLE>Fake claim</STYLE>',
                     '<template>Fake claim</template>', '<noscript>Fake claim</noscript>',
                     '<!-- Fake > claim -->', '<script>Unclosed fake claim'):
            with self.subTest(body=body):
                S._KB_CACHE.clear()
                payload = {"hits": {"hits": [{"doi": "10.5281/zenodo.123",
                            "metadata": {"title": "T", "description": body}}]}}
                with mock.patch.object(S, "kb_json", return_value=(payload, "")):
                    cite = S.kb_zenodo("q")["citations"][0]
                self.assertEqual(cite["summary"], "")
                self.assertFalse(S._kb_has_evidence(cite))
                self.assertEqual(S._evidence_block([cite]), "")
        self.assertEqual(S._open_metadata_text('<script>Fake</script><p>Real &amp; <code>&lt;T&gt;</code></p>'), 'Real & <T>')

    def test_ror_rejects_invalid_alphabet_and_checksum_without_repair(self):
        for identifier in ('https://ror.org/0iiiiiiii', 'https://ror.org/057zh3y00',
                           'https://ror.org/057zh3y96 ', 'https://ror.org/057zh3yab'):
            with self.subTest(identifier=identifier):
                S._KB_CACHE.clear()
                row = {"id": identifier, "names": [{"value": "T", "types": ["ror_display"]}], "status": "active"}
                with mock.patch.object(S, "kb_json", return_value=({"items": [row]}, "")) as http:
                    got = S.kb_ror("q")
                self.assertTrue(got.get("error"))
                self.assertFalse(got.get("citations"))
                self.assertEqual(http.call_count, 1)

    def test_merged_provider_keeps_metadata_and_file_license_separate(self):
        rows = [{"source": "zenodo", "title": "Dataset", "url": "https://zenodo.org/records/123",
                 "doi": "10.5281/zenodo.123", "summary": "Metadata", "license": "CC0-1.0",
                 "file_license": "cc-by-4.0", "access_right": "restricted", "summary_kind": "metadata_description"},
                {"source": "datacite", "title": "Dataset", "url": "https://doi.org/10.5281/zenodo.123",
                 "doi": "10.5281/zenodo.123", "summary": "Longer registered abstract body"}]
        result = S._kb_merge_citations(rows)[0]
        self.assertEqual(result["summary_source"], "datacite")
        self.assertEqual(next(m for m in result["provider_metadata"] if m["source"] == "zenodo")["file_license"], "cc-by-4.0")
        self.assertEqual(next(m for m in result["provider_metadata"] if m["source"] == "zenodo")["license"], "CC0-1.0")

    def test_bad_arguments_and_malformed_responses_do_not_leak(self):
        for fn in (S.kb_zenodo, S.kb_ror):
            with mock.patch.object(S, "kb_json") as http:
                self.assertTrue(fn(123).get("error"))
                http.assert_not_called()
            for data in (None, [], {"hits": "wrong", "items": "wrong"},
                         {"hits": {"hits": [{"metadata": "wrong"}]}},
                         {"items": [{"id": "not-a-ror-id", "names": [{"value": "Name", "types": ["ror_display"]}]}]}):
                S._KB_CACHE.clear()
                with mock.patch.object(S, "kb_json", return_value=(data, "")):
                    got = fn("q")
                self.assertTrue(got.get("error"), got)
                self.assertFalse(got.get("citations"))

    def test_title_only_records_are_not_body_evidence(self):
        records = [(S.kb_zenodo, {"hits": {"hits": [{"doi": "10.5281/zenodo.123", "metadata": {"title": "T"}}]}}),
                   (S.kb_ror, {"items": [{"id": "https://ror.org/057zh3y96", "names": [{"value": "T", "types": ["ror_display"]}]}]})]
        for fn, payload in records:
            S._KB_CACHE.clear()
            with mock.patch.object(S, "kb_json", return_value=(payload, "")):
                cite = fn("q")["citations"][0]
            self.assertTrue(cite["metadata_only"])
            self.assertFalse(S._kb_has_evidence(cite))

    def test_failures_recover_and_cached_values_are_copied(self):
        payload = {"hits": {"hits": [{"doi": "10.5281/zenodo.123", "metadata": {"title": "T"}}]}}
        with mock.patch.object(S, "kb_json", side_effect=[(None, "HTTP 503"), (payload, "")]) as http:
            self.assertIn("503", S.kb_zenodo("q")["error"])
            result = S.kb_zenodo("q")
            result["items"][0]["title"] = "Mutated"
            self.assertEqual(S.kb_zenodo("q")["items"][0]["title"], "T")
        self.assertEqual(http.call_count, 2)

    def test_ror_measurement_uses_an_institution_query(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "measure_kb.py")
        spec = importlib.util.spec_from_file_location("meter_stage3", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        seen = []
        def fake(query, limit, opts):
            seen.append(query)
            return {"items": [], "citations": []}
        with mock.patch.dict(S.KB_BACKENDS, {"ror": fake}, clear=True):
            records = mod.measure_round(index=0, sources=["ror"])
        self.assertEqual(seen, ["CERN"])
        self.assertEqual(records[0]["source"], "ror")
        self.assertNotIn("query", records[0])

    def test_probe_requires_new_source_evidence_types(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "probe_knowledge_stdio.py")
        spec = importlib.util.spec_from_file_location("probe_stage3", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for source, url in (("ror", "https://ror.org/057zh3y96"), ("zenodo", "https://zenodo.org/records/123")):
            cite = {"source": source, "title": "T", "url": url, "summary": "Body", "year": ""}
            data = {"sources": [source], "results": {source: {"source": source, "items": [{"title": "T"}]}},
                    "citations": [cite], "citation_count": 1}
            with self.assertRaises(ValueError):
                mod.validate_lookup(data, [source], False)

    def test_sources_are_opt_in_and_schema_exposes_them(self):
        self.assertIn("zenodo", S.KB_BACKENDS)
        self.assertIn("ror", S.KB_BACKENDS)
        self.assertEqual(len(S.DEFAULT_SOURCES), 6)
        for tool_name in ("freeagent_lookup", "freeagent_grounded"):
            tool = next(t for t in S.TOOLS if t["name"] == tool_name)
            self.assertIn("zenodo", tool["inputSchema"]["properties"]["sources"]["description"])
            self.assertIn("ror", tool["inputSchema"]["properties"]["sources"]["description"])
        with mock.patch.object(S, "kb_zenodo", return_value={"source": "zenodo", "items": [], "citations": []}) as zen, \
             mock.patch.object(S, "kb_ror", return_value={"source": "ror", "items": [], "citations": []}) as ror:
            got = S.tool_lookup({"query": "q", "sources": ["zenodo", "ror"]})
        self.assertEqual(got["sources"], ["zenodo", "ror"])
        self.assertEqual(zen.call_count, 1)
        self.assertEqual(ror.call_count, 1)
        self.assertIn("zenodo", S.AGENT_SYSTEM)
        self.assertIn("ror", S.AGENT_SYSTEM)

    def test_ror_facts_are_structured_and_establishment_is_not_publication_year(self):
        self.assertTrue(callable(getattr(S, "kb_ror", None)), "ROR backend missing")
        row = {"id": "https://ror.org/057zh3y96", "established": 1877, "status": "active", "types": ["education"],
               "names": [{"value": "The University of Tokyo", "types": ["ror_display"], "lang": "en"},
                         {"value": "東京大学", "types": ["label"], "lang": "ja"}],
               "locations": [{"geonames_details": {"name": "Tokyo", "country_name": "Japan"}}],
               "links": [{"type": "website", "value": "https://www.u-tokyo.ac.jp/"}]}
        with mock.patch.object(S, "kb_json", return_value=({"items": [row]}, "")) as http:
            got = S.kb_ror("東京大学", limit=1)
        self.assertFalse(got.get("error"), got)
        self.assertEqual(parse_qs(urlsplit(http.call_args.args[0]).query)["query"], ["東京大学"])
        cite = got["citations"][0]
        self.assertEqual(cite["summary_kind"], "structured_metadata")
        self.assertIn("1877", cite["summary"])
        self.assertIn("Japan", cite["summary"])
        self.assertEqual(cite["year"], "")
        self.assertEqual(cite["established"], 1877)
        self.assertEqual(cite["license"], "CC0-1.0")
        self.assertEqual(got["items"][0]["name_variants"], ["The University of Tokyo", "東京大学"])

    def test_zenodo_description_is_metadata_not_file_content(self):
        self.assertTrue(callable(getattr(S, "kb_zenodo", None)), "Zenodo backend missing")
        payload = {"hits": {"hits": [{"id": 123, "doi": "10.5281/zenodo.123", "conceptdoi": "10.5281/zenodo.100",
            "links": {"self_html": "https://zenodo.org/records/123"},
            "metadata": {"title": "Graphene dataset", "description": "<p>Experimental &amp; measured data</p>",
                         "notes": "contact user@example.org", "publication_date": "2024-01-01",
                         "resource_type": {"type": "dataset"}, "access_right": "restricted",
                         "license": {"id": "cc-by-4.0"}, "creators": [{"name": "Author", "email": "private@example.org"}]},
            "files": [{"links": {"self": "https://zenodo.org/file/private"}}]}]}}
        with mock.patch.object(S, "kb_json", return_value=(payload, "")) as http:
            got = S.kb_zenodo("graphene thermal", limit=2)
        self.assertFalse(got.get("error"), got)
        self.assertEqual(http.call_count, 1)
        args = parse_qs(urlsplit(http.call_args.args[0]).query)
        self.assertEqual(args["q"], ['"graphene" AND "thermal"'])
        self.assertEqual(args["sort"], ["bestmatch"])
        cite = got["citations"][0]
        self.assertIn("Experimental & measured data", cite["summary"])
        self.assertNotIn("@", str(got))
        self.assertNotIn("files", got["items"][0])
        self.assertEqual(cite["doi"], "10.5281/zenodo.123")
        self.assertEqual(cite["license"], "CC0-1.0")
        self.assertEqual(cite["file_license"], "cc-by-4.0")
        self.assertEqual(cite["resource_type"], "dataset")
        self.assertEqual(cite["access_right"], "restricted")
        self.assertEqual(cite["year"], "2024")
        self.assertEqual(cite["summary_kind"], "metadata_description")
