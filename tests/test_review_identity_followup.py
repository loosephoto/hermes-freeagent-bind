"""Regression tests for saved reviewer identity and exclusion semantics."""
import hashlib
import json
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class ReviewIdentityFollowupTest(unittest.TestCase):
    def test_legacy_duplicate_hf_slots_fail_closed_without_calls(self):
        refs = ['huggingface/org/c:novita', 'huggingface/org/c:together']
        stored = {'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B',
                  'models': refs, 'rounds': []}
        with mock.patch.object(S, 'session_get', return_value=stored), \
             mock.patch.object(S, 'ask_many') as ask, \
             mock.patch.object(S, 'session_put') as save:
            result = S.tool_consult({'session_id': 'legacy', 'main_reply': 'same'})
        self.assertIn('error', result)
        self.assertIn('huggingface/org/c:novita', result['error'])
        self.assertIn('huggingface/org/c:together', result['error'])
        ask.assert_not_called()
        save.assert_not_called()

    def test_legacy_prior_answers_same_hf_served_model_fail_closed(self):
        refs = ['huggingface/org/a', 'huggingface/org/b']
        served = ['huggingface/org/c:novita', 'huggingface/org/c:together']
        fingerprint = hashlib.sha256(json.dumps(['Q', 'D', 'B', 'same'], ensure_ascii=False).encode('utf-8')).hexdigest()
        stored = {'question': 'Q', 'mode': 'review', 'draft': 'D', 'basis': 'B', 'models': refs,
                  'rounds': [{'kind': 'compliance', 'fingerprint': fingerprint, 'answers': [
                      {'model': ref, 'served_by': actual, 'compliance': 'compliant'}
                      for ref, actual in zip(refs, served)]}]}
        with mock.patch.object(S, 'session_get', return_value=stored), \
             mock.patch.object(S, 'ask_many') as ask, \
             mock.patch.object(S, 'session_put') as save:
            result = S.tool_consult({'session_id': 'legacy', 'main_reply': 'same'})
        self.assertIn('error', result)
        for ref in served:
            self.assertIn(ref, result['error'])
        ask.assert_not_called()
        save.assert_not_called()

    def test_dynamic_identity_is_exact_for_non_hf_and_route_agnostic_for_hf(self):
        for provider in ('nous', 'openrouter'):
            base, free = provider + '/org/c', provider + '/org/c:free'
            for excluded, candidate in ((base, free), (free, base)):
                with self.subTest(excluded=excluded, candidate=candidate):
                    claims = S._ModelClaims([], identity_avoid=[excluded])
                    self.assertTrue(claims.take(candidate))
                    self.assertFalse(claims.take(excluded))
        routes = ['huggingface/org/c', 'huggingface/org/c:novita', 'huggingface/org/c:together']
        for excluded in routes:
            for candidate in routes:
                with self.subTest(excluded=excluded, candidate=candidate):
                    claims = S._ModelClaims([], identity_avoid=[excluded])
                    self.assertTrue(claims.is_excluded(candidate))
                    self.assertFalse(claims.take(candidate))

    def test_explicit_base_exclusion_remains_wildcard(self):
        for provider in ('nous', 'openrouter'):
            claims = S._ModelClaims([], avoid=[provider + '/org/c'])
            self.assertTrue(claims.is_excluded(provider + '/org/c:free'))
            self.assertFalse(claims.take(provider + '/org/c:free'))

    def test_environment_base_exclusion_remains_wildcard_in_selection_and_fallback(self):
        refs = ['nous/org/a', 'nous/org/c:free', 'nous/org/d']
        with mock.patch.dict(S.os.environ, {'FREEAGENT_MODEL_EXCLUDE': 'nous/org/c'}), \
             mock.patch.object(S, 'free_model_refs', return_value=refs), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}):
            chosen, _ = S.select_models(3)
            candidates = S._candidates(refs[0])
        self.assertNotIn('nous/org/c:free', chosen)
        self.assertNotIn('nous/org/c:free', candidates)
        self.assertIn('nous/org/d', chosen)
        self.assertIn('nous/org/d', candidates)

    def test_resume_non_hf_prior_base_does_not_exclude_free_suffix(self):
        refs = ['nous/org/a', 'nous/org/b']
        saved = {}
        attempts = []
        calls = []
        real_ask = S.ask_many

        def ask(models, prompt, **kw):
            if kw.get('system') != S.COMPLIANCE_SYSTEM:
                return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]
            calls.append(list(models))
            if len(calls) == 1:
                return [{'ref': refs[0], 'served_by': 'nous/org/c', 'text': '判定: compliant'},
                        {'ref': refs[1], 'error': 'HTTP 503'}]
            return real_ask(models, prompt, **kw)

        def gated(provider, model, *args, **kw):
            attempts.append(provider + '/' + model)
            if model == 'org/b':
                raise S.HttpStatusError(500, 'bad')
            return {'text': '判定: compliant', 'latency_s': 0.1,
                    'truncated': False, 'tokens': {}}, 0.0

        with mock.patch.dict(S.os.environ, {'FREEAGENT_MODEL_EXCLUDE': ''}), \
             mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False), \
             mock.patch.object(S, 'ask_many', side_effect=ask), \
             mock.patch.object(S, 'resolve_ref', side_effect=lambda ref, **kw: ref.split('/', 1)), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_candidates', return_value=[refs[1], 'nous/org/c:free']), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D',
                                    'basis': 'B', 'main_reply': 'same'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same'})
        self.assertEqual(attempts, [refs[1], 'nous/org/c:free'])
        self.assertEqual(second['compliance_summary']['reviewed'], 2)
        self.assertEqual(second['compliance_summary']['unreviewed'], 0)
