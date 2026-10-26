"""vercel — Vercel AI GatewayのFree tier（月 $5 クレジット）確認が必須（FREEAGENT_VERCEL_FREE_TIER=1）。無料枠の対（server.py §0 から分割・v0.16.0）.

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_VERCEL_BASE_URL", "https://ai-gateway.vercel.sh/v1"),
        "key": os.environ.get("AI_GATEWAY_API_KEY", ""),
        "key_env": "AI_GATEWAY_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("openai/gpt-oss-120b", "openai/gpt-5-mini", "openai/gpt-5-mini-fast",
                            "openai/gpt-5-nano", "openai/gpt-4o-mini",
                            "google/gemini-2.5-flash-lite",
                            "inclusionai/ling-3.1-flash", "inclusionai/ling-3.1-flash-free",
                            "alibaba/qwen3.7-flash", "stepfun/step-3.7-flash",
                            "poolside/laguna-s-2.1-free",
                            "nvidia/nemotron-3-ultra-550b-a55b",
                            "xiaomi/mimo-v2.5", "xiaomi/mimo-v2.5-pro",
                            "xiaomi/mimo-v2.6-flash"),
        "required_env_flags": ("FREEAGENT_VERCEL_FREE_TIER",), "catalog_requires_ready": True,
        "note": "Vercel AI GatewayのFree tier（月 $5 クレジット）確認が必須（FREEAGENT_VERCEL_FREE_TIER=1）。"
                "無料枠の対象は Free Tier 対象モデルのサブセット（実測 15 件だけを許可）。"
                "クレジットを購入すると paid tier に移り、月次の無料クレジットは適用されません",
    }
