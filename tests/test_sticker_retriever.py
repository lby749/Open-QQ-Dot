import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from persona_store import PersonaStore, ScopeMismatch
from persona_types import ContextBudgets, FriendScope, RetrievalFilters, RetrievalRequest
from sticker_retriever import StickerRetriever


NOW = '2026-10-07T12:00:00+09:00'


def request(friend_id='12345', query='happy', filters=None):
    return RetrievalRequest(friend_id=friend_id, query=query, recent_context='',
        scene='daily_chat', plan=None, filters=filters or RetrievalFilters(),
        budgets=ContextBudgets(), allowed_pack_versions=frozenset(), now=NOW)


class StickerRetrieverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.friend = self.root / 'friends' / '12345'
        media = self.friend / 'media'
        media.mkdir(parents=True)
        (self.friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
        (self.root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
            'qq_id': '12345', 'directory': str(self.friend)}}}), 'utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)
        image = media / 'temp.png'
        Image.new('RGB', (16, 12), 'red').save(image, format='PNG')
        self.asset_id = hashlib.sha256(image.read_bytes()).hexdigest()
        self.asset_path = media / (self.asset_id + '.png')
        image.replace(self.asset_path)
        self._seed_asset()
        history = sqlite3.connect(self.friend / 'history.sqlite3')
        history.execute('CREATE TABLE messages (seq INTEGER PRIMARY KEY, direction TEXT, body TEXT)')
        history.execute("INSERT INTO messages VALUES(1,'friend','fixture source sticker')")
        history.commit()
        history.close()

    def _seed_asset(self, *, asset_id=None, asset_path=None, relpath=None, local=1,
                    redistribute=1, emotion='happy', intent='happy',
                    analyzed_at=NOW, event_time=NOW, origin='REAL_RELATIONSHIP'):
        asset_id = asset_id or self.asset_id
        asset_path = asset_path or self.asset_path
        ref = {'dataset_id': None, 'import_id': None, 'record_id': None,
            'message_seq': 1, 'speaker_id': '12345', 'origin': 'REAL_RELATIONSHIP',
            'data_scope': 'PRIVATE_FRIEND', 'privacy_view': 'LOCAL_FULL',
            'friend_id': '12345', 'event_time': event_time, 'recorded_at': NOW,
            'license_id': None, 'model_analysis_allowed': True}
        ref['origin'] = origin
        source = json.dumps({'locator': 'history:1', 'source_refs': [ref]},
                            ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            store.db.execute(
                'INSERT INTO sticker_assets '
                '(friend_id,asset_id,original_relpath,privacy_scope,source_origin,source,'
                'declared_kind,license_image,license_evidence,local_use_allowed,'
                'model_analysis_allowed,image_redistribution_allowed,valid,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('12345', asset_id, relpath or asset_path.name, 'PRIVATE_FRIEND',
                 origin, source, 'STICKER', 'fixture-license', 'fixture-evidence',
                 local, 1, redistribute, 1, NOW))
            store.db.execute(
                'INSERT INTO sticker_metadata '
                '(friend_id,asset_id,analysis_status,sticker_kind,emotion,intent,visual_style,'
                'intensity,text_content,description,analyzer_id,analysis_version,unknown_reason,'
                'analyzed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ('12345', asset_id, 'COMPLETED', 'STICKER', emotion, intent,
                 'flat illustration', 0.8, None, '开心的红色表情', 'fixture-local', 'v1', None,
                 analyzed_at))
        finally:
            store.close()

    def _retriever(self):
        retriever = StickerRetriever(self.scope)
        self.addCleanup(retriever.close)
        return retriever

    def test_returns_one_friend_local_marker_without_exposing_path(self):
        batch = self._retriever().retrieve(request(), 20)
        self.assertEqual(len(batch.candidates), 1)
        candidate = batch.candidates[0]
        self.assertEqual(candidate.friend_id, '12345')
        self.assertEqual(candidate.namespace, 'sticker')
        self.assertEqual(candidate.id, self.asset_id)
        content = json.loads(candidate.content)
        self.assertEqual(content['marker'], f'[[LOCAL_STICKER:{self.asset_id}]]')
        self.assertNotIn(str(self.friend), candidate.content)
        self.assertNotIn('file://', candidate.content)

    def test_cross_friend_request_is_rejected(self):
        with self.assertRaises(ScopeMismatch):
            self._retriever().retrieve(request(friend_id='23456'), 1)

    def test_path_traversal_asset_is_rejected(self):
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            store.db.execute('UPDATE sticker_assets SET original_relpath=? WHERE friend_id=?',
                             ('../' + self.asset_path.name, '12345'))
        finally:
            store.close()
        with self.assertRaises(ScopeMismatch):
            self._retriever().retrieve(request(), 1)

    def test_successful_same_image_is_not_retrieved_again(self):
        history = sqlite3.connect(self.friend / 'history.sqlite3')
        history.execute('INSERT INTO messages(direction,body) VALUES(?,?)',
                        ('me', f'[[LOCAL_STICKER:{self.asset_id}]]'))
        history.commit()
        history.close()
        batch = self._retriever().retrieve(request(), 1)
        self.assertEqual(batch.candidates, ())

    def test_no_metadata_match_returns_empty_for_text_only(self):
        batch = self._retriever().retrieve(request(query='完全无关的星际天气'), 1)
        self.assertEqual(batch.candidates, ())

    def test_unlicensed_asset_is_not_a_candidate(self):
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            store.db.execute('UPDATE sticker_assets SET image_redistribution_allowed=0 WHERE friend_id=?',
                             ('12345',))
        finally:
            store.close()
        batch = self._retriever().retrieve(request(), 1)
        self.assertEqual(batch.candidates, ())

    def test_valid_false_and_emotion_filter_are_exact(self):
        invalid = request(filters=RetrievalFilters(valid=False))
        emotion_miss = request(filters=RetrievalFilters(emotion=('sad',)))
        retriever = self._retriever()
        self.assertEqual(retriever.retrieve(invalid, 1).candidates, ())
        self.assertEqual(retriever.retrieve(emotion_miss, 1).candidates, ())

    def test_source_type_filter_is_unsupported_even_for_source_origins(self):
        retriever = self._retriever()
        for source_type in ('REAL_RELATIONSHIP', 'SYNTHETIC', 'REAL_CHAT'):
            with self.subTest(source_type=source_type):
                batch = retriever.retrieve(request(filters=RetrievalFilters(
                    source_type=(source_type,))), 1)
                self.assertEqual(batch.candidates, ())
                self.assertTrue(batch.degraded)
                self.assertEqual(batch.errors, ('unsupported_source_type_filter',))

    def test_unsupported_metadata_filters_fail_closed_with_stable_codes(self):
        retriever = self._retriever()
        unsupported = (
            (RetrievalFilters(source_type=('CHAT_MESSAGE',)), 'unsupported_source_type_filter'),
            (RetrievalFilters(topic='music'), 'unsupported_topic_filter'),
            (RetrievalFilters(relationship_tags=('close_friend',)), 'unsupported_relationship_filter'),
        )
        for filters, code in unsupported:
            with self.subTest(code=code):
                batch = retriever.retrieve(request(filters=filters), 1)
                self.assertEqual(batch.candidates, ())
                self.assertTrue(batch.degraded)
                self.assertIn(code, batch.errors)

    def test_time_range_uses_aware_verified_source_event_time(self):
        retriever = self._retriever()
        matching = request(filters=RetrievalFilters(time_range=(NOW, NOW)))
        outside = request(filters=RetrievalFilters(time_range=(
            '2026-10-08T00:00:00+09:00', '2026-10-09T00:00:00+09:00')))
        self.assertEqual(len(retriever.retrieve(matching, 1).candidates), 1)
        self.assertEqual(retriever.retrieve(outside, 1).candidates, ())
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            row = store.db.execute('SELECT source FROM sticker_assets WHERE friend_id=?',
                                   ('12345',)).fetchone()
            source = json.loads(row['source'])
            source['source_refs'][0]['event_time'] = '2026-10-07T12:00:00'
            store.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=?',
                             (json.dumps(source, separators=(',', ':')), '12345'))
        finally:
            store.close()
        self.assertEqual(retriever.retrieve(matching, 1).candidates, ())
        self.assertEqual(retriever.retrieve(request(filters=RetrievalFilters(
            time_range=('2026-10-07', '2026-10-07'))), 1).candidates, ())
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            row = store.db.execute('SELECT source FROM sticker_assets WHERE friend_id=?',
                                   ('12345',)).fetchone()
            source = json.loads(row['source'])
            source['source_refs'][0]['event_time'] = None
            store.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=?',
                             (json.dumps(source, separators=(',', ':')), '12345'))
        finally:
            store.close()
        self.assertEqual(retriever.retrieve(matching, 1).candidates, ())

    def test_date_bounds_include_event_local_date_and_mixed_utc_bound(self):
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            row = store.db.execute('SELECT source FROM sticker_assets WHERE friend_id=?',
                                   ('12345',)).fetchone()
            source = json.loads(row['source'])
            source['source_refs'][0]['event_time'] = '2026-10-07T00:30:00+09:00'
            store.db.execute('UPDATE sticker_assets SET source=? WHERE friend_id=?',
                             (json.dumps(source, separators=(',', ':')), '12345'))
        finally:
            store.close()
        retriever = self._retriever()
        cases = (
            (('2026-10-07', '2026-10-07'), 1),
            (('2026-10-06', '2026-10-06'), 0),
            (('2026-10-08', None), 0),
            ((None, '2026-10-07'), 1),
            (('2026-10-06T15:30:00Z', '2026-10-06T15:30:00+00:00'), 1),
            (('2026-10-07', '2026-10-06T15:30:00Z'), 1),
            (('2026-10-07T00:30:00', None), 0),
        )
        for bounds, count in cases:
            with self.subTest(bounds=bounds):
                batch = retriever.retrieve(request(filters=RetrievalFilters(time_range=bounds)), 1)
                self.assertEqual(len(batch.candidates), count)

    def test_confidence_filter_uses_current_overlap_score(self):
        retriever = self._retriever()
        baseline = retriever.retrieve(request(), 1).candidates[0].confidence
        self.assertEqual(len(retriever.retrieve(request(filters=RetrievalFilters(
            confidence=baseline)), 1).candidates), 1)
        self.assertEqual(retriever.retrieve(request(filters=RetrievalFilters(
            confidence=min(1.0, baseline + 0.01))), 1).candidates, ())

    def test_emotion_predicate_is_applied_before_bounded_limit(self):
        store = PersonaStore('12345', self.friend, project_root=self.root)
        try:
            store.db.execute('UPDATE sticker_metadata SET emotion=?,analyzed_at=? WHERE friend_id=?',
                             ('sad', '2026-10-08T00:00:00+09:00', '12345'))
        finally:
            store.close()
        image = self.friend / 'media' / 'new.png'
        Image.new('RGB', (16, 12), 'blue').save(image, format='PNG')
        asset_id = hashlib.sha256(image.read_bytes()).hexdigest()
        asset_path = image.with_name(asset_id + '.png')
        image.replace(asset_path)
        self._seed_asset(asset_id=asset_id, asset_path=asset_path,
                         emotion='happy', analyzed_at=NOW)
        from unittest.mock import patch
        with patch('sticker_retriever._POOL_LIMIT', 1):
            batch = self._retriever().retrieve(request(filters=RetrievalFilters(
                emotion=('happy',))), 1)
        self.assertEqual([item.id for item in batch.candidates], [asset_id])


if __name__ == '__main__':
    unittest.main()
