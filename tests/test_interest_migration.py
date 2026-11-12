from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage


class InterestMigrationTests(unittest.TestCase):
    @patch.object(storage, "MIGRATIONS", storage.MIGRATIONS[:11])
    def test_v10_preserved_with_backup_and_interest_scope_source_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            directory = root / 'friends' / '12345'
            def open_store(readonly=False):
                return storage.PersonaStore('12345', directory, readonly, project_root=root)
            with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:10]):
                old = open_store()
                old.db.execute("INSERT INTO pack_bindings VALUES('12345','pack','v1',1,1,20,1,'now','now')")
                before = tuple(old.db.iterdump())
                old.close()
            path = directory / 'persona.sqlite3'
            before_bytes = path.read_bytes()
            with self.assertRaises(storage.SchemaMismatch):
                open_store(readonly=True)
            broken = storage.MIGRATIONS[:10] + ((11, storage.INTEREST_STATEMENTS + ('INVALID DDL',)),)
            with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
                open_store()
            self.assertEqual(path.read_bytes(), before_bytes)
            new = open_store()
            try:
                with closing(sqlite3.connect(new.backup_path)) as backup:
                    self.assertEqual(tuple(backup.iterdump()), before)
                self.assertEqual(new.db.execute('SELECT count(*) FROM pack_bindings').fetchone()[0], 1)
                row = ('12345', 'interest', '周杰伦', '喜欢周杰伦的歌', 'music', .8, .9,
                       'stable', 'REAL_CHAT', '[]', None, None, None, 0, 1, 1, 'now', 'now')
                sql = 'INSERT INTO interest_entries VALUES(' + ','.join('?' for _ in row) + ')'
                for index, value in ((0, '23456'), (5, 2), (7, 'fake'), (8, 'PUBLIC_RESEARCH'), (13, 2), (15, 0)):
                    bad = list(row)
                    bad[index] = value
                    with self.assertRaises(sqlite3.IntegrityError):
                        new.db.execute(sql, bad)
                new.db.execute(sql, row)
                self.assertIsNone(new.db.execute('PRAGMA foreign_key_check').fetchone())
            finally:
                new.close()


if __name__ == '__main__':
    unittest.main()
