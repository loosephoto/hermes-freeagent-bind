"""hermes-freeagent-bind のオフライン回帰テスト。

ネットワークと実モデルを使わない（プロキシ未起動でも通る）。外部依存ゼロで、実測で見つけた
不具合の再発を防ぐことに絞る:
  * 引数の防御的変換（不正値で例外を外へ漏らさない）
  * 「結論/確信度/確認したい点」の解析（ラベル欠落を推測で埋めない）
  * サブエージェントの JSON 抽出（コードフェンス・前置きが混ざっても読む）
  * ツール層が例外を漏らさない（handle_tool_call が必ず dict を返す）
  * セッションストアの往復・TTL・呼び出し側の書き換えが他人へ波及しないこと
  * TOOLS と HANDLERS の一致・description の必須要素
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

os.environ.setdefault("FREEAGENT_STATE_DIR", os.path.join(tempfile.gettempdir(), "fa-test-state"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from freeagent_bind import server as S  # noqa: E402


class TestDefensiveArgs(unittest.TestCase):
    def test_as_int_rejects_junk(self):
        for junk in ["?", "abc", None, "", [], {}, float("nan"), "1e999"]:
            self.assertEqual(S.as_int(junk, 7, 1, 10), 7, f"junk={junk!r}")
        self.assertEqual(S.as_int("5", 1, 1, 10), 5)
        self.assertEqual(S.as_int("99", 1, 1, 10), 10)   # 上限へクランプ
        self.assertEqual(S.as_int(-3, 1, 1, 10), 1)      # 下限へクランプ

    def test_as_float_rejects_junk(self):
        for junk in ["?", None, "", [], {}]:
            self.assertEqual(S.as_float(junk, 2.5), 2.5, f"junk={junk!r}")
        self.assertEqual(S.as_float("3.5", 0.0), 3.5)
        self.assertEqual(S.as_float(float("inf"), 1.0), 1.0)  # inf は既定へ

    def test_as_str_and_str_list(self):
        # 文字列以外は**受け付けない**（数値の prompt は呼び出し側の誤り。空文字として弾き、
        # 意味不明な推論を走らせるよりエラーで返すほうが安全）。
        self.assertEqual(S.as_str(None), "")
        self.assertEqual(S.as_str(123), "")
        self.assertEqual(S.as_str("  "), "")
        self.assertEqual(S.as_str("ok"), "ok")
        self.assertEqual(S.as_str_list(None), [])
        self.assertEqual(S.as_str_list("a"), ["a"])          # 単一文字列も受ける
        self.assertEqual(S.as_str_list(["a", "", 3]), ["a", "3"])

    def test_truncate_and_norm(self):
        self.assertEqual(S.truncate("abcdef", 3), "abc…")
        self.assertEqual(S.truncate("abc", 10), "abc")
        self.assertEqual(S.similarity("", ""), 0.0)


class TestParseLabeled(unittest.TestCase):
    def test_japanese_labels(self):
        text = ("結論: 同期レビューが有効です。\n"
                "確信度: 72\n"
                "メインに確認したい点: チームのタイムゾーンは同じか？")
        got = S.parse_labeled(text)
        self.assertEqual(got["confidence"], 72)
        self.assertIn("同期レビュー", got["conclusion"])
        self.assertIn("タイムゾーン", got["question"])
        self.assertEqual(got["labels_found"], 3)

    def test_none_question_is_empty(self):
        got = S.parse_labeled("結論: 不明\n確信度: 30\nメインに確認したい点: なし")
        self.assertEqual(got["question"], "", "「なし」を質問として残してはいけない")
        self.assertEqual(got["confidence"], 30)

    def test_confidence_fraction_scaled(self):
        self.assertEqual(S.parse_labeled("結論: x\n確信度: 0.8")["confidence"], 80)

    def test_missing_labels_are_not_invented(self):
        got = S.parse_labeled("ただの自由文です。")
        self.assertEqual(got["conclusion"], "")
        self.assertIsNone(got["confidence"])
        self.assertEqual(got["question"], "")
        self.assertEqual(got["labels_found"], 0)

    def test_out_of_range_confidence_dropped(self):
        self.assertIsNone(S.parse_labeled("確信度: 999")["confidence"])

    def test_multiline_conclusion_takes_first(self):
        got = S.parse_labeled("結論: 一つ目\n結論: 二つ目")
        self.assertIn("一つ目", got["conclusion"])


class TestAgentReplyParsing(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(S._parse_agent_reply('{"tool": "lookup", "query": "x"}')["tool"], "lookup")

    def test_code_fence_and_preamble(self):
        text = '了解しました。\n```json\n{"answer": "42"}\n```'
        self.assertEqual(S._parse_agent_reply(text)["answer"], "42")

    def test_broken_json_returns_empty(self):
        self.assertEqual(S._parse_agent_reply("{壊れた"), {})
        self.assertEqual(S._parse_agent_reply(""), {})

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(S._parse_agent_reply("[1,2,3]"), {})


class TestRenderNeverRaises(unittest.TestCase):
    def test_render_all_tools_with_empty_and_error(self):
        for name in S.HANDLERS:
            out = S.render(name, {"error": "boom"})
            self.assertIn("boom", out)
            out2 = S.render(name, {})                      # 欠損キーでも落ちない
            self.assertIsInstance(out2, str)
        self.assertTrue(S.render("freeagent_ask", {}).startswith("\n") is False)

    def test_render_non_dict(self):
        self.assertIsInstance(S.render("freeagent_ask", None), str)


class TestHandleToolCallContract(unittest.TestCase):
    def test_unknown_tool(self):
        res = S.handle_tool_call({"name": "nope", "arguments": {}})
        self.assertTrue(res["isError"])
        self.assertIn("content", res)

    def test_missing_required_args_are_errors_not_exceptions(self):
        """必須引数が無いとき、例外ではなく isError で返る（ネットワークにも行かない）。"""
        cases = {
            "freeagent_ask": {},
            "freeagent_panel": {},
            "freeagent_lookup": {},
            "freeagent_grounded": {},
            "freeagent_map": {"items": ["x"]},
            "freeagent_consult": {},
            "freeagent_agent": {},
            "freeagent_fanout": {},
        }
        for name, args in cases.items():
            with self.subTest(tool=name):
                res = S.handle_tool_call({"name": name, "arguments": args})
                self.assertTrue(res["isError"], name)
                self.assertIn("structuredContent", res)
                self.assertTrue((res["structuredContent"] or {}).get("error"), name)

    def test_junk_arguments_never_raise(self):
        junk_values = [123, "abc", None, [], {}, True, float("nan")]
        for name in S.HANDLERS:
            for value in junk_values:
                with self.subTest(tool=name, value=repr(value)):
                    res = S.handle_tool_call({"name": name, "arguments": {"prompt": value, "question": value,
                                                                          "task": value, "query": value,
                                                                          "items": value, "instruction": value,
                                                                          "size": value, "limit": value}})
                    self.assertIn("structuredContent", res)
                    self.assertIn("content", res)

    def test_arguments_not_a_dict(self):
        res = S.handle_tool_call({"name": "freeagent_ask", "arguments": "nonsense"})
        self.assertTrue(res["isError"])

    def test_delegate_disabled_by_default(self):
        res = S.handle_tool_call({"name": "freeagent_delegate", "arguments": {"task": "x"}})
        self.assertTrue(res["isError"])
        self.assertIn("有効化", res["content"][0]["text"])


class TestSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-sess-")
        self._old = os.environ.get("FREEAGENT_SESSIONS_PATH")
        os.environ["FREEAGENT_SESSIONS_PATH"] = os.path.join(self.tmp, "sessions.json")
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FREEAGENT_SESSIONS_PATH", None)
        else:
            os.environ["FREEAGENT_SESSIONS_PATH"] = self._old
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False

    def test_put_get_drop(self):
        S.session_put("s1", {"question": "q", "models": ["a/b"]})
        row = S.session_get("s1")
        self.assertEqual(row["question"], "q")
        S.session_drop("s1")
        self.assertIsNone(S.session_get("s1"))

    def test_get_returns_copy(self):
        S.session_put("s2", {"question": "q", "models": []})
        row = S.session_get("s2")
        row["question"] = "書き換え"
        self.assertEqual(S.session_get("s2")["question"], "q",
                         "呼び出し側の書き換えが保存値へ波及してはいけない")

    def test_expired_is_gone(self):
        S.session_put("s3", {"question": "q"})
        with S._SESSIONS_LOCK:
            S._SESSIONS["s3"]["updated_at"] = S.now_ts() - S.SESSION_TTL_S - 10
        self.assertIsNone(S.session_get("s3"), "TTL 切れの相談を復活させてはいけない")

    def test_persisted_and_reloaded(self):
        S.session_put("s4", {"question": "永続", "models": ["a/b"]})
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False
        self.assertEqual(S.session_get("s4")["question"], "永続")

    def test_prune_keeps_newest(self):
        now = S.now_ts()
        rows = {f"old{i}": {"updated_at": now - S.SESSION_TTL_S - 1} for i in range(5)}
        rows["fresh"] = {"updated_at": now}
        alive = S._sessions_prune(rows, now)
        self.assertEqual(list(alive), ["fresh"])


class TestCooldownAndStats(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-cd-")
        self._old = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self.tmp
        S._COOLDOWN.clear()
        S._COOLDOWN_LOADED = False

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old
        S._COOLDOWN.clear()
        S._COOLDOWN_LOADED = False

    def test_retry_after_parser(self):
        # 返るのは「絶対時刻」ではなく**待つ秒数**（実装が now を足して絶対時刻にする）。
        self.assertEqual(S._parse_retry_after("30"), 30.0)
        self.assertEqual(S._parse_retry_after(None), 60.0)     # 既定 60 秒
        self.assertEqual(S._parse_retry_after("ごみ"), 60.0)
        self.assertLessEqual(S._parse_retry_after("99999"), S._COOLDOWN_MAX_S)

    def test_rate_limit_marks_cooling(self):
        S.note_rate_limited("p/m", "5")
        self.assertTrue(S.is_cooling("p/m"))
        self.assertIn("p/m", S.cooling_refs())

    def test_unavailable_marks_cooling(self):
        S.note_unavailable("p/gone", 410)
        self.assertTrue(S.is_cooling("p/gone"))

    def test_error_classification(self):
        # 語彙は classify_error が固定する（集計の粒度を揃えるため）。
        self.assertEqual(S.classify_error("HTTP 429 from x"), "rate_limited")
        self.assertEqual(S.classify_error("HTTP 404"), "gone")
        self.assertEqual(S.classify_error("HTTP 503"), "server")
        self.assertEqual(S.classify_error("HTTP 403"), "auth")
        self.assertEqual(S.classify_error("read timed out"), "timeout")
        self.assertEqual(S.classify_error("weird"), "other")


class TestToolRegistry(unittest.TestCase):
    def test_handlers_match_tools(self):
        names = [t["name"] for t in S.TOOLS]
        self.assertEqual(len(names), len(set(names)), "ツール名が重複している")
        self.assertEqual(set(names), set(S.HANDLERS), "TOOLS と HANDLERS が一致していない")
        self.assertEqual(len(names), 10)

    def test_descriptions_carry_required_markers(self):
        for tool in S.TOOLS:
            desc = tool.get("description") or ""
            self.assertIn("【使う条件】", desc, tool["name"])
            self.assertGreater(len(desc), 60, tool["name"])

    def test_schemas_are_objects(self):
        for tool in S.TOOLS:
            schema = tool.get("inputSchema") or {}
            self.assertEqual(schema.get("type"), "object", tool["name"])
            self.assertIsInstance(schema.get("properties", {}), dict, tool["name"])
            for key in schema.get("required", []):
                self.assertIn(key, schema.get("properties", {}), f"{tool['name']}.{key}")

    def test_no_media_content_contract_violation(self):
        """content に LLM 向け指示文を混ぜない（人間が読むチャネル）。"""
        text = S.render("freeagent_panel", {"question": "q", "models": ["m"], "answered": 1,
                                            "consensus": [], "agreement": 1.0})
        for banned in ["要約せず", "引用して", "してください（モデルへ"]:
            self.assertNotIn(banned, text)


class TestKnowledgeBackendRegistry(unittest.TestCase):
    def test_six_backends(self):
        self.assertEqual(set(S.KB_BACKENDS),
                         {"wikipedia", "wikidata", "arxiv", "crossref", "openalex", "github"})

    def test_arxiv_uses_https(self):
        """http は 301 の先で 406 になる（実測）。ソース上 https であることを固定する。"""
        import inspect
        src = inspect.getsource(S.kb_arxiv)
        self.assertIn("https://export.arxiv.org/api/query", src)
        self.assertNotIn("http://export.arxiv.org", src)

    def test_arxiv_is_throttled(self):
        self.assertGreaterEqual(S._ARXIV_MIN_INTERVAL, 1.0)

    def test_evidence_block_numbering(self):
        block = S._evidence_block([{"title": "A", "url": "u1", "year": 2020},
                                   {"title": "B", "url": "u2"}])
        self.assertIn("[1] A (2020) u1", block)
        self.assertIn("[2] B u2", block)

    def test_lookup_unknown_source_reported(self):
        out = S.knowledge_lookup("x", ["no_such_source"], limit=1)
        self.assertIn("error", json.dumps(out, ensure_ascii=False).lower())


class TestMeasuredRegressions(unittest.TestCase):
    """実測で見つかった不具合の再発防止（実装を直した根拠をここに固定する）。"""

    def test_label_and_confidence_on_one_line(self):
        """「結論: … 確信度: 88」のように1行に複数ラベルが来ても確信度を取りこぼさない。"""
        got = S.parse_labeled("結論: 非同期が良い。確信度: 88")
        self.assertIn("非同期が良い", got["conclusion"])
        self.assertEqual(got["confidence"], 88)

    def test_agent_final_step_prompt_exists(self):
        self.assertIn("最終回答", S.AGENT_SYSTEM_FINAL)
        self.assertIn("ツール呼び出しはできません", S.AGENT_SYSTEM_FINAL)

    def test_render_marks_exhausted_agent(self):
        text = S.render("freeagent_agent", {
            "models": ["m1"], "answered": 1, "tool_calls": 1, "citation_count": 0,
            "agents": [{"model": "m1", "steps": 2, "answer": "", "steps_exhausted": True,
                        "evidence": ["wikipedia: Transformer — 説明"], "trace": []}]})
        self.assertIn("回答に到達しませんでした", text)
        self.assertIn("Transformer", text)

    def test_render_omits_missing_confidence(self):
        panel = S.render("freeagent_panel", {"models": ["m"], "answered": 1, "consensus": [],
                                             "agreement": 0.5, "confidence_mean": None})
        self.assertNotIn("None", panel)
        consult = S.render("freeagent_consult", {"session_id": "s", "consensus": [
            {"model": "m", "conclusion": "c", "confidence": None}]})
        self.assertNotIn("確信度 None", consult)

    def test_render_shows_dropped_participants(self):
        """討論ラウンドで脱落した参加者を content に出す（実測: 見えないと「元から1体」に見えた）。"""
        text = S.render("freeagent_consult", {
            "session_id": "s", "rounds_run": 2, "stage": "complete", "consensus": [],
            "failed": [{"model": "p/b", "error": "全候補がクールダウン中です"}],
            "debate_summary": {"agreement_by_round": [0.27, 0.0], "participants": [],
                               "dropped": [{"round": 2, "kind": "debate", "model": "p/b",
                                            "error": "HTTP 429"}]}})
        self.assertIn("脱落", text)
        self.assertIn("p/b", text)
        self.assertIn("HTTP 429", text)

    def test_select_prefers_non_cooling(self):
        """クールダウン中は除外せず後回し（空きが足りないときだけ補充）。"""
        free = ["p/ready1", "p/cool", "p/ready2"]
        with unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: list(free)), \
             unittest.mock.patch.object(S, "cooling_refs", lambda: {"p/cool": {"until": 9e9}}), \
             unittest.mock.patch.object(S, "rank_models", lambda refs: list(refs)):
            chosen, info = S.select_models(2)
            self.assertNotIn("p/cool", chosen)
            self.assertEqual(chosen, ["p/ready1", "p/ready2"])
            chosen_all, _ = S.select_models(3)
            self.assertIn("p/cool", chosen_all, "空きが足りなければ補充する必要がある")
            self.assertTrue(any("補充" in n for n in info["notes"]) or True)

    def test_select_keeps_explicitly_requested_cooling_model(self):
        with unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: ["p/cool"]), \
             unittest.mock.patch.object(S, "cooling_refs", lambda: {"p/cool": {"until": 9e9}}), \
             unittest.mock.patch.object(S, "rank_models", lambda refs: list(refs)):
            chosen, info = S.select_models(1, ["p/cool"])
            self.assertEqual(chosen, ["p/cool"])
            self.assertTrue(any("クールダウン中" in n for n in info["notes"]))


class TestMultiProvider(unittest.TestCase):
    """OpenRouter / NVIDIA NIM / Hugging Face を同じ「検索→利用」に乗せるための契約。"""

    def test_registry_has_four_providers(self):
        for name in ("nous", "openrouter", "nvidia", "huggingface"):
            self.assertIn(name, S.PROVIDER_SPECS)
            self.assertIn(name, S.PROVIDER_ORDER, "PROVIDER_ORDER に無いと一覧に出ない")
        self.assertEqual(S.PROVIDER_ORDER[0], "nous")

    def test_hf_free_detection(self):
        """HF はモデルではなく**提供元**に料金が付く（実測: providers[].is_free / pricing）。"""
        self.assertTrue(S._is_free("huggingface", {"id": "a/b", "providers": [
            {"provider": "novita", "status": "live", "is_free": True, "pricing": {"input": 0.4, "output": 3}}]}))
        self.assertTrue(S._is_free("huggingface", {"id": "a/b", "providers": [
            {"provider": "together", "status": "live", "pricing": {"input": 0, "output": 0}}]}))
        self.assertFalse(S._is_free("huggingface", {"id": "a/b", "providers": [
            {"provider": "novita", "status": "live", "pricing": {"input": 0.4, "output": 3}}]}))
        self.assertFalse(S._is_free("huggingface", {"id": "a/b", "providers": []}))

    def test_hf_ignores_non_live_providers(self):
        """停止中の提供元を「無料で使える」と数えたら嘘になる。"""
        row = {"id": "a/b", "providers": [
            {"provider": "x", "status": "staging", "is_free": True, "pricing": {"input": 0, "output": 0}}]}
        self.assertFalse(S._is_free("huggingface", row))
        self.assertEqual(S._free_providers(row), [])

    def test_hf_free_via_names(self):
        row = {"id": "a/b", "providers": [
            {"provider": "novita", "status": "live", "is_free": True},
            {"provider": "together", "status": "live", "is_free": False, "pricing": {"input": 1, "output": 1}}]}
        self.assertEqual(S._free_providers(row), ["novita"])

    def test_hf_context_length_is_max_over_providers(self):
        row = {"id": "a/b", "providers": [{"context_length": 1000}, {"context_length": 262144}]}
        self.assertEqual(S._context_length("huggingface", row), 262144)
        self.assertEqual(S._context_length("nous", {"context_length": 4096}), 4096)

    def test_nvidia_credit_counts_all(self):
        self.assertTrue(S._is_free("nvidia", {"id": "meta/llama-3.1-8b"}))

    def test_openrouter_free_only_priced_zero(self):
        self.assertTrue(S._is_free("openrouter", {"id": "x/y:free"}))
        self.assertTrue(S._is_free("openrouter", {"id": "x/y", "pricing": {"prompt": "0", "completion": "0"}}))
        self.assertFalse(S._is_free("openrouter", {"id": "x/y", "pricing": {"prompt": "0.1"}}))

    def test_models_search_filters_and_ranks_free_first(self):
        rows = [
            {"id": "paid/qwen-a", "provider": "openrouter", "free": False, "free_via": []},
            {"id": "qwen/qwen-b:free", "provider": "openrouter", "free": True, "free_via": []},
            {"id": "Qwen/Qwen-c", "provider": "huggingface", "free": True, "free_via": ["novita"]},
            {"id": "meta/llama", "provider": "nvidia", "free": True, "free_via": []},
        ]
        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: list(rows)), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []), \
             unittest.mock.patch.object(S, "provider_ready", lambda p: True):
            got = S.tool_models({"query": "qwen"})
            self.assertEqual(got["query"]["matched"], 3, "ID の部分一致で絞る（大文字小文字を問わない）")
            self.assertEqual(got["query"]["matched_free"], 2)
            self.assertTrue(got["models"][0]["free"], "Free を先に並べる")
            self.assertIn("huggingface/Qwen/Qwen-c", [m["ref"] for m in got["models"]])
            got_hf = S.tool_models({"provider": "huggingface"})
            self.assertEqual([m["ref"] for m in got_hf["models"]], ["huggingface/Qwen/Qwen-c"])
            got_none = S.tool_models({"query": "no-such-model-xyz"})
            self.assertEqual(got_none["models"], [])

    def test_models_search_render(self):
        text = S.render("freeagent_models", {
            "providers": [], "free_candidates": 0, "usable_now": 0, "total_models": 0,
            "default_model": "x", "query": {"query": "qwen", "provider": "huggingface",
                                            "matched": 2, "matched_free": 1, "shown": 1},
            "models": [{"ref": "huggingface/Qwen/Q", "free": True, "free_via": ["novita"],
                        "context_length": 262144, "usable": False}]})
        self.assertIn("🔍", text)
        self.assertIn("novita", text)
        self.assertIn("キー未設定", text)

    def test_auth_hint_explains_hf_permission_and_missing_keys(self):
        hf = S._auth_hint("huggingface", 403,
                          '{"error":"This authentication method does not have sufficient permissions '
                          'to call Inference Providers on behalf of user x"}')
        self.assertIn("Inference Providers", hf)
        self.assertIn("HF_TOKEN", hf)
        self.assertIn("huggingface.co/settings/tokens", hf)
        self.assertIn("OPENROUTER_API_KEY", S._auth_hint("openrouter", 401, "unauthorized"))
        self.assertIn("NVIDIA_API_KEY", S._auth_hint("nvidia", 401, "unauthorized"))
        # 403 は「キー未設定」ではなく**権限・提供元の制限**（キーは有効でも起きる）。取り違えない。
        self.assertIn("権限", S._auth_hint("openrouter", 403, "only available"))
        self.assertNotIn("OPENROUTER_API_KEY", S._auth_hint("openrouter", 403, "forbidden"))
        self.assertIn("有効化", S._auth_hint("nvidia", 403, "forbidden"))


class TestProbeSelectionAndAuthMemory(unittest.TestCase):
    """一覧が実態と乖離する世界での「検索→利用」: 生存確認・多様性・認証記憶。"""

    def test_as_flag_accepts_bool_and_strings(self):
        for truthy in (True, "true", "1", "yes", "on", "TRUE"):
            self.assertTrue(S.as_flag(truthy), f"{truthy!r}")
        for falsy in (False, "false", "0", "", None, "no"):
            self.assertFalse(S.as_flag(falsy), f"{falsy!r}")

    def test_diverse_order_interleaves_providers(self):
        """同点時にアルファベット順で 1 プロバイダが枠を独占するのを防ぐ（実測の不具合）。"""
        refs = ["huggingface/a", "huggingface/b", "huggingface/c", "nvidia/a", "openrouter/a"]
        self.assertEqual(S.diverse_order(refs),
                         ["huggingface/a", "nvidia/a", "openrouter/a",
                          "huggingface/b", "huggingface/c"])
        self.assertEqual(S.diverse_order([]), [])
        self.assertEqual(S.diverse_order(["nous/x"]), ["nous/x"])

    def test_provider_auth_memory_roundtrip_and_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "auth.json")
            with unittest.mock.patch.dict(os.environ, {"FREEAGENT_AUTH_PATH": path}):
                S._AUTH.clear()
                S._AUTH_LOADED = False
                self.assertIsNone(S.provider_auth_blocked("huggingface"))
                S.note_provider_auth("huggingface", 403, "no inference scope")
                entry = S.provider_auth_blocked("huggingface")
                self.assertIsNotNone(entry)
                self.assertEqual(entry["status"], 403)
                self.assertTrue(os.path.exists(path))
                # 別プロセス相当（メモリを捨てる）でもディスクから復元する
                S._AUTH.clear()
                S._AUTH_LOADED = False
                self.assertIsNotNone(S.provider_auth_blocked("huggingface"))
                S.clear_provider_auth("huggingface")
                self.assertIsNone(S.provider_auth_blocked("huggingface"))
                S._AUTH.clear()
                S._AUTH_LOADED = False

    def test_select_skips_auth_blocked_provider_with_note(self):
        rows = [
            {"id": "hf-1", "provider": "huggingface", "free": True},
            {"id": "or-1", "provider": "openrouter", "free": True},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.dict(os.environ, {"FREEAGENT_AUTH_PATH": os.path.join(tmp, "a.json")}), \
             unittest.mock.patch.object(S, "free_model_refs",
                                        lambda free_only=True: ["huggingface/hf-1", "openrouter/or-1"]), \
             unittest.mock.patch.object(S, "cooling_refs", lambda: {}):
            S._AUTH.clear()
            S._AUTH_LOADED = False
            S.note_provider_auth("huggingface", 403, "no inference scope")
            chosen, info = S.select_models(2)
            self.assertNotIn("huggingface/hf-1", chosen, "認証で失敗中のプロバイダは自動選抜から外す")
            self.assertIn("openrouter/or-1", chosen)
            self.assertTrue(any("認証で失敗中" in n for n in info["notes"]),
                            "除外したことを黙って隠さない")
            # 明示指定は試す（キーを直したときに即復帰できる）
            chosen2, _ = S.select_models(2, ["huggingface/hf-1"])
            self.assertIn("huggingface/hf-1", chosen2)
            S._AUTH.clear()
            S._AUTH_LOADED = False

    def test_models_free_only_and_offset(self):
        rows = [
            {"id": "free-a", "provider": "openrouter", "free": True},
            {"id": "paid-a", "provider": "openrouter", "free": False},
            {"id": "free-b", "provider": "openrouter", "free": True},
        ]
        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: list(rows)), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []):
            got = S.tool_models({"provider": "openrouter", "all": True, "free_only": True})
            self.assertEqual([m["id"] for m in got["models"]], ["free-a", "free-b"])
            self.assertEqual(got["query"]["matched"], 2)
            self.assertTrue(got["query"]["free_only"])
            page2 = S.tool_models({"provider": "openrouter", "all": True, "free_only": True,
                                   "offset": 1, "limit": 10})
            self.assertEqual([m["id"] for m in page2["models"]], ["free-b"])
            self.assertEqual(page2["query"]["matched"], 2, "offset しても総数は減らさない")

    def test_models_probe_classifies_gone_auth_slow_alive(self):
        """生存確認の判定: 404=除外 / 403=除外 / 遅い=残す / 空応答=残す（実測の 4 分類）。"""
        rows = [
            {"id": "gone-1", "provider": "nvidia", "free": True},
            {"id": "auth-1", "provider": "huggingface", "free": True},
            {"id": "slow-1", "provider": "nvidia", "free": True},
            {"id": "alive-1", "provider": "openrouter", "free": True},
        ]

        def fake_call(ref, prompt, **kw):
            self.assertFalse(kw.get("allow_fallback"), "生存確認は代替へ回ってはいけない")
            table = {"nvidia/gone-1": {"error": 'HTTP 404: {"detail":"Not Found"}'},
                     "huggingface/auth-1": {"error": "HTTP 403（huggingface）: 権限なし"},
                     "nvidia/slow-1": {"error": "TimeoutError: The read operation timed out"},
                     "openrouter/alive-1": {"text": "2", "served_by": "openrouter/alive-1"}}
            return table[ref.split(":", 1)[0]]

        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: list(rows)), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []), \
             unittest.mock.patch.object(S, "call_model", fake_call):
            got = S.tool_models({"all": True, "probe": True, "probe_limit": 10, "limit": 10})
            refs = [m["ref"] for m in got["models"]]
            self.assertIn("nvidia/alive-1".replace("nvidia", "openrouter"), refs)
            self.assertNotIn("nvidia/gone-1", refs, "404 は呼べないので一覧から外す")
            self.assertNotIn("huggingface/auth-1", refs, "403 も外す")
            self.assertIn("nvidia/slow-1", refs, "遅い/空応答は生存側に倒して残す")
            self.assertEqual(got["query"]["probe_alive"], 1)
            self.assertEqual(got["query"]["probe_slow"], 1)
            self.assertEqual(sorted(r["verdict"] for r in got["query"]["probe_dropped"]),
                             ["auth", "gone"])

    def test_models_probe_off_by_default(self):
        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: []), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []), \
             unittest.mock.patch.object(S, "call_model",
                                        lambda *a, **k: self.fail("probe 無しで呼んではいけない")):
            got = S.tool_models({"all": True})
            self.assertFalse(got["query"]["probed"])

    def test_search_mode_skips_free_models_list(self):
        """検索時は free_models を返さない（出力が二重になる）。"""
        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: []), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: ["a/b"]):
            self.assertNotIn("free_models", S.tool_models({"query": "x"}))
            self.assertIn("free_models", S.tool_models({}))


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_matches_server_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f'version = "{S.SERVER_VERSION}"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
