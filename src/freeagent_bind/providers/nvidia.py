"""nvidia — NVIDIA NIM（無料クレジット枠。一覧には廃止・未有効のモデルが混在 → probe で確認）（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "key": os.environ.get("NVIDIA_API_KEY", ""),
        "key_env": "NVIDIA_API_KEY", "free_kind": "credit", "always_ready": False,
        # 実測: 一覧 82 件の大半が 410（EOL）や 404（アカウントで未有効）で、**一覧を信じると呼べない**。
        # `freeagent_models` の `probe: true` で生存確認してから使うこと。
        "note": "NVIDIA NIM（無料クレジット枠。一覧には廃止・未有効のモデルが混在 → probe で確認）",
    }
