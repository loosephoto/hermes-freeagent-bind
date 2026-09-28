"""Free モデルの生存確認を行い、結果を**永続ストア**へ定着させる（ウォームアップ）。

なぜ必要か: 各プロバイダの `/v1/models` は実態と乖離している（実測: NVIDIA の一覧 82 件のうち
55 件が 404=EOL、Hugging Face の無料 3 件は トークン権限不足で 403）。生存確認の結果を
`cooldowns.json` / `model_stats.json` / `provider_auth.json` に残しておくと、以後の自動選抜が
**生きているモデルだけ**を選ぶようになる。

使い方（鍵は環境変数で渡す。値は表示されない）:

    env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py
    env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --providers openrouter
    env -u PYTHONPATH PYTHONPATH=src python scripts/warmup_models.py --page 25 --max-pages 6

数分かかる（NVIDIA は 82 件を実際に叩く）。モデルは週単位で入れ替わるので、定期的に再実行する。
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from freeagent_bind import server as S  # noqa: E402


def sweep(provider: str, page: int, max_pages: int) -> dict:
    alive, slow, dropped = [], [], []
    offset, pages = 0, 0
    while pages < max_pages:
        res = S.handle_tool_call({"name": "freeagent_models", "arguments": {
            "provider": provider, "free_only": True, "all": True, "limit": page,
            "offset": offset, "probe": True, "probe_limit": page}})
        data = res.get("structuredContent") or {}
        q = data.get("query") or {}
        if not q.get("probed"):
            break
        got = q.get("probe_attempted") or 0
        alive += [m["ref"] for m in (data.get("models") or []) if m.get("probe") == "alive"]
        slow += [(r["ref"], r.get("verdict")) for r in (q.get("probe_errors") or [])]
        dropped += [(r["ref"], r.get("verdict")) for r in (q.get("probe_dropped") or [])]
        print(f"  page {pages}: 試行 {got} / 応答 {q.get('probe_alive')}"
              f" / 遅い {q.get('probe_slow')} / 除外 {len(q.get('probe_dropped') or [])}"
              f"（Free 総数 {q.get('matched_free')}）", flush=True)
        pages += 1
        if got < page:
            break
        offset += got
    return {"alive": alive, "slow": slow, "dropped": dropped}


def main() -> int:
    ap = argparse.ArgumentParser(description="Free モデルの生存確認（結果を永続ストアへ定着）")
    ap.add_argument("--providers", default=",".join(S.PROVIDER_ORDER),
                    help="対象プロバイダ（カンマ区切り。既定は全部）")
    ap.add_argument("--page", type=int, default=25, help="1 回の確認件数（既定 25）")
    ap.add_argument("--max-pages", type=int, default=6, help="プロバイダごとの最大ページ数（既定 6）")
    args = ap.parse_args()

    print(f"状態ディレクトリ: {S.state_dir()}")
    print(f"プロバイダ: {args.providers}\n")
    started = time.time()
    summary = {}
    for provider in [p.strip() for p in args.providers.split(",") if p.strip()]:
        if not S.provider_ready(provider):
            print(f"=== {provider}: 資格情報が無いのでスキップ（一覧だけ取得） ===")
            continue
        print(f"=== {provider} ===", flush=True)
        summary[provider] = sweep(provider, args.page, args.max_pages)
        got = summary[provider]
        print(f"  ✅ 生存 {len(got['alive'])}: {got['alive'][:8]}")
        if got["slow"]:
            print(f"  ◷ 要再確認 {len(got['slow'])}: {[x[0] for x in got['slow']][:6]}")
        if got["dropped"]:
            kinds = {}
            for _, kind in got["dropped"]:
                kinds[kind] = kinds.get(kind, 0) + 1
            print(f"  ✗ 除外 {len(got['dropped'])} 内訳={kinds}")
        print(flush=True)

    print(f"完了（{time.time() - started:.0f}s）。生存確認の結果は以下のストアに残りました:")
    for path in (S.cooldowns_path(), S.stats_path(), S.auth_path()):
        print(f"  {path} ({os.path.getsize(path) if os.path.exists(path) else 0} B)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())