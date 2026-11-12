import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from persona_types import DataScope, FriendScope, PrivacyView, SourceOrigin, SourceRef
from sticker_metadata import StickerAsset, StickerMetadataStore


class FakeLocalAnalyzer:
    provider_kind = 'LOCAL'

    def __init__(self, result):
        self.result = result
        self.calls = []

    def analyze(self, image_bytes):
        self.calls.append(image_bytes)
        return self.result


class StickerMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.friend = self.root / 'friends' / '12345'
        (self.friend / 'media').mkdir(parents=True)
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.friend)}}}), 'utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        self.image = self.friend / 'media' / 'sample.bin'
        self.image.write_bytes(b'private fixture image bytes')
        self.asset_id = hashlib.sha256(self.image.read_bytes()).hexdigest()
        self.ref = SourceRef(dataset_id=None, import_id=None, record_id=None,
                             message_seq=1, speaker_id='12345',
                             origin=SourceOrigin.REAL_RELATIONSHIP,
                             data_scope=DataScope.PRIVATE_FRIEND,
                             privacy_view=PrivacyView.LOCAL_FULL, friend_id='12345',
                             event_time=None, recorded_at='2026-10-07T12:00:00+09:00',
                             model_analysis_allowed=True)
        self.store = StickerMetadataStore(self.scope)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def asset(self, **changes):
        fields = dict(friend_id='12345', asset_id=self.asset_id,
                      original_relpath='sample.bin', declared_kind='STICKER',
                      source_origin=SourceOrigin.REAL_RELATIONSHIP,
                      source='history:1', source_refs=(self.ref,),
                      local_use_allowed=True, model_analysis_allowed=True,
                      image_redistribution_allowed=False)
        fields.update(changes)
        return StickerAsset(**fields)

    def add_image(self, name, content):
        path = self.friend / 'media' / name
        path.write_bytes(content)
        return path, hashlib.sha256(content).hexdigest()

    @staticmethod
    def recognized_non_sticker(path, asset, refs):
        return {'sticker_kind': 'NON_STICKER', 'emotion': None, 'intent': None,
                'visual_style': None, 'intensity': None, 'text_content': None,
                'description': 'ordinary photograph', 'analyzer_id': 'fake-local',
                'analysis_version': 'fixture-1'}

    def test_local_analyzer_can_classify_declared_image_as_non_sticker(self):
        analyzer = FakeLocalAnalyzer(self.recognized_non_sticker(None, None, None))
        store = StickerMetadataStore(self.scope, analyzer=analyzer)
        try:
            row = store.analyze_once(self.asset())
            self.assertEqual((row['analysis_status'], row['sticker_kind']),
                             ('COMPLETED', 'NON_STICKER'))
            self.assertEqual(row['description'], 'ordinary photograph')
            self.assertEqual(analyzer.calls, [self.image.read_bytes()])
        finally:
            store.close()

    def test_explicit_analyzer_abstention_stays_unknown(self):
        analyzer = FakeLocalAnalyzer({
            'sticker_kind': 'UNKNOWN', 'emotion': 'unconfirmed', 'intent': 'unconfirmed',
            'visual_style': 'unconfirmed', 'intensity': 0.2, 'text_content': '',
            'description': 'unconfirmed', 'analyzer_id': 'fake-local',
            'analysis_version': 'fixture-1'})
        store = StickerMetadataStore(self.scope, analyzer=analyzer)
        try:
            row = store.analyze_once(self.asset())
            self.assertEqual((row['analysis_status'], row['sticker_kind'],
                              row['unknown_reason']),
                             ('UNKNOWN', 'UNKNOWN', 'UNCLASSIFIED'))
            self.assertEqual((row['emotion'], row['intent'], row['visual_style'],
                              row['intensity'], row['text_content'], row['description']),
                             (None, None, None, None, None, None))
            self.assertEqual((row['analyzer_id'], row['analysis_version']),
                             ('fake-local', 'fixture-1'))
        finally:
            store.close()

    def test_unavailable_analyzer_returns_unknown_without_call(self):
        row = self.store.analyze_once(self.asset())
        self.assertEqual((row['analysis_status'], row['sticker_kind'], row['unknown_reason']),
                         ('UNKNOWN', 'UNKNOWN', 'LOCAL_UNAVAILABLE'))

    def test_unknown_and_non_sticker_declarations_are_never_promoted(self):
        calls = []
        class CountingAnalyzer:
            provider_kind = 'LOCAL'

            def analyze(self, image_bytes):
                calls.append(image_bytes)

        store = StickerMetadataStore(self.scope, analyzer=CountingAnalyzer())
        try:
            unknown = store.analyze_once(self.asset(declared_kind='UNKNOWN'))
            _, other_id = self.add_image('not-sticker.bin', b'ordinary photo')
            non_sticker = store.analyze_once(self.asset(
                asset_id=other_id, original_relpath='not-sticker.bin',
                declared_kind='NON_STICKER'))
            self.assertEqual((unknown['sticker_kind'], unknown['unknown_reason']),
                             ('UNKNOWN', 'UNCLASSIFIED'))
            self.assertEqual((non_sticker['sticker_kind'], non_sticker['unknown_reason']),
                             ('NON_STICKER', 'UNCLASSIFIED'))
        finally:
            store.close()
        self.assertEqual(calls, [])

    def test_null_and_denied_permissions_skip_analyzer(self):
        calls = []
        class CountingAnalyzer:
            provider_kind = 'LOCAL'

            def analyze(self, image_bytes):
                calls.append(image_bytes)

        store = StickerMetadataStore(self.scope, analyzer=CountingAnalyzer())
        try:
            unknown = store.analyze_once(self.asset(model_analysis_allowed=None))
            _, denied_id = self.add_image('denied.bin', b'denied photo')
            denied = store.analyze_once(self.asset(
                asset_id=denied_id, original_relpath='denied.bin',
                model_analysis_allowed=False))
            self.assertEqual(unknown['unknown_reason'], 'PERMISSION_UNKNOWN')
            self.assertEqual(denied['unknown_reason'], 'PERMISSION_DENIED')
            self.assertEqual(calls, [])
        finally:
            store.close()

    def test_private_analysis_is_local_and_same_asset_is_idempotent(self):
        output = {'sticker_kind': 'STICKER', 'emotion': 'happy', 'intent': 'react',
                  'visual_style': None, 'intensity': 0.7, 'text_content': '',
                  'description': None, 'analyzer_id': 'fake-local',
                  'analysis_version': 'fixture-1'}
        analyzer = FakeLocalAnalyzer(output)
        store = StickerMetadataStore(self.scope, analyzer=analyzer)
        try:
            before = store.store.revision
            first = store.analyze_once(self.asset())
            after_first = store.store.revision
            second = store.analyze_once(self.asset())
            self.assertEqual(first, second)
            self.assertEqual(after_first, before + 1)
            self.assertEqual(store.store.revision, after_first)
            self.assertEqual(analyzer.calls, [self.image.read_bytes()])
            self.assertEqual(first['text_content'], '')
        finally:
            store.close()

    def test_same_asset_with_changed_source_is_a_conflict(self):
        from persona_store import Conflict
        first = self.store.analyze_once(self.asset())
        self.assertEqual(first['analysis_status'], 'UNKNOWN')
        with self.assertRaises(Conflict):
            self.store.analyze_once(self.asset(source='history:2'))

    def test_missing_source_records_unknown_without_fabricating_a_ref(self):
        asset = self.asset(source=None, source_origin=None, source_refs=())
        row = self.store.analyze_once(asset)
        self.assertEqual((row['sticker_kind'], row['unknown_reason']),
                         ('UNKNOWN', 'UNCLASSIFIED'))
        stored = self.store.store.db.execute(
            'SELECT source_origin,source FROM sticker_assets WHERE friend_id=? AND asset_id=?',
            ('12345', self.asset_id)).fetchone()
        self.assertEqual(tuple(stored), (None, None))

    def test_cloud_analyzer_is_rejected_without_receiving_bytes(self):
        class CloudAnalyzer:
            provider_kind = 'CLOUD'

            def analyze(self, image_bytes):
                self.fail('cloud must never receive image bytes')

        with self.assertRaises(ValueError):
            StickerMetadataStore(self.scope, analyzer=CloudAnalyzer())

    def test_asset_path_escape_and_hash_mismatch_are_rejected(self):
        from persona_store import ScopeMismatch
        with self.assertRaises(ScopeMismatch):
            self.store.analyze_once(self.asset(original_relpath='../outside.bin'))
        with self.assertRaises(ScopeMismatch):
            self.store.analyze_once(self.asset(asset_id='0' * 64))
        with self.assertRaises(ValueError):
            self.store.analyze_once(self.asset(original_relpath=None))


if __name__ == '__main__':
    unittest.main()
