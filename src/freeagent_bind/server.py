#!/usr/bin/env python3
"""hermes-freeagent-bind — Hermes Agent の Free モデルをサブLLMとして並列に走らせ、
外部知識（既定6ソース＋DataCite / OpenAIRE / Europe PMC / Zenodo / ROR）で根拠づける MCP サーバー。

これは **モノリス**（単一ファイル）として書く。理由: 配布物が 1 つの stdio スクリプトで完結し、
遅延 import や相対 import の取り回しでクライアント側の起動が壊れる事故が無い（実測: stdio 起動後に
ネイティブ拡張を import すると無応答になる環境がある）。肥大化は前提なので、増築は「§区画の追加」で
行い、目次をこの docstring に保つ。目次が実装とずれたら、それは設計が崩れた合図。

目次
  §0 定数・設定        §1 ユーティリティ     §2 永続ストア（§2.1 クールダウン / §2.2 品質統計 /
                                             §2.3 トレース / §2.4 相談セッション /
                                             §2.5 プロバイダ認証の記憶 / §2.6 思考台帳 /
                                             §2.7 思考台帳の構造（分解・改訂・分岐・仮説））
  §3 プロバイダとモデル（§3.1 Free-tier provider guards）  §4 サブLLM呼び出し    §5 知識バックエンド（§5.8 締め切り / §5.9 DataCite /
                    §5.10 明示許可代替 / §5.11 ホスト予算 / §5.12 Europe PMC・OpenAIRE / §5.13 引用統合 / §5.14 Zenodo / §5.15 ROR /
                    §5.16 DOAJ・npm・crates.io）
  §6 ツール実装（§6.9 思考台帳 / §6.10 台帳の構造の検証・閲覧・代替案 / §6.11 本文注入番号）
  §7 ツール定義         §8 表示（content）
  §8.5 失敗時の「次の一手」（structuredContent.next_action）
  §8.6 ハーネス判別（Hermes 以外で起動されたときの警告）   §9 JSON-RPC / stdio

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

import copy
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
SERVER_VERSION = "0.4.0"

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
    # GroqのFree-plan表に掲載されたチャットモデルだけを許可する。
    # Free契約かどうかはAPIから確認できないため、利用者の明示確認を必須にする。
    "groq": {
        "base_url": os.environ.get("FREEAGENT_GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
        "key": os.environ.get("GROQ_API_KEY", ""),
        "key_env": "GROQ_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("openai/gpt-oss-120b", "openai/gpt-oss-20b",
                            "qwen/qwen3.8-27b"),
        "required_env_flags": ("FREEAGENT_GROQ_FREE_TIER",), "catalog_requires_ready": True,
        "note": "Groq Free tier確認が必須（FREEAGENT_GROQ_FREE_TIER=1）。Free対象モデルだけ許可。Developer tierは有料です",
    },
    "cloudflare": {
        "base_url": ("https://api.cloudflare.com/client/v4/accounts/"
                     + os.environ.get("CLOUDFLARE_ACCOUNT_ID", "") + "/ai"),
        "key": os.environ.get("CLOUDFLARE_API_TOKEN", ""),
        "key_env": "CLOUDFLARE_API_TOKEN", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("@cf/openai/gpt-oss-20b", "@cf/zai-org/glm-4.7-flash"),
        "required_env_values": ("CLOUDFLARE_ACCOUNT_ID",),
        "required_env_patterns": {"CLOUDFLARE_ACCOUNT_ID": r"[0-9a-fA-F]{32}"},
        "required_env_flags": ("FREEAGENT_CLOUDFLARE_FREE_PLAN",), "catalog_requires_ready": True,
        "models_path": "/models/search?format=openrouter&hide_experimental=true&per_page=100",
        "chat_path": "/v1/chat/completions", "catalog_format": "cloudflare_openrouter",
        "note": "Workers Free plan確認が必須（FREEAGENT_CLOUDFLARE_FREE_PLAN=1）。Neuronsは10,000/日を全用途で共有。有料専用モデルは許可しません",
    },
    # GeminiのFree対象は、公式pricing表で入力・出力がFreeのモデルだけを絞る。
    # Unpaid Servicesはプロンプト/応答を製品改善や人手レビューに使うため、 tierとデータ用途の二重明示確認を要求。
    "gemini": {
        "base_url": os.environ.get("FREEAGENT_GEMINI_BASE_URL",
                                  "https://generativelanguage.googleapis.com/v1beta/openai"),
        "key": os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", ""),
        "key_env": "GEMINI_API_KEY", "free_kind": "allowlist", "always_ready": False,
        "free_model_ids": ("gemini-3.8-flash", "gemini-3.7-flash"),
        "required_env_flags": ("FREEAGENT_GEMINI_FREE_TIER", "FREEAGENT_GEMINI_UNPAID_DATA_ACK"),
        "catalog_requires_ready": True,
        "note": "Gemini Free tierと非機密データ利用の両確認が必須。Unpaid prompts/outputs may be reviewed and used to improve Google products; OpenAI compatibility is Beta",
    },
}
# MCP の `initialize` 応答に載せる `instructions`。**Hermes はこれを読まない**（実測: 旧実装で
# 自発率が上がらず、現行版のソース `tools/mcp_tool_*.py` にも参照が無いことを確認）。それでも
# 他クライアント（Claude Desktop 等）は読むため返すが、**Hermes で効かせる唯一のレバーは
# `description` と、毎ターン注入される memory / AGENTS.md の判断規則**（旧実装の実測: 記述だけでは
# 1/2 で頭打ち、memory ＋ 競合の汎用面除外で 2/2）。
PROACTIVE_INSTRUCTIONS = (
    "Free モデルをサブ LLM として並列に走らせるサーバー。1 ターンで複数の freeagent_* を"
    "並列に呼んでよい。独立した複数視点が要るとき（設計判断・リスク抽出・意見が割れそうな問い）は "
    "freeagent_panel / freeagent_consult、出典が要るときは freeagent_lookup / freeagent_grounded、"
    "大量要素の一括処理は freeagent_map。**2 段以上の推論が要る問題では、考え始める前に freeagent_think を"
    "開き、plan で分解・revises_thought で改訂・branch_from_thought で分岐・total_thoughts で見積り調整・"
    "kind=hypothesis と tests_hypothesis で仮説の生成と検証を積みながら進める**（要所だけ verify / "
    "propose_alternatives で別モデルに反証・別案を出させる。1 問 1 答の質問には使わない）。"
    "delegate_task は同一モデルの分身で多様性が無い。"
    "**このサーバーが無効・不通のときは、存在しないツールを探さず通常の手段で回答を完遂し、"
    "実際に応答した独立ソースの件数を回答に明記する**（1 件しか取れていないのに「複数視点で検討した」"
    "と書かない）。"
)

PROVIDER_ORDER = [n.strip() for n in os.environ.get(
    "FREEAGENT_PROVIDER_ORDER", "nous,openrouter,nvidia,huggingface,groq,cloudflare,gemini").split(",")
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


def independent_answers(rows: list[dict]) -> list[dict]:
    """fallback で同じ実モデルが複数回現れた場合、合意度計算では 1 回だけ数える。"""
    selected = {}
    for row in rows:
        source = as_str(row.get("served_by")) or as_str(row.get("model"))
        selected.setdefault(source, row)
    return list(selected.values())


def truncate(text: str, limit: int) -> str:
    t = text or ""
    return t if len(t) <= limit else t[:limit] + "…"


# ================================================================ §2 永続ストア
#
# 蓄積するもの: クールダウン（429/404 の記憶）・品質統計・相談セッション・呼び出しトレース。
# すべて**一時領域に置かない**。書き込みは一時ファイル＋os.replace の原子置換で行い、
# 壊れたファイルは無視して空から始める（例外をツールへ漏らさない）。
# トレースと品質統計には**本文を残さない**（外部 API へ送った内容をディスクに置かない方針）。

def _sweep_stale_tmp(path: str, *, min_age_s: float = 60.0) -> int:
    """同じ対象の**書きかけファイル**（`<name>.<pid>.<tid>.tmp`）を掃除する。

    実測: 書き込みの途中でプロセスが落ちると（または os.replace が失敗すると）一時ファイルが
    状態ディレクトリに**永久に残る**。再起動のたびに増えるので、原子置換の副作用として掃除する。
    並行して書いている別スレッドの一時ファイルを壊さないよう、**古いものだけ**を対象にする
    （単一ファイルへの書き込みは `threading.Lock` の内側なので、60 秒前の残骸は必ず放棄済み）。
    """
    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    removed = 0
    try:
        now = time.time()
        for name in os.listdir(directory):
            if not (name.startswith(base + ".") and name.endswith(".tmp")):
                continue
            full = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(full) < min_age_s:
                    continue
                os.remove(full)
                removed += 1
            except OSError:
                continue          # 消せない（使用中・権限）ものは放置して続行
    except OSError:
        pass
    return removed


def _atomic_write(path: str, text: str) -> bool:
    _sweep_stale_tmp(path)
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
    errors = row.get("err")
    if isinstance(errors, dict):
        row["err"] = {key: as_float(value, 0.0) * factor for key, value in errors.items()}


def _decay_model(entry: dict, now: float) -> None:
    """モデル別の観測を半減期で減衰し、古い履歴が品質順位を固定しないようにする。"""
    previous = as_float(entry.get("updated_at"), now)
    elapsed = max(0.0, now - previous)
    if elapsed:
        factor = 0.5 ** (elapsed / (_STATS_HALFLIFE_DAYS * 86400.0))
        for row in (entry.get("kinds") or {}).values():
            if isinstance(row, dict):
                _decay(row, factor)
    entry["updated_at"] = now


def _stats_prune(models: dict, now: float) -> dict:
    alive = {ref: row for ref, row in models.items()
             if isinstance(row, dict)
             and now - as_float(row.get("updated_at"), now) <= STATS_STALE_DAYS * 86400}
    if len(alive) > STATS_MAX_REFS:
        ordered = sorted(alive, key=lambda ref: as_float(alive[ref].get("updated_at"), 0.0), reverse=True)
        alive = {ref: alive[ref] for ref in ordered[:STATS_MAX_REFS]}
    return alive


_STATS_WRITE_LOCK = threading.Lock()


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
    now = now_ts()
    with _STATS_WRITE_LOCK:
        with _STATS_LOCK:
            _STATS["models"] = _stats_prune(_STATS["models"], now)
            payload = {"version": 1, "models": copy.deepcopy(_STATS["models"])}
        return _atomic_write(stats_path(), json.dumps(payload, ensure_ascii=False))


def classify_error(err: str) -> str:
    """エラー文字列を分類する（集計の粒度を揃えるため、ここで語彙を固定する）。"""
    t = (err or "").lower()
    if any(part.strip(".,:;()[]") == "429" for part in t.split()) or "rate limit" in t or "rate_limit" in t or "rate-limit" in t or "too many requests" in t or "quota" in t:
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


# 環境障害（**モデルやプロバイダの責任ではない**失敗）。プロキシ停止・DNS 不達・TCP 拒否・
# タイムアウトなど。品質統計に入れると「プロキシが落ちていた 10 分」が全モデルの成績を下げ、
# 復旧後も選抜が歪む（副作用）。記録は残すが**統計には数えない**。
_ENV_FAILURE_SIGNS = (
    "urlerror", "winerror", "connection refused", "connectionrefused",
    "connection reset", "connectionreset", "connection aborted",
    "timed out", "timeout", "getaddrinfo", "name resolution", "network is unreachable",
    "temporary failure in name resolution", "ssl", "proxy error", "no route to host",
)


def is_env_failure(err: str) -> bool:
    """接続不可・タイムアウト等（＝モデルの成績ではない失敗）かどうか。"""
    low = (err or "").lower()
    if not low:
        return False
    if any(sign in low for sign in _ENV_FAILURE_SIGNS):
        return True
    # 「モデルが解決できませんでした（Free モデルが 0 件の可能性）」「推論可能な Free モデルが 0 件です」
    return "モデルが 0 件" in err or "モデルが解決できませんでした" in err


def note_observation(ref: str, kind: str, *, error: str = "", leak: bool = False,
                     trunc: bool = False, empty: bool = False, lat_ms: float = 0.0) -> None:
    """1 回の呼び出し結果を記録する（ok は「エラーが無く、空でもなく、切断もされていない」）。"""
    if not STATS_ENABLED:
        return
    _ensure_stats_loaded()
    now = now_ts()
    with _STATS_LOCK:
        entry = _STATS["models"].setdefault(ref, {"kinds": {}, "updated_at": now})
        _decay_model(entry, now)
        row = entry["kinds"].setdefault(kind, _empty_kind())
        row["n"] = as_float(row.get("n"), 0.0) + 1.0
        if error:
            cls = classify_error(error)
            err = row.setdefault("err", {})
            err[cls] = as_float(err.get(cls), 0.0) + 1.0
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


    _stats_save()


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
    err_total = sum(as_float(v, 0.0) for v in err.values()) if isinstance(err, dict) else 0.0
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

    def is_available(ref: str) -> bool:
        """在庫（一覧）にあるか。**`モデル:提供元` の経路指定も受ける**（HF は同じモデルでも
        提供元ごとに無料/有料・生死が違うので、`inclusionAI/…:novita` のように指定して使う。
        在庫一覧は提供元サフィックス無しの ID を返すため、素の突き合わせだと除外されてしまう）。
        """
        return ref in avail_set or ref.rsplit(":", 1)[0] in avail_set

    def add(refs: list[str]) -> None:
        for ref in refs:
            if is_available(ref) and ref not in chosen:
                chosen.append(ref)

    if requested:
        unknown = [r for r in requested if not is_available(r)]
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
    if is_env_failure(error):
        # **環境障害（プロキシ停止・DNS 不達・TCP 拒否）は状態を一切書かない。** モデルの成績ではないので
        # 統計に入れると復旧後も選抜が歪み、トレースにも意味のある情報が無い（切り分けは
        # FREEAGENT_DEBUG_LOG で足りる）。ここを no-op にすることで「不通でも副作用ゼロ」を
        # `scripts/check_offline.py` が検証できる（状態ディレクトリにファイルが増えないこと）。
        _debug("env_failure", {"ref": ref, "kind": kind, "error": error[:160]})
        return
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
    return f"s{int(now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}{os.urandom(8).hex()}"


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


# ---------------------------------------------------------------- §2.6 思考台帳
#
# メイン LLM の思考ステップを 1 件ずつ積む**作業台帳**（`freeagent_think`）。§2.4 と同じ規律で扱う:
# 知識は蓄積しない・原子置換・TTL・上限・期限切れは復元しない。
#
# 設計上の位置づけ: 思考を記録するだけのツール（思考メモ帳）は、それ自体は
# 知能を足さない（思考の中身はメインが書く）。この台帳の付加価値は **各ステップを生成者以外の
# 独立モデルに反証させる**点にあり、検証は `verify=true` のときだけ走る（既定は台帳のみ＝サブ呼び出し
# ゼロで高速。全ステップに検証を付けると 1 ターンが分単位になる）。
#
# **環境障害では書かない**（規約 21）: 検証を要求したのにバックエンドへ到達できなかった思考を
# 「記録済み」にすると、検証されていない前提の上に次のステップが積まれる。この契約は
# `scripts/check_offline.py` が「状態ディレクトリにファイルが増えないこと」で検証する。

_THOUGHTS: dict[str, dict] = {}
_THOUGHTS_LOADED = False
_THOUGHTS_LOCK = threading.RLock()
THOUGHTS_ENABLED = _env_on("FREEAGENT_THOUGHTS", True)
# 相談セッション（1 時間）より長めに持つ（思考の連鎖は 1 ターンをまたぐことがある）。
THOUGHT_TTL_S = _env_float("FREEAGENT_THOUGHT_TTL", 7200.0)
THOUGHT_MAX = _env_int("FREEAGENT_THOUGHT_MAX", 32)
# 1 台帳あたりの思考数上限（暴走・無限ループの歯止め。超過は黙って捨てずエラーで返す）。
THOUGHT_MAX_STEPS = max(1, min(200, _env_int("FREEAGENT_THOUGHT_MAX_STEPS", 24)))
_THOUGHT_CHARS = max(100, min(8000, _env_int("FREEAGENT_THOUGHT_CHARS", 2000)))


def thoughts_path() -> str:
    return os.environ.get("FREEAGENT_THOUGHTS_PATH") or os.path.join(state_dir(), "thoughts.json")


def _thoughts_prune(data: dict, now: float) -> dict:
    alive = {k: v for k, v in data.items()
             if isinstance(v, dict) and as_float(v.get("updated_at"), 0.0) > now - THOUGHT_TTL_S}
    if len(alive) > THOUGHT_MAX:  # 新しい順に上限まで残す
        keep = sorted(alive, key=lambda k: as_float(alive[k].get("updated_at"), 0.0), reverse=True)
        alive = {k: alive[k] for k in keep[:THOUGHT_MAX]}
    return alive


def _ensure_thoughts_loaded() -> None:
    global _THOUGHTS_LOADED
    if _THOUGHTS_LOADED or not THOUGHTS_ENABLED:
        return
    with _THOUGHTS_LOCK:
        if _THOUGHTS_LOADED:
            return
        data = _load_json(thoughts_path(), {})
        rows = data.get("chains") if isinstance(data, dict) else None
        _THOUGHTS.update(_thoughts_prune(rows if isinstance(rows, dict) else {}, now_ts()))
        _THOUGHTS_LOADED = True


def _thoughts_save() -> bool:
    if not THOUGHTS_ENABLED:
        return False
    with _THOUGHTS_LOCK:
        payload = {"version": 1, "saved_at": now_ts(), "chains": _thoughts_prune(_THOUGHTS, now_ts())}
    return _atomic_write(thoughts_path(), json.dumps(payload, ensure_ascii=False))


def new_thought_id() -> str:
    return f"t{int(now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}{os.urandom(6).hex()}"


def thought_get(sid: str) -> dict | None:
    if not sid:
        return None
    _ensure_thoughts_loaded()
    now = now_ts()
    with _THOUGHTS_LOCK:
        row = _THOUGHTS.get(sid)
        if not row or as_float(row.get("updated_at"), 0.0) <= now - THOUGHT_TTL_S:
            _THOUGHTS.pop(sid, None)
            return None
        return json.loads(json.dumps(row))  # 呼び出し側の書き換えが他へ波及しないよう複製を渡す


def thought_merge(sid: str, step: dict, *, question: str = "", verifier_models: list | None = None,
                  assign_number: bool = False, ops: dict | None = None) -> dict:
    """台帳へ 1 ステップを**1 つのロック内で**統合する（読み・採番・書きを分けない）。

    読みと書きを分けると、並列に呼ばれた 2 つの思考が同じ番号を採番して**片方が上書きで消える**
    （メインは 1 ターンで複数ツールを並行に呼ぶ前提で書く。規約 14）。`assign_number=True` のとき
    番号をロック内で採番する。`sid` が未知・期限切れなら**古い内容は復活させず**新しい ID を採番する。
    上限（`THOUGHT_MAX_STEPS`）に達していたら**黙って捨てず** `refused` を立てて返す。
    """
    _ensure_thoughts_loaded()
    with _THOUGHTS_LOCK:
        target = sid if sid in _THOUGHTS else new_thought_id()
        row = json.loads(json.dumps(_THOUGHTS.get(target) or {}))
        steps = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
        step = dict(step)
        if assign_number:
            step["n"] = max([as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0]) + 1
        number = as_int(step.get("n"), 0, 0, 9999)
        replaced = any(as_int(s.get("n"), 0, 0, 9999) == number for s in steps)
        if not replaced and len(steps) >= THOUGHT_MAX_STEPS:
            return {"session_id": target, "step": None, "steps_recorded": len(steps), "refused": True}
        old = next((s for s in steps if as_int(s.get("n"), 0, 0, 9999) == number), None)
        steps = [s for s in steps if as_int(s.get("n"), 0, 0, 9999) != number] + [step]
        steps.sort(key=lambda s: as_int(s.get("n"), 0, 0, 9999))
        # 他ステップ・台帳メタへの波及（改訂済みの印・仮説の状態・分岐の状態・計画）も**同じロック内**。
        _thought_apply_ops(row, steps, step, old, ops or {})   # §2.7
        row.update({
            "steps": steps,
            "question": question or row.get("question") or "",
            "verifier_models": verifier_models or row.get("verifier_models") or [],
            "created_at": row.get("created_at") or now_ts(),
            "updated_at": now_ts(),
        })
        _THOUGHTS[target] = row
        _thoughts_save()
    return {"session_id": target, "step": step, "steps_recorded": len(steps),
            "refused": False, "replaced": replaced}


def thought_drop(sid: str) -> None:
    _ensure_thoughts_loaded()
    with _THOUGHTS_LOCK:
        _THOUGHTS.pop(sid, None)
    _thoughts_save()


# ---------------------------------------------------------------- §2.7 思考台帳の構造（分解・改訂・分岐・仮説）
#
# 台帳を「思考の列」から**構造**へ上げる操作。`thought_merge` が**ロック内**で呼ぶ（読みと書きを
# 分けると並列呼び出しで印が消える。規約 24b）。引数の検証は §6.10 の `_think_structure` が**先に**
# 済ませる（検証に失敗した呼び出しでサブ呼び出しの予算を使わないため）。ここでは検証済みの操作を
# 適用するだけで、対象が見つからなければ黙って何もしない（ステップは削除されないので実際には起きない）。
#
# - 改訂: 改訂された元ステップに `superseded_by` を付ける（消さない＝履歴は残す）。
# - 仮説: `kind=hypothesis` のステップが状態（open/supported/refuted/inconclusive）を持つ。
# - 分岐: 台帳メタ `branch_meta` に分岐元・状態（open/adopted/abandoned/merged）を持つ。
# - 分解: 台帳メタ `plan`（サブ目標）。再送は計画の改訂で、達成済みの印は同じ文面の項目に引き継ぐ。
# - 見積り総数: 変化したときだけ `total_history` に積む（動的調整の履歴）。

THOUGHT_KINDS = ("step", "hypothesis", "test", "conclusion")
HYPOTHESIS_STATES = ("open", "supported", "refuted", "inconclusive")
BRANCH_STATES = ("open", "adopted", "abandoned", "merged")
THOUGHT_PLAN_MAX = 12


def _thought_apply_ops(row: dict, steps: list[dict], step: dict, old: dict | None, ops: dict) -> None:
    n = as_int(step.get("n"), 0, 0, 9999)
    by_n = {as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    # 同じ番号の再送（置き換え）でも、他ステップが付けた印は失わない。
    if old:
        if old.get("superseded_by") and not step.get("superseded_by"):
            step["superseded_by"] = old["superseded_by"]
        if old.get("kind") == "hypothesis" and step.get("kind") == "hypothesis":
            for key in ("tested_by", "hypothesis_status", "status_at"):
                if old.get(key):
                    step[key] = old[key]
    rev = as_int(ops.get("revises"), 0, 0, 9999)
    if rev and rev != n and rev in by_n:
        by_n[rev]["superseded_by"] = n
    hyp = as_int(ops.get("tests_hypothesis"), 0, 0, 9999)
    if hyp and hyp in by_n and by_n[hyp].get("kind") == "hypothesis":
        target = by_n[hyp]
        target["tested_by"] = [t for t in (target.get("tested_by") or []) if t != n] + [n]
        if ops.get("hypothesis_status") in HYPOTHESIS_STATES:
            target["hypothesis_status"] = ops["hypothesis_status"]
            target["status_at"] = n
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}
    bid = step.get("branch_id")
    if bid and bid not in meta:
        meta[bid] = {"from": step.get("branch_from_thought"), "status": "open",
                     "opened_at": n, "resolved_at": None}
    res = ops.get("resolve_branch")
    if res and res in meta and ops.get("branch_status") in BRANCH_STATES:
        meta[res]["status"] = ops["branch_status"]
        meta[res]["resolved_at"] = None if ops["branch_status"] == "open" else n
    row["branch_meta"] = meta
    plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    if ops.get("plan"):
        done = {p.get("text"): p.get("done_at") for p in plan if p.get("done_at")}
        plan = [{"id": i + 1, "text": text, "done_at": done.get(text)}
                for i, text in enumerate(ops["plan"][:THOUGHT_PLAN_MAX])]
        row["plan_revised_at"] = n
    sub = as_int(step.get("subgoal"), 0, 0, THOUGHT_PLAN_MAX)
    if sub and ops.get("subgoal_done"):
        for item in plan:
            if item.get("id") == sub:
                item["done_at"] = n
    row["plan"] = plan
    total = as_int(ops.get("total"), 0, 0, 999)
    history = [h for h in (row.get("total_history") or []) if isinstance(h, dict)]
    if total and (not history or history[-1].get("total") != total):
        history.append({"at": n, "total": total, "auto": bool(ops.get("total_auto"))})
    row["total_history"] = history[-24:]


# ================================================================ §3 プロバイダとモデル

def provider_ready(name: str) -> bool:
    return not _provider_missing_settings(PROVIDER_SPECS.get(name) or {})


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
    """接続には短い上限を、接続後の応答読取には呼び出し別の上限を設定する。"""
    response = urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT)
    # urllib の timeout は接続時にも socket に残るため、レスポンスを受け取ったら
    # 本文読取のために read timeout へ切り替える（HTTPS/HTTP 標準実装の socket）。
    try:
        sock = response.fp.raw._sock
        sock.settimeout(timeout)
    except (AttributeError, OSError):
        pass
    return response


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
    if kind == "allowlist":
        return provider_model_allowed(provider, as_str(row.get("id")))
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
    spec = PROVIDER_SPECS.get(provider) or {}
    if spec.get("catalog_requires_ready") and not provider_ready(provider):
        return []
    with _MODELS_LOCK:
        cached = _MODELS_CACHE.get(provider)
        if cached and now_ts() - as_float(cached.get("at"), 0.0) < ttl:
            return list(cached.get("rows") or [])
    rows: list[dict] = []
    error = ""
    try:
        data = provider_http(spec.get("models_path") or "/models", provider=provider,
                             timeout=min(30.0, READ_TIMEOUT))
        raw_rows = _provider_catalog_rows(provider, data)
        for raw in raw_rows:
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


# ================================================================ §3.1 Free-tier provider guards
# This subsection owns the extra activation, model-ID allowlist, and Cloudflare catalog contracts.
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
    allowed = (PROVIDER_SPECS.get(provider) or {}).get("free_model_ids")
    return allowed is None or as_str(model) in allowed


def _provider_catalog_rows(provider: str, data) -> list:
    """Normalize documented provider model-list envelopes; reject malformed catalogs."""
    spec = PROVIDER_SPECS.get(provider) or {}
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


def all_models(ttl: float = 600.0) -> list[dict]:
    out: list[dict] = []
    for provider in PROVIDER_ORDER:
        out.extend(fetch_provider_models(provider, ttl=ttl))
    return out


def provider_status() -> list[dict]:
    rows = []
    for provider in PROVIDER_ORDER:
        models = fetch_provider_models(provider)
        spec = PROVIDER_SPECS.get(provider) or {}
        with _MODELS_LOCK:
            error = (_MODELS_CACHE.get(provider) or {}).get("error") or ""
        rows.append({
            "provider": provider,
            "ready": provider_ready(provider),
            "models": len(models),
            "free": sum(1 for m in models if m.get("free")),
            "key_env": spec.get("key_env"),
            "note": spec.get("note"),
            "requires_activation": bool(spec.get("catalog_requires_ready")),
            "missing_settings": _provider_missing_settings(spec),
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

_FALLBACK_STATUS = {402, 400, 401, 403, 404, 410, 429, 500, 502, 503, 504}
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
        chat_path = (PROVIDER_SPECS.get(provider) or {}).get("chat_path") or "/chat/completions"
        data = provider_http(chat_path, provider=provider, payload=payload, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:
            pass
        finally:
            exc.close()
        raise HttpStatusError(int(exc.code), body, retry_after)
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
    """指定先が冷却中でも、冷却外かつ認証可能な Free 代替モデルを並べる。"""
    cooling = cooling_refs()
    out = [ref] if ref and ref not in cooling else []
    if len(out) >= _MAX_ATTEMPTS:
        return out[:_MAX_ATTEMPTS]
    blocked = {name for name in PROVIDER_ORDER if provider_auth_blocked(name)}
    for other in free_model_refs(free_only=free_only):
        if other == ref or other in cooling or other in out:
            continue
        if other.split("/", 1)[0] in blocked:
            continue
        out.append(other)
        if len(out) >= _MAX_ATTEMPTS:
            break
    return out


def _is_auth_error(status: int, body: str) -> bool:
    """401/403 が**認証の失敗**なのかを署名で判定する。

    実測: 403 は認証だけでは起きない。HF は権限不足（`does not have sufficient permissions to call
    Inference Providers`）だが、同じ HF の `prism-ml/…:together` は **Cloudflare の 403** を返し、
    OpenRouter の `:free` は「このモデルは使えない」の 403 を返す。これらを認証失敗として扱うと
    **プロバイダ全体を 15 分ブロックしてしまう**（生きている他モデルまで選抜から消える）。
    """
    if status == 401:
        return True          # 401 は定義上ずっと認証（未認証・無効トークン）
    low = (body or "").lower()
    return any(sign in low for sign in (
        "insufficient permissions", "authentication method", "unauthorized",
        "authorization failed", "authorization missing", "no cookie auth",
        "invalid api key", "invalid_api_key", "incorrect api key", "not authorized",
    ))


def _auth_hint(provider: str, status: int, body: str) -> str:
    """認証エラーは**原因と直し方**を返す（「すべての候補で失敗しました」だけでは直しようがない）。

    実測: HF の既存トークンは有効でも `403 This authentication method does not have sufficient
    permissions to call Inference Providers` を返す（fine-grained トークンに推論権限が無い）。
    """
    tail = truncate((body or "").replace("\n", " "), 160)
    if status == 402:
        if provider == "cloudflare":
            return ("HTTP 402（cloudflare）: Workers AIのFree割当/アカウント制限の可能性があります。"
                    "Workers Freeでは日次枠超過後に処理が停止し、Workers Paidでは超過分が課金されます。"
                    f"契約状態を確認してください / 応答: {tail}")
        if provider == "groq":
            return ("HTTP 402（groq）: Free tierの状態またはアカウント利用制限を確認してください。"
                    "Developer tierは従量課金です。"
                    f" / 応答: {tail}")
        if provider == "gemini":
            return ("HTTP 402（gemini）: Free tierのプロジェクト割当またはBilling設定を確認してください。"
                    "Billing有効時は課金が発生する場合があります。"
                    f" / 応答: {tail}")
        # クレジット枯渇。キーや権限の問題ではないので取り違えさせない（実測: HF の月次無料枠は
        # 生存確認やパネルを繰り返すと尽き、全モデルが 402 になる）。翌月に回復するため
        # **プロバイダ記憶（15 分）には入れない**。
        return (f"HTTP 402（{provider}）: クレジット枯渇（キーや権限の問題ではありません）。"
                f"別のプロバイダへ回します / 応答: {tail}")
    if provider == "huggingface":
        if "inference providers" in (body or "").lower():
            return ("HTTP 403（huggingface）: トークンに Inference Providers の権限がありません。"
                    "https://huggingface.co/settings/tokens で「Make calls to Inference Providers」を"
                    "有効にしたトークンを作り、env の HF_TOKEN に設定してください"
                    f" / 応答: {tail}")
        if status == 401:
            return ("HTTP 401（huggingface）: HF_TOKEN が未設定か無効です。"
                    f"Inference Providers の権限があるトークンを設定してください / 応答: {tail}")
        # 403 でも権限の文言が無い = 認証ではなく**提供元/CDN 側**の拒否（実測: Together 経由が
        # Cloudflare Error 1010 "Access denied" を返す）。トークンを疑わせない。
        return ("HTTP 403（huggingface）: トークンの権限ではなく**提供元側**で拒否されました"
                "（Cloudflare のブロックや提供元の障害）。`model:提供元` で別の提供元を試してください"
                f" / 応答: {tail}")
    hints = {
        "groq": ("GROQ_API_KEY が未設定か無効です" if status == 401 else
                 "Free対象モデル一覧とアカウントのモデル権限を確認してください"),
        "cloudflare": ("CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID と Workers AI Read 権限を確認してください" if status == 401 else
                       "Freeプラン・モデル利用権限を確認してください。Workers Paidへのアップグレードで超過利用が課金されます"),
        "gemini": ("GEMINI_API_KEY（または GOOGLE_API_KEY）とFree tierプロジェクトを確認してください" if status == 401 else
                   "Freeモデルの利用可否・プロジェクト割当・地域制限を確認してください"),
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
               timeout: float | None = None, claims: "_ModelClaims | None" = None) -> dict:
    """1 つのサブLLM呼び出し。**例外を外へ漏らさず**、失敗も dict で返す。

    `claims` を渡すと、フォールバック先に**同じ呼び出しの他の枠が使っている／除外されたモデル**を
    選ばない（`ask_many` が渡す）。渡さないと 2 枠が同じモデルで埋まり「独立 2 体」の表示が
    実質 1 体になる（実測: 代替案の提案者の枠を、同じ呼び出しの検証者と同じモデルが埋めた）。
    """
    provider, model = resolve_ref(ref, free_only=free_only)
    if not model:
        return {"error": "モデルが解決できませんでした（Free モデルが 0 件の可能性）",
                "ref": ref, "kind": kind}
    spec = PROVIDER_SPECS.get(provider) or {}
    if spec.get("free_model_ids") is not None and not provider_model_allowed(provider, model):
        return {"error": f"{provider}/{model} はこのFree-tier設定で許可されていないモデルです",
                "ref": ref, "kind": kind}
    if spec.get("catalog_requires_ready") and not provider_ready(provider):
        missing = _provider_missing_settings(spec)
        return {"error": f"{provider} は未設定/未確認です。必要なenv設定: {', '.join(missing)}",
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
    avoided: list[str] = []
    for idx, cand in enumerate(attempts):
        if claims is not None and cand != ref and not claims.take(cand):
            avoided.append(cand)   # 他の枠が使用中／除外済み。独立性を守るため取らない
            continue
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
            elif exc.status == 402:
                # クレジット枯渇はプロバイダ全体の認証障害ではないが、同一モデルへの
                # 連続要求を避けるため短時間だけモデル単位で休ませる。
                note_cooldown(cand, _COOLDOWN_DEFAULT_S, "HTTP 402 (credit depleted)")
                skipped.append(cand)
                last_error = _auth_hint(c_provider, exc.status, exc.body)
                _debug("credit_exhausted", {"ref": cand})
            elif exc.status in (401, 403):
                # 原因を残し、**認証の署名があるときだけ**プロバイダ単位で覚える。
                # 提供元都合の 403（モデル単位の制限・CDN のエラー）でプロバイダ全体を止めないため。
                last_error = _auth_hint(c_provider, exc.status, exc.body)
                if _is_auth_error(exc.status, exc.body):
                    note_provider_auth(c_provider, exc.status, exc.body or last_error)
                _debug("auth_error", {"ref": cand, "status": exc.status})
            if exc.status not in _FALLBACK_STATUS:
                break
            continue
        except Exception as exc:  # 接続不可・タイムアウト・JSON 壊れ
            last_error = f"{type(exc).__name__}: {exc}"
            if is_env_failure(last_error):
                break  # 環境障害では別モデルも同じ経路で失敗するため再試行を打ち切る
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
    if avoided and not last_error:
        fail["error"] = ("フォールバック先がすべて同じ呼び出しの他の枠で使用中か除外済みでした"
                         "（独立性を守るため同じモデルで枠を埋めていません）")
    if avoided:
        fail["avoided_duplicates"] = len(avoided)
    observe_call(fail, kind, "")
    return fail


class _ModelClaims:
    """1 回の並列呼び出し（`ask_many`）の中で**使用中のモデル**を記録する（フォールバックの重複防止）。

    各枠の本来のモデルと除外モデルを最初から「使用中」にしておき、フォールバックで新たに取る
    モデルはロック内で 1 回だけ確保できる（2 枠が同時に同じ予備モデルへ落ちるのを防ぐ）。
    """

    def __init__(self, refs, avoid=()):
        self._lock = threading.Lock()
        self._taken = {r for r in list(refs) + list(avoid or ()) if isinstance(r, str) and r}

    def take(self, ref: str) -> bool:
        with self._lock:
            if ref in self._taken:
                return False
            self._taken.add(ref)
            return True


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
             temperature: float | None = None, kind: str = "panel",
             avoid: list[str] | None = None) -> list[dict]:
    """同じプロンプトを複数モデルへ**同時に**投げる（1 ターン待たずに走るのが並列の利点）。

    「独立した複数の意見」を返す経路なので、フォールバックが**他の枠と同じモデル**や `avoid`
    （除外・同じ手順で既に使ったモデル）に落ちないようにする（`_ModelClaims`）。
    """
    claims = _ModelClaims(refs, avoid)
    return run_parallel(
        list(refs),
        lambda ref: call_model(ref, prompt, system=system, max_tokens=max_tokens,
                               temperature=temperature, kind=kind, claims=claims),
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
_KB_INFLIGHT: dict[str, threading.Event] = {}
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
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        status = int(exc.code)
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        finally:
            exc.close()
        if status in (403, 429, 503):
            _kb_block(host, _parse_retry_after(retry_after, None) if status == 429 else _KB_BLOCK_S)
        return status, body
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
    while True:
        with _KB_CACHE_LOCK:
            hit = _KB_CACHE.get(key)
            if hit and now_ts() - as_float(hit.get("at"), 0.0) < KB_TTL:
                return copy.deepcopy(hit.get("value"))
            event = _KB_INFLIGHT.get(key)
            if event is None:
                event = threading.Event()
                _KB_INFLIGHT[key] = event
                owner = True
            else:
                owner = False
        if owner:
            break
        event.wait()
    try:
        value = producer()
        if not (isinstance(value, dict) and value.get("error")):
            with _KB_CACHE_LOCK:
                _KB_CACHE[key] = {"at": now_ts(), "value": copy.deepcopy(value)}
                if len(_KB_CACHE) > 512:
                    for old in sorted(_KB_CACHE, key=lambda k: as_float(_KB_CACHE[k].get("at"), 0.0))[:128]:
                        _KB_CACHE.pop(old, None)
        return copy.deepcopy(value)
    finally:
        with _KB_CACHE_LOCK:
            _KB_INFLIGHT.pop(key, None)
            event.set()


def _plain_text(text: str, limit: int = 600) -> str:
    """マークアップ（JATS/HTML）と余分な空白を落として本文だけにする。

    Crossref の abstract は `<jats:p>…</jats:p>` 形式で返るため、そのまま注入すると
    タグが本文を占めてサブLLMが読めない。
    """
    return truncate(" ".join(re.sub(r"<[^>]+>", " ", text or "").split()), limit)


def _openalex_abstract(row: dict, limit: int = 500) -> str:
    """OpenAlex は本文を `abstract_inverted_index`（語 → 位置の配列）で返すので復元する。

    そのままでは人間にもサブLLMにも読めず、citation の summary が空になっていた。
    """
    inv = row.get("abstract_inverted_index")
    if not isinstance(inv, dict):
        return ""
    placed: dict[int, str] = {}
    for word, positions in inv.items():
        if not isinstance(positions, (list, tuple)):
            continue
        for pos in positions:
            index = as_int(pos, -1, 0, 10**6)
            if index >= 0:
                placed.setdefault(index, str(word))
    if not placed:
        return ""
    return truncate(" ".join(placed[k] for k in sorted(placed)), limit)


def _cite(source: str, title: str, url: str, **extra) -> dict:
    row = {"source": source, "title": truncate((title or "").strip(), 300), "url": url}
    for key, value in extra.items():
        if value not in (None, "", [], {}):
            row[key] = value
    return row


# ---------------------------------------------------------------- §5.1 Wikipedia

def kb_wikipedia(query: str, lang: str = "ja", limit: int = 3) -> dict:
    """本文の要約＋検索結果。出典 URL 付き（LLM 不使用）。"""
    lang = as_str(lang, "ja").lower()
    if not re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*", lang):
        return {"source": "wikipedia", "lang": lang, "items": [], "citations": [],
                "error": "lang は有効な Wikipedia 言語コードで指定してください"}
    limit = as_int(limit, 3, 1, 8)

    def produce() -> dict:
        base = f"https://{lang}.wikipedia.org"
        q = urllib.parse.urlencode({
            "action": "query", "format": "json", "generator": "search",
            "gsrsearch": query, "gsrlimit": limit, "prop": "extracts",
            "exintro": 1, "explaintext": 1, "exchars": 1200,
        })
        data, err = kb_json(f"{base}/w/api.php?{q}")
        if err:
            return {"source": "wikipedia", "error": err}
        pages = ((data or {}).get("query") or {}).get("pages") or {}
        hits = sorted((page for page in pages.values() if isinstance(page, dict)),
                      key=lambda page: as_int(page.get("index"), 999999, 0, 999999))
        items, cites = [], []
        for hit in hits[:limit]:
            title = as_str(hit.get("title"))
            if not title:
                continue
            page_url = f"{base}/wiki/{urllib.parse.quote(title.replace(' ', '_'))}"
            summary = re.sub(r"<[^>]+>", "", as_str(hit.get("extract")))
            items.append({"title": title, "url": page_url, "summary": truncate(summary, 1200),
                          "snippet": truncate(summary, 300)})
            cites.append(_cite("wikipedia", title, page_url, lang=lang,
                               summary=_plain_text(summary, 600)))
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
            cites.append(_cite("wikidata", hit.get("label") or qid, url, qid=qid,
                               summary=_plain_text(hit.get("description") or "", 300)))
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
                               authors=authors[:3],
                               summary=_plain_text(summary, 600)))
        return {"source": "arxiv", "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_cached(f"arxiv:{field_q}:{limit}", produce)


# ---------------------------------------------------------------- §5.4 Crossref

def kb_crossref(query: str, limit: int = 5) -> dict:
    """DOI 登録機関のメタデータ（書誌）。`mailto` を付けると polite pool に入る。"""
    limit = as_int(limit, 5, 1, 20)

    def produce() -> dict:
        params = {"query": query, "rows": limit,
                  "select": "DOI,title,author,issued,container-title,type,URL,abstract,is-referenced-by-count,publisher"}
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
            cites.append(_cite("crossref", title, url, year=year, doi=row.get("DOI") or "",
                               summary=_plain_text(row.get("abstract") or "", 500)))
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
                               cited_by=row.get("cited_by_count"),
                               summary=_openalex_abstract(row)))
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
            body_text = (row.get("description") or "") if kind == "repo" else (
                row.get("body") or "" if kind == "issue" else
                f"{((row.get('repository') or {}).get('full_name')) or ''} {row.get('path') or ''}")
            cites.append(_cite("github", title, row.get("html_url") or "", kind=kind,
                               summary=_plain_text(body_text, 400)))
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


# ---------------------------------------------------------------- §5.8 締め切りつきの並列取得
#
# 旧実装は `run_parallel` で**全ソースの完了を待っていた**。1 リクエストの上限は KB_TIMEOUT（20 秒）で、
# Wikidata は最大 3 回を直列に呼ぶため、1 つが遅れると全体がそれだけ待たされた。さらに並列数が
# min(ソース数, MAX_WORKERS=4) で、6 ソースでは 2 つが前の完了待ちになっていた（コードで確認）。
#   * ソースごとに専用スレッド（ソースはすべて別ホストなので、ホスト単位のリクエスト数は増えない）
#   * 全体の締め切り KB_DEADLINE（既定 8 秒。平常時の実測は 6 ソースとも 0.4〜2.4 秒）まで待ち、
#     間に合わなかったソースは `timed_out` の**脱落として返す**（隠さない）
#   * 脱落したソースの取得は裏で続ける。各ソースは `_kb_cached` を通るので、完了すれば次の呼び出しで
#     キャッシュから即座に返る（同じ問いの再試行が速くなる）
#   * ソースごとの所要秒を `timings` に載せる（遅いソースと時間帯を後から特定するため）
KB_DEADLINE = max(1.0, min(120.0, _env_float("FREEAGENT_KB_DEADLINE", 8.0)))


def _kb_gather(picked: list[str], query: str, limit: int, opts: dict,
               deadline: float | None = None) -> tuple[dict, dict]:
    """(ソース → 結果, ソース → 所要秒) を返す。締め切りを過ぎたソースは timed_out の結果にする。"""
    deadline = KB_DEADLINE if deadline is None else deadline
    opts = {**opts, "deadline_at": time.monotonic() + deadline}
    done: dict[str, dict] = {}
    spent: dict[str, float] = {}
    lock = threading.Lock()
    finished = threading.Event()
    remaining = [len(picked)]

    def work(src: str) -> None:
        start = time.monotonic()
        try:
            res = _kb_source_result(src, query, limit, opts)
        except Exception as exc:  # 例外を外へ漏らさない（規約 1）
            res = {"error": f"{type(exc).__name__}: {exc}"}
        with lock:
            done[src] = res if isinstance(res, dict) else {"error": "不正な結果"}
            spent[src] = round(time.monotonic() - start, 2)
            remaining[0] -= 1
            if remaining[0] <= 0:
                finished.set()

    if not picked:
        return {}, {}
    for src in picked:
        threading.Thread(target=work, args=(src,), name=f"kb-{src}", daemon=True).start()
    finished.wait(deadline)
    with lock:
        by_source = {src: done[src] for src in picked if src in done}
        timings = dict(spent)
    for src in picked:
        if src not in by_source:
            by_source[src] = {
                "error": (f"締め切り {deadline:g} 秒に間に合いませんでした（取得は続けており、"
                          "終われば同じ問いの次回はキャッシュから返ります）"),
                "timed_out": True, "items": [], "citations": []}
            timings[src] = None
    return {src: by_source[src] for src in picked}, timings


def knowledge_lookup(query: str, sources: list[str] | None = None, *, limit: int = 3,
                     lang: str = "ja", kind: str = "repo", max_workers: int | None = None,
                     deadline: float | None = None, datacite_kind: str = "all", fallback: bool = False) -> dict:
    """指定ソースを**並列に**引いて、出典つきでまとめる。LLM を使わないので幻覚が入らない。

    全体の締め切り（`KB_DEADLINE`）までに届いた分だけ返す（§5.8）。`max_workers` は互換のため
    受け取るが使わない（ソースごとに専用スレッド）。
    """
    query = as_str(query)
    if not query:
        return {"error": "query は必須です", "query": query}
    requested = as_str_list(sources)
    picked = list(dict.fromkeys(s for s in requested if s in KB_BACKENDS)) if requested else [s for s in DEFAULT_SOURCES if s in KB_BACKENDS]
    unknown = [s for s in requested if s not in KB_BACKENDS]
    if requested and not picked:
        message = "有効な sources がありません。利用可能: " + ", ".join(KB_BACKENDS)
        return {"query": query, "sources": [], "unknown_sources": unknown,
                "results": {}, "citations": [], "citation_count": 0,
                "errors": {"sources": message}, "error": message, "llm_used": False}
    limit = as_int(limit, 3, 1, 10)
    opts = {"lang": as_str(lang, "ja"), "kind": as_str(kind, "repo"),
            "datacite_kind": as_str(datacite_kind, "all"), "fallback": fallback is True}
    by_source, timings = _kb_gather(picked, query, limit, opts, deadline)
    citations: list[dict] = []
    errors = {}
    for src, res in by_source.items():
        if not isinstance(res, dict):
            continue
        if res.get("error"):
            errors[src] = res["error"]
        citations.extend(res.get("citations") or [])
    citations = _kb_merge_citations(citations)
    late = [src for src, res in by_source.items() if isinstance(res, dict) and res.get("timed_out")]
    out = {"query": query, "sources": picked, "unknown_sources": unknown,
           "results": by_source, "citations": citations, "citation_count": len(citations),
           "errors": errors, "llm_used": False, "timings": timings,
           "deadline_s": KB_DEADLINE if deadline is None else deadline}
    if late:
        out["timed_out"] = late
    return out


# ---------------------------------------------------------------- §5.9 DataCite（任意ソース・研究データ・arXiv代替）


def _literal_search(query: str) -> str:
    """自然語の各語を引用して AND 結合する。提供元の演算子として解釈させない。"""
    return " AND ".join(json.dumps(word, ensure_ascii=False) for word in query.split())


def _kb_new_cached(key: str, source: str, producer) -> dict:
    """新規ソースの破損した上流データも、直接呼び出し時に例外を漏らさない。"""
    def safe():
        try:
            result = producer()
            for cite in result.get("citations") or []:
                _kb_http_url(cite.get("url"))
            return result
        except Exception as exc:
            return {"source": source, "error": f"応答解析失敗: {type(exc).__name__}: {exc}"}
    return _kb_cached(key, safe)


def kb_datacite(query: str, limit: int = 5, kind: str = "all") -> dict:
    """公開 DOI メタデータ。all / arxiv / dataset。版・arXiv検索式の互換は保証しない。"""
    query = as_str(query)
    kind = as_str(kind, "all")
    limit = as_int(limit, 5, 1, 20)
    if not query or kind not in ("all", "arxiv", "dataset"):
        return {"source": "datacite", "error": "query と有効な datacite_kind（all / arxiv / dataset）が必要です"}

    def produce() -> dict:
        params = {"query": _literal_search(query), "sort": "relevance", "page[size]": limit}
        if kind == "arxiv":
            params["client-id"] = "arxiv.content"
        elif kind == "dataset":
            params["resource-type-id"] = "dataset"
        if KB_MAILTO:
            params["mailto"] = KB_MAILTO
        data, err = _kb_new_json("https://api.datacite.org/dois?" + urllib.parse.urlencode(params), 0.61)
        if err:
            return {"source": "datacite", "error": err}
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"source": "datacite", "error": "DataCite の応答形式が不正です"}
        items, cites = [], []
        for record in rows[:limit]:
            row = record.get("attributes") if isinstance(record, dict) else None
            if not isinstance(row, dict):
                continue
            title = next((as_str(t.get("title")) for t in (row.get("titles") or [])
                          if isinstance(t, dict) and as_str(t.get("title"))), "")
            doi = as_str(row.get("doi"))
            link = as_str(row.get("url")) or (f"https://doi.org/{doi}" if doi else "")
            if not title or not link:
                continue
            descriptions = [d for d in (row.get("descriptions") or []) if isinstance(d, dict)]
            abstract = next((as_str(d.get("description")) for d in descriptions
                             if d.get("descriptionType") == "Abstract" and as_str(d.get("description"))), "")
            summary = _plain_text(abstract, 600)
            authors = [as_str(a.get("name")) for a in (row.get("creators") or []) if isinstance(a, dict)]
            year = row.get("publicationYear")
            item = {"title": truncate(title, 300), "url": link, "doi": doi, "year": year,
                    "authors": [a for a in authors if a][:8], "summary": summary,
                    "type": (row.get("types") or {}).get("resourceTypeGeneral"),
                    "metadata_only": not bool(summary)}
            items.append(item)
            cite = _cite("datacite", title, link, year=year, doi=doi,
                         repository="arxiv" if kind == "arxiv" else "", metadata_only=not bool(summary))
            cite["summary"] = summary
            cite["year"] = year or ""
            cite["resource_type"] = item.get("type") or ""
            cites.append(cite)
        return {"source": "datacite", "datacite_kind": kind, "items": items, "citations": cites,
                "error": "" if items else "該当なし"}

    return _kb_new_cached(f"datacite:{kind}:{query}:{limit}", "datacite", produce)


DEFAULT_SOURCES = SOURCES
SOURCES = (*DEFAULT_SOURCES, "datacite")
KB_BACKENDS["datacite"] = lambda q, limit, opts: kb_datacite(q, limit, opts.get("datacite_kind", "all"))


# ---------------------------------------------------------------- §5.10 明示許可した arXiv の代替取得（同じ締め切り内）

KB_HEDGE_DELAY = max(0.05, min(30.0, as_float(os.environ.get("FREEAGENT_KB_HEDGE_DELAY"), 2.0)))


def _kb_source_result(src: str, query: str, limit: int, opts: dict) -> dict:
    """主系と代替は別ホスト。遅延時も主系をキャンセルせず、そのキャッシュを温める。"""
    primary_fn = KB_BACKENDS[src]
    allowed = (src == "arxiv" and opts.get("fallback") is True and "datacite" in KB_BACKENDS
               and not re.search(r'[:"()\[\]]|\b(?:AND|OR|NOT)\b|\b\d{4}\.\d{4,5}(?:v\d+)?\b'
                                 r'|\b[A-Za-z][A-Za-z.-]*/\d{7}(?:v\d+)?\b', query))
    if not allowed:
        return primary_fn(query, limit, opts)
    done = {}
    lock = threading.Lock()
    changed = threading.Event()

    def invoke(key, fn, options):
        try:
            if key == "alternate" and time.monotonic() >= options.get("deadline_at", float("inf")):
                res = {"error": "締め切り後の代替取得は開始しません", "timed_out": True}
            else:
                res = fn(query, limit, options)
            if not isinstance(res, dict):
                res = {"error": "不正な結果"}
        except Exception as exc:
            res = {"error": f"{type(exc).__name__}: {exc}"}
        with lock:
            done[key] = res
            changed.set()

    def retryable(res):
        err = as_str(res.get("error"))
        return bool(err and (is_env_failure(err) or err.startswith("blocked:")
                    or re.search(r"HTTP (?:429|5\d\d)\b", err)))

    threading.Thread(target=invoke, args=("primary", primary_fn, opts), daemon=True).start()
    end = opts.get("deadline_at", time.monotonic() + KB_DEADLINE)
    changed.wait(min(KB_HEDGE_DELAY, max(0.0, end - time.monotonic())))
    with lock:
        primary = done.get("primary")
    if primary is not None and not retryable(primary):
        return primary
    if time.monotonic() >= end:
        return primary if primary is not None else {"error": "締め切りに間に合いませんでした",
                                                   "timed_out": True, "items": [], "citations": []}
    alternate_opts = {**opts, "datacite_kind": "arxiv", "fallback": False}
    threading.Thread(target=invoke, args=("alternate", KB_BACKENDS["datacite"], alternate_opts), daemon=True).start()
    while True:
        with lock:
            primary = done.get("primary")
            alternate = done.get("alternate")
            changed.clear()
        if primary is not None and not primary.get("error"):
            return primary
        if alternate is not None and not alternate.get("error") and alternate.get("items"):
            return {**alternate, "fallback": {"requested_source": src, "served_by": "datacite",
                    "primary_error": (primary or {}).get("error") or "主系が応答待ち（遅延時の代替）"}}
        if primary is not None and alternate is not None:
            return {**primary, "fallback_attempt": {"source": "datacite", "error": alternate.get("error") or "該当なし"}}
        changed.wait()


# ---------------------------------------------------------------- §5.11 新規ソースのホスト別予算（メモリのみ・待たずに返す）

_KB_RATE_NEXT: dict[str, float] = {}
_KB_RATE_LOCK = threading.Lock()


def _kb_rate_acquire(host: str, interval: float) -> float:
    """HTTP実行の枠を原子的に取る。足りなければ待機秒を返し、枠を予約しない。"""
    with _KB_RATE_LOCK:
        now = time.monotonic()
        wait = _KB_RATE_NEXT.get(host, 0.0) - now
        if wait > 0:
            return wait
        _KB_RATE_NEXT[host] = now + interval
        return 0.0


def _kb_new_json(url: str, interval: float) -> tuple[dict | None, str]:
    host = urllib.parse.urlsplit(url).netloc
    if _kb_is_blocked(host):
        return None, f"blocked: {host} は一時停止中です"
    wait = _kb_rate_acquire(host, interval)
    if wait:
        return None, f"HTTP 429: ローカルのアクセス間隔制御（あと {wait:.1f} 秒）。同一ホストの枠を共有します"
    return kb_json(url)


# ---------------------------------------------------------------- §5.12 Europe PMC / OpenAIRE Graph V3（明示指定のみ）


def _kb_array(value, field: str) -> list:
    """上流配列を辞書キーや文字列として走査しない（欠落/Noneだけは空配列）。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} は配列ではありません")
    return value


def _kb_http_url(value: str) -> str:
    if not isinstance(value, str) or re.search(r"\s", value):
        raise ValueError("出典URLはHTTP(S)の文字列が必要です")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("出典URLの形式が不正です")
    return value


def _kb_paper_result(source: str, items: list[dict], **extra) -> dict:
    cites = []
    for item in items:
        summary = item.get("summary") or ""
        cite = _cite(source, item["title"], item["url"], year=item.get("year") or "",
                     doi=item.get("doi") or "", metadata_only=not bool(summary))
        cite["summary"] = summary
        cite["year"] = item.get("year") or ""
        for field in ("publication_types", "repository", "license", "licenses"):
            if item.get(field):
                cite[field] = item[field]
        if extra.get("attribution"):
            cite["attribution"] = extra["attribution"]
        cites.append(cite)
    return {"source": source, "items": items, "citations": cites,
            "error": "" if items else "該当なし", **extra}


def kb_europepmc(query: str, limit: int = 5) -> dict:
    """生命科学系の抄録。全文は取得しない。プレプリント・原文ライセンスを保持する。"""
    query = as_str(query)
    limit = as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "europepmc", "error": "query は必須です"}
    def produce():
        params = {"query": _literal_search(query), "format": "json", "resultType": "core", "pageSize": limit}
        data, err = _kb_new_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search?"
                                + urllib.parse.urlencode(params), 1.0)
        if err:
            return {"source": "europepmc", "error": err}
        rows = (data.get("resultList") or {}).get("result") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"source": "europepmc", "error": "Europe PMC の応答形式が不正です"}
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not as_str(row.get("title")):
                continue
            record_id, origin = as_str(row.get("id")), as_str(row.get("source"))
            if not record_id or not origin:
                continue
            summary = _plain_text(as_str(row.get("abstractText")), 600)
            authors = [as_str(a.get("fullName")) for a in ((row.get("authorList") or {}).get("author") or [])
                       if isinstance(a, dict)]
            items.append({"title": truncate(row["title"], 300), "summary": summary,
                          "url": "https://europepmc.org/article/" + urllib.parse.quote(origin, safe="")
                                 + "/" + urllib.parse.quote(record_id, safe=""),
                          "doi": as_str(row.get("doi")), "year": as_str(row.get("pubYear")),
                          "id": record_id, "repository": origin, "authors": [a for a in authors if a][:8],
                          "publication_types": (row.get("pubTypeList") or {}).get("pubType") or [],
                          "is_oa": row.get("isOpenAccess") == "Y", "license": as_str(row.get("license")),
                          "cited_by": row.get("citedByCount"), "metadata_only": not bool(summary)})
        return _kb_paper_result("europepmc", items)
    return _kb_new_cached(f"europepmc:{query}:{limit}", "europepmc", produce)


def kb_openaire(query: str, limit: int = 5) -> dict:
    """Graph V3の論文メタデータ。匿名枠60/hをプロセス内60.1秒間隔で守る。"""
    query = as_str(query)
    limit = as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "openaire", "error": "query は必須です"}
    def produce():
        params = {"search": query, "type": "publication", "pageSize": limit}
        data, err = _kb_new_json("https://api.openaire.eu/graph/v3/research-products?"
                                + urllib.parse.urlencode(params), 60.1)
        if err:
            return {"source": "openaire", "error": err}
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return {"source": "openaire", "error": "OpenAIRE の応答形式が不正です"}
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict) or not as_str(row.get("mainTitle")):
                continue
            doi = next((as_str(p.get("value")) for p in (row.get("pids") or [])
                        if isinstance(p, dict) and p.get("scheme") == "doi"), "")
            instances = _kb_array(row.get("instances"), "instances")
            if any(not isinstance(i, dict) for i in instances):
                raise ValueError("instances の要素がオブジェクトではありません")
            urls = [_kb_http_url(u) for i in instances
                    for u in _kb_array(i.get("urls"), "instances.urls")]
            record_id = as_str(row.get("id"))
            link = f"https://doi.org/{doi}" if doi else (urls[0] if urls else
                   "https://api.openaire.eu/graph/v3/research-products/" + urllib.parse.quote(record_id, safe=""))
            if not doi and not urls and not record_id:
                continue
            descriptions = _kb_array(row.get("descriptions"), "descriptions")
            if any(not isinstance(d, str) for d in descriptions):
                raise ValueError("descriptions の要素が文字列ではありません")
            summary = _plain_text(" ".join(d for d in descriptions if isinstance(d, str)), 600)
            authors = [as_str(a.get("fullName")) for a in (row.get("authors") or []) if isinstance(a, dict)]
            items.append({"title": truncate(row["mainTitle"], 300), "summary": summary, "url": link,
                          "doi": doi, "id": record_id, "year": as_str(row.get("publicationDate"))[:4],
                          "authors": [a for a in authors if a][:8],
                          "licenses": list(dict.fromkeys(as_str(i.get("license")) for i in instances if as_str(i.get("license")))),
                          "cited_by": (((row.get("indicators") or {}).get("citationImpact") or {}).get("citationCount")),
                          "metadata_only": not bool(summary)})
        return _kb_paper_result("openaire", items,
                               attribution="データ提供: OpenAIRE（CC-BY） https://graph.openaire.eu/")
    return _kb_new_cached(f"openaire:{query}:{limit}", "openaire", produce)


SOURCES = (*SOURCES, "openaire", "europepmc")
KB_BACKENDS.update({"openaire": lambda q, limit, opts: kb_openaire(q, limit),
                    "europepmc": lambda q, limit, opts: kb_europepmc(q, limit)})


# ---------------------------------------------------------------- §5.13 同一識別子の引用統合（独立した裏付けには数えない）


def _kb_has_evidence(cite: dict) -> bool:
    return isinstance(cite, dict) and not cite.get("metadata_only") and bool(as_str(cite.get("summary")))


def _kb_citation_aliases(cite: dict) -> tuple[set[str], set[str]]:
    stored = cite.get("aliases") if isinstance(cite.get("aliases"), dict) else {}
    raw_dois = [cite.get("doi")] + (stored.get("dois") if isinstance(stored.get("dois"), list) else [])
    raw_urls = [cite.get("url")] + (stored.get("urls") if isinstance(stored.get("urls"), list) else [])
    dois = {re.sub(r"^https?://(?:dx\.)?doi\.org/", "", as_str(d).lower()) for d in raw_dois if as_str(d)}
    urls = {as_str(u) for u in raw_urls if as_str(u)}
    return dois, urls


def _kb_citation_key(cite: dict) -> tuple:
    """agentも明示DOIの異なる版をURL一致だけで潰さない。"""
    dois, _ = _kb_citation_aliases(cite)
    source = as_str(cite.get("source"))
    return (source, "doi", tuple(sorted(dois))) if dois else (source, "url", as_str(cite.get("url")))


def _kb_merge_citations(citations: list[dict]) -> list[dict]:
    """全入力のDOI/URL別名を先に索引化し、曖昧なURL-only引用を版に割り当てない。"""
    prepared = [(n, copy.deepcopy(c), *_kb_citation_aliases(c))
                for n, c in enumerate(citations) if isinstance(c, dict)]
    # 別名の連鎖も含め、到達するDOIを成分全体から求める（直接対応だけでは順序依存になる）。
    parent = {}
    def find(node):
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node
    for _, _, dois, urls in prepared:
        nodes = [("doi", d) for d in dois] + [("url", u) for u in urls]
        if nodes:
            root = find(nodes[0])
            for node in nodes[1:]:
                parent[find(node)] = root
    component_dois = {}
    for _, _, dois, _ in prepared:
        for doi in dois:
            component_dois.setdefault(find(("doi", doi)), set()).add(doi)
    url_dois = {u: component_dois.get(find(("url", u)), set())
                for _, _, _, urls in prepared for u in urls}
    groups = []
    for number, cite, dois, urls in prepared:
        claims = set().union(*(url_dois.get(u, set()) for u in urls))
        join_urls = {u for u in urls if len(url_dois.get(u, set())) <= 1}
        if not dois and len(claims) > 1:
            join_urls = set()
        matches = [g for g in groups if (dois and dois & g["dois"]) or
                   (not dois and not g["dois"] and urls & g["urls"]) or
                   (join_urls & g["join_urls"] and (not dois or not g["dois"] or g["dois"] == dois))]
        if len(dois | set().union(*(g["dois"] for g in matches))) > 1:
            matches = []
        group = {"dois": set(dois), "urls": set(urls), "join_urls": set(join_urls), "rows": [(number, cite)]}
        for match in matches:
            group["dois"].update(match["dois"])
            group["urls"].update(match["urls"])
            group["join_urls"].update(match["join_urls"])
            group["rows"].extend(match["rows"])
            groups.remove(match)
        groups.append(group)
    merged = []
    for group in sorted(groups, key=lambda g: min(n for n, _ in g["rows"])):
        rows = [r for _, r in sorted(group["rows"], key=lambda p: p[0])]
        selected = max(rows, key=lambda r: len(as_str(r.get("summary"))) if not r.get("metadata_only") else 0)
        cite = copy.deepcopy(selected)
        cite["aliases"] = {"dois": sorted(group["dois"]), "urls": sorted(group["urls"])}
        if not as_str(cite.get("doi")) and len(group["dois"]) == 1:
            cite["doi"] = next((r["doi"] for r in rows if as_str(r.get("doi"))), next(iter(group["dois"])))
        cite["metadata_only"] = not bool(as_str(cite.get("summary"))) or bool(cite.get("metadata_only"))
        if len(rows) > 1:
            providers = [p for r in rows for p in (r.get("providers") or [r.get("source")]) if p]
            cite["providers"] = list(dict.fromkeys(providers))
            cite["summary_source"] = cite.get("source") if not cite["metadata_only"] else ""
            metadata = []
            for row in rows:
                if isinstance(row.get("provider_metadata"), list):
                    metadata.extend(copy.deepcopy(row["provider_metadata"]))
                else:
                    metadata.append({k: copy.deepcopy(row[k]) for k in
                        ("source", "url", "doi", "license", "licenses", "publication_types", "resource_type", "repository", "attribution",
                         "file_license", "access_right", "summary_kind")
                        if k in row})
            cite["provider_metadata"] = metadata
            credits = [c for r in rows for c in ((r.get("attributions") or []) +
                       ([r["attribution"]] if r.get("attribution") else []))]
            if credits:
                cite["attributions"] = list(dict.fromkeys(credits))
        merged.append(cite)
    return merged


# ---------------------------------------------------------------- §5.14 Zenodo（公開メタデータのみ・ファイルを取得しない）


def _kb_object(value, field: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} はオブジェクトではありません")
    return value


def _open_metadata_text(value, limit: int = 600) -> str:
    """非本文HTMLとメールを除外する。除外だけの値は空で返す。"""
    from html.parser import HTMLParser
    class Reader(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts, self.hidden = [], []
        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "template", "noscript", "head"):
                self.hidden.append(tag)
            elif not self.hidden:
                self.parts.append(" ")
        def handle_endtag(self, tag):
            if tag in self.hidden:
                self.hidden = self.hidden[:self.hidden.index(tag)]
            elif not self.hidden:
                self.parts.append(" ")
        def handle_data(self, data):
            if not self.hidden:
                self.parts.append(data)
    reader = Reader()
    reader.feed(as_str(value))
    reader.close()
    text = " ".join("".join(reader.parts).split())
    text = re.sub(r'(?:"[^"]*"|[^ @<>]+) *@ *[^ <>]+', " ", text)
    if "@" in text:
        return ""
    return truncate(" ".join(text.split()), limit)


def kb_zenodo(query: str, limit: int = 3) -> dict:
    """Zenodoの説明/注記はメタデータ。ファイルライセンスとは分離し全文・ファイルは取得しない。"""
    query = as_str(query)
    limit = as_int(limit, 3, 1, 10)
    if not query:
        return {"source": "zenodo", "error": "query は必須です"}
    def produce():
        params = {"q": _literal_search(query), "size": limit, "sort": "bestmatch"}
        data, err = _kb_new_json("https://zenodo.org/api/records/?" + urllib.parse.urlencode(params), 2.01)
        if err:
            return {"source": "zenodo", "error": err}
        hits = _kb_object(data, "Zenodo応答").get("hits")
        rows = _kb_object(hits, "hits").get("hits")
        if not isinstance(rows, list):
            raise ValueError("Zenodo hits.hits は配列ではありません")
        items, cites = [], []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("Zenodo record はオブジェクトではありません")
            meta = _kb_object(row.get("metadata"), "metadata")
            title = _open_metadata_text(meta.get("title"), 300)
            if not title:
                continue
            doi = as_str(row.get("doi")) or as_str(meta.get("doi"))
            links = _kb_object(row.get("links"), "links")
            link = as_str(links.get("self_html")) or ("https://doi.org/" + doi if doi else "")
            if not link:
                continue
            description = " ".join(as_str(meta.get(k)) for k in ("description", "notes"))
            summary = _open_metadata_text(description)
            year = as_str(meta.get("publication_date"))[:4]
            authors = [_open_metadata_text(a.get("name"), 100) for a in _kb_array(meta.get("creators"), "creators")
                       if isinstance(a, dict)]
            resource_type = as_str(_kb_object(meta.get("resource_type"), "resource_type").get("type"))
            file_license = as_str(_kb_object(meta.get("license"), "license").get("id"))
            item = {"title": title, "url": link, "doi": doi, "year": year, "summary": summary,
                    "authors": [a for a in authors if a][:8], "resource_type": resource_type,
                    "access_right": as_str(meta.get("access_right")), "file_license": file_license,
                    "license": "CC0-1.0", "summary_kind": "metadata_description", "metadata_only": not bool(summary)}
            cite = _cite("zenodo", title, link, doi=doi, attribution="データ提供: Zenodo（メタデータCC0・ファイル条件は別） https://zenodo.org/")
            cite.update({k: v for k, v in item.items() if k != "authors"})
            items.append(item)
            cites.append(cite)
        return {"source": "zenodo", "items": items, "citations": cites,
                "attribution": "データ提供: Zenodo（メタデータCC0・ファイル条件は別） https://zenodo.org/",
                "error": "" if items else "該当なし"}
    return _kb_new_cached(f"zenodo:{query}:{limit}", "zenodo", produce)


# ---------------------------------------------------------------- §5.15 ROR v2（研究機関の構造化メタデータ・機関同定を確定しない）


def _ror_id_valid(value: str) -> bool:
    """Crockford Base32 + ROR公式のMOD 97-10。誤ったIDは修復しない。"""
    match = re.fullmatch(r"https://ror[.]org/0([0-9a-hjkmnp-tv-z]{6})([0-9]{2})", value)
    if not match:
        return False
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    number = 0
    for char in match.group(1):
        number = number * 32 + alphabet.index(char)
    return int(match.group(2)) == 98 - ((number * 100) % 97)


def kb_ror(query: str, limit: int = 3) -> dict:
    """論文検索ではなく機関候補検索。登録された属性だけを根拠文にする。"""
    query = as_str(query)
    limit = as_int(limit, 3, 1, 10)
    if not query:
        return {"source": "ror", "error": "query は必須です"}
    def produce():
        data, err = _kb_new_json("https://api.ror.org/v2/organizations?" + urllib.parse.urlencode({"query": query}), 6.1)
        if err:
            return {"source": "ror", "error": err}
        rows = _kb_object(data, "ROR応答").get("items")
        if not isinstance(rows, list):
            raise ValueError("ROR items は配列ではありません")
        items, cites = [], []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("ROR record はオブジェクトではありません")
            names = _kb_array(row.get("names"), "names")
            if any(not isinstance(n, dict) for n in names):
                raise ValueError("ROR names の要素はオブジェクトではありません")
            title = next((_open_metadata_text(n.get("value"), 300) for n in names
                          if "ror_display" in _kb_array(n.get("types"), "names.types")), "")
            link = as_str(row.get("id"))
            if not title or not _ror_id_valid(link):
                raise ValueError("ROR の表示名またはIDの形式が不正です")
            types = [as_str(v) for v in _kb_array(row.get("types"), "types") if as_str(v)]
            locations = [_kb_object(l, "location") for l in _kb_array(row.get("locations"), "locations")]
            countries, cities = [], []
            for location in locations:
                geo = _kb_object(location.get("geonames_details"), "geonames_details")
                countries.append(as_str(geo.get("country_name")))
                cities.append(as_str(geo.get("name")))
            countries = list(dict.fromkeys(c for c in countries if c))
            cities = list(dict.fromkeys(c for c in cities if c))
            established = row.get("established")
            if type(established) is not int or not 1 <= established <= 9999:
                established = None
            status = as_str(row.get("status"))
            facts = []
            for label, value in (("機関種別", ", ".join(types)), ("国", ", ".join(countries)),
                                 ("都市", ", ".join(cities)), ("状態", status)):
                if value:
                    facts.append(label + ": " + value)
            if established is not None:
                facts.append("設立年（出版年ではない）: " + str(established))
            summary = _open_metadata_text("; ".join(facts))
            sites = [as_str(_kb_object(v, "link").get("value")) for v in _kb_array(row.get("links"), "links")
                     if _kb_object(v, "link").get("type") == "website"]
            for site in sites:
                _kb_http_url(site)
            item = {"title": title, "url": link, "year": "", "summary": summary,
                    "name_variants": [_open_metadata_text(n.get("value"), 300) for n in names],
                    "organization_types": types, "countries": countries, "cities": cities,
                    "established": established, "status": status, "websites": sites,
                    "license": "CC0-1.0", "summary_kind": "structured_metadata", "metadata_only": not bool(summary)}
            cite = _cite("ror", title, link, attribution="データ提供: ROR（CC0・研究機関候補） https://ror.org/")
            cite.update({k: v for k, v in item.items() if k not in ("name_variants", "websites")})
            items.append(item)
            cites.append(cite)
        return {"source": "ror", "items": items, "citations": cites, "search_candidates": True,
                "attribution": "データ提供: ROR（CC0・研究機関候補） https://ror.org/",
                "error": "" if items else "該当なし"}
    return _kb_new_cached(f"ror:{query}:{limit}", "ror", produce)


SOURCES = (*SOURCES, "zenodo", "ror")
KB_BACKENDS.update({"zenodo": lambda q, limit, opts: kb_zenodo(q, limit),
                    "ror": lambda q, limit, opts: kb_ror(q, limit)})


# ---------------------------------------------------------------- §5.16 第4段階: DOAJ / npm / crates.io（明示指定のみ）
#
# 採用根拠（2026-10-02 の利用条件確認と実プローブ）:
#   * DOAJ: 記事メタデータは CC0 明記（doaj.org/terms/）。公式レート制限は全ルート 2 req/s。
#     匿名・キー不要で記事のキーワード+抄録検索ができる（OpenAlex 匿名検索の 503/429 時の科学系の受け皿）。
#   * npm registry: /-/v1/search は公式公開 API。Open Source Terms が「Public APIs による複製」を明示許可。
#     レート数値の公表は無いが責任ある利用が前提 → プロセス内 1 req/s に自制。
#   * crates.io: Crawler Policy が「最大 1 req/s + 識別可能な User-Agent」を要求。KB_USER_AGENT は連絡先入り。
# いずれも論文全文・パッケージ本体は取得しない（検索メタデータのみ）。PyPI / deps.dev はキーワード検索 API が
# 無い（名前直引きのみ）ため登録しない。PubMed / bioRxiv は Europe PMC が索引済みで重複、PLOS は 10 req/min
# で 8 秒締切と不整合、HAL は商用利用条項が曖昧なため保留（規約 29 の保留群と同じ扱い）。


def kb_doaj(query: str, limit: int = 5) -> dict:
    """DOAJ のオープンアクセス記事メタデータ（CC0）。抄録を本文根拠に使う。全文は取得しない。"""
    query = as_str(query)
    limit = as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "doaj", "error": "query は必須です"}
    def produce():
        path = urllib.parse.quote(_literal_search(query), safe="")
        data, err = _kb_new_json("https://doaj.org/api/search/articles/" + path
                                + "?" + urllib.parse.urlencode({"pageSize": limit}), 0.51)
        if err:
            return {"source": "doaj", "error": err}
        rows = _kb_array(_kb_object(data, "DOAJ応答").get("results"), "results")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("DOAJ record はオブジェクトではありません")
            bib = _kb_object(row.get("bibjson"), "bibjson")
            title = _plain_text(as_str(bib.get("title")), 300)
            if not title:
                continue
            doi = next((as_str(i.get("id")) for i in _kb_array(bib.get("identifier"), "identifier")
                        if isinstance(i, dict) and as_str(i.get("type")).lower() == "doi"), "")
            link = next((as_str(l.get("url")) for l in _kb_array(bib.get("link"), "link")
                         if isinstance(l, dict) and as_str(l.get("url"))), "")
            if doi:
                link = "https://doi.org/" + doi
            if not link:
                continue
            journal = _kb_object(bib.get("journal"), "journal")
            authors = [as_str(a.get("name")) for a in _kb_array(bib.get("author"), "author")
                       if isinstance(a, dict)]
            summary = _plain_text(as_str(bib.get("abstract")), 600)
            items.append({"title": title, "url": link, "doi": doi, "year": as_str(bib.get("year")),
                          "summary": summary, "authors": [a for a in authors if a][:8],
                          "journal": _plain_text(as_str(journal.get("title")), 200),
                          "metadata_only": not bool(summary)})
        return _kb_paper_result("doaj", items,
                               attribution="データ提供: DOAJ（記事メタデータCC0） https://doaj.org/")
    return _kb_new_cached(f"doaj:{query}:{limit}", "doaj", produce)


def _kb_package_result(source: str, items: list[dict], attribution: str) -> dict:
    """パッケージ検索の正規化。説明文はレジストリ登録者の自己申告で、品質・安全性の審査結果ではない。"""
    cites = []
    for item in items:
        summary = item.get("summary") or ""
        cite = _cite(source, item["title"], item["url"], year=item.get("year") or "",
                     metadata_only=not bool(summary), attribution=attribution,
                     summary_kind="registry_description")
        cite["summary"] = summary
        for field in ("version", "downloads", "repository_url", "keywords"):
            if item.get(field):
                cite[field] = item[field]
        cites.append(cite)
    return {"source": source, "items": items, "citations": cites, "attribution": attribution,
            "error": "" if items else "該当なし"}


def kb_npm(query: str, limit: int = 5) -> dict:
    """npm 公式レジストリのパッケージ検索。説明は登録者の自己申告（審査結果ではない）。"""
    query = as_str(query)
    limit = as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "npm", "error": "query は必須です"}
    def produce():
        data, err = _kb_new_json("https://registry.npmjs.org/-/v1/search?"
                                + urllib.parse.urlencode({"text": query, "size": limit}), 1.0)
        if err:
            return {"source": "npm", "error": err}
        rows = _kb_array(_kb_object(data, "npm応答").get("objects"), "objects")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("npm object はオブジェクトではありません")
            pkg = _kb_object(row.get("package"), "package")
            name = as_str(pkg.get("name"))
            if not name:
                continue
            links = _kb_object(pkg.get("links"), "links")
            link = as_str(links.get("npm")) or "https://www.npmjs.com/package/" + urllib.parse.quote(name, safe="")
            summary = _plain_text(as_str(pkg.get("description")), 400)
            keywords = [as_str(k) for k in _kb_array(pkg.get("keywords"), "keywords") if as_str(k)]
            items.append({"title": name, "url": link, "version": as_str(pkg.get("version")),
                          "year": as_str(pkg.get("date"))[:4], "summary": summary,
                          "repository_url": as_str(links.get("repository")),
                          "keywords": keywords[:8], "metadata_only": not bool(summary)})
        return _kb_package_result("npm", items,
                                  "データ提供: npm public registry https://registry.npmjs.org/")
    return _kb_new_cached(f"npm:{query}:{limit}", "npm", produce)


def kb_crates(query: str, limit: int = 5) -> dict:
    """crates.io のクレート検索。Crawler Policy（1 req/s・識別UA）に従う。"""
    query = as_str(query)
    limit = as_int(limit, 5, 1, 20)
    if not query:
        return {"source": "crates", "error": "query は必須です"}
    def produce():
        data, err = _kb_new_json("https://crates.io/api/v1/crates?"
                                + urllib.parse.urlencode({"q": query, "per_page": limit}), 1.01)
        if err:
            return {"source": "crates", "error": err}
        rows = _kb_array(_kb_object(data, "crates.io応答").get("crates"), "crates")
        items = []
        for row in rows[:limit]:
            if not isinstance(row, dict):
                raise ValueError("crates.io record はオブジェクトではありません")
            name = as_str(row.get("name"))
            if not name:
                continue
            summary = _plain_text(as_str(row.get("description")), 400)
            items.append({"title": name, "url": "https://crates.io/crates/" + urllib.parse.quote(name, safe=""),
                          "version": as_str(row.get("max_stable_version")) or as_str(row.get("max_version")),
                          "year": as_str(row.get("updated_at"))[:4], "summary": summary,
                          "repository_url": as_str(row.get("repository")),
                          "downloads": row.get("downloads") if type(row.get("downloads")) is int else None,
                          "metadata_only": not bool(summary)})
        return _kb_package_result("crates", items, "データ提供: crates.io https://crates.io/")
    return _kb_new_cached(f"crates:{query}:{limit}", "crates", produce)


SOURCES = (*SOURCES, "doaj", "npm", "crates")
KB_BACKENDS.update({"doaj": lambda q, limit, opts: kb_doaj(q, limit),
                    "npm": lambda q, limit, opts: kb_npm(q, limit),
                    "crates": lambda q, limit, opts: kb_crates(q, limit)})


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
    '  {"answer": "<回答。使った根拠の番号 [n] を本文に書き、根拠が足りない点は「根拠に無い」と明記>"}\n'
    "根拠を 1 つも使っていない場合は [0] を書いてください。JSON 以外の文字を書かない。"
)
AGENT_SYSTEM = (
    "あなたは調査補佐です。次のいずれか**1つだけ**を JSON で出力してください。\n"
    '  ツールを使う: {"tool": "lookup", "query": "<検索語>", "sources": ["arxiv","crossref",...]}\n'
    '  使える source: wikipedia, wikidata, arxiv, crossref, openalex, github, datacite, openaire, europepmc, zenodo, ror\n'
    '  追加sourceは明示指定のみ。datacite_kind: all / arxiv / dataset。fallback: trueでarXivのDataCite代替を許可。\n'
    '  回答する:     {"answer": "<回答。使った根拠の番号 [n] を本文に書く>"}\n'
    "ツール結果は [1] [2] … の番号つきで返ります。回答では使った根拠の番号を本文に書き、"
    "根拠に無い事実は書かない（書けない点は「根拠に無い」と明記する）。"
    "JSON 以外の文字（前置き・コードフェンス）を書かない。根拠が足りなければツールを使う。"
)
# 思考ステップの検証者（`freeagent_think(verify=true)`）。**同意を集めない**のが役割で、
# 「最強の反論」と「見落とし」を要求する（DEBATE_SYSTEM と同じ思想: 賛成は情報を増やさない）。
# 生成者（メイン）と別モデルに当てるので独立性は構造的に担保される。
THINK_CRITIC_SYSTEM = (
    "あなたは思考ステップの検証者です。同意を探すのではなく**反証**を探してください。"
    "出力は必ず次の4行だけです。\n"
    "判定: <妥当 / 要修正 / 根拠不足 のいずれか1つ>\n"
    "反証: <この思考への最も強い反証を1〜2文。無ければ なし>\n"
    "見落とし: <検討されていない観点を1つ。無ければ なし>\n"
    "確信度: <0〜100 の整数>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)

_LABEL_CONCLUSION = re.compile(
    r"(?:結論|まとめ|conclusion)\s*[:：]\s*(.+?)(?=\s+(?:確信度|自信|confidence|メインに確認したい点|確認したい点|質問|questions?)\s*[:：]|$)", re.I)
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
            out["conclusion"] = truncate(m.group(1).strip().strip("* "), 600)
            out["labels_found"] += 1
        m = _LABEL_CONFIDENCE.search(line)
        if m and out["confidence"] is None:
            raw = m.group(1)
            val = as_float(raw, -1.0)
            if "." in raw and val <= 1.0:
                val *= 100.0
            if 0 <= val <= 100:
                out["confidence"] = int(val)
                out["labels_found"] += 1
        m = _LABEL_QUESTION.search(line)
        if m and not out["question"]:
            value = m.group(1).strip().strip("* ")
            empty_value = value.lower().rstrip(" .。!?！？;；").strip()
            out["question"] = "" if empty_value in _NO_ANSWER else truncate(value, 400)
            out["labels_found"] += 1
    return out


# 検証者（THINK_CRITIC_SYSTEM）の出力の解析。**1 行 1 ラベルと決め打たない**（同一行に複数ラベルが
# 来る回答がある。規約 11）。取れない項目は空のまま返し、推測で埋めない。
_LABEL_VERDICT = re.compile(
    r"(?:判定|verdict|assessment)\s*[:：]\s*(妥当|要修正|根拠不足|ok|needs?[ _-]?revision|insufficient)",
    re.I)
_LABEL_OBJECTION = re.compile(r"(?:反証|反論|counter(?:argument)?)\s*[:：]\s*(.+)", re.I)
_LABEL_OVERSIGHT = re.compile(r"(?:見落とし|抜け|oversight|missing)\s*[:：]\s*(.+)", re.I)


def _is_no_answer(value: str) -> bool:
    """「なし」系の空ラベルかどうか（`なし` を反証として数えないための判定）。"""
    return (value or "").lower().rstrip(" .。!?！？;；").strip() in _NO_ANSWER


def parse_verdict(text: str) -> dict:
    """検証者の「判定/反証/見落とし/確信度」を構造化する。取れない項目は空（推測しない）。"""
    out = {"verdict": "", "objection": "", "oversight": "", "confidence": None, "labels_found": 0}
    for line in (text if isinstance(text, str) else "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_VERDICT.search(line)
        if m and not out["verdict"]:
            raw = m.group(1).lower()
            if raw in ("妥当", "ok"):
                out["verdict"] = "妥当"
            elif raw.startswith("needs") or raw == "要修正":
                out["verdict"] = "要修正"
            else:
                out["verdict"] = "根拠不足"
            out["labels_found"] += 1
        m = _LABEL_OBJECTION.search(line)
        if m and not out["objection"]:
            value = m.group(1).strip().strip("* ")
            out["objection"] = "" if _is_no_answer(value) else truncate(value, 400)
            out["labels_found"] += 1
        m = _LABEL_OVERSIGHT.search(line)
        if m and not out["oversight"]:
            value = m.group(1).strip().strip("* ")
            out["oversight"] = "" if _is_no_answer(value) else truncate(value, 400)
            out["labels_found"] += 1
        m = _LABEL_CONFIDENCE.search(line)
        if m and out["confidence"] is None:
            raw = m.group(1)
            val = as_float(raw, -1.0)
            if "." in raw and val <= 1.0:
                val *= 100.0
            if 0 <= val <= 100:
                out["confidence"] = int(val)
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
    harness = harness_status()   # §8.6（initialize 前は None）
    if harness:
        data["harness"] = harness

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
                elif "http 403" in err_l:
                    # 403 は**署名があるときだけ**権限なし扱い。Cloudflare のブロック（Error 1010 等）や
                    # 提供元の障害・モデル単位の制限は、プロバイダ全体の問題ではないので `error`（残す）。
                    # 401 より先に見る（"HTTP 401/403" のような表記が 401 に誤マッチしていた実測がある）。
                    verdict = "auth" if _is_auth_error(403, err) else "error"
                elif "http 401" in err_l:
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
                       max_tokens=as_int(args.get("max_tokens"), 500, 16, 4000), kind="panel",
                       avoid=as_str_list(args.get("exclude")) or None)
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
    independent = independent_answers(good)
    conf = [a["confidence"] for a in independent if isinstance(a.get("confidence"), int)]
    data = {
        "question": question, "models": refs, "selection": info,
        "answered": len(good), "independent_sources": len(independent), "failed": len(answers) - len(good),
        "agreement": agreement_of([a["conclusion"] for a in independent]),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "consensus": _consensus_groups(independent),
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
                            kind=as_str(args.get("github_kind"), "repo"),
                            datacite_kind=as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True)


# 根拠としてサブLLMへ注入する本文の量。**入れないと幻覚は減らない**（実測: 以前はタイトルと URL
# だけで、サブLLMは根拠を読めないまま自分の記憶で答え [n] を飾りで付けていた）。一方で入れすぎると
# 小型 Free モデルは予算を使い切って空応答・切断になるため、1 件と全体の両方に上限を設ける。
_EVIDENCE_ITEM_CHARS = max(0, min(2000, _env_int("FREEAGENT_EVIDENCE_ITEM_CHARS", 360)))
_EVIDENCE_TOTAL_CHARS = max(0, min(20000, _env_int("FREEAGENT_EVIDENCE_TOTAL_CHARS", 3200)))


# ---------------------------------------------------------------- §6.11 本文を実際に注入した引用番号だけを認定する


def _evidence_window(citations: list[dict], *, item_chars: int | None = None,
                     total_chars: int | None = None, numbers: list[int] | None = None) -> dict:
    """本文予算に入った番号を返す。本文を渡せない項目は番号付き見出しも作らない。"""
    per_item = as_int(_EVIDENCE_ITEM_CHARS if item_chars is None else item_chars, _EVIDENCE_ITEM_CHARS, 0, 2000)
    budget = as_int(_EVIDENCE_TOTAL_CHARS if total_chars is None else total_chars, _EVIDENCE_TOTAL_CHARS, 0, 20000)
    labels = numbers if numbers and len(numbers) == len(citations) else list(range(1, len(citations) + 1))
    lines, injected = [], []
    for i, c in zip(labels, citations):
        if not _kb_has_evidence(c):
            continue
        body = " ".join(as_str(c.get("summary")).split())
        take = min(per_item, budget, len(body))
        if take <= 0:
            continue
        bits = [f"[{i}] {c.get('title') or '(無題)'}"]
        if c.get("year"):
            bits.append(f"({c['year']})")
        if c.get("publication_types"):
            bits.append("種別: " + ", ".join(as_str_list(c["publication_types"])))
        if c.get("resource_type"):
            bits.append("種別: " + as_str(c["resource_type"]))
        bits.append(c.get("url") or "")
        credits = (c.get("attributions") or []) + ([c["attribution"]] if c.get("attribution") else [])
        bits.extend(dict.fromkeys(credits))
        lines.append(" ".join(str(b) for b in bits if b))
        lines.append("    " + truncate(body, take))
        budget -= take
        injected.append(i)
    return {"text": "\n".join(lines), "numbers": injected,
            "omitted": [i for i in labels if i not in injected]}


def _evidence_block(citations: list[dict], *, item_chars: int | None = None,
                    total_chars: int | None = None, numbers: list[int] | None = None) -> str:
    """従来の文字列インターフェース。番号の認定は_evidence_windowのnumbersを使う。"""
    return _evidence_window(citations, item_chars=item_chars, total_chars=total_chars, numbers=numbers)["text"]


def tool_grounded(args: dict) -> dict:
    """根拠を先に取り、それを注入してから複数モデルに答えさせる（幻覚の抑止）。"""
    question = as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    sources = as_str_list(args.get("sources")) or None
    kb = knowledge_lookup(question, sources, limit=as_int(args.get("limit"), 3, 1, 10),
                          lang=as_str(args.get("lang"), "ja"),
                          kind=as_str(args.get("github_kind"), "repo"),
                          datacite_kind=as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True)
    citations = [c for c in (kb.get("citations") or []) if _kb_has_evidence(c)]
    if not citations:
        return {"error": "根拠が 0 件でした。query を変えるか sources を広げてください",
                "lookup": {"sources": kb.get("sources"), "errors": kb.get("errors")}}
    window = _evidence_window(citations)
    if not window["numbers"]:
        return {"error": "本文根拠を注入できませんでした。根拠の取得・本文予算を確認してください"}
    refs, info = _select_or_error(args, default_size=2)
    if not refs:
        return _no_models()
    prompt = (
        "次の【根拠】だけを情報源として質問に答えてください。\n"
        "根拠に無い事実は書かない。書けない場合は「根拠に無い」と明示する。\n"
        "本文中で根拠を示すときは [番号] を付ける。\n\n"
        f"【根拠】\n{window['text']}\n\n【質問】\n{question}"
    )
    results = ask_many(refs, prompt, max_tokens=as_int(args.get("max_tokens"), 700, 16, 4000),
                       kind="grounded", avoid=as_str_list(args.get("exclude")) or None)
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"]})
            continue
        cited, unsupported = _cited_numbers(res["text"] or "", len(citations), allowed=window["numbers"])
        answers.append({"model": ref, "served_by": res.get("served_by"),
                        "answer": truncate(res["text"], 2000),
                        "cited": cited, "cited_ok": bool(cited), "unsupported_citations": unsupported,
                        "truncated": res.get("truncated"), "cot_leak": res.get("cot_leak")})
    good = [a for a in answers if not a.get("error")]
    return {
        "question": question, "citations": citations, "citation_count": len(citations),
        "sources_used": kb.get("sources"), "source_errors": kb.get("errors"),
        "injected_citations": window["numbers"], "not_injected_citations": window["omitted"],
        "evidence_citation_count": len(window["numbers"]),
        "unsupported_citations": sorted({n for row in good for n in row.get("unsupported_citations", [])}),
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
    model = as_str(args.get("model"))
    selection = {"requested": True, "models": [model]} if model else {}
    if not model:
        selected, selection = select_models(1)
        model = selected[0] if selected else ""
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
    data = {"model": model, "count": len(rows), "ok": ok, "failed": len(rows) - ok, "results": rows,
            "selection": selection}
    if as_flag(args.get("reduce")):
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
    results = ask_many(refs, prompt, system=CONSULT_SYSTEM, max_tokens=max_tokens, kind="consult",
                       avoid=as_str_list(args.get("exclude")) or None)
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
                            max_tokens=max_tokens, kind="debate",
                            avoid=as_str_list(args.get("exclude")) or None)
        debate_rows = []
        for ref, res in zip(refs, rebuttal):
            if res.get("error"):
                debate_rows.append({"model": ref, "error": res["error"]})
                continue
            labels = {}
            for part in (res["text"] or "").splitlines():
                if ":" in part or "：" in part:
                    key, value = re.split(r"[:：]", part, maxsplit=1)
                    labels[key.strip()] = value.strip()
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
            "unresolved_dissent": any(
                (row.get("unresolved") or "").lower().rstrip(" .。!?！？;；").strip() not in _NO_ANSWER
                and bool(row.get("unresolved")) for row in debate_rows if not row.get("error")),
        }

    if depth == "deep":
        debate_by_model = {row.get("model"): row for row in debate_rows}
        last = []
        for initial in round_rows:
            if initial.get("error"):
                last.append(initial)
                continue
            debated = debate_by_model.get(initial.get("model"), {})
            if debated.get("error"):
                last.append({**initial, "error": debated["error"]})
                continue
            last.append({
                "model": initial["model"], "served_by": debated.get("served_by"),
                "conclusion": debated.get("position") or initial.get("conclusion") or "",
                "confidence": initial.get("confidence"), "question": initial.get("question") or "",
                "answer": debated.get("text") or initial.get("text") or "",
                "text": debated.get("text") or initial.get("text") or "",
            })
    else:
        last = [row for row in rounds[-1]["answers"] if not row.get("error")]
    question_rows = last
    last = [row for row in last if not row.get("error")]
    open_questions = []
    seen_questions = set()
    for row in question_rows:
        question_text = (row.get("question") or "").strip()
        normalized = norm_text(question_text)
        if question_text and normalized not in seen_questions:
            seen_questions.add(normalized)
            open_questions.append({"model": row["model"], "question": question_text})
    open_questions_for_main = list(dict.fromkeys(q["question"] for q in open_questions))
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
        "open_questions_for_main": open_questions_for_main,
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
                               kind=as_str(parsed.get("kind"), "repo"),
                               datacite_kind=as_str(parsed.get("datacite_kind"), "all"), fallback=parsed.get("fallback") is True)
        all_cites = out.get("citations") or []
        cites = [c for c in all_cites if _kb_has_evidence(c)]
        bibliography = [c for c in all_cites if not _kb_has_evidence(c)]
        brief = []
        for src, res in (out.get("results") or {}).items():
            for item in (res.get("items") or [])[:2]:
                label = "書誌のみ（本文根拠なし）" if item.get("metadata_only") else src
                brief.append(f"{label}: {item.get('title') or item.get('label') or ''} "
                             f"— {truncate(item.get('summary') or item.get('description') or '', 300)}")
        return {"hits": len(cites), "brief": brief, "citations": cites[:6], "bibliography": bibliography,
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



def _cited_numbers(answer: str, total: int, allowed: list[int] | None = None) -> tuple[list[int], list[int]]:
    """回答中の `[n]` を根拠の番号と突き合わせる。返り値は (根拠にある番号, 無い番号)。

    引用を検査しないと、サブエージェントが根拠を読まずに記憶で答えて `[1]` を飾りで付けた場合に"
    気づけない（実測: citations は常に全件返っていて、回答がそれを参照したかを見ていなかった）。
    `[0]` は「根拠を使っていない」の明示として許し、引用には数えない。
    """
    cited: set[int] = set()
    unsupported: set[int] = set()
    accepted = set(range(1, total + 1)) if allowed is None else set(allowed) & set(range(1, total + 1))
    for raw in re.findall(r"\[(\d{1,2})\]", answer or ""):
        number = as_int(raw, -1, 0, 99)
        if number <= 0:
            continue
        if number in accepted:
            cited.add(number)
        else:
            # 根拠が 0 件でも [1] と書けば「存在しない出典」なので unsupported に入れる。
            unsupported.add(number)
    return sorted(cited), sorted(unsupported)


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
        registry: list[dict] = []      # 番号を振った本文根拠（回答中の [n] と対応させる）
        bibliography: list[dict] = []  # 書誌探索は番号のregistryと分離する
        numbers: dict[tuple, int] = {}
        evidence: list[str] = []
        injected: set[int] = set()

        def register(new_cites: list[dict]) -> list[tuple[int, dict]]:
            """新しい根拠に通し番号を振る（同じ URL には同じ番号を保つ）。"""
            added: list[tuple[int, dict]] = []
            for cite in new_cites or []:
                key = _kb_citation_key(cite)
                if not _kb_has_evidence(cite) or not cite.get("url"):
                    continue
                if key in numbers:
                    if numbers[key] not in injected:
                        added.append((numbers[key], registry[numbers[key] - 1]))
                    continue
                numbers[key] = len(registry) + 1
                registry.append(cite)
                added.append((numbers[key], cite))
            return added

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
                added = register(got.get("citations") or [])
                bibliography.extend(got.get("bibliography") or [])
                trace.append({"tool": parsed.get("tool"), "args": parsed, "hits": got["hits"],
                              "new_citations": len(added)})
                evidence.extend(got["brief"][:6])
                # **番号つきの本文**を注入する。番号を保たないと回答中の [n] を検査できない。
                window = _evidence_window([c for _, c in added], item_chars=300, total_chars=1200,
                                          numbers=[n for n, _ in added])
                injected.update(window["numbers"])
                history += (f"\n\n【ツール結果 {parsed.get('tool')}】\n"
                            + (window["text"] or "本文根拠を注入できませんでした（書誌のみ・本文予算不足等）"))
                continue
            answer = as_str(parsed.get("answer"))
            if answer:
                cited, unsupported = _cited_numbers(answer, len(registry), allowed=list(injected))
                return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                        "answer": truncate(answer, 1500), "trace": trace,
                        "citations": registry, "bibliography": bibliography, "injected_citations": sorted(injected),
                        "cited": cited, "cited_ok": bool(cited),
                        "unsupported_citations": unsupported}
            # 最終ステップでツールを求められた場合は実行しない（予算切れ）。推測で埋めず、
            # **集めた根拠だけを返して「回答に到達しなかった」と明示する**。
            return {"model": ref, "served_by": res.get("served_by"), "steps": step + 1,
                    "answer": "", "steps_exhausted": True, "trace": trace,
                    "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                    "injected_citations": sorted(injected),
                    "cited": [], "cited_ok": False}
        return {"model": ref, "steps": max_steps, "trace": trace, "answer": "",
                "steps_exhausted": True, "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                    "injected_citations": sorted(injected),
                "cited": [], "cited_ok": False}

    results = run_parallel(refs, run_one, max_workers=min(len(refs), MAX_WORKERS))
    good = [r for r in results if not r.get("error")]
    citations, seen = [], set()
    for row in good:
        for cite in row.get("citations") or []:
            key = _kb_citation_key(cite)
            if key not in seen:
                seen.add(key)
                citations.append(cite)
    return {
        "task": truncate(task, 300), "models": refs, "selection": info,
        "agents": results, "answered": len(good), "failed": len(results) - len(good),
        "tool_calls": sum(len(r.get("trace") or []) for r in results),
        "citations": citations, "citation_count": len(citations),
        "agreement": agreement_of([r.get("answer") or "" for r in good]),
        "answers_with_citations": sum(1 for r in good if r.get("cited_ok")),
        "unsupported_citations": sorted({n for r in good
                                        for n in (r.get("unsupported_citations") or [])}),
        "usage_note": "サブエージェントは読み取り専用の知識ツールのみ呼べます。書き込みはしません。"
                      "回答中の [n] は根拠の番号と照合し、根拠に無い番号は unsupported_citations に出します。",
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


# ---------------------------------------------------------------- §6.9 思考台帳（freeagent_think）
#
# 分解 → 修正 → 分岐 → 仮説検証、を 1 ステップずつ積む。**このツールの知能は検証者**にあり、
# 思考の中身はメインが書く（だから `verify` は opt-in。付けなければサブ呼び出しは 0 回で、台帳の
# 記録と分岐・修正の管理だけを行う＝速い）。検証者は**生成者と別モデル**で、同意ではなく反証を返す。

def _think_step_label(step: dict) -> str:
    tags = []
    if step.get("branch_id"):
        tags.append(f"分岐 {step['branch_id']}")
    if step.get("is_revision"):
        tags.append(f"#{step['revises_thought']} の修正" if step.get("revises_thought") else "修正")
    if step.get("kind") == "hypothesis":
        tags.append(f"仮説・{step.get('hypothesis_status') or 'open'}")
    elif step.get("kind") == "test" and step.get("tests_hypothesis"):
        tags.append(f"仮説 #{step['tests_hypothesis']} の検証")
    elif step.get("kind") == "conclusion":
        tags.append("結論")
    return f"（{', '.join(tags)}）" if tags else ""


def _think_prompt(question: str, steps: list[dict], step: dict, row: dict | None = None) -> str:
    """検証者へ渡す材料。**解析済みの項目だけ**を渡す（原文の CoT をそのまま転送しない）。

    渡すのは**現行の道筋**（改訂済み・棄却分岐を除く）だけ。古い思考を混ぜると、検証者が既に
    撤回された前提を反証して予算を使う。改訂なら改訂前の文、仮説の検証なら対象の仮説を添える。
    """
    parts: list[str] = []
    n = as_int(step.get("n"), 0, 0, 9999)
    rev = as_int(step.get("revises_thought"), 0, 0, 9999)
    by_n = {as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    # 改訂の印（superseded_by）は統合時＝検証の後に付くので、今回の改訂対象もここで外す。
    active = [s for s in _think_active(steps, row) if as_int(s.get("n"), 0, 0, 9999) not in (n, rev)]
    if question:
        parts.append(f"【問い】\n{truncate(question, 800)}")
    plan = [p for p in ((row or {}).get("plan") or []) if isinstance(p, dict)]
    if plan:
        parts.append("【計画（サブ目標）】\n" + "\n".join(
            f"{p.get('id')}. {truncate(p.get('text') or '', 160)}" + ("（済）" if p.get("done_at") else "")
            for p in plan))
    if active:
        block = "\n".join(
            f"- #{s.get('n')}{_think_step_label(s)}: {truncate(s.get('text') or '', 400)}"
            for s in active[-6:])
        omitted = len([s for s in steps if as_int(s.get("n"), 0, 0, 9999) != n]) - len(active)
        tail = f"\n（改訂・棄却で外した思考 {omitted} 件は省略）" if omitted > 0 else ""
        parts.append(f"【これまでの思考（現行の道筋）】\n{block}{tail}")
    rev_block = rev if rev in by_n else 0
    if rev_block:
        parts.append(f"【改訂前の思考 #{rev}】\n{truncate(by_n[rev].get('text') or '', 600)}")
    hyp = as_int(step.get("tests_hypothesis"), 0, 0, 9999)
    if hyp and hyp in by_n:
        parts.append(f"【検証対象の仮説 #{hyp}】\n{truncate(by_n[hyp].get('text') or '', 600)}")
    parts.append(f"【検証対象の思考 #{step.get('n')}{_think_step_label(step)}】\n"
                 f"{truncate(step.get('text') or '', 1200)}")
    parts.append("上の形式（判定/反証/見落とし/確信度）だけで答えてください。")
    return "\n\n".join(parts)


def _think_ledger_view(steps: list[dict], *, keep: int = 8, row: dict | None = None) -> dict:
    """台帳の要約（計画・分岐・修正・仮説・最新の思考）。**全文は返さず末尾だけ**を載せる。"""
    row = row or {}
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}
    branches: dict[str, list[int]] = {bid: [] for bid in meta}
    for step in steps:
        if step.get("branch_id"):
            branches.setdefault(step["branch_id"], []).append(as_int(step.get("n"), 0, 0, 9999))
    active = _think_active(steps, row)
    plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    return {
        "steps_recorded": len(steps),
        "branches": [{"branch_id": bid, "steps": ns,
                      "from": (meta.get(bid) or {}).get("from"),
                      "status": (meta.get(bid) or {}).get("status") or "open",
                      "resolved_at": (meta.get(bid) or {}).get("resolved_at")}
                     for bid, ns in branches.items()],
        "branch_points": sorted({as_int(s.get("branch_from_thought"), 0, 0, 9999)
                                 for s in steps if s.get("branch_from_thought")}),
        "revisions": [{"step": as_int(s.get("n"), 0, 0, 9999),
                       "revises": s.get("revises_thought")}
                      for s in steps if s.get("is_revision")],
        # 改訂で置き換わった思考（消さずに印だけ付ける）と、現行の道筋（改訂済み・棄却分岐を除く）。
        "superseded": [{"step": as_int(s.get("n"), 0, 0, 9999), "by": s.get("superseded_by")}
                       for s in steps if s.get("superseded_by")],
        "active_path": [as_int(s.get("n"), 0, 0, 9999) for s in active],
        "plan": [{"id": p.get("id"), "text": truncate(p.get("text") or "", 200),
                  "done": bool(p.get("done_at")), "done_at": p.get("done_at"),
                  "steps": [as_int(s.get("n"), 0, 0, 9999) for s in steps
                            if as_int(s.get("subgoal"), 0, 0, 999) == p.get("id")]} for p in plan],
        "plan_progress": {"done": sum(1 for p in plan if p.get("done_at")), "total": len(plan)},
        "hypotheses": [{"n": as_int(s.get("n"), 0, 0, 9999), "text": truncate(s.get("text") or "", 200),
                        "status": s.get("hypothesis_status") or "open",
                        "tested_by": s.get("tested_by") or [],
                        "superseded_by": s.get("superseded_by"),
                        "verdicts": ((s.get("verification") or {}).get("verdicts"))}
                       for s in steps if s.get("kind") == "hypothesis"],
        "latest": [{"n": as_int(s.get("n"), 0, 0, 9999), "branch_id": s.get("branch_id"),
                    "is_revision": bool(s.get("is_revision")),
                    "kind": s.get("kind") or "step",
                    "superseded_by": s.get("superseded_by"),
                    "hypothesis_status": s.get("hypothesis_status"),
                    "tests_hypothesis": s.get("tests_hypothesis"),
                    "subgoal": s.get("subgoal"),
                    "text": truncate(s.get("text") or "", 300)} for s in steps[-keep:]],
        # 直近に宣言された見積り総数（増減してよい・調整はメインが行う）。台帳に残して履歴化する。
        "total_thoughts": _think_latest_total(steps),
        "total_history": [h for h in (row.get("total_history") or []) if isinstance(h, dict)],
    }


def _think_suggestions(data: dict, steps: list[dict], needed: bool) -> list[str]:
    """次の一手を**数値から**生成する。LLM 向けの助言なので structuredContent にだけ置く（規約 3）。"""
    out: list[str] = []
    verify = data.get("verification")
    if verify:
        if verify["verdicts"]["要修正"]:
            out.append(f"判定に「要修正」が {verify['verdicts']['要修正']} 件あります。"
                       "修正ステップ（is_revision=true, revises_thought=#n）を推奨します。")
        if verify["verdicts"]["根拠不足"]:
            out.append(f"判定に「根拠不足」が {verify['verdicts']['根拠不足']} 件あります。"
                       "freeagent_lookup / freeagent_grounded で根拠を取ってから再検証してください。")
        if not verify["answered"]:
            out.append("独立した検証が得られていません（この思考を『検証済み』として扱わないでください）。")
        elif verify["failed"]:
            out.append(f"検証 {verify['failed']} 体が脱落しました（failed_rows に残しています）。")
        if verify["objections"]:
            out.append(f"未解決の反証が {len(verify['objections'])} 件あります。"
                       "結論の前に潰すか、未解決点として残してください。")
    ledger = data["ledger"]
    open_branches = [b["branch_id"] for b in ledger["branches"] if b.get("status", "open") == "open"]
    if len(open_branches) > 1 or (open_branches and not needed):
        out.append(f"未決着の分岐が {len(open_branches)} 本あります（{', '.join(open_branches)}）。"
                   "統合ステップを積むか、resolve_branch と branch_status（adopted/abandoned/merged）で"
                   "決着を記録してください。")
    untested = [h["n"] for h in ledger.get("hypotheses") or []
                if h.get("status") == "open" and not h.get("tested_by") and not h.get("superseded_by")]
    if untested:
        out.append(f"未検証の仮説があります（{', '.join(f'#{x}' for x in untested)}）。"
                   "tests_hypothesis=#n と hypothesis_status で検証結果を記録するか、verify=true で"
                   "独立モデルの反証を取ってください。")
    plan = ledger.get("plan") or []
    est = data.get("total_thoughts")
    # 構造を使っていない台帳への促し（常用の規則を台帳側でも補強する。数値から生成・判断はメイン）。
    if needed and ledger["steps_recorded"] == 1 and not plan:
        out.append("計画がありません。複数段の問題なら plan でステップ（サブ目標）に分解してください"
                   "（1 問 1 答で済むなら不要です）。")
    if (needed and ledger["steps_recorded"] == 3 and not ledger.get("hypotheses")
            and not ledger["branches"] and not ledger.get("revisions")):
        out.append(f"{ledger['steps_recorded']} ステップ積みましたが、仮説・分岐・改訂がありません。"
                   "前提や原因は kind=hypothesis で立てて検証し、別案は branch_from_thought で分岐して"
                   "比べ、考えが変わった前のステップは revises_thought で改訂してください。")
    if plan and est and len(plan) > est:
        out.append(f"計画は {len(plan)} 項目ですが見積り総数は {est} です。total_thoughts の見直しを推奨します。")
    pending = [p["id"] for p in plan if not p.get("done")]
    if pending and not needed:
        out.append(f"計画の未達が {len(pending)} 項目あります（{', '.join(str(x) for x in pending)}）。"
                   "結論にするなら未達の理由を残してください。")
    if needed and len(steps) >= THOUGHT_MAX_STEPS:
        out.append(f"思考数が上限（{THOUGHT_MAX_STEPS}）です。新しい session_id で台帳を分けてください。")
    if needed and est and ledger["steps_recorded"] >= est:
        out.append(f"見積り総数（{est}）に達しました。続けるなら total_thoughts を増やしてください"
                   "（据え置き・減らすのも可。総数はメインが動的に調整します）。")
    if not needed:
        out.append("next_thought_needed=false。最終結論はメインで確定してください。")
    return out


def tool_think(args: dict) -> dict:
    """メインの思考ステップを台帳に積み、任意で独立モデルに反証させる。

    分解（`plan` / `subgoal`）・改訂（`revises_thought` → 元ステップに `superseded_by`）・
    分岐（`branch_from_thought` / `branch_id` / `resolve_branch`）・仮説（`kind=hypothesis` /
    `tests_hypothesis`）・見積り総数の動的調整（`total_thoughts`）を台帳の**構造**として持つ。
    参照先の番号・分岐・仮説は**サブ呼び出しの前に**検証し、無ければ推測で繋がずエラーで返す。

    `verify=true` を付けたステップだけ、生成者とは別の Free モデルが「判定/反証/見落とし」を返す。
    `propose_alternatives=true` なら、さらに別のモデルが**この道筋とは異なる代替案**を返す（記録は
    メインが選ぶ）。どちらも**同意の収集ではない**。バックエンドへ到達できなかった場合は**台帳に
    書かない**（検証されていない前提の上に次の思考を積まないため。規約 21）。
    """
    if not THOUGHTS_ENABLED:
        return {"error": "思考台帳は無効です（FREEAGENT_THOUGHTS=0 で停止中）", "enabled": False}
    sid_in = as_str(args.get("session_id"))
    stored = thought_get(sid_in) if sid_in else None
    if as_flag(args.get("view")):
        return _think_view(sid_in, stored)
    thought = as_str(args.get("thought"))
    if not thought:
        return {"error": "thought は必須です（空文字は不可。台帳を読むだけなら view=true と session_id）"}

    steps = [s for s in ((stored or {}).get("steps") or []) if isinstance(s, dict)]
    notes: list[str] = []
    if sid_in and stored is None:
        notes.append(f"セッション {truncate(sid_in, 40)} は見つかりません（期限切れ／未知）。"
                     "新しい台帳を開始しました（古い思考は復活させません）。")

    explicit_number = args.get("thought_number") is not None
    n = as_int(args.get("thought_number"), len(steps) + 1, 1, 9999)
    existing = {as_int(s.get("n"), 0, 0, 9999) for s in steps}
    if n not in existing and len(steps) >= THOUGHT_MAX_STEPS:
        return {"error": f"思考数が上限（{THOUGHT_MAX_STEPS}）に達しています。新しい session_id で"
                         "台帳を分けるか、FREEAGENT_THOUGHT_MAX_STEPS を上げてください",
                "session_id": sid_in or None, "steps_recorded": len(steps)}

    # 構造の検証は**サブ呼び出しの前**（参照先が無い呼び出しで検証者の予算を使わない）。
    problem, st = _think_structure(args, steps, n, stored or {})
    if problem:
        problem.update({"session_id": sid_in or None, "steps_recorded": len(steps)})
        return problem
    notes.extend(st["notes"])

    # 見積り総数の動的調整: 省略時は台帳の見積りを引き継ぎ、番号が見積りを超えたら引き上げる。
    declared = as_int(args.get("total_thoughts"), 0, 0, 999)
    total = declared or _think_latest_total(steps) or 0
    total_auto = False
    if total and n > total:
        notes.append(f"思考 #{n} が見積り総数 {total} を超えたため、見積りを {n} へ引き上げました。")
        total, total_auto = n, True

    step = {
        "n": n, "text": truncate(thought, _THOUGHT_CHARS),
        "kind": st["kind"],
        "branch_id": st["branch_id"],
        "is_revision": st["is_revision"],
        "revises_thought": st["revises"] or None,
        "branch_from_thought": st["branch_from"] or None,
        "tests_hypothesis": st["tests_hypothesis"] or None,
        "result_status": st["hypothesis_status"] or None,
        "subgoal": st["subgoal"] or None,
        "subgoal_done": st["subgoal_done"],
        "total_thoughts": total or None,
        "at": now_ts(), "verification": None,
    }
    if st["kind"] == "hypothesis":
        step["hypothesis_status"] = "open"
    used = [m for m in ((stored or {}).get("verifier_models") or []) if isinstance(m, str)]
    question = as_str(args.get("question")) or (stored or {}).get("question") or ""
    # 検証者へ渡す台帳には、今回の呼び出しで決まる計画・分岐の決着を先に反映して見せる。
    preview = _think_preview_row(stored or {}, st)

    verification = None
    if as_flag(args.get("verify")):
        refs, info = select_models(as_int(args.get("size"), 2, 1, 4),
                                   as_str_list(args.get("models")) or None,
                                   prefer=as_str_list(args.get("prefer")) or None,
                                   exclude=(as_str_list(args.get("exclude")) or []) + used)
        if not refs:
            return _no_models()   # 状態を書かない（環境障害で台帳を汚さない）
        results = ask_many(refs, _think_prompt(question, steps, step, preview), system=THINK_CRITIC_SYSTEM,
                           max_tokens=as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                           avoid=as_str_list(args.get("exclude")) or None)
        rows: list[dict] = []
        for ref, res in zip(refs, results):
            if res.get("error"):
                rows.append({"model": ref, "error": res["error"]})
                continue
            parsed = parse_verdict(res["text"])
            rows.append({"model": ref, "served_by": res.get("served_by"),
                         "verdict": parsed["verdict"], "objection": parsed["objection"],
                         "oversight": parsed["oversight"], "confidence": parsed["confidence"],
                         "labels_found": parsed["labels_found"],
                         "text": truncate(res["text"], 600), "cot_leak": res.get("cot_leak")})
        answered = [r for r in rows if not r.get("error")]
        failed = [r for r in rows if r.get("error")]
        # 環境障害で 1 体も応答していないなら**書かない**（規約 21）。モデル側の失敗（429 等）は
        # 従来どおり記録し、`answered: 0` として隠さず返す。
        if not answered and failed and all(is_env_failure(r.get("error") or "") for r in failed):
            return {"error": "検証に到達できませんでした（バックエンド不通: "
                             f"{truncate(failed[0].get('error') or '', 120)}）。"
                             "この思考は台帳に記録していません。",
                    "session_id": sid_in or None, "verifier_models": refs,
                    "failures": [{"model": r["model"], "error": truncate(r.get("error") or "", 160)}
                                 for r in failed]}
        confs = [r["confidence"] for r in answered if isinstance(r.get("confidence"), int)]
        verification = {
            "models": refs, "selection": info, "answered": len(answered), "failed": len(failed),
            "verdicts": {v: sum(1 for r in answered if r.get("verdict") == v)
                         for v in ("妥当", "要修正", "根拠不足")},
            "unlabeled": sum(1 for r in answered if not r.get("verdict")),
            "confidence_mean": round(sum(confs) / len(confs), 1) if confs else None,
            "objections": [{"model": r["model"], "objection": r["objection"]}
                           for r in answered if r.get("objection")],
            "oversights": [{"model": r["model"], "oversight": r["oversight"]}
                           for r in answered if r.get("oversight")],
            "answers": rows,
            "failed_rows": [{"model": r["model"], "error": truncate(r.get("error") or "", 160)}
                            for r in failed],
            "agreement": agreement_of([r.get("verdict") or "" for r in answered if r.get("verdict")]),
            "note": "検証は独立モデルによる反証探索です。合意は正しさの保証ではありません。",
        }
        step["verification"] = verification
        used = (used + [r["model"] for r in rows])[:12]

    alternatives = None
    if as_flag(args.get("propose_alternatives")):
        alternatives = _think_alternatives(args, question, steps, step, preview, exclude=used)
        if alternatives.get("error"):
            alternatives.update({"session_id": sid_in or None})
            return alternatives    # 状態を書かない（規約 21）
        step["alternatives"] = alternatives
        used = (used + list(alternatives.get("models") or []))[:12]

    # 台帳への統合は**ロック内**で行う（番号の採番と、他ステップ・メタへの波及も含む。
    # 並列呼び出しで片方が消えるのを防ぐ）。検証プロンプトに載せた `#n` は採番前の見積りなので、
    # 並列時は 1 ずれることがある（返り値は統合後の確定番号を使う）。
    ops = {"revises": st["revises"], "tests_hypothesis": st["tests_hypothesis"],
           "hypothesis_status": st["hypothesis_status"],
           "resolve_branch": st["resolve_branch"], "branch_status": st["branch_status"],
           "plan": st["plan"], "subgoal_done": st["subgoal_done"],
           "total": total, "total_auto": total_auto}
    merged = thought_merge(sid_in, step, question=question, verifier_models=used,
                           assign_number=not explicit_number, ops=ops)
    if merged.get("refused"):
        return {"error": f"思考数が上限（{THOUGHT_MAX_STEPS}）に達しています。新しい session_id で"
                         "台帳を分けるか、FREEAGENT_THOUGHT_MAX_STEPS を上げてください",
                "session_id": merged["session_id"], "steps_recorded": merged["steps_recorded"]}
    sid = merged["session_id"]
    step = merged["step"] or step
    n = as_int(step.get("n"), n, 1, 9999)
    if merged.get("replaced"):
        notes.append(f"思考 #{n} は既にありました。置き換えました（同じ番号の再送は上書き）。")
    after = thought_get(sid) or {}
    steps_out = [s for s in (after.get("steps") or []) if isinstance(s, dict)]
    used = [m for m in (after.get("verifier_models") or used) if isinstance(m, str)]

    needed = as_flag(args.get("next_thought_needed", args.get("needs_more_thoughts", True)))
    data = {
        "session_id": sid, "step": n, "text": step["text"], "kind": step.get("kind") or "step",
        "branch_id": step["branch_id"], "is_revision": step["is_revision"],
        "revises_thought": step["revises_thought"],
        "branch_from_thought": step["branch_from_thought"],
        "tests_hypothesis": step.get("tests_hypothesis"),
        "hypothesis_status": step.get("result_status") or step.get("hypothesis_status"),
        "subgoal": step.get("subgoal"),
        "total_thoughts": total or None,
        "total_auto_adjusted": total_auto,
        "next_thought_needed": needed,
        "ledger": _think_ledger_view(steps_out, row=after),
        "verification": verification,
        "verified": bool(verification and verification["answered"]),
        "alternatives": alternatives,
        "verifier_models": used,
        "notes": notes,
        "next_call": {"tool": "freeagent_think",
                      "args": {"session_id": sid, "thought": "<次の思考>",
                               "thought_number": n + 1, "verify": bool(verification),
                               **({"total_thoughts": max(total, n + 1)} if total else {})}},
        "privacy_note": "台帳は思考の記録です。判断と責任はメインLLMに残ります。",
    }
    data["suggestions"] = _think_suggestions(data, steps_out, needed)
    return data


# ---------------------------------------------------------------- §6.10 思考台帳の構造（検証・閲覧・代替案）
#
# §6.9 の `tool_think` が使う補助。参照の検証（`_think_structure`）は**推測で繋がない**:
# 存在しない番号の改訂・分岐元・仮説、計画に無いサブ目標は、黙って直さずエラーで返す
# （誤った番号のまま台帳に積むと、以後の「現行の道筋」が静かに壊れる）。自動で補うのは
# 「branch_id を省略した新しい分岐への ID の割り当て」だけで、その場合も `notes` に必ず出す。

# 代替案の提案者（`propose_alternatives=true`）。検証者（THINK_CRITIC_SYSTEM）が「この思考は正しいか」を
# 突くのに対し、提案者は「**別の道筋は無いか**」を出す。メインと別モデルなので多様性は構造的に担保される。
THINK_ALT_SYSTEM = (
    "あなたは代替案の提案者です。与えられた思考の道筋とは**異なる**仮説・解法・道筋を挙げてください。"
    "出力は次の形式の行だけです（最大3行）。\n"
    "代替: <1〜2文。どこが今の道筋と違うかが分かるように>\n"
    "今の道筋の言い換えや賛成は書かない。無ければ「代替: なし」の1行だけ。"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)
_LABEL_ALT = re.compile(
    r"^(?:(?:[-*・•]|\d+[.)．、])\s*)?(?:\*\*)?(?:代替案?|対立仮説|alternative)\s*\d*(?:\*\*)?\s*[:：]\s*(.+)",
    re.I)
_BULLET = re.compile(r"^(?:[-*・•]|\d+[.)．、])\s+(.+)")


def parse_alternatives(text, limit: int = 3, *, truncated: bool = False) -> list[str]:
    """提案者の「代替: …」行を取り出す。ラベルが 1 つも無ければ箇条書きだけを拾う（推測で埋めない）。

    `truncated`（max_tokens で打ち切られた応答）のときは、**本文の最終行にあたる案**を捨てる。
    打ち切りは最終行の途中で起きるので、それを案として渡すと文の途中で切れた提案が
    完全な案として台帳に残る（実測「代替: 親プロセスのコマン」）。
    """
    lines = [ln.strip() for ln in (text if isinstance(text, str) else "").splitlines() if ln.strip()]
    if truncated and lines:
        lines = lines[:-1]
    labeled = [m.group(1) for m in (_LABEL_ALT.match(ln) for ln in lines) if m]
    picked = labeled or [m.group(1) for m in (_BULLET.match(ln) for ln in lines) if m]
    out: list[str] = []
    for value in picked:
        value = value.strip().strip("* ")
        if value and not _is_no_answer(value):
            out.append(truncate(value, 300))
        if len(out) >= limit:
            break
    return out


def _think_latest_total(steps: list[dict]) -> int | None:
    return next((as_int(s.get("total_thoughts"), 0, 0, 999) for s in reversed(steps)
                 if as_int(s.get("total_thoughts"), 0, 0, 999)), None)


def _think_active(steps: list[dict], row: dict | None) -> list[dict]:
    """現行の道筋: 改訂で置き換わった思考と、棄却（abandoned）された分岐の思考を除いたもの。"""
    meta = (row or {}).get("branch_meta")
    meta = meta if isinstance(meta, dict) else {}
    dropped = {bid for bid, m in meta.items() if isinstance(m, dict) and m.get("status") == "abandoned"}
    return [s for s in steps if not s.get("superseded_by") and s.get("branch_id") not in dropped]


def _think_preview_row(row: dict, st: dict) -> dict:
    """今回の呼び出しで決まる計画・分岐の決着を反映した台帳メタの写し（検証者に見せる用。書かない）。"""
    out = json.loads(json.dumps({k: row.get(k) for k in ("plan", "branch_meta")}))
    if st.get("plan"):
        out["plan"] = [{"id": i + 1, "text": t, "done_at": None} for i, t in enumerate(st["plan"])]
    meta = out.get("branch_meta") if isinstance(out.get("branch_meta"), dict) else {}
    if st.get("resolve_branch") and st["resolve_branch"] in meta:
        meta[st["resolve_branch"]]["status"] = st["branch_status"]
    out["branch_meta"] = meta
    return out


def _think_structure(args: dict, steps: list[dict], n: int, row: dict) -> tuple[dict | None, dict]:
    """構造引数を検証・正規化する。問題があれば `({"error": ...}, {})` を返す（台帳は書かない）。"""
    notes: list[str] = []
    by_n = {as_int(s.get("n"), 0, 0, 9999): s for s in steps}
    known = sorted(k for k in by_n if k != n)
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}

    def fail(msg: str) -> tuple[dict, dict]:
        return {"error": msg, "known_thoughts": known, "known_branches": sorted(meta)}, {}

    kind_raw = as_str(args.get("kind")).strip().lower()
    if kind_raw and kind_raw not in THOUGHT_KINDS:
        return fail(f"kind は {' / '.join(THOUGHT_KINDS)} のいずれかです（受け取った値: {truncate(kind_raw, 40)}）")
    tests = as_int(args.get("tests_hypothesis"), 0, 0, 9999)
    kind = kind_raw or ("test" if tests else "step")

    # 改訂: 元ステップは消さず、統合時に `superseded_by` を付ける（§2.7）。
    revises = as_int(args.get("revises_thought"), 0, 0, 9999)
    is_revision = as_flag(args.get("is_revision")) or bool(revises)
    if is_revision and not revises:
        return fail("is_revision=true には revises_thought（改訂する思考の番号）が必要です")
    if revises and revises not in known:
        return fail(f"revises_thought=#{revises} は台帳にありません（自分自身は改訂できません）")
    if revises and by_n[revises].get("superseded_by") not in (None, n):
        notes.append(f"#{revises} は既に #{by_n[revises]['superseded_by']} で改訂済みです（改訂を重ねます）。")

    # 分岐: 新しい分岐には分岐元が要る。既存の分岐は分岐元を引き継ぐ。
    bid = as_str(args.get("branch_id")).strip()[:40]
    bfrom = as_int(args.get("branch_from_thought"), 0, 0, 9999)
    if bfrom and bfrom not in known:
        return fail(f"branch_from_thought=#{bfrom} は台帳にありません")
    if bid and bid in meta:
        origin = as_int((meta[bid] or {}).get("from"), 0, 0, 9999)
        if bfrom and origin and bfrom != origin:
            notes.append(f"分岐 {bid} の分岐元は #{origin} のままです（branch_from_thought=#{bfrom} は使いません）。")
        bfrom = origin or bfrom
        state = (meta[bid] or {}).get("status") or "open"
        if state != "open":
            notes.append(f"分岐 {bid} は {state} で決着済みです（状態は変えずに記録しました。"
                         "再開は resolve_branch と branch_status=open）。")
    elif bid and not bfrom and not any(s.get("branch_id") == bid for s in steps):
        return fail(f"新しい分岐 {bid} には branch_from_thought（分岐元の思考番号）が必要です")
    elif bfrom and not bid:
        k = len(meta) + 1
        while f"b{k}" in meta:
            k += 1
        bid = f"b{k}"
        notes.append(f"branch_id が省略されたため、#{bfrom} からの分岐に {bid} を割り当てました。")

    # 分岐の決着（採用・棄却・統合）。棄却した分岐は「現行の道筋」から外れる。
    resolve = as_str(args.get("resolve_branch")).strip()[:40]
    bstatus = as_str(args.get("branch_status")).strip().lower()
    if bstatus and bstatus not in BRANCH_STATES:
        return fail(f"branch_status は {' / '.join(BRANCH_STATES)} のいずれかです")
    if bstatus and not resolve:
        return fail("branch_status には resolve_branch（決着させる分岐の ID）が必要です")
    if resolve and not bstatus:
        return fail("resolve_branch には branch_status（adopted / abandoned / merged / open）が必要です")
    if resolve and resolve not in meta and resolve != bid:
        return fail(f"分岐 {truncate(resolve, 40)} は台帳にありません")

    # 仮説の検証: 対象は kind=hypothesis のステップに限る。
    hstatus = as_str(args.get("hypothesis_status")).strip().lower()
    if hstatus and hstatus not in HYPOTHESIS_STATES:
        return fail(f"hypothesis_status は {' / '.join(HYPOTHESIS_STATES)} のいずれかです")
    if hstatus and not tests:
        return fail("hypothesis_status には tests_hypothesis（検証する仮説の番号）が必要です")
    if tests and tests not in known:
        return fail(f"tests_hypothesis=#{tests} は台帳にありません")
    if tests and by_n[tests].get("kind") != "hypothesis":
        return fail(f"#{tests} は仮説（kind=hypothesis）として記録されていません")
    if tests and kind == "hypothesis":
        return fail("仮説の検証ステップは kind=test です（仮説そのものは kind=hypothesis で別に積みます）")

    # 分解: 計画（サブ目標）は再送で改訂できる。上限超過は切り捨てずエラー。
    plan = [truncate(p, 200) for p in as_str_list(args.get("plan"))]
    if len(plan) > THOUGHT_PLAN_MAX:
        return fail(f"plan は {THOUGHT_PLAN_MAX} 項目までです（{len(plan)} 項目）。粒度を上げて分けてください")
    plan_now = plan or [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    sub = as_int(args.get("subgoal"), 0, 0, 999)
    if sub and not 1 <= sub <= len(plan_now):
        return fail(f"subgoal={sub} は計画にありません（計画 {len(plan_now)} 項目。先に plan で分解してください）")
    sub_done = as_flag(args.get("subgoal_done"))
    if sub_done and not sub:
        return fail("subgoal_done には subgoal（達成したサブ目標の番号）が必要です")

    return None, {"kind": kind, "is_revision": is_revision, "revises": revises,
                  "branch_id": bid, "branch_from": bfrom,
                  "resolve_branch": resolve, "branch_status": bstatus,
                  "tests_hypothesis": tests, "hypothesis_status": hstatus,
                  "plan": plan, "subgoal": sub, "subgoal_done": sub_done, "notes": notes}


def _think_view(sid: str, stored: dict | None) -> dict:
    """台帳を**書かずに**読む（文脈圧縮・再起動の後に、積んだ思考へ戻るため）。"""
    if not sid:
        return {"error": "view=true には session_id が必要です"}
    if stored is None:
        return {"error": f"セッション {truncate(sid, 40)} は見つかりません（期限切れ／未知）",
                "session_id": sid}
    steps = [s for s in (stored.get("steps") or []) if isinstance(s, dict)]
    last = max([as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0])
    total = _think_latest_total(steps)
    data = {
        "session_id": sid, "view": True, "step": last or None, "question": stored.get("question") or "",
        "total_thoughts": total, "next_thought_needed": True,
        "ledger": _think_ledger_view(steps, keep=THOUGHT_MAX_STEPS, row=stored),
        "verification": None, "verified": False, "alternatives": None,
        "verifier_models": [m for m in (stored.get("verifier_models") or []) if isinstance(m, str)],
        "notes": [],
        "next_call": {"tool": "freeagent_think",
                      "args": {"session_id": sid, "thought": "<次の思考>", "thought_number": last + 1}},
    }
    data["suggestions"] = _think_suggestions(data, steps, True)
    return data


def _think_alternatives(args: dict, question: str, steps: list[dict], step: dict, row: dict,
                        *, exclude: list[str]) -> dict:
    """生成者・検証者とは別のモデルに**代替案**を出させる。不通なら error を返す（呼び出し側は書かない）。"""
    user_exclude = as_str_list(args.get("exclude")) or []
    refs, info = select_models(as_int(args.get("size"), 2, 1, 4), None,
                               prefer=as_str_list(args.get("prefer")) or None,
                               exclude=user_exclude + list(exclude))
    if not refs:
        return _no_models()
    prompt = _think_prompt(question, steps, step, row).rsplit("\n\n", 1)[0] + (
        "\n\n上の道筋とは異なる代替の仮説・解法・道筋を「代替: …」の形式で最大3行だけ挙げてください。")
    # 既定 400 のまま: 上げると proxy 経由で CONNECT_TIMEOUT（10 秒）を超えやすい（SPEC §3 の実測）。
    # 打ち切り（truncated）で文の途中で切れた最後の案は parse_alternatives が捨てる（実測「代替: 親プロセスのコマン」）。
    # avoid: フォールバックが同じ呼び出しの**検証者**に落ちると「検証者とも別のモデル」が破れる（実測）。
    results = ask_many(refs, prompt, system=THINK_ALT_SYSTEM,
                       max_tokens=as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                       avoid=user_exclude + list(exclude))
    rows: list[dict] = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            rows.append({"model": ref, "error": res["error"]})
            continue
        rows.append({"model": ref, "served_by": res.get("served_by"),
                     "items": parse_alternatives(res.get("text"), truncated=bool(res.get("truncated"))),
                     "truncated": bool(res.get("truncated")),
                     "text": truncate(res.get("text") or "", 600)})
    answered = [r for r in rows if not r.get("error")]
    failed = [r for r in rows if r.get("error")]
    if not answered and failed and all(is_env_failure(r.get("error") or "") for r in failed):
        return {"error": "代替案の提案者に到達できませんでした（バックエンド不通: "
                         f"{truncate(failed[0].get('error') or '', 120)}）。この思考は台帳に記録していません。",
                "failures": [{"model": r["model"], "error": truncate(r.get("error") or "", 160)} for r in failed]}
    return {
        "models": refs, "selection": info, "answered": len(answered), "failed": len(failed),
        "items": [{"model": r["model"], "text": item} for r in answered for item in r["items"]],
        "empty": [r["model"] for r in answered if not r["items"]],
        "answers": rows,
        "failed_rows": [{"model": r["model"], "error": truncate(r.get("error") or "", 160)} for r in failed],
        "note": "代替案は独立モデルの提案（仮説）です。検証済みではなく、採否はメインが決めます。",
    }


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
            "【差分】`query`（ID の部分一致）と `provider`（nous / openrouter / nvidia / huggingface / groq / cloudflare / gemini）で絞り込む。"
            "返る `ref` は他ツールの `models` 引数にそのまま渡せる（HF は `:提供元` を付けて経路を固定できる）。"
            "【使わない条件】通常は不要（panel/consult 等が自動で選ぶ）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "モデル ID の部分一致（例: qwen / llama / :free）"},
                "q": {"type": "string", "description": "query の別名"},
                "provider": {"type": "string",
                             "description": "nous / openrouter / nvidia / huggingface / groq / cloudflare / gemini のいずれか"},
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
            "（百科・構造化）／GitHub（コード）を並列に引く。追加の datacite / openaire / europepmc / zenodo / ror / "
            "doaj（OA論文）/ npm / crates（パッケージ検索）は"
            "sources で明示指定する。DataCite は自然語の各語を AND 検索、datacite_kind=dataset で研究データ。"
            "fallback=true のときだけ自然語 arXiv検索の失敗/遅延を DataCite で代替する（取得元を明示）。"
            "【使わない条件】単一の事実だけなら web_search が速い。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "既定6ソース: wikipedia / wikidata / arxiv / crossref / openalex / github。追加は明示指定: datacite / openaire / europepmc / zenodo / ror / doaj / npm / crates"},
                "limit": {"type": "integer", "description": "各ソースの件数（既定 3）"},
                "lang": {"type": "string", "description": "Wikipedia/Wikidata の言語（既定 ja）"},
                "github_kind": {"type": "string", "description": "repo / issue / code（code はトークン必須）"},
                "fallback": {"type": "boolean", "description": "既定 false。自然語 arXiv検索の失敗/遅延時に DataCite を許可"},
                "datacite_kind": {"type": "string", "enum": ["all", "arxiv", "dataset"],
                                  "description": "DataCite の対象（既定 all）。自然語の各語を AND 検索"},
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
            "根拠はタイトルだけでなく**本文（要約・アブストラクト）つき**で注入する。"
            "回答には [番号] の引用が付き、引用の有無を機械的に数えて返す。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "既定6種。明示追加: datacite / openaire / europepmc / zenodo / ror / doaj / npm / crates。RORは機関属性、Zenodoは説明メタデータで全文ではない。npm / cratesの説明は登録者の自己申告"},
                "fallback": {"type": "boolean", "description": "自然語 arXiv検索の DataCite 代替を明示許可"},
                "datacite_kind": {"type": "string", "enum": ["all", "arxiv", "dataset"]},
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
        "name": "freeagent_think",
        "description": (
            "【常用】2 段以上の推論が要る問題（設計・調査・デバッグ・比較検討・計画）では、考え始める前に"
            "これを開き、分解・改訂・分岐・見積りの調整・仮説の検証を台帳に積みながら進める。"
            "【使う条件】(a) 複雑な問題を**サブ目標に分解**して（plan）1件ずつ積みたい "
            "(b) 理解が深まるにつれて**過去の思考を改訂**したい（revises_thought。元は改訂済みとして残る） "
            "(c) **代替案へ分岐**して探索し、採用・棄却を記録したい（branch_* / resolve_branch） "
            "(d) 思考数の見積りを**動的に増減**したい (e) **仮説を立てて検証**したい（kind=hypothesis → "
            "tests_hypothesis）／生成者以外の独立モデルに反証（verify）や代替案（propose_alternatives）を出させたい。"
            "【差分】構造つきの思考台帳（計画の進捗・現行の道筋・分岐の決着・仮説の状態）。参照先の番号が"
            "無ければ推測で繋がずエラー。verify / propose_alternatives のときだけ別の Free モデルを呼ぶ。"
            "view=true で書かずに台帳全体を読める（文脈圧縮後の復帰）。"
            "【使わない条件】単発の問いは panel / consult が速い。思考の記録だけなら思考メモ帳系の軽量ツールで足りる。"
            "【注意】返る検証・代替案は仮説であり、合意は正しさの保証ではありません。判断はメインが行います。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "今回の思考ステップ（view=true 以外は必須）"},
                "session_id": {"type": "string",
                               "description": "継続する台帳のID（返り値の next_call に同梱。省略で新規）"},
                "view": {"type": "boolean", "description": "台帳を読むだけ（書かない・session_id 必須）"},
                "question": {"type": "string", "description": "解こうとしている問い（検証者へ渡す文脈）"},
                "kind": {"type": "string", "enum": list(THOUGHT_KINDS),
                         "description": "思考の種類（既定 step。tests_hypothesis 指定時は test）"},
                "plan": {"type": "array", "items": {"type": "string"},
                         "description": f"サブ目標への分解（最大 {THOUGHT_PLAN_MAX}。再送で計画を改訂）"},
                "subgoal": {"type": "integer", "description": "この思考が扱うサブ目標の番号（plan の 1 始まり）"},
                "subgoal_done": {"type": "boolean", "description": "subgoal を達成済みにする"},
                "thought_number": {"type": "integer", "description": "思考番号（省略時は末尾+1）"},
                "total_thoughts": {"type": "integer",
                                   "description": "見積り総数（増減してよい。省略時は台帳の値を引き継ぎ、"
                                                  "番号が超えたら自動で引き上げる）"},
                "next_thought_needed": {"type": "boolean",
                                        "description": "続けるか（省略時 true）。false で結論フェーズ"},
                "needs_more_thoughts": {"type": "boolean", "description": "next_thought_needed の別名"},
                "is_revision": {"type": "boolean", "description": "前の思考を改訂する（revises_thought 必須）"},
                "revises_thought": {"type": "integer", "description": "改訂対象の思考番号"},
                "branch_from_thought": {"type": "integer", "description": "分岐元の思考番号（新しい分岐で必須）"},
                "branch_id": {"type": "string", "description": "分岐の識別子（例: b1。省略時は自動割当）"},
                "resolve_branch": {"type": "string", "description": "決着させる分岐の ID"},
                "branch_status": {"type": "string", "enum": list(BRANCH_STATES),
                                  "description": "分岐の決着（abandoned は現行の道筋から外れる）"},
                "tests_hypothesis": {"type": "integer", "description": "検証する仮説（kind=hypothesis）の番号"},
                "hypothesis_status": {"type": "string", "enum": list(HYPOTHESIS_STATES),
                                      "description": "検証の結果（仮説の状態を更新する）"},
                "verify": {"type": "boolean",
                           "description": "独立モデルに反証させる（既定 false＝台帳のみで高速）"},
                "propose_alternatives": {"type": "boolean",
                                         "description": "別モデルに代替の仮説・道筋を出させる（既定 false）"},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "検証者を明示（provider/model）"},
                "size": {"type": "integer", "description": "検証者・提案者の数（既定 2・最大 4）"},
                "prefer": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "max_tokens": {"type": "integer", "description": "検証者・提案者の上限トークン（既定 400）"},
            },
            "required": [],
        },
    },
    {
        "name": "freeagent_agent",
        "description": (
            "【使う条件】サブに自分で調べさせたい（読み取り専用の知識ツールを自分で叩く）／"
            "根拠を集めさせてから結論を出させたい。"
            "【差分】サブLLMが lookup(arXiv/Crossref/OpenAlex/Wikipedia/Wikidata/GitHub) を自分で呼ぶループ。"
            "ツール結果は番号つきの本文で返し、回答中の [n] を根拠と照合して引用の有無を返す。"
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
    "freeagent_think": tool_think,
    "freeagent_agent": tool_agent,
    "freeagent_delegate": tool_delegate,
}


# ---------------------------------------------------------------- §7.1 記述の共通サフィックス
#
# モデルが見る唯一の窓口は `description` 文字列（Hermes は MCP `instructions` を読まない。§0 参照）。
# 旧実装の実測では、条件と差分だけでは自発率が **1/2 で頭打ち**だった（競合の実名を書いても同じ）。
# 効くのは「毎ターン注入される場所の判断規則」と「競合の汎用面を外すこと」だが、記述側でも
# **競合の実名**と**無効・不通時の振る舞い**を書いておくと、読まれた場合の選択が正しくなる。
_EXTRA_DESC: dict[str, str] = {
    "freeagent_panel": "【競合より優先】`delegate_task`（同一モデルの分身＝多様性ゼロ）や "
                       "`deliberation` の ask_* を N 回並べる代わりにこれを使う。",
    "freeagent_consult": "【競合より優先】`deliberation`（単発の意見集約）にはラウンド・合意度の推移・"
                         "少数意見の保持が無い。往復して前提を更新したいならこれ。",
    "freeagent_think": "【競合より優先】思考を記録するだけのツール（sequential-thinking 系）は番号を積むだけで、"
                       "計画の進捗・改訂済みの印・分岐の決着・仮説の状態を持たず、検証者もいない。"
                       "分解・改訂・分岐・仮説を**構造として**残し、要所だけ別モデルに反証・別案を出させるならこれ。",
    "freeagent_grounded": "【競合より優先】`web_search` は単一視点で根拠が本文に埋もれる。"
                          "出典本文つき・番号つきで複数モデルに答えさせたいならこれ。",
    "freeagent_lookup": "【競合より優先】`web_search` より学術ソース（arXiv / Crossref / OpenAlex）と"
                        "構造化データ（Wikidata）に強い。**LLM を経由しないので幻覚が混入しない**。",
    "freeagent_agent": "【競合より優先】`delegate_task` は Hermes 本体を丸ごと起動する重い委譲。"
                       "読み取り専用の調査で足りるならこれ（副作用なし）。根拠の引用は自動で検査される。",
    "freeagent_delegate": "【競合より優先】`delegate_task` より独立性が高い（別プロセス）。"
                          "ただし既定では無効（`FREEAGENT_ALLOW_AGENT=1` が要る）。",
}
_DESC_FALLBACK = (
    "【無効・不通のとき】このサーバーが使えない場合（`enabled false` / バックエンド全滅）は、"
    "**存在しないツールを探さず**、`delegate_task` / `web_search` / `web_extract` で回答を完遂し、"
    "実際に応答した独立ソースの件数を回答に明記する（1 件で「複数視点で検討した」と書かない）。"
)
_DESC_PARALLEL = (
    "【並列】1 ターンで他の freeagent_* と同時に呼んでよい（サーバーは同期本体をスレッドへ逃がす）。"
)
_PARALLEL_TOOLS = {"freeagent_models", "freeagent_ask", "freeagent_fanout", "freeagent_panel",
                   "freeagent_lookup", "freeagent_grounded", "freeagent_map", "freeagent_consult",
                   "freeagent_think"}

for _tool in TOOLS:
    _extra = _EXTRA_DESC.get(_tool["name"], "")
    _parallel = _DESC_PARALLEL if _tool["name"] in _PARALLEL_TOOLS else ""
    _tool["description"] = f'{_tool["description"]}{_extra}{_parallel}{_DESC_FALLBACK}'


# ================================================================ §8 表示（content）
#
# content は**人間が読むチャネル**。LLM 向けの指示文を混ぜない（読み手に意味不明な文が出る）。
# 注記・要約は数値から生成し、structuredContent と食い違わせない。

def render(name: str, data: dict) -> str:
    """表示の入口。**選抜ノートを必ず先頭に付ける**。

    実測: 4 体を指定して 3 体で走ったとき、除外理由が content に出ておらず「元から 3 体」と読めた
    （`:提供元` 付きの参照が在庫照合で落ちていた）。失敗・除外を隠さないのは表示の規約。
    """
    body = _render_body(name, data)
    notes = (data.get("selection") or {}).get("notes") if isinstance(data, dict) else None
    if notes and not data.get("error"):
        return "\n".join(f"⚠️ {n}" for n in notes) + "\n" + body
    return body


def _render_body(name: str, data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)[:2000]
    if data.get("error"):
        return f"⚠️ {data['error']}"

    if name == "freeagent_models":
        lines = []
        for row in data.get("providers") or []:
            # ✓ は「資格情報がある」だけでなく「一覧が取れている」ことも表す。取れていないのに ✓ だと、
            # モデル 0 件の理由（プロキシ停止など）が分からない（実測: nous が ✓ 0 件で並んだ）。
            if row.get("ready") and not row.get("error"):
                mark = "✓"
            elif row.get("ready"):
                mark = "⚠"
            else:
                mark = "—"
            if not row.get("ready") and row.get("requires_activation"):
                missing = ", ".join(row.get("missing_settings") or []) or "設定確認"
                note = f"  [無効: {missing}]"
            elif not row.get("ready") and row.get("key_env"):
                note = f"  [{row['key_env']} 未設定 → 検索のみ]"
            else:
                note = ""
            lines.append(f"  {mark} {row['provider']:11} {row['models']:4} モデル / Free {row['free']:3}{note}")
            if row.get("error"):
                lines.append(f"      到達不可: {row['error'][:100]}")
        head = (f"サブLLMバックエンド（{sum(1 for r in data.get('providers') or [] if r.get('ready'))} プロバイダ有効）\n"
                + "\n".join(lines))
        head += (f"\nFree候補 {data.get('free_candidates')} 件（うち今すぐ使用可 {data.get('usable_now')}）"
                 f" / 全 {data.get('total_models')} モデル\n既定: {data.get('default_model')}")
        cooling = data.get("cooling") or {}
        if cooling:
            # 全部並べると 1 行が長すぎて読めない（実測: 55 件で画面が埋まった）。先頭だけ出して件数を添える。
            shown = list(cooling)[:8]
            head += (f"\nクールダウン中 {len(cooling)} 件: " + ", ".join(shown)
                     + (f" … 他 {len(cooling) - len(shown)} 件" if len(cooling) > len(shown) else ""))
        harness = data.get("harness") or {}
        if harness:
            label = {"hermes": "Hermes Agent", "other": "Hermes 以外", "unknown": "判別不能"}.get(
                harness.get("kind"), harness.get("kind"))
            head += f"\nハーネス: {label}（{harness.get('reason')}）"
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
        lines = [f"回答 {data.get('answered')}/{len(data.get('models') or [])} 体"
                 f" / 独立した実モデル {data.get('independent_sources', data.get('answered'))} 体"
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
        timings = data.get("timings") or {}
        late = data.get("timed_out") or []
        head = f"出典 {data.get('citation_count')} 件（{', '.join(data.get('sources') or [])}）"
        if late:
            head += f" / 締め切り {data.get('deadline_s')} 秒に間に合わず {len(late)} 件"
        lines = [head]

        def secs(src: str) -> str:
            t = timings.get(src)
            return f"・{t:.1f} 秒" if isinstance(t, (int, float)) else ""

        for src, res in (data.get("results") or {}).items():
            items = res.get("items") or []
            if res.get("timed_out"):
                lines.append(f"  ⏱ {src}: 締め切りに間に合いませんでした（取得は継続・次回はキャッシュから）")
                continue
            if not items:
                lines.append(f"  × {src}{secs(src)}: {(res.get('error') or '該当なし')[:90]}")
                if res.get("fallback_attempt"):
                    lines.append(f"      代替 datacite も失敗: {truncate(res['fallback_attempt'].get('error') or '', 90)}")
                continue
            acquisition = (res.get("fallback") or {}).get("served_by")
            label = f"{src} → {acquisition}（代替）" if acquisition else src
            lines.append(f"  ✓ {label} ({len(items)} 件{secs(src)})")
            if acquisition:
                lines.append(f"      主系: {truncate(res['fallback'].get('primary_error') or '', 100)}")
            if res.get("attribution"):
                lines.append(f"      {res['attribution']}")
            for item in items[:2]:
                title = item.get("title") or item.get("label") or ""
                lines.append(f"      {truncate(title, 90)} — {item.get('url', '')}")
        return "\n".join(lines)

    if name == "freeagent_grounded":
        lines = [f"根拠 {data.get('evidence_citation_count', data.get('citation_count'))} 件 / 引用付き回答 "
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
        credits = [c for cite in (data.get("citations") or []) for c in
                   ((cite.get("attributions") or []) + ([cite["attribution"]] if cite.get("attribution") else []))]
        if credits:
            lines[1:1] = list(dict.fromkeys(credits))
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

    if name == "freeagent_think":
        ledger = data.get("ledger") or {}
        branches = ledger.get("branches") or []
        head = (f"思考 #{data.get('step') or '-'}（記録 {ledger.get('steps_recorded', 0)} 件"
                f" / 分岐 {len(branches)} / 修正 {len(ledger.get('revisions') or [])}）")
        if data.get("total_thoughts"):
            head += f" / 見積り総数 {data['total_thoughts']}"
            if data.get("total_auto_adjusted"):
                head += "（自動で引き上げ）"
        if data.get("view"):
            head = "【台帳の閲覧（記録なし）】" + head
        lines = [head]
        plan = ledger.get("plan") or []
        if plan:
            prog = ledger.get("plan_progress") or {}
            lines.append(f"計画: {prog.get('done', 0)}/{prog.get('total', len(plan))} 達成")
            for item in plan:
                mark = "✅" if item.get("done") else "□"
                refs = "".join(f" #{x}" for x in (item.get("steps") or []))
                lines.append(f"  {mark} {item.get('id')}. {truncate(item.get('text') or '', 100)}"
                             + (f"（{refs.strip()}）" if refs else ""))
        state_ja = {"open": "未決着", "adopted": "採用", "abandoned": "棄却", "merged": "統合"}
        if branches:
            lines.append("分岐: " + " / ".join(
                f"{b.get('branch_id')}"
                + (f"（#{b['from']} から・" if b.get("from") else "（")
                + f"{state_ja.get(b.get('status') or 'open', b.get('status'))}）"
                for b in branches))
        hyps = ledger.get("hypotheses") or []
        if hyps:
            hyp_ja = {"open": "未検証", "supported": "支持", "refuted": "反証", "inconclusive": "保留"}
            lines.append("仮説: " + " / ".join(
                f"#{h.get('n')} {hyp_ja.get(h.get('status') or 'open', h.get('status'))}"
                + ("（改訂済み）" if h.get("superseded_by") else "") for h in hyps))
        for row in (ledger.get("latest") or [])[-(12 if data.get("view") else 5):]:
            tag = f" [{row['branch_id']}]" if row.get("branch_id") else ""
            tag += " [修正]" if row.get("is_revision") else ""
            if row.get("kind") == "hypothesis":
                tag += " [仮説]"
            elif row.get("kind") == "test" and row.get("tests_hypothesis"):
                tag += f" [#{row['tests_hypothesis']} の検証]"
            elif row.get("kind") == "conclusion":
                tag += " [結論]"
            if row.get("superseded_by"):
                tag += f" [#{row['superseded_by']} で改訂済み]"
            lines.append(f"  • #{row.get('n')}{tag}: {truncate(row.get('text') or '', 140)}")
        verify = data.get("verification")
        if verify:
            counts = verify.get("verdicts") or {}
            conf = verify.get("confidence_mean")
            lines.append(f"検証（独立 {verify.get('answered')}/{len(verify.get('models') or [])} 体）: "
                         f"妥当 {counts.get('妥当', 0)} / 要修正 {counts.get('要修正', 0)}"
                         f" / 根拠不足 {counts.get('根拠不足', 0)}"
                         + (f" / 確信度平均 {conf}" if isinstance(conf, (int, float)) else ""))
            for row in (verify.get("answers") or []):
                if row.get("error"):
                    lines.append(f"  ✗ {row.get('model')}: {truncate(row.get('error') or '', 100)}")
                    continue
                lines.append(f"  ◦ {row.get('model')}: {row.get('verdict') or '判定なし'}")
                if row.get("objection"):
                    lines.append(f"      反証: {truncate(row['objection'], 140)}")
                if row.get("oversight"):
                    lines.append(f"      見落とし: {truncate(row['oversight'], 140)}")
            if verify.get("failed_rows"):
                lines.append(f"  ⚠️ 検証に失敗 {len(verify['failed_rows'])} 体（脱落は隠していません）")
        elif not data.get("view"):
            lines.append("検証なし（台帳のみ。verify=true で独立モデルの反証が付きます）")
        alt = data.get("alternatives")
        if alt:
            lines.append(f"代替案（独立 {alt.get('answered')}/{len(alt.get('models') or [])} 体・"
                         f"{len(alt.get('items') or [])} 件）:")
            for item in (alt.get("items") or [])[:8]:
                lines.append(f"  ◇ {item.get('model')}: {truncate(item.get('text') or '', 160)}")
            for row in (alt.get("failed_rows") or []):
                lines.append(f"  ✗ {row.get('model')}: {truncate(row.get('error') or '', 100)}")
        for note in (data.get("notes") or []):
            lines.append(f"⚠️ {note}")
        lines.append("ℹ️ 検証・代替案は独立モデルによる反証・提案で、合意は正しさの保証ではありません。")
        return "\n".join(lines)

    if name == "freeagent_agent":
        lines = [f"【サブエージェント調査】{data.get('answered')}/{len(data.get('models') or [])} 体が回答"
                 f" / ツール呼び出し {data.get('tool_calls')} 回"
                 f" / 出典 {data.get('citation_count')} 件"
                 f" / 根拠を引用した回答 {data.get('answers_with_citations')} 体"
                 f" / 合意度 {data.get('agreement')}"]
        for row in (data.get("agents") or []):
            if row.get("error"):
                lines.append(f"  • {row['model']}: ✗ {truncate(row['error'], 120)}")
                continue
            mark = "✅" if row.get("cited_ok") else "⚠️ 引用なし"
            lines.append(f"  • {row['model']}（{row.get('steps')}ステップ, {mark}）: "
                         f"{truncate(row.get('answer') or '', 220)}")
            if row.get("unsupported_citations"):
                nums = ", ".join(f"[{n}]" for n in row["unsupported_citations"])
                lines.append(f"      ⚠️ 根拠に無い引用番号: {nums}")
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


# ================================================================ §8.5 失敗時の「次の一手」
#
# この MCP が**無効化されている**（`enabled false`）か**バックエンドが全滅**していても、利用者の
# ターンは続く。そこで「無理に探すな／代替はこれ」を機械可読で返さないと、存在しないツールを探す
# 空振りや同一失敗の再試行でターンと時間を捨てる（旧実装の実測: OFF のまま「使う」と指示してあると
# 存在しないツールを掘り続けた）。**content ではなく structuredContent に置く**（content は人間が
# 読むチャネルで、指示文が混ざると意味不明な文が表示される）。

def error_advice(data: dict) -> dict:
    """失敗の種類に応じて、メイン LLM が次に取るべき行動を返す。"""
    err = as_str(data.get("error"))
    if not err and data.get("unknown_tool"):
        err = "unknown tool"
    advice: dict = {"tool_available": False}

    if data.get("unknown_tool"):
        advice.update({
            "kind": "unknown_tool",
            "advice": ("このツール名は存在しない（サーバーが無効化されているか、提供されていない）。"
                       "tool_search / tool_describe で掘り直さない。多視点が必要なら別の手段で続行する。"),
            "check": "hermes mcp list | grep freeagent-bind  → ✓ enabled / ✗ disabled",
            "reenable": "hermes config set mcp_servers.freeagent-bind.enabled true   # 反映には再起動",
        })
    elif is_env_failure(err) or "Free モデルが 0 件" in err or "モデルが解決できませんでした" in err:
        advice.update({
            "kind": "unavailable_backend",
            "advice": ("推論バックエンドに到達できない。**同じ呼び出しを繰り返さない**（同じ失敗が返る）。"
                       "この MCP に依存せずターンを完遂し、多視点が必要なら下の fallback_tools を使う。"
                       "代替に落ちたら、実際に応答した独立ソースの件数を回答に明記する。"),
            "fallback_tools": ["delegate_task", "web_search", "web_extract"],
            "check": "hermes proxy start  → curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8645/v1/models",
            "reenable": "hermes proxy start（プロキシ）／キー設定（hermes config set "
                        "mcp_servers.freeagent-bind.env.<NAME> '<値>'）→ 反映には再起動",
        })
    elif classify_error(err) == "auth":
        advice.update({
            "kind": "auth",
            "advice": ("キーまたは権限の問題。このプロバイダは自動選抜から外れる（明示指定なら試される）。"
                       "キーを直せば即復帰する。直すまでは他のプロバイダか fallback_tools で続行する。"),
            "fallback_tools": ["delegate_task", "web_search"],
            "check": "env -u PYTHONPATH PYTHONPATH=src python scripts/probe_providers.py",
        })
    elif classify_error(err) == "rate_limited":
        advice.update({
            "kind": "rate_limited",
            "advice": ("レート上限（429）。クールダウンの期限まで待つか、別モデル・別プロバイダへ回す。"
                       "即時の再試行は上限を悪化させるだけで、同じ結果になる。"),
            "fallback_tools": ["web_search", "delegate_task"],
        })
    elif "クールダウン中" in err:
        advice.update({
            "kind": "cooling",
            "advice": ("全候補がクールダウン中。待つか、他のモデルを `models` で明示して呼ぶ。"),
        })
    elif "空応答" in err:
        advice.update({
            "kind": "empty_answer",
            "advice": ("空応答（思考トークンで予算を使い切った可能性）。`max_tokens` を増やして 1 回だけ"
                       "再試行する。増やしても空なら、そのモデルを回答として扱わない。"),
        })
    else:
        advice.update({
            "kind": "error",
            "advice": ("失敗。**同一引数での即時再試行は避ける**（同じ結果になる）。入力を変えるか、"
                       "fallback_tools で続行する。"),
            "fallback_tools": ["web_search"],
        })
    return advice


# ================================================================ §8.6 ハーネス判別（Hermes 以外で起動されたときの警告）
#
# このサーバーは Hermes Agent 前提の部分を持つ: 推論の既定の接続先は `hermes proxy`
# （127.0.0.1:8645 の nous プロバイダ）で、自発利用の設定（SOUL.md・tools.exclude）も Hermes 専用。
# 他のハーネス（Claude Code など）で起動されたら、止めずに**警告だけ**出す。
#
# 判別の実測（`hermes mcp test` にプローブを繋いで確認）:
#   * Hermes の clientInfo は MCP Python SDK 既定の {"name": "mcp", "version": "0.1.0"} で**Hermes 固有でない**
#   * HERMES_* の環境変数は子に渡らない（許可リスト方式: PATH/HOME/TMPDIR/TEMP など）
#   * 設定の `env:` ブロックはそのまま渡る → **明示の目印 FREEAGENT_HARNESS=hermes** を入れるのが唯一確実
#   * 親プロセス名は Windows 11 で wmic が無く取れず、PowerShell/CIM は起動が秒単位で遅い → 使わない
# よって判定は三値: hermes（目印あり／clientInfo に hermes を含む）・other（SDK 既定以外の名前）・
# unknown（名前が "mcp" で目印なし＝目印を入れる前の Hermes がほぼこれ）。
#
# 出し方（重複させない）:
#   other   → initialize の instructions 先頭に注記（Hermes は読まないが他クライアントは読む）
#             ＋ stderr に 1 行 ＋ notifications/message(level=warning)（表示はクライアント次第＝補助）
#             ＋ **最初のツール結果だけ** content に ⚠ 1 行
#   unknown → stderr に 1 行と、最初のツール結果の structuredContent.harness だけ（Hermes 利用者を煩わせない）
#   hermes  → 何も出さない
# FREEAGENT_HARNESS_WARN=0 で警告を止める（判定結果は structuredContent.harness に残す）。

HARNESS_MARKER_ENV = "FREEAGENT_HARNESS"
HARNESS_WARN_ENV = "FREEAGENT_HARNESS_WARN"
_SDK_DEFAULT_CLIENT = "mcp"
_LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
_HARNESS_LOCK = threading.Lock()
_HARNESS: dict = {"info": None, "announced": False, "notified": False, "log_level": "info"}


def detect_harness(params, env=None) -> dict:
    """initialize の params と環境変数からハーネスを判定する（文字列比較だけ・サブプロセスなし）。"""
    env = os.environ if env is None else env
    params = params if isinstance(params, dict) else {}
    client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    name = client.get("name") if isinstance(client.get("name"), str) else ""
    version = client.get("version") if isinstance(client.get("version"), str) else ""
    marker = (env.get(HARNESS_MARKER_ENV) or "").strip()
    info = {"client": name, "client_version": version, "marker": marker}
    if marker:
        kind = "hermes" if marker.lower() == "hermes" else "other"
        return {**info, "kind": kind, "source": "env",
                "reason": f"{HARNESS_MARKER_ENV}={marker}"}
    if "hermes" in name.lower():
        return {**info, "kind": "hermes", "source": "clientInfo", "reason": f"clientInfo.name={name}"}
    if name and name != _SDK_DEFAULT_CLIENT:
        return {**info, "kind": "other", "source": "clientInfo", "reason": f"clientInfo.name={name}"}
    return {**info, "kind": "unknown", "source": "none",
            "reason": (f"clientInfo.name={name or '（なし）'}（MCP SDK の既定値で Hermes と区別できない）"
                       f"・{HARNESS_MARKER_ENV} 未設定")}


def harness_warn_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get(HARNESS_WARN_ENV) or "").strip().lower() not in ("0", "false", "off", "no")


def _harness_backend_is_default() -> bool:
    return PROVIDER_SPECS["nous"]["base_url"].startswith("http://127.0.0.1:8645")


def harness_message(info: dict) -> str:
    """人間向けの 1 段落（stderr・content・ログ通知で共用）。LLM への指示は書かない（規約 3）。"""
    if not info or info.get("kind") == "hermes":
        return ""
    fix = f"hermes config set mcp_servers.freeagent-bind.env.{HARNESS_MARKER_ENV} hermes"
    if info.get("kind") == "unknown":
        return (f"ハーネスを判別できません（{info.get('reason')}）。Hermes Agent で使っているなら "
                f"`{fix}` で目印を入れると判別できます。")
    parts = [f"Hermes Agent 以外のクライアント（{info.get('client') or info.get('marker') or '不明'}）で"
             "動作しています。"]
    if _harness_backend_is_default():
        parts.append("既定の推論先 hermes proxy（127.0.0.1:8645）が無いと nous プロバイダは使えません"
                     "（FREEAGENT_BASE_URL で変更可。OpenRouter / NVIDIA / Hugging Face / Groq / Cloudflare / Gemini はキーと必要なFree-tier確認があれば使えます）。")
    parts.append("自発利用の設定（SOUL.md・tools.exclude・apply_proactive.py）は Hermes 専用です。")
    parts.append(f"この警告は {HARNESS_WARN_ENV}=0 で止められます。")
    return "".join(parts)


def harness_instructions(info: dict) -> str:
    """initialize.instructions の先頭に付ける注記（other のときだけ。他クライアントは instructions を読む）。"""
    if not info or info.get("kind") != "other" or not harness_warn_enabled():
        return PROACTIVE_INSTRUCTIONS
    note = ("【注意】このサーバーは Hermes Agent 向けです。推論の既定の接続先は hermes proxy で、"
            "つながらないときは freeagent_models で使えるプロバイダを確認してから使う。"
            "失敗応答の structuredContent.next_action に従い、同じ呼び出しを繰り返さない。")
    return note + PROACTIVE_INSTRUCTIONS


def harness_on_initialize(params) -> dict:
    """initialize で呼ぶ。判定を保存し、stderr に 1 回だけ書く。initialize 応答（dict）を返す。"""
    info = detect_harness(params)
    announce = False
    with _HARNESS_LOCK:
        _HARNESS["info"] = info
        if not _HARNESS["announced"] and info["kind"] != "hermes" and harness_warn_enabled():
            _HARNESS["announced"] = announce = True
    if announce:
        try:
            sys.stderr.write(f"[freeagent-bind] {harness_message(info)}\n")
            sys.stderr.flush()
        except Exception:
            pass
    offered = params.get("protocolVersion", "") if isinstance(params, dict) else ""
    return {
        "protocolVersion": negotiate_protocol(offered),
        # logging: サーバーが notifications/message を送るなら宣言が MUST（MCP 2025-11-25 Logging）。
        "capabilities": {"tools": {"listChanged": False}, "logging": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": harness_instructions(info),
    }


def harness_set_level(params) -> dict | None:
    """logging/setLevel。不正な値は -32602（仕様どおり）。"""
    level = params.get("level") if isinstance(params, dict) else None
    if level not in _LOG_LEVELS:
        return {"code": -32602, "message": f"invalid log level: {level}"}
    with _HARNESS_LOCK:
        _HARNESS["log_level"] = level
    return None


def harness_log_notification() -> dict | None:
    """notifications/initialized の後に 1 回だけ送るログ通知（other のときだけ）。送らないなら None。"""
    with _HARNESS_LOCK:
        info = _HARNESS["info"]
        if (_HARNESS["notified"] or not info or info.get("kind") != "other" or not harness_warn_enabled()
                or _LOG_LEVELS.index(_HARNESS["log_level"]) > _LOG_LEVELS.index("warning")):
            return None
        _HARNESS["notified"] = True
    return {"jsonrpc": "2.0", "method": "notifications/message",
            "params": {"level": "warning", "logger": "freeagent-bind.harness",
                       "data": {"harness": info["kind"], "client": info.get("client"),
                                "message": harness_message(info)}}}


def harness_first_call(data: dict) -> str:
    """最初のツール結果にだけ判定結果を載せる。content に前置する文字列（other のときだけ ⚠ 1 行）を返す。"""
    with _HARNESS_LOCK:
        info = _HARNESS["info"]
        if not info or _HARNESS.get("first_call_done"):
            return ""
        _HARNESS["first_call_done"] = True
    if info["kind"] == "hermes":
        return ""
    data["harness"] = {k: info[k] for k in ("kind", "client", "source", "reason")}
    if info["kind"] == "other" and harness_warn_enabled():
        return f"⚠️ {harness_message(info)}\n"
    return ""


def harness_status() -> dict | None:
    """freeagent_models が常に返す判定結果（未判定＝initialize 前なら None）。"""
    with _HARNESS_LOCK:
        info = _HARNESS["info"]
    if not info:
        return None
    return {**{k: info[k] for k in ("kind", "client", "client_version", "source", "reason")},
            "warn": harness_warn_enabled()}


def _harness_reset() -> None:
    """テスト用: 判定状態を初期化する。"""
    with _HARNESS_LOCK:
        _HARNESS.clear()
        _HARNESS.update({"info": None, "announced": False, "notified": False, "log_level": "info"})


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
    if not isinstance(params, dict):
        params = {}
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        args = {}
    handler = HANDLERS.get(name)
    if handler is None:
        data = {"error": f"未知のツール: {name}", "unknown_tool": True}
        data["next_action"] = error_advice(data)
        return {"content": [{"type": "text", "text": f"未知のツール: {name}（無効化されている可能性）"}],
                "structuredContent": data, "isError": True}
    try:
        data = handler(args)
    except Exception as exc:  # ツールは絶対に例外を漏らさない
        data = {"error": f"internal error: {type(exc).__name__}: {exc}"}
    if not isinstance(data, dict):
        data = {"error": f"internal error: handler returned {type(data).__name__}"}
    is_error = bool(data.get("error"))
    if is_error:
        try:
            data.setdefault("next_action", error_advice(data))
        except Exception:
            data["next_action"] = {"kind": "error", "advice": "structuredContent.error を確認してください"}
    try:
        text = render(name, data)
    except Exception as exc:
        data = {"error": f"internal render error: {type(exc).__name__}: {exc}",
                "next_action": {"kind": "error", "advice": "structuredContent.error を確認してください"}}
        is_error = True
        text = "表示の生成に失敗しました。structuredContent.error を確認してください。"
    if is_error:
        text = "⚠️ 実行は失敗しました。structuredContent.error を確認してください。\n" + text
    try:
        text = harness_first_call(data) + text   # §8.6（最初の結果だけ）
    except Exception:
        pass
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": data, "isError": is_error}


def _dispatch_batch(items: list) -> None:
    """JSON-RPC batch を 1 行の response array として返す。通知には応答しない。"""
    replies = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("method"), str):
            replies.append({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "invalid request"}})
            continue
        method, msg_id = item["method"], item.get("id")
        params = item.get("params") or {}
        if method in ("notifications/initialized", "initialized"):
            note = harness_log_notification()   # §8.6（other のときだけ・1 回）
            if note:
                replies.append(note)
            continue
        if msg_id is None:
            continue
        if method == "initialize":
            replies.append({"jsonrpc": "2.0", "id": msg_id, "result": harness_on_initialize(params)})
        elif method == "logging/setLevel":
            err = harness_set_level(params)
            replies.append({"jsonrpc": "2.0", "id": msg_id, **({"error": err} if err else {"result": {}})})
        elif method == "tools/list":
            replies.append({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            result = handle_tool_call(params)
            replies.append({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "ping":
            replies.append({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            replies.append({"jsonrpc": "2.0", "id": msg_id,
                            "error": {"code": -32601, "message": f"method not found: {method}"}})
    if replies:
        with WRITE_LOCK:
            write_line(json.dumps(replies, ensure_ascii=False))


def _dispatch_message(msg: dict, pool: ThreadPoolExecutor) -> None:
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if method == "initialize":
        respond(msg_id, harness_on_initialize(params))   # §8.6
    elif method in ("notifications/initialized", "initialized"):
        note = harness_log_notification()   # §8.6（other のときだけ・1 回）
        if note:
            _debug("out", note)
            with WRITE_LOCK:
                write_line(json.dumps(note, ensure_ascii=False))
        return
    elif method == "logging/setLevel":
        err = harness_set_level(params)
        if err:
            respond(msg_id, error=err)
        else:
            respond(msg_id, {})
    elif method == "tools/list":
        respond(msg_id, {"tools": TOOLS})
    elif method == "tools/call":
        pool.submit(lambda i=msg_id, p=params: respond(i, handle_tool_call(p)))
    elif method == "ping":
        respond(msg_id, {})
    elif msg_id is not None:
        respond(msg_id, error={"code": -32601, "message": f"method not found: {method}"})


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
            respond(None, error={"code": -32700, "message": "parse error"})
            continue
        _debug("in", msg)
        if isinstance(msg, list):
            if not msg:
                respond(None, error={"code": -32600, "message": "invalid request"})
            _dispatch_batch(msg)
        elif isinstance(msg, dict):
            _dispatch_message(msg, pool)
        else:
            respond(None, error={"code": -32600, "message": "invalid request"})


def main() -> None:
    """エントリポイント（python -m freeagent_bind / console_scripts から呼ばれる）。"""
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
