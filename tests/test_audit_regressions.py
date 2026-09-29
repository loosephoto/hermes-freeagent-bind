"""追加監査で再現した不具合の回帰テスト。"""
import concurrent.futures
import io
import json
import os
import sys
import time
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-audit-tests"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from freeagent_bind import server as S  # noqa: E402


class TestAuditRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-audit-")
        self.env = {key: os.environ.get(key) for key in (
            "FREEAGENT_STATE_DIR", "FREEAGENT_STATS_PATH", "FREEAGENT_SESSIONS_PATH")}
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        os.environ["FREEAGENT_STATS_PATH"] = os.path.join(self.tmp, "stats.json")
        os.environ["FREEAGENT_SESSIONS_PATH"] = os.path.join(self.tmp, "sessions.json")
        S._STATS["models"].clear()
        S._STATS_LOADED = False
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False

    def tearDown(self):
        for key, value in self.env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        S._STATS["models"].clear()
        S._STATS_LOADED = False
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False

    def test_observations_are_persisted_and_reloaded(self):
        S.note_observation("nous/model", "ask")
        self.assertTrue(os.path.exists(S.stats_path()))
        S._STATS["models"].clear()
        S._STATS_LOADED = False
        self.assertEqual(S.model_observations("nous/model"), 1.0)

    def test_session_ids_are_unique_within_same_second(self):
        self.assertEqual(len({S.new_session_id() for _ in range(20)}), 20)

    def test_parser_handles_punctuation_integer_one_and_inline_labels(self):
        self.assertEqual(S.parse_labeled("結論: x\n確信度: 1")["confidence"], 1)
        self.assertEqual(S.parse_labeled("結論: x\nメインに確認したい点: なし。\n")["question"], "")
        self.assertEqual(S.parse_labeled("結論: 賛成 確信度: 88")["conclusion"], "賛成")

    def test_cooling_requested_model_falls_back_to_ready_alternative(self):
        with mock.patch.object(S, "free_model_refs", return_value=["nous/a", "openrouter/b"]), \
             mock.patch.object(S, "cooling_refs", return_value={"nous/a": {"until": S.now_ts()+60}}):
            self.assertEqual(S._candidates("nous/a"), ["openrouter/b"])

    def test_http_402_cools_down_only_the_exhausted_model(self):
        success = {"text": "ok", "latency_s": 0.1, "truncated": False, "tokens": {}}
        with mock.patch.object(S, "_candidates", return_value=["huggingface/a", "openrouter/b"]), \
             mock.patch.object(S, "resolve_ref", side_effect=[("huggingface", "a"), ("huggingface", "a"), ("openrouter", "b")]), \
             mock.patch.object(S, "_call_once", side_effect=[S.HttpStatusError(402, "credit depleted"), success]), \
             mock.patch.object(S, "note_cooldown") as cooldown, \
             mock.patch.object(S, "observe_call"), mock.patch.object(S, "clear_provider_auth"):
            got = S.call_model("huggingface/a", "prompt")
        self.assertEqual(got["text"], "ok")
        cooldown.assert_called_once_with("huggingface/a", S._COOLDOWN_DEFAULT_S,
                                         "HTTP 402 (credit depleted)")

    def test_panel_agreement_does_not_count_duplicate_fallback_models(self):
        def fake_ask(refs, *args, **kwargs):
            return [
                {"ref": refs[0], "served_by": "nous/actual", "text": "結論: 賛成"},
                {"ref": refs[1], "served_by": "nous/actual", "text": "結論: 反対"},
            ]
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "openrouter/b"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_panel({"question": "Q"})
        self.assertEqual(got["answered"], 2)
        self.assertEqual(got["independent_sources"], 1)
        self.assertEqual(got["agreement"], 0.0)
        self.assertIn("独立した実モデル 1 体", S.render("freeagent_panel", got))

    def test_deep_consult_preserves_questions_and_final_agreement(self):
        def fake_call(ref, prompt, *, system="", **kwargs):
            if system == S.DEBATE_SYSTEM:
                return {"ref": ref, "served_by": ref,
                        "text": "立場: 賛成\n最強の反論: X\n応答: Y\n未解決: なし"}
            return {"ref": ref, "served_by": ref,
                    "text": "結論: 賛成です\n確信度: 80\nメインに確認したい点: 予算は？"}
        with mock.patch.object(S, "call_model", side_effect=fake_call), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "nous/b"], {})):
            got = S.tool_consult({"question": "Q", "debate_depth": "deep"})
        self.assertEqual(got["stage"], "awaiting_main")
        self.assertEqual(got["open_questions_for_main"], ["予算は？"])
        self.assertGreater(got["agreement"], 0.9)
        self.assertEqual(got["debate_summary"]["participants"][0]["final_position"], "賛成")
        self.assertFalse(got["debate_summary"]["unresolved_dissent"])

    def test_unknown_only_sources_do_not_fan_out_to_every_provider(self):
        with mock.patch.object(S, "run_parallel", side_effect=AssertionError("unknown sources must not trigger HTTP")):
            got = S.knowledge_lookup("query", ["invalid-source"])
        self.assertEqual(got["sources"], [])
        self.assertEqual(got["unknown_sources"], ["invalid-source"])
        self.assertTrue(got["error"])

    def test_wikipedia_search_fetches_summaries_in_one_api_call(self):
        S._KB_CACHE.pop("wiki:ja:one-call-test:1", None)
        search_result = {"query": {"pages": {"1": {"title": "Article", "index": 1,
                                                        "extract": "Plain summary"}}}}
        with mock.patch.object(S, "kb_json", return_value=(search_result, "")) as request:
            got = S.kb_wikipedia("one-call-test", limit=1)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(got["items"][0]["summary"], "Plain summary")

    def test_kb_errors_are_not_cached(self):
        calls = []
        def producer():
            calls.append(1)
            return {"source": "x", "error": "offline"} if len(calls) == 1 else {"items": ["ok"]}
        self.assertTrue(S._kb_cached("retry-key", producer).get("error"))
        self.assertEqual(S._kb_cached("retry-key", producer), {"items": ["ok"]})
        self.assertEqual(len(calls), 2)

    def test_wikipedia_language_rejects_host_injection_without_network(self):
        with mock.patch.object(S, "kb_json", side_effect=AssertionError("network must not be called")) as request:
            got = S.kb_wikipedia("query", lang="127.1#")
        self.assertTrue(got.get("error"))
        request.assert_not_called()

    def test_kb_cache_single_flights_concurrent_identical_queries(self):
        count = []
        def producer():
            count.append(1)
            time.sleep(0.05)
            return {"items": ["ok"]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(S._kb_cached, "single-flight", producer) for _ in range(2)]
            results = [future.result(timeout=2) for future in futures]
        self.assertEqual(len(count), 1)
        self.assertEqual(results, [{"items": ["ok"]}, {"items": ["ok"]}])

    def test_urlopen_applies_distinct_connect_and_read_timeouts(self):
        sock = mock.Mock()
        response = mock.Mock()
        response.fp.raw._sock = sock
        with mock.patch.object(S.urllib.request, "urlopen", return_value=response) as open_url:
            self.assertIs(S._urlopen(S.urllib.request.Request("https://example.invalid"), 37), response)
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.kwargs["timeout"], S.CONNECT_TIMEOUT)
        sock.settimeout.assert_called_once_with(37)

    def test_invalid_jsonrpc_shapes_do_not_kill_stdio_server(self):
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]
        single = {"jsonrpc": "2.0", "id": 2, "method": "ping"}
        stdin = io.StringIO("123" + chr(10) + json.dumps(batch) + chr(10) + json.dumps(single) + chr(10))
        stdout = io.StringIO()
        with mock.patch.object(S.sys, "stdin", stdin), mock.patch.object(S.sys, "stdout", stdout):
            S.serve()
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual([row["id"] for row in responses[1]], [1])
        self.assertEqual(responses[2]["id"], 2)

    def test_render_failure_is_converted_to_tool_error(self):
        with mock.patch.dict(S.HANDLERS, {"freeagent_lookup": lambda _: {"result": "ok"}}):
            with mock.patch.object(S, "render", side_effect=RuntimeError("render broke")):
                got = S.handle_tool_call({"name": "freeagent_lookup", "arguments": {"query": "x"}})
        self.assertTrue(got["isError"])
        self.assertIn("structuredContent", got)

    def test_rate_classifier_does_not_match_arbitrary_substrings(self):
        self.assertEqual(S.classify_error("generate request failed"), "other")

    def test_consult_deduplicates_identical_open_questions(self):
        def fake_call(ref, prompt, *, system="", **kwargs):
            return {"ref": ref, "served_by": ref,
                    "text": "結論: OK\n確信度: 80\nメインに確認したい点: 予算？"}
        with mock.patch.object(S, "call_model", side_effect=fake_call), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "nous/b"], {})):
            got = S.tool_consult({"question": "Q"})
        self.assertEqual(got["open_questions_for_main"], ["予算？"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
