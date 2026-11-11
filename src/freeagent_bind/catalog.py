"""§3.2/§3.3 モデル一覧の取得・キャッシュ・チャット向け選別

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import os
import re
import threading
import time

from . import server as S  # noqa: E402

CATALOG_TTL = 600.0                       # メモリ／ディスクの既定 TTL（秒）


_CATALOG_FILE = "model_catalog.json"


_CATALOG_DEADLINE_S = max(1.0, S._env_float("FREEAGENT_CATALOG_DEADLINE", 10.0))


_MODELS_INFLIGHT: dict[str, threading.Event] = {}


_MODELS_INFLIGHT_LOCK = threading.Lock()


_CATALOG_DISK: dict[str, dict] = {}       # provider -> {"at": float, "rows": [...]}（ディスクのミラー）




_CATALOG_DISK_LOCK = threading.Lock()


_CATALOG_DISK_MAX_ROWS = 2000             # 1 プロバイダで保持する行数の上限（壊れた応答の歯止め）


def catalog_path() -> str:
    """モデル一覧のプロセス間キャッシュの置き場。"""
    return os.path.join(S.state_dir(), _CATALOG_FILE)


def catalog_cache_enabled() -> bool:
    """ディスクキャッシュの有無（`FREEAGENT_CATALOG_CACHE=0` で無効化。テストと切り分け用）。"""
    return S._env_on("FREEAGENT_CATALOG_CACHE", True)


def _catalog_disk_snapshot() -> dict:
    """ディスクの内容を返す。**更新時刻（mtime）が変わったときだけ読み直す**（別プロセスの追記を見る）。

    壊れたファイル・知らない形は**空として扱う**（例外を外へ漏らさない・規約 1）。
    """
    if not catalog_cache_enabled():
        return {}
    path = catalog_path()
    mtime = S._file_mtime(path)
    with _CATALOG_DISK_LOCK:
        if mtime is None:
            _CATALOG_DISK.clear()
            S._CATALOG_DISK_MTIME = None
            return {}
        if S._CATALOG_DISK_MTIME == mtime:
            return dict(_CATALOG_DISK)
        data = S._load_json(path, {})
        providers = data.get("providers") if isinstance(data, dict) else None
        loaded = {
            name: {"at": row.get("at"), "rows": row.get("rows")}
            for name, row in (providers or {}).items()
            if isinstance(row, dict) and isinstance(row.get("rows"), list) and row.get("rows")
        }
        _CATALOG_DISK.clear()
        _CATALOG_DISK.update(loaded)
        S._CATALOG_DISK_MTIME = mtime
        return dict(loaded)


def _catalog_disk_rows(provider: str, ttl: float) -> tuple[list[dict], bool]:
    """ディスクの行と「TTL 内か」を返す（`ttl <= 0` は常に鮮度なし＝HTTP を出す）。"""
    entry = _catalog_disk_snapshot().get(provider) or {}
    rows = entry.get("rows")
    if not isinstance(rows, list) or not rows:
        return [], False
    fresh = ttl > 0 and S.now_ts() - S.as_float(entry.get("at"), 0.0) < ttl
    return list(rows), fresh


def _catalog_disk_store(provider: str, rows: list[dict]) -> bool:
    """成功した一覧をディスクへ書く。**空では書かない**（環境障害で状態を汚さない・規約 21）。

    読み直してから足す（他プロセスの項目を消さない）＋ `S._file_lock` でプロセス間を直列化する。
    """
    if not rows or not catalog_cache_enabled():
        return False
    path = catalog_path()
    entry = {"at": S.now_ts(), "rows": rows[:_CATALOG_DISK_MAX_ROWS]}
    with S._file_lock(path):
        data = S._load_json(path, {})
        providers = dict(data.get("providers") or {}) if isinstance(data, dict) else {}
        providers[provider] = entry
        ok = S._atomic_write(path, S._dumps({"version": 1, "providers": providers}))
    if ok:
        with _CATALOG_DISK_LOCK:
            _CATALOG_DISK[provider] = entry
            S._CATALOG_DISK_MTIME = S._file_mtime(path)
    return ok


def _models_memory(provider: str, ttl: float | None = None) -> list[dict] | None:
    """メモリの一覧。`ttl=None` は期限を見ない（前回値として使う）。無ければ None。"""
    with S._MODELS_LOCK:
        cached = S._MODELS_CACHE.get(provider)
        if not cached:
            return None
        if ttl is not None and S.now_ts() - S.as_float(cached.get("at"), 0.0) >= ttl:
            return None
        return list(cached.get("rows") or [])


def _models_store(provider: str, rows: list[dict], error: str = "") -> None:
    with S._MODELS_LOCK:
        S._MODELS_CACHE[provider] = {"at": S.now_ts(), "rows": list(rows), "error": error}


def _catalog_stale_rows(provider: str) -> list[dict]:
    """取得できなかったプロバイダを**前回の成功値**で埋める（メモリ → ディスク・TTL 無視）。"""
    with S._MODELS_LOCK:
        cached = S._MODELS_CACHE.get(provider) or {}
    if cached.get("rows") and not cached.get("error"):
        return list(cached["rows"])
    rows, _fresh = _catalog_disk_rows(provider, 0.0)
    return rows


def _fetch_provider_catalog(provider: str, spec: dict) -> list[dict]:
    """HTTP で 1 回取る（失敗も `S._MODELS_CACHE` に載せる＝隠さない）。"""
    rows: list[dict] = []
    error = ""
    try:
        data = S.provider_http(spec.get("models_path") or "/models", provider=provider,
                             timeout=min(30.0, S.READ_TIMEOUT))
        raw_rows = _provider_catalog_rows(provider, data)
        for raw in raw_rows:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            rows.append({
                "id": str(raw.get("id")),
                "provider": provider,
                "free": S._is_free(provider, raw),
                # サブLLM（テキスト対話）に使えるか。埋め込み・分類器・画像/音声生成は選抜から外す（§3.2）。
                "chat": is_chat_model(provider, raw),
                # 無料で使える**経路**（HF は提供元名。他プロバイダでは空）。呼び出しのヒントになる。
                "free_via": S._free_providers(raw) if S._is_hf(provider) else [],
                "context_length": S._context_length(provider, raw),
                "pricing": raw.get("pricing") or {},
                "access": raw.get("access"),
            })
        rows = _with_allowlisted_rows(provider, spec, rows)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    _models_store(provider, rows, error)
    if rows:
        _catalog_disk_store(provider, rows)
    return rows


def _with_allowlisted_rows(provider: str, spec: dict, rows: list[dict]) -> list[dict]:
    """一覧が返さない Free ID を在庫として補う（spec の `synthesize_free_ids` が真のときだけ）。

    実測（2026-10-23）: Z.ai の `/models` は有料の旗艦 11 件だけを返し、公式価格表で $0 の
    `glm-4.7-flash` などは載らない。載っていないことを理由に Free を隠すと、その提供元は
    自動選抜に永久に入らず、`freeagent_models` も「Free 0 件」という嘘を返す。
    公表された Free ID は**呼べる在庫**なので、一覧に無いときだけ足す（既存行は触らない）。

    **既定では足さない**: 一覧が許可リストの ID を返す提供元では、一覧が唯一の在庫の根拠
    （許可リストは「有料を呼ばない」ための門番）なので、勝手に行を増やすと在庫の意味が変わる。
    """
    allowed = spec.get("free_model_ids")
    if not allowed or not spec.get("synthesize_free_ids"):
        return rows
    known = {S.as_str(row.get("id")) for row in rows}
    extra = [{"id": model, "provider": provider, "free": True, "chat": True, "free_via": [],
              "context_length": None, "pricing": {}, "access": None, "synthesized": True}
             for model in allowed if model not in known]
    return rows + extra if extra else rows


def fetch_provider_models(provider: str, ttl: float = CATALOG_TTL) -> list[dict]:
    """モデル一覧。Free モデルは入れ替わるので**固定しない**（毎回ここから解決する）。

    取得の順序はメモリ → ディスク → HTTP（§3.3）。取得が締め切りに間に合わなかったときだけ
    **前回の成功値**で埋める（取得の「失敗」は空 + `error` のまま返す＝失敗を隠さない）。
    """
    spec = S.PROVIDER_SPECS.get(provider) or {}
    if spec.get("catalog_requires_ready") and not S.provider_ready(provider):
        return []
    hit = S._models_memory(provider, ttl)
    if hit is not None:
        return hit
    disk_rows, disk_fresh = _catalog_disk_rows(provider, ttl)
    if disk_fresh:
        _models_store(provider, disk_rows)
        return list(disk_rows)
    owner = False
    with _MODELS_INFLIGHT_LOCK:
        event = _MODELS_INFLIGHT.get(provider)
        if event is None:
            event = threading.Event()
            _MODELS_INFLIGHT[provider] = event
            owner = True
    if owner:
        try:
            return _fetch_provider_catalog(provider, spec)
        finally:
            with _MODELS_INFLIGHT_LOCK:
                _MODELS_INFLIGHT.pop(provider, None)
            event.set()
    # 進行中の取得を待つ（上限 `_CATALOG_DEADLINE_S`）。取れなければ前回の成功値で埋める。
    event.wait(S._CATALOG_DEADLINE_S)
    hit = S._models_memory(provider, ttl)
    return hit if hit is not None else _catalog_stale_rows(provider)


def _catalog_prefetch(providers: list[str], ttl: float = CATALOG_TTL,
                      deadline: float | None = None) -> None:
    """プロバイダごとの一覧を**並列に**取り、全体の締め切りで打ち切る（§3.3）。

    締め切りを超えたプロバイダの取得は裏で続く（完了すれば次の呼び出しがディスク／メモリから温まる）。
    """
    budget = S._CATALOG_DEADLINE_S if deadline is None else max(0.0, deadline)
    started = []
    for name in providers:
        if S._models_memory(name, ttl) is not None:
            continue
        thread = threading.Thread(target=fetch_provider_models, args=(name, ttl),
                                  name=f"catalog-{name}", daemon=True)
        thread.start()
        started.append(thread)
    end = time.monotonic() + budget
    for thread in started:
        left = end - time.monotonic()
        if left <= 0:
            break
        thread.join(left)


def _provider_missing_settings(spec: dict) -> list[str]:
    """List missing key/plan/privacy confirmations without exposing their values."""
    missing = []
    if not spec.get("always_ready") and not spec.get("key"):
        missing.append(spec.get("key_env") or "API key")
    missing.extend(name for name in spec.get("required_env_values") or ()
                   if not os.environ.get(name, "").strip())
    missing.extend(name for name, pattern in (spec.get("required_env_patterns") or {}).items()
                   if not re.fullmatch(pattern, os.environ.get(name, "")))
    accepted = {"1", "true", "yes", "on"}
    missing.extend(name for name in spec.get("required_env_flags") or ()
                   if os.environ.get(name, "").strip().lower() not in accepted)
    return list(dict.fromkeys(missing))


def provider_model_allowed(provider: str, model: str) -> bool:
    """Reject paid/unknown IDs even when explicitly selected by the caller."""
    allowed = (S.PROVIDER_SPECS.get(provider) or {}).get("free_model_ids")
    return allowed is None or S.as_str(model) in allowed


def _provider_catalog_rows(provider: str, data) -> list:
    """Normalize documented provider model-list envelopes; reject malformed catalogs."""
    spec = S.PROVIDER_SPECS.get(provider) or {}
    if spec.get("catalog_format") == "cloudflare_openrouter":
        if not isinstance(data, dict) or ("success" in data and data.get("success") is not True):
            raise ValueError("Cloudflare model catalog response is not successful")
        result = data.get("result")
        if isinstance(result, dict):
            rows = result.get("data")
        elif isinstance(result, list):
            rows = result
        elif isinstance(data.get("data"), list):
            rows = data["data"]  # documented marketplace-format response
        else:
            raise ValueError("Cloudflare marketplace model catalog has no data array")
        if not isinstance(rows, list):
            raise ValueError("Cloudflare marketplace model catalog data is not an array")
        return rows
    rows = data.get("data") if isinstance(data, dict) else None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError(f"{provider} model list is not an array")
    return rows


_NON_CHAT_PURPOSE_RE = re.compile(
    r"(?:^|[-/_.])("
    r"embed|embedqa|embedding|arctic-embed|rerank|reranker|nemoretriever|"
    r"guard|nemoguard|content-safety|topic-control|safety|"
    r"reward|parse|riva-translate|ising-calibration|"
    r"whisper|tts|asr"
    r")(?:$|[-/_.:])",
    re.IGNORECASE,
)


_NON_CHAT_OUTPUT_RE = re.compile(
    r"(?:^|[-/_.])("
    r"clip|nclip|nvclip|vision|vila|neva|kosmos|deplot|vlm|"
    r"lyria|diffusion|video-detector|synthetic-video|stable-diffusion"
    r")(?:$|[-/_.:])",
    re.IGNORECASE,
)


def _declared_output_modalities(raw: dict) -> list[str] | None:
    """提供元が宣言した出力モダリティ（OpenRouter の `architecture`）。無ければ None。"""
    arch = raw.get("architecture")
    if not isinstance(arch, dict):
        return None
    outs = arch.get("output_modalities")
    if isinstance(outs, list):
        vals = [S.as_str(o).lower() for o in outs if S.as_str(o)]
        return vals or None
    modality = S.as_str(arch.get("modality"))
    if "->" in modality:
        return [m.strip().lower() for m in modality.split("->", 1)[1].split("+") if m.strip()] or None
    return None


def is_chat_model(provider: str, raw: dict) -> bool:
    """サブLLM（テキスト対話）に使えるモデルか。

    用途の目印（A 群）は出力モダリティの宣言より優先し、出力の形の目印（B 群）は提供元の宣言が
    あればそれに従う。宣言が無い一覧（NVIDIA など）は両群の ID 目印で保守的に外す。
    """
    model_id = S.as_str(raw.get("id"))
    if _NON_CHAT_PURPOSE_RE.search(model_id):
        return False
    declared = _declared_output_modalities(raw)
    if declared is not None:
        return all(m == "text" for m in declared)
    return not _NON_CHAT_OUTPUT_RE.search(model_id)


def all_models(ttl: float = CATALOG_TTL) -> list[dict]:
    """全プロバイダの一覧。**プロバイダごとに並列に取り、全体の締め切りで打ち切る**（§3.3）。"""
    S._catalog_prefetch(list(S.PROVIDER_ORDER), ttl)
    out: list[dict] = []
    for provider in S.PROVIDER_ORDER:
        rows = S._models_memory(provider, ttl)
        out.extend(rows if rows is not None else _catalog_stale_rows(provider))
    return out


def provider_status() -> list[dict]:
    """プロバイダごとの要約。取得も**並列**（1 ホストの遅延が他を直列で待たせない・§3.3）。"""
    S._catalog_prefetch(list(S.PROVIDER_ORDER), CATALOG_TTL)
    rows = []
    for provider in S.PROVIDER_ORDER:
        fresh = S._models_memory(provider, CATALOG_TTL)
        models = fresh if fresh is not None else _catalog_stale_rows(provider)
        spec = S.PROVIDER_SPECS.get(provider) or {}
        with S._MODELS_LOCK:
            error = (S._MODELS_CACHE.get(provider) or {}).get("error") or ""
        rows.append({
            "provider": provider,
            "ready": S.provider_ready(provider),
            "models": len(models),
            "free": sum(1 for m in models if m.get("free")),
            # Free のうちサブLLM に使えない（非チャット）件数。選抜には載せない（§3.2）。
            "non_chat": sum(1 for m in models if m.get("free") and not m.get("chat", True)),
            "key_env": spec.get("key_env"),
            "note": spec.get("note"),
            "requires_activation": bool(spec.get("catalog_requires_ready")),
            "missing_settings": _provider_missing_settings(spec),
            "error": error,
        })
    return rows


def free_model_refs(free_only: bool = True, chat_only: bool = False) -> list[str]:
    """推論可能（ready）なプロバイダの Free モデル参照を、優先順で返す。

    `chat_only=True` は**サブLLM に使える（テキスト対話の）モデルだけ**を返す（§3.2）。
    自動選抜とフォールバックはこちらを使い、明示指定の在庫確認は既定（全 Free）を使う。
    """
    refs: list[str] = []
    # 一覧は**並列**に温めてから読む（冷起動の直列取得を利用者の待ち時間にしない・§3.3）。
    S._catalog_prefetch([p for p in S.PROVIDER_ORDER if S.provider_ready(p)], CATALOG_TTL)
    for provider in S.PROVIDER_ORDER:
        if not S.provider_ready(provider):
            continue
        rows = S._models_memory(provider, CATALOG_TTL)
        for row in (rows if rows is not None else _catalog_stale_rows(provider)):
            if free_only and not row.get("free"):
                continue
            if chat_only and not row.get("chat", True):
                continue
            ref = S.make_ref(provider, row["id"])
            if ref not in refs:
                refs.append(ref)
    return refs


def default_model(free_only: bool = True) -> str:
    """既定モデル。**品質統計・クールダウン・認証失敗を通した並び**の先頭を返す。

    以前は `S.free_model_refs(chat_only=True)` の先頭（＝カタログ順の先頭）をそのまま返していた。
    そのため `freeagent_ask`（モデル省略時）は、冷却中・観測上ボロボロ・認証で落ちている
    モデルでもカタログの先頭なら選び続けた（実測: スコア 0.1 かつクールダウン中のモデルが
    `S.select_models(1)` の 0.9 のモデルより先に返った）。`S.select_models` を通すと
    `diverse_order` / 品質順 / 冷却の後回し / 認証失敗の除外が同じ規則で効く。
    """
    if S.DEFAULT_MODEL:
        return S.DEFAULT_MODEL
    try:
        chosen, _info = S.select_models(1, free_only=free_only)
    except Exception:                 # 選抜の失敗で呼び出し経路を止めない（規約 1）
        chosen = []
    if chosen:
        return chosen[0]
    refs = S.free_model_refs(free_only=free_only, chat_only=True)
    return refs[0] if refs else ""


def resolve_ref(ref: str, free_only: bool = True) -> tuple[str, str]:
    """参照から (provider, model) を決める。未指定なら最初の ready な Free モデル。"""
    provider, model = S.split_ref(ref)
    if provider:
        return provider, model
    if model:
        S._catalog_prefetch(list(S.PROVIDER_ORDER), CATALOG_TTL)   # 並列に温めてから探す（§3.3）
        for name in S.PROVIDER_ORDER:
            if any(r["id"] == model for r in fetch_provider_models(name)):
                return name, model
        return S.PROVIDER_ORDER[0], model
    return S.split_ref(S.default_model(free_only=free_only))


EXPORTS = {
    "CATALOG_TTL": CATALOG_TTL,
    "_CATALOG_FILE": _CATALOG_FILE,
    "_CATALOG_DEADLINE_S": _CATALOG_DEADLINE_S,
    "_MODELS_INFLIGHT": _MODELS_INFLIGHT,
    "_MODELS_INFLIGHT_LOCK": _MODELS_INFLIGHT_LOCK,
    "_CATALOG_DISK": _CATALOG_DISK,
    "_CATALOG_DISK_LOCK": _CATALOG_DISK_LOCK,
    "_CATALOG_DISK_MAX_ROWS": _CATALOG_DISK_MAX_ROWS,
    "catalog_path": catalog_path,
    "catalog_cache_enabled": catalog_cache_enabled,
    "_catalog_disk_snapshot": _catalog_disk_snapshot,
    "_catalog_disk_rows": _catalog_disk_rows,
    "_catalog_disk_store": _catalog_disk_store,
    "_models_memory": _models_memory,
    "_models_store": _models_store,
    "_catalog_stale_rows": _catalog_stale_rows,
    "_fetch_provider_catalog": _fetch_provider_catalog,
    "_with_allowlisted_rows": _with_allowlisted_rows,
    "fetch_provider_models": fetch_provider_models,
    "_catalog_prefetch": _catalog_prefetch,
    "_provider_missing_settings": _provider_missing_settings,
    "provider_model_allowed": provider_model_allowed,
    "_provider_catalog_rows": _provider_catalog_rows,
    "_NON_CHAT_PURPOSE_RE": _NON_CHAT_PURPOSE_RE,
    "_NON_CHAT_OUTPUT_RE": _NON_CHAT_OUTPUT_RE,
    "_declared_output_modalities": _declared_output_modalities,
    "is_chat_model": is_chat_model,
    "all_models": all_models,
    "provider_status": provider_status,
    "free_model_refs": free_model_refs,
    "default_model": default_model,
    "resolve_ref": resolve_ref,
}
