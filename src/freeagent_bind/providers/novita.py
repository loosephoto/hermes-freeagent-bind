"""novita — Novita AI。新規アカウントの無料クレジット（$0.5〜$1 程度・期間限定）で試せます。一覧は未認証でも 200（実測 95 件）。クレジットは**使い（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_NOVITA_BASE_URL", "https://api.novita.ai/v3/openai"),
        "key": os.environ.get("NOVITA_API_KEY", ""),
        "key_env": "NOVITA_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_NOVITA_FREE_CREDITS",), "catalog_requires_ready": True,
        "note": "Novita AI。新規アカウントの無料クレジット（$0.5〜$1 程度・期間限定）で試せます。"
                "一覧は未認証でも 200（実測 95 件）。クレジットは**使い切ると課金**なので、"
                "無料クレジットで使う場合だけ FREEAGENT_NOVITA_FREE_CREDITS=1 を設定してください",
    }
