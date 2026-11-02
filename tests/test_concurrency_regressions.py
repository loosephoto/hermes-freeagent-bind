"""Regression tests for request-scoped cancellation and deadline collection."""
import http.client
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))
from _tmpstate import isolated_state
isolated_state('fa-concurrency-')
from freeagent_bind import server as S


class DeadlineCollectionTest(unittest.TestCase):
    def test_budget_scope_propagates_and_is_restored_in_fair_workers(self):
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(1, 16)
        self.addCleanup(scheduler.shutdown)
        deadline = S.Deadline(3)
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler):
            seen = S.run_parallel([0], lambda _: S.call_budget() is deadline, 1, deadline, fair_keys=[0])
            clean = S.run_parallel([0], lambda _: getattr(S._BUDGET_LOCAL, 'deadline', None), 1, None, fair_keys=[0])
        self.assertEqual(seen, [True])
        self.assertEqual(clean, [None])

    def test_cancelling_expired_deadline_never_extends_its_cutoff(self):
        deadline = S.Deadline(.01)
        cutoff = deadline._until
        time.sleep(.03)
        deadline.cancel()
        self.assertEqual(deadline._until, cutoff)

    def test_slow_progress_preserves_on_time_future_but_not_late_future(self):
        from freeagent_bind.fair_scheduler import FairScheduler
        for fair in (False, True):
            with self.subTest(fair=fair):
                scheduler = FairScheduler(3, 16)
                allow_second = threading.Event()
                allow_third = threading.Event()
                second_done = threading.Event()
                third_done = threading.Event()
                deadline = S.Deadline(.5)
                def worker(job):
                    if job == 1:
                        allow_second.wait(2)
                        second_done.set()
                    if job == 2:
                        allow_third.wait(2)
                        third_done.set()
                    return {'value':job}
                def progress(done, total, label):
                    if done == 1:
                        allow_second.set()
                        self.assertTrue(second_done.wait(2))
                        while not deadline.expired:
                            time.sleep(.005)
                        allow_third.set()
                        self.assertTrue(third_done.wait(2))
                        time.sleep(.03)
                try:
                    with mock.patch.object(S, 'FAIR_ENABLED', fair), \
                         mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler), \
                         mock.patch.object(S, 'progress_emit', side_effect=progress):
                        rows = S.run_parallel([0,1,2], worker, 3, deadline, fair_keys=[0,1,2])
                    self.assertEqual(rows[0], {'value':0})
                    self.assertEqual(rows[1], {'value':1})
                    self.assertTrue(rows[2]['dropped'])
                finally:
                    allow_second.set(); allow_third.set()
                    scheduler.shutdown()

    def test_agent_workers_do_not_start_next_inference_after_deadline(self):
        for count in (1, 2):
            with self.subTest(workers=count):
                started = threading.Barrier(count + 1)
                release = threading.Event()
                finished = threading.Event()
                lock = threading.Lock()
                calls = []
                output = []
                def model(ref, *args, **kwargs):
                    with lock:
                        calls.append((ref, S.call_budget()))
                    started.wait(timeout=2)
                    release.wait(2)
                    return {'text': '{"tool":"search","query":"q"}'}
                def invoke():
                    with S.budget_scope(.3) as deadline:
                        output.append(deadline)
                        S.tool_agent({'task':'q', 'max_steps':2})
                    finished.set()
                refs = [f'nous/{n}' for n in range(count)]
                with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
                     mock.patch.object(S, 'call_model', side_effect=model), \
                     mock.patch.object(S, '_agent_tool_call', return_value={'citations':[], 'hits':0, 'brief':[]}):
                    caller = threading.Thread(target=invoke)
                    caller.start()
                    try:
                        started.wait(timeout=2)
                        self.assertTrue(finished.wait(2))
                    finally:
                        release.set()
                        caller.join(2)
                        S._drain_parallel_futures()
                self.assertEqual(len(calls), count)
                self.assertTrue(all(deadline is output[0] for _, deadline in calls))

    def test_stdio_exit_drains_timed_out_worker_before_nested_executor_shutdown(self):
        import subprocess
        code = r'''import sys, io, json, time
sys.path.insert(0, sys.argv[1])
from concurrent.futures import ThreadPoolExecutor
from freeagent_bind import server as S
def work(x):
    time.sleep(.2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        print('NESTED', pool.submit(lambda: 42).result(), file=sys.stderr, flush=True)
    return {'x':x}
def handler(args):
    return {'rows': S.run_parallel([1], work, 1, S.Deadline(.05))}
S.HANDLERS['probe'] = handler
sys.stdin = io.StringIO(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call', 'params':{'name':'probe'}})+'\n')
S.serve()
'''
        p = subprocess.run([sys.executable, '-c', code, os.path.join(ROOT, 'src')],
                           capture_output=True, text=True, encoding='utf-8', timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('NESTED 42', p.stderr)
        replies = [json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
        self.assertTrue(replies[0]['result']['structuredContent']['rows'][0]['dropped'])

    def test_cancel_wakes_collector_and_prevents_queued_jobs(self):
        from freeagent_bind.fair_scheduler import FairScheduler
        for fair in (False, True):
            with self.subTest(fair=fair):
                scheduler = FairScheduler(2, 16)
                started = threading.Event()
                release = threading.Event()
                lock = threading.Lock()
                starts = []
                output = []
                def worker(job):
                    with lock:
                        starts.append(job)
                        if len(starts) == 2: started.set()
                    release.wait(2)
                    return {'value': job}
                deadline = S.Deadline(3)
                def run():
                    output.extend(S.run_parallel(list(range(4)), worker, 2, deadline,
                                                  fair_keys=list(range(4))))
                collector_ready = threading.Event()
                original_as_completed = S.as_completed
                def observed_as_completed(*args, **kwargs):
                    collector_ready.set()
                    yield from original_as_completed(*args, **kwargs)
                with mock.patch.object(S, 'FAIR_ENABLED', fair), \
                     mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler), \
                     mock.patch.object(S, 'as_completed', observed_as_completed):
                    caller = threading.Thread(target=run)
                    caller.start()
                    try:
                        self.assertTrue(started.wait(1))
                        self.assertTrue(collector_ready.wait(1))
                        deadline.cancel()
                        caller.join(.5)
                        self.assertFalse(caller.is_alive(), 'cancellation did not wake collector')
                        self.assertTrue(all(row.get('dropped') for row in output))
                    finally:
                        release.set()
                        caller.join(3)
                        scheduler.shutdown()
                self.assertEqual(sorted(starts), [0, 1])

    def test_collector_error_cancels_queued_legacy_jobs(self):
        starts = []
        first_started = threading.Event()
        release_first = threading.Event()
        release_other = threading.Event()
        errors = []
        def worker(job):
            starts.append(job)
            if job == 0:
                first_started.set()
                release_first.wait(2)
            else:
                release_other.wait(2)
            return {'x':job}
        def invoke():
            try:
                S.run_parallel(list(range(6)), worker, 1, S.Deadline(3))
            except RuntimeError as exc:
                errors.append(str(exc))
        with mock.patch.object(S, 'FAIR_ENABLED', False), \
             mock.patch.object(S, 'progress_emit', side_effect=RuntimeError('collector failed')):
            thread = threading.Thread(target=invoke)
            thread.start()
            try:
                self.assertTrue(first_started.wait(1))
                release_first.set()
                thread.join(.5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, ['collector failed'])
            finally:
                release_first.set()
                release_other.set()
                thread.join(3)
                S._drain_parallel_futures()
        self.assertLessEqual(len(starts), 2)

    def test_single_worker_returns_at_deadline_while_running_job_finishes(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)
        def worker(job):
            started.set()
            release.wait(2)
            finished.set()
            return {'value': job}
        before = time.monotonic()
        rows = S.run_parallel([1], worker, max_workers=1, deadline=S.Deadline(.08))
        elapsed = time.monotonic() - before
        self.assertTrue(started.is_set())
        self.assertLess(elapsed, .5)
        self.assertTrue(rows[0]['dropped'])
        self.assertFalse(finished.is_set())
        release.set()
        self.assertTrue(finished.wait(1))


class RequestCancellationTest(unittest.TestCase):
    def test_stdio_notification_cancels_running_unlimited_call(self):
        import subprocess
        code = r'''import sys, json, threading, time
sys.path.insert(0, sys.argv[1])
from freeagent_bind import server as S
S.CALL_BUDGET_S = 0
def work(x):
    print('START', file=sys.stderr, flush=True)
    time.sleep(1)
    return {'x':x}
def handler(args):
    return {'rows':S.run_parallel([1], work, 1, S.call_budget())}
S.HANDLERS['probe'] = handler
S.serve()
'''
        p = subprocess.Popen([sys.executable, '-u', '-c', code, os.path.join(ROOT,'src')],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8')
        assert p.stdin is not None and p.stdout is not None and p.stderr is not None
        def cleanup():
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=3)
        self.addCleanup(cleanup)
        p.stdin.write(json.dumps({'jsonrpc':'2.0','id':9,'method':'tools/call',
                                  'params':{'name':'probe'}})+'\n')
        p.stdin.flush()
        lines = []
        ready = threading.Event()
        def read_stderr():
            for line in p.stderr:
                if line.strip() == 'START':
                    ready.set()
        def read_stdout():
            lines.append(p.stdout.readline())
        threading.Thread(target=read_stderr, daemon=True).start()
        threading.Thread(target=read_stdout, daemon=True).start()
        self.assertTrue(ready.wait(3))
        p.stdin.write(json.dumps({'jsonrpc':'2.0','method':'notifications/cancelled',
                                  'params':{'requestId':9}})+'\n')
        p.stdin.flush()
        end = time.monotonic() + .5
        while not lines and time.monotonic() < end:
            time.sleep(.01)
        self.assertTrue(lines, 'stdio response did not arrive after cancellation')
        self.assertTrue(json.loads(lines[0])['result']['structuredContent']['rows'][0]['dropped'])
        p.stdin.close()
        p.stdin = None
        self.assertEqual(p.wait(timeout=3), 0)

    def test_duplicate_id_cleanup_keeps_other_request_registered(self):
        first = S.Deadline(10)
        second = S.Deadline(10)
        with S.request_scope('same'):
            S._register_active('same', first)
            S._register_active('same', second)
            S._unregister_active('same', first)
            self.assertTrue(S.cancel_request('same'))
            self.assertFalse(first.expired)
            self.assertTrue(second.expired)
            self.assertFalse(S.cancel_request('same'))

    def test_duplicate_id_cancel_reaches_both_active_requests(self):
        first, second = S.Deadline(10), S.Deadline(10)
        with S.request_scope(7):
            S._register_active(7, first)
            S._register_active(7, second)
            self.assertTrue(S.cancel_request(7))
            self.assertTrue(first.expired)
            self.assertTrue(second.expired)
            S._unregister_active(7, first)
            S._unregister_active(7, second)

    def test_unlimited_budget_still_allows_stdio_cancel(self):
        started = threading.Event()
        release = threading.Event()
        output = []
        def handler(_):
            return {'rows': S.run_parallel([1], lambda x: (started.set(), release.wait(3), {'x':x})[-1],
                                           1, deadline=S.call_budget())}
        def call():
            with S.request_scope('unlimited'):
                output.append(S.handle_tool_call({'name':'cancel_probe','arguments':{}}))
        with mock.patch.object(S, 'CALL_BUDGET_S', 0), \
             mock.patch.dict(S.HANDLERS, {'cancel_probe':handler}):
            thread = threading.Thread(target=call)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                self.assertEqual(S._replies_for({'jsonrpc':'2.0','method':'notifications/cancelled',
                                                 'params':{'requestId':'unlimited'}}), [])
                thread.join(.5)
                self.assertFalse(thread.is_alive())
                self.assertTrue(output[0]['structuredContent']['rows'][0]['dropped'])
            finally:
                release.set()
                thread.join(3)


class HttpCancellationTest(unittest.TestCase):
    def test_stateless_cancel_does_not_cancel_other_clients_with_same_id(self):
        entered = {tag: threading.Event() for tag in ('A', 'B')}
        release = threading.Event()
        results = {}
        def handler(args):
            deadline = S.call_budget()
            entered[args['tag']].set()
            release.wait(3)
            return {'cancelled': deadline.expired}
        server = S._http_build_server({'host': '127.0.0.1', 'port': 0, 'path': '/mcp'})
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        def post(body):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=4)
            try:
                conn.request('POST', '/mcp', json.dumps(body), {'Content-Type':'application/json'})
                response = conn.getresponse()
                data = response.read()
                return response.status, json.loads(data) if data else None
            finally:
                conn.close()
        def call(tag):
            results[tag] = post({'jsonrpc':'2.0', 'id':7, 'method':'tools/call',
                                 'params':{'name':'cancel_probe','arguments':{'tag':tag}}})
        with mock.patch.dict(S.HANDLERS, {'cancel_probe':handler}):
            threads = []
            try:
                for tag in entered:
                    t = threading.Thread(target=call, args=(tag,))
                    t.start(); threads.append(t)
                    self.assertTrue(entered[tag].wait(2))
                status, _ = post({'jsonrpc':'2.0','method':'notifications/cancelled',
                                  'params':{'requestId':7}})
                self.assertEqual(status, 202)
            finally:
                release.set()
                for t in threads: t.join(3)
        self.assertEqual(set(results), {'A', 'B'})
        for tag in entered:
            self.assertFalse(results[tag][1]['result']['structuredContent']['cancelled'])

    def test_same_id_in_distinct_sessions_cancels_only_sender(self):
        entered = {tag: threading.Event() for tag in ('A', 'B')}
        release = threading.Event()
        results = {}
        def handler(args):
            deadline = S.call_budget()
            entered[args['tag']].set()
            release.wait(3)
            return {'cancelled': deadline.expired}
        server = S._http_build_server({'host': '127.0.0.1', 'port': 0, 'path': '/mcp'})
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        port = server.server_address[1]
        def post(body, sid=None):
            conn = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
            headers = {'Content-Type': 'application/json', 'x-freeagent-harness': 'hermes'}
            if sid: headers['mcp-session-id'] = sid
            try:
                conn.request('POST', '/mcp', json.dumps(body), headers)
                response = conn.getresponse()
                data = response.read()
                return response.status, dict(response.getheaders()), json.loads(data) if data else None
            finally:
                conn.close()
        with mock.patch.dict(S.HANDLERS, {'cancel_probe': handler}):
            sessions = {}
            for tag in entered:
                status, headers, _ = post({'jsonrpc':'2.0','id':1,'method':'initialize',
                    'params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':tag,'version':'1'}}})
                self.assertEqual(status, 200)
                sessions[tag] = next(v for k, v in headers.items() if k.lower() == 'mcp-session-id')
            self.assertNotEqual(sessions['A'], sessions['B'])
            def call(tag):
                results[tag] = post({'jsonrpc':'2.0','id':7,'method':'tools/call',
                    'params':{'name':'cancel_probe','arguments':{'tag':tag}}}, sessions[tag])
            threads = []
            for tag in entered:
                t = threading.Thread(target=call, args=(tag,)); t.start(); threads.append(t)
                self.assertTrue(entered[tag].wait(2))
            status, _, _ = post({'jsonrpc':'2.0','method':'notifications/cancelled',
                                  'params':{'requestId':7}}, sessions['A'])
            self.assertEqual(status, 202)
            release.set()
            for t in threads: t.join(3); self.assertFalse(t.is_alive())
            self.assertTrue(results['A'][2]['result']['structuredContent']['cancelled'])
            self.assertFalse(results['B'][2]['result']['structuredContent']['cancelled'])
