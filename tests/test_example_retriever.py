from contextlib import closing
from dataclasses import asdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import example_retriever
from persona_pack_bindings import PackBindingStore
from persona_store import Conflict, ScopeMismatch
from persona_types import (ContextBudgets, DataScope, EmbeddingBatch, EmbeddingMetadata,
    FriendScope, InvalidSource, PrivacyView, RetrievalFilters, RetrievalRequest,
    SourceOrigin, SourceRef, Unavailable)
from persona_versions import EXAMPLE_COLUMNS, FILES
from semantic_index import SemanticIndex
from example_retriever import ExampleRetriever


NOW = '2026-10-06T00:00:00+00:00'


class MockEmbedding:
    def __init__(self):
        self.metadata = EmbeddingMetadata(model='mock-local', dimension=2, version='v1')
        self.query_calls = []
        self.zero_query = False

    def embed_texts(self, texts, *, source_refs, expected_metadata):
        return EmbeddingBatch(vectors=tuple((0.3, 0.953939201) if '弱样本' in text else
            ((1., 0.) if '旅行' in text else (0., 1.))
            for text in texts), model=self.metadata.model, dimension=2, version=self.metadata.version)

    def embed_query(self, text, *, source_refs, expected_metadata):
        self.query_calls.append(text)
        vector = ((0., 0.) if self.zero_query or 'daily_chat' in text else
                  (0.1, 0.994987437) if '弱相关' in text else
                  (-0.809016994, 0.587785252) if 'medium' in text else
                  (0.809016994, 0.587785252) if '强依赖' in text else
                  ((1., 0.) if '旅行' in text else (0., 1.)))
        return EmbeddingBatch(vectors=(vector,), model=self.metadata.model,
            dimension=2, version=self.metadata.version)


class ExampleRetrieverIntegrationTests(unittest.TestCase):
    def test_joint_filter_bubbles_low_relevance_and_query_only_embedding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
            with closing(sqlite3.connect(friend / 'history.sqlite3')) as db, db:
                db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                    'CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,body TEXT,created_at TEXT);')
                db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', '12345'))
                db.execute('INSERT INTO messages VALUES(1,?,?,?,?)',
                    ('in-1', 'friend', '旅行回忆', NOW))
            scope = FriendScope.load('12345', project_root=root)

            refs = (SourceRef(dataset_id='fixture', import_id='run1', record_id='user',
                message_seq=None, speaker_id='human', origin=SourceOrigin.PUBLIC_DATASET,
                data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id=None, event_time=None, recorded_at=NOW, license_id='CC-BY',
                model_analysis_allowed=True),)
            pack_id, version = 'example-pack', 'v1'
            pack_dir = root / 'data' / 'persona_packs' / pack_id / 'versions' / version
            pack_dir.mkdir(parents=True)
            profile = {'pack_id': pack_id, 'version': version, 'scope': 'PUBLIC_PACK',
                       'friend_id': None, 'claims': []}
            style = dict(profile, observed={})
            for name, value in ((FILES[0], profile), (FILES[1], style)):
                (pack_dir / name).write_text(json.dumps(value), 'utf-8')
            for name in FILES[2:4]:
                (pack_dir / name).write_text('derived fixture', 'utf-8')

            def bubble(identifier, text, ref, message_type='text', media_id=None, target_role='target'):
                return {'id': identifier, 'text': text, 'message_type': message_type,
                    'media_id': media_id, 'source': asdict(ref), 'target_role': target_role}

            user = [bubble('u1', '旅行回忆很开心', refs[0], target_role='input')]
            context = [{'id': 'turn-private-id', 'session_id': 'session-private-id',
                'speaker_id': 'speaker-private-id', 'previous_turn_id': 'previous-private-id',
                'speaker_role': 'real-name-role', 'event_time': NOW, 'timestamp': NOW, 'ordinal': 7,
                'bubbles': [bubble('c1', '以前也聊过旅行', refs[0], target_role='input')]},
                {'id': 'second-turn-private-id', 'session_id': 'session-private-id',
                 'speaker_id': 'second-speaker-private-id', 'previous_turn_id': 'turn-private-id',
                 'event_time': NOW, 'ordinal': 8,
                 'bubbles': [bubble('c2', '后来又说了一次', refs[0], target_role='target')]}]
            targets = [bubble('t1', '真的好想再去', refs[0]),
                       bubble('t2', '那里的海边很美', refs[0], 'sticker', 'private-media-target')]
            example_rows = [('keep', 'daily_chat', 'happy'), ('wrong-scene', 'sad_chat', 'sad')]
            example_rows.extend((f'fixture-weak-{index}', 'casual_chat', 'happy') for index in range(1, 22))
            with closing(sqlite3.connect(pack_dir / 'examples.db')) as db, db:
                db.execute('CREATE TABLE examples (' + ','.join(column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
                for identifier, scene, emotion in example_rows:
                    example_user = ([bubble('u1', '弱样本旅行回忆很开心', refs[0], target_role='input')]
                                    if identifier.startswith('fixture-weak-') else user)
                    db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                        (identifier, json.dumps(example_user), json.dumps(context), json.dumps(targets), scene, emotion, 'music',
                         'empathic', 'PUBLIC_DATASET', None, json.dumps(['familiar']),
                         json.dumps([asdict(refs[0])]), version))
            manifest = dict(profile, source_refs=[asdict(refs[0])], selected_pair_ids=[row[0] for row in example_rows],
                sources=[{'dataset_id': 'fixture', 'local_use_allowed': True,
                    'derived_stats_allowed': True, 'license_data': 'CC-BY', 'raw_export_allowed': False}],
                raw_export_allowed=False,
                files={name: hashlib.sha256((pack_dir / name).read_bytes()).hexdigest() for name in FILES})
            (pack_dir / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
            bindings = PackBindingStore(scope)
            self.addCleanup(bindings.close)
            bindings.bind(pack_id, version)
            scope = bindings.bound_scope()
            provider = MockEmbedding()
            index = SemanticIndex(scope, embedding_provider=provider)
            self.addCleanup(index.close)
            source_ref = SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=1,
                speaker_id='12345', origin=SourceOrigin.REAL_RELATIONSHIP,
                data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                friend_id='12345', event_time=NOW, recorded_at=NOW)
            revision = index.store.revision
            generation = index.begin_generation(0, 'example-fixture', provider.metadata)
            index.write_batch(generation, [
                {'kind': 'example', 'pack_id': pack_id, 'version': version, 'id': identifier}
                for identifier, _, _ in example_rows],
                {}, revision)
            index.activate(generation, 0, revision, 'example-fixture')

            request = RetrievalRequest(friend_id='12345', query='旅行回忆', recent_context='',
                scene='daily_chat', plan=None,
                filters=RetrievalFilters(namespace=('example',), topic='music', emotion=('happy',),
                    relationship_tags=('familiar',)), budgets=ContextBudgets(),
                allowed_pack_versions=scope.allowed_pack_versions, now=NOW)
            # User tiers (2026-10-09, shrunk to save tokens): ordinary <=3, medium <=6, complex <=12 examples.
            for scene, cap in (('daily_chat', 3), ('greeting', 3), ('unknown', 3), ('any', 3),
                               ('study_help', 6), (' Concrete_Question ', 6), ('celebration', 6),
                               ('emotion_low', 12), ('conflict', 12), ('misunderstanding', 12)):
                self.assertEqual(example_retriever._scene_cap(replace(request, scene=scene)), cap)
            self.assertEqual(example_retriever.scene_budget('conflict'), 12 * 500)
            retriever = ExampleRetriever(scope, provider, source_refs=(source_ref,))
            self.addCleanup(retriever.close)
            result = retriever.retrieve(request, 20)
            self.assertEqual(len(result.candidates), 1)
            self.assertEqual(result.candidates[0].id, 'keep')
            content = json.loads(result.candidates[0].content)
            self.assertEqual(content['user'], [{'text': user[0]['text'], 'message_type': 'text'}])
            self.assertEqual(content['context'], [
                {'bubbles': [{'text': context[0]['bubbles'][0]['text'], 'message_type': 'text'}], 'role': 'input'},
                {'bubbles': [{'text': context[1]['bubbles'][0]['text'], 'message_type': 'text'}], 'role': 'target'}])
            self.assertEqual(content['target_bubbles'], [
                {'text': targets[0]['text'], 'message_type': 'text'},
                {'text': targets[1]['text'], 'message_type': 'sticker', 'has_media': True}])
            forbidden_fields = {'id', 'source', 'media_id', 'session_id', 'speaker_id',
                'previous_turn_id', 'event_time', 'ordinal', 'timestamp', 'source_refs',
                'source_locator', 'locator', 'target_role', 'speaker_role'}

            def assert_no_internal_fields(value):
                if isinstance(value, dict):
                    self.assertTrue(forbidden_fields.isdisjoint(value))
                    for nested in value.values():
                        assert_no_internal_fields(nested)
                elif isinstance(value, list):
                    for nested in value:
                        assert_no_internal_fields(nested)

            assert_no_internal_fields(content)
            self.assertEqual(content['scene'], 'daily_chat')
            self.assertEqual(content['emotion'], 'happy')
            self.assertEqual(content['topic'], 'music')
            self.assertEqual(content['reaction_type'], 'empathic')
            self.assertEqual(content['source_origin'], 'PUBLIC_DATASET')
            self.assertEqual(content['relationship_tags'], ['familiar'])
            self.assertEqual(len(provider.query_calls), 1)

            weak = retriever.retrieve(replace(request, query='弱相关'), 20)
            self.assertEqual(weak.candidates, ())

            tiny_budget = ContextBudgets(persona=100, examples=1, knowledge=0)
            under_budget = retriever.retrieve(replace(request, budgets=tiny_budget), 20)
            self.assertEqual(under_budget.candidates, ())

            provider.zero_query = True
            fallback = retriever.retrieve(request, 20)
            self.assertEqual([item.id for item in fallback.candidates], ['keep'])
            self.assertEqual(len(provider.query_calls), 4)
            provider.zero_query = False

            large_budget = ContextBudgets(persona=20000, examples=20000, knowledge=0)
            broad_request = replace(request, query='旅行回忆', scene='any', budgets=large_budget)
            with patch.object(example_retriever, '_scene_cap', return_value=8):  # relevance filtering, not the tier cap
                high_quality = retriever.retrieve(broad_request, 20)
                lower_quality = retriever.retrieve(replace(broad_request, query='medium'), 20)
            self.assertEqual(len(high_quality.candidates), 6)
            self.assertEqual(len(lower_quality.candidates), 5)

            strong_dependency_request = replace(broad_request, query='强依赖',
                filters=replace(broad_request.filters, emotion=()))
            with patch.object(example_retriever, '_scene_cap', return_value=48):  # complex-scene tier
                strong_dependency = retriever.retrieve(strong_dependency_request, 20)
            self.assertEqual(len(strong_dependency.candidates), 20)

            real_rank = example_retriever.rank_candidates

            def weak_first_rank(*args, **kwargs):
                ranked = real_rank(*args, **kwargs)
                weak = [item for item in ranked.candidates if item.id.startswith('fixture-weak-')]
                strong = [item for item in ranked.candidates if item.id == 'keep']
                self.assertTrue(weak)
                self.assertTrue(strong)
                return replace(ranked, candidates=tuple(weak[:9] + strong[:1]))

            with patch.object(example_retriever, 'rank_candidates', side_effect=weak_first_rank), \
                    patch.object(example_retriever, '_scene_cap', return_value=8):
                weak_first = retriever.retrieve(broad_request, 20)
            self.assertEqual(len(weak_first.candidates), 6)
            self.assertEqual(weak_first.candidates[0].id, 'keep')
            self.assertEqual(sum(item.id.startswith('fixture-weak-') for item in weak_first.candidates), 5)

            fts_only = ExampleRetriever(scope, None, source_refs=())
            self.addCleanup(fts_only.close)
            with patch.object(example_retriever, '_scene_cap', return_value=8):
                lexical = fts_only.retrieve(broad_request, 20)
            self.assertEqual(len(lexical.candidates), 5)
            self.assertTrue(lexical.degraded)

            metadata_only = retriever.retrieve(replace(broad_request, query='daily_chat'), 20)
            self.assertEqual(metadata_only.candidates, ())

            calls_before_head_change = len(provider.query_calls)
            with patch.object(retriever, '_head_identity', return_value=('changed-generation', 999)):
                changed_head = retriever.retrieve(broad_request, 20)
            self.assertEqual(changed_head.candidates, ())
            self.assertTrue(changed_head.degraded)
            self.assertEqual(len(provider.query_calls), calls_before_head_change)

            current_head = retriever.index.metadata()
            current_identity = (current_head['generation'], current_head['revision'])
            calls_before_late_head_change = len(provider.query_calls)
            with patch.object(retriever, '_head_identity', side_effect=[
                    current_identity, current_identity, current_identity, ('late-generation', 1000)]):
                late_changed_head = retriever.retrieve(request, 20)
            self.assertEqual(late_changed_head.candidates, ())
            self.assertTrue(late_changed_head.degraded)
            self.assertEqual(len(provider.query_calls), calls_before_late_head_change + 1)

            source_changed = [False]
            source_resolve = retriever.index._resolve

            def changed_source(locator):
                if source_changed[0]:
                    raise InvalidSource('fixture source changed after recall')
                return source_resolve(locator)

            def rank_then_change_source(*args, **kwargs):
                ranked = real_rank(*args, **kwargs)
                source_changed[0] = True
                return ranked

            with patch.object(retriever.index, '_resolve', side_effect=changed_source), \
                    patch.object(example_retriever, 'rank_candidates', side_effect=rank_then_change_source):
                stale_source = retriever.retrieve(request, 20)
            self.assertEqual(stale_source.candidates, ())
            self.assertTrue(stale_source.degraded)
            self.assertTrue(any('source invalid' in error for error in stale_source.errors))

            scope_changed = [False]

            def cross_friend_source(locator):
                if scope_changed[0]:
                    raise ScopeMismatch('candidate belongs to another friend')
                return source_resolve(locator)

            def rank_then_scope_change(*args, **kwargs):
                ranked = real_rank(*args, **kwargs)
                scope_changed[0] = True
                return ranked

            with patch.object(retriever.index, '_resolve', side_effect=cross_friend_source), \
                    patch.object(example_retriever, 'rank_candidates', side_effect=rank_then_scope_change):
                with self.assertRaises(ScopeMismatch):
                    retriever.retrieve(request, 20)

            binding_revision = next(row['revision'] for row in bindings.bindings()
                                    if row['pack_id'] == pack_id)

            def rank_then_unbind(*args, **kwargs):
                ranked = real_rank(*args, **kwargs)
                bindings.unbind(pack_id, binding_revision)
                return ranked

            with patch.object(example_retriever, 'rank_candidates', side_effect=rank_then_unbind):
                with self.assertRaises(Conflict):
                    retriever.retrieve(request, 20)
            unbound_pack = retriever.retrieve(request, 20)
            self.assertEqual(unbound_pack.candidates, ())
            self.assertFalse(unbound_pack.degraded)
            fts_only.close()
            retriever.close()
            index.close()
            bindings.close()


if __name__ == '__main__':
    unittest.main()
