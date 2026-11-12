from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage


class CurationMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'

    def open(self, **kwargs):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:8]):
            store = storage.PersonaStore('12345', self.directory, project_root=self.root, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_upgrade_preserves_catalog_backup_and_failure_rolls_back(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:7]):
            old = self.open()
            old.db.execute("UPDATE scope_state SET revision=4")
            before = tuple(old.db.iterdump())
            old.close()
        with self.assertRaises(storage.SchemaMismatch):
            self.open(readonly=True)
        broken = storage.MIGRATIONS[:7] + ((8, storage.CURATION_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open()
        path = self.directory / 'persona.sqlite3'
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(tuple(db.iterdump()), before)
        new = self.open()
        self.assertEqual(new.revision, 4)
        self.assertEqual(new.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 8)
        with closing(sqlite3.connect(new.backup_path)) as db:
            self.assertEqual(tuple(db.iterdump()), before)

    def test_curation_scope_status_and_revision_constraints(self):
        store = self.open()
        row = ('12345', 'run', 'RUNNING', 0, '{}', '{}', 'model', 'v1', 'hash',
               '{}', '{"known":false}', None, 'now', None)
        insert = 'INSERT INTO dataset_curation_runs VALUES(' + ','.join('?' for _ in row) + ')'
        for bad in [('23456',) + row[1:], row[:2] + ('INVALID',) + row[3:],
                    row[:3] + (-1,) + row[4:]]:
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(insert, bad)
        store.db.execute(insert, row)
        with self.assertRaises(sqlite3.IntegrityError):
            store.db.execute(insert, row)
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())


if __name__ == '__main__':
    unittest.main()
