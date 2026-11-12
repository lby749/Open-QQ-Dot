from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage
from persona_feedback_store import PersonaFeedbackStore
from persona_store import Conflict, SchemaMismatch, ScopeMismatch, Unavailable
from persona_types import FriendScope


class PersonaFeedbackStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        registry = {'friends': {}}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': friend_id,
                'model_api': {'model': 'unchanged'}, 'proactive': {'enabled': False}}), encoding='utf-8')
            registry['friends'][friend_id] = {'qq_id': friend_id, 'directory': str(directory)}
        (self.root / 'friends.json').write_text(json.dumps(registry), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.other_scope = FriendScope.load('23456', project_root=self.root)

    def open(self, scope=None, *, readonly=False):
        store = PersonaFeedbackStore(scope or self.scope, readonly=readonly)
        self.addCleanup(store.close)
        return store

    def entry(self, **changes):
        return dict({'friend_id': '12345', 'comparison_id': 'comparison-1',
            'a_version': 'legacy-v1', 'b_version': 'context-v2',
            'comparison_kind': 'context_only', 'choice': 'A'}, **changes)

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_v17_upgrade_has_online_backup_and_preserves_old_data_and_hashes(self):
        old_migrations = storage.MIGRATIONS[:17]
        with patch.object(storage, 'MIGRATIONS', old_migrations):
            with closing(storage.PersonaStore('12345', self.scope.friend_dir,
                    project_root=self.root)) as old:
                old.db.execute('UPDATE scope_state SET revision=7')
                old.db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)',
                    ('12345', 'existing', 'COMPLETED', 'versions/existing/manifest.json',
                     'a' * 64, '2026-10-08T00:00:00+00:00'))
                before = tuple(old.db.iterdump())
                hashes = tuple(tuple(row) for row in old.db.execute(
                    'SELECT version,migration_hash FROM schema_migrations ORDER BY version'))
        store = self.open()
        self.assertEqual(store._store.revision, 7)
        self.assertEqual(tuple(tuple(row) for row in store._store.db.execute(
            'SELECT version,migration_hash FROM schema_migrations WHERE version<=17 ORDER BY version')), hashes)
        self.assertEqual(store._store.db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 18)
        with closing(sqlite3.connect(store._store.backup_path)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before)
            self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='persona_feedback'").fetchone())
        self.assertEqual(tuple(storage.MIGRATIONS[:17]), old_migrations)

    def test_failed_v18_ddl_rolls_back_the_entire_migration(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:17]):
            storage.PersonaStore('12345', self.scope.friend_dir, project_root=self.root).close()
        path = self.scope.friend_dir / 'persona.sqlite3'
        before = path.read_bytes()
        broken = storage.MIGRATIONS[:17] + ((18, storage.FEEDBACK_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            storage.PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        self.assertEqual(path.read_bytes(), before)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 17)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='persona_feedback'").fetchone())
        self.open().close()

    def test_readonly_missing_old_bad_hash_and_incomplete_schema_never_write(self):
        before = self.files()
        with self.assertRaises(Unavailable):
            PersonaFeedbackStore(self.scope)
        self.assertEqual(self.files(), before)
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:17]):
            storage.PersonaStore('12345', self.scope.friend_dir, project_root=self.root).close()
        before = self.files()
        with self.assertRaises(SchemaMismatch):
            PersonaFeedbackStore(self.scope)
        self.assertEqual(self.files(), before)
        self.open().close()
        path = self.scope.friend_dir / 'persona.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE schema_migrations SET migration_hash='bad' WHERE version=18")
        before = self.files()
        with self.assertRaises(SchemaMismatch):
            PersonaFeedbackStore(self.scope)
        self.assertEqual(self.files(), before)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE schema_migrations SET migration_hash=? WHERE version=18',
                       (storage.migration_hash(storage.FEEDBACK_STATEMENTS),))
            db.execute('DROP TABLE persona_feedback')
        before = self.files()
        with self.assertRaises(SchemaMismatch):
            PersonaFeedbackStore(self.scope)
        self.assertEqual(self.files(), before)

    def test_repeated_initialization_and_readonly_access_leave_all_files_unchanged(self):
        writer = self.open()
        saved = writer.save(self.entry())
        writer.close()
        before = self.files()
        repeated = self.open()
        self.assertIsNone(repeated._store.backup_path)
        repeated.close()
        reader = self.open(readonly=True)
        self.assertEqual(reader.get('comparison-1'), saved)
        self.assertEqual(reader.list_recent(), (saved,))
        with self.assertRaises(Unavailable):
            reader.save(self.entry())
        with self.assertRaises(sqlite3.OperationalError):
            reader._store.db.execute('DELETE FROM persona_feedback')
        reader.close()
        self.assertEqual(self.files(), before)

    def test_idempotent_submission_and_conflict_preserve_human_feedback(self):
        store = self.open()
        saved = store.save(self.entry())
        self.assertEqual(saved['source_type'], 'HUMAN_SUBJECTIVE')
        self.assertEqual(datetime.fromisoformat(saved['created_at']).utcoffset(), timezone.utc.utcoffset(None))
        before = self.files()
        self.assertEqual(store.save(self.entry()), saved)
        self.assertEqual(self.files(), before)
        for changes in ({'choice': 'B'}, {'a_version': 'different'}, {'b_version': 'different'},
                        {'comparison_kind': 'offline_reply'}):
            with self.subTest(changes=changes), self.assertRaises(Conflict):
                store.save(self.entry(**changes))
        self.assertEqual(store.get('comparison-1'), saved)
        self.assertEqual(len(store.list_recent()), 1)
        saved['choice'] = 'neither'
        self.assertEqual(store.get('comparison-1')['choice'], 'A')

    def test_metadata_whitelist_choices_kinds_and_identifier_limits(self):
        store = self.open()
        invalid = [self.entry(prompt='private text'), self.entry(created_at='caller-time'),
                   self.entry(choice='model-score'), self.entry(comparison_kind='live_reply')]
        for name in ('comparison_id', 'a_version', 'b_version'):
            for value in ('', ' ', 'x' * 129, 'embedded\0text', None):
                invalid.append(self.entry(**{name: value}))
        missing = self.entry()
        del missing['choice']
        invalid.append(missing)
        for entry in invalid:
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                store.save(entry)
        self.assertEqual(store.list_recent(), ())

    def test_sql_constraints_and_cross_friend_storage(self):
        first, other = self.open(), self.open(self.other_scope)
        row = ('12345', 'sql-comparison', 'a1', 'b1', 'context_only', 'A',
               'HUMAN_SUBJECTIVE', '2026-10-08T00:00:00+00:00')
        for index, value in ((0, '23456'), (1, ''), (2, 'x' * 129), (3, ' '),
                             (4, 'live'), (5, 'unknown'), (6, 'MODEL_EVALUATION'), (7, '')):
            bad = list(row)
            bad[index] = value
            with self.subTest(index=index), self.assertRaises(sqlite3.IntegrityError):
                first._store.db.execute('INSERT INTO persona_feedback VALUES(?,?,?,?,?,?,?,?)', bad)
        saved = first.save(self.entry())
        with self.assertRaises(ScopeMismatch):
            first.save(self.entry(friend_id='23456'))
        self.assertIsNone(other.get(saved['comparison_id']))
        other_saved = other.save(self.entry(friend_id='23456', choice='B'))
        self.assertEqual(first.get(saved['comparison_id'])['choice'], 'A')
        self.assertEqual(other_saved['choice'], 'B')
        self.assertEqual({row['friend_id'] for row in other.list_recent()}, {'23456'})

    def test_source_identity_change_and_other_root_cannot_redirect_store(self):
        store = self.open()
        saved = store.save(self.entry())
        with self.assertRaises(ScopeMismatch):
            FriendScope.load('12345', project_root=self.root / 'other', friend_dir=self.scope.friend_dir)
        profile = self.scope.friend_dir / 'friend.json'
        profile.write_text(json.dumps({'qq_id': '23456'}), encoding='utf-8')
        for operation in (lambda: store.get('comparison-1'), store.list_recent,
                          lambda: store.save(self.entry(comparison_id='later'))):
            with self.assertRaises(ScopeMismatch):
                operation()
        self.assertEqual(store._store.db.execute('SELECT choice FROM persona_feedback').fetchone()[0], saved['choice'])
        self.assertEqual(store._store.db.execute('SELECT COUNT(*) FROM persona_feedback').fetchone()[0], 1)

    def test_insert_failure_rolls_back_and_connection_can_retry(self):
        store = self.open()
        store._store.db.execute("CREATE TEMP TRIGGER reject_feedback AFTER INSERT ON persona_feedback "
            "WHEN NEW.comparison_id='blocked' BEGIN SELECT RAISE(ABORT,'fixture insert failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            store.save(self.entry(comparison_id='blocked'))
        self.assertFalse(store._store.db.in_transaction)
        self.assertIsNone(store.get('blocked'))
        self.assertEqual(store.save(self.entry())['choice'], 'A')

    def test_same_friend_in_two_valid_project_roots_is_isolated(self):
        first = self.open()
        saved = first.save(self.entry())
        second_root = self.root / 'second-project'
        directory = second_root / 'friends' / '12345'
        directory.mkdir(parents=True)
        (directory / 'friend.json').write_text('{"qq_id":"12345"}', encoding='utf-8')
        (second_root / 'friends.json').write_text(json.dumps({'friends': {
            '12345': {'qq_id': '12345', 'directory': str(directory)}}}), encoding='utf-8')
        second_scope = FriendScope.load('12345', project_root=second_root)
        second = self.open(second_scope)
        self.assertIsNone(second.get(saved['comparison_id']))
        second.save(self.entry(choice='B'))
        self.assertEqual(first.get(saved['comparison_id']), saved)
        self.assertEqual(second.get(saved['comparison_id'])['choice'], 'B')

    def test_feedback_preserves_history_config_persona_versions_and_source_revisions(self):
        history = self.scope.friend_dir / 'history.sqlite3'
        with closing(sqlite3.connect(history)) as db, db:
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO metadata VALUES('unanswered_rounds','7')")
        versions = self.scope.friend_dir / 'persona_versions'
        versions.mkdir()
        (versions / 'active.json').write_text('{"version":"unchanged"}', encoding='utf-8')
        store = self.open()
        files = (history, self.scope.friend_dir / 'friend.json', self.root / 'friends.json', versions / 'active.json')
        before = tuple(path.read_bytes() for path in files)
        scope_revision = store._store.revision
        source_revision = tuple(store._store.db.execute('SELECT * FROM source_state').fetchone())
        with patch('socket.create_connection', side_effect=AssertionError('network')), \
             patch('urllib.request.urlopen', side_effect=AssertionError('network')):
            store.save(self.entry())
        self.assertEqual(tuple(path.read_bytes() for path in files), before)
        self.assertEqual(store._store.revision, scope_revision)
        self.assertEqual(tuple(store._store.db.execute('SELECT * FROM source_state').fetchone()), source_revision)
        self.assertEqual(store._store.db.execute('SELECT COUNT(*) FROM analysis_jobs').fetchone()[0], 0)
        self.assertEqual(store._store.db.execute('SELECT COUNT(*) FROM artifact_versions').fetchone()[0], 0)

    def test_recent_read_is_bounded_and_read_after_close_is_unavailable(self):
        store = self.open()
        for index in range(3):
            store.save(self.entry(comparison_id=f'comparison-{index}'))
        self.assertEqual(len(store.list_recent(2)), 2)
        self.assertEqual(len(store.list_recent()), 3)
        times = [row['created_at'] for row in store.list_recent()]
        self.assertEqual(times, sorted(times, reverse=True))
        for limit in (True, 0, 501, '1'):
            with self.assertRaises(ValueError):
                store.list_recent(limit)
        store.close()
        with self.assertRaises(Unavailable):
            store.get('comparison-1')
        with self.assertRaises(Unavailable):
            store.save(self.entry())


if __name__ == '__main__':
    unittest.main()
