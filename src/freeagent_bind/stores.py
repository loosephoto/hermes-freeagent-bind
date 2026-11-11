"""§2.1/§2.5 クールダウンとプロバイダ認証の記憶

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import copy
import datetime
import email
import os
import threading

from . import server as S  # noqa: E402

_COOLDOWN: dict[str, dict] = {}






_COOLDOWN_LOCK = threading.Lock()


_COOLDOWN_DEFAULT_S = 60.0


_COOLDOWN_MAX_S = 900.0


_UNAVAILABLE_S = 3600.0


_COOLDOWN_LADDER_S = (60.0, 180.0, 600.0)


def cooldowns_path() -> str:
    return os.environ.get("FREEAGENT_COOLDOWN_PATH") or os.path.join(S.state_dir(), "cooldowns.json")


def _cooldown_prune(entries: dict, now: float) -> dict:
    return {ref: e for ref, e in entries.items()
            if isinstance(e, dict) and S.as_float(e.get("until"), 0.0) > now}


def _ensure_cooldown_loaded() -> None:
    if not S.COOLDOWN_ENABLED:
        return
    path = cooldowns_path()
    mtime = S._file_mtime(path)
    # **別プロセスが書いたクールダウンも見る**（v0.25.0）。1 回だけ読む実装だと、stdio と常駐 HTTP が
    # 同じディレクトリを共有しているとき、片方が学習した「Free 終了（404）」を知らないまま叩き続ける。
    if S._COOLDOWN_LOADED and (mtime is None or mtime == S._COOLDOWN_MTIME):
        return
    with _COOLDOWN_LOCK:
        if S._COOLDOWN_LOADED and (mtime is None or mtime == S._COOLDOWN_MTIME):
            return
        data = S._load_json(path, {})
        saved = data.get("cooldowns") if isinstance(data, dict) else None
        now = S.now_ts()
        if isinstance(saved, dict):
            for ref, entry in _cooldown_prune(saved, now).items():
                cur = _COOLDOWN.get(ref)
                # ディスク側が新しい期限を持つときだけ採用する（メモリの記憶を巻き戻さない）
                if not cur or S.as_float(entry.get("until"), 0.0) > S.as_float(cur.get("until"), 0.0):
                    _COOLDOWN[ref] = entry
        S._COOLDOWN_LOADED = True
        S._COOLDOWN_MTIME = mtime


def _cooldown_save() -> bool:
    """ディスクの内容と**併合してから**書く（他プロセスの期限を消さない）。"""
    if not S.COOLDOWN_ENABLED:
        return False
    now = S.now_ts()
    path = cooldowns_path()
    with S._file_lock(path):
        with _COOLDOWN_LOCK:
            merged = _cooldown_prune(copy.deepcopy(_COOLDOWN), now)
        disk = S._load_json(path, {})
        saved = disk.get("cooldowns") if isinstance(disk, dict) else None
        if isinstance(saved, dict):
            for ref, entry in _cooldown_prune(saved, now).items():
                cur = merged.get(ref)
                if not cur or S.as_float(entry.get("until"), 0.0) > S.as_float(cur.get("until"), 0.0):
                    merged[ref] = entry
        merged = _cooldown_prune(merged, now)
        ok = S._atomic_write(path, S._dumps({"version": 1, "saved_at": now, "cooldowns": merged}))
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
            delta = dt.timestamp() - (now if now is not None else S.now_ts())
            return min(_COOLDOWN_MAX_S, max(1.0, delta))
    except (TypeError, ValueError):
        pass
    return _COOLDOWN_DEFAULT_S


def note_cooldown(ref: str, secs: float, reason: str, strikes: int = 1) -> float:
    if not S.COOLDOWN_ENABLED:
        return 0.0
    _ensure_cooldown_loaded()
    until = S.now_ts() + max(1.0, secs)
    with _COOLDOWN_LOCK:
        cur = _COOLDOWN.get(ref)
        if cur and S.as_float(cur.get("until"), 0.0) > until:
            return S.as_float(cur.get("until"), 0.0)  # 既存の方が長いなら伸ばさない
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
        previous = int(S.as_float(entry.get("strikes"), 0.0))
        still_cooling = S.as_float(entry.get("until"), 0.0) > S.now_ts()
    strikes = previous + 1 if still_cooling else 1
    step = _COOLDOWN_LADDER_S[min(strikes - 1, len(_COOLDOWN_LADDER_S) - 1)]
    wait = _parse_retry_after(retry_after) if retry_after else step
    if strikes > 1:
        wait = max(wait, step)          # 連続で踏んだら短い Retry-After でも段を下回らない
    label = reason if strikes == 1 else f"{reason}（{strikes} 回目）"
    return S.note_cooldown(ref, min(wait, _COOLDOWN_MAX_S), label, strikes=strikes)


def note_unavailable(ref: str, status: int) -> float:
    """404/410 = モデル消滅・Free 提供終了。1 時間外す（復活していれば期限切れで戻る）。"""
    return S.note_cooldown(ref, _UNAVAILABLE_S, f"HTTP {status}（モデル消滅/Free終了）")


def cooling_refs() -> dict[str, dict]:
    _ensure_cooldown_loaded()
    now = S.now_ts()
    with _COOLDOWN_LOCK:
        return _cooldown_prune(_COOLDOWN, now)


def is_cooling(ref: str) -> bool:
    return ref in S.cooling_refs()


_AUTH: dict[str, dict] = {}




_AUTH_MTIME: float | None = None


_AUTH_LOCK = threading.Lock()


AUTH_TTL_S = S._env_float("FREEAGENT_AUTH_TTL", 900.0)


def auth_path() -> str:
    return os.environ.get("FREEAGENT_AUTH_PATH") or os.path.join(S.state_dir(), "provider_auth.json")


def _auth_prune(entries: dict, now: float) -> dict:
    return {p: e for p, e in entries.items()
            if isinstance(e, dict) and S.as_float(e.get("until"), 0.0) > now}


def _ensure_auth_loaded() -> None:
    global _AUTH_MTIME
    path = auth_path()
    mtime = S._file_mtime(path)
    if S._AUTH_LOADED:
        if mtime is None:
            return
        if _AUTH_MTIME is not None and abs(mtime - _AUTH_MTIME) < 1e-6:
            return
        with _AUTH_LOCK:
            if _AUTH_MTIME is not None and abs(mtime - _AUTH_MTIME) < 1e-6:
                return
            data = S._load_json(path, {})
            saved = data.get("providers") if isinstance(data, dict) else None
            if isinstance(saved, dict):
                for p, e in saved.items():
                    if not isinstance(e, dict):
                        continue
                    cur = _AUTH.get(p)
                    if cur is None or S.as_float(e.get("until"), 0.0) > S.as_float(cur.get("until"), 0.0):
                        _AUTH[p] = e
            S._AUTH_LOADED = True
            _AUTH_MTIME = mtime
        return
    with _AUTH_LOCK:
        if S._AUTH_LOADED:
            return
        data = S._load_json(path, {})
        saved = data.get("providers") if isinstance(data, dict) else None
        if isinstance(saved, dict):
            _AUTH.update(_auth_prune(saved, S.now_ts()))
        S._AUTH_LOADED = True
        _AUTH_MTIME = mtime if mtime is not None else _AUTH_MTIME


def _auth_save() -> bool:
    global _AUTH_MTIME
    path = auth_path()
    with S._file_lock(path):
        data = S._load_json(path, {})
        saved = data.get("providers") if isinstance(data, dict) else None
        merged = dict(saved) if isinstance(saved, dict) else {}
        now = S.now_ts()
        with _AUTH_LOCK:
            for p, e in _AUTH.items():
                cur = merged.get(p)
                if cur is None or S.as_float(e.get("until"), 0.0) >= S.as_float(cur.get("until"), 0.0):
                    merged[p] = e
            merged = _auth_prune(merged, now)
            payload = {"version": 1, "saved_at": now, "providers": merged}
            ok = S._atomic_write(path, S._dumps(payload))
            if ok:
                _AUTH.clear()
                _AUTH.update(merged)
                S._AUTH_LOADED = True
                _AUTH_MTIME = S._file_mtime(path)
        return ok


def note_provider_auth(provider: str, status: int, detail: str = "") -> float:
    """プロバイダ単位の認証失敗を覚える。既存の期限が長いときは伸ばさない。"""
    if not provider:
        return 0.0
    _ensure_auth_loaded()
    until = S.now_ts() + max(1.0, AUTH_TTL_S)
    with _AUTH_LOCK:
        cur = _AUTH.get(provider)
        if cur and S.as_float(cur.get("until"), 0.0) > until:
            return S.as_float(cur.get("until"), 0.0)
        _AUTH[provider] = {"until": until, "status": int(status), "at": S.now_ts(),
                           "detail": S.truncate(detail or "", 200)}
    _auth_save()
    return until


def clear_provider_auth(provider: str) -> None:
    """呼び出しが通ったら記憶を消す（キーを直したのに古い記憶で避け続けるのを防ぐ）。"""
    if not provider:
        return
    _ensure_auth_loaded()
    path = auth_path()
    with S._file_lock(path):
        with _AUTH_LOCK:
            _AUTH.pop(provider, None)
            data = S._load_json(path, {})
            saved = data.get("providers") if isinstance(data, dict) else None
            merged = dict(saved) if isinstance(saved, dict) else {}
            merged.pop(provider, None)
            now = S.now_ts()
            merged = _auth_prune(merged, now)
            payload = {"version": 1, "saved_at": now, "providers": merged}
            ok = S._atomic_write(path, S._dumps(payload))
            if ok:
                _AUTH.clear()
                _AUTH.update(merged)
                S._AUTH_LOADED = True
                global _AUTH_MTIME
                _AUTH_MTIME = S._file_mtime(path)


def provider_auth_blocked(provider: str) -> dict | None:
    """認証失敗の記憶が生きていればその内容を返す（自動選抜の除外判断に使う）。"""
    _ensure_auth_loaded()
    now = S.now_ts()
    with _AUTH_LOCK:
        entry = _AUTH.get(provider)
        if not entry or S.as_float(entry.get("until"), 0.0) <= now:
            return None
        return dict(entry)


EXPORTS = {
    "_COOLDOWN": _COOLDOWN,
    "_COOLDOWN_LOCK": _COOLDOWN_LOCK,
    "_COOLDOWN_DEFAULT_S": _COOLDOWN_DEFAULT_S,
    "_COOLDOWN_MAX_S": _COOLDOWN_MAX_S,
    "_UNAVAILABLE_S": _UNAVAILABLE_S,
    "_COOLDOWN_LADDER_S": _COOLDOWN_LADDER_S,
    "cooldowns_path": cooldowns_path,
    "_cooldown_prune": _cooldown_prune,
    "_ensure_cooldown_loaded": _ensure_cooldown_loaded,
    "_cooldown_save": _cooldown_save,
    "_parse_retry_after": _parse_retry_after,
    "note_cooldown": note_cooldown,
    "note_rate_limited": note_rate_limited,
    "note_unavailable": note_unavailable,
    "cooling_refs": cooling_refs,
    "is_cooling": is_cooling,
    "_AUTH": _AUTH,
    "_AUTH_MTIME": _AUTH_MTIME,
    "_AUTH_LOCK": _AUTH_LOCK,
    "AUTH_TTL_S": AUTH_TTL_S,
    "auth_path": auth_path,
    "_auth_prune": _auth_prune,
    "_ensure_auth_loaded": _ensure_auth_loaded,
    "_auth_save": _auth_save,
    "note_provider_auth": note_provider_auth,
    "clear_provider_auth": clear_provider_auth,
    "provider_auth_blocked": provider_auth_blocked,
}
