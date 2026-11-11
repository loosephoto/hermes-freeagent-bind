"""§4.0/§4.1 要求単位のキャンセルと公平な共有スケジューラ（並列・合流）

server.py から機械的に切り出した（scripts/split_module.py）。
server 側の名前は `S.<name>` の属性参照で呼ぶ（テストが `patch.object(server, ...)` で
差し替えるため、直接名で束縛すると届かない）。
"""
from __future__ import annotations

import contextlib
import math
import os
import threading
import time

from . import server as S  # noqa: E402

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


FAIR_ENABLED = S.as_flag(os.environ.get("FREEAGENT_FAIR_SCHEDULER", "0"))


FAIR_WORKERS = max(1, min(32, S._env_int("FREEAGENT_FAIR_WORKERS", S.MAX_WORKERS)))


FAIR_MAX_PENDING = max(1, min(10000, S._env_int("FREEAGENT_FAIR_MAX_PENDING", 1024)))


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
    if not S.FAIR_ENABLED or keys is None or len(keys) != len(jobs):
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

    scheduler = S._get_fair_scheduler()
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
        for future in S.as_completed(signals, timeout=None if deadline is None or math.isinf(deadline.remaining()) else deadline.remaining()):
            if deadline is not None and (future is deadline._cancel_signal or deadline.expired):
                timed_out = True
                break
            i = futures[future]
            if not _collect_parallel_future(future, i, out, collected, deadline):
                timed_out = True
                break
            done += 1
            S.progress_emit(done, len(jobs), S.progress_label(jobs[i]))
            if done == len(futures):
                break
        completed = not timed_out
    except S.FuturesTimeout:
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
        S.progress_emit(done, len(jobs), "予算切れで打ち切りました")
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
    completed_at, result = S._parallel_future_result(future, deadline)
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
                    else S.time.perf_counter() < deadline._until)
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
    workers = max(1, min(max_workers or S.MAX_WORKERS, len(jobs)))
    # 合流器は**呼び出し側スレッド**に張られている（ツール呼び出し 1 回ぶん）。ワーカーは別スレッド
    # なので、ここで包んで同じ合流器を渡す（渡さないと並列経路で重複が畳まれない）。
    _coalescer = S._call_coalescer()
    if _coalescer is not None:
        worker = S._coalescer_worker(worker, _coalescer)
    # 呼び出し単位の ID も**ワーカーへ明示的に渡す**（スレッドローカルは別スレッドに伝播しない）。
    # 渡さないと、並列経路の traces だけ run が空になり「呼び出しの内訳」が復元できない。
    _parent_run, _parent_tool = S.current_run(), S.current_tool()
    if _parent_run:
        worker = S._run_worker(worker, _parent_run, _parent_tool)
    if deadline is not None:
        inner_worker = worker
        def budgeted_worker(job):
            previous = getattr(S._BUDGET_LOCAL, "deadline", None)
            S._BUDGET_LOCAL.deadline = deadline
            try:
                if deadline.expired:
                    result = _dropped_result(deadline, "未実行")
                else:
                    try:
                        result = inner_worker(job)
                    except Exception as exc:
                        result = {"error": f"{type(exc).__name__}: {exc}"}
                return S.time.perf_counter(), result
            finally:
                S._BUDGET_LOCAL.deadline = previous
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
            S.progress_emit(i + 1, len(jobs), S.progress_label(job))
        return out
    pool = S.ThreadPoolExecutor(max_workers=workers)
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
        for fut in S.as_completed(signals, timeout=None if deadline is None or math.isinf(deadline.remaining()) else deadline.remaining()):
            if deadline is not None and (fut is deadline._cancel_signal or deadline.expired):
                timed_out = True
                break
            i = futures[fut]
            if not _collect_parallel_future(fut, i, out, collected, deadline):
                timed_out = True
                break
            done += 1
            S.progress_emit(done, len(jobs), S.progress_label(jobs[i]))
            if done == len(futures):
                break
        completed = not timed_out
    except S.FuturesTimeout:
        timed_out = True
    finally:
        pool.shutdown(wait=completed, cancel_futures=not completed)
    if timed_out:
        _collect_completed_parallel(futures, out, collected, deadline)
        done = len(collected)
        for i, value in enumerate(out):
            if i not in collected:
                out[i] = _dropped_result(deadline, "回収を打ち切り")
        S.progress_emit(done, len(jobs), "予算切れで打ち切りました")
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
    （利用者の明示除外）や `identity_avoid`（既に使ったモデル）に落ちないようにする（`S._ModelClaims`）。
    """
    claims = S._ModelClaims(refs, avoid, identity_avoid)
    return S.run_parallel(
        list(refs),
        lambda ref: S.call_model(ref, prompt, system=system, max_tokens=max_tokens,
                               temperature=temperature, kind=kind, claims=claims),
        max_workers=min(len(refs), S.MAX_WORKERS), deadline=S.call_budget(), fair_keys=refs,
        fair_providers=[S.split_ref(ref)[0] for ref in refs])


def ask_map(pairs: list[tuple[str, str]], *, system: str = "", max_tokens: int = 800,
            temperature: float | None = None, kind: str = "fanout") -> list[dict]:
    """(ref, prompt) の組を並列に処理する。"""
    return S.run_parallel(
        list(pairs),
        lambda pair: S.call_model(pair[0], pair[1], system=system, max_tokens=max_tokens,
                                temperature=temperature, kind=kind),
        max_workers=min(len(pairs), S.MAX_WORKERS), deadline=S.call_budget(),
        fair_keys=[(ref, prompt) for ref, prompt in pairs],
        fair_providers=[S.split_ref(ref)[0] for ref, _ in pairs])


EXPORTS = {
    "_REQ_LOCAL": _REQ_LOCAL,
    "_ACTIVE_LOCK": _ACTIVE_LOCK,
    "_ACTIVE_BUDGETS": _ACTIVE_BUDGETS,
    "_active_key": _active_key,
    "_register_active": _register_active,
    "_unregister_active": _unregister_active,
    "cancel_request": cancel_request,
    "request_scope": request_scope,
    "FAIR_ENABLED": FAIR_ENABLED,
    "FAIR_WORKERS": FAIR_WORKERS,
    "FAIR_MAX_PENDING": FAIR_MAX_PENDING,
    "_FAIR_SCHEDULER": _FAIR_SCHEDULER,
    "_FAIR_SCHEDULER_LOCK": _FAIR_SCHEDULER_LOCK,
    "_get_fair_scheduler": _get_fair_scheduler,
    "_fair_eligible": _fair_eligible,
    "_run_fair": _run_fair,
    "_PARALLEL_INFLIGHT": _PARALLEL_INFLIGHT,
    "_PARALLEL_INFLIGHT_LOCK": _PARALLEL_INFLIGHT_LOCK,
    "_track_parallel_future": _track_parallel_future,
    "_drain_parallel_futures": _drain_parallel_futures,
    "_parallel_future_result": _parallel_future_result,
    "_collect_parallel_future": _collect_parallel_future,
    "_collect_completed_parallel": _collect_completed_parallel,
    "run_parallel": run_parallel,
    "_dropped_result": _dropped_result,
    "ask_many": ask_many,
    "ask_map": ask_map,
}
