import unittest
from unittest import mock

from freeagent_bind import server as S


class AuditFixes(unittest.TestCase):
    def test_diagnose_reports_selected_category_count(self):
        for counts, kind, expected in [
            ({'rate_limited': 4, 'auth': 6}, 'rate_limited', '4'),
            ({'no_output': 3, 'auth': 7}, 'empty_output', '3'),
            ({'timeout': 2, 'auth': 8}, 'timeout', '2'),
        ]:
            with self.subTest(kind=kind):
                advice = S._diag_advice({'calls': 10, 'failed': 10}, counts, {}, [])
                self.assertEqual(advice['kind'], kind)
                self.assertIn(f'うち {expected} 件', advice['advice'])

    def test_previous_verifier_cannot_be_fallback(self):
        def resolve(ref, free_only=True):
            return ref.split('/', 1)
        def gated(provider, model, *args, **kwargs):
            if model == 'b':
                raise S.HttpStatusError(500, 'broken')
            return {'text': '判定: 妥当', 'latency_s': 0.1, 'truncated': False, 'tokens': {}}, 0.0
        with mock.patch.object(S, 'select_models', return_value=(['nous/b'], {})), \
             mock.patch.object(S, '_candidates', return_value=['nous/b', 'nous/a']), \
             mock.patch.object(S, 'resolve_ref', side_effect=resolve), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated) as upstream, \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            result, _, _ = S._think_verify_block({'size': 1}, 'Q', [], {'n': 2, 'text': 'x'}, {}, ['nous/a'], 'sid')
        self.assertEqual(upstream.call_count, 1)
        self.assertTrue(result['answers'][0].get('error'))

    def test_failed_attempt_recorded_once(self):
        with mock.patch.object(S, 'resolve_ref', return_value=('nous', 'a')), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_gated_call_once', side_effect=S.HttpStatusError(500, 'bad')) as upstream, \
             mock.patch.object(S, 'observe_call') as observe:
            result = S.call_model('nous/a', 'x', allow_fallback=False)
        self.assertTrue(result.get('error'))
        self.assertEqual(upstream.call_count, 1)
        self.assertEqual(observe.call_count, 1)

    def test_deep_preserves_initial_results(self):
        refs = ['nous/a', 'nous/b']
        def fake(refs, prompt, **kw):
            system = kw.get('system')
            if system == S.REVIEW_SYSTEM:
                text = '結論: 問題あり\n重要度: required\n指摘: [security] 危険'
            elif system == S.DEBATE_SYSTEM:
                text = '立場: 問題あり\n最強の反論: 危険\n応答: 修正\n未解決: なし'
            else:
                text = '結論: 初回結果\n確信度: 80\nメインに確認したい点: 予算は？'
            return [{'ref': r, 'served_by': r, 'text': text} for r in refs]
        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'ask_many', side_effect=fake), \
             mock.patch.object(S, 'session_put'):
            with mock.patch.object(S, 'budget_expired', return_value=True):
                skipped = S.tool_consult({'question': 'Q', 'debate_depth': 'deep'})
            review = S.tool_consult({'question': 'Q', 'debate_depth': 'deep', 'mode': 'review', 'draft': 'D'})
        with self.subTest('skipped'):
            self.assertEqual(len(skipped['consensus']), 2)
            self.assertTrue(skipped['open_questions_for_main'])
            self.assertEqual(skipped['stage'], 'awaiting_main')
        with self.subTest('review'):
            self.assertEqual(review['findings_summary']['required'], 2)
            self.assertTrue(review['review'][0]['findings'])

    def test_compliance_failure_blocks_and_retries_missing_reviewers(self):
        refs = ['nous/a', 'nous/b']
        saved = {}
        calls = []
        def fake(models, prompt, **kw):
            if kw.get('system') == S.COMPLIANCE_SYSTEM:
                calls.append(list(models))
                if len(calls) == 1:
                    return [{'ref': models[0], 'text': '判定: compliant'},
                            {'ref': models[1], 'error': 'HTTP 503: bad'}]
                return [{'ref': r, 'text': '判定: compliant'} for r in models]
            return [{'ref': r, 'text': '結論: OK\n重要度: nit\n指摘: なし'} for r in models]
        def put(sid, value):
            saved[sid] = value
        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'ask_many', side_effect=fake), \
             mock.patch.object(S, 'session_put', side_effect=put), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B', 'main_reply': 'retry'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'retry'})
        self.assertTrue(first['compliance_summary']['blocking'])
        self.assertEqual(first['compliance_summary']['unreviewed'], 1)
        self.assertTrue(first['findings_summary']['blocking'])
        self.assertTrue(any(row['model'] == 'nous/b' for row in first['failed']))
        self.assertEqual(calls, [refs, ['nous/b']])
        self.assertEqual(second['compliance_summary']['compliant'], 2)
        self.assertFalse(second['compliance_summary']['blocking'])

    def test_changed_draft_or_basis_invalidates_compliance(self):
        refs = ['nous/a', 'nous/b']
        saved = {}
        calls = []
        def fake(models, prompt, **kw):
            if kw.get('system') == S.COMPLIANCE_SYSTEM:
                calls.append(prompt)
                return [{'ref': r, 'text': '判定: compliant'} for r in models]
            return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]
        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'ask_many', side_effect=fake), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D1', 'basis': 'B1', 'main_reply': 'continue'})
            sid = first['session_id']
            S.tool_consult({'session_id': sid, 'main_reply': 'continue'})
            self.assertEqual(len(calls), 1)
            S.tool_consult({'session_id': sid, 'main_reply': 'continue', 'draft': 'D2'})
            self.assertEqual(len(calls), 2)
            S.tool_consult({'session_id': sid, 'main_reply': 'continue', 'basis': 'B2'})
            self.assertEqual(len(calls), 3)
            self.assertIn('D2', calls[-1])
            self.assertIn('B2', calls[-1])
            S.tool_consult({'session_id': sid, 'main_reply': 'continue', 'draft': 'D1', 'basis': 'B1'})
            self.assertEqual(len(calls), 4)

    def test_fallback_honors_base_model_exclusion(self):
        claims = S._ModelClaims(['nous/b'], ['nous/a'])
        self.assertFalse(claims.take('nous/a:free'))

    def test_fallback_failure_is_not_attributed_to_requested_model(self):
        def resolve(ref, free_only=True):
            return ref.split('/', 1)
        def gated(provider, model, *args, **kwargs):
            raise S.HttpStatusError(500, 'bad') if model == 'a' else S.HttpStatusError(429, 'limited')
        with mock.patch.object(S, 'resolve_ref', side_effect=resolve), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_candidates', return_value=['nous/a', 'nous/b']), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'note_rate_limited', return_value=0), \
             mock.patch.object(S, 'observe_call') as observe:
            S.call_model('nous/a', 'x')
        self.assertEqual([call.args[0]['ref'] for call in observe.call_args_list], ['nous/a', 'nous/b'])

    def test_prefer_exclude_refills(self):
        refs = ['nous/a', 'nous/b']
        with mock.patch.object(S, 'free_model_refs', return_value=refs), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}):
            chosen, _ = S.select_models(1, prefer=['nous/a'], exclude=['nous/a'])
        self.assertEqual(chosen, ['nous/b'])
