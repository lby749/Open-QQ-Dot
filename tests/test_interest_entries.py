import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, PrivacyView,
                           SourceOrigin, SourceRef)
from interest_entries import InterestStore


class InterestEntryIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friends = {}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(
                json.dumps({'qq_id': friend_id, 'basic_info': {}}), encoding='utf-8')
            friends[friend_id] = {'qq_id': friend_id, 'directory': str(directory), 'self_id': '34567'}
            with closing(sqlite3.connect(directory / 'history.sqlite3')) as db:
                with db:
                    db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                        CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,
                            body TEXT,created_at TEXT,handled INTEGER);''')
                    db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', friend_id))
                    db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', (
                        (1, 'in-1', 'friend', 'astronomy', '2026-10-01T12:00:00+00:00', 0),
                        (2, 'in-2', 'friend', '最近也看棋谱', None, 0),
                        (3, 'out-1', 'me', 'bot suggestion', '2026-10-02T12:00:00+00:00', 1)))
            store = PersonaStore(friend_id, directory, project_root=self.root)
            store.close()
        (self.root / 'friends.json').write_text(json.dumps({'friends': friends}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)

    def ref(self, seq=1, event_time='2026-10-01T12:00:00+00:00', friend_id='12345',
            origin=SourceOrigin.REAL_RELATIONSHIP, speaker_id=None):
        return SourceRef(dataset_id=None, import_id=None, record_id=None, message_seq=seq,
                         speaker_id=speaker_id or friend_id, origin=origin,
                         data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
                         friend_id=friend_id, event_time=event_time,
                         recorded_at='2026-10-06T00:00:00+00:00')

    def entry(self, identifier, *, ref=None, topic='astronomy', interest_type='stable',
              first='2026-10-01T12:00:00+00:00', last='2026-10-01T12:00:00+00:00',
              locked=False, valid_to=None):
        return {'id': identifier, 'topic': topic, 'content': 'clearastronomy', 'category': '兴趣',
                'strength': 0.8, 'confidence': 0.9, 'interest_type': interest_type,
                'source_type': 'REAL_CHAT', 'source_refs': [ref or self.ref()],
                'first_seen': first, 'last_seen': last, 'locked': locked, 'valid_to': valid_to}

    def test_interest_source_lock_cas_expiry_recency_isolation_and_readonly(self):
        store = InterestStore(self.scope)
        self.addCleanup(store.close)
        self.assertEqual(store.upsert(self.entry('same'), expected_revision=0), 1)
        self.assertEqual(store._store.revision, 1)

        updated = self.entry('same', topic='astronomy')
        updated['strength'] = 0.7
        self.assertEqual(store.upsert(updated, expected_revision=1), 2)
        with self.assertRaises(Conflict):
            store.upsert(updated, expected_revision=1)

        manual_ref = SourceRef(
            dataset_id='manual:interest_entries', import_id='locked', record_id='locked',
            message_seq=None, speaker_id='operator', origin=SourceOrigin.MANUAL,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id='12345', event_time=None, recorded_at='2026-10-06T00:00:00+00:00')
        manual = {'id': 'locked', 'topic': '棋谱', 'content': '操作者确认喜欢棋谱', 'category': '兴趣',
                  'strength': 1.0, 'confidence': 1.0, 'interest_type': 'manual',
                  'source_type': 'MANUAL', 'source_refs': [manual_ref], 'first_seen': None,
                  'last_seen': None, 'locked': True, 'valid_to': None}
        self.assertEqual(store.upsert(manual, expected_revision=0, manual=True), 1)
        with self.assertRaises(Conflict):
            store.upsert(self.entry('locked'), expected_revision=1)
        with self.assertRaises(Conflict):
            store.tombstone('locked', 1)
        manual['content'] = '操作者人工更正'
        self.assertEqual(store.upsert(manual, expected_revision=1, manual=True), 2)
        self.assertEqual(store.tombstone('locked', 2, manual=True), 3)
        with self.assertRaises(Conflict):
            store.tombstone('same', 1)

        # Missing event time stays unknown and does not acquire a recency value.
        unknown_ref = self.ref(2, None)
        unknown = self.entry('unknown', ref=unknown_ref, interest_type='temporary',
                             first=None, last=None)
        self.assertEqual(store.upsert(unknown), 1)
        expired = self.entry('expired', valid_to='2026-10-05T23:59:59+00:00')
        store.upsert(expired)

        # Completed, private imports accept only a verified friend-human inbound row.
        db = store._store.db
        db.execute('INSERT INTO dataset_registry(friend_id,dataset_id,name,data_scope,usage_scope,'
                   'active,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   ('12345', 'private-chat', 'fixture', 'PRIVATE_FRIEND', 'local', 1, 1,
                    '2026-10-01T00:00:00+00:00', '2026-10-01T00:00:00+00:00'))
        db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,data_scope,'
                   'source_hash,mapping_hash,settings_hash,status,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('12345', 'import-1', 'private-chat', 'PRIVATE_FRIEND', 'a'*64, 'b'*64,
                    'c'*64, 'COMPLETED', '2026-10-01T00:00:00+00:00',
                    '2026-10-01T00:00:00+00:00'))
        db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
                   'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,'
                   'message_type,content_fingerprint,source_origin) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('12345', 'import-1', 'private-chat', 'record-1', 1, '12345', 'friend',
                    'friend', '2026-09-29T08:00:00+00:00', '2026-10-01T00:00:00+00:00',
                    '喜欢星图', 'text', 'f'*64, 'REAL_RELATIONSHIP'))
        imported_ref = SourceRef(
            dataset_id='private-chat', import_id='import-1', record_id='record-1',
            message_seq=1, speaker_id='12345', origin=SourceOrigin.REAL_RELATIONSHIP,
            data_scope=DataScope.PRIVATE_FRIEND, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id='12345', event_time='2026-09-29T08:00:00+00:00',
            recorded_at='2026-10-01T00:00:00+00:00')
        imported = self.entry('imported', ref=imported_ref, first='2026-09-29T08:00:00+00:00',
                              last='2026-09-29T08:00:00+00:00')
        store.upsert(imported)

        # A public/model/BOT claim and a wrong owner cannot become REAL_CHAT interest.
        with self.assertRaises(ScopeMismatch):
            store.upsert(self.entry('bot', ref=self.ref(origin=SourceOrigin.BOT_GENERATED)))
        with self.assertRaises(ScopeMismatch):
            store.upsert(self.entry('other', ref=self.ref(friend_id='23456')))
        with self.assertRaises(ScopeMismatch):
            store.upsert(self.entry('outbound', ref=self.ref(3,
                '2026-10-02T12:00:00+00:00')))
        with self.assertRaises(InvalidSource):
            store.upsert(self.entry('fake-time', first='2020-01-01T00:00:00+00:00'))

        # Same row id is isolated by friend scope, even on another store.
        other_scope = FriendScope.load('23456', project_root=self.root)
        other = InterestStore(other_scope)
        self.addCleanup(other.close)
        other_entry = self.entry('same', ref=self.ref(friend_id='23456'), topic='另一好友数据')
        other.upsert(other_entry)
        self.assertEqual(other.entries('2026-10-06T00:00:00+00:00')[0]['topic'], '另一好友数据')

        items = store.entries('2026-10-06T00:00:00+00:00', limit=100)
        by_id = {item['id']: item for item in items}
        self.assertNotIn('locked', by_id)
        self.assertNotIn('expired', by_id)
        self.assertIsNone(by_id['unknown']['last_seen'])
        self.assertIsNone(by_id['unknown']['recency_days'])
        self.assertEqual(by_id['same']['recency_days'], 4.5)

        store.close()
        path = self.scope.friend_dir / 'persona.sqlite3'
        before = path.read_bytes()
        readonly = InterestStore(self.scope, readonly=True)
        self.assertIn('same', {item['id'] for item in readonly.entries(
            '2026-10-06T00:00:00+00:00')})
        with self.assertRaises(Unavailable):
            readonly.upsert(self.entry('readonly'))
        readonly.close()
        self.assertEqual(path.read_bytes(), before)

        # Recheck the live source on read and write; an edited history direction invalidates it.
        with closing(sqlite3.connect(self.scope.friend_dir / 'history.sqlite3')) as db:
            with db:
                db.execute("UPDATE messages SET direction='me' WHERE seq=1")
        reopened = InterestStore(self.scope)
        with self.assertRaises(ScopeMismatch):
            reopened.upsert(self.entry('stale'))
        self.assertNotIn('same', {item['id'] for item in reopened.entries(
            '2026-10-06T00:00:00+00:00')})
        reopened.close()


if __name__ == '__main__':
    unittest.main()
