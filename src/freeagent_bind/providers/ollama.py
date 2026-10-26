"""ollama — Ollama Cloud Free plan確認が必須（FREEAGENT_OLLAMA_FREE_PLAN=1）。無料は starter モデルだけで範囲は非（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_OLLAMA_BASE_URL", "https://ollama.com/v1"),
        "key": os.environ.get("OLLAMA_API_KEY", ""),
        "key_env": "OLLAMA_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_OLLAMA_FREE_PLAN",), "catalog_requires_ready": True,
        "note": "Ollama Cloud Free plan確認が必須（FREEAGENT_OLLAMA_FREE_PLAN=1）。無料は starter モデルだけで"
                "範囲は非公開 → probe で確認。同時 1 リクエスト。クレジットを購入すると全モデルが使えますが課金対象です",
    }
