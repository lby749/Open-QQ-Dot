import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from unittest.mock import Mock, patch

from persona_feedback_gui import (PersonaFeedbackService, _can_start_comparison,
                                  _freeze_comparison, comparison_preview)
from persona_feedback_store import PersonaFeedbackStore
import persona_store as storage
from persona_store import Conflict, PersonaStore, SchemaMismatch, Unavailable
from persona_types import FriendScope


class PersonaFeedbackGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.friend_id = '12345'
        self.friend_dir = self.root / 'friends' / self.friend_id
        self.friend_dir.mkdir(parents=True)
        (self.friend_dir / 'friend.json').write_text(
            json.dumps({'qq_id': self.friend_id}), encoding='utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {
            self.friend_id: {'qq_id': self.friend_id,
                             'directory': str(self.friend_dir)}}}), encoding='utf-8')
        self.scope = FriendScope.load(self.friend_id, project_root=self.root)
        store = PersonaFeedbackStore(self.scope, readonly=False)
        store.close()
        self.service = PersonaFeedbackService(self.scope)

    def comparison(self, kind='offline_reply'):
        return _freeze_comparison(self.friend_id, 'legacy', 'a' * 32, kind,
            'PRIVATE A CANDIDATE', 'PRIVATE B CANDIDATE')

    def test_feedback_is_scoped_and_persists_only_subjective_metadata(self):
        self.assertEqual(self.service.list_versions(), ())
        comparison = self.comparison()
        fields = {key: comparison[key] for key in
            ('comparison_id', 'a_version', 'b_version', 'comparison_kind')}
        row = self.service.save_feedback(**fields, choice='B')
        retry = self.service.save_feedback(**fields, choice='B')
        self.assertEqual(retry, row)
        with self.assertRaises(Conflict):
            self.service.save_feedback(**fields, choice='A')
        self.assertEqual(row['friend_id'], self.friend_id)
        self.assertEqual(row['source_type'], 'HUMAN_SUBJECTIVE')
        self.assertEqual((row['a_version'], row['b_version'], row['choice']),
                         ('legacy', 'a' * 32, 'B'))
        self.assertNotIn('a_candidate', row)
        self.assertNotIn('b_candidate', row)
        self.assertNotIn('PRIVATE A CANDIDATE', repr(row))
        self.assertEqual(self.service.list_recent(), (row,))

    def test_feedback_choices_are_four_way(self):
        for choice in ('A', 'B', 'similar', 'neither'):
            comparison = self.comparison('context_only')
            self.service.save_feedback(
                **{key: comparison[key] for key in
                   ('comparison_id', 'a_version', 'b_version', 'comparison_kind')},
                choice=choice)
        self.assertEqual({row['choice'] for row in self.service.list_recent()},
                         {'A', 'B', 'similar', 'neither'})

    def test_candidates_must_be_nonempty_and_comparison_freezes_display_content(self):
        self.assertFalse(_can_start_comparison('legacy', 'v1', '', 'candidate'))
        self.assertFalse(_can_start_comparison('legacy', 'v1', 'candidate', '  '))
        self.assertFalse(_can_start_comparison('v1', 'v1', 'candidate', 'candidate'))
        self.assertTrue(_can_start_comparison('legacy', 'v1', 'A context', 'B context'))
        for a, b in (('', 'candidate'), ('  ', 'candidate'), ('candidate', '\t')):
            with self.subTest(a=a, b=b), self.assertRaises(ValueError):
                _freeze_comparison(self.friend_id, 'legacy', 'v1', 'offline_reply', a, b)
        comparison = self.comparison('context_only')
        preview = comparison_preview(comparison)
        self.assertIn('PRIVATE A CANDIDATE', preview)
        self.assertIn('PRIVATE B CANDIDATE', preview)
        self.assertIn('\u4e24\u4efd\u5b9e\u9645\u4e0a\u4e0b\u6587', preview)
        self.assertIn('\u6765\u6e90\u672a\u6838\u9a8c', preview)
        again = self.comparison('context_only')
        self.assertNotEqual(comparison['comparison_id'], again['comparison_id'])
        self.assertEqual(comparison['a_candidate'], 'PRIVATE A CANDIDATE')

    def test_version_list_uses_artifacts_and_keeps_latest_completed_order(self):
        catalog = Mock()
        catalog.artifacts.return_value = [
            {'id': 'old', 'state': 'COMPLETED'},
            {'id': 'middle', 'state': 'COMPLETED'},
            {'id': 'staged', 'state': 'STAGED'},
            {'id': 'new', 'state': 'COMPLETED'},
        ]
        with patch('persona_store.PersonaStore', return_value=catalog):
            rows = self.service.list_versions(limit=2)
        self.assertEqual(tuple(row['id'] for row in rows), ('new', 'middle'))
        catalog.artifacts.assert_called_once_with()
        catalog.close.assert_called_once_with()

    def test_v17_is_read_only_until_explicit_feedback_store_initialization(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            friend_id = '54321'
            friend_dir = root / 'friends' / friend_id
            friend_dir.mkdir(parents=True)
            (friend_dir / 'friend.json').write_text(
                json.dumps({'qq_id': friend_id}), encoding='utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {
                friend_id: {'qq_id': friend_id, 'directory': str(friend_dir)}}}), encoding='utf-8')
            scope = FriendScope.load(friend_id, project_root=root)
            with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:17]):
                old = PersonaStore(friend_id, friend_dir, project_root=root)
                old.db.execute('UPDATE scope_state SET revision=7')
                old.db.execute('INSERT INTO artifact_versions VALUES(?,?,?,?,?,?)',
                    (friend_id, 'existing', 'COMPLETED', 'versions/existing/manifest.json',
                     'a' * 64, '2026-10-08T00:00:00+00:00'))
                old.close()

            service = PersonaFeedbackService(scope)
            path = friend_dir / 'persona.sqlite3'
            before = {item.relative_to(root): item.read_bytes() for item in root.rglob('*')
                      if item.is_file()}
            with self.assertRaises(SchemaMismatch):
                service.list_versions()
            with self.assertRaises(SchemaMismatch):
                service.list_recent()
            self.assertEqual({item.relative_to(root): item.read_bytes() for item in root.rglob('*')
                              if item.is_file()}, before)

            service.initialize_feedback_store()
            self.assertEqual(tuple(row['id'] for row in service.list_versions()), ('existing',))
            self.assertEqual(service.list_recent(), ())
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 18)
                self.assertEqual(db.execute('SELECT revision FROM scope_state').fetchone()[0], 7)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM persona_feedback').fetchone()[0], 0)
            backups = tuple(friend_dir.glob('*.backup.sqlite3'))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0], 17)
                self.assertEqual(backup.execute('SELECT id FROM artifact_versions').fetchone()[0], 'existing')
                self.assertIsNone(backup.execute(
                    "SELECT name FROM sqlite_master WHERE name='persona_feedback'").fetchone())

    def test_explicit_initialization_does_not_repair_bad_schema_hash(self):
        path = self.scope.friend_dir / 'persona.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("UPDATE schema_migrations SET migration_hash='bad' WHERE version=18")
        before = {item.relative_to(self.root): item.read_bytes() for item in self.root.rglob('*')
                  if item.is_file()}
        with self.assertRaises(SchemaMismatch):
            self.service.initialize_feedback_store()
        self.assertEqual({item.relative_to(self.root): item.read_bytes() for item in self.root.rglob('*')
                          if item.is_file()}, before)
        self.assertEqual(tuple(self.scope.friend_dir.glob('*.backup.sqlite3')), ())

    def test_explicit_initialization_constructs_and_closes_store_under_friend_lock(self):
        import source_state

        events = []
        active = [False]
        fail_close = [False]

        @contextmanager
        def lock(directory):
            self.assertEqual(directory, self.scope.friend_dir)
            self.assertFalse(active[0])
            active[0] = True
            events.append('lock_enter')
            try:
                yield
            finally:
                active[0] = False
                events.append('lock_exit')

        class FakeFeedbackStore:
            def __init__(self, scope, *, readonly):
                self.assert_scope(scope, readonly)
                events.append('construct')

            @staticmethod
            def assert_scope(scope, readonly):
                self_outer.assertEqual(scope, self_outer.scope)
                self_outer.assertFalse(readonly)
                self_outer.assertTrue(active[0])

            def close(self):
                self_outer.assertTrue(active[0])
                events.append('close')
                if fail_close[0]:
                    raise RuntimeError('close failed')

        self_outer = self
        with patch.object(source_state, 'source_lock', lock), \
                patch('persona_feedback_store.PersonaFeedbackStore', FakeFeedbackStore):
            self.service.initialize_feedback_store()
        self.assertEqual(events, ['lock_enter', 'construct', 'close', 'lock_exit'])
        events.clear()
        fail_close[0] = True
        with patch.object(source_state, 'source_lock', lock), \
                patch('persona_feedback_store.PersonaFeedbackStore', FakeFeedbackStore), \
                self.assertRaisesRegex(RuntimeError, 'close failed'):
            self.service.initialize_feedback_store()
        self.assertEqual(events, ['lock_enter', 'construct', 'close', 'lock_exit'])

    def test_same_friend_id_in_another_project_reads_its_own_store(self):
        other_root = self.root / 'other'
        other_dir = other_root / 'friends' / self.friend_id
        other_dir.mkdir(parents=True)
        (other_dir / 'friend.json').write_text(
            json.dumps({'qq_id': self.friend_id}), encoding='utf-8')
        (other_root / 'friends.json').write_text(json.dumps({'friends': {
            self.friend_id: {'qq_id': self.friend_id,
                             'directory': str(other_dir)}}}), encoding='utf-8')
        other_scope = FriendScope.load(self.friend_id, project_root=other_root)
        other_service = PersonaFeedbackService(other_scope)
        with self.assertRaises(Unavailable):
            other_service.list_recent()
        self.assertFalse((other_dir / 'persona.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
