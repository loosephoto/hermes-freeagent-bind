"""同一実行内の重複合流（`_CallCoalescer`・SPEC §4.7）の回帰テスト.

固定する契約:

* 同じ `(kind, ref, prompt, system, max_tokens, temperature, …)` は **上流 1 回**に畳む
* **モデルが違えば畳まない**（`ask_many` の「同じ問いを N 体へ」の多様性を壊さない）
* **ツール呼び出しの外では畳まない**（`scripts/measure_quality.py --repeat` の測定を壊さない）
* 畳んだ行は `coalesced: true`、ツールの要約は `coalesced_calls: N` で見える（規約 8: 隠さない）
* 先頭が失敗したら、待っている側にも同じ失敗を配る（上流は 1 回のまま）
* 待ちが上限を超えたら**自分で取りに行く**（固まらない・規約 53 と同じ型）
* 配る結果は**深いコピー**（片方を書き換えても他方に影響しない）

ネットワークは使わない（偽プロバイダ + `provider_http` のスタブで呼び出し回数を数える）。
"""

import contextlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402

REF = "fakep/fake-1"
REF2 = "fakep/fake-2"

FAKE_SPEC = {
    "base_url": "http://127.0.0.1:1/v1",
    "key": "test-key",
    "key_env": None,
    "free_kind": "allowlist",
    "free_model_ids": ["fake-1", "fake-2"],
    "always_ready": True,
    "models_path": "/models",
    "note": "offline test provider（重複合流の回帰テスト用）",
}


class _FakeProviderCase(unittest.TestCase):
    """偽プロバイダで `call_model` を成功させ、`provider_http` の呼び出し回数を数える."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-coalesce-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        self.addCleanup(self._restore_state)
        self.addCleanup(setattr, S._COALESCE_LOCAL, "coalescer", None)
        self.calls: list = []

    def _restore_state(self):
        if self._old_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old_state

    def _http(self):
        def http(path, provider=None, payload=None, timeout=None, **kw):
            messages = (payload or {}).get("messages") or []
            prompt = messages[-1].get("content", "") if messages else ""
            self.calls.append({"path": path, "provider": provider, "prompt": prompt})
            return {"choices": [{"message": {"content": f"echo:{prompt}"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        return http

    def chat(self):
        """推論の呼び出しだけ（一覧の取得 `/models` を混ぜない）。"""
        return [c for c in self.calls if "chat" in c["path"]]

    @contextlib.contextmanager
    def fake_provider(self, http=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(S.PROVIDER_SPECS, {"fakep": FAKE_SPEC}))
            stack.enter_context(mock.patch.object(S, "PROVIDER_ORDER", ["fakep"]))
            stack.enter_context(mock.patch.object(S, "provider_http", http or self._http()))
            stack.enter_context(mock.patch.object(S, "observe_call"))
            yield


class CallModelCoalescingTest(_FakeProviderCase):
    def test_identical_calls_in_one_tool_call_cost_one_upstream_call(self):
        with self.fake_provider(), S.bound_coalescer():
            first = S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            second = S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 1, self.calls)
        self.assertEqual(first["text"], second["text"])
        self.assertTrue(second.get("coalesced"))
        self.assertIsNone(first.get("coalesced"))     # 先頭は印を付けない

    def test_outside_a_tool_call_nothing_is_coalesced(self):
        with self.fake_provider():
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 2)

    def test_env_flag_off_disables_coalescing(self):
        with self.fake_provider(), mock.patch.object(S, "COALESCE_ENABLED", False), S.bound_coalescer():
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 2)

    def test_different_models_are_never_coalesced(self):
        """多様性の防護: 同じ問いを別モデルへ投げるのは畳んではいけない."""
        with self.fake_provider(), S.bound_coalescer():
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            S.call_model(REF2, "p", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 2)

    def test_different_prompts_are_never_coalesced(self):
        with self.fake_provider(), S.bound_coalescer():
            S.call_model(REF, "p1", allow_fallback=False, max_tokens=32)
            S.call_model(REF, "p2", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 2)

    def test_different_max_tokens_are_never_coalesced(self):
        with self.fake_provider(), S.bound_coalescer():
            S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            S.call_model(REF, "p", allow_fallback=False, max_tokens=64)
        self.assertEqual(len(self.chat()), 2)

    def test_parallel_duplicates_cost_one_upstream_call(self):
        """並列経路（`run_parallel` のワーカーは別スレッド）でも畳めること."""
        with self.fake_provider(), S.bound_coalescer():
            results = S.ask_map([(REF, "same"), (REF, "same")], max_tokens=32)
        self.assertEqual(len(self.chat()), 1, self.calls)
        texts = {r.get("text") for r in results}
        self.assertEqual(len(texts), 1)
        self.assertTrue(all(r.get("text") for r in results))
        self.assertEqual(sum(1 for r in results if r.get("coalesced")), 1)

    def test_failure_is_shared_and_still_costs_one_attempt(self):
        def boom(*a, **kw):
            self.calls.append({"path": "/chat/completions", "provider": "fakep", "prompt": "boom"})
            raise RuntimeError("probe failure")

        with self.fake_provider(http=boom), S.bound_coalescer():
            first = S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
            second = S.call_model(REF, "p", allow_fallback=False, max_tokens=32)
        self.assertEqual(len(self.chat()), 1)
        self.assertIn("probe failure", first.get("error", ""))
        self.assertIn("probe failure", second.get("error", ""))
        self.assertTrue(second.get("coalesced"))


class CoalescerUnitTest(unittest.TestCase):
    """`_CallCoalescer` 単体（スレッドの待ち方と配り方）."""

    def test_leader_exception_is_handed_to_the_waiter(self):
        coalescer = S._CallCoalescer(1.0)
        started = threading.Event()
        out: dict = {}

        def leader():
            def boom():
                started.set()
                raise RuntimeError("boom")

            try:
                coalescer.run("k", boom)
            except RuntimeError:
                out["raised"] = True

        def follower():
            out["follower"] = coalescer.run("k", lambda: {"text": "should not run"})

        th_lead = threading.Thread(target=leader)
        th_lead.start()
        self.assertTrue(started.wait(2.0))       # 先頭を「走り出した」状態にしてから参加させる
        th_fol = threading.Thread(target=follower)
        th_fol.start()
        th_lead.join(3.0)
        th_fol.join(3.0)
        self.assertTrue(out.get("raised"))                       # 先頭には例外が伝わる
        self.assertIn("error", out.get("follower", {}))          # 待つ側には同じ失敗を配る
        self.assertTrue(out["follower"].get("coalesced"))
        self.assertNotIn("should not run", str(out))

    def test_wait_timeout_falls_back_to_its_own_call(self):
        coalescer = S._CallCoalescer(0.02)
        runs = []

        def slow():
            runs.append("leader")
            time.sleep(0.2)
            return {"text": "leader"}

        th = threading.Thread(target=lambda: coalescer.run("k", slow))
        th.start()
        time.sleep(0.05)                      # 先頭を走らせてから参加する
        value = coalescer.run("k", lambda: runs.append("follower") or {"text": "follower"})
        th.join(3.0)
        self.assertEqual(value["text"], "follower")
        self.assertEqual(runs, ["leader", "follower"])
        self.assertFalse(value.get("coalesced"))   # 自分で取りに行ったので合流ではない

    def test_waiter_gets_a_deep_copy(self):
        coalescer = S._CallCoalescer(1.0)
        leader_value = {"text": "x", "tokens": {"prompt": 1}}

        def leader():
            return coalescer.run("k", lambda: leader_value)

        th = threading.Thread(target=leader)
        th.start()
        time.sleep(0.05)
        follower_value = coalescer.run("k", lambda: {"text": "fallback"})
        th.join(3.0)
        self.assertTrue(follower_value.get("coalesced"))
        follower_value["tokens"]["prompt"] = 999      # 書き換えても
        follower_value["text"] = "changed"
        self.assertEqual(leader_value["tokens"]["prompt"], 1)   # 先頭は無傷
        self.assertEqual(leader_value["text"], "x")


class ToolReportingTest(_FakeProviderCase):
    """ツールの出力に件数と行の印が出ること（規約 8・63）."""

    def test_tool_map_reports_coalesced_calls_and_marks_the_row(self):
        args = {"model": REF, "instruction": "1 語で言い換えて", "items": ["同じ要素", "同じ要素"],
                "max_tokens": 32}
        with self.fake_provider():
            result = S.handle_tool_call({"name": "freeagent_map", "arguments": args,
                                         "_meta": {"progressToken": "tok"}})
        self.assertFalse(result.get("isError"), result.get("structuredContent"))
        data = result["structuredContent"]
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["ok"], 2)
        self.assertEqual(data["coalesced_calls"], 1)
        self.assertEqual(data["results"][0]["output"], data["results"][1]["output"])
        self.assertEqual(sum(1 for r in data["results"] if r.get("coalesced")), 1)
        self.assertEqual(len(self.chat()), 1)
        self.assertIn("上流へは 1 回だけ流しました", result["content"][0]["text"])

    def test_tool_fanout_reports_coalesced_calls(self):
        args = {"prompts": ["同じ問い", "同じ問い"], "models": [REF], "max_tokens": 32}
        with self.fake_provider():
            result = S.handle_tool_call({"name": "freeagent_fanout", "arguments": args})
        data = result["structuredContent"]
        self.assertEqual(data["calls"], 2)
        self.assertEqual(data["coalesced_calls"], 1)
        self.assertEqual(sum(1 for r in data["results"] if r.get("coalesced")), 1)
        self.assertEqual(len(self.chat()), 1)

    def test_map_without_duplicates_reports_zero(self):
        args = {"model": REF, "instruction": "1 語で言い換えて", "items": ["a", "b"], "max_tokens": 32}
        with self.fake_provider():
            result = S.handle_tool_call({"name": "freeagent_map", "arguments": args})
        self.assertEqual(result["structuredContent"]["coalesced_calls"], 0)
        self.assertEqual(len(self.chat()), 2)
        self.assertNotIn("上流へは 1 回だけ流しました", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
