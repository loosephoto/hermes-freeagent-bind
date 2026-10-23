"""Z.ai（Zhipu GLM）Free-tier provider contracts (offline)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S


ZAI_SPEC = {
    "base_url": "https://api.z.ai/api/paas/v4",
    "key": "test-key",
    "key_env": "GLM_API_KEY",
    "free_kind": "allowlist",
    "free_model_ids": ("glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"),
    "synthesize_free_ids": True,
    "required_env_flags": ("FREEAGENT_ZAI_FREE_TIER",),
    "catalog_requires_ready": True,
    "note": "Z.ai test provider",
}


class TestZaiFreeTier(unittest.TestCase):
    def test_zai_provider_is_registered_by_default(self):
        self.assertIn("zai", S.PROVIDER_SPECS)
        self.assertIn("zai", S.PROVIDER_ORDER)
        spec = S.PROVIDER_SPECS["zai"]
        self.assertEqual(spec["key_env"], "GLM_API_KEY")
        self.assertEqual(spec["base_url"], "https://api.z.ai/api/paas/v4")
        self.assertEqual(spec["free_kind"], "allowlist")
        self.assertEqual(spec["free_model_ids"],
                         ("glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"))
        self.assertTrue(spec["catalog_requires_ready"])
        self.assertTrue(spec["synthesize_free_ids"], "一覧が Free ID を返さない提供元")

    def _restore_state_dir(self):
        if self._old_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old_state

    def setUp(self):
        # カタログのディスクキャッシュ（§3.3）が**本番の状態ディレクトリ**へ書かないようにする。
        self._tmp_state = tempfile.mkdtemp(prefix="fa-provider-")
        self.addCleanup(lambda: shutil.rmtree(self._tmp_state, ignore_errors=True))
        self._old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp_state
        self.addCleanup(self._restore_state_dir)
        with S._MODELS_LOCK:
            S._MODELS_CACHE.pop("zai", None)

    def test_free_tier_confirmation_is_required(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_ZAI_FREE_TIER": "0"}, clear=False):
            self.assertFalse(S.provider_ready("zai"))
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_ZAI_FREE_TIER": "yes"}, clear=False):
            self.assertTrue(S.provider_ready("zai"))

    def test_missing_key_is_reported_by_the_documented_env_name(self):
        """キー欠落の案内は正式名（GLM_API_KEY）で出す。別名は説明文に残す。"""
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": dict(ZAI_SPEC, key="")}):
            self.assertEqual(S._provider_missing_settings(S.PROVIDER_SPECS["zai"])[0],
                             "GLM_API_KEY")
        for alias in ("ZAI_API_KEY", "ZHIPUAI_API_KEY"):
            self.assertIn(alias, S.PROVIDER_SPECS["zai"]["note"])

    def test_only_published_free_models_are_marked_free(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}):
            self.assertTrue(S._is_free("zai", {"id": "glm-4.7-flash"}))
            # 有料の旗艦は pricing が 0 でも Free とみなさない（許可リスト方式）。
            self.assertFalse(S._is_free("zai", {"id": "glm-5.3-flash",
                                               "pricing": {"prompt": "0", "completion": "0"}}))
            self.assertFalse(S._is_free("zai", {"id": "glm-5.3"}))

    def test_paid_or_unlisted_explicit_model_is_rejected_before_http(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_ZAI_FREE_TIER": "1"}, clear=False), \
             mock.patch.object(S, "provider_http") as http, \
             mock.patch.object(S, "observe_call"):
            result = S.call_model("zai/glm-5.3", "test", allow_fallback=False)
        self.assertIn("error", result)
        self.assertIn("Free", result["error"])
        http.assert_not_called()

    def test_unconfirmed_free_tier_does_not_trigger_catalog_request(self):
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}), \
             mock.patch.dict(os.environ, {"FREEAGENT_ZAI_FREE_TIER": "no"}, clear=False), \
             mock.patch.object(S, "provider_http") as http:
            self.assertEqual(S.fetch_provider_models("zai", ttl=0), [])
            http.assert_not_called()

    def test_allowlisted_rows_are_added_when_the_catalog_omits_them(self):
        """実測: Z.ai の一覧は有料の旗艦 11 件だけを返し、$0 の Flash を返さない。

        載っていないことを理由に Free を隠すと、その提供元は自動選抜に永久に入らない。
        """
        existing = {"id": "glm-5.3", "provider": "zai", "free": False, "chat": True}
        rows = S._with_allowlisted_rows("zai", ZAI_SPEC, [existing])
        self.assertEqual(rows[0], existing, "既存行は触らない")
        self.assertEqual([r["id"] for r in rows[1:]], list(ZAI_SPEC["free_model_ids"]))
        self.assertTrue(all(r["free"] and r["chat"] for r in rows[1:]))

        # 一覧が既に返している ID は重複させない。
        full = [{"id": model, "provider": "zai", "free": True, "chat": True}
                for model in ZAI_SPEC["free_model_ids"]]
        self.assertEqual(S._with_allowlisted_rows("zai", ZAI_SPEC, full), full)
        # 許可リストを持たない提供元には何もしない。
        self.assertEqual(S._with_allowlisted_rows("zai", {"free_kind": "credit"}, full), full)
        # フラグの無い許可リスト方式（一覧が ID を返す提供元）にも何もしない。
        self.assertEqual(S._with_allowlisted_rows("zai", dict(ZAI_SPEC, synthesize_free_ids=False), [existing]),
                         [existing])

    def test_allowlisted_inventory_reaches_the_selection_pool(self):
        rows = S._with_allowlisted_rows("zai", ZAI_SPEC, [{"id": "glm-5.3", "free": False}])
        with mock.patch.dict(S.PROVIDER_SPECS, {"zai": ZAI_SPEC}), \
             mock.patch.object(S, "PROVIDER_ORDER", ["zai"]), \
             mock.patch.dict(os.environ, {"FREEAGENT_ZAI_FREE_TIER": "1"}, clear=False), \
             mock.patch.object(S, "_catalog_prefetch"), \
             mock.patch.object(S, "_models_memory", return_value=rows):
            refs = S.free_model_refs(free_only=True, chat_only=True)
        self.assertIn("zai/glm-4.7-flash", refs)


if __name__ == "__main__":
    unittest.main()
