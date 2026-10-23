"""Mistral La Plateforme free-plan provider contracts (offline)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


MISTRAL_SPEC = {
    "base_url": "https://api.mistral.ai/v1",
    "key": "test-key",
    "key_env": "MISTRAL_API_KEY",
    "free_kind": "credit",
    "required_env_flags": ("FREEAGENT_MISTRAL_FREE_PLAN",),
    "catalog_requires_ready": True,
    "note": "Mistral experiment-plan acknowledgement required",
}


class TestMistralFreePlan(unittest.TestCase):
    def test_mistral_provider_is_registered_by_default(self):
        self.assertIn("mistral", S.PROVIDER_SPECS)
        self.assertIn("mistral", S.PROVIDER_ORDER)
        spec = S.PROVIDER_SPECS["mistral"]
        self.assertEqual(spec["key_env"], "MISTRAL_API_KEY")
        self.assertEqual(spec["base_url"], "https://api.mistral.ai/v1")
        # 一覧が認証必須（未認証は 401）で Free/paid を返さないため、許可リストは持たない。
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
            S._MODELS_CACHE.pop("mistral", None)

    def test_plan_confirmation_is_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MISTRAL_FREE_PLAN": "0"}, clear=False):
            self.assertFalse(S.provider_ready("mistral"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MISTRAL_FREE_PLAN": "1"}, clear=False):
            self.assertTrue(S.provider_ready("mistral"))

    def test_malformed_flag_does_not_count_as_confirmation(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MISTRAL_FREE_PLAN": "experiment"},
                             clear=False):
            self.assertFalse(S.provider_ready("mistral"))

    def test_credit_kind_marks_catalog_rows_free(self):
        """一覧が pricing を持たないので、クレジット枠は全件を候補として扱う。"""
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}):
            self.assertTrue(S._is_free("mistral", {"id": "mistral-large-latest"}))
            self.assertTrue(S._is_free("mistral", {"id": "codestral-latest",
                                                  "pricing": {"prompt": "0.3", "completion": "0.9"}}))

    def test_unconfirmed_plan_does_not_trigger_catalog_request(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_MISTRAL_FREE_PLAN": "false"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("mistral", ttl=0), [])
            http.assert_not_called()

    def test_status_explains_missing_plan_confirmation(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"mistral": MISTRAL_SPEC}), \
             mock.patch.object(S, "PROVIDER_ORDER", ["mistral"]), \
             mock.patch.dict(os.environ, {"FREEAGENT_MISTRAL_FREE_PLAN": "0"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            row = S.provider_status()[0]
        self.assertIn("FREEAGENT_MISTRAL_FREE_PLAN", row["missing_settings"])
        self.assertTrue(row["requires_activation"])
        http.assert_not_called()


if __name__ == "__main__":
    unittest.main()
