"""Final regressions for invalid quota dates and resumed-model exclusions."""
import datetime
import json
import multiprocessing
import os
import pathlib
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock
import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S

def _plain_child(sid, ready, release, results):
    real_get = S.thought_get
    paused = False
    def delayed_get(target):
        nonlocal paused
        row = real_get(target)
        if not paused:
            paused = True
            ready.set()
            if not release.wait(15):
                raise TimeoutError('plain child was not released')
        return row
    try:
        with mock.patch.object(S, 'thought_get', side_effect=delayed_get):
            results.put(S.tool_think({'session_id': sid, 'thought': 'child plain'}))
    except Exception as exc:
        results.put({'error': repr(exc)})


class FinalEdgeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-final-edges-')
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': self.tmp.name,
                                                'FREEAGENT_QUOTA_PATH': str(pathlib.Path(self.tmp.name)/'quota.json'),
                                                'FREEAGENT_MODEL_EXCLUDE': ''})
        self.env.start(); self.addCleanup(self.env.stop)
        self.enabled = mock.patch.object(S, 'QUOTA_ENABLED', True)
        self.enabled.start(); self.addCleanup(self.enabled.stop)
        self.limits = mock.patch.object(S, '_QUOTA_LIMITS', {'nous': {'rpd': 1}})
        self.limits.start(); self.addCleanup(self.limits.stop)
        S._QUOTA.clear(); S._QUOTA_LOADED = False
        self.addCleanup(S._QUOTA.clear)

    def test_bad_existing_day_is_not_reset_or_written(self):
        path = pathlib.Path(os.environ['FREEAGENT_QUOTA_PATH'])
        future = (datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1)).isoformat()
        rows = [{'day_req': -1, 'day_tok': None},
                {'day': None, 'day_req': 1, 'day_tok': 1},
                {'day': 'not-a-date', 'day_req': 1, 'day_tok': 1},
                {'day': future, 'day_req': 1, 'day_tok': 1}]
        for row in rows:
            with self.subTest(row=row):
                path.write_text(json.dumps({'providers': {'nous': row}}), encoding='utf-8')
                before = path.read_bytes(); S._QUOTA.clear(); S._QUOTA_LOADED = False
                reason = S.quota_reserve('nous')
                self.assertIsInstance(reason, str)
                self.assertEqual(before, path.read_bytes())

    def test_resumed_consult_does_not_use_env_excluded_model(self):
        saved = {'question':'Q','models':['nous/org/c:free'],'rounds':[],
                 'mode':'review','draft':'D','basis':'B'}
        with mock.patch.dict(os.environ, {'FREEAGENT_MODEL_EXCLUDE':'nous/org/c'}), \
             mock.patch.object(S,'session_get',return_value=saved), \
             mock.patch.object(S,'ask_many') as ask:
            result = S.tool_consult({'session_id':'sid','main_reply':'continue'})
        self.assertTrue(result.get('error'), result)
        self.assertEqual('sid', result.get('session_id'))
        ask.assert_not_called()

    def _exercise_think(self, alternative):
        refs = ['nous/org/c', 'nous/org/c:free']
        response = {'text': '代替: 別案\n判定: 妥当', 'served_by': 'nous/org/c:free'}
        with mock.patch.object(S,'free_model_refs',return_value=refs), \
             mock.patch.object(S,'cooling_refs',return_value={}), \
             mock.patch.object(S,'provider_auth_blocked',return_value=''), \
             mock.patch.object(S,'model_status',return_value={}), \
             mock.patch.object(S,'_no_models',return_value={'error':'no_models'}), \
             mock.patch.object(S,'ask_many',return_value=[response]) as ask:
            args={'size':1, 'models':['nous/org/c:free']}
            if alternative:
                result = S._think_alternatives(args, 'Q', [], {'n':1,'text':'x'}, {}, exclude=['nous/org/c'])
                self.assertFalse(result.get('error'), result)
                self.assertEqual(['nous/org/c:free'], result['models'])
            else:
                result, _, error = S._think_verify_block(args, 'Q', [], {'n':1,'text':'x'}, {}, ['nous/org/c'], '')
                self.assertIsNone(error)
                self.assertEqual(['nous/org/c:free'], result['models'])
            self.assertEqual(['nous/org/c:free'], ask.call_args.args[0])
            self.assertEqual(['nous/org/c'], ask.call_args.kwargs.get('identity_avoid'))

    def test_think_verify_dynamic_exclusion_is_exact_for_non_hf(self):
        self._exercise_think(False)

    def test_reused_compliance_from_excluded_actual_model_is_rejected(self):
        saved = {}
        refs = ['nous/slot']
        def fake_ask(models, prompt, **kwargs):
            return [{'text': '判定: compliant\n結論: OK\n重要度: nit',
                     'served_by': 'nous/org/c:free'} for _ in models]
        def put(sid, row):
            saved[sid] = row
        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'session_put', side_effect=put), \
             mock.patch.object(S, 'ask_many', side_effect=fake_ask) as ask, \
             mock.patch.object(S, 'quota_report', return_value=None), \
             mock.patch.object(S, 'is_cooling', return_value=False):
            first = S.tool_consult({'question':'Q','mode':'review','draft':'D','basis':'B','main_reply':'same'})
            prior_calls = ask.call_count
            with mock.patch.dict(os.environ, {'FREEAGENT_MODEL_EXCLUDE':'nous/org/c'}):
                second = S.tool_consult({'session_id':first['session_id'],'main_reply':'same'})
        self.assertTrue(second.get('error'), second)
        self.assertEqual(first['session_id'], second.get('session_id'))
        self.assertEqual(prior_calls, ask.call_count)

    def test_think_keeps_current_verifier_until_alternatives_finish(self):
        old = [f'nous/old{i}' for i in range(12)]
        stored = {'steps': [], 'verifier_models': old, 'question':'Q'}
        def choose(size, requested=None, **kwargs):
            excluded = set(kwargs.get('identity_exclude') or [])
            for ref in ('nous/new-verifier', 'nous/new-alternative'):
                if ref not in excluded:
                    return [ref], {}
            return [], {}
        def ask(refs, prompt, **kwargs):
            return [{'text':'判定: 妥当\n代替: 別案', 'served_by':ref} for ref in refs]
        saved = {}
        def merge(sid, step, **kwargs):
            saved.update(kwargs)
            return {'session_id':sid, 'step':step, 'steps_recorded':1}
        with mock.patch.object(S,'thought_get',return_value=stored), \
             mock.patch.object(S,'select_models',side_effect=choose), \
             mock.patch.object(S,'ask_many',side_effect=ask), \
             mock.patch.object(S,'thought_merge',side_effect=merge):
            result = S.tool_think({'session_id':'sid','thought':'x','verify':True,'propose_alternatives':True,'size':1})
        self.assertEqual('nous/new-verifier', result['verification']['answers'][0]['served_by'])
        self.assertEqual('nous/new-alternative', result['alternatives']['models'][0])
        self.assertIn('nous/new-verifier', saved['verifier_models'])
        self.assertIn('nous/new-alternative', saved['verifier_models'])

    def test_thought_merge_retains_newest_verifiers_with_bounded_history(self):
        old = [f'nous/old{i}' for i in range(12)]
        result = S._thoughts_merge_row({'updated_at':1,'verifier_models':old},
                                       {'updated_at':2,'verifier_models':old[1:]+['nous/new']})
        self.assertIn('nous/new', result['verifier_models'])
        self.assertLessEqual(len(result['verifier_models']), 12)

    def test_one_large_think_call_retains_all_actual_models_for_next_call(self):
        old = [f'nous/old{i}' for i in range(12)]
        verify = [f'nous/v{i}' for i in range(4)]
        alternative = [f'nous/a{i}' for i in range(4)]
        expected = set(verify + alternative + [f'{x}:fallback' for x in verify + alternative])
        stored = {'steps': [], 'verifier_models': old, 'question':'Q', 'updated_at':1}
        saved = {}
        def select(size, requested=None, **kwargs):
            return (verify if not set(verify).intersection(kwargs.get('identity_exclude') or []) else alternative), {}
        def ask(refs, prompt, **kwargs):
            return [{'text':'判定: 妥当\n代替: 別案', 'served_by':f'{ref}:fallback'} for ref in refs]
        def merge(sid, step, **kwargs):
            saved.update(kwargs)
            return {'session_id':sid, 'step':step, 'steps_recorded':1}
        with mock.patch.object(S,'thought_get',return_value=stored), \
             mock.patch.object(S,'select_models',side_effect=select), \
             mock.patch.object(S,'ask_many',side_effect=ask), \
             mock.patch.object(S,'thought_merge',side_effect=merge):
            S.tool_think({'session_id':'sid','thought':'x','verify':True,'propose_alternatives':True,'size':4})
        self.assertTrue(expected.issubset(saved['verifier_models']))
        merged = S._thoughts_merge_row(stored, {'updated_at':2, 'verifier_models':saved['verifier_models']})
        self.assertTrue(expected.issubset(merged['verifier_models']))

    def test_plain_step_preserves_large_history_until_next_real_selection(self):
        verify = [f'nous/v{i}' for i in range(4)]
        alternatives = [f'nous/a{i}' for i in range(4)]
        fallback = [f'nous/fallback-{ref.split("/")[1]}' for ref in verify + alternatives]
        spare = [f'nous/z{i}' for i in range(4)]
        catalog = verify + alternatives + fallback + spare
        responses = dict(zip(verify + alternatives, fallback))
        ledger_path = pathlib.Path(self.tmp.name) / 'thoughts.json'

        def ask(refs, prompt, **kwargs):
            return [{'text': '判定: 妥当\n代替: 別案',
                     'served_by': responses.get(ref, ref)} for ref in refs]

        with mock.patch.dict(os.environ, {'FREEAGENT_THOUGHTS_PATH': str(ledger_path),
                                          'FREEAGENT_SELECT_STRATEGY': 'catalog'}), \
             mock.patch.object(S, '_THOUGHTS', {}), \
             mock.patch.object(S, '_THOUGHTS_LOADED', False), \
             mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
             mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
             mock.patch.object(S, 'free_model_refs', return_value=catalog), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            first = S.tool_think({'thought': 'first', 'verify': True,
                                  'propose_alternatives': True, 'size': 4})
            self.assertNotIn('error', first)
            self.assertEqual(verify, first['verification']['models'])
            self.assertEqual(alternatives, first['alternatives']['models'])
            sid = first['session_id']
            first_history = json.loads(ledger_path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
            self.assertEqual(set(verify + alternatives + fallback), set(first_history))
            self.assertEqual(16, len(first_history))

            ordinary = S.tool_think({'session_id': sid, 'thought': 'ordinary'})
            self.assertNotIn('error', ordinary)
            ordinary_history = json.loads(ledger_path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
            next_result = S.tool_think({'session_id': sid, 'thought': 'next', 'verify': True, 'size': 4})
            self.assertNotIn('error', next_result)
            self.assertEqual(spare, next_result['verification']['models'])
            self.assertEqual(first_history, ordinary_history)
            self.assertFalse(any('履歴を' in note and '省略' in note for note in ordinary['notes']))
            next_history = json.loads(ledger_path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
            self.assertEqual(12, len(next_history))
            self.assertTrue(any('履歴を' in note and '省略' in note for note in next_result['notes']))

    def test_concurrent_plain_step_cannot_erase_selected_models(self):
        old = [f'nous/old{i}' for i in range(12)]
        verify = [f'nous/v{i}' for i in range(4)]
        alternatives = [f'nous/a{i}' for i in range(4)]
        fallback = [f'nous/f{i}' for i in range(8)]
        spare = [f'nous/z{i}' for i in range(4)]
        responses = dict(zip(verify + alternatives, fallback))
        path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        both_read, release = threading.Barrier(3, timeout=10), threading.Event()
        worker_state = threading.local()
        real_get = S.thought_get

        def delayed_get(sid):
            row = real_get(sid)
            if threading.current_thread().name.startswith('plain') and not getattr(worker_state, 'paused', False):
                worker_state.paused = True
                both_read.wait()
                if not release.wait(10):
                    raise TimeoutError('plain step was not released')
            return row

        def ask(refs, prompt, **kwargs):
            return [{'text': '判定: 妥当\n代替: 別案',
                     'served_by': responses.get(ref, ref)} for ref in refs]

        with mock.patch.dict(os.environ, {'FREEAGENT_THOUGHTS_PATH': str(path),
                                          'FREEAGENT_SELECT_STRATEGY': 'catalog'}), \
             mock.patch.object(S, '_THOUGHTS', {}), \
             mock.patch.object(S, '_THOUGHTS_LOADED', False), \
             mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
             mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
             mock.patch.object(S, 'free_model_refs', return_value=verify + alternatives + fallback + spare), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            sid = S.tool_think({'thought': 'seed'})['session_id']
            S.thought_merge(sid, {'n': 1, 'text': 'seed'}, verifier_models=old, verifier_snapshot=[])
            with mock.patch.object(S, 'thought_get', side_effect=delayed_get), \
                 ThreadPoolExecutor(max_workers=2, thread_name_prefix='plain') as pool:
                futures = [pool.submit(S.tool_think, {'session_id': sid, 'thought': f'plain-{i}'})
                           for i in range(2)]
                try:
                    both_read.wait()
                    selected = S.tool_think({'session_id': sid, 'thought': 'select', 'verify': True,
                                             'propose_alternatives': True, 'size': 4})
                    self.assertNotIn('error', selected)
                    expected = set(verify + alternatives + fallback)
                    history = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                    self.assertEqual(expected, set(history))
                finally:
                    release.set()
                for future in futures:
                    self.assertNotIn('error', future.result(timeout=10))
            ledger = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]
            self.assertEqual(expected, set(ledger['verifier_models']))
            self.assertEqual(4, len(ledger['steps']))
            next_step = S.tool_think({'session_id': sid, 'thought': 'next', 'verify': True, 'size': 4})
            self.assertEqual(spare, next_step['verification']['models'])

    def test_concurrent_verifications_keep_both_histories(self):
        first_refs = [f'nous/p{i}' for i in range(4)]
        second_refs = [f'nous/q{i}' for i in range(4)]
        fallback = [f'nous/f{i}' for i in range(8)]
        spare = [f'nous/z{i}' for i in range(4)]
        expected = set(first_refs + second_refs + fallback)
        answers = dict(zip(first_refs + second_refs, fallback))
        path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        both_read = threading.Barrier(2, timeout=10)
        release = threading.Event()

        def ask(refs, prompt, **kwargs):
            if refs in (first_refs, second_refs):
                both_read.wait()
                if refs == second_refs and not release.wait(10):
                    raise TimeoutError('second verification was not released')
            return [{'text': '判定: 妥当', 'served_by': answers.get(ref, ref)} for ref in refs]

        with mock.patch.dict(os.environ, {'FREEAGENT_THOUGHTS_PATH': str(path)}), \
             mock.patch.object(S, '_THOUGHTS', {}), \
             mock.patch.object(S, '_THOUGHTS_LOADED', False), \
             mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
             mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
             mock.patch.object(S, 'free_model_refs', return_value=first_refs + second_refs + fallback + spare), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            sid = S.tool_think({'thought': 'seed'})['session_id']
            S.thought_merge(sid, {'n': 1, 'text': 'seed'},
                            verifier_models=[f'nous/old{i}' for i in range(12)], verifier_snapshot=[])
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'first',
                                                'verify': True, 'models': first_refs})
                b = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'second',
                                                'verify': True, 'models': second_refs})
                try:
                    self.assertNotIn('error', a.result(timeout=10))
                    latest = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
                finally:
                    release.set()
                self.assertNotIn('error', b.result(timeout=10))
            history = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
            self.assertEqual(set(latest) | set(second_refs + fallback[4:]), set(history))
            self.assertTrue(expected.issubset(history), (expected, history))
            next_step = S.tool_think({'session_id': sid, 'thought': 'next',
                                      'propose_alternatives': True, 'size': 4})
            self.assertEqual(set(spare), set(next_step['alternatives']['models']))

    def test_cross_process_plain_preserves_concurrent_selection(self):
        verify = [f'nous/v{i}' for i in range(4)]
        alternatives = [f'nous/a{i}' for i in range(4)]
        fallback = [f'nous/f{i}' for i in range(8)]
        spare = [f'nous/z{i}' for i in range(4)]
        responses = dict(zip(verify + alternatives, fallback))
        path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        ctx = multiprocessing.get_context('spawn')
        ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        def ask(refs, prompt, **kwargs):
            return [{'text': '判定: 妥当\n代替: 別案', 'served_by': responses.get(ref, ref)} for ref in refs]
        with mock.patch.dict(os.environ, {'FREEAGENT_THOUGHTS_PATH': str(path),
                                          'FREEAGENT_THOUGHTS': '1',
                                          'FREEAGENT_SELECT_STRATEGY': 'catalog'}), \
             mock.patch.object(S, '_THOUGHTS', {}), \
             mock.patch.object(S, '_THOUGHTS_LOADED', False), \
             mock.patch.object(S, '_THOUGHTS_MTIME', 0.0), \
             mock.patch.object(S, 'THOUGHTS_ENABLED', True), \
             mock.patch.object(S, 'free_model_refs', return_value=verify + alternatives + fallback + spare), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            sid = S.tool_think({'thought': 'seed'})['session_id']
            S.thought_merge(sid, {'n': 1, 'text': 'seed'},
                            verifier_models=[f'nous/old{i}' for i in range(12)], verifier_snapshot=[])
            child = ctx.Process(target=_plain_child, args=(sid, ready, release, results))
            child.start()
            try:
                self.assertTrue(ready.wait(15), 'child did not read the ledger')
                result = S.tool_think({'session_id': sid, 'thought': 'selection', 'verify': True,
                                       'propose_alternatives': True, 'size': 4})
                self.assertNotIn('error', result)
            finally:
                release.set()
                child.join(15)
                if child.is_alive():
                    child.terminate()
                    child.join(5)
            self.assertEqual(0, child.exitcode)
            self.assertNotIn('error', results.get(timeout=5))
            history = json.loads(path.read_text(encoding='utf-8'))['chains'][sid]['verifier_models']
            self.assertEqual(set(verify + alternatives + fallback), set(history))
            next_step = S.tool_think({'session_id': sid, 'thought': 'next', 'verify': True, 'size': 4})
            self.assertEqual(spare, next_step['verification']['models'])
            results.close()
            results.join_thread()

    def test_disabled_thoughts_do_not_write_and_merge_history_in_memory(self):
        path = pathlib.Path(self.tmp.name) / 'disabled' / 'thoughts.json'
        old = [f'nous/old{i}' for i in range(12)]
        first = [f'nous/a{i}' for i in range(8)]
        second = [f'nous/b{i}' for i in range(8)]
        with mock.patch.dict(os.environ, {'FREEAGENT_THOUGHTS_PATH': str(path)}), \
             mock.patch.object(S, '_THOUGHTS', {}), \
             mock.patch.object(S, 'THOUGHTS_ENABLED', False):
            self.assertFalse(S.tool_think({'thought': 'x'})['enabled'])
            sid = S.thought_merge('', {'text': 'seed'}, assign_number=True,
                                  verifier_models=old, verifier_snapshot=[])['session_id']
            S.thought_merge(sid, {'text': 'a'}, assign_number=True,
                            verifier_models=old + first, verifier_snapshot=old)
            latest = (S.thought_get(sid) or {})['verifier_models']
            S.thought_merge(sid, {'text': 'b'}, assign_number=True,
                            verifier_models=old + second, verifier_snapshot=old)
            S.thought_merge(sid, {'text': 'plain'}, assign_number=True)
            row = S.thought_get(sid)
            self.assertIsNotNone(row)
            self.assertEqual(set(latest + second), set((row or {})['verifier_models']))
            self.assertFalse(path.parent.exists())

    def test_think_alternative_dynamic_exclusion_is_exact_for_non_hf(self):
        self._exercise_think(True)
