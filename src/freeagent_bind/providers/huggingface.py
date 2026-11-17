"""huggingface — Hugging Face Inference Providers（router）.

**2026-11-16 にチャット推論のルータが OpenRouter のルータへ置き換わった**（URL とトークンは不変・
課金は HF アカウント・HF の手数料なし・独自プロバイダキーは使用不可）。一覧は未認証でも取得でき、
`providers[]` に提供元ごとの `is_free` / `pricing` / `context_length` が付く形は維持されている。

実測（2026-11-17）: 一覧 164 件（`is_free` は全件 false・価格 0 の提供元も 0 件）、推論は全件 402
「Purchase pre-paid credits」（`/api/whoami-v2` は isPro=false / canPay=false / billingMode=prepaid）。
つまり **Free の在庫は無く、PRO の月次クレジットか前払いクレジットが前提**。よって既定では
`PROVIDER_ORDER`（＝一覧・自動選抜）に載せない。`FREEAGENT_HF_CREDITS=1` を明示したときだけ
クレジット枠として在庫に入る（§4.1）。

SPEC の各フィールドの意味は SPEC.md §4.1。値は import 時に環境変数から読む。
"""
import os  # noqa: F401  各 SPEC が env を読む

SPEC = {
        "base_url": os.environ.get("FREEAGENT_HF_BASE_URL", "https://router.huggingface.co/v1"),
        "key": (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
                or os.environ.get("HUGGINGFACEHUB_API_TOKEN") or ""),
        "key_env": "HF_TOKEN", "free_kind": "hf_credits", "always_ready": False,
        # HF（Router）かどうかの判定に使う。`_is_hf` はこの印を第一に見る（free_kind の改名に耐える）。
        "hf_router": True,
        # 既定で `PROVIDER_ORDER` に載せない（載せるにはこの env を真にする）。
        "opt_in_env": "FREEAGENT_HF_CREDITS",
        "note": ("Hugging Face Inference Providers（2026-11-16 から中身は OpenRouter のルータ。一覧は"
                 "未認証でも取得可・推論はトークン必須・独自プロバイダキー不可・課金は HF アカウント）。"
                 "Free の提供元は 0 件なので既定では一覧に出さない（PRO の月次クレジット等で使うときだけ "
                 "FREEAGENT_HF_CREDITS=1）"),
    }
