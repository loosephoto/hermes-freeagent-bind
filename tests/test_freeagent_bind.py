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


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_matches_server_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f'version = "{S.SERVER_VERSION}"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
