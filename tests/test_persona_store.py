import json
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch

import persona_store as storage
from persona_store import PersonaStore, ScopeMismatch, Unavailable, SchemaMismatch, Conflict


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.friend_id = '12345'
        self.directory = self.root / 'friends' / self.friend_id
        # Keep the original V1 recovery/constraints fixtures at their actual version.
        version_one = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:1])
        version_one.start()
        self.addCleanup(version_one.stop)

    def store(self, friend_id=None, directory=None, readonly=False):
        store = PersonaStore(friend_id or self.friend_id, directory or self.directory,
                             readonly, project_root=self.root)
        self.addCleanup(store.close)
        return store

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def artifact(self, identifier='v1', **changes):
        data = dict(friend_id=self.friend_id, id=identifier, state='STAGED',
                    manifest_relpath=f'persona/versions/{identifier}/version_manifest.json',
                    manifest_sha256='a' * 64, created_at='2026-10-04T10:00:00+09:00')
        data.update(changes)
        return data

    def directory_link(self, link, target):
        self.assertTrue(link.absolute().is_relative_to(self.root))
        self.assertTrue(target.resolve().is_relative_to(self.root))
        try:
            link.symlink_to(target, target_is_directory=True)
            self.addCleanup(link.unlink)
        except OSError:
            if not hasattr(subprocess, 'CREATE_NO_WINDOW'):
                raise
            subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(target)], check=True,
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.addCleanup(link.rmdir)

    def test_first_initialization_has_only_three_tables_and_expected_constraints(self):
        store = self.store()
        tables = {r[0] for r in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {'schema_migrations', 'scope_state', 'artifact_versions'})
        self.assertEqual(store.revision, 0)
        self.assertIsNone(store.backup_path)
        self.assertEqual(list(self.directory.glob('*.backup.sqlite3')), [])
        self.assertEqual(store.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(store.db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
        self.assertEqual([r[1] for r in store.db.execute("PRAGMA table_info('artifact_versions')")
                          if r[5]], ['friend_id', 'id'])
        fk = store.db.execute("PRAGMA foreign_key_list('artifact_versions')").fetchone()
        self.assertEqual((fk[2], fk[3], fk[4]), ('scope_state', 'friend_id', 'friend_id'))
        version, digest = store.db.execute('SELECT version,migration_hash FROM schema_migrations').fetchone()
        self.assertEqual((version, digest), (1, storage.migration_hash(storage.SCHEMA_STATEMENTS)))

    def test_reopening_is_byte_identical_and_does_not_duplicate_migrations(self):
        self.store().close()
        before = self.snapshot()
        store = self.store()
        self.assertEqual(store.db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 1)
        store.close()
        self.assertEqual(before, self.snapshot())

    def test_missing_readonly_sidecar_creates_neither_directory_nor_file(self):
        before = list(self.root.rglob('*'))
        with self.assertRaises(Unavailable):
            self.store(readonly=True)
        self.assertEqual(before, list(self.root.rglob('*')))

    def test_readonly_query_and_rejected_writes_leave_all_files_unchanged(self):
        self.store().close()
        before = self.snapshot()
        directories = list(self.root.rglob('*'))
        store = self.store(readonly=True)
        self.assertEqual(store.db.execute('PRAGMA query_only').fetchone()[0], 1)
        self.assertEqual(store.db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(store.artifacts(), [])
        for sql in ['CREATE TABLE bad(id)', 'UPDATE scope_state SET revision=99']:
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute(sql)
        with self.assertRaises(Unavailable):
            store.write_batch(0, [])
        store.close()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(directories, list(self.root.rglob('*')))

    def test_readonly_wal_header_rejected_before_sqlite_and_creates_no_shm(self):
        store = self.store()
        store.db.execute('PRAGMA journal_mode=WAL')
        store.close()
        before = self.snapshot()
        entries = list(self.root.rglob('*'))
        with patch.object(storage.sqlite3, 'connect', side_effect=AssertionError('must not connect')):
            with self.assertRaises(Unavailable):
                self.store(readonly=True)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(entries, list(self.root.rglob('*')))

    def test_readonly_existing_wal_or_shm_files_rejected_before_connect(self):
        self.store().close()
        for suffix in ('-wal', '-shm'):
            extra = self.directory / ('persona.sqlite3' + suffix)
            extra.write_bytes(b'fixture')
            before = self.snapshot()
            with patch.object(storage.sqlite3, 'connect', side_effect=AssertionError('must not connect')):
                with self.assertRaises(Unavailable):
                    self.store(readonly=True)
            self.assertEqual(before, self.snapshot())
            extra.unlink()

    def test_bad_scalar_ids_and_wrong_project_directories_fail_before_mkdir(self):
        for identifier in [12345, '', '1234', '1234567890123', '', '12345x']:
            with self.subTest(identifier=identifier), self.assertRaises(ScopeMismatch):
                PersonaStore(identifier, self.directory, project_root=self.root)
        for directory in [self.root / 'other' / self.friend_id,
                          self.root / 'friends' / '67890', self.directory / 'nested']:
            with self.subTest(directory=directory), self.assertRaises(ScopeMismatch):
                self.store(directory=directory)
        self.assertEqual(list(self.root.rglob('*')), [])

    def test_existing_other_scope_rejected_without_ddl_or_changes(self):
        other = self.store('67890', self.root / 'friends' / '67890')
        other.close()
        self.directory.mkdir()
        shutil.copy2(other.path, self.directory / 'persona.sqlite3')
        before = self.snapshot()
        for readonly in (False, True):
            with self.subTest(readonly=readonly), self.assertRaises(ScopeMismatch):
                self.store(readonly=readonly)
        self.assertEqual(before, self.snapshot())

    def test_sidecar_symlink_to_history_rejected_without_touching_history(self):
        self.directory.mkdir(parents=True)
        history = self.directory / 'history.sqlite3'
        with closing(sqlite3.connect(history, isolation_level=None)) as db:
            db.execute('CREATE TABLE messages(body TEXT)')
            db.execute("INSERT INTO messages VALUES('fixture')")
        try:
            (self.directory / 'persona.sqlite3').symlink_to(history)
        except OSError:
            (self.directory / 'persona.sqlite3').hardlink_to(history)
        before = self.snapshot()
        with self.assertRaises(ScopeMismatch):
            self.store()
        self.assertEqual(before, self.snapshot())

    def test_friend_directory_symlink_outside_project_rejected(self):
        friends = self.root / 'friends'
        friends.mkdir()
        outside = self.root / 'outside' / self.friend_id
        outside.mkdir(parents=True)
        self.directory_link(self.directory, outside)
        with self.assertRaises(ScopeMismatch):
            self.store()
        self.assertFalse((outside / 'persona.sqlite3').exists())

    def test_project_friends_directory_symlink_outside_project_rejected(self):
        outside = self.root / 'outside'
        (outside / self.friend_id).mkdir(parents=True)
        self.directory_link(self.root / 'friends', outside)
        with self.assertRaises(ScopeMismatch):
            self.store()
        self.assertFalse((outside / self.friend_id / 'persona.sqlite3').exists())

    def test_nonempty_legacy_database_misnamed_as_sidecar_is_not_migrated(self):
        self.directory.mkdir(parents=True)
        path = self.directory / 'persona.sqlite3'
        with closing(sqlite3.connect(path, isolation_level=None)) as db:
            db.execute('CREATE TABLE messages(body TEXT)')
            db.execute("INSERT INTO messages VALUES('preserve')")
        before = self.snapshot()
        with self.assertRaises(SchemaMismatch):
            self.store()
        self.assertEqual(before, self.snapshot())

    def test_hash_mismatch_and_future_version_rejected_without_writes(self):
        for corrupt in ["UPDATE schema_migrations SET migration_hash='wrong'",
                        "INSERT INTO schema_migrations VALUES(2,'future','now')"]:
            with self.subTest(corrupt=corrupt):
                store = self.store()
                store.db.execute(corrupt)
                store.close()
                before = self.snapshot()
                for readonly in (False, True):
                    with self.assertRaises(SchemaMismatch):
                        self.store(readonly=readonly)
                self.assertEqual(before, self.snapshot())
                (self.directory / 'persona.sqlite3').unlink()

    def test_first_migration_failure_rolls_back_all_tables(self):
        failed = storage.SCHEMA_STATEMENTS + ('SELECT * FROM missing_fixture_table',)
        with patch.object(storage, 'MIGRATIONS', ((1, failed),)):
            with self.assertRaises(sqlite3.OperationalError):
                self.store()
        with closing(sqlite3.connect(self.directory / 'persona.sqlite3')) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [])
        self.assertEqual(self.store().revision, 0)

    def test_failed_forward_migration_restores_existing_database_bytes(self):
        self.store().close()
        before = self.snapshot()
        migration = (2, ('CREATE TABLE future_fixture(id TEXT)', 'SELECT * FROM missing_fixture_table'))
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS + (migration,)):
            with self.assertRaises(sqlite3.OperationalError):
                self.store()
        after = self.snapshot()
        self.assertEqual({key: after[key] for key in before}, before)
        backups = list(self.directory.glob('*.backup.sqlite3'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(set(after) - set(before), {str(backups[0].relative_to(self.root))})
        with closing(sqlite3.connect(backups[0].as_uri() + '?mode=ro', uri=True)) as db:
            self.assertEqual(db.execute('SELECT friend_id FROM scope_state').fetchall(), [(self.friend_id,)])
            self.assertEqual(db.execute('SELECT version FROM schema_migrations').fetchall(), [(1,)])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='future_fixture'").fetchone())

    def test_forward_migration_applies_once_and_readonly_never_upgrades(self):
        original = self.store()
        original.write_batch(0, [self.artifact()])
        original.close()
        migration = (2, ('CREATE TABLE future_fixture(id TEXT)',))
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS + (migration,)):
            before = self.snapshot()
            with self.assertRaises(SchemaMismatch):
                self.store(readonly=True)
            self.assertEqual(before, self.snapshot())
            migrated = self.store()
            self.assertIsNotNone(migrated.backup_path)
            with closing(sqlite3.connect(migrated.backup_path.as_uri() + '?mode=ro', uri=True)) as db:
                self.assertEqual(db.execute('SELECT friend_id,revision FROM scope_state').fetchall(),
                                 [(self.friend_id, 1)])
                self.assertEqual(db.execute('SELECT version FROM schema_migrations').fetchall(), [(1,)])
                self.assertEqual(db.execute('SELECT id FROM artifact_versions').fetchall(), [('v1',)])
            migrated.close()
            after = self.snapshot()
            upgraded = self.store()
            self.assertEqual(upgraded.db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], 2)
            upgraded.close()
            self.assertEqual(after, self.snapshot())

    def test_backup_failure_prevents_upgrade_and_keeps_source_unchanged(self):
        self.store().close()
        before = self.snapshot()
        migration = (2, ('CREATE TABLE future_fixture(id TEXT)',))
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS + (migration,)), \
                patch.object(PersonaStore, '_backup_before_upgrade', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.store()
        self.assertEqual(before, self.snapshot())

    def test_foreign_key_and_duplicate_manifest_constraints_are_real(self):
        store = self.store()
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)',
                             tuple(self.artifact(friend_id='67890').values()))
        store.write_batch(0, [self.artifact()])
        with self.assertRaises(sqlite3.IntegrityError):
            store.write_batch(1, [self.artifact('v2', manifest_relpath=self.artifact()['manifest_relpath'])])
        self.assertEqual(store.revision, 1)
        self.assertEqual([r['id'] for r in store.artifacts()], ['v1'])

    def test_batch_and_checkpoint_revision_rollback_together_on_constraint_error(self):
        store = self.store()
        same_path = self.artifact()['manifest_relpath']
        with self.assertRaises(sqlite3.IntegrityError):
            store.write_batch(0, [self.artifact(), self.artifact('v2', manifest_relpath=same_path)],
                              {'pending_publication_id': 'publication'})
        self.assertEqual(store.artifacts(), [])
        self.assertEqual(store.revision, 0)
        self.assertIsNone(store.db.execute('SELECT pending_publication_id FROM scope_state').fetchone()[0])

    def test_two_connections_cas_rejects_stale_batch_without_overwriting(self):
        first, second = self.store(), self.store()
        self.assertEqual(second.revision, 0)
        self.assertEqual(first.write_batch(0, [self.artifact()], {'pending_publication_id': 'nonce'}), 1)
        with self.assertRaises(Conflict):
            second.write_batch(0, [self.artifact('v2')], {'pending_publication_id': None})
        self.assertEqual(first.db.execute('SELECT pending_publication_id FROM scope_state').fetchone()[0], 'nonce')
        self.assertEqual([r['id'] for r in second.artifacts()], ['v1'])
        second.write_batch(1, [self.artifact(state='COMPLETED')], {'pending_publication_id': None})
        self.assertEqual(first.revision, 2)

    def test_invalid_scope_path_records_and_unsupported_checkpoint_fail_without_changes(self):
        store = self.store()
        bad_records = [self.artifact(friend_id='67890'), self.artifact(manifest_relpath='../escape.json'),
                       self.artifact(manifest_relpath=str(self.root / 'outside.json')),
                       self.artifact(manifest_sha256='wrong'), self.artifact(state='ACTIVE'),
                       dict(self.artifact(), unknown='data')]
        for record in bad_records:
            with self.subTest(record=record), self.assertRaises((ScopeMismatch, ValueError)):
                store.write_batch(0, [record])
        with self.assertRaises(ValueError):
            store.write_batch(0, [], {'job_cursor': 1})
        self.assertEqual(store.revision, 0)
        self.assertEqual(store.artifacts(), [])

    def test_sidecar_operations_do_not_modify_old_database_settings_or_authority_files(self):
        self.directory.mkdir(parents=True)
        history = self.directory / 'history.sqlite3'
        with closing(sqlite3.connect(history, isolation_level=None)) as db:
            db.execute('CREATE TABLE messages(seq INTEGER PRIMARY KEY, body TEXT)')
            db.execute("INSERT INTO messages VALUES(1,'synthetic fixture')")
        for name in ['friend.json', 'state.json', 'persona_profile.json', 'style_profile.json']:
            (self.directory / name).write_text(json.dumps({'fixture': name}), encoding='utf-8')
        (self.root / 'friends.json').write_text('{"friends":{}}', encoding='utf-8')
        before = self.snapshot()
        store = self.store()
        store.write_batch(0, [self.artifact(state='COMPLETED')])
        store.close()
        after = self.snapshot()
        self.assertEqual({k: after[k] for k in before}, before)
        self.assertEqual(set(after) - set(before), {str(Path('friends') / '12345' / 'persona.sqlite3')})

    def test_connections_are_not_shared_across_threads(self):
        store = self.store()
        errors = []
        def read_shared():
            try:
                store.db.execute('SELECT 1')
            except sqlite3.ProgrammingError as exc:
                errors.append(exc)
        worker = threading.Thread(target=read_shared)
        worker.start()
        worker.join()
        self.assertEqual(len(errors), 1)


class RegistryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_two = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:2])
        version_two.start()
        self.addCleanup(version_two.stop)

    def open(self, readonly=False):
        return PersonaStore('12345', self.directory, readonly, project_root=self.root)

    def create_v1(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:1]):
            store = self.open()
            try:
                store.write_batch(0, [dict(friend_id='12345', id='profile-v1', state='STAGED',
                    manifest_relpath='persona/versions/profile-v1/manifest.json',
                    manifest_sha256='a' * 64, created_at='2026-10-04T12:00:00+09:00')])
            finally:
                store.close()

    def test_fresh_v2_has_registry_and_separate_scope_and_permissions(self):
        store = self.open()
        try:
            self.assertIsNone(store.backup_path)
            self.assertEqual([r[0] for r in store.db.execute(
                'SELECT version FROM schema_migrations ORDER BY version')], [1, 2])
            self.assertEqual({r[0] for r in store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")},
                {'schema_migrations', 'scope_state', 'artifact_versions', 'dataset_registry'})
            columns = {r[1] for r in store.db.execute('PRAGMA table_info(dataset_registry)')}
            self.assertTrue({'data_scope', 'usage_scope', 'license_code', 'license_data',
                             'license_image', 'model_analysis_allowed',
                             'raw_corpus_redistribution_allowed', 'image_redistribution_allowed'} <= columns)
            insert = ('INSERT INTO dataset_registry(friend_id,dataset_id,name,data_scope,usage_scope,'
                      'active,revision,created_at,updated_at,model_analysis_allowed) VALUES(?,?,?,?,?,?,?,?,?,?)')
            values = ('12345', 'sample', 'sample', 'PUBLIC_DATASET', 'research', 1, 1, 'now', 'now', None)
            store.db.execute(insert, values)
            self.assertIsNone(store.db.execute(
                'SELECT model_analysis_allowed FROM dataset_registry').fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(insert, ('23456',) + values[1:])
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(insert, values[:1] + ('invalid',) + values[2:-1] + (2,))
        finally:
            store.close()

    def test_v1_upgrade_preserves_catalog_and_readable_backup(self):
        self.create_v1()
        with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True)) as original:
            before_dump = tuple(original.iterdump())
        store = self.open()
        try:
            self.assertEqual(store.revision, 1)
            self.assertEqual(store.artifacts()[0]['id'], 'profile-v1')
            backup_path = store.backup_path
            self.assertTrue(backup_path.is_file())
            with closing(sqlite3.connect(backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
                self.assertEqual(backup.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 1)
                self.assertEqual(backup.execute('SELECT revision FROM scope_state').fetchone()[0], 1)
                self.assertEqual(backup.execute('SELECT id FROM artifact_versions').fetchone()[0], 'profile-v1')
                self.assertIsNone(backup.execute(
                    "SELECT name FROM sqlite_master WHERE name='dataset_registry'").fetchone())
                self.assertEqual(tuple(backup.iterdump()), before_dump)
        finally:
            store.close()
        unchanged = self.path.read_bytes()
        store = self.open()
        try:
            self.assertIsNone(store.backup_path)
        finally:
            store.close()
        self.assertEqual(unchanged, self.path.read_bytes())

    def test_readonly_v1_does_not_upgrade_or_create_backup(self):
        self.create_v1()
        before = self.path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            self.open(readonly=True)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.directory.glob('*.backup.sqlite3')), [])

    def test_failed_v2_ddl_rolls_back_to_exact_v1_database(self):
        self.create_v1()
        before = self.path.read_bytes()
        failed = storage.MIGRATIONS[:1] + ((2, storage.REGISTRY_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', failed):
            with self.assertRaises(sqlite3.OperationalError):
                self.open()
        self.assertEqual(before, self.path.read_bytes())
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:1]):
            store = self.open(readonly=True)
            try:
                self.assertEqual(store.revision, 1)
                self.assertIsNone(store.db.execute(
                    "SELECT name FROM sqlite_master WHERE name='dataset_registry'").fetchone())
            finally:
                store.close()
        self.assertEqual(len(list(self.directory.glob('*.backup.sqlite3'))), 1)


class ImportMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_three = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:3])
        version_three.start()
        self.addCleanup(version_three.stop)

    def open(self, readonly=False):
        store = PersonaStore('12345', self.directory, readonly, project_root=self.root)
        self.addCleanup(store.close)
        return store

    def registry(self, store, dataset='sample'):
        store.db.execute('INSERT INTO dataset_registry(friend_id,dataset_id,name,data_scope,'
                         'usage_scope,active,revision,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?)',
                         ('12345', dataset, dataset, 'PRIVATE_FRIEND', 'local', 1, 1, 'now', 'now'))

    def import_row(self, store, import_id='i1', dataset='sample', friend='12345'):
        store.db.execute('INSERT INTO dataset_imports(friend_id,import_id,dataset_id,data_scope,'
                         'source_hash,mapping_hash,settings_hash,status,created_at,updated_at) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)',
                         (friend, import_id, dataset, 'PRIVATE_FRIEND', 'a'*64, 'b'*64,
                          'c'*64, 'CREATED', 'now', 'now'))

    def message(self, store, import_id='i1', dataset='sample', record='r1', fingerprint='f1'):
        store.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
                         'message_seq,speaker_role,recorded_at,text,message_type,content_fingerprint) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('12345', import_id, dataset, record, 0, 'UNKNOWN', 'now', 'fixture', 'text', fingerprint))

    def test_fresh_v3_constraints_reject_wrong_dataset_and_duplicate_records(self):
        store = self.open()
        self.assertEqual([r[0] for r in store.db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')], [1, 2, 3])
        self.registry(store)
        self.registry(store, 'other')
        with self.assertRaises(sqlite3.IntegrityError):
            self.import_row(store, friend='67890')
        self.import_row(store)
        self.import_row(store, 'i2')
        with self.assertRaises(sqlite3.IntegrityError):
            self.message(store, dataset='other')
        self.message(store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.message(store, 'i2', fingerprint='f2')
        with self.assertRaises(sqlite3.IntegrityError):
            self.message(store, 'i2', record='r2')
        row = store.db.execute('SELECT event_time,source_origin,speaker_id FROM imported_messages').fetchone()
        self.assertEqual(tuple(row), (None, None, None))
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())

    def test_v2_upgrade_preserves_registry_and_backup_and_reopens_without_changes(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:2]):
            previous = self.open()
            self.registry(previous)
            previous.write_batch(0, [])
            before_dump = tuple(previous.db.iterdump())
            previous.close()
        upgraded = self.open()
        self.assertEqual(upgraded.revision, 1)
        self.assertEqual(upgraded.db.execute('SELECT dataset_id FROM dataset_registry').fetchone()[0], 'sample')
        with closing(sqlite3.connect(upgraded.backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before_dump)
        upgraded.close()
        before = self.path.read_bytes()
        reopened = self.open()
        self.assertIsNone(reopened.backup_path)
        reopened.close()
        self.assertEqual(before, self.path.read_bytes())

    def test_readonly_v2_refuses_upgrade_and_failed_v3_rolls_back(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:2]):
            previous = self.open()
            self.registry(previous)
            previous.close()
        before = self.path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            self.open(readonly=True)
        self.assertEqual(list(self.directory.glob('*.backup.sqlite3')), [])
        broken = storage.MIGRATIONS[:2] + ((3, storage.IMPORT_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(len(list(self.directory.glob('*.backup.sqlite3'))), 1)

    def test_message_and_checkpoint_rollback_in_same_transaction(self):
        store = self.open()
        self.registry(store)
        self.import_row(store)
        store.db.execute('BEGIN IMMEDIATE')
        try:
            self.message(store)
            store.db.execute("UPDATE dataset_imports SET checkpoint_json='fixture',status='RUNNING'")
            store.db.execute('UPDATE scope_state SET revision=revision+1')
            with self.assertRaises(sqlite3.IntegrityError):
                self.message(store, record='duplicate')
        finally:
            store.db.execute('ROLLBACK')
        self.assertEqual(store.db.execute('SELECT count(*) FROM imported_messages').fetchone()[0], 0)
        self.assertEqual(tuple(store.db.execute(
            'SELECT checkpoint_json,status FROM dataset_imports').fetchone()), (None, 'CREATED'))
        self.assertEqual(store.revision, 0)

    def test_media_only_message_keeps_type_and_reference_without_fake_text(self):
        store = self.open()
        self.registry(store)
        self.import_row(store)
        store.db.execute('INSERT INTO imported_messages(friend_id,import_id,dataset_id,record_id,'
                         'message_seq,speaker_role,recorded_at,text,message_type,media_id,content_fingerprint) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                         ('12345', 'i1', 'sample', 'media', 0, 'UNKNOWN', 'now', '', 'sticker', 'asset-1', 'media-1'))
        self.assertEqual(tuple(store.db.execute(
            'SELECT text,message_type,media_id FROM imported_messages').fetchone()), ('', 'sticker', 'asset-1'))


class DialogueMigrationTests(unittest.TestCase):
    open = ImportMigrationTests.open
    registry = ImportMigrationTests.registry
    import_row = ImportMigrationTests.import_row
    message = ImportMigrationTests.message

    def setUp(self):
        # Keep historical V3 tests fixed; these exercise the actual V4 migration.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_four = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:4])
        version_four.start()
        self.addCleanup(version_four.stop)

    def session(self, store, session='s1', friend='12345'):
        store.db.execute('INSERT INTO dialogue_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                         (friend, session, 'boundary', '["human"]', None, None,
                          1800, 'UNKNOWN', '["m1","m2"]'))

    def turn(self, store, turn='t1', session='s1', ordinal=0, previous=None,
             friend='12345'):
        store.db.execute('INSERT INTO dialogue_turns VALUES(?,?,?,?,?,?,?,?,?)',
                         (friend, turn, session, ordinal, 'human', None, 'UNKNOWN',
                          previous, '[{"text":"fixture","media_id":null}]'))

    def test_v3_upgrade_preserves_import_and_consistent_backup(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:3]):
            previous = self.open()
            self.registry(previous)
            self.import_row(previous)
            self.message(previous)
            previous.write_batch(0, [])
            before_dump = tuple(previous.db.iterdump())
            previous.close()
        upgraded = self.open()
        self.assertEqual([r[0] for r in upgraded.db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')], [1, 2, 3, 4])
        self.assertEqual(upgraded.revision, 1)
        self.assertEqual(upgraded.db.execute('SELECT text FROM imported_messages').fetchone()[0], 'fixture')
        with closing(sqlite3.connect(upgraded.backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before_dump)
        upgraded.close()
        before = self.path.read_bytes()
        reopened = self.open()
        self.assertIsNone(reopened.backup_path)
        reopened.close()
        self.assertEqual(before, self.path.read_bytes())

    def test_readonly_v3_and_failed_v4_do_not_modify_database(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:3]):
            previous = self.open()
            self.registry(previous)
            self.import_row(previous)
            self.message(previous)
            previous.close()
        before = self.path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            self.open(readonly=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('*.backup.sqlite3')), [])
        broken = storage.MIGRATIONS[:3] + ((4, storage.DIALOGUE_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(len(list(self.directory.glob('*.backup.sqlite3'))), 1)

    def test_cross_friend_and_cross_session_previous_turn_rejected(self):
        store = self.open()
        with self.assertRaises(sqlite3.IntegrityError):
            self.session(store, friend='67890')
        self.session(store)
        self.session(store, 's2')
        self.turn(store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.turn(store, 'wrong-friend', friend='67890')
        with self.assertRaises(sqlite3.IntegrityError):
            self.turn(store, 'wrong-session', session='s2', previous='t1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.turn(store, 'wrong-ordinal')
        self.turn(store, 't2', ordinal=1, previous='t1')
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())

    def test_complete_session_turn_and_revision_rollback_together(self):
        store = self.open()
        store.db.execute('BEGIN IMMEDIATE')
        try:
            self.session(store)
            self.turn(store)
            store.db.execute('UPDATE scope_state SET revision=revision+1')
            with self.assertRaises(sqlite3.IntegrityError):
                self.turn(store, 'bad', ordinal=1, previous='missing')
        finally:
            store.db.execute('ROLLBACK')
        for table in ('dialogue_sessions', 'dialogue_turns'):
            self.assertEqual(store.db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(store.revision, 0)

class PairMigrationTests(unittest.TestCase):
    open = ImportMigrationTests.open
    session = DialogueMigrationTests.session
    turn = DialogueMigrationTests.turn

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_five = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:5])
        version_five.start()
        self.addCleanup(version_five.stop)

    def pair(self, store, pair='p1', session='s1', input_turn='t1', target_turn='t2',
             friend='12345', gap=None):
        store.db.execute('INSERT INTO dialogue_pairs VALUES(?,?,?,?,?,?,?,?,?)',
                         (friend, pair, session, input_turn, target_turn, '[]', 'human', gap, 'UNKNOWN'))

    def create_v4(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:4]):
            previous = self.open()
            self.session(previous)
            self.turn(previous)
            self.turn(previous, 't2', ordinal=1, previous='t1')
            previous.write_batch(0, [])
            before_dump = tuple(previous.db.iterdump())
            previous.close()
            return before_dump

    def test_v4_upgrade_preserves_complete_session_and_backup(self):
        before_dump = self.create_v4()
        upgraded = self.open()
        self.assertEqual([r[0] for r in upgraded.db.execute(
            'SELECT version FROM schema_migrations ORDER BY version')], [1, 2, 3, 4, 5])
        self.assertEqual(upgraded.revision, 1)
        self.assertEqual(upgraded.db.execute('SELECT count(*) FROM dialogue_turns').fetchone()[0], 2)
        with closing(sqlite3.connect(upgraded.backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before_dump)
        upgraded.close()
        before = self.path.read_bytes()
        self.open().close()
        self.assertEqual(before, self.path.read_bytes())

    def test_readonly_v4_and_failed_v5_leave_previous_database_unchanged(self):
        self.create_v4()
        before = self.path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            self.open(readonly=True)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.directory.glob('*.backup.sqlite3')), [])
        broken = storage.MIGRATIONS[:4] + ((5, storage.PAIR_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        self.assertEqual(self.path.read_bytes(), before)

    def test_pair_references_same_friend_and_same_session_turns(self):
        store = self.open()
        self.session(store)
        self.session(store, 's2')
        self.turn(store)
        self.turn(store, 't2', ordinal=1, previous='t1')
        self.turn(store, 't3', session='s2')
        with self.assertRaises(sqlite3.IntegrityError):
            self.pair(store, friend='67890')
        with self.assertRaises(sqlite3.IntegrityError):
            self.pair(store, target_turn='t3')
        with self.assertRaises(sqlite3.IntegrityError):
            self.pair(store, input_turn='missing')
        with self.assertRaises(sqlite3.IntegrityError):
            self.pair(store, gap=-1)
        self.pair(store)
        with self.assertRaises(sqlite3.IntegrityError):
            self.pair(store, pair='duplicate-target')
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())

    def test_pair_batch_and_revision_failure_roll_back_together(self):
        store = self.open()
        self.session(store)
        self.turn(store)
        self.turn(store, 't2', ordinal=1, previous='t1')
        store.db.execute('BEGIN IMMEDIATE')
        try:
            self.pair(store)
            store.db.execute('UPDATE scope_state SET revision=revision+1')
            with self.assertRaises(sqlite3.IntegrityError):
                self.pair(store, pair='invalid', target_turn='missing')
        finally:
            store.db.execute('ROLLBACK')
        self.assertEqual(store.db.execute('SELECT count(*) FROM dialogue_pairs').fetchone()[0], 0)
        self.assertEqual(store.revision, 0)


class AnalysisMigrationTests(unittest.TestCase):
    open = ImportMigrationTests.open

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_six = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:6])
        version_six.start()
        self.addCleanup(version_six.stop)

    def create_v5(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:5]):
            previous = self.open()
            previous.write_batch(0, [])
            before = tuple(previous.db.iterdump())
            previous.close()
        return before

    def test_v5_upgrade_preserves_catalog_and_backup(self):
        before = self.create_v5()
        store = self.open()
        self.assertEqual(store.revision, 1)
        self.assertEqual(store.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 6)
        self.assertEqual({r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'persona_%'")},
            {'persona_analysis_runs', 'persona_claims'})
        with closing(sqlite3.connect(store.backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before)

    def test_readonly_old_schema_and_failed_upgrade_preserve_old_database(self):
        self.create_v5()
        before = self.path.read_bytes()
        with self.assertRaises(SchemaMismatch):
            self.open(readonly=True)
        self.assertEqual(self.path.read_bytes(), before)
        broken = storage.MIGRATIONS[:5] + ((6, storage.ANALYSIS_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        self.assertEqual(self.path.read_bytes(), before)

    def test_claim_requires_same_friend_run_and_valid_state(self):
        store = self.open()
        store.db.execute('INSERT INTO persona_analysis_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('12345', 'run', 'DRAFT', 'now', 0, 'a' * 64, '{}', '{}', '', ''))
        store.db.execute('INSERT INTO scope_state VALUES(?,0,NULL)', ('23456',))
        base = ('12345', 'claim', 'run', 'key', 'BOT_PERSONA', 'ReplyPattern', 'greeting', 'DRAFT', '{}')
        for row in ((('23456',) + base[1:]), base[:2] + ('missing',) + base[3:],
                    base[:7] + ('PUBLISHED',) + base[8:]):
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute('INSERT INTO persona_claims VALUES(?,?,?,?,?,?,?,?,?)', row)
        store.db.execute('INSERT INTO persona_claims VALUES(?,?,?,?,?,?,?,?,?)', base)
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute('INSERT INTO persona_claims VALUES(?,?,?,?,?,?,?,?,?)',
                             base[:1] + ('other',) + base[2:])


class EvidenceMigrationTests(unittest.TestCase):
    open = ImportMigrationTests.open

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'
        self.path = self.directory / 'persona.sqlite3'
        version_seven = patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:7])
        version_seven.start()
        self.addCleanup(version_seven.stop)

    def add_run(self, store, run='run'):
        store.db.execute('INSERT INTO persona_analysis_runs VALUES(?,?,?,?,?,?,?,?,?,?)',
                         ('12345', run, 'DRAFT', 'now', 0, 'a' * 64, '{}', '{}', '', ''))

    def test_v6_upgrade_preserves_run_and_backup_and_failed_upgrade_rolls_back(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:6]):
            previous = self.open()
            self.add_run(previous)
            before = tuple(previous.db.iterdump())
            previous.close()
        broken = storage.MIGRATIONS[:6] + ((7, storage.EVIDENCE_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        with closing(sqlite3.connect(self.path)) as old:
            self.assertEqual(tuple(old.iterdump()), before)
        store = self.open()
        self.assertEqual(store.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 7)
        self.assertEqual(store.db.execute('SELECT count(*) FROM persona_analysis_runs').fetchone()[0], 1)
        with closing(sqlite3.connect(store.backup_path.as_uri() + '?mode=ro', uri=True)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before)

    def test_evidence_requires_same_run_claim_and_distinguishes_counter_and_media(self):
        store = self.open()
        self.add_run(store)
        self.add_run(store, 'other-run')
        store.db.execute('INSERT INTO persona_claims VALUES(?,?,?,?,?,?,?,?,?)',
            ('12345', 'claim', 'run', 'key', 'BOT_PERSONA', 'ReplyPattern', 'greeting', 'DRAFT', '{}'))
        base = ('12345', 'run', 'claim', 'support', 'episode', 'message', 0, 2, 'Hello',
                'PUBLIC_DATASET', 'PUBLIC_DATASET', 'human', 'observed_structure')
        for row in (base[:1] + ('other-run',) + base[2:],
                    ('23456',) + base[1:], base[:3] + ('invalid',) + base[4:],
                    base[:6] + (None, 2, 'Hello') + base[9:]):
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute('INSERT INTO persona_claim_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
        store.db.execute('INSERT INTO persona_claim_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', base)
        store.db.execute('INSERT INTO persona_claim_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         base[:3] + ('counter',) + base[4:])
        store.db.execute('INSERT INTO persona_claim_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                         base[:5] + ('media', None, None, None) + base[9:])
        self.assertEqual(store.db.execute('SELECT count(*) FROM persona_claim_evidence').fetchone()[0], 3)
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())


if __name__ == '__main__':
    unittest.main()
