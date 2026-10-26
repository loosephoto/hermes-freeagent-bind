"""siliconflow — SiliconFlow（硅基流动）。**無料モデル**（レート制限つき）と有料モデルが同居し、一覧は未認証で 401（実測）。Free/paid を API が（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_SILICONFLOW_BASE_URL", "https://api.siliconflow.com/v1"),
        "key": os.environ.get("SILICONFLOW_API_KEY", ""),
        "key_env": "SILICONFLOW_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_SILICONFLOW_FREE_TIER",), "catalog_requires_ready": True,
        "note": "SiliconFlow（硅基流动）。**無料モデル**（レート制限つき）と有料モデルが同居し、一覧は"
                "未認証で 401（実測）。Free/paid を API が返さないので確認変数 "
                "FREEAGENT_SILICONFLOW_FREE_TIER=1 が必須で、対象は `probe` で確認します。"
                "中国版は FREEAGENT_SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1",
    }
