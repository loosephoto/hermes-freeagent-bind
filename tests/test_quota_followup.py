import os
import tempfile
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class QuotaFollowup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='fa-quota-followup-')
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': tmp.name,
                                          'FREEAGENT_QUOTA_PATH': os.path.join(tmp.name, 'quota.json')})
        env.start()
        self.addCleanup(env.stop)
        for name, value in [('QUOTA_ENABLED', True), ('_QUOTA_LIMITS', {'nous': {'tpm': 30, 'tpd': 25}})]:
            p = mock.patch.object(S, name, value)
            p.start()
            self.addCleanup(p.stop)
        S._QUOTA.clear()
        S._QUOTA_LOADED = False

    def test_remaining_capacity_caps_and_reserves_atomically(self):
        self.assertIsNone(S.quota_reserve('nous', 10, 'other'))
        gate = mock.Mock()
        gate.acquire.return_value = 0.0
        with mock.patch.object(S, 'PROVIDER_GATE', gate), \
             mock.patch.object(S, '_call_once', return_value={'text': 'answer', 'tokens': 5}) as upstream:
            result, _ = S._gated_call_once('nous', 'fake', 'x', '', 50, None)
        self.assertEqual(upstream.call_args.args[4], 14)
        self.assertIn('50 -> 14', result['quota_accounting'])
        self.assertEqual(S.quota_report(['nous'])['providers']['nous']['reserved_tokens'], 10)

    def test_warnings_survive_all_result_views_and_postprocessing(self):
        warning = 'max_tokens_capped: 20 -> 5; usage_unknown_reservation_retained; settlement_failed: disk'
        result = {'text': 'answer', 'ref': 'nous/fake', 'served_by': 'nous/fake',
                  'quota_accounting': warning}
        with mock.patch.object(S, 'call_model', return_value=result), \
             mock.patch.object(S, 'model_status', return_value='unknown'), \
             mock.patch.object(S, 'model_quality', return_value=0), \
             mock.patch.object(S, 'ask_map', return_value=[result]), \
             mock.patch.object(S, 'ask_many', return_value=[result]), \
             mock.patch.object(S, 'run_parallel', return_value=[result]), \
             mock.patch.object(S, '_select_or_error', return_value=(['nous/fake'], {})):
            ask = S.tool_ask({'model': 'nous/fake', 'prompt': 'x'})
            fanout = S.tool_fanout({'models': ['nous/fake'], 'prompts': ['x']})
            panel = S.tool_panel({'question': 'x', 'refine': True, 'refine_model': 'nous/fake'})
            mapped = S.tool_map({'model': 'nous/fake', 'items': ['x'], 'instruction': 'x', 'reduce': True})
        for data, row, render in [(ask, ask, S._render_ask),
                                  (fanout, fanout['results'][0], S._render_fanout),
                                  (panel, panel['answers'][0], S._render_panel),
                                  (mapped, mapped['results'][0], S._render_map)]:
            self.assertEqual(row.get('quota_accounting'), warning)
            self.assertIn(warning, render(data))
        self.assertEqual(panel['refined'].get('quota_accounting'), warning)
        self.assertEqual(mapped.get('reduce_quota_accounting'), warning)

    def test_empty_first_answer_warning_survives_retry(self):
        first = {'text': '', 'tokens': {}, 'latency_s': 0, 'truncated': True,
                 'quota_accounting': 'usage_unknown_reservation_retained'}
        second = {'text': 'answer', 'tokens': 5, 'latency_s': 0, 'truncated': False}
        with mock.patch.object(S, 'resolve_ref', return_value=('nous', 'fake')), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_gated_call_once', side_effect=[(first, 0), (second, 0)]), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            result = S._call_model_uncached('nous/fake', 'x', max_tokens=20, allow_fallback=False)
        self.assertEqual(result['text'], 'answer')
        self.assertIn('usage_unknown_reservation_retained', result.get('quota_accounting', ''))

    def test_report_failure_does_not_discard_answers(self):
        result = {'text': 'valuable', 'ref': 'nous/fake', 'quota_accounting': 'settlement_failed: disk'}
        with mock.patch.object(S, 'quota_report', side_effect=S.QuotaExhaustedError('broken report')), \
             mock.patch.object(S, 'ask_map', return_value=[result]), \
             mock.patch.object(S, 'ask_many', return_value=[result]), \
             mock.patch.object(S, '_select_or_error', return_value=(['nous/fake'], {})):
            fanout = S.tool_fanout({'models': ['nous/fake'], 'prompts': ['x']})
            panel = S.tool_panel({'question': 'x'})
        for data, row, render in [(fanout, fanout['results'][0], S._render_fanout),
                                  (panel, panel['answers'][0], S._render_panel)]:
            self.assertIn('valuable', row['answer'])
            self.assertIn('broken report', data['quota_report_error'])
            self.assertIn('broken report', render(data))

    def test_nonfinite_usage_keeps_answer_and_reservation(self):
        gate = mock.Mock()
        gate.acquire.return_value = 0
        with mock.patch.object(S, 'PROVIDER_GATE', gate), \
             mock.patch.object(S, '_call_once', return_value={'text': 'valuable',
                                'tokens': {'prompt': float('nan'), 'completion': 2}}):
            result, _ = S._gated_call_once('nous', 'fake', 'x', '', 8, None)
        self.assertEqual(result['text'], 'valuable')
        self.assertIn('usage_unknown_reservation_retained', result['quota_accounting'])
        self.assertEqual(S.quota_report(['nous'])['providers']['nous']['reserved_tokens'], 9)
