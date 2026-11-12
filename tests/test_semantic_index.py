from dataclasses import asdict, replace
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_pack_bindings import PackBindingStore
from persona_store import Conflict, ScopeMismatch, Unavailable
from persona_types import (ContextBudgets, DataScope, EmbeddingBatch, EmbeddingMetadata,
    FriendScope, InvalidSource, PrivacyView, RetrievalFilters, RetrievalRequest, SourceOrigin, SourceRef)
from persona_versions import EXAMPLE_COLUMNS, FILES, render_derived
from semantic_index import SemanticIndex
from unittest.mock import patch


NOW = '2026-10-06T00:00:00+00:00'


class Provider:
    def __init__(self, version='v1'):
        self.metadata = EmbeddingMetadata(model='mock-local', dimension=2, version=version)
        self.text_calls, self.query_calls = [], []
        self.fail = False

    def batch(self, texts):
        if self.fail:
            raise Unavailable('mock GPU unavailable')
        return EmbeddingBatch(vectors=tuple((1., .1) if '周杰伦' in text else (.1, 1.) for text in texts),
            model=self.metadata.model, dimension=2, version=self.metadata.version)

    def embed_texts(self, texts, *, source_refs, expected_metadata):
        expected_metadata.validate_compatible(self.metadata)
        self.text_calls.append(tuple(texts))
        return self.batch(texts)

    def embed_query(self, text, *, source_refs, expected_metadata):
        expected_metadata.validate_compatible(self.metadata)
        self.query_calls.append(text)
        return self.batch([text])


class IndexIntegrationTests(unittest.TestCase):
    def test_topic_offset_and_calendar_validity_boundaries(self):
        from topic_knowledge import TopicKnowledgeStore
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        friend = root / 'friends' / '12345'
        friend.mkdir(parents=True)
        (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
        (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
        with closing(sqlite3.connect(friend / 'history.sqlite3')) as db, db:
            db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,body TEXT,created_at TEXT,handled INTEGER);')
            db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', '12345'))
            db.execute('INSERT INTO messages VALUES(1,?,?,?,?,0)',
                ('in-1', 'friend', '星际故事', '2026-10-06T11:00:00+09:00'))
        scope = FriendScope.load('12345', project_root=root)
        windows = {
            'started-offset': ('2026-10-06T10:00:00+09:00', None),
            'future-offset': ('2026-10-06T12:00:00+09:00', None),
            'expires-offset': (None, '2026-10-06T11:00:00+09:00'),
            'calendar-end': (None, '2026-10-06'),
        }
        with TopicKnowledgeStore(scope) as topics:
            for identifier, (start, end) in windows.items():
                ref = SourceRef(dataset_id='manual:topic_knowledge', import_id=identifier,
                    record_id=identifier, message_seq=None, speaker_id='operator',
                    origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
                    privacy_view=PrivacyView.LOCAL_FULL, friend_id='12345', event_time=None,
                    recorded_at=NOW)
                topics.upsert(dict(id=identifier, topic='星际', subtopic='', title='星际',
                    content='星际知识', summary='星际', keywords=['星际'], source_type='MANUAL',
                    source_url=None, source_refs=[ref], valid_from=start, valid_to=end,
                    confidence=.9, quality_score=.9, time_sensitivity='STABLE',
                    last_verified_at=None, embedding_model=None, embedding_version=None))
        provider = Provider()
        index = SemanticIndex(scope, embedding_provider=provider)
        self.addCleanup(index.close)
        revision = index.store.revision
        generation = index.begin_generation(0, 'time-fixture', Provider().metadata)
        index.write_batch(generation, [{'kind': 'topic', 'id': value} for value in windows] +
            [{'kind': 'history_message', 'seq': 1}], {}, revision)
        index.activate(generation, 0, revision, 'time-fixture')
        def ids(now):
            request = RetrievalRequest(friend_id='12345', query='星际', recent_context='',
                scene='any', plan=None, filters=RetrievalFilters(namespace=('knowledge',)),
                budgets=ContextBudgets(), allowed_pack_versions=frozenset(), now=now)
            return {item.id for item in index.keyword(request, 10).candidates}
        self.assertEqual(ids('2026-10-06T01:59:59+00:00'),
            {'started-offset', 'expires-offset', 'calendar-end'})
        self.assertEqual(ids('2026-10-06T02:00:00+00:00'), {'started-offset', 'calendar-end'})
        self.assertEqual(ids('2026-10-06T23:59:59+09:00'),
            {'started-offset', 'future-offset', 'calendar-end'})
        self.assertEqual(ids('2026-10-07T00:00:00+09:00'), {'started-offset', 'future-offset'})
        history_request = RetrievalRequest(friend_id='12345', query='星际', recent_context='',
            scene='any', plan=None, filters=RetrievalFilters(namespace=('memory',),
                time_range=('2026-10-06T01:00:00+00:00', '2026-10-06T03:00:00+00:00')),
            budgets=ContextBudgets(), allowed_pack_versions=frozenset(), now=NOW)
        self.assertEqual([item.id for item in index.keyword(history_request, 10).candidates], ['1'])
        from keyword_retriever import KeywordRetriever
        from memory_retriever import MemoryRetriever
        from semantic_retriever import SemanticRetriever
        refs = (index._message_ref(index._legacy_message(seq=1)),)
        text_calls = len(provider.text_calls)
        for retriever in (KeywordRetriever(scope), MemoryRetriever(scope),
                SemanticRetriever(scope, provider, source_refs=refs)):
            result = retriever.retrieve(history_request, 10)
            self.assertFalse(result.degraded)
            self.assertEqual([item.id for item in result.candidates], ['1'])
        self.assertEqual(len(provider.text_calls), text_calls)
        self.assertEqual(len(provider.query_calls), 1)
        self.assertEqual(index.store.revision, revision)

    def test_generations_sources_filters_fts_and_query_only_dense(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        friends = {}
        for owner in ('12345', '23456'):
            directory = root / 'friends' / owner
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': owner,
                'basic_info': {'nickname': '小林'}, 'persona': 'untouched'}), 'utf-8')
            friends[owner] = {'qq_id': owner, 'directory': str(directory)}
            with closing(sqlite3.connect(directory / 'history.sqlite3')) as db, db:
                db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                    'CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,body TEXT,created_at TEXT,handled INTEGER);')
                db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', owner))
                db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,0)', [
                    (1, 'in-1', 'friend', '周杰伦和夔州', NOW), (2, 'in-2', 'friend', '琵琶', NOW),
                    (3, 'out-1', 'me', '机器人说喜欢火星', NOW)])
        (root / 'friends.json').write_text(json.dumps({'friends': friends}), 'utf-8')
        scope = FriendScope.load('12345', project_root=root)
        other_scope = FriendScope.load('23456', project_root=root)
        configs = {path: path.read_bytes() for path in [root / 'friends.json', scope.friend_dir / 'friend.json']}
        public_refs = [asdict(SourceRef(dataset_id='fixture', import_id='fixture-import', record_id=identifier,
            message_seq=None, speaker_id=identifier, origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=None, event_time=None, recorded_at=NOW, license_id='CC-BY', model_analysis_allowed=True))
            for identifier in ('user', 'target')]
        version, pack_id = 'public-v1', 'index-test'
        directory = root / 'data/persona_packs' / pack_id / 'versions' / version
        directory.mkdir(parents=True)
        profile = {'pack_id': pack_id, 'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None, 'claims': []}
        style = dict(profile, observed={})
        for name, value in (('persona_profile.json', profile), ('style_profile.json', style)):
            (directory / name).write_text(json.dumps(value), 'utf-8')
        for name in ('persona_skill.md', 'persona_report.md'):
            (directory / name).write_text('fixture', 'utf-8')
        def bubble(ref, text): return {'id': ref['record_id'], 'text': text, 'source': ref}
        with closing(sqlite3.connect(directory / 'examples.db')) as db, db:
            db.execute('CREATE TABLE examples (' + ','.join(column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
            for identifier, scene, emotion in (('wrong', 'sad_chat', 'sad'), ('right', 'daily_chat', 'happy')):
                db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                    (identifier, json.dumps([bubble(public_refs[0], '周杰伦演唱会')]), '[]',
                     json.dumps([bubble(public_refs[1], '目标回应秘词')]), scene, emotion, '音乐', None,
                     'PUBLIC_DATASET', None, json.dumps(['familiar', 'close']), json.dumps(public_refs), version))
        manifest = dict(profile, source_refs=public_refs, selected_pair_ids=['wrong', 'right'],
            sources=[{'dataset_id': 'fixture', 'local_use_allowed': True, 'derived_stats_allowed': True,
                      'license_data': 'CC-BY', 'raw_export_allowed': False}], raw_export_allowed=False,
            files={name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in FILES})
        (directory / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
        binding = PackBindingStore(scope)
        self.addCleanup(binding.close)
        binding.bind(pack_id, version)
        scope = binding.bound_scope()
        provider = Provider()
        index = SemanticIndex(scope, embedding_provider=provider)
        other = SemanticIndex(other_scope, embedding_provider=Provider())
        self.addCleanup(index.close)
        self.addCleanup(other.close)
        source_revision = index.store.revision
        def request(query, filters=None, scene='any'):
            return RetrievalRequest(friend_id=scope.friend_id, query=query, recent_context='', scene=scene,
                plan=None, filters=filters or RetrievalFilters(), budgets=ContextBudgets(),
                allowed_pack_versions=scope.allowed_pack_versions, now=NOW)
        locators = [{'kind': 'history_message', 'seq': 1}, {'kind': 'history_message', 'seq': 2},
            {'kind': 'structured_manual', 'field': 'basic_info'},
            *[{'kind': 'example', 'pack_id': pack_id, 'version': version, 'id': value} for value in ('wrong', 'right')]]
        generation = index.begin_generation(0, 'selected-fixture', provider.metadata)
        index.write_batch(generation, locators, {'cursor': 5}, source_revision)
        self.assertEqual(index.activate(generation, 0, source_revision, 'selected-fixture'), 1)
        self.assertEqual(index.store.revision, source_revision)
        self.assertEqual(json.loads(index.metadata()['checkpoint_json']), {'cursor': 5})
        self.assertTrue(all('目标回应秘词' not in text for call in provider.text_calls for text in call))
        self.assertEqual(index.keyword(request('夔州'), 1).candidates[0].id, '1')
        self.assertEqual(index.keyword(request('琵琶'), 1).candidates[0].id, '2')
        self.assertEqual(index.keyword(request('目标回应秘词'), 5).candidates, ())
        filtered = request('周杰伦', RetrievalFilters(namespace=('example',), topic='音乐',
            emotion=('happy',), relationship_tags=('familiar',)), 'daily_chat')
        self.assertEqual([item.id for item in index.keyword(filtered, 1).candidates], ['right'])
        self.assertIn('目标回应秘词', index.keyword(filtered, 1).candidates[0].content)
        self.assertEqual(other.keyword(replace(filtered, friend_id='23456', allowed_pack_versions=frozenset()), 2).candidates, ())
        with self.assertRaises(ScopeMismatch): index.keyword(replace(filtered, friend_id='23456'), 1)
        refs = (index._message_ref(index._legacy_message(seq=1)),)
        calls = len(provider.text_calls)
        self.assertTrue(index.dense(filtered, 1, source_refs=refs).candidates)
        self.assertEqual(len(provider.query_calls), 1)
        self.assertEqual(len(provider.text_calls), calls)
        with patch.object(index, '_candidate', side_effect=ScopeMismatch('scope fixture')):
            with self.assertRaises(ScopeMismatch):
                index.dense(request('夔州'), 10, source_refs=refs)
        with patch.object(index, '_candidate', side_effect=ScopeMismatch('scope fixture')):
            with self.assertRaises(ScopeMismatch):
                index.keyword(request('夔州'), 10)
        with patch.object(index, '_candidate', side_effect=InvalidSource('stale fixture')):
            self.assertTrue(index.keyword(request('夔州'), 10).degraded)
        # New inbound traffic does not invalidate old documents or require their re-encoding.
        with closing(sqlite3.connect(scope.friend_dir / 'history.sqlite3')) as db, db:
            db.execute('INSERT INTO messages VALUES(4,?,?,?,?,0)', ('in-4', 'friend', '新来信', NOW))
        self.assertTrue(index.keyword(request('夔州'), 1).candidates)
        second = index.begin_generation(1, 'same-selected', provider.metadata)
        index.write_batch(second, locators, {'cursor': 5}, source_revision)
        self.assertEqual(len(provider.text_calls), calls)
        self.assertEqual(index.activate(second, 1, source_revision, 'same-selected'), 2)
        # Failed CAS and changed source publication preserve the existing head.
        staged = index.begin_generation(2, 'changed', provider.metadata)
        index.write_batch(staged, [{'kind': 'history_message', 'seq': 2}], {}, source_revision)
        with self.assertRaises(Conflict): index.activate(staged, 1, source_revision, 'changed')
        with closing(sqlite3.connect(scope.friend_dir / 'history.sqlite3')) as db, db:
            db.execute('DELETE FROM messages WHERE seq=2')
        with self.assertRaises(Exception): index.activate(staged, 2, source_revision, 'changed')
        self.assertEqual(index.metadata()['generation'], second)
        self.assertEqual(index.keyword(request('琵琶'), 2).candidates, ())
        # Model mismatch degrades dense only, then rebuild encodes current true sources.
        provider.metadata = EmbeddingMetadata(model='mock-local', dimension=2, version='v2')
        self.assertTrue(index.dense(filtered, 1, source_refs=refs).degraded)
        self.assertTrue(index.keyword(filtered, 1).candidates)
        rebuilt = index.begin_generation(2, 'new-model', provider.metadata)
        remaining = [locator for locator in locators if locator.get('seq') != 2]
        index.write_batch(rebuilt, remaining, {'cursor': 4}, source_revision)
        self.assertEqual(len(provider.text_calls), calls+1)
        self.assertEqual(index.activate(rebuilt, 2, source_revision, 'new-model'), 3)
        self.assertEqual(index.metadata()['version'], 'v2')
        binding.unbind(pack_id, expected_revision=1)
        self.assertEqual(index.keyword(filtered, 2).candidates, ())
        self.assertEqual(index.dense(filtered, 2, source_refs=refs).candidates, ())
        # Real private immutable version DB uses trigger text and retains full Target display.
        private_version = 'a' * 32
        private_dir = scope.friend_dir / 'persona_versions' / private_version
        private_dir.mkdir(parents=True)
        private_ref = asdict(refs[0])
        private_profile = {'version': private_version, 'claims': [], 'manual_revision': 0}
        private_style = {'version': private_version, 'observed': {}, 'manual_revision': 0}
        for name, data in ((FILES[0], private_profile), (FILES[1], private_style)):
            (private_dir / name).write_text(json.dumps(data), 'utf-8')
        for name, data in zip(FILES[2:4], render_derived(private_profile, private_style)):
            (private_dir / name).write_text(data, 'utf-8')
        with closing(sqlite3.connect(private_dir / 'examples.db')) as db, db:
            db.execute('CREATE TABLE examples (' + ','.join(column + ' TEXT' for column in EXAMPLE_COLUMNS) + ')')
            db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                ('private', json.dumps([bubble(private_ref, '周杰伦和夔州')]), '[]',
                 json.dumps([bubble(private_ref, '不可用于检索目标')]), 'daily_chat', None, None, None,
                 'REAL_RELATIONSHIP', scope.friend_id, None, json.dumps([private_ref]), private_version))
        private_manifest = {'friend_id': scope.friend_id, 'version': private_version, 'source_refs': [private_ref],
            'files': {name: hashlib.sha256((private_dir / name).read_bytes()).hexdigest() for name in FILES}}
        manifest_path = private_dir / 'manifest.json'
        manifest_path.write_text(json.dumps(private_manifest), 'utf-8')
        (private_dir.parent / 'active.json').write_text(json.dumps({'friend_id': scope.friend_id,
            'version': private_version, 'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest()}), 'utf-8')
        provider.fail = True
        fts_only = index.begin_generation(3, 'private-selected', provider.metadata)
        index.write_batch(fts_only, [{'kind': 'example', 'version': private_version, 'id': 'private'}], {}, source_revision)
        index.activate(fts_only, 3, source_revision, 'private-selected')
        self.assertTrue(index.keyword(request('夔州'), 1).candidates)
        self.assertTrue(index.dense(request('夔州'), 1, source_refs=refs).degraded)
        self.assertEqual(index.keyword(request('不可用于检索目标'), 1).candidates, ())
        self.assertEqual(index.tombstone('example', 'private', 4), 5)
        self.assertEqual(index.keyword(request('夔州'), 1).candidates, ())
        self.assertEqual(index.store.revision, source_revision)
        readonly = SemanticIndex(scope, readonly=True)
        self.addCleanup(readonly.close)
        with self.assertRaises(Unavailable): readonly.begin_generation(5, 'no-write', provider.metadata)
        for path, data in configs.items(): self.assertEqual(path.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
