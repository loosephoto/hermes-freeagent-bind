"""HF（Hugging Face Inference Providers）が OpenRouter のルータになった件の契約（オフライン）.

2026-11-16 に HF のチャット推論は OpenRouter のルータへ置き換わった。実測（2026-11-17）は
「一覧 164 件・`is_free` は全件 false・推論は全件 402（前払い/PRO クレジットが必要）」。
ここでは**ネットワークを出さずに**、その状態での判定・選抜・表示の契約だけを固定する:

  1. Free の提供元が無い HF は Free 在庫に入らない（既定）
  2. `FREEAGENT_HF_CREDITS=1` でも、アカウントに課金手段が無ければ在庫に入らない
  3. HF は既定で `PROVIDER_ORDER` に載らない（`hidden_providers` に理由つきで残る）
  4. 402 はモデル単位ではなく**プロバイダ単位**で覚える（同じ 402 を繰り返さない）
  5. OpenRouter の `hugging_face_id` が同じモデルは独立意見の identity を共有する
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
from freeagent_bind import server as S  # noqa: E402

HF_ROW_PAID = {"id": "a/b", "providers": [
    {"provider": "novita", "status": "live", "is_free": False, "pricing": {"input": 0.4, "output": 3}}]}
HF_ROW_FREE = {"id": "a/b", "providers": [
    {"provider": "novita", "status": "live", "is_free": True, "pricing": {"input": 0, "output": 0}}]}


class _HFTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp_state = tempfile.mkdtemp(prefix="fa-hf-")
        self.addCleanup(lambda: shutil.rmtree(self._tmp_state, ignore_errors=True))
        self._old_state = os.environ.get("FREEAGENT_STATE_DIR")
        os.environ["FREEAGENT_STATE_DIR"] = self._tmp_state
        self.addCleanup(self._restore)

        self._spec = dict(S.PROVIDER_SPECS["huggingface"])
        self.addCleanup(lambda: S.PROVIDER_SPECS.__setitem__("huggingface", self._spec))
        self._account = dict(S._HF_ACCOUNT)
        self.addCleanup(lambda: (S._HF_ACCOUNT.clear(), S._HF_ACCOUNT.update(self._account)))
        self._models = dict(S._MODELS_CACHE)
        self.addCleanup(lambda: (S._MODELS_CACHE.clear(), S._MODELS_CACHE.update(self._models)))
        self._credit = dict(S._CREDIT)
        self.addCleanup(lambda: (S._CREDIT.clear(), S._CREDIT.update(self._credit)))

    def _restore(self):
        if self._old_state is None:
            os.environ.pop("FREEAGENT_STATE_DIR", None)
        else:
            os.environ["FREEAGENT_STATE_DIR"] = self._old_state

    def _set_account(self, checked=True, is_pro=False, can_pay=False, error=None):
        S._HF_ACCOUNT.clear()
        S._HF_ACCOUNT.update({"checked": checked, "is_pro": is_pro, "can_pay": can_pay,
                              "at": S.now_ts(), "error": error})

    def _with_token(self):
        S.PROVIDER_SPECS["huggingface"] = dict(self._spec, key="hf_test")


class TestHFCreditsGate(_HFTestBase):
    def test_paid_routes_are_not_free_without_opt_in(self):
        """既定（申告なし）は Free の提供元が無いので Free に入らない。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FREEAGENT_HF_CREDITS", None)
            self.assertFalse(S._is_free("huggingface", HF_ROW_PAID))

    def test_provider_free_route_still_wins(self):
        """提供元の Free が戻れば従来どおり拾う（将来 is_free が復活しても壊れない）。"""
        with mock.patch.dict(os.environ, {"FREEAGENT_HF_CREDITS": "0"}):
            self.assertTrue(S._is_free("huggingface", HF_ROW_FREE))

    def test_opt_in_without_token_is_not_free(self):
        S.PROVIDER_SPECS["huggingface"] = dict(self._spec, key="")
        self._set_account(is_pro=True)
        with mock.patch.dict(os.environ, {"FREEAGENT_HF_CREDITS": "1"}):
            self.assertFalse(S._is_free("huggingface", HF_ROW_PAID))
            self.assertFalse(S.hf_credits_active())

    def test_opt_in_without_payment_method_is_not_free(self):
        """whoami が「PRO でも支払い手段でもない」と言ったら 402 にしかならない（実測のアカウント）。"""
        self._with_token()
        self._set_account(is_pro=False, can_pay=False)
        with mock.patch.dict(os.environ, {"FREEAGENT_HF_CREDITS": "1"}):
            self.assertFalse(S._is_free("huggingface", HF_ROW_PAID))

    def test_opt_in_with_pro_or_payment_method_is_free(self):
        self._with_token()
        for state in ({"is_pro": True, "can_pay": False}, {"is_pro": False, "can_pay": True}):
            self._set_account(**state)
            with mock.patch.dict(os.environ, {"FREEAGENT_HF_CREDITS": "1"}):
                self.assertTrue(S._is_free("huggingface", HF_ROW_PAID), state)

    def test_whoami_failure_trusts_the_declaration(self):
        """規約 21: 不通で利用者の設定を無効化しない（whoami が落ちたら申告だけを信じる）。"""
        self._with_token()
        self._set_account(checked=False, error="URLError")
        with mock.patch.dict(os.environ, {"FREEAGENT_HF_CREDITS": "1"}):
            self.assertTrue(S._is_free("huggingface", HF_ROW_PAID))

    def test_whoami_is_not_called_without_the_flag(self):
        """既定で誰も呼ばない（起動・一覧のたびに whoami を叩かない）。"""
        self._with_token()
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.object(S, "_urlopen", side_effect=AssertionError("HTTP を出した")):
            os.environ.pop("FREEAGENT_HF_CREDITS", None)
            S._HF_ACCOUNT.clear()
            self.assertFalse(S._is_free("huggingface", HF_ROW_PAID))


class TestHFHiddenByDefault(_HFTestBase):
    def test_spec_marks_hf_as_opt_in(self):
        spec = S.PROVIDER_SPECS["huggingface"]
        self.assertEqual(spec.get("opt_in_env"), "FREEAGENT_HF_CREDITS")
        self.assertEqual(spec.get("free_kind"), "hf_credits")
        self.assertTrue(spec.get("hf_router"), "分類名ではなく印で HF を判定する")

    def test_hidden_from_default_order_but_reported(self):
        self.assertNotIn("huggingface", S.PROVIDER_ORDER)
        hidden = {row["provider"]: row for row in S.hidden_providers()}
        self.assertIn("huggingface", hidden)
        self.assertEqual(hidden["huggingface"]["opt_in_env"], "FREEAGENT_HF_CREDITS")
        self.assertTrue(hidden["huggingface"]["reason"])

    def test_rendered_line_explains_how_to_enable(self):
        text = S.render("freeagent_models", {
            "providers": [], "free_candidates": 0, "usable_now": 0, "total_models": 0,
            "default_model": "", "hidden_providers": S.hidden_providers(),
            "credit_blocked": {}, "non_chat_candidates": 0,
        })
        self.assertIn("既定は非表示", text)
        self.assertIn("FREEAGENT_HF_CREDITS=1", text)


class TestIdentityUnification(_HFTestBase):
    def _seed_openrouter(self, rows):
        S._MODELS_CACHE["openrouter"] = {"at": S.now_ts(), "rows": rows}

    def test_same_hub_model_is_the_same_identity(self):
        """HF と OpenRouter が同じ重みを配信するとき、独立した 2 意見に数えない。"""
        self._seed_openrouter([
            {"id": "liquid/lfm-3-1.7b:free", "provider": "openrouter", "free": True,
             "hf_id": "LiquidAI/LFM3-1.7B"}])
        self.assertEqual(S._model_identity("openrouter/liquid/lfm-3-1.7b:free"),
                         S._model_identity("huggingface/LiquidAI/LFM3-1.7B"))
        self.assertEqual(S._model_identity("huggingface/LiquidAI/LFM3-1.7B:cheapest"),
                         S._model_identity("huggingface/LiquidAI/LFM3-1.7B:akashml"))

    def test_unknown_hub_id_keeps_refs_apart(self):
        self._seed_openrouter([{"id": "x/y:free", "provider": "openrouter", "hf_id": ""}])
        self.assertNotEqual(S._model_identity("openrouter/x/y:free"),
                            S._model_identity("huggingface/Other/Model"))

    def test_non_router_providers_are_untouched(self):
        self._seed_openrouter([])
        self.assertEqual(S._model_identity("nous/x/y:free"), "nous/x/y:free")
        self.assertEqual(S._model_identity("nvidia/a/b"), "nvidia/a/b")


class TestCreditBlockIsProviderLevel(_HFTestBase):
    def test_402_blocks_the_provider_not_just_the_model(self):
        S.note_provider_credit("huggingface", 402, "Purchase pre-paid credits")
        self.assertIsNotNone(S.provider_credit_blocked("huggingface"))
        self.assertIsNone(S.provider_credit_blocked("openrouter"))

    def test_block_keeps_the_longer_deadline(self):
        first = S.note_provider_credit("huggingface", 402, "a")
        second = S.note_provider_credit("huggingface", 402, "b")
        self.assertGreaterEqual(second, first)

    def test_expired_block_is_ignored(self):
        S.note_provider_credit("huggingface", 402, "x")
        with mock.patch.object(S, "now_ts", lambda: S.time.time() + S.CREDIT_TTL_S + 10):
            self.assertIsNone(S.provider_credit_blocked("huggingface"))

    def test_success_clears_the_block(self):
        S.note_provider_credit("huggingface", 402, "x")
        S.clear_provider_credit("huggingface")
        self.assertIsNone(S.provider_credit_blocked("huggingface"))

    def test_fallbacks_skip_blocked_providers(self):
        S.note_provider_credit("huggingface", 402, "x")
        refs = ["nous/a", "huggingface/b", "openrouter/c"]
        with mock.patch.object(S, "free_model_refs",
                               lambda free_only=True, chat_only=False: list(refs)), \
                mock.patch.object(S, "provider_auth_blocked", lambda p: None), \
                mock.patch.object(S, "cooling_refs", lambda: {}), \
                mock.patch.object(S, "env_excluded_models", lambda: []):
            got = S._candidates("nous/a")
        self.assertIn("openrouter/c", got)
        self.assertNotIn("huggingface/b", got, "クレジット枯渇中の提供元へフォールバックしない")


class TestErrorHints(_HFTestBase):
    def test_402_points_at_the_hf_account_not_the_key(self):
        hint = S._auth_hint("huggingface", 402,
                            '{"error":"Purchase pre-paid credits to continue using Inference Providers."}')
        self.assertIn("クレジット", hint)
        self.assertIn("PRO", hint)
        self.assertIn("FREEAGENT_HF_CREDITS", hint)

    def test_400_provider_mismatch_explains_suffixes(self):
        hint = S._auth_hint("huggingface", 400, '{"error":{"message":"The requested model \'a/b\''
                                                 ' is not supported by provider \'free\'.","code":'
                                                 '"model_not_supported"}}')
        self.assertIn(":fastest", hint)
        self.assertIn("`:free`", hint)


if __name__ == "__main__":
    unittest.main()
