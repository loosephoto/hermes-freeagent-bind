"""openrouter — OpenRouter の :free SKU（一覧は未認証でも取得可・推論はキー必須）（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key": os.environ.get("OPENROUTER_API_KEY", ""),
        "key_env": "OPENROUTER_API_KEY", "free_kind": "pricing", "always_ready": False,
        "note": "OpenRouter の :free SKU（一覧は未認証でも取得可・推論はキー必須）",
    }
