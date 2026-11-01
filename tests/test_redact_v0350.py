"""v0.35.0: 上流へ送る前の秘密の伏せ字。ネット不要。"""
import os
import tempfile
import unittest
from unittest import mock

from freeagent_bind import server as S

SK = "sk-abcdefghijklmnopqrstuvwx"
GHP = "ghp_012345678901234567890123456789012345"
AWS = "AKIAIOSFODNN7EXAMPLE"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"


class ScrubTest(unittest.TestCase):
    def test_known_shapes_are_masked(self):
        text = f"a={SK} b={GHP} c={AWS} d={JWT}"
        out, n = S.scrub_secrets(text)
        self.assertEqual(n, 4)
        for secret in (SK, GHP, AWS, JWT):
            self.assertNotIn(secret, out)
        self.assertIn("[REDACTED:sk]", out)
        self.assertIn("[REDACTED:jwt]", out)

    def test_private_key_block_is_masked(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----"
        out, n = S.scrub_secrets(f"key:\n{block}\nend")
        self.assertEqual(n, 1)
        self.assertNotIn("MIIabc", out)

    def test_bearer_header_is_masked(self):
        out, n = S.scrub_secrets("Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
        self.assertEqual(n, 1)
        self.assertIn("Bearer [REDACTED]", out)

    def test_ordinary_text_is_left_alone(self):
        # コミット SHA・モデル ID・短い文字列は伏せない（誤爆させない）
        text = ("commit 587bf073a1b2c3d4e5f60718293a4b5c6d7e8f90 / "
                "nous/ornith-1.5-122b:free / 30c1a1c9f6b1c0e2")
        out, n = S.scrub_secrets(text)
        self.assertEqual(n, 0)
        self.assertEqual(out, text)

    def test_short_lookalikes_are_not_masked(self):
        out, n = S.scrub_secrets("sk-short and ghp_tiny")
        self.assertEqual(n, 0)
        self.assertEqual(out, "sk-short and ghp_tiny")

    def test_can_be_disabled(self):
        out, n = S.scrub_secrets(f"x={SK}", enabled=False)
        self.assertEqual(n, 0)
        self.assertEqual(out, f"x={SK}")


class UpstreamTest(unittest.TestCase):
    def test_call_once_masks_before_sending(self):
        seen = {}

        def fake_http(path, provider=None, payload=None, timeout=None):
            seen["payload"] = payload
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        with mock.patch.object(S, "provider_http", side_effect=fake_http):
            result = S._call_once("nous", "m", f"key is {GHP}", "sys", 64, None)
        sent = seen["payload"]["messages"][-1]["content"]
        self.assertNotIn(GHP, sent)
        self.assertIn("[REDACTED:ghp]", sent)
        self.assertEqual(result["redactions"], 1)

    def test_consult_reports_total_redactions_and_warns(self):
        def fake_ask(refs, prompt, **kwargs):
            return [{"ref": r, "served_by": r, "redactions": 1,
                     "text": "結論: OK\n重要度: nit\nメインに確認したい点: なし"} for r in refs]

        tmp = tempfile.mkdtemp(prefix="fa-redact-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        with mock.patch.dict(os.environ, {"FREEAGENT_STATE_DIR": tmp}), \
             mock.patch.object(S, "_select_or_error", return_value=(["nous/a", "nous/b"], {})), \
             mock.patch.object(S, "ask_many", side_effect=fake_ask):
            got = S.tool_consult({"question": "Q", "mode": "review", "draft": "D"})
        self.assertEqual(got["redactions"], 2)
        self.assertIn("伏せ字", S.render("freeagent_consult", got))


if __name__ == "__main__":
    unittest.main()
