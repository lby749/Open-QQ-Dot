from contextlib import closing
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import persona_store as storage
from persona_pack_bindings import PackBindingStore
from persona_store import Conflict, PersonaStore, ScopeMismatch, Unavailable
from persona_types import DataScope, FriendScope, InvalidSource, PrivacyViolation, PrivacyView, SourceOrigin, SourceRef
from persona_versions import EXAMPLE_COLUMNS


class PackBindingIntegrationTests(unittest.TestCase):
    def test_friend_cas_weights_readonly_and_export_permissions(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        records = {}
        for friend in ('12345', '23456', '34567', '45678'):
            directory = root / 'friends' / friend
            directory.mkdir(parents=True)
            (directory / 'friend.json').write_text(json.dumps({'qq_id': friend,
                'persona': 'keep existing persona', 'proactive_density': 2}), 'utf-8')
            records[friend] = {'qq_id': friend, 'directory': str(directory)}
        (root / 'friends.json').write_text(json.dumps({'friends': records}), 'utf-8')
        scopes = [FriendScope.load(friend, project_root=root) for friend in records]
        profiles_before = [(scope.friend_dir / 'friend.json').read_bytes() for scope in scopes]
        now = '2026-10-06T00:00:00+00:00'
        refs = [asdict(SourceRef(dataset_id='public-fixture', import_id='public-import', record_id=record,
            message_seq=None, speaker_id=speaker, origin=SourceOrigin.PUBLIC_DATASET,
            data_scope=DataScope.PUBLIC_DATASET, privacy_view=PrivacyView.LOCAL_FULL, friend_id=None,
            event_time=None, recorded_at=now, license_id='CC-BY', model_analysis_allowed=True))
            for record, speaker in (('user-record', 'human-user'), ('target-record', 'human-target'))]
        def save_pack(version, raw_allowed=False, private=False):
            directory = root / 'data/persona_packs/public-test/versions' / version
            directory.mkdir(parents=True)
            profile = {'pack_id': 'public-test', 'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None,
                'claims': [{'subject': 'BOT_PERSONA', 'source_origin': ['PUBLIC_DATASET'],
                    'evidence': [{'data_scope': 'PRIVATE_FRIEND' if private else 'PUBLIC_DATASET',
                                  'source_origin': 'PUBLIC_DATASET'}]}]}
            style = dict(profile, claims=[], observed={'sample_info': {'friend_id': None, 'data_scope': 'PUBLIC_DATASET'}})
            (directory / 'persona_profile.json').write_text(json.dumps(profile), 'utf-8')
            (directory / 'style_profile.json').write_text(json.dumps(style), 'utf-8')
            for name in ('persona_skill.md', 'persona_report.md'):
                (directory / name).write_text('public fixture derived document', 'utf-8')
            def bubble(ref, text): return {'id': ref['record_id'], 'text': text, 'source': ref}
            with sqlite3.connect(directory / 'examples.db') as db:
                columns = [column + (' TEXT PRIMARY KEY' if column == 'id' else ' TEXT') for column in EXAMPLE_COLUMNS]
                db.execute('CREATE TABLE examples (' + ','.join(columns) + ')')
                db.execute('INSERT INTO examples VALUES(' + ','.join('?' for _ in EXAMPLE_COLUMNS) + ')',
                    ('pair-fixture', json.dumps([bubble(refs[0], '今天怎么样呀')]), '[]',
                     json.dumps([bubble(refs[1], '刚听了首歌，挺开心的')]), 'daily_chat', None, None, None,
                     'PUBLIC_DATASET', None, None, json.dumps(refs), version))
            db.close()
            manifest = {'pack_id': 'public-test', 'version': version, 'scope': 'PUBLIC_PACK', 'friend_id': None,
                'source_refs': refs, 'selected_pair_ids': ['pair-fixture'],
                'sources': [{'dataset_id': 'public-fixture', 'local_use_allowed': True,
                    'derived_stats_allowed': True, 'license_data': 'CC-BY', 'raw_export_allowed': raw_allowed}],
                'raw_export_allowed': raw_allowed, 'raw_export_reasons': [] if raw_allowed else ['fixture prohibits redistribution'],
                'files': {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in directory.iterdir()}}
            (directory / 'manifest.json').write_text(json.dumps(manifest), 'utf-8')
            return directory
        first, second, denied_private = save_pack('v1'), save_pack('v2', True), save_pack('bad', private=True)
        public_root = first.parent.parent
        (public_root / 'active.json').write_text(json.dumps({'pack_id': 'public-test', 'version': 'v2'}), 'utf-8')
        public_before = {str(path.relative_to(public_root)): path.read_bytes()
                         for path in public_root.rglob('*') if path.is_file()}
        with self.assertRaises(Unavailable): PackBindingStore(scopes[2], readonly=True)
        self.assertFalse((scopes[2].friend_dir / 'persona.sqlite3').exists())
        # The old read-only sidecar must not migrate itself to obtain binding support.
        with patch.object(storage, 'MIGRATIONS', storage.MIGRATIONS[:9]):
            old = PersonaStore(scopes[3].friend_id, scopes[3].friend_dir, project_root=root)
            old.close()
        old_path = scopes[3].friend_dir / 'persona.sqlite3'
        old_bytes = old_path.read_bytes()
        with self.assertRaises(Unavailable): PackBindingStore(scopes[3], readonly=True)
        self.assertEqual(old_path.read_bytes(), old_bytes)
        first_store, other_store = PackBindingStore(scopes[0]), PackBindingStore(scopes[1])
        self.addCleanup(first_store.close)
        self.addCleanup(other_store.close)
        source_revision = first_store.store.revision
        with self.assertRaises(ScopeMismatch): first_store.read_pack('public-test', 'v1')
        with self.assertRaises(PrivacyViolation): first_store.bind('public-test', 'bad')
        self.assertEqual(first_store.bindings(), [])
        first_binding = first_store.bind('public-test', 'v1', public_strength=.8, relationship_scale=20)
        self.assertEqual(first_binding['revision'], 1)
        scope = first_store.bound_scope()
        scope.require_pack('public-test', 'v1')
        with self.assertRaises(ScopeMismatch): scope.require_pack('public-test', 'v2')
        with self.assertRaises(ScopeMismatch): other_store.read_pack('public-test', 'v1')
        other_store.bind('public-test', 'v2')
        detached = first_store.read_pack('public-test', 'v1')
        self.assertIsNone(detached['examples'][0]['friend_id'])
        self.assertEqual(detached['examples'][0]['target_bubbles'][0]['text'], '刚听了首歌，挺开心的')
        self.assertTrue(all(ref['friend_id'] is None for ref in detached['manifest']['source_refs']))
        detached['persona_profile']['friend_id'] = 'detached-copy-only'
        self.assertIsNone(first_store.read_pack('public-test', 'v1')['persona_profile']['friend_id'])
        weights = [first_store.weights('public-test', 'v1', n)['public_weight'] for n in range(101)]
        self.assertEqual(weights[0], .8)
        self.assertAlmostEqual(weights[20], .4)
        self.assertTrue(all(0 < after <= before and before-after < .04 for before, after in zip(weights, weights[1:])))
        self.assertEqual(first_store.weights('public-test', 'v1', 20)['relationship_weight'], .6)
        for count in (-1, True, 1.5):
            with self.assertRaises(InvalidSource): first_store.weights('public-test', 'v1', count)
        with self.assertRaises(Conflict): first_store.bind('public-test', 'v2', expected_revision=0)
        with self.assertRaises(PrivacyViolation): first_store.export_pack('public-test', 'v1', root / 'denied-export')
        self.assertFalse((root / 'denied-export').exists())
        updated = first_store.bind('public-test', 'v2', expected_revision=1)
        self.assertEqual(updated['revision'], 2)
        with self.assertRaises(ScopeMismatch): first_store.read_pack('public-test', 'v1')
        exported = first_store.export_pack('public-test', 'v2', root / 'allowed-export')
        self.assertEqual({p.name: p.read_bytes() for p in exported.iterdir()}, {p.name: p.read_bytes() for p in second.iterdir()})
        with self.assertRaises(InvalidSource): first_store.export_pack('public-test', 'v2', exported)
        rolled_back = first_store.bind('public-test', 'v1', expected_revision=2)
        self.assertEqual(rolled_back['revision'], 3)
        self.assertEqual(other_store.bindings()[0]['pack_version'], 'v2')
        readonly = PackBindingStore(scopes[0], readonly=True)
        self.addCleanup(readonly.close)
        self.assertEqual(readonly.read_pack('public-test', 'v1')['manifest']['version'], 'v1')
        with self.assertRaises(Unavailable): readonly.bind('public-test', 'v2', expected_revision=3)
        # Verify all five hashes on every read, rather than trusting an enabled binding.
        for name in ('persona_profile.json', 'style_profile.json', 'persona_skill.md', 'persona_report.md', 'examples.db'):
            path = first / name
            original = path.read_bytes()
            path.write_bytes(original + b'tampered')
            with self.assertRaisesRegex(InvalidSource, 'hash changed'): readonly.read_pack('public-test', 'v1')
            path.write_bytes(original)
        with self.assertRaises(Conflict): first_store.unbind('public-test', expected_revision=2)
        self.assertEqual(first_store.unbind('public-test', expected_revision=3), 4)
        self.assertFalse(first_store.bindings()[0]['enabled'])
        with self.assertRaises(ScopeMismatch): readonly.read_pack('public-test', 'v1')
        self.assertEqual(first_store.bound_scope().allowed_pack_versions, frozenset())
        self.assertEqual(first_store.store.revision, source_revision)
        self.assertEqual([ (scope.friend_dir / 'friend.json').read_bytes() for scope in scopes], profiles_before)
        self.assertEqual({str(path.relative_to(public_root)): path.read_bytes()
                         for path in public_root.rglob('*') if path.is_file()}, public_before)


if __name__ == '__main__':
    unittest.main()
