"""nous — hermes proxy（Nous Portal の資格情報を代理付与・キー不要）（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_BASE_URL", "http://127.0.0.1:8645/v1"),
        "key": os.environ.get("FREEAGENT_API_KEY", "proxy-attaches-real-credentials"),
        "key_env": None, "free_kind": "pricing", "always_ready": True,
        "note": "hermes proxy（Nous Portal の資格情報を代理付与・キー不要）",
    }
