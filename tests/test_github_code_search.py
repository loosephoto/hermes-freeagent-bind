"""GitHub コード検索の契約（オフライン）。

実測（2026-10）: code 検索は認証必須で 10 req/分（search は 30/分）、返るのは
ファイルへのポインタと `text_matches.fragment`（断片）だけで、ライセンス情報は含まない。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


CODE_PAYLOAD = {
    "total_count": 1,
    "items": [{
        "name": "retry.py",
        "path": "src/app/retry.py",
        "html_url": "https://github.com/o/r/blob/abc/src/app/retry.py",
        "text_matches": [
            {"fragment": "    def backoff(n: int) -> List<String>:\n        return sleep(n)",
             "matches": [{"text": "backoff", "indices": [4, 11]}],
             "property": "content", "object_type": "File", "object_url": "u"},
            {"fragment": "    retries = 3", "matches": [], "property": "content"},
        ],
        "repository": {"full_name": "o/r", "html_url": "https://github.com/o/r"},
    }],
}

REPO_PAYLOAD = {"items": [{"full_name": "o/r", "html_url": "https://github.com/o/r",
                           "description": "Repo description"}]}


class TestGitHubCodeSearch(unittest.TestCase):
    def setUp(self):
        S._KB_CACHE.clear()
        self.rate = mock.patch.object(S, "_kb_rate_acquire", return_value=0.0)
        self.rate.start()
        self.addCleanup(self.rate.stop)

    def test_code_kind_requests_text_match_and_returns_snippet_and_repo_url(self):
        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_new_json", return_value=(CODE_PAYLOAD, "")) as http:
            got = S.kb_github("backoff helper", kind="code", limit=1)
        kwargs = http.call_args.kwargs
        self.assertEqual(kwargs["extra_headers"]["Accept"], "application/vnd.github.text-match+json")
        self.assertEqual(kwargs["budget_key"], "api.github.com/search/code")
        self.assertEqual(kwargs["extra_headers"]["Authorization"], "Bearer tok")
        item = got["items"][0]
        self.assertEqual(item["repository"], "o/r")
        self.assertEqual(item["repo_url"], "https://github.com/o/r")
        self.assertEqual(item["path"], "src/app/retry.py")
        # 同じファイルの断片は最大 2 件まで繋ぐ
        self.assertIn("def backoff", item["snippet"])
        self.assertIn("retries = 3", item["snippet"])
        self.assertEqual(got["kind"], "code")

    def test_code_snippet_keeps_generics(self):
        """`_plain_text` を通すと `<String>` が消える。コード断片では通さない。"""
        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_new_json", return_value=(CODE_PAYLOAD, "")):
            got = S.kb_github("backoff helper", kind="code", limit=1)
        self.assertIn("List<String>", got["items"][0]["snippet"])
        self.assertIn("List<String>", got["citations"][0]["summary"])

    def test_code_citation_summary_uses_the_fragment(self):
        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_new_json", return_value=(CODE_PAYLOAD, "")):
            got = S.kb_github("backoff helper", kind="code", limit=1)
        self.assertIn("def backoff", got["citations"][0]["summary"])

    def test_code_kind_without_token_makes_no_http(self):
        with mock.patch.object(S, "GITHUB_TOKEN", ""), \
             mock.patch.object(S, "_kb_new_json") as http:
            got = S.kb_github("anything", kind="code", limit=1)
        self.assertIn("GITHUB_TOKEN", got["error"])
        self.assertEqual(got["items"], [])
        http.assert_not_called()

    def test_repo_kind_keeps_plain_accept_and_description_summary(self):
        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_new_json", return_value=(REPO_PAYLOAD, "")) as http:
            got = S.kb_github("cite-summary", kind="repo", limit=1)
        self.assertEqual(http.call_args.kwargs["extra_headers"]["Accept"],
                         "application/vnd.github+json")
        self.assertEqual(http.call_args.kwargs["budget_key"], "api.github.com/search/repositories")
        self.assertEqual(got["citations"][0]["summary"], "Repo description")
        self.assertNotIn("snippet", got["items"][0])

    def test_budget_keys_and_intervals_are_split_per_endpoint(self):
        """code の 10/分に repo / issue の 30/分を巻き込まない（実測: 枠が別）。"""
        seen = {}

        def capture(url, interval, **kwargs):
            seen[kwargs["budget_key"]] = interval
            return ({"items": []}, "")

        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_new_json", side_effect=capture):
            S.kb_github("q1", kind="repo", limit=1)
            S.kb_github("q2", kind="issue", limit=1)
            S.kb_github("q3", kind="code", limit=1)
        self.assertEqual(seen["api.github.com/search/repositories"], S._GH_SEARCH_INTERVAL)
        self.assertEqual(seen["api.github.com/search/issues"], S._GH_SEARCH_INTERVAL)
        self.assertEqual(seen["api.github.com/search/code"], S._GH_CODE_INTERVAL)
        self.assertGreater(S._GH_CODE_INTERVAL, S._GH_SEARCH_INTERVAL)

    def test_unauthenticated_search_uses_the_slower_interval(self):
        """未認証は 10/分なので、認証ありの 30/分より長い間隔にする。"""
        seen = {}

        def capture(url, interval, **kwargs):
            seen["interval"] = interval
            return ({"items": []}, "")

        with mock.patch.object(S, "GITHUB_TOKEN", ""), \
             mock.patch.object(S, "_kb_new_json", side_effect=capture):
            S.kb_github("q", kind="repo", limit=1)
        self.assertEqual(seen["interval"], S._GH_SEARCH_INTERVAL_UNAUTH)
        self.assertGreater(S._GH_SEARCH_INTERVAL_UNAUTH, S._GH_SEARCH_INTERVAL)

    def test_local_budget_exhaustion_is_reported_as_an_error(self):
        """GitHub の実枠（code 10/分）に合わせたローカル予算で弾かれたら、待たずに理由を返す。"""
        with mock.patch.object(S, "GITHUB_TOKEN", "tok"), \
             mock.patch.object(S, "_kb_rate_acquire", return_value=3.5), \
             mock.patch.object(S, "kb_json") as http:
            got = S.kb_github("q", kind="repo", limit=1)
        self.assertIn("アクセス間隔制御", got["error"])
        self.assertNotIn("items", got)
        http.assert_not_called()

    def test_kb_new_json_passes_extra_headers_through(self):
        """code 検索は Authorization が要るので、予算機構から extra_headers を渡せること。"""
        with mock.patch.object(S, "_kb_rate_acquire", return_value=0.0), \
             mock.patch.object(S, "kb_json", return_value=({}, "")) as kj:
            S._kb_new_json("https://api.github.com/search/code?q=x", 6.0,
                           budget_key="api.github.com/search/code",
                           extra_headers={"Authorization": "Bearer t"})
        self.assertEqual(kj.call_args.kwargs["extra_headers"], {"Authorization": "Bearer t"})

    def test_kb_new_json_keeps_the_positional_only_call_without_headers(self):
        """後方互換: extra_headers が無ければ従来どおり位置引数 1 つで呼ぶ。"""
        with mock.patch.object(S, "_kb_rate_acquire", return_value=0.0), \
             mock.patch.object(S, "kb_json", return_value=({}, "")) as kj:
            S._kb_new_json("https://example.test/x", 1.0)
        self.assertEqual(kj.call_args.args, ("https://example.test/x",))


if __name__ == "__main__":
    unittest.main()
