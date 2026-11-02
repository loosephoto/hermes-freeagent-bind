import unittest
from unittest import mock

from freeagent_bind import server as S


class ComplianceResumeRegressions(unittest.TestCase):
    def test_legacy_missing_or_old_fingerprint_is_conservatively_re_reviewed(self):
        import hashlib
        import json

        old_hash = hashlib.sha256(json.dumps(['Q', 'D', 'B'], ensure_ascii=False).encode('utf-8')).hexdigest()
        for legacy in (None, old_hash):
            with self.subTest(fingerprint=legacy):
                saved = {}
                calls = []

                def ask(models, prompt, **kw):
                    if kw.get('system') == S.COMPLIANCE_SYSTEM:
                        calls.append(list(models))
                        return [{'ref': r, 'text': '判定: compliant'} for r in models]
                    return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]

                with mock.patch.object(S, '_select_or_error', return_value=(['nous/a'], {})), \
                     mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
                     mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
                     mock.patch.object(S, 'is_cooling', return_value=False), \
                     mock.patch.object(S, 'ask_many', side_effect=ask):
                    first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B',
                                            'main_reply': 'context'})
                    compliance = next(r for r in saved[first['session_id']]['rounds'] if r['kind'] == 'compliance')
                    if legacy is None:
                        compliance.pop('fingerprint')
                    else:
                        compliance['fingerprint'] = legacy
                    second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'context'})
                self.assertEqual(calls, [['nous/a'], ['nous/a']])
                self.assertFalse(second['compliance_summary']['blocking'])

    def test_changed_main_reply_invalidates_compliance_and_same_reply_reuses_it(self):
        refs = ['nous/a', 'nous/b']
        saved = {}
        calls = []

        def ask(models, prompt, **kw):
            if kw.get('system') == S.COMPLIANCE_SYSTEM:
                calls.append(prompt)
                verdict = 'noncompliant' if 'changed context' in prompt else 'compliant'
                return [{'ref': r, 'served_by': r, 'text': f'判定: {verdict}'} for r in models]
            return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]

        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B',
                                    'main_reply': 'old context'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'changed context'})
            third = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'changed context'})
        self.assertFalse(first['compliance_summary']['blocking'])
        self.assertTrue(second['compliance_summary']['blocking'])
        self.assertTrue(third['compliance_summary']['blocking'])
        self.assertEqual(len(calls), 2)
        self.assertIn('changed context', calls[1])

    def test_cooling_unreviewed_model_remains_blocking_across_resumes(self):
        refs = ['nous/a', 'nous/b']
        saved = {}
        cooling = set()
        recovered = set()
        calls = []

        def ask(models, prompt, **kw):
            if kw.get('system') == S.COMPLIANCE_SYSTEM:
                calls.append(list(models))
                return [{'ref': r, 'served_by': r, 'text': '判定: compliant'} if r == 'nous/a' or r in recovered
                        else {'ref': r, 'error': 'HTTP 429'} for r in models]
            return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]

        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', side_effect=lambda ref: ref in cooling), \
             mock.patch.object(S, 'ask_many', side_effect=ask):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B',
                                    'main_reply': 'same context'})
            cooling.add('nous/b')
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same context'})
            cooling.update(refs)
            third = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same context'})
            cooling.clear()
            recovered.add('nous/b')
            fourth = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same context'})
        for result in (first, second, third):
            self.assertEqual(result['compliance_summary']['unreviewed'], 1)
            self.assertTrue(result['compliance_summary']['blocking'])
            self.assertTrue(result['findings_summary']['blocking'])
            self.assertTrue(any(row['model'] == 'nous/b' for row in result['failed']))
        self.assertEqual(calls, [refs, ['nous/b']])
        self.assertFalse(fourth['compliance_summary']['blocking'])
        self.assertEqual(fourth['compliance_summary']['compliant'], 2)
        self.assertEqual(saved[first['session_id']]['models'], refs)

    def test_retry_fallback_cannot_reuse_prior_requested_or_served_model(self):
        refs = ['nous/a', 'nous/b']
        saved = {}
        actual_ask = S.ask_many
        attempts = []
        compliance_calls = []

        def ask(models, prompt, **kw):
            if kw.get('system') != S.COMPLIANCE_SYSTEM:
                return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]
            compliance_calls.append(list(models))
            if len(compliance_calls) == 1:
                return [{'ref': 'nous/a', 'served_by': 'nous/c', 'text': '判定: compliant'},
                        {'ref': 'nous/b', 'error': 'HTTP 503: bad'}]
            return actual_ask(models, prompt, **kw)

        def gated(provider, model, *args, **kwargs):
            attempts.append(model)
            if model == 'b':
                raise S.HttpStatusError(500, 'bad')
            return {'text': '判定: compliant', 'latency_s': 0.1,
                    'truncated': False, 'tokens': {}}, 0.0

        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False), \
             mock.patch.object(S, 'ask_many', side_effect=ask), \
             mock.patch.object(S, 'resolve_ref', side_effect=lambda ref, **kw: ref.split('/', 1)), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_candidates', return_value=['nous/b', 'nous/c', 'nous/a']), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B',
                                    'main_reply': 'same context'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same context'})
        self.assertEqual(compliance_calls, [refs, ['nous/b']])
        self.assertEqual(attempts, ['b'])
        self.assertEqual(second['compliance_summary']['unreviewed'], 1)
        self.assertTrue(second['compliance_summary']['blocking'])
