"""cloudflare — Workers Free plan確認が必須（FREEAGENT_CLOUDFLARE_FREE_PLAN=1）。Neuronsは10,000/日を全用途で共有（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": ("https://api.cloudflare.com/client/v4/accounts/"
                     + os.environ.get("CLOUDFLARE_ACCOUNT_ID", "") + "/ai"),
        "key": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
        "key_env": "CLOUDFLARE_API_TOKEN", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("@cf/openai/gpt-oss-20b", "@cf/zai-org/glm-4.7-flash"),
        "required_env_values": ("CLOUDFLARE_ACCOUNT_ID",),
        "required_env_patterns": {"CLOUDFLARE_ACCOUNT_ID": r"[0-9a-fA-F]{32}"},
        "required_env_flags": ("FREEAGENT_CLOUDFLARE_FREE_PLAN",), "catalog_requires_ready": True,
        "models_path": "/models/search?format=openrouter&hide_experimental=true&per_page=100",
        "chat_path": "/v1/chat/completions", "catalog_format": "cloudflare_openrouter",
        "note": "Workers Free plan確認が必須（FREEAGENT_CLOUDFLARE_FREE_PLAN=1）。Neuronsは10,000/日を全用途で共有。有料専用モデルは許可しません",
    }
