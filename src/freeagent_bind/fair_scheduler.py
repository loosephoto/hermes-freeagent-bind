"""Bounded request-round-robin scheduling with independent provider lanes."""
import atexit
import collections
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor  # register its shutdown first
import threading
import weakref


_REGISTRY = weakref.WeakSet()
_REGISTRY_LOCK = threading.Lock()


def _shutdown_all():
    with _REGISTRY_LOCK:
        schedulers = list(_REGISTRY)
    for scheduler in schedulers:
        scheduler.shutdown()


# The threading exit hooks run before concurrent.futures shuts down, in reverse
# registration order.  This lets a draining worker finish nested executor work.
_register_thread_exit = getattr(threading, '_register_atexit', None)
if _register_thread_exit is not None:
    _register_thread_exit(_shutdown_all)
else:  # non-CPython fallback: drain workers during ordinary interpreter exit
    atexit.register(_shutdown_all)


class QueueFullError(Exception):
    """The shared queue cannot accept this batch without exceeding its bound."""


class _Batch:
    def __init__(self, fn, jobs, limit, providers):
        self.pending = {}
        self.futures = []
        for item, provider in zip(jobs, providers):
            future = concurrent.futures.Future()
            self.futures.append(future)
            self.pending.setdefault(provider, collections.deque()).append((fn, item, future))
        self.limit = limit
        self.running = 0
        self.queued = set()


class FairScheduler:
    """Schedule batches in turn within provider lanes; running calls are not interrupted.

    Each provider gets up to max_workers threads, created only as demand grows.
    """

    def __init__(self, max_workers, max_pending):
        if max_workers < 1 or max_pending < 1:
            raise ValueError('worker and queue limits must be positive')
        self._cv = threading.Condition()
        self._queues = {}
        self._pending_by_provider = {}
        self._running_by_provider = {}
        self._threads = {}
        self._pending = 0
        self._running = 0
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._closed = False
        with _REGISTRY_LOCK:
            _REGISTRY.add(self)

    def _enqueue(self, batch):
        if batch.running >= batch.limit:
            return
        for provider, pending in batch.pending.items():
            if pending and provider not in batch.queued:
                batch.queued.add(provider)
                self._queues[provider].append(batch)
        self._cv.notify_all()

    def _ensure_workers(self, provider):
        threads = self._threads.setdefault(provider, [])
        demand = self._pending_by_provider.get(provider, 0) + self._running_by_provider.get(provider, 0)
        target = min(self._max_workers, demand)
        while len(threads) < target:
            thread = threading.Thread(target=self._worker, args=(provider,), daemon=True,
                                      name=f'freeagent-fair-{provider}-{len(threads)}')
            thread.start()
            threads.append(thread)

    def submit_batch(self, fn, jobs, limit, providers=None):
        if limit < 1:
            raise ValueError('batch limit must be positive')
        if providers is None:
            providers = [None] * len(jobs)
        if len(providers) != len(jobs):
            raise ValueError('provider count does not match jobs')
        batch = _Batch(fn, jobs, limit, providers)
        with self._cv:
            if self._closed:
                raise RuntimeError('scheduler is closed')
            if self._pending + len(jobs) > self._max_pending:
                raise QueueFullError('shared execution queue is full')
            self._pending += len(jobs)
            for provider, pending in batch.pending.items():
                self._queues.setdefault(provider, collections.deque())
                self._pending_by_provider[provider] = self._pending_by_provider.get(provider, 0) + len(pending)
            self._enqueue(batch)
            try:
                for provider in batch.pending:
                    self._ensure_workers(provider)
            except BaseException:
                # Workers cannot dequeue while _cv is held: undo the whole batch.
                for provider in tuple(batch.queued):
                    self._queues[provider].remove(batch)
                    batch.queued.remove(provider)
                for provider, pending in batch.pending.items():
                    self._pending -= len(pending)
                    self._pending_by_provider[provider] -= len(pending)
                    pending.clear()
                self._cv.notify_all()
                raise
        return batch

    def cancel_batch(self, batch):
        removed = []
        with self._cv:
            for provider in tuple(batch.queued):
                self._queues[provider].remove(batch)
                batch.queued.remove(provider)
            for provider, pending in batch.pending.items():
                while pending:
                    _, _, future = pending.popleft()
                    self._pending -= 1
                    self._pending_by_provider[provider] -= 1
                    removed.append(future)
            self._cv.notify_all()
        # Future callbacks may call back into this scheduler: never run them under _cv.
        for future in removed:
            if future.cancel():
                future.set_running_or_notify_cancel()

    def _worker(self, provider):
        while True:
            with self._cv:
                queue = self._queues[provider]
                while not queue:
                    if self._closed and not self._pending and not self._running:
                        return
                    self._cv.wait()
                batch = queue.popleft()
                batch.queued.remove(provider)
                if batch.running >= batch.limit:
                    continue
                fn, job, future = batch.pending[provider].popleft()
                self._pending -= 1
                self._pending_by_provider[provider] -= 1
                self._running += 1
                self._running_by_provider[provider] = self._running_by_provider.get(provider, 0) + 1
                batch.running += 1
                self._enqueue(batch)
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(job))
                    except BaseException as exc:
                        future.set_exception(exc)
            finally:
                with self._cv:
                    batch.running -= 1
                    self._running -= 1
                    self._running_by_provider[provider] -= 1
                    self._enqueue(batch)
                    self._cv.notify_all()

    def shutdown(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()
            threads = [thread for lane in self._threads.values() for thread in lane]
        for thread in threads:
            thread.join()
        with _REGISTRY_LOCK:
            _REGISTRY.discard(self)
