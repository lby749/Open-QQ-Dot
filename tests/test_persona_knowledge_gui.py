import unittest
import hashlib
import json
from unittest.mock import Mock

from persona_knowledge_gui import (PersonaKnowledgeService, display_permission,
                                   display_time, entry_times)
from persona_store import Conflict
from persona_types import (ContextBudgets, DataScope, FriendScope, RetrievalFilters,
                           RetrievalRequest, ScopeMismatch)
from tests import test_persona_evidence as fixtures


class PersonaKnowledgeGuiTests(unittest.TestCase):
    def setUp(self):
        fixtures.PersonaEvidenceTests.setUp(self)
        self.service = PersonaKnowledgeService(self.scope)
        self.addCleanup(self.service.close)

    def test_exact_full_scope_is_required_for_injected_store(self):
        other_root = self.root.parent
        # Same QQ and directory cannot be reinterpreted under another project root.
        with self.assertRaises(ScopeMismatch):
            FriendScope.load(self.scope.friend_id, project_root=other_root,
                             friend_dir=self.scope.friend_dir)

    def test_unknown_event_time_is_rendered_as_unknown(self):
        self.assertEqual(display_time(None), '未知')
        self.assertEqual(display_time('2026-01-01T00:00:00Z'), '2026-01-01T00:00:00Z')
        self.assertEqual(entry_times({'updated_at': '2026-01-02',
            'analyzed_at': '2026-01-03'}), ('未知', '未知', '未知', '未知', '未知'))
        self.assertEqual((display_permission(1), display_permission(0),
                          display_permission(None)), ('允许', 'refuse', '未知'))

    def test_store_scope_mismatch_is_rejected(self):
        factory = Mock()
        store = Mock()
        store.scope = object()
        store.close = Mock()
        factory.return_value = store
        service = PersonaKnowledgeService(self.scope, interest_store_factory=factory)
        with self.assertRaises(ScopeMismatch):
            service.entries('interest')
        store.close.assert_called_once_with()

    def test_sticker_snapshot_is_bounded_and_unknowns_stay_unknown(self):
        store = Mock()
        store.db.execute.return_value.fetchall.return_value = [
            {'asset_id': str(i), 'valid': 0 if i == 0 else 1,
             'local_use_allowed': None, 'image_redistribution_allowed': 0,
             'analysis_status': 'UNKNOWN', 'sticker_kind': None, 'analyzed_at': None}
            for i in range(101)]
        with unittest.mock.patch('persona_knowledge_gui.PersonaStore', return_value=store):
            rows, overflow = self.service.stickers()
        self.assertEqual(len(rows), 100)
        self.assertTrue(overflow)
        self.assertIsNone(rows[0]['local_use_allowed'])
        self.assertIsNone(rows[0]['sticker_kind'])
        self.assertEqual(rows[0]['valid'], 0)
        store.close.assert_called_once_with()

    def test_interest_topic_and_sticker_snapshots_do_not_write_database(self):
        interest = self.service.manual_interest(entry_id='read-interest', topic='astronomy',
            content='隔离读测试', category='人工标记')
        self.service.save_manual_interest(interest, 0)
        topic = self.service.manual_topic(entry_id='read-topic', topic='星云', content='隔离读测试')
        self.service.save_topic(topic, 0)
        databases = (self.scope.friend_dir / 'persona.sqlite3',
                     self.scope.friend_dir / 'persona.sqlite3-wal',
                     self.scope.friend_dir / 'history.sqlite3',
                     self.scope.friend_dir / 'history.sqlite3-wal')
        before = tuple(path.read_bytes() if path.exists() else None for path in databases)
        self.service.entries('interest')
        self.service.entries('topic')
        self.service.stickers()
        after = tuple(path.read_bytes() if path.exists() else None for path in databases)
        self.assertEqual(after, before)

    def test_manual_interest_is_locked_and_tombstone_uses_current_revision(self):
        entry = self.service.manual_interest(entry_id='manual-interest', topic='astronomy',
            content='喜欢观测星云', category='人工标记')
        self.assertEqual((entry['source_type'], entry['interest_type'], entry['locked']),
                         ('MANUAL', 'manual', True))
        self.assertIsNone(entry['first_seen'])
        self.assertIsNone(entry['last_seen'])
        times = entry_times(entry)
        self.assertEqual(times[:2], ('未知', entry['source_refs'][0].recorded_at))
        self.assertEqual(self.service.save_manual_interest(entry, 0), 1)
        backups = tuple((self.scope.friend_dir / '.persona_knowledge_gui_backups').glob('*.sqlite3'))
        self.assertEqual(len(backups), 1)
        current = next(row for row in self.service.entries('interest')
                       if row['id'] == 'manual-interest')
        self.assertIsNone(current['source_refs'][0].event_time)
        with self.assertRaises(Conflict):
            self.service.tombstone('interest', current['id'], current['revision'])
        self.assertEqual(next(row for row in self.service.entries('interest')
                              if row['id'] == current['id'])['revision'], 1)

    def test_stale_edit_keeps_draft_until_explicit_refresh_and_retry(self):
        entry = self.service.manual_interest(entry_id='editable', topic='astronomy',
            content='旧内容', category='人工标记')
        self.service.save_manual_interest(entry, 0)
        stale = next(row for row in self.service.entries('interest') if row['id'] == 'editable')
        self.assertEqual(stale['_scope'], self.service.scope)
        self.service.save_manual_edit('interest', stale, '先提交的新内容')
        with self.assertRaises(Conflict):
            self.service.save_manual_edit('interest', stale, '保留的草稿')
        self.assertEqual(self.service.drafts[('interest', 'editable')], '保留的草稿')
        refreshed = next(row for row in self.service.entries('interest')
                         if row['id'] == 'editable')
        self.assertEqual(refreshed['_scope'], self.service.scope)
        self.assertEqual(refreshed['revision'], 2)
        self.service.save_manual_edit('interest', refreshed,
                                      self.service.drafts[('interest', 'editable')])
        self.assertNotIn(('interest', 'editable'), self.service.drafts)
        final = next(row for row in self.service.entries('interest') if row['id'] == 'editable')
        self.assertEqual((final['revision'], final['content']), (3, '保留的草稿'))

    def test_foreign_friend_manual_row_with_same_id_revision_is_rejected(self):
        local = self.service.manual_interest(entry_id='shared-id', topic='本地主题',
            content='items', category='人工标记')
        self.service.save_manual_interest(local, 0)

        other_dir = self.root / 'friends' / '23456'
        other_dir.mkdir(parents=True)
        (other_dir / 'friend.json').write_text(json.dumps({'qq_id': '23456'}), encoding='utf-8')
        registry_path = self.root / 'friends.json'
        registry = json.loads(registry_path.read_text(encoding='utf-8'))
        registry['friends']['23456'] = {'qq_id': '23456', 'directory': str(other_dir)}
        registry_path.write_text(json.dumps(registry), encoding='utf-8')
        from persona_store import PersonaStore
        other_store = PersonaStore('23456', other_dir, project_root=self.root)
        other_store.close()
        other_scope = FriendScope.load('23456', project_root=self.root)
        other_service = PersonaKnowledgeService(other_scope)
        self.addCleanup(other_service.close)
        foreign = other_service.manual_interest(entry_id='shared-id', topic='外部主题',
            content='items', category='人工标记')
        other_service.save_manual_interest(foreign, 0)
        row = next(item for item in other_service.entries('interest')
                   if item['id'] == 'shared-id')
        row['_scope'] = other_scope
        self.assertEqual(row['revision'], 1)

        def scope_revision():
            store = self.service._store('interest', True)
            try:
                return store._store.revision
            finally:
                store.close()

        revision_before = scope_revision()
        with self.assertRaises(ScopeMismatch):
            self.service.save_manual_edit('interest', row, '越权内容')
        # Even a forged GUI scope cannot make the foreign SourceRef belong locally.
        row['_scope'] = self.service.scope
        row.pop('friend_id', None)
        with self.assertRaises(ScopeMismatch):
            self.service.save_manual_edit('interest', row, '越权内容')
        target = next(item for item in self.service.entries('interest')
                      if item['id'] == 'shared-id')
        self.assertEqual((target['revision'], target['content']), (1, 'items'))
        self.assertEqual(scope_revision(), revision_before)

    def test_tombstoned_entry_is_not_returned_by_existing_retriever(self):
        topic = self.service.manual_topic(entry_id='topic-one', topic='星云',
            content='研究星云形成', summary='研究星云形成')
        self.service.save_topic(topic, 0)
        request = RetrievalRequest(friend_id=self.scope.friend_id, query='星云',
            recent_context='', scene='ordinary', plan=None,
            filters=RetrievalFilters(namespace=('knowledge',),
                privacy_scope=DataScope.PRIVATE_FRIEND), budgets=ContextBudgets(),
            allowed_pack_versions=self.scope.allowed_pack_versions,
            now='2026-10-08T12:00:00+00:00')
        from context_retrievers import TopicKnowledgeRetriever
        retriever = TopicKnowledgeRetriever(self.scope)
        self.assertEqual(len(retriever.retrieve(request, 10).candidates), 1)
        row = next(row for row in self.service.entries('topic') if row['id'] == 'topic-one')
        self.assertEqual(self.service.tombstone('topic', row['id'], row['revision']), 2)
        self.assertEqual(retriever.retrieve(request, 10).candidates, ())

    def test_invalidated_public_source_is_not_returned_by_existing_retriever(self):
        import sqlite3
        from contextlib import closing
        from topic_knowledge import TopicKnowledgeStore
        path = self.scope.friend_dir / 'history.sqlite3'
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
            db.execute("INSERT INTO metadata VALUES('qq_id','12345')")
            db.execute('CREATE TABLE topic_library (url TEXT PRIMARY KEY,topic TEXT,summary TEXT,source_title TEXT,day TEXT)')
            db.execute('INSERT INTO topic_library VALUES(?,?,?,?,?)',
                       ('https://example.test/nebula', '星云', '公开研究摘要',
                        '星云资料', '2026-10-08'))
        with TopicKnowledgeStore(self.scope) as store:
            store.mirror_legacy()
        from context_retrievers import TopicKnowledgeRetriever
        request = RetrievalRequest(friend_id=self.scope.friend_id, query='星云',
            recent_context='', scene='ordinary', plan=None,
            filters=RetrievalFilters(namespace=('knowledge',),
                privacy_scope=DataScope.PRIVATE_FRIEND), budgets=ContextBudgets(),
            allowed_pack_versions=self.scope.allowed_pack_versions,
            now='2026-10-08T12:00:00+00:00')
        retriever = TopicKnowledgeRetriever(self.scope)
        self.assertEqual(len(retriever.retrieve(request, 10).candidates), 1)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('DELETE FROM topic_library')
        self.assertEqual(retriever.retrieve(request, 10).candidates, ())

    def test_public_research_is_read_only_and_manual_edit_uses_separate_id(self):
        self.assertFalse(hasattr(self.service, 'mirror_public_research'))
        with self.assertRaises(ValueError):
            self.service.save_topic({'source_type': 'PUBLIC_RESEARCH'}, 0)
        manual = self.service.manual_topic(entry_id='manual-revision', topic='镜像主题',
            content='人工修订', summary='人工修订')
        self.service.save_topic(manual, 0)
        self.assertEqual(self.service.entries('topic')[0]['id'], 'manual-revision')

    def test_public_research_tombstone_is_rejected_by_gui_service(self):
        store = Mock(scope=self.scope)
        store._store.db.execute.return_value.fetchone.return_value = {'source_type': 'PUBLIC_RESEARCH'}
        service = PersonaKnowledgeService(self.scope, topic_store_factory=Mock(return_value=store))
        service._preflight_backup = Mock()
        with self.assertRaises(ValueError):
            service.tombstone('topic', 'legacy-mirror', 1)
        store.tombstone.assert_not_called()

    def test_sticker_snapshot_has_no_retrieval_or_image_validation_entry(self):
        self.assertFalse(hasattr(self.service, 'preview_sticker'))


if __name__ == '__main__':
    unittest.main()
