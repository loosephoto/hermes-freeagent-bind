"""groq — Groq Free tier確認が必須（FREEAGENT_GROQ_FREE_TIER=1）。Free対象モデルだけ許可。Developer tierは有料で（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "key": os.environ.get("GROQ_API_KEY", ""),
        "key_env": "GROQ_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("openai/gpt-oss-120b", "openai/gpt-oss-20b",
                            "qwen/qwen3.8-27b"),
        "required_env_flags": ("FREEAGENT_GROQ_FREE_TIER",), "catalog_requires_ready": True,
        "note": "Groq Free tier確認が必須（FREEAGENT_GROQ_FREE_TIER=1）。Free対象モデルだけ許可。Developer tierは有料です",
    }
