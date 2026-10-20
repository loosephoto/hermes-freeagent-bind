"""probe の「上流のレート制限で gate を落とさない」契約の回帰テスト。

実測（2026-10-20, v0.8.4 の CI）: GitHub の未認証 search 枠は **IP 単位の共有**なので、
CI の共有 IP では 6 ジョブが枠を奪い合い、**1 ジョブだけ 403 で落ちた**
（`× github: HTTP 403: API rate limit exceeded for 20.119.102.65`）。

→ レート制限は**実装の不具合ではない**ので gate を落とさない。ただし
**全ソースが枠切れなら失敗**（= 何も検証できていない状態を許さない）。
"""
import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "scripts", "probe_knowledge_stdio.py")


def _load():
    spec = importlib.util.spec_from_file_location("probe_rate", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ProbeRateLimitTest(unittest.TestCase):
    def setUp(self):
        self.mod = _load()

    def _data(self, ok_src="wikipedia", extra=None):
        cite = {"source": ok_src, "title": "T", "url": "https://example.org/x"}
        results = {ok_src: {"source": ok_src, "items": [cite], "error": ""}}
        if extra:
            results.update(extra)
        return {"sources": list(results), "results": results,
                "citations": [cite], "citation_count": 1}

    def test_github_rate_limit_does_not_fail_the_gate(self):
        """github が 403 でも、他のソースが通っていれば gate は落とさない。"""
        data = self._data(extra={
            "github": {"source": "github", "items": [],
                       "error": 'HTTP 403: {"message":"API rate limit exceeded for 20.119.102.65."}'}})
        cites = self.mod.validate_lookup(data, ["wikipedia", "github"], False)
        self.assertEqual(len(cites), 1)

    def test_all_sources_rate_limited_is_a_failure(self):
        """全部が枠切れなら失敗する（= 何も検証できていない）。"""
        data = self._data(ok_src="github", extra={
            "github": {"source": "github", "items": [],
                       "error": 'HTTP 403: {"message":"API rate limit exceeded for 1.2.3.4."}'}})
        with self.assertRaises(ValueError):
            self.mod.validate_lookup(data, ["github"], False)

    def test_openalex_daily_budget_is_treated_as_rate_limit(self):
        """openalex の日次予算切れもレート制限扱い。"""
        data = self._data(extra={
            "openalex": {"source": "openalex", "items": [],
                         "error": "HTTP 429: Insufficient budget for free daily budget $0 remaining"}})
        cites = self.mod.validate_lookup(data, ["wikipedia", "openalex"], False)
        self.assertEqual(len(cites), 1)

    def test_real_failure_still_fails(self):
        """**本物の失敗は落とさない**（rate limit 以外のエラーは失敗）。"""
        data = self._data(extra={
            "github": {"source": "github", "items": [], "error": "HTTP 500: internal error"}})
        with self.assertRaises(ValueError):
            self.mod.validate_lookup(data, ["wikipedia", "github"], False)

    def test_non_rate_limited_source_403_is_a_failure(self):
        """arxiv などの 403 は正常な失敗として扱う（arxiv は枠を共有しない）。"""
        data = self._data(extra={
            "arxiv": {"source": "arxiv", "items": [], "error": "HTTP 403: forbidden"}})
        with self.assertRaises(ValueError):
            self.mod.validate_lookup(data, ["wikipedia", "arxiv"], False)

    def test_is_rate_limit_error_predicate(self):
        self.assertTrue(self.mod.is_rate_limit_error(
            "github", {"error": "HTTP 403: API rate limit exceeded"}))
        self.assertFalse(self.mod.is_rate_limit_error(
            "github", {"error": "HTTP 500: internal"}))
        self.assertFalse(self.mod.is_rate_limit_error(
            "arxiv", {"error": "HTTP 403: forbidden"}))
        self.assertFalse(self.mod.is_rate_limit_error("github", {"error": ""}))

    def test_timeout_does_not_fail_the_gate(self):
        """締め切りに間に合わなかっただけ（= 取得は継続している）は失敗にしない。

        実測: arxiv は 3 秒間隔で直列化するので、1 ソースだけ KB_DEADLINE を超えることがある。
        """
        data = self._data(extra={
            "arxiv": {"source": "arxiv", "items": [], "timed_out": True,
                      "error": "締め切り 8 秒に間に合いませんでした（取得は続けており、"
                               "終われば同じ問いの次回はキャッシュから返ります）"}})
        cites = self.mod.validate_lookup(data, ["wikipedia", "arxiv"], False)
        self.assertEqual(len(cites), 1)

    def test_timeout_predicate(self):
        self.assertTrue(self.mod.is_timeout_error({"timed_out": True}))
        self.assertTrue(self.mod.is_timeout_error({"error": "締め切り 8 秒に間に合いませんでした"}))
        self.assertFalse(self.mod.is_timeout_error({"error": "HTTP 500: internal"}))
        self.assertFalse(self.mod.is_timeout_error({}))


if __name__ == "__main__":
    unittest.main()
