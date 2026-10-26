"""huggingface — Hugging Face Inference Providers（router。一覧は未認証でも取得可・推論はトークン必須）（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_HF_BASE_URL", "https://router.huggingface.co/v1"),
        "key": (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
                or os.environ.get("HUGGINGFACEHUB_API_TOKEN") or ""),
        "key_env": "HF_TOKEN", "free_kind": "hf_providers", "always_ready": False,
        "note": "Hugging Face Inference Providers（router。一覧は未認証でも取得可・推論はトークン必須）",
    }
