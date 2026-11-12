import ast
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import persona_snapshot as module
from persona_snapshot import PreviewSnapshot, ReadOnlyLegacyReader
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import FriendScope, InvalidSource


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as db:
        with db:
            yield db


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        registry = {'friends': {}}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            profile = {'qq_id': friend_id, 'display_name': 'same nickname',
                       'style': 'keep this persona', 'auto_reply': True,
                       'bridge_enabled': True,
                       'daily_research_enabled': True,
                       'model_api': {'provider': 'codex', 'codex_model': 'keep-model'},
                       'proactive': {'enabled': True, 'time_aware': True, 'rounds_max': 10},
                       'scheduled_care': {'enabled': True, 'slots': {'morning': {'count': 2}}}}
            (directory / 'friend.json').write_text(json.dumps(profile), encoding='utf-8')
            registry['friends'][friend_id] = {'qq_id': friend_id, 'directory': str(directory),
                                             'self_id': '34567'}
            with database(directory / 'history.sqlite3') as db:
                db.executescript('''CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,
                                          body TEXT,created_at TEXT,handled INTEGER);
                    CREATE TABLE memories(seq INTEGER PRIMARY KEY,body TEXT,updated_at TEXT);''')
                db.executemany('INSERT INTO metadata VALUES(?,?)', (
                    ('qq_id', friend_id), ('thread_id', 'production-thread'),
                    ('inbound_generation', '7'), ('last_seen_id', 'message-2')))
                db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', (
                    (1, 'message-1', 'friend', friend_id + ':hello', '2026-10-04', 0),
                    (2, 'message-2', 'me', 'bot context', '2026-10-04', 1)))
                db.execute('INSERT INTO memories VALUES(1,?,?)', ('kept memory', '2026-10-04'))
            (directory / 'deepseek-key.bin').write_bytes(b'not-a-real-key')
        (self.root / 'friends.json').write_text(json.dumps(registry), encoding='utf-8')
        (self.root / 'napcat.local.json').write_text('{}', encoding='utf-8')

    def scope(self):
        return FriendScope.load('12345', project_root=self.root)

    def production(self):
        paths = [self.root / 'friends.json', self.root / 'napcat.local.json']
        paths += [p for p in (self.root / 'friends').rglob('*') if p.is_file()]
        return {str(p.relative_to(self.root)): p.read_bytes() for p in paths}

    def test_reader_has_no_source_writes_and_bounded_paging(self):
        before = self.production()
        with ReadOnlyLegacyReader(self.scope()) as reader:
            self.assertEqual(reader.get('inbound_generation'), '7')
            first = reader.page('messages', limit=1)
            second = reader.page('messages', after_rowid=first[0]['snapshot_rowid'], limit=1)
            self.assertEqual((first[0]['seq'], second[0]['seq']), (1, 2))
            self.assertEqual(reader.recent_messages(1)[0]['direction'], 'me')
            self.assertEqual(reader.memory(), 'kept memory')
            with self.assertRaises(sqlite3.OperationalError):
                reader._db.execute("UPDATE metadata SET value='evil'")
        self.assertEqual(before, self.production())
        self.assertFalse((self.root / 'build').exists())

    def test_snapshot_is_isolated_and_preserves_data(self):
        before = self.production()
        snapshot = PreviewSnapshot.create(self.scope())
        self.assertEqual(snapshot.source_friend_id, '12345')
        self.assertTrue(snapshot.scope.project_root.is_relative_to(self.root / 'build' / 'persona-previews'))
        self.assertEqual(before, self.production())
        self.assertEqual(sorted(p.name for p in (snapshot.scope.project_root / 'friends').iterdir()), ['12345'])
        with ReadOnlyLegacyReader(snapshot.scope) as reader:
            self.assertIsNone(reader.get('thread_id'))
            self.assertEqual(reader.memory(), 'kept memory')
            self.assertEqual(reader.get('inbound_generation'), '7')
            self.assertEqual(len(reader.page('messages')), 2)
        with database(snapshot.scope.friend_dir / 'history.sqlite3') as db:
            db.execute("UPDATE memories SET body='preview-only'")
        self.assertEqual(before, self.production())
        profile = json.loads((snapshot.scope.friend_dir / 'friend.json').read_text(encoding='utf-8'))
        self.assertEqual(profile['style'], 'keep this persona')
        self.assertEqual(profile['model_api']['codex_model'], 'keep-model')
        self.assertEqual(profile['proactive']['rounds_max'], 10)
        self.assertEqual(profile['scheduled_care']['slots']['morning']['count'], 2)
        for key in ('auto_reply', 'bridge_enabled', 'daily_research_enabled',
                    'persona_enabled', 'hybrid_enabled', 'sticker_metadata_enabled'):
            self.assertIs(profile[key], False)
        self.assertFalse(profile['proactive']['enabled'])
        self.assertFalse(profile['proactive']['time_aware'])
        self.assertFalse(profile['scheduled_care']['enabled'])
        self.assertTrue(all(v is False for v in profile['feature_flags'].values()))
        registry = json.loads((snapshot.scope.project_root / 'friends.json').read_text(encoding='utf-8'))
        self.assertEqual(registry['friends']['12345']['self_id'], '')
        self.assertFalse((snapshot.scope.friend_dir / 'deepseek-key.bin').exists())
        self.assertFalse((snapshot.scope.project_root / 'napcat.local.json').exists())
        self.assertEqual(json.loads((snapshot.scope.project_root / 'preview.json').read_text())['state'], 'COMPLETED')

    def test_snapshot_unique_and_no_send_or_model_entry_point(self):
        first = PreviewSnapshot.create(self.scope())
        second = PreviewSnapshot.create(self.scope())
        self.assertNotEqual(first.scope.project_root, second.scope.project_root)
        tree = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
        imports = {item.module for item in ast.walk(tree) if isinstance(item, ast.ImportFrom)}
        imports |= {alias.name for item in ast.walk(tree) if isinstance(item, ast.Import) for alias in item.names}
        self.assertTrue(imports.isdisjoint({'main', 'store', 'napcat_ws', 'qq_link', 'app_server', 'api_backend'}))
        self.assertFalse(hasattr(first, 'send'))

    def test_verified_preview_scope_works_with_isolated_catalog(self):
        before = self.production()
        snapshot = PreviewSnapshot.create(self.scope())
        scope = snapshot.scope
        with self.assertRaises(Unavailable):
            PersonaStore(scope.friend_id, scope.friend_dir, readonly=True,
                         project_root=scope.project_root)
        self.assertFalse((scope.friend_dir / 'persona.sqlite3').exists())
        store = PersonaStore(scope.friend_id, scope.friend_dir, project_root=scope.project_root)
        try:
            self.assertEqual(store.revision, 0)
            self.assertEqual(store.artifacts(), [])
        finally:
            store.close()
        self.assertEqual(before, self.production())
        self.assertFalse((self.scope().friend_dir / 'persona.sqlite3').exists())

    def test_reader_missing_database_creates_nothing(self):
        (self.scope().friend_dir / 'history.sqlite3').unlink()
        before = self.production()
        with self.assertRaises(Unavailable):
            ReadOnlyLegacyReader(self.scope())
        self.assertEqual(before, self.production())

    def test_lazy_missing_table_unavailable_without_init(self):
        before = self.production()
        with ReadOnlyLegacyReader(self.scope()) as reader:
            with self.assertRaises(Unavailable):
                reader.page('memory_entries')
            with self.assertRaises(InvalidSource):
                reader.page('messages; DELETE FROM metadata')
            with self.assertRaises(InvalidSource):
                reader.page([])
            for limit in (0, 501, True, -1):
                with self.assertRaises(InvalidSource):
                    reader.page('messages', limit=limit)
            with self.assertRaises(InvalidSource):
                reader.page('messages', after_rowid=-1)
        self.assertEqual(before, self.production())

    def test_wrong_database_identity_rejected_before_preview(self):
        with database(self.scope().friend_dir / 'history.sqlite3') as db:
            db.execute("UPDATE metadata SET value='23456' WHERE key='qq_id'")
        before = self.production()
        with self.assertRaises(ScopeMismatch):
            PreviewSnapshot.create(self.scope())
        self.assertEqual(before, self.production())
        self.assertFalse((self.root / 'build').exists())

    def test_scope_changed_after_binding_rejected(self):
        scope = self.scope()
        path = scope.friend_dir / 'friend.json'
        profile = json.loads(path.read_text())
        profile['qq_id'] = '23456'
        path.write_text(json.dumps(profile))
        before = self.production()
        with self.assertRaises(ScopeMismatch):
            ReadOnlyLegacyReader(scope)
        self.assertEqual(before, self.production())

    def test_path_traversal_and_other_friend_directory_rejected(self):
        for identifier in ('../12345', '12345/23456', '１２３４５'):
            with self.assertRaises(ScopeMismatch):
                FriendScope.load(identifier, project_root=self.root)
        with self.assertRaises(ScopeMismatch):
            FriendScope.load('12345', project_root=self.root,
                             friend_dir=self.root / 'friends' / '23456')
        with self.assertRaises(ScopeMismatch):
            ReadOnlyLegacyReader(str(self.scope().friend_dir))

    def test_history_hardlink_rejected(self):
        scope = self.scope()
        path = scope.friend_dir / 'history.sqlite3'
        path.unlink()
        os.link(self.root / 'friends' / '23456' / 'history.sqlite3', path)
        before = self.production()
        with self.assertRaises(ScopeMismatch):
            ReadOnlyLegacyReader(scope)
        self.assertEqual(before, self.production())

    def test_wal_rejected_without_shm_creation_or_source_writes(self):
        scope = self.scope()
        with database(scope.friend_dir / 'history.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
        before = self.production()
        with self.assertRaises(Unavailable):
            ReadOnlyLegacyReader(scope)
        self.assertEqual(before, self.production())

    def test_journal_side_file_rejected_before_sqlite_open(self):
        scope = self.scope()
        for suffix in ('-wal', '-shm', '-journal'):
            path = Path(str(scope.friend_dir / 'history.sqlite3') + suffix)
            path.write_bytes(b'pending')
            before = self.production()
            with self.assertRaises(Unavailable):
                ReadOnlyLegacyReader(scope)
            self.assertEqual(before, self.production())
            path.unlink()

    def test_backup_failure_returns_no_completed_snapshot(self):
        before = self.production()
        with patch.object(module.time, 'monotonic', side_effect=(0, 20)):
            with self.assertRaises(TimeoutError):
                PreviewSnapshot.create(self.scope(), timeout_seconds=1)
        self.assertEqual(before, self.production())
        self.assertEqual(list((self.root / 'build').rglob('preview.json')), [])

    def test_actual_busy_backup_obeys_short_timeout_without_source_writes(self):
        scope = self.scope()
        before = self.production()
        reader = ReadOnlyLegacyReader(scope)
        with closing(sqlite3.connect(scope.friend_dir / 'history.sqlite3')) as writer:
            writer.execute('BEGIN EXCLUSIVE')
            try:
                with patch.object(module, 'ReadOnlyLegacyReader', return_value=reader):
                    started = time.monotonic()
                    with self.assertRaises(TimeoutError):
                        PreviewSnapshot.create(scope, timeout_seconds=0.2)
                    self.assertLess(time.monotonic() - started, 1.2)
            finally:
                writer.rollback()
                reader.close()
        self.assertEqual(before, self.production())
        self.assertEqual(list((self.root / 'build').rglob('preview.json')), [])

    def test_profile_change_during_backup_returns_conflict(self):
        scope = self.scope()
        path = scope.friend_dir / 'friend.json'
        real = module._identity_bytes
        count = 0

        def changing(value):
            nonlocal count
            count += 1
            if count == 2:
                profile = json.loads(path.read_text())
                profile['style'] = 'new operator style'
                path.write_text(json.dumps(profile))
            return real(value)

        with patch.object(module, '_identity_bytes', side_effect=changing):
            with self.assertRaises(Conflict):
                PreviewSnapshot.create(scope)
        self.assertEqual(list((self.root / 'build').rglob('preview.json')), [])
        self.assertEqual(json.loads(path.read_text())['style'], 'new operator style')

    def test_invalid_timeout_before_destination_creation(self):
        for value in (0, -1, 61, True, float('nan'), float('inf')):
            with self.assertRaises(InvalidSource):
                PreviewSnapshot.create(self.scope(), timeout_seconds=value)
        self.assertFalse((self.root / 'build').exists())


if __name__ == '__main__':
    unittest.main()
