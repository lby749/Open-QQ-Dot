import json
import unittest

from local_decision_router import LocalDecisionRouter
from persona_store import ScopeMismatch
from persona_types import (ContextBudgets, ConversationPlan, DataScope, InvalidSource,
    PrivacyViolation, PrivacyView, ProviderKind, RetrievalFilters, RetrievalRequest,
    SourceOrigin, SourceRef)
from retrieval_router import RetrievalQueryRouter


NOW = '2026-10-07T12:00:00+00:00'


class FakeProvider:
    provider_kind = ProviderKind.LOCAL

    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        return self.output


def request(*, query='Hello', recent_context='itemsdays', scene='daily_chat',
            plan=None, friend_id='12345'):
    return RetrievalRequest(friend_id=friend_id, query=query, recent_context=recent_context,
        scene=scene, plan=plan, filters=RetrievalFilters(time_range=(
            '2026-10-01T00:00:00+00:00', None)), budgets=ContextBudgets(),
        allowed_pack_versions=frozenset(), now=NOW)


def source_ref(friend_id='12345', *, scope=DataScope.PRIVATE_FRIEND):
    private = scope == DataScope.PRIVATE_FRIEND
    return SourceRef(dataset_id='fixture', import_id='fixture-import', record_id='row-1',
        message_seq=1 if private else None, speaker_id='speaker',
        origin=SourceOrigin.REAL_SINGLE_SPEAKER if private else SourceOrigin.PUBLIC_DATASET,
        data_scope=scope, privacy_view=PrivacyView.LOCAL_FULL,
        friend_id=friend_id if private else None,
        event_time='2026-10-06T10:00:00+00:00', recorded_at=NOW)


def output(*, scene='daily_chat', reason='clear', status='ok', route_hints=None):
    return {'scene': scene, 'reason_code': reason, 'status': status,
            'route_hints': [] if route_hints is None else route_hints}


def wait_plan(action='wait'):
    return ConversationPlan(action=action, intent='', tone='', core_meaning='',
        ask_question=False, continue_topic=False, introduce_new_topic=False,
        desired_bubbles=0, sticker_intent=None)


class LocalDecisionRouterTests(unittest.TestCase):
    def test_provider_is_called_once_with_only_local_analysis_input_and_hints_are_additive(self):
        provider = FakeProvider(output(route_hints=['memory', 'persona', 'memory']))
        req = request(query='Hello')
        filters = req.filters
        result = LocalDecisionRouter(provider).decide(req, [source_ref()])

        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0], {'scene': 'daily_chat', 'query': 'Hello',
                                             'recent_context': 'itemsdays'})
        self.assertEqual(result.baseline, RetrievalQueryRouter().route(req))
        self.assertEqual(result.baseline.retrievers, ['example', 'persona', 'interest'])
        self.assertEqual(result.retrievers, ('example', 'persona', 'interest', 'memory'))
        self.assertEqual(result.route_hints, ('memory', 'persona'))
        self.assertIs(req.filters, filters)
        self.assertEqual(result.baseline.scene, req.scene)
        self.assertEqual(result.baseline.query_terms, ['Hello'])
        self.assertEqual(result.baseline.time_range, req.filters.time_range)

    def test_provider_must_explicitly_declare_local(self):
        with self.assertRaises(PrivacyViolation):
            LocalDecisionRouter(lambda payload: output())
        cloud_provider = FakeProvider(output())
        cloud_provider.provider_kind = ProviderKind.CLOUD
        with self.assertRaises(PrivacyViolation):
            LocalDecisionRouter(cloud_provider)

    def test_json_output_and_fixed_unknown_values(self):
        provider = FakeProvider(json.dumps(output(scene='knowledge', reason='mixed_signals',
            route_hints=['topic']), ensure_ascii=False))
        result = LocalDecisionRouter(provider).decide(request(), [source_ref()])
        self.assertEqual((result.scene, result.reason_code, result.status),
                         ('knowledge', 'mixed_signals', 'ok'))
        self.assertEqual(result.retrievers[-1], 'topic')

    def test_unimplemented_route_and_unbounded_hints_fallback(self):
        for hints in (['unfinished_topic'], ['memory'] * 6):
            with self.subTest(hints=hints):
                provider = FakeProvider(output(route_hints=hints))
                result = LocalDecisionRouter(provider).decide(request(), [source_ref()])
                self.assertEqual(result.degraded_code, 'provider_output_invalid')
                self.assertEqual(result.status, 'unknown')
                self.assertEqual(result.scene, 'unknown')
                self.assertEqual(result.retrievers, tuple(result.baseline.retrievers))
                self.assertEqual(len(provider.calls), 1)

    def test_reason_code_is_a_fixed_enum(self):
        result = LocalDecisionRouter(FakeProvider(output(reason='ambiguous'))).decide(
            request(), [source_ref()])
        self.assertEqual(result.reason_code, 'ambiguous')
        for reason in ('not-listed', None):
            with self.subTest(reason=reason):
                result = LocalDecisionRouter(FakeProvider(output(reason=reason))).decide(
                    request(), [source_ref()])
                self.assertEqual(result.degraded_code, 'provider_output_invalid')

    def test_oversized_query_and_context_fall_back_without_truncation_or_provider_call(self):
        provider = FakeProvider(output(route_hints=['memory']))
        req = request(query='q' * 8193)
        baseline = RetrievalQueryRouter().route(req)
        result = LocalDecisionRouter(provider).decide(req, [source_ref()])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result.degraded_code, 'input_too_large')
        self.assertEqual(result.baseline, baseline)
        self.assertEqual(result.retrievers, tuple(baseline.retrievers))

        boundary_provider = FakeProvider(output())
        boundary = request(query='q' * 8180, recent_context='r' * 12)
        LocalDecisionRouter(boundary_provider).decide(boundary, [source_ref()])
        self.assertEqual(len(boundary_provider.calls), 1)

    def test_merged_routes_are_capped_and_accepted_hints_are_reported(self):
        provider = FakeProvider(output(route_hints=[
            'memory', 'structured', 'keyword', 'semantic', 'temporal']))
        result = LocalDecisionRouter(provider).decide(request(), [source_ref()])
        self.assertEqual(len(result.retrievers), 6)
        self.assertEqual(result.retrievers[:3], tuple(result.baseline.retrievers))
        self.assertEqual(result.retrievers[3:], ('memory', 'structured', 'keyword'))
        self.assertEqual(result.route_hints, ('memory', 'structured', 'keyword'))

    def test_messages_reply_bubbles_and_extra_fields_fallback(self):
        for key in ('messages', 'reply', 'bubbles', 'friend_id', 'filters'):
            with self.subTest(key=key):
                payload = output()
                payload[key] = 'do not accept'
                result = LocalDecisionRouter(FakeProvider(payload)).decide(
                    request(), [source_ref()])
                self.assertEqual(result.degraded_code, 'provider_output_invalid')
                self.assertEqual(result.retrievers, tuple(result.baseline.retrievers))

    def test_unavailable_timeout_and_provider_exception_fall_back_without_retry(self):
        cases = ((None, 'provider_unavailable'),
                 (FakeProvider(error=TimeoutError()), 'provider_timeout'),
                 (FakeProvider(error=RuntimeError('private exception text')), 'provider_error'))
        for provider, code in cases:
            with self.subTest(code=code):
                router = LocalDecisionRouter(provider)
                result = router.decide(request(), [source_ref()])
                self.assertEqual(result.degraded_code, code)
                self.assertEqual(result.status, 'unknown')
                self.assertEqual(result.retrievers, tuple(result.baseline.retrievers))
                if provider is not None:
                    self.assertEqual(len(provider.calls), 1)

    def test_provider_privacy_and_scope_exceptions_propagate(self):
        failures = (ScopeMismatch('scope'), PrivacyViolation('privacy'),
                    InvalidSource('source'))
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                provider = FakeProvider(error=failure)
                with self.assertRaises(type(failure)):
                    LocalDecisionRouter(provider).decide(request(), [source_ref()])
                self.assertEqual(len(provider.calls), 1)

    def test_wait_and_ignore_skip_provider_and_retrieval(self):
        for action in ('wait', 'ignore'):
            provider = FakeProvider(output(route_hints=['memory']))
            result = LocalDecisionRouter(provider).decide(request(plan=wait_plan(action)), [])
            self.assertEqual(provider.calls, [])
            self.assertEqual(result.baseline.mode, 'skip')
            self.assertEqual(result.retrievers, ())
            self.assertEqual(result.reason_code, 'planner_skip')
            self.assertIsNone(result.degraded_code)

    def test_cross_friend_source_hard_fails_before_provider(self):
        provider = FakeProvider(output())
        with self.assertRaises(ScopeMismatch):
            LocalDecisionRouter(provider).decide(request(), [source_ref('54321')])
        self.assertEqual(provider.calls, [])

    def test_analysis_policy_requires_sources_and_only_local_provider(self):
        provider = FakeProvider(output())
        with self.assertRaises(InvalidSource):
            LocalDecisionRouter(provider).decide(request(), [])
        self.assertEqual(provider.calls, [])
        cloud_provider = FakeProvider(output())
        cloud_provider.provider_kind = ProviderKind.CLOUD
        with self.assertRaises(PrivacyViolation):
            LocalDecisionRouter(cloud_provider)

    def test_public_source_is_gated_as_local_scene_analysis(self):
        provider = FakeProvider(output())
        result = LocalDecisionRouter(provider).decide(
            request(), [source_ref(scope=DataScope.PUBLIC_DATASET)])
        self.assertEqual(result.status, 'ok')
        self.assertEqual(len(provider.calls), 1)


if __name__ == '__main__':
    unittest.main()
