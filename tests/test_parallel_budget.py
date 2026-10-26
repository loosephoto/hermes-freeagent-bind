"""並列実行の壁時計予算（§4 `Deadline` / `run_parallel`）の契約.

何を固定するか（実測ベース）:
  * `run_parallel` は**入力順**で返し、worker の例外を吸収する（従来どおり）。
  * `deadline` を渡すと、回収を打ち切って**未回収の位置を `dropped: true` で埋める**（黙って None にしない）。
  * 打ち切りは**壁時計で効く**（遅い worker を待たない）。走っていないジョブは `cancel_futures` で取り消す。
  * `FREEAGENT_CALL_BUDGET=0` なら `call_budget()` は None（無制限・従来と同じ）。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
os.environ.setdefault("FREEAGENT_STATE_DIR", tempfile.mkdtemp(prefix="fa-budget-test-"))

import freeagent_bind.server as S  # noqa: E402


class TestRunParallelBase(unittest.TestCase):
    def test_order_is_preserved_and_exceptions_are_absorbed(self):
        jobs = list(range(6))

        def worker(i):
            time.sleep(0.01 * (6 - i))      # 遅い順に完了させる（順序が崩れやすい並び）
            if i == 3:
                raise RuntimeError("boom")
            return {"i": i}

        got = S.run_parallel(jobs, worker, max_workers=3)
        self.assertEqual([g.get("i") for g in got if "error" not in g], [0, 1, 2, 4, 5])
        self.assertIn("boom", got[3]["error"])

    def test_no_deadline_means_no_dropped(self):
        got = S.run_parallel([1, 2, 3], lambda i: {"i": i}, max_workers=2)
        self.assertEqual(got, [{"i": 1}, {"i": 2}, {"i": 3}])
        self.assertFalse(any(g.get("dropped") for g in got))


class TestDeadlineDrops(unittest.TestCase):
    def test_expired_deadline_drops_everything(self):
        d = S.Deadline(0.01)
        time.sleep(0.02)
        got = S.run_parallel([1, 2], lambda i: {"i": i}, max_workers=2, deadline=d)
        self.assertEqual(len(got), 2)
        for g in got:
            self.assertTrue(g.get("dropped"))
            self.assertTrue(g.get("timed_out"))
            self.assertIn("予算", g["error"])

    def test_wall_clock_cutoff_does_not_wait_for_slow_workers(self):
        """1.0 秒かかる worker を 0.1 秒の予算で打ち切る → 0.5 秒以内に返る。"""
        start = time.monotonic()
        got = S.run_parallel([1, 2], lambda i: time.sleep(1.0) or {"i": i},
                             max_workers=2, deadline=S.Deadline(0.1))
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5, f"打ち切りが壁時計で効いていない（{elapsed:.2f} 秒）")
        self.assertTrue(all(g.get("dropped") for g in got), got)

    def test_finished_work_is_kept_and_missing_slots_are_dropped(self):
        """速い 1 件は残り、遅い 1 件は dropped になる（位置は入力順のまま）。"""

        def worker(i):
            if i == 0:
                return {"i": 0}
            time.sleep(1.0)
            return {"i": 1}

        got = S.run_parallel([0, 1], worker, max_workers=2, deadline=S.Deadline(0.25))
        self.assertEqual(got[0], {"i": 0})
        self.assertTrue(got[1].get("dropped"))

    def test_cancel_futures_prevents_queued_jobs_from_running(self):
        """並列度 1・予算 0.05 秒 → 走り出した 1 件以外は**実行されない**（枠を無駄に使わない）。"""
        ran = []
        lock = threading.Lock()

        def worker(i):
            with lock:
                ran.append(i)
            time.sleep(0.3)
            return {"i": i}

        got = S.run_parallel([0, 1, 2, 3], worker, max_workers=1, deadline=S.Deadline(0.05))
        self.assertLessEqual(len(ran), 1, f"取り消されずに走った: {ran}")
        self.assertEqual(sum(1 for g in got if g.get("dropped")), 4 - len(ran))


class TestCallBudget(unittest.TestCase):
    def test_budget_zero_disables_the_deadline(self):
        with mock.patch.object(S, "CALL_BUDGET_S", 0.0):
            self.assertIsNone(S.call_budget())

    def test_budget_is_applied_to_ask_many(self):
        with mock.patch.object(S, "CALL_BUDGET_S", 5.0), \
                mock.patch.object(S, "run_parallel", return_value=[]) as rp:
            S.ask_many(["nous/a"], "q")
        self.assertIsNotNone(rp.call_args.kwargs.get("deadline"))
        self.assertAlmostEqual(rp.call_args.kwargs["deadline"].total, 5.0, places=3)

    def test_budget_is_applied_to_ask_map(self):
        with mock.patch.object(S, "CALL_BUDGET_S", 7.0), \
                mock.patch.object(S, "run_parallel", return_value=[]) as rp:
            S.ask_map([("nous/a", "p")])
        self.assertAlmostEqual(rp.call_args.kwargs["deadline"].total, 7.0, places=3)


class TestDroppedIsVisibleToTheTool(unittest.TestCase):
    def test_tool_map_reports_a_dropped_item_as_an_error(self):
        """脱落は results の error として見える（黙って減らない）。"""
        dropped = S._dropped_result(S.Deadline(30), "回収を打ち切り")
        with mock.patch.object(S, "run_parallel", return_value=[dropped]):
            r = S.tool_map({"items": ["a"], "instruction": "i", "model": "nous/x"})
        self.assertTrue(r["results"][0].get("error"))
        self.assertIn("予算", r["results"][0]["error"])


if __name__ == "__main__":
    unittest.main()
