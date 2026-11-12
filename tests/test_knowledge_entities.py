import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from persona_store import Conflict, ScopeMismatch
from persona_types import DataScope, FriendScope, InvalidSource, PrivacyView, SourceOrigin, SourceRef
from knowledge_entities import KnowledgeEntityStore


class KnowledgeEntityTests(unittest.TestCase):
    def test_explicit_alias_friend_isolation_relation_expiry_and_cas(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            registry = {'friends': {}}
            for fid in ('12345', '23456'):
                directory = root / 'friends' / fid
                directory.mkdir(parents=True)
                (directory / 'friend.json').write_text(json.dumps({'qq_id': fid}), encoding='utf-8')
                registry['friends'][fid] = {'qq_id': fid, 'directory': str(directory)}
            (root / 'friends.json').write_text(json.dumps(registry), encoding='utf-8')
            history = root / 'friends' / '12345' / 'history.sqlite3'
            db = sqlite3.connect(history)
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO metadata VALUES('qq_id','12345')")
            db.execute('CREATE TABLE messages(seq INTEGER PRIMARY KEY,direction TEXT,created_at TEXT)')
            db.execute("INSERT INTO messages VALUES(1,'friend','2026-10-06T00:00:00+00:00')")
            db.commit()
            db.close()

            def ref(fid, kind, record):
                return SourceRef(dataset_id='manual:knowledge_' + kind, import_id=record,
                    record_id=record, message_seq=None, speaker_id='operator',
                    origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
                    privacy_view=PrivacyView.LOCAL_FULL, friend_id=fid, event_time=None,
                    recorded_at='2026-10-06T00:00:00+00:00')

            scope = FriendScope.load('12345', project_root=root)
            with KnowledgeEntityStore(scope) as store:
                singer = dict(id='person-1', name='周杰伦', entity_type='Person', aliases=['周董'],
                              source_type='MANUAL', source_refs=[ref('12345', 'entities', 'person-1')],
                              confidence=1.0)
                music = dict(id='topic-1', name='days', entity_type='Music', aliases=[],
                             source_type='MANUAL', source_refs=[ref('12345', 'entities', 'topic-1')],
                             confidence=1.0)
                self.assertEqual(store.upsert_entity(singer, manual=True), 1)
                self.assertEqual(store.upsert_entity(music, manual=True), 1)
                self.assertEqual(store.resolve('周董')['name'], '周杰伦')
                with self.assertRaises(InvalidSource):
                    store.upsert_entity({**singer, 'id': 'unconfirmed', 'source_refs': [
                        ref('12345', 'entities', 'unconfirmed')], 'source_type': 'REAL_CHAT'}, manual=False)

                relation = dict(id='r1', source_entity='person-1', relation='LIKES', target_entity='topic-1',
                    confidence=1.0, strength=1.0, valid_from=None, valid_to=None, source_type='MANUAL',
                    source_refs=[ref('12345', 'relations', 'r1')])
                self.assertEqual(store.upsert_relation(relation, manual=True), 1)
                self.assertIsNone(store.relations('2026-10-06T00:00:00+00:00')[0]['first_seen'])
                with self.assertRaises(Conflict):
                    store.upsert_relation(relation, expected_revision=0, manual=True)
                self.assertEqual(store.upsert_relation({**relation, 'id': 'old',
                    'source_refs': [ref('12345', 'relations', 'old')], 'valid_to': '2026-10-05'},
                    manual=True), 1)
                self.assertEqual([row['id'] for row in store.relations('2026-10-06T00:00:00+00:00')], ['r1'])

                real_ref = SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=1,
                    speaker_id='12345', origin=SourceOrigin.REAL_RELATIONSHIP,
                    data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                    friend_id='12345', event_time='2026-10-06T00:00:00+00:00',
                    recorded_at='2026-10-06T00:00:00+00:00')
                real_entity = dict(id='mentioned', name='Rain', entity_type='Topic', aliases=[],
                    source_type='REAL_CHAT', source_refs=[real_ref], confidence=0.5)
                self.assertEqual(store.upsert_entity(real_entity), 1)
                self.assertEqual(store.resolve('Rain')['name'], 'Rain')
                real_relation = {**relation, 'id': 'r-chat', 'source_type': 'REAL_CHAT',
                    'source_refs': [real_ref]}
                self.assertEqual(store.upsert_relation(real_relation), 1)
                self.assertIn('r-chat', [row['id'] for row in store.relations('2026-10-06T00:00:00+00:00')])
                db = sqlite3.connect(history)
                db.execute('DELETE FROM messages WHERE seq=1')
                db.commit()
                db.close()
                self.assertIsNone(store.resolve('Rain'))
                self.assertNotIn('r-chat', [row['id'] for row in store.relations('2026-10-06T00:00:00+00:00')])

            other = FriendScope.load('23456', project_root=root)
            with KnowledgeEntityStore(other) as store:
                self.assertIsNone(store.resolve('周董'))
                own = dict(id='person-1', name='周杰伦', entity_type='Person', aliases=[],
                           source_type='MANUAL', source_refs=[ref('23456', 'entities', 'person-1')],
                           confidence=1.0)
                self.assertEqual(store.upsert_entity(own, manual=True), 1)
                self.assertIsNone(store.resolve('周董'))
                foreign_relation = dict(id='leak', source_entity='person-1', relation='LIKES',
                    target_entity='topic-1', confidence=1, strength=1, valid_from=None, valid_to=None,
                    source_type='MANUAL', source_refs=[ref('23456', 'relations', 'leak')])
                with self.assertRaises(ScopeMismatch):
                    store.upsert_relation(foreign_relation, manual=True)


if __name__ == '__main__':
    unittest.main()
