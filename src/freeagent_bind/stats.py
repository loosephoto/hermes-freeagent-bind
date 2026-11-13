"""§2.2 品質統計（観測の記録・減衰・順位付け）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import copy
import math
import os
import re
import threading

from . import server as S  # noqa: E402

S._STATS: dict = {"models": {}}




_STATS_LOCK = threading.Lock()


_STATS_HALFLIFE_DAYS = max(0.5, S._env_float("FREEAGENT_STATS_HALFLIFE_DAYS", 14.0))


STATS_MAX_REFS = 400


STATS_STALE_DAYS = 60.0


_KIND_KEYS = ("n", "ok", "leak", "trunc", "empty", "over_budget")


def stats_path() -> str:
    return os.environ.get("FREEAGENT_STATS_PATH") or os.path.join(S.state_dir(), "model_stats.json")


# 鮮度（v0.50.0・issue #12）。**減衰の対象にしない**（`_decay` は `_KIND_KEYS` だけを掛ける）。
# 「最後にいつ答えたか」「最後にいつ 404/410 だったか」を持ち、既定モデルの選抜から
# 廃止済みを外すのに使う。件数の減衰とは別物（時刻を割り引くと意味が壊れる）。
_FRESH_KEYS = ("last_at", "last_ok_at", "last_alive_at", "last_gone_at", "last_rate_limited_at")


def _empty_kind() -> dict:
    row = {k: 0.0 for k in _KIND_KEYS}
    row["err"] = {}
    row["lat_ms"] = 0.0
    for key in _FRESH_KEYS:
        row[key] = 0.0
    row["gone_reason"] = ""
    return row


def _decay(row: dict, factor: float) -> None:
    for k in _KIND_KEYS:
        row[k] = S.as_float(row.get(k), 0.0) * factor
    row["lat_ms"] = S.as_float(row.get("lat_ms"), 0.0) * factor
    errors = row.get("err")
    if isinstance(errors, dict):
        row["err"] = {key: S.as_float(value, 0.0) * factor for key, value in errors.items()}


def _decay_model(entry: dict, now: float) -> None:
    """モデル別の観測を半減期で減衰し、古い履歴が品質順位を固定しないようにする。"""
    previous = S.as_float(entry.get("updated_at"), now)
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
             and now - S.as_float(row.get("updated_at"), now) <= STATS_STALE_DAYS * 86400}
    if len(alive) > STATS_MAX_REFS:
        ordered = sorted(alive, key=lambda ref: S.as_float(alive[ref].get("updated_at"), 0.0), reverse=True)
        alive = {ref: alive[ref] for ref in ordered[:STATS_MAX_REFS]}
    return alive


_STATS_WRITE_LOCK = threading.Lock()


S._STATS_MTIME: float | None = None


def _stats_read(path: str) -> dict:
    """`model_stats.json` を読む（壊れていれば空）。**ロックの内側で呼ぶ**。"""
    data = S._load_json(path, {})
    models = data.get("models") if isinstance(data, dict) else None
    return models if isinstance(models, dict) else {}


def _ensure_stats_loaded() -> None:
    if not S.STATS_ENABLED:
        return
    path = stats_path()
    mtime = S._file_mtime(path)
    # 読み込み済みでも、**ファイルが別プロセスに更新されていたら読み直す**。
    # 「起動時に 1 回だけ」だと、常駐 HTTP と stdio が同じディレクトリを共有する構成で
    # 片方の学習が永久に見えない（`S._STATS_MTIME` は自分が読んだ時刻だけを覚える）。
    if S._STATS_LOADED and (mtime is None or mtime == S._STATS_MTIME):
        return
    with _STATS_LOCK:
        if S._STATS_LOADED and (mtime is None or mtime == S._STATS_MTIME):
            return
        # **丸ごと置き換えない**。ディスクを土台にし、メモリ側の行は `updated_at` が新しいときだけ
        # 採用する（読むだけで学んだ行を消さない・テストが直接入れた行も消えない）。
        merged = _stats_read(path)
        for ref, row in S._STATS["models"].items():
            cur = merged.get(ref)
            if cur is None or S.as_float(row.get("updated_at"), 0.0) >= S.as_float(cur.get("updated_at"), 0.0):
                merged[ref] = row
        S._STATS["models"] = merged
        S._STATS_LOADED = True
        S._STATS_MTIME = mtime


def _stats_apply(models: dict, ref: str, kind: str, *, now: float, error: str = "",
                 leak: bool = False, trunc: bool = False, empty: bool = False,
                 over_budget: bool = False, lat_ms: float = 0.0) -> dict:
    """1 件の観測を `models` に足す（読み直した**最新の写し**に対して行う）。"""
    entry = models.setdefault(ref, {"kinds": {}, "updated_at": now})
    if not isinstance(entry.get("kinds"), dict):
        entry["kinds"] = {}
    _decay_model(entry, now)
    row = entry["kinds"].setdefault(kind, _empty_kind())
    row["n"] = S.as_float(row.get("n"), 0.0) + 1.0
    row["last_at"] = now
    if error:
        cls = classify_error(error)
        err = row.setdefault("err", {})
        err[cls] = S.as_float(err.get(cls), 0.0) + 1.0
        if cls == "gone":
            # **廃止の証拠を残す**（既定の選抜から外す判断に使う・v0.50.0）。
            row["last_gone_at"] = now
            row["gone_reason"] = S.truncate(error, 160)
        elif cls == "rate_limited":
            row["last_rate_limited_at"] = now
        elif cls == "no_output":
            # 応答は返っている（本文 0）＝**そのモデルは存在する**。廃止の判定は打ち消すが、
            # 成功としては数えない（`last_ok_at` は上げない）。
            row["last_alive_at"] = now
    else:
        # 応答が返った＝そのモデルは存在する。
        row["last_ok_at"] = now
        row["last_alive_at"] = now
        if leak:
            row["leak"] = S.as_float(row.get("leak"), 0.0) + 1.0
        elif trunc:
            row["trunc"] = S.as_float(row.get("trunc"), 0.0) + 1.0
        elif empty:
            row["empty"] = S.as_float(row.get("empty"), 0.0) + 1.0
        else:
            row["ok"] = S.as_float(row.get("ok"), 0.0) + 1.0
    if over_budget:
        # 成功・失敗とは独立に数える（空応答でも成功でも、予算を超えた事実は残す）
        row["over_budget"] = S.as_float(row.get("over_budget"), 0.0) + 1.0
    if lat_ms:
        row["lat_ms"] = S.as_float(row.get("lat_ms"), 0.0) + lat_ms
    return models


def _stats_save() -> bool:
    """いまのメモリ上の統計を書く（`note_observation` は差分を足す経路を使う）。"""
    if not S.STATS_ENABLED:
        return False
    now = S.now_ts()
    path = stats_path()
    with _STATS_WRITE_LOCK, S._file_lock(path):
        with _STATS_LOCK:
            models = _stats_prune(copy.deepcopy(S._STATS["models"]), now)
        payload = {"version": 1, "models": models}
        return S._atomic_write(path, S._dumps(payload))


GONE_TTL_S = max(0.0, S._env_float("FREEAGENT_GONE_TTL_H", 24.0) * 3600.0)


def _kind_rows(ref: str) -> dict:
    _ensure_stats_loaded()
    entry = S._STATS["models"].get(ref)
    kinds = entry.get("kinds") if isinstance(entry, dict) else None
    return kinds if isinstance(kinds, dict) else {}


def model_freshness(ref: str) -> dict:
    """`ref` の鮮度（最後に答えた時刻・最後に廃止だった時刻・その理由）を返す。

    直近の `gone`（404/410）が**最後の応答より新しく**、かつ `FREEAGENT_GONE_TTL_H`（既定 24 時間）
    以内なら `gone: true`。TTL を過ぎたら再確認に戻す（NVIDIA の「アカウントで提供終了」のような
    提供元都合の廃止は復活しうるので、永久に除外しない）。値が無ければ空の辞書。
    """
    last_ok = last_alive = last_gone = last_limited = last_probe = 0.0
    reason = ""
    for kind_name, row in _kind_rows(ref).items():
        if not isinstance(row, dict):
            continue
        last_ok = max(last_ok, S.as_float(row.get("last_ok_at"), 0.0))
        last_alive = max(last_alive, S.as_float(row.get("last_alive_at"), 0.0))
        gone_at = S.as_float(row.get("last_gone_at"), 0.0)
        if gone_at > last_gone:
            last_gone = gone_at
            reason = S.as_str(row.get("gone_reason"))
        last_limited = max(last_limited, S.as_float(row.get("last_rate_limited_at"), 0.0))
        if kind_name == "probe":
            last_probe = max(last_probe, S.as_float(row.get("last_at"), 0.0))
    if not any((last_ok, last_gone, last_limited, last_probe)):
        return {}
    now = S.now_ts()
    # 「最後に応答があった時刻」より新しい廃止だけを gone とする（空応答でも復活は拾う）。
    gone = bool(last_gone) and last_gone > max(last_ok, last_alive) and (now - last_gone) <= GONE_TTL_S
    out: dict = {"gone": gone}
    if last_ok:
        out["last_ok_at"] = round(last_ok, 3)
        out["last_ok_age_s"] = round(max(0.0, now - last_ok), 1)
    if last_gone:
        out["last_gone_at"] = round(last_gone, 3)
        out["gone_age_s"] = round(max(0.0, now - last_gone), 1)
        out["gone_reason"] = S.truncate(reason, 160)
        out["gone_expires_in_s"] = round(max(0.0, GONE_TTL_S - (now - last_gone)), 1)
    if last_limited:
        out["last_rate_limited_at"] = round(last_limited, 3)
    if last_probe:
        out["probed_at"] = round(last_probe, 3)
    return out


def is_gone(ref: str) -> bool:
    """`ref` が直近の 404/410 で廃止と判定されているか（TTL 内・明示指定は妨げない）。"""
    return bool(model_freshness(ref).get("gone"))


def gone_refs() -> dict[str, dict]:
    """いま廃止と判定されている ref → 鮮度辞書（選抜の除外と一覧の可視化に使う）。"""
    _ensure_stats_loaded()
    out: dict[str, dict] = {}
    for ref in list(S._STATS["models"]):
        info = model_freshness(ref)
        if info.get("gone"):
            out[ref] = info
    return out


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
    if not S.STATS_ENABLED:
        return
    now = S.now_ts()
    path = stats_path()
    # **プロセス間で read-modify-write を直列化する**（v0.25.0）。メモリ上の写しに足して
    # 丸ごと上書きすると、同じ `FREEAGENT_STATE_DIR` を共有する 2 つのプロセス
    # （常駐 HTTP と stdio）で **後勝ちの欠落**が出る（実測: 2 プロセス × 50 件 = 期待 100 件に対し
    # 旧実装は **50 件**。ほぼ片方の記録が丸ごと消える）。ロックの内側でディスクの最新を読み、
    # そこへ 1 件だけ足す。
    with S._file_lock(path):
        models = _stats_apply(_stats_read(path), ref, kind, now=now, error=error,
                              leak=leak, trunc=trunc, empty=empty, over_budget=over_budget,
                              lat_ms=lat_ms)
        models = _stats_prune(models, now)
        S._atomic_write(path, S._dumps({"version": 1, "models": models}))
        with _STATS_LOCK:
            S._STATS["models"] = models
            S._STATS_LOADED = True
            S._STATS_MTIME = S._file_mtime(path)


def _kind_quality(row: dict) -> tuple[float, float]:
    """(品質 0〜1, 観測数) を返す。切断・CoT混入・空を減点し、エラー率を掛ける。

    **空応答（`no_output`）はハード失敗の半分の重み**で減点する（v0.18.0・実測にもとづく）。
    思考トークンで予算を使い切っただけの失敗は、`max_tokens` を上げれば回復する種類であり、
    「廃止（404）」「権限（403）」「5xx」と同じ重みで沈めると、**大きい予算でなら正しく答える
    モデルが永久に選ばれなくなる**。かといって無視はしない（実測: 床 512・上限 2048 まで上げても
    空のままのモデルがいる＝その用途では実質使えない）。減点の重みが半分なだけで、
    成功が 1 件も無ければ品質は 0 のまま（選抜で上に来ることはない）。
    """
    n = S.as_float(row.get("n"), 0.0)
    if n <= 0:
        return 0.0, 0.0
    ok = S.as_float(row.get("ok"), 0.0)
    leak = S.as_float(row.get("leak"), 0.0)
    trunc = S.as_float(row.get("trunc"), 0.0)
    empty = S.as_float(row.get("empty"), 0.0)
    err = row.get("err") or {}
    if isinstance(err, dict):
        soft = S.as_float(err.get("no_output"), 0.0)
        hard = sum(S.as_float(v, 0.0) for k, v in err.items() if k != "no_output")
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
    entry = S._STATS["models"].get(ref) or {}
    kinds = entry.get("kinds") or {}
    if kind and kind in kinds:
        return _kind_quality(kinds[kind])[0]
    vals = [_kind_quality(row)[0] for row in kinds.values()]
    return round(sum(vals) / len(vals), 4) if vals else 0.0


def model_observations(ref: str) -> float:
    _ensure_stats_loaded()
    kinds = (S._STATS["models"].get(ref) or {}).get("kinds") or {}
    return sum(S.as_float(row.get("n"), 0.0) for row in kinds.values())


def model_status(ref: str) -> str:
    """ok / watch / degraded / unobserved。選択の説明に使う（数値を丸めずに渡す）。"""
    n = model_observations(ref)
    if n <= 0:
        return "unobserved"
    q = S.model_quality(ref)
    if q >= 0.6:
        return "ok"
    if q >= 0.3:
        return "watch"
    return "degraded"


def _rank_counts(ref: str) -> tuple[float, float]:
    """順位づけ用の (成功, 失敗) の減衰つき疑似カウント。

    **種別（kind）を観測数 n で重み付けして合算する**。`S.model_quality` は種別ごとの品質を単純平均するので、
    `ask` 1 件成功・`panel` 50 件失敗のモデルが「品質 0.5」に見える。順位はその歪みをそのまま使わない。
    """
    _ensure_stats_loaded()
    kinds = (S._STATS["models"].get(ref) or {}).get("kinds") or {}
    success = 0.0
    total = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = S.as_float(row.get("n"), 0.0)
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
    同点の内側だけを並べ替える。実測: 判断は `S._rank_score` の値そのまま）。
    """
    latency = S.model_mean_latency(ref)
    return latency if latency is not None else math.inf


RANK_KIND_BIAS = S._env_on("FREEAGENT_RANK_KIND_BIAS", False)


_KIND_BIAS_MIN_CALLS = 3


_KIND_BIAS_CAP = 0.30


def _kind_bias(ref: str, kind: str) -> float:
    """同じ kind の失敗率から引くペナルティ（0〜`_KIND_BIAS_CAP`。未観測は 0）。"""
    if not S.RANK_KIND_BIAS or not kind:
        return 0.0
    _ensure_stats_loaded()
    row = ((S._STATS["models"].get(ref) or {}).get("kinds") or {}).get(kind)
    if not isinstance(row, dict):
        return 0.0
    n = S.as_float(row.get("n"), 0.0)
    if n < _KIND_BIAS_MIN_CALLS:
        return 0.0
    err = row.get("err") or {}
    soft = S.as_float(err.get("no_output"), 0.0) if isinstance(err, dict) else 0.0
    hard = (sum(S.as_float(v, 0.0) for k, v in err.items() if k != "no_output")
            if isinstance(err, dict) else 0.0)
    return round(min(_KIND_BIAS_CAP, 0.3 * soft / n + 0.5 * hard / n), 4)




_RANK_BUDGET_CAP = 0.20


_RANK_BUDGET_MIN_CALLS = 2


def _budget_penalty(ref: str) -> float:
    """`max_tokens` を超えて返した割合ぶんのペナルティ（0〜`_RANK_BUDGET_CAP`）。"""
    if not S.RANK_BUDGET:
        return 0.0
    _ensure_stats_loaded()
    kinds = (S._STATS["models"].get(ref) or {}).get("kinds") or {}
    total = 0.0
    over = 0.0
    for row in kinds.values():
        if not isinstance(row, dict):
            continue
        n = S.as_float(row.get("n"), 0.0)
        if n <= 0:
            continue
        total += n
        over += S.as_float(row.get("over_budget"), 0.0)
    if total < _RANK_BUDGET_MIN_CALLS or over <= 0:
        return 0.0
    return round(min(_RANK_BUDGET_CAP, over / total), 4)


RANK_LATENCY_W = max(0.0, min(1.0, S._env_float("FREEAGENT_RANK_LATENCY", 0.0)))


RANK_LATENCY_FULL_S = max(1.0, S._env_float("FREEAGENT_RANK_LATENCY_FULL_S", 20.0))


def _latency_penalty(ref: str) -> float:
    """平均所要が `S.RANK_LATENCY_FULL_S` 秒に達したとき、品質点から `S.RANK_LATENCY_W` を引く。

    比例（頭打ちあり）にするのは、**極端に遅いモデル 1 体が順位を総取りしない**ため
    （20 秒を超えても引く量は増えない）。観測が無いモデルは 0（探索を妨げない）。
    """
    if S.RANK_LATENCY_W <= 0.0:
        return 0.0
    latency_ms = S.model_mean_latency(ref)
    if latency_ms is None:
        return 0.0
    share = min(1.0, (float(latency_ms) / 1000.0) / S.RANK_LATENCY_FULL_S)
    return S.RANK_LATENCY_W * share


def rank_models(refs: list[str]) -> list[str]:
    """品質順。観測が少ないモデルへ探索ボーナスを与え、新しい Free モデルが試されない問題を防ぐ。

    `FREEAGENT_RANK_LATENCY` が 0（既定）なら **v0.26.0 と同じ並び**（同点は平均所要の速い順）。
    """
    if not S.RANK_ENABLED:
        return list(refs)
    return sorted(refs, key=lambda r: (-(S._rank_score(r) - _latency_penalty(r)
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


EXPORTS = {
    "GONE_TTL_S": GONE_TTL_S,
    "_FRESH_KEYS": _FRESH_KEYS,
    "model_freshness": model_freshness,
    "is_gone": is_gone,
    "gone_refs": gone_refs,
    "_STATS_LOCK": _STATS_LOCK,
    "_STATS_HALFLIFE_DAYS": _STATS_HALFLIFE_DAYS,
    "STATS_MAX_REFS": STATS_MAX_REFS,
    "STATS_STALE_DAYS": STATS_STALE_DAYS,
    "_KIND_KEYS": _KIND_KEYS,
    "stats_path": stats_path,
    "_empty_kind": _empty_kind,
    "_decay": _decay,
    "_decay_model": _decay_model,
    "_stats_prune": _stats_prune,
    "_STATS_WRITE_LOCK": _STATS_WRITE_LOCK,
    "_stats_read": _stats_read,
    "_ensure_stats_loaded": _ensure_stats_loaded,
    "_stats_apply": _stats_apply,
    "_stats_save": _stats_save,
    "classify_error": classify_error,
    "_ENV_FAILURE_SIGNS": _ENV_FAILURE_SIGNS,
    "is_env_failure": is_env_failure,
    "note_observation": note_observation,
    "_kind_quality": _kind_quality,
    "model_quality": model_quality,
    "model_observations": model_observations,
    "model_status": model_status,
    "_rank_counts": _rank_counts,
    "_RANK_EXPLORE": _RANK_EXPLORE,
    "_rank_score": _rank_score,
    "_rank_latency_key": _rank_latency_key,
    "RANK_KIND_BIAS": RANK_KIND_BIAS,
    "_KIND_BIAS_MIN_CALLS": _KIND_BIAS_MIN_CALLS,
    "_KIND_BIAS_CAP": _KIND_BIAS_CAP,
    "_kind_bias": _kind_bias,
    "_RANK_BUDGET_CAP": _RANK_BUDGET_CAP,
    "_RANK_BUDGET_MIN_CALLS": _RANK_BUDGET_MIN_CALLS,
    "_budget_penalty": _budget_penalty,
    "RANK_LATENCY_W": RANK_LATENCY_W,
    "RANK_LATENCY_FULL_S": RANK_LATENCY_FULL_S,
    "_latency_penalty": _latency_penalty,
    "rank_models": rank_models,
    "diverse_order": diverse_order,
}
