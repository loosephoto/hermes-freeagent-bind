"""mistral — Mistral La Plateforme。Experiment plan（無料・月 約10億トークン・電話確認）の確認が必須（FREEAGENT_MISTRA（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_MISTRAL_BASE_URL", "https://api.mistral.ai/v1"),
        "key": os.environ.get("MISTRAL_API_KEY", ""),
        "key_env": "MISTRAL_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_MISTRAL_FREE_PLAN",), "catalog_requires_ready": True,
        "note": "Mistral La Plateforme。Experiment plan（無料・月 約10億トークン・電話確認）の確認が必須"
                "（FREEAGENT_MISTRAL_FREE_PLAN=1）。API は Free/paid を返さないのでクレジット枠として扱い、"
                "対象は `probe` で確認します。RPM が低いので並列 fan-out より長文・少数リクエスト向きです。"
                "有料プランへ変更したらこのフラグを外してください",
    }
