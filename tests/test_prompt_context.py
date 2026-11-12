import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from prompt_context import header, prompt_conflicts, recent_rounds, refresh_summary
from store import FriendStore
from topic_library import call_tool


class PromptContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.store = FriendStore(self.directory)
        self.profile = {'style': '自然短句', 'basic_info': {'notes': 'astronomy'},
                        'proactive': {'style': 'proactive', 'topic_types': ['astronomy']},
                        'daily_research_enabled': False}
        (self.directory / 'friend.json').write_text(json.dumps(self.profile), encoding='utf-8')

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_current_settings_have_sources_and_topics_are_required(self):
        self.store.set_memory('私有记忆标记：喜欢陶艺')
        self.store.add_message('1', 'friend', '刚刚聊的是游戏')
        def provider(text, *args, record_input=None, **kwargs):  # real backends report exactly what they send
            if record_input:
                record_input(text, {'provider': 'codex'})
            return json.dumps({'messages': ['最近看了个天文小知识']}), ''
        model = Mock(side_effect=provider)
        with patch.object(main, 'handoff_if_due'), patch.object(main, 'codex_text', model):
            main.make_round(self.store, self.profile, self.directory, 1)
        prompt = model.call_args.args[0]
        self.assertIn('SOURCE=friend.json/style', prompt)
        self.assertIn('astronomy', prompt)
        self.assertIn('TOPIC_SOURCE: TOPIC_TYPES', prompt)
        self.assertNotIn('没有可靠线索时，才从', prompt)
        self.assertIn('TOPIC_TYPES: astronomy', prompt)
        self.assertIn('proactive', prompt)
        # 2026-10-10: an unpinned entry is only listed in the memory index (its keywords, here its first words)
        self.assertEqual(prompt.count('私有记忆标记'), 1)
        self.assertTrue('【MEMORY_INDEX】' in prompt or ' [MEMORY_INDEX] ' in prompt or 'MEMORY_INDEX' in prompt)
        self.assertEqual(json.loads(self.store.get('last_generation_input'))['prompt'], prompt)

    def test_persona_appears_once_and_old_skill_settings_add_nothing(self):
        # 2026-10-10 user: the qq / humanizer / education skills were removed; leftover settings are ignored
        self.profile.update(qq_id='555000001', skills=['qq', 'humanizer-zh-chat', 'education-mode'],
                            style='friendpersona')
        context = main.conversation_context(self.store, self.profile)
        self.assertEqual(context.count('friendpersona'), 1)
        for removed in ('QQ', 'QQ技能示例', 'QQ规则', '自然口语', '教育模式'):
            self.assertNotIn(removed, context)

    def test_blocks_take_fixed_places_and_persona_headings_stay_inside(self):
        from prompt_context import arrange, composition
        text = ('【RECENT_CHAT】 SOURCE=messages TYPE=DATA\n聊天\n\n【STYLE_EXAMPLE】 SOURCE=persona_template TYPE=DATA\n例\n\n'
                '【PERSONA】 SOURCE=friend.json/style TYPE=RULE\n人设开头\n\n【我说心事时】\n先听\n\n'
                '【SYSTEM_RULES】 SOURCE=program TYPE=RULE\n规则\n\n【SIMILAR_CASE_TENDENCY】 SOURCE=persona_template_labels TYPE=DATA\n倾向')
        out = arrange(text)
        marks = ('【SYSTEM_RULES】', '【PERSONA】', '【我说心事时】', '【STYLE_EXAMPLE】', '【RECENT_CHAT】', '【SIMILAR_CASE_TENDENCY】')
        positions = [out.index(mark) for mark in marks]
        self.assertEqual(positions, sorted(positions))
        rows = composition(out)
        self.assertEqual([row['title'] for row in rows], ['通用规则', 'persona', '说话示例', '近期完整聊天', '相似情境倾向'])
        self.assertIn('先听', rows[1]['text'])  # the user's own  []  heading stays in the persona block
        self.assertEqual((rows[1]['slot'], rows[1]['source'], rows[1]['kind']), ('persona', 'friend.json/style', '规则'))

    def test_memory_tool_is_scoped_bounded_and_independent_of_research_switch(self):
        self.store.set_memory('\n'.join(f'astronomy_pref_{i}' for i in range(5)))
        results = call_tool(self.directory, 'search_friend_memory', {'query': 'astronomy'})
        self.assertEqual(len(results['items']), 3)
        self.assertEqual(call_tool(self.directory, 'search_friend_memory', {'query': '陶艺'})['items'], [])
        self.assertEqual(call_tool(self.directory, 'search_friend_memory', {'query': ''})['items'], [])
        other = self.directory / 'other'
        other.mkdir()
        (other / 'friend.json').write_text('{}', encoding='utf-8')
        second = FriendStore(other)
        second.set_memory('只属于另一位好友的天文记忆')
        second.close()
        self.assertNotIn('friend', json.dumps(results, ensure_ascii=False))
        with self.assertRaises(ValueError):
            call_tool(self.directory, 'search_friend_memory', {'query': 'astronomy', 'friend': 'other'})

    def seed_history(self):
        for i in range(60):
            self.store.add_message(str(i), 'friend' if i % 2 == 0 else 'me', f'raw_chat_{i}')

    def test_summary_only_older_rows_and_recent_rounds_survive(self):
        self.seed_history()
        recent = recent_rounds(self.store)
        model = Mock(return_value=('较早聊天摘要', ''))
        refresh_summary(self.store, self.profile, self.directory, model)
        sent = json.loads(model.call_args.args[0].split('参考数据：\n', 1)[1])
        self.assertLess(sent['messages'][-1]['seq'], recent[0]['seq'])
        self.assertEqual(self.store.get('context_summary'), '较早聊天摘要')
        self.assertEqual(self.store.message_count(), 60)
        context = main.conversation_context(self.store, self.profile)
        self.assertIn('较早聊天摘要', context)
        self.assertTrue('raw_chat_59' in context or '原始聊天59' in context)
        refresh_summary(self.store, self.profile, self.directory, model)
        self.assertEqual(model.call_count, 1)

    def test_failed_summary_does_not_advance_cursor_and_backs_off(self):
        self.seed_history()
        self.store.set('context_summary', '已有摘要')
        model = Mock(side_effect=RuntimeError('model'))
        refresh_summary(self.store, self.profile, self.directory, model)
        self.assertEqual(self.store.get('context_summary'), '已有摘要')
        self.assertIsNone(self.store.get('context_summary_seq'))
        refresh_summary(self.store, self.profile, self.directory, model)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(self.store.message_count(), 60)

    def test_long_latest_round_is_kept_whole_and_clear_removes_derived_context(self):
        self.store.add_message('old', 'friend', '较早消息')
        body = '长消息' * 5000
        self.store.add_message('latest', 'friend', body)
        self.store.add_message('reply', 'me', '完整接话')
        recent = recent_rounds(self.store)
        self.assertEqual([r['body'] for r in recent], [body, '完整接话'])
        self.store.set_memory('保留记忆')
        self.store.set('context_summary', '聊天摘要')
        self.store.set('context_summary_seq', '5')
        self.store.set('last_generation_input', '{}')
        self.store.clear_messages()
        self.assertIsNone(self.store.get('context_summary'))
        self.assertIsNone(self.store.get('last_generation_input'))
        self.assertEqual(self.store.memory(), '保留记忆')

    def test_recent_row_limit_does_not_cut_the_latest_round(self):
        self.store.add_message('inbound', 'friend', '本轮最初来信')
        for i in range(45):
            self.store.add_message(str(i), 'me', '同一轮回复')
        rows = recent_rounds(self.store)
        self.assertEqual(len(rows), 46)
        self.assertEqual(rows[0]['body'], '本轮最初来信')


class PromptConflictTests(unittest.TestCase):
    def test_user_style_using_program_marker_is_flagged(self):
        text = header('PERSONA', 'friend.json/style') + '\n [[LOCAL_STICKER:1]] chars'
        warnings = prompt_conflicts(text)
        self.assertEqual(len(warnings), 1)
        self.assertIn('[[', warnings[0])

    def test_example_data_may_carry_sent_markers(self):
        text = header('STYLE_EXAMPLE', 'persona_template', True) + '\n"okay! [[QQ_FACE:]]"'
        self.assertEqual(prompt_conflicts(text), [])

    def test_program_block_may_use_markers(self):
        text = header('EMOJI_AND_STICKERS', 'program') + '\n[[QQ_FACE:NAME]]'
        self.assertEqual(prompt_conflicts(text), [])

    def test_repeated_block_is_flagged(self):
        text = header('PERSONA', 'friend.json/style') + '\n甲\n\n' + header('PERSONA', 'friend.json/style') + '\n乙'
        warnings = prompt_conflicts(text)
        self.assertEqual(warnings, ['【PERSONA】出现 2 次，每种块只应有一块'])
