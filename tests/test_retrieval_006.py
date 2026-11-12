from contextlib import closing
from dataclasses import replace
import json
import sqlite3
import unittest

from persona_types import RetrievalFilters
import structured_profile
from structured_profile import StructuredFactRetriever
from tests import test_structured_profile as fixtures


class StructuredFilterTests(unittest.TestCase):
    setUp = fixtures.StructuredProfileTests.setUp
    add_memory = fixtures.StructuredProfileTests.add_memory

    def request(self, **filters):
        return replace(fixtures.StructuredProfileTests.request(self), query='生日是什么',
            filters=RetrievalFilters(namespace=('structured',), **filters))

    def add_birthday(self):
        path = self.scope.friend_dir / 'friend.json'
        profile = json.loads(path.read_text('utf-8'))
        profile['basic_info']['birthday'] = '10月1日'
        path.write_text(json.dumps(profile, ensure_ascii=False), 'utf-8')

    def test_sources_filter_before_limit_and_conflict(self):
        self.add_birthday()
        self.add_memory('birthday', '生日：10月2日', ['in-birthday'])
        retriever = StructuredFactRetriever(self.scope)
        default = retriever.retrieve(self.request(), 20).candidates
        self.assertEqual([item.content for item in default], ['生日：10月1日', '生日：10月2日'])
        self.assertTrue(all('冲突' in item.reason for item in default))
        for source_type, content in (('MANUAL', '生日：10月1日'), ('REAL_CHAT', '生日：10月2日')):
            result = retriever.retrieve(self.request(source_type=(source_type,)), 1).candidates
            self.assertEqual([item.content for item in result], [content])
            self.assertNotIn('冲突', result[0].reason)
        self.assertEqual(retriever.retrieve(self.request(source_type=('REAL_RELATIONSHIP',)), 1).candidates, ())
        self.assertEqual(len(retriever.retrieve(self.request(source_type=('MANUAL', 'REAL_CHAT')), 20).candidates), 2)

    def test_valid_missing_metadata_and_confidence(self):
        self.add_birthday()
        retriever = StructuredFactRetriever(self.scope)
        for filters in ({'valid': False}, {'topic': '生日'}, {'emotion': ('开心',)},
                        {'relationship_tags': ('familiar',)}):
            with self.subTest(filters=filters):
                self.assertEqual(retriever.retrieve(self.request(**filters), 20).candidates, ())
        item = retriever.retrieve(self.request(confidence=1.0), 1).candidates[0]
        self.assertTrue(structured_profile._matches_filters(replace(item, confidence=0.4),
            RetrievalFilters(confidence=0.4)))
        self.assertFalse(structured_profile._matches_filters(replace(item, confidence=0.4),
            RetrievalFilters(confidence=0.5)))
        foreign_manual = replace(item, source=replace(item.source, dataset_id='manual:other'))
        self.assertFalse(structured_profile._matches_filters(foreign_manual,
            RetrievalFilters(source_type=('MANUAL',))))

    def test_timestamp_precision_boundaries_and_no_recorded_at_fallback(self):
        self.add_birthday()
        for seq, stamp in enumerate(('2026-10-01T09:00:00+09:00', '2026-10-01',
                                    '2026-10-01T09:00:00', None), 1):
            self.add_memory('time-' + str(seq), '生日：10月' + str(seq) + '日',
                ['in-' + str(seq)], seq=seq)
            with closing(sqlite3.connect(self.scope.friend_dir / 'history.sqlite3')) as db, db:
                db.execute('UPDATE messages SET created_at=? WHERE seq=?', (stamp, seq))
        retriever = StructuredFactRetriever(self.scope)
        self.assertEqual(len(retriever.retrieve(self.request(), 20).candidates), 5)
        instant = self.request(time_range=('2026-10-01T00:00:00Z', '2026-10-01T00:00:00+00:00'))
        self.assertEqual([item.id for item in retriever.retrieve(instant, 20).candidates], ['memory:time-1'])
        dates = self.request(time_range=('2026-10-01', '2026-10-01'))
        self.assertEqual({item.id for item in retriever.retrieve(dates, 20).candidates},
                         {'memory:time-1', 'memory:time-2'})
        naive_bound = self.request(time_range=('2026-10-01T00:00:00', None))
        self.assertEqual(retriever.retrieve(naive_bound, 20).candidates, ())
        tomorrow = self.request(time_range=('2026-10-02', '2026-10-02'))
        self.assertEqual(retriever.retrieve(tomorrow, 20).candidates, ())

