from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage


class JobMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'

    def open(self, **kwargs):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:9]):
            store = storage.PersonaStore('12345', self.directory, project_root=self.root, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_upgrade_backup_readonly_and_failed_ddl(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:8]):
            old = self.open()
            old.db.execute('UPDATE scope_state SET revision=7')
            before = tuple(old.db.iterdump())
            old.close()
        with self.assertRaises(storage.SchemaMismatch):
            self.open(readonly=True)
        broken = storage.MIGRATIONS[:8] + ((9, storage.JOB_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        with closing(sqlite3.connect(self.directory / 'persona.sqlite3')) as db:
            self.assertEqual(tuple(db.iterdump()), before)
        new = self.open()
        self.assertEqual(new.revision, 7)
        self.assertEqual(new.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 9)
        with closing(sqlite3.connect(new.backup_path)) as db:
            self.assertEqual(tuple(db.iterdump()), before)

    def test_job_owner_state_unique_and_checkpoint_constraints(self):
        store = self.open()
        row = ('12345', 'job', 'distill', 'friend:12345', 'hash', '{}', '[]',
               'settings', 'local', 'v1', 0, 'queued', None, None, None, '{}', '{}',
               0, 0, None, 'now', 'now')
        insert = 'INSERT INTO analysis_jobs VALUES(' + ','.join('?' for _ in row) + ')'
        for index, value in ((0, '23456'), (10, -1), (11, 'invalid'), (17, -1), (18, 2)):
            bad = list(row)
            bad[index] = value
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(insert, bad)
        store.db.execute(insert, row)
        duplicate = list(row)
        duplicate[1] = 'another-job'
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute(insert, duplicate)
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())


if __name__ == '__main__':
    unittest.main()
