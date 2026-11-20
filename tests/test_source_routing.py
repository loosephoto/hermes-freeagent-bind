"""知識ソース自動選定（route_sources / sources=["auto"]）のオフライン回帰テスト。"""
from __future__ import annotations

import os
import unittest
from unittest import mock

from freeagent_bind import server as S


class TestSourceRouting(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                S.SOURCE_ROUTING_ENV,
                "FREEAGENT_CINII_APPID",
                "FREEAGENT_LIBRARIESIO_KEY",
                "FREEAGENT_ZBMATH_ACK",
                "FREEAGENT_QIITA_TOKEN",
            )
        }
        for k in self._saved_env:
            os.environ.pop(k, None)
        with S._KB_BLOCK_LOCK:
            self._saved_blocked = dict(S._KB_BLOCKED)
            S._KB_BLOCKED.clear()

    def tearDown(self) -> None:
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with S._KB_BLOCK_LOCK:
            S._KB_BLOCKED.clear()
            S._KB_BLOCKED.update(self._saved_blocked)

    def test_meta_covers_all_registered_backends(self) -> None:
        missing = [name for name in S.KB_BACKENDS if name not in S.SOURCE_ROUTING_META]
        self.assertEqual(missing, [], f"SOURCE_ROUTING_META に未登録のバックエンド: {missing}")

    def test_strong_identifiers(self) -> None:
        r_cve = S.route_sources("CVE-2024-3094 xz utils")
        self.assertEqual(r_cve["sources"][:3], ["osv", "nvd", "github"])
        self.assertFalse(r_cve["fallback_default"])

        r_rfc = S.route_sources("RFC 9110 HTTP Semantics")
        self.assertEqual(r_rfc["sources"][:2], ["ietf", "wikipedia"])

        r_arxiv = S.route_sources("arXiv:1706.03762")
        self.assertEqual(r_arxiv["sources"][:3], ["arxiv", "crossref", "openalex"])

        r_oeis = S.route_sources("A000045 Fibonacci")
        self.assertEqual(r_oeis["sources"][:2], ["oeis", "wikipedia"])

    def test_domain_keywords_and_gate_filters(self) -> None:
        # Web standards -> mdn
        r_web = S.route_sources("CSS popover anchor positioning")
        self.assertIn("mdn", r_web["sources"])

        # npm without LIBRARIESIO key -> librariesio skipped
        r_npm = S.route_sources("npm package.json react")
        self.assertIn("npm", r_npm["sources"])
        self.assertNotIn("librariesio", r_npm["sources"])
        self.assertIn("librariesio", r_npm["skipped"])

        # With LIBRARIESIO key -> included
        os.environ["FREEAGENT_LIBRARIESIO_KEY"] = "dummy-key"
        r_npm2 = S.route_sources("npm package.json react")
        self.assertIn("librariesio", r_npm2["sources"])

    def test_qiita_skipped_when_unauthenticated_and_included_with_token(self) -> None:
        r1 = S.route_sources("Hacker News discussion about Rust")
        self.assertIn("hn", r1["sources"])
        self.assertNotIn("qiita", r1["sources"])
        self.assertIn("qiita", r1["skipped"])

        os.environ["FREEAGENT_QIITA_TOKEN"] = "tok-123"
        r2 = S.route_sources("Hacker News discussion about Rust")
        self.assertIn("qiita", r2["sources"])

    def test_cjk_only_query_skips_en_only_sources(self) -> None:
        r = S.route_sources("量子計算の論文")
        self.assertNotIn("arxiv", r["sources"])
        self.assertIn("arxiv", r["skipped"])
        self.assertIn("英語クエリ必須", r["skipped"]["arxiv"])
        # CJK academic still hits crossref / openalex / wikipedia
        self.assertTrue(any(s in r["sources"] for s in ("crossref", "openalex", "wikipedia")))

    def test_zbmath_consent_gate_and_need_evidence(self) -> None:
        r1 = S.route_sources("local smoothing conjecture zbmath")
        self.assertNotIn("zbmath", r1["sources"])
        self.assertIn("zbmath", r1["skipped"])
        # Pure math conjecture should not pull particle physics (inspirehep)
        self.assertNotIn("inspirehep", r1["sources"])
        self.assertIn("arxiv", r1["sources"])
        self.assertIn("openalex", r1["sources"])

        os.environ["FREEAGENT_ZBMATH_ACK"] = "1"
        r2 = S.route_sources("local smoothing conjecture zbmath", need_evidence=False)
        self.assertIn("zbmath", r2["sources"])

        # When need_evidence=True (grounded), zbmath is metadata_only -> skipped
        r3 = S.route_sources("local smoothing conjecture zbmath", need_evidence=True)
        self.assertNotIn("zbmath", r3["sources"])
        self.assertIn("zbmath", r3["skipped"])

    def test_physics_and_sequence_domains_separated(self) -> None:
        r_phys = S.route_sources("cosmic birefringence dark matter")
        self.assertFalse(r_phys["fallback_default"])
        self.assertEqual(r_phys["sources"], ["inspirehep", "arxiv", "openalex", "wikipedia"])

        r_seq = S.route_sources("integer sequence Fibonacci")
        self.assertEqual(r_seq["sources"][:2], ["oeis", "arxiv"])
        self.assertNotIn("inspirehep", r_seq["sources"])

    def test_blocked_host_is_skipped(self) -> None:
        S._kb_block("developer.mozilla.org", secs=60.0)
        r = S.route_sources("CSS popover")
        self.assertNotIn("mdn", r["sources"])
        self.assertIn("mdn", r["skipped"])

    def test_fallback_to_default_sources_when_unmatched(self) -> None:
        r = S.route_sources("foobarbaz qux")
        self.assertTrue(r["fallback_default"])
        self.assertEqual(r["sources"], list(S.DEFAULT_SOURCES))

    def test_knowledge_lookup_auto_and_mixed_and_env(self) -> None:
        seen_picked: list[list[str]] = []

        def fake_gather(picked, query, limit, opts, deadline):
            seen_picked.append(list(picked))
            return ({src: {"source": src, "items": [], "citations": [], "error": ""} for src in picked},
                    {src: 0.01 for src in picked})

        with mock.patch.object(S, "_kb_gather", side_effect=fake_gather):
            # 1. Default omitted (env off) -> DEFAULT_SOURCES, routed not set
            out_default = S.knowledge_lookup("CVE-2024-3094")
            self.assertEqual(out_default["sources"], list(S.DEFAULT_SOURCES))
            self.assertNotIn("routed", out_default)

            # 2. Explicit sources=["auto"] -> routed to osv/nvd/github, "auto" not in unknown_sources
            out_auto = S.knowledge_lookup("CVE-2024-3094", sources=["auto"])
            self.assertEqual(out_auto["sources"], ["osv", "nvd", "github"])
            self.assertTrue(out_auto.get("routed"))
            self.assertEqual(out_auto["unknown_sources"], [])
            self.assertIn("osv", out_auto["routing"]["reasons"])

            # 3. Mixed ["auto", "hn"] -> merges explicit source without duplicate
            out_mix = S.knowledge_lookup("CVE-2024-3094", sources=["auto", "hn"])
            self.assertEqual(out_mix["sources"], ["osv", "nvd", "github", "hn"])

            # 4. Env FREEAGENT_SOURCE_ROUTING=auto with omitted sources
            os.environ[S.SOURCE_ROUTING_ENV] = "auto"
            out_env = S.knowledge_lookup("RFC 9110")
            self.assertEqual(out_env["sources"], ["ietf", "wikipedia"])
            self.assertTrue(out_env.get("routed"))

            # 5. Renderer shows routing summary
            rendered = S._render_lookup(out_auto)
            self.assertIn("自動選定", rendered)

    def test_grounded_propagates_routing_and_agent_passes_auto_lang(self) -> None:
        # 1. tool_grounded propagates routing metadata on 0-citation early return
        def fake_gather_empty(picked, query, limit, opts, deadline):
            return ({src: {"source": src, "items": [], "citations": [], "error": "該当なし"} for src in picked},
                    {src: 0.01 for src in picked})

        with mock.patch.object(S, "_kb_gather", side_effect=fake_gather_empty):
            res = S.tool_grounded({"question": "CVE-2024-3094 について", "search_query": "CVE-2024-3094", "sources": ["auto"]})
            self.assertIn("lookup", res)
            self.assertTrue(res["lookup"].get("routed"))
            self.assertEqual(res["lookup"]["sources"], ["osv", "nvd", "github"])

        # 2. _agent_tool_call passes lang="" by default so wikipedia auto-detects language
        captured = {}

        def fake_lookup(query, sources=None, **kwargs):
            captured["lang"] = kwargs.get("lang")
            return {"results": {}, "citations": [], "errors": {}}

        with mock.patch.object(S, "knowledge_lookup", side_effect=fake_lookup):
            S._agent_tool_call({"tool": "lookup", "query": "Transformer"})
            self.assertEqual(captured["lang"], "")


if __name__ == "__main__":
    unittest.main()
