import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from context_retrievers import EntityRetriever, InterestRetriever, TemporalRetriever, TopicKnowledgeRetriever
from interest_entries import InterestStore
from knowledge_entities import KnowledgeEntityStore
from persona_store import PersonaStore
from persona_types import (ContextBudgets, DataScope, FriendScope, PrivacyView, RetrievalFilters,
    RetrievalRequest, SourceOrigin, SourceRef)
from topic_knowledge import TopicKnowledgeStore


NOW = '2026-10-06T00:00:00+00:00'


class ContextRetrieverTests(unittest.TestCase):
    def test_verified_entities_interest_topic_validity_and_unknown_time(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
            with closing(sqlite3.connect(friend / 'history.sqlite3')) as db, db:
                db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                    'CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,body TEXT,created_at TEXT);'
                    'CREATE TABLE topic_library(url TEXT PRIMARY KEY,topic TEXT,summary TEXT,source_title TEXT,day TEXT);')
                db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', '12345'))
                db.executemany('INSERT INTO messages VALUES(?,?,?,?,?)', [
                    (1, 'm1', 'friend', 'astronomy', '2026-10-05T12:00:00+00:00'),
                    (2, 'm2', 'friend', '时间未知的兴趣', None)])
                db.execute('INSERT INTO topic_library VALUES(?,?,?,?,?)',
                    ('https://example.test/research', 'astronomy', '研究摘要', '公开研究', '2026-10-05'))
            scope = FriendScope.load('12345', project_root=root)
            PersonaStore(scope.friend_id, scope.friend_dir, project_root=root).close()

            def ref(kind, identifier, seq=None, stamp=None):
                manual = kind != 'chat'
                return SourceRef(dataset_id=('manual:knowledge_entities' if kind == 'entity' else
                        'manual:knowledge_relations' if kind == 'relation' else
                        'manual:topic_knowledge' if kind == 'topic' else
                        'manual:interest_entries' if kind == 'interest' else None),
                    import_id=identifier if manual else None, record_id=identifier if manual else None,
                    message_seq=seq, speaker_id='operator' if manual else '12345',
                    origin=SourceOrigin.MANUAL if manual else SourceOrigin.REAL_RELATIONSHIP,
                    data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                    friend_id='12345', event_time=stamp, recorded_at=NOW)

            with KnowledgeEntityStore(scope) as store:
                store.upsert_entity(dict(id='singer', name='周杰伦', entity_type='Person', aliases=['周董'],
                    source_type='MANUAL', source_refs=[ref('entity', 'singer')], confidence=0.95), manual=True)
                store.upsert_entity(dict(id='friend', name='friend', entity_type='Person', aliases=[],
                    source_type='MANUAL', source_refs=[ref('entity', 'friend')], confidence=1.0), manual=True)
                store.upsert_relation(dict(id='likes', source_entity='friend', relation='LIKES',
                    target_entity='singer', confidence=.9, strength=.8, valid_from=None, valid_to=None,
                    source_type='MANUAL', source_refs=[ref('relation', 'likes')]), manual=True)
            with InterestStore(scope) as store:
                store.upsert(dict(id='recent', topic='astronomy', content='astronomy', category='兴趣',
                    strength=0.8, confidence=0.9, interest_type='recent', source_type='REAL_CHAT',
                    source_refs=[ref('chat', 'm1', 1, '2026-10-05T12:00:00+00:00')],
                    first_seen='2026-10-05T12:00:00+00:00', last_seen='2026-10-05T12:00:00+00:00',
                    locked=False, valid_to=None))
                store.upsert(dict(id='unknown', topic='时间未知', content='时间未知的兴趣', category='兴趣',
                    strength=0.8, confidence=0.9, interest_type='stable', source_type='REAL_CHAT',
                    source_refs=[ref('chat', 'm2', 2, None)], first_seen=None, last_seen=None,
                    locked=False, valid_to=None))
                store.upsert(dict(id='expired', topic='旧兴趣', content='过期兴趣', category='兴趣',
                    strength=1.0, confidence=1.0, interest_type='recent', source_type='REAL_CHAT',
                    source_refs=[ref('chat', 'm1', 1, '2026-10-05T12:00:00+00:00')],
                    first_seen='2026-10-05T12:00:00+00:00', last_seen='2026-10-05T12:00:00+00:00',
                    locked=False, valid_to='2026-10-05T23:59:59+00:00'))
            with TopicKnowledgeStore(scope) as store:
                def topic(identifier, **changes):
                    entry = dict(id=identifier, topic='astronomy', subtopic='', title='astronomy',
                        content='星空与行星', summary='星空摘要', keywords=['星空'], source_type='MANUAL',
                        source_url=None, source_refs=[ref('topic', identifier)], valid_from=None,
                        valid_to=None, confidence=0.8, quality_score=0.8, time_sensitivity='UNKNOWN',
                        last_verified_at=None, embedding_model=None, embedding_version=None)
                    return {**entry, **changes}
                store.mirror_legacy()
                store.upsert(topic('active', valid_from='2026-10-06'))
                store.upsert(topic('expired', valid_to='2026-10-05'))
                store.upsert(topic('future', valid_from='2026-10-07'))

            def request(query, filters=None):
                return RetrievalRequest(friend_id='12345', query=query, recent_context='', scene='any',
                    plan=None, filters=filters or RetrievalFilters(), budgets=ContextBudgets(),
                    allowed_pack_versions=frozenset(), now=NOW)

            entities = EntityRetriever(scope).retrieve(request('昨天提到周董嗎'), 10)
            self.assertEqual([item.id for item in entities.candidates], ['singer', 'relation:likes'])
            self.assertIn(entities.candidates[1].content, ('friend LIKES 周杰伦', '好友 LIKES 周杰伦'))
            interests = InterestRetriever(scope).retrieve(request('兴趣'), 10)
            self.assertEqual([item.id for item in interests.candidates], ['recent', 'unknown'])
            unknown = next(item for item in interests.candidates if item.id == 'unknown')
            self.assertIsNone(unknown.timestamp)
            self.assertIn('time_unknown', unknown.signals)
            topics = TopicKnowledgeRetriever(scope).retrieve(request('astronomy'), 10)
            self.assertEqual(set(item.id for item in topics.candidates),
                             {'active', 'legacy-topic:' + __import__('hashlib').sha256(
                                 b'https://example.test/research').hexdigest()})
            research_only = TopicKnowledgeRetriever(scope).retrieve(request('公开研究',
                RetrievalFilters(source_type=('PUBLIC_RESEARCH',))), 10)
            self.assertEqual(len(research_only.candidates), 1)
            self.assertEqual(research_only.candidates[0].source.origin, SourceOrigin.BOT_GENERATED)
            date_filter = request('astronomy', RetrievalFilters(time_range=('2026-10-06', '2026-10-06')))
            self.assertEqual([item.id for item in TopicKnowledgeRetriever(scope).retrieve(date_filter, 10).candidates],
                             ['active'])
            temporal = TemporalRetriever(scope).retrieve(request(''), 10)
            self.assertIsNone(temporal.candidates[-1].timestamp)
            self.assertIn('time_unknown', temporal.candidates[-1].signals)


if __name__ == '__main__':
    unittest.main()
