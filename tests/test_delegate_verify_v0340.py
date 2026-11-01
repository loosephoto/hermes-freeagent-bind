"""v0.34.0: delegate の完了報告条件を依頼文に前置きする（description 依存をやめる）。"""
import unittest
from unittest import mock

from freeagent_bind import server as S


class _FakeProc:
    returncode = 0
    stdout = "done\n"
    stderr = ""


class DelegateVerifyTest(unittest.TestCase):
    def _run(self, args):
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _FakeProc()

        with mock.patch.object(S, "ALLOW_AGENT", True), \
             mock.patch.object(S, "HERMES_BIN", "hermes"), \
             mock.patch("subprocess.run", side_effect=fake_run):
            return S.tool_delegate(args), seen

    def test_prefix_is_prepended_by_default(self):
        got, seen = self._run({"task": "テストを直して"})
        sent = seen["argv"][-1]
        self.assertTrue(sent.startswith(S.DELEGATE_VERIFY_PREFIX), sent[:60])
        self.assertIn("テストを直して", sent)
        self.assertTrue(got["verification_enforced"])
        self.assertIn(S.DELEGATE_VERIFY_PREFIX, got["task_effective"])

    def test_prefix_can_be_disabled(self):
        got, seen = self._run({"task": "テストを直して", "verify_required": False})
        self.assertEqual(seen["argv"][-1], "テストを直して", "素の依頼がそのまま渡る")
        self.assertFalse(got["verification_enforced"])

    def test_string_false_is_respected(self):
        got, seen = self._run({"task": "x", "verify_required": "false"})
        self.assertFalse(got["verification_enforced"])
        self.assertEqual(seen["argv"][-1], "x")

    def test_string_true_is_respected(self):
        got, _ = self._run({"task": "x", "verify_required": "1"})
        self.assertTrue(got["verification_enforced"])

    def test_next_step_points_at_admit_report(self):
        got, _ = self._run({"task": "x"})
        self.assertIn("freeagent_admit", got["next_step"])
        self.assertIn("report", got["next_step"])

    def test_render_says_enforced(self):
        got, _ = self._run({"task": "x"})
        self.assertIn("報告条件: 強制", S.render("freeagent_delegate", got))
        off, _ = self._run({"task": "x", "verify_required": False})
        self.assertIn("報告条件: 未強制", S.render("freeagent_delegate", off))

    def test_disabled_route_still_refuses(self):
        with mock.patch.object(S, "ALLOW_AGENT", False):
            got = S.tool_delegate({"task": "x"})
        self.assertFalse(got.get("enabled", True))
        self.assertIn("FREEAGENT_ALLOW_AGENT", got["error"])


if __name__ == "__main__":
    unittest.main()
