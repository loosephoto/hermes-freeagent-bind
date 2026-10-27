"""Measure real run_parallel scheduling with offline, simulated I/O workers.

No providers, credentials, network, or persistent state are used. Results describe
only this process and synthetic workload, not real API latency or quality.
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from freeagent_bind import server as S  # noqa: E402


def measure(calls, jobs, workers, delay, budget):
    lock = threading.Lock()
    active = 0
    peak = 0
    peak_threads = 0
    started = 0
    finished = 0
    idle = threading.Event()
    idle.set()
    launch = threading.Barrier(calls)

    def worker(index):
        nonlocal active, peak, peak_threads, started, finished
        with lock:
            active += 1
            started += 1
            peak = max(peak, active)
            peak_threads = max(peak_threads, threading.active_count())
            idle.clear()
        try:
            time.sleep(delay)
            return {"item": index}
        finally:
            with lock:
                active -= 1
                finished += 1
                if not active:
                    idle.set()

    def one_call(_):
        launch.wait(timeout=10)
        begin = time.monotonic()
        rows = S.run_parallel(list(range(jobs)), worker, max_workers=workers,
                              deadline=S.Deadline(budget) if budget else None)
        return time.monotonic() - begin, rows

    tracemalloc.start()
    begin = time.monotonic()
    with ThreadPoolExecutor(max_workers=calls) as pool:
        results = list(pool.map(one_call, range(calls)))
    elapsed = time.monotonic() - begin
    with lock:
        active_at_return = active
        started_at_return = started
        finished_at_return = finished
    drained = idle.wait(timeout=max(2.0, delay + 1.0))
    with lock:
        after_drain = active
        final_finished = finished
    _, python_peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    times = sorted(t for t, _ in results)
    rows = [r for _, batch in results for r in batch]
    dropped = sum(bool(row.get("dropped")) for row in rows)
    errors = sum(bool(row.get("error")) and not bool(row.get("dropped")) for row in rows)
    p95_index = max(0, (95 * len(times) + 99) // 100 - 1)
    return {"workload": "simulated_io", "calls": calls, "jobs_per_call": jobs,
            "workers_per_call": workers, "delay_s": delay, "budget_s": budget,
            "requested_jobs": calls * jobs, "completed_jobs": len(rows) - dropped - errors,
            "dropped_jobs": dropped, "errors": errors,
            "elapsed_s": round(elapsed, 4), "call_p50_s": round(statistics.median(times), 4),
            "call_p95_s": round(times[p95_index], 4), "peak_active_workers": peak,
            "peak_process_threads": peak_threads,
            "started_at_return": started_at_return, "finished_at_return": finished_at_return,
            "active_workers_at_return": active_at_return, "drained": drained,
            "active_workers_after_drain": after_drain, "finished_after_drain": final_finished,
            "python_peak_bytes": python_peak_bytes}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--calls", type=int, default=4)
    p.add_argument("--jobs", type=int, default=8)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--delay", type=float, default=0.1)
    p.add_argument("--budget", type=float, default=1.0, help="Seconds; 0 means no deadline")
    args = p.parse_args(argv)
    if not (1 <= args.calls <= 64 and 1 <= args.jobs <= 256 and 1 <= args.workers <= 32):
        p.error("calls must be 1..64, jobs 1..256, and workers 1..32")
    if not (0 <= args.delay <= 60 and 0 <= args.budget <= 300):
        p.error("delay must be 0..60 and budget 0..300 seconds")
    print(json.dumps(measure(args.calls, args.jobs, args.workers, args.delay, args.budget), ensure_ascii=False))


if __name__ == "__main__":
    main()
