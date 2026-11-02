"""Regressions for verifier history updates during overlapping thoughts."""
import json
import itertools
import multiprocessing
import os
import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from freeagent_bind import server as S


def _stale_selection_child(sid, snapshot, additions, ready, release, results):
    try:
        observed = S.thought_get(sid) or {}
        if not observed or observed.get('verifier_models') != snapshot:
            results.put({'error': 'child did not observe the expected snapshot'})
            return
        ready.set()
        if not release.wait(15):
            results.put({'error': 'child timed out'})
            return
        results.put(S.thought_merge(sid, {'text': 'child'}, assign_number=True,
                    verifier_models=snapshot + additions, verifier_snapshot=snapshot,
                    verifier_additions=additions,
                    verifier_snapshot_generation=observed.get('verifier_generation', 0)))
    except Exception as exc:
        results.put({'error': repr(exc)})


class ThoughtHistoryConcurrencyTest(unittest.TestCase):
    def test_equal_forward_and_backward_clock_preserve_latest_and_stale_call(self):
        old = [f'nous/old{i}' for i in range(12)]
        for later in (9999, 10000, 10001):
            with self.subTest(later=later), tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
                path = pathlib.Path(tmp) / 'thoughts.json'
                with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path)}), \
                     mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                     mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
                     mock.patch.object(S, 'now_ts', return_value=10000):
                    sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                          verifier_models=old, verifier_snapshot=[])['session_id']
                    observed = S.thought_get(sid) or {}
                    with mock.patch.object(S, 'now_ts', return_value=later):
                        S.thought_merge(sid, {'text': 'other'}, assign_number=True,
                                        verifier_models=old + ['nous/other'], verifier_snapshot=old,
                                        verifier_snapshot_generation=observed['verifier_generation'])
                        latest = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
                        result = S.thought_merge(sid, {'text': 'stale'}, assign_number=True,
                                                 verifier_models=old + ['nous/stale'], verifier_snapshot=old,
                                                 verifier_snapshot_generation=observed['verifier_generation'])
                    self.assertNotIn('error', result)
                    row = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
                    self.assertEqual(set(latest['verifier_models'] + ['nous/stale']), set(row['verifier_models']))
                    self.assertEqual(latest['verifier_generation'] + 1, row['verifier_generation'])

    def test_disk_history_snapshot_ignores_stale_cache_and_mtime(self):
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path)}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=['nous/a'], verifier_snapshot=[])['session_id']
                S._THOUGHTS[sid]['verifier_models'] = ['nous/stale']
                S._THOUGHTS[sid]['verifier_generation'] = 0
                with mock.patch.object(S, '_file_mtime', return_value=S._THOUGHTS_MTIME):
                    observed = S.thought_get(sid) or {}
                self.assertEqual(['nous/a'], observed['verifier_models'])
                self.assertEqual(1, observed['verifier_generation'])

    def test_legacy_ledger_migrates_and_failure_does_not_advance_generation(self):
        old = [f'nous/old{i}' for i in range(12)]
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path)}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=old, verifier_snapshot=[])['session_id']
                legacy = json.loads(path.read_text(encoding='utf-8'))
                legacy['chains'][sid].pop('verifier_generation')
                path.write_text(json.dumps(legacy), encoding='utf-8')
                S._THOUGHTS.clear()
                S._THOUGHTS_LOADED = False
                observed = S.thought_get(sid) or {}
                self.assertTrue(observed)
                self.assertEqual(0, observed['verifier_generation'])
                before = path.read_bytes()
                memory_before = json.dumps(S._THOUGHTS, sort_keys=True)
                with mock.patch.object(S, '_atomic_write', return_value=False):
                    failed = S.thought_merge(sid, {'text': 'failed'}, assign_number=True,
                                             verifier_models=old + ['nous/new'], verifier_snapshot=old,
                                             verifier_snapshot_generation=0)
                self.assertIn('error', failed)
                self.assertEqual(before, path.read_bytes())
                self.assertEqual(memory_before, json.dumps(S._THOUGHTS, sort_keys=True))
                result = S.thought_merge(sid, {'text': 'success'}, assign_number=True,
                                         verifier_models=old + ['nous/new'], verifier_snapshot=old,
                                         verifier_snapshot_generation=0)
                self.assertNotIn('error', result)
                stored = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
                self.assertEqual(1, stored['verifier_generation'])
                self.assertEqual(old[1:] + ['nous/new'], stored['verifier_models'])

    def test_disabled_memory_history_never_creates_files(self):
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path)}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, 'THOUGHTS_ENABLED', False):
                self.assertFalse(S.tool_think({'thought': 'disabled'})['enabled'])
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=['nous/a'], verifier_snapshot=[],
                                      verifier_snapshot_generation=0)['session_id']
                initial = S.thought_get(sid) or {}
                self.assertEqual(1, initial['verifier_generation'])
                S.thought_merge(sid, {'text': 'next'}, assign_number=True,
                                verifier_models=['nous/a', 'nous/b'], verifier_snapshot=['nous/a'],
                                verifier_snapshot_generation=1)
                self.assertEqual(2, (S.thought_get(sid) or {})['verifier_generation'])
                self.assertEqual([], list(pathlib.Path(tmp).iterdir()))

    def test_cross_process_aba_keeps_whole_latest_history(self):
        old = [f'nous/old{i}' for i in range(12)]
        ctx = multiprocessing.get_context('spawn')
        ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path),
                                             'FREEAGENT_THOUGHTS': '1'}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=old, verifier_snapshot=[])['session_id']
                child = ctx.Process(target=_stale_selection_child,
                                    args=(sid, old, ['nous/child'], ready, release, results))
                child.start()
                try:
                    self.assertTrue(ready.wait(15))
                    first = S.thought_get(sid) or {}
                    S.thought_merge(sid, {'text': 'X'}, assign_number=True, verifier_models=old,
                                    verifier_snapshot=old, verifier_additions=[old[0]],
                                    verifier_snapshot_generation=first['verifier_generation'])
                    changed = S.thought_get(sid) or {}
                    self.assertNotEqual(old, changed['verifier_models'])
                    S.thought_merge(sid, {'text': 'H'}, assign_number=True, verifier_models=old,
                                    verifier_snapshot=changed['verifier_models'], verifier_additions=old,
                                    verifier_snapshot_generation=changed['verifier_generation'])
                    self.assertEqual(old, (S.thought_get(sid) or {})['verifier_models'])
                finally:
                    release.set()
                    child.join(15)
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                self.assertEqual(0, child.exitcode)
                self.assertNotIn('error', results.get(timeout=5))
                row = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
                self.assertEqual(set(old + ['nous/child']), set(row['verifier_models']))
                self.assertEqual(4, row['verifier_generation'])
        results.close()
        results.join_thread()

    def test_real_ledger_aba_does_not_trim_latest_history(self):
        old = [f'nous/old{i}' for i in range(12)]
        started, release = threading.Event(), threading.Event()

        def ask(refs, prompt, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError('delayed selection was not released')
            return [{'text': '判定: 妥当', 'served_by': 'nous/fallback'}]

        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path),
                                             'FREEAGENT_MODEL_EXCLUDE': ''}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
                 mock.patch.object(S, 'free_model_refs', return_value=old + ['nous/delayed']), \
                 mock.patch.object(S, 'cooling_refs', return_value={}), \
                 mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
                 mock.patch.object(S, 'model_status', return_value={}), mock.patch.object(S, 'ask_many', side_effect=ask):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=old, verifier_snapshot=[])['session_id']
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'delayed',
                                                        'verify': True, 'models': ['nous/delayed']})
                    try:
                        self.assertTrue(started.wait(10))
                        S.thought_merge(sid, {'text': 'X'}, assign_number=True, verifier_models=old,
                                        verifier_snapshot=old, verifier_additions=[old[0]])
                        changed = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                        self.assertNotEqual(old, changed)
                        S.thought_merge(sid, {'text': 'H'}, assign_number=True, verifier_models=old,
                                        verifier_snapshot=changed, verifier_additions=old)
                        self.assertEqual(old, json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models'])
                    finally:
                        release.set()
                    self.assertNotIn('error', pending.result(timeout=10))
                history = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                self.assertEqual(set(old) | {'nous/delayed', 'nous/fallback'}, set(history))

    def test_real_ledger_clock_rollback_keeps_current_selection(self):
        old = [f'nous/old{i}' for i in range(12)]
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp, 'FREEAGENT_THOUGHTS_PATH': str(path)}), \
                 mock.patch.object(S, '_THOUGHTS', {}), mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
                 mock.patch.object(S, 'now_ts', return_value=10000):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                      verifier_models=old, verifier_snapshot=[])['session_id']
                with mock.patch.object(S, 'now_ts', return_value=9999):
                    result = S.thought_merge(sid, {'text': 'next'}, assign_number=True,
                                             verifier_models=old + ['nous/new'], verifier_snapshot=old,
                                             verifier_additions=['nous/new'])
                self.assertNotIn('error', result)
                history = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                self.assertIn('nous/new', history)

    def test_all_relative_orders_keep_latest_and_every_addition(self):
        snapshot = [f'nous/old{i}' for i in range(12)]
        fixed = [f'nous/fixed{i}' for i in range(8)]
        varying = snapshot[:3] + [f'nous/concurrent{i}' for i in range(3)]
        additions = [snapshot[9], varying[3], 'nous/new']
        cases = 0
        for suffix in itertools.permutations(varying):
            ordered = fixed + list(suffix)
            for offset in range(len(ordered)):
                current = ordered[offset:] + ordered[:offset]
                for extra in itertools.permutations(additions):
                    updated, omitted = S._thought_verifier_update(
                        {'verifier_models': current}, snapshot + list(extra), snapshot, list(extra))
                    self.assertEqual(set(current) | set(extra), set(updated), (current, extra, updated))
                    self.assertEqual(0, omitted)
                    self.assertEqual(list(dict.fromkeys([x for x in current if x not in extra] + list(extra))), updated)
                    cases += 1
        self.assertEqual(60480, cases)

    def test_matching_snapshot_rotates_but_keeps_all_current_call_models(self):
        snapshot = [f'nous/old{i}' for i in range(16)]
        additions = [snapshot[0]] + [f'nous/new{i}' for i in range(13)]
        updated, omitted = S._thought_verifier_update(
            {'verifier_models': snapshot, 'verifier_generation': 2}, snapshot + additions, snapshot, additions, 2)
        self.assertEqual(additions, updated)
        self.assertEqual(15, omitted)
        # ABA has the same list but a different generation, so nothing is trimmed.
        updated_aba, omitted_aba = S._thought_verifier_update(
            {'verifier_models': snapshot[:], 'verifier_generation': 4}, snapshot + additions, snapshot, additions, 2)
        self.assertEqual(set(snapshot + additions), set(updated_aba))
        self.assertEqual(0, omitted_aba)

    def test_plain_update_uses_latest_not_stale_supplied_history(self):
        latest = [f'nous/current{i}' for i in range(16)]
        actual, omitted = S._thought_verifier_update(
            {'verifier_models': latest}, ['nous/stale'], None)
        self.assertEqual(latest, actual)
        self.assertEqual(0, omitted)
        self.assertEqual(([], 0), S._thought_verifier_update({}, ['nous/stale'], None))

    def test_cross_process_stale_selection_preserves_latest_history(self):
        old = [f'nous/old{i}' for i in range(12)]
        concurrent = [f'nous/concurrent{i}' for i in range(8)]
        additions = [old[0], 'nous/child']
        ctx = multiprocessing.get_context('spawn')
        ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp,
                    'FREEAGENT_THOUGHTS_PATH': str(path), 'FREEAGENT_THOUGHTS': '1'}), \
                 mock.patch.object(S, '_THOUGHTS', {}), \
                 mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
                 mock.patch.object(S, 'THOUGHTS_ENABLED', True):
                sid = S.thought_merge('', {'text': 'seed'}, assign_number=True, verifier_models=old, verifier_snapshot=[])['session_id']
                child = ctx.Process(target=_stale_selection_child, args=(sid, old, additions, ready, release, results))
                child.start()
                try:
                    self.assertTrue(ready.wait(15), 'child did not read the snapshot')
                    other = S.thought_merge(sid, {'text': 'parent'}, assign_number=True,
                            verifier_models=old + concurrent, verifier_snapshot=old, verifier_additions=concurrent)
                    self.assertNotIn('error', other)
                    latest = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                finally:
                    release.set()
                    child.join(15)
                    if child.is_alive():
                        child.terminate()
                        child.join(5)
                self.assertEqual(0, child.exitcode)
                result = results.get(timeout=5)
                self.assertNotIn('error', result)
                self.assertEqual(0, result['verifier_omitted'])
                row = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
                self.assertEqual(set(latest + additions), set(row['verifier_models']))
                self.assertEqual(3, len(row['steps']))
        results.close()
        results.join_thread()

    def test_reselected_snapshot_model_survives_if_concurrent_update_removed_it(self):
        old = [f'nous/old{i}' for i in range(12)]
        new = [f'nous/new{i}' for i in range(12)]
        started, release = threading.Event(), threading.Event()

        def ask(refs, prompt, **kwargs):
            if refs == ['nous/delayed']:
                started.set()
                if not release.wait(10):
                    raise TimeoutError('delayed verification was not released')
                return [{'text': '判定: 妥当', 'served_by': old[0]}]
            return [{'text': '判定: 妥当', 'served_by': ref} for ref in refs]

        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp,
                    'FREEAGENT_THOUGHTS_PATH': str(path), 'FREEAGENT_SELECT_STRATEGY': 'catalog',
                    'FREEAGENT_MODEL_EXCLUDE': ''}), \
                 mock.patch.object(S, '_THOUGHTS', {}), \
                 mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
                 mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
                 mock.patch.object(S, 'free_model_refs', return_value=old + new + ['nous/delayed']), \
                 mock.patch.object(S, 'cooling_refs', return_value={}), \
                 mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
                 mock.patch.object(S, 'model_status', return_value={}), \
                 mock.patch.object(S, 'ask_many', side_effect=ask):
                sid = S.tool_think({'thought': 'seed'})['session_id']
                S.thought_merge(sid, {'text': 'seed'}, assign_number=True, verifier_models=old, verifier_snapshot=[])
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'delayed',
                                                        'verify': True, 'models': ['nous/delayed'], 'size': 1})
                    try:
                        self.assertTrue(started.wait(10))
                        # Simulate another call's completed 12-model selection under the real ledger lock.
                        merged = S.thought_merge(sid, {'text': 'other'}, assign_number=True,
                                                 verifier_models=old + new, verifier_snapshot=old,
                                                 verifier_snapshot_generation=(S.thought_get(sid) or {})['verifier_generation'])
                        self.assertNotIn('error', merged)
                        latest = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                        self.assertNotIn(old[0], latest)
                    finally:
                        release.set()
                    self.assertNotIn('error', pending.result(timeout=10))
                final = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                self.assertEqual(set(latest) | {old[0], 'nous/delayed'}, set(final))

    def test_interleaved_real_selections_keep_entire_latest_history(self):
        old = [f'nous/h{i}' for i in range(12)]
        delayed = [f'nous/a{i}' for i in range(4)]
        verifiers = [f'nous/bv{i}' for i in range(4)]
        alternatives = [f'nous/ba{i}' for i in range(4)]
        fallback = [f'nous/f{i}' for i in range(8)]
        spare = [f'nous/z{i}' for i in range(4)]
        responses = dict(zip(verifiers + alternatives, fallback))
        started, release = threading.Event(), threading.Event()

        def ask(refs, prompt, **kwargs):
            if refs == delayed:
                started.set()
                if not release.wait(10):
                    raise TimeoutError('delayed verification was not released')
            return [{'text': '判定: 妥当\n代替: 別案',
                     'served_by': responses.get(ref, ref)} for ref in refs]

        with tempfile.TemporaryDirectory(prefix='fa-thought-history-') as tmp:
            path = pathlib.Path(tmp) / 'thoughts.json'
            with mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp,
                    'FREEAGENT_THOUGHTS_PATH': str(path), 'FREEAGENT_SELECT_STRATEGY': 'catalog',
                    'FREEAGENT_MODEL_EXCLUDE': ''}), \
                 mock.patch.object(S, '_THOUGHTS', {}), \
                 mock.patch.object(S, '_THOUGHTS_LOADED', False), \
                 mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
                 mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
                 mock.patch.object(S, 'free_model_refs', return_value=alternatives + verifiers + fallback + old + delayed + spare), \
                 mock.patch.object(S, 'cooling_refs', return_value={}), \
                 mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
                 mock.patch.object(S, 'model_status', return_value={}), \
                 mock.patch.object(S, 'ask_many', side_effect=ask):
                sid = S.tool_think({'thought': 'seed'})['session_id']
                S.thought_merge(sid, {'text': 'seed'}, assign_number=True, verifier_models=old, verifier_snapshot=[])
                def read_history():
                    return json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pending = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'A',
                                                        'verify': True, 'models': delayed})
                    try:
                        self.assertTrue(started.wait(10))
                        b = S.tool_think({'session_id': sid, 'thought': 'B', 'verify': True,
                                          'models': verifiers, 'propose_alternatives': True, 'size': 4})
                        self.assertNotIn('error', b)
                        c = S.tool_think({'session_id': sid, 'thought': 'C', 'verify': True, 'models': old[:4]})
                        self.assertNotIn('error', c)
                        latest = read_history()
                    finally:
                        release.set()
                    a = pending.result(timeout=10)
                    self.assertNotIn('error', a)
                final = read_history()
                self.assertEqual(set(latest) | set(delayed), set(final))
                self.assertFalse(any('履歴を' in note and '省略' in note for note in a['notes']))
                next_step = S.tool_think({'session_id': sid, 'thought': 'next', 'verify': True, 'size': 4})
                self.assertNotIn('error', next_step)
                self.assertEqual(verifiers, next_step['verification']['models'])
                self.assertTrue(set(next_step['verification']['models']).isdisjoint(final))
