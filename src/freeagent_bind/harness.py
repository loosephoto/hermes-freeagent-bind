"""ハーネス判別（Hermes 以外で起動されたときの警告）

`server.py` の § から機械的に切り出した（**共有ヘルパーは `S.<name>` の属性参照**で呼ぶ。
テストが `patch.object(server, ...)` で差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import os
import sys
import threading

from . import server as S  # noqa: E402

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
# HTTP では子プロセスの env が渡らないので、同じ目印をヘッダでも受ける（Hermes の mcp_servers は
# `headers:` を毎リクエスト送る）。stdio は従来どおり env の目印。
HARNESS_MARKER_HEADER = "x-freeagent-harness"
HARNESS_WARN_ENV = "FREEAGENT_HARNESS_WARN"
_SDK_DEFAULT_CLIENT = "mcp"
_LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")
_HARNESS_LOCK = threading.Lock()
_HARNESS: dict = {"info": None, "announced": False, "notified": False, "log_level": "info"}


def detect_harness(params, env=None, marker=None) -> dict:
    """initialize の params・目印（env か HTTP ヘッダ）からハーネスを判定する（文字列比較だけ・サブプロセスなし）。

    `marker` は HTTP のヘッダ（`HARNESS_MARKER_HEADER`）由来。HTTP では子プロセスの env が渡らないため、
    明示の目印はヘッダで受ける（stdio は env）。
    """
    env = os.environ if env is None else env
    params = params if isinstance(params, dict) else {}
    client = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
    name = client.get("name") if isinstance(client.get("name"), str) else ""
    version = client.get("version") if isinstance(client.get("version"), str) else ""
    from_header = (marker or "").strip()
    declared = from_header or (env.get(HARNESS_MARKER_ENV) or "").strip()
    info = {"client": name, "client_version": version, "marker": declared}
    if declared:
        kind = "hermes" if declared.lower() == "hermes" else "other"
        return {**info, "kind": kind, "source": "header" if from_header else "env",
                "reason": (f"{HARNESS_MARKER_HEADER}={declared}" if from_header
                           else f"{HARNESS_MARKER_ENV}={declared}")}
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
    return S.PROVIDER_SPECS["nous"]["base_url"].startswith("http://127.0.0.1:8645")


def harness_message(info: dict) -> str:
    """人間向けの 1 段落（stderr・content・ログ通知で共用）。LLM への指示は書かない（規約 3）。"""
    if not info or info.get("kind") == "hermes":
        return ""
    fix = (f"HTTP なら設定の headers に {HARNESS_MARKER_HEADER}: hermes、"
           f"stdio なら `hermes config set mcp_servers.freeagent-bind.env.{HARNESS_MARKER_ENV} hermes`")
    if info.get("kind") == "unknown":
        return (f"ハーネスを判別できません（{info.get('reason')}）。Hermes Agent で使っているなら "
                f"{fix} で目印を入れると判別できます。")
    parts = [f"Hermes Agent 以外のクライアント（{info.get('client') or info.get('marker') or '不明'}）で"
             "動作しています。"]
    if _harness_backend_is_default():
        parts.append("既定の推論先 hermes proxy（127.0.0.1:8645）が無いと nous プロバイダは使えません"
                     "（FREEAGENT_BASE_URL で変更可。nous 以外の " + str(len(S.PROVIDER_ORDER) - 1) +
                     " 社（OpenRouter / NVIDIA / Hugging Face ほか・一覧は freeagent_models）は"
                     "キーと必要な Free-tier 確認があれば使えます）。")
    parts.append("自発利用の設定（SOUL.md・tools.exclude・apply_proactive.py）は Hermes 専用です。")
    parts.append(f"この警告は {HARNESS_WARN_ENV}=0 で止められます。")
    return "".join(parts)


def harness_instructions(info: dict) -> str:
    """initialize.instructions の先頭に付ける注記（other のときだけ。他クライアントは instructions を読む）。"""
    if not info or info.get("kind") != "other" or not harness_warn_enabled():
        return S.PROACTIVE_INSTRUCTIONS
    note = ("【注意】このサーバーは Hermes Agent 向けです。推論の既定の接続先は hermes proxy で、"
            "つながらないときは freeagent_models で使えるプロバイダを確認してから使う。"
            "失敗応答の structuredContent.next_action に従い、同じ呼び出しを繰り返さない。")
    return note + S.PROACTIVE_INSTRUCTIONS


def harness_on_initialize(params, marker=None) -> dict:
    """initialize で呼ぶ。判定を保存し、stderr に 1 回だけ書く。initialize 応答（dict）を返す。

    `marker` は HTTP のヘッダ（`HARNESS_MARKER_HEADER`）由来の目印。HTTP では 1 プロセスを複数
    クライアントが共有するので、この判定はプロセス内で最後に initialize したクライアントのものになる
    （警告は 1 プロセス 1 回・最初のツール結果の注記も 1 回だけ、という従来の性質は変わらない）。
    """
    info = detect_harness(params, marker=marker)
    announce = False
    with _HARNESS_LOCK:
        # **鍵の欠落で落ちない**: 状態はテストや再初期化で消えることがある（実測: フルスイートで
        # `KeyError: 'announced'` になり、initialize が例外になった）。既定値を置いてから読む。
        S._HARNESS.setdefault("announced", False)
        S._HARNESS.setdefault("notified", False)
        S._HARNESS.setdefault("log_level", "info")
        S._HARNESS["info"] = info
        if not S._HARNESS["announced"] and info["kind"] != "hermes" and harness_warn_enabled():
            S._HARNESS["announced"] = announce = True
    if announce:
        try:
            sys.stderr.write(f"[freeagent-bind] {harness_message(info)}\n")
            sys.stderr.flush()
        except Exception:
            pass
    offered = params.get("protocolVersion", "") if isinstance(params, dict) else ""
    return {
        "protocolVersion": S.negotiate_protocol(offered),
        # logging: サーバーが notifications/message を送るなら宣言が MUST（MCP 2025-11-25 Logging）。
        # resources: 変化の遅い知識（使い分け・在庫・鍵の入れ方・返り値の形）を呼ばれたときだけ読ませる（§11）。
        "capabilities": {"tools": {"listChanged": False},
                         "resources": {"subscribe": False, "listChanged": False},
                         "completions": {},
                         "logging": {}},
        "serverInfo": {"name": S.SERVER_NAME, "version": S.SERVER_VERSION},
        "instructions": harness_instructions(info),
    }


def harness_set_level(params) -> dict | None:
    """logging/setLevel。不正な値は -32602（仕様どおり）。"""
    level = params.get("level") if isinstance(params, dict) else None
    if level not in _LOG_LEVELS:
        return {"code": -32602, "message": f"invalid log level: {level}"}
    with _HARNESS_LOCK:
        S._HARNESS["log_level"] = level
    return None


def harness_log_notification() -> dict | None:
    """notifications/initialized の後に 1 回だけ送るログ通知（other のときだけ）。送らないなら None。"""
    with _HARNESS_LOCK:
        info = S._HARNESS["info"]
        if (S._HARNESS["notified"] or not info or info.get("kind") != "other" or not harness_warn_enabled()
                or _LOG_LEVELS.index(S._HARNESS["log_level"]) > _LOG_LEVELS.index("warning")):
            return None
        S._HARNESS["notified"] = True
    return {"jsonrpc": "2.0", "method": "notifications/message",
            "params": {"level": "warning", "logger": "freeagent-bind.harness",
                       "data": {"harness": info["kind"], "client": info.get("client"),
                                "message": harness_message(info)}}}


def harness_first_call(data: dict) -> str:
    """最初のツール結果にだけ判定結果を載せる。content に前置する文字列（other のときだけ ⚠ 1 行）を返す。"""
    with _HARNESS_LOCK:
        info = S._HARNESS["info"]
        if not info or S._HARNESS.get("first_call_done"):
            return ""
        S._HARNESS["first_call_done"] = True
    if info["kind"] == "hermes":
        return ""
    data["harness"] = {k: info[k] for k in ("kind", "client", "source", "reason")}
    if info["kind"] == "other" and harness_warn_enabled():
        return f"⚠️ {harness_message(info)}\n"
    return ""


def harness_status() -> dict | None:
    """freeagent_models が常に返す判定結果（未判定＝initialize 前なら None）。"""
    with _HARNESS_LOCK:
        info = S._HARNESS["info"]
    if not info:
        return None
    return {**{k: info[k] for k in ("kind", "client", "client_version", "source", "reason")},
            "warn": harness_warn_enabled()}


def _harness_reset() -> None:
    """テスト用: 判定状態を初期化する。"""
    with _HARNESS_LOCK:
        S._HARNESS.clear()
        S._HARNESS.update({"info": None, "announced": False, "notified": False, "log_level": "info"})


EXPORTS = {name: globals()[name] for name in (
    'HARNESS_MARKER_ENV',
    'HARNESS_MARKER_HEADER',
    'HARNESS_WARN_ENV',
    '_HARNESS',
    '_HARNESS_LOCK',
    '_LOG_LEVELS',
    '_SDK_DEFAULT_CLIENT',
    '_harness_backend_is_default',
    '_harness_reset',
    'detect_harness',
    'harness_first_call',
    'harness_instructions',
    'harness_log_notification',
    'harness_message',
    'harness_on_initialize',
    'harness_set_level',
    'harness_status',
    'harness_warn_enabled',
)}
