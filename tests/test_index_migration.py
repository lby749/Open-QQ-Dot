from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import persona_store as storage


class IndexMigrationTests(unittest.TestCase):
    @patch.object(storage, "MIGRATIONS", storage.MIGRATIONS[:13])
    def test_v12_backup_rollback_fts_sync_and_active_scope_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp).resolve()/'friends'/'12345'
            def open_store(readonly=False):
                return storage.PersonaStore('12345',directory,readonly,project_root=Path(temp))
            with patch.object(storage,'MIGRATIONS',storage.MIGRATIONS[:12]):
                old=open_store(); before=tuple(old.db.iterdump()); old.close()
            path=directory/'persona.sqlite3'; before_bytes=path.read_bytes()
            with self.assertRaises(storage.SchemaMismatch):open_store(True)
            broken=storage.MIGRATIONS[:12]+((13,storage.INDEX_STATEMENTS+('INVALID DDL',)),)
            with patch.object(storage,'MIGRATIONS',broken),self.assertRaises(sqlite3.OperationalError):open_store()
            self.assertEqual(path.read_bytes(),before_bytes)
            new=open_store()
            try:
                with closing(sqlite3.connect(new.backup_path)) as backup:
                    self.assertEqual(tuple(backup.iterdump()),before)
                meta=('12345','g','ACTIVE','model',3,'v1',0,'hash',1,'{}','now','now')
                new.db.execute('INSERT INTO embedding_index_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',meta)
                for index,value in ((0,'23456'),(2,'unknown'),(4,0),(6,-1),(8,-1)):
                    bad=list(meta);bad[1]='bad';bad[index]=value
                    with self.assertRaises(sqlite3.IntegrityError):
                        new.db.execute('INSERT INTO embedding_index_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',bad)
                bad=list(meta);bad[1]='duplicate'
                with self.assertRaises(sqlite3.IntegrityError):
                    new.db.execute('INSERT INTO embedding_index_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',bad)
                row=(1,'12345','g','memory','m1','memory','REAL_CHAT','music','music',
                     '周杰 杰伦 周 杰 伦','text_hash','source_hash','{}','[]','PRIVATE_FRIEND',
                     None,None,None,None,None,'music',.8,1,None)
                sql='INSERT INTO semantic_documents VALUES('+','.join('?' for _ in row)+')'
                bad=list(row);bad[1]='23456'
                with self.assertRaises(sqlite3.IntegrityError):new.db.execute(sql,bad)
                new.db.execute(sql,row)
                def hits(query):
                    return [r[0] for r in new.db.execute('SELECT rowid FROM semantic_documents_fts WHERE semantic_documents_fts MATCH ?',(query,))]
                self.assertEqual(hits('周杰'),[1]);self.assertEqual(hits('杰伦'),[1])
                new.db.execute('UPDATE semantic_documents SET search_text=? WHERE rowid=1',('学校 学 校',))
                self.assertEqual(hits('周杰'),[]);self.assertEqual(hits('学校'),[1])
                new.db.execute('DELETE FROM semantic_documents WHERE rowid=1')
                self.assertEqual(hits('学校'),[])
                self.assertIsNone(new.db.execute('PRAGMA foreign_key_check').fetchone())
            finally:new.close()


if __name__=='__main__':unittest.main()
