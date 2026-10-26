"""sambanova — SambaNova Cloud。**支払い方法を登録していないアカウントが Free tier**（公式レート制限表・実測で一覧は未認証でも 200）。Free（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1"),
        "key": os.environ.get("SAMBANOVA_API_KEY", ""),
        "key_env": "SAMBANOVA_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_SAMBANOVA_FREE_TIER",), "catalog_requires_ready": True,
        "note": "SambaNova Cloud。**支払い方法を登録していないアカウントが Free tier**（公式レート制限表・"
                "実測で一覧は未認証でも 200）。Free と Developer の区別は API が返さないため、"
                "確認変数 FREEAGENT_SAMBANOVA_FREE_TIER=1 が必須。カードを登録したら解除してください",
    }
