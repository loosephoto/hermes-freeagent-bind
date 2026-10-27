"""v0.24.0 の高速化・改良の契約（接続の再利用・ラベルキャッシュ・同点のタイブレーク・
キャンセル・arXiv の CJK 早期拒否・一時ファイル掃除の間引き）.

何を固定するか:
  * `kb_json` は**許可ホストだけ**持続接続経路へ委譲する（リダイレクトを追わない経路なので
    ホストを絞る）。POST と `extra_headers` 付きは共用経路のまま。
  * `kb_wikidata` はラベル解決をキャッシュし、**2 回目は labels の往復を投げない**。
  * `rank_models` は品質スコアが同点のとき**平均所要の速い順**（品質の順位は変えない）。
  * `notifications/cancelled` で要求の予算が打ち切られ、未開始のジョブが投入されない。
  * arXiv は CJK だけの問いで HTTP を出さずに理由を返す（3 秒直列の枠を無駄にしない）。
  * `_sweep_stale_tmp` は同じディレクトリを 60 秒に 1 回しか走査しない。

ネットワークは一切使わない（偽の経路を差し込む）。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))


def _isolated():
    tmp = tempfile.mkdtemp(prefix="fa-v0240-")
    os.environ["FREEAGENT_STATE_DIR"] = tmp
    return tmp


import freeagent_bind.server as S  # noqa: E402
from freeagent_bind.sources import wikidata as W  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _isolated()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(self._reset)
        self._reset()

    def _reset(self):
        S._KB_CACHE.clear()
        S._STATS["models"].clear()
        S._STATS_LOADED = False
        S._SWEEP_LAST.clear()
        S._ka_reset()
        W._label_cache_reset()


class TestKeepAliveDelegation(_Base):
    def test_trusted_host_uses_the_persistent_path(self):
        with mock.patch.object(S, "kb_json_keepalive", return_value=({"ok": 1}, "")) as ka:
            got = S.kb_json("https://api.crossref.org/works?query=x")
        self.assertEqual(got, ({"ok": 1}, ""))
        self.assertTrue(ka.called, "許可ホストが持続接続経路に委譲されていない")

    def test_untrusted_host_stays_on_the_shared_path(self):
        # arXiv は 3 秒直列・リダイレクト（http→https）を持つので委譲しない。
        with mock.patch.object(S, "kb_json_keepalive", side_effect=AssertionError("委譲した")), \
                mock.patch.object(S, "kb_http", return_value=(200, '{"ok": 1}')):
            data, err = S.kb_json("https://export.arxiv.org/api/query?search_query=all:x")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(err, "")

    def test_post_payload_is_never_delegated(self):
        with mock.patch.object(S, "kb_json_keepalive", side_effect=AssertionError("委譲した")), \
                mock.patch.object(S, "kb_http", return_value=(200, '{"ok": 1}')):
            data, _err = S.kb_json("https://api.openalex.org/works", payload={"q": "x"})
        self.assertEqual(data, {"ok": 1})

    def test_extra_headers_are_not_delegated(self):
        with mock.patch.object(S, "kb_json_keepalive", side_effect=AssertionError("委譲した")), \
                mock.patch.object(S, "kb_http", return_value=(200, '{"ok": 1}')):
            S.kb_json("https://api.crossref.org/works?query=x", extra_headers={"X": "1"})

    def test_keepalive_disabled_falls_back(self):
        with mock.patch.dict(os.environ, {"FREEAGENT_KB_KEEPALIVE": "0"}), \
                mock.patch.object(S, "kb_json_keepalive", side_effect=AssertionError("委譲した")), \
                mock.patch.object(S, "kb_http", return_value=(200, '{"ok": 1}')):
            S.kb_json("https://api.crossref.org/works?query=x")


class TestWikidataLabelCache(_Base):
    def _fake(self, calls):
        def fake(url, **kwargs):
            calls.append(url)
            if "wbsearchentities" in url:
                return {"search": [{"id": "Q1", "label": "A", "description": "d"}]}, ""
            if "props=claims" in url:
                return {"entities": {"Q1": {"claims": {
                    "P31": [{"mainsnak": {"datavalue": {"value": {"id": "Q7397"}}}}]}}}}, ""
            if "props=labels" in url:
                return {"entities": {"Q7397": {"labels": {"ja": {"value": "ソフトウェア"}}}}}, ""
            return {}, ""

        return fake

    def test_labels_are_requested_once_then_cached(self):
        calls = []
        with mock.patch.object(S, "kb_json_keepalive", side_effect=self._fake(calls)):
            first = S.kb_wikidata("cache-a", lang="ja", limit=1)
            S._KB_CACHE.clear()          # クエリ結果のキャッシュは外す（ラベルのキャッシュだけを見る）
            second = S.kb_wikidata("cache-a", lang="ja", limit=1)
        labels_calls = [u for u in calls if "props=labels" in u]
        self.assertEqual(len(labels_calls), 1, f"labels を毎回投げている: {labels_calls}")
        self.assertEqual(first["items"][0]["claims"][0]["value_text"], "ソフトウェア")
        self.assertEqual(second["items"][0]["claims"][0]["value_text"], "ソフトウェア")

    def test_cache_is_bounded(self):
        with mock.patch.object(W, "_LABEL_CACHE_MAX", 3):
            W._label_cache_reset()
            W._store_labels("ja", {f"Q{i}": f"L{i}" for i in range(10)})
            self.assertLessEqual(len(W._LABEL_CACHE), 6,
                                 "上限を超えて増え続けている（追い出しが効いていない）")


class TestLatencyTieBreak(_Base):
    def _observe(self, ref, lat_ms):
        S._STATS["models"][ref] = {"updated_at": S.now_ts(),
                                   "kinds": {"ask": {"n": 2.0, "ok": 1.0, "leak": 0.0, "trunc": 0.0,
                                                     "empty": 0.0, "lat_ms": lat_ms * 2,
                                                     "err": {"server": 1.0}}}}

    def test_equal_quality_prefers_the_faster_model(self):
        S._STATS_LOADED = True
        self._observe("nous/slow", 25000.0)
        self._observe("nous/fast", 1800.0)
        self.assertEqual(S._rank_score("nous/slow"), S._rank_score("nous/fast"),
                         "このテストは同点を前提にしている")
        self.assertEqual(S.rank_models(["nous/slow", "nous/fast"]),
                         ["nous/fast", "nous/slow"])

    def test_unobserved_models_stay_after_observed_ones_on_ties(self):
        S._STATS_LOADED = True
        self._observe("nous/observed", 9000.0)
        self.assertEqual(S._rank_latency_key("nous/never-seen"), float("inf"))
        self.assertEqual(S._rank_latency_key("nous/observed"), 9000.0)

    def test_quality_difference_is_not_overridden_by_latency(self):
        S._STATS_LOADED = True
        self._observe("nous/bad-but-fast", 100.0)     # 1 勝 1 敗
        S._STATS["models"]["nous/good-but-slow"] = {
            "updated_at": S.now_ts(),
            "kinds": {"ask": {"n": 40.0, "ok": 40.0, "leak": 0.0, "trunc": 0.0, "empty": 0.0,
                              "lat_ms": 25000.0 * 40, "err": {}}}}
        self.assertEqual(S.rank_models(["nous/bad-but-fast", "nous/good-but-slow"])[0],
                         "nous/good-but-slow")


class TestCancelRequest(_Base):
    def test_cancelled_notification_expires_the_running_deadline(self):
        deadline = S.Deadline(60)
        S._register_active(7, deadline)
        replies = S._replies_for({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                  "params": {"requestId": 7}})
        self.assertEqual(replies, [], "キャンセル（通知）に応答してはいけない")
        self.assertTrue(deadline.expired, "キャンセルで予算が打ち切られていない")
        self.assertFalse(S.cancel_request(7), "2 回目のキャンセルは対象が無い")

    def test_cancelled_request_does_not_start_queued_jobs(self):
        """予算が打ち切られたら、`run_parallel` は**投入せずに**脱落で埋める。"""
        ran = []
        deadline = S.Deadline(60)
        S._register_active("req-9", deadline)
        S._replies_for({"jsonrpc": "2.0", "method": "notifications/cancelled",
                        "params": {"requestId": "req-9"}})
        got = S.run_parallel([1, 2, 3], lambda i: ran.append(i) or {"i": i},
                             max_workers=1, deadline=deadline)
        self.assertEqual(ran, [], f"キャンセル後にも関わらず実行された: {ran}")
        self.assertTrue(all(g.get("dropped") for g in got))

    def test_unknown_request_id_is_ignored(self):
        self.assertFalse(S.cancel_request("never-seen"))


class TestArxivCjk(_Base):
    def test_japanese_only_query_does_not_hit_the_network(self):
        with mock.patch.object(S, "kb_http", side_effect=AssertionError("HTTP を出した")), \
                mock.patch.object(S, "_arxiv_throttle", side_effect=AssertionError("直列枠を取った")):
            got = S.kb_arxiv("大規模言語モデル")
        self.assertEqual(got["items"], [])
        self.assertIn("英語", got["error"])

    def test_mixed_query_still_goes_to_arxiv(self):
        atom = ("<feed xmlns='http://www.w3.org/2005/Atom'><entry><id>x</id><title>t</title>"
                "<summary>s</summary><published>2024-01-01T00:00:00Z</published>"
                "<link rel='alternate' href='https://arxiv.org/abs/1'/></entry></feed>")
        with mock.patch.object(S, "kb_http", return_value=(200, atom)), \
                mock.patch.object(S, "_arxiv_throttle", lambda: None):
            got = S.kb_arxiv("Transformer 大規模言語モデル")
        self.assertEqual(got["error"], "")


class TestSweepThrottle(_Base):
    def test_second_sweep_in_the_same_minute_is_skipped(self):
        target = os.path.join(self.tmp, "stats.json")
        # 古い残骸を 1 つ置く（1 回目の掃除で消える）
        stale = f"{target}.{os.getpid()}.1.tmp"
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("x")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        self.assertEqual(S._sweep_stale_tmp(target), 1)
        # もう 1 つ置いても、間引きの間は走査しない（0 が返り、ファイルは残る）
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(stale, (old, old))
        self.assertEqual(S._sweep_stale_tmp(target), 0)
        self.assertTrue(os.path.exists(stale))


if __name__ == "__main__":
    unittest.main()
