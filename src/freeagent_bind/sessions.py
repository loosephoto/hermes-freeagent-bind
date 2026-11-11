"""§2.3.1/§2.4 呼び出し ID・トレース記録・相談セッション

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import contextlib
import os
import threading
import uuid

from . import server as S  # noqa: E402

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
    return os.environ.get("FREEAGENT_TRACE_PATH") or os.path.join(S.state_dir(), "traces.jsonl")


_TRACE_MAX_BYTES = S._env_int("FREEAGENT_TRACE_MAX_BYTES", 5_000_000)


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
    if not S.TRACE_ENABLED:
        return
    row = {"run": run or current_run(), "tool": tool or current_tool(),
           "kind": kind, "ref": ref, "ok": bool(ok), "error": error or None,
           "error_class": S.classify_error(error) if error else None,
           "served_by": served_by or ref, "rate_limited": rate_limited,
           "unavailable": unavailable, "skipped_cooling": skipped_cooling,
           "answer_len": int(answer_len or 0), "answer_sha1": answer_sha1 or "",
           "truncated": bool(truncated), "latency_s": round(S.as_float(latency_s, 0.0), 3),
           "tokens": tokens or {}, "finish_reason": finish_reason or None,
           "max_tokens": max_tokens, "retries": int(retries or 0), "ts": S.now_ts()}
    path = S.trace_path()
    try:
        with S._STATE_LOCK, S._file_lock(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                if os.path.getsize(path) > _TRACE_MAX_BYTES:
                    os.replace(path, path + ".1")
            except OSError:
                pass
            with open(path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(S._dumps(row) + "\n")
    except OSError:
        pass


def observe_call(result: dict, kind: str, text: str = "") -> None:
    """呼び出し結果を統計とトレースへ流す（1 か所に集約し、記録漏れを防ぐ）。"""
    ref = S.as_str(result.get("ref")) or S.as_str(result.get("model"))
    if not ref:
        return
    error = S.as_str(result.get("error"))
    if S.is_env_failure(error):
        # **環境障害（プロキシ停止・DNS 不達・TCP 拒否）は状態を一切書かない。** モデルの成績ではないので
        # 統計に入れると復旧後も選抜が歪み、トレースにも意味のある情報が無い（切り分けは
        # FREEAGENT_DEBUG_LOG で足りる）。ここを no-op にすることで「不通でも副作用ゼロ」を
        # `scripts/check_offline.py` が検証できる（状態ディレクトリにファイルが増えないこと）。
        S._debug("env_failure", {"ref": ref, "kind": kind, "error": error[:160]})
        return
    S.note_observation(ref, kind,
                     error=error,
                     leak=bool(result.get("cot_leak")),
                     trunc=bool(result.get("truncated")),
                     empty=not (text or "").strip() and not error,
                     over_budget=S._is_over_budget(result),
                     lat_ms=S.as_float(result.get("latency_s"), 0.0) * 1000.0)
    trace_call(kind, ref, not error, error=error,
               served_by=S.as_str(result.get("served_by")) or ref,
               rate_limited=result.get("rate_limited"),
               unavailable=result.get("unavailable"),
               skipped_cooling=result.get("skipped_cooling"),
               answer_len=len(text or ""), answer_sha1=S.sha1_12(text or ""),
               truncated=bool(result.get("truncated")),
               latency_s=S.as_float(result.get("latency_s"), 0.0),
               tokens=result.get("tokens") or {},
               finish_reason=S.as_str(result.get("finish_reason")),
               max_tokens=S.as_int(result.get("max_tokens"), 0, 0, 10 ** 9) or None,
               retries=S.as_int(result.get("retries"), 0, 0, 100))


_SESSIONS: dict[str, dict] = {}




_SESSIONS_MTIME: float | None = None


_SESSIONS_LOCK = threading.Lock()


SESSION_TTL_S = S._env_float("FREEAGENT_SESSION_TTL", 3600.0)


SESSION_MAX = S._env_int("FREEAGENT_SESSION_MAX", 32)


def sessions_path() -> str:
    return os.environ.get("FREEAGENT_SESSIONS_PATH") or os.path.join(S.state_dir(), "sessions.json")


def _sessions_prune(data: dict, now: float) -> dict:
    alive = {k: v for k, v in data.items()
             if isinstance(v, dict) and S.as_float(v.get("updated_at"), 0.0) > now - SESSION_TTL_S}
    if len(alive) > SESSION_MAX:  # 新しい順に上限まで残す
        keep = sorted(alive, key=lambda k: S.as_float(alive[k].get("updated_at"), 0.0), reverse=True)
        alive = {k: alive[k] for k in keep[:SESSION_MAX]}
    return alive


def _ensure_sessions_loaded() -> None:
    global _SESSIONS_MTIME
    if not S.SESSIONS_ENABLED:
        return
    path = sessions_path()
    mtime = S._file_mtime(path)
    if S._SESSIONS_LOADED:
        if mtime is None:
            return
        if _SESSIONS_MTIME is not None and abs(mtime - _SESSIONS_MTIME) < 1e-6:
            return
        with _SESSIONS_LOCK:
            if _SESSIONS_MTIME is not None and abs(mtime - _SESSIONS_MTIME) < 1e-6:
                return
            data = S._load_json(path, {})
            rows = data.get("sessions") if isinstance(data, dict) else None
            rows = rows if isinstance(rows, dict) else {}
            for sid, row in rows.items():
                if not isinstance(row, dict):
                    continue
                cur = _SESSIONS.get(sid)
                if cur is None or S.as_float(row.get("updated_at"), 0.0) >= S.as_float(cur.get("updated_at"), 0.0):
                    _SESSIONS[sid] = row
            S._SESSIONS_LOADED = True
            _SESSIONS_MTIME = mtime
        return
    with _SESSIONS_LOCK:
        if S._SESSIONS_LOADED:
            return
        data = S._load_json(path, {})
        rows = data.get("sessions") if isinstance(data, dict) else None
        _SESSIONS.update(_sessions_prune(rows if isinstance(rows, dict) else {}, S.now_ts()))
        S._SESSIONS_LOADED = True
        _SESSIONS_MTIME = mtime if mtime is not None else _SESSIONS_MTIME


def _sessions_save() -> bool:
    global _SESSIONS_MTIME
    if not S.SESSIONS_ENABLED:
        return False
    path = sessions_path()
    with S._file_lock(path):
        data = S._load_json(path, {})
        disk_rows = data.get("sessions") if isinstance(data, dict) else None
        disk_rows = disk_rows if isinstance(disk_rows, dict) else {}
        merged = dict(disk_rows)
        with _SESSIONS_LOCK:
            for sid, row in _SESSIONS.items():
                cur = merged.get(sid)
                if cur is None or (isinstance(row, dict) and S.as_float(row.get("updated_at"), 0.0) >= S.as_float((cur or {}).get("updated_at"), 0.0)):
                    merged[sid] = row
            merged = _sessions_prune(merged, S.now_ts())
            payload = {"version": 1, "saved_at": S.now_ts(), "sessions": merged}
            ok = S._atomic_write(path, S._dumps(payload))
            if ok:
                _SESSIONS.clear()
                _SESSIONS.update(merged)
                S._SESSIONS_LOADED = True
                _SESSIONS_MTIME = S._file_mtime(path)
        return ok


def new_session_id() -> str:
    return f"s{int(S.now_ts()) % 100000000:08d}{os.getpid() % 1000:03d}{os.urandom(8).hex()}"


def session_get(sid: str) -> dict | None:
    if not sid:
        return None
    _ensure_sessions_loaded()
    now = S.now_ts()
    with _SESSIONS_LOCK:
        row = _SESSIONS.get(sid)
        if not row or S.as_float(row.get("updated_at"), 0.0) <= now - SESSION_TTL_S:
            _SESSIONS.pop(sid, None)
            return None
        return S._deep_copy(row)  # 呼び出し側の書き換えが他へ波及しないよう複製を渡す


def session_put(sid: str, row: dict) -> None:
    if not S.SESSIONS_ENABLED or not sid:
        return
    _ensure_sessions_loaded()
    row = dict(row or {})
    row["updated_at"] = S.now_ts()
    with _SESSIONS_LOCK:
        _SESSIONS[sid] = row
    _sessions_save()


def session_drop(sid: str) -> None:
    if not sid or not S.SESSIONS_ENABLED:
        return
    _ensure_sessions_loaded()
    path = sessions_path()
    with S._file_lock(path):
        with _SESSIONS_LOCK:
            _SESSIONS.pop(sid, None)
            data = S._load_json(path, {})
            disk_rows = data.get("sessions") if isinstance(data, dict) else None
            disk_rows = disk_rows if isinstance(disk_rows, dict) else {}
            merged = dict(disk_rows)
            merged.pop(sid, None)
            merged = _sessions_prune(merged, S.now_ts())
            payload = {"version": 1, "saved_at": S.now_ts(), "sessions": merged}
            ok = S._atomic_write(path, S._dumps(payload))
            if ok:
                _SESSIONS.clear()
                _SESSIONS.update(merged)
                S._SESSIONS_LOADED = True
                global _SESSIONS_MTIME
                _SESSIONS_MTIME = S._file_mtime(path)


EXPORTS = {
    "_RUN_LOCAL": _RUN_LOCAL,
    "current_run": current_run,
    "current_tool": current_tool,
    "run_scope": run_scope,
    "new_run_id": new_run_id,
    "trace_path": trace_path,
    "_TRACE_MAX_BYTES": _TRACE_MAX_BYTES,
    "trace_call": trace_call,
    "observe_call": observe_call,
    "_SESSIONS": _SESSIONS,
    "_SESSIONS_MTIME": _SESSIONS_MTIME,
    "_SESSIONS_LOCK": _SESSIONS_LOCK,
    "SESSION_TTL_S": SESSION_TTL_S,
    "SESSION_MAX": SESSION_MAX,
    "sessions_path": sessions_path,
    "_sessions_prune": _sessions_prune,
    "_ensure_sessions_loaded": _ensure_sessions_loaded,
    "_sessions_save": _sessions_save,
    "new_session_id": new_session_id,
    "session_get": session_get,
    "session_put": session_put,
    "session_drop": session_drop,
}
