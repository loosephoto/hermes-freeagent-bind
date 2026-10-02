"""Cloudflare Workers AI provider contracts (offline)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


CLOUDFLARE_SPEC = {
    "base_url": "https://api.cloudflare.com/client/v4/accounts/" + "a" * 32 + "/ai",
    "key": "test-token",
    "key_env": "CLOUDFLARE_API_TOKEN",
    "free_kind": "allowlist",
    "free_model_ids": ("@cf/openai/gpt-oss-20b", "@cf/zai-org/glm-4.7-flash"),
    "required_env_flags": ("FREEAGENT_CLOUDFLARE_FREE_PLAN",),
    "required_env_values": ("CLOUDFLARE_ACCOUNT_ID",),
    "catalog_requires_ready": True,
    "models_path": "/models/search?format=openrouter&hide_experimental=true&per_page=100",
    "chat_path": "/v1/chat/completions",
    "catalog_format": "cloudflare_openrouter",
    "note": "Cloudflare Workers Free only",
}


class TestCloudflareWorkersAI(unittest.TestCase):
    def setUp(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("cloudflare", None)

    def test_cloudflare_requires_account_token_and_free_plan_ack(self):
        env = {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
               "CLOUDFLARE_ACCOUNT_ID": "a" * 32}
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, env, clear=False):
            self.assertTrue(S.provider_ready("cloudflare"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "0",
                                          "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False):
            self.assertFalse(S.provider_ready("cloudflare"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1"}, clear=True):
            self.assertFalse(S.provider_ready("cloudflare"))

    def test_catalog_uses_account_model_search_and_accepts_openrouter_format(self):
        rows = [
            {"id": "@cf/openai/gpt-oss-20b", "context_length": 131072,
             "pricing": {"prompt": "0.20", "completion": "0.30"}},
            {"id": "@cf/zai-org/glm-5.2", "context_length": 262144,
             "pricing": {"prompt": "1.40", "completion": "4.40"}},
        ]
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
                                          "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False), \
             mock.patch.object(S, "provider_http", return_value={"success": True,
                                                                    "result": {"data": rows}}) as http:
            got = S.fetch_provider_models("cloudflare", ttl=0)
        self.assertEqual(http.call_args.args[0], CLOUDFLARE_SPEC["models_path"])
        self.assertEqual([row["id"] for row in got], ["@cf/openai/gpt-oss-20b", "@cf/zai-org/glm-5.2"])
        self.assertTrue(got[0]["free"])
        self.assertFalse(got[1]["free"], "paid-only / not-allowlisted models must never be Free")
        self.assertEqual(got[0]["context_length"], 131072)

    def test_top_level_marketplace_data_shape_is_supported(self):
        rows = [{"id": "@cf/openai/gpt-oss-20b", "pricing": {"prompt": "0.2"}}]
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
                                          "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False), \
             mock.patch.object(S, "provider_http", return_value={"success": True, "data": rows}):
            got = S.fetch_provider_models("cloudflare", ttl=0)
        self.assertEqual([row["id"] for row in got], ["@cf/openai/gpt-oss-20b"])

    def test_cloudflare_chat_uses_account_scoped_openai_compatible_path(self):
        response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.object(S, "provider_http", return_value=response) as http:
            got = S._call_once("cloudflare", "@cf/openai/gpt-oss-20b", "q", "", 8, None)
        self.assertEqual(http.call_args.args[0], "/v1/chat/completions")
        self.assertEqual(got["text"], "ok")

    def test_paid_or_unknown_explicit_model_is_rejected_before_http(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
                                          "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False), \
             mock.patch.object(S, "provider_http") as http, \
             mock.patch.object(S, "observe_call"):
            got = S.call_model("cloudflare/@cf/zai-org/glm-5.2", "q", allow_fallback=False)
        self.assertIn("error", got)
        self.assertIn("Free", got["error"])
        http.assert_not_called()

    def test_unconfirmed_plan_never_reads_catalog(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "0",
                                          "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("cloudflare", ttl=0), [])
            http.assert_not_called()


    def test_official_marketplace_response_shape_is_supported(self):
        rows = [{"id": "@cf/openai/gpt-oss-20b", "pricing": {"prompt": "0.2"}}]
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}):
            with mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
                                             "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False):
                with mock.patch.object(S, "provider_http", return_value={"data": rows}):
                    got = S.fetch_provider_models("cloudflare", ttl=0)
        self.assertEqual([row["id"] for row in got], ["@cf/openai/gpt-oss-20b"])

    def test_unsuccessful_catalog_envelope_is_not_a_successful_empty_list(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"cloudflare": CLOUDFLARE_SPEC}):
            with mock.patch.dict(os.environ, {"FREEAGENT_CLOUDFLARE_FREE_PLAN": "1",
                                             "CLOUDFLARE_ACCOUNT_ID": "a" * 32}, clear=False):
                with mock.patch.object(S, "provider_http", return_value={"success": False, "result": []}):
                    self.assertEqual(S.fetch_provider_models("cloudflare", ttl=0), [])
        self.assertIn("error", S._MODELS_CACHE["cloudflare"])


if __name__ == "__main__":
    unittest.main()
