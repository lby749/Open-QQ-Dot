from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from persona_pack import _empty_sticker_profile, _manifest
from persona_pack_bindings import PackBindingStore
from persona_types import (DataScope, FriendScope, InvalidSource, PrivacyViolation,
                           PrivacyView, SourceOrigin, SourceRef)
from persona_versions import EXAMPLE_COLUMNS


class PackMediaFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.friend = self.root / 'friends' / '12345'
        self.friend.mkdir(parents=True)
        self.write(self.friend / 'friend.json', {'qq_id': '12345'})
        self.write(self.root / 'friends.json', {'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.friend)}}})
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.store = PackBindingStore(self.scope)
        self.addCleanup(self.store.close)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), 'utf-8')

    def fixture(self, version, schema=None, raw_allowed=True):
        directory = self.root / 'data' / 'persona_packs' / 'public-test' / 'versions' / version
        directory.mkdir(parents=True)
        refs = [asdict(SourceRef(dataset_id='public-fixture', import_id='import-fixture',
            record_id=record, message_seq=None, speaker_id=speaker,
            origin=SourceOrigin.PUBLIC_DATASET, data_scope=DataScope.PUBLIC_DATASET,
            privacy_view=PrivacyView.LOCAL_FULL, friend_id=None, event_time=None,
            recorded_at='2026-10-07T00:00:00+00:00', license_id='fixture-license',
            model_analysis_allowed=True)) for record, speaker in
            (('user-record', 'user-speaker'), ('target-record', 'target-speaker'))]
        profile = {'pack_id': 'public-test', 'version': version, 'scope': 'PUBLIC_PACK',
                   'friend_id': None, 'claims': []}
        style = dict(profile, observed={'sticker_behavior': {'legacy_fixture_value': 7}})
        if schema == 'persona-pack-manifest-v2':
            media = _empty_sticker_profile('public-test', version, 1)
            style['sticker_profile'] = media
            self.write(directory / 'sticker_profile.json', media)
        self.write(directory / 'persona_profile.json', profile)
        self.write(directory / 'style_profile.json', style)
        for name in ('persona_skill.md', 'persona_report.md'):
            (directory / name).write_text('public fixture, no media retrieval', 'utf-8')
        bubbles = [{'id': ref['record_id'], 'text': text, 'source': ref}
                   for ref, text in zip(refs, ('今天怎么样', '刚回来'))]
        with closing(sqlite3.connect(directory / 'examples.db')) as db:
            columns = [column + (' TEXT PRIMARY KEY' if column == 'id' else ' TEXT')
                       for column in EXAMPLE_COLUMNS]
            db.execute('CREATE TABLE examples (' + ','.join(columns) + ')')
            db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in columns) + ')',
                ('pair-fixture', json.dumps([bubbles[0]]), '[]', json.dumps([bubbles[1]]),
                 'unknown', None, None, None, 'PUBLIC_DATASET', None, None,
                 json.dumps(refs), version))
            db.commit()
        manifest = {'pack_id': 'public-test', 'version': version, 'scope': 'PUBLIC_PACK',
            'friend_id': None, 'source_refs': refs, 'selected_pair_ids': ['pair-fixture'],
            'sources': [{'dataset_id': 'public-fixture', 'local_use_allowed': True,
                'derived_stats_allowed': True, 'license_data': 'fixture-license',
                'raw_export_allowed': raw_allowed}],
            'raw_export_allowed': raw_allowed, 'raw_export_reasons': [],
            'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in directory.iterdir()}}
        if schema is not None:
            manifest['schema_version'] = schema
        self.write(directory / 'manifest.json', manifest)
        return directory

    def refresh_hashes(self, directory):
        manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
        manifest['files'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in directory.iterdir() if p.name != 'manifest.json'}
        self.write(directory / 'manifest.json', manifest)

    def test_legacy_v1_and_v2_read_bind_export_preserve_versions(self):
        versions = [('legacy', None), ('explicit-v1', 'persona-pack-manifest-v1'),
                    ('media-v2', 'persona-pack-manifest-v2')]
        directories = [self.fixture(version, schema) for version, schema in versions]
        before = {str(p): p.read_bytes() for directory in directories
                  for p in directory.iterdir()}
        revision = 0
        for (version, schema), directory in zip(versions, directories):
            manifest, digest = _manifest(directory, 'public-test', version)
            self.assertEqual(len(manifest['files']), 6 if schema and schema.endswith('v2') else 5)
            revision = self.store.bind('public-test', version, expected_revision=revision)['revision']
            pack = self.store.read_pack('public-test', version)
            self.assertEqual(pack['manifest_sha256'], digest)
            if schema == 'persona-pack-manifest-v2':
                media = pack['sticker_profile']
                self.assertEqual(media, pack['style_profile']['sticker_profile'])
                self.assertEqual(media['status'], 'unknown')
                self.assertIsNone(media['send_type_rates'])
                self.assertIsNone(media['sample_info']['emotion_coverage'])
                self.assertFalse(media['available_for_retrieval'])
                self.assertEqual(media['image_rights_status'], 'NOT_APPLICABLE')
                self.assertEqual(pack['style_profile']['observed']['sticker_behavior'],
                                 {'legacy_fixture_value': 7})
            else:
                self.assertIsNone(pack['sticker_profile'])
            exported = self.store.export_pack('public-test', version, self.root / ('export-' + version))
            self.assertEqual({p.name: p.read_bytes() for p in exported.iterdir()},
                             {p.name: p.read_bytes() for p in directory.iterdir()})
        active = directories[-1].parent.parent / 'active.json'
        self.write(active, {'pack_id': 'public-test', 'version': 'legacy'})
        self.assertEqual(self.store.bindings()[0]['pack_version'], 'media-v2')
        self.assertEqual(before, {str(p): p.read_bytes() for directory in directories
                                  for p in directory.iterdir()})

    def test_unknown_schemas_files_paths_and_hashes_are_rejected(self):
        for index, schema in enumerate(('unknown', 'persona-pack-manifest-v3',
                                         'persona-pack-manifest-v2', None)):
            version = 'schema-' + str(index)
            directory = self.fixture(version)
            manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
            manifest['schema_version'] = schema
            self.write(directory / 'manifest.json', manifest)
            with self.subTest(schema=schema), self.assertRaises(InvalidSource):
                _manifest(directory, 'public-test', version)
        directory = self.fixture('files', 'persona-pack-manifest-v2')
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        for fault in ('missing', 'extra', 'directory', 'hash', 'traversal'):
            with self.subTest(fault=fault):
                if fault == 'missing':
                    (directory / 'sticker_profile.json').unlink()
                elif fault == 'extra':
                    (directory / 'sticker_examples.db').write_bytes(b'unauthorized')
                elif fault == 'directory':
                    (directory / 'stickers').mkdir()
                elif fault == 'hash':
                    (directory / 'sticker_profile.json').write_text('{}', 'utf-8')
                with self.assertRaises(InvalidSource):
                    path = directory / '..' / directory.name if fault == 'traversal' else directory
                    _manifest(path, 'public-test', 'files')
                if fault == 'extra':
                    (directory / 'sticker_examples.db').unlink()
                if fault == 'directory':
                    (directory / 'stickers').rmdir()
                for name, content in original.items():
                    (directory / name).write_bytes(content)
        for key, value in (('pack_id', 'other'), ('version', 'other'),
                           ('scope', 'PRIVATE_FRIEND'), ('friend_id', '12345')):
            manifest = json.loads(original['manifest.json'])
            manifest[key] = value
            self.write(directory / 'manifest.json', manifest)
            with self.subTest(manifest_key=key), self.assertRaises(InvalidSource):
                _manifest(directory, 'public-test', 'files')
        (directory / 'manifest.json').write_bytes(original['manifest.json'])

    def test_media_identity_unknown_values_and_style_copy_are_enforced(self):
        directory = self.fixture('semantic', 'persona-pack-manifest-v2')
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        changes = [('pack_id', 'other'), ('version', 'other'), ('friend_id', '12345'),
                   ('scope', 'PRIVATE_FRIEND'), ('status', 'completed'),
                   ('asset_count', 1), ('asset_count', False), ('occurrence_count', 1),
                   ('send_type_rates', {'text_only': 0}), ('available_for_retrieval', True),
                   ('preferred_styles', ['cute']), ('emotion_groups', ['happy']),
                   ('image_rights_status', 'VERIFIED'), ('reason_codes', ['IMAGE_LICENSE_VERIFIED']),
                   ('sample_info', {'text_pair_count': 2, 'media_evaluable_count': 0,
                                    'emotion_coverage': None}),
                   ('private_asset_path', 'friends/12345/media/secret.png')]
        for key, value in changes:
            media = json.loads(original['sticker_profile.json'])
            media[key] = value
            style = json.loads(original['style_profile.json'])
            style['sticker_profile'] = media
            self.write(directory / 'sticker_profile.json', media)
            self.write(directory / 'style_profile.json', style)
            self.refresh_hashes(directory)
            with self.subTest(key=key, value=value), self.assertRaises(InvalidSource):
                self.store.bind('public-test', 'semantic')
        for name, content in original.items():
            (directory / name).write_bytes(content)
        style = json.loads(original['style_profile.json'])
        style['sticker_profile']['sample_info']['text_pair_count'] = 2
        self.write(directory / 'style_profile.json', style)
        self.refresh_hashes(directory)
        with self.assertRaises(InvalidSource):
            self.store.bind('public-test', 'semantic')
        self.assertEqual(self.store.bindings(), [])

    def test_v2_public_provenance_and_text_export_permission_still_required(self):
        denied = self.fixture('denied', 'persona-pack-manifest-v2', raw_allowed=False)
        self.store.bind('public-test', 'denied')
        with self.assertRaises(PrivacyViolation):
            self.store.export_pack('public-test', 'denied', self.root / 'denied-export')
        self.assertFalse((self.root / 'denied-export').exists())
        manifest = json.loads((denied / 'manifest.json').read_text('utf-8'))
        manifest['source_refs'][0]['data_scope'] = 'PRIVATE_FRIEND'
        manifest['source_refs'][0]['friend_id'] = '12345'
        self.write(denied / 'manifest.json', manifest)
        with self.assertRaises(PrivacyViolation):
            self.store.read_pack('public-test', 'denied')


if __name__ == '__main__':
    unittest.main()
