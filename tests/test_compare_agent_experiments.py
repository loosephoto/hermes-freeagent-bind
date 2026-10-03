"""Offline tests for paired communication-experiment scoring."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "compare_agent_experiments.py"


def run_row(mode, score, passed, tokens, elapsed, models=None, steps=2, max_tokens=100):
    models = models or ["openrouter/model-a", "nvidia/model-b"]
    criteria = {
        "task_id": "retrieval-01", "required_terms": ["target"],
        "required_citations": ["10.1/target"], "allowed_citations": ["10.1/target"],
    }
    return {
        "structuredContent": {
            "experiment": {
                "task_id": "retrieval-01", "task_fingerprint": "sha256-same", "communication": mode,
                "models": models,
                "served_by_models": models,
                "served_by_by_agent": [{"requested": model, "served_by": [model]} for model in models],
                "max_steps": steps, "max_tokens_per_step": max_tokens,
                "criteria": criteria,
                "objective_evaluation": {
                    "best_agent_score": score, "pass_any": passed,
                    "team_coverage_score": score, "team_coverage_pass": passed,
                    "unexpected_citations": [],
                },
                "metrics": {
                    "total_tokens": tokens, "wall_time_s": elapsed,
                    "peer_evidence_injections": 1 if mode == "shared_evidence" else 0,
                    "unique_citations": 1,
                },
            }
        }
    }


class TestCompareAgentExperiments(unittest.TestCase):
    def _run(self, records):
        with tempfile.TemporaryDirectory(prefix="fa-exp-test-") as tmp:
            path = Path(tmp) / "runs.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in records), encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                                  capture_output=True, text=True, encoding="utf-8", timeout=30)

    def test_reports_paired_objective_quality_and_cost(self):
        result = self._run([
            run_row("independent", 0.5, False, 1000, 10.0),
            run_row("shared_evidence", 1.0, True, 1200, 12.0),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["paired_tasks"], 1)
        self.assertEqual(report["delta_shared_minus_independent"]["best_agent_score"], 0.5)
        self.assertEqual(report["delta_shared_minus_independent"]["total_tokens"], 200)
        self.assertEqual(report["delta_shared_minus_independent"]["wall_time_s"], 2.0)
        self.assertIn("not an automatic promotion", report["interpretation"])

    def test_rejects_unmatched_models_or_budgets(self):
        result = self._run([
            run_row("independent", 0.5, False, 1000, 10.0),
            run_row("shared_evidence", 1.0, True, 1200, 12.0, models=["other/model"]),
        ])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("models", result.stderr)

    def test_rejects_different_task_text_fingerprints(self):
        baseline = run_row("independent", 0.5, False, 1000, 10.0)
        treatment = run_row("shared_evidence", 1.0, True, 1200, 12.0)
        treatment["structuredContent"]["experiment"]["task_fingerprint"] = "sha256-different"
        result = self._run([baseline, treatment])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("task_fingerprint", result.stderr)

    def test_rejects_swapped_fallback_assignment(self):
        baseline = run_row("independent", 0.5, False, 1000, 10.0)
        treatment = run_row("shared_evidence", 1.0, True, 1200, 12.0)
        treatment["structuredContent"]["experiment"]["served_by_by_agent"] = [
            {"requested": "openrouter/model-a", "served_by": ["nvidia/model-b"]},
            {"requested": "nvidia/model-b", "served_by": ["openrouter/model-a"]},
        ]
        result = self._run([baseline, treatment])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("served_by_by_agent", result.stderr)

    def test_rejects_unmatched_per_step_budget(self):
        baseline = run_row("independent", 0.5, False, 1000, 10.0)
        treatment = run_row("shared_evidence", 1.0, True, 1200, 12.0)
        treatment["structuredContent"]["experiment"]["max_tokens_per_step"] = 250
        result = self._run([baseline, treatment])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("max_tokens_per_step", result.stderr)

    def test_rejects_different_fallback_models(self):
        baseline = run_row("independent", 0.5, False, 1000, 10.0)
        treatment = run_row("shared_evidence", 1.0, True, 1200, 12.0)
        baseline["structuredContent"]["experiment"]["served_by_models"] = ["openrouter/model-a"]
        treatment["structuredContent"]["experiment"]["served_by_models"] = ["nvidia/model-b"]
        result = self._run([baseline, treatment])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("served_by_models", result.stderr)

    def test_rejects_unpaired_task_records(self):
        result = self._run([run_row("independent", 0.5, False, 1000, 10.0)])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("paired", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()