#!/usr/bin/env python3
"""hermes-freeagent-bind — Hermes Agent の Free モデルをサブLLMとして並列に走らせ、
外部知識（既定6ソース＋DataCite / OpenAIRE / Zenodo / ROR / DOAJ / npm / crates / CiNii /
OSV / IETF / INSPIRE-HEP / OEIS / HF Hub / Hacker News / Software Heritage / Libraries.io /
Internet Archive / Qiita / OpenReview / MDN / DevDocs / NVD / Terraform Registry / zbMATH）で
根拠づける MCP サーバー。**対象は科学（物理・数学・計算機）とプログラミングまでで、医学・生物学は
対象外**（第6段階で採った生命・医学系ソースは 2026-10-05 に方針変更で削除。§5.19 に記録）。

**モジュラーモノリス**として書く。本体はこの 1 ファイル（§0〜§4・§5 の横断部・§6〜§11）で、
HTTP トランスポート・MCP Resources・ハーネス判別・JSON ヘルパー・表示は分割モジュール
（`transport_http.py` / `resources.py` / `harness.py` / `jsonutil.py`・v0.40.0 /
`rendering.py`・v0.42.0）に切り出してある（テストが patch.object で差し替える名前はこのファイルに
残す。表示は `render` / `_RENDERERS` がここに残り、`_render_*` 本体は `rendering.py`）。
知識ソース 30 種は `sources/<name>.py`（1 ソース 1 ファイル）、プロバイダ 18 社は
`providers/<name>.py`（1 社 1 ファイル）に置く。配布物は今も
**stdio スクリプト 1 本**で完結する（`python server.py` / `runpy.run_path(server.py)` の直接実行は、
冒頭の互換シムがパッケージ版を import して `main()` を呼ぶ。実測: stdio 起動後にネイティブ拡張を
import すると無応答になる環境があるため、起動経路は増やさない）。肥大化は前提なので、増築は
「§区画の追加」（ソースなら `sources/` に 1 ファイル＋レジストリ 1 行）で行い、目次をこの docstring に保つ。
目次が実装とずれたら、それは設計が崩れた合図。

目次
  §0 定数・設定        §1 ユーティリティ     §2 永続ストア（§2.1 クールダウン / §2.2 品質統計 /
                                             §2.3 トレース / §2.4 相談セッション /
                                             §2.5 プロバイダ認証の記憶 / §2.6 思考台帳 /
                                             §2.7 思考台帳の構造（分解・改訂・分岐・仮説））
  §3 プロバイダとモデル（§3.1 Free-tier provider guards / §3.2 チャット向けの選別 /
                    §3.3 一覧の取得＝並列・締め切り・プロセス間キャッシュ）  §4 サブLLM呼び出し    §5 知識バックエンド（§5.2.1 接続の再利用 / §5.8 締め切り / §5.9 DataCite /
                    §5.10 明示許可代替 / §5.11 ホスト予算 / §5.12 OpenAIRE / §5.13 引用統合 / §5.14 Zenodo / §5.15 ROR /
                    §5.16 DOAJ・npm・crates.io / §5.17 CiNii Research（appid 必須） / §5.18 プログラミング・標準 /
                    §5.19 科学（医学・生物学は対象外） / §5.20 プログラミング特化: HN・Software Heritage・Libraries.io /
                    §5.21 Internet Archive（明示指定のみ） / §5.22 日本語の技術記事: Qiita（明示指定のみ） /
                    §5.23 OpenReview（明示指定のみ） / §5.24 MDN・DevDocs（明示指定のみ） /
                    §5.25 NVD・Terraform Registry・zbMATH（明示指定のみ・v0.39.0））
  §6 ツール実装（§6.9 思考台帳 / §6.10 台帳の構造の検証・閲覧・代替案 / §6.11 本文注入番号 /
                 §6.12 panel の統合 / §6.32 受付と報告 / §6.37 記録からの診断）
  §7 ツール定義         §8 表示（content）
  §8.5 失敗時の「次の一手」（structuredContent.next_action）
  §8.6 ハーネス判別（Hermes 以外で起動されたときの警告）   §9 JSON-RPC / stdio
  §10 HTTP (StreamableHTTP) トランスポート（1 プロセスを複数クライアントで共有）
  §11 MCP Resources（使い分け・在庫・鍵・返り値の形を呼ばれたときだけ読ませる）

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

import contextlib
import copy
import datetime
import email.utils
import http.client
import json
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout

# `python server.py` / `runpy.run_path(server.py)` で**直接実行される配布形態**を保つ。
# ソース実装を `sources/` へ分割したので、相対 import の効くパッケージ文脈で動かす必要がある。
# __main__ として起動されたときは、パッケージ版の server モジュールを import して main() を呼ぶ
# （プロセスは増えない・argv と cwd はそのまま・状態は 1 つのモジュールに閉じる）。
if __package__ in (None, ""):
    import importlib as _importlib
    import pathlib as _pathlib

    _pkg_root = str(_pathlib.Path(__file__).resolve().parents[1])
    if _pkg_root not in sys.path:
        sys.path.insert(0, _pkg_root)
    _importlib.import_module("freeagent_bind.server").main()
    raise SystemExit(0)

# ================================================================ §0 定数・設定

SERVER_NAME = "hermes-freeagent-bind"
SERVER_VERSION = "0.50.0"

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
# 推論の同時実行数。**429 を踏まない責任は提供元別ゲート（§3.4）が持つ**ので、ここは
# 「同時に走らせたい本数」の上限として置く。実測（nous へ 12 呼び出し・2026-10-23）: 4 並列 13.13 秒 /
# 8 並列 8.08 秒 / 12 並列 13.78 秒（1 件の最大遅延 4.5→12.2 秒）。膝は 8 本。
MAX_WORKERS = max(1, min(32, _env_int("FREEAGENT_MAX_WORKERS", 8)))
# ツール 1 回の並列実行の**壁時計予算**（秒）。0 で無効。
# MCP クライアントの待ち（実測 300 秒）より短くしておく: 300 秒で切られると**取れていた結果も
# 失われる**。打ち切った分は `dropped: true` の脱落として返す（規約 8・63 と同じ「黙って捨てない」）。
CALL_BUDGET_S = max(0.0, _env_float("FREEAGENT_CALL_BUDGET", 240.0))
# 1 相談あたりの呼び出し上限（暴走・課金の歯止め）。超過したらラウンドを打ち切る。
MAX_CALLS_PER_RUN = max(1, _env_int("FREEAGENT_MAX_CALLS_PER_RUN", 40))
# 1 回のツール呼び出しに参加させるモデル数の 2 段の上限。
#   MAX_SUB_MODELS     … `size` の上限（自動選抜の目標本数。既定 8）
#   MAX_MODELS_PER_CALL … `models` 配列・`models: ["all"]` の展開上限（既定 16）
MAX_SUB_MODELS = max(1, min(32, _env_int("FREEAGENT_MAX_SUB_MODELS", 8)))
MAX_MODELS_PER_CALL = max(1, min(40, _env_int("FREEAGENT_MAX_MODELS_PER_CALL", 16)))
#   MAX_PROMPTS_PER_CALL … fanout の `prompts` の上限（既定 16）。
#     **切り捨ては黙ってやらない**（規約 8・56 と同じ理由で `prompts_dropped` に件数を返す）。
MAX_PROMPTS_PER_CALL = max(1, min(64, _env_int("FREEAGENT_MAX_PROMPTS_PER_CALL", 16)))
#   MAX_MAP_ITEMS … map の `items` の上限（既定 64）。切り捨ては件数を返す（規約 63）。
MAX_MAP_ITEMS = max(1, min(256, _env_int("FREEAGENT_MAX_MAP_ITEMS", 64)))
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
# プロバイダ定義は `providers/<name>.py`（1 社 1 ファイル・v0.16.0）。`SPECS` の挿入順が
# そのまま既定の巡回順で、`PROVIDER_ORDER` の既定値もそこから生成する（手書きの列挙を持たない）。
from . import providers as _providers  # noqa: E402
from . import jsonutil as _jsonutil  # noqa: E402  JSON の入出力（NaN・サロゲート・抽出）
from .jsonutil import deep_copy as _deep_copy  # noqa: E402
from .jsonutil import dumps as _dumps  # noqa: E402

PROVIDER_SPECS: dict[str, dict] = dict(_providers.SPECS)
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
    "FREEAGENT_PROVIDER_ORDER", ",".join(_providers.NAMES)).split(",")
    if n.strip() in PROVIDER_SPECS] or ["nous"]

# 知識バックエンドの識別用 User-Agent。MediaWiki / OpenAlex / Crossref は連絡先入りの UA を求める。
KB_USER_AGENT = os.environ.get(
    "FREEAGENT_USER_AGENT",
    "hermes-freeagent-bind/0.1 (+https://github.com/loosephoto/hermes-freeagent-bind)")
KB_MAILTO = os.environ.get("FREEAGENT_MAILTO", "")  # OpenAlex / Crossref の polite pool 用（任意）
# CiNii Research は利用登録（アプリケーションID）が前提。未設定なら cinii は HTTP を出さずにエラーを返す
# （実測: appid 無しでも 200 が返るが、規約は登録を要求している。§5.17）。
CINII_APPID = os.environ.get("FREEAGENT_CINII_APPID", "").strip()
# Libraries.io は無料の登録 API キーが前提（60 req/min）。未設定なら librariesio は HTTP を出さずにエラーを返す
# （§5.20。cinii と同じ「キー必須の明示ソース」の型）。
LIBRARIESIO_KEY = os.environ.get("FREEAGENT_LIBRARIESIO_KEY", "").strip()
KB_TTL = _env_float("FREEAGENT_KB_TTL", 1800.0)     # 知識取得のメモリ内 TTL（秒）
# **429 の再試行回数**（0 なら再試行しない）。実測: crossref の 429 は共有プールのTransientな
# 拒否で、再試行すると 10 試行すべて成功した（待つ必要なし）。§5.8。
_KB_HTTP_429_RETRIES = max(0, _env_int("FREEAGENT_KB_429_RETRIES", 2))
# 429 の `Retry-After` を尊重して待つ上限（秒）。
_KB_HTTP_429_MAX_WAIT_S = max(0.0, _env_float("FREEAGENT_KB_429_MAX_WAIT", 2.0))
# 同一キーの取得を待つ上限（秒）。既定は締め切り（`KB_DEADLINE`）と同じ。
# 超えたら待たずに自分で取得する（利用者を待たせない。§5.8）。
_KB_INFLIGHT_WAIT_MAX_S = max(0.0, _env_float("FREEAGENT_KB_INFLIGHT_WAIT_MAX", 0.0)
                               or _env_float("FREEAGENT_KB_DEADLINE", 8.0))
KB_TIMEOUT = _env_float("FREEAGENT_KB_TIMEOUT", 20.0)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
# OpenAlex は匿名の検索を提供元側で一時停止することがある（実測: 503 + "Anonymous search is paused"）。
# 無料 API キーを入れると止まらない。単一 work の取得はキー無しでも通る。
OPENALEX_API_KEY = os.environ.get("OPENALEX_API_KEY", "")
# Qiita は未認証でも 60 回/時で動く（実測）。トークンを入れると 1,000 回/時になり、間隔が 60 秒→3.6 秒に縮む。
QIITA_TOKEN = os.environ.get("FREEAGENT_QIITA_TOKEN", "").strip()

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
        line = _dumps(payload)
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



# ---- 合意度と参加者の数え方（v0.42.2・issue #4 / #6 / #7） ----------------------
#
# 実測: `agreement_of` は結論文字列の**表層類似度**なので、回答が `C` や `29` のような
# 短い記号・数値だと「全員一致」でも 0.0〜0.3 になり、逆に同じ前置きを共有しただけの
# 回答は高く出る（20 問 × 4 択で、実票の多数派シェアとの平均絶対差 **0.500**、
# 全会一致 7 問の agreement は 0.167〜1.000）。短答は**完全一致**で数える。
_SHORT_ANSWER_MAX = 24


def _norm_short(text: str) -> str:
    """短答の比較キー。確信度の行を落とし、強調・括弧・句読点・空白を除いた先頭行。"""
    t = text or ""
    lines = [ln for ln in t.splitlines() if "確信度" not in ln and ln.strip()]
    t = lines[0].strip() if lines else ""
    for junk in ("**", "__", "`", "結論:", "結論：", "答え:", "答え："):
        t = t.replace(junk, "")
    t = t.strip(" 　#*>-「」『』（）()[]【】")
    return re.sub(r"[\s、。,.!?！？:：*`\"'「」『』]+", "", t)


def answer_shape(text: str) -> str:
    """回答の型。`exact`（短答）か `similarity`（長文）か。

    短答とみなすのは **1 行だけ**で、正規化後 `_SHORT_ANSWER_MAX` 文字以下、句読点を含まない
    もの（`C` / `29` / `1/216` / `**1.414**` は短答、`結論: … 。` の 2 行組は長文）。
    """
    raw_lines = [ln.strip() for ln in (text or "").splitlines()
                 if ln.strip() and "確信度" not in ln]
    if len(raw_lines) != 1:
        return "similarity"
    line = raw_lines[0]
    n = _norm_short(line)
    if not n or len(n) > _SHORT_ANSWER_MAX:
        return "similarity"
    if any(ch in line for ch in "。、，,"):
        return "similarity"
    return "exact"


_CONSENSUS_LINK = 0.55   # 長文を「同じ答え」とみなす類似度の下限（多数派シェア用）


def consensus_share(texts: list[str]) -> dict:
    """**多数派シェア**（最大クラスタの人数 / 参加者数）を返す（v0.47.0・issue #5 の案3）。

    確信度（自己申告）が正誤と無関係に 100 になる一方、**票の割れ**は実測で正答と単調だった
    （合意 2/3 以上の問題は 17/17 正解）。確信度の**代理**としてこちらを使う。
    長文は表層類似度 `_CONSENSUS_LINK` 以上のものを単連結でまとめてクラスタにする。
    """
    vals = [t for t in (texts or []) if (t or "").strip()]
    n = len(vals)
    if n < 2:
        return {"share": None, "clusters": n, "source": "none", "n": n}
    if all(answer_shape(t) == "exact" for t in vals):
        counts: dict[str, int] = {}
        for t in vals:
            key = _norm_short(t).lower()
            counts[key] = counts.get(key, 0) + 1
        top = max(counts.values())
        return {"share": round(top / n, 3), "clusters": len(counts), "source": "exact", "n": n}
    # 単連結クラスタリング（類似度が閾値以上のものを同じグループに）
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if similarity(vals[i], vals[j]) >= _CONSENSUS_LINK:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    sizes: dict[int, int] = {}
    for i in range(n):
        root = find(i)
        sizes[root] = sizes.get(root, 0) + 1
    return {"share": round(max(sizes.values()) / n, 3), "clusters": len(sizes),
            "source": "similarity", "n": n}


def consensus_note() -> str:
    return ("`consensus_share` は**多数派シェア**（確信度の代理）です。自己申告の `confidence` は"
            "正誤と無関係に高く出るため、割れていないかをこちらで見てください（正しさの確率ではありません）。")


def agreement_detail(texts: list[str]) -> dict:
    """合意度。短答は完全一致、長文は表層類似度（v0.42.2）。

    返り値は `{value, source, votes, n}`。`votes` は短答のときだけ中身が入る
    （表層類似度では「何票が同じ答えか」を言えないため）。v0.47.0 で多数派シェア
    （`share` / `clusters`）も返す（issue #5 の案3）。
    """
    vals = [t for t in (texts or []) if (t or "").strip()]
    if len(vals) < 2:
        return {"value": 0.0, "source": "none", "votes": {}, "n": len(vals),
                "share": None, "clusters": len(vals)}
    cons = consensus_share(vals)
    if all(answer_shape(t) == "exact" for t in vals):
        counts: dict[str, int] = {}
        for t in vals:
            key = _norm_short(t).lower()
            counts[key] = counts.get(key, 0) + 1
        top = max(counts.values())
        return {"value": round(top / len(vals), 3), "source": "exact",
                "votes": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
                "n": len(vals), "share": cons["share"], "clusters": cons["clusters"]}
    return {"value": agreement_of(vals), "source": "similarity", "votes": {}, "n": len(vals),
            "share": cons["share"], "clusters": cons["clusters"]}


def _agreement_note(source: str) -> str:
    """合意度の「数え方」を返す（exact と similarity を混同しない）。"""
    if source == "exact":
        return "合意度は短答の完全一致（記号・数値・短い語）で数えています。正しさの確率ではありません。"
    if source == "similarity":
        return "合意度は結論の表層類似度です（長文のため票では数えられません）。正しさの確率ではありません。"
    return "参加者が 2 体未満のため合意度は計算していません。"


# 確信度は**モデルの自己申告**で、正誤に対して較正されていない（実測: 20 問 × 4 択で
# 正解 99.7 / 誤答 100.0、20 問すべて 97.5〜100.0）。順位付けには使わず、
# 「高い確信度 + 低い合意度」の**矛盾**だけを警告として出す（v0.42.3・issue #5）。
CONFIDENCE_CALIBRATION_NOTE = ("確信度はモデルの自己申告で、正誤に対して較正されていません"
                              "（実測: 20 問 × 4 択で正解 99.7 / 誤答 100.0）。")


def confidence_warning(conf_mean, agreement) -> str:
    """確信度が高いのに合意度が低いときの警告（どちらも無いときは空）。"""
    c = as_float(conf_mean, -1.0)
    a = as_float(agreement, -1.0)
    if c >= 90.0 and 0.0 <= a < 0.5:
        return (f"確信度 {c:g} に対して合意度 {a:g} と割れています。"
                + CONFIDENCE_CALIBRATION_NOTE + "多数決や討議で確かめてください。")
    return ""



# ---- 確信度の較正（v0.42.6・issue #5 の続き） -------------------------------------
#
# 確信度は自己申告で、正誤に対して較正されていない（実測: 正解 99.7 / 誤答 100.0）。
# 真の正誤ラベルは無いので、**討論で立場を変えた**ことを誤答の代理ラベルに使う
# （変えた＝直前の答えを自分で取り下げた、とみなす）。代理ラベルなので**割合だけ**を出し、
# 「誤答率」とは書かない（SPEC §6.52）。
CALIB_FILE = "confidence_calibration.json"
_CALIB_BUCKETS = ((89, "90-100"), (69, "70-89"), (49, "50-69"), (-1, "0-49"))


def confidence_bucket(conf) -> str:
    """確信度（0〜100）を帯に落とす。数値でなければ `"unknown"`。"""
    if not isinstance(conf, (int, float)):
        return "unknown"
    for floor, name in _CALIB_BUCKETS:
        if conf > floor:
            return name
    return "0-49"


def calib_path() -> str:
    return os.path.join(state_dir(), CALIB_FILE)


def _calib_read() -> dict:
    try:
        with open(calib_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return {"version": 1, "models": {}}
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return {"version": 1, "models": {}}
    return data


def note_confidence(ref: str, conf, flipped: bool) -> None:
    """1 件の確信度と「立場を変えたか」を記録する（読んで足してから原子置換）。"""
    if not ref or not isinstance(conf, (int, float)):
        return
    path = calib_path()
    bucket = confidence_bucket(conf)
    try:
        with _file_lock(path):
            data = _calib_read()
            slot = data["models"].setdefault(ref, {"buckets": {}})
            row = slot["buckets"].setdefault(bucket, {"n": 0, "flipped": 0})
            row["n"] = as_int(row.get("n"), 0, 0, 10 ** 9) + 1
            if flipped:
                row["flipped"] = as_int(row.get("flipped"), 0, 0, 10 ** 9) + 1
            slot["updated_at"] = now_ts()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _atomic_write(path, _dumps(data))
    except OSError:
        pass


def confidence_calibration(refs: list[str] | None = None) -> dict:
    """帯ごとの「立場を変えた割合」を返す（`refs` を渡すとその範囲だけ）。"""
    data = _calib_read()
    wanted = {r for r in (refs or []) if r}
    buckets: dict[str, dict] = {}
    for ref, slot in (data.get("models") or {}).items():
        if wanted and ref not in wanted:
            continue
        for bucket, row in ((slot or {}).get("buckets") or {}).items():
            n = as_int((row or {}).get("n"), 0, 0, 10 ** 9)
            if not n:
                continue
            flipped = as_int((row or {}).get("flipped"), 0, 0, 10 ** 9)
            agg = buckets.setdefault(bucket, {"n": 0, "flipped": 0})
            agg["n"] += n
            agg["flipped"] += flipped
    out = {}
    for bucket, agg in sorted(buckets.items(), key=lambda kv: -kv[1]["n"]):
        out[bucket] = {"n": agg["n"], "flipped": agg["flipped"],
                       "flip_rate": round(agg["flipped"] / agg["n"], 3) if agg["n"] else None}
    return {"buckets": out,
            "note": ("「立場を変えた」を誤答の代理ラベルにした割合です（真の正誤ではありません）。"
                     "討論を実行した `consult(debate_depth=deep)` の分だけが貯まります。")}


def _calibration_warning(calib: dict) -> str:
    """高確信度の帯で覆る割合が高いときの警告（閾値未満なら空）。"""
    hb = ((calib or {}).get("buckets") or {}).get("90-100") or {}
    n = as_int(hb.get("n"), 0, 0, 10 ** 9)
    rate = hb.get("flip_rate")
    if n >= 5 and isinstance(rate, (int, float)) and rate >= 0.3:
        return (f"確信度 90-100 の帯でも {rate:.0%} が討論で立場を変えています"
                f"（n={n}）。確信度は自己申告で、正誤の確率ではありません。")
    return ""

def _drop_reason(error: str) -> str:
    """落ちた理由の分類（content で「なぜ減ったか」を言えるようにする）。"""
    t = (error or "").lower()
    if not t:
        return "not_reached"
    if "冷" in t or "cooling" in t:
        return "cooling"
    if "429" in t or "同時実行" in t or "使用中" in t or "busy" in t or "枠" in t:
        return "busy"
    if "空応答" in t or "no output" in t or "empty" in t:
        return "empty"
    if "timeout" in t or "時間切れ" in t or "deadline" in t:
        return "timeout"
    return "error"


def participants_of(requested: list[str], rows: list[dict]) -> dict:
    """要求したモデルのうち、実際に答えた数と落ちた理由（issue #6）。"""
    asked = [m for m in (requested or []) if m]
    answered = {as_str(r.get("model")) for r in (rows or []) if not r.get("error")}
    errors: dict[str, str] = {}
    for r in rows or []:
        if r.get("error"):
            errors.setdefault(as_str(r.get("model")), as_str(r.get("error")))
    dropped = [{"model": m, "reason": _drop_reason(errors.get(m, ""))}
               for m in asked if m and m not in answered]
    return {"requested": len(asked), "answered": len(answered), "dropped": dropped}


def substitutions_of(rows: list[dict]) -> dict:
    """要求したモデルと実際に答えたモデルがズレた件数（issue #7）。

    冷却や枠待ちでサーバーが代替へ回ると `served_by` に別モデルが入る。
    「独立 N 体」を名乗る前にこの数を出す。
    """
    per: dict[tuple[str, str], int] = {}
    total = 0
    for r in rows or []:
        if r.get("error"):
            continue
        total += 1
        asked = as_str(r.get("model"))
        served = as_str(r.get("served_by")) or asked
        if asked and served and asked != served:
            per[(asked, served)] = per.get((asked, served), 0) + 1
    items = [{"requested": a, "served_by": s, "n": n}
             for (a, s), n in sorted(per.items(), key=lambda kv: (-kv[1], kv[0]))]
    count = sum(per.values())
    return {"count": count, "ratio": round(count / total, 3) if total else 0.0,
            "models": items[:8]}


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

_SWEEP_LAST: dict[str, float] = {}
_SWEEP_LOCK = threading.Lock()
# 掃除の間隔（秒）。`note_observation` はサブLLM 呼び出し 1 回ごとに `_stats_save` を通るので、
# 毎回 `os.listdir` すると**並列 8 本の完了ごとにディレクトリ全走査**が直列ロックの内側で走る
# （実測: 50 モデルの状態で 16 回の保存に 32.3ms）。残骸は 60 秒より古いものだけを消すので、
# 走査自体を 60 秒に 1 回に間引いても取りこぼさない。
_SWEEP_INTERVAL_S = 60.0


def _sweep_stale_tmp(path: str, *, min_age_s: float = 60.0) -> int:
    """同じ対象の**書きかけファイル**（`<name>.<pid>.<tid>.tmp`）を掃除する。

    実測: 書き込みの途中でプロセスが落ちると（または os.replace が失敗すると）一時ファイルが
    状態ディレクトリに**永久に残る**。再起動のたびに増えるので、原子置換の副作用として掃除する。
    並行して書いている別スレッドの一時ファイルを壊さないよう、**古いものだけ**を対象にする
    （単一ファイルへの書き込みは `threading.Lock` の内側なので、60 秒前の残骸は必ず放棄済み）。
    走査は**同じディレクトリにつき `_SWEEP_INTERVAL_S` に 1 回**（毎回 listdir しない）。
    """
    directory = os.path.dirname(path) or "."
    with _SWEEP_LOCK:
        last = _SWEEP_LAST.get(directory)
        now = time.monotonic()
        if last is not None and now - last < _SWEEP_INTERVAL_S:
            return 0
        _SWEEP_LAST[directory] = now
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


def _file_mtime(path: str) -> float | None:
    """ファイルの更新時刻（無ければ None）。**別プロセスの更新を検知する**ために使う。"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


@contextlib.contextmanager
def _file_lock(path: str, timeout: float = 10.0):
    """**プロセス間**の排他ロック（同じファイルへの同時更新を 1 本に直列化する）。

    台帳（`thoughts.json`）のロックはプロセス内だけ（`threading`）なので、同じ
    `FREEAGENT_STATE_DIR` を複数プロセスで共有すると **last-writer-wins** になり、
    他プロセスの思考が黙って消える（実測: 4 プロセスの同時書き込みで 5 試行中 3 回で欠落）。
    ロックファイルは対象ファイルの隣（`thoughts.json.lock`）に置く。
    Windows は `msvcrt.locking`、POSIX は `fcntl.flock`。**取れなくても止めない**（best-effort）。
    """
    lock_path = f"{path}.lock"
    handle = None
    acquired = False
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        handle = open(lock_path, "a+b")
        try:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
        except OSError:
            pass
        deadline = time.monotonic() + max(0.1, timeout)
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if handle is not None:
            try:
                if acquired:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass


# ---------------------------------------------------------------- §2.1 クールダウン
# ---------------------------------------------------------------- §2.1/§2.5 クールダウンとプロバイダ認証の記憶
#
# 実装は `stores.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
# テストが直接代入して差し替えるので server.py に残す（移動先からは S._AUTH_LOADED で読む）
_AUTH_LOADED = False

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._COOLDOWN_LOADED で読む）
_COOLDOWN_LOADED = False

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._COOLDOWN_MTIME で読む）
_COOLDOWN_MTIME: float | None = None

from . import stores as _stores  # noqa: E402
globals().update(_stores.EXPORTS)

# ---------------------------------------------------------------- §2.2 品質統計
# ---------------------------------------------------------------- §2.2 品質統計（観測の記録・減衰・順位付け）
#
# 実装は `stats.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
# テストが直接代入して差し替えるので server.py に残す（移動先からは S.RANK_BUDGET で読む）
RANK_BUDGET = _env_on("FREEAGENT_RANK_BUDGET", False)

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._STATS で読む）
_STATS: dict = {"models": {}}

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._STATS_LOADED で読む）
_STATS_LOADED = False

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._STATS_MTIME で読む）
_STATS_MTIME: float | None = None

from . import stats as _stats  # noqa: E402
globals().update(_stats.EXPORTS)

# ---------------------------------------------------------------- §3.6 自動選抜の strategy
# ---------------------------------------------------------------- §3.6/§3.7 選抜ストラテジとタスク分類カスケード
#
# 実装は `routing.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import routing as _routing  # noqa: E402
globals().update(_routing.EXPORTS)

# ---------------------------------------------------------------- §2.3 トレース
#
# 1 行 1 呼び出しのメタデータ（本文なし）。上限を超えたら 1 世代だけ退避する。

# ---------------------------------------------------------------- §2.3.1 呼び出し単位の ID
# ---------------------------------------------------------------- §2.3.1/§2.4 呼び出し ID・トレース記録・相談セッション
#
# 実装は `sessions.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
# テストが直接代入して差し替えるので server.py に残す（移動先からは S._SESSIONS_LOADED で読む）
_SESSIONS_LOADED = False

from . import sessions as _sessions  # noqa: E402
globals().update(_sessions.EXPORTS)

# ---------------------------------------------------------------- §2.6 思考台帳
# ---------------------------------------------------------------- §2.6/§2.7 思考台帳（永続・改訂・分岐・仮説）
#
# 実装は `thoughts.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
# テストが直接代入して差し替えるので server.py に残す（移動先からは S._THOUGHTS_LOADED で読む）
_THOUGHTS_LOADED = False

from . import thoughts as _thoughts  # noqa: E402
globals().update(_thoughts.EXPORTS)

# ================================================================ §3 プロバイダとモデル
# ---------------------------------------------------------------- §3 プロバイダの仕様とモデル参照の正規化
#
# 実装は `provider_specs.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import provider_specs as _provider_specs  # noqa: E402
globals().update(_provider_specs.EXPORTS)

# ---------------------------------------------------------------- §3.3 一覧の取得（並列・締め切り・プロセス間キャッシュ）
# ---------------------------------------------------------------- §3.2/§3.3 モデル一覧の取得・キャッシュ・チャット向け選別
#
# 実装は `catalog.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
# テストが直接代入して差し替えるので server.py に残す（移動先からは S._CATALOG_DISK_MTIME で読む）
_CATALOG_DISK_MTIME: float | None = None

from . import catalog as _catalog  # noqa: E402
globals().update(_catalog.EXPORTS)

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
# **モデルの責任で落ちた**ステータス（統計に残す）。429（提供元の一時制限）・枠待ち・クォータは
# 提供元側の都合なので、モデルの成績としては記録しない（規約 21 の精神）。
_MODEL_FAULT_STATUS = {400, 401, 402, 403, 404, 410, 500, 502, 503, 504}


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


def _extract_text(data: object) -> str:
    """応答から本文を取る。形が想定外（配列・文字列・null・パートの text が null）でも例外にしない。"""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        return ""
    text = msg.get("content")
    if isinstance(text, list):  # 一部プロバイダは content をパート配列で返す
        text = "".join(part["text"] for part in text
                       if isinstance(part, dict) and isinstance(part.get("text"), str))
    return text if isinstance(text, str) else ""


def _is_known_paid_model(provider: str, model: str) -> bool:
    """キャッシュ済み一覧で明確に `free=False` と判明している有料モデルか。"""
    target = as_str(model)
    if not provider or not target:
        return False
    with _MODELS_LOCK:
        rows = (_MODELS_CACHE.get(provider) or {}).get("rows") or []
    for row in rows:
        if isinstance(row, dict) and row.get("id") == target:
            return not bool(row.get("free"))
    if _is_hf(provider) and ":" in target:
        base = target.rsplit(":", 1)[0]
        for row in rows:
            if isinstance(row, dict) and row.get("id") == base:
                return not bool(row.get("free"))
    return False


# ---------------------------------------------------------------- §3.4 提供元別の同時実行ゲート
# ---------------------------------------------------------------- §3.4/§3.5 提供元ゲートとクォータ窓（予約・精算・報告）
#
# 実装は `quota.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import quota as _quota  # noqa: E402
globals().update(_quota.EXPORTS)

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._QUOTA_LOADED で読む）
_QUOTA_LOADED = False

# テストが直接代入して差し替えるので server.py に残す（移動先からは S._QUOTA_MTIME で読む）
_QUOTA_MTIME: float | None = None

# 上流（外部の Free モデル）へ送る直前に秘密を伏せる。**誤爆より見逃しの方が高くつく**ので、
# ---------------------------------------------------------------- §4 サブLLM呼び出し（秘密の伏せ字・キャッシュ・進捗・合流・予算）
#
# 実装は `caller.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import caller as _caller  # noqa: E402
globals().update(_caller.EXPORTS)

# テストが直接代入して差し替えるので server.py に残す（移動先からは S.call_model で読む）
def call_model(ref: str, prompt: str, *, system: str = "", max_tokens: int = 800,
               temperature: float | None = None, kind: str = "ask",
               allow_fallback: bool = True, free_only: bool = True,
               timeout: float | None = None, claims: "_ModelClaims | None" = None) -> dict:
    """1 つのサブLLM呼び出し（**同じツール呼び出しの中の完全な重複を 1 回に畳む**）。

    畳むのは `bound_coalescer()` が張られているとき＝`handle_tool_call` の中だけ。実際の呼び出しは
    `_call_model_uncached`。キーは `(kind, ref, prompt, system, max_tokens, temperature,
    allow_fallback, free_only)` の完全一致で、**モデルが違えば畳まない**（多様性を壊さない）。
    """
    coalescer = _call_coalescer()
    if coalescer is None:
        return _call_model_uncached(ref, prompt, system=system, max_tokens=max_tokens,
                                    temperature=temperature, kind=kind,
                                    allow_fallback=allow_fallback, free_only=free_only,
                                    timeout=timeout, claims=claims)
    key = (kind, ref, prompt, system, max_tokens, temperature, allow_fallback, free_only)
    return coalescer.run(key, lambda: _call_model_uncached(
        ref, prompt, system=system, max_tokens=max_tokens, temperature=temperature, kind=kind,
        allow_fallback=allow_fallback, free_only=free_only, timeout=timeout, claims=claims))

# ---------------------------------------------------------------- §4.0 要求単位のキャンセル
# ---------------------------------------------------------------- §4.0/§4.1 要求単位のキャンセルと公平な共有スケジューラ（並列・合流）
#
# 実装は `parallel.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import parallel as _parallel  # noqa: E402
globals().update(_parallel.EXPORTS)

# ================================================================ §5 知識バックエンド
# ---------------------------------------------------------------- §5 知識バックエンド（取得・キャッシュ・合流）
#
# 実装は `knowledge.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import knowledge as _knowledge  # noqa: E402
globals().update(_knowledge.EXPORTS)

# ---------------------------------------------------------------- §5.99 ソース実装の読み込み
#
# 各ソース実装は `sources/<name>.py`（v0.15.0 で分割）。レジストリが SOURCES と KB_BACKENDS を
# 1 回だけ組み立てる（従来の `SOURCES = (*SOURCES, ...)` の連鎖は廃止）。ソースは共有ヘルパーを
# `S.<name>` の属性参照で呼ぶので、`patch.object(server, ...)` は分割後もそのまま効く。
from . import sources as _kb_sources  # noqa: E402  定数・横断ヘルパーの定義後に読む

globals().update(_kb_sources.EXPORTS)  # `server.kb_*` / `server._ia_query` 等の互換（テストが参照）
KB_BACKENDS.update(_kb_sources.BACKENDS)
SOURCES = tuple(dict.fromkeys((*SOURCES, *_kb_sources.NAMES)))


# ================================================================ §6 ツール実装
# ---------------------------------------------------------------- §6 ツール実装（プロンプト・応答の解析・models/ask/fanout/panel・統合）
#
# 実装は `tools_core.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import tools_core as _tools_core  # noqa: E402
globals().update(_tools_core.EXPORTS)

# ---------------------------------------------------------------- §6.11 本文を実際に注入した引用番号だけを認定する
# ---------------------------------------------------------------- §6.11 回答の組み立て（grounded / map / consult / agent / delegate）
#
# 実装は `answers.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import answers as _answers  # noqa: E402
globals().update(_answers.EXPORTS)

# ---------------------------------------------------------------- §6.9 思考台帳（freeagent_think）
# ---------------------------------------------------------------- §6.9/§6.10 freeagent_think の実装（計画・改訂・分岐・仮説・検証）
#
# 実装は `think_tools.py` に切り出した。名前は EXPORTS 経由でこのモジュールの
# 名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import think_tools as _think_tools  # noqa: E402
globals().update(_think_tools.EXPORTS)

# ================================================================ §7 ツール定義
# テストが直接代入して差し替えるので、この定数は server.py に残す（v0.42.7）。
_ADMIT_MAX_BYTES = _env_int("FREEAGENT_ADMIT_MAX_BYTES", 5_000_000)

# ---------------------------------------------------------------- §6 決定論ツール（admit）
#
# `admit` の実装は `admission.py` に切り出した（v0.42.7）。`tool_admit` / `_render_admit` は
# `EXPORTS` 経由でこのモジュールの名前空間に入る（HANDLERS・_RENDERERS がそのまま使える）。
from . import admission as _admission  # noqa: E402
globals().update(_admission.EXPORTS)

TOOLS: list[dict] = [
    {
        "name": "freeagent_models",
        "description": (
            "【使う条件】使えるモデルを**検索**したい（どのプロバイダにどんな Free モデルがあるか）／"
            "品質統計やクールダウンを確認したいとき。"
            "【差分】`query`（ID の部分一致）と `provider`（" + " / ".join(PROVIDER_ORDER)
            + "）で絞り込む。"
            "返る `ref` は他ツールの `models` 引数にそのまま渡せる（HF は `:提供元` を付けて経路を固定できる）。"
            "`Free候補` は**サブLLM に使えるモデルだけ**（埋め込み・分類器・画像/音声生成は自動選抜から外す。"
            "件数は `non_chat_candidates`）。"
            "**`free_candidates` は在庫・`usable_now` はクールダウンと直近 404/410 を除いた数**で、"
            "差は `gone`（`gone_models` に理由と再確認までの秒数）と `cooling` で説明できる。"
            "各 `ref` の `freshness` に `last_ok_at` / `last_gone_at` / `probed_at` を返す（無い場合は省略）。"
            "【使わない条件】通常は不要（panel/consult 等が自動で選ぶ）。"
            "**個別の ref を並べる代わりに、他ツールの `models` へ `[\"all\"]` を渡せる**"
            "（在庫の全 Free・対話モデルのみが対象）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "モデル ID の部分一致（例: qwen / llama / :free）"},
                "q": {"type": "string", "description": "query の別名"},
                "provider": {"type": "string",
                             "description": " / ".join(PROVIDER_ORDER) + " のいずれか"},
                "limit": {"type": "integer", "description": "表示件数（既定 40・最大 200）"},
                "offset": {"type": "integer", "description": "読み飛ばす件数（ページ送り）"},
                "free_only": {"type": "boolean", "description": "有料モデルを除き、Free だけを対象にする"},
                "all": {"type": "boolean", "description": "検索モードを強制（引数なしで全件を見たいとき）"},
                "probe": {"type": "boolean",
                          "description": "候補を実際に呼んで生存確認し、**呼べないモデルを除外**する"
                                         "（一覧は廃止・未有効を含むため。呼び出しが発生する）"},
                "probe_limit": {"type": "integer", "description": "生存確認する件数（既定 12・最大 40）"},
                "probe_prompt": {"type": "string", "description": "生存確認に使う短い問い"},
                "probe_max_tokens": {"type": "integer", "description": "生存確認の上限トークン（既定 64・最大 512）"},
                "include_gone": {"type": "boolean",
                                 "description": "直近 404/410 で廃止と判定したモデルを行にも含める"
                                                "（既定は伏せる。理由は `gone_models` に常に出る）"},
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
            "【競合より優先】`deliberation` の ask-* を N 回並べるより速い（1 ターンを待たずに同時に走る）。"
            "【差分】速度と失敗の集計（wall と逐次見積り）付き。合意度が要るなら panel。"
            "**`models: [\"all\"]` で在庫の全 Free を束ねられる**（プロンプト 1 本なら全部が 1 回で走る。"
            "合計呼び出しは FREEAGENT_MAX_CALLS_PER_RUN・既定 40 で打ち切る）。"
            "**提供元ごとの同時実行枠を超える呼び出しは 429 を踏まずに順番待ちする**"
            "（枠と枠待ちは `concurrency` に出る）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompts": {"type": "array", "items": {"type": "string"},
                            "maxItems": MAX_PROMPTS_PER_CALL,
                            "description": f"依頼文の配列（最大 {MAX_PROMPTS_PER_CALL} 件。"
                                           f"超過分は切り捨て、件数を `prompts_dropped` で返します）"},
                "prompt": {"type": "string", "description": "単一の依頼文（prompts の代わり）"},
                "items": {"type": "array", "items": {"type": "string"},
                          "description": "prompts の別名"},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "明示するモデル（provider/model）。[\"all\"] を渡すと在庫の"
                                          "全 Free（対話モデルのみ）へ展開する（上限は "
                                          "FREEAGENT_MAX_MODELS_PER_CALL・既定 16）。"
                                          "明示した件数は size で切り落とさない"},
                "size": {"type": "integer",
                         "description": f"自動選択するモデル数（既定 2・最大 {MAX_SUB_MODELS}）"},
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
            "`models: [\"all\"]` で在庫の全 Free を参加させる（`size` は既定 3・最大 "
            + str(MAX_SUB_MODELS) + "）。"
            "**提供元ごとの同時実行枠を超える呼び出しは 429 を踏まずに順番待ちする**"
            "（枠と枠待ち件数は `concurrency` に出る）。"
            "**`refine: true` で MoA の集約**: 参加者の回答を別のモデルが 1 つに統合し `refined` に返す"
            "（呼び出し +1。統合役は参加していないモデルを優先する）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "prompt": {"type": "string", "description": "question の別名"},
                "system": {"type": "string", "description": "各参加モデルへ渡すシステム指示"},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "明示するモデル（provider/model）。[\"all\"] を渡すと在庫の"
                                          "全 Free（対話モデルのみ）へ展開する。"
                                          "明示した件数は size で切り落とさない"},
                "size": {"type": "integer",
                         "description": f"参加モデル数（既定 3・最大 {MAX_SUB_MODELS}）"},
                "prefer": {"type": "array", "items": {"type": "string"}},
                "exclude": {"type": "array", "items": {"type": "string"}},
                "refine": {"type": "boolean",
                           "description": "true で参加者の回答を 1 つに統合する（MoA の集約層）。"
                                          "モデルを 1 回余分に呼ぶので枠を 1 つ使います"},
                "refine_model": {"type": "string",
                                 "description": "統合役のモデル（省略時は**参加していない**モデルの最上位。"
                                                "全員参加しているときは最上位が兼務）"},
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
            "（百科・構造化）／GitHub（コード）を並列に引く。追加の datacite / openaire / zenodo / ror / "
            "doaj / openreview（OA論文・会議の査読状態）／cinii（日本語論文・APPID が前提）／"
            "npm / crates / librariesio / terraform（パッケージ・モジュール検索）／osv・nvd（脆弱性）／"
            "ietf（標準）／inspirehep・oeis・zbmath（物理・数学。zbmath は規約同意が前提）／hfhub（ML）／"
            "mdn・devdocs（Web 標準のリファレンス）／hn（Hacker News の議論）／swh（Software Heritage の"
            "コードアーカイブ）／ia（Internet Archive のアイテム検索。**query は英語の語で**）／qiita（Qiita の日本語技術記事）は sources で明示指定する。"
            "DataCite は自然語の各語を AND 検索、datacite_kind=dataset で研究データ。"
            "fallback=true のときだけ自然語 arXiv検索の失敗/遅延を DataCite で代替する（取得元を明示）。"
            "【使わない条件】単一の事実だけなら web_search が速い。"
            "【資源】ソースの在庫・使い分け・返り値の形は MCP Resources（list_resources / read_resource、"
            "resource://freeagent/sources など）で読める。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "q": {"type": "string", "description": "query の別名"},
                "topic": {"type": "string", "description": "query の別名"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "既定6ソース: wikipedia / wikidata / arxiv（**英文の語で指定**。日本語クエリは0件） / crossref / openalex / github。追加は明示指定: datacite / openaire / zenodo / ror / doaj / npm / crates / cinii（利用者自身の appid を FREEAGENT_CINII_APPID に設定した場合のみ動く。書誌のみ）/ librariesio（19+ 登録簿の横断検索。利用者自身のキーを FREEAGENT_LIBRARIESIO_KEY に設定した場合のみ動く）/ osv（脆弱性。パッケージ名か CVE-/GHSA- 等のID）/ ietf（RFC・Internet-Draft。RFC番号か1語のタイトル一致）/ inspirehep / oeis / hfhub / hn（Hacker News の投稿・議論。帰属表示）/ swh（Software Heritage のリポジトリ検索）/ ia（Internet Archive のアイテム検索。**query は英語の語**で指定する。日本語クエリは IA 側が拒否する。歴史的な計算機資料・マニュアル・書籍が対象で、summary は目録の注記であり抄録ではない）／qiita（Qiita の日本語技術記事。検索構文 tag:Python user:foo created:>2026-01-01。**未認証は 60 回/時**で別の問いは 1 分待ち、FREEAGENT_QIITA_TOKEN を設定すると 1,000 回/時）／openreview（OpenReview の会議論文。**query は英語の語**で指定する。日本語クエリは 0 件。venue に採択・スポットライト・撤回・Desk Reject が出るので、撤回済みを根拠に使う前に確認できる）／mdn（MDN Web Docs の Web 技術ドキュメント。**帰属表示が必須**（Mozilla contributors, CC-BY-SA 2.5）。lang でロケール指定可、内容は HTML/CSS/JS/Web API に限る）／devdocs（DevDocs の docset 索引。doc で docset slug を指定（例: `python~3.14`）。**索引だけで本文は取得しない**＝`metadata_only`）。／nvd（NIST 脆弱性 DB。**query は英語の語**で指定する。osv が ID・パッケージ名しか受けないのに対し**キーワードで CVE を引ける**。説明文と CVSS が付く。無認証 5 req/30s＝既定 6 秒間隔、FREEAGENT_NVD_API_KEY で 0.6 秒）／terraform（Terraform Registry のモジュール検索。説明は公開者の自己申告）／zbmath（数学の書誌・MSC 分類・レビュー。**抄録は返らない**ので metadata_only が主。英語の語が必須で、規約同意として FREEAGENT_ZBMATH_ACK=1 が必要）。対象は科学とプログラミングまでで医学・生物学は含まない"},
                "doc": {"type": "string",
                        "description": "devdocs の docset slug（例: python~3.14）。指定すると docset の"
                                       "索引を引き、省略時は docset 名の一覧を返す"},
                "limit": {"type": "integer", "description": "各ソースの件数（既定 3）"},
                "lang": {"type": "string", "description": "Wikipedia / Wikidata の言語。**wikipedia は省略時に問いから自動**（日本語・中国語・韓国語なら ja、それ以外は en。英語の語（Transformer など）は自動で en になるので、通常は指定しない。**wikidata は既定 ja 固定**（実測で ja の方が当たったため。en にすると機械学習の項目が電気機器に化ける）。明示するとその言語で引く"},
                "github_kind": {"type": "string", "description": "repo / issue / code（code はトークン必須・10 req/分）。code はファイルへのポインタと一致箇所の断片を返し、ライセンス情報を含まない（取り込む前に repo 検索で確認する）。repo: owner/name・path:・language:・filename:・in:file の修飾子を付けると精度が上がる"},
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
                "prompt": {"type": "string", "description": "question の別名"},
                "search_query": {"type": "string",
                                 "description": "根拠を引くときの**検索語**。日本語の質問のまま arXiv / "
                                                "Crossref / openalex / ia を引くと 0 件になりやすいので、"
                                                "英語の語（例: \"large language model scaling\"）を"
                                                "ここに置くと質問文はそのままモデルへ渡る。"
                                                "省略すると question をそのまま検索語にする"},
                "sources": {"type": "array", "items": {"type": "string"},
                            "description": "既定6種（wikipedia / wikidata / arxiv（**英文の語で指定**。日本語クエリは0件）/ crossref / openalex / github）。明示追加: datacite / openaire / zenodo / ror / doaj / npm / crates / cinii（FREEAGENT_CINII_APPID 必須）/ librariesio（FREEAGENT_LIBRARIESIO_KEY 必須。19+ 登録簿の横断検索）/ osv（脆弱性。パッケージ名か CVE-/GHSA- 等のID）/ ietf（RFC・Internet-Draft。RFC番号か1語のタイトル一致）/ inspirehep / oeis / hfhub / hn（Hacker News の投稿・議論）/ swh（Software Heritage のリポジトリ検索）/ ia（Internet Archive のアイテム検索。**search_query は英語の語**で指定する（question は日本語のままでよい）。日本語クエリは IA 側が拒否する。summary は目録の注記であり抄録ではない）／qiita（Qiita の日本語技術記事。**未認証は 60 回/時**、FREEAGENT_QIITA_TOKEN で 1,000 回/時）／openreview（OpenReview の会議論文。**search_query は英語の語**で指定する（question は日本語のままでよい）。日本語クエリは 0 件。venue に採択・撤回・Desk Reject が出る）／mdn（MDN Web Docs の Web 技術ドキュメント。**帰属表示が必須**（Mozilla contributors, CC-BY-SA 2.5）。lang でロケール指定可）／devdocs（DevDocs の docset 索引。doc で docset slug を指定（例: `python~3.14`）。**索引だけで本文が無い**ので grounded の根拠にはならない）。RORは機関属性、Zenodoは説明メタデータで全文ではない。npm / crates / librariesioの説明は登録者の自己申告。hfhub は構造化レコードを1行にまとめた summary_kind=structured_record。**crossref は抄録が無い書誌が多い**（grounded では抄録の有る論文に絞る） / **hn は議論のコメントが本文根拠になる**（story はリンク投稿で本文が無い）。／nvd（NIST 脆弱性 DB。**search_query は英語の語**で指定する（question は日本語のままでよい）。osv が ID・パッケージ名しか受けないのに対し**キーワードで CVE を引ける**。説明文と CVSS が付く＝grounded の根拠に使える。無認証 5 req/30s＝既定 6 秒間隔、FREEAGENT_NVD_API_KEY で 0.6 秒）／terraform（Terraform Registry のモジュール検索。説明は公開者の自己申告）／zbmath（数学の書誌・MSC 分類・レビュー。**抄録は返らない**ので metadata_only が主で grounded の根拠にはなりにくい。英語の語が必須で、規約同意として FREEAGENT_ZBMATH_ACK=1 が必要）。医学・生物学は対象外"},
                "doc": {"type": "string", "description": "devdocs の docset slug（例: python~3.14）。"
                        "指定すると docset の索引を引き、省略時は docset 名の一覧を返す（索引のみで本文は無い）"},
                "fallback": {"type": "boolean", "description": "自然語 arXiv検索の DataCite 代替を明示許可"},
                "datacite_kind": {"type": "string", "enum": ["all", "arxiv", "dataset"]},
                "github_kind": {"type": "string", "description": "github ソースの対象（repo / issue / code。"
                                "code はトークン必須・10 req/分）"},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "除外するモデル（provider/model）"},
                "limit": {"type": "integer"},
                "lang": {"type": "string", "description": "Wikipedia / Wikidata の言語。**wikipedia は省略時に問いから自動**（日本語・中国語・韓国語なら ja、それ以外は en）。**wikidata は既定 ja 固定**（明示すればその言語で引く）"},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "明示するモデル（provider/model）。[\"all\"] を渡すと在庫の"
                                          "全 Free（対話モデルのみ）へ展開する。"
                                          "明示した件数は size で切り落とさない"},
                "size": {"type": "integer",
                         "description": f"参加モデル数（既定 2・最大 {MAX_SUB_MODELS}）"},
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
                "items": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_MAP_ITEMS,
                          "description": f"処理する要素の配列（最大 {MAX_MAP_ITEMS} 件。"
                                         f"超過分は実行せず、件数を `items_dropped` で返します）"},
                "prompts": {"type": "array", "items": {"type": "string"},
                            "description": "items の別名"},
                "instruction": {"type": "string", "description": "各要素へ適用する指示"},
                "model": {"type": "string"},
                "reduce": {"type": "boolean", "description": "true なら全出力を統合する"},
                "reduce_model": {"type": "string"},
                "reduce_max_tokens": {"type": "integer",
                                      "description": "reduce 段の上限トークン（省略時は max_tokens）"},
                "system": {"type": "string", "description": "instruction の別名"},
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
            "**mode=\"review\" + draft は 5 軸（correctness / readability / architecture / security / "
            "performance）の査読**になり、重要度（critical / required / optional / nit）と "
            "`findings_summary.blocking` を返す（語彙外は unknown として blocking に数える）。"
            "**質問は最大 5 件・1 件ずつ既定値つき**で返し、超えた分は `deferred_questions` に残す"
            "（`coverage` で解消/未回答が分かる）。**`basis`（仕様・計画）を渡すと 2 段になる**: "
            "1 段目が仕様適合（compliant / partial / noncompliant）、2 段目が品質（5 軸＋重要度）で、 "
            "`compliance_summary.blocking` と `findings_summary.blocking` のどちらかが立つと差し戻し。"
            "**上流へ送る直前に既知の形の秘密（sk- / ghp_ / AKIA / Bearer / JWT / 秘密鍵）は"
            "自動で伏せる**（件数は `redactions`）。"
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
                "basis": {"type": "string",
                          "description": "仕様・計画（モードが review のとき）。渡すと**仕様適合 → 品質**の"
                                         "2 段査読になり、compliance_summary が返る"},
                "debate_depth": {"type": "string", "description": "normal / deep"},
                "models": {"type": "array", "items": {"type": "string"},
                           "description": "明示するモデル（provider/model）。[\"all\"] を渡すと在庫の"
                                          "全 Free（対話モデルのみ）へ展開する。"
                                          "明示した件数は size で切り落とさない"},
                "size": {"type": "integer",
                         "description": f"参加モデル数（既定 3・最大 {MAX_SUB_MODELS}）"},
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
            "view=true で書かずに台帳を読める（文脈圧縮後の復帰）。**台帳が大きいときは brief=true を併用**すると"
            "現行の道筋だけを軽く読める。"
            "**KANBAN のカードで作業しているなら `card` にカード ID（t_xxxx）を入れる**。card は台帳の取っ手に"
            "なり、**同じ card で呼び続ければ同じ台帳を継続する**（session_id を覚えていなくてよい）。"
            "後から `view=true` + `card` でそのカードの道筋を読み戻せる。**カードの完了・ブロックはこのツールでは"
            "なく `kanban_complete` / `kanban_block` で行う**（この台帳は書き換えない）。"
            "【使わない条件】単発の問いは panel / consult が速い。思考の記録だけなら思考メモ帳系の軽量ツールで足りる。"
            "【注意】返る検証・代替案は仮説であり、合意は正しさの保証ではありません。判断はメインが行います。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "thought": {"type": "string", "description": "今回の思考ステップ（view=true 以外は必須）"},
                "session_id": {"type": "string",
                               "description": "継続する台帳のID（返り値の next_call に同梱。省略で新規）"},
                "view": {"type": "boolean", "description": "台帳を読むだけ（書かない）。session_id か card が必要"},
                "card": {"type": "string", "description": "KANBAN のカード ID（t_xxxx）。同じ card で呼び続けると"
                                                          "同じ台帳を継続する（session_id を覚えていなくてよい）。"
                                                          "view=true + card でそのカードの道筋を読み戻せる。"
                                                          "カードの完了は kanban_complete で行う"},
                "brief": {"type": "boolean",
                          "description": "view と併用。現行の道筋（改訂済み・棄却済み分岐を除く）だけを返す"
                                         "（台帳が大きいときの読み戻し用。省いた件数は omitted に入る）"},
                "question": {"type": "string", "description": "解こうとしている問い（検証者へ渡す文脈）"},
                "kind": {"type": "string", "enum": list(THOUGHT_KINDS),
                         "description": "思考の種類（既定 step。tests_hypothesis 指定時は test）"},
                "plan": {"type": "array", "items": {"type": "string"},
                         "maxItems": THOUGHT_PLAN_MAX,
                         "description": f"サブ目標への分解（最大 {THOUGHT_PLAN_MAX}。再送で計画を改訂。"
                                        f"超えるとエラーになります）。各項目は 2〜5 分で終わる単位で書き、"
                                        f"対象（ファイル・節）と検証方法まで含める（検証できない項目は"
                                        f"項目として数えない）"},
                "subgoal": {"type": "integer",
                            "description": "この思考が扱うサブ目標の番号。**plan 項目の id**（台帳の plan[].id と"
                                           "出力の「N.」に同じ数が出る）で指定する。id に一致する項目が無く"
                                           "1 始まりの位置に当たる場合は、その位置の項目として扱う"
                                           "（差し替えで id が飛び番号になるため）。"},
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
                "prompt": {"type": "string", "description": "task の別名"},
                "models": {"type": "array", "items": {"type": "string"}},
                "size": {"type": "integer", "description": "サブエージェント数（既定 2）"},
                "max_steps": {"type": "integer", "description": "1体あたりのツール呼び出し上限（既定 2）"},
                "main_reply": {"type": "string", "description": "メインの補足（最優先の前提として注入）"},
                "exclude": {"type": "array", "items": {"type": "string"},
                            "description": "除外するモデル（provider/model）"},
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
            "**完了報告の条件は依頼文に自動で前置きする**（既定 `verify_required=true`。サブプロセスは"
            "description を読まないので、呼び出し側が書き足すのを待たない）。返ってきた証拠は"
            "`freeagent_admit(action=\"report\")` に渡すと受理/差し戻しを決定論で判定できる。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "timeout": {"type": "integer", "description": "秒（既定 300）"},
                "verify_required": {"type": "boolean",
                                    "description": "完了報告の条件（検証コマンドと出力の明記）を"
                                                   "依頼文に前置きする（既定 true。false で素の依頼）"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "freeagent_admit",
        "description": (
            "【使う条件】外部（GitHub / チャット / CI / KANBAN の依頼）から来た出来事について、"
            "**誰が・どの強度で頼めるか**を決定論で裁定したいとき（モデル呼び出し 0 回）、または"
            "実行役の完了報告に**証拠があるか**を検査したいとき。action=evaluate は triggers"
            "（source / thread / trigger_id / actor / kind / scope / ticket 等）を裁定し、"
            "認可（Mandate: ポリシー版・強度・一致ルール・card）を返す。既定は observe で、"
            "推測候補は「受け入れるはずだった」を would_accept として記録だけする。"
            "同じ (source, thread, trigger_id) の再実行は裁定を replay して二重起票しない。"
            "action=report は ready に evidence.commands（cmd / result）、blocked / failed / dropped に "
            "reason を要求し、無ければ差し戻す。action=list_recent は記録の読み戻し。"
            "【使わない条件】作業の状態（作業中／レビュー待ち／完了）を管理したいなら KANBAN"
            "（kanban_*）を使う。このツールは状態機械を持たず、並行実行・資源割当は KANBAN の "
            "dispatch が持ち主。合意や意見そのものが欲しいなら panel / consult。"
            "【競合より優先】「頼まれたか」「完了を名乗る資格があるか」は**モデルに聞かずに決める**ので、"
            "ここで裁定する（consult に聞くと毎回モデルの判断と待ち時間を払うことになる）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "evaluate（裁定・既定）/ report（報告の検査）/ list_recent（記録の読み戻し）"},
                "triggers": {"type": "array", "items": {"type": "object"},
                             "maxItems": ADMIT_MAX_TRIGGERS,
                             "description": f"裁定する出来事（最大 {ADMIT_MAX_TRIGGERS} 件。超過はエラーで返す）。"
                                            "各要素: source / thread / trigger_id（必須）/ actor / kind"
                                            "（mention・direct・comment・ci）/ scope / is_bot / ticket"
                                            "（既存カード ID）/ card / assumptions（既定値の記録）"},
                "report": {"type": "object",
                           "description": "action=report のときの報告。{kind: progress|waiting|ready|"
                                          "blocked|failed|dropped, evidence: {commands: [{cmd, result}]}, "
                                          "reason, ticket}"},
                "disposition": {"type": "string",
                                "description": "list_recent の絞り込み（accept / would_accept / ignored）"},
                "limit": {"type": "integer", "description": "list_recent の件数（既定 20・最大 200）"},
            },
            "required": [],
        },
    },

    {
        "name": "freeagent_diagnose",
        "description": (
            "【使う条件】「なぜ落ちたか」「どのモデルが遅いか」を**事後の記録から決定論で**知りたいとき"
            "（モデル呼び出し 0 回・ネットワーク 0 回）。traces（1 行 1 呼び出しのメタデータ）を集計し、"
            "成功率・失敗理由の内訳・上位のエラー文・遅延の分位（p50/p90/p95/p99）・モデル別の遅さ・"
            "直近の失敗・次の一手（`advice`）を返す。`days` で窓（既定 7 日）、`kind` / `model` で絞れる。"
            "`run` を渡すと**1 回の呼び出しの内訳**（panel の 3 体中 2 体が落ちた等）に絞れる。"
            "【使わない条件】いま使えるモデルの一覧や Free 判定が要るなら `freeagent_models`。"
            "実際に意見や答えが欲しいなら `panel` / `consult` / `ask`。記録に無いことは返さない"
            "（推測で原因を書かない）。"
            "【競合より優先】ログを手で開いたり、失敗の理由を推測で説明したりする代わりにこれを使う。"
            "`error_class` は 429/timeout/空応答/廃止/認証/その他に固定してあるので、"
            "「直せる失敗」と「待つしかない失敗」を分けて読める。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer",
                         "description": "集計する窓（日・既定 7・1〜90）。狭いほど直近の傾向が出る"},
                "kind": {"type": "string",
                         "description": "呼び出し種別で絞る（ask / panel / fanout / map / consult / think / "
                                        "eval / probe など traces の kind と同じ語）"},
                "model": {"type": "string",
                          "description": "モデル（provider/model）で絞る。ref か served_by の部分一致"},
                "run": {"type": "string",
                        "description": "呼び出し単位の ID（`r…`）で絞る。1 回の panel / consult の内訳を見るときに使う"},
                "limit": {"type": "integer", "description": "上位エラー・遅いモデル・直近失敗の件数（既定 5・1〜20）"},
            },
            "required": [],
        },
    },
]


# ---------------------------------------------------------------- §6.38 記録からの診断
#
# 外部 OSS（`obra/superpowers` の `diagnosing-*`、`affaan-m/ECC` の観測層）の「事後に証拠で説明する」型を、
# **モデル呼び出し 0 回**で写したもの。traces（1 行 1 呼び出し）は既に残っているのに集計する手段が無く、
# 失敗の切り分けを毎回手で開いていた（実測 2026-11-02: 直近 3 日 44 件中 16 件が失敗・全部 `rate_limited`
# だが、その事実は traces を開かないと分からない）。**記録に無いことは「無い」と返す**（推測しない）。

# ---------------------------------------------------------------- §6 決定論ツール（diagnose）
#
# `diagnose` の実装は `diagnostics.py` に切り出した（v0.42.7）。
from . import diagnostics as _diagnostics  # noqa: E402
globals().update(_diagnostics.EXPORTS)

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
    "freeagent_admit": tool_admit,
    "freeagent_diagnose": tool_diagnose,
}


# ---------------------------------------------------------------- §7.1 記述の共通サフィックス
#
# モデルが見る唯一の窓口は `description` 文字列（Hermes は MCP `instructions` を読まない。§0 参照）。
# 旧実装の実測では、条件と差分だけでは自発率が **1/2 で頭打ち**だった（競合の実名を書いても同じ）。
# 効くのは「毎ターン注入される場所の判断規則」と「競合の汎用面を外すこと」だが、記述側でも
# **競合の実名**と**無効・不通時の振る舞い**を書いておくと、読まれた場合の選択が正しくなる。
_EXTRA_DESC: dict[str, str] = {
    "freeagent_models": "モデルの**一覧**が要るだけの場面では `panel` / `consult` の自動選抜で足りる。"
                        "どの Free モデルが存在するかを自分で引きたいときはこちら。",
    "freeagent_ask": "`delegate_task`（Hermes 本体を別プロセスで走らせる重い委譲）ほどではなく、"
                     "1 つの独立意見だけで足りるならこちらが速い。",
    "freeagent_map": "要素ごとに別モデルへ並列に投げる必要が無いなら `panel` で足りる。",
    "freeagent_panel": "`delegate_task`（同一モデルの分身＝多様性ゼロ）や "
                       "`deliberation` の ask-* を N 回並べる代わりにこれを使う。",
    "freeagent_consult": "`deliberation`（単発の意見集約）にはラウンド・合意度の推移・"
                         "少数意見の保持が無い。往復して前提を更新したいならこれ。",
    "freeagent_think": "思考を記録するだけのツール（sequential-thinking 系）は番号を積むだけで、"
                       "計画の進捗・改訂済みの印・分岐の決着・仮説の状態を持たず、検証者もいない。"
                       "分解・改訂・分岐・仮説を**構造として**残し、要所だけ別モデルに反証・別案を出させるならこれ。",
    "freeagent_grounded": "`web_search` は単一視点で根拠が本文に埋もれる。"
                          "出典本文つき・番号つきで複数モデルに答えさせたいならこれ。",
    "freeagent_lookup": "`web_search` より学術ソース（arXiv / Crossref / OpenAlex）と"
                        "構造化データ（Wikidata）に強い。**LLM を経由しないので幻覚が混入しない**。",
    "freeagent_agent": "`delegate_task` は Hermes 本体を丸ごと起動する重い委譲。"
                       "読み取り専用の調査で足りるならこれ（副作用なし）。根拠の引用は自動で検査される。",
    "freeagent_delegate": "`delegate_task` より独立性が高い（別プロセス）。"
                          "ただし既定では無効（`FREEAGENT_ALLOW_AGENT=1` が要る）。",
}
# 【使わない条件】…「この場面では別の手段が速い/軽い」。全ツールに必須（規約 5 / SPEC §7）。
_NOT_USE_DESC: dict[str, str] = {
    "freeagent_models": "通常は不要（panel / consult 等が自動で選ぶ）。",
    "freeagent_ask": "単発の事実確認は `web_search` や `lookup` が速い。",
    "freeagent_fanout": "1 つの問いに少数の視点なら `panel` が十分。多数決の記録が要らないなら `ask`。",
    "freeagent_panel": "1 モデルで足りるなら `ask`。出典つきの回答が要るなら `grounded`。",
    "freeagent_lookup": "単一の事実だけなら `web_search` が速い。",
    "freeagent_grounded": "根拠の引用が要らないなら `panel`。引用の番号が要らないなら `ask` で足りる。",
    "freeagent_map": "要素が数個だけなら `panel` / `ask` で足りる。",
    "freeagent_consult": "前提の往復が要らない単発の意見なら `panel`。",
    "freeagent_think": "1 問 1 答・単純な事実確認・雑談では使わない（1 ターンが無駄に伸びる）。",
    "freeagent_agent": "読み取り専用の調査で足りず、書き込みや実作業が必要なときは `delegate` を使う。",
    "freeagent_delegate": "軽い委譲で足りるなら `agent` / `panel`。",
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


_CJK_HEAD_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def _dense_head(text: str, limit: int = 140) -> str:
    """**頭 `limit` 字だけ**和文に隣接する ASCII 空白を落とす（遅延カタログの切り詰め対策）。

    Hermes はツール一覧（遅延カタログ）の説明を**先頭 100 字前後で切る**（実測 2026-11-11:
    `【常用】2 段…` のように空白の直後で切れ、用途と競合名が一覧から読めない）。
    切れる領域だけ「2 段」→「2段」「Hermes 本体」→「Hermes本体」のように詰めると、
    同じ文字数で多くの語が見える。`limit` の外は従来どおり（README と同じ表記）。
    """
    if len(text) <= limit:
        head, tail = text, ""
    else:
        head, tail = text[:limit], text[limit:]
    out: list[str] = []
    for i, ch in enumerate(head):
        if ch == " ":
            prev = out[-1] if out else ""
            nxt = head[i + 1] if i + 1 < len(head) else (tail[:1] or "")
            if _CJK_HEAD_RE.match(prev or "") or _CJK_HEAD_RE.match(nxt or ""):
                continue
        out.append(ch)
    return "".join(out) + tail


def _desc_assemble(base: str, name: str) -> str:
    """description を【使う条件】【使わない条件】【競合より優先】…の順に組み立てる。

    **マーカーの重複を組まない**（同じ見出しを 2 回出さない）。各マーカーの本文は
    1 か所だけを正とする:
      * 【使う条件】はツール定義の直書きにだけ書く。
      * 【使わない条件】は `_NOT_USE_DESC` にだけ書く。
      * 【競合より優先】は**直書きと `_EXTRA_DESC` の両方に書かない**。
        直書きに既有ならそれを優先し、無ければ `_EXTRA_DESC` を使う。
    値が空ならマーカーごと省く（空のマーカーを残さない）。
    """
    parts = [base]
    not_use = _NOT_USE_DESC.get(name, "")
    if not_use and "【使わない条件】" not in base:
        parts.append("【使わない条件】" + not_use)
    extra = _EXTRA_DESC.get(name, "")
    if extra and "【競合より優先】" not in base:
        parts.append("【競合より優先】" + extra)
    if name in _PARALLEL_TOOLS:
        parts.append(_DESC_PARALLEL)
    parts.append(_DESC_FALLBACK)
    # 頭 140 字を凝縮する（遅延カタログの切り詰めで用途が読めなくなる対策・v0.41.0）。
    # 圧縮で後ろの語が前に滑るので、**不動点まで繰り返す**（1 回だと、もと 140 字の外にあった
    # 空白が 140 字以内に滑り込んで残ることがある＝fanout の「panel。」で実測）。
    out = "".join(parts)
    for _ in range(3):
        collapsed = _dense_head(out, 140)
        if collapsed == out:
            break
        out = collapsed
    return out


for _tool in TOOLS:
    _tool["description"] = _desc_assemble(_tool["description"], _tool["name"])


# ================================================================ §8 表示（content）
#
# 人間向け表示（`content`）は `rendering.py` に切り出した（v0.42.0 の分割。references/module-split.md）。
# テストが差し替える `render` / `_RENDERERS` はこのファイルに残す（移動先から `S.` で参照される）。
from . import rendering as _rendering  # noqa: E402
globals().update(_rendering.EXPORTS)

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
_RENDERERS = {
    "freeagent_models": _render_models,
    "freeagent_ask": _render_ask,
    "freeagent_fanout": _render_fanout,
    "freeagent_panel": _render_panel,
    "freeagent_lookup": _render_lookup,
    "freeagent_grounded": _render_grounded,
    "freeagent_map": _render_map,
    "freeagent_consult": _render_consult,
    "freeagent_think": _render_think,
    "freeagent_agent": _render_agent,
    "freeagent_delegate": _render_delegate,
    "freeagent_admit": _render_admit,
    "freeagent_diagnose": _render_diagnose,
}
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



from . import harness as _harness  # noqa: E402  §から切り出し（ハーネス判別（Hermes 以外で起動されたときの警告））
globals().update(_harness.EXPORTS)  # `server.<name>` の互換（テストが参照する）
from . import resources as _resources  # noqa: E402  §から切り出し（MCP Resources（知識のオンデマンド公開）と completion/complete）
globals().update(_resources.EXPORTS)  # `server.<name>` の互換（テストが参照する）


# ================================================================ §9 JSON-RPC / stdio

def write_line(line: str) -> None:
    """1 行を **必ず UTF-8 バイト列として** stdout へ書く。

    MCP 仕様は UTF-8 固定。`sys.stdout.write` はロケール依存で、日本語 Windows では
    クライアントが環境変数を整えて起動すると cp932 になる（実測）。すると日本語を含む JSON が
    クライアント側の UTF-8 デコードで壊れ、`Failed to parse JSONRPC message from server` として
    **応答が黙って捨てられ**、接続が connect_timeout まで固まる。
    """
    data = (line + "\n").encode("utf-8", "replace")      # 孤立サロゲートで例外にしない（応答を失わない）
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
        write_line(_dumps(out))


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
    # §3.7 の層は**呼び出し側スレッド**に置く（選抜は handler の中の同じスレッドで走る。
    # `run_parallel` のワーカーへは層を渡さない＝層は「誰を選ぶか」だけに効き、実行の分岐は作らない）。
    previous_tier = _routing_scope(route_tier(as_str(name), _routing_text_chars(args),
                                              as_int(args.get("max_tokens"), 0, 0, 1000000)))
    request_id = getattr(_REQ_LOCAL, "id", None)
    try:
        # `budget_scope()` は**この呼び出し 1 回ぶん**の `Deadline` を張る（入れ子の
        # `run_parallel` / refine / reduce / 討論 2 ラウンド目が満額を新しく作らない）。
        # 要求 ID に紐づけておくと、`notifications/cancelled` で**未開始のジョブを投入させない**。
        with run_scope(new_run_id(), as_str(name)), progress_for(params), \
                bound_coalescer(), budget_scope() as deadline:
            _register_active(request_id, deadline)
            try:
                data = handler(args)
            finally:
                _unregister_active(request_id, deadline)
    except Exception as exc:  # ツールは絶対に例外を漏らさない
        data = {"error": f"internal error: {type(exc).__name__}: {exc}"}
    finally:
        _routing_scope(previous_tier)
    if not isinstance(data, dict):
        data = {"error": f"internal error: handler returned {type(data).__name__}"}
    is_error = bool(data.get("error"))
    if is_error:
        try:
            data.setdefault("next_action", error_advice(data))
        except Exception:
            data["next_action"] = {"kind": "error", "advice": "structuredContent.error を確認してください"}
    render_failed = False
    try:
        text = render(name, data)
    except Exception as exc:
        # **表示の失敗で構造化された結果を捨てない**（実測: 描画の KeyError で、取れていた
        # agents ごと `{error}` に潰れた）。`isError` は立てたまま、本文だけ差し替える。
        data["render_error"] = f"{type(exc).__name__}: {exc}"
        is_error = True
        render_failed = True
        text = (f"⚠️ 表示の生成に失敗しました（{type(exc).__name__}: {exc}）。"
                "結果は structuredContent に入っています。\n"
                + truncate(_dumps(data), 2000))
    if is_error and not render_failed:
        # `error` を持たない失敗（描画だけ）に「error を確認してください」と書かない。
        text = "⚠️ 実行は失敗しました。structuredContent.error を確認してください。\n" + text
    coalesced = as_int(data.get("coalesced_calls"), 0, 0, 1000000) if not is_error else 0
    if coalesced:
        # 結果は全件返っているが、**上流へは 1 回しか流していない**（規約 8: 隠さない）。
        text = text + (f"\n※ 同じ呼び出しが {coalesced} 件あり、上流へは 1 回だけ流しました"
                       "（同じ結果を配っています）")
    try:
        text = harness_first_call(data) + text   # §8.6（最初の結果だけ）
    except Exception:
        pass
    return {"content": [{"type": "text", "text": text}],
            "structuredContent": data, "isError": is_error}


def _replies_for(msg, marker=None) -> list:
    """メッセージ 1 件分の応答（通知を含む）。**メソッド表はここ 1 か所**に置く。

    stdio の `_dispatch_batch` / `_dispatch_message` と HTTP（§10）が同じ表を使う。表を分けると、
    メソッドやツールを足したときに片方だけ直して **stdio と HTTP で挙動が食い違う**（規約 20 と同型の事故）。
    id を持たないメッセージ（通知）には応答を返さない（MCP 仕様）。ただし `notifications/initialized`
    の直後に送る §8.6 のログ通知だけは「通知」として返す。
    """
    if not isinstance(msg, dict) or not isinstance(msg.get("method"), str):
        return [{"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}]
    method, msg_id = msg["method"], msg.get("id")
    params = msg.get("params") or {}
    if method in ("notifications/cancelled", "notifications/cancel", "cancelled"):
        # **キャンセルは応答しない**（通知）。該当要求の予算を打ち切り、未開始のジョブを投入させない。
        cancel_request(params.get("requestId") if isinstance(params, dict) else None)
        return []
    if method in ("notifications/initialized", "initialized"):
        note = harness_log_notification()   # §8.6（other のときだけ・1 回）
        return [note] if note else []
    if msg_id is None:
        return []
    if method == "initialize":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": harness_on_initialize(params, marker=marker)}]
    if method == "logging/setLevel":
        err = harness_set_level(params)
        return [{"jsonrpc": "2.0", "id": msg_id, **({"error": err} if err else {"result": {}})}]
    if method == "tools/list":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}]
    if method == "tools/call":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": handle_tool_call(params)}]
    # §11 Resources。**表は 1 か所**（stdio と HTTP が同じ表を通る。規約 38）。
    if method == "resources/list":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": {"resources": resource_list()}}]
    if method == "resources/read":
        uri = params.get("uri") if isinstance(params, dict) else None
        text = resource_read(uri) if uri else None
        if text is None:
            # MCP の Resource not found は -32002（未知の URI を空文字で成功にしない）。
            return [{"jsonrpc": "2.0", "id": msg_id,
                     "error": {"code": -32002, "message": f"resource not found: {uri}"}}]
        return [{"jsonrpc": "2.0", "id": msg_id,
                 "result": {"contents": [{"uri": uri, "mimeType": RESOURCE_MIME, "text": text}]}}]
    if method == "completion/complete":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": completion_complete(params)}]
    if method == "ping":
        return [{"jsonrpc": "2.0", "id": msg_id, "result": {}}]
    return [{"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"method not found: {method}"}}]


def _emit(replies: list) -> None:
    """応答の配列を 1 行で stdout へ書く（batch の応答形）。"""
    if not replies:
        return
    with WRITE_LOCK:
        write_line(_dumps(replies))


def _stdio_progress_maker(token=None):
    """stdio の送り手: `notifications/progress` を stdout へ 1 行（UTF-8・規約 14）。"""

    def sender(note):
        with WRITE_LOCK:
            write_line(_dumps(note))

    return sender


def _dispatch_batch(items: list) -> None:
    """JSON-RPC batch を 1 行の response array として返す。通知には応答しない。"""
    replies = []
    for item in items:
        replies.extend(_replies_for(item))
    _emit(replies)


def _dispatch_message(msg: dict, pool: ThreadPoolExecutor) -> None:
    """単一メッセージ。tools/call だけはプールで並列に流す（他のメソッドは表を共用）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    if method == "tools/call" and msg_id is not None:
        params = msg.get("params") or {}
        def _run_call(i=msg_id, p=params):
            # 要求 ID を張る（この呼び出しが `notifications/cancelled` の対象になる）。
            with request_scope(i), bound_progress(_stdio_progress_maker):
                respond(i, handle_tool_call(p))

        pool.submit(_run_call)
        return
    for reply in _replies_for(msg):
        _debug("out", reply)
        with WRITE_LOCK:
            write_line(_dumps(reply))


def serve() -> None:
    # ロケール非依存にする（PYTHONUTF8 の有無で cp932 に落ちる環境があるため）
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    pool = ThreadPoolExecutor(max_workers=MAX_WORKERS * 2)
    try:
        for raw in iter_stdin_lines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, RecursionError):
                respond(None, error={"code": -32700, "message": "parse error"})
                continue
            _debug("in", msg)
            if isinstance(msg, list):
                if not msg:
                    respond(None, error={"code": -32600, "message": "invalid request"})
                with bound_progress(_stdio_progress_maker):
                    _dispatch_batch(msg)
            elif isinstance(msg, dict):
                _dispatch_message(msg, pool)
            else:
                respond(None, error={"code": -32600, "message": "invalid request"})
    finally:
        # Drain already-dispatched calls before interpreter exit starts closing
        # the optional scheduler or the stdlib ThreadPoolExecutor registry.
        pool.shutdown(wait=True)
        _drain_parallel_futures()


HTTP_TRANSPORT_ENV = "FREEAGENT_TRANSPORT"
HTTP_HOST_ENV = "FREEAGENT_HTTP_HOST"
HTTP_PORT_ENV = "FREEAGENT_HTTP_PORT"
HTTP_PATH_ENV = "FREEAGENT_HTTP_PATH"
HTTP_TOKEN_ENV = "FREEAGENT_HTTP_TOKEN"
HTTP_DEFAULT_HOST = "127.0.0.1"
HTTP_DEFAULT_PORT = 8765
HTTP_DEFAULT_PATH = "/mcp"
HTTP_SESSION_HEADER = "mcp-session-id"
HTTP_SESSION_TTL = _env_float("FREEAGENT_HTTP_SESSION_TTL", 1800.0)
HTTP_SSE_KEEPALIVE = _env_float("FREEAGENT_HTTP_KEEPALIVE", 15.0)
HTTP_MAX_BODY = max(65536, _env_int("FREEAGENT_HTTP_MAX_BODY", 4 * 1024 * 1024))
HTTP_MAX_SESSIONS = max(1, _env_int("FREEAGENT_HTTP_MAX_SESSIONS", 64))

from . import transport_http as _transport_http  # noqa: E402  §から切り出し（HTTP (StreamableHTTP) トランスポート）
globals().update(_transport_http.EXPORTS)  # `server.<name>` の互換（テストが参照する）


def main(argv=None) -> None:
    """エントリポイント。既定は stdio（従来どおり）。`--transport http` / `FREEAGENT_TRANSPORT=http` で HTTP。"""
    config = http_config(sys.argv[1:] if argv is None else argv)
    if config["transport"] == "http":
        serve_http(config)
        return
    try:
        serve()
    except (KeyboardInterrupt, BrokenPipeError):
        pass


if __name__ == "__main__":
    main()
