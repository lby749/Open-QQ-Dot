import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from persona_store import Conflict, PersonaStore
from persona_types import DataScope, FriendScope, PrivacyView, SourceOrigin, SourceRef
from topic_knowledge import TopicKnowledgeStore


class TopicKnowledgeTests(unittest.TestCase):
    def test_legacy_mirror_freshness_scope_and_cas(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), encoding='utf-8')
            history = friend / 'history.sqlite3'
            db = sqlite3.connect(history)
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO metadata VALUES('qq_id','12345')")
            db.execute('CREATE TABLE topic_library(url TEXT PRIMARY KEY,topic TEXT,summary TEXT,source_title TEXT,day TEXT)')
            db.execute('INSERT INTO topic_library VALUES(?,?,?,?,?)',
                       ('https://example.test/a', 'science', 'summary', 'title', '2026-10-01'))
            db.commit()
            db.close()
            scope = FriendScope.load('12345', project_root=root)
            base = dict(topic='science', subtopic='', title='manual', content='detail', summary='detail',
                        keywords=[], source_type='MANUAL', source_url=None, source_refs=[],
                        valid_from=None, valid_to=None, confidence=0.5, quality_score=0.5,
                        time_sensitivity='UNKNOWN', last_verified_at=None,
                        embedding_model=None, embedding_version=None)

            def manual(entry_id, **changes):
                ref = SourceRef(dataset_id='manual:topic_knowledge', import_id=entry_id,
                    record_id=entry_id, message_seq=None, speaker_id='operator',
                    origin=SourceOrigin.MANUAL, data_scope=DataScope.PRIVATE_FRIEND,
                    privacy_view=PrivacyView.LOCAL_FULL, friend_id='12345', event_time=None,
                    recorded_at='2026-10-06T00:00:00+00:00')
                return {**base, 'id': entry_id, 'source_refs': [ref], **changes}

            with TopicKnowledgeStore(scope) as store:
                rev0 = store._store.revision
                self.assertEqual(store.mirror_legacy(), 1)
                self.assertEqual(store._store.revision, rev0 + 1)
                self.assertEqual(store.mirror_legacy(), 0)
                research = store.entries('2026-10-06T00:00:00+00:00')
                self.assertEqual(len(research), 1)
                mirrored_id = research[0]['id']
                db = sqlite3.connect(history)
                db.execute('DELETE FROM topic_library')
                db.commit()
                db.close()
                history_after_delete = hashlib.sha256(history.read_bytes()).hexdigest()
                self.assertEqual(store.entries('2026-10-06T00:00:00+00:00'), [])
                self.assertEqual(store.mirror_legacy(), 1)
                self.assertEqual(store._store.db.execute(
                    'SELECT valid,revision FROM topic_knowledge WHERE id=?',
                    (mirrored_id,)).fetchone()['valid'], 0)

                self.assertEqual(store.upsert(manual('future', valid_from='2026-10-06T12:00:00+00:00')), 1)
                self.assertEqual(store.upsert(manual('expired', valid_to='2026-10-05')), 1)
                self.assertEqual(store.upsert(manual('unknown')), 1)
                visible = store.entries('2026-10-06T00:00:00+00:00')
                self.assertEqual([item['id'] for item in visible], ['unknown'])
                self.assertEqual(visible[0]['freshness_score'], 0.25)
                self.assertTrue(visible[0]['needs_verification'])
                with self.assertRaises(Conflict):
                    store.upsert(manual('unknown', summary='changed'), expected_revision=0)
                with self.assertRaises(Conflict):
                    store.tombstone('unknown', 0)
                row = store._store.db.execute(
                    'SELECT revision FROM topic_knowledge WHERE id=?', ('unknown',)).fetchone()
                self.assertEqual(store.tombstone('unknown', row['revision']), row['revision'] + 1)

            sidecar = friend / 'persona.sqlite3'
            sidecar_before = hashlib.sha256(sidecar.read_bytes()).hexdigest()
            with TopicKnowledgeStore(scope, readonly=True) as reader:
                self.assertEqual(reader.entries('2026-10-06T00:00:00+00:00'), [])
            self.assertEqual(hashlib.sha256(sidecar.read_bytes()).hexdigest(), sidecar_before)
            self.assertEqual(hashlib.sha256(history.read_bytes()).hexdigest(), history_after_delete)


if __name__ == '__main__':
    unittest.main()
