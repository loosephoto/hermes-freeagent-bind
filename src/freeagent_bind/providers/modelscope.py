"""modelscope — ModelScope API-Inference（魔搭社区）。無料枠（公称 2,000 リクエスト/日・日次回復）の確認が必須（FREEAGENT_MODELS（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_MODELSCOPE_BASE_URL",
                                  "https://api-inference.modelscope.cn/v1"),
        "key": (os.environ.get("MODELSCOPE_API_KEY") or os.environ.get("MODELSCOPE_API_TOKEN") or ""),
        "key_env": "MODELSCOPE_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_MODELSCOPE_FREE_PLAN",), "catalog_requires_ready": True,
        "note": "ModelScope API-Inference（魔搭社区）。無料枠（公称 2,000 リクエスト/日・日次回復）の確認が"
                "必須（FREEAGENT_MODELSCOPE_FREE_PLAN=1）。一覧（未認証でも 200・37 件）は pricing を持たない"
                "ためクレジット枠として扱い、対象は `probe` で確認します。Qwen / DeepSeek / GLM / Step など。"
                "キーは MODELSCOPE_API_KEY（別名 MODELSCOPE_API_TOKEN）。キー発行に実名・SMS 確認が要る場合があります",
    }
