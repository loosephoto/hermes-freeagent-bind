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
SERVER_VERSION = "0.42.9"

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


def agreement_detail(texts: list[str]) -> dict:
    """合意度。短答は完全一致、長文は表層類似度（v0.42.2）。

    返り値は `{value, source, votes, n}`。`votes` は短答のときだけ中身が入る
    （表層類似度では「何票が同じ答えか」を言えないため）。
    """
    vals = [t for t in (texts or []) if (t or "").strip()]
    if len(vals) < 2:
        return {"value": 0.0, "source": "none", "votes": {}, "n": len(vals)}
    if all(answer_shape(t) == "exact" for t in vals):
        counts: dict[str, int] = {}
        for t in vals:
            key = _norm_short(t).lower()
            counts[key] = counts.get(key, 0) + 1
        top = max(counts.values())
        return {"value": round(top / len(vals), 3), "source": "exact",
                "votes": dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))),
                "n": len(vals)}
    return {"value": agreement_of(vals), "source": "similarity", "votes": {}, "n": len(vals)}


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
#
# 前提: 上限に当たっても 429 は「結果が返った後」に観測される。よって 429 は回答の破棄理由ではなく
# **会計イベント**として扱い、Retry-After を尊重した期限を覚えて、その間は代替モデルへ回す。
# ディスクに置く理由（実測）: Free 提供が終わったモデルは 404 を返し続けるが、記憶がプロセス内だけだと
# **再起動のたびに選び直して空振り**する。ディスク側が新しい期限を持つときだけ採用する。

_COOLDOWN: dict[str, dict] = {}
_COOLDOWN_LOADED = False
_COOLDOWN_MTIME: float | None = None
_COOLDOWN_LOCK = threading.Lock()
_COOLDOWN_DEFAULT_S = 60.0
_COOLDOWN_MAX_S = 900.0
_UNAVAILABLE_S = 3600.0
# 429 のクールダウンは**回数ではしごを上る**（1 回目 60 秒 → 2 回目 180 秒 → 3 回目以降 600 秒）。
# 一律 60 秒だと「1 分ごとに叩いて毎回 429」になり、相手の枠を無駄に消費する（実測: 8 モデルの
# panel で同じ提供元が 1〜2 件の 429 を返す）。上限は `_COOLDOWN_MAX_S`。
_COOLDOWN_LADDER_S = (60.0, 180.0, 600.0)


def cooldowns_path() -> str:
    return os.environ.get("FREEAGENT_COOLDOWN_PATH") or os.path.join(state_dir(), "cooldowns.json")


def _cooldown_prune(entries: dict, now: float) -> dict:
    return {ref: e for ref, e in entries.items()
            if isinstance(e, dict) and as_float(e.get("until"), 0.0) > now}


def _ensure_cooldown_loaded() -> None:
    global _COOLDOWN_LOADED, _COOLDOWN_MTIME
    if not COOLDOWN_ENABLED:
        return
    path = cooldowns_path()
    mtime = _file_mtime(path)
    # **別プロセスが書いたクールダウンも見る**（v0.25.0）。1 回だけ読む実装だと、stdio と常駐 HTTP が
    # 同じディレクトリを共有しているとき、片方が学習した「Free 終了（404）」を知らないまま叩き続ける。
    if _COOLDOWN_LOADED and (mtime is None or mtime == _COOLDOWN_MTIME):
        return
    with _COOLDOWN_LOCK:
        if _COOLDOWN_LOADED and (mtime is None or mtime == _COOLDOWN_MTIME):
            return
        data = _load_json(path, {})
        saved = data.get("cooldowns") if isinstance(data, dict) else None
        now = now_ts()
        if isinstance(saved, dict):
            for ref, entry in _cooldown_prune(saved, now).items():
                cur = _COOLDOWN.get(ref)
                # ディスク側が新しい期限を持つときだけ採用する（メモリの記憶を巻き戻さない）
                if not cur or as_float(entry.get("until"), 0.0) > as_float(cur.get("until"), 0.0):
                    _COOLDOWN[ref] = entry
        _COOLDOWN_LOADED = True
        _COOLDOWN_MTIME = mtime


def _cooldown_save() -> bool:
    """ディスクの内容と**併合してから**書く（他プロセスの期限を消さない）。"""
    if not COOLDOWN_ENABLED:
        return False
    now = now_ts()
    path = cooldowns_path()
    with _file_lock(path):
        with _COOLDOWN_LOCK:
            merged = _cooldown_prune(copy.deepcopy(_COOLDOWN), now)
        disk = _load_json(path, {})
        saved = disk.get("cooldowns") if isinstance(disk, dict) else None
        if isinstance(saved, dict):
            for ref, entry in _cooldown_prune(saved, now).items():
                cur = merged.get(ref)
                if not cur or as_float(entry.get("until"), 0.0) > as_float(cur.get("until"), 0.0):
                    merged[ref] = entry
        merged = _cooldown_prune(merged, now)
        ok = _atomic_write(path, _dumps({"version": 1, "saved_at": now, "cooldowns": merged}))
        with _COOLDOWN_LOCK:
            _COOLDOWN.clear()
            _COOLDOWN.update(merged)
        return ok


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


def note_cooldown(ref: str, secs: float, reason: str, strikes: int = 1) -> float:
    if not COOLDOWN_ENABLED:
        return 0.0
    _ensure_cooldown_loaded()
    until = now_ts() + max(1.0, secs)
    with _COOLDOWN_LOCK:
        cur = _COOLDOWN.get(ref)
        if cur and as_float(cur.get("until"), 0.0) > until:
            return as_float(cur.get("until"), 0.0)  # 既存の方が長いなら伸ばさない
        _COOLDOWN[ref] = {"until": until, "reason": reason, "retry_after_s": round(secs, 1),
                          "strikes": int(strikes)}
    _cooldown_save()
    return until


def note_rate_limited(ref: str, retry_after: str | None = None, reason: str = "HTTP 429") -> float:
    """429 を記録する。**回数ではしごを上る**（1 回目は Retry-After を尊重、2 回目以降は段を守る）。

    冷えきってから再度 429 なら 1 段目からやり直す（時間をあけた 1 回を「何度目か」に数えない）。
    """
    _ensure_cooldown_loaded()
    with _COOLDOWN_LOCK:
        entry = _COOLDOWN.get(ref) or {}
        previous = int(as_float(entry.get("strikes"), 0.0))
        still_cooling = as_float(entry.get("until"), 0.0) > now_ts()
    strikes = previous + 1 if still_cooling else 1
    step = _COOLDOWN_LADDER_S[min(strikes - 1, len(_COOLDOWN_LADDER_S) - 1)]
    wait = _parse_retry_after(retry_after) if retry_after else step
    if strikes > 1:
        wait = max(wait, step)          # 連続で踏んだら短い Retry-After でも段を下回らない
    label = reason if strikes == 1 else f"{reason}（{strikes} 回目）"
    return note_cooldown(ref, min(wait, _COOLDOWN_MAX_S), label, strikes=strikes)


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
_AUTH_MTIME: float | None = None
_AUTH_LOCK = threading.Lock()
AUTH_TTL_S = _env_float("FREEAGENT_AUTH_TTL", 900.0)


def auth_path() -> str:
    return os.environ.get("FREEAGENT_AUTH_PATH") or os.path.join(state_dir(), "provider_auth.json")


def _auth_prune(entries: dict, now: float) -> dict:
    return {p: e for p, e in entries.items()
            if isinstance(e, dict) and as_float(e.get("until"), 0.0) > now}


def _ensure_auth_loaded() -> None:
    global _AUTH_LOADED, _AUTH_MTIME
    path = auth_path()
    mtime = _file_mtime(path)
    if _AUTH_LOADED:
        if mtime is None:
            return
        if _AUTH_MTIME is not None and abs(mtime - _AUTH_MTIME) < 1e-6:
            return
        with _AUTH_LOCK:
            if _AUTH_MTIME is not None and abs(mtime - _AUTH_MTIME) < 1e-6:
                return
            data = _load_json(path, {})
            saved = data.get("providers") if isinstance(data, dict) else None
            if isinstance(saved, dict):
                for p, e in saved.items():
                    if not isinstance(e, dict):
                        continue
                    cur = _AUTH.get(p)
                    if cur is None or as_float(e.get("until"), 0.0) > as_float(cur.get("until"), 0.0):
                        _AUTH[p] = e
            _AUTH_LOADED = True
            _AUTH_MTIME = mtime
        return
    with _AUTH_LOCK:
        if _AUTH_LOADED:
            return
        data = _load_json(path, {})
        saved = data.get("providers") if isinstance(data, dict) else None
        if isinstance(saved, dict):
            _AUTH.update(_auth_prune(saved, now_ts()))
        _AUTH_LOADED = True
        _AUTH_MTIME = mtime if mtime is not None else _AUTH_MTIME


def _auth_save() -> bool:
    global _AUTH_MTIME
    path = auth_path()
    with _file_lock(path):
        data = _load_json(path, {})
        saved = data.get("providers") if isinstance(data, dict) else None
        merged = dict(saved) if isinstance(saved, dict) else {}
        now = now_ts()
        with _AUTH_LOCK:
            for p, e in _AUTH.items():
                cur = merged.get(p)
                if cur is None or as_float(e.get("until"), 0.0) >= as_float(cur.get("until"), 0.0):
                    merged[p] = e
            merged = _auth_prune(merged, now)
            payload = {"version": 1, "saved_at": now, "providers": merged}
            ok = _atomic_write(path, _dumps(payload))
            if ok:
                _AUTH.clear()
                _AUTH.update(merged)
                _AUTH_LOADED = True
                _AUTH_MTIME = _file_mtime(path)
        return ok


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
    if not provider:
        return
    _ensure_auth_loaded()
    path = auth_path()
    with _file_lock(path):
        with _AUTH_LOCK:
            _AUTH.pop(provider, None)
            data = _load_json(path, {})
            saved = data.get("providers") if isinstance(data, dict) else None
            merged = dict(saved) if isinstance(saved, dict) else {}
            merged.pop(provider, None)
            now = now_ts()
            merged = _auth_prune(merged, now)
            payload = {"version": 1, "saved_at": now, "providers": merged}
            ok = _atomic_write(path, _dumps(payload))
            if ok:
                _AUTH.clear()
                _AUTH.update(merged)
                _AUTH_LOADED = True
                global _AUTH_MTIME
                _AUTH_MTIME = _file_mtime(path)


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

# `over_budget` は「max_tokens を大きく超えて返した」回数（v0.42.4・issue #8）。
# 予算での出力長制御が効かない提供元があり（実測: 64 指定で 931 字・同じ条件 5/5 で逸脱）、
# 選抜で降格したいが、既定の並びは**静かに変えない**（`FREEAGENT_RANK_BUDGET` で on）。
_KIND_KEYS = ("n", "ok", "leak", "trunc", "empty", "over_budget")


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
# 最後に読んだファイルの更新時刻（**別プロセスの記録を取り込む**ため・v0.25.0）。
_STATS_MTIME: float | None = None


def _stats_read(path: str) -> dict:
    """`model_stats.json` を読む（壊れていれば空）。**ロックの内側で呼ぶ**。"""
    data = _load_json(path, {})
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def _ensure_stats_loaded() -> None:
    global _STATS_LOADED, _STATS_MTIME
    if not STATS_ENABLED:
        return
    path = stats_path()
    mtime = _file_mtime(path)
    # 読み込み済みでも、**ファイルが別プロセスに更新されていたら読み直す**。
    # 「起動時に 1 回だけ」だと、常駐 HTTP と stdio が同じディレクトリを共有する構成で
    # 片方の学習が永久に見えない（`_STATS_MTIME` は自分が読んだ時刻だけを覚える）。
    if _STATS_LOADED and (mtime is None or mtime == _STATS_MTIME):
        return
    with _STATS_LOCK:
        if _STATS_LOADED and (mtime is None or mtime == _STATS_MTIME):
            return
        # **丸ごと置き換えない**。ディスクを土台にし、メモリ側の行は `updated_at` が新しいときだけ
        # 採用する（読むだけで学んだ行を消さない・テストが直接入れた行も消えない）。
        merged = _stats_read(path)
        for ref, row in _STATS["models"].items():
            cur = merged.get(ref)
            if cur is None or as_float(row.get("updated_at"), 0.0) >= as_float(cur.get("updated_at"), 0.0):
                merged[ref] = row
        _STATS["models"] = merged
        _STATS_LOADED = True
        _STATS_MTIME = mtime


def _stats_apply(models: dict, ref: str, kind: str, *, now: float, error: str = "",
                 leak: bool = False, trunc: bool = False, empty: bool = False,
                 over_budget: bool = False, lat_ms: float = 0.0) -> dict:
    """1 件の観測を `models` に足す（読み直した**最新の写し**に対して行う）。"""
    entry = models.setdefault(ref, {"kinds": {}, "updated_at": now})
    if not isinstance(entry.get("kinds"), dict):
        entry["kinds"] = {}
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
    if over_budget:
        # 成功・失敗とは独立に数える（空応答でも成功でも、予算を超えた事実は残す）
        row["over_budget"] = as_float(row.get("over_budget"), 0.0) + 1.0
    if lat_ms:
        row["lat_ms"] = as_float(row.get("lat_ms"), 0.0) + lat_ms
    return models


def _stats_save() -> bool:
    """いまのメモリ上の統計を書く（`note_observation` は差分を足す経路を使う）。"""
    if not STATS_ENABLED:
        return False
    now = now_ts()
    path = stats_path()
    with _STATS_WRITE_LOCK, _file_lock(path):
        with _STATS_LOCK:
            models = _stats_prune(copy.deepcopy(_STATS["models"]), now)
        payload = {"version": 1, "models": models}
        return _atomic_write(path, _dumps(payload))


def classify_error(err: str) -> str:
    """エラー文字列を分類する（集計の粒度を揃えるため、ここで語彙を固定する）。"""
    t = (err or "").lower()
    # **空応答は「壊れている」ではなく「予算で落ちた」**（実測: 思考モデルが max_tokens を使い切ると
    # 本文が空で返る。自動の再試行で床まで上げても空のことがある）。ハード失敗と混ぜると
    # 「キーが無い・廃止された」と同じ扱いになり、復旧可能な失敗の内訳が見えなくなる（v0.18.0）。
    if "空応答" in (err or "") or "empty response" in t or "no output" in t:
        return "no_output"
    raw_err = err or ""
    if (any(part.strip(".,:;()[]") == "429" for part in t.split())
            or "rate limit" in t or "rate_limit" in t or "rate-limit" in t
            or "too many requests" in t or "quota" in t
            or "同時実行枠" in raw_err or ("上限" in raw_err and "達しました" in raw_err)):
        return "rate_limited"
    if ("timeout" in t or "timed out" in t or "時間切れ" in raw_err
            or "締め切り" in raw_err or "並列実行の予算" in raw_err or "予算を使い切った" in raw_err):
        return "timeout"
    if "404" in t or "410" in t or "gone" in t:
        return "gone"
    if "401" in t or "403" in t or "unauthor" in t or "forbidden" in t:
        return "auth"
    if "5" == t[:1] and t[1:2].isdigit():
        return "server"
    # 自前のエラー文字列は "HTTP 503: ..." の形なので、**先頭が 5 かの判定だけでは拾えない**（実測:
    # classify_error("HTTP 503") が other に落ち、5xx が統計上「その他」に混ざっていた）。
    if "http" in t and re.search(r"\b5\d\d\b", t):
        return "server"
    # "HTTP 500: ... Authorization<Bearer> was not found" 等が gone に化けないよう 5xx/auth 判定の後で見る。
    if "not found" in t:
        return "gone"
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
    # または未設定プロバイダ・有料モデル拒否（呼び出し前の設定ガード＝モデルの成績ではない）
    return ("モデルが 0 件" in err or "モデルが解決できませんでした" in err
            or "未設定/未確認です" in err
            or "このFree-tier設定で許可されていない" in err
            or "有料モデルのため呼び出しを拒否" in err)


def note_observation(ref: str, kind: str, *, error: str = "", leak: bool = False,
                     trunc: bool = False, empty: bool = False, over_budget: bool = False,
                     lat_ms: float = 0.0) -> None:
    """1 回の呼び出し結果を記録する（ok は「エラーが無く、空でもなく、切断もされていない」）。"""
    global _STATS_LOADED, _STATS_MTIME
    if not STATS_ENABLED:
        return
    now = now_ts()
    path = stats_path()
    # **プロセス間で read-modify-write を直列化する**（v0.25.0）。メモリ上の写しに足して
    # 丸ごと上書きすると、同じ `FREEAGENT_STATE_DIR` を共有する 2 つのプロセス
    # （常駐 HTTP と stdio）で **後勝ちの欠落**が出る（実測: 2 プロセス × 50 件 = 期待 100 件に対し
    # 旧実装は **50 件**。ほぼ片方の記録が丸ごと消える）。ロックの内側でディスクの最新を読み、
    # そこへ 1 件だけ足す。
    with _file_lock(path):
        models = _stats_apply(_stats_read(path), ref, kind, now=now, error=error,
                              leak=leak, trunc=trunc, empty=empty, over_budget=over_budget,
                              lat_ms=lat_ms)
        models = _stats_prune(models, now)
        _atomic_write(path, _dumps({"version": 1, "models": models}))
        with _STATS_LOCK:
            _STATS["models"] = models
            _STATS_LOADED = True
            _STATS_MTIME = _file_mtime(path)


def _kind_quality(row: dict) -> tuple[float, float]:
    """(品質 0〜1, 観測数) を返す。切断・CoT混入・空を減点し、エラー率を掛ける。

    **空応答（`no_output`）はハード失敗の半分の重み**で減点する（v0.18.0・実測にもとづく）。
    思考トークンで予算を使い切っただけの失敗は、`max_tokens` を上げれば回復する種類であり、
    「廃止（404）」「権限（403）」「5xx」と同じ重みで沈めると、**大きい予算でなら正しく答える
    モデルが永久に選ばれなくなる**。かといって無視はしない（実測: 床 512・上限 2048 まで上げても
    空のままのモデルがいる＝その用途では実質使えない）。減点の重みが半分なだけで、
    成功が 1 件も無ければ品質は 0 のまま（選抜で上に来ることはない）。
    """
    n = as_float(row.get("n"), 0.0)
    if n <= 0:
        return 0.0, 0.0
    ok = as_float(row.get("ok"), 0.0)
    leak = as_float(row.get("leak"), 0.0)
    trunc = as_float(row.get("trunc"), 0.0)
    empty = as_float(row.get("empty"), 0.0)
    err = row.get("err") or {}
    if isinstance(err, dict):
        soft = as_float(err.get("no_output"), 0.0)
        hard = sum(as_float(v, 0.0) for k, v in err.items() if k != "no_output")
    else:
        soft, hard = 0.0, 0.0
    # 失敗は **1 回だけ** 数える（v0.40.3）。旧実装は「成功比」（分子が既に失敗を含まない）に
    # さらに (1 - 失敗率) を掛けていたため失敗率が 2 乗で効き、勝率 50% のモデルが
    # 実質 1 勝 3 敗相当に歪んだ（本番 model_stats 実測: ok=2/n=4 で _rank_score 0.4328＜未観測 0.5722）。
    # 追加の減点はハード失敗（404・5xx・auth 等）だけに留め、空応答（soft・本番失敗の 80%）は
    # 「成功に数えない」1 回の減点にとどめる。`empty`（旧フィールド）は参照しない。
    score = (ok + 0.5 * trunc + 0.3 * leak) / n
    score *= max(0.0, 1.0 - min(1.0, hard / n))
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


def _rank_counts(ref: str) -> tuple[float, float]:
    """順位づけ用の (成功, 失敗) の減衰つき疑似カウント。

    **種別（kind）を観測数 n で重み付けして合算する**。`model_quality` は種別ごとの品質を単純平均するので、
    `ask` 1 件成功・`panel` 50 件失敗のモデルが「品質 0.5」に見える。順位はその歪みをそのまま使わない。
    """
    _ensure_stats_loaded()
    kinds = (_STATS["models"].get(ref) or {}).get("kinds") or {}
    success = 0.0
    total = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = as_float(row.get("n"), 0.0)
        if n <= 0:
            continue
        success += _kind_quality(row)[0] * n
        total += n
    return success, max(0.0, total - success)


_RANK_EXPLORE = 0.25     # Beta 事後の標準偏差に掛ける探索の強さ


def _rank_score(ref: str) -> float:
    """Beta 事後の平均 + 探索項（標準偏差）。**観測 1 件の幸運が実績を上回り続けない**。

    事後の平均は (1+成功)/(2+n) なので、n が小さいほど 0.5 へ縮む（事前分布による縮小が自動で入る）。
    標準偏差も n とともに小さくなるので、探索は「まだ分からないモデル」にだけ効く。
    乱数は使わない（同じ入力で同じ順位＝説明とテストが再現する。Thompson の事後を決定的に使う形）。
    """
    success, fail = _rank_counts(ref)
    a = 1.0 + success
    b = 1.0 + fail
    total = a + b
    var = (a * b) / (total * total * (total + 1.0))
    return a / total + _RANK_EXPLORE * math.sqrt(max(0.0, var))


def _rank_latency_key(ref: str) -> float:
    """同点のときの第 2 キー（平均所要ミリ秒）。**観測が無いモデルは後ろ**（`math.inf`）。

    品質の事後平均が完全に一致する 2 体（例: どちらも 1 勝 0 敗）は、これまで ID の
    アルファベット順で決まっていた。応答が 2 秒のモデルと 25 秒のモデルが同点なら、
    速い方を先に置く方が利用者の待ち時間は短い（品質の順位は**1 ミリも変えない**＝
    同点の内側だけを並べ替える。実測: 判断は `_rank_score` の値そのまま）。
    """
    latency = model_mean_latency(ref)
    return latency if latency is not None else math.inf


# 遅延ペナルティ（**既定 off**・v0.27.0）。`FREEAGENT_RANK_LATENCY` に 0〜1 の重みを入れたときだけ
# 品質点から引く。品質統計（§2.2）は「正しく答えたか」しか見ていないので、**同じ正答率なら速い方**を
# 選びたい場面（`map` のように件数が多い呼び出し）では順位が遅いモデルに固定され得る。
# **既定を静かに変えない**ため、重み 0 では `_rank_score` の値を 1 ミリも変えない（`_latency_penalty`
# が厳密に 0.0 を返す）＝オフの並びは v0.26.0 と同一。
# **kind 別の品質バイアス（既定 off**・v0.41.0）。本番 traces の実測では失敗の 80.4% が空応答で、
# その多くは「短答の kind（eval 等）で好成績の思考モデルが、長答の kind（consult 700〜800 tokens）
# でも上位選抜され続ける」型だった。on にすると、**同じ kind の観測**の失敗率（空応答・ハード失敗）
# を選抜順位から引く（他 kind の成績は参照しない＝短答での実績で長答を選ばない）。off では
# `_rank_score` の値を 1 ミリも変えない（v0.27.0 の遅延項と同じ「既定 off」の型）。
RANK_KIND_BIAS = _env_on("FREEAGENT_RANK_KIND_BIAS", False)
_KIND_BIAS_MIN_CALLS = 3
_KIND_BIAS_CAP = 0.30


def _kind_bias(ref: str, kind: str) -> float:
    """同じ kind の失敗率から引くペナルティ（0〜`_KIND_BIAS_CAP`。未観測は 0）。"""
    if not RANK_KIND_BIAS or not kind:
        return 0.0
    _ensure_stats_loaded()
    row = ((_STATS["models"].get(ref) or {}).get("kinds") or {}).get(kind)
    if not isinstance(row, dict):
        return 0.0
    n = as_float(row.get("n"), 0.0)
    if n < _KIND_BIAS_MIN_CALLS:
        return 0.0
    err = row.get("err") or {}
    soft = as_float(err.get("no_output"), 0.0) if isinstance(err, dict) else 0.0
    hard = (sum(as_float(v, 0.0) for k, v in err.items() if k != "no_output")
            if isinstance(err, dict) else 0.0)
    return round(min(_KIND_BIAS_CAP, 0.3 * soft / n + 0.5 * hard / n), 4)


# 予算超過の降格（既定 off・v0.42.4）。on にすると、観測のうち `over_budget` の割合だけ
# 品質点から引く（上限 0.20）。**2 件未満は効かせない**（1 回の偶然で順位を動かさない）。
RANK_BUDGET = _env_on("FREEAGENT_RANK_BUDGET", False)
_RANK_BUDGET_CAP = 0.20
_RANK_BUDGET_MIN_CALLS = 2


def _budget_penalty(ref: str) -> float:
    """`max_tokens` を超えて返した割合ぶんのペナルティ（0〜`_RANK_BUDGET_CAP`）。"""
    if not RANK_BUDGET:
        return 0.0
    _ensure_stats_loaded()
    kinds = (_STATS["models"].get(ref) or {}).get("kinds") or {}
    total = 0.0
    over = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = as_float(row.get("n"), 0.0)
        if n <= 0:
            continue
        total += n
        over += as_float(row.get("over_budget"), 0.0)
    if total < _RANK_BUDGET_MIN_CALLS or over <= 0:
        return 0.0
    return round(min(_RANK_BUDGET_CAP, over / total), 4)


RANK_LATENCY_W = max(0.0, min(1.0, _env_float("FREEAGENT_RANK_LATENCY", 0.0)))
RANK_LATENCY_FULL_S = max(1.0, _env_float("FREEAGENT_RANK_LATENCY_FULL_S", 20.0))


def _latency_penalty(ref: str) -> float:
    """平均所要が `RANK_LATENCY_FULL_S` 秒に達したとき、品質点から `RANK_LATENCY_W` を引く。

    比例（頭打ちあり）にするのは、**極端に遅いモデル 1 体が順位を総取りしない**ため
    （20 秒を超えても引く量は増えない）。観測が無いモデルは 0（探索を妨げない）。
    """
    if RANK_LATENCY_W <= 0.0:
        return 0.0
    latency_ms = model_mean_latency(ref)
    if latency_ms is None:
        return 0.0
    share = min(1.0, (float(latency_ms) / 1000.0) / RANK_LATENCY_FULL_S)
    return RANK_LATENCY_W * share


def rank_models(refs: list[str]) -> list[str]:
    """品質順。観測が少ないモデルへ探索ボーナスを与え、新しい Free モデルが試されない問題を防ぐ。

    `FREEAGENT_RANK_LATENCY` が 0（既定）なら **v0.26.0 と同じ並び**（同点は平均所要の速い順）。
    """
    if not RANK_ENABLED:
        return list(refs)
    return sorted(refs, key=lambda r: (-(_rank_score(r) - _latency_penalty(r)
                                         - _budget_penalty(r)),
                                       _rank_latency_key(r), r))


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


# ---------------------------------------------------------------- §3.6 自動選抜の strategy
#
# 「誰を働かせるか」の**並べ方**を差し替え可能にする（プロバイダの巡回 `diverse_order` は共通の
# 後段なので、多様性はどの strategy でも保たれる）。既定は `quality`（品質統計＋探索ボーナス）で、
# **v0.16.0 までの挙動と同一**。
#   * quality    … `rank_models`（Beta 事後 mean + 0.25·sd、観測の少ないモデルを試す）
#   * catalog    … 一覧が返した順（提供元の並びをそのまま使う。比較・切り分け用）
#   * least_busy … 提供元の**実行中本数**が少ない順（同時実行の詰まりを避ける。litellm の
#                  least_busy と同じ発想で、こちらは `_ProviderGate` の in_flight を使う）
SELECT_STRATEGIES = ("quality", "catalog", "least_busy")
SELECT_STRATEGY_ENV = "FREEAGENT_SELECT_STRATEGY"


def selection_strategy() -> str:
    """`FREEAGENT_SELECT_STRATEGY` を読む。未知の値は `quality` に落とす（起動を止めない）。"""
    want = (os.environ.get(SELECT_STRATEGY_ENV) or "quality").strip().lower()
    return want if want in SELECT_STRATEGIES else "quality"


# ---------------------------------------------------------------- §3.7 タスク分類カスケード
#
# 「どの層のモデルに当てるか」を**呼び出しの形から決定的に**決める（分類のための余分な LLM 呼び出しを
# しない。1 回分類すると、その分だけ枠・クォータ・待ち時間を使う）。
#   * light  … 抽出・分類・要約（`map` / `ask` かつ max_tokens が小さい短い入力）→ **平均所要時間の速い順**
#   * long   … 長い入力を渡す呼び出し → **打ち切り（trunc）の率が低い順**（長文では切れた回答が使い物にならない）
#   * reason … それ以外（panel / consult / grounded / think の検証など）→ **品質順**（v0.21.0 までと同じ）
#
# **既定は off**（`FREEAGENT_ROUTING=auto` で有効）。off のときは並びを 1 ミリも変えない
# （`tests/test_routing.py` が「off は従来と同一の並び」を固定する）。
# 測定用に層を**強制**できる（`FREEAGENT_ROUTING_TIER=light|long|reason`。
# `scripts/measure_quality.py --select-strategy quality` と組み合わせて A/B を取る）。
ROUTING_ENV = "FREEAGENT_ROUTING"
ROUTING_TIER_ENV = "FREEAGENT_ROUTING_TIER"
ROUTING_TIERS = ("light", "long", "reason")
ROUTING_LONG_CHARS = max(200, min(200000, _env_int("FREEAGENT_ROUTING_LONG_CHARS", 6000)))
ROUTING_LIGHT_MAX_TOKENS = max(16, min(4000, _env_int("FREEAGENT_ROUTING_LIGHT_MAX_TOKENS", 400)))
ROUTING_LIGHT_MIN_QUALITY = max(0.0, min(1.0, _env_float("FREEAGENT_ROUTING_LIGHT_MIN_QUALITY", 0.5)))
# 層はスレッドローカルに置く（**選抜は呼び出し側スレッドで走る**。`run_parallel` のワーカーではない）。
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
    name = as_str(kind).lower()
    # **`freeagent_ask` は対象外**: あれは `default_model()` を 1 回呼ぶだけで選抜を通らない（実測）。
    # 層は「選抜される呼び出し」にだけ意味がある（map / panel / grounded / fanout / agent）。
    if name == "freeagent_map" and 0 < max_tokens <= ROUTING_LIGHT_MAX_TOKENS \
            and text_chars < ROUTING_LONG_CHARS // 2:
        return "light"
    return "reason"


# 長さの判定に使う引数（**ツールごとに名前が違う**ので、代表的な本文キーだけを数える）。
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
    _ensure_stats_loaded()
    kinds = (_STATS["models"].get(ref) or {}).get("kinds") or {}
    total_n = total_ms = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = as_float(row.get("n"), 0.0)
        ms = as_float(row.get("lat_ms"), 0.0)
        if n > 0 and ms > 0:
            total_n += n
            total_ms += ms
    return round(total_ms / total_n, 1) if total_n and total_ms else None


def model_trunc_rate(ref: str) -> float | None:
    """打ち切りの率（trunc / n）。観測が無ければ None。"""
    _ensure_stats_loaded()
    kinds = (_STATS["models"].get(ref) or {}).get("kinds") or {}
    n = trunc = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n += as_float(row.get("n"), 0.0)
        trunc += as_float(row.get("trunc"), 0.0)
    return round(trunc / n, 4) if n else None


def _tier_order(refs: list[str], tier: str) -> list[str]:
    """層ごとの並び。**品質順（`rank_models`）を土台**にし、層の指標で前後させる。"""
    quality_index = {ref: i for i, ref in enumerate(rank_models(refs))}
    if tier == "light":
        def light_key(ref: str):
            quality = model_quality(ref)
            observed = model_observations(ref) > 0
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
    """自動選抜の並び順を strategy ごとに決める（`diverse_order` は呼び出し側で共通）。"""
    strategy = selection_strategy()
    if strategy == "catalog":
        base = list(refs)
    elif strategy == "least_busy":
        per = (PROVIDER_GATE.report(list(PROVIDER_ORDER)).get("per_provider") or {})
        quality_index = {ref: i for i, ref in enumerate(rank_models(refs))}
        def busy_key(ref: str):
            provider = ref.split("/", 1)[0]
            in_flight = (per.get(provider) or {}).get("in_flight") or 0
            # 空きが同じなら品質順（決定的。乱数を使わない）
            return (in_flight, quality_index.get(ref, len(refs)), ref)
        base = sorted(refs, key=busy_key)
    else:
        base = rank_models(refs)
        if kind and RANK_KIND_BIAS:
            # **kind 別バイアス**: 同じ kind の失敗率を引いて並べ替える（既定 off では通らない）。
            base = sorted(refs, key=lambda r: (-(_rank_score(r) - _kind_bias(r, kind)),
                                               _rank_latency_key(r), r))
    # §3.7 タスク分類カスケード。**層が選ばれていない（off）ときは 1 ミリも並べ替えない**。
    tier = forced_tier() or current_tier()
    return _tier_order(base, tier) if tier in ROUTING_TIERS else base


def env_excluded_models() -> list[str]:
    """`FREEAGENT_MODEL_EXCLUDE`（カンマ区切り）で指定された除外モデル一覧."""
    raw = os.environ.get("FREEAGENT_MODEL_EXCLUDE") or ""
    return [part.strip() for part in raw.split(",") if part.strip()]


def _model_identity(ref: str) -> str:
    """Identity for independent opinions, not for catalog pricing or authorization."""
    provider, model = split_ref(ref)
    if _is_hf(provider) and ":" in model:
        return make_ref(provider, model.rsplit(":", 1)[0])
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
    all_exclude = list(dict.fromkeys(as_str_list(exclude) + env_excluded_models()))
    ex_set = set(all_exclude)
    identity_set = {_model_identity(ref) for ref in as_str_list(identity_exclude)}
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
        provider, model = split_ref(ref)
        return ref in avail_set or (_is_hf(provider) and ":" in model
                                    and make_ref(provider, model.rsplit(":", 1)[0]) in avail_set)

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
        cooling_requested = [r for r in chosen if is_cooling(r)]
        if cooling_requested:
            notes.append(f"指定モデルがクールダウン中（再試行で回復します）: {', '.join(cooling_requested[:3])}")
    if prefer:
        add(prefer)
    if len(chosen) < size:
        # **非チャットモデルは自動選抜に載せない**（§3.2）。明示指定の在庫確認（is_available）は
        # 在庫の全 Free を使うので、名指しされたモデルはこれまでどおり試す。
        chat_ok = set(free_model_refs(free_only=free_only, chat_only=True))
        pool = [r for r in available if r not in chosen and r in chat_ok
                and _model_identity(r) not in identity_set]
        if ex_set:
            pool = [r for r in pool if not _is_model_excluded(r, ex_set)]
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
        picked = []
        seen = {_model_identity(ref) for ref in chosen}
        for candidate in diverse_order(_order_for_selection(ready, kind)):
            identity = _model_identity(candidate)
            if identity not in seen:
                picked.append(candidate)
                seen.add(identity)
                if len(picked) >= need:
                    break
        if len(picked) < need and waiting:
            for candidate in diverse_order(_order_for_selection(waiting)):
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
    explicit_kept = len([r for r in as_str_list(requested) if r in chosen])
    chosen = chosen[:max(size, explicit_kept)]
    info = {
        "requested": bool(requested),
        "prefer": as_str_list(prefer),
        "exclude": all_exclude,
        "available": len(available),
        "ranking": RANK_ENABLED,
        "selection_strategy": selection_strategy(),
        # §3.7 の層（off のときは "off"）。選んだ根拠を隠さない（規約 8）。
        "cascade_tier": forced_tier() or current_tier() or "off",
        "status": {r: model_status(r) for r in chosen},
        "notes": notes,
    }
    return chosen, info


# ---------------------------------------------------------------- §2.3 トレース
#
# 1 行 1 呼び出しのメタデータ（本文なし）。上限を超えたら 1 世代だけ退避する。

# ---------------------------------------------------------------- §2.3.1 呼び出し単位の ID
#
# traces は 1 行 1 モデル呼び出しなので、**同じツール呼び出しの中の内訳**（panel の 3 体中 2 体が
# 落ちた、など）を後から復元できなかった（実測 2026-11-02: 1,812 行のうち run/parent を持つ行は 0。
# 複数モデル系が 497 行、うち 58 行は「同じ呼び出しの他の枠が使用中」という**呼び出し文脈でしか
# 説明できない失敗**で、集計では other に埋もれていた）。
#
# 呼び出し 1 回に 1 つの `run` を張り、**並列ワーカーへは明示的に引き継ぐ**（スレッドローカルは
# 別スレッドに伝播しない。合流器・進捗と同じ非対称）。ツール名も一緒に持たせる（集計の軸になる）。

_RUN_LOCAL = threading.local()


def current_run() -> str:
    return getattr(_RUN_LOCAL, "run", "") or ""


def current_tool() -> str:
    return getattr(_RUN_LOCAL, "tool", "") or ""


@contextlib.contextmanager
def run_scope(run: str, tool: str = ""):
    """このスレッドの `run` / `tool` を差し替える（`finally` で必ず戻す）。"""
    previous = (current_run(), current_tool())
    _RUN_LOCAL.run = run or ""
    _RUN_LOCAL.tool = tool or previous[1]
    try:
        yield run
    finally:
        _RUN_LOCAL.run, _RUN_LOCAL.tool = previous


def new_run_id() -> str:
    return "r" + uuid.uuid4().hex[:12]


def trace_path() -> str:
    return os.environ.get("FREEAGENT_TRACE_PATH") or os.path.join(state_dir(), "traces.jsonl")


_TRACE_MAX_BYTES = _env_int("FREEAGENT_TRACE_MAX_BYTES", 5_000_000)


def trace_call(kind: str, ref: str, ok: bool, *, error: str = "", served_by: str = "",
               rate_limited=None, unavailable=None, skipped_cooling=None,
               answer_len: int = 0, answer_sha1: str = "", truncated: bool = False,
               latency_s: float = 0.0, tokens: dict | None = None,
               finish_reason: str = "", max_tokens: int | None = None, retries: int = 0,
               run: str = "", tool: str = "") -> None:
    """1 呼び出しを traces に 1 行で残す。

    **`finish_reason` / `max_tokens` / `retries` を残す**（v0.42.1）: 空応答の原因が
    「思考トークンで予算を使い切った（length）」のか「そもそも stop で本文が空」なのかは
    記録に無いと後から切り分けられない（実測: v0.42.0 までの traces は 438 件の空応答すべてで
    tokens が空・finish_reason の列も無かった）。
    """
    if not TRACE_ENABLED:
        return
    row = {"run": run or current_run(), "tool": tool or current_tool(),
           "kind": kind, "ref": ref, "ok": bool(ok), "error": error or None,
           "error_class": classify_error(error) if error else None,
           "served_by": served_by or ref, "rate_limited": rate_limited,
           "unavailable": unavailable, "skipped_cooling": skipped_cooling,
           "answer_len": int(answer_len or 0), "answer_sha1": answer_sha1 or "",
           "truncated": bool(truncated), "latency_s": round(as_float(latency_s, 0.0), 3),
           "tokens": tokens or {}, "finish_reason": finish_reason or None,
           "max_tokens": max_tokens, "retries": int(retries or 0), "ts": now_ts()}
    path = trace_path()
    try:
        with _STATE_LOCK, _file_lock(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) > _TRACE_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(_dumps(row) + "\n")
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
                     over_budget=_is_over_budget(result),
                     lat_ms=as_float(result.get("latency_s"), 0.0) * 1000.0)
    trace_call(kind, ref, not error, error=error,
               served_by=as_str(result.get("served_by")) or ref,
               rate_limited=result.get("rate_limited"),
               unavailable=result.get("unavailable"),
               skipped_cooling=result.get("skipped_cooling"),
               answer_len=len(text or ""), answer_sha1=sha1_12(text or ""),
               truncated=bool(result.get("truncated")),
               latency_s=as_float(result.get("latency_s"), 0.0),
               tokens=result.get("tokens") or {},
               finish_reason=as_str(result.get("finish_reason")),
               max_tokens=as_int(result.get("max_tokens"), 0, 0, 10 ** 9) or None,
               retries=as_int(result.get("retries"), 0, 0, 100))


# ---------------------------------------------------------------- §2.4 相談セッション
#
# 双方向相談（サブがメインへ問い返す → メインが答えて同じセッションを再開する）の**進行状態**。
# 知識は蓄積しない（保存するのは作業状態だけ）。永続化する理由: セッションがプロセス内だけだと
# **再起動でメインが答える前に消え**、往復そのものが成立しない（利用者からは「相談が途中で切れる」と
# しか見えない）。原子置換 + TTL + 上限で prune し、期限切れは復元しない（古い相談を復活させない）。

_SESSIONS: dict[str, dict] = {}
_SESSIONS_LOADED = False
_SESSIONS_MTIME: float | None = None
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
    global _SESSIONS_LOADED, _SESSIONS_MTIME
    if not SESSIONS_ENABLED:
        return
    path = sessions_path()
    mtime = _file_mtime(path)
    if _SESSIONS_LOADED:
        if mtime is None:
            return
        if _SESSIONS_MTIME is not None and abs(mtime - _SESSIONS_MTIME) < 1e-6:
            return
        with _SESSIONS_LOCK:
            if _SESSIONS_MTIME is not None and abs(mtime - _SESSIONS_MTIME) < 1e-6:
                return
            data = _load_json(path, {})
            rows = data.get("sessions") if isinstance(data, dict) else None
            rows = rows if isinstance(rows, dict) else {}
            for sid, row in rows.items():
                if not isinstance(row, dict):
                    continue
                cur = _SESSIONS.get(sid)
                if cur is None or as_float(row.get("updated_at"), 0.0) >= as_float(cur.get("updated_at"), 0.0):
                    _SESSIONS[sid] = row
            _SESSIONS_LOADED = True
            _SESSIONS_MTIME = mtime
        return
    with _SESSIONS_LOCK:
        if _SESSIONS_LOADED:
            return
        data = _load_json(path, {})
        rows = data.get("sessions") if isinstance(data, dict) else None
        _SESSIONS.update(_sessions_prune(rows if isinstance(rows, dict) else {}, now_ts()))
        _SESSIONS_LOADED = True
        _SESSIONS_MTIME = mtime if mtime is not None else _SESSIONS_MTIME


def _sessions_save() -> bool:
    global _SESSIONS_MTIME
    if not SESSIONS_ENABLED:
        return False
    path = sessions_path()
    with _file_lock(path):
        data = _load_json(path, {})
        disk_rows = data.get("sessions") if isinstance(data, dict) else None
        disk_rows = disk_rows if isinstance(disk_rows, dict) else {}
        merged = dict(disk_rows)
        with _SESSIONS_LOCK:
            for sid, row in _SESSIONS.items():
                cur = merged.get(sid)
                if cur is None or (isinstance(row, dict) and as_float(row.get("updated_at"), 0.0) >= as_float((cur or {}).get("updated_at"), 0.0)):
                    merged[sid] = row
            merged = _sessions_prune(merged, now_ts())
            payload = {"version": 1, "saved_at": now_ts(), "sessions": merged}
            ok = _atomic_write(path, _dumps(payload))
            if ok:
                _SESSIONS.clear()
                _SESSIONS.update(merged)
                _SESSIONS_LOADED = True
                _SESSIONS_MTIME = _file_mtime(path)
        return ok


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
        return _deep_copy(row)  # 呼び出し側の書き換えが他へ波及しないよう複製を渡す


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
    if not sid or not SESSIONS_ENABLED:
        return
    _ensure_sessions_loaded()
    path = sessions_path()
    with _file_lock(path):
        with _SESSIONS_LOCK:
            _SESSIONS.pop(sid, None)
            data = _load_json(path, {})
            disk_rows = data.get("sessions") if isinstance(data, dict) else None
            disk_rows = disk_rows if isinstance(disk_rows, dict) else {}
            merged = dict(disk_rows)
            merged.pop(sid, None)
            merged = _sessions_prune(merged, now_ts())
            payload = {"version": 1, "saved_at": now_ts(), "sessions": merged}
            ok = _atomic_write(path, _dumps(payload))
            if ok:
                _SESSIONS.clear()
                _SESSIONS.update(merged)
                _SESSIONS_LOADED = True
                global _SESSIONS_MTIME
                _SESSIONS_MTIME = _file_mtime(path)


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
# Compatibility bookkeeping; reads refresh from the locked disk ledger.
_THOUGHTS_MTIME = 0.0
_THOUGHTS_LOCK = threading.RLock()
# Per-process, per-ledger high-water time. Reads never persist expiry; this
# prevents a local clock rollback from reviving an already-observed expiry.
_THOUGHTS_CLOCK: dict[str, float] = {}
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


def _thoughts_disk_chains(path: str, *, strict: bool = False) -> dict:
    """Read chains; strict writes must never replace an unreadable ledger."""
    if strict:
        try:
            with open(path, "r", encoding="utf-8") as stream:
                data = json.load(stream)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise OSError("思考台帳を読めません。上書きしません") from exc
        rows = data.get("chains") if isinstance(data, dict) else None
        if not isinstance(rows, dict) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("updated_at"), (int, float))
            or isinstance(row.get("updated_at"), bool)
            or not math.isfinite(row["updated_at"])
            or ("verifier_models" in row and
                (not isinstance(row["verifier_models"], list) or
                 any(not isinstance(model, str) for model in row["verifier_models"])))
            or ("verifier_generation" in row and
                (type(row["verifier_generation"]) is not int or row["verifier_generation"] < 0))
            or ("steps" in row and (not isinstance(row["steps"], list)
                                   or any(not isinstance(step, dict) for step in row["steps"])))
            for row in rows.values()
        ):
            raise OSError("思考台帳の形式が不正です。上書きしません")
        return rows
    data = _load_json(path, {})
    rows = data.get("chains") if isinstance(data, dict) else None
    return rows if isinstance(rows, dict) else {}


def _thoughts_merge_row(base: dict, mine: dict) -> dict:
    """2 つの台帳行を統合する（別プロセスが同じ台帳へ同時に書いたときの last-writer-wins 防止）。

    **集合もの（`steps`）は番号の和集合**（同じ番号は新しい方が優先）で、
    **状態もの（`plan` / `plan_retired` / `branch_meta` / `total_history`）は
    `updated_at` が新しい方の値をそのまま使う**（計画の改訂は「差し替え」なので
    和集合にすると廃止した項目が復活してしまう）。
    """
    base = base or {}
    mine = mine or {}
    newer, older = (mine, base)
    if as_float(base.get("updated_at"), 0.0) > as_float(mine.get("updated_at"), 0.0):
        newer, older = base, mine
    out = _deep_copy(older)
    # ステップは番号の和集合（古い方 → 新しい方 で上書き）。
    steps: dict[int, dict] = {}
    for s in list(older.get("steps") or []) + list(newer.get("steps") or []):
        if isinstance(s, dict):
            steps[as_int(s.get("n"), 0, 0, 9999)] = s
    out["steps"] = [steps[k] for k in sorted(steps)]
    # 状態もの（計画・分岐・見積り履歴）は新しい方の値をそのまま使う。
    for key in ("plan", "plan_retired", "branch_meta", "total_history"):
        if key in newer:
            out[key] = newer.get(key)
    for key in ("card", "question"):
        if newer.get(key):
            out[key] = newer[key]
    vms = [m for m in (out.get("verifier_models") or []) if isinstance(m, str)]
    newer_vms = list(dict.fromkeys(m for m in (newer.get("verifier_models") or []) if isinstance(m, str)))
    # Prefer the newer snapshot's order; retain all of its names even if a
    # single call used more than the ordinary 12-name rotation window.
    newer_vm_set = set(newer_vms)
    vms = [m for m in vms if m not in newer_vm_set] + newer_vms
    out["verifier_models"] = vms[-max(12, len(newer_vms)):]
    out["created_at"] = newer.get("created_at") or older.get("created_at") or now_ts()
    out["updated_at"] = max(as_float(base.get("updated_at"), 0.0),
                             as_float(mine.get("updated_at"), 0.0))
    return out


def _thoughts_write_locked(path: str, chains: dict) -> bool:
    """ロックの中で台帳を書き出す（**呼び出し側がロックを持っている**前提）。"""
    global _THOUGHTS_MTIME
    payload = {"version": 1, "saved_at": now_ts(), "chains": chains}
    ok = _atomic_write(path, _dumps(payload))
    if ok:
        _THOUGHTS_MTIME = _file_mtime(path) or 0.0
    return ok


def _ensure_thoughts_loaded() -> None:
    """Refresh from the locked disk ledger, including deletions and read failures."""
    global _THOUGHTS_LOADED, _THOUGHTS_MTIME
    if not THOUGHTS_ENABLED:
        return
    path = thoughts_path()
    clock_key = os.path.normcase(os.path.abspath(path))
    with _THOUGHTS_LOCK:
        try:
            os.stat(path)
        except FileNotFoundError:
            # The absent observation linearizes before any concurrent creation.
            # A writer still takes the lock and re-reads before modifying the file.
            _THOUGHTS.clear()
            _THOUGHTS_LOADED = True
            _THOUGHTS_MTIME = 0.0
            return
        with _file_lock(path) as locked:
            if not locked:
                raise OSError("思考台帳のロックを取得できませんでした")
            disk = _thoughts_disk_chains(path, strict=True)
            effective_now = max(now_ts(), _THOUGHTS_CLOCK.get(clock_key, 0.0))
            rows = _thoughts_prune(disk, effective_now)
            _THOUGHTS_CLOCK[clock_key] = effective_now
            _THOUGHTS.clear()
            _THOUGHTS.update(rows)
            _THOUGHTS_LOADED = True
            _THOUGHTS_MTIME = _file_mtime(path) or 0.0


def new_thought_id() -> str:
    return f"t{int(now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}{os.urandom(6).hex()}"


def thought_get(sid: str) -> dict | None:
    if not sid:
        return None
    with _THOUGHTS_LOCK:
        _ensure_thoughts_loaded()
        row = _THOUGHTS.get(sid)
        if not row:
            return None
        result = _deep_copy(row)
        if THOUGHTS_ENABLED:
            result["verifier_models"] = result.get("verifier_models") or []
            result["verifier_generation"] = result.get("verifier_generation", 0)
        return result


def _thought_verifier_update(row: dict, models: list | None, snapshot: list | None,
                             additions: list | None = None,
                             snapshot_generation: int | None = None) -> tuple[list, int]:
    """Apply this call's additions to the locked history, preserving concurrent additions."""
    current = [m for m in (row.get("verifier_models") or []) if isinstance(m, str)]
    if snapshot is None:
        return current, 0
    before = set(snapshot)
    candidates = additions if additions is not None else [m for m in (models or []) if m not in before]
    added = list(dict.fromkeys(m for m in candidates if isinstance(m, str) and m))
    combined = list(dict.fromkeys([m for m in current if m not in added] + added))
    # Unknown generations are conservative; equality alone cannot detect ABA.
    if (snapshot_generation is None or snapshot_generation != row.get("verifier_generation", 0)
            or current != snapshot):
        return combined, 0
    keep = max(12, len(set(added)))
    return combined[-keep:], max(0, len(combined) - keep)


def thought_merge(sid: str, step: dict, *, question: str = "", verifier_models: list | None = None,
                  assign_number: bool = False, ops: dict | None = None, card: str = "",
                  verifier_snapshot: list | None = None, verifier_additions: list | None = None,
                  verifier_snapshot_generation: int | None = None, require_existing: bool = False) -> dict:
    """台帳へ 1 ステップを**1 つのロック内で**統合する（読み・採番・書きを分けない）。

    読みと書きを分けると、並列に呼ばれた 2 つの思考が同じ番号を採番して**片方が上書きで消える**
    （メインは 1 ターンで複数ツールを並行に呼ぶ前提で書く。規約 14）。`assign_number=True` のとき
    番号をロック内で採番する。`sid` が未知・期限切れなら**古い内容は復活させず**新しい ID を採番する。
    上限（`THOUGHT_MAX_STEPS`）に達していたら**黙って捨てず** `refused` を立てて返す。

    **プロセス間でも同じ**ようにする: ファイルロック（`_file_lock`）を取り、最新のディスク内容を
    正本として読み、採番・更新・保存まで直列化する。期限切れや削除済み ID は新規扱いにし、
    容量や期限の制約で更新行を残せない場合は何も保存せずエラーを返す。
    """
    global _THOUGHTS_LOADED
    with _THOUGHTS_LOCK:
        path = thoughts_path()
        clock_key = os.path.normcase(os.path.abspath(path))
        if not THOUGHTS_ENABLED:
            # 台帳が無効なら**ロックファイルも作らない**（状態ディレクトリを汚さない）。
            target = sid if sid in _THOUGHTS else new_thought_id()
            row = _deep_copy(_THOUGHTS.get(target) or {})
            steps = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
            step = dict(step)
            if not _thought_subgoal_still_matches(row, steps, step, ops or {}):
                return {"error": "計画が並行して変更され、subgoal の参照先が変わりました。記録していません。台帳を確認して再試行してください。"}
            if assign_number:
                step["n"] = max([as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0]) + 1
            number = as_int(step.get("n"), 0, 0, 9999)
            replaced = any(as_int(s.get("n"), 0, 0, 9999) == number for s in steps)
            if not replaced and len(steps) >= THOUGHT_MAX_STEPS:
                return {"session_id": target, "step": None, "steps_recorded": len(steps), "refused": True}
            old = next((s for s in steps if as_int(s.get("n"), 0, 0, 9999) == number), None)
            steps = [s for s in steps if as_int(s.get("n"), 0, 0, 9999) != number] + [step]
            steps.sort(key=lambda s: as_int(s.get("n"), 0, 0, 9999))
            _thought_apply_ops(row, steps, step, old, ops or {})
            history, omitted = _thought_verifier_update(row, verifier_models, verifier_snapshot, verifier_additions,
                                                         verifier_snapshot_generation)
            row.update({"steps": steps,
                        "card": card or row.get("card") or "",
                        "question": question or row.get("question") or "",
                        "verifier_models": history,
                        "verifier_generation": row.get("verifier_generation", 0) + (verifier_snapshot is not None),
                        "created_at": row.get("created_at") or now_ts(),
                        "updated_at": now_ts()})
            _THOUGHTS[target] = row
            return {"session_id": target, "step": step, "steps_recorded": len(steps),
                    "refused": False, "replaced": replaced, "verifier_omitted": omitted}
        with _file_lock(path) as locked:
            if not locked:
                return {"error": "思考台帳のロックを取得できませんでした。記録していません。"}
            try:
                disk = _thoughts_disk_chains(path, strict=True)
            except OSError as exc:
                return {"error": str(exc)}
            effective_now = max(now_ts(), _THOUGHTS_CLOCK.get(clock_key, 0.0))
            disk_row = disk.get(sid) if sid else None
            known = sid if (disk_row is not None and
                            as_float(disk_row.get("updated_at"), 0.0) > effective_now - THOUGHT_TTL_S) else ""
            if require_existing and not known:
                return {"error": "読み取り後に思考台帳が削除または期限切れになりました。記録していません。"}
            base = disk[known] if known else {}
            target = known or new_thought_id()
            row = _deep_copy(base)
            steps = [s for s in (row.get("steps") or []) if isinstance(s, dict)]
            step = dict(step)
            if not _thought_subgoal_still_matches(row, steps, step, ops or {}):
                return {"error": "計画が並行して変更され、subgoal の参照先が変わりました。記録していません。台帳を確認して再試行してください。"}
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
            history, omitted = _thought_verifier_update(row, verifier_models, verifier_snapshot, verifier_additions,
                                                         verifier_snapshot_generation)
            row.update({
                "steps": steps,
                # KANBAN のカード ID（任意）。渡された値が優先で、未指定なら既存を保つ（§6.9 の card 引数）。
                "card": card or row.get("card") or "",
                "question": question or row.get("question") or "",
                "verifier_models": history,
                "verifier_generation": row.get("verifier_generation", 0) + (verifier_snapshot is not None),
                "created_at": row.get("created_at") or effective_now,
                "updated_at": max(as_float(base.get("updated_at"), 0.0), effective_now),
            })
            disk[target] = row
            retained = _thoughts_prune(disk, effective_now)
            if target not in retained:
                return {"error": "思考台帳の容量または期限により記録できませんでした。"}
            if not _thoughts_write_locked(path, retained):
                return {"error": "思考台帳の保存に失敗しました。記録していません。"}
            _THOUGHTS_CLOCK[clock_key] = effective_now
            _THOUGHTS.clear()
            _THOUGHTS.update(retained)
            _THOUGHTS_LOADED = True
    return {"session_id": target, "step": step, "steps_recorded": len(steps),
            "refused": False, "replaced": replaced, "verifier_omitted": omitted}


def thought_find_by_card(card: str) -> str:
    """カード ID に紐づく台帳の `session_id` を返す（無ければ ""）。新しい順に 1 つだけ。

    KANBAN のワーカーが `card` に自分のカード ID（t_xxxx）を渡すと、思考がカードに紐づく。台帳は
    上限 `THOUGHT_MAX` 件しか持たないので線形走査で足りる。**複数のワーカーが同じカードを触っても
    最後に更新された台帳を返す**（カード 1 枚 = 1 本の思考の道筋、という運用を想定）。
    """
    card = as_str(card).strip()
    if not card:
        return ""
    with _THOUGHTS_LOCK:
        _ensure_thoughts_loaded()
        hits = [(as_float((row or {}).get("updated_at"), 0.0), sid)
                for sid, row in _THOUGHTS.items()
                if isinstance(row, dict) and as_str(row.get("card")).strip() == card]
    return max(hits)[1] if hits else ""


def thought_drop(sid: str) -> None:
    global _THOUGHTS_LOADED
    if not sid:
        return
    with _THOUGHTS_LOCK:
        if not THOUGHTS_ENABLED:
            _THOUGHTS.pop(sid, None)
            return
        path = thoughts_path()
        clock_key = os.path.normcase(os.path.abspath(path))
        with _file_lock(path) as locked:
            if not locked:
                raise OSError("思考台帳のロックを取得できませんでした")
            disk = _thoughts_disk_chains(path, strict=True)
            disk.pop(sid, None)
            effective_now = max(now_ts(), _THOUGHTS_CLOCK.get(clock_key, 0.0))
            retained = _thoughts_prune(disk, effective_now)
            if not _thoughts_write_locked(path, retained):
                raise OSError("思考台帳の保存に失敗しました")
            _THOUGHTS_CLOCK[clock_key] = effective_now
            _THOUGHTS.clear()
            _THOUGHTS.update(retained)
            _THOUGHTS_LOADED = True


# ---------------------------------------------------------------- §2.7 思考台帳の構造（分解・改訂・分岐・仮説）
#
# 台帳を「思考の列」から**構造**へ上げる操作。`thought_merge` が**ロック内**で呼ぶ（読みと書きを
# 分けると並列呼び出しで印が消える。規約 24b）。引数の検証は §6.10 の `_think_structure` が**先に**
# 済ませる（検証に失敗した呼び出しでサブ呼び出しの予算を使わないため）。subgoal は保存時にも
# 最新の台帳で再検査する。その他の操作はここで検証済みの対象に適用する。
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


def _plan_merge(old_items: list[dict], old_retired: list[dict],
                new_texts: list[str]) -> tuple[list[dict], list[dict]]:
    """計画の差し替え（新しい plan と、廃止された項目）。

    **同じ文面は同じ id を保つ**。id は「そのサブ目標の同一性」なので、差し替えで
    入れ替わると、それまで `subgoal` で参照していたステップが**別の項目を指すようになる**
    （実測: `plan=["A","B"]` で A 完了 → `plan=["B"]` で B が id 1 になり、ステップ 1 が
    A についてのものだったのに B に付け替わる）。

    * 同じ文面は元の id と `done_at`（達成済みの印）を引き継ぐ。
    * 新しい文面は**使っていない id**を割り当てる（別項目の id を再利用しない）。
    * 抜けた項目は**廃止**として残す（達成済みの記録と参照を黙って消さない）。
      廃止項目も `subgoal` の参照先として有効なので、参照が宙に浮かない。
    """
    by_text: dict[str, dict] = {}
    for item in list(old_items) + list(old_retired):
        text = as_str(item.get("text"))
        if text and text not in by_text:
            by_text[text] = item
    used_ids = {as_int(item.get("id"), 0, 0, 9999)
                for item in list(old_items) + list(old_retired)}
    next_id = max(used_ids or {0}) + 1
    new_plan: list[dict] = []
    for text in new_texts:
        prev = by_text.get(text)
        if prev is not None:
            new_plan.append({"id": as_int(prev.get("id"), 0, 0, 9999), "text": text,
                             "done_at": prev.get("done_at")})
        else:
            new_plan.append({"id": next_id, "text": text, "done_at": None})
            next_id += 1
    wanted = set(new_texts)
    new_retired = [dict(item) for item in old_retired
                   if as_str(item.get("text")) not in wanted]
    for item in old_items:
        if as_str(item.get("text")) not in wanted:
            new_retired.append(dict(item))
    return new_plan, new_retired


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
    retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    if ops.get("plan"):
        # 同じ文面は id と達成済みの印を引き継ぐ。新しい文面には**未使用の id**を割り当てる
        # （id を再利用すると、既存の subgoal 参照が別の項目を指すようになる）。
        plan, retired = _plan_merge(plan, retired, list(ops["plan"][:THOUGHT_PLAN_MAX]))
        # ここに来る前に `_think_args` が上限超過を fail で止めるので、この切り詰めは
        # **防御用**（呼び出し経路が増えても台帳が壊れないように）。規約 63 の対象は
        # 「実行数を減らして黙って続行する」箇所で、ここは契約違反として拒否する側。
        row["plan_revised_at"] = n
    # `subgoal` は**解決済みの plan 項目の id**（§6.10 の `_think_structure` が解決する）。
    # id は差し替えをまたいで 12 を超えることがあるので上限を広く取る（THOUGHT_PLAN_MAX は位置数）。
    sub = as_int(step.get("subgoal"), 0, 0, 9999)
    if sub and ops.get("subgoal_done"):
        for item in plan:
            if as_int(item.get("id"), 0, 0, 9999) == sub:
                item["done_at"] = n
    row["plan"] = plan
    row["plan_retired"] = retired
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


class _ShortConnectMixin:
    """接続の間だけ接続上限を使い、接続後は urllib が渡した読み取り上限へ戻す。

    実測（Python 3.11〜3.14）: `urlopen(timeout=…)` の上限は socket に残るため、応答ヘッダを
    受信するまで読み取り上限へ切り替わらない。生成完了までヘッダを返さないローカルプロキシでは、
    生成が `CONNECT_TIMEOUT`（既定 10 秒）を超えると `READ_TIMEOUT`（既定 180 秒）が使われずに
    `TimeoutError` になった（実測: 1200 トークンの要求が 10.0 秒で失敗）。接続だけを短い上限に
    絞ることで、**不通ホストへの fail fast は保ったまま**長時間の生成を待てる。
    """

    def connect(self):
        read_timeout = self.timeout
        self.timeout = CONNECT_TIMEOUT
        try:
            super().connect()  # 接続と TLS ハンドシェイクだけを接続上限で打ち切る
        finally:
            self.timeout = read_timeout
        self.sock.settimeout(read_timeout)


class _ShortConnectHTTPConnection(_ShortConnectMixin, http.client.HTTPConnection):
    pass


class _ShortConnectHTTPSConnection(_ShortConnectMixin, http.client.HTTPSConnection):
    pass


class _HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_ShortConnectHTTPConnection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        # 版差: 3.13 以前の HTTPSHandler は check_hostname を保持して do_open へ渡すが、
        # 3.14 以降は context だけを渡す（HTTPSConnection も check_hostname を受け付けない）。
        # 実測: 3.14 で `self._check_hostname` を参照すると AttributeError になった。
        kwargs = {"context": self._context}
        check_hostname = getattr(self, "_check_hostname", None)
        if check_hostname is not None:
            kwargs["check_hostname"] = check_hostname
        return self.do_open(_ShortConnectHTTPSConnection, req, **kwargs)


# build_opener はサブクラスを既定ハンドラの置換と見なすので、リダイレクト（HTTPRedirectHandler）・
# プロキシ（ProxyHandler）・HTTPError の既定挙動はそのまま残る（標準ライブラリのみ・依存ゼロ）。
_OPENER = urllib.request.build_opener(_HTTPHandler, _HTTPSHandler)
_SSL_REFRESH_LOCK = threading.Lock()


def _refresh_ssl_context(url: str) -> None:
    """Windows で未訪問のルート CA（例: Starfield Root G2 → export.arxiv.org）がストアに無く
    `CERTIFICATE_VERIFY_FAILED` になったとき、Schannel 経由の HEAD で Windows CryptoAPI の
    自動ルート証明書取得を促し、共有 `_OPENER` の `SSLContext` を作り直す（検証は無効化しない）。
    """
    with _SSL_REFRESH_LOCK:
        if os.name == "nt" and isinstance(url, str) and url.startswith("https://"):
            curl_bin = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                                    "System32", "curl.exe")
            if os.path.exists(curl_bin):
                try:
                    _nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                    subprocess.run([curl_bin, "-sI", "--max-time", "5", url],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=6, check=False, creationflags=_nw)
                except Exception:
                    pass
        ctx = ssl.create_default_context()
        for handler in getattr(_OPENER, "handlers", ()):
            if isinstance(handler, _HTTPSHandler):
                handler._context = ctx


def _urlopen(req: urllib.request.Request, timeout: float):
    """接続は短い上限で、接続後のヘッダ待ちと本文読取は `timeout` で打ち切る。"""
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            _refresh_ssl_context(getattr(req, "full_url", ""))
            return _OPENER.open(req, timeout=timeout)
        raise


def provider_http(path: str, provider: str = "nous", payload: dict | None = None,
                  timeout: float | None = None) -> dict:
    spec = PROVIDER_SPECS[provider]
    if payload is not None and not provider_ready(provider):
        missing = _provider_missing_settings(spec)
        raise RuntimeError(f"{provider} は未設定/未確認です。必要なenv設定: {', '.join(missing)}")
    url = f"{spec['base_url'].rstrip('/')}{path}"
    headers = {"Authorization": f"Bearer {spec['key']}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = _dumps(payload).encode("utf-8")     # 孤立サロゲート・NaN を除いた UTF-8（上流が 400 にしない）
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with _urlopen(req, timeout or READ_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as exc:
        # HTML のエラーページ等を 200 で返す上流がある。素の「Expecting value」では原因が分からない。
        raise ValueError(f"上流の応答が JSON ではありません（{type(exc).__name__}）: {truncate(body, 80)}") from exc


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
#
# **429 を踏まないための一次防護**。従来は「プロセス全体の `MAX_WORKERS`」しか無く、429 の防護は
# 事後のクールダウン（そのモデルを数分避ける）だけだった。Free の在庫が 1 提供元に偏ると
# 「並列度を上げる＝同じ提供元へ同時に投げる」になるので、上流の枠を超えて 429 を踏む。
#   * 提供元ごとに「同時に投げてよい本数（枠）」を持ち、超える呼び出しは**待つ**（429 にしない）
#   * 429 を受けたらその提供元の枠を 1 本減らし（最低 1 本）、連続成功が `_GATE_RESTORE_AFTER`
#     回続いたら 1 本戻す（上限は実測値または `FREEAGENT_PROVIDER_LIMITS` の明示）
#   * 待ちには上限（`FREEAGENT_GATE_WAIT_S` 既定 20 秒）を置き、取れない呼び出しは
#     **脱落として返す**（無限待ちを作らない。規約 8 / 規約 27）
# 実測（2026-10-23・nous）:
#   * 生の並列度（同一プロンプト × 12 呼び出し・fallback 無し）: 429 は 0 件、壁時計は 4 並列 13.13 秒 /
#     8 並列 8.08 秒 / 12 並列 13.78 秒（最大 1 件の遅延 12.2 秒）→ **並列は 8 本まで効く**
#   * 8 モデルの panel（max_tokens 128）: 枠 8 だと HTTP 429 が 1〜2 件（`The requested model is
#     temporarily at capacity upstream`）／枠 6 なら 0〜1 件。**429 の主因はモデル単位の upstream
#     capacity** で、こちらの同時数を下げても 0 にはならない。だから枠は「自爆を減らす」一次防護で
#     あって万能ではない（受けたら縮めて、次から待ちに変える）
# 提供元ごとに枠を分けるのは、**別ホストの提供元なら同時に走らせてよい**（上流の枠は提供元ごとに
# 独立）ためで、全体の並列度 1 本ではそれを表現できない。

class ProviderBusyError(Exception):
    """提供元の同時実行枠が取れなかった（＝429 を踏む代わりに待って諦めた）。"""


_PROVIDER_LIMIT_DEFAULT = max(1, min(16, _env_int("FREEAGENT_PROVIDER_LIMIT", 4)))
# 実測が取れている提供元だけ個別の既定を持つ。未実測の提供元は保守的に `_PROVIDER_LIMIT_DEFAULT`。
# nous の実測（8 モデルの panel・max_tokens 128・各条件 2〜3 回）:
#   枠 8 → HTTP 429 が 1〜2 件 / 回答 4〜5/8 / 壁 11.3 秒
#   枠 6 → HTTP 429 が 0〜1 件 / 回答 6〜7/8 / 壁 6.0〜6.3 秒
#   枠 4 → HTTP 429 が 1 件 / 回答 6/8 / 壁 4.4〜6.7 秒
# → 6 を既定にする（膝の内側。**429 の主因はモデル単位の upstream capacity** で、
#   こちらの同時数を下げても 0 にはならない。だから枠は「自爆を減らす」ためのもの）。
_PROVIDER_LIMIT_MEASURED = {"nous": 6}
GATE_WAIT_S = max(1.0, min(120.0, _env_float("FREEAGENT_GATE_WAIT_S", 20.0)))
_GATE_RESTORE_AFTER = max(1, _env_int("FREEAGENT_GATE_RESTORE_AFTER", 3))
_GATE_COUNT_WAIT_S = 0.05      # これ以上待ったら「枠待ちが起きた」として数える


def _parse_provider_limits(raw: str, valid) -> dict[str, int]:
    """`FREEAGENT_PROVIDER_LIMITS=openrouter=2,huggingface=1` を読む（未知の名前は無視）。"""
    out: dict[str, int] = {}
    for part in (raw or "").split(","):
        name, sep, value = part.partition("=")
        name = name.strip().lower()
        if not sep or name not in valid:
            continue
        try:
            out[name] = max(1, min(16, int(value.strip())))
        except ValueError:
            continue
    return out


_PROVIDER_LIMITS = _parse_provider_limits(os.environ.get("FREEAGENT_PROVIDER_LIMITS", ""),
                                          set(PROVIDER_SPECS))


def provider_slot_cap(provider: str) -> int:
    """その提供元の同時実行枠の上限（`FREEAGENT_PROVIDER_LIMITS` > 実測既定 > 全体既定）。"""
    if provider in _PROVIDER_LIMITS:
        return _PROVIDER_LIMITS[provider]
    if provider in _PROVIDER_LIMIT_MEASURED:
        return _PROVIDER_LIMIT_MEASURED[provider]
    return _PROVIDER_LIMIT_DEFAULT


def _gate_provider_names(items) -> list[str]:
    """提供元名の一覧を作る。**ref（provider/model）でも受ける**（呼び出し側で迷わないため）。"""
    out: list[str] = []
    for item in items or []:
        name = as_str(item)
        if not name:
            continue
        head, sep, _ = name.partition("/")
        if sep and head in PROVIDER_SPECS:
            name = head
        if name not in out:
            out.append(name)
    return out


class _ProviderGate:
    """提供元ごとの同時実行枠。**429 で縮み、成功が続けば戻る**（固定のセマフォでは足りない）。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._in_flight: dict[str, int] = {}
        self._limit: dict[str, int] = {}
        self._success: dict[str, int] = {}
        self._reduced: dict[str, int] = {}
        self._waited: dict[str, int] = {}
        self._timeouts: dict[str, int] = {}

    def limit(self, provider: str) -> int:
        with self._cond:
            return self._limit.get(provider) or provider_slot_cap(provider)

    def acquire(self, provider: str, timeout: float | None = None) -> float | None:
        """枠を取る。取れたら**待ち秒**（待たなければ 0.0）、取れなければ None を返す。"""
        budget = GATE_WAIT_S if timeout is None else max(0.0, float(timeout))
        started = time.monotonic()
        end = started + budget
        with self._cond:
            cap = provider_slot_cap(provider)
            while self._in_flight.get(provider, 0) >= (self._limit.get(provider) or cap):
                remain = end - time.monotonic()
                if remain <= 0:
                    self._timeouts[provider] = self._timeouts.get(provider, 0) + 1
                    return None
                self._cond.wait(min(remain, 0.25))
            self._in_flight[provider] = self._in_flight.get(provider, 0) + 1
        waited = time.monotonic() - started
        if waited >= _GATE_COUNT_WAIT_S:
            with self._cond:
                self._waited[provider] = self._waited.get(provider, 0) + 1
        return round(waited, 3)

    def release(self, provider: str) -> None:
        with self._cond:
            left = self._in_flight.get(provider, 0) - 1
            if left > 0:
                self._in_flight[provider] = left
            else:
                self._in_flight.pop(provider, None)
            self._cond.notify_all()

    def note_success(self, provider: str) -> None:
        """連続成功で枠を 1 本ずつ戻す（429 で縮んだ提供元を、踏まなくなったら回復させる）。"""
        with self._cond:
            cap = provider_slot_cap(provider)
            cur = self._limit.get(provider)
            if cur is None or cur >= cap:
                return
            self._success[provider] = self._success.get(provider, 0) + 1
            if self._success[provider] >= _GATE_RESTORE_AFTER:
                self._success[provider] = 0
                self._limit[provider] = cur + 1

    def note_rate_limited(self, provider: str) -> tuple[int, int]:
        """429。枠を 1 本減らして (前, 後) を返す（待ち行列の呼び出しはそのまま進む）。"""
        with self._cond:
            cap = provider_slot_cap(provider)
            cur = self._limit.get(provider) or cap
            new = max(1, cur - 1)
            self._limit[provider] = new
            self._success[provider] = 0
            if new < cap:
                self._reduced[provider] = self._reduced.get(provider, 0) + 1
            self._cond.notify_all()
            return cur, new

    def report(self, providers: list[str] | None = None) -> dict:
        """今の枠と待ちの実績（返り値に載せて「429 を踏まずに待った」ことを示す）。

        `providers` は**提供元名でも ref（provider/model）でも受ける**（実測: 参加モデルの ref を
        そのまま渡すと、キーが提供元名でなく ref になり `nous/…` という架空の提供元が並んだ）。
        """
        with self._cond:
            names = _gate_provider_names(providers if providers is not None else list(PROVIDER_ORDER))
            per = {}
            for name in names:
                cap = provider_slot_cap(name)
                per[name] = {"limit": self._limit.get(name) or cap, "cap": cap,
                             "in_flight": self._in_flight.get(name, 0)}
            return {"per_provider": per, "waited_total": sum(self._waited.values()),
                    "reduced_total": sum(self._reduced.values()),
                    "timeouts_total": sum(self._timeouts.values()),
                    "scope": "このプロセスの累計"}

    def reset(self) -> None:
        """テスト用。学習した枠と実績を初期化する。"""
        with self._cond:
            self._in_flight.clear()
            self._limit.clear()
            self._success.clear()
            self._reduced.clear()
            self._waited.clear()
            self._timeouts.clear()
            self._cond.notify_all()


PROVIDER_GATE = _ProviderGate()


# ---------------------------------------------------------------- §3.5 提供元のクォータ窓
#
# Free の上限は「分あたりの回数」だけでなく**日あたり**で切られる（公開情報: OpenRouter の無料枠は
# 1,000 リクエスト/日、ModelScope は 2,000/日）。日次は 429 と違って**静かに使い切って翌日まで
# 全部失敗**になるので、同時実行の枠（§3.4）だけでは守れない。
#   * **宣言された上限だけ**を守る（`FREEAGENT_QUOTA_LIMITS="openrouter:rpd=1000,nous:rpm=60"`）。
#     宣言が無ければ何もしない（推測で止めない・状態も書かない＝既定の挙動を変えない）。
#   * 単位は提供元全体（提供元は口座単位で数えることが多い）。rpm / rpd / tpm / tpd。
#   * 分は直近 60 秒、日は **UTC の日付境界**（提供元は 00:00 UTC に戻す）。
#   * 超過する呼び出しは 429 を踏む前に `QuotaExhaustedError` で**脱落**にする（理由つき）。

_QUOTA_UNITS = ("rpm", "rpd", "tpm", "tpd")


class QuotaExhaustedError(Exception):
    """宣言されたクォータ（分次・日次）を使い切った（429 を踏む前に止めた）。"""


def _parse_quota_limits(raw: str, valid) -> dict[str, dict[str, int]]:
    """`openrouter:rpd=1000,nous:rpm=60` を読む（未知の提供元名・壊れた値は無視）。"""
    out: dict[str, dict[str, int]] = {}
    for part in (raw or "").replace(";", ",").split(","):
        name, sep, spec = part.partition(":")
        name = name.strip().lower()
        if not sep or name not in valid:
            continue
        for item in spec.split("+"):
            key, eq, value = item.partition("=")
            key = key.strip().lower()
            if not eq or key not in _QUOTA_UNITS:
                continue
            try:
                out.setdefault(name, {})[key] = max(1, int(value.strip()))
            except ValueError:
                continue
    return out


_QUOTA_LIMITS = _parse_quota_limits(os.environ.get("FREEAGENT_QUOTA_LIMITS", ""), set(PROVIDER_SPECS))
QUOTA_ENABLED = _env_on("FREEAGENT_QUOTA", True) and bool(_QUOTA_LIMITS)
_QUOTA: dict[str, dict] = {}
_QUOTA_LOADED = False
_QUOTA_MTIME: float | None = None
_QUOTA_LOCK = threading.Lock()
_QUOTA_MINUTE_S = 60.0


def quota_path() -> str:
    return os.environ.get("FREEAGENT_QUOTA_PATH") or os.path.join(state_dir(), "quota.json")


def _quota_row(provider: str, now: float) -> dict:
    """分・日の窓を必要なら進める（分は直近 60 秒、日は UTC の日付境界）。"""
    row = _QUOTA.setdefault(provider, {})
    today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date()
    day = today.isoformat()
    if row:
        prior = row.get("day")
        try:
            prior_date = datetime.date.fromisoformat(prior) if isinstance(prior, str) else None
        except ValueError:
            prior_date = None
        if prior_date is None or prior_date.isoformat() != prior or prior_date > today:
            raise QuotaExhaustedError("クォータの日付が不正です。送信を中止しました")
    if row.get("day") != day:
        row["day"] = day
        row["day_req"] = 0
        row["day_tok"] = 0
    for key in ("day_req", "day_tok"):
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise QuotaExhaustedError("クォータの日次集計が不正です。送信を中止しました")
    if "minute_events" not in row:
        start = as_float(row.get("min_start"), 0.0)
        req = int(row.get("min_req") or 0)
        tok = int(row.get("min_tok") or 0)
        # Legacy fixed-window counts have no individual timestamps. Charge them
        # at the latest possible time, never earlier than the original events.
        row["minute_events"] = [[start + _QUOTA_MINUTE_S, req, tok]] if req or tok else []
    raw_events = row["minute_events"]
    if (not isinstance(raw_events, list) or any(
        not isinstance(e, list) or len(e) != 3
        or isinstance(e[0], bool) or not isinstance(e[0], (int, float)) or not math.isfinite(e[0])
        or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in e[1:])
        for e in raw_events
    )):
        raise QuotaExhaustedError("クォータの分次履歴が不正です。送信を中止しました")
    if ("min_req" in row and row["min_req"] != sum(e[1] for e in raw_events)
            or "min_tok" in row and row["min_tok"] != sum(e[2] for e in raw_events)):
        raise QuotaExhaustedError("クォータの分次集計が履歴と不一致です。旧版との併用を停止してください")
    events = [e for e in raw_events if e[0] > now - _QUOTA_MINUTE_S]
    row["minute_events"] = events
    row["min_req"] = sum(int(e[1]) for e in events)
    row["min_tok"] = sum(int(e[2]) for e in events)
    return row


def _quota_read(path: str) -> dict:
    """`quota.json` の `providers` を読む（**ロックの内側で呼ぶ**）。"""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise QuotaExhaustedError("クォータ状態を読めません。送信を中止しました") from exc
    rows = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(rows, dict) or any(not isinstance(row, dict) for row in rows.values()):
        raise QuotaExhaustedError("クォータ状態の形式が不正です。送信を中止しました")
    return rows


def _ensure_quota_loaded() -> None:
    global _QUOTA_LOADED, _QUOTA_MTIME
    if not QUOTA_ENABLED:
        return
    path = quota_path()
    mtime = _file_mtime(path)
    # **別プロセスの使用量も見る**（v0.25.0）。1 回だけ読むと、常駐 HTTP と stdio が同じ上限を
    # 別々に数えて **合算で上限を超える**（プロセス数だけ枠を食い潰す）。
    if _QUOTA_LOADED and (mtime is None or mtime == _QUOTA_MTIME):
        return
    with _QUOTA_LOCK:
        if _QUOTA_LOADED and (mtime is None or mtime == _QUOTA_MTIME):
            return
        _QUOTA.update(_quota_read(path))
        _QUOTA_LOADED = True
        _QUOTA_MTIME = mtime


def _quota_write(rows: dict) -> bool:
    """ロックの内側から呼ぶ（`providers` 全体を書く）。"""
    ok = _atomic_write(quota_path(), _dumps({"version": 1, "providers": rows}))
    global _QUOTA_MTIME
    _QUOTA_MTIME = _file_mtime(quota_path())
    return ok


def _quota_save() -> bool:
    if not QUOTA_ENABLED:
        return False
    with _file_lock(quota_path()):
        with _QUOTA_LOCK:
            payload = copy.deepcopy(_QUOTA)
        return _quota_write(payload)


def _quota_used(row: dict) -> dict[str, int]:
    reservations = row.get("pending", {})
    if not isinstance(reservations, dict) or any(
        not isinstance(v, dict) or isinstance(v.get("tokens"), bool)
        or not isinstance(v.get("tokens"), int) or v["tokens"] < 0
        for v in reservations.values()
    ):
        raise QuotaExhaustedError("クォータの予約状態が不正です。送信を中止しました")
    pending = sum(v["tokens"] for v in reservations.values())
    return {"rpm": int(row.get("min_req") or 0), "rpd": int(row.get("day_req") or 0),
            "tpm": int(row.get("min_tok") or 0) + pending,
            "tpd": int(row.get("day_tok") or 0) + pending}


def _quota_reason(provider: str, used: dict[str, int], limits: dict[str, int]) -> str | None:
    for unit in _QUOTA_UNITS:
        limit = limits.get(unit)
        if limit and used[unit] >= limit:
            when = "次の 60 秒" if unit in ("rpm", "tpm") else "UTC の日付が変わるまで"
            return (f"提供元 {provider} の {unit.upper()} 上限 {limit} に達しました（使用 {used[unit]}）。"
                    f"{when}待つか、FREEAGENT_QUOTA_LIMITS を見直してください")
    return None


def quota_block(provider: str) -> str | None:
    """**読むだけ**の判定（消費しない）。1 回分を確保するのは `quota_reserve`。"""
    limits = _QUOTA_LIMITS.get(provider) if QUOTA_ENABLED else None
    if not limits:
        return None
    _ensure_quota_loaded()
    with _QUOTA_LOCK:
        used = _quota_used(_quota_row(provider, now_ts()))
    return _quota_reason(provider, used, limits)


def quota_reserve(provider: str, tokens: int = 0, reservation_id: str = "", *,
                  output_cap: list[int] | None = None, input_tokens: int = 0) -> str | None:
    """1 リクエスト分を**先に確保**する（check-then-act の穴を塞ぐ）。

    上限判定と加算を 1 つのロック内で行う。呼び出し側は「送る直前に確保 → 送る → トークンだけ足す」の
    順にする。確保しないと、並列 N 本が同じ残量を読んで**全員が通る**（実測: 枠 4 本・上限 3 で 4 本通った）。

    Token reservations are shared by processes using this state file. The estimate
    is not an upper bound on provider usage; upstream limits remain authoritative.
    Unknown or interrupted usage remains pending until explicitly reconciled.
    """
    limits = _QUOTA_LIMITS.get(provider) if QUOTA_ENABLED else None
    if not limits:
        return None
    # **プロセス間でも check-then-act を 1 つのクリティカルセクションにする**（v0.25.0）。
    # ロックの内側でディスクの最新を取り込み、判定と加算を同じ写しに対して行う。
    # プロセス内の `threading.Lock` だけでは、常駐 HTTP と stdio が同じ上限を別々に数える。
    path = quota_path()
    with _file_lock(path) as locked:
        if not locked:
            return f"提供元 {provider} のクォータのロックを取得できませんでした"
        with _QUOTA_LOCK:
            _QUOTA.clear()
            try:
                _QUOTA.update(_quota_read(path))
                row = _quota_row(provider, now_ts())
                used = _quota_used(row)
            except QuotaExhaustedError as exc:
                return str(exc)
            reason = _quota_reason(provider, used, limits)
            if reason:
                return reason
            if output_cap is not None:
                remaining = [limits[u] - used[u] for u in ("tpm", "tpd") if limits.get(u)]
                if remaining:
                    allowed = min(remaining) - input_tokens
                    if allowed < 1:
                        return f"提供元 {provider} のクォータ残量に入力と出力 1 トークンが収まりません"
                    output_cap[0] = min(output_cap[0], allowed)
                    tokens = input_tokens + output_cap[0]
            for unit in ("tpm", "tpd"):
                if limits.get(unit) and used[unit] + tokens > limits[unit]:
                    return f"提供元 {provider} の {unit.upper()} 上限 {limits[unit]} に予約量が収まりません"
            if tokens and reservation_id:
                row.setdefault("pending", {})[reservation_id] = {"tokens": tokens, "at": now_ts()}
            row["minute_events"].append([now_ts(), 1, 0])
            row["min_req"] = used["rpm"] + 1
            row["day_req"] = used["rpd"] + 1
            payload = copy.deepcopy(_QUOTA)
            if not _quota_write(payload):
                _QUOTA.clear()
                _QUOTA.update(_quota_read(path))
                return f"提供元 {provider} のクォータの保存に失敗しました"
    return None


def quota_add_tokens(provider: str, tokens: int = 0, reservation_id: str = "") -> None:
    """消費したトークンを足す（リクエスト数は `quota_reserve` で確保済み）。"""
    if tokens <= 0 or not QUOTA_ENABLED or not _QUOTA_LIMITS.get(provider):
        return
    path = quota_path()
    with _file_lock(path) as locked:
        if not locked:
            raise QuotaExhaustedError(f"提供元 {provider} のクォータ精算ロックを取得できませんでした")
        with _QUOTA_LOCK:
            _QUOTA.clear()
            _QUOTA.update(_quota_read(path))
            row = _quota_row(provider, now_ts())
            if reservation_id:
                (row.get("pending") or {}).pop(reservation_id, None)
            row["minute_events"].append([now_ts(), 0, int(tokens)])
            row["min_tok"] = int(row.get("min_tok") or 0) + int(tokens)
            row["day_tok"] = int(row.get("day_tok") or 0) + int(tokens)
            payload = copy.deepcopy(_QUOTA)
            if not _quota_write(payload):
                _QUOTA.clear()
                _QUOTA.update(_quota_read(path))
                raise QuotaExhaustedError(f"提供元 {provider} のクォータ精算の保存に失敗しました")


def quota_record(provider: str, tokens: int = 0) -> None:
    """1 回分（リクエスト＋トークン）を記録する。上限なら何もせず False 相当を返す。"""
    if quota_reserve(provider) is not None:
        return
    quota_add_tokens(provider, tokens)


def quota_report(providers: list[str] | None = None) -> dict | None:
    """今の使用量（宣言がある提供元だけ）。宣言が無ければ None。"""
    if not QUOTA_ENABLED:
        return None
    names = [n for n in _gate_provider_names(providers if providers is not None else list(_QUOTA_LIMITS))
             if _QUOTA_LIMITS.get(n)]
    if not names:
        return None
    _ensure_quota_loaded()
    now = now_ts()
    out: dict[str, dict] = {}
    with _QUOTA_LOCK:
        for name in names:
            row = _quota_row(name, now)
            used = _quota_used(row)
            limits = _QUOTA_LIMITS.get(name) or {}
            out[name] = {unit: {"used": used[unit], "limit": limits[unit]}
                         for unit in _QUOTA_UNITS if limits.get(unit)}
            pending = row.get("pending", {})
            out[name]["pending_reservations"] = len(pending)
            out[name]["reserved_tokens"] = sum(v["tokens"] for v in pending.values())
    # v0.25.0: 同じ `FREEAGENT_STATE_DIR` を共有する限り**プロセスをまたいで合算**される
    # （以前はプロセスごとに別々に数えていたので、2 プロセスで上限の 2 倍まで通った）。
    return {"providers": out, "scope": "状態ディレクトリを共有するプロセスの合算（提供元単位・UTC 日境界）"}


def quota_reset() -> None:
    """テスト用。学習した使用量を初期化する。"""
    with _QUOTA_LOCK:
        _QUOTA.clear()


def _quota_report_after_answers(refs: list[str]) -> dict:
    try:
        return {"quota": quota_report(refs)}
    except Exception as exc:
        return {"quota": None, "quota_report_error": str(exc)}


def _result_tokens(result: dict) -> int:
    """使用トークン数（`tokens` は dict / 数値 / 欠落のどれでも来る＝テストのモックで実測した）。"""
    tokens = result.get("tokens") if isinstance(result, dict) else None
    if isinstance(tokens, bool):
        return 0
    if isinstance(tokens, (int, float)):
        if isinstance(tokens, float) and (not math.isfinite(tokens) or not tokens.is_integer()):
            return 0
        return int(tokens)
    if not isinstance(tokens, dict):
        return 0
    if any(not isinstance(tokens.get(key), (int, float)) or isinstance(tokens.get(key), bool)
           or tokens[key] < 0
           or (isinstance(tokens[key], float)
               and (not math.isfinite(tokens[key]) or not tokens[key].is_integer()))
           for key in ("prompt", "completion")):
        return 0
    total = 0
    for key in ("prompt", "completion"):
        value = tokens.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total


def _gated_call_once(provider: str, model: str, prompt: str, system: str,
                     max_tokens: int, temperature: float | None,
                     timeout: float | None = None,
                     wait_budget: float | None = None) -> tuple[dict, float]:
    """提供元の枠（§3.4・クォータ §3.5）を取って `_call_once` する。`(結果, 待ち秒)` を返す。

    枠が取れないときは **429 を踏む代わりに待って諦める**（`ProviderBusyError`）。
    宣言されたクォータを使い切っていれば `QuotaExhaustedError`（待たずに別の提供元へ回す）。
    `wait_budget` は 1 つの `call_model` が候補を回るあいだに使ってよい待ち時間の残り。
    """
    waited = PROVIDER_GATE.acquire(provider, timeout=wait_budget)
    if waited is None:
        raise ProviderBusyError(
            f"提供元 {provider} の同時実行枠（{PROVIDER_GATE.limit(provider)} 本）が埋まったままで、"
            f"{GATE_WAIT_S:g} 秒待っても取れませんでした。size を減らす／`FREEAGENT_PROVIDER_LIMITS` で"
            "枠を上げる／別の提供元を有効にする、のいずれかで解消します")
    try:
        # 送る**直前**に 1 回分を確保する（並列 N 本が同じ残量を読んで全員通るのを防ぐ・§3.5）
        limits = _QUOTA_LIMITS.get(provider, {}) if QUOTA_ENABLED else {}
        # UTF-8 bytes plus max output is a conservative heuristic, not a
        # tokenizer-independent upper bound (provider framing can add tokens).
        input_estimate = len((prompt + system).encode("utf-8"))
        token_limits = [limits[u] for u in ("tpm", "tpd") if limits.get(u)]
        cap = [max_tokens]
        reservation_id = os.urandom(16).hex() if token_limits else ""
        exhausted = (quota_reserve(provider, reservation_id=reservation_id, output_cap=cap,
                                   input_tokens=input_estimate) if token_limits else quota_reserve(provider))
        if exhausted:
            raise QuotaExhaustedError(exhausted)
        effective_max = cap[0]
        result = _call_once(provider, model, prompt, system, effective_max, temperature,
                            timeout=timeout)
    finally:
        PROVIDER_GATE.release(provider)
    if effective_max != max_tokens:
        result["quota_accounting"] = f"max_tokens_capped: {max_tokens} -> {effective_max}"
    actual_tokens = _result_tokens(result)
    try:
        quota_add_tokens(provider, actual_tokens, reservation_id)
    except Exception as exc:
        warning = f"settlement_failed: {exc}"
        result["quota_accounting"] = "; ".join(filter(None, (result.get("quota_accounting"), warning)))
        return result, waited
    if reservation_id and actual_tokens <= 0:
        result["quota_accounting"] = "; ".join(filter(None, (result.get("quota_accounting"),
                                                               "usage_unknown_reservation_retained")))
    return result, waited


# 上流（外部の Free モデル）へ送る直前に秘密を伏せる。**誤爆より見逃しの方が高くつく**ので、
# 形がはっきりしたもの（既知の接頭辞・JWT・秘密鍵ブロック）だけを対象にする。
# 伏せた件数は返り値の `redactions` に載せ、呼び出し側が「送る前に消えた」ことを見せる。
REDACT_SECRETS = as_flag(os.environ.get("FREEAGENT_REDACT_SECRETS", "1"))
_SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED:sk]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"), "[REDACTED:ghp]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED:github_pat]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED:aws]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED:slack]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "[REDACTED:google]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b"),
     "[REDACTED:jwt]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private-key]"),
]


def scrub_secrets(text: str, enabled: bool | None = None) -> tuple[str, int]:
    """秘密を伏せた本文と、伏せた件数を返す。既定は有効（`FREEAGENT_REDACT_SECRETS=0` で無効）。

    上流は外部の無料モデルなので、鍵が混ざったまま送ると取り消しが効かない。
    """
    if enabled is None:
        enabled = REDACT_SECRETS
    if not enabled or not text:
        return text, 0
    total = 0
    for pattern, replacement in _SECRET_PATTERNS:
        text, count = pattern.subn(replacement, text)
        total += count
    return text, total


def _call_once(provider: str, model: str, prompt: str, system: str,
               max_tokens: int, temperature: float | None,
               timeout: float | None = None) -> dict:
    prompt, prompt_redactions = scrub_secrets(prompt)
    system, system_redactions = scrub_secrets(system)
    redactions = prompt_redactions + system_redactions
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
    if not isinstance(data, dict):
        raise ValueError(f"上流の応答が JSON オブジェクトではありません（{type(data).__name__}）")
    text = _extract_text(data)
    finish = ""
    try:
        finish = (data.get("choices") or [{}])[0].get("finish_reason") or ""
    except Exception:
        finish = ""
    if not isinstance(finish, str):
        finish = ""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    return {"text": text, "latency_s": round(latency, 3), "finish_reason": finish,
            "truncated": finish == "length", "redactions": redactions,
            "max_tokens": max_tokens,
            "tokens": {"prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens")}}


def _candidates(ref: str, free_only: bool = True) -> list[str]:
    """指定先が冷却中でも、冷却外かつ認証可能な Free 代替モデルを並べる。

    予備の並びは**選抜と同じ規則**（`diverse_order(_order_for_selection(...))`）にする。
    以前はカタログ順だったため、実績のあるモデルより「まだ観測の無いカタログ前半のモデル」を
    先に試し、外れるたびに 1 回ぶんの失敗待ちを払っていた（実測: スコア 0.95 の実績モデルが
    0.2 の未検証モデルの後ろに並んだ）。
    """
    cooling = cooling_refs()
    out = [ref] if ref and ref not in cooling else []
    if len(out) >= _MAX_ATTEMPTS:
        return out[:_MAX_ATTEMPTS]
    blocked = {name for name in PROVIDER_ORDER if provider_auth_blocked(name)}
    ex_set = set(env_excluded_models())
    pool = [other for other in free_model_refs(free_only=free_only, chat_only=True)
            if other != ref and other not in cooling and other not in out
            and not _is_model_excluded(other, ex_set)
            and other.split("/", 1)[0] not in blocked]
    for other in diverse_order(_order_for_selection(pool)):
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


def _call_model_uncached(ref: str, prompt: str, *, system: str = "", max_tokens: int = 800,
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
    if free_only and _is_known_paid_model(provider, model):
        return {"error": f"{provider}/{model} は有料モデルのため呼び出しを拒否しました（Free モデルのみ許可）",
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
        cool_reason = as_str((cooling.get(ref) or {}).get("reason"))
        return {"error": f"全候補がクールダウン中です（{ref}）", "ref": ref, "kind": kind,
                "cooling_reason": cool_reason or None,
                "skipped_cooling": [ref], "rate_limited": bool(ref in cooling)}

    last_error = ""
    last_error_ref = ref
    last_observed_error = ""
    quota_warnings: list[str] = []
    skipped: list[str] = []
    avoided: list[str] = []
    # 提供元の枠を待つ予算は **1 回の `call_model` で共有する**。候補ごとに満額待つと、枠が
    # 全部埋まっているときに「候補の数 × GATE_WAIT_S」待ってしまう（規約 27 を推論側にも適用）。
    wait_left = GATE_WAIT_S
    gate_wait = 0.0

    def note_failure(cand: str, message: str, result: dict | None = None) -> None:
        """**フォールバックで落ちた候補**を統計へ残す（成功した候補だけ記録してはいけない）。

        成功時だけ `observe_call` を呼ぶと、毎回 5xx / 404 で落ちるモデルは観測 0 件のまま
        ＝`_rank_score` の探索ボーナス（`0.25·sd`）が最大で残り、**毎回いちばん最初に選ばれて
        1 回ぶんの失敗待ちを発生させ続ける**（実測: `nous/meituan/longcat-2.0:free` が
        `unobserved` のまま第 1 候補に居座り、毎回 `laguna-s-2.1` へフォールバックした）。
        環境障害（`is_env_failure`）はモデルの成績ではないので `observe_call` が no-op にする。
        """
        nonlocal last_observed_error
        if not message:
            return
        row = {"ref": cand, "error": message}
        if isinstance(result, dict):
            # 空応答でも finish_reason と消費 tokens は記録する（length 枯渇とそれ以外を区別する）。
            row.update({"truncated": bool(result.get("truncated")),
                        "tokens": result.get("tokens") or {},
                        "finish_reason": as_str(result.get("finish_reason")),
                        "max_tokens": as_int(result.get("max_tokens"), 0, 0, 10 ** 9) or None,
                        "retries": as_int(result.get("retries"), 0, 0, 100)})
        observe_call(row, kind, "")
        last_observed_error = message

    for idx, cand in enumerate(attempts):
        if claims is not None and ((cand == ref and claims.is_excluded(cand))
                                   or (cand != ref and not claims.take(cand))):
            avoided.append(cand)   # 他の枠が使用中／除外済み。独立性を守るため取らない
            continue
        c_provider, c_model = resolve_ref(cand, free_only=free_only)
        if not c_model:
            continue
        last_error_ref = cand
        try:
            result, waited = _gated_call_once(c_provider, c_model, prompt, system, max_tokens,
                                              temperature, timeout=timeout, wait_budget=wait_left)
            wait_left = max(0.0, wait_left - waited)
            gate_wait += waited
        except ProviderBusyError as exc:
            # **429 を踏む代わりに待って諦めた**ケース。隠さず脱落として返し、以後は待たない。
            last_error = str(exc)
            wait_left = 0.0
            skipped.append(cand)
            _debug("provider_busy", {"ref": cand})
            continue
        except QuotaExhaustedError as exc:
            # 宣言された上限に達した（§3.5）。**待たない**が、別の提供元の候補は試す。
            last_error = str(exc)
            skipped.append(cand)
            _debug("quota_exhausted", {"ref": cand})
            continue
        except HttpStatusError as exc:
            last_error = f"HTTP {exc.status}: {exc.body[:200]}"
            if exc.status == 429:
                until = note_rate_limited(cand, exc.retry_after)
                was, now = PROVIDER_GATE.note_rate_limited(c_provider)
                if now < was:
                    _debug("gate_reduced", {"provider": c_provider, "from": was, "to": now})
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
            # 落ちた候補を成績に残す（429 は提供元の一時制限なので除く）。
            if exc.status in _MODEL_FAULT_STATUS:
                note_failure(cand, last_error)
            if exc.status not in _FALLBACK_STATUS:
                break
            continue
        except Exception as exc:  # 接続不可・タイムアウト・JSON 壊れ
            last_error = f"{type(exc).__name__}: {exc}"
            if is_env_failure(last_error):
                break  # 環境障害では別モデルも同じ経路で失敗するため再試行を打ち切る
            note_failure(cand, last_error)   # 環境障害ではない（JSON 壊れ等）＝モデル側の失敗
            continue

        if result.get("quota_accounting"):
            quota_warnings.append(result["quota_accounting"])
        text = result["text"]
        if not text.strip():
            # 空応答。**成功として返さない**（メインLLMが「無回答」を回答と誤解する）。思考トークンで
            # 予算を使い切った可能性が高いので、予算を上げて 1 回だけ同じ候補で引き上げる。
            retry_note = ""
            bumped = max_tokens
            # **引き上げ再試行は「length 枯渇（または不明）」のときだけ**（v0.41.0）。
            # finish_reason が stop / content_filter 等の確定値で本文が空のとき、予算を増やしても
            # 空のままだった（本番 traces 実測: 失敗の大半がこの型で、再試行 1 回ぶんの待ちと
            # 枠を無駄にしていた）。枯渇が疑えるとき（truncated / length / 不明）だけ上げる。
            _finish = as_str(result.get("finish_reason")) if isinstance(result, dict) else ""
            _length_starved = bool(result.get("truncated")) or _finish in ("", "length")
            if max_tokens < _EMPTY_RETRY_CAP and _length_starved:
                bumped = min(max(max_tokens * 3, _EMPTY_TOKEN_FLOOR), _EMPTY_RETRY_CAP)
                try:
                    retry, retry_wait = _gated_call_once(c_provider, c_model, prompt, system,
                                                         bumped, temperature, timeout=timeout,
                                                         wait_budget=wait_left)
                    wait_left = max(0.0, wait_left - retry_wait)
                    gate_wait += retry_wait
                    if retry.get("quota_accounting"):
                        quota_warnings.append(retry["quota_accounting"])
                    if (retry.get("text") or "").strip():
                        result, text = retry, retry["text"]
                except Exception as exc:
                    # **リトライの失敗を「空応答」に潰さない**（v0.40.3）。429・環境障害は
                    # モデルの成績ではないので observe_call に流さない（規約 21）。
                    if isinstance(exc, HttpStatusError):
                        retry_note = f"HTTP {exc.status}: {(exc.body or '')[:120]}"
                        if exc.status == 429:
                            note_rate_limited(cand, exc.retry_after)
                    else:
                        retry_note = f"{type(exc).__name__}: {exc}"
            if not text.strip():
                if retry_note:
                    if is_env_failure(retry_note):
                        # 環境障害: 別候補も同じ経路で失敗するので打ち切る（モデルの成績に数えない）。
                        last_error = f"再試行（max_tokens={bumped}）でも環境障害: {retry_note}"
                        break
                    # **「空応答」で始めない**: classify_error は語を先に見るので、429 を含む文を
                    # 空応答（no_output）に誤分類させる（v0.40.3）。
                    last_error = f"再試行（max_tokens={bumped}）でも失敗: {retry_note}"
                    if "429" not in retry_note:
                        note_failure(cand, last_error)
                    skipped.append(cand)
                    continue
                finish = as_str(result.get("finish_reason")) if isinstance(result, dict) else ""
                why = "（finish_reason=length: 思考トークンで予算を使い切った）" if finish == "length" else ""
                tried = f"・再試行 {bumped} も空" if max_tokens < _EMPTY_RETRY_CAP else ""
                last_error = (f"空応答（{cand}）。max_tokens={max_tokens}{tried} を思考トークンで"
                              f"使い切った可能性があります{why}。max_tokens を増やすか、"
                              "FREEAGENT_MODEL_EXCLUDE で外してください")
                # 失敗行にも **どの max_tokens で・何回目で** 空だったかを残す（v0.42.1）。
                _fail_meta = dict(result) if isinstance(result, dict) else {}
                _fail_meta.update({"max_tokens": bumped,
                                   "retries": 1 if max_tokens < _EMPTY_RETRY_CAP else 0})
                note_failure(cand, last_error, _fail_meta)
                skipped.append(cand)
                continue

        # `ref` は**呼び出し側が要求したモデル**、`served_by` が実際に答えたモデル。
        # `ref` を cand で上書きすると `tool_ask` の「要求 → 代替へ回った」の表示が消える（実測）。
        out = {"ref": ref, "served_by": cand, "model": c_model, "provider": c_provider,
               "text": text, "latency_s": result["latency_s"],
               "truncated": result["truncated"], "tokens": result["tokens"],
               "max_tokens": result.get("max_tokens"),
               "redactions": result.get("redactions", 0),
               "cot_leak": _cot_leak(text), "kind": kind,
               "gate_wait_s": round(gate_wait, 3) if gate_wait else None,
               "fallback": cand != ref, "skipped_cooling": skipped or None}
        if quota_warnings:
            out["quota_accounting"] = "; ".join(quota_warnings)
        PROVIDER_GATE.note_success(c_provider)   # 枠が縮んでいたら 1 本ずつ戻す
        clear_provider_auth(c_provider)   # 通ったら「認証で駄目」の記憶を消す
        # 統計・トレースは**答えたモデル**に付ける（要求元に付けると、代替で答えた事実が消える）。
        observe_call(dict(out, ref=cand), kind, text)
        return out

    fail = {"error": last_error or "すべての候補で失敗しました", "ref": ref, "kind": kind,
            "skipped_cooling": skipped or None,
            "rate_limited": any(r in cooling_refs() for r in attempts)}
    if quota_warnings:
        fail["quota_accounting"] = "; ".join(quota_warnings)
    if avoided and not last_error:
        fail["error"] = ("フォールバック先がすべて同じ呼び出しの他の枠で使用中か除外済みでした"
                         "（独立性を守るため同じモデルで枠を埋めていません）")
    if avoided:
        fail["avoided_duplicates"] = len(avoided)
    if fail["error"] != last_observed_error:
        observe_call(dict(fail, ref=last_error_ref), kind, "")
    return fail


class _ModelClaims:
    """1 回の並列呼び出し（`ask_many`）の中で**使用中のモデル**を記録する（フォールバックの重複防止）。

    各枠の本来のモデルと除外モデルを最初から「使用中」にしておき、フォールバックで新たに取る
    モデルはロック内で 1 回だけ確保できる（2 枠が同時に同じ予備モデルへ落ちるのを防ぐ）。
    """

    def __init__(self, refs, avoid=(), identity_avoid=()):
        self._lock = threading.Lock()
        self._excluded = {r for r in (avoid or ()) if isinstance(r, str) and r}
        self._identity_excluded = {_model_identity(r) for r in (identity_avoid or ())
                                   if isinstance(r, str) and r}
        self._taken = {_model_identity(r) for r in list(refs) + list(self._excluded) + list(identity_avoid or ())
                       if isinstance(r, str) and r}

    def is_excluded(self, ref: str) -> bool:
        return (_model_identity(ref) in self._identity_excluded
                or _is_model_excluded(ref, self._excluded))

    def take(self, ref: str) -> bool:
        with self._lock:
            identity = _model_identity(ref)
            if identity in self._taken or self.is_excluded(ref):
                return False
            self._taken.add(identity)
            return True


# ---------------------------------------------------------------- 進捗通知（notifications/progress）
# MCP の `notifications/progress` は **クライアントが `_meta.progressToken` を付けた要求にだけ**送る
# （仕様: トークンの無い要求に送ってはならない）。Hermes はツール呼び出しごとに `progress_callback` を
# 渡し、SDK が `_meta.progressToken` を載せてくる（実測: `agent/tool_progress_relay.py` の
# `bound_tool_progress` と `tools/mcp_tool_handlers.py` の `_call_tool_racing_stdio_death`）。
# **送り先は transport で違う**（stdio は stdout に 1 行、HTTP はセッションの SSE 待ち行列）ので、
# transport が**作り手**をスレッドローカルに置き、`handle_tool_call` が要求から読んだトークンで
# **送り手**を組み立てる。`run_parallel` が回収のたびに 1 件送る（回収ループは呼び出し側スレッドで
# 回るので、送り手はここから見える）。
PROGRESS_ENABLED = as_flag(os.environ.get("FREEAGENT_PROGRESS", "1"))
_PROGRESS_LOCAL = threading.local()


def _progress_token_of(params) -> "str | int | None":
    """`tools/call` の params から `_meta.progressToken` を読む（str / int だけ。bool は除く）。"""
    if not isinstance(params, dict):
        return None
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        return None
    token = meta.get("progressToken")
    if isinstance(token, bool) or not isinstance(token, (str, int)):
        return None
    return token


class _ProgressSink:
    """1 回のツール呼び出しぶんの進捗。`emit` は `run_parallel` の回収ループから呼ばれる。"""

    def __init__(self, token, sender):
        self.token = token
        self._send = sender
        self._lock = threading.Lock()
        self.sent = 0

    def emit(self, progress, total=None, message=None) -> None:
        note = {"jsonrpc": "2.0", "method": "notifications/progress",
                "params": {"progressToken": self.token, "progress": float(progress)}}
        if total is not None:
            note["params"]["total"] = float(total)
        if message:
            note["params"]["message"] = str(message)[:120]
        with self._lock:               # 1 行 / 1 フレームを割らない
            self._send(note)
            self.sent += 1


@contextlib.contextmanager
def bound_progress(maker):
    """このスレッドの「送り手の作り」を差し替える（transport が要求 1 件ぶんだけ張る）。"""
    prev = getattr(_PROGRESS_LOCAL, "maker", None)
    _PROGRESS_LOCAL.maker = maker
    try:
        yield
    finally:
        _PROGRESS_LOCAL.maker = prev


@contextlib.contextmanager
def progress_for(params):
    """`tools/call` の params に応じて送り手を張る（トークンが無ければ**何もしない**）。"""
    prev = getattr(_PROGRESS_LOCAL, "sink", None)
    maker = getattr(_PROGRESS_LOCAL, "maker", None)
    token = _progress_token_of(params) if PROGRESS_ENABLED else None
    sink = None
    if maker is not None and token is not None:
        try:
            sink = _ProgressSink(token, maker(token))
        except Exception as exc:            # 送り先の用意に失敗しても本体は動かす
            _debug("progress_error", f"{type(exc).__name__}: {exc}")
    try:
        _PROGRESS_LOCAL.sink = sink         # 前の要求のぶんを引き継がない
        yield sink
    finally:
        _PROGRESS_LOCAL.sink = prev


def progress_emit(progress, total=None, message=None) -> None:
    """送り手があれば 1 件送る（無ければ何もしない＝仕様どおり）。進捗で本体を壊さない。"""
    sink = getattr(_PROGRESS_LOCAL, "sink", None)
    if sink is None:
        return
    try:
        sink.emit(progress, total, message)
    except Exception as exc:
        _debug("progress_error", f"{type(exc).__name__}: {exc}")


def progress_label(job) -> str:
    """進捗の 1 行に添える短いラベル（モデル名・ソース名など）。長い本文は切る。"""
    if isinstance(job, str):
        text = job
    elif isinstance(job, (tuple, list)) and job and isinstance(job[0], str):
        text = job[0]
    else:
        return ""
    return text.strip().replace("\n", " ")[:40]


# ---------------------------------------------------------------- 同一実行内の重複合流（重複排除）
# 知識側の `_KB_INFLIGHT`（同じ問いの同時実行を 1 回の上流取得に畳む）と**同じ型**を推論側へ写したもの。
# 畳むのは **1 回のツール呼び出しの中の、完全に同じ呼び出し**だけ。プロセスをまたぐキャッシュには
# しない——`scripts/measure_quality.py --repeat` が同一応答を引くと、**ばらつきの測定が壊れる**ため
# （測る道具は `S.call_model` を直接呼ぶので、そもそもここを通らない）。
# **モデルが違えば畳まない**: `ask_many` の「同じ問いを N 体へ」は多様性が目的なので、キーに `ref` を
# 含めて壊さない（実測の考え方は `tests/test_coalesce.py`）。
COALESCE_ENABLED = as_flag(os.environ.get("FREEAGENT_COALESCE", "1"))
COALESCE_WAIT_S = max(0.0, _env_float("FREEAGENT_COALESCE_WAIT", CALL_BUDGET_S))
_COALESCE_LOCAL = threading.local()


def _call_coalescer():
    """このスレッドの合流器（ツール呼び出しの外では None＝畳まない）。"""
    return getattr(_COALESCE_LOCAL, "coalescer", None)


@contextlib.contextmanager
def bound_coalescer():
    """ツール呼び出し 1 回ぶんの合流器を張る（入れ子は元に戻す）。"""
    prev = getattr(_COALESCE_LOCAL, "coalescer", None)
    _COALESCE_LOCAL.coalescer = _CallCoalescer(COALESCE_WAIT_S) if COALESCE_ENABLED else None
    try:
        yield
    finally:
        _COALESCE_LOCAL.coalescer = prev


def _run_worker(worker, run: str, tool: str):
    """ワーカーのスレッドに呼び出し ID を張ってから本体を呼ぶ（`run_parallel` の内側でのみ使う）。"""
    def wrapped(job):
        with run_scope(run, tool):
            return worker(job)
    return wrapped


def _coalescer_worker(worker, coalescer):
    """ワーカーを包んで、合流器を**そのスレッド**にも見せる。

    合流器は `bound_coalescer()` を張ったスレッド（= ツール呼び出しを実行しているスレッド）の
    スレッドローカルにある。`run_parallel` はワーカーを**別スレッド**で走らせるので、包まないと
    ワーカーから `_call_coalescer()` が見えず、**並列経路では重複が 1 件も畳まれない**
    （進捗通知は「回収ループが呼び出し側スレッドで回る」ので包み不要——ここが非対称な点）。
    """

    def run(job):
        prev = getattr(_COALESCE_LOCAL, "coalescer", None)
        _COALESCE_LOCAL.coalescer = coalescer
        try:
            return worker(job)
        finally:
            _COALESCE_LOCAL.coalescer = prev

    return run


class _CallCoalescer:
    """同じキーの呼び出しを 1 本だけ上流へ流し、結果を全員に配る。

    待つ側には**上限**を置く（規約 53 と同じ型）。超えたら自分で取りに行く＝上流 1 本を無駄にする
    代わりに固まらない。`run_parallel` は先頭のジョブから順にワーカーへ渡すので、待つ側が走り出して
    いる時点で同じキーの先頭は**既に走っている**（だから待ち合いが循環しない）。
    """

    def __init__(self, wait_max: float = 0.0):
        self._lock = threading.Lock()
        self._slots: dict = {}
        self._wait_max = max(0.0, float(wait_max))
        self.saved = 0          # 上流を呼ばずに済んだ件数

    def run(self, key, thunk):
        with self._lock:
            slot = self._slots.get(key)
            first = slot is None
            if first:
                slot = {"event": threading.Event(), "value": None, "error": None}
                self._slots[key] = slot
        if first:
            try:
                value = thunk()
            except BaseException as exc:        # 先頭が落ちたら、待っている側にはエラーを配る
                slot["error"] = {"error": f"{type(exc).__name__}: {exc}"}
                raise
            else:
                slot["value"] = value           # `finally` より先に置く（待つ側が読む前に）
                return value
            finally:
                slot["event"].set()
        if slot["event"].wait(self._wait_max or None):
            if slot["error"] is not None:
                return dict(slot["error"], coalesced=True)
            value = copy.deepcopy(slot["value"])
            if isinstance(value, dict):
                value["coalesced"] = True       # 上流を呼んでいないことを**隠さない**（規約 8）
            with self._lock:
                self.saved += 1
            return value
        return thunk()                          # 待ち切れなかったので自分で取りに行く


class Deadline:
    """並列実行の壁時計予算。`run_parallel` が**残り時間で回収を打ち切る**ために使う。

    Python のスレッドは強制終了できない（anyio の文書も「shutdown にはコード側の完全な協力が要る」と
    明記している）。したがって打ち切りは「**回収をやめる**」であり、投入済みの呼び出しは裏で完了する
    （結果は捨てるが、キャッシュ・統計・クォータには正しく残る）。打ち切った分は `dropped: true` として
    呼び出し側に返す（規約 8・63「黙って捨てない」）。
    """

    def __init__(self, seconds: float):
        self.total = max(0.0, float(seconds))
        # All cutoff and worker-completion readings use the same high-resolution
        # clock; monotonic() is coarser on Windows Python 3.11/3.12.
        self._until = time.perf_counter() + self.total
        self._cancel_signal = Future()
        self._cancel_lock = threading.Lock()

    def remaining(self) -> float:
        return max(0.0, self._until - time.perf_counter())

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def cancel(self) -> None:
        """打ち切る（回収は即座に終わり、未開始のジョブは投入されない）。"""
        with self._cancel_lock:
            self._until = min(self._until, time.perf_counter())
        # Future callbacks execute synchronously and may block or re-enter.
        # Every canceller can signal even if another is paused before set_result.
        try:
            self._cancel_signal.set_result(None)
        except InvalidStateError:
            pass  # another canceller has already signalled

    def __repr__(self) -> str:
        return f"Deadline(total={self.total:.1f}s, remaining={self.remaining():.1f}s)"


_BUDGET_LOCAL = threading.local()


@contextlib.contextmanager
def budget_scope(seconds: float | None = None):
    """**ツール呼び出し 1 回ぶん**の予算をこのスレッドに張る（入れ子は元に戻す）。

    以前は `call_budget()` が呼ばれるたびに新しい `Deadline` を作っていたため、
    (a) `tool_map` / `tool_agent` は予算を渡しておらず打ち切りが効かない、
    (b) `consult(debate_depth=deep)` はラウンドごとに満額（240 秒 × 2）を作る、
    (c) `panel(refine)` / `map(reduce)` の後段は残り時間を見ない、
    という 3 つの穴があった（実測: `CALL_BUDGET_S=0.1` で `tool_fanout` は 0.11 秒で打ち切るのに
    `tool_map` は 0.50 秒完走、`consult(deep)` は 0.15 秒予算で 0.24 秒走った）。
    1 回の呼び出しに 1 つの `Deadline` を共有すれば、MCP クライアントの 300 秒より手前で必ず返る。
    """
    previous = getattr(_BUDGET_LOCAL, "deadline", None)
    limit = CALL_BUDGET_S if seconds is None else seconds
    _BUDGET_LOCAL.deadline = (Deadline(limit) if limit > 0 else
                              Deadline(float("inf")) if getattr(_REQ_LOCAL, "id", None) is not None else None)
    try:
        yield _BUDGET_LOCAL.deadline
    finally:
        _BUDGET_LOCAL.deadline = previous


def call_budget() -> "Deadline | None":
    """このツール呼び出しの予算（`FREEAGENT_CALL_BUDGET` が 0 なら None＝無制限）。

    呼び出し側スレッドに `budget_scope()` が張られていれば**その残り**を共有する（後段の
    refine / reduce / 討論の 2 ラウンド目が満額を新しく作らない）。
    """
    shared = getattr(_BUDGET_LOCAL, "deadline", None)
    if shared is not None:
        return shared
    return Deadline(CALL_BUDGET_S) if CALL_BUDGET_S > 0 else None


def budget_expired() -> bool:
    """このツール呼び出しの予算を使い切ったか（後段を始めてよいかの判定）。"""
    shared = getattr(_BUDGET_LOCAL, "deadline", None)
    return bool(shared is not None and shared.expired)


# ---------------------------------------------------------------- §4.0 要求単位のキャンセル
# MCP の `notifications/cancelled`（`requestId`）を受けたら、**その要求の予算を打ち切る**。
# スレッドは殺せないので「回収をやめる」だけだが、未開始のジョブは投入されず（`run_parallel` の
# 冒頭判定）、上流の枠とクォータを無駄に使わない。クライアントが 300 秒で切った後も裏で走り続ける
# 呼び出しを減らすのが目的（v0.24.0）。
_REQ_LOCAL = threading.local()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_BUDGETS: dict = {}


def _active_key(msg_id):
    return (getattr(_REQ_LOCAL, "session", None), f"{type(msg_id).__name__}:{msg_id}")


def _register_active(msg_id, deadline) -> None:
    if deadline is None or msg_id is None:
        return
    with _ACTIVE_LOCK:
        _ACTIVE_BUDGETS.setdefault(_active_key(msg_id), set()).add(deadline)


def _unregister_active(msg_id, deadline=None) -> None:
    if msg_id is None:
        return
    with _ACTIVE_LOCK:
        key = _active_key(msg_id)
        active = _ACTIVE_BUDGETS.get(key)
        if active is not None:
            if deadline is None:
                _ACTIVE_BUDGETS.pop(key, None)
            else:
                active.discard(deadline)
                if not active:
                    _ACTIVE_BUDGETS.pop(key, None)


def cancel_request(msg_id) -> bool:
    """Cancel all active occurrences of this ID within the current transport session."""
    if msg_id is None:
        return False
    with _ACTIVE_LOCK:
        deadlines = _ACTIVE_BUDGETS.pop(_active_key(msg_id), ())
    for deadline in deadlines:
        deadline.cancel()
    return bool(deadlines)


@contextlib.contextmanager
def request_scope(msg_id, session=None):
    """Bind a request ID and transport session for cancellation."""
    previous = getattr(_REQ_LOCAL, "id", None)
    previous_session = getattr(_REQ_LOCAL, "session", None)
    _REQ_LOCAL.id = msg_id
    _REQ_LOCAL.session = session
    try:
        yield
    finally:
        _REQ_LOCAL.id = previous
        _REQ_LOCAL.session = previous_session


# ---------------------------------------------------------------- §4.1 任意の公平な共有スケジューラ
FAIR_ENABLED = as_flag(os.environ.get("FREEAGENT_FAIR_SCHEDULER", "0"))
FAIR_WORKERS = max(1, min(32, _env_int("FREEAGENT_FAIR_WORKERS", MAX_WORKERS)))
FAIR_MAX_PENDING = max(1, min(10000, _env_int("FREEAGENT_FAIR_MAX_PENDING", 1024)))
_FAIR_SCHEDULER = None
_FAIR_SCHEDULER_LOCK = threading.Lock()


def _get_fair_scheduler():
    global _FAIR_SCHEDULER
    with _FAIR_SCHEDULER_LOCK:
        if _FAIR_SCHEDULER is None:
            from .fair_scheduler import FairScheduler
            _FAIR_SCHEDULER = FairScheduler(FAIR_WORKERS, FAIR_MAX_PENDING)
        return _FAIR_SCHEDULER


def _fair_eligible(jobs, keys, providers):
    if not FAIR_ENABLED or keys is None or len(keys) != len(jobs):
        return False
    # Unknown/unqualified refs require catalogue lookup; retain legacy execution
    # rather than constructing unbounded arbitrary provider lanes.
    if providers is not None and (len(providers) != len(jobs) or not all(providers)):
        return False
    try:
        return len(set(keys)) == len(keys)
    except TypeError:
        return False


def _run_fair(jobs, worker, workers, deadline, providers):
    from .fair_scheduler import QueueFullError

    scheduler = _get_fair_scheduler()
    try:
        batch = scheduler.submit_batch(worker, jobs, workers, providers)
    except QueueFullError:
        return [{"error": "共有実行キューの上限に達したため実行しませんでした",
                 "dropped": True, "overloaded": True} for _ in jobs]
    out: list = [None] * len(jobs)
    futures = {future: i for i, future in enumerate(batch.futures)}
    collected = set()
    done = 0
    timed_out = False
    completed = False
    try:
        signals = list(futures) + ([deadline._cancel_signal] if deadline is not None else [])
        for future in as_completed(signals, timeout=None if deadline is None or math.isinf(deadline.remaining()) else deadline.remaining()):
            if deadline is not None and (future is deadline._cancel_signal or deadline.expired):
                timed_out = True
                break
            i = futures[future]
            if not _collect_parallel_future(future, i, out, collected, deadline):
                timed_out = True
                break
            done += 1
            progress_emit(done, len(jobs), progress_label(jobs[i]))
            if done == len(futures):
                break
        completed = not timed_out
    except FuturesTimeout:
        timed_out = True
    finally:
        if not completed:
            scheduler.cancel_batch(batch)
    if timed_out:
        _collect_completed_parallel(futures, out, collected, deadline)
        done = len(collected)
        for i, value in enumerate(out):
            if i not in collected:
                out[i] = _dropped_result(deadline, "回収を打ち切り")
        progress_emit(done, len(jobs), "予算切れで打ち切りました")
    return out


_PARALLEL_INFLIGHT = set()
_PARALLEL_INFLIGHT_LOCK = threading.Condition()


def _track_parallel_future(future):
    with _PARALLEL_INFLIGHT_LOCK:
        _PARALLEL_INFLIGHT.add(future)
    def forget(done):
        with _PARALLEL_INFLIGHT_LOCK:
            _PARALLEL_INFLIGHT.discard(done)
            _PARALLEL_INFLIGHT_LOCK.notify_all()
    future.add_done_callback(forget)


def _drain_parallel_futures():
    with _PARALLEL_INFLIGHT_LOCK:
        while _PARALLEL_INFLIGHT:
            _PARALLEL_INFLIGHT_LOCK.wait()


def _parallel_future_result(future, deadline):
    try:
        result = future.result()
        return result if deadline is not None else (None, result)
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}


def _collect_parallel_future(future, i, out, collected, deadline):
    # Only call this for a done future, and never invoke it under _cancel_lock.
    completed_at, result = _parallel_future_result(future, deadline)
    if deadline is None:
        out[i] = result
        collected.add(i)
        return True
    with deadline._cancel_lock:
        # This comparison and publication linearize against cancel(). Equal
        # timestamps cannot establish order and are conservatively rejected.
        # No timestamp is available for an executor-level exception: retain it
        # only while the cutoff is still in the future.
        eligible = (completed_at < deadline._until if completed_at is not None
                    else time.perf_counter() < deadline._until)
        if eligible:
            out[i] = result
            collected.add(i)
        return eligible


def _collect_completed_parallel(futures, out, collected, deadline):
    # A progress sender can block past the cutoff; use worker completion time,
    # not the later time when the collector happens to inspect the future.
    for future, i in futures.items():
        if i in collected or not future.done() or future.cancelled():
            continue
        _collect_parallel_future(future, i, out, collected, deadline)


def run_parallel(jobs: list, worker, max_workers: int | None = None,
                 deadline: "Deadline | None" = None, *, fair_keys=None, fair_providers=None) -> list:
    """jobs を並列に処理して**入力順**で返す。並列化が効くのは I/O 待ち（API 待ち）である点に注意。

    `deadline` を渡すと、**残り時間で回収を打ち切る**（未回収の位置は `dropped: true` の dict で埋める）。
    `cancel_futures=True` で**まだ走っていない**ジョブは取り消す（枠・クォータを無駄に使わない）。
    """
    if not jobs:
        return []
    # 予算が既に尽きているなら**投入しない**（枠・クォータを使わずに脱落として返す）。
    if deadline is not None and deadline.expired:
        return [_dropped_result(deadline, "未実行") for _ in jobs]
    out: list = [None] * len(jobs)
    workers = max(1, min(max_workers or MAX_WORKERS, len(jobs)))
    # 合流器は**呼び出し側スレッド**に張られている（ツール呼び出し 1 回ぶん）。ワーカーは別スレッド
    # なので、ここで包んで同じ合流器を渡す（渡さないと並列経路で重複が畳まれない）。
    _coalescer = _call_coalescer()
    if _coalescer is not None:
        worker = _coalescer_worker(worker, _coalescer)
    # 呼び出し単位の ID も**ワーカーへ明示的に渡す**（スレッドローカルは別スレッドに伝播しない）。
    # 渡さないと、並列経路の traces だけ run が空になり「呼び出しの内訳」が復元できない。
    _parent_run, _parent_tool = current_run(), current_tool()
    if _parent_run:
        worker = _run_worker(worker, _parent_run, _parent_tool)
    if deadline is not None:
        inner_worker = worker
        def budgeted_worker(job):
            previous = getattr(_BUDGET_LOCAL, "deadline", None)
            _BUDGET_LOCAL.deadline = deadline
            try:
                if deadline.expired:
                    result = _dropped_result(deadline, "未実行")
                else:
                    try:
                        result = inner_worker(job)
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                return time.perf_counter(), result
            finally:
                _BUDGET_LOCAL.deadline = previous
        worker = budgeted_worker
    if _fair_eligible(jobs, fair_keys, fair_providers):
        return _run_fair(jobs, worker, workers, deadline, fair_providers)
    if workers == 1 and deadline is None:
        for i, job in enumerate(jobs):
            if deadline is not None and deadline.expired:
                out[i] = _dropped_result(deadline, "未実行")
                continue
            try:
                out[i] = worker(job)
            except Exception as exc:  # worker 側の例外もここで吸収する
                out[i] = {"error": f"{type(exc).__name__}: {exc}"}
            progress_emit(i + 1, len(jobs), progress_label(job))
        return out
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    collected = set()
    timed_out = False
    done = 0
    completed = False
    try:
        for i, job in enumerate(jobs):
            future = pool.submit(worker, job)
            _track_parallel_future(future)
            futures[future] = i
        signals = list(futures) + ([deadline._cancel_signal] if deadline is not None else [])
        for fut in as_completed(signals, timeout=None if deadline is None or math.isinf(deadline.remaining()) else deadline.remaining()):
            if deadline is not None and (fut is deadline._cancel_signal or deadline.expired):
                timed_out = True
                break
            i = futures[fut]
            if not _collect_parallel_future(fut, i, out, collected, deadline):
                timed_out = True
                break
            done += 1
            progress_emit(done, len(jobs), progress_label(jobs[i]))
            if done == len(futures):
                break
        completed = not timed_out
    except FuturesTimeout:
        timed_out = True
    finally:
        pool.shutdown(wait=completed, cancel_futures=not completed)
    if timed_out:
        _collect_completed_parallel(futures, out, collected, deadline)
        done = len(collected)
        for i, value in enumerate(out):
            if i not in collected:
                out[i] = _dropped_result(deadline, "回収を打ち切り")
        progress_emit(done, len(jobs), "予算切れで打ち切りました")
    return out


def _dropped_result(deadline: "Deadline | None", why: str) -> dict:
    """予算切れの位置を埋める dict（`dropped` を機械可読に立てる）。"""
    # `:.0f` は 1 秒未満の予算を「0 秒」と表示する（実測: 0.25 秒 → 「予算（0 秒）」）。`:.3g` で刻む。
    total = ("無制限" if math.isinf(deadline.total) else f"{deadline.total:.3g} 秒") if deadline is not None else "予算"
    return {"error": f"並列実行の予算（{total}）で{why}になりました（この結果は脱落です）",
            "dropped": True, "timed_out": True}


def ask_many(refs: list[str], prompt: str, *, system: str = "", max_tokens: int = 800,
             temperature: float | None = None, kind: str = "panel",
             avoid: list[str] | None = None, identity_avoid: list[str] | None = None) -> list[dict]:
    """同じプロンプトを複数モデルへ**同時に**投げる（1 ターン待たずに走るのが並列の利点）。

    「独立した複数の意見」を返す経路なので、フォールバックが**他の枠と同じモデル**や `avoid`
    （利用者の明示除外）や `identity_avoid`（既に使ったモデル）に落ちないようにする（`_ModelClaims`）。
    """
    claims = _ModelClaims(refs, avoid, identity_avoid)
    return run_parallel(
        list(refs),
        lambda ref: call_model(ref, prompt, system=system, max_tokens=max_tokens,
                               temperature=temperature, kind=kind, claims=claims),
        max_workers=min(len(refs), MAX_WORKERS), deadline=call_budget(), fair_keys=refs,
        fair_providers=[split_ref(ref)[0] for ref in refs])


def ask_map(pairs: list[tuple[str, str]], *, system: str = "", max_tokens: int = 800,
            temperature: float | None = None, kind: str = "fanout") -> list[dict]:
    """(ref, prompt) の組を並列に処理する。"""
    return run_parallel(
        list(pairs),
        lambda pair: call_model(pair[0], pair[1], system=system, max_tokens=max_tokens,
                                temperature=temperature, kind=kind),
        max_workers=min(len(pairs), MAX_WORKERS), deadline=call_budget(),
        fair_keys=[(ref, prompt) for ref, prompt in pairs],
        fair_providers=[split_ref(ref)[0] for ref, _ in pairs])


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
# 査読（`freeagent_consult(mode="review", draft=...)`）専用の型。**軸と重要度の語彙を固定**しないと、
# モデルごとに「重大」「blocker」「高い」と揺れて比較できない（外部 OSS の 5 軸レビューと同じ理由）。
# 重要度を名乗らなかった/語彙外の回答は `unknown` として残し、**blocking 側に倒す**（黙って通さない）。
REVIEW_SYSTEM = (
    "あなたは下書きの査読者です。出力は必ず次の4行だけです。\n"
    "結論: <総評を1〜3文>\n"
    "重要度: <critical / required / optional / nit のうち最も重いものを1つ>\n"
    "指摘: <根拠つきの指摘を最大3個。各「[軸] 箇所 — 理由」。軸は correctness / readability / "
    "architecture / security / performance>\n"
    "メインに確認したい点: <前提が足りず判断できない点を1つだけ。推奨の既定値を「既定: …」で添える。"
    "無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)
# 仕様適合の査読（`basis` を渡したときの第 1 段）。**品質は見ない**のが役割で、
# 「計画どおりか」と「良いコードか」を混ぜないために段を分ける（superpowers の
# subagent-driven-development が仕様適合 → 品質の順に見るのと同じ理由）。
COMPLIANCE_SYSTEM = (
    "あなたは仕様適合の査読者です。**品質の良し悪しは判定せず**、下書きが【仕様・計画】の要件を"
    "満たしているかだけを見てください。出力は必ず次の4行だけです。\n"
    "結論: <満たしている点と足りない点を1〜3文>\n"
    "判定: <compliant / partial / noncompliant のうち1つ>\n"
    "指摘: <満たしていない要件を最大3個。各「[要件] 該当箇所 — 何が足りないか」>\n"
    "メインに確認したい点: <前提が足りず判断できない点を1つだけ。推奨の既定値を「既定: …」で"
    "添える。無ければ なし>\n"
    "前置き・思考過程・挨拶・Markdown の見出しは書かない。"
)
REVIEW_AXES = ("correctness", "readability", "architecture", "security", "performance")
REVIEW_SEVERITIES = ("critical", "required", "optional", "nit")
_SEVERITY_ALIAS = {
    "critical": "critical", "クリティカル": "critical", "重大": "critical", "blocker": "critical",
    "required": "required", "必須": "required", "high": "required",
    "optional": "optional", "任意": "optional", "推奨": "optional", "medium": "optional",
    "nit": "nit", "軽微": "nit", "low": "nit", "nitpick": "nit",
}
# メインへ返す質問の上限（dsh-superagent の「注意予算」と spec-kit の質問上限の写し）。
# 上限を超えた分は**捨てずに** `deferred_questions` に移す（規約 8）。
QUESTION_CAP = 5
AGENT_SYSTEM_FINAL = (
    "これ以上のツール呼び出しはできません。集めた情報だけを根拠に、最終回答を JSON 1つで出してください。\n"
    '  {"answer": "<回答。使った根拠の番号 [n] を本文に書き、根拠が足りない点は「根拠に無い」と明記>"}\n'
    "根拠を 1 つも使っていない場合は [0] を書いてください。JSON 以外の文字を書かない。"
)
AGENT_SYSTEM = (
    "あなたは調査補佐です。次のいずれか**1つだけ**を JSON で出力してください。\n"
    '  ツールを使う: {"tool": "lookup", "query": "<検索語>", "sources": ["arxiv","crossref",...]}\n'
    '  使える source: wikipedia, wikidata, arxiv, crossref, openalex, github, datacite, openaire, zenodo, ror, doaj, npm, crates, cinii\n'
    '  追加（明示指定のみ）: osv, ietf, inspirehep, oeis, hfhub, hn, swh, librariesio, ia, qiita, openreview, mdn, devdocs, nvd, terraform, zbmath\n'
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


# 査読の解析。`重要度` は語彙に寄せ、寄らない綴りは **unknown** として残す（勝手に丸めない）。
_LABEL_SEVERITY = re.compile(r"(?:重要度|severity|重大度)\s*[:：]\s*([A-Za-zぁ-んァ-ヶ一-龠]{2,12})", re.I)
_LABEL_FINDINGS = re.compile(r"(?:指摘|findings?)\s*[:：]\s*(.+)", re.I)
_LABEL_DEFAULT = re.compile(r"既定\s*[:：]\s*([^。\n]{1,60})")


def parse_review(text: str) -> dict:
    """査読回答から重要度・指摘・軸を取る。取れない項目は空（推測で埋めない）。"""
    out = {"severity": "", "findings": [], "axes": []}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_SEVERITY.search(line)
        if m and not out["severity"]:
            out["severity"] = _SEVERITY_ALIAS.get(m.group(1).strip().lower(), "unknown")
        m = _LABEL_FINDINGS.search(line)
        if m and len(out["findings"]) < 6:
            out["findings"].append(truncate(m.group(1).strip().strip("* "), 240))
    low = (text or "").lower()
    out["axes"] = [axis for axis in REVIEW_AXES if axis in low]
    return out


def summarize_review(rows: list[dict]) -> dict:
    """査読の重要度を数える。**語彙外も、名乗らない回答も unknown** として blocking に数える。

    査読モードの system は重要度の表明を要求しているので、無言は「軽い」ではなく「不明」として扱う。
    """
    counts = {sev: 0 for sev in REVIEW_SEVERITIES}
    counts["unknown"] = 0
    reviewed = 0
    for row in rows:
        if row.get("error"):
            continue
        reviewed += 1
        sev = as_str(row.get("severity")).strip().lower()
        if sev in counts:
            counts[sev] += 1
        else:
            counts["unknown"] += 1
    counts["blocking"] = counts["critical"] + counts["required"] + counts["unknown"] > 0
    counts["reviewed"] = reviewed
    counts["unreviewed"] = len(rows) - reviewed
    return counts


_LABEL_COMPLIANCE = re.compile(
    r"(?:判定|適合|compliance)\s*[:：]\s*([A-Za-zぁ-んァ-ヶ一-龠]{2,16})", re.I)
_COMPLIANCE_ALIAS = {
    "compliant": "compliant", "適合": "compliant", "ok": "compliant", "満たす": "compliant",
    "partial": "partial", "一部適合": "partial", "一部": "partial", "部分的": "partial",
    "noncompliant": "noncompliant", "不適合": "noncompliant", "ng": "noncompliant",
    "満たさない": "noncompliant",
}


def parse_compliance(text: str) -> dict:
    """仕様適合の判定を取る。語彙外・名乗りなしは `unknown` として残す（推測で丸めない）。"""
    out = {"verdict": "", "findings": []}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = _LABEL_COMPLIANCE.search(line)
        if m and not out["verdict"]:
            out["verdict"] = _COMPLIANCE_ALIAS.get(m.group(1).strip().lower(), "unknown")
        m = _LABEL_FINDINGS.search(line)
        if m and len(out["findings"]) < 6:
            out["findings"].append(truncate(m.group(1).strip().strip("* "), 240))
    return out


def summarize_compliance(rows: list[dict]) -> dict:
    """適合判定を数える。**noncompliant と unknown（名乗りなし）は blocking**。"""
    counts = {"compliant": 0, "partial": 0, "noncompliant": 0, "unknown": 0}
    reviewed = 0
    for row in rows:
        if row.get("error"):
            continue
        reviewed += 1
        verdict = as_str(row.get("verdict")).strip().lower()
        if verdict in counts:
            counts[verdict] += 1
        else:
            counts["unknown"] += 1
    counts["blocking"] = counts["noncompliant"] + counts["unknown"] + len(rows) - reviewed > 0
    counts["reviewed"] = reviewed
    counts["unreviewed"] = len(rows) - reviewed
    return counts


def parse_question_default(text: str) -> tuple[str, str]:
    """質問文から「既定: …」を切り離す（本文は変えない。既定値は別のキーで返す）。"""
    raw = (text or "").strip()
    m = _LABEL_DEFAULT.search(raw)
    return raw, (truncate(m.group(1).strip(), 120) if m else "")


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


def _expand_models(names: list[str]) -> tuple[list[str], bool]:
    """`models: ["all"]`（または `*` / `全部`）を**在庫の全 chat Free** へ展開する。

    戻り値は (refs, 上限で切り詰めたか)。名指しした ref を先に残し、そのうえで在庫を足す。
    上限は `FREEAGENT_MAX_MODELS_PER_CALL`（既定 16）。`freeagent_models` の `ref` を並べる代わりに
    「今 使える Free を全部」を 1 語で指定するための入口（ツールを増やさずに済ませる）。
    """
    wildcard = ("all", "*", "全部", "すべて")
    toks = [n.strip() for n in names if isinstance(n, str) and n.strip()]
    if any(t.lower() in wildcard for t in toks):
        out = [t for t in toks if t.lower() not in wildcard]
        for ref in free_model_refs(free_only=True, chat_only=True):
            if ref not in out:
                out.append(ref)
    else:
        out = toks
    return out[:MAX_MODELS_PER_CALL], len(out) > MAX_MODELS_PER_CALL


def _select_or_error(args: dict, default_size: int = 3, kind: str | None = None) -> tuple[list[str], dict]:
    """`models`（`all` 展開つき）→ `prefer` → 品質統計の順に参加モデルを決める。"""
    requested, cut = _expand_models(as_str_list(args.get("models")))
    size = as_int(args.get("size"), default_size, 1, MAX_SUB_MODELS)
    if requested:
        size = max(size, len(requested))     # 明示指定は size で切り落とさない
    refs, info = select_models(size, requested or None,
                               prefer=as_str_list(args.get("prefer")) or None,
                               exclude=as_str_list(args.get("exclude")) or None, kind=kind)
    if cut:
        info.setdefault("notes", []).append(
            f"models は上限 {MAX_MODELS_PER_CALL} 件で切りました（FREEAGENT_MAX_MODELS_PER_CALL）")
    return refs, info


def _rate_limit_report(answers: list[dict]) -> dict:
    limited = [a for a in answers if a.get("rate_limited")]
    skipped = sorted({ref for a in answers for ref in (a.get("skipped_cooling") or [])})
    # 「429 を踏まずに枠待ちで吸収した」件数（0 なら提供元の枠に余裕があった）。
    return {"rate_limited": len(limited), "skipped_cooling": skipped,
            "slot_waited": sum(1 for a in answers if (a.get("gate_wait_s") or 0) > 0)}


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
    free = free_model_refs(chat_only=True)          # サブLLM に使える Free だけ（§3.2）
    usable = [ref for ref in free if not is_cooling(ref)]
    query = as_str(args.get("query")) or as_str(args.get("q"))
    provider_filter = as_str(args.get("provider"))
    limit = as_int(args.get("limit"), 40, 1, 200)
    shown: list[dict] = []          # 検索モードで絞り込んだ一覧（非検索モードでは空）
    data = {
        "providers": status,
        "total_models": sum(row["models"] for row in status),
        "free_candidates": len(free),
        "non_chat_candidates": sum(row.get("non_chat", 0) for row in status),
        "usable_now": len(usable),
        "default_model": default_model(),
        "cooling": {ref: row for ref, row in cooling_refs().items()},
        "ranking_enabled": RANK_ENABLED,
    }
    harness = harness_status()   # §8.6（initialize 前は None）
    if harness:
        data["harness"] = harness

    searching = bool(query or provider_filter or args.get("all") or args.get("probe"))
    # probe は実際にモデルを呼ぶため、free_only 省略時でも有料モデルは対象にしない。
    free_only = as_flag(args.get("free_only")) or bool(args.get("probe"))
    if searching:
        rows = all_models()
        needle = query.lower()
        matched = [r for r in rows
                   if (not provider_filter or r.get("provider") == provider_filter)
                   and (not free_only or r.get("free"))
                   and (not needle or needle in str(r.get("id", "")).lower())]
        if args.get("probe") and not provider_filter:
            ready_rows = [r for r in matched if provider_ready(as_str(r.get("provider")))]
            if ready_rows:
                matched = ready_rows
        matched.sort(key=lambda r: (not r.get("free"),
                                    not provider_ready(as_str(r.get("provider"))),
                                    str(r.get("id", ""))))
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
            shown = [r for r in shown if r.get("free")][:probe_limit]
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
                if res.get("cooling_reason"):
                    err = f"{err} / {res['cooling_reason']}"
                err_l = err.lower()
                if not err:
                    verdict = "alive"
                elif "timed out" in err_l or "timeout" in err_l:
                    # **遅いだけかもしれない**（NVIDIA はコールドスタートが長い）。死んだ扱いにしない。
                    verdict = "slow"
                elif "空応答" in err:
                    # 応答自体は返っている（思考トークンで probe の予算を使い切った）。生存側に倒す。
                    verdict = "slow"
                elif "http 404" in err_l or "http 410" in err_l or (
                    "not found" in err_l and not re.search(r"\bhttp\s+[45]\d\d\b", err_l)
                ):
                    verdict = "gone"
                elif "http 403" in err_l:
                    # 403 は**署名があるときだけ**権限なし扱い。Cloudflare のブロック（Error 1010 等）や
                    # 提供元の障害・モデル単位の制限は、プロバイダ全体の問題ではないので `error`（残す）。
                    # 401 より先に見る（"HTTP 401/403" のような表記が 401 に誤マッチしていた実測がある）。
                    verdict = "auth" if _is_auth_error(403, err) else "error"
                elif "http 401" in err_l or "未設定/未確認です" in err:
                    verdict = "auth"
                else:
                    # 429（提供元が一時制限）や 5xx。**除外しない**（生きているが今は応えない）。
                    verdict = "error"
                return {"model": row, "verdict": verdict, "served_by": res.get("served_by"),
                        "error": truncate(err, 200),
                        "sample": truncate((res.get("text") or "").strip(), 60)}

            probed = run_parallel(shown, check, max_workers=probe_workers, deadline=call_budget())
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
    # **返り値のキーはモードで変えない**。`models`（絞り込んだ一覧）と
    # `free_models`（サブLLMに使える Free 全体）は**常に両方**を返す。片方だけだと
    # 利用側がモードで別のキーを読む羽目になる（実測: 引数なし → free_models のみ /
    # query 付き → models のみ）。
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
                "rate_limited": result.get("rate_limited"),
                **({"quota_accounting": result["quota_accounting"]} if result.get("quota_accounting") else {})}
    return {
        "answer": result["text"],
        "model": result["ref"],
        "served_by": result.get("served_by"),
        "fell_back": bool(result.get("fallback")),
        "latency_s": result.get("latency_s"),
        "truncated": result.get("truncated"),
        "cot_leak": result.get("cot_leak"),
        "tokens": result.get("tokens"),
        **({"quota_accounting": result["quota_accounting"]} if result.get("quota_accounting") else {}),
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
    prompts_dropped = max(0, len(prompts) - MAX_PROMPTS_PER_CALL)
    prompts = prompts[:MAX_PROMPTS_PER_CALL]
    explicit, cut = _expand_models(as_str_list(args.get("models")))
    if explicit:
        refs = explicit
        info = {"requested": True, "models": refs,
                "notes": [f"models は上限 {MAX_MODELS_PER_CALL} 件で切りました"
                          "（FREEAGENT_MAX_MODELS_PER_CALL）"] if cut else []}
    else:
        size = as_int(args.get("size"), 2, 1, MAX_SUB_MODELS)
        refs, info = select_models(size, None, kind="fanout")
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
        if res.get("quota_accounting"):
            row["quota_accounting"] = res["quota_accounting"]
        if res.get("coalesced"):
            row["coalesced"] = True      # 同一実行内の重複を畳んだ（上流は 1 回だけ）
        if res.get("error"):
            row["error"] = res["error"]
        else:
            row["answer"] = res["text"]
            row["served_by"] = res.get("served_by")
            row["cot_leak"] = res.get("cot_leak")
            row["truncated"] = res.get("truncated")
        rows.append(row)
    ok = sum(1 for r in rows if not r.get("error"))
    coalesced_calls = sum(1 for r in rows if r.get("coalesced"))
    sequential = round(sum(as_float(r.get("latency_s"), 0.0) for r in results), 3)
    return {
        "calls": len(rows), "ok": ok, "failed": len(rows) - ok, "coalesced_calls": coalesced_calls,
        "wall_s": wall, "sequential_estimate_s": sequential,
        "speedup": round(sequential / wall, 2) if wall > 0 else None,
        "results": rows, "selection": info, "concurrency": PROVIDER_GATE.report(refs),
        **_quota_report_after_answers(refs),
        "prompts_dropped": prompts_dropped,
        "participants": participants_of(refs, rows),
        "substitutions": substitutions_of(rows),
        **_rate_limit_report(results),
    }


# ---------------------------------------------------------------- §6.12 panel の統合（MoA 1 層）
#
# Mixture-of-Agents（Wang et al. 2024 / togethercomputer/MoA の手法）の**1 層だけ**を実装する。
# 参加モデルの回答を**別のモデル**が 1 つに統合する。論文は多層だが、層数だけ呼び出しが倍になるので
# Free の枠とクォータ（§3.5）を考えると 1 層（提案 N → 統合 1）が現実的（実測: 8 体 + 統合 1 = 9 呼び出し）。
# **統合は「平均」ではなく「事実確認して決める」**（平均すると誤りが薄まって残る）。
_REFINE_PROMPT = (
    "あなたは複数の独立した回答を統合する編集者です。平均せず、対立点は事実確認して決めてください。\n\n"
    "【問い】\n{question}\n\n【候補の回答】\n{candidates}\n\n"
    "次の形式だけで答えてください。\n"
    "結論: （統合した最終回答）\n"
    "確信度: （0-100 の整数）\n"
    "根拠: （候補のうち、どの番号の主張を採用し、どれを却下したか）"
)


# 統合役の選び方（v0.28.0）: **品質がほぼ同じ帯の中だけ**、平均所要の速い順に並べ替える。
# 実測（panel+refine・3 体・本機）: 壁 11.9 / 8.1 秒のうち **refine 単発が 4.4〜5.1 秒**（35〜63%）。
# refine は前段の出力に依存するので**並列化できない**。代わりに「誰に統合させるか」を速さで選ぶ。
# 品質の足切りは `REFINE_BAND`（既定 0.05）で、帯の外のモデルは決して選ばない（品質を犠牲にしない）。
# 観測が無いモデルは `_rank_latency_key` が inf なので、**学習前は従来と同じ**（品質順の先頭）。
REFINE_BAND = max(0.0, min(0.5, _env_float("FREEAGENT_REFINE_BAND", 0.05)))


def _pick_refiner(candidates: list[str], fallback: str = "品質順の先頭") -> tuple[str, str]:
    """統合役を選ぶ。品質の帯（`REFINE_BAND`）の中で**平均所要が速い**モデルを優先する。

    帯に該当が無い（学習前・帯の外しか居ない）ときは `fallback` の文面で品質順の先頭を返す
    （**表示の後方互換**＝既存の `refined.reason` の文言を保つ）。
    """
    ranked = rank_models(candidates)
    if not ranked:
        return "", ""
    if REFINE_BAND <= 0.0:
        return ranked[0], fallback
    best = _rank_score(ranked[0])
    band = [ref for ref in ranked if best - _rank_score(ref) <= REFINE_BAND]
    observed = [ref for ref in band if model_mean_latency(ref) is not None]
    if len(band) > 1 and observed:
        fastest = min(observed, key=lambda ref: float(model_mean_latency(ref) or 0.0))
        if fastest != ranked[0]:
            return fastest, (f"品質の帯（差 {REFINE_BAND} 以内）で最も速い"
                             f"（{float(model_mean_latency(fastest) or 0.0):.0f}ms）")
    return ranked[0], fallback


def _refine_answers(question: str, answers: list[dict], refs: list[str], args: dict) -> dict:
    """参加者の回答を 1 つの答えに統合する（MoA の集約層）。失敗も dict で返す（規約 1）。"""
    good = [a for a in answers if not a.get("error")]
    if not good:
        return {"model": None, "calls": 0, "error": "統合できる回答がありません（全員脱落）"}
    if budget_expired():
        # 予算を使い切った後の統合は 300 秒のクライアント待ちに当たるだけ（本体の結果は返す）。
        return {"model": None, "calls": 0,
                "error": "呼び出しの予算を使い切ったため統合を実行しませんでした（パネルの回答は有効です）"}
    requested = as_str(args.get("refine_model"))
    if requested:
        model, why = requested, "refine_model の指定"
    else:
        # 統合役は**参加していないモデル**（要求 ref と実応答 served_by の両方）を優先する。
        # 同じモデルに自分の答えを統合させると合意度が上がるだけで新しい情報が入らない。
        # 全員参加しているときだけ最上位が兼務する。
        used = set(refs) | {as_str(a.get("served_by")) for a in good if as_str(a.get("served_by"))}
        ex_set = set(as_str_list(args.get("exclude")) + env_excluded_models())
        cooling = cooling_refs()
        blocked = {p for p in PROVIDER_ORDER if provider_auth_blocked(p)}
        pool = [r for r in free_model_refs(free_only=True, chat_only=True)
                if r not in used and not _is_model_excluded(r, ex_set)
                and r not in cooling and r.split("/", 1)[0] not in blocked]
        if pool:
            model, why = _pick_refiner(pool, fallback="参加していないモデルの最上位")
        else:
            fallback_pool = [r for r in refs if not _is_model_excluded(r, ex_set)] or list(refs)
            model, why = _pick_refiner(fallback_pool, fallback="全員参加のため最上位が兼務")
    candidates = "\n".join(
        f"[{i + 1}] {a['model']}: {truncate(a.get('answer') or a.get('conclusion') or '', 900)}"
        for i, a in enumerate(good))
    res = call_model(model, _REFINE_PROMPT.format(question=question, candidates=candidates),
                     max_tokens=as_int(args.get("max_tokens"), 500, 16, 4000),
                     temperature=0.0, kind="panel_refine")
    out: dict = {"model": model, "calls": 1, "reason": why,
                 "from_panel": model in (set(refs) if requested else used)}
    if res.get("quota_accounting"):
        out["quota_accounting"] = res["quota_accounting"]
    if res.get("error"):
        out["error"] = truncate(res["error"], 240)
        return out
    parsed = parse_labeled(res["text"])
    out.update({
        "served_by": res.get("served_by"),
        "text": truncate(res["text"], 2000),
        "text_truncated": len(res["text"]) > 2000,
        "conclusion": parsed["conclusion"] or truncate(res["text"], 400),
        "confidence": parsed["confidence"],
        "cot_leak": res.get("cot_leak"),
        "truncated": res.get("truncated"),
        "rate_limited": res.get("rate_limited"),
        "gate_wait_s": res.get("gate_wait_s"),
    })
    return out


def tool_panel(args: dict) -> dict:
    """同じ問いを複数モデルへ投げ、合意度・不一致・CoT混入つきで返す。"""
    question = as_str(args.get("question") or args.get("prompt"))
    if not question:
        return {"error": "question は必須です"}
    refs, info = _select_or_error(args, default_size=3, kind="panel")
    if not refs:
        return _no_models()
    results = ask_many(refs, question, system=as_str(args.get("system")) or CONSULT_SYSTEM,
                       max_tokens=as_int(args.get("max_tokens"), 500, 16, 4000), kind="panel",
                       avoid=as_str_list(args.get("exclude")) or None)
    answers = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            answers.append({"model": ref, "error": res["error"],
                            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        parsed = parse_labeled(res["text"])
        answers.append({
            "model": ref, "served_by": res.get("served_by"),
            "label": parsed["conclusion"] or truncate(res["text"], 400),
            "conclusion": parsed["conclusion"] or truncate(res["text"], 400),
            "confidence": parsed["confidence"], "labels_found": parsed["labels_found"],
            "question": parsed["question"], "answer": truncate(res["text"], 1500),
            "text_truncated": len(res["text"]) > 1500,
            "cot_leak": res.get("cot_leak"), "truncated": res.get("truncated"),
            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {}),
        })
    good = [a for a in answers if not a.get("error")]
    independent = independent_answers(good)
    conf = [a["confidence"] for a in independent if isinstance(a.get("confidence"), int)]
    agree = agreement_detail([a["conclusion"] for a in independent])
    # `refine: true` で MoA の集約層（§6.12）。統合役の呼び出しも枠・クォータ・統計を通る。
    refine = _refine_answers(question, answers, refs, args) if as_flag(args.get("refine")) else None
    reported = results + ([refine] if refine and refine.get("calls") else [])
    data = {
        "question": question, "models": refs, "selection": info,
        "answered": len(good), "independent_sources": len(independent), "failed": len(answers) - len(good),
        "agreement": agree["value"], "agreement_source": agree["source"],
        "agreement_votes": agree["votes"],
        "participants": participants_of(refs, answers),
        "substitutions": substitutions_of(answers),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "confidence_source": "self_report",
        "confidence_warning": confidence_warning(
            round(sum(conf) / len(conf), 1) if conf else None, agree["value"]),
        "consensus": _consensus_groups(independent),
        "disagreements": [a["model"] for a in good if a.get("question")],
        "open_questions": [{"model": a["model"], "question": a["question"]}
                           for a in good if a.get("question")],
        "answers": answers,
        "concurrency": PROVIDER_GATE.report(refs),
        **_quota_report_after_answers(refs),
        **_rate_limit_report(reported),
        "agreement_note": _agreement_note(agree["source"]),
    }
    if refine is not None:
        data["refined"] = refine
        data["calls"] = len(results) + int(refine.get("calls") or 0)
    return data


def tool_lookup(args: dict) -> dict:
    """外部知識を出典つきで取得する（LLM を使わない＝幻覚が入らない経路）。"""
    query = as_str(args.get("query") or args.get("topic") or args.get("q"))
    if not query:
        return {"error": "query は必須です"}
    return knowledge_lookup(query,
                            as_str_list(args.get("sources")) or None,
                            limit=as_int(args.get("limit"), 3, 1, 10),
                            lang=as_str(args.get("lang"), ""),
                            kind=as_str(args.get("github_kind"), "repo"),
                            datacite_kind=as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True,
                            doc=as_str(args.get("doc")))


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
    # **検索語と質問文を分けられる**（v0.26.0）。arXiv / Crossref / Internet Archive は英語の語で
    # 引く必要があり、日本語の質問をそのまま投げると 0 件（arXiv は CJK を早期拒否）になる。
    # `search_query` を省略したときは従来どおり question をそのまま検索語にする。
    search_query = as_str(args.get("search_query")) or question
    kb = knowledge_lookup(search_query, sources, limit=as_int(args.get("limit"), 3, 1, 10),
                          lang=as_str(args.get("lang"), ""),
                          kind=as_str(args.get("github_kind"), "repo"),
                          datacite_kind=as_str(args.get("datacite_kind"), "all"), fallback=args.get("fallback") is True,
                          doc=as_str(args.get("doc")),
                          # **本文根拠が要る場面**（§6.18）。crossref は `has-abstract:true` を、
                          # hn はコメントも検索する（根拠の充填率を上げる）。
                          need_evidence=True)
    citations = [c for c in (kb.get("citations") or []) if _kb_has_evidence(c)]
    if not citations:
        return {"error": "根拠が 0 件でした。query を変えるか sources を広げてください",
                "lookup": {"sources": kb.get("sources"), "errors": kb.get("errors"),
                           "unknown_sources": kb.get("unknown_sources") or []}}
    window = _evidence_window(citations)
    if not window["numbers"]:
        return {"error": "本文根拠を注入できませんでした。根拠の取得・本文予算を確認してください"}
    # **根拠 0 件・本文なしで早期に返す経路では選抜を走らせない**（提供元へ無駄な一覧要求を出さない）。
    # モデル選抜を知識取得と重ねる案は実測で採らなかった: 一覧がディスクキャッシュ（§3.3）で温まる
    # ため、重ねて縮むのは初回だけの 0.3 秒で、早期返却の契約を壊すほうが損。
    refs, info = _select_or_error(args, default_size=2, kind="grounded")
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
            answers.append({"model": ref, "error": res["error"],
                            **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        cited, unsupported = _cited_numbers(res["text"] or "", len(citations), allowed=window["numbers"])
        answers.append({"model": ref, "served_by": res.get("served_by"),
                        "answer": truncate(res["text"], 2000),
                        "cited": cited, "cited_ok": bool(cited), "unsupported_citations": unsupported,
                        "truncated": res.get("truncated"), "cot_leak": res.get("cot_leak"),
                        **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
    good = [a for a in answers if not a.get("error")]
    _grounded_agree = agreement_detail([a.get("answer") or "" for a in good])
    return {
        "question": question, "search_query": search_query,
        "citations": citations, "citation_count": len(citations),
        "sources_used": kb.get("sources"), "source_errors": kb.get("errors"),
        "injected_citations": window["numbers"], "not_injected_citations": window["omitted"],
        "evidence_citation_count": len(window["numbers"]),
        "unsupported_citations": sorted({n for row in good for n in row.get("unsupported_citations", [])}),
        "models": refs, "selection": info, "answers": answers,
        "concurrency": PROVIDER_GATE.report(refs),
        **_quota_report_after_answers(refs),
        "agreement": _grounded_agree["value"], "agreement_source": _grounded_agree["source"],
        "agreement_votes": _grounded_agree["votes"],
        "participants": participants_of(refs, answers),
        "substitutions": substitutions_of(answers),
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
    # **黙って捨てない**（規約 63）: 上限を超えた分は実行せず、件数を返す。
    items_dropped = max(0, len(items) - MAX_MAP_ITEMS)
    items = items[:MAX_MAP_ITEMS]
    model = as_str(args.get("model"))
    selection = {"requested": True, "models": [model]} if model else {}
    if not model:
        selected, selection = select_models(1, kind="map")
        model = selected[0] if selected else ""
    if not model:
        return _no_models()
    max_tokens = as_int(args.get("max_tokens"), 500, 16, 4000)
    prompts = [f"{instruction}\n\n---\n{truncate(item, 4000)}" for item in items]
    # 進捗のラベルに使うので **(要素, プロンプト) の組**で流す（プロンプト全体をラベルにしない）
    pairs = list(zip(items, prompts))
    # **予算を渡す**（渡さないと 240 秒の打ち切りが効かず、遅い要素 1 件で 300 秒の
    # クライアント待ちに当たって**取れていた結果まで失われる**。実測: 0.1 秒予算でも完走した）。
    map_started = now_ts()
    results = run_parallel(pairs, lambda pair: call_model(model, pair[1], max_tokens=max_tokens, kind="map"),
                           max_workers=MAX_WORKERS, deadline=call_budget(), fair_keys=prompts,
                           fair_providers=[split_ref(model)[0]] * len(pairs))
    wall = round(now_ts() - map_started, 3)
    sequential = round(sum(as_float(res.get("latency_s"), 0.0) for res in results), 3)
    rows = []
    for item, res in zip(items, results):
        row = {"item": truncate(item, 200),
               "output": truncate(res.get("text") or "", 1500) if not res.get("error") else "",
               "error": res.get("error") or ""}
        if res.get("quota_accounting"):
            row["quota_accounting"] = res["quota_accounting"]
        if res.get("coalesced"):
            row["coalesced"] = True      # 同一実行内の重複を畳んだ（上流は 1 回だけ）
        rows.append(row)
    ok = sum(1 for r in rows if not r["error"])
    coalesced_calls = sum(1 for r in rows if r.get("coalesced"))
    data = {"model": model, "count": len(rows), "ok": ok, "failed": len(rows) - ok, "results": rows,
            "items_dropped": items_dropped, "coalesced_calls": coalesced_calls, "selection": selection,
            # 所要時間は **fanout と同じ形**で返す（v0.42.2・issue #9）。サーバーが返さないと
            # 「並列が速い」ことを content でも言えない（実測: 12 件で 6.3s / 逐次 24.1s・3.8 倍）。
            "wall_s": wall, "sequential_estimate_s": sequential,
            "speedup": round(sequential / wall, 2) if wall > 0 and sequential else None}
    if as_flag(args.get("reduce")) and budget_expired():
        # 後段の統合は 1 回ぶんの推論を足す。予算切れで始めると 300 秒の待ちに当たるだけ。
        data["reduce_error"] = ("呼び出しの予算を使い切ったため reduce を実行しませんでした"
                                "（各要素の出力は results に残しています）")
    elif as_flag(args.get("reduce")):
        joined = "\n".join(f"- {r['output']}" for r in rows if r["output"])
        if joined.strip():
            reduce_prompt = (f"次の {len(rows)} 件の出力を統合してください。"
                            f"重複をまとめ、対立点は残し、出典番号や行番号を落とさない。\n\n{truncate(joined, 12000)}")
            reduce_started = now_ts()
            red = call_model(as_str(args.get("reduce_model")) or model, reduce_prompt,
                             max_tokens=as_int(args.get("reduce_max_tokens"), 900, 16, 4000),
                             kind="map_reduce")
            data["reduce_s"] = round(now_ts() - reduce_started, 3)
            data["reduced"] = red.get("text") or ""
            if red.get("quota_accounting"):
                data["reduce_quota_accounting"] = red["quota_accounting"]
            if red.get("error"):
                data["reduce_error"] = red["error"]
    return data


def _consult_prompt(question: str, main_reply: str, round_no: int, peers: list[dict] | None = None,
                    mode: str = "discuss", draft: str = "", basis: str = "",
                    stage: str = "quality") -> str:
    parts = [f"【問い】\n{question}"]
    if mode == "review" and draft:
        parts.append(f"【査読対象の下書き】\n{truncate(draft, 4000)}")
    if basis:
        parts.append(f"【仕様・計画（これに適合するかを見る）】\n{truncate(basis, 3000)}")
    if main_reply:
        parts.append(f"【メインからの回答（権威ある前提として扱う）】\n{truncate(main_reply, 3000)}")
    if peers:
        block = "\n".join(f"- {p['model']}: {truncate(p.get('conclusion') or p.get('text') or '', 400)}"
                          for p in peers)
        parts.append(f"【他の参加者の回答（第{round_no}ラウンド）】\n{block}")
    if stage == "compliance":
        parts.append("上の形式（結論/判定/指摘/メインに確認したい点）だけで答えてください。"
                     "**品質の良し悪しは書かず**、仕様・計画の各要件を満たすかどうかだけを見てください。")
    elif mode == "review" and draft:
        parts.append("上の形式（結論/重要度/指摘/メインに確認したい点）だけで答えてください。"
                     "5 軸（correctness / readability / architecture / security / performance）を"
                     "一通り見てから、最も重い重要度を 1 つ名乗ってください。")
    else:
        parts.append("上の形式（結論/確信度/メインに確認したい点）だけで答えてください。"
                     "確認したい点は最も重要な 1 つだけにし、推奨の既定値を「既定: …」で添えてください。")
    return "\n\n".join(parts)


def _previous_questions(rounds: list[dict]) -> list[str]:
    """直前ラウンドで出ていた質問（「解消した」を数えるため）。無ければ空。"""
    if len(rounds) < 2:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for row in rounds[-2].get("answers") or []:
        question = as_str(row.get("question")).strip()
        if question and norm_text(question) not in seen:
            seen.add(norm_text(question))
            out.append(question)
    return out


def tool_consult(args: dict) -> dict:
    """メイン↔サブの双方向相談。`debate_depth="deep"` で独立回答→反論→統合の 3 段討論。"""
    sid = as_str(args.get("session_id"))
    main_reply = as_str(args.get("main_reply"))
    draft = as_str(args.get("draft"))
    basis = as_str(args.get("basis"))
    mode = as_str(args.get("mode"), "discuss")
    depth = as_str(args.get("debate_depth"), "normal")
    stored = session_get(sid) if sid else None
    if stored:
        # 再開では mode / draft / basis を引き継ぐ（渡されなければ保存値）。
        # 2 段査読は basis が無いと成立しないので、ここを落とすと再開で片肺になる（実測で踏んだ）。
        mode = as_str(args.get("mode")) or as_str(stored.get("mode"), mode)
        draft = draft or as_str(stored.get("draft"))
        basis = basis or as_str(stored.get("basis"))
    question = as_str(args.get("question") or (stored or {}).get("question"))

    if stored and not args.get("question"):
        question = stored.get("question") or ""
        if not main_reply:
            return {"error": f"セッション {sid} を再開するには main_reply（または question）が必要です",
                    "session_id": sid, "stage": stored.get("stage")}
    if not question:
        return {"error": "question は必須です（session_id を付けると前回の問いを引き継ぎます）"}

    if stored and stored.get("models"):
        # A compliance review retains its original slots, including cooling models.
        saved_refs = [m for m in stored["models"] if m][:MAX_MODELS_PER_CALL]
        excluded = set(as_str_list(args.get("exclude")) + env_excluded_models())
        blocked_refs = [ref for ref in saved_refs if _is_model_excluded(ref, excluded)]
        if blocked_refs:
            return {"error": "保存済みセッションに除外対象のモデルがあります。除外設定を確認するか、"
                             "別のモデルで新しいセッションを開始してください: " + ", ".join(blocked_refs),
                    "session_id": sid, "excluded_models": blocked_refs}
        seen_slots = {}
        for ref in saved_refs:
            identity = _model_identity(ref)
            if identity in seen_slots:
                return {"error": f"Duplicate model identity in saved session: {seen_slots[identity]}, {ref}. Start a new session with distinct models.",
                        "session_id": sid}
            seen_slots[identity] = ref
        refs = saved_refs if mode == "review" and draft and basis else [m for m in saved_refs if not is_cooling(m)]
        info = {"requested": True, "resumed_from": sid, "models": refs}
        if not refs:
            refs, info = _select_or_error(args, default_size=3, kind="consult")
    else:
        refs, info = _select_or_error(args, default_size=3, kind="consult")
    if not refs:
        return _no_models()

    rounds: list[dict] = list((stored or {}).get("rounds") or [])
    # 査読は 4 行（結論/重要度/指摘/確認点）を要求するので既定を上げる（実測: 500 で指摘が切れた）。
    review_mode = mode == "review" and bool(draft)
    max_tokens = as_int(args.get("max_tokens"), 800 if review_mode else 600, 16, 4000)
    replies = list((stored or {}).get("main_replies") or [])
    if main_reply:
        replies.append(main_reply)

    # 第1ラウンド（または再開時の再検討）
    round_no = len(rounds) + 1
    # `basis` を渡したときだけ、先に**仕様適合**の段を 1 回走らせる（2 段目が品質）。
    # 再開時は保存済みの適合結果を使い回す（毎ラウンド 2 倍の呼び出しを払わない）。
    compliance_rows: list[dict] = []
    compliance_failures: list[dict] = []
    compliance_summary = None
    if review_mode and basis:
        import hashlib
        fingerprint = hashlib.sha256(_dumps([question, draft, basis, main_reply]).encode("utf-8")).hexdigest()
        stored_compliance = next((r for r in reversed(rounds) if r.get("kind") == "compliance"), None)
        # Legacy rounds with no fingerprint (or an older fingerprint) are re-reviewed conservatively.
        if stored_compliance and stored_compliance.get("fingerprint") != fingerprint:
            stored_compliance = None
        prior = {row.get("model"): row for row in (stored_compliance or {}).get("answers", [])
                 if isinstance(row, dict) and not row.get("error")}
        excluded_now = set(as_str_list(args.get("exclude")) + env_excluded_models())
        blocked_prior = [as_str(row.get("served_by")) for row in prior.values()
                         if as_str(row.get("served_by"))
                         and _is_model_excluded(as_str(row.get("served_by")), excluded_now)]
        if blocked_prior:
            return {"error": "保存済み査読の回答モデルが除外対象です。除外設定を確認するか、"
                             "別のモデルで新しいセッションを開始してください: " + ", ".join(blocked_prior),
                    "session_id": sid, "excluded_models": blocked_prior}
        missing = [ref for ref in refs if ref not in prior]
        crows = [prior[ref] for ref in refs if ref in prior]
        seen_reviewers = {}
        for row in crows:
            reviewer = row.get("served_by") or row["model"]
            identity = _model_identity(reviewer)
            if identity in seen_reviewers:
                return {"error": f"Duplicate reviewer identity in saved session: {seen_reviewers[identity]}, {reviewer}. Start a new session with distinct models.",
                        "session_id": sid}
            seen_reviewers[identity] = reviewer
        cooling_missing = [ref for ref in missing if stored and is_cooling(ref)]
        crows.extend({"model": ref, "error": "適合査読はクールダウン中のため未実施です"}
                     for ref in cooling_missing)
        pending = [ref for ref in missing if ref not in cooling_missing]
        if pending:
            cprompt = _consult_prompt(question, main_reply, round_no, mode=mode, draft=draft,
                                      basis=basis, stage="compliance")
            used_reviewers = [name for row in crows if not row.get("error")
                              for name in (row.get("model"), row.get("served_by"))
                              if isinstance(name, str) and name]
            cres = ask_many(pending, cprompt, system=COMPLIANCE_SYSTEM, max_tokens=max_tokens,
                            kind="consult", avoid=as_str_list(args.get("exclude")),
                            identity_avoid=used_reviewers + cooling_missing)
            for index, ref in enumerate(pending):
                res = cres[index] if index < len(cres) else {"error": "適合査読の応答がありません"}
                if res.get("error"):
                    crows.append({"model": ref, "error": res["error"],
                                  **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
                    continue
                row = {"model": ref, "served_by": res.get("served_by"),
                       "conclusion": truncate(res["text"], 400),
                       "text": truncate(res["text"], 1500),
                       "truncated": bool(res.get("truncated")),
                       "redactions": as_int(res.get("redactions"), 0, 0, 999),
                       **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})}
                row.update(parse_compliance(res["text"]))
                crows.append(row)
        if missing:
            rounds.append({"round": round_no, "kind": "compliance", "answers": crows,
                           "fingerprint": fingerprint})
            round_no += 1
        compliance_rows = [row for row in crows if not row.get("error")]
        compliance_failures = [row for row in crows if row.get("error")]
        compliance_summary = summarize_compliance(crows)
    prompt = _consult_prompt(question, main_reply, round_no, mode=mode, draft=draft)
    results = ask_many(refs, prompt, system=REVIEW_SYSTEM if review_mode else CONSULT_SYSTEM,
                       max_tokens=max_tokens, kind="consult",
                       avoid=as_str_list(args.get("exclude")) or None)
    round_rows = []
    for ref, res in zip(refs, results):
        if res.get("error"):
            round_rows.append({"model": ref, "error": res["error"],
                               **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
            continue
        parsed = parse_labeled(res["text"])
        row = {"model": ref, "served_by": res.get("served_by"),
               "conclusion": parsed["conclusion"] or truncate(res["text"], 400),
               "confidence": parsed["confidence"], "question": parsed["question"],
               "text": truncate(res["text"], 1500), "cot_leak": res.get("cot_leak"),
               "truncated": bool(res.get("truncated")),
               "redactions": as_int(res.get("redactions"), 0, 0, 999),
               **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})}
        if review_mode:
            row.update(parse_review(res["text"]))
        round_rows.append(row)
    quality_rounds = [r for r in rounds if r.get("kind") in ("initial", "reconsider")]
    rounds.append({"round": round_no,
                   "kind": "reconsider" if quality_rounds else "initial",
                   "answers": round_rows})

    debate_summary = None
    _conf_by_model: dict = {}
    do_debate = (depth == "deep") and not budget_expired()
    if depth == "deep" and not do_debate:
        dropped_rows = [_dropped_result(call_budget(), "未実行") for _ in refs]
        debate_rows = [{"model": ref, "error": r.get("error")} for ref, r in zip(refs, dropped_rows)]
        rounds.append({"round": round_no + 1, "kind": "debate", "answers": debate_rows})
        dropped = [{"round": r["round"], "kind": r["kind"], "model": a.get("model"),
                    "error": truncate(a.get("error") or "", 160)}
                   for r in rounds for a in r["answers"] if a.get("error")]
        debate_summary = {"dropped": dropped,
                          "error": "呼び出しの予算を使い切ったため討論を実行しませんでした（初回回答は有効です）"}
    if do_debate:
        peers = [row for row in round_rows if not row.get("error")]
        rebuttal_prompt = _consult_prompt(question, main_reply, round_no, peers=peers)
        rebuttal = ask_many(refs, rebuttal_prompt, system=DEBATE_SYSTEM,
                            max_tokens=max_tokens, kind="debate",
                            avoid=as_str_list(args.get("exclude")) or None)
        _conf_by_model = {r["model"]: r.get("confidence") for r in round_rows
                          if isinstance(r.get("confidence"), int)}
        debate_rows = []
        for ref, res in zip(refs, rebuttal):
            if res.get("error"):
                debate_rows.append({"model": ref, "error": res["error"],
                                    **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
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
                                "unresolved": truncate(labels.get("未解決") or "", 400),
                                **({"quota_accounting": res["quota_accounting"]} if res.get("quota_accounting") else {})})
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
        # 確信度の較正（v0.42.6）: **討論で立場を変えた**を誤答の代理ラベルとして数える。
        # 真の正誤ラベルではないので割合だけを出す（SPEC §6.52）。
        for part in debate_summary["participants"]:
            conf = _conf_by_model.get(part.get("model"))
            if isinstance(conf, int):
                note_confidence(part["model"], conf, flipped=bool(part.get("changed")))

    if do_debate:
        debate_by_model = {row.get("model"): row for row in debate_rows}
        last = []
        for initial in round_rows:
            if initial.get("error"):
                last.append(initial)
                continue
            debated = debate_by_model.get(initial.get("model"), {})
            if debated.get("error"):
                last.append(initial)
                continue
            last.append({
                **initial, "model": initial["model"], "served_by": debated.get("served_by") or initial.get("served_by"),
                "conclusion": debated.get("position") or initial.get("conclusion") or "",
                "confidence": initial.get("confidence"), "question": initial.get("question") or "",
                "answer": debated.get("text") or initial.get("text") or "",
                "text": debated.get("text") or initial.get("text") or "",
            })
    else:
        last = [row for row in round_rows if not row.get("error")]
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
    # 上限までをメインへ返し、超えた分は deferred に回す（黙って捨てない。規約 8）。
    reviewed = [{"model": q["model"], "question": q["question"],
                 "default": parse_question_default(q["question"])[1]} for q in open_questions]
    kept, deferred = reviewed[:QUESTION_CAP], reviewed[QUESTION_CAP:]
    open_questions_for_main = [q["question"] for q in kept]
    prev_questions = _previous_questions(rounds)
    current_norm = {norm_text(q["question"]) for q in kept}
    resolved = [q for q in prev_questions if norm_text(q) not in current_norm]
    coverage = {"status": "clear" if not kept else "outstanding",
                "resolved": resolved, "outstanding": len(kept), "deferred": len(deferred),
                "note": "resolved は質問文の表層一致で判定します（言い換えは解消と誤判定しえます）"}
    findings_summary = summarize_review(last) if review_mode else None
    if findings_summary is not None:
        findings_summary["truncated"] = sum(1 for row in last if row.get("truncated"))
        if compliance_summary:
            # 仕様に適合していない・判定を名乗らない、は品質の重さに関係なく差し戻し。
            findings_summary["compliance_blocking"] = compliance_summary["blocking"]
            findings_summary["blocking"] = (findings_summary["blocking"]
                                            or compliance_summary["blocking"])
    review_rows = ([{"model": row.get("model"), "severity": row.get("severity") or "",
                     "findings": row.get("findings") or [], "axes": row.get("axes") or [],
                     "truncated": bool(row.get("truncated"))}
                    for row in last] if review_mode else [])
    sid = sid or new_session_id()
    session_put(sid, {
        "question": question, "models": refs, "rounds": rounds,
        "main_replies": replies, "mode": mode, "depth": depth, "draft": draft,
        "basis": basis,
        "stage": "awaiting_main" if open_questions else "complete",
        "created_at": (stored or {}).get("created_at") or now_ts(),
    })
    conf = [row["confidence"] for row in last if isinstance(row.get("confidence"), int)]
    quota_accounting = [{"round": r.get("round"), "kind": r.get("kind"), "model": a.get("model"),
                         "warning": a["quota_accounting"]}
                        for r in rounds for a in r.get("answers", []) if a.get("quota_accounting")]
    _consult_agree = agreement_detail([row.get("conclusion") or "" for row in last])
    return {
        "session_id": sid,
        "mode": mode,
        "stage": "awaiting_main" if open_questions else "complete",
        "rounds_run": len(rounds),
        "question": truncate(question, 400),
        "models": refs, "selection": info,
        "concurrency": PROVIDER_GATE.report(refs),
        **_quota_report_after_answers(refs),
        "quota_accounting": quota_accounting,
        "agreement": _consult_agree["value"], "agreement_source": _consult_agree["source"],
        "agreement_votes": _consult_agree["votes"],
        "participants": participants_of(refs, last),
        "substitutions": substitutions_of(last),
        "agreement_note": _agreement_note(_consult_agree["source"]),
        "confidence_mean": round(sum(conf) / len(conf), 1) if conf else None,
        "confidence_source": "self_report",
        "confidence_warning": confidence_warning(
            round(sum(conf) / len(conf), 1) if conf else None, _consult_agree["value"]),
        "confidence_calibration": confidence_calibration([r.get("model") for r in round_rows]),
        "consensus": last,
        "failed": [{"model": row.get("model"), "error": truncate(row.get("error") or "", 160)}
                   for row in compliance_failures + rounds[-1]["answers"] if row.get("error")],
        "open_questions_for_main": open_questions_for_main,
        "open_questions": kept,
        "deferred_questions": deferred,
        "coverage": coverage,
        "question_budget": {"cap": QUESTION_CAP, "open": len(kept), "deferred": len(deferred)},
        "findings_summary": findings_summary,
        "review": review_rows,
        "redactions": sum(as_int((row or {}).get("redactions"), 0, 0, 999) for row in last),
        "stages": (["compliance", "quality"] if compliance_summary else ["quality"])
                  if review_mode else ["discuss"],
        "compliance_summary": compliance_summary,
        "compliance": compliance_rows,
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
    """JSON だけを取り出す（コードフェンスや前置き・末尾の補足に `{}` が混ざっても最初の dict を読む）。"""
    return _jsonutil.extract_object(text)



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
    refs, info = _select_or_error(args, default_size=2, kind="agent")
    if not refs:
        return _no_models()
    max_steps = as_int(args.get("max_steps"), 2, 1, 4)
    main_reply = as_str(args.get("main_reply"))
    max_tokens = as_int(args.get("max_tokens"), 600, 16, 3000)
    base = f"【依頼】\n{task}"
    if main_reply:
        base += f"\n\n【メインからの回答（最優先の前提）】\n{truncate(main_reply, 3000)}"

    claims = _ModelClaims(refs, as_str_list(args.get("exclude")) + env_excluded_models() or None)

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
            if budget_expired():
                return {"model": ref, "steps": step, "trace": trace, "answer": "",
                        "steps_exhausted": True, "timed_out": True, "dropped": True,
                        "error": f"並列実行の予算で打ち切りました（ステップ {step}/{max_steps}）",
                        "evidence": evidence[:8], "citations": registry, "bibliography": bibliography,
                        "injected_citations": sorted(injected), "cited": [], "cited_ok": False}
            final_step = step == max_steps - 1
            res = call_model(ref, history + f"\n\n（{step + 1}/{max_steps} ステップ目。"
                                            "JSON を1つだけ出力）",
                             system=AGENT_SYSTEM_FINAL if final_step else AGENT_SYSTEM,
                             max_tokens=max_tokens, kind="agent", claims=claims)
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

    # サブエージェントは 1 体あたり最大 4 ステップ（各ステップが推論 1 回）走るので、
    # 予算を渡さないと `max_steps` の掛け算で 300 秒を超える（`tool_map` と同じ穴）。
    results = run_parallel(refs, run_one, max_workers=min(len(refs), MAX_WORKERS),
                           deadline=call_budget())
    good = [r for r in results if not r.get("error")]
    citations, seen = [], set()
    for row in good:
        for cite in row.get("citations") or []:
            key = _kb_citation_key(cite)
            if key not in seen:
                seen.add(key)
                citations.append(cite)
    _agent_agree = agreement_detail([r.get("answer") or "" for r in good])
    return {
        "task": truncate(task, 300), "models": refs, "selection": info,
        "agents": results, "answered": len(good), "failed": len(results) - len(good),
        "tool_calls": sum(len(r.get("trace") or []) for r in results),
        "citations": citations, "citation_count": len(citations),
        "agreement": _agent_agree["value"], "agreement_source": _agent_agree["source"],
        "agreement_votes": _agent_agree["votes"],
        "participants": participants_of(refs, results),
        "substitutions": substitutions_of(results),
        "answers_with_citations": sum(1 for r in good if r.get("cited_ok")),
        "unsupported_citations": sorted({n for r in good
                                        for n in (r.get("unsupported_citations") or [])}),
        "usage_note": "サブエージェントは読み取り専用の知識ツールのみ呼べます。書き込みはしません。"
                      "回答中の [n] は根拠の番号と照合し、根拠に無い番号は unsupported_citations に出します。",
    }


# 完了報告の条件。**description の願いではなく依頼文に必ず前置きする**（サブプロセスは
# description を読まないので、文面だけでは強制できない）。不要なら verify_required=false。
DELEGATE_VERIFY_PREFIX = (
    "【完了報告の条件（この依頼では必ず守る）】\n"
    "- 最終報告に、実行した検証コマンドとその出力（成功/失敗）を必ず含める\n"
    "- 確かめていない項目は「未検証」と明記する（推測で埋めない）\n"
    "- 失敗が 1 つでもあれば「完了」と書かない\n"
    "\n"
    "【依頼】\n"
)


def tool_delegate(args: dict) -> dict:
    """フルツール付きの Hermes 本体を独立プロセスで起動する（opt-in・重い経路）。"""
    if not ALLOW_AGENT:
        return {"error": "この経路は無効です。FREEAGENT_ALLOW_AGENT=1 で有効化してください",
                "enabled": False}
    task = as_str(args.get("task"))
    if not task:
        return {"error": "task は必須です"}
    timeout = as_int(args.get("timeout"), 300, 30, 1800)
    # 真偽の既定は `dict.get(既定)` に載せない（明示 None で既定が死ぬ。規約 D）。
    verify_raw = args.get("verify_required")
    verify_required = True if verify_raw is None else as_flag(verify_raw)
    effective = (DELEGATE_VERIFY_PREFIX + task) if verify_required else task
    import subprocess
    started = now_ts()
    try:
        _nw = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.run([HERMES_BIN, "chat", "-q", effective], capture_output=True,
                              timeout=timeout, text=True, encoding="utf-8", errors="replace",
                              creationflags=_nw)
        return {"task": truncate(task, 300), "verification_enforced": verify_required,
                "task_effective": truncate(effective, 400),
                "exit_code": proc.returncode,
                "stdout": truncate(proc.stdout or "", 4000),
                "stderr": truncate(proc.stderr or "", 1000),
                "elapsed_s": round(now_ts() - started, 1),
                "next_step": "返ってきた報告の証拠を freeagent_admit(action=\"report\", "
                             "report={kind, evidence:{commands:[{cmd,result}]}}) に渡すと"
                             "受理/差し戻しを決定論で判定できます"}
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


def _think_ledger_view(steps: list[dict], *, keep: int = 8, row: dict | None = None,
                       brief: bool = False) -> dict:
    """台帳の要約（計画・分岐・修正・仮説・最新の思考）。**全文は返さず末尾だけ**を載せる。

    `brief=True` は**現行の道筋だけ**を載せる（改訂済み・棄却分岐の本文を省く。文脈圧縮後の復帰用）。
    省いた件数は `omitted` に残す（黙って隠さない）。
    """
    row = row or {}
    meta = row.get("branch_meta") if isinstance(row.get("branch_meta"), dict) else {}
    branches: dict[str, list[int]] = {bid: [] for bid in meta}
    for step in steps:
        if step.get("branch_id"):
            branches.setdefault(step["branch_id"], []).append(as_int(step.get("n"), 0, 0, 9999))
    active = _think_active(steps, row)
    plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    shown = active if brief else steps[-keep:]
    out = {
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
        # `id` は**同一性**（差し替えをまたいで不変）／`position` は今の plan での並び順（1 始まり）。
        # `subgoal` はこの id を指す（§6.10）。
        "plan": [{"id": p.get("id"), "position": i + 1,
                  "text": truncate(p.get("text") or "", 200),
                  "done": bool(p.get("done_at")), "done_at": p.get("done_at"),
                  "steps": [as_int(s.get("n"), 0, 0, 9999) for s in steps
                            if as_int(s.get("subgoal"), 0, 0, 999) == p.get("id")]}
                 for i, p in enumerate(plan)],
        # 計画から抜けた項目（廃止）。**記録を黙って消さない**（§2.7）。
        "plan_retired": [{"id": p.get("id"), "position": None,
                          "text": truncate(p.get("text") or "", 200),
                          "done": bool(p.get("done_at")), "done_at": p.get("done_at"),
                          "steps": [as_int(s.get("n"), 0, 0, 9999) for s in steps
                                    if as_int(s.get("subgoal"), 0, 0, 999) == p.get("id")]}
                         for p in retired],
        "plan_progress": {"done": sum(1 for p in plan if p.get("done_at")), "total": len(plan)},
        # 廃止した達成済みの件数。`plan_progress` は**今の plan の項目だけ**を数えるので、
        # 達成済みの項目を plan から外すと進捗が減って見える（実測: 1/3 → 0/3）。
        # 数を消さずに別枠で出し、描画側でも明示する（§6.16）。
        "plan_retired_done": sum(1 for p in retired if p.get("done_at")),
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
                    "text": truncate(s.get("text") or "", 200 if brief else 300)} for s in shown],
        # 直近に宣言された見積り総数（増減してよい・調整はメインが行う）。台帳に残して履歴化する。
        "total_thoughts": _think_latest_total(steps),
        "total_history": [h for h in (row.get("total_history") or []) if isinstance(h, dict)],
    }
    if brief:
        active_ns = {as_int(s.get("n"), 0, 0, 9999) for s in active}
        out["brief"] = True
        # 省いた件数は隠さない（「要約だから無い」と「そもそも無い」を混同させない）。
        out["omitted"] = sum(1 for s in steps if as_int(s.get("n"), 0, 0, 9999) not in active_ns)
    return out


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
    if ledger.get("steps_recorded", 0) >= 12 and not data.get("brief"):
        out.append(f"台帳が {ledger['steps_recorded']} ステップあります。読み戻すときは view=true に "
                   "brief=true を併用すると、現行の道筋だけを軽く読めます。")
    return out


def _think_verify_block(args: dict, question: str, steps: list, step: dict,
                       preview: dict, used: list, sid_in: str):
    """`verify=true` の検証を実行して (verification, used, failure) を返す。

    `failure` は**環境障害で台帳を汚さずに終わらせる場合の応答**（`None` なら正常）。
    検証は独立モデルによる反証探索であり、合意の収集ではない（§7.2）。
    """
    refs, info = select_models(as_int(args.get("size"), 2, 1, 4),
                               as_str_list(args.get("models")) or None,
                               prefer=as_str_list(args.get("prefer")) or None,
                               exclude=as_str_list(args.get("exclude")) or None,
                               identity_exclude=used)
    if not refs:
        return None, used, _no_models()   # 状態を書かない（環境障害で台帳を汚さない）
    results = ask_many(refs, _think_prompt(question, steps, step, preview), system=THINK_CRITIC_SYSTEM,
                       max_tokens=as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                       avoid=as_str_list(args.get("exclude")) or None, identity_avoid=used)
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
        return None, used, {"error": "検証に到達できませんでした（バックエンド不通: "
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
    # `used` には**要求したモデルだけでなく実際に答えたモデル（`served_by`）**も入れる。
    # 要求名だけだと、`longcat` が `laguna-s-2.1` へフォールバックした直後に、同じ呼び出しの
    # 「代替案の提案者」が再び `laguna-s-2.1` に決まり、検証者と提案者が同一モデルになる
    # （実測。規約 26 の「検証者とも別のモデル」が破れる）。
    served: list[str] = []
    for row in rows:
        for name in (row.get("model"), row.get("served_by")):
            if isinstance(name, str) and name and name not in served:
                served.append(name)
    return verification, list(dict.fromkeys(used + served)), None


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
    card = truncate(as_str(args.get("card")).strip(), 64)
    # card だけでも同じ台帳を継続できる（KANBAN のワーカーはカード ID しか持たない。実測: session_id を
    # 省略した 2 手目が新しい台帳になり、積んだ道筋が分断された）。明示の session_id が常に優先。
    resumed_by_card = False
    if not sid_in and card:
        sid_in = thought_find_by_card(card)
        resumed_by_card = bool(sid_in)
    stored = thought_get(sid_in) if sid_in else None
    if as_flag(args.get("view")):
        # カードから読み戻す（KANBAN のワーカーが自分のカードの思考を引く経路）。session_id が優先。
        if not sid_in and card:
            sid_in = thought_find_by_card(card)
            if not sid_in:
                return {"error": f"カード {card} に紐づく台帳はありません"
                                 "（同じ card を付けて思考を積むと紐づきます）",
                        "view": True, "card": card}
            stored = thought_get(sid_in)
        return _think_view(sid_in, stored, brief=as_flag(args.get("brief")))
    thought = as_str(args.get("thought"))
    if not thought:
        return {"error": "thought は必須です（空文字は不可。台帳を読むだけなら view=true と session_id）"}

    steps = [s for s in ((stored or {}).get("steps") or []) if isinstance(s, dict)]
    notes: list[str] = []
    if resumed_by_card:
        notes.append(f"card {card} に紐づく台帳を継続しました（session_id={sid_in}）。")
    if as_flag(args.get("brief")):
        notes.append("brief は view=true のときだけ効きます（思考の記録には影響しません）。")
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
    previous_used = list(used)
    previous_generation = (stored or {}).get("verifier_generation", 0)
    explicit_question = as_str(args.get("question"))
    question = explicit_question or (stored or {}).get("question") or ""
    # 検証者へ渡す台帳には、今回の呼び出しで決まる計画・分岐の決着を先に反映して見せる。
    preview = _think_preview_row(stored or {}, st)

    verification = None
    if as_flag(args.get("verify")):
        # 検証ブロックは `_think_verify_block` へ分けた（`tool_think` の巨大化対策。§7.2）。
        verification, used, failure = _think_verify_block(
            args, question, steps, step, preview, used, sid_in)
        if failure is not None:
            return failure     # 状態を書かない（環境障害で台帳を汚さない。規約 21）
        step["verification"] = verification

    alternatives = None
    if as_flag(args.get("propose_alternatives")):
        alternatives = _think_alternatives(args, question, steps, step, preview, exclude=used)
        if alternatives.get("error"):
            alternatives.update({"session_id": sid_in or None})
            return alternatives    # 状態を書かない（規約 21）
        step["alternatives"] = alternatives
        alt_served = [name for row in alternatives.get("answers", []) if isinstance(row, dict)
                      for name in (row.get("model"), row.get("served_by"))
                      if isinstance(name, str) and name]
        used = list(dict.fromkeys(used + list(alternatives.get("models") or []) + alt_served))

    # Merge and rotate against the latest history while holding the ledger lock.
    selected = verification is not None or alternatives is not None
    selected_models = []
    for result in (verification, alternatives):
        if result is not None:
            selected_models.extend(result.get("models") or [])
            selected_models.extend(name for answer in result.get("answers", []) if isinstance(answer, dict)
                                   for name in (answer.get("model"), answer.get("served_by"))
                                   if isinstance(name, str) and name)

    # 台帳への統合は**ロック内**で行う（番号の採番と、他ステップ・メタへの波及も含む。
    # 並列呼び出しで片方が消えるのを防ぐ）。検証プロンプトに載せた `#n` は採番前の見積りなので、
    # 並列時は 1 ずれることがある（返り値は統合後の確定番号を使う）。
    ops = {"revises": st["revises"], "tests_hypothesis": st["tests_hypothesis"],
           "hypothesis_status": st["hypothesis_status"],
           "resolve_branch": st["resolve_branch"], "branch_status": st["branch_status"],
           "plan": st["plan"], "subgoal_done": st["subgoal_done"],
           "total": total, "total_auto": total_auto}
    if st["subgoal"]:
        ops["subgoal_check"] = {"raw": st["subgoal_raw"],
                                "identity": _thought_subgoal_identity(preview, st["subgoal"])}
    merged = thought_merge(sid_in, step, question=explicit_question, verifier_models=used if selected else None, card=card,
                           assign_number=not explicit_number, ops=ops,
                           verifier_snapshot=previous_used if selected else None,
                           verifier_additions=selected_models if selected else None,
                           verifier_snapshot_generation=previous_generation if selected else None,
                           require_existing=stored is not None)
    if merged.get("error"):
        return merged
    if merged.get("refused"):
        return {"error": f"思考数が上限（{THOUGHT_MAX_STEPS}）に達しています。新しい session_id で"
                         "台帳を分けるか、FREEAGENT_THOUGHT_MAX_STEPS を上げてください",
                "session_id": merged["session_id"], "steps_recorded": merged["steps_recorded"]}
    if merged.get("verifier_omitted"):
        notes.append(f"検証者の古い履歴を{merged['verifier_omitted']}件省略しました。古いモデルは次回以降に再選択され得ます。")
    sid = merged["session_id"]
    step = merged["step"] or step
    n = as_int(step.get("n"), n, 1, 9999)
    if merged.get("replaced"):
        notes.append(f"思考 #{n} は既にありました。置き換えました（同じ番号の再送は上書き）。")
    after = thought_get(sid) or {}
    steps_out = [s for s in (after.get("steps") or []) if isinstance(s, dict)]
    used = [m for m in (after.get("verifier_models") or used) if isinstance(m, str)]

    # 「続けるか」。**明示された null は「未指定」扱い**（既定 True へ戻す）。
    # `as_flag(None)` は False になるので素朴に書くと、クライアントが null を送っただけで
    # 台帳が暗黙に終端する（実測: null → False / キー無し → True）。
    needed = _think_needed_flag(args)
    data = {
        "session_id": sid, "step": n, "text": step["text"], "kind": step.get("kind") or "step",
        "branch_id": step["branch_id"], "is_revision": step["is_revision"],
        "revises_thought": step["revises_thought"],
        "branch_from_thought": step["branch_from_thought"],
        "tests_hypothesis": step.get("tests_hypothesis"),
        "hypothesis_status": step.get("result_status") or step.get("hypothesis_status"),
        "subgoal": step.get("subgoal"),
        # 解決前（呼び出し側の指定）と解決後（plan 項目の id）の両方を出す。
        "subgoal_ref": st.get("subgoal_raw") or None,
        "total_thoughts": total or None,
        "total_auto_adjusted": total_auto,
        "next_thought_needed": needed,
        "ledger": _think_ledger_view(steps_out, row=after),
        "verification": verification,
        "verified": bool(verification and verification["answered"]),
        "alternatives": alternatives,
        "verifier_models": used,
        "notes": notes,
        # 終端（next_thought_needed=false）のときは**次の思考を誘導しない**。
        # 「続けるか」を False にしておきながら next_call を出すと、呼び出し側が
        # 勝手に次を積んでしまう（実測: 終端なのに「<次の思考>」が出ていた）。
        "privacy_note": "台帳は思考の記録です。判断と責任はメインLLMに残ります。",
    }
    if needed:
        data["next_call"] = {"tool": "freeagent_think",
                             "args": {"session_id": sid, "thought": "<次の思考>",
                                      "thought_number": n + 1, "verify": bool(verification),
                                      **({"total_thoughts": max(total, n + 1)} if total else {}),
                                      **({"card": card} if card else {})}}
    else:
        data["next_call"] = None
    if card:
        data["card"] = card          # KANBAN のカードへの紐づけ（引けたことを呼び出し側に見せる）
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
    out = _deep_copy({k: row.get(k) for k in ("plan", "plan_retired", "branch_meta")})
    if st.get("plan"):
        # 実際のマージと同じ id 割当てにする（検証者に別の plan を見せない）。
        old_plan = [p for p in (out.get("plan") or []) if isinstance(p, dict)]
        old_retired = [p for p in (out.get("plan_retired") or []) if isinstance(p, dict)]
        merged, retired = _plan_merge(old_plan, old_retired, list(st["plan"]))
        out["plan"] = merged
        out["plan_retired"] = retired
    meta = out.get("branch_meta") if isinstance(out.get("branch_meta"), dict) else {}
    if st.get("resolve_branch") and st["resolve_branch"] in meta:
        meta[st["resolve_branch"]]["status"] = st["branch_status"]
    out["branch_meta"] = meta
    return out


def _thought_subgoal_identity(preview: dict, subgoal: int):
    for key in ("plan", "plan_retired"):
        for item in preview.get(key) or []:
            if isinstance(item, dict) and as_int(item.get("id"), 0, 0, 9999) == subgoal:
                return (subgoal, as_str(item.get("text")), key)
    return None


def _thought_subgoal_still_matches(row: dict, steps: list[dict], step: dict, ops: dict) -> bool:
    check = ops.get("subgoal_check")
    if not check:
        return True
    problem, current = _think_structure({"plan": ops.get("plan"), "subgoal": check["raw"],
                                         "subgoal_done": ops.get("subgoal_done")},
                                        steps, as_int(step.get("n"), 0, 0, 9999), row)
    if problem:
        return False
    identity = _thought_subgoal_identity(_think_preview_row(row, current), current["subgoal"])
    return identity is not None and identity == check["identity"]


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

    # 分解: 計画（サブ目標）は再送で改訂できる。上限超過は切り捨てずエラー（規約 63 の例外ではなく
    # 「多すぎる入力をそのまま受け取らない」契約なので、件数を返すのではなく fail で止める）。
    # schema にも maxItems を宣言して、呼び出し側が事前に知れるようにする。
    plan = [truncate(p, 200) for p in as_str_list(args.get("plan"))]
    if len(plan) > THOUGHT_PLAN_MAX:
        return fail(f"plan は {THOUGHT_PLAN_MAX} 項目までです（{len(plan)} 項目）。粒度を上げて分けてください")
    old_plan = [p for p in (row.get("plan") or []) if isinstance(p, dict)]
    old_retired = [p for p in (row.get("plan_retired") or []) if isinstance(p, dict)]
    if plan:
        # 差し替え後の plan で id を振り直す（実際のマージと同じ割当て）。
        plan_now, _retired = _plan_merge(old_plan, old_retired, plan)
    else:
        plan_now = old_plan
    # `subgoal` の解決（§6.10）。**番号の解釈は 1 つだけ**にする:
    #   1) 現在の plan の**id**に一致 → その項目（同一性。差し替えをまたいでも不変）
    #   2) 現在の plan の**1 始まりの位置** → その項目（id が一致しない番号は位置として扱う）
    #   3) 廃止済み項目の id → その項目（記録は残す。達成済みの印は付けない）
    # どれにも当たらなければエラー（推測で繋がない）。
    # 旧実装は id だけで照合していたため、差し替えの後は位置と id がずれて
    # **位置 1 の項目ではなく廃止済み項目を完了扱い**にしていた（実測: 20 回反復で 20 回再現）。
    sub_raw = as_int(args.get("subgoal"), 0, 0, 999)
    sub = 0
    sub_kind = ""
    if sub_raw:
        live_ids = [as_int(p.get("id"), 0, 0, 9999) for p in plan_now]
        if sub_raw in live_ids:
            sub, sub_kind = sub_raw, "id"
        elif 1 <= sub_raw <= len(plan_now):
            sub = as_int(plan_now[sub_raw - 1].get("id"), 0, 0, 9999)
            sub_kind = "position"
        elif any(as_int(p.get("id"), 0, 0, 9999) == sub_raw for p in old_retired):
            sub, sub_kind = sub_raw, "retired"
        else:
            return fail(f"subgoal={sub_raw} は計画にありません（計画 {len(plan_now)} 項目。"
                        "先に plan で分解してください）")
        if sub_kind == "position":
            notes.append(f"subgoal={sub_raw} は plan の {sub_raw} 番目（id={sub}）として解決しました。")
        elif sub_kind == "retired":
            notes.append(f"subgoal={sub_raw} は計画から廃止済みの項目（id={sub}）です。"
                         "記録は残しますが、達成済みの印は付けません。")
    sub_done = as_flag(args.get("subgoal_done"))
    if sub_done and not sub:
        return fail("subgoal_done には subgoal（達成したサブ目標の番号）が必要です")

    return None, {"kind": kind, "is_revision": is_revision, "revises": revises,
                  "branch_id": bid, "branch_from": bfrom,
                  "resolve_branch": resolve, "branch_status": bstatus,
                  "tests_hypothesis": tests, "hypothesis_status": hstatus,
                  "plan": plan, "subgoal": sub, "subgoal_raw": sub_raw,
                  "subgoal_kind": sub_kind, "subgoal_done": sub_done, "notes": notes}


def _think_needed_flag(args: dict) -> bool:
    """「続けるか」を読む。**明示された null は未指定扱い**（既定 True）。

    `as_flag(None)` は False なので、素朴に `as_flag(args.get("next_thought_needed", True))` と書くと、
    クライアントが `null` を送っただけで台帳が暗黙に終端する（実測: null → False）。
    MCP クライアントは「引数を省略した」と「null を送った」を区別しないので、
    どちらも「未指定 → True」と解釈するのが安全側。
    `needs_more_thoughts` は `next_thought_needed` の別名（両方あれば前者を優先）。
    """
    for key in ("next_thought_needed", "needs_more_thoughts"):
        if key not in args:
            continue
        value = args.get(key)
        if value is None:
            continue          # null = 未指定 → 次の別名か既定へ
        return as_flag(value)
    return True


def _think_view(sid: str, stored: dict | None, brief: bool = False) -> dict:
    """台帳を**書かずに**読む（文脈圧縮・再起動の後に、積んだ思考へ戻るため）。

    `brief=True` は現行の道筋と未解決の項目だけを返す（大きな台帳を軽く読み戻すため）。
    """
    if not sid:
        return {"error": "view=true には session_id が必要です"}
    if stored is None:
        return {"error": f"セッション {truncate(sid, 40)} は見つかりません（期限切れ／未知）",
                "session_id": sid}
    steps = [s for s in (stored.get("steps") or []) if isinstance(s, dict)]
    last = max([as_int(s.get("n"), 0, 0, 9999) for s in steps] + [0])
    total = _think_latest_total(steps)
    card = as_str(stored.get("card"))
    data = {
        "session_id": sid, "view": True, "brief": bool(brief), "step": last or None,
        "question": stored.get("question") or "",
        "total_thoughts": total, "next_thought_needed": True,
        "ledger": _think_ledger_view(steps, keep=THOUGHT_MAX_STEPS, row=stored, brief=brief),
        "verification": None, "verified": False, "alternatives": None,
        "verifier_models": [m for m in (stored.get("verifier_models") or []) if isinstance(m, str)],
        "card": card,
        "notes": [],
        "next_call": {"tool": "freeagent_think",
                      "args": {"session_id": sid, "thought": "<次の思考>", "thought_number": last + 1,
                               **({"card": card} if card else {})}},
    }
    data["suggestions"] = _think_suggestions(data, steps, True)
    return data


def _think_alternatives(args: dict, question: str, steps: list[dict], step: dict, row: dict,
                        *, exclude: list[str]) -> dict:
    """生成者・検証者とは別のモデルに**代替案**を出させる。不通なら error を返す（呼び出し側は書かない）。"""
    user_exclude = as_str_list(args.get("exclude")) or []
    refs, info = select_models(as_int(args.get("size"), 2, 1, 4), None,
                               prefer=as_str_list(args.get("prefer")) or None,
                               exclude=user_exclude, identity_exclude=exclude)
    if not refs:
        return _no_models()
    prompt = _think_prompt(question, steps, step, row).rsplit("\n\n", 1)[0] + (
        "\n\n上の道筋とは異なる代替の仮説・解法・道筋を「代替: …」の形式で最大3行だけ挙げてください。")
    # 既定 400 のまま: 予算を増やすと 1 体あたりの待ち時間と利用枠の消費が増える。
    # （proxy 経由の 10 秒接続タイムアウトという旧制約は 2026-10-04 の接続/読み取り分離で解消した。）
    # 打ち切り（truncated）で文の途中で切れた最後の案は parse_alternatives が捨てる（実測「代替: 親プロセスのコマン」）。
    # avoid: フォールバックが同じ呼び出しの**検証者**に落ちると「検証者とも別のモデル」が破れる（実測）。
    results = ask_many(refs, prompt, system=THINK_ALT_SYSTEM,
                       max_tokens=as_int(args.get("max_tokens"), 400, 16, 2000), kind="think",
                       avoid=user_exclude, identity_avoid=exclude)
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
