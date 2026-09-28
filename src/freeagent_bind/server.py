#!/usr/bin/env python3
"""hermes-freeagent-bind — Hermes Agent の Free モデルをサブLLMとして並列に走らせ、
外部知識（arXiv / Crossref / OpenAlex / Wikipedia / Wikidata / GitHub）で根拠づける MCP サーバー。

これは **モノリス**（単一ファイル）として書く。理由: 配布物が 1 つの stdio スクリプトで完結し、
遅延 import や相対 import の取り回しでクライアント側の起動が壊れる事故が無い（実測: stdio 起動後に
ネイティブ拡張を import すると無応答になる環境がある）。肥大化は前提なので、増築は「§区画の追加」で
行い、目次をこの docstring に保つ。目次が実装とずれたら、それは設計が崩れた合図。

目次
  §0 定数・設定        §1 ユーティリティ     §2 永続ストア（§2.1 クールダウン / §2.2 品質統計 /
                                             §2.3 トレース / §2.4 相談セッション /
                                             §2.5 プロバイダ認証の記憶）
  §3 プロバイダとモデル  §4 サブLLM呼び出し    §5 知識バックエンド
  §6 ツール実装        §7 ツール定義         §8 表示（content）
  §9 JSON-RPC / stdio

設計方針（旧 hermes-memex の実測で裏づけられた規約を継承）
  * 実行時依存ゼロ（標準ライブラリのみ）・stdio の JSON-RPC 2.0 を自前実装
  * どのツールも例外を外へ漏らさない（失敗は structuredContent.error で返す）
  * content（人間向けテキスト）と structuredContent（LLM向け純粋JSON）を両方返す
  * 外部 HTTP は (connect, read) のタイムアウト必須・429 は Retry-After を尊重して記憶する
  * 数値引数は防御的に変換する（不正値で例外を外へ出さない）
  * **モデル一覧を信じない**（実測: NVIDIA は 82 件中 55 件が 404=EOL、HF の無料 3 件は権限不足で 403）。
    `freeagent_models(probe=true)` で生存確認し、404/410・401/403 だけを除外する
  * protocolVersion は**クライアントが提示した版をそのまま返す**（交渉）。固定すると
    新しい版を提示するクライアントが接続直後に tools/list を cancel し「60秒タイムアウト」に見える
  * stdout へは **必ず UTF-8 バイト列**で書く（日本語 Windows は cp932 に落ち、応答が黙って捨てられる）
"""
from __future__ import annotations

import datetime
import email.utils
import json
import math
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================================================================ §0 定数・設定

SERVER_NAME = "hermes-freeagent-bind"
SERVER_VERSION = "0.2.0"

# クライアントが提示した版をそのまま返す（交渉）。自前実装で版を固定すると、新しい版を提示する
# クライアント（例: Hermes の MCP クライアントは 2025-11-25）が接続直後に tools/list を cancel
# してしまい「60秒タイムアウト」に見える（実測）。tools/list / tools/call のメッセージ形は
# これらの版で同一なので、提示版を返して差し支えない。
SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-11-25"
PROTOCOL_VERSION = DEFAULT_PROTOCOL_VERSION  # 後方互換（テストが参照する）


def negotiate_protocol(offered: str) -> str:
    """提示版がサポート内ならそのまま返し、未知なら最新の既定版を返す（MCP 仕様の交渉）。"""
    return offered if offered in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL_VERSION


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _env_on(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "off", "false", "no", "")


CONNECT_TIMEOUT = _env_float("FREEAGENT_CONNECT_TIMEOUT", 10.0)
READ_TIMEOUT = _env_float("FREEAGENT_READ_TIMEOUT", 180.0)
MAX_WORKERS = max(1, min(16, _env_int("FREEAGENT_MAX_WORKERS", 4)))
# 1 相談あたりの呼び出し上限（暴走・課金の歯止め）。超過したらラウンドを打ち切る。
MAX_CALLS_PER_RUN = max(1, _env_int("FREEAGENT_MAX_CALLS_PER_RUN", 40))
# サブエージェント起動（hermes chat -q）と Hermes 本体の委譲は明示オプトインのみ。既定 off。
ALLOW_AGENT = _env_on("FREEAGENT_ALLOW_AGENT", False)
HERMES_BIN = os.environ.get("FREEAGENT_HERMES_BIN") or shutil.which("hermes") or "hermes"
# 空なら「最初の ready な Free モデル」を既定にする。
DEFAULT_MODEL = os.environ.get("FREEAGENT_DEFAULT_MODEL", "")
TRACE_ENABLED = _env_on("FREEAGENT_TRACE", True)
STATS_ENABLED = _env_on("FREEAGENT_STATS", True)
RANK_ENABLED = _env_on("FREEAGENT_RANK", True)
SESSIONS_ENABLED = _env_on("FREEAGENT_SESSIONS", True)
COOLDOWN_ENABLED = _env_on("FREEAGENT_COOLDOWN", True)
# クライアント互換の切り分け用: FREEAGENT_DEBUG_LOG=<path> で送受信を 1 行ずつ追記する。
DEBUG_LOG = os.environ.get("FREEAGENT_DEBUG_LOG", "")

# プロバイダ登録。キーが無くても **一覧取得は通る**ものがあるので、一覧は常に集め、
# 推論可否は key の有無で分ける（ready）。
PROVIDER_SPECS: dict[str, dict] = {
    "nous": {
        "base_url": os.environ.get("FREEAGENT_BASE_URL", "http://127.0.0.1:8645/v1"),
        "key": os.environ.get("FREEAGENT_API_KEY", "proxy-attaches-real-credentials"),
        "key_env": None, "free_kind": "pricing", "always_ready": True,
        "note": "hermes proxy（Nous Portal の資格情報を代理付与・キー不要）",
    },
    "openrouter": {
        "base_url": os.environ.get("FREEAGENT_OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        "key": os.environ.get("OPENROUTER_API_KEY", ""),
        "key_env": "OPENROUTER_API_KEY", "free_kind": "pricing", "always_ready": False,
        "note": "OpenRouter の :free SKU（一覧は未認証でも取得可・推論はキー必須）",
    },
    "nvidia": {
        "base_url": os.environ.get("FREEAGENT_NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
        "key": os.environ.get("NVIDIA_API_KEY", ""),
        "key_env": "NVIDIA_API_KEY", "free_kind": "credit", "always_ready": False,
        # 実測: 一覧 82 件の大半が 410（EOL）や 404（アカウントで未有効）で、**一覧を信じると呼べない**。
        # `freeagent_models` の `probe: true` で生存確認してから使うこと。
        "note": "NVIDIA NIM（無料クレジット枠。一覧には廃止・未有効のモデルが混在 → probe で確認）",
    },
    # Hugging Face は OpenAI 互換の **Inference Providers router** を使う。料金はモデルではなく
    # **提供元（novita / together 等）ごと**に付くので、free 判定は提供元単位で行う（実測: 一覧の
    # 各モデルが providers[] を持ち、各要素に pricing と is_free がある）。
    "huggingface": {
        "base_url": os.environ.get("FREEAGENT_HF_BASE_URL", "https://router.huggingface.co/v1"),
        "key": (os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_API_KEY")
                or os.environ.get("HUGGINGFACEHUB_API_TOKEN") or ""),
        "key_env": "HF_TOKEN", "free_kind": "hf_providers", "always_ready": False,
        "note": "Hugging Face Inference Providers（router。一覧は未認証でも取得可・推論はトークン必須）",
    },
}
PROVIDER_ORDER = [n.strip() for n in os.environ.get(
    "FREEAGENT_PROVIDER_ORDER", "nous,openrouter,nvidia,huggingface").split(",")
    if n.strip() in PROVIDER_SPECS] or ["nous"]

# 知識バックエンドの識別用 User-Agent。MediaWiki / OpenAlex / Crossref は連絡先入りの UA を求める。
KB_USER_AGENT = os.environ.get(
    "FREEAGENT_USER_AGENT",
    "hermes-freeagent-bind/0.1 (+https://github.com/loosephoto/hermes-freeagent-bind)")
KB_MAILTO = os.environ.get("FREEAGENT_MAILTO", "")  # OpenAlex / Crossref の polite pool 用（任意）
KB_TTL = _env_float("FREEAGENT_KB_TTL", 1800.0)     # 知識取得のメモリ内 TTL（秒）
KB_TIMEOUT = _env_float("FREEAGENT_KB_TIMEOUT", 20.0)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
# OpenAlex は匿名の検索を提供元側で一時停止することがある（実測: 503 + "Anonymous search is paused"）。
# 無料 API キーを入れると止まらない。単一 work の取得はキー無しでも通る。
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "")

_MODELS_CACHE: dict[str, dict] = {}   # provider -> {"at": float, "rows": [...], "error": str}
_MODELS_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()         # stdout への書き込み直列化
_DEBUG_LOCK = threading.Lock()
_STATE_LOCK = threading.RLock()


def state_dir() -> str:
    """蓄積ストアの置き場。**一時領域に置かない**（予定・統計・クールダウンは消えてはいけない）。"""
    override = os.environ.get("FREEAGENT_STATE_DIR", "")
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, SERVER_NAME)


def _debug(kind: str, payload) -> None:
    if not DEBUG_LOG:
        return
    try:
        line = json.dumps(payload, ensure_ascii=False)
    except Exception:
        line = repr(payload)
    try:
        with _DEBUG_LOCK:
            with open(DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write(f"[{time.strftime('%H:%M:%S')}] {kind} {line[:4000]}\n")
    except Exception:
        pass


# ================================================================ §1 ユーティリティ

def as_int(value, default: int, lo: int, hi: int) -> int:
    """数値引数の防御的変換。`int("?")` のような例外をツールの外へ漏らさない。

    `OverflowError` も捕まえる（実測: `"1e999"` は float にすると inf になり、`int(inf)` が
    OverflowError を投げて**変換関数から例外が漏れた**）。非有限は既定値へ落とす。
    """
    try:
        f = float(value)
        if not math.isfinite(f):
            return default
        n = int(f)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, n))


def as_float(value, default: float) -> float:
    """同じく防御的変換。**非有限（inf / nan）は既定へ落とす**（そのまま API へ送らない）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def as_str(value, default: str = "") -> str:
    return value if isinstance(value, str) and value.strip() else default


def as_flag(value) -> bool:
    """真偽引数。MCP クライアントは bool でも文字列（"true" / "1"）でも送ってくるので両方受ける。"""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def as_str_list(value) -> list[str]:
    """文字列だけを残したリスト（要素が数値などでも落とさない）。"""
    out: list[str] = []
    for item in as_list(value):
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            out.append(str(item))
    return out


def sha1_12(text: str) -> str:
    import hashlib
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def now_ts() -> float:
    return time.time()


def iso_utc(ts: float | None = None) -> str:
    dt = datetime.datetime.fromtimestamp(ts if ts else now_ts(), datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_text(text: str) -> str:
    """比較用の正規化（表層合意度の計算に使う）。"""
    t = (text or "").lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    t = re.sub(r"[、。,.!?！？:：;；\-—_()（）\[\]「」『』\"'`]", "", t)
    return t


def similarity(a: str, b: str) -> float:
    """文字バイグラムの Dice 係数。意味の一致ではなく**表層の一致**である点に注意。"""
    x, y = norm_text(a), norm_text(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if len(x) < 2 or len(y) < 2:
        return 1.0 if x == y else 0.0
    gx = {x[i:i + 2] for i in range(len(x) - 1)}
    gy = {y[i:i + 2] for i in range(len(y) - 1)}
    if not gx or not gy:
        return 0.0
    return round(2 * len(gx & gy) / (len(gx) + len(gy)), 3)


def agreement_of(texts: list[str]) -> float:
    """参加者の回答どうしの平均類似度（0〜1）。「一致したか」であって「正しいか」ではない。"""
    vals = [t for t in texts if t and t.strip()]
    if len(vals) < 2:
        return 0.0
    pairs, total = 0, 0.0
    for i in range(len(vals)):
        for j in range(i + 1, len(vals)):
            total += similarity(vals[i], vals[j])
            pairs += 1
    return round(total / pairs, 3) if pairs else 0.0


def truncate(text: str, limit: int) -> str:
    t = text or ""
    return t if len(t) <= limit else t[:limit] + "…"


# ================================================================ §2 永続ストア
#
# 蓄積するもの: クールダウン（429/404 の記憶）・品質統計・相談セッション・呼び出しトレース。
# すべて**一時領域に置かない**。書き込みは一時ファイル＋os.replace の原子置換で行い、
# 壊れたファイルは無視して空から始める（例外をツールへ漏らさない）。
# トレースと品質統計には**本文を残さない**（外部 API へ送った内容をディスクに置かない方針）。

def _atomic_write(path: str, text: str) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def _load_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


# ---------------------------------------------------------------- §2.1 クールダウン
#
# 前提: 上限に当たっても 429 は「結果が返った後」に観測される。よって 429 は回答の破棄理由ではなく
# **会計イベント**として扱い、Retry-After を尊重した期限を覚えて、その間は代替モデルへ回す。
# ディスクに置く理由（実測）: Free 提供が終わったモデルは 404 を返し続けるが、記憶がプロセス内だけだと
# **再起動のたびに選び直して空振り**する。ディスク側が新しい期限を持つときだけ採用する。

_COOLDOWN: dict[str, dict] = {}
_COOLDOWN_LOADED = False
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_DEFAULT_S = 60.0
_COOLDOWN_MAX_S = 900.0
_UNAVAILABLE_S = 3600.0


def cooldowns_path() -> str:
    return os.environ.get("FREEAGENT_COOLDOWN_PATH") or os.path.join(state_dir(), "cooldowns.json")


def _cooldown_prune(entries: dict, now: float) -> dict:
    return {ref: e for ref, e in entries.items()
            if isinstance(e, dict) and as_float(e.get("until"), 0.0) > now}


def _ensure_cooldown_loaded() -> None:
    global _COOLDOWN_LOADED
    if _COOLDOWN_LOADED or not COOLDOWN_ENABLED:
        return
    with _COOLDOWN_LOCK:
        if _COOLDOWN_LOADED:
            return
        data = _load_json(cooldowns_path(), {})
        saved = data.get("cooldowns") if isinstance(data, dict) else None
        now = now_ts()
        if isinstance(saved, dict):
            for ref, entry in _cooldown_prune(saved, now).items():
                cur = _COOLDOWN.get(ref)
                # ディスク側が新しい期限を持つときだけ採用する（メモリの記憶を巻き戻さない）
                if not cur or as_float(entry.get("until"), 0.0) > as_float(cur.get("until"), 0.0):
                    _COOLDOWN[ref] = entry
        _COOLDOWN_LOADED = True


def _cooldown_save() -> bool:
    if not COOLDOWN_ENABLED:
        return False
    with _COOLDOWN_LOCK:
        payload = {"version": 1, "saved_at": now_ts(),
                   "cooldowns": _cooldown_prune(_COOLDOWN, now_ts())}
    return _atomic_write(cooldowns_path(), json.dumps(payload, ensure_ascii=False))


def _parse_retry_after(value: str | None, now: float | None = None) -> float:
    """Retry-After は秒数と HTTP-date の両方が来る（RFC 9110）。どちらも受ける。"""
    if not value:
        return _COOLDOWN_DEFAULT_S
    raw = str(value).strip()
    if raw.isdigit():
        return min(_COOLDOWN_MAX_S, max(1.0, float(raw)))
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            delta = dt.timestamp() - (now if now is not None else now_ts())
            return min(_COOLDOWN_MAX_S, max(1.0, delta))
    except (TypeError, ValueError):
        pass
    return _COOLDOWN_DEFAULT_S


def note_cooldown(ref: str, secs: float, reason: str) -> float:
    if not COOLDOWN_ENABLED:
        return 0.0
    _ensure_cooldown_loaded()
    until = now_ts() + max(1.0, secs)
    with _COOLDOWN_LOCK:
        cur = _COOLDOWN.get(ref)
        if cur and as_float(cur.get("until"), 0.0) > until:
            return as_float(cur.get("until"), 0.0)  # 既存の方が長いなら伸ばさない
        _COOLDOWN[ref] = {"until": until, "reason": reason, "retry_after_s": round(secs, 1)}
    _cooldown_save()
    return until


def note_rate_limited(ref: str, retry_after: str | None = None, reason: str = "HTTP 429") -> float:
    return note_cooldown(ref, _parse_retry_after(retry_after), reason)


def note_unavailable(ref: str, status: int) -> float:
    """404/410 = モデル消滅・Free 提供終了。1 時間外す（復活していれば期限切れで戻る）。"""
    return note_cooldown(ref, _UNAVAILABLE_S, f"HTTP {status}（モデル消滅/Free終了）")


def cooling_refs() -> dict[str, dict]:
    _ensure_cooldown_loaded()
    now = now_ts()
    with _COOLDOWN_LOCK:
        return _cooldown_prune(_COOLDOWN, now)


def is_cooling(ref: str) -> bool:
    return ref in cooling_refs()


# ---------------------------------------------------------------- §2.5 プロバイダ認証の記憶
#
# 実測: HF のトークンに推論権限が無いと **無料候補 3 件すべてが 403**、OpenRouter の一部の `:free` も
# 提供元側の制限で 403 になる。これを記憶しないと、パネルを組むたびに同じプロバイダを引き当て、
# 失敗 → 代替へ回る分だけ呼び出しと待ち時間を浪費する（実測: 4 体選抜のうち 3 体が HF、かつ既知の
# 死んだ NVIDIA モデル）。**自動選抜からだけ**外し、明示指定は常に試す（キーを直せば即復帰）。

_AUTH: dict[str, dict] = {}
_AUTH_LOADED = False
_AUTH_LOCK = threading.Lock()
AUTH_TTL_S = _env_float("FREEAGENT_AUTH_TTL", 900.0)


def auth_path() -> str:
    return os.environ.get("FREEAGENT_AUTH_PATH") or os.path.join(state_dir(), "provider_auth.json")


def _auth_prune(entries: dict, now: float) -> dict:
    return {p: e for p, e in entries.items()
            if isinstance(e, dict) and as_float(e.get("until"), 0.0) > now}


def _ensure_auth_loaded() -> None:
    global _AUTH_LOADED
    if _AUTH_LOADED:
        return
    with _AUTH_LOCK:
        if _AUTH_LOADED:
            return
        data = _load_json(auth_path(), {})
        saved = data.get("providers") if isinstance(data, dict) else None
        if isinstance(saved, dict):
            _AUTH.update(_auth_prune(saved, now_ts()))
        _AUTH_LOADED = True


def _auth_save() -> bool:
    with _AUTH_LOCK:
        payload = {"version": 1, "saved_at": now_ts(),
                   "providers": _auth_prune(_AUTH, now_ts())}
    return _atomic_write(auth_path(), json.dumps(payload, ensure_ascii=False))


def note_provider_auth(provider: str, status: int, detail: str = "") -> float:
    """プロバイダ単位の認証失敗を覚える。既存の期限が長いときは伸ばさない。"""
    if not provider:
        return 0.0
    _ensure_auth_loaded()
    until = now_ts() + max(1.0, AUTH_TTL_S)
    with _AUTH_LOCK:
        cur = _AUTH.get(provider)
        if cur and as_float(cur.get("until"), 0.0) > until:
            return as_float(cur.get("until"), 0.0)
        _AUTH[provider] = {"until": until, "status": int(status), "at": now_ts(),
                           "detail": truncate(detail or "", 200)}
    _auth_save()
    return until


def clear_provider_auth(provider: str) -> None:
    """呼び出しが通ったら記憶を消す（キーを直したのに古い記憶で避け続けるのを防ぐ）。"""
    _ensure_auth_loaded()
    with _AUTH_LOCK:
        if provider not in _AUTH:
            return
        _AUTH.pop(provider, None)
    _auth_save()


def provider_auth_blocked(provider: str) -> dict | None:
    """認証失敗の記憶が生きていればその内容を返す（自動選抜の除外判断に使う）。"""
    _ensure_auth_loaded()
    now = now_ts()
    with _AUTH_LOCK:
        entry = _AUTH.get(provider)
        if not entry or as_float(entry.get("until"), 0.0) <= now:
            return None
        return dict(entry)


# ---------------------------------------------------------------- §2.2 品質統計
#
# 「どの Free モデルが実際に使えるか」を実測から学ぶ。記録するのはメタデータだけ:
# 形式適合(ok)・CoT混入(leak)・切断(trunc)・空(empty)・エラー分類(err)・レイテンシ。
# 本文は保存しない。観測は半減期で減衰させ、古い観測が選択を支配しないようにする。

_STATS: dict = {"models": {}}
_STATS_LOADED = False
_STATS_LOCK = threading.Lock()
_STATS_HALFLIFE_DAYS = max(0.5, _env_float("FREEAGENT_STATS_HALFLIFE_DAYS", 14.0))
STATS_MAX_REFS = 400
STATS_STALE_DAYS = 60.0

_KIND_KEYS = ("n", "ok", "leak", "trunc", "empty")


def stats_path() -> str:
    return os.environ.get("FREEAGENT_STATS_PATH") or os.path.join(state_dir(), "model_stats.json")


def _empty_kind() -> dict:
    row = {k: 0.0 for k in _KIND_KEYS}
    row["err"] = {}
    row["lat_ms"] = 0.0
    return row


def _decay(row: dict, factor: float) -> None:
    for k in _KIND_KEYS:
        row[k] = as_float(row.get(k), 0.0) * factor
    row["lat_ms"] = as_float(row.get("lat_ms"), 0.0) * factor


def _ensure_stats_loaded() -> None:
    global _STATS_LOADED
    if _STATS_LOADED or not STATS_ENABLED:
        return
    with _STATS_LOCK:
        if _STATS_LOADED:
            return
        data = _load_json(stats_path(), {})
        models = data.get("models") if isinstance(data, dict) else None
        _STATS["models"] = models if isinstance(models, dict) else {}
        _STATS_LOADED = True


def _stats_save() -> bool:
    if not STATS_ENABLED:
        return False
    with _STATS_LOCK:
        payload = {"version": 1, "models": _STATS["models"]}
    return _atomic_write(stats_path(), json.dumps(payload, ensure_ascii=False))


def classify_error(err: str) -> str:
    """エラー文字列を分類する（集計の粒度を揃えるため、ここで語彙を固定する）。"""
    t = (err or "").lower()
    if "429" in t or "rate" in t or "quota" in t:
        return "rate_limited"
    if "timeout" in t or "timed out" in t:
        return "timeout"
    if "404" in t or "410" in t or "not found" in t or "gone" in t:
        return "gone"
    if "401" in t or "403" in t or "unauthor" in t or "forbidden" in t:
        return "auth"
    if "5" == t[:1] and t[1:2].isdigit():
        return "server"
    # 自前のエラー文字列は "HTTP 503: ..." の形なので、**先頭が 5 かの判定だけでは拾えない**（実測:
    # classify_error("HTTP 503") が other に落ち、5xx が統計上「その他」に混ざっていた）。
    if "http" in t and re.search(r"\b5\d\d\b", t):
        return "server"
    return "other"


def note_observation(ref: str, kind: str, *, error: str = "", leak: bool = False,
                     trunc: bool = False, empty: bool = False, lat_ms: float = 0.0) -> None:
    """1 回の呼び出し結果を記録する（ok は「エラーが無く、空でもなく、切断もされていない」）。"""
    if not STATS_ENABLED:
        return
    _ensure_stats_loaded()
    with _STATS_LOCK:
        entry = _STATS["models"].setdefault(ref, {"kinds": {}})
        row = entry["kinds"].setdefault(kind, _empty_kind())
        row["n"] = as_float(row.get("n"), 0.0) + 1.0
        if error:
            cls = classify_error(error)
            err = row.setdefault("err", {})
            err[cls] = int(err.get(cls) or 0) + 1
        else:
            if leak:
                row["leak"] = as_float(row.get("leak"), 0.0) + 1.0
            elif trunc:
                row["trunc"] = as_float(row.get("trunc"), 0.0) + 1.0
            elif empty:
                row["empty"] = as_float(row.get("empty"), 0.0) + 1.0
            else:
                row["ok"] = as_float(row.get("ok"), 0.0) + 1.0
        if lat_ms:
            row["lat_ms"] = as_float(row.get("lat_ms"), 0.0) + lat_ms


def _kind_quality(row: dict) -> tuple[float, float]:
    """(品質 0〜1, 観測数) を返す。切断・CoT混入・空を減点し、エラー率を掛ける。"""
    n = as_float(row.get("n"), 0.0)
    if n <= 0:
        return 0.0, 0.0
    ok = as_float(row.get("ok"), 0.0)
    leak = as_float(row.get("leak"), 0.0)
    trunc = as_float(row.get("trunc"), 0.0)
    empty = as_float(row.get("empty"), 0.0)
    score = (ok + 0.5 * trunc + 0.3 * leak) / n
    score *= (1.0 - min(1.0, empty / n))
    err = row.get("err") or {}
    err_total = sum(int(v) for v in err.values()) if isinstance(err, dict) else 0
    score *= max(0.0, 1.0 - err_total / n)
    return round(max(0.0, min(1.0, score)), 4), n


def model_quality(ref: str, kind: str | None = None) -> float:
    _ensure_stats_loaded()
    entry = _STATS["models"].get(ref) or {}
    kinds = entry.get("kinds") or {}
    if kind and kind in kinds:
        return _kind_quality(kinds[kind])[0]
    vals = [_kind_quality(row)[0] for row in kinds.values()]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def model_observations(ref: str) -> float:
    _ensure_stats_loaded()
    kinds = (_STATS["models"].get(ref) or {}).get("kinds") or {}
    return sum(as_float(row.get("n"), 0.0) for row in kinds.values())


def model_status(ref: str) -> str:
    """ok / watch / degraded / unobserved。選択の説明に使う（数値を丸めずに渡す）。"""
    n = model_observations(ref)
    if n <= 0:
        return "unobserved"
    q = model_quality(ref)
    if q >= 0.6:
        return "ok"
    if q >= 0.3:
        return "watch"
    return "degraded"


def rank_models(refs: list[str]) -> list[str]:
    """品質順。観測が少ないモデルへ探索ボーナスを与え、新しい Free モデルが試されない問題を防ぐ。"""
    if not RANK_ENABLED:
        return list(refs)
    total = sum(model_observations(r) for r in refs) or 1.0

    def score(ref: str) -> float:
        n = model_observations(ref)
        q = model_quality(ref) if n else 0.5
        explore = 0.25 * (1.0 / (1.0 + n)) * (1.0 + 1.0 / (1.0 + total))
        return q + explore

    return sorted(refs, key=lambda r: (-score(r), r))


def diverse_order(refs: list[str]) -> list[str]:
    """プロバイダを巡回させて並べる。

    品質観測が無いモデルは同点になるため、素の rank 順だと **ID のアルファベット順**で並び、
    `huggingface/...` のような早い名前のプロバイダが枠を独占する（実測: 4 体選抜のうち 3 体が HF）。
    パネルの意味は多様性なので、プロバイダ交互に取り、プロバイダの順序は最良モデルの順位で決める
    （品質順を捨てない）。
    """
    buckets: dict[str, list[str]] = {}
    for ref in refs:
        buckets.setdefault(ref.split("/", 1)[0], []).append(ref)
    ranked_index = {ref: i for i, ref in enumerate(refs)}
    order = sorted(buckets, key=lambda p: ranked_index[buckets[p][0]])
    out: list[str] = []
    while any(buckets.get(p) for p in order):
        for provider in order:
            pool = buckets.get(provider) or []
            if pool:
                out.append(pool.pop(0))
    return out


def select_models(size: int, requested: list[str] | None = None, *,
                  prefer: list[str] | None = None, exclude: list[str] | None = None,
                  free_only: bool = True) -> tuple[list[str], dict]:
    """参加モデルを決める。明示 > prefer > 品質統計 の順に効かせる（選んだ根拠を返す）。"""
    available = free_model_refs(free_only=free_only)
    if not available:
        return [], {"reason": "no_models", "available": 0}
    avail_set = set(available)
    notes: list[str] = []
    chosen: list[str] = []

    def add(refs: list[str]) -> None:
        for ref in refs:
            if ref in avail_set and ref not in chosen:
                chosen.append(ref)

    if requested:
        unknown = [r for r in requested if r not in avail_set]
        add(requested)
        if unknown:
            notes.append(f"未知/未提供のモデルを除外: {', '.join(unknown[:5])}")
        cooling_requested = [r for r in chosen if is_cooling(r)]
        if cooling_requested:
            notes.append(f"指定モデルがクールダウン中（再試行で回復します）: {', '.join(cooling_requested[:3])}")
    if prefer:
        add(prefer)
    if len(chosen) < size:
        pool = [r for r in available if r not in chosen]
        if exclude:
            ex = set(exclude)
            pool = [r for r in pool if r not in ex]
        # クールダウン中を**先に選ばない**（除外ではなく後回し）。選んだ直後に 429 が記録されると
        # 「全候補がクールダウン中」で 1 体へ縮退し、失敗に見える（実測）。空きが足りないときだけ補充する。
        cooling = cooling_refs()
        # **認証で駄目だったプロバイダを自動選抜から外す**（実測: HF の権限不足で 3 体が空振りし、
        # 代替へ回る分だけ遅くなる）。明示 `requested` は上の add() で既に入っているので影響しない。
        auth_blocked = {p: provider_auth_blocked(p) for p in PROVIDER_ORDER}
        blocked = {p for p, e in auth_blocked.items() if e}
        if blocked:
            pool = [r for r in pool if r.split("/", 1)[0] not in blocked]
            notes.append("認証で失敗中のため自動選抜から除外: " + ", ".join(sorted(blocked))
                         + "（明示指定すれば試します。キー/権限を直せば数分で戻ります）")
        ready = [r for r in pool if r not in cooling]
        waiting = [r for r in pool if r in cooling]
        need = size - len(chosen)
        picked = diverse_order(rank_models(ready))[:need]
        if len(picked) < need and waiting:
            picked += diverse_order(rank_models(waiting))[:need - len(picked)]
            notes.append(f"空きが足りずクールダウン中から補充: {', '.join(waiting[:3])}")
        chosen.extend(picked)
    if exclude:
        ex = set(exclude)
        chosen = [r for r in chosen if r not in ex]

    chosen = chosen[:size]
    info = {
        "requested": bool(requested),
        "prefer": as_str_list(prefer),
        "exclude": as_str_list(exclude),
        "available": len(available),
        "ranking": RANK_ENABLED,
        "status": {r: model_status(r) for r in chosen},
        "notes": notes,
    }
    return chosen, info


# ---------------------------------------------------------------- §2.3 トレース
#
# 1 行 1 呼び出しのメタデータ（本文なし）。上限を超えたら 1 世代だけ退避する。

def trace_path() -> str:
    return os.environ.get("FREEAGENT_TRACE_PATH") or os.path.join(state_dir(), "traces.jsonl")


_TRACE_MAX_BYTES = _env_int("FREEAGENT_TRACE_MAX_BYTES", 5_000_000)


def trace_call(kind: str, ref: str, ok: bool, *, error: str = "", served_by: str = "",
               rate_limited=None, unavailable=None, skipped_cooling=None,
               answer_len: int = 0, answer_sha1: str = "", truncated: bool = False,
               latency_s: float = 0.0, tokens: dict | None = None) -> None:
    if not TRACE_ENABLED:
        return
    row = {"kind": kind, "ref": ref, "ok": bool(ok), "error": error or None,
           "error_class": classify_error(error) if error else None,
           "served_by": served_by or ref, "rate_limited": rate_limited,
           "unavailable": unavailable, "skipped_cooling": skipped_cooling,
           "answer_len": int(answer_len or 0), "answer_sha1": answer_sha1 or "",
           "truncated": bool(truncated), "latency_s": round(as_float(latency_s, 0.0), 3),
           "tokens": tokens or {}, "ts": now_ts()}
    path = trace_path()
    try:
        with _STATE_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) > _TRACE_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def observe_call(result: dict, kind: str, text: str = "") -> None:
    """呼び出し結果を統計とトレースへ流す（1 か所に集約し、記録漏れを防ぐ）。"""
    ref = as_str(result.get("ref")) or as_str(result.get("model"))
    if not ref:
        return
    error = as_str(result.get("error"))
    note_observation(ref, kind,
                     error=error,
                     leak=bool(result.get("cot_leak")),
                     trunc=bool(result.get("truncated")),
                     empty=not (text or "").strip() and not error,
                     lat_ms=as_float(result.get("latency_s"), 0.0) * 1000.0)
    trace_call(kind, ref, not error, error=error,
               served_by=as_str(result.get("served_by")) or ref,
               rate_limited=result.get("rate_limited"),
               unavailable=result.get("unavailable"),
               skipped_cooling=result.get("skipped_cooling"),
               answer_len=len(text or ""), answer_sha1=sha1_12(text or ""),
               truncated=bool(result.get("truncated")),
               latency_s=as_float(result.get("latency_s"), 0.0),
               tokens=result.get("tokens") or {})


# ---------------------------------------------------------------- §2.4 相談セッション
#
# 双方向相談（サブがメインへ問い返す → メインが答えて同じセッションを再開する）の**進行状態**。
# 知識は蓄積しない（保存するのは作業状態だけ）。永続化する理由: セッションがプロセス内だけだと
# **再起動でメインが答える前に消え**、往復そのものが成立しない（利用者からは「相談が途中で切れる」と
# しか見えない）。原子置換 + TTL + 上限で prune し、期限切れは復元しない（古い相談を復活させない）。

_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOADED = False
_SESSIONS_LOCK = threading.Lock()
SESSION_TTL_S = _env_float("FREEAGENT_SESSION_TTL", 3600.0)
SESSION_MAX = _env_int("FREEAGENT_SESSION_MAX", 32)


def sessions_path() -> str:
    return os.environ.get("FREEAGENT_SESSIONS_PATH") or os.path.join(state_dir(), "sessions.json")


def _sessions_prune(data: dict, now: float) -> dict:
    alive = {k: v for k, v in data.items()
             if isinstance(v, dict) and as_float(v.get("updated_at"), 0.0) > now - SESSION_TTL_S}
    if len(alive) > SESSION_MAX:  # 新しい順に上限まで残す
        keep = sorted(alive, key=lambda k: as_float(alive[k].get("updated_at"), 0.0), reverse=True)
        alive = {k: alive[k] for k in keep[:SESSION_MAX]}
    return alive


def _ensure_sessions_loaded() -> None:
    global _SESSIONS_LOADED
    if _SESSIONS_LOADED or not SESSIONS_ENABLED:
        return
    with _SESSIONS_LOCK:
        if _SESSIONS_LOADED:
            return
        data = _load_json(sessions_path(), {})
        rows = data.get("sessions") if isinstance(data, dict) else None
        _SESSIONS.update(_sessions_prune(rows if isinstance(rows, dict) else {}, now_ts()))
        _SESSIONS_LOADED = True


def _sessions_save() -> bool:
    if not SESSIONS_ENABLED:
        return False
    with _SESSIONS_LOCK:
        payload = {"version": 1, "saved_at": now_ts(),
                   "sessions": _sessions_prune(_SESSIONS, now_ts())}
    return _atomic_write(sessions_path(), json.dumps(payload, ensure_ascii=False))


def new_session_id() -> str:
    return f"s{int(now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}"


def session_get(sid: str) -> dict | None:
    if not sid:
        return None
    _ensure_sessions_loaded()
    now = now_ts()
    with _SESSIONS_LOCK:
        row = _SESSIONS.get(sid)
        if not row or as_float(row.get("updated_at"), 0.0) <= now - SESSION_TTL_S:
            _SESSIONS.pop(sid, None)
            return None
        return json.loads(json.dumps(row))  # 呼び出し側の書き換えが他へ波及しないよう複製を渡す


def session_put(sid: str, row: dict) -> None:
    if not SESSIONS_ENABLED or not sid:
        return
    _ensure_sessions_loaded()
    row = dict(row or {})
    row["updated_at"] = now_ts()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = row
    _sessions_save()


def session_drop(sid: str) -> None:
    _ensure_sessions_loaded()
    with _SESSIONS_LOCK:
        _SESSIONS.pop(sid, None)
    _sessions_save()


# ================================================================ §3 プロバイダとモデル

def provider_ready(name: str) -> bool:
    spec = PROVIDER_SPECS.get(name) or {}
    return bool(spec.get("always_ready")) or bool(spec.get("key"))


def make_ref(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def split_ref(ref: str) -> tuple[str, str]:
    """'openrouter/qwen/qwen3.8-27b:free' → ('openrouter', 'qwen/qwen3.8-27b:free')。"""
    ref = as_str(ref)
    head, _, rest = ref.partition("/")
    if head in PROVIDER_SPECS and rest:
        return head, rest
    return "", ref


def _urlopen(req: urllib.request.Request, timeout: float):
    """(connect, read) のタイムアウトを必ず与える。遮断されたホストで分単位に固まらないため。"""
    return urllib.request.urlopen(req, timeout=timeout)


def provider_http(path: str, provider: str = "nous", payload: dict | None = None,
                  timeout: float | None = None) -> dict:
    spec = PROVIDER_SPECS[provider]
    url = f"{spec['base_url'].rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {spec['key']}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with _urlopen(req, timeout or READ_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _is_hf(provider: str) -> bool:
    """HF（Inference Providers router）か。料金と文脈長が**提供元ごと**という違いを 1 か所で判定する。"""
    return (PROVIDER_SPECS.get(provider) or {}).get("free_kind") == "hf_providers"


def _free_providers(row: dict) -> list[str]:
    """HF の一覧で**無料で使える提供元**を列挙する（`is_free` か、価格が全部 0 のもの）。

    実測: HF の `/v1/models` は各モデルに `providers[]` を持ち、要素は
    `{provider, status, context_length, pricing:{input,output}, is_free, ...}`。停止中（status が
    live 以外）の提供元は数えない（数えると「無料で使える」と嘘をつくことになる）。
    """
    out: list[str] = []
    for p in (row.get("providers") or []):
        if not isinstance(p, dict) or p.get("status") != "live":
            continue
        name = as_str(p.get("provider"))
        if not name:
            continue
        if p.get("is_free") is True:
            out.append(name)
            continue
        pricing = p.get("pricing") or {}
        vals = [float(pricing[k]) for k in ("input", "output")
                if isinstance(pricing.get(k), (int, float))]
        if vals and all(v == 0.0 for v in vals):
            out.append(name)
    return out


def _is_free(provider: str, row: dict) -> bool:
    """Free 判定。NIM は「無料クレジット枠」なので全モデルが対象、HF は提供元単位、他は pricing が 0。"""
    kind = (PROVIDER_SPECS.get(provider) or {}).get("free_kind")
    if kind == "credit":
        return True
    if _is_hf(provider):
        return bool(_free_providers(row))
    if str(row.get("id") or "").endswith(":free"):
        return True
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return False
    vals = []
    for key in ("prompt", "completion", "input", "output", "request"):
        if key not in pricing or pricing.get(key) is None:
            continue
        try:
            vals.append(float(pricing[key]))
        except (TypeError, ValueError):
            return False
    return bool(vals) and all(v == 0.0 for v in vals)


def _context_length(provider: str, raw: dict):
    """文脈長。HF は**トップレベルに無く提供元ごと**にあるので最大値を採る（実測）。"""
    if _is_hf(provider):
        lens = [p.get("context_length") for p in (raw.get("providers") or [])
                if isinstance(p, dict) and isinstance(p.get("context_length"), (int, float))]
        return int(max(lens)) if lens else None
    return raw.get("context_length") or raw.get("context_window")


def fetch_provider_models(provider: str, ttl: float = 600.0) -> list[dict]:
    """モデル一覧。Free モデルは入れ替わるので**固定しない**（毎回ここから解決する）。"""
    with _MODELS_LOCK:
        cached = _MODELS_CACHE.get(provider)
        if cached and now_ts() - as_float(cached.get("at"), 0.0) < ttl:
            return list(cached.get("rows") or [])
    rows: list[dict] = []
    error = ""
    try:
        data = provider_http("/models", provider=provider, timeout=min(30.0, READ_TIMEOUT))
        for raw in (data.get("data") or []):
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            rows.append({
                "id": str(raw.get("id")),
                "provider": provider,
                "free": _is_free(provider, raw),
                # 無料で使える**経路**（HF は提供元名。他プロバイダでは空）。呼び出しのヒントになる。
                "free_via": _free_providers(raw) if _is_hf(provider) else [],
                "context_length": _context_length(provider, raw),
                "pricing": raw.get("pricing") or {},
                "access": raw.get("access"),
            })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    with _MODELS_LOCK:
        _MODELS_CACHE[provider] = {"at": now_ts(), "rows": rows, "error": error}
    return rows


def all_models(ttl: float = 600.0) -> list[dict]:
    out: list[dict] = []
    for provider in PROVIDER_ORDER:
        out.extend(fetch_provider_models(provider, ttl=ttl))
    return out


def provider_status() -> list[dict]:
    rows = []
    for provider in PROVIDER_ORDER:
        models = fetch_provider_models(provider)
        with _MODELS_LOCK:
            error = (_MODELS_CACHE.get(provider) or {}).get("error") or ""
        rows.append({
            "provider": provider,
            "ready": provider_ready(provider),
            "models": len(models),
            "free": sum(1 for m in models if m.get("free")),
            "key_env": (PROVIDER_SPECS.get(provider) or {}).get("key_env"),
            "note": (PROVIDER_SPECS.get(provider) or {}).get("note"),
            "error": error,
        })
    return rows


def free_model_refs(free_only: bool = True) -> list[str]:
    """推論可能（ready）なプロバイダの Free モデル参照を、優先順で返す。"""
    refs: list[str] = []
    for provider in PROVIDER_ORDER:
        if not provider_ready(provider):
            continue
        for row in fetch_provider_models(provider):
            if free_only and not row.get("free"):
                continue
            ref = make_ref(provider, row["id"])
            if ref not in refs:
                refs.append(ref)
    return refs


def default_model(free_only: bool = True) -> str:
    if DEFAULT_MODEL:
        return DEFAULT_MODEL
    refs = free_model_refs(free_only=free_only)
    return refs[0] if refs else ""


def resolve_ref(ref: str, free_only: bool = True) -> tuple[str, str]:
    """参照から (provider, model) を決める。未指定なら最初の ready な Free モデル。"""
    provider, model = split_ref(ref)
    if provider:
        return provider, model
    if model:
        for name in PROVIDER_ORDER:
            if any(r["id"] == model for r in fetch_provider_models(name)):
                return name, model
        return PROVIDER_ORDER[0], model
    return split_ref(default_model(free_only=free_only))


# ================================================================ §4 サブLLM呼び出し
#
# 失敗の扱い（旧実装の実測を継承）:
#   * モデルは**消える**（Free 提供終了で 404/410）。だから参照を固定せず、呼び出しのたびに一覧から
#     解決し、失敗したら代替へ回す。落とす条件は 404/410・429・401/403・5xx。
#   * 429 は「結果が返った後」に観測される会計イベント。Retry-After を尊重して記憶し、その間は代替へ。
#   * 遮断（接続不可・403）はホスト単位で記憶して fail fast する。素の呼び出しを投げると 1 回で
#     分単位に固まり、並列で走っている他の呼び出しまで待たされる（実測: 120 秒以上無応答）。

_FALLBACK_STATUS = {400, 401, 403, 404, 410, 429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 4


# 空応答の再試行（実測: max_tokens=220 で 3 体中 2 体が空。思考トークンで予算を使い切るモデルがある）。
# 空を「回答」として返すとメインLLMが無回答を回答と誤解するので、**成功扱いにしない**。
_EMPTY_TOKEN_FLOOR = _env_int("FREEAGENT_EMPTY_TOKEN_FLOOR", 512)
_EMPTY_RETRY_CAP = _env_int("FREEAGENT_EMPTY_TOKEN_CAP", 2048)


class HttpStatusError(Exception):
    def __init__(self, status: int, body: str = "", retry_after: str | None = None):
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


def _cot_leak(text: str) -> bool:
    """CoT 混入の**ヒューリスティック**検出。

    改行数で判定しない（ラベル付きの複数行出力は正当な回答であり、改行数だけでは漏れと区別できない
    ＝実測で誤判定した）。前置き・思考の宣言・思考の見出しといった**マーカー**だけを見る。
    用途は統計での減点のみで、回答を捨てる理由にはしない。
    """
    t = (text or "").strip()
    if not t:
        return False
    head = t[:300].lower()
    markers = ("let me think", "let's think", "the user wants", "the user asks", "the user is asking",
               "i need to", "i should", "we need to", "okay, i", "analyze the request",
               "step 1:", "ステップ1", "まず、", "まず考え", "考えます", "分析します", "要求を理解",
               "回答を作成", "以下が回答です。まず")
    return any(m in head for m in markers)


def _extract_text(data: dict) -> str:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message") or {}
    text = msg.get("content")
    if isinstance(text, list):  # 一部プロバイダは content をパート配列で返す
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return text if isinstance(text, str) else ""


def _call_once(provider: str, model: str, prompt: str, system: str,
               max_tokens: int, temperature: float | None,
               timeout: float | None = None) -> dict:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if temperature is not None:
        payload["temperature"] = temperature
    started = now_ts()
    try:
        data = provider_http("/chat/completions", provider=provider, payload=payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        raise HttpStatusError(int(exc.code), body, exc.headers.get("Retry-After") if exc.headers else None)
    latency = now_ts() - started
    text = _extract_text(data)
    finish = ""
    try:
        finish = (data.get("choices") or [{}])[0].get("finish_reason") or ""
    except Exception:
        finish = ""
    usage = data.get("usage") or {}
    return {"text": text, "latency_s": round(latency, 3), "finish_reason": finish,
            "truncated": finish == "length",
            "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}}


def _candidates(ref: str, free_only: bool = True) -> list[str]:
    """冷却中なら代替を並べる（同じ ref を先頭に残し、他の Free モデルを後ろに足す）。"""
    cooling = cooling_refs()
    out = [ref] if ref and ref not in cooling else []
    if not out:
        return []
    if len(out) >= _MAX_ATTEMPTS:
        return out[:_MAX_ATTEMPTS]
    for other in free_model_refs(free_only=free_only):
        if other == ref or other in cooling or other in out:
            continue
        out.append(other)
        if len(out) >= _MAX_ATTEMPTS:
            break
    return out


def _auth_hint(provider: str, status: int, body: str) -> str:
    """認証エラーは**原因と直し方**を返す（「すべての候補で失敗しました」だけでは直しようがない）。

    実測: HF の既存トークンは有効でも `403 This authentication method does not have sufficient
    permissions to call Inference Providers` を返す（fine-grained トークンに推論権限が無い）。
    """
    tail = truncate((body or "").replace("\n", " "), 160)
    if provider == "huggingface":
        if "inference providers" in (body or "").lower():
            return ("HTTP 403（huggingface）: トークンに Inference Providers の権限がありません。"
                    "https://huggingface.co/settings/tokens で「Make calls to Inference Providers」を"
                    "有効にしたトークンを作り、env の HF_TOKEN に設定してください"
                    f" / 応答: {tail}")
        return ("HTTP 401/403（huggingface）: HF_TOKEN が未設定か無効です。"
                f"Inference Providers の権限があるトークンを設定してください / 応答: {tail}")
    hints = {
        "openrouter": ("OPENROUTER_API_KEY が未設定か無効です" if status == 401 else
                       "キーは有効ですが、このモデルを使う権限・プランがありません"
                       "（`:free` でも提供元側の制限で 403 になるものがあります）"),
        "nvidia": ("NVIDIA_API_KEY が未設定か無効です" if status == 401 else
                   "このモデルはアカウントで有効化されていません（一覧に出ても呼べないものがあります）"),
    }
    hint = hints.get(provider, "キーまたは権限を確認してください")
    return f"HTTP {status}（{provider}）: {hint} / 応答: {tail}"


def call_model(ref: str, prompt: str, *, system: str = "", max_tokens: int = 800,
               temperature: float | None = None, kind: str = "ask",
               allow_fallback: bool = True, free_only: bool = True,
               timeout: float | None = None) -> dict:
    """1 つのサブLLM呼び出し。**例外を外へ漏らさず**、失敗も dict で返す。"""
    provider, model = resolve_ref(ref, free_only=free_only)
    if not model:
        return {"error": "モデルが解決できませんでした（Free モデルが 0 件の可能性）",
                "ref": ref, "kind": kind}
    ref = make_ref(provider, model)
    cooling = cooling_refs()
    attempts = _candidates(ref, free_only=free_only) if allow_fallback else (
        [ref] if ref not in cooling else [])
    if not attempts:
        return {"error": f"全候補がクールダウン中です（{ref}）", "ref": ref, "kind": kind,
                "skipped_cooling": [ref], "rate_limited": bool(ref in cooling)}

    last_error = ""
    skipped: list[str] = []
    for idx, cand in enumerate(attempts):
        c_provider, c_model = resolve_ref(cand, free_only=free_only)
        if not c_model:
            continue
        try:
            result = _call_once(c_provider, c_model, prompt, system, max_tokens, temperature,
                                timeout=timeout)
        except HttpStatusError as exc:
            last_error = f"HTTP {exc.status}: {exc.body[:200]}"
            if exc.status == 429:
                until = note_rate_limited(cand, exc.retry_after)
                skipped.append(cand)
                _debug("rate_limited", {"ref": cand, "until": until})
            elif exc.status in (404, 410):
                note_unavailable(cand, exc.status)
                skipped.append(cand)
            elif exc.status in (401, 403):
                # キー不備・権限なし。プロバイダ単位で覚えて**自動選抜から外す**（明示指定では再挑戦できる）。
                # 原因を last_error に残す: 空のままだと「すべての候補で失敗しました」しか出ず直しようがない。
                last_error = _auth_hint(c_provider, exc.status, exc.body)
                note_provider_auth(c_provider, exc.status, exc.body or last_error)
                _debug("auth_error", {"ref": cand, "status": exc.status})
            if exc.status not in _FALLBACK_STATUS:
                break
            continue
        except Exception as exc:  # 接続不可・タイムアウト・JSON 壊れ
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        text = result["text"]
        if not text.strip():
            # 空応答。**成功として返さない**（メインLLMが「無回答」を回答と誤解する）。思考トークンで
            # 予算を使い切った可能性が高いので、予算を上げて 1 回だけ同じ候補で引き上げる。
            if max_tokens < _EMPTY_RETRY_CAP:
                bumped = min(max(max_tokens * 3, _EMPTY_TOKEN_FLOOR), _EMPTY_RETRY_CAP)
                try:
                    retry = _call_once(c_provider, c_model, prompt, system, bumped, temperature,
                                       timeout=timeout)
                    if (retry.get("text") or "").strip():
                        result, text = retry, retry["text"]
                except Exception:
                    pass
            if not text.strip():
                last_error = (f"空応答（{cand}）。max_tokens={max_tokens} を思考トークンで使い切った"
                              "可能性があります。max_tokens を増やしてください")
                observe_call({"ref": cand, "error": last_error}, kind, "")
                skipped.append(cand)
                continue

        out = {"ref": cand, "served_by": cand, "model": c_model, "provider": c_provider,
               "text": text, "latency_s": result["latency_s"],
               "truncated": result["truncated"], "tokens": result["tokens"],
               "cot_leak": _cot_leak(text), "kind": kind,
               "fallback": cand != ref, "skipped_cooling": skipped or None}
        clear_provider_auth(c_provider)   # 通ったら「認証で駄目」の記憶を消す
        observe_call(out, kind, text)
        return out

    fail = {"error": last_error or "すべての候補で失敗しました", "ref": ref, "kind": kind,
            "skipped_cooling": skipped or None,
            "rate_limited": any(r in cooling_refs() for r in attempts)}
    observe_call(fail, kind, "")
    return fail


def run_parallel(jobs: list, worker, max_workers: int | None = None) -> list:
    """jobs を並列に処理して**入力順**で返す。並列化が効くのは I/O 待ち（API 待ち）である点に注意。"""
    if not jobs:
        return []
    out: list = [None] * len(jobs)
    workers = max(1, min(max_workers or MAX_WORKERS, len(jobs)))
    if workers == 1:
        for i, job in enumerate(jobs):
            try:
                out[i] = worker(job)
            except Exception as exc:  # worker 側の例外もここで吸収する
                out[i] = {"error": f"{type(exc).__name__}: {exc}"}
        return out
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(worker, job): i for i, job in enumerate(jobs)}
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as exc:
                out[i] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


def ask_many(refs: list[str], prompt: str, *, system: str = "", max_tokens: int = 800,
             temperature: float | None = None, kind: str = "panel") -> list[dict]:
    """同じプロンプトを複数モデルへ**同時に**投げる（1 ターン待たずに走るのが並列の利点）。"""
    return run_parallel(
        list(refs),
        lambda ref: call_model(ref, prompt, system=system, max_tokens=max_tokens,
                               temperature=temperature, kind=kind),
        max_workers=min(len(refs), MAX_WORKERS))


def ask_map(pairs: list[tuple[str, str]], *, system: str = "", max_tokens: int = 800,
            temperature: float | None = None, kind: str = "fanout") -> list[dict]:
    """(ref, prompt) の組を並列に処理する。"""
    return run_parallel(
        list(pairs),
        lambda pair: call_model(pair[0], pair[1], system=system, max_tokens=max_tokens,
                                temperature=temperature, kind=kind),
        max_workers=min(len(pairs), MAX_WORKERS))


# ================================================================ §5 知識バックエンド
#
# メイン／サブLLM の知識補助。**LLM を使わない**（幻覚が入らない）経路として §6 の lookup が使う。
# 提供元: arXiv / Crossref / OpenAlex / Wikipedia / Wikidata / GitHub。
#
# 共通の作法:
#   * 識別可能な User-Agent を送る（MediaWiki / OpenAlex / Crossref の要件。連絡先は FREEAGENT_MAILTO）
#   * 429 と 403 はホスト単位で記憶して以降は fail fast（遮断されたホストへ素の呼び出しを投げると
#     1 回の呼び出しが分単位で固まり、並列で走っている他の呼び出しまで待たされる）
#   * 結果は TTL 付きでメモリに置く（同じ問いの連打で提供元を叩き続けない）

_KB_CACHE: dict[str, dict] = {}
_KB_CACHE_LOCK = threading.Lock()
_KB_BLOCKED: dict[str, float] = {}
_KB_BLOCK_LOCK = threading.Lock()
_KB_BLOCK_S = 600.0
SOURCES = ("wikipedia", "wikidata", "arxiv", "crossref", "openalex", "github")


def _kb_block(host: str, secs: float = _KB_BLOCK_S) -> None:
    with _KB_BLOCK_LOCK:
        _KB_BLOCKED[host] = now_ts() + secs


def _kb_is_blocked(host: str) -> bool:
    with _KB_BLOCK_LOCK:
        until = _KB_BLOCKED.get(host)
        if until is None:
            return False
        if until <= now_ts():
            _KB_BLOCKED.pop(host, None)
            return False
        return True


def kb_http(url: str, *, accept: str = "application/json", extra_headers: dict | None = None,
            timeout: float | None = None) -> tuple[int, str]:
    """(status, body) を返す。例外は投げず、失敗も status で表す（呼び出し側で分岐する）。"""
    host = urllib.parse.urlparse(url).netloc
    if _kb_is_blocked(host):
        return 0, f"blocked: {host} は直近の失敗により一時的にスキップしています"
    headers = {"User-Agent": KB_USER_AGENT, "Accept": accept}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with _urlopen(req, timeout or KB_TIMEOUT) as resp:
            return int(resp.status), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code in (403, 429, 503):
            _kb_block(host, _parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None,
                                               None) if exc.code == 429 else _KB_BLOCK_S)
        return int(exc.code), body
    except Exception as exc:
        _kb_block(host, 120.0)  # 接続不可・タイムアウトも短くブロックする
        return 0, f"{type(exc).__name__}: {exc}"


def kb_json(url: str, *, extra_headers: dict | None = None) -> tuple[dict | None, str]:
    status, body = kb_http(url, extra_headers=extra_headers)
    if status != 200:
        return None, f"HTTP {status}: {body[:200]}" if status else body
    try:
        return json.loads(body), ""
    except ValueError as exc:
        return None, f"JSON 解析失敗: {exc}"


def _kb_cached(key: str, producer):
    with _KB_CACHE_LOCK:
        hit = _KB_CACHE.get(key)
        if hit and now_ts() - as_float(hit.get("at"), 0.0) < KB_TTL:
            return hit.get("value")
    value = producer()
    with _KB_CACHE_LOCK:
        _KB_CACHE[key] = {"at": now_ts(), "value": value}
        if len(_KB_CACHE) > 512:  # 単純な上限（古い順に捨てる）
            for old in sorted(_KB_CACHE, key=lambda k: as_float(_KB_CACHE[k].get("at"), 0.0))[:128]:
                _KB_CACHE.pop(old, None)
    return value


def _cite(source: str, title: str, url: str, **extra) -> dict:
    row = {"source": source, "title": truncate((title or "").strip(), 300), "url": url}
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            row[key] = value
    return row


# ---------------------------------------------------------------- §5.1 Wikipedia

def kb_wikipedia(query: str, lang: str = "ja", limit: int = 3) -> dict:
    """本文の要約＋検索結果。出典 URL 付き（LLM 不使用）。"""
    lang = as_str(lang, "ja")[:8]
    limit = as_int(limit, 3, 1, 8)

    def produce() -> dict:
        base = f"https://{lang}.wikipedia.org"
        q = urllib.parse.urlencode({"action": "query", "format": "json", "list": "search",
                                    "srsearch": query, "srlimit": limit, "srprop": "snippet"})
        data, err = kb_json(f"{base}/w/api.php?{q}")
        if err:
            return {"source": "wikipedia", "error": err}
        hits = ((data or {}).get("query") or {}).get("search") or []
        items, cites = [], []
        for hit in hits[:limit]:
            title = hit.get("title") or ""
            page_url = f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            snippet = re.sub(r"<[^>]+>", "", hit.get("snippet") or "")
            summary = ""
            s_data, s_err = kb_json(f"{base}/api/rest_v1/page/summary/{urllib.parse.quote(title)}")
            if not s_err and isinstance(s_data, dict):
                summary = s_data.get("extract") or ""
                page_url = ((s_data.get("content_urls") or {}).get("desktop") or {}).get("page") or page_url
            items.append({"title": title, "url": page_url, "summary": truncate(summary, 1200),
                          "snippet": truncate(snippet, 300)})
            cites.append(_cite("wikipedia", title, page_url, lang=lang))
        return {"source": "wikipedia", "lang": lang, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"wiki:{lang}:{query}:{limit}", produce)


# ---------------------------------------------------------------- §5.2 Wikidata

def kb_wikidata(query: str, lang: str = "ja", limit: int = 3) -> dict:
    """項目を検索し、代表的な主張（P31 分類 / P17 国 / P569 生年月日 など）を**ラベル解決つき**で返す。"""
    lang = as_str(lang, "ja")[:8]
    limit = as_int(limit, 3, 1, 5)
    # 値を読むときに意味が取れる代表プロパティだけを出す（全主張を並べても読めない）
    PROPS = {"P31": "分類", "P279": "上位分類", "P17": "国", "P569": "生年月日", "P570": "没年月日",
             "P106": "職業", "P361": "一部", "P159": "本社所在地", "P571": "設立", "P50": "著者"}

    def produce() -> dict:
        q = urllib.parse.urlencode({"action": "wbsearchentities", "format": "json", "language": lang,
                                    "uselang": lang, "search": query, "limit": limit})
        data, err = kb_json(f"https://www.wikidata.org/w/api.php?{q}")
        if err:
            return {"source": "wikidata", "error": err}
        hits = (data or {}).get("search") or []
        ids = [h.get("id") for h in hits if h.get("id")]
        items, cites, ref_ids = [], [], set()
        entities: dict = {}
        if ids:
            q2 = urllib.parse.urlencode({"action": "wbgetentities", "format": "json",
                                         "ids": "|".join(ids[:limit]), "props": "labels|descriptions|claims|sitelinks"})
            e_data, _ = kb_json(f"https://www.wikidata.org/w/api.php?{q2}")
            entities = ((e_data or {}).get("entities") or {})
        claims_out: dict[str, list] = {}
        for qid in ids[:limit]:
            ent = entities.get(qid) or {}
            claims = ent.get("claims") or {}
            rows = []
            for prop, label in PROPS.items():
                for claim in (claims.get(prop) or [])[:2]:
                    value = (((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value"))
                    if isinstance(value, dict):
                        if "id" in value:
                            rows.append({"property": prop, "label": label, "value": value["id"]})
                            ref_ids.add(value["id"])
                        elif "time" in value:
                            rows.append({"property": prop, "label": label,
                                         "value": value["time"].lstrip("+")[:10]})
                        elif "text" in value:
                            rows.append({"property": prop, "label": label, "value": value["text"]})
                    elif value is not None:
                        rows.append({"property": prop, "label": label, "value": str(value)})
            claims_out[qid] = rows
        # 参照した QID のラベルを 1 回の追加呼び出しでまとめて解決する
        labels: dict[str, str] = {}
        if ref_ids:
            q3 = urllib.parse.urlencode({"action": "wbgetentities", "format": "json",
                                         "ids": "|".join(sorted(ref_ids)[:40]), "props": "labels",
                                         "languages": lang})
            l_data, _ = kb_json(f"https://www.wikidata.org/w/api.php?{q3}")
            for qid, ent in ((l_data or {}).get("entities") or {}).items():
                lab = (((ent.get("labels") or {}).get(lang) or {}).get("value"))
                if lab:
                    labels[qid] = lab
        for hit in hits[:limit]:
            qid = hit.get("id")
            if not qid:
                continue
            rows = [dict(row, value_text=labels.get(str(row.get("value")), "")) for row in claims_out.get(qid, [])]
            url = f"https://www.wikidata.org/wiki/{qid}"
            items.append({"id": qid, "label": hit.get("label") or "",
                          "description": hit.get("description") or "",
                          "url": url, "claims": rows})
            cites.append(_cite("wikidata", hit.get("label") or qid, url, qid=qid))
        return {"source": "wikidata", "lang": lang, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"wd:{lang}:{query}:{limit}", produce)


# ---------------------------------------------------------------- §5.3 arXiv
#
# arXiv の API は**連続アクセスを避ける**よう求めている（推奨 3 秒間隔）。守らないと CDN が 406 を
# 返す（実測: 同一リクエストでも直前の呼び出しから間隔が短いと 406、時間を置くと 200）。よって
# ここだけは直列化して間隔を空ける。並列 fan-out の中でも arXiv だけは順番待ちになるが、
# 406 で落ちるより速い。

_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST = 0.0
_ARXIV_MIN_INTERVAL = _env_float("FREEAGENT_ARXIV_INTERVAL", 3.0)


def _arxiv_throttle() -> None:
    global _ARXIV_LAST
    with _ARXIV_LOCK:
        wait = _ARXIV_MIN_INTERVAL - (now_ts() - _ARXIV_LAST)
        if wait > 0:
            time.sleep(min(wait, 10.0))
        _ARXIV_LAST = now_ts()


def kb_arxiv(query: str, limit: int = 5) -> dict:
    """プレプリント検索（Atom XML）。検索語は `all:` に寄せる（フィールド指定はそのまま活かす）。"""
    limit = as_int(limit, 5, 1, 20)
    field_q = query if re.search(r"\b(all|ti|au|abs|cat):", query) else f"all:{query}"

    def produce() -> dict:
        # **https でなければならない**（実測: http は 301 を返し、urllib のリダイレクト処理の先で
        # 406 になる。https なら 200）。
        url = ("https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {"search_query": field_q, "start": 0, "max_results": limit,
             "sortBy": "relevance", "sortOrder": "descending"}))
        status, body = 0, ""
        for attempt in range(3):
            _arxiv_throttle()
            status, body = kb_http(url, accept="application/atom+xml")
            if status != 406:
                break
        if status != 200:
            return {"source": "arxiv", "error": f"HTTP {status}: {body[:200]}" if status else body}
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            return {"source": "arxiv", "error": f"XML 解析失敗: {exc}"}
        items, cites = [], []
        for entry in root.findall("a:entry", ns)[:limit]:
            link = ""
            for ln in entry.findall("a:link", ns):
                if ln.get("rel") == "alternate" or not link:
                    link = ln.get("href") or link
            title = " ".join((entry.findtext("a:title", "", ns) or "").split())
            summary = " ".join((entry.findtext("a:summary", "", ns) or "").split())
            authors = [a.findtext("a:name", "", ns) for a in entry.findall("a:author", ns)]
            items.append({
                "id": entry.findtext("a:id", "", ns),
                "title": truncate(title, 300),
                "summary": truncate(summary, 1200),
                "published": (entry.findtext("a:published", "", ns) or "")[:10],
                "updated": (entry.findtext("a:updated", "", ns) or "")[:10],
                "authors": authors[:8],
                "primary_category": (entry.find("arxiv:primary_category", ns).get("term")
                                     if entry.find("arxiv:primary_category", ns) is not None else ""),
                "doi": entry.findtext("arxiv:doi", "", ns),
                "url": link,
            })
            cites.append(_cite("arxiv", title, link,
                               year=(entry.findtext("a:published", "", ns) or "")[:4],
                               authors=authors[:3]))
        return {"source": "arxiv", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"arxiv:{field_q}:{limit}", produce)


# ---------------------------------------------------------------- §5.4 Crossref

def kb_crossref(query: str, limit: int = 5) -> dict:
    """DOI 登録機関のメタデータ（書誌）。`mailto` を付けると polite pool に入る。"""
    limit = as_int(limit, 5, 1, 20)

    def produce() -> dict:
        params = {"query": query, "rows": limit,
                  "select": "DOI,title,author,issued,container-title,type,URL,is-referenced-by-count,publisher"}
        if KB_MAILTO:
            params["mailto"] = KB_MAILTO
        data, err = kb_json("https://api.crossref.org/works?" + urllib.parse.urlencode(params))
        if err:
            return {"source": "crossref", "error": err}
        rows = ((data or {}).get("message") or {}).get("items") or []
        items, cites = [], []
        for row in rows[:limit]:
            title = (row.get("title") or [""])[0]
            year = ""
            parts = ((row.get("issued") or {}).get("date-parts") or [[]])[0]
            if parts:
                year = str(parts[0])
            authors = [" ".join(x for x in (a.get("given"), a.get("family")) if x)
                       for a in (row.get("author") or [])]
            url = row.get("URL") or (f"https://doi.org/{row.get('DOI')}" if row.get("DOI") else "")
            items.append({"title": truncate(title, 300), "doi": row.get("DOI") or "",
                          "year": year, "type": row.get("type") or "",
                          "container": (row.get("container-title") or [""])[0],
                          "publisher": row.get("publisher") or "",
                          "cited_by": row.get("is-referenced-by-count"),
                          "authors": authors[:8], "url": url})
            cites.append(_cite("crossref", title, url, year=year, doi=row.get("DOI") or ""))
        return {"source": "crossref", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"crossref:{query}:{limit}", produce)


# ---------------------------------------------------------------- §5.5 OpenAlex

def kb_openalex(query: str, limit: int = 5) -> dict:
    """書誌＋被引用数＋OA リンク。`mailto` を付けると polite pool に入る（任意）。"""
    limit = as_int(limit, 5, 1, 20)

    def produce() -> dict:
        params = {"search": query, "per-page": limit}
        if KB_MAILTO:
            params["mailto"] = KB_MAILTO
        if OPENALEX_API_KEY:
            params["api_key"] = OPENALEX_API_KEY
        data, err = kb_json("https://api.openalex.org/works?" + urllib.parse.urlencode(params))
        if err:
            # 提供元側の一時停止は「自分のバグ」と区別して伝える（再試行の判断が変わる）
            low = err.lower()
            if "temporarily unavailable" in low or "paused" in low or "503" in low:
                return {"source": "openalex", "items": [], "citations": [],
                        "provider_status": "paused",
                        "error": "OpenAlex の検索基盤が提供元側で一時停止中です（匿名検索の停止）。"
                                 "OPENALEX_API_KEY を設定すると回避できます"}
            return {"source": "openalex", "error": err}
        items, cites = [], []
        for row in (data or {}).get("results") or []:
            title = row.get("title") or row.get("display_name") or ""
            authors = [(a.get("author") or {}).get("display_name")
                       for a in (row.get("authorships") or [])]
            authors = [a for a in authors if a]
            source_name = (((row.get("primary_location") or {}).get("source") or {}).get("display_name")) or ""
            url = row.get("doi") or row.get("id") or ""
            oa = row.get("open_access") or {}
            items.append({"title": truncate(title, 300), "year": row.get("publication_year"),
                          "cited_by": row.get("cited_by_count"), "container": source_name,
                          "authors": authors[:8], "url": url,
                          "oa_url": oa.get("oa_url") or "", "is_oa": bool(oa.get("is_oa")),
                          "type": row.get("type") or ""})
            cites.append(_cite("openalex", title, url, year=row.get("publication_year"),
                               cited_by=row.get("cited_by_count")))
        return {"source": "openalex", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"openalex:{query}:{limit}", produce)


# ---------------------------------------------------------------- §5.6 GitHub

def kb_github(query: str, kind: str = "repo", limit: int = 5) -> dict:
    """リポジトリ / コード / Issue の検索。**コード検索はトークン必須**（未設定なら理由を返す）。"""
    kind = as_str(kind, "repo")
    if kind not in ("repo", "code", "issue"):
        kind = "repo"
    limit = as_int(limit, 5, 1, 20)

    def produce() -> dict:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if GITHUB_TOKEN:
            headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
        if kind == "code" and not GITHUB_TOKEN:
            return {"source": "github", "kind": kind, "items": [], "citations": [],
                    "error": "コード検索は GITHUB_TOKEN（または GH_TOKEN）が必要です。"
                             "トークンを設定するか kind=\"repo\"/\"issue\" を使ってください"}
        endpoint = {"repo": "repositories", "code": "code", "issue": "issues"}[kind]
        sort = "&sort=stars&order=desc" if kind == "repo" else ""
        url = (f"https://api.github.com/search/{endpoint}?"
               + urllib.parse.urlencode({"q": query, "per_page": limit}) + sort)
        data, err = kb_json(url, extra_headers=headers)
        if err:
            return {"source": "github", "kind": kind, "error": err}
        rows = (data or {}).get("items") or []
        items, cites = [], []
        for row in rows[:limit]:
            if kind == "repo":
                title = row.get("full_name") or ""
                items.append({"title": title, "url": row.get("html_url") or "",
                              "description": truncate(row.get("description") or "", 400),
                              "stars": row.get("stargazers_count"), "language": row.get("language"),
                              "updated_at": row.get("updated_at"), "topics": (row.get("topics") or [])[:8],
                              "license": ((row.get("license") or {}).get("spdx_id"))})
            elif kind == "issue":
                title = row.get("title") or ""
                items.append({"title": truncate(title, 300), "url": row.get("html_url") or "",
                              "state": row.get("state"), "comments": row.get("comments"),
                              "created_at": row.get("created_at"),
                              "repository_url": row.get("repository_url") or ""})
            else:
                title = row.get("name") or ""
                items.append({"title": title, "url": row.get("html_url") or "",
                              "repository": ((row.get("repository") or {}).get("full_name")) or "",
                              "path": row.get("path") or ""})
            cites.append(_cite("github", title, row.get("html_url") or "", kind=kind))
        return {"source": "github", "kind": kind, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"gh:{kind}:{query}:{limit}:{bool(GITHUB_TOKEN)}", produce)


# ---------------------------------------------------------------- §5.7 横断検索

KB_BACKENDS = {
    "wikipedia": lambda q, limit, opts: kb_wikipedia(q, lang=opts.get("lang") or "ja", limit=limit),
    "wikidata": lambda q, limit, opts: kb_wikidata(q, lang=opts.get("lang") or "ja", limit=limit),
    "arxiv": lambda q, limit, opts: kb_arxiv(q, limit=limit),
    "crossref": lambda q, limit, opts: kb_crossref(q, limit=limit),
    "openalex": lambda q, limit, opts: kb_openalex(q, limit=limit),
    "github": lambda q, limit, opts: kb_github(q, kind=opts.get("kind") or "repo", limit=limit),
}


def knowledge_lookup(query: str, sources: list[str] | None = None, *, limit: int = 3,
                     lang: str = "ja", kind: str = "repo", max_workers: int | None = None) -> dict:
    """指定ソースを**並列に**引いて、出典つきでまとめる。LLM を使わないので幻覚が入らない。"""
    query = as_str(query)
    if not query:
        return {"error": "query は必須です", "query": query}
    picked = [s for s in as_str_list(sources) if s in KB_BACKENDS] or list(KB_BACKENDS)
    unknown = [s for s in as_str_list(sources) if s not in KB_BACKENDS]
    limit = as_int(limit, 3, 1, 10)
    opts = {"lang": as_str(lang, "ja"), "kind": as_str(kind, "repo")}
    results = run_parallel(
        picked, lambda src: KB_BACKENDS[src](query, limit, opts),
        max_workers=max_workers or min(len(picked), MAX_WORKERS))
    by_source = {src: res for src, res in zip(picked, results)}
    citations: list[dict] = []
    errors = {}
    for src, res in by_source.items():
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            errors[src] = res["error"]
        citations.extend(res.get("citations") or [])
    return {"query": query, "sources": picked, "unknown_sources": unknown,
            "results": by_source, "citations": citations, "citation_count": len(citations),
            "errors": errors, "llm_used": False}


# ================================================================ §6 ツール実装
#
# どのツールも例外を外へ漏らさず、content（人間向け）と structuredContent（LLM向け純粋JSON）を
# 両方返す。失敗は {"error": "..."} で返し、呼び出し側（メインLLM）が次の手を選べるようにする。

CONSULT_SYSTEM = (
    "あなたはメインLLMの補佐です。出力は必ず次の3行だけで答えてください。\n"
    "結論: <1〜3文。断定を避け、確信が無い部分は「不明」と書く>\n"
    "確信度: <0〜100 の整数>\n"
    "メインに確認したい点: <前提が足りず判断できない点。無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)
DEBATE_SYSTEM = (
    "あなたは複数の立場を比較する補佐です。出力は必ず次の4行だけです。\n"
    "立場: <あなたの立場を1文>\n"
    "最強の反論: <自分と異なる立場への、最も強い反論を1〜2文>\n"
    "応答: <その反論への応答を1〜2文>\n"
    "未解決: <合意できずに残った点。無ければ なし>"
)
AGENT_SYSTEM_FINAL = (
    "これ以上のツール呼び出しはできません。集めた情報だけを根拠に、最終回答を JSON 1つで出してください。\n"
    '  {"answer": "<回答。根拠が足りない点は「根拠に無い」と明記>"}\n'
    "JSON 以外の文字を書かない。"
)
AGENT_SYSTEM = (
    "あなたは調査補佐です。次のいずれか**1つだけ**を JSON で出力してください。\n"
    '  ツールを使う: {"tool": "lookup", "query": "<検索語>", "sources": ["arxiv","crossref",...]}\n'
    '  使える source: wikipedia, wikidata, arxiv, crossref, openalex, github\n'
    '  回答する:     {"answer": "<回答>"}\n'
    "JSON 以外の文字（前置き・コードフェンス）を書かない。根拠が足りなければツールを使う。"
)

_LABEL_CONCLUSION = re.compile(r"(?:結論|まとめ|conclusion)\s*[:：]\s*(.+)", re.I)
_LABEL_CONFIDENCE = re.compile(r"(?:確信度|自信|confidence)\s*[:：]?\s*([0-9]{1,3}(?:\.[0-9]+)?|0?\.[0-9]+)", re.I)
_LABEL_QUESTION = re.compile(r"(?:メインに確認したい点|確認したい点|質問|questions?)\s*[:：]\s*(.+)", re.I)
_NO_ANSWER = ("なし", "無し", "ありません", "none", "-", "n/a", "特になし")


def parse_labeled(text: str) -> dict:
    """「結論/確信度/確認したい点」形式の回答を構造化する。取れない項目は None（推測しない）。"""
    out = {"conclusion": "", "confidence": None, "question": "", "labels_found": 0}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_CONCLUSION.search(line)
        if m and not out["conclusion"]:
            out["conclusion"] = truncate(m.group(1).strip().strip("*"), 600)
            out["labels_found"] += 1
        # **同じ行に別のラベルが続くことがある**（実測: 「結論: … 確信度: 88」）ので continue しない。
        # 1 行 1 ラベルと決め打つと確信度を取りこぼし、確信度が None のまま返る。
        m = _LABEL_CONFIDENCE.search(line)
        if m and out["confidence"] is None:
            raw = m.group(1)
            val = as_float(raw, -1.0)
            if val <= 1.0:
                val *= 100.0
            if 0 <= val <= 100:
                out["confidence"] = int(val)
                out["labels_found"] += 1
        m = _LABEL_QUESTION.search(line)
        if m and not out["question"]:
            value = m.group(1).strip().strip("*")
            out["question"] = "" if value.lower() in _NO_ANSWER else truncate(value, 400)
            out["labels_found"] += 1
    return out


def _no_models() -> dict:
    status = provider_status()
    ready = [row["provider"] for row in status if row["ready"]]
    return {
        "error": "推論可能な Free モデルが 0 件です。"
                 "`hermes proxy start` が動いているか、プロバイダのキーを確認してください。",
        "providers": status,
        "ready_providers": ready,
    }


def _select_or_error(args: dict, default_size: int = 3) -> tuple[list[str], dict]:
    size = as_int(args.get("size"), default_size, 1, 6)
    return select_models(size, as_str_list(args.get("models")) or None,
                         prefer=as_str_list(args.get("prefer")) or None,
                         exclude=as_str_list(args.get("exclude")) or None)


def _rate_limit_report(answers: list[dict]) -> dict:
    limited = [a for a in answers if a.get("rate_limited")]
    skipped = sorted({ref for a in answers for ref in (a.get("skipped_cooling") or [])})
    return {"rate_limited": len(limited), "skipped_cooling": skipped}


def _consensus_groups(entries: list[dict]) -> list[dict]:
    """結論が似ている回答をまとめる（表層の類似であって正しさの一致ではない）。"""
    groups: list[dict] = []
    for entry in entries:
        text = entry.get("conclusion") or entry.get("answer") or ""
        placed = False
        for group in groups:
            if similarity(text, group["representative"]) >= 0.55:
                group["models"].append(entry["model"])
                placed = True
                break
        if not placed:
            groups.append({"representative": text, "models": [entry["model"]],
                           "excerpt": truncate(text, 200)})
    return groups


def tool_models(args: dict) -> dict:
    """利用可能なモデルを**検索**し、必要なら**生存確認**する。

    `query` / `provider` を付けると、その条件に合うモデルを（Free かどうか付きで）返す。引数なしなら
    プロバイダの要約と Free モデル一覧を返す。`probe=true` なら候補を実際に 1 回ずつ呼び、
    **一覧に載っているが呼べないモデル**（廃止 410 / アカウント未有効 404 など）を除外する。
    ここで得た `provider/model`（HF は `:提供元` を付けられる）は他ツールの `models` 引数に渡せる。
    """
    status = provider_status()
    free = free_model_refs()
    usable = [ref for ref in free if not is_cooling(ref)]
    query = as_str(args.get("query")) or as_str(args.get("q"))
    provider_filter = as_str(args.get("provider"))
    limit = as_int(args.get("limit"), 40, 1, 200)
    data = {
        "providers": status,
        "total_models": sum(row["models"] for row in status),
        "free_candidates": len(free),
        "usable_now": len(usable),
        "default_model": default_model(),
        "cooling": {ref: row for ref, row in cooling_refs().items()},
        "ranking_enabled": RANK_ENABLED,
    }

    searching = bool(query or provider_filter or args.get("all") or args.get("probe"))
    free_only = as_flag(args.get("free_only"))
    if searching:
        rows = all_models()
        needle = query.lower()
        matched = [r for r in rows
                   if (not provider_filter or r.get("provider") == provider_filter)
                   and (not free_only or r.get("free"))
                   and (not needle or needle in str(r.get("id", "")).lower())]
        matched.sort(key=lambda r: (not r.get("free"), str(r.get("id", ""))))
        matched_free = sum(1 for r in matched if r.get("free"))   # ページ前の総数（offset で減らさない）
        offset = as_int(args.get("offset"), 0, 0, 5000)
        matched = matched[offset:]
        data["query"] = {
            "query": query, "provider": provider_filter or None,
            "offset": offset,
            "free_only": free_only,
            "matched": len(matched) + offset,
            "matched_free": matched_free,
            "shown": min(len(matched), limit),
            "probed": False,
        }
        shown = matched[:limit]
        if args.get("probe") and shown:
            # 一覧は実態と乖離する（実測: NVIDIA の一覧 82 件の大半が 410 EOL、HF は権限で 403）。
            # 実際に 1 回だけ呼んで**呼べるものだけ**を残す。件数は絞る（呼び出しは課金・レートに触れる）。
            probe_limit = as_int(args.get("probe_limit"), min(len(shown), 12), 1, 40)
            shown = shown[:probe_limit]
            probe_prompt = as_str(args.get("probe_prompt")) or "1+1=? 数字1つだけ。"
            probe_tokens = as_int(args.get("probe_max_tokens"), 16, 1, 64)
            probe_timeout = _env_float("FREEAGENT_PROBE_TIMEOUT", 25.0)
            probe_workers = max(1, min(8, _env_int("FREEAGENT_PROBE_WORKERS", 8),
                                       len(shown))) if shown else 1

            def check(row: dict) -> dict:
                model = row["id"]
                if _is_hf(row["provider"]) and row.get("free_via"):
                    model = f"{model}:{row['free_via'][0]}"
                # **フォールバックを切る**: 有効なままだと他プロバイダが答えて「生存」と誤判定する
                # （実測: HF の 403 が OpenRouter の応答で隠れ、生きているように見えた）。
                # 読み取りタイムアウトは短くする（既定 180 秒だと 1 件の遅いモデルが探索全体を止める）。
                res = call_model(make_ref(row["provider"], model), probe_prompt,
                                 max_tokens=probe_tokens, kind="probe", allow_fallback=False,
                                 timeout=probe_timeout)
                err = res.get("error") or ""
                err_l = err.lower()
                if not err:
                    verdict = "alive"
                elif "timed out" in err_l or "timeout" in err_l:
                    # **遅いだけかもしれない**（NVIDIA はコールドスタートが長い）。死んだ扱いにしない。
                    verdict = "slow"
                elif "空応答" in err:
                    # 応答自体は返っている（思考トークンで probe の予算を使い切った）。生存側に倒す。
                    verdict = "slow"
                elif "http 404" in err_l or "http 410" in err_l or "not found" in err_l:
                    verdict = "gone"
                elif "http 401" in err_l or "http 403" in err_l:
                    verdict = "auth"
                else:
                    # 429（提供元が一時制限）や 5xx。**除外しない**（生きているが今は応えない）。
                    verdict = "error"
                return {"model": row, "verdict": verdict, "served_by": res.get("served_by"),
                        "error": truncate(err, 200),
                        "sample": truncate((res.get("text") or "").strip(), 60)}

            probed = run_parallel(shown, check, max_workers=probe_workers)
            # 消えたもの（404/410）と権限で拒否されたものだけ一覧から外す。**遅い・一時エラーは残す**
            # （残さないと「まだ生きているが遅い」モデルを永久に隠すことになる）。
            dropped = {"gone", "auth"}
            alive = [r["model"] for r in probed if r["verdict"] == "alive"]
            kept = [r["model"] for r in probed if r["verdict"] not in dropped]
            data["query"].update({
                "probed": True, "probe_attempted": len(probed),
                "probe_alive": len(alive),
                "probe_slow": sum(1 for r in probed if r["verdict"] == "slow"),
                "probe_dropped": [{"ref": make_ref(r["model"]["provider"], r["model"]["id"]),
                                   "verdict": r["verdict"], "error": r["error"]}
                                  for r in probed if r["verdict"] in dropped],
                "probe_errors": [{"ref": make_ref(r["model"]["provider"], r["model"]["id"]),
                                  "verdict": r["verdict"], "error": r["error"]}
                                 for r in probed if r["verdict"] in ("slow", "error")],
            })
            by_id = {(r["model"]["provider"], r["model"]["id"]): r["verdict"] for r in probed}
            shown = [dict(m, probe=by_id.get((m["provider"], m["id"])) or "alive") for m in kept]
        data["models"] = [{
            "ref": make_ref(r["provider"], r["id"]),
            "provider": r["provider"],
            "id": r["id"],
            "free": bool(r.get("free")),
            "free_via": r.get("free_via") or [],
            "context_length": r.get("context_length"),
            "usable": provider_ready(r["provider"]),
            "probe": r.get("probe"),
        } for r in shown]
    if args.get("stats"):
        data["stats"] = [
            {"model": ref, "status": model_status(ref), "quality": model_quality(ref),
             "observations": model_observations(ref)}
            for ref in free
        ]
    elif not searching:
        data["free_models"] = free
    return data


def tool_ask(args: dict) -> dict:
    """1 つのサブLLMへ 1 回だけ問い合わせる。"""
    prompt = as_str(args.get("prompt"))
    if not prompt:
        return {"error": "prompt は必須です（空文字は不可）"}
    model = as_str(args.get("model")) or default_model()
    result = call_model(model, prompt, system=as_str(args.get("system")),
                        max_tokens=as_int(args.get("max_tokens"), 800, 16, 8000),
                        temperature=as_float(args.get("temperature"), None),
                        kind="ask")
    if result.get("error"):
        return {"error": result["error"], "model": result.get("ref"), "kind": "ask",
                "rate_limited": result.get("rate_limited")}
    return {
        "answer": result["text"],
        "model": result["ref"],
        "served_by": result.get("served_by"),
        "fell_back": bool(result.get("fallback")),
        "latency_s": result.get("latency_s"),
        "truncated": result.get("truncated"),
        "cot_leak": result.get("cot_leak"),
        "tokens": result.get("tokens"),
        "quality": {"status": model_status(result["ref"]), "score": model_quality(result["ref"])},
    }


def tool_fanout(args: dict) -> dict:
    """プロンプト × モデルを並列に実行する（相互検証・ベストオブN・多数決）。"""
    prompts = as_str_list(args.get("prompts") or args.get("items"))
    if not prompts:
        single = as_str(args.get("prompt"))
        prompts = [single] if single else []
    if not prompts:
        return {"error": "prompts（または prompt）は必須です"}
    prompts = prompts[:16]
    explicit = as_str_list(args.get("models"))
    if explicit:
        refs = explicit
        info = {"requested": True, "models": refs}
    else:
        size = as_int(args.get("size"), 2, 1, 4)
        refs, info = select_models(size, None)
    if not refs:
        return _no_models()
    system = as_str(args.get("system"))
    max_tokens = as_int(args.get("max_tokens"), 800, 16, 8000)
    pairs = [(ref, prompt) for prompt in prompts for ref in refs]
    if len(pairs) > MAX_CALLS_PER_RUN:
        return {"error": f"呼び出し数が上限を超えています（{len(pairs)} > {MAX_CALLS_PER_RUN}）。"
                         "prompts か size を減らしてください"}
    started = now_ts()
    results = ask_map(pairs, system=system, max_tokens=max_tokens, kind="fanout")
    wall = round(now_ts() - started, 3)
    rows = []
    for (ref, prompt), res in zip(pairs, results):
        row = {"model": ref, "prompt": truncate(prompt, 200)}
        if res.get("error"):
            row["error"] = res["error"]
        else:
            row["answer"] = res["text"]
            row["served_by"] = res.get("served_by")
            row["cot_leak"] = res.get("cot_leak")
            row["truncated"] = res.get("truncated")
        rows.append(row)
    ok = sum(1 for r in rows if not r.get("error"))
    sequential = round(sum(as_float(r.get("latency_s"), 0.0) for r in results), 3)
    return {
        "calls": len(rows), "ok": ok, "failed": len(rows) - ok,
        "wall_s": wall, "sequential_estimate_s": sequential,
        "speedup": round(sequential / wall, 2) if wall > 0 else None,
        "results": rows, "selection": info, **_rate_limit_report(results),
    }


def tool_panel(args: dict) -> dict:
    """同じ問いを複数モデルへ投げ、合意度・不一致・CoT混入つきで返す。"""
    question = as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    refs, info = _select_or_error(args, default_size=3)
    if not refs:
        return _no_models()
    results = ask_many(refs, question, system=as_str(args.get("system")) or CONSULT_SYSTEM,
                       max_tokens=as_int(args.get("max_tokens"), 500, 16, 4000), kind="panel")
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"]})
            continue
        parsed = parse_labeled(res["text"])
        answers.append({
            "model": ref, "served_by": res.get("served_by"),
            "label": parsed["conclusion"] or truncate(res["text"], 400),
            "conclusion": parsed["conclusion"] or truncate(res["text"], 400),
            "confidence": parsed["confidence"], "labels_found": parsed["labels_found"],
            "question": parsed["question"], "answer": truncate(res["text"], 1500),
            "cot_leak": res.get("cot_leak"), "truncated": res.get("truncated"),
        })
    good = [a for a in answers if not a.get("error")]
    conf = [a["confidence"] for a in good if isinstance(a.get("confidence"), int)]
    data = {
        "question": question, "models": refs, "selection": info,
        "answered": len(good), "failed": len(answers) - len(good),
        "agreement": agreement_of([a["conclusion"] for a in good]),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "consensus": _consensus_groups(good),
        "disagreements": [a["model"] for a in good if a.get("question")],
        "open_questions": [{"model": a["model"], "question": a["question"]}
                           for a in good if a.get("question")],
        "answers": answers,
        **_rate_limit_report(results),
        "agreement_note": "合意度は解析済みの結論の表層類似度であり、正しさの確率ではありません。",
    }
    return data


def tool_lookup(args: dict) -> dict:
    """外部知識を出典つきで取得する（LLM を使わない＝幻覚が入らない経路）。"""
    query = as_str(args.get("query") or args.get("topic") or args.get("q"))
    if not query:
        return {"error": "query は必須です"}
    return knowledge_lookup(query,
                            as_str_list(args.get("sources")) or None,
                            limit=as_int(args.get("limit"), 3, 1, 10),
                            lang=as_str(args.get("lang"), "ja"),
                            kind=as_str(args.get("github_kind"), "repo"))


def _evidence_block(citations: list[dict]) -> str:
    lines = []
    for i, c in enumerate(citations, 1):
        bits = [f"[{i}] {c.get('title') or '(無題)'}"]
        if c.get("year"):
            bits.append(f"({c['year']})")
        bits.append(c.get("url") or "")
        lines.append(" ".join(str(b) for b in bits if b))
    return "\n".join(lines)


def tool_grounded(args: dict) -> dict:
    """根拠を先に取り、それを注入してから複数モデルに答えさせる（幻覚の抑止）。"""
    question = as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    sources = as_str_list(args.get("sources")) or None
    kb = knowledge_lookup(question, sources, limit=as_int(args.get("limit"), 3, 1, 10),
                          lang=as_str(args.get("lang"), "ja"),
                          kind=as_str(args.get("github_kind"), "repo"))
    citations = kb.get("citations") or []
    if not citations:
        return {"error": "根拠が 0 件でした。query を変えるか sources を広げてください",
                "lookup": {"sources": kb.get("sources"), "errors": kb.get("errors")}}
    refs, info = _select_or_error(args, default_size=2)
    if not refs:
        return _no_models()
    prompt = (
        "次の【根拠】だけを情報源として質問に答えてください。\n"
        "根拠に無い事実は書かない。書けない場合は「根拠に無い」と明示する。\n"
        "本文中で根拠を示すときは [番号] を付ける。\n\n"
        f"【根拠】\n{_evidence_block(citations)}\n\n【質問】\n{question}"
    )
    results = ask_many(refs, prompt, max_tokens=as_int(args.get("max_tokens"), 700, 16, 4000),
                       kind="grounded")
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"]})
            continue
        cited = sorted({int(n) for n in re.findall(r"\[(\d{1,2})\]", res["text"] or "")
                        if 1 <= int(n) <= len(citations)})
        answers.append({"model": ref, "served_by": res.get("served_by"),
                        "answer": truncate(res["text"], 2000),
                        "cited": cited, "cited_ok": bool(cited),
                        "truncated": res.get("truncated"), "cot_leak": res.get("cot_leak")})
    good = [a for a in answers if not a.get("error")]
    return {
        "question": question, "citations": citations, "citation_count": len(citations),
        "sources_used": kb.get("sources"), "source_errors": kb.get("errors"),
        "models": refs, "selection": info, "answers": answers,
        "agreement": agreement_of([a.get("answer") or "" for a in good]),
        "answers_with_citations": sum(1 for a in good if a.get("cited_ok")),
        "answered": len(good), "failed": len(answers) - len(good),
        **_rate_limit_report(results),
    }


def tool_map(args: dict) -> dict:
    """多数の要素へ同一指示を並列適用し、必要なら reduce で統合する。"""
    items = as_str_list(args.get("items") or args.get("prompts"))
    instruction = as_str(args.get("instruction") or args.get("system"))
    if not items:
        return {"error": "items は必須です（文字列の配列）"}
    if not instruction:
        return {"error": "instruction は必須です（各要素へ適用する指示）"}
    items = items[:64]
    model = as_str(args.get("model")) or default_model()
    if not model:
        return _no_models()
    max_tokens = as_int(args.get("max_tokens"), 500, 16, 4000)
    prompts = [f"{instruction}\n\n---\n{truncate(item, 4000)}" for item in items]
    results = run_parallel(prompts, lambda p: call_model(model, p, max_tokens=max_tokens, kind="map"),
                           max_workers=MAX_WORKERS)
    rows = []
    for item, res in zip(items, results):
        rows.append({"item": truncate(item, 200),
                     "output": truncate(res.get("text") or "", 1500) if not res.get("error") else "",
                     "error": res.get("error") or ""})
    ok = sum(1 for r in rows if not r["error"])
    data = {"model": model, "count": len(rows), "ok": ok, "failed": len(rows) - ok, "results": rows}
    if args.get("reduce"):
        joined = "\n".join(f"- {r['output']}" for r in rows if r["output"])
        if joined.strip():
            reduce_prompt = (f"次の {len(rows)} 件の出力を統合してください。"
                            f"重複をまとめ、対立点は残し、出典番号や行番号を落とさない。\n\n{truncate(joined, 12000)}")
            red = call_model(as_str(args.get("reduce_model")) or model, reduce_prompt,
                             max_tokens=as_int(args.get("reduce_max_tokens"), 900, 16, 4000),
                             kind="map_reduce")
            data["reduced"] = red.get("text") or ""
            if red.get("error"):
                data["reduce_error"] = red["error"]
    return data


def _consult_prompt(question: str, main_reply: str, round_no: int, peers: list[dict] | None = None,
                    mode: str = "discuss", draft: str = "") -> str:
    parts = [f"【問い】\n{question}"]
    if mode == "review" and draft:
        parts.append(f"【査読対象の下書き】\n{truncate(draft, 4000)}")
    if main_reply:
        parts.append(f"【メインからの回答（権威ある前提として扱う）】\n{truncate(main_reply, 3000)}")
    if peers:
        block = "\n".join(f"- {p['model']}: {truncate(p.get('conclusion') or p.get('text') or '', 400)}"
                          for p in peers)
        parts.append(f"【他の参加者の回答（第{round_no}ラウンド）】\n{block}")
    parts.append("上の形式（結論/確信度/メインに確認したい点）だけで答えてください。")
    return "\n\n".join(parts)


def tool_consult(args: dict) -> dict:
    """メイン↔サブの双方向相談。`debate_depth="deep"` で独立回答→反論→統合の 3 段討論。"""
    sid = as_str(args.get("session_id"))
    main_reply = as_str(args.get("main_reply"))
    draft = as_str(args.get("draft"))
    mode = as_str(args.get("mode"), "discuss")
    depth = as_str(args.get("debate_depth"), "normal")
    stored = session_get(sid) if sid else None
    question = as_str(args.get("question") or (stored or {}).get("question"))

    if stored and not args.get("question"):
        question = stored.get("question") or ""
        if not main_reply:
            return {"error": f"セッション {sid} を再開するには main_reply（または question）が必要です",
                    "session_id": sid, "stage": stored.get("stage")}
    if not question:
        return {"error": "question は必須です（session_id を付けると前回の問いを引き継ぎます）"}

    if stored and stored.get("models"):
        refs = [m for m in stored["models"] if m and not is_cooling(m)][:3]
        info = {"requested": True, "resumed_from": sid, "models": refs}
        if not refs:
            refs, info = _select_or_error(args, default_size=3)
    else:
        refs, info = _select_or_error(args, default_size=3)
    if not refs:
        return _no_models()

    rounds: list[dict] = list((stored or {}).get("rounds") or [])
    max_tokens = as_int(args.get("max_tokens"), 600, 16, 4000)
    replies = list((stored or {}).get("main_replies") or [])
    if main_reply:
        replies.append(main_reply)

    # 第1ラウンド（または再開時の再検討）
    round_no = len(rounds) + 1
    prompt = _consult_prompt(question, main_reply, round_no, mode=mode, draft=draft)
    results = ask_many(refs, prompt, system=CONSULT_SYSTEM, max_tokens=max_tokens, kind="consult")
    round_rows = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            round_rows.append({"model": ref, "error": res["error"]})
            continue
        parsed = parse_labeled(res["text"])
        round_rows.append({"model": ref, "served_by": res.get("served_by"),
                           "conclusion": parsed["conclusion"] or truncate(res["text"], 400),
                           "confidence": parsed["confidence"], "question": parsed["question"],
                           "text": truncate(res["text"], 1500), "cot_leak": res.get("cot_leak")})
    rounds.append({"round": round_no, "kind": "initial" if round_no == 1 else "reconsider",
                   "answers": round_rows})

    debate_summary = None
    if depth == "deep":
        peers = [row for row in round_rows if not row.get("error")]
        rebuttal_prompt = _consult_prompt(question, main_reply, round_no, peers=peers)
        rebuttal = ask_many(refs, rebuttal_prompt, system=DEBATE_SYSTEM,
                            max_tokens=max_tokens, kind="debate")
        debate_rows = []
        for ref, res in zip(refs, rebuttal):
            if res.get("error"):
                debate_rows.append({"model": ref, "error": res["error"]})
                continue
            labels = {}
            for part in (res["text"] or "").splitlines():
                if ":" in part or "：" in part:
                    key, _, value = re.split(r"[:：]", part, maxsplit=1)[0], None, part
                    labels[part.split(":")[0].split("：")[0].strip()] = value
            debate_rows.append({"model": ref, "served_by": res.get("served_by"),
                                "text": truncate(res["text"], 1500),
                                "position": truncate(labels.get("立場") or "", 300),
                                "strongest_objection": truncate(labels.get("最強の反論") or "", 400),
                                "response": truncate(labels.get("応答") or "", 400),
                                "unresolved": truncate(labels.get("未解決") or "", 400)})
        rounds.append({"round": round_no + 1, "kind": "debate", "answers": debate_rows})
        initial = {row["model"]: row.get("conclusion") for row in round_rows if not row.get("error")}
        # ラウンド途中で脱落した参加者を**明示的に残す**（実測: deep 討論の 2 ラウンド目で 1 体が落ち、
        # content には 1 体しか出ず「元から 1 体だった」ように見えた。失敗は隠さない）。
        dropped = [{"round": r["round"], "kind": r["kind"], "model": a.get("model"),
                    "error": truncate(a.get("error") or "", 160)}
                   for r in rounds for a in r["answers"] if a.get("error")]
        debate_summary = {
            "dropped": dropped,
            "agreement_by_round": [agreement_of([a.get("conclusion") or a.get("text") or ""
                                                 for a in r["answers"] if not a.get("error")])
                                   for r in rounds],
            "participants": [{
                "model": row["model"],
                "initial_position": initial.get(row["model"], ""),
                "final_position": row.get("position") or row.get("text", "")[:200],
                "changed": bool(initial.get(row["model"])
                                and row.get("position")
                                and similarity(initial[row["model"]], row["position"]) < 0.5),
                "strongest_objection": row.get("strongest_objection"),
                "unresolved": row.get("unresolved"),
            } for row in debate_rows if not row.get("error")],
            "unresolved_dissent": any(row.get("unresolved") for row in debate_rows
                                      if not row.get("error")),
        }

    last = [row for row in rounds[-1]["answers"] if not row.get("error")]
    open_questions = [{"model": row["model"], "question": row["question"]}
                      for row in last if row.get("question")]
    sid = sid or new_session_id()
    session_put(sid, {
        "question": question, "models": refs, "rounds": rounds,
        "main_replies": replies, "mode": mode, "depth": depth,
        "stage": "awaiting_main" if open_questions else "complete",
        "created_at": (stored or {}).get("created_at") or now_ts(),
    })
    conf = [row["confidence"] for row in last if isinstance(row.get("confidence"), int)]
    return {
        "session_id": sid,
        "mode": mode,
        "stage": "awaiting_main" if open_questions else "complete",
        "rounds_run": len(rounds),
        "question": truncate(question, 400),
        "models": refs, "selection": info,
        "agreement": agreement_of([row.get("conclusion") or "" for row in last]),
        "agreement_note": "表層合意度。正しさの確率ではありません。",
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "consensus": last,
        "failed": [{"model": row.get("model"), "error": truncate(row.get("error") or "", 160)}
                   for row in rounds[-1]["answers"] if row.get("error")],
        "open_questions_for_main": [q["question"] for q in open_questions],
        "debate_summary": debate_summary,
        "next_call": ({"tool": "freeagent_consult",
                       "args": {"session_id": sid, "main_reply": "<メインの回答>",
                                "max_tokens": max_tokens}}
                      if open_questions else None),
        "privacy_note": "決断はメインLLMが行ってください。サブの出力は仮説です。",
    }


def _agent_tool_call(parsed: dict) -> dict:
    """サブエージェントが要求した読み取り専用ツールを実行する（書き込み系は無い）。"""
    tool = as_str(parsed.get("tool"))
    query = as_str(parsed.get("query") or parsed.get("q"))
    if tool == "lookup" or tool in KB_BACKENDS:
        sources = as_str_list(parsed.get("sources")) or ([tool] if tool in KB_BACKENDS else None)
        out = knowledge_lookup(query, sources, limit=as_int(parsed.get("limit"), 2, 1, 5),
                               kind=as_str(parsed.get("kind"), "repo"))
        cites = out.get("citations") or []
        brief = []
        for src, res in (out.get("results") or {}).items():
            for item in (res.get("items") or [])[:2]:
                brief.append(f"{src}: {item.get('title') or item.get('label') or ''} "
                             f"— {truncate(item.get('summary') or item.get('description') or '', 300)}")
        return {"hits": len(cites), "brief": brief, "citations": cites[:6],
                "errors": out.get("errors") or {}}
    return {"hits": 0, "brief": [f"未知のツール: {tool}（使えるのは lookup / {', '.join(KB_BACKENDS)}）"],
            "citations": []}


def _parse_agent_reply(text: str) -> dict:
    """JSON だけを取り出す（コードフェンスや前置きが混ざっても最初の {...} を読む）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def tool_agent(args: dict) -> dict:
    """サブLLMが自分で知識ツールを呼んで調査するループ（読み取り専用・並列）。"""
    task = as_str(args.get("task") or args.get("prompt"))
    if not task:
        return {"error": "task は必須です"}
    refs, info = _select_or_error(args, default_size=2)
    if not refs:
        return _no_models()
    max_steps = as_int(args.get("max_steps"), 2, 1, 4)
    main_reply = as_str(args.get("main_reply"))
    max_tokens = as_int(args.get("max_tokens"), 600, 16, 3000)
    base = f"【依頼】\n{task}"
    if main_reply:
        base += f"\n\n【メインからの回答（最優先の前提）】\n{truncate(main_reply, 3000)}"

    def run_one(ref: str) -> dict:
        history = base
        trace: list[dict] = []
        citations: list[dict] = []
        evidence: list[str] = []
        for step in range(max_steps):
            final_step = step == max_steps - 1
            res = call_model(ref, history + f"\n\n（{step + 1}/{max_steps} ステップ目。"
                                            "JSON を1つだけ出力）",
                             system=AGENT_SYSTEM_FINAL if final_step else AGENT_SYSTEM,
                             max_tokens=max_tokens, kind="agent")
            if res.get("error"):
                return {"model": ref, "error": res["error"], "steps": step, "trace": trace}
            parsed = _parse_agent_reply(res["text"])
            if parsed.get("tool") and not final_step:
                got = _agent_tool_call(parsed)
                trace.append({"tool": parsed.get("tool"), "args": parsed, "hits": got["hits"]})
                citations.extend(got.get("citations") or [])
                evidence.extend(got["brief"][:6])
                history += (f"\n\n【ツール結果 {parsed.get('tool')}】\n"
                            + "\n".join(got["brief"][:6]))
                continue
            answer = as_str(parsed.get("answer"))
            if answer:
                return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                        "answer": truncate(answer, 1500), "trace": trace,
                        "citations": citations[:8]}
            # 最終ステップでツールを求められた場合は実行しない（予算切れ）。推測で埋めず、
            # **集めた根拠だけを返して「回答に到達しなかった」と明示する**。
            return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                    "answer": "", "steps_exhausted": True, "trace": trace,
                    "evidence": evidence[:8], "citations": citations[:8]}
        return {"model": ref, "steps": max_steps, "trace": trace, "answer": "",
                "steps_exhausted": True, "evidence": evidence[:8], "citations": citations[:8]}

    results = run_parallel(refs, run_one, max_workers=min(len(refs), MAX_WORKERS))
    good = [r for r in results if not r.get("error")]
    citations, seen = [], set()
    for row in good:
        for cite in row.get("citations") or []:
            key = (cite.get("source"), cite.get("url"))
            if key not in seen:
                seen.add(key)
                citations.append(cite)
    return {
        "task": truncate(task, 300), "models": refs, "selection": info,
        "agents": results, "answered": len(good), "failed": len(results) - len(good),
        "tool_calls": sum(len(r.get("trace") or []) for r in results),
        "citations": citations, "citation_count": len(citations),
        "agreement": agreement_of([r.get("answer") or "" for r in good]),
        "usage_note": "サブエージェントは読み取り専用の知識ツールのみ呼べます。書き込みはしません。",
    }


def tool_delegate(args: dict) -> dict:
    """フルツール付きの Hermes 本体を独立プロセスで起動する（opt-in・重い経路）。"""
    if not ALLOW_AGENT:
        return {"error": "この経路は無効です。FREEAGENT_ALLOW_AGENT=1 で有効化してください",
                "enabled": False}
    task = as_str(args.get("task"))
    if not task:
        return {"error": "task は必須です"}
    timeout = as_int(args.get("timeout"), 300, 30, 1800)
    import subprocess
    started = now_ts()
    try:
        proc = subprocess.run([HERMES_BIN, "chat", "-q", task], capture_output=True,
                              timeout=timeout, text=True, encoding="utf-8", errors="replace")
        return {"task": truncate(task, 300), "exit_code": proc.returncode,
                "stdout": truncate(proc.stdout or "", 4000),
                "stderr": truncate(proc.stderr or "", 1000),
                "elapsed_s": round(now_ts() - started, 1)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}", "elapsed_s": round(now_ts() - started, 1)}


# ================================================================ §7 ツール定義
#
# description は**モデルが読む唯一の窓口**（MCP の instructions は読まれない）。よって各ツールに
# 【使う条件】【使わない条件】【競合より優先】を先頭に置く。書かないと、モデルは名前だけを見て
# delegate_task（同一モデルの分身＝多様性ゼロ）や deliberation（単発集約）を選ぶ。

TOOLS: list[dict] = [
    {
        "name": "freeagent_models",
        "description": (
            "【使う条件】使えるモデルを**検索**したい（どのプロバイダにどんな Free モデルがあるか）／"
            "品質統計やクールダウンを確認したいとき。"
            "【差分】`query`（ID の部分一致）と `provider`（nous / openrouter / nvidia / huggingface）で絞り込む。"
            "返る `ref` は他ツールの `models` 引数にそのまま渡せる（HF は `:提供元` を付けて経路を固定できる）。"
            "【使わない条件】通常は不要（panel/consult 等が自動で選ぶ）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "モデル ID の部分一致（例: qwen / llama / :free）"},
                "q": {"type": "string", "description": "query の別名"},
                "provider": {"type": "string",
                             "description": "nous / openrouter / nvidia / huggingface のいずれか"},
                "limit": {"type": "integer", "description": "表示件数（既定 40・最大 200）"},
                "offset": {"type": "integer", "description": "読み飛ばす件数（ページ送り）"},
                "free_only": {"type": "boolean", "description": "有料モデルを除き、Free だけを対象にする"},
                "all": {"type": "boolean", "description": "検索モードを強制（引数なしで全件を見たいとき）"},
                "probe": {"type": "boolean",
                          "description": "候補を実際に呼んで生存確認し、**呼べないモデルを除外**する"
                                         "（一覧は廃止・未有効を含むため。呼び出しが発生する）"},
                "probe_limit": {"type": "integer", "description": "生存確認する件数（既定 12・最大 40）"},
                "probe_prompt": {"type": "string", "description": "生存確認に使う短い問い"},
                "probe_max_tokens": {"type": "integer", "description": "生存確認の上限トークン（既定 16）"},
                "stats": {"type": "boolean", "description": "品質統計（形式適合・CoT混入・切断の観測）を含める"},
            },
        },
    },
    {
        "name": "freeagent_ask",
        "description": (
            "【使う条件】独立した下読み・分類・下書き・要約を1つ任せたい／メインと別のモデルの第二意見が欲しい。"
            "【使わない条件】単発の事実確認は web_search や lookup が速い。"
            "【差分】安価な小型モデルの1回呼び出し。複数視点が要るなら panel を使う。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "依頼文"},
                "model": {"type": "string", "description": "provider/model。省略時は既定の Free モデル"},
                "system": {"type": "string", "description": "システム指示（任意）"},
                "max_tokens": {"type": "integer", "description": "上限トークン（既定 800）"},
                "temperature": {"type": "number"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "freeagent_fanout",
        "description": (
            "【使う条件】複数のプロンプト×複数モデルを並列に当てて相互検証・ベストオブN・多数決をしたい。"
            "【競合より優先】`deliberation` の ask_* を N 回並べるより速い（1 ターンを待たずに同時に走る）。"
            "【差分】速度と失敗の集計（wall と逐次見積り）付き。合意度が要るなら panel。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompts": {"type": "array", "items": {"type": "string"}, "description": "依頼文の配列"},
                "prompt": {"type": "string", "description": "単一の依頼文（prompts の代わり）"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer", "description": "自動選択するモデル数（既定 2）"},
                "system": {"type": "string"},
                "max_tokens": {"type": "integer"},
            },
        },
    },
    {
        "name": "freeagent_panel",
        "description": (
            "【使う条件】(a) 1つの問いに独立した複数視点が要る (b) 意見が割れそう (c) 多数決・第二意見が欲しい。"
            "【競合より優先】`delegate_task`（同じモデルの分身＝多様性ゼロ）／`deliberation`（単発集約で"
            "合意度の推移・少数意見の保持が無い）。"
            "【差分】合意度・不一致・確信度平均・「メインに確認したい点」を構造化して返す。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer", "description": "参加モデル数（既定 3）"},
                "prefer": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "max_tokens": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "freeagent_lookup",
        "description": (
            "【使う条件】出典URLが要る／判断の前に知識を補強したい／LLM を介さず一次情報に当たりたい。"
            "【差分】LLM を使わないので幻覚が無い。arXiv・Crossref・OpenAlex（論文）／Wikipedia・Wikidata"
            "（百科・構造化）／GitHub（コード）を並列に引く。"
            "【使わない条件】単一の事実だけなら web_search が速い。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "wikipedia / wikidata / arxiv / crossref / openalex / github"},
                "limit": {"type": "integer", "description": "各ソースの件数（既定 3）"},
                "lang": {"type": "string", "description": "Wikipedia/Wikidata の言語（既定 ja）"},
                "github_kind": {"type": "string", "description": "repo / issue / code（code はトークン必須）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "freeagent_grounded",
        "description": (
            "【使う条件】出典に基づく回答が要る（幻覚を抑えたい）／根拠が薄い話題で複数の意見が欲しい。"
            "【競合より優先】`deliberation` や素の panel は根拠を持たない（知識の穴と古さがそのまま出る）。"
            "【差分】先に外部知識を取得し、番号付きの根拠として注入してから答えさせる。"
            "回答には [番号] の引用が付き、引用の有無を機械的に数えて返す。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
                "lang": {"type": "string"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer", "description": "参加モデル数（既定 2）"},
                "max_tokens": {"type": "integer"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "freeagent_map",
        "description": (
            "【使う条件】大量の要素へ同じ指示（要約・分類・抽出）を並列適用し、必要なら reduce で統合したい。"
            "【使わない条件】要素が 1〜2 個なら ask を直接呼ぶ。"
            "【差分】map-reduce を 1 呼び出しで完結。個別の成功/失敗を行ごとに返す。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}},
                "instruction": {"type": "string", "description": "各要素へ適用する指示"},
                "model": {"type": "string"},
                "reduce": {"type": "boolean", "description": "true なら全出力を統合する"},
                "reduce_model": {"type": "string"},
                "max_tokens": {"type": "integer"},
            },
            "required": ["items", "instruction"],
        },
    },
    {
        "name": "freeagent_consult",
        "description": (
            "【使う条件】(a) 複数観点の検討と統合 (b) 意見が割れそう (c) 参加者どうしを討議させたい"
            "（debate_depth=\"deep\" で独立回答→反論→統合の3段）。"
            "【競合より優先】`delegate_task`（分身＝多様性ゼロ）／`deliberation`（単発集約）。"
            "【差分】サブが「メインに確認したい点」を返し、メインが main_reply で答えると同じ session_id で"
            "再開して前提を更新する（セッションは再起動をまたいで保持）。"
            "【注意】返るのは仮説と根拠。最終判断はメインが行う。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "session_id": {"type": "string", "description": "継続する相談のID（返り値の next_call に同梱）"},
                "main_reply": {"type": "string", "description": "サブの問いへのメインの回答"},
                "mode": {"type": "string", "description": "discuss / review（review は draft を査読）"},
                "draft": {"type": "string", "description": "mode=review のときの査読対象"},
                "debate_depth": {"type": "string", "description": "normal / deep"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer"},
                "prefer": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "max_tokens": {"type": "integer"},
            },
        },
    },
    {
        "name": "freeagent_agent",
        "description": (
            "【使う条件】サブに自分で調べさせたい（読み取り専用の知識ツールを自分で叩く）／"
            "根拠を集めさせてから結論を出させたい。"
            "【差分】サブLLMが lookup(arXiv/Crossref/OpenAlex/Wikipedia/Wikidata/GitHub) を自分で呼ぶループ。"
            "書き込み・外部副作用は無い。"
            "【使わない条件】1回の問いで足りるなら ask。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer", "description": "サブエージェント数（既定 2）"},
                "max_steps": {"type": "integer", "description": "1体あたりのツール呼び出し上限（既定 2）"},
                "main_reply": {"type": "string", "description": "メインの補足（最優先の前提として注入）"},
                "max_tokens": {"type": "integer"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "freeagent_delegate",
        "description": (
            "【使う条件】フルツール付きの Hermes 本体を独立プロセスで走らせる重い委譲（調査から実作業まで）。"
            "【使わない条件】既定では無効（FREEAGENT_ALLOW_AGENT=1 で有効化）。"
            "【差分】起動コストが高い（実測 15〜20 秒/回）が、既存のツールセット・skills・独立セッションを使える。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "timeout": {"type": "integer", "description": "秒（既定 300）"},
            },
            "required": ["task"],
        },
    },
]

HANDLERS = {
    "freeagent_models": tool_models,
    "freeagent_ask": tool_ask,
    "freeagent_fanout": tool_fanout,
    "freeagent_panel": tool_panel,
    "freeagent_lookup": tool_lookup,
    "freeagent_grounded": tool_grounded,
    "freeagent_map": tool_map,
    "freeagent_consult": tool_consult,
    "freeagent_agent": tool_agent,
    "freeagent_delegate": tool_delegate,
}


# ================================================================ §8 表示（content）
#
# content は**人間が読むチャネル**。LLM 向けの指示文を混ぜない（読み手に意味不明な文が出る）。
# 注記・要約は数値から生成し、structuredContent と食い違わせない。

def render(name: str, data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)[:2000]
    if data.get("error"):
        return f"⚠️ {data['error']}"

    if name == "freeagent_models":
        lines = []
        for row in data.get("providers") or []:
            mark = "✓" if row.get("ready") else "—"
            note = f"  [{row['key_env']} 未設定 → 検索のみ]" if not row.get("ready") and row.get("key_env") else ""
            lines.append(f"  {mark} {row['provider']:11} {row['models']:4} モデル / Free {row['free']:3}{note}")
            if row.get("error"):
                lines.append(f"      エラー: {row['error'][:100]}")
        head = (f"サブLLMバックエンド（{sum(1 for r in data.get('providers') or [] if r.get('ready'))} プロバイダ有効）\n"
                + "\n".join(lines))
        head += (f"\nFree候補 {data.get('free_candidates')} 件（うち今すぐ使用可 {data.get('usable_now')}）"
                 f" / 全 {data.get('total_models')} モデル\n既定: {data.get('default_model')}")
        if data.get("cooling"):
            head += "\nクールダウン中: " + ", ".join(data["cooling"])
        q = data.get("query")
        if q:
            head += (f"\n\n🔍 検索: query={q.get('query')!r} provider={q.get('provider')}"
                     + (" free 限定" if q.get("free_only") else "")
                     + (f" offset={q['offset']}" if q.get("offset") else "")
                     + f" → {q.get('matched')} 件（Free {q.get('matched_free')}）"
                     f"／表示 {q.get('shown')} 件")
            if q.get("probed"):
                head += (f"\n   生存確認: 試行 {q.get('probe_attempted')} / 応答 {q.get('probe_alive')}"
                         + (f" / 遅い {q.get('probe_slow')}" if q.get("probe_slow") else "")
                         + "（404=廃止・403=権限なしは一覧から除外）")
                for row in (q.get("probe_dropped") or []):
                    head += f"\n     ✗ {row['ref']} [{row.get('verdict')}]: {(row.get('error') or '')[:80]}"
                for row in (q.get("probe_errors") or []):
                    head += f"\n     ◷ {row['ref']} [{row.get('verdict')}]: {(row.get('error') or '')[:80]}"
            for m in (data.get("models") or []):
                tags = []
                if m.get("free"):
                    tags.append("Free" + (f" via {','.join(m['free_via'])}" if m.get("free_via") else ""))
                if not m.get("usable"):
                    tags.append("キー未設定")
                if m.get("probe") and m["probe"] != "alive":
                    tags.append(f"要再確認={m['probe']}")
                ctx = f" ctx={m['context_length']}" if m.get("context_length") else ""
                head += f"\n  • {m['ref']}{ctx} [{', '.join(tags) or '有料'}]"
            if not (data.get("models") or []):
                hint = ("該当なし。query を短くするか provider を外してください" if not q.get("probed")
                        else "生存確認で全滅しました（キー・権限を確認してください）")
                head += f"\n  （{hint}）"
        for row in (data.get("stats") or [])[:12]:
            head += (f"\n  • {row['model']} [{row['status']}] 品質 {row['quality']}"
                     f" 観測 {row['observations']}")
        return head

    if name == "freeagent_ask":
        out = [f"[{data.get('served_by')}] {data.get('answer', '')}"]
        meta = [f"{data.get('latency_s')}s"]
        if data.get("fell_back"):
            meta.append(f"代替へ回った（要求 {data.get('model')}）")
        if data.get("truncated"):
            meta.append("切断あり")
        if data.get("cot_leak"):
            meta.append("思考過程の混入")
        out.append("(" + ", ".join(str(m) for m in meta) + ")")
        return "\n".join(out)

    if name == "freeagent_fanout":
        head = (f"{data.get('calls')} 呼び出し / 成功 {data.get('ok')} / 失敗 {data.get('failed')}"
                f" / wall {data.get('wall_s')}s（逐次なら約 {data.get('sequential_estimate_s')}s、"
                f"倍率 {data.get('speedup')}）")
        body = ""
        for row in (data.get("results") or [])[:12]:
            text = row.get("answer") or row.get("error") or ""
            body += f"\n  • {row['model']}: {truncate(text, 200)}"
        return head + body

    if name == "freeagent_panel":
        conf_mean = data.get("confidence_mean")
        conf_note = f"（確信度平均 {conf_mean}）" if isinstance(conf_mean, (int, float)) else ""
        lines = [f"参加 {data.get('answered')}/{len(data.get('models') or [])} 体"
                 f" / 合意度 {data.get('agreement')}{conf_note}"]
        for group in (data.get("consensus") or [])[:5]:
            lines.append(f"  • {', '.join(group['models'])}: {truncate(group['excerpt'], 160)}")
        if data.get("open_questions"):
            lines.append("⚠️ メインに確認したい点:")
            for row in data["open_questions"][:5]:
                lines.append(f"  - {row['model']}: {truncate(row['question'], 160)}")
        lines.append("ℹ️ 合意度は表層の一致であって正しさの保証ではありません。")
        return "\n".join(lines)

    if name == "freeagent_lookup":
        lines = [f"出典 {data.get('citation_count')} 件（{', '.join(data.get('sources') or [])}）"]
        for src, res in (data.get("results") or {}).items():
            items = res.get("items") or []
            if not items:
                lines.append(f"  × {src}: {(res.get('error') or '該当なし')[:90]}")
                continue
            lines.append(f"  ✓ {src} ({len(items)} 件)")
            for item in items[:2]:
                title = item.get("title") or item.get("label") or ""
                lines.append(f"      {truncate(title, 90)} — {item.get('url', '')}")
        return "\n".join(lines)

    if name == "freeagent_grounded":
        lines = [f"根拠 {data.get('citation_count')} 件 / 引用付き回答 "
                 f"{data.get('answers_with_citations')}/{data.get('answered')} 体"
                 f" / 合意度 {data.get('agreement')}"]
        for row in (data.get("answers") or []):
            if row.get("error"):
                lines.append(f"  • {row['model']}: ✗ {truncate(row['error'], 120)}")
                continue
            lines.append(f"  • {row['model']}: {truncate(row.get('answer') or '', 400)}")
        lines.append("出典:")
        for i, cite in enumerate(data.get("citations") or [], 1):
            lines.append(f"  [{i}] [{cite.get('source')}] {truncate(cite.get('title') or '', 80)} {cite.get('url')}")
        return "\n".join(lines[:40])

    if name == "freeagent_map":
        lines = [f"{data.get('count')} 件 / 成功 {data.get('ok')} / 失敗 {data.get('failed')}"
                 f"（モデル {data.get('model')}）"]
        for row in (data.get("results") or [])[:10]:
            text = row.get("output") or f"✗ {row.get('error', '')}"
            lines.append(f"  • {truncate(row.get('item') or '', 60)} → {truncate(text, 160)}")
        if data.get("reduced"):
            lines.append(f"統合:\n{truncate(data['reduced'], 1200)}")
        return "\n".join(lines)

    if name == "freeagent_consult":
        lines = [f"相談 {data.get('session_id')} / mode={data.get('mode')} / "
                 f"ラウンド{data.get('rounds_run')} / stage={data.get('stage')}",
                 f"合意度 {data.get('agreement')} / 確信度平均 {data.get('confidence_mean')}"]
        failed_rows = data.get("failed") or []
        shown = {(row.get("model"), row.get("error")) for row in failed_rows}
        for row in failed_rows:
            lines.append(f"  ✗ {row.get('model')}（このラウンドは脱落）: {truncate(row.get('error') or '', 90)}")
        for row in (data.get("consensus") or [])[:5]:
            conf = row.get("confidence")
            suffix = f"（確信度 {conf}）" if isinstance(conf, int) else ""
            lines.append(f"  • {row['model']}: {truncate(row.get('conclusion') or '', 160)}{suffix}")
        debate = data.get("debate_summary")
        if debate:
            lines.append("討論推移（表層合意度）: "
                         + " → ".join(str(x) for x in debate.get("agreement_by_round", [])))
            for p in debate.get("participants", [])[:5]:
                lines.append(f"  ◦ {p['model']}: 初回「{truncate(p.get('initial_position') or '', 60)}」"
                             f" → 最終「{truncate(p.get('final_position') or '', 60)}」"
                             + ("（立場変更）" if p.get("changed") else ""))
                if p.get("strongest_objection"):
                    lines.append(f"      反論: {truncate(p['strongest_objection'], 120)}")
            for row in debate.get("dropped") or []:
                if (row.get("model"), row.get("error")) in shown:
                    continue  # 直近ラウンドの脱落として既に出している（二重表示しない）
                lines.append(f"  ✗ {row.get('model')}（第{row.get('round')}ラウンド "
                             f"{row.get('kind')} で脱落）: {truncate(row.get('error') or '', 90)}")
        if data.get("open_questions_for_main"):
            lines.append("⚠️ メインに確認したい点:")
            lines += [f"  - {q}" for q in data["open_questions_for_main"][:5]]
            lines.append("→ 回答して session_id を付けて再呼び出ししてください（main_reply）")
        lines.append("ℹ️ 最終判断はメイン。サブの出力は仮説として扱ってください。")
        return "\n".join(lines)

    if name == "freeagent_agent":
        lines = [f"【サブエージェント調査】{data.get('answered')}/{len(data.get('models') or [])} 体が回答"
                 f" / ツール呼び出し {data.get('tool_calls')} 回"
                 f" / 出典 {data.get('citation_count')} 件 / 合意度 {data.get('agreement')}"]
        for row in (data.get("agents") or []):
            if row.get("error"):
                lines.append(f"  • {row['model']}: ✗ {truncate(row['error'], 120)}")
                continue
            lines.append(f"  • {row['model']}（{row.get('steps')}ステップ）: "
                         f"{truncate(row.get('answer') or '', 220)}")
            if row.get("steps_exhausted"):
                lines.append("      ⚠️ ステップ上限に達し、回答に到達しませんでした（推測はしません）")
                for ev in (row.get("evidence") or [])[:4]:
                    lines.append(f"      根拠: {truncate(ev, 130)}")
            for step in (row.get("trace") or [])[:3]:
                lines.append(f"      ↳ {step['tool']}"
                             f"({truncate(json.dumps(step['args'], ensure_ascii=False), 60)})"
                             f" hits={step['hits']}")
        if data.get("citations"):
            lines.append("出典:")
            for cite in data["citations"][:6]:
                lines.append(f"  - [{cite.get('source')}] {truncate(cite.get('title') or '', 80)} {cite.get('url')}")
        lines.append(f"ℹ️ {data.get('usage_note', '')}")
        return "\n".join(lines)

    if name == "freeagent_delegate":
        return (f"exit={data.get('exit_code')}（{data.get('elapsed_s')}s）\n"
                f"{truncate(data.get('stdout') or '', 2000)}")

    return truncate(json.dumps(data, ensure_ascii=False), 2000)


# ================================================================ §9 JSON-RPC / stdio

def write_line(line: str) -> None:
    """1 行を **必ず UTF-8 バイト列として** stdout へ書く。

    MCP 仕様は UTF-8 固定。`sys.stdout.write` はロケール依存で、日本語 Windows では
    クライアントが環境変数を整えて起動すると cp932 になる（実測）。すると日本語を含む JSON が
    クライアント側の UTF-8 デコードで壊れ、`Failed to parse JSONRPC message from server` として
    **応答が黙って捨てられ**、接続が connect_timeout まで固まる。
    """
    data = (line + "\n").encode("utf-8")
    buf = getattr(sys.stdout, "buffer", None)
    if buf is not None:
        buf.write(data)
        buf.flush()
    else:  # buffer が無い環境（テスト用の StringIO など）
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def iter_stdin_lines():
    """stdin を UTF-8 として 1 行ずつ読む（同じ理由でロケール依存を避ける）。"""
    buf = getattr(sys.stdin, "buffer", None)
    if buf is None:
        yield from sys.stdin
        return
    for raw in buf:
        yield raw.decode("utf-8", "replace")


def respond(msg_id, result=None, error=None) -> None:
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    _debug("out", out)
    with WRITE_LOCK:
        write_line(json.dumps(out, ensure_ascii=False))


def handle_tool_call(params: dict) -> dict:
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    handler = HANDLERS.get(name)
    if handler is None:
        return {"content": [{"type": "text", "text": f"未知のツール: {name}"}], "isError": True}
    try:
        data = handler(args)
    except Exception as exc:  # ツールは絶対に例外を漏らさない
        data = {"error": f"internal error: {type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        data = {"error": f"internal error: handler returned {type(data).__name__}"}
    is_error = bool(data.get("error"))
    text = render(name, data)
    if is_error:
        text = "⚠️ 実行は失敗しました。structuredContent.error を確認してください。\n" + text
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": data, "isError": is_error}


def serve() -> None:
    # ロケール非依存にする（PYTHONUTF8 の有無で cp932 に落ちる環境があるため）
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS * 2)
    for raw in iter_stdin_lines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        _debug("in", msg)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            offered = ((msg.get("params") or {}).get("protocolVersion") or "")
            respond(msg_id, {
                "protocolVersion": negotiate_protocol(offered),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            respond(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            pool.submit(lambda i=msg_id, p=msg.get("params") or {}: respond(i, handle_tool_call(p)))
        elif method == "ping":
            respond(msg_id, {})
        elif msg_id is not None:
            respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


def main() -> None:
    """エントリポイント（python -m freeagent_bind / console_scripts から呼ばれる）。"""
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
