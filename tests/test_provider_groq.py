"""Groq Free-tier provider safety contracts (offline)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


GROQ_SPEC = {
    "base_url": "https://api.groq.com/openai/v1",
    "key": "test-key",
    "key_env": "GROQ_API_KEY",
    "free_kind": "allowlist",
    "free_model_ids": ("openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"),
    "required_env_flags": ("FREEAGENT_GROQ_FREE_TIER",),
    "catalog_requires_ready": True,
    "note": "Groq test provider",
}


class TestGroqFreeTier(unittest.TestCase):
    def setUp(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("groq", None)

    def test_free_plan_confirmation_is_required_before_provider_is_ready(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "0"}, clear=False):
            self.assertFalse(S.provider_ready("groq"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "1"}, clear=False):
            self.assertTrue(S.provider_ready("groq"))

    def test_malformed_free_plan_flag_does_not_count_as_confirmation(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}):
            with mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "configured"}, clear=False):
                self.assertFalse(S.provider_ready("groq"))

    def test_selected_allowed_model_is_blocked_without_free_plan_confirmation(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}):
            with mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "0"}, clear=False):
                with mock.patch.object(S, "provider_http") as http:
                    with mock.patch.object(S, "observe_call"):
                        result = S.call_model("groq/openai/gpt-oss-20b", "test", allow_fallback=False)
        self.assertIn("FREEAGENT_GROQ_FREE_TIER", result["error"])
        http.assert_not_called()

    def test_status_explains_missing_plan_confirmation(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}):
            with mock.patch.object(S, "PROVIDER_ORDER", ["groq"]):
                with mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "0"}, clear=False):
                    with mock.patch.object(S, "provider_http") as http:
                        row = S.provider_status()[0]
        self.assertIn("FREEAGENT_GROQ_FREE_TIER", row["missing_settings"])
        self.assertTrue(row["requires_activation"])
        http.assert_not_called()

    def test_unconfirmed_free_plan_does_not_trigger_catalog_request(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "false"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("groq", ttl=0), [])
            http.assert_not_called()

    def test_only_published_free_models_are_marked_free(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}):
            self.assertTrue(S._is_free("groq", {"id": "openai/gpt-oss-120b",
                                                  "pricing": {"prompt": "0.15", "completion": "0.60"}}))
            self.assertFalse(S._is_free("groq", {"id": "meta-llama/llama-3.3-70b-versatile",
                                                   "pricing": {"prompt": "0", "completion": "0"}}))
            self.assertFalse(S._is_free("groq", {"id": "openai/gpt-oss-120b:preview",
                                                   "pricing": {"prompt": "0", "completion": "0"}}))

    def test_paid_or_unlisted_explicit_model_is_rejected_before_http(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GROQ_FREE_TIER": "1"}, clear=False), \
             mock.patch.object(S, "provider_http") as http, \
             mock.patch.object(S, "observe_call"):
            result = S.call_model("groq/meta-llama/llama-3.3-70b-versatile", "test",
                                  allow_fallback=False)
        self.assertIn("error", result)
        self.assertIn("Free", result["error"])
        http.assert_not_called()

    def test_api_id_suffixes_do_not_erase_groq_provider_prefix(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"groq": GROQ_SPEC}):
            self.assertEqual(S.split_ref("groq/openai/gpt-oss-120b"),
                             ("groq", "openai/gpt-oss-120b"))


if __name__ == "__main__":
    unittest.main()
