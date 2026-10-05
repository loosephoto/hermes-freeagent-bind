"""Ollama Cloud free-plan provider contracts (offline)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


OLLAMA_SPEC = {
    "base_url": "https://ollama.com/v1",
    "key": "test-key",
    "key_env": "OLLAMA_API_KEY",
    "free_kind": "credit",
    "required_env_flags": ("FREEAGENT_OLLAMA_FREE_PLAN",),
    "catalog_requires_ready": True,
    "note": "Ollama Cloud free plan acknowledgement required",
}


class TestOllamaFreePlan(unittest.TestCase):
    def test_ollama_provider_is_registered_by_default(self):
        self.assertIn("ollama", S.PROVIDER_SPECS)
        self.assertIn("ollama", S.PROVIDER_ORDER)
        self.assertEqual(S.PROVIDER_SPECS["ollama"]["key_env"], "OLLAMA_API_KEY")
        self.assertEqual(S.PROVIDER_SPECS["ollama"]["base_url"], "https://ollama.com/v1")
        # starter の範囲は非公開なので許可リストを持たない（クレジット枠として扱う）
        self.assertEqual(S.PROVIDER_SPECS["ollama"]["free_kind"], "credit")
        self.assertNotIn("free_model_ids", S.PROVIDER_SPECS["ollama"])

    def setUp(self):
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("ollama", None)

    def test_free_plan_flag_is_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_OLLAMA_FREE_PLAN": "0"}, clear=False):
            self.assertFalse(S.provider_ready("ollama"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_OLLAMA_FREE_PLAN": "true"}, clear=False):
            self.assertTrue(S.provider_ready("ollama"))

    def test_credit_kind_marks_catalog_rows_free(self):
        """一覧 17 件は pricing を持たない（実測）→ credit は全件を無料枠の候補として扱う。"""
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}):
            self.assertTrue(S._is_free("ollama", {"id": "gemma4:31b"}))
            self.assertTrue(S._is_free("ollama", {"id": "gpt-oss:120b", "object": "model"}))
            self.assertTrue(S._is_free("ollama", {"id": "nemotron-3-ultra"}))

    def test_model_listing_is_not_called_without_free_plan_flag(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_OLLAMA_FREE_PLAN": ""}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("ollama", ttl=0), [])
            http.assert_not_called()

    def test_models_and_chat_endpoints_are_openai_compatible(self):
        models = {"data": [{"id": "gemma4:31b", "object": "model"},
                           {"id": "gpt-oss:120b", "object": "model"}]}
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_OLLAMA_FREE_PLAN": "1"}, clear=False), \
             mock.patch.object(S, "provider_http", return_value=models) as http:
            got = S.fetch_provider_models("ollama", ttl=0)
        self.assertEqual(http.call_args.args[0], "/models")
        self.assertEqual([row["id"] for row in got], ["gemma4:31b", "gpt-oss:120b"])
        self.assertTrue(all(row["free"] for row in got))
        completion = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
                      "usage": {"prompt_tokens": 1, "completion_tokens": 1}}
        with mock.patch.dict(S.PROVIDER_SPECS, {"ollama": OLLAMA_SPEC}), \
             mock.patch.object(S, "provider_http", return_value=completion) as chat:
            result = S._call_once("ollama", "gemma4:31b", "q", "system", 8, None)
        self.assertEqual(chat.call_args.args[0], "/chat/completions")
        self.assertEqual(result["text"], "hello")


if __name__ == "__main__":
    unittest.main()
