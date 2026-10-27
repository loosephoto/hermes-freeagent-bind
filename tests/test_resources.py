"""§11 MCP Resources と引用の重複排除（v0.22.0）の回帰テスト.

固定する契約（実測の型をそのまま書く）:

* `initialize` が `capabilities.resources` を宣言する（Hermes は capabilities を見て
  `list_resources` / `read_resource` を生やす＝実測: hermes-agent の mcp_tool_registration.py）
* `resources/list` の目録と `resources/read` の本文が**実装の登録簿から**作られる
  （ソース数・プロバイダ数を手で書かない）
* 未知の URI は -32002（空文字で成功にしない）／`read` はネットワークを出さない
* `knowledge_lookup` は引用をトップレベルの `citations` に統合し、`results[source]` の中へ
  二重に置かない（`citations_in_results=False` で明示・件数は変わらない）

ネットワークは使わない（知識取得はスタブ）。
"""

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


class ResourcesTest(unittest.TestCase):
    def test_initialize_declares_resources_capability(self):
        replies = S._replies_for({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
        caps = replies[0]["result"]["capabilities"]
        self.assertIn("resources", caps)
        self.assertFalse(caps["resources"]["subscribe"])
        # 既存の宣言を落としていない（logging は notifications/message を送るので MUST）
        self.assertIn("tools", caps)
        self.assertIn("logging", caps)

    def test_list_matches_registry(self):
        replies = S._replies_for({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
        rows = replies[0]["result"]["resources"]
        self.assertEqual([r["uri"] for r in rows],
                         [S.RESOURCE_SCHEME + name for name in S._RESOURCE_BUILDERS])
        for row in rows:
            self.assertTrue(row["uri"].startswith(S.RESOURCE_SCHEME))
            self.assertEqual(row["mimeType"], "text/markdown")
            self.assertTrue(row["title"])

    def test_read_returns_text_for_every_resource(self):
        for name in S._RESOURCE_BUILDERS:
            uri = S.RESOURCE_SCHEME + name
            reply = S._replies_for({"jsonrpc": "2.0", "id": 3, "method": "resources/read",
                                    "params": {"uri": uri}})[0]
            block = reply["result"]["contents"][0]
            self.assertEqual(block["uri"], uri)
            self.assertEqual(block["mimeType"], S.RESOURCE_MIME)
            self.assertGreater(len(block["text"]), 100)

    def test_sources_resource_counts_come_from_registry(self):
        text = S._resource_sources()
        self.assertIn(f"{len(S.KB_BACKENDS)} 種", text)
        self.assertIn(f"{len(S.DEFAULT_SOURCES)} 種", text)
        for name in S.DEFAULT_SOURCES:
            self.assertIn(name, text)

    def test_providers_resource_lists_every_provider_without_values(self):
        text = S._resource_providers()
        for name in S.PROVIDER_ORDER:
            self.assertIn(name, text)
        self.assertIn("値はここに出さない", text)

    def test_unknown_uri_is_resource_not_found(self):
        for uri in ("resource://freeagent/nope", "https://example.com/x", ""):
            reply = S._replies_for({"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                                    "params": {"uri": uri}})[0]
            self.assertEqual(reply["error"]["code"], -32002, uri)

    def test_read_never_touches_the_network(self):
        with mock.patch.object(S, "_kb_new_json", side_effect=AssertionError("HTTP を出した")), \
             mock.patch.object(S, "provider_http", side_effect=AssertionError("HTTP を出した")), \
             mock.patch.object(S, "_catalog_prefetch", side_effect=AssertionError("一覧を取りに行った")):
            for name in S._RESOURCE_BUILDERS:
                self.assertGreater(len(S.resource_read(S.RESOURCE_SCHEME + name) or ""), 100)


class CitationsNotDuplicatedTest(unittest.TestCase):
    """引用はトップレベルの `citations` に 1 回だけ置く（二重送信をやめる）。"""

    CITE = {"source": "wikipedia", "title": "強化学習",
            "url": "https://ja.wikipedia.org/wiki/%E5%BC%B7%E5%8C%96%E5%AD%A6%E7%BF%92",
            "summary": "強化学習（きょうかがくしゅう）は…"}

    def _stub(self, query, limit, opts):
        return {"source": "wikipedia", "items": [{"title": "強化学習", "url": self.CITE["url"]}],
                "citations": [dict(self.CITE)], "error": ""}

    def test_results_do_not_carry_citations(self):
        with mock.patch.dict(S.KB_BACKENDS, {"wikipedia": self._stub}, clear=False):
            out = S.knowledge_lookup("強化学習", ["wikipedia"], limit=1)
        self.assertFalse(out["citations_in_results"])
        self.assertEqual(out["citation_count"], 1)
        self.assertEqual(out["citations"][0]["url"], self.CITE["url"])
        self.assertNotIn("citations", out["results"]["wikipedia"])
        # 状態は残す（timed_out / error / fallback を落とさない）
        self.assertIn("items", out["results"]["wikipedia"])
        self.assertIn("error", out["results"]["wikipedia"])

    def test_content_mentions_the_merge(self):
        with mock.patch.dict(S.KB_BACKENDS, {"wikipedia": self._stub}, clear=False):
            out = S.knowledge_lookup("強化学習", ["wikipedia"], limit=1)
        text = S._render_lookup(out)
        self.assertIn("citations に統合済み", text)

    def test_structured_payload_is_smaller_than_before(self):
        """二重化していた分だけ小さくなる（同じ入力を 2 通りに組み立てて比べる）。"""
        with mock.patch.dict(S.KB_BACKENDS, {"wikipedia": self._stub}, clear=False):
            out = S.knowledge_lookup("強化学習", ["wikipedia"], limit=1)
        thin = len(json.dumps(out, ensure_ascii=False))
        fat = dict(out)
        fat["results"] = {k: {**v, "citations": [dict(self.CITE)]} for k, v in out["results"].items()}
        self.assertLess(thin, len(json.dumps(fat, ensure_ascii=False)))


if __name__ == "__main__":
    unittest.main()
