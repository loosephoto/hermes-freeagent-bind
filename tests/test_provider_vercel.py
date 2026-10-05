"""Vercel AI Gateway free-tier provider contracts (offline)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


VERCEL_SPEC = {
    "base_url": "https://ai-gateway.vercel.sh/v1",
    "key": "test-key",
    "key_env": "AI_GATEWAY_API_KEY",
    "free_kind": "allowlist",
    "free_model_ids": ("openai/gpt-oss-120b", "openai/gpt-5-mini",
                       "inclusionai/ling-3.1-flash", "poolside/laguna-s-2.1-free"),
    "required_env_flags": ("FREEAGENT_VERCEL_FREE_TIER",),
    "catalog_requires_ready": True,
    "note": "Vercel AI Gateway free tier (monthly credit) acknowledgement required",
}


class TestVercelFreeTier(unittest.TestCase):
    def test_vercel_provider_is_registered_by_default(self):
        self.assertIn("vercel", S.PROVIDER_SPECS)
        self.assertIn("vercel", S.PROVIDER_ORDER)
        self.assertEqual(S.PROVIDER_SPECS["vercel"]["key_env"], "AI_GATEWAY_API_KEY")
        self.assertIn("ai-gateway.vercel.sh/v1", S.PROVIDER_SPECS["vercel"]["base_url"])
        self.assertEqual(S.PROVIDER_SPECS["vercel"]["free_kind"], "allowlist")
        self.assertTrue(S.PROVIDER_SPECS["vercel"]["catalog_requires_ready"])

    def test_allowlist_is_the_measured_free_tier_snapshot(self):
        """実測（2026-10・公式 freeTier フィルタ）で 15 件。$0 価格モデルとは一致しない。"""
        ids = S.PROVIDER_SPECS["vercel"]["free_model_ids"]
        self.assertEqual(len(ids), 15)
        for expected in ("openai/gpt-oss-120b", "google/gemini-2.5-flash-lite",
                         "alibaba/qwen3.7-flash", "stepfun/step-3.7-flash",
                         "poolside/laguna-s-2.1-free", "nvidia/nemotron-3-ultra-550b-a55b",
                         "xiaomi/mimo-v2.5", "inclusionai/ling-3.1-flash-free"):
            self.assertIn(expected, ids)

    def setUp(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("vercel", None)

    def test_free_tier_flag_is_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_VERCEL_FREE_TIER": "0"}, clear=False):
            self.assertFalse(S.provider_ready("vercel"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_VERCEL_FREE_TIER": "1"}, clear=False):
            self.assertTrue(S.provider_ready("vercel"))

    def test_only_allowlisted_vercel_models_are_free(self):
        """有料価格のモデルでも許可リストにあるものは Free（クレジット対象）として扱う。"""
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}):
            self.assertTrue(S._is_free("vercel", {"id": "openai/gpt-oss-120b",
                                                  "pricing": {"input": "0.0000001", "output": "0.0000005"}}))
            self.assertTrue(S._is_free("vercel", {"id": "poolside/laguna-s-2.1-free"}))
            # カタログ 406 件の大半は Free Tier 対象外 → 許可しない
            self.assertFalse(S._is_free("vercel", {"id": "anthropic/claude-sonnet-4.6"}))
            self.assertFalse(S._is_free("vercel", {"id": "openai/gpt-5.2-pro",
                                                   "pricing": {"input": "0", "output": "0"}}))

    def test_model_listing_is_not_called_without_free_tier_flag(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_VERCEL_FREE_TIER": ""}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("vercel", ttl=0), [])
            http.assert_not_called()

    def test_models_and_chat_endpoints_are_openai_compatible(self):
        models = {"data": [{"id": "openai/gpt-oss-120b", "object": "model"},
                           {"id": "anthropic/claude-sonnet-4.6", "object": "model"}]}
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_VERCEL_FREE_TIER": "1"}, clear=False), \
             mock.patch.object(S, "provider_http", return_value=models) as http:
            got = S.fetch_provider_models("vercel", ttl=0)
        self.assertEqual(http.call_args.args[0], "/models")
        self.assertEqual([row["id"] for row in got],
                         ["openai/gpt-oss-120b", "anthropic/claude-sonnet-4.6"])
        self.assertTrue(got[0]["free"])
        self.assertFalse(got[1]["free"])
        completion = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.object(S, "provider_http", return_value=completion) as chat:
            result = S._call_once("vercel", "openai/gpt-oss-120b", "q", "system", 8, None)
        self.assertEqual(chat.call_args.args[0], "/chat/completions")
        self.assertEqual(result["text"], "hello")

    def test_explicit_paid_or_unlisted_vercel_model_is_rejected(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"vercel": VERCEL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_VERCEL_FREE_TIER": "1"}, clear=False), \
             mock.patch.object(S, "provider_http") as http, \
             mock.patch.object(S, "observe_call"):
            result = S.call_model("vercel/anthropic/claude-sonnet-4.6", "q", allow_fallback=False)
        self.assertIn("error", result)
        self.assertIn("Free", result["error"])
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
