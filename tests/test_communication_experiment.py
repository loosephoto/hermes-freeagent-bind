"""Opt-in shared-evidence experiment and objective evaluation regressions."""
import os
import sys
import threading
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-communication-tests"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from freeagent_bind import server as S  # noqa: E402


class TestSharedEvidenceExperiment(unittest.TestCase):
    def test_render_shows_opt_in_experiment_score_and_cost(self):
        text = S.render("freeagent_agent", {
            "answered": 1, "models": ["openrouter/model"], "tool_calls": 1,
            "citation_count": 1, "answers_with_citations": 1, "agreement": 1.0,
            "agents": [], "citations": [], "usage_note": "experiment fixture",
            "experiment": {
                "task_id": "case-1", "communication": "shared_evidence",
                "objective_evaluation": {"best_agent_score": 0.75, "pass_any": False,
                                         "team_coverage_score": 1.0},
                "metrics": {"total_tokens": 180, "wall_time_s": 2.5,
                            "peer_evidence_injections": 1},
            },
        })
        self.assertIn("共有エビデンス実験", text)
        self.assertIn("0.75", text)
        self.assertIn("180", text)
        self.assertIn("2.5", text)

    def test_agent_schema_exposes_opt_in_mode_and_objective_rubric(self):
        tool = next(item for item in S.TOOLS if item["name"] == "freeagent_agent")
        properties = tool["inputSchema"]["properties"]
        self.assertEqual(properties["communication"]["enum"], ["independent", "shared_evidence"])
        self.assertIn("evaluation", properties)
        self.assertIn("required_terms", properties["evaluation"]["properties"])
        self.assertIn("required_citations", properties["evaluation"]["properties"])

    def test_evaluator_separates_best_agent_from_union_coverage(self):
        evaluation = {
            "task_id": "split-1", "required_terms": ["result"],
            "required_citations": ["10.4242/target"], "allowed_citations": ["10.4242/target"],
        }
        got = S._score_agent_results([
            {"model": "a", "answer": "The result is known.", "citations": []},
            {"model": "b", "answer": "Other answer.",
             "citations": [{"doi": "10.4242/target", "url": "https://doi.org/10.4242/target"},
                           {"doi": "10.4242/noise", "url": "https://doi.org/10.4242/noise"}]},
        ], evaluation)

        self.assertEqual(got["best_agent_score"], 0.5)
        self.assertEqual(len(got["unexpected_citations"]), 1)
        self.assertFalse(got["pass_any"])
        self.assertEqual(got["team_coverage_score"], 1.0)
        self.assertTrue(got["team_coverage_pass"])

    def test_board_deduplicates_evidence_and_never_shares_metadata_only(self):
        board = S._SharedEvidenceBoard()
        metadata = {"url": "https://example.org/meta", "summary": "metadata only", "metadata_only": True}
        no_url = {"summary": "body, but no link"}
        bad_scheme = {"url": "file:///private/value", "summary": "not an HTTP citation"}
        citation = {"source": "arxiv", "url": "https://example.org/body", "summary": "body evidence"}

        self.assertFalse(board.publish("a", metadata))
        self.assertFalse(board.publish("a", no_url))
        self.assertFalse(board.publish("a", bad_scheme))
        self.assertTrue(board.publish("a", citation))
        self.assertFalse(board.publish("b", citation))
        self.assertEqual(board.take_for("a", set()), [])
        peer = board.take_for("b", set())
        self.assertEqual(len(peer), 1)
        self.assertEqual(peer[0]["origin"], "a")

    def test_shared_mode_requires_objective_evaluation_before_selecting_models(self):
        with (
            mock.patch.object(S, "_select_or_error", side_effect=AssertionError("models must not be selected")),
            mock.patch.object(S, "call_model", side_effect=AssertionError("must not infer")),
        ):
            got = S.tool_agent({"task": "Find a result", "communication": "shared_evidence"})

        self.assertIn("evaluation", got["error"])

    def test_shared_mode_requires_two_selected_agents(self):
        evaluation = {"task_id": "one-agent", "required_terms": ["answer"]}
        with (
            mock.patch.object(S, "_select_or_error", return_value=(["openrouter/model"], {})),
            mock.patch.object(S, "call_model", side_effect=AssertionError("single-agent mode must not run")),
        ):
            got = S.tool_agent({"task": "Find an answer", "communication": "shared_evidence",
                                "evaluation": evaluation, "max_steps": 1})

        self.assertIn("2", got["error"])

    def test_default_mode_stays_independent_and_does_not_create_shared_board(self):
        with (
            mock.patch.object(S, "_select_or_error", return_value=(["openrouter/model"], {})),
            mock.patch.object(S, "_SharedEvidenceBoard", side_effect=AssertionError("default must not share")),
            mock.patch.object(S, "call_model", return_value={
                "text": '{"answer":"A checked answer."}',
                "tokens": {"prompt": 12, "completion": 3}, "latency_s": 0.1,
            }),
        ):
            got = S.tool_agent({"task": "Find a result", "max_steps": 1})

        self.assertEqual(got["communication"], "independent")
        self.assertNotIn("shared_evidence", got)
        self.assertEqual(got["agents"][0]["answer"], "A checked answer.")

    def test_shared_mode_publishes_valid_tool_evidence_to_call_local_board(self):
        refs = ["openrouter/model-a", "nvidia/model-b"]
        counters = {ref: 0 for ref in refs}
        counter_lock = threading.Lock()

        def call(ref, prompt, **kwargs):
            with counter_lock:
                counters[ref] += 1
                step = counters[ref]
            if step == 1:
                text = '{"tool":"lookup","query":"' + ref + '"}'
            else:
                text = '{"answer":"The result is supported."}'
            return {"text": text, "served_by": ref,
                    "tokens": {"prompt": 10, "completion": 5}, "latency_s": 0.01}

        def tool(parsed):
            ref = parsed["query"]
            return {"hits": 1, "brief": [f"Evidence from {ref}"], "bibliography": [],
                    "citations": [{"source": "fixture", "title": ref,
                                   "url": f"https://example.org/{ref}",
                                   "summary": f"Source evidence from {ref}."}]}

        with (
            mock.patch.object(S, "_select_or_error", return_value=(refs, {})),
            mock.patch.object(S, "call_model", side_effect=call),
            mock.patch.object(S, "_agent_tool_call", side_effect=tool),
        ):
            got = S.tool_agent({
                "task": "Find the result.", "max_steps": 2,
                "communication": "shared_evidence",
                "evaluation": {"task_id": "publish-1", "required_terms": ["result"]},
            })

        self.assertEqual(got["experiment"]["metrics"]["published_evidence"], 2)
        self.assertEqual(got["experiment"]["metrics"]["model_calls"], 4)
        self.assertEqual(len(got["citations"]), 2)

    def test_shared_mode_injects_only_peer_evidence_and_scores_objective_criteria(self):
        citation = {
            "source": "arxiv", "title": "Fixture paper", "url": "https://arxiv.org/abs/10.4242/demo",
            "doi": "10.4242/demo", "summary": "The benchmark result is 7."
        }
        original_board = S._SharedEvidenceBoard

        class BoardWithSeed(original_board):
            def __init__(self):
                super().__init__()
                self.publish("peer-model", citation)

        prompts = []

        def answer(ref, prompt, **kwargs):
            prompts.append(prompt)
            return {"text": '{"answer":"The benchmark result is 7. [1]"}',
                    "served_by": ref, "tokens": {"prompt": 20, "completion": 8}, "latency_s": 0.25}

        with (
            mock.patch.object(S, "_select_or_error", return_value=(["openrouter/model", "nvidia/model"], {})),
            mock.patch.object(S, "_SharedEvidenceBoard", BoardWithSeed),
            mock.patch.object(S, "call_model", side_effect=answer),
        ):
            got = S.tool_agent({
                "task": "Find the benchmark result.", "max_steps": 1,
                "communication": "shared_evidence",
                "evaluation": {
                    "task_id": "fixture-1", "required_terms": ["result is 7"],
                    "required_citations": ["10.4242/demo", "https://arxiv.org/abs/10.4242/demo"],
                    "allowed_citations": ["10.4242/demo"],
                },
            })

        agent = got["agents"][0]
        self.assertEqual(got["communication"], "shared_evidence")
        self.assertIn("The benchmark result is 7.", prompts[0])
        self.assertNotIn("fixture-1", prompts[0])
        self.assertEqual(agent["cited"], [1])
        self.assertTrue(agent["cited_ok"])
        self.assertEqual(got["experiment"]["served_by_models"], ["nvidia/model", "openrouter/model"])
        self.assertEqual(got["experiment"]["served_by_by_agent"], [
            {"requested": "openrouter/model", "served_by": ["openrouter/model"]},
            {"requested": "nvidia/model", "served_by": ["nvidia/model"]},
        ])
        evaluation = got["experiment"]["objective_evaluation"]
        self.assertEqual(evaluation["best_agent_score"], 1.0)
        self.assertTrue(evaluation["pass_any"])
        metrics = got["experiment"]["metrics"]
        self.assertEqual(metrics["peer_evidence_injections"], 2)
        self.assertEqual(metrics["prompt_tokens"], 40)
        self.assertEqual(metrics["completion_tokens"], 16)
        self.assertEqual(metrics["total_tokens"], 56)


if __name__ == "__main__":
    unittest.main()