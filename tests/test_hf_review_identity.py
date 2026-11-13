"""HF routing suffixes must not manufacture independent review models."""
import threading
import unittest
from unittest import mock

import os as _fa_os, sys as _fa_sys
_FA_SRC = _fa_os.path.join(_fa_os.path.dirname(_fa_os.path.dirname(_fa_os.path.abspath(__file__))), 'src')
if _FA_SRC not in _fa_sys.path:
    _fa_sys.path.insert(0, _FA_SRC)
from freeagent_bind import server as S


class HFReviewerIdentityTest(unittest.TestCase):
    def test_resumed_compliance_does_not_count_prior_hf_route_twice(self):
        refs = ['huggingface/org/a', 'huggingface/org/b']
        saved = {}
        actual_ask = S.ask_many
        compliance_calls = []
        attempts = []

        def ask(models, prompt, **kw):
            if kw.get('system') != S.COMPLIANCE_SYSTEM:
                return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]
            compliance_calls.append(list(models))
            if len(compliance_calls) == 1:
                return [{'ref': refs[0], 'served_by': 'huggingface/org/c:novita',
                         'text': '判定: compliant'},
                        {'ref': refs[1], 'error': 'HTTP 503'}]
            return actual_ask(models, prompt, **kw)

        def gated(provider, model, *args, **kw):
            attempts.append(provider + '/' + model)
            if model == 'org/b':
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
             mock.patch.object(S, '_candidates', return_value=[refs[1], 'huggingface/org/c']), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D',
                                    'basis': 'B', 'main_reply': 'same'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same'})
        self.assertEqual(compliance_calls, [refs, [refs[1]]])
        self.assertEqual(attempts, [refs[1]])
        self.assertEqual(second['compliance_summary']['reviewed'], 1)
        self.assertEqual(second['compliance_summary']['unreviewed'], 1)
        self.assertTrue(second['compliance_summary']['blocking'])

    def test_legacy_prior_served_alias_blocks_retry_primary(self):
        refs = ['huggingface/org/a', 'huggingface/org/b']
        saved = {}
        actual_ask = S.ask_many
        calls = []
        attempts = []

        def ask(models, prompt, **kw):
            if kw.get('system') != S.COMPLIANCE_SYSTEM:
                return [{'ref': r, 'text': '結論: OK\n重要度: nit'} for r in models]
            calls.append(list(models))
            if len(calls) == 1:
                return [{'ref': refs[0], 'served_by': refs[1] + ':novita', 'text': '判定: compliant'},
                        {'ref': refs[1], 'error': 'HTTP 503'}]
            return actual_ask(models, prompt, **kw)

        def gated(provider, model, *args, **kw):
            attempts.append(model)
            return {'text': '判定: compliant', 'latency_s': 0.1, 'truncated': False, 'tokens': {}}, 0.0

        with mock.patch.object(S, '_select_or_error', return_value=(refs, {})), \
             mock.patch.object(S, 'session_put', side_effect=lambda sid, value: saved.update({sid: value})), \
             mock.patch.object(S, 'session_get', side_effect=lambda sid: saved.get(sid)), \
             mock.patch.object(S, 'is_cooling', return_value=False), \
             mock.patch.object(S, 'ask_many', side_effect=ask), \
             mock.patch.object(S, 'resolve_ref', side_effect=lambda ref, **kw: ref.split('/', 1)), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_candidates', return_value=[refs[1]]), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            first = S.tool_consult({'question': 'Q', 'mode': 'review', 'draft': 'D',
                                    'basis': 'B', 'main_reply': 'same'})
            second = S.tool_consult({'session_id': first['session_id'], 'main_reply': 'same'})
        self.assertEqual(attempts, [])
        self.assertEqual(second['compliance_summary']['unreviewed'], 1)
        self.assertTrue(second['compliance_summary']['blocking'])

    def test_hf_claims_match_both_directions_and_different_routes(self):
        variants = ['huggingface/org/c', 'huggingface/org/c:novita', 'huggingface/org/c:together']
        for reserved in variants:
            for candidate in variants:
                with self.subTest(reserved=reserved, candidate=candidate):
                    self.assertFalse(S._ModelClaims([reserved]).take(candidate))
                    self.assertFalse(S._ModelClaims([], [reserved]).take(candidate))
        claims = S._ModelClaims(['huggingface/org/c:novita'])
        self.assertTrue(claims.take('huggingface/org/d:together'))
        self.assertTrue(claims.take('nous/org/c:free'))

    def test_parallel_fallbacks_use_distinct_hf_models(self):
        refs = ['huggingface/org/a', 'huggingface/org/b']
        first_fallback_started = threading.Event()
        attempts = []

        def gated(provider, model, *args, **kw):
            attempts.append(model)
            if model == 'org/a':
                raise S.HttpStatusError(500, 'bad')
            if model == 'org/b':
                if not first_fallback_started.wait(5):
                    raise AssertionError('first fallback did not start')
                raise S.HttpStatusError(500, 'bad')
            if model == 'org/c:novita':
                first_fallback_started.set()
            return {'text': 'ok', 'latency_s': 0.1, 'truncated': False, 'tokens': {}}, 0.0

        def candidates(ref, free_only=True):
            if ref == refs[0]:
                return [ref, 'huggingface/org/c:novita']
            return [ref, 'huggingface/org/c:together', 'huggingface/org/d']

        with mock.patch.object(S, 'resolve_ref', side_effect=lambda ref, **kw: ref.split('/', 1)), \
             mock.patch.object(S, '_is_known_paid_model', return_value=False), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, '_candidates', side_effect=candidates), \
             mock.patch.object(S, '_gated_call_once', side_effect=gated), \
             mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
            results = S.ask_many(refs, 'review')
        self.assertEqual([row['served_by'] for row in results],
                         ['huggingface/org/c:novita', 'huggingface/org/d'])
        self.assertNotIn('org/c:together', attempts)

    def test_paid_and_free_catalog_ids_remain_distinct(self):
        for provider in ('nous', 'openrouter'):
            with self.subTest(provider=provider), mock.patch.dict(S._MODELS_CACHE, {
                provider: {'rows': [{'id': 'org/c', 'free': False},
                                    {'id': 'org/c:free', 'free': True}]}
            }):
                self.assertTrue(S._is_known_paid_model(provider, 'org/c'))
                self.assertFalse(S._is_known_paid_model(provider, 'org/c:free'))
                self.assertTrue(S._ModelClaims([provider + '/org/c']).take(provider + '/org/c:free'))
                self.assertTrue(S._ModelClaims([provider + '/org/c:free']).take(provider + '/org/c'))

    def test_free_only_calls_refuse_paid_but_allow_free_exact_id(self):
        for provider in ('nous', 'openrouter'):
            with self.subTest(provider=provider), \
                 mock.patch.dict(S._MODELS_CACHE, {provider: {'rows': [
                     {'id': 'org/c', 'free': False}, {'id': 'org/c:free', 'free': True}]} }), \
                 mock.patch.object(S, 'cooling_refs', return_value={}), \
                 mock.patch.object(S, '_gated_call_once', return_value=(
                     {'text': 'ok', 'latency_s': 0.1, 'truncated': False, 'tokens': {}}, 0.0)) as upstream, \
                 mock.patch.object(S, 'observe_call'), mock.patch.object(S, 'clear_provider_auth'):
                paid = S.call_model(provider + '/org/c', 'q', allow_fallback=False)
                free = S.call_model(provider + '/org/c:free', 'q', allow_fallback=False)
            self.assertIn('有料モデル', paid['error'])
            self.assertEqual(free['served_by'], provider + '/org/c:free')
            self.assertEqual(upstream.call_count, 1)
            self.assertEqual(upstream.call_args.args[:2], (provider, 'org/c:free'))

    def test_selection_hf_aliases_do_not_count_as_separate_models(self):
        available = ['huggingface/org/c', 'huggingface/org/d', 'nous/org/c:free']
        with mock.patch.object(S, 'free_model_refs', return_value=available), \
             mock.patch.object(S, 'cooling_refs', return_value={}), \
             mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
             mock.patch.object(S, 'model_status', return_value={}):
            chosen, _ = S.select_models(3, ['huggingface/org/c:novita',
                                          'huggingface/org/c:together'])
        self.assertEqual(len(chosen), 3)
        self.assertEqual(sum(ref.startswith('huggingface/org/c') for ref in chosen), 1)
        self.assertIn('huggingface/org/d', chosen)
        self.assertIn('nous/org/c:free', chosen)

    def test_selection_does_not_accept_non_hf_suffix_as_base_catalog_id(self):
        for provider in ('nous', 'openrouter'):
            with self.subTest(provider=provider), \
                 mock.patch.object(S, 'free_model_refs', return_value=[provider + '/org/c']), \
                 mock.patch.object(S, 'cooling_refs', return_value={}), \
                 mock.patch.object(S, 'provider_auth_blocked', return_value=''), \
                 mock.patch.object(S, 'model_status', return_value={}):
                chosen, info = S.select_models(1, [provider + '/org/c:free'])
            self.assertNotIn(provider + '/org/c:free', chosen)
            self.assertTrue(info['notes'])
