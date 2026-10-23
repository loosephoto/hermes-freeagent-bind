"""v0.11.0 のオーケストレーション改善（選抜・クォータ・クールダウンのはしご）の回帰テスト。

ネットワークを使わない（`_call_once` をモックする）。実測で見つけた歪みだけを固定する:

  * 選抜: 観測 1 件の成功が「50 件の実績」を上回らない（旧実装は上回っていた）
  * 選抜: 種別（kind）を n で重み付けする（旧実装は種別の単純平均だった）
  * クォータ: **宣言した上限だけ**を守る。宣言が無ければ状態を書かない
  * クォータ: 日は UTC 境界で戻る
  * 429: 2 回目以降はクールダウンの段を下回らない（1 回目は Retry-After を尊重）
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="fa-orch-test-")
os.environ["FREEAGENT_STATE_DIR"] = _TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


def _kind(n=0.0, ok=0.0, **kw) -> dict:
    row = {"n": float(n), "ok": float(ok), "leak": 0.0, "trunc": 0.0, "empty": 0.0,
           "err": {}, "lat_ms": 0.0}
    for key, value in kw.items():
        row[key] = value
    return row


class _StateIsolated(unittest.TestCase):
    """状態ファイル（統計・クールダウン・クォータ）をテストごとに隔離する。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-orch-case-", dir=_TMP)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patcher = mock.patch.dict(os.environ, {
            "FREEAGENT_STATS_PATH": os.path.join(self.tmp, "model_stats.json"),
            "FREEAGENT_COOLDOWN_PATH": os.path.join(self.tmp, "cooldowns.json"),
            "FREEAGENT_QUOTA_PATH": os.path.join(self.tmp, "quota.json"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        # メモリ上のキャッシュを空にして、このテストの書き込みだけを見る
        with S._STATS_LOCK:
            S._STATS["models"].clear()
        S._STATS_LOADED = True
        with S._COOLDOWN_LOCK:
            S._COOLDOWN.clear()
        S._COOLDOWN_LOADED = True
        S.quota_reset()
        S._QUOTA_LOADED = True

    def tearDown(self):
        with S._STATS_LOCK:
            S._STATS["models"].clear()
        with S._COOLDOWN_LOCK:
            S._COOLDOWN.clear()
        S.quota_reset()


class TestRanking(_StateIsolated):
    def _seed(self, ref: str, kinds: dict) -> None:
        S._STATS["models"][ref] = {"kinds": kinds, "updated_at": time.time()}

    def test_lucky_single_call_does_not_outrank_evidence(self):
        # 旧実装: q=1.0 (n=1) に探索項が乗って 1.12、q=0.9 (n=50) は 0.905 → 1 件の幸運が勝った。
        self._seed("p/one-lucky", {"ask": _kind(n=1, ok=1)})
        self._seed("p/fifty", {"ask": _kind(n=50, ok=45)})
        self.assertEqual(S.rank_models(["p/one-lucky", "p/fifty"]), ["p/fifty", "p/one-lucky"])

    def test_kinds_are_weighted_by_observations(self):
        # 1 件の成功（ask）＋ 50 件の失敗（panel）は「品質 0.5」ではなく実質ほぼ全滅として扱う。
        self._seed("p/mixed", {"ask": _kind(n=1, ok=1),
                               "panel": _kind(n=50, ok=0, err={"rate_limited": 50.0})})
        self.assertLess(S._rank_score("p/mixed"), 0.25)

    def test_unobserved_model_is_neither_zero_nor_top(self):
        self._seed("p/none", {"ask": _kind(n=0)})
        score = S._rank_score("p/none")
        # a=b=1 の Beta 事後: 平均 0.5・分散 1/12 → 0.5 + 0.25 * sqrt(1/12)
        self.assertAlmostEqual(score, 0.5 + 0.25 * (1.0 / 12.0) ** 0.5, places=6)

    def test_ranking_is_deterministic(self):
        for i in range(5):
            self._seed(f"p/m{i}", {"ask": _kind(n=i, ok=i)})
        refs = [f"p/m{i}" for i in range(5)]
        self.assertEqual(S.rank_models(refs), S.rank_models(refs))

    def test_rank_disabled_keeps_input_order(self):
        refs = ["z/z", "a/a", "m/m"]
        with mock.patch.object(S, "RANK_ENABLED", False):
            self.assertEqual(S.rank_models(refs), refs)


class TestQuota(_StateIsolated):
    def test_parse(self):
        parsed = S._parse_quota_limits(
            "nous:rpm=60+rpd=1000, openrouter:rpd=1000, bogus:rpd=5, nous:tpd=x",
            set(S.PROVIDER_SPECS))
        self.assertEqual(parsed["nous"], {"rpm": 60, "rpd": 1000})
        self.assertEqual(parsed["openrouter"], {"rpd": 1000})
        self.assertNotIn("bogus", parsed)

    def test_no_declaration_writes_nothing(self):
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {}):
            self.assertIsNone(S.quota_block("nous"))
            S.quota_record("nous", 100)
        self.assertFalse(os.path.exists(os.environ["FREEAGENT_QUOTA_PATH"]),
                         "宣言が無いのに状態ファイルを作った")

    def test_blocks_when_daily_limit_reached(self):
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 2}}):
            self.assertIsNone(S.quota_block("nous"))
            S.quota_record("nous", 10)
            self.assertIsNone(S.quota_block("nous"))
            S.quota_record("nous", 10)
            reason = S.quota_block("nous") or ""
        self.assertTrue(reason)
        self.assertIn("RPD 上限 2", reason)
        self.assertIn("UTC", reason)

    def test_reserve_closes_check_then_act(self):
        """並列 N 本が同じ残量を読んで全員通る穴を塞ぐ（実測: 枠 4 本・上限 3 で 4 本通った）。"""
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 3}}):
            results = [S.quota_reserve("nous") for _ in range(4)]
        self.assertEqual(results[:3], [None, None, None], "上限までは確保できる")
        self.assertIn("RPD 上限 3", results[3] or "", "上限を超える確保は拒否される")

    def test_day_boundary_resets(self):
        now = time.time()
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 1}}), \
                mock.patch.object(S, "now_ts", lambda: now):
            S.quota_record("nous", 5)
            self.assertIsNotNone(S.quota_block("nous"))
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 1}}), \
                mock.patch.object(S, "now_ts", lambda: now + 86400):
            self.assertIsNone(S.quota_block("nous"), "UTC の日付が変わったのに戻っていない")

    def test_call_model_reports_quota_dropout(self):
        with mock.patch.object(S, "QUOTA_ENABLED", True), \
                mock.patch.object(S, "_QUOTA_LIMITS", {"nous": {"rpd": 1, "rpm": 99}}), \
                mock.patch.object(S, "provider_slot_cap", lambda prov: 8), \
                mock.patch.object(S, "free_model_refs", lambda **kw: ["nous/a:free"]), \
                mock.patch.object(S, "cooling_refs", lambda: {}), \
                mock.patch.object(S, "is_cooling", lambda ref: False), \
                mock.patch.object(S, "_call_once", lambda *a, **k: {
                    "text": "ok", "latency_s": 0.01, "finish_reason": "stop",
                    "truncated": False, "tokens": {"prompt": 1, "completion": 1}}):
            first = S.call_model("nous/a:free", "q", max_tokens=32, allow_fallback=False)
            second = S.call_model("nous/a:free", "q", max_tokens=32, allow_fallback=False)
        self.assertNotIn("error", first)
        self.assertIn("上限", second.get("error") or "")
        self.assertTrue(second.get("skipped_cooling"))


class TestCooldownLadder(_StateIsolated):
    def test_ladder_escalates_and_caps(self):
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["strikes"], 1)
        self.assertEqual(S._COOLDOWN["p/m"]["retry_after_s"], 60.0)
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["strikes"], 2)
        self.assertEqual(S._COOLDOWN["p/m"]["retry_after_s"], 180.0)
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["retry_after_s"], 600.0)
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["retry_after_s"], 600.0, "MAX を超えてはしごを上った")
        self.assertIn("4 回目", S._COOLDOWN["p/m"]["reason"])

    def test_retry_after_respected_on_first_strike(self):
        S.note_rate_limited("p/short", "5")
        self.assertEqual(S._COOLDOWN["p/short"]["retry_after_s"], 5.0)

    def test_ladder_restarts_after_expiry(self):
        S.note_rate_limited("p/m")
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["strikes"], 2)
        with S._COOLDOWN_LOCK:                       # 冷えきった状態を作る
            S._COOLDOWN["p/m"]["until"] = time.time() - 1
        S.note_rate_limited("p/m")
        self.assertEqual(S._COOLDOWN["p/m"]["strikes"], 1)


if __name__ == "__main__":
    unittest.main()
