import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from persona_incremental import IncrementalUpdate, reconcile, revoke_import
from persona_store import Conflict, PersonaStore, ScopeMismatch
from source_state import snapshot
from store import FriendStore, dump_json


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        from tests.test_persona_types import PersonaTypesTests
        fixture = PersonaTypesTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.scope = fixture.scope()
        self.history = FriendStore(self.scope.friend_dir)
        self.addCleanup(self.history.close)
        self.history.initialize(self.scope.friend_id, None)
        self.persona = PersonaStore(self.scope.friend_id, self.scope.friend_dir,
                                    project_root=self.scope.project_root)
        self.addCleanup(self.persona.close)
        self.update = IncrementalUpdate(self.scope, self.history)

    def message(self, identifier='one', direction='friend'):
        with self.history.db:
            self.history.db.execute('INSERT INTO messages(qq_id,direction,body,created_at,handled) '
                'VALUES(?,?,?,?,?)', (identifier, direction, 'private body '+identifier,
                                      '2026-10-07T12:00:00+09:00', direction == 'me'))

    def test_commit_cursor_and_receipt_are_atomic_idempotent_and_body_free(self):
        self.message()
        batch = self.update.prepare()
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM incremental_cursors').fetchone()[0], 0)
        with patch.object(self.update, 'prepare', side_effect=RuntimeError('commit interrupted')):
            with self.assertRaises(RuntimeError):
                self.update.commit(batch)
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM incremental_cursors').fetchone()[0], 0)
        receipt = self.update.commit(batch)
        self.assertEqual(receipt, self.update.commit(batch))
        rebuilt = IncrementalUpdate(self.scope, self.history)
        self.assertIsNone(rebuilt.process())
        self.assertEqual(receipt['count'], 1)
        self.assertNotIn('private body', json.dumps(dict(self.history.db.execute('SELECT * FROM incremental_projections').fetchone())))
        self.message('two', 'me')
        self.assertEqual(rebuilt.process()['from_cursor'], 1)
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM incremental_projections').fetchone()[0], 2)

    def test_stale_batch_and_tampered_projection_never_advance_cursor(self):
        self.message()
        batch = self.update.prepare()
        with self.assertRaises(Conflict):
            self.update.commit(replace(batch, projections=({**batch.projections[0], 'source_hash': 'bad'},)))
        self.message('two')
        with self.assertRaises(Conflict):
            self.update.commit(batch)
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM incremental_receipts').fetchone()[0], 0)

    def test_clear_reset_and_revoke_have_different_semantics(self):
        from tests.test_dataset_normalizer import DatasetNormalizerTests
        imports = DatasetNormalizerTests()
        imports.setUp()
        self.addCleanup(imports.doCleanups)
        raw = imports.add_message()
        before_raw = dict(raw)
        self.assertTrue(revoke_import(imports.scope, 'imp-1'))
        token = snapshot(imports.scope)
        self.assertFalse(revoke_import(imports.scope, 'imp-1'))
        self.assertEqual(token, snapshot(imports.scope))
        with self.assertRaises(Conflict):
            list(imports.normalizer.normalize([raw]))
        self.assertEqual(before_raw, dict(imports.registry.db.execute('SELECT * FROM imported_messages').fetchone()))
        self.message()
        self.history.set_memory('durable manual fact', locked=True)
        profile = (self.scope.friend_dir / 'friend.json').read_bytes()
        self.assertEqual(self.history.clear_messages(), 1)
        self.assertEqual(self.history.memory(), 'durable manual fact')
        self.assertEqual(profile, (self.scope.friend_dir / 'friend.json').read_bytes())
        self.message('new')
        self.history.reset_memory()
        self.assertEqual(self.history.message_count(), 1)
        self.assertEqual(self.history.memory(), '')
        self.assertEqual(self.history.db.execute('SELECT count(*) FROM memory_versions').fetchone()[0], 0)
        self.assertEqual(int(self.history.get('activity_memory_reset_seq')), 2)

    def test_clear_epoch_cursor_is_independent_and_old_batch_is_rejected(self):
        self.message()
        batch = self.update.prepare()
        self.update.commit(batch)
        self.history.clear_messages()
        self.message('new')
        with self.assertRaises(Conflict):
            self.update.commit(batch)
        receipt = self.update.process()
        self.assertEqual((receipt['source_epoch'], receipt['from_cursor'], receipt['count']), (1, 0, 1))

    def test_pending_operation_rejects_reads_and_reconciles_once(self):
        self.message()
        with patch('persona_incremental._invalidate_sidecar', side_effect=RuntimeError('power loss')):
            with self.assertRaises(RuntimeError):
                self.history.clear_messages()
        with self.assertRaises(Conflict):
            snapshot(self.scope)
        with self.assertRaises(Conflict):
            self.history.recent_messages()
        epoch = self.history.db.execute('SELECT clear_epoch FROM source_state').fetchone()[0]
        reconcile(self.history)
        reconcile(self.history)
        self.assertEqual(epoch, self.history.db.execute('SELECT clear_epoch FROM source_state').fetchone()[0])
        self.assertEqual(self.history.message_count(), 0)
        self.assertIsNotNone(snapshot(self.scope))

    def test_database_restore_cannot_reuse_old_token(self):
        self.message()
        before = snapshot(self.scope)
        memory_backup = sqlite3.connect(':memory:')
        self.addCleanup(memory_backup.close)
        self.history.db.backup(memory_backup)
        self.history.set_memory('new memory')
        memory_backup.backup(self.history.db)
        self.assertNotEqual(before, snapshot(self.scope))

    def test_controlled_profile_aba_changes_revision(self):
        path = self.scope.friend_dir / 'friend.json'
        original = json.loads(path.read_text('utf-8'))
        before = snapshot(self.scope)
        dump_json(path, {**original, 'basic_info': {'nickname': 'new'}})
        dump_json(path, original)
        self.assertNotEqual(before, snapshot(self.scope))

    def test_precise_import_scope_and_public_pack_independence(self):
        from tests.test_dataset_normalizer import DatasetNormalizerTests
        imports = DatasetNormalizerTests()
        imports.setUp()
        self.addCleanup(imports.doCleanups)
        private = imports.add_message()
        public = imports.add_message(dataset_id='public', import_id='public-imp', record_id='public',
                                     origin='PUBLIC_DATASET')
        pack = imports.scope.project_root / 'data' / 'persona_packs' / 'immutable.json'
        pack.parent.mkdir(parents=True)
        pack.write_text('immutable pack fixture', 'utf-8')
        before = pack.read_bytes()
        self.assertTrue(revoke_import(imports.scope, 'public-imp'))
        self.assertEqual(before, pack.read_bytes())
        with self.assertRaises(Conflict):
            list(imports.normalizer.normalize([public]))
        self.assertEqual(len(list(imports.normalizer.normalize([private]))), 1)
        with self.assertRaises(ScopeMismatch):
            revoke_import(self.scope, 'public-imp')
