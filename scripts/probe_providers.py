"""3 プロバイダ（OpenRouter / NVIDIA NIM / Hugging Face）の実推論を検証する（鍵は環境変数から）。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from freeagent_bind import server as S

print("=== ready 判定（鍵が入っているか） ===")
for r in S.provider_status():
    print(f"  {'✓' if r['ready'] else '—'} {r['provider']:12} models={r['models']:4} free={r['free']:4} err={r['error'][:50]}")

print("\n=== 各プロバイダの Free モデルで実推論 ===")
prompt = "「並列」を英単語1つで答えよ。記号も説明も書くな。"
for provider in ("openrouter", "nvidia", "huggingface"):
    if not S.provider_ready(provider):
        print(f"  {provider}: 鍵が無いのでスキップ")
        continue
    rows = [m for m in S.fetch_provider_models(provider) if m.get("free")]
    if not rows:
        print(f"  {provider}: Free モデルが 0 件")
        continue
    row = rows[0]
    model = row["id"]
    if provider == "huggingface" and row.get("free_via"):
        model = f"{model}:{row['free_via'][0]}"   # 無料の経路を固定
    ref = f"{provider}/{model}"
    res = S.call_model(ref, prompt, max_tokens=80, kind="probe")
    if res.get("error"):
        print(f"  ✗ {ref}\n      {res['error'][:220]}")
    else:
        print(f"  ✓ {res['ref']} -> {res['text'].strip()[:70]!r}"
              f" ({res['latency_s']}s, tokens={res.get('tokens')})")
        if res.get("fallback"):
            print(f"      （要求 {ref} から代替へ回った）")

print("\n=== プロバイダ横断の並列（4 プロバイダから選抜） ===")
refs, info = S.select_models(4)
print("  選抜:", refs, "| notes:", info.get("notes"))
if refs:
    results = S.ask_many(refs, prompt, max_tokens=60, kind="probe")
    for ref, r in zip(refs, results):
        print(f"  • {ref}: {'✗ ' + str(r.get('error'))[:90] if r.get('error') else repr(r['text'].strip()[:50])}")