"""§3.6/§3.7 選抜ストラテジとタスク分類カスケード

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import os
import threading

from . import server as S  # noqa: E402

SELECT_STRATEGIES = ("quality", "catalog", "least_busy")


SELECT_STRATEGY_ENV = "FREEAGENT_SELECT_STRATEGY"


def selection_strategy() -> str:
    """`FREEAGENT_SELECT_STRATEGY` を読む。未知の値は `quality` に落とす（起動を止めない）。"""
    want = (os.environ.get(SELECT_STRATEGY_ENV) or "quality").strip().lower()
    return want if want in SELECT_STRATEGIES else "quality"


ROUTING_ENV = "FREEAGENT_ROUTING"


ROUTING_TIER_ENV = "FREEAGENT_ROUTING_TIER"


ROUTING_TIERS = ("light", "long", "reason")


ROUTING_LONG_CHARS = max(200, min(200000, S._env_int("FREEAGENT_ROUTING_LONG_CHARS", 6000)))


ROUTING_LIGHT_MAX_TOKENS = max(16, min(4000, S._env_int("FREEAGENT_ROUTING_LIGHT_MAX_TOKENS", 400)))


ROUTING_LIGHT_MIN_QUALITY = max(0.0, min(1.0, S._env_float("FREEAGENT_ROUTING_LIGHT_MIN_QUALITY", 0.5)))


_ROUTING_LOCAL = threading.local()


def routing_enabled() -> bool:
    """`FREEAGENT_ROUTING` が auto のときだけ層分けする（既定 off）。"""
    return (os.environ.get(ROUTING_ENV) or "off").strip().lower() in ("auto", "on", "tier", "1", "true")


def forced_tier() -> str:
    """測定用の強制層。未指定・未知は空文字。"""
    want = (os.environ.get(ROUTING_TIER_ENV) or "").strip().lower()
    return want if want in ROUTING_TIERS else ""


def route_tier(kind: str, text_chars: int, max_tokens: int) -> str:
    """呼び出しの形から層を決める（決定的）。off なら `"off"`。

    `kind` はツール名（`freeagent_map` など）。長さは**渡す文字列の合計**、`max_tokens` は指定値
    （未指定は 0）。**迷ったら reason**（品質順）に倒す — 層を分けたことで品質が落ちる事故を避ける。
    """
    forced = forced_tier()
    if forced:
        return forced
    if not routing_enabled():
        return "off"
    if text_chars >= ROUTING_LONG_CHARS:
        return "long"
    name = S.as_str(kind).lower()
    # **`freeagent_ask` は対象外**: あれは `default_model()` を 1 回呼ぶだけで選抜を通らない（実測）。
    # 層は「選抜される呼び出し」にだけ意味がある（map / panel / grounded / fanout / agent）。
    if name == "freeagent_map" and 0 < max_tokens <= ROUTING_LIGHT_MAX_TOKENS \
            and text_chars < ROUTING_LONG_CHARS // 2:
        return "light"
    return "reason"


ROUTING_TEXT_KEYS = ("prompt", "question", "thought", "text", "query", "draft", "main_reply",
                     "items", "prompts", "plan", "evidence")


def _routing_text_chars(args: dict) -> int:
    """渡す本文の合計文字数（文字列と文字列の配列だけを数える）。型が違う値は数えない。"""
    total = 0
    for key in ROUTING_TEXT_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            total += len(value)
        elif isinstance(value, list):
            total += sum(len(item) for item in value if isinstance(item, str))
    return total


def _routing_scope(tier: str) -> str:
    """現在のスレッドに層を置き、直前の値を返す（呼び出し側が戻せるように）。"""
    previous = getattr(_ROUTING_LOCAL, "tier", "")
    _ROUTING_LOCAL.tier = tier
    return previous


def current_tier() -> str:
    """いまのスレッドの層（未設定は空文字）。"""
    return getattr(_ROUTING_LOCAL, "tier", "")


def model_mean_latency(ref: str) -> float | None:
    """平均所要時間（ミリ秒）。観測が無ければ None（**推測で埋めない**）。"""
    S._ensure_stats_loaded()
    kinds = (S._STATS["models"].get(ref) or {}).get("kinds") or {}
    total_n = total_ms = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = S.as_float(row.get("n"), 0.0)
        ms = S.as_float(row.get("lat_ms"), 0.0)
        if n > 0 and ms > 0:
            total_n += n
            total_ms += ms
    return round(total_ms / total_n, 1) if total_n and total_ms else None


def model_trunc_rate(ref: str) -> float | None:
    """打ち切りの率（trunc / n）。観測が無ければ None。"""
    S._ensure_stats_loaded()
    kinds = (S._STATS["models"].get(ref) or {}).get("kinds") or {}
    n = trunc = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n += S.as_float(row.get("n"), 0.0)
        trunc += S.as_float(row.get("trunc"), 0.0)
    return round(trunc / n, 4) if n else None


def _tier_order(refs: list[str], tier: str) -> list[str]:
    """層ごとの並び。**品質順（`S.rank_models`）を土台**にし、層の指標で前後させる。"""
    quality_index = {ref: i for i, ref in enumerate(S.rank_models(refs))}
    if tier == "light":
        def light_key(ref: str):
            quality = S.model_quality(ref)
            observed = S.model_observations(ref) > 0
            latency = model_mean_latency(ref)
            # 0 = 品質の下限を満たす観測済み（速い順）/ 1 = 未観測 / 2 = 品質不足か観測なしの遅い側
            lane = 0 if (observed and quality >= ROUTING_LIGHT_MIN_QUALITY and latency is not None) else (1 if not observed else 2)
            return (lane, latency if latency is not None else 0.0, quality_index[ref])
        return sorted(refs, key=light_key)
    if tier == "long":
        def long_key(ref: str):
            rate = model_trunc_rate(ref)
            # 打ち切りの少ない順。観測が無いモデルは「分からない」ので品質順の位置に置く（0.5 扱い）。
            return (rate if rate is not None else 0.5, quality_index[ref])
        return sorted(refs, key=long_key)
    return list(refs)


def _order_for_selection(refs: list[str], kind: str | None = None) -> list[str]:
    """自動選抜の並び順を strategy ごとに決める（`S.diverse_order` は呼び出し側で共通）。"""
    strategy = selection_strategy()
    if strategy == "catalog":
        base = list(refs)
    elif strategy == "least_busy":
        per = (S.PROVIDER_GATE.report(list(S.PROVIDER_ORDER)).get("per_provider") or {})
        quality_index = {ref: i for i, ref in enumerate(S.rank_models(refs))}
        def busy_key(ref: str):
            provider = ref.split("/", 1)[0]
            in_flight = (per.get(provider) or {}).get("in_flight") or 0
            # 空きが同じなら品質順（決定的。乱数を使わない）
            return (in_flight, quality_index.get(ref, len(refs)), ref)
        base = sorted(refs, key=busy_key)
    else:
        base = S.rank_models(refs)
        if kind and S.RANK_KIND_BIAS:
            # **kind 別バイアス**: 同じ kind の失敗率を引いて並べ替える（既定 off では通らない）。
            base = sorted(refs, key=lambda r: (-(S._rank_score(r) - S._kind_bias(r, kind)),
                                               S._rank_latency_key(r), r))
    # §3.7 タスク分類カスケード。**層が選ばれていない（off）ときは 1 ミリも並べ替えない**。
    tier = forced_tier() or current_tier()
    return _tier_order(base, tier) if tier in ROUTING_TIERS else base


def env_excluded_models() -> list[str]:
    """`FREEAGENT_MODEL_EXCLUDE`（カンマ区切り）で指定された除外モデル一覧."""
    raw = os.environ.get("FREEAGENT_MODEL_EXCLUDE") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _model_identity(ref: str) -> str:
    """Identity for independent opinions, not for catalog pricing or authorization."""
    provider, model = S.split_ref(ref)
    if S._is_hf(provider) and ":" in model:
        return S.make_ref(provider, model.rsplit(":", 1)[0])
    return ref


def _is_model_excluded(ref: str, excluded: set[str]) -> bool:
    """Explicit base exclusions retain their existing wildcard semantics."""
    if not excluded or not ref:
        return False
    identity = _model_identity(ref)
    return (ref in excluded or ref.rsplit(":", 1)[0] in excluded
            or any(_model_identity(item) == identity for item in excluded))


def select_models(size: int, requested: list[str] | None = None, *,
                  prefer: list[str] | None = None, exclude: list[str] | None = None,
                  free_only: bool = True, identity_exclude: list[str] | None = None,
                  kind: str | None = None) -> tuple[list[str], dict]:
    """参加モデルを決める。明示 > prefer > 品質統計 の順に効かせる（選んだ根拠を返す）。"""
    all_exclude = list(dict.fromkeys(S.as_str_list(exclude) + env_excluded_models()))
    ex_set = set(all_exclude)
    identity_set = {_model_identity(ref) for ref in S.as_str_list(identity_exclude)}
    available = S.free_model_refs(free_only=free_only)
    if not available:
        return [], {"reason": "no_models", "available": 0}
    avail_set = set(available)
    notes: list[str] = []
    chosen: list[str] = []

    def is_available(ref: str) -> bool:
        """在庫（一覧）にあるか。**`モデル:提供元` の経路指定も受ける**（HF は同じモデルでも
        提供元ごとに無料/有料・生死が違うので、`inclusionAI/…:novita` のように指定して使う。
        在庫一覧は提供元サフィックス無しの ID を返すため、素の突き合わせだと除外されてしまう）。
        """
        provider, model = S.split_ref(ref)
        return ref in avail_set or (S._is_hf(provider) and ":" in model
                                    and S.make_ref(provider, model.rsplit(":", 1)[0]) in avail_set)

    def add(refs: list[str]) -> None:
        for ref in refs:
            if (is_available(ref) and _model_identity(ref) not in {_model_identity(r) for r in chosen}
                    and _model_identity(ref) not in identity_set
                    and not _is_model_excluded(ref, ex_set)):
                chosen.append(ref)

    if requested:
        unknown = [r for r in requested if not is_available(r)]
        add(requested)
        if unknown:
            notes.append(f"未知/未提供のモデルを除外: {', '.join(unknown[:5])}")
        cooling_requested = [r for r in chosen if S.is_cooling(r)]
        if cooling_requested:
            notes.append(f"指定モデルがクールダウン中（再試行で回復します）: {', '.join(cooling_requested[:3])}")
        # 明示指定は**廃止でも試す**（利用者が復活を確かめたいことがある）。ただし黙って 404 を返さない。
        gone_requested = [r for r in chosen if S.is_gone(r)]
        if gone_requested:
            notes.append("指定モデルが直近 404/410（廃止の記録あり・指定なので試します）: "
                         + ", ".join(gone_requested[:3]))
    if prefer:
        add(prefer)
    if len(chosen) < size:
        # **非チャットモデルは自動選抜に載せない**（§3.2）。明示指定の在庫確認（is_available）は
        # 在庫の全 Free を使うので、名指しされたモデルはこれまでどおり試す。
        chat_ok = set(S.free_model_refs(free_only=free_only, chat_only=True))
        pool = [r for r in available if r not in chosen and r in chat_ok
                and _model_identity(r) not in identity_set]
        if ex_set:
            pool = [r for r in pool if not _is_model_excluded(r, ex_set)]
        # クールダウン中を**先に選ばない**（除外ではなく後回し）。選んだ直後に 429 が記録されると
        # 「全候補がクールダウン中」で 1 体へ縮退し、失敗に見える（実測）。空きが足りないときだけ補充する。
        cooling = S.cooling_refs()
        # **認証で駄目だったプロバイダを自動選抜から外す**（実測: HF の権限不足で 3 体が空振りし、
        # 代替へ回る分だけ遅くなる）。明示 `requested` は上の add() で既に入っているので影響しない。
        auth_blocked = {p: S.provider_auth_blocked(p) for p in S.PROVIDER_ORDER}
        blocked = {p for p, e in auth_blocked.items() if e}
        if blocked:
            pool = [r for r in pool if r.split("/", 1)[0] not in blocked]
            notes.append("認証で失敗中のため自動選抜から除外: " + ", ".join(sorted(blocked))
                         + "（明示指定すれば試します。キー/権限を直せば数分で戻ります）")
        # **直近 404/410（gone）は自動選抜から外す**（v0.50.0・issue #12）。廃止済みが既定に選ばれると
        # `model` 省略の ask が 404 で落ち、同じ失敗が続くとクライアント側がサーバーを一時停止する。
        # 在庫（一覧）からは消さない（`freeagent_models` が理由つきで返す）。
        gone = S.gone_refs()
        if gone:
            pool = [r for r in pool if r not in gone]
            notes.append("直近 404/410（gone）のため自動選抜から除外: %d 件"
                         "（freeagent_models が理由を返します。明示指定すれば試します）" % len(gone))
        ready = [r for r in pool if r not in cooling]
        waiting = [r for r in pool if r in cooling]
        need = size - len(chosen)
        picked = []
        seen = {_model_identity(ref) for ref in chosen}
        for candidate in S.diverse_order(_order_for_selection(ready, kind)):
            identity = _model_identity(candidate)
            if identity not in seen:
                picked.append(candidate)
                seen.add(identity)
                if len(picked) >= need:
                    break
        if len(picked) < need and waiting:
            for candidate in S.diverse_order(_order_for_selection(waiting)):
                identity = _model_identity(candidate)
                if identity not in seen:
                    picked.append(candidate)
                    seen.add(identity)
                    if len(picked) >= need:
                        break
            notes.append(f"空きが足りずクールダウン中から補充: {', '.join(waiting[:3])}")
        chosen.extend(picked)
    if ex_set:
        chosen = [r for r in chosen if not _is_model_excluded(r, ex_set)]

    # **明示指定は size で切り落とさない**。実測: `models` に 8 件渡しても size=3 で 3 件に切られ、
    # 「在庫のモデルを全部走らせる」ができなかった。`size` は**自動選抜の目標本数**として扱う。
    explicit_kept = len([r for r in S.as_str_list(requested) if r in chosen])
    chosen = chosen[:max(size, explicit_kept)]
    info = {
        "requested": bool(requested),
        "prefer": S.as_str_list(prefer),
        "exclude": all_exclude,
        "available": len(available),
        "ranking": S.RANK_ENABLED,
        "selection_strategy": selection_strategy(),
        # §3.7 の層（off のときは "off"）。選んだ根拠を隠さない（規約 8）。
        "cascade_tier": forced_tier() or current_tier() or "off",
        "status": {r: S.model_status(r) for r in chosen},
        "notes": notes,
    }
    return chosen, info


EXPORTS = {
    "SELECT_STRATEGIES": SELECT_STRATEGIES,
    "SELECT_STRATEGY_ENV": SELECT_STRATEGY_ENV,
    "selection_strategy": selection_strategy,
    "ROUTING_ENV": ROUTING_ENV,
    "ROUTING_TIER_ENV": ROUTING_TIER_ENV,
    "ROUTING_TIERS": ROUTING_TIERS,
    "ROUTING_LONG_CHARS": ROUTING_LONG_CHARS,
    "ROUTING_LIGHT_MAX_TOKENS": ROUTING_LIGHT_MAX_TOKENS,
    "ROUTING_LIGHT_MIN_QUALITY": ROUTING_LIGHT_MIN_QUALITY,
    "_ROUTING_LOCAL": _ROUTING_LOCAL,
    "routing_enabled": routing_enabled,
    "forced_tier": forced_tier,
    "route_tier": route_tier,
    "ROUTING_TEXT_KEYS": ROUTING_TEXT_KEYS,
    "_routing_text_chars": _routing_text_chars,
    "_routing_scope": _routing_scope,
    "current_tier": current_tier,
    "model_mean_latency": model_mean_latency,
    "model_trunc_rate": model_trunc_rate,
    "_tier_order": _tier_order,
    "_order_for_selection": _order_for_selection,
    "env_excluded_models": env_excluded_models,
    "_model_identity": _model_identity,
    "_is_model_excluded": _is_model_excluded,
    "select_models": select_models,
}
