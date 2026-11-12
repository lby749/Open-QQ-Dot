from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import persona_store as storage


class EntityMigrationTests(unittest.TestCase):
    def test_v13_backup_rollback_and_relation_member_constraints(self):
        with tempfile.TemporaryDirectory() as temp:
            directory=Path(temp).resolve()/'friends'/'12345'
            def open_store(readonly=False):
                return storage.PersonaStore('12345',directory,readonly,project_root=Path(temp))
            with patch.object(storage,'MIGRATIONS',storage.MIGRATIONS[:13]):
                old=open_store()
                old.db.execute("INSERT INTO embedding_index_metadata VALUES('12345','g','ACTIVE','model',3,'v1',0,'hash',1,'{}','now','now')")
                before=tuple(old.db.iterdump());old.close()
            path=directory/'persona.sqlite3';before_bytes=path.read_bytes()
            with self.assertRaises(storage.SchemaMismatch):open_store(True)
            broken=storage.MIGRATIONS[:13]+((14,storage.ENTITY_STATEMENTS+('INVALID DDL',)),)
            with patch.object(storage,'MIGRATIONS',broken),self.assertRaises(sqlite3.OperationalError):open_store()
            self.assertEqual(path.read_bytes(),before_bytes)
            new=open_store()
            try:
                with closing(sqlite3.connect(new.backup_path)) as backup:
                    self.assertEqual(tuple(backup.iterdump()),before)
                self.assertEqual(new.db.execute('SELECT state FROM embedding_index_metadata').fetchone()[0],'ACTIVE')
                entity=('12345','jay','周杰伦','Person','["周董"]','[]','hash',.8,1,1,'now','now')
                sql='INSERT INTO knowledge_entities VALUES('+','.join('?' for _ in entity)+')'
                for index,value in ((0,'23456'),(3,'fake'),(7,2),(8,2),(9,0)):
                    bad=list(entity);bad[index]=value
                    with self.assertRaises(sqlite3.IntegrityError):new.db.execute(sql,bad)
                new.db.execute(sql,entity)
                relation=('12345','r','jay','RELATED_TO','jay',.8,.5,None,None,None,None,'[]','hash',1,1,'now','now')
                sql='INSERT INTO knowledge_relations VALUES('+','.join('?' for _ in relation)+')'
                for index,value in ((0,'23456'),(2,'missing'),(4,'missing'),(5,2),(6,-1),(13,2),(14,0)):
                    bad=list(relation);bad[index]=value
                    with self.assertRaises(sqlite3.IntegrityError):new.db.execute(sql,bad)
                new.db.execute(sql,relation)
                self.assertIsNone(new.db.execute('PRAGMA foreign_key_check').fetchone())
            finally:new.close()


if __name__=='__main__':unittest.main()
