"""cerebras — Cerebras Inference。Free trial（$5 クレジット・カード登録・30 日）＋無料枠のレート制限。一覧はキー必須（実測: 未認証 401（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
        "key": os.environ.get("CEREBRAS_API_KEY", ""),
        "key_env": "CEREBRAS_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_CEREBRAS_FREE_TRIAL",), "catalog_requires_ready": True,
        "note": "Cerebras Inference。Free trial（$5 クレジット・カード登録・30 日）＋無料枠のレート制限。"
                "一覧はキー必須（実測: 未認証 401 Wrong API Key）。クレジットは 30 日で失効し、"
                "その後の従量課金は有料です。確認変数 FREEAGENT_CEREBRAS_FREE_TRIAL=1 が必須",
    }
