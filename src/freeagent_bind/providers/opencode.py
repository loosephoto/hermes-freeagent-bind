"""opencode — opencode Zen。**-free 接尾辞のモデルだけ**を許可リストで許可（一覧に pricing が無いため・実測 40 件中 8 件）。キーは OP（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_OPENCODE_BASE_URL", "https://opencode.ai/zen/v1"),
        "key": os.environ.get("OPENCODE_ZEN_API_KEY") or os.environ.get("OPENCODE_GO_API_KEY", ""),
        "key_env": "OPENCODE_ZEN_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("deepseek-v4.1-flash-free", "glm-5.3-flash-free", "beam-free",
                           "flint-40b-free", "mimo-v2.6-flash-free", "jev-1.13-free",
                           "nemotron-3.5-lightning-free", "nemotron-3.5-super-vl-free"),
        "required_env_flags": ("FREEAGENT_OPENCODE_FREE_TIER",), "catalog_requires_ready": True,
        "note": "opencode Zen。**-free 接尾辞のモデルだけ**を許可リストで許可（一覧に pricing が無いため・"
                "実測 40 件中 8 件）。キーは OPENCODE_ZEN_API_KEY（別名 OPENCODE_GO_API_KEY）。"
                "実測: 一覧 200 でも推論は 403 Model access is disabled になるキーがある"
                "（キーの権利の問題で、無料モデルも同じ）。確認変数 FREEAGENT_OPENCODE_FREE_TIER=1 が必須",
    }
