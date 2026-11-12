import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from memory_retriever import MemoryRetriever
from persona_types import ContextBudgets, FriendScope, InvalidSource, RetrievalFilters, RetrievalRequest


class MemoryRetrieverTests(unittest.TestCase):
    def test_legacy_fallback_requires_verified_current_friend_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            friend = root / 'friends' / '12345'
            friend.mkdir(parents=True)
            (friend / 'friend.json').write_text(json.dumps({'qq_id': '12345'}), 'utf-8')
            (root / 'friends.json').write_text(json.dumps({'friends': {'12345': {
                'qq_id': '12345', 'directory': str(friend)}}}), 'utf-8')
            with closing(sqlite3.connect(friend / 'history.sqlite3')) as db, db:
                db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);'
                    'CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,body TEXT,created_at TEXT);'
                    'CREATE TABLE memory_entries(id TEXT PRIMARY KEY,text TEXT,kind TEXT,keywords TEXT,'
                    'sources TEXT,active INTEGER,locked INTEGER,updated_at TEXT);')
                db.execute('INSERT INTO metadata VALUES(?,?)', ('qq_id', '12345'))
                db.execute('INSERT INTO messages VALUES(1,?,?,?,?)',
                    ('in-1', 'friend', '我最近喜欢周杰伦', '2026-10-05T12:00:00+00:00'))
                db.execute('INSERT INTO memory_entries VALUES(?,?,?,?,?,?,?,?)',
                    ('m1', '喜欢周杰伦', 'fact', '[]', json.dumps(['in-1']), 1, 1,
                     '2026-10-05T12:01:00+00:00'))
                db.execute('INSERT INTO messages VALUES(2,?,?,?,?)',
                    ('out-1', 'me', '机器人说喜欢周杰伦', '2026-10-05T12:02:00+00:00'))
                db.execute('INSERT INTO memory_entries VALUES(?,?,?,?,?,?,?,?)',
                    ('m2', '喜欢周杰伦', 'fact', '[]', json.dumps(['out-1']), 1, 1,
                     '2026-10-05T12:03:00+00:00'))
            scope = FriendScope.load('12345', project_root=root)
            request = RetrievalRequest(friend_id='12345', query='周杰伦', recent_context='',
                scene='any', plan=None, filters=RetrievalFilters(), budgets=ContextBudgets(),
                allowed_pack_versions=frozenset(), now='2026-10-06T00:00:00+00:00')
            result = MemoryRetriever(scope).retrieve(request, 5)
            self.assertEqual([item.id for item in result.candidates], ['memory:m1'])
            self.assertEqual(result.candidates[0].namespace, 'memory')
            self.assertTrue(result.degraded)

            excluded = RetrievalRequest(friend_id='12345', query='周杰伦', recent_context='',
                scene='any', plan=None, filters=RetrievalFilters(namespace=('persona',)),
                budgets=ContextBudgets(), allowed_pack_versions=frozenset(),
                now='2026-10-06T00:00:00+00:00')
            self.assertEqual(MemoryRetriever(scope).retrieve(excluded, 5).candidates, ())
            wrong_source = RetrievalRequest(friend_id='12345', query='周杰伦', recent_context='',
                scene='any', plan=None, filters=RetrievalFilters(source_type=('MANUAL',)),
                budgets=ContextBudgets(), allowed_pack_versions=frozenset(),
                now='2026-10-06T00:00:00+00:00')
            self.assertEqual(MemoryRetriever(scope).retrieve(wrong_source, 5).candidates, ())
            dated = replace(request, filters=RetrievalFilters(
                time_range=('2026-10-05', '2026-10-05')))
            self.assertEqual([item.id for item in MemoryRetriever(scope).retrieve(dated, 5).candidates],
                ['memory:m1'])
            with closing(sqlite3.connect(friend / 'history.sqlite3')) as db, db:
                db.execute('UPDATE messages SET created_at=? WHERE seq=1', ('2026-10-05T12:00:00',))
            self.assertEqual(MemoryRetriever(scope).retrieve(dated, 5).candidates, ())
            self.assertEqual(MemoryRetriever(scope).retrieve(request, 5).candidates[0].timestamp,
                '2026-10-05T12:00:00')
            with self.assertRaises(InvalidSource):
                MemoryRetriever(scope).retrieve(request, 51)


if __name__ == '__main__':
    unittest.main()
