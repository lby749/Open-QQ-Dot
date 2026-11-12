import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from dataset_registry import DatasetRecord, DatasetRegistry
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import DataScope, FriendScope, PrivacyView, SourceOrigin, SourceRef


class DatasetRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        registry = {'friends': {}}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': friend_id}), encoding='utf-8')
            registry['friends'][friend_id] = {'qq_id': friend_id, 'directory': str(directory)}
        (self.root / 'friends.json').write_text(json.dumps(registry), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.other_scope = FriendScope.load('23456', project_root=self.root)

    def open_registry(self, scope=None, readonly=False):
        value = DatasetRegistry(scope or self.scope, readonly=readonly)
        self.addCleanup(value.close)
        return value

    def make_record(self, **changes):
        values = dict(dataset_id='corpus-a', name='Corpus A', data_scope='PUBLIC_DATASET',
                      usage_scope='research', license_data='CC-BY-4.0',
                      model_analysis_allowed=True)
        values.update(changes)
        return DatasetRecord(**values)

    def test_registration_permissions_and_public_provenance_are_independent(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        registry = self.open_registry()
        record = registry.register(self.make_record(), expected_revision=0)
        self.assertEqual(record.data_scope, 'PUBLIC_DATASET')
        self.assertEqual(record.friend_id, '12345')  # registry owner, not source provenance
        permissions = registry.get_permissions('corpus-a')
        self.assertTrue(permissions.cloud_model_analysis_allowed)
        self.assertFalse(permissions.raw_corpus_redistribution_effective)
        self.assertTrue(permissions.local_research_allowed)

    def test_unknown_permissions_are_local_research_only_and_cloud_denied(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        registry = self.open_registry()
        registry.register(self.make_record(license_data=None, model_analysis_allowed=None), 0)
        permissions = registry.get_permissions('corpus-a')
        self.assertIsNone(permissions.model_analysis_allowed)
        self.assertTrue(permissions.local_research_allowed)
        self.assertFalse(permissions.cloud_model_analysis_allowed)

    def test_each_license_and_permission_is_independent_and_public_source_has_no_friend(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        registry = self.open_registry()
        registry.register(self.make_record(license_code='MIT', license_data='CC-BY-4.0',
            license_image=None, local_use_allowed=True, derived_stats_allowed=False,
            model_analysis_allowed=True, raw_corpus_redistribution_allowed=False,
            image_redistribution_allowed=None), 0)
        item = registry.get('corpus-a')
        self.assertEqual((item.license_code, item.license_data, item.license_image),
                         ('MIT', 'CC-BY-4.0', None))
        self.assertEqual((item.local_use_allowed, item.derived_stats_allowed,
                          item.model_analysis_allowed, item.raw_corpus_redistribution_allowed,
                          item.image_redistribution_allowed), (True, False, True, False, None))
        public_source = SourceRef(dataset_id='corpus-a', import_id='import-1', record_id='row-1',
            message_seq=None, speaker_id='speaker-a', origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL,
            friend_id=None, event_time=None, recorded_at='2026-10-04T10:00:00+09:00')
        self.assertIsNone(public_source.friend_id)

    def test_private_data_never_becomes_public_or_cloud_eligible(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        registry = self.open_registry()
        registry.register(self.make_record(data_scope='PRIVATE_FRIEND',
                                           model_analysis_allowed=True,
                                           license_data='CC-BY-4.0'), 0)
        permissions = registry.get_permissions('corpus-a')
        self.assertEqual(permissions.data_scope, 'PRIVATE_FRIEND')
        self.assertFalse(permissions.cloud_model_analysis_allowed)
        with self.assertRaises(ScopeMismatch):
            registry.update(self.make_record(), expected_revision=1)

    def test_cas_and_owner_scope_prevent_stale_and_cross_friend_access(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        registry = self.open_registry()
        registry.register(self.make_record(), 0)
        with self.assertRaises(Conflict):
            registry.register(self.make_record(dataset_id='other'), 0)
        with self.assertRaises(ScopeMismatch):
            registry.register(self.make_record(friend_id='23456', dataset_id='cross'), 1)
        second_sidecar = self.other_scope.friend_dir / 'persona.sqlite3'
        second_sidecar.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.scope.friend_dir / 'persona.sqlite3', second_sidecar)
        with self.assertRaises(ScopeMismatch):
            self.open_registry(self.other_scope)
        self.assertEqual([item.dataset_id for item in registry.list()], ['corpus-a'])

    def test_update_is_persisted_and_stale_second_connection_cannot_overwrite(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        first = self.open_registry()
        first.register(self.make_record(), expected_revision=0)
        second = self.open_registry()
        updated = first.update(replace(first.get('corpus-a'), name='Renamed'),
                               expected_revision=1)
        self.assertEqual(updated.revision, 2)
        self.assertEqual(first.revision, 2)
        self.assertEqual(second.get('corpus-a').name, 'Renamed')
        self.assertEqual(second.get('corpus-a').revision, 2)
        with self.assertRaises(Conflict):
            second.register(self.make_record(dataset_id='stale'), expected_revision=1)
        self.assertIsNone(first.get('stale'))
        self.assertEqual(first.revision, 2)

    def test_list_is_bounded_and_deactivation_is_a_tombstone(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        history = self.scope.friend_dir / 'history.sqlite3'
        with closing(sqlite3.connect(history)) as db:  # `with connect()` alone commits but leaves the file open
            db.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('INSERT INTO metadata VALUES("qq_id","12345")')
            db.commit()
        before_history = history.read_bytes()
        store.close()
        registry = self.open_registry()
        registry.register(self.make_record(), 0)
        with self.assertRaises(ValueError):
            registry.list(501)
        registry.deactivate('corpus-a', expected_revision=1)
        self.assertEqual(registry.list(), ())
        permissions = registry.get_permissions('corpus-a')
        self.assertFalse(permissions.cloud_model_analysis_allowed)
        self.assertFalse(permissions.local_research_allowed)
        self.assertEqual(registry.list(include_inactive=True)[0].active, False)
        self.assertEqual(history.read_bytes(), before_history)

    def test_readonly_missing_sidecar_does_not_create_it(self):
        before = list(self.root.rglob('*'))
        with self.assertRaises(Unavailable):
            self.open_registry(readonly=True)
        self.assertEqual(before, list(self.root.rglob('*')))

    def test_readonly_existing_v2_reads_without_mutation_and_rejects_all_writes(self):
        store = PersonaStore('12345', self.scope.friend_dir, project_root=self.root)
        store.close()
        writer = self.open_registry()
        stored = writer.register(self.make_record(), expected_revision=0)
        writer.close()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        readonly = self.open_registry(readonly=True)
        self.assertEqual([item.dataset_id for item in readonly.list()], ['corpus-a'])
        self.assertEqual(readonly.get('corpus-a').name, 'Corpus A')
        with self.assertRaises(Unavailable):
            readonly.register(self.make_record(dataset_id='new'), expected_revision=1)
        with self.assertRaises(Unavailable):
            readonly.update(replace(stored, name='Changed'), expected_revision=1)
        with self.assertRaises(Unavailable):
            readonly.deactivate('corpus-a', expected_revision=1)
        readonly.close()
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
