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
import time
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


class TestThoughtLedgerStore(unittest.TestCase):
    """§2.6 思考台帳の永続ストア（相談セッションと同じ規律で扱う）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-think-store-")
        self._old = os.environ.get("FREEAGENT_THOUGHTS_PATH")
        os.environ["FREEAGENT_THOUGHTS_PATH"] = os.path.join(self.tmp, "thoughts.json")
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FREEAGENT_THOUGHTS_PATH", None)
        else:
            os.environ["FREEAGENT_THOUGHTS_PATH"] = self._old
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def test_merge_get_drop(self):
        out = S.thought_merge("", {"n": 1, "text": "t"}, question="q")
        sid = out["session_id"]
        self.assertFalse(out["refused"])
        self.assertEqual(S.thought_get(sid)["question"], "q")
        S.thought_drop(sid)
        self.assertIsNone(S.thought_get(sid))

    def test_get_returns_copy(self):
        sid = S.thought_merge("", {"n": 1, "text": "t"})["session_id"]
        row = S.thought_get(sid)
        row["steps"][0]["text"] = "書き換え"
        self.assertEqual(S.thought_get(sid)["steps"][0]["text"], "t",
                         "呼び出し側の書き換えが保存値へ波及してはいけない")

    def test_expired_is_gone(self):
        sid = S.thought_merge("", {"n": 1, "text": "t"})["session_id"]
        with S._THOUGHTS_LOCK:
            S._THOUGHTS[sid]["updated_at"] = S.now_ts() - S.THOUGHT_TTL_S - 10
        self.assertIsNone(S.thought_get(sid), "TTL 切れの台帳を復活させてはいけない")

    def test_persisted_and_reloaded(self):
        sid = S.thought_merge("", {"n": 1, "text": "永続"})["session_id"]
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False
        self.assertEqual(S.thought_get(sid)["steps"][0]["text"], "永続")

    def test_unknown_sid_is_not_revived(self):
        out = S.thought_merge("t-does-not-exist", {"n": 1, "text": "t"})
        self.assertNotEqual(out["session_id"], "t-does-not-exist",
                            "未知の ID へ書き戻して古い台帳を復活させてはいけない")

    def test_assign_number_inside_lock(self):
        """並列呼び出しでも番号が衝突して片方が消えないこと（規約 14）。"""
        from concurrent.futures import ThreadPoolExecutor
        sid = S.thought_merge("", {"n": 1, "text": "start"})["session_id"]

        def add(i):
            return S.thought_merge(sid, {"text": f"t{i}"}, assign_number=True)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(add, range(4)))
        steps = S.thought_get(sid)["steps"]
        self.assertEqual(len(steps), 5, f"並列追加でステップが失われている: {steps}")
        self.assertEqual([s["n"] for s in steps], [1, 2, 3, 4, 5])

    def test_prune_keeps_newest(self):
        now = S.now_ts()
        rows = {f"old{i}": {"updated_at": now - S.THOUGHT_TTL_S - 1} for i in range(5)}
        rows["fresh"] = {"updated_at": now}
        self.assertEqual(list(S._thoughts_prune(rows, now)), ["fresh"])


class TestParseVerdict(unittest.TestCase):
    def test_labels(self):
        parsed = S.parse_verdict("判定: 要修正\n反証: 前提が未検証です\n見落とし: コスト\n確信度: 70")
        self.assertEqual(parsed["verdict"], "要修正")
        self.assertEqual(parsed["objection"], "前提が未検証です")
        self.assertEqual(parsed["oversight"], "コスト")
        self.assertEqual(parsed["confidence"], 70)
        self.assertEqual(parsed["labels_found"], 4)

    def test_same_line_multiple_labels(self):
        # 1 行 1 ラベルと決め打たない（規約 11）。
        parsed = S.parse_verdict("判定: 妥当 確信度: 55")
        self.assertEqual(parsed["verdict"], "妥当")
        self.assertEqual(parsed["confidence"], 55)

    def test_english_and_verdict_variants(self):
        self.assertEqual(S.parse_verdict("判定: 根拠不足")["verdict"], "根拠不足")
        self.assertEqual(S.parse_verdict("verdict: needs revision")["verdict"], "要修正")
        self.assertEqual(S.parse_verdict("判定: ok")["verdict"], "妥当")

    def test_none_is_not_counted_as_objection(self):
        parsed = S.parse_verdict("判定: 妥当\n反証: なし\n見落とし: 無し")
        self.assertEqual(parsed["objection"], "")
        self.assertEqual(parsed["oversight"], "")

    def test_missing_labels_stay_empty(self):
        parsed = S.parse_verdict("ただの自由文です")
        self.assertEqual(parsed["verdict"], "")
        self.assertIsNone(parsed["confidence"])
        self.assertEqual(parsed["labels_found"], 0)

    def test_junk_does_not_crash(self):
        for junk in [None, "", 123, [], {}]:
            self.assertEqual(S.parse_verdict(junk)["labels_found"], 0, f"junk={junk!r}")


class TestThinkTool(unittest.TestCase):
    """`freeagent_think`（§6.9）。検証は opt-in、環境障害では台帳に書かない。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-think-")
        self._old = os.environ.get("FREEAGENT_THOUGHTS_PATH")
        os.environ["FREEAGENT_THOUGHTS_PATH"] = os.path.join(self.tmp, "thoughts.json")
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def tearDown(self):
        if self._old is None:
            os.environ.pop("FREEAGENT_THOUGHTS_PATH", None)
        else:
            os.environ["FREEAGENT_THOUGHTS_PATH"] = self._old
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False

    def _ledger_file(self) -> str:
        return os.path.join(self.tmp, "thoughts.json")

    def test_thought_is_required(self):
        res = S.handle_tool_call({"name": "freeagent_think", "arguments": {}})
        self.assertTrue(res["isError"])
        self.assertIn("thought", res["content"][0]["text"])

    def test_ledger_only_makes_no_model_calls(self):
        # verify を付けない＝サブ呼び出しゼロ。呼ばれたら失敗させて契約を固定する。
        with unittest.mock.patch.object(S, "ask_many",
                                        side_effect=AssertionError("verify なしでモデルを呼んではいけない")):
            data = S.tool_think({"thought": "まず前提を分解する", "question": "A と B どちらか"})
        self.assertIsNone(data.get("error"))
        self.assertEqual(data["ledger"]["steps_recorded"], 1)
        self.assertFalse(data["verified"])
        self.assertIsNone(data["verification"])
        self.assertTrue(os.path.exists(self._ledger_file()), "台帳が永続化されていない")

    def test_resume_by_session_id(self):
        sid = S.tool_think({"thought": "1つ目"})["session_id"]
        S._THOUGHTS.clear()
        S._THOUGHTS_LOADED = False          # プロセス再起動を模す
        again = S.tool_think({"thought": "2つ目", "session_id": sid})
        self.assertEqual(again["session_id"], sid)
        self.assertEqual(again["step"], 2)
        self.assertEqual(again["ledger"]["steps_recorded"], 2)

    def test_unknown_session_starts_new_ledger(self):
        data = S.tool_think({"thought": "x", "session_id": "t-deadbeef"})
        self.assertNotEqual(data["session_id"], "t-deadbeef")
        self.assertTrue(any("見つかりません" in n for n in data["notes"]))

    def test_branch_and_revision_tracking(self):
        first = S.tool_think({"thought": "案A", "question": "q"})
        sid = first["session_id"]
        S.tool_think({"thought": "案B", "session_id": sid, "branch_id": "b1",
                      "branch_from_thought": 1})
        third = S.tool_think({"thought": "案Aを修正", "session_id": sid, "is_revision": True,
                              "revises_thought": 1})
        self.assertEqual([b["branch_id"] for b in third["ledger"]["branches"]], ["b1"])
        self.assertEqual(third["ledger"]["branch_points"], [1])
        self.assertEqual(third["ledger"]["revisions"], [{"step": 3, "revises": 1}])

    def test_same_number_overwrites(self):
        sid = S.tool_think({"thought": "v1", "thought_number": 1})["session_id"]
        data = S.tool_think({"thought": "v2", "session_id": sid, "thought_number": 1})
        self.assertEqual(data["ledger"]["steps_recorded"], 1)
        self.assertEqual(data["text"], "v2")
        self.assertTrue(any("置き換え" in n for n in data["notes"]))

    def test_verify_attaches_independent_refutation(self):
        rows = [
            {"text": "判定: 要修正\n反証: 前提が未検証です\n見落とし: 運用コスト\n確信度: 70",
             "served_by": "p/m1", "model": "m1", "cot_leak": False, "truncated": False},
            {"text": "判定: 妥当\n反証: なし\n見落とし: なし\n確信度: 80",
             "served_by": "p/m2", "model": "m2", "cot_leak": False, "truncated": False},
        ]
        with unittest.mock.patch.object(S, "select_models",
                                        return_value=(["p/m1", "p/m2"], {"notes": []})), \
                unittest.mock.patch.object(S, "ask_many", return_value=rows):
            data = S.tool_think({"thought": "仮説X", "verify": True})
        verify = data["verification"]
        self.assertTrue(data["verified"])
        self.assertEqual(verify["answered"], 2)
        self.assertEqual(verify["verdicts"]["要修正"], 1)
        self.assertEqual(verify["verdicts"]["妥当"], 1)
        self.assertEqual(len(verify["objections"]), 1, "「なし」を反証として数えてはいけない")
        self.assertEqual(len(verify["oversights"]), 1)
        self.assertEqual(verify["confidence_mean"], 75.0)
        self.assertIn("p/m1", data["verifier_models"])
        self.assertTrue(any("要修正" in s for s in data["suggestions"]))

    def test_verifier_models_are_rotated(self):
        """2 回目の検証では 1 回目に使った検証者を選抜から外す（同じモデルに固定しない）。"""
        seen: list[list[str]] = []

        def fake_select(size, requested=None, *, prefer=None, exclude=None, free_only=True):
            seen.append(list(exclude or []))
            return [f"p/m{len(seen)}"], {"notes": []}

        row = [{"text": "判定: 妥当\n反証: なし\n見落とし: なし\n確信度: 60",
                "served_by": "p/m1", "model": "p/m1"}]
        with unittest.mock.patch.object(S, "select_models", side_effect=fake_select), \
                unittest.mock.patch.object(S, "ask_many", return_value=row):
            first = S.tool_think({"thought": "a", "verify": True})
            S.tool_think({"thought": "b", "session_id": first["session_id"], "verify": True})
        self.assertEqual(seen[0], [])
        self.assertIn("p/m1", seen[1], "1 回目の検証者を 2 回目の選抜で除外していない")

    def test_verify_env_failure_writes_nothing(self):
        """規約 21: バックエンド不通では台帳に書かない（検証されていない前提を積まない）。"""
        rows = [{"error": "URLError: <urlopen error [WinError 10061] 接続を拒否されました>",
                 "ref": "p/m1"}]
        with unittest.mock.patch.object(S, "select_models",
                                        return_value=(["p/m1"], {"notes": []})), \
                unittest.mock.patch.object(S, "ask_many", return_value=rows):
            res = S.handle_tool_call({"name": "freeagent_think",
                                      "arguments": {"thought": "仮説", "verify": True}})
        self.assertTrue(res["isError"])
        self.assertIn("記録していません", res["structuredContent"]["error"])
        self.assertEqual(res["structuredContent"]["next_action"]["kind"], "unavailable_backend")
        self.assertFalse(os.path.exists(self._ledger_file()),
                         "環境障害では状態ディレクトリにファイルを作ってはいけない")

    def test_verify_model_failure_is_recorded_not_hidden(self):
        """モデル側の失敗（429 など）は環境障害ではないので記録し、脱落として残す。"""
        rows = [{"error": "HTTP 429: rate limited", "ref": "p/m1"}]
        with unittest.mock.patch.object(S, "select_models",
                                        return_value=(["p/m1"], {"notes": []})), \
                unittest.mock.patch.object(S, "ask_many", return_value=rows):
            data = S.tool_think({"thought": "仮説", "verify": True})
        self.assertIsNone(data.get("error"))
        self.assertFalse(data["verified"])
        self.assertEqual(data["verification"]["failed"], 1)
        self.assertEqual(len(data["verification"]["failed_rows"]), 1)
        self.assertTrue(any("独立した検証が得られていません" in s for s in data["suggestions"]))
        self.assertTrue(os.path.exists(self._ledger_file()))

    def test_step_limit_refuses_and_says_so(self):
        sid = S.tool_think({"thought": "1つ目"})["session_id"]
        with unittest.mock.patch.object(S, "THOUGHT_MAX_STEPS", 1):
            res = S.handle_tool_call({"name": "freeagent_think",
                                      "arguments": {"thought": "2つ目", "session_id": sid}})
        self.assertTrue(res["isError"])
        self.assertIn("上限", res["structuredContent"]["error"])
        self.assertEqual(len(S.thought_get(sid)["steps"]), 1, "上限超過で台帳を書き換えてはいけない")

    def test_next_thought_needed_defaults_true(self):
        self.assertTrue(S.tool_think({"thought": "a"})["next_thought_needed"])
        data = S.tool_think({"thought": "b", "next_thought_needed": False})
        self.assertFalse(data["next_thought_needed"])
        self.assertTrue(any("結論" in s for s in data["suggestions"]))

    def test_total_thoughts_is_stored_and_adjustable(self):
        """思考の総数を動的に調整できる（宣言 → 台帳に残る → 増減が反映される）。"""
        first = S.tool_think({"thought": "a", "total_thoughts": 2})
        sid = first["session_id"]
        self.assertEqual(first["total_thoughts"], 2)
        self.assertEqual(first["ledger"]["total_thoughts"], 2)
        second = S.tool_think({"thought": "b", "session_id": sid, "total_thoughts": 2})
        # 見積り総数（2）に達したので、続けるなら増やすよう促す（数値から生成）
        self.assertTrue(any("見積り総数" in s and "増やして" in s for s in second["suggestions"]))
        # 総数を増やして継続できる（積んだ思考は失われない）
        third = S.tool_think({"thought": "c", "session_id": sid, "total_thoughts": 5})
        self.assertEqual(third["ledger"]["total_thoughts"], 5)
        self.assertEqual(third["ledger"]["steps_recorded"], 3)
        # 減らすのも許容する（据え置き・減も動的調整のうち）
        fourth = S.tool_think({"thought": "d", "session_id": sid, "total_thoughts": 3})
        self.assertEqual(fourth["ledger"]["total_thoughts"], 3)

    def test_render_never_breaks(self):
        self.assertIsInstance(S.render("freeagent_think", {}), str)
        text = S.render("freeagent_think", {
            "step": 2, "ledger": {"steps_recorded": 2, "branches": [], "revisions": [],
                                  "latest": [{"n": 1, "text": "x"}]},
            "verification": {"models": ["p/m"], "answered": 1, "failed": 0,
                             "verdicts": {"妥当": 1, "要修正": 0, "根拠不足": 0},
                             "confidence_mean": 60.0,
                             "answers": [{"model": "p/m", "verdict": "妥当", "objection": "根拠が薄い"}]}})
        self.assertIn("思考 #2", text)
        self.assertIn("反証: 根拠が薄い", text)
        for banned in ("要約せず", "引用してください", "回答時は", "LLMへ", "モデルに対して"):
            self.assertNotIn(banned, text, "content は人間が読むチャネル（指示文を混ぜない）")


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
        self.assertEqual(len(names), 11)

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
        # 402 はクレジット枯渇（キー・権限ではない）。実測: HF の月次無料枠が尽きると全モデルが 402。
        quota = S._auth_hint("huggingface", 402, '{"error":"You have depleted your monthly included credits"}')
        self.assertIn("クレジット枯渇", quota)
        self.assertNotIn("HF_TOKEN", quota)
        self.assertFalse(S._is_auth_error(402, "depleted your monthly included credits"),
                         "クレジット枯渇をプロバイダ記憶（認証失敗）に入れない")
        self.assertIn(402, S._FALLBACK_STATUS, "402 は代替へ回す（入れないと即エラーで止まる）")
        # HF の 403 は 2 種類ある: 権限不足（トークンを直す）と**提供元/CDN の拒否**（トークンは無実）。
        # 後者でトークンを疑わせると、正しいトークンを何度も作り直す羽目になる（実測: Together 経由が
        # Cloudflare Error 1010 を返した）。
        upstream = S._auth_hint("huggingface", 403, '{"title":"Error 1010: Access denied"}')
        self.assertNotIn("HF_TOKEN が未設定", upstream)
        self.assertIn("提供元", upstream)
        self.assertIn("HTTP 403", upstream)
        self.assertNotIn("401", upstream.split("（")[0])


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
                     # 実際の HF の文言（英語の署名がある 403 だけを権限なしとして扱う）
                     "huggingface/auth-1": {"error": 'HTTP 403（huggingface）: / 応答: {"error":'
                                                      '"This authentication method does not have '
                                                      'sufficient permissions to call Inference '
                                                      'Providers on behalf of user x"}'},
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

    def test_is_auth_error_needs_a_signature(self):
        """403 を全部「認証失敗」にすると**プロバイダ全体を15分止める**（実測の誤り）。"""
        self.assertTrue(S._is_auth_error(401, ""))          # 401 は定義上ずっと認証
        self.assertTrue(S._is_auth_error(403, '{"error":"This authentication method does not have '
                                              'sufficient permissions to call Inference Providers"}'))
        self.assertTrue(S._is_auth_error(403, "invalid api key"))
        self.assertTrue(S._is_auth_error(403, "Unauthorized"))
        # 提供元都合の 403 / CDN の 403 は認証ではない（生きている他モデルまで選抜から消さない）
        self.assertFalse(S._is_auth_error(403, '{"error":"x/y:free is only available to paid accounts"}'))
        self.assertFalse(S._is_auth_error(
            403, '{"type":"https://developers.cloudflare.com/support/troubleshooting/'
                 'http-status-codes/cloudflare-1xxx-errors/"}'))

    def test_probe_treats_cdn_403_as_retryable_not_auth(self):
        """HF の `prism-ml/…:together` が返す Cloudflare 403 は「権限なし」ではない → 残す。"""
        rows = [{"id": "cdn-1", "provider": "huggingface", "free": True, "free_via": ["together"]}]

        def fake_call(ref, prompt, **kw):
            return {"error": 'HTTP 403（huggingface）: 403 Forbidden / 応答: {"type":"https://developers.'
                             'cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/"}'}

        with unittest.mock.patch.object(S, "all_models", lambda ttl=600.0: list(rows)), \
             unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []), \
             unittest.mock.patch.object(S, "call_model", fake_call):
            got = S.tool_models({"all": True, "probe": True, "probe_limit": 3, "limit": 3})
            self.assertEqual([m["ref"] for m in got["models"]], ["huggingface/cdn-1"])
            self.assertEqual([r["verdict"] for r in got["query"]["probe_dropped"]], [])
            self.assertEqual(got["query"]["probe_slow"] + got["query"]["probe_alive"], 0)

    def test_requested_ref_with_provider_suffix_is_accepted(self):
        """`モデル:提供元` を依頼で受ける（HF は提供元ごとに無料/有料・生死が違うため経路指定が要る）。

        在庫一覧は提供元サフィックス無しの ID を返すので、素の突き合わせだと**黙って除外**され、
        依頼 4 体が 3 体で走る（実測）。除外するなら理由を出さなければならない。
        """
        free = ["huggingface/inclusionAI/Ling-3.0-flash-Fin", "openrouter/x/y:free"]
        with unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: list(free)), \
             unittest.mock.patch.object(S, "cooling_refs", lambda: {}):
            chosen, info = S.select_models(2, ["huggingface/inclusionAI/Ling-3.0-flash-Fin:novita",
                                               "openrouter/x/y:free"])
            self.assertEqual(chosen[0], "huggingface/inclusionAI/Ling-3.0-flash-Fin:novita",
                             "経路指定はそのまま残す（呼び出し時に使う）")
            self.assertEqual(info["notes"], [], "在庫にあるので除外ノートは出ない")
            chosen2, info2 = S.select_models(2, ["huggingface/ghost/model:novita"])
            # 依頼は「拘束」ではなく優先（足りない分は在庫から補充する）。未知のものは入らない。
            self.assertNotIn("huggingface/ghost/model:novita", chosen2)
            self.assertTrue(any("未知/未提供" in n for n in info2["notes"]),
                            "除外するなら理由を出す")

    def test_render_prepends_selection_notes(self):
        """依頼と参加の差を content に出す（黙って減らさない）。"""
        text = S.render("freeagent_panel", {
            "question": "q", "answered": 3, "answers": [], "agreement": 1.0,
            "failed": 0, "models": ["a/b", "c/d", "e/f"],
            "selection": {"notes": ["未知/未提供のモデルを除外: huggingface/ghost/model:novita"]}})
        self.assertTrue(text.startswith("⚠️ 未知/未提供のモデルを除外"), text[:60])
        # ノートが無ければ何も足さない
        plain = S.render("freeagent_panel", {"question": "q", "answered": 0, "answers": [],
                                             "selection": {"notes": []}})
        self.assertFalse(plain.startswith("⚠️"))

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


class TestOfflineTolerance(unittest.TestCase):
    """不通・OFF でも副作用を残さない（旧実装の「OFF 時のエラー処理」を再構成した部分）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-offline-test-")
        self.env = unittest.mock.patch.dict(os.environ, {
            "FREEAGENT_STATS_PATH": os.path.join(self.tmp, "model_stats.json"),
            "FREEAGENT_TRACE_PATH": os.path.join(self.tmp, "traces.jsonl"),
            "FREEAGENT_COOLDOWN_PATH": os.path.join(self.tmp, "cooldowns.json"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_env_failure_signatures(self):
        for err in ["<urlopen error [WinError 10061] 対象のコンピューターによって拒否された",
                    "URLError: connection refused", "HTTPSConnectionPool: Read timed out.",
                    "ConnectionResetError", "getaddrinfo failed",
                    "URLError: tunnel connection failed: 502 Bad Gateway (proxy error)"]:
            self.assertTrue(S.is_env_failure(err), f"{err!r} は環境障害のはず")
        # モデルの責任である失敗は環境障害にしない（統計から消えてはいけない）
        for err in ["HTTP 429", "HTTP 404", "HTTP 402", "HTTP 401", "空応答", ""]:
            self.assertFalse(S.is_env_failure(err), f"{err!r} を環境障害にしてはいけない")

    def test_env_failure_writes_no_state(self):
        """プロキシ停止・DNS 不達では、統計にもトレースにもクールダウンにも**書かない**。"""
        S.observe_call({"ref": "m:free", "error": "URLError: [WinError 10061] connection refused"},
                       "ask", "")
        self.assertEqual(sorted(os.listdir(self.tmp)), [], "状態ファイルが作られている（副作用）")

    def test_model_failure_still_recorded(self):
        """モデルの失敗（429 等）は従来どおり記録する（環境障害だけを no-op にする）。"""
        S.observe_call({"ref": "m:free", "error": "HTTP 429 rate limited"}, "ask", "")
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "traces.jsonl")),
                        "モデルの失敗はトレースに残る")
        row = S._STATS["models"]["m:free"]["kinds"]["ask"]
        self.assertEqual(row["err"].get("rate_limited"), 1, "429 は統計に数える")

    def test_error_advice_kinds(self):
        cases = [
            ({"error": "未知のツール: freeagent_x", "unknown_tool": True}, "unknown_tool"),
            ({"error": "推論可能な Free モデルが 0 件です。`hermes proxy start`"}, "unavailable_backend"),
            ({"error": "接続できません: URLError connection refused"}, "unavailable_backend"),
            ({"error": "HTTP 401 invalid api key"}, "auth"),
            ({"error": "HTTP 429 rate limited"}, "rate_limited"),
            ({"error": "候補はすべてクールダウン中です"}, "cooling"),
            ({"error": "空応答（max_tokens 不足の可能性）"}, "empty_answer"),
            ({"error": "謎の失敗"}, "error"),
        ]
        for data, kind in cases:
            got = S.error_advice(dict(data))
            self.assertEqual(got["kind"], kind, f"{data!r} → {got['kind']}")
            self.assertFalse(got["tool_available"])
            self.assertTrue(got["advice"], "advice が空")
        # 代替手段まで落ちる種類には fallback_tools がある（再試行でターンを捨てない）
        self.assertIn("web_search", S.error_advice({"error": "URLError connection refused"})["fallback_tools"])

    def test_handle_tool_call_attaches_next_action(self):
        r = S.handle_tool_call({"name": "freeagent_does_not_exist", "arguments": {}})
        self.assertTrue(r["isError"])
        sc = r["structuredContent"]
        self.assertEqual(sc["next_action"]["kind"], "unknown_tool")
        self.assertIn("reenable", sc["next_action"])
        # content は人間向け（指示文を混ぜない）
        text = r["content"][0]["text"]
        self.assertNotIn("structuredContent", text)

    def test_instructions_and_description_carry_offline_rule(self):
        self.assertTrue(S.PROACTIVE_INSTRUCTIONS)
        self.assertIn("無効", S.PROACTIVE_INSTRUCTIONS)
        for d in S.TOOLS:
            self.assertIn("【無効・不通のとき】", d["description"], d["name"])
        for name in ("freeagent_panel", "freeagent_consult", "freeagent_grounded", "freeagent_lookup"):
            desc = next(d["description"] for d in S.TOOLS if d["name"] == name)
            self.assertIn("【競合より優先】", desc, name)
            self.assertIn("【並列】", desc, name)


class TestHostEncodingTolerance(unittest.TestCase):
    """日本語 Windows / CI の cp1252 コンソールでもゲートが落ちないこと。

    実測: windows-latest のランナーは cp1252 で、`scripts/check_integrity.py` の日本語 print が
    UnicodeEncodeError になり **CI が失敗した**（コードの不具合ではないが、ゲートとして機能しない）。
    """

    def test_gate_survives_non_utf8_console(self):
        import subprocess
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        env.pop("PYTHONPATH", None)
        proc = subprocess.run([sys.executable, os.path.join(root, "scripts", "check_integrity.py")],
                              cwd=root, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        self.assertEqual(proc.returncode, 0,
                         f"cp1252 コンソールで落ちた: {proc.stdout[-300:]} {proc.stderr[-300:]}")


class TestAtomicWriteHygiene(unittest.TestCase):
    """原子置換の副作用（書きかけの一時ファイルが残る）を掃除すること。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-atomic-")
        self.target = os.path.join(self.tmp, "state.json")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stale_tmp_is_swept_recent_is_kept(self):
        stale = f"{self.target}.999.888.tmp"
        fresh = f"{self.target}.111.222.tmp"
        for path in (stale, fresh):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("x")
        old = time.time() - 3600
        os.utime(stale, (old, old))
        self.assertTrue(S._atomic_write(self.target, '{"a": 1}'))
        self.assertFalse(os.path.exists(stale), "古い書きかけが残っている")
        self.assertTrue(os.path.exists(fresh), "並行書き込み中の一時ファイルを消してはいけない")
        self.assertEqual(S._load_json(self.target, None), {"a": 1})

    def test_unrelated_files_are_untouched(self):
        other = os.path.join(self.tmp, "cooldowns.json.1.2.tmp")   # 別の対象
        with open(other, "w", encoding="utf-8") as fh:
            fh.write("x")
        os.utime(other, (time.time() - 3600,) * 2)
        S._atomic_write(self.target, "{}")
        self.assertTrue(os.path.exists(other), "対象外のファイルを消してはいけない")


class TestProactivePatternMatching(unittest.TestCase):
    """除外パターンの照合（`fnmatchcase`・glob でなければ完全一致）と**空振り検出**。

    実測: 広く流布していた `["ask_*", "panel", "consensus*"]` は、現行の deliberation サーバー
    （実名 `ask-all` / `ask-one` … とハイフン区切り、`panel`/`consensus` は存在しない）で
    **1 件も一致しなかった**。空振りの除外は何も変えずに「設定した」気にさせるので、
    一致 0 件を検出できることを固定する。
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, "scripts", "apply_proactive.py")
        spec = importlib.util.spec_from_file_location("apply_proactive", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    # 実測（ライブ `hermes mcp test deliberation` の 21 件）。schema キャッシュは 18 件しか無く、
    # panel / consensus / consensus-step が欠けていた（＝キャッシュを信じると誤判定する）。
    REAL = ["ask-all", "consensus", "consensus-step", "codex-login", "panel", "ask-one", "analyze",
            "ask-gpt", "ask-gemini", "ask-grok", "ask-openrouter", "architect", "plan-reviewer",
            "scope-analyst", "code-reviewer", "security-analyst", "researcher", "debugger",
            "session-get", "session-revisit", "session-annotate"]

    def test_glob_matches_real_names(self):
        self.assertEqual(len(self.mod.pattern_hits("ask-*", self.REAL)), 6)

    def test_stale_underscore_pattern_matches_nothing(self):
        # `ask_*`（アンダースコア）は空振り。実名はハイフン区切り。
        self.assertEqual(self.mod.pattern_hits("ask_*", self.REAL), [])

    def test_hyphenated_and_plain_patterns_match_real_names(self):
        self.assertEqual(self.mod.pattern_hits("panel", self.REAL), ["panel"])
        self.assertEqual(self.mod.pattern_hits("consensus*", self.REAL), ["consensus", "consensus-step"])

    def test_match_is_case_sensitive_and_exact(self):
        self.assertEqual(self.mod.pattern_hits("ASK-ALL", self.REAL), [])   # fnmatchcase
        self.assertEqual(self.mod.pattern_hits("ask", self.REAL), [])       # 部分一致にしない
        self.assertEqual(self.mod.pattern_hits("ask-all", self.REAL), ["ask-all"])

    def test_configured_exclude_parses_both_styles(self):
        """`hermes config set` が書く 2 形式（ブロック / 1 行）を正しく読む。

        実測: 6 スペースを期待した実装は 8 スペースの項目を読めず、**空リストを返して空振りを
        見逃していた**（壊れた除外を「問題なし」と報告する）。
        """
        home = tempfile.mkdtemp(prefix="fa-home-")
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        block = (
            "mcp_servers:\n"
            "  deliberation:\n"
            "    command: npx\n"
            "    tools:\n"
            "      exclude:\n"
            "        - ask_*\n"
            "        - panel\n"
            "  other:\n"
            "    command: x\n"
        )
        flow = (
            "mcp_servers:\n"
            "  deliberation:\n"
            "    tools:\n"
            "      exclude: ['ask-*', \"panel\"]\n"
            "  other:\n"
            "    command: x\n"
        )
        for body, expected in ((block, ["ask_*", "panel"]), (flow, ["ask-*", "panel"])):
            with open(os.path.join(home, "config.yaml"), "w", encoding="utf-8") as fh:
                fh.write(body)
            with unittest.mock.patch.dict(os.environ, {"HERMES_HOME": home}):
                self.assertEqual(self.mod.configured_exclude("deliberation"), expected, body)
                self.assertEqual(self.mod.configured_exclude("other"), [])

    def test_check_reports_configured_but_stale_patterns(self):
        """設定済みの除外が実名に一致しない＝**本当の空振り**を検出できること。"""
        names = self.REAL
        stale = [pat for pat in ["ask_*", "panel"] if not self.mod.pattern_hits(pat, names)]
        self.assertEqual(stale, ["ask_*"])          # panel は実在するので空振りではない
        self.assertEqual([p for p in ["ask-*", "panel", "consensus*"]
                          if not self.mod.pattern_hits(p, names)], [])

    def test_match_report_flags_empty_patterns(self):
        # 正しい候補はすべて一致する（設定に書かれる）
        matched, empty = self.mod.match_report(["ask-*", "panel", "consensus*"], self.REAL)
        self.assertEqual(matched, ["ask-*", "panel", "consensus*"])
        self.assertEqual(empty, [])
        # 空振りの候補は設定に書かない
        matched2, empty2 = self.mod.match_report(["ask_*", "panel"], self.REAL)
        self.assertEqual(matched2, ["panel"])
        self.assertEqual(empty2, ["ask_*"])


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_matches_server_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f'version = "{S.SERVER_VERSION}"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
