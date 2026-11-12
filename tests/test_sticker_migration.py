from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import persona_store as storage
from persona_types import ContextBudgets, RetrievalFilters, RetrievalRequest
from retrieval_orchestrator import RetrievalOrchestrator
from retrieval_router import RouterDecision
from tests import test_structured_profile as fixtures


HISTORICAL_HASHES = (
    '1d5f7b6e2fa44d786c132d52ab0ccb4ce3dc496a02c357a3b7565e3a899b420a',
    '65334a233f7a0314756162ffac070e96b4d392b4cda1a1b12e5fec7da3c0f721',
    'f98cdda591edb9d193e4a98039841bf2d65ab70231191472ba558d21c56d103e',
    'cedde6bceb4a6eba06ee416623219718a472a0164aac93057ac9b687ebf53a16',
    'ebf4fdfaeac039751b88289c554a75c439c3ccfd97bd1ebd2ec81965f97da8cd',
    '022f6d5066af54fd587811d0b44a8a8afc579b79093c03876bc2cd18174d1e26',
    '69267d404c429820970af93459aaa6e0f08d770cbc3083e187b994249cda6312',
    '30ff1e8b28a013b9c7eee03851a5dbb86c65a89a2215f85eee857e1768efb1d4',
    '7199efae718ba52312481fdc6580bf8fd945e96a676270ea2602fc6867b97ac2',
    'b973966bf95cc78dc93ff94d772a7628348c4fadbbaf5ae9068827ef616f3e15',
    '315d6acffaef01e183f89904576585454b9e0fb1d63f1d91b99ff29ecade6dcc',
    'e7d38025cfb2595720e2374cd04156075bc2c801e0ad536fd62ff734662d6b2d',
    '77b3b1bc5da3425d6d709c4abf35406f807f8b852df61bf730cbd4136e07d9f1',
    'e0719e6eba5c57096486c171f18d7f119fb0ad6f7aa18c9b210d747c7a6a9206',
)


class StickerMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'

    def open_store(self, readonly=False):
        store = storage.PersonaStore('12345', self.directory, readonly, project_root=self.root)
        self.addCleanup(store.close)
        return store

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def historical(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:14]):
            old = self.open_store()
            old.db.execute("UPDATE scope_state SET revision=7")
            old.db.execute("INSERT INTO embedding_index_metadata VALUES('12345','g','ACTIVE','model',3,'v1',0,'hash',1,'{}','now','now')")
            old.db.execute("INSERT INTO knowledge_entities VALUES('12345','jay','周杰伦','Person','[]','[]','hash',.8,1,1,'now','now')")
            before = tuple(old.db.iterdump())
            old.close()
        return before

    def test_v14_upgrade_backup_rollback_and_historical_hashes(self):
        self.assertEqual(tuple(storage.migration_hash(s) for _, s in storage.MIGRATIONS[:14]), HISTORICAL_HASHES)
        self.assertEqual([v for v, _ in storage.MIGRATIONS], list(range(1, len(storage.MIGRATIONS) + 1)))
        before = self.historical()
        path = self.directory / 'persona.sqlite3'
        raw = path.read_bytes()
        broken = storage.MIGRATIONS[:14] + ((15, storage.STICKER_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open_store()
        self.assertEqual(path.read_bytes(), raw)
        new = self.open_store()
        with closing(sqlite3.connect(new.backup_path)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before)
        self.assertEqual(new.revision, 7)
        altered = {line for line in before if line.startswith('CREATE TABLE analysis_jobs')}  # v17 adds a column
        self.assertTrue(set(before) - altered <= set(new.db.iterdump()))
        self.assertEqual(new.db.execute('SELECT name FROM knowledge_entities').fetchone()[0], '周杰伦')
        self.assertEqual(new.db.execute('SELECT state FROM embedding_index_metadata').fetchone()[0], 'ACTIVE')
        tables = {r[0] for r in new.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'sticker_assets', 'sticker_metadata'} <= tables)
        self.assertIn('sticker_examples', tables)
        self.assertEqual([r[2] for r in new.db.execute("PRAGMA index_info('sticker_metadata_kind')")],
                         ['friend_id', 'sticker_kind', 'analysis_status'])
        for version, digest in new.db.execute('SELECT version,migration_hash FROM schema_migrations WHERE version<15 ORDER BY version'):
            self.assertEqual(digest, HISTORICAL_HASHES[version - 1])

    def test_readonly_v14_and_exception_types_never_write(self):
        self.historical()
        before = self.snapshot()
        with self.assertRaises(storage.SchemaMismatch) as caught:
            self.open_store(True)
        self.assertIsInstance(caught.exception, storage.Unavailable)
        self.assertIsInstance(caught.exception, RuntimeError)
        self.assertNotIsInstance(storage.ScopeMismatch('other'), storage.Unavailable)
        self.assertEqual(before, self.snapshot())

    def test_bad_hash_and_incomplete_schema_never_write(self):
        for fault in ('hash', 'incomplete'):
            with self.subTest(fault=fault):
                store = self.open_store()
                if fault == 'hash':
                    store.db.execute("UPDATE schema_migrations SET migration_hash='bad' WHERE version=14")
                else:
                    store.db.execute('DROP TABLE artifact_versions')
                store.close()
                before = self.snapshot()
                for readonly in (True, False):
                    with self.assertRaises(storage.SchemaMismatch):
                        self.open_store(readonly)
                    self.assertEqual(before, self.snapshot())
                # Repair only the isolated fixture to exercise the other corruption case.
                with closing(sqlite3.connect(self.directory / 'persona.sqlite3')) as db:
                    if fault == 'hash':
                        db.execute('UPDATE schema_migrations SET migration_hash=? WHERE version=14', (HISTORICAL_HASHES[13],))
                        db.commit()

    def test_assets_and_metadata_constraints(self):
        store = self.open_store()
        asset = ('12345', 'a' * 64, 'media/a.png', 'PRIVATE_FRIEND', None, None,
                 'UNKNOWN', None, None, None, None, None, 1, 'now')
        sql = 'INSERT INTO sticker_assets VALUES(' + ','.join('?' for _ in asset) + ')'
        for index, value in ((0, '23456'), (1, 'A'*64), (1, 'g'*64), (1, 'a'*63),
                             (2, ''), (3, 'fake'), (4, 'fake'), (6, 'PHOTO'),
                             (9, 2), (10, -1), (11, 2), (12, 2)):
            bad = list(asset); bad[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(sql, bad)
        store.db.execute(sql, asset)
        store.db.execute("INSERT INTO scope_state VALUES('23456',0,NULL)")
        other = list(asset); other[0] = '23456'; other[1] = 'b'*64
        other[2] = 'media/b.png'
        store.db.execute(sql, other)
        for index, value in ((1, 'b'*64), (2, 'media/b.png')):
            duplicate = list(asset); duplicate[index] = value
            with self.assertRaises(sqlite3.IntegrityError): store.db.execute(sql, duplicate)
        self.assertEqual(tuple(store.db.execute("SELECT license_image,local_use_allowed,model_analysis_allowed,image_redistribution_allowed FROM sticker_assets WHERE friend_id='12345'").fetchone()), (None,)*4)
        metadata = ('12345', 'a'*64, 'UNKNOWN', 'UNKNOWN', None, None, None, None,
                    None, None, 'unavailable', 'v1', 'LOCAL_UNAVAILABLE', 'now')
        sql = 'INSERT INTO sticker_metadata VALUES(' + ','.join('?' for _ in metadata) + ')'
        for index, value in ((0, '23456'), (1, 'b'*64), (2, 'FAILED'), (3, 'PHOTO'),
                             (7, -0.1), (7, 1.1), (10, ''), (11, ''), (12, None), (12, 'secret exception')):
            bad = list(metadata); bad[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(sqlite3.IntegrityError):
                store.db.execute(sql, bad)
        bad = list(metadata); bad[2] = 'COMPLETED'
        with self.assertRaises(sqlite3.IntegrityError): store.db.execute(sql, bad)
        store.db.execute(sql, metadata)
        with self.assertRaises(sqlite3.IntegrityError): store.db.execute(sql, metadata)
        store.db.execute('DELETE FROM sticker_metadata')
        completed = list(metadata); completed[2] = 'COMPLETED'; completed[3] = 'NON_STICKER'
        completed[8] = ''; completed[12] = None
        store.db.execute(sql, completed)
        self.assertEqual(store.db.execute('SELECT text_content FROM sticker_metadata').fetchone()[0], '')
        self.assertIsNone(store.db.execute('PRAGMA foreign_key_check').fetchone())


class StickerReadonlyIntegrationTests(unittest.TestCase):
    setUp = fixtures.StructuredProfileTests.setUp

    def old_sidecar(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:14]):
            store = storage.PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
            store.close()
        pointer = self.scope.friend_dir / 'persona_versions' / 'active.json'
        pointer.parent.mkdir()
        pointer.write_text('{}', 'utf-8')

    def orchestrator(self, lane):
        orchestrator = RetrievalOrchestrator(self.scope)
        # Only route selection is fixed; every retriever and connection is real.
        orchestrator.router = SimpleNamespace(route=lambda request: RouterDecision(
            ['structured', lane], request.scene, [], [], (None, None), 'default', 'initial'))
        return orchestrator

    def request(self):
        return RetrievalRequest(friend_id='12345', query='', recent_context='', scene='any',
            plan=None, filters=RetrievalFilters(), budgets=ContextBudgets(),
            allowed_pack_versions=frozenset(), now='2026-10-06T00:00:00+00:00')

    def snapshot(self):
        return {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_real_old_v14_lanes_degrade_and_keep_structured(self):
        self.old_sidecar()
        before = self.snapshot()
        for lane in ('persona', 'entity', 'interest', 'topic'):
            with self.subTest(lane=lane):
                result = self.orchestrator(lane).retrieve(self.request())
                self.assertTrue(result.degraded)
                self.assertTrue(result.structured)
                trace = json.dumps(result.trace)
                self.assertIn(lane, trace)
                self.assertNotIn('migration version or fixed SQL hash differs', trace)
                self.assertEqual(before, self.snapshot())

    def test_real_scope_mismatch_is_not_degraded(self):
        self.old_sidecar()
        with closing(sqlite3.connect(self.scope.friend_dir / 'persona.sqlite3')) as db:
            db.execute("UPDATE scope_state SET friend_id='23456'"); db.commit()
        before = self.snapshot()
        for lane in ('persona', 'entity', 'interest', 'topic'):
            with self.subTest(lane=lane), self.assertRaises(storage.ScopeMismatch):
                self.orchestrator(lane).retrieve(self.request())
            self.assertEqual(before, self.snapshot())


if __name__ == '__main__': unittest.main()
