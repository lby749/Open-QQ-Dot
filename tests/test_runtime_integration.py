from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import app_server
import main
import prompt_context
from persona_store import ScopeMismatch, Unavailable
from persona_types import Candidate, DataScope, FriendScope, PackVersion, PrivacyView, RetrievalBatch, SourceOrigin, SourceRef
from retrieval_orchestrator import RetrievalOrchestrator
from store import FriendStore


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.directory.mkdir(parents=True)
        self.profile = {'qq_id': '12345', 'style': '自然短句', 'daily_research_enabled': False}
        (self.directory / 'friend.json').write_text(json.dumps(self.profile), 'utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.directory)}}}), 'utf-8')
        env = patch.dict(os.environ, QQ_BRIDGE_HOME=str(self.root))
        env.start()
        self.addCleanup(env.stop)
        classifier = patch('message_classification.enqueue_for_database')
        classifier.start()
        self.addCleanup(classifier.stop)
        embedding = patch('embedding_provider.EmbeddingProvider')
        self.embedding = embedding.start()
        self.addCleanup(embedding.stop)
        self.store = FriendStore(self.directory)
        self.addCleanup(self.store.close)
        self.store.add_message('one', 'friend', '晚上好')
        for name in ('handoff_if_due', 'refresh_summary'):
            mock = patch.object(main, name)
            mock.start()
            self.addCleanup(mock.stop)

    def candidate(self, namespace, friend_id='12345'):
        source = SourceRef(dataset_id='manual:test', import_id='fixture', record_id=namespace,
            message_seq=None, event_time=None,
            speaker_id='operator', origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=friend_id,
            recorded_at='2026-10-07T00:00:00+00:00')
        return Candidate(id=namespace, namespace=namespace, type='fixture', content='SUPPLEMENT_' + namespace,
            score=0.9, source=source, reason='fixture', retriever=namespace,
            friend_id=friend_id, timestamp=None, confidence=0.9, original_scope=DataScope.PRIVATE_FRIEND,
            pack_version=None)

    def context(self):
        return prompt_context.reply_runtime_context(self.profile, self.directory, '晚上好', 'RECENT', 'LEGACY')

    def test_consolidated_current_turn_reaches_actual_model_prompt(self):
        """Never acknowledge a multi-message batch if the model only sees its last text."""
        self.store.add_message('second', 'friend', 'Could you investigate error 42?')
        self.profile['daily_research_enabled'] = False
        merged = '[User sent 2 messages]:\n1. Please investigate the release issue\n2. Could you investigate error 42?'
        with patch.object(main, 'codex_text', return_value=('{"messages":["I will inspect both issues."]}', None)) as model:
            answer = main.make_reply(self.store, self.profile, self.directory,
                                     target_message_id='second', consolidated_body=merged)
        self.assertEqual(answer, ['I will inspect both issues.'])
        prompt = model.call_args.args[0]
        self.assertIn('BATCH_USER_MESSAGES:', prompt)
        self.assertIn('Please investigate the release issue', prompt)
        self.assertIn('Could you investigate error 42?', prompt)

    def test_disabled_calls_no_components(self):
        # 2026-10-10: the prompt was reorganized on purpose, so it is no longer byte-compared with RUNTIME-003
        self.profile['feature_flags'] = dict.fromkeys(
            ('persona_enabled', 'hybrid_enabled', 'sticker_metadata_enabled'), False)
        with patch.object(main, 'codex_text', return_value=('okay!', None)), \
                patch('runtime_context.RuntimeContextBuilder') as builder, \
                patch('retrieval_orchestrator.RetrievalOrchestrator') as retrieval, \
                patch('persona_pack_bindings.PackBindingStore') as bindings:
            main.make_reply(self.store, self.profile, self.directory)
            builder.assert_not_called()
            retrieval.assert_not_called()
            bindings.assert_not_called()
            self.embedding.assert_not_called()

    def test_enabled_passes_local_provider_and_persisted_current_source_without_model_load(self):
        self.profile['feature_flags'] = {'hybrid_enabled': True}
        self.store.add_message('two', 'friend', '以前聊过什么')
        seen = []

        def retrieve(orchestrator, request):
            seen.append((orchestrator.embedding_provider, orchestrator.source_refs, request))
            raise Unavailable('offline fixture')

        with patch.object(RetrievalOrchestrator, 'retrieve', retrieve), \
                patch('embedding_provider._load_backend') as load, \
                patch('semantic_index.SemanticIndex.begin_generation') as rebuild, \
                patch.object(main, 'codex_text', return_value=('okay!', None)):
            main.make_reply(self.store, self.profile, self.directory)
        self.embedding.assert_called_once()
        self.assertEqual(len(seen), 1)
        provider, refs, request = seen[0]
        self.assertIs(provider, self.embedding.return_value)
        self.assertEqual(request.query, '以前聊过什么')
        row = self.store.db.execute("SELECT * FROM messages WHERE qq_id='two'").fetchone()
        self.assertEqual(refs[0].message_seq, row['seq'])
        self.assertEqual(refs[0].event_time, row['created_at'])
        self.assertEqual(refs[0].friend_id, '12345')
        self.assertEqual(refs[0].origin, SourceOrigin.REAL_RELATIONSHIP)
        load.assert_not_called()
        rebuild.assert_not_called()
        provider.embed_texts.assert_not_called()

    def test_missing_current_source_degrades_dense_without_loading_provider(self):
        self.profile['feature_flags'] = {'hybrid_enabled': True}
        with patch.object(RetrievalOrchestrator, '_retriever', return_value=None):
            text, evidence = self.context()
        self.embedding.assert_not_called()
        self.assertIn('embedding_query_source_unavailable', evidence['reason_codes'])

    def test_namespace_filters_skip_disabled_retriever_construction(self):
        for persona, hybrid, expected in ((True, False, ['example', 'persona']),
                                         (False, True, ['interest']),
                                         (True, True, ['example', 'persona', 'interest'])):
            with self.subTest(persona=persona, hybrid=hybrid):
                self.profile['feature_flags'] = {'persona_enabled': persona, 'hybrid_enabled': hybrid}
                loaded = []

                class Lane:
                    def __init__(inner, route):
                        inner.route = route

                    def retrieve(inner, request, limit):
                        namespace = {'example': 'example', 'persona': 'persona', 'interest': 'interest'}[inner.route]
                        return RetrievalBatch(candidates=(self.candidate(namespace),), elapsed_ms=0)

                def retriever(orchestrator, route):
                    loaded.append(route)
                    return Lane(route)

                with patch.object(RetrievalOrchestrator, '_retriever', retriever):
                    text, evidence = self.context()
                self.assertEqual(loaded, expected)
                self.assertTrue(text.startswith('LEGACY\n\n'))
                self.assertEqual(text.count('LEGACY'), 1)
                # 2026-10-09: private Examples may reach the cloud model (only when retrieved).
                self.assertEqual('SUPPLEMENT_example' in text, persona)
                self.assertEqual('SUPPLEMENT_persona' in text, persona)
                self.assertEqual('SUPPLEMENT_interest' in text, hybrid)
                self.assertTrue(evidence['estimate'])
                self.assertEqual(evidence['estimate_kind'], 'utf8_bytes_proxy')
                self.assertEqual(evidence['estimate_scope'], 'retrieval_context_only')
        self.assertFalse((self.directory / 'persona.sqlite3').exists())

    def test_sticker_only_degrades_without_retrieval(self):
        self.profile['feature_flags'] = {'sticker_metadata_enabled': True}
        with patch('retrieval_orchestrator.RetrievalOrchestrator') as retrieval:
            text, evidence = self.context()
        self.assertEqual(text, 'LEGACY')
        self.assertEqual(evidence['reason_codes'], ['sticker_retrieval_unavailable'])
        retrieval.assert_not_called()

    def test_public_pack_request_keeps_exact_allowlist_and_cloud_examples(self):
        self.profile['feature_flags'] = {'persona_enabled': True}
        pack = PackVersion(pack_id='fixture', version='v1')
        scope = FriendScope.load('12345', project_root=self.root, allowed_pack_versions=(pack,))
        item = self.candidate('example')
        item = replace(item, original_scope=DataScope.PUBLIC_PACK, pack_version='v1',
            source=replace(item.source, data_scope=DataScope.PUBLIC_PACK, friend_id=None,
                           pack_id='fixture', pack_version='v1', origin=SourceOrigin.PUBLIC_DATASET))
        requests = []

        class Lane:
            def retrieve(inner, request, limit):
                requests.append(request)
                return RetrievalBatch(candidates=(item,) if request.filters.privacy_scope == DataScope.PUBLIC_PACK
                                      and 'example' in request.filters.namespace else (), elapsed_ms=0)

        with patch('persona_pack_bindings.PackBindingStore') as bindings, \
                patch.object(RetrievalOrchestrator, '_retriever', return_value=Lane()):
            bindings.return_value.bound_scope.return_value = scope
            text, evidence = self.context()
        bindings.assert_called_once()
        self.assertTrue(bindings.call_args.kwargs['readonly'])
        self.assertIn('SUPPLEMENT_example', text)
        public = [request for request in requests if request.filters.privacy_scope == DataScope.PUBLIC_PACK]
        self.assertTrue(public)
        self.assertTrue(all(request.allowed_pack_versions == frozenset((pack,)) for request in public))
        self.assertTrue(all(set(request.filters.namespace) <= {'persona', 'example', 'style'} for request in public))

    def test_sticker_does_not_block_other_enabled_sections(self):
        self.profile['feature_flags'] = {'hybrid_enabled': True, 'sticker_metadata_enabled': True}
        with patch.object(RetrievalOrchestrator, '_retriever', return_value=None):
            text, evidence = self.context()
        self.assertEqual(text, 'LEGACY')
        self.assertIn('sticker_retrieval_unavailable', evidence['reason_codes'])

    def test_hard_scope_failure_and_oversize_deny_generation(self):
        self.profile['feature_flags'] = {'hybrid_enabled': True}
        with patch.object(RetrievalOrchestrator, 'retrieve', side_effect=ScopeMismatch('fixture')), \
                patch.object(main, 'codex_text') as model:
            with self.assertRaises(ScopeMismatch):
                main.make_reply(self.store, self.profile, self.directory)
            model.assert_not_called()
        self.profile['runtime_context'] = {'max_context_tokens': 1}
        with patch.object(RetrievalOrchestrator, 'retrieve', side_effect=Unavailable('fixture')), \
                patch.object(main, 'codex_text') as model:
            with self.assertRaisesRegex(RuntimeError, '超出预算'):
                main.make_reply(self.store, self.profile, self.directory)
            model.assert_not_called()

    def server(self, response, sent):
        server = app_server.CodexAppServer(1000)
        server.start = lambda: None
        server.close = lambda: None

        def request(method, params, deadline):
            if method in ('thread/start', 'thread/resume'):
                return {'thread': {'id': 'fixture', 'cwd': str(self.directory)}}
            if method == 'turn/start':
                record = json.loads(self.store.get('last_generation_input'))
                self.assertEqual(record['prompt'], params['input'][0]['text'])
                sent.append((params['input'][0]['text'], record))
                return {'turn': {'id': 'turn'}}
            return {}

        server._request = request
        events = []

        def next_event(deadline):
            if not events:
                events.extend([
                    {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': response(len(sent))}}},
                    {'method': 'turn/completed', 'params': {'turn': {'id': 'turn', 'status': 'completed'}}}])
            return events.pop(0)

        server._next = next_event
        return server

    def test_actual_provider_text_budget_and_lookup_retry_record(self):
        sent = []
        server = self.server(lambda count: json.dumps({'messages': ['okay!'], 'lookup_needed': count == 1}), sent)
        with patch.object(main, 'SERVER', server), patch.object(main, 'search_budget', return_value=1), \
                patch.object(app_server, 'search_budget', side_effect=[3, 0]) as budget, \
                patch.object(main, 'conversation_context', return_value='UNIQUE_LEGACY'):
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['okay!'])
        self.assertEqual(budget.call_count, 2)
        self.assertEqual(len(sent), 2)
        # the retry continues the same thread, so the unchanged context is not sent again
        for (text, record), remaining, legacy in zip(sent, (3, 0), (1, 0)):
            self.assertEqual(text.count('UNIQUE_LEGACY'), legacy)
            self.assertEqual(text.count('【TASK】'), 1)
            self.assertEqual(text.count('【WEB_SEARCH_BUDGET】'), 1)
            self.assertEqual(record['characters'], len(text))
            self.assertEqual(record['evidence']['search_budget']['remaining'], remaining)
            self.assertEqual(record['evidence']['search_budget']['source'], 'history.sqlite3.metadata.web_search_rounds')
        self.assertEqual(sent[1][1]['mode'], 'normal reply () ')
        self.assertIn('RETRY_LOOKUP', sent[1][0])

    def test_record_failure_prevents_turn_start(self):
        sent = []
        server = self.server(lambda count: 'okay!', sent)
        with patch.object(app_server, 'search_budget', return_value=2):
            with self.assertRaisesRegex(RuntimeError, 'record failed'):
                server.generate('prompt', self.directory, None,
                    record_input=lambda text, evidence: (_ for _ in ()).throw(RuntimeError('record failed')))
        self.assertEqual(sent, [])

    def test_enabled_lookup_retry_keeps_retrieval_context_once(self):
        self.profile['feature_flags'] = {'persona_enabled': True}
        sent = []
        server = self.server(lambda count: json.dumps({'messages': ['okay!'], 'lookup_needed': count == 1}), sent)

        class Lane:
            def retrieve(inner, request, limit):
                return RetrievalBatch(candidates=(self.candidate('persona'),)
                                      if 'persona' in request.filters.namespace else (), elapsed_ms=0)

        with patch.object(main, 'SERVER', server), patch.object(main, 'search_budget', return_value=1), \
                patch.object(app_server, 'search_budget', side_effect=[3, 0]), \
                patch.object(main, 'conversation_context', return_value='UNIQUE_LEGACY'), \
                patch.object(RetrievalOrchestrator, '_retriever', return_value=Lane()):
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['okay!'])
        self.assertEqual(len(sent), 2)
        for (text, record), once in zip(sent, (1, 0)):  # the retry resumes the thread
            self.assertEqual(text.count('UNIQUE_LEGACY'), once)
            self.assertEqual(text.count('SUPPLEMENT_persona'), once)
            self.assertEqual(text.count('【TASK】'), 1)
            self.assertEqual(record['evidence']['runtime']['estimate_scope'], 'retrieval_context_only')

    def test_full_provider_input_oversize_stops_before_turn_start(self):
        self.profile['feature_flags'] = {'hybrid_enabled': True}
        self.profile['runtime_context'] = {'max_context_tokens': 100}
        sent = []
        server = self.server(lambda count: 'okay!', sent)
        with patch.object(main, 'SERVER', server), patch.object(app_server, 'search_budget', return_value=2), \
                patch.object(main, 'conversation_context', return_value='LEGACY'), \
                patch.object(RetrievalOrchestrator, 'retrieve', side_effect=Unavailable('fixture')):
            with self.assertRaisesRegex(RuntimeError, '完整输入超出预算'):
                main.make_reply(self.store, self.profile, self.directory)
        self.assertEqual(sent, [])

    def test_deepseek_records_initial_text_without_codex_budget(self):
        self.profile['model_api'] = {'provider': 'deepseek'}
        with patch.object(main, 'deepseek_generate', return_value='okay!') as provider, \
                patch.object(app_server, 'search_budget') as budget:
            self.assertEqual(main.make_reply(self.store, self.profile, self.directory), ['okay!'])
        record = json.loads(self.store.get('last_generation_input'))
        self.assertEqual(record['prompt'], provider.call_args.args[0])
        self.assertIsNone(record['evidence']['search_budget'])
        self.assertFalse(record['evidence']['tool_followups_recorded'])
        budget.assert_not_called()


if __name__ == '__main__':
    unittest.main()
