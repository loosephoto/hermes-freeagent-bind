"""gemini — Gemini Free tierと非機密データ利用の両確認が必須。Unpaid prompts/outputs may be reviewed and used（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_GEMINI_BASE_URL",
                                  "https://generativelanguage.googleapis.com/v1beta/openai"),
        "key": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
        "key_env": "GEMINI_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("gemini-3.8-flash", "gemini-3.7-flash"),
        "required_env_flags": ("FREEAGENT_GEMINI_FREE_TIER", "FREEAGENT_GEMINI_UNPAID_DATA_ACK"),
        "catalog_requires_ready": True,
        "note": "Gemini Free tierと非機密データ利用の両確認が必須。Unpaid prompts/outputs may be reviewed and used to improve Google products; OpenAI compatibility is Beta",
    }
