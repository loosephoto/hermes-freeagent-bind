"""提供元別の同時実行ゲート（§3.4）とモデル数の上限まわりの回帰テスト。

ネットワークを使わない（`_call_once` / `free_model_refs` / `ask_map` / `ask_many` をモックする）。
実測にもとづく不変条件だけを鳴らす:

  * 同時実行数が提供元の枠を超えない（＝429 の一次防護。規約 55）
  * 提供元が違えば枠は独立（別ホストへは同時に投げてよい）
  * 429 で枠が 1 本縮み、連続成功で戻る（縮みっぱなしにしない）
  * 枠が取れないときは**無限待ちせず**、脱落として見える（規約 8 / 規約 27）
  * 明示 `models` は `size` で切り落とさない（実測: 8 件渡しても 3 件に切られていた）
  * `models: ["all"]` は在庫の全 Free へ展開し、上限で切る
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-gate-test"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402

REFS8 = [f"nous/model-{i}:free" for i in range(8)]


def _canned(count: int) -> list[dict]:
    return [{"text": f"結論: ok-{i}\n確信度: 70", "latency_s": 0.01, "finish_reason": "stop",
             "truncated": False, "tokens": {}} for i in range(count)]


class TestProviderGate(unittest.TestCase):
    def setUp(self):
        S.PROVIDER_GATE.reset()
        self.addCleanup(S.PROVIDER_GATE.reset)

    def _cap(self, mapping, default=1):
        patcher = mock.patch.object(S, "provider_slot_cap", lambda p: mapping.get(p, default))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_limits_and_env_parse(self):
        # nous は実測（枠 8 で 429 が 1〜2 件 / 6 で 0〜1 件・壁 6 秒台）で 6。未実測は保守的な既定。
        self.assertEqual(S.provider_slot_cap("nous"), 6)
        self.assertEqual(S.provider_slot_cap("openrouter"), S._PROVIDER_LIMIT_DEFAULT)
        self.assertGreaterEqual(S.MAX_SUB_MODELS, 8)     # 既定 4 に戻ったら気づく
        self.assertGreaterEqual(S.MAX_WORKERS, 8)
        parsed = S._parse_provider_limits("openrouter=2, bogus=9, nous=99, broken=x, =3",
                                          set(S.PROVIDER_SPECS))
        # 未知の提供元・壊れた値・空の名前は無視し、値は 1..16 に丸める
        self.assertEqual(parsed, {"openrouter": 2, "nous": 16})

    def test_concurrency_never_exceeds_limit(self):
        self._cap({"nous": 2})
        gate = S.PROVIDER_GATE
        peak, cur = [0], [0]
        lock = threading.Lock()

        def worker():
            self.assertIsNotNone(gate.acquire("nous"))
            with lock:
                cur[0] += 1
                peak[0] = max(peak[0], cur[0])
            time.sleep(0.05)
            with lock:
                cur[0] -= 1
            gate.release("nous")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertLessEqual(peak[0], 2, "同時実行が提供元の枠を超えた")
        self.assertEqual(gate.report(["nous"])["per_provider"]["nous"]["in_flight"], 0)

    def test_providers_are_independent(self):
        # 別の提供元（別ホスト）は同時に投げてよい。枠が 1 本でも互いに待たない。
        self._cap({"a": 1, "b": 4})
        gate = S.PROVIDER_GATE
        self.assertIsNotNone(gate.acquire("a"))
        self.addCleanup(gate.release, "a")
        self.assertIsNotNone(gate.acquire("b", timeout=0.05), "b の枠が a に巻き込まれた")
        gate.release("b")
        self.assertIsNone(gate.acquire("a", timeout=0.05), "満杯の a の枠が取れてしまった")
        self.assertGreaterEqual(gate.report(["a"])["timeouts_total"], 1)

    def test_rate_limit_reduces_and_success_restores(self):
        self._cap({"nous": 4})
        gate = S.PROVIDER_GATE
        self.assertEqual(gate.limit("nous"), 4)
        self.assertEqual(gate.note_rate_limited("nous"), (4, 3))
        for _ in range(S._GATE_RESTORE_AFTER):
            gate.note_success("nous")
        self.assertEqual(gate.limit("nous"), 4, "連続成功で枠が戻っていない")
        for _ in range(10):
            gate.note_rate_limited("nous")
        self.assertEqual(gate.limit("nous"), 1, "枠が 1 本を下回った（0 は無限待ちの元）")
        self.assertGreaterEqual(gate.report(["nous"])["reduced_total"], 1)

    def test_report_scope_is_explicit(self):
        report = S.PROVIDER_GATE.report(["nous"])
        self.assertEqual(report["scope"], "このプロセスの累計")
        self.assertIn("limit", report["per_provider"]["nous"])

    def test_report_accepts_refs(self):
        # 実測: 参加モデルの ref をそのまま渡すと `nous/…` という架空の提供元が並んだ（修正済み）。
        self._cap({"nous": 3})
        report = S.PROVIDER_GATE.report(["nous/a:free", "nous/b:free", "openrouter/c:free"])
        self.assertEqual(sorted(report["per_provider"]), ["nous", "openrouter"])
        self.assertEqual(report["per_provider"]["nous"]["cap"], 3)


class TestGateInCallPath(unittest.TestCase):
    def setUp(self):
        S.PROVIDER_GATE.reset()
        self.addCleanup(S.PROVIDER_GATE.reset)
        p = mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS8))
        p.start()
        self.addCleanup(p.stop)
        # **クールダウンから切り離す**（このファイルはゲートの不変条件を見る。429 のクールダウンは
        # 既存テストの担当で、ここで書くと次のテストが「冷却中なので候補から外す」で別の結論になる）。
        q = mock.patch.object(S, "cooling_refs", lambda: {})
        q.start()
        self.addCleanup(q.stop)
        q = mock.patch.object(S, "is_cooling", lambda ref: False)
        q.start()
        self.addCleanup(q.stop)
        q = mock.patch.object(S, "note_rate_limited", lambda *a, **k: 0.0)
        q.start()
        self.addCleanup(q.stop)

    def _fake_call_once(self, delay=0.05, text="ok"):
        state = {"cur": 0, "peak": 0}
        lock = threading.Lock()

        def fake(provider, model, prompt, system, max_tokens, temperature, timeout=None):
            with lock:
                state["cur"] += 1
                state["peak"] = max(state["peak"], state["cur"])
            time.sleep(delay)
            with lock:
                state["cur"] -= 1
            return {"text": text, "latency_s": delay, "finish_reason": "stop",
                    "truncated": False, "tokens": {}}

        return fake, state

    def test_ask_many_respects_provider_slots(self):
        fake, state = self._fake_call_once()
        p = mock.patch.object(S, "provider_slot_cap", lambda prov: 2)
        p.start()
        self.addCleanup(p.stop)
        with mock.patch.object(S, "_call_once", fake):
            rows = S.ask_many(list(REFS8), "問い", max_tokens=32)
        self.assertEqual(len(rows), 8)
        self.assertEqual([r for r in rows if r.get("error")], [])
        self.assertLessEqual(state["peak"], 2, "8 体を枠 2 本で走らせたのに同時数が超えた")
        # **待ちで吸収したことを計測・報告する**（429 を踏んだのと区別できる）
        self.assertGreaterEqual(S._rate_limit_report(rows)["slot_waited"], 1)
        self.assertTrue(any((r.get("gate_wait_s") or 0) > 0 for r in rows))

    def test_429_reduces_provider_slot(self):
        p = mock.patch.object(S, "provider_slot_cap", lambda prov: 3)
        p.start()
        self.addCleanup(p.stop)

        def boom(*a, **k):
            raise S.HttpStatusError(429, "rate limited", "1")

        with mock.patch.object(S, "_call_once", boom):
            res = S.call_model(REFS8[0], "問い", max_tokens=32, allow_fallback=False)
        self.assertIn("error", res)
        self.assertEqual(S.PROVIDER_GATE.limit("nous"), 2, "429 で枠が縮んでいない")

    def test_busy_slot_becomes_dropout_not_hang(self):
        # 枠 1 本に 8 体が並ぶ。**無限待ちせず**、待ち上限で脱落として見える。
        fake, _ = self._fake_call_once(delay=0.4)
        p = mock.patch.object(S, "provider_slot_cap", lambda prov: 1)
        p.start()
        self.addCleanup(p.stop)
        started = time.monotonic()
        with mock.patch.object(S, "GATE_WAIT_S", 0.1), mock.patch.object(S, "_call_once", fake):
            rows = S.ask_many(list(REFS8), "問い", max_tokens=32)
        wall = time.monotonic() - started
        errors = [r for r in rows if r.get("error")]
        self.assertTrue(errors, "枠が取れないのに全員成功として返った")
        self.assertTrue(all("同時実行枠" in r["error"] for r in errors), errors[:1])
        self.assertLess(wall, 5.0, f"枠待ちで固まった（{wall:.2f} 秒）")


class TestModelCountLimits(unittest.TestCase):
    def setUp(self):
        # 選抜の本数だけを見る（クールダウンの有無で結論が変わらないように切り離す）。
        q = mock.patch.object(S, "cooling_refs", lambda: {})
        q.start()
        self.addCleanup(q.stop)
        q = mock.patch.object(S, "is_cooling", lambda ref: False)
        q.start()
        self.addCleanup(q.stop)

    def test_expand_all_uses_inventory(self):
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS8)):
            refs, cut = S._expand_models(["all"])
            self.assertEqual(refs, REFS8)
            self.assertFalse(cut)
            mixed, _ = S._expand_models(["nous/named", "all"])
            self.assertEqual(mixed[0], "nous/named", "名指しした ref が先頭に残っていない")
            self.assertIn("nous/model-0:free", mixed)
        self.assertEqual(S._expand_models(["nous/x"]), (["nous/x"], False))
        self.assertEqual(S._expand_models([]), ([], False))

    def test_expand_all_is_capped(self):
        pool = [f"nous/m{i}" for i in range(50)]
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(pool)), \
                mock.patch.object(S, "MAX_MODELS_PER_CALL", 4):
            refs, cut = S._expand_models(["all"])
        self.assertEqual(len(refs), 4)
        self.assertTrue(cut)

    def test_explicit_models_not_truncated_by_size(self):
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS8)):
            refs, info = S.select_models(3, list(REFS8))
        self.assertEqual(len(refs), 8, "明示 8 件が size=3 で切り落とされた")
        self.assertTrue(info["requested"])

    def test_size_clamps_to_max_sub_models(self):
        pool = [f"nous/m{i}" for i in range(20)]
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(pool)):
            refs, _ = S._select_or_error({"size": 99})
        self.assertEqual(len(refs), S.MAX_SUB_MODELS)

    def test_select_or_error_expands_all(self):
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS8)):
            refs, info = S._select_or_error({"models": ["all"], "size": 3})
        self.assertEqual(len(refs), 8)
        self.assertEqual(info.get("notes") or [], [])

    def test_panel_runs_all_explicit_models(self):
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(REFS8)), \
                mock.patch.object(S, "ask_many", lambda refs, prompt, **kw: _canned(len(refs))):
            data = S.tool_panel({"question": "問い", "models": list(REFS8)})
        self.assertEqual(len(data["models"]), 8)
        self.assertEqual(data["answered"], 8)
        self.assertIn("concurrency", data)

    def test_fanout_runs_all_explicit_models(self):
        with mock.patch.object(S, "ask_map", lambda pairs, **kw: _canned(len(pairs))):
            data = S.tool_fanout({"prompt": "p", "models": list(REFS8)})
        self.assertEqual(data["calls"], 8)
        self.assertEqual(data["ok"], 8)
        self.assertIn("concurrency", data)
        self.assertEqual(data["selection"]["models"], REFS8)

    def test_fanout_still_caps_total_calls(self):
        # `all` でも**合計呼び出しの上限**は効く（2 プロンプト × 40 モデル = 80 > 40）。
        pool = [f"nous/m{i}" for i in range(40)]
        with mock.patch.object(S, "free_model_refs", lambda **kw: list(pool)), \
                mock.patch.object(S, "MAX_MODELS_PER_CALL", 40):
            data = S.tool_fanout({"prompts": ["a", "b"], "models": ["all"]})
        self.assertIn("error", data)
        self.assertIn("上限", data["error"])


if __name__ == "__main__":
    unittest.main()
