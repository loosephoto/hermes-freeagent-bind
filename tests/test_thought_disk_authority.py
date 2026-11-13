"""Real-ledger regressions for cross-process deletion and capacity pruning."""
import concurrent.futures
import threading
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class ThoughtDiskAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-thought-disk-')
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        patches = [mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': self.tmp.name,
                    'FREEAGENT_THOUGHTS_PATH': str(self.path), 'FREEAGENT_THOUGHTS': '1'}),
                   mock.patch.object(S, '_THOUGHTS', {}),
                   mock.patch.object(S, '_THOUGHTS_LOADED', False),
                   mock.patch.object(S, '_THOUGHTS_MTIME', 0.0),
                   mock.patch.object(S, 'THOUGHTS_ENABLED', True)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def _child_env(self):
        env = os.environ.copy()
        env['PYTHONPATH'] = _FA_SRC + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        return env

    def rows(self):
        return json.loads(self.path.read_text(encoding='utf-8'))['chains']

    def test_other_process_deletion_does_not_resurrect_cached_sid(self):
        seed = S.thought_merge('', {'text': 'old'}, assign_number=True,
                               verifier_models=['old'], verifier_snapshot=[])
        sid = seed['session_id']
        self.assertIsNotNone(S.thought_get(sid))
        child = subprocess.run([sys.executable, '-c',
            'import sys; from freeagent_bind import server as S; S.thought_drop(sys.argv[1])', sid],
            env=self._child_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertNotIn(sid, self.rows())
        result = S.thought_merge(sid, {'text': 'fresh'}, assign_number=True,
                                 verifier_models=['new'], verifier_snapshot=[])
        self.assertNotIn('error', result)
        self.assertNotEqual(sid, result['session_id'])
        rows = self.rows()
        self.assertNotIn(sid, rows)
        self.assertEqual(['fresh'], [step['text'] for step in rows[result['session_id']]['steps']])
        self.assertEqual(rows, S._THOUGHTS)

    def test_deletion_during_verification_does_not_save_stale_context(self):
        sid = S.tool_think({'thought': 'old', 'question': 'old question'})['session_id']
        started, release = threading.Event(), threading.Event()

        def ask(refs, prompt, **kwargs):
            started.set()
            if not release.wait(10):
                raise TimeoutError('test did not release verification')
            return [{'text': '判定: 妥当', 'served_by': refs[0]}]

        with mock.patch.object(S, 'free_model_refs', return_value=['nous/verifier']), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'next',
                                                      'verify': True, 'models': ['nous/verifier']})
                try:
                    self.assertTrue(started.wait(10))
                    child = subprocess.run([sys.executable, '-c',
                        'import sys; from freeagent_bind import server as S; S.thought_drop(sys.argv[1])', sid],
                        env=self._child_env(), capture_output=True, text=True, timeout=30)
                    self.assertEqual(0, child.returncode, child.stderr)
                finally:
                    release.set()
                result = pending.result(timeout=10)
        self.assertIn('error', result)
        self.assertEqual({}, self.rows())

    def test_read_after_external_delete_and_unchanged_mtime(self):
        sid = S.thought_merge('', {'text': 'old'}, assign_number=True, card='card')['session_id']
        child = subprocess.run([sys.executable, '-c',
            'import sys; from freeagent_bind import server as S; S.thought_drop(sys.argv[1])', sid],
            env=self._child_env(), capture_output=True, text=True, timeout=30)
        self.assertEqual(0, child.returncode, child.stderr)
        with mock.patch.object(S, '_file_mtime', return_value=S._THOUGHTS_MTIME):
            self.assertIsNone(S.thought_get(sid))
            self.assertEqual('', S.thought_find_by_card('card'))
        self.assertEqual({}, S._THOUGHTS)

    def test_expired_disk_row_is_a_new_id(self):
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            sid = S.thought_merge('', {'text': 'old'}, assign_number=True)['session_id']
        with mock.patch.object(S, 'now_ts', return_value=10000.0 + S.THOUGHT_TTL_S + 1):
            result = S.thought_merge(sid, {'text': 'fresh'}, assign_number=True)
        self.assertNotEqual(sid, result['session_id'])
        self.assertNotIn(sid, self.rows())
        self.assertEqual(self.rows(), S._THOUGHTS)

    def test_failures_leave_memory_and_disk_unchanged(self):
        sid = S.thought_merge('', {'text': 'old'}, assign_number=True,
                              verifier_snapshot=[], verifier_additions=['old'])['session_id']
        before = self.path.read_bytes()
        memory = json.loads(json.dumps(S._THOUGHTS))
        for replacement in (mock.patch.object(S, '_atomic_write', return_value=False),
                            mock.patch.object(S, '_file_lock', return_value=__import__('contextlib').nullcontext(False)),
                            mock.patch.object(S, '_thoughts_disk_chains', side_effect=OSError('read failed'))):
            with replacement:
                result = S.thought_merge(sid, {'text': 'new'}, assign_number=True,
                                         verifier_snapshot=['old'], verifier_additions=['new'])
            self.assertIn('error', result)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(memory, S._THOUGHTS)

    def test_capacity_evicts_old_rows_and_memory_matches_disk(self):
        with mock.patch.object(S, 'THOUGHT_MAX', 1):
            with mock.patch.object(S, 'now_ts', return_value=1000.0):
                first = S.thought_merge('', {'text': 'first'}, assign_number=True)['session_id']
            with mock.patch.object(S, 'now_ts', return_value=1001.0):
                second = S.thought_merge('', {'text': 'second'}, assign_number=True)['session_id']
            self.assertNotEqual(first, second)
            self.assertEqual([second], list(self.rows()))
            self.assertEqual(self.rows(), S._THOUGHTS)
            self.assertIsNone(S.thought_get(first))

    def test_drop_failure_and_read_failure_do_not_change_state(self):
        sid = S.thought_merge('', {'text': 'old'}, assign_number=True)['session_id']
        before = self.path.read_bytes()
        memory = json.loads(json.dumps(S._THOUGHTS))
        for replacement in (mock.patch.object(S, '_atomic_write', return_value=False),
                            mock.patch.object(S, '_file_lock', return_value=__import__('contextlib').nullcontext(False)),
                            mock.patch.object(S, '_thoughts_disk_chains', side_effect=OSError('read failed'))):
            with replacement, self.assertRaises(OSError):
                S.thought_drop(sid)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(memory, S._THOUGHTS)
        for replacement in (mock.patch.object(S, '_file_lock', return_value=__import__('contextlib').nullcontext(False)),
                            mock.patch.object(S, '_thoughts_disk_chains', side_effect=OSError('read failed'))):
            with replacement, self.assertRaises(OSError):
                S.thought_get(sid)
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(memory, S._THOUGHTS)

    def test_capacity_never_reports_success_for_pruned_updated_row(self):
        with mock.patch.object(S, 'now_ts', return_value=1000.0), mock.patch.object(S, 'THOUGHT_MAX', 1):
            rows = {'low': {'updated_at': 1100.0, 'steps': [{'n': 1, 'text': 'old'}]},
                    'high': {'updated_at': 3000.0, 'steps': [{'n': 1, 'text': 'other'}]}}
            self.path.write_text(json.dumps({'version': 1, 'chains': rows}), encoding='utf-8')
            before = self.path.read_bytes()
            memory_before = dict(S._THOUGHTS)
            result = S.thought_merge('low', {'text': 'new'}, assign_number=True)
            if result.get('error'):
                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual(memory_before, S._THOUGHTS)
            else:
                stored = self.rows()
                sid = result['session_id']
                self.assertIn(sid, stored)
                self.assertEqual('new', stored[sid]['steps'][-1]['text'])
                self.assertEqual(stored, S._THOUGHTS)
                self.assertLessEqual(len(stored), 1)
