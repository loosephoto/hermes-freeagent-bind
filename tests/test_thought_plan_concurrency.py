"""Plan references must still identify the validated item at commit time."""
import json
import multiprocessing
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


def _delayed_think(sid, ready, release, results, args):
    def verify(*_args):
        ready.set()
        if not release.wait(15):
            raise RuntimeError('verification barrier timed out')
        return {'answered': 1, 'failed': 0, 'objections': [], 'models': [], 'answers': [],
                'verdicts': {'要修正': 0, '根拠不足': 0}}, [], None
    try:
        with mock.patch.object(S, '_think_verify_block', side_effect=verify):
            results.put(S.tool_think(dict(args, session_id=sid, verify=True)))
    except Exception as exc:
        results.put({'exception': repr(exc)})


class ThoughtPlanConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-plan-race-')
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / 'thoughts.json'
        env = mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': self.tmp.name,
                                           'FREEAGENT_THOUGHTS_PATH': str(self.path)})
        env.start()
        self.addCleanup(env.stop)
        self.thoughts = mock.patch.object(S, '_THOUGHTS', {})
        self.thoughts.start()
        self.addCleanup(self.thoughts.stop)
        loaded = mock.patch.object(S, '_THOUGHTS_LOADED', False)
        loaded.start()
        self.addCleanup(loaded.stop)
        enabled = mock.patch.object(S, 'THOUGHTS_ENABLED', True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def row(self, sid):
        return json.loads(self.path.read_text(encoding='utf-8'))['chains'][sid]

    def test_overlapping_plan_change_rejects_stale_resolved_id(self):
        sid = S.tool_think({'thought': 'seed'})['session_id']
        ready, release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=lambda: _delayed_think(
            sid, ready, release, result_proxy(result),
            {'thought': 'A step', 'plan': ['A'], 'subgoal': 1, 'subgoal_done': True}))
        worker.start()
        try:
            self.assertTrue(ready.wait(15), 'verification was not reached')
            other = S.tool_think({'session_id': sid, 'thought': 'B step', 'plan': ['B']})
            self.assertNotIn('error', other)
            before = self.path.read_bytes()
            memory_before = json.dumps(S._THOUGHTS, sort_keys=True)
        finally:
            release.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, len(result))
        self.assertIn('error', result[0], result[0])
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(memory_before, json.dumps(S._THOUGHTS, sort_keys=True))
        row = self.row(sid)
        self.assertEqual(['B'], [p['text'] for p in row['plan']])
        self.assertNotIn('A step', [s['text'] for s in row['steps']])

    def test_normal_id_position_and_retired_references(self):
        first = S.tool_think({'thought': 'seed', 'plan': ['A', 'B', 'C']})
        sid = first['session_id']
        second = S.tool_think({'session_id': sid, 'thought': 'replace', 'plan': ['B', 'C']})
        self.assertNotIn('error', second)
        position = S.tool_think({'session_id': sid, 'thought': 'position', 'subgoal': 1, 'subgoal_done': True})
        self.assertEqual(2, position['subgoal'])
        explicit = S.tool_think({'session_id': sid, 'thought': 'explicit', 'subgoal': 3, 'subgoal_done': True})
        self.assertEqual(3, explicit['subgoal'])
        row = self.row(sid)
        self.assertTrue(all(p['done_at'] for p in row['plan']))
        self.assertIsNone(row['plan_retired'][0]['done_at'])
        S.tool_think({'session_id': sid, 'thought': 'retire B', 'plan': ['C']})
        prior_done = next(p['done_at'] for p in self.row(sid)['plan_retired'] if p['id'] == 2)
        retired = S.tool_think({'session_id': sid, 'thought': 'retired', 'subgoal': 2, 'subgoal_done': True})
        self.assertEqual(2, retired['subgoal'])
        self.assertNotIn('error', retired)
        row = self.row(sid)
        self.assertEqual('B', next(p['text'] for p in row['plan_retired'] if p['id'] == 2))
        self.assertEqual(prior_done, next(p['done_at'] for p in row['plan_retired'] if p['id'] == 2))

    def test_existing_reference_changes_or_retires_while_verifying(self):
        for replacement, raw, done in ((['B'], 1, True), (['B'], 1, False), (['A'], 2, True)):
            with self.subTest(replacement=replacement, raw=raw, done=done):
                sid = S.tool_think({'thought': 'seed', 'plan': ['A', 'B']})['session_id']
                ready, release = threading.Event(), threading.Event()
                result = []
                worker = threading.Thread(target=_delayed_think, args=(
                    sid, ready, release, result_proxy(result),
                    {'thought': 'stale', 'subgoal': raw, 'subgoal_done': done}))
                worker.start()
                try:
                    self.assertTrue(ready.wait(15))
                    other = S.tool_think({'session_id': sid, 'thought': 'replace', 'plan': replacement})
                    self.assertNotIn('error', other)
                    before = self.path.read_bytes()
                finally:
                    release.set()
                    worker.join(15)
                self.assertFalse(worker.is_alive())
                self.assertIn('error', result[0], result[0])
                self.assertEqual(before, self.path.read_bytes())

    def test_unrelated_concurrent_step_allows_unchanged_reference(self):
        sid = S.tool_think({'thought': 'seed', 'plan': ['A']})['session_id']
        ready, release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=_delayed_think, args=(
            sid, ready, release, result_proxy(result),
            {'thought': 'A step', 'subgoal': 1, 'subgoal_done': True}))
        worker.start()
        try:
            self.assertTrue(ready.wait(15))
            self.assertNotIn('error', S.tool_think({'session_id': sid, 'thought': 'other'}))
        finally:
            release.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertNotIn('error', result[0], result[0])
        self.assertEqual(1, result[0]['subgoal'])
        row = self.row(sid)
        self.assertEqual(result[0]['step'], row['plan'][0]['done_at'])
        self.assertEqual(['seed', 'other', 'A step'], [s['text'] for s in row['steps']])

    def test_nonverified_call_also_rechecks_at_save(self):
        sid = S.tool_think({'thought': 'seed', 'plan': ['A']})['session_id']
        ready, release = threading.Event(), threading.Event()
        result = []
        merge = S.thought_merge

        def delayed_merge(sid_arg, step, **kwargs):
            if step['text'] == 'stale':
                ready.set()
                if not release.wait(15):
                    raise RuntimeError('merge barrier timed out')
            return merge(sid_arg, step, **kwargs)

        def run():
            result.append(S.tool_think({'session_id': sid, 'thought': 'stale', 'subgoal': 1}))

        with mock.patch.object(S, 'thought_merge', side_effect=delayed_merge):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(ready.wait(15))
                other = S.tool_think({'session_id': sid, 'thought': 'replace', 'plan': ['B']})
                self.assertNotIn('error', other)
                before = self.path.read_bytes()
            finally:
                release.set()
                worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertIn('error', result[0], result[0])
        self.assertEqual(before, self.path.read_bytes())

    def test_concurrent_plan_change_keeps_same_reference_valid(self):
        sid = S.tool_think({'thought': 'seed', 'plan': ['A', 'B']})['session_id']
        ready, release = threading.Event(), threading.Event()
        result = []
        worker = threading.Thread(target=_delayed_think, args=(
            sid, ready, release, result_proxy(result),
            {'thought': 'A step', 'subgoal': 1, 'subgoal_done': True}))
        worker.start()
        try:
            self.assertTrue(ready.wait(15))
            self.assertNotIn('error', S.tool_think({'session_id': sid, 'thought': 'replace',
                                                    'plan': ['A', 'C']}))
        finally:
            release.set()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertNotIn('error', result[0], result[0])
        row = self.row(sid)
        self.assertEqual(['A', 'C'], [p['text'] for p in row['plan']])
        self.assertEqual(result[0]['step'], row['plan'][0]['done_at'])

    def test_two_processes_reject_stale_new_plan_reference(self):
        sid = S.tool_think({'thought': 'seed'})['session_id']
        ctx = multiprocessing.get_context('spawn')
        ready, release, results = ctx.Event(), ctx.Event(), ctx.Queue()
        process = ctx.Process(target=_delayed_think, args=(
            sid, ready, release, results,
            {'thought': 'A step', 'plan': ['A'], 'subgoal': 1, 'subgoal_done': True}))
        process.start()
        try:
            self.assertTrue(ready.wait(15), 'child did not reach verification')
            self.assertNotIn('error', S.tool_think({'session_id': sid, 'thought': 'B step', 'plan': ['B']}))
            before = self.path.read_bytes()
        finally:
            release.set()
            process.join(20)
            if process.is_alive():
                process.terminate()
                process.join(5)
        self.assertEqual(0, process.exitcode)
        result = results.get(timeout=5)
        self.assertIn('error', result, result)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(['B'], [p['text'] for p in self.row(sid)['plan']])
        results.close()


class result_proxy:
    def __init__(self, results):
        self.results = results

    def put(self, value):
        self.results.append(value)


if __name__ == '__main__':
    unittest.main()
