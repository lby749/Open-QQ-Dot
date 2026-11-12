from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import persona_store as storage


class TopicMigrationTests(unittest.TestCase):
    @patch.object(storage, "MIGRATIONS", storage.MIGRATIONS[:12])
    def test_v11_backup_rollback_and_topic_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp).resolve() / 'friends' / '12345'
            def open_store(readonly=False):
                return storage.PersonaStore('12345', directory, readonly, project_root=Path(temp))
            with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:11]):
                old = open_store()
                old.db.execute("INSERT INTO interest_entries VALUES('12345','i','music','likes music','music',.8,.9,'stable','REAL_CHAT','[]',NULL,NULL,NULL,0,1,1,'now','now')")
                before = tuple(old.db.iterdump())
                old.close()
            path = directory / 'persona.sqlite3'
            before_bytes = path.read_bytes()
            with self.assertRaises(storage.SchemaMismatch):
                open_store(True)
            broken = storage.MIGRATIONS[:11] + ((12, storage.TOPIC_STATEMENTS + ('INVALID DDL',)),)
            with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
                open_store()
            self.assertEqual(path.read_bytes(), before_bytes)
            new = open_store()
            try:
                with closing(sqlite3.connect(new.backup_path)) as backup:
                    self.assertEqual(tuple(backup.iterdump()), before)
                self.assertEqual(new.db.execute('SELECT count(*) FROM interest_entries').fetchone()[0], 1)
                row = ('12345','topic','AI','','model','text','summary','[]','PUBLIC_RESEARCH',
                       'https://example.com','[]','hash','now','now',None,None,.7,.8,
                       'FAST_CHANGING',None,None,None,1,1)
                sql = 'INSERT INTO topic_knowledge VALUES(' + ','.join('?' for _ in row) + ')'
                for index,value in ((0,'23456'),(8,'fake'),(16,2),(17,-1),(18,'forever'),(22,2),(23,0)):
                    bad = list(row); bad[index] = value
                    with self.assertRaises(sqlite3.IntegrityError):
                        new.db.execute(sql,bad)
                new.db.execute(sql,row)
                self.assertIsNone(new.db.execute('PRAGMA foreign_key_check').fetchone())
            finally:
                new.close()


if __name__ == '__main__':
    unittest.main()
