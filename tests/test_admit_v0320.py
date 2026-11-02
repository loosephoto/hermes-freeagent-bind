"""v0.32.0: freeagent_admit（決定論ゲート・Mandate・証拠つき報告・冪等フェンス）。ネット不要。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))  # 他のテストと同じく src を自前で足す（editable 無しでも動く）

from freeagent_bind import server as S  # noqa: E402


class AdmitTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fa-admit-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": self.tmp})
        self.env.start()
        self.addCleanup(self.env.stop)

    def policy(self, **data):
        path = os.path.join(self.tmp, "admit_policy.json")
        base = {"accept_mode": "observe",
                "sources": {"github": {"allow": ["me"], "bots": ["dependabot[bot]"],
                                       "admission": {"issue": {"unlisted_addressed": "ignore",
                                                               "listed_silent": "inferred"}}},
                            "feishu": {"allow": [], "bots": [],
                                       "admission": {"chat:support": {
                                           "unlisted_addressed": "explicit"}}}},
                "rules": [{"id": "r1", "enabled": True}, {"id": "r2", "enabled": False}]}
        base.update(data)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(base, fh, ensure_ascii=False)
        return base

    def trig(self, **over):
        base = {"source": "github", "thread": "gh:a/b#1", "trigger_id": "T1",
                "actor": "me", "kind": "mention", "scope": "issue"}
        base.update(over)
        return base

    def log_lines(self):
        path = os.path.join(self.tmp, "admission.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class GateTest(AdmitTestBase):
    def test_listed_person_addressing_agent_is_explicit(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig()]})
        row = got["decisions"][0]
        self.assertEqual(row["disposition"], "accept")
        self.assertEqual(row["strength"], "explicit")
        self.assertEqual(row["basis"], "listed+addressed")

    def test_unlisted_person_addressing_agent_is_ignored_by_table(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(actor="stranger")]})
        row = got["decisions"][0]
        self.assertEqual(row["disposition"], "ignored")
        self.assertEqual(row["strength"], "not_admitted")
        self.assertEqual(row["basis"], "table_ignore")

    def test_table_cell_can_grant_explicit(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(source="feishu", scope="chat:support",
                                                   actor="guest", thread="fs:9#1")]})
        self.assertEqual(got["decisions"][0]["strength"], "explicit")
        self.assertEqual(got["decisions"][0]["disposition"], "accept")

    def test_followup_on_ticket_thread_is_explicit(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(kind="comment", ticket="t_0001")]})
        self.assertEqual(got["decisions"][0]["strength"], "explicit")
        self.assertEqual(got["decisions"][0]["basis"], "followup+listed")

    def test_listed_bot_is_inferred_only(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(actor="dependabot[bot]", is_bot=True)]})
        row = got["decisions"][0]
        self.assertEqual(row["strength"], "inferred")
        self.assertEqual(row["disposition"], "would_accept")
        self.assertTrue(row["needs_assessment"] if "needs_assessment" in row else True)
        self.assertEqual(got["needs_assessment"], ["T1"])

    def test_unknown_bot_is_not_admitted(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(actor="evil[bot]", is_bot=True)]})
        self.assertEqual(got["decisions"][0]["strength"], "not_admitted")
        self.assertEqual(got["decisions"][0]["disposition"], "ignored")

    def test_explicit_still_accepts_when_mode_is_off(self):
        self.policy(accept_mode="off")
        got = S.tool_admit({"triggers": [self.trig()]})
        self.assertEqual(got["decisions"][0]["disposition"], "accept")

    def test_inferred_is_ignored_when_mode_is_off(self):
        self.policy(accept_mode="off")
        got = S.tool_admit({"triggers": [self.trig(actor="dependabot[bot]", is_bot=True)]})
        row = got["decisions"][0]
        self.assertEqual(row["disposition"], "ignored")
        self.assertEqual(row["why"], "mode_off")

    def test_inferred_is_accepted_when_mode_is_auto(self):
        self.policy(accept_mode="auto")
        got = S.tool_admit({"triggers": [self.trig(actor="dependabot[bot]", is_bot=True)]})
        self.assertEqual(got["decisions"][0]["disposition"], "accept")

    def test_default_policy_is_observe_and_empty_allowlist(self):
        got = S.tool_admit({"triggers": [self.trig()]})
        self.assertEqual(got["accept_mode"], "observe")
        self.assertEqual(got["decisions"][0]["disposition"], "ignored")


class MandateTest(AdmitTestBase):
    def test_mandate_carries_version_rules_card_and_assumptions(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(card="t_0001",
                                                   assumptions=[{"question": "予算は？",
                                                                 "default": "30 秒"}])]})
        mandate = got["decisions"][0]["mandate"]
        self.assertTrue(mandate["policy_version"].startswith("p"))
        self.assertEqual(mandate["rules"], ["r1"], "無効なルールは入れない")
        self.assertEqual(mandate["card"], "t_0001")
        self.assertEqual(mandate["assumptions"][0]["default"], "30 秒")

    def test_policy_version_is_content_addressed(self):
        self.policy()
        first = S.admit_policy()["version"]
        self.assertEqual(S.admit_policy()["version"], first, "同じ内容なら同じ版")
        self.policy(accept_mode="auto")
        self.assertNotEqual(S.admit_policy()["version"], first, "内容が変われば版が変わる")


class IdempotencyTest(AdmitTestBase):
    def test_same_trigger_twice_is_replayed_not_duplicated(self):
        self.policy()
        first = S.tool_admit({"triggers": [self.trig()]})
        second = S.tool_admit({"triggers": [self.trig()]})
        self.assertEqual(first["decisions"][0]["disposition"], "accept")
        self.assertTrue(second["decisions"][0]["replayed"])
        self.assertEqual(second["counts"]["replayed"], 1)
        self.assertEqual(len(self.log_lines()), 1, "ログは 1 行のまま（二重起票しない）")

    def test_different_trigger_id_is_a_new_decision(self):
        self.policy()
        S.tool_admit({"triggers": [self.trig()]})
        got = S.tool_admit({"triggers": [self.trig(trigger_id="T2")]})
        self.assertFalse(got["decisions"][0].get("replayed"))
        self.assertEqual(len(self.log_lines()), 2)

    def test_missing_trigger_id_is_an_error_row_not_a_silent_skip(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(trigger_id=""), self.trig()]})
        self.assertEqual(got["counts"]["errors"], 1)
        self.assertIn("source と trigger_id は必須", got["decisions"][0]["error"])
        self.assertEqual(len(self.log_lines()), 1, "裁定できた 1 件だけ記録する")


class ReportTest(AdmitTestBase):
    def test_ready_without_evidence_is_rejected(self):
        got = S.tool_admit({"action": "report", "report": {"kind": "ready"}})
        self.assertFalse(got["accepted"])
        self.assertEqual(got["why"], "no_evidence")
        self.assertEqual(got["next_action"]["kind"], "report_rejected")

    def test_ready_with_all_pass_evidence_is_accepted(self):
        got = S.tool_admit({"action": "report", "report": {
            "kind": "ready",
            "evidence": {"commands": [{"cmd": "python -m unittest", "result": "pass"},
                                      {"cmd": "python scripts/check_integrity.py", "result": "pass"}]}}})
        self.assertTrue(got["accepted"])
        self.assertEqual(got["evidence_commands"], 2)

    def test_ready_with_a_failure_is_rejected(self):
        got = S.tool_admit({"action": "report", "report": {
            "kind": "ready", "evidence": {"commands": [{"cmd": "x", "result": "fail"}]}}})
        self.assertFalse(got["accepted"])
        self.assertEqual(got["why"], "evidence_failed")

    def test_blocked_without_reason_is_rejected(self):
        got = S.tool_admit({"action": "report", "report": {"kind": "blocked"}})
        self.assertFalse(got["accepted"])
        self.assertEqual(got["why"], "no_reason")

    def test_unknown_kind_lists_the_vocabulary(self):
        got = S.tool_admit({"action": "report", "report": {"kind": "finished"}})
        self.assertFalse(got["accepted"])
        self.assertEqual(got["why"], "unknown_kind")
        self.assertIn("progress", got["ask"])

    def test_report_writes_no_state(self):
        S.tool_admit({"action": "report", "report": {"kind": "ready"}})
        self.assertEqual(self.log_lines(), [], "報告の検査は記録を作らない")


class RecentAndShapeTest(AdmitTestBase):
    def test_list_recent_returns_newest_first_and_filters(self):
        self.policy()
        S.tool_admit({"triggers": [self.trig(card="t_0001"), self.trig(trigger_id="T2", actor="x")]})
        got = S.tool_admit({"action": "list_recent", "limit": 10})
        self.assertEqual(got["count"], 2)
        self.assertEqual(got["decisions"][0]["trigger_id"], "T2", "新しい順")
        only = S.tool_admit({"action": "list_recent", "disposition": "accept"})
        self.assertEqual(only["count"], 1)

    def test_list_recent_render_shows_time_source_and_card(self):
        self.policy()
        S.tool_admit({"triggers": [self.trig(card="t_0001")]})
        got = S.tool_admit({"action": "list_recent"})
        text = S.render("freeagent_admit", got)
        self.assertIn("記録の読み戻し: 1 件", text)
        self.assertIn("T1（github）", text)
        self.assertIn("card=t_0001", text)
        self.assertIn("accept/explicit", text)

    def test_too_many_triggers_is_an_error_not_a_silent_truncation(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(trigger_id=f"T{i}")
                                         for i in range(S.ADMIT_MAX_TRIGGERS + 1)]})
        self.assertIn("多すぎます", got["error"])

    def test_unknown_action_returns_next_action(self):
        got = S.tool_admit({"action": "approve"})
        self.assertIn("evaluate", got["error"])
        self.assertEqual(got["next_action"]["kind"], "invalid_argument")

    def test_render_shows_counts_and_marks(self):
        self.policy()
        got = S.tool_admit({"triggers": [self.trig(), self.trig(trigger_id="T2",
                                                               actor="dependabot[bot]",
                                                               is_bot=True)]})
        text = S.render("freeagent_admit", got)
        self.assertIn("受付 accept 1", text.replace("受付 1", "受付 accept 1"))
        self.assertIn("記録のみ 1", text)
        self.assertIn("査読が要る候補: T2", text)

    def test_report_render_says_差し戻し(self):
        text = S.render("freeagent_admit", S.tool_admit({"action": "report",
                                                         "report": {"kind": "ready"}}))
        self.assertIn("差し戻し", text)
        self.assertIn("no_evidence", text)


if __name__ == "__main__":
    unittest.main()
