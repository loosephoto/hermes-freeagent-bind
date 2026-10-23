"""ModelScope API-Inference free-quota provider contracts (offline)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


MODELSCOPE_SPEC = {
    "base_url": "https://api-inference.modelscope.cn/v1",
    "key": "test-key",
    "key_env": "MODELSCOPE_API_KEY",
    "free_kind": "credit",
    "required_env_flags": ("FREEAGENT_MODELSCOPE_FREE_PLAN",),
    "catalog_requires_ready": True,
    "note": "ModelScope free-quota acknowledgement required",
}


class TestModelScopeFreeQuota(unittest.TestCase):
    def test_modelscope_provider_is_registered_by_default(self):
        self.assertIn("modelscope", S.PROVIDER_SPECS)
        self.assertIn("modelscope", S.PROVIDER_ORDER)
        spec = S.PROVIDER_SPECS["modelscope"]
        self.assertEqual(spec["key_env"], "MODELSCOPE_API_KEY")
        self.assertEqual(spec["base_url"], "https://api-inference.modelscope.cn/v1")
        # 無料枠は日次で回復するが、一覧は pricing を持たない（実測）→ クレジット枠として扱う。
        self.assertEqual(spec["free_kind"], "credit")
        self.assertNotIn("free_model_ids", spec)

    def _restore_state_dir(self):
        if self._old_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old_state

    def setUp(self):
        self._tmp_state = tempfile.mkdtemp(prefix="fa-provider-")
        self.addCleanup(lambda: shutil.rmtree(self._tmp_state, ignore_errors=True))
        self._old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp_state
        self.addCleanup(self._restore_state_dir)
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("modelscope", None)

    def test_free_quota_confirmation_is_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"modelscope": MODELSCOPE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MODELSCOPE_FREE_PLAN": ""}, clear=False):
            self.assertFalse(S.provider_ready("modelscope"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"modelscope": MODELSCOPE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MODELSCOPE_FREE_PLAN": "ON"}, clear=False):
            self.assertTrue(S.provider_ready("modelscope"))

    def test_missing_key_is_reported_with_the_sdk_alias(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"modelscope": dict(MODELSCOPE_SPEC, key="")}):
            self.assertEqual(S._provider_missing_settings(S.PROVIDER_SPECS["modelscope"])[0],
                             "MODELSCOPE_API_KEY")
        self.assertIn("MODELSCOPE_API_TOKEN", S.PROVIDER_SPECS["modelscope"]["note"])

    def test_catalog_envelope_is_parsed_as_openai_style_data(self):
        """ModelScope の一覧は OpenAI 互換の `{"data": [...]}`（実測・未認証でも 200）。"""
        with mock.patch.dict(S.PROVIDER_SPECS, {"modelscope": MODELSCOPE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MODELSCOPE_FREE_PLAN": "1"}, clear=False), \
             mock.patch.object(S, "provider_http",
                               return_value={"data": [{"id": "Qwen/Qwen3.8-Flash-Next"},
                                                      {"id": "ZhipuAI/GLM-4.7-Flash"}]}) as http:
            rows = S.fetch_provider_models("modelscope", ttl=0)
        self.assertEqual([r["id"] for r in rows],
                         ["Qwen/Qwen3.8-Flash-Next", "ZhipuAI/GLM-4.7-Flash"])
        self.assertTrue(all(r["free"] for r in rows))
        http.assert_called_once()
        self.assertEqual(http.call_args.kwargs.get("provider"), "modelscope")

    def test_unconfirmed_free_quota_does_not_trigger_catalog_request(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"modelscope": MODELSCOPE_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MODELSCOPE_FREE_PLAN": "0"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("modelscope", ttl=0), [])
            http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
