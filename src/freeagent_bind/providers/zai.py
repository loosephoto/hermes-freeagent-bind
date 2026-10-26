"""zai — Z.ai（Zhipu GLM）。公式価格表で $0 の GLM-4.7-Flash / 4.5-Flash / 4.6V-Flash だけを許可（一覧 API （server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_ZAI_BASE_URL", "https://api.z.ai/api/paas/v4"),
        "key": (os.environ.get("GLM_API_KEY") or os.environ.get("ZAI_API_KEY")
                or os.environ.get("ZHIPUAI_API_KEY") or ""),
        "key_env": "GLM_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("glm-4.7-flash", "glm-4.5-flash", "glm-4.6v-flash"),
        # 一覧が Free ID を返さない提供元なので、許可リストを在庫として補う（下の関数）。
        "synthesize_free_ids": True,
        "required_env_flags": ("FREEAGENT_ZAI_FREE_TIER",), "catalog_requires_ready": True,
        "note": "Z.ai（Zhipu GLM）。公式価格表で $0 の GLM-4.7-Flash / 4.5-Flash / 4.6V-Flash だけを許可"
                "（一覧 API はこの 3 件を返さないため許可リストから在庫を補います）。Free tier確認が必須"
                "（FREEAGENT_ZAI_FREE_TIER=1）。キーは GLM_API_KEY（別名 ZAI_API_KEY / ZHIPUAI_API_KEY）。"
                "有料モデルは残高が無いと 429 を返します。Flash 系は**思考モデル**なので max_tokens に余裕を"
                "（実測: glm-4.5-flash は 256 で思考 202 トークンの後に本文。64 では本文が空になり、"
                "サーバーの空応答リトライが上限を引き上げます）",
    }
