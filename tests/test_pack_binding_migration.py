from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage


class PackBindingMigrationTests(unittest.TestCase):
    @patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:10])
    def test_v9_backup_readonly_failure_and_binding_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = root / 'friends' / '12345'
            def open_store():
                return storage.PersonaStore('12345', directory, project_root=root)
            with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:9]):
                old = open_store()
                old.db.execute('UPDATE scope_state SET revision=8')
                before = tuple(old.db.iterdump())
                old.close()
            path = directory / 'persona.sqlite3'
            before_bytes = path.read_bytes()
            with self.assertRaises(storage.SchemaMismatch):
                storage.PersonaStore('12345', directory, readonly=True, project_root=root)
            broken = storage.MIGRATIONS[:9] + ((10, storage.PACK_BINDING_STATEMENTS + ('INVALID DDL',)),)
            with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
                open_store()
            self.assertEqual(path.read_bytes(), before_bytes)
            store = open_store()
            self.addCleanup(store.close)
            self.assertEqual(store.revision, 8)
            with closing(sqlite3.connect(store.backup_path)) as backup:
                self.assertEqual(tuple(backup.iterdump()), before)
            self.assertEqual(store.db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 10)
            row = ('12345', 'public-pack', 'v1', 1, 1.0, 20.0, 1, 'now', 'now')
            sql = 'INSERT INTO pack_bindings VALUES(?,?,?,?,?,?,?,?,?)'
            for index, value in ((0, '23456'), (3, 2), (4, 1.1), (5, 0), (6, 0)):
                bad = list(row)
                bad[index] = value
                with self.assertRaises(sqlite3.IntegrityError):
                    store.db.execute(sql, bad)
            store.db.execute(sql, row)
            with self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(sql, row)
            self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())
            store.close()


if __name__ == '__main__':
    unittest.main()
