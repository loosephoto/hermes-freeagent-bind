"""longcat — Meituan LongCat（美团）。公開ベータの無料枠（公称 50 万トークン/日）があります。一覧は未認証で 401（実測）。無料枠は予告なく変わるため確（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_LONGCAT_BASE_URL", "https://api.longcat.chat/openai/v1"),
        "key": os.environ.get("LONGCAT_API_KEY", ""),
        "key_env": "LONGCAT_API_KEY", "free_kind": "credit", "always_ready": False,
        "required_env_flags": ("FREEAGENT_LONGCAT_FREE_PLAN",), "catalog_requires_ready": True,
        "note": "Meituan LongCat（美团）。公開ベータの無料枠（公称 50 万トークン/日）があります。"
                "一覧は未認証で 401（実測）。無料枠は予告なく変わるため確認変数 "
                "FREEAGENT_LONGCAT_FREE_PLAN=1 を必須にし、対象は `probe` で確認します",
    }
