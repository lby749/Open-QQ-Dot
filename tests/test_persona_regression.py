"""Offline cross-module regression; all stores are disposable fixtures."""
from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

import embedding_provider
import main
import persona_store
from bubble_planner import BubblePlanner
from persona_evaluation import evaluate, generation_input
from persona_feedback_gui import PersonaFeedbackService, _freeze_comparison
from persona_incremental import IncrementalUpdate
from persona_store import Conflict, PersonaStore, SchemaMismatch, ScopeMismatch, Unavailable
from runtime_context import RuntimeContextBuilder
from semantic_retriever import SemanticRetriever
from tests import (test_bubble_planner as bubbles, test_persona_evaluation as evaluation,
                   test_persona_incremental as incremental,
                   test_retrieval_orchestrator as retrieval,
                   test_runtime_context as context, test_runtime_integration as runtime)


class PersonaRegressionTests(unittest.TestCase):
    def fixture(self, cls):
        case = cls()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_v17_feedback_migration_preserves_catalog_and_online_backup(self):
        case = self.fixture(retrieval.RetrievalOrchestratorTests)
        scope = case.scope
        with patch.object(persona_store, 'MIGRATIONS', persona_store.MIGRATIONS[:17]):
            with closing(PersonaStore(scope.friend_id, scope.friend_dir,
                                      project_root=scope.project_root)) as old:
                old.db.execute('UPDATE scope_state SET revision=7')
                old.db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)',
                    (scope.friend_id, 'existing', 'COMPLETED', 'versions/manifest.json',
                     'a' * 64, context.NOW))
        path = scope.friend_dir / 'persona.sqlite3'
        before = path.read_bytes()
        service = PersonaFeedbackService(scope)
        with self.assertRaises(SchemaMismatch):
            service.list_recent()
        self.assertEqual(path.read_bytes(), before)
        service.initialize_feedback_store()
        self.assertEqual(service.list_versions()[0]['id'], 'existing')
        self.assertEqual(service.list_recent(), ())
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 18)
            self.assertEqual(db.execute('SELECT revision FROM scope_state').fetchone()[0], 7)
        backups = tuple(scope.friend_dir.glob('*.backup.sqlite3'))
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(backups[0])) as db:
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 17)
            self.assertEqual(db.execute('SELECT id FROM artifact_versions').fetchone()[0], 'existing')

    def test_evaluation_to_runtime_hides_target(self):
        case = self.fixture(evaluation.PersonaEvaluationTests)
        prepared = case.prepare()[0]
        generated_input = generation_input(prepared)
        legacy = repr(generated_input)
        private = context.candidate('example', 'PRIVATE_EXAMPLE', friend=case.scope.friend_id)
        request = replace(context.request(), friend_id=case.scope.friend_id)
        built = RuntimeContextBuilder().build(request, context.relevant(examples=(private,)),
            legacy, {'enabled': True, 'provider_kind': 'CLOUD'})
        self.assertTrue(built.prompt_blocks.startswith(legacy))
        self.assertNotIn('TARGET-SENTINEL', built.prompt_blocks)
        self.assertIn('PRIVATE_EXAMPLE', built.prompt_blocks)  # 2026-10-09: private examples allowed in cloud
        metrics = evaluate(prepared, ['generated reply']).metrics
        self.assertEqual(metrics.target_bubbles, 1)
        self.assertEqual(metrics.generated_bubbles, 1)

    def test_bad_migration_hash_is_rejected_without_repair(self):
        case = self.fixture(retrieval.RetrievalOrchestratorTests)
        service = PersonaFeedbackService(case.scope)
        service.initialize_feedback_store()
        path = case.scope.friend_dir / 'persona.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            db.execute("UPDATE schema_migrations SET migration_hash='bad' WHERE version=18")
            db.commit()
        before = path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            service.initialize_feedback_store()
        self.assertEqual(path.read_bytes(), before)

    def test_training_overlap_is_rejected_before_generation_input(self):
        case = self.fixture(evaluation.PersonaEvaluationTests)
        with patch('persona_evaluation.generation_input') as generation:
            with self.assertRaisesRegex(ValueError, 'overlaps training'):
                case.prepare(training=(case.refs[-1],))
            generation.assert_not_called()

    def test_feedback_persists_subjective_metadata_without_candidate_text(self):
        case = self.fixture(retrieval.RetrievalOrchestratorTests)
        service = PersonaFeedbackService(case.scope)
        service.initialize_feedback_store()
        comparison = _freeze_comparison(case.scope.friend_id, 'legacy', 'v1',
                                        'offline_reply', 'PRIVATE_A', 'PRIVATE_B')
        fields = {name: comparison[name] for name in
                  ('comparison_id', 'a_version', 'b_version', 'comparison_kind')}
        first = service.save_feedback(**fields, choice='B')
        self.assertEqual(service.save_feedback(**fields, choice='B'), first)
        self.assertEqual(first['source_type'], 'HUMAN_SUBJECTIVE')
        self.assertNotIn('PRIVATE_', repr(service.list_recent()))
        with self.assertRaises(Conflict):
            service.save_feedback(**fields, choice='A')

    def test_disabled_legacy_bytes_and_no_sidecar_or_model_components(self):
        case = self.fixture(runtime.RuntimeIntegrationTests)
        case.profile['feature_flags'] = dict.fromkeys(
            ('persona_enabled', 'hybrid_enabled', 'sticker_metadata_enabled'), False)
        with patch('runtime_context.RuntimeContextBuilder') as builder, \
                patch('retrieval_orchestrator.RetrievalOrchestrator') as orchestrator, \
                patch('persona_pack_bindings.PackBindingStore') as packs:
            text, evidence = case.context()
        self.assertEqual(text.encode('utf-8'), b'LEGACY')
        builder.assert_not_called()
        orchestrator.assert_not_called()
        packs.assert_not_called()
        case.embedding.assert_not_called()
        self.assertFalse((case.directory / 'persona.sqlite3').exists())

    def test_each_disabled_lane_is_not_constructed(self):
        case = self.fixture(runtime.RuntimeIntegrationTests)
        for persona, hybrid, expected in ((True, False, {'example', 'persona'}),
                                          (False, True, {'interest'}),
                                          (False, False, set())):
            with self.subTest(persona=persona, hybrid=hybrid):
                case.profile['feature_flags'] = {'persona_enabled': persona, 'hybrid_enabled': hybrid}
                with patch.object(retrieval.RetrievalOrchestrator, '_retriever', return_value=None) as lane:
                    text, _ = case.context()
                self.assertEqual({call.args[0] for call in lane.call_args_list}, expected)
                self.assertEqual(text, 'LEGACY')

    def test_embedding_load_failure_keeps_keyword_and_legacy_without_private_trace(self):
        case = self.fixture(retrieval.RetrievalOrchestratorTests)
        provider = embedding_provider.EmbeddingProvider(case.scope)
        found = case.candidate('fallback', 'memory', content='KEEP_MEMORY')
        semantic = SemanticRetriever(case.scope, provider, source_refs=(found.source,))
        index = unittest.mock.Mock()
        index.dense.side_effect = lambda request, limit, source_refs: provider.embed_query(
            request.query, source_refs=source_refs)
        with patch('house_settings.local_models_enabled', return_value=True), \
                patch.object(embedding_provider, '_WORKER', None), \
                patch.object(embedding_provider, '_load_backend', side_effect=Unavailable('PRIVATE_CUDA_ERROR')) as load, \
                patch('semantic_retriever.SemanticIndex', return_value=index), \
                case.install({'semantic': semantic, 'keyword': retrieval.StubRetriever((found,))}):
            result = case.orchestrator.retrieve(case.request('类似感觉'))
            self.assertTrue(result.degraded)
            self.assertEqual(tuple(item.id for item in result.memory), ('fallback',))
            self.assertEqual(load.call_count, 1)
        index.close.assert_called_once()
        self.assertNotIn('PRIVATE_CUDA_ERROR', repr(result.trace))
        built = RuntimeContextBuilder().build(case.request(), result, 'LEGACY', {'enabled': True})
        self.assertIn('KEEP_MEMORY', built.prompt_blocks)
        self.assertTrue(built.prompt_blocks.startswith('LEGACY'))

    def test_scope_error_never_becomes_legacy_fallback_or_model_call(self):
        case = self.fixture(runtime.RuntimeIntegrationTests)
        case.profile['feature_flags'] = {'hybrid_enabled': True}
        with patch.object(retrieval.RetrievalOrchestrator, 'retrieve', side_effect=ScopeMismatch('wrong friend')), \
                patch.object(main, 'codex_text') as model:
            with self.assertRaises(ScopeMismatch):
                main.make_reply(case.store, case.profile, case.directory)
            model.assert_not_called()

    def test_optional_lane_failures_preserve_available_context(self):
        case = self.fixture(retrieval.RetrievalOrchestratorTests)
        # Exercise the real queue/fallback/context builder, with only lane IO replaced.
        for route, fallback, namespace in (('structured', None, None),
                                            ('persona', 'memory', 'memory'),
                                            ('example', 'persona', 'persona'),
                                            ('sticker', None, None)):
            with self.subTest(route=route):
                request = case.request()
                decision = replace(case.orchestrator.router.route(request), retrievers=(route,))
                lanes = {route: retrieval.StubRetriever(error=Unavailable('PRIVATE_FAILURE'))}
                if fallback:
                    lanes[fallback] = retrieval.StubRetriever((case.candidate(
                        'surviving', namespace, retriever=fallback, content='KEEP_CONTEXT'),))
                with patch.object(case.orchestrator.router, 'route', return_value=decision), case.install(lanes):
                    result = case.orchestrator.retrieve(request)
                self.assertTrue(result.degraded)
                self.assertNotIn('PRIVATE_FAILURE', repr(result.trace))
                built = RuntimeContextBuilder().build(request, result, 'LEGACY', {'enabled': True})
                self.assertTrue(built.prompt_blocks.startswith('LEGACY'))
                self.assertEqual('KEEP_CONTEXT' in built.prompt_blocks, bool(fallback))

    def test_model_failure_keeps_source_history_and_does_not_retry(self):
        case = self.fixture(runtime.RuntimeIntegrationTests)
        case.profile['feature_flags'] = {'hybrid_enabled': True}
        before = tuple(dict(row) for row in case.store.db.execute('SELECT * FROM messages'))
        with patch.object(retrieval.RetrievalOrchestrator, 'retrieve', side_effect=Unavailable('offline')), \
                patch.object(main, 'codex_text', side_effect=RuntimeError('model unavailable')) as model:
            with self.assertRaisesRegex(RuntimeError, 'model unavailable'):
                main.make_reply(case.store, case.profile, case.directory)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(before, tuple(dict(row) for row in case.store.db.execute('SELECT * FROM messages')))
        self.assertFalse((case.directory / 'persona.sqlite3').exists())

    def test_token_budget_removes_whole_example_then_denies_protected_overflow(self):
        private = context.candidate('example', 'whole example')
        legacy = '当前问题与完整最近上下文'
        cap = len(legacy.encode('utf-8'))
        built = RuntimeContextBuilder().build(context.request(), context.relevant(examples=(private,)),
            legacy, {'enabled': True, 'provider_kind': 'LOCAL', 'max_context_tokens': cap})
        self.assertEqual(built.prompt_blocks, legacy)
        self.assertEqual(built.token_usage['total'], cap)
        self.assertTrue(built.estimate)
        self.assertTrue(built.trace['generation_allowed'])
        self.assertIn('global_examples_removed', built.trace['reason_codes'])
        overflow = RuntimeContextBuilder().build(context.request(), context.relevant(), legacy,
            {'enabled': True, 'max_context_tokens': cap - 1})
        self.assertEqual(overflow.prompt_blocks, legacy)
        self.assertFalse(overflow.trace['generation_allowed'])

    def test_bubble_identity_and_uncertain_generation_rejects_send(self):
        case = self.fixture(runtime.RuntimeIntegrationTests)
        snapshot = bubbles.snapshot(case.root)
        plan = BubblePlanner().plan({'messages': ['one', 'two'], 'friend_id': '54321',
                                     'generation': 99}, None, {}, snapshot)
        self.assertEqual((plan.friend_id, plan.generation), ('12345', 7))
        from send_validator import ReplyPlan, ReplySnapshot, ReplySendState, SendValidator
        trusted = ReplySnapshot('12345', str(snapshot.friend_dir), '99999', 7, 'source-1')
        legacy = ReplyPlan(trusted, plan.bubbles)
        state = ReplySendState('12345', str(snapshot.friend_dir), '99999', '99999', 7,
            {'qq_id': '12345', 'bridge_enabled': True, 'auto_reply': True}, 'processing', 0)
        self.assertTrue(SendValidator.validate(legacy, trusted, state, {}).allowed)
        for changed in (replace(state, current_generation=8),
                        replace(state, attempt_state='uncertain', successful_bubbles=1)):
            self.assertFalse(SendValidator.validate(legacy, trusted, changed, {}).allowed)

    def test_incremental_stale_batch_rejects_and_replay_is_idempotent(self):
        case = self.fixture(incremental.IncrementalTests)
        case.message()
        stale = case.update.prepare()
        case.message('two')
        with self.assertRaises(Conflict):
            case.update.commit(stale)
        self.assertEqual(case.history.db.execute('SELECT COUNT(*) FROM incremental_receipts').fetchone()[0], 0)
        batch = case.update.prepare()
        receipt = case.update.commit(batch)
        self.assertEqual(case.update.commit(batch), receipt)
        self.assertIsNone(IncrementalUpdate(case.scope, case.history).process())
        self.assertEqual(receipt['count'], 2)
        projections = tuple(dict(row) for row in case.history.db.execute('SELECT * FROM incremental_projections'))
        self.assertNotIn('private body', json.dumps(projections))
        self.assertEqual(len(projections), 2)


if __name__ == '__main__':
    unittest.main()
