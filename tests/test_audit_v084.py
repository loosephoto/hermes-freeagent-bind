"""v0.8.4 の P4（引用の根拠充填率）の回帰テスト。

P4 の主張:
1. crossref は `need_evidence=True`（= grounded）で `filter=has-abstract:true` を付ける。
   → 根拠の充填率が **27% → 100%**（実測 9 クエリ × 5 件）。
2. hn は `need_evidence=True` で**コメントも検索**する。
   → 議論本文が根拠になり、充填率が **0% → 100%**。
3. `lookup`（根拠が要らない場面）は** Traditional のまま**（書誌の網羅性を保つ）。
4. キャッシュキーに根拠の有無が入る（同じクエリでも結果を共有しない）。
5. hn の comment 検索は**別予算キー**を使う（同一ホストの 2 連打で落とさない）。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-v084-test-"))
from freeagent_bind import server as S  # noqa: E402


class EvidenceFillRateTest(unittest.TestCase):
    """根拠の充填率（need_evidence の効果）。"""

    def setUp(self):
        S._KB_CACHE.clear()
        S._KB_RATE_NEXT.clear()

    def test_crossref_need_evidence_sets_has_abstract_filter(self):
        """crossref は根拠が要る場面だけ has-abstract フィルタを付ける。"""
        seen = []

        def fake(url, interval, payload=None, budget_key="", extra_headers=None):
            seen.append(url)
            return {"message": {"items": []}}, ""

        with mock.patch.object(S, "_kb_new_json", side_effect=fake):
            S.kb_crossref("llm", limit=3, need_evidence=True)
            S.kb_crossref("llm", limit=3, need_evidence=False)
        self.assertEqual(len(seen), 2)
        self.assertIn("filter=has-abstract%3Atrue", seen[0])
        self.assertNotIn("filter=has-abstract", seen[1])

    def test_hn_need_evidence_searches_comments(self):
        """hn は根拠が要る場面だけ comment も検索する。"""
        seen = []

        def fake(url, interval, payload=None, budget_key="", extra_headers=None):
            seen.append(url)
            if "tags=comment" in url:
                return {"hits": [{"objectID": "111", "story_title": "T",
                                  "comment_text": "議論の本文がここにある",
                                  "created_at": "2024-01-02T03:04:05Z",
                                  "author": "alice", "story_id": "222"}]}, ""
            return {"hits": [{"objectID": "333", "title": "Story",
                              "story_text": "", "created_at": "2024-01-02T03:04:05Z",
                              "author": "bob"}]}, ""

        with mock.patch.object(S, "_kb_new_json", side_effect=fake):
            r_on = S.kb_hn("rust", limit=5, need_evidence=True)
            r_off = S.kb_hn("rust", limit=5, need_evidence=False)
        self.assertTrue(any("tags=comment" in u for u in seen))
        cites = r_on.get("citations") or []
        self.assertTrue(cites)
        self.assertEqual(cites[0]["kind"], "hn-comment")
        self.assertTrue(S._kb_has_evidence(cites[0]))
        # 根拠が要らない場面では comment を叩かない
        off_cites = r_off.get("citations") or []
        self.assertTrue(off_cites)
        self.assertEqual(off_cites[0]["kind"], "hn-story")

    def test_hn_comment_uses_separate_budget_key(self):
        """同一ホストの 2 連打で落とさないため、comment 側は別予算キーを使う。"""
        keys = []

        def fake(url, interval, payload=None, budget_key="", extra_headers=None):
            keys.append(budget_key)
            return {"hits": []}, ""

        with mock.patch.object(S, "_kb_new_json", side_effect=fake):
            S.kb_hn("rust", limit=3, need_evidence=True)
        self.assertTrue(any(k for k in keys), keys)
        self.assertTrue(any(k != "hn.algolia.com" for k in keys), keys)

    def test_cache_key_separates_evidence_modes(self):
        """同じクエリでも根拠の有無でキャッシュを共有しない。"""
        keys = []
        orig = S._kb_cached

        def spy(key, producer):
            keys.append(key)
            return orig(key, producer)

        with mock.patch.object(S, "_kb_cached", side_effect=spy):
            S.kb_crossref("llm", limit=3, need_evidence=True)
            S.kb_crossref("llm", limit=3, need_evidence=False)
        self.assertNotEqual(keys[0], keys[1], keys)

    def test_knowledge_lookup_passes_need_evidence_through(self):
        """knowledge_lookup が need_evidence を opts 传到 backend へ渡す。"""
        seen = {}

        def fake(q, limit, opts):
            seen.update(opts)
            return {"source": "crossref", "citations": []}

        with mock.patch.dict(S.KB_BACKENDS, {"crossref": fake}):
            S.knowledge_lookup("llm", ["crossref"], limit=2, need_evidence=True)
        self.assertIs(seen.get("need_evidence"), True)
        with mock.patch.dict(S.KB_BACKENDS, {"crossref": fake}):
            S.knowledge_lookup("llm", ["crossref"], limit=2)
        self.assertIs(seen.get("need_evidence"), False)

    def test_grounded_requests_evidence(self):
        """grounded は need_evidence=True で知識 layer を呼ぶ。"""
        seen = {}

        def fake(query, sources=None, **kw):
            seen.update(kw)
            return {"citations": [], "sources": {}}

        with mock.patch.object(S, "knowledge_lookup", side_effect=fake):
            S.tool_grounded({"question": "llm の根拠"})
        self.assertIs(seen.get("need_evidence"), True)


class DescriptionTest(unittest.TestCase):
    """sources 説明に arXiv の「英語必須」が書いてある。"""

    def test_arxiv_english_only_is_documented(self):
        for name in ("freeagent_lookup", "freeagent_grounded"):
            tool = next(t for t in S.TOOLS if t["name"] == name)
            desc = tool["inputSchema"]["properties"]["sources"]["description"]
            self.assertIn("arxiv", desc.lower())
            self.assertIn("英語", desc, f"{name} の sources 説明に arXiv の英語必須がない")


class OfflineEvidenceTest(unittest.TestCase):
    """上游を叩かずに、根拠の判定と構造だけ確認する（CI で安定して走る）。"""

    def test_has_evidence_requires_body(self):
        self.assertTrue(S._kb_has_evidence({"summary": "本文がある"}))
        self.assertFalse(S._kb_has_evidence({"summary": ""}))
        self.assertFalse(S._kb_has_evidence({}))

    def test_hn_comment_shape_keeps_story_link(self):
        """コメント引用は上位 story の ID を残す（辿れるように）。"""
        data = {"hits": [{"objectID": "111", "story_title": "題",
                          "comment_text": "本文", "created_at": "2024-01-02T03:04:05Z",
                          "author": "alice", "story_id": "222", "parent_id": "333"}]}
        with mock.patch.object(S, "_kb_new_json", return_value=(data, "")):
            r = S.kb_hn("rust", limit=3, need_evidence=True)
        cites = r.get("citations") or []
        self.assertTrue(cites)
        self.assertEqual(cites[0]["extra"].get("story_id"), "222")
        self.assertIn("item?id=111", cites[0]["url"])


if __name__ == "__main__":
    unittest.main()
