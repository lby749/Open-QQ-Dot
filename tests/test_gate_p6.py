import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from contextlib import closing
from dataclasses import replace

from persona_types import RetrievalFilters, RetrievalRequest, ContextBudgets
from sticker_metadata import StickerAsset, StickerMetadataStore
from sticker_retriever import StickerRetriever


class GateP6Tests(unittest.TestCase):
    def setUp(self):
        from tests.test_sticker_profile import StickerProfileTests

        self.profile_fixture = StickerProfileTests()
        self.profile_fixture.setUp()
        self.addCleanup(self.profile_fixture.doCleanups)
        self.scope = self.profile_fixture.scope
        self.friend = self.scope.friend_dir

    def test_metadata_profile_occurrence_retriever_chain_and_nonsticker_exclusion(self):
        from PIL import Image

        profile_fixture = self.profile_fixture
        valid_asset_ids = []
        sessions = []
        for index, kind in enumerate(('STICKER', 'NON_STICKER', 'UNKNOWN')):
            image_path = self.friend / 'media' / f'generated-{index}.png'
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new('RGB', (12, 12), ('red', 'green', 'blue')[index]).save(image_path)
            asset_id = hashlib.sha256(image_path.read_bytes()).hexdigest()
            stored_image = image_path.with_name(asset_id + '.png')
            image_path.replace(stored_image)
            if kind == 'STICKER':
                valid_asset_ids.append(asset_id)
            session = profile_fixture.session([('', 'image', asset_id)])
            sessions.append(session)
            bubble = next(message for message in session.messages if message.media_id == asset_id)
            # This generated fixture explicitly grants local analysis; it is not live friend permission.
            analyzed_ref = replace(bubble.source, model_analysis_allowed=True)
            asset = StickerAsset(friend_id=self.scope.friend_id, asset_id=asset_id,
                original_relpath=stored_image.name, declared_kind=kind,
                source_origin=bubble.source.origin, source=f'fixture:{index}',
                source_refs=(analyzed_ref,), license_image='fixture-license',
                license_evidence='generated-only', local_use_allowed=True,
                model_analysis_allowed=True, image_redistribution_allowed=True)

            class Analyzer:
                provider_kind = 'LOCAL'

                def analyze(self, image_bytes):
                    return {'sticker_kind': kind, 'emotion': 'happy' if kind == 'STICKER' else None,
                        'intent': 'happy' if kind == 'STICKER' else None,
                        'visual_style': 'fixture', 'intensity': 0.5, 'text_content': None,
                        'description': 'generated fixture', 'analyzer_id': 'fixture-local',
                        'analysis_version': 'fixture-v1'}

            metadata = StickerMetadataStore(self.scope, analyzer=Analyzer())
            try:
                row = metadata.analyze_once(asset)
                self.assertEqual(row['sticker_kind'], kind)
                self.assertEqual(row['analysis_status'],
                    'COMPLETED' if kind == 'STICKER' else 'UNKNOWN', msg=repr(row))
            finally:
                metadata.close()

        result = profile_fixture.profile.calculate([session.id for session in sessions])
        self.assertEqual(len(result['occurrences']), 1)
        self.assertEqual(result['occurrences'][0]['asset_id'], valid_asset_ids[0])
        profile_fixture.profile.save(result, profile_fixture.pairs.revision)
        examples = profile_fixture.profile.read_examples()
        self.assertEqual(len(examples), 1)
        self.assertEqual(examples[0]['locator']['asset_id'], valid_asset_ids[0])

        history = self.friend / 'history.sqlite3'
        with closing(sqlite3.connect(history)) as db:
            db.execute('CREATE TABLE messages (seq INTEGER PRIMARY KEY, direction TEXT, body TEXT)')
            db.commit()
        retriever = StickerRetriever(self.scope)
        self.addCleanup(retriever.close)
        request = RetrievalRequest(friend_id=self.scope.friend_id, query='happy',
            recent_context='', scene='daily_chat', plan=None,
            filters=RetrievalFilters(), budgets=ContextBudgets(),
            allowed_pack_versions=frozenset(), now='2026-10-07T12:00:00+09:00')
        batch = retriever.retrieve(request, 5)
        self.assertEqual([candidate.id for candidate in batch.candidates], valid_asset_ids)
        candidate = batch.candidates[0]
        self.assertEqual(candidate.friend_id, self.scope.friend_id)
        self.assertEqual(candidate.source.friend_id, self.scope.friend_id)
        self.assertEqual(candidate.source.origin.value, 'REAL_RELATIONSHIP')
        self.assertEqual(json.loads(candidate.content)['marker'],
                         f'[[LOCAL_STICKER:{valid_asset_ids[0]}]]')
        self.assertNotIn(str(self.friend), batch.candidates[0].content)

    def test_missing_friend_success_history_fails_closed(self):
        from PIL import Image
        image_path = self.friend / 'media' / 'generated.png'
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (12, 12), 'red').save(image_path)
        asset_id = hashlib.sha256(image_path.read_bytes()).hexdigest()
        stored_image = image_path.with_name(asset_id + '.png')
        image_path.replace(stored_image)
        session = self.profile_fixture.session([('', 'image', asset_id)])
        bubble = next(message for message in session.messages if message.media_id == asset_id)
        analyzed_ref = replace(bubble.source, model_analysis_allowed=True)
        metadata = StickerMetadataStore(self.scope, analyzer=_StickerAnalyzer())
        try:
            metadata.analyze_once(StickerAsset(friend_id=self.scope.friend_id,
                asset_id=asset_id, original_relpath=stored_image.name,
                declared_kind='STICKER', source_origin=bubble.source.origin,
                source='fixture', source_refs=(analyzed_ref,), license_image='fixture-license',
                license_evidence='generated-only', local_use_allowed=True,
                model_analysis_allowed=True, image_redistribution_allowed=True))
        finally:
            metadata.close()
        history = self.friend / 'history.sqlite3'
        with closing(sqlite3.connect(history)) as db:
            db.execute('CREATE TABLE messages (seq INTEGER PRIMARY KEY, direction TEXT, body TEXT)')
            db.commit()
        self.profile_fixture.profile.save(
            self.profile_fixture.profile.calculate([session.id]), self.profile_fixture.pairs.revision)
        history.unlink()
        retriever = StickerRetriever(self.scope)
        self.addCleanup(retriever.close)
        request = RetrievalRequest(friend_id=self.scope.friend_id, query='happy',
            recent_context='', scene='daily_chat', plan=None, filters=RetrievalFilters(),
            budgets=ContextBudgets(), allowed_pack_versions=frozenset(),
            now='2026-10-07T12:00:00+09:00')
        batch = retriever.retrieve(request, 1)
        self.assertEqual(batch.candidates, ())
        self.assertTrue(batch.degraded)
        self.assertEqual(batch.errors, ('success_history_unavailable',))

    def test_public_empty_v2_pack_read_and_bind_has_no_private_asset_identity(self):
        from tests.test_persona_pack_media_format import PackMediaFormatTests
        from PIL import Image

        fixture = PackMediaFormatTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        private_media = fixture.friend / 'media' / 'private-fixture.png'
        private_media.parent.mkdir(parents=True)
        Image.new('RGB', (2, 2), 'red').save(private_media, format='PNG')
        private_asset_id = hashlib.sha256(private_media.read_bytes()).hexdigest()
        private_asset_path = str(private_media.resolve())
        directory = fixture.fixture('empty-v2', 'persona-pack-manifest-v2')
        fixture.store.bind('public-test', 'empty-v2')
        pack = fixture.store.read_pack('public-test', 'empty-v2')
        self.assertEqual(pack['sticker_profile']['status'], 'unknown')
        self.assertFalse(pack['sticker_profile']['available_for_retrieval'])
        serialized = json.dumps({key: value for key, value in pack.items() if key != 'path'},
                                ensure_ascii=False)
        self.assertNotIn('PRIVATE_FRIEND', serialized)
        self.assertNotIn(str(fixture.friend), serialized)
        self.assertNotIn(private_asset_id, serialized)
        self.assertNotIn(private_asset_path, serialized)
        self.assertEqual(pack['sticker_profile'], pack['style_profile']['sticker_profile'])
        self.assertFalse(any(Path(path).is_absolute() for path in
            json.loads((directory / 'sticker_profile.json').read_text('utf-8')).values()
            if isinstance(path, str)))


class _StickerAnalyzer:
    provider_kind = 'LOCAL'

    def analyze(self, image_bytes):
        return {'sticker_kind': 'STICKER', 'emotion': 'happy', 'intent': 'happy',
            'visual_style': 'fixture', 'intensity': 0.5, 'text_content': None,
            'description': 'generated fixture', 'analyzer_id': 'fixture-local',
            'analysis_version': 'fixture-v1'}


if __name__ == '__main__':
    unittest.main()
