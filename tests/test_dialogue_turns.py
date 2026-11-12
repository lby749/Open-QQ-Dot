import json
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import persona_store as storage
from dataclasses import replace
from pathlib import Path

from dataset_normalizer import privacy
from dataset_registry import DatasetRecord, DatasetRegistry
from dialogue_turns import (MAX_SESSION_MESSAGES, DialogueStore, Session, SessionSegmenter,
                            build_turns)
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import (DataScope, FriendScope, InvalidSource, NormalizedMessage,
                           PackVersion, PrivacyView, PrivacyViolation, SourceOrigin, SourceRef)


class DialogueTurnTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friend_dir = self.root / 'friends' / '12345'
        friend_dir.mkdir(parents=True)
        (friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(friend_dir)}}}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root,
            allowed_pack_versions=[PackVersion(pack_id='fixture-pack', version='1')])

    def message(self, mid, *, speaker='human', role='user', timestamp=None, text=None,
                dataset='private', import_id='imp', record=None, privacy=PrivacyView.LOCAL_FULL,
                scope=DataScope.PRIVATE_FRIEND, pack=None, pack_version=None):
        if record is None:
            record = mid
        ref = SourceRef(dataset_id=dataset, import_id=import_id, record_id=record,
            message_seq=None, speaker_id=speaker, origin=SourceOrigin.REAL_SINGLE_SPEAKER,
            data_scope=scope, privacy_view=privacy,
            friend_id='12345' if scope == DataScope.PRIVATE_FRIEND else None,
            event_time=timestamp, recorded_at='2026-10-04T12:00:00+00:00',
            pack_id=pack, pack_version=pack_version,
            license_id='fixture-license' if scope != DataScope.PRIVATE_FRIEND else None,
            model_analysis_allowed=scope != DataScope.PRIVATE_FRIEND)
        return NormalizedMessage(id=mid, timestamp=timestamp, speaker_id=speaker,
            speaker_role=role, text=text or mid, message_type='text', media_id=None,
            source=ref, target_role='unmapped', target_selected=False)

    def test_turns_keep_short_bubbles_and_link_speaker_changes(self):
        messages = [self.message('a'), self.message('b'),
                    self.message('c', speaker='other')]
        session = next(SessionSegmenter(self.scope).segment(messages))
        turns = build_turns(session)
        self.assertEqual([[item.id for item in turn.bubbles] for turn in turns], [['a', 'b'], ['c']])
        self.assertIsNone(turns[0].previous_turn_id)
        self.assertEqual(turns[1].previous_turn_id, turns[0].id)
        self.assertEqual(session.speaker_set, ('human', 'other'))

    def test_preserves_input_order_and_same_text_distinct_records(self):
        messages = [self.message('z', text='yeah'), self.message('a', text='yeah')]
        session = next(SessionSegmenter(self.scope).segment(messages))
        self.assertEqual([item.id for item in session.messages], ['z', 'a'])
        self.assertEqual(len(build_turns(session)[0].bubbles), 2)

    def test_gap_split_and_space_separated_aware_iso(self):
        messages = [self.message('a', timestamp='2026-10-04 10:00:00+00:00'),
                    self.message('b', timestamp='2026-10-04 11:00:01+00:00')]
        sessions = list(SessionSegmenter(self.scope).segment(messages, gap_seconds=3600))
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].time_quality, 'KNOWN')

    def test_missing_naive_and_date_only_times_do_not_infer_gap(self):
        messages = [self.message('a', timestamp='2026-10-04'),
                    self.message('b', timestamp='2026-10-04T00:00:00'),
                    self.message('c', timestamp=None)]
        sessions = list(SessionSegmenter(self.scope).segment(messages, gap_seconds=0))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].time_quality, 'UNKNOWN')
        self.assertEqual([m.timestamp for m in sessions[0].messages],
                         ['2026-10-04', '2026-10-04T00:00:00', None])

    def test_backward_time_splits_without_sorting(self):
        messages = [self.message('later', timestamp='2026-10-04T12:00:00+00:00'),
                    self.message('earlier', timestamp='2026-10-04T11:59:59+00:00')]
        self.assertEqual(len(list(SessionSegmenter(self.scope).segment(messages))), 2)

    def test_public_without_conversation_id_never_merges(self):
        messages = [self.message('a', scope=DataScope.PUBLIC_DATASET, dataset='pub',
                                 import_id='pubimp'),
                    self.message('b', scope=DataScope.PUBLIC_DATASET, dataset='pub',
                                 import_id='pubimp')]
        self.assertEqual(len(list(SessionSegmenter(self.scope).segment(messages))), 2)
        sessions = list(SessionSegmenter(self.scope).segment(messages,
                         conversation_ids={'a': 'chat-1', 'b': 'chat-1'}))
        self.assertEqual(len(sessions), 1)

    def test_boundaries_include_privacy_and_dataset_import(self):
        messages = [self.message('a'), self.message('b', privacy=PrivacyView.CLOUD_SAFE),
                    self.message('c', dataset='other'), self.message('d', import_id='other')]
        self.assertEqual(len(list(SessionSegmenter(self.scope).segment(messages))), 4)

    def test_unknown_speakers_are_not_merged(self):
        messages = [self.message('a', speaker='unknown:a', role='unknown'),
                    self.message('b', speaker='unknown:b', role='unknown')]
        session = next(SessionSegmenter(self.scope).segment(messages))
        self.assertEqual(len(build_turns(session)), 2)

    def test_duplicate_id_and_oversized_session_are_controlled_errors(self):
        duplicate = self.message('same')
        with self.assertRaises(InvalidSource):
            list(SessionSegmenter(self.scope).segment([duplicate, duplicate]))
        forged_session = next(SessionSegmenter(self.scope).segment([duplicate]))
        with self.assertRaises(InvalidSource):
            build_turns(replace(forged_session, messages=(duplicate, duplicate)))
        messages = (self.message(str(i)) for i in range(MAX_SESSION_MESSAGES + 1))
        with self.assertRaises(Unavailable):
            list(SessionSegmenter(self.scope).segment(messages))

    def test_public_pack_requires_scope_allowlist(self):
        packed = self.message('a', scope=DataScope.PUBLIC_PACK, dataset='pub', import_id='i',
                              pack='sample', pack_version='1')
        with self.assertRaises(ScopeMismatch):
            list(SessionSegmenter(self.scope).segment([packed]))
        allowed = FriendScope.load('12345', project_root=self.root,
                                   allowed_pack_versions=[PackVersion(pack_id='sample', version='1')])
        self.assertEqual(len(list(SessionSegmenter(allowed).segment([packed]))), 1)

    def test_empty_input_invalid_gap_and_binding_change(self):
        self.assertEqual(list(SessionSegmenter(self.scope).segment([])), [])
        with self.assertRaises(InvalidSource):
            list(SessionSegmenter(self.scope).segment([], gap_seconds=-1))
        pending = SessionSegmenter(self.scope).segment([self.message('a')])
        (self.root / 'friends' / '12345' / 'friend.json').write_text(
            json.dumps({'qq_id': '99999'}), encoding='utf-8')
        with self.assertRaises(ScopeMismatch):
            next(pending)


class DialogueStoreTests(unittest.TestCase):
    def setUp(self):
        if self._testMethodName == 'test_v3_sidecar_migrates_before_readonly_registry_and_keeps_import_backup':
            version_four = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:4])
            version_four.start()
            self.addCleanup(version_four.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friend_dir = self.root / 'friends' / '12345'
        friend_dir.mkdir(parents=True)
        (friend_dir / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(friend_dir)}}}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root,
            allowed_pack_versions=[PackVersion(pack_id='fixture-pack', version='1')])
        bootstrap = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        bootstrap.close()
        registry = DatasetRegistry(self.scope)
        registry.register(DatasetRecord(dataset_id='private', name='fixture',
            data_scope='PRIVATE_FRIEND', usage_scope='local only', local_use_allowed=True,
            model_analysis_allowed=True, license_data='private'), registry.revision)
        registry.register(DatasetRecord(dataset_id='public', name='public fixture',
            data_scope='PUBLIC_DATASET', usage_scope='local fixture', local_use_allowed=True,
            model_analysis_allowed=True, license_data='public-license'), registry.revision)
        now = '2026-10-04T12:00:00+00:00'
        registry.db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,source_origin,'
            'data_scope,source_hash,mapping_hash,settings_hash,checkpoint_json,status,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('12345', 'imp', 'private', 'REAL_SINGLE_SPEAKER',
            'PRIVATE_FRIEND', 'a' * 64, 'b' * 64, 'c' * 64, '{}', 'COMPLETED', now, now))
        registry.db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,source_origin,'
            'data_scope,source_hash,mapping_hash,settings_hash,checkpoint_json,status,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', ('12345', 'pubimp', 'public', 'PUBLIC_DATASET',
            'PUBLIC_DATASET', 'd' * 64, 'e' * 64, 'f' * 64, '{}', 'COMPLETED', now, now))
        rows = ((0, 'yeah', '2026-10-04T10:00:00+00:00', None),
                (1, 'yeah', '2026-10-04T10:00:01+00:00', None),
                (2, 'later', '2026-10-04T12:00:00+00:00', None),
                (3, 'other', '2026-10-04T12:00:01+00:00', None),
                (4, '联系我 13800138000，邮箱 a.person@example.com，https://example.test/?token=secret',
                 '2026-10-04T12:00:02+00:00', 'asset-4'))
        for seq, body, event, media in rows:
            fingerprint = hashlib.sha256(f'imp:r{seq}:{seq}'.encode()).hexdigest()
            registry.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
                'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,message_type,media_id,'
                'content_fingerprint,source_origin,source_record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('12345', 'imp', 'private', f'r{seq}', seq,
                 'other' if seq == 3 else 'human', 'user', 'in', event, now,
                 body, 'image' if media else 'text', media, fingerprint,
                 'REAL_SINGLE_SPEAKER', '{}'))
        registry.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
            'message_seq,speaker_id,speaker_role,direction,event_time,recorded_at,text,message_type,media_id,'
            'content_fingerprint,source_origin,source_record_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('12345', 'pubimp', 'public', 'pubrec', 0, 'public-speaker', 'user', 'in', None, now,
             'public context', 'text', None, hashlib.sha256(b'public-fixture').hexdigest(),
             'PUBLIC_DATASET', '{}'))
        registry.close()
        self.dialogues = DialogueStore(self.scope)
        self.addCleanup(self.dialogues.close)
        self.messages = list(self.dialogues.normalizer.normalize([
            {'friend_id': '12345', 'dataset_id': 'private', 'import_id': 'imp',
             'record_id': f'r{i}', 'message_seq': i} for i in range(5)]))

    def test_round_trip_full_bubbles_and_idempotent_replay(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[:2]))
        revision = self.dialogues.revision
        self.assertEqual(self.dialogues.save_session(session, revision), revision + 1)
        self.assertEqual(self.dialogues.load_session(session.id), session)
        self.assertEqual(self.dialogues.save_session(session, revision), revision + 1)
        self.assertEqual(self.dialogues.revision, revision + 1)
        grouped = next(SessionSegmenter(self.scope).segment(self.messages[:2],
                       conversation_ids={item.id: 'explicit-chat' for item in self.messages[:2]}))
        self.dialogues.save_session(grouped, self.dialogues.revision)
        self.assertEqual(self.dialogues.load_session(grouped.id), grouped)

    def test_list_sessions_returns_bounded_metadata_without_message_text(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[:2]))
        self.dialogues.save_session(session, self.dialogues.revision)
        rows = self.dialogues.list_sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], session.id)
        self.assertEqual(rows[0]['speaker_ids'], session.speaker_set)
        self.assertNotIn('messages', rows[0])
        with self.assertRaises(InvalidSource):
            self.dialogues.list_sessions(0)

    def test_cloudsafe_projection_round_trips_but_cannot_restore_local_full(self):
        safe = list(privacy(self.messages[:1], PrivacyView.CLOUD_SAFE).messages)
        session = next(SessionSegmenter(self.scope).segment(safe))
        revision = self.dialogues.revision
        self.dialogues.save_session(session, revision)
        self.assertEqual(self.dialogues.load_session(session.id), session)
        forged = replace(safe[0], source=replace(safe[0].source,
                                                 privacy_view=PrivacyView.LOCAL_FULL))
        forged_session = replace(session, messages=(forged,))
        with self.assertRaises(InvalidSource):
            self.dialogues.save_session(forged_session, self.dialogues.revision)

    def test_actual_pii_redaction_and_media_reference_round_trip(self):
        full = self.messages[4]
        result = privacy([full], PrivacyView.CLOUD_SAFE)
        safe = list(result.messages)[0]
        self.assertNotIn('13800138000', safe.text)
        self.assertNotIn('a.person@example.com', safe.text)
        self.assertNotIn('token=secret', safe.text)
        self.assertEqual((safe.media_id, safe.message_type), ('asset-4', 'image'))
        session = next(SessionSegmenter(self.scope).segment([safe]))
        self.dialogues.save_session(session, self.dialogues.revision)
        loaded = self.dialogues.load_session(session.id)
        self.assertEqual(loaded, session)
        self.assertEqual(loaded.messages[0].media_id, 'asset-4')

    def test_public_pack_without_manifest_is_not_persisted(self):
        base = list(self.dialogues.normalizer.normalize([{'friend_id': '12345',
            'dataset_id': 'public', 'import_id': 'pubimp', 'record_id': 'pubrec',
            'message_seq': 0}]))[0]
        packed_source = replace(base.source, data_scope=DataScope.PUBLIC_PACK,
            pack_id='fixture-pack', pack_version='1')
        packed = replace(base, source=packed_source)
        session = next(SessionSegmenter(self.scope).segment([packed],
                       conversation_ids={packed.id: 'public-chat'}))
        with self.assertRaises(Unavailable):
            self.dialogues.save_session(session, self.dialogues.revision)

    def test_v3_sidecar_migrates_before_readonly_registry_and_keeps_import_backup(self):
        sidecar = self.scope.friend_dir / 'persona.sqlite3'
        self.dialogues.close()
        db = sqlite3.connect(sidecar)
        try:
            before = db.execute('SELECT count(*) FROM imported_messages').fetchone()[0]
            db.execute('DROP TABLE dialogue_turns')
            db.execute('DROP TABLE dialogue_sessions')
            db.execute('DELETE FROM schema_migrations WHERE version=4')
            db.commit()
        finally:
            db.close()
        self.dialogues = DialogueStore(self.scope)
        self.addCleanup(self.dialogues.close)
        self.assertEqual(self.dialogues.db.execute(
            'SELECT count(*) FROM imported_messages').fetchone()[0], before)
        self.assertEqual(self.dialogues.db.execute(
            'SELECT max(version) FROM schema_migrations').fetchone()[0], 4)
        backups = sorted(self.scope.friend_dir.glob('persona-schema-v3-before-v4-*.backup.sqlite3'))
        self.assertTrue(backups)
        backup = sqlite3.connect(backups[-1])
        try:
            self.assertEqual(backup.execute('SELECT count(*) FROM imported_messages').fetchone()[0], before)
            self.assertEqual(backup.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 3)
        finally:
            backup.close()

    def test_tampered_bubble_and_incomplete_session_are_rejected(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[:1]))
        forged = replace(self.messages[0], text='edited')
        with self.assertRaises(InvalidSource):
            self.dialogues.save_session(replace(session, messages=(forged,)), self.dialogues.revision)
        separate = list(SessionSegmenter(self.scope).segment(
            [self.messages[0], self.messages[2]], gap_seconds=1800))
        merged = replace(separate[0], messages=(self.messages[0], self.messages[2]),
                         end_time=self.messages[2].timestamp)
        with self.assertRaises(InvalidSource):
            self.dialogues.save_session(merged, self.dialogues.revision)

    def test_cas_and_same_id_different_verified_content_conflict(self):
        first = next(SessionSegmenter(self.scope).segment(self.messages[:1]))
        second = next(SessionSegmenter(self.scope).segment(self.messages[1:2]))
        revision = self.dialogues.revision
        self.dialogues.save_session(first, revision)
        with self.assertRaises(Conflict):
            self.dialogues.save_session(second, revision)
        conflicting = replace(second, id=first.id)
        with self.assertRaises(Conflict):
            self.dialogues.save_session(conflicting, self.dialogues.revision)
        for invalid in (True, -1, None):
            with self.assertRaises(InvalidSource):
                self.dialogues.save_session(second, invalid)

    def test_unpersisted_locator_is_rejected(self):
        forged = replace(self.messages[0], id='import:["private","imp","missing",0]')
        session = next(SessionSegmenter(self.scope).segment([forged]))
        with self.assertRaises(InvalidSource):
            self.dialogues.save_session(session, self.dialogues.revision)

    def test_malformed_stored_bubbles_use_fixed_error_without_payload(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[:1]))
        self.dialogues.save_session(session, self.dialogues.revision)
        row = self.dialogues.db.execute('SELECT id,bubbles_json FROM dialogue_turns '
            'WHERE friend_id=? AND session_id=?', ('12345', session.id)).fetchone()
        original = row['bubbles_json']
        for bad in ('not-json', '{}', '[{"id":"secret-body"}]'):
            self.dialogues.db.execute('UPDATE dialogue_turns SET bubbles_json=? WHERE friend_id=? AND id=?',
                                      (bad, '12345', row['id']))
            with self.assertRaises(InvalidSource) as caught:
                self.dialogues.load_session(session.id)
            self.assertEqual(str(caught.exception), 'stored dialogue projection is invalid')
            self.assertNotIn('secret-body', str(caught.exception))
        self.dialogues.db.execute('UPDATE dialogue_turns SET bubbles_json=? WHERE friend_id=? AND id=?',
                                  (original, '12345', row['id']))
        self.dialogues.db.execute('UPDATE dialogue_sessions SET boundary_key=? WHERE friend_id=? AND id=?',
                                  ('[]', '12345', session.id))
        with self.assertRaises(InvalidSource) as caught:
            self.dialogues.load_session(session.id)
        self.assertEqual(str(caught.exception), 'stored dialogue projection is invalid')

    def test_stored_invalid_origin_preserves_privacy_violation(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[:1]))
        self.dialogues.save_session(session, self.dialogues.revision)
        row = self.dialogues.db.execute('SELECT id,bubbles_json FROM dialogue_turns '
            'WHERE friend_id=? AND session_id=?', ('12345', session.id)).fetchone()
        bubble = json.loads(row['bubbles_json'])
        bubble[0]['source'].update(data_scope='PUBLIC_DATASET', friend_id=None,
                                   origin='REAL_RELATIONSHIP')
        self.dialogues.db.execute('UPDATE dialogue_turns SET bubbles_json=? WHERE friend_id=? AND id=?',
            (json.dumps(bubble), '12345', row['id']))
        with self.assertRaises(PrivacyViolation):
            self.dialogues.load_session(session.id)

    def test_second_turn_insert_failure_rolls_back_entire_session_and_revision(self):
        session = next(SessionSegmenter(self.scope).segment(self.messages[2:4]))
        self.assertEqual(len(build_turns(session)), 2)
        revision = self.dialogues.revision
        self.dialogues.db.execute("CREATE TEMP TRIGGER fail_second_turn BEFORE INSERT ON dialogue_turns "
            "WHEN NEW.ordinal=1 BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.dialogues.save_session(session, revision)
        self.assertEqual(self.dialogues.revision, revision)
        self.assertEqual(self.dialogues.db.execute('SELECT count(*) FROM dialogue_sessions WHERE friend_id=? '
            'AND id=?', ('12345', session.id)).fetchone()[0], 0)
        self.assertEqual(self.dialogues.db.execute('SELECT count(*) FROM dialogue_turns WHERE friend_id=? '
            'AND session_id=?', ('12345', session.id)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
