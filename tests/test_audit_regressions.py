"""追加監査で再現した不具合の回帰テスト。"""
import concurrent.futures
import http.client
import http.server
import io
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import tempfile
import unittest
import urllib.error
import urllib.request
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

    def test_urlopen_passes_the_read_budget_to_the_shared_opener(self):
        """新契約: `_urlopen` は接続/読み取りを分離した共有 opener へ読み取り上限を渡す。"""
        response = mock.Mock()
        with mock.patch.object(S._OPENER, "open", return_value=response) as open_url:
            self.assertIs(S._urlopen(S.urllib.request.Request("https://example.invalid"), 37), response)
        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.kwargs["timeout"], 37)

    def test_connect_budget_applies_only_while_connecting(self):
        """接続中だけ短い上限を使い、接続後は読み取り上限へ戻す（10 秒制約の根本対策）。"""
        seen = []

        class _FakeConnection:
            def __init__(self, timeout):
                self.timeout = timeout
                self.sock = mock.Mock()

            def connect(self):
                seen.append(self.timeout)  # 接続中に見えている上限
                self.sock.settimeout(self.timeout)

        class _Probe(S._ShortConnectMixin, _FakeConnection):
            pass

        conn = _Probe(180.0)
        with mock.patch.object(S, "CONNECT_TIMEOUT", 7.5):
            conn.connect()
        self.assertEqual(seen, [7.5], "接続中は接続上限を使う")
        self.assertEqual(conn.timeout, 180.0, "接続後は読み取り上限へ戻す")
        conn.sock.settimeout.assert_called_with(180.0)

    def test_short_connect_classes_keep_stdlib_behaviour(self):
        """既定の HTTP/HTTPS 実装を継承し、リダイレクト・プロキシ・HTTPError の既定挙動を保つ。"""
        self.assertTrue(issubclass(S._ShortConnectHTTPConnection, http.client.HTTPConnection))
        self.assertTrue(issubclass(S._ShortConnectHTTPSConnection, http.client.HTTPSConnection))
        self.assertTrue(issubclass(S._HTTPHandler, urllib.request.HTTPHandler))
        self.assertTrue(issubclass(S._HTTPSHandler, urllib.request.HTTPSHandler))

    def test_handlers_pass_our_connection_classes_to_do_open(self):
        """ハンドラが自前の接続クラスを do_open へ渡す。3.14 の HTTPSHandler には
        `_check_hostname` が無い（実バックエンド検証で AttributeError を検出した）ので、
        版差を吸収して context だけを渡せていることも同時に確認する。"""
        seen = {}

        def fake_do_open(self, http_class, req, **kwargs):
            seen["class"] = http_class
            seen["kwargs"] = kwargs
            return "response"

        with mock.patch.object(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open):
            self.assertEqual(S._HTTPHandler().http_open(
                urllib.request.Request("http://example.invalid")), "response")
        self.assertIs(seen["class"], S._ShortConnectHTTPConnection)
        self.assertEqual(seen["kwargs"], {})

        with mock.patch.object(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open):
            self.assertEqual(S._HTTPSHandler().https_open(
                urllib.request.Request("https://example.invalid")), "response")
        self.assertIs(seen["class"], S._ShortConnectHTTPSConnection)
        self.assertIn("context", seen["kwargs"])
        if not hasattr(urllib.request.HTTPSHandler, "_check_hostname"):
            self.assertNotIn("check_hostname", seen["kwargs"],
                             "3.14 以降は check_hostname を渡さない（HTTPSConnection が受け付けない）")

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

    # ---------------------------------------------------------------- 根拠注入と引用検査

    def test_evidence_block_injects_summary_and_respects_budgets(self):
        cites = [{"title": "A", "url": "u1", "summary": "S" * 500},
                 {"title": "B", "url": "u2", "summary": "T" * 500}]
        block = S._evidence_block(cites)
        self.assertIn("[1] A u1", block)
        self.assertIn("S" * 100, block)
        # 1 件あたりの上限を超えて詰め込まない
        self.assertLessEqual(max(len(line.strip()) for line in block.splitlines()), 400)
        tight = S._evidence_block(cites, item_chars=100, total_chars=150)
        self.assertIn("S" * 100, tight)
        self.assertIn("T" * 50, tight)
        self.assertNotIn("T" * 60, tight)

    def test_evidence_block_excludes_headers_without_body_evidence(self):
        block = S._evidence_block([{"title": "A", "url": "u1", "year": 2020},
                                   {"title": "B", "url": "u2"}])
        self.assertEqual(block, "")

    def test_evidence_block_honours_external_numbering(self):
        block = S._evidence_block([{"title": "A", "url": "u1", "summary": "Body A"},
                                   {"title": "B", "url": "u2", "summary": "Body B"}],
                                  numbers=[3, 4])
        self.assertIn("[3] A u1", block)
        self.assertIn("[4] B u2", block)

    def test_plain_text_strips_jats_markup(self):
        self.assertEqual(S._plain_text("<jats:p>Hello  <b>world</b></jats:p>"), "Hello world")

    def test_openalex_abstract_is_rebuilt_from_inverted_index(self):
        row = {"abstract_inverted_index": {"We": [0], "propose": [1], "Transformer": [2]}}
        self.assertEqual(S._openalex_abstract(row), "We propose Transformer")
        self.assertEqual(S._openalex_abstract({"abstract_inverted_index": None}), "")

    def test_wikipedia_citation_carries_summary(self):
        S._KB_CACHE.pop("wiki:ja:cite-summary:1", None)
        page = {"query": {"pages": {"1": {"title": "T", "index": 1,
                                          "extract": "<p>Body text</p>"}}}}
        with mock.patch.object(S, "kb_json", return_value=(page, "")):
            got = S.kb_wikipedia("cite-summary", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "Body text")
        self.assertIn("Body text", S._evidence_block(got["citations"]))

    def test_arxiv_citation_carries_summary(self):
        S._KB_CACHE.pop("arxiv:all:cite-summary:1", None)
        atom = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
                "<entry><id>i1</id><title>Paper Title</title>"
                "<summary>Abstract body</summary><published>2020-01-01T00:00:00Z</published>"
                "<link rel='alternate' href='https://arxiv.org/abs/1'/></entry></feed>")
        with mock.patch.object(S, "_arxiv_throttle"), \
             mock.patch.object(S, "kb_http", return_value=(200, atom)):
            got = S.kb_arxiv("cite-summary", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "Abstract body")

    def test_crossref_citation_carries_abstract_without_tags(self):
        S._KB_CACHE.pop("crossref:cite-summary:1", None)
        payload = {"message": {"items": [{"title": ["T"], "DOI": "10.1/x",
                                        "abstract": "<jats:p>Plain abstract</jats:p>",
                                        "URL": "https://doi.org/10.1/x"}]}}
        with mock.patch.object(S, "kb_json", return_value=(payload, "")):
            got = S.kb_crossref("cite-summary", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "Plain abstract")

    def test_openalex_citation_carries_rebuilt_abstract(self):
        S._KB_CACHE.pop("openalex:cite-summary:1", None)
        payload = {"results": [{"title": "T", "id": "https://openalex.org/W1",
                                "abstract_inverted_index": {"Body": [0], "text": [1]}}]}
        with mock.patch.object(S, "kb_json", return_value=(payload, "")):
            got = S.kb_openalex("cite-summary", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "Body text")

    def test_github_citation_carries_description(self):
        S._KB_CACHE.pop("gh:repo:cite-summary:1:False", None)
        payload = {"items": [{"full_name": "o/r", "html_url": "https://github.com/o/r",
                              "description": "Repo description"}]}
        with mock.patch.object(S, "kb_json", return_value=(payload, "")):
            got = S.kb_github("cite-summary", kind="repo", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "Repo description")

    def test_wikidata_citation_carries_description(self):
        S._KB_CACHE.pop("wd:ja:cite-summary:1", None)
        responses = [
            ({"search": [{"id": "Q1", "label": "Thing", "description": "A thing"}]}, ""),
            ({"entities": {"Q1": {"claims": {}}}}, ""),
        ]
        with mock.patch.object(S, "kb_json", side_effect=responses):
            got = S.kb_wikidata("cite-summary", limit=1)
        self.assertEqual(got["citations"][0]["summary"], "A thing")

    def test_cited_numbers_separates_real_and_phantom_citations(self):
        self.assertEqual(S._cited_numbers("見解 [1] と [2]", 2), ([1, 2], []))
        self.assertEqual(S._cited_numbers("見解 [7]", 2), ([], [7]))
        self.assertEqual(S._cited_numbers("根拠なし [0]", 0), ([], []))
        self.assertEqual(S._cited_numbers("[1]", 0), ([], [1]))

    def test_grounded_injects_evidence_body_into_the_sub_prompt(self):
        kb = {"citations": [{"source": "arxiv", "title": "Paper", "url": "https://x/1",
                             "summary": "Unique evidence sentence."}],
              "sources": ["arxiv"], "errors": {}}
        seen: list[str] = []

        def fake_ask(refs, prompt, **kwargs):
            seen.append(prompt)
            return [{"ref": refs[0], "served_by": refs[0], "text": "答え [1]"}]

        with mock.patch.object(S, "knowledge_lookup", return_value=kb), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_grounded({"question": "Q"})
        self.assertIn("Unique evidence sentence.", seen[0])
        self.assertEqual(got["answers_with_citations"], 1)

    def test_agent_loop_numbers_evidence_and_verifies_citations(self):
        lookup = {"results": {"wikipedia": {"items": [{"title": "T", "summary": "Body"}]}},
                  "citations": [{"source": "wikipedia", "title": "T", "url": "u1",
                                 "summary": "Body"}],
                  "errors": {}}
        replies = [{"ref": "nous/a", "served_by": "nous/a",
                    "text": '{"tool": "lookup", "query": "q"}'},
                   {"ref": "nous/a", "served_by": "nous/a", "text": '{"answer": "結論は [1] です"}'}]
        prompts: list[str] = []

        def fake_call(ref, prompt, *, system="", **kwargs):
            prompts.append(prompt)
            return replies[len(prompts) - 1]

        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=fake_call), \
             mock.patch.object(S, "knowledge_lookup", return_value=lookup):
            got = S.tool_agent({"task": "t", "max_steps": 2})
        row = got["agents"][0]
        self.assertEqual(row["cited"], [1])
        self.assertTrue(row["cited_ok"])
        self.assertEqual(got["answers_with_citations"], 1)
        # 注入された根拠は番号つきの本文（タイトルだけでは幻覚が減らない）
        self.assertIn("[1] T u1", prompts[1])
        self.assertIn("Body", prompts[1])

    def test_agent_loop_flags_phantom_citation_numbers(self):
        lookup = {"results": {"wikipedia": {"items": [{"title": "T", "summary": "Body"}]}},
                  "citations": [{"source": "wikipedia", "title": "T", "url": "u1",
                                 "summary": "Body"}],
                  "errors": {}}
        replies = [{"ref": "nous/a", "text": '{"tool": "lookup", "query": "q"}'},
                   {"ref": "nous/a", "text": '{"answer": "根拠は [5] です"}'}]
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=replies), \
             mock.patch.object(S, "knowledge_lookup", return_value=lookup):
            got = S.tool_agent({"task": "t", "max_steps": 2})
        row = got["agents"][0]
        self.assertEqual(row["cited"], [])
        self.assertFalse(row["cited_ok"])
        self.assertEqual(row["unsupported_citations"], [5])
        self.assertEqual(got["unsupported_citations"], [5])
        self.assertEqual(got["answers_with_citations"], 0)

    def test_agent_marks_answers_that_used_no_evidence(self):
        replies = [{"ref": "nous/a", "text": '{"answer": "記憶で答えます [0]"}'}]
        with mock.patch.object(S, "_select_or_error", return_value=(["nous/a"], {})), \
             mock.patch.object(S, "call_model", side_effect=replies):
            got = S.tool_agent({"task": "t", "max_steps": 1})
        row = got["agents"][0]
        self.assertEqual(row["cited"], [])
        self.assertEqual(row["unsupported_citations"], [])
        self.assertFalse(row["cited_ok"])
        self.assertIn("引用なし", S.render("freeagent_agent", got))


class _HeaderDelayHandler(http.server.BaseHTTPRequestHandler):
    """ヘッダを遅らせるサーバ（生成完了までヘッダを返さないローカルプロキシの模擬）。"""

    protocol_version = "HTTP/1.0"

    def do_GET(self):
        if self.path.startswith("/stall"):
            time.sleep(5.0)
            self._respond(200, b'{"late": true}')
        elif self.path.startswith("/slow"):
            time.sleep(0.8)
            self._respond(200, b'{"ok": true}')
        elif self.path.startswith("/redirect"):
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path.startswith("/missing"):
            self._respond(404, b"nope")
        else:
            self._respond(200, b'{"ok": true}')

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestHttpTimeoutSeparation(unittest.TestCase):
    """接続と読み取りの分離を localhost の遅延サーバで実測する（外部ネットワーク不要）。

    修正前: `urlopen(timeout=CONNECT_TIMEOUT)` の上限は**応答ヘッダの待ち時間にも掛かり**、
    生成が 10 秒を超えるローカルプロキシでは読み取り上限（既定 180 秒）が使われずに
    `TimeoutError` になった（実測: 1200 トークンの要求が 10.0 秒で失敗）。
    修正後は接続だけが接続上限の対象で、ヘッダ待ちと本文は読み取り上限まで待てる。
    """

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HeaderDelayHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # 環境のプロキシ設定で 127.0.0.1 が回り道をしないよう、同じハンドラで組んだ opener を使う。
        cls.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), S._HTTPHandler, S._HTTPSHandler)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        patcher = mock.patch.object(S, "_OPENER", self.opener)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _get(self, path, read_timeout=10.0):
        return S._urlopen(urllib.request.Request(self._url(path)), read_timeout)

    def test_header_wait_is_not_capped_by_the_connect_budget(self):
        with mock.patch.object(S, "CONNECT_TIMEOUT", 0.25):
            with self._get("/slow") as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn(b"ok", resp.read())

    def test_read_budget_still_caps_a_stalled_response(self):
        with mock.patch.object(S, "CONNECT_TIMEOUT", 0.25):
            with self.assertRaises((TimeoutError, socket.timeout, urllib.error.URLError)):
                self._get("/stall", read_timeout=0.3).read()

    def test_unreachable_host_fails_fast_within_the_connect_budget(self):
        started = time.time()
        with mock.patch.object(S, "CONNECT_TIMEOUT", 0.5):
            with self.assertRaises(urllib.error.URLError):
                S._urlopen(urllib.request.Request("http://127.0.0.1:9/"), 30.0)
        self.assertLess(time.time() - started, 5.0, "接続できないホストを読み取り上限まで待たない")

    def test_redirects_are_followed(self):
        with self._get("/redirect") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"ok", resp.read())

    def test_http_errors_still_raise_httperror(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/missing")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()


class TestMeasureAdoptionScenes(unittest.TestCase):
    """自発利用の「必要場面」計測（依頼文からの近似判定）を一時 state.db で検査する。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "scripts", "measure_adoption.py")
        spec = importlib.util.spec_from_file_location("measure_adoption", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_classify_prompts_matches_japanese_and_english_cues(self):
        self.assertEqual(self.mod.classify_prompts(["この設計の弱点を別のAIにも聞いて"]), ["multi_view"])
        self.assertEqual(self.mod.classify_prompts(["出典つきで調べて"]), ["sources"])
        self.assertEqual(self.mod.classify_prompts(["原因を切り分けて手順に分解して"]), ["stepwise"])
        self.assertEqual(self.mod.classify_prompts(["100 件をそれぞれ要約して"]), ["bulk"])
        self.assertEqual(self.mod.classify_prompts(["Please review this and cite sources"]),
                         ["sources", "multi_view"])

    def test_classify_prompts_ignores_small_talk(self):
        for text in ("おはよう", "1+1 は？", "ありがとう", "", None):
            self.assertEqual(self.mod.classify_prompts([text]), [], repr(text))

    def test_classify_prompts_returns_each_cue_once(self):
        self.assertEqual(self.mod.classify_prompts(["根拠を調べて", "根拠をもう一度"]), ["sources"])

    def test_classify_prompts_ignores_self_reading_review_requests(self):
        """実測: 「読み取り専用の再レビュー。…を読み、弱点があれば指摘して。」は自分で読む依頼で、
        freeagent に投げる場面ではない（分類器の誤検出が見逃しを水増ししていた）。"""
        for text in ("読み取り専用の再レビュー。server.py を読み、弱点があれば指摘して。",
                     "読み取り専用コードレビュー。git diff を読んで、見落としを確認して。",
                     "複数視点の相談機能が使えません。読み取り専用で状態を調べてください。"):
            self.assertEqual(self.mod.classify_prompts([text]), [], text)
        # 同じ語でも「読み取り専用」が無ければ必要場面として拾う
        self.assertEqual(self.mod.classify_prompts(["この設計の弱点を指摘して"]), ["multi_view"])

    def test_classify_prompts_excludes_service_and_terms_requests(self):
        """実測: サービス・提供元・公式条件を探す依頼は学術コーパス外なので見逃しに数えない。

        `freeagent_lookup` に "LLM inference API provider comparison alternatives" を引くと
        **論文 9 件が返り、提供元の一覧は返らない**（＝この型は web_search が正しい選択）。
        """
        for text in (
            "hermes freeagent bindの推論バックエンドAPIを、より良いAPIがあるか世界中から見つけて"
            "代用できるか検討して下さい。プログラムや科学分野について利用できそうなAPIは無いか調べて下さい",
            "第3段階候補J-STAGE/CiNii Research/COREの公式利用条件と現在API仕様を短く再調査。"
            "商用承認・登録・検索サービス組込制限を実際に公式文書読んで根拠URLと引用文付きで報告。",
        ):
            self.assertEqual(self.mod.classify_prompts([text]), [], text)
        # 学術コーパスの語があれば除外しない（API の話でも「論文を探して」は必要場面）
        self.assertEqual(self.mod.classify_prompts(["この API の設計に関する論文を探して"]), ["sources"])
        self.assertEqual(self.mod.classify_prompts(["宇宙エレベータの材料研究について出典つきで調べて"]),
                         ["sources"])

    def test_classify_prompts_ignores_attached_context(self):
        """実測: `@url` で貼られたページ本文の英語 "sources" を依頼文と誤認していた。"""
        attached = ("ブラウザベースのAIを複数集め会話させる実装は可能か？\n"
                    "@url:`https://gemini.google.com/app`\n\n--- Attached Context ---\n\n"
                    "[Sign in](https://accounts.google.com/ServiceLogin?continue=https://x)\n"
                    "utm_source=gemini / 参考 sources の一覧\n")
        self.assertEqual(self.mod.classify_prompts([attached]), [])
        # 依頼文の側に手掛かりがあれば当然拾う
        self.assertEqual(
            self.mod.classify_prompts(["出典つきで調べて\n\n--- Attached Context ---\nsources の一覧\n"]),
            ["sources"])

    def test_classify_prompts_ignores_machine_notices(self):
        """実測: 委譲の完了通知（機械が差し込む user ロール）の本文が誤検出されていた。"""
        notice = ("[ASYNC DELEGATION BATCH COMPLETE — deleg_0732bc70] A background fan-out unit "
                  "you dispatched earlier — 1 subagent(s) — has finished; its consolidated report "
                  "lists sources and citations.")
        self.assertEqual(self.mod.classify_prompts([notice]), [])
        # 日本語の依頼が角括弧で始まっても依頼文として扱う
        self.assertEqual(self.mod.classify_prompts(["[重要] 出典つきで調べて"]), ["sources"])

    def _db(self, tmp):
        """必要場面 2 件（うち 1 件は未使用）＋雑談 1 件の最小 state.db を作る。"""
        path = os.path.join(tmp, "state.db")
        con = sqlite3.connect(path)
        con.execute("create table sessions (id text primary key, started_at real)")
        con.execute("create table messages (id integer primary key autoincrement,"
                    " session_id text, role text, content text, tool_calls text, timestamp real)")
        freeagent = json.dumps([{"function": {"name": "mcp__freeagent_bind__freeagent_panel",
                                              "arguments": "{}"}}])
        competing = json.dumps([{"function": {"name": "web_search", "arguments": "{}"}}])
        rows = [("s1", "出典つきで調べて", competing),
                ("s2", "この設計の弱点を別のAIにも聞いて", freeagent),
                ("s3", "おはよう", competing)]
        for i, (sid, prompt, calls) in enumerate(rows):
            con.execute("insert into sessions values (?, ?)", (sid, 1000.0 + i))
            con.execute("insert into messages (session_id, role, content, tool_calls, timestamp)"
                        " values (?, 'user', ?, null, ?)", (sid, prompt, 1000.0 + i))
            con.execute("insert into messages (session_id, role, content, tool_calls, timestamp)"
                        " values (?, 'assistant', '', ?, ?)", (sid, calls, 1000.0 + i))
        con.commit()
        con.close()
        return path

    def test_main_reports_needed_scene_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", ["measure_adoption.py", "--db", db]), \
                 mock.patch.object(sys, "stdout", buf):
                self.assertEqual(self.mod.main(), 0)
        out = buf.getvalue()
        self.assertIn("必要場面（依頼文からの近似判定）: 2/3 セッション", out)
        self.assertIn("必要場面の採用率 50%", out)
        self.assertIn("見逃し 1", out)
        self.assertIn("web_search", out)

    def test_main_json_exposes_needed_scenes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            buf = io.StringIO()
            with mock.patch.object(sys, "argv", ["measure_adoption.py", "--db", db, "--json"]), \
                 mock.patch.object(sys, "stdout", buf):
                self.assertEqual(self.mod.main(), 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["needed_scenes"]["needed_sessions"], 2)
        self.assertEqual(data["needed_scenes"]["needed_using_freeagent"], 1)
        self.assertEqual(data["needed_scenes"]["needed_rate"], 0.5)

    def test_min_needed_rate_gate_returns_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            with mock.patch.object(sys, "argv",
                                   ["measure_adoption.py", "--db", db, "--min-needed-rate", "0.9"]), \
                 mock.patch.object(sys, "stdout", io.StringIO()), \
                 mock.patch.object(sys, "stderr", io.StringIO()):
                self.assertEqual(self.mod.main(), 1)

    def test_include_tool_free_widens_the_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = self._db(tmp)
            con = sqlite3.connect(db)  # messages を持たないセッションを足す
            con.execute("insert into sessions values ('s4', 2000.0)")
            con.commit()
            con.close()
            buf = io.StringIO()
            with mock.patch.object(sys, "argv",
                                   ["measure_adoption.py", "--db", db, "--include-tool-free"]), \
                 mock.patch.object(sys, "stdout", buf):
                self.assertEqual(self.mod.main(), 0)
        self.assertIn("対象セッション: 4 件", buf.getvalue())
        self.assertIn("ツール未使用も含む", buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
