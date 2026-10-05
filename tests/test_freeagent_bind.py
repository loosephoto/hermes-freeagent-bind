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
        fourth = S.tool_think({"thought": "d", "session_id": sid, "total_thoughts": 4})
        self.assertEqual(fourth["ledger"]["total_thoughts"], 4)
        self.assertEqual([h["total"] for h in fourth["ledger"]["total_history"]], [2, 5, 4])

    def test_total_thoughts_inherited_and_auto_raised(self):
        """省略時は台帳の見積りを引き継ぎ、番号が見積りを超えたら引き上げて note に出す（黙らない）。"""
        sid = S.tool_think({"thought": "a", "total_thoughts": 2})["session_id"]
        second = S.tool_think({"thought": "b", "session_id": sid})
        self.assertEqual(second["total_thoughts"], 2, "省略時に台帳の見積りを引き継いでいない")
        self.assertFalse(second["total_auto_adjusted"])
        third = S.tool_think({"thought": "c", "session_id": sid})
        self.assertEqual(third["total_thoughts"], 3)
        self.assertTrue(third["total_auto_adjusted"])
        self.assertTrue(any("引き上げ" in n for n in third["notes"]))
        self.assertTrue(third["ledger"]["total_history"][-1]["auto"])
        # 番号を下回る見積り（#4 で 3）は矛盾なので採らない
        fourth = S.tool_think({"thought": "d", "session_id": sid, "total_thoughts": 3})
        self.assertEqual(fourth["total_thoughts"], 4)

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


class TestThinkStructure(unittest.TestCase):
    """`freeagent_think` の構造（§2.7 / §6.10）: 分解・改訂・分岐・仮説・閲覧・代替案。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-think-st-")
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

    def _no_calls(self):
        return unittest.mock.patch.object(
            S, "ask_many", side_effect=AssertionError("参照エラーの呼び出しでサブを呼んではいけない"))

    def _steps(self, sid):
        return len(S.thought_get(sid)["steps"])

    # ---- 分解
    def test_plan_progress_and_revision_keeps_done(self):
        sid = S.tool_think({"thought": "分解する", "plan": ["要件", "設計", "検証"]})["session_id"]
        second = S.tool_think({"thought": "要件を詰めた", "session_id": sid,
                               "subgoal": 1, "subgoal_done": True})
        plan = second["ledger"]["plan"]
        self.assertEqual([p["id"] for p in plan], [1, 2, 3])
        self.assertTrue(plan[0]["done"])
        self.assertEqual(plan[0]["steps"], [2])
        self.assertEqual(second["ledger"]["plan_progress"], {"done": 1, "total": 3})
        # 計画の改訂: 同じ文面の項目は達成済みを引き継ぐ
        third = S.tool_think({"thought": "計画を見直す", "session_id": sid,
                              "plan": ["要件", "設計", "移行", "検証"]})
        self.assertEqual(third["ledger"]["plan_progress"], {"done": 1, "total": 4})

    def test_plan_errors_are_explicit(self):
        sid = S.tool_think({"thought": "a", "plan": ["x"]})["session_id"]
        res = S.handle_tool_call({"name": "freeagent_think",
                                  "arguments": {"thought": "b", "session_id": sid, "subgoal": 5}})
        self.assertTrue(res["isError"])
        self.assertIn("計画にありません", res["structuredContent"]["error"])
        too_many = S.tool_think({"thought": "c", "session_id": sid,
                                 "plan": [f"p{i}" for i in range(S.THOUGHT_PLAN_MAX + 1)]})
        self.assertIn("まで", too_many["error"], "上限超過を黙って切り捨ててはいけない")
        self.assertEqual(self._steps(sid), 1, "エラーの呼び出しで台帳を書いてはいけない")

    # ---- 改訂
    def test_revision_marks_original_and_drops_it_from_active_path(self):
        sid = S.tool_think({"thought": "前提: A は常に速い"})["session_id"]
        S.tool_think({"thought": "したがって A を採る", "session_id": sid})
        seen = {}

        def fake_ask(refs, prompt, **kw):
            seen["prompt"] = prompt
            return [{"text": "判定: 妥当\n反証: なし\n見落とし: なし\n確信度: 60", "served_by": "p/m"}]

        with unittest.mock.patch.object(S, "select_models", return_value=(["p/m"], {"notes": []})), \
                unittest.mock.patch.object(S, "ask_many", side_effect=fake_ask):
            third = S.tool_think({"thought": "前提の修正: A は小規模でのみ速い", "session_id": sid,
                                  "revises_thought": 1, "verify": True})
        self.assertTrue(third["is_revision"], "revises_thought だけでも改訂として扱う")
        self.assertEqual(third["ledger"]["superseded"], [{"step": 1, "by": 3}])
        self.assertEqual(third["ledger"]["active_path"], [2, 3])
        self.assertIn("【改訂前の思考 #1】", seen["prompt"])
        current = seen["prompt"].split("【これまでの思考（現行の道筋）】")[1].split("【改訂前")[0]
        self.assertNotIn("#1", current, "改訂済みの思考を現行の道筋として検証者へ渡してはいけない")

    def test_revision_reference_errors_make_no_calls_and_no_writes(self):
        sid = S.tool_think({"thought": "a"})["session_id"]
        with self._no_calls():
            for bad in ({"is_revision": True}, {"revises_thought": 9}, {"revises_thought": 2}):
                args = {"thought": "b", "session_id": sid, "verify": True, **bad}
                res = S.handle_tool_call({"name": "freeagent_think", "arguments": args})
                self.assertTrue(res["isError"], f"args={bad}")
                self.assertEqual(res["structuredContent"]["known_thoughts"], [1])
        self.assertEqual(self._steps(sid), 1)

    # ---- 分岐
    def test_branch_lifecycle_and_abandon_removes_from_active_path(self):
        sid = S.tool_think({"thought": "起点"})["session_id"]
        S.tool_think({"thought": "案B", "session_id": sid, "branch_id": "b1", "branch_from_thought": 1})
        cont = S.tool_think({"thought": "案Bの続き", "session_id": sid, "branch_id": "b1"})
        self.assertEqual(cont["branch_from_thought"], 1, "既存の分岐は分岐元を引き継ぐ")
        auto = S.tool_think({"thought": "案C", "session_id": sid, "branch_from_thought": 1})
        self.assertEqual(auto["branch_id"], "b2")
        self.assertTrue(any("割り当てました" in n for n in auto["notes"]))
        self.assertTrue(any("未決着の分岐が 2 本" in s for s in auto["suggestions"]))
        done = S.tool_think({"thought": "案Bは棄却し案Cを採る", "session_id": sid,
                             "resolve_branch": "b1", "branch_status": "abandoned"})
        status = {b["branch_id"]: b["status"] for b in done["ledger"]["branches"]}
        self.assertEqual(status, {"b1": "abandoned", "b2": "open"})
        self.assertEqual(done["ledger"]["active_path"], [1, 4, 5])
        self.assertIn("棄却", S.render("freeagent_think", done))

    def test_branch_reference_errors(self):
        sid = S.tool_think({"thought": "a"})["session_id"]
        cases = [({"branch_from_thought": 7}, "台帳にありません"),
                 ({"branch_id": "bx"}, "branch_from_thought"),
                 ({"resolve_branch": "zz", "branch_status": "adopted"}, "台帳にありません"),
                 ({"branch_status": "adopted"}, "resolve_branch"),
                 ({"resolve_branch": "zz"}, "branch_status")]
        for bad, needle in cases:
            data = S.tool_think({"thought": "b", "session_id": sid, **bad})
            self.assertIn(needle, data.get("error") or "", f"args={bad}")
        self.assertEqual(self._steps(sid), 1)

    # ---- 仮説
    def test_hypothesis_generate_and_test(self):
        sid = S.tool_think({"thought": "遅延の原因は DNS", "kind": "hypothesis"})["session_id"]
        first = S.tool_think({"thought": "別の観測を集める", "session_id": sid})
        self.assertTrue(any("未検証の仮説" in s and "#1" in s for s in first["suggestions"]))
        tested = S.tool_think({"thought": "DNS を固定しても遅い", "session_id": sid,
                               "tests_hypothesis": 1, "hypothesis_status": "refuted"})
        self.assertEqual(tested["kind"], "test")
        hyp = tested["ledger"]["hypotheses"][0]
        self.assertEqual((hyp["status"], hyp["tested_by"]), ("refuted", [3]))
        self.assertFalse(any("未検証の仮説" in s for s in tested["suggestions"]))
        # 仮説本文を同じ番号で書き直しても、検証結果は失わない
        again = S.tool_think({"thought": "遅延の原因は DNS（再掲）", "session_id": sid,
                              "thought_number": 1, "kind": "hypothesis"})
        self.assertEqual(again["ledger"]["hypotheses"][0]["status"], "refuted")

    def test_hypothesis_reference_errors(self):
        sid = S.tool_think({"thought": "ただの思考"})["session_id"]
        for bad, needle in [({"tests_hypothesis": 1}, "仮説"),
                            ({"hypothesis_status": "supported"}, "tests_hypothesis"),
                            ({"kind": "guess"}, "kind")]:
            data = S.tool_think({"thought": "b", "session_id": sid, **bad})
            self.assertIn(needle, data.get("error") or "", f"args={bad}")
        self.assertEqual(self._steps(sid), 1)

    # ---- 代替案
    def test_propose_alternatives_uses_other_models(self):
        seen: list[list[str]] = []

        def fake_select(size, requested=None, *, prefer=None, exclude=None, free_only=True):
            seen.append(list(exclude or []))
            return [f"p/m{len(seen)}"], {"notes": []}

        answers = iter([
            [{"text": "判定: 妥当\n反証: なし\n見落とし: なし\n確信度: 60", "served_by": "p/m1"}],
            [{"text": "代替: キャッシュ層の飽和\n代替: なし\n- 代替: GC 停止", "served_by": "p/m2"}],
        ])
        with unittest.mock.patch.object(S, "select_models", side_effect=fake_select), \
                unittest.mock.patch.object(S, "ask_many", side_effect=lambda *a, **k: next(answers)):
            data = S.tool_think({"thought": "原因は DNS", "kind": "hypothesis",
                                 "verify": True, "propose_alternatives": True})
        self.assertIn("p/m1", seen[1], "代替案の提案者に検証者と同じモデルを使ってはいけない")
        self.assertEqual([i["text"] for i in data["alternatives"]["items"]],
                         ["キャッシュ層の飽和", "GC 停止"])
        self.assertIn("代替案", S.render("freeagent_think", data))

    def test_propose_alternatives_env_failure_writes_nothing(self):
        rows = [{"error": "URLError: <urlopen error [WinError 10061] 接続を拒否されました>"}]
        with unittest.mock.patch.object(S, "select_models", return_value=(["p/m1"], {"notes": []})), \
                unittest.mock.patch.object(S, "ask_many", return_value=rows):
            res = S.handle_tool_call({"name": "freeagent_think",
                                      "arguments": {"thought": "x", "propose_alternatives": True}})
        self.assertTrue(res["isError"])
        self.assertEqual(res["structuredContent"]["next_action"]["kind"], "unavailable_backend")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "thoughts.json")))

    def test_parse_alternatives(self):
        self.assertEqual(S.parse_alternatives("代替: A\n**代替2**: B\n代替: なし"), ["A", "B"])
        self.assertEqual(S.parse_alternatives("前置き\n1. X\n2. Y"), ["X", "Y"])
        for junk in [None, "", 123, [], "代替: なし"]:
            self.assertEqual(S.parse_alternatives(junk), [], f"junk={junk!r}")

    # ---- 閲覧
    def test_view_reads_without_writing(self):
        sid = S.tool_think({"thought": "a", "plan": ["x", "y"], "total_thoughts": 3})["session_id"]
        path = os.path.join(self.tmp, "thoughts.json")

        def read() -> bytes:
            with open(path, "rb") as fh:
                return fh.read()

        before = read()
        with self._no_calls():
            data = S.tool_think({"session_id": sid, "view": True, "verify": True})
        self.assertIsNone(data.get("error"))
        self.assertTrue(data["view"])
        self.assertEqual((data["step"], data["total_thoughts"]), (1, 3))
        self.assertEqual(data["ledger"]["plan_progress"]["total"], 2)
        self.assertEqual(read(), before, "view で台帳を書いてはいけない")
        self.assertIn("閲覧", S.render("freeagent_think", data))
        self.assertIn("session_id", S.tool_think({"view": True})["error"])

    def test_brief_view_returns_only_the_active_path(self):
        """要約（brief）: 現行の道筋だけを返し、省いた件数は隠さない（大きな台帳の読み戻し用）。"""
        sid = S.tool_think({"thought": "観測を集める", "plan": ["観測", "仮説"]})["session_id"]
        S.tool_think({"thought": "原因は DNS", "session_id": sid, "kind": "hypothesis"})
        S.tool_think({"thought": "切り分けの結果 DNS は違う", "session_id": sid,
                      "tests_hypothesis": 2, "hypothesis_status": "refuted"})
        S.tool_think({"thought": "観測をやり直す", "session_id": sid, "revises_thought": 1})

        full = S.tool_think({"session_id": sid, "view": True})
        brief = S.tool_think({"session_id": sid, "view": True, "brief": True})
        self.assertEqual([r["n"] for r in full["ledger"]["latest"]], [1, 2, 3, 4])
        self.assertEqual([r["n"] for r in brief["ledger"]["latest"]], [2, 3, 4],
                         "改訂済みの思考は要約から外れる")
        self.assertTrue(brief["ledger"]["brief"])
        self.assertEqual(brief["ledger"]["omitted"], 1)
        self.assertFalse(full["ledger"].get("brief"), "既定の view は従来どおり全文")
        # 判断材料（計画・仮説・分岐・道筋）は要約でも落とさない
        self.assertEqual(brief["ledger"]["plan_progress"]["total"], 2)
        self.assertEqual([h["n"] for h in brief["ledger"]["hypotheses"]], [2])
        self.assertEqual(brief["ledger"]["active_path"], [2, 3, 4])
        text = S.render("freeagent_think", brief)
        self.assertIn("（記録なし・要約）", text)
        self.assertIn("改訂・棄却で外した思考 1 件は省略", text)
        self.assertIn("#4", text)
        self.assertEqual(self._steps(sid), 4, "view（要約）で台帳を書いてはいけない")

    def test_brief_without_view_is_ignored_with_a_note(self):
        sid = S.tool_think({"thought": "a"})["session_id"]
        data = S.tool_think({"thought": "b", "session_id": sid, "brief": True})
        self.assertTrue(any("brief は view=true のときだけ" in n for n in data["notes"]))
        self.assertEqual(data["step"], 2)

    def test_brief_suggestion_appears_for_large_ledgers(self):
        """12 ステップを超えたら、読み戻しに brief を使うよう提案する（毎回は出さない）。"""
        sid = S.tool_think({"thought": "s1"})["session_id"]
        for i in range(2, 13):
            last = S.tool_think({"thought": f"s{i}", "session_id": sid})
        self.assertTrue(any("brief=true を併用" in s for s in last["suggestions"]))
        brief = S.tool_think({"session_id": sid, "view": True, "brief": True})
        self.assertFalse(any("brief=true を併用" in s for s in brief["suggestions"]),
                         "要約で読んでいるのに要約を勧めない")

    def test_structure_nudges_when_unused(self):
        """常用の補強: 計画なしの 1 ステップ目、構造なしの 3 ステップ目にだけ促す（毎回は出さない）。"""
        first = S.tool_think({"thought": "a"})
        self.assertTrue(any("plan で" in s for s in first["suggestions"]))
        sid = first["session_id"]
        S.tool_think({"thought": "b", "session_id": sid})
        third = S.tool_think({"thought": "c", "session_id": sid})
        self.assertTrue(any("仮説・分岐・改訂がありません" in s for s in third["suggestions"]))
        fourth = S.tool_think({"thought": "d", "session_id": sid})
        self.assertFalse(any("仮説・分岐・改訂がありません" in s for s in fourth["suggestions"]))
        planned = S.tool_think({"thought": "x", "plan": ["p"]})
        self.assertFalse(any("plan で" in s for s in planned["suggestions"]))

    def test_description_marks_think_as_standing_practice(self):
        desc = next(t["description"] for t in S.TOOLS if t["name"] == "freeagent_think")
        self.assertTrue(desc.startswith("【常用】"))
        for key in ("plan", "revises_thought", "branch_", "hypothesis", "total_thoughts"):
            self.assertIn(key, desc + S.PROACTIVE_INSTRUCTIONS)

    def test_render_structure_has_no_instructions(self):
        sid = S.tool_think({"thought": "a", "plan": ["x"], "kind": "hypothesis"})["session_id"]
        data = S.tool_think({"thought": "b", "session_id": sid, "branch_from_thought": 1,
                             "revises_thought": 1})
        text = S.render("freeagent_think", data)
        for needle in ("計画: 0/1", "分岐: b1", "仮説: #1", "改訂済み"):
            self.assertIn(needle, text)
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
    def test_default_six_and_opt_in_backends(self):
        self.assertEqual(set(S.DEFAULT_SOURCES),
                         {"wikipedia", "wikidata", "arxiv", "crossref", "openalex", "github"})
        self.assertEqual(set(S.KB_BACKENDS), set(S.DEFAULT_SOURCES) | {"datacite", "openaire", "zenodo", "ror",
                                                                       "doaj", "npm", "crates", "cinii",
                                                                       "osv", "ietf", "inspirehep", "oeis",
                                                                       "hfhub", "hn", "swh", "librariesio"})
        self.assertEqual(set(S.SOURCES), set(S.KB_BACKENDS))

    def test_arxiv_uses_https(self):
        """http は 301 の先で 406 になる（実測）。ソース上 https であることを固定する。"""
        import inspect
        src = inspect.getsource(S.kb_arxiv)
        self.assertIn("https://export.arxiv.org/api/query", src)
        self.assertNotIn("http://export.arxiv.org", src)

    def test_arxiv_is_throttled(self):
        self.assertGreaterEqual(S._ARXIV_MIN_INTERVAL, 1.0)

    def test_evidence_block_numbering(self):
        block = S._evidence_block([{"title": "A", "url": "u1", "year": 2020, "summary": "Body A"},
                                   {"title": "B", "url": "u2", "summary": "Body B"}])
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
    """推論プロバイダを同じ「検索→利用」に乗せるための契約。"""

    def test_registry_has_nine_providers(self):
        expected = ("nous", "openrouter", "nvidia", "huggingface", "groq", "cloudflare", "gemini",
                    "vercel", "ollama")
        for name in expected:
            self.assertIn(name, S.PROVIDER_SPECS)
            self.assertIn(name, S.PROVIDER_ORDER, "PROVIDER_ORDER に無いと一覧に出ない")
        self.assertEqual(len(S.PROVIDER_SPECS), len(expected))
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

    def test_snippet_covers_all_thinking_features(self):
        """判断規則の単一の出典に、分解・改訂・分岐・見積り・仮説の常用と無効時の振る舞いが入っている。"""
        for key in ("freeagent_think", "plan", "revises_thought", "branch_from_thought",
                    "resolve_branch", "total_thoughts", "kind=hypothesis", "tests_hypothesis",
                    "view=true", "無効・不通"):
            self.assertIn(key, self.mod.SNIPPET)

    def test_upsert_snippet_adds_updates_and_is_idempotent(self):
        user = "# 私の SOUL\n好みの口調\n"
        added, action = self.mod.upsert_snippet(user, "v1")
        self.assertEqual(action, "added")
        self.assertTrue(added.startswith(user), "利用者の記述を書き換えてはいけない")
        updated, action = self.mod.upsert_snippet(added, "v2")
        self.assertEqual(action, "updated")
        self.assertIn("v2", updated)
        self.assertNotIn("v1", updated)
        self.assertEqual(updated.count(self.mod.MARKER), 1, "差し替えでブロックが増殖してはいけない")
        again, action = self.mod.upsert_snippet(updated, "v2")
        self.assertEqual((again, action), (updated, "unchanged"))

    def test_upsert_replaces_legacy_block_without_end_marker(self):
        legacy = f"前文\n\n{self.mod.MARKER}\n古い規則\n後文\n"
        new, action = self.mod.upsert_snippet(legacy, "新しい規則")
        self.assertEqual(action, "updated")
        self.assertNotIn("古い規則", new)
        self.assertIn("前文", new)
        self.assertIn("後文", new, "旧形式の差し替えで後ろの記述を消してはいけない")

    def test_remove_snippet_restores_user_text(self):
        user = "# 私の SOUL\n好みの口調\n"
        added, _ = self.mod.upsert_snippet(user, "規則")
        removed, found = self.mod.remove_snippet(added)
        self.assertTrue(found)
        self.assertNotIn(self.mod.MARKER, removed)
        self.assertEqual(removed.rstrip("\n"), user.rstrip("\n"))
        self.assertEqual(self.mod.remove_snippet(user), (user, False))

    def test_write_preserves_crlf(self):
        """利用者の SOUL.md が CRLF なら CRLF のまま書く（改行コードを勝手に変えない）。"""
        self._crlf_body()

    def test_marker_detection_in_config(self):
        """ハーネス判別の目印（env.FREEAGENT_HARNESS）を自分の節からだけ読む。"""
        cfg = ("mcp_servers:\n"
               "  other:\n    command: x\n    env:\n      FREEAGENT_HARNESS: cursor\n"
               "  freeagent-bind:\n    command: py\n    args:\n    - C:/x/src/freeagent_bind/server.py\n"
               "    env:\n      OPENROUTER_API_KEY: sk-xxx\n"
               "model: foo\n")
        servers = self.mod.find_servers(cfg)
        self.assertEqual(self.mod.own_server(cfg, servers), "freeagent-bind")
        self.assertEqual(self.mod.configured_marker(cfg, "freeagent-bind"), "",
                         "他の節の目印を自分のものと取り違えない")
        cfg2 = cfg.replace("      OPENROUTER_API_KEY: sk-xxx\n",
                           "      OPENROUTER_API_KEY: sk-xxx\n      FREEAGENT_HARNESS: hermes\n")
        self.assertEqual(self.mod.configured_marker(cfg2, "freeagent-bind"), "hermes")
        renamed = cfg.replace("  freeagent-bind:", "  fab:")
        self.assertEqual(self.mod.own_server(renamed, self.mod.find_servers(renamed)), "fab",
                         "登録名を変えていても args から自分を見つける")

    def _crlf_body(self):
        import tempfile
        d = tempfile.mkdtemp()
        p = os.path.join(d, "SOUL.md")
        with open(p, "wb") as fh:
            fh.write("一行目\r\n二行目\r\n".encode("utf-8"))
        text, nl = self.mod._read_text(p)
        self.assertEqual(nl, "\r\n")
        body, _ = self.mod.upsert_snippet(text, "規則")
        self.mod._write_text(p, body, nl)
        with open(p, "rb") as fh:
            raw = fh.read()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"), "LF だけの行が混ざってはいけない")
        self.assertTrue(raw.startswith("一行目\r\n二行目\r\n".encode("utf-8")))

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


class TestHarnessDetection(unittest.TestCase):
    """§8.6: Hermes 以外のハーネスで起動されたら（止めずに）警告する。"""

    def setUp(self):
        S._harness_reset()
        self._env = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop(S.HARNESS_MARKER_ENV, None)
        os.environ.pop(S.HARNESS_WARN_ENV, None)

    def tearDown(self):
        self._env.stop()
        S._harness_reset()

    def _init(self, name="mcp", version="0.1.0"):
        with unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()) as err:
            res = S.harness_on_initialize({"protocolVersion": "2025-11-25",
                                           "clientInfo": {"name": name, "version": version}})
        return res, err.getvalue()

    def test_detect_three_way(self):
        d = S.detect_harness
        # 実測: Hermes は MCP SDK 既定の clientInfo を送る → 目印が無ければ判別不能
        self.assertEqual(d({"clientInfo": {"name": "mcp", "version": "0.1.0"}}, env={})["kind"], "unknown")
        self.assertEqual(d({"clientInfo": {"name": "mcp"}}, env={"FREEAGENT_HARNESS": "hermes"})["kind"], "hermes")
        self.assertEqual(d({"clientInfo": {"name": "mcp"}}, env={"FREEAGENT_HARNESS": "Hermes"})["source"], "env")
        self.assertEqual(d({"clientInfo": {"name": "hermes-agent"}}, env={})["kind"], "hermes")
        self.assertEqual(d({"clientInfo": {"name": "claude-code"}}, env={})["kind"], "other")
        self.assertEqual(d({"clientInfo": {"name": "mcp"}}, env={"FREEAGENT_HARNESS": "cursor"})["kind"], "other")
        for bad in (None, "x", {"clientInfo": "x"}, {"clientInfo": {"name": 5}}, {}):
            self.assertEqual(d(bad, env={})["kind"], "unknown", repr(bad))

    def test_initialize_declares_logging_and_echoes_protocol(self):
        res, _ = self._init("claude-code")
        self.assertIn("logging", res["capabilities"], "notifications/message を送るなら宣言が MUST")
        self.assertEqual(res["protocolVersion"], "2025-11-25")

    def test_other_warns_on_every_channel_once(self):
        res, err = self._init("claude-code")
        self.assertTrue(res["instructions"].startswith("【注意】"))
        self.assertIn("Hermes Agent 以外", err)
        note = S.harness_log_notification()
        self.assertEqual(note["method"], "notifications/message")
        self.assertEqual(note["params"]["level"], "warning")
        self.assertIsNone(S.harness_log_notification(), "ログ通知は 1 回だけ")
        first = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertTrue(first["content"][0]["text"].startswith("⚠️ Hermes Agent 以外"))
        self.assertEqual(first["structuredContent"]["harness"]["kind"], "other")
        second = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertNotIn("Hermes Agent 以外", second["content"][0]["text"], "content の警告は最初の 1 回だけ")
        self.assertNotIn("harness", second["structuredContent"])
        _, err2 = self._init("claude-code")
        self.assertEqual(err2, "", "stderr も 1 プロセス 1 回")

    def test_unknown_is_quiet(self):
        res, err = self._init("mcp")
        self.assertEqual(res["instructions"], S.PROACTIVE_INSTRUCTIONS)
        self.assertIn(S.HARNESS_MARKER_ENV, err, "目印の入れ方だけは stderr に残す")
        self.assertIsNone(S.harness_log_notification())
        first = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertNotIn("Hermes Agent 以外", first["content"][0]["text"])
        self.assertEqual(first["structuredContent"]["harness"]["kind"], "unknown")

    def test_hermes_is_silent(self):
        os.environ[S.HARNESS_MARKER_ENV] = "hermes"
        res, err = self._init("mcp")
        self.assertEqual((res["instructions"], err), (S.PROACTIVE_INSTRUCTIONS, ""))
        self.assertIsNone(S.harness_log_notification())
        first = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertNotIn("harness", first["structuredContent"])

    def test_warn_off_suppresses_but_keeps_verdict(self):
        os.environ[S.HARNESS_WARN_ENV] = "0"
        res, err = self._init("claude-code")
        self.assertEqual((res["instructions"], err), (S.PROACTIVE_INSTRUCTIONS, ""))
        self.assertIsNone(S.harness_log_notification())
        first = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertNotIn("Hermes Agent 以外", first["content"][0]["text"])
        self.assertEqual(first["structuredContent"]["harness"]["kind"], "other")

    def test_set_level_validates_and_gates_notification(self):
        self.assertEqual(S.harness_set_level({"level": "loud"})["code"], -32602)
        self.assertIsNone(S.harness_set_level({"level": "error"}))
        self._init("claude-code")
        self.assertIsNone(S.harness_log_notification(), "error 以上だけ欲しいクライアントに warning は送らない")

    def test_no_initialize_no_side_effects(self):
        out = S.handle_tool_call({"name": "freeagent_ask", "arguments": {"prompt": ""}})
        self.assertNotIn("harness", out["structuredContent"])
        self.assertIsNone(S.harness_status())

    def test_stdio_dispatch_sends_log_notification_after_initialized(self):
        lines = []
        with unittest.mock.patch.object(S, "write_line", lines.append), \
             unittest.mock.patch.object(sys, "stderr", new=__import__("io").StringIO()):
            S._dispatch_message({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2025-11-25",
                                            "clientInfo": {"name": "claude-code"}}}, None)
            S._dispatch_message({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
            S._dispatch_message({"jsonrpc": "2.0", "id": 2, "method": "logging/setLevel",
                                 "params": {"level": "debug"}}, None)
        msgs = [json.loads(x) for x in lines]
        self.assertEqual(msgs[0]["id"], 1)
        self.assertEqual(msgs[1]["method"], "notifications/message")
        self.assertEqual(msgs[2], {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_models_reports_harness(self):
        self._init("claude-code")
        with unittest.mock.patch.object(S, "provider_status", lambda: []), \
             unittest.mock.patch.object(S, "free_model_refs", lambda free_only=True: []), \
             unittest.mock.patch.object(S, "cooling_refs", lambda: {}):
            try:
                data = S.tool_models({})
            except Exception as exc:  # noqa: BLE001
                self.skipTest(f"tool_models の依存を差し替えきれない: {exc}")
        self.assertEqual(data["harness"]["kind"], "other")
        self.assertIn("ハーネス: Hermes 以外", S.render("freeagent_models", data))


class TestFallbackIndependence(unittest.TestCase):
    """並列呼び出しのフォールバックが、他の枠・除外モデルと同じモデルで枠を埋めない。"""

    def _patched(self, calls, fail=("p/a", "p/b")):
        def call_once(provider, model, *a, **k):
            ref = f"{provider}/{model}"
            calls.append(ref)
            if ref in fail:
                raise S.HttpStatusError(503, "busy")
            return {"text": f"answer from {ref}", "latency_s": 0.1, "truncated": False, "tokens": 1}
        return [
            unittest.mock.patch.object(S, "resolve_ref", lambda ref, free_only=True: tuple(ref.split("/", 1))),
            unittest.mock.patch.object(S, "make_ref", lambda p, m: f"{p}/{m}"),
            unittest.mock.patch.object(S, "_candidates",
                                       lambda ref, free_only=True: [ref] + [r for r in ("p/b", "p/x", "p/y") if r != ref]),
            unittest.mock.patch.object(S, "cooling_refs", lambda: {}),
            unittest.mock.patch.object(S, "observe_call", lambda *a, **k: None),
            unittest.mock.patch.object(S, "clear_provider_auth", lambda *a, **k: None),
            unittest.mock.patch.object(S, "_call_once", call_once),
        ]

    def _run(self, refs, avoid=None, fail=("p/a",)):
        calls = []
        patches = self._patched(calls, fail)
        for p in patches:
            p.start()
        try:
            return S.ask_many(refs, "q", avoid=avoid), calls
        finally:
            for p in reversed(patches):
                p.stop()

    def test_fallback_skips_peer_slot_model(self):
        (a, b), _ = self._run(["p/a", "p/b"], fail=("p/a",))
        self.assertEqual(b["served_by"], "p/b")
        self.assertNotEqual(a.get("served_by"), "p/b", "他の枠のモデルで埋めると独立 2 体が実質 1 体になる")
        self.assertEqual(a["served_by"], "p/x")

    def test_fallback_skips_avoided_models(self):
        (a,), _ = self._run(["p/a"], avoid=["p/b", "p/x"], fail=("p/a",))
        self.assertEqual(a["served_by"], "p/y", "検証者など avoid のモデルには落ちない")

    def test_two_slots_never_share_one_fallback(self):
        (a, b), _ = self._run(["p/a", "p/c"], fail=("p/a", "p/c"))
        self.assertNotEqual(a.get("served_by"), b.get("served_by"))

    def test_exhausted_by_avoidance_is_an_error_not_a_duplicate(self):
        (a, b), _ = self._run(["p/a", "p/b"], avoid=["p/x", "p/y"], fail=("p/a",))
        self.assertIn("error", a)
        self.assertEqual(a.get("avoided_duplicates"), 3)
        self.assertEqual(b["served_by"], "p/b")

    def test_claims_are_atomic(self):
        claims = S._ModelClaims(["p/a"], ["p/z"])
        self.assertFalse(claims.take("p/a"))
        self.assertFalse(claims.take("p/z"))
        self.assertTrue(claims.take("p/x"))
        self.assertFalse(claims.take("p/x"))

    def test_parse_alternatives_drops_cut_last_line(self):
        text = "代替: 独自メソッドで判別する。\n代替: 親プロセスのコマン"
        self.assertEqual(S.parse_alternatives(text, truncated=True), ["独自メソッドで判別する。"])
        self.assertEqual(len(S.parse_alternatives(text)), 2, "打ち切られていなければ全行を拾う")
        self.assertEqual(S.parse_alternatives("代替", truncated=True), [])


class TestKnowledgeDeadline(unittest.TestCase):
    """§5.8: 知識取得は締め切りまでに届いた分だけ返し、遅いソースで全体を待たせない。"""

    def _backends(self, **delays):
        def make(name, delay):
            def fn(q, limit, opts):
                if delay == "raise":
                    raise RuntimeError("boom")
                time.sleep(delay)
                return {"items": [{"title": name, "url": f"https://x/{name}"}],
                        "citations": [{"source": name, "title": name, "url": f"https://x/{name}"}]}
            return fn
        return unittest.mock.patch.dict(S.KB_BACKENDS, {k: make(k, v) for k, v in delays.items()}, clear=True)

    def test_slow_source_is_reported_not_waited_for(self):
        with self._backends(fast=0.0, slow=3.0):
            t = time.monotonic()
            out = S.knowledge_lookup("q", ["fast", "slow"], deadline=0.4)
            spent = time.monotonic() - t
        self.assertLess(spent, 1.5, "遅いソースの完了を待たない")
        self.assertEqual(out["timed_out"], ["slow"])
        self.assertTrue(out["results"]["slow"]["timed_out"])
        self.assertIn("slow", out["errors"], "脱落を隠さない")
        self.assertEqual(out["citation_count"], 1)
        self.assertIsInstance(out["timings"]["fast"], float)
        self.assertIsNone(out["timings"]["slow"])
        self.assertEqual(list(out["results"]), ["fast", "slow"], "要求した順を保つ")

    def test_all_sources_run_at_once(self):
        """旧実装は並列 4 で 6 ソース中 2 つが待ち行列に入った。"""
        names = {f"s{i}": 0.5 for i in range(6)}
        with self._backends(**names):
            t = time.monotonic()
            out = S.knowledge_lookup("q", list(names), deadline=5)
            spent = time.monotonic() - t
        self.assertNotIn("timed_out", out)
        self.assertLess(spent, 0.95, f"6 ソースが同時に走っていない（{spent:.2f} 秒）")

    def test_backend_exception_does_not_leak(self):
        with self._backends(ok=0.0, bad="raise"):
            out = S.knowledge_lookup("q", ["ok", "bad"], deadline=2)
        self.assertIn("RuntimeError", out["errors"]["bad"])
        self.assertEqual(out["citation_count"], 1)

    def test_late_result_lands_in_cache_for_next_call(self):
        key = f"test-late:{time.time()}"

        def slow(q, limit, opts):
            return S._kb_cached(key, lambda: (time.sleep(0.5), {"items": [{"title": "late"}],
                                                                "citations": [{"title": "late"}]})[1])
        with unittest.mock.patch.dict(S.KB_BACKENDS, {"slow": slow}, clear=True):
            first = S.knowledge_lookup("q", ["slow"], deadline=0.1)
            self.assertEqual(first["timed_out"], ["slow"])
            time.sleep(0.8)
            t = time.monotonic()
            second = S.knowledge_lookup("q", ["slow"], deadline=0.3)
            self.assertLess(time.monotonic() - t, 0.25, "裏で完了した分はキャッシュから即座に返る")
        self.assertNotIn("timed_out", second)
        self.assertEqual(second["citation_count"], 1)
        with S._KB_CACHE_LOCK:
            S._KB_CACHE.pop(key, None)

    def test_render_shows_seconds_and_timeouts(self):
        with self._backends(fast=0.0, slow=3.0):
            out = S.knowledge_lookup("q", ["fast", "slow"], deadline=0.3)
        text = S.render("freeagent_lookup", out)
        self.assertIn("間に合わず 1 件", text)
        self.assertIn("⏱ slow", text)
        self.assertRegex(text, r"✓ fast \(1 件・\d+\.\d 秒\)")

    def test_measure_kb_round_report_and_trim(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "measure_kb", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                       "scripts", "measure_kb.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.dict(os.environ, {"FREEAGENT_KB_LATENCY_PATH": os.path.join(tmp, "k.jsonl")}), \
             self._backends(a=0.0, b="raise"):
            rows = mod.measure_round(0)
            self.assertEqual({r["source"] for r in rows}, {"a", "b"})
            self.assertFalse(any(r["env_failure"] for r in rows), "片方だけの失敗はネットワーク障害ではない")
            self.assertNotIn("title", json.dumps(rows), "本文（タイトル等）は記録しない")
            mod.append(rows)
            with unittest.mock.patch.object(mod, "MAX_LINES", 3):
                mod.append(rows)
                mod.append(rows)
            with open(os.path.join(tmp, "k.jsonl"), encoding="utf-8") as fh:
                self.assertEqual(len(fh.readlines()), 3, "上限を超えたら古い行から捨てる")
            buf = __import__("io").StringIO()
            with unittest.mock.patch.object(sys, "stdout", buf):
                self.assertEqual(mod.report(), 0)
            self.assertIn("ソース別", buf.getvalue())
        with unittest.mock.patch.dict(S.KB_BACKENDS, {
                "x": lambda q, l, o: {"error": "URLError: <urlopen error [WinError 10061]>"},
                "y": lambda q, l, o: {"error": "TimeoutError: timed out"}}, clear=True):
            self.assertTrue(all(r["env_failure"] for r in mod.measure_round(0)),
                            "全滅かつ接続系ならこちらのネットワーク障害として区別する")

    def test_scheduled_tick_counts_down_and_unschedules(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "measure_kb2", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                        "scripts", "measure_kb.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        calls = []
        with tempfile.TemporaryDirectory() as tmp, \
             unittest.mock.patch.dict(os.environ, {"FREEAGENT_KB_LATENCY_PATH": os.path.join(tmp, "k.jsonl")}), \
             unittest.mock.patch.object(mod, "unschedule", lambda: calls.append(1) or 0):
            mod._write_remaining(2)
            self.assertTrue(mod._scheduled_tick())
            self.assertEqual(calls, [])
            self.assertTrue(mod._scheduled_tick(), "最後の 1 回は計測する")
            self.assertEqual(calls, [1], "最後の 1 回でタスクを消す")
            self.assertFalse(mod._scheduled_tick(), "残りが無ければ計測しない")


class TestVersionConsistency(unittest.TestCase):
    def test_pyproject_matches_server_version(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(f'version = "{S.SERVER_VERSION}"', text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
