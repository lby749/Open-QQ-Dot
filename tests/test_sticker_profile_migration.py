from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage
from tests.test_sticker_migration import HISTORICAL_HASHES


V15_HASHES = HISTORICAL_HASHES + (
    '003e3e6f4a1cf940e182463540b0abd2db2fb59f4e0d978fcb28036be6bcc1cf',
)


class StickerProfileMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'friends' / '12345'

    def open_store(self, readonly=False):
        store = storage.PersonaStore('12345', self.directory, readonly,
                                     project_root=self.root)
        self.addCleanup(store.close)
        return store

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    @staticmethod
    def seed(db, friend_id, suffix, asset_id):
        session = 'session-' + suffix
        input_turn, target_turn = 'input-' + suffix, 'target-' + suffix
        db.execute('INSERT INTO dialogue_sessions VALUES(?,?,?,?,?,?,?,?,?)',
                   (friend_id, session, '{}', '[]', None, None, 1800, 'UNKNOWN', '[]'))
        db.execute('INSERT INTO dialogue_turns VALUES(?,?,?,?,?,?,?,?,?)',
                   (friend_id, input_turn, session, 0, 'input-speaker', None,
                    'UNKNOWN', None, '[]'))
        db.execute('INSERT INTO dialogue_turns VALUES(?,?,?,?,?,?,?,?,?)',
                   (friend_id, target_turn, session, 1, 'target-speaker', None,
                    'UNKNOWN', input_turn, '[]'))
        db.execute('INSERT INTO dialogue_pairs VALUES(?,?,?,?,?,?,?,?,?)',
                   (friend_id, 'pair-' + suffix, session, input_turn, target_turn,
                    '[]', 'target-speaker', None, 'UNKNOWN'))
        db.execute('INSERT INTO sticker_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (friend_id, asset_id, suffix + '.png', 'PRIVATE_FRIEND', None, None,
                    'UNKNOWN', None, None, None, None, None, 1, 'now'))

    def historical(self):
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:15]):
            old = self.open_store()
            self.seed(old.db, '12345', 'a', 'a' * 64)
            old.db.execute('INSERT INTO sticker_metadata VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ('12345', 'a' * 64, 'UNKNOWN', 'UNKNOWN', None, None, None,
                            None, None, None, 'none', 'none', 'LOCAL_UNAVAILABLE', 'now'))
            old.db.execute("UPDATE scope_state SET revision=9,pending_publication_id='pending'")
            old.db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)',
                           ('12345', 'artifact', 'STAGED', 'versions/artifact/manifest.json',
                            'c' * 64, 'now'))
            before = tuple(old.db.iterdump())
            old.close()
        return before

    def test_v15_upgrade_preserves_data_backup_and_fixed_hashes(self):
        self.assertEqual(tuple(storage.migration_hash(s) for _, s in storage.MIGRATIONS[:15]),
                         V15_HASHES)
        self.assertEqual([v for v, _ in storage.MIGRATIONS], list(range(1, len(storage.MIGRATIONS) + 1)))
        before = self.historical()
        self.assertFalse(any('sticker_examples' in row for row in before))
        history = self.directory / 'history.sqlite3'
        config = self.directory / 'friend.json'
        # a real database: persona writes first check it for pending source operations (source_state, v17)
        with closing(sqlite3.connect(history)) as db:
            db.execute('CREATE TABLE fixture(value TEXT)')
            db.execute("INSERT INTO fixture VALUES('untouched historical source fixture')")
            db.commit()
        history_bytes = history.read_bytes()
        config.write_bytes(b'{"qq_id":"12345"}')
        store = self.open_store()
        with closing(sqlite3.connect(store.backup_path)) as backup:
            self.assertEqual(tuple(backup.iterdump()), before)
            self.assertEqual(backup.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        altered = {line for line in before if line.startswith('CREATE TABLE analysis_jobs')}  # v17 adds a column
        self.assertTrue(set(before) - altered <= set(store.db.iterdump()))
        self.assertEqual(store.revision, 9)
        self.assertEqual(store.db.execute('SELECT pending_publication_id FROM scope_state').fetchone()[0],
                         'pending')
        self.assertEqual(history.read_bytes(), history_bytes)
        self.assertEqual(config.read_bytes(), b'{"qq_id":"12345"}')
        self.assertEqual(tuple(r[0] for r in store.db.execute(
            'SELECT migration_hash FROM schema_migrations WHERE version<=15 ORDER BY version')),
            V15_HASHES)
        self.assertEqual([r[1] for r in store.db.execute('PRAGMA table_info(sticker_examples)')],
                         ['friend_id', 'subject', 'episode_id', 'session_id', 'turn_id',
                          'message_id', 'asset_id', 'pair_id', 'source_content_hash', 'created_at'])
        self.assertEqual([r[2] for r in store.db.execute(
            "PRAGMA index_info('sticker_examples_asset')")], ['friend_id', 'asset_id'])
        store.close()
        before_replay = self.snapshot()
        replay = self.open_store()
        self.assertIsNone(replay.backup_path)
        replay.close()
        self.assertEqual(before_replay, self.snapshot())

    def test_readonly_v15_refuses_upgrade_and_failed_ddl_rolls_back(self):
        self.historical()
        before = self.snapshot()
        with self.assertRaises(storage.SchemaMismatch) as caught:
            self.open_store(True)
        self.assertIsInstance(caught.exception, storage.Unavailable)
        self.assertEqual(before, self.snapshot())
        path = self.directory / 'persona.sqlite3'
        broken = storage.MIGRATIONS[:15] + (
            (16, storage.STICKER_EXAMPLE_STATEMENTS + ('INVALID DDL',)),)
        with patch.object(storage, 'MIGRATIONS', broken), self.assertRaises(sqlite3.OperationalError):
            self.open_store()
        self.assertEqual(path.read_bytes(), before[str(path.relative_to(self.root))])
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0], 15)
            self.assertIsNone(db.execute(
                "SELECT name FROM sqlite_master WHERE name='sticker_examples'").fetchone())

    def test_occurrence_constraints_and_same_owner_foreign_keys(self):
        store = self.open_store()
        db = store.db
        self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.seed(db, '12345', 'a', 'a' * 64)
        self.seed(db, '12345', 'c', 'c' * 64)
        db.execute("INSERT INTO scope_state VALUES('23456',0,NULL)")
        self.seed(db, '23456', 'b', 'b' * 64)
        row = ('12345', 'BOT_PERSONA', 'pair-a', 'session-a', 'target-a',
               'message-a', 'a' * 64, 'pair-a', 'd' * 64, 'now')
        sql = 'INSERT INTO sticker_examples VALUES(?,?,?,?,?,?,?,?,?,?)'
        invalid = ((0, '23456'), (1, 'DYAD'), (1, 'FRIEND'), (2, 'pair-c'),
                   (3, 'session-b'), (3, 'session-c'), (4, 'target-b'),
                   (4, 'target-c'), (6, 'b' * 64), (6, 'e' * 64),
                   (7, None), (8, 'D' * 64), (8, 'g' * 64),
                   (8, 'd' * 63), (8, 'd' * 65))
        for index, value in invalid:
            bad = list(row)
            bad[index] = value
            with self.subTest(index=index, value=value), self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, bad)
        for index in range(len(row)):
            bad = list(row)
            bad[index] = None
            with self.subTest(null=index), self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, bad)
        for pair_id in ('pair-b', 'missing-pair'):
            bad = list(row)
            bad[2] = bad[7] = pair_id
            with self.subTest(pair=pair_id), self.assertRaises(sqlite3.IntegrityError):
                db.execute(sql, bad)
        db.execute(sql, row)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute(sql, row)
        inbound = list(row)
        inbound[1], inbound[2], inbound[4], inbound[5], inbound[7] = (
            'FRIEND', 'event-a', 'input-a', 'message-inbound', None)
        db.execute(sql, inbound)
        self.assertEqual(db.execute('SELECT count(*) FROM sticker_examples').fetchone()[0], 2)
        self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())


if __name__ == '__main__':
    unittest.main()
