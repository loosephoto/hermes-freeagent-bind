"""Regressions on the actual isolated thought ledger."""
import concurrent.futures
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class ThoughtReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-review-')
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        for patch in (mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': self.tmp.name,
                       'FREEAGENT_THOUGHTS_PATH': str(self.path)}),
                      mock.patch.object(S, 'THOUGHTS_ENABLED', True),
                      mock.patch.object(S, '_THOUGHTS', {}),
                      mock.patch.object(S, '_THOUGHTS_CLOCK', {}),
                      mock.patch.object(S, '_THOUGHTS_LOADED', False)):
            patch.start()
            self.addCleanup(patch.stop)

    def _child_env(self):
        env = os.environ.copy()
        env['PYTHONPATH'] = _FA_SRC + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
        return env

    def rows(self):
        return json.loads(self.path.read_text(encoding='utf-8'))['chains']

    def test_failed_write_does_not_advance_clock_or_prune(self):
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            sid = S.thought_merge('', {'text': 'old'}, assign_number=True)['session_id']
        before = self.path.read_bytes()
        clock = dict(S._THOUGHTS_CLOCK)
        memory = json.loads(json.dumps(S._THOUGHTS))
        with mock.patch.object(S, 'now_ts', return_value=10000.0 + S.THOUGHT_TTL_S + 1), \
             mock.patch.object(S, '_atomic_write', return_value=False):
            self.assertIn('error', S.thought_merge('', {'text': 'new'}, assign_number=True))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(clock, S._THOUGHTS_CLOCK)
        self.assertEqual(memory, S._THOUGHTS)
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            self.assertIsNotNone(S.thought_get(sid))

    def test_expired_observation_stays_expired_after_clock_rollback(self):
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            sid = S.thought_merge('', {'text': 'old'}, assign_number=True, card='card')['session_id']
        before = self.path.read_bytes()
        with mock.patch.object(S, 'now_ts', return_value=10000.0 + S.THOUGHT_TTL_S + 1):
            self.assertIsNone(S.thought_get(sid))
            self.assertEqual('', S.thought_find_by_card('card'))
        self.assertEqual(before, self.path.read_bytes())
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            self.assertIsNone(S.thought_get(sid))
            self.assertEqual('', S.thought_find_by_card('card'))
            denied = S.thought_merge(sid, {'text': 'stale'}, assign_number=True, require_existing=True)
            self.assertIn('error', denied)
            self.assertEqual(before, self.path.read_bytes())
            fresh = S.thought_merge(sid, {'text': 'fresh'}, assign_number=True)
            self.assertNotEqual(sid, fresh['session_id'])
            self.assertNotIn(sid, self.rows())
            self.assertIsNotNone(S.thought_get(fresh['session_id']))

    def test_absence_read_racing_external_creation_sees_it_next_read(self):
        stat = os.stat
        child_sid = []
        def stat_with_concurrent_create(path, *args, **kwargs):
            try:
                return stat(path, *args, **kwargs)
            except FileNotFoundError:
                if str(path) != str(self.path) or child_sid:
                    raise
                child = subprocess.run([sys.executable, '-c',
                    "from freeagent_bind import server as S; "
                    "print(S.thought_merge('', {'text': 'external'}, assign_number=True, card='card')['session_id'])"],
                    env=self._child_env(), capture_output=True, text=True, timeout=30)
                self.assertEqual(0, child.returncode, child.stderr)
                child_sid.append(child.stdout.strip())
                raise
        with mock.patch.object(S.os, 'stat', side_effect=stat_with_concurrent_create):
            self.assertEqual('', S.thought_find_by_card('card'))
        self.assertEqual(child_sid[0], S.thought_find_by_card('card'))
        self.assertEqual('external', self.rows()[child_sid[0]]['steps'][0]['text'])

    def test_clock_rollback_limit_across_restart_and_prune(self):
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            sid = S.thought_merge('', {'text': 'old'}, assign_number=True)['session_id']
        with mock.patch.object(S, 'now_ts', return_value=10000.0 + S.THOUGHT_TTL_S + 1):
            self.assertIsNone(S.thought_get(sid))
        script = ("import sys; from freeagent_bind import server as S; "
                  "S.now_ts=lambda:10000.0; print('visible' if S.thought_get(sys.argv[1]) else 'expired')")
        def child_read():
            child = subprocess.run([sys.executable, '-c', script, sid], env=self._child_env(),
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(0, child.returncode, child.stderr)
            return child.stdout.strip()
        # Read-only expiry has no cross-process memory.
        self.assertEqual('visible', child_read())
        with mock.patch.object(S, 'now_ts', return_value=10000.0):
            self.assertNotIn('error', S.thought_merge('', {'text': 'other'}, assign_number=True))
        self.assertNotIn(sid, self.rows())
        self.assertEqual('expired', child_read())

    def test_initial_card_offline_does_not_create_a_lock_file(self):
        with mock.patch.object(S, 'free_model_refs', return_value=['nous/v']), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', return_value=[{'error': 'connection refused'}]):
            result = S.tool_think({'card': 'card-one', 'thought': 'offline', 'verify': True,
                                   'models': ['nous/v']})
        self.assertIn('error', result)
        self.assertEqual([], list(self.path.parent.iterdir()))

    def test_corrupt_verifier_history_fails_under_lock_without_writes(self):
        sid = S.thought_merge('', {'text': 'seed'}, assign_number=True)['session_id']
        for bad in ('nous/old', None, ['ok', 123], {'nous/old': True}):
            with self.subTest(bad=bad):
                doc = json.loads(self.path.read_text(encoding='utf-8'))
                doc['chains'][sid]['verifier_models'] = bad
                self.path.write_text(json.dumps(doc), encoding='utf-8')
                before = self.path.read_bytes()
                memory = json.loads(json.dumps(S._THOUGHTS))
                result = S.thought_merge(sid, {'text': 'next'}, assign_number=True,
                                         verifier_snapshot=[], verifier_additions=['nous/new'])
                self.assertIn('error', result)
                self.assertEqual(before, self.path.read_bytes())
                self.assertEqual(memory, S._THOUGHTS)

    def test_implicit_old_question_never_overwrites_explicit_concurrent_update(self):
        entered, release = threading.Event(), threading.Event()
        prompts = []
        def ask(refs, prompt, **kwargs):
            prompts.append(prompt)
            entered.set()
            if not release.wait(10):
                raise TimeoutError('verification was not released')
            return [{'text': 'verdict: valid', 'served_by': refs[0]}]
        with mock.patch.object(S, 'free_model_refs', return_value=['nous/v']), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            sid = S.tool_think({'thought': 'seed', 'question': 'old'})['session_id']
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                delayed = pool.submit(S.tool_think, {'session_id': sid, 'thought': 'delayed',
                                                     'verify': True, 'models': ['nous/v']})
                try:
                    self.assertTrue(entered.wait(10))
                    changed = S.tool_think({'session_id': sid, 'thought': 'new step', 'question': 'new'})
                    self.assertNotIn('error', changed)
                    self.assertEqual('new', self.rows()[sid]['question'])
                finally:
                    release.set()
                self.assertNotIn('error', delayed.result(timeout=10))
        self.assertTrue(any('old' in prompt for prompt in prompts))
        self.assertEqual('new', self.rows()[sid]['question'])
        self.assertEqual(3, len(self.rows()[sid]['steps']))
