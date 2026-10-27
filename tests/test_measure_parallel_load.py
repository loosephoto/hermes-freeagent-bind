"""Offline benchmark exercises actual run_parallel without network or state writes."""
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "measure_parallel_load.py")


class ParallelLoadBenchmarkTest(unittest.TestCase):
    def test_readme_explains_worker_limit_is_per_invocation(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        self.assertIn("呼び出しごとの上限", readme)
        self.assertNotIn("クライアントを増やしても並列度は増えません", readme)

    def test_reports_real_parallel_runs_as_json(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--calls", "2", "--jobs", "4",
                               "--workers", "4", "--delay", "0.5", "--budget", "5"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["requested_jobs"], 8)
        self.assertEqual(result["completed_jobs"], 8)
        self.assertEqual(result["dropped_jobs"], 0)
        self.assertGreater(result["peak_active_workers"], 4)
        self.assertIn("peak_process_threads", result)
        self.assertGreaterEqual(result["peak_process_threads"], result["peak_active_workers"])
        self.assertEqual(result["active_workers_after_drain"], 0)

    def test_timeout_reports_workers_that_continue_then_drain(self):
        proc = subprocess.run([sys.executable, SCRIPT, "--calls", "2", "--jobs", "2",
                               "--workers", "2", "--delay", "1.0", "--budget", "0.3"],
                              cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["dropped_jobs"], 4)
        self.assertEqual(result["active_workers_at_return"], 4)
        self.assertTrue(result["drained"])
        self.assertEqual(result["active_workers_after_drain"], 0)
        self.assertEqual(result["finished_after_drain"], 4)


if __name__ == "__main__":
    unittest.main()
