from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from persona_store import ScopeMismatch, Unavailable
from persona_types import (Candidate, ContextBudgets, DataScope,
    FriendScope, PackVersion, PrivacyView, RetrievalBatch, RetrievalFilters,
    RetrievalRequest, SourceOrigin, SourceRef, ConversationPlan)
import retrieval_orchestrator as orchestrator_module
from retrieval_orchestrator import RetrievalOrchestrator
from retrieval_ranking import rank_candidates


NOW = '2026-10-06T00:00:00+00:00'


class StubRetriever:
    def __init__(self, candidates=(), error=None, calls=None, marker=None):
        self.candidates = tuple(candidates)
        self.error = error
        self.calls = calls if calls is not None else []
        self.marker = marker

    def retrieve(self, request, limit):
        self.calls.append((self.marker, request.query, limit))
        if self.error:
            raise self.error
        return RetrievalBatch(candidates=self.candidates[:limit], elapsed_ms=0)


class RetrievalOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        friend = root / 'friends' / '12345'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345', 'basic_info': {}}), 'utf-8')
        (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
        self.scope = FriendScope.load('12345', project_root=root)
        self.orchestrator = RetrievalOrchestrator(self.scope)

    def request(self, query='普通内容', *, scene='unknown_scene', plan=None,
                namespace=(), privacy=DataScope.PRIVATE_FRIEND, allowed=(), budgets=None):
        return RetrievalRequest(friend_id='12345', query=query, recent_context='PRIVATE_CONTEXT',
            scene=scene, plan=plan, filters=RetrievalFilters(namespace=namespace,
            privacy_scope=privacy), budgets=budgets or ContextBudgets(),
            allowed_pack_versions=frozenset(allowed), now=NOW)

    def candidate(self, identifier, namespace, *, retriever='keyword', friend_id='12345',
                  original_scope=DataScope.PRIVATE_FRIEND, pack_id=None, pack_version=None,
                  content='PRIVATE_CANDIDATE_CONTENT'):
        source = SourceRef(dataset_id='manual:test', import_id='test-import',
            record_id=identifier, message_seq=None, speaker_id='operator',
            origin=(SourceOrigin.PUBLIC_DATASET if original_scope == DataScope.PUBLIC_PACK
                    else SourceOrigin.MANUAL), data_scope=original_scope,
            privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=friend_id if original_scope == DataScope.PRIVATE_FRIEND else None,
            event_time=None, recorded_at=NOW, pack_id=pack_id, pack_version=pack_version)
        return Candidate(id=identifier, namespace=namespace, type='fixture', content=content,
            score=0.5, source=source, reason='private reason text', retriever=retriever,
            friend_id=friend_id, timestamp=None, confidence=0.9,
            original_scope=original_scope, pack_version=pack_version)

    def install(self, mapping):
        def create(name):
            factory = mapping.get(name)
            return factory() if callable(factory) else factory
        return patch.object(self.orchestrator, '_retriever', side_effect=create)

    def test_lane_failure_preserves_other_lane_and_trace_is_private(self):
        calls = []
        found = self.candidate('birthday', 'structured', retriever='structured')
        with self.install({'structured': lambda: StubRetriever((found,), calls=calls,
                marker='structured'), 'keyword': lambda: StubRetriever(error=Unavailable(
                'SECRET_QUERY PRIVATE_CONTEXT raw exception'), calls=calls, marker='keyword')}):
            result = self.orchestrator.retrieve(self.request())
        self.assertEqual([item.id for item in result.structured], ['birthday'])
        self.assertTrue(result.degraded)
        trace = json.dumps(result.trace, ensure_ascii=False)
        for private in ('SECRET_QUERY', 'PRIVATE_CONTEXT', 'raw exception', 'PRIVATE_CANDIDATE_CONTENT'):
            self.assertNotIn(private, trace)
        self.assertIn('Unavailable', str(result.trace))

    def test_isolation_failure_rejects_the_whole_retrieval(self):
        first = self.candidate('fact', 'structured', retriever='structured')
        with self.install({'structured': lambda: StubRetriever((first,)),
                           'keyword': lambda: StubRetriever(error=ScopeMismatch('other friend'))}):
            with self.assertRaises(ScopeMismatch):
                self.orchestrator.retrieve(self.request())

    def test_snapshot_change_discards_all_results_without_retry(self):
        calls = []
        candidate = self.candidate('dense-hit', 'memory', retriever='semantic')
        with self.install({'semantic': lambda: StubRetriever((candidate,), calls=calls,
                marker='semantic'), 'keyword': lambda: StubRetriever(calls=calls, marker='keyword')}):
            with patch.object(orchestrator_module, '_snapshot_signature', side_effect=('before', 'after')):
                result = self.orchestrator.retrieve(self.request('像之前那样'))
        self.assertEqual(result.memory, ())
        self.assertTrue(result.degraded)
        self.assertTrue(result.trace['snapshot_changed'])
        self.assertEqual([call[0] for call in calls], ['semantic'])

    def test_route_buckets_and_unknown_namespace_are_separated(self):
        example = self.candidate('e1', 'example', retriever='example')
        persona = self.candidate('p1', 'persona', retriever='persona')
        style = self.candidate('s1', 'style', retriever='persona')
        interest = self.candidate('i1', 'interest', retriever='interest')
        unknown = self.candidate('x1', 'secret_ns', retriever='keyword')
        with self.install({'example': lambda: StubRetriever((example,)),
                'persona': lambda: StubRetriever((persona, style)),
                'interest': lambda: StubRetriever((interest,))}):
            result = self.orchestrator.retrieve(self.request('嘿', scene='daily_chat'))
        self.assertEqual([item.id for item in result.examples], ['e1'])
        self.assertEqual([item.id for item in result.style], ['s1'])
        self.assertEqual([item.id for item in result.interests], ['i1'])
        self.assertIn('example', {item['namespace'] for item in result.trace['candidates']})
        self.assertIn('style', {item['namespace'] for item in result.trace['candidates']})
        with self.install({'keyword': lambda: StubRetriever((unknown,))}):
            default = self.orchestrator.retrieve(self.request())
        self.assertEqual(default.trace['unknown_namespace_count'], 1)
        self.assertEqual(default.structured, ())

    def test_rrf_deduplicates_candidates_across_routes(self):
        topic = self.candidate('same', 'knowledge', retriever='topic', content='months')
        keyword = replace(topic, retriever='keyword', score=0.1)
        with self.install({'topic': lambda: StubRetriever((topic,)),
                           'entity': lambda: StubRetriever(),
                           'keyword': lambda: StubRetriever((keyword,))}):
            result = self.orchestrator.retrieve(self.request('为什么', scene='knowledge'))
        self.assertEqual([item.id for item in result.topics], ['same'])
        self.assertEqual(result.trace['generic_selected'], 1)
        self.assertEqual(len(result.trace['candidates']), 1)

    def test_examples_keep_their_own_order_and_skip_generic_ranker(self):
        examples = [self.candidate('e1', 'example', retriever='example'),
                    self.candidate('e2', 'example', retriever='example')]
        persona = self.candidate('p1', 'persona', retriever='persona')
        original_ranker = rank_candidates
        with self.install({'example': lambda: StubRetriever(examples),
                'persona': lambda: StubRetriever((persona,)),
                'interest': lambda: StubRetriever()}):
            with patch.object(orchestrator_module, 'rank_candidates', wraps=original_ranker) as ranker:
                result = self.orchestrator.retrieve(self.request('嘿', scene='daily_chat'))
        self.assertEqual([item.id for item in result.examples], ['e1', 'e2'])
        self.assertEqual(ranker.call_count, 1)
        ranked_batches = ranker.call_args.args[2]
        self.assertTrue(all(item.namespace != 'example' for batch in ranked_batches
                            for item in batch.candidates))

    def test_total_receive_and_generic_result_caps(self):
        structured = [self.candidate('s' + str(i), 'structured', retriever='structured')
                      for i in range(25)]
        keywords = [self.candidate('k' + str(i), 'structured', retriever='keyword')
                    for i in range(25)]
        with self.install({'structured': lambda: StubRetriever(structured),
                           'keyword': lambda: StubRetriever(keywords)}):
            result = self.orchestrator.retrieve(self.request())
        self.assertEqual(result.trace['total_received'], 50)
        self.assertEqual(result.trace['generic_selected'], 20)
        self.assertEqual(len(result.structured), 20)
        self.assertLessEqual(result.trace['candidate_count'], 50)
        self.assertEqual(len(result.trace['candidates']), 50)
        self.assertEqual(sum(item['selected'] for item in result.trace['candidates']), 20)
        self.assertTrue(all('raw_score' in item and 'rrf' in item and
            'output_rank' in item and 'excluded_by' in item for item in result.trace['candidates']))
        self.assertTrue(all(isinstance(item['rrf'], (int, float)) for item in result.trace['candidates']))

    def test_request_budgets_trim_whole_candidates_and_trace_exclusions(self):
        budgets = ContextBudgets(persona=200, examples=100, knowledge=100)
        topics = [self.candidate('t1', 'knowledge', retriever='topic', content='a' * 70),
                  self.candidate('t2', 'knowledge', retriever='topic', content='b' * 70)]
        with self.install({'topic': lambda: StubRetriever(topics),
                'entity': lambda: StubRetriever(), 'keyword': lambda: StubRetriever()}):
            knowledge = self.orchestrator.retrieve(self.request('为什么', scene='knowledge',
                budgets=budgets))
        self.assertLessEqual(sum(len(item.content.encode('utf-8')) + 24
                                 for item in knowledge.topics), budgets.knowledge)
        self.assertEqual(len(knowledge.topics), 1)

        examples = [self.candidate('e1', 'example', retriever='example', content='e' * 70),
                    self.candidate('e2', 'example', retriever='example', content='f' * 70)]
        persona = self.candidate('p1', 'persona', retriever='persona', content='p' * 70)
        style = self.candidate('s1', 'style', retriever='persona', content='s' * 70)
        with self.install({'example': lambda: StubRetriever(examples),
                'persona': lambda: StubRetriever((persona, style)),
                'interest': lambda: StubRetriever()}):
            result = self.orchestrator.retrieve(self.request('嘿', scene='daily_chat',
                budgets=budgets))
        combined = result.examples + result.persona + result.style
        self.assertLessEqual(sum(len(item.content.encode('utf-8')) + 24 for item in result.examples),
                             budgets.examples)
        self.assertLessEqual(sum(len(item.content.encode('utf-8')) + 24 for item in combined),
                             budgets.persona)
        self.assertTrue(result.trace['budget']['excluded'])
        self.assertTrue(all(item['excluded_by'] in ('examples_budget', 'persona_budget')
                            for item in result.trace['budget']['excluded']))

    def test_unfinished_topic_is_explicitly_not_implemented(self):
        with self.install({'interest': lambda: StubRetriever(), 'temporal': lambda: StubRetriever(),
                'topic': lambda: StubRetriever()}):
            result = self.orchestrator.retrieve(self.request('继续聊点什么', plan='active'))
        pending = [item for item in result.trace['routes'] if item.get('route') == 'unfinished_topic']
        self.assertEqual(len(pending), 1)
        self.assertIn('not_implemented', pending[0]['error_codes'])
        self.assertNotIn('unknown_route', pending[0]['error_codes'])

    def test_skip_and_supplemental_preserve_original_request(self):
        skip_plan = ConversationPlan(action='ignore', intent='reply', tone='casual',
            core_meaning='', ask_question=False, continue_topic=False,
            introduce_new_topic=False, desired_bubbles=1, sticker_intent=None)
        with patch.object(self.orchestrator, '_retriever', side_effect=AssertionError('called')):
            skipped = self.orchestrator.retrieve(self.request('不要召回', plan=skip_plan))
        self.assertEqual(skipped.router.mode, 'skip')
        self.assertEqual(skipped.trace['snapshot_version'], 'skipped')

        plan = ConversationPlan(action='reply', intent='topic', tone='casual',
            core_meaning='months', ask_question=False, continue_topic=False,
            introduce_new_topic=True, desired_bubbles=1, sticker_intent=None)
        original = self.request('继续', plan=plan)
        before = asdict(original)
        calls = []
        with self.install({'topic': lambda: StubRetriever(calls=calls, marker='topic'),
                'entity': lambda: StubRetriever(calls=calls, marker='entity'),
                'keyword': lambda: StubRetriever(calls=calls, marker='keyword')}):
            result = self.orchestrator.retrieve(original)
        self.assertEqual(asdict(original), before)
        self.assertTrue(all(call[1] == 'months' for call in calls))
        self.assertEqual(result.router.mode, 'supplemental')

    def test_missing_sidecar_is_not_created(self):
        sidecar = self.scope.friend_dir / 'persona.sqlite3'
        self.assertFalse(sidecar.exists())
        result = self.orchestrator.retrieve(self.request('继续', scene='knowledge'))
        self.assertTrue(result.degraded)
        self.assertFalse(sidecar.exists())

    def test_public_candidate_in_private_namespace_is_rejected(self):
        pack = PackVersion(pack_id='public', version='v1')
        self.scope = FriendScope.load('12345', project_root=self.scope.project_root,
            allowed_pack_versions=(pack,))
        self.orchestrator = RetrievalOrchestrator(self.scope)
        invalid = self.candidate('leak', 'memory', retriever='example',
            original_scope=DataScope.PUBLIC_PACK, pack_id='public', pack_version='v1')
        with self.install({'example': lambda: StubRetriever((invalid,)),
                           'persona': lambda: StubRetriever(),
                           'interest': lambda: StubRetriever()}):
            with self.assertRaises(ScopeMismatch):
                self.orchestrator.retrieve(self.request('嘿', scene='daily_chat',
                    privacy=DataScope.PUBLIC_PACK, allowed=(pack,)))


if __name__ == '__main__':
    unittest.main()
