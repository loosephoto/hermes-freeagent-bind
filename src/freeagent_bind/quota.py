"""§3.4/§3.5 提供元ゲートとクォータ窓（予約・精算・報告）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import copy
import datetime
import json
import math
import os
import threading
import time

from . import server as S  # noqa: E402

class ProviderBusyError(Exception):
    """提供元の同時実行枠が取れなかった（＝429 を踏む代わりに待って諦めた）。"""


_PROVIDER_LIMIT_DEFAULT = max(1, min(16, S._env_int("FREEAGENT_PROVIDER_LIMIT", 4)))


_PROVIDER_LIMIT_MEASURED = {"nous": 6}


GATE_WAIT_S = max(1.0, min(120.0, S._env_float("FREEAGENT_GATE_WAIT_S", 20.0)))


_GATE_RESTORE_AFTER = max(1, S._env_int("FREEAGENT_GATE_RESTORE_AFTER", 3))


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
                                          set(S.PROVIDER_SPECS))


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
        name = S.as_str(item)
        if not name:
            continue
        head, sep, _ = name.partition("/")
        if sep and head in S.PROVIDER_SPECS:
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
            return self._limit.get(provider) or S.provider_slot_cap(provider)

    def acquire(self, provider: str, timeout: float | None = None) -> float | None:
        """枠を取る。取れたら**待ち秒**（待たなければ 0.0）、取れなければ None を返す。"""
        budget = S.GATE_WAIT_S if timeout is None else max(0.0, float(timeout))
        started = S.time.monotonic()
        end = started + budget
        with self._cond:
            cap = S.provider_slot_cap(provider)
            while self._in_flight.get(provider, 0) >= (self._limit.get(provider) or cap):
                remain = end - S.time.monotonic()
                if remain <= 0:
                    self._timeouts[provider] = self._timeouts.get(provider, 0) + 1
                    return None
                self._cond.wait(min(remain, 0.25))
            self._in_flight[provider] = self._in_flight.get(provider, 0) + 1
        waited = S.time.monotonic() - started
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
            cap = S.provider_slot_cap(provider)
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
            cap = S.provider_slot_cap(provider)
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
            names = _gate_provider_names(providers if providers is not None else list(S.PROVIDER_ORDER))
            per = {}
            for name in names:
                cap = S.provider_slot_cap(name)
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


_QUOTA_LIMITS = _parse_quota_limits(os.environ.get("FREEAGENT_QUOTA_LIMITS", ""), set(S.PROVIDER_SPECS))


QUOTA_ENABLED = S._env_on("FREEAGENT_QUOTA", True) and bool(_QUOTA_LIMITS)


_QUOTA: dict[str, dict] = {}


_QUOTA_LOCK = threading.Lock()


_QUOTA_MINUTE_S = 60.0


def quota_path() -> str:
    return os.environ.get("FREEAGENT_QUOTA_PATH") or os.path.join(S.state_dir(), "quota.json")


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
        start = S.as_float(row.get("min_start"), 0.0)
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
    if not S.QUOTA_ENABLED:
        return
    path = quota_path()
    mtime = S._file_mtime(path)
    # **別プロセスの使用量も見る**（v0.25.0）。1 回だけ読むと、常駐 HTTP と stdio が同じ上限を
    # 別々に数えて **合算で上限を超える**（プロセス数だけ枠を食い潰す）。
    if S._QUOTA_LOADED and (mtime is None or mtime == S._QUOTA_MTIME):
        return
    with _QUOTA_LOCK:
        if S._QUOTA_LOADED and (mtime is None or mtime == S._QUOTA_MTIME):
            return
        _QUOTA.update(_quota_read(path))
        _QUOTA_LOADED = True
        _QUOTA_MTIME = mtime


def _quota_write(rows: dict) -> bool:
    """ロックの内側から呼ぶ（`providers` 全体を書く）。"""
    ok = S._atomic_write(quota_path(), S._dumps({"version": 1, "providers": rows}))
    _QUOTA_MTIME = S._file_mtime(quota_path())
    return ok


def _quota_save() -> bool:
    if not S.QUOTA_ENABLED:
        return False
    with S._file_lock(quota_path()):
        with _QUOTA_LOCK:
            payload = copy.deepcopy(_QUOTA)
        return S._quota_write(payload)


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
    limits = S._QUOTA_LIMITS.get(provider) if S.QUOTA_ENABLED else None
    if not limits:
        return None
    _ensure_quota_loaded()
    with _QUOTA_LOCK:
        used = _quota_used(_quota_row(provider, S.now_ts()))
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
    limits = S._QUOTA_LIMITS.get(provider) if S.QUOTA_ENABLED else None
    if not limits:
        return None
    # **プロセス間でも check-then-act を 1 つのクリティカルセクションにする**（v0.25.0）。
    # ロックの内側でディスクの最新を取り込み、判定と加算を同じ写しに対して行う。
    # プロセス内の `threading.Lock` だけでは、常駐 HTTP と stdio が同じ上限を別々に数える。
    path = quota_path()
    with S._file_lock(path) as locked:
        if not locked:
            return f"提供元 {provider} のクォータのロックを取得できませんでした"
        with _QUOTA_LOCK:
            _QUOTA.clear()
            try:
                _QUOTA.update(_quota_read(path))
                row = _quota_row(provider, S.now_ts())
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
                row.setdefault("pending", {})[reservation_id] = {"tokens": tokens, "at": S.now_ts()}
            row["minute_events"].append([S.now_ts(), 1, 0])
            row["min_req"] = used["rpm"] + 1
            row["day_req"] = used["rpd"] + 1
            payload = copy.deepcopy(_QUOTA)
            if not S._quota_write(payload):
                _QUOTA.clear()
                _QUOTA.update(_quota_read(path))
                return f"提供元 {provider} のクォータの保存に失敗しました"
    return None


def quota_add_tokens(provider: str, tokens: int = 0, reservation_id: str = "") -> None:
    """消費したトークンを足す（リクエスト数は `quota_reserve` で確保済み）。"""
    if tokens <= 0 or not S.QUOTA_ENABLED or not S._QUOTA_LIMITS.get(provider):
        return
    path = quota_path()
    with S._file_lock(path) as locked:
        if not locked:
            raise QuotaExhaustedError(f"提供元 {provider} のクォータ精算ロックを取得できませんでした")
        with _QUOTA_LOCK:
            _QUOTA.clear()
            _QUOTA.update(_quota_read(path))
            row = _quota_row(provider, S.now_ts())
            if reservation_id:
                (row.get("pending") or {}).pop(reservation_id, None)
            row["minute_events"].append([S.now_ts(), 0, int(tokens)])
            row["min_tok"] = int(row.get("min_tok") or 0) + int(tokens)
            row["day_tok"] = int(row.get("day_tok") or 0) + int(tokens)
            payload = copy.deepcopy(_QUOTA)
            if not S._quota_write(payload):
                _QUOTA.clear()
                _QUOTA.update(_quota_read(path))
                raise QuotaExhaustedError(f"提供元 {provider} のクォータ精算の保存に失敗しました")


def quota_record(provider: str, tokens: int = 0) -> None:
    """1 回分（リクエスト＋トークン）を記録する。上限なら何もせず False 相当を返す。"""
    if quota_reserve(provider) is not None:
        return
    S.quota_add_tokens(provider, tokens)


def quota_report(providers: list[str] | None = None) -> dict | None:
    """今の使用量（宣言がある提供元だけ）。宣言が無ければ None。"""
    if not S.QUOTA_ENABLED:
        return None
    names = [n for n in _gate_provider_names(providers if providers is not None else list(S._QUOTA_LIMITS))
             if S._QUOTA_LIMITS.get(n)]
    if not names:
        return None
    _ensure_quota_loaded()
    now = S.now_ts()
    out: dict[str, dict] = {}
    with _QUOTA_LOCK:
        for name in names:
            row = _quota_row(name, now)
            used = _quota_used(row)
            limits = S._QUOTA_LIMITS.get(name) or {}
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
        return {"quota": S.quota_report(refs)}
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
    """提供元の枠（§3.4・クォータ §3.5）を取って `S._call_once` する。`(結果, 待ち秒)` を返す。

    枠が取れないときは **429 を踏む代わりに待って諦める**（`ProviderBusyError`）。
    宣言されたクォータを使い切っていれば `QuotaExhaustedError`（待たずに別の提供元へ回す）。
    `wait_budget` は 1 つの `call_model` が候補を回るあいだに使ってよい待ち時間の残り。
    """
    waited = S.PROVIDER_GATE.acquire(provider, timeout=wait_budget)
    if waited is None:
        raise ProviderBusyError(
            f"提供元 {provider} の同時実行枠（{S.PROVIDER_GATE.limit(provider)} 本）が埋まったままで、"
            f"{S.GATE_WAIT_S:g} 秒待っても取れませんでした。size を減らす／`FREEAGENT_PROVIDER_LIMITS` で"
            "枠を上げる／別の提供元を有効にする、のいずれかで解消します")
    try:
        # 送る**直前**に 1 回分を確保する（並列 N 本が同じ残量を読んで全員通るのを防ぐ・§3.5）
        limits = S._QUOTA_LIMITS.get(provider, {}) if S.QUOTA_ENABLED else {}
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
        result = S._call_once(provider, model, prompt, system, effective_max, temperature,
                            timeout=timeout)
    finally:
        S.PROVIDER_GATE.release(provider)
    if effective_max != max_tokens:
        result["quota_accounting"] = f"max_tokens_capped: {max_tokens} -> {effective_max}"
    actual_tokens = _result_tokens(result)
    try:
        S.quota_add_tokens(provider, actual_tokens, reservation_id)
    except Exception as exc:
        warning = f"settlement_failed: {exc}"
        result["quota_accounting"] = "; ".join(filter(None, (result.get("quota_accounting"), warning)))
        return result, waited
    if reservation_id and actual_tokens <= 0:
        result["quota_accounting"] = "; ".join(filter(None, (result.get("quota_accounting"),
                                                               "usage_unknown_reservation_retained")))
    return result, waited


EXPORTS = {
    "ProviderBusyError": ProviderBusyError,
    "_PROVIDER_LIMIT_DEFAULT": _PROVIDER_LIMIT_DEFAULT,
    "_PROVIDER_LIMIT_MEASURED": _PROVIDER_LIMIT_MEASURED,
    "GATE_WAIT_S": GATE_WAIT_S,
    "_GATE_RESTORE_AFTER": _GATE_RESTORE_AFTER,
    "_GATE_COUNT_WAIT_S": _GATE_COUNT_WAIT_S,
    "_parse_provider_limits": _parse_provider_limits,
    "_PROVIDER_LIMITS": _PROVIDER_LIMITS,
    "provider_slot_cap": provider_slot_cap,
    "_gate_provider_names": _gate_provider_names,
    "_ProviderGate": _ProviderGate,
    "PROVIDER_GATE": PROVIDER_GATE,
    "_QUOTA_UNITS": _QUOTA_UNITS,
    "QuotaExhaustedError": QuotaExhaustedError,
    "_parse_quota_limits": _parse_quota_limits,
    "_QUOTA_LIMITS": _QUOTA_LIMITS,
    "QUOTA_ENABLED": QUOTA_ENABLED,
    "_QUOTA": _QUOTA,
    "_QUOTA_LOCK": _QUOTA_LOCK,
    "_QUOTA_MINUTE_S": _QUOTA_MINUTE_S,
    "quota_path": quota_path,
    "_quota_row": _quota_row,
    "_quota_read": _quota_read,
    "_ensure_quota_loaded": _ensure_quota_loaded,
    "_quota_write": _quota_write,
    "_quota_save": _quota_save,
    "_quota_used": _quota_used,
    "_quota_reason": _quota_reason,
    "quota_block": quota_block,
    "quota_reserve": quota_reserve,
    "quota_add_tokens": quota_add_tokens,
    "quota_record": quota_record,
    "quota_report": quota_report,
    "quota_reset": quota_reset,
    "_quota_report_after_answers": _quota_report_after_answers,
    "_result_tokens": _result_tokens,
    "_gated_call_once": _gated_call_once,
}
