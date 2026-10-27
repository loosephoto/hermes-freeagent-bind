"""Fair scheduler is optional, bounded and preserves caller isolation."""
import importlib.util
import json
import os
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))


class FairIntegrationTest(unittest.TestCase):
    def test_explicit_fair_keys_are_accepted_by_parallel_runner(self):
        import inspect
        from freeagent_bind import server as S
        self.assertIn('fair_keys', inspect.signature(S.run_parallel).parameters)

    def test_opt_in_uses_shared_scheduler_for_unique_keys(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(2, 16)
        self.addCleanup(scheduler.shutdown)
        with mock.patch.object(S, 'FAIR_ENABLED', True, create=True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler, create=True) as get:
            rows = S.run_parallel(['a', 'b'], lambda x: {'value': x}, max_workers=2,
                                  fair_keys=['a', 'b'])
        self.assertEqual(rows, [{'value': 'a'}, {'value': 'b'}])
        get.assert_called_once()

    def test_unqualified_model_does_not_create_arbitrary_provider_lanes(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(2,128)
        self.addCleanup(scheduler.shutdown)
        with mock.patch.object(S,'FAIR_ENABLED',True), \
             mock.patch.object(S,'_get_fair_scheduler',return_value=scheduler) as get, \
             mock.patch.object(S,'call_model',return_value={'text':'ok'}):
            for i in range(35):
                data=S.tool_map({'items':['item'],'instruction':'test','model':f'unknown{i}/model'})
                self.assertEqual(data['ok'],1)
        get.assert_not_called()
        self.assertEqual(len(scheduler._threads),0)

    def test_map_uses_scheduler_for_distinct_prompts(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(2, 16)
        self.addCleanup(scheduler.shutdown)
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler) as get, \
             mock.patch.object(S, 'call_model', side_effect=lambda *a, **k: {'text': 'ok'}):
            data = S.tool_map({'items':['a','b'], 'instruction':'test', 'model':'nous/model'})
        self.assertEqual(data['ok'], 2)
        get.assert_called_once()

    def test_ask_many_uses_scheduler_for_distinct_models(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(2, 16)
        self.addCleanup(scheduler.shutdown)
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler) as get, \
             mock.patch.object(S, 'call_model', side_effect=lambda ref, *a, **k: {'model':ref}):
            rows = S.ask_many(['nous/a', 'nous/b'], 'test')
        self.assertEqual([x['model'] for x in rows], ['nous/a', 'nous/b'])
        get.assert_called_once()

    def test_ask_map_uses_scheduler_for_distinct_prompts(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(2, 16)
        self.addCleanup(scheduler.shutdown)
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler) as get, \
             mock.patch.object(S, 'call_model', side_effect=lambda *a, **k: {'text':'ok'}):
            rows = S.ask_map([('nous/model','a'),('nous/model','b')])
        self.assertEqual(len(rows), 2)
        get.assert_called_once()

    def test_repeated_map_prompts_use_legacy_coalescer_with_wait_expiry(self):
        from unittest import mock
        from freeagent_bind import server as S
        spec = {'base_url':'http://127.0.0.1:1/v1', 'key':'test-key', 'key_env':None,
                'free_kind':'allowlist', 'free_model_ids':['m'], 'always_ready':True,
                'models_path':'/models'}
        calls = []
        def upstream(path, provider=None, payload=None, timeout=None, **kwargs):
            calls.append(path)
            time.sleep(0.2)
            return {'choices':[{'message':{'content':'ok'}, 'finish_reason':'stop'}], 'usage':{}}
        with mock.patch.dict(S.PROVIDER_SPECS, {'testp': spec}), \
             mock.patch.object(S, 'PROVIDER_ORDER', ['testp']), \
             mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, 'COALESCE_WAIT_S', 0.04), \
             mock.patch.object(S, 'provider_http', side_effect=upstream), \
             mock.patch.object(S, 'free_model_refs', return_value=['testp/m']), \
             mock.patch.object(S, 'observe_call'), \
             mock.patch.object(S, '_get_fair_scheduler') as get:
            r = S.handle_tool_call({'name':'freeagent_map','arguments':{
                'model':'testp/m','instruction':'test','items':['same','same']}})
        self.assertFalse(r['isError'], r)
        self.assertEqual(r['structuredContent']['ok'], 2)
        self.assertEqual(r['structuredContent']['coalesced_calls'], 0)
        self.assertEqual(len([p for p in calls if 'chat' in p]), 2)
        get.assert_not_called()

    def test_timeout_cancels_queued_but_not_running_jobs(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(2, 32)
        self.addCleanup(scheduler.shutdown)
        starts = []
        lock = threading.Lock()
        def work(x):
            with lock: starts.append(x)
            time.sleep(0.3)
            return {'value':x}
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler):
            started=time.monotonic()
            rows=S.run_parallel(list(range(8)),work,2,S.Deadline(0.08),fair_keys=list(range(8)))
            self.assertLess(time.monotonic()-started,0.25)
            self.assertEqual(sum(bool(r.get('dropped')) for r in rows),8)
            time.sleep(0.35)
        self.assertEqual(len(starts),2)
        self.assertEqual(scheduler._pending,0)

    def test_saturated_provider_does_not_block_another_provider(self):
        from unittest import mock
        from concurrent.futures import ThreadPoolExecutor
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(2,32)
        self.addCleanup(scheduler.shutdown)
        gate=S._ProviderGate()
        started=threading.Event(); release=threading.Event()
        self.addCleanup(release.set)
        def work(provider):
            waited=gate.acquire(provider,timeout=2)
            if waited is None: return {'error':'gate timeout'}
            try:
                if provider=='a':
                    started.set(); release.wait(2)
                return {'provider':provider}
            finally: gate.release(provider)
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler), \
             mock.patch.object(S, 'provider_slot_cap', return_value=1):
            with ThreadPoolExecutor(max_workers=2) as callers:
                a=callers.submit(S.run_parallel,['a','a'],work,2,S.Deadline(1),
                                 fair_keys=['a1','a2'],fair_providers=['a','a'])
                self.assertTrue(started.wait(1))
                b=callers.submit(S.run_parallel,['b'],work,1,S.Deadline(.25),
                                 fair_keys=['b1'],fair_providers=['b'])
                result=b.result(timeout=1)
                release.set()
                self.assertEqual(result,[{'provider':'b'}])
                self.assertEqual(len(a.result(timeout=2)),2)

    def test_collector_exception_cancels_unstarted_work(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(1,32)
        self.addCleanup(scheduler.shutdown)
        starts=[]
        def work(x):
            starts.append(x)
            if x: time.sleep(0.2)
            return {'value':x}
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler), \
             mock.patch.object(S, 'progress_emit', side_effect=RuntimeError('progress failed')):
            with self.assertRaisesRegex(RuntimeError, 'progress failed'):
                S.run_parallel(list(range(8)),work,1,fair_keys=list(range(8)))
        time.sleep(0.25)
        self.assertLessEqual(len(starts),2)
        self.assertEqual(scheduler._pending,0)

    def test_queue_full_returns_visible_errors_without_starting_work(self):
        from unittest import mock
        from freeagent_bind import server as S
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(1,1)
        self.addCleanup(scheduler.shutdown)
        starts=[]
        with mock.patch.object(S, 'FAIR_ENABLED', True), \
             mock.patch.object(S, '_get_fair_scheduler', return_value=scheduler):
            rows=S.run_parallel([1,2],lambda x: starts.append(x),2,fair_keys=[1,2])
        self.assertEqual(starts,[])
        self.assertTrue(all(r.get('overloaded') and r.get('error') for r in rows))


class FairSchedulerTest(unittest.TestCase):
    def test_cancelled_queued_future_notifies_standard_wait(self):
        from concurrent.futures import wait
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(1,16)
        self.addCleanup(scheduler.shutdown)
        started=threading.Event(); release=threading.Event()
        def block(_):
            started.set(); release.wait(2)
        self.addCleanup(release.set)
        busy=scheduler.submit_batch(block,[0],limit=1)
        self.assertTrue(started.wait(2))
        queued=scheduler.submit_batch(lambda x:x,[1],limit=1)
        scheduler.cancel_batch(queued)
        done, not_done=wait(queued.futures,timeout=.1)
        self.assertEqual(done,set(queued.futures))
        self.assertFalse(not_done)
        release.set(); busy.futures[0].result(timeout=2)

    def test_stdio_eof_drains_dispatched_calls_before_scheduler_shutdown(self):
        import subprocess
        code = '''\nimport os,sys,io,json,time\nos.environ['FREEAGENT_FAIR_SCHEDULER']='1'\nsys.path.insert(0,sys.argv[1])\nfrom freeagent_bind import server as S\nif sys.argv[2]=='preinit': S._get_fair_scheduler()\ndef delayed(params):\n    time.sleep(.2)\n    rows=S.run_parallel([1],lambda x:{"value":x},1,fair_keys=[1],fair_providers=['nous'])\n    return {"rows":rows}\nS.handle_tool_call=delayed\nsys.stdin=io.StringIO(json.dumps({"jsonrpc":"2.0","id":42,"method":"tools/call","params":{"name":"probe"}})+'\\n')\nS.serve()\n'''
        for mode in ('preinit','lazy'):
            with self.subTest(mode=mode):
                p=subprocess.run([sys.executable,'-c',code,os.path.join(ROOT,'src'),mode],
                                 capture_output=True,text=True,timeout=5,encoding='utf-8')
                self.assertEqual(p.returncode,0,p.stderr)
                lines=[json.loads(line) for line in p.stdout.splitlines() if line.startswith('{')]
                self.assertEqual(lines,[{'jsonrpc':'2.0','id':42,'result':{'rows':[{'value':1}]}}],p.stdout+p.stderr)

    def test_thread_start_failure_rolls_back_and_allows_retry(self):
        from unittest import mock
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(1,16)
        self.addCleanup(scheduler.shutdown)
        with mock.patch('threading.Thread.start', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError,'start failed'):
                scheduler.submit_batch(lambda x:x, [1], 1, ['provider'])
        self.assertEqual(scheduler._pending,0)
        batch=scheduler.submit_batch(lambda x:x, [2], 1, ['provider'])
        self.assertEqual(batch.futures[0].result(timeout=2),2)

    def test_partial_thread_start_failure_rolls_back_all_providers(self):
        from unittest import mock
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(2,16)
        self.addCleanup(scheduler.shutdown)
        original=threading.Thread.start
        calls=[]; executed=[]
        def start(thread):
            calls.append(thread)
            if len(calls)==2: raise RuntimeError('start failed')
            return original(thread)
        with mock.patch('threading.Thread.start',autospec=True,side_effect=start):
            with self.assertRaisesRegex(RuntimeError,'start failed'):
                scheduler.submit_batch(lambda x:executed.append(x),[1,2],2,['a','b'])
        self.assertEqual(scheduler._pending,0)
        self.assertEqual(executed,[])
        batch=scheduler.submit_batch(lambda x:x,[3,4],2,['a','b'])
        self.assertEqual([f.result(timeout=2) for f in batch.futures],[3,4])
        self.assertEqual(executed,[])

    def test_shutdown_permits_inflight_nested_executor(self):
        import subprocess
        code = '''\nimport sys,threading,time\nsys.path.insert(0,sys.argv[1])\nfrom freeagent_bind.fair_scheduler import FairScheduler\ns=FairScheduler(1,16)\nstarted=threading.Event()\ndef work(i):\n    started.set()\n    time.sleep(.2)\n    from concurrent.futures import ThreadPoolExecutor\n    with ThreadPoolExecutor(max_workers=1) as pool:\n        print("NESTED",pool.submit(lambda: 42).result(),flush=True)\ns.submit_batch(work,[1],1)\nassert started.wait(2)\nprint("RETURNED",flush=True)\n'''
        p=subprocess.run([sys.executable,'-c',code,os.path.join(ROOT,'src')],
                         capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertIn('NESTED 42',p.stdout,p.stdout+p.stderr)

    def test_normal_process_exit_drains_running_jobs(self):
        import subprocess
        code = '''\nimport sys,threading,time\nsys.path.insert(0,sys.argv[1])\nfrom freeagent_bind.fair_scheduler import FairScheduler\ns=FairScheduler(2,16)\nb=threading.Barrier(3)\ndef work(i):\n    b.wait(timeout=3)\n    time.sleep(.3)\n    print("WORK_DONE", i, flush=True)\ns.submit_batch(work,[1,2],2)\nb.wait(timeout=3)\nprint("RETURNED",flush=True)\n'''
        p=subprocess.run([sys.executable,'-c',code,os.path.join(ROOT,'src')],
                         capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(p.stdout.count('WORK_DONE'),2,p.stdout)

    def test_mixed_provider_batch_respects_request_limit_and_drains_on_shutdown(self):
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler=FairScheduler(2,128)
        lock=threading.Lock()
        first_three=threading.Barrier(3)
        active=0; peak=0; seen=[]; started=0
        def work(i):
            nonlocal active, peak, started
            with lock:
                active+=1; peak=max(peak,active); started+=1
                first = started <= 3
            if first: first_three.wait(timeout=3)
            time.sleep(.002)
            with lock:
                active-=1; seen.append(i)
            return i
        jobs=list(range(60))
        batch=scheduler.submit_batch(work,jobs,3,providers=[str(i % 3) for i in jobs])
        scheduler.shutdown()
        self.assertEqual(peak,3)
        self.assertEqual(sorted(seen),jobs)
        self.assertEqual([f.result(timeout=1) for f in batch.futures],jobs)
        self.assertEqual(scheduler._pending,0)

    def test_per_batch_limit_global_limit_and_worker_survives_exception(self):
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(3, 32)
        self.addCleanup(scheduler.shutdown)
        lock=threading.Lock()
        gate=threading.Event()
        active=[0,0]; peaks=[0,0]; entered=threading.Event()
        def work(batch):
            with lock:
                active[batch]+=1
                peaks[batch]=max(peaks[batch], active[batch])
                if sum(active)==3: entered.set()
            gate.wait(2)
            with lock: active[batch]-=1
            return batch
        first=scheduler.submit_batch(work,[0]*8,limit=1)
        second=scheduler.submit_batch(work,[1]*8,limit=2)
        self.assertTrue(entered.wait(2))
        self.assertEqual(peaks,[1,2])
        gate.set()
        self.assertEqual(len([x.result(timeout=3) for x in first.futures]),8)
        self.assertEqual(len([x.result(timeout=3) for x in second.futures]),8)
        def broken(_): raise SystemExit('worker exit')
        failure=scheduler.submit_batch(broken,[0],limit=1)
        with self.assertRaises(SystemExit): failure.futures[0].result(timeout=2)
        success=scheduler.submit_batch(lambda x:x,[42],limit=1)
        self.assertEqual(success.futures[0].result(timeout=2),42)

    def test_waiting_short_request_is_not_queued_behind_entire_batch(self):
        spec = importlib.util.find_spec('freeagent_bind.fair_scheduler')
        self.assertIsNotNone(spec, 'fair_scheduler has not been implemented')
        from freeagent_bind.fair_scheduler import FairScheduler
        scheduler = FairScheduler(max_workers=2, max_pending=32)
        self.addCleanup(scheduler.shutdown)
        started = threading.Event()
        gate = threading.Event()
        count_lock = threading.Lock()
        started_count = 0
        def slow(item):
            nonlocal started_count
            with count_lock:
                started_count += 1
                if started_count == 2: started.set()
            gate.wait(2)
            time.sleep(0.05)
            return item
        batch = scheduler.submit_batch(slow, list(range(12)), limit=2)
        self.assertTrue(started.wait(2))
        short = scheduler.submit_batch(lambda _: 'short', [0], limit=1)
        gate.set()
        self.assertEqual(short.futures[0].result(timeout=2), 'short')
        self.assertLess(sum(f.done() for f in batch.futures), 12)
        self.assertEqual([f.result(timeout=3) for f in batch.futures], list(range(12)))


if __name__ == '__main__':
    unittest.main()
