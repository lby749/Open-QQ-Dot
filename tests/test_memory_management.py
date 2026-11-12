import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
import memory_management
from store import FriendStore
from memory_management import entries, refresh, core_blocks, edit_entry
from topic_library import call_tool

BATCH = 12
OLD = '2026-01-01T10:00:00+08:00'  # a conversation that has ended (quiet for over IDLE_SECONDS)


class MemoryManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        (self.directory / 'friend.json').write_text('{}', encoding='utf-8')
        for i in range(BATCH):
            self.store.add_message(str(i), 'friend', f'friend_msg_{i}', OLD)
        self.twins = patch.object(memory_management, '_twins', return_value={})  # no embedder; see the dedupe test
        self.twins.start()
        self.addCleanup(self.twins.stop)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def change(self, op='add', identifier='', text='偏好短回复', sources=None):
        return {'op': op, 'id': identifier, 'text': text, 'kind': 'preference',
                'keywords': ['句子长度', '回复长短'], 'sources': sources or ['0']}

    def model(self, changes):
        return Mock(return_value=(json.dumps({'changes': changes}, ensure_ascii=False), None))

    def test_incremental_add_preserves_old_facts_and_exposes_sources(self):
        self.store.set_memory('旧事实不可遗漏')
        model = self.model([self.change()])
        refresh(self.store, {}, self.directory, model)
        self.assertIn('旧事实不可遗漏', self.store.memory())
        found = call_tool(self.directory, 'search_friend_memory', {'query': '句子长度'})['items']
        self.assertEqual(found[0]['text'], '偏好短回复')
        self.assertEqual(found[0]['sources'], ['0'])
        self.assertTrue(model.call_args.kwargs['text_only'])

    def test_concurrent_manual_edit_wins_and_cursor_does_not_advance(self):
        self.store.set_memory('旧偏好')
        def generate(*args, **kwargs):
            self.store.set_memory('手动纠正', locked=True)
            return json.dumps({'changes': [self.change()]}), None
        refresh(self.store, {}, self.directory, generate)
        self.assertEqual(self.store.memory(), '手动纠正')
        self.assertIsNone(self.store.get('memory_processed_seq'))
        self.assertTrue(self.store.get('memory_retry_at'))

    def test_locked_memory_is_core_and_cannot_be_updated(self):
        self.store.set_memory('不要每句提问', locked=True)
        item = entries(self.store)[0]
        refresh(self.store, {}, self.directory, self.model([self.change('update', item['id'])]))
        self.assertEqual(self.store.memory(), '不要每句提问')
        self.assertEqual(core_blocks(self.store), {'FRIEND_PORTRAIT': '手动：不要每句提问'})

    def test_valid_update_and_retire_keep_versions(self):
        self.store.set_memory('旧的爱好')
        identifier = entries(self.store)[0]['id']
        refresh(self.store, {}, self.directory, self.model([self.change('update', identifier)]))
        self.assertEqual(self.store.memory(), '偏好短回复')
        self.store.restore_memory()
        self.assertEqual(self.store.memory(), '旧的爱好')
        self.store.set('memory_processed_seq', '0')
        refresh(self.store, {}, self.directory, self.model([self.change('retire', identifier, '已明确不再喜欢')]), force=True)
        self.assertEqual(self.store.memory(), '')
        self.assertEqual(call_tool(self.directory, 'search_friend_memory', {'query': '喜欢'})['items'], [])

    def test_empty_changes_preserve_existing_memory(self):
        self.store.set_memory('原有事实')
        refresh(self.store, {}, self.directory, self.model([]))
        self.assertEqual(self.store.memory(), '原有事实')
        self.assertEqual(self.store.get('memory_refresh_count'), str(BATCH))

    def test_invalid_changes_are_dropped_and_the_rest_of_the_batch_counts(self):
        # 2026-10-10: one bad change (a real replay mistyped a source id) no longer fails the whole batch forever
        changes = [self.change(sources=['another-friend']), self.change(text='密码：123456'), {'op': 'add'},
                   self.change(text='有效条目', sources=[1, 999])]  # seq 1 = message '0'; an unknown seq is dropped
        refresh(self.store, {}, self.directory, self.model(changes))
        self.assertEqual(self.store.memory(), '有效条目')
        self.assertEqual(json.loads(entries(self.store)[0]['sources']), ['0'])
        self.assertTrue('丢弃 3 条' in self.store.get('memory_status') or '3 items' in self.store.get('memory_status'))
        self.assertEqual(self.store.get('memory_processed_seq'), str(BATCH))
        self.store.add_message('later', 'friend', '后来的一句', OLD)
        refresh(self.store, {}, self.directory, Mock(return_value=('不是 JSON', None)), force=True)
        self.assertTrue('未提交' in self.store.get('memory_status') or 'Submit' in self.store.get('memory_status'))  # an unreadable answer still retries

    def test_backlog_processed_in_order_not_marked_complete_early(self):
        self.store.clear_messages()
        for i in range(30):
            self.store.add_message('in' + str(i), 'friend', '来信' + str(i), OLD)
            for j in range(4):
                self.store.add_message(f'out{i}-{j}', 'me', '回复', OLD)
        model = self.model([])
        refresh(self.store, {}, self.directory, model)
        payload = json.loads(model.call_args.args[0].split('\nDATA: ')[1])
        self.assertEqual(payload['messages'][0]['body'], '来信0')
        self.assertNotIn('qq_id', payload['messages'][0])  # sources are cited by seq
        self.assertEqual(self.store.get('memory_refresh_count'), '16')
        refresh(self.store, {}, self.directory, model)  # still quiet: the backlog continues
        self.assertEqual(self.store.get('memory_refresh_count'), '30')

    def test_clear_chat_retains_memory_and_allows_next_batch(self):
        self.store.set_memory('记忆保留')
        refresh(self.store, {}, self.directory, self.model([]))
        self.store.clear_messages()
        for i in range(BATCH):
            self.store.add_message('new' + str(i), 'friend', '新消息', OLD)
        model = self.model([])
        refresh(self.store, {}, self.directory, model)
        self.assertTrue(model.called)
        self.assertEqual(self.store.memory(), '记忆保留')

    def test_reset_is_separate_clears_versions_and_excludes_old_history(self):
        self.store.set_memory('旧记忆')
        self.store.set('thread_id', 'original-thread')
        self.store.set('daily_research', 'preserved')
        count = self.store.message_count()
        self.store.reset_memory()
        self.assertEqual(self.store.memory(), '')
        self.assertEqual(self.store.message_count(), count)
        self.assertEqual(self.store.get('thread_id'), 'original-thread')
        self.assertEqual(self.store.get('daily_research'), 'preserved')
        with self.assertRaises(ValueError):
            self.store.restore_memory()
        model = self.model([])
        refresh(self.store, {}, self.directory, model, force=True)
        model.assert_not_called()

    def test_reset_during_generation_rejects_inflight_memory(self):
        def generate(*args, **kwargs):
            self.store.reset_memory()
            return json.dumps({'changes': [self.change()]}), None
        refresh(self.store, {}, self.directory, generate)
        self.assertEqual(self.store.memory(), '')

    def test_failure_is_contained_and_old_memory_remains(self):
        self.store.set_memory('旧记忆')
        refresh(self.store, {}, self.directory, Mock(side_effect=RuntimeError('模拟超时')))
        self.assertEqual(self.store.memory(), '旧记忆')
        self.assertIsNone(self.store.get('memory_processed_seq'))

    def test_manual_save_rejects_stale_revision(self):
        revision = self.store.memory_revision()
        self.store.set_memory('新内容')
        with self.assertRaises(ValueError):
            self.store.set_memory('过时编辑', locked=True, expected_revision=revision)
        self.assertEqual(self.store.memory(), '新内容')

    def test_manual_unlock_allows_later_evidenced_correction(self):
        self.store.set_memory('旧偏好', locked=True)
        identifier = entries(self.store)[0]['id']
        edit_entry(self.store, identifier, self.store.memory_revision(), locked=False)
        refresh(self.store, {}, self.directory, self.model([self.change('update', identifier)]))
        self.assertEqual(self.store.memory(), '偏好短回复')

    def test_legacy_migration_preserves_text_and_timestamp_without_inventing_sources(self):
        self.store.db.execute('DELETE FROM memory_entries')
        self.store.db.execute("DELETE FROM metadata WHERE key='memory_entries_initialized'")
        self.store.db.execute("INSERT INTO memories(body,updated_at) VALUES('旧事实一\n旧事实二','2026-01-01')")
        self.store.db.commit()
        self.store.close()
        self.store = FriendStore(self.directory)
        self.assertEqual(self.store.memory(), '旧事实一\n旧事实二')
        self.assertEqual(self.store.db.execute('SELECT updated_at FROM memories').fetchone()[0], '2026-01-01')
        self.assertEqual([item['sources'] for item in entries(self.store)], ['[]', '[]'])
        self.assertTrue(all(item['kind'] == 'imported' for item in entries(self.store)))

    def test_update_keeps_old_entry_invalid_and_slots_build_core_blocks(self):
        # 2026-10-10 5stage: Graphiti-style validity + Memobase slots + Letta core blocks
        self.store.set_memory('喜欢猫')
        old = entries(self.store)[0]['id']
        changes = [dict(self.change('update', old, '现在更喜欢狗'), category='兴趣'),
                   dict(self.change(text='我们约好周末一起打游戏'), category='关系'),
                   dict(self.change(text='下周要期末考试'), category='近况', expires_at='2099-01-01'),
                   dict(self.change(text='不知道放哪'), category='不存在的格子')]
        refresh(self.store, {}, self.directory, self.model(changes))
        rows = {row['text']: row for row in entries(self.store)}
        self.assertFalse(rows['喜欢猫']['active'])
        self.assertTrue(rows['喜欢猫']['invalid_at'])
        self.assertEqual(rows['喜欢猫']['replaced_by'], rows['现在更喜欢狗']['id'])
        self.assertEqual(rows['不知道放哪']['category'], 'other')
        blocks = core_blocks(self.store)
        self.assertEqual(blocks['FRIEND_PORTRAIT'], '兴趣：现在更喜欢狗')  # other stays on demand
        self.assertEqual(blocks['RELATIONSHIP_STATE'], '关系：我们约好周末一起打游戏')
        self.assertEqual(blocks['RECENT_STATE'], '近况：下周要期末考试')

    def test_consolidates_only_after_the_conversation_ends(self):
        # 2026-10-10 user: while chatting the recent rounds are the short-term memory; consolidate once it has ended
        refresh(self.store, {}, self.directory, self.model([]))
        self.store.add_message('now1', 'friend', '刚说的一句')
        self.store.add_message('now2', 'me', '刚回的一句')
        model = self.model([])
        for i in range(40):  # however many messages: no consolidation mid-conversation
            self.store.add_message(f'chat{i}', 'friend', f'chatting_{i}')
        refresh(self.store, {}, self.directory, model)
        model.assert_not_called()
        with patch.object(memory_management, 'IDLE_SECONDS', 0):  # nothing said for IDLE_SECONDS
            refresh(self.store, {}, self.directory, model)
        self.assertTrue(model.called)
        self.assertIn(' [MEMORY_CONSOLIDATION] ', model.call_args.args[0])
        self.assertIn('KEYWORDS:', model.call_args.args[0])

    # ---------- 2026-10-10 user: four memory layers ----------

    def test_saving_text_keeps_each_entrys_own_lock(self):
        refresh(self.store, {}, self.directory, self.model([self.change()]))  # a model entry, unlocked
        self.store.set_memory('偏好短回复\n手写的', locked=True, expected_revision=self.store.memory_revision())
        rows = {row['text']: row for row in entries(self.store) if row['active']}
        self.assertFalse(rows['偏好短回复']['locked'])  # the checkbox no longer locks every kept line
        self.assertTrue(rows['手写的']['locked'])
        self.store.set_memory('偏好短回复\n手写的\n[已查实用语] 滚木：梗')  # as save_learned_terms does
        self.assertTrue({row['text']: row for row in entries(self.store)}['手写的']['locked'])

    def test_entry_lock_slot_and_retire_from_the_page(self):
        self.store.set_memory('喜欢猫')
        identifier = entries(self.store)[0]['id']
        edit_entry(self.store, identifier, self.store.memory_revision(), category='兴趣')
        self.assertEqual(core_blocks(self.store), {'FRIEND_PORTRAIT': '兴趣：喜欢猫'})
        edit_entry(self.store, identifier, self.store.memory_revision(), locked=True)
        self.assertTrue(entries(self.store)[0]['locked'])
        with self.assertRaises(ValueError):
            edit_entry(self.store, identifier, self.store.memory_revision() - 1, retire=True)
        edit_entry(self.store, identifier, self.store.memory_revision(), retire=True)
        self.assertEqual(self.store.memory(), '')
        self.assertTrue(entries(self.store)[0]['invalid_at'])

    def test_dates_are_the_message_time_and_recent_news_expires(self):
        self.store.clear_messages()
        self.store.record_inbound('exam', '下周要考试', '2026-01-05T10:00:00+08:00')
        self.store.add_message('promise', 'me', '那我考前一天提醒你')
        changes = [dict(self.change(text='1月12日期末考试', sources=['exam']), kind='plan', category='近况',
                        expires_at='2026-01-12'),
                   dict(self.change(text='平时爱喝奶茶', sources=['exam']), kind='preference', category='兴趣',
                        expires_at=''),
                   dict(self.change(text='我答应1月11日提醒好友考试', sources=['promise']), kind='plan', category='关系')]
        refresh(self.store, {}, self.directory, self.model(changes), force=True)
        rows = {row['text']: row for row in entries(self.store)}
        self.assertEqual(rows['1月12日期末考试']['valid_from'], '2026-01-05T10:00:00+08:00')
        self.assertEqual(rows['1月12日期末考试']['expires_at'], '2026-01-12')
        self.assertIsNone(rows['平时爱喝奶茶']['expires_at'])
        self.assertTrue(rows['我答应1月11日提醒好友考试']['expires_at'])  # a plan without a date: RECENT_DAYS
        self.assertEqual(core_blocks(self.store), {'FRIEND_PORTRAIT': '兴趣：平时爱喝奶茶',  # the exam is past its day
                                                   'RELATIONSHIP_STATE': '关系：我答应1月11日提醒好友考试'})
        self.assertEqual(memory_management.expire(self.store), 1)
        self.assertEqual(self.store.memory(), '平时爱喝奶茶\n我答应1月11日提醒好友考试')

    def test_our_messages_are_a_source_only_for_our_promise(self):
        self.store.add_message('mine', 'me', '我觉得猫很可爱')
        refresh(self.store, {}, self.directory, self.model([self.change(text='喜欢猫', sources=['mine'])]), force=True)
        self.assertEqual(self.store.memory(), '')
        self.assertTrue('丢弃 1 条' in self.store.get('memory_status') or '1 items' in self.store.get('memory_status'))

    def test_write_time_dedupe_merges_twins_and_asks_about_close_ones(self):
        self.twins.stop()
        refresh(self.store, {}, self.directory, self.model([dict(self.change(text='平时很喜欢喝奶茶'), category='兴趣'),
                                                            dict(self.change(text='姐姐下个月结婚'), category='重要的人')]))
        ids = {row['text']: row['id'] for row in entries(self.store)}
        self.store.add_message('more', 'friend', '新的来信', OLD)
        old = {ids['平时很喜欢喝奶茶']: (1.0, 0.0), ids['姐姐下个月结婚']: (0.0, 1.0)}
        new = {'很爱喝奶茶': (1.0, 0.0), '姐姐下月办婚礼': (0.6, 0.8), '表哥下个月结婚': (0.6, 0.8)}
        changes = [dict(self.change(text=text, sources=['more']), category='兴趣') for text in new]
        decisions = {'decisions': [{'new': 1, 'verdict': 'same', 'old': ids['姐姐下个月结婚']},
                                   {'new': 2, 'verdict': 'different', 'old': ''}]}
        model = Mock(side_effect=[(json.dumps({'changes': changes}, ensure_ascii=False), None),
                                  (json.dumps(decisions), None)])
        with patch.object(memory_management, '_vectors', side_effect=lambda d, texts: {k: old[k] for k in texts}), \
                patch('embedding_provider.embed_unscoped', side_effect=lambda texts, *a, **k: [new[t] for t in texts]):
            refresh(self.store, {}, self.directory, model)
        # cosine 1.0 -> the program updates; 0.8 (between DUP_ASK and DUP_SAME) -> the model was asked
        self.assertIn(' [MEMORY_DEDUPE] ', model.call_args.args[0])
        self.assertEqual(self.store.memory(), '姐姐下个月结婚\n很爱喝奶茶\n表哥下个月结婚')
        self.assertEqual({row['id']: row for row in entries(self.store)}[ids['平时很喜欢喝奶茶']]['replaced_by'],
                         {row['text']: row for row in entries(self.store)}['很爱喝奶茶']['id'])

    def test_core_blocks_hold_whole_entries_and_leave_the_rest_to_recall(self):
        texts = ['约好每周五晚上一起打排位' + '甲' * 150, '说好周末一起去看新上映的电影' + '乙' * 150, '互相叫对方小名' + '丙' * 150]
        refresh(self.store, {}, self.directory, self.model([dict(self.change(text=text), category='关系') for text in texts]))
        text = core_blocks(self.store)['RELATIONSHIP_STATE']
        self.assertEqual(text, '关系：' + texts[0] + '；' + texts[1])  # whole entries; the third does not fit 400
        with patch('embedding_provider.embed_unscoped', side_effect=RuntimeError('no model')), \
                patch('reranker.score', side_effect=lambda query, passages: [0.9] * len(passages)):
            self.assertIn('互相叫对方小名', memory_management.recall_block(self.directory, '小名'))

    def test_tool_finds_a_chinese_sentence_without_spaces(self):
        refresh(self.store, {}, self.directory, self.model([dict(self.change(text='周末常去爬山'), keywords=['户外'])]))
        found = call_tool(self.directory, 'search_friend_memory', {'query': '你周末一般干什么'})['items']
        self.assertEqual([item['text'] for item in found], ['周末常去爬山'])
        self.assertEqual(call_tool(self.directory, 'search_friend_memory', {'query': '末日电影'})['items'], [])

    def test_recall_brings_matching_entries_outside_the_core_blocks(self):
        self.store.set_memory('养了一只叫团子的橘猫\n喜欢吃辣')
        edit_entry(self.store, entries(self.store)[1]['id'], self.store.memory_revision(), category='兴趣')
        with patch('embedding_provider.embed_unscoped', side_effect=RuntimeError('no model')), \
                patch('reranker.score', side_effect=lambda query, texts: [0.9 if '团子' in t else 0.01 for t in texts]):
            block = memory_management.recall_block(self.directory, '团子今天又拆家了')
        self.assertTrue(block.startswith('other（') or block.startswith('其他（') or block.startswith('other (') or block.startswith('oth'))
        self.assertIn('团子的橘猫', block)
        self.assertNotIn('喜欢吃辣', block)  # pinned in FRIEND_PORTRAIT already
        # the unpinned entry is also listed in the index (no keywords here: its first words)
        self.assertEqual(core_blocks(self.store)['MEMORY_INDEX'], '养了一只叫团子的橘猫')

    def test_upkeep_runs_off_the_calling_thread(self):
        model = self.model([self.change()])
        job = memory_management.queue_refresh(self.store, {}, self.directory, model)
        self.assertIsNotNone(job)
        job.join(30)
        self.assertEqual(self.store.memory(), '偏好短回复')
        self.assertIsNone(memory_management.queue_refresh(self.store, {}, self.directory, model))  # nothing due

    def test_versions_are_capped(self):
        for i in range(memory_management.VERSIONS_KEPT + 5):
            self.store.set_memory(f'version_{i}')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM memory_versions').fetchone()[0],
                         memory_management.VERSIONS_KEPT)