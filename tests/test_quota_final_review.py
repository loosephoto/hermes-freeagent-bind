"""Quota corruption and post-answer reporting regressions with isolated state."""
import json
import os
import tempfile
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class QuotaFinalReview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fa-quota-review-')
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, 'quota.json')
        env = mock.patch.dict(os.environ, {'FREEAGENT_STATE_DIR': self.tmp.name,
                                          'FREEAGENT_QUOTA_PATH': self.path})
        env.start()
        self.addCleanup(env.stop)
        for name, value in [('QUOTA_ENABLED', True), ('_QUOTA_LIMITS', {'nous': {'tpm': 100}})]:
            patch = mock.patch.object(S, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        S._QUOTA.clear()
        S._QUOTA_LOADED = False
        S._SESSIONS.clear()
        S._SESSIONS_LOADED = False

    def test_corrupt_pending_fails_before_upstream_and_preserves_file(self):
        invalid = [None, [], '', {'old': None}, {'old': {'tokens': -100, 'at': 1}},
                   {'old': {'tokens': None}}, {'old': {'tokens': True}},
                   {'old': {'tokens': 1.5}}, {'old': {}}, {'old': {'tokens': '4'}}]
        gate = mock.Mock()
        gate.acquire.return_value = 0
        for pending in invalid:
            with self.subTest(pending=pending):
                original = json.dumps({'providers': {'nous': {'pending': pending}}}).encode()
                with open(self.path, 'wb') as f:
                    f.write(original)
                S._QUOTA.clear()
                S._QUOTA_LOADED = False
                with mock.patch.object(S, 'PROVIDER_GATE', gate), \
                     mock.patch.object(S, '_call_once', return_value={'text': 'upstream'}) as upstream:
                    with self.assertRaises(S.QuotaExhaustedError):
                        S._gated_call_once('nous', 'fake', 'x', '', 9, None)
                    upstream.assert_not_called()
                with open(self.path, 'rb') as f:
                    self.assertEqual(original, f.read())

    def test_grounded_report_error_preserves_answer_and_accounting(self):
        good = {'text': 'valuable [1]', 'ref': 'nous/fake', 'served_by': 'nous/fake',
                'quota_accounting': 'settlement_failed: disk'}
        kb = {'citations': [{'source': 'x', 'title': 'x', 'url': 'https://example.com',
                             'summary': 'evidence'}], 'sources': ['x'], 'errors': {}}
        with mock.patch.object(S, 'knowledge_lookup', return_value=kb), \
             mock.patch.object(S, '_select_or_error', return_value=(['nous/fake'], {})), \
             mock.patch.object(S, 'ask_many', return_value=[good]), \
             mock.patch.object(S, 'quota_report', side_effect=S.QuotaExhaustedError('broken quota')):
            result = S.handle_tool_call({'name': 'freeagent_grounded', 'arguments': {'question': 'hello'}})
        data = result['structuredContent']
        self.assertEqual('valuable [1]', data['answers'][0]['answer'])
        self.assertEqual(good['quota_accounting'], data['answers'][0]['quota_accounting'])
        self.assertEqual('broken quota', data['quota_report_error'])
        self.assertNotIn('next_action', data)
        text = result['content'][0]['text']
        self.assertIn('broken quota', text)
        self.assertIn(good['quota_accounting'], text)

    def test_grounded_warnings_survive_many_attributions(self):
        text = S._render_grounded({'quota_report_error': 'report failed',
                                   'answers': [{'model': 'nous/fake', 'answer': 'ok',
                                                'quota_accounting': 'settlement failed'}],
                                   'citations': [{'attributions': [str(i) for i in range(50)]}]})
        self.assertIn('report failed', text)
        self.assertIn('settlement failed', text)

    def test_consult_report_error_keeps_session_and_all_round_warnings(self):
        answers = [
            [{'text': '結論: compliant\n判定: compliant', 'quota_accounting': 'compliance warning'}],
            [{'text': '結論: valuable\n重要度: optional', 'quota_accounting': 'quality warning'}],
            [{'text': '立場: final', 'quota_accounting': 'debate warning'}],
        ]
        with mock.patch.object(S, '_select_or_error', return_value=(['nous/fake'], {})), \
             mock.patch.object(S, 'ask_many', side_effect=answers), \
             mock.patch.object(S, 'quota_report', side_effect=S.QuotaExhaustedError('broken quota')):
            result = S.handle_tool_call({'name': 'freeagent_consult', 'arguments': {
                'question': 'hello', 'mode': 'review', 'draft': 'draft', 'basis': 'basis',
                'debate_depth': 'deep'}})
        data = result['structuredContent']
        self.assertTrue(data['session_id'])
        session = S.session_get(data['session_id'])
        self.assertIsNotNone(session)
        self.assertEqual('final', data['consensus'][0]['conclusion'])
        self.assertEqual('broken quota', data['quota_report_error'])
        self.assertNotIn('next_action', data)
        text = result['content'][0]['text']
        self.assertIn('broken quota', text)
        for row, warning in zip(session['rounds'], ['compliance warning', 'quality warning', 'debate warning']):
            self.assertEqual(warning, row['answers'][0]['quota_accounting'])
            self.assertIn(warning, text)
        self.assertEqual('compliance warning', data['compliance'][0]['quota_accounting'])
        self.assertEqual(['compliance warning', 'quality warning', 'debate warning'],
                         [item['warning'] for item in data['quota_accounting']])

    def test_real_call_path_with_fake_upstream_and_failed_settlement(self):
        S._QUOTA_LIMITS = {'nous': {'tpm': 10000}}
        response = {'choices': [{'message': {'content': 'valuable [1]'}, 'finish_reason': 'stop'}],
                    'usage': {'prompt_tokens': 2, 'completion_tokens': 3}}
        kb = {'citations': [{'source': 'x', 'title': 'x', 'url': 'https://example.com',
                             'summary': 'evidence'}], 'sources': ['x'], 'errors': {}}
        with mock.patch.object(S, 'knowledge_lookup', return_value=kb), \
             mock.patch.object(S, '_select_or_error', return_value=(['nous/fake'], {})), \
             mock.patch.object(S, 'resolve_ref', return_value=('nous', 'fake')), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, '_candidates', return_value=['nous/fake']), \
             mock.patch.object(S, 'provider_http', return_value=response) as upstream, \
             mock.patch.object(S, 'quota_add_tokens', side_effect=OSError('disk failed')), \
             mock.patch.object(S, 'quota_report', side_effect=OSError('report failed')):
            result = S.handle_tool_call({'name': 'freeagent_grounded', 'arguments': {'question': 'hello'}})
        data = result['structuredContent']
        self.assertEqual(1, upstream.call_count)
        self.assertEqual('valuable [1]', data['answers'][0]['answer'])
        self.assertIn('settlement_failed: disk failed', data['answers'][0]['quota_accounting'])
        self.assertIn('report failed', result['content'][0]['text'])
        with open(self.path, encoding='utf-8') as f:
            pending = json.load(f)['providers']['nous']['pending']
        self.assertEqual(1, len(pending))


if __name__ == '__main__':
    unittest.main()
