import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from persona_store import ScopeMismatch
from persona_types import (ContextBudgets, DataScope, FriendScope, RetrievalFilters,
                           RetrievalRequest, SourceOrigin)
from structured_profile import StructuredFactRetriever


class StructuredProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        friends = {}
        for friend_id in ('12345', '23456'):
            directory = self.root / 'friends' / friend_id
            directory.mkdir(parents=True)
            profile = {'qq_id': friend_id, 'display_name': '不得作为实名',
                       'basic_info': {'nickname': '小雨', 'school': 'medium',
                                      'notes': '兴趣：生日可能是10月1日'}}
            (directory / 'friend.json').write_text(
                json.dumps(profile, ensure_ascii=False), encoding='utf-8')
            friends[friend_id] = {'qq_id': friend_id, 'directory': str(directory),
                                  'self_id': '34567'}
            with closing(sqlite3.connect(directory / 'history.sqlite3')) as db:
                with db:
                    db.executescript('''
                    CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
                    CREATE TABLE messages(seq INTEGER PRIMARY KEY,qq_id TEXT,direction TEXT,
                                          body TEXT,created_at TEXT,handled INTEGER);
                    CREATE TABLE memory_entries(id TEXT PRIMARY KEY,text TEXT NOT NULL,
                        kind TEXT NOT NULL,keywords TEXT NOT NULL,sources TEXT NOT NULL,
                        locked INTEGER NOT NULL,active INTEGER NOT NULL,updated_at TEXT NOT NULL);
                    INSERT INTO metadata VALUES('qq_id','12345');
                    ''')
        (self.root / 'friends.json').write_text(
            json.dumps({'friends': friends}), encoding='utf-8')
        self.scope = FriendScope.load('12345', project_root=self.root)

    def request(self, friend_id='12345', *, query='生日'):
        return RetrievalRequest(
            friend_id=friend_id, query=query, recent_context='', scene='recall',
            plan=None, filters=RetrievalFilters(namespace=('structured',)),
            budgets=ContextBudgets(), allowed_pack_versions=frozenset(),
            now='2026-10-06T00:00:00+00:00')

    def add_memory(self, row_id, text, source_ids, *, direction='friend', seq=1,
                   kind='fact', locked=1, active=1, add_message=True):
        directory = self.root / 'friends' / '12345'
        with closing(sqlite3.connect(directory / 'history.sqlite3')) as db:
            with db:
                if add_message:
                    db.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?)',
                               (seq, source_ids[0], direction, text,
                                '2026-10-01T09:00:00+00:00', 0))
                db.execute('INSERT INTO memory_entries VALUES(?,?,?,?,?,?,?,?)',
                           (row_id, text, kind, '[]', json.dumps(source_ids), locked, active,
                            '2026-10-02T09:00:00+00:00'))

    def test_manual_locked_conflict_unknown_and_verified_legacy_source(self):
        self.add_memory('m1', '称呼：小云', ['in-1'])
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        result = StructuredFactRetriever(self.scope).retrieve(self.request(query=''), 20)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        items = result.candidates
        nicknames = [item for item in items if '称呼' in item.content]
        self.assertTrue(len(nicknames) == 2 and any('小雨' in n.content for n in nicknames) and any('小云' in n.content for n in nicknames))
        self.assertIn('冲突', nicknames[0].reason)
        self.assertEqual(nicknames[0].source.origin, SourceOrigin.MANUAL)
        self.assertEqual(nicknames[0].source.dataset_id, 'manual:friend_profile')
        self.assertEqual(nicknames[0].source.import_id, hashlib.sha256(
            (self.scope.friend_dir / 'friend.json').read_bytes()).hexdigest())
        self.assertEqual(nicknames[1].source.message_seq, 1)
        self.assertEqual(nicknames[1].source.origin, SourceOrigin.REAL_RELATIONSHIP)
        self.assertNotIn('生日', [item.content for item in items])
        self.assertNotIn('不得作为实名', '\n'.join(item.content for item in items))

    def test_unconfirmed_or_non_friend_legacy_memory_is_omitted(self):
        self.add_memory('bot', ': 10months1', ['out-1'], direction='me', seq=2)
        self.add_memory('missing', ': 10months2', ['missing-source'], seq=3,
                        add_message=False)
        self.add_memory('unlocked', ': 10months3', ['in-4'], seq=4, locked=0)
        result = StructuredFactRetriever(self.scope).retrieve(self.request(query=''), 20)
        self.assertTrue(len(result.candidates) == 2 and any('小雨' in c.content for c in result.candidates) and any('medium' in c.content or '一中' in c.content for c in result.candidates))

    def test_missing_history_falls_back_to_manual_profile_without_writes(self):
        history = self.scope.friend_dir / 'history.sqlite3'
        history.unlink()
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        result = StructuredFactRetriever(self.scope).retrieve(self.request(query=''), 20)
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.assertTrue(len(result.candidates) == 2 and any('小雨' in c.content for c in result.candidates) and any('medium' in c.content or '一中' in c.content for c in result.candidates))

    def test_cross_friend_request_is_rejected(self):
        with self.assertRaises(ScopeMismatch):
            StructuredFactRetriever(self.scope).retrieve(self.request('23456'), 10)

    def test_exact_school_query_keeps_requested_fact_under_small_limit(self):
        from dataclasses import replace
        request = replace(self.request(), query='学校是哪所？')
        result = StructuredFactRetriever(self.scope).retrieve(request, 1)
        self.assertTrue(len(result.candidates) == 1 and ('medium' in result.candidates[0].content or '一中' in result.candidates[0].content))


if __name__ == '__main__':
    unittest.main()
