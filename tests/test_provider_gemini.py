"""Gemini Developer API free-tier provider contracts (offline)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


GEMINI_SPEC = {
    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
    "key": "test-key",
    "key_env": "GEMINI_API_KEY",
    "free_kind": "allowlist",
    "free_model_ids": ("gemini-3.8-flash", "gemini-3.7-flash"),
    "required_env_flags": ("FREEAGENT_GEMINI_FREE_TIER", "FREEAGENT_GEMINI_UNPAID_DATA_ACK"),
    "catalog_requires_ready": True,
    "note": "Gemini Free tier and data-use acknowledgement required",
}


class TestGeminiFreeTier(unittest.TestCase):
    def test_gemini_provider_is_registered_by_default(self):
        self.assertIn("gemini", S.PROVIDER_SPECS)
        self.assertIn("gemini", S.PROVIDER_ORDER)
        self.assertEqual(S.PROVIDER_SPECS["gemini"]["key_env"], "GEMINI_API_KEY")
        self.assertIn("generativelanguage.googleapis.com/v1beta/openai",
                      S.PROVIDER_SPECS["gemini"]["base_url"])

    def setUp(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("gemini", None)

    def test_free_tier_and_unpaid_data_ack_are_both_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GEMINI_FREE_TIER": "1",
                                          "FREEAGENT_GEMINI_UNPAID_DATA_ACK": "0"}, clear=False):
            self.assertFalse(S.provider_ready("gemini"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GEMINI_FREE_TIER": "1",
                                          "FREEAGENT_GEMINI_UNPAID_DATA_ACK": "1"}, clear=False):
            self.assertTrue(S.provider_ready("gemini"))

    def test_only_currently_free_allowlisted_gemini_models_are_free(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}):
            self.assertTrue(S._is_free("gemini", {"id": "gemini-3.8-flash"}))
            self.assertTrue(S._is_free("gemini", {"id": "gemini-3.7-flash"}))
            self.assertFalse(S._is_free("gemini", {"id": "gemini-3.1-pro-preview",
                                                     "pricing": {"input": "0", "output": "0"}}))

    def test_model_listing_is_not_called_without_privacy_ack(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GEMINI_FREE_TIER": "1",
                                          "FREEAGENT_GEMINI_UNPAID_DATA_ACK": "false"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("gemini", ttl=0), [])
            http.assert_not_called()

    def test_models_endpoint_and_chat_endpoint_are_openai_compatible(self):
        models = {"data": [{"id": "gemini-3.8-flash", "object": "model"},
                           {"id": "gemini-2.5-pro", "object": "model"}]}
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GEMINI_FREE_TIER": "1",
                                          "FREEAGENT_GEMINI_UNPAID_DATA_ACK": "1"}, clear=False), \
             mock.patch.object(S, "provider_http", return_value=models) as http:
            got = S.fetch_provider_models("gemini", ttl=0)
        self.assertEqual(http.call_args.args[0], "/models")
        self.assertEqual([row["id"] for row in got], ["gemini-3.8-flash", "gemini-2.5-pro"])
        self.assertTrue(got[0]["free"])
        self.assertFalse(got[1]["free"])
        completion = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.object(S, "provider_http", return_value=completion) as chat:
            result = S._call_once("gemini", "gemini-3.8-flash", "q", "system", 8, None)
        self.assertEqual(chat.call_args.args[0], "/chat/completions")
        self.assertEqual(result["text"], "hello")

    def test_explicit_paid_or_unlisted_gemini_model_is_rejected(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"gemini": GEMINI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_GEMINI_FREE_TIER": "1",
                                          "FREEAGENT_GEMINI_UNPAID_DATA_ACK": "1"}, clear=False), \
             mock.patch.object(S, "provider_http") as http, \
             mock.patch.object(S, "observe_call"):
            result = S.call_model("gemini/gemini-2.5-pro", "q", allow_fallback=False)
        self.assertIn("error", result)
        self.assertIn("Free", result["error"])
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
